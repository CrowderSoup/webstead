"""upload_media: images only in v1."""
from __future__ import annotations

import base64
import binascii
import os
from urllib.parse import urljoin, urlparse

import requests
from django.conf import settings
from django.core.files.base import ContentFile

from blog import services

from . import WRITE, ToolError, tool

MAX_REDIRECTS = 3
FETCH_TIMEOUT = 15  # seconds

# (magic-bytes check, extension, MIME type)
IMAGE_SIGNATURES = (
    (lambda b: b.startswith(b"\xff\xd8\xff"), ".jpg", "image/jpeg"),
    (lambda b: b.startswith(b"\x89PNG\r\n\x1a\n"), ".png", "image/png"),
    (lambda b: b[:6] in (b"GIF87a", b"GIF89a"), ".gif", "image/gif"),
    (lambda b: b[:4] == b"RIFF" and b[8:12] == b"WEBP", ".webp", "image/webp"),
)


def _max_bytes() -> int:
    return getattr(settings, "MCP_MAX_UPLOAD_BYTES", 10 * 1024 * 1024)


def _sniff(data: bytes):
    for matches, ext, mime in IMAGE_SIGNATURES:
        if matches(data):
            return ext, mime
    raise ToolError("Only JPEG, PNG, GIF, and WebP images can be uploaded.")


def _fetch(url: str) -> bytes:
    """GET a public URL, re-checking the host on every redirect (SSRF guard)."""
    from indieauth.views import _is_public_host

    limit = _max_bytes()
    for _ in range(MAX_REDIRECTS + 1):
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not _is_public_host(parsed.hostname):
            raise ToolError("url must be a public http(s) URL.")
        try:
            response = requests.get(url, stream=True, timeout=FETCH_TIMEOUT, allow_redirects=False)
        except requests.RequestException as exc:
            raise ToolError(f"Couldn't fetch that URL: {exc.__class__.__name__}.") from exc
        with response:
            if response.is_redirect:
                url = urljoin(url, response.headers.get("Location", ""))
                continue
            if response.status_code != 200:
                raise ToolError(f"Fetching that URL returned HTTP {response.status_code}.")
            data = bytearray()
            for chunk in response.iter_content(64 * 1024):
                data.extend(chunk)
                if len(data) > limit:
                    raise ToolError(f"That image is over the {limit // (1024 * 1024)} MB limit.")
            return bytes(data)
    raise ToolError("Too many redirects.")


@tool(
    name="upload_media",
    title="Upload an image",
    description=(
        "Upload an image (JPEG, PNG, GIF, or WebP) from a public URL or base64 data. Returns "
        "a url to pass in create_post/update_post photos. Always give alt text that "
        "describes the image for someone who can't see it."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "A public image URL to fetch."},
            "data_base64": {"type": "string", "description": "The image bytes, base64-encoded."},
            "filename": {"type": "string"},
            "alt": {"type": "string", "minLength": 1, "description": "Alt text."},
        },
        "required": ["alt"],
        "additionalProperties": False,
    },
    scopes=["media", "create"],
    annotations={**WRITE, "openWorldHint": True},
)
def upload_media(ctx, arguments):
    url, encoded = arguments.get("url"), arguments.get("data_base64")
    if bool(url) == bool(encoded):
        raise ToolError("Pass exactly one of url or data_base64.")
    if encoded:
        if len(encoded) > _max_bytes() * 4 // 3 + 4:
            raise ToolError(f"That image is over the {_max_bytes() // (1024 * 1024)} MB limit.")
        try:
            data = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ToolError("data_base64 isn't valid base64.") from exc
    else:
        data = _fetch(url)
    if not data:
        raise ToolError("The image is empty.")
    if len(data) > _max_bytes():
        raise ToolError(f"That image is over the {_max_bytes() // (1024 * 1024)} MB limit.")

    ext, mime = _sniff(data)
    stem = os.path.splitext(os.path.basename(arguments.get("filename") or urlparse(url or "").path))[0]
    asset = services.create_media(ctx.actor, ContentFile(data, name=f"{stem or 'image'}{ext}"), alt=arguments["alt"])
    return {
        "message": "Uploaded. Pass url in create_post or update_post photos.",
        "id": asset.pk,
        "url": ctx.absolute(asset.file.url),
        "alt": asset.alt_text,
        "mime_type": mime,
        "bytes": len(data),
    }
