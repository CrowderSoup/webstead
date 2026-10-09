"""The /mcp endpoint: MCP over Streamable HTTP, as a plain Django view.

Every POST carries one JSON-RPC message and gets one ``application/json``
reply; there are no sessions and no SSE streams. Both client eras are served
(see ``protocol``): modern requests are validated against their mirrored
headers, and legacy clients get an ``initialize`` handshake.
"""
from __future__ import annotations

import json
import logging
import time
from urllib.parse import urlparse

from django.conf import settings
from django.core.cache import cache
from django.http import Http404, HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt

from blog.services import Actor, ContentError
from indieauth import resources
from indieauth.tokens import active_local_token

from . import protocol as p
from .models import McpRequestLog
from .resources import INSTRUCTIONS, RESOURCES
from .schema import validate
from .tools import ToolContext, ToolError, load_tools

logger = logging.getLogger(__name__)

CAPABILITIES = {"tools": {}, "resources": {}}
SERVER_INFO = {"name": p.SERVER_NAME, "version": p.SERVER_VERSION, "title": "Webstead"}
# Scopes that mean anything here; a token needs at least one of them.
MCP_SCOPES = {"read", "draft", "create", "update", "delete", "undelete", "media"}
# What a new OAuth connection asks for: draft anything, publish from the
# preview link. The consent screen lets the owner grant more.
DEFAULT_SCOPE = "read draft media"


class _Call:
    """What gets logged about one request."""

    def __init__(self):
        self.started = time.monotonic()
        self.request_id = None
        self.method = ""
        self.tool = ""
        self.arguments = {}
        self.status = McpRequestLog.OK
        self.message = ""
        self.protocol_version = ""
        self.token = None
        self.client_name = ""

    def save(self, http_status):
        try:
            McpRequestLog.objects.create(
                method=self.method[:64],
                tool=self.tool[:128],
                arguments=self.arguments,
                status=self.status,
                http_status=http_status,
                message=self.message[:2000],
                duration_ms=int((time.monotonic() - self.started) * 1000),
                protocol_version=self.protocol_version[:32],
                token=self.token,
                client_id=self.token.client_id if self.token else "",
                client_name=self.client_name[:255],
            )
        except Exception:  # logging must never break the response
            logger.exception("Failed to write McpRequestLog")


@csrf_exempt
def mcp_endpoint(request):
    if not getattr(settings, "MCP_ENABLED", False):
        raise Http404
    if request.method != "POST":
        # No standalone SSE stream (GET) or session teardown (DELETE).
        response = HttpResponse(status=405)
        response["Allow"] = "POST"
        return response

    call = _Call()
    try:
        response = _handle(request, call)
    except p.McpError as exc:
        call.status = McpRequestLog.ERROR
        call.message = exc.message
        response = JsonResponse(exc.payload(call.request_id), status=exc.status)
        if exc.challenge:
            response["WWW-Authenticate"] = exc.challenge
    except Exception:
        logger.exception("MCP request failed")
        call.status = McpRequestLog.ERROR
        call.message = "internal error"
        error = p.McpError(p.INTERNAL_ERROR, "Internal error", status=500)
        response = JsonResponse(error.payload(call.request_id), status=500)
    call.save(response.status_code)
    return response


def _handle(request, call: _Call):
    _check_origin(request)
    # Authenticate before reading the body: any unauthenticated request gets
    # the 401 that starts an OAuth client's sign-in.
    token = _authenticate(request)
    call.token = token
    scopes = token.scopes & MCP_SCOPES

    message = _parse(request)
    call.request_id = message.get("id")
    call.method = message["method"]
    params = message.get("params") or {}
    if not isinstance(params, dict):
        raise p.McpError(p.INVALID_PARAMS, "params must be an object")

    if "id" not in message:
        # A notification (e.g. a legacy client's notifications/initialized).
        return HttpResponse(status=202)

    meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else {}
    if call.method == "initialize":
        modern = False
        call.protocol_version = _legacy_version(params.get("protocolVersion"))
        call.client_name = str((params.get("clientInfo") or {}).get("name", ""))
    elif p.META_PROTOCOL_VERSION in meta:
        modern = True
        call.protocol_version = _validate_modern(request, message, params, meta)
        call.client_name = str((meta.get(p.META_CLIENT_INFO) or {}).get("name", ""))
    else:
        modern = False
        call.protocol_version = _validate_legacy(request)

    ctx = ToolContext(
        actor=Actor(
            user=token.user,
            source="mcp",
            token_id=token.pk,
            client_id=token.client_id,
            base_url=request.build_absolute_uri("/").rstrip("/"),
        ),
        token=token,
        scopes=scopes,
        base_url=request.build_absolute_uri("/").rstrip("/"),
    )
    try:
        result = _dispatch(call, ctx, params)
    except p.McpError as exc:
        if not modern:
            # Handshake-era clients expect JSON-RPC errors on HTTP 200.
            exc.status = 200
        elif exc.code == p.METHOD_NOT_FOUND:
            exc.status = 404
        raise

    if modern:
        result.setdefault("resultType", "complete")
        result["_meta"] = {**result.get("_meta", {}), p.META_SERVER_INFO: SERVER_INFO}
    return JsonResponse({"jsonrpc": "2.0", "id": call.request_id, "result": result})


# transport checks ------------------------------------------------------


def _check_origin(request):
    """DNS-rebinding guard: a browser Origin must be this site."""
    origin = request.headers.get("Origin")
    if not origin:
        return
    allowed = {request.get_host()}
    allowed.update(urlparse(o).netloc for o in getattr(settings, "CSRF_TRUSTED_ORIGINS", []))
    if urlparse(origin).netloc not in allowed:
        raise p.McpError(p.INVALID_REQUEST, "Origin not allowed", status=403)


def _parse(request) -> dict:
    try:
        message = json.loads(request.body or b"")
    except (ValueError, UnicodeDecodeError) as exc:
        raise p.McpError(p.PARSE_ERROR, "Body must be a JSON-RPC message") from exc
    if isinstance(message, list):
        raise p.McpError(p.INVALID_REQUEST, "Batches aren't supported; send one message per POST")
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str):
        raise p.McpError(p.INVALID_REQUEST, "Not a JSON-RPC 2.0 request")
    if "id" in message and (message["id"] is None or isinstance(message["id"], (bool, float, dict, list))):
        raise p.McpError(p.INVALID_REQUEST, "id must be a string or an integer")
    return message


def resource_metadata_url(request) -> str:
    return f"{resources.site_origin(request)}/.well-known/oauth-protected-resource{resources.MCP_PATH}"


def _challenge(request, *, error="", description="", scope=DEFAULT_SCOPE) -> str:
    """RFC 6750/9728 WWW-Authenticate value that starts (or steps up) OAuth."""
    parts = [f'resource_metadata="{resource_metadata_url(request)}"', f'scope="{scope}"']
    if error:
        parts.append(f'error="{error}"')
    if description:
        parts.append(f'error_description="{description}"')
    return "Bearer " + ", ".join(parts)


def _authenticate(request):
    header = request.headers.get("Authorization", "")
    raw = header[7:].strip() if header.startswith("Bearer ") else ""
    if not raw:
        raise p.McpError(p.UNAUTHORIZED, "Missing bearer token", status=401, challenge=_challenge(request))
    # Only tokens this site issued: never remote introspection.
    token = active_local_token(raw)
    if token is None:
        message = "Invalid, expired, or revoked token"
        raise p.McpError(
            p.UNAUTHORIZED, message, status=401,
            challenge=_challenge(request, error="invalid_token", description=message),
        )
    # Audience (RFC 8707): OAuth tokens must have been issued for this
    # endpoint. Personal tokens are minted in the admin for MCP and scripts.
    if not token.is_personal and token.resource not in resources.mcp_audiences(request):
        message = "Token wasn't issued for this MCP server"
        raise p.McpError(
            p.UNAUTHORIZED, message, status=401,
            challenge=_challenge(request, error="invalid_token", description=message),
        )
    if not token.scopes & MCP_SCOPES:
        message = f"Token has no MCP scopes (needs one of: {', '.join(sorted(MCP_SCOPES))})"
        raise p.McpError(
            p.UNAUTHORIZED, message, status=403,
            challenge=_challenge(request, error="insufficient_scope", description="No MCP scopes"),
        )
    token.mark_used()
    return token


def protected_resource_metadata(request, *, at_root=False):
    """RFC 9728 metadata naming this site as the MCP endpoint's authorization server."""
    if not getattr(settings, "MCP_ENABLED", False):
        raise Http404
    from core.models import SiteConfiguration

    resource = resources.site_origin(request) if at_root else resources.mcp_resource(request)
    response = JsonResponse(
        {
            "resource": resource,
            "authorization_servers": [resources.site_origin(request)],
            "scopes_supported": sorted(MCP_SCOPES),
            "bearer_methods_supported": ["header"],
            "resource_name": SiteConfiguration.get_solo().title or "Webstead",
        }
    )
    response["Cache-Control"] = "public, max-age=3600"
    response["Access-Control-Allow-Origin"] = "*"
    return response


def _unsupported(requested):
    return p.McpError(
        p.UNSUPPORTED_PROTOCOL_VERSION,
        "Unsupported protocol version",
        data={"supported": list(p.SUPPORTED_VERSIONS), "requested": requested},
    )


def _validate_modern(request, message, params, meta) -> str:
    version = meta[p.META_PROTOCOL_VERSION]
    header = request.headers.get("MCP-Protocol-Version")
    if header is None:
        raise p.McpError(p.HEADER_MISMATCH, "MCP-Protocol-Version header is required")
    if header != version:
        raise p.McpError(
            p.HEADER_MISMATCH,
            f"Header mismatch: MCP-Protocol-Version '{header}' does not match body '{version}'",
        )
    if version not in p.SUPPORTED_VERSIONS:
        raise _unsupported(version)
    if p.META_CLIENT_CAPABILITIES not in meta:
        raise p.McpError(p.INVALID_PARAMS, f"_meta is missing {p.META_CLIENT_CAPABILITIES}")

    method_header = request.headers.get("Mcp-Method")
    if method_header is None:
        raise p.McpError(p.HEADER_MISMATCH, "Mcp-Method header is required")
    if method_header != message["method"]:
        raise p.McpError(
            p.HEADER_MISMATCH,
            f"Header mismatch: Mcp-Method '{method_header}' does not match body '{message['method']}'",
        )
    name_key = p.NAMED_METHODS.get(message["method"])
    if name_key:
        name_header = request.headers.get("Mcp-Name")
        if name_header is None:
            raise p.McpError(p.HEADER_MISMATCH, "Mcp-Name header is required")
        decoded = p.decode_header_value(name_header)
        if decoded != params.get(name_key):
            raise p.McpError(
                p.HEADER_MISMATCH,
                f"Header mismatch: Mcp-Name '{decoded}' does not match body '{params.get(name_key)}'",
            )
    return version


def _legacy_version(requested) -> str:
    return requested if requested in p.LEGACY_VERSIONS else p.LEGACY_VERSIONS[0]


def _validate_legacy(request) -> str:
    header = request.headers.get("MCP-Protocol-Version")
    if header in p.MODERN_VERSIONS:
        raise p.McpError(p.INVALID_PARAMS, f"_meta is missing {p.META_PROTOCOL_VERSION}")
    if header and header not in p.LEGACY_VERSIONS:
        raise _unsupported(header)
    # Before 2025-06-18 clients didn't send the header.
    return header or "2025-03-26"


# methods ---------------------------------------------------------------


def _dispatch(call: _Call, ctx: ToolContext, params: dict) -> dict:
    method = call.method
    if method == "initialize":
        return {
            "protocolVersion": call.protocol_version,
            "capabilities": CAPABILITIES,
            "serverInfo": SERVER_INFO,
            "instructions": INSTRUCTIONS,
        }
    if method == "server/discover":
        return {
            "supportedVersions": list(p.SUPPORTED_VERSIONS),
            "capabilities": CAPABILITIES,
            "instructions": INSTRUCTIONS,
        }
    if method == "ping":
        return {}
    if method == "tools/list":
        tools = load_tools()
        return {"tools": [t.definition() for t in tools.values() if t.visible_to(ctx.scopes)]}
    if method == "tools/call":
        return _call_tool(call, ctx, params)
    if method == "resources/list":
        return {"resources": [{k: v for k, v in r.items() if k != "text"} for r in RESOURCES.values()]}
    if method == "resources/templates/list":
        return {"resourceTemplates": []}
    if method == "resources/read":
        uri = params.get("uri")
        resource = RESOURCES.get(uri)
        if resource is None:
            raise p.McpError(p.INVALID_PARAMS, "Resource not found", data={"uri": uri})
        return {"contents": [{"uri": uri, "mimeType": resource["mimeType"], "text": resource["text"]}]}
    raise p.McpError(p.METHOD_NOT_FOUND, f"Method not found: {method}", status=404)


def _tool_result(call: _Call, data: dict, *, error=False) -> dict:
    call.message = str(data.get("message", ""))
    if error:
        call.status = McpRequestLog.TOOL_ERROR
        return {"content": [{"type": "text", "text": call.message}], "isError": True}
    return {
        "content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False, default=str)}],
        "structuredContent": data,
        "isError": False,
    }


def _call_tool(call: _Call, ctx: ToolContext, params: dict) -> dict:
    name = params.get("name")
    arguments = params.get("arguments") or {}
    tools = load_tools()
    tool = tools.get(name)
    if tool is None:
        raise p.McpError(p.INVALID_PARAMS, f"Unknown tool: {name}")
    if not isinstance(arguments, dict):
        raise p.McpError(p.INVALID_PARAMS, "arguments must be an object")
    call.tool = name
    call.arguments = _redact(arguments)

    if not tool.visible_to(ctx.scopes):
        needed = " or ".join(f"`{s}`" for s in tool.scopes)
        return _tool_result(call, {"message": f"This token can't use {name}: it needs the {needed} scope."}, error=True)
    limited = _rate_limited(ctx)
    if limited:
        return _tool_result(call, {"message": limited}, error=True)
    errors = validate(arguments, tool.input_schema)
    if errors:
        return _tool_result(call, {"message": "Invalid arguments: " + "; ".join(errors)}, error=True)
    try:
        data = tool.handler(ctx, arguments)
    except (ToolError, ContentError) as exc:
        return _tool_result(call, {"message": str(exc)}, error=True)
    return _tool_result(call, data)


def _rate_limited(ctx: ToolContext) -> str:
    limit = getattr(settings, "MCP_RATE_LIMIT", 60)
    window = int(time.time() // 60)
    key = f"mcp:rate:{ctx.token.pk}:{window}"
    cache.add(key, 0, timeout=120)
    try:
        count = cache.incr(key)
    except ValueError:
        cache.set(key, 1, timeout=120)
        count = 1
    if count > limit:
        wait = 60 - int(time.time()) % 60
        return f"Rate limit: {limit} tool calls a minute for this token. Try again in {wait} seconds."
    return ""


def _redact(value, key=""):
    if key == "data_base64" and isinstance(value, str):
        return f"<{len(value)} chars of base64>"
    if key == "confirm_token":
        return "<redacted>"
    if isinstance(value, dict):
        return {k: _redact(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(v) for v in value[:50]]
    if isinstance(value, str) and len(value) > 500:
        return value[:500] + "…"
    return value
