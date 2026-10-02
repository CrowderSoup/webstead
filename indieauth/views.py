import base64
import hashlib
import json
import logging
import ipaddress
import secrets
import socket
from datetime import timedelta
from html.parser import HTMLParser
import http.client
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from django.conf import settings
from django.db import transaction
from django.http import HttpResponseBadRequest, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from core.models import SiteConfiguration, RequestErrorLog
from core.request_logs import log_request_error

from .models import (
    IndieAuthAccessToken,
    IndieAuthAuthorizationCode,
    IndieAuthClient,
    IndieAuthConsent,
)

logger = logging.getLogger(__name__)

INDIEAUTH_REDACT_FIELDS = {
    "access_token",
    "refresh_token",
    "client_secret",
    "code",
    "code_verifier",
    "code_challenge",
    "token",
}

AUTH_CODE_TTL = timedelta(minutes=10)
ACCESS_TOKEN_TTL = timedelta(days=30)
CLIENT_CACHE_TTL = timedelta(hours=12)
MAX_METADATA_BYTES = 1_000_000


def _log_indieauth_error(request, response) -> None:
    if response.status_code < 400:
        return
    try:
        log_request_error(
            RequestErrorLog.SOURCE_INDIEAUTH,
            request,
            response,
            redact_fields=INDIEAUTH_REDACT_FIELDS,
        )
    except Exception:
        logger.exception(
            "IndieAuth error log failed",
            extra={"indieauth_path": request.path, "indieauth_status": response.status_code},
        )


class _ClientMetadataParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.redirect_uris = []
        self.logo_url = ""
        self.title = ""
        self._capture_title = False

    def handle_starttag(self, tag, attrs):
        attr_map = {key.lower(): value for key, value in attrs}
        if tag.lower() == "link":
            rel_value = attr_map.get("rel", "")
            href = attr_map.get("href")
            if not rel_value or not href:
                return
            rels = {rel.strip() for rel in rel_value.split() if rel.strip()}
            if "redirect_uri" in rels:
                self.redirect_uris.append(href)
            if "icon" in rels or "logo" in rels:
                if not self.logo_url:
                    self.logo_url = href
        elif tag.lower() == "title":
            self._capture_title = True

    def handle_endtag(self, tag):
        if tag.lower() == "title":
            self._capture_title = False

    def handle_data(self, data):
        if self._capture_title and not self.title:
            self.title = (data or "").strip()


def _hash_token(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _issuer(request) -> str:
    base = request.build_absolute_uri("/")
    return base[:-1] if base.endswith("/") else base


def _normalize_url(value: str) -> str | None:
    if not value:
        return None
    value = value.strip()
    parsed = urlparse(value)
    if not parsed.scheme:
        parsed = urlparse(f"https://{value}")
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None
    if parsed.username or parsed.password:
        return None
    if parsed.fragment:
        return None
    if parsed.port and not (_is_localhost(parsed.hostname) and settings.DEBUG):
        return None
    path = parsed.path or "/"
    if value.endswith("/") and not path.endswith("/"):
        path = f"{path}/"
    cleaned = parsed._replace(path=path, fragment="", query="")
    return cleaned.geturl()


def _is_localhost(hostname: str | None) -> bool:
    if not hostname:
        return False
    return hostname.lower() in {"localhost", "127.0.0.1", "::1"}


def _resolve_host_ips(hostname: str) -> set[str]:
    results = set()
    for family, _, _, _, sockaddr in socket.getaddrinfo(hostname, None):
        if family == socket.AF_INET:
            results.add(sockaddr[0])
        elif family == socket.AF_INET6:
            results.add(sockaddr[0])
    return results


def _is_public_host(hostname: str | None) -> bool:
    if not hostname:
        return False
    if _is_localhost(hostname):
        return False
    try:
        addresses = _resolve_host_ips(hostname)
    except socket.gaierror:
        return False
    if not addresses:
        return False
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
        ):
            return False
    return True


def _allowed_me_urls(request) -> set[str]:
    allowed = set()
    base = _normalize_url(request.build_absolute_uri("/"))
    if base:
        allowed.add(base)
    settings_obj = SiteConfiguration.get_solo()
    if settings_obj.site_author_id:
        hcard = (
            settings_obj.site_author.hcards.prefetch_related("urls")
            .order_by("pk")
            .first()
        )
        if hcard:
            for url in hcard.urls.all():
                normalized = _normalize_url(url.value)
                if normalized:
                    allowed.add(normalized)
    return allowed


def _is_allowed_me(request, me_url: str) -> bool:
    normalized = _normalize_url(me_url)
    if not normalized:
        return False
    return normalized in _allowed_me_urls(request)


def _normalize_scopes(scope_value: str) -> list[str]:
    if not scope_value:
        return []
    return [item for item in scope_value.split() if item]


def _redirect_with_params(base_url: str, params: dict) -> str:
    parsed = urlparse(base_url)
    query = parse_qs(parsed.query)
    for key, value in params.items():
        if value is None:
            continue
        query[key] = [str(value)]
    new_query = urlencode(query, doseq=True)
    return urlunparse(parsed._replace(query=new_query))


def _base64url_sha256(value: str) -> str:
    digest = hashlib.sha256(value.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _parse_link_header(header_value: str, rel_name: str) -> list[str]:
    results = []
    for part in header_value.split(","):
        segment = part.strip()
        if not segment.startswith("<") or ">" not in segment:
            continue
        url, _, params = segment.partition(">")
        rel = None
        for param in params.split(";"):
            name, _, value = param.strip().partition("=")
            if name.lower() == "rel":
                rel = value.strip('"')
                break
        if rel and rel_name in rel.split():
            results.append(url[1:])
    return results


def _fetch_client_metadata(client_id: str) -> dict:
    parsed = urlparse(client_id)
    if not _is_public_host(parsed.hostname):
        raise ValueError("Client host is not allowed")

    class _RedirectGuard(HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            if not newurl:
                return None
            target = urlparse(newurl)
            if not _is_public_host(target.hostname):
                raise ValueError("Redirect target is not allowed")
            return super().redirect_request(req, fp, code, msg, headers, newurl)

    opener = build_opener(_RedirectGuard())
    request = Request(client_id, headers={"User-Agent": "webstead-indieauth"})
    with opener.open(request, timeout=10) as response:
        content_type = response.headers.get("Content-Type", "")
        body_bytes = response.read(MAX_METADATA_BYTES + 1)
        if len(body_bytes) > MAX_METADATA_BYTES:
            raise ValueError("Client metadata response too large")
        body = body_bytes.decode("utf-8", errors="ignore")
        redirect_uris: list[str] = []
        client_name = ""
        logo_url = ""

        link_header = response.headers.get("Link")
        if link_header:
            for redirect_uri in _parse_link_header(link_header, "redirect_uri"):
                redirect_uris.append(urljoin(client_id, redirect_uri))
            logos = _parse_link_header(link_header, "logo")
            if logos:
                logo_url = urljoin(client_id, logos[0])

        if "json" in content_type:
            try:
                payload = json.loads(body or "{}")
            except json.JSONDecodeError:
                payload = {}
            if isinstance(payload, dict):
                redirect_value = payload.get("redirect_uris")
                if isinstance(redirect_value, list):
                    redirect_uris.extend(redirect_value)
                elif isinstance(redirect_value, str):
                    redirect_uris.append(redirect_value)
                client_name = str(payload.get("client_name") or payload.get("name") or "")
                logo_url = str(payload.get("logo_uri") or payload.get("logo") or logo_url or "")
        elif "html" in content_type:
            parser = _ClientMetadataParser()
            parser.feed(body)
            redirect_uris.extend(parser.redirect_uris)
            if parser.title:
                client_name = parser.title
            if parser.logo_url:
                logo_url = parser.logo_url

        cleaned_redirects = []
        for uri in redirect_uris:
            if not uri:
                continue
            absolute = urljoin(client_id, uri)
            parsed = urlparse(absolute)
            if parsed.scheme not in ("http", "https") or not parsed.netloc:
                continue
            if parsed.fragment:
                continue
            cleaned_redirects.append(absolute)

        return {
            "redirect_uris": sorted(set(cleaned_redirects)),
            "client_name": client_name.strip(),
            "logo_url": logo_url.strip(),
        }


def _get_or_fetch_client(client_id: str) -> IndieAuthClient | None:
    if not client_id:
        return None
    parsed = urlparse(client_id)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None
    if parsed.fragment:
        return None

    client, _created = IndieAuthClient.objects.get_or_create(client_id=client_id)
    refresh_needed = not client.last_fetched_at or timezone.now() - client.last_fetched_at > CLIENT_CACHE_TTL

    if client.redirect_uris and client.name and not refresh_needed:
        return client
    if client.redirect_uris and client.name and not client.last_fetched_at:
        client.last_fetched_at = timezone.now()
        client.save(update_fields=["last_fetched_at"])
        return client

    if refresh_needed or not client.redirect_uris:
        try:
            metadata = _fetch_client_metadata(client_id)
            client.redirect_uris = metadata.get("redirect_uris", [])
            client.name = metadata.get("client_name") or client.name
            client.logo_url = metadata.get("logo_url") or client.logo_url
            client.fetch_error = ""
        except (HTTPError, URLError, TimeoutError, ValueError, http.client.HTTPException) as exc:
            client.fetch_error = str(exc)
        client.last_fetched_at = timezone.now()
        client.save()

    return client


def _redirect_uri_allowed(client_id: str, redirect_uri: str, client: IndieAuthClient | None) -> bool:
    if not redirect_uri:
        return False
    parsed = urlparse(redirect_uri)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return False
    if parsed.fragment:
        return False

    if client and client.redirect_uris:
        return redirect_uri in client.redirect_uris

    client_parsed = urlparse(client_id)
    if client_parsed.scheme != parsed.scheme or client_parsed.netloc != parsed.netloc:
        return False
    if client_parsed.path:
        # For directory-style client_ids (ending in /), require the redirect to
        # be under that directory.  For file-style client_ids (e.g. /client.json),
        # use the parent directory as the prefix — otherwise the path-prefix check
        # would never pass for any redirect that doesn't literally start with the
        # filename (e.g. /client.json/...).
        if client_parsed.path.endswith("/"):
            prefix = client_parsed.path
        else:
            parent = client_parsed.path.rsplit("/", 1)[0]
            prefix = (parent + "/") if parent else "/"
        if prefix != "/" and not parsed.path.startswith(prefix):
            return False
    return True


def _build_metadata_payload(request) -> dict:
    issuer = _issuer(request)
    return {
        "issuer": issuer,
        "authorization_endpoint": request.build_absolute_uri(reverse("indieauth-authorize")),
        "token_endpoint": request.build_absolute_uri(reverse("indieauth-token")),
        "introspection_endpoint": request.build_absolute_uri(reverse("indieauth-introspect")),
        "revocation_endpoint": request.build_absolute_uri(reverse("indieauth-token")),
        "userinfo_endpoint": request.build_absolute_uri(reverse("indieauth-userinfo")),
        "code_challenge_methods_supported": ["S256"],
        "scopes_supported": ["create", "draft", "update", "delete", "undelete", "read", "media", "channels", "follow", "mute", "block"],
    }


def metadata(request):
    return JsonResponse(_build_metadata_payload(request))


@csrf_exempt
def authorize(request):
    if request.method == "POST":
        return _authorize_post(request)
    return _authorize_get(request)


def _authorize_get(request):
    params = request.GET
    client_id = params.get("client_id", "")
    redirect_uri = params.get("redirect_uri", "")
    me = params.get("me", "")
    scope = params.get("scope", "")
    state = params.get("state", "")
    response_type = params.get("response_type", "")
    code_challenge = params.get("code_challenge", "")
    code_challenge_method = params.get("code_challenge_method", "")
    prompt = params.get("prompt", "")

    if response_type != "code":
        return _render_error(request, "Unsupported response_type", status=400)

    client = _get_or_fetch_client(client_id)
    if not client:
        return _render_error(request, "Invalid client_id", status=400)

    if not _redirect_uri_allowed(client_id, redirect_uri, client):
        return _render_error(request, "Invalid redirect_uri", status=400)

    normalized_me = _normalize_url(me)
    if not normalized_me or not _is_allowed_me(request, normalized_me):
        return _render_error(request, "Invalid or unauthorized me URL", status=400)

    if not code_challenge or code_challenge_method != "S256":
        return _render_error(request, "PKCE code challenge required", status=400)

    if not request.user.is_authenticated:
        login_url = reverse("site_admin:login")
        return redirect(f"{login_url}?{urlencode({'next': request.get_full_path()})}")

    scopes = _normalize_scopes(scope)
    scope_value = " ".join(scopes)

    if prompt != "consent":
        consent = IndieAuthConsent.objects.filter(
            user=request.user,
            client_id=client_id,
            scope=scope_value,
        ).first()
        if consent:
            consent.last_used_at = timezone.now()
            consent.save(update_fields=["last_used_at"])
            return _issue_authorization_code(
                request,
                user=request.user,
                client_id=client_id,
                redirect_uri=redirect_uri,
                me=normalized_me,
                scope=scope_value,
                state=state,
                code_challenge=code_challenge,
                code_challenge_method=code_challenge_method,
            )

    context = {
        "client": client,
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "me": normalized_me,
        "scope": scope_value,
        "scopes": scopes,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": code_challenge_method,
    }
    return render(request, "indieauth/authorize.html", context)


def _verify_code_at_authorization_endpoint(request):
    """Handle server-to-server code verification POSTs at the authorization endpoint.

    Per IndieAuth spec §5.3, clients that only need identity (no access token)
    exchange the authorization code here and receive {"me": "..."} in return.
    """
    code = request.POST.get("code", "")
    client_id = request.POST.get("client_id", "")
    redirect_uri = request.POST.get("redirect_uri", "")
    code_verifier = request.POST.get("code_verifier", "")

    if not code or not client_id or not redirect_uri or not code_verifier:
        response = JsonResponse({"error": "invalid_request"}, status=400)
        _log_indieauth_error(request, response)
        return response

    with transaction.atomic():
        code_hash = _hash_token(code)
        auth_code = (
            IndieAuthAuthorizationCode.objects.select_for_update()
            .filter(code_hash=code_hash, used_at__isnull=True)
            .first()
        )
        if not auth_code:
            response = JsonResponse({"error": "invalid_grant"}, status=400)
            _log_indieauth_error(request, response)
            return response
        if auth_code.expires_at <= timezone.now():
            response = JsonResponse({"error": "invalid_grant"}, status=400)
            _log_indieauth_error(request, response)
            return response
        if auth_code.client_id != client_id or auth_code.redirect_uri != redirect_uri:
            response = JsonResponse({"error": "invalid_grant"}, status=400)
            _log_indieauth_error(request, response)
            return response
        if auth_code.code_challenge_method != "S256":
            response = JsonResponse({"error": "invalid_grant"}, status=400)
            _log_indieauth_error(request, response)
            return response

        computed = _base64url_sha256(code_verifier)
        if computed != auth_code.code_challenge:
            response = JsonResponse({"error": "invalid_grant"}, status=400)
            _log_indieauth_error(request, response)
            return response

        auth_code.used_at = timezone.now()
        auth_code.save(update_fields=["used_at"])

    return JsonResponse({"me": auth_code.me})


def _authorize_post(request):
    # Server-to-server code verification (IndieAuth spec §5.3).
    # Clients that only need identity (no token) POST the code here instead of
    # the token endpoint.  These requests come without a browser session or CSRF
    # token, so they must be handled before the user-auth check.
    code = request.POST.get("code", "")
    code_verifier = request.POST.get("code_verifier", "")
    if code and code_verifier:
        return _verify_code_at_authorization_endpoint(request)

    if not request.user.is_authenticated:
        login_url = reverse("site_admin:login")
        return redirect(f"{login_url}?{urlencode({'next': request.get_full_path()})}")

    client_id = request.POST.get("client_id", "")
    redirect_uri = request.POST.get("redirect_uri", "")
    me = request.POST.get("me", "")
    scope = request.POST.get("scope", "")
    state = request.POST.get("state", "")
    code_challenge = request.POST.get("code_challenge", "")
    code_challenge_method = request.POST.get("code_challenge_method", "")
    decision = request.POST.get("decision", "")
    remember = request.POST.get("remember") == "1"

    client = _get_or_fetch_client(client_id)
    if not client or not _redirect_uri_allowed(client_id, redirect_uri, client):
        return _render_error(request, "Invalid client_id or redirect_uri", status=400)

    normalized_me = _normalize_url(me)
    if not normalized_me or not _is_allowed_me(request, normalized_me):
        return _render_error(request, "Invalid or unauthorized me URL", status=400)

    if not code_challenge or code_challenge_method != "S256":
        return _render_error(request, "PKCE code challenge required", status=400)

    if decision != "approve":
        return redirect(
            _redirect_with_params(
                redirect_uri,
                {"error": "access_denied", "state": state},
            )
        )

    scopes = _normalize_scopes(scope)
    scope_value = " ".join(scopes)

    if remember:
        IndieAuthConsent.objects.update_or_create(
            user=request.user,
            client_id=client_id,
            scope=scope_value,
            defaults={"last_used_at": timezone.now()},
        )

    return _issue_authorization_code(
        request,
        user=request.user,
        client_id=client_id,
        redirect_uri=redirect_uri,
        me=normalized_me,
        scope=scope_value,
        state=state,
        code_challenge=code_challenge,
        code_challenge_method=code_challenge_method,
    )


def _issue_authorization_code(
    request,
    *,
    user,
    client_id: str,
    redirect_uri: str,
    me: str,
    scope: str,
    state: str,
    code_challenge: str,
    code_challenge_method: str,
):
    code = secrets.token_urlsafe(32)
    code_hash = _hash_token(code)
    IndieAuthAuthorizationCode.objects.create(
        code_hash=code_hash,
        code_challenge=code_challenge,
        code_challenge_method=code_challenge_method,
        client_id=client_id,
        redirect_uri=redirect_uri,
        me=me,
        scope=scope,
        user=user,
        expires_at=timezone.now() + AUTH_CODE_TTL,
    )
    params = {"code": code, "state": state, "iss": _issuer(request)}
    return redirect(_redirect_with_params(redirect_uri, params))


@csrf_exempt
def token(request):
    if request.method == "GET":
        token_value = request.GET.get("token", "")
        if not token_value:
            auth_header = request.META.get("HTTP_AUTHORIZATION", "")
            if auth_header.startswith("Bearer "):
                token_value = auth_header[7:].strip()
        if token_value:
            return _verify_access_token(request, token_value)
    if request.method != "POST":
        response = HttpResponseBadRequest("Invalid request")
        _log_indieauth_error(request, response)
        return response

    action = request.POST.get("action", "")
    if action == "revoke":
        token_value = request.POST.get("token") or request.POST.get("access_token")
        if not token_value:
            auth_header = request.META.get("HTTP_AUTHORIZATION", "")
            if auth_header.startswith("Bearer "):
                token_value = auth_header[7:].strip()
        if not token_value:
            return JsonResponse({"revoked": False})
        token_hash = _hash_token(token_value)
        IndieAuthAccessToken.objects.filter(token_hash=token_hash, revoked_at__isnull=True).update(
            revoked_at=timezone.now()
        )
        return JsonResponse({"revoked": True})
    if action == "verify":
        token_value = request.POST.get("token") or request.POST.get("access_token") or ""
        return _verify_access_token(request, token_value)

    token_value = request.POST.get("token") or request.POST.get("access_token")
    if not token_value:
        auth_header = request.META.get("HTTP_AUTHORIZATION", "")
        if auth_header.startswith("Bearer "):
            token_value = auth_header[7:].strip()
    if token_value:
        return _verify_access_token(request, token_value or "")

    grant_type = request.POST.get("grant_type", "")
    if grant_type and grant_type != "authorization_code":
        response = JsonResponse({"error": "unsupported_grant_type"}, status=400)
        _log_indieauth_error(request, response)
        return response

    code = request.POST.get("code", "")
    client_id = request.POST.get("client_id", "")
    redirect_uri = request.POST.get("redirect_uri", "")
    code_verifier = request.POST.get("code_verifier", "")

    if not code or not client_id or not redirect_uri or not code_verifier:
        response = JsonResponse({"error": "invalid_request"}, status=400)
        _log_indieauth_error(request, response)
        return response

    with transaction.atomic():
        code_hash = _hash_token(code)
        auth_code = (
            IndieAuthAuthorizationCode.objects.select_for_update()
            .filter(code_hash=code_hash, used_at__isnull=True)
            .first()
        )
        if not auth_code:
            response = JsonResponse({"error": "invalid_grant"}, status=400)
            _log_indieauth_error(request, response)
            return response
        if auth_code.expires_at <= timezone.now():
            response = JsonResponse({"error": "invalid_grant"}, status=400)
            _log_indieauth_error(request, response)
            return response
        if auth_code.client_id != client_id or auth_code.redirect_uri != redirect_uri:
            response = JsonResponse({"error": "invalid_grant"}, status=400)
            _log_indieauth_error(request, response)
            return response
        if auth_code.code_challenge_method != "S256":
            response = JsonResponse({"error": "invalid_grant"}, status=400)
            _log_indieauth_error(request, response)
            return response

        computed = _base64url_sha256(code_verifier)
        if computed != auth_code.code_challenge:
            response = JsonResponse({"error": "invalid_grant"}, status=400)
            _log_indieauth_error(request, response)
            return response

        auth_code.used_at = timezone.now()
        auth_code.save(update_fields=["used_at"])

        access_token = secrets.token_urlsafe(32)
        token_hash = _hash_token(access_token)
        expires_at = timezone.now() + ACCESS_TOKEN_TTL if ACCESS_TOKEN_TTL else None
        IndieAuthAccessToken.objects.create(
            token_hash=token_hash,
            client_id=auth_code.client_id,
            me=auth_code.me,
            scope=auth_code.scope,
            user=auth_code.user,
            expires_at=expires_at,
        )

    payload = {
        "access_token": access_token,
        "token_type": "Bearer",
        "me": auth_code.me,
        "scope": auth_code.scope,
    }
    if expires_at:
        payload["expires_in"] = int((expires_at - timezone.now()).total_seconds())
    return JsonResponse(payload)


@csrf_exempt
def introspect(request):
    token_value = ""
    if request.method == "POST":
        token_value = request.POST.get("token", "")
    if request.method == "GET":
        token_value = token_value or request.GET.get("token", "")

    auth_header = request.META.get("HTTP_AUTHORIZATION", "")
    if not token_value and auth_header.startswith("Bearer "):
        token_value = auth_header[7:].strip()

    if not token_value:
        response = JsonResponse({"active": False}, status=400)
        _log_indieauth_error(request, response)
        return response

    token_hash = _hash_token(token_value)
    token = IndieAuthAccessToken.objects.filter(token_hash=token_hash).first()
    if not token:
        return JsonResponse({"active": False})

    if token.revoked_at:
        return JsonResponse({"active": False})

    if token.expires_at and token.expires_at <= timezone.now():
        return JsonResponse({"active": False})

    payload = {
        "active": True,
        "scope": token.scope,
        "client_id": token.client_id,
        "me": token.me,
        "token_type": "Bearer",
        "iat": int(token.created_at.timestamp()),
    }
    if token.expires_at:
        payload["exp"] = int(token.expires_at.timestamp())
    return JsonResponse(payload)


@csrf_exempt
def userinfo(request):
    auth_header = request.META.get("HTTP_AUTHORIZATION", "")
    if not auth_header.startswith("Bearer "):
        response = JsonResponse({"error": "unauthorized"}, status=401)
        _log_indieauth_error(request, response)
        return response
    token_value = auth_header[7:].strip()
    token_hash = _hash_token(token_value)
    token = IndieAuthAccessToken.objects.filter(token_hash=token_hash).first()
    if not token or token.revoked_at:
        response = JsonResponse({"error": "unauthorized"}, status=401)
        _log_indieauth_error(request, response)
        return response
    if token.expires_at and token.expires_at <= timezone.now():
        response = JsonResponse({"error": "unauthorized"}, status=401)
        _log_indieauth_error(request, response)
        return response

    user = token.user
    user_data = {
        "me": token.me,
        "name": user.get_full_name() or user.get_username() or "",
    }
    email = getattr(user, "email", "")
    if email:
        user_data["email"] = email
    return JsonResponse(user_data)


def _verify_access_token(request, token_value: str):
    if not token_value:
        response = JsonResponse({"active": False}, status=400)
        _log_indieauth_error(request, response)
        return response

    token_hash = _hash_token(token_value)
    token = IndieAuthAccessToken.objects.filter(token_hash=token_hash).first()
    if not token or token.revoked_at:
        return JsonResponse({"active": False})

    if token.expires_at and token.expires_at <= timezone.now():
        return JsonResponse({"active": False})

    payload = {
        "active": True,
        "scope": token.scope,
        "client_id": token.client_id,
        "me": token.me,
        "token_type": "Bearer",
        "iat": int(token.created_at.timestamp()),
    }
    if token.expires_at:
        payload["exp"] = int(token.expires_at.timestamp())
    return JsonResponse(payload)


def _render_error(request, message: str, status: int = 400):
    response = render(request, "indieauth/error.html", {"message": message}, status=status)
    _log_indieauth_error(request, response)
    return response
