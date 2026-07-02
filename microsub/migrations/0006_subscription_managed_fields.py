from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("microsub", "0005_full_spec_support"),
    ]

    operations = [
        migrations.AddField(
            model_name="subscription",
            name="managed_by",
            field=models.CharField(blank=True, default="", max_length=32),
        ),
        migrations.AddField(
            model_name="subscription",
            name="managed_key",
            field=models.CharField(blank=True, default="", max_length=64),
        ),
    ]
