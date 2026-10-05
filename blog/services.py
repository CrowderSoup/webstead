"""The one write path for posts.

Micropub (and later the admin, MCP and importers) create and change posts
through these functions, so they share the same rules and side effects. They
take an ``Actor`` and plain data, never a request.

Side effects (webmentions, Bridgy, Mastodon) are queued on commit and only
fire once a post is live; see ``micropub.webmention.queue_webmentions_for_post``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from django.db import transaction
from django.utils import timezone
from django.utils.text import slugify

from blog.models import Post, Tag
from files.models import Attachment, File

if TYPE_CHECKING:
    from django.contrib.auth.models import AbstractBaseUser

DRAFT = "draft"
SCHEDULED = "scheduled"
PUBLISHED = "published"
DELETED = "deleted"

GEO_URI_RE = re.compile(
    r"^geo:(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)(?:,(-?\d+(?:\.\d+)?))?(?:;.*)?$"
)


class ContentError(ValueError):
    """A change the post can't take. The message is safe to show the client."""


@dataclass(frozen=True)
class Actor:
    """Who is making a change, and through what."""

    user: AbstractBaseUser | None = None
    source: str = "system"  # "admin" | "micropub" | "mcp" | "strava" | "system"
    token_id: int | None = None  # IndieAuthAccessToken that made the call, if any
    client_id: str = ""
    # Origin of the request (e.g. "https://example.com"), used to build the
    # webmention source URL. Empty means use the site URL from settings.
    base_url: str = ""


def post_status(post: Post) -> str:
    """draft | scheduled | published | deleted"""
    if post.deleted:
        return DELETED
    if post.published_on is None:
        return DRAFT
    if post.published_on > timezone.now():
        return SCHEDULED
    return PUBLISHED


def post_url(actor: Actor, post: Post) -> str:
    if actor.base_url:
        return f"{actor.base_url.rstrip('/')}{post.get_absolute_url()}"
    from mastodon_integration.tasks import _build_canonical_url

    return _build_canonical_url(post)


def parse_geo_uri(uri: str) -> dict | None:
    if not uri:
        return None
    match = GEO_URI_RE.match(uri.strip())
    if not match:
        return None
    result = {
        "latitude": float(match.group(1)),
        "longitude": float(match.group(2)),
    }
    if match.group(3) is not None:
        result["altitude"] = float(match.group(3))
    return result


def _first(values, default=None):
    if isinstance(values, list):
        return values[0] if values else default
    return values if values is not None else default


def _default_content(kind, *, like_of="", repost_of="", in_reply_to="", bookmark_of=""):
    if kind == Post.LIKE:
        return f"Liked {like_of}"
    if kind == Post.REPOST:
        return f"Reposted {repost_of}"
    if kind == Post.REPLY:
        return f"Reply to {in_reply_to}"
    if kind == Post.BOOKMARK:
        return f"Bookmarked {bookmark_of}"
    if kind == Post.CHECKIN:
        return "Checked in"
    return ""


def _apply_categories(post, categories, *, clear_first=False):
    if clear_first:
        post.tags.clear()
    for category in categories:
        tag_slug = slugify(str(category))
        if not tag_slug:
            continue
        tag, _ = Tag.objects.get_or_create(tag=tag_slug)
        post.tags.add(tag)


def _remove_categories(post, categories):
    if categories == []:
        post.tags.clear()
        return
    for category in categories:
        tag_slug = slugify(str(category))
        if tag_slug:
            # remove() unlinks the tag from this post only; deleting through
            # the M2M queryset would delete the Tag row and strip it from
            # every post that uses it.
            post.tags.remove(*Tag.objects.filter(tag=tag_slug))


def _attach_photo_files(actor, post, photo_files):
    for uploaded in photo_files:
        asset = File.objects.create(kind=File.IMAGE, file=uploaded, owner=actor.user)
        Attachment.objects.create(content_object=post, asset=asset, role="photo")


def _attach_remote_photos(post, photos):
    """Queue downloads for photo URLs ({"url", "alt"} dicts or plain strings).

    A Markdown image string is appended to the content instead.
    """
    from micropub.tasks import download_post_photo

    content_changed = False
    for photo in photos:
        if isinstance(photo, str) and photo and not photo.startswith("<UploadedFile"):
            if photo.startswith("!["):
                post.content += f"\n{photo}\n"
                content_changed = True
                continue
            transaction.on_commit(lambda u=photo: download_post_photo.delay(post.pk, u))
        elif isinstance(photo, dict):
            url = photo.get("url")
            alt_text = photo.get("alt") or ""
            if isinstance(url, str) and url:
                transaction.on_commit(lambda u=url, a=alt_text: download_post_photo.delay(post.pk, u, a))
    if content_changed:
        post.save(update_fields=["content"])


def _remove_photos(post, urls):
    """Detach photos by URL; an empty list removes them all."""
    normalized_urls = set()
    for item in urls:
        if isinstance(item, dict):
            item = item.get("url")
        if isinstance(item, str) and item:
            normalized_urls.add(item)

    attachments = post.attachments.filter(asset__kind=File.IMAGE).select_related("asset")
    for attachment in attachments:
        asset = attachment.asset
        if urls == [] or asset.file.url in normalized_urls:
            attachment.delete()
            if not asset.is_in_use():
                asset.delete()


def _queue_side_effects(actor, post, *, include_bridgy=True):
    """Queue webmentions and syndication on commit; a no-op until the post is live."""
    from core.models import SiteConfiguration
    from micropub.webmention import queue_webmentions_for_post

    source_url = post_url(actor, post)
    settings_obj = SiteConfiguration.get_solo()
    transaction.on_commit(
        lambda: queue_webmentions_for_post(
            post,
            source_url,
            include_bridgy=include_bridgy,
            settings_obj=settings_obj,
        )
    )


def _set_author(actor, post):
    if post.author_id is None and actor.user is not None:
        post.author = actor.user


def _resolve_published_on(status, published_on, current=None):
    if status == DRAFT:
        return None
    if status != PUBLISHED:
        raise ContentError(f"status must be one of: {DRAFT}, {PUBLISHED}")
    if published_on is not None:
        return published_on
    # keep the date of a post that's already out; otherwise publish now
    if current is not None and current <= timezone.now():
        return current
    return timezone.now()


def create_post(
    actor: Actor,
    *,
    kind: str,
    content: str = "",
    name: str | None = None,
    tags=(),
    photos=(),
    photo_files=(),
    status: str = DRAFT,
    published_on=None,
    like_of: str = "",
    repost_of: str = "",
    in_reply_to: str = "",
    bookmark_of: str = "",
    mf2: dict | None = None,
    mastodon_syndicate: bool | None = None,
) -> Post:
    """Create a post. A draft unless ``status="published"``.

    With ``status="published"``, ``published_on`` in the future schedules it.
    ``photos`` are URLs (or {"url", "alt"} dicts) to download; ``photo_files``
    are uploaded files.
    """
    if kind not in dict(Post.KIND_CHOICES):
        raise ContentError(f"kind must be one of: {', '.join(dict(Post.KIND_CHOICES))}")

    with transaction.atomic():
        post = Post(
            title=name or "",
            content=content
            or _default_content(
                kind,
                like_of=like_of,
                repost_of=repost_of,
                in_reply_to=in_reply_to,
                bookmark_of=bookmark_of,
            ),
            kind=kind,
            published_on=_resolve_published_on(status, published_on),
            like_of=like_of or "",
            repost_of=repost_of or "",
            in_reply_to=in_reply_to or "",
            bookmark_of=bookmark_of or "",
            mf2=mf2 or {},
            mastodon_syndicate=mastodon_syndicate,
        )
        _set_author(actor, post)
        post.save()

        _apply_categories(post, tags)
        _attach_photo_files(actor, post, photo_files)
        _attach_remote_photos(post, photos)
        _queue_side_effects(actor, post)
    return post


def update_post(actor: Actor, post: Post, *, replace=None, add=None, delete=None, photo_files=()) -> Post:
    """Apply a Micropub-style update.

    ``replace`` and ``add`` map property names to lists of values; ``delete``
    maps them to values to remove (an empty list removes the property).
    Supported properties: content, name, category, location (check-ins only),
    photo, and post-status (replace only).
    """
    replace = replace or {}
    add = add or {}
    delete = delete or {}

    if "location" in replace and post.kind != Post.CHECKIN:
        raise ContentError("location is only editable on check-in posts")

    new_status = _first(replace.get("post-status")) if "post-status" in replace else None
    if new_status is not None and new_status not in (DRAFT, PUBLISHED):
        raise ContentError(f"post-status must be one of: {DRAFT}, {PUBLISHED}")

    with transaction.atomic():
        if "content" in replace:
            new_content = _first(replace["content"])
            if new_content is not None:
                post.content = new_content

        if "name" in replace:
            new_name = _first(replace["name"])
            if new_name:
                post.title = new_name

        if "category" in replace:
            _apply_categories(post, replace["category"], clear_first=True)
        if "category" in add:
            _apply_categories(post, add["category"])
        if "category" in delete:
            _remove_categories(post, delete["category"])

        if "location" in replace:
            new_location = _first(replace["location"])
            geo = parse_geo_uri(new_location) if new_location else None
            if geo:
                if not isinstance(post.mf2, dict):
                    post.mf2 = {}
                checkin = {"latitude": geo["latitude"], "longitude": geo["longitude"]}
                if post.title:
                    checkin["name"] = post.title
                post.mf2["checkin"] = checkin

        if "photo" in delete:
            _remove_photos(post, delete["photo"])
        if "photo" in replace:
            _remove_photos(post, [])
            _attach_photo_files(actor, post, photo_files)
            _attach_remote_photos(post, replace["photo"])
        elif "photo" in add:
            _attach_photo_files(actor, post, photo_files)
        if "photo" in add:
            _attach_remote_photos(post, add["photo"])

        if new_status is not None:
            post.published_on = _resolve_published_on(new_status, None, post.published_on)

        _set_author(actor, post)
        post.save()
        _queue_side_effects(actor, post)
    return post


def set_status(actor: Actor, post: Post, status: str, *, at=None) -> Post:
    """Move a post to draft, or publish it now (or at ``at``, to schedule it).

    Going back to draft is local only: webmentions and toots already sent
    stay sent, and publishing again won't send them a second time.
    """
    with transaction.atomic():
        post.published_on = _resolve_published_on(status, at, post.published_on)
        _set_author(actor, post)
        post.save()
        _queue_side_effects(actor, post)
    return post


def delete_post(actor: Actor, post: Post) -> Post:
    if not post.deleted:
        post.deleted = True
        post.save(update_fields=["deleted"])
    return post


def undelete_post(actor: Actor, post: Post) -> Post:
    """Restore a soft-deleted post.

    A scheduled post restored after its time goes live on the next
    ``publish_due_posts`` run and sends its webmentions and syndication then.
    """
    if post.deleted:
        post.deleted = False
        post.save(update_fields=["deleted"])
    return post


def create_media(actor: Actor, upload, *, alt: str = "") -> File:
    return File.objects.create(kind=File.IMAGE, file=upload, alt_text=alt[:255], owner=actor.user)
