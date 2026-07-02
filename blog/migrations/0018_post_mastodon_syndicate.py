from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("blog", "0017_add_bookmark_post_type"),
    ]

    operations = [
        migrations.AddField(
            model_name="post",
            name="mastodon_syndicate",
            field=models.BooleanField(
                blank=True,
                null=True,
                help_text=(
                    "Override Mastodon syndication for this post. "
                    "Null = use the per-kind default from MastodonSyndicationDefault."
                ),
            ),
        ),
    ]
