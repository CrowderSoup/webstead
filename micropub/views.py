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
        return False

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
        return False

    if status_code != 200:
        return False

    if body:
        try:
            token_data = json.loads(body) if "application/json" in content_type else parse_qs(body)
        except json.JSONDecodeError:
            return False

        active = token_data.get("active")
        if isinstance(active, list):
            active = _first_value({"active": active}, "active")
        if active is False or (isinstance(active, str) and active.lower() == "false"):
            return False

        error = token_data.get("error")
        if isinstance(error, list):
            error = _first_value({"error": error}, "error")
        if error:
            return False

    return True


@method_decorator(csrf_exempt, name="dispatch")
class MicropubView(View):
    http_method_names = ["get", "post"]

    def dispatch(self, request, *args, **kwargs):
        if not _authorized(request):
            return JsonResponse({"error": "unauthorized"}, status=401)
        return super().dispatch(request, *args, **kwargs)

    def get(self, request):
        query = request.GET.get("q")
        if query == "config":
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
                }
            )
        return HttpResponseBadRequest("Unsupported query")

    def post(self, request):
        data = _normalize_payload(request)
        action = _first_value(data, "action")

        if action == "delete":
            target_url = _first_value(data, "url")
            if not target_url:
                return HttpResponseBadRequest("Missing url for delete")

            parsed = urlparse(target_url)
            path_parts = [part for part in parsed.path.split("/") if part]
            slug = path_parts[-1] if path_parts else ""
            if not slug:
                return HttpResponseBadRequest("Invalid url for delete")

            try:
                post = Post.objects.get(slug=slug)
            except Post.DoesNotExist:
                return HttpResponse(status=404)

            post.delete()
            return HttpResponse(status=204)

        if action == "update":
            target_url = _first_value(data, "url")
            if not target_url:
                return HttpResponseBadRequest("Missing url for update")

            parsed = urlparse(target_url)
            path_parts = [part for part in parsed.path.split("/") if part]
            slug = path_parts[-1] if path_parts else ""
            if not slug:
                return HttpResponseBadRequest("Invalid url for update")

            try:
                post = Post.objects.get(slug=slug)
            except Post.DoesNotExist:
                return HttpResponseBadRequest("Post not found for update")

            replace_list = data.get("replace", [])
            replace_data = replace_list[0] if replace_list else {}
            if replace_data and not isinstance(replace_data, dict):
                return HttpResponseBadRequest("Invalid replace payload")

            normalized_replace = {}
            for key, value in (replace_data or {}).items():
                value_list = value if isinstance(value, list) else [value]
                n_key, n_values = _normalize_property(key, value_list)
                normalized_replace[n_key] = n_values

            add_list = data.get("add", [])
            add_data = add_list[0] if add_list else {}
            if add_data and not isinstance(add_data, dict):
                return HttpResponseBadRequest("Invalid add payload")

            normalized_add = {}
            for key, value in (add_data or {}).items():
                value_list = value if isinstance(value, list) else [value]
                n_key, n_values = _normalize_property(key, value_list)
                normalized_add[n_key] = n_values

            delete_list = data.get("delete", [])
            delete_data = delete_list[0] if delete_list else {}
            if delete_data and not isinstance(delete_data, (dict, list, str)):
                return HttpResponseBadRequest("Invalid delete payload")

            normalized_delete = {}
            if isinstance(delete_data, dict):
                for key, value in (delete_data or {}).items():
                    value_list = value if isinstance(value, list) else [value]
                    n_key, n_values = _normalize_property(key, value_list)
                    normalized_delete[n_key] = n_values
            elif isinstance(delete_data, (list, str)):
                props = delete_data if isinstance(delete_data, list) else [delete_data]
                normalized_delete = {prop: [] for prop in props}

            if "content" in normalized_replace:
                new_content = _first_value({"content": normalized_replace["content"]}, "content")
                if new_content is not None:
                    post.content = new_content

            if "category" in normalized_replace:
                post.tags.clear()
                for category in normalized_replace["category"]:
                    tag_slug = slugify(str(category))
                    if not tag_slug:
                        continue
                    tag, _ = Tag.objects.get_or_create(tag=tag_slug)
                    post.tags.add(tag)

            if "category" in normalized_add:
                for category in normalized_add["category"]:
                    tag_slug = slugify(str(category))
                    if not tag_slug:
                        continue
                    tag, _ = Tag.objects.get_or_create(tag=tag_slug)
                    post.tags.add(tag)

            if "category" in normalized_delete:
                for category in normalized_delete["category"]:
                    tag_slug = slugify(str(category))
                    if not tag_slug:
                        continue
                    post.tags.filter(tag=tag_slug).delete()
                if normalized_delete["category"] == []:
                    post.tags.clear()

            post.save()
            return HttpResponse(status=204)

        content = _first_value(data, "content", "") or ""
        name = _first_value(data, "name")
        like_of = _first_value(data, "like-of")
        repost_of = _first_value(data, "repost-of")
        in_reply_to = _first_value(data, "in-reply-to")
        categories = data.get("category", [])
        published = _first_value(data, "published")
        mf2_objects = _extract_mf2_objects(data)

        if like_of:
            kind = Post.LIKE
        elif repost_of:
            kind = Post.REPOST
        elif in_reply_to:
            kind = Post.REPLY
        elif request.FILES.getlist("photo") or request.FILES.getlist("photo[]") or data.get("photo"):
            kind = Post.PHOTO
        elif name:
            kind = Post.ARTICLE
        else:
            kind = Post.NOTE

        if not content:
            if kind == Post.LIKE:
                content = f"Liked {like_of}"
            elif kind == Post.REPOST:
                content = f"Reposted {repost_of}"
            elif kind == Post.REPLY:
                content = f"Reply to {in_reply_to}"

        if published:
            try:
                parsed = datetime.fromisoformat(published)
                if timezone.is_naive(parsed):
                    published_on = timezone.make_aware(parsed)
                else:
                    published_on = parsed
            except ValueError:
                published_on = timezone.now()
        else:
            published_on = timezone.now()

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

        for category in categories:
            tag_slug = slugify(str(category))
            if not tag_slug:
                continue
            tag, _ = Tag.objects.get_or_create(tag=tag_slug)
            post.tags.add(tag)

        uploaded_photos = request.FILES.getlist("photo") + request.FILES.getlist("photo[]")
        for uploaded in uploaded_photos:
            asset = File.objects.create(kind=File.IMAGE, file=uploaded)
            Attachment.objects.create(content_object=post, asset=asset, role="photo")

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

        location = request.build_absolute_uri(post.get_absolute_url())

        # Send outgoing webmentions to referenced targets
        send_webmentions_for_post(post, location)

        response = HttpResponse(status=201)
        response["Location"] = location
        return response


@method_decorator(csrf_exempt, name="dispatch")
class MicropubMediaView(View):
    http_method_names = ["post"]

    def dispatch(self, request, *args, **kwargs):
        if not _authorized(request):
            return JsonResponse({"error": "unauthorized"}, status=401)
        return super().dispatch(request, *args, **kwargs)

    def post(self, request):
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
