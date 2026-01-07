from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import Post, Tag
import copy
import json
from unittest.mock import patch

import requests

from .mf2 import (
    DEFAULT_AVATAR_URL,
    fetch_target_from_url,
    normalize_interaction_properties,
    parse_target_from_html,
)


class TagModelTests(TestCase):
    def test_string_representation(self):
        tag = Tag.objects.create(tag="django")
        self.assertEqual(str(tag), "django")


class PostModelTests(TestCase):
    def test_html_renders_markdown(self):
        post = Post.objects.create(
            title="Markdown post",
            slug="markdown-post",
            content="**bold** text",
        )

        rendered = post.html()

        self.assertIn("<strong>bold</strong>", rendered)

    def test_summary_truncates_and_strips_markdown(self):
        content = "**markdown** " + ("body " * 200)
        post = Post.objects.create(
            title="Summary",
            slug="summary",
            content=content,
        )

        summary = post.summary()

        self.assertTrue(summary.endswith("..."))
        self.assertNotIn("**", summary)
        self.assertLessEqual(len(summary), 503)

    def test_is_published_flag(self):
        post = Post.objects.create(
            title="Draft",
            slug="draft",
            content="text",
        )
        self.assertFalse(post.is_published())

        post.published_on = timezone.now()
        self.assertTrue(post.is_published())

    def test_slug_auto_generated_from_title(self):
        post = Post(title="Hello World", content="text")
        post.save()

        self.assertTrue(post.slug.startswith("hello-world-"))
        suffix = post.slug.split("hello-world-", 1)[1]
        self.assertTrue(suffix.isdigit())

    def test_slug_defaults_to_page_when_title_blank(self):
        Post.objects.create(title="Existing", slug="page", content="content", published_on=timezone.now())

        post = Post(title="", content="text")
        post.save()

        self.assertTrue(post.slug.startswith("article-"))
        suffix = post.slug.split("article-", 1)[1]
        self.assertTrue(suffix.isdigit())


class PostViewTests(TestCase):
    def test_draft_post_requires_login(self):
        post = Post.objects.create(
            title="Draft",
            slug="draft-post",
            content="text",
        )

        response = self.client.get(reverse("post", kwargs={"slug": post.slug}))

        self.assertEqual(response.status_code, 404)

    def test_authenticated_user_can_view_draft_post(self):
        post = Post.objects.create(
            title="Draft",
            slug="draft-for-user",
            content="text",
        )
        user = get_user_model().objects.create_user(
            username="reader",
            email="reader@example.com",
            password="password",
        )
        self.client.force_login(user)

        response = self.client.get(reverse("post", kwargs={"slug": post.slug}))

        self.assertEqual(response.status_code, 200)


class Mf2ParsingTests(TestCase):
    def test_parse_target_from_html_prefers_entry_content(self):
        html = """
        <article class="h-entry">
          <a class="u-url" href="https://example.com/post/1">Permalink</a>
          <p class="p-name">Hello world</p>
          <div class="e-content">This is <strong>content</strong>.</div>
          <a class="p-author h-card" href="https://example.com">Alice</a>
        </article>
        """

        target = parse_target_from_html(html, "https://example.com/post/1")

        self.assertEqual(target["original_url"], "https://example.com/post/1")
        self.assertEqual(target["title"], "Hello world")
        self.assertIn("This is content.", target["summary_text"])

    def test_parse_target_from_html_falls_back_to_url(self):
        html = "<p>No microformats here.</p>"

        target = parse_target_from_html(html, "https://example.com/post/2")

        self.assertIsNone(target)


class Mf2NormalizationTests(TestCase):
    def test_normalize_sample_one(self):
        sample = json.loads(
            """
            {
              "name": [
                "His soul swooned slowly as he heard the snow falling faintly through the universe and faintly falling, like the descent of their last end, upon all the living and the dead.\\n\\n— James Joyce, The Dead"
              ],
              "content": [
                {
                  "value": "His soul swooned slowly as he heard the snow falling faintly through the universe and faintly falling, like the descent of their last end, upon all the living and the dead.\\n\\n— James Joyce, The Dead",
                  "lang": "en-ie",
                  "html": "<blockquote>\\n  <p>His soul swooned slowly as he heard the snow falling faintly through the universe and faintly falling, like the descent of their last end, upon all the living and the dead.</p>\\n</blockquote>\\n\\n<p>— James Joyce, The Dead</p>"
                }
              ],
              "published": [
                "2026-01-06T12:05:13Z"
              ],
              "comment": [
                {
                  "type": [
                    "h-entry"
                  ],
                  "properties": {
                    "name": [
                      "Aaron Crowder"
                    ],
                    "url": [
                      "https://crowdersoup.com/blog/post/like-1767704949"
                    ],
                    "content": [
                      {
                        "value": "Liked https://adactio.com/notes/22340",
                        "lang": "en-ie",
                        "html": "<p>Liked https://adactio.com/notes/22340</p>"
                      }
                    ],
                    "author": [
                      {
                        "type": [
                          "h-card"
                        ],
                        "properties": {
                          "name": [
                            "Aaron Crowder"
                          ],
                          "url": [
                            "https://crowdersoup.com/blog/post/like-1767704949"
                          ]
                        },
                        "value": "Aaron Crowder",
                        "lang": "en-ie"
                      }
                    ]
                  }
                }
              ]
            }
            """
        )
        sample_with_author = copy.deepcopy(sample)
        sample_with_author["author"] = sample["comment"][0]["properties"]["author"]

        target = normalize_interaction_properties(
            sample_with_author,
            target_url="https://adactio.com/notes/22340",
        )

        self.assertEqual(target["original_url"], "https://adactio.com/notes/22340")
        self.assertIsNone(target["title"])
        self.assertEqual(target["summary_html"], sample["content"][0]["html"])
        self.assertEqual(target["author_name"], "Aaron Crowder")
        self.assertEqual(target["author_photo"], DEFAULT_AVATAR_URL)

    def test_normalize_sample_two(self):
        sample = json.loads(
            """
            {
              "url": [
                "https://www.ciccarello.me/posts/2026/01/01/omnibear-available-for-firefox/"
              ],
              "published": [
                "2026-01-01T14:42:00Z"
              ],
              "content": [
                {
                  "value": "Just in time for the IndieWeb Hackathon, you can now install Omnibear from the Firefox Add-on store! It’s also available for Edge and we’re working on Chrome. Please try it out if your site supports Micropub and consider contributing!\\n\\nposted via Omnibear",
                  "lang": "en-US",
                  "html": "<p>Just in time for the IndieWeb Hackathon, you can now install Omnibear from the <a href=\\"https://addons.mozilla.org/en-US/firefox/addon/omnibear/\\">Firefox Add-on store</a>! It’s also available for <a href=\\"https://microsoftedge.microsoft.com/addons/detail/mkmdbhjfgbbdpdemimcmgmacfebjdajl\\">Edge</a> and we’re working on Chrome. Please try it out if your site supports Micropub and consider contributing!</p>\\n<p><em>posted via <a href=\\"https://omnibear.com/\\">Omnibear</a></em></p>"
                }
              ],
              "author": [
                {
                  "type": [
                    "h-card"
                  ],
                  "properties": {
                    "photo": [
                      {
                        "value": "https://gravatar.com/avatar/ec965a0e16969d009a7d9807822ee81f?size=512?s=512",
                        "alt": ""
                      }
                    ],
                    "name": [
                      "Anthony Ciccarello"
                    ],
                    "url": [
                      "https://www.ciccarello.me/"
                    ],
                    "summary": [
                      "I'm a software engineer living in Southern California building cool things using JavaScript and other web technologies. I enjoy travel, disc sports, and spending time in nature."
                    ]
                  },
                  "value": "https://www.ciccarello.me/",
                  "lang": "en-US"
                }
              ]
            }
            """
        )

        target = normalize_interaction_properties(sample)

        self.assertEqual(target["original_url"], sample["url"][0])
        self.assertIsNone(target["title"])
        self.assertEqual(target["summary_text"], sample["content"][0]["value"])
        self.assertEqual(target["summary_html"], sample["content"][0]["html"])
        self.assertEqual(target["author_name"], "Anthony Ciccarello")
        self.assertEqual(target["author_photo"], sample["author"][0]["properties"]["photo"][0]["value"])

    def test_normalize_sample_three(self):
        sample = json.loads(
            """
            {
              "author": [
                {
                  "type": [
                    "h-card"
                  ],
                  "properties": {
                    "url": [
                      "https://cleverdevil.io/profile/cleverdevil",
                      "https://cleverdevil.io/profile/cleverdevil",
                      "https://cleverdevil.io/profile/cleverdevil"
                    ],
                    "photo": [
                      "https://cleverdevil.io/file/e37c3982acf4f0a8421d085b9971cd71/thumb.jpg"
                    ],
                    "name": [
                      "Jonathan LaCour"
                    ]
                  },
                  "value": "Jonathan LaCour",
                  "lang": "en"
                }
              ],
              "url": [
                "https://cleverdevil.io/2026/icloud-bridge-was-developed-with-the-ai-assisted"
              ],
              "published": [
                "2026-01-05T07:42:42+0000"
              ],
              "name": [
                "iCloud Bridge was developed with the AI-assisted methodology I posted about recently. You can dive into the design and implementation plans in the repo - https://github.com/cleverdevil/iCloudBridge/tree/main/docs/plans - The app was developed in a few short weeks."
              ],
              "content": [
                {
                  "value": "iCloud Bridge was developed with the AI-assisted methodology I posted about recently. You can dive into the design and implementation plans in the repo - https://github.com/cleverdevil/iCloudBridge/tree/main/docs/plans - The app was developed in a few short weeks.",
                  "lang": "en",
                  "html": "iCloud Bridge was developed with the AI-assisted methodology I posted about recently. You can dive into the design and implementation plans in the repo - <a href=\\"https://github.com/cleverdevil/iCloudBridge/tree/main/docs/plans\\" target=\\"_blank\\">https://<wbr/>github.com/<wbr/>cleverdevil/<wbr/>iCloudBridge/<wbr/>tree/<wbr/>main/<wbr/>docs/<wbr/>plans</a> - The app was developed in a few short weeks."
                }
              ]
            }
            """
        )

        target = normalize_interaction_properties(sample)

        self.assertEqual(target["original_url"], sample["url"][0])
        self.assertIsNone(target["title"])
        self.assertEqual(target["summary_text"], sample["content"][0]["value"])
        self.assertEqual(target["summary_html"], sample["content"][0]["html"])
        self.assertEqual(target["author_name"], "Jonathan LaCour")
        self.assertEqual(target["author_photo"], sample["author"][0]["properties"]["photo"][0])

    def test_fetch_target_from_url_failure_returns_none(self):
        fetch_target_from_url.cache_clear()
        with patch("blog.mf2.requests.get", side_effect=requests.RequestException):
            target = fetch_target_from_url("https://example.com/post/404")

        self.assertIsNone(target)
