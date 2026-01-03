from __future__ import annotations

from celery import shared_task
from django.conf import settings
from django.core.files.base import ContentFile
from django.utils import timezone

from .activity import activity_from_mf2
from .flyover import fetch_remote_gpx, generate_flyover_video
from .models import ActivityFlyover, Post
from files.models import File


def enqueue_activity_flyover(flyover: ActivityFlyover) -> None:
    if getattr(settings, "RUNNING_TESTS", False):
        return
    if flyover.status == ActivityFlyover.READY and flyover.video:
        return
    if flyover.enqueued_at:
        return

    flyover.status = ActivityFlyover.PENDING
    flyover.enqueued_at = timezone.now()
    flyover.save(update_fields=["status", "enqueued_at", "updated_at"])
    generate_activity_flyover.delay(flyover.post_id)


@shared_task(bind=True)
def generate_activity_flyover(self, post_id: int) -> None:
    flyover = ActivityFlyover.objects.select_related("post").get(post_id=post_id)
    post = flyover.post

    activity = activity_from_mf2(post)
    gpx_bytes = None

    if post.gpx_attachment:
        with post.gpx_attachment.asset.file.open("rb") as handle:
            gpx_bytes = handle.read()
    elif activity.get("track_url"):
        gpx_bytes = fetch_remote_gpx(activity["track_url"])

    if not gpx_bytes:
        flyover.status = ActivityFlyover.FAILED
        flyover.error_message = "No GPX track available to generate flyover."
        flyover.save(update_fields=["status", "error_message", "updated_at"])
        return

    try:
        video_bytes, filename = generate_flyover_video(gpx_bytes)
    except Exception as exc:
        message = str(exc) or "Flyover generation failed."
        flyover.status = ActivityFlyover.FAILED
        flyover.error_message = message[:255]
        flyover.save(update_fields=["status", "error_message", "updated_at"])
        return

    video_file = File.objects.create(
        kind=File.VIDEO,
        file=ContentFile(video_bytes, name=filename),
        owner=post.author,
    )

    flyover.video = video_file
    flyover.status = ActivityFlyover.READY
    flyover.error_message = ""
    flyover.save(update_fields=["video", "status", "error_message", "updated_at"])
