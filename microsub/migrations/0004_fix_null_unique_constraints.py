"""
Replace unique_together on MutedUser and BlockedUser with two partial
UniqueConstraints that correctly enforce uniqueness even when channel is NULL.

Background: the previous unique_together = [["channel", "url"]] relied on a
regular UNIQUE (channel_id, url) database constraint.  Both SQLite and
PostgreSQL follow the SQL standard where NULL != NULL inside unique constraints,
so (NULL, "https://same-url") could be inserted multiple times without raising
an IntegrityError.  The Django ORM's get_or_create() uses .get() internally;
when duplicate rows exist that .get() raises MultipleObjectsReturned, which
propagated as an unhandled 500 error for every subsequent mute/block call.

This migration:
  1. Deduplicates any existing (NULL, url) duplicates (keep lowest-pk row).
  2. Drops the old unique_together constraints.
  3. Adds two partial UniqueConstraints per model.
"""

from django.db import migrations, models


def _deduplicate_nullchannel(apps, schema_editor, model_name):
    """Keep the lowest-pk site-wide row for each url; delete the rest."""
    Model = apps.get_model("microsub", model_name)
    seen = {}
    for obj in Model.objects.filter(channel__isnull=True).order_by("pk"):
        if obj.url in seen:
            obj.delete()
        else:
            seen[obj.url] = obj.pk


def deduplicate_forward(apps, schema_editor):
    _deduplicate_nullchannel(apps, schema_editor, "MutedUser")
    _deduplicate_nullchannel(apps, schema_editor, "BlockedUser")


class Migration(migrations.Migration):

    dependencies = [
        ("microsub", "0003_add_entry_author_url"),
    ]

    operations = [
        # Step 1: remove duplicate site-wide rows before adding constraints.
        migrations.RunPython(deduplicate_forward, migrations.RunPython.noop),

        # Step 2: drop old unique_together constraints.
        migrations.AlterUniqueTogether(
            name="muteduser",
            unique_together=set(),
        ),
        migrations.AlterUniqueTogether(
            name="blockeduser",
            unique_together=set(),
        ),

        # Step 3: add partial unique constraints.
        migrations.AddConstraint(
            model_name="muteduser",
            constraint=models.UniqueConstraint(
                fields=["url"],
                condition=models.Q(channel__isnull=True),
                name="microsub_muteduser_sitewide_unique",
            ),
        ),
        migrations.AddConstraint(
            model_name="muteduser",
            constraint=models.UniqueConstraint(
                fields=["channel", "url"],
                condition=models.Q(channel__isnull=False),
                name="microsub_muteduser_channel_unique",
            ),
        ),
        migrations.AddConstraint(
            model_name="blockeduser",
            constraint=models.UniqueConstraint(
                fields=["url"],
                condition=models.Q(channel__isnull=True),
                name="microsub_blockeduser_sitewide_unique",
            ),
        ),
        migrations.AddConstraint(
            model_name="blockeduser",
            constraint=models.UniqueConstraint(
                fields=["channel", "url"],
                condition=models.Q(channel__isnull=False),
                name="microsub_blockeduser_channel_unique",
            ),
        ),
    ]
