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

class Post(models.Model):
    ARTICLE = "article"; NOTE = "note"; PHOTO = "photo"; LIKE = "like"; REPOST = "repost"; REPLY = "reply"
    KIND_CHOICES = [
        (ARTICLE, "Article"),
        (NOTE, "Note"),
        (PHOTO, "Photo"),
        (LIKE, "Like"),
        (REPOST, "Repost"),
        (REPLY, "Reply"),
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

    def __str__(self):
        return self.title

    def save(self, *args, **kwargs):
        timestamp = int(timezone.now().timestamp())

        if not self.slug:
            self.slug = slugify(f"{self.title}-{timestamp}") if self.title else slugify(f"{self.kind}-{timestamp}")

        if not self.title:
            base_titles = {
                Post.NOTE: "Note",
                Post.PHOTO: "Photo",
                Post.LIKE: "Like",
                Post.REPOST: "Repost",
                Post.REPLY: "Reply",
            }
            self.title = f"{base_titles.get(self.kind, 'Article')}: {timestamp}"

        super().save(*args, **kwargs)

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
    
    class Meta:
        ordering = ['-published_on']
