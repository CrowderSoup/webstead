from django.db import migrations, models


def clear_ambiguous_consents(apps, schema_editor):
    # Old approvals contain no audience and cannot safely be assigned one.
    # They also cannot distinguish an empty grant from the requested scopes.
    Consent = apps.get_model("indieauth", "IndieAuthConsent")
    Consent.objects.using(schema_editor.connection.alias).all().delete()


class Migration(migrations.Migration):
    dependencies = [
        ("indieauth", "0004_oauth_resource_and_refresh_tokens"),
    ]

    operations = [
        migrations.RunPython(clear_ambiguous_consents, migrations.RunPython.noop),
        migrations.AddField(
            model_name="indieauthconsent",
            name="resource",
            field=models.CharField(blank=True, default="", max_length=2000),
        ),
        migrations.AlterUniqueTogether(
            name="indieauthconsent",
            unique_together={("user", "client_id", "scope", "resource")},
        ),
    ]
