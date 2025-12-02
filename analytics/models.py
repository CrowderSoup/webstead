from django.db import models
from django.conf import settings

class Visit(models.Model):
    session_key = models.CharField(max_length=40, db_index=True, blank=True, null=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL
    )
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(blank=True)
    path = models.CharField(max_length=512)
    referrer = models.TextField(blank=True)
    started_at = models.DateTimeField(auto_now_add=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    duration_seconds = models.PositiveIntegerField(null=True, blank=True)
    country = models.CharField(max_length=2, blank=True)
    region = models.CharField(max_length=64, blank=True)
    city = models.CharField(max_length=128, blank=True)

    def __str__(self):
        return self.path

    class Meta:
        indexes = [
            models.Index(fields=["path"]),
            models.Index(fields=["session_key", "started_at"]),
        ]
