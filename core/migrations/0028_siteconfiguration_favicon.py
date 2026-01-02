from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("files", "0001_initial"),
        ("core", "0027_themeinstall_last_synced_commit"),
    ]

    operations = [
        migrations.AddField(
            model_name="siteconfiguration",
            name="favicon",
            field=models.ForeignKey(
                blank=True,
                help_text="File to use as the site favicon.",
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="site_favicons",
                to="files.file",
            ),
        ),
    ]
