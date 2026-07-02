import logging
import re
from html import escape
from html.parser import HTMLParser
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen

from .utils import normalize_entry_data, normalize_url

logger = logging.getLogger(__name__)

USER_AGENT = "Webstead Microsub/1.0"
FETCH_TIMEOUT = 15


def _strip_html(html_str: str) -> str:
    """Strip HTML tags and return plain text."""
    class _Stripper(HTMLParser):
        def __init__(self):
            super().__init__()
            self._parts: list[str] = []

        def handle_data(self, d: str) -> None:
            self._parts.append(d)

    s = _Stripper()
    try:
        s.feed(html_str)
        return " ".join("".join(s._parts).split())
    except Exception:
        return html_str


def _normalize_text(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n").strip()


def _looks_like_html(value: str) -> bool:
    return bool(re.search(r"<[A-Za-z!/][^>]*>", value))


def _plain_text_to_html(text: str) -> str:
    text = _normalize_text(text)
    if not text:
        return ""
    paragraphs = []
    for block in text.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        paragraphs.append(f"<p>{escape(block).replace(chr(10), '<br>')}</p>")
    if paragraphs:
        return "".join(paragraphs)
    return f"<p>{escape(text).replace(chr(10), '<br>')}</p>"


def _feedparser_content_to_jf2(value: str, content_type: str | None = None) -> dict | None:
    """Convert feedparser content/summary fields into a JF2 content block."""
    if not value:
        return None

    normalized = _normalize_text(value)
    if not normalized:
        return None

    content_type = (content_type or "").lower()
    if "html" in content_type or "xhtml" in content_type:
        is_html = _looks_like_html(value)
    elif "text" in content_type or "plain" in content_type:
        is_html = False
    else:
        is_html = _looks_like_html(value)

    if is_html:
        return {"html": value, "text": _strip_html(value)}
    text_value = _strip_html(normalized) if _looks_like_html(value) else normalized
    return {"html": _plain_text_to_html(normalized), "text": text_value}


def _feedparser_media_urls(values, *, allowed_types: tuple[str, ...] | None = None) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    for item in values or []:
        if not isinstance(item, dict):
            continue
        media_url = item.get("url")
        if not media_url or media_url in seen:
            continue
        media_type = str(item.get("type", "")).lower()
        if allowed_types and not any(media_type.startswith(prefix) for prefix in allowed_types):
            continue
        seen.add(media_url)
        urls.append(media_url)
    return urls


class _HubLinkParser(HTMLParser):
    """Parse <link rel="hub"> from HTML."""

    def __init__(self):
        super().__init__()
        self.hub_url = None
        self.feed_url = None

    def handle_starttag(self, tag, attrs):
        if tag.lower() != "link":
            return
        attr_map = {k.lower(): v for k, v in attrs}
        rels = {r.strip() for r in attr_map.get("rel", "").split()}
        href = attr_map.get("href", "")
        if "hub" in rels and not self.hub_url:
            self.hub_url = href
        if "alternate" in rels and not self.feed_url:
            ct = attr_map.get("type", "")
            if any(f in ct for f in ("rss", "atom", "xml", "json")):
                self.feed_url = href


def _parse_link_header_for_rel(header_value: str, rel_name: str) -> str | None:
    if not header_value:
        return None
    for part in header_value.split(","):
        segment = part.strip()
        if not segment.startswith("<") or ">" not in segment:
            continue
        url, _, params = segment.partition(">")
        rel = None
        for param in params.split(";"):
            name, _, value = param.strip().partition("=")
            if name.lower() == "rel":
                rel = value.strip('"')
                break
        if rel and rel_name in rel.split():
            return url[1:]
    return None


def discover_websub_hub(url: str, link_header: str | None, html_body: str | None = None) -> str | None:
    """Return WebSub hub URL from Link header or <link rel="hub"> in HTML."""
    if link_header:
        hub = _parse_link_header_for_rel(link_header, "hub")
        if hub:
            return urljoin(url, hub)
    if html_body:
        parser = _HubLinkParser()
        parser.feed(html_body[:50_000])
        if parser.hub_url:
            return urljoin(url, parser.hub_url)
    return None


def _mf2_embedded_to_jf2(val, base_url: str) -> dict | None:
    """Convert an embedded mf2 h-card/h-adr (or plain URL string) to a JF2 dict."""
    if isinstance(val, str):
        return {"type": "card", "url": urljoin(base_url, val)} if val else None
    if not isinstance(val, dict):
        return None
    props = val.get("properties", {})
    types = val.get("type", [])
    kind = "adr" if "h-adr" in types and "h-card" not in types else "card"
    out: dict = {"type": kind}

    for mf2_key, jf2_key in [
        ("name",           "name"),
        ("locality",       "locality"),
        ("region",         "region"),
        ("country-name",   "country"),
        ("postal-code",    "postal-code"),
        ("street-address", "street-address"),
        ("latitude",       "latitude"),
        ("longitude",      "longitude"),
        ("altitude",       "altitude"),
        ("tel",            "tel"),
        ("email",          "email"),
    ]:
        vals = props.get(mf2_key, [])
        if vals and isinstance(vals[0], str):
            out[jf2_key] = vals[0]

    url_vals = props.get("url", [])
    if url_vals and isinstance(url_vals[0], str):
        out["url"] = urljoin(base_url, url_vals[0])

    photo_vals = props.get("photo", [])
    if photo_vals:
        p = photo_vals[0]
        out["photo"] = (p.get("value") or p) if isinstance(p, dict) else p

    return out if len(out) > 1 else None


def _author_from_mf2(author_val, base_url: str) -> dict | None:
    return _mf2_embedded_to_jf2(author_val, base_url)


def _hentry_to_jf2(item: dict, base_url: str) -> dict:
    props = item.get("properties", {})

    def _first(key, default=""):
        vals = props.get(key, [])
        if not vals:
            return default
        v = vals[0]
        if isinstance(v, dict):
            return v.get("value", v.get("html", "")) or default
        return v or default

    entry: dict = {"type": "entry"}

    url = _first("url")
    if url:
        entry["url"] = urljoin(base_url, url)

    uid = _first("uid") or entry.get("url", "")
    if uid:
        entry["_uid"] = uid

    name = _first("name")
    if name:
        entry["name"] = name

    content_vals = props.get("content", [])
    if content_vals:
        cv = content_vals[0]
        if isinstance(cv, dict):
            html_val = cv.get("html", "")
            text_val = cv.get("value", "") or _strip_html(html_val)
            entry["content"] = {"html": html_val, "text": text_val}
        else:
            entry["content"] = {"text": str(cv)}

    published = _first("published")
    if published:
        entry["published"] = published

    authors = props.get("author", [])
    if authors:
        author = _author_from_mf2(authors[0], base_url)
        if author:
            entry["author"] = author

    # Simple scalar strings
    for mf2_key, jf2_key in [("summary", "summary"), ("updated", "updated")]:
        v = _first(mf2_key)
        if v:
            entry[jf2_key] = v

    # RSVP — normalize to lowercase
    rsvp = _first("rsvp")
    if rsvp:
        entry["rsvp"] = rsvp.lower()

    # Embedded objects — checkin and location
    for mf2_key, jf2_key in [("checkin", "checkin"), ("location", "location")]:
        vals = props.get(mf2_key, [])
        if vals:
            card = _mf2_embedded_to_jf2(vals[0], base_url)
            if card:
                entry[jf2_key] = card

    # Multi-value URL arrays
    def _url_vals(key: str) -> list[str]:
        out = []
        for v in props.get(key, []):
            if isinstance(v, str) and v:
                out.append(urljoin(base_url, v))
            elif isinstance(v, dict):
                u = v.get("value") or v.get("url", "")
                if u:
                    out.append(urljoin(base_url, u))
        return out

    for mf2_key in ("photo", "video", "audio", "syndication"):
        urls = _url_vals(mf2_key)
        if urls:
            entry[mf2_key] = urls

    featured = _first("featured")
    if featured:
        entry["featured"] = urljoin(base_url, featured)

    # Category — array of plain strings
    cats = [v for v in props.get("category", []) if isinstance(v, str) and v]
    if cats:
        entry["category"] = cats

    # Response types (URL references)
    for prop, jf2_key in [
        ("in-reply-to",  "in-reply-to"),
        ("like-of",      "like-of"),
        ("repost-of",    "repost-of"),
        ("bookmark-of",  "bookmark-of"),
        ("listen-of",    "listen-of"),
        ("watch-of",     "watch-of"),
        ("read-of",      "read-of"),
        ("checkin-of",   "checkin-of"),
    ]:
        vals = props.get(prop, [])
        if vals:
            v = vals[0]
            if isinstance(v, str):
                entry[jf2_key] = urljoin(base_url, v)
            elif isinstance(v, dict):
                inner = v.get("value") or v.get("url", "")
                if inner:
                    entry[jf2_key] = urljoin(base_url, inner)

    return entry


def _apply_feed_author_fallback(entries: list[dict], feed_meta: dict) -> None:
    """Assign feed-level author to entries that lack an author URL."""
    if isinstance(feed_meta.get("author"), dict) and feed_meta["author"].get("url"):
        feed_author = feed_meta["author"]
    else:
        if not (feed_meta.get("url") or feed_meta.get("name")):
            return
        feed_author: dict = {"type": "card"}
        if feed_meta.get("name"):
            feed_author["name"] = feed_meta["name"]
        if feed_meta.get("url"):
            feed_author["url"] = feed_meta["url"]
        if feed_meta.get("photo"):
            feed_author["photo"] = feed_meta["photo"]
    if not feed_author:
        return
    for e in entries:
        if not e.get("author") or not e["author"].get("url"):
            e["author"] = feed_author


def _parse_hfeed(html: str, base_url: str) -> tuple[list[dict], dict]:
    """Return (entries, feed_meta) from an h-feed HTML document."""
    try:
        import mf2py
    except ImportError:
        logger.warning("mf2py not installed; skipping h-feed parsing")
        return [], {}

    parsed = mf2py.parse(doc=html, url=base_url)
    items = parsed.get("items", [])
    feed_meta: dict = {"name": "", "photo": "", "url": normalize_url(base_url) or "", "description": "", "author": None}

    def _apply_hcard_meta(search_items: list) -> None:
        """Populate feed_meta from an h-card if name/photo/url not yet found."""
        for i in search_items:
            if "h-card" in i.get("type", []):
                if not feed_meta["name"]:
                    card_name = i.get("properties", {}).get("name", [])
                    if card_name and isinstance(card_name[0], str):
                        feed_meta["name"] = card_name[0]
                if not feed_meta["photo"]:
                    card_photo = i.get("properties", {}).get("photo", [])
                    if card_photo:
                        p = card_photo[0]
                        feed_meta["photo"] = (p.get("value") or p) if isinstance(p, dict) else p
                if not feed_meta["url"]:
                    card_url = i.get("properties", {}).get("url", [])
                    if card_url and isinstance(card_url[0], str):
                        feed_meta["url"] = urljoin(base_url, card_url[0])
                if not feed_meta["author"]:
                    card = _mf2_embedded_to_jf2(i, base_url)
                    if card:
                        feed_meta["author"] = card
                break

    def _apply_title_fallback() -> None:
        """Extract <title> tag as last-resort feed name."""
        if not feed_meta["name"]:
            import re as _re
            from html import unescape as _unescape
            m = _re.search(r"<title[^>]*>(.*?)</title>", html, _re.IGNORECASE | _re.DOTALL)
            if m:
                feed_meta["name"] = _unescape(m.group(1).strip())

    # Find h-feed and use its children, or fall back to bare h-entries
    for item in items:
        if "h-feed" in item.get("type", []):
            props = item.get("properties", {})
            name_vals = props.get("name", [])
            if name_vals and isinstance(name_vals[0], str):
                feed_meta["name"] = name_vals[0]
            photo_vals = props.get("photo", [])
            if photo_vals:
                p = photo_vals[0]
                feed_meta["photo"] = (p.get("value") or p) if isinstance(p, dict) else p
            url_vals = props.get("url", [])
            if url_vals and isinstance(url_vals[0], str):
                feed_meta["url"] = urljoin(base_url, url_vals[0])
            summary_vals = props.get("summary", [])
            if summary_vals and isinstance(summary_vals[0], str):
                feed_meta["description"] = summary_vals[0]
            if not feed_meta["description"]:
                content_vals = props.get("content", [])
                if content_vals:
                    first_content = content_vals[0]
                    if isinstance(first_content, dict):
                        feed_meta["description"] = first_content.get("value", "") or _strip_html(first_content.get("html", ""))
                    elif isinstance(first_content, str):
                        feed_meta["description"] = first_content
            author_vals = props.get("author", [])
            if author_vals and not feed_meta["author"]:
                author = _author_from_mf2(author_vals[0], base_url)
                if author:
                    feed_meta["author"] = author
            children = item.get("children", [])
            entries = [
                _hentry_to_jf2(child, base_url)
                for child in children
                if "h-entry" in child.get("type", [])
            ]
            _apply_hcard_meta(items)
            _apply_title_fallback()
            _apply_feed_author_fallback(entries, feed_meta)
            return entries, feed_meta

    # Bare h-entries — use h-card name as the feed name if present
    _apply_hcard_meta(items)
    _apply_title_fallback()

    entries = [
        _hentry_to_jf2(item, base_url)
        for item in items
        if "h-entry" in item.get("type", [])
    ]
    _apply_feed_author_fallback(entries, feed_meta)
    return entries, feed_meta


def _parse_rss_atom(content: bytes, url: str) -> tuple[list[dict], dict]:
    """Return (entries, feed_meta) from RSS/Atom content."""
    try:
        import feedparser
    except ImportError:
        logger.warning("feedparser not installed; skipping RSS/Atom parsing")
        return [], {}

    feed = feedparser.parse(content)
    feed_meta: dict = {"name": "", "photo": "", "url": normalize_url(url) or "", "description": "", "author": None}

    # Feed-level metadata
    feed_info = getattr(feed, "feed", None)
    if feed_info:
        title = getattr(feed_info, "title", "") or ""
        feed_meta["name"] = title
        feed_meta["url"] = (
            getattr(feed_info, "link", None)
            or getattr(feed_info, "href", None)
            or ""
        )
        feed_meta["description"] = (
            getattr(feed_info, "subtitle", None)
            or getattr(feed_info, "description", None)
            or ""
        )
        image = getattr(feed_info, "image", None)
        if image and getattr(image, "href", None):
            feed_meta["photo"] = image.href
        elif getattr(feed_info, "icon", None):
            feed_meta["photo"] = feed_info.icon
        feed_author = getattr(feed_info, "author_detail", None) or getattr(feed_info, "author", None)
        if feed_author:
            if isinstance(feed_author, str):
                feed_meta["author"] = {"type": "card", "name": feed_author}
            else:
                card: dict = {"type": "card"}
                if getattr(feed_author, "name", None):
                    card["name"] = feed_author.name
                if getattr(feed_author, "href", None):
                    card["url"] = feed_author.href
                if card:
                    feed_meta["author"] = card

    entries = []
    for e in feed.entries:
        entry: dict = {"type": "entry"}
        uid = getattr(e, "id", None) or getattr(e, "link", None)
        if uid:
            entry["_uid"] = uid
        link = getattr(e, "link", None)
        if link:
            entry["url"] = link
        title = getattr(e, "title", None)
        if title:
            entry["name"] = title
        # content
        content_list = getattr(e, "content", None)
        summary = getattr(e, "summary", None)
        summary_detail = getattr(e, "summary_detail", None)
        summary_block = _feedparser_content_to_jf2(
            summary,
            summary_detail.get("type") if hasattr(summary_detail, "get") else getattr(summary_detail, "type", None),
        )
        if summary_block:
            entry["summary"] = summary_block["text"]
        if content_list:
            c = content_list[0]
            content_block = _feedparser_content_to_jf2(c.get("value", ""), c.get("type"))
            if content_block:
                entry["content"] = content_block
        elif summary_block:
            entry["content"] = summary_block

        photos = _feedparser_media_urls(getattr(e, "media_thumbnail", None))
        photos.extend(
            media_url
            for media_url in _feedparser_media_urls(
                getattr(e, "media_content", None),
                allowed_types=("image/",),
            )
            if media_url not in photos
        )
        if photos:
            entry["photo"] = photos

        videos = _feedparser_media_urls(
            getattr(e, "media_content", None),
            allowed_types=("video/",),
        )
        if videos:
            entry["video"] = videos

        audio = _feedparser_media_urls(
            getattr(e, "media_content", None),
            allowed_types=("audio/",),
        )
        if audio:
            entry["audio"] = audio
        # published / updated — use the parsed time.struct_time to produce a
        # stable ISO 8601 string regardless of what format the feed used.
        pub_parsed = getattr(e, "published_parsed", None) or getattr(e, "updated_parsed", None)
        if pub_parsed:
            from datetime import datetime, timezone as _tz
            entry["published"] = datetime(*pub_parsed[:6], tzinfo=_tz.utc).isoformat()
        else:
            pub = getattr(e, "published", None) or getattr(e, "updated", None)
            if pub:
                entry["published"] = pub
        updated_parsed = getattr(e, "updated_parsed", None)
        if updated_parsed:
            from datetime import datetime, timezone as _tz
            entry["updated"] = datetime(*updated_parsed[:6], tzinfo=_tz.utc).isoformat()
        else:
            updated = getattr(e, "updated", None)
            if updated:
                entry["updated"] = updated
        # author
        author = getattr(e, "author_detail", None) or getattr(e, "author", None)
        if author:
            if isinstance(author, str):
                entry["author"] = {"type": "card", "name": author}
            else:
                card: dict = {"type": "card"}
                if getattr(author, "name", None):
                    card["name"] = author.name
                # Some RSS feeds put the name in email field (author@example.com (Name))
                elif getattr(author, "email", None):
                    email = author.email
                    # Strip trailing "(Name)" style
                    if "(" in email and email.endswith(")"):
                        card["name"] = email[email.index("(") + 1:-1].strip()
                    else:
                        card["name"] = email
                if getattr(author, "href", None):
                    card["url"] = author.href
                entry["author"] = card
        entries.append(entry)
    _apply_feed_author_fallback(entries, feed_meta)
    return entries, feed_meta


def _parse_json_feed(data: dict, base_url: str) -> tuple[list[dict], dict]:
    """Return (entries, feed_meta) from a JSON Feed dict."""
    feed_meta: dict = {
        "name": "",
        "photo": "",
        "url": normalize_url(data.get("home_page_url", "") or data.get("feed_url", "") or base_url) or "",
        "description": data.get("description", "") or "",
        "author": None,
    }
    feed_meta["name"] = data.get("title", "") or ""
    feed_meta["photo"] = data.get("icon", "") or data.get("favicon", "") or ""
    feed_author = data.get("author") or (data.get("authors") or [None])[0]
    if isinstance(feed_author, dict):
        card: dict = {"type": "card"}
        if feed_author.get("name"):
            card["name"] = feed_author["name"]
        if feed_author.get("url"):
            card["url"] = feed_author["url"]
        if feed_author.get("avatar"):
            card["photo"] = feed_author["avatar"]
        if len(card) > 1:
            feed_meta["author"] = card

    items = data.get("items", [])
    entries = []
    for item in items:
        entry: dict = {"type": "entry"}
        uid = item.get("id")
        if uid:
            entry["_uid"] = str(uid)
        url = item.get("url") or item.get("external_url")
        if url:
            entry["url"] = url
        title = item.get("title")
        if title:
            entry["name"] = title
        content_html = item.get("content_html", "")
        content_text = item.get("content_text", "") or (
            _strip_html(content_html) if content_html else ""
        )
        if content_html or content_text:
            entry["content"] = {"html": content_html, "text": content_text}
        published = item.get("date_published") or item.get("date_modified")
        if published:
            entry["published"] = published
        author_data = item.get("author") or (item.get("authors") or [None])[0]
        if author_data and isinstance(author_data, dict):
            card: dict = {"type": "card"}
            if author_data.get("name"):
                card["name"] = author_data["name"]
            if author_data.get("url"):
                card["url"] = author_data["url"]
            if author_data.get("avatar"):
                card["photo"] = author_data["avatar"]
            entry["author"] = card
        tags = [t for t in item.get("tags", []) if isinstance(t, str) and t]
        if tags:
            entry["category"] = tags
        entries.append(entry)
    _apply_feed_author_fallback(entries, feed_meta)
    return entries, feed_meta


def _normalize_entries(entries: list[dict]) -> list[dict]:
    normalized_entries: list[dict] = []
    for entry in entries:
        uid = entry.get("_uid") or entry.get("uid") or entry.get("url")
        if not uid:
            continue
        payload, _ = normalize_entry_data(entry, uid=str(uid))
        normalized_entries.append(payload)
    return normalized_entries


def fetch_and_parse_feed(url: str) -> tuple[list[dict], str | None, dict]:
    """Fetch a URL and return (jf2_entries, websub_hub_url, feed_meta).

    feed_meta is {"name": str, "photo": str} with feed-level title and icon.
    """
    req = Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urlopen(req, timeout=FETCH_TIMEOUT) as response:
            content_type = response.headers.get("Content-Type", "").lower()
            link_header = response.headers.get("Link")
            raw = response.read(10 * 1024 * 1024)  # 10 MB cap
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        raise RuntimeError(f"Failed to fetch {url}: {exc}") from exc

    hub_url = discover_websub_hub(url, link_header)
    feed_meta: dict = {"name": "", "photo": "", "url": normalize_url(url) or "", "description": "", "author": None}

    # Detect format
    if "json" in content_type:
        try:
            import json
            data = json.loads(raw.decode("utf-8", errors="replace"))
        except Exception:
            data = {}
        # JSON Feed has a "version" key starting with "https://jsonfeed.org/"
        if isinstance(data.get("version"), str) and "jsonfeed" in data["version"]:
            entries, feed_meta = _parse_json_feed(data, url)
        else:
            entries, feed_meta = _parse_rss_atom(raw, url)
    elif "html" in content_type:
        html_str = raw.decode("utf-8", errors="replace")
        entries, feed_meta = _parse_hfeed(html_str, url)
        hub_url = discover_websub_hub(url, link_header, html_str) or hub_url
    else:
        # Try RSS/Atom (XML)
        entries, feed_meta = _parse_rss_atom(raw, url)

    return _normalize_entries(entries), hub_url, feed_meta
