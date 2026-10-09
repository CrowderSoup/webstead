import json
import tempfile
import urllib.error
from urllib.parse import parse_qs, urlparse
from unittest.mock import patch, MagicMock
from types import SimpleNamespace

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.test.utils import override_settings
from django.urls import reverse

from blog.models import Post, Tag
from core.models import RequestErrorLog, SiteConfiguration
from files.models import Attachment, File
from micropub.models import Webmention
from micropub.webmention import (
    send_bridgy_publish_webmentions,
    send_webmentions_for_post,
    send_webmention,
    resend_webmention,
    verify_webmention_source,
    _normalize_url_for_compare,
)


MICROPUB_URL = "/micropub"


class MicropubViewTests(TestCase):
    def test_conflicting_tokens_returns_400(self):
        response = self.client.post(
            MICROPUB_URL,
            data={"access_token": "body-token", "content": "hi"},
            HTTP_AUTHORIZATION="Bearer header-token",
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), {"error": "invalid_request"})
        self.assertEqual(RequestErrorLog.objects.count(), 1)
        log_entry = RequestErrorLog.objects.first()
        self.assertEqual(log_entry.source, RequestErrorLog.SOURCE_MICROPUB)
        self.assertEqual(log_entry.status_code, 400)
        self.assertEqual(log_entry.error, "invalid_request")
        self.assertEqual(log_entry.path, MICROPUB_URL)

    @patch("micropub.views._authorized", return_value=(True, ["create"]))
    def test_matching_tokens_in_header_and_body_allowed(self, _authorized):
        response = self.client.post(
            MICROPUB_URL,
            data={"access_token": "token", "content": "Hello world"},
            HTTP_AUTHORIZATION="Bearer token",
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(Post.objects.count(), 1)

    @patch("micropub.views._authorized", return_value=(True, []))
    def test_create_requires_scope(self, _authorized):
        response = self.client.post(
            MICROPUB_URL,
            data={"content": "Hello world"},
            HTTP_AUTHORIZATION="Bearer token",
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json(), {"error": "insufficient_scope"})

    @patch("micropub.views._authorized", return_value=(True, ["create"]))
    def test_create_with_scope_persists_post(self, _authorized):
        response = self.client.post(
            MICROPUB_URL,
            data={"content": "Hello world"},
            HTTP_AUTHORIZATION="Bearer token",
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(Post.objects.count(), 1)
        post = Post.objects.first()
        self.assertEqual(post.content, "Hello world")

    @patch("micropub.views._authorized", return_value=(True, ["update"]))
    def test_update_replaces_content(self, _authorized):
        post = Post.objects.create(title="Old", slug="page-2", content="Old content")
        payload = {
            "action": "update",
            "url": "https://example.com/blog/post/page-2/",
            "replace": {"content": ["New content"]},
        }
        response = self.client.post(
            MICROPUB_URL,
            data=json.dumps(payload),
            content_type="application/json",
            HTTP_AUTHORIZATION="Bearer token",
        )
        self.assertEqual(response.status_code, 204)
        post.refresh_from_db()
        self.assertEqual(post.content, "New content")

    @patch("micropub.views._authorized", return_value=(True, ["delete"]))
    def test_delete_soft_deletes_post(self, _authorized):
        post = Post.objects.create(title="To delete", slug="page-3", content="hi")
        response = self.client.post(
            MICROPUB_URL,
            data={"action": "delete", "url": "https://example.com/blog/post/page-3/"},
            HTTP_AUTHORIZATION="Bearer token",
        )
        self.assertEqual(response.status_code, 204)
        post.refresh_from_db()
        self.assertTrue(post.deleted)

    @patch("micropub.views._authorized", return_value=(True, ["undelete"]))
    def test_undelete_clears_deleted_flag(self, _authorized):
        post = Post.objects.create(title="Deleted", slug="page-4", content="hi", deleted=True)
        response = self.client.post(
            MICROPUB_URL,
            data={"action": "undelete", "url": "https://example.com/blog/post/page-4/"},
            HTTP_AUTHORIZATION="Bearer token",
        )
        self.assertEqual(response.status_code, 204)
        post.refresh_from_db()
        self.assertFalse(post.deleted)

    @patch("micropub.views._authorized", return_value=(True, ["update"]))
    def test_add_and_delete_categories(self, _authorized):
        post = Post.objects.create(title="Tags", slug="page-5", content="hi")
        tag_existing = Tag.objects.create(tag="existing")
        post.tags.add(tag_existing)

        payload = {
            "action": "update",
            "url": "https://example.com/blog/post/page-5/",
            "add": {"category": ["added"]},
            "delete": {"category": ["existing"]},
        }
        response = self.client.post(
            MICROPUB_URL,
            data=json.dumps(payload),
            content_type="application/json",
            HTTP_AUTHORIZATION="Bearer token",
        )
        self.assertEqual(response.status_code, 204)
        tags = set(post.tags.values_list("tag", flat=True))
        self.assertIn("added", tags)
        self.assertNotIn("existing", tags)

    @patch("micropub.views._authorized", return_value=(True, ["update"]))
    def test_delete_category_leaves_other_posts_tagged(self, _authorized):
        post = Post.objects.create(title="Tags", slug="page-shared-tag", content="hi")
        other = Post.objects.create(title="Other", slug="page-other", content="hi")
        shared = Tag.objects.create(tag="shared")
        post.tags.add(shared)
        other.tags.add(shared)

        payload = {
            "action": "update",
            "url": "https://example.com/blog/post/page-shared-tag/",
            "delete": {"category": ["shared"]},
        }
        response = self.client.post(
            MICROPUB_URL,
            data=json.dumps(payload),
            content_type="application/json",
            HTTP_AUTHORIZATION="Bearer token",
        )
        self.assertEqual(response.status_code, 204)
        self.assertFalse(post.tags.filter(tag="shared").exists())
        self.assertTrue(other.tags.filter(tag="shared").exists())
        self.assertTrue(Tag.objects.filter(tag="shared").exists())

    @patch("micropub.views._authorized", return_value=(True, ["update"]))
    def test_update_replaces_name(self, _authorized):
        post = Post.objects.create(title="Old Name", slug="page-name", content="hi")
        original_slug = post.slug
        payload = {
            "action": "update",
            "url": "https://example.com/blog/post/page-name/",
            "replace": {"name": ["New Name"]},
        }
        response = self.client.post(
            MICROPUB_URL,
            data=json.dumps(payload),
            content_type="application/json",
            HTTP_AUTHORIZATION="Bearer token",
        )
        self.assertEqual(response.status_code, 204)
        post.refresh_from_db()
        self.assertEqual(post.title, "New Name")
        self.assertEqual(post.slug, original_slug)

    @patch("micropub.views._authorized", return_value=(True, ["update"]))
    def test_update_replaces_name_ignores_empty_string(self, _authorized):
        post = Post.objects.create(title="Keep Me", slug="page-name-empty", content="hi")
        payload = {
            "action": "update",
            "url": "https://example.com/blog/post/page-name-empty/",
            "replace": {"name": [""]},
        }
        response = self.client.post(
            MICROPUB_URL,
            data=json.dumps(payload),
            content_type="application/json",
            HTTP_AUTHORIZATION="Bearer token",
        )
        self.assertEqual(response.status_code, 204)
        post.refresh_from_db()
        self.assertEqual(post.title, "Keep Me")

    @patch("micropub.views._authorized", return_value=(True, ["update"]))
    def test_update_replaces_location_on_checkin_post(self, _authorized):
        post = Post.objects.create(
            title="Coffee Shop",
            slug="page-checkin",
            content="Checked in",
            kind=Post.CHECKIN,
        )
        payload = {
            "action": "update",
            "url": "https://example.com/blog/post/page-checkin/",
            "replace": {"location": ["geo:40.7,-74.0"]},
        }
        response = self.client.post(
            MICROPUB_URL,
            data=json.dumps(payload),
            content_type="application/json",
            HTTP_AUTHORIZATION="Bearer token",
        )
        self.assertEqual(response.status_code, 204)
        post.refresh_from_db()
        self.assertEqual(
            post.mf2.get("checkin"),
            {"latitude": 40.7, "longitude": -74.0, "name": "Coffee Shop"},
        )

    @patch("micropub.views._authorized", return_value=(True, ["update"]))
    def test_update_location_rejected_on_non_checkin_post(self, _authorized):
        post = Post.objects.create(title="Article", slug="page-not-checkin", content="hi")
        payload = {
            "action": "update",
            "url": "https://example.com/blog/post/page-not-checkin/",
            "replace": {"location": ["geo:40.7,-74.0"], "content": ["Should not persist"]},
        }
        response = self.client.post(
            MICROPUB_URL,
            data=json.dumps(payload),
            content_type="application/json",
            HTTP_AUTHORIZATION="Bearer token",
        )
        self.assertEqual(response.status_code, 400)
        post.refresh_from_db()
        self.assertNotIn("checkin", post.mf2)
        self.assertEqual(post.content, "hi")

    @patch("micropub.tasks.download_post_photo.delay")
    @patch("micropub.views._authorized", return_value=(True, ["update"]))
    def test_update_add_photo_via_url(self, _authorized, mock_delay):
        post = Post.objects.create(title="Photo Post", slug="page-add-photo", content="hi")
        payload = {
            "action": "update",
            "url": "https://example.com/blog/post/page-add-photo/",
            "add": {"photo": ["https://example.com/photo.jpg"]},
        }
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                MICROPUB_URL,
                data=json.dumps(payload),
                content_type="application/json",
                HTTP_AUTHORIZATION="Bearer token",
            )
        self.assertEqual(response.status_code, 204)
        mock_delay.assert_called_once_with(post.pk, "https://example.com/photo.jpg")

    @patch("micropub.tasks.download_post_photo.delay")
    @patch("micropub.views._authorized", return_value=(True, ["update"]))
    def test_update_add_photo_via_dict(self, _authorized, mock_delay):
        post = Post.objects.create(title="Photo Post", slug="page-add-photo-dict", content="hi")
        payload = {
            "action": "update",
            "url": "https://example.com/blog/post/page-add-photo-dict/",
            "add": {"photo": [{"value": "https://example.com/p.jpg", "alt": "desc"}]},
        }
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                MICROPUB_URL,
                data=json.dumps(payload),
                content_type="application/json",
                HTTP_AUTHORIZATION="Bearer token",
            )
        self.assertEqual(response.status_code, 204)
        mock_delay.assert_called_once_with(post.pk, "https://example.com/p.jpg", "desc")

    @patch("micropub.views._authorized", return_value=(True, ["update"]))
    def test_update_delete_photo_by_url(self, _authorized):
        with tempfile.TemporaryDirectory() as media_root:
            with override_settings(MEDIA_ROOT=media_root):
                post = Post.objects.create(title="Photo Post", slug="page-delete-photo", content="hi")
                upload = SimpleUploadedFile("photo.jpg", b"fake-image-data", content_type="image/jpeg")
                asset = File.objects.create(kind=File.IMAGE, file=upload)
                Attachment.objects.create(content_object=post, asset=asset, role="photo")
                asset_url = asset.file.url

                payload = {
                    "action": "update",
                    "url": "https://example.com/blog/post/page-delete-photo/",
                    "delete": {"photo": [asset_url]},
                }
                response = self.client.post(
                    MICROPUB_URL,
                    data=json.dumps(payload),
                    content_type="application/json",
                    HTTP_AUTHORIZATION="Bearer token",
                )
                self.assertEqual(response.status_code, 204)
                self.assertEqual(post.attachments.filter(asset__kind=File.IMAGE).count(), 0)
                # detached, not deleted, so reverting the post can restore it
                self.assertTrue(File.objects.filter(pk=asset.pk).exists())

    @patch("micropub.views._authorized", return_value=(True, ["update"]))
    def test_update_delete_photo_keeps_shared_asset(self, _authorized):
        with tempfile.TemporaryDirectory() as media_root:
            with override_settings(MEDIA_ROOT=media_root):
                post = Post.objects.create(title="Photo Post", slug="page-shared-photo", content="hi")
                other_post = Post.objects.create(title="Other Post", slug="page-shared-photo-2", content="hi")
                upload = SimpleUploadedFile("photo.jpg", b"fake-image-data", content_type="image/jpeg")
                asset = File.objects.create(kind=File.IMAGE, file=upload)
                Attachment.objects.create(content_object=post, asset=asset, role="photo")
                Attachment.objects.create(content_object=other_post, asset=asset, role="photo")
                asset_url = asset.file.url

                payload = {
                    "action": "update",
                    "url": "https://example.com/blog/post/page-shared-photo/",
                    "delete": {"photo": [asset_url]},
                }
                response = self.client.post(
                    MICROPUB_URL,
                    data=json.dumps(payload),
                    content_type="application/json",
                    HTTP_AUTHORIZATION="Bearer token",
                )
                self.assertEqual(response.status_code, 204)
                self.assertEqual(post.attachments.filter(asset__kind=File.IMAGE).count(), 0)
                self.assertTrue(File.objects.filter(pk=asset.pk).exists())
                self.assertEqual(other_post.attachments.filter(asset__kind=File.IMAGE).count(), 1)

    @patch("micropub.tasks.download_post_photo.delay")
    @patch("micropub.views._authorized", return_value=(True, ["update"]))
    def test_update_replace_photo_clears_and_readds(self, _authorized, mock_delay):
        with tempfile.TemporaryDirectory() as media_root:
            with override_settings(MEDIA_ROOT=media_root):
                post = Post.objects.create(title="Photo Post", slug="page-replace-photo", content="hi")
                upload = SimpleUploadedFile("photo.jpg", b"fake-image-data", content_type="image/jpeg")
                asset = File.objects.create(kind=File.IMAGE, file=upload)
                Attachment.objects.create(content_object=post, asset=asset, role="photo")

                payload = {
                    "action": "update",
                    "url": "https://example.com/blog/post/page-replace-photo/",
                    "replace": {"photo": ["https://example.com/new.jpg"]},
                }
                with self.captureOnCommitCallbacks(execute=True):
                    response = self.client.post(
                        MICROPUB_URL,
                        data=json.dumps(payload),
                        content_type="application/json",
                        HTTP_AUTHORIZATION="Bearer token",
                    )
                self.assertEqual(response.status_code, 204)
                self.assertEqual(post.attachments.filter(asset__kind=File.IMAGE).count(), 0)
                # detached, not deleted, so reverting the post can restore it
                self.assertTrue(File.objects.filter(pk=asset.pk).exists())
                mock_delay.assert_called_once_with(post.pk, "https://example.com/new.jpg")

    @patch("micropub.views._authorized", return_value=(True, ["update"]))
    def test_update_delete_all_photos_empty_list(self, _authorized):
        with tempfile.TemporaryDirectory() as media_root:
            with override_settings(MEDIA_ROOT=media_root):
                post = Post.objects.create(title="Photo Post", slug="page-delete-all-photos", content="hi")
                for name in ("photo1.jpg", "photo2.jpg"):
                    upload = SimpleUploadedFile(name, b"fake-image-data", content_type="image/jpeg")
                    asset = File.objects.create(kind=File.IMAGE, file=upload)
                    Attachment.objects.create(content_object=post, asset=asset, role="photo")

                payload = {
                    "action": "update",
                    "url": "https://example.com/blog/post/page-delete-all-photos/",
                    "delete": ["photo"],
                }
                response = self.client.post(
                    MICROPUB_URL,
                    data=json.dumps(payload),
                    content_type="application/json",
                    HTTP_AUTHORIZATION="Bearer token",
                )
                self.assertEqual(response.status_code, 204)
                self.assertEqual(post.attachments.filter(asset__kind=File.IMAGE).count(), 0)

    @patch("micropub.views._authorized", return_value=(True, ["read"]))
    def test_source_query_returns_properties(self, _authorized):
        post = Post.objects.create(
            title="Title",
            slug="page-6",
            content="Body",
            published_on=None,
        )
        post.tags.add(Tag.objects.create(tag="tag1"))
        response = self.client.get(
            MICROPUB_URL,
            {"q": "source", "url": "https://example.com/blog/post/page-6/"},
            HTTP_AUTHORIZATION="Bearer token",
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        props = body.get("properties", {})
        self.assertEqual(props.get("content"), ["Body"])
        self.assertIn("tag1", props.get("category", []))

    @patch("micropub.views._authorized", return_value=(True, []))
    def test_syndicate_to_returns_enabled_bridgy_targets(self, _authorized):
        settings_obj = SiteConfiguration.get_solo()
        settings_obj.bridgy_publish_bluesky = False
        settings_obj.bridgy_publish_flickr = True
        settings_obj.bridgy_publish_github = False
        settings_obj.save()

        response = self.client.get(
            MICROPUB_URL,
            data={"q": "syndicate-to"},
            HTTP_AUTHORIZATION="Bearer token",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "syndicate-to": [
                    {"uid": "https://brid.gy/publish/flickr", "name": "Bridgy Publish: Flickr"},
                ]
            },
        )

    @patch("micropub.views._authorized", return_value=(True, []))
    def test_config_includes_syndicate_targets(self, _authorized):
        settings_obj = SiteConfiguration.get_solo()
        settings_obj.bridgy_publish_bluesky = True
        settings_obj.bridgy_publish_flickr = False
        settings_obj.bridgy_publish_github = False
        settings_obj.save()

        response = self.client.get(
            MICROPUB_URL,
            data={"q": "config"},
            HTTP_AUTHORIZATION="Bearer token",
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(
            body["syndicate-to"],
            [{"uid": "https://brid.gy/publish/bluesky", "name": "Bridgy Publish: Bluesky"}],
        )


class IndieAuthLoginTests(TestCase):
    def setUp(self):
        super().setUp()
        self.login_url = reverse("indieauth-login")
        self.callback_url = reverse("indieauth-callback")

    @patch("micropub.views._discover_indieauth_endpoints", return_value=("https://auth.example/authorize", None))
    def test_login_start_redirects_to_endpoint(self, _discover):
        response = self.client.get(
            self.login_url,
            data={"me": "https://example.com", "next": "/blog/post/hello/"},
        )

        self.assertEqual(response.status_code, 302)
        location = response["Location"]
        parsed = urlparse(location)
        self.assertEqual(parsed.netloc, "auth.example")
        params = parse_qs(parsed.query)
        self.assertEqual(params["me"][0], "https://example.com/")
        self.assertEqual(params["response_type"][0], "code")
        self.assertEqual(params["client_id"][0], "http://testserver/")
        self.assertEqual(params["redirect_uri"][0], "http://testserver/indieauth/callback")
        self.assertEqual(params["state"][0], self.client.session.get("indieauth_state"))

    @patch("micropub.views.urlopen")
    def test_callback_stores_session_on_success(self, mocked_urlopen):
        class DummyResponse:
            def __init__(self, body):
                self._body = body
                self.headers = {"Content-Type": "application/json"}

            def read(self):
                return self._body

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

        mocked_urlopen.return_value = DummyResponse(json.dumps({"me": "https://example.com/"}).encode("utf-8"))
        session = self.client.session
        session["indieauth_state"] = "state123"
        session["indieauth_pending_me"] = "https://example.com/"
        session["indieauth_next"] = "/blog/post/hello/"
        session["indieauth_token_endpoint"] = "https://tokens.example/token"
        session.save()

        response = self.client.get(
            self.callback_url,
            data={"code": "code123", "state": "state123", "me": "https://example.com/"},
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/blog/post/hello/")
        self.assertEqual(self.client.session.get("indieauth_me"), "https://example.com/")

    @patch("micropub.views.urlopen")
    def test_callback_logs_and_ignores_invalid_response(self, mocked_urlopen):
        class DummyResponse:
            def __init__(self, body):
                self._body = body
                self.headers = {"Content-Type": "application/json"}

            def read(self):
                return self._body

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

        mocked_urlopen.return_value = DummyResponse(json.dumps({"me": "https://wrong.example/"}).encode("utf-8"))
        session = self.client.session
        session["indieauth_state"] = "state456"
        session["indieauth_pending_me"] = "https://example.com/"
        session["indieauth_next"] = "/blog/post/hello/"
        session["indieauth_token_endpoint"] = "https://tokens.example/token"
        session.save()

        with self.assertLogs("micropub.views", level="INFO"):
            response = self.client.get(
                self.callback_url,
                data={"code": "code456", "state": "state456", "me": "https://example.com/"},
            )

        self.assertEqual(response.status_code, 302)
        self.assertIsNone(self.client.session.get("indieauth_me"))


@override_settings(ALLOWED_HOSTS=["testserver"])
class WebmentionViewTests(TestCase):
    def setUp(self):
        super().setUp()
        self.post = Post.objects.create(title="Hello", slug="hello", content="Hello world")
        self.endpoint = reverse("webmention-endpoint")

    def test_rejects_target_outside_site(self):
        response = self.client.post(
            self.endpoint,
            data={"source": "https://source.example", "target": "https://example.com/blog/post/hello/"},
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(Webmention.objects.count(), 0)

    @override_settings(WEBMENTION_TRUSTED_DOMAINS=[])
    @patch("micropub.webmention.verify_webmention_source", return_value=(True, "", False))
    def test_verified_webmention_is_pending_by_default(self, _verify):
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                self.endpoint,
                data={"source": "https://source.example", "target": "http://testserver/blog/post/hello/"},
            )

        self.assertEqual(response.status_code, 202)
        mention = Webmention.objects.get()
        self.assertEqual(mention.status, Webmention.PENDING)

    @override_settings(WEBMENTION_TRUSTED_DOMAINS=["trusted.example"])
    @patch("micropub.webmention.verify_webmention_source", return_value=(True, "", False))
    def test_trusted_domain_auto_approves(self, _verify):
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                self.endpoint,
                data={"source": "https://trusted.example/post", "target": "http://testserver/blog/post/hello/"},
            )

        self.assertEqual(response.status_code, 202)
        mention = Webmention.objects.get()
        self.assertEqual(mention.status, Webmention.ACCEPTED)

    @patch("micropub.webmention.verify_webmention_source", return_value=(False, "No link found", False))
    def test_missing_link_rejects(self, _verify):
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                self.endpoint,
                data={"source": "https://source.example", "target": "http://testserver/blog/post/hello/"},
            )

        self.assertEqual(response.status_code, 202)
        mention = Webmention.objects.get()
        self.assertEqual(mention.status, Webmention.REJECTED)

    def test_fetch_failures_stay_pending(self):
        # The view always creates PENDING immediately; transient failures keep it PENDING
        # because the async task retries without updating the status on failure.
        response = self.client.post(
            self.endpoint,
            data={"source": "https://source.example", "target": "http://testserver/blog/post/hello/"},
        )

        self.assertEqual(response.status_code, 202)
        mention = Webmention.objects.get()
        self.assertEqual(mention.status, Webmention.PENDING)


@override_settings(ALLOWED_HOSTS=["testserver"])
class WebmentionSubmissionTests(TestCase):
    def setUp(self):
        super().setUp()
        self.post = Post.objects.create(title="Hello", slug="hello", content="Hello world")
        self.endpoint = reverse("webmention-submit")
        self.target_url = "http://testserver/blog/post/hello/"

    def test_authenticated_submission_creates_webmention(self):
        session = self.client.session
        session["indieauth_me"] = "https://example.com/"
        session.save()

        response = self.client.post(
            self.endpoint,
            data={
                "source": "https://blog.example.com/post",
                "target": self.target_url,
                "mention_type": Webmention.REPOST,
                "next": "/blog/post/hello/",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(Webmention.objects.count(), 1)
        self.assertEqual(Webmention.objects.first().mention_type, Webmention.REPOST)

    def test_unauthenticated_submission_is_rejected(self):
        with self.assertLogs("micropub.views", level="INFO"):
            response = self.client.post(
                self.endpoint,
                data={
                    "source": "https://source.example/post",
                    "target": self.target_url,
                    "next": "/blog/post/hello/",
                },
            )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(Webmention.objects.count(), 0)


    def test_submission_rejected_when_source_not_owned(self):
        session = self.client.session
        session["indieauth_me"] = "https://example.com/"
        session.save()

        with self.assertLogs("micropub.views", level="INFO"):
            response = self.client.post(
                self.endpoint,
                data={
                    "source": "https://not-example.com/post",
                    "target": self.target_url,
                    "next": "/blog/post/hello/",
                },
            )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(Webmention.objects.count(), 0)

    def test_invalid_mention_type_defaults(self):
        session = self.client.session
        session["indieauth_me"] = "https://example.com/"
        session.save()

        response = self.client.post(
            self.endpoint,
            data={
                "source": "https://example.com/post",
                "target": self.target_url,
                "mention_type": "unknown",
                "next": "/blog/post/hello/",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(Webmention.objects.count(), 1)
        self.assertEqual(Webmention.objects.first().mention_type, Webmention.MENTION)

    def test_missing_source_logs_error(self):
        session = self.client.session
        session["indieauth_me"] = "https://example.com/"
        session.save()

        with self.assertLogs("micropub.views", level="INFO"):
            response = self.client.post(
                self.endpoint,
                data={
                    "source": "",
                    "target": self.target_url,
                    "next": "/blog/post/hello/",
                },
            )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(Webmention.objects.count(), 0)


class BridgyPublishWebmentionTests(TestCase):
    @patch("micropub.tasks.send_single_webmention")
    def test_bridgy_publish_skips_like_reply_repost(self, mock_task):
        """dispatch_webmentions must not send bridgy webmentions for likes, replies, and reposts.

        The guard lives in dispatch_webmentions (micropub/tasks.py), not in
        send_bridgy_publish_webmentions which no longer contains this check.
        """
        from micropub.tasks import dispatch_webmentions

        config = SiteConfiguration.get_solo()
        config.bridgy_publish_bluesky = True
        config.bridgy_publish_flickr = False
        config.bridgy_publish_github = False
        config.save()

        source_url = "http://testserver/blog/post/hello/"
        for kind in (Post.LIKE, Post.REPLY, Post.REPOST):
            with self.subTest(kind=kind):
                mock_task.delay.reset_mock()
                post = Post.objects.create(
                    title="Hello",
                    slug=f"hello-{kind}",
                    content="Hello world",
                    kind=kind,
                )
                dispatch_webmentions(post.id, source_url, include_bridgy=True)

                for call in mock_task.delay.call_args_list:
                    target_url = call[0][2]  # positional arg: (post_id, source_url, target_url, ...)
                    self.assertNotIn(
                        "brid.gy",
                        target_url,
                        f"dispatch_webmentions sent a bridgy webmention for post kind={kind}",
                    )


class WebmentionDirectionTests(TestCase):
    """Tests for the is_incoming direction guard."""

    def test_outgoing_webmention_does_not_notify_microsub(self):
        from microsub.models import Channel, Entry

        channel = Channel.objects.get(uid="notifications")
        post = Post.objects.create(title="Like", slug="like-post", content="", like_of="https://example.com/post")

        # Simulate what send_webmention creates (is_incoming=False)
        mention = Webmention.objects.create(
            source="http://testserver/blog/post/like-post/",
            target="https://example.com/post",
            mention_type=Webmention.LIKE,
            status=Webmention.ACCEPTED,
            target_post=post,
            is_incoming=False,
        )

        self.assertEqual(Entry.objects.filter(channel=channel).count(), 0)

    def test_incoming_webmention_creates_microsub_notification(self):
        from microsub.models import Channel, Entry

        channel = Channel.objects.get(uid="notifications")
        post = Post.objects.create(title="Hello", slug="hello-notify", content="Hello world")

        Webmention.objects.create(
            source="https://source.example/post",
            target="http://testserver/blog/post/hello-notify/",
            mention_type=Webmention.MENTION,
            status=Webmention.ACCEPTED,
            target_post=post,
            is_incoming=True,
        )

        self.assertEqual(Entry.objects.filter(channel=channel).count(), 1)


class GlobalBlockWebmentionTests(TestCase):
    @patch("micropub.tasks.send_single_webmention")
    def test_dispatch_webmentions_skips_globally_blocked_targets(self, mock_task):
        from microsub.models import BlockedUser
        from micropub.tasks import dispatch_webmentions

        BlockedUser.objects.create(channel=None, url="https://blocked.example/")
        post = Post.objects.create(
            title="Blocked target",
            slug="blocked-target",
            content="A link to https://blocked.example/post/",
        )

        dispatch_webmentions(post.id, "http://testserver/blog/post/blocked-target/", include_bridgy=False)

        mock_task.delay.assert_not_called()

    @patch("micropub.webmention._send_webmention_request")
    def test_send_webmention_does_not_send_to_globally_blocked_target(self, mock_send):
        from microsub.models import BlockedUser

        BlockedUser.objects.create(channel=None, url="https://blocked.example/")
        wm = send_webmention("https://example.com/source/", "https://blocked.example/post/")

        self.assertEqual(wm.status, Webmention.REJECTED)
        self.assertIn("blocked", wm.error.lower())
        mock_send.assert_not_called()


class WebmentionResendTests(TestCase):
    """Tests for resend_webmention behaviour."""

    @patch("micropub.webmention._send_webmention_request", return_value=(Webmention.ACCEPTED, ""))
    def test_resend_preserves_mention_type(self, mock_send):
        post = Post.objects.create(title="Like", slug="like-resend", content="", like_of="https://example.com/t")
        mention = Webmention.objects.create(
            source="http://testserver/blog/post/like-resend/",
            target="https://example.com/t",
            mention_type=Webmention.LIKE,
            status=Webmention.TIMED_OUT,
            is_incoming=False,
        )

        resend_webmention(mention)

        mock_send.assert_called_once_with(mention.source, mention.target, Webmention.LIKE)


class WebmentionDeduplicationTests(TestCase):
    """Tests for incoming-webmention deduplication via update_or_create."""

    @override_settings(ALLOWED_HOSTS=["testserver"], WEBMENTION_TRUSTED_DOMAINS=[])
    def test_duplicate_incoming_webmention_updates_not_duplicates(self):
        post = Post.objects.create(title="Hello", slug="hello-dedup", content="Hello world")
        endpoint = reverse("webmention-endpoint")
        data = {
            "source": "https://source.example/post",
            "target": "http://testserver/blog/post/hello-dedup/",
        }

        self.client.post(endpoint, data=data)
        self.client.post(endpoint, data=data)

        self.assertEqual(Webmention.objects.count(), 1)


class WebmentionRetryTests(TestCase):
    """Tests for retrying failed/timed-out outgoing webmentions."""

    @patch("micropub.webmention.send_webmention")
    def test_rejected_outgoing_is_retried(self, mock_send):
        post = Post.objects.create(
            title="Reply",
            slug="reply-retry",
            content="",
            in_reply_to="https://example.com/original",
        )
        source_url = "http://testserver/blog/post/reply-retry/"
        # Pre-existing REJECTED record for the same source+target
        Webmention.objects.create(
            source=source_url,
            target="https://example.com/original",
            mention_type=Webmention.REPLY,
            status=Webmention.REJECTED,
            is_incoming=False,
        )

        send_webmentions_for_post(post, source_url)

        mock_send.assert_called_once()

    @patch("micropub.webmention.send_webmention")
    def test_timed_out_outgoing_is_retried(self, mock_send):
        post = Post.objects.create(
            title="Reply TO",
            slug="reply-timeout-retry",
            content="",
            in_reply_to="https://example.com/original2",
        )
        source_url = "http://testserver/blog/post/reply-timeout-retry/"
        Webmention.objects.create(
            source=source_url,
            target="https://example.com/original2",
            mention_type=Webmention.REPLY,
            status=Webmention.TIMED_OUT,
            is_incoming=False,
        )

        send_webmentions_for_post(post, source_url)

        mock_send.assert_called_once()


class WebmentionBookmarkTests(TestCase):
    """Tests for bookmark_of mention type."""

    @patch("micropub.webmention.send_webmention")
    def test_bookmark_post_sends_bookmark_mention_type(self, mock_send):
        post = Post.objects.create(
            title="Bookmark",
            slug="bookmark-test",
            content="",
            bookmark_of="https://example.com/bookmarked",
        )
        source_url = "http://testserver/blog/post/bookmark-test/"

        send_webmentions_for_post(post, source_url)

        mock_send.assert_called_once()
        _, kwargs = mock_send.call_args
        self.assertEqual(kwargs.get("mention_type"), Webmention.BOOKMARK)


class NormalizeUrlTests(TestCase):
    """Tests for _normalize_url_for_compare trailing-slash handling."""

    def test_with_and_without_trailing_slash_match(self):
        self.assertEqual(
            _normalize_url_for_compare("https://example.com/post/hello"),
            _normalize_url_for_compare("https://example.com/post/hello/"),
        )

    def test_scheme_and_netloc_lowercased(self):
        self.assertEqual(
            _normalize_url_for_compare("HTTPS://EXAMPLE.COM/post/"),
            _normalize_url_for_compare("https://example.com/post/"),
        )


class VerifyWebmentionSourceTests(TestCase):
    """Tests for verify_webmention_source edge cases."""

    @patch("micropub.webmention.urllib.request.urlopen")
    def test_410_gone_returns_rejected_not_pending(self, mock_urlopen):
        exc = urllib.error.HTTPError(
            url="https://source.example/gone",
            code=410,
            msg="Gone",
            hdrs=MagicMock(),
            fp=None,
        )
        mock_urlopen.side_effect = exc

        verified, error, fetch_failed = verify_webmention_source(
            "https://source.example/gone",
            "https://target.example/post/",
        )

        self.assertFalse(verified)
        self.assertIn("410", error)
        self.assertFalse(fetch_failed)  # fetch_failed=False → REJECTED status

    @patch("micropub.webmention.urllib.request.urlopen")
    def test_non_410_http_error_returns_fetch_failed(self, mock_urlopen):
        exc = urllib.error.HTTPError(
            url="https://source.example/error",
            code=500,
            msg="Internal Server Error",
            hdrs=MagicMock(),
            fp=None,
        )
        mock_urlopen.side_effect = exc

        verified, error, fetch_failed = verify_webmention_source(
            "https://source.example/error",
            "https://target.example/post/",
        )

        self.assertFalse(verified)
        self.assertTrue(fetch_failed)  # fetch_failed=True → stays PENDING


class WmPropertyRetryTests(TestCase):
    """Tests for the wm-property retry logic in send_webmention / resend_webmention."""

    @patch("micropub.webmention._send_webmention_request")
    def test_wm_property_rejection_retries_without_wm_property(self, mock_req):
        # First call: REJECTED (endpoint exists but rejects wm-property)
        # Second call: ACCEPTED without wm-property
        mock_req.side_effect = [
            (Webmention.REJECTED, "Bad request"),
            (Webmention.ACCEPTED, ""),
        ]
        wm = send_webmention("https://example.com/source/", "https://remote.example/post/")

        self.assertEqual(mock_req.call_count, 2)
        # Second call should pass include_wm_property=False
        _, kwargs = mock_req.call_args
        self.assertFalse(kwargs.get("include_wm_property", True))
        self.assertEqual(wm.status, Webmention.ACCEPTED)

    @patch("micropub.webmention._send_webmention_request")
    def test_no_endpoint_rejection_does_not_retry(self, mock_req):
        mock_req.return_value = (Webmention.REJECTED, "No webmention endpoint found")
        wm = send_webmention("https://example.com/source/", "https://remote.example/post/")

        self.assertEqual(mock_req.call_count, 1)
        self.assertEqual(wm.status, Webmention.REJECTED)

    @patch("micropub.webmention._send_webmention_request")
    def test_resend_retries_without_wm_property(self, mock_req):
        mock_req.side_effect = [
            (Webmention.REJECTED, "Bad request"),
            (Webmention.ACCEPTED, ""),
        ]
        wm = Webmention.objects.create(
            source="https://example.com/source/",
            target="https://remote.example/post/",
            mention_type=Webmention.MENTION,
            status=Webmention.REJECTED,
            is_incoming=False,
        )
        result = resend_webmention(wm)

        self.assertEqual(mock_req.call_count, 2)
        self.assertEqual(result.status, Webmention.ACCEPTED)


class MicropubDraftTests(TestCase):
    def _post(self, data, *, json_body=False):
        if json_body:
            return self.client.post(
                MICROPUB_URL,
                data=json.dumps(data),
                content_type="application/json",
                HTTP_AUTHORIZATION="Bearer token",
            )
        return self.client.post(MICROPUB_URL, data=data, HTTP_AUTHORIZATION="Bearer token")

    def _live_post(self, slug):
        from django.utils import timezone

        return Post.objects.create(
            title="Live", slug=slug, content="hi", published_on=timezone.now() - timezone.timedelta(hours=1)
        )

    @patch("micropub.views._authorized", return_value=(True, ["create"]))
    def test_post_status_draft_creates_draft(self, _authorized):
        response = self._post({"content": "Hello", "post-status": "draft"})

        self.assertEqual(response.status_code, 201)
        post = Post.objects.get()
        self.assertIsNone(post.published_on)
        self.assertTrue(response["Location"].endswith(post.get_absolute_url()))

    @patch("micropub.views._authorized", return_value=(True, ["create"]))
    def test_create_without_post_status_publishes(self, _authorized):
        response = self._post({"content": "Hello"})

        self.assertEqual(response.status_code, 201)
        self.assertTrue(Post.objects.get().is_live())

    @patch("micropub.views._authorized", return_value=(True, ["create"]))
    def test_unknown_post_status_is_rejected(self, _authorized):
        response = self._post({"content": "Hello", "post-status": "private"})

        self.assertEqual(response.status_code, 400)
        self.assertFalse(Post.objects.exists())

    @patch("micropub.views._authorized", return_value=(True, ["draft"]))
    def test_draft_scope_always_creates_drafts(self, _authorized):
        response = self._post({"content": "Hello", "post-status": "published"})

        self.assertEqual(response.status_code, 201)
        self.assertIsNone(Post.objects.get().published_on)

    @patch("micropub.views._authorized", return_value=(True, ["draft"]))
    def test_draft_scope_can_update_draft(self, _authorized):
        post = Post.objects.create(title="Draft", slug="draft-1", content="old")

        response = self._post(
            {"action": "update", "url": "https://example.com/blog/post/draft-1/", "replace": {"content": ["new"]}},
            json_body=True,
        )

        self.assertEqual(response.status_code, 204)
        post.refresh_from_db()
        self.assertEqual(post.content, "new")

    @patch("micropub.views._authorized", return_value=(True, ["draft"]))
    def test_draft_scope_cannot_update_live_post(self, _authorized):
        post = self._live_post("live-1")

        response = self._post(
            {"action": "update", "url": "https://example.com/blog/post/live-1/", "replace": {"content": ["new"]}},
            json_body=True,
        )

        self.assertEqual(response.status_code, 403)
        post.refresh_from_db()
        self.assertEqual(post.content, "hi")

    @patch("micropub.views._authorized", return_value=(True, ["draft", "update"]))
    def test_publishing_draft_via_update_needs_create(self, _authorized):
        post = Post.objects.create(title="Draft", slug="draft-2", content="hi")

        response = self._post(
            {
                "action": "update",
                "url": "https://example.com/blog/post/draft-2/",
                "replace": {"post-status": ["published"]},
            },
            json_body=True,
        )

        self.assertEqual(response.status_code, 403)
        post.refresh_from_db()
        self.assertIsNone(post.published_on)

    @patch("micropub.views._authorized", return_value=(True, ["create", "update"]))
    def test_create_scope_can_publish_draft_via_update(self, _authorized):
        post = Post.objects.create(title="Draft", slug="draft-3", content="hi")

        response = self._post(
            {
                "action": "update",
                "url": "https://example.com/blog/post/draft-3/",
                "replace": {"post-status": ["published"]},
            },
            json_body=True,
        )

        self.assertEqual(response.status_code, 204)
        post.refresh_from_db()
        self.assertTrue(post.is_live())

    @patch("micropub.views._authorized", return_value=(True, ["draft"]))
    def test_draft_scope_can_delete_draft_but_not_live_post(self, _authorized):
        draft = Post.objects.create(title="Draft", slug="draft-4", content="hi")
        live = self._live_post("live-2")

        response = self._post({"action": "delete", "url": "https://example.com/blog/post/draft-4/"})
        self.assertEqual(response.status_code, 204)
        response = self._post({"action": "delete", "url": "https://example.com/blog/post/live-2/"})
        self.assertEqual(response.status_code, 403)

        draft.refresh_from_db()
        live.refresh_from_db()
        self.assertTrue(draft.deleted)
        self.assertFalse(live.deleted)

    @patch("micropub.views._authorized", return_value=(True, ["read"]))
    def test_source_query_reports_draft_status(self, _authorized):
        Post.objects.create(title="Draft", slug="draft-5", content="hi")

        response = self.client.get(
            MICROPUB_URL,
            {"q": "source", "url": "https://example.com/blog/post/draft-5/"},
            HTTP_AUTHORIZATION="Bearer token",
        )

        self.assertEqual(response.json()["properties"]["post-status"], ["draft"])


class MicropubSlugTests(TestCase):
    def _post(self, data, *, json_body=False):
        if json_body:
            return self.client.post(
                MICROPUB_URL,
                data=json.dumps(data),
                content_type="application/json",
                HTTP_AUTHORIZATION="Bearer token",
            )
        return self.client.post(MICROPUB_URL, data=data, HTTP_AUTHORIZATION="Bearer token")

    @patch("micropub.views._authorized", return_value=(True, ["create"]))
    def test_form_encoded_mp_slug_is_used(self, _authorized):
        response = self._post({"content": "Hello", "mp-slug": "My Cool Post!"})

        self.assertEqual(response.status_code, 201)
        post = Post.objects.get()
        self.assertEqual(post.slug, "my-cool-post")
        self.assertTrue(response["Location"].endswith(post.get_absolute_url()))
        self.assertNotIn("mp-slug", post.mf2 or {})

    @patch("micropub.views._authorized", return_value=(True, ["create"]))
    def test_json_mp_slug_at_top_level_and_under_properties(self, _authorized):
        top = self._post(
            {"type": ["h-entry"], "properties": {"content": ["Hello"]}, "mp-slug": ["top-level-slug"]},
            json_body=True,
        )
        nested = self._post(
            {"type": ["h-entry"], "properties": {"content": ["Hello"], "mp-slug": ["nested-slug"]}},
            json_body=True,
        )

        self.assertEqual(top.status_code, 201)
        self.assertEqual(nested.status_code, 201)
        self.assertTrue(Post.objects.filter(slug="top-level-slug").exists())
        self.assertTrue(Post.objects.filter(slug="nested-slug").exists())
        top_post = Post.objects.get(slug="top-level-slug")
        self.assertTrue(top["Location"].endswith(top_post.get_absolute_url()))
        self.assertNotIn("mp-slug", top_post.mf2 or {})

    @patch("micropub.views._authorized", return_value=(True, ["create"]))
    def test_omitted_mp_slug_keeps_title_timestamp_slug(self, _authorized):
        response = self._post({"name": "Hello World", "content": "Body"})

        self.assertEqual(response.status_code, 201)
        post = Post.objects.get()
        self.assertTrue(post.slug.startswith("hello-world-"))
        self.assertTrue(response["Location"].endswith(post.get_absolute_url()))


class MicropubTokenAuthorTests(TestCase):
    def test_post_author_is_token_user(self):
        import hashlib
        from django.contrib.auth import get_user_model
        from django.utils import timezone
        from indieauth.models import IndieAuthAccessToken

        user = get_user_model().objects.create_user(username="owner", password="pw")
        IndieAuthAccessToken.objects.create(
            token_hash=hashlib.sha256(b"local-token").hexdigest(),
            client_id="https://client.example/",
            me="http://testserver/",
            scope="create",
            user=user,
            expires_at=timezone.now() + timezone.timedelta(hours=1),
        )

        response = self.client.post(
            MICROPUB_URL, data={"content": "Hello"}, HTTP_AUTHORIZATION="Bearer local-token"
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(Post.objects.get().author, user)


class MicropubMediaScopeTests(TestCase):
    def _upload(self, **extra):
        upload = SimpleUploadedFile("photo.jpg", b"fake-image-data", content_type="image/jpeg")
        return self.client.post(
            reverse("micropub-media"),
            data={"file": upload, **extra},
            HTTP_AUTHORIZATION="Bearer token",
        )

    @patch("micropub.views._authorized", return_value=(True, ["media"]))
    def test_media_scope_can_upload_with_alt_text(self, _authorized):
        with tempfile.TemporaryDirectory() as media_root:
            with override_settings(MEDIA_ROOT=media_root):
                response = self._upload(alt="A red bike")

        self.assertEqual(response.status_code, 201)
        self.assertEqual(File.objects.get().alt_text, "A red bike")

    @patch("micropub.views._authorized", return_value=(True, ["create"]))
    def test_create_scope_can_still_upload(self, _authorized):
        with tempfile.TemporaryDirectory() as media_root:
            with override_settings(MEDIA_ROOT=media_root):
                response = self._upload()

        self.assertEqual(response.status_code, 201)

    @patch("micropub.views._authorized", return_value=(True, ["read", "draft"]))
    def test_upload_requires_media_or_create(self, _authorized):
        response = self._upload()

        self.assertEqual(response.status_code, 403)
        self.assertFalse(File.objects.exists())


class MicropubRevisionTests(TestCase):
    def setUp(self):
        import hashlib
        from django.contrib.auth import get_user_model
        from django.utils import timezone
        from indieauth.models import IndieAuthAccessToken

        self.user = get_user_model().objects.create_user(username="owner", password="pw")
        self.token = IndieAuthAccessToken.objects.create(
            token_hash=hashlib.sha256(b"local-token").hexdigest(),
            client_id="https://client.example/",
            me="http://testserver/",
            scope="create update delete undelete",
            user=self.user,
            expires_at=timezone.now() + timezone.timedelta(hours=1),
        )

    def _micropub(self, payload):
        return self.client.post(
            MICROPUB_URL,
            data=json.dumps(payload),
            content_type="application/json",
            HTTP_AUTHORIZATION="Bearer local-token",
        )

    @patch("micropub.webmention.queue_webmentions_for_post")
    def test_every_action_writes_a_revision_with_the_token(self, _queue):
        response = self._micropub({"type": ["h-entry"], "properties": {"content": ["Hello"]}})
        self.assertEqual(response.status_code, 201)
        post = Post.objects.get()
        url = f"http://testserver{post.get_absolute_url()}"

        for payload in (
            {"action": "update", "url": url, "replace": {"content": ["Edited"]}},
            {"action": "delete", "url": url},
            {"action": "undelete", "url": url},
        ):
            response = self._micropub(payload)
            self.assertIn(response.status_code, (200, 201, 204), payload)

        revisions = list(post.revisions.order_by("id"))
        self.assertEqual([r.action for r in revisions], ["create", "update", "delete", "undelete"])
        for revision in revisions:
            self.assertEqual(revision.actor_source, "micropub")
            self.assertEqual(revision.actor_user, self.user)
            self.assertEqual(revision.token, self.token)
            self.assertEqual(revision.client_id, "https://client.example/")
        self.assertEqual(revisions[1].snapshot["content"], "Hello")
        self.assertEqual(revisions[2].snapshot["content"], "Edited")
