import base64
import json
import tempfile
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone

from blog.models import Post
from files.models import File
from indieauth.tokens import mint_personal_token
from mcp_server import protocol as p
from mcp_server.models import McpRequestLog

MODERN = p.MODERN_VERSION
# 1x1 transparent PNG
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)


@override_settings(MCP_ENABLED=True, MCP_RATE_LIMIT=60)
class McpTestCase(TestCase):
    scopes = ("read", "draft", "create", "update", "delete", "undelete", "media")

    def setUp(self):
        cache.clear()
        queue_patcher = patch("micropub.webmention.queue_webmentions_for_post")
        self.queue = queue_patcher.start()
        self.addCleanup(queue_patcher.stop)
        media_root = tempfile.TemporaryDirectory()
        self.addCleanup(media_root.cleanup)
        media_override = override_settings(MEDIA_ROOT=media_root.name)
        media_override.enable()
        self.addCleanup(media_override.disable)
        self.user = get_user_model().objects.create_user(username="owner", password="pw")
        self.token, self.raw = self.mint(self.scopes)
        self._ids = iter(range(1, 10_000))

    def mint(self, scopes, **kwargs):
        return mint_personal_token(self.user, name="test", scopes=scopes, me="http://testserver/", **kwargs)

    def post(self, body, *, raw_token=None, headers=None):
        all_headers = {"Authorization": f"Bearer {raw_token or self.raw}"}
        all_headers.update(headers or {})
        return self.client.post(
            "/mcp",
            data=json.dumps(body),
            content_type="application/json",
            headers={k: v for k, v in all_headers.items() if v is not None},
        )

    def modern(self, method, params=None, *, raw_token=None, headers=None, meta=None):
        params = dict(params or {})
        params["_meta"] = meta if meta is not None else {
            p.META_PROTOCOL_VERSION: MODERN,
            p.META_CLIENT_INFO: {"name": "test-client", "version": "1.0"},
            p.META_CLIENT_CAPABILITIES: {},
        }
        all_headers = {"MCP-Protocol-Version": MODERN, "Mcp-Method": method}
        key = p.NAMED_METHODS.get(method)
        if key:
            all_headers["Mcp-Name"] = params.get(key)
        all_headers.update(headers or {})
        return self.post(
            {"jsonrpc": "2.0", "id": next(self._ids), "method": method, "params": params},
            raw_token=raw_token,
            headers=all_headers,
        )

    def call(self, name, arguments=None, **kwargs):
        response = self.modern("tools/call", {"name": name, "arguments": arguments or {}}, **kwargs)
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()["result"]

    def ok(self, name, arguments=None, **kwargs):
        result = self.call(name, arguments, **kwargs)
        self.assertFalse(result["isError"], result["content"][0]["text"])
        self.assertEqual(json.loads(result["content"][0]["text"]), result["structuredContent"])
        return result["structuredContent"]

    def error(self, name, arguments=None, **kwargs):
        result = self.call(name, arguments, **kwargs)
        self.assertTrue(result["isError"], result)
        return result["content"][0]["text"]


class McpTransportTests(McpTestCase):
    def test_disabled_returns_404(self):
        with override_settings(MCP_ENABLED=False):
            self.assertEqual(self.modern("server/discover").status_code, 404)

    def test_get_and_delete_are_not_allowed(self):
        for method in (self.client.get, self.client.delete):
            response = method("/mcp")
            self.assertEqual(response.status_code, 405)
            self.assertEqual(response["Allow"], "POST")

    def test_missing_token_is_401_and_logged(self):
        response = self.client.post(
            "/mcp",
            data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 401)
        self.assertIn("Bearer", response["WWW-Authenticate"])
        log = McpRequestLog.objects.get()
        self.assertEqual((log.status, log.http_status), (McpRequestLog.ERROR, 401))

    def test_revoked_expired_and_unknown_tokens_are_401(self):
        revoked, revoked_raw = self.mint(["read"])
        revoked.revoked_at = timezone.now()
        revoked.save()
        _, expired_raw = self.mint(["read"], expires_in=timezone.timedelta(seconds=-1))

        for raw in (revoked_raw, expired_raw, "not-a-token"):
            self.assertEqual(self.modern("ping", raw_token=raw).status_code, 401)

    def test_token_without_mcp_scopes_is_403(self):
        _, raw = self.mint(["channels", "follow"])

        response = self.modern("ping", raw_token=raw)

        self.assertEqual(response.status_code, 403)
        self.assertIn("insufficient_scope", response["WWW-Authenticate"])

    def test_foreign_origin_is_403(self):
        self.assertEqual(self.modern("ping", headers={"Origin": "https://evil.example"}).status_code, 403)
        self.assertEqual(self.modern("ping", headers={"Origin": "http://testserver"}).status_code, 200)

    def test_marks_token_used(self):
        self.modern("ping")

        self.token.refresh_from_db()
        self.assertIsNotNone(self.token.last_used_at)

    def test_discover(self):
        result = self.modern("server/discover").json()["result"]

        self.assertEqual(result["resultType"], "complete")
        self.assertIn(MODERN, result["supportedVersions"])
        self.assertIn("2025-11-25", result["supportedVersions"])
        self.assertEqual(set(result["capabilities"]), {"tools", "resources"})
        self.assertEqual(result["_meta"][p.META_SERVER_INFO]["name"], "webstead")
        self.assertIn("get_site", result["instructions"])

    def test_header_validation(self):
        cases = [
            {"MCP-Protocol-Version": None},
            {"MCP-Protocol-Version": "2025-11-25"},
            {"Mcp-Method": None},
            {"Mcp-Method": "tools/list"},
        ]
        for headers in cases:
            response = self.modern("ping", headers=headers)
            self.assertEqual(response.status_code, 400, headers)
            self.assertEqual(response.json()["error"]["code"], p.HEADER_MISMATCH, headers)

        response = self.modern("tools/call", {"name": "get_site"}, headers={"Mcp-Name": "search_posts"})
        self.assertEqual(response.json()["error"]["code"], p.HEADER_MISMATCH)
        response = self.modern("tools/call", {"name": "get_site"}, headers={"Mcp-Name": None})
        self.assertEqual(response.json()["error"]["code"], p.HEADER_MISMATCH)

    def test_base64_encoded_name_header(self):
        encoded = "=?base64?" + base64.b64encode(b"get_site").decode() + "?="

        response = self.modern("tools/call", {"name": "get_site"}, headers={"Mcp-Name": encoded})

        self.assertEqual(response.status_code, 200)

    def test_unsupported_version(self):
        meta = {p.META_PROTOCOL_VERSION: "1900-01-01", p.META_CLIENT_CAPABILITIES: {}}

        response = self.modern("ping", meta=meta, headers={"MCP-Protocol-Version": "1900-01-01"})

        self.assertEqual(response.status_code, 400)
        error = response.json()["error"]
        self.assertEqual(error["code"], p.UNSUPPORTED_PROTOCOL_VERSION)
        self.assertEqual(error["data"]["requested"], "1900-01-01")
        self.assertIn(MODERN, error["data"]["supported"])

    def test_missing_client_capabilities_is_invalid_params(self):
        response = self.modern("ping", meta={p.META_PROTOCOL_VERSION: MODERN})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], p.INVALID_PARAMS)

    def test_unknown_method_is_404(self):
        response = self.modern("sampling/createMessage")

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error"]["code"], p.METHOD_NOT_FOUND)

    def test_malformed_bodies(self):
        response = self.client.post(
            "/mcp", data="{", content_type="application/json", headers={"Authorization": f"Bearer {self.raw}"}
        )
        self.assertEqual(response.json()["error"]["code"], p.PARSE_ERROR)
        response = self.post([{"jsonrpc": "2.0", "id": 1, "method": "ping"}])
        self.assertEqual(response.json()["error"]["code"], p.INVALID_REQUEST)

    def test_legacy_handshake_session(self):
        """A handshake-era client: initialize, initialized, then plain requests."""
        init = self.post(
            {
                "jsonrpc": "2.0",
                "id": 0,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "legacy-client", "version": "0.9"},
                },
            }
        )
        self.assertEqual(init.status_code, 200)
        result = init.json()["result"]
        self.assertEqual(result["protocolVersion"], "2025-06-18")
        self.assertEqual(result["serverInfo"]["name"], "webstead")
        self.assertNotIn("Mcp-Session-Id", init)
        self.assertNotIn("resultType", result)

        initialized = self.post(
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            headers={"MCP-Protocol-Version": "2025-06-18"},
        )
        self.assertEqual(initialized.status_code, 202)

        tools = self.post(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={"MCP-Protocol-Version": "2025-06-18"},
        ).json()["result"]["tools"]
        self.assertIn("create_post", [t["name"] for t in tools])

        unknown = self.post(
            {"jsonrpc": "2.0", "id": 2, "method": "nope"}, headers={"MCP-Protocol-Version": "2025-06-18"}
        )
        self.assertEqual(unknown.status_code, 200)
        self.assertEqual(unknown.json()["error"]["code"], p.METHOD_NOT_FOUND)
        self.assertEqual(
            McpRequestLog.objects.filter(client_name="legacy-client", protocol_version="2025-06-18").count(), 1
        )

    def test_legacy_initialize_with_unknown_version_gets_newest_legacy(self):
        init = self.post(
            {"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {"protocolVersion": "2099-01-01"}}
        )

        self.assertEqual(init.json()["result"]["protocolVersion"], p.LEGACY_VERSIONS[0])

    def test_modern_session_replay(self):
        """discover -> tools/list -> tools/call, as a 2026-07-28 client sends them."""
        discover = self.modern("server/discover").json()["result"]
        self.assertIn(MODERN, discover["supportedVersions"])

        tools = self.modern("tools/list").json()["result"]
        self.assertEqual(tools["resultType"], "complete")
        names = [t["name"] for t in tools["tools"]]
        self.assertEqual(names[0], "get_site")
        for definition in tools["tools"]:
            self.assertEqual(definition["inputSchema"]["type"], "object")

        site = self.ok("get_site")
        self.assertEqual(site["scopes"], sorted(self.scopes))
        log = McpRequestLog.objects.filter(tool="get_site").get()
        self.assertEqual((log.status, log.client_name, log.token), (McpRequestLog.OK, "test-client", self.token))

    def test_resources(self):
        listed = self.modern("resources/list").json()["result"]["resources"]
        self.assertEqual([r["uri"] for r in listed], ["webstead://docs/agent-guide"])
        self.assertNotIn("text", listed[0])

        read = self.modern("resources/read", {"uri": "webstead://docs/agent-guide"}).json()["result"]
        self.assertIn("Draft first", read["contents"][0]["text"])

        missing = self.modern("resources/read", {"uri": "webstead://nope"})
        self.assertEqual(missing.json()["error"]["code"], p.INVALID_PARAMS)
        self.assertEqual(missing.json()["error"]["data"], {"uri": "webstead://nope"})

    def test_unknown_tool_is_protocol_error(self):
        response = self.modern("tools/call", {"name": "launch_rocket", "arguments": {}})

        self.assertEqual(response.json()["error"]["code"], p.INVALID_PARAMS)

    def test_invalid_arguments_are_tool_errors(self):
        message = self.error("create_post", {"kind": "podcast", "colour": "blue"})

        self.assertIn("arguments.kind must be one of", message)
        self.assertIn("arguments.colour is not a known argument", message)

    def test_rate_limit(self):
        with override_settings(MCP_RATE_LIMIT=2):
            self.ok("get_site")
            self.ok("get_site")
            message = self.error("get_site")

        self.assertIn("Rate limit: 2 tool calls a minute", message)

    def test_log_redacts_upload_data_and_confirm_tokens(self):
        self.call("upload_media", {"data_base64": base64.b64encode(PNG).decode(), "alt": "dot"})
        self.call("delete_post", {"id": 999, "confirm_token": "secret"})

        upload = McpRequestLog.objects.get(tool="upload_media")
        self.assertTrue(upload.arguments["data_base64"].startswith("<"))
        delete = McpRequestLog.objects.get(tool="delete_post")
        self.assertEqual(delete.arguments["confirm_token"], "<redacted>")
        self.assertEqual(delete.status, McpRequestLog.TOOL_ERROR)


class McpScopeTests(McpTestCase):
    def test_tools_list_only_shows_permitted_tools(self):
        _, raw = self.mint(["draft", "read", "media"])

        names = {t["name"] for t in self.modern("tools/list", raw_token=raw).json()["result"]["tools"]}

        self.assertIn("create_post", names)
        self.assertIn("update_post", names)
        self.assertIn("upload_media", names)
        self.assertNotIn("publish_post", names)
        self.assertNotIn("delete_post", names)
        self.assertNotIn("revert_post", names)

    def test_draft_token_cannot_publish(self):
        _, raw = self.mint(["draft", "read"])

        message = self.error("create_post", {"kind": "note", "content": "hi", "status": "published"}, raw_token=raw)
        self.assertIn("`create` scope", message)
        self.assertIn("`create` scope", self.error("publish_post", {"id": 1}, raw_token=raw))
        self.assertFalse(Post.objects.exists())

    def test_draft_token_edits_drafts_but_not_live_posts(self):
        _, raw = self.mint(["draft", "read"])
        draft = self.ok("create_post", {"kind": "note", "content": "draft"}, raw_token=raw)
        live = Post.objects.create(title="Live", slug="live", content="x", published_on=timezone.now())

        self.ok("update_post", {"id": draft["id"], "replace": {"content": ["edited"]}}, raw_token=raw)
        message = self.error("update_post", {"id": live.pk, "replace": {"content": ["no"]}}, raw_token=raw)
        self.assertIn("`update` scope", message)
        message = self.error(
            "update_post", {"id": draft["id"], "replace": {"post-status": ["published"]}}, raw_token=raw
        )
        self.assertIn("`create` scope", message)

        live.refresh_from_db()
        self.assertEqual(live.content, "x")
        self.assertEqual(Post.objects.get(pk=draft["id"]).content, "edited")


class McpContentToolTests(McpTestCase):
    def test_create_post_is_a_draft_with_preview_and_revision(self):
        data = self.ok("create_post", {"kind": "article", "name": "Hello", "content": "Body", "tags": ["Home Lab"]})

        post = Post.objects.get(pk=data["id"])
        self.assertEqual(data["status"], "draft")
        self.assertIn("?preview=", data["preview_url"])
        self.assertEqual(data["tags"], ["home-lab"])
        self.assertIsNone(post.published_on)
        self.assertEqual(post.author, self.user)
        revision = post.revisions.get()
        self.assertEqual((revision.actor_source, revision.token, revision.client_id), ("mcp", self.token, "urn:webstead:pat"))
        self.assertEqual(self.client.get(data["preview_url"]).status_code, 200)

    def test_create_post_validates_kind_specific_fields(self):
        self.assertIn("needs like_of", self.error("create_post", {"kind": "like"}))
        self.assertIn("needs content", self.error("create_post", {"kind": "note"}))
        self.assertIn("only applies to check-ins", self.error("create_post", {"kind": "note", "content": "x", "location": "geo:1,2"}))
        self.assertIn("published_at only applies", self.error("create_post", {"kind": "note", "content": "x", "published_at": "2030-01-01"}))

    def test_create_checkin(self):
        data = self.ok("create_post", {"kind": "checkin", "name": "Cafe", "location": "geo:39.7,-104.9"})

        post = Post.objects.get(pk=data["id"])
        self.assertEqual(post.mf2["checkin"], {"latitude": 39.7, "longitude": -104.9, "name": "Cafe"})

    def test_create_scheduled_and_published(self):
        later = (timezone.now() + timezone.timedelta(days=1)).isoformat()
        scheduled = self.ok("create_post", {"kind": "note", "content": "later", "status": "published", "published_at": later})
        self.assertEqual(scheduled["status"], "scheduled")
        self.assertIn("on_go_live", scheduled)

        with self.captureOnCommitCallbacks(execute=True):
            now = self.ok("create_post", {"kind": "like", "like_of": "https://other.example/x", "status": "published"})
        self.assertEqual(now["status"], "published")
        self.assertEqual(now["sent"]["webmention_targets"], ["https://other.example/x"])
        self.assertNotIn("preview_url", now)

    def test_publish_and_unpublish(self):
        draft = self.ok("create_post", {"kind": "note", "content": "hi"})

        published = self.ok("publish_post", {"id": draft["id"]})
        self.assertEqual(published["status"], "published")
        self.assertEqual(published["message"], "Published.")
        again = self.ok("publish_post", {"id": draft["id"]})
        self.assertIn("already live", again["message"])

        unpublished = self.ok("unpublish_post", {"id": draft["id"]})
        self.assertEqual(unpublished["status"], "draft")
        self.assertIn("stays sent", unpublished["message"])
        self.assertIn("already a draft", self.error("unpublish_post", {"id": draft["id"]}))

    def test_schedule_with_at(self):
        draft = self.ok("create_post", {"kind": "note", "content": "hi"})

        data = self.ok("publish_post", {"id": draft["id"], "at": "2099-01-01T09:00:00Z"})

        self.assertEqual(data["status"], "scheduled")
        self.assertIn("Nothing is sent until then", data["message"])
        self.assertIn("must be an ISO 8601", self.error("publish_post", {"id": draft["id"], "at": "tomorrow"}))

    def test_update_post(self):
        draft = self.ok("create_post", {"kind": "note", "content": "one", "tags": ["a"]})

        data = self.ok(
            "update_post",
            {"id": draft["id"], "replace": {"content": ["two"]}, "add": {"category": ["b"]}, "delete": {"category": ["a"]}},
        )

        self.assertEqual(data["tags"], ["b"])
        self.assertIn("revert_post(revision_id=", data["message"])
        self.assertEqual(Post.objects.get(pk=draft["id"]).content, "two")
        self.assertIn("isn't supported", self.error("update_post", {"id": draft["id"], "replace": {"slug": ["x"]}}))
        self.assertIn("must be a list", self.error("update_post", {"id": draft["id"], "replace": {"content": "x"}}))
        self.assertIn("Nothing to change", self.error("update_post", {"id": draft["id"]}))
        self.assertIn("exactly one of id or url", self.error("update_post", {"replace": {"content": ["x"]}}))

    def test_delete_needs_confirmation(self):
        draft = self.ok("create_post", {"kind": "note", "content": "bye"})

        plan = self.ok("delete_post", {"id": draft["id"]})
        self.assertIn("Nothing changed yet", plan["message"])
        self.assertFalse(Post.objects.get(pk=draft["id"]).deleted)

        other = self.ok("create_post", {"kind": "note", "content": "other"})
        self.assertIn("doesn't match", self.error("delete_post", {"id": other["id"], "confirm_token": plan["confirm_token"]}))
        _, other_raw = self.mint(["delete"])
        self.assertIn(
            "doesn't match",
            self.error("delete_post", {"id": draft["id"], "confirm_token": plan["confirm_token"]}, raw_token=other_raw),
        )

        done = self.ok("delete_post", {"id": draft["id"], "confirm_token": plan["confirm_token"]})
        self.assertEqual(done["status"], "deleted")
        self.assertTrue(Post.objects.get(pk=draft["id"]).deleted)

        restored = self.ok("undelete_post", {"id": draft["id"]})
        self.assertEqual(restored["status"], "draft")

    def test_confirm_token_expires(self):
        draft = self.ok("create_post", {"kind": "note", "content": "bye"})
        plan = self.ok("delete_post", {"id": draft["id"]})

        with patch("django.core.signing.time.time", return_value=timezone.now().timestamp() + 601):
            message = self.error("delete_post", {"id": draft["id"], "confirm_token": plan["confirm_token"]})

        self.assertIn("expired", message)

    def test_revert_post(self):
        draft = self.ok("create_post", {"kind": "note", "content": "original"})
        edit = self.ok("update_post", {"id": draft["id"], "replace": {"content": ["changed"]}})

        revisions = self.ok("list_revisions", {"id": draft["id"]})["revisions"]
        self.assertEqual([r["action"] for r in revisions], ["update", "create"])
        self.assertFalse(revisions[1]["can_revert"])

        plan = self.ok("revert_post", {"id": draft["id"], "revision_id": edit["revision_id"]})
        self.assertEqual(plan["plan"]["changes"], ["content"])
        self.assertEqual(Post.objects.get(pk=draft["id"]).content, "changed")

        self.ok("revert_post", {"id": draft["id"], "revision_id": edit["revision_id"], "confirm_token": plan["confirm_token"]})
        self.assertEqual(Post.objects.get(pk=draft["id"]).content, "original")
        self.assertIn("nothing before it", self.error("revert_post", {"id": draft["id"], "revision_id": revisions[1]["id"]}))

    def test_search_and_get(self):
        now = timezone.now()
        live = Post.objects.create(title="Live hike", slug="live-hike", content="mountain", kind="note", published_on=now)
        Post.objects.create(title="Later", slug="later", content="x", published_on=now + timezone.timedelta(days=1))
        Post.objects.create(title="Gone", slug="gone", content="x", published_on=now, deleted=True)
        for i in range(3):
            Post.objects.create(title=f"Draft {i}", slug=f"draft-{i}", content="x")

        everything = self.ok("search_posts")["posts"]
        self.assertEqual(len(everything), 5)
        self.assertTrue(everything[0]["title"].startswith("Draft"))
        self.assertEqual([r["title"] for r in self.ok("search_posts", {"status": "deleted"})["posts"]], ["Gone"])
        self.assertEqual([r["title"] for r in self.ok("search_posts", {"status": "scheduled"})["posts"]], ["Later"])
        self.assertEqual([r["title"] for r in self.ok("search_posts", {"query": "mountain"})["posts"]], ["Live hike"])

        first = self.ok("search_posts", {"status": "draft", "limit": 2})
        second = self.ok("search_posts", {"status": "draft", "limit": 2, "cursor": first["next_cursor"]})
        self.assertEqual(len(first["posts"]) + len(second["posts"]), 3)
        self.assertNotIn("next_cursor", second)

        by_url = self.ok("get_post", {"url": f"http://testserver/blog/post/{live.slug}/", "include_revisions": True})
        self.assertEqual(by_url["id"], live.pk)
        self.assertEqual(by_url["properties"]["content"], ["mountain"])
        self.assertEqual(by_url["mastodon"]["tooted"], None)
        self.assertEqual(by_url["revisions"], [])
        self.assertIn("No post with id", self.error("get_post", {"id": 99999}))
        self.assertIn("isn't a post", self.error("get_post", {"url": "http://testserver/about/"}))


class McpMediaToolTests(McpTestCase):
    def test_upload_base64_then_attach_without_redownloading(self):
        upload = self.ok("upload_media", {"data_base64": base64.b64encode(PNG).decode(), "alt": "A dot", "filename": "dot.gif"})

        self.assertEqual(upload["mime_type"], "image/png")
        self.assertTrue(upload["url"].endswith(".png"))
        asset = File.objects.get(pk=upload["id"])
        self.assertEqual(asset.alt_text, "A dot")

        with patch("micropub.tasks.download_post_photo.delay") as download:
            with self.captureOnCommitCallbacks(execute=True):
                post = self.ok("create_post", {"kind": "photo", "content": "pic", "photos": [upload["url"]]})
        download.assert_not_called()
        self.assertEqual(list(Post.objects.get(pk=post["id"]).attachments.values_list("asset_id", flat=True)), [asset.pk])

    def test_rejects_non_images_and_bad_input(self):
        text = base64.b64encode(b"just text").decode()
        self.assertIn("Only JPEG, PNG", self.error("upload_media", {"data_base64": text, "alt": "x"}))
        self.assertIn("valid base64", self.error("upload_media", {"data_base64": "%%%", "alt": "x"}))
        self.assertIn("exactly one of", self.error("upload_media", {"alt": "x"}))
        self.assertIn("must not be empty", self.error("upload_media", {"data_base64": text, "alt": ""}))
        with override_settings(MCP_MAX_UPLOAD_BYTES=10):
            self.assertIn("over the", self.error("upload_media", {"data_base64": base64.b64encode(PNG).decode(), "alt": "x"}))
        self.assertFalse(File.objects.exists())

    def test_url_fetch_refuses_private_hosts(self):
        with patch("requests.get") as get:
            message = self.error("upload_media", {"url": "http://127.0.0.1/secret.png", "alt": "x"})
            self.assertIn("public http(s) URL", message)
            get.assert_not_called()

    def test_url_fetch_rechecks_redirects(self):
        class Redirect:
            is_redirect = True
            headers = {"Location": "http://169.254.169.254/latest"}

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        with patch("indieauth.views._is_public_host", side_effect=lambda host: host == "images.example"):
            with patch("requests.get", return_value=Redirect()) as get:
                message = self.error("upload_media", {"url": "https://images.example/a.png", "alt": "x"})

        self.assertIn("public http(s) URL", message)
        self.assertEqual(get.call_count, 1)


class McpUrlFetchTests(McpTestCase):
    def test_fetches_public_image(self):
        class Ok:
            is_redirect = False
            status_code = 200
            headers = {}

            def iter_content(self, size):
                yield PNG[:10]
                yield PNG[10:]

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        with patch("indieauth.views._is_public_host", return_value=True):
            with patch("requests.get", return_value=Ok()):
                data = self.ok("upload_media", {"url": "https://images.example/photos/sunset.png", "alt": "Sunset"})

        self.assertTrue(data["url"].endswith(".png"))
        self.assertTrue(data["url"].startswith("http://testserver/"))
        self.assertEqual(data["bytes"], len(PNG))


CLAUDE_CODE_CLIENT_ID = "https://claude.ai/oauth/claude-code-client-metadata"
CLAUDE_CODE_METADATA = {
    "redirect_uris": ["http://localhost/callback", "http://127.0.0.1/callback"],
    "client_name": "Claude Code",
    "logo_url": "",
}


def _challenge_for(verifier):
    import hashlib

    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


@override_settings(MCP_ENABLED=True)
class McpOAuthTests(TestCase):
    """Connecting an MCP client with OAuth instead of a personal token."""

    def setUp(self):
        cache.clear()
        self.user = get_user_model().objects.create_user(username="owner", password="pw")
        fetch = patch("indieauth.views._fetch_client_metadata", return_value=CLAUDE_CODE_METADATA)
        self.fetch = fetch.start()
        self.addCleanup(fetch.stop)

    def _parse_challenge(self, header):
        import re

        return dict(re.findall(r'(\w+)="([^"]*)"', header))

    def _mcp(self, token, method="tools/list"):
        return self.client.post(
            "/mcp",
            data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": method}),
            content_type="application/json",
            headers={"Authorization": f"Bearer {token}", "MCP-Protocol-Version": "2025-11-25"},
        )

    def _authorize(self, *, verifier="v" * 43, redirect_uri="http://localhost:53682/callback", **extra):
        params = {
            "response_type": "code",
            "client_id": CLAUDE_CODE_CLIENT_ID,
            "redirect_uri": redirect_uri,
            "state": "xyz",
            "code_challenge": _challenge_for(verifier),
            "code_challenge_method": "S256",
            "scope": "read draft media offline_access",
            "resource": "http://testserver/mcp",
        }
        params.update(extra)
        return self.client.get("/indieauth/authorize", {k: v for k, v in params.items() if v is not None}), params

    def _approve(self, params, scopes=None, **extra):
        data = {
            "client_id": params["client_id"],
            "redirect_uri": params["redirect_uri"],
            "me": "http://testserver/",
            "scope": "read draft media",
            "state": params["state"],
            "code_challenge": params["code_challenge"],
            "code_challenge_method": "S256",
            "decision": "approve",
            **extra,
        }
        if scopes is not None:
            data["scope_choice_present"] = "1"
            data["scope_choice"] = scopes
        return self.client.post("/indieauth/authorize", data)

    def _code_from(self, response):
        from urllib.parse import parse_qs, urlparse

        self.assertEqual(response.status_code, 302, getattr(response, "content", b"")[:500])
        query = parse_qs(urlparse(response["Location"]).query)
        return {k: v[0] for k, v in query.items()}

    def _exchange(self, code, verifier="v" * 43, redirect_uri="http://localhost:53682/callback", **extra):
        return self.client.post(
            "/indieauth/token",
            {
                "grant_type": "authorization_code",
                "code": code,
                "client_id": CLAUDE_CODE_CLIENT_ID,
                "redirect_uri": redirect_uri,
                "code_verifier": verifier,
                "resource": "http://testserver/mcp",
                **extra,
            },
        )

    def _refresh(self, refresh_token, **extra):
        return self.client.post(
            "/indieauth/token",
            {"grant_type": "refresh_token", "refresh_token": refresh_token, "client_id": CLAUDE_CODE_CLIENT_ID, **extra},
        )

    def _connect(self, scopes=None):
        self.client.force_login(self.user)
        _, params = self._authorize()
        query = self._code_from(self._approve(params, scopes=scopes))
        self.client.logout()
        response = self._exchange(query["code"])
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()

    def test_claude_code_connects_end_to_end(self):
        from indieauth.models import IndieAuthAccessToken

        # 1. No token: 401 pointing at the protected resource metadata.
        unauthorized = self.client.post("/mcp", data="{}", content_type="application/json")
        self.assertEqual(unauthorized.status_code, 401)
        challenge = self._parse_challenge(unauthorized["WWW-Authenticate"])
        self.assertEqual(challenge["resource_metadata"], "http://testserver/.well-known/oauth-protected-resource/mcp")
        self.assertEqual(challenge["scope"], "read draft media")

        # 2. Protected resource metadata names this site as the authorization server.
        prm = self.client.get(challenge["resource_metadata"]).json()
        self.assertEqual(prm["resource"], "http://testserver/mcp")
        self.assertEqual(prm["authorization_servers"], ["http://testserver"])

        # 3. Authorization server metadata advertises what Claude needs for CIMD.
        asm = self.client.get("/.well-known/oauth-authorization-server").json()
        self.assertEqual(asm["issuer"], "http://testserver")
        self.assertTrue(asm["client_id_metadata_document_supported"])
        self.assertIn("none", asm["token_endpoint_auth_methods_supported"])
        self.assertIn("refresh_token", asm["grant_types_supported"])
        self.assertIn("offline_access", asm["scopes_supported"])
        self.assertTrue(asm["authorization_response_iss_parameter_supported"])

        # 4. Authorize: no `me`, ephemeral loopback port, resource + offline_access.
        self.client.force_login(self.user)
        consent, params = self._authorize()
        self.assertEqual(consent.status_code, 200)
        self.assertContains(consent, "through its MCP server")
        self.assertContains(consent, "runs on your own computer")
        self.assertContains(consent, 'name="scope_choice" value="create"')
        self.assertContains(consent, 'value="read" checked')

        # 5. Approve, widening to `create` on the consent screen.
        query = self._code_from(self._approve(params, scopes=["read", "draft", "media", "create"]))
        self.assertEqual((query["state"], query["iss"]), ("xyz", "http://testserver"))
        self.client.logout()

        # 6. Code for tokens, bound to /mcp, with a refresh token.
        tokens = self._exchange(query["code"])
        self.assertEqual(tokens["Cache-Control"], "no-store")
        tokens = tokens.json()
        self.assertEqual(tokens["scope"], "read draft media create")
        self.assertLessEqual(tokens["expires_in"], 3600)
        self.assertIn("refresh_token", tokens)
        connection = IndieAuthAccessToken.objects.get()
        self.assertEqual(connection.resource, "http://testserver/mcp")
        self.assertEqual(connection.client_id, CLAUDE_CODE_CLIENT_ID)

        # 7. Use it.
        names = {t["name"] for t in self._mcp(tokens["access_token"]).json()["result"]["tools"]}
        self.assertIn("publish_post", names)

        # 8. Refresh rotates both tokens; the connection stays one row.
        refreshed = self._refresh(tokens["refresh_token"]).json()
        self.assertNotEqual(refreshed["refresh_token"], tokens["refresh_token"])
        self.assertEqual(self._mcp(tokens["access_token"]).status_code, 401)
        self.assertEqual(self._mcp(refreshed["access_token"]).status_code, 200)
        self.assertEqual(IndieAuthAccessToken.objects.count(), 1)

        # 9. Replaying a used refresh token revokes the connection.
        replay = self._refresh(tokens["refresh_token"])
        self.assertEqual(replay.json()["error"], "invalid_grant")
        self.assertEqual(self._mcp(refreshed["access_token"]).status_code, 401)
        self.assertEqual(self._refresh(refreshed["refresh_token"]).json()["error"], "invalid_grant")

    def test_loopback_redirect_matches_any_port_only(self):
        self.client.force_login(self.user)
        for uri in ("http://localhost:1234/callback", "http://127.0.0.1:65000/callback", "http://localhost/callback"):
            response, _ = self._authorize(redirect_uri=uri)
            self.assertEqual(response.status_code, 200, uri)
        for uri in (
            "http://localhost:1234/other",
            "https://localhost:1234/callback",
            "http://evil.example:1234/callback",
            "http://127.0.0.1.evil.example/callback",
        ):
            response, _ = self._authorize(redirect_uri=uri)
            self.assertEqual(response.status_code, 400, uri)

    def test_unknown_resource_is_invalid_target(self):
        self.client.force_login(self.user)
        response, _ = self._authorize(resource="https://elsewhere.example/mcp")

        query = self._code_from(response)
        self.assertEqual(query["error"], "invalid_target")
        self.assertEqual(query["iss"], "http://testserver")

    def test_resource_must_match_at_token_endpoint(self):
        self.client.force_login(self.user)
        _, params = self._authorize()
        query = self._code_from(self._approve(params))

        response = self._exchange(query["code"], resource="http://testserver/other")

        self.assertEqual(response.json()["error"], "invalid_target")

    def test_resource_disabled_when_mcp_off(self):
        self.client.force_login(self.user)
        with override_settings(MCP_ENABLED=False):
            response, _ = self._authorize()
            self.assertEqual(self._code_from(response)["error"], "invalid_target")
            self.assertEqual(self.client.get("/.well-known/oauth-protected-resource/mcp").status_code, 404)

    def test_deny_redirects_with_access_denied(self):
        self.client.force_login(self.user)
        _, params = self._authorize()

        query = self._code_from(self._approve(params, decision="deny"))

        self.assertEqual((query["error"], query["iss"]), ("access_denied", "http://testserver"))

    def test_default_consent_keeps_requested_scopes(self):
        tokens = self._connect()

        self.assertEqual(tokens["scope"], "read draft media")

    def test_remembered_consent_reissues_granted_scopes(self):
        self.client.force_login(self.user)
        _, params = self._authorize()
        self._approve(params, scopes=["read"], remember="1")

        response, _ = self._authorize(verifier="w" * 43)
        query = self._code_from(response)
        self.client.logout()
        tokens = self._exchange(query["code"], verifier="w" * 43).json()

        self.assertEqual(tokens["scope"], "read")

    def test_remembered_consent_preserves_empty_grant(self):
        self.client.force_login(self.user)
        _, params = self._authorize()
        first = self._code_from(self._approve(params, scopes=[], remember="1"))
        self.assertEqual(self._exchange(first["code"]).json()["scope"], "")

        response, _ = self._authorize(verifier="w" * 43)
        query = self._code_from(response)
        tokens = self._exchange(query["code"], verifier="w" * 43).json()

        self.assertEqual(tokens["scope"], "")
        self.assertEqual(self._mcp(tokens["access_token"]).status_code, 403)

    def test_remembered_consent_preserves_unchanged_grant(self):
        self.client.force_login(self.user)
        _, params = self._authorize()
        self._approve(params, scopes=["read", "draft", "media"], remember="1")

        response, _ = self._authorize(verifier="w" * 43)
        query = self._code_from(response)
        tokens = self._exchange(query["code"], verifier="w" * 43).json()

        self.assertEqual(tokens["scope"], "read draft media")

    def test_remembered_consents_are_independent_per_resource(self):
        from indieauth.models import IndieAuthConsent

        self.client.force_login(self.user)
        for resource, scopes, verifier in (
            ("http://testserver/mcp", ["read", "create"], "m" * 43),
            (None, None, "p" * 43),
            ("http://testserver", ["read"], "o" * 43),
        ):
            with self.subTest(resource=resource):
                response, params = self._authorize(resource=resource, verifier=verifier)
                self.assertEqual(response.status_code, 200)
                self._approve(params, scopes=scopes, remember="1")

        self.assertEqual(IndieAuthConsent.objects.count(), 3)
        for resource, expected_scope in (
            ("http://testserver/mcp", "read create"),
            (None, "read draft media"),
            ("http://testserver", "read"),
        ):
            with self.subTest(resource=resource):
                response, _ = self._authorize(resource=resource, verifier="w" * 43)
                query = self._code_from(response)
                tokens = self._exchange(
                    query["code"], verifier="w" * 43, resource=resource or ""
                ).json()
                self.assertEqual(tokens["scope"], expected_scope)

    def test_plain_consent_cannot_skip_mcp_consent(self):
        self.client.force_login(self.user)
        _, params = self._authorize(resource=None)
        self._approve(params, remember="1")

        response, _ = self._authorize(verifier="w" * 43)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "through its MCP server")

    def test_old_refresh_token_replay_revokes_current_connection(self):
        from indieauth.models import IndieAuthRefreshToken
        from indieauth.views import _hash_token

        tokens = self._connect()
        second = self._refresh(tokens["refresh_token"]).json()
        IndieAuthRefreshToken.objects.filter(
            token_hash=_hash_token(tokens["refresh_token"])
        ).update(used_at=timezone.now() - timezone.timedelta(days=2))
        third = self._refresh(second["refresh_token"]).json()
        self.assertEqual(self._mcp(third["access_token"]).status_code, 200)

        replay = self._refresh(tokens["refresh_token"])

        self.assertEqual(replay.json()["error"], "invalid_grant")
        self.assertEqual(self._mcp(third["access_token"]).status_code, 401)
        self.assertEqual(
            self._refresh(third["refresh_token"]).json()["error"], "invalid_grant"
        )

    def test_refresh_errors(self):
        tokens = self._connect()
        refresh = tokens["refresh_token"]

        self.assertEqual(self._refresh(refresh, client_id="https://other.example/client").json()["error"], "invalid_grant")
        self.assertEqual(self._refresh(refresh, scope="read delete").json()["error"], "invalid_scope")
        self.assertEqual(self._refresh(refresh, resource="http://testserver/x").json()["error"], "invalid_target")
        missing = self.client.post("/indieauth/token", {"grant_type": "refresh_token", "refresh_token": refresh})
        self.assertEqual(missing.json()["error"], "invalid_request")
        self.assertEqual(self._refresh("nope").json()["error"], "invalid_grant")
        # None of those used it up.
        self.assertIn("access_token", self._refresh(refresh).json())

    def test_expired_refresh_token(self):
        from indieauth.models import IndieAuthRefreshToken

        tokens = self._connect()
        IndieAuthRefreshToken.objects.update(expires_at=timezone.now() - timezone.timedelta(seconds=1))

        self.assertEqual(self._refresh(tokens["refresh_token"]).json()["error"], "invalid_grant")

    def test_revoking_refresh_token_ends_connection(self):
        tokens = self._connect()

        response = self.client.post("/indieauth/revoke", {"token": tokens["refresh_token"]})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self._mcp(tokens["access_token"]).status_code, 401)
        self.assertEqual(self.client.post("/indieauth/revoke", {"token": "unknown"}).status_code, 200)

    def test_admin_revoke_ends_refresh_chain(self):
        from indieauth.models import IndieAuthAccessToken

        tokens = self._connect()
        IndieAuthAccessToken.objects.update(revoked_at=timezone.now())

        self.assertEqual(self._refresh(tokens["refresh_token"]).json()["error"], "invalid_grant")

    def test_audience_is_enforced_both_ways(self):
        from indieauth.models import IndieAuthAccessToken
        from indieauth.views import _hash_token

        tokens = self._connect()
        # An MCP-bound token isn't accepted by Micropub.
        micropub = self.client.get("/micropub", {"q": "config"}, headers={"Authorization": f"Bearer {tokens['access_token']}"})
        self.assertIn(micropub.status_code, (401, 403))

        # A Micropub client's token (no resource) isn't accepted by /mcp.
        IndieAuthAccessToken.objects.create(
            token_hash=_hash_token("padd-token"), client_id="https://padd.example/", me="http://testserver/",
            scope="create update media read", user=self.user,
        )
        response = self._mcp("padd-token")
        self.assertEqual(response.status_code, 401)
        self.assertIn('error="invalid_token"', response["WWW-Authenticate"])

    def test_root_metadata_and_origin_audience(self):
        from indieauth.models import IndieAuthAccessToken
        from indieauth.views import _hash_token

        root = self.client.get("/.well-known/oauth-protected-resource").json()
        self.assertEqual(root["resource"], "http://testserver")
        IndieAuthAccessToken.objects.create(
            token_hash=_hash_token("origin-token"), client_id=CLAUDE_CODE_CLIENT_ID, me="http://testserver/",
            scope="read", user=self.user, resource="http://testserver",
        )
        self.assertEqual(self._mcp("origin-token").status_code, 200)

    def test_plain_indieauth_flow_unchanged(self):
        """No resource: no refresh token, 30-day token, and `me` may be omitted."""
        from indieauth.models import IndieAuthClient

        IndieAuthClient.objects.create(client_id="https://padd.example/", name="PADD", redirect_uris=["https://padd.example/cb"])
        self.client.force_login(self.user)
        verifier = "p" * 43
        consent = self.client.get(
            "/indieauth/authorize",
            {
                "response_type": "code", "client_id": "https://padd.example/", "redirect_uri": "https://padd.example/cb",
                "state": "s", "code_challenge": _challenge_for(verifier), "code_challenge_method": "S256", "scope": "create",
            },
        )
        self.assertEqual(consent.status_code, 200)
        self.assertNotContains(consent, "scope_choice")
        approve = self.client.post(
            "/indieauth/authorize",
            {
                "client_id": "https://padd.example/", "redirect_uri": "https://padd.example/cb", "me": "",
                "scope": "create", "state": "s", "code_challenge": _challenge_for(verifier),
                "code_challenge_method": "S256", "decision": "approve",
            },
        )
        code = self._code_from(approve)["code"]
        tokens = self.client.post(
            "/indieauth/token",
            {"grant_type": "authorization_code", "code": code, "client_id": "https://padd.example/",
             "redirect_uri": "https://padd.example/cb", "code_verifier": verifier},
        ).json()

        self.assertNotIn("refresh_token", tokens)
        self.assertGreater(tokens["expires_in"], 29 * 24 * 3600)
        self.assertEqual(tokens["me"], "http://testserver/")


class ClientMetadataDocumentTests(TestCase):
    def _fetch(self, body):
        from indieauth.views import _fetch_client_metadata

        class Response:
            headers = {"Content-Type": "application/json"}

            def read(self, n):
                return json.dumps(body).encode()

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        opener = type("Opener", (), {"open": lambda self, req, timeout: Response()})()
        with patch("indieauth.views._is_public_host", return_value=True), patch("indieauth.views.build_opener", return_value=opener):
            return _fetch_client_metadata(CLAUDE_CODE_CLIENT_ID)

    def test_matching_document_parses(self):
        data = self._fetch(
            {"client_id": CLAUDE_CODE_CLIENT_ID, "client_name": "Claude Code", "redirect_uris": ["http://localhost/callback"]}
        )

        self.assertEqual(data["redirect_uris"], ["http://localhost/callback"])
        self.assertEqual(data["client_name"], "Claude Code")

    def test_mismatched_client_id_is_rejected(self):
        with self.assertRaises(ValueError):
            self._fetch({"client_id": "https://evil.example/client", "redirect_uris": ["http://localhost/callback"]})
