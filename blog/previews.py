"""Signed, expiring preview links for posts that aren't live yet.

A link is the post URL plus ``?preview=<token>``, where the token is the post
id signed with ``TimestampSigner``. It shows a draft or scheduled post to
anyone holding it until it expires. A deleted post 404s whatever the token.
"""
from __future__ import annotations

import re
from datetime import timedelta
from urllib.parse import urlencode

from django.core import signing
from django.template.loader import render_to_string

PREVIEW_MAX_AGE = timedelta(days=7)
_SALT = "blog.post-preview"
_BODY_OPEN_RE = re.compile(rb"<body\b[^>]*>", re.IGNORECASE)


def make_token(post) -> str:
    return signing.TimestampSigner(salt=_SALT).sign(str(post.pk))


def token_is_valid(token: str, post, *, max_age: timedelta = PREVIEW_MAX_AGE) -> bool:
    if not token:
        return False
    try:
        value = signing.TimestampSigner(salt=_SALT).unsign(token, max_age=max_age)
    except signing.BadSignature:  # includes SignatureExpired
        return False
    return value == str(post.pk)


def preview_path(post) -> str:
    return f"{post.get_absolute_url()}?{urlencode({'preview': make_token(post)})}"


def mark_preview_response(request, response, post):
    """Keep a preview out of search engines, caches, referrers, and analytics,
    and show a banner so nobody mistakes it for the live site."""
    request._skip_analytics = True
    response["X-Robots-Tag"] = "noindex, nofollow"
    response["Cache-Control"] = "private, no-store"
    response["Referrer-Policy"] = "no-referrer"

    banner = render_to_string("blog/_preview_banner.html", {"post": post}).encode(response.charset)
    content = response.content
    match = _BODY_OPEN_RE.search(content)
    if match:
        response.content = content[: match.end()] + banner + content[match.end():]
    else:
        response.content = banner + content
    return response
