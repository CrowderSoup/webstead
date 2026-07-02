"""Tests for the poll_microsub_feeds management command."""
import datetime
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from microsub.models import Channel, Entry, Subscription


def _make_sub(channel, url="https://example.com/feed", **kwargs):
    defaults = {"is_active": True}
    defaults.update(kwargs)
    return Subscription.objects.create(channel=channel, url=url, **defaults)


class PollMicrosubFeedsCommandTests(TestCase):
    def setUp(self):
        self.channel = Channel.objects.create(uid="news", name="News")

    def _call(self, *args, **kwargs):
        out = StringIO()
        call_command("poll_microsub_feeds", *args, stdout=out, **kwargs)
        return out.getvalue()

    @patch("microsub.feed_parser.fetch_and_parse_feed",
           return_value=([], None, {}))
    def test_polls_active_subscriptions(self, mock_fetch):
        _make_sub(self.channel)
        self._call()
        mock_fetch.assert_called_once()

    @patch("microsub.feed_parser.fetch_and_parse_feed",
           return_value=([], None, {}))
    def test_skips_inactive_subscriptions(self, mock_fetch):
        _make_sub(self.channel, is_active=False)
        self._call()
        mock_fetch.assert_not_called()

    @patch("microsub.feed_parser.fetch_and_parse_feed",
           return_value=([], None, {}))
    def test_channel_filter_limits_subs(self, mock_fetch):
        ch2 = Channel.objects.create(uid="tech", name="Tech")
        _make_sub(self.channel, url="https://news.example.com/feed")
        _make_sub(ch2, url="https://tech.example.com/feed")
        self._call("--channel", "tech")
        self.assertEqual(mock_fetch.call_count, 1)
        call_url = mock_fetch.call_args[0][0]
        self.assertEqual(call_url, "https://tech.example.com/feed")

    @patch("microsub.feed_parser.fetch_and_parse_feed",
           return_value=([], None, {}))
    def test_skips_recently_fetched_without_force(self, mock_fetch):
        sub = _make_sub(self.channel)
        sub.last_fetched_at = timezone.now() - datetime.timedelta(minutes=5)
        sub.save()
        self._call()
        mock_fetch.assert_not_called()

    @patch("microsub.feed_parser.fetch_and_parse_feed",
           return_value=([], None, {}))
    def test_force_polls_recently_fetched(self, mock_fetch):
        sub = _make_sub(self.channel)
        sub.last_fetched_at = timezone.now() - datetime.timedelta(minutes=5)
        sub.save()
        self._call("--force")
        mock_fetch.assert_called_once()

    @patch("microsub.feed_parser.fetch_and_parse_feed",
           return_value=([], None, {}))
    def test_polls_sub_with_no_last_fetched_at(self, mock_fetch):
        _make_sub(self.channel)  # last_fetched_at is None
        self._call()
        mock_fetch.assert_called_once()

    @patch("microsub.feed_parser.fetch_and_parse_feed",
           return_value=([], None, {}))
    def test_polls_sub_older_than_15_minutes(self, mock_fetch):
        sub = _make_sub(self.channel)
        sub.last_fetched_at = timezone.now() - datetime.timedelta(minutes=20)
        sub.save()
        self._call()
        mock_fetch.assert_called_once()

    @patch("microsub.feed_parser.fetch_and_parse_feed",
           return_value=([{"_uid": "e1", "type": "entry"}], None, {}))
    def test_successful_fetch_updates_last_fetched_at(self, mock_fetch):
        sub = _make_sub(self.channel)
        before = timezone.now()
        self._call()
        sub.refresh_from_db()
        self.assertIsNotNone(sub.last_fetched_at)
        self.assertGreaterEqual(sub.last_fetched_at, before)

    @patch("microsub.feed_parser.fetch_and_parse_feed",
           return_value=([{"_uid": "e1"}], None, {}))
    def test_successful_fetch_clears_fetch_error(self, mock_fetch):
        sub = _make_sub(self.channel)
        sub.fetch_error = "previous error"
        sub.save()
        self._call()
        sub.refresh_from_db()
        self.assertEqual(sub.fetch_error, "")

    @patch("microsub.feed_parser.fetch_and_parse_feed",
           return_value=([{"_uid": "e1"}], None, {}))
    def test_successful_fetch_stores_entries(self, mock_fetch):
        _make_sub(self.channel)
        self._call()
        self.assertEqual(Entry.objects.filter(channel=self.channel).count(), 1)

    @patch("microsub.feed_parser.fetch_and_parse_feed",
           side_effect=RuntimeError("connection refused"))
    def test_fetch_error_saves_error_message(self, mock_fetch):
        sub = _make_sub(self.channel)
        self._call()
        sub.refresh_from_db()
        self.assertIn("connection refused", sub.fetch_error)

    @patch("microsub.feed_parser.fetch_and_parse_feed",
           side_effect=RuntimeError("connection refused"))
    def test_fetch_error_updates_last_fetched_at(self, mock_fetch):
        sub = _make_sub(self.channel)
        before = timezone.now()
        self._call()
        sub.refresh_from_db()
        self.assertIsNotNone(sub.last_fetched_at)
        self.assertGreaterEqual(sub.last_fetched_at, before)

    @patch("microsub.feed_parser.fetch_and_parse_feed")
    def test_fetch_error_continues_to_next_sub(self, mock_fetch):
        mock_fetch.side_effect = [
            RuntimeError("first fails"),
            ([{"_uid": "e1"}], None, {}),
            ([{"_uid": "e2"}], None, {}),
        ]
        _make_sub(self.channel, url="https://a.example.com/feed")
        _make_sub(self.channel, url="https://b.example.com/feed")
        self._call()
        self.assertGreaterEqual(Entry.objects.count(), 1)

    @patch("microsub.feed_parser.fetch_and_parse_feed",
           return_value=([], "https://hub.example.com/", {}))
    def test_hub_url_saved_when_discovered(self, mock_fetch):
        sub = _make_sub(self.channel)
        self._call()
        sub.refresh_from_db()
        self.assertEqual(sub.websub_hub, "https://hub.example.com/")

    @patch("microsub.feed_parser.fetch_and_parse_feed",
           return_value=([], "https://new-hub.example.com/", {}))
    def test_existing_hub_not_overwritten(self, mock_fetch):
        sub = _make_sub(self.channel, websub_hub="https://existing-hub.example.com/")
        self._call()
        sub.refresh_from_db()
        self.assertEqual(sub.websub_hub, "https://existing-hub.example.com/")

    @patch("microsub.feed_parser.fetch_and_parse_feed",
           return_value=([], None, {}))
    def test_command_runs_without_error(self, mock_fetch):
        _make_sub(self.channel)
        output = self._call()
        # Command output is per-subscription (no trailing "Done" summary)
        self.assertIsInstance(output, str)

    @patch("microsub.views._subscribe_to_websub_with_base_url")
    @patch("microsub.feed_parser.fetch_and_parse_feed",
           return_value=([], "https://hub.example.com/", {}))
    def test_websub_subscription_attempted_when_base_url_set(self, mock_fetch, mock_sub):
        from django.test import override_settings
        Subscription.objects.create(
            channel=self.channel,
            url="https://example.com/feed",
            is_active=True,
            websub_hub="https://hub.example.com/",
        )
        with override_settings(MICROSUB_BASE_URL="https://mysite.example.com"):
            self._call()
        mock_sub.assert_called_once()
