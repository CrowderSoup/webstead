"""Tests for microsub/feed_parser.py."""
import json
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase

from microsub.feed_parser import (
    _HubLinkParser,
    _apply_feed_author_fallback,
    _author_from_mf2,
    _hentry_to_jf2,
    _mf2_embedded_to_jf2,
    _parse_hfeed,
    _parse_json_feed,
    _parse_link_header_for_rel,
    _parse_rss_atom,
    discover_websub_hub,
    fetch_and_parse_feed,
)


class ParseLinkHeaderTests(SimpleTestCase):
    def test_returns_url_for_matching_rel(self):
        header = '<https://hub.example.com/>; rel="hub"'
        result = _parse_link_header_for_rel(header, "hub")
        self.assertEqual(result, "https://hub.example.com/")

    def test_returns_none_when_rel_not_found(self):
        header = '<https://example.com/feed>; rel="self"'
        self.assertIsNone(_parse_link_header_for_rel(header, "hub"))

    def test_multiple_links_returns_correct_one(self):
        header = '<https://self.example.com/>; rel="self", <https://hub.example.com/>; rel="hub"'
        result = _parse_link_header_for_rel(header, "hub")
        self.assertEqual(result, "https://hub.example.com/")

    def test_empty_string_returns_none(self):
        self.assertIsNone(_parse_link_header_for_rel("", "hub"))

    def test_none_returns_none(self):
        self.assertIsNone(_parse_link_header_for_rel(None, "hub"))

    def test_malformed_segment_skipped(self):
        header = 'no-angle-bracket; rel="hub"'
        self.assertIsNone(_parse_link_header_for_rel(header, "hub"))


class HubLinkParserTests(SimpleTestCase):
    def test_parses_hub_link(self):
        p = _HubLinkParser()
        p.feed('<link rel="hub" href="https://hub.example.com/">')
        self.assertEqual(p.hub_url, "https://hub.example.com/")

    def test_only_first_hub_captured(self):
        p = _HubLinkParser()
        p.feed('<link rel="hub" href="https://hub1.example.com/"><link rel="hub" href="https://hub2.example.com/">')
        self.assertEqual(p.hub_url, "https://hub1.example.com/")

    def test_parses_rss_alternate_feed(self):
        p = _HubLinkParser()
        p.feed('<link rel="alternate" type="application/rss+xml" href="/feed.rss">')
        self.assertEqual(p.feed_url, "/feed.rss")

    def test_ignores_alternate_without_feed_type(self):
        p = _HubLinkParser()
        p.feed('<link rel="alternate" type="text/html" href="/page">')
        self.assertIsNone(p.feed_url)

    def test_non_link_tags_ignored(self):
        p = _HubLinkParser()
        p.feed('<meta name="author" content="foo">')
        self.assertIsNone(p.hub_url)


class DiscoverWebsubHubTests(SimpleTestCase):
    def test_hub_from_link_header(self):
        result = discover_websub_hub(
            "https://example.com/",
            '<https://hub.example.com/>; rel="hub"',
        )
        self.assertEqual(result, "https://hub.example.com/")

    def test_hub_from_html_when_no_link_header(self):
        html = '<link rel="hub" href="https://hub.example.com/">'
        result = discover_websub_hub("https://example.com/", None, html)
        self.assertEqual(result, "https://hub.example.com/")

    def test_relative_hub_url_resolved(self):
        html = '<link rel="hub" href="/websub">'
        result = discover_websub_hub("https://example.com/page", None, html)
        self.assertEqual(result, "https://example.com/websub")

    def test_none_when_no_hub_anywhere(self):
        self.assertIsNone(discover_websub_hub("https://example.com/", None, None))

    def test_link_header_takes_precedence_over_html(self):
        html = '<link rel="hub" href="https://html-hub.example.com/">'
        result = discover_websub_hub(
            "https://example.com/",
            '<https://header-hub.example.com/>; rel="hub"',
            html,
        )
        self.assertEqual(result, "https://header-hub.example.com/")


class AuthorFromMf2Tests(SimpleTestCase):
    def test_string_author_returns_card_with_url(self):
        result = _author_from_mf2("https://author.example.com/", "https://example.com/")
        self.assertEqual(result, {"type": "card", "url": "https://author.example.com/"})

    def test_dict_author_returns_full_card(self):
        author = {
            "type": ["h-card"],
            "properties": {
                "name": ["Alice"],
                "url": ["https://alice.example.com/"],
            },
        }
        result = _author_from_mf2(author, "https://example.com/")
        self.assertEqual(result["type"], "card")
        self.assertEqual(result["name"], "Alice")
        self.assertEqual(result["url"], "https://alice.example.com/")

    def test_empty_hcard_returns_none(self):
        author = {"type": ["h-card"], "properties": {}}
        result = _author_from_mf2(author, "https://example.com/")
        self.assertIsNone(result)


class HentryToJf2Tests(SimpleTestCase):
    def _make_hentry(self, props):
        return {"type": ["h-entry"], "properties": props}

    def test_url_is_included(self):
        item = self._make_hentry({"url": ["https://example.com/post"]})
        result = _hentry_to_jf2(item, "https://example.com/")
        self.assertEqual(result["url"], "https://example.com/post")

    def test_relative_url_resolved(self):
        item = self._make_hentry({"url": ["/post/1"]})
        result = _hentry_to_jf2(item, "https://example.com/")
        self.assertTrue(result["url"].startswith("https://example.com"))

    def test_uid_falls_back_to_url(self):
        item = self._make_hentry({"url": ["https://example.com/post"]})
        result = _hentry_to_jf2(item, "https://example.com/")
        self.assertIn("_uid", result)

    def test_name_included(self):
        item = self._make_hentry({"name": ["My Post"], "url": ["https://example.com/"]})
        result = _hentry_to_jf2(item, "https://example.com/")
        self.assertEqual(result["name"], "My Post")

    def test_content_dict_with_html(self):
        item = self._make_hentry({"content": [{"html": "<b>hi</b>", "value": "hi"}]})
        result = _hentry_to_jf2(item, "https://example.com/")
        self.assertEqual(result["content"]["html"], "<b>hi</b>")
        self.assertEqual(result["content"]["text"], "hi")

    def test_content_plain_string(self):
        item = self._make_hentry({"content": ["plain text"]})
        result = _hentry_to_jf2(item, "https://example.com/")
        self.assertEqual(result["content"]["text"], "plain text")

    def test_in_reply_to_included(self):
        item = self._make_hentry({"in-reply-to": ["https://target.example.com/"]})
        result = _hentry_to_jf2(item, "https://example.com/")
        self.assertIn("in-reply-to", result)

    def test_like_of_included(self):
        item = self._make_hentry({"like-of": ["https://liked.example.com/"]})
        result = _hentry_to_jf2(item, "https://example.com/")
        self.assertIn("like-of", result)

    def test_repost_of_included(self):
        item = self._make_hentry({"repost-of": ["https://reposted.example.com/"]})
        result = _hentry_to_jf2(item, "https://example.com/")
        self.assertIn("repost-of", result)

    def test_empty_props_returns_type_entry(self):
        item = self._make_hentry({})
        result = _hentry_to_jf2(item, "https://example.com/")
        self.assertEqual(result["type"], "entry")

    def test_checkin_embedded_hcard(self):
        item = self._make_hentry({
            "checkin": [{
                "type": ["h-card"],
                "properties": {
                    "name": ["Coffee Shop"],
                    "latitude": ["37.7749"],
                    "longitude": ["-122.4194"],
                },
            }]
        })
        result = _hentry_to_jf2(item, "https://example.com/")
        self.assertIn("checkin", result)
        self.assertEqual(result["checkin"]["name"], "Coffee Shop")
        self.assertEqual(result["checkin"]["latitude"], "37.7749")
        self.assertEqual(result["checkin"]["longitude"], "-122.4194")

    def test_checkin_string_url(self):
        item = self._make_hentry({"checkin": ["https://venue.example.com/"]})
        result = _hentry_to_jf2(item, "https://example.com/")
        self.assertEqual(result["checkin"], {"type": "card", "url": "https://venue.example.com/"})

    def test_location_embedded_hadr(self):
        item = self._make_hentry({
            "location": [{
                "type": ["h-adr"],
                "properties": {
                    "locality": ["San Francisco"],
                    "region": ["CA"],
                },
            }]
        })
        result = _hentry_to_jf2(item, "https://example.com/")
        self.assertIn("location", result)
        self.assertEqual(result["location"]["type"], "adr")
        self.assertEqual(result["location"]["locality"], "San Francisco")
        self.assertEqual(result["location"]["region"], "CA")

    def test_photo_single(self):
        item = self._make_hentry({"photo": ["https://example.com/photo.jpg"]})
        result = _hentry_to_jf2(item, "https://example.com/")
        self.assertEqual(result["photo"], ["https://example.com/photo.jpg"])

    def test_photo_multiple(self):
        item = self._make_hentry({"photo": [
            "https://example.com/photo1.jpg",
            "https://example.com/photo2.jpg",
        ]})
        result = _hentry_to_jf2(item, "https://example.com/")
        self.assertEqual(len(result["photo"]), 2)

    def test_photo_dict_value(self):
        item = self._make_hentry({"photo": [{"value": "https://example.com/photo.jpg"}]})
        result = _hentry_to_jf2(item, "https://example.com/")
        self.assertEqual(result["photo"], ["https://example.com/photo.jpg"])

    def test_video_included(self):
        item = self._make_hentry({"video": ["https://example.com/video.mp4"]})
        result = _hentry_to_jf2(item, "https://example.com/")
        self.assertEqual(result["video"], ["https://example.com/video.mp4"])

    def test_audio_included(self):
        item = self._make_hentry({"audio": ["https://example.com/episode.mp3"]})
        result = _hentry_to_jf2(item, "https://example.com/")
        self.assertEqual(result["audio"], ["https://example.com/episode.mp3"])

    def test_syndication_included(self):
        item = self._make_hentry({"syndication": ["https://twitter.com/user/status/123"]})
        result = _hentry_to_jf2(item, "https://example.com/")
        self.assertEqual(result["syndication"], ["https://twitter.com/user/status/123"])

    def test_category_included(self):
        item = self._make_hentry({"category": ["IndieWeb", "python", "webdev"]})
        result = _hentry_to_jf2(item, "https://example.com/")
        self.assertEqual(result["category"], ["IndieWeb", "python", "webdev"])

    def test_summary_included(self):
        item = self._make_hentry({"summary": ["A brief summary of the post."]})
        result = _hentry_to_jf2(item, "https://example.com/")
        self.assertEqual(result["summary"], "A brief summary of the post.")

    def test_updated_included(self):
        item = self._make_hentry({"updated": ["2024-06-01T12:00:00Z"]})
        result = _hentry_to_jf2(item, "https://example.com/")
        self.assertEqual(result["updated"], "2024-06-01T12:00:00Z")

    def test_rsvp_normalized(self):
        item = self._make_hentry({"rsvp": ["YES"]})
        result = _hentry_to_jf2(item, "https://example.com/")
        self.assertEqual(result["rsvp"], "yes")

    def test_listen_of_included(self):
        item = self._make_hentry({"listen-of": ["https://podcast.example.com/episode/1"]})
        result = _hentry_to_jf2(item, "https://example.com/")
        self.assertEqual(result["listen-of"], "https://podcast.example.com/episode/1")

    def test_watch_of_included(self):
        item = self._make_hentry({"watch-of": ["https://video.example.com/watch?v=abc"]})
        result = _hentry_to_jf2(item, "https://example.com/")
        self.assertEqual(result["watch-of"], "https://video.example.com/watch?v=abc")

    def test_read_of_included(self):
        item = self._make_hentry({"read-of": ["https://book.example.com/isbn/9780000000000"]})
        result = _hentry_to_jf2(item, "https://example.com/")
        self.assertEqual(result["read-of"], "https://book.example.com/isbn/9780000000000")


class Mf2EmbeddedToJf2Tests(SimpleTestCase):
    def test_string_input_returns_minimal_card(self):
        result = _mf2_embedded_to_jf2("https://venue.example.com/", "https://example.com/")
        self.assertEqual(result, {"type": "card", "url": "https://venue.example.com/"})

    def test_empty_string_returns_none(self):
        result = _mf2_embedded_to_jf2("", "https://example.com/")
        self.assertIsNone(result)

    def test_hcard_dict_returns_full_card(self):
        val = {
            "type": ["h-card"],
            "properties": {
                "name": ["Alice"],
                "url": ["https://alice.example.com/"],
                "email": ["alice@example.com"],
                "tel": ["+1-555-0100"],
            },
        }
        result = _mf2_embedded_to_jf2(val, "https://example.com/")
        self.assertIsNotNone(result)
        self.assertEqual(result["type"], "card")
        self.assertEqual(result["name"], "Alice")
        self.assertEqual(result["url"], "https://alice.example.com/")
        self.assertEqual(result["email"], "alice@example.com")
        self.assertEqual(result["tel"], "+1-555-0100")

    def test_hadr_dict_returns_adr_type(self):
        val = {
            "type": ["h-adr"],
            "properties": {
                "locality": ["Portland"],
                "region": ["OR"],
                "country-name": ["US"],
            },
        }
        result = _mf2_embedded_to_jf2(val, "https://example.com/")
        self.assertIsNotNone(result)
        self.assertEqual(result["type"], "adr")
        self.assertEqual(result["locality"], "Portland")
        self.assertEqual(result["region"], "OR")
        self.assertEqual(result["country"], "US")

    def test_returns_none_for_empty_dict(self):
        val = {"type": ["h-card"], "properties": {}}
        result = _mf2_embedded_to_jf2(val, "https://example.com/")
        self.assertIsNone(result)

    def test_non_string_non_dict_returns_none(self):
        result = _mf2_embedded_to_jf2(42, "https://example.com/")
        self.assertIsNone(result)


class ApplyFeedAuthorFallbackTests(SimpleTestCase):
    def _make_entry(self, author=None):
        e = {"type": "entry"}
        if author is not None:
            e["author"] = author
        return e

    def test_assigns_feed_author_when_entry_has_no_author(self):
        entry = self._make_entry()
        _apply_feed_author_fallback([entry], {"name": "Alice", "url": "https://alice.example.com/", "photo": ""})
        self.assertEqual(entry["author"]["name"], "Alice")
        self.assertEqual(entry["author"]["url"], "https://alice.example.com/")

    def test_assigns_feed_author_when_entry_author_lacks_url(self):
        entry = self._make_entry(author={"type": "card", "name": "Unknown"})
        _apply_feed_author_fallback([entry], {"name": "Alice", "url": "https://alice.example.com/", "photo": ""})
        self.assertEqual(entry["author"]["url"], "https://alice.example.com/")

    def test_does_not_overwrite_existing_author_url(self):
        entry = self._make_entry(author={"type": "card", "url": "https://bob.example.com/"})
        _apply_feed_author_fallback([entry], {"name": "Alice", "url": "https://alice.example.com/", "photo": ""})
        self.assertEqual(entry["author"]["url"], "https://bob.example.com/")

    def test_no_op_when_feed_meta_has_no_name_or_url(self):
        entry = self._make_entry()
        _apply_feed_author_fallback([entry], {"name": "", "url": "", "photo": ""})
        self.assertNotIn("author", entry)

    def test_includes_photo_when_present(self):
        entry = self._make_entry()
        _apply_feed_author_fallback([entry], {"name": "Alice", "url": "https://alice.example.com/", "photo": "https://alice.example.com/photo.jpg"})
        self.assertEqual(entry["author"]["photo"], "https://alice.example.com/photo.jpg")


class ParseHfeedTests(SimpleTestCase):
    BASE_URL = "https://example.com/"

    def _wrapped_hfeed(self, children, hcard=None):
        """Build a minimal h-feed HTML document with optional h-card at page level."""
        hcard_html = ""
        if hcard:
            name = hcard.get("name", "")
            url = hcard.get("url", "")
            photo = hcard.get("photo", "")
            hcard_html = (
                f'<div class="h-card">'
                f'<a class="p-name u-url" href="{url}">{name}</a>'
                + (f'<img class="u-photo" src="{photo}">' if photo else "")
                + "</div>"
            )
        items_html = "".join(
            f'<div class="h-entry"><a class="u-url" href="{c["url"]}"></a>'
            + (f'<a class="p-author h-card u-url" href="{c["author_url"]}">{c["author_name"]}</a>' if c.get("author_url") else "")
            + "</div>"
            for c in children
        )
        # h-card lives at page level (sibling of h-feed), not nested inside it
        return f'{hcard_html}<div class="h-feed">{items_html}</div>'

    def test_entries_without_author_get_hcard_author(self):
        html = self._wrapped_hfeed(
            [{"url": "https://example.com/post1"}],
            hcard={"name": "Alice", "url": "https://example.com/"},
        )
        entries, _ = _parse_hfeed(html, self.BASE_URL)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["author"]["name"], "Alice")
        self.assertEqual(entries[0]["author"]["url"], "https://example.com/")

    def test_entries_with_author_url_are_not_overwritten(self):
        html = self._wrapped_hfeed(
            [{"url": "https://example.com/post1", "author_url": "https://bob.example.com/", "author_name": "Bob"}],
            hcard={"name": "Alice", "url": "https://example.com/"},
        )
        entries, _ = _parse_hfeed(html, self.BASE_URL)
        self.assertEqual(entries[0]["author"]["url"], "https://bob.example.com/")

    def test_bare_hentries_get_hcard_author_fallback(self):
        """h-entries at top level (no wrapping h-feed) also get the h-card author."""
        html = (
            '<div class="h-card"><a class="p-name u-url" href="https://carol.example.com/">Carol</a></div>'
            '<div class="h-entry"><a class="u-url" href="https://carol.example.com/post1"></a></div>'
        )
        entries, _ = _parse_hfeed(html, self.BASE_URL)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["author"]["name"], "Carol")
        self.assertEqual(entries[0]["author"]["url"], "https://carol.example.com/")

    def test_empty_html_returns_empty_entries(self):
        entries, feed_meta = _parse_hfeed("<html></html>", self.BASE_URL)
        self.assertEqual(entries, [])
        self.assertEqual(feed_meta["name"], "")


class ParseJsonFeedTests(SimpleTestCase):
    def _make_data(self, items):
        return {
            "version": "https://jsonfeed.org/version/1",
            "title": "Test",
            "items": items,
        }

    def test_basic_item_parsed(self):
        data = self._make_data([{
            "id": "https://example.com/1",
            "title": "Hello",
            "url": "https://example.com/1",
        }])
        entries, meta = _parse_json_feed(data, "https://example.com/")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["_uid"], "https://example.com/1")
        self.assertEqual(entries[0]["name"], "Hello")

    def test_feed_meta_title_extracted(self):
        data = self._make_data([])
        _, meta = _parse_json_feed(data, "https://example.com/")
        self.assertEqual(meta["name"], "Test")

    def test_external_url_used_when_url_absent(self):
        data = self._make_data([{
            "id": "1",
            "external_url": "https://external.example.com/",
        }])
        entries, _ = _parse_json_feed(data, "https://example.com/")
        self.assertEqual(entries[0]["url"], "https://external.example.com/")

    def test_content_html_and_text_included(self):
        data = self._make_data([{
            "id": "1",
            "content_html": "<p>hi</p>",
            "content_text": "hi",
        }])
        entries, _ = _parse_json_feed(data, "https://example.com/")
        self.assertEqual(entries[0]["content"]["html"], "<p>hi</p>")
        self.assertEqual(entries[0]["content"]["text"], "hi")

    def test_date_modified_used_when_date_published_absent(self):
        data = self._make_data([{
            "id": "1",
            "date_modified": "2024-01-01T00:00:00Z",
        }])
        entries, _ = _parse_json_feed(data, "https://example.com/")
        self.assertEqual(entries[0]["published"], "2024-01-01T00:00:00Z")

    def test_author_from_authors_array(self):
        data = self._make_data([{
            "id": "1",
            "authors": [{"name": "Alice", "url": "https://alice.example.com/"}],
        }])
        entries, _ = _parse_json_feed(data, "https://example.com/")
        self.assertEqual(entries[0]["author"]["name"], "Alice")

    def test_empty_items_returns_empty_list(self):
        data = self._make_data([])
        entries, _ = _parse_json_feed(data, "https://example.com/")
        self.assertEqual(entries, [])

    def test_tags_mapped_to_category(self):
        data = self._make_data([{
            "id": "https://example.com/1",
            "content_text": "At the coffee shop.",
            "tags": ["checkin"],
        }])
        entries, _ = _parse_json_feed(data, "https://example.com/")
        self.assertEqual(entries[0]["category"], ["checkin"])

    def test_empty_tags_not_included(self):
        data = self._make_data([{
            "id": "https://example.com/1",
            "tags": [],
        }])
        entries, _ = _parse_json_feed(data, "https://example.com/")
        self.assertNotIn("category", entries[0])

    def test_feed_author_fallback_applied_when_no_entry_author(self):
        data = {
            "version": "https://jsonfeed.org/version/1",
            "title": "My Blog",
            "items": [{"id": "https://example.com/1", "title": "A Post"}],
        }
        entries, _ = _parse_json_feed(data, "https://example.com/")
        self.assertEqual(len(entries), 1)
        self.assertIn("author", entries[0])
        self.assertEqual(entries[0]["author"]["name"], "My Blog")


class ParseRssAtomTests(SimpleTestCase):
    RSS_FEED = b"""<?xml version="1.0"?>
<rss version="2.0">
  <channel>
    <title>Test Feed</title>
    <item>
      <title>Item One</title>
      <link>https://example.com/1</link>
      <guid>https://example.com/1</guid>
      <pubDate>Mon, 15 Jan 2024 10:00:00 +0000</pubDate>
    </item>
  </channel>
</rss>"""

    RSS_NO_AUTHOR_FEED = b"""<?xml version="1.0"?>
<rss version="2.0">
  <channel>
    <title>Blog Feed</title>
    <item>
      <title>Post One</title>
      <link>https://example.com/1</link>
      <guid>https://example.com/1</guid>
    </item>
  </channel>
</rss>"""

    ATOM_TEXT_CONTENT_FEED = b"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Test</title>
  <entry>
    <id>https://example.com/1</id>
    <link href="https://example.com/1"/>
    <content type="text">&lt;b&gt;bold&lt;/b&gt; text</content>
    </entry>
</feed>"""

    YOUTUBE_ATOM_FEED = b"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns:yt="http://www.youtube.com/xml/schemas/2015"
      xmlns:media="http://search.yahoo.com/mrss/"
      xmlns="http://www.w3.org/2005/Atom">
  <title>Video Channel</title>
  <link rel="alternate" href="https://www.youtube.com/channel/CHANNEL123"/>
  <author>
    <name>Video Channel</name>
    <uri>https://www.youtube.com/channel/CHANNEL123</uri>
  </author>
  <entry>
    <id>yt:video:abc123</id>
    <yt:videoId>abc123</yt:videoId>
    <link rel="alternate" href="https://www.youtube.com/watch?v=abc123"/>
    <author>
      <name>Video Channel</name>
      <uri>https://www.youtube.com/channel/CHANNEL123</uri>
    </author>
    <published>2026-03-24T10:00:00+00:00</published>
    <updated>2026-03-24T10:05:00+00:00</updated>
    <media:group>
      <media:thumbnail url="https://img.example.com/thumb.jpg" width="480" height="360"/>
      <media:description>First line

Second line</media:description>
    </media:group>
  </entry>
</feed>"""

    def test_parses_rss_item(self):
        entries, meta = _parse_rss_atom(self.RSS_FEED, "https://example.com/")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["_uid"], "https://example.com/1")
        self.assertEqual(entries[0]["name"], "Item One")

    def test_rss_feed_meta_title_extracted(self):
        _, meta = _parse_rss_atom(self.RSS_FEED, "https://example.com/")
        self.assertEqual(meta["name"], "Test Feed")

    def test_empty_feed_returns_empty_list(self):
        empty_rss = b"<?xml version='1.0'?><rss version='2.0'><channel></channel></rss>"
        entries, _ = _parse_rss_atom(empty_rss, "https://example.com/")
        self.assertEqual(entries, [])

    def test_feed_author_fallback_applied_when_no_entry_author(self):
        entries, _ = _parse_rss_atom(self.RSS_NO_AUTHOR_FEED, "https://example.com/")
        self.assertEqual(len(entries), 1)
        self.assertIn("author", entries[0])
        self.assertEqual(entries[0]["author"]["name"], "Blog Feed")

    def test_content_text_stripped_for_non_html_content_type(self):
        entries, _ = _parse_rss_atom(self.ATOM_TEXT_CONTENT_FEED, "https://example.com/")
        self.assertEqual(len(entries), 1)
        # The raw value contains HTML tags; text should be stripped
        self.assertNotIn("<b>", entries[0]["content"]["text"])
        self.assertIn("bold", entries[0]["content"]["text"])
        self.assertNotIn("<b>", entries[0]["content"]["html"])
        self.assertIn("&lt;b&gt;bold&lt;/b&gt;", entries[0]["content"]["html"])

    def test_youtube_entries_include_reader_friendly_media_fields(self):
        entries, meta = _parse_rss_atom(
            self.YOUTUBE_ATOM_FEED,
            "https://www.youtube.com/feeds/videos.xml?channel_id=CHANNEL123",
        )
        self.assertEqual(meta["name"], "Video Channel")
        self.assertEqual(meta["url"], "https://www.youtube.com/channel/CHANNEL123")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["url"], "https://www.youtube.com/watch?v=abc123")
        self.assertEqual(entries[0]["photo"], ["https://img.example.com/thumb.jpg"])
        self.assertEqual(entries[0]["summary"], "First line\n\nSecond line")
        self.assertEqual(entries[0]["content"]["text"], "First line\n\nSecond line")
        self.assertIn("<p>First line</p><p>Second line</p>", entries[0]["content"]["html"])
        self.assertEqual(entries[0]["updated"], "2026-03-24T10:05:00+00:00")
        self.assertEqual(entries[0]["author"]["url"], "https://www.youtube.com/channel/CHANNEL123")


class FetchAndParseFeedTests(SimpleTestCase):
    def _mock_urlopen(self, content_type, body):
        mock_resp = MagicMock()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.headers.get.side_effect = lambda k, d=None: {
            "Content-Type": content_type,
            "Link": None,
        }.get(k, d)
        mock_resp.read.return_value = body
        return mock_resp

    @patch("microsub.feed_parser.urlopen")
    def test_network_error_raises_runtime_error(self, mock_urlopen):
        from urllib.error import URLError
        mock_urlopen.side_effect = URLError("no route")
        with self.assertRaises(RuntimeError):
            fetch_and_parse_feed("https://example.com/feed")

    @patch("microsub.feed_parser.urlopen")
    def test_json_feed_parsed_for_jsonfeed_content(self, mock_urlopen):
        data = json.dumps({
            "version": "https://jsonfeed.org/version/1",
            "title": "Test",
            "items": [{"id": "1", "title": "Post"}],
        }).encode()
        mock_urlopen.return_value = self._mock_urlopen("application/json", data)
        entries, hub, meta = fetch_and_parse_feed("https://example.com/feed")
        self.assertEqual(len(entries), 1)

    @patch("microsub.feed_parser.urlopen")
    def test_rss_parsed_for_xml_content_type(self, mock_urlopen):
        rss = b"""<?xml version="1.0"?><rss version="2.0"><channel>
          <item><guid>1</guid><link>https://example.com/1</link></item>
        </channel></rss>"""
        mock_urlopen.return_value = self._mock_urlopen("application/rss+xml", rss)
        entries, hub, meta = fetch_and_parse_feed("https://example.com/feed")
        self.assertEqual(len(entries), 1)

    @patch("microsub.feed_parser.urlopen")
    def test_hub_url_extracted_from_link_header(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.headers.get.side_effect = lambda k, d=None: {
            "Content-Type": "application/rss+xml",
            "Link": '<https://hub.example.com/>; rel="hub"',
        }.get(k, d)
        mock_resp.read.return_value = b"<rss version='2.0'><channel></channel></rss>"
        mock_urlopen.return_value = mock_resp
        _, hub, _ = fetch_and_parse_feed("https://example.com/feed")
        self.assertEqual(hub, "https://hub.example.com/")

    @patch("microsub.feed_parser.urlopen")
    def test_no_hub_returns_none(self, mock_urlopen):
        rss = b"<rss version='2.0'><channel></channel></rss>"
        mock_urlopen.return_value = self._mock_urlopen("application/rss+xml", rss)
        _, hub, _ = fetch_and_parse_feed("https://example.com/feed")
        self.assertIsNone(hub)

    @patch("microsub.feed_parser.urlopen")
    def test_correct_user_agent_sent(self, mock_urlopen):
        rss = b"<rss version='2.0'><channel></channel></rss>"
        mock_urlopen.return_value = self._mock_urlopen("application/rss+xml", rss)
        fetch_and_parse_feed("https://example.com/feed")
        req = mock_urlopen.call_args[0][0]
        self.assertEqual(req.get_header("User-agent"), "Webstead Microsub/1.0")
