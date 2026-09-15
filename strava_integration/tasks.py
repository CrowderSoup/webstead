"""
Strava Celery tasks.

import_activity_task(strava_activity_id, skip_if_private=False) — imports a
    single activity as a blog.Post; shared by the historical-import view, the
    webhook handler, and the reconciliation safety net below.
update_activity_task(strava_activity_id) — refreshes an already-imported
    post's title/content/stats/photos from Strava's current data.
delete_activity_task(strava_activity_id) — soft-deletes the post linked to
    an activity that was deleted on Strava.
download_strava_photo(post_id, url)   — downloads and attaches a Strava
    activity photo to a post.
handle_strava_webhook_event(payload)  — dispatches a single webhook event to
    one of the above, by its aspect_type (create/update/delete).
reconcile_strava_activities()         — hourly safety net for activities a
    missed/failed webhook delivery never told us about.
"""

import logging
from datetime import timedelta

from celery import shared_task
from celery.exceptions import MaxRetriesExceededError
from django.utils import timezone

from .client import StravaAPIError

logger = logging.getLogger(__name__)


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def import_activity_task(self, strava_activity_id, skip_if_private=False):
    from django.db import close_old_connections

    from .importer import import_activity
    from .models import StravaAccount

    close_old_connections()
    try:
        account = StravaAccount.get_active()
        if not account:
            logger.warning("import_activity_task: no active Strava account")
            return

        post = import_activity(account, strava_activity_id, skip_if_private=skip_if_private)
        if post:
            logger.info(
                "import_activity_task: imported activity %s as post %s",
                strava_activity_id, post.id,
            )
    except StravaAPIError as exc:
        if exc.retriable:
            logger.warning(
                "import_activity_task: retriable error for %s: %s", strava_activity_id, exc
            )
            raise self.retry(exc=exc)
        logger.exception(
            "import_activity_task: non-retriable error for %s", strava_activity_id
        )
    finally:
        close_old_connections()


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def update_activity_task(self, strava_activity_id):
    from django.db import close_old_connections

    from .importer import update_activity
    from .models import StravaAccount

    close_old_connections()
    try:
        account = StravaAccount.get_active()
        if not account:
            logger.warning("update_activity_task: no active Strava account")
            return

        post = update_activity(account, strava_activity_id)
        if post:
            logger.info(
                "update_activity_task: refreshed post %s from activity %s",
                post.id, strava_activity_id,
            )
    except StravaAPIError as exc:
        if exc.retriable:
            logger.warning(
                "update_activity_task: retriable error for %s: %s", strava_activity_id, exc
            )
            raise self.retry(exc=exc)
        logger.exception(
            "update_activity_task: non-retriable error for %s", strava_activity_id
        )
    finally:
        close_old_connections()


@shared_task(ignore_result=True)
def delete_activity_task(strava_activity_id) -> None:
    from django.db import close_old_connections

    from .importer import delete_activity

    close_old_connections()
    try:
        post = delete_activity(strava_activity_id)
        if post:
            logger.info(
                "delete_activity_task: soft-deleted post %s for activity %s",
                post.id, strava_activity_id,
            )
    finally:
        close_old_connections()


@shared_task(bind=True, max_retries=3, default_retry_delay=30)
def download_strava_photo(self, post_id: int, url: str) -> None:
    from django.db import close_old_connections

    from blog.models import Post
    from micropub.views import _download_and_attach_photo

    close_old_connections()
    try:
        post = Post.objects.get(id=post_id)
    except Post.DoesNotExist:
        return

    # Separate the exception path from the success=False path to avoid the
    # outer except swallowing the Retry exception raised by self.retry().
    exhausted = False
    try:
        success = _download_and_attach_photo(post, url)
    except Exception as exc:
        close_old_connections()
        try:
            raise self.retry(exc=exc)
        except MaxRetriesExceededError:
            exhausted = True
    else:
        close_old_connections()
        if not success:
            try:
                raise self.retry()
            except MaxRetriesExceededError:
                exhausted = True

    if exhausted:
        logger.warning(
            "download_strava_photo: retries exhausted for post %s, %s", post_id, url
        )


@shared_task(ignore_result=True)
def handle_strava_webhook_event(payload: dict) -> None:
    from django.db import close_old_connections

    from .models import StravaAccount

    close_old_connections()

    if payload.get("object_type") != "activity":
        return

    account = StravaAccount.get_active()
    if not account or not account.auto_post_enabled:
        return

    object_id = payload.get("object_id")
    if not object_id:
        return

    aspect_type = payload.get("aspect_type")
    if aspect_type == "create":
        import_activity_task.delay(str(object_id), skip_if_private=True)
    elif aspect_type == "update":
        update_activity_task.delay(str(object_id))
    elif aspect_type == "delete":
        delete_activity_task.delay(str(object_id))

    close_old_connections()


@shared_task(ignore_result=True)
def reconcile_strava_activities() -> None:
    """
    Safety net for missed webhook deliveries. Scheduled hourly by Celery
    Beat; only does anything when auto-posting is enabled.
    """
    from django.db import close_old_connections

    from . import client
    from .models import StravaAccount

    close_old_connections()

    account = StravaAccount.get_active()
    if not account or not account.auto_post_enabled:
        return

    after = account.last_reconciled_at or (timezone.now() - timedelta(hours=24))

    try:
        activities = client.list_activities(account, after=int(after.timestamp()), per_page=100)
    except Exception:
        logger.exception("reconcile_strava_activities: failed to list activities")
        return

    for activity in activities:
        activity_id = activity.get("id")
        if activity_id:
            import_activity_task.delay(str(activity_id), skip_if_private=True)

    account.last_reconciled_at = timezone.now()
    account.save(update_fields=["last_reconciled_at"])
    close_old_connections()
