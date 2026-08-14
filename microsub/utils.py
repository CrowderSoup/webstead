import re
from collections.abc import Iterable
from urllib.parse import urlparse, urlunparse

from django.db.models import Q


_TOKEN_RE = re.compile(r"\w+", re.UNICODE)

ARRAY_PROPERTIES = (
    "audio",
    "bookmark-of",
    "category",
    "in-reply-to",
    "like-of",
    "listen-of",
    "photo",
    "read-of",
    "repost-of",
    "syndication",
    "video",
    "watch-of",
)

KIND_FIELD_MAP = {
    "bookmark": "kind_bookmark",
    "reply": "kind_reply",
    "repost": "kind_repost",
    "like": "kind_like",
    "checkin": "kind_checkin",
    "photo": "kind_photo",
    "video": "kind_video",
    "audio": "kind_audio",
}


def normalize_url(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    parsed = urlparse(value)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc or parsed.fragment:
        return ""
    path = parsed.path or "/"
    return urlunparse(
        (
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            path,
            parsed.params,
            parsed.query,
            "",
        )
    )


def normalize_profile_url(value: str) -> str:
    return normalize_url(value)


def normalize_category(value: str) -> str:
    return " ".join((value or "").split()).strip().lower()


def ensure_string_list(value) -> list[str]:
    if value in (None, ""):
        return []
    if isinstance(value, (list, tuple, set)):
        items = value
    else:
        items = [value]
    result: list[str] = []
    for item in items:
        if isinstance(item, str):
            item = item.strip()
            if item:
                result.append(item)
    return result


def ensure_photo_list(value) -> list:
    """
    Like ensure_string_list, but preserves {"value": url, "alt": ...} dict
    form for photos that carry alt text, instead of collapsing everything
    to a bare URL string. Items without alt text still collapse to plain
    strings, matching ensure_string_list's existing output shape.
    """
    if value in (None, ""):
        return []
    if isinstance(value, (list, tuple, set)):
        items = value
    else:
        items = [value]
    result: list = []
    for item in items:
        if isinstance(item, str):
            item = item.strip()
            if item:
                result.append(item)
        elif isinstance(item, dict):
            url = item.get("value") or item.get("url", "")
            url = url.strip() if isinstance(url, str) else ""
            if not url:
                continue
            alt = item.get("alt")
            if isinstance(alt, str) and alt.strip():
                result.append({"value": url, "alt": alt.strip()})
            else:
                result.append(url)
    return result


def extract_content_text(data: dict) -> str:
    content = data.get("content")
    if isinstance(content, dict):
        text = content.get("text")
        if isinstance(text, str) and text.strip():
            return text
        html = content.get("html")
        if isinstance(html, str):
            return html
    if isinstance(content, str):
        return content
    return ""


def tokenize_text(value: str) -> list[str]:
    tokens = []
    seen: set[str] = set()
    for token in _TOKEN_RE.findall((value or "").lower()):
        if token and token not in seen:
            seen.add(token)
            tokens.append(token)
    return tokens


def infer_kind_flags(data: dict) -> dict[str, bool]:
    photos = ensure_photo_list(data.get("photo"))
    videos = ensure_string_list(data.get("video"))
    audio = ensure_string_list(data.get("audio"))
    return {
        "kind_like": bool(ensure_string_list(data.get("like-of"))),
        "kind_repost": bool(ensure_string_list(data.get("repost-of"))),
        "kind_bookmark": bool(ensure_string_list(data.get("bookmark-of"))),
        "kind_reply": bool(ensure_string_list(data.get("in-reply-to"))),
        "kind_checkin": isinstance(data.get("checkin"), dict),
        "kind_photo": bool(photos),
        "kind_video": bool(videos),
        "kind_audio": bool(audio),
    }


def normalize_entry_data(
    data,
    *,
    uid: str,
    subscription_url: str = "",
    existing_author_url: str = "",
    existing_source_url: str = "",
) -> tuple[dict, dict]:
    payload = data.copy() if isinstance(data, dict) else {}
    internal_uid = payload.pop("_uid", "") or payload.get("uid") or uid
    payload["uid"] = str(internal_uid or uid)

    for prop in ARRAY_PROPERTIES:
        values = ensure_photo_list(payload.get(prop)) if prop == "photo" else ensure_string_list(payload.get(prop))
        if values:
            payload[prop] = values
        elif prop in payload:
            payload.pop(prop, None)

    author_url = ""
    author = payload.get("author")
    if isinstance(author, dict):
        normalized_author = author.copy()
        normalized_author_url = normalize_profile_url(author.get("url", "") or "")
        if normalized_author_url:
            normalized_author["url"] = normalized_author_url
            author_url = normalized_author_url
        elif existing_author_url:
            author_url = normalize_profile_url(existing_author_url)
        payload["author"] = normalized_author
    elif existing_author_url:
        author_url = normalize_profile_url(existing_author_url)

    source_url = ""
    if subscription_url:
        source_url = normalize_url(subscription_url)
    else:
        source = payload.get("_source")
        if isinstance(source, dict):
            source_url = normalize_url(source.get("url", "") or "")
        if not source_url and existing_source_url:
            source_url = normalize_url(existing_source_url)

    kind_flags = infer_kind_flags(payload)
    search_text = " ".join(
        filter(
            None,
            [
                payload.get("name", "") if isinstance(payload.get("name"), str) else "",
                payload.get("summary", "") if isinstance(payload.get("summary"), str) else "",
                extract_content_text(payload),
            ],
        )
    )
    categories = [normalize_category(value) for value in ensure_string_list(payload.get("category"))]
    categories = [value for value in categories if value]
    tokens = tokenize_text(search_text)

    metadata = {
        "author_url": author_url,
        "source_url": source_url,
        "categories": categories,
        "tokens": tokens,
        **kind_flags,
    }
    return payload, metadata


def url_matches_profile_prefix(profile_url: str, target_url: str) -> bool:
    profile = normalize_profile_url(profile_url)
    target = normalize_url(target_url)
    if not profile or not target:
        return False
    profile_parts = urlparse(profile)
    target_parts = urlparse(target)
    if profile_parts.netloc != target_parts.netloc:
        return False
    if profile_parts.scheme != target_parts.scheme:
        return False
    profile_path = profile_parts.path or "/"
    target_path = target_parts.path or "/"
    if target_path == profile_path:
        return True
    profile_prefix = profile_path.rstrip("/")
    if not profile_prefix:
        return True
    return target_path.startswith(profile_prefix + "/")


def normalize_repeated_values(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        item = (value or "").strip()
        if item and item not in seen:
            seen.add(item)
            result.append(item)
    return result


def profile_prefix_q(field_name: str, profile_urls: Iterable[str]) -> Q:
    query = Q()
    for profile_url in profile_urls:
        normalized = normalize_profile_url(profile_url)
        if not normalized:
            continue
        prefix = normalized.rstrip("/") + "/"
        query |= Q(**{field_name: normalized})
        query |= Q(**{f"{field_name}__startswith": prefix})
    return query


def _as_value_list(value) -> list[str]:
    """Coerce a single value or an iterable of values into a plain list of strings."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value else []
    return [item for item in value if item]


def apply_timeline_filters(
    qs,
    *,
    source: str | Iterable[str] = "",
    author: str | Iterable[str] = "",
    categories: Iterable[str] | None = None,
    kinds: Iterable[str] | None = None,
):
    """
    Apply the non-spec source/author/category/kind filters to an Entry queryset.

    `source`/`author` accept a single value or an iterable of values (OR
    semantics across multiple values via `__in`). Shared by `_get_timeline`,
    `_content_search_qs`, and the admin timeline debug view so the three
    callers can't drift apart on filter semantics.

    If `kinds` is non-empty but none of the values map to a known kind, the
    queryset is filtered to empty rather than left unfiltered.
    """
    sources = [normalize_url(value) for value in _as_value_list(source)]
    sources = [value for value in sources if value]
    if sources:
        qs = qs.filter(source_url__in=sources)

    authors = [normalize_profile_url(value) for value in _as_value_list(author)]
    authors = [value for value in authors if value]
    if authors:
        qs = qs.filter(author_url__in=authors)

    normalized_categories = [normalize_category(value) for value in (categories or [])]
    normalized_categories = [value for value in normalized_categories if value]
    if normalized_categories:
        qs = qs.filter(search_categories__value__in=normalized_categories)

    normalized_kinds = normalize_repeated_values(kinds or [])
    if normalized_kinds:
        kind_query = Q()
        for kind in normalized_kinds:
            field_name = KIND_FIELD_MAP.get(kind)
            if field_name:
                kind_query |= Q(**{field_name: True})
        qs = qs.filter(kind_query) if kind_query else qs.none()

    if normalized_categories:
        qs = qs.distinct()

    return qs
