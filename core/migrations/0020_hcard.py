from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0019_alter_page_content_alter_siteconfiguration_bio_and_more"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="HCard",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(blank=True, default="", max_length=255)),
                ("honorific_prefix", models.CharField(blank=True, default="", max_length=255)),
                ("given_name", models.CharField(blank=True, default="", max_length=255)),
                ("additional_name", models.CharField(blank=True, default="", max_length=255)),
                ("family_name", models.CharField(blank=True, default="", max_length=255)),
                ("honorific_suffix", models.CharField(blank=True, default="", max_length=255)),
                ("nickname", models.CharField(blank=True, default="", max_length=255)),
                ("sort_string", models.CharField(blank=True, default="", max_length=255)),
                ("uid", models.URLField(blank=True, default="", max_length=2000)),
                ("bday", models.DateField(blank=True, null=True)),
                ("anniversary", models.DateField(blank=True, null=True)),
                ("org_name", models.CharField(blank=True, default="", max_length=255)),
                ("job_title", models.CharField(blank=True, default="", max_length=255)),
                ("role", models.CharField(blank=True, default="", max_length=255)),
                ("post_office_box", models.CharField(blank=True, default="", max_length=255)),
                ("extended_address", models.CharField(blank=True, default="", max_length=255)),
                ("street_address", models.CharField(blank=True, default="", max_length=255)),
                ("locality", models.CharField(blank=True, default="", max_length=255)),
                ("region", models.CharField(blank=True, default="", max_length=255)),
                ("postal_code", models.CharField(blank=True, default="", max_length=64)),
                ("country_name", models.CharField(blank=True, default="", max_length=255)),
                ("label", models.CharField(blank=True, default="", max_length=512)),
                ("latitude", models.DecimalField(blank=True, decimal_places=6, max_digits=9, null=True)),
                ("longitude", models.DecimalField(blank=True, decimal_places=6, max_digits=9, null=True)),
                ("altitude", models.DecimalField(blank=True, decimal_places=2, max_digits=9, null=True)),
                ("note", models.TextField(blank=True, default="")),
                ("sex", models.CharField(blank=True, default="", max_length=64)),
                ("gender_identity", models.CharField(blank=True, default="", max_length=255)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "org_hcard",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="org_members",
                        to="core.hcard",
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="hcards",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="HCardCategory",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("value", models.CharField(max_length=255)),
                ("hcard", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="categories", to="core.hcard")),
            ],
        ),
        migrations.CreateModel(
            name="HCardEmail",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("value", models.CharField(max_length=254)),
                ("hcard", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="emails", to="core.hcard")),
            ],
        ),
        migrations.CreateModel(
            name="HCardImpp",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("value", models.CharField(max_length=2000)),
                ("hcard", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="impps", to="core.hcard")),
            ],
        ),
        migrations.CreateModel(
            name="HCardKey",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("value", models.CharField(max_length=2000)),
                ("hcard", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="keys", to="core.hcard")),
            ],
        ),
        migrations.CreateModel(
            name="HCardLogo",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("value", models.URLField(max_length=2000)),
                ("hcard", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="logos", to="core.hcard")),
            ],
        ),
        migrations.CreateModel(
            name="HCardPhoto",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("value", models.URLField(max_length=2000)),
                ("hcard", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="photos", to="core.hcard")),
            ],
        ),
        migrations.CreateModel(
            name="HCardTel",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("value", models.CharField(max_length=64)),
                ("hcard", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="tels", to="core.hcard")),
            ],
        ),
        migrations.CreateModel(
            name="HCardUrl",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("value", models.URLField(max_length=2000)),
                ("hcard", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="urls", to="core.hcard")),
            ],
        ),
    ]
