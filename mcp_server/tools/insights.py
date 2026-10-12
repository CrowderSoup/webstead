"""Visit counts and imported Strava activities. Both are read-only."""
from __future__ import annotations

from datetime import date, datetime, timedelta

from django.db.models import Count
from django.db.models.functions import TruncDate
from django.utils import timezone

from blog import services

from . import READ_ONLY, ToolError, tool

# Issue #48 asks the admin for this clamp. The MCP tool does it now so a
# wide or future range cannot scan the whole visits table.
MAX_WINDOW_DAYS = 90
DEFAULT_WINDOW_DAYS = 30
DEFAULT_LIMIT = 20


def _parse_day(value, name):
    if not isinstance(value, str):
        raise ToolError(f"{name} must be an ISO date (YYYY-MM-DD).")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ToolError(f"{name} must be an ISO date (YYYY-MM-DD).") from None


def _as_date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        return date.fromisoformat(value[:10])
    return value


def _range(arguments):
    today = timezone.localdate()
    notes = []
    until = _parse_day(arguments["until"], "until") if arguments.get("until") else today
    since = _parse_day(arguments["since"], "since") if arguments.get("since") else None
    moved_future = False
    if until > today:
        until = today
        moved_future = True
    if since is not None and since > today:
        since = today
        moved_future = True
    if moved_future:
        notes.append("Future dates were moved to today.")
    if since is None:
        since = until - timedelta(days=DEFAULT_WINDOW_DAYS - 1)
    if since > until:
        raise ToolError("since is after until.")
    if (until - since).days + 1 > MAX_WINDOW_DAYS:
        since = until - timedelta(days=MAX_WINDOW_DAYS - 1)
        notes.append(f"The range was clamped to {MAX_WINDOW_DAYS} days.")
    return since, until, notes


def _groups(qs, group_by, since, until, limit):
    if group_by == "day":
        rows = qs.annotate(day=TruncDate("started_at")).values("day").annotate(count=Count("id"))
        counts = {_as_date(row["day"]): row["count"] for row in rows}
        groups = []
        current = since
        while current <= until:
            groups.append({"day": current.isoformat(), "count": counts.get(current, 0)})
            current += timedelta(days=1)
        return groups, False

    field = group_by
    rows = list(
        qs.values(field).annotate(count=Count("id")).order_by("-count", field)[: limit + 1]
    )
    truncated = len(rows) > limit
    return [{field: row[field], "count": row["count"]} for row in rows[:limit]], truncated


@tool(
    name="analytics",
    title="Visit counts",
    description=(
        "Count visits, grouped by path, referrer, country, or day. Bots "
        "(is_suspected_bot) are excluded unless include_bots is true. The range "
        "defaults to the last 30 days, future dates move to today, and a range "
        "longer than 90 days is clamped. Paths under /admin and /analytics are "
        "left out, same as the admin dashboard. limit caps path, referrer, and "
        "country groups (default 20); day returns every day in the range. "
        "Needs the analytics scope."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "since": {"type": "string", "description": "First day, YYYY-MM-DD. Default is 30 days before until."},
            "until": {"type": "string", "description": "Last day, YYYY-MM-DD. Default is today."},
            "group_by": {"type": "string", "enum": ["path", "referrer", "country", "day"]},
            "include_bots": {"type": "boolean"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 50},
        },
        "additionalProperties": False,
    },
    scopes=["analytics"],
    annotations=READ_ONLY,
)
def analytics(ctx, arguments):
    from analytics.models import Visit

    since, until, notes = _range(arguments)
    group_by = arguments.get("group_by", "path")
    include_bots = bool(arguments.get("include_bots", False))
    limit = arguments.get("limit", DEFAULT_LIMIT)
    visits = Visit.objects.filter(
        started_at__date__gte=since,
        started_at__date__lte=until,
    ).exclude(path__startswith="/admin").exclude(path__startswith="/analytics")
    if not include_bots:
        visits = visits.filter(is_suspected_bot=False)
    groups, truncated = _groups(visits, group_by, since, until, limit)
    total = visits.count()
    bots = "Bots included." if include_bots else "Bots excluded."
    extra = (" " + " ".join(notes)) if notes else ""
    return {
        "message": (
            f"{total} visit(s) from {since.isoformat()} to {until.isoformat()}, "
            f"grouped by {group_by}. {bots}{extra}"
        ),
        "since": since.isoformat(),
        "until": until.isoformat(),
        "group_by": group_by,
        "include_bots": include_bots,
        "total": total,
        "groups": groups,
        "truncated": truncated,
    }


@tool(
    name="strava_recent",
    title="Recent imported Strava activities",
    description=(
        "Strava activities already imported onto this site, newest import first. "
        "Each row says whether it already has a post (the importer always links one, "
        "including a deleted post). Activities that were never imported are not listed; "
        "this does not call Strava. Needs the read scope."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "limit": {"type": "integer", "minimum": 1, "maximum": 50},
        },
        "additionalProperties": False,
    },
    scopes=["read"],
    annotations=READ_ONLY,
)
def strava_recent(ctx, arguments):
    from strava_integration.models import StravaAccount, StravaActivity

    limit = arguments.get("limit", DEFAULT_LIMIT)
    connected = StravaAccount.get_active() is not None
    rows = []
    activities = (
        StravaActivity.objects.select_related("post").order_by("-imported_at", "-id")[:limit]
    )
    for activity in activities:
        post = activity.post
        rows.append(
            {
                "strava_activity_id": activity.strava_activity_id,
                "imported_at": activity.imported_at.isoformat(),
                "has_post": post is not None,
                "post_id": post.pk if post else None,
                "post_url": ctx.absolute(post.get_absolute_url()) if post else "",
                "post_status": services.post_status(post) if post else None,
                "post_kind": post.kind if post else "",
                "post_title": post.title if post else "",
                "published": post.published_on.isoformat() if post and post.published_on else None,
            }
        )
    linked = sum(1 for row in rows if row["has_post"])
    activity_word = "activity" if len(rows) == 1 else "activities"
    linked_verb = "has" if linked == 1 else "have"
    connection = "Strava is connected." if connected else "Strava is not connected."
    return {
        "message": (
            f"{len(rows)} imported Strava {activity_word}. "
            f"{linked} already {linked_verb} a post. {connection}"
        ),
        "connected": connected,
        "activities": rows,
    }
