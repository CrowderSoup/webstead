from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0032_siteconfiguration_bridgy_publish_toggles"),
    ]

    operations = [
        migrations.AddField(
            model_name="siteconfiguration",
            name="comments_enabled",
            field=models.BooleanField(default=False, verbose_name="Comments enabled"),
        ),
    ]
