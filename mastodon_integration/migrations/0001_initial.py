import django.db.models.deletion
from django.db import migrations, models


def _populate_syndication_defaults(apps, schema_editor):
    MastodonSyndicationDefault = apps.get_model("mastodon_integration", "MastodonSyndicationDefault")
    kinds = [
        ("article", True),
        ("note", True),
        ("photo", False),
        ("activity", False),
        ("like", False),
        ("repost", False),
        ("reply", False),
        ("event", False),
        ("rsvp", False),
        ("checkin", False),
        ("bookmark", False),
    ]
    MastodonSyndicationDefault.objects.bulk_create(
        [MastodonSyndicationDefault(post_kind=kind, publish=publish) for kind, publish in kinds]
    )


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ("blog", "0017_add_bookmark_post_type"),
        ("microsub", "__first__"),
    ]

    operations = [
        migrations.CreateModel(
            name="MastodonApp",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("instance_url", models.URLField(unique=True)),
                ("client_id", models.CharField(max_length=512)),
                ("client_secret", models.CharField(max_length=512)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={
                "verbose_name": "Mastodon App",
            },
        ),
        migrations.CreateModel(
            name="MastodonAccount",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "app",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="accounts",
                        to="mastodon_integration.mastodonapp",
                    ),
                ),
                ("access_token", models.CharField(max_length=512)),
                ("account_id", models.CharField(max_length=255)),
                (
                    "username",
                    models.CharField(
                        max_length=255,
                        help_text="Full handle, e.g. aaron@mastodon.social",
                    ),
                ),
                ("display_name", models.CharField(blank=True, max_length=255)),
                ("avatar_url", models.URLField(blank=True)),
                ("is_active", models.BooleanField(default=True)),
                (
                    "timeline_channel",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="mastodon_timeline_source",
                        to="microsub.channel",
                        help_text="Microsub channel that receives the Mastodon home timeline.",
                    ),
                ),
                (
                    "notifications_channel",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="mastodon_notifications_source",
                        to="microsub.channel",
                        help_text="Microsub channel that receives Mastodon notifications.",
                    ),
                ),
                (
                    "last_timeline_id",
                    models.CharField(
                        blank=True,
                        max_length=255,
                        help_text="Mastodon status ID used as since_id for timeline polling.",
                    ),
                ),
                (
                    "last_notification_id",
                    models.CharField(
                        blank=True,
                        max_length=255,
                        help_text="Mastodon notification ID used as since_id for notification polling.",
                    ),
                ),
                (
                    "max_toot_chars",
                    models.PositiveIntegerField(
                        default=500,
                        help_text="Maximum toot length for this instance, fetched during OAuth.",
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={
                "verbose_name": "Mastodon Account",
            },
        ),
        migrations.CreateModel(
            name="MastodonSyndicationDefault",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "post_kind",
                    models.CharField(
                        choices=[
                            ("article", "Article"),
                            ("note", "Note"),
                            ("photo", "Photo"),
                            ("activity", "Activity"),
                            ("like", "Like"),
                            ("repost", "Repost"),
                            ("reply", "Reply"),
                            ("event", "Event"),
                            ("rsvp", "RSVP"),
                            ("checkin", "Check-in"),
                            ("bookmark", "Bookmark"),
                        ],
                        max_length=16,
                        unique=True,
                    ),
                ),
                (
                    "publish",
                    models.BooleanField(
                        default=False,
                        help_text="Publish posts of this kind to Mastodon by default.",
                    ),
                ),
            ],
            options={
                "verbose_name": "Mastodon Syndication Default",
                "ordering": ["post_kind"],
            },
        ),
        migrations.CreateModel(
            name="MastodonPost",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "post",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="mastodon_post",
                        to="blog.post",
                    ),
                ),
                (
                    "mastodon_id",
                    models.CharField(
                        db_index=True,
                        max_length=255,
                        help_text="Status ID on the Mastodon instance.",
                    ),
                ),
                (
                    "mastodon_url",
                    models.URLField(help_text="Public URL of the toot."),
                ),
                ("published_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={
                "verbose_name": "Mastodon Post",
            },
        ),
        # Pre-populate syndication defaults: publish notes and articles by default
        migrations.RunPython(
            code=_populate_syndication_defaults,
            reverse_code=migrations.RunPython.noop,
        ),
    ]
