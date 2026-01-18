import json
import tempfile
from pathlib import Path
from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib.messages import get_messages
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.test.utils import override_settings
from django.urls import reverse
from django.utils import timezone

from blog.models import Comment, Post
from core.models import HCard, HCardPhoto, SiteConfiguration, ThemeInstall
from core.themes import ThemeDefinition, ThemeUpdateResult
from core.test_utils import build_test_theme
from files.models import Attachment, File
from micropub.models import Webmention


class SiteAdminAccessTests(TestCase):
    def setUp(self):
        super().setUp()
        self.user = get_user_model().objects.create_user(
            username="reader",
            email="reader@example.com",
            password="password",
        )
        self.staff = get_user_model().objects.create_user(
            username="editor",
            email="editor@example.com",
            password="password",
            is_staff=True,
        )

    def test_admin_bar_requires_staff(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse("site_admin:admin_bar"))

        self.assertEqual(response.status_code, 403)

        self.client.force_login(self.staff)
        response = self.client.get(reverse("site_admin:admin_bar"))

        self.assertEqual(response.status_code, 200)

    def test_dashboard_requires_staff(self):
        response = self.client.get(reverse("site_admin:dashboard"))

        self.assertRedirects(
            response,
            f"{reverse('site_admin:login')}?next={reverse('site_admin:dashboard')}",
        )

        self.client.force_login(self.staff)
        response = self.client.get(reverse("site_admin:dashboard"))

        self.assertEqual(response.status_code, 200)

    def test_admin_bar_hides_theme_toggle_on_site(self):
        self.client.force_login(self.staff)
        response = self.client.get(reverse("site_admin:admin_bar"))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "data-admin-theme-toggle")
        self.assertNotContains(response, "Dark mode")
        self.assertNotContains(response, "Light mode")


class SiteAdminPageTests(TestCase):
    def setUp(self):
        super().setUp()
        self.staff = get_user_model().objects.create_user(
            username="editor",
            email="editor@example.com",
            password="password",
            is_staff=True,
        )

    def test_page_create_htmx_redirects_to_edit(self):
        self.client.force_login(self.staff)
        published_on = timezone.localtime(timezone.now()).strftime("%Y-%m-%dT%H:%M")
        response = self.client.post(
            reverse("site_admin:page_create"),
            {
                "title": "About",
                "content": "Hello world",
                "published_on": published_on,
            },
            HTTP_HX_REQUEST="true",
        )

        self.assertEqual(response.status_code, 204)
        self.assertIn("/admin/pages/", response["HX-Redirect"])

        page = self.staff.page_set.first()
        self.assertIsNotNone(page)
        self.assertEqual(page.author, self.staff)


class SiteAdminPostTests(TestCase):
    def setUp(self):
        super().setUp()
        self.staff = get_user_model().objects.create_user(
            username="editor",
            email="editor@example.com",
            password="password",
            is_staff=True,
        )

    def test_photo_post_requires_caption_or_photo(self):
        self.client.force_login(self.staff)
        response = self.client.post(
            reverse("site_admin:post_create"),
            {
                "kind": Post.PHOTO,
                "content": "",
                "title": "",
                "slug": "",
            },
        )

        self.assertEqual(response.status_code, 200)
        form = response.context["form"]
        self.assertIn(
            "Add a caption or at least one photo for photo posts.",
            form.non_field_errors(),
        )
        self.assertEqual(Post.objects.count(), 0)

    def test_like_post_auto_fills_content(self):
        self.client.force_login(self.staff)
        response = self.client.post(
            reverse("site_admin:post_create"),
            {
                "kind": Post.LIKE,
                "like_of": "https://example.com",
                "content": "",
                "title": "",
                "slug": "",
            },
        )

        self.assertEqual(response.status_code, 302)
        post = Post.objects.get()
        self.assertEqual(post.content, "Liked https://example.com")
        self.assertIsNotNone(post.published_on)


class SiteAdminProfilePhotoTests(TestCase):
    def setUp(self):
        super().setUp()
        self.staff = get_user_model().objects.create_user(
            username="editor",
            email="editor@example.com",
            password="password",
            is_staff=True,
        )

    def test_profile_upload_and_delete_photo(self):
        self.client.force_login(self.staff)
        with tempfile.TemporaryDirectory() as media_root:
            with override_settings(MEDIA_ROOT=media_root):
                upload = SimpleUploadedFile(
                    "profile.jpg",
                    b"fake-image-data",
                    content_type="image/jpeg",
                )
                response = self.client.post(
                    reverse("site_admin:profile_upload_photo"),
                    {"photo": upload},
                )

                self.assertEqual(response.status_code, 200)
                payload = json.loads(response.content.decode())
                asset_id = payload["id"]

                self.assertTrue(File.objects.filter(id=asset_id).exists())

                response = self.client.post(
                    reverse("site_admin:profile_delete_photo"),
                    {"id": asset_id},
                )

                self.assertEqual(response.status_code, 200)
                payload = json.loads(response.content.decode())
                self.assertEqual(payload["status"], "deleted")
                self.assertFalse(File.objects.filter(id=asset_id).exists())

    def test_profile_delete_photo_blocks_in_use_asset(self):
        self.client.force_login(self.staff)
        with tempfile.TemporaryDirectory() as media_root:
            with override_settings(MEDIA_ROOT=media_root):
                upload = SimpleUploadedFile(
                    "profile.jpg",
                    b"fake-image-data",
                    content_type="image/jpeg",
                )
                asset = File.objects.create(
                    kind=File.IMAGE,
                    file=upload,
                    owner=self.staff,
                )
                hcard = HCard.objects.create(user=self.staff, name="Editor")
                HCardPhoto.objects.create(
                    hcard=hcard,
                    asset=asset,
                    value=asset.file.url,
                    sort_order=0,
                )

                response = self.client.post(
                    reverse("site_admin:profile_delete_photo"),
                    {"id": asset.id},
                )

                self.assertEqual(response.status_code, 409)
                payload = json.loads(response.content.decode())
                self.assertEqual(
                    payload["error"],
                    "File is still used in a profile photo.",
                )
                self.assertTrue(File.objects.filter(id=asset.id).exists())


class SiteAdminFileDeleteTests(TestCase):
    def setUp(self):
        super().setUp()
        self.staff = get_user_model().objects.create_user(
            username="editor",
            email="editor@example.com",
            password="password",
            is_staff=True,
        )

    def test_file_delete_removes_asset(self):
        self.client.force_login(self.staff)
        with tempfile.TemporaryDirectory() as media_root:
            with override_settings(MEDIA_ROOT=media_root):
                upload = SimpleUploadedFile(
                    "asset.jpg",
                    b"fake-image-data",
                    content_type="image/jpeg",
                )
                asset = File.objects.create(
                    kind=File.IMAGE,
                    file=upload,
                    owner=self.staff,
                )

                response = self.client.post(
                    reverse("site_admin:file_delete", kwargs={"file_id": asset.id})
                )

                self.assertRedirects(response, reverse("site_admin:file_list"))
                self.assertFalse(File.objects.filter(id=asset.id).exists())

    def test_file_delete_blocks_in_use_asset(self):
        self.client.force_login(self.staff)
        with tempfile.TemporaryDirectory() as media_root:
            with override_settings(MEDIA_ROOT=media_root):
                upload = SimpleUploadedFile(
                    "asset.jpg",
                    b"fake-image-data",
                    content_type="image/jpeg",
                )
                asset = File.objects.create(
                    kind=File.IMAGE,
                    file=upload,
                    owner=self.staff,
                )
                post = Post.objects.create(
                    title="Post A",
                    slug="post-a",
                    kind=Post.ARTICLE,
                    content="Hello A",
                    published_on=timezone.now(),
                )
                Attachment.objects.create(
                    content_object=post,
                    asset=asset,
                    role="photo",
                )

                response = self.client.post(
                    reverse("site_admin:file_delete", kwargs={"file_id": asset.id})
                )

                self.assertContains(
                    response,
                    "File is still attached to content.",
                    status_code=409,
                )
                self.assertContains(response, post.title, status_code=409)
                self.assertTrue(File.objects.filter(id=asset.id).exists())

    def test_delete_post_photo_blocks_in_use_asset(self):
        self.client.force_login(self.staff)
        with tempfile.TemporaryDirectory() as media_root:
            with override_settings(MEDIA_ROOT=media_root):
                upload = SimpleUploadedFile(
                    "post.jpg",
                    b"fake-image-data",
                    content_type="image/jpeg",
                )
                asset = File.objects.create(
                    kind=File.IMAGE,
                    file=upload,
                    owner=self.staff,
                )
                post = Post.objects.create(
                    title="Post",
                    slug="post",
                    kind=Post.ARTICLE,
                    content="Hello",
                    published_on=timezone.now(),
                )
                Attachment.objects.create(
                    content_object=post,
                    asset=asset,
                    role="photo",
                )

                response = self.client.post(
                    reverse("site_admin:post_delete_photo"),
                    {"id": asset.id},
                )

                self.assertEqual(response.status_code, 409)
                payload = json.loads(response.content.decode())
                self.assertEqual(
                    payload["error"],
                    "File is still attached to content.",
                )
                self.assertTrue(File.objects.filter(id=asset.id).exists())

    def test_post_edit_removes_attachment_without_deleting_shared_file(self):
        self.client.force_login(self.staff)
        with tempfile.TemporaryDirectory() as media_root:
            with override_settings(MEDIA_ROOT=media_root):
                upload = SimpleUploadedFile(
                    "post.jpg",
                    b"fake-image-data",
                    content_type="image/jpeg",
                )
                asset = File.objects.create(
                    kind=File.IMAGE,
                    file=upload,
                    owner=self.staff,
                )
                post_a = Post.objects.create(
                    title="Post A",
                    slug="post-a",
                    kind=Post.ARTICLE,
                    content="Hello A",
                    published_on=timezone.now(),
                )
                post_b = Post.objects.create(
                    title="Post B",
                    slug="post-b",
                    kind=Post.ARTICLE,
                    content="Hello B",
                    published_on=timezone.now(),
                )
                Attachment.objects.create(
                    content_object=post_a,
                    asset=asset,
                    role="photo",
                )
                Attachment.objects.create(
                    content_object=post_b,
                    asset=asset,
                    role="photo",
                )

                response = self.client.post(
                    reverse("site_admin:post_edit", kwargs={"slug": post_a.slug}),
                    {
                        "title": post_a.title,
                        "slug": post_a.slug,
                        "kind": post_a.kind,
                        "content": post_a.content,
                        "published_on": timezone.now().strftime("%Y-%m-%dT%H:%M"),
                        "existing_remove_ids": [str(asset.id)],
                    },
                )

                self.assertEqual(response.status_code, 302)
                self.assertTrue(File.objects.filter(id=asset.id).exists())
                self.assertEqual(
                    Attachment.objects.filter(asset=asset).count(),
                    1,
                )


class SiteAdminThemeFileTests(TestCase):
    def setUp(self):
        super().setUp()
        self.staff = get_user_model().objects.create_user(
            username="editor",
            email="editor@example.com",
            password="password",
            is_staff=True,
        )

    def test_theme_file_edit_rejects_disallowed_extension(self):
        self.client.force_login(self.staff)
        with tempfile.TemporaryDirectory() as themes_root:
            with override_settings(THEMES_ROOT=themes_root):
                build_test_theme("demo", themes_root)
                response = self.client.post(
                    reverse("site_admin:theme_file_edit", kwargs={"slug": "demo"}),
                    {
                        "theme": "demo",
                        "path": "templates/base.html",
                        "content": "test",
                        "new_entry_name": "bad.exe",
                        "new_file": "1",
                    },
                )

                self.assertEqual(response.status_code, 302)
                self.assertFalse(
                    (Path(themes_root) / "demo" / "templates" / "bad.exe").exists()
                )


class SiteAdminThemeInstallTests(TestCase):
    def setUp(self):
        super().setUp()
        self.staff = get_user_model().objects.create_user(
            username="theme-admin",
            email="theme-admin@example.com",
            password="password",
            is_staff=True,
        )

    def test_theme_install_from_git_validates_form(self):
        self.client.force_login(self.staff)
        response = self.client.post(
            reverse("site_admin:theme_settings"),
            {
                "action": "install_git",
                "git_url": "not-a-url",
                "slug": "demo",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["git_form"].errors)

    def test_theme_install_from_git_invokes_helper(self):
        self.client.force_login(self.staff)
        theme = ThemeDefinition(slug="demo", path=Path("/tmp/demo"), label="Demo")
        with mock.patch("site_admin.views.install_theme_from_git", return_value=theme) as install:
            response = self.client.post(
                reverse("site_admin:theme_settings"),
                {
                    "action": "install_git",
                    "git_url": "https://example.com/demo.git",
                    "ref": "main",
                    "slug": "demo",
                },
            )

        install.assert_called_once_with("https://example.com/demo.git", "demo", ref="main")
        self.assertEqual(response.status_code, 302)

    def test_theme_update_from_git_invokes_helper(self):
        self.client.force_login(self.staff)
        install = ThemeInstall.objects.create(
            slug="demo",
            source_type=ThemeInstall.SOURCE_GIT,
            source_url="https://example.com/demo.git",
            source_ref="main",
        )
        result = ThemeUpdateResult(slug="demo", ref="main", commit="abc123", updated=True)
        with mock.patch("site_admin.views.update_theme_from_git", return_value=result) as update:
            response = self.client.post(
                reverse("site_admin:theme_install_detail", kwargs={"slug": install.slug}),
                {"ref": "main"},
            )

        update.assert_called_once_with(install, ref="main")
        self.assertEqual(response.status_code, 302)

    def test_theme_settings_save_persists_values(self):
        self.client.force_login(self.staff)
        with tempfile.TemporaryDirectory() as themes_root:
            with override_settings(THEMES_ROOT=themes_root):
                metadata = {
                    "label": "Demo",
                    "slug": "demo",
                    "settings": {
                        "fields": {
                            "accent_color": {"type": "color", "default": "#111111"},
                            "show_banner": {"type": "boolean", "default": True},
                        }
                    },
                }
                build_test_theme("demo", themes_root, metadata=metadata)
                settings_obj = SiteConfiguration.get_solo()
                settings_obj.active_theme = "demo"
                settings_obj.save()

                response = self.client.post(
                    reverse("site_admin:theme_settings"),
                    {
                        "action": "save_theme_settings",
                        "accent_color": "#222222",
                        "show_banner": "on",
                    },
                )

        self.assertEqual(response.status_code, 302)
        settings_obj.refresh_from_db()
        self.assertEqual(
            settings_obj.theme_settings.get("demo"),
            {"accent_color": "#222222", "show_banner": True},
        )

    def test_theme_storage_healthcheck_defaults_read_only(self):
        self.client.force_login(self.staff)
        result = {
            "ok": True,
            "read_ok": True,
            "write_ok": False,
            "write_test": False,
            "errors": [],
        }
        with mock.patch("site_admin.views.theme_storage_healthcheck", return_value=result) as healthcheck:
            response = self.client.post(
                reverse("site_admin:theme_settings"),
                {"action": "theme_storage_healthcheck"},
            )

        healthcheck.assert_called_once_with(write_test=False)
        self.assertEqual(response.status_code, 302)
        messages = [message.message for message in get_messages(response.wsgi_request)]
        self.assertTrue(any("healthcheck" in message.lower() for message in messages))


class SiteAdminWebmentionModerationTests(TestCase):
    def setUp(self):
        super().setUp()
        self.staff = get_user_model().objects.create_user(
            username="editor",
            email="editor@example.com",
            password="password",
            is_staff=True,
        )
        self.client.force_login(self.staff)

    def test_approve_pending_webmention(self):
        mention = Webmention.objects.create(
            source="https://source.example",
            target="https://testserver/blog/post/hello/",
            status=Webmention.PENDING,
        )
        response = self.client.post(
            reverse("site_admin:webmention_approve", kwargs={"mention_id": mention.id})
        )

        self.assertEqual(response.status_code, 302)
        mention.refresh_from_db()
        self.assertEqual(mention.status, Webmention.ACCEPTED)

    def test_reject_pending_webmention(self):
        mention = Webmention.objects.create(
            source="https://source.example",
            target="https://testserver/blog/post/hello/",
            status=Webmention.PENDING,
        )
        response = self.client.post(
            reverse("site_admin:webmention_reject", kwargs={"mention_id": mention.id})
        )

        self.assertEqual(response.status_code, 302)
        mention.refresh_from_db()
        self.assertEqual(mention.status, Webmention.REJECTED)

    def test_pending_outgoing_webmention_cannot_be_moderated(self):
        mention = Webmention.objects.create(
            source="https://testserver/blog/post/hello/",
            target="https://external.example/post/1/",
            status=Webmention.PENDING,
        )
        response = self.client.get(
            reverse("site_admin:webmention_detail", kwargs={"mention_id": mention.id})
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["can_moderate"])


class SiteAdminCommentModerationTests(TestCase):
    def setUp(self):
        super().setUp()
        self.staff = get_user_model().objects.create_user(
            username="editor",
            email="editor@example.com",
            password="password",
            is_staff=True,
        )
        self.post = Post.objects.create(
            title="Comment Post",
            slug="comment-post",
            content="text",
            published_on=timezone.now(),
        )

    def test_comment_approve_marks_status(self):
        self.client.force_login(self.staff)
        comment = Comment.objects.create(
            post=self.post,
            author_name="Ada",
            content="Hello",
            status=Comment.PENDING,
        )

        with mock.patch("site_admin.views.submit_ham") as submit_ham:
            response = self.client.post(
                reverse("site_admin:comment_approve", kwargs={"comment_id": comment.id})
            )

        self.assertEqual(response.status_code, 302)
        comment.refresh_from_db()
        self.assertEqual(comment.status, Comment.APPROVED)
        submit_ham.assert_called_once()

    def test_comment_mark_spam_updates_status(self):
        self.client.force_login(self.staff)
        comment = Comment.objects.create(
            post=self.post,
            author_name="Ada",
            content="Hello",
            status=Comment.PENDING,
        )

        with mock.patch("site_admin.views.submit_spam") as submit_spam:
            response = self.client.post(
                reverse("site_admin:comment_mark_spam", kwargs={"comment_id": comment.id})
            )

        self.assertEqual(response.status_code, 302)
        comment.refresh_from_db()
        self.assertEqual(comment.status, Comment.SPAM)
        submit_spam.assert_called_once()
