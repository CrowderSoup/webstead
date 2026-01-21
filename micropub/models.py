from django.db import models

from blog.models import Post


class Webmention(models.Model):
    MENTION = "mention"
    REPLY = "reply"
    REPOST = "repost"
    LIKE = "like"
    MENTION_CHOICES = [
        (MENTION, "Mention"),
        (REPLY, "Reply"),
        (REPOST, "Repost"),
        (LIKE, "Like"),
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

    source = models.URLField()
    target = models.URLField()
    mention_type = models.CharField(max_length=16, choices=MENTION_CHOICES, default=MENTION)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=PENDING)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    target_post = models.ForeignKey(Post, null=True, blank=True, on_delete=models.SET_NULL)
    error = models.TextField(blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.source} -> {self.target}"


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
