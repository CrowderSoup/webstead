from django.contrib.auth import get_user_model
from django.test import TestCase, RequestFactory
from django.test.utils import override_settings
from django.urls import reverse
from django.utils import timezone

from .models import Comment, Post, Tag
from core.models import SiteConfiguration
from micropub.models import Webmention
from .views import _interaction_payload
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
from .comments import AkismetError, AkismetResult
from files.models import Attachment, File
from django.core.files.uploadedfile import SimpleUploadedFile
import tempfile


class TagModelTests(TestCase):
    def test_string_representation(self):
        tag = Tag.objects.create(tag="django")
        self.assertEqual(str(tag), "django")


class PostModelTests(TestCase):
    def test_untitled_posts_in_same_second_get_distinct_slugs(self):
        frozen = timezone.now()
        with patch("blog.models.timezone.now", return_value=frozen):
            first = Post.objects.create(kind=Post.NOTE, content="one")
            second = Post.objects.create(kind=Post.NOTE, content="two")
            third = Post.objects.create(kind=Post.NOTE, content="three")

        timestamp = int(frozen.timestamp())
        self.assertEqual(first.slug, f"note-{timestamp}")
        self.assertEqual(second.slug, f"note-{timestamp}-2")
        self.assertEqual(third.slug, f"note-{timestamp}-3")

    def test_titled_posts_in_same_second_get_distinct_slugs(self):
        frozen = timezone.now()
        with patch("blog.models.timezone.now", return_value=frozen):
            first = Post.objects.create(title="Hello", content="one")
            second = Post.objects.create(title="Hello", content="two")

        self.assertNotEqual(first.slug, second.slug)
        self.assertTrue(second.slug.startswith(f"hello-{int(frozen.timestamp())}"))

    def test_long_title_slug_fits_max_length(self):
        post = Post.objects.create(title="word " * 100, content="hi")

        self.assertLessEqual(len(post.slug), Post._meta.get_field("slug").max_length)

    def test_explicit_slug_is_kept(self):
        post = Post.objects.create(title="T", slug="chosen", content="hi")

        self.assertEqual(post.slug, "chosen")

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

    def test_post_view_accepts_no_trailing_slash(self):
        post = Post.objects.create(
            title="Published",
            slug="published-post",
            content="text",
            published_on=timezone.now(),
        )

        response = self.client.get(f"/blog/post/{post.slug}")

        self.assertEqual(response.status_code, 200)


class WebmentionFormDisplayTests(TestCase):
    def setUp(self):
        self.post = Post.objects.create(
            title="Webmention Post",
            slug="webmention-post",
            content="text",
            published_on=timezone.now(),
        )

    def test_renders_login_cta_when_unauthenticated(self):
        response = self.client.get(reverse("post", kwargs={"slug": self.post.slug}))

        self.assertContains(response, "Send a Webmention")
        self.assertContains(response, "Login with your website")
        self.assertNotContains(response, "Your post URL")

    def test_renders_form_when_authenticated(self):
        session = self.client.session
        session["indieauth_me"] = "https://example.com/"
        session.save()

        response = self.client.get(reverse("post", kwargs={"slug": self.post.slug}))

        self.assertContains(response, "Your post URL")
        self.assertContains(response, "Send Webmention")


class WebmentionDisplayTests(TestCase):
    def setUp(self):
        self.post = Post.objects.create(
            title="Webmention Post",
            slug="webmention-post",
            content="text",
            published_on=timezone.now(),
        )

    def test_replies_render_with_mf2_data(self):
        Webmention.objects.create(
            source="https://example.com/reply-1",
            target="https://testserver/blog/post/webmention-post",
            mention_type=Webmention.REPLY,
            status=Webmention.ACCEPTED,
            target_post=self.post,
        )

        payload = {
            "author_name": "Indie Reply",
            "author_url": "https://example.com/",
            "author_photo": "https://example.com/avatar.png",
            "summary_excerpt": "Hello from the webmention.",
        }

        with patch("blog.views.fetch_target_from_url", return_value=payload):
            response = self.client.get(reverse("post", kwargs={"slug": self.post.slug}))

        self.assertContains(response, "Indie Reply")
        self.assertContains(response, "Hello from the webmention.")
        self.assertContains(response, "https://example.com/avatar.png")

    def test_reply_fallback_when_mf2_missing(self):
        Webmention.objects.create(
            source="https://example.com/reply-2",
            target="https://testserver/blog/post/webmention-post",
            mention_type=Webmention.REPLY,
            status=Webmention.ACCEPTED,
            target_post=self.post,
        )

        with patch("blog.views.fetch_target_from_url", return_value=None):
            response = self.client.get(reverse("post", kwargs={"slug": self.post.slug}))

        self.assertContains(response, "https://example.com/reply-2")
        self.assertContains(response, DEFAULT_AVATAR_URL)

    def test_likes_and_reposts_render_counts_and_links(self):
        Webmention.objects.create(
            source="https://example.com/like-1",
            target="https://testserver/blog/post/webmention-post",
            mention_type=Webmention.MENTION,
            status=Webmention.ACCEPTED,
            target_post=self.post,
        )
        Webmention.objects.create(
            source="https://example.com/repost-1",
            target="https://testserver/blog/post/webmention-post",
            mention_type=Webmention.REPOST,
            status=Webmention.ACCEPTED,
            target_post=self.post,
        )

        response = self.client.get(reverse("post", kwargs={"slug": self.post.slug}))

        self.assertContains(response, "Likes (1)")
        self.assertContains(response, "Reposts (1)")
        self.assertContains(response, "https://example.com/like-1")
        self.assertContains(response, "https://example.com/repost-1")

    def test_outgoing_webmentions_not_shown_on_post(self):
        # Outgoing webmentions (is_incoming=False) stored with target_post set to
        # the local post must not appear in the post's display.
        Webmention.objects.create(
            source="https://testserver/blog/post/webmention-post",
            target="https://example.com/their-post",
            mention_type=Webmention.LIKE,
            status=Webmention.ACCEPTED,
            target_post=self.post,
            is_incoming=False,
        )

        response = self.client.get(reverse("post", kwargs={"slug": self.post.slug}))

        self.assertNotContains(response, "https://example.com/their-post")


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

    def test_parse_target_from_html_falls_back_to_open_graph_metadata(self):
        html = """
        <html>
          <head>
            <meta property="og:title" content="Original Post Title" />
            <meta property="og:description" content="A plain HTML page without microformats." />
            <meta property="og:site_name" content="Example Social" />
          </head>
          <body><p>No microformats here either.</p></body>
        </html>
        """

        target = parse_target_from_html(html, "https://example.com/post/3")

        self.assertEqual(target["original_url"], "https://example.com/post/3")
        self.assertEqual(target["title"], "Original Post Title")
        self.assertEqual(target["summary_text"], "A plain HTML page without microformats.")
        self.assertEqual(target["author_name"], "Example Social")
        self.assertEqual(target["author_url"], "https://example.com")

    def test_parse_target_from_html_supports_h_event(self):
        html = """
        <article class="h-event">
          <a class="u-url" href="https://events.example.com/meetup">Event link</a>
          <p class="p-name">Website Club</p>
          <p class="p-description">Bring your laptop.</p>
        </article>
        """

        target = parse_target_from_html(html, "https://events.example.com/meetup")

        self.assertEqual(target["original_url"], "https://events.example.com/meetup")
        self.assertIsNone(target["title"])
        self.assertIn("Website Club", target["summary_text"])


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
        with self.assertLogs("blog.mf2", level="WARNING"):
            with patch("blog.mf2.requests.get", side_effect=requests.RequestException):
                target = fetch_target_from_url("https://example.com/post/404")

        self.assertIsNone(target)


class InteractionPayloadTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()

    def test_falls_back_to_local_post_when_fetch_fails(self):
        target = Post.objects.create(
            title="Target Post",
            slug="target-post",
            content="Target content",
            published_on=timezone.now(),
        )
        like = Post.objects.create(
            title="Like Post",
            slug="like-post",
            content=f"Liked /blog/post/{target.slug}",
            kind=Post.LIKE,
            published_on=timezone.now(),
            like_of=f"/blog/post/{target.slug}",
        )

        request = self.factory.get("/blog/")

        with patch("blog.views.fetch_target_from_url", return_value=None):
            payload = _interaction_payload(like, request=request)

        self.assertEqual(payload["target"]["original_url"], f"/blog/post/{target.slug}")
        self.assertEqual(payload["target"]["title"], target.title)

    def test_rsvp_builds_interaction_payload(self):
        rsvp = Post.objects.create(
            title="RSVP Post",
            slug="rsvp-post",
            content="RSVP yes",
            kind=Post.RSVP,
            published_on=timezone.now(),
            in_reply_to="https://events.example.com/meetup",
        )
        request = self.factory.get("/blog/")

        with patch("blog.views.fetch_target_from_url", return_value=None):
            payload = _interaction_payload(rsvp, request=request)

        self.assertEqual(payload["kind"], Post.RSVP)
        self.assertEqual(payload["label"], "RSVP to")
        self.assertEqual(payload["target_url"], "https://events.example.com/meetup")


class InteractionRenderingTests(TestCase):
    def test_fallback_renders_target_url_when_preview_missing(self):
        post = Post.objects.create(
            title="Reply Post",
            slug="reply-post",
            content="My reply body",
            kind=Post.REPLY,
            in_reply_to="https://example.com/original",
            published_on=timezone.now(),
        )

        with patch("blog.views.fetch_target_from_url", return_value=None):
            response = self.client.get(reverse("post", kwargs={"slug": post.slug}))

        self.assertContains(response, "interaction-missing")
        self.assertContains(response, "https://example.com/original")
        self.assertContains(response, "My reply body")


class PostFilterTests(TestCase):
    def setUp(self):
        self.tag_arcane = Tag.objects.create(tag="arcane")
        self.tag_docker = Tag.objects.create(tag="docker")
        self.tag_extra = Tag.objects.create(tag="extra")

        self.article = Post.objects.create(
            title="Arcane Docker",
            slug="arcane-docker",
            content="text",
            kind=Post.ARTICLE,
            published_on=timezone.now(),
        )
        self.article.tags.add(self.tag_arcane, self.tag_docker)

        self.note = Post.objects.create(
            title="Arcane Note",
            slug="arcane-note",
            content="text",
            kind=Post.NOTE,
            published_on=timezone.now(),
        )
        self.note.tags.add(self.tag_arcane)

        self.photo = Post.objects.create(
            title="Docker Photo",
            slug="docker-photo",
            content="text",
            kind=Post.PHOTO,
            published_on=timezone.now(),
        )
        self.photo.tags.add(self.tag_docker, self.tag_extra)

    def test_filter_query_serialization(self):
        response = self.client.get(reverse("posts"), {"kind": "article,note", "tag": "arcane"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["filter_query"], "kind=article,note&tag=arcane")

    def test_filter_by_kind_and_tags(self):
        response = self.client.get(
            reverse("posts"),
            {"kind": "article,note", "tag": "arcane,docker"},
        )

        posts = list(response.context["posts"].object_list)

        self.assertIn(self.article, posts)
        self.assertNotIn(self.note, posts)
        self.assertNotIn(self.photo, posts)

    def test_tag_pill_targets_filter(self):
        response = self.client.get(reverse("posts"))

        self.assertContains(response, 'data-tag="arcane"')
        self.assertContains(response, "/?tag=arcane")

    def test_tag_page_redirects_to_filter(self):
        response = self.client.get(reverse("posts_by_tag", kwargs={"tag": "arcane"}))

        self.assertEqual(response.status_code, 301)
        self.assertEqual(response["Location"], "/?tag=arcane")


class PostListingPhotoTests(TestCase):
    def _make_photo_post(self, *, kind, title, slug):
        post = Post.objects.create(
            title=title,
            slug=slug,
            content="text",
            kind=kind,
            published_on=timezone.now(),
        )
        upload = SimpleUploadedFile("photo.jpg", b"fake-image-data", content_type="image/jpeg")
        asset = File.objects.create(kind=File.IMAGE, file=upload)
        Attachment.objects.create(content_object=post, asset=asset, role="photo")
        return post

    def test_checkin_photo_renders_on_listing_page(self):
        with tempfile.TemporaryDirectory() as media_root:
            with override_settings(MEDIA_ROOT=media_root):
                self._make_photo_post(
                    kind=Post.CHECKIN, title="Sugar House", slug="sugar-house-checkin"
                )

                response = self.client.get(reverse("posts"), {"kind": "checkin"})

                self.assertEqual(response.status_code, 200)
                self.assertContains(response, 'class="u-photo"')

    def test_activity_photo_renders_on_listing_page(self):
        with tempfile.TemporaryDirectory() as media_root:
            with override_settings(MEDIA_ROOT=media_root):
                self._make_photo_post(
                    kind=Post.ACTIVITY, title="Morning Run", slug="morning-run-activity"
                )

                response = self.client.get(reverse("posts"), {"kind": "activity"})

                self.assertEqual(response.status_code, 200)
                self.assertContains(response, 'class="u-photo"')

    def test_checkin_photo_renders_on_index_page(self):
        with tempfile.TemporaryDirectory() as media_root:
            with override_settings(MEDIA_ROOT=media_root):
                self._make_photo_post(
                    kind=Post.CHECKIN, title="Sugar House", slug="sugar-house-checkin-index"
                )

                response = self.client.get(reverse("index"))

                self.assertEqual(response.status_code, 200)
                self.assertContains(response, 'class="u-photo"')


class PostMapSliderTests(TestCase):
    def _make_checkin_post(self, *, slug, with_location, with_photo=True):
        post = Post.objects.create(
            title="Sugar House",
            slug=slug,
            content="text",
            kind=Post.CHECKIN,
            published_on=timezone.now(),
            mf2=(
                {"checkin": {"latitude": 40.7241, "longitude": -111.8563}}
                if with_location
                else {}
            ),
        )
        if with_photo:
            upload = SimpleUploadedFile("photo.jpg", b"fake-image-data", content_type="image/jpeg")
            asset = File.objects.create(kind=File.IMAGE, file=upload)
            Attachment.objects.create(content_object=post, asset=asset, role="photo")
        return post

    def _make_activity_post(self, *, slug, with_track, with_photo=True):
        post = Post.objects.create(
            title="Morning Run",
            slug=slug,
            content="text",
            kind=Post.ACTIVITY,
            published_on=timezone.now(),
        )
        if with_track:
            gpx_upload = SimpleUploadedFile("track.gpx", b"<gpx></gpx>", content_type="application/gpx+xml")
            gpx_asset = File.objects.create(kind=File.DOC, file=gpx_upload)
            Attachment.objects.create(content_object=post, asset=gpx_asset, role="gpx")
        if with_photo:
            upload = SimpleUploadedFile("photo.jpg", b"fake-image-data", content_type="image/jpeg")
            asset = File.objects.create(kind=File.IMAGE, file=upload)
            Attachment.objects.create(content_object=post, asset=asset, role="photo")
        return post

    def test_checkin_map_is_first_slide_on_listing_page(self):
        with tempfile.TemporaryDirectory() as media_root:
            with override_settings(MEDIA_ROOT=media_root):
                self._make_checkin_post(slug="sugar-house-map-listing", with_location=True)

                response = self.client.get(reverse("posts"), {"kind": "checkin"})
                content = response.content.decode()

                self.assertContains(response, 'slide--map')
                self.assertContains(response, "data-checkin-map")
                self.assertContains(response, 'data-map-static="true"')
                self.assertLess(content.index("slide--map"), content.index('class="u-photo"'))

    def test_checkin_map_is_first_slide_on_detail_page(self):
        with tempfile.TemporaryDirectory() as media_root:
            with override_settings(MEDIA_ROOT=media_root):
                post = self._make_checkin_post(slug="sugar-house-map-detail", with_location=True)

                response = self.client.get(reverse("post", kwargs={"slug": post.slug}))
                content = response.content.decode()

                self.assertContains(response, 'slide--map')
                self.assertLess(content.index("slide--map"), content.index('class="u-photo"'))

    def test_checkin_without_location_has_no_map_slide(self):
        with tempfile.TemporaryDirectory() as media_root:
            with override_settings(MEDIA_ROOT=media_root):
                self._make_checkin_post(slug="sugar-house-no-location", with_location=False)

                response = self.client.get(reverse("posts"), {"kind": "checkin"})

                self.assertNotContains(response, "slide--map")
                self.assertContains(response, 'class="u-photo"')

    def test_activity_map_and_stats_precede_photos_on_listing_page(self):
        with tempfile.TemporaryDirectory() as media_root:
            with override_settings(MEDIA_ROOT=media_root):
                self._make_activity_post(slug="morning-run-listing", with_track=True)

                response = self.client.get(reverse("posts"), {"kind": "activity"})
                content = response.content.decode()

                self.assertContains(response, "data-gpx-url")
                self.assertContains(response, 'data-map-static="true"')
                self.assertContains(response, "data-activity-stats")
                map_index = content.index("slide--map")
                stats_index = content.index("slide--stats")
                photo_index = content.index('class="u-photo"')
                self.assertLess(map_index, stats_index)
                self.assertLess(stats_index, photo_index)

    def test_activity_map_and_stats_precede_photos_on_detail_page(self):
        with tempfile.TemporaryDirectory() as media_root:
            with override_settings(MEDIA_ROOT=media_root):
                post = self._make_activity_post(slug="morning-run-detail", with_track=True)

                response = self.client.get(reverse("post", kwargs={"slug": post.slug}))
                content = response.content.decode()

                map_index = content.index("slide--map")
                stats_index = content.index("slide--stats")
                photo_index = content.index('class="u-photo"')
                self.assertLess(map_index, stats_index)
                self.assertLess(stats_index, photo_index)

    def test_activity_without_track_has_no_map_or_stats_slide(self):
        with tempfile.TemporaryDirectory() as media_root:
            with override_settings(MEDIA_ROOT=media_root):
                self._make_activity_post(slug="morning-run-no-track", with_track=False)

                response = self.client.get(reverse("posts"), {"kind": "activity"})

                self.assertNotContains(response, "slide--map")
                self.assertNotContains(response, "slide--stats")
                self.assertNotContains(response, "GPX track coming soon")
                self.assertContains(response, 'class="u-photo"')


class CommentSubmissionTests(TestCase):
    def setUp(self):
        self.post = Post.objects.create(
            title="Comment Post",
            slug="comment-post",
            content="text",
            published_on=timezone.now(),
        )

    def _enable_comments(self):
        settings_obj = SiteConfiguration.get_solo()
        settings_obj.comments_enabled = True
        settings_obj.save(update_fields=["comments_enabled"])

    def test_comments_disabled_blocks_post_and_hides_form(self):
        response = self.client.get(reverse("post", kwargs={"slug": self.post.slug}))
        self.assertNotContains(response, "Leave a comment")

        response = self.client.post(
            reverse("comment_create", kwargs={"slug": self.post.slug}),
            {
                "author_name": "Ada",
                "content": "Hello",
            },
        )
        self.assertEqual(response.status_code, 404)

    @override_settings(
        AKISMET_API_KEY="test",
        TURNSTILE_SITE_KEY="site",
        TURNSTILE_SECRET_KEY="secret",
    )
    def test_turnstile_failure_rejects_comment(self):
        self._enable_comments()
        with patch("blog.views.verify_turnstile", return_value=(False, ["invalid-input-response"])):
            response = self.client.post(
                reverse("comment_create", kwargs={"slug": self.post.slug}),
                {
                    "author_name": "Ada",
                    "content": "Hello",
                    "cf-turnstile-response": "token",
                },
            )

        self.assertEqual(Comment.objects.count(), 0)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Turnstile verification failed")

    @override_settings(
        AKISMET_API_KEY="test",
        TURNSTILE_SITE_KEY="site",
        TURNSTILE_SECRET_KEY="secret",
    )
    def test_akismet_spam_response_saves_spam_comment(self):
        self._enable_comments()
        with patch("blog.views.verify_turnstile", return_value=(True, [])):
            with patch(
                "blog.views.check_comment",
                return_value=AkismetResult(True, "spam", "hash", None),
            ):
                response = self.client.post(
                    reverse("comment_create", kwargs={"slug": self.post.slug}),
                    {
                        "author_name": "Ada",
                        "content": "Spammy",
                        "cf-turnstile-response": "token",
                    },
                )

        self.assertEqual(response.status_code, 302)
        comment = Comment.objects.get()
        self.assertEqual(comment.status, Comment.SPAM)

    @override_settings(
        AKISMET_API_KEY="test",
        TURNSTILE_SITE_KEY="site",
        TURNSTILE_SECRET_KEY="secret",
    )
    def test_akismet_error_rejects_comment(self):
        self._enable_comments()
        with patch("blog.views.verify_turnstile", return_value=(True, [])):
            with patch("blog.views.check_comment", side_effect=AkismetError("Boom")):
                response = self.client.post(
                    reverse("comment_create", kwargs={"slug": self.post.slug}),
                    {
                        "author_name": "Ada",
                        "content": "Hello",
                        "cf-turnstile-response": "token",
                    },
                )

        self.assertEqual(Comment.objects.count(), 0)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Unable to verify comment content")

    @override_settings(
        AKISMET_API_KEY="test",
        TURNSTILE_SITE_KEY="site",
        TURNSTILE_SECRET_KEY="secret",
    )
    def test_invalid_referrer_is_dropped(self):
        self._enable_comments()
        with patch("blog.views.verify_turnstile", return_value=(True, [])):
            with patch(
                "blog.views.check_comment",
                return_value=AkismetResult(False, "ham", "hash", None),
            ):
                response = self.client.post(
                    reverse("comment_create", kwargs={"slug": self.post.slug}),
                    {
                        "author_name": "Ada",
                        "content": "Hello",
                        "cf-turnstile-response": "token",
                    },
                    HTTP_REFERER="not a url",
                )

        self.assertEqual(response.status_code, 302)
        comment = Comment.objects.get()
        self.assertEqual(comment.referrer, "")

    @override_settings(
        AKISMET_API_KEY="test",
        TURNSTILE_SITE_KEY="site",
        TURNSTILE_SECRET_KEY="secret",
    )
    def test_referrer_is_truncated_to_field_limit(self):
        self._enable_comments()
        max_length = Comment._meta.get_field("referrer").max_length
        long_referrer = "https://example.com/" + ("a" * (max_length + 50))
        with patch("blog.views.verify_turnstile", return_value=(True, [])):
            with patch(
                "blog.views.check_comment",
                return_value=AkismetResult(False, "ham", "hash", None),
            ):
                response = self.client.post(
                    reverse("comment_create", kwargs={"slug": self.post.slug}),
                    {
                        "author_name": "Ada",
                        "content": "Hello",
                        "cf-turnstile-response": "token",
                    },
                    HTTP_REFERER=long_referrer,
                )

        self.assertEqual(response.status_code, 302)
        comment = Comment.objects.get()
        self.assertTrue(comment.referrer.startswith("https://example.com/"))
        self.assertLessEqual(len(comment.referrer), max_length)

    @override_settings(
        AKISMET_API_KEY="test",
        TURNSTILE_SITE_KEY="site",
        TURNSTILE_SECRET_KEY="secret",
    )
    def test_only_approved_comments_render_on_post(self):
        self._enable_comments()
        Comment.objects.create(
            post=self.post,
            author_name="Approved",
            content="Visible",
            status=Comment.APPROVED,
        )
        Comment.objects.create(
            post=self.post,
            author_name="Pending",
            content="Hidden",
            status=Comment.PENDING,
        )
        Comment.objects.create(
            post=self.post,
            author_name="Spam",
            content="Also hidden",
            status=Comment.SPAM,
        )

        response = self.client.get(reverse("post", kwargs={"slug": self.post.slug}))
        self.assertContains(response, "Visible")
        self.assertNotContains(response, "Hidden")
        self.assertNotContains(response, "Also hidden")


class ScheduledPostVisibilityTests(TestCase):
    def setUp(self):
        self.scheduled = Post.objects.create(
            title="Future Post",
            slug="future-post",
            content="not yet",
            kind=Post.NOTE,
            published_on=timezone.now() + timezone.timedelta(days=1),
        )
        self.published = Post.objects.create(
            title="Past Post",
            slug="past-post",
            content="already out",
            kind=Post.NOTE,
            published_on=timezone.now() - timezone.timedelta(hours=1),
        )

    def test_live_excludes_scheduled_drafts_and_deleted(self):
        Post.objects.create(title="Draft", slug="draft", content="text")
        Post.objects.create(
            title="Gone",
            slug="gone",
            content="text",
            published_on=timezone.now() - timezone.timedelta(hours=1),
            deleted=True,
        )

        self.assertEqual(list(Post.objects.live()), [self.published])
        self.assertTrue(self.published.is_live())
        self.assertFalse(self.scheduled.is_live())

    def test_scheduled_post_is_404_when_logged_out(self):
        response = self.client.get(reverse("post", kwargs={"slug": self.scheduled.slug}))

        self.assertEqual(response.status_code, 404)

    def test_logged_in_user_can_view_scheduled_post(self):
        user = get_user_model().objects.create_user(
            username="owner", email="owner@example.com", password="password"
        )
        self.client.force_login(user)

        response = self.client.get(reverse("post", kwargs={"slug": self.scheduled.slug}))

        self.assertEqual(response.status_code, 200)

    def test_scheduled_post_not_in_listing(self):
        response = self.client.get(reverse("posts"), follow=True)

        self.assertContains(response, "/blog/post/past-post/")
        self.assertNotContains(response, "future-post")

    def test_scheduled_post_not_in_feed(self):
        response = self.client.get(reverse("posts_feed"))

        self.assertContains(response, "past-post")
        self.assertNotContains(response, "future-post")

    def test_scheduled_post_not_in_sitemap(self):
        response = self.client.get(reverse("sitemap"))

        self.assertContains(response, "past-post")
        self.assertNotContains(response, "future-post")


@patch("mastodon_integration.tasks.publish_post_to_mastodon.delay")
@patch("micropub.tasks.dispatch_webmentions.delay")
class GoLiveSideEffectTests(TestCase):
    source_url = "https://example.com/blog/post/x/"

    def test_draft_sends_nothing(self, dispatch, mastodon):
        from micropub.webmention import queue_webmentions_for_post

        post = Post.objects.create(title="Draft", slug="draft", content="text")
        queue_webmentions_for_post(post, self.source_url, include_bridgy=True)

        dispatch.assert_not_called()
        mastodon.assert_not_called()

    def test_scheduled_post_sends_nothing_until_due(self, dispatch, mastodon):
        from micropub.tasks import publish_due_posts
        from micropub.webmention import queue_webmentions_for_post

        post = Post.objects.create(
            title="Later",
            slug="later",
            content="text",
            published_on=timezone.now() + timezone.timedelta(hours=1),
        )
        queue_webmentions_for_post(post, self.source_url, include_bridgy=True)
        publish_due_posts()

        dispatch.assert_not_called()
        mastodon.assert_not_called()
        post.refresh_from_db()
        self.assertIsNone(post.went_live_at)

    @override_settings(MICROSUB_BASE_URL="https://example.com")
    def test_publish_due_posts_sends_once_when_time_passes(self, dispatch, mastodon):
        from micropub.tasks import publish_due_posts

        post = Post.objects.create(
            title="Due",
            slug="due",
            content="text",
            published_on=timezone.now() - timezone.timedelta(minutes=1),
        )

        publish_due_posts()
        publish_due_posts()

        dispatch.assert_called_once_with(
            post.id, "https://example.com/blog/post/due/", include_bridgy=True
        )
        mastodon.assert_called_once_with(post.id)
        post.refresh_from_db()
        self.assertIsNotNone(post.went_live_at)

    def test_first_go_live_always_includes_bridgy(self, dispatch, mastodon):
        from micropub.webmention import queue_webmentions_for_post

        post = Post.objects.create(
            title="Now",
            slug="now",
            content="text",
            published_on=timezone.now() - timezone.timedelta(minutes=1),
        )
        queue_webmentions_for_post(post, self.source_url, include_bridgy=False)
        queue_webmentions_for_post(post, self.source_url, include_bridgy=False)

        self.assertEqual(
            [call.kwargs["include_bridgy"] for call in dispatch.call_args_list],
            [True, False],
        )

    def test_already_live_posts_are_not_picked_up(self, dispatch, mastodon):
        from micropub.tasks import publish_due_posts

        Post.objects.create(
            title="Old",
            slug="old",
            content="text",
            published_on=timezone.now() - timezone.timedelta(days=30),
            went_live_at=timezone.now() - timezone.timedelta(days=30),
        )

        publish_due_posts()

        dispatch.assert_not_called()
        mastodon.assert_not_called()


class MastodonScheduledGuardTests(TestCase):
    @patch("mastodon_integration.tasks._should_syndicate", return_value=True)
    def test_scheduled_post_is_not_tooted(self, should_syndicate):
        from mastodon_integration.models import MastodonPost
        from mastodon_integration.tasks import publish_post_to_mastodon

        post = Post.objects.create(
            title="Later",
            slug="later-toot",
            content="text",
            published_on=timezone.now() + timezone.timedelta(hours=1),
        )

        publish_post_to_mastodon(post.id)

        should_syndicate.assert_not_called()
        self.assertFalse(MastodonPost.objects.filter(post=post).exists())


@patch("micropub.webmention.queue_webmentions_for_post")
class ContentServiceTests(TestCase):
    def setUp(self):
        from blog.services import Actor

        self.user = get_user_model().objects.create_user(username="author", password="pw")
        self.actor = Actor(user=self.user, source="mcp", base_url="https://example.com")

    def test_create_defaults_to_draft(self, queue):
        from blog.services import DRAFT, create_post, post_status

        with self.captureOnCommitCallbacks(execute=True):
            post = create_post(self.actor, kind=Post.NOTE, content="hi", tags=["a tag"])

        self.assertEqual(post_status(post), DRAFT)
        self.assertEqual(post.author, self.user)
        self.assertEqual(list(post.tags.values_list("tag", flat=True)), ["a-tag"])
        queue.assert_called_once()
        self.assertFalse(Post.objects.live().filter(pk=post.pk).exists())

    def test_create_published_queues_side_effects_with_source_url(self, queue):
        from blog.services import PUBLISHED, create_post

        with self.captureOnCommitCallbacks(execute=True):
            post = create_post(self.actor, kind=Post.LIKE, like_of="https://other.example/x", status=PUBLISHED)

        self.assertTrue(post.is_live())
        self.assertEqual(post.content, "Liked https://other.example/x")
        self.assertEqual(queue.call_args.args[1], f"https://example.com{post.get_absolute_url()}")

    def test_create_published_in_future_is_scheduled(self, queue):
        from blog.services import PUBLISHED, SCHEDULED, create_post, post_status

        at = timezone.now() + timezone.timedelta(days=1)
        post = create_post(self.actor, kind=Post.NOTE, content="later", status=PUBLISHED, published_on=at)

        self.assertEqual(post_status(post), SCHEDULED)
        self.assertEqual(post.published_on, at)

    def test_create_rejects_unknown_kind(self, queue):
        from blog.services import ContentError, create_post

        with self.assertRaises(ContentError):
            create_post(self.actor, kind="podcast", content="hi")
        self.assertFalse(Post.objects.exists())

    def test_create_uses_normalized_slug(self, queue):
        from blog.services import create_post

        post = create_post(self.actor, kind=Post.ARTICLE, name="Hello", content="Body", slug="My Cool Post!")

        self.assertEqual(post.slug, "my-cool-post")

    def test_create_dedupes_colliding_slug(self, queue):
        from blog.services import create_post

        Post.objects.create(title="Existing", slug="my-cool-post", content="x")
        Post.objects.create(title="Existing 2", slug="my-cool-post-2", content="x")

        post = create_post(self.actor, kind=Post.ARTICLE, name="Hello", content="Body", slug="my-cool-post")

        self.assertEqual(post.slug, "my-cool-post-3")

    def test_create_empty_slug_falls_back_to_title_timestamp(self, queue):
        from blog.services import create_post

        frozen = timezone.now()
        with patch("blog.models.timezone.now", return_value=frozen):
            blank = create_post(self.actor, kind=Post.ARTICLE, name="Hello World", content="Body", slug="")
            punctuation = create_post(
                self.actor, kind=Post.ARTICLE, name="Another Title", content="Body", slug="!!!"
            )
            omitted = create_post(self.actor, kind=Post.ARTICLE, name="Plain Title", content="Body")

        timestamp = int(frozen.timestamp())
        self.assertEqual(blank.slug, f"hello-world-{timestamp}")
        self.assertEqual(punctuation.slug, f"another-title-{timestamp}")
        self.assertEqual(omitted.slug, f"plain-title-{timestamp}")

    def test_create_slug_respects_max_length(self, queue):
        from blog.services import create_post

        max_length = Post._meta.get_field("slug").max_length
        post = create_post(self.actor, kind=Post.NOTE, content="x", slug="a" * 400)

        self.assertEqual(post.slug, "a" * max_length)

        Post.objects.create(title="Full", slug="b" * max_length, content="x")
        deduped = create_post(self.actor, kind=Post.NOTE, content="x", slug="b" * 400)

        self.assertLessEqual(len(deduped.slug), max_length)
        self.assertNotEqual(deduped.slug, "b" * max_length)
        self.assertTrue(deduped.slug.startswith("b"))

    def test_set_status_publishes_and_unpublishes(self, queue):
        from blog.services import DRAFT, PUBLISHED, create_post, set_status

        post = create_post(self.actor, kind=Post.NOTE, content="hi")
        set_status(self.actor, post, PUBLISHED)
        self.assertTrue(post.is_live())

        set_status(self.actor, post, DRAFT)
        self.assertIsNone(post.published_on)

    def test_set_status_keeps_date_of_live_post(self, queue):
        from blog.services import PUBLISHED, set_status

        published_on = timezone.now() - timezone.timedelta(days=3)
        post = Post.objects.create(title="Old", slug="old", content="hi", published_on=published_on)

        set_status(self.actor, post, PUBLISHED)

        self.assertEqual(post.published_on, published_on)

    def test_update_does_not_replace_existing_author(self, queue):
        from blog.services import update_post

        other = get_user_model().objects.create_user(username="other", password="pw")
        post = Post.objects.create(title="T", slug="t", content="old", author=other)

        update_post(self.actor, post, replace={"content": ["new"]})

        post.refresh_from_db()
        self.assertEqual(post.content, "new")
        self.assertEqual(post.author, other)

    def test_update_post_status_publishes_draft(self, queue):
        from blog.services import create_post, update_post

        post = create_post(self.actor, kind=Post.NOTE, content="hi")
        update_post(self.actor, post, replace={"post-status": ["published"]})

        self.assertTrue(post.is_live())

    def test_update_rejects_location_on_non_checkin(self, queue):
        from blog.services import ContentError, update_post

        post = Post.objects.create(title="T", slug="t", content="hi", kind=Post.NOTE)

        with self.assertRaises(ContentError):
            update_post(self.actor, post, replace={"location": ["geo:1,2"]})


@patch("micropub.webmention.queue_webmentions_for_post")
class PostRevisionTests(TestCase):
    def setUp(self):
        from blog.services import Actor

        self.user = get_user_model().objects.create_user(username="author", password="pw")
        self.actor = Actor(user=self.user, source="mcp", client_id="https://client.example/")
        media_root = tempfile.TemporaryDirectory()
        self.addCleanup(media_root.cleanup)
        media_override = override_settings(MEDIA_ROOT=media_root.name)
        media_override.enable()
        self.addCleanup(media_override.disable)

    def _photo(self, post, sort_order=0):
        upload = SimpleUploadedFile("photo.jpg", b"fake-image-data", content_type="image/jpeg")
        asset = File.objects.create(kind=File.IMAGE, file=upload)
        Attachment.objects.create(content_object=post, asset=asset, role="photo", sort_order=sort_order)
        return asset

    def test_create_records_who_without_snapshot(self, queue):
        from blog.models import PostRevision
        from blog.services import create_post

        post = create_post(self.actor, kind=Post.NOTE, content="hi")

        revision = post.revisions.get()
        self.assertEqual(revision.action, PostRevision.CREATE)
        self.assertIsNone(revision.snapshot)
        self.assertEqual(revision.actor_source, "mcp")
        self.assertEqual(revision.actor_user, self.user)
        self.assertEqual(revision.client_id, "https://client.example/")

    def test_each_change_records_the_state_before_it(self, queue):
        from blog.models import PostRevision
        from blog.services import PUBLISHED, create_post, delete_post, set_status, undelete_post, update_post

        post = create_post(self.actor, kind=Post.NOTE, content="first", tags=["one"])
        update_post(self.actor, post, replace={"content": ["second"]}, add={"category": ["two"]})
        set_status(self.actor, post, PUBLISHED)
        delete_post(self.actor, post)
        undelete_post(self.actor, post)

        revisions = list(post.revisions.order_by("id"))
        self.assertEqual(
            [r.action for r in revisions],
            [
                PostRevision.CREATE,
                PostRevision.UPDATE,
                PostRevision.STATUS,
                PostRevision.DELETE,
                PostRevision.UNDELETE,
            ],
        )
        update, status, delete, undelete = revisions[1:]
        self.assertEqual(update.snapshot["content"], "first")
        self.assertEqual(update.snapshot["tags"], ["one"])
        self.assertEqual(update.change_summary, "replace content; add category")
        self.assertEqual(status.snapshot["content"], "second")
        self.assertEqual(status.snapshot["tags"], ["one", "two"])
        self.assertIsNone(status.snapshot["published_on"])
        self.assertIsNotNone(delete.snapshot["published_on"])
        self.assertFalse(delete.snapshot["deleted"])
        self.assertTrue(undelete.snapshot["deleted"])

    def test_noop_delete_and_undelete_record_nothing(self, queue):
        from blog.services import create_post, delete_post, undelete_post

        post = create_post(self.actor, kind=Post.NOTE, content="hi")
        undelete_post(self.actor, post)
        delete_post(self.actor, post)
        delete_post(self.actor, post)

        self.assertEqual(post.revisions.count(), 2)

    def test_failed_update_records_nothing(self, queue):
        from blog.services import ContentError, create_post, update_post

        post = create_post(self.actor, kind=Post.NOTE, content="hi")

        with self.assertRaises(ContentError):
            update_post(self.actor, post, replace={"post-status": ["archived"]})

        self.assertEqual(post.revisions.count(), 1)

    def test_saving_rolls_back_change_and_revision_on_error(self, queue):
        from blog.services import saving

        post = Post.objects.create(title="T", slug="t", content="before")

        with self.assertRaises(RuntimeError):
            with saving(self.actor, post):
                post.content = "after"
                post.save()
                raise RuntimeError("boom")

        post.refresh_from_db()
        self.assertEqual(post.content, "before")
        self.assertFalse(post.revisions.exists())
        queue.assert_not_called()

    def test_revert_restores_content_tags_and_photos(self, queue):
        from blog.services import create_post, revert_to, update_post

        post = create_post(self.actor, kind=Post.ARTICLE, name="Title", content="original", tags=["keep", "drop"])
        first = self._photo(post, sort_order=0)
        second = self._photo(post, sort_order=1)

        update_post(
            self.actor,
            post,
            replace={"content": ["rewritten"], "name": ["New title"]},
            delete={"category": ["drop"], "photo": [first.file.url]},
        )
        update_post(self.actor, post, delete={"photo": []}, add={"category": ["extra"]})
        before_edits = post.revisions.order_by("id")[1]

        post, missing = revert_to(self.actor, post, before_edits)

        post.refresh_from_db()
        self.assertEqual(missing, [])
        self.assertEqual(post.title, "Title")
        self.assertEqual(post.content, "original")
        self.assertEqual(sorted(post.tags.values_list("tag", flat=True)), ["drop", "keep"])
        self.assertEqual(
            list(post.attachments.values_list("asset_id", "sort_order")),
            [(first.pk, 0), (second.pk, 1)],
        )

    def test_revert_is_itself_undoable(self, queue):
        from blog.models import PostRevision
        from blog.services import create_post, revert_to, update_post

        post = create_post(self.actor, kind=Post.NOTE, content="one")
        update_post(self.actor, post, replace={"content": ["two"]})
        revert_to(self.actor, post, post.revisions.get(action=PostRevision.UPDATE))
        self.assertEqual(post.content, "one")

        revert_to(self.actor, post, post.revisions.get(action=PostRevision.REVERT))

        post.refresh_from_db()
        self.assertEqual(post.content, "two")

    def test_revert_restores_status(self, queue):
        from blog.models import PostRevision
        from blog.services import DRAFT, PUBLISHED, create_post, delete_post, post_status, revert_to, set_status

        post = create_post(self.actor, kind=Post.NOTE, content="hi")
        set_status(self.actor, post, PUBLISHED)
        delete_post(self.actor, post)

        revert_to(self.actor, post, post.revisions.get(action=PostRevision.STATUS))

        post.refresh_from_db()
        self.assertEqual(post_status(post), DRAFT)

    def test_revert_skips_photos_that_were_deleted_since(self, queue):
        from blog.services import create_post, revert_to, update_post

        post = create_post(self.actor, kind=Post.PHOTO, content="hi")
        asset = self._photo(post)
        update_post(self.actor, post, delete={"photo": []})
        asset_id = asset.pk
        asset.delete()

        post, missing = revert_to(self.actor, post, post.revisions.order_by("id")[1])

        self.assertEqual(missing, [asset_id])
        self.assertFalse(post.attachments.exists())

    def test_revert_rejects_create_revision_and_other_posts(self, queue):
        from blog.services import ContentError, create_post, revert_to, update_post

        post = create_post(self.actor, kind=Post.NOTE, content="hi")
        other = create_post(self.actor, kind=Post.NOTE, content="other")
        update_post(self.actor, other, replace={"content": ["changed"]})

        with self.assertRaises(ContentError):
            revert_to(self.actor, post, post.revisions.get())
        with self.assertRaises(ContentError):
            revert_to(self.actor, post, other.revisions.order_by("id")[1])
        self.assertEqual(post.revisions.count(), 1)

    def test_revert_rejects_slug_taken_by_another_post(self, queue):
        from blog.services import ContentError, update_post, revert_to

        post = Post.objects.create(title="T", slug="original-slug", content="hi")
        update_post(self.actor, post, replace={"content": ["changed"]})
        Post.objects.filter(pk=post.pk).update(slug="renamed")
        post.refresh_from_db()
        Post.objects.create(title="Squatter", slug="original-slug", content="x")

        with self.assertRaises(ContentError):
            revert_to(self.actor, post, post.revisions.get())

        post.refresh_from_db()
        self.assertEqual(post.content, "changed")


class PostPreviewTests(TestCase):
    def setUp(self):
        self.draft = Post.objects.create(title="Draft", slug="draft-post", content="secret draft", kind=Post.NOTE)

    def _get(self, post, token=None):
        from blog.previews import make_token

        token = make_token(post) if token is None else token
        return self.client.get(reverse("post", kwargs={"slug": post.slug}), {"preview": token})

    @patch("analytics.tasks.record_visit.delay")
    def test_valid_link_shows_draft_logged_out(self, record_visit):
        response = self._get(self.draft)

        self.assertContains(response, "secret draft")
        self.assertContains(response, "Preview: this post is")
        self.assertContains(response, "a draft")
        self.assertEqual(response["X-Robots-Tag"], "noindex, nofollow")
        self.assertEqual(response["Cache-Control"], "private, no-store")
        self.assertEqual(response["Referrer-Policy"], "no-referrer")
        record_visit.assert_not_called()

    def test_banner_is_inside_body(self):
        content = self._get(self.draft).content.decode()

        self.assertLess(content.index("<body"), content.index("Preview: this post is"))

    def test_scheduled_post_banner_says_when(self):
        scheduled = Post.objects.create(
            title="Later",
            slug="later-post",
            content="soon",
            published_on=timezone.now() + timezone.timedelta(days=1),
        )

        self.assertContains(self._get(scheduled), "scheduled for")

    def test_missing_or_tampered_token_is_404(self):
        from blog.previews import make_token

        self.assertEqual(self.client.get(reverse("post", kwargs={"slug": self.draft.slug})).status_code, 404)
        self.assertEqual(self._get(self.draft, token="nonsense").status_code, 404)
        self.assertEqual(self._get(self.draft, token=make_token(self.draft) + "x").status_code, 404)

    def test_token_for_another_post_is_404(self):
        from blog.previews import make_token

        other = Post.objects.create(title="Other", slug="other-draft", content="x")

        self.assertEqual(self._get(self.draft, token=make_token(other)).status_code, 404)

    def test_expired_token_is_404(self):
        import time

        from blog.previews import PREVIEW_MAX_AGE, make_token

        token = make_token(self.draft)
        later = time.time() + PREVIEW_MAX_AGE.total_seconds() + 60
        with patch("django.core.signing.time.time", return_value=later):
            response = self._get(self.draft, token=token)

        self.assertEqual(response.status_code, 404)

    def test_deleted_post_is_404(self):
        from blog.previews import make_token

        token = make_token(self.draft)
        self.draft.deleted = True
        self.draft.save()

        self.assertEqual(self._get(self.draft, token=token).status_code, 404)

    @patch("analytics.tasks.record_visit.delay")
    def test_live_post_ignores_token_and_has_no_banner(self, record_visit):
        live = Post.objects.create(
            title="Live",
            slug="live-post",
            content="out",
            published_on=timezone.now() - timezone.timedelta(hours=1),
        )

        response = self._get(live)

        self.assertContains(response, "out")
        self.assertNotContains(response, "Preview: this post is")
        self.assertNotIn("X-Robots-Tag", response)
        record_visit.assert_called_once()

    def test_logged_in_user_sees_banner_on_draft(self):
        user = get_user_model().objects.create_user(username="owner", password="pw")
        self.client.force_login(user)

        response = self.client.get(reverse("post", kwargs={"slug": self.draft.slug}))

        self.assertContains(response, "Preview: this post is")
        self.assertEqual(response["X-Robots-Tag"], "noindex, nofollow")

    def test_service_preview_url(self):
        from blog.services import Actor, preview_url

        actor = Actor(base_url="https://example.com")
        url = preview_url(actor, self.draft)

        self.assertTrue(url.startswith("https://example.com/blog/post/draft-post/?preview="))
        self.draft.published_on = timezone.now() - timezone.timedelta(minutes=1)
        self.assertIsNone(preview_url(actor, self.draft))
