import markdown

from django.conf import settings
from django.db import models
from django.utils.text import slugify
from django.utils.safestring import mark_safe
from django.contrib.contenttypes.fields import GenericRelation

from solo.models import SingletonModel
from mdeditor.fields import MDTextField

from files.models import Attachment


class Page(models.Model):
    title = models.CharField(max_length=512)
    slug = models.SlugField(max_length=255, unique=True)
    author = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    content = MDTextField()
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


class Elsewhere(models.Model):
    X = 'x'; BLUESKY = 'bsky'; EMAIL = 'email'; MASTODON = 'mastodon'; GITHUB = 'github'; INSTAGRAM = 'instagram';
    PLACE_CHOICES = [(X, 'X/Twitter'), (BLUESKY, 'BSky'), (EMAIL, 'Email'), (MASTODON, 'Mastodon/ActivityPub'), (GITHUB, 'GitHub'), (INSTAGRAM, 'Instagram')]

    text = models.CharField(max_length=128)
    profile_url = models.CharField(max_length=1024)
    place = models.CharField(max_length=16, choices=PLACE_CHOICES, default=X)

    def __str__(self):
        return self.text


class SiteConfiguration(SingletonModel):
    title = models.CharField(max_length=255)
    tagline = models.CharField(max_length=1024)
    intro = MDTextField(max_length=512, default="")
    bio = MDTextField()
    main_menu = models.ForeignKey(Menu, null=True, on_delete=models.SET_NULL)
    mastodon_base_url = models.CharField(max_length=255, blank=True, default="")
    mastodon_client_id = models.CharField(max_length=255, blank=True, default="")
    mastodon_client_secret = models.CharField(max_length=255, blank=True, default="")
    mastodon_access_token = models.CharField(max_length=512, blank=True, default="")
    mastodon_refresh_token = models.CharField(max_length=512, blank=True, default="")
    mastodon_token_expires_at = models.DateTimeField(null=True, blank=True)
    bluesky_service = models.CharField(max_length=255, blank=True, default="")
    bluesky_client_id = models.CharField(max_length=255, blank=True, default="")
    bluesky_client_secret = models.CharField(max_length=255, blank=True, default="")
    bluesky_access_token = models.CharField(max_length=512, blank=True, default="")
    bluesky_refresh_token = models.CharField(max_length=512, blank=True, default="")
    bluesky_token_expires_at = models.DateTimeField(null=True, blank=True)
    bluesky_did = models.CharField(max_length=255, blank=True, default="")
    bluesky_handle = models.CharField(max_length=255, blank=True, default="")

    def __str__(self):
        return "Site Configuration"

    class Meta:
        verbose_name = "Site Configuration"
