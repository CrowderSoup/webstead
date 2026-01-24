from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("analytics", "0003_visit_user_agent_details"),
    ]

    operations = [
        migrations.CreateModel(
            name="UserAgentIgnore",
            fields=[
                (
                    "id",
                    models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID"),
                ),
                ("user_agent", models.TextField(unique=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
        ),
    ]
