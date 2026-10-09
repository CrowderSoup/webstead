"""Resource indicators (RFC 8707): which resource server a token is for.

Only the MCP endpoint asks for audience-bound tokens. Micropub and other
IndieAuth clients keep getting tokens without a resource, as before.
"""
from __future__ import annotations

from urllib.parse import urlparse, urlunparse

from django.conf import settings

MCP_PATH = "/mcp"


def canonical(value: str) -> str:
    """Lowercase scheme and host, no trailing slash, no fragment. '' if invalid."""
    parsed = urlparse((value or "").strip())
    if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.fragment:
        return ""
    path = parsed.path.rstrip("/")
    return urlunparse((parsed.scheme.lower(), parsed.netloc.lower(), path, "", parsed.query, ""))


def site_origin(request) -> str:
    return canonical(request.build_absolute_uri("/"))


def mcp_resource(request) -> str:
    """The MCP endpoint's canonical URL, which MCP clients send as `resource`."""
    return f"{site_origin(request)}{MCP_PATH}"


def mcp_audiences(request) -> set[str]:
    """Resource values that mean "the MCP endpoint". The bare origin is what a
    client gets from the root protected-resource metadata document."""
    if not getattr(settings, "MCP_ENABLED", False):
        return set()
    return {mcp_resource(request), site_origin(request)}


def is_mcp_resource(request, value: str) -> bool:
    return bool(value) and canonical(value) in mcp_audiences(request)
