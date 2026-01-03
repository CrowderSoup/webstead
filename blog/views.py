from django.core.paginator import Paginator, EmptyPage, PageNotAnInteger
from django.http import HttpResponse, JsonResponse
from django.shortcuts import redirect, render, get_object_or_404
from django.urls import reverse
from django.views.decorators.http import require_POST
from django.utils.text import Truncator

from urllib.parse import urlencode

from .models import Post, Tag, ActivityFlyover
from .activity import activity_from_mf2
from .tasks import enqueue_activity_flyover
from core.models import SiteConfiguration
from core.og import absolute_url, first_attachment_image_url


def _staff_guard(request):
    if not request.user.is_authenticated or not request.user.is_staff:
        return HttpResponse(status=401)
    return None

def posts(request):
    settings = SiteConfiguration.get_solo()
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
            post.activity = activity_from_mf2(post)

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
            "og_title": f"{settings.title} posts" if settings.title else "Posts",
            "og_description": settings.tagline,
            "og_url": request.build_absolute_uri(),
        },
    )

def posts_by_tag(request, tag):
    settings = SiteConfiguration.get_solo()
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
            post.activity = activity_from_mf2(post)


    return render(
        request, 
        'blog/posts_by_tag.html',
        {
            "posts": posts,
            "tag": tag,
            "has_activity": has_activity,
            "og_title": f"{settings.title} · Tag: {tag.tag}" if settings.title else f"Tag: {tag.tag}",
            "og_description": settings.tagline,
            "og_url": request.build_absolute_uri(),
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

    activity = activity_from_mf2(post) if post.kind == Post.ACTIVITY else None
    activity_photos = list(post.photo_attachments) if post.kind == Post.ACTIVITY else []
    activity_flyover = None
    if post.kind == Post.ACTIVITY and activity and activity.get("track_url"):
        activity_flyover, _ = ActivityFlyover.objects.get_or_create(post=post)
        enqueue_activity_flyover(activity_flyover)
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
            "activity_flyover": activity_flyover,
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


def activity_flyover_status(request, slug):
    post = get_object_or_404(Post, slug=slug, deleted=False)
    if post.kind != Post.ACTIVITY:
        return JsonResponse({"status": "unavailable"}, status=404)

    activity = activity_from_mf2(post)
    if not activity or not activity.get("track_url"):
        return JsonResponse({"status": "missing_gpx"}, status=404)

    flyover, _ = ActivityFlyover.objects.get_or_create(post=post)
    enqueue_activity_flyover(flyover)
    payload = {"status": flyover.status}

    if flyover.status == ActivityFlyover.READY and flyover.video:
        payload["url"] = flyover.video.file.url
    elif flyover.status == ActivityFlyover.FAILED and flyover.error_message:
        payload["error"] = flyover.error_message

    return JsonResponse(payload)
