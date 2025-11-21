import json
from datetime import datetime
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen
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


def _normalize_payload(request):
    if request.content_type and "json" in request.content_type:
        try:
            raw = json.loads(request.body or "{}")
        except json.JSONDecodeError:
            return {}
        return {key: value if isinstance(value, list) else [value] for key, value in raw.items()}
    return {key: request.POST.getlist(key) for key in request.POST}


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
        content = _first_value(data, "content", "") or ""
        name = _first_value(data, "name")
        like_of = _first_value(data, "like-of")
        repost_of = _first_value(data, "repost-of")
        in_reply_to = _first_value(data, "in-reply-to")
        categories = data.get("category", [])
        published = _first_value(data, "published")

        if like_of:
            kind = Post.LIKE
        elif repost_of:
            kind = Post.REPOST
        elif in_reply_to:
            kind = Post.REPLY
        elif request.FILES.getlist("photo") or data.get("photo"):
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
        )
        post.save()

        for category in categories:
            tag_slug = slugify(str(category))
            if not tag_slug:
                continue
            tag, _ = Tag.objects.get_or_create(tag=tag_slug)
            post.tags.add(tag)

        for uploaded in request.FILES.getlist("photo"):
            asset = File.objects.create(kind=File.IMAGE, file=uploaded)
            Attachment.objects.create(content_object=post, asset=asset, role="photo")

        for photo_url in data.get("photo", []):
            if isinstance(photo_url, str) and photo_url and not photo_url.startswith("<UploadedFile"):
                post.content += f"\n![Photo]({photo_url})\n"
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
