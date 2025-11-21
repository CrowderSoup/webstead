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
    STATUS_CHOICES = [
        (PENDING, "Pending"),
        (ACCEPTED, "Accepted"),
        (REJECTED, "Rejected"),
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
