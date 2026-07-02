"""Tests for microsub/signals.py — webmention_to_notifications."""
from django.test import TestCase

from micropub.models import Webmention
from microsub.models import Channel, Entry


def _make_webmention(status="pending", mention_type="mention", **kwargs):
    defaults = {
        "source": "https://source.example.com/post",
        "target": "https://mysite.example.com/blog/hello/",
        "status": status,
        "mention_type": mention_type,
    }
    defaults.update(kwargs)
    return Webmention.objects.create(**defaults)


class WebmentionToNotificationsTests(TestCase):
    def setUp(self):
        self.notifications, _ = Channel.objects.get_or_create(
            uid="notifications", defaults={"name": "Notifications"}
        )

    def test_pending_creates_entry(self):
        _make_webmention(status="pending")
        self.assertEqual(Entry.objects.count(), 1)

    def test_accepted_creates_entry(self):
        _make_webmention(status="accepted")
        self.assertEqual(Entry.objects.count(), 1)

    def test_rejected_creates_entry(self):
        _make_webmention(status="rejected")
        self.assertEqual(Entry.objects.count(), 1)

    def test_outgoing_webmention_creates_no_entry(self):
        _make_webmention(is_incoming=False)
        self.assertEqual(Entry.objects.count(), 0)

    def test_no_notifications_channel_creates_no_entry(self):
        self.notifications.delete()
        _make_webmention()
        self.assertEqual(Entry.objects.count(), 0)

    def test_status_update_does_not_create_duplicate(self):
        """Re-saving a webmention (e.g. approval) does not add a second entry."""
        wm = _make_webmention(status="pending")
        wm.status = "accepted"
        wm.save()
        self.assertEqual(Entry.objects.count(), 1)

    def test_url_points_to_admin_detail_page(self):
        wm = _make_webmention()
        entry = Entry.objects.get(channel=self.notifications)
        self.assertIn(f"/webmentions/{wm.pk}/", entry.data["url"])
        self.assertNotEqual(entry.data["url"], wm.source)

    def test_url_uses_scheme_and_host_from_target(self):
        wm = _make_webmention(target="https://mysite.example.com/blog/hello/")
        entry = Entry.objects.get(channel=self.notifications)
        self.assertTrue(entry.data["url"].startswith("https://mysite.example.com"))

    def test_wm_source_equals_source(self):
        wm = _make_webmention()
        entry = Entry.objects.get(channel=self.notifications)
        self.assertEqual(entry.data["wm-source"], wm.source)

    def test_author_url_equals_source(self):
        wm = _make_webmention()
        entry = Entry.objects.get(channel=self.notifications)
        self.assertEqual(entry.data["author"]["url"], wm.source)

    def test_uid_is_pk_based(self):
        wm = _make_webmention()
        entry = Entry.objects.get(channel=self.notifications)
        self.assertEqual(entry.uid, f"webmention:{wm.pk}")

    def test_two_webmentions_different_sources_create_two_entries(self):
        """Each distinct webmention gets its own notification entry."""
        target = "https://mysite.example.com/blog/hello/"
        _make_webmention(source="https://source-a.example.com/post", target=target, mention_type="like")
        _make_webmention(source="https://source-b.example.com/post", target=target, mention_type="like")
        self.assertEqual(Entry.objects.filter(channel=self.notifications).count(), 2)

    def test_like_sets_like_of(self):
        wm = _make_webmention(mention_type="like")
        entry = Entry.objects.get(channel=self.notifications)
        self.assertEqual(entry.data.get("like-of"), [wm.target])
        self.assertNotIn("in-reply-to", entry.data)
        self.assertNotIn("repost-of", entry.data)

    def test_reply_sets_in_reply_to(self):
        wm = _make_webmention(mention_type="reply")
        entry = Entry.objects.get(channel=self.notifications)
        self.assertEqual(entry.data.get("in-reply-to"), [wm.target])

    def test_repost_sets_repost_of(self):
        wm = _make_webmention(mention_type="repost")
        entry = Entry.objects.get(channel=self.notifications)
        self.assertEqual(entry.data.get("repost-of"), [wm.target])

    def test_plain_mention_has_no_interaction_keys(self):
        _make_webmention(mention_type="mention")
        entry = Entry.objects.get(channel=self.notifications)
        self.assertNotIn("like-of", entry.data)
        self.assertNotIn("in-reply-to", entry.data)
        self.assertNotIn("repost-of", entry.data)

    def test_subscription_is_none(self):
        _make_webmention()
        entry = Entry.objects.get(channel=self.notifications)
        self.assertIsNone(entry.subscription)
