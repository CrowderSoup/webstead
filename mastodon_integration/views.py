"""
Mastodon OAuth views.

oauth_start    — POST: register/look-up app, redirect to Mastodon authorisation page
oauth_callback — GET:  exchange code for token, create MastodonAccount, redirect to admin

Admin-facing management views (mastodon_settings, mastodon_connect, mastodon_disconnect,
mastodon_manual_sync) live in site_admin/views.py.
"""

import logging
import secrets

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import HttpResponseForbidden
from django.shortcuts import redirect
from django.urls import reverse
from django.views.decorators.http import require_POST, require_GET
from mastodon import Mastodon  # installed Mastodon.py library

from .models import MastodonApp, MastodonAccount
from .subscriptions import sync_managed_subscriptions

logger = logging.getLogger(__name__)

MASTODON_SCOPES = ["read", "write"]


def _get_redirect_uri(request):
    return request.build_absolute_uri(reverse("mastodon_oauth_callback"))


@login_required
@require_POST
def oauth_start(request):
    """
    Register (or look up) a Mastodon OAuth app for the given instance URL,
    then redirect the user to the Mastodon authorisation page.
    """
    if not request.user.is_staff:
        return HttpResponseForbidden()

    instance_url = request.POST.get("instance_url", "").strip().rstrip("/")
    if not instance_url:
        messages.error(request, "Please enter a Mastodon instance URL.")
        return redirect("site_admin:mastodon_settings")

    # Normalise to include scheme
    if not instance_url.startswith(("http://", "https://")):
        instance_url = f"https://{instance_url}"

    redirect_uri = _get_redirect_uri(request)

    try:
        app_obj = MastodonApp.objects.filter(instance_url=instance_url).first()

        if not app_obj or not app_obj.client_id:
            # Register a new OAuth app with the instance — only write to the DB
            # after successful registration so we never persist empty credentials.
            client_id, client_secret = Mastodon.create_app(
                "Webstead",
                api_base_url=instance_url,
                redirect_uris=redirect_uri,
                scopes=MASTODON_SCOPES,
            )
            with transaction.atomic():
                app_obj, _ = MastodonApp.objects.update_or_create(
                    instance_url=instance_url,
                    defaults={"client_id": client_id, "client_secret": client_secret},
                )

        mastodon = Mastodon(
            client_id=app_obj.client_id,
            client_secret=app_obj.client_secret,
            api_base_url=instance_url,
        )
        auth_url = mastodon.auth_request_url(
            redirect_uris=redirect_uri,
            scopes=MASTODON_SCOPES,
        )

    except Exception as exc:
        logger.exception("Mastodon OAuth start failed for %s", instance_url)
        messages.error(request, f"Could not connect to {instance_url}: {exc}")
        return redirect("site_admin:mastodon_settings")

    # Store instance URL and a CSRF state token in the session
    state = secrets.token_urlsafe(32)
    request.session["mastodon_oauth_instance_url"] = instance_url
    request.session["mastodon_oauth_state"] = state
    return redirect(f"{auth_url}&state={state}")


@login_required
@require_GET
def oauth_callback(request):
    """
    Mastodon redirects here after the user authorises (or denies) the app.
    Exchange the code for an access token and create the MastodonAccount.
    """
    if not request.user.is_staff:
        return HttpResponseForbidden()

    error = request.GET.get("error")
    if error:
        messages.error(request, f"Mastodon authorisation denied: {error}")
        return redirect("site_admin:mastodon_settings")

    code = request.GET.get("code")
    if not code:
        messages.error(request, "No authorisation code received from Mastodon.")
        return redirect("site_admin:mastodon_settings")

    # Verify the OAuth state token to prevent CSRF on the callback
    expected_state = request.session.pop("mastodon_oauth_state", None)
    received_state = request.GET.get("state")
    if not expected_state or expected_state != received_state:
        messages.error(request, "OAuth state mismatch. Please try connecting again.")
        return redirect("site_admin:mastodon_settings")

    instance_url = request.session.pop("mastodon_oauth_instance_url", None)
    if not instance_url:
        messages.error(request, "OAuth session expired. Please try connecting again.")
        return redirect("site_admin:mastodon_settings")

    try:
        app_obj = MastodonApp.objects.get(instance_url=instance_url)
    except MastodonApp.DoesNotExist:
        messages.error(request, "OAuth app not found. Please try connecting again.")
        return redirect("site_admin:mastodon_settings")

    redirect_uri = _get_redirect_uri(request)

    try:
        mastodon = Mastodon(
            client_id=app_obj.client_id,
            client_secret=app_obj.client_secret,
            api_base_url=instance_url,
        )

        # Exchange code for access token
        access_token = mastodon.log_in(
            code=code,
            redirect_uri=redirect_uri,
            scopes=MASTODON_SCOPES,
        )

        # Re-initialise with the access token to fetch account info
        mastodon = Mastodon(
            client_id=app_obj.client_id,
            client_secret=app_obj.client_secret,
            access_token=access_token,
            api_base_url=instance_url,
        )
        me = mastodon.me()
        instance_info = mastodon.instance()

        # Derive full handle (username@instance_host)
        instance_host = instance_url.replace("https://", "").replace("http://", "")
        username = f"{me['username']}@{instance_host}"

        # Fetch max_toot_chars — try v2 first, fall back to 500
        try:
            max_chars = instance_info.get("configuration", {}).get(
                "statuses", {}
            ).get("max_characters") or instance_info.get("max_toot_chars") or 500
        except Exception:
            max_chars = 500

        # Deactivate any existing accounts (one active account policy).
        # Also clear their channel assignments so orphaned channels don't
        # continue to be polled under the old inactive account.
        MastodonAccount.objects.filter(is_active=True).update(
            is_active=False,
            timeline_channel=None,
            notifications_channel=None,
        )

        account_obj, _ = MastodonAccount.objects.update_or_create(
            app=app_obj,
            account_id=str(me["id"]),
            defaults={
                "access_token": access_token,
                "username": username,
                "display_name": me.get("display_name", "") or "",
                "avatar_url": me.get("avatar", "") or "",
                "max_toot_chars": int(max_chars),
                "is_active": True,
            },
        )
        sync_managed_subscriptions(account_obj)

    except Exception as exc:
        logger.exception("Mastodon OAuth callback failed for %s", instance_url)
        messages.error(request, f"Failed to complete Mastodon connection: {exc}")
        return redirect("site_admin:mastodon_settings")

    messages.success(
        request,
        f"Connected to Mastodon as @{account_obj.username}.",
    )
    return redirect("site_admin:mastodon_settings")
