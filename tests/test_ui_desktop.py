"""Tests for the JARVIS Desktop rendering helpers and session commands.

These never create a Tk window: the markdown parser, inline splitter, title
derivation and date grouping are pure functions, and the worker-thread
session is exercised with fake Memory/Agent objects.
"""

from __future__ import annotations

import json
import queue
import tempfile

import threading
import time
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from jarvis import council
from jarvis.memory import Memory
from jarvis.ui import (
    CouncilSession,
    DEFAULT_CHAT_TITLE,
    JarvisDesktop,
    JarvisSession,
    Message,
    ChatMeta,
    UiJobs,
    _branch_rows,
    _task_rows,
    _worker_alive,
    highlight_runs,
    looks_like_path,
    probe_paths,
    read_attachments,
    settings_window_height,
    split_fact_seed,
    write_text_file,
    bounded_timeline_record,
    load_timelines,
    save_timeline,
    tool_headline,
    tool_paths,
    tool_result_summary,
    _search_messages,
    _workspace_files,
    chat_group_label,
    chat_title_from_prompt,
    classify_attachment,
    encode_png,
    format_elapsed,
    inline_runs,
    parse_markdown,
    read_text_attachment,
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
        self.assertTrue(lines[1].startswith("\u2500"))
        self.assertLessEqual(max(len(line) for line in lines), 48 + 2 + 48)


class TitleAndTimeTests(unittest.TestCase):
    def test_chat_title_is_short_clean_and_never_empty(self):
        self.assertEqual(chat_title_from_prompt("   "), DEFAULT_CHAT_TITLE)
        self.assertEqual(chat_title_from_prompt("# Fix the login bug\nplease"), "Fix the login bug please")
        long_title = chat_title_from_prompt("word " * 40)
        self.assertLessEqual(len(long_title), 57)
        self.assertTrue(long_title.endswith("\u2026"))
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
        # Titles by id and the full id set: H-1 reads these directly, never the
        # bounded sidebar list. 900 is an old conversation outside that list.
        self.titles: dict[int, str] = {1: "New chat", 42: "Older chat", 900: "Ancient plan"}
        self.all_ids: list[int] = [1, 42, 77, 900]
        # The newest spine claim event and the claim row it points at: the
        # receipt's fact must come from here, not from the operator's prompt.
        self.spine_event = _Row(id=501, kind="claim.created", subject_id=7, outcome="applied", created_at="2026-09-03T10:00:00")
        self.claim = _Row(id=7, subject="lab server", predicate="hostname", value="atlas.internal", status="active")

    def execute(self, sql: str, params: tuple = ()):
        if sql.startswith("UPDATE"):
            self.updates.append((sql, params))
            return self
        db = self
        if sql.startswith("SELECT role"):
            class _Cursor:
                def fetchall(self_inner):
                    return list(reversed(db.messages))

            return _Cursor()
        if sql.lstrip().startswith("SELECT m.id"):
            needle = str(params[0]).strip("%").lower()

            class _Search:
                def fetchall(self_inner):
                    rows = []
                    for index, row in enumerate(db.messages):
                        if needle in row["content"].lower():
                            rows.append(_Row(id=index + 1, conversation_id=42, role=row["role"], content=row["content"], created_at=row["created_at"], title="Older chat"))
                    return rows

            return _Search()

        class _One:
            def __init__(self_inner, row):
                self_inner.row = row

            def fetchone(self_inner):
                return self_inner.row

            def fetchall(self_inner):
                return [self_inner.row] if self_inner.row is not None else []

        compact = " ".join(sql.split())
        if compact.startswith("SELECT title FROM conversations"):
            return _One(_Row(title=self.titles.get(int(params[0]))) if int(params[0]) in self.titles else None)
        if compact.startswith("SELECT id FROM conversations"):
            class _Ids:
                def fetchall(self_inner):
                    return [_Row(id=value) for value in db.all_ids]

            return _Ids()
        if compact.startswith("SELECT id, kind, subject_id, outcome, created_at FROM memory_spine_events"):
            return _One(self.spine_event)
        if compact.startswith("SELECT subject, predicate, value, status FROM memory_claims"):
            return _One(self.claim if self.claim is not None and int(params[0]) == self.claim["id"] else None)
        raise AssertionError(f"unexpected sql {sql}")


class _FakeMemory:
    def __init__(self, _path):
        self.closed = False
        self.conversation = 0
        self.decisions = []
        self.db = _FakeDb()
        _FAKE_DBS.append(self.db)
        self.deleted = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.closed = True

    def new_conversation(self, _title, *, project_id=None):
        self.conversation += 1
        self.last_project = project_id
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

    def decide_approval_for_session(self, approval_id, *, ttl_hours=24):
        self.decisions.append((approval_id, "session", ttl_hours))
        return 501 if approval_id == 12 else None

    def decide_approval_always(self, approval_id):
        self.decisions.append((approval_id, "always"))
        return 502 if approval_id == 12 else None

    def list_persistent_approvals(self, limit=100, *, include_revoked=True):
        return [{"id": 502, "action": "access_private_files", "resource": "computer_read_file C:/x.txt", "reason": "r", "grant_kind": "always", "scope": None, "created_at": "2026-09-03T00:00:00", "expires_at": None}]

    def revoke_persistent_approval(self, grant_id):
        self.revoked = getattr(self, "revoked", [])
        self.revoked.append(grant_id)
        return grant_id == 502

    def add_message(self, conversation_id, role, content):
        self.added = getattr(self, "added", [])
        self.added.append((conversation_id, role, content))
        return len(self.added)

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
        return conversation_id in {1, 42, 900}

    def pending_fact_proposal(self, conversation_id):
        if getattr(self, "proposal_for", None) == conversation_id:
            return {"id": 3, "assistant_message_id": 12, "project_id": 1, "command": 'Remember this project fact: {"subject":"deploy target","predicate":"region","value":"eu-west-1"}', "assisted": True, "reply_asked_question": False, "previous_user_text": "we deploy to eu-west-1", "command_sha256": "0" * 64, "spine_event_id": None}
        return None

    def delete_conversation(self, conversation_id):
        self.deleted.append(conversation_id)
        return {"id": conversation_id, "project_id": 1}

    def list_projects(self):
        return [
            {"id": 1, "name": "Default workspace", "relative_path": ".", "enabled": 1, "conversation_count": 2, "task_count": 0},
            {"id": 3, "name": "Lab", "relative_path": "@projects/lab", "enabled": 1, "conversation_count": 0, "task_count": 1},
            {"id": 4, "name": "Old", "relative_path": "@projects/old", "enabled": 0, "conversation_count": 0, "task_count": 0},
        ]

    def get_project(self, project_id):
        return {"id": int(project_id or 1), "name": "Default workspace", "relative_path": ".", "enabled": 1}

    def conversation_project(self, conversation_id):
        return {"id": 3 if conversation_id == 42 else 1}

    def list_tasks(self, limit=20):
        return [{"id": 9, "status": "queued", "prompt": "Summarize research", "updated_at": "2026-09-02T01:00:00", "requested_model": "reasoning", "project_id": 1}]

    def add_task(self, prompt, *, project_id=None, requested_model=None, **_kwargs):
        self.tasks = getattr(self, "tasks", [])
        self.tasks.append((prompt, project_id, requested_model))
        return 77

    def list_memories(self, limit=20):
        return [{"created_at": "2026-09-02T00:00:00", "kind": "fact", "content": "The lab server is atlas.local"}]


class _FakeAgent:
    calls = []

    def __init__(self, _config, _memory, on_event):
        self.on_event = on_event

    def run(self, prompt, **kwargs):
        type(self).calls.append((prompt, kwargs))
        self.on_event("model - qwen-test - quick task")
        if prompt.lower().startswith("remember this project fact"):
            self.on_event("governed project memory - created")
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
        workspace=Path(temporary) / "workspace",
    )


_FAKE_DBS: list = []


def session_db_updates(session: JarvisSession) -> list:
    """UPDATE statements the fake store received during this session."""
    return list(_FAKE_DBS[-1].updates) if _FAKE_DBS else []

def _council_config(temporary: str) -> SimpleNamespace:
    return SimpleNamespace(
        data_dir=Path(temporary),
        cloud_enabled=False,
        openai_api_enabled=False,
        codex_cli_enabled=False,
        reasoning_model="qwen-test",
        fast_model="qwen-test",
        model="qwen-test",
        model_call_limit_per_request=48,
        prompt_token_limit_per_request=400_000,
        completion_token_limit_per_request=40_000,
        proactive_max_task_seconds=1800,
        daily_tool_limit=500,
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
                self.assertEqual([chat["project_id"] for chat in ready.payload["chats"]], [1, 1])
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

    def test_desktop_stream_withholds_a_secret_split_between_fragments(self):
        secret = "sk-" + "EXAMPLEONLY123456"

        class SplitSecretAgent(_FakeAgent):
            def run(self, _prompt, **kwargs):
                stream = kwargs["stream_callback"]
                stream("Result api_")
                stream("key=" + secret)
                return _Result("Result api_key=" + secret)

        with tempfile.TemporaryDirectory() as temporary:
            with patch("jarvis.ui.Memory", _FakeMemory), patch(
                "jarvis.ui.Agent", SplitSecretAgent
            ):
                session = JarvisSession(_config(temporary))
                session.start()
                _drain(session, "ready")
                session.submit("show a safe result", "Fast")
                received = _drain(session, "assistant")
                rendered = "".join(
                    event.payload["text"]
                    for event in received
                    if event.kind == "delta"
                )
                self.assertNotIn(secret, rendered)
                self.assertEqual(rendered, "Result [REDACTED]")
                session.shutdown()
                session.join(timeout=2)

    def test_load_rename_and_delete_commands_round_trip(self):
        with tempfile.TemporaryDirectory() as temporary:
            with patch("jarvis.ui.Memory", _FakeMemory), patch("jarvis.ui.Agent", _FakeAgent):
                session = JarvisSession(_config(temporary))
                session.start()
                ready = _drain(session, "chat_ids")
                ids = next(event.payload for event in ready if event.kind == "chat_ids")
                self.assertEqual(ids, [1, 42, 77, 900])  # every id the store holds, not the sidebar's 120
                session.load_chat(42)
                loaded = next(event for event in _drain(session, "chat_loaded") if event.kind == "chat_loaded")
                self.assertEqual(loaded.payload["title"], "Older chat")
                # A conversation outside the sidebar list keeps its stored title and is not retitled.
                session.load_chat(900)
                ancient = next(event for event in _drain(session, "chat_loaded") if event.kind == "chat_loaded")
                self.assertEqual(ancient.payload["title"], "Ancient plan")
                _FakeAgent.calls.clear()
                session.submit("a new line", "Auto")
                _drain(session, "assistant")
                self.assertFalse(any("UPDATE conversations" in sql and params[1] == 900 for sql, params in session_db_updates(session)))
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


class PracticalHelpersTests(unittest.TestCase):
    def test_attachment_classification_and_bounded_text_blocks(self):
        self.assertEqual(classify_attachment("notes.MD"), "text")
        self.assertEqual(classify_attachment("shot.PNG"), "image")
        self.assertEqual(classify_attachment("archive.zip"), "unsupported")
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "big.py"
            path.write_text("x = 1\n" * 60_000, encoding="utf-8")
            name, block = read_text_attachment(str(path))
            self.assertEqual(name, "big.py")
            self.assertTrue(block.startswith("File `big.py` (py):\n```py\n"))
            self.assertIn("truncated", block)
            self.assertTrue(block.rstrip().endswith("```"))
            self.assertLess(len(block), 220_000)

    def test_workspace_files_skip_hidden_and_respect_since(self):
        import os
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "code").mkdir()
            (root / ".git").mkdir()
            (root / ".git" / "HEAD").write_text("ref", encoding="utf-8")
            (root / "code" / "a.py").write_text("a", encoding="utf-8")
            (root / "old.txt").write_text("old", encoding="utf-8")
            os.utime(root / "old.txt", (1_000_000, 1_000_000))
            found = _workspace_files(root, since_epoch=2_000_000)
            self.assertEqual([item["relative"] for item in found], ["code/a.py"])
            self.assertTrue(found[0]["path"].endswith("a.py"))

    def test_message_search_uses_like_and_snippets(self):
        memory = _FakeMemory("ignored")
        self.assertEqual(_search_messages(memory, "h"), [])
        rows = _search_messages(memory, "HELLO")
        self.assertEqual(len(rows), 1)
        self.assertEqual(_search_messages(memory, "C:\\temp\\"), [])
        self.assertEqual(rows[0]["conversation_id"], 42)
        self.assertEqual(rows[0]["title"], "Older chat")
        self.assertIn("hello", rows[0]["snippet"])

    def test_png_encoder_produces_valid_chunks(self):
        import struct
        import zlib
        rows = [bytes([255, 0, 0, 0, 255, 0]), bytes([0, 0, 255, 255, 255, 255])]
        data = encode_png(2, 2, rows, alpha=False)
        self.assertTrue(data.startswith(b"\x89PNG\r\n\x1a\n"))
        length = struct.unpack(">I", data[8:12])[0]
        self.assertEqual(data[12:16], b"IHDR")
        width, height, depth, color = struct.unpack(">IIBB", data[16:26])
        self.assertEqual((width, height, depth, color, length), (2, 2, 8, 2, 13))
        idat_start = data.index(b"IDAT") - 4
        idat_length = struct.unpack(">I", data[idat_start:idat_start + 4])[0]
        payload = data[idat_start + 8:idat_start + 8 + idat_length]
        self.assertEqual(zlib.decompress(payload), b"\x00" + rows[0] + b"\x00" + rows[1])
        self.assertTrue(data.endswith(b"IEND\xaeB`\x82"))


class PracticalSessionTests(unittest.TestCase):
    def test_ready_carries_projects_and_context_answers_with_bounded_rows(self):
        with tempfile.TemporaryDirectory() as temporary:
            (Path(temporary) / "workspace").mkdir()
            with patch("jarvis.ui.Memory", _FakeMemory), patch("jarvis.ui.Agent", _FakeAgent):
                session = JarvisSession(_config(temporary))
                session.start()
                ready = _drain(session, "ready")[-1]
                self.assertEqual([project["id"] for project in ready.payload["projects"]], [1, 3])
                self.assertEqual(ready.payload["project_id"], 1)
                self.assertTrue(ready.payload["workspace"].endswith("workspace"))
                session.request_context(0.0)
                context = next(event for event in _drain(session, "context") if event.kind == "context")
                self.assertEqual(context.payload["project_name"], "Default workspace")
                self.assertEqual(context.payload["tasks"][0]["id"], 9)
                self.assertEqual(context.payload["memories"][0]["kind"], "fact")
                self.assertEqual(context.payload["pending_approvals"], [])
                session.shutdown()
                session.join(timeout=2)

    def test_project_bound_chats_search_tasks_and_receipts(self):
        with tempfile.TemporaryDirectory() as temporary:
            (Path(temporary) / "workspace").mkdir()
            _FakeAgent.calls.clear()
            with patch("jarvis.ui.Memory", _FakeMemory), patch("jarvis.ui.Agent", _FakeAgent):
                session = JarvisSession(_config(temporary))
                session.start()
                _drain(session, "ready")
                session.new_chat(3)
                fresh = next(event for event in _drain(session, "new_chat") if event.kind == "new_chat")
                self.assertEqual(fresh.payload["project_id"], 1)  # the fake maps unknown ids to 1
                session.search_messages("hello")
                found = next(event for event in _drain(session, "search_results") if event.kind == "search_results")
                self.assertEqual(found.payload["query"], "hello")
                self.assertEqual(found.payload["results"][0]["conversation_id"], 42)
                session.queue_task("Summarize the workspace", "Reasoning")
                queued = next(event for event in _drain(session, "task_queued") if event.kind == "task_queued")
                self.assertEqual(queued.payload["task_id"], 77)
                session.approval_detail(4)
                detail = next(event for event in _drain(session, "approval_detail") if event.kind == "approval_detail")
                self.assertEqual(detail.payload["approval"], {"missing": True})
                session.submit('Remember this project fact: {"subject":"lab server","predicate":"hostname","value":"atlas.local"}', "Auto")
                received = _drain(session, "memory_receipt")
                receipt = next(event for event in received if event.kind == "memory_receipt")
                self.assertEqual(receipt.payload["action"], "created")
                # The fact is the store's claim row (value atlas.internal), not the prompt's atlas.local.
                self.assertEqual(receipt.payload["fact"]["subject"], "lab server")
                self.assertEqual(receipt.payload["fact"]["value"], "atlas.internal")
                self.assertEqual(receipt.payload["claim_id"], 7)
                self.assertEqual(receipt.payload["event_kind"], "claim.created")
                kinds = [event.kind for event in received]
                self.assertLess(kinds.index("assistant"), kinds.index("memory_receipt"))
                self.assertNotIn("fact_proposal", kinds)
                session.shutdown()
                session.join(timeout=2)

    def test_pending_fact_proposal_from_the_store_becomes_a_fact_proposal_event(self):
        with tempfile.TemporaryDirectory() as temporary:
            (Path(temporary) / "workspace").mkdir()

            class _ProposingMemory(_FakeMemory):
                def __init__(self, path):
                    super().__init__(path)
                    self.proposal_for = 1  # the conversation the session opens first

            with patch("jarvis.ui.Memory", _ProposingMemory), patch("jarvis.ui.Agent", _FakeAgent):
                session = JarvisSession(_config(temporary))
                session.start()
                _drain(session, "ready")
                session.submit("we deploy to eu-west-1", "Auto")
                proposal = next(e.payload for e in _drain(session, "fact_proposal") if e.kind == "fact_proposal")
                self.assertEqual(proposal["conversation_id"], 1)
                self.assertEqual(proposal["proposal_id"], 3)
                self.assertTrue(proposal["assisted"])
                self.assertEqual(proposal["fact"], {"subject": "deploy target", "predicate": "region", "value": "eu-west-1"})
                session.shutdown()
                session.join(timeout=2)


class _ToolBox:
    """Stand-in for jarvis.tools.ToolBox: execute() is wrapped per run."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def execute(self, name, arguments):
        self.calls.append((name, dict(arguments)))
        if name == "read_file":
            return '{"ok": true, "content": "secret=[REDACTED]\\nmodel: qwen"}'
        if name == "explode":
            return '{"ok": false, "error": "boom"}'
        return '{"ok": true, "result": {"approval_required": true, "approval_id": 12}}'


class _ToolAgent(_FakeAgent):
    """Agent that calls tools through its toolbox during run()."""

    def __init__(self, _config, _memory, on_event):
        super().__init__(_config, _memory, on_event)
        self.toolbox = _ToolBox()
        type(self).instances = getattr(type(self), "instances", [])
        type(self).instances.append(self)

    def run(self, prompt, **kwargs):
        self.on_event("model - qwen-test - quick task")
        self.on_event("tool - read_file")
        self.toolbox.execute("read_file", {"path": "C:/work/config.yaml", "token": "abc"})
        self.on_event("tool - explode")
        self.toolbox.execute("explode", {})
        self.toolbox.execute("computer_read_file", {"path": "C:/private/x.txt"})
        result = _Result("Tool response")
        return result


class ToolTraceTests(unittest.TestCase):
    def test_result_summary_classifies_ok_error_and_approval(self):
        ok = tool_result_summary('{"ok": true, "content": "hello"}')
        self.assertEqual(ok["status"], "ok")
        self.assertEqual(ok["preview"], "hello")
        error = tool_result_summary('{"ok": false, "error": "boom"}')
        self.assertEqual(error["status"], "error")
        self.assertIn("boom", error["preview"])
        approval = tool_result_summary('{"ok": true, "result": {"approval_required": true, "approval_id": 7}}')
        self.assertEqual(approval["status"], "approval")
        self.assertEqual(approval["approval_id"], 7)
        plain = tool_result_summary("not json")
        self.assertEqual(plain["status"], "ok")
        self.assertEqual(plain["preview"], "not json")
        big = tool_result_summary('{"ok": true, "content": "' + "x" * 5000 + '"}')
        self.assertTrue(big["truncated"])
        self.assertLess(len(big["preview"]), 800)

    def test_headline_and_paths_prefer_paths_commands_and_queries(self):
        self.assertEqual(tool_headline("read_file", {"max_bytes": 5, "path": "C:/a/b.txt"}), "C:/a/b.txt")
        self.assertEqual(tool_headline("run", {"command": "git status"}), "git status")
        self.assertEqual(tool_headline("x", {"count": 3}), "3")
        self.assertEqual(tool_headline("x", None), "")
        paths = tool_paths({"path": "C:/a/b.txt", "content": "hello world"}, {"written": ["C:/a/c.txt", "C:/a/b.txt"]})
        self.assertEqual(paths, ["C:/a/b.txt", "C:/a/c.txt"])
        self.assertEqual(tool_paths({"path": "x" * 400}, None), [])

    def test_timeline_record_is_bounded_and_round_trips(self):
        record = {
            "steps": [f"step {index}" for index in range(100)],
            "tools": [{"seq": index, "name": "t", "arguments": {"path": "p" * 900}, "status": "ok", "ms": 3, "preview": "y" * 2000, "paths": ["p"], "extra": "dropped"} for index in range(120)],
            "metrics": {"prompt_tokens": 10, "trace_id": "abc", "nested": {"a": 1}},
            "status": "complete", "reason": "", "model": "qwen", "elapsed": 1.5, "tool_calls": 3, "retryable": False,
            "receipt": {"action": "created", "fact": {"subject": "s"}},
            "versions": ["one", "two", "three", "four", "five", "six"],
        }
        clean = bounded_timeline_record(record)
        self.assertEqual(len(clean["steps"]), 60)
        self.assertEqual(len(clean["tools"]), 80)
        self.assertNotIn("extra", clean["tools"][0])
        self.assertLessEqual(len(clean["tools"][0]["arguments"]["path"]), 430)
        self.assertLessEqual(len(clean["tools"][0]["preview"]), 730)
        self.assertEqual(clean["metrics"]["prompt_tokens"], 10)
        self.assertEqual(clean["versions"], ["three", "four", "five", "six"])
        self.assertEqual(bounded_timeline_record("nope"), {})
        with tempfile.TemporaryDirectory() as temporary:
            self.assertTrue(save_timeline(temporary, 5, 11, record))
            self.assertTrue(save_timeline(temporary, 5, 12, {"steps": ["a"]}))
            loaded = load_timelines(temporary, 5)
            self.assertEqual(sorted(loaded), ["11", "12"])
            self.assertEqual(loaded["11"]["model"], "qwen")
            self.assertEqual(load_timelines(temporary, 6), {})
            message = Message("assistant", "body")
            message.restore_timeline(loaded["11"])
            self.assertEqual(len(message.tools), 80)
            self.assertEqual(message.version_index, 3)
            self.assertEqual(message.receipt["action"], "created")

    def test_session_traces_tool_calls_with_arguments_results_and_persists(self):
        with tempfile.TemporaryDirectory() as temporary:
            (Path(temporary) / "workspace").mkdir()
            with patch("jarvis.ui.Memory", _FakeMemory), patch("jarvis.ui.Agent", _ToolAgent):
                session = JarvisSession(_config(temporary))
                session.start()
                _drain(session, "ready")
                session.submit("read the config", "Auto")
                received = _drain(session, "assistant")
                steps = [event.payload for event in received if event.kind == "step"]
                activities = [event.payload for event in received if event.kind == "activity"]
                self.assertFalse(any(str(item).startswith("tool - ") for item in activities), activities)
                self.assertEqual([step["status"] for step in steps], ["running", "ok", "running", "error", "running", "approval"])
                first = steps[1]
                self.assertEqual(first["name"], "read_file")
                self.assertEqual(first["headline"], "C:/work/config.yaml")
                self.assertEqual(first["arguments"]["path"], "C:/work/config.yaml")
                self.assertIn("model: qwen", first["preview"])
                self.assertEqual(first["paths"], ["C:/work/config.yaml"])
                self.assertEqual(steps[5]["approval_id"], 12)
                assistant = next(event.payload for event in received if event.kind == "assistant")
                self.assertEqual(len(assistant["tools"]), 3)
                self.assertEqual(assistant["tools"][1]["status"], "error")
                self.assertFalse(assistant["retryable"])
                self.assertIsNone(assistant["message_id"])  # the fake db has no id column
                agent = _ToolAgent.instances[-1]
                self.assertNotIn("execute", agent.toolbox.__dict__)  # wrapper removed after the run
                session.save_timeline(1, 5, {"steps": ["a"], "tools": assistant["tools"]})
                deadline = time.monotonic() + 3.0
                while "5" not in load_timelines(temporary, 1) and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertEqual(len(load_timelines(temporary, 1)["5"]["tools"]), 3)
                session.shutdown()
                session.join(timeout=2)


class ApprovalScopeSessionTests(unittest.TestCase):
    def test_scoped_decisions_grants_and_revoke_round_trip(self):
        with tempfile.TemporaryDirectory() as temporary:
            (Path(temporary) / "workspace").mkdir()
            with patch("jarvis.ui.Memory", _FakeMemory), patch("jarvis.ui.Agent", _FakeAgent):
                session = JarvisSession(_config(temporary))
                session.start()
                _drain(session, "ready")
                session.decide_approval(12, True, "session")
                events = _drain(session, "grants")
                decided = next(e.payload for e in events if e.kind == "approval_decided")
                self.assertTrue(decided["changed"])
                self.assertEqual(decided["scope"], "session")
                self.assertEqual(decided["grant_id"], 501)
                session.decide_approval(13, True, "always")
                decided = next(e.payload for e in _drain(session, "approval_decided") if e.kind == "approval_decided")
                self.assertFalse(decided["changed"])
                self.assertIn("not eligible", decided["note"])
                session.decide_approval(14, False, "deny")
                decided = next(e.payload for e in _drain(session, "approval_decided") if e.kind == "approval_decided")
                self.assertTrue(decided["changed"])
                self.assertEqual(decided["scope"], "deny")
                session.request_grants()
                grants = next(e.payload for e in _drain(session, "grants") if e.kind == "grants")
                self.assertEqual(grants[0]["id"], 502)
                self.assertEqual(grants[0]["kind"], "always")
                session.revoke_grant(502)
                revoked = next(e.payload for e in _drain(session, "grant_revoked") if e.kind == "grant_revoked")
                self.assertTrue(revoked["revoked"])
                session.branch_chat(42, None, 1, "Branch \u00b7 Older chat")
                branched = next(e.payload for e in _drain(session, "chat_branched") if e.kind == "chat_branched")
                self.assertEqual(branched["source"], 42)
                self.assertEqual(branched["copied"], 1)
                session.shutdown()
                session.join(timeout=2)

    def test_turn_payloads_carry_the_workspace_diff_record(self):
        class _WritingAgent(_FakeAgent):
            def run(self, prompt, **kwargs):
                workspace = Path(self.workspace)
                (workspace / "notes.md").write_text("# changed\nline\n", encoding="utf-8")
                (workspace / "new.py").write_text("print('new')\n", encoding="utf-8")
                return _Result("Edited two files")

        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary) / "workspace"
            workspace.mkdir()
            (workspace / "notes.md").write_text("# before\n", encoding="utf-8")
            _WritingAgent.workspace = str(workspace)
            with patch("jarvis.ui.Memory", _FakeMemory), patch("jarvis.ui.Agent", _WritingAgent):
                session = JarvisSession(_config(temporary))
                session.start()
                _drain(session, "ready")
                session.submit("edit the notes", "Auto")
                answer = next(e.payload for e in _drain(session, "assistant") if e.kind == "assistant")
                diff = answer["diff"]
                self.assertIsInstance(diff, dict)
                self.assertEqual(diff["files_changed"], 2)
                self.assertEqual(sorted(item["relative"] for item in diff["files"]), ["new.py", "notes.md"])
                self.assertEqual(sorted(item["kind"] for item in diff["files"]), ["added", "modified"])
                clean = bounded_timeline_record({"diff": diff, "steps": []})
                self.assertEqual(clean["diff"]["files_changed"], 2)
                self.assertEqual(bounded_timeline_record({"diff": "unavailable: workspace too large"})["diff"], "unavailable: workspace too large")
                message = Message("assistant", "Edited two files")
                message.restore_timeline(clean)
                self.assertEqual(message.diff["files_changed"], 2)
                session.shutdown()
                session.join(timeout=2)

    def test_file_index_is_built_on_the_worker_after_ready_and_on_request(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary) / "workspace"
            (workspace / "code").mkdir(parents=True)
            (workspace / "code" / "main.py").write_text("print(1)", encoding="utf-8")
            (workspace / "notes.md").write_text("# n", encoding="utf-8")
            (workspace / ".git").mkdir()
            (workspace / ".git" / "HEAD").write_text("ref", encoding="utf-8")
            with patch("jarvis.ui.Memory", _FakeMemory), patch("jarvis.ui.Agent", _FakeAgent):
                session = JarvisSession(_config(temporary))
                session.start()
                first = next(e.payload for e in _drain(session, "file_index") if e.kind == "file_index")
                self.assertIsNone(first["error"])
                self.assertTrue(first["root"].endswith("workspace"))
                index = first["index"]
                self.assertEqual(sorted(entry.path for entry in index.entries), ["code/main.py", "notes.md"])
                self.assertFalse(index.truncated)
                (workspace / "later.txt").write_text("x", encoding="utf-8")
                session.request_file_index()
                again = next(e.payload for e in _drain(session, "file_index") if e.kind == "file_index")
                self.assertIn("later.txt", [entry.path for entry in again["index"].entries])
                self.assertIsNot(again["index"], index)  # a fresh immutable snapshot, never a mutation
                # A reply landing rebuilds the index too.
                session.submit("hello", "Auto")
                events = _drain(session, "file_index")
                kinds = [event.kind for event in events]
                self.assertLess(kinds.index("assistant"), kinds.index("file_index"))
                session.shutdown()
                session.join(timeout=2)

    def test_worker_alive_reads_heartbeat_age(self):
        with tempfile.TemporaryDirectory() as temporary:
            self.assertFalse(_worker_alive(temporary))  # never started: offline, not unknown
            beat = Path(temporary) / "worker.heartbeat"
            beat.write_text(f"{time.time() - 30:.0f} pid=1", encoding="utf-8")
            self.assertTrue(_worker_alive(temporary))
            beat.write_text(f"{time.time() - 400:.0f} pid=1", encoding="utf-8")
            self.assertFalse(_worker_alive(temporary))
            beat.write_text("garbage", encoding="utf-8")
            self.assertIsNone(_worker_alive(temporary))


class ChatMetaTests(unittest.TestCase):
    def test_flags_persist_prune_and_remove(self):
        with tempfile.TemporaryDirectory() as temporary:
            meta = ChatMeta(Path(temporary))
            meta.set(5, pinned=True, unread=True, branched_from=2)
            meta.set(6, archived=True)
            meta.set(6, archived=False)  # false flags are dropped, not stored
            again = ChatMeta(Path(temporary))
            self.assertTrue(again.flag(5, "pinned"))
            self.assertEqual(again.get(5)["branched_from"], 2)
            self.assertEqual(again.get(6), {})
            again.set(5, unread=False)
            self.assertFalse(again.flag(5, "unread"))
            again.set(9, pinned=True, bogus=True)
            self.assertNotIn("bogus", again.get(9))
            again.prune({5})
            self.assertEqual(sorted(again.values), ["5"])
            again.remove(5)
            self.assertEqual(ChatMeta(Path(temporary)).values, {})

    def test_branch_rows_fall_back_to_recent_messages_and_count(self):
        class _NoDb:
            def recent_messages(self, conversation_id, limit=2000):
                return [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}, {"role": "system", "content": "x"}, {"role": "user", "content": "c"}]

        rows = _branch_rows(_NoDb(), 1, None, 3)
        self.assertEqual([row["content"] for row in rows], ["a", "b"])
        self.assertEqual(_branch_rows(_NoDb(), 1, 99, 3), [])  # id known but no db \u2192 nothing copied


class V3BacklogHelperTests(unittest.TestCase):
    """Pure helpers behind the v3 backlog items (settings size, seeds, task rows, search hits)."""

    def test_settings_window_height_caps_at_screen_fraction(self):
        self.assertEqual(settings_window_height(1387, 1080), 864)  # 80 % of 1080
        self.assertEqual(settings_window_height(700, 1440), 700)
        self.assertEqual(settings_window_height(100, 1440), 320)  # never smaller than the minimum
        self.assertEqual(settings_window_height(5000, 900, fraction=0.5), 450)

    def test_split_fact_seed_prefills_subject_and_predicate(self):
        self.assertEqual(split_fact_seed("lab server hostname "), {"subject": "lab server", "predicate": "hostname", "value": ""})
        self.assertEqual(split_fact_seed("deploy region "), {"subject": "deploy", "predicate": "region", "value": ""})
        self.assertEqual(split_fact_seed("atlas.local"), {"subject": "", "predicate": "", "value": "atlas.local"})
        self.assertEqual(split_fact_seed("single "), {"subject": "", "predicate": "", "value": "single"})
        self.assertEqual(split_fact_seed(""), {"subject": "", "predicate": "", "value": ""})
        self.assertEqual(split_fact_seed(None), {"subject": "", "predicate": "", "value": ""})

    def test_task_rows_carry_detail_fields_bounded_and_redacted(self):
        class _TaskMemory:
            def list_tasks(self, limit=30):
                return [
                    {"id": 9, "status": "failed", "prompt": "Summarize research", "updated_at": "2026-09-02T01:00:00", "requested_model": "reasoning", "project_id": 1,
                     "result": "# Done\n" + "x" * 9000, "last_error": "token=sk-proj-" + "A" * 40 + " rejected", "attempt_count": "2", "max_attempts": 3, "awaiting_approval_id": 31},
                    {"id": 10, "status": "queued", "prompt": "later", "attempt_count": None, "max_attempts": None, "awaiting_approval_id": None},
                    "not a row",
                ]

        rows = _task_rows(_TaskMemory())
        self.assertEqual([row["id"] for row in rows], [9, 10])
        first = rows[0]
        self.assertTrue(first["result"].startswith("# Done"))
        self.assertLess(len(first["result"]), 4_100)
        self.assertIn("[display truncated]", first["result"])
        self.assertNotIn("sk-proj-AAAA", first["last_error"])
        self.assertEqual((first["attempt_count"], first["max_attempts"], first["awaiting_approval_id"]), (2, 3, 31))
        self.assertEqual((rows[1]["attempt_count"], rows[1]["max_attempts"], rows[1]["awaiting_approval_id"]), (0, 0, None))
        self.assertEqual(rows[1]["result"], "")

    def test_highlight_runs_marks_hits_case_insensitively_and_bounds_the_excerpt(self):
        runs = highlight_runs("Your Workspace has a workspace folder", "workspace")
        self.assertEqual(runs, [("Your ", False), ("Workspace", True), (" has a ", False), ("workspace", True), (" folder", False)])
        self.assertEqual("".join(text for text, _hit in runs), "Your Workspace has a workspace folder")
        far = highlight_runs("a" * 200 + " needle " + "b" * 200, "needle", limit=60)
        self.assertTrue(any(hit for _text, hit in far))
        self.assertLessEqual(len("".join(text for text, _hit in far)), 62)
        self.assertTrue("".join(text for text, _hit in far).startswith("\u2026"))
        self.assertEqual(highlight_runs("no hit here", "zzz"), [("no hit here", False)])
        self.assertEqual(highlight_runs("", "x"), [])
        self.assertEqual(highlight_runs("plain", ""), [("plain", False)])

    def test_message_search_rows_carry_the_message_id(self):
        memory = _FakeMemory("ignored")
        rows = _search_messages(memory, "hello")
        self.assertEqual(rows[0]["message_id"], 1)


class ReviewRound1Tests(unittest.TestCase):
    """Regression tests for the round-1 review findings (H-2, H-4, M-9)."""

    def test_traced_tool_is_thread_safe_across_a_pool(self):
        import threading
        from concurrent.futures import ThreadPoolExecutor
        from jarvis.ui import JarvisSession as _Session

        session = _Session(_config("ignored"))
        gate = threading.Barrier(4)

        def original(name, arguments):
            gate.wait(timeout=5)  # every call starts at once
            time.sleep(0.01)
            return '{"ok": true, "result": "' + arguments["url"] + '"}'

        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(session._traced_tool, original, "web_fetch", {"url": f"https://example.com/{index}"}, 1) for index in range(4)]
            for future in futures:
                future.result(timeout=5)
        log = session._tool_log
        self.assertEqual(sorted(entry["seq"] for entry in log), [1, 2, 3, 4])
        for entry in log:
            self.assertEqual(entry["arguments"]["url"], entry["preview"])  # a row never shows another call's result
        steps = [event.payload for event in iter(lambda: session.events.get_nowait() if not session.events.empty() else None, None)]
        finished = [step for step in steps if step["status"] == "ok"]
        self.assertEqual(len({step["seq"] for step in finished}), 4)

    def test_tool_paths_and_open_path_reject_urls_uncs_and_schemes(self):
        from jarvis.ui import JarvisDesktop, is_local_path_text
        for bad in ("https://example.com/x.txt", r"\\example-server\share\x.docx", "shell:startup", "ms-screenclip:", "mailto:example-user@example.com", "//host/share"):
            self.assertFalse(is_local_path_text(bad), bad)
        for good in (r"C:\Users\example-user\notes.txt", "/home/example-user/a.py", "notes.md", r"D:\x"):
            self.assertTrue(is_local_path_text(good), good)
        paths = tool_paths({"path": r"C:\work\a.txt", "target": "https://example.com/a.txt"}, {"written": [r"\\example-server\share\b.txt", "shell:x"]})
        self.assertEqual(paths, [r"C:\work\a.txt"])
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "code").mkdir()
            (root / "code" / "a.py").write_text("x", encoding="utf-8")
            relative = tool_paths({"path": "code/a.py", "file": "code/missing.py"}, None, workspace=str(root))
            self.assertEqual(len(relative), 1)
            self.assertTrue(relative[0].endswith("a.py"))
            self.assertTrue(Path(relative[0]).is_absolute())
        for suffix in (".url", ".website", ".appref-ms", ".pif", ".cpl", ".msc", ".inf", ".application", ".docm", ".xlsm", ".pptm", ".psm1", ".vb", ".wsc", ".sct", ".ps1xml", ".exe", ".bat"):
            self.assertIn(suffix, JarvisDesktop.EXECUTABLE_SUFFIXES, suffix)

    def test_timeline_records_and_files_are_bounded_and_written_atomically(self):
        from jarvis.ui import TIMELINE_FILE_BYTES, TIMELINE_RECORD_BYTES, TIMELINE_DIFF_BYTES, shrink_timeline_record
        big = {
            "steps": ["s"] * 10,
            "tools": [{"seq": index, "name": "t", "arguments": {"path": "p" * 300}, "status": "ok", "ms": 1, "preview": "x" * 700, "paths": []} for index in range(80)],
            "diff": {"version": 1, "files_changed": 1, "files": [{"relative": "a", "unified": "u" * 50_000}]},
            "versions": ["v" * 20_000, "w" * 20_000],
        }
        clean = bounded_timeline_record(big)
        # Diff text has its own bounded budget and stays available in the side pane.
        ordinary = {key: value for key, value in clean.items() if key != "diff"}
        self.assertLessEqual(len(json.dumps(ordinary, ensure_ascii=False).encode("utf-8")), TIMELINE_RECORD_BYTES)
        self.assertTrue(all(entry["preview"] == "" and entry["truncated"] for entry in clean["tools"]))
        self.assertEqual(clean["diff"]["files"][0]["unified"], "u" * 50_000)
        self.assertLessEqual(sum(len(entry["unified"].encode("utf-8")) for entry in clean["diff"]["files"]), TIMELINE_DIFF_BYTES)
        small = shrink_timeline_record({"steps": ["a"], "tools": [{"preview": "keep"}]})
        self.assertEqual(small["tools"][0]["preview"], "keep")
        from jarvis.ui import _record_size
        unicode_record = {"text": "\u754c" * 100}
        self.assertEqual(_record_size(unicode_record), len(json.dumps(unicode_record, ensure_ascii=False).encode("utf-8")))
        with tempfile.TemporaryDirectory() as temporary:
            record = {"steps": ["a"], "tools": [{"seq": 1, "name": "t", "arguments": {}, "status": "ok", "ms": 1, "preview": "y" * 600, "paths": []}] * 60}
            for message_id in range(1, 80):
                self.assertTrue(save_timeline(temporary, 9, message_id, record))
            path = Path(temporary) / "desktop_timelines" / "9.json"
            self.assertLessEqual(path.stat().st_size, TIMELINE_FILE_BYTES + 2_000)
            kept = load_timelines(temporary, 9)
            self.assertIn("79", kept)  # newest survives, oldest pruned
            self.assertLess(len(kept), 79)
            self.assertEqual([p.name for p in path.parent.iterdir() if p.suffix == ".tmp"], [])
            self.assertTrue(save_timeline(temporary, 10, 5, {"steps": ["a"], "approval_id": 12}))
            self.assertEqual(load_timelines(temporary, 10)["5"]["approval_id"], 12)


class Pass5HelperTests(unittest.TestCase):
    """Context pill, table cells, message windowing, digests, HTML export, queue keying."""

    def test_context_pill_reports_tokens_and_percent_only_with_a_context_length(self):
        from jarvis.ui import context_pill, format_k
        self.assertEqual(context_pill({}), ("", "ok"))
        self.assertEqual(context_pill({"prompt_tokens": 6200, "completion_tokens": 400}), ("6.2k in \u00b7 0.4k out", "ok"))
        text, level = context_pill({"prompt_tokens": 6200, "completion_tokens": 400, "num_ctx": 16384})
        self.assertEqual(text, "6.2k in \u00b7 0.4k out / 16k (38 %)")
        self.assertEqual(level, "ok")
        self.assertEqual(context_pill({"prompt_tokens": 12500, "completion_tokens": 10, "context_length": 16000})[1], "amber")
        self.assertEqual(context_pill({"prompt_tokens": 15000, "completion_tokens": 10, "context_window": 16000})[1], "red")
        self.assertEqual(context_pill({"prompt_tokens": 15000, "completion_tokens": 10, "token_measurement": "estimated"})[0], "15.0k in \u00b7 10 out")
        self.assertEqual(context_pill({"prompt_tokens": True})[0], "")
        self.assertEqual(format_k(42), "42")
        self.assertEqual(format_k(131072, coarse=True), "131k")

    def test_plain_inline_and_window_start_and_digest(self):
        from jarvis.ui import MESSAGE_WINDOW, message_window_start, plain_inline, render_digest
        self.assertEqual(plain_inline("**bold** and `code` and [Docs](https://example.com/d)"), "bold and code and Docs")
        self.assertEqual(message_window_start(300), 300 - MESSAGE_WINDOW)
        self.assertEqual(message_window_start(10), 0)
        self.assertEqual(message_window_start(300, target_index=100), 95)
        self.assertEqual(message_window_start(300, target_index=2), 0)
        self.assertEqual(message_window_start(300, target_index=290), 260)
        self.assertEqual(render_digest([{"a": 1}, "x"]), render_digest([{"a": 1}, "x"]))
        self.assertNotEqual(render_digest([{"a": 1}]), render_digest([{"a": 2}]))

    def test_html_export_is_self_contained_and_escapes(self):
        from jarvis.ui import render_html_export, render_markdown_export
        reply = Message("assistant", "## Plan\n\nUse `pip` and **bold** <script>alert(1)</script>\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n```python\nprint('x')\n```\n\n- one\n- [x] two\n\n> quoted\n\n[Docs](https://example.com/d) and [bad](javascript:alert(1))", created_at=1_700_000_000)
        reply.receipt = {"action": "created", "fact": {"subject": "lab server", "predicate": "hostname", "value": "atlas.local"}}
        user = Message("user", "hello <b>there</b>", attachments=["notes.md"], created_at=1_700_000_000)
        page = render_html_export("My <chat>", [user, reply], exported_at=datetime(2026, 9, 4, 9, 0))
        self.assertTrue(page.startswith("<!doctype html>"))
        self.assertIn("<title>My &lt;chat&gt;</title>", page)
        self.assertNotIn("<script>", page)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", page)
        self.assertIn("<table>", page)
        self.assertIn("<th>a</th>", page)
        self.assertIn("<pre><code class=\"language-python\">print(&#x27;x&#x27;)</code></pre>", page)
        self.assertIn("<h3>Plan</h3>", page)
        self.assertIn("<li>\u2611 two</li>", page)
        self.assertIn("<blockquote>quoted</blockquote>", page)
        self.assertIn('href="https://example.com/d"', page)
        self.assertNotIn('href="javascript', page)  # never a link; the text stays literal, escaped
        self.assertIn("<code>notes.md</code>", page)
        self.assertIn("Memory created: lab server", page)
        self.assertNotIn("http://", page.split("<main>")[0].replace("http://www.w3.org", ""))  # no external resources in the head
        self.assertNotIn("<link", page)
        markdown = render_markdown_export("My chat", [user, reply], exported_at=datetime(2026, 9, 4, 9, 0))
        self.assertIn("Attachments: `notes.md`", markdown)
        self.assertIn("> Memory created: lab server", markdown)
        self.assertIn("## You", markdown)

    def test_queued_follow_ups_are_keyed_by_conversation(self):
        from jarvis.ui import JarvisDesktop
        stub = SimpleNamespace(conversation_id=7, _queued_by_chat={}, _last_turn_status={})
        JarvisDesktop.queued.fget(stub).append({"text": "for seven"})
        stub.conversation_id = 9
        self.assertEqual(JarvisDesktop.queued.fget(stub), [])
        self.assertIsNone(JarvisDesktop.last_turn_status(stub))
        stub._last_turn_status[9] = "cancelled"
        self.assertEqual(JarvisDesktop.last_turn_status(stub), "cancelled")
        stub.conversation_id = 7
        self.assertEqual([item["text"] for item in JarvisDesktop.queued.fget(stub)], ["for seven"])

    def test_approval_helpers_follow_the_contract_strings(self):
        from jarvis.ui import JarvisDesktop, approval_expiry_text, approval_scope_text, decision_from_row, deny_instruction_text, format_clock
        now = time.time()
        self.assertEqual(approval_expiry_text(datetime.fromtimestamp(now + 23 * 60 + 30).isoformat(), now), "expires in 23 min")
        self.assertEqual(approval_expiry_text(datetime.fromtimestamp(now - 5).isoformat(), now), "expired")
        self.assertEqual(approval_expiry_text("", now), "")
        self.assertEqual(approval_scope_text("conversation:7"), "This chat")
        self.assertEqual(approval_scope_text("task:81"), "Task #81")
        self.assertEqual(deny_instruction_text(12, "access_private_files", "computer_read_file C:\\x", "use the copy"), "Denied approval #12 (computer_read_file C:\\x). Instead: use the copy")
        self.assertEqual(deny_instruction_text(12, "run_command", "", "no"), "Denied approval #12 (run_command). Instead: no")
        self.assertEqual(decision_from_row({"status": "denied", "decided_at": "2026-09-04T14:02:00"}), "Denied \u00b7 " + format_clock(_iso_to_epoch_for_test("2026-09-04T14:02:00")))
        self.assertEqual(decision_from_row({"status": "expired"}), "Expired")
        self.assertIsNone(decision_from_row({"status": "pending"}))
        self.assertTrue(JarvisDesktop._decision_label(True, "once").startswith("Approved once \u00b7 "))
        self.assertEqual(JarvisDesktop._decision_label(True, "always"), "Always allowed")
        self.assertEqual(JarvisDesktop._decision_label(False, "once", reason_sent=True), "Denied \u00b7 reason sent")
        self.assertTrue(JarvisDesktop._decision_label(True, "session", until=now + 86400).startswith("Allowed in this chat until tomorrow "))


def _iso_to_epoch_for_test(value: str) -> float:
    return datetime.fromisoformat(value).timestamp()


class ScrollFrameSettleTests(unittest.TestCase):
    """The scrollbar decision runs outside the scroll callback with a dead band,
    so content within a few pixels of the viewport never flips the bar forever."""

    def test_scrollbar_settles_when_content_is_within_the_dead_band(self):
        import tkinter as tk
        from jarvis.ui import Fonts, ScrollFrame, THEMES, AutoText
        try:
            root = tk.Tk()
        except tk.TclError as exc:  # pragma: no cover - headless hosts
            raise unittest.SkipTest(f"Tk cannot start here: {exc}")
        root.withdraw()
        try:
            app = SimpleNamespace(theme=THEMES["midnight"], fonts=Fonts(root), px=lambda value: int(value), root=root)
            holder = tk.Frame(root, width=360, height=240)
            holder.pack_propagate(False)
            holder.pack()
            frame = ScrollFrame(holder, app, bg="#101010")
            frame.pack(fill="both", expand=True)
            root.update_idletasks()
            viewport = frame.canvas.winfo_height()

            def settle(seconds: float) -> list[int]:
                deadline = time.monotonic() + seconds
                heights = []
                while time.monotonic() < deadline:
                    root.update_idletasks()
                    root.update()
                    heights.append(frame.inner.winfo_reqheight())
                return heights

            # Content 6 px taller than the viewport: inside the dead band, so the
            # bar never appears and nothing oscillates.
            filler = tk.Frame(frame.inner, bg="#202020", height=viewport + 6)
            filler.pack(fill="x")
            heights = settle(0.6)
            self.assertEqual(frame.bar_toggles, 0, f"bar flipped {frame.bar_toggles} times; heights {heights[-6:]} viewport {viewport}")
            self.assertFalse(frame.scrollbar.winfo_ismapped())
            # A wrapping text whose height depends on the canvas width (the bar
            # narrows it): the decision runs once per idle cycle and settles.
            filler.configure(height=viewport - 30)
            text = AutoText(frame.inner, app, font=app.fonts.body, bg="#101010", fg="#eeeeee")
            text.pack(fill="x")
            width = frame.canvas.winfo_width()
            unit = app.fonts.body.measure("x")
            text.set_text("\n".join(["x" * max(1, (width - 4) // max(1, unit))] * 3))
            heights = settle(1.2)
            self.assertLessEqual(frame.bar_toggles, 2, f"bar flipped {frame.bar_toggles} times; heights {heights[-6:]} viewport {viewport}")
            self.assertLess(abs(heights[-1] - heights[-2]), 2)
        finally:
            root.destroy()


class VisualHygieneTests(unittest.TestCase):
    """M10: contrast, zoomed fonts and the single keymap table."""

    def test_faint_text_reaches_aa_contrast_on_every_surface_of_every_theme(self):
        from jarvis.ui import THEMES, THEME_TEXT_SURFACES, contrast_ratio
        self.assertAlmostEqual(contrast_ratio("#000000", "#ffffff"), 21.0, places=1)
        self.assertAlmostEqual(contrast_ratio("#777777", "#777777"), 1.0, places=3)
        for key, theme in THEMES.items():
            for surface in THEME_TEXT_SURFACES:
                ratio = contrast_ratio(theme.faint, getattr(theme, surface))
                self.assertGreaterEqual(ratio, 4.5, f"{key}.faint on {surface}: {ratio:.2f}")
            self.assertGreater(contrast_ratio(theme.muted, theme.bg), contrast_ratio(theme.faint, theme.bg))

    def test_zoom_covers_the_small_fonts_too(self):
        from jarvis.ui import JarvisDesktop
        for name in ("tiny", "small", "small_bold", "label", "label_bold", "mono_small", "body", "mono"):
            self.assertIn(name, JarvisDesktop.ZOOM_FONT_SIZES)

    def test_shortcut_table_lists_every_v3_chord(self):
        from jarvis.ui import SHORTCUTS
        keys = "\n".join(keys for _group, rows in SHORTCUTS for keys, _desc in rows)
        for chord in ("Ctrl + O", "Ctrl + U", "Ctrl + Shift + M", "Ctrl + Shift + R", "Ctrl + Tab", "Ctrl + Alt + P", "Ctrl + Shift + U", "F3", "Ctrl + Shift + P", "Esc", "Enter", "Tab", "\u2191", "F2", "Ctrl + Alt + J"):
            self.assertIn(chord, keys, chord)
        descriptions = "\n".join(desc for _group, rows in SHORTCUTS for _keys, desc in rows)
        self.assertIn("Approve once", descriptions)
        self.assertIn("Deny", descriptions)
        self.assertIn("queue", descriptions.lower())


class OffThreadIoTests(unittest.TestCase):
    """M5: blocking I/O runs on the pool and reports back as ``ui_job`` events."""

    def test_jobs_post_results_and_errors_as_ui_job_events(self):
        events: queue.Queue = queue.Queue()
        jobs = UiJobs(events, max_workers=2)
        seen: list[tuple] = []
        token = jobs.submit("double", lambda value: value * 2, 21, on_done=lambda result, error: seen.append((result, error)))
        event = events.get(timeout=2)
        self.assertEqual(event.kind, "ui_job")
        self.assertEqual(event.payload["job"], "double")
        self.assertEqual(event.payload["token"], token)
        self.assertEqual(event.payload["result"], 42)
        self.assertIsNone(event.payload["error"])
        self.assertTrue(jobs.dispatch(event.payload))
        self.assertEqual(seen, [(42, None)])
        self.assertFalse(jobs.dispatch(event.payload))  # a callback fires once

        def boom():
            raise OSError("disk gone token=sk-proj-" + "B" * 40)

        jobs.submit("boom", boom, on_done=lambda result, error: seen.append((result, error)))
        event = events.get(timeout=2)
        self.assertIsNone(event.payload["result"])
        self.assertTrue(event.payload["error"].startswith("OSError: disk gone"))
        self.assertNotIn("sk-proj-BBBB", event.payload["error"])
        jobs.dispatch(event.payload)
        self.assertEqual(seen[-1][0], None)
        self.assertEqual(jobs.in_flight, 0)
        jobs.shutdown()
        # After shutdown a job still reports instead of vanishing.
        jobs.submit("late", lambda: 1)
        late = events.get(timeout=2)
        self.assertIn("closing", late.payload["error"])
        self.assertFalse(jobs.dispatch({"token": "nope"}))
        self.assertFalse(jobs.dispatch("garbage"))

    def test_path_probe_and_attachment_readers_are_bounded(self):
        self.assertTrue(looks_like_path(r"C:\Users\example-user\notes.txt"))
        self.assertTrue(looks_like_path("/home/example-user/a.py"))
        self.assertTrue(looks_like_path(r"\\example-server\share\file.md"))
        self.assertFalse(looks_like_path("just a sentence with words"))
        self.assertFalse(looks_like_path(""))
        with tempfile.TemporaryDirectory() as temporary:
            real = Path(temporary) / "a.txt"
            real.write_text("alpha", encoding="utf-8")
            missing = str(Path(temporary) / "missing.txt")
            self.assertEqual(probe_paths([str(real), missing]), [str(real)])
            blocks, errors = read_attachments([str(real), missing])
            self.assertEqual(len(blocks), 1)
            self.assertIn("alpha", blocks[0])
            self.assertEqual(len(errors), 1)
            self.assertIn("missing.txt", errors[0])
            target = Path(temporary) / "out" / "export.md"
            target.parent.mkdir()
            self.assertEqual(write_text_file(str(target), "# hi"), str(target))
            self.assertEqual(target.read_text(encoding="utf-8"), "# hi")

class CouncilSessionSafetyTests(unittest.TestCase):
    @staticmethod
    def wait_event(session, kind, timeout=3.0):
        deadline = time.time() + timeout
        seen = []
        while time.time() < deadline:
            try:
                event = session.events.get(timeout=max(0.01, deadline - time.time()))
            except queue.Empty:
                break
            seen.append(event)
            if event.kind == kind:
                return event
        raise AssertionError(f"Council session never emitted {kind}: {[event.kind for event in seen]}")

    def test_duplicate_convene_is_rejected_synchronously_and_in_the_worker_queue(self):
        with tempfile.TemporaryDirectory() as temporary:
            session = CouncilSession(_council_config(temporary))
            self.assertTrue(session.convene("First", "Brief"))
            self.assertFalse(session.convene("Second", "Brief"))
            self.assertEqual(session.commands.qsize(), 1)
            meeting = council.open_meeting("Already active")
            session.commands.put(("convene", ("Bypass", "Brief")))
            self.assertIsNone(session._drain_commands(meeting))
            self.assertTrue(session.commands.empty())
            errors = []
            while not session.events.empty():
                event = session.events.get_nowait()
                if event.kind == "council_error":
                    errors.append(event)
            self.assertEqual(len(errors), 3)

    def test_persisted_stop_rejects_manual_convene_without_building_a_client(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = _council_config(temporary)
            with Memory(config.data_dir / "jarvis.db") as memory:
                memory.set_control_state("stopped", "operator emergency stop")
            builds = []

            def forbidden_builder(_config):
                builds.append(True)
                raise AssertionError("stopped Council must not build a provider client")

            with patch("jarvis.model_client.build_model_client", forbidden_builder):
                session = CouncilSession(config)
                session.start()
                self.wait_event(session, "council_ready")
                self.assertTrue(session.convene("Do not call", "Brief"))
                error = self.wait_event(session, "council_error")
                self.assertIn("emergency stop", str(error.payload).casefold())
                self.assertEqual(builds, [])
                session.shutdown()
                session.join(timeout=2)
                self.assertFalse(session.is_alive())

    def test_persisted_pause_blocks_night_watch_without_a_provider_call(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = _council_config(temporary)
            with Memory(config.data_dir / "jarvis.db") as memory:
                memory.set_control_state("paused", "maintenance")
            builds = []

            def forbidden_builder(_config):
                builds.append(True)
                raise AssertionError("paused night watch must not build a provider client")

            plan = council.NightPlan(enabled=True, idle_seconds=60)
            with patch("jarvis.model_client.build_model_client", forbidden_builder), patch.object(
                CouncilSession, "IDLE_TICK_SECONDS", 0.01
            ), patch("jarvis.ui.council.night_should_sit", return_value=(True, "Ready to sit")):
                session = CouncilSession(config, plan)
                session.last_touch = 0
                session.start()
                self.wait_event(session, "council_ready")
                deadline = time.time() + 2
                paused = None
                while time.time() < deadline:
                    event = session.events.get(timeout=0.2)
                    if (
                        event.kind == "council_night"
                        and "paused" in str(event.payload.get("reason", "")).casefold()
                    ):
                        paused = event
                        break
                self.assertIsNotNone(paused)
                self.assertEqual(builds, [])
                session.shutdown()
                session.join(timeout=2)

    def test_shutdown_closes_blocking_client_and_files_an_interruption(self):
        class BlockingClient:
            def __init__(self):
                self.started = threading.Event()
                self.released = threading.Event()
                self.closed = False
                self.calls = 0

            def chat(self, _messages, tools, _model, **_kwargs):
                self.calls += 1
                self.assert_tool_free = tools
                self.started.set()
                if not self.released.wait(timeout=5):
                    raise RuntimeError("blocking provider was not closed")
                return {"role": "assistant", "content": "1. Shutdown handling"}

            def close(self):
                self.closed = True
                self.released.set()

        with tempfile.TemporaryDirectory() as temporary:
            config = _council_config(temporary)
            client = BlockingClient()
            with patch("jarvis.model_client.build_model_client", return_value=client):
                session = CouncilSession(config)
                session.start()
                self.wait_event(session, "council_ready")
                self.assertTrue(session.convene("Shutdown proof", "Brief"))
                self.assertTrue(client.started.wait(timeout=2))
                started = time.monotonic()
                session.shutdown()
                session.join(timeout=3)
                self.assertLess(time.monotonic() - started, 3)
                self.assertFalse(session.is_alive())
                self.assertTrue(client.closed)
                self.assertEqual(client.assert_tool_free, [])
                meetings = council.list_meetings(config.data_dir)
                self.assertEqual(len(meetings), 1)
                minutes = (Path(meetings[0]["path"]) / "minutes.md").read_text(
                    encoding="utf-8"
                )
                self.assertIn("interrupted while the desktop shut down", minutes)

    def test_desktop_waits_for_both_workers_before_destroying_tk(self):
        class Worker:
            def __init__(self, alive):
                self.alive = alive
                self.events = queue.Queue()

            def is_alive(self):
                return self.alive

        class Root:
            def __init__(self):
                self.destroyed = False
                self.after_calls = []

            def destroy(self):
                self.destroyed = True

            def after(self, delay, callback):
                self.after_calls.append((delay, callback))

        desktop = object.__new__(JarvisDesktop)
        desktop.root = Root()
        desktop.session = Worker(False)
        desktop.council = Worker(True)
        desktop._closing = True
        desktop._close_deadline = time.monotonic() + 2
        desktop._poll_events()
        self.assertFalse(desktop.root.destroyed)
        self.assertEqual(desktop.root.after_calls[0][0], 40)
        desktop.council.alive = False
        desktop._poll_events()
        self.assertTrue(desktop.root.destroyed)


if __name__ == "__main__":
    unittest.main()
