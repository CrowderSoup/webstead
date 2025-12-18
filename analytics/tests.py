import json
from unittest.mock import patch

from django.db import IntegrityError
from django.test import TestCase

from blog.models import Post, Tag
from analytics.models import Visit


MICROPUB_URL = "/micropub"


class AnalyticsMiddlewareTests(TestCase):
    @patch("analytics.middleware.Visit.objects.create", side_effect=IntegrityError)
    @patch("micropub.views._authorized", return_value=(True, ["update"]))
    def test_db_error_does_not_break_request_transaction(self, _authorized, _visit_create):
        post = Post.objects.create(title="Tags", slug="page-5", content="hi")
        post.tags.add(Tag.objects.create(tag="existing"))

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
        self.assertSetEqual(set(post.tags.values_list("tag", flat=True)), {"added"})

    @patch("analytics.middleware.Visit.objects.create")
    @patch("analytics.middleware.enqueue_user_agent_lookup")
    def test_user_agent_lookup_is_enqueued_off_thread(self, enqueue_lookup, create_visit):
        user_agent = "Test User Agent"
        create_visit.return_value.id = 123
        create_visit.return_value.user_agent = user_agent

        response = self.client.get("/missing-path/", HTTP_USER_AGENT=user_agent)

        self.assertEqual(response.status_code, 404)
        enqueue_lookup.assert_called_once()
        self.assertEqual(enqueue_lookup.call_args[0], (123, user_agent))
        create_visit.assert_called_once()
