from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("files", "0001_initial"),
        ("blog", "0012_alter_post_content"),
    ]

    operations = [
        migrations.CreateModel(
            name="ActivityFlyover",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("status", models.CharField(choices=[("pending", "Pending"), ("ready", "Ready"), ("failed", "Failed")], default="pending", max_length=16)),
                ("error_message", models.CharField(blank=True, max_length=255)),
                ("enqueued_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("post", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="flyover", to="blog.post")),
                ("video", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="activity_flyovers", to="files.file")),
            ],
        ),
    ]
