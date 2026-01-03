from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("blog", "0014_alter_post_kind"),
    ]

    operations = [
        migrations.AddField(
            model_name="activityflyover",
            name="enqueued_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
