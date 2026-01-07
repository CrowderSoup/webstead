import json
import mimetypes
import os
from datetime import datetime
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen
from uuid import uuid4
from django.http import (
    HttpResponse,
    HttpResponseBadRequest,
    JsonResponse,
)
from django.utils import timezone
from django.utils.text import slugify
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt
from django.urls import reverse
from django.core.files.base import ContentFile

from markdownify import markdownify as html_to_markdown

from blog.models import Post, Tag
from files.models import Attachment, File
from .models import Webmention
from .webmention import send_webmentions_for_post

TOKEN_ENDPOINT = "https://tokens.indieauth.com/token"


def _first_value(data: dict, key: str, default=None):
    value = data.get(key, [])
    if isinstance(value, list):
        return value[0] if value else default
    return value or default


def _parse_scope(scope_value):
    if isinstance(scope_value, list):
        scope_value = _first_value({"scope": scope_value}, "scope", "")
    if isinstance(scope_value, str):
        return [s for s in scope_value.split() if s]
    return []


def _has_token_conflict(request):
    auth_header = request.META.get("HTTP_AUTHORIZATION", "")
    has_header = auth_header.startswith("Bearer ")
    has_body_token = False

    if request.content_type and "json" in request.content_type:
        try:
            raw = json.loads(request.body or "{}")
            if isinstance(raw, dict) and raw.get("access_token"):
                has_body_token = True
        except json.JSONDecodeError:
            has_body_token = False
    else:
        has_body_token = bool(request.POST.get("access_token"))

    has_query_token = bool(request.GET.get("access_token"))

    return has_header and (has_body_token or has_query_token)


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
    send_webmentions_for_post(post, source_url)
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


def _determine_kind(request, data, name, like_of, repost_of, in_reply_to):
    if like_of:
        return Post.LIKE
    if repost_of:
        return Post.REPOST
    if in_reply_to:
        return Post.REPLY
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
    for photo_item in data.get("photo", []):
        if isinstance(photo_item, str) and photo_item and not photo_item.startswith("<UploadedFile"):
            if photo_item.startswith("!["):
                post.content += f"\n{photo_item}\n"
                continue
            if _download_and_attach_photo(post, photo_item):
                continue
            post.content += f"\n![Photo]({photo_item})\n"
        elif isinstance(photo_item, dict):
            url = photo_item.get("url")
            alt_text = photo_item.get("alt") or ""
            if isinstance(url, str) and url:
                if _download_and_attach_photo(post, url, alt_text=alt_text):
                    continue
                alt_fragment = alt_text if alt_text else "Photo"
                post.content += f"\n![{alt_fragment}]({url})\n"
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
    categories = data.get("category", [])
    published = _first_value(data, "published")
    mf2_objects = _extract_mf2_objects(data)

    kind = _determine_kind(request, data, name, like_of, repost_of, in_reply_to)

    if not content:
        if kind == Post.LIKE:
            content = f"Liked {like_of}"
        elif kind == Post.REPOST:
            content = f"Reposted {repost_of}"
        elif kind == Post.REPLY:
            content = f"Reply to {in_reply_to}"

    published_on = _parse_published_date(published)

    post = Post(
        title=name or "",
        content=content,
        kind=kind,
        published_on=published_on,
        like_of=like_of or "",
        repost_of=repost_of or "",
        in_reply_to=in_reply_to or "",
        mf2=mf2_objects,
    )
    post.save()

    _apply_categories(post, categories)
    _attach_uploaded_photos(request, post)
    _attach_remote_photos(data, post)

    location = request.build_absolute_uri(post.get_absolute_url())
    send_webmentions_for_post(post, location)

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


def _authorized(request):
    auth_header = request.META.get("HTTP_AUTHORIZATION", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:]
    else:
        token = request.POST.get("access_token") or request.GET.get("access_token")

    if not token:
        return False, []

    verification_request = Request(
        TOKEN_ENDPOINT,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        },
        method="GET",
    )

    try:
        with urlopen(verification_request, timeout=5) as response:
            body = response.read().decode()
            status_code = response.status
            content_type = response.headers.get("Content-Type", "")
    except (HTTPError, URLError, TimeoutError):
        return False, []

    if status_code != 200:
        return False, []

    scopes = []
    if body:
        try:
            token_data = json.loads(body) if "application/json" in content_type else parse_qs(body)
        except json.JSONDecodeError:
            return False, []

        active = token_data.get("active")
        if isinstance(active, list):
            active = _first_value({"active": active}, "active")
        if active is False or (isinstance(active, str) and active.lower() == "false"):
            return False, []

        error = token_data.get("error")
        if isinstance(error, list):
            error = _first_value({"error": error}, "error")
        if error:
            return False, []

        scopes = _parse_scope(token_data.get("scope", []))

    return True, scopes


@method_decorator(csrf_exempt, name="dispatch")
class MicropubView(View):
    http_method_names = ["get", "post"]

    def dispatch(self, request, *args, **kwargs):
        if _has_token_conflict(request):
            return JsonResponse({"error": "invalid_request"}, status=400)
        authorized, scopes = _authorized(request)
        if not authorized:
            return JsonResponse({"error": "unauthorized"}, status=401)
        request.micropub_scopes = scopes
        return super().dispatch(request, *args, **kwargs)

    def get(self, request):
        query = request.GET.get("q")
        if query == "config":
            insufficient = _require_scope(request, None)
            if insufficient:
                return insufficient
            media_endpoint = request.build_absolute_uri(reverse("micropub-media"))
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
                    ],
                    "syndicate-to": [],
                }
            )
        if query == "syndicate-to":
            insufficient = _require_scope(request, None)
            if insufficient:
                return insufficient
            return JsonResponse({"syndicate-to": []})
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
            return JsonResponse({"error": "invalid_request"}, status=400)
        authorized, scopes = _authorized(request)
        if not authorized:
            return JsonResponse({"error": "unauthorized"}, status=401)
        request.micropub_scopes = scopes
        return super().dispatch(request, *args, **kwargs)

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


@method_decorator(csrf_exempt, name="dispatch")
class WebmentionView(View):
    http_method_names = ["post"]

    def post(self, request):
        source = request.POST.get("source")
        target = request.POST.get("target")
        mention_type = request.POST.get("wm-property") or Webmention.MENTION

        if not source or not target:
            return HttpResponseBadRequest("Missing source or target")

        target_post = None
        parsed = urlparse(target)
        slug = parsed.path.rstrip("/").split("/")[-1]
        if slug:
            try:
                target_post = Post.objects.get(slug=slug)
            except Post.DoesNotExist:
                target_post = None

        status = Webmention.ACCEPTED if target_post else Webmention.PENDING

        Webmention.objects.create(
            source=source,
            target=target,
            mention_type=mention_type if mention_type in dict(Webmention.MENTION_CHOICES) else Webmention.MENTION,
            status=status,
            target_post=target_post,
        )

        return HttpResponse(status=202)
