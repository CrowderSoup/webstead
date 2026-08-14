"""
Strava OAuth views + webhook receiver.

oauth_start     — POST: redirect to Strava's authorization page
oauth_callback  — GET:  exchange code for tokens, create/update StravaAccount
webhook_receive — GET:  subscription validation handshake
                  POST: receive an activity/athlete event

Admin-facing management views (strava_settings, strava_disconnect,
strava_import) live in site_admin/views.py.
"""

import json
import logging
import secrets
from datetime import datetime, timezone as dt_timezone
from urllib.parse import urlencode

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpResponseForbidden, JsonResponse
from django.shortcuts import redirect
from django.urls import reverse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from . import client
from .models import StravaAccount

logger = logging.getLogger(__name__)

STRAVA_SCOPE = "activity:read_all"


def _get_redirect_uri(request):
    return request.build_absolute_uri(reverse("strava_oauth_callback"))


@login_required
@require_POST
def oauth_start(request):
    if not request.user.is_staff:
        return HttpResponseForbidden()

    if not settings.STRAVA_CLIENT_ID or not settings.STRAVA_CLIENT_SECRET:
        messages.error(
            request,
            "Strava is not configured. Set STRAVA_CLIENT_ID and STRAVA_CLIENT_SECRET.",
        )
        return redirect("site_admin:strava_settings")

    state = secrets.token_urlsafe(32)
    request.session["strava_oauth_state"] = state

    params = {
        "client_id": settings.STRAVA_CLIENT_ID,
        "redirect_uri": _get_redirect_uri(request),
        "response_type": "code",
        "approval_prompt": "auto",
        "scope": STRAVA_SCOPE,
        "state": state,
    }
    return redirect(f"{client.OAUTH_AUTHORIZE_URL}?{urlencode(params)}")


@login_required
@require_GET
def oauth_callback(request):
    if not request.user.is_staff:
        return HttpResponseForbidden()

    error = request.GET.get("error")
    if error:
        messages.error(request, f"Strava authorization denied: {error}")
        return redirect("site_admin:strava_settings")

    code = request.GET.get("code")
    if not code:
        messages.error(request, "No authorization code received from Strava.")
        return redirect("site_admin:strava_settings")

    expected_state = request.session.pop("strava_oauth_state", None)
    received_state = request.GET.get("state")
    if not expected_state or expected_state != received_state:
        messages.error(request, "OAuth state mismatch. Please try connecting again.")
        return redirect("site_admin:strava_settings")

    try:
        token_data = client.exchange_code_for_token(code)
        athlete = token_data.get("athlete") or {}

        # Single-active-account policy, same as MastodonAccount.
        StravaAccount.objects.filter(is_active=True).update(is_active=False)

        expires_at = datetime.fromtimestamp(token_data["expires_at"], tz=dt_timezone.utc)

        account, _ = StravaAccount.objects.update_or_create(
            athlete_id=str(athlete.get("id")),
            defaults={
                "access_token": token_data["access_token"],
                "refresh_token": token_data["refresh_token"],
                "expires_at": expires_at,
                "username": athlete.get("username") or "",
                "firstname": athlete.get("firstname") or "",
                "lastname": athlete.get("lastname") or "",
                "profile_photo_url": athlete.get("profile") or "",
                "is_active": True,
            },
        )
    except Exception as exc:
        logger.exception("Strava OAuth callback failed")
        messages.error(request, f"Failed to complete Strava connection: {exc}")
        return redirect("site_admin:strava_settings")

    messages.success(request, f"Connected to Strava as {account.display_name}.")
    return redirect("site_admin:strava_settings")


@csrf_exempt
def webhook_receive(request):
    """Strava POSTs JSON with no session/CSRF token, so this route is exempt."""
    if request.method == "GET":
        return _webhook_validate(request)
    if request.method == "POST":
        return _webhook_event(request)
    return HttpResponseForbidden()


def _webhook_validate(request):
    account = StravaAccount.get_active()
    mode = request.GET.get("hub.mode")
    challenge = request.GET.get("hub.challenge")
    verify_token = request.GET.get("hub.verify_token")

    if (
        mode != "subscribe"
        or not challenge
        or not account
        or not account.webhook_verify_token
        or verify_token != account.webhook_verify_token
    ):
        return HttpResponseForbidden()

    return JsonResponse({"hub.challenge": challenge})


def _webhook_event(request):
    from .tasks import handle_strava_webhook_event

    try:
        payload = json.loads(request.body or b"{}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse({"error": "invalid JSON"}, status=400)

    # Enqueue and return immediately — Strava requires a 200 within 2 seconds
    # and retries up to 3 times otherwise.
    handle_strava_webhook_event.delay(payload)
    return JsonResponse({}, status=200)
