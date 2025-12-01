import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone as dt_timezone
from typing import Iterable, List, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from django.conf import settings
from django.utils import timezone as django_timezone
from django.utils.text import Truncator

from core.models import SiteConfiguration
from .models import Post

logger = logging.getLogger(__name__)


@dataclass
class SyndicationTarget:
    uid: str
    name: str

    def is_configured(self) -> bool:
        return False

    def should_syndicate(self, post: Post) -> bool:
        return post.kind in (Post.NOTE, Post.PHOTO)

    def _compose_status(self, post: Post, canonical_url: str, limit: int) -> str:
        summary_with_link = f"{post.summary()} {canonical_url}".strip()
        return Truncator(summary_with_link).chars(limit, truncate="")

    def syndicate(self, post: Post, canonical_url: str) -> str | None:  # pragma: no cover - interface
        raise NotImplementedError


class MastodonTarget(SyndicationTarget):
    def __init__(self):
        super().__init__(uid="mastodon", name="Mastodon")
        config = SiteConfiguration.get_solo()
        env_base = getattr(settings, "MASTODON_BASE_URL", "")
        env_token = getattr(settings, "MASTODON_ACCESS_TOKEN", "")

        self.base_url = (config.mastodon_base_url or env_base).rstrip("/")
        self.access_token = (config.mastodon_access_token or env_token).strip()
        self.refresh_token = (config.mastodon_refresh_token or "").strip()
        self.expires_at = config.mastodon_token_expires_at
        self.client_id = (config.mastodon_client_id or "").strip()
        self.client_secret = (config.mastodon_client_secret or "").strip()

    def is_configured(self) -> bool:
        if not self.base_url:
            return False
        if self.access_token and not self._is_expired():
            return True
        return bool(self.refresh_token)

    def _is_expired(self) -> bool:
        return bool(self.expires_at and self.expires_at <= django_timezone.now())

    def _save_tokens(self, access_token: str, refresh_token: str | None, expires_at):
        config = SiteConfiguration.get_solo()
        config.mastodon_access_token = access_token
        config.mastodon_refresh_token = refresh_token or ""
        config.mastodon_token_expires_at = expires_at
        if self.base_url and self.base_url != config.mastodon_base_url:
            config.mastodon_base_url = self.base_url
        if self.client_id and self.client_id != config.mastodon_client_id:
            config.mastodon_client_id = self.client_id
        if self.client_secret and self.client_secret != config.mastodon_client_secret:
            config.mastodon_client_secret = self.client_secret
        config.save(
            update_fields=[
                "mastodon_access_token",
                "mastodon_refresh_token",
                "mastodon_token_expires_at",
                "mastodon_base_url",
                "mastodon_client_id",
                "mastodon_client_secret",
            ]
        )
        self.access_token = access_token
        self.refresh_token = refresh_token or ""
        self.expires_at = expires_at
        return access_token

    def _refresh_access_token(self) -> str | None:
        if not (self.refresh_token and self.client_id and self.client_secret and self.base_url):
            return None

        payload = urlencode(
            {
                "grant_type": "refresh_token",
                "refresh_token": self.refresh_token,
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            }
        ).encode("utf-8")
        request = Request(
            f"{self.base_url}/oauth/token",
            data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )

        try:
            with urlopen(request, timeout=10) as response:
                body = response.read().decode()
                if response.status >= 400:
                    logger.error("Mastodon token refresh failed with status %s: %s", response.status, body)
                    return None
        except (HTTPError, URLError, TimeoutError) as exc:  # pragma: no cover - network
            logger.exception("Error refreshing Mastodon token: %s", exc)
            return None

        try:
            data = json.loads(body or "{}")
        except json.JSONDecodeError:
            logger.error("Invalid JSON from Mastodon refresh response")
            return None

        token = data.get("access_token")
        if not isinstance(token, str) or not token:
            logger.error("Mastodon refresh response missing access_token")
            return None

        expires_at = None
        expires_in = data.get("expires_in")
        try:
            if expires_in:
                expires_at = django_timezone.now() + timedelta(seconds=int(expires_in))
        except (TypeError, ValueError):
            expires_at = None

        refresh_token = data.get("refresh_token", self.refresh_token)
        return self._save_tokens(token, refresh_token, expires_at)

    def syndicate(self, post: Post, canonical_url: str) -> str | None:
        if not self.is_configured() or not self.should_syndicate(post):
            return None

        access_token = self.access_token
        if not access_token or self._is_expired():
            access_token = self._refresh_access_token()

        if not access_token:
            return None

        status = self._compose_status(post, canonical_url, limit=470)
        payload = json.dumps({"status": status}).encode("utf-8")
        endpoint = f"{self.base_url}/api/v1/statuses"
        request = Request(
            endpoint,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/json",
            },
            method="POST",
        )

        try:
            with urlopen(request, timeout=10) as response:
                body = response.read().decode()
                if response.status >= 400:
                    logger.error("Mastodon syndication failed with status %s: %s", response.status, body)
                    return None
        except (HTTPError, URLError, TimeoutError) as exc:  # pragma: no cover - network
            logger.exception("Error posting to Mastodon: %s", exc)
            return None

        try:
            data = json.loads(body or "{}")
        except json.JSONDecodeError:
            logger.error("Invalid JSON from Mastodon response")
            return None

        url = data.get("url")
        if isinstance(url, str):
            return url
        logger.error("Mastodon response missing status URL")
        return None


class BlueskyTarget(SyndicationTarget):
    def __init__(self):
        super().__init__(uid="bluesky", name="Bluesky")
        config = SiteConfiguration.get_solo()
        env_service = getattr(settings, "BLUESKY_SERVICE", "https://bsky.social")
        env_handle = getattr(settings, "BLUESKY_HANDLE", "")

        self.service = (config.bluesky_service or env_service).rstrip("/")
        self.handle = (config.bluesky_handle or env_handle).strip()
        self.did = (config.bluesky_did or "").strip()
        self.client_id = (config.bluesky_client_id or "").strip()
        self.client_secret = (config.bluesky_client_secret or "").strip()
        self.access_token = (config.bluesky_access_token or "").strip()
        self.refresh_token = (config.bluesky_refresh_token or "").strip()
        self.expires_at = config.bluesky_token_expires_at
        self.app_password = getattr(settings, "BLUESKY_APP_PASSWORD", "")

    def is_configured(self) -> bool:
        token_available = self.access_token and not self._is_expired()
        token_ready = token_available and (self.did or self.handle)
        return bool(token_ready or (self.handle and self.app_password) or self.refresh_token)

    def _is_expired(self) -> bool:
        return bool(self.expires_at and self.expires_at <= django_timezone.now())

    def _persist_tokens(
        self,
        access_token: str,
        refresh_token: str | None,
        expires_at,
        *,
        did: str | None = None,
        handle: str | None = None,
    ) -> Mapping[str, str]:
        config = SiteConfiguration.get_solo()
        updates = [
            "bluesky_access_token",
            "bluesky_refresh_token",
            "bluesky_token_expires_at",
            "bluesky_service",
        ]
        config.bluesky_access_token = access_token
        config.bluesky_refresh_token = refresh_token or ""
        config.bluesky_token_expires_at = expires_at
        config.bluesky_service = self.service

        if self.client_id:
            config.bluesky_client_id = self.client_id
            updates.append("bluesky_client_id")
        if self.client_secret:
            config.bluesky_client_secret = self.client_secret
            updates.append("bluesky_client_secret")
        if did:
            config.bluesky_did = did
            updates.append("bluesky_did")
        if handle:
            config.bluesky_handle = handle
            updates.append("bluesky_handle")

        config.save(update_fields=updates)
        self.access_token = access_token
        self.refresh_token = refresh_token or ""
        self.expires_at = expires_at
        self.did = did or self.did
        self.handle = handle or self.handle

        return {"accessJwt": access_token, "refreshJwt": refresh_token, "did": self.did, "handle": self.handle}

    def _fetch_identity(self, token: str) -> tuple[str | None, str | None]:
        endpoint = f"{self.service}/xrpc/com.atproto.server.getSession"
        request = Request(
            endpoint,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            method="GET",
        )
        try:
            with urlopen(request, timeout=10) as response:
                body = response.read().decode()
                if response.status >= 400:
                    logger.error("Bluesky getSession failed with status %s: %s", response.status, body)
                    return None, None
        except (HTTPError, URLError, TimeoutError) as exc:  # pragma: no cover - network
            logger.exception("Error fetching Bluesky identity: %s", exc)
            return None, None

        try:
            data = json.loads(body or "{}")
        except json.JSONDecodeError:
            logger.error("Invalid JSON from Bluesky session response")
            return None, None

        did = data.get("did") if isinstance(data.get("did"), str) else None
        handle = data.get("handle") if isinstance(data.get("handle"), str) else None
        return did, handle

    def _refresh_oauth_token(self) -> Mapping[str, str] | None:
        if not (self.refresh_token and self.client_id):
            return None

        payload = {
            "grant_type": "refresh_token",
            "refresh_token": self.refresh_token,
            "client_id": self.client_id,
        }
        if self.client_secret:
            payload["client_secret"] = self.client_secret

        body_payload = urlencode(payload).encode("utf-8")
        request = Request(
            f"{self.service}/oauth/token",
            data=body_payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )

        try:
            with urlopen(request, timeout=10) as response:
                body = response.read().decode()
                if response.status >= 400:
                    logger.error("Bluesky token refresh failed with status %s: %s", response.status, body)
                    return None
        except (HTTPError, URLError, TimeoutError) as exc:  # pragma: no cover - network
            logger.exception("Error refreshing Bluesky token: %s", exc)
            return None

        try:
            data = json.loads(body or "{}")
        except json.JSONDecodeError:
            logger.error("Invalid JSON from Bluesky refresh response")
            return None

        access_token = data.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            logger.error("Bluesky refresh response missing access_token")
            return None

        expires_at = None
        expires_in = data.get("expires_in")
        try:
            if expires_in:
                expires_at = django_timezone.now() + timedelta(seconds=int(expires_in))
        except (TypeError, ValueError):
            expires_at = None

        refresh_token = data.get("refresh_token", self.refresh_token)
        did = data.get("did") if isinstance(data.get("did"), str) else None
        handle = data.get("handle") if isinstance(data.get("handle"), str) else None

        if not (did and handle):
            fetched_did, fetched_handle = self._fetch_identity(access_token)
            did = did or fetched_did
            handle = handle or fetched_handle

        return self._persist_tokens(access_token, refresh_token, expires_at, did=did, handle=handle)

    def _create_session(self) -> Mapping[str, str] | None:
        payload = json.dumps({"identifier": self.handle, "password": self.app_password}).encode("utf-8")
        endpoint = f"{self.service}/xrpc/com.atproto.server.createSession"
        request = Request(
            endpoint,
            data=payload,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=10) as response:
                body = response.read().decode()
                if response.status >= 400:
                    logger.error("Bluesky session failed with status %s: %s", response.status, body)
                    return None
        except (HTTPError, URLError, TimeoutError) as exc:  # pragma: no cover - network
            logger.exception("Error creating Bluesky session: %s", exc)
            return None

        try:
            session = json.loads(body or "{}")
        except json.JSONDecodeError:
            logger.error("Invalid JSON from Bluesky session response")
            return None

        if not isinstance(session.get("accessJwt"), str) or not isinstance(session.get("did"), str):
            logger.error("Bluesky session missing credentials")
            return None

        return session

    def syndicate(self, post: Post, canonical_url: str) -> str | None:
        if not self.is_configured() or not self.should_syndicate(post):
            return None

        session = None
        token_expired = self._is_expired()
        if self.access_token and not token_expired:
            session = {"accessJwt": self.access_token, "did": self.did, "handle": self.handle}

        if not session and self.refresh_token:
            session = self._refresh_oauth_token()

        if (not session or not session.get("accessJwt")) and self.handle and self.app_password:
            session = self._create_session()

        if not session or not session.get("accessJwt"):
            return None

        if (not session.get("did") or not session.get("handle")) and session.get("accessJwt"):
            did, handle = self._fetch_identity(session["accessJwt"])
            session["did"] = session.get("did") or did
            session["handle"] = session.get("handle") or handle
            if session.get("did") and session.get("handle"):
                self._persist_tokens(
                    session["accessJwt"],
                    session.get("refreshJwt") or self.refresh_token,
                    self.expires_at,
                    did=session["did"],
                    handle=session["handle"],
                )

        text = self._compose_status(post, canonical_url, limit=280)
        record = {
            "$type": "app.bsky.feed.post",
            "text": text,
            "createdAt": datetime.now(dt_timezone.utc).isoformat().replace("+00:00", "Z"),
        }
        repo_id = session.get("did")
        if not repo_id:
            return None
        payload = json.dumps({"repo": repo_id, "collection": "app.bsky.feed.post", "record": record}).encode("utf-8")
        endpoint = f"{self.service}/xrpc/com.atproto.repo.createRecord"
        request = Request(
            endpoint,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Authorization": f"Bearer {session.get('accessJwt')}",
            },
            method="POST",
        )

        try:
            with urlopen(request, timeout=10) as response:
                body = response.read().decode()
                if response.status >= 400:
                    logger.error("Bluesky syndication failed with status %s: %s", response.status, body)
                    return None
        except (HTTPError, URLError, TimeoutError) as exc:  # pragma: no cover - network
            logger.exception("Error posting to Bluesky: %s", exc)
            return None

        try:
            data = json.loads(body or "{}")
        except json.JSONDecodeError:
            logger.error("Invalid JSON from Bluesky response")
            return None

        uri = data.get("uri", "")
        if not isinstance(uri, str) or not uri:
            logger.error("Bluesky response missing URI")
            return None

        rkey = uri.split("/")[-1]
        if not rkey:
            return None

        handle = session.get("handle") or self.handle
        return f"https://bsky.app/profile/{handle}/post/{rkey}"


def _all_targets() -> List[SyndicationTarget]:
    return [MastodonTarget(), BlueskyTarget()]


def available_targets() -> List[SyndicationTarget]:
    return [target for target in _all_targets() if target.is_configured()]


def target_statuses() -> list[dict[str, object]]:
    statuses: list[dict[str, object]] = []
    for target in _all_targets():
        statuses.append(
            {
                "uid": target.uid,
                "name": target.name,
                "connected": target.is_configured(),
            }
        )
    return statuses


def syndicate_post(post: Post, canonical_url: str, target_ids: Iterable[str] | None = None) -> dict[str, str]:
    requested = set(target_ids or [])
    results: dict[str, str] = {}

    for target in available_targets():
        if requested and target.uid not in requested:
            continue
        try:
            url = target.syndicate(post, canonical_url)
        except Exception:  # pragma: no cover - defensive
            logger.exception("Unexpected error syndicating to %s", target.uid)
            continue

        if url:
            results[target.uid] = url
    return results
