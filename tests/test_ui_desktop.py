"""Tests for the JARVIS Desktop rendering helpers and session commands.

These never create a Tk window: the markdown parser, inline splitter, title
derivation and date grouping are pure functions, and the worker-thread
session is exercised with fake Memory/Agent objects.
"""

from __future__ import annotations

import queue
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from jarvis.ui import (
    DEFAULT_CHAT_TITLE,
    JarvisSession,
    Message,
    chat_group_label,
    chat_title_from_prompt,
    format_elapsed,
    inline_runs,
    parse_markdown,
    render_table_text,
    safe_http_url,
)


class MarkdownParserTests(unittest.TestCase):
    def test_blocks_cover_headings_code_lists_quotes_tables_and_rules(self):
        text = (
            "## Plan\n"
            "Intro line one\nIntro line two\n\n"
            "```python\nprint('hi')\n```\n"
            "- first\n- second\n  continued\n"
            "1. one\n2) two\n"
            "> quoted\n> more\n"
            "---\n"
            "| a | b |\n|---|---|\n| 1 | 2 |\n"
            "- [x] done\n- [ ] todo\n"
        )
        blocks = parse_markdown(text)
        kinds = [block["type"] for block in blocks]
        self.assertEqual(
            kinds,
            ["heading", "paragraph", "code", "list", "list", "quote", "hr", "table", "list"],
        )
        self.assertEqual(blocks[0]["level"], 2)
        self.assertEqual(blocks[0]["text"], "Plan")
        self.assertEqual(blocks[1]["text"], "Intro line one\nIntro line two")
        self.assertEqual(blocks[2]["lang"], "python")
        self.assertEqual(blocks[2]["text"], "print('hi')")
        self.assertFalse(blocks[3]["ordered"])
        self.assertEqual(blocks[3]["items"][1]["text"], "second\ncontinued")
        self.assertTrue(blocks[4]["ordered"])
        self.assertEqual(blocks[5]["text"], "quoted\nmore")
        self.assertEqual(blocks[7]["rows"], [["a", "b"], ["1", "2"]])
        self.assertEqual([item["checked"] for item in blocks[8]["items"]], [True, False])

    def test_unterminated_fence_and_unknown_syntax_are_never_dropped(self):
        blocks = parse_markdown("```\nstill code\nno closing fence")
        self.assertEqual(blocks[0]["type"], "code")
        self.assertIn("still code", blocks[0]["text"])
        plain = parse_markdown("<img src=x onerror=alert(1)> plain text")
        self.assertEqual(plain[0]["type"], "paragraph")
        self.assertIn("<img src=x onerror=alert(1)>", plain[0]["text"])
        self.assertEqual(parse_markdown(""), [])

    def test_inline_runs_split_code_links_and_emphasis(self):
        runs = inline_runs(
            "Use `pip install x` then **bold** and *italic* and ~~gone~~ "
            "[Docs](https://example.com/d) or https://example.com/raw, done"
        )
        styles = [(style, text) for style, text, _url in runs]
        self.assertIn(("code", "pip install x"), styles)
        self.assertIn(("bold", "bold"), styles)
        self.assertIn(("italic", "italic"), styles)
        self.assertIn(("strike", "gone"), styles)
        links = [(text, url) for style, text, url in runs if style == "link"]
        self.assertEqual(links, [("Docs", "https://example.com/d"), ("https://example.com/raw", "https://example.com/raw")])
        trailing = "".join(text for style, text, _url in runs[-2:] if style == "text")
        self.assertEqual(trailing, ", done")

    def test_inline_runs_leave_snake_case_and_math_alone(self):
        runs = inline_runs("snake_case_name and 2*3*4 stay literal")
        self.assertEqual(runs, [("text", "snake_case_name and 2*3*4 stay literal", None)])

    def test_unsafe_urls_are_rejected(self):
        self.assertIsNone(safe_http_url("javascript:alert(1)"))
        # Credentials in the authority are refused; the host is a bare IPv6
        # literal so the fixture never reads as an email address.
        self.assertIsNone(safe_http_url("https://user:pw@[::1]/"))
        self.assertEqual(safe_http_url("https://example.com/a?b=1"), "https://example.com/a?b=1")

    def test_table_text_is_aligned_and_bounded(self):
        rendered = render_table_text([["Name", "Value"], ["alpha", "1"], ["b", "x" * 80]])
        lines = rendered.splitlines()
        self.assertEqual(len(lines), 4)
        self.assertTrue(lines[1].startswith("─"))
        self.assertLessEqual(max(len(line) for line in lines), 48 + 2 + 48)


class TitleAndTimeTests(unittest.TestCase):
    def test_chat_title_is_short_clean_and_never_empty(self):
        self.assertEqual(chat_title_from_prompt("   "), DEFAULT_CHAT_TITLE)
        self.assertEqual(chat_title_from_prompt("# Fix the login bug\nplease"), "Fix the login bug please")
        long_title = chat_title_from_prompt("word " * 40)
        self.assertLessEqual(len(long_title), 57)
        self.assertTrue(long_title.endswith("…"))
        secret = "sk-proj-" + "A" * 32
        self.assertNotIn(secret, chat_title_from_prompt(f"use token {secret} now"))

    def test_chat_groups_follow_calendar_days(self):
        now = datetime(2026, 9, 2, 10, 0, 0)
        self.assertEqual(chat_group_label("2026-09-02T01:00:00", now), "Today")
        self.assertEqual(chat_group_label("2026-09-01T23:00:00", now), "Yesterday")
        self.assertEqual(chat_group_label("2026-08-29T12:00:00", now), "Previous 7 days")
        self.assertEqual(chat_group_label("2026-08-10T12:00:00", now), "Previous 30 days")
        self.assertEqual(chat_group_label("2025-01-01T00:00:00", now), "Earlier")
        self.assertEqual(chat_group_label("not a date", now), "Earlier")

    def test_elapsed_formatting(self):
        self.assertEqual(format_elapsed(None), "")
        self.assertEqual(format_elapsed(0.25), "250 ms")
        self.assertEqual(format_elapsed(12.34), "12.3s")
        self.assertEqual(format_elapsed(125), "2m 05s")

    def test_message_defaults(self):
        message = Message("assistant", "hi")
        self.assertEqual(message.status, "complete")
        self.assertFalse(message.working)
        self.assertEqual(message.steps, [])


class _Result(str):
    status = "complete"
    reason = None
    approval_id = None
    model = "qwen-test"
    tool_calls = 2
    metrics = {"total_ms": 12}


class _Row(dict):
    """sqlite3.Row look-alike supporting item access."""


class _FakeDb:
    def __init__(self) -> None:
        self.updates: list[tuple[str, tuple]] = []
        self.messages: list[_Row] = [
            _Row(role="user", content="hello", created_at="2026-09-01T10:00:00"),
            _Row(role="assistant", content="hi there", created_at="2026-09-01T10:00:05"),
        ]

    def execute(self, sql: str, params: tuple = ()):
        if sql.startswith("UPDATE"):
            self.updates.append((sql, params))
            return self
        if sql.startswith("SELECT role"):
            db = self

            class _Cursor:
                def fetchall(self_inner):
                    return list(reversed(db.messages))

            return _Cursor()
        raise AssertionError(f"unexpected sql {sql}")


class _FakeMemory:
    def __init__(self, _path):
        self.closed = False
        self.conversation = 0
        self.decisions = []
        self.db = _FakeDb()
        self.deleted = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.closed = True

    def new_conversation(self, _title):
        self.conversation += 1
        return self.conversation

    def control_state(self):
        return {"state": "running"}

    def activity_count_since(self, _category, _since):
        return 0

    def list_approvals(self, limit=100):
        del limit
        return []

    def decide_approval(self, approval_id, approve, ttl_hours=24):
        self.decisions.append((approval_id, approve, ttl_hours))
        return True

    def list_conversations(self, limit=50):
        del limit
        return [
            {"id": 1, "title": "New chat", "created_at": "2026-09-02T09:00:00", "message_count": 0, "project_name": "Default workspace"},
            {"id": 42, "title": "Older chat", "created_at": "2026-08-30T09:00:00", "message_count": 2, "project_name": "Default workspace"},
            {"id": 77, "title": "internal", "created_at": "2026-08-30T09:00:00", "message_count": 9, "project_name": "Default workspace"},
        ]

    def is_screen_companion_conversation(self, conversation_id):
        return conversation_id == 77

    def conversation_exists(self, conversation_id):
        return conversation_id in {1, 42}

    def delete_conversation(self, conversation_id):
        self.deleted.append(conversation_id)
        return {"id": conversation_id, "project_id": 1}


class _FakeAgent:
    calls = []

    def __init__(self, _config, _memory, on_event):
        self.on_event = on_event

    def run(self, prompt, **kwargs):
        type(self).calls.append((prompt, kwargs))
        self.on_event("model - qwen-test - quick task")
        stream = kwargs.get("stream_callback")
        if stream:
            stream("Desk")
            stream("top response")
        return _Result("Desktop response")


def _config(temporary: str) -> SimpleNamespace:
    return SimpleNamespace(
        data_dir=Path(temporary),
        fast_model="qwen-test",
        reasoning_model="gemma-test",
        coding_model="gemma-test",
        deep_model="qwen-deep",
        proactive_max_task_seconds=1800,
        daily_tool_limit=500,
        approval_ttl_hours=24,
    )


def _drain(session: JarvisSession, until_kind: str, timeout: float = 3.0) -> list:
    received = []
    while not any(event.kind == until_kind for event in received):
        try:
            received.append(session.events.get(timeout=timeout))
        except queue.Empty as exc:
            raise AssertionError(f"session never emitted {until_kind}: {[e.kind for e in received]}") from exc
    return received


class DesktopSessionTests(unittest.TestCase):
    def test_ready_lists_chats_and_hides_internal_conversations(self):
        with tempfile.TemporaryDirectory() as temporary:
            with patch("jarvis.ui.Memory", _FakeMemory), patch("jarvis.ui.Agent", _FakeAgent):
                session = JarvisSession(_config(temporary))
                session.start()
                ready = session.events.get(timeout=2)
                self.assertEqual(ready.kind, "ready")
                self.assertIsNone(ready.payload["provider_error"])
                ids = [chat["id"] for chat in ready.payload["chats"]]
                self.assertEqual(ids, [1, 42])
                session.shutdown()
                session.join(timeout=2)

    def test_send_streams_deltas_auto_titles_and_reports_timing(self):
        with tempfile.TemporaryDirectory() as temporary:
            _FakeAgent.calls.clear()
            with patch("jarvis.ui.Memory", _FakeMemory), patch("jarvis.ui.Agent", _FakeAgent):
                session = JarvisSession(_config(temporary))
                session.start()
                _drain(session, "ready")
                session.submit("Write me a haiku about servers", "Coding")
                received = _drain(session, "assistant")
                kinds = [event.kind for event in received]
                self.assertIn("delta", kinds)
                self.assertLess(kinds.index("busy"), kinds.index("assistant"))
                deltas = "".join(event.payload["text"] for event in received if event.kind == "delta")
                self.assertEqual(deltas, "Desktop response")
                answer = next(event for event in received if event.kind == "assistant")
                self.assertEqual(answer.payload["content"], "Desktop response")
                self.assertEqual(answer.payload["model"], "qwen-test")
                self.assertEqual(answer.payload["tool_calls"], 2)
                self.assertIsInstance(answer.payload["elapsed"], float)
                self.assertEqual(_FakeAgent.calls[0][1]["model_override"], "coding")
                self.assertTrue(callable(_FakeAgent.calls[0][1]["stream_callback"]))
                self.assertNotIn("attachments", _FakeAgent.calls[0][1])
                # The first prompt renames the fresh conversation, ChatGPT-style.
                self.assertTrue(any(event.kind == "chats" for event in received))
                session.shutdown()
                session.join(timeout=2)

    def test_load_rename_and_delete_commands_round_trip(self):
        with tempfile.TemporaryDirectory() as temporary:
            with patch("jarvis.ui.Memory", _FakeMemory), patch("jarvis.ui.Agent", _FakeAgent):
                session = JarvisSession(_config(temporary))
                session.start()
                _drain(session, "ready")
                session.load_chat(42)
                loaded = next(event for event in _drain(session, "chat_loaded") if event.kind == "chat_loaded")
                self.assertEqual(loaded.payload["title"], "Older chat")
                self.assertEqual([row["role"] for row in loaded.payload["messages"]], ["user", "assistant"])
                self.assertGreater(loaded.payload["messages"][0]["created_at"], 0)
                session.rename_chat(42, "Renamed  chat")
                renamed = next(event for event in _drain(session, "chat_renamed") if event.kind == "chat_renamed")
                self.assertEqual(renamed.payload["title"], "Renamed  chat")
                session.load_chat(999)
                error = next(event for event in _drain(session, "error") if event.kind == "error")
                self.assertIn("no longer exists", error.payload["message"])
                session.delete_chat(42)
                deleted = next(event for event in _drain(session, "chat_deleted") if event.kind == "chat_deleted")
                self.assertTrue(deleted.payload["deleted"])
                session.shutdown()
                session.join(timeout=2)

    def test_provider_failure_at_startup_is_recoverable_not_fatal(self):
        attempts = {"count": 0}

        class _FlakyAgent(_FakeAgent):
            def __init__(self, config, memory, on_event):
                attempts["count"] += 1
                if attempts["count"] == 1:
                    raise RuntimeError("provider down")
                super().__init__(config, memory, on_event)

        with tempfile.TemporaryDirectory() as temporary:
            with patch("jarvis.ui.Memory", _FakeMemory), patch("jarvis.ui.Agent", _FlakyAgent):
                session = JarvisSession(_config(temporary))
                session.start()
                ready = session.events.get(timeout=2)
                self.assertEqual(ready.kind, "ready")
                self.assertIn("provider down", ready.payload["provider_error"])
                session.submit("hello", "Auto")
                received = _drain(session, "assistant")
                provider = next(event for event in received if event.kind == "provider")
                self.assertIsNone(provider.payload["error"])
                answer = next(event for event in received if event.kind == "assistant")
                self.assertEqual(answer.payload["content"], "Desktop response")
                session.shutdown()
                session.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
