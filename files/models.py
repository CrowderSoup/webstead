import uuid, os

from django.conf import settings
from django.utils import timezone
from django.db import models
from django.contrib.contenttypes.fields import GenericForeignKey, GenericRelation
from django.contrib.contenttypes.models import ContentType


def upload_to(instance, filename):
    ext = os.path.splitext(filename)[1].lower()
    now = timezone.now()
    return f"uploads/{instance.kind}/{now:%Y/%m}/{uuid.uuid4()}{ext}"


class File(models.Model):
    IMAGE = "image"; DOC = "doc"; VIDEO = "video"
    KIND_CHOICES = [(IMAGE, "Image"), (DOC, "Document"), (VIDEO, "Video")]

    kind = models.CharField(max_length=16, choices=KIND_CHOICES, default=IMAGE)
    file = models.FileField(upload_to=upload_to)
    alt_text = models.CharField(max_length=255, blank=True)
    caption = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)

    def __str__(self):
        return os.path.basename(self.file.name)

    def is_in_use(self):
        return self.attachments.exists() or self.hcard_photos.exists()

    def in_use_message(self):
        if self.attachments.exists():
            return "File is still attached to content."
        if self.hcard_photos.exists():
            return "File is still used in a profile photo."
        return ""

class Attachment(models.Model):
    content_type = models.ForeignKey(ContentType, on_delete=models.CASCADE)
    object_id = models.PositiveIntegerField()
    content_object = GenericForeignKey()

    asset = models.ForeignKey(File, on_delete=models.CASCADE, related_name="attachments")
    role = models.CharField(max_length=32, blank=True)  # hero, inline, gallery, etc.
    sort_order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["sort_order", "id"]

    def __str__(self):
        return self.asset.file.name
