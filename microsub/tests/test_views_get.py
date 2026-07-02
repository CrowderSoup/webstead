"""Tests for MicrosubView GET handlers."""
import datetime
from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from microsub.models import BlockedUser, Channel, Entry, MutedUser, Subscription

MICROSUB_URL = "/microsub"
ALL_SCOPES = ["read", "follow", "channels", "mute", "block"]
authorized = patch("micropub.views._authorized", return_value=(True, ALL_SCOPES))


class MicrosubViewAuthTests(TestCase):
    def test_no_token_returns_401(self):
        response = self.client.get(MICROSUB_URL)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"], "unauthorized")

    @patch("micropub.views._authorized", return_value=(False, []))
    def test_invalid_token_returns_401(self, _auth):
        response = self.client.get(MICROSUB_URL, HTTP_AUTHORIZATION="Bearer bad")
        self.assertEqual(response.status_code, 401)

    @authorized
    def test_unknown_get_action_returns_400(self, _auth):
        response = self.client.get(
            MICROSUB_URL, {"action": "bogus"}, HTTP_AUTHORIZATION="Bearer token"
        )
        self.assertEqual(response.status_code, 400)

    @authorized
    def test_unknown_post_action_returns_400(self, _auth):
        response = self.client.post(
            MICROSUB_URL, {"action": "bogus"}, HTTP_AUTHORIZATION="Bearer token"
        )
        self.assertEqual(response.status_code, 400)


class GetChannelsTests(TestCase):
    @patch("micropub.views._authorized", return_value=(True, []))
    def test_missing_read_scope_returns_403(self, _auth):
        response = self.client.get(
            MICROSUB_URL, {"action": "channels"}, HTTP_AUTHORIZATION="Bearer token"
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"], "insufficient_scope")

    @authorized
    def test_returns_all_channels(self, _auth):
        Channel.objects.create(uid="news", name="News", order=1)
        Channel.objects.create(uid="tech", name="Tech", order=2)
        response = self.client.get(
            MICROSUB_URL, {"action": "channels"}, HTTP_AUTHORIZATION="Bearer token"
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("channels", data)
        uids = [channel["uid"] for channel in data["channels"]]
        self.assertEqual(uids[0], "notifications")
        self.assertIn("home", uids)
        self.assertIn("news", uids)
        self.assertIn("tech", uids)

    @authorized
    def test_empty_channel_list_contains_seed_channel(self, _auth):
        # Seed migration always creates 'notifications'; expect at least that one
        response = self.client.get(
            MICROSUB_URL, {"action": "channels"}, HTTP_AUTHORIZATION="Bearer token"
        )
        uids = [c["uid"] for c in response.json()["channels"]]
        self.assertIn("notifications", uids)

    @authorized
    def test_channel_json_shape(self, _auth):
        Channel.objects.create(uid="news", name="News")
        response = self.client.get(
            MICROSUB_URL, {"action": "channels"}, HTTP_AUTHORIZATION="Bearer token"
        )
        ch = response.json()["channels"][0]
        self.assertIn("uid", ch)
        self.assertIn("name", ch)
        self.assertIn("unread", ch)


class GetFollowTests(TestCase):
    def setUp(self):
        self.channel = Channel.objects.create(uid="news", name="News")

    @patch("micropub.views._authorized", return_value=(True, []))
    def test_missing_read_scope_returns_403(self, _auth):
        response = self.client.get(
            MICROSUB_URL, {"action": "follow", "channel": "news"}, HTTP_AUTHORIZATION="Bearer token"
        )
        self.assertEqual(response.status_code, 403)

    @authorized
    def test_unknown_channel_returns_400(self, _auth):
        response = self.client.get(
            MICROSUB_URL, {"action": "follow", "channel": "nope"}, HTTP_AUTHORIZATION="Bearer token"
        )
        self.assertEqual(response.status_code, 400)

    @authorized
    def test_returns_active_subscriptions(self, _auth):
        Subscription.objects.create(channel=self.channel, url="https://a.example.com/feed", is_active=True)
        Subscription.objects.create(channel=self.channel, url="https://b.example.com/feed", is_active=False)
        response = self.client.get(
            MICROSUB_URL, {"action": "follow", "channel": "news"}, HTTP_AUTHORIZATION="Bearer token"
        )
        self.assertEqual(response.status_code, 200)
        items = response.json()["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["url"], "https://a.example.com/feed")

    @authorized
    def test_each_item_has_required_fields(self, _auth):
        Subscription.objects.create(
            channel=self.channel, url="https://a.example.com/feed", name="A Feed", photo=""
        )
        response = self.client.get(
            MICROSUB_URL, {"action": "follow", "channel": "news"}, HTTP_AUTHORIZATION="Bearer token"
        )
        item = response.json()["items"][0]
        self.assertEqual(item["type"], "feed")
        self.assertIn("url", item)
        self.assertIn("name", item)
        self.assertIn("photo", item)

    @authorized
    def test_managed_items_include_provider_metadata(self, _auth):
        Subscription.objects.create(
            channel=self.channel,
            url="https://a.example.com/feed",
            name="Mastodon home timeline",
            managed_by="mastodon",
            managed_key="timeline",
        )
        response = self.client.get(
            MICROSUB_URL, {"action": "follow", "channel": "news"}, HTTP_AUTHORIZATION="Bearer token"
        )

        item = response.json()["items"][0]
        self.assertTrue(item["_is_managed"])
        self.assertEqual(item["_provider"], "mastodon")
        self.assertEqual(item["_managed_key"], "timeline")

    @authorized
    def test_empty_channel_returns_empty_items(self, _auth):
        response = self.client.get(
            MICROSUB_URL, {"action": "follow", "channel": "news"}, HTTP_AUTHORIZATION="Bearer token"
        )
        self.assertEqual(response.json(), {"items": []})


class GetTimelineTests(TestCase):
    def setUp(self):
        self.channel = Channel.objects.create(uid="news", name="News")
        now = timezone.now()
        self.e1 = Entry.objects.create(
            channel=self.channel, uid="e1",
            data={"type": "entry", "author": {"url": "https://author.example.com/"}},
            author_url="https://author.example.com/",
            published=now,
        )
        self.e2 = Entry.objects.create(
            channel=self.channel, uid="e2",
            data={"type": "entry"},
            published=now + datetime.timedelta(seconds=1),
        )

    @patch("micropub.views._authorized", return_value=(True, []))
    def test_missing_read_scope_returns_403(self, _auth):
        response = self.client.get(
            MICROSUB_URL, {"action": "timeline", "channel": "news"}, HTTP_AUTHORIZATION="Bearer token"
        )
        self.assertEqual(response.status_code, 403)

    @authorized
    def test_unknown_channel_returns_400(self, _auth):
        response = self.client.get(
            MICROSUB_URL, {"action": "timeline", "channel": "nope"}, HTTP_AUTHORIZATION="Bearer token"
        )
        self.assertEqual(response.status_code, 400)

    @authorized
    def test_returns_entries_with_injected_fields(self, _auth):
        response = self.client.get(
            MICROSUB_URL, {"action": "timeline", "channel": "news"}, HTTP_AUTHORIZATION="Bearer token"
        )
        self.assertEqual(response.status_code, 200)
        items = response.json()["items"]
        self.assertEqual(len(items), 2)
        for item in items:
            self.assertIn("_id", item)
            self.assertIn("_is_read", item)

    @authorized
    def test_removed_entries_excluded(self, _auth):
        self.e1.is_removed = True
        self.e1.save()
        response = self.client.get(
            MICROSUB_URL, {"action": "timeline", "channel": "news"}, HTTP_AUTHORIZATION="Bearer token"
        )
        items = response.json()["items"]
        ids = [i["_id"] for i in items]
        self.assertNotIn(str(self.e1.pk), ids)
        self.assertIn(str(self.e2.pk), ids)

    @authorized
    def test_site_wide_muted_author_excluded(self, _auth):
        MutedUser.objects.create(channel=None, url="https://author.example.com/")
        response = self.client.get(
            MICROSUB_URL, {"action": "timeline", "channel": "news"}, HTTP_AUTHORIZATION="Bearer token"
        )
        items = response.json()["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["_id"], str(self.e2.pk))

    @authorized
    def test_channel_specific_muted_author_excluded(self, _auth):
        MutedUser.objects.create(channel=self.channel, url="https://author.example.com/")
        response = self.client.get(
            MICROSUB_URL, {"action": "timeline", "channel": "news"}, HTTP_AUTHORIZATION="Bearer token"
        )
        items = response.json()["items"]
        self.assertEqual(len(items), 1)

    @authorized
    def test_blocked_author_excluded(self, _auth):
        BlockedUser.objects.create(channel=None, url="https://author.example.com/")
        response = self.client.get(
            MICROSUB_URL, {"action": "timeline", "channel": "news"}, HTTP_AUTHORIZATION="Bearer token"
        )
        items = response.json()["items"]
        self.assertEqual(len(items), 1)

    @authorized
    def test_before_cursor_filters_by_published_gt(self, _auth):
        # "before" cursor is the entry PK of the newest entry seen;
        # the response should contain entries newer than that entry (for polling).
        response = self.client.get(
            MICROSUB_URL,
            {"action": "timeline", "channel": "news", "before": str(self.e1.pk)},
            HTTP_AUTHORIZATION="Bearer token",
        )
        items = response.json()["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["_id"], str(self.e2.pk))

    @authorized
    def test_after_cursor_filters_by_published_lt(self, _auth):
        # "after" cursor is the entry PK of the oldest entry on the current page;
        # the response should contain entries older than that entry (next page).
        response = self.client.get(
            MICROSUB_URL,
            {"action": "timeline", "channel": "news", "after": str(self.e2.pk)},
            HTTP_AUTHORIZATION="Bearer token",
        )
        items = response.json()["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["_id"], str(self.e1.pk))

    @authorized
    def test_invalid_cursor_is_ignored(self, _auth):
        response = self.client.get(
            MICROSUB_URL,
            {"action": "timeline", "channel": "news", "before": "not-an-integer"},
            HTTP_AUTHORIZATION="Bearer token",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["items"]), 2)

    @authorized
    def test_paging_keys_present_when_entries_exist(self, _auth):
        response = self.client.get(
            MICROSUB_URL, {"action": "timeline", "channel": "news"}, HTTP_AUTHORIZATION="Bearer token"
        )
        paging = response.json()["paging"]
        # "before" is always present when entries exist; "after" only when has_more=True.
        self.assertIn("before", paging)

    @authorized
    def test_paging_empty_when_no_entries(self, _auth):
        Entry.objects.all().delete()
        response = self.client.get(
            MICROSUB_URL, {"action": "timeline", "channel": "news"}, HTTP_AUTHORIZATION="Bearer token"
        )
        self.assertEqual(response.json()["paging"], {})

    @authorized
    def test_paging_before_is_newest_entry_pk(self, _auth):
        # Items ordered newest-first: e2 (newer) then e1 (older).
        # "before" cursor should be the PK of the newest entry so clients can poll for new entries.
        response = self.client.get(
            MICROSUB_URL, {"action": "timeline", "channel": "news"}, HTTP_AUTHORIZATION="Bearer token"
        )
        paging = response.json()["paging"]
        self.assertEqual(paging["before"], str(self.e2.pk))

    @authorized
    def test_paging_after_is_oldest_entry_pk_on_nonfinal_page(self, _auth):
        # "after" cursor should be the PK of the oldest entry on the page when has_more=True.
        from microsub.views import PAGE_SIZE
        now = timezone.now()
        # Add enough entries to push e1/e2 off the first page entirely, ensuring has_more=True.
        for i in range(PAGE_SIZE):
            Entry.objects.create(
                channel=self.channel,
                uid=f"newer-{i}",
                data={},
                published=now + datetime.timedelta(hours=i + 1),
            )
        response = self.client.get(
            MICROSUB_URL, {"action": "timeline", "channel": "news"}, HTTP_AUTHORIZATION="Bearer token"
        )
        paging = response.json()["paging"]
        self.assertIn("after", paging)
        items = response.json()["items"]
        # "after" must equal the PK of the last (oldest) item on the page.
        self.assertEqual(paging["after"], items[-1]["_id"])

    @authorized
    def test_paging_after_cursor_fetches_next_older_page(self, _auth):
        # Requesting ?after=<paging.after> must return entries OLDER than the current page.
        now = timezone.now()
        # Create PAGE_SIZE + 2 entries so there are multiple pages.
        from microsub.views import PAGE_SIZE
        older_entries = []
        for i in range(PAGE_SIZE):
            e = Entry.objects.create(
                channel=self.channel,
                uid=f"old-{i}",
                data={},
                published=now - datetime.timedelta(hours=i + 1),
            )
            older_entries.append(e)

        # Get the first page (newest entries).
        page1 = self.client.get(
            MICROSUB_URL, {"action": "timeline", "channel": "news"}, HTTP_AUTHORIZATION="Bearer token"
        ).json()
        after_cursor = page1["paging"]["after"]
        page1_ids = {item["_id"] for item in page1["items"]}

        # Get the second page using the after cursor.
        page2 = self.client.get(
            MICROSUB_URL,
            {"action": "timeline", "channel": "news", "after": after_cursor},
            HTTP_AUTHORIZATION="Bearer token",
        ).json()
        page2_ids = {item["_id"] for item in page2["items"]}

        # Pages must not overlap.
        self.assertEqual(page1_ids & page2_ids, set(), "Pages overlap — paging cursor is wrong")


class GetMuteTests(TestCase):
    def setUp(self):
        self.channel = Channel.objects.create(uid="news", name="News")

    @patch("micropub.views._authorized", return_value=(True, ["read"]))
    def test_missing_mute_scope_returns_403(self, _auth):
        response = self.client.get(
            MICROSUB_URL, {"action": "mute"}, HTTP_AUTHORIZATION="Bearer token"
        )
        self.assertEqual(response.status_code, 403)

    @authorized
    def test_returns_site_wide_mutes_when_no_channel(self, _auth):
        MutedUser.objects.create(channel=None, url="https://spammer.example.com/")
        MutedUser.objects.create(channel=self.channel, url="https://other.example.com/")
        response = self.client.get(
            MICROSUB_URL, {"action": "mute"}, HTTP_AUTHORIZATION="Bearer token"
        )
        items = response.json()["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["url"], "https://spammer.example.com/")

    @authorized
    def test_returns_channel_mutes_when_channel_given(self, _auth):
        MutedUser.objects.create(channel=None, url="https://spammer.example.com/")
        MutedUser.objects.create(channel=self.channel, url="https://other.example.com/")
        response = self.client.get(
            MICROSUB_URL, {"action": "mute", "channel": "news"}, HTTP_AUTHORIZATION="Bearer token"
        )
        items = response.json()["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["url"], "https://other.example.com/")

    @authorized
    def test_unknown_channel_returns_400(self, _auth):
        response = self.client.get(
            MICROSUB_URL, {"action": "mute", "channel": "nonexistent"}, HTTP_AUTHORIZATION="Bearer token"
        )
        self.assertEqual(response.status_code, 400)


class GetBlockTests(TestCase):
    def setUp(self):
        self.channel = Channel.objects.create(uid="news", name="News")

    @patch("micropub.views._authorized", return_value=(True, ["read"]))
    def test_missing_block_scope_returns_403(self, _auth):
        response = self.client.get(
            MICROSUB_URL, {"action": "block"}, HTTP_AUTHORIZATION="Bearer token"
        )
        self.assertEqual(response.status_code, 403)

    @authorized
    def test_returns_site_wide_blocks(self, _auth):
        BlockedUser.objects.create(channel=None, url="https://troll.example.com/")
        response = self.client.get(
            MICROSUB_URL, {"action": "block"}, HTTP_AUTHORIZATION="Bearer token"
        )
        self.assertEqual(len(response.json()["items"]), 1)

    @authorized
    def test_returns_channel_blocks(self, _auth):
        BlockedUser.objects.create(channel=self.channel, url="https://troll.example.com/")
        response = self.client.get(
            MICROSUB_URL, {"action": "block", "channel": "news"}, HTTP_AUTHORIZATION="Bearer token"
        )
        self.assertEqual(len(response.json()["items"]), 1)

    @authorized
    def test_unknown_channel_returns_400(self, _auth):
        response = self.client.get(
            MICROSUB_URL, {"action": "block", "channel": "nonexistent"}, HTTP_AUTHORIZATION="Bearer token"
        )
        self.assertEqual(response.status_code, 400)


class GetSearchTests(TestCase):
    @authorized
    def test_empty_query_returns_empty_results(self, _auth):
        response = self.client.get(
            MICROSUB_URL, {"action": "search", "query": ""}, HTTP_AUTHORIZATION="Bearer token"
        )
        self.assertEqual(response.json(), {"results": []})

    @authorized
    def test_missing_query_returns_empty_results(self, _auth):
        response = self.client.get(
            MICROSUB_URL, {"action": "search"}, HTTP_AUTHORIZATION="Bearer token"
        )
        self.assertEqual(response.json(), {"results": []})

    @authorized
    @patch("microsub.views.fetch_and_parse_feed", return_value=([], None, {}))
    @patch("microsub.views.urlopen")
    def test_non_http_query_prepends_https(self, mock_urlopen, _mock_fetch, _auth):
        from unittest.mock import MagicMock

        mock_resp = MagicMock()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.headers.get.side_effect = lambda k, d="": "application/rss+xml" if k == "Content-Type" else d
        mock_resp.read.return_value = b""
        mock_urlopen.return_value = mock_resp

        self.client.get(
            MICROSUB_URL, {"action": "search", "query": "example.com"}, HTTP_AUTHORIZATION="Bearer token"
        )
        call_args = mock_urlopen.call_args
        req_obj = call_args[0][0]
        self.assertTrue(req_obj.full_url.startswith("https://"))

    @authorized
    @patch("microsub.views.fetch_and_parse_feed", return_value=([], None, {"name": "Feed"}))
    @patch("microsub.views.urlopen")
    def test_network_error_returns_empty_results(self, mock_urlopen, _mock_fetch, _auth):
        from urllib.error import URLError
        mock_urlopen.side_effect = URLError("connection refused")
        response = self.client.get(
            MICROSUB_URL,
            {"action": "search", "query": "https://example.com"},
            HTTP_AUTHORIZATION="Bearer token",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"results": []})

    @authorized
    @patch("microsub.views.fetch_and_parse_feed", return_value=([], None, {"name": "Feed"}))
    @patch("microsub.views.urlopen")
    def test_non_html_response_returns_url_as_feed(self, mock_urlopen, _mock_fetch, _auth):
        from unittest.mock import MagicMock

        mock_resp = MagicMock()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.headers.get.side_effect = lambda k, d="": "application/rss+xml" if k == "Content-Type" else d
        mock_resp.read.return_value = b"<rss/>"
        mock_urlopen.return_value = mock_resp

        response = self.client.get(
            MICROSUB_URL,
            {"action": "search", "query": "https://example.com/feed"},
            HTTP_AUTHORIZATION="Bearer token",
        )
        results = response.json()["results"]
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["url"], "https://example.com/feed")

    @authorized
    @patch("microsub.views.fetch_and_parse_feed")
    @patch("microsub.views.urlopen")
    def test_html_response_discovers_alternate_feeds(self, mock_urlopen, mock_fetch, _auth):
        from unittest.mock import MagicMock

        html = b'<html><head><link rel="alternate" type="application/rss+xml" href="/feed.rss" title="RSS Feed"></head></html>'
        mock_resp = MagicMock()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.headers.get.side_effect = lambda k, d="": "text/html; charset=utf-8" if k == "Content-Type" else d
        mock_resp.read.return_value = html
        mock_urlopen.return_value = mock_resp
        mock_fetch.side_effect = [
            ([], None, {}),
            ([], None, {"name": "RSS Feed"}),
        ]

        response = self.client.get(
            MICROSUB_URL,
            {"action": "search", "query": "https://example.com"},
            HTTP_AUTHORIZATION="Bearer token",
        )
        results = response.json()["results"]
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["type"], "feed")
        self.assertEqual(results[0]["url"], "https://example.com/feed.rss")


class GetPreviewTests(TestCase):
    @authorized
    def test_missing_url_returns_400(self, _auth):
        response = self.client.get(
            MICROSUB_URL, {"action": "preview"}, HTTP_AUTHORIZATION="Bearer token"
        )
        self.assertEqual(response.status_code, 400)

    @authorized
    @patch("microsub.views.fetch_and_parse_feed", return_value=([{"type": "entry"}], None, {}))
    def test_returns_items_on_success(self, mock_fetch, _auth):
        response = self.client.get(
            MICROSUB_URL,
            {"action": "preview", "url": "https://example.com/feed"},
            HTTP_AUTHORIZATION="Bearer token",
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("items", response.json())

    @authorized
    @patch("microsub.views.fetch_and_parse_feed", return_value=([{"type": "entry"}] * 25, None, {}))
    def test_returns_at_most_20_items(self, mock_fetch, _auth):
        response = self.client.get(
            MICROSUB_URL,
            {"action": "preview", "url": "https://example.com/feed"},
            HTTP_AUTHORIZATION="Bearer token",
        )
        self.assertLessEqual(len(response.json()["items"]), 20)

    @authorized
    @patch("microsub.views.fetch_and_parse_feed", side_effect=RuntimeError("could not fetch"))
    def test_fetch_error_returns_502(self, mock_fetch, _auth):
        response = self.client.get(
            MICROSUB_URL,
            {"action": "preview", "url": "https://example.com/feed"},
            HTTP_AUTHORIZATION="Bearer token",
        )
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["error"], "fetch_error")


class GetTimelineDefaultChannelTests(TestCase):
    def setUp(self):
        self.home = Channel.objects.get(uid="home")
        self.entry = Entry.objects.create(
            channel=self.home,
            uid="home-entry",
            data={"type": "entry"},
            published=timezone.now(),
        )

    @authorized
    def test_missing_channel_defaults_to_home(self, _auth):
        response = self.client.get(
            MICROSUB_URL,
            {"action": "timeline"},
            HTTP_AUTHORIZATION="Bearer token",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual([item["uid"] for item in response.json()["items"]], ["home-entry"])


class GetMuteBlockGlobalTests(TestCase):
    def setUp(self):
        self.channel = Channel.objects.create(uid="news", name="News")

    @authorized
    def test_get_mute_accepts_channel_global(self, _auth):
        MutedUser.objects.create(channel=None, url="https://muted.example/")
        response = self.client.get(
            MICROSUB_URL,
            {"action": "mute", "channel": "global"},
            HTTP_AUTHORIZATION="Bearer token",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["items"], [{"type": "card", "url": "https://muted.example/"}])

    @authorized
    def test_get_block_accepts_channel_global(self, _auth):
        BlockedUser.objects.create(channel=None, url="https://blocked.example/")
        response = self.client.get(
            MICROSUB_URL,
            {"action": "block", "channel": "global"},
            HTTP_AUTHORIZATION="Bearer token",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["items"], [{"type": "card", "url": "https://blocked.example/"}])


class DiscoveryTests(TestCase):
    """Fix 2 — Endpoint discovery via HTML link tag and HTTP Link header."""

    def test_homepage_contains_microsub_link_tag(self):
        response = self.client.get("/")
        self.assertContains(response, 'rel="microsub"')

    def test_homepage_link_header_contains_microsub_rel(self):
        response = self.client.get("/")
        link_header = response.get("Link", "")
        self.assertIn('rel="microsub"', link_header)


class GetTimelineSourceFilterTests(TestCase):
    """Fix 5 — source parameter filtering on timeline."""

    def setUp(self):
        self.channel = Channel.objects.create(uid="news", name="News")

    @authorized
    def test_source_filter_returns_only_matching_entries(self, _auth):
        sub_a = Subscription.objects.create(channel=self.channel, url="https://a.example.com/")
        sub_b = Subscription.objects.create(channel=self.channel, url="https://b.example.com/")
        now = timezone.now()
        Entry.objects.create(
            channel=self.channel, uid="sa1", data={}, published=now,
            subscription=sub_a,
        )
        Entry.objects.create(
            channel=self.channel, uid="sb1", data={}, published=now,
            subscription=sub_b,
        )
        response = self.client.get(
            MICROSUB_URL,
            {"action": "timeline", "channel": "news", "source": "https://a.example.com/"},
            HTTP_AUTHORIZATION="Bearer token",
        )
        self.assertEqual(response.status_code, 200)
        items = response.json()["items"]
        self.assertEqual(len(items), 1)

    @authorized
    def test_source_filter_unknown_source_returns_empty(self, _auth):
        response = self.client.get(
            MICROSUB_URL,
            {"action": "timeline", "channel": "news", "source": "https://unknown.example.com/"},
            HTTP_AUTHORIZATION="Bearer token",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["items"], [])


class GetTimelineMuteDbLevelTests(TestCase):
    """Fix 7 — mute/block filtering at DB level does not truncate pages."""

    def setUp(self):
        self.channel = Channel.objects.create(uid="news", name="News")

    @authorized
    def test_muted_author_exclusion_does_not_truncate_page(self, _auth):
        """Muted entries should be excluded at DB level, not shrink the page."""
        from microsub.views import PAGE_SIZE
        MutedUser.objects.create(channel=None, url="https://muted.example.com/")
        now = timezone.now()
        # Create PAGE_SIZE entries from an innocent author
        for i in range(PAGE_SIZE):
            Entry.objects.create(
                channel=self.channel,
                uid=f"innocent-{i}",
                data={"author": {"url": "https://innocent.example.com/"}},
                author_url="https://innocent.example.com/",
                published=now - datetime.timedelta(seconds=i),
            )
        # Create some entries from the muted author
        for i in range(5):
            Entry.objects.create(
                channel=self.channel,
                uid=f"muted-{i}",
                data={"author": {"url": "https://muted.example.com/"}},
                author_url="https://muted.example.com/",
                published=now - datetime.timedelta(seconds=i + 100),
            )
        response = self.client.get(
            MICROSUB_URL,
            {"action": "timeline", "channel": "news"},
            HTTP_AUTHORIZATION="Bearer token",
        )
        items = response.json()["items"]
        self.assertEqual(len(items), PAGE_SIZE)


class GetTimelineUnreadFilterTests(TestCase):
    """filter=unread parameter only returns entries with is_read=False."""

    def setUp(self):
        self.channel = Channel.objects.create(uid="news", name="News")
        now = timezone.now()
        self.unread = Entry.objects.create(
            channel=self.channel, uid="u1", data={}, published=now, is_read=False,
        )
        self.read = Entry.objects.create(
            channel=self.channel, uid="r1", data={}, published=now + datetime.timedelta(seconds=1), is_read=True,
        )

    @authorized
    def test_filter_unread_returns_only_unread_entries(self, _auth):
        response = self.client.get(
            MICROSUB_URL,
            {"action": "timeline", "channel": "news", "filter": "unread"},
            HTTP_AUTHORIZATION="Bearer token",
        )
        self.assertEqual(response.status_code, 200)
        ids = [item["_id"] for item in response.json()["items"]]
        self.assertIn(str(self.unread.pk), ids)

    @authorized
    def test_filter_unread_excludes_read_entries(self, _auth):
        response = self.client.get(
            MICROSUB_URL,
            {"action": "timeline", "channel": "news", "filter": "unread"},
            HTTP_AUTHORIZATION="Bearer token",
        )
        ids = [item["_id"] for item in response.json()["items"]]
        self.assertNotIn(str(self.read.pk), ids)

    @authorized
    def test_no_filter_returns_all_entries(self, _auth):
        response = self.client.get(
            MICROSUB_URL,
            {"action": "timeline", "channel": "news"},
            HTTP_AUTHORIZATION="Bearer token",
        )
        ids = [item["_id"] for item in response.json()["items"]]
        self.assertIn(str(self.unread.pk), ids)
        self.assertIn(str(self.read.pk), ids)


class GetTimelinePagingBehaviorTests(TestCase):
    """paging.after is only present when there are more older entries."""

    def setUp(self):
        self.channel = Channel.objects.create(uid="news", name="News")

    @authorized
    def test_paging_after_absent_on_last_page(self, _auth):
        now = timezone.now()
        Entry.objects.create(channel=self.channel, uid="only", data={}, published=now)
        response = self.client.get(
            MICROSUB_URL, {"action": "timeline", "channel": "news"}, HTTP_AUTHORIZATION="Bearer token"
        )
        paging = response.json()["paging"]
        self.assertNotIn("after", paging)

    @authorized
    def test_paging_after_present_on_nonfinal_page(self, _auth):
        from microsub.views import PAGE_SIZE
        now = timezone.now()
        for i in range(PAGE_SIZE + 1):
            Entry.objects.create(
                channel=self.channel,
                uid=f"e{i}",
                data={},
                published=now - datetime.timedelta(seconds=i),
            )
        response = self.client.get(
            MICROSUB_URL, {"action": "timeline", "channel": "news"}, HTTP_AUTHORIZATION="Bearer token"
        )
        paging = response.json()["paging"]
        self.assertIn("after", paging)


class EntryJsonWithoutSubscriptionTests(TestCase):
    """_entry_json does not crash when entry has no subscription (e.g. Webmention entries)."""

    def setUp(self):
        self.channel, _ = Channel.objects.get_or_create(uid="notifications", defaults={"name": "Notifications"})

    @authorized
    def test_entry_without_subscription_has_no_source(self, _auth):
        Entry.objects.create(
            channel=self.channel,
            uid="wm1",
            data={"type": "entry", "name": "Someone mentioned you"},
            published=timezone.now(),
            subscription=None,
        )
        response = self.client.get(
            MICROSUB_URL,
            {"action": "timeline", "channel": "notifications"},
            HTTP_AUTHORIZATION="Bearer token",
        )
        self.assertEqual(response.status_code, 200)
        items = response.json()["items"]
        self.assertEqual(len(items), 1)
        self.assertNotIn("_source", items[0])
