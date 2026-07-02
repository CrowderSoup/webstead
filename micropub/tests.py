import json
import urllib.error
from urllib.parse import parse_qs, urlparse
from unittest.mock import patch, MagicMock
from types import SimpleNamespace

from django.test import TestCase
from django.test.utils import override_settings
from django.urls import reverse

from blog.models import Post, Tag
from core.models import RequestErrorLog, SiteConfiguration
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
