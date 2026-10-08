"""Tool registry for the MCP endpoint.

Each tool declares the scopes that unlock it (any one of them is enough).
``tools/list`` only shows a token the tools it can use, and handlers check
finer rules themselves (e.g. ``draft`` may only edit drafts).
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Callable

from django.core import signing

from blog.services import Actor


class ToolError(Exception):
    """A problem the model can act on. Comes back as an ``isError`` result."""


@dataclass
class ToolContext:
    actor: Actor
    token: object  # IndieAuthAccessToken
    scopes: set[str]
    base_url: str

    def has(self, *scopes: str) -> bool:
        return bool(self.scopes.intersection(scopes))

    def absolute(self, url: str) -> str:
        """Storage URLs are relative with local storage; agents need full ones."""
        return f"{self.base_url}{url}" if url.startswith("/") else url

    def require(self, *scopes: str, action: str) -> None:
        if not self.has(*scopes):
            needed = " or ".join(f"`{scope}`" for scope in scopes)
            raise ToolError(
                f"This token can't {action}: that needs the {needed} scope. "
                "Ask the site owner to do it, or to use a token with that scope."
            )


@dataclass(frozen=True)
class Tool:
    name: str
    title: str
    description: str
    input_schema: dict
    handler: Callable[[ToolContext, dict], dict]
    scopes: tuple[str, ...] = ()  # any one unlocks it; empty means any token
    annotations: dict = field(default_factory=dict)

    def visible_to(self, scopes: set[str]) -> bool:
        return not self.scopes or bool(scopes.intersection(self.scopes))

    def definition(self) -> dict:
        return {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "inputSchema": self.input_schema,
            "annotations": {"title": self.title, **self.annotations},
        }


REGISTRY: dict[str, Tool] = {}


def tool(*, name, title, description, input_schema, scopes=(), annotations=None):
    def register(handler):
        REGISTRY[name] = Tool(
            name=name,
            title=title,
            description=description,
            input_schema=input_schema,
            handler=handler,
            scopes=tuple(scopes),
            annotations=annotations or {},
        )
        return handler

    return register


READ_ONLY = {"readOnlyHint": True, "openWorldHint": False}
WRITE = {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False}
DESTRUCTIVE = {"readOnlyHint": False, "destructiveHint": True, "openWorldHint": False}

# Dry-run then confirm, for changes worth a second look.
CONFIRM_MAX_AGE = 600  # seconds
_CONFIRM_SALT = "mcp_server.confirm"


def _arguments_digest(tool_name: str, arguments: dict) -> str:
    canonical = json.dumps(
        {k: v for k, v in arguments.items() if k != "confirm_token"},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(f"{tool_name}:{canonical}".encode()).hexdigest()


def make_confirm_token(ctx: ToolContext, tool_name: str, arguments: dict) -> str:
    """Signed, 10-minute token bound to this access token and these exact arguments."""
    return signing.dumps(
        {"t": ctx.token.pk, "d": _arguments_digest(tool_name, arguments)},
        salt=_CONFIRM_SALT,
    )


def check_confirm_token(ctx: ToolContext, tool_name: str, arguments: dict) -> bool:
    """False when there's no confirm_token (so this is a dry run); raises
    ToolError when there's one that doesn't fit this call."""
    value = arguments.get("confirm_token")
    if not value:
        return False
    try:
        data = signing.loads(value, salt=_CONFIRM_SALT, max_age=CONFIRM_MAX_AGE)
    except signing.BadSignature:
        data = None
    if not data or data.get("t") != ctx.token.pk or data.get("d") != _arguments_digest(tool_name, arguments):
        raise ToolError(
            "confirm_token is expired or doesn't match these arguments. "
            "Call again without confirm_token to get a fresh plan and token."
        )
    return True


def load_tools():
    """Import the tool modules so they register themselves (in a fixed order)."""
    from . import site, content, media  # noqa: F401

    return REGISTRY
