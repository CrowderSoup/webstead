from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0017_siteconfiguration_robots_txt"),
    ]

    operations = [
        migrations.AddField(
            model_name="siteconfiguration",
            name="active_theme",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
    ]
