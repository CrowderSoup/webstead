"""Minting and looking up access tokens issued by this site."""
from __future__ import annotations

import secrets

from django.utils import timezone

from .models import IndieAuthAccessToken
from .views import _hash_token

# Scopes a personal access token can carry. Micropub's standard ones plus
# the ones only MCP uses; see the AI-first CMS spec for what each allows.
PERSONAL_TOKEN_SCOPES = (
    ("read", "Read posts, drafts, and revisions"),
    ("draft", "Create drafts and edit posts that are still drafts"),
    ("create", "Publish posts (now or scheduled) and unpublish them"),
    ("update", "Edit any post, including live ones, and revert revisions"),
    ("delete", "Delete posts"),
    ("undelete", "Restore deleted posts"),
    ("media", "Upload images"),
)


def mint_personal_token(user, *, name: str, scopes, me: str, expires_in=None):
    """Create a personal access token. Returns (token, raw_value); the raw
    value is shown once and only its hash is stored."""
    raw = secrets.token_urlsafe(32)
    token = IndieAuthAccessToken.objects.create(
        token_hash=_hash_token(raw),
        client_id=IndieAuthAccessToken.PERSONAL_CLIENT_ID,
        me=me,
        scope=" ".join(sorted(set(scopes))),
        user=user,
        name=name,
        expires_at=timezone.now() + expires_in if expires_in else None,
    )
    return token, raw


def active_local_token(raw: str) -> IndieAuthAccessToken | None:
    """The unrevoked, unexpired token this site issued for ``raw``, if any."""
    if not raw:
        return None
    token = (
        IndieAuthAccessToken.objects.select_related("user")
        .filter(token_hash=_hash_token(raw), revoked_at__isnull=True)
        .first()
    )
    if token is None or (token.expires_at and token.expires_at <= timezone.now()):
        return None
    return token
