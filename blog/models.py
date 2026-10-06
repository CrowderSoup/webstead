import markdown
from datetime import datetime

from django.conf import settings
from django.db import models
from django.utils.text import slugify, Truncator
from django.utils.safestring import mark_safe
from django.utils.html import strip_tags
from django.utils import timezone
from django.urls import reverse
from django.contrib.contenttypes.fields import GenericRelation

from files.models import Attachment


class Tag(models.Model):
    tag = models.SlugField(max_length=64, unique=True)
    
    def __str__(self):
        return self.tag

    class Meta:
        ordering = ['tag']

class PostQuerySet(models.QuerySet):
    def live(self):
        """Posts the public can see: not deleted, with a publish time that has passed."""
        return self.filter(deleted=False, published_on__lte=timezone.now())


class Post(models.Model):
    ARTICLE = "article"; NOTE = "note"; PHOTO = "photo"; ACTIVITY = "activity"; LIKE = "like"; REPOST = "repost"; REPLY = "reply"; EVENT = "event"; RSVP = "rsvp"; CHECKIN = "checkin"; BOOKMARK = "bookmark"
    KIND_CHOICES = [
        (ARTICLE, "Article"),
        (NOTE, "Note"),
        (PHOTO, "Photo"),
        (ACTIVITY, "Activity"),
        (LIKE, "Like"),
        (REPOST, "Repost"),
        (REPLY, "Reply"),
        (EVENT, "Event"),
        (RSVP, "RSVP"),
        (CHECKIN, "Check-in"),
        (BOOKMARK, "Bookmark"),
    ]

    title = models.CharField(max_length=512)
    slug = models.SlugField(max_length=255, unique=True)
    kind = models.CharField(max_length=16, choices=KIND_CHOICES, default=ARTICLE)
    author = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    content = models.TextField()
    mf2 = models.JSONField(default=dict, blank=True)
    deleted = models.BooleanField(default=False)
    published_on = models.DateTimeField("date published", null=True, blank=True)
    tags = models.ManyToManyField(Tag)
    attachments = GenericRelation(Attachment, related_query_name="posts")
    like_of = models.URLField(blank=True)
    repost_of = models.URLField(blank=True)
    in_reply_to = models.URLField(blank=True)
    bookmark_of = models.URLField(blank=True)
    mastodon_syndicate = models.BooleanField(
        null=True,
        blank=True,
        help_text=(
            "Override Mastodon syndication for this post. "
            "Null = use the per-kind default from MastodonSyndicationDefault."
        ),
    )
    went_live_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text=(
            "When go-live side effects (webmentions, syndication) were queued. "
            "Null on drafts and scheduled posts that haven't reached their time."
        ),
    )

    objects = PostQuerySet.as_manager()

    def __str__(self):
        return self.title

    def save(self, *args, **kwargs):
        timestamp = int(timezone.now().timestamp())

        if not self.slug:
            # Leave room for the timestamp and a de-dupe suffix under max_length.
            base = slugify(self.title)[:200].strip("-") or self.kind
            self.slug = self._unique_slug(f"{base}-{timestamp}")

        if not self.title:
            base_titles = {
                Post.NOTE: "Note",
                Post.PHOTO: "Photo",
                Post.ACTIVITY: "Activity",
                Post.LIKE: "Like",
                Post.REPOST: "Repost",
                Post.REPLY: "Reply",
                Post.EVENT: "Event",
                Post.RSVP: "RSVP",
                Post.CHECKIN: "Check-in",
                Post.BOOKMARK: "Bookmark",
            }
            self.title = f"{base_titles.get(self.kind, 'Article')}: {timestamp}"

        super().save(*args, **kwargs)

    def _unique_slug(self, base):
        """``base``, or ``base-2``, ``base-3``… if another post already has it.

        Untitled posts saved in the same second (a fast Micropub client or an
        agent) would otherwise share a slug and fail the unique constraint.
        """
        slug = base
        suffix = 2
        while Post.objects.exclude(pk=self.pk).filter(slug=slug).exists():
            slug = f"{base}-{suffix}"
            suffix += 1
        return slug

    def get_absolute_url(self):
        return reverse("post", kwargs={"slug": self.slug})

    def html(self):
        md = markdown.Markdown(extensions=["fenced_code"])
        return mark_safe(md.convert(self.content))

    def summary(self):
        md = markdown.Markdown(extensions=["fenced_code"])

        html = md.convert(self.content)
        text = strip_tags(html)

        return Truncator(text).chars(500, truncate="...")
    
    def is_published(self):
        return self.published_on is not None

    def is_live(self):
        """Visible to the public: not deleted and the publish time has passed."""
        return (
            not self.deleted
            and self.published_on is not None
            and self.published_on <= timezone.now()
        )

    @property
    def photo_attachments(self):
        return self.attachments.select_related("asset").filter(role="photo")

    @property
    def gpx_attachment(self):
        return self.attachments.select_related("asset").filter(role="gpx").first()
    
    class Meta:
        ordering = ['-published_on']


class PostRevision(models.Model):
    """A post as it was just before one change, and who made that change.

    Written by ``blog.services`` for every mutation. The newest revision plus
    the live row give the latest diff; ``services.revert_to`` restores a
    revision's snapshot. A ``create`` revision has no snapshot (there was no
    post before it) and only records who created the post.
    """

    CREATE = "create"; UPDATE = "update"; STATUS = "status"; DELETE = "delete"; UNDELETE = "undelete"; REVERT = "revert"
    ACTION_CHOICES = [
        (CREATE, "Create"),
        (UPDATE, "Update"),
        (STATUS, "Status change"),
        (DELETE, "Delete"),
        (UNDELETE, "Undelete"),
        (REVERT, "Revert"),
    ]

    post = models.ForeignKey(Post, on_delete=models.CASCADE, related_name="revisions")
    created_at = models.DateTimeField(auto_now_add=True)
    action = models.CharField(max_length=16, choices=ACTION_CHOICES)
    change_summary = models.CharField(max_length=255, blank=True)
    snapshot = models.JSONField(null=True, blank=True)
    actor_source = models.CharField(max_length=16)
    actor_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    token = models.ForeignKey(
        "indieauth.IndieAuthAccessToken",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    client_id = models.CharField(max_length=2000, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return f"{self.post_id} {self.action} @ {self.created_at:%Y-%m-%d %H:%M}"


class Comment(models.Model):
    PENDING = "pending"
    APPROVED = "approved"
    SPAM = "spam"
    REJECTED = "rejected"
    DELETED = "deleted"
    STATUS_CHOICES = [
        (PENDING, "Pending"),
        (APPROVED, "Approved"),
        (SPAM, "Spam"),
        (REJECTED, "Rejected"),
        (DELETED, "Deleted"),
    ]

    post = models.ForeignKey(Post, on_delete=models.CASCADE, related_name="comments")
    author_name = models.CharField(max_length=255)
    author_email = models.EmailField(blank=True, null=True)
    author_url = models.URLField(max_length=2000, blank=True)
    content = models.TextField()
    excerpt = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(blank=True, default="")
    referrer = models.URLField(max_length=2000, blank=True)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=PENDING)
    akismet_score = models.FloatField(null=True, blank=True)
    akismet_classification = models.CharField(max_length=32, blank=True, default="")
    akismet_submit_hash = models.CharField(max_length=255, blank=True, default="")

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["status"]),
            models.Index(fields=["created_at"]),
            models.Index(fields=["post"]),
        ]

    def __str__(self):
        return f"Comment by {self.author_name} on {self.post}"
