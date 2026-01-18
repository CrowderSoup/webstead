import json
from urllib.parse import parse_qs, urlparse
from unittest.mock import patch

from django.test import TestCase
from django.test.utils import override_settings
from django.urls import reverse

from blog.models import Post, Tag
from micropub.models import Webmention


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
    @patch("micropub.views.verify_webmention_source", return_value=(True, "", False))
    def test_verified_webmention_is_pending_by_default(self, _verify):
        response = self.client.post(
            self.endpoint,
            data={"source": "https://source.example", "target": "http://testserver/blog/post/hello/"},
        )

        self.assertEqual(response.status_code, 202)
        mention = Webmention.objects.get()
        self.assertEqual(mention.status, Webmention.PENDING)

    @override_settings(WEBMENTION_TRUSTED_DOMAINS=["trusted.example"])
    @patch("micropub.views.verify_webmention_source", return_value=(True, "", False))
    def test_trusted_domain_auto_approves(self, _verify):
        response = self.client.post(
            self.endpoint,
            data={"source": "https://trusted.example/post", "target": "http://testserver/blog/post/hello/"},
        )

        self.assertEqual(response.status_code, 202)
        mention = Webmention.objects.get()
        self.assertEqual(mention.status, Webmention.ACCEPTED)

    @patch("micropub.views.verify_webmention_source", return_value=(False, "No link found", False))
    def test_missing_link_rejects(self, _verify):
        response = self.client.post(
            self.endpoint,
            data={"source": "https://source.example", "target": "http://testserver/blog/post/hello/"},
        )

        self.assertEqual(response.status_code, 400)
        mention = Webmention.objects.get()
        self.assertEqual(mention.status, Webmention.REJECTED)

    @patch("micropub.views.verify_webmention_source", return_value=(False, "Fetch failed", True))
    def test_fetch_failures_stay_pending(self, _verify):
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

    @patch("micropub.views.verify_webmention_source", return_value=(True, "", False))
    def test_authenticated_submission_creates_webmention(self, _verify):
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

    @patch("micropub.views.verify_webmention_source", return_value=(True, "", False))
    def test_submission_rejected_when_source_not_owned(self, _verify):
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

    @patch("micropub.views.verify_webmention_source", return_value=(True, "", False))
    def test_invalid_mention_type_defaults(self, _verify):
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
