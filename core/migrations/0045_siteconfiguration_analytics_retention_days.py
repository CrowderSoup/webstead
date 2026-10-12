from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0044_siteconfiguration_activity_units'),
    ]

    operations = [
        migrations.AddField(
            model_name='siteconfiguration',
            name='analytics_retention_days',
            field=models.PositiveIntegerField(default=90, help_text='Analytics visit records older than this many days are deleted automatically. Set to 0 to keep visits forever.', verbose_name='Analytics retention (days)'),
        ),
    ]
