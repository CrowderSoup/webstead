from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("mastodon_integration", "0002_encrypt_credentials"),
    ]

    operations = [
        migrations.AddField(
            model_name="mastodonaccount",
            name="timeline_reply_filter",
            field=models.CharField(
                choices=[
                    ("all", "Include all posts"),
                    ("hide", "Hide replies"),
                    ("self_threads", "Hide replies except self-threads"),
                ],
                default="all",
                help_text="Control whether replies are ingested into the Mastodon home timeline channel.",
                max_length=20,
            ),
        ),
    ]
