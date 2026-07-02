from django.db import IntegrityError
from django.test import TestCase
from django.utils import timezone

from microsub.models import (
    BlockedUser,
    Channel,
    Entry,
    EntryCategory,
    EntrySearchToken,
    MutedUser,
    Subscription,
)


class ChannelModelTests(TestCase):
    def test_str_returns_name(self):
        ch = Channel(uid="test", name="My Feed")
        self.assertEqual(str(ch), "My Feed")

    def test_default_order_is_zero(self):
        ch = Channel.objects.create(uid="ch1", name="One")
        self.assertEqual(ch.order, 0)

    def test_uid_uniqueness_enforced(self):
        Channel.objects.create(uid="same", name="First")
        with self.assertRaises(IntegrityError):
            Channel.objects.create(uid="same", name="Second")

    def test_default_ordering_by_order_then_id(self):
        ch_b = Channel.objects.create(uid="b", name="B", order=10)
        ch_a = Channel.objects.create(uid="a", name="A", order=5)
        ch_c = Channel.objects.create(uid="c", name="C", order=10)
        # Filter to just our test channels and confirm their relative order
        channels = list(Channel.objects.filter(uid__in=["a", "b", "c"]))
        self.assertEqual(channels[0], ch_a)   # order=5
        self.assertEqual(channels[1], ch_b)   # order=10, lower id
        self.assertEqual(channels[2], ch_c)   # order=10, higher id


class SubscriptionModelTests(TestCase):
    def setUp(self):
        self.channel = Channel.objects.create(uid="ch", name="Test Channel")

    def test_str_format(self):
        sub = Subscription(channel=self.channel, url="https://example.com/feed")
        self.assertEqual(str(sub), "https://example.com/feed in Test Channel")

    def test_unique_together_channel_url(self):
        Subscription.objects.create(channel=self.channel, url="https://example.com/feed")
        with self.assertRaises(IntegrityError):
            Subscription.objects.create(channel=self.channel, url="https://example.com/feed")

    def test_cascade_delete_with_channel(self):
        Subscription.objects.create(channel=self.channel, url="https://example.com/feed")
        self.channel.delete()
        self.assertEqual(Subscription.objects.count(), 0)

    def test_optional_fields_accept_blanks(self):
        sub = Subscription.objects.create(channel=self.channel, url="https://example.com/")
        self.assertEqual(sub.name, "")
        self.assertEqual(sub.photo, "")
        self.assertEqual(sub.websub_hub, "")
        self.assertIsNone(sub.last_fetched_at)
        self.assertIsNone(sub.websub_subscribed_at)
        self.assertIsNone(sub.websub_expires_at)

    def test_is_active_defaults_true(self):
        sub = Subscription.objects.create(channel=self.channel, url="https://example.com/")
        self.assertTrue(sub.is_active)

    def test_callback_token_is_generated_on_save(self):
        sub = Subscription.objects.create(channel=self.channel, url="https://example.com/")
        self.assertTrue(sub.websub_callback_token)


class EntryModelTests(TestCase):
    def setUp(self):
        self.channel = Channel.objects.create(uid="ch", name="Channel")
        self.sub = Subscription.objects.create(channel=self.channel, url="https://example.com/feed")

    def _make_entry(self, uid="entry-1", **kwargs):
        defaults = {
            "channel": self.channel,
            "uid": uid,
            "data": {"type": "entry"},
            "published": timezone.now(),
        }
        defaults.update(kwargs)
        return Entry.objects.create(**defaults)

    def test_str_truncates_to_50_chars(self):
        long_uid = "x" * 60
        entry = self._make_entry(uid=long_uid)
        self.assertEqual(str(entry), f"Entry {'x' * 50}")

    def test_unique_together_channel_uid(self):
        self._make_entry(uid="dup")
        with self.assertRaises(IntegrityError):
            self._make_entry(uid="dup")

    def test_fetched_at_set_automatically(self):
        entry = self._make_entry()
        self.assertIsNotNone(entry.fetched_at)

    def test_subscription_set_null_on_sub_delete(self):
        entry = self._make_entry(subscription=self.sub)
        self.sub.delete()
        entry.refresh_from_db()
        self.assertIsNone(entry.subscription)

    def test_defaults_unread_and_not_removed(self):
        entry = self._make_entry()
        self.assertFalse(entry.is_read)
        self.assertFalse(entry.is_removed)

    def test_default_ordering_descending_published(self):
        now = timezone.now()
        e1 = self._make_entry("e1", published=now)
        import datetime
        e2 = self._make_entry("e2", published=now + datetime.timedelta(hours=1))
        entries = list(self.channel.entries.all())
        self.assertEqual(entries[0], e2)
        self.assertEqual(entries[1], e1)

    def test_save_normalizes_uid_arrays_and_search_metadata(self):
        entry = self._make_entry(
            uid="entry-2",
            data={
                "type": "entry",
                "_uid": "internal-uid",
                "name": "Hello Search",
                "content": {"text": "hello world"},
                "category": "Python",
                "like-of": "https://liked.example/post",
                "_source": {"url": "https://feeds.example/news.xml"},
                "author": {"type": "card", "url": "https://authors.example/alice/"},
            },
        )
        entry.refresh_from_db()
        self.assertEqual(entry.data["uid"], "internal-uid")
        self.assertEqual(entry.data["like-of"], ["https://liked.example/post"])
        self.assertEqual(entry.author_url, "https://authors.example/alice/")
        self.assertEqual(entry.source_url, "https://feeds.example/news.xml")
        self.assertTrue(entry.kind_like)
        self.assertTrue(EntrySearchToken.objects.filter(entry=entry, token="hello").exists())
        self.assertTrue(EntryCategory.objects.filter(entry=entry, value="python").exists())

    def test_explicit_author_and_source_fields_are_preserved_when_data_is_sparse(self):
        entry = self._make_entry(
            uid="entry-3",
            data={"type": "entry"},
            author_url="https://authors.example/alice/",
            source_url="https://feeds.example/news.xml",
        )
        entry.refresh_from_db()
        self.assertEqual(entry.author_url, "https://authors.example/alice/")
        self.assertEqual(entry.source_url, "https://feeds.example/news.xml")


class MutedUserModelTests(TestCase):
    def setUp(self):
        self.channel = Channel.objects.create(uid="ch", name="Channel")

    def test_str_format(self):
        m = MutedUser(url="https://spammer.example.com/")
        self.assertEqual(str(m), "Muted https://spammer.example.com/")

    def test_site_wide_mute_with_null_channel(self):
        m = MutedUser.objects.create(channel=None, url="https://spammer.example.com/")
        self.assertIsNone(m.channel)

    def test_unique_together_channel_url(self):
        MutedUser.objects.create(channel=self.channel, url="https://spammer.example.com/")
        with self.assertRaises(IntegrityError):
            MutedUser.objects.create(channel=self.channel, url="https://spammer.example.com/")

    def test_cascade_delete_with_channel(self):
        MutedUser.objects.create(channel=self.channel, url="https://spammer.example.com/")
        self.channel.delete()
        self.assertEqual(MutedUser.objects.count(), 0)


class BlockedUserModelTests(TestCase):
    def setUp(self):
        self.channel = Channel.objects.create(uid="ch", name="Channel")

    def test_str_format(self):
        b = BlockedUser(url="https://troll.example.com/")
        self.assertEqual(str(b), "Blocked https://troll.example.com/")

    def test_site_wide_block_with_null_channel(self):
        b = BlockedUser.objects.create(channel=None, url="https://troll.example.com/")
        self.assertIsNone(b.channel)

    def test_unique_together_channel_url(self):
        BlockedUser.objects.create(channel=self.channel, url="https://troll.example.com/")
        with self.assertRaises(IntegrityError):
            BlockedUser.objects.create(channel=self.channel, url="https://troll.example.com/")

    def test_cascade_delete_with_channel(self):
        BlockedUser.objects.create(channel=self.channel, url="https://troll.example.com/")
        self.channel.delete()
        self.assertEqual(BlockedUser.objects.count(), 0)
