from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from microsub.models import BlockedUser, Channel, Entry, MutedUser

MICROSUB_URL = "/microsub"
ALL_SCOPES = ["read", "follow", "channels", "mute", "block"]
authorized = patch("micropub.views._authorized", return_value=(True, ALL_SCOPES))


class SearchPreviewMethodTests(TestCase):
    @authorized
    @patch("microsub.views._search_feed_results", return_value=[{"type": "feed", "url": "https://example.com/feed"}])
    def test_post_search_returns_feed_results(self, _mock_search, _auth):
        response = self.client.post(
            MICROSUB_URL,
            {"action": "search", "query": "example.com"},
            HTTP_AUTHORIZATION="Bearer token",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["results"], [{"type": "feed", "url": "https://example.com/feed"}])

    @authorized
    @patch("microsub.views._preview_payload", return_value={"type": "feed", "url": "https://example.com/feed", "items": [], "paging": {}})
    def test_post_preview_returns_feed_payload(self, _mock_preview, _auth):
        response = self.client.post(
            MICROSUB_URL,
            {"action": "preview", "url": "https://example.com/feed"},
            HTTP_AUTHORIZATION="Bearer token",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["type"], "feed")
        self.assertIn("items", response.json())


class ContentSearchTests(TestCase):
    def setUp(self):
        self.news = Channel.objects.create(uid="news", name="News")
        self.tech = Channel.objects.create(uid="tech", name="Tech")
        now = timezone.now()

        self.news_like = Entry.objects.create(
            channel=self.news,
            uid="news-like",
            data={
                "type": "entry",
                "name": "Hello Python",
                "summary": "Microsub search coverage",
                "content": {"text": "hello world from alice"},
                "author": {"type": "card", "url": "https://authors.example/alice/"},
                "category": ["python", "django"],
                "like-of": ["https://liked.example/post"],
                "_source": {"url": "https://feeds.example/news.xml"},
            },
            published=now,
        )
        self.news_reply = Entry.objects.create(
            channel=self.news,
            uid="news-reply",
            data={
                "type": "entry",
                "name": "Reply from Bob",
                "content": {"text": "hello bob world"},
                "author": {"type": "card", "url": "https://authors.example/bob/"},
                "category": ["python"],
                "in-reply-to": ["https://reply.example/post"],
                "_source": {"url": "https://feeds.example/news.xml"},
            },
            published=now.replace(microsecond=0) + timedelta(seconds=1),
        )
        self.tech_like = Entry.objects.create(
            channel=self.tech,
            uid="tech-like",
            data={
                "type": "entry",
                "name": "Hello Python Again",
                "content": {"text": "hello world from alice in tech"},
                "author": {"type": "card", "url": "https://authors.example/alice/"},
                "category": ["python"],
                "like-of": ["https://liked.example/post"],
                "_source": {"url": "https://feeds.example/tech.xml"},
            },
            published=now.replace(microsecond=0) + timedelta(seconds=2),
        )
        self.removed = Entry.objects.create(
            channel=self.news,
            uid="removed-entry",
            data={
                "type": "entry",
                "content": {"text": "hello hidden world"},
                "author": {"type": "card", "url": "https://authors.example/hidden/"},
                "_source": {"url": "https://feeds.example/news.xml"},
            },
            published=now.replace(microsecond=0) + timedelta(seconds=3),
            is_removed=True,
        )

    @authorized
    def test_content_search_requires_at_least_one_filter(self, _auth):
        response = self.client.post(
            MICROSUB_URL,
            {"action": "search", "channel": "news"},
            HTTP_AUTHORIZATION="Bearer token",
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "invalid_request")

    @authorized
    def test_post_content_search_query_matches_all_tokens(self, _auth):
        response = self.client.post(
            MICROSUB_URL,
            {"action": "search", "channel": "news", "query": "hello world"},
            HTTP_AUTHORIZATION="Bearer token",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual([item["uid"] for item in response.json()["items"]], ["news-reply", "news-like"])

    @authorized
    def test_content_search_combines_or_within_filter_and_and_across_filters(self, _auth):
        response = self.client.post(
            MICROSUB_URL,
            {
                "action": "search",
                "channel": "news",
                "author": [
                    "https://authors.example/alice/",
                    "https://authors.example/bob/",
                ],
                "category": ["python"],
                "source": ["https://feeds.example/news.xml"],
            },
            HTTP_AUTHORIZATION="Bearer token",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual([item["uid"] for item in response.json()["items"]], ["news-reply", "news-like"])

    @authorized
    def test_content_search_kind_and_source_filters(self, _auth):
        response = self.client.get(
            MICROSUB_URL,
            {
                "action": "search",
                "channel": "news",
                "kind": "like",
                "source": "https://feeds.example/news.xml",
            },
            HTTP_AUTHORIZATION="Bearer token",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual([item["uid"] for item in response.json()["items"]], ["news-like"])

    @authorized
    def test_global_content_search_spans_channels_without_deduping(self, _auth):
        response = self.client.post(
            MICROSUB_URL,
            {"action": "search", "channel": "global", "query": "hello world", "limit": "10"},
            HTTP_AUTHORIZATION="Bearer token",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [item["uid"] for item in response.json()["items"]],
            ["tech-like", "news-reply", "news-like"],
        )

    @authorized
    def test_content_search_excludes_muted_blocked_and_removed_entries(self, _auth):
        MutedUser.objects.create(channel=None, url="https://authors.example/alice/")
        BlockedUser.objects.create(channel=None, url="https://authors.example/bob/")
        response = self.client.post(
            MICROSUB_URL,
            {"action": "search", "channel": "global", "query": "hello world"},
            HTTP_AUTHORIZATION="Bearer token",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["items"], [])

    @authorized
    def test_content_search_limit_and_paging(self, _auth):
        response = self.client.get(
            MICROSUB_URL,
            {"action": "search", "channel": "global", "query": "hello world", "limit": "1"},
            HTTP_AUTHORIZATION="Bearer token",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual([item["uid"] for item in response.json()["items"]], ["tech-like"])
        self.assertIn("after", response.json()["paging"])
