from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("blog", "0013_alter_post_kind"),
    ]

    operations = [
        migrations.CreateModel(
            name="Comment",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("author_name", models.CharField(max_length=255)),
                ("author_email", models.EmailField(blank=True, max_length=254, null=True)),
                ("author_url", models.URLField(blank=True, max_length=2000)),
                ("content", models.TextField()),
                ("excerpt", models.TextField(blank=True, default="")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("ip_address", models.GenericIPAddressField(blank=True, null=True)),
                ("user_agent", models.TextField(blank=True, default="")),
                ("referrer", models.URLField(blank=True, max_length=2000)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("pending", "Pending"),
                            ("approved", "Approved"),
                            ("spam", "Spam"),
                            ("rejected", "Rejected"),
                            ("deleted", "Deleted"),
                        ],
                        default="pending",
                        max_length=16,
                    ),
                ),
                ("akismet_score", models.FloatField(blank=True, null=True)),
                ("akismet_classification", models.CharField(blank=True, default="", max_length=32)),
                ("akismet_submit_hash", models.CharField(blank=True, default="", max_length=255)),
                (
                    "post",
                    models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="comments", to="blog.post"),
                ),
            ],
            options={
                "ordering": ["-created_at"],
            },
        ),
        migrations.AddIndex(
            model_name="comment",
            index=models.Index(fields=["status"], name="blog_comment_status_idx"),
        ),
        migrations.AddIndex(
            model_name="comment",
            index=models.Index(fields=["created_at"], name="blog_comment_created_at_idx"),
        ),
        migrations.AddIndex(
            model_name="comment",
            index=models.Index(fields=["post"], name="blog_comment_post_idx"),
        ),
    ]
