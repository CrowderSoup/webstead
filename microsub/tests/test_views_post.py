"""Tests for MicrosubView POST handlers."""
import datetime
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from microsub.models import BlockedUser, Channel, Entry, MutedUser, Subscription

MICROSUB_URL = "/microsub"
ALL_SCOPES = ["read", "follow", "channels", "mute", "block"]
authorized = patch("micropub.views._authorized", return_value=(True, ALL_SCOPES))


class PostChannelsTests(TestCase):
    def setUp(self):
        self.channel = Channel.objects.create(uid="news", name="News", order=1)

    @patch("micropub.views._authorized", return_value=(True, ["read"]))
    def test_missing_channels_scope_returns_403(self, _auth):
        response = self.client.post(
            MICROSUB_URL, {"action": "channels", "name": "New"},
            HTTP_AUTHORIZATION="Bearer token",
        )
        self.assertEqual(response.status_code, 403)

    @authorized
    def test_delete_channel(self, _auth):
        response = self.client.post(
            MICROSUB_URL,
            {"action": "channels", "method": "delete", "channel": "news"},
            HTTP_AUTHORIZATION="Bearer token",
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Channel.objects.filter(uid="news").exists())

    @authorized
    def test_delete_unknown_channel_returns_400(self, _auth):
        response = self.client.post(
            MICROSUB_URL,
            {"action": "channels", "method": "delete", "channel": "nope"},
            HTTP_AUTHORIZATION="Bearer token",
        )
        self.assertEqual(response.status_code, 400)

    @authorized
    def test_delete_notifications_channel_returns_403(self, _auth):
        # Seed migration already creates this channel; ensure it exists
        Channel.objects.get_or_create(uid="notifications", defaults={"name": "Notifications"})
        response = self.client.post(
            MICROSUB_URL,
            {"action": "channels", "method": "delete", "channel": "notifications"},
            HTTP_AUTHORIZATION="Bearer token",
        )
        self.assertEqual(response.status_code, 403)
        self.assertTrue(Channel.objects.filter(uid="notifications").exists())

    @authorized
    def test_order_reorders_channels(self, _auth):
        ch2 = Channel.objects.create(uid="tech", name="Tech", order=2)
        response = self.client.post(
            MICROSUB_URL,
            {"action": "channels", "method": "order", "channels[]": ["tech", "news"]},
            HTTP_AUTHORIZATION="Bearer token",
        )
        self.assertEqual(response.status_code, 200)
        ordered = list(Channel.objects.order_by("order", "id").values_list("uid", flat=True))
        self.assertEqual(ordered[0], "notifications")
        self.assertEqual(ordered[1], "home")
        self.assertLess(ordered.index("tech"), ordered.index("news"))

    @authorized
    def test_rename_channel(self, _auth):
        response = self.client.post(
            MICROSUB_URL,
            {"action": "channels", "channel": "news", "name": "World News"},
            HTTP_AUTHORIZATION="Bearer token",
        )
        self.assertEqual(response.status_code, 200)
        self.channel.refresh_from_db()
        self.assertEqual(self.channel.name, "World News")

    @authorized
    def test_rename_unknown_channel_returns_400(self, _auth):
        response = self.client.post(
            MICROSUB_URL,
            {"action": "channels", "channel": "nope", "name": "Whatever"},
            HTTP_AUTHORIZATION="Bearer token",
        )
        self.assertEqual(response.status_code, 400)

    @authorized
    def test_create_channel(self, _auth):
        response = self.client.post(
            MICROSUB_URL,
            {"action": "channels", "name": "Tech Feeds"},
            HTTP_AUTHORIZATION="Bearer token",
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(Channel.objects.filter(name="Tech Feeds").exists())

    @authorized
    def test_create_channel_uid_derived_from_name(self, _auth):
        self.client.post(
            MICROSUB_URL,
            {"action": "channels", "name": "Hello World"},
            HTTP_AUTHORIZATION="Bearer token",
        )
        self.assertTrue(Channel.objects.filter(uid="hello-world").exists())

    @authorized
    def test_create_channel_uid_collision_adds_suffix(self, _auth):
        Channel.objects.create(uid="tech", name="Tech")
        response = self.client.post(
            MICROSUB_URL,
            {"action": "channels", "name": "Tech"},
            HTTP_AUTHORIZATION="Bearer token",
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(Channel.objects.filter(uid="tech-1").exists())

    @authorized
    def test_create_channel_order_is_max_plus_one(self, _auth):
        self.client.post(
            MICROSUB_URL,
            {"action": "channels", "name": "New Channel"},
            HTTP_AUTHORIZATION="Bearer token",
        )
        ch = Channel.objects.get(name="New Channel")
        self.assertEqual(ch.order, 2)  # existing max is 1

    @authorized
    def test_no_method_no_name_returns_400(self, _auth):
        response = self.client.post(
            MICROSUB_URL, {"action": "channels"}, HTTP_AUTHORIZATION="Bearer token"
        )
        self.assertEqual(response.status_code, 400)

    @authorized
    def test_create_reserved_global_channel_returns_400(self, _auth):
        response = self.client.post(
            MICROSUB_URL,
            {"action": "channels", "name": "Global"},
            HTTP_AUTHORIZATION="Bearer token",
        )
        self.assertEqual(response.status_code, 400)

    @authorized
    def test_delete_last_non_notifications_channel_returns_403(self, _auth):
        Channel.objects.exclude(uid__in=["notifications", "home", "news"]).delete()
        Channel.objects.filter(uid="home").delete()
        response = self.client.post(
            MICROSUB_URL,
            {"action": "channels", "method": "delete", "channel": "news"},
            HTTP_AUTHORIZATION="Bearer token",
        )
        self.assertEqual(response.status_code, 403)


class PostFollowTests(TestCase):
    def setUp(self):
        self.channel = Channel.objects.create(uid="news", name="News")

    @patch("micropub.views._authorized", return_value=(True, ["read"]))
    def test_missing_follow_scope_returns_403(self, _auth):
        response = self.client.post(
            MICROSUB_URL,
            {"action": "follow", "channel": "news", "url": "https://example.com/feed"},
            HTTP_AUTHORIZATION="Bearer token",
        )
        self.assertEqual(response.status_code, 403)

    @authorized
    def test_missing_url_returns_400(self, _auth):
        response = self.client.post(
            MICROSUB_URL, {"action": "follow", "channel": "news"}, HTTP_AUTHORIZATION="Bearer token"
        )
        self.assertEqual(response.status_code, 400)

    @authorized
    def test_missing_channel_returns_400(self, _auth):
        response = self.client.post(
            MICROSUB_URL,
            {"action": "follow", "url": "https://example.com/feed"},
            HTTP_AUTHORIZATION="Bearer token",
        )
        self.assertEqual(response.status_code, 400)

    @authorized
    def test_unknown_channel_returns_400(self, _auth):
        response = self.client.post(
            MICROSUB_URL,
            {"action": "follow", "channel": "nope", "url": "https://example.com/feed"},
            HTTP_AUTHORIZATION="Bearer token",
        )
        self.assertEqual(response.status_code, 400)

    @authorized
    def test_creates_new_subscription(self, _auth):
        response = self.client.post(
            MICROSUB_URL,
            {"action": "follow", "channel": "news", "url": "https://example.com/feed"},
            HTTP_AUTHORIZATION="Bearer token",
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(Subscription.objects.filter(url="https://example.com/feed").exists())

    @authorized
    def test_reactivates_inactive_subscription(self, _auth):
        Subscription.objects.create(channel=self.channel, url="https://example.com/feed", is_active=False)
        self.client.post(
            MICROSUB_URL,
            {"action": "follow", "channel": "news", "url": "https://example.com/feed"},
            HTTP_AUTHORIZATION="Bearer token",
        )
        sub = Subscription.objects.get(url="https://example.com/feed")
        self.assertTrue(sub.is_active)

    @authorized
    @patch("microsub.views._subscribe_to_websub_with_base_url")
    @patch("microsub.feed_parser.fetch_and_parse_feed", return_value=([], "https://hub.example.com/", {}))
    def test_hub_discovered_and_stored(self, mock_fetch, mock_websub, _auth):
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(
                MICROSUB_URL,
                {"action": "follow", "channel": "news", "url": "https://example.com/feed"},
                HTTP_AUTHORIZATION="Bearer token",
            )
        sub = Subscription.objects.get(url="https://example.com/feed")
        self.assertEqual(sub.websub_hub, "https://hub.example.com/")
        mock_websub.assert_called_once()

    @authorized
    def test_feed_discovery_error_still_creates_subscription(self, _auth):
        response = self.client.post(
            MICROSUB_URL,
            {"action": "follow", "channel": "news", "url": "https://example.com/feed"},
            HTTP_AUTHORIZATION="Bearer token",
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(Subscription.objects.filter(url="https://example.com/feed").exists())

    @authorized
    def test_response_has_feed_shape(self, _auth):
        response = self.client.post(
            MICROSUB_URL,
            {"action": "follow", "channel": "news", "url": "https://example.com/feed"},
            HTTP_AUTHORIZATION="Bearer token",
        )
        data = response.json()
        self.assertEqual(data["type"], "feed")
        self.assertIn("url", data)
        self.assertIn("name", data)
        self.assertIn("photo", data)


class PostUnfollowTests(TestCase):
    def setUp(self):
        self.channel = Channel.objects.create(uid="news", name="News")
        self.sub = Subscription.objects.create(channel=self.channel, url="https://example.com/feed")

    @patch("micropub.views._authorized", return_value=(True, ["read"]))
    def test_missing_follow_scope_returns_403(self, _auth):
        response = self.client.post(
            MICROSUB_URL,
            {"action": "unfollow", "channel": "news", "url": "https://example.com/feed"},
            HTTP_AUTHORIZATION="Bearer token",
        )
        self.assertEqual(response.status_code, 403)

    @authorized
    def test_missing_params_returns_400(self, _auth):
        response = self.client.post(
            MICROSUB_URL, {"action": "unfollow", "channel": "news"}, HTTP_AUTHORIZATION="Bearer token"
        )
        self.assertEqual(response.status_code, 400)

    @authorized
    def test_unknown_channel_returns_400(self, _auth):
        response = self.client.post(
            MICROSUB_URL,
            {"action": "unfollow", "channel": "nope", "url": "https://example.com/feed"},
            HTTP_AUTHORIZATION="Bearer token",
        )
        self.assertEqual(response.status_code, 400)

    @authorized
    def test_deactivates_subscription(self, _auth):
        response = self.client.post(
            MICROSUB_URL,
            {"action": "unfollow", "channel": "news", "url": "https://example.com/feed"},
            HTTP_AUTHORIZATION="Bearer token",
        )
        self.assertEqual(response.status_code, 200)
        sub = Subscription.objects.get(url="https://example.com/feed")
        self.assertFalse(sub.is_active)

    @authorized
    def test_nonexistent_subscription_is_ok(self, _auth):
        response = self.client.post(
            MICROSUB_URL,
            {"action": "unfollow", "channel": "news", "url": "https://notsubscribed.example.com/"},
            HTTP_AUTHORIZATION="Bearer token",
        )
        self.assertEqual(response.status_code, 200)

    @authorized
    def test_managed_subscription_cannot_be_unfollowed_here(self, _auth):
        self.sub.managed_by = "mastodon"
        self.sub.managed_key = "timeline"
        self.sub.save(update_fields=["managed_by", "managed_key"])

        response = self.client.post(
            MICROSUB_URL,
            {"action": "unfollow", "channel": "news", "url": "https://example.com/feed"},
            HTTP_AUTHORIZATION="Bearer token",
        )

        self.assertEqual(response.status_code, 403)
        self.sub.refresh_from_db()
        self.assertTrue(self.sub.is_active)

    @authorized
    @patch("microsub.views._unsubscribe_from_websub")
    def test_unfollow_attempts_websub_unsubscribe(self, mock_unsubscribe, _auth):
        self.sub.websub_hub = "https://hub.example/"
        self.sub.websub_subscribed_at = timezone.now()
        self.sub.save(update_fields=["websub_hub", "websub_subscribed_at"])
        self.client.post(
            MICROSUB_URL,
            {"action": "unfollow", "channel": "news", "url": "https://example.com/feed"},
            HTTP_AUTHORIZATION="Bearer token",
        )
        mock_unsubscribe.assert_called_once()

    @authorized
    def test_unfollow_removes_entries_when_configured(self, _auth):
        from core.models import SiteConfiguration

        Entry.objects.create(
            channel=self.channel,
            subscription=self.sub,
            uid="entry-1",
            data={"type": "entry"},
            published=timezone.now(),
        )
        config = SiteConfiguration.get_solo()
        config.microsub_unfollow_removes_entries = True
        config.save(update_fields=["microsub_unfollow_removes_entries"])

        self.client.post(
            MICROSUB_URL,
            {"action": "unfollow", "channel": "news", "url": "https://example.com/feed"},
            HTTP_AUTHORIZATION="Bearer token",
        )
        self.assertTrue(Entry.objects.get(uid="entry-1").is_removed)


class PostTimelineTests(TestCase):
    def setUp(self):
        self.channel = Channel.objects.create(uid="news", name="News")
        now = timezone.now()
        self.e1 = Entry.objects.create(channel=self.channel, uid="e1", data={}, published=now)
        self.e2 = Entry.objects.create(
            channel=self.channel, uid="e2", data={},
            published=now + datetime.timedelta(seconds=1),
        )

    @patch("micropub.views._authorized", return_value=(True, []))
    def test_missing_read_scope_returns_403(self, _auth):
        response = self.client.post(
            MICROSUB_URL,
            {"action": "timeline", "channel": "news", "method": "mark_read"},
            HTTP_AUTHORIZATION="Bearer token",
        )
        self.assertEqual(response.status_code, 403)

    @authorized
    def test_missing_channel_returns_400(self, _auth):
        response = self.client.post(
            MICROSUB_URL,
            {"action": "timeline", "method": "mark_read"},
            HTTP_AUTHORIZATION="Bearer token",
        )
        self.assertEqual(response.status_code, 400)

    @authorized
    def test_unknown_channel_returns_400(self, _auth):
        response = self.client.post(
            MICROSUB_URL,
            {"action": "timeline", "channel": "nope", "method": "mark_read"},
            HTTP_AUTHORIZATION="Bearer token",
        )
        self.assertEqual(response.status_code, 400)

    @authorized
    def test_mark_read_with_last_read_entry(self, _auth):
        response = self.client.post(
            MICROSUB_URL,
            {"action": "timeline", "channel": "news", "method": "mark_read",
             "last_read_entry": str(self.e1.pk)},
            HTTP_AUTHORIZATION="Bearer token",
        )
        self.assertEqual(response.status_code, 200)
        self.e1.refresh_from_db()
        self.assertTrue(self.e1.is_read)
        self.e2.refresh_from_db()
        self.assertTrue(self.e2.is_read)

    @authorized
    def test_mark_read_with_entry_ids(self, _auth):
        response = self.client.post(
            MICROSUB_URL,
            {"action": "timeline", "channel": "news", "method": "mark_read",
             "entry[]": [str(self.e1.pk)]},
            HTTP_AUTHORIZATION="Bearer token",
        )
        self.assertEqual(response.status_code, 200)
        self.e1.refresh_from_db()
        self.assertTrue(self.e1.is_read)
        self.e2.refresh_from_db()
        self.assertFalse(self.e2.is_read)

    @authorized
    def test_mark_read_all_when_no_ids(self, _auth):
        response = self.client.post(
            MICROSUB_URL,
            {"action": "timeline", "channel": "news", "method": "mark_read"},
            HTTP_AUTHORIZATION="Bearer token",
        )
        self.assertEqual(response.status_code, 200)
        self.e1.refresh_from_db()
        self.e2.refresh_from_db()
        self.assertTrue(self.e1.is_read)
        self.assertTrue(self.e2.is_read)

    @authorized
    def test_mark_read_invalid_last_read_entry_is_ignored(self, _auth):
        response = self.client.post(
            MICROSUB_URL,
            {"action": "timeline", "channel": "news", "method": "mark_read",
             "last_read_entry": "not-an-int"},
            HTTP_AUTHORIZATION="Bearer token",
        )
        self.assertEqual(response.status_code, 200)
        self.e1.refresh_from_db()
        self.assertFalse(self.e1.is_read)

    @authorized
    def test_mark_read_non_integer_entry_ids_returns_400(self, _auth):
        response = self.client.post(
            MICROSUB_URL,
            {"action": "timeline", "channel": "news", "method": "mark_read",
             "entry[]": ["not-an-int"]},
            HTTP_AUTHORIZATION="Bearer token",
        )
        self.assertEqual(response.status_code, 400)

    @authorized
    def test_mark_unread_entries(self, _auth):
        self.e1.is_read = True
        self.e1.save()
        response = self.client.post(
            MICROSUB_URL,
            {"action": "timeline", "channel": "news", "method": "mark_unread",
             "entry[]": [str(self.e1.pk)]},
            HTTP_AUTHORIZATION="Bearer token",
        )
        self.assertEqual(response.status_code, 200)
        self.e1.refresh_from_db()
        self.assertFalse(self.e1.is_read)

    @authorized
    def test_remove_entries(self, _auth):
        response = self.client.post(
            MICROSUB_URL,
            {"action": "timeline", "channel": "news", "method": "remove",
             "entry[]": [str(self.e1.pk)]},
            HTTP_AUTHORIZATION="Bearer token",
        )
        self.assertEqual(response.status_code, 200)
        self.e1.refresh_from_db()
        self.assertTrue(self.e1.is_removed)

    @authorized
    def test_unknown_method_returns_400(self, _auth):
        response = self.client.post(
            MICROSUB_URL,
            {"action": "timeline", "channel": "news", "method": "bogus"},
            HTTP_AUTHORIZATION="Bearer token",
        )
        self.assertEqual(response.status_code, 400)


class PostMuteTests(TestCase):
    def setUp(self):
        self.channel = Channel.objects.create(uid="news", name="News")

    @patch("micropub.views._authorized", return_value=(True, ["read"]))
    def test_missing_mute_scope_returns_403(self, _auth):
        response = self.client.post(
            MICROSUB_URL,
            {"action": "mute", "url": "https://spammer.example.com/"},
            HTTP_AUTHORIZATION="Bearer token",
        )
        self.assertEqual(response.status_code, 403)

    @authorized
    def test_missing_url_returns_400(self, _auth):
        response = self.client.post(
            MICROSUB_URL, {"action": "mute"}, HTTP_AUTHORIZATION="Bearer token"
        )
        self.assertEqual(response.status_code, 400)

    @authorized
    def test_creates_site_wide_mute_without_channel(self, _auth):
        response = self.client.post(
            MICROSUB_URL,
            {"action": "mute", "url": "https://spammer.example.com/"},
            HTTP_AUTHORIZATION="Bearer token",
        )
        self.assertEqual(response.status_code, 200)
        m = MutedUser.objects.get(url="https://spammer.example.com/")
        self.assertIsNone(m.channel)

    @authorized
    def test_creates_channel_specific_mute(self, _auth):
        response = self.client.post(
            MICROSUB_URL,
            {"action": "mute", "url": "https://spammer.example.com/", "channel": "news"},
            HTTP_AUTHORIZATION="Bearer token",
        )
        self.assertEqual(response.status_code, 200)
        m = MutedUser.objects.get(url="https://spammer.example.com/")
        self.assertEqual(m.channel, self.channel)

    @authorized
    def test_unknown_channel_returns_400(self, _auth):
        response = self.client.post(
            MICROSUB_URL,
            {"action": "mute", "url": "https://spammer.example.com/", "channel": "nonexistent"},
            HTTP_AUTHORIZATION="Bearer token",
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(MutedUser.objects.filter(url="https://spammer.example.com/").exists())

    @authorized
    def test_duplicate_mute_is_idempotent(self, _auth):
        self.client.post(
            MICROSUB_URL,
            {"action": "mute", "url": "https://spammer.example.com/"},
            HTTP_AUTHORIZATION="Bearer token",
        )
        response = self.client.post(
            MICROSUB_URL,
            {"action": "mute", "url": "https://spammer.example.com/"},
            HTTP_AUTHORIZATION="Bearer token",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(MutedUser.objects.count(), 1)


class PostUnmuteTests(TestCase):
    def setUp(self):
        self.channel = Channel.objects.create(uid="news", name="News")

    @patch("micropub.views._authorized", return_value=(True, ["read"]))
    def test_missing_mute_scope_returns_403(self, _auth):
        response = self.client.post(
            MICROSUB_URL,
            {"action": "unmute", "url": "https://spammer.example.com/"},
            HTTP_AUTHORIZATION="Bearer token",
        )
        self.assertEqual(response.status_code, 403)

    @authorized
    def test_missing_url_returns_400(self, _auth):
        response = self.client.post(
            MICROSUB_URL, {"action": "unmute"}, HTTP_AUTHORIZATION="Bearer token"
        )
        self.assertEqual(response.status_code, 400)

    @authorized
    def test_deletes_muted_user(self, _auth):
        MutedUser.objects.create(channel=None, url="https://spammer.example.com/")
        response = self.client.post(
            MICROSUB_URL,
            {"action": "unmute", "url": "https://spammer.example.com/"},
            HTTP_AUTHORIZATION="Bearer token",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(MutedUser.objects.count(), 0)

    @authorized
    def test_nonexistent_mute_is_ok(self, _auth):
        response = self.client.post(
            MICROSUB_URL,
            {"action": "unmute", "url": "https://notmuted.example.com/"},
            HTTP_AUTHORIZATION="Bearer token",
        )
        self.assertEqual(response.status_code, 200)

    @authorized
    def test_unknown_channel_returns_400(self, _auth):
        response = self.client.post(
            MICROSUB_URL,
            {"action": "unmute", "url": "https://spammer.example.com/", "channel": "nonexistent"},
            HTTP_AUTHORIZATION="Bearer token",
        )
        self.assertEqual(response.status_code, 400)

    @authorized
    def test_unknown_channel_does_not_delete_site_wide_mute(self, _auth):
        MutedUser.objects.create(channel=None, url="https://spammer.example.com/")
        self.client.post(
            MICROSUB_URL,
            {"action": "unmute", "url": "https://spammer.example.com/", "channel": "nonexistent"},
            HTTP_AUTHORIZATION="Bearer token",
        )
        # The site-wide mute must survive; bad channel uid must not fall through to site-wide.
        self.assertEqual(MutedUser.objects.filter(url="https://spammer.example.com/").count(), 1)

    @authorized
    def test_channel_global_targets_sitewide_mute(self, _auth):
        MutedUser.objects.create(channel=None, url="https://spammer.example.com/")
        response = self.client.post(
            MICROSUB_URL,
            {"action": "unmute", "url": "https://spammer.example.com/", "channel": "global"},
            HTTP_AUTHORIZATION="Bearer token",
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(MutedUser.objects.filter(url="https://spammer.example.com/").exists())


class PostBlockTests(TestCase):
    def setUp(self):
        self.channel = Channel.objects.create(uid="news", name="News")

    @patch("micropub.views._authorized", return_value=(True, ["read"]))
    def test_missing_block_scope_returns_403(self, _auth):
        response = self.client.post(
            MICROSUB_URL,
            {"action": "block", "url": "https://troll.example.com/"},
            HTTP_AUTHORIZATION="Bearer token",
        )
        self.assertEqual(response.status_code, 403)

    @authorized
    def test_missing_url_returns_400(self, _auth):
        response = self.client.post(
            MICROSUB_URL, {"action": "block"}, HTTP_AUTHORIZATION="Bearer token"
        )
        self.assertEqual(response.status_code, 400)

    @authorized
    def test_creates_block(self, _auth):
        response = self.client.post(
            MICROSUB_URL,
            {"action": "block", "url": "https://troll.example.com/"},
            HTTP_AUTHORIZATION="Bearer token",
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(BlockedUser.objects.filter(url="https://troll.example.com/").exists())

    @authorized
    def test_existing_entries_from_blocked_author_marked_removed(self, _auth):
        Entry.objects.create(
            channel=self.channel,
            uid="e1",
            data={"type": "entry", "author": {"url": "https://troll.example.com/"}},
            author_url="https://troll.example.com/",
            published=timezone.now(),
        )
        self.client.post(
            MICROSUB_URL,
            {"action": "block", "url": "https://troll.example.com/", "channel": "news"},
            HTTP_AUTHORIZATION="Bearer token",
        )
        entry = Entry.objects.get(uid="e1")
        self.assertTrue(entry.is_removed)

    @authorized
    def test_entries_from_other_authors_not_removed(self, _auth):
        Entry.objects.create(
            channel=self.channel,
            uid="e1",
            data={"type": "entry", "author": {"url": "https://innocent.example.com/"}},
            published=timezone.now(),
        )
        self.client.post(
            MICROSUB_URL,
            {"action": "block", "url": "https://troll.example.com/", "channel": "news"},
            HTTP_AUTHORIZATION="Bearer token",
        )
        entry = Entry.objects.get(uid="e1")
        self.assertFalse(entry.is_removed)

    @authorized
    def test_unknown_channel_returns_400(self, _auth):
        response = self.client.post(
            MICROSUB_URL,
            {"action": "block", "url": "https://troll.example.com/", "channel": "nonexistent"},
            HTTP_AUTHORIZATION="Bearer token",
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(BlockedUser.objects.filter(url="https://troll.example.com/").exists())

    @authorized
    def test_site_wide_block_removes_entries_from_all_channels(self, _auth):
        ch2 = Channel.objects.create(uid="tech", name="Tech")
        Entry.objects.create(
            channel=self.channel,
            uid="e_news",
            data={},
            author_url="https://troll.example.com/",
            published=timezone.now(),
        )
        Entry.objects.create(
            channel=ch2,
            uid="e_tech",
            data={},
            author_url="https://troll.example.com/",
            published=timezone.now(),
        )
        self.client.post(
            MICROSUB_URL,
            {"action": "block", "url": "https://troll.example.com/"},
            HTTP_AUTHORIZATION="Bearer token",
        )
        self.assertTrue(Entry.objects.get(uid="e_news").is_removed)
        self.assertTrue(Entry.objects.get(uid="e_tech").is_removed)

    @authorized
    def test_channel_global_creates_sitewide_block(self, _auth):
        response = self.client.post(
            MICROSUB_URL,
            {"action": "block", "url": "https://troll.example.com/", "channel": "global"},
            HTTP_AUTHORIZATION="Bearer token",
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(BlockedUser.objects.filter(channel=None, url="https://troll.example.com/").exists())


class PostUnblockTests(TestCase):
    def setUp(self):
        self.channel = Channel.objects.create(uid="news", name="News")

    @patch("micropub.views._authorized", return_value=(True, ["read"]))
    def test_missing_block_scope_returns_403(self, _auth):
        response = self.client.post(
            MICROSUB_URL,
            {"action": "unblock", "url": "https://troll.example.com/"},
            HTTP_AUTHORIZATION="Bearer token",
        )
        self.assertEqual(response.status_code, 403)

    @authorized
    def test_missing_url_returns_400(self, _auth):
        response = self.client.post(
            MICROSUB_URL, {"action": "unblock"}, HTTP_AUTHORIZATION="Bearer token"
        )
        self.assertEqual(response.status_code, 400)

    @authorized
    def test_deletes_block(self, _auth):
        BlockedUser.objects.create(channel=None, url="https://troll.example.com/")
        response = self.client.post(
            MICROSUB_URL,
            {"action": "unblock", "url": "https://troll.example.com/"},
            HTTP_AUTHORIZATION="Bearer token",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(BlockedUser.objects.count(), 0)

    @authorized
    def test_nonexistent_block_is_ok(self, _auth):
        response = self.client.post(
            MICROSUB_URL,
            {"action": "unblock", "url": "https://notblocked.example.com/"},
            HTTP_AUTHORIZATION="Bearer token",
        )
        self.assertEqual(response.status_code, 200)

    @authorized
    def test_unknown_channel_returns_400(self, _auth):
        response = self.client.post(
            MICROSUB_URL,
            {"action": "unblock", "url": "https://troll.example.com/", "channel": "nonexistent"},
            HTTP_AUTHORIZATION="Bearer token",
        )
        self.assertEqual(response.status_code, 400)

    @authorized
    def test_unknown_channel_does_not_delete_site_wide_block(self, _auth):
        BlockedUser.objects.create(channel=None, url="https://troll.example.com/")
        self.client.post(
            MICROSUB_URL,
            {"action": "unblock", "url": "https://troll.example.com/", "channel": "nonexistent"},
            HTTP_AUTHORIZATION="Bearer token",
        )
        self.assertEqual(BlockedUser.objects.filter(url="https://troll.example.com/").count(), 1)

    @authorized
    def test_channel_global_deletes_sitewide_block(self, _auth):
        BlockedUser.objects.create(channel=None, url="https://troll.example.com/")
        response = self.client.post(
            MICROSUB_URL,
            {"action": "unblock", "url": "https://troll.example.com/", "channel": "global"},
            HTTP_AUTHORIZATION="Bearer token",
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(BlockedUser.objects.filter(url="https://troll.example.com/").exists())


class PostTimelineDefaultMethodTests(TestCase):
    """Fix 3 — omitting method defaults to mark_read per the spec."""

    def setUp(self):
        self.channel = Channel.objects.create(uid="news", name="News")
        now = timezone.now()
        self.e1 = Entry.objects.create(channel=self.channel, uid="e1", data={}, published=now)

    @authorized
    def test_mark_read_without_method_param(self, _auth):
        """Omitting 'method' should default to mark_read per the spec."""
        response = self.client.post(
            MICROSUB_URL,
            {"action": "timeline", "channel": "news", "entry[]": [str(self.e1.pk)]},
            HTTP_AUTHORIZATION="Bearer token",
        )
        self.assertEqual(response.status_code, 200)
        self.e1.refresh_from_db()
        self.assertTrue(self.e1.is_read)


class PostChannelsReorderTests(TestCase):
    """Fix 4 — reorder channels via channels[] without method=order."""

    def setUp(self):
        self.channel = Channel.objects.create(uid="news", name="News", order=1)

    @authorized
    def test_order_reorders_channels_without_method_param(self, _auth):
        """Spec-compliant reorder: channels[] with no method param."""
        ch2 = Channel.objects.create(uid="tech", name="Tech", order=2)
        response = self.client.post(
            MICROSUB_URL,
            {"action": "channels", "channels[]": ["tech", "news"]},
            HTTP_AUTHORIZATION="Bearer token",
        )
        self.assertEqual(response.status_code, 200)
        ordered = list(Channel.objects.order_by("order", "id").values_list("uid", flat=True))
        self.assertEqual(ordered[0], "notifications")
        self.assertEqual(ordered[1], "home")
        self.assertLess(ordered.index("tech"), ordered.index("news"))


class PostFollowInitialEntriesTests(TestCase):
    """Fix 10 — initial entries stored immediately on follow."""

    def setUp(self):
        self.channel = Channel.objects.create(uid="news", name="News")

    @authorized
    @patch("microsub.feed_parser.fetch_and_parse_feed", return_value=(
        [{"type": "entry", "url": "https://example.com/post/1",
          "_uid": "https://example.com/post/1", "published": "2024-01-01T00:00:00Z"}],
        None,
        {"name": "Example Feed", "photo": ""},
    ))
    def test_initial_entries_stored_on_follow(self, mock_fetch, _auth):
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                MICROSUB_URL,
                {"action": "follow", "channel": "news", "url": "https://example.com/feed"},
                HTTP_AUTHORIZATION="Bearer token",
            )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(Entry.objects.filter(channel=self.channel).exists())
