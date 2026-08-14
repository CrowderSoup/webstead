from django.db import models
from encrypted_model_fields.fields import EncryptedCharField

from blog.models import Post


class StravaAccount(models.Model):
    """
    A connected Strava account. In practice this is a singleton (one account
    per Webstead installation), but modelled as a normal row like
    MastodonAccount to allow for multi-account support in the future.
    """

    athlete_id = models.CharField(max_length=64, unique=True)
    access_token = EncryptedCharField(max_length=512)
    refresh_token = EncryptedCharField(max_length=512)
    expires_at = models.DateTimeField(help_text="When the current access token expires.")

    username = models.CharField(max_length=255, blank=True)
    firstname = models.CharField(max_length=255, blank=True)
    lastname = models.CharField(max_length=255, blank=True)
    profile_photo_url = models.URLField(blank=True)

    is_active = models.BooleanField(default=True)

    auto_post_enabled = models.BooleanField(
        default=False,
        help_text="Automatically create a blog post whenever a new Strava activity is recorded.",
    )

    webhook_subscription_id = models.CharField(
        max_length=64,
        blank=True,
        help_text="Strava push_subscriptions ID, set once the webhook is enabled.",
    )
    webhook_verify_token = models.CharField(
        max_length=64,
        blank=True,
        help_text="Random token Strava echoes back during the webhook validation handshake.",
    )

    last_reconciled_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Cursor for the hourly reconciliation task that catches missed webhook deliveries.",
    )

    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.display_name

    class Meta:
        verbose_name = "Strava Account"

    @property
    def display_name(self):
        name = f"{self.firstname} {self.lastname}".strip()
        return name or self.username or f"Athlete {self.athlete_id}"

    @classmethod
    def get_active(cls):
        """Return the single active account, or None."""
        return cls.objects.filter(is_active=True).first()


class StravaActivity(models.Model):
    """
    Links a Webstead Post to the Strava activity it was imported from.
    The unique `strava_activity_id` is the idempotency guard that prevents
    the same activity from being imported twice, whether via the historical
    import page, the webhook, or the reconciliation safety net.
    """

    post = models.OneToOneField(
        Post, on_delete=models.CASCADE, related_name="strava_activity"
    )
    strava_activity_id = models.CharField(max_length=64, unique=True, db_index=True)
    imported_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.post} → strava:{self.strava_activity_id}"

    class Meta:
        verbose_name = "Strava Activity"
        verbose_name_plural = "Strava Activities"
