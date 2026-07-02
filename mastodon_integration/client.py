"""
Thin wrapper around Mastodon.py.

Provides:
  - get_client(account)     → authenticated Mastodon instance
  - status_to_jf2(status)   → JF2 dict for use as microsub.Entry.data
"""

import logging
import re

from mastodon import Mastodon  # installed Mastodon.py library

from .models import MastodonAccount
from ._utils import _get

logger = logging.getLogger(__name__)


def get_client(account: MastodonAccount) -> Mastodon:
    """Return an authenticated Mastodon client for the given account."""
    return Mastodon(
        client_id=account.app.client_id,
        client_secret=account.app.client_secret,
        access_token=account.access_token,
        api_base_url=account.app.instance_url,
    )


def strip_html(html: str) -> str:
    """Very lightweight HTML stripper for toot content."""
    # Replace block-level tags with newlines before stripping
    html = re.sub(r"<br\s*/?>", "\n", html, flags=re.IGNORECASE)
    html = re.sub(r"</p>", "\n\n", html, flags=re.IGNORECASE)
    html = re.sub(r"<[^>]+>", "", html)
    return html.strip()


def status_to_jf2(status) -> dict:
    """
    Convert a Mastodon.py Status dict/object to a JF2 dict compatible with
    microsub.Entry.data.
    """
    # Mastodon.py returns AttribAccessDict objects — treat them like dicts
    # but access as attributes for clarity.

    # If this is a boost, use the original status for content but record
    # the boost as a repost-of relationship.
    reblog = status.get("reblog") if isinstance(status, dict) else status.reblog
    content_status = reblog if reblog else status

    account = _get(content_status, "account")
    media_attachments = _get(content_status, "media_attachments") or []
    spoiler_text = _get(content_status, "spoiler_text") or ""
    content_html = _get(content_status, "content") or ""
    url = _get(content_status, "url") or ""
    in_reply_to_url = _get(content_status, "in_reply_to_url") or ""
    created_at = _get(status, "created_at")  # use original status timestamp

    jf2 = {
        "type": "entry",
        "content": {
            "html": content_html,
            "text": strip_html(content_html),
        },
        "url": url,
        "published": created_at.isoformat() if created_at else None,
        "author": {
            "type": "card",
            "name": _get(account, "display_name") or _get(account, "username") or "",
            "url": _get(account, "url") or "",
            "photo": _get(account, "avatar") or "",
        },
    }

    # Optional fields — only include when non-empty
    if spoiler_text:
        jf2["summary"] = spoiler_text

    photos = [_get(a, "url") for a in media_attachments if _get(a, "type") == "image"]
    if photos:
        jf2["photo"] = photos

    videos = [_get(a, "url") for a in media_attachments if _get(a, "type") == "video"]
    if videos:
        jf2["video"] = videos

    if reblog:
        jf2["repost-of"] = _get(reblog, "url") or ""

    if in_reply_to_url:
        jf2["in-reply-to"] = [in_reply_to_url]

    return jf2
