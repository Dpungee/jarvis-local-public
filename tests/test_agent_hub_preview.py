"""Build → run → verify → open: a verified local app reaches the operator's preview panel.

The end-to-end tests use real components (a real ToolBox, a real loopback static server
started through the managed-process policy, and a real headless browser) with a scripted
agent in place of a model, so the workflow's gates are exercised without any provider call.
"""
import json
import socket
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from jarvis import agent as agent_module
from jarvis.agent_hub import HubService, HubAuth, HubHTTPServer
from jarvis.natural_language import operator_action_text
from jarvis.policy import validate_process
from jarvis.web_check import browser_executable, run_web_check, validate_local_url

GAME = """<!doctype html><html><head><title>Block Mover</title></head><body>
<p>Score: <span id="score">0</span></p><canvas id="c" width="200" height="200"></canvas>
<button id="restart">Restart</button>
<script>
const ctx = document.getElementById('c').getContext('2d'); let x = 90, y = 0, score = 0;
window.appState = {get x() { return x; }, get score() { return score; }};
function draw() { ctx.fillStyle = '#123'; ctx.fillRect(0, 0, 200, 200); ctx.fillStyle = '#4f4'; ctx.fillRect(x, y, 20, 20); }
setInterval(() => { y = (y + 5) % 200; draw(); }, 100);
addEventListener('keydown', e => {
  if (e.key === 'ArrowLeft') x -= 10;
  if (e.key === 'ArrowRight') x += 10;
  if (e.key === ' ') { score += 10; document.getElementById('score').textContent = score; }
  draw();
});
document.getElementById('restart').onclick = () => { score = 0; x = 90; document.getElementById('score').textContent = 0; };
</script></body></html>"""

BROKEN = "<!doctype html><title>Broken</title><p>Hi</p><script>undefinedFunction();</script>"

ACTIONS = [
    {"type": "inspect", "expression": "window.appState.x"},
    {"type": "key", "key": "ArrowLeft", "repeat": 3},
    {"type": "inspect", "expression": "window.appState.x"},
    {"type": "key", "key": "Space"},
    {"type": "click", "selector": "#restart"},
]


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class IntentTests(unittest.TestCase):
    def classify(self, prompt):
        action = operator_action_text(prompt)
        return (agent_module._requires_web(prompt), agent_module._requires_coding(action),
                bool(agent_module._LAUNCH_INTENT.search(action)))

    def test_building_games_and_web_apps_is_coding_with_launch(self):
        for prompt in ("Build me a simple web Tetris game and open it for me.",
                       "build me a simple web tetris game and than open it up for me",
                       "make a snake game in the browser and launch it",
                       "create a pomodoro timer web page and open it"):
            with self.subTest(prompt=prompt):
                self.assertEqual(self.classify(prompt), (False, True, True))

    def test_ordinary_wording_is_not_rerouted(self):
        self.assertEqual(self.classify("make a game plan for my week")[1], False)
        # Repairing or updating an installed game is not writing code.
        for prompt in ("Fix my game launcher because its window is blank.", "update my game please"):
            self.assertFalse(self.classify(prompt)[1], prompt)
        self.assertEqual(self.classify("what browser do you recommend?"), (False, False, False))
        self.assertTrue(self.classify("can you research the latest news")[0])

    def test_role_text_alone_would_misroute_so_it_never_enters_the_prompt(self):
        # The Hub sends only the operator's words; this documents why.
        polluted = "Your role: Helps me research things.\n\nbuild me a simple web tetris game"
        self.assertTrue(agent_module._requires_web(polluted))


class StaticServerPolicyTests(unittest.TestCase):
    def test_only_loopback_bound_static_serving(self):
        workspace = Path(tempfile.gettempdir())
        allowed = [["-m", "http.server", "8123", "--bind", "127.0.0.1"],
                   ["-m", "http.server", "--bind=::1", "--directory", "game", "8124"]]
        refused = [["-m", "http.server", "8123"], ["-m", "http.server", "--bind", "0.0.0.0"],
                   ["-m", "http.server", "--cgi", "--bind", "127.0.0.1"],
                   ["-m", "http.server", "80", "--bind", "127.0.0.1"],
                   ["-m", "http.server", "--bind", "127.0.0.1", "--directory", "../../.."]]
        for args in allowed:
            self.assertTrue(validate_process(workspace, "python", args)[0], args)
        for args in refused:
            self.assertFalse(validate_process(workspace, "python", args)[0], args)


class LaunchObligationTests(unittest.TestCase):
    def obligation(self, tools, markers):
        fake = SimpleNamespace(toolbox=SimpleNamespace(tools=dict.fromkeys(tools)))
        return agent_module.Agent._web_launch_obligation(fake, True, set(markers))

    def test_hub_requires_browser_verification_then_opening(self):
        tools = ["web_app_check", "open_preview"]
        self.assertIn("not verified in a browser", self.obligation(tools, {"__http_app_launched__"}))
        self.assertIn("not opened", self.obligation(tools, {"__http_app_launched__", "__app_interaction_verified__"}))
        self.assertIsNone(self.obligation(tools, {"__http_app_launched__", "__app_interaction_verified__",
                                                  "__app_opened__"}))

    def test_hosts_without_a_preview_keep_the_earlier_rule(self):
        self.assertIsNone(self.obligation(["web_app_check"], {"__http_app_launched__"}))


@unittest.skipIf(browser_executable() is None, "no Edge/Chrome installation for browser checks")
class WebCheckTests(unittest.TestCase):
    def serve(self, html):
        import http.server
        import threading
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        Path(root.name, "index.html").write_text(html, encoding="utf-8")
        handler = lambda *a, **k: http.server.SimpleHTTPRequestHandler(*a, directory=root.name, **k)  # noqa: E731
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_port}/"

    def test_playable_page_is_verified_through_state(self):
        result = run_web_check(self.serve(GAME), actions=ACTIONS)
        self.assertTrue(result["verified"], result["reasons_not_verified"])
        self.assertTrue(result["animating_without_input"])
        self.assertTrue(any(step.get("changed_since_input") for step in result["steps"]))

    def test_input_response_does_not_hide_missing_required_control(self):
        result = run_web_check(self.serve(GAME), actions=[
            {"type": "key", "key": "Space"},
            {"type": "click", "selector": "#missing-required-control"},
        ])
        self.assertTrue(result["responded_to_input"])
        self.assertEqual(result["steps"][-1]["error"], "selector matched nothing")
        self.assertFalse(result["verified"])
        self.assertTrue(any("requested checks failed" in reason for reason in result["reasons_not_verified"]))

    def test_input_response_does_not_hide_failed_state_inspection(self):
        result = run_web_check(self.serve(GAME), actions=[
            {"type": "key", "key": "Space"},
            {"type": "inspect", "expression": "window.missingRequiredState.score"},
        ])
        self.assertTrue(result["responded_to_input"])
        self.assertIn("error", result["steps"][-1])
        self.assertFalse(result["verified"])
        self.assertTrue(any("requested checks failed" in reason for reason in result["reasons_not_verified"]))

    def test_script_errors_fail_verification(self):
        result = run_web_check(self.serve(BROKEN), actions=[{"type": "click", "x": 10, "y": 10}])
        self.assertFalse(result["verified"])
        self.assertTrue(result["errors"])

    def test_no_input_is_not_verification(self):
        result = run_web_check(self.serve(GAME), actions=[])
        self.assertFalse(result["verified"])

    def test_external_requests_are_blocked_and_inspect_is_read_only(self):
        page = GAME.replace("<body>", '<body><img src="https://example.com/pixel.png" alt="">')
        result = run_web_check(self.serve(page), actions=ACTIONS + [
            {"type": "inspect", "expression": "document.title = 'changed'"}])
        self.assertIn("https://example.com/pixel.png", result["blocked_non_local_requests"])
        self.assertIn("side-effect", result["steps"][-1]["error"])
        self.assertEqual(result["title"], "Block Mover")
        self.assertFalse(result["verified"])
        self.assertTrue(any("requested checks failed" in reason for reason in result["reasons_not_verified"]))

    def test_only_loopback_targets(self):
        for url in ("http://example.com/", "https://127.0.0.1:1/", "http://10.0.0.5:8000/", "file:///C:/x"):
            with self.assertRaises((PermissionError, ValueError)):
                validate_local_url(url)


@unittest.skipIf(browser_executable() is None, "no Edge/Chrome installation for browser checks")
class HubPreviewFlowTests(unittest.TestCase):
    """The scripted agent drives the real tools through the Hub executor."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.script = []
        self.outputs = []
        case = self

        class Result(str):
            status = "complete"
            model = "claude-cli:claude-opus-5-5"
            tool_calls = 0

        class ScriptedAgent:
            def __init__(self, config, memory, on_event, client):
                from jarvis.tools import ToolBox
                self.toolbox = ToolBox(config, memory)
                self.operator_brief = None

            def run(self, prompt, **kwargs):
                for name, arguments in case.script:
                    arguments = arguments(case.outputs) if callable(arguments) else arguments
                    case.outputs.append(json.loads(self.toolbox.execute(name, arguments)))
                return Result("done")

        self.service = HubService(state_dir=self.root, provider_profile_dir=self.root / "profile",
                                  runtime_kwargs={"autostart": False,
                                                  "provider_probe": lambda _: {"installed": True, "authenticated": True,
                                                                               "detail": "test", "version": "test"},
                                                  "agent_factory": ScriptedAgent,
                                                  "client_factory": lambda config: SimpleNamespace()})
        self.addCleanup(self.cleanup)
        agent = self.service.create_agent({"name": "Builder", "role": "Builds things", "provider": "claude-cli",
                                           "model": "claude-opus-5-5", "enable": True,
                                           "permissions": {"files_read": True, "files_write": True,
                                                           "web_research": False, "run_commands": True,
                                                           "memory": False}})
        self.agent_id = agent["agent_id"]
        self.chat_id = self.service.create_chat(self.agent_id, {"title": "Build"})["chat_id"]
        self.port = free_port()
        self.url = f"http://127.0.0.1:{self.port}/"

    def cleanup(self):
        from jarvis.tools import stop_registered_process
        for preview in self.service.runtime.previews():
            if preview["state"] == "RUNNING":
                self.service.runtime.stop_preview(preview["preview_id"])
        data_dir = self.root / "agents" / self.agent_id / "data"
        for output in self.outputs:
            process_id = (output.get("result") or {}).get("process_id") if output.get("ok") else None
            if process_id:
                stop_registered_process(data_dir, process_id)
        self.service.close()
        self.tmp.cleanup()

    def run_turn(self):
        self.service.chat(self.agent_id, self.chat_id, {"body": "Build me a small game and open it for me.",
                                                        "request_id": uuid.uuid4().hex})
        runtime = self.service.runtime
        with patch.object(runtime, "_open_memory", wraps=runtime._open_memory):
            runtime.dispatch_once()
            deadline = time.monotonic() + 120
            while runtime.running_ids() and time.monotonic() < deadline:
                time.sleep(0.05)
        return self.service.chat(self.agent_id, self.chat_id)

    def build_and_serve(self):
        return [
            ("write_file", {"path": "game/index.html", "content": GAME}),
            ("start_process", {"program": "python", "name": "Block Mover",
                               "arguments": ["-m", "http.server", str(self.port), "--bind", "127.0.0.1",
                                             "--directory", "game"]}),
            ("http_health", lambda out: {"url": self.url, "process_id": out[1]["result"]["process_id"],
                                         "retries": 10, "interval_ms": 200}),
        ]

    def test_verified_app_opens_stays_running_and_can_be_stopped_and_restarted(self):
        self.script = self.build_and_serve() + [
            ("web_app_check", lambda out: {"url": self.url, "process_id": out[1]["result"]["process_id"],
                                           "actions": ACTIONS}),
            ("open_preview", lambda out: {"url": self.url, "process_id": out[1]["result"]["process_id"],
                                          "title": "Block Mover"}),
        ]
        chat = self.run_turn()
        self.assertTrue(self.outputs[3]["result"]["verified"], self.outputs[3])
        self.assertTrue(self.outputs[4]["result"]["opened"], self.outputs[4])
        previews = chat["agent_turns"][-1]["previews"]
        self.assertEqual([(p["url"], p["state"]) for p in previews], [(self.url, "RUNNING")])
        # The server outlived the agent's run, so the operator can play.
        with socket.create_connection(("127.0.0.1", self.port), timeout=5):
            pass
        preview_id = previews[0]["preview_id"]
        self.assertEqual(self.service.runtime.stop_preview(preview_id)["state"], "STOPPED")
        with self.assertRaises(OSError):
            socket.create_connection(("127.0.0.1", self.port), timeout=2).close()
        restarted = self.service.runtime.start_preview(preview_id)
        self.assertEqual(restarted["state"], "RUNNING")
        with socket.create_connection(("127.0.0.1", self.port), timeout=5):
            pass

    def test_opening_requires_a_passing_browser_check(self):
        self.script = self.build_and_serve() + [
            ("open_preview", lambda out: {"url": self.url, "process_id": out[1]["result"]["process_id"]}),
        ]
        self.run_turn()
        self.assertFalse(self.outputs[3]["ok"])
        self.assertIn("web_app_check", self.outputs[3]["error"])
        self.assertEqual(self.service.runtime.previews(), [])

    def test_opening_refuses_processes_this_run_did_not_start_and_the_hub_itself(self):
        self.service.runtime.reserved_ports.add(self.port)
        self.script = self.build_and_serve() + [
            ("open_preview", {"url": self.url, "process_id": "0123456789ab"}),
            ("open_preview", lambda out: {"url": self.url, "process_id": out[1]["result"]["process_id"]}),
        ]
        self.run_turn()
        self.assertFalse(self.outputs[3]["ok"])
        self.assertFalse(self.outputs[4]["ok"])
        self.assertIn("Agent Hub itself", self.outputs[4]["error"])

    def test_a_port_held_by_another_program_never_counts_as_this_app(self):
        import http.server
        import threading
        foreign = http.server.ThreadingHTTPServer(("127.0.0.1", self.port), http.server.SimpleHTTPRequestHandler)
        threading.Thread(target=foreign.serve_forever, daemon=True).start()
        self.addCleanup(foreign.server_close)
        self.addCleanup(foreign.shutdown)
        self.script = self.build_and_serve() + [
            ("web_app_check", lambda out: {"url": self.url, "process_id": out[1]["result"]["process_id"],
                                           "actions": ACTIONS}),
        ]
        self.run_turn()
        health, check = self.outputs[2], self.outputs[3]
        self.assertFalse(health["ok"] and health["result"]["healthy"], health)
        self.assertFalse(check["ok"] and check["result"]["verified"], check)

    def test_restart_marks_running_previews_stopped(self):
        self.script = self.build_and_serve() + [
            ("web_app_check", lambda out: {"url": self.url, "process_id": out[1]["result"]["process_id"],
                                           "actions": ACTIONS}),
            ("open_preview", lambda out: {"url": self.url, "process_id": out[1]["result"]["process_id"]}),
        ]
        self.run_turn()
        runtime = self.service.runtime
        preview = runtime.previews()[0]
        runtime.stop_preview(preview["preview_id"])
        with runtime.db() as db:
            db.execute("UPDATE hub_previews SET state='RUNNING'")
        runtime._initialize()
        self.assertEqual(runtime.preview(preview["preview_id"])["state"], "STOPPED")


@unittest.skipIf(__import__("os").name != "nt", "port ownership lookup is Windows-only")
class PortOwnershipTests(unittest.TestCase):
    def test_port_owner_distinguishes_this_process_others_and_nobody(self):
        import http.server
        import os
        import threading
        from jarvis.local_ports import port_owner
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), http.server.SimpleHTTPRequestHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        port = server.server_port
        self.assertEqual(port_owner(port, os.getpid()), "self")
        self.assertEqual(port_owner(port, 4), "other")
        self.assertEqual(port_owner(free_port(), os.getpid()), "none")


class PreviewIsolationTests(unittest.TestCase):
    def test_hub_frames_only_loopback_apps_and_is_never_framed(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        service = HubService(state_dir=root, provider_profile_dir=root / "profile",
                             runtime_kwargs={"autostart": False, "provider_probe": lambda _: {
                                 "installed": True, "authenticated": True, "detail": "t", "version": "t"}})
        server = HubHTTPServer(("127.0.0.1", 0), service, HubAuth(root))
        import threading
        import urllib.request
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}/") as response:
                policy = response.headers["Content-Security-Policy"]
                self.assertEqual(response.headers["X-Frame-Options"], "DENY")
        finally:
            server.shutdown()
            server.server_close()
            service.close()
        self.assertIn("frame-src http://127.0.0.1:* http://localhost:* http://[::1]:*;", policy)
        self.assertIn("frame-ancestors 'none'", policy)
        self.assertIn("script-src 'self'", policy)


if __name__ == "__main__":
    unittest.main()
