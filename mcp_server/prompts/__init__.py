"""Prompt registry for the MCP endpoint.

Each prompt declares the scopes that unlock it (any one of them is enough).
``prompts/list`` only shows a token the prompts it can use. ``prompts/get``
fills the prompt with this site's data and returns messages for the client
model. Prompts do not publish posts or activate themes.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from mcp_server.tools import ToolContext


@dataclass(frozen=True)
class Prompt:
    name: str
    title: str
    description: str
    handler: Callable[[ToolContext, dict], dict]
    arguments: tuple[dict, ...] = ()
    input_schema: dict = None  # set by the decorator
    scopes: tuple[str, ...] = ()  # any one unlocks it; empty means any token

    def visible_to(self, scopes: set[str]) -> bool:
        return not self.scopes or bool(scopes.intersection(self.scopes))

    def definition(self) -> dict:
        data = {"name": self.name, "title": self.title, "description": self.description}
        if self.arguments:
            data["arguments"] = [dict(arg) for arg in self.arguments]
        return data


REGISTRY: dict[str, Prompt] = {}


def _schema(arguments: tuple[dict, ...]) -> dict:
    properties = {}
    required = []
    for arg in arguments:
        properties[arg["name"]] = {"type": "string", "minLength": 1 if arg.get("required") else 0}
        if arg.get("required"):
            required.append(arg["name"])
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def prompt(*, name, title, description, arguments=(), scopes=()):
    argument_list = tuple(arguments)

    def register(handler):
        REGISTRY[name] = Prompt(
            name=name,
            title=title,
            description=description,
            handler=handler,
            arguments=argument_list,
            input_schema=_schema(argument_list),
            scopes=tuple(scopes),
        )
        return handler

    return register


def messages(description: str, text: str) -> dict:
    return {
        "description": description,
        "messages": [{"role": "user", "content": {"type": "text", "text": text}}],
    }


def load_prompts():
    """Import the prompt modules so they register themselves (in a fixed order)."""
    from . import content, themes  # noqa: F401

    return REGISTRY
