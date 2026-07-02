import logging
from functools import lru_cache
from html.parser import HTMLParser
from urllib.parse import urlparse

import mf2py
import requests
from django.templatetags.static import static
from django.utils.html import strip_tags
from django.utils.text import Truncator


logger = logging.getLogger(__name__)

USER_AGENT = "Webstead/1.0 (+https://webstead.dev/)"
DEFAULT_AVATAR_URL = static("img/default-avatar.svg")
INTERACTION_SUMMARY_LENGTH = 240
REQUEST_TIMEOUT = (3, 8)


class _HeadMetadataParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self._in_title = False
        self._title_parts = []
        self.meta = {}

    @property
    def title(self):
        return " ".join(self._title_parts).strip()

    def handle_starttag(self, tag, attrs):
        attrs_dict = {
            key.lower(): value
            for key, value in attrs
            if key and value is not None
        }
        if tag == "title":
            self._in_title = True
            return
        if tag != "meta":
            return

        content = (attrs_dict.get("content") or "").strip()
        if not content:
            return

        for key in ("property", "name", "itemprop"):
            meta_name = (attrs_dict.get(key) or "").strip().lower()
            if meta_name and meta_name not in self.meta:
                self.meta[meta_name] = content

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title and data.strip():
            self._title_parts.append(data.strip())


def _first_text(value, default=""):
    if isinstance(value, dict):
        value = value.get("value") or value.get("html") or ""
    if isinstance(value, list):
        value = _first_text(value[0], default=default)
    return value if isinstance(value, str) else default


def _strip_text(value):
    if not value:
        return ""
    return " ".join(strip_tags(value).split())


def _normalize_whitespace(value):
    if not value:
        return ""
    return " ".join(str(value).split())


def _first_content_value(value):
    if isinstance(value, list):
        return _first_content_value(value[0]) if value else ""
    if isinstance(value, dict):
        return value.get("value") or ""
    return value if isinstance(value, str) else ""


def _first_content_html(value):
    if isinstance(value, list):
        return _first_content_html(value[0]) if value else ""
    if isinstance(value, dict):
        return value.get("html") or ""
    return ""


def _extract_photo(value):
    if isinstance(value, list):
        return _extract_photo(value[0]) if value else ""
    if isinstance(value, dict):
        url = value.get("value") or ""
        return url if isinstance(url, str) else ""
    return value if isinstance(value, str) else ""


def _extract_author(author_value):
    if isinstance(author_value, list) and author_value:
        author_value = author_value[0]

    if not isinstance(author_value, dict):
        return None

    properties = author_value.get("properties") or {}
    if not isinstance(properties, dict):
        return None

    author_name = _strip_text(_first_text(properties.get("name")))
    author_url = _first_text(properties.get("url"))
    author_photo = _extract_photo(properties.get("photo"))
    if not author_photo:
        author_photo = DEFAULT_AVATAR_URL

    if not author_name and not author_url:
        return None

    return {
        "author_name": author_name,
        "author_url": author_url,
        "author_photo": author_photo,
    }


def _summary_excerpt(value):
    text = _strip_text(value)
    if not text:
        return "", False
    truncated = len(text) > INTERACTION_SUMMARY_LENGTH
    excerpt = Truncator(text).chars(INTERACTION_SUMMARY_LENGTH, truncate="...")
    return excerpt, truncated


def _find_entry(items):
    for item in items or []:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type") or []
        if any(entry_type in item_type for entry_type in ("h-entry", "h-cite", "h-event")):
            return item
    for item in items or []:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type") or []
        if "h-card" in item_type:
            return item
    return None


def _normalized_title(name_value, summary_for_compare):
    name_text = _strip_text(name_value)
    if not name_text:
        return None
    if len(name_text) > 120:
        return None
    normalized_name = _normalize_whitespace(name_text)
    normalized_summary = _normalize_whitespace(summary_for_compare)
    if normalized_summary and normalized_name == normalized_summary:
        return None
    return name_text


def _site_root_url(value):
    parsed = urlparse(value)
    if not parsed.scheme or not parsed.netloc:
        return ""
    return f"{parsed.scheme}://{parsed.netloc}"


def _host_label(value):
    parsed = urlparse(value)
    host = parsed.netloc.strip().lower()
    if host.startswith("www."):
        host = host[4:]
    return host


def _fallback_target_from_metadata(html, base_url):
    parser = _HeadMetadataParser()
    parser.feed(html)

    title = _strip_text(
        parser.meta.get("og:title")
        or parser.meta.get("twitter:title")
        or parser.title
    )
    description = _strip_text(
        parser.meta.get("og:description")
        or parser.meta.get("twitter:description")
        or parser.meta.get("description")
    )
    original_url = (
        parser.meta.get("og:url")
        or parser.meta.get("twitter:url")
        or base_url
    ).strip()

    if not title and not description:
        return None

    summary_excerpt, summary_truncated = _summary_excerpt(description)
    payload = {
        "original_url": original_url,
        "summary_excerpt": summary_excerpt,
        "summary_truncated": summary_truncated,
        "summary_text": description,
        "summary_html": None,
        "title": title or None,
    }

    author_name = _strip_text(
        parser.meta.get("og:site_name")
        or parser.meta.get("application-name")
        or _host_label(original_url or base_url)
    )
    author_url = _site_root_url(original_url or base_url)
    if author_name:
        payload["author_name"] = author_name
    if author_url:
        payload["author_url"] = author_url

    return payload


def normalize_interaction_properties(properties, target_url=""):
    if not isinstance(properties, dict):
        return None

    original_url = _first_text(properties.get("url")).strip() or (target_url or "")

    content_value = _first_content_value(properties.get("content"))
    summary_text = content_value or _first_text(properties.get("name")) or ""
    summary_text = summary_text.strip()
    summary_html = _first_content_html(properties.get("content")) or None

    summary_source = summary_text or summary_html or ""
    summary_excerpt, summary_truncated = _summary_excerpt(summary_source)

    summary_for_compare = content_value or summary_text
    title = _normalized_title(_first_text(properties.get("name")), summary_for_compare)

    author = _extract_author(properties.get("author"))

    payload = {
        "original_url": original_url,
        "summary_excerpt": summary_excerpt,
        "summary_truncated": summary_truncated,
        "summary_text": summary_text,
        "summary_html": summary_html,
        "title": title,
    }
    if author:
        payload.update(author)

    return payload


def parse_target_from_html(html, base_url):
    parsed = mf2py.parse(doc=html, url=base_url)
    items = parsed.get("items") if isinstance(parsed, dict) else []
    entry = _find_entry(items)
    if not entry:
        return _fallback_target_from_metadata(html, base_url)
    properties = entry.get("properties") or {}
    return normalize_interaction_properties(properties, target_url=base_url)


@lru_cache(maxsize=128)
def fetch_target_from_url(target_url):
    try:
        response = requests.get(
            target_url,
            headers={"User-Agent": USER_AGENT},
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
    except requests.Timeout:
        logger.info("Timeout fetching mf2 target for %s", target_url)
        return None
    except requests.RequestException as exc:
        logger.warning(
            "Unable to fetch mf2 target for %s (%s)",
            target_url,
            exc.__class__.__name__,
        )
        logger.debug("mf2 fetch error detail", exc_info=exc)
        return None

    return parse_target_from_html(response.text, target_url)
