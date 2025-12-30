import json
import tempfile
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.test.utils import override_settings
from django.urls import reverse
from django.utils import timezone

from blog.models import Post
from core.models import HCard, HCardPhoto
from core.test_utils import build_test_theme
from files.models import Attachment, File


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
