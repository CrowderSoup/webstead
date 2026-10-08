"""Post tools: search, read, draft, edit, publish, delete, and revisions.

All writes go through ``blog.services``, so they get the same rules,
revisions, and side effects as Micropub and the admin.
"""
from __future__ import annotations

from datetime import datetime, time
from urllib.parse import urlparse

from django.core.exceptions import ObjectDoesNotExist
from django.db.models import F, Q
from django.urls import Resolver404, resolve
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime
from django.utils.text import slugify

from blog import services
from blog.models import Post, PostRevision

from . import DESTRUCTIVE, READ_ONLY, WRITE, ToolError, check_confirm_token, make_confirm_token, tool

# Kinds an agent can create. Activities come from the Strava importer, and
# events/RSVPs have no structured fields yet.
CREATABLE_KINDS = ["note", "article", "photo", "reply", "like", "repost", "bookmark", "checkin"]
STATUSES = [services.DRAFT, services.SCHEDULED, services.PUBLISHED, services.DELETED]
UPDATABLE_PROPERTIES = ["content", "name", "category", "location", "photo", "post-status"]

POST_REF = {
    "id": {"type": "integer", "description": "Post id (from search_posts or get_post)."},
    "url": {"type": "string", "description": "The post's URL on this site. Use instead of id."},
}
PHOTOS = {
    "type": "array",
    "description": (
        "Photo URLs: ones returned by upload_media, or public image URLs to fetch. "
        'Each item is a URL string or {"url": ..., "alt": ...}.'
    ),
    "items": {"type": ["string", "object"]},
}


# helpers ---------------------------------------------------------------


def _absolute(ctx, path: str) -> str:
    return f"{ctx.base_url}{path}"


def _post_url(ctx, post) -> str:
    return _absolute(ctx, post.get_absolute_url())


def _iso(value):
    return value.isoformat() if value else None


def _row(ctx, post) -> dict:
    status = services.post_status(post)
    row = {
        "id": post.pk,
        "url": _post_url(ctx, post),
        "title": post.title,
        "kind": post.kind,
        "status": status,
        "published": _iso(post.published_on),
        "tags": sorted(post.tags.values_list("tag", flat=True)),
    }
    preview = services.preview_url(ctx.actor, post)
    if preview:
        row["preview_url"] = preview
    return row


def _get_post(arguments: dict) -> Post:
    post_id, url = arguments.get("id"), arguments.get("url")
    if (post_id is None) == (not url):
        raise ToolError("Pass exactly one of id or url.")
    if post_id is not None:
        post = Post.objects.filter(pk=post_id).first()
        if post is None:
            raise ToolError(f"No post with id {post_id}. Use search_posts to find it.")
        return post
    try:
        match = resolve(urlparse(url).path)
    except Resolver404:
        match = None
    slug = match.kwargs.get("slug") if match and match.url_name == "post" else None
    post = Post.objects.filter(slug=slug).first() if slug else None
    if post is None:
        raise ToolError(f"{url} isn't a post on this site. Use search_posts to find it.")
    return post


def _parse_when(value: str, field: str):
    """An ISO 8601 date or datetime; naive values are in the site's time zone."""
    parsed = parse_datetime(value)
    if parsed is None:
        day = parse_date(value)
        parsed = datetime.combine(day, time()) if day else None
    if parsed is None:
        raise ToolError(f"{field} must be an ISO 8601 date or datetime, e.g. 2026-10-08T09:00:00-06:00")
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed)
    return parsed


def _require_edit_scope(ctx, post, action="edit this post"):
    """``update`` edits anything; ``draft`` only posts that are still drafts."""
    if services.post_status(post) == services.DRAFT:
        ctx.require("update", "draft", action=action)
    else:
        ctx.require("update", action=f"{action} (it isn't a draft)")


def _latest_revision_id(post):
    return post.revisions.order_by("-id").values_list("pk", flat=True).first()


def _go_live_effects(post) -> dict:
    """What going live sends: webmentions, Mastodon, Bridgy."""
    from core.models import SiteConfiguration
    from mastodon_integration.tasks import _should_syndicate
    from micropub.webmention import _bridgy_publish_targets, _extract_targets

    return {
        "webmention_targets": sorted(set(_extract_targets(post))),
        "mastodon": _should_syndicate(post),
        "bridgy": _bridgy_publish_targets(SiteConfiguration.get_solo()),
    }


def _content_error(exc: services.ContentError):
    raise ToolError(str(exc)) from exc


# read ------------------------------------------------------------------


@tool(
    name="search_posts",
    title="Search posts",
    description=(
        "Find posts by text, kind, status, tag, or date. Returns compact rows, newest first "
        "(drafts first). Deleted posts only appear with status=deleted. "
        "Drafts and scheduled posts include a preview_url that works logged out."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Text to find in the title or content."},
            "kind": {"type": "string", "description": "Post kind, e.g. note or article."},
            "status": {"type": "string", "enum": STATUSES},
            "tag": {"type": "string"},
            "since": {"type": "string", "description": "Published on or after (ISO 8601)."},
            "until": {"type": "string", "description": "Published before (ISO 8601)."},
            "limit": {"type": "integer", "minimum": 1, "maximum": 50, "description": "Default 20."},
            "cursor": {"type": "string", "description": "next_cursor from a previous call."},
        },
        "additionalProperties": False,
    },
    scopes=["read"],
    annotations=READ_ONLY,
)
def search_posts(ctx, arguments):
    posts = Post.objects.prefetch_related("tags")
    status = arguments.get("status")
    now = timezone.now()
    if status == services.DELETED:
        posts = posts.filter(deleted=True)
    else:
        posts = posts.filter(deleted=False)
        if status == services.DRAFT:
            posts = posts.filter(published_on__isnull=True)
        elif status == services.SCHEDULED:
            posts = posts.filter(published_on__gt=now)
        elif status == services.PUBLISHED:
            posts = posts.filter(published_on__lte=now)
    if arguments.get("query"):
        query = arguments["query"]
        posts = posts.filter(Q(title__icontains=query) | Q(content__icontains=query))
    if arguments.get("kind"):
        posts = posts.filter(kind=arguments["kind"])
    if arguments.get("tag"):
        posts = posts.filter(tags__tag=slugify(arguments["tag"]))
    if arguments.get("since"):
        posts = posts.filter(published_on__gte=_parse_when(arguments["since"], "since"))
    if arguments.get("until"):
        posts = posts.filter(published_on__lt=_parse_when(arguments["until"], "until"))

    limit = arguments.get("limit", 20)
    try:
        offset = int(arguments.get("cursor") or 0)
    except ValueError:
        raise ToolError("cursor must be the next_cursor value from a previous search_posts call.")
    posts = posts.order_by(F("published_on").desc(nulls_first=True), "-id").distinct()
    page = list(posts[offset : offset + limit + 1])
    rows = [_row(ctx, post) for post in page[:limit]]
    data = {"message": f"{len(rows)} post(s).", "posts": rows}
    if len(page) > limit:
        data["next_cursor"] = str(offset + limit)
    return data


@tool(
    name="get_post",
    title="Get a post",
    description=(
        "Full source of one post: its Micropub properties, status, URLs, author, photos with "
        "alt text, and Mastodon syndication state. Optionally its revision history."
    ),
    input_schema={
        "type": "object",
        "properties": {
            **POST_REF,
            "include_revisions": {"type": "boolean", "description": "Default false."},
        },
        "additionalProperties": False,
    },
    scopes=["read"],
    annotations=READ_ONLY,
)
def get_post(ctx, arguments):
    from micropub.views import _build_properties_response

    post = _get_post(arguments)
    data = _row(ctx, post)
    data["properties"] = _build_properties_response(post)
    data["author"] = post.author.get_username() if post.author else None
    data["went_live_at"] = _iso(post.went_live_at)
    data["attachments"] = [
        {"id": a.asset_id, "role": a.role, "url": ctx.absolute(a.asset.file.url), "alt": a.asset.alt_text}
        for a in post.attachments.select_related("asset").order_by("sort_order", "id")
    ]
    try:
        mastodon_post = post.mastodon_post
    except ObjectDoesNotExist:
        mastodon_post = None
    data["mastodon"] = {
        "override": post.mastodon_syndicate,
        "will_syndicate": _go_live_effects(post)["mastodon"],
        "tooted": mastodon_post.mastodon_url if mastodon_post else None,
    }
    if arguments.get("include_revisions"):
        data["revisions"] = _revision_rows(post, 20)
    data["message"] = f"{post.kind} {post.pk} ({data['status']})."
    return data


# write -----------------------------------------------------------------


@tool(
    name="create_post",
    title="Create a post",
    description=(
        "Create a post. It's a DRAFT unless you pass status=published, which needs the "
        "`create` scope; with a draft, nothing is public or sent anywhere. Returns a "
        "preview_url the user can open logged out. Prefer drafting and letting the user "
        "publish. For a reply/like/repost/bookmark, pass the URL it responds to. "
        "Check-ins take location as a geo: URI."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": CREATABLE_KINDS},
            "content": {"type": "string", "description": "Markdown body."},
            "name": {"type": "string", "description": "Title (articles) or place name (check-ins)."},
            "tags": {"type": "array", "items": {"type": "string"}},
            "photos": PHOTOS,
            "in_reply_to": {"type": "string"},
            "like_of": {"type": "string"},
            "repost_of": {"type": "string"},
            "bookmark_of": {"type": "string"},
            "location": {"type": "string", "description": "Check-ins only: geo:LAT,LON"},
            "status": {"type": "string", "enum": [services.DRAFT, services.PUBLISHED]},
            "published_at": {
                "type": "string",
                "description": "With status=published: when to go live (ISO 8601). Future = scheduled.",
            },
            "mastodon": {
                "type": ["boolean", "null"],
                "description": "Toot it when it goes live? null = the site's default for this kind.",
            },
        },
        "required": ["kind"],
        "additionalProperties": False,
    },
    scopes=["draft", "create"],
    annotations=WRITE,
)
def create_post(ctx, arguments):
    status = arguments.get("status", services.DRAFT)
    if status == services.PUBLISHED:
        ctx.require("create", action="publish (create it as a draft instead and share the preview_url)")
    published_on = None
    if arguments.get("published_at"):
        if status != services.PUBLISHED:
            raise ToolError("published_at only applies with status=published.")
        published_on = _parse_when(arguments["published_at"], "published_at")

    kind = arguments["kind"]
    mf2 = None
    if arguments.get("location"):
        if kind != "checkin":
            raise ToolError("location only applies to check-ins (kind=checkin).")
        geo = services.parse_geo_uri(arguments["location"])
        if not geo:
            raise ToolError("location must be a geo: URI like geo:39.7392,-104.9903")
        checkin = {"latitude": geo["latitude"], "longitude": geo["longitude"]}
        if arguments.get("name"):
            checkin["name"] = arguments["name"]
        mf2 = {"checkin": checkin}
    for field, needed_by in (("in_reply_to", "reply"), ("like_of", "like"), ("repost_of", "repost"), ("bookmark_of", "bookmark")):
        if kind == needed_by and not arguments.get(field):
            raise ToolError(f"A {kind} needs {field}.")
    if kind in ("note", "article") and not (arguments.get("content") or "").strip():
        raise ToolError(f"A {kind} needs content.")

    try:
        post = services.create_post(
            ctx.actor,
            kind=kind,
            content=arguments.get("content", ""),
            name=arguments.get("name"),
            tags=arguments.get("tags", ()),
            photos=arguments.get("photos", ()),
            status=status,
            published_on=published_on,
            like_of=arguments.get("like_of", ""),
            repost_of=arguments.get("repost_of", ""),
            in_reply_to=arguments.get("in_reply_to", ""),
            bookmark_of=arguments.get("bookmark_of", ""),
            mf2=mf2,
            mastodon_syndicate=arguments.get("mastodon"),
        )
    except services.ContentError as exc:
        _content_error(exc)

    data = _row(ctx, post)
    data["revision_id"] = _latest_revision_id(post)
    if data["status"] == services.DRAFT:
        data["message"] = "Draft created. Nothing is public yet; share the preview_url."
    elif data["status"] == services.SCHEDULED:
        data["message"] = f"Scheduled for {data['published']}. It goes live (and sends) then."
        data["on_go_live"] = _go_live_effects(post)
    else:
        data["message"] = "Published."
        data["sent"] = _go_live_effects(post)
    return data


@tool(
    name="update_post",
    title="Update a post",
    description=(
        "Change a post with Micropub update semantics. `replace`, `add`, and `delete` map "
        f"properties ({', '.join(UPDATABLE_PROPERTIES)}) to lists of values; `delete` can also "
        "be a list of property names to clear. Example: "
        '{"id": 12, "replace": {"content": ["New text"]}, "add": {"category": ["travel"]}}. '
        "Every change is saved as a revision and can be reverted. Editing a live post "
        "re-sends its webmentions."
    ),
    input_schema={
        "type": "object",
        "properties": {
            **POST_REF,
            "replace": {"type": "object"},
            "add": {"type": "object"},
            "delete": {"type": ["object", "array"]},
        },
        "additionalProperties": False,
    },
    scopes=["update", "draft"],
    annotations=WRITE,
)
def update_post(ctx, arguments):
    post = _get_post(arguments)
    if post.deleted:
        raise ToolError("That post is deleted. Restore it with undelete_post first.")
    replace = arguments.get("replace") or {}
    add = arguments.get("add") or {}
    delete = arguments.get("delete") or {}
    if isinstance(delete, list):
        delete = {name: [] for name in delete}
    if not (replace or add or delete):
        raise ToolError("Nothing to change: pass replace, add, or delete.")

    for op_name, op in (("replace", replace), ("add", add), ("delete", delete)):
        for prop, values in op.items():
            if prop not in UPDATABLE_PROPERTIES:
                raise ToolError(f"{op_name}.{prop} isn't supported. Properties: {', '.join(UPDATABLE_PROPERTIES)}.")
            if not isinstance(values, list):
                raise ToolError(f"{op_name}.{prop} must be a list, e.g. [\"value\"].")
    if ("post-status" in add) or ("post-status" in delete):
        raise ToolError("post-status can only be replaced. Or use publish_post / unpublish_post.")

    _require_edit_scope(ctx, post)
    new_status = (replace.get("post-status") or [None])[0]
    if new_status and new_status != services.post_status(post):
        ctx.require("create", action=f"change a post's status to {new_status}")

    try:
        services.update_post(ctx.actor, post, replace=replace, add=add, delete=delete)
    except services.ContentError as exc:
        _content_error(exc)

    post.refresh_from_db()
    data = _row(ctx, post)
    data["revision_id"] = _latest_revision_id(post)
    changed = ", ".join(sorted({*replace, *add, *delete}))
    data["message"] = f"Updated {changed}. Revert with revert_post(revision_id={data['revision_id']}) to undo."
    return data


@tool(
    name="publish_post",
    title="Publish a post",
    description=(
        "Publish a draft now, or schedule it with `at` (ISO 8601, in the future). Going live "
        "sends webmentions and, per the site's settings, Mastodon and Bridgy; the result lists "
        "what will be sent. Needs the `create` scope. Only publish when the user asked to."
    ),
    input_schema={
        "type": "object",
        "properties": {**POST_REF, "at": {"type": "string", "description": "When to go live (ISO 8601)."}},
        "additionalProperties": False,
    },
    scopes=["create"],
    annotations=WRITE,
)
def publish_post(ctx, arguments):
    post = _get_post(arguments)
    if post.deleted:
        raise ToolError("That post is deleted. Restore it with undelete_post first.")
    at = _parse_when(arguments["at"], "at") if arguments.get("at") else None
    was_live = post.is_live()
    already_sent = post.went_live_at is not None

    services.set_status(ctx.actor, post, services.PUBLISHED, at=at)

    data = _row(ctx, post)
    data["revision_id"] = _latest_revision_id(post)
    effects = _go_live_effects(post)
    if data["status"] == services.SCHEDULED:
        data["message"] = f"Scheduled for {data['published']}. Nothing is sent until then."
        data["on_go_live"] = effects
    elif was_live:
        data["message"] = "It was already live. Its webmentions were re-sent; nothing else changed."
    elif already_sent:
        data["message"] = (
            "Published again. It went live before, so webmentions are re-sent but Mastodon and "
            "Bridgy won't post it a second time."
        )
    else:
        data["message"] = "Published."
        data["sent"] = effects
    return data


@tool(
    name="unpublish_post",
    title="Unpublish a post",
    description=(
        "Move a live or scheduled post back to draft. This only changes this site: webmentions "
        "and toots already sent stay sent. Needs the `create` scope."
    ),
    input_schema={"type": "object", "properties": POST_REF, "additionalProperties": False},
    scopes=["create"],
    annotations=WRITE,
)
def unpublish_post(ctx, arguments):
    post = _get_post(arguments)
    if services.post_status(post) == services.DRAFT:
        raise ToolError("That post is already a draft.")
    services.set_status(ctx.actor, post, services.DRAFT)
    data = _row(ctx, post)
    data["revision_id"] = _latest_revision_id(post)
    data["message"] = "Back to draft. Anything already sent (webmentions, toots) stays sent."
    return data


@tool(
    name="delete_post",
    title="Delete a post",
    description=(
        "Delete a post (soft delete; undelete_post restores it). Two steps: call without "
        "confirm_token to get the plan and a confirm_token, show the user, then call again "
        "with the same arguments plus confirm_token. Nothing changes on the first call."
    ),
    input_schema={
        "type": "object",
        "properties": {**POST_REF, "confirm_token": {"type": "string"}},
        "additionalProperties": False,
    },
    scopes=["delete"],
    annotations=DESTRUCTIVE,
)
def delete_post(ctx, arguments):
    post = _get_post(arguments)
    if post.deleted:
        raise ToolError("That post is already deleted.")
    if not check_confirm_token(ctx, "delete_post", arguments):
        return {
            "message": (
                f"Nothing changed yet. This would delete {post.kind} {post.pk} "
                f"({services.post_status(post)}): \"{post.title}\". It disappears from the site, "
                "its feed, and the sitemap; webmentions already sent aren't retracted. To go "
                "ahead, call delete_post again with confirm_token."
            ),
            "plan": {"action": "delete", "post": _row(ctx, post)},
            "confirm_token": make_confirm_token(ctx, "delete_post", arguments),
        }
    services.delete_post(ctx.actor, post)
    data = _row(ctx, post)
    data["revision_id"] = _latest_revision_id(post)
    data["message"] = "Deleted. undelete_post restores it."
    return data


@tool(
    name="undelete_post",
    title="Restore a deleted post",
    description="Restore a deleted post to whatever status it had.",
    input_schema={"type": "object", "properties": POST_REF, "additionalProperties": False},
    scopes=["undelete"],
    annotations=WRITE,
)
def undelete_post(ctx, arguments):
    post = _get_post(arguments)
    if not post.deleted:
        raise ToolError("That post isn't deleted.")
    services.undelete_post(ctx.actor, post)
    data = _row(ctx, post)
    data["revision_id"] = _latest_revision_id(post)
    data["message"] = f"Restored as {data['status']}."
    if data["status"] == services.PUBLISHED and post.went_live_at is None:
        data["message"] += (
            " Its scheduled time has passed, so it goes live within a minute and sends its "
            "webmentions and syndication then."
        )
    return data


# revisions -------------------------------------------------------------


def _revision_rows(post, limit):
    return [
        {
            "id": r.pk,
            "created_at": _iso(r.created_at),
            "action": r.action,
            "summary": r.change_summary,
            "by": r.actor_source,
            "client_id": r.client_id,
            "can_revert": r.snapshot is not None,
        }
        for r in post.revisions.order_by("-id")[:limit]
    ]


@tool(
    name="list_revisions",
    title="List a post's revisions",
    description=(
        "A post's change history, newest first. Each revision holds the post as it was "
        "just BEFORE that change, so revert_post(revision_id) undoes that change and "
        "everything after it."
    ),
    input_schema={
        "type": "object",
        "properties": {**POST_REF, "limit": {"type": "integer", "minimum": 1, "maximum": 100}},
        "additionalProperties": False,
    },
    scopes=["read"],
    annotations=READ_ONLY,
)
def list_revisions(ctx, arguments):
    post = _get_post(arguments)
    rows = _revision_rows(post, arguments.get("limit", 20))
    return {"message": f"{len(rows)} revision(s) of post {post.pk}.", "post_id": post.pk, "revisions": rows}


@tool(
    name="revert_post",
    title="Revert a post",
    description=(
        "Undo a change: put the post back the way it was just before revision_id. Two steps "
        "like delete_post: the first call returns what would change and a confirm_token; call "
        "again with it to apply. The revert is itself a revision, so it can be undone too."
    ),
    input_schema={
        "type": "object",
        "properties": {
            **POST_REF,
            "revision_id": {"type": "integer"},
            "confirm_token": {"type": "string"},
        },
        "required": ["revision_id"],
        "additionalProperties": False,
    },
    scopes=["update"],
    annotations=DESTRUCTIVE,
)
def revert_post(ctx, arguments):
    post = _get_post(arguments)
    revision = PostRevision.objects.filter(pk=arguments["revision_id"], post=post).first()
    if revision is None:
        raise ToolError("No such revision for this post. Use list_revisions.")
    if revision.snapshot is None:
        raise ToolError("That's the post's creation; there's nothing before it. Use delete_post instead.")

    if not check_confirm_token(ctx, "revert_post", arguments):
        current = services._snapshot(post)
        changes = sorted(k for k, v in revision.snapshot.items() if current.get(k) != v)
        return {
            "message": (
                "Nothing changed yet. "
                + (f"Reverting would change: {', '.join(changes)}. " if changes else "The post already matches that revision. ")
                + "To go ahead, call revert_post again with confirm_token."
            ),
            "plan": {"action": "revert", "revision_id": revision.pk, "changes": changes},
            "confirm_token": make_confirm_token(ctx, "revert_post", arguments),
        }
    try:
        post, missing = services.revert_to(ctx.actor, post, revision)
    except services.ContentError as exc:
        _content_error(exc)
    data = _row(ctx, post)
    data["revision_id"] = _latest_revision_id(post)
    data["message"] = f"Reverted to before revision {revision.pk}."
    if missing:
        data["missing_photo_ids"] = missing
        data["message"] += f" {len(missing)} photo(s) had been deleted from the media library and couldn't come back."
    return data
