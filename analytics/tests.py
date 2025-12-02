import json
from unittest.mock import patch

from django.db import IntegrityError
from django.test import TestCase

from blog.models import Post, Tag


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
