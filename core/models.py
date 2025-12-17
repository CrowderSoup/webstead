import markdown

from django.conf import settings
from django.db import models
from django.utils.text import slugify
from django.utils.safestring import mark_safe
from django.contrib.contenttypes.fields import GenericRelation

from solo.models import SingletonModel
from files.models import Attachment


class Page(models.Model):
    title = models.CharField(max_length=512)
    slug = models.SlugField(max_length=255, unique=True)
    author = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    content = models.TextField()
    published_on = models.DateTimeField("date published")
    attachments = GenericRelation(Attachment, related_query_name="pages")


    def __str__(self):
        return self.title

    def save(self, *args, **kwargs):
        if not self.slug:
            base = slugify(self.title or '') or 'page'
            slug = base
            i = 2
            # Ensure uniqueness without race conditions
            while Page.objects.filter(slug=slug).exclude(pk=self.pk).exists():
                slug = f"{base}-{i}"
                i += 1
            self.slug = slug
        super().save(*args, **kwargs)

    def html(self):
        md = markdown.Markdown(extensions=["fenced_code"])
        return mark_safe(md.convert(self.content))


class Menu(models.Model):
    title = models.CharField(max_length=64)

    def __str__(self):
        return self.title


class MenuItem(models.Model):
    text = models.CharField(max_length=512)
    menu = models.ForeignKey(Menu, on_delete=models.CASCADE)
    url = models.CharField(max_length=2000)
    weight = models.IntegerField(default=0)

    def __str__(self):
        return self.text
    
    class Meta:
        ordering = ['weight']


class Redirect(models.Model):
    TEMPORARY = 'temporary'; PERMANENTLY = 'permanently';
    REDIRECT_TYPE_CHOICES = [(TEMPORARY, "307 Temporary Redirect"), (PERMANENTLY, "301 Moved Permanently")]

    from_path = models.CharField()
    to_path = models.CharField()
    redirect_type = models.CharField(max_length=16, choices=REDIRECT_TYPE_CHOICES, default=TEMPORARY)

    def __str__(self):
        return f"{self.from_path} ➡️ {self.to_path}"


class SiteConfiguration(SingletonModel):
    title = models.CharField(max_length=255, default="", blank=True)
    tagline = models.CharField(max_length=1024, default="", blank=True)
    site_author = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        help_text="Default author for the site.",
    )
    active_theme = models.CharField(max_length=255, default="", blank=True)
    robots_txt = models.TextField(default="", blank=True)
    main_menu = models.ForeignKey(Menu, null=True, on_delete=models.SET_NULL)
    footer_menu = models.ForeignKey(Menu, null=True, on_delete=models.SET_NULL, related_name="footer_siteconfigurations")

    def __str__(self):
        return "Site Configuration"

    class Meta:
        verbose_name = "Site Configuration"

    def save(self, *args, **kwargs):
        previous_theme = None
        if self.pk:
            previous_theme = (
                SiteConfiguration.objects.filter(pk=self.pk)
                .values_list("active_theme", flat=True)
                .first()
            )

        result = super().save(*args, **kwargs)

        if previous_theme != self.active_theme:
            from core.themes import clear_template_caches

            clear_template_caches()

        return result


class HCard(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="hcards",
    )

    name = models.CharField(max_length=255, blank=True, default="")
    honorific_prefix = models.CharField(max_length=255, blank=True, default="")
    given_name = models.CharField(max_length=255, blank=True, default="")
    additional_name = models.CharField(max_length=255, blank=True, default="")
    family_name = models.CharField(max_length=255, blank=True, default="")
    honorific_suffix = models.CharField(max_length=255, blank=True, default="")
    nickname = models.CharField(max_length=255, blank=True, default="")
    sort_string = models.CharField(max_length=255, blank=True, default="")

    uid = models.URLField(max_length=2000, blank=True, default="")
    bday = models.DateField(null=True, blank=True)
    anniversary = models.DateField(null=True, blank=True)

    org_name = models.CharField(max_length=255, blank=True, default="")
    org_hcard = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="org_members",
    )
    job_title = models.CharField(max_length=255, blank=True, default="")
    role = models.CharField(max_length=255, blank=True, default="")

    post_office_box = models.CharField(max_length=255, blank=True, default="")
    extended_address = models.CharField(max_length=255, blank=True, default="")
    street_address = models.CharField(max_length=255, blank=True, default="")
    locality = models.CharField(max_length=255, blank=True, default="")
    region = models.CharField(max_length=255, blank=True, default="")
    postal_code = models.CharField(max_length=64, blank=True, default="")
    country_name = models.CharField(max_length=255, blank=True, default="")
    label = models.CharField(max_length=512, blank=True, default="")

    latitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    longitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    altitude = models.DecimalField(max_digits=9, decimal_places=2, null=True, blank=True)

    note = models.TextField(blank=True, default="")
    sex = models.CharField(max_length=64, blank=True, default="")
    gender_identity = models.CharField(max_length=255, blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.name or self.nickname or f"HCard {self.pk}"


class HCardEmail(models.Model):
    hcard = models.ForeignKey(HCard, on_delete=models.CASCADE, related_name="emails")
    value = models.CharField(max_length=254)

    def __str__(self):
        return self.value


class HCardUrl(models.Model):
    X = "x"
    BLUESKY = "bsky"
    EMAIL = "email"
    MASTODON = "mastodon"
    GITHUB = "github"
    INSTAGRAM = "instagram"
    OTHER = "other"
    KIND_CHOICES = [
        (X, "X/Twitter"),
        (BLUESKY, "BSky"),
        (EMAIL, "Email"),
        (MASTODON, "Mastodon/ActivityPub"),
        (GITHUB, "GitHub"),
        (INSTAGRAM, "Instagram"),
        (OTHER, "Other"),
    ]

    hcard = models.ForeignKey(HCard, on_delete=models.CASCADE, related_name="urls")
    value = models.URLField(max_length=2000)
    kind = models.CharField(max_length=16, choices=KIND_CHOICES, default=OTHER)

    def __str__(self):
        return self.value

    @property
    def href(self):
        if self.kind == self.EMAIL and self.value and not self.value.startswith("mailto:"):
            return f"mailto:{self.value}"
        return self.value


class HCardPhoto(models.Model):
    hcard = models.ForeignKey(HCard, on_delete=models.CASCADE, related_name="photos")
    value = models.URLField(max_length=2000)

    def __str__(self):
        return self.value


class HCardLogo(models.Model):
    hcard = models.ForeignKey(HCard, on_delete=models.CASCADE, related_name="logos")
    value = models.URLField(max_length=2000)

    def __str__(self):
        return self.value


class HCardTel(models.Model):
    hcard = models.ForeignKey(HCard, on_delete=models.CASCADE, related_name="tels")
    value = models.CharField(max_length=64)

    def __str__(self):
        return self.value


class HCardCategory(models.Model):
    hcard = models.ForeignKey(HCard, on_delete=models.CASCADE, related_name="categories")
    value = models.CharField(max_length=255)

    def __str__(self):
        return self.value


class HCardImpp(models.Model):
    hcard = models.ForeignKey(HCard, on_delete=models.CASCADE, related_name="impps")
    value = models.CharField(max_length=2000)

    def __str__(self):
        return self.value


class HCardKey(models.Model):
    hcard = models.ForeignKey(HCard, on_delete=models.CASCADE, related_name="keys")
    value = models.CharField(max_length=2000)

    def __str__(self):
        return self.value
