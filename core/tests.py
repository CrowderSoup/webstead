import importlib
import tempfile
from pathlib import Path
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.test.utils import override_settings
from django.urls import reverse
from django.utils import timezone
from django.core.files.storage import FileSystemStorage

from .models import (
    Menu,
    MenuItem,
    Page,
    Redirect,
    SiteConfiguration,
    HCard,
    HCardEmail,
    HCardUrl,
)
from .apps import CoreConfig
from .themes import get_theme
from blog.models import Post, Tag


class PageModelTests(TestCase):
    def test_html_renders_markdown(self):
        page = Page.objects.create(
            title="About",
            slug="about",
            content="**bold** text",
            published_on=timezone.now(),
        )

        rendered = page.html()

        self.assertIn("<strong>bold</strong>", rendered)

    def test_slug_generation_handles_duplicates(self):
        Page.objects.create(
            title="Contact",
            slug="contact",
            content="text",
            published_on=timezone.now(),
        )

        duplicate = Page(title="Contact", content="text", published_on=timezone.now())
        duplicate.save()

        self.assertEqual(duplicate.slug, "contact-2")


class MenuItemTests(TestCase):
    def test_items_are_ordered_by_weight(self):
        menu = Menu.objects.create(title="Main")
        second = MenuItem.objects.create(menu=menu, text="Second", url="/second", weight=10)
        first = MenuItem.objects.create(menu=menu, text="First", url="/first", weight=0)
        third = MenuItem.objects.create(menu=menu, text="Third", url="/third", weight=20)

        ordered_text = [item.text for item in MenuItem.objects.filter(menu=menu)]

        self.assertEqual(ordered_text, [first.text, second.text, third.text])


class RedirectMiddlewareTests(TestCase):
    def test_permanent_redirects_to_target_path(self):
        Redirect.objects.create(
            from_path="/old/",
            to_path="/new/",
            redirect_type=Redirect.PERMANENTLY,
        )

        response = self.client.get("/old/")

        self.assertEqual(response.status_code, 301)
        self.assertEqual(response["Location"], "/new/")

    def test_temporary_redirect_uses_307(self):
        Redirect.objects.create(
            from_path="/temp/",
            to_path="/hot/",
            redirect_type=Redirect.TEMPORARY,
        )

        response = self.client.get("/temp/")

        self.assertEqual(response.status_code, 307)
        self.assertEqual(response["Location"], "/hot/")


class RobotsTxtTests(TestCase):
    def test_returns_configured_content(self):
        settings = SiteConfiguration.get_solo()
        settings.robots_txt = "User-agent: *\nDisallow: /admin/"
        settings.save()

        response = self.client.get("/robots.txt")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response["Content-Type"].startswith("text/plain"))
        self.assertEqual(response.content.decode(), settings.robots_txt)


class SitemapTests(TestCase):
    def test_includes_public_routes_and_excludes_admin(self):
        page = Page.objects.create(
            title="About",
            slug="about",
            content="text",
            published_on=timezone.now(),
        )
        tag = Tag.objects.create(tag="news")
        post = Post.objects.create(
            title="Hello",
            slug="hello",
            content="text",
            kind=Post.ARTICLE,
            published_on=timezone.now(),
        )
        post.tags.add(tag)
        Post.objects.create(
            title="Draft",
            slug="draft",
            content="text",
            kind=Post.ARTICLE,
            published_on=None,
        )

        response = self.client.get("/sitemap.xml")

        body = response.content.decode()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response["Content-Type"].startswith("application/xml"))
        self.assertIn("http://testserver/", body)
        self.assertIn(f"http://testserver{reverse('posts')}", body)
        self.assertIn(f"http://testserver{reverse('page', kwargs={'slug': page.slug})}", body)
        self.assertIn(f"http://testserver{post.get_absolute_url()}", body)
        self.assertIn(f"http://testserver{reverse('posts_by_tag', kwargs={'tag': tag.tag})}", body)
        self.assertNotIn("/admin/", body)


class HCardTests(TestCase):
    def test_can_create_empty_hcard(self):
        hcard = HCard.objects.create()

        self.assertIsNotNone(hcard.pk)

    def test_can_attach_multiple_related_values(self):
        hcard = HCard.objects.create(name="Example")
        HCardEmail.objects.create(hcard=hcard, value="one@example.com")
        HCardEmail.objects.create(hcard=hcard, value="two@example.com")
        HCardUrl.objects.create(hcard=hcard, value="https://example.com")
        HCardUrl.objects.create(hcard=hcard, value="https://example.org")

        self.assertEqual(hcard.emails.count(), 2)
        self.assertEqual(hcard.urls.count(), 2)

    def test_can_assign_and_unassign_user(self):
        user = get_user_model().objects.create_user(
            username="person",
            email="person@example.com",
            password="password",
        )
        hcard = HCard.objects.create(user=user)

        self.assertEqual(hcard.user, user)

        hcard.user = None
        hcard.save()

        hcard.refresh_from_db()
        self.assertIsNone(hcard.user)


class ThemeStartupSyncTests(TestCase):
    def test_ready_syncs_themes_from_storage(self):
        with tempfile.TemporaryDirectory() as storage_root, tempfile.TemporaryDirectory() as themes_root:
            storage = FileSystemStorage(location=storage_root)
            slug = "sample"
            storage_theme_dir = Path(storage_root) / "themes" / slug
            (storage_theme_dir / "templates").mkdir(parents=True)
            (storage_theme_dir / "static").mkdir(exist_ok=True)
            (storage_theme_dir / "theme.json").write_text('{"label": "Sample"}')
            (storage_theme_dir / "templates" / "base.html").write_text("hello")
            (storage_theme_dir / "static" / "style.css").write_text("body{}")

            with override_settings(
                THEMES_ROOT=themes_root,
                THEME_STORAGE_PREFIX="themes",
                THEME_STARTUP_SYNC_ENABLED=True,
            ), mock.patch("core.themes.get_theme_storage", return_value=storage):
                with self.assertLogs("core.apps", level="INFO") as logs:
                    CoreConfig("core", importlib.import_module("core")).ready()

                local_theme_dir = Path(themes_root) / slug
                self.assertTrue((local_theme_dir / "theme.json").exists())
                self.assertTrue((local_theme_dir / "templates" / "base.html").exists())
                theme = get_theme(slug)
                self.assertIsNotNone(theme)
                self.assertIn("Synced 1 theme(s) from storage on startup", "\n".join(logs.output))

    def test_ready_logs_warning_when_storage_unavailable(self):
        with override_settings(THEME_STARTUP_SYNC_ENABLED=True):
            with mock.patch("core.apps.sync_themes_from_storage", side_effect=Exception("boom")):
                with self.assertLogs("core.apps", level="WARNING") as logs:
                    CoreConfig("core", importlib.import_module("core")).ready()

        self.assertIn("Skipping theme sync on startup", "\n".join(logs.output))
