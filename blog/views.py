from django.core.cache import cache
from django.core.paginator import Paginator, EmptyPage, PageNotAnInteger
from django.db.models import Count, Q
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import redirect, render, get_object_or_404
from django.urls import reverse
from django.views.decorators.http import require_POST
from django.utils.text import Truncator

from urllib.parse import urlencode, urlparse

from .models import Post, Tag
from .mf2 import DEFAULT_AVATAR_URL, fetch_target_from_url
from core.models import SiteConfiguration
from core.og import absolute_url, first_attachment_image_url
from micropub.models import Webmention


def _activity_from_mf2(post):
    activity = {"name": "", "track_url": ""}
    mf2_data = post.mf2 if isinstance(post.mf2, dict) else {}
    activity_items = mf2_data.get("activity") or []
    activity_item = activity_items[0] if isinstance(activity_items, list) and activity_items else activity_items
    if isinstance(activity_item, dict):
        properties = activity_item.get("properties") or {}
        if isinstance(properties, dict):
            for key in ("name", "activity-type", "category"):
                values = properties.get(key) or []
                if values and not activity["name"]:
                    activity["name"] = str(values[0])
            track_values = properties.get("track") or []
            if track_values:
                activity["track_url"] = track_values[0]
    if not activity["track_url"] and post.gpx_attachment:
        activity["track_url"] = post.gpx_attachment.asset.file.url
    return activity


def _staff_guard(request):
    if not request.user.is_authenticated or not request.user.is_staff:
        return HttpResponse(status=401)
    return None

def _is_default_interaction_content(post, target_url):
    content = (post.content or "").strip()
    if not content:
        return True
    if not target_url:
        return False
    if post.kind == Post.LIKE:
        return content == f"Liked {target_url}"
    if post.kind == Post.REPOST:
        return content == f"Reposted {target_url}"
    if post.kind == Post.REPLY:
        return content == f"Reply to {target_url}"
    return False


def _local_target_from_url(target_url, request):
    if not target_url:
        return None
    parsed = urlparse(target_url)
    if not parsed.scheme and not parsed.netloc:
        is_local = True
    elif request:
        is_local = parsed.netloc == request.get_host()
    else:
        is_local = False

    if not is_local:
        return None

    slug = parsed.path.rstrip("/").split("/")[-1]
    if not slug:
        return None

    target_post = (
        Post.objects.filter(slug=slug, deleted=False, published_on__isnull=False)
        .only("title", "content")
        .first()
    )
    if not target_post:
        return None

    summary_text = target_post.summary()
    summary_excerpt = Truncator(summary_text).chars(240, truncate="...")

    return {
        "original_url": target_url,
        "summary_excerpt": summary_excerpt,
        "summary_truncated": len(summary_text) > 240,
        "summary_text": summary_text,
        "summary_html": None,
        "title": target_post.title,
    }


def _interaction_payload(post, request=None):
    if post.kind == Post.LIKE:
        target_url = post.like_of
        label = "Liked"
    elif post.kind == Post.REPOST:
        target_url = post.repost_of
        label = "Reposted"
    elif post.kind == Post.REPLY:
        target_url = post.in_reply_to
        label = "Replying to"
    else:
        return None

    target_url = target_url or ""
    target = fetch_target_from_url(target_url) if target_url else None
    if not target and target_url:
        target = _local_target_from_url(target_url, request)

    return {
        "kind": post.kind,
        "label": label,
        "target_url": target_url,
        "target": target,
        "show_content": not _is_default_interaction_content(post, target_url),
    }


def _normalize_webmention_reply(source_url, created_at, payload):
    author_name = payload.get("author_name") or payload.get("author_url") or source_url
    author_url = payload.get("author_url") or source_url
    author_photo = payload.get("author_photo") or DEFAULT_AVATAR_URL
    excerpt = (
        payload.get("summary_excerpt")
        or payload.get("summary_text")
        or payload.get("title")
        or ""
    )
    return {
        "source": source_url,
        "created_at": created_at,
        "author_name": author_name,
        "author_url": author_url,
        "author_photo": author_photo,
        "excerpt": excerpt,
    }


def _webmentions_for_post(post, request=None):
    target_urls = {post.get_absolute_url()}
    if request:
        target_urls.add(request.build_absolute_uri(post.get_absolute_url()))

    mentions = (
        Webmention.objects.filter(status=Webmention.ACCEPTED)
        .filter(Q(target_post=post) | Q(target__in=target_urls))
        .only("source", "mention_type", "created_at")
    )

    replies = []
    likes = []
    reposts = []
    for mention in mentions:
        if mention.mention_type == Webmention.REPLY:
            cache_key = f"webmention:source:{mention.source}"
            payload = cache.get(cache_key)
            if payload is None:
                payload = fetch_target_from_url(mention.source) or {}
                cache.set(cache_key, payload, timeout=60 * 10)
            replies.append(_normalize_webmention_reply(mention.source, mention.created_at, payload))
        elif mention.mention_type == Webmention.REPOST:
            reposts.append(mention)
        else:
            likes.append(mention)

    replies.sort(key=lambda item: item["created_at"], reverse=True)
    likes.sort(key=lambda item: item.created_at)
    reposts.sort(key=lambda item: item.created_at)

    return replies, likes, reposts

def _split_filter_values(values):
    items = []
    for value in values:
        if value is None:
            continue
        for chunk in value.split(","):
            chunk = chunk.strip()
            if chunk:
                items.append(chunk.lower())
    seen = set()
    deduped = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        deduped.append(item)
    return deduped

def _build_filter_query(selected_kinds, selected_tags):
    params = []
    if selected_kinds:
        params.append(("kind", ",".join(selected_kinds)))
    if selected_tags:
        params.append(("tag", ",".join(selected_tags)))
    return urlencode(params, safe=",")

def posts(request):
    settings = SiteConfiguration.get_solo()
    requested_kinds = _split_filter_values(request.GET.getlist("kind"))
    selected_tags = _split_filter_values(request.GET.getlist("tag"))
    valid_kinds = {kind for kind, _ in Post.KIND_CHOICES}
    selected_kinds = [kind for kind in requested_kinds if kind in valid_kinds]
    default_kinds = [Post.ARTICLE, Post.NOTE, Post.PHOTO, Post.ACTIVITY]
    if not selected_kinds and not selected_tags:
        selected_kinds = default_kinds[:]
    filter_query = _build_filter_query(selected_kinds, selected_tags)
    has_active_filters = bool(requested_kinds or selected_tags)

    query_set = (
        Post.objects.select_related("author")
        .prefetch_related("author__hcards", "tags")
        .exclude(published_on__isnull=True)
        .filter(deleted=False)
        .order_by("-published_on")
    )
    if selected_kinds:
        query_set = query_set.filter(kind__in=selected_kinds)
    for tag in selected_tags:
        query_set = query_set.filter(tags__tag=tag)
    query_set = query_set.distinct()

    paginator = Paginator(query_set, 10)
    page_number = request.GET.get("page")

    try:
        posts = paginator.page(page_number)
    except PageNotAnInteger:
        posts = paginator.page(1)
    except EmptyPage:
        posts = paginator.page(paginator.num_pages)

    has_activity = False
    for post in posts:
        if post.kind == Post.ACTIVITY:
            has_activity = True
            post.activity = _activity_from_mf2(post)
        elif post.kind in (Post.LIKE, Post.REPLY, Post.REPOST):
            post.interaction = _interaction_payload(post, request=request)

    return render(
        request,
        'blog/posts.html',
        {
            "posts": posts,
            "post_kinds": Post.KIND_CHOICES,
            "selected_kinds": selected_kinds,
            "selected_tags": selected_tags,
            "default_kinds": default_kinds,
            "filter_query": filter_query,
            "feed_filter_query": filter_query,
            "has_active_filters": has_active_filters,
            "has_activity": has_activity,
            "og_title": f"{settings.title} posts" if settings.title else "Posts",
            "og_description": settings.tagline,
            "og_url": request.build_absolute_uri(),
        },
    )

def posts_by_tag(request, tag):
    tag = get_object_or_404(Tag, tag=tag)
    posts_url = reverse("posts").rstrip("/")
    target = f"{posts_url}?{urlencode({'tag': tag.tag})}"
    return redirect(target, permanent=True)

def tag_suggestions(request):
    query = request.GET.get("q", "").strip()
    if not query:
        return JsonResponse({"tags": []})

    suggestions = (
        Tag.objects.filter(tag__icontains=query)
        .annotate(post_count=Count("post"))
        .order_by("-post_count", "tag")
        .values_list("tag", flat=True)[:8]
    )
    return JsonResponse({"tags": list(suggestions)})

def post(request, slug):
    post = get_object_or_404(
        Post.objects.select_related("author").prefetch_related(
            "author__hcards",
            "tags",
            "attachments__asset",
        ),
        slug=slug,
        deleted=False,
    )
    if not post.is_published() and not request.user.is_authenticated:
        raise Http404

    activity = _activity_from_mf2(post) if post.kind == Post.ACTIVITY else None
    if post.kind in (Post.LIKE, Post.REPLY, Post.REPOST):
        post.interaction = _interaction_payload(post, request=request)
    activity_photos = list(post.photo_attachments) if post.kind == Post.ACTIVITY else []
    webmention_replies, webmention_likes, webmention_reposts = _webmentions_for_post(post, request=request)
    og_image = ""
    og_image_alt = ""
    if activity_photos:
        og_image = activity_photos[0].asset.file.url
        og_image_alt = activity_photos[0].asset.alt_text or ""
    else:
        og_image, og_image_alt = first_attachment_image_url(post.attachments.all())

    return render(
        request,
        "blog/post.html",
        {
            "post": post,
            "activity": activity,
            "activity_photos": activity_photos,
            "webmention_replies": webmention_replies,
            "webmention_likes": webmention_likes,
            "webmention_reposts": webmention_reposts,
            "webmention_total": len(webmention_replies) + len(webmention_likes) + len(webmention_reposts),
            "indieauth_me": request.session.get("indieauth_me"),
            "indieauth_login_url": f"{reverse('indieauth-login')}?{urlencode({'next': request.get_full_path()})}",
            "webmention_target": request.build_absolute_uri(post.get_absolute_url()),
            "webmention_next": request.get_full_path(),
            "og_title": post.title,
            "og_description": Truncator(post.summary()).chars(200, truncate="..."),
            "og_image": absolute_url(request, og_image),
            "og_image_alt": og_image_alt or post.title,
            "og_url": request.build_absolute_uri(post.get_absolute_url()),
            "og_type": "article" if post.kind == Post.ARTICLE else "website",
        },
    )


@require_POST
def delete_post(request, slug):
    guard = _staff_guard(request)
    if guard:
        return guard

    post = get_object_or_404(Post, slug=slug)
    post.deleted = True
    post.save(update_fields=["deleted"])
    return redirect(reverse("posts"))
