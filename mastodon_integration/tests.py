from datetime import timedelta
from unittest.mock import MagicMock, patch

from django.test import TestCase
from django.utils import timezone

from blog.models import Post
from core.models import SiteConfiguration
from mastodon_integration.client import status_to_jf2
from mastodon_integration.models import MastodonAccount, MastodonApp
from mastodon_integration.subscriptions import ensure_managed_subscription, sync_managed_subscriptions
from mastodon_integration.tasks import _build_canonical_url, poll_mastodon_notifications, poll_mastodon_timeline
from microsub.models import Channel, Entry
from microsub.views import _entry_json


class StatusToJf2Tests(TestCase):
    def test_in_reply_to_url_is_preserved(self):
        status = {
            "id": "1",
            "content": "<p>Hello</p>",
            "url": "https://social.example/@alice/1",
            "created_at": timezone.now(),
            "in_reply_to_url": "https://social.example/@alice/0",
            "account": {
                "id": "10",
                "display_name": "Alice",
                "username": "alice",
                "url": "https://social.example/@alice",
                "avatar": "https://social.example/media/alice.jpg",
            },
        }

        jf2 = status_to_jf2(status)

        self.assertEqual(jf2["in-reply-to"], ["https://social.example/@alice/0"])


class PollMastodonTimelineReplyFilterTests(TestCase):
    def setUp(self):
        self.channel = Channel.objects.create(uid="news", name="News")
        self.app = MastodonApp.objects.create(
            instance_url="https://social.example",
            client_id="client-id",
            client_secret="client-secret",
        )
        self.account = MastodonAccount.objects.create(
            app=self.app,
            access_token="access-token",
            account_id="10",
            username="alice@social.example",
            timeline_channel=self.channel,
            timeline_reply_filter=MastodonAccount.TIMELINE_REPLIES_ALL,
        )

    def _status(self, status_id: str, *, author_id: str = "10", reply_to_id=None, reply_to_account_id=None):
        created_at = timezone.now() + timedelta(seconds=int(status_id))
        return {
            "id": status_id,
            "content": "<p>Hello</p>",
            "url": f"https://social.example/@alice/{status_id}",
            "created_at": created_at,
            "in_reply_to_id": reply_to_id,
            "in_reply_to_account_id": reply_to_account_id,
            "account": {
                "id": author_id,
                "display_name": "Alice",
                "username": "alice",
                "url": "https://social.example/@alice",
                "avatar": "https://social.example/media/alice.jpg",
            },
        }

    @patch("mastodon_integration.client.get_client")
    def test_default_mode_keeps_replies(self, mock_get_client):
        mock_client = MagicMock()
        mock_client.timeline_home.return_value = [self._status("1", reply_to_id="0", reply_to_account_id="99")]
        mock_get_client.return_value = mock_client

        poll_mastodon_timeline()

        self.assertEqual(Entry.objects.filter(channel=self.channel).count(), 1)

    @patch("mastodon_integration.client.get_client")
    def test_hide_mode_skips_replies(self, mock_get_client):
        self.account.timeline_reply_filter = MastodonAccount.TIMELINE_REPLIES_HIDE
        self.account.save(update_fields=["timeline_reply_filter"])
        mock_client = MagicMock()
        mock_client.timeline_home.return_value = [self._status("2", reply_to_id="1", reply_to_account_id="99")]
        mock_get_client.return_value = mock_client

        poll_mastodon_timeline()

        self.assertFalse(Entry.objects.filter(channel=self.channel).exists())
        self.account.refresh_from_db()
        self.assertEqual(self.account.last_timeline_id, "2")

    @patch("mastodon_integration.client.get_client")
    def test_self_threads_mode_keeps_self_replies(self, mock_get_client):
        self.account.timeline_reply_filter = MastodonAccount.TIMELINE_REPLIES_SELF_THREADS
        self.account.save(update_fields=["timeline_reply_filter"])
        mock_client = MagicMock()
        mock_client.timeline_home.return_value = [self._status("3", author_id="10", reply_to_id="2", reply_to_account_id="10")]
        mock_get_client.return_value = mock_client

        poll_mastodon_timeline()

        self.assertEqual(Entry.objects.filter(channel=self.channel).count(), 1)

    @patch("mastodon_integration.client.get_client")
    def test_self_threads_mode_skips_replies_to_other_people(self, mock_get_client):
        self.account.timeline_reply_filter = MastodonAccount.TIMELINE_REPLIES_SELF_THREADS
        self.account.save(update_fields=["timeline_reply_filter"])
        mock_client = MagicMock()
        mock_client.timeline_home.return_value = [self._status("4", author_id="10", reply_to_id="1", reply_to_account_id="99")]
        mock_get_client.return_value = mock_client

        poll_mastodon_timeline()

        self.assertFalse(Entry.objects.filter(channel=self.channel).exists())

    def _reblog_status(self, status_id: str, *, original_id: str = "0"):
        created_at = timezone.now() + timedelta(seconds=int(status_id))
        original = self._status(original_id, author_id="99")
        return {
            "id": status_id,
            "content": "",
            "url": f"https://social.example/@alice/{status_id}",
            "created_at": created_at,
            "in_reply_to_id": None,
            "in_reply_to_account_id": None,
            "account": {
                "id": "10",
                "display_name": "Alice",
                "username": "alice",
                "url": "https://social.example/@alice",
                "avatar": "https://social.example/media/alice.jpg",
            },
            "reblog": original,
        }

    @patch("mastodon_integration.client.get_client")
    def test_default_mode_skips_reblogs(self, mock_get_client):
        mock_client = MagicMock()
        mock_client.timeline_home.return_value = [self._reblog_status("6")]
        mock_get_client.return_value = mock_client

        poll_mastodon_timeline()

        self.assertFalse(Entry.objects.filter(channel=self.channel).exists())
        self.account.refresh_from_db()
        self.assertEqual(self.account.last_timeline_id, "6")

    @patch("mastodon_integration.client.get_client")
    def test_default_mode_keeps_original_posts(self, mock_get_client):
        mock_client = MagicMock()
        mock_client.timeline_home.return_value = [self._status("7")]
        mock_get_client.return_value = mock_client

        poll_mastodon_timeline()

        self.assertEqual(Entry.objects.filter(channel=self.channel).count(), 1)

    @patch("mastodon_integration.client.get_client")
    def test_include_reblogs_opt_in_keeps_reblogs(self, mock_get_client):
        self.account.timeline_include_reblogs = True
        self.account.save(update_fields=["timeline_include_reblogs"])
        mock_client = MagicMock()
        mock_client.timeline_home.return_value = [self._reblog_status("8")]
        mock_get_client.return_value = mock_client

        poll_mastodon_timeline()

        self.assertEqual(Entry.objects.filter(channel=self.channel).count(), 1)

    @patch("mastodon_integration.client.get_client")
    def test_timeline_entries_are_attached_to_managed_subscription(self, mock_get_client):
        mock_client = MagicMock()
        mock_client.timeline_home.return_value = [self._status("5")]
        mock_get_client.return_value = mock_client

        poll_mastodon_timeline()

        entry = Entry.objects.get(channel=self.channel)
        self.assertIsNotNone(entry.subscription)
        self.assertEqual(entry.subscription.managed_by, "mastodon")
        payload = _entry_json(entry)
        self.assertEqual(payload["_source"]["name"], "Mastodon home timeline")
        self.assertTrue(payload["_source"]["_is_managed"])
        self.assertEqual(payload["_source"]["_provider"], "mastodon")


class PollMastodonNotificationsGroupingTests(TestCase):
    def setUp(self):
        self.notifications_channel, _ = Channel.objects.get_or_create(
            uid="notifications", defaults={"name": "Notifications"}
        )
        self.app = MastodonApp.objects.create(
            instance_url="https://social.example",
            client_id="client-id",
            client_secret="client-secret",
        )
        self.account = MastodonAccount.objects.create(
            app=self.app,
            access_token="access-token",
            account_id="10",
            username="alice@social.example",
            notifications_channel=self.notifications_channel,
        )

    def _favourite(self, notif_id: str, *, status_url: str, status_id: str = "500", actor: str = "bob"):
        return {
            "id": notif_id,
            "type": "favourite",
            "created_at": timezone.now(),
            "account": {
                "id": "20",
                "display_name": actor.title(),
                "username": actor,
                "url": f"https://social.example/@{actor}",
                "avatar": "",
            },
            "status": {"id": status_id, "url": status_url, "uri": status_url},
        }

    def _reblog(self, notif_id: str, *, status_url: str, status_id: str = "500", actor: str = "bob"):
        notif = self._favourite(notif_id, status_url=status_url, status_id=status_id, actor=actor)
        notif["type"] = "reblog"
        return notif

    @patch("mastodon_integration.client.get_client")
    def test_repeated_favourites_on_same_status_are_grouped(self, mock_get_client):
        mock_client = MagicMock()
        mock_client.notifications.return_value = [
            self._favourite("1", status_url="https://social.example/@carol/1", actor="alice_fan"),
            self._favourite("2", status_url="https://social.example/@carol/1", actor="bob_fan"),
        ]
        mock_get_client.return_value = mock_client

        poll_mastodon_notifications()

        entries = Entry.objects.filter(channel=self.notifications_channel)
        self.assertEqual(entries.count(), 1)
        self.assertEqual(entries.first().data["_count"], 2)

    @patch("mastodon_integration.client.get_client")
    def test_favourites_on_different_statuses_are_not_grouped(self, mock_get_client):
        mock_client = MagicMock()
        mock_client.notifications.return_value = [
            self._favourite("1", status_url="https://social.example/@carol/1"),
            self._favourite("2", status_url="https://social.example/@carol/2"),
        ]
        mock_get_client.return_value = mock_client

        poll_mastodon_notifications()

        self.assertEqual(Entry.objects.filter(channel=self.notifications_channel).count(), 2)

    @patch("mastodon_integration.client.get_client")
    def test_favourite_and_reblog_on_same_status_are_not_grouped_together(self, mock_get_client):
        mock_client = MagicMock()
        mock_client.notifications.return_value = [
            self._favourite("1", status_url="https://social.example/@carol/1"),
            self._reblog("2", status_url="https://social.example/@carol/1"),
        ]
        mock_get_client.return_value = mock_client

        poll_mastodon_notifications()

        self.assertEqual(Entry.objects.filter(channel=self.notifications_channel).count(), 2)

    @patch("mastodon_integration.client.get_client")
    def test_favourite_outside_grouping_window_creates_new_entry(self, mock_get_client):
        status_url = "https://social.example/@carol/1"
        stale = Entry.objects.create(
            channel=self.notifications_channel,
            uid="mastodon:notification:old",
            data={"type": "entry", "like-of": [status_url], "_count": 1},
            published=timezone.now() - timedelta(hours=48),
        )
        mock_client = MagicMock()
        mock_client.notifications.return_value = [
            self._favourite("2", status_url=status_url),
        ]
        mock_get_client.return_value = mock_client

        poll_mastodon_notifications()

        self.assertEqual(Entry.objects.filter(channel=self.notifications_channel).count(), 2)
        stale.refresh_from_db()
        self.assertEqual(stale.data["_count"], 1)

    @patch("mastodon_integration.client.get_client")
    def test_mentions_are_never_grouped(self, mock_get_client):
        mention = {
            "id": "1",
            "type": "mention",
            "created_at": timezone.now(),
            "account": {
                "id": "20", "display_name": "Bob", "username": "bob",
                "url": "https://social.example/@bob", "avatar": "",
            },
            "status": {
                "id": "500", "url": "https://social.example/@bob/1", "uri": "https://social.example/@bob/1",
                "content": "<p>hi</p>",
            },
        }
        mock_client = MagicMock()
        mock_client.notifications.return_value = [mention, dict(mention, id="2")]
        mock_get_client.return_value = mock_client

        poll_mastodon_notifications()

        self.assertEqual(Entry.objects.filter(channel=self.notifications_channel).count(), 2)

    @patch("mastodon_integration.client.get_client")
    def test_last_notification_id_advances_past_grouped_notifications(self, mock_get_client):
        mock_client = MagicMock()
        mock_client.notifications.return_value = [
            self._favourite("1", status_url="https://social.example/@carol/1"),
            self._favourite("2", status_url="https://social.example/@carol/1"),
        ]
        mock_get_client.return_value = mock_client

        poll_mastodon_notifications()

        self.account.refresh_from_db()
        self.assertEqual(self.account.last_notification_id, "2")


class ManagedSubscriptionTests(TestCase):
    def setUp(self):
        self.timeline_channel = Channel.objects.create(uid="social", name="Social")
        self.notifications_channel, _ = Channel.objects.get_or_create(
            uid="notifications",
            defaults={"name": "Notifications"},
        )
        self.app = MastodonApp.objects.create(
            instance_url="https://social.example",
            client_id="client-id",
            client_secret="client-secret",
        )
        self.account = MastodonAccount.objects.create(
            app=self.app,
            access_token="access-token",
            account_id="10",
            username="alice@social.example",
            display_name="Alice",
            avatar_url="https://social.example/media/alice.jpg",
            timeline_channel=self.timeline_channel,
            notifications_channel=self.notifications_channel,
        )

    def test_ensure_managed_subscription_creates_virtual_feed(self):
        sub = ensure_managed_subscription(self.account, "timeline")

        self.assertIsNotNone(sub)
        self.assertEqual(sub.channel, self.timeline_channel)
        self.assertEqual(sub.name, "Mastodon home timeline")
        self.assertEqual(sub.photo, self.account.avatar_url)
        self.assertEqual(sub.managed_by, "mastodon")
        self.assertEqual(sub.managed_key, "timeline")
        self.assertTrue(sub.is_active)

    def test_sync_moves_feed_when_channel_changes(self):
        original = ensure_managed_subscription(self.account, "timeline")
        new_channel = Channel.objects.create(uid="news", name="News")
        self.account.timeline_channel = new_channel
        self.account.save(update_fields=["timeline_channel"])

        synced = sync_managed_subscriptions(self.account)["timeline"]

        original.refresh_from_db()
        self.assertFalse(original.is_active)
        self.assertIsNotNone(synced)
        self.assertEqual(synced.channel, new_channel)


class CanonicalUrlTests(TestCase):
    def test_uses_site_configuration_site_url(self):
        config = SiteConfiguration.get_solo()
        config.site_url = "https://example.com"
        config.save(update_fields=["site_url"])
        post = Post.objects.create(title="Hello", slug="hello", kind=Post.NOTE, content="Hi")

        canonical = _build_canonical_url(post)

        self.assertEqual(canonical, "https://example.com/blog/post/hello/")
