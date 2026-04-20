import hashlib
import hmac
import json
import logging
import secrets
from html.parser import HTMLParser
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.parse import urljoin as _urljoin
from urllib.request import Request, urlopen

from django.conf import settings
from django.db.models import Q
from django.http import HttpResponse, JsonResponse
from django.urls import reverse
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.utils.text import slugify
from django.views import View
from django.views.decorators.csrf import csrf_exempt

from core.models import SiteConfiguration

from .feed_parser import fetch_and_parse_feed
from .models import BlockedUser, Channel, Entry, MutedUser, Subscription
from .utils import (
    KIND_FIELD_MAP,
    normalize_category,
    normalize_profile_url,
    normalize_repeated_values,
    normalize_url,
    profile_prefix_q,
    url_matches_profile_prefix,
)

logger = logging.getLogger(__name__)

PAGE_SIZE = 20
MAX_PAGE_SIZE = 100
CHANNEL_NOTIFICATIONS = "notifications"
CHANNEL_HOME = "home"
CHANNEL_GLOBAL = "global"
RESERVED_CHANNEL_UIDS = {CHANNEL_NOTIFICATIONS, CHANNEL_GLOBAL}


def _json_error(error: str, *, status: int, description: str = "", scope: str = "") -> JsonResponse:
    payload = {"error": error}
    if description:
        payload["error_description"] = description
    if scope:
        payload["scope"] = scope
    return JsonResponse(payload, status=status)


def _require_scope(scopes: list[str], required: str) -> bool:
    return required in scopes


def _ordered_channels() -> list[Channel]:
    channels = list(Channel.objects.order_by("order", "id"))
    notifications = [channel for channel in channels if channel.uid == CHANNEL_NOTIFICATIONS]
    others = [channel for channel in channels if channel.uid != CHANNEL_NOTIFICATIONS]
    return notifications + others


def _default_channel() -> Channel | None:
    home = Channel.objects.filter(uid=CHANNEL_HOME).first()
    if home:
        return home
    return Channel.objects.exclude(uid=CHANNEL_NOTIFICATIONS).order_by("order", "id").first()


def _channel_order_response(channels: list[Channel]) -> list[Channel]:
    notifications = [channel for channel in channels if channel.uid == CHANNEL_NOTIFICATIONS]
    others = [channel for channel in channels if channel.uid != CHANNEL_NOTIFICATIONS]
    return notifications + others


def _resolve_channel(channel_uid: str | None, *, allow_global: bool = False, default_to_home: bool = False):
    uid = (channel_uid or "").strip()
    if uid == CHANNEL_GLOBAL:
        if allow_global:
            return CHANNEL_GLOBAL
        return None
    if not uid:
        return _default_channel() if default_to_home else None
    return Channel.objects.filter(uid=uid).first()


def _normalize_block_urls(qs) -> set[str]:
    return {
        normalized
        for normalized in (normalize_profile_url(value) for value in qs.values_list("url", flat=True))
        if normalized
    }


def _visible_entries_qs(channel: Channel | None = None, *, base_qs=None):
    qs = base_qs if base_qs is not None else Entry.objects.all()
    qs = qs.filter(is_removed=False)

    global_muted = _normalize_block_urls(MutedUser.objects.filter(channel__isnull=True))
    global_blocked = _normalize_block_urls(BlockedUser.objects.filter(channel__isnull=True))
    excluded_global = global_muted | global_blocked
    if excluded_global:
        qs = qs.exclude(profile_prefix_q("author_url", excluded_global))

    if channel is not None:
        scoped = _normalize_block_urls(
            MutedUser.objects.filter(channel=channel)
        ) | _normalize_block_urls(BlockedUser.objects.filter(channel=channel))
        if scoped:
            qs = qs.exclude(profile_prefix_q("author_url", scoped))
        return qs

    scoped_exclusion = Q()
    for row in MutedUser.objects.filter(channel__isnull=False).values("channel_id", "url"):
        normalized = normalize_profile_url(row["url"])
        if not normalized:
            continue
        prefix = normalized.rstrip("/") + "/"
        scoped_exclusion |= Q(channel_id=row["channel_id"], author_url=normalized)
        scoped_exclusion |= Q(channel_id=row["channel_id"], author_url__startswith=prefix)
    for row in BlockedUser.objects.filter(channel__isnull=False).values("channel_id", "url"):
        normalized = normalize_profile_url(row["url"])
        if not normalized:
            continue
        prefix = normalized.rstrip("/") + "/"
        scoped_exclusion |= Q(channel_id=row["channel_id"], author_url=normalized)
        scoped_exclusion |= Q(channel_id=row["channel_id"], author_url__startswith=prefix)
    if scoped_exclusion:
        qs = qs.exclude(scoped_exclusion)
    return qs


def _channel_json(channel: Channel) -> dict:
    unread = _visible_entries_qs(channel, base_qs=channel.entries.all()).filter(is_read=False).count()
    return {
        "uid": channel.uid,
        "name": channel.name,
        "unread": unread,
    }


def _entry_json(entry: Entry) -> dict:
    data = entry.data.copy() if isinstance(entry.data, dict) else {}
    data.pop("_uid", None)
    data["uid"] = data.get("uid") or entry.uid
    data["_id"] = str(entry.pk)
    data["_is_read"] = entry.is_read
    data["published"] = entry.published.isoformat()
    if entry.subscription:
        source = {
            "_id": str(entry.subscription.pk),
            "url": entry.subscription.url,
            "name": entry.subscription.name or entry.subscription.url,
            "photo": entry.subscription.photo,
        }
        if entry.subscription.managed_by:
            source["_is_managed"] = True
            source["_provider"] = entry.subscription.managed_by
            source["_managed_key"] = entry.subscription.managed_key
        data["_source"] = source
    elif entry.source_url:
        source = data.get("_source")
        if not isinstance(source, dict):
            source = {}
        data["_source"] = {
            **source,
            "url": source.get("url") or entry.source_url,
        }
    return data


def _callback_url(base_url: str, subscription: Subscription) -> str:
    callback_path = reverse("microsub-websub-callback", kwargs={"subscription_id": subscription.pk})
    callback_root = base_url.rstrip("/") + callback_path
    return f"{callback_root}?token={subscription.websub_callback_token}"


def _subscribe_to_websub(subscription: Subscription, request) -> None:
    if not subscription.websub_hub:
        return
    return _subscribe_to_websub_with_base_url(subscription, request.build_absolute_uri("/"))


def _subscribe_to_websub_with_base_url(subscription: Subscription, base_url: str) -> None:
    if not subscription.websub_hub:
        return
    callback_url = _callback_url(base_url, subscription)
    secret = secrets.token_hex(32)
    body = urlencode(
        {
            "hub.mode": "subscribe",
            "hub.topic": subscription.url,
            "hub.callback": callback_url,
            "hub.secret": secret,
        }
    ).encode()
    try:
        req = Request(
            subscription.websub_hub,
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        with urlopen(req, timeout=10) as resp:
            status = resp.status
        if status in (200, 202):
            subscription.websub_secret = secret
            subscription.websub_requested_at = timezone.now()
            subscription.websub_subscribed_at = None
            subscription.save(
                update_fields=["websub_secret", "websub_requested_at", "websub_subscribed_at"]
            )
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        logger.warning("WebSub subscribe failed for %s: %s", subscription.url, exc)


def _unsubscribe_from_websub(subscription: Subscription, base_url: str) -> None:
    if not subscription.websub_hub:
        return
    callback_url = _callback_url(base_url, subscription)
    body = urlencode(
        {
            "hub.mode": "unsubscribe",
            "hub.topic": subscription.url,
            "hub.callback": callback_url,
        }
    ).encode()
    try:
        req = Request(
            subscription.websub_hub,
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        with urlopen(req, timeout=10):
            pass
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        logger.warning("WebSub unsubscribe failed for %s: %s", subscription.url, exc)


def _is_blocked_for_channel(channel: Channel, author_url: str) -> bool:
    normalized = normalize_profile_url(author_url)
    if not normalized:
        return False
    blocked_urls = BlockedUser.objects.filter(
        Q(channel__isnull=True) | Q(channel=channel)
    ).values_list("url", flat=True)
    return any(url_matches_profile_prefix(blocked_url, normalized) for blocked_url in blocked_urls)


def _mark_existing_entries_blocked(channel: Channel | None, author_url: str) -> None:
    normalized = normalize_profile_url(author_url)
    if not normalized:
        return
    qs = Entry.objects.filter(profile_prefix_q("author_url", [normalized]))
    if channel is not None:
        qs = qs.filter(channel=channel)
    qs.update(is_removed=True)


def _apply_paging(qs, *, before_cursor: str = "", after_cursor: str = ""):
    if after_cursor:
        try:
            cursor_id = int(after_cursor)
            cursor = Entry.objects.filter(pk=cursor_id).values("published", "id").first()
            if cursor:
                qs = qs.filter(
                    Q(published__lt=cursor["published"])
                    | Q(published=cursor["published"], id__lt=cursor_id)
                )
        except (TypeError, ValueError):
            pass

    if before_cursor:
        try:
            cursor_id = int(before_cursor)
            cursor = Entry.objects.filter(pk=cursor_id).values("published", "id").first()
            if cursor:
                qs = qs.filter(
                    Q(published__gt=cursor["published"])
                    | Q(published=cursor["published"], id__gt=cursor_id)
                )
        except (TypeError, ValueError):
            pass
    return qs


def _limit_value(raw_value: str) -> int:
    try:
        value = int(raw_value)
    except (TypeError, ValueError):
        return PAGE_SIZE
    return max(1, min(MAX_PAGE_SIZE, value))


def _feed_result(url: str, feed_meta: dict | None = None) -> dict:
    meta = feed_meta or {}
    payload = {
        "type": "feed",
        "url": normalize_url(url) or url,
    }
    for key in ("name", "photo", "description"):
        if key in meta:
            payload[key] = meta.get(key, "")
    author = meta.get("author")
    if isinstance(author, dict):
        payload["author"] = author
    return payload


def _extract_feed_candidates(url: str) -> list[dict]:
    class _FeedDiscoveryParser(HTMLParser):
        def __init__(self):
            super().__init__()
            self.feeds = []
            self.title = ""
            self._in_title = False

        def handle_starttag(self, tag, attrs):
            if tag.lower() == "title":
                self._in_title = True
            if tag.lower() != "link":
                return
            attr_map = {k.lower(): v for k, v in attrs}
            rels = {r.strip() for r in attr_map.get("rel", "").split()}
            if "alternate" not in rels:
                return
            content_type = attr_map.get("type", "")
            href = attr_map.get("href", "")
            if href and any(value in content_type for value in ("rss", "atom", "xml", "json")):
                self.feeds.append(
                    {
                        "url": _urljoin(url, href),
                        "name": attr_map.get("title", ""),
                    }
                )

        def handle_data(self, data):
            if self._in_title and not self.title:
                self.title = data.strip()

        def handle_endtag(self, tag):
            if tag.lower() == "title":
                self._in_title = False

    candidates: list[dict] = []
    req = Request(url, headers={"User-Agent": "Webstead Microsub/1.0"})
    with urlopen(req, timeout=10) as response:
        content_type = response.headers.get("Content-Type", "")
        body = response.read(100_000).decode("utf-8", errors="replace")
    if "html" not in content_type:
        _, _, feed_meta = fetch_and_parse_feed(url)
        candidates.append(_feed_result(url, feed_meta))
        return candidates

    parser = _FeedDiscoveryParser()
    parser.feed(body)
    try:
        entries, _, page_meta = fetch_and_parse_feed(url)
        page_meta = page_meta or {}
        if parser.title and not page_meta.get("name"):
            page_meta["name"] = parser.title
        if entries or page_meta:
            candidates.append(_feed_result(url, page_meta))
    except Exception:
        pass

    for feed in parser.feeds:
        try:
            _, _, feed_meta = fetch_and_parse_feed(feed["url"])
        except Exception:
            feed_meta = {"name": feed["name"]}
        if feed["name"] and not feed_meta.get("name"):
            feed_meta["name"] = feed["name"]
        candidates.append(_feed_result(feed["url"], feed_meta))
    return candidates


def _search_feed_results(query: str) -> list[dict]:
    if not query:
        return []
    url = query if query.startswith("http://") or query.startswith("https://") else f"https://{query}"
    try:
        raw_results = _extract_feed_candidates(url)
    except Exception as exc:
        logger.debug("Feed search failed for %s: %s", query, exc)
        return []

    results = []
    seen: set[str] = set()
    for result in raw_results:
        normalized = normalize_url(result.get("url", ""))
        if normalized and normalized in seen:
            continue
        if normalized:
            seen.add(normalized)
        results.append(result)
    return results


def _preview_payload(url: str) -> dict:
    entries, _, feed_meta = fetch_and_parse_feed(url)
    payload = _feed_result(url, feed_meta)
    payload["items"] = entries[:PAGE_SIZE]
    payload["paging"] = {}
    return payload


def _apply_channel_order(channel_ids: list[str]) -> None:
    ordered = [channel for channel in _ordered_channels() if channel.uid != CHANNEL_NOTIFICATIONS]
    seen: set[str] = set()
    requested = []
    for uid in channel_ids:
        if uid in {CHANNEL_NOTIFICATIONS, CHANNEL_GLOBAL} or uid in seen:
            continue
        seen.add(uid)
        requested.append(uid)
    if not requested:
        return

    positions: list[int] = []
    channel_map = {channel.uid: channel for channel in ordered}
    for index, channel in enumerate(ordered):
        if channel.uid in requested:
            positions.append(index)
    if len(positions) != len(requested):
        raise ValueError("Unknown channel in reorder request")

    final = ordered[:]
    for index, uid in zip(sorted(positions), requested):
        final[index] = channel_map[uid]

    notifications = [channel for channel in _ordered_channels() if channel.uid == CHANNEL_NOTIFICATIONS]
    all_channels = notifications + final
    for order, channel in enumerate(all_channels):
        if channel.order != order:
            channel.order = order
            channel.save(update_fields=["order"])


def _content_search_qs(request, channel):
    data = request.POST if request.method == "POST" else request.GET
    query = data.get("query", "").strip()
    authors = [normalize_profile_url(value) for value in (data.getlist("author") or [])]
    authors = [value for value in authors if value]
    categories = [normalize_category(value) for value in (data.getlist("category") or [])]
    categories = [value for value in categories if value]
    kinds = normalize_repeated_values(data.getlist("kind"))
    sources = [normalize_url(value) for value in (data.getlist("source") or [])]
    sources = [value for value in sources if value]

    if not any([query, authors, categories, kinds, sources]):
        return _json_error(
            "invalid_request",
            status=400,
            description="Provide query, author, category, kind, or source when searching for content.",
        )

    qs = _visible_entries_qs(None if channel == CHANNEL_GLOBAL else channel)
    if channel != CHANNEL_GLOBAL:
        qs = qs.filter(channel=channel)

    if query:
        from .utils import tokenize_text

        for token in tokenize_text(query):
            qs = qs.filter(search_tokens__token=token)

    if authors:
        qs = qs.filter(author_url__in=authors)
    if categories:
        qs = qs.filter(search_categories__value__in=categories)
    if kinds:
        kind_query = Q()
        for kind in kinds:
            field_name = KIND_FIELD_MAP.get(kind)
            if field_name:
                kind_query |= Q(**{field_name: True})
        if kind_query:
            qs = qs.filter(kind_query)
        else:
            qs = qs.none()
    if sources:
        qs = qs.filter(source_url__in=sources)

    qs = qs.distinct()
    qs = _apply_paging(
        qs,
        before_cursor=data.get("before", ""),
        after_cursor=data.get("after", ""),
    )
    limit = _limit_value(data.get("limit", PAGE_SIZE))
    qs = qs.select_related("subscription").order_by("-published", "-id")[: limit + 1]
    entries = list(qs)
    has_more = len(entries) > limit
    entries = entries[:limit]
    paging = {}
    if entries:
        paging["before"] = str(entries[0].pk)
    if has_more:
        paging["after"] = str(entries[-1].pk)
    return JsonResponse({"items": [_entry_json(entry) for entry in entries], "paging": paging})


def _channel_feed_items(channel: Channel) -> list[dict]:
    subs = channel.subscriptions.filter(is_active=True).order_by("name", "url")
    items = []
    for sub in subs:
        item = {
            "type": "feed",
            "url": sub.url,
            "name": sub.name or "",
            "photo": sub.photo or "",
        }
        if sub.pk:
            item["_id"] = str(sub.pk)
        if sub.managed_by:
            item["_is_managed"] = True
            item["_provider"] = sub.managed_by
            item["_managed_key"] = sub.managed_key
        items.append(item)
    return items


@method_decorator(csrf_exempt, name="dispatch")
class MicrosubView(View):
    def dispatch(self, request, *args, **kwargs):
        from micropub.views import _authorized

        authorized, scopes = _authorized(request)
        if not authorized:
            return _json_error("unauthorized", status=401)
        request.microsub_scopes = scopes
        return super().dispatch(request, *args, **kwargs)

    def get(self, request):
        action = request.GET.get("action", "")
        if action == "channels":
            return self._get_channels(request)
        if action == "follow":
            return self._get_follow(request)
        if action == "timeline":
            return self._get_timeline(request)
        if action == "mute":
            return self._get_mute(request)
        if action == "block":
            return self._get_block(request)
        if action == "search":
            return self._search(request)
        if action == "preview":
            return self._preview(request)
        return _json_error("invalid_request", status=400, description="Unknown action")

    def post(self, request):
        action = request.POST.get("action", "")
        if action == "channels":
            return self._post_channels(request)
        if action == "follow":
            return self._post_follow(request)
        if action == "unfollow":
            return self._post_unfollow(request)
        if action == "timeline":
            return self._post_timeline(request)
        if action == "mute":
            return self._post_mute(request)
        if action == "unmute":
            return self._post_unmute(request)
        if action == "block":
            return self._post_block(request)
        if action == "unblock":
            return self._post_unblock(request)
        if action == "search":
            return self._search(request)
        if action == "preview":
            return self._preview(request)
        return _json_error("invalid_request", status=400, description="Unknown action")

    def _get_channels(self, request):
        if not _require_scope(request.microsub_scopes, "read"):
            return _json_error("insufficient_scope", status=403, scope="read")
        channels = _channel_order_response(_ordered_channels())
        return JsonResponse({"channels": [_channel_json(channel) for channel in channels]})

    def _get_follow(self, request):
        if not _require_scope(request.microsub_scopes, "read"):
            return _json_error("insufficient_scope", status=403, scope="read")
        channel = _resolve_channel(request.GET.get("channel"))
        if not channel:
            return _json_error("invalid_request", status=400, description="Channel not found")
        return JsonResponse({"items": _channel_feed_items(channel)})

    def _get_timeline(self, request):
        if not _require_scope(request.microsub_scopes, "read"):
            return _json_error("insufficient_scope", status=403, scope="read")
        channel = _resolve_channel(request.GET.get("channel"), default_to_home=True)
        if not isinstance(channel, Channel):
            return _json_error("invalid_request", status=400, description="Channel not found")

        qs = _visible_entries_qs(channel, base_qs=channel.entries.all())

        filter_param = request.GET.get("filter", "")
        if filter_param == "unread":
            qs = qs.filter(is_read=False)

        is_read_param = request.GET.get("is_read", "")
        if is_read_param == "false":
            qs = qs.filter(is_read=False)
        elif is_read_param == "true":
            qs = qs.filter(is_read=True)

        source_url = normalize_url(request.GET.get("source", ""))
        if source_url:
            qs = qs.filter(source_url=source_url)

        qs = _apply_paging(
            qs,
            before_cursor=request.GET.get("before", ""),
            after_cursor=request.GET.get("after", ""),
        )
        limit = _limit_value(request.GET.get("limit", PAGE_SIZE))
        qs = qs.select_related("subscription").order_by("-published", "-id")[: limit + 1]
        entries = list(qs)
        has_more = len(entries) > limit
        entries = entries[:limit]

        paging = {}
        if entries:
            paging["before"] = str(entries[0].pk)
        if has_more:
            paging["after"] = str(entries[-1].pk)

        return JsonResponse({"items": [_entry_json(entry) for entry in entries], "paging": paging})

    def _get_mute(self, request):
        if not _require_scope(request.microsub_scopes, "mute"):
            return _json_error("insufficient_scope", status=403, scope="mute")
        channel = _resolve_channel(request.GET.get("channel"), allow_global=True)
        if request.GET.get("channel") and channel is None:
            return _json_error("invalid_request", status=400, description="Channel not found")
        if channel == CHANNEL_GLOBAL or channel is None:
            qs = MutedUser.objects.filter(channel__isnull=True)
        else:
            qs = MutedUser.objects.filter(channel=channel)
        return JsonResponse({"items": [{"type": "card", "url": mute.url} for mute in qs.order_by("url")]})

    def _get_block(self, request):
        if not _require_scope(request.microsub_scopes, "block"):
            return _json_error("insufficient_scope", status=403, scope="block")
        channel = _resolve_channel(request.GET.get("channel"), allow_global=True)
        if request.GET.get("channel") and channel is None:
            return _json_error("invalid_request", status=400, description="Channel not found")
        if channel == CHANNEL_GLOBAL or channel is None:
            qs = BlockedUser.objects.filter(channel__isnull=True)
        else:
            qs = BlockedUser.objects.filter(channel=channel)
        return JsonResponse({"items": [{"type": "card", "url": blocked.url} for blocked in qs.order_by("url")]})

    def _search(self, request):
        if not _require_scope(request.microsub_scopes, "read"):
            return _json_error("insufficient_scope", status=403, scope="read")
        data = request.POST if request.method == "POST" else request.GET
        channel = data.get("channel", "").strip()
        if channel:
            resolved = _resolve_channel(channel, allow_global=True)
            if resolved is None:
                return _json_error("invalid_request", status=400, description="Channel not found")
            return _content_search_qs(request, resolved)
        return JsonResponse({"results": _search_feed_results(data.get("query", "").strip())})

    def _preview(self, request):
        if not _require_scope(request.microsub_scopes, "read"):
            return _json_error("insufficient_scope", status=403, scope="read")
        data = request.POST if request.method == "POST" else request.GET
        url = normalize_url(data.get("url", ""))
        if not url:
            return _json_error("invalid_request", status=400, description="Preview requires a valid URL.")
        try:
            return JsonResponse(_preview_payload(url))
        except Exception as exc:
            return _json_error("fetch_error", status=502, description=str(exc))

    def _post_channels(self, request):
        if not _require_scope(request.microsub_scopes, "channels"):
            return _json_error("insufficient_scope", status=403, scope="channels")

        method = request.POST.get("method", "")
        channel_uid = request.POST.get("channel", "").strip()
        name = request.POST.get("name", "").strip()
        channels_order = request.POST.getlist("channels[]") or request.POST.getlist("channels")
        if channels_order:
            try:
                _apply_channel_order(channels_order)
            except ValueError as exc:
                return _json_error("invalid_request", status=400, description=str(exc))
            return JsonResponse({})

        if method == "delete":
            channel = Channel.objects.filter(uid=channel_uid).first()
            if not channel:
                return _json_error("invalid_request", status=400, description="Channel not found")
            if channel.uid == CHANNEL_NOTIFICATIONS:
                return _json_error("forbidden", status=403, description="Cannot delete notifications channel")
            if Channel.objects.exclude(uid=CHANNEL_NOTIFICATIONS).count() <= 1:
                return _json_error("forbidden", status=403, description="At least one non-notifications channel is required")
            channel.delete()
            return JsonResponse({})

        if channel_uid and name:
            channel = Channel.objects.filter(uid=channel_uid).first()
            if not channel:
                return _json_error("invalid_request", status=400, description="Channel not found")
            channel.name = name
            channel.save(update_fields=["name"])
            return JsonResponse(_channel_json(channel))

        if name:
            base_slug = slugify(name) or "channel"
            if base_slug in RESERVED_CHANNEL_UIDS:
                return _json_error("invalid_request", status=400, description="That channel name is reserved")
            uid = base_slug
            suffix = 1
            while Channel.objects.filter(uid=uid).exists():
                uid = f"{base_slug}-{suffix}"
                suffix += 1
            max_order = Channel.objects.order_by("-order").values_list("order", flat=True).first() or 0
            channel = Channel.objects.create(uid=uid, name=name, order=max_order + 1)
            return JsonResponse(_channel_json(channel))

        return _json_error("invalid_request", status=400, description="Invalid channels request")

    def _post_follow(self, request):
        if not _require_scope(request.microsub_scopes, "follow"):
            return _json_error("insufficient_scope", status=403, scope="follow")
        channel = _resolve_channel(request.POST.get("channel"))
        url = normalize_url(request.POST.get("url", ""))
        if not isinstance(channel, Channel) or not url:
            return _json_error("invalid_request", status=400, description="Follow requires channel and URL")

        sub, created = Subscription.objects.get_or_create(channel=channel, url=url, defaults={"is_active": True})
        if not created:
            sub.is_active = True
            sub.save(update_fields=["is_active"])

        if created:
            from django.db import transaction
            from microsub.tasks import populate_subscription_metadata

            base_url = request.build_absolute_uri("/")
            transaction.on_commit(lambda: populate_subscription_metadata.delay(sub.id, base_url))

        return JsonResponse(_feed_result(sub.url, {"name": sub.name or "", "photo": sub.photo or ""}))

    def _post_unfollow(self, request):
        if not _require_scope(request.microsub_scopes, "follow"):
            return _json_error("insufficient_scope", status=403, scope="follow")
        channel = _resolve_channel(request.POST.get("channel"))
        url = normalize_url(request.POST.get("url", ""))
        if not isinstance(channel, Channel) or not url:
            return _json_error("invalid_request", status=400, description="Unfollow requires channel and URL")
        sub = Subscription.objects.filter(channel=channel, url=url).first()
        if sub:
            if sub.managed_by:
                return _json_error(
                    "forbidden",
                    status=403,
                    description=f"This feed is managed by the {sub.managed_by} integration.",
                )
            sub.is_active = False
            sub.websub_subscribed_at = None
            sub.websub_requested_at = None
            sub.websub_expires_at = None
            sub.save(
                update_fields=[
                    "is_active",
                    "websub_subscribed_at",
                    "websub_requested_at",
                    "websub_expires_at",
                ]
            )
            if sub.websub_hub:
                base_url = getattr(settings, "MICROSUB_BASE_URL", "").rstrip("/") or request.build_absolute_uri("/").rstrip("/")
                _unsubscribe_from_websub(sub, base_url)
            if SiteConfiguration.get_solo().microsub_unfollow_removes_entries:
                channel.entries.filter(subscription=sub).update(is_removed=True)
        return JsonResponse({})

    def _post_timeline(self, request):
        if not _require_scope(request.microsub_scopes, "read"):
            return _json_error("insufficient_scope", status=403, scope="read")
        method = request.POST.get("method", "")
        channel = _resolve_channel(request.POST.get("channel"))
        if not isinstance(channel, Channel):
            return _json_error("invalid_request", status=400, description="Channel not found")

        entry_ids = request.POST.getlist("entry[]") or request.POST.getlist("entry")
        if entry_ids:
            try:
                entry_ids = [int(value) for value in entry_ids]
            except (TypeError, ValueError):
                return _json_error("invalid_request", status=400, description="Invalid entry ID")

        if method in ("", "mark_read"):
            last_read_entry = request.POST.get("last_read_entry", "")
            if last_read_entry:
                try:
                    cursor_id = int(last_read_entry)
                except (TypeError, ValueError):
                    return JsonResponse({})
                cursor = channel.entries.filter(pk=cursor_id).values("published", "id").first()
                if cursor:
                    channel.entries.filter(
                        Q(published__gt=cursor["published"])
                        | Q(published=cursor["published"], id__gte=cursor_id)
                    ).filter(is_removed=False).update(is_read=True)
            elif entry_ids:
                channel.entries.filter(pk__in=entry_ids, is_removed=False).update(is_read=True)
            else:
                channel.entries.filter(is_removed=False).update(is_read=True)
            return JsonResponse({})

        if method == "mark_unread":
            if entry_ids:
                channel.entries.filter(pk__in=entry_ids).update(is_read=False)
            return JsonResponse({})

        if method == "remove":
            if entry_ids:
                channel.entries.filter(pk__in=entry_ids).update(is_removed=True)
            return JsonResponse({})

        return _json_error("invalid_request", status=400, description="Unknown timeline method")

    def _post_mute(self, request):
        if not _require_scope(request.microsub_scopes, "mute"):
            return _json_error("insufficient_scope", status=403, scope="mute")
        url = normalize_profile_url(request.POST.get("url", ""))
        channel = _resolve_channel(request.POST.get("channel"), allow_global=True)
        if not url:
            return _json_error("invalid_request", status=400, description="Mute requires a valid URL")
        if request.POST.get("channel") and channel is None:
            return _json_error("invalid_request", status=400, description="Channel not found")
        if channel == CHANNEL_GLOBAL:
            channel = None
        MutedUser.objects.get_or_create(channel=channel, url=url)
        return JsonResponse({})

    def _post_unmute(self, request):
        if not _require_scope(request.microsub_scopes, "mute"):
            return _json_error("insufficient_scope", status=403, scope="mute")
        url = normalize_profile_url(request.POST.get("url", ""))
        channel = _resolve_channel(request.POST.get("channel"), allow_global=True)
        if not url:
            return _json_error("invalid_request", status=400, description="Unmute requires a valid URL")
        if request.POST.get("channel") and channel is None:
            return _json_error("invalid_request", status=400, description="Channel not found")
        if channel == CHANNEL_GLOBAL:
            channel = None
        MutedUser.objects.filter(channel=channel, url=url).delete()
        return JsonResponse({})

    def _post_block(self, request):
        if not _require_scope(request.microsub_scopes, "block"):
            return _json_error("insufficient_scope", status=403, scope="block")
        url = normalize_profile_url(request.POST.get("url", ""))
        channel = _resolve_channel(request.POST.get("channel"), allow_global=True)
        if not url:
            return _json_error("invalid_request", status=400, description="Block requires a valid URL")
        if request.POST.get("channel") and channel is None:
            return _json_error("invalid_request", status=400, description="Channel not found")
        if channel == CHANNEL_GLOBAL:
            channel = None
        BlockedUser.objects.get_or_create(channel=channel, url=url)
        _mark_existing_entries_blocked(channel, url)
        return JsonResponse({})

    def _post_unblock(self, request):
        if not _require_scope(request.microsub_scopes, "block"):
            return _json_error("insufficient_scope", status=403, scope="block")
        url = normalize_profile_url(request.POST.get("url", ""))
        channel = _resolve_channel(request.POST.get("channel"), allow_global=True)
        if not url:
            return _json_error("invalid_request", status=400, description="Unblock requires a valid URL")
        if request.POST.get("channel") and channel is None:
            return _json_error("invalid_request", status=400, description="Channel not found")
        if channel == CHANNEL_GLOBAL:
            channel = None
        BlockedUser.objects.filter(channel=channel, url=url).delete()
        return JsonResponse({})


def _parse_signature_header(header: str, body: bytes, secret: str) -> bool:
    if not header:
        return False
    algorithm, _, provided = header.partition("=")
    algorithm = algorithm.lower()
    if algorithm == "sha1":
        digestmod = hashlib.sha1
    else:
        digestmod = hashlib.sha256
    expected = hmac.new(secret.encode(), body, digestmod).hexdigest()
    return hmac.compare_digest(expected, provided)


@method_decorator(csrf_exempt, name="dispatch")
class WebSubCallbackView(View):
    def _valid_callback(self, request, sub: Subscription) -> bool:
        token = request.GET.get("token", "")
        if not token or token != sub.websub_callback_token:
            return False
        topic = normalize_url(request.GET.get("hub.topic", "") or sub.url)
        return not topic or topic == normalize_url(sub.url)

    def get(self, request, subscription_id):
        sub = Subscription.objects.filter(pk=subscription_id).first()
        if not sub or not self._valid_callback(request, sub):
            return HttpResponse(status=404)
        challenge = request.GET.get("hub.challenge", "")
        mode = request.GET.get("hub.mode", "")
        topic = normalize_url(request.GET.get("hub.topic", ""))
        if topic and topic != normalize_url(sub.url):
            return HttpResponse(status=404)
        if mode == "subscribe" and challenge and sub.is_active:
            sub.websub_subscribed_at = timezone.now()
            lease_seconds = request.GET.get("hub.lease_seconds")
            if lease_seconds:
                try:
                    sub.websub_expires_at = timezone.now() + timezone.timedelta(seconds=int(lease_seconds))
                except (TypeError, ValueError):
                    pass
            sub.save(update_fields=["websub_subscribed_at", "websub_expires_at"])
            return HttpResponse(challenge, content_type="text/plain")
        if mode == "unsubscribe" and challenge and not sub.is_active:
            sub.websub_subscribed_at = None
            sub.websub_expires_at = None
            sub.save(update_fields=["websub_subscribed_at", "websub_expires_at"])
            return HttpResponse(challenge, content_type="text/plain")
        return HttpResponse(status=400)

    def post(self, request, subscription_id):
        sub = Subscription.objects.filter(pk=subscription_id).first()
        if not sub or not sub.is_active or not sub.websub_subscribed_at or not self._valid_callback(request, sub):
            return HttpResponse(status=404)
        sig_header = request.META.get("HTTP_X_HUB_SIGNATURE_256") or request.META.get("HTTP_X_HUB_SIGNATURE", "")
        if not sub.websub_secret or not _parse_signature_header(sig_header, request.body, sub.websub_secret):
            return HttpResponse(status=401)

        content_type = request.content_type or ""
        try:
            from .feed_parser import _parse_hfeed, _parse_json_feed, _parse_rss_atom

            if "json" in content_type:
                data = json.loads(request.body.decode("utf-8", errors="replace"))
                if isinstance(data.get("version"), str) and "jsonfeed" in data["version"]:
                    entries, _ = _parse_json_feed(data, sub.url)
                else:
                    entries, _ = _parse_rss_atom(request.body, sub.url)
            elif "html" in content_type:
                entries, _ = _parse_hfeed(request.body.decode("utf-8", errors="replace"), sub.url)
            else:
                entries, _ = _parse_rss_atom(request.body, sub.url)
            _store_entries(sub.channel, sub, entries)
        except Exception as exc:
            logger.exception("WebSub notification processing failed for sub %s: %s", subscription_id, exc)
            return HttpResponse(status=200)
        return HttpResponse(status=200)


def _doctor_entries(channel: Channel, entries: list[dict]) -> int:
    from dateutil.parser import parse as parse_dt
    from django.utils import timezone as tz

    updated = 0
    for entry_data in entries:
        uid = entry_data.get("_uid") or entry_data.get("url") or entry_data.get("uid")
        if not uid:
            continue

        entry = Entry.objects.filter(channel=channel, uid=str(uid)).first()
        if not entry:
            continue

        published_str = entry_data.get("published")
        if published_str:
            try:
                published = parse_dt(published_str)
                if published.tzinfo is None:
                    published = tz.make_aware(published)
                entry_data["published"] = published.isoformat()
                entry.published = published
            except Exception:
                pass
        entry.data = entry_data
        entry.save(update_fields=["data", "published"])
        updated += 1
    return updated


def _store_entries(channel: Channel, subscription: Subscription | None, entries: list[dict]) -> int:
    from dateutil.parser import parse as parse_dt
    from django.utils import timezone as tz

    if subscription and subscription.url:
        for entry_data in entries:
            author = entry_data.get("author")
            if isinstance(author, dict) and not author.get("url"):
                entry_data["author"] = {**author, "url": subscription.url}
            elif not author:
                card: dict = {"type": "card", "url": subscription.url}
                if subscription.name:
                    card["name"] = subscription.name
                if subscription.photo:
                    card["photo"] = subscription.photo
                entry_data["author"] = card

    new_count = 0
    for entry_data in entries:
        uid = entry_data.get("_uid") or entry_data.get("url") or entry_data.get("uid")
        if not uid:
            continue
        published = tz.now()
        published_str = entry_data.get("published")
        if published_str:
            try:
                published = parse_dt(published_str)
                if published.tzinfo is None:
                    published = tz.make_aware(published)
                entry_data["published"] = published.isoformat()
            except Exception:
                pass

        author = entry_data.get("author", {})
        author_url = ""
        if isinstance(author, dict):
            author_url = normalize_profile_url(author.get("url", "") or "")
        if author_url and _is_blocked_for_channel(channel, author_url):
            continue

        _, created = Entry.objects.get_or_create(
            channel=channel,
            uid=str(uid),
            defaults={
                "subscription": subscription,
                "data": entry_data,
                "published": published,
                "author_url": author_url,
            },
        )
        if created:
            new_count += 1
    return new_count
