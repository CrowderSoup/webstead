from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0031_themeinstall_last_sync_error"),
    ]

    operations = [
        migrations.AddField(
            model_name="siteconfiguration",
            name="bridgy_publish_bluesky",
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name="siteconfiguration",
            name="bridgy_publish_flickr",
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name="siteconfiguration",
            name="bridgy_publish_github",
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name="siteconfiguration",
            name="bridgy_publish_mastodon",
            field=models.BooleanField(default=True),
        ),
    ]
