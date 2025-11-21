import re
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from typing import Iterable, Optional

from django.utils.encoding import force_str

from blog.models import Post
from .models import Webmention


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


def send_webmentions_for_post(post: Post, source_url: str) -> None:
    source_host = urllib.parse.urlparse(source_url).netloc
    targets = [url for url in _extract_targets(post) if urllib.parse.urlparse(url).netloc != source_host]

    for target in targets:
        endpoint = discover_webmention_endpoint(target)
        mention_type = Webmention.MENTION
        if target == post.like_of:
            mention_type = Webmention.LIKE
        elif target == post.repost_of:
            mention_type = Webmention.REPOST
        elif target == post.in_reply_to:
            mention_type = Webmention.REPLY

        status = Webmention.PENDING
        error = ""
        if not endpoint:
            status = Webmention.REJECTED
            error = "No webmention endpoint found"
        else:
            data = urllib.parse.urlencode({"source": source_url, "target": target}).encode()
            send_request = urllib.request.Request(
                endpoint,
                data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": "django-blog-webmention"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(send_request, timeout=10) as response:
                    if response.status in (200, 201, 202):
                        status = Webmention.ACCEPTED
                    else:
                        status = Webmention.REJECTED
                        error = f"Unexpected status {response.status}"
            except (urllib.error.HTTPError, urllib.error.URLError, ValueError) as exc:
                status = Webmention.REJECTED
                error = str(exc)

        Webmention.objects.create(
            source=source_url,
            target=target,
            mention_type=mention_type,
            status=status,
            target_post=post,
            error=error,
        )
