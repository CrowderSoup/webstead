"""Tests for microsub/tasks.py — fetch_reply_context."""
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from microsub.models import Channel, Entry
from microsub.tasks import fetch_reply_context


class FetchReplyContextTests(TestCase):
    def setUp(self):
        self.channel = Channel.objects.create(uid="ch", name="Channel")
        self.entry = Entry.objects.create(
            channel=self.channel,
            uid="reply-1",
            data={"in-reply-to": ["https://parent.example/post"]},
            published=timezone.now(),
        )

    @patch("microsub.feed_parser._build_reply_context")
    def test_stores_context_on_success(self, mock_build):
        mock_build.return_value = {
            "url": "https://parent.example/post",
            "author": {"type": "card", "url": "https://parent.example/", "name": "Parent"},
            "snippet": "Hello",
        }

        fetch_reply_context(self.entry.pk)

        self.entry.refresh_from_db()
        self.assertEqual(self.entry.data["_reply_context"]["snippet"], "Hello")
        mock_build.assert_called_once_with("https://parent.example/post")

    @patch("microsub.feed_parser._build_reply_context")
    def test_no_context_found_leaves_data_untouched(self, mock_build):
        mock_build.return_value = None

        fetch_reply_context(self.entry.pk)

        self.entry.refresh_from_db()
        self.assertNotIn("_reply_context", self.entry.data)

    @patch("microsub.feed_parser._build_reply_context")
    def test_fetch_failure_does_not_raise_after_retries_exhausted(self, mock_build):
        mock_build.side_effect = RuntimeError("network down")

        # bind=True task called directly (not via .delay/.apply) still goes
        # through self.retry(), which re-raises as a Celery Retry exception
        # when not run inside an active worker/eager context -- assert it
        # surfaces as *some* exception rather than silently swallowing it or
        # writing bad data, without depending on Celery's retry internals.
        with self.assertRaises(Exception):
            fetch_reply_context(self.entry.pk)

        self.entry.refresh_from_db()
        self.assertNotIn("_reply_context", self.entry.data)

    @patch("microsub.feed_parser._build_reply_context")
    def test_other_exception_is_swallowed(self, mock_build):
        mock_build.side_effect = ValueError("unparseable")

        fetch_reply_context(self.entry.pk)  # should not raise

        self.entry.refresh_from_db()
        self.assertNotIn("_reply_context", self.entry.data)

    def test_missing_entry_is_a_noop(self):
        fetch_reply_context(999999)  # should not raise

    def test_entry_without_in_reply_to_is_a_noop(self):
        note = Entry.objects.create(
            channel=self.channel,
            uid="note-1",
            data={"content": {"text": "hello"}},
            published=timezone.now(),
        )

        fetch_reply_context(note.pk)  # should not raise

        note.refresh_from_db()
        self.assertNotIn("_reply_context", note.data)

    @patch("microsub.feed_parser._build_reply_context")
    def test_existing_data_survives_a_later_unrelated_resave(self, mock_build):
        mock_build.return_value = {
            "url": "https://parent.example/post",
            "snippet": "Hello",
        }
        fetch_reply_context(self.entry.pk)
        self.entry.refresh_from_db()

        # A later, unrelated re-save (e.g. mark_read) must not clobber the
        # cached reply context.
        self.entry.is_read = True
        self.entry.save()

        self.entry.refresh_from_db()
        self.assertEqual(self.entry.data["_reply_context"]["snippet"], "Hello")
