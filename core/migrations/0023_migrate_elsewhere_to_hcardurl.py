from django.conf import settings
from django.db import migrations, models


def migrate_elsewhere_to_hcardurl(apps, schema_editor):
    SiteConfiguration = apps.get_model("core", "SiteConfiguration")
    HCard = apps.get_model("core", "HCard")
    HCardUrl = apps.get_model("core", "HCardUrl")
    Elsewhere = apps.get_model("core", "Elsewhere")

    config = SiteConfiguration.objects.first()
    if not config or not config.site_author_id:
        return

    app_label, model_name = settings.AUTH_USER_MODEL.split(".")
    User = apps.get_model(app_label, model_name)
    site_author = User.objects.filter(pk=config.site_author_id).first()
    if not site_author:
        return

    hcard = HCard.objects.filter(user_id=site_author.pk).order_by("pk").first()
    if not hcard:
        hcard = HCard.objects.create(user_id=site_author.pk)

    for elsewhere in Elsewhere.objects.all():
        value = elsewhere.profile_url
        kind = elsewhere.place
        if kind == "email" and value and not value.startswith("mailto:"):
            value = f"mailto:{value}"
        HCardUrl.objects.create(hcard_id=hcard.pk, value=value, kind=kind)


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0022_remove_siteconfiguration_intro_bio"),
    ]

    operations = [
        migrations.AddField(
            model_name="hcardurl",
            name="kind",
            field=models.CharField(
                choices=[
                    ("x", "X/Twitter"),
                    ("bsky", "BSky"),
                    ("email", "Email"),
                    ("mastodon", "Mastodon/ActivityPub"),
                    ("github", "GitHub"),
                    ("instagram", "Instagram"),
                    ("other", "Other"),
                ],
                default="other",
                max_length=16,
            ),
        ),
        migrations.RunPython(migrate_elsewhere_to_hcardurl, noop_reverse),
        migrations.DeleteModel(
            name="Elsewhere",
        ),
    ]
