from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0021_siteconfiguration_site_author"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="siteconfiguration",
            name="intro",
        ),
        migrations.RemoveField(
            model_name="siteconfiguration",
            name="bio",
        ),
    ]
