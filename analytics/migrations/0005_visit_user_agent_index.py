from django.db import migrations, models


def delete_ignored_user_agent_visits(apps, schema_editor):
    Visit = apps.get_model("analytics", "Visit")
    UserAgentIgnore = apps.get_model("analytics", "UserAgentIgnore")
    ignored = list(
        UserAgentIgnore.objects.values_list("user_agent", flat=True)
    )
    if ignored:
        Visit.objects.filter(user_agent__in=ignored).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("analytics", "0004_useragentignore"),
    ]

    operations = [
        migrations.AlterField(
            model_name="visit",
            name="user_agent",
            field=models.TextField(blank=True, db_index=True),
        ),
        migrations.RunPython(
            delete_ignored_user_agent_visits,
            reverse_code=migrations.RunPython.noop,
        ),
    ]
