import logging
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from typing import Iterable, Optional

from django.conf import settings
from django.utils.encoding import force_str

from blog.models import Post
from .models import Webmention

logger = logging.getLogger(__name__)

BRIDGY_PUBLISH_TARGETS = (
    ("bridgy_publish_bluesky", "https://brid.gy/publish/bluesky"),
    ("bridgy_publish_flickr", "https://brid.gy/publish/flickr"),
    ("bridgy_publish_github", "https://brid.gy/publish/github"),
)


class _WebmentionDiscoveryParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.endpoint: Optional[str] = None

    def handle_starttag(self, tag, attrs):
        if self.endpoint:
            return
        rels = []
        href = None
        for key, value in attrs:
            if key.lower() == "rel" and value:
                rels = [r.strip().lower() for r in value.split()]
            elif key.lower() == "href":
                href = value
        if href and "webmention" in rels:
            self.endpoint = href


class _WebmentionLinkParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() != "a":
            return
        for key, value in attrs:
            if key.lower() == "href" and value:
                self.links.append(value)
                break


def _parse_link_header(header_value: str) -> Optional[str]:
    # Basic Link header parsing to find rel="webmention"
    # Split only on commas followed by '<' to handle URLs with commas in query strings
    for part in re.split(r",\s*(?=<)", header_value):
        segment = part.strip()
        if not segment.startswith("<") or ">" not in segment:
            continue
        url, _, params = segment.partition(">")
        rel = None
        for param in params.split(";"):
            name, _, value = param.strip().partition("=")
            if name.lower() == "rel":
                rel = value.strip('"')
                break
        if rel and "webmention" in rel.split():
            return url[1:]
    return None


def _normalize_url_for_compare(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    scheme = parsed.scheme.lower()
    netloc = parsed.netloc.lower()
    path = parsed.path or "/"
    if not path.endswith("/"):
        path = f"{path}/"
    return urllib.parse.urlunparse((scheme, netloc, path, "", "", ""))


def discover_webmention_endpoint(target_url: str) -> Optional[str]:
    request = urllib.request.Request(target_url, headers={"User-Agent": "django-blog-webmention"})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            link_header = response.headers.get("Link")
            if link_header:
                endpoint = _parse_link_header(link_header)
                if endpoint:
                    return urllib.parse.urljoin(target_url, endpoint)

            content_type = response.headers.get("Content-Type", "")
            if "html" not in content_type:
                return None

            body = force_str(response.read(), errors="ignore")
    except (urllib.error.HTTPError, urllib.error.URLError, ValueError):
        return None

    parser = _WebmentionDiscoveryParser()
    parser.feed(body)
    if parser.endpoint:
        return urllib.parse.urljoin(target_url, parser.endpoint)
    return None


def verify_webmention_source(source_url: str, target_url: str) -> tuple[bool, str, bool]:
    parsed_source = urllib.parse.urlparse(source_url)
    if parsed_source.scheme not in ("http", "https"):
        return False, "Unsupported source scheme", False

    request = urllib.request.Request(source_url, headers={"User-Agent": "django-blog-webmention"})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            content_type = response.headers.get("Content-Type", "")
            if "html" not in content_type:
                return False, "Source is not HTML", False
            body = force_str(response.read(), errors="ignore")
    except urllib.error.HTTPError as exc:
        if exc.code == 410:
            return False, "Source is gone (410)", False  # REJECTED, not PENDING
        return False, str(exc), True
    except (urllib.error.URLError, TimeoutError, socket.timeout, ValueError) as exc:
        return False, str(exc), True

    parser = _WebmentionLinkParser()
    parser.feed(body)
    if not parser.links:
        return False, "No links found", False

    normalized_target = _normalize_url_for_compare(target_url)
    for href in parser.links:
        resolved = urllib.parse.urljoin(source_url, href)
        if _normalize_url_for_compare(resolved) == normalized_target:
            return True, "", False

    return False, "Source does not link to target", False


def _extract_targets(post: Post) -> Iterable[str]:
    links = set()
    for field in [post.like_of, post.repost_of, post.in_reply_to, post.bookmark_of]:
        if field:
            links.add(field)

    url_pattern = re.compile(r"https?://[^\s)]+")
    for url in url_pattern.findall(post.content or ""):
        cleaned = url.rstrip(".,;:)")
        if cleaned:
            links.add(cleaned)
    return links


def _post_from_url(url: str) -> Optional[Post]:
    if not url:
        return None
    parsed = urllib.parse.urlparse(url)
    slug = parsed.path.rstrip("/").split("/")[-1]
    if not slug:
        return None
    try:
        return Post.objects.get(slug=slug, deleted=False)
    except Post.DoesNotExist:
        return None


def _is_globally_blocked_target(target_url: str) -> bool:
    from microsub.models import BlockedUser
    from microsub.utils import url_matches_profile_prefix

    blocked_urls = BlockedUser.objects.filter(channel__isnull=True).values_list("url", flat=True)
    return any(url_matches_profile_prefix(blocked_url, target_url) for blocked_url in blocked_urls)


def _send_webmention_request(
    source_url: str,
    target_url: str,
    mention_type: str = Webmention.MENTION,
    *,
    include_wm_property: bool = True,
) -> tuple[str, str]:
    endpoint = discover_webmention_endpoint(target_url)
    if not endpoint:
        return Webmention.REJECTED, "No webmention endpoint found"

    params = {"source": source_url, "target": target_url}
    if include_wm_property:
        params["wm-property"] = mention_type
    data = urllib.parse.urlencode(params).encode()
    send_request = urllib.request.Request(
        endpoint,
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": "django-blog-webmention"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(send_request, timeout=10) as response:
            body = response.read()
            body_preview = body[:2000].decode("utf-8", errors="replace") if body else ""
            logger.info(
                "Webmention response received",
                extra={
                    "webmention_source": source_url,
                    "webmention_target": target_url,
                    "webmention_endpoint": endpoint,
                    "webmention_status": response.status,
                    "webmention_body": body_preview,
                },
            )
            if response.status == 202:
                return Webmention.PENDING, ""
            if response.status in (200, 201):
                return Webmention.ACCEPTED, ""
            return Webmention.REJECTED, f"Unexpected status {response.status}"
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, socket.timeout, ValueError) as exc:
        error_status = getattr(exc, "code", None)
        error_body = ""
        if isinstance(exc, urllib.error.HTTPError):
            try:
                error_body = exc.read().decode("utf-8", errors="replace")
            except Exception:
                error_body = ""
        status = Webmention.REJECTED
        if isinstance(exc, (TimeoutError, socket.timeout)):
            status = Webmention.TIMED_OUT
        if not settings.RUNNING_TESTS:
            logger.info(
                "Webmention request failed",
                extra={
                    "webmention_source": source_url,
                    "webmention_target": target_url,
                    "webmention_endpoint": endpoint,
                    "webmention_status": error_status,
                    "webmention_error": str(exc),
                    "webmention_body": error_body[:2000],
                },
            )
        return status, str(exc)


def send_webmention(
    source_url: str,
    target_url: str,
    *,
    mention_type: str = Webmention.MENTION,
    local_post: Optional[Post] = None,
) -> Webmention:
    if _is_globally_blocked_target(target_url):
        if not local_post:
            local_post = _post_from_url(source_url)
        mention_type = mention_type if mention_type in dict(Webmention.MENTION_CHOICES) else Webmention.MENTION
        return Webmention.objects.create(
            source=source_url,
            target=target_url,
            mention_type=mention_type,
            status=Webmention.REJECTED,
            target_post=local_post,
            error="Target blocked by Microsub global block",
            is_incoming=False,
        )

    status, error = _send_webmention_request(source_url, target_url, mention_type)
    if status == Webmention.REJECTED and "No webmention endpoint" not in error:
        retry_status, retry_error = _send_webmention_request(
            source_url, target_url, mention_type, include_wm_property=False
        )
        if retry_status in (Webmention.ACCEPTED, Webmention.PENDING):
            status, error = retry_status, retry_error
    if not local_post:
        local_post = _post_from_url(source_url)
    mention_type = mention_type if mention_type in dict(Webmention.MENTION_CHOICES) else Webmention.MENTION
    return Webmention.objects.create(
        source=source_url,
        target=target_url,
        mention_type=mention_type,
        status=status,
        # For outgoing webmentions, target_post is the local post that sent the mention.
        target_post=local_post,
        error=error,
        is_incoming=False,
    )


def resend_webmention(webmention: Webmention) -> Webmention:
    if _is_globally_blocked_target(webmention.target):
        webmention.status = Webmention.REJECTED
        webmention.error = "Target blocked by Microsub global block"
        webmention.save(update_fields=["status", "error", "updated_at"])
        return webmention

    status, error = _send_webmention_request(webmention.source, webmention.target, webmention.mention_type)
    if status == Webmention.REJECTED and "No webmention endpoint" not in error:
        retry_status, retry_error = _send_webmention_request(
            webmention.source, webmention.target, webmention.mention_type, include_wm_property=False
        )
        if retry_status in (Webmention.ACCEPTED, Webmention.PENDING):
            status, error = retry_status, retry_error
    webmention.status = status
    webmention.error = error
    webmention.save(update_fields=["status", "error", "updated_at"])
    return webmention


def _resolve_mention_type(post: Post, target: str) -> str:
    if target == post.like_of:
        return Webmention.LIKE
    if target == post.repost_of:
        return Webmention.REPOST
    if target == post.in_reply_to:
        return Webmention.REPLY
    if target == post.bookmark_of:
        return Webmention.BOOKMARK
    return Webmention.MENTION


def send_webmentions_for_post(post: Post, source_url: str) -> None:
    source_host = urllib.parse.urlparse(source_url).netloc
    targets = [
        url
        for url in _extract_targets(post)
        if urllib.parse.urlparse(url).netloc != source_host and not _is_globally_blocked_target(url)
    ]
    existing_targets = set()
    if targets:
        existing_targets = set(
            Webmention.objects.filter(source=source_url, target__in=targets)
            .exclude(status__in=[Webmention.REJECTED, Webmention.TIMED_OUT])
            .values_list("target", flat=True)
        )

    for target in targets:
        if target in existing_targets:
            continue
        mention_type = Webmention.MENTION
        if target == post.like_of:
            mention_type = Webmention.LIKE
        elif target == post.repost_of:
            mention_type = Webmention.REPOST
        elif target == post.in_reply_to:
            mention_type = Webmention.REPLY
        elif target == post.bookmark_of:
            mention_type = Webmention.BOOKMARK

        send_webmention(
            source_url,
            target,
            mention_type=mention_type,
            local_post=post,
        )


def _bridgy_publish_targets(settings_obj) -> list[str]:
    if not settings_obj:
        return []
    targets = []
    for field_name, target_url in BRIDGY_PUBLISH_TARGETS:
        if getattr(settings_obj, field_name, False):
            targets.append(target_url)
    return targets


def send_bridgy_publish_webmentions(post: Post, source_url: str, settings_obj) -> None:
    targets = [target for target in _bridgy_publish_targets(settings_obj) if not _is_globally_blocked_target(target)]
    if not targets:
        return
    existing_targets = set(
        Webmention.objects.filter(source=source_url, target__in=targets).values_list("target", flat=True)
    )
    for target in targets:
        if target in existing_targets:
            continue
        send_webmention(
            source_url,
            target,
            mention_type=Webmention.MENTION,
            local_post=post,
        )


def queue_webmentions_for_post(
    post: Post,
    source_url: str,
    *,
    include_bridgy: bool = False,
    settings_obj=None,
) -> None:
    from micropub.tasks import dispatch_webmentions

    dispatch_webmentions.delay(post.id, source_url, include_bridgy=include_bridgy)

    # Dispatch Mastodon syndication unconditionally — the task itself checks
    # _should_syndicate() and idempotency, so it is safe to call on every save.
    try:
        from mastodon_integration.tasks import publish_post_to_mastodon
        publish_post_to_mastodon.delay(post.id)
    except Exception:
        import logging
        logging.getLogger(__name__).exception(
            "queue_webmentions_for_post: failed to dispatch publish_post_to_mastodon"
        )
