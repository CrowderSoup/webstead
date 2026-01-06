import importlib
import io
import json
import os
import subprocess
import tempfile
import zipfile
from datetime import timedelta
from pathlib import Path
from typing import Optional
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.core.management.base import CommandError
from django.template import Context, Template
from django.test import TestCase, RequestFactory
from django.test.utils import override_settings
from django.urls import reverse
from django.templatetags.static import static
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
from .apps import CoreConfig, _reset_startup_state, _run_startup_reconcile
from .theme_sync import reconcile_installed_themes
from .themes import (
    ThemeUploadError,
    get_theme,
    ingest_theme_archive,
    install_theme_from_git,
    theme_exists_in_storage,
    update_theme_from_git,
)
from .theme_validation import validate_theme_dir
from .test_utils import build_test_theme
from .views import server_error
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


class ServerErrorHandlerTests(TestCase):
    def setUp(self):
        super().setUp()
        self.factory = RequestFactory()

    def test_renders_without_error(self):
        SiteConfiguration.get_solo()

        request = self.factory.get("/__server_error__/")
        response = server_error(request)

        self.assertEqual(response.status_code, 500)
        self.assertIn("Server error", response.content.decode())


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
    def setUp(self):
        super().setUp()
        _reset_startup_state()

    def test_ready_reconciles_themes_from_installed_records(self):
        with tempfile.TemporaryDirectory() as storage_root, tempfile.TemporaryDirectory() as themes_root:
            storage = FileSystemStorage(location=storage_root)
            slug = "sample"
            build_test_theme(slug, Path(storage_root) / "themes")
            ThemeInstall.objects.create(slug=slug, source_type=ThemeInstall.SOURCE_STORAGE)

            with override_settings(
                THEMES_ROOT=themes_root,
                THEME_STORAGE_PREFIX="themes",
                THEMES_STARTUP_RECONCILE=True,
            ), mock.patch("core.themes.get_theme_storage", return_value=storage):
                with self.assertLogs("core.apps", level="INFO") as logs:
                    CoreConfig("core", importlib.import_module("core")).ready()
                    _run_startup_reconcile()

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
                    _run_startup_reconcile()

        self.assertIn("Skipping theme reconciliation on startup", "\n".join(logs.output))


class ThemeReconciliationTests(TestCase):
    def test_restores_missing_local_from_storage(self):
        with tempfile.TemporaryDirectory() as storage_root, tempfile.TemporaryDirectory() as themes_root:
            storage = FileSystemStorage(location=storage_root)
            slug = "sample"
            build_test_theme(slug, Path(storage_root) / "themes")
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
            build_test_theme(slug, Path(themes_root))

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
                build_test_theme(target_install.slug, base_dir or themes_root)
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
            build_test_theme(slug, Path(themes_root))

            with override_settings(THEMES_ROOT=themes_root):
                with mock.patch("core.theme_sync.theme_exists_in_storage", side_effect=Exception("boom")):
                    with self.assertLogs("core.theme_sync", level="WARNING") as logs:
                        reconcile_installed_themes()

            record = ThemeInstall.objects.get(slug=slug)
            self.assertEqual(record.last_sync_status, ThemeInstall.STATUS_FAILED)
            self.assertIn("storage unavailable", "\n".join(logs.output).lower())

    def test_dry_run_reports_without_writing(self):
        with tempfile.TemporaryDirectory() as storage_root, tempfile.TemporaryDirectory() as themes_root:
            storage = FileSystemStorage(location=storage_root)
            slug = "sample"
            build_test_theme(slug, Path(storage_root) / "themes")
            install = ThemeInstall.objects.create(slug=slug, source_type=ThemeInstall.SOURCE_STORAGE)

            with override_settings(
                THEMES_ROOT=themes_root,
                THEME_STORAGE_PREFIX="themes",
            ), mock.patch("core.themes.get_theme_storage", return_value=storage):
                results = reconcile_installed_themes(dry_run=True)

            install.refresh_from_db()
            self.assertEqual(install.last_sync_status, "")
            self.assertFalse((Path(themes_root) / slug / "theme.json").exists())
            self.assertEqual(results[0].action, "downloaded")


class ThemeReconcileCommandTests(TestCase):
    def test_strict_mode_raises_on_failure(self):
        with tempfile.TemporaryDirectory() as themes_root:
            slug = "sample"
            ThemeInstall.objects.create(slug=slug, source_type=ThemeInstall.SOURCE_UPLOAD)
            build_test_theme(slug, Path(themes_root))

            with override_settings(THEMES_ROOT=themes_root):
                with mock.patch("core.theme_sync.theme_exists_in_storage", return_value=False):
                    with self.assertLogs("core.theme_sync", level="WARNING"):
                        with self.assertRaises(CommandError):
                            call_command(
                                "theme_reconcile",
                                "--strict",
                                verbosity=0,
                                stdout=io.StringIO(),
                                stderr=io.StringIO(),
                            )


class ThemeListCommandTests(TestCase):
    def test_list_command_outputs_text(self):
        ThemeInstall.objects.create(
            slug="alpha",
            source_type=ThemeInstall.SOURCE_UPLOAD,
            last_sync_status=ThemeInstall.STATUS_SUCCESS,
        )

        out = io.StringIO()
        call_command("theme_list", stdout=out)
        output = out.getvalue()

        self.assertIn("SLUG", output)
        self.assertIn("alpha", output)
        self.assertIn("upload", output)

    def test_list_command_outputs_json(self):
        ThemeInstall.objects.create(
            slug="beta",
            source_type=ThemeInstall.SOURCE_GIT,
            source_url="https://user:pass@example.com/themes.git?token=secret",
            source_ref="main",
            last_sync_status=ThemeInstall.STATUS_FAILED,
        )

        out = io.StringIO()
        call_command("theme_list", "--json", stdout=out)
        payload = json.loads(out.getvalue())

        self.assertEqual(len(payload), 1)
        self.assertEqual(payload[0]["slug"], "beta")
        self.assertEqual(payload[0]["source_ref"], "main")
        self.assertEqual(payload[0]["source_url"], "https://example.com/themes.git")

    def test_list_command_filters_by_slug(self):
        ThemeInstall.objects.create(slug="alpha", source_type=ThemeInstall.SOURCE_UPLOAD)
        ThemeInstall.objects.create(slug="beta", source_type=ThemeInstall.SOURCE_UPLOAD)

        out = io.StringIO()
        call_command("theme_list", "--slug", "beta", stdout=out)
        output = out.getvalue()

        self.assertIn("beta", output)
        self.assertNotIn("alpha", output)


class ThemeStorageTests(TestCase):
    def test_theme_exists_in_storage_handles_missing_key_error(self):
        class MissingKeyError(Exception):
            def __init__(self, message="NoSuchKey"):
                super().__init__(message)
                self.response = {"Error": {"Code": "NoSuchKey"}}

        class FakeStorage:
            def listdir(self, prefix):
                raise MissingKeyError("An error occurred (NoSuchKey) when calling the ListObjects operation: None")

        with mock.patch("core.themes.get_theme_storage", return_value=FakeStorage()):
            exists = theme_exists_in_storage("sample")

        self.assertFalse(exists)


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
            theme_dir = build_test_theme("sample", tmp_dir)

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

    def test_validate_theme_dir_uses_expected_slug_when_metadata_slug_missing(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            theme_dir = self._build_theme_dir(
                Path(tmp_dir),
                slug="tmp6j1c8bag",
                metadata={"label": "Sample", "version": "1.0"},
            )

            result = validate_theme_dir(theme_dir, expected_slug="webstead-default-2026")

        self.assertTrue(result.is_valid)
        self.assertEqual(result.slug, "webstead-default-2026")

    def test_fixture_theme_is_valid(self):
        fixture_dir = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "themes" / "example"

        result = validate_theme_dir(fixture_dir)

        self.assertTrue(result.is_valid)


class ThemeTestHelperTests(TestCase):
    def test_build_test_theme_creates_minimal_structure(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            theme_dir = build_test_theme(
                "sample",
                tmp_dir,
                extra_files=[("templates/post.html", "<article></article>")],
            )

            result = validate_theme_dir(theme_dir)
            metadata = json.loads((theme_dir / "theme.json").read_text())

            self.assertTrue(result.is_valid)
            self.assertEqual(metadata["slug"], "sample")
            self.assertTrue((theme_dir / "templates" / "post.html").exists())


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

    def _git_env(self):
        env = os.environ.copy()
        env.update(
            {
                "GIT_AUTHOR_NAME": "Test",
                "GIT_AUTHOR_EMAIL": "test@example.com",
                "GIT_COMMITTER_NAME": "Test",
                "GIT_COMMITTER_EMAIL": "test@example.com",
            }
        )
        return env

    def _git(self, repo_dir: Path, *args: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(repo_dir), *args],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=self._git_env(),
        )
        return result.stdout.strip()

    def _init_theme_repo(self, repo_dir: Path, slug: str, *, version: str) -> Path:
        theme_dir = build_test_theme(slug, repo_dir, metadata={"version": version})
        self._git(repo_dir, "init")
        self._git(repo_dir, "add", ".")
        self._git(repo_dir, "commit", "-m", f"init {version}")
        return theme_dir

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

    def test_install_theme_from_git_creates_install_record(self):
        with tempfile.TemporaryDirectory() as storage_root, tempfile.TemporaryDirectory() as themes_root:
            storage = FileSystemStorage(location=storage_root)
            with tempfile.TemporaryDirectory() as repo_root:
                repo_dir = Path(repo_root)
                self._init_theme_repo(repo_dir, "sample", version="1.0")
                expected_commit = self._git(repo_dir, "rev-parse", "HEAD")

                with override_settings(THEMES_ROOT=themes_root, THEME_STORAGE_PREFIX="themes"):
                    with mock.patch("core.themes.get_theme_storage", return_value=storage):
                        theme = install_theme_from_git(str(repo_dir), "sample")

        record = ThemeInstall.objects.get(slug="sample")

        self.assertEqual(record.source_type, ThemeInstall.SOURCE_GIT)
        self.assertEqual(record.source_url, str(repo_dir))
        self.assertEqual(record.version, "1.0")
        self.assertEqual(record.last_synced_commit, expected_commit)
        self.assertEqual(record.last_sync_status, ThemeInstall.STATUS_SUCCESS)
        self.assertIsNotNone(record.last_synced_at)
        self.assertEqual(theme.slug, record.slug)

    def test_install_theme_from_git_checks_out_ref(self):
        with tempfile.TemporaryDirectory() as storage_root, tempfile.TemporaryDirectory() as themes_root:
            storage = FileSystemStorage(location=storage_root)
            with tempfile.TemporaryDirectory() as repo_root:
                repo_dir = Path(repo_root)
                theme_dir = self._init_theme_repo(repo_dir, "sample", version="1.0")
                base_meta = json.loads((theme_dir / "theme.json").read_text())
                base_meta["version"] = "2.0"
                (theme_dir / "theme.json").write_text(json.dumps(base_meta))
                self._git(repo_dir, "add", "sample/theme.json")
                self._git(repo_dir, "commit", "-m", "bump version")
                first_commit = self._git(repo_dir, "rev-parse", "HEAD~1")

                with override_settings(THEMES_ROOT=themes_root, THEME_STORAGE_PREFIX="themes"):
                    with mock.patch("core.themes.get_theme_storage", return_value=storage):
                        theme = install_theme_from_git(str(repo_dir), "sample", ref=first_commit)

        self.assertEqual(theme.version, "1.0")

    def test_install_theme_from_git_rejects_invalid_repo(self):
        with tempfile.TemporaryDirectory() as storage_root, tempfile.TemporaryDirectory() as themes_root:
            storage = FileSystemStorage(location=storage_root)
            with tempfile.TemporaryDirectory() as repo_root:
                repo_dir = Path(repo_root)
                repo_dir.mkdir(parents=True, exist_ok=True)
                self._git(repo_dir, "init")
                (repo_dir / "README.md").write_text("no theme here")
                self._git(repo_dir, "add", "README.md")
                self._git(repo_dir, "commit", "-m", "docs")

                with override_settings(THEMES_ROOT=themes_root, THEME_STORAGE_PREFIX="themes"):
                    with mock.patch("core.themes.get_theme_storage", return_value=storage):
                        with self.assertRaises(ThemeUploadError):
                            install_theme_from_git(str(repo_dir), "sample")

        self.assertFalse((Path(themes_root) / "sample").exists())
        self.assertFalse(ThemeInstall.objects.filter(slug="sample").exists())

    def test_update_theme_from_git_updates_commit(self):
        with tempfile.TemporaryDirectory() as storage_root, tempfile.TemporaryDirectory() as themes_root:
            storage = FileSystemStorage(location=storage_root)
            with tempfile.TemporaryDirectory() as repo_root:
                repo_dir = Path(repo_root)
                theme_dir = self._init_theme_repo(repo_dir, "sample", version="1.0")
                first_commit = self._git(repo_dir, "rev-parse", "HEAD")
                default_branch = self._git(repo_dir, "symbolic-ref", "--short", "HEAD")
                self._git(repo_dir, "tag", "v1")
                meta = json.loads((theme_dir / "theme.json").read_text())
                meta["version"] = "2.0"
                (theme_dir / "theme.json").write_text(json.dumps(meta))
                self._git(repo_dir, "add", "sample/theme.json")
                self._git(repo_dir, "commit", "-m", "bump version")
                second_commit = self._git(repo_dir, "rev-parse", "HEAD")

                with override_settings(THEMES_ROOT=themes_root, THEME_STORAGE_PREFIX="themes"):
                    with mock.patch("core.themes.get_theme_storage", return_value=storage):
                        install_theme_from_git(str(repo_dir), "sample", ref="v1")
                        install = ThemeInstall.objects.get(slug="sample")
                        self.assertEqual(install.last_synced_commit, first_commit)
                        result = update_theme_from_git(install, ref=default_branch)
                        record = ThemeInstall.objects.get(slug="sample")
                        theme_meta = json.loads(
                            (Path(themes_root) / "sample" / "theme.json").read_text()
                        )

                        self.assertTrue(result.updated)
                        self.assertEqual(record.last_synced_commit, second_commit)
                        self.assertEqual(theme_meta["version"], "2.0")

    def test_theme_update_command_updates_git_theme(self):
        with tempfile.TemporaryDirectory() as storage_root, tempfile.TemporaryDirectory() as themes_root:
            storage = FileSystemStorage(location=storage_root)
            with tempfile.TemporaryDirectory() as repo_root:
                repo_dir = Path(repo_root)
                theme_dir = self._init_theme_repo(repo_dir, "sample", version="1.0")
                first_commit = self._git(repo_dir, "rev-parse", "HEAD")
                default_branch = self._git(repo_dir, "symbolic-ref", "--short", "HEAD")
                self._git(repo_dir, "tag", "v1")
                meta = json.loads((theme_dir / "theme.json").read_text())
                meta["version"] = "2.0"
                (theme_dir / "theme.json").write_text(json.dumps(meta))
                self._git(repo_dir, "add", "sample/theme.json")
                self._git(repo_dir, "commit", "-m", "bump version")
                second_commit = self._git(repo_dir, "rev-parse", "HEAD")

                with override_settings(THEMES_ROOT=themes_root, THEME_STORAGE_PREFIX="themes"):
                    with mock.patch("core.themes.get_theme_storage", return_value=storage):
                        install_theme_from_git(str(repo_dir), "sample", ref="v1")
                        install = ThemeInstall.objects.get(slug="sample")
                        self.assertEqual(install.last_synced_commit, first_commit)
                        out = io.StringIO()
                        call_command(
                            "theme_update",
                            "--slug",
                            "sample",
                            "--ref",
                            default_branch,
                            stdout=out,
                        )
                        install.refresh_from_db()

                        self.assertEqual(install.last_synced_commit, second_commit)
                        self.assertIn("Updated:", out.getvalue())

    def test_expected_slugs_returns_sorted_list(self):
        ThemeInstall.objects.create(slug="beta", source_type=ThemeInstall.SOURCE_UPLOAD)
        ThemeInstall.objects.create(slug="alpha", source_type=ThemeInstall.SOURCE_UPLOAD)

        self.assertEqual(ThemeInstall.objects.expected_slugs(), ["alpha", "beta"])


class ThemeStaticTemplateTagTests(TestCase):
    def test_theme_static_uses_context_active_theme(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            build_test_theme("sample", tmp_dir)
            with override_settings(THEMES_ROOT=tmp_dir):
                theme = get_theme("sample", base_dir=tmp_dir)
                template = Template("{% load theme %}{% theme_static 'css/theme.css' %}")

                rendered = template.render(Context({"active_theme": theme}))

        self.assertEqual(rendered, static("themes/sample/static/css/theme.css"))

    def test_theme_static_falls_back_to_site_configuration(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            build_test_theme("sample", tmp_dir)
            with override_settings(THEMES_ROOT=tmp_dir):
                settings = SiteConfiguration.get_solo()
                settings.active_theme = "sample"
                settings.save()
                template = Template("{% load theme %}{% theme_static 'css/theme.css' %}")

                rendered = template.render(Context({}))

        self.assertEqual(rendered, static("themes/sample/static/css/theme.css"))
