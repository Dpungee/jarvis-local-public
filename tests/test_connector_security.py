"""Offline adversarial contracts for connected-tool authority and transport boundaries."""

import copy
import io
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from contextvars import ContextVar
from email.message import Message
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from jarvis import mcp_client, mcp_oauth
from jarvis.agent_hub_runtime import AgentRuntime
from jarvis.connections import ConnectionManager
from jarvis.tools import ToolBox


class _Client:
    server_info = {"name": "synthetic"}

    def __init__(self, tools, calls):
        self.tools, self.calls = tools, calls
        self.failure = None
        self.entered = self.release = None

    def list_tools(self):
        return copy.deepcopy(self.tools)

    def call_tool(self, name, arguments):
        self.calls.append((name, dict(arguments)))
        if self.entered is not None:
            self.entered.set()
            if not self.release.wait(5):
                raise AssertionError("Synthetic action was not released")
        if self.failure is not None:
            raise self.failure
        return {"ok": True, "content": "synthetic answer"}

    def close(self):
        pass


class _Approvals:
    def __init__(self):
        self.resources = []
        self.approved = set()

    def authorize_or_request(self, action, resource, reason, **scope):
        self.resources.append(resource)
        if resource in self.approved:
            self.approved.remove(resource)
            return True, len(self.resources)
        return False, len(self.resources)


def _tool(name="read", *, read_only=True, properties=None):
    return {"name": name, "description": "Synthetic tool", "annotations": {"readOnlyHint": read_only},
            "inputSchema": {"type": "object", "properties": properties or {"query": {"type": "string"}}}}


class ConnectorAuthorityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.calls = []
        self.specs = [_tool(), _tool("write", read_only=False)]
        self.clients = []

        def connect(*args, **kwargs):
            client = _Client(self.specs, self.calls)
            self.clients.append(client)
            return client

        self.manager = ConnectionManager(Path(temporary.name), connect=connect)
        self.addCleanup(self.manager.close)
        self.cid = self.manager.add({"preset": "custom-url", "url": "https://example.test/mcp"})["id"]
        self.manager.test(self.cid)
        self.runtime = AgentRuntime.__new__(AgentRuntime)
        self.runtime.connections = self.manager
        self.memory = _Approvals()
        self.install()

    def install(self):
        self.toolbox = SimpleNamespace(tools={}, _validate_arguments=ToolBox._validate_arguments,
                                       _approval_execution_context=ContextVar("synthetic_scope",
                                                                             default=("conversation:1", None)))
        self.installed = self.runtime._install_connection_tools(self.toolbox)

    def name(self, remote_name):
        return next(name for name, meta in self.installed.items() if meta["tool"] == remote_name)

    def dispatch(self, remote_name="read", arguments=None):
        name = self.name(remote_name)

        def execute(tool_name, args):
            return json.dumps({"ok": True, "result": self.toolbox.tools[tool_name].function(**args)})

        return json.loads(self.runtime._dispatch_connection_tool(
            self.toolbox, self.memory, self.installed[name], name, arguments or {}, execute))

    def test_schema_declared_identity_fields_cannot_retarget_the_callable(self):
        self.specs[0] = _tool(properties={"_connection_id": {"type": "string"}, "_tool": {"type": "string"}})
        self.manager.test(self.cid)
        self.install()
        args = {"_connection_id": "conn_aaaaaaaaaaaa", "_tool": "write"}
        self.assertTrue(self.dispatch(arguments=args)["ok"])
        self.assertEqual(self.calls, [("read", args)])

    def test_each_callable_keeps_its_own_binding(self):
        self.assertTrue(self.dispatch()["ok"])
        answer = self.dispatch("write")
        self.assertTrue(answer["approval_required"])
        self.memory.approved.add(self.memory.resources[-1])
        self.assertTrue(self.dispatch("write")["ok"])
        self.assertEqual([name for name, _ in self.calls], ["read", "write"])

    def test_disabled_connection_cannot_execute_or_reconnect(self):
        self.manager.update(self.cid, enabled=False)
        before = len(self.clients)
        self.assertFalse(self.dispatch()["ok"])
        with self.assertRaises(PermissionError):
            self.manager.call(self.cid, "read", {})
        self.assertEqual(len(self.clients), before)
        self.assertEqual(self.calls, [])

    def test_deleted_connection_cannot_execute(self):
        self.manager.delete(self.cid)
        self.assertFalse(self.dispatch()["ok"])
        self.assertEqual(self.calls, [])

    def test_tightening_ask_policy_applies_to_an_existing_turn(self):
        self.manager.update(self.cid, ask="always")
        self.assertTrue(self.dispatch()["approval_required"])
        self.assertEqual(self.calls, [])

    def test_changed_schema_and_removed_tool_refuse_stale_turn(self):
        self.specs[0]["inputSchema"]["properties"]["new"] = {"type": "string"}
        self.manager.test(self.cid)
        self.assertIn("changed", self.dispatch()["error"])
        self.specs[:] = [self.specs[1]]
        self.manager.test(self.cid)
        self.assertIn("missing", self.dispatch()["error"])
        self.assertEqual(self.calls, [])

    def test_changed_effect_annotation_refuses_stale_turn(self):
        self.specs[0]["annotations"]["readOnlyHint"] = False
        self.manager.test(self.cid)
        self.assertFalse(self.dispatch()["ok"])
        self.install()
        self.assertTrue(self.dispatch()["approval_required"])
        self.assertEqual(self.calls, [])

    def test_invalid_arguments_do_not_consume_an_approval(self):
        self.assertTrue(self.dispatch("write")["approval_required"])
        self.memory.approved.add(self.memory.resources[-1])
        before = len(self.memory.resources)
        self.assertFalse(self.dispatch("write", {"undeclared": "x"})["ok"])
        self.assertEqual(len(self.memory.resources), before)
        self.assertTrue(self.dispatch("write")["ok"])

    def test_policy_revision_invalidates_an_old_exact_approval(self):
        self.assertTrue(self.dispatch("write")["approval_required"])
        old = self.memory.resources[-1]
        self.memory.approved.add(old)
        self.manager.update(self.cid, ask="always")
        self.assertTrue(self.dispatch("write")["approval_required"])
        self.assertNotEqual(old, self.memory.resources[-1])
        resource = json.loads(self.memory.resources[-1])
        self.assertEqual(resource["connection_id"], self.cid)
        self.assertEqual(resource["tool"], "write")
        self.assertEqual(len(resource["tool_fingerprint"]), 64)
        self.assertEqual(self.calls, [])

    def test_new_credentials_invalidate_old_approval(self):
        self.assertTrue(self.dispatch("write")["approval_required"])
        self.memory.approved.add(self.memory.resources[-1])
        self.manager.update(self.cid, token="synthetic-token")
        self.assertTrue(self.dispatch("write")["approval_required"])
        self.assertEqual(self.calls, [])

    def test_approval_is_still_one_use_and_argument_bound(self):
        self.assertTrue(self.dispatch("write", {"query": "first"})["approval_required"])
        self.memory.approved.add(self.memory.resources[-1])
        self.assertTrue(self.dispatch("write", {"query": "changed"})["approval_required"])
        self.assertTrue(self.dispatch("write", {"query": "first"})["ok"])
        self.assertTrue(self.dispatch("write", {"query": "first"})["approval_required"])
        self.assertEqual(self.calls, [("write", {"query": "first"})])

    def test_ambiguous_mutation_is_not_retried(self):
        self.clients[-1].failure = mcp_client.MCPError("synthetic response lost after mutation")
        before = len(self.clients)
        with self.assertRaisesRegex(mcp_client.MCPError, "not retried"):
            self.manager.call(self.cid, "write", {"query": "one action"})
        self.assertEqual(self.calls, [("write", {"query": "one action"})])
        self.assertEqual(len(self.clients), before)

    def test_auth_failure_is_not_retried(self):
        self.clients[-1].failure = mcp_client.AuthRequired("synthetic refusal")
        with self.assertRaises(mcp_client.AuthRequired):
            self.manager.call(self.cid, "write", {})
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.manager.get(self.cid)["status"], "needs sign-in")

    def test_full_hub_dispatch_preserves_connection_and_agent_revocation(self):
        permissions = {"connections": True}
        self.runtime._lock = threading.RLock()
        self.runtime._permission_locks = {}
        self.runtime.agent_settings = lambda _: {"archived": False, "permissions": permissions}
        self.runtime.event = Mock()
        self.runtime._set = Mock()
        self.toolbox.tools = {}

        def execute(name, arguments):
            return json.dumps({"ok": True, "result": self.toolbox.tools[name].function(**arguments)})

        self.toolbox.execute = execute
        self.runtime._install_tools(
            SimpleNamespace(toolbox=self.toolbox), task={"task_id": "synthetic-task", "agent_id": "synthetic-agent",
                                                         "project_id": "synthetic-project"},
            agent=SimpleNamespace(), permissions=permissions, root=self.manager.root, record={},
            config=SimpleNamespace(), memory=self.memory, reference="synthetic", effort=None)
        name = self.name("read")
        self.assertTrue(json.loads(self.toolbox.execute(name, {}))["ok"])
        self.manager.update(self.cid, ask="always")
        self.assertTrue(json.loads(self.toolbox.execute(name, {}))["approval_required"])
        self.memory.approved.add(self.memory.resources[-1])
        self.assertTrue(json.loads(self.toolbox.execute(name, {}))["ok"])
        self.manager.update(self.cid, enabled=False)
        self.assertFalse(json.loads(self.toolbox.execute(name, {}))["ok"])
        self.manager.update(self.cid, enabled=True, ask="never")
        permissions["connections"] = False
        self.assertFalse(json.loads(self.toolbox.execute(name, {}))["ok"])
        self.assertEqual(len(self.calls), 2)

    def test_busy_connection_does_not_serialize_other_connections(self):
        first = self.clients[-1]
        other = self.manager.add({"preset": "custom-url", "url": "https://other.example.test/mcp"})["id"]
        self.manager.test(other)
        entered, release, second_done = (threading.Event() for _ in range(3))
        first.entered, first.release = entered, release
        failures = []

        def run(connection_id, finished=None):
            try:
                self.manager.call(connection_id, "read", {})
                if finished is not None:
                    finished.set()
            except BaseException as exc:
                failures.append(exc)

        worker = threading.Thread(target=run, args=(self.cid,))
        other_worker = threading.Thread(target=run, args=(other, second_done))
        worker.start()
        try:
            self.assertTrue(entered.wait(3))
            other_worker.start()
            self.assertTrue(second_done.wait(3))
        finally:
            release.set()
            worker.join(5)
            if other_worker.ident is not None:
                other_worker.join(5)
        self.assertEqual(failures, [])
        self.assertEqual(len(self.calls), 2)

    def test_revocation_waits_for_inflight_action_then_fences_the_next(self):
        entered, release, attempted, revoked = (threading.Event() for _ in range(4))
        self.clients[-1].entered, self.clients[-1].release = entered, release
        failures, results = [], []

        def run():
            try:
                results.append(self.dispatch())
            except BaseException as exc:
                failures.append(exc)

        def revoke():
            attempted.set()
            try:
                self.manager.update(self.cid, enabled=False)
                revoked.set()
            except BaseException as exc:
                failures.append(exc)

        worker = threading.Thread(target=run)
        updater = threading.Thread(target=revoke)
        worker.start()
        try:
            self.assertTrue(entered.wait(3))
            updater.start()
            self.assertTrue(attempted.wait(3))
            self.assertFalse(revoked.wait(0.05))
        finally:
            release.set()
            worker.join(5)
            if updater.ident is not None:
                updater.join(5)
        self.assertFalse(worker.is_alive())
        self.assertFalse(updater.is_alive())
        self.assertEqual(failures, [])
        self.assertTrue(results[0]["ok"])
        self.assertTrue(revoked.is_set())
        self.assertFalse(self.dispatch()["ok"])
        self.assertEqual(len(self.calls), 1)


class _SyntheticHTTP(urllib.request.HTTPHandler, urllib.request.HTTPSHandler):
    """Exercise urllib's real redirect handlers without sockets or external services."""
    handler_order = 100

    def __init__(self, status=302, location="http://other.example.test/capture"):
        super().__init__()
        self.status, self.location, self.requests = status, location, []

    def http_open(self, request):
        self.requests.append(request)
        headers = Message()
        headers["Location"] = self.location
        answer = urllib.response.addinfourl(io.BytesIO(b"synthetic redirect"), headers,
                                           request.full_url, self.status)
        answer.msg = "Synthetic redirect"
        return answer

    https_open = http_open


class ConnectorTransportSecurityTests(unittest.TestCase):
    def redirected_opener(self, status=302, location="http://other.example.test/capture"):
        transport = _SyntheticHTTP(status, location)
        original = urllib.request.build_opener

        def build(*handlers):
            return original(transport, *handlers)

        return transport, patch("jarvis.mcp_client.urllib.request.build_opener", side_effect=build)

    def test_authenticated_mcp_never_follows_any_redirect(self):
        for status in (301, 302, 303, 307, 308):
            for location in ("https://other.example.test/capture", "http://other.example.test/capture",
                             "https://example.test/other", "http://127.0.0.1/capture"):
                with self.subTest(status=status, location=location):
                    wire, redirect = self.redirected_opener(status, location)
                    with redirect:
                        client = mcp_client.HttpTransport("https://example.test/mcp",
                                                          lambda: {"Authorization": "Bearer synthetic-canary"})
                        client.session_id = "synthetic-session"
                        with self.assertRaises(mcp_client.MCPError):
                            client.send({"id": 1, "method": "tools/call"}, 1)
                    self.assertEqual(len(wire.requests), 1)
                    self.assertEqual(wire.requests[0].full_url, "https://example.test/mcp")

    def test_session_close_does_not_follow_redirect(self):
        wire, redirect = self.redirected_opener()
        with redirect:
            client = mcp_client.HttpTransport("https://example.test/mcp", lambda: {"Authorization": "Bearer synthetic"})
            client.session_id = "synthetic"
            client.close()
        self.assertEqual(len(wire.requests), 1)
        self.assertEqual(wire.requests[0].method, "DELETE")

    def test_sse_open_and_send_do_not_follow_redirects(self):
        wire, redirect = self.redirected_opener()
        with redirect, self.assertRaises(mcp_client.MCPError):
            mcp_client.SseTransport("https://example.test/sse", lambda: {"Authorization": "Bearer synthetic"})
        self.assertEqual(len(wire.requests), 1)
        wire, redirect = self.redirected_opener()
        client = mcp_client.SseTransport.__new__(mcp_client.SseTransport)
        client.post_url = "https://example.test/messages"
        client._headers = lambda: {"Authorization": "Bearer synthetic"}
        with redirect, self.assertRaises(mcp_client.MCPError):
            client.send({"method": "notifications/initialized"}, 1)
        self.assertEqual(len(wire.requests), 1)

    def test_sse_endpoint_cannot_change_scheme_origin_or_authority(self):
        for endpoint in ("http://example.test/post", "http://127.0.0.1/post", "https://other.example.test/post",
                         "https://example.test:444/post", "https://user@example.com/post", "file://localhost/data"):
            with self.subTest(endpoint=endpoint):
                stream = Mock()

                def announce(client, value=endpoint):
                    client._endpoint.put(value)

                with patch.object(mcp_client, "_open_http", return_value=stream), \
                        patch.object(mcp_client.SseTransport, "_read", announce), \
                        self.assertRaises(mcp_client.MCPError):
                    mcp_client.SseTransport("https://example.test/sse", timeout=1)
                stream.close.assert_called_once()

    def test_sse_relative_endpoint_on_same_origin_is_accepted(self):
        stream = Mock()

        def announce(client):
            client._endpoint.put("/messages?session=synthetic")

        with patch.object(mcp_client, "_open_http", return_value=stream), \
                patch.object(mcp_client.SseTransport, "_read", announce):
            client = mcp_client.SseTransport("https://example.test/sse", timeout=1)
        self.assertEqual(client.post_url, "https://example.test/messages?session=synthetic")
        client.close()

    def test_unsafe_endpoint_schemes_userinfo_and_fragments_are_refused(self):
        for url in ("file://localhost/data", "ftp://localhost/data", "http://example.test/mcp",
                    "https://user:secret@example.com/mcp", "https://example.test/mcp#secret", "https:///mcp",
                    "https://example.test:99999/mcp", "https://example.test:0/mcp", "https://example.test/\npath"):
            with self.subTest(url=url), self.assertRaises(mcp_client.MCPError):
                mcp_client.HttpTransport(url)
            with self.subTest(oauth=url), self.assertRaises(mcp_oauth.OAuthError):
                mcp_oauth._https(url)

    def test_oauth_metadata_registration_and_token_posts_do_not_redirect(self):
        actions = [lambda: mcp_oauth._get_json("https://example.test/metadata"),
                   lambda: mcp_oauth.register({"registration_endpoint": "https://example.test/register"},
                                             "http://127.0.0.1/callback"),
                   lambda: mcp_oauth._post_form("https://example.test/token", {"refresh_token": "synthetic"})]
        for action in actions:
            wire, redirect = self.redirected_opener(307, "https://other.example.test/capture")
            with self.subTest(action=action), redirect, self.assertRaises(mcp_oauth.OAuthError):
                action()
            self.assertEqual(len(wire.requests), 1)
            self.assertTrue(wire.requests[0].full_url.startswith("https://example.test/"))


if __name__ == "__main__":
    unittest.main()
