import markdown

from django.conf import settings
from django.core.validators import EmailValidator
from django.db import models
from django.utils.text import slugify
from django.utils.safestring import mark_safe
from django.contrib.contenttypes.fields import GenericRelation

from solo.models import SingletonModel
from files.models import Attachment, File


class Page(models.Model):
    title = models.CharField(max_length=512)
    slug = models.SlugField(max_length=255, unique=True)
    author = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    content = models.TextField()
    published_on = models.DateTimeField("date published")
    is_gallery = models.BooleanField(default=False)
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


class RequestErrorLog(models.Model):
    SOURCE_MICROPUB = "micropub"
    SOURCE_INDIEAUTH = "indieauth"
    SOURCE_CHOICES = [
        (SOURCE_MICROPUB, "Micropub"),
        (SOURCE_INDIEAUTH, "IndieAuth"),
    ]

    source = models.CharField(max_length=32, choices=SOURCE_CHOICES)
    method = models.CharField(max_length=10)
    path = models.CharField(max_length=255)
    status_code = models.PositiveSmallIntegerField()
    error = models.TextField(blank=True)
    request_headers = models.JSONField(default=dict)
    request_query = models.JSONField(default=dict)
    request_body = models.TextField(blank=True)
    response_body = models.TextField(blank=True)
    remote_addr = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(blank=True)
    content_type = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return f"{self.source} {self.method} {self.path} -> {self.status_code}"


class ThemeInstallManager(models.Manager):
    def expected_slugs(self) -> list[str]:
        """Return the ordered list of slugs this site expects to exist."""
        return list(self.get_queryset().order_by("slug").values_list("slug", flat=True))


class ThemeInstall(models.Model):
    SOURCE_UPLOAD = "upload"
    SOURCE_GIT = "git"
    SOURCE_STORAGE = "storage"
    SOURCE_CHOICES = [
        (SOURCE_UPLOAD, "Upload"),
        (SOURCE_GIT, "Git"),
        (SOURCE_STORAGE, "Storage"),
    ]

    STATUS_PENDING = "pending"
    STATUS_SUCCESS = "success"
    STATUS_FAILED = "failed"
    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending"),
        (STATUS_SUCCESS, "Success"),
        (STATUS_FAILED, "Failed"),
    ]

    slug = models.SlugField(max_length=255, unique=True)
    source_type = models.CharField(max_length=16, choices=SOURCE_CHOICES)
    source_url = models.URLField(max_length=2000, blank=True, default="")
    source_ref = models.CharField(max_length=255, blank=True, default="")
    version = models.CharField(max_length=255, blank=True, default="")
    checksum = models.CharField(max_length=255, blank=True, default="")
    installed_at = models.DateTimeField(auto_now_add=True)
    last_synced_commit = models.CharField(max_length=255, blank=True, default="")
    last_synced_at = models.DateTimeField(null=True, blank=True)
    last_sync_status = models.CharField(max_length=16, choices=STATUS_CHOICES, blank=True, default="")
    last_sync_error = models.CharField(max_length=500, blank=True, default="")

    objects = ThemeInstallManager()

    class Meta:
        ordering = ("slug",)

    def __str__(self) -> str:
        return self.slug

    def safe_source_url(self) -> str:
        if not self.source_url:
            return ""
        try:
            from urllib.parse import urlsplit, urlunsplit
        except ImportError:
            return self.source_url
        try:
            parts = urlsplit(self.source_url)
        except ValueError:
            return self.source_url
        netloc = parts.netloc
        if "@" in netloc:
            netloc = netloc.split("@", 1)[1]
        return urlunsplit((parts.scheme, netloc, parts.path, "", ""))

    def source_reference(self) -> str:
        return self.source_ref or self.safe_source_url()


class PluginInstall(models.Model):
    SOURCE_BUILTIN = "builtin"
    SOURCE_GIT = "git"
    SOURCE_CHOICES = [
        (SOURCE_BUILTIN, "Built-in"),
        (SOURCE_GIT, "Git"),
    ]

    STATUS_PENDING = "pending"
    STATUS_SUCCESS = "success"
    STATUS_FAILED = "failed"
    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending"),
        (STATUS_SUCCESS, "Success"),
        (STATUS_FAILED, "Failed"),
    ]

    name = models.SlugField(max_length=255, unique=True)
    django_app = models.CharField(max_length=255)
    label = models.CharField(max_length=255, blank=True, default="")
    source_type = models.CharField(max_length=16, choices=SOURCE_CHOICES)
    source_url = models.URLField(max_length=2000, blank=True, default="")
    source_ref = models.CharField(max_length=255, blank=True, default="")
    version = models.CharField(max_length=255, blank=True, default="")
    installed_at = models.DateTimeField(auto_now_add=True)
    last_synced_commit = models.CharField(max_length=255, blank=True, default="")
    last_synced_at = models.DateTimeField(null=True, blank=True)
    last_sync_status = models.CharField(max_length=16, choices=STATUS_CHOICES, blank=True, default="")
    last_sync_error = models.CharField(max_length=500, blank=True, default="")

    class Meta:
        ordering = ("name",)

    def __str__(self) -> str:
        return self.label or self.name


class SiteConfiguration(SingletonModel):
    title = models.CharField(max_length=255, default="", blank=True)
    tagline = models.CharField(max_length=1024, default="", blank=True)
    site_url = models.URLField(
        max_length=2000,
        default="",
        blank=True,
        help_text="Canonical base URL used when background jobs need to build absolute links.",
    )
    home_page = models.ForeignKey(
        "Page",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="site_home_pages",
        help_text="Optional page to display on the site homepage.",
    )
    favicon = models.ForeignKey(
        File,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="site_favicons",
        help_text="File to use as the site favicon.",
    )
    site_author = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        help_text="Default author for the site.",
    )
    active_theme = models.CharField(max_length=255, default="", blank=True)
    theme_settings = models.JSONField(default=dict, blank=True)
    robots_txt = models.TextField(default="", blank=True)
    main_menu = models.ForeignKey(Menu, null=True, on_delete=models.SET_NULL)
    footer_menu = models.ForeignKey(Menu, null=True, on_delete=models.SET_NULL, related_name="footer_siteconfigurations")
    bridgy_publish_bluesky = models.BooleanField("Bridgy Publish: Bluesky", default=True)
    bridgy_publish_flickr = models.BooleanField("Bridgy Publish: Flickr", default=True)
    bridgy_publish_github = models.BooleanField("Bridgy Publish: GitHub", default=True)
    comments_enabled = models.BooleanField("Comments enabled", default=False)
    developer_tools_enabled = models.BooleanField("Developer tools enabled", default=False)
    default_feed_kinds = models.CharField(
        max_length=255,
        default="",
        blank=True,
        help_text="Comma-separated list of post kinds shown by default when no filter is applied. Leave blank for the default (article, note, photo, activity, event, checkin). Valid values: article, note, photo, activity, like, repost, reply, event, rsvp, checkin, bookmark.",
    )
    microsub_unfollow_removes_entries = models.BooleanField(
        "Microsub unfollow removes history",
        default=False,
        help_text="If enabled, unfollowing a feed removes its previously fetched entries from the channel instead of leaving them visible.",
    )

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

    @property
    def primary_photo(self):
        return self.photos.order_by("sort_order", "id").first()

    @property
    def primary_photo_url(self):
        photo = self.primary_photo
        return photo.url if photo else ""


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

    def clean_fields(self, exclude=None):
        exclude = set(exclude or [])
        if self.kind == self.EMAIL:
            exclude.add("value")
        super().clean_fields(exclude=exclude)
        if self.kind == self.EMAIL:
            value = (self.value or "").strip()
            self.value = value
            validator = EmailValidator()
            validator(value)

    @property
    def href(self):
        if self.kind == self.EMAIL and self.value and not self.value.startswith("mailto:"):
            return f"mailto:{self.value}"
        return self.value


class HCardPhoto(models.Model):
    hcard = models.ForeignKey(HCard, on_delete=models.CASCADE, related_name="photos")
    asset = models.ForeignKey(
        File,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="hcard_photos",
    )
    value = models.URLField(max_length=2000)
    sort_order = models.PositiveIntegerField(default=0)

    def __str__(self):
        return self.value

    @property
    def url(self):
        if self.asset_id:
            return self.asset.file.url
        return self.value

    class Meta:
        ordering = ["sort_order", "id"]


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
