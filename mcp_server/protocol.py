"""Model Context Protocol constants and wire helpers.

Pinned to MCP revision 2026-07-28 (checked against
https://modelcontextprotocol.io/specification/2026-07-28 on 2026-10-07).
That revision is stateless: every request carries its protocol version and
client capabilities in ``params._meta``, and Streamable HTTP mirrors them
into headers. Older clients open with an ``initialize`` handshake instead,
so the endpoint serves both eras ("dual-era"), without sessions.
"""
from __future__ import annotations

import base64

MODERN_VERSION = "2026-07-28"
MODERN_VERSIONS = (MODERN_VERSION,)
# Handshake-era revisions we still answer. The newest is offered when an
# initialize asks for one we don't know.
LEGACY_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26")
SUPPORTED_VERSIONS = MODERN_VERSIONS + LEGACY_VERSIONS

META_PROTOCOL_VERSION = "io.modelcontextprotocol/protocolVersion"
META_CLIENT_INFO = "io.modelcontextprotocol/clientInfo"
META_CLIENT_CAPABILITIES = "io.modelcontextprotocol/clientCapabilities"
META_SERVER_INFO = "io.modelcontextprotocol/serverInfo"

SERVER_NAME = "webstead"
SERVER_VERSION = "1.0.0"

# JSON-RPC
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
# MCP (2026-07-28)
HEADER_MISMATCH = -32020
UNSUPPORTED_PROTOCOL_VERSION = -32022
# Webstead, outside the JSON-RPC reserved range
UNAUTHORIZED = -31001

# Methods whose Streamable HTTP request must carry an Mcp-Name header,
# and the params key it mirrors.
NAMED_METHODS = {"tools/call": "name", "resources/read": "uri", "prompts/get": "name"}


class McpError(Exception):
    """A JSON-RPC error response, with the HTTP status to send it with."""

    def __init__(self, code: int, message: str, *, status: int = 400, data=None, challenge: str = ""):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.data = data
        self.challenge = challenge  # WWW-Authenticate value for 401/403

    def payload(self, request_id=None) -> dict:
        error = {"code": self.code, "message": self.message}
        if self.data is not None:
            error["data"] = self.data
        body = {"jsonrpc": "2.0", "error": error}
        if request_id is not None:
            body["id"] = request_id
        return body


def decode_header_value(value: str) -> str:
    """Undo the ``=?base64?…?=`` sentinel encoding used for Mcp-Name values."""
    if value.startswith("=?base64?") and value.endswith("?="):
        try:
            return base64.b64decode(value[len("=?base64?"):-2], validate=True).decode("utf-8")
        except (ValueError, UnicodeDecodeError) as exc:
            raise McpError(HEADER_MISMATCH, "Mcp-Name header has invalid base64 encoding") from exc
    return value
