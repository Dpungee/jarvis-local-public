"""Tests for the JARVIS Desktop v3 panes (Memory, Routines, Inbox, Companion).

These build the real Tk widgets against a hidden root and a fake app that
records every call the panes make, so the worker contract (which session
commands are issued, which app methods are called, with which arguments) is
pinned by execution rather than by reading the code.
"""

from __future__ import annotations

import json
import re
import shutil
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from unittest import mock

import tkinter as tk
from tkinter import ttk

import jarvis.ui as ui
import jarvis.ui_panes as ui_panes
from jarvis.ui_panes import (
    CompanionWindow,
    approval_decision_text,
    InboxPopover,
    MarkdownBox,
    MemoryView,
    RoutinesView,
    cadence_minutes,
    companion_image_path,
    erase_fact_command,
    fact_source_label,
    format_interval,
    format_next_run,
    group_claims,
    relative_age,
)


# Every relative time in these tests is measured from one frozen instant, so
# "in 1h" stays "in 1h" no matter how long the full suite has been running.
# Fixtures are seeded relative to FROZEN_AT and the panes' clock is patched to
# NOW (see ``freeze_clock``); nothing here consults the wall clock.
FROZEN_AT = datetime(2026, 9, 3, 12, 0, 0)
NOW = FROZEN_AT.timestamp()
# Decided lines carry ``format_clock`` output, which shows the date when the
# frozen instant is not the wall-clock's today.
CLOCK = ui.format_clock(NOW)
FAKE_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def _iso(delta_seconds: float = 0.0) -> str:
    return (FROZEN_AT + timedelta(seconds=delta_seconds)).isoformat(timespec="seconds")


def freeze_clock(at: float = NOW) -> Any:
    """Patch the panes' ``_now`` hook so every rendered age is relative to ``at``."""
    return mock.patch.object(ui_panes, "_now", lambda: at)


class FakeSettings:
    def __init__(self) -> None:
        self.values: dict[str, Any] = {}

    def get(self, key: str, default: Any = None) -> Any:
        return self.values.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self.values[key] = value


class FakeSession:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def request_facts(self, query: str = "") -> None:
        self.calls.append(("request_facts", (query,)))

    def request_claim_history(self, subject: str, predicate: str) -> None:
        self.calls.append(("request_claim_history", (subject, predicate)))

    def approval_detail(self, approval_id: int) -> None:
        self.calls.append(("approval_detail", (approval_id,)))

    def request_routines(self) -> None:
        self.calls.append(("request_routines", ()))

    def add_routine(self, name: str, prompt: str, interval_minutes: int, project_id: Any) -> None:
        self.calls.append(("add_routine", (name, prompt, interval_minutes, project_id)))

    def set_routine_enabled(self, job_id: Any, enabled: bool) -> None:
        self.calls.append(("set_routine_enabled", (job_id, enabled)))

    def delete_routine(self, job_id: Any) -> None:
        self.calls.append(("delete_routine", (job_id,)))

    def named(self, name: str) -> list[tuple[Any, ...]]:
        return [args for called, args in self.calls if called == name]


class FakeApp:
    """Exactly the surface the panes are allowed to touch."""

    def __init__(self, root: tk.Tk, data_dir: str = "") -> None:
        self.root = root
        self.theme = ui.THEMES["midnight"]
        self.fonts = ui.Fonts(root)
        self.settings = FakeSettings()
        self.session = FakeSession()
        self.data_dir = data_dir
        self.model_label = "Auto"
        self.model_names = {"auto": "qwen3.5:9b"}
        self.conversation_id = 7
        self.project_id = 2
        self.projects = [{"id": 1, "name": "Default"}, {"id": 2, "name": "Atlas"}]
        self.chats = [{"id": 7, "title": "Chat", "created_at": _iso(), "message_count": 2}]
        self.busy = False
        self.ready = True
        self.worker_alive: bool | None = None
        self.pending_approvals = 0
        self.inbox_items: list[dict[str, Any]] = []
        self.inbox_popover: Any = None
        self.approval_details: dict[int, dict[str, Any]] = {}
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def px(self, value: float) -> int:
        return int(value)

    def _record(self, name: str, *args: Any, **kwargs: Any) -> None:
        self.calls.append((name, args, kwargs))

    def named(self, name: str) -> list[tuple[tuple[Any, ...], dict[str, Any]]]:
        return [(args, kwargs) for called, args, kwargs in self.calls if called == name]

    def toast(self, text: str, kind: str = "info") -> None:
        self._record("toast", text, kind=kind)

    def open_chat(self, conversation_id: int) -> None:
        self._record("open_chat", conversation_id)

    def new_chat(self) -> None:
        self._record("new_chat")

    def copy_text(self, text: str) -> None:
        self._record("copy_text", text)

    def remember_fact_dialog(self, seed: str = "") -> None:
        self._record("remember_fact_dialog", seed=seed)

    def focus_composer(self) -> None:
        self._record("focus_composer")

    def show_approvals(self) -> None:
        self._record("show_approvals")

    def open_settings(self) -> None:
        self._record("open_settings")

    def scroll_to_approval(self, approval_id: int) -> None:
        self._record("scroll_to_approval", approval_id)

    def decide_context_approval(self, approval_id: int, approve: bool, *, scope: str = "once", reason: str = "") -> None:
        self._record("decide_context_approval", approval_id, approve, scope=scope, reason=reason)

    def approval_detail_for(self, approval_id: int) -> dict[str, Any] | None:
        self._record("approval_detail_for", approval_id)
        return self.approval_details.get(approval_id)

    def revoke_grant(self, grant_id: int) -> None:
        self._record("revoke_grant", grant_id)

    # The shared ApprovalCard (jarvis.ui) touches these two on the app when
    # its Deny entry opens; they are part of the surface the companion uses.
    def _placeholder(self, entry: tk.Entry, variable: tk.StringVar, text: str) -> None:
        ui.JarvisDesktop._placeholder(self, entry, variable, text)  # type: ignore[arg-type]

    def _refit_texts(self) -> None:
        self._record("_refit_texts")

    def set_view(self, key: str) -> None:
        self._record("set_view", key)

    def send_text(self, text: str) -> None:
        self._record("send_text", text)

    def queue_task_text(self, prompt: str) -> None:
        self._record("queue_task_text", prompt)

    def open_inbox_item(self, item: dict[str, Any]) -> None:
        self._record("open_inbox_item", item)

    def mark_all_read(self) -> None:
        for item in self.inbox_items:
            item["read"] = True
        self._record("mark_all_read")

    def companion_send(self, text: str, images: list[str] | None = None) -> None:
        if images is None:
            self._record("companion_send", text)
        else:
            self._record("companion_send", text, images=images)

    def companion_open_main(self) -> None:
        self._record("companion_open_main")


def _labels(widget: tk.Misc) -> list[str]:
    """Every Label/Text string below ``widget`` in creation order."""
    found: list[str] = []
    stack = [widget]
    while stack:
        current = stack.pop(0)
        if isinstance(current, tk.Label):
            found.append(str(current.cget("text")))
        elif isinstance(current, tk.Text):
            found.append(current.get("1.0", "end-1c"))
        elif isinstance(current, ui.RoundButton):
            found.append(current.text)
        stack.extend(current.winfo_children())
    return found


def _joined(widget: tk.Misc) -> str:
    return "\n".join(_labels(widget))


def _buttons(widget: tk.Misc) -> list[str]:
    """Every RoundButton caption below ``widget`` in creation order."""
    found: list[str] = []
    stack = [widget]
    while stack:
        current = stack.pop(0)
        if isinstance(current, ui.RoundButton):
            found.append(current.text)
        stack.extend(current.winfo_children())
    return found


def _button(widget: tk.Misc, text: str) -> ui.RoundButton:
    stack = [widget]
    while stack:
        current = stack.pop(0)
        if isinstance(current, ui.RoundButton) and current.text == text:
            return current
        stack.extend(current.winfo_children())
    raise AssertionError(f"no button {text!r} under {widget}")


def _pending_afters(root: tk.Tk) -> tuple[str, ...]:
    """Every timer still registered in ``root``'s interpreter (``after info``)."""
    return tuple(str(item) for item in root.tk.splitlist(root.tk.call("after", "info")))


class HelperTests(unittest.TestCase):
    def test_format_interval_presets_and_fallback(self):
        self.assertEqual(format_interval(60), "Every hour")
        self.assertEqual(format_interval(360), "Every 6 h")
        self.assertEqual(format_interval(1440), "Every day")
        self.assertEqual(format_interval(10080), "Every week")
        self.assertEqual(format_interval(45), "Every 45 min")
        self.assertEqual(format_interval("90"), "Every 90 min")
        self.assertEqual(format_interval(None), "Every ? min")

    def test_group_claims_keeps_first_seen_order_and_membership(self):
        rows = [
            {"id": 1, "subject": "deploy", "predicate": "host", "value": "a"},
            {"id": 2, "subject": "owner", "predicate": "name", "value": "b"},
            {"id": 3, "subject": "deploy", "predicate": "port", "value": "c"},
            "not a row",
        ]
        groups = group_claims(rows)
        self.assertEqual([subject for subject, _rows in groups], ["deploy", "owner"])
        self.assertEqual([row["id"] for row in groups[0][1]], [1, 3])
        self.assertEqual([row["id"] for row in groups[1][1]], [2])
        self.assertEqual(group_claims([]), [])

    def test_erase_fact_command_is_exact(self):
        command = erase_fact_command("deploy target", "hostname")
        self.assertEqual(command, 'Erase this project fact: {"subject": "deploy target", "predicate": "hostname"}')
        unicode_command = erase_fact_command("café", "nom")
        self.assertIn("café", unicode_command)
        self.assertEqual(json.loads(unicode_command[len("Erase this project fact: "):]), {"subject": "café", "predicate": "nom"})

    def test_cadence_minutes_maps_presets_and_validates_custom(self):
        self.assertEqual(cadence_minutes("Every hour", ""), (60, ""))
        self.assertEqual(cadence_minutes("Every week", ""), (10080, ""))
        self.assertEqual(cadence_minutes("Custom minutes", "15"), (15, ""))
        minutes, error = cadence_minutes("Custom minutes", "0")
        self.assertIsNone(minutes)
        self.assertTrue(error)
        minutes, error = cadence_minutes("Custom minutes", "ten")
        self.assertIsNone(minutes)
        self.assertTrue(error)
        minutes, error = cadence_minutes("", "")
        self.assertIsNone(minutes)
        self.assertTrue(error)

    def test_time_helpers_are_honest_about_missing_values(self):
        with freeze_clock():
            self.assertEqual(relative_age(None), "")
            self.assertEqual(relative_age(NOW - 5), "just now")
            self.assertEqual(relative_age(NOW - 300), "5m ago")
            self.assertEqual(format_next_run(None), "not scheduled")
            self.assertEqual(format_next_run(_iso(-30)), "due now")
            self.assertEqual(format_next_run(_iso(600)), "in 10m")
            self.assertEqual(format_next_run(NOW + 7200), "in 2h")

    def test_time_helpers_take_an_explicit_now(self):
        # The `now` parameter wins over the module clock, and the module clock
        # is the only wall-clock these helpers ever consult.
        with freeze_clock(NOW + 3600):
            self.assertEqual(relative_age(NOW - 300, now=NOW), "5m ago")
            self.assertEqual(relative_age(NOW - 300), "1h ago")
            self.assertEqual(format_next_run(_iso(3600), now=NOW), "in 1h")
            self.assertEqual(format_next_run(_iso(3600)), "due now")
        self.assertEqual(format_next_run(_iso(3600), now=NOW + 360), "in 54m")

    def test_fact_source_label_maps_store_enums_and_keeps_raw(self):
        # The store's own strings (memory.py writes "explicit operator project
        # fact" with authority "operator") collapse to the contract's words.
        self.assertEqual(fact_source_label({"source": "explicit operator project fact", "actor": "operator"}), ("operator", "source: explicit operator project fact · actor: operator"))
        self.assertEqual(fact_source_label({"source": "chat", "actor": "model"})[0], "model-proposed")
        self.assertEqual(fact_source_label({"source": "model proposal", "actor": "operator", "status": "confirmed"})[0], "model-proposed · confirmed")
        self.assertEqual(fact_source_label({"source": "chat", "actor": "model", "confirmed": True})[0], "model-proposed · confirmed")
        # Nothing recognisable: show the raw source rather than guessing.
        self.assertEqual(fact_source_label({"source": "import-2024", "actor": ""}), ("import-2024", "source: import-2024"))
        self.assertEqual(fact_source_label({}), ("", ""))

    def test_approval_decision_text_matches_the_main_window_wording(self):
        clock = ui.format_clock(NOW)  # "12:00" today, "Sep 03, 12:00" on any other day
        with freeze_clock():
            self.assertEqual(approval_decision_text(True, "once"), f"Approved once · {clock}")
            self.assertEqual(approval_decision_text(True, "session"), f"Allowed in this chat until tomorrow {clock}")
            self.assertEqual(approval_decision_text(True, "session", until=NOW + 86400), "Allowed in this chat until tomorrow 12:00")
            self.assertEqual(approval_decision_text(True, "session", until=NOW + 3600), "Allowed in this chat until 13:00")
            self.assertEqual(approval_decision_text(True, "always"), "Always allowed")
            self.assertEqual(approval_decision_text(False, "deny"), "Denied")
            self.assertEqual(approval_decision_text(False, "deny", reason_sent=True), "Denied · reason sent")
        # An explicit ``now`` wins over the module clock.
        self.assertEqual(approval_decision_text(True, "once", now=NOW + 60), f"Approved once · {ui.format_clock(NOW + 60)}")

    def test_companion_image_path_is_stamped_and_never_overwrites(self):
        folder = tempfile.mkdtemp(prefix="jx-companion-")
        try:
            with freeze_clock():
                first = companion_image_path(folder)
                self.assertEqual(first.name, "companion-20260903-120000.png")
                first.write_bytes(b"x")
                second = companion_image_path(folder)
                self.assertEqual(second.name, "companion-20260903-120000-2.png")
        finally:
            shutil.rmtree(folder, ignore_errors=True)


class TkCase(unittest.TestCase):
    root: tk.Tk

    @classmethod
    def setUpClass(cls) -> None:
        try:
            cls.root = tk.Tk()
        except tk.TclError as exc:  # pragma: no cover - headless hosts
            raise unittest.SkipTest(f"Tk cannot start here: {exc}")
        cls.root.withdraw()

    @classmethod
    def tearDownClass(cls) -> None:
        try:
            cls.root.destroy()
        except tk.TclError:
            pass

    def setUp(self) -> None:
        clock = freeze_clock()
        clock.start()
        self.addCleanup(clock.stop)
        self.data_dir = tempfile.mkdtemp(prefix="jx-panes-")
        self.addCleanup(shutil.rmtree, self.data_dir, True)
        self.app = FakeApp(self.root, self.data_dir)
        self.widgets: list[tk.Misc] = []

    def tearDown(self) -> None:
        # Let the toolkit's own short `after` callbacks (AutoText.settle fits
        # 90 ms later) run before the widgets go away, so nothing fires on a
        # destroyed window and prints a Tk background error.
        time.sleep(0.12)
        self.root.update()
        for widget in self.widgets:
            try:
                widget.destroy()
            except tk.TclError:
                pass
        self.root.update()

    def keep(self, widget: Any) -> Any:
        self.widgets.append(widget)
        return widget

    def pump(self) -> None:
        self.root.update()


FACTS = {
    "query": "",
    "project_id": 2,
    "claims": [
        {"id": 11, "subject": "deploy target", "predicate": "hostname", "value": "atlas.local", "scope": "project", "source": "operator", "actor": "operator", "created_at": _iso(-3600), "updated_at": _iso(-3600), "confidence": None, "superseded_count": 1},
        {"id": 12, "subject": "deploy target", "predicate": "port", "value": "8443", "scope": "project", "source": "chat", "actor": "model", "created_at": _iso(-120), "updated_at": _iso(-120), "confidence": 0.9, "superseded_count": 0},
        {"id": 13, "subject": "owner", "predicate": "timezone", "value": "Europe/Berlin", "scope": "global", "source": "operator", "actor": "operator", "created_at": _iso(-86400 * 3), "updated_at": _iso(-86400 * 3), "confidence": None, "superseded_count": 0},
    ],
}


class MemoryViewTests(TkCase):
    def make(self) -> MemoryView:
        view = self.keep(MemoryView(self.root, self.app))
        view.pack(fill="both", expand=True)
        self.pump()
        return view

    def test_empty_state_before_any_facts(self):
        view = self.make()
        self.assertIn("No governed facts yet", _joined(view.list.inner))
        self.assertEqual(view.count_label.cget("text"), "0 facts")

    def test_refresh_requests_facts_with_empty_query(self):
        view = self.make()
        view.refresh()
        self.assertEqual(self.app.session.named("request_facts"), [("",)])

    def test_facts_event_renders_grouped_rows_and_footer(self):
        view = self.make()
        view.on_event("facts", FACTS)
        self.pump()
        text = _joined(view.list.inner)
        self.assertEqual(len(view.rows), 3)
        self.assertLess(text.index("deploy target"), text.index("owner"))
        self.assertIn("hostname", text)
        self.assertIn("atlas.local", text)
        self.assertIn("since 1h ago", text)
        self.assertIn("since 3d ago", text)
        self.assertIn("global", text)
        self.assertEqual(view.count_label.cget("text"), "3 facts · project Atlas")
        # Only the row with superseded versions offers history.
        toggles = [row for row in view.rows if hasattr(row, "history_toggle")]
        self.assertEqual(len(toggles), 1)
        self.assertEqual(toggles[0].claim["id"], 11)

    def test_source_text_uses_contract_words_and_keeps_raw_enums_in_tooltip(self):
        view = self.make()
        payload = json.loads(json.dumps(FACTS))
        payload["claims"].append({"id": 14, "subject": "owner", "predicate": "editor", "value": "vim", "scope": "global", "source": "model proposal", "actor": "operator", "status": "confirmed", "created_at": _iso(-60), "updated_at": _iso(-60), "confidence": None, "superseded_count": 0})
        view.on_event("facts", payload)
        self.pump()
        metas = [row.meta_label.cget("text") for row in view.rows]
        self.assertEqual(metas[0], "operator · project · since 1h ago")
        self.assertEqual(metas[1], "model-proposed · project · since 2m ago")
        self.assertEqual(metas[3], "model-proposed · confirmed · global · since 1m ago")
        text = _joined(view.list.inner)
        self.assertNotIn("(operator)", text)
        self.assertNotIn("(model-proposed)", text)
        # The raw enums survive in the tooltip only.
        self.assertEqual(view.rows[1].meta_raw, "source: chat · actor: model")
        self.assertEqual(view.rows[1].meta_tooltip.text, "source: chat · actor: model")
        self.assertEqual(view.rows[3].meta_raw, "source: model proposal · actor: operator · status: confirmed")

    def test_filter_narrows_client_side_and_requests_server_query_once_debounced(self):
        view = self.make()
        view.on_event("facts", FACTS)
        self.pump()
        view.set_filter("time")
        self.pump()
        self.assertEqual(len(view.rows), 1)
        self.assertEqual(view.rows[0].claim["id"], 13)
        self.assertEqual(view.count_label.cget("text"), "1 of 3 facts · project Atlas")
        self.assertEqual(self.app.session.named("request_facts"), [])
        time.sleep(0.3)
        self.pump()
        self.assertEqual(self.app.session.named("request_facts"), [("time",)])
        view.set_filter("nothing-like-this")
        self.pump()
        self.assertEqual(len(view.rows), 0)
        self.assertIn("No facts match", _joined(view.list.inner))
        view.set_filter("")
        self.pump()
        self.assertEqual(len(view.rows), 3)
        time.sleep(0.3)
        self.pump()
        # Clearing the filter re-requests the unfiltered list so the view is not stuck on a narrowed set.
        self.assertEqual(self.app.session.named("request_facts")[-1], ("",))

    def test_one_char_filter_does_not_hit_the_store(self):
        view = self.make()
        view.on_event("facts", FACTS)
        view.set_filter("a")
        time.sleep(0.3)
        self.pump()
        self.assertEqual(self.app.session.named("request_facts"), [])

    def test_erase_needs_inline_confirmation_and_sends_exact_command(self):
        view = self.make()
        view.on_event("facts", FACTS)
        self.pump()
        row = view.rows[0]
        row.erase_link.invoke()
        self.pump()
        self.assertEqual(self.app.named("send_text"), [])
        row = view.rows[0]
        self.assertIn("Erase this fact?", _joined(row.actions))
        confirm = [child for child in row.actions.winfo_children()][0]
        confirm.cancel_button.invoke()
        self.pump()
        self.assertEqual(self.app.named("send_text"), [])
        view.rows[0].erase_link.invoke()
        self.pump()
        confirm = view.rows[0].actions.winfo_children()[0]
        confirm.confirm_button.invoke()
        self.pump()
        self.assertEqual(
            self.app.named("send_text"),
            [(('Erase this project fact: {"subject": "deploy target", "predicate": "hostname"}',), {})],
        )
        self.assertIsNone(view.confirming)

    def test_update_opens_dialog_with_seed_and_copy_copies_the_fact(self):
        view = self.make()
        view.on_event("facts", FACTS)
        self.pump()
        view.rows[1].update_link.invoke()
        self.assertEqual(self.app.named("remember_fact_dialog"), [((), {"seed": "deploy target port "})])
        view.remember_button.invoke()
        self.assertEqual(self.app.named("remember_fact_dialog")[-1], ((), {"seed": ""}))
        view.rows[1].copy_link.invoke()
        self.assertEqual(self.app.named("copy_text"), [(("deploy target port 8443",), {})])

    def test_history_toggle_requests_once_and_renders_versions(self):
        view = self.make()
        view.on_event("facts", FACTS)
        self.pump()
        view.rows[0].history_toggle.invoke()
        self.pump()
        self.assertEqual(self.app.session.named("request_claim_history"), [("deploy target", "hostname")])
        self.assertIn("Loading history…", _joined(view.rows[0]))
        view.on_event("claim_history", {
            "subject": "deploy target", "predicate": "hostname",
            "versions": [
                {"value": "atlas.local", "status": "current", "created_at": _iso(-3600), "actor": "operator", "source": "operator"},
                {"value": "old.local", "status": "superseded", "created_at": _iso(-86400), "actor": "operator", "source": "operator"},
                {"value": "gone.local", "status": "retracted", "created_at": _iso(-86400 * 2), "actor": "model", "source": "chat"},
            ],
        })
        self.pump()
        text = _joined(view.rows[0].history_box)
        for glyph, value in (("●", "atlas.local"), ("○", "old.local"), ("✕", "gone.local")):
            self.assertIn(glyph, text)
            self.assertIn(value, text)
        self.assertIn("superseded · 1d ago · operator", text)
        self.assertIn("retracted · 2d ago · model-proposed", text)
        # Collapse and reopen: the cached history is reused, no second request.
        view.rows[0].history_toggle.invoke()
        self.pump()
        self.assertFalse(hasattr(view.rows[0], "history_box"))
        view.rows[0].history_toggle.invoke()
        self.pump()
        self.assertEqual(len(self.app.session.named("request_claim_history")), 1)
        self.assertIn("old.local", _joined(view.rows[0].history_box))

    def test_malformed_payload_renders_nothing_rather_than_crashing(self):
        view = self.make()
        view.on_event("facts", {"claims": "nope"})
        self.pump()
        self.assertEqual(view.rows, [])
        view.on_event("facts", None)
        view.on_event("claim_history", None)
        self.pump()
        self.assertIn("No governed facts yet", _joined(view.list.inner))


ROUTINES = {
    "jobs": [
        {"id": 1, "name": "Morning digest", "prompt": "Summarise overnight logs", "interval_minutes": 1440, "next_run_at": _iso(3600), "last_run_at": _iso(-82800), "last_task_id": 41, "enabled": True, "project_id": 2, "created_at": _iso(-86400 * 4)},
        {"id": 2, "name": "Disk check", "prompt": "Check free disk space", "interval_minutes": 360, "next_run_at": None, "last_run_at": None, "last_task_id": None, "enabled": False, "project_id": 1, "created_at": _iso(-86400)},
    ],
    "tasks": {"41": {"id": 41, "status": "done", "updated_at": _iso(-82800), "last_error": "", "result": "ok"}},
    "worker_alive": True,
}


class FakeAfter:
    """Stand-in for ``widget.after`` that captures callbacks instead of scheduling."""

    def __init__(self) -> None:
        self.scheduled: list[tuple[int, Any]] = []
        self.counter = 0

    def __call__(self, ms: int, callback: Any = None, *args: Any) -> str:
        self.counter += 1
        self.scheduled.append((int(ms), callback))
        return f"fake-after#{self.counter}"

    def fire_last(self) -> None:
        _ms, callback = self.scheduled[-1]
        callback()


class RoutinesViewTests(TkCase):
    def make(self) -> RoutinesView:
        view = self.keep(RoutinesView(self.root, self.app))
        view.pack(fill="both", expand=True)
        self.pump()
        return view

    def test_empty_state_and_refresh_command(self):
        view = self.make()
        self.assertIn("No routines yet", _joined(view.list.inner))
        view.refresh()
        self.assertEqual(self.app.session.named("request_routines"), [()])

    def test_rows_render_cadence_next_last_and_pill(self):
        view = self.make()
        view.on_event("routines", ROUTINES)
        self.pump()
        self.assertEqual(len(view.rows), 2)
        first, second = view.rows
        self.assertEqual(first.meta.cget("text"), "Every day · Next: in 1h · Last: task #41 · done")
        self.assertEqual(first.pill.cget("text"), "Active")
        self.assertEqual(first.pause_link.cget("text"), "Pause")
        self.assertEqual(second.meta.cget("text"), "Every 6 h · Next: paused · Last: never")
        self.assertEqual(second.pill.cget("text"), "Paused")
        self.assertEqual(second.pause_link.cget("text"), "Resume")
        self.assertEqual(second.delete_link.cget("text"), "Delete")

    def test_next_run_text_follows_the_injected_clock_not_the_wall_clock(self):
        # Regression: the seed is one hour out from FROZEN_AT. Rendering six
        # minutes later on the injected clock honestly says "in 54m"; with the
        # clock back at the seed instant it says "in 1h" regardless of how long
        # the suite has been running (the wall clock is never consulted).
        with freeze_clock(NOW + 360):
            view = self.make()
            view.on_event("routines", ROUTINES)
            self.pump()
            self.assertEqual(view.rows[0].meta.cget("text"), "Every day · Next: in 54m · Last: task #41 · done")
        with freeze_clock(), mock.patch.object(time, "time", lambda: NOW + 360):
            view.on_event("routines", ROUTINES)
            self.pump()
            self.assertEqual(view.rows[0].meta.cget("text"), "Every day · Next: in 1h · Last: task #41 · done")

    def test_last_task_without_status_in_map_shows_only_the_id(self):
        view = self.make()
        payload = json.loads(json.dumps(ROUTINES))
        payload["tasks"] = {}
        view.on_event("routines", payload)
        self.pump()
        self.assertIn("Last: task #41", view.rows[0].meta.cget("text"))
        self.assertNotIn("done", view.rows[0].meta.cget("text"))

    def test_pause_resume_and_queue_actions(self):
        view = self.make()
        view.on_event("routines", ROUTINES)
        self.pump()
        view.rows[0].pause_link.invoke()
        self.assertEqual(self.app.session.named("set_routine_enabled"), [(1, False)])
        view.rows[1].pause_link.invoke()
        self.assertEqual(self.app.session.named("set_routine_enabled")[-1], (2, True))
        view.rows[0].queue_link.invoke()
        self.assertEqual(self.app.named("queue_task_text"), [(("Summarise overnight logs",), {})])

    def test_delete_hides_the_row_and_only_reaches_the_store_after_the_undo_window(self):
        view = self.make()
        view.on_event("routines", ROUTINES)
        self.pump()
        fake_after = FakeAfter()
        view.after = fake_after  # instance attribute wins over tk.Misc.after
        view.rows[1].delete_link.invoke()
        self.pump()
        # The row is gone, the strip is up, the store has not been touched.
        self.assertEqual([row.job["id"] for row in view.rows], [1])
        self.assertEqual(self.app.session.named("delete_routine"), [])
        self.assertEqual(fake_after.scheduled[-1][0], ui_panes.ROUTINE_DELETE_UNDO_MS)
        self.assertEqual(ui_panes.ROUTINE_DELETE_UNDO_MS, 5000)
        strip = view.undo_strips[2]
        self.assertEqual(_labels(strip)[:2], ["Routine deleted", "·"])
        self.assertEqual(strip.undo_link.cget("text"), "Undo")
        # Undo restores the row without any store call.
        strip.undo_link.invoke()
        self.pump()
        self.assertEqual([row.job["id"] for row in view.rows], [1, 2])
        self.assertEqual(view.undo_strips, {})
        self.assertEqual(self.app.session.named("delete_routine"), [])
        # A refresh during the window keeps the deletion pending.
        view.rows[1].delete_link.invoke()
        view.on_event("routines", ROUTINES)
        self.pump()
        self.assertEqual([row.job["id"] for row in view.rows], [1])
        self.assertIn(2, view.undo_strips)
        self.assertEqual(self.app.session.named("delete_routine"), [])
        # The timer fires: now, and only now, the store hears about it.
        fake_after.fire_last()
        self.pump()
        self.assertEqual(self.app.session.named("delete_routine"), [(2,)])
        self.assertEqual(view.undo_strips, {})
        self.assertEqual(view.pending_delete, {})

    def test_worker_offline_footer_when_false_or_unknown_with_an_enabled_job(self):
        view = self.make()
        view.on_event("routines", ROUTINES)
        self.pump()
        # The root stays withdrawn, so "shown" means "managed by pack".
        self.assertEqual(view.footer.winfo_manager(), "")
        offline = dict(ROUTINES, worker_alive=False)
        view.on_event("routines", offline)
        self.pump()
        self.assertEqual(view.footer.winfo_manager(), "pack")
        self.assertEqual(view.footer.cget("text"), "Runs will not start until `python -m jarvis worker` is running.")
        # No heartbeat file at all (a fresh data dir) with an enabled routine:
        # honest about the fact that nothing will run.
        unknown = dict(ROUTINES, worker_alive=None)
        view.on_event("routines", unknown)
        self.pump()
        self.assertEqual(view.footer.winfo_manager(), "pack")
        self.assertEqual(view.footer.cget("text"), "Runs will not start until `python -m jarvis worker` is running (no worker heartbeat found).")
        # Unknown with every routine paused: nothing would run anyway.
        paused = json.loads(json.dumps(ROUTINES))
        for job in paused["jobs"]:
            job["enabled"] = False
        paused["worker_alive"] = None
        view.on_event("routines", paused)
        self.pump()
        self.assertEqual(view.footer.winfo_manager(), "")
        # Absent key falls back to what the app knows.
        self.app.worker_alive = False
        payload = {key: value for key, value in ROUTINES.items() if key != "worker_alive"}
        view.on_event("routines", payload)
        self.pump()
        self.assertEqual(view.footer.winfo_manager(), "pack")

    def test_comboboxes_carry_the_desktop_style(self):
        # ttk accepts the style name before the desktop defines it (nothing in
        # these tests does), so the boxes build here and pick up the colours
        # the moment JarvisDesktop._configure_style configures the style.
        view = self.make()
        self.assertEqual(str(view.project_box.cget("style")), "Jarvis.TCombobox")
        self.assertEqual(str(view.cadence_box.cget("style")), "Jarvis.TCombobox")
        style = ttk.Style(self.root)
        style.configure(ui_panes.COMBOBOX_STYLE, fieldbackground="#123456")
        for box in (view.project_box, view.cadence_box):
            self.assertEqual(str(style.lookup(str(box.cget("style")), "fieldbackground")), "#123456")

    def test_form_presets_map_to_minutes_on_create(self):
        view = self.make()
        view.new_button.invoke()
        self.pump()
        self.assertEqual(view.form.winfo_manager(), "pack")
        self.assertEqual(view.new_button.text, "Close form")
        self.assertEqual(view.project_var.get(), "Atlas")
        expectations = (("Every hour", 60), ("Every 6 h", 360), ("Every day", 1440), ("Every week", 10080))
        for label, minutes in expectations:
            view.name_entry.delete(0, "end")
            view.name_entry.insert(0, f"Job {minutes}")
            view.prompt_text.delete("1.0", "end")
            view.prompt_text.insert("1.0", f"do {minutes}")
            view.set_cadence(label)
            view.create_button.invoke()
            self.pump()
            self.assertEqual(self.app.session.named("add_routine")[-1], (f"Job {minutes}", f"do {minutes}", minutes, 2))
            self.assertEqual(view.form.winfo_manager(), "")
            view.new_button.invoke()
            self.pump()
        view.project_var.set("Default")
        view.name_entry.insert(0, "Custom job")
        view.prompt_text.insert("1.0", "custom")
        view.set_cadence("Custom minutes", "15")
        self.assertTrue(view.custom_entry.winfo_manager())
        view.create_button.invoke()
        self.assertEqual(self.app.session.named("add_routine")[-1], ("Custom job", "custom", 15, 1))

    def test_prefill_opens_the_form_with_the_prompt_and_creates_nothing(self):
        view = self.make()
        view.prefill("  check the backups  ")
        self.pump()
        self.assertEqual(view.form.winfo_manager(), "pack")
        self.assertEqual(view.prompt_text.get("1.0", "end-1c"), "check the backups")
        self.assertEqual(view.name_entry.get(), "")
        self.assertEqual(self.app.session.named("add_routine"), [])
        view.prefill("second prompt")
        self.assertEqual(view.prompt_text.get("1.0", "end-1c"), "second prompt")

    def test_form_validation_is_inline(self):
        view = self.make()
        view.show_form()
        self.pump()
        view.create_button.invoke()
        self.assertEqual(view.validation_label.cget("text"), "Give the routine a name.")
        view.name_entry.insert(0, "Named")
        view.create_button.invoke()
        self.assertEqual(view.validation_label.cget("text"), "Write the prompt Jarvis should run.")
        view.prompt_text.insert("1.0", "prompt")
        view.set_cadence("Custom minutes", "abc")
        view.create_button.invoke()
        self.assertIn("whole number", view.validation_label.cget("text"))
        self.assertEqual(self.app.session.named("add_routine"), [])
        self.assertEqual(self.app.named("toast"), [])


class InboxPopoverTests(TkCase):
    def make(self) -> InboxPopover:
        popover = self.keep(InboxPopover(self.app))
        self.app.inbox_popover = popover
        popover.open_at(100, 100)
        self.pump()
        return popover

    def test_empty_state(self):
        popover = self.make()
        self.assertIn("Nothing needs you.", _joined(popover.list.inner))
        self.assertFalse(popover.mark_read_button.enabled)

    def test_only_non_empty_sections_in_order(self):
        now = NOW
        self.app.inbox_items = [
            {"id": "t1", "kind": "task", "title": "Task #9 finished", "detail": "Summary ready", "conversation_id": 3, "approval_id": None, "task_id": 9, "created_at": now - 120, "read": False},
            {"id": "a1", "kind": "approval", "title": "Approve shell command", "detail": "rm -rf build", "conversation_id": 7, "approval_id": 5, "task_id": None, "created_at": now - 10, "read": False},
            {"id": "w1", "kind": "worker", "title": "Worker stopped", "detail": "no heartbeat", "conversation_id": None, "approval_id": None, "task_id": None, "created_at": now - 3600, "read": True},
        ]
        popover = self.make()
        text = _joined(popover.list.inner)
        self.assertIn("NEEDS YOU", text)
        self.assertNotIn("UNREAD REPLIES", text)
        self.assertIn("FINISHED TASKS & ROUTINE RUNS", text)
        self.assertIn("ERRORS", text)
        self.assertLess(text.index("NEEDS YOU"), text.index("FINISHED TASKS"))
        self.assertLess(text.index("FINISHED TASKS"), text.index("ERRORS"))
        self.assertIn("rm -rf build · just now", text)
        self.assertIn("Summary ready · 2m ago", text)
        self.assertIn("no heartbeat · 1h ago", text)
        self.assertEqual(len(popover.rows), 3)

    def test_long_detail_wraps_inside_the_popover_instead_of_clipping(self):
        # Inside compact_activity's 120-char bound, but far wider
        # than the popover, so it must wrap rather than run off the edge.
        detail = "Approve reading C:\\ExampleProject\\Documents\\path\\notes-from-the-meeting-2026-09-03.txt because the model needs it"
        self.assertLessEqual(len(detail), 120)
        self.app.inbox_items = [
            {"id": "a1", "kind": "approval", "title": "Approval #12 · access_private_files computer_read_file with a very long action name", "detail": detail, "conversation_id": 7, "approval_id": 12, "task_id": None, "created_at": NOW - 10, "read": False},
        ]
        popover = self.make()
        row = popover.rows[0]
        for label in (row.title_label, row.detail_label):
            wrap = int(str(label.cget("wraplength")))
            self.assertGreater(wrap, 0)
            self.assertLessEqual(wrap, popover.width)
            self.assertLessEqual(label.winfo_reqwidth(), popover.width)
        # The text is complete (wrapped onto several lines, not truncated to fit).
        self.assertTrue(row.detail_label.cget("text").endswith("because the model needs it · just now"))
        self.assertGreaterEqual(row.detail_label.winfo_reqheight(), 2 * int(self.app.fonts.tiny.metrics("linespace")))
        self.assertGreaterEqual(row.title_label.winfo_reqheight(), 2 * int(self.app.fonts.label_bold.metrics("linespace")))

    def test_click_opens_item_and_closes(self):
        item = {"id": "u1", "kind": "unread", "title": "Reply in Atlas", "detail": "Done", "conversation_id": 3, "approval_id": None, "task_id": None, "created_at": NOW, "read": False}
        self.app.inbox_items = [item]
        popover = self.make()
        popover.rows[0].event_generate("<Button-1>")
        self.pump()
        self.assertEqual(self.app.named("open_inbox_item"), [((item,), {})])
        self.assertTrue(popover.closed)
        self.assertFalse(popover.winfo_exists())
        self.assertIsNone(self.app.inbox_popover)

    def test_mark_all_read_calls_app_and_refreshes(self):
        self.app.inbox_items = [
            {"id": "u1", "kind": "unread", "title": "Reply", "detail": "Done", "conversation_id": 3, "approval_id": None, "task_id": None, "created_at": NOW, "read": False},
        ]
        popover = self.make()
        self.assertTrue(popover.mark_read_button.enabled)
        popover.mark_read_button.invoke()
        self.pump()
        self.assertEqual(self.app.named("mark_all_read"), [((), {})])
        self.assertFalse(popover.mark_read_button.enabled)
        self.assertTrue(popover.winfo_exists())

    def test_escape_closes(self):
        popover = self.make()
        try:
            popover.focus_force()
        except tk.TclError:
            pass
        self.pump()
        popover.event_generate("<Escape>")
        self.pump()
        if not popover.closed:
            # Key events only reach a window that owns keyboard focus; when the
            # host refuses focus to a hidden test window, fall back to the binding.
            self.assertIn("_on_escape", popover.bind("<Escape>"))
            popover.close()
        self.assertTrue(popover.closed)


class CompanionWindowTests(TkCase):
    def make(self) -> CompanionWindow:
        window = self.keep(CompanionWindow(self.app))
        window.show()
        self.pump()
        return window

    def test_initial_geometry_and_title(self):
        window = self.make()
        self.assertEqual(window.title(), "Jarvis")
        self.assertEqual(window.head_label.cget("text"), "Jarvis · Auto")
        geometry = window.geometry()
        self.assertTrue(geometry.startswith("640x160"), geometry)

    def test_saved_geometry_is_used_when_valid(self):
        self.app.settings.set("companion_geometry", "700x200+50+60")
        window = self.make()
        self.assertTrue(window.geometry().startswith("700x200"), window.geometry())
        window.hide()
        self.app.settings.set("companion_geometry", "garbage")
        other = self.keep(CompanionWindow(self.app))
        other.show()
        self.pump()
        self.assertTrue(other.geometry().startswith("640x160"), other.geometry())

    def test_enter_sends_and_clears_shift_enter_keeps_typing(self):
        window = self.make()
        window.input.set_value("what is the deploy host")
        self.pump()
        result = window._on_return(None)
        self.assertEqual(result, "break")
        self.assertEqual(self.app.named("companion_send"), [(("what is the deploy host",), {})])
        self.assertEqual(window.input.value(), "")
        window.input.set_value("   ")
        window.send()
        self.assertEqual(len(self.app.named("companion_send")), 1)
        window.input.focus_force()
        window.input.set_value("line one")
        self.pump()
        window.input.event_generate("<Shift-Return>")
        self.pump()
        self.assertEqual(len(self.app.named("companion_send")), 1)

    def test_deltas_stream_into_reply_and_status_tracks_busy(self):
        window = self.make()
        window.on_event("busy", True)
        self.pump()
        self.assertTrue(window.status_label.cget("text").startswith("Working · "))
        window.on_event("activity", "Warming up qwen3.5:9b")
        self.pump()
        self.assertTrue(window.status_label.cget("text").startswith("Loading model · "))
        window.on_event("delta", {"text": "The host "})
        window.on_event("delta", {"text": "is atlas.local"})
        self.pump()
        self.assertEqual(window.reply.plain_text(), "The host is atlas.local")
        self.assertEqual(window.reply.block_kinds(), ["stream"])
        self.assertEqual(window.reply_frame.winfo_manager(), "pack")
        window.on_event("assistant", {"content": "The host is atlas.local.", "status": "complete", "reason": "", "approval_id": None, "model": "qwen3.5:9b", "elapsed": 2.5})
        window.on_event("busy", False)
        self.pump()
        self.assertEqual(window.reply.plain_text(), "The host is atlas.local.")
        self.assertEqual(window.reply.block_kinds(), ["paragraph"])
        self.assertEqual(window.status_label.cget("text"), "qwen3.5:9b · 2.5s")
        self.assertFalse(window.approval_row.winfo_ismapped())
        self.assertIsNone(window._timer)

    def test_reply_is_rendered_as_markdown_with_a_copyable_fenced_block(self):
        window = self.make()
        window.on_event("busy", True)
        window.on_event("delta", {"text": "# Deploy\n\nUse **this**:\n\n```python\nprint('hi')\n```"})
        self.pump()
        # While streaming the text is shown verbatim, fences and all.
        self.assertEqual(window.reply.block_kinds(), ["stream"])
        self.assertIn("```python", window.reply.plain_text())
        content = "# Deploy\n\nUse **this** with `care`:\n\n```python\nprint('hi')\n```\n\n- first\n- second\n\n> quoted"
        window.on_event("assistant", {"content": content, "status": "complete", "reason": "", "approval_id": None, "model": "qwen3.5:9b", "elapsed": 1.0})
        window.on_event("busy", False)
        self.pump()
        self.assertEqual(window.reply.block_kinds(), ["heading", "paragraph", "code", "list", "quote"])
        text = window.reply.plain_text()
        self.assertNotIn("```", text)
        self.assertNotIn("**", text)
        self.assertIn("Deploy", text)
        self.assertIn("Use this with care:", text)
        self.assertIn("print('hi')", text)
        self.assertIn("•  first\n•  second", text)
        self.assertIn("quoted", text)
        frames = window.reply.code_frames()
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0].code, "print('hi')")
        self.assertIn("PYTHON", _labels(frames[0]))
        self.assertEqual(frames[0].copy_link.cget("text"), "Copy")
        # The only Copy in the whole reply is the one inside the code frame.
        self.assertEqual(_labels(window.reply).count("Copy"), 1)
        frames[0].copy_link.invoke()
        self.pump()
        self.assertEqual(self.app.named("copy_text"), [(("print('hi')",), {})])
        self.assertEqual(frames[0].copy_link.cget("text"), "Copied ✓")
        # The bold run carries the bold tag in the rendered paragraph.
        paragraph = window.reply.text_widgets()[1]
        self.assertIn("bold", paragraph.tag_names("1.4"))

    def test_ctrl_v_with_no_text_saves_the_clipboard_image_and_sends_its_path(self):
        window = self.make()
        with mock.patch.object(ui_panes, "clipboard_image_png", lambda: FAKE_PNG), \
                mock.patch.object(CompanionWindow, "_clipboard_text", lambda self: ""):
            self.assertEqual(window._paste(None), "break")
        self.pump()
        self.assertEqual(len(window.images), 1)
        path = Path(window.images[0])
        self.assertEqual(path.parent, Path(self.data_dir) / "attachments")
        self.assertEqual(path.name, "companion-20260903-120000.png")
        self.assertEqual(path.read_bytes(), FAKE_PNG)
        self.assertEqual(window.chips_row.winfo_manager(), "pack")
        self.assertIn("companion-20260903-120000.png", _joined(window.chips_row))
        self.assertIn("Image attached", window.status_label.cget("text"))
        # Text on the clipboard means an ordinary paste (None lets Tk handle it) and no file.
        with mock.patch.object(ui_panes, "clipboard_image_png", lambda: FAKE_PNG), \
                mock.patch.object(CompanionWindow, "_clipboard_text", lambda self: "some text"):
            self.assertIsNone(window._paste(None))
        self.assertEqual(len(window.images), 1)
        # No image either: say so in the status line, nothing written.
        with mock.patch.object(ui_panes, "clipboard_image_png", lambda: None), \
                mock.patch.object(CompanionWindow, "_clipboard_text", lambda self: ""):
            self.assertEqual(window._paste(None), "break")
        self.assertEqual(len(window.images), 1)
        self.assertEqual(window.status_label.cget("text"), ui_panes.COMPANION_NOTHING_TO_PASTE)
        self.assertEqual(len(list((Path(self.data_dir) / "attachments").iterdir())), 1)
        # Sending carries the image through the new keyword and clears the chip.
        window.input.set_value("what is in this?")
        window.send()
        self.assertEqual(self.app.named("companion_send"), [(("what is in this?",), {"images": [str(path)]})])
        self.assertEqual(window.images, [])
        self.assertEqual(window.chips_row.winfo_manager(), "")
        # Removing a chip drops the image without sending.
        with mock.patch.object(ui_panes, "clipboard_image_png", lambda: FAKE_PNG), \
                mock.patch.object(CompanionWindow, "_clipboard_text", lambda self: ""):
            window._paste(None)
        self.assertEqual(len(window.images), 1)
        window.chips[0].remove_link.invoke()
        self.pump()
        self.assertEqual(window.images, [])
        self.assertEqual(window.chips, [])

    def test_reply_area_grows_to_twelve_lines_then_scrolls_and_never_hides_the_status_line(self):
        window = self.make()
        ceiling = window.reply_ceiling()
        short = "Two lines.\n\nThat is all."
        window.on_event("assistant", {"content": short, "status": "complete", "reason": "", "approval_id": None, "model": "qwen3.5:9b", "elapsed": 0.4})
        window.update()
        self.pump()
        self.assertFalse(window.reply_frame.scrollable())
        self.assertEqual(window.reply_frame.scrollbar.winfo_manager(), "")
        self.assertLess(window.reply_frame.viewport_height, ceiling)
        self.assertEqual(window.reply_frame.viewport_height, window.reply_frame.content_height)
        # Nothing but paragraph text lives in the reply area (no stray buttons or bars).
        self.assertEqual({type(child).__name__ for child in window.reply.winfo_children()}, {"AutoText"})
        self.assertEqual(window.reply.block_kinds(), ["paragraph", "paragraph"])
        long = "\n\n".join(f"Paragraph {index} of a long reply." for index in range(1, 31))
        window.on_event("assistant", {"content": long, "status": "complete", "reason": "", "approval_id": None, "model": "qwen3.5:9b", "elapsed": 3.0})
        window.update()
        self.pump()
        self.assertTrue(window.reply_frame.scrollable())
        self.assertEqual(window.reply_frame.scrollbar.winfo_manager(), "pack")
        self.assertEqual(window.reply_frame.viewport_height, ceiling)
        self.assertGreater(window.reply_frame.content_height, ceiling)
        # The window grew to fit, and the status line sits inside it, below the reply.
        window.update()
        height = int(window.geometry().split("+")[0].split("x")[1])
        self.assertGreaterEqual(height, window.body.winfo_reqheight())
        self.assertEqual(window.status_row.winfo_manager(), "pack")
        status_bottom = window.status_row.winfo_y() + window.status_row.winfo_height()
        self.assertLessEqual(status_bottom, window.body.winfo_height())
        self.assertGreater(window.status_row.winfo_y(), window.reply_frame.winfo_y() + window.reply_frame.winfo_height() - 1)
        self.assertEqual(window.status_label.cget("text"), "qwen3.5:9b · 3.0s")
        # New chat shrinks back to the default height.
        window.on_event("new_chat", {"conversation_id": 5})
        window.update()
        self.pump()
        self.assertTrue(window.geometry().startswith("640x160"), window.geometry())

    def test_stopped_early_status(self):
        window = self.make()
        window.on_event("busy", True)
        window.on_event("assistant", {"content": "", "status": "cancelled", "reason": "stopped by you", "approval_id": None, "model": None, "elapsed": None})
        self.pump()
        self.assertEqual(window.status_label.cget("text"), "Stopped early: stopped by you")
        self.assertFalse(window.approval_row.winfo_ismapped())
        self.assertIsNone(window.approval_card)

    ELIGIBLE_ROW = {
        "id": 12, "action": "computer_read_file", "resource": "C:\\ExampleProject\\notes.txt",
        "reason": "The model wants to read a private file.", "scope": "conversation:7",
        "persistent_eligible": True, "status": "pending",
    }

    def show_card(self, window: CompanionWindow, approval_id: int = 12, **overrides: Any) -> ui.ApprovalCard:
        row = dict(self.ELIGIBLE_ROW, id=approval_id, **overrides)
        # expires_at is judged by the shared card against the wall clock.
        row.setdefault("expires_at", (datetime.now().replace(microsecond=0) + timedelta(minutes=30)).isoformat(timespec="seconds"))
        self.app.approval_details[approval_id] = row
        window.on_event("assistant", {"content": "Need to read a file.", "status": "approval", "reason": "", "approval_id": approval_id, "model": "qwen3.5:9b", "elapsed": 1.0})
        self.pump()
        card = window.approval_card
        self.assertIsInstance(card, ui.ApprovalCard)
        assert card is not None
        return card

    def test_approval_is_the_shared_card_with_standing_buttons_for_an_eligible_chat_row(self):
        window = self.make()
        card = self.show_card(window)
        self.assertTrue(window.approval_row.winfo_ismapped())
        self.assertIs(card.master, window.approval_row)
        self.assertTrue(card.compact)
        self.assertEqual(window.status_label.cget("text"), "Waiting for your approval above")
        self.assertEqual(self.app.named("approval_detail_for"), [((12,), {})])
        self.assertEqual(self.app.session.calls, [])  # the cached row was enough
        self.assertRegex(card.headline.cget("text"), r"^Needs your approval · computer_read_file · expires in (29|30) min$")
        text = _joined(card)
        self.assertLess(text.index("C:\\ExampleProject\\notes.txt"), text.index("The model wants to read a private file."))
        self.assertLess(text.index("The model wants"), text.index("Scope: This chat"))
        # Same buttons as the chat card: Deny is created first (packed right).
        self.assertEqual(_buttons(card), ["Deny", "Approve once", "This chat · 24 h", "Always"])
        self.assertNotIn(ui.STANDING_UNAVAILABLE_TEXT, text)
        # Labels are re-wrapped for the companion's width, not the context panel's.
        wraps = {int(str(child.cget("wraplength"))) for child in card.winfo_children() if isinstance(child, tk.Label)}
        self.assertIn(self.app.px(ui_panes.COMPANION_CARD_WRAP), wraps)
        self.assertNotIn(self.app.px(230), wraps)
        # The companion's extra: an Open in Jarvis link in the card head.
        self.assertEqual(window.open_approval_link.cget("text"), "Open in Jarvis")
        window.open_approval_link.invoke()
        self.assertEqual(self.app.named("scroll_to_approval"), [((12,), {})])

    def test_non_eligible_row_has_no_standing_buttons_and_says_why(self):
        window = self.make()
        card = self.show_card(window, action="shell", resource="git status", persistent_eligible=False)
        self.assertEqual(_buttons(card), ["Deny", "Approve once"])
        self.assertIn(ui.STANDING_UNAVAILABLE_TEXT, _joined(card))
        # Eligible but task-scoped: Always only, never This chat.
        card = self.show_card(window, approval_id=14, scope="task:3")
        self.assertEqual(_buttons(card), ["Deny", "Approve once", "Always"])
        self.assertIn("Scope: Task #3", _joined(card))

    def test_approve_once_goes_through_the_app_and_the_decided_line_comes_from_the_event(self):
        window = self.make()
        card = self.show_card(window)
        assert card.approve_button is not None
        card.approve_button.invoke()
        self.pump()
        self.assertEqual(self.app.named("decide_context_approval"), [((12, True), {"scope": "once", "reason": ""})])
        card = window.approval_card
        assert card is not None
        self.assertEqual(_buttons(card), [])
        self.assertEqual(card.headline.cget("text"), "Approval #12 · computer_read_file")
        self.assertIn(f"Approved once · {CLOCK} · recording…", _joined(card))
        # A second click while recording decides nothing twice.
        window._decide(12, False, "once", "")
        self.assertEqual(len(self.app.named("decide_context_approval")), 1)
        window.on_event("approval_decided", {"approval_id": 12, "approved": True, "changed": True, "scope": "once", "grant_id": None, "note": ""})
        self.pump()
        card = window.approval_card
        assert card is not None
        self.assertIn(f"Approved once · {CLOCK}", _joined(card))
        self.assertNotIn("recording", _joined(card))
        # Another approval's event is ignored.
        window.on_event("approval_decided", {"approval_id": 99, "approved": False, "changed": True, "scope": "deny", "grant_id": None, "note": ""})
        self.assertIn(f"Approved once · {CLOCK}", _joined(window.approval_card))

    def test_this_chat_decision_reads_allowed_until_the_time_the_store_reports(self):
        window = self.make()
        card = self.show_card(window)
        _button(card, "This chat · 24 h").invoke()
        self.pump()
        self.assertEqual(self.app.named("decide_context_approval"), [((12, True), {"scope": "session", "reason": ""})])
        self.assertIn(f"Allowed in this chat until tomorrow {CLOCK} · recording…", _joined(window.approval_card))
        window.on_event("approval_decided", {"approval_id": 12, "approved": True, "changed": True, "scope": "session", "grant_id": None, "note": "", "until": _iso(86400)})
        self.pump()
        self.assertIn("Allowed in this chat until tomorrow 12:00", _joined(window.approval_card))
        # Always: the grant id arrives with the event and the card offers Revoke.
        card = self.show_card(window, approval_id=15)
        _button(card, "Always").invoke()
        window.on_event("approval_decided", {"approval_id": 15, "approved": True, "changed": True, "scope": "always", "grant_id": 4, "note": ""})
        self.pump()
        self.assertIn("Always allowed", _joined(window.approval_card))
        _button(window.approval_card, "Revoke").invoke()
        self.assertEqual(self.app.named("revoke_grant"), [((4,), {})])

    def test_deny_with_instruction_sends_the_reason_through_the_app(self):
        window = self.make()
        card = self.show_card(window)
        assert card.deny_button is not None
        card.deny_button.invoke()
        self.pump()
        self.assertEqual(self.app.named("decide_context_approval"), [])  # Deny only reveals the entry
        entry = card.deny_entry
        entry._placeholder_active = False  # type: ignore[attr-defined]
        entry.delete(0, "end")
        entry.insert(0, "use the cached copy instead")
        _button(card, "Send").invoke()
        self.pump()
        self.assertEqual(self.app.named("decide_context_approval"), [((12, False), {"scope": "once", "reason": "use the cached copy instead"})])
        self.assertIn("Denied · recording…", _joined(window.approval_card))
        window.on_event("approval_decided", {"approval_id": 12, "approved": False, "changed": True, "scope": "deny", "grant_id": None, "note": ""})
        self.pump()
        self.assertIn("Denied · reason sent", _joined(window.approval_card))
        # Just deny: no reason, plain Denied.
        card = self.show_card(window, approval_id=16)
        card.show_deny()
        _button(card, "Just deny").invoke()
        window.on_event("approval_decided", {"approval_id": 16, "approved": False, "changed": True, "scope": "deny", "grant_id": None, "note": ""})
        self.pump()
        self.assertEqual(self.app.named("decide_context_approval")[-1], ((16, False), {"scope": "once", "reason": ""}))
        self.assertIn("Denied", _joined(window.approval_card))
        self.assertNotIn("reason sent", _joined(window.approval_card))

    def test_a_decision_the_store_declined_is_read_back_or_left_pending(self):
        window = self.make()
        card = self.show_card(window)
        assert card.approve_button is not None
        card.approve_button.invoke()
        # Expired: the row read back from the store says so.
        window.on_event("approval_decided", {"approval_id": 12, "approved": True, "changed": False, "scope": "once", "grant_id": None, "note": "", "row": dict(self.ELIGIBLE_ROW, status="expired")})
        self.pump()
        self.assertIn("Expired", _joined(window.approval_card))
        self.assertEqual(_buttons(window.approval_card), [])
        # Not eligible / not recorded: the note goes to the status line and
        # the card stays pending so Approve once is still possible.
        card = self.show_card(window, approval_id=17)
        _button(card, "This chat · 24 h").invoke()
        window.on_event("approval_decided", {"approval_id": 17, "approved": True, "changed": False, "scope": "session", "grant_id": None, "note": "This action is not eligible for a chat-wide grant; approve it once instead."})
        self.pump()
        self.assertEqual(window.status_label.cget("text"), "This action is not eligible for a chat-wide grant; approve it once instead.")
        self.assertIn("Approve once", _buttons(window.approval_card))
        window.approval_card.approve_button.invoke()
        self.assertEqual(self.app.named("decide_context_approval")[-1], ((17, True), {"scope": "once", "reason": ""}))
        # No row, no note: the honest fallback.
        card = self.show_card(window, approval_id=18)
        card.approve_button.invoke()
        window.on_event("approval_decided", {"approval_id": 18, "approved": True, "changed": False, "scope": "once", "grant_id": None, "note": ""})
        self.pump()
        self.assertIn("Already decided", _joined(window.approval_card))

    def test_uncached_row_is_requested_from_the_store_and_the_detail_event_fills_the_card(self):
        window = self.make()
        window.on_event("assistant", {"content": "Need to run a command.", "status": "approval", "reason": "", "approval_id": 13, "model": "qwen3.5:9b", "elapsed": 1.0})
        self.pump()
        self.assertEqual(self.app.session.calls, [("approval_detail", (13,))])
        card = window.approval_card
        self.assertIsInstance(card, ui.ApprovalCard)
        self.assertEqual(card.headline.cget("text"), "Needs your approval")
        self.assertIn("Loading the exact target…", _joined(card))
        self.assertEqual(_buttons(card), [])
        # A detail for another approval changes nothing.
        window.on_event("approval_detail", {"approval_id": 99, "approval": dict(self.ELIGIBLE_ROW, id=99)})
        self.assertIn("Loading the exact target…", _joined(window.approval_card))
        window.on_event("approval_detail", {"approval_id": 13, "approval": dict(self.ELIGIBLE_ROW, id=13, action="shell", resource="git status", persistent_eligible=False)})
        self.pump()
        card = window.approval_card
        self.assertEqual(card.headline.cget("text"), "Needs your approval · shell")
        self.assertEqual(_buttons(card), ["Deny", "Approve once"])
        # A row that is gone, and one that arrives already decided.
        window.on_event("assistant", {"content": "", "status": "approval", "reason": "", "approval_id": 21, "model": None, "elapsed": None})
        window.on_event("approval_detail", {"approval_id": 21, "approval": {"missing": True}})
        self.pump()
        self.assertIn("no longer available", _joined(window.approval_card))
        window.on_event("assistant", {"content": "", "status": "approval", "reason": "", "approval_id": 22, "model": None, "elapsed": None})
        window.on_event("approval_detail", {"approval_id": 22, "approval": dict(self.ELIGIBLE_ROW, id=22, status="denied", decided_at=_iso(-60))})
        self.pump()
        self.assertIn(f"Denied · {ui.format_clock(NOW - 60)}", _joined(window.approval_card))
        self.assertEqual(_buttons(window.approval_card), [])

    def test_an_unseen_target_offers_only_deny_and_open_in_jarvis(self):
        window = self.make()
        # The store no longer has the row.
        window.on_event("assistant", {"content": "", "status": "approval", "reason": "", "approval_id": 31, "model": None, "elapsed": None})
        window.on_event("approval_detail", {"approval_id": 31, "approval": {"missing": True}})
        self.pump()
        card = window.approval_card
        self.assertEqual(_buttons(card), ["Deny"])
        self.assertIsNone(card.approve_button)
        text = _joined(card)
        self.assertIn("no longer available", text)
        self.assertIn(ui_panes.COMPANION_UNSEEN_TARGET_TEXT, text)
        self.assertNotIn(ui.STANDING_UNAVAILABLE_TEXT, text)
        self.assertEqual(window.open_approval_link.cget("text"), "Open in Jarvis")
        # A row whose resource is empty is just as unseen, eligible or not.
        card = self.show_card(window, approval_id=32, resource="")
        self.assertEqual(_buttons(card), ["Deny"])
        self.assertIn(ui_panes.COMPANION_UNSEEN_TARGET_TEXT, _joined(card))
        card = self.show_card(window, approval_id=33, resource="   ", persistent_eligible=False)
        self.assertEqual(_buttons(card), ["Deny"])
        self.assertNotIn(ui.STANDING_UNAVAILABLE_TEXT, _joined(card))
        # Deny still goes through the app, with an instruction if given.
        card.show_deny()
        _button(card, "Just deny").invoke()
        self.assertEqual(self.app.named("decide_context_approval"), [((33, False), {"scope": "once", "reason": ""})])
        # Nothing was ever offered for approval on the unseen rows.
        self.assertNotIn(True, [args[1] for args, _kw in self.app.named("decide_context_approval")])
        # With the target shown, the full card is back.
        card = self.show_card(window, approval_id=34)
        self.assertEqual(_buttons(card), ["Deny", "Approve once", "This chat · 24 h", "Always"])
        self.assertNotIn(ui_panes.COMPANION_UNSEEN_TARGET_TEXT, _joined(card))

    def test_window_grows_upward_so_its_bottom_edge_stays_on_screen(self):
        window = self.make()
        window.show()
        window.geometry("640x160+120+420")
        window.update()
        self.pump()
        before = re.match(r"^(\d+)x(\d+)\+(\d+)\+(\d+)$", window.geometry())
        assert before is not None
        width0, height0, x0, y0 = (int(part) for part in before.groups())
        self.assertEqual(height0, 160)
        self.show_card(window)
        window.update()
        self.pump()
        after = re.match(r"^(\d+)x(\d+)\+(\d+)\+(\d+)$", window.geometry())
        assert after is not None
        width, height, x, y = (int(part) for part in after.groups())
        self.assertGreater(height, height0)
        self.assertGreaterEqual(height, window.body.winfo_reqheight())
        self.assertEqual((width, x), (width0, x0))
        self.assertEqual(y + height, y0 + height0)  # bottom edge unchanged
        # New chat shrinks back down onto the same bottom edge.
        window.on_event("new_chat", {"conversation_id": 5})
        window.update()
        self.pump()
        self.assertEqual(window.geometry(), f"{width0}x{height0}+{x0}+{y0}")

    def test_retry_with_approval_resends_the_last_request_as_a_new_turn(self):
        window = self.make()
        window.input.set_value("what is in my notes?")
        window.send()
        self.assertEqual(self.app.named("companion_send"), [(("what is in my notes?",), {})])
        card = self.show_card(window)
        self.assertNotIn("Retry with approval", _buttons(card))
        assert card.approve_button is not None
        card.approve_button.invoke()
        self.pump()
        self.assertNotIn("Retry with approval", _buttons(window.approval_card))  # still recording
        self.assertEqual(window.status_label.cget("text"), "Waiting for your approval above")
        window.on_event("approval_decided", {"approval_id": 12, "approved": True, "changed": True, "scope": "once", "grant_id": None, "note": ""})
        self.pump()
        self.assertEqual(window.status_label.cget("text"), f"Approved once · {CLOCK}")
        self.assertIn("nothing resumes on its own", _joined(window.approval_card))
        _button(window.approval_card, "Retry with approval").invoke()
        self.pump()
        self.assertEqual(self.app.named("companion_send"), [(("what is in my notes?",), {})] * 2)
        self.assertIsNone(window.approval_card)
        self.assertFalse(window.approval_row.winfo_ismapped())
        self.assertEqual(window.status_label.cget("text"), "Sending…")
        # Images ride along with the retried request.
        window._last_request = ("see this", ["C:\\shots\\one.png"])
        window._retry_after_approval()
        self.assertEqual(self.app.named("companion_send")[-1], (("see this",), {"images": ["C:\\shots\\one.png"]}))
        # While Jarvis is busy there is nothing to retry into; a denial offers nothing.
        self.app.busy = True
        card = self.show_card(window, approval_id=41)
        card.approve_button.invoke()
        window.on_event("approval_decided", {"approval_id": 41, "approved": True, "changed": True, "scope": "once", "grant_id": None, "note": ""})
        self.pump()
        self.assertNotIn("Retry with approval", _buttons(window.approval_card))
        self.app.busy = False
        card = self.show_card(window, approval_id=42)
        card.show_deny()
        _button(card, "Just deny").invoke()
        window.on_event("approval_decided", {"approval_id": 42, "approved": False, "changed": True, "scope": "deny", "grant_id": None, "note": ""})
        self.pump()
        self.assertNotIn("Retry with approval", _buttons(window.approval_card))
        self.assertEqual(window.status_label.cget("text"), "Denied")
        # Without any request sent from this box there is nothing to retry.
        window._last_request = None
        card = self.show_card(window, approval_id=43)
        card.approve_button.invoke()
        window.on_event("approval_decided", {"approval_id": 43, "approved": True, "changed": True, "scope": "once", "grant_id": None, "note": ""})
        self.pump()
        self.assertNotIn("Retry with approval", _buttons(window.approval_card))

    def test_new_chat_and_chat_created_events(self):
        window = self.make()
        self.show_card(window)
        window.on_event("new_chat", {"conversation_id": 99})
        self.pump()
        self.assertFalse(window.approval_row.winfo_ismapped())
        self.assertIsNone(window.approval_card)
        self.assertIsNone(window.approval_id)
        self.assertEqual(window.reply.plain_text(), "")
        self.assertEqual(window.conversation_id, 99)
        # chat_created (a chat made for the companion without switching the
        # main view) only records the id; the reply area is left alone.
        window.on_event("assistant", {"content": "Kept.", "status": "complete", "reason": "", "approval_id": None, "model": "qwen3.5:9b", "elapsed": 0.2})
        window.on_event("chat_created", {"conversation_id": 100, "project_id": 2, "activate": False})
        self.pump()
        self.assertEqual(window.conversation_id, 100)
        self.assertEqual(window.reply.plain_text(), "Kept.")
        window.on_event("chat_created", "garbage")
        window.on_event("chat_created", {"project_id": 2})
        self.assertEqual(window.conversation_id, 100)
        window.on_event("unknown_kind", {"conversation_id": 5})
        self.assertEqual(window.conversation_id, 100)

    def test_header_buttons_call_the_app(self):
        window = self.make()
        window.new_chat_button.invoke()
        window.open_button.invoke()
        self.assertEqual(self.app.named("new_chat"), [((), {})])
        self.assertEqual(self.app.named("companion_open_main"), [((), {})])

    def test_new_chat_while_busy_says_so_in_the_companion_not_the_main_window(self):
        window = self.make()
        self.app.busy = True
        window.new_chat_button.invoke()
        self.assertEqual(self.app.named("new_chat"), [])
        self.assertEqual(self.app.named("toast"), [])
        self.assertEqual(window.status_label.cget("text"), "Stop or finish the current request first.")
        self.app.busy = False
        window.new_chat_button.invoke()
        self.assertEqual(self.app.named("new_chat"), [((), {})])

    def test_geometry_saved_on_hide(self):
        window = self.make()
        self.assertIsNone(self.app.settings.get("companion_geometry"))
        window.hide()
        self.pump()
        saved = self.app.settings.get("companion_geometry")
        self.assertIsInstance(saved, str)
        self.assertRegex(saved, r"^\d+x\d+[+-]\d+[+-]\d+$")
        self.assertEqual(window.state(), "withdrawn")
        window.show()
        self.pump()
        self.assertNotEqual(window.state(), "withdrawn")


class AfterCancellationTests(unittest.TestCase):
    """Destroying the root with panes mid-schedule must leave no timer behind.

    A leaked ``after`` fires in the next Tk event loop against a deleted
    command and prints ``bgerror … invalid command name "…save_geometry"``.
    The check is deterministic: after ``root.destroy()`` the interpreter's
    ``after info`` must be empty, and a ``bgerror`` hook installed in that
    interpreter must stay silent while another root pumps events.
    """

    def test_root_destroy_cancels_every_scheduled_callback(self):
        try:
            root = tk.Tk()
        except tk.TclError as exc:  # pragma: no cover - headless hosts
            raise unittest.SkipTest(f"Tk cannot start here: {exc}")
        root.withdraw()
        errors: list[str] = []
        root.tk.createcommand("bgerror", lambda *args: errors.append(" ".join(str(a) for a in args)))
        data_dir = tempfile.mkdtemp(prefix="jx-panes-after-")
        self.addCleanup(shutil.rmtree, data_dir, True)
        app = FakeApp(root, data_dir)
        with freeze_clock():
            companion = CompanionWindow(app)
            companion.show()
            root.update()
            companion.on_event("busy", True)            # 1 s status timer
            companion.on_event("delta", {"text": "hi"})  # refit timers
            companion.hide()                            # used to orphan the geometry save
            companion.show()                            # re-arms the 400 ms geometry save
            inbox = InboxPopover(app)
            inbox.open_at(50, 50)                       # 10 ms focus grab
            inbox._on_focus_out(None)                   # 150 ms close debounce
            memory = MemoryView(root, app)
            memory.pack()
            memory.on_event("facts", FACTS)
            memory.set_filter("time")                   # 250 ms filter debounce
            routines = RoutinesView(root, app)
            routines.pack()
            routines.on_event("routines", ROUTINES)
            routines.rows[1].delete_link.invoke()       # 5 s undo window
            box = MarkdownBox(root, app)
            box.pack()
            box.set_markdown("```\nx = 1\n```")
            box.code_frames()[0].copy_link.invoke()     # 1.4 s copy feedback
            root.update()
            # Let the toolkit's own settle timers (AutoText, 90 ms) run first;
            # they belong to jarvis.ui, not to the panes under test.
            time.sleep(0.12)
            root.update()
            self.assertGreater(len(_pending_afters(root)), 0)
            root.destroy()
        self.assertEqual(_pending_afters(root), ())
        # The pending Delete was committed rather than dropped on the floor.
        self.assertEqual(app.session.named("delete_routine"), [(2,)])
        # Pump another interpreter long enough for any leaked timer to fire.
        other = tk.Tk()
        other.withdraw()
        try:
            time.sleep(0.6)
            other.update()
        finally:
            other.destroy()
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
