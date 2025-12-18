from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0023_migrate_elsewhere_to_hcardurl"),
    ]

    operations = [
        migrations.CreateModel(
            name="ThemeInstall",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("slug", models.SlugField(max_length=255, unique=True)),
                (
                    "source_type",
                    models.CharField(
                        choices=[("upload", "Upload"), ("git", "Git"), ("storage", "Storage")],
                        max_length=16,
                    ),
                ),
                ("source_url", models.URLField(blank=True, default="", max_length=2000)),
                ("source_ref", models.CharField(blank=True, default="", max_length=255)),
                ("version", models.CharField(blank=True, default="", max_length=255)),
                ("checksum", models.CharField(blank=True, default="", max_length=255)),
                ("installed_at", models.DateTimeField(auto_now_add=True)),
                ("last_synced_at", models.DateTimeField(blank=True, null=True)),
                (
                    "last_sync_status",
                    models.CharField(
                        blank=True,
                        choices=[("pending", "Pending"), ("success", "Success"), ("failed", "Failed")],
                        default="",
                        max_length=16,
                    ),
                ),
            ],
            options={
                "ordering": ("slug",),
            },
        ),
    ]
