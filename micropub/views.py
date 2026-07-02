import hashlib
import json
import logging
import mimetypes
import os
import re
from datetime import datetime
from html.parser import HTMLParser
from typing import Optional
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urljoin, urlparse
from urllib.request import Request, urlopen
from uuid import uuid4

from django.core.cache import cache
from django.http import (
    HttpResponse,
    HttpResponseBadRequest,
    JsonResponse,
)
from django.utils import timezone
from django.utils.text import slugify
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt
from django.urls import reverse, resolve
from django.core.files.base import ContentFile
from django.conf import settings
from django.shortcuts import redirect, render
from django.db import IntegrityError, transaction

from markdownify import markdownify as html_to_markdown

from blog.models import Post, Tag
from core.models import Page, SiteConfiguration, RequestErrorLog
from core.request_logs import extract_response_error, log_request_error
from files.models import Attachment, File
from .models import Webmention
from .webmention import BRIDGY_PUBLISH_TARGETS, queue_webmentions_for_post

DEFAULT_TOKEN_ENDPOINT = "https://tokens.indieauth.com/token"
logger = logging.getLogger(__name__)


def _first_value(data: dict, key: str, default=None):
    value = data.get(key, [])
    if isinstance(value, list):
        return value[0] if value else default
    return value or default


_GEO_URI_RE = re.compile(
    r"^geo:(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)(?:,(-?\d+(?:\.\d+)?))?(?:;.*)?$"
)


def _parse_geo_uri(uri: str) -> dict | None:
    if not uri:
        return None
    match = _GEO_URI_RE.match(uri.strip())
    if not match:
        return None
    result = {
        "latitude": float(match.group(1)),
        "longitude": float(match.group(2)),
    }
    if match.group(3) is not None:
        result["altitude"] = float(match.group(3))
    return result


class _IndieAuthEndpointParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.authorization_endpoint = None
        self.token_endpoint = None

    def handle_starttag(self, tag, attrs):
        if tag.lower() not in ("a", "link"):
            return
        attr_map = {key.lower(): value for key, value in attrs}
        rel_value = attr_map.get("rel", "")
        href = attr_map.get("href")
        if not rel_value or not href:
            return
        rels = {rel.strip() for rel in rel_value.split() if rel.strip()}
        if "authorization_endpoint" in rels and not self.authorization_endpoint:
            self.authorization_endpoint = href
        if "token_endpoint" in rels and not self.token_endpoint:
            self.token_endpoint = href


def _parse_link_header_for_rel(header_value: str, rel_name: str) -> Optional[str]:
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
            return url[1:]
    return None


def _normalize_me_url(me_value: str) -> Optional[str]:
    if not me_value:
        return None
    me_value = me_value.strip()
    parsed = urlparse(me_value)
    if not parsed.scheme:
        parsed = urlparse(f"https://{me_value}")
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None
    path = parsed.path or "/"
    if me_value.endswith("/") and not path.endswith("/"):
        path = f"{path}/"
    return parsed._replace(path=path, fragment="").geturl()


def _discover_indieauth_endpoints(me_url: str) -> tuple[Optional[str], Optional[str]]:
    request = Request(me_url, headers={"User-Agent": "django-blog-indieauth"})
    try:
        with urlopen(request, timeout=10) as response:
            link_header = response.headers.get("Link")
            auth_endpoint = None
            token_endpoint = None
            if link_header:
                auth_endpoint = _parse_link_header_for_rel(link_header, "authorization_endpoint")
                token_endpoint = _parse_link_header_for_rel(link_header, "token_endpoint")

            content_type = response.headers.get("Content-Type", "")
            if "html" in content_type:
                parser = _IndieAuthEndpointParser()
                parser.feed(response.read().decode("utf-8", errors="ignore"))
                if not auth_endpoint:
                    auth_endpoint = parser.authorization_endpoint
                if not token_endpoint:
                    token_endpoint = parser.token_endpoint

            if auth_endpoint:
                auth_endpoint = urljoin(me_url, auth_endpoint)
            if token_endpoint:
                token_endpoint = urljoin(me_url, token_endpoint)

            return auth_endpoint, token_endpoint
    except (HTTPError, URLError, TimeoutError) as exc:
        logger.info(
            "IndieAuth discovery failed",
            extra={"indieauth_me": me_url, "indieauth_error": str(exc)},
        )
        return None, None


def _parse_scope(scope_value):
    if isinstance(scope_value, list):
        scope_value = _first_value({"scope": scope_value}, "scope", "")
    if isinstance(scope_value, str):
        return [s for s in scope_value.split() if s]
    return []


def _token_from_post(request) -> str | None:
    token = request.POST.get("access_token")
    if not token:
        token = request.POST.get("access_token[]")
    if not token:
        tokens = request.POST.getlist("access_token") or request.POST.getlist("access_token[]")
        if tokens:
            token = tokens[0]
    if isinstance(token, str) and token:
        return token
    return None


def _token_from_json(raw) -> str | None:
    if not isinstance(raw, dict):
        return None
    token_value = raw.get("access_token")
    if isinstance(token_value, list):
        token_value = _first_value({"access_token": token_value}, "access_token")
    if isinstance(token_value, str) and token_value:
        return token_value
    return None


def _has_token_conflict(request):
    auth_header = request.META.get("HTTP_AUTHORIZATION", "")
    header_token = None
    if auth_header.startswith("Bearer "):
        header_token = auth_header[7:].strip()
    body_token = None

    if request.content_type and "json" in request.content_type:
        try:
            raw = json.loads(request.body or "{}")
            body_token = _token_from_json(raw)
        except json.JSONDecodeError:
            body_token = None
    else:
        body_token = _token_from_post(request)

    query_token = request.GET.get("access_token")

    tokens = [token for token in (header_token, body_token, query_token) if token]
    return len(set(tokens)) > 1


MICROPUB_REDACT_FIELDS = {"access_token", "refresh_token", "client_secret"}


def _log_micropub_error(request, response):
    if response.status_code < 400:
        return
    try:
        error, response_body = extract_response_error(response)
        if response.status_code == 401 and error == "unauthorized":
            auth_error = getattr(request, "micropub_auth_error", "")
            if auth_error:
                error = f"unauthorized:{auth_error}"
        log_request_error(
            RequestErrorLog.SOURCE_MICROPUB,
            request,
            response,
            error=error,
            response_body=response_body,
            redact_fields=MICROPUB_REDACT_FIELDS,
        )
    except Exception:
        logger.exception(
            "Micropub error log failed",
            extra={"micropub_path": request.path, "micropub_status": response.status_code},
        )


def _normalize_property(key: str, values):
    normalized_key = key[:-2] if key.endswith("[]") else key
    normalized_values = []
    for item in values:
        if normalized_key == "content" and isinstance(item, dict):
            html_content = item.get("html")
            if isinstance(html_content, str):
                item = html_to_markdown(html_content)
            elif isinstance(item.get("value"), str):
                item = item["value"]
        elif normalized_key == "photo" and isinstance(item, dict):
            url = item.get("value")
            if not url:
                url_candidate = item.get("url")
                if isinstance(url_candidate, list) and url_candidate:
                    url = url_candidate[0]
                elif isinstance(url_candidate, str):
                    url = url_candidate
            alt = item.get("alt")
            if isinstance(alt, list) and alt:
                alt = alt[0]
            if isinstance(url, str) and url:
                alt_text = alt if isinstance(alt, str) else ""
                item = {"url": url, "alt": alt_text}
        normalized_values.append(item)
    return normalized_key, normalized_values


def _normalize_payload(request):
    if request.content_type and "json" in request.content_type:
        try:
            raw = json.loads(request.body or "{}")
        except json.JSONDecodeError:
            return {}
        raw_data = {}
        if isinstance(raw, dict) and isinstance(raw.get("properties"), dict):
            properties = raw["properties"]
            raw_data.update({key: value if isinstance(value, list) else [value] for key, value in properties.items()})
            for key in ("action", "url", "replace", "add", "delete", "type"):
                if key in raw and key not in raw_data:
                    value = raw[key]
                    raw_data[key] = value if isinstance(value, list) else [value]
        else:
            raw_data = {key: value if isinstance(value, list) else [value] for key, value in raw.items()}
    else:
        raw_data = {key: request.POST.getlist(key) for key in request.POST}

    normalized = {}
    for key, value in raw_data.items():
        normalized_key, normalized_values = _normalize_property(key, value)
        normalized.setdefault(normalized_key, []).extend(normalized_values)

    return normalized


def _allowed_webmention_hosts(request):
    allowed_hosts = list(settings.ALLOWED_HOSTS or [])
    if not allowed_hosts:
        allowed_hosts = [request.get_host()]
    return allowed_hosts


def _safe_next_url(request, next_url: str) -> str:
    if next_url and url_has_allowed_host_and_scheme(
        next_url,
        allowed_hosts=_allowed_webmention_hosts(request),
        require_https=False,
    ):
        return next_url
    return "/"


def _source_matches_indieauth_me(indieauth_me: str, source_url: str) -> bool:
    normalized_me = _normalize_me_url(indieauth_me or "")
    if not normalized_me:
        return False

    parsed_me = urlparse(normalized_me)
    parsed_source = urlparse(source_url or "")
    if not parsed_source.scheme:
        parsed_source = urlparse(f"https://{source_url}")
    if parsed_source.scheme not in ("http", "https"):
        return False

    me_host = (parsed_me.hostname or "").lower()
    source_host = (parsed_source.hostname or "").lower()
    if not me_host or not source_host:
        return False
    return source_host == me_host or source_host.endswith(f".{me_host}")


def _start_indieauth_login(request, me_value: str, next_url: str):
    normalized_me = _normalize_me_url(me_value)
    if not normalized_me:
        logger.info("IndieAuth start rejected invalid me", extra={"indieauth_me": me_value})
        return redirect(_safe_next_url(request, next_url))

    auth_endpoint, token_endpoint = _discover_indieauth_endpoints(normalized_me)
    if not auth_endpoint:
        logger.info("IndieAuth start missing authorization endpoint", extra={"indieauth_me": normalized_me})
        return redirect(_safe_next_url(request, next_url))

    state = uuid4().hex
    request.session["indieauth_state"] = state
    request.session["indieauth_pending_me"] = normalized_me
    request.session["indieauth_next"] = _safe_next_url(request, next_url)
    if token_endpoint:
        request.session["indieauth_token_endpoint"] = token_endpoint
    else:
        request.session["indieauth_token_endpoint"] = DEFAULT_TOKEN_ENDPOINT

    params = {
        "me": normalized_me,
        "client_id": request.build_absolute_uri("/"),
        "redirect_uri": request.build_absolute_uri(reverse("indieauth-callback")),
        "state": state,
        "response_type": "code",
    }
    target = f"{auth_endpoint}?{urlencode(params)}"
    return redirect(target)


def _target_is_valid(request, target_url: str) -> tuple[Optional[Post], Optional[HttpResponse]]:
    if not url_has_allowed_host_and_scheme(
        target_url,
        allowed_hosts=_allowed_webmention_hosts(request),
        require_https=False,
    ):
        logger.info(
            "Webmention target host rejected",
            extra={"webmention_target": target_url},
        )
        return None, HttpResponseBadRequest("Target host is not allowed")

    parsed = urlparse(target_url)
    if parsed.scheme not in ("http", "https"):
        logger.info(
            "Webmention target scheme rejected",
            extra={"webmention_target": target_url},
        )
        return None, HttpResponseBadRequest("Target scheme is not allowed")

    try:
        match = resolve(parsed.path)
    except Exception:
        logger.info(
            "Webmention target path rejected",
            extra={"webmention_target": target_url, "webmention_path": parsed.path},
        )
        return None, HttpResponseBadRequest("Target path is not recognized")

    if match.url_name == "post":
        slug = match.kwargs.get("slug")
        if not slug:
            return None, HttpResponseBadRequest("Target slug is missing")
        try:
            return Post.objects.get(slug=slug), None
        except Post.DoesNotExist:
            return None, HttpResponseBadRequest("Target post not found")

    if match.url_name == "page":
        slug = match.kwargs.get("slug")
        if not slug:
            return None, HttpResponseBadRequest("Target slug is missing")
        try:
            Page.objects.get(slug=slug)
            return None, None
        except Page.DoesNotExist:
            return None, HttpResponseBadRequest("Target page not found")

    logger.info(
        "Webmention target view rejected",
        extra={"webmention_target": target_url, "webmention_view": match.url_name},
    )
    return None, HttpResponseBadRequest("Target path is not recognized")


def _is_trusted_domain(source_url: str) -> bool:
    trusted = [domain.lower() for domain in settings.WEBMENTION_TRUSTED_DOMAINS]
    if not trusted:
        return False
    source_host = urlparse(source_url).hostname
    if not source_host:
        return False
    source_host = source_host.lower()
    for domain in trusted:
        if source_host == domain or source_host.endswith(f".{domain}"):
            return True
    return False


def _is_mf2_object(value):
    return isinstance(value, dict) and value.get("type") and value.get("properties")


def _extract_mf2_objects(data: dict):
    mf2_objects = {}
    for key, values in data.items():
        nested_objects = [value for value in values if _is_mf2_object(value)]
        if nested_objects:
            mf2_objects[key] = nested_objects
    return mf2_objects


def _require_scope(request, needed):
    scopes = getattr(request, "micropub_scopes", [])
    if needed and needed not in scopes:
        return JsonResponse({"error": "insufficient_scope"}, status=403)
    return None


def _syndication_targets(settings_obj) -> list[dict]:
    if not settings_obj:
        return []
    targets = []
    for field_name, target_url in BRIDGY_PUBLISH_TARGETS:
        if not getattr(settings_obj, field_name, False):
            continue
        name = field_name
        try:
            name = settings_obj._meta.get_field(field_name).verbose_name
        except Exception:
            pass
        targets.append({"uid": target_url, "name": str(name)})
    return targets


def _slug_from_url(target_url, error_prefix):
    if not target_url:
        return None, HttpResponseBadRequest(f"Missing url for {error_prefix}")

    parsed = urlparse(target_url)
    path_parts = [part for part in parsed.path.split("/") if part]
    slug = path_parts[-1] if path_parts else ""
    if not slug:
        return None, HttpResponseBadRequest(f"Invalid url for {error_prefix}")
    return slug, None


def _get_post_for_action(slug, *, allow_deleted=True, not_found_status=404, not_found_message=None):
    try:
        qs = Post.objects.all()
        if not allow_deleted:
            qs = qs.filter(deleted=False)
        post = qs.get(slug=slug)
        return post, None
    except Post.DoesNotExist:
        if not_found_message:
            return None, HttpResponseBadRequest(not_found_message)
        return None, HttpResponse(status=not_found_status)


def _normalize_update_ops(raw_ops, error_message="Invalid payload"):
    """Normalize replace/add/delete dicts into Micropub-ready values."""
    if raw_ops and not isinstance(raw_ops, dict):
        return None, HttpResponseBadRequest(error_message)

    normalized = {}
    for key, value in (raw_ops or {}).items():
        value_list = value if isinstance(value, list) else [value]
        n_key, n_values = _normalize_property(key, value_list)
        normalized[n_key] = n_values
    return normalized, None


def _normalize_delete_ops(raw_delete):
    if raw_delete and not isinstance(raw_delete, (dict, list, str)):
        return None, HttpResponseBadRequest("Invalid delete payload")

    if isinstance(raw_delete, dict):
        return _normalize_update_ops(raw_delete, error_message="Invalid delete payload")

    if isinstance(raw_delete, (list, str)):
        props = raw_delete if isinstance(raw_delete, list) else [raw_delete]
        return {prop: [] for prop in props}, None

    return {}, None


def _build_properties_response(post, requested_props=None):
    props = {
        "content": [post.content] if post.content else [],
        "name": [post.title] if post.title else [],
        "published": [post.published_on.isoformat()] if post.published_on else [],
        "category": list(post.tags.values_list("tag", flat=True)),
    }
    if post.like_of:
        props["like-of"] = [post.like_of]
    if post.repost_of:
        props["repost-of"] = [post.repost_of]
    if post.in_reply_to:
        props["in-reply-to"] = [post.in_reply_to]

    mf2 = post.mf2 if isinstance(post.mf2, dict) else {}
    checkin = mf2.get("checkin")
    if isinstance(checkin, dict) and "latitude" in checkin and "longitude" in checkin:
        props["location"] = [f"geo:{checkin['latitude']},{checkin['longitude']}"]

    photos = []
    for attachment in post.attachments.filter(asset__kind=File.IMAGE):
        url = attachment.asset.file.url
        alt = attachment.asset.alt_text
        if alt:
            photos.append({"value": url, "alt": alt})
        else:
            photos.append(url)
    if photos:
        props["photo"] = photos

    if requested_props:
        props = {k: v for k, v in props.items() if k in requested_props}
    return props


def _handle_delete_action(request, data):
    insufficient = _require_scope(request, "delete")
    if insufficient:
        return insufficient

    target_url = _first_value(data, "url")
    slug, error = _slug_from_url(target_url, "delete")
    if error:
        return error

    post, error = _get_post_for_action(slug)
    if error:
        return error

    post.deleted = True
    post.save(update_fields=["deleted"])
    return HttpResponse(status=204)


def _apply_categories(post, categories, *, clear_first=False):
    if clear_first:
        post.tags.clear()
    for category in categories:
        tag_slug = slugify(str(category))
        if not tag_slug:
            continue
        tag, _ = Tag.objects.get_or_create(tag=tag_slug)
        post.tags.add(tag)


def _handle_update_action(request, data):
    insufficient = _require_scope(request, "update")
    if insufficient:
        return insufficient

    target_url = _first_value(data, "url")
    slug, error = _slug_from_url(target_url, "update")
    if error:
        return error

    post, error = _get_post_for_action(slug, not_found_status=400, not_found_message="Post not found for update")
    if error:
        return error

    replace_data = _first_value(data, "replace", {}) or {}
    normalized_replace, error = _normalize_update_ops(replace_data, error_message="Invalid replace payload")
    if error:
        return error

    add_data = _first_value(data, "add", {}) or {}
    normalized_add, error = _normalize_update_ops(add_data, error_message="Invalid add payload")
    if error:
        return error

    delete_data = _first_value(data, "delete", {}) or {}
    normalized_delete, error = _normalize_delete_ops(delete_data)
    if error:
        return error

    if "content" in normalized_replace:
        new_content = _first_value({"content": normalized_replace["content"]}, "content")
        if new_content is not None:
            post.content = new_content

    if "category" in normalized_replace:
        _apply_categories(post, normalized_replace["category"], clear_first=True)

    if "category" in normalized_add:
        _apply_categories(post, normalized_add["category"])

    if "category" in normalized_delete:
        for category in normalized_delete["category"]:
            tag_slug = slugify(str(category))
            if not tag_slug:
                continue
            post.tags.filter(tag=tag_slug).delete()
        if normalized_delete["category"] == []:
            post.tags.clear()

    post.save()
    source_url = request.build_absolute_uri(post.get_absolute_url())
    settings_obj = SiteConfiguration.get_solo()
    transaction.on_commit(
        lambda: queue_webmentions_for_post(
            post,
            source_url,
            include_bridgy=True,
            settings_obj=settings_obj,
        )
    )
    return HttpResponse(status=204)


def _handle_undelete_action(request, data):
    insufficient = _require_scope(request, "undelete")
    if insufficient:
        return insufficient

    target_url = _first_value(data, "url")
    slug, error = _slug_from_url(target_url, "undelete")
    if error:
        return error

    post, error = _get_post_for_action(slug)
    if error:
        return error

    if post.deleted:
        post.deleted = False
        post.save(update_fields=["deleted"])

    return HttpResponse(status=204)


def _parse_published_date(published):
    if not published:
        return timezone.now()
    try:
        parsed = datetime.fromisoformat(published)
        if timezone.is_naive(parsed):
            return timezone.make_aware(parsed)
        return parsed
    except ValueError:
        return timezone.now()


def _determine_kind(request, data, name, like_of, repost_of, in_reply_to, bookmark_of):
    if bookmark_of:
        return Post.BOOKMARK
    if like_of:
        return Post.LIKE
    if repost_of:
        return Post.REPOST
    if in_reply_to:
        return Post.REPLY
    location = _first_value(data, "location")
    if location and _parse_geo_uri(location):
        return Post.CHECKIN
    if request.FILES.getlist("photo") or request.FILES.getlist("photo[]") or data.get("photo"):
        return Post.PHOTO
    if name:
        return Post.ARTICLE
    return Post.NOTE


def _attach_uploaded_photos(request, post):
    uploaded_photos = request.FILES.getlist("photo") + request.FILES.getlist("photo[]")
    for uploaded in uploaded_photos:
        asset = File.objects.create(kind=File.IMAGE, file=uploaded)
        Attachment.objects.create(content_object=post, asset=asset, role="photo")


def _attach_remote_photos(data, post):
    from micropub.tasks import download_post_photo
    for photo_item in data.get("photo", []):
        if isinstance(photo_item, str) and photo_item and not photo_item.startswith("<UploadedFile"):
            if photo_item.startswith("!["):
                post.content += f"\n{photo_item}\n"
                continue
            transaction.on_commit(lambda u=photo_item: download_post_photo.delay(post.pk, u))
        elif isinstance(photo_item, dict):
            url = photo_item.get("url")
            alt_text = photo_item.get("alt") or ""
            if isinstance(url, str) and url:
                transaction.on_commit(lambda u=url, a=alt_text: download_post_photo.delay(post.pk, u, a))
    if data.get("photo"):
        post.save()


def _handle_create_action(request, data):
    insufficient = _require_scope(request, "create")
    if insufficient:
        return insufficient

    content = _first_value(data, "content", "") or ""
    name = _first_value(data, "name")
    like_of = _first_value(data, "like-of")
    repost_of = _first_value(data, "repost-of")
    in_reply_to = _first_value(data, "in-reply-to")
    bookmark_of = _first_value(data, "bookmark-of")
    location = _first_value(data, "location")
    categories = data.get("category", [])
    published = _first_value(data, "published")
    mf2_objects = _extract_mf2_objects(data)

    kind = _determine_kind(request, data, name, like_of, repost_of, in_reply_to, bookmark_of)

    if kind == Post.CHECKIN and location:
        geo = _parse_geo_uri(location)
        if geo:
            checkin = {"latitude": geo["latitude"], "longitude": geo["longitude"]}
            if name:
                checkin["name"] = name
            mf2_objects["checkin"] = checkin

    if not content:
        if kind == Post.LIKE:
            content = f"Liked {like_of}"
        elif kind == Post.REPOST:
            content = f"Reposted {repost_of}"
        elif kind == Post.REPLY:
            content = f"Reply to {in_reply_to}"
        elif kind == Post.BOOKMARK:
            content = f"Bookmarked {bookmark_of}"
        elif kind == Post.CHECKIN:
            content = "Checked in"

    published_on = _parse_published_date(published)

    post = Post(
        title=name or "",
        content=content,
        kind=kind,
        published_on=published_on,
        like_of=like_of or "",
        repost_of=repost_of or "",
        in_reply_to=in_reply_to or "",
        bookmark_of=bookmark_of or "",
        mf2=mf2_objects,
    )
    post.save()

    _apply_categories(post, categories)
    _attach_uploaded_photos(request, post)
    _attach_remote_photos(data, post)

    location = request.build_absolute_uri(post.get_absolute_url())
    settings_obj = SiteConfiguration.get_solo()
    transaction.on_commit(
        lambda: queue_webmentions_for_post(
            post,
            location,
            include_bridgy=True,
            settings_obj=settings_obj,
        )
    )

    response = HttpResponse(status=201)
    response["Location"] = location
    return response


def _download_and_attach_photo(post, url: str, alt_text: str = ""):
    try:
        with urlopen(url, timeout=10) as response:
            data = response.read()
            content_type = response.headers.get("Content-Type", "")
    except (HTTPError, URLError, TimeoutError, ValueError):
        return False

    if not data:
        return False

    parsed = urlparse(url)
    filename = os.path.basename(parsed.path)
    if not filename or "." not in filename:
        ext = mimetypes.guess_extension(content_type.split(";")[0].strip()) or ".jpg"
        filename = f"{uuid4().hex}{ext}"

    asset = File(kind=File.IMAGE, alt_text=alt_text or "")
    asset.file.save(filename, ContentFile(data), save=True)
    Attachment.objects.create(content_object=post, asset=asset, role="photo")
    return True


def _introspect_local(token: str) -> tuple[bool, list[str]] | None:
    try:
        from indieauth.models import IndieAuthAccessToken
    except Exception:
        return None

    try:
        from django.utils import timezone
        from indieauth.views import _hash_token
    except Exception:
        return None

    token_hash = _hash_token(token)
    token_obj = IndieAuthAccessToken.objects.filter(token_hash=token_hash).first()
    if not token_obj:
        return False, []
    if token_obj.revoked_at:
        return False, []
    if token_obj.expires_at and token_obj.expires_at <= timezone.now():
        return False, []
    scopes = _parse_scope(token_obj.scope)
    return True, scopes


def _authorized(request):
    request.micropub_auth_error = ""
    auth_header = request.META.get("HTTP_AUTHORIZATION", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:].strip()
    else:
        token = _token_from_post(request) or request.GET.get("access_token")

    if not token:
        request.micropub_auth_error = "missing_token"
        return False, []

    local = _introspect_local(token)
    if local is not None:
        authorized, scopes = local
        if not authorized:
            request.micropub_auth_error = "introspect_inactive"
        return authorized, scopes

    cache_key = "token_introspect:" + hashlib.sha256(token.encode()).hexdigest()
    cached = cache.get(cache_key)
    if cached is not None:
        authorized, scopes = cached
        if not authorized:
            request.micropub_auth_error = "introspect_inactive"
        return authorized, scopes

    introspect_url = request.build_absolute_uri(reverse("indieauth-introspect"))
    body = urlencode({"token": token}).encode("utf-8")
    verification_request = Request(
        introspect_url,
        data=body,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )

    try:
        with urlopen(verification_request, timeout=5) as response:
            body = response.read().decode()
            status_code = response.status
            content_type = response.headers.get("Content-Type", "")
    except (HTTPError, URLError, TimeoutError) as exc:
        request.micropub_auth_error = f"introspect_request_failed:{exc.__class__.__name__}"
        return False, []

    if status_code != 200:
        request.micropub_auth_error = f"introspect_status:{status_code}"
        return False, []

    scopes = []
    if body:
        try:
            token_data = json.loads(body) if "application/json" in content_type else parse_qs(body)
        except json.JSONDecodeError:
            request.micropub_auth_error = "introspect_parse_error"
            return False, []

        active = token_data.get("active")
        if isinstance(active, list):
            active = _first_value({"active": active}, "active")
        if active is False or (isinstance(active, str) and active.lower() == "false"):
            request.micropub_auth_error = "introspect_inactive"
            cache.set(cache_key, (False, []), timeout=30)
            return False, []

        error = token_data.get("error")
        if isinstance(error, list):
            error = _first_value({"error": error}, "error")
        if error:
            request.micropub_auth_error = f"introspect_error:{error}"
            cache.set(cache_key, (False, []), timeout=30)
            return False, []

        scopes = _parse_scope(token_data.get("scope", []))

    cache.set(cache_key, (True, scopes), timeout=300)
    return True, scopes


@method_decorator(csrf_exempt, name="dispatch")
class MicropubView(View):
    http_method_names = ["get", "post"]

    def dispatch(self, request, *args, **kwargs):
        if _has_token_conflict(request):
            response = JsonResponse({"error": "invalid_request"}, status=400)
            _log_micropub_error(request, response)
            return response
        authorized, scopes = _authorized(request)
        if not authorized:
            response = JsonResponse({"error": "unauthorized"}, status=401)
            _log_micropub_error(request, response)
            return response
        request.micropub_scopes = scopes
        response = super().dispatch(request, *args, **kwargs)
        _log_micropub_error(request, response)
        return response

    def get(self, request):
        query = request.GET.get("q")
        if query == "config":
            insufficient = _require_scope(request, None)
            if insufficient:
                return insufficient
            media_endpoint = request.build_absolute_uri(reverse("micropub-media"))
            settings_obj = SiteConfiguration.get_solo()
            return JsonResponse(
                {
                    "media-endpoint": media_endpoint,
                    "post-types": [
                        {"type": Post.ARTICLE, "name": "Article"},
                        {"type": Post.NOTE, "name": "Note"},
                        {"type": Post.PHOTO, "name": "Photo"},
                        {"type": Post.LIKE, "name": "Like"},
                        {"type": Post.REPOST, "name": "Repost"},
                        {"type": Post.REPLY, "name": "Reply"},
                        {"type": Post.BOOKMARK, "name": "Bookmark"},
                    ],
                    "syndicate-to": _syndication_targets(settings_obj),
                }
            )
        if query == "syndicate-to":
            insufficient = _require_scope(request, None)
            if insufficient:
                return insufficient
            settings_obj = SiteConfiguration.get_solo()
            return JsonResponse({"syndicate-to": _syndication_targets(settings_obj)})
        if query == "source":
            insufficient = _require_scope(request, "read")
            if insufficient:
                return insufficient
            target_url = request.GET.get("url")
            slug, error = _slug_from_url(target_url, "source query")
            if error:
                return error

            post, error = _get_post_for_action(slug, allow_deleted=False)
            if error:
                return error

            requested_props = request.GET.getlist("properties[]") or request.GET.getlist("properties")
            props = _build_properties_response(post, requested_props)
            return JsonResponse({"properties": props})
        return HttpResponseBadRequest("Unsupported query")

    def post(self, request):
        data = _normalize_payload(request)
        action = _first_value(data, "action")

        if action == "delete":
            return _handle_delete_action(request, data)

        if action == "update":
            return _handle_update_action(request, data)

        if action == "undelete":
            return _handle_undelete_action(request, data)

        return _handle_create_action(request, data)


@method_decorator(csrf_exempt, name="dispatch")
class MicropubMediaView(View):
    http_method_names = ["post"]

    def dispatch(self, request, *args, **kwargs):
        if _has_token_conflict(request):
            response = JsonResponse({"error": "invalid_request"}, status=400)
            _log_micropub_error(request, response)
            return response
        authorized, scopes = _authorized(request)
        if not authorized:
            response = JsonResponse({"error": "unauthorized"}, status=401)
            _log_micropub_error(request, response)
            return response
        request.micropub_scopes = scopes
        response = super().dispatch(request, *args, **kwargs)
        _log_micropub_error(request, response)
        return response

    def post(self, request):
        insufficient = _require_scope(request, "create")
        if insufficient:
            return insufficient

        upload = request.FILES.get("file") or request.FILES.get("photo")
        if not upload:
            return HttpResponseBadRequest("No file provided")

        asset = File.objects.create(kind=File.IMAGE, file=upload)
        response = HttpResponse(status=201)
        response["Location"] = asset.file.url
        return response


class IndieAuthLoginView(View):
    http_method_names = ["get", "post"]

    def get(self, request):
        me_value = request.GET.get("me", "").strip()
        next_url = request.GET.get("next", "").strip()
        if not me_value:
            return render(request, "micropub/indieauth_login.html", {"next": _safe_next_url(request, next_url)})
        return _start_indieauth_login(request, me_value, next_url)

    def post(self, request):
        me_value = request.POST.get("me", "").strip()
        next_url = request.POST.get("next", "").strip()
        if not me_value:
            return render(request, "micropub/indieauth_login.html", {"next": _safe_next_url(request, next_url)})
        return _start_indieauth_login(request, me_value, next_url)


class IndieAuthCallbackView(View):
    http_method_names = ["get"]

    def get(self, request):
        next_url = _safe_next_url(request, request.session.get("indieauth_next", "/"))
        expected_state = request.session.get("indieauth_state")
        pending_me = request.session.get("indieauth_pending_me")
        token_endpoint = request.session.get("indieauth_token_endpoint", DEFAULT_TOKEN_ENDPOINT)
        code = request.GET.get("code")
        state = request.GET.get("state")
        returned_me = request.GET.get("me")

        for key in ("indieauth_state", "indieauth_pending_me", "indieauth_next", "indieauth_token_endpoint"):
            request.session.pop(key, None)

        if not code or not state or not expected_state or state != expected_state:
            logger.info(
                "IndieAuth callback state mismatch",
                extra={"indieauth_me": pending_me, "indieauth_state": state},
            )
            return redirect(next_url)

        if not returned_me:
            logger.info("IndieAuth callback missing me", extra={"indieauth_me": pending_me})
            return redirect(next_url)

        try:
            body = urlencode(
                {
                    "grant_type": "authorization_code",
                    "code": code,
                    "client_id": request.build_absolute_uri("/"),
                    "redirect_uri": request.build_absolute_uri(reverse("indieauth-callback")),
                }
            ).encode("utf-8")
            token_request = Request(
                token_endpoint,
                data=body,
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
            )
            with urlopen(token_request, timeout=10) as response:
                content_type = response.headers.get("Content-Type", "")
                response_body = response.read().decode("utf-8", errors="ignore")
                if "json" in content_type:
                    token_data = json.loads(response_body or "{}")
                else:
                    token_data = parse_qs(response_body)
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
            logger.info(
                "IndieAuth token exchange failed",
                extra={"indieauth_me": pending_me, "indieauth_error": str(exc)},
            )
            return redirect(next_url)

        token_me = _first_value(token_data, "me")
        normalized_me = _normalize_me_url(token_me)
        pending_normalized = _normalize_me_url(pending_me or "")
        returned_normalized = _normalize_me_url(returned_me)

        if not normalized_me or normalized_me != pending_normalized or normalized_me != returned_normalized:
            logger.info(
                "IndieAuth token verification failed",
                extra={
                    "indieauth_me": pending_me,
                    "indieauth_token_me": token_me,
                    "indieauth_returned_me": returned_me,
                },
            )
            return redirect(next_url)

        request.session["indieauth_me"] = normalized_me
        return redirect(next_url)


@method_decorator(csrf_exempt, name="dispatch")
class WebmentionView(View):
    http_method_names = ["post"]

    def post(self, request):
        source = request.POST.get("source")
        target = request.POST.get("target")
        mention_type = request.POST.get("wm-property") or Webmention.MENTION

        if not source or not target:
            return HttpResponseBadRequest("Missing source or target")

        target_post, error = _target_is_valid(request, target)
        if error:
            return error

        mention_type = mention_type if mention_type in dict(Webmention.MENTION_CHOICES) else Webmention.MENTION

        defaults = {
            "mention_type": mention_type,
            "status": Webmention.PENDING,
            "target_post": target_post,
            "error": "",
            "is_incoming": True,
        }
        try:
            obj, _ = Webmention.objects.update_or_create(source=source, target=target, defaults=defaults)
        except IntegrityError:
            Webmention.objects.filter(source=source, target=target).update(**defaults)
            obj = Webmention.objects.get(source=source, target=target)

        from micropub.tasks import verify_and_update_webmention
        transaction.on_commit(lambda: verify_and_update_webmention.delay(obj.id))
        return HttpResponse(status=202)


class WebmentionSubmitView(View):
    http_method_names = ["post"]

    def post(self, request):
        source = (request.POST.get("source") or "").strip()
        target = (request.POST.get("target") or "").strip()
        mention_type = (request.POST.get("mention_type") or "").strip()
        next_url = _safe_next_url(request, request.POST.get("next", ""))
        indieauth_me = request.session.get("indieauth_me")

        if not indieauth_me:
            logger.info(
                "Webmention submission rejected without IndieAuth",
                extra={"webmention_source": source, "webmention_target": target},
            )
            return redirect(next_url)

        if not source or not target:
            logger.info(
                "Webmention submission missing fields",
                extra={
                    "webmention_source": source,
                    "webmention_target": target,
                    "indieauth_me": indieauth_me,
                },
            )
            return redirect(next_url)

        if not _source_matches_indieauth_me(indieauth_me, source):
            logger.info(
                "Webmention submission source mismatch",
                extra={
                    "webmention_source": source,
                    "webmention_target": target,
                    "indieauth_me": indieauth_me,
                },
            )
            return redirect(next_url)

        target_post, error = _target_is_valid(request, target)
        if error:
            logger.info(
                "Webmention submission invalid target",
                extra={
                    "webmention_source": source,
                    "webmention_target": target,
                    "indieauth_me": indieauth_me,
                },
            )
            return redirect(next_url)

        mention_type = mention_type if mention_type in dict(Webmention.MENTION_CHOICES) else Webmention.MENTION
        defaults = {
            "mention_type": mention_type,
            "status": Webmention.PENDING,
            "target_post": target_post,
            "error": "",
            "is_incoming": True,
        }
        try:
            obj, _ = Webmention.objects.update_or_create(source=source, target=target, defaults=defaults)
        except IntegrityError:
            Webmention.objects.filter(source=source, target=target).update(**defaults)
            obj = Webmention.objects.get(source=source, target=target)

        from micropub.tasks import verify_and_update_webmention
        transaction.on_commit(lambda: verify_and_update_webmention.delay(obj.id))
        return redirect(next_url)
