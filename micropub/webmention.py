import logging
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from typing import Iterable, Optional

from django.utils.encoding import force_str

from blog.models import Post
from .models import Webmention

logger = logging.getLogger(__name__)


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


def _parse_link_header(header_value: str) -> Optional[str]:
    # Basic Link header parsing to find rel="webmention"
    for part in header_value.split(","):
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


def _extract_targets(post: Post) -> Iterable[str]:
    links = set()
    for field in [post.like_of, post.repost_of, post.in_reply_to]:
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
        return Post.objects.get(slug=slug)
    except Post.DoesNotExist:
        return None


def _send_webmention_request(source_url: str, target_url: str) -> tuple[str, str]:
    endpoint = discover_webmention_endpoint(target_url)
    if not endpoint:
        return Webmention.REJECTED, "No webmention endpoint found"

    data = urllib.parse.urlencode({"source": source_url, "target": target_url}).encode()
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
    source_post: Optional[Post] = None,
) -> Webmention:
    status, error = _send_webmention_request(source_url, target_url)
    if not source_post:
        source_post = _post_from_url(source_url)
    mention_type = mention_type if mention_type in dict(Webmention.MENTION_CHOICES) else Webmention.MENTION
    return Webmention.objects.create(
        source=source_url,
        target=target_url,
        mention_type=mention_type,
        status=status,
        target_post=source_post,
        error=error,
    )


def resend_webmention(webmention: Webmention) -> Webmention:
    status, error = _send_webmention_request(webmention.source, webmention.target)
    webmention.status = status
    webmention.error = error
    webmention.save(update_fields=["status", "error", "updated_at"])
    return webmention


def send_webmentions_for_post(post: Post, source_url: str) -> None:
    source_host = urllib.parse.urlparse(source_url).netloc
    targets = [url for url in _extract_targets(post) if urllib.parse.urlparse(url).netloc != source_host]
    existing_targets = set()
    if targets:
        existing_targets = set(
            Webmention.objects.filter(source=source_url, target__in=targets).values_list("target", flat=True)
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

        send_webmention(
            source_url,
            target,
            mention_type=mention_type,
            source_post=post,
        )
