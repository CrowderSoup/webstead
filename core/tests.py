import importlib
import io
import json
import tempfile
import zipfile
from datetime import timedelta
from pathlib import Path
from typing import Optional
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
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
    ThemeInstall,
)
from .apps import CoreConfig
from .theme_sync import reconcile_installed_themes
from .themes import ThemeUploadError, get_theme, ingest_theme_archive
from .theme_validation import validate_theme_dir
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


class ThemeStartupReconcileTests(TestCase):
    def test_ready_reconciles_themes_from_installed_records(self):
        with tempfile.TemporaryDirectory() as storage_root, tempfile.TemporaryDirectory() as themes_root:
            storage = FileSystemStorage(location=storage_root)
            slug = "sample"
            storage_theme_dir = Path(storage_root) / "themes" / slug
            (storage_theme_dir / "templates").mkdir(parents=True)
            (storage_theme_dir / "static").mkdir(exist_ok=True)
            (storage_theme_dir / "theme.json").write_text('{"label": "Sample"}')
            (storage_theme_dir / "templates" / "base.html").write_text("hello")
            (storage_theme_dir / "static" / "style.css").write_text("body{}")
            ThemeInstall.objects.create(slug=slug, source_type=ThemeInstall.SOURCE_STORAGE)

            with override_settings(
                THEMES_ROOT=themes_root,
                THEME_STORAGE_PREFIX="themes",
                THEMES_STARTUP_RECONCILE=True,
            ), mock.patch("core.themes.get_theme_storage", return_value=storage):
                with self.assertLogs("core.apps", level="INFO") as logs:
                    CoreConfig("core", importlib.import_module("core")).ready()

            local_theme_dir = Path(themes_root) / slug
            self.assertTrue((local_theme_dir / "theme.json").exists())
            self.assertTrue((local_theme_dir / "templates" / "base.html").exists())
            record = ThemeInstall.objects.get(slug=slug)
            self.assertEqual(record.last_sync_status, ThemeInstall.STATUS_SUCCESS)
            self.assertIn("Reconciled 1 theme(s) on startup", "\n".join(logs.output))

    def test_ready_logs_warning_when_reconcile_unavailable(self):
        with override_settings(THEMES_STARTUP_RECONCILE=True):
            with mock.patch("core.apps.reconcile_installed_themes", side_effect=Exception("boom")):
                with self.assertLogs("core.apps", level="WARNING") as logs:
                    CoreConfig("core", importlib.import_module("core")).ready()

        self.assertIn("Skipping theme reconciliation on startup", "\n".join(logs.output))


class ThemeReconciliationTests(TestCase):
    def _create_local_theme(self, root: Path, slug: str = "sample") -> Path:
        theme_dir = root / slug
        (theme_dir / "templates").mkdir(parents=True, exist_ok=True)
        (theme_dir / "static").mkdir(parents=True, exist_ok=True)
        (theme_dir / "theme.json").write_text(json.dumps({"slug": slug, "label": "Sample"}))
        return theme_dir

    def test_restores_missing_local_from_storage(self):
        with tempfile.TemporaryDirectory() as storage_root, tempfile.TemporaryDirectory() as themes_root:
            storage = FileSystemStorage(location=storage_root)
            slug = "sample"
            storage_theme_dir = Path(storage_root) / "themes" / slug
            (storage_theme_dir / "templates").mkdir(parents=True)
            (storage_theme_dir / "static").mkdir(exist_ok=True)
            (storage_theme_dir / "theme.json").write_text('{"label": "Sample"}')
            ThemeInstall.objects.create(slug=slug, source_type=ThemeInstall.SOURCE_STORAGE)

            with override_settings(
                THEMES_ROOT=themes_root,
                THEME_STORAGE_PREFIX="themes",
            ), mock.patch("core.themes.get_theme_storage", return_value=storage):
                results = reconcile_installed_themes()

            record = ThemeInstall.objects.get(slug=slug)
            local_theme_dir = Path(themes_root) / slug
            self.assertTrue((local_theme_dir / "theme.json").exists())
            self.assertEqual(record.last_sync_status, ThemeInstall.STATUS_SUCCESS)
            self.assertTrue(any(result.restored for result in results))

    def test_warns_when_storage_missing_and_upload_disabled(self):
        with tempfile.TemporaryDirectory() as storage_root, tempfile.TemporaryDirectory() as themes_root:
            storage = FileSystemStorage(location=storage_root)
            slug = "sample"
            ThemeInstall.objects.create(slug=slug, source_type=ThemeInstall.SOURCE_UPLOAD)
            self._create_local_theme(Path(themes_root), slug=slug)

            with override_settings(
                THEMES_ROOT=themes_root,
                THEME_STORAGE_PREFIX="themes",
                THEMES_STARTUP_UPLOAD_MISSING=False,
            ), mock.patch("core.themes.get_theme_storage", return_value=storage):
                with self.assertLogs("core.theme_sync", level="WARNING") as logs:
                    results = reconcile_installed_themes()

                record = ThemeInstall.objects.get(slug=slug)
                self.assertEqual(record.last_sync_status, ThemeInstall.STATUS_FAILED)
                self.assertIn("missing from storage", "\n".join(logs.output).lower())
                self.assertFalse(any(result.restored for result in results))

    def test_rehydrates_missing_git_theme(self):
        with tempfile.TemporaryDirectory() as themes_root:
            slug = "sample"
            install = ThemeInstall.objects.create(
                slug=slug,
                source_type=ThemeInstall.SOURCE_GIT,
                source_url="https://example.com/themes.git",
                source_ref="main",
            )

            def _fake_rehydrate(target_install, *, base_dir=None):
                theme_dir = Path(base_dir or themes_root) / target_install.slug
                (theme_dir / "templates").mkdir(parents=True, exist_ok=True)
                (theme_dir / "static").mkdir(parents=True, exist_ok=True)
                (theme_dir / "theme.json").write_text(json.dumps({"slug": slug, "label": "Sample"}))
                return True

            with override_settings(THEMES_ROOT=themes_root):
                with mock.patch("core.theme_sync.rehydrate_theme_from_git", side_effect=_fake_rehydrate) as rehydrate:
                    results = reconcile_installed_themes()

            install.refresh_from_db()
            rehydrate.assert_called_once()
            self.assertEqual(install.last_sync_status, ThemeInstall.STATUS_SUCCESS)
            self.assertTrue(any(result.restored for result in results))
            self.assertTrue((Path(themes_root) / slug / "theme.json").exists())

    def test_storage_unavailable_sets_failure_status(self):
        with tempfile.TemporaryDirectory() as themes_root:
            slug = "sample"
            ThemeInstall.objects.create(slug=slug, source_type=ThemeInstall.SOURCE_UPLOAD)
            self._create_local_theme(Path(themes_root), slug=slug)

            with override_settings(THEMES_ROOT=themes_root):
                with mock.patch("core.theme_sync.theme_exists_in_storage", side_effect=Exception("boom")):
                    with self.assertLogs("core.theme_sync", level="WARNING") as logs:
                        reconcile_installed_themes()

            record = ThemeInstall.objects.get(slug=slug)
            self.assertEqual(record.last_sync_status, ThemeInstall.STATUS_FAILED)
            self.assertIn("storage unavailable", "\n".join(logs.output).lower())


class ThemeValidationTests(TestCase):
    def _build_theme_dir(
        self,
        root: Path,
        *,
        slug: str = "sample",
        include_templates: bool = True,
        include_static: bool = True,
        metadata: Optional[dict] = None,
    ) -> Path:
        theme_dir = root / slug
        theme_dir.mkdir(parents=True, exist_ok=True)
        if include_templates:
            (theme_dir / "templates").mkdir(parents=True, exist_ok=True)
        if include_static:
            (theme_dir / "static").mkdir(parents=True, exist_ok=True)
        payload = metadata or {"slug": slug, "label": "Sample", "version": "1.0"}
        (theme_dir / "theme.json").write_text(json.dumps(payload))
        return theme_dir

    def test_validate_theme_dir_passes_valid_theme(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            theme_dir = self._build_theme_dir(Path(tmp_dir))

            result = validate_theme_dir(theme_dir)

        self.assertTrue(result.is_valid)
        self.assertEqual(result.slug, "sample")
        self.assertEqual(result.metadata.get("version"), "1.0")

    def test_validate_theme_dir_flags_missing_files_and_slug_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            metadata = {"slug": "other", "label": "Broken", "version": 2}
            theme_dir = self._build_theme_dir(
                Path(tmp_dir),
                slug="folder-slug",
                include_templates=False,
                include_static=False,
                metadata=metadata,
            )

            result = validate_theme_dir(theme_dir, expected_slug="expected-slug")

        codes = {issue.code for issue in result.errors}
        self.assertFalse(result.is_valid)
        self.assertIn("missing_templates", codes)
        self.assertIn("missing_static", codes)
        self.assertIn("slug_mismatch_directory", codes)
        self.assertIn("slug_mismatch_expected", codes)
        self.assertIn("invalid_version", codes)

    def test_validate_theme_dir_requires_theme_json(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            theme_dir = Path(tmp_dir) / "no-meta"
            theme_dir.mkdir()

            result = validate_theme_dir(theme_dir)

        codes = {issue.code for issue in result.errors}
        self.assertFalse(result.is_valid)
        self.assertIn("missing_meta", codes)
        self.assertIn("missing_templates", codes)
        self.assertIn("missing_static", codes)


class ThemeInstallTests(TestCase):
    def _theme_archive(self, *, slug: str = "sample", version: str = "1.0") -> SimpleUploadedFile:
        buffer = io.BytesIO()
        metadata = {"label": "Sample"}
        if slug:
            metadata["slug"] = slug
        if version:
            metadata["version"] = version
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("theme.json", json.dumps(metadata))
            archive.writestr("templates/base.html", "hello")
            archive.writestr("static/style.css", "body{}")
        buffer.seek(0)
        return SimpleUploadedFile("theme.zip", buffer.read(), content_type="application/zip")

    def test_upload_rejects_invalid_theme(self):
        with tempfile.TemporaryDirectory() as storage_root, tempfile.TemporaryDirectory() as themes_root:
            storage = FileSystemStorage(location=storage_root)
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, "w") as archive:
                archive.writestr("theme.json", json.dumps({"slug": "broken", "label": "Broken"}))
            buffer.seek(0)
            upload = SimpleUploadedFile("broken.zip", buffer.read(), content_type="application/zip")

            with override_settings(THEMES_ROOT=themes_root, THEME_STORAGE_PREFIX="themes"):
                with mock.patch("core.themes.get_theme_storage", return_value=storage):
                    with self.assertRaises(ThemeUploadError) as exc:
                        ingest_theme_archive(upload)

        message = str(exc.exception)
        self.assertIn("templates/", message)
        self.assertIn("static/", message)
        self.assertFalse((Path(themes_root) / "broken").exists())

    def test_upload_creates_theme_install_record(self):
        with tempfile.TemporaryDirectory() as storage_root, tempfile.TemporaryDirectory() as themes_root:
            storage = FileSystemStorage(location=storage_root)
            upload = self._theme_archive()

            with override_settings(THEMES_ROOT=themes_root, THEME_STORAGE_PREFIX="themes"):
                with mock.patch("core.themes.get_theme_storage", return_value=storage):
                    theme = ingest_theme_archive(upload)

        record = ThemeInstall.objects.get(slug="sample")

        self.assertEqual(record.source_type, ThemeInstall.SOURCE_UPLOAD)
        self.assertEqual(record.version, "1.0")
        self.assertEqual(record.last_sync_status, ThemeInstall.STATUS_SUCCESS)
        self.assertIsNotNone(record.last_synced_at)
        self.assertEqual(theme.slug, record.slug)

    def test_upload_updates_existing_install_record(self):
        with tempfile.TemporaryDirectory() as storage_root, tempfile.TemporaryDirectory() as themes_root:
            storage = FileSystemStorage(location=storage_root)
            previous = ThemeInstall.objects.create(
                slug="sample",
                source_type=ThemeInstall.SOURCE_GIT,
                source_url="https://example.com/themes.git",
                source_ref="main",
                version="0.1",
                checksum="abc123",
                last_synced_at=timezone.now() - timedelta(days=1),
                last_sync_status=ThemeInstall.STATUS_FAILED,
            )
            installed_at = previous.installed_at
            upload = self._theme_archive(version="2.0")

            with override_settings(THEMES_ROOT=themes_root, THEME_STORAGE_PREFIX="themes"):
                with mock.patch("core.themes.get_theme_storage", return_value=storage):
                    ingest_theme_archive(upload)

        previous.refresh_from_db()

        self.assertEqual(previous.installed_at, installed_at)
        self.assertEqual(previous.source_type, ThemeInstall.SOURCE_UPLOAD)
        self.assertEqual(previous.source_url, "")
        self.assertEqual(previous.source_ref, "")
        self.assertEqual(previous.version, "2.0")
        self.assertEqual(previous.checksum, "")
        self.assertEqual(previous.last_sync_status, ThemeInstall.STATUS_SUCCESS)
        self.assertGreater(previous.last_synced_at, installed_at)

    def test_expected_slugs_returns_sorted_list(self):
        ThemeInstall.objects.create(slug="beta", source_type=ThemeInstall.SOURCE_UPLOAD)
        ThemeInstall.objects.create(slug="alpha", source_type=ThemeInstall.SOURCE_UPLOAD)

        self.assertEqual(ThemeInstall.objects.expected_slugs(), ["alpha", "beta"])
