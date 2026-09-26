"""Tests for the JARVIS Desktop v3 composer popups (jarvis.ui_popups).

The pure parts (slash ranking, the file index) are tested directly. The
widgets are built against a hidden Tk root with a fake app that records
every call, the same pattern as tests/test_ui_panes.py, so the contract the
composer relies on (which keys the popups swallow, which callbacks fire,
what text is left in the box) is pinned by execution.
"""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

import tkinter as tk

import jarvis.ui as ui
import jarvis.ui_win as ui_win
from jarvis import ui_popups
from jarvis.ui_popups import (
    ATTACH_ENTRIES,
    DICTATION_HINT,
    SLASH_COMMANDS,
    SNIP_LAUNCH_FAILED,
    AtPopup,
    AttachMenu,
    FileEntry,
    FileIndex,
    SlashPopup,
    at_token,
    find_slash,
    is_unknown_slash,
    match_slash,
    run_snip,
    slash_token,
)


class FakeApp:
    """Exactly the surface the popups are allowed to touch."""

    def __init__(self, root: tk.Tk, theme: str = "midnight") -> None:
        self.root = root
        self.theme = ui.THEMES[theme]
        self.fonts = ui.Fonts(root)
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def px(self, value: float) -> int:
        return int(value)

    def toast(self, text: str, kind: str = "info") -> None:
        self.calls.append(("toast", (text,), {"kind": kind}))


def _labels(widget: tk.Misc) -> list[str]:
    found: list[str] = []
    stack = [widget]
    while stack:
        current = stack.pop(0)
        if isinstance(current, tk.Label):
            found.append(str(current.cget("text")))
        stack.extend(current.winfo_children())
    return found


def _joined(widget: tk.Misc) -> str:
    return "\n".join(_labels(widget))


def _names(commands: list[Any]) -> list[str]:
    return [command.name for command in commands]


# --------------------------------------------------------------------------
# Pure helpers
# --------------------------------------------------------------------------

class SlashMatchTests(unittest.TestCase):
    def test_every_handle_slash_command_is_listed_with_usage_and_description(self):
        names = {command.name for command in SLASH_COMMANDS}
        for expected in ("new", "model", "theme", "project", "task", "remember", "export", "council", "context", "settings", "help", "facts", "schedule", "inbox"):
            self.assertIn(expected, names)
        for command in SLASH_COMMANDS:
            self.assertTrue(command.usage.startswith(f"/{command.name}"), command)
            self.assertTrue(command.description, command)
        aliases = {alias: command.name for command in SLASH_COMMANDS for alias in command.aliases}
        self.assertEqual(aliases["ctx"], "context")
        self.assertEqual(aliases["prefs"], "settings")
        self.assertEqual(aliases["?"], "help")
        self.assertEqual(aliases["memory"], "facts")
        self.assertEqual(aliases["routine"], "schedule")
        self.assertEqual(aliases["routines"], "schedule")
        self.assertEqual(aliases["n"], "new")
        self.assertEqual(find_slash("schedule").usage, "/schedule <prompt>")
        self.assertEqual(find_slash("inbox").usage, "/inbox")
        self.assertEqual(find_slash("facts").usage, "/facts")

    def test_empty_query_lists_every_command_in_order(self):
        self.assertEqual(match_slash("/"), list(SLASH_COMMANDS))
        self.assertEqual(match_slash(""), list(SLASH_COMMANDS))

    def test_prefix_outranks_subsequence_and_exact_outranks_prefix(self):
        self.assertEqual(_names(match_slash("/mo"))[0], "model")
        self.assertEqual(_names(match_slash("mo"))[0], "model")
        # "se" is a prefix of settings and only a subsequence of "remember"/"schedule"... prefix first
        ranked = _names(match_slash("/se"))
        self.assertEqual(ranked[0], "settings")
        # exact name beats a longer prefix match
        ranked = _names(match_slash("/new"))
        self.assertEqual(ranked[0], "new")
        # subsequence still finds a command
        self.assertIn("remember", _names(match_slash("/rmbr")))
        self.assertEqual(_names(match_slash("/zzz")), [])

    def test_aliases_rank_like_names(self):
        self.assertEqual(_names(match_slash("/ctx"))[0], "context")
        self.assertEqual(_names(match_slash("/?"))[0], "help")
        self.assertEqual(_names(match_slash("/n"))[0], "new")
        self.assertEqual(_names(match_slash("/routi"))[0], "schedule")
        self.assertEqual(_names(match_slash("/MEM"))[0], "facts")

    def test_only_the_first_token_counts(self):
        self.assertEqual(_names(match_slash("/model fast please"))[0], "model")
        self.assertEqual(slash_token("/model fast"), "model")
        self.assertEqual(slash_token("/"), "")
        self.assertIsNone(slash_token(" /model"))
        self.assertIsNone(slash_token("hello /model"))

    def test_is_unknown_slash(self):
        self.assertTrue(is_unknown_slash("/nope"))
        self.assertTrue(is_unknown_slash("/"))
        self.assertTrue(is_unknown_slash("/mo"))
        self.assertFalse(is_unknown_slash("/model fast"))
        self.assertFalse(is_unknown_slash("/CTX"))
        self.assertFalse(is_unknown_slash("/? anything"))
        self.assertFalse(is_unknown_slash("plain prose with / inside"))
        self.assertFalse(is_unknown_slash(""))

    def test_dictation_hint_is_exported(self):
        self.assertEqual(DICTATION_HINT, "Win+H dictates into any box")


class FileIndexTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="jx-popups-")
        self.root = Path(self.tmp.name)
        for relative in (
            "jarvis/ui.py", "jarvis/ui_popups.py", "jarvis/memory.py", "tests/test_ui_popups.py",
            "docs/DESKTOP_UI.md", "assets/logo.png", "assets/model.bin", "README.md",
            ".git/config", "node_modules/pkg/index.js", "__pycache__/x.pyc", "data/store.db",
        ):
            target = self.root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("x", encoding="utf-8")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_build_skips_workspace_skip_directories_and_classifies(self):
        index = FileIndex.build(self.root)
        paths = {entry.path for entry in index.entries}
        self.assertIn("jarvis/ui.py", paths)
        self.assertIn("assets/logo.png", paths)
        self.assertNotIn(".git/config", paths)
        self.assertNotIn("node_modules/pkg/index.js", paths)
        self.assertNotIn("__pycache__/x.pyc", paths)
        self.assertNotIn("data/store.db", paths)
        self.assertFalse(index.truncated)
        kinds = {entry.path: entry.kind for entry in index.entries}
        self.assertEqual(kinds["jarvis/ui.py"], "text")
        self.assertEqual(kinds["assets/logo.png"], "image")
        self.assertEqual(kinds["assets/model.bin"], "other")
        for entry in index.entries:
            self.assertNotIn("\\", entry.path)
            self.assertGreater(entry.mtime, 0)
        self.assertEqual(index.root, str(self.root))

    def test_build_is_bounded_and_never_raises(self):
        index = FileIndex.build(self.root, max_entries=3)
        self.assertEqual(len(index.entries), 3)
        self.assertTrue(index.truncated)
        missing = FileIndex.build(self.root / "does-not-exist")
        self.assertEqual(len(missing.entries), 0)
        self.assertFalse(missing.truncated)
        self.assertEqual(len(FileIndex.build("")), 0)
        self.assertEqual(len(FileIndex.build(None)), 0)
        custom = FileIndex.build(self.root, skip={"jarvis", "tests", ".git", "node_modules", "__pycache__", "data"})
        self.assertFalse(any(entry.path.startswith("jarvis/") for entry in custom.entries))

    def test_search_ranks_basename_prefix_over_subsequence(self):
        index = FileIndex.build(self.root)
        ranked = [entry.path for entry in index.search("ui")]
        self.assertEqual(ranked[0], "jarvis/ui.py")
        self.assertIn("jarvis/ui_popups.py", ranked[:3])
        self.assertIn("tests/test_ui_popups.py", ranked)
        by_path = [entry.path for entry in index.search("tests/ui")]
        self.assertEqual(by_path[0], "tests/test_ui_popups.py")
        self.assertEqual([entry.path for entry in index.search("zzzq")], [])
        self.assertEqual(len(index.search("", limit=2)), 2)
        self.assertEqual(len(index.search("e", limit=1)), 1)
        self.assertEqual([entry.path for entry in index.search("jarvis\\mem")][0], "jarvis/memory.py")

    def test_search_is_pure_and_empty_query_lists_newest_first(self):
        newest = self.root / "README.md"
        os.utime(newest, (time.time() + 60, time.time() + 60))
        index = FileIndex.build(self.root)
        self.assertEqual(index.search("")[0].path, "README.md")
        entries = tuple(index.entries)
        index.search("ui")
        self.assertEqual(entries, index.entries)
        self.assertEqual(index.absolute(FileEntry("jarvis/ui.py", "text", 0.0, 1)), os.path.normpath(str(self.root / "jarvis" / "ui.py")))

    def test_at_token_needs_a_token_start(self):
        self.assertEqual(at_token("@"), (0, ""))
        self.assertEqual(at_token("see @ui"), (4, "ui"))
        self.assertEqual(at_token("line\n@doc"), (5, "doc"))
        self.assertIsNone(at_token("mail@host"))
        self.assertIsNone(at_token("@ui done"))
        self.assertIsNone(at_token(""))


# --------------------------------------------------------------------------
# Widgets
# --------------------------------------------------------------------------

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
        self.app = FakeApp(self.root)
        self.widgets: list[tk.Misc] = []

    def tearDown(self) -> None:
        time.sleep(0.2)
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

    def composer(self) -> ui.GrowText:
        holder = self.keep(tk.Frame(self.root))
        holder.pack()
        text = ui.GrowText(holder, self.app)
        text.pack()
        return text

    def type_text(self, text: tk.Text, value: str) -> None:
        text.delete("1.0", "end")
        text.insert("1.0", value)
        text.mark_set("insert", "end-1c")


class SlashPopupTests(TkCase):
    def test_opens_on_slash_filters_and_completes_with_tab(self):
        text = self.composer()
        chosen: list[Any] = []
        popup = self.keep(SlashPopup(self.app, text, chosen.append))
        self.assertFalse(popup.is_open)
        self.type_text(text, "/")
        popup.sync()
        self.assertTrue(popup.is_open)
        self.assertEqual(popup.state(), "normal")
        self.assertEqual(len(popup.items), len(SLASH_COMMANDS))
        self.type_text(text, "/mo")
        popup.sync()
        self.assertEqual(popup.items[0].name, "model")
        self.assertIn("/model", _joined(popup))
        self.assertIn("Switch the model profile", _joined(popup))
        self.assertNotIn("/export", _joined(popup))
        self.assertEqual(popup._on_tab(None), "break")
        self.assertEqual(text.get("1.0", "end-1c"), "/model ")
        self.assertEqual(text.index("insert"), "1.7")
        self.assertEqual([command.name for command in chosen], ["model"])
        self.assertFalse(popup.is_open)

    def test_private_bindtag_runs_before_the_composer_and_is_released(self):
        # Generated key events never reach a widget on a withdrawn root, so the
        # routing is pinned structurally: the popup's tag precedes the widget's
        # own tag (a "break" there stops the composer's <Return>/<Tab>), every
        # sequence has a class binding, and destroy() removes all of it.
        text = self.composer()
        popup = self.keep(SlashPopup(self.app, text, None))
        tags = text.bindtags()
        self.assertEqual(tags[0], popup._keys.tag)
        self.assertEqual(tags[1], str(text))
        for sequence in ("<KeyRelease>", "<ButtonRelease-1>", "<FocusOut>", "<Up>", "<Down>", "<Return>", "<KP_Enter>", "<Tab>", "<Escape>"):
            self.assertTrue(text.bind_class(popup._keys.tag, sequence), sequence)
        # while closed, every key handler defers to the composer
        self.assertIsNone(popup._on_return(None))
        self.assertIsNone(popup._on_tab(None))
        # a second popup on the same widget stacks in front without disturbing the first
        other = self.keep(SlashPopup(self.app, text, None))
        self.assertEqual(text.bindtags()[:3], (other._keys.tag, popup._keys.tag, str(text)))
        popup.destroy()
        self.widgets.remove(popup)
        self.assertNotIn(popup._keys.tag, text.bindtags())
        self.assertEqual(text.bind_class(popup._keys.tag, "<Tab>"), "")
        self.assertEqual(text.bindtags()[:2], (other._keys.tag, str(text)))

    def test_enter_on_exact_match_closes_and_lets_the_composer_send(self):
        text = self.composer()
        chosen: list[Any] = []
        popup = self.keep(SlashPopup(self.app, text, chosen.append))
        self.type_text(text, "/new")
        popup.sync()
        self.assertTrue(popup.is_open)
        self.assertIsNone(popup._on_return(None))
        self.assertFalse(popup.is_open)
        self.assertEqual(text.get("1.0", "end-1c"), "/new")
        self.assertEqual([command.name for command in chosen], ["new"])
        # alias counts as exact too
        self.type_text(text, "/ctx")
        popup.sync()
        self.assertIsNone(popup._on_return(None))
        self.assertEqual(text.get("1.0", "end-1c"), "/ctx")
        # a partial token completes instead of sending
        self.type_text(text, "/exp")
        popup.sync()
        self.assertEqual(popup._on_return(None), "break")
        self.assertEqual(text.get("1.0", "end-1c"), "/export ")
        # unknown token: pass through so the composer's guard refuses it
        self.type_text(text, "/nope")
        popup.sync()
        self.assertTrue(popup.is_open)
        self.assertIn("No command named /nope", _joined(popup))
        self.assertIsNone(popup._on_return(None))
        self.assertTrue(is_unknown_slash(text.get("1.0", "end-1c")))

    def test_arrows_move_the_selection_and_escape_closes(self):
        text = self.composer()
        popup = self.keep(SlashPopup(self.app, text, None))
        self.type_text(text, "/")
        popup.sync()
        self.assertEqual(popup.selected, 0)
        self.assertEqual(popup._on_move(1), "break")
        self.assertEqual(popup.selected, 1)
        popup._on_move(-1)
        popup._on_move(-1)
        self.assertEqual(popup.selected, len(SLASH_COMMANDS) - 1)
        self.assertEqual(popup._on_escape(None), "break")
        self.assertFalse(popup.is_open)
        # the key release after Esc must not re-open it for the same token
        popup.sync()
        self.assertFalse(popup.is_open)
        self.type_text(text, "/t")
        popup.sync()
        self.assertTrue(popup.is_open)
        # with nothing open every handler defers to the composer
        self.type_text(text, "hello")
        popup.sync()
        self.assertFalse(popup.is_open)
        self.assertIsNone(popup._on_move(1))
        self.assertIsNone(popup._on_return(None))
        self.assertIsNone(popup._on_tab(None))
        self.assertIsNone(popup._on_escape(None))

    def test_only_while_the_caret_is_in_the_first_token(self):
        text = self.composer()
        popup = self.keep(SlashPopup(self.app, text, None))
        self.type_text(text, "/model fast")
        popup.sync()
        self.assertFalse(popup.is_open)
        text.mark_set("insert", "1.3")
        popup.sync()
        self.assertTrue(popup.is_open)
        self.assertEqual(popup.token(), "model")
        self.type_text(text, "/mo\nsecond line")
        popup.sync()
        self.assertFalse(popup.is_open)
        self.type_text(text, "not /a command")
        popup.sync()
        self.assertFalse(popup.is_open)

    def test_focus_out_closes_after_the_debounce(self):
        text = self.composer()
        popup = self.keep(SlashPopup(self.app, text, None))
        self.type_text(text, "/")
        popup.sync()
        self.assertTrue(popup.is_open)
        popup._on_focus_out(None)
        self.assertTrue(popup.is_open)
        time.sleep(0.25)
        self.pump()
        self.assertFalse(popup.is_open)

    def test_click_on_a_row_completes(self):
        text = self.composer()
        popup = self.keep(SlashPopup(self.app, text, None))
        self.type_text(text, "/")
        popup.sync()
        popup.choose(1)
        self.assertEqual(text.get("1.0", "end-1c"), f"/{SLASH_COMMANDS[1].name} ")


class AtPopupTests(TkCase):
    def setUp(self) -> None:
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory(prefix="jx-at-")
        root = Path(self.tmp.name)
        for relative in ("jarvis/ui.py", "jarvis/ui_popups.py", "docs/DESKTOP_UI.md", "assets/logo.png"):
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("x", encoding="utf-8")
        self.index = FileIndex.build(root)

    def tearDown(self) -> None:
        super().tearDown()
        self.tmp.cleanup()

    def test_lists_matches_with_kind_glyphs_and_chooses(self):
        text = self.composer()
        chosen: list[str] = []
        popup = self.keep(AtPopup(self.app, text, lambda: self.index, chosen.append))
        self.type_text(text, "look at @ui")
        popup.sync()
        self.assertTrue(popup.is_open)
        self.assertEqual(popup.items[0].path, "jarvis/ui.py")
        joined = _joined(popup)
        self.assertIn("jarvis/ui.py", joined)
        self.assertIn(ui_popups.KIND_GLYPHS["text"], joined)
        self.assertNotIn("logo.png", joined)
        self.assertEqual(popup._on_return(None), "break")
        self.assertEqual(chosen, [self.index.absolute(popup_entry) for popup_entry in [FileEntry("jarvis/ui.py", "text", 0, 0)]])
        self.assertFalse(popup.is_open)
        # the composer removes the token through the popup's helper
        popup.remove_token()
        self.assertEqual(text.get("1.0", "end-1c"), "look at ")

    def test_tab_chooses_and_escape_dismisses(self):
        text = self.composer()
        chosen: list[str] = []
        popup = self.keep(AtPopup(self.app, text, lambda: self.index, chosen.append))
        self.type_text(text, "@log")
        popup.sync()
        self.assertTrue(popup.is_open)
        self.assertIn(ui_popups.KIND_GLYPHS["image"], _joined(popup))
        self.assertEqual(popup._on_tab(None), "break")
        self.assertTrue(chosen[0].endswith("logo.png"))
        self.type_text(text, "@doc")
        popup.sync()
        self.assertTrue(popup.is_open)
        self.assertEqual(popup._on_escape(None), "break")
        self.assertFalse(popup.is_open)
        popup.sync()
        self.assertFalse(popup.is_open)

    def test_only_at_a_token_start_and_honest_when_the_index_is_missing(self):
        text = self.composer()
        popup = self.keep(AtPopup(self.app, text, lambda: None, None))
        self.type_text(text, "mail@example")
        popup.sync()
        self.assertFalse(popup.is_open)
        self.type_text(text, "@")
        popup.sync()
        self.assertTrue(popup.is_open)
        self.assertIn("Workspace index not ready", _joined(popup))
        self.assertIsNone(popup._on_return(None))
        popup.index_provider = lambda: self.index
        self.type_text(text, "@zzzz")
        popup.sync()
        self.assertIn("No workspace file matches", _joined(popup))
        self.assertEqual(popup.items, [])

    def test_private_bindtag_and_key_release_sync(self):
        text = self.composer()
        chosen: list[str] = []
        popup = self.keep(AtPopup(self.app, text, lambda: self.index, chosen.append))
        self.assertEqual(text.bindtags()[0], popup._keys.tag)
        for sequence in ("<KeyRelease>", "<FocusOut>", "<Up>", "<Down>", "<Return>", "<Tab>", "<Escape>"):
            self.assertTrue(text.bind_class(popup._keys.tag, sequence), sequence)
        self.type_text(text, "@ui_p")
        popup._on_key_release(None)  # what the <KeyRelease> class binding calls
        self.assertTrue(popup.is_open)
        self.assertEqual(popup._on_move(1), "break")
        self.assertEqual(popup._on_return(None), "break")
        self.assertTrue(chosen and chosen[0].endswith("ui_popups.py"))
        self.assertFalse(popup.is_open)
        # the token is still in the box (the composer removes it); it must not reopen
        popup._on_key_release(None)
        self.assertFalse(popup.is_open)
        self.type_text(text, "@ui_po")
        popup._on_key_release(None)
        self.assertTrue(popup.is_open)
        popup.destroy()
        self.widgets.remove(popup)
        self.assertNotIn(popup._keys.tag, text.bindtags())

    def test_focus_out_closes_after_the_debounce(self):
        text = self.composer()
        popup = self.keep(AtPopup(self.app, text, lambda: self.index, None))
        self.type_text(text, "@")
        popup.sync()
        popup._on_focus_out(None)
        time.sleep(0.25)
        self.pump()
        self.assertFalse(popup.is_open)


class AttachMenuTests(TkCase):
    def test_shows_three_entries_and_calls_the_right_callback(self):
        anchor = self.keep(tk.Label(self.root, text="+"))
        anchor.pack()
        called: list[str] = []
        menu = self.keep(AttachMenu(self.app, anchor, {
            "files": lambda: called.append("files"),
            "snip": lambda: called.append("snip"),
            "paste": lambda: called.append("paste"),
        }))
        self.pump()
        labels = _labels(menu)
        self.assertIn("Files…", labels)
        self.assertIn("Snip screen", labels)
        self.assertIn("Paste", labels)
        self.assertEqual(len(menu.rows), 3)
        self.assertEqual([entry[0] for entry in ATTACH_ENTRIES], ["files", "snip", "paste"])
        menu.move(1)
        self.assertEqual(menu.selected, 1)
        menu.activate()
        self.assertEqual(called, ["snip"])
        self.assertTrue(menu.closed)
        self.widgets.remove(menu)

    def test_choose_by_key_and_missing_callbacks_are_not_listed(self):
        anchor = self.keep(tk.Label(self.root, text="+"))
        anchor.pack()
        called: list[str] = []
        menu = self.keep(AttachMenu(self.app, anchor, {"files": lambda: called.append("files"), "paste": lambda: called.append("paste")}))
        self.assertEqual(len(menu.rows), 2)
        self.assertNotIn("Snip screen", _labels(menu))
        menu.choose("paste")
        self.assertEqual(called, ["paste"])
        self.widgets.remove(menu)

    def test_escape_closes_without_calling_anything(self):
        anchor = self.keep(tk.Label(self.root, text="+"))
        anchor.pack()
        called: list[str] = []
        menu = self.keep(AttachMenu(self.app, anchor, {"files": lambda: called.append("files")}))
        for sequence in ("<Escape>", "<Return>", "<Up>", "<Down>", "<FocusOut>"):
            self.assertTrue(menu.bind(sequence), sequence)
        self.assertFalse(menu.closed)
        # while the menu owns focus, a FocusOut/FocusIn bounce keeps it open
        menu._on_focus_out(None)
        time.sleep(0.25)
        self.pump()
        self.assertFalse(menu.closed)
        # focus gone to another window: the debounce closes it without calling anything
        with mock.patch.object(menu, "focus_get", return_value=None):
            menu._on_focus_out(None)
            time.sleep(0.25)
            self.pump()
        self.assertTrue(menu.closed)
        self.assertEqual(called, [])
        menu.close()  # idempotent
        self.widgets.remove(menu)


class RunSnipTests(TkCase):
    def wait_for(self, done: list[Any], seconds: float = 3.0) -> None:
        deadline = time.monotonic() + seconds
        while not done and time.monotonic() < deadline:
            self.root.update()
            time.sleep(0.01)

    def test_launch_failure_reports_without_polling(self):
        received: list[bytes] = []
        failures: list[str] = []
        with mock.patch.object(ui_win, "launch_snip", return_value=False), \
                mock.patch.object(ui_win, "clipboard_sequence", return_value=5):
            started = run_snip(self.app, received.append, failures.append, timeout_s=0.5, poll_ms=10)
        self.assertFalse(started)
        self.assertEqual(failures, [SNIP_LAUNCH_FAILED])
        time.sleep(0.05)
        self.pump()
        self.assertEqual(received, [])

    def test_png_arrives_after_the_sequence_changes(self):
        received: list[bytes] = []
        failures: list[str] = []
        sequence = {"value": 5}
        clipboard = {"png": None}
        with mock.patch.object(ui_win, "launch_snip", return_value=True), \
                mock.patch.object(ui_win, "clipboard_sequence", side_effect=lambda: sequence["value"]), \
                mock.patch.object(ui, "clipboard_image_png", side_effect=lambda: clipboard["png"]):
            started = run_snip(self.app, received.append, failures.append, timeout_s=5, poll_ms=10)
            self.assertTrue(started)
            time.sleep(0.05)
            self.pump()
            self.assertEqual(received, [])
            # text copied: the sequence moves but no image is there yet
            sequence["value"] = 6
            time.sleep(0.05)
            self.pump()
            self.assertEqual(received, [])
            sequence["value"] = 7
            clipboard["png"] = b"\x89PNG-fake"
            self.wait_for(received)
        self.assertEqual(received, [b"\x89PNG-fake"])
        self.assertEqual(failures, [])

    def test_timeout_reports_failure_once(self):
        received: list[bytes] = []
        failures: list[str] = []
        with mock.patch.object(ui_win, "launch_snip", return_value=True), \
                mock.patch.object(ui_win, "clipboard_sequence", return_value=5), \
                mock.patch.object(ui, "clipboard_image_png", return_value=b"stale"):
            run_snip(self.app, received.append, failures.append, timeout_s=0.05, poll_ms=10)
            self.wait_for(failures)
            time.sleep(0.05)
            self.pump()
        self.assertEqual(received, [])
        self.assertEqual(len(failures), 1)
        self.assertTrue(failures[0].startswith("No screenshot arrived"))


if __name__ == "__main__":
    unittest.main()
