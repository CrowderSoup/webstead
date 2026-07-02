"""Helpers for exposing Mastodon-managed sources as Microsub feeds."""

from __future__ import annotations

MANAGED_BY_MASTODON = "mastodon"

_STREAM_CONFIG = {
    "timeline": {
        "channel_attr": "timeline_channel",
        "name": "Mastodon home timeline",
    },
    "notifications": {
        "channel_attr": "notifications_channel",
        "name": "Mastodon notifications",
    },
}


def _managed_subscription_url(account, stream: str) -> str:
    base = account.app.instance_url.rstrip("/")
    return f"{base}/.well-known/webstead/mastodon/{stream}/{account.account_id}/"


def deactivate_managed_subscriptions(*, stream: str | None = None) -> None:
    from microsub.models import Subscription

    qs = Subscription.objects.filter(managed_by=MANAGED_BY_MASTODON)
    if stream:
        qs = qs.filter(managed_key=stream)
    qs.update(
        is_active=False,
        websub_subscribed_at=None,
        websub_requested_at=None,
        websub_expires_at=None,
    )


def ensure_managed_subscription(account, stream: str) -> Subscription | None:
    from microsub.models import Subscription

    config = _STREAM_CONFIG.get(stream)
    if not config:
        raise ValueError(f"Unknown Mastodon stream '{stream}'")

    channel = getattr(account, config["channel_attr"], None)
    if not channel:
        deactivate_managed_subscriptions(stream=stream)
        return None

    sub, _created = Subscription.objects.get_or_create(
        channel=channel,
        url=_managed_subscription_url(account, stream),
        defaults={
            "name": config["name"],
            "photo": account.avatar_url or "",
            "managed_by": MANAGED_BY_MASTODON,
            "managed_key": stream,
            "is_active": True,
        },
    )

    update_fields: list[str] = []
    desired_fields = {
        "name": config["name"],
        "photo": account.avatar_url or "",
        "managed_by": MANAGED_BY_MASTODON,
        "managed_key": stream,
        "is_active": True,
    }
    for field, value in desired_fields.items():
        if getattr(sub, field) != value:
            setattr(sub, field, value)
            update_fields.append(field)
    if update_fields:
        sub.save(update_fields=update_fields)

    Subscription.objects.filter(
        managed_by=MANAGED_BY_MASTODON,
        managed_key=stream,
    ).exclude(pk=sub.pk).update(
        is_active=False,
        websub_subscribed_at=None,
        websub_requested_at=None,
        websub_expires_at=None,
    )
    return sub


def sync_managed_subscriptions(account) -> dict[str, Subscription | None]:
    return {stream: ensure_managed_subscription(account, stream) for stream in _STREAM_CONFIG}
