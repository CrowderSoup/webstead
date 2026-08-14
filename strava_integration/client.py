"""
Strava API client.

A thin wrapper over `requests` — Strava's API is plain OAuth2 REST, so no
dedicated SDK dependency is needed here (unlike Mastodon, whose per-instance
protocol justified pulling in the Mastodon.py library).
"""

import logging
from datetime import datetime, timezone as dt_timezone

import requests
from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)

API_BASE = "https://www.strava.com/api/v3"
OAUTH_AUTHORIZE_URL = "https://www.strava.com/oauth/authorize"
OAUTH_TOKEN_URL = "https://www.strava.com/oauth/token"
OAUTH_DEAUTHORIZE_URL = "https://www.strava.com/oauth/deauthorize"

# Refresh a little before actual expiry so a request never races token expiry.
TOKEN_REFRESH_MARGIN_SECONDS = 300

REQUEST_TIMEOUT = 15


class StravaAPIError(Exception):
    """
    Raised for non-2xx Strava API responses.

    `retriable` marks rate-limit (429) and server (5xx) errors — the errors
    Celery tasks should retry on, as opposed to 4xx auth/validation errors
    that won't resolve themselves.
    """

    def __init__(self, message, status_code=None, retriable=False):
        super().__init__(message)
        self.status_code = status_code
        self.retriable = retriable


def _raise_for_response(response):
    if response.ok:
        return
    retriable = response.status_code == 429 or response.status_code >= 500
    raise StravaAPIError(
        f"Strava API error {response.status_code}: {response.text[:500]}",
        status_code=response.status_code,
        retriable=retriable,
    )


def exchange_code_for_token(code: str) -> dict:
    response = requests.post(
        OAUTH_TOKEN_URL,
        data={
            "client_id": settings.STRAVA_CLIENT_ID,
            "client_secret": settings.STRAVA_CLIENT_SECRET,
            "code": code,
            "grant_type": "authorization_code",
        },
        timeout=REQUEST_TIMEOUT,
    )
    _raise_for_response(response)
    return response.json()


def _refresh_token(refresh_token: str) -> dict:
    response = requests.post(
        OAUTH_TOKEN_URL,
        data={
            "client_id": settings.STRAVA_CLIENT_ID,
            "client_secret": settings.STRAVA_CLIENT_SECRET,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        },
        timeout=REQUEST_TIMEOUT,
    )
    _raise_for_response(response)
    return response.json()


def get_valid_access_token(account) -> str:
    """
    Return a valid access token for `account`, refreshing (and persisting the
    rotated refresh token) if the current one is expired or expiring soon.

    Strava rotates the refresh token on every refresh, so both tokens must be
    re-saved together — persisting only the new access token would silently
    invalidate future refreshes.
    """
    now = timezone.now()
    if account.expires_at and (account.expires_at - now).total_seconds() > TOKEN_REFRESH_MARGIN_SECONDS:
        return account.access_token

    token_data = _refresh_token(account.refresh_token)
    account.access_token = token_data["access_token"]
    account.refresh_token = token_data["refresh_token"]
    account.expires_at = datetime.fromtimestamp(token_data["expires_at"], tz=dt_timezone.utc)
    account.save(update_fields=["access_token", "refresh_token", "expires_at"])
    return account.access_token


def _get(account, path, params=None):
    token = get_valid_access_token(account)
    response = requests.get(
        f"{API_BASE}{path}",
        headers={"Authorization": f"Bearer {token}"},
        params=params or {},
        timeout=REQUEST_TIMEOUT,
    )
    _raise_for_response(response)
    return response.json()


def list_activities(account, after=None, before=None, page=1, per_page=30):
    """GET /athlete/activities — after/before are epoch-second timestamps."""
    params = {"page": page, "per_page": per_page}
    if after is not None:
        params["after"] = int(after)
    if before is not None:
        params["before"] = int(before)
    return _get(account, "/athlete/activities", params)


def get_activity(account, activity_id):
    """GET /activities/{id} — DetailedActivity."""
    return _get(account, f"/activities/{activity_id}")


def get_activity_streams(account, activity_id):
    """GET /activities/{id}/streams — latlng/altitude/time series for the GPX track."""
    return _get(
        account,
        f"/activities/{activity_id}/streams",
        {"keys": "latlng,altitude,time", "key_by_type": "true"},
    )


def list_activity_photos(account, activity_id):
    """GET /activities/{id}/photos."""
    return _get(
        account,
        f"/activities/{activity_id}/photos",
        {"size": 2000, "photo_sources": "true"},
    )


def create_push_subscription(callback_url: str, verify_token: str) -> dict:
    """
    POST /push_subscriptions — an application may only have one subscription
    at a time; it covers webhook events for every athlete who has authorized
    the app.
    """
    response = requests.post(
        f"{API_BASE}/push_subscriptions",
        data={
            "client_id": settings.STRAVA_CLIENT_ID,
            "client_secret": settings.STRAVA_CLIENT_SECRET,
            "callback_url": callback_url,
            "verify_token": verify_token,
        },
        timeout=REQUEST_TIMEOUT,
    )
    _raise_for_response(response)
    return response.json()


def delete_push_subscription(subscription_id) -> None:
    response = requests.delete(
        f"{API_BASE}/push_subscriptions/{subscription_id}",
        params={
            "client_id": settings.STRAVA_CLIENT_ID,
            "client_secret": settings.STRAVA_CLIENT_SECRET,
        },
        timeout=REQUEST_TIMEOUT,
    )
    if response.status_code not in (204, 404):
        _raise_for_response(response)


def revoke_access(account) -> None:
    """Deauthorize the app for this athlete, invalidating all their tokens."""
    token = get_valid_access_token(account)
    response = requests.post(
        OAUTH_DEAUTHORIZE_URL,
        data={"access_token": token},
        timeout=REQUEST_TIMEOUT,
    )
    _raise_for_response(response)
