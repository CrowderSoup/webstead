import base64
import hashlib
import json
import logging
import markdown
import secrets
from datetime import timedelta
from string import Template

from django.conf import settings
from django.core.paginator import Paginator, EmptyPage, PageNotAnInteger
from django.http import HttpResponse, JsonResponse
from django.shortcuts import redirect, render, get_object_or_404
from django.templatetags.static import static
from django.urls import reverse
from django.utils import timezone
from django.utils.text import slugify
from django.views.decorators.http import require_http_methods, require_POST

from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from core.models import SiteConfiguration
from files.models import Attachment, File
from .syndication import available_targets, syndicate_post, target_statuses

from .models import Post, Tag

logger = logging.getLogger(__name__)


def _staff_guard(request):
    if not request.user.is_authenticated or not request.user.is_staff:
        return HttpResponse(status=401)
    return None


def _flash(request, key, message):
    if message:
        request.session[key] = message


def _oauth_error(request, message):
    _flash(request, "post_editor_error", message)
    return redirect(reverse("post_editor"))


def _oauth_success(request, message):
    _flash(request, "post_editor_success", message)
    return redirect(reverse("post_editor"))


def _pkce_pair():
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("utf-8")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("utf-8")
    return verifier, challenge


def _fetch_bluesky_identity(service: str, token: str) -> tuple[str | None, str | None]:
    endpoint = f"{service}/xrpc/com.atproto.server.getSession"
    request = Request(
        endpoint,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        method="GET",
    )

    try:
        with urlopen(request, timeout=10) as response:
            body = response.read().decode()
            if response.status >= 400:
                logger.error("Bluesky identity lookup failed with status %s: %s", response.status, body)
                return None, None
    except (HTTPError, URLError, TimeoutError) as exc:  # pragma: no cover - network
        logger.exception("Error fetching Bluesky identity: %s", exc)
        return None, None

    try:
        data = json.loads(body or "{}")
    except json.JSONDecodeError:
        logger.error("Invalid JSON from Bluesky identity response")
        return None, None

    did = data.get("did") if isinstance(data.get("did"), str) else None
    handle = data.get("handle") if isinstance(data.get("handle"), str) else None
    return did, handle

def posts(request):
    requested_kinds = request.GET.getlist("kind")
    valid_kinds = {kind for kind, _ in Post.KIND_CHOICES}
    selected_kinds = [kind for kind in requested_kinds if kind in valid_kinds]
    feed_kinds_query = urlencode([("kind", kind) for kind in selected_kinds])
    selected_kinds = selected_kinds or [Post.ARTICLE]

    query_set = Post.objects.exclude(published_on__isnull=True).filter(deleted=False).order_by("-published_on")
    query_set = query_set.filter(kind__in=selected_kinds)

    paginator = Paginator(query_set, 10)
    page_number = request.GET.get("page")

    try:
        posts = paginator.page(page_number)
    except PageNotAnInteger:
        posts = paginator.page(1)
    except EmptyPage:
        posts = paginator.page(paginator.num_pages)

    return render(
        request,
        'blog/posts.html',
        {
            "posts": posts,
            "post_kinds": Post.KIND_CHOICES,
            "selected_kinds": selected_kinds,
            "selected_kinds_query": urlencode([("kind", kind) for kind in selected_kinds]),
            "feed_kinds_query": feed_kinds_query,
        },
    )

def posts_by_tag(request, tag):
    tag = get_object_or_404(Tag, tag=tag)
    query_set = Post.objects.exclude(published_on__isnull=True).filter(tags=tag, deleted=False).order_by("-published_on")
    paginator = Paginator(query_set, 10)
    page_number = request.GET.get("page")

    try:
        posts = paginator.page(page_number)
    except PageNotAnInteger:
        posts = paginator.page(1)
    except EmptyPage:
        posts.paginator.page(paginator.num_pages)

    return render(request, 'blog/posts_by_tag.html', { "posts": posts, "tag": tag })

def post(request, slug):
    post = get_object_or_404(
        Post.objects.only("title", "content", "slug", "published_on", "tags", "mastodon_url", "bluesky_url"),
        slug=slug,
        deleted=False,
    )

    tags = post.tags.all()

    return render(request, 'blog/post.html', { "post": post })


@require_http_methods(["GET", "POST"])
def post_editor(request, slug=None):
    guard = _staff_guard(request)
    if guard:
        return guard

    flash_error = request.session.pop("post_editor_error", None)
    flash_success = request.session.pop("post_editor_success", None)

    editing_post = None
    if slug:
        editing_post = get_object_or_404(Post, slug=slug, deleted=False)

    selected_kind = request.POST.get("kind") or (editing_post.kind if editing_post else Post.ARTICLE)
    valid_kinds = {kind for kind, _ in Post.KIND_CHOICES}
    if selected_kind not in valid_kinds:
        selected_kind = Post.ARTICLE

    connect_urls = {
        "mastodon": reverse("mastodon_oauth_start"),
        "bluesky": reverse("bluesky_oauth_start"),
    }
    syndication_options = []
    for status in target_statuses():
        syndication_options.append(
            {
                "uid": status["uid"],
                "name": status["name"],
                "connected": status["connected"],
                "connect_url": connect_urls.get(status["uid"]),
            }
        )

    configured_targets = available_targets()
    configured_target_ids = {target.uid for target in configured_targets}
    connected_by_uid = {target.uid: True for target in configured_targets}

    existing_tags = Tag.objects.all()

    initial_tags = list(editing_post.tags.values_list("tag", flat=True)) if editing_post else []
    title_initial = editing_post.title if editing_post else ""
    content_initial = editing_post.content if editing_post else ""
    mastodon_initial = editing_post.mastodon_url if editing_post else ""
    bluesky_initial = editing_post.bluesky_url if editing_post else ""
    default_checked = not editing_post

    def _initial_syndicate(uid: str, initial_url: str) -> bool:
        if uid not in configured_target_ids:
            return False
        if request.method == "POST":
            return bool(request.POST.get(f"syndicate_{uid}"))
        if editing_post:
            return bool(initial_url)
        return default_checked

    syndicate_mastodon_raw = _initial_syndicate("mastodon", mastodon_initial)
    syndicate_bluesky_raw = _initial_syndicate("bluesky", bluesky_initial)

    context = {
        "hide_nav": True,
        "hide_site_description": True,
        "post_kinds": Post.KIND_CHOICES,
        "selected_kind": selected_kind,
        "tags": existing_tags,
        "errors": [],
        "title_value": request.POST.get("title", title_initial).strip(),
        "content_value": request.POST.get("content", content_initial).strip(),
        "like_of_value": request.POST.get("like_of", editing_post.like_of if editing_post else "").strip(),
        "repost_of_value": request.POST.get("repost_of", editing_post.repost_of if editing_post else "").strip(),
        "in_reply_to_value": request.POST.get("in_reply_to", editing_post.in_reply_to if editing_post else "").strip(),
        "new_tags_value": request.POST.get("new_tags", "").strip(),
        "selected_tags": [slug for slug in request.POST.getlist("tags") if slug] or initial_tags,
        "syndicate_mastodon": syndicate_mastodon_raw,
        "syndicate_bluesky": syndicate_bluesky_raw,
        "syndication_targets": configured_targets,
        "syndication_options": syndication_options,
        "syndication_connect": [opt for opt in syndication_options if not opt["connected"]],
        "syndication_links": {"mastodon": mastodon_initial, "bluesky": bluesky_initial},
        "manifest_url": static("pwa/post-editor.webmanifest"),
        "form_action": reverse("post_editor_edit", kwargs={"slug": editing_post.slug}) if editing_post else reverse("post_editor"),
        "edit_mode": bool(editing_post),
        "status_message": flash_success or "",
        "existing_photos_json": json.dumps(
            [
                {
                    "id": attachment.asset.id,
                    "url": attachment.asset.file.url,
                    "alt": attachment.asset.alt_text,
                    "caption": attachment.asset.caption,
                }
                for attachment in editing_post.attachments.all()
            ]
        ) if editing_post else "[]",
    }
    if flash_error:
        context["errors"].append(flash_error)

    if request.method == "POST":
        tag_slugs = context["selected_tags"]
        new_tags_raw = context["new_tags_value"].replace(",", " ")
        new_tags = []
        for raw_tag in new_tags_raw.split():
            normalized = slugify(raw_tag)
            if normalized and normalized not in tag_slugs and normalized not in new_tags:
                new_tags.append(normalized)
        all_tags = list(dict.fromkeys(tag_slugs + new_tags))
        context["selected_tags"] = all_tags

        existing_ids = request.POST.getlist("existing_ids")
        existing_alts = request.POST.getlist("existing_alts")
        existing_captions = request.POST.getlist("existing_captions")
        existing_positions = request.POST.getlist("existing_positions")
        existing_remove_ids = set()
        for raw_id in request.POST.getlist("existing_remove_ids"):
            try:
                existing_remove_ids.add(int(raw_id))
            except (TypeError, ValueError):
                continue
        existing_meta = {}
        for i in range(min(len(existing_ids), len(existing_positions))):
            try:
                asset_id = int(existing_ids[i])
                position = int(existing_positions[i])
            except (TypeError, ValueError):
                continue
            alt_text = existing_alts[i] if i < len(existing_alts) else ""
            caption = existing_captions[i] if i < len(existing_captions) else ""
            existing_meta[asset_id] = {"position": position, "alt": alt_text, "caption": caption}

        uploaded_ids = request.POST.getlist("uploaded_ids")
        uploaded_alts = request.POST.getlist("uploaded_alts")
        uploaded_captions = request.POST.getlist("uploaded_captions")
        uploaded_positions = request.POST.getlist("uploaded_positions")
        uploaded_meta = {}
        for i in range(min(len(uploaded_ids), len(uploaded_positions))):
            try:
                asset_id = int(uploaded_ids[i])
                position = int(uploaded_positions[i])
            except (TypeError, ValueError):
                continue
            alt_text = uploaded_alts[i] if i < len(uploaded_alts) else ""
            caption = uploaded_captions[i] if i < len(uploaded_captions) else ""
            uploaded_meta[asset_id] = {"position": position, "alt": alt_text, "caption": caption}

        uploads = request.FILES.getlist("photos")

        syndicate_mastodon = context["syndicate_mastodon"] if selected_kind in (Post.NOTE, Post.PHOTO) else False
        syndicate_bluesky = context["syndicate_bluesky"] if selected_kind in (Post.NOTE, Post.PHOTO) else False

        if selected_kind not in (Post.NOTE, Post.PHOTO):
            context["syndicate_mastodon"] = False
            context["syndicate_bluesky"] = False
        if "mastodon" not in connected_by_uid:
            syndicate_mastodon = False
            context["syndicate_mastodon"] = False
        if "bluesky" not in connected_by_uid:
            syndicate_bluesky = False
            context["syndicate_bluesky"] = False

        errors = list(context["errors"])
        if selected_kind == Post.LIKE and not context["like_of_value"]:
            errors.append("Provide a URL for the like.")
        if selected_kind == Post.REPOST and not context["repost_of_value"]:
            errors.append("Provide a URL for the repost.")
        if selected_kind == Post.REPLY and not context["in_reply_to_value"]:
            errors.append("Provide a URL for the reply.")
        if selected_kind in (Post.ARTICLE, Post.NOTE) and not context["content_value"]:
            errors.append("Content is required for this post type.")
        remaining_existing_photos = (
            editing_post.attachments.exclude(asset__id__in=existing_remove_ids).exists() if editing_post else False
        )
        has_new_uploads = bool(uploaded_meta) or bool(uploads)
        if selected_kind == Post.PHOTO and not (context["content_value"] or has_new_uploads or remaining_existing_photos):
            errors.append("Add a caption or at least one photo for photo posts.")

        if errors:
            context["errors"] = errors
            return render(request, "blog/editor.html", context)

        content = context["content_value"]
        if not content:
            if selected_kind == Post.LIKE:
                content = f"Liked {context['like_of_value']}"
            elif selected_kind == Post.REPOST:
                content = f"Reposted {context['repost_of_value']}"
            elif selected_kind == Post.REPLY:
                content = f"Reply to {context['in_reply_to_value']}"

        post = editing_post or Post(author=request.user)
        post.title = context["title_value"]
        post.content = content
        post.kind = selected_kind
        post.like_of = context["like_of_value"] if selected_kind == Post.LIKE else ""
        post.repost_of = context["repost_of_value"] if selected_kind == Post.REPOST else ""
        post.in_reply_to = context["in_reply_to_value"] if selected_kind == Post.REPLY else ""
        if selected_kind in (Post.NOTE, Post.PHOTO):
            if not syndicate_mastodon:
                post.mastodon_url = ""
            if not syndicate_bluesky:
                post.bluesky_url = ""
        else:
            post.mastodon_url = ""
            post.bluesky_url = ""
        if not editing_post:
            post.published_on = timezone.now()
        post.save()

        if all_tags:
            tags_to_assign = []
            for slug in all_tags:
                tag, _ = Tag.objects.get_or_create(tag=slug)
                tags_to_assign.append(tag)
            post.tags.set(tags_to_assign)

        if editing_post:
            for attachment in list(post.attachments.select_related("asset")):
                asset_id = attachment.asset.id
                if asset_id in existing_remove_ids:
                    attachment.asset.delete()
                    continue
                meta = existing_meta.get(asset_id)
                if not meta:
                    continue
                asset = attachment.asset
                asset.alt_text = meta.get("alt", "")
                asset.caption = meta.get("caption", "")
                asset.save(update_fields=["alt_text", "caption"])
                attachment.sort_order = meta.get("position", attachment.sort_order)
                attachment.save(update_fields=["sort_order"])

        if uploaded_meta:
            uploaded_assets = File.objects.filter(id__in=uploaded_meta.keys(), owner=request.user)
            for asset in uploaded_assets:
                meta = uploaded_meta.get(asset.id, {})
                asset.alt_text = meta.get("alt", "")
                asset.caption = meta.get("caption", "")
                asset.save(update_fields=["alt_text", "caption"])
                Attachment.objects.create(
                    content_object=post,
                    asset=asset,
                    role="photo",
                    sort_order=meta.get("position", 0),
                )

        for index, upload in enumerate(uploads):
            meta = {}
            asset = File.objects.create(
                kind=File.IMAGE,
                file=upload,
                owner=request.user,
                alt_text=meta.get("alt", ""),
                caption=meta.get("caption", ""),
            )
            Attachment.objects.create(content_object=post, asset=asset, role="photo", sort_order=index)

        if selected_kind in (Post.NOTE, Post.PHOTO) and configured_target_ids:
            requested_targets = []
            if syndicate_mastodon and "mastodon" in configured_target_ids:
                requested_targets.append("mastodon")
            if syndicate_bluesky and "bluesky" in configured_target_ids:
                requested_targets.append("bluesky")

            if requested_targets:
                canonical_url = request.build_absolute_uri(post.get_absolute_url())
                results = syndicate_post(post, canonical_url, requested_targets)
                updates = []
                mastodon_result = results.get("mastodon")
                bluesky_result = results.get("bluesky")
                if mastodon_result:
                    post.mastodon_url = mastodon_result
                    updates.append("mastodon_url")
                if bluesky_result:
                    post.bluesky_url = bluesky_result
                    updates.append("bluesky_url")
                if updates:
                    post.save(update_fields=updates)

        return redirect(post.get_absolute_url())

    return render(request, "blog/editor.html", context)


@require_http_methods(["GET"])
def mastodon_oauth_start(request):
    guard = _staff_guard(request)
    if guard:
        return guard

    config = SiteConfiguration.get_solo()
    base_url = (config.mastodon_base_url or getattr(settings, "MASTODON_BASE_URL", "")).rstrip("/")
    if not base_url:
        return _oauth_error(request, "Set MASTODON_BASE_URL to your instance URL before connecting Mastodon.")

    redirect_uri = request.build_absolute_uri(reverse("mastodon_oauth_callback"))
    client_id = config.mastodon_client_id
    client_secret = config.mastodon_client_secret

    if not client_id or not client_secret:
        payload = urlencode(
            {
                "client_name": config.title or "Blog syndication",
                "redirect_uris": redirect_uri,
                "scopes": "write:statuses offline",
                "website": request.build_absolute_uri("/"),
            }
        ).encode("utf-8")
        app_request = Request(
            f"{base_url}/api/v1/apps",
            data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )

        try:
            with urlopen(app_request, timeout=10) as response:
                body = response.read().decode()
                if response.status >= 400:
                    logger.error("Mastodon app registration failed with status %s: %s", response.status, body)
                    return _oauth_error(request, "Unable to register Mastodon application.")
        except (HTTPError, URLError, TimeoutError) as exc:  # pragma: no cover - network
            logger.exception("Error registering Mastodon app: %s", exc)
            return _oauth_error(request, "Unable to register Mastodon app right now.")

        try:
            app_data = json.loads(body or "{}")
        except json.JSONDecodeError:
            return _oauth_error(request, "Invalid response while registering Mastodon app.")

        client_id = app_data.get("client_id")
        client_secret = app_data.get("client_secret")
        if not client_id or not client_secret:
            return _oauth_error(request, "Mastodon did not return client credentials.")

        config.mastodon_client_id = client_id
        config.mastodon_client_secret = client_secret
        config.mastodon_base_url = base_url
        config.save(update_fields=["mastodon_client_id", "mastodon_client_secret", "mastodon_base_url"])

    state = secrets.token_urlsafe(16)
    request.session["mastodon_oauth_state"] = state
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": "write:statuses offline",
        "state": state,
    }
    authorize_url = f"{base_url}/oauth/authorize?{urlencode(params)}"
    return redirect(authorize_url)


@require_http_methods(["GET"])
def mastodon_oauth_callback(request):
    guard = _staff_guard(request)
    if guard:
        return guard

    code = request.GET.get("code")
    state = request.GET.get("state")
    expected_state = request.session.pop("mastodon_oauth_state", None)
    if not code or not state or state != expected_state:
        return _oauth_error(request, "Mastodon authorization failed. Please try again.")

    config = SiteConfiguration.get_solo()
    base_url = (config.mastodon_base_url or getattr(settings, "MASTODON_BASE_URL", "")).rstrip("/")
    client_id = config.mastodon_client_id
    client_secret = config.mastodon_client_secret
    if not (base_url and client_id and client_secret):
        return _oauth_error(request, "Mastodon OAuth is not configured for this site.")

    redirect_uri = request.build_absolute_uri(reverse("mastodon_oauth_callback"))
    payload = urlencode(
        {
            "grant_type": "authorization_code",
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
            "scope": "write:statuses offline",
        }
    ).encode("utf-8")
    token_request = Request(
        f"{base_url}/oauth/token",
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )

    try:
        with urlopen(token_request, timeout=10) as response:
            body = response.read().decode()
            if response.status >= 400:
                logger.error("Mastodon token exchange failed with status %s: %s", response.status, body)
                return _oauth_error(request, "Mastodon did not accept the authorization code.")
    except (HTTPError, URLError, TimeoutError) as exc:  # pragma: no cover - network
        logger.exception("Error exchanging Mastodon code: %s", exc)
        return _oauth_error(request, "Unable to complete Mastodon authorization.")

    try:
        data = json.loads(body or "{}")
    except json.JSONDecodeError:
        return _oauth_error(request, "Invalid Mastodon token response.")

    access_token = data.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        return _oauth_error(request, "Mastodon did not return an access token.")

    refresh_token = data.get("refresh_token", "")
    expires_at = None
    expires_in = data.get("expires_in")
    try:
        if expires_in:
            expires_at = timezone.now() + timedelta(seconds=int(expires_in))
    except (TypeError, ValueError):
        expires_at = None

    config.mastodon_access_token = access_token
    config.mastodon_refresh_token = refresh_token or ""
    config.mastodon_token_expires_at = expires_at
    config.mastodon_base_url = base_url
    config.save(
        update_fields=[
            "mastodon_access_token",
            "mastodon_refresh_token",
            "mastodon_token_expires_at",
            "mastodon_base_url",
        ]
    )

    return _oauth_success(request, "Mastodon connected. You can syndicate posts now.")


@require_http_methods(["GET"])
def bluesky_oauth_start(request):
    guard = _staff_guard(request)
    if guard:
        return guard

    config = SiteConfiguration.get_solo()
    env_client_id = getattr(settings, "BLUESKY_CLIENT_ID", "")
    env_client_secret = getattr(settings, "BLUESKY_CLIENT_SECRET", "")
    client_id = config.bluesky_client_id or env_client_id
    client_secret = config.bluesky_client_secret or env_client_secret
    service = (config.bluesky_service or getattr(settings, "BLUESKY_SERVICE", "https://bsky.social")).rstrip("/")

    if not client_id:
        return _oauth_error(request, "Set a Bluesky OAuth client id in site settings before connecting.")

    verifier, challenge = _pkce_pair()
    state = secrets.token_urlsafe(16)
    request.session["bluesky_oauth_state"] = state
    request.session["bluesky_code_verifier"] = verifier
    redirect_uri = request.build_absolute_uri(reverse("bluesky_oauth_callback"))

    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": "atproto",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    authorize_url = f"{service}/oauth/authorize?{urlencode(params)}"

    updates = ["bluesky_client_id", "bluesky_service"]
    config.bluesky_client_id = client_id
    config.bluesky_service = service
    if client_secret:
        config.bluesky_client_secret = client_secret
        updates.append("bluesky_client_secret")
    config.save(update_fields=updates)

    return redirect(authorize_url)


@require_http_methods(["GET"])
def bluesky_oauth_callback(request):
    guard = _staff_guard(request)
    if guard:
        return guard

    code = request.GET.get("code")
    state = request.GET.get("state")
    expected_state = request.session.pop("bluesky_oauth_state", None)
    code_verifier = request.session.pop("bluesky_code_verifier", None)
    if not code or not state or state != expected_state or not code_verifier:
        return _oauth_error(request, "Bluesky authorization failed. Please try again.")

    config = SiteConfiguration.get_solo()
    env_client_id = getattr(settings, "BLUESKY_CLIENT_ID", "")
    env_client_secret = getattr(settings, "BLUESKY_CLIENT_SECRET", "")
    client_id = config.bluesky_client_id or env_client_id
    client_secret = config.bluesky_client_secret or env_client_secret
    service = (config.bluesky_service or getattr(settings, "BLUESKY_SERVICE", "https://bsky.social")).rstrip("/")
    if not (client_id and service):
        return _oauth_error(request, "Bluesky OAuth is not configured for this site.")

    redirect_uri = request.build_absolute_uri(reverse("bluesky_oauth_callback"))
    payload = {
        "grant_type": "authorization_code",
        "code": code,
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "code_verifier": code_verifier,
    }
    if client_secret:
        payload["client_secret"] = client_secret

    token_body = urlencode(payload).encode("utf-8")
    token_request = Request(
        f"{service}/oauth/token",
        data=token_body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )

    try:
        with urlopen(token_request, timeout=10) as response:
            body = response.read().decode()
            if response.status >= 400:
                logger.error("Bluesky token exchange failed with status %s: %s", response.status, body)
                return _oauth_error(request, "Bluesky did not accept the authorization code.")
    except (HTTPError, URLError, TimeoutError) as exc:  # pragma: no cover - network
        logger.exception("Error exchanging Bluesky code: %s", exc)
        return _oauth_error(request, "Unable to complete Bluesky authorization.")

    try:
        data = json.loads(body or "{}")
    except json.JSONDecodeError:
        return _oauth_error(request, "Invalid Bluesky token response.")

    access_token = data.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        return _oauth_error(request, "Bluesky did not return an access token.")

    refresh_token = data.get("refresh_token", "")
    expires_at = None
    expires_in = data.get("expires_in")
    try:
        if expires_in:
            expires_at = timezone.now() + timedelta(seconds=int(expires_in))
    except (TypeError, ValueError):
        expires_at = None

    did = data.get("did") if isinstance(data.get("did"), str) else None
    handle = data.get("handle") if isinstance(data.get("handle"), str) else None
    if not did or not handle:
        fetched_did, fetched_handle = _fetch_bluesky_identity(service, access_token)
        did = did or fetched_did
        handle = handle or fetched_handle

    updates = [
        "bluesky_access_token",
        "bluesky_refresh_token",
        "bluesky_token_expires_at",
        "bluesky_service",
        "bluesky_client_id",
    ]
    config.bluesky_access_token = access_token
    config.bluesky_refresh_token = refresh_token or ""
    config.bluesky_token_expires_at = expires_at
    config.bluesky_service = service
    config.bluesky_client_id = client_id
    if client_secret:
        config.bluesky_client_secret = client_secret
        updates.append("bluesky_client_secret")
    if did:
        config.bluesky_did = did
        updates.append("bluesky_did")
    if handle:
        config.bluesky_handle = handle
        updates.append("bluesky_handle")

    config.save(update_fields=updates)
    return _oauth_success(request, "Bluesky connected. You can syndicate posts now.")


@require_POST
def upload_editor_photo(request):
    guard = _staff_guard(request)
    if guard:
        return guard

    upload = request.FILES.get("photo")
    if not upload:
        return JsonResponse({"error": "No file provided."}, status=400)

    asset = File.objects.create(kind=File.IMAGE, file=upload, owner=request.user)
    return JsonResponse({"id": asset.id, "url": asset.file.url})


@require_POST
def delete_editor_photo(request):
    guard = _staff_guard(request)
    if guard:
        return guard

    try:
        asset_id = int(request.POST.get("id", ""))
    except (TypeError, ValueError):
        return JsonResponse({"error": "Invalid id."}, status=400)

    try:
        asset = File.objects.get(id=asset_id, owner=request.user)
    except File.DoesNotExist:
        return JsonResponse({"error": "Not found."}, status=404)

    asset.delete()
    return JsonResponse({"status": "deleted"})


def post_editor_service_worker(request):
    guard = _staff_guard(request)
    if guard:
        return guard

    start_url = reverse("post_editor")
    css_url = static("css/site.css")
    manifest_url = static("pwa/post-editor.webmanifest")
    template = Template(
        """
const CACHE_NAME = 'post-editor-cache-v1';
const OFFLINE_URLS = [
  '$start',
  '$css',
  '$manifest'
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(OFFLINE_URLS))
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.map((key) => (key !== CACHE_NAME ? caches.delete(key) : null))))
  );
});

self.addEventListener('fetch', (event) => {
  const { request } = event;
  if (request.method !== 'GET') return;

  event.respondWith(
    caches.match(request).then((cached) =>
      cached || fetch(request).then((response) => {
        const cloned = response.clone();
        caches.open(CACHE_NAME).then((cache) => cache.put(request, cloned));
        return response;
      }).catch(() => cached))
  );
});
"""
    )

    script = template.substitute(start=start_url, css=css_url, manifest=manifest_url)
    response = HttpResponse(script, content_type="application/javascript")
    response["Cache-Control"] = "no-cache"
    return response


@require_POST
def delete_post(request, slug):
    guard = _staff_guard(request)
    if guard:
        return guard

    post = get_object_or_404(Post, slug=slug)
    post.deleted = True
    post.save(update_fields=["deleted"])
    return redirect(reverse("posts"))
