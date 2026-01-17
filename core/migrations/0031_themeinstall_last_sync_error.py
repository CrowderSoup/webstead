from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0030_siteconfiguration_home_page"),
    ]

    operations = [
        migrations.AddField(
            model_name="themeinstall",
            name="last_sync_error",
            field=models.CharField(blank=True, default="", max_length=500),
        ),
    ]
