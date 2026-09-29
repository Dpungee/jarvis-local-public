"""Connections: the MCP client (stdio and HTTP), the connection store and agent tool wiring."""
import json
import sys
import tempfile
import textwrap
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from jarvis import mcp_client
from jarvis.connections import ConnectionManager

STUB_SERVER = textwrap.dedent('''
    import json, sys
    TOOLS = [
        {"name": "search_mail", "description": "Search the inbox", "annotations": {"readOnlyHint": True},
         "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
        {"name": "send_mail", "description": "Send an email",
         "inputSchema": {"type": "object", "properties": {"to": {"type": "string"}, "body": {"type": "string"}},
                         "required": ["to", "body"]}},
    ]
    print("stub server starting (a log line on stdout that is not JSON)", flush=True)
    for line in sys.stdin:
        message = json.loads(line)
        if "id" not in message:
            continue
        method, rid = message["method"], message["id"]
        if method == "initialize":
            result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                      "serverInfo": {"name": "stub-mail", "version": "1"}}
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            args = message["params"]["arguments"]
            if message["params"]["name"] == "search_mail":
                result = {"content": [{"type": "text", "text": "2 messages match " + args["query"]}]}
            else:
                result = {"content": [{"type": "text", "text": "sent to " + args["to"]}]}
        else:
            print(json.dumps({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "no"}}), flush=True)
            continue
        print(json.dumps({"jsonrpc": "2.0", "id": rid, "result": result}), flush=True)
''')


def stub_command(root: Path) -> list[str]:
    script = root / "stub_mcp.py"
    script.write_text(STUB_SERVER, encoding="utf-8")
    return [sys.executable, "-u", str(script)]


class StdioClientTests(unittest.TestCase):
    def test_initialize_list_and_call_over_stdio(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = mcp_client.connect("stdio", command=stub_command(Path(tmp)))
            try:
                self.assertEqual(client.server_info["name"], "stub-mail")
                self.assertEqual([t["name"] for t in client.list_tools()], ["search_mail", "send_mail"])
                self.assertEqual(client.call_tool("search_mail", {"query": "invoice"}),
                                 {"ok": True, "content": "2 messages match invoice"})
            finally:
                client.close()

    def test_a_missing_program_is_a_clear_error(self):
        with self.assertRaises(mcp_client.MCPError):
            mcp_client.connect("stdio", command=["definitely-not-a-real-program-xyz"])


class _Handler(BaseHTTPRequestHandler):
    token = "secret-token"
    sse = False

    def log_message(self, *args):
        pass

    def do_POST(self):
        if self.headers.get("Authorization") != f"Bearer {self.token}":
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Bearer resource_metadata="https://example.test/.well-known/x"')
            self.end_headers()
            return
        message = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if "id" not in message:
            self.send_response(202)
            self.end_headers()
            return
        result = {"initialize": {"protocolVersion": "2025-06-18", "serverInfo": {"name": "http-stub"}},
                  "tools/list": {"tools": [{"name": "echo", "inputSchema": {"type": "object"},
                                            "annotations": {"readOnlyHint": True}}]},
                  "tools/call": {"content": [{"type": "text", "text": "echo ok"}]}}[message["method"]]
        body = json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}).encode()
        self.send_response(200)
        self.send_header("Mcp-Session-Id", "s-1")
        if self.sse:
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(b"event: message\ndata: " + body + b"\n\n")
        else:
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)


class HttpClientTests(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_port}/mcp"

    def test_bearer_token_json_and_event_stream_answers(self):
        for sse in (False, True):
            with self.subTest(sse=sse):
                _Handler.sse = sse
                client = mcp_client.connect("http", url=self.url,
                                            headers=lambda: {"Authorization": "Bearer secret-token"})
                self.assertEqual(client.transport.session_id, "s-1")
                self.assertEqual(client.call_tool("echo", {}), {"ok": True, "content": "echo ok"})
        _Handler.sse = False

    def test_no_token_raises_auth_required_with_the_metadata_hint(self):
        with self.assertRaises(mcp_client.AuthRequired) as caught:
            mcp_client.connect("http", url=self.url)
        self.assertIn("resource_metadata", caught.exception.www_authenticate)


class ConnectionManagerTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.manager = ConnectionManager(self.root)
        self.addCleanup(self.manager.close)

    def test_a_local_server_is_added_tested_and_offered(self):
        added = self.manager.add({"preset": "custom-command", "name": "Mail", "command": stub_command(self.root),
                                  "env": {"MAIL_TOKEN": "super-secret-value"}})
        self.assertEqual(added["env_names"], ["MAIL_TOKEN"])
        tested = self.manager.test(added["id"])
        self.assertEqual(tested["status"], "connected")
        self.assertEqual([(t["name"], t["read_only"]) for t in tested["tools"]],
                         [("search_mail", True), ("send_mail", False)])
        offered = self.manager.agent_tools()
        self.assertEqual([t["tool"] for t in offered], ["search_mail", "send_mail"])
        self.assertEqual(self.manager.call(added["id"], "search_mail", {"query": "x"})["content"], "2 messages match x")
        # Secrets stay out of the config file and out of every view.
        self.assertNotIn("super-secret-value", (self.root / "connections" / "config.json").read_text())
        self.assertNotIn("super-secret-value", json.dumps(self.manager.list()))
        self.manager.update(added["id"], enabled=False)
        self.assertEqual(self.manager.agent_tools(), [])

    def test_tokens_and_keyed_urls_are_secret(self):
        added = self.manager.add({"preset": "zapier", "url": "https://mcp.zapier.com/api/mcp/s/ABCDEF123/mcp"})
        self.assertNotIn("ABCDEF123", json.dumps(self.manager.list()))
        self.assertIn("key hidden", added["url"])
        github = self.manager.add({"preset": "github", "token": "ghp_exampletoken"})
        self.assertTrue(github["signed_in"])
        self.assertNotIn("ghp_exampletoken", json.dumps(self.manager.list()))

    def test_bad_input_is_refused(self):
        for spec in ({"preset": "custom-url", "url": "ftp://x"}, {"preset": "custom-command", "command": []},
                     {"preset": "custom-command", "command": ["x"], "env": {"bad name": "v"}}, {"preset": "nope"}):
            with self.subTest(spec=spec), self.assertRaises(ValueError):
                self.manager.add(spec)

    def test_a_refused_token_is_reported_not_raised(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        added = self.manager.add({"preset": "custom-url", "url": f"http://127.0.0.1:{server.server_port}/mcp",
                                  "token": "wrong"})
        self.assertEqual(self.manager.test(added["id"])["status"], "needs sign-in")
        self.manager.update(added["id"], token="secret-token")
        self.assertEqual(self.manager.test(added["id"])["status"], "connected")


class _OAuthServer(BaseHTTPRequestHandler):
    """A fake MCP server with its own OAuth authorization server (PKCE, dynamic registration)."""
    issued = {}
    codes = {}

    def log_message(self, *args):
        pass

    def _json(self, status, value, headers=None):
        body = json.dumps(value).encode()
        self.send_response(status)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        base = f"http://127.0.0.1:{self.server.server_port}"
        if self.path.startswith("/.well-known/oauth-protected-resource"):
            return self._json(200, {"resource": base + "/mcp", "authorization_servers": [base]})
        if self.path.startswith("/.well-known/oauth-authorization-server"):
            return self._json(200, {"authorization_endpoint": base + "/authorize", "token_endpoint": base + "/token",
                                    "registration_endpoint": base + "/register"})
        self._json(404, {})

    def do_POST(self):
        import hashlib, base64, urllib.parse
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        if self.path == "/register":
            return self._json(201, {"client_id": "client-123"})
        if self.path == "/token":
            form = dict(urllib.parse.parse_qsl(raw.decode()))
            challenge = self.codes.get(form.get("code"))
            digest = base64.urlsafe_b64encode(hashlib.sha256(form.get("code_verifier", "").encode()).digest()).rstrip(b"=").decode()
            if challenge != digest or form.get("client_id") != "client-123":
                return self._json(400, {"error": "invalid_grant"})
            return self._json(200, {"access_token": "tok-1", "refresh_token": "ref-1", "expires_in": 3600})
        if self.headers.get("Authorization") != "Bearer tok-1":
            base = f"http://127.0.0.1:{self.server.server_port}"
            self.send_response(401)
            self.send_header("WWW-Authenticate", f'Bearer resource_metadata="{base}/.well-known/oauth-protected-resource"')
            self.end_headers()
            return
        message = json.loads(raw)
        if "id" not in message:
            self.send_response(202)
            self.end_headers()
            return
        result = {"initialize": {"protocolVersion": "2025-06-18", "serverInfo": {"name": "oauth-stub"}},
                  "tools/list": {"tools": [{"name": "list_pages", "inputSchema": {"type": "object"},
                                            "annotations": {"readOnlyHint": True}}]}}[message["method"]]
        self._json(200, {"jsonrpc": "2.0", "id": message["id"], "result": result})


class OAuthFlowTests(unittest.TestCase):
    def test_connect_signs_in_with_pkce_and_registers_itself(self):
        import urllib.parse
        server = ThreadingHTTPServer(("127.0.0.1", 0), _OAuthServer)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        with tempfile.TemporaryDirectory() as tmp:
            manager = ConnectionManager(Path(tmp), callback_url=lambda: "http://127.0.0.1:9/api/connections/oauth/callback")
            added = manager.add({"preset": "custom-url", "url": f"http://127.0.0.1:{server.server_port}/mcp"})
            self.assertEqual(manager.test(added["id"])["status"], "needs sign-in")
            authorize = manager.oauth_start(added["id"])
            query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(authorize).query))
            self.assertEqual((query["client_id"], query["code_challenge_method"]), ("client-123", "S256"))
            _OAuthServer.codes["code-xyz"] = query["code_challenge"]  # the operator approved on the provider page
            with self.assertRaises(ValueError):
                manager.oauth_callback("forged-state", "code-xyz")
            connected = manager.oauth_callback(query["state"], "code-xyz")
            self.assertEqual((connected["status"], connected["auth"], connected["signed_in"]), ("connected", "oauth", True))
            with self.assertRaises(ValueError):
                manager.oauth_callback(query["state"], "code-xyz")  # single use
            secrets_file = next((Path(tmp) / "connections" / "secrets").glob("*.json")).read_text()
            self.assertIn("tok-1", secrets_file)
            self.assertNotIn("tok-1", (Path(tmp) / "connections" / "config.json").read_text())
            manager.close()


class HubConnectionTests(unittest.TestCase):
    def setUp(self):
        from jarvis.agent_hub import HubService
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.service = HubService(state_dir=self.root, provider_profile_dir=self.root / "profile",
                                  runtime_kwargs={"autostart": False, "provider_probe": lambda _: {
                                      "installed": True, "authenticated": True, "detail": "t", "version": "t"}})
        self.addCleanup(self.service.close)
        runtime = self.service.runtime
        added = runtime.connections.add({"preset": "custom-command", "name": "Mail",
                                         "command": stub_command(self.root)})
        self.assertEqual(runtime.connections.test(added["id"])["status"], "connected")
        self.connection_id = added["id"]

    def toolbox(self):
        from jarvis.tools import ToolBox
        from jarvis.agent_hub_runtime import DEFAULT_PERMISSIONS
        runtime = self.service.runtime
        config = runtime._agent_config(agent_id="a", project_root=self.root, reference="claude-cli:x",
                                       permissions=DEFAULT_PERMISSIONS)
        memory = runtime._open_memory(config)
        self.addCleanup(memory.close)
        toolbox = ToolBox(config, memory)
        return toolbox, memory, runtime._install_connection_tools(toolbox)

    def test_connected_tools_are_offered_and_gated(self):
        toolbox, memory, installed = self.toolbox()
        runtime = self.service.runtime
        self.assertEqual(sorted(installed), ["mcp_mail_search_mail", "mcp_mail_send_mail"])
        read = json.loads(toolbox.execute("mcp_mail_search_mail", {"query": "invoice"}))
        self.assertTrue(read["ok"])
        self.assertIn("2 messages match invoice", read["result"]["content"])
        send_meta = installed["mcp_mail_send_mail"]
        args = {"to": "a@example.com", "body": "hi"}
        with toolbox.approval_context("conversation:1"):
            self.assertIsNone(runtime._connection_gate(toolbox, memory, installed["mcp_mail_search_mail"], {"query": "x"}))
            gate = json.loads(runtime._connection_gate(toolbox, memory, send_meta, args))
            self.assertTrue(gate["approval_required"])
            memory.decide_approval(gate["approval_id"], True)
            self.assertIsNone(runtime._connection_gate(toolbox, memory, send_meta, args))  # approved once
            self.assertTrue(json.loads(runtime._connection_gate(toolbox, memory, send_meta, args))["approval_required"])
        self.service.runtime.connections.update(self.connection_id, ask="never")
        _t, _m, trusted = self.toolbox()
        with toolbox.approval_context("conversation:1"):
            self.assertIsNone(runtime._connection_gate(toolbox, memory, trusted["mcp_mail_send_mail"], args))
        self.service.runtime.connections.update(self.connection_id, ask="always")
        _t, _m, strict = self.toolbox()
        with toolbox.approval_context("conversation:1"):
            self.assertTrue(json.loads(runtime._connection_gate(
                toolbox, memory, strict["mcp_mail_search_mail"], {"query": "x"}))["approval_required"])

    def test_new_abilities_require_explicit_grants_for_saved_agents(self):
        runtime = self.service.runtime
        full = self.service.create_agent({"name": "Full", "role": "r", "provider": "claude-cli",
                                          "model": "claude-opus-5-5", "enable": True})
        limited = self.service.create_agent({"name": "Limited", "role": "r", "provider": "claude-cli",
                                             "model": "claude-opus-5-5", "enable": True})
        with runtime.db() as db:  # settings saved before "connections" existed
            db.execute("UPDATE hub_agent_settings SET permissions=? WHERE agent_id=?",
                       (json.dumps({"files_read": True, "web_research": True}), full["agent_id"]))
            db.execute("UPDATE hub_agent_settings SET permissions=? WHERE agent_id=?",
                       (json.dumps({"files_read": True, "web_research": False}), limited["agent_id"]))
        self.assertFalse(runtime.agent_settings(full["agent_id"])["permissions"]["connections"])
        self.assertFalse(runtime.agent_settings(limited["agent_id"])["permissions"]["connections"])
        grants = runtime.agent_settings(full["agent_id"])["permissions"]
        grants["connections"] = True
        runtime.save_agent_settings(full["agent_id"], permissions=grants)
        self.assertTrue(runtime.agent_settings(full["agent_id"])["permissions"]["connections"])

    def test_the_permission_and_brief(self):
        from jarvis.agent_hub_runtime import DEFAULT_PERMISSIONS, PERMISSION_LABELS
        self.assertIn("connections", PERMISSION_LABELS)
        self.assertTrue(DEFAULT_PERMISSIONS["connections"])


if __name__ == "__main__":
    unittest.main()
