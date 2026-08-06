from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("mastodon_integration", "0003_mastodonaccount_timeline_reply_filter"),
    ]

    operations = [
        migrations.AddField(
            model_name="mastodonaccount",
            name="timeline_include_reblogs",
            field=models.BooleanField(
                default=False,
                help_text="Include boosts/reposts from the home timeline in the Mastodon timeline channel. Off by default so only original posts are ingested.",
            ),
        ),
    ]
