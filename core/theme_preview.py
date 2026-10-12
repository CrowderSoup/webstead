"""Signed preview links that render one theme for a single request.

The token is the theme slug, signed with ``TimestampSigner``. Middleware sets
a request-scoped override that ``get_active_theme_slug()`` reads, so templates
and theme static URLs resolve that theme without changing the live one.
"""
from __future__ import annotations

import re
from contextvars import ContextVar, Token
from datetime import timedelta
from urllib.parse import urlencode

from django.core import signing
from django.template.loader import render_to_string

PREVIEW_MAX_AGE = timedelta(days=7)
_SALT = "core.theme-preview"
_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_BODY_OPEN_RE = re.compile(rb"<body\b[^>]*>", re.IGNORECASE)
_preview_slug: ContextVar[str | None] = ContextVar("theme_preview_slug", default=None)


def make_token(slug: str) -> str:
    return signing.TimestampSigner(salt=_SALT).sign(slug)


def token_slug(token: str, *, max_age: timedelta = PREVIEW_MAX_AGE) -> str | None:
    if not token:
        return None
    try:
        slug = signing.TimestampSigner(salt=_SALT).unsign(token, max_age=max_age)
    except signing.BadSignature:
        return None
    if not isinstance(slug, str) or not _SLUG_RE.fullmatch(slug):
        return None
    return slug


def preview_path(slug: str) -> str:
    return f"/?{urlencode({'theme_preview': make_token(slug)})}"


def current_preview_slug() -> str | None:
    return _preview_slug.get()


def set_preview_slug(slug: str) -> Token:
    return _preview_slug.set(slug)


def reset_preview_slug(token: Token) -> None:
    _preview_slug.reset(token)


def mark_preview_response(response, slug: str):
    """Keep a theme preview out of search engines, caches, and referrers."""
    response["X-Robots-Tag"] = "noindex, nofollow"
    response["Cache-Control"] = "private, no-store"
    response["Referrer-Policy"] = "no-referrer"
    content_type = response.get("Content-Type", "")
    if "text/html" not in content_type or not hasattr(response, "content"):
        return response
    try:
        banner = render_to_string("core/_theme_preview_banner.html", {"slug": slug}).encode(response.charset)
    except Exception:
        return response
    content = response.content
    match = _BODY_OPEN_RE.search(content)
    if match:
        response.content = content[: match.end()] + banner + content[match.end() :]
    else:
        response.content = banner + content
    # Middleware that ran earlier may have set Content-Length for the
    # original body; a stale value makes browsers cut off the page's end.
    if response.has_header("Content-Length"):
        response["Content-Length"] = str(len(response.content))
    return response
