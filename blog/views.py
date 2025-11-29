import json
import markdown
from string import Template

from django.core.paginator import Paginator, EmptyPage, PageNotAnInteger
from django.http import HttpResponse
from django.shortcuts import redirect, render, get_object_or_404
from django.templatetags.static import static
from django.urls import reverse
from django.utils import timezone
from django.utils.text import slugify
from django.views.decorators.http import require_http_methods, require_POST

from urllib.parse import urlencode

from files.models import Attachment, File

from .models import Post, Tag


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
        Post.objects.only("title", "content", "slug", "published_on", "tags"),
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

    editing_post = None
    if slug:
        editing_post = get_object_or_404(Post, slug=slug, deleted=False)

    selected_kind = request.POST.get("kind") or (editing_post.kind if editing_post else Post.ARTICLE)
    valid_kinds = {kind for kind, _ in Post.KIND_CHOICES}
    if selected_kind not in valid_kinds:
        selected_kind = Post.ARTICLE

    existing_tags = Tag.objects.all()

    initial_tags = list(editing_post.tags.values_list("tag", flat=True)) if editing_post else []
    title_initial = editing_post.title if editing_post else ""
    content_initial = editing_post.content if editing_post else ""
    context = {
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
        "manifest_url": static("pwa/post-editor.webmanifest"),
        "form_action": reverse("post_editor_edit", kwargs={"slug": editing_post.slug}) if editing_post else reverse("post_editor"),
        "edit_mode": bool(editing_post),
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

        uploads = request.FILES.getlist("photos")
        photo_indices = request.POST.getlist("photo_indices")
        photo_alts = request.POST.getlist("photo_alts")
        photo_captions = request.POST.getlist("photo_captions")
        photo_positions = request.POST.getlist("photo_positions")
        photo_meta = {}
        for i in range(min(len(photo_indices), len(photo_positions))):
            try:
                original_index = int(photo_indices[i])
                position = int(photo_positions[i])
            except (TypeError, ValueError):
                continue

            alt_text = photo_alts[i] if i < len(photo_alts) else ""
            caption = photo_captions[i] if i < len(photo_captions) else ""
            photo_meta[original_index] = {"position": position, "alt": alt_text, "caption": caption}

        existing_ids = request.POST.getlist("existing_ids")
        existing_alts = request.POST.getlist("existing_alts")
        existing_captions = request.POST.getlist("existing_captions")
        existing_positions = request.POST.getlist("existing_positions")
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

        errors = []
        if selected_kind == Post.LIKE and not context["like_of_value"]:
            errors.append("Provide a URL for the like.")
        if selected_kind == Post.REPOST and not context["repost_of_value"]:
            errors.append("Provide a URL for the repost.")
        if selected_kind == Post.REPLY and not context["in_reply_to_value"]:
            errors.append("Provide a URL for the reply.")
        if selected_kind in (Post.ARTICLE, Post.NOTE) and not context["content_value"]:
            errors.append("Content is required for this post type.")
        if selected_kind == Post.PHOTO and not (context["content_value"] or uploads or editing_post and editing_post.attachments.exists()):
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
        if not editing_post:
            post.published_on = timezone.now()
        post.save()

        if all_tags:
            tags_to_assign = []
            for slug in all_tags:
                tag, _ = Tag.objects.get_or_create(tag=slug)
                tags_to_assign.append(tag)
            post.tags.set(tags_to_assign)

        if editing_post and existing_meta:
            for attachment in post.attachments.select_related("asset"):
                meta = existing_meta.get(attachment.asset.id)
                if not meta:
                    continue
                asset = attachment.asset
                asset.alt_text = meta.get("alt", "")
                asset.caption = meta.get("caption", "")
                asset.save(update_fields=["alt_text", "caption"])
                attachment.sort_order = meta.get("position", attachment.sort_order)
                attachment.save(update_fields=["sort_order"])

        for index, upload in enumerate(uploads):
            meta = photo_meta.get(index, {})
            asset = File.objects.create(
                kind=File.IMAGE,
                file=upload,
                owner=request.user,
                alt_text=meta.get("alt", ""),
                caption=meta.get("caption", ""),
            )
            sort_order = meta.get("position", index)
            Attachment.objects.create(content_object=post, asset=asset, role="photo", sort_order=sort_order)

        return redirect(post.get_absolute_url())

    return render(request, "blog/editor.html", context)


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
