import secrets

from django.db import models

from .utils import normalize_entry_data


class Channel(models.Model):
    uid = models.CharField(max_length=100, unique=True)
    name = models.CharField(max_length=255)
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["order", "id"]

    def __str__(self):
        return self.name


class Subscription(models.Model):
    channel = models.ForeignKey(Channel, on_delete=models.CASCADE, related_name="subscriptions")
    url = models.URLField(max_length=2000)
    name = models.CharField(max_length=255, blank=True)
    photo = models.URLField(max_length=2000, blank=True)
    managed_by = models.CharField(max_length=32, blank=True, default="")
    managed_key = models.CharField(max_length=64, blank=True, default="")
    is_active = models.BooleanField(default=True)
    last_fetched_at = models.DateTimeField(null=True, blank=True)
    fetch_error = models.TextField(blank=True)
    websub_hub = models.URLField(max_length=2000, blank=True)
    websub_secret = models.CharField(max_length=255, blank=True)
    websub_callback_token = models.CharField(max_length=64, blank=True)
    websub_requested_at = models.DateTimeField(null=True, blank=True)
    websub_subscribed_at = models.DateTimeField(null=True, blank=True)
    websub_expires_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        unique_together = [["channel", "url"]]

    def __str__(self):
        return f"{self.url} in {self.channel}"

    def save(self, *args, **kwargs):
        if not self.websub_callback_token:
            self.websub_callback_token = secrets.token_hex(16)
            update_fields = kwargs.get("update_fields")
            if update_fields is not None:
                kwargs["update_fields"] = set(update_fields) | {"websub_callback_token"}
        super().save(*args, **kwargs)


class Entry(models.Model):
    channel = models.ForeignKey(Channel, on_delete=models.CASCADE, related_name="entries")
    subscription = models.ForeignKey(
        Subscription,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="entries",
    )
    uid = models.TextField()
    data = models.JSONField()
    published = models.DateTimeField()
    author_url = models.URLField(max_length=2000, blank=True, db_index=True)
    source_url = models.URLField(max_length=2000, blank=True, db_index=True)
    kind_like = models.BooleanField(default=False, db_index=True)
    kind_repost = models.BooleanField(default=False, db_index=True)
    kind_bookmark = models.BooleanField(default=False, db_index=True)
    kind_reply = models.BooleanField(default=False, db_index=True)
    kind_checkin = models.BooleanField(default=False, db_index=True)
    kind_photo = models.BooleanField(default=False, db_index=True)
    kind_video = models.BooleanField(default=False, db_index=True)
    kind_audio = models.BooleanField(default=False, db_index=True)
    is_read = models.BooleanField(default=False, db_index=True)
    is_removed = models.BooleanField(default=False, db_index=True)
    fetched_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [["channel", "uid"]]
        ordering = ["-published"]

    def __str__(self):
        return f"Entry {str(self.uid)[:50]}"

    def _normalized_metadata(self) -> dict:
        payload, metadata = normalize_entry_data(
            self.data,
            uid=self.uid,
            subscription_url=self.subscription.url if self.subscription_id else "",
            existing_author_url=self.author_url,
            existing_source_url=self.source_url,
        )
        self.data = payload
        self.author_url = metadata["author_url"]
        self.source_url = metadata["source_url"]
        self.kind_like = metadata["kind_like"]
        self.kind_repost = metadata["kind_repost"]
        self.kind_bookmark = metadata["kind_bookmark"]
        self.kind_reply = metadata["kind_reply"]
        self.kind_checkin = metadata["kind_checkin"]
        self.kind_photo = metadata["kind_photo"]
        self.kind_video = metadata["kind_video"]
        self.kind_audio = metadata["kind_audio"]
        return metadata

    def save(self, *args, **kwargs):
        metadata = self._normalized_metadata()
        update_fields = kwargs.get("update_fields")
        if update_fields is not None:
            kwargs["update_fields"] = set(update_fields) | {
                "data",
                "author_url",
                "source_url",
                "kind_like",
                "kind_repost",
                "kind_bookmark",
                "kind_reply",
                "kind_checkin",
                "kind_photo",
                "kind_video",
                "kind_audio",
            }
        super().save(*args, **kwargs)
        self._sync_search_index(metadata)

    def _sync_search_index(self, metadata: dict) -> None:
        EntrySearchToken.objects.filter(entry=self).delete()
        EntryCategory.objects.filter(entry=self).delete()
        EntrySearchToken.objects.bulk_create(
            [EntrySearchToken(entry=self, token=token) for token in metadata["tokens"]],
            ignore_conflicts=True,
        )
        EntryCategory.objects.bulk_create(
            [EntryCategory(entry=self, value=value) for value in metadata["categories"]],
            ignore_conflicts=True,
        )


class EntrySearchToken(models.Model):
    entry = models.ForeignKey(Entry, on_delete=models.CASCADE, related_name="search_tokens")
    token = models.CharField(max_length=100, db_index=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["entry", "token"], name="microsub_entrysearchtoken_unique"),
        ]


class EntryCategory(models.Model):
    entry = models.ForeignKey(Entry, on_delete=models.CASCADE, related_name="search_categories")
    value = models.CharField(max_length=255, db_index=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["entry", "value"], name="microsub_entrycategory_unique"),
        ]


class MutedUser(models.Model):
    channel = models.ForeignKey(
        Channel,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="muted_users",
    )
    url = models.URLField(max_length=2000)

    class Meta:
        # unique_together cannot enforce uniqueness when channel is NULL because
        # databases treat NULL != NULL in unique constraints.  Two partial indexes
        # are used instead: one for site-wide (channel IS NULL) and one for
        # channel-specific records.
        constraints = [
            models.UniqueConstraint(
                fields=["url"],
                condition=models.Q(channel__isnull=True),
                name="microsub_muteduser_sitewide_unique",
            ),
            models.UniqueConstraint(
                fields=["channel", "url"],
                condition=models.Q(channel__isnull=False),
                name="microsub_muteduser_channel_unique",
            ),
        ]

    def __str__(self):
        return f"Muted {self.url}"


class BlockedUser(models.Model):
    channel = models.ForeignKey(
        Channel,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="blocked_users",
    )
    url = models.URLField(max_length=2000)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["url"],
                condition=models.Q(channel__isnull=True),
                name="microsub_blockeduser_sitewide_unique",
            ),
            models.UniqueConstraint(
                fields=["channel", "url"],
                condition=models.Q(channel__isnull=False),
                name="microsub_blockeduser_channel_unique",
            ),
        ]

    def __str__(self):
        return f"Blocked {self.url}"
