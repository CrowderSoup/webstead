import logging

from celery import shared_task

logger = logging.getLogger("micropub.webmention")


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def verify_and_update_webmention(self, webmention_id: int) -> None:
    from django.db import close_old_connections
    from micropub.models import Webmention
    from micropub.webmention import verify_webmention_source

    close_old_connections()
    try:
        wm = Webmention.objects.get(id=webmention_id)
    except Webmention.DoesNotExist:
        return

    verified, verify_error, fetch_failed = verify_webmention_source(wm.source, wm.target)
    if fetch_failed:
        raise self.retry()

    if not verified:
        status = Webmention.REJECTED
    else:
        from micropub.views import _is_trusted_domain
        status = Webmention.ACCEPTED if _is_trusted_domain(wm.source) else Webmention.PENDING

    wm.status = status
    wm.error = verify_error or ""
    wm.save(update_fields=["status", "error", "updated_at"])
    close_old_connections()


@shared_task(bind=True, max_retries=3, default_retry_delay=30)
def download_post_photo(self, post_id: int, url: str, alt_text: str = "") -> None:
    from celery.exceptions import MaxRetriesExceededError
    from django.db import close_old_connections
    from blog.models import Post
    from micropub.views import _download_and_attach_photo

    close_old_connections()
    try:
        post = Post.objects.get(id=post_id)
    except Post.DoesNotExist:
        return

    # Separate the exception path from the success=False path to avoid
    # the outer except clause swallowing the Retry exception raised by
    # self.retry() when retries remain.
    exhausted = False
    try:
        success = _download_and_attach_photo(post, url, alt_text)
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
        # Rather than silently discarding the photo URL, fall back to the
        # original behaviour: append a raw markdown image so the post at
        # least contains a reference to the photo.
        logger.warning(
            "download_post_photo: retries exhausted for post %s; "
            "appending markdown fallback for %s",
            post_id,
            url,
        )
        post.content = (post.content or "") + f"\n![{alt_text or 'Photo'}]({url})\n"
        post.save(update_fields=["content", "updated_at"])


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def send_single_webmention(self, post_id: int, source_url: str, target_url: str, mention_type: str) -> None:
    from django.db import close_old_connections
    from blog.models import Post
    from micropub.webmention import send_webmention

    close_old_connections()
    try:
        post = Post.objects.get(id=post_id)
    except Post.DoesNotExist:
        return

    try:
        wm = send_webmention(source_url, target_url, mention_type=mention_type, local_post=post)
        if wm.status == wm.TIMED_OUT:
            raise self.retry()
    except Exception as exc:
        raise self.retry(exc=exc)
    finally:
        close_old_connections()


@shared_task
def dispatch_webmentions(post_id: int, source_url: str, *, include_bridgy: bool = False) -> None:
    import urllib.parse
    from blog.models import Post
    from micropub.models import Webmention
    from micropub.webmention import (
        _bridgy_publish_targets,
        _extract_targets,
        _is_globally_blocked_target,
        _resolve_mention_type,
    )

    try:
        post = Post.objects.get(id=post_id)
    except Post.DoesNotExist:
        return

    source_host = urllib.parse.urlparse(source_url).netloc
    targets = [
        u
        for u in _extract_targets(post)
        if urllib.parse.urlparse(u).netloc != source_host and not _is_globally_blocked_target(u)
    ]
    existing = set(
        Webmention.objects.filter(source=source_url, target__in=targets)
        .exclude(status__in=[Webmention.REJECTED, Webmention.TIMED_OUT])
        .values_list("target", flat=True)
    )
    for target in targets:
        if target in existing:
            continue
        mention_type = _resolve_mention_type(post, target)
        send_single_webmention.delay(post_id, source_url, target, mention_type)

    if include_bridgy:
        from core.models import SiteConfiguration
        settings_obj = SiteConfiguration.get_solo()
        bridgy_targets = [
            target
            for target in _bridgy_publish_targets(settings_obj)
            if not _is_globally_blocked_target(target)
        ]
        bridgy_existing = set(
            Webmention.objects.filter(source=source_url, target__in=bridgy_targets)
            .values_list("target", flat=True)
        )
        for target in bridgy_targets:
            if target not in bridgy_existing and post.kind not in (Post.LIKE, Post.REPLY, Post.REPOST):
                send_single_webmention.delay(post_id, source_url, target, Webmention.MENTION)
