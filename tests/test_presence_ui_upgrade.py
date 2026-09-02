"""Contracts for the Presence interface upgrade.

Covers the grouped shell, safe markdown rendering, the command palette, the
adaptive polling cadence, and the additive API endpoints (memory search,
activity, tasks, schedule toggles, preferences, conversation rename).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from jarvis.config import Config
from jarvis.memory import Memory
from jarvis.presence import PresenceHTTPServer, PresenceRuntime

ROOT = Path(__file__).resolve().parents[1]
PAGE = (ROOT / "jarvis" / "presence.html").read_text(encoding="utf-8")
STYLE = (ROOT / "jarvis" / "presence.css").read_text(encoding="utf-8")
SCRIPT = (ROOT / "jarvis" / "presence.js").read_text(encoding="utf-8")

try:  # ``unittest discover -s tests`` puts the tests directory on sys.path.
    from test_presence_js_behavior import DOM_HARNESS, function_block
except ImportError:  # pragma: no cover - package-style invocation
    from tests.test_presence_js_behavior import DOM_HARNESS, function_block


class PresenceShellStaticTests(unittest.TestCase):
    def test_shell_exposes_new_navigation_dialogs_and_controls(self):
        for marker in (
            'data-view="overview"', 'data-view="memory"', 'data-view="activity"',
            'data-view="customize"><span class="nav-icon">☷</span><span>Settings</span>',
            'id="open-palette"', 'id="palette-dialog"', 'id="palette-input"', 'id="palette-list"',
            'id="shortcuts-dialog"', 'id="confirm-dialog"', 'id="rename-chat-dialog"',
            'id="chat-search"', 'id="theme-toggle"', 'id="refresh-utility"',
            'id="scroll-to-bottom"', 'id="quick-actions"', 'id="runtime-quick"',
            'id="prompt-counter"', 'class="nav-group-title"', 'content="dark light"',
        ):
            self.assertIn(marker, PAGE, marker)
        for selector in (
            '[data-theme="light"]', '[data-density="compact"]', ".palette-item",
            ".code-block", ".md-table", ".message-actions", ".row-menu",
            ".overview-grid", ".activity-row", ".memory-result",
            ".new-network-device-facts, .network-defense-incident-facts",
            "@media (max-width: 760px)", "@media (prefers-reduced-motion: reduce)",
        ):
            self.assertIn(selector, STYLE, selector)

    def test_script_keeps_text_only_rendering_and_registers_new_views(self):
        self.assertNotIn("innerHTML", SCRIPT)
        self.assertNotIn("insertAdjacentHTML", SCRIPT)
        self.assertNotIn("eval(", SCRIPT)
        self.assertNotIn("new Function", SCRIPT)
        open_utility = function_block(SCRIPT, "openUtility")
        for renderer, view in (
            ("renderOverview", "overview"),
            ("renderMemory", "memory"),
            ("renderActivity", "activity"),
        ):
            self.assertIn(f"{renderer}(generation)", open_utility)
            block = function_block(SCRIPT, renderer)
            self.assertIn("generation = null", block)
            self.assertIn(f'beginUtilityRender("{view}", generation)', block)
            self.assertIn("if (!render) return;", block)
            self.assertIn("if (!isUtilityRenderCurrent(render)) return;", block)
        # The event loop keeps its job-correlated cadence and adds idle backoff.
        self.assertIn("adaptivePollDelay(currentJobId() ? 150 : 700)", SCRIPT)
        self.assertIn('post("/api/memory/search"', SCRIPT)
        self.assertIn('post("/api/tasks"', SCRIPT)
        self.assertIn('post("/api/control"', SCRIPT)
        self.assertIn("/api/activity?limit=", SCRIPT)
        self.assertIn("/api/memory/recent?limit=", SCRIPT)
        self.assertIn("/rename`", SCRIPT)
        self.assertIn('event.kind === "conversation_renamed"', SCRIPT)
        self.assertIn('event.kind === "task_queued"', SCRIPT)

    def test_backend_registers_additive_routes(self):
        backend = (ROOT / "jarvis" / "presence.py").read_text(encoding="utf-8")
        for route in (
            '"/api/memory/recent"', '"/api/memory/search"', '"/api/activity"',
            '"/api/preferences"', '"/api/tasks"',
            'r"/api/schedule/(learning|backlog)/([1-9][0-9]{0,18})/(enable|disable)"',
            'r"/api/conversations/([1-9][0-9]{0,18})/rename"',
        ):
            self.assertIn(route, backend, route)


@unittest.skipUnless(shutil.which("node"), "Node.js is required for Presence behavior tests")
class PresenceUpgradeBehaviorTests(unittest.TestCase):
    node = shutil.which("node") or "node"

    def run_harness(self, functions: tuple[str, ...], body: str) -> str:
        source = DOM_HARNESS + "\n" + "\n".join(
            function_block(SCRIPT, name) for name in functions
        ) + "\n" + body
        completed = subprocess.run(
            [self.node, "-e", source], cwd=ROOT, text=True,
            capture_output=True, timeout=20, check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
        return completed.stdout

    MARKDOWN = (
        "safeHttpUrl", "trimBareUrl", "renderLinkedText", "splitTableRow",
        "markdownBlocks", "renderInline", "makeCodeBlock", "renderList",
        "renderTable", "renderMarkdown", "copyToClipboard",
    )

    def test_markdown_renders_structure_without_evaluating_html(self):
        self.run_harness(
            self.MARKDOWN,
            r"""
const container = new Element("div");
renderMarkdown(container, [
  "## Plan <b>x</b>",
  "Use `pip install x` with **bold `code`** and *em* ~~gone~~ [Docs](https://example.com/d) https://example.com/raw.",
  "",
  "- one",
  "- two",
  "  - nested",
  "1. first",
  "- [x] done",
  "",
  "```python",
  "print('<script>alert(1)</script>')",
  "```",
  "",
  "| a | b |",
  "|---|---|",
  "| 1 | <img src=x onerror=boom> |",
  "",
  "> quoted **strong**",
  "---",
  "javascript:alert(1) [bad](javascript:alert(1))",
].join("\n"));
assert(container.classList.contains("markdown"), "markdown class missing");
const headings = container.querySelectorAll("h4");
assert(headings.length === 1 && headings[0].textContent === "Plan <b>x</b>", "heading text was not preserved literally");
assert(container.querySelectorAll("strong").length === 2, "bold runs missing");
assert(container.querySelectorAll("code").length === 3, `code spans/blocks: ${container.querySelectorAll("code").length}`);
const links = container.querySelectorAll("a");
assert(links.length === 2, `expected two safe links, got ${links.length}`);
assert(links.every((link) => link.rel === "noopener noreferrer"), "link rel missing");
assert(container.querySelectorAll("ul").length === 3, `ul count ${container.querySelectorAll("ul").length}`);
assert(container.querySelectorAll("ol").length === 1, "ordered list missing");
assert(container.querySelectorAll("input").length === 1, "task checkbox missing");
assert(container.querySelectorAll("pre").length === 1, "code block missing");
assert(container.querySelectorAll(".code-copy").length === 1, "copy button missing");
assert(container.querySelectorAll("table").length === 1, "table missing");
assert(container.querySelectorAll("th").length === 2 && container.querySelectorAll("td").length === 2, "table cells");
assert(container.querySelectorAll("blockquote").length === 1, "quote missing");
assert(container.querySelectorAll("hr").length === 1, "rule missing");
assert(container.querySelectorAll("img").length === 0 && container.querySelectorAll("script").length === 0, "HTML text became elements");
assert(container.textContent.includes("<img src=x onerror=boom>"), "HTML text was dropped");
assert(container.textContent.includes("javascript:alert(1)"), "unsafe scheme text disappeared");
""",
        )

    def test_message_cards_carry_time_raw_text_and_actions(self):
        self.run_harness(
            self.MARKDOWN + ("formatMessageTime", "messageActions", "renderMessageContent", "appendMessage"),
            r"""
const messages = new Element("main");
const state = {streamNodes: new Map(), projectId: null};
const imageArtifactPattern = /\[\[jarvis-image:([A-Za-z0-9][A-Za-z0-9._/-]{0,999})\]\]/g;
const user = appendMessage("user", "hello **there**", "", [], messages, {time: "2026-09-02T10:15:00"});
assert(user._raw === "hello **there**", "raw text not kept");
assert(user._time.length > 0, "time label missing");
const actions = user.querySelector(".message-actions");
assert(actions && actions.querySelectorAll("button").length === 2, "user actions should be Copy + Edit");
const assistant = appendMessage("assistant", "", "responding…", [], messages);
assert(assistant.querySelector(".message-actions").hidden === true, "empty placeholder must hide actions");
const reply = appendMessage("assistant", "# Title\nbody", "", [], messages);
assert(reply.querySelectorAll("h3").length === 1, "assistant markdown not rendered");
assert(reply.querySelector(".message-actions").querySelectorAll("button").length === 3, "assistant actions should be Copy + Regenerate + Quote");
""",
        )

    def test_palette_filter_ranks_prefix_matches_and_bounds_results(self):
        self.run_harness(
            ("scorePaletteItem", "filterPaletteItems"),
            r"""
const items = [
  {label: "New chat", keywords: "create"},
  {label: "Review approvals", detail: "2 pending"},
  {label: "Open project Chat archive"},
  ...Array.from({length: 60}, (_, index) => ({label: `Chat ${index}`})),
];
assert(filterPaletteItems(items, "").length === 40, "empty query must return the bounded list in order");
const chat = filterPaletteItems(items, "chat");
assert(chat[0].label === "New chat" || chat[0].label.startsWith("Chat"), `prefix ranking: ${chat[0].label}`);
assert(chat.every((item) => item.label.toLowerCase().includes("chat")), "non-matching item leaked");
assert(filterPaletteItems(items, "approv pend").length === 1, "multi-word query should match across fields");
assert(filterPaletteItems(items, "zzz").length === 0, "no match should be empty");
""",
        )

    def test_relative_time_and_poll_cadence_are_bounded(self):
        self.run_harness(
            ("relativeTime", "adaptivePollDelay"),
            r"""
const now = Date.parse("2026-09-02T12:00:00Z");
assert(relativeTime("2026-09-02T11:59:40Z", now) === "just now", "just now");
assert(relativeTime("2026-09-02T11:30:00Z", now) === "30m ago", "minutes");
assert(relativeTime("2026-09-02T09:00:00Z", now) === "3h ago", "hours");
assert(relativeTime("2026-08-30T12:00:00Z", now) === "3d ago", "days");
assert(relativeTime("nonsense", now) === "", "invalid");
const state = {lastActivityAt: Date.now()};
document.hidden = false;
assert(adaptivePollDelay(150) === 150, "busy cadence must stay fast");
assert(adaptivePollDelay(700) === 700, "active idle cadence unchanged");
state.lastActivityAt = Date.now() - 45000;
assert(adaptivePollDelay(700) === 1500, "30s idle backs off");
state.lastActivityAt = Date.now() - 200000;
assert(adaptivePollDelay(700) === 3000, "2m idle backs off further");
document.hidden = true;
assert(adaptivePollDelay(700) === 4000, "hidden tab polls slowly");
""",
        )


class _FakeRuntime:
    runtime_epoch = "b" * 32

    def __init__(self) -> None:
        self.searches: list[tuple[str, int]] = []
        self.tasks: list[tuple[str, int | None, str]] = []
        self.toggles: list[tuple[str, int, bool]] = []
        self.renames: list[tuple[int, str]] = []
        self.preferences_set: list[tuple[str, str]] = []

    def status(self):
        return {"runtime_epoch": self.runtime_epoch, "ready": True, "uptime_seconds": 1}

    def recent_memories(self, limit=30):
        return [{"created_at": "2026-09-02T00:00:00", "kind": "fact", "content": f"limit {limit}", "source": "test"}]

    def search_memory(self, query, limit=20):
        self.searches.append((query, limit))
        if not query.strip():
            raise ValueError("Search query must not be empty")
        return {"query": query, "results": [], "report": {"mode": "empty", "abstained": False}}

    def activity(self, limit=200):
        return [{"id": 1, "created_at": "2026-09-02T00:00:00", "category": "control", "action": "paused", "status": "complete", "task_id": None, "details": f"limit {limit}"}]

    def preferences(self):
        return [{"id": 1, "name": "units", "value": "metric", "source": "user", "confidence": 1.0, "updated_at": ""}]

    def set_preference(self, name, value):
        self.preferences_set.append((name, value))
        if not name:
            raise ValueError("Preference name/value is empty or too long")
        return 7

    def queue_task(self, prompt, project_id=None, model="auto"):
        self.tasks.append((prompt, project_id, model))
        if not prompt.strip():
            raise ValueError("Task prompt must not be empty")
        return 41

    def set_learning_topic_enabled(self, topic_id, enabled):
        self.toggles.append(("learning", topic_id, enabled))
        return topic_id != 99

    def set_backlog_enabled(self, backlog_id, enabled):
        self.toggles.append(("backlog", backlog_id, enabled))
        return True

    def rename_conversation(self, conversation_id, title):
        self.renames.append((conversation_id, title))
        if conversation_id == 404:
            raise LookupError("Conversation does not exist")
        if not title.strip():
            raise ValueError("Title must not be empty")
        return {"conversation_id": conversation_id, "title": title.strip()}


class PresenceUpgradeRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runtime = _FakeRuntime()
        self.server = PresenceHTTPServer(("127.0.0.1", 0), self.runtime)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def call(self, path, payload=None, method=None):
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"} if body is not None else {}
        request = urllib.request.Request(self.base + path, data=body, headers=headers, method=method or ("POST" if body is not None else "GET"))
        try:
            with urllib.request.urlopen(request, timeout=2) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            return error.code, json.load(error)

    def test_read_routes_validate_limits(self):
        status, payload = self.call("/api/memory/recent?limit=5")
        self.assertEqual(status, 200)
        self.assertEqual(payload["memories"][0]["content"], "limit 5")
        self.assertEqual(self.call("/api/memory/recent?limit=999")[0], 400)
        self.assertEqual(self.call("/api/memory/recent?limit=abc")[0], 400)
        status, payload = self.call("/api/activity?limit=50")
        self.assertEqual(status, 200)
        self.assertEqual(payload["activity"][0]["details"], "limit 50")
        self.assertEqual(self.call("/api/activity?limit=0")[0], 400)
        status, payload = self.call("/api/preferences")
        self.assertEqual(status, 200)
        self.assertEqual(payload["preferences"][0]["name"], "units")

    def test_memory_search_is_a_bounded_post(self):
        status, payload = self.call("/api/memory/search", {"q": "zephyr calibration", "limit": 10})
        self.assertEqual(status, 200)
        self.assertEqual(payload["query"], "zephyr calibration")
        self.assertEqual(self.runtime.searches, [("zephyr calibration", 10)])
        self.assertEqual(self.call("/api/memory/search", {"q": "x", "limit": 500})[0], 400)
        self.assertEqual(self.call("/api/memory/search", {"q": "x", "limit": True})[0], 400)
        self.assertEqual(self.call("/api/memory/search", {"q": "   "})[0], 400)

    def test_task_schedule_preference_and_rename_writes(self):
        status, payload = self.call("/api/tasks", {"prompt": "Digest research", "project_id": 3, "model": "fast"})
        self.assertEqual((status, payload["task_id"]), (201, 41))
        self.assertEqual(self.runtime.tasks[-1], ("Digest research", 3, "fast"))
        self.assertEqual(self.call("/api/tasks", {"prompt": "x", "project_id": 0})[0], 400)
        self.assertEqual(self.call("/api/tasks", {"prompt": ""})[0], 400)
        self.assertEqual(self.call("/api/schedule/learning/5/disable", {})[1], {"changed": True, "enabled": False})
        self.assertEqual(self.call("/api/schedule/backlog/8/enable", {})[1], {"changed": True, "enabled": True})
        self.assertEqual(self.call("/api/schedule/learning/99/enable", {})[0], 404)
        self.assertEqual(self.call("/api/schedule/other/1/enable", {})[0], 404)
        self.assertEqual(self.runtime.toggles[0], ("learning", 5, False))
        status, payload = self.call("/api/preferences", {"name": "units", "value": "metric"})
        self.assertEqual((status, payload["preference_id"]), (201, 7))
        self.assertEqual(self.call("/api/preferences", {"name": "", "value": "x"})[0], 400)
        status, payload = self.call("/api/conversations/12/rename", {"title": "  Sprint notes "})
        self.assertEqual((status, payload["title"]), (200, "Sprint notes"))
        self.assertEqual(self.call("/api/conversations/404/rename", {"title": "x"})[0], 404)
        self.assertEqual(self.call("/api/conversations/12/rename", {"title": " "})[0], 400)


class PresenceUpgradeRuntimeTests(unittest.TestCase):
    def _runtime(self, root: Path) -> PresenceRuntime:
        workspace = root / "workspace"
        data = root / "data"
        workspace.mkdir()
        data.mkdir()
        config = Config(
            root=root, workspace=workspace, data_dir=data, soul_path=root / "SOUL.md",
            model="auto", fast_model="openai:gpt-test", reasoning_model="openai:gpt-test",
            coding_model="openai:gpt-test", deep_model="openai:gpt-test",
            ollama_url="http://127.0.0.1:11434", ollama_api_key=None, max_steps=5,
            context_length=4096, command_timeout=30, autonomy="autonomous",
        )
        return PresenceRuntime(config)

    def test_memory_activity_tasks_and_rename_against_real_memory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime = self._runtime(root)
            with Memory(runtime.config.data_dir / "jarvis.db") as memory:
                memory.remember_verified(
                    "The observatory dome uses a walnut oil lamp.",
                    kind="fact", source="test", origin="explicit_operator_memory",
                )
                memory.set_control_state("paused", "operator break")
            secret = "sk-proj-" + "A" * 32
            recent = runtime.recent_memories(limit=10)
            self.assertTrue(any("walnut oil" in row["content"] for row in recent))
            found = runtime.search_memory("walnut oil lamp", limit=5)
            self.assertTrue(found["results"], "ordinary search should find the stored fact")
            self.assertIsInstance(found["report"], dict)
            refused = runtime.search_memory(f"token {secret}", limit=5)
            self.assertEqual(refused["results"], [])
            self.assertNotIn(secret, json.dumps(refused))
            with self.assertRaises(ValueError):
                runtime.search_memory("   ")
            with self.assertRaises(ValueError):
                runtime.search_memory("x" * 501)

            rows = runtime.activity(limit=20)
            self.assertTrue(any(row["category"] == "control" and row["action"] == "paused" for row in rows))
            self.assertTrue(all(len(row["details"]) <= 400 for row in rows))

            task_id = runtime.queue_task("Summarize the research folder", project_id=None, model="reasoning")
            self.assertGreater(task_id, 0)
            with Memory(runtime.config.data_dir / "jarvis.db") as memory:
                task = next(row for row in memory.list_tasks(limit=5) if row["id"] == task_id)
            self.assertEqual(task["requested_model"], "reasoning")
            with self.assertRaises(ValueError):
                runtime.queue_task("x", model="gpt-secret-model")
            queued = [event for event in runtime.events_after(0) if event["kind"] == "task_queued"]
            self.assertEqual(queued[-1]["payload"]["task_id"], task_id)

            conversation_id = runtime.create_conversation("Presence chat")
            with Memory(runtime.config.data_dir / "jarvis.db") as memory:
                memory.add_message(conversation_id, "user", "hello")
            renamed = runtime.rename_conversation(conversation_id, f"  Sprint {secret} plan ")
            self.assertNotIn(secret, renamed["title"])
            self.assertTrue(renamed["title"].startswith("Sprint"))
            titles = {row["id"]: row["title"] for row in runtime.conversations()}
            self.assertEqual(titles[conversation_id], renamed["title"])
            with self.assertRaises(LookupError):
                runtime.rename_conversation(conversation_id + 1000, "ghost")
            messages = runtime.conversation_messages(conversation_id)
            self.assertEqual(messages[-1]["content"], "hello")
            self.assertTrue(messages[-1]["created_at"])

            preference_id = runtime.set_preference("Units", "metric")
            self.assertGreater(preference_id, 0)
            self.assertEqual(runtime.preferences()[0]["name"], "units")
            runtime.shutdown()


if __name__ == "__main__":
    unittest.main()
