"""Tests for view helper functions: _require_scope, _channel_json, _entry_json, _store_entries."""
from django.test import TestCase
from django.utils import timezone

from microsub.models import BlockedUser, Channel, Entry, Subscription
from microsub.utils import apply_timeline_filters, ensure_photo_list
from microsub.views import _channel_json, _entry_json, _require_scope, _store_entries


class RequireScopeTests(TestCase):
    def test_present_scope_returns_true(self):
        self.assertTrue(_require_scope(["read", "follow"], "read"))

    def test_absent_scope_returns_false(self):
        self.assertFalse(_require_scope(["read"], "channels"))

    def test_empty_scopes_returns_false(self):
        self.assertFalse(_require_scope([], "read"))


class ChannelJsonTests(TestCase):
    def setUp(self):
        self.channel = Channel.objects.create(uid="news", name="News")

    def test_includes_uid_name_unread(self):
        result = _channel_json(self.channel)
        self.assertEqual(result["uid"], "news")
        self.assertEqual(result["name"], "News")
        self.assertIn("unread", result)

    def test_unread_count_excludes_read_entries(self):
        now = timezone.now()
        Entry.objects.create(channel=self.channel, uid="e1", data={}, published=now, is_read=False)
        Entry.objects.create(channel=self.channel, uid="e2", data={}, published=now, is_read=True)
        result = _channel_json(self.channel)
        self.assertEqual(result["unread"], 1)

    def test_unread_count_excludes_removed_entries(self):
        now = timezone.now()
        Entry.objects.create(channel=self.channel, uid="e1", data={}, published=now, is_removed=True)
        result = _channel_json(self.channel)
        self.assertEqual(result["unread"], 0)

    def test_unread_count_zero_when_empty(self):
        result = _channel_json(self.channel)
        self.assertEqual(result["unread"], 0)


class EntryJsonTests(TestCase):
    def setUp(self):
        self.channel = Channel.objects.create(uid="ch", name="Channel")
        self.sub = Subscription.objects.create(
            channel=self.channel,
            url="https://example.com/feed",
            name="Example",
            photo="https://example.com/photo.jpg",
        )

    def test_injects_id_as_string(self):
        entry = Entry.objects.create(
            channel=self.channel, uid="e1", data={"type": "entry"}, published=timezone.now()
        )
        result = _entry_json(entry)
        self.assertIsInstance(result["_id"], str)
        self.assertEqual(result["_id"], str(entry.pk))

    def test_injects_is_read(self):
        entry = Entry.objects.create(
            channel=self.channel, uid="e1", data={}, published=timezone.now(), is_read=True
        )
        result = _entry_json(entry)
        self.assertTrue(result["_is_read"])

    def test_no_source_when_no_subscription(self):
        entry = Entry.objects.create(
            channel=self.channel, uid="e1", data={}, published=timezone.now(), subscription=None
        )
        result = _entry_json(entry)
        self.assertNotIn("_source", result)

    def test_source_added_when_subscription_present(self):
        entry = Entry.objects.create(
            channel=self.channel, uid="e1", data={}, published=timezone.now(), subscription=self.sub
        )
        result = _entry_json(entry)
        self.assertIn("_source", result)
        self.assertEqual(result["_source"]["_id"], str(self.sub.pk))
        self.assertEqual(result["_source"]["url"], "https://example.com/feed")
        self.assertEqual(result["_source"]["name"], "Example")
        self.assertEqual(result["_source"]["photo"], "https://example.com/photo.jpg")

    def test_managed_subscription_source_includes_provider_metadata(self):
        self.sub.managed_by = "mastodon"
        self.sub.managed_key = "timeline"
        self.sub.save(update_fields=["managed_by", "managed_key"])
        entry = Entry.objects.create(
            channel=self.channel, uid="e1", data={}, published=timezone.now(), subscription=self.sub
        )

        result = _entry_json(entry)

        self.assertTrue(result["_source"]["_is_managed"])
        self.assertEqual(result["_source"]["_provider"], "mastodon")
        self.assertEqual(result["_source"]["_managed_key"], "timeline")

    def test_does_not_mutate_original_data(self):
        original_data = {"type": "entry", "name": "Hello"}
        entry = Entry.objects.create(
            channel=self.channel, uid="e1", data=original_data, published=timezone.now()
        )
        _entry_json(entry)
        self.assertNotIn("_id", original_data)


class StoreEntriesTests(TestCase):
    def setUp(self):
        self.channel = Channel.objects.create(uid="ch", name="Channel")
        self.sub = Subscription.objects.create(channel=self.channel, url="https://example.com/feed")

    def test_stores_entry_with_uid(self):
        entries = [{"_uid": "https://example.com/1", "type": "entry"}]
        count = _store_entries(self.channel, self.sub, entries)
        self.assertEqual(count, 1)
        self.assertEqual(Entry.objects.count(), 1)

    def test_uses_url_as_uid_fallback(self):
        entries = [{"url": "https://example.com/post", "type": "entry"}]
        count = _store_entries(self.channel, self.sub, entries)
        self.assertEqual(count, 1)
        self.assertEqual(Entry.objects.first().uid, "https://example.com/post")

    def test_skips_entry_with_no_uid(self):
        entries = [{"type": "entry", "name": "No ID"}]
        count = _store_entries(self.channel, self.sub, entries)
        self.assertEqual(count, 0)
        self.assertEqual(Entry.objects.count(), 0)

    def test_duplicate_uid_not_recreated(self):
        entries = [{"_uid": "https://example.com/1", "type": "entry"}]
        _store_entries(self.channel, self.sub, entries)
        count = _store_entries(self.channel, self.sub, entries)
        self.assertEqual(count, 0)
        self.assertEqual(Entry.objects.count(), 1)

    def test_parses_valid_iso_published(self):
        entries = [{"_uid": "e1", "published": "2024-01-15T10:30:00Z"}]
        _store_entries(self.channel, self.sub, entries)
        entry = Entry.objects.first()
        self.assertEqual(entry.published.year, 2024)
        self.assertEqual(entry.published.month, 1)
        self.assertEqual(entry.published.day, 15)

    def test_makes_naive_datetime_aware(self):
        entries = [{"_uid": "e1", "published": "2024-01-15T10:30:00"}]
        _store_entries(self.channel, self.sub, entries)
        entry = Entry.objects.first()
        self.assertIsNotNone(entry.published.tzinfo)

    def test_falls_back_to_now_on_missing_published(self):
        before = timezone.now()
        entries = [{"_uid": "e1"}]
        _store_entries(self.channel, self.sub, entries)
        entry = Entry.objects.first()
        self.assertGreaterEqual(entry.published, before)

    def test_falls_back_to_now_on_unparseable_published(self):
        before = timezone.now()
        entries = [{"_uid": "e1", "published": "not-a-date"}]
        _store_entries(self.channel, self.sub, entries)
        entry = Entry.objects.first()
        self.assertGreaterEqual(entry.published, before)

    def test_none_subscription_accepted(self):
        entries = [{"_uid": "e1", "type": "entry"}]
        count = _store_entries(self.channel, None, entries)
        self.assertEqual(count, 1)
        self.assertIsNone(Entry.objects.first().subscription)

    def test_returns_accurate_new_count(self):
        entries = [
            {"_uid": "e1"},
            {"_uid": "e2"},
            {"_uid": "e3"},
        ]
        count = _store_entries(self.channel, self.sub, entries)
        self.assertEqual(count, 3)

    def test_checkin_venue_data_survives_storage(self):
        """
        Regression guard: checkin venue data (name/geo) already survives
        _hentry_to_jf2 parsing (see test_checkin_embedded_hcard in
        test_feed_parser.py) -- this locks in that it also survives the
        full _store_entries -> Entry.save() -> normalize_entry_data path,
        which nothing previously tested end-to-end.
        """
        entries = [
            {
                "_uid": "https://venue.example.com/checkin/1",
                "type": "entry",
                "checkin": {
                    "type": "card",
                    "name": "Coffee Shop",
                    "latitude": "37.7749",
                    "longitude": "-122.4194",
                },
            }
        ]
        count = _store_entries(self.channel, self.sub, entries)
        self.assertEqual(count, 1)
        entry = Entry.objects.get(uid="https://venue.example.com/checkin/1")
        self.assertTrue(entry.kind_checkin)
        self.assertEqual(entry.data["checkin"]["name"], "Coffee Shop")
        self.assertEqual(entry.data["checkin"]["latitude"], "37.7749")
        self.assertEqual(entry.data["checkin"]["longitude"], "-122.4194")

    def test_skips_future_entries_from_blocked_author(self):
        BlockedUser.objects.create(channel=self.channel, url="https://blocked.example/")
        count = _store_entries(
            self.channel,
            self.sub,
            [
                {
                    "_uid": "blocked-entry",
                    "author": {"type": "card", "url": "https://blocked.example/"},
                }
            ],
        )
        self.assertEqual(count, 0)
        self.assertFalse(Entry.objects.filter(uid="blocked-entry").exists())


class EnsurePhotoListTests(TestCase):
    def test_none_returns_empty_list(self):
        self.assertEqual(ensure_photo_list(None), [])

    def test_plain_string_wrapped_in_list(self):
        self.assertEqual(ensure_photo_list("https://example.com/p.jpg"), ["https://example.com/p.jpg"])

    def test_list_of_plain_strings(self):
        self.assertEqual(
            ensure_photo_list(["https://a.example/1.jpg", "https://a.example/2.jpg"]),
            ["https://a.example/1.jpg", "https://a.example/2.jpg"],
        )

    def test_dict_without_alt_collapses_to_string(self):
        self.assertEqual(
            ensure_photo_list([{"value": "https://a.example/1.jpg"}]),
            ["https://a.example/1.jpg"],
        )

    def test_dict_with_alt_preserved(self):
        self.assertEqual(
            ensure_photo_list([{"value": "https://a.example/1.jpg", "alt": "A cat"}]),
            [{"value": "https://a.example/1.jpg", "alt": "A cat"}],
        )

    def test_dict_with_blank_alt_collapses_to_string(self):
        self.assertEqual(
            ensure_photo_list([{"value": "https://a.example/1.jpg", "alt": "  "}]),
            ["https://a.example/1.jpg"],
        )

    def test_mixed_list(self):
        self.assertEqual(
            ensure_photo_list([
                {"value": "https://a.example/1.jpg", "alt": "One"},
                "https://a.example/2.jpg",
            ]),
            [
                {"value": "https://a.example/1.jpg", "alt": "One"},
                "https://a.example/2.jpg",
            ],
        )

    def test_dict_without_value_or_url_is_skipped(self):
        self.assertEqual(ensure_photo_list([{"alt": "orphaned alt text"}]), [])


class ApplyTimelineFiltersTests(TestCase):
    """Shared filter helper used by _get_timeline, _content_search_qs, and the admin debug view."""

    def setUp(self):
        self.channel = Channel.objects.create(uid="ch", name="Channel")
        now = timezone.now()
        self.photo_entry = Entry.objects.create(
            channel=self.channel,
            uid="photo-1",
            data={"photo": ["https://example.com/p.jpg"]},
            published=now,
            author_url="https://alice.example/",
            source_url="https://alice.example/feed",
        )
        self.note_entry = Entry.objects.create(
            channel=self.channel,
            uid="note-1",
            data={"content": {"text": "hello"}},
            published=now,
            author_url="https://bob.example/",
            source_url="https://bob.example/feed",
        )

    def _base_qs(self):
        return Entry.objects.filter(channel=self.channel)

    def test_no_filters_returns_everything(self):
        qs = apply_timeline_filters(self._base_qs())
        self.assertEqual(qs.count(), 2)

    def test_filters_by_kind(self):
        qs = apply_timeline_filters(self._base_qs(), kinds=["photo"])
        self.assertEqual(list(qs), [self.photo_entry])

    def test_unknown_kind_returns_none(self):
        qs = apply_timeline_filters(self._base_qs(), kinds=["not-a-real-kind"])
        self.assertEqual(qs.count(), 0)

    def test_filters_by_single_author(self):
        qs = apply_timeline_filters(self._base_qs(), author="https://alice.example/")
        self.assertEqual(list(qs), [self.photo_entry])

    def test_filters_by_multiple_authors(self):
        qs = apply_timeline_filters(
            self._base_qs(), author=["https://alice.example/", "https://bob.example/"]
        )
        self.assertEqual(qs.count(), 2)

    def test_filters_by_single_source(self):
        qs = apply_timeline_filters(self._base_qs(), source="https://bob.example/feed")
        self.assertEqual(list(qs), [self.note_entry])

    def test_filters_by_category(self):
        tagged = Entry.objects.create(
            channel=self.channel,
            uid="tagged-1",
            data={"category": ["news"]},
            published=timezone.now(),
        )
        qs = apply_timeline_filters(self._base_qs(), categories=["news"])
        self.assertEqual(list(qs), [tagged])

    def test_no_matching_author_returns_empty(self):
        qs = apply_timeline_filters(self._base_qs(), author="https://nobody.example/")
        self.assertEqual(qs.count(), 0)
