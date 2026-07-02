from django.db import models
from encrypted_model_fields.fields import EncryptedCharField

from blog.models import Post


class MastodonApp(models.Model):
    """
    OAuth application registration for a Mastodon instance.
    Created automatically during the OAuth flow when connecting a new instance.
    """

    instance_url = models.URLField(unique=True)
    client_id = models.CharField(max_length=512)
    client_secret = EncryptedCharField(max_length=512)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.instance_url

    class Meta:
        verbose_name = "Mastodon App"


class MastodonAccount(models.Model):
    """
    A connected Mastodon account. In practice this is a singleton (one account
    per Webstead installation), but modelled as a normal row to allow for
    multi-account support in the future.
    """

    app = models.ForeignKey(
        MastodonApp, on_delete=models.CASCADE, related_name="accounts"
    )
    access_token = EncryptedCharField(max_length=512)
    account_id = models.CharField(max_length=255)
    username = models.CharField(
        max_length=255,
        help_text="Full handle, e.g. aaron@mastodon.social",
    )
    display_name = models.CharField(max_length=255, blank=True)
    avatar_url = models.URLField(blank=True)
    is_active = models.BooleanField(default=True)

    TIMELINE_REPLIES_ALL = "all"
    TIMELINE_REPLIES_HIDE = "hide"
    TIMELINE_REPLIES_SELF_THREADS = "self_threads"
    TIMELINE_REPLY_FILTER_CHOICES = [
        (TIMELINE_REPLIES_ALL, "Include all posts"),
        (TIMELINE_REPLIES_HIDE, "Hide replies"),
        (TIMELINE_REPLIES_SELF_THREADS, "Hide replies except self-threads"),
    ]

    # Microsub channels to route Mastodon content into (set via admin UI)
    timeline_channel = models.ForeignKey(
        "microsub.Channel",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="mastodon_timeline_source",
        help_text="Microsub channel that receives the Mastodon home timeline.",
    )
    timeline_reply_filter = models.CharField(
        max_length=20,
        choices=TIMELINE_REPLY_FILTER_CHOICES,
        default=TIMELINE_REPLIES_ALL,
        help_text="Control whether replies are ingested into the Mastodon home timeline channel.",
    )
    notifications_channel = models.ForeignKey(
        "microsub.Channel",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="mastodon_notifications_source",
        help_text="Microsub channel that receives Mastodon notifications.",
    )

    # Pagination cursors for incremental polling
    last_timeline_id = models.CharField(
        max_length=255,
        blank=True,
        help_text="Mastodon status ID used as since_id for timeline polling.",
    )
    last_notification_id = models.CharField(
        max_length=255,
        blank=True,
        help_text="Mastodon notification ID used as since_id for notification polling.",
    )

    # Instance character limit (fetched during OAuth)
    max_toot_chars = models.PositiveIntegerField(
        default=500,
        help_text="Maximum toot length for this instance, fetched during OAuth.",
    )

    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.username

    class Meta:
        verbose_name = "Mastodon Account"

    @classmethod
    def get_active(cls):
        """Return the single active account, or None."""
        return cls.objects.filter(is_active=True).select_related("app").first()


class MastodonSyndicationDefault(models.Model):
    """
    Per-post-kind default: should new posts of this kind be published to
    Mastodon?  Individual posts can override this via Post.mastodon_syndicate.
    """

    post_kind = models.CharField(
        max_length=16,
        choices=Post.KIND_CHOICES,
        unique=True,
    )
    publish = models.BooleanField(
        default=False,
        help_text="Publish posts of this kind to Mastodon by default.",
    )

    def __str__(self):
        return f"{self.post_kind}: {'publish' if self.publish else 'skip'}"

    class Meta:
        verbose_name = "Mastodon Syndication Default"
        ordering = ["post_kind"]


class MastodonPost(models.Model):
    """
    Links a Webstead Post to the Mastodon status that was created for it.
    Used for backfeed matching (looking up which toot corresponds to a post
    when a favourite or boost notification arrives).
    """

    post = models.OneToOneField(
        Post, on_delete=models.CASCADE, related_name="mastodon_post"
    )
    mastodon_id = models.CharField(
        max_length=255,
        db_index=True,
        help_text="Status ID on the Mastodon instance.",
    )
    mastodon_url = models.URLField(help_text="Public URL of the toot.")
    published_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.post} → {self.mastodon_url}"

    class Meta:
        verbose_name = "Mastodon Post"
