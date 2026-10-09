"""The one write path for posts.

Micropub (and later the admin, MCP and importers) create and change posts
through these functions, so they share the same rules and side effects. They
take an ``Actor`` and plain data, never a request.

Side effects (webmentions, Bridgy, Mastodon) are queued on commit and only
fire once a post is live; see ``micropub.webmention.queue_webmentions_for_post``.

Every change writes a ``PostRevision`` holding the post as it was just before
the change, so ``revert_to`` can undo it. Undo is local only: webmentions and
toots that already went out stay sent.
"""
from __future__ import annotations

import re
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import urlencode, urlparse

from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.utils.text import slugify

from blog.models import Post, PostRevision, Tag
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


def preview_url(actor: Actor, post: Post) -> str | None:
    """A signed link that shows the post logged-out until it expires, or None
    if the post is already live (its plain URL works) or deleted."""
    if post.deleted or post.is_live():
        return None
    from blog.previews import make_token

    return f"{post_url(actor, post)}?{urlencode({'preview': make_token(post)})}"


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


def _local_file_for_url(url):
    """The media-library File a URL points at, if it's one of ours.

    Uploads are stored as ``uploads/<kind>/<yyyy>/<mm>/<uuid>.<ext>``, so the
    path from ``/uploads/`` on names the file whatever host serves it.
    """
    path = urlparse(url).path
    index = path.find("/uploads/")
    if index == -1:
        return None
    return File.objects.filter(file=path[index + 1 :]).first()


def _attach_remote_photos(post, photos):
    """Attach photo URLs ({"url", "alt"} dicts or plain strings).

    URLs of files already in the media library (e.g. from the media
    endpoint) are attached directly; others are downloaded after commit. A
    Markdown image string is appended to the content instead.
    """
    from micropub.tasks import download_post_photo

    content_changed = False
    next_order = post.attachments.count()
    for photo in photos:
        if isinstance(photo, str) and photo.startswith("!["):
            post.content += f"\n{photo}\n"
            content_changed = True
            continue
        if isinstance(photo, str) and photo and not photo.startswith("<UploadedFile"):
            url, alt_text = photo, ""
        elif isinstance(photo, dict) and isinstance(photo.get("url"), str) and photo["url"]:
            url, alt_text = photo["url"], photo.get("alt") or ""
        else:
            continue

        local = _local_file_for_url(url)
        if local is not None:
            if alt_text and not local.alt_text:
                local.alt_text = alt_text[:255]
                local.save(update_fields=["alt_text"])
            Attachment.objects.create(content_object=post, asset=local, role="photo", sort_order=next_order)
            next_order += 1
        elif alt_text:
            transaction.on_commit(lambda u=url, a=alt_text: download_post_photo.delay(post.pk, u, a))
        else:
            transaction.on_commit(lambda u=url: download_post_photo.delay(post.pk, u))
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
        # Detach only: revisions still point at the asset, so reverting can
        # bring the photo back.
        if urls == [] or attachment.asset.file.url in normalized_urls:
            attachment.delete()


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


SNAPSHOT_FIELDS = (
    "title",
    "slug",
    "kind",
    "content",
    "mf2",
    "deleted",
    "like_of",
    "repost_of",
    "in_reply_to",
    "bookmark_of",
    "mastodon_syndicate",
)


def _snapshot(post: Post) -> dict:
    """Everything ``_restore`` needs to put the post back the way it is now."""
    snapshot = {field: getattr(post, field) for field in SNAPSHOT_FIELDS}
    snapshot["published_on"] = post.published_on.isoformat() if post.published_on else None
    snapshot["tags"] = sorted(post.tags.values_list("tag", flat=True))
    snapshot["attachments"] = [
        {"asset_id": a.asset_id, "role": a.role, "sort_order": a.sort_order}
        for a in post.attachments.order_by("sort_order", "id")
    ]
    return snapshot


def _record(actor: Actor, post: Post, action: str, summary: str = "", *, snapshot=True) -> PostRevision:
    """Write a revision holding the post's current (pre-change) state."""
    return PostRevision.objects.create(
        post=post,
        action=action,
        change_summary=summary[:255],
        snapshot=_snapshot(post) if snapshot else None,
        actor_source=actor.source,
        actor_user=actor.user,
        token_id=actor.token_id,
        client_id=actor.client_id,
    )


def _summarize_update(replace, add, delete) -> str:
    parts = [
        f"{op} {', '.join(props)}"
        for op, props in (("replace", replace), ("add", add), ("delete", delete))
        if props
    ]
    return "; ".join(parts)


def _restore(post: Post, snapshot: dict) -> list[int]:
    """Put a snapshot back onto the post. Returns asset ids that no longer exist."""
    if Post.objects.exclude(pk=post.pk).filter(slug=snapshot["slug"]).exists():
        raise ContentError(f"another post now uses the slug {snapshot['slug']!r}")

    for field in SNAPSHOT_FIELDS:
        setattr(post, field, snapshot[field])
    post.published_on = parse_datetime(snapshot["published_on"]) if snapshot["published_on"] else None
    post.save()

    post.tags.set([Tag.objects.get_or_create(tag=slug)[0] for slug in snapshot["tags"]])

    wanted = snapshot["attachments"]
    existing = set(File.objects.filter(pk__in=[a["asset_id"] for a in wanted]).values_list("pk", flat=True))
    post.attachments.all().delete()
    for item in wanted:
        if item["asset_id"] in existing:
            Attachment.objects.create(
                content_object=post,
                asset_id=item["asset_id"],
                role=item["role"],
                sort_order=item["sort_order"],
            )
    return [a["asset_id"] for a in wanted if a["asset_id"] not in existing]


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


def _requested_slug(post: Post, raw: str | None) -> str:
    """Normalize a caller-chosen slug so it fits ``Post.slug``.

    Returns "" when there is nothing usable (missing, blank, or all
    punctuation). ``Post.save`` then keeps the title-plus-timestamp slug.
    A value that is already taken becomes unique via ``Post._unique_slug``.
    """
    base = slugify(raw or "")
    if not base:
        return ""
    max_length = Post._meta.get_field("slug").max_length
    base = base[:max_length].strip("-")
    if not base:
        return ""
    slug = post._unique_slug(base)
    # ``_unique_slug`` appends ``-2``, ``-3``, … and does not know the column
    # width. Shorten the base until the de-duped value fits.
    while len(slug) > max_length:
        overflow = len(slug) - max_length
        base = base[:-overflow].strip("-")
        if not base:
            return ""
        slug = post._unique_slug(base)
    return slug


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
    slug: str | None = None,
) -> Post:
    """Create a post. A draft unless ``status="published"``.

    With ``status="published"``, ``published_on`` in the future schedules it.
    ``photos`` are URLs (or {"url", "alt"} dicts) to download; ``photo_files``
    are uploaded files. ``slug`` is optional; blank or all-punctuation input
    leaves the usual title-and-timestamp slug.
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
        chosen = _requested_slug(post, slug)
        if chosen:
            post.slug = chosen
        post.save()
        _record(actor, post, PostRevision.CREATE, f"create {kind} as {status}", snapshot=False)

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
        _record(actor, post, PostRevision.UPDATE, _summarize_update(replace, add, delete))

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


@contextmanager
def saving(actor: Actor, post: Post, *, summary: str = "", include_bridgy: bool = True):
    """Run a hand-rolled change to ``post`` (e.g. the admin form) as one
    service call: a revision of the before-state, author defaulting, one
    transaction, and go-live side effects on commit.

    ``post`` may be unsaved (a new post) or already modified in memory: the
    before-state is read from the database, not from ``post``, because a
    ModelForm writes the submitted values onto its instance during
    ``is_valid()``. The body may save ``post`` and its tags and attachments
    as often as it needs. Raising inside the block rolls everything back.
    """
    is_new = post.pk is None
    with transaction.atomic():
        if not is_new:
            _record(actor, Post.objects.get(pk=post.pk), PostRevision.UPDATE, summary)
        yield post
        _set_author(actor, post)
        post.save()
        if is_new:
            _record(actor, post, PostRevision.CREATE, summary, snapshot=False)
        _queue_side_effects(actor, post, include_bridgy=include_bridgy)


def set_status(actor: Actor, post: Post, status: str, *, at=None) -> Post:
    """Move a post to draft, or publish it now (or at ``at``, to schedule it).

    Going back to draft is local only: webmentions and toots already sent
    stay sent, and publishing again won't send them a second time.
    """
    with transaction.atomic():
        _record(actor, post, PostRevision.STATUS, f"set status to {status}" + (f" at {at.isoformat()}" if at else ""))
        post.published_on = _resolve_published_on(status, at, post.published_on)
        _set_author(actor, post)
        post.save()
        _queue_side_effects(actor, post)
    return post


def delete_post(actor: Actor, post: Post) -> Post:
    if not post.deleted:
        with transaction.atomic():
            _record(actor, post, PostRevision.DELETE, "delete")
            post.deleted = True
            post.save(update_fields=["deleted"])
    return post


def undelete_post(actor: Actor, post: Post) -> Post:
    """Restore a soft-deleted post.

    A scheduled post restored after its time goes live on the next
    ``publish_due_posts`` run and sends its webmentions and syndication then.
    """
    if post.deleted:
        with transaction.atomic():
            _record(actor, post, PostRevision.UNDELETE, "undelete")
            post.deleted = False
            post.save(update_fields=["deleted"])
    return post


def revert_to(actor: Actor, post: Post, revision: PostRevision) -> tuple[Post, list[int]]:
    """Put the post back the way it was just before ``revision``'s change.

    The revert is itself a revision, so it can be undone too. Returns the post
    and the ids of any photos that have since been deleted and couldn't come
    back. Restoring a live state re-sends webmentions as any edit to a live
    post does; a post that already went live doesn't toot or hit Bridgy again.
    """
    if revision.post_id != post.pk:
        raise ContentError("that revision belongs to a different post")
    if revision.snapshot is None:
        raise ContentError("that revision is the post's creation, so there's nothing before it; delete the post instead")

    with transaction.atomic():
        _record(actor, post, PostRevision.REVERT, f"revert to before revision {revision.pk}")
        missing_assets = _restore(post, revision.snapshot)
        _set_author(actor, post)
        post.save()
        _queue_side_effects(actor, post)
    return post, missing_assets


def create_media(actor: Actor, upload, *, alt: str = "") -> File:
    return File.objects.create(kind=File.IMAGE, file=upload, alt_text=alt[:255], owner=actor.user)
