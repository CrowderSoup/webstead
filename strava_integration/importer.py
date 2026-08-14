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
from files.gpx import GpxAnonymizeError, GpxAnonymizeOptions, anonymize_gpx
from files.models import Attachment, File

from . import client
from .gpx import streams_to_gpx
from .models import StravaActivity

logger = logging.getLogger(__name__)


def _parse_start_date(activity: dict):
    raw = activity.get("start_date") or activity.get("start_date_local")
    parsed = parse_datetime(raw) if raw else None
    return parsed or dj_timezone.now()


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

    with transaction.atomic():
        post = Post(
            title=activity.get("name") or "",
            content=activity.get("description") or "",
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
