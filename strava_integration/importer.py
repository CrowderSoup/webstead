"""
Shared Strava-activity -> blog.Post import pipeline.

Used by both the historical-import admin view (site_admin/views.py:strava_import)
and the webhook/reconciliation Celery tasks (strava_integration/tasks.py), so
the two entry points can't drift apart.
"""

import logging

from django.core.files.base import ContentFile
from django.db import IntegrityError, transaction
from django.utils import timezone as dj_timezone
from django.utils.dateparse import parse_datetime

from blog.models import Post
from core.models import SiteConfiguration
from files.gpx import GpxAnonymizeError, GpxAnonymizeOptions, anonymize_gpx
from files.models import Attachment, File

from . import client
from .gpx import streams_to_gpx
from .models import StravaActivity

logger = logging.getLogger(__name__)

METERS_PER_MILE = 1609.34
FEET_PER_METER = 3.28084

_ACTIVITY_VERBS = {
    "Run": "Ran",
    "TrailRun": "Ran",
    "Ride": "Rode",
    "GravelRide": "Rode",
    "MountainBikeRide": "Rode",
    "VirtualRide": "Rode",
    "EBikeRide": "Rode",
    "Walk": "Walked",
    "Hike": "Hiked",
    "Swim": "Swam",
    "Rowing": "Rowed",
    "Kayaking": "Kayaked",
    "Ski": "Skied",
    "AlpineSki": "Skied",
    "BackcountrySki": "Skied",
    "NordicSki": "Skied",
    "Snowboard": "Snowboarded",
}


def _parse_start_date(activity: dict):
    raw = activity.get("start_date") or activity.get("start_date_local")
    parsed = parse_datetime(raw) if raw else None
    return parsed or dj_timezone.now()


def _activity_verb(activity_type: str) -> str:
    if not activity_type:
        return "Did an activity"
    return _ACTIVITY_VERBS.get(activity_type, f"Did a {activity_type}")


def _format_duration(seconds) -> str:
    seconds = int(seconds or 0)
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def _activity_summary(activity: dict, *, units: str) -> str:
    """
    Plain-text summary used as Post.content when Strava didn't supply a
    description (the common case). Post.content is rendered through
    markdown.Markdown(), so this must stay Markdown-safe plain text, not HTML.
    """
    metric = units == SiteConfiguration.ACTIVITY_UNITS_METRIC
    distance_unit = "km" if metric else "mi"

    verb = _activity_verb(activity.get("sport_type") or activity.get("type") or "")
    distance_m = activity.get("distance") or 0
    moving_time = activity.get("moving_time") or 0
    elevation_m = activity.get("total_elevation_gain") or 0

    clause = verb
    distance_val = 0.0
    if distance_m:
        distance_val = (distance_m / 1000) if metric else (distance_m / METERS_PER_MILE)
        clause += f" {distance_val:.1f} {distance_unit}"
    if moving_time:
        clause += f" in {_format_duration(moving_time)}"
        if distance_val:
            pace_seconds = moving_time / distance_val
            clause += f" ({_format_duration(round(pace_seconds))}/{distance_unit} pace)"

    clauses = [clause]
    if elevation_m:
        elevation_val = elevation_m if metric else (elevation_m * FEET_PER_METER)
        elevation_unit = "m" if metric else "ft"
        clauses.append(f"{elevation_val:.0f} {elevation_unit} elevation gain")

    return ", ".join(clauses) + "."


# Extra h-activity properties beyond core/name/track. Non-standard --
# "activity" isn't a recognized post-type-discovery type -- so these are
# additive/ignorable, x-prefixed per the mp-/x- vendor-extension convention,
# and stay in Strava's native SI units (meters, m/s) regardless of the
# site's activity_units display setting. See docs/microsub-extensions.md.
_MF2_NUMERIC_FIELDS = (
    ("distance", "x-distance"),
    ("moving_time", "x-moving-time"),
    ("elapsed_time", "x-elapsed-time"),
    ("total_elevation_gain", "x-total-elevation-gain"),
    ("average_speed", "x-average-speed"),
    ("max_speed", "x-max-speed"),
)
_MF2_HEARTRATE_FIELDS = (
    ("average_heartrate", "x-average-heartrate"),
    ("max_heartrate", "x-max-heartrate"),
)
_MF2_LATLNG_FIELDS = (
    ("start_latlng", "x-start-latlng"),
    ("end_latlng", "x-end-latlng"),
)


def _build_mf2(activity: dict, track_url: str = "") -> dict:
    properties = {}
    activity_type = activity.get("sport_type") or activity.get("type") or ""
    name = activity.get("name") or ""
    if activity_type:
        properties["activity-type"] = [activity_type]
    if name:
        properties["name"] = [name]
    if track_url:
        properties["track"] = [track_url]

    for source_key, mf2_key in _MF2_NUMERIC_FIELDS:
        value = activity.get(source_key)
        if value is not None:
            properties[mf2_key] = [str(value)]

    if activity.get("has_heartrate"):
        for source_key, mf2_key in _MF2_HEARTRATE_FIELDS:
            value = activity.get(source_key)
            if value is not None:
                properties[mf2_key] = [str(value)]

    for source_key, mf2_key in _MF2_LATLNG_FIELDS:
        value = activity.get(source_key)
        if isinstance(value, list) and len(value) == 2:
            properties[mf2_key] = [f"{value[0]},{value[1]}"]

    kudos_count = activity.get("kudos_count")
    if kudos_count is not None:
        properties["x-kudos-count"] = [str(kudos_count)]

    if not properties:
        return {}
    return {"activity": [{"type": ["h-activity"], "properties": properties}]}


def _attach_gpx(post, account, strava_activity_id, activity, start_date) -> str:
    """Fetch stream data, synthesize + anonymize a GPX track, attach it. Returns the track URL, or ''."""
    try:
        streams = client.get_activity_streams(account, strava_activity_id)
    except client.StravaAPIError as exc:
        logger.warning(
            "import_activity: could not fetch streams for %s: %s", strava_activity_id, exc
        )
        return ""

    latlng = (streams.get("latlng") or {}).get("data") or []
    if not latlng:
        return ""

    altitude = (streams.get("altitude") or {}).get("data")
    time_offsets = (streams.get("time") or {}).get("data")

    gpx_bytes = streams_to_gpx(
        latlng, altitude, time_offsets, start_date, name=activity.get("name", "")
    )
    try:
        gpx_bytes = anonymize_gpx(gpx_bytes, GpxAnonymizeOptions())
    except GpxAnonymizeError as exc:
        logger.warning(
            "import_activity: anonymize_gpx failed for %s, attaching un-trimmed track: %s",
            strava_activity_id, exc,
        )

    asset = File.objects.create(
        kind=File.DOC,
        file=ContentFile(gpx_bytes, name=f"strava-{strava_activity_id}.gpx"),
    )
    Attachment.objects.create(content_object=post, asset=asset, role="gpx")
    return asset.file.url


def _queue_photo_downloads(post_id, account, strava_activity_id) -> None:
    try:
        photos = client.list_activity_photos(account, strava_activity_id)
    except client.StravaAPIError as exc:
        logger.warning(
            "import_activity: could not fetch photos for %s: %s", strava_activity_id, exc
        )
        return

    from .tasks import download_strava_photo

    for photo in photos:
        urls = photo.get("urls") or {}
        if not urls:
            continue
        # Prefer the largest available size; fall back to whatever's there.
        url = urls.get("2000") or urls.get("1000") or urls.get("600") or next(iter(urls.values()), None)
        if url:
            transaction.on_commit(lambda u=url: download_strava_photo.delay(post_id, u))


def import_activity(account, strava_activity_id, *, skip_if_private=False):
    """
    Import a single Strava activity as a blog.Post(kind=ACTIVITY).

    Idempotent: returns None immediately if this activity was already
    imported. Also returns None (without creating anything) if
    skip_if_private is True and the activity is marked private on Strava.
    """
    strava_activity_id = str(strava_activity_id)

    if StravaActivity.objects.filter(strava_activity_id=strava_activity_id).exists():
        logger.debug("import_activity: %s already imported", strava_activity_id)
        return None

    activity = client.get_activity(account, strava_activity_id)

    if skip_if_private and activity.get("private"):
        logger.debug("import_activity: skipping private activity %s", strava_activity_id)
        return None

    start_date = _parse_start_date(activity)
    description = activity.get("description") or ""
    if not description:
        units = SiteConfiguration.get_solo().activity_units
        description = _activity_summary(activity, units=units)

    with transaction.atomic():
        post = Post(
            title=activity.get("name") or "",
            content=description,
            kind=Post.ACTIVITY,
            published_on=start_date,
        )
        post.save()

        track_url = _attach_gpx(post, account, strava_activity_id, activity, start_date)
        mf2 = _build_mf2(activity, track_url)
        if mf2:
            post.mf2 = mf2
            post.save(update_fields=["mf2"])

        try:
            StravaActivity.objects.create(post=post, strava_activity_id=strava_activity_id)
        except IntegrityError:
            # Lost a race with another import of the same activity — roll the
            # whole thing back rather than leaving an orphaned duplicate post.
            transaction.set_rollback(True)
            return None

    _queue_photo_downloads(post.id, account, strava_activity_id)

    return post
