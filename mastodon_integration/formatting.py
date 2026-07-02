"""
Converts a Webstead Post into toot text for the Mastodon API.

Entry point: format_post(post, max_chars, canonical_url) → (text, cw_text)

Returns a tuple of:
  - text       : the toot body
  - cw_text    : content warning / spoiler_text (empty string if none)
"""

import re

from django.utils.html import strip_tags
import markdown


# Tags used internally that should never appear as Mastodon hashtags
_EXCLUDED_TAGS = {"cw"}


def _md_to_plain(content: str) -> str:
    """Convert markdown content to plain text."""
    md = markdown.Markdown(extensions=["fenced_code"])
    html = md.convert(content)
    return strip_tags(html).strip()


def _hashtags_from_post(post, max_remaining: int) -> str:
    """
    Build a hashtag string from the post's tags, excluding reserved tags,
    fitting within max_remaining characters (including a leading space).
    Returns an empty string if no tags fit.
    """
    tags = [
        f"#{t.tag}"
        for t in post.tags.all()
        if t.tag not in _EXCLUDED_TAGS
    ]
    if not tags:
        return ""

    result = ""
    for tag in tags:
        candidate = result + " " + tag
        if len(candidate) <= max_remaining:
            result = candidate
        else:
            break
    return result


def _truncate_to_fit(text: str, max_chars: int, suffix: str) -> str:
    """
    Truncate text so that text + suffix fits within max_chars.
    Truncates on a word boundary where possible.
    """
    if len(text) + len(suffix) <= max_chars:
        return text

    budget = max_chars - len(suffix) - 1  # -1 for the ellipsis character
    truncated = text[:budget].rsplit(" ", 1)[0]
    return truncated + "…"


def _extract_cw(post) -> tuple[str, str]:
    """
    If the post is tagged 'cw', split content into (cw_line, body).
    The first line becomes the content warning; the remainder is the body.
    Returns (body, cw_text). If not a CW post, cw_text is empty.
    """
    tag_slugs = {t.tag for t in post.tags.all()}
    if "cw" not in tag_slugs:
        return post.content, ""

    lines = post.content.split("\n", 1)
    cw_text = _md_to_plain(lines[0].strip())
    body = lines[1].strip() if len(lines) > 1 else ""
    return body, cw_text


def format_post(post, max_chars: int, canonical_url: str) -> tuple[str, str]:
    """
    Build (toot_text, cw_text) for a Webstead Post.

    max_chars      : account.max_toot_chars
    canonical_url  : the absolute URL of the post on this Webstead installation
    """
    content, cw_text = _extract_cw(post)

    # Mastodon's spoiler_text shares the same character limit as the post body.
    if cw_text and len(cw_text) > max_chars:
        cw_text = cw_text[:max_chars - 1] + "…"

    kind = post.kind

    if kind == "article":
        # Title + canonical URL
        title = post.title or ""
        text = f"{title}\n\n{canonical_url}"

    elif kind == "note":
        plain = _md_to_plain(content)
        url_suffix = f" {canonical_url}"
        hashtag_budget = max_chars - len(plain) - len(url_suffix)
        hashtags = _hashtags_from_post(post, hashtag_budget)
        if len(plain) + len(hashtags) + len(url_suffix) <= max_chars:
            text = plain + hashtags + url_suffix
        else:
            plain = _truncate_to_fit(plain, max_chars, url_suffix)
            text = plain + url_suffix

    elif kind == "photo":
        # Caption + canonical URL (media uploaded separately in the task)
        plain = _md_to_plain(content)
        url_suffix = f" {canonical_url}"
        if len(plain) + len(url_suffix) > max_chars:
            plain = _truncate_to_fit(plain, max_chars, url_suffix)
        text = plain + url_suffix if plain else canonical_url

    elif kind == "like":
        target = post.like_of or canonical_url
        text = f"♥ {target}"

    elif kind == "repost":
        target = post.repost_of or canonical_url
        text = f"🔁 {target}"

    elif kind == "bookmark":
        target = post.bookmark_of or canonical_url
        text = f"🔖 {target}"

    elif kind == "reply":
        plain = _md_to_plain(content)
        url_suffix = f" {canonical_url}"
        if len(plain) + len(url_suffix) > max_chars:
            plain = _truncate_to_fit(plain, max_chars, url_suffix)
        text = plain + url_suffix if plain else canonical_url

    else:
        # activity, event, rsvp, checkin, and any future kinds
        plain = _md_to_plain(content) if content else post.title or ""
        url_suffix = f" {canonical_url}"
        if len(plain) + len(url_suffix) > max_chars:
            plain = _truncate_to_fit(plain, max_chars, url_suffix)
        text = plain + url_suffix if plain else canonical_url

    return text, cw_text
