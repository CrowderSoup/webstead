import json
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
