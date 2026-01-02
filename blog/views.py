from django.core.paginator import Paginator, EmptyPage, PageNotAnInteger
from django.http import HttpResponse
from django.shortcuts import redirect, render, get_object_or_404
from django.urls import reverse
from django.views.decorators.http import require_POST

from urllib.parse import urlencode

from .models import Post, Tag


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

def posts(request):
    requested_kinds = request.GET.getlist("kind")
    valid_kinds = {kind for kind, _ in Post.KIND_CHOICES}
    selected_kinds = [kind for kind in requested_kinds if kind in valid_kinds]
    feed_kinds_query = urlencode([("kind", kind) for kind in selected_kinds])
    selected_kinds = selected_kinds or [Post.ARTICLE]

    query_set = (
        Post.objects.select_related("author")
        .prefetch_related("author__hcards")
        .exclude(published_on__isnull=True)
        .filter(deleted=False)
        .order_by("-published_on")
    )
    query_set = query_set.filter(kind__in=selected_kinds)

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

    return render(
        request,
        'blog/posts.html',
        {
            "posts": posts,
            "post_kinds": Post.KIND_CHOICES,
            "selected_kinds": selected_kinds,
            "selected_kinds_query": urlencode([("kind", kind) for kind in selected_kinds]),
            "feed_kinds_query": feed_kinds_query,
            "has_activity": has_activity,
        },
    )

def posts_by_tag(request, tag):
    tag = get_object_or_404(Tag, tag=tag)
    query_set = (
        Post.objects.select_related("author")
        .prefetch_related("author__hcards")
        .exclude(published_on__isnull=True)
        .filter(tags=tag, deleted=False)
        .order_by("-published_on")
    )
    paginator = Paginator(query_set, 10)
    page_number = request.GET.get("page")

    try:
        posts = paginator.page(page_number)
    except PageNotAnInteger:
        posts = paginator.page(1)
    except EmptyPage:
        posts.paginator.page(paginator.num_pages)

    has_activity = False
    for post in posts:
        if post.kind == Post.ACTIVITY:
            has_activity = True
            post.activity = _activity_from_mf2(post)


    return render(
        request, 
        'blog/posts_by_tag.html',
        {
            "posts": posts,
            "tag": tag,
            "has_activity": has_activity,
        }
    )

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

    activity = _activity_from_mf2(post) if post.kind == Post.ACTIVITY else None
    activity_photos = list(post.photo_attachments) if post.kind == Post.ACTIVITY else []

    return render(
        request,
        "blog/post.html",
        {
            "post": post,
            "activity": activity,
            "activity_photos": activity_photos,
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
