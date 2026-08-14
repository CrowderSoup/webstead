import django.db.models.deletion
import encrypted_model_fields.fields
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ("blog", "0018_post_mastodon_syndicate"),
    ]

    operations = [
        migrations.CreateModel(
            name="StravaAccount",
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
                ("athlete_id", models.CharField(max_length=64, unique=True)),
                ("access_token", encrypted_model_fields.fields.EncryptedCharField()),
                ("refresh_token", encrypted_model_fields.fields.EncryptedCharField()),
                (
                    "expires_at",
                    models.DateTimeField(help_text="When the current access token expires."),
                ),
                ("username", models.CharField(blank=True, max_length=255)),
                ("firstname", models.CharField(blank=True, max_length=255)),
                ("lastname", models.CharField(blank=True, max_length=255)),
                ("profile_photo_url", models.URLField(blank=True)),
                ("is_active", models.BooleanField(default=True)),
                (
                    "auto_post_enabled",
                    models.BooleanField(
                        default=False,
                        help_text="Automatically create a blog post whenever a new Strava activity is recorded.",
                    ),
                ),
                (
                    "webhook_subscription_id",
                    models.CharField(
                        blank=True,
                        max_length=64,
                        help_text="Strava push_subscriptions ID, set once the webhook is enabled.",
                    ),
                ),
                (
                    "webhook_verify_token",
                    models.CharField(
                        blank=True,
                        max_length=64,
                        help_text="Random token Strava echoes back during the webhook validation handshake.",
                    ),
                ),
                (
                    "last_reconciled_at",
                    models.DateTimeField(
                        blank=True,
                        null=True,
                        help_text="Cursor for the hourly reconciliation task that catches missed webhook deliveries.",
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={
                "verbose_name": "Strava Account",
            },
        ),
        migrations.CreateModel(
            name="StravaActivity",
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
                        related_name="strava_activity",
                        to="blog.post",
                    ),
                ),
                (
                    "strava_activity_id",
                    models.CharField(db_index=True, max_length=64, unique=True),
                ),
                ("imported_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={
                "verbose_name": "Strava Activity",
                "verbose_name_plural": "Strava Activities",
            },
        ),
    ]
