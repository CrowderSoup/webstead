from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0042_siteconfiguration_microsub_unfollow_removes_entries"),
    ]

    operations = [
        migrations.AddField(
            model_name="siteconfiguration",
            name="site_url",
            field=models.URLField(
                blank=True,
                default="",
                help_text="Canonical base URL used when background jobs need to build absolute links.",
                max_length=2000,
            ),
        ),
    ]
