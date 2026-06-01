from celery import shared_task


def _apply_feed_meta(sub, feed_meta: dict, hub_url: str | None) -> list[str]:
    """Update sub fields from feed metadata; return the list of changed field names.

    Shared between populate_subscription_metadata and poll_subscription so the
    logic only lives in one place.
    """
    changed: list[str] = []
    if feed_meta.get("name") and (not sub.name or sub.name == sub.url):
        sub.name = feed_meta["name"]
        changed.append("name")
    if feed_meta.get("photo") and not sub.photo:
        sub.photo = feed_meta["photo"]
        changed.append("photo")
    if hub_url and not sub.websub_hub:
        sub.websub_hub = hub_url
        changed.append("websub_hub")
    if changed:
        sub.save(update_fields=changed)
    return changed


@shared_task(bind=True, max_retries=2, default_retry_delay=60)
def populate_subscription_metadata(self, sub_id: int, base_url: str = "") -> None:
    from django.db import close_old_connections
    from microsub.models import Subscription
    from microsub.feed_parser import fetch_and_parse_feed
    from microsub.views import _store_entries, _subscribe_to_websub_with_base_url

    close_old_connections()
    try:
        sub = Subscription.objects.get(id=sub_id)
    except Subscription.DoesNotExist:
        return

    try:
        entries, hub_url, feed_meta = fetch_and_parse_feed(sub.url)
    except Exception as exc:
        raise self.retry(exc=exc)

    _apply_feed_meta(sub, feed_meta, hub_url)

    if entries:
        _store_entries(sub.channel, sub, entries)

    if hub_url and base_url:
        _subscribe_to_websub_with_base_url(sub, base_url.rstrip("/"))

    close_old_connections()


@shared_task(bind=True, max_retries=2, default_retry_delay=120)
def poll_subscription(self, sub_id: int, *, force: bool = False, doctor: bool = False) -> dict:
    from django.db import close_old_connections
    from django.utils import timezone
    from microsub.models import Subscription
    from microsub.feed_parser import fetch_and_parse_feed
    from microsub.views import _store_entries, _doctor_entries, _subscribe_to_websub_with_base_url
    from django.conf import settings

    REFETCH_INTERVAL_SECONDS = 900

    close_old_connections()
    try:
        sub = Subscription.objects.select_related("channel").get(id=sub_id)
    except Subscription.DoesNotExist:
        return {"skipped": True}

    now = timezone.now()
    if not force and sub.last_fetched_at:
        if (now - sub.last_fetched_at).total_seconds() < REFETCH_INTERVAL_SECONDS:
            return {"skipped": True, "reason": "recently_fetched"}

    try:
        entries, hub_url, feed_meta = fetch_and_parse_feed(sub.url)
    except Exception as exc:
        sub.fetch_error = str(exc)
        sub.last_fetched_at = now
        sub.save(update_fields=["fetch_error", "last_fetched_at"])
        raise self.retry(exc=exc)

    _apply_feed_meta(sub, feed_meta, hub_url)

    if sub.websub_hub and not sub.websub_subscribed_at:
        base_url = getattr(settings, "MICROSUB_BASE_URL", "").rstrip("/")
        if base_url:
            _subscribe_to_websub_with_base_url(sub, base_url)

    new_count = _store_entries(sub.channel, sub, entries)
    updated_count = _doctor_entries(sub.channel, entries) if doctor else 0

    sub.fetch_error = ""
    sub.last_fetched_at = now
    sub.save(update_fields=["fetch_error", "last_fetched_at"])
    close_old_connections()

    return {"new": new_count, "updated": updated_count, "url": sub.url}


@shared_task(ignore_result=True)
def poll_microsub_feeds() -> None:
    from microsub.models import Subscription

    sub_ids = list(Subscription.objects.filter(is_active=True).values_list("id", flat=True))
    for sub_id in sub_ids:
        poll_subscription.delay(sub_id)
