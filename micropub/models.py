from django.db import models

from blog.models import InvalidTransition, Post


class WebmentionQuerySet(models.QuerySet):
    def awaiting_moderation(self):
        return self.filter(is_incoming=True, status=Webmention.PENDING)


class Webmention(models.Model):
    MENTION = "mention"
    REPLY = "reply"
    REPOST = "repost"
    LIKE = "like"
    BOOKMARK = "bookmark"
    MENTION_CHOICES = [
        (MENTION, "Mention"),
        (REPLY, "Reply"),
        (REPOST, "Repost"),
        (LIKE, "Like"),
        (BOOKMARK, "Bookmark"),
    ]

    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    TIMED_OUT = "timed_out"
    STATUS_CHOICES = [
        (PENDING, "Pending"),
        (ACCEPTED, "Accepted"),
        (REJECTED, "Rejected"),
        (TIMED_OUT, "Timed out"),
    ]

    # Incoming and outgoing webmentions share the status field but run
    # different state machines:
    #
    # Incoming: (re)received -> PENDING, from any state, since a sender
    #   re-sending means "check again". PENDING -> ACCEPTED | REJECTED, either
    #   by source verification or by a moderator. Verification of a trusted
    #   source accepts; an untrusted one stays PENDING for moderation.
    #   TIMED_OUT never applies.
    # Outgoing: each send attempt's result is the state (any of the four).
    #   PENDING and TIMED_OUT can be re-sent.
    RESENDABLE = {PENDING, TIMED_OUT}

    source = models.URLField(max_length=2000)
    target = models.URLField(max_length=2000)
    mention_type = models.CharField(max_length=16, choices=MENTION_CHOICES, default=MENTION)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=PENDING)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    target_post = models.ForeignKey(Post, null=True, blank=True, on_delete=models.SET_NULL)
    error = models.TextField(blank=True)
    is_incoming = models.BooleanField(default=True)

    objects = WebmentionQuerySet.as_manager()

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["source", "target"], name="unique_webmention_source_target"),
        ]

    def __str__(self):
        return f"{self.source} -> {self.target}"

    @property
    def can_moderate(self):
        return self.is_incoming and self.status == self.PENDING

    @property
    def can_resend(self):
        return self.status in self.RESENDABLE

    @property
    def can_delete(self):
        """Outgoing records can always go; incoming ones only once rejected."""
        return not self.is_incoming or self.status == self.REJECTED

    def _set_status(self, status, error):
        self.status = status
        self.error = error
        self.save(update_fields=["status", "error", "updated_at"])

    def accept(self):
        if not self.can_moderate:
            raise InvalidTransition(self.status, self.ACCEPTED)
        self._set_status(self.ACCEPTED, "")

    def reject(self, error="Rejected by admin"):
        if not self.can_moderate:
            raise InvalidTransition(self.status, self.REJECTED)
        self._set_status(self.REJECTED, self.error or error)

    def record_verification(self, *, verified, trusted, error=""):
        """Apply the result of checking that the source links to the target.

        Only a PENDING incoming mention takes it: if a moderator decided while
        verification was queued, their decision stands. Returns whether the
        result was applied.
        """
        if not self.can_moderate:
            return False
        if not verified:
            status = self.REJECTED
        else:
            status = self.ACCEPTED if trusted else self.PENDING
        self._set_status(status, error or "")
        return True

    def record_send_result(self, status, error=""):
        """Store the outcome of (re)sending an outgoing webmention."""
        if self.is_incoming:
            raise InvalidTransition(self.status, status)
        self._set_status(status, error)


class MicropubRequestLog(models.Model):
    method = models.CharField(max_length=10)
    path = models.CharField(max_length=255)
    status_code = models.PositiveSmallIntegerField()
    error = models.TextField(blank=True)
    request_headers = models.JSONField(default=dict)
    request_query = models.JSONField(default=dict)
    request_body = models.TextField(blank=True)
    response_body = models.TextField(blank=True)
    remote_addr = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(blank=True)
    content_type = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.method} {self.path} -> {self.status_code}"
