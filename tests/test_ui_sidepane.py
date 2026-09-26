"""Tests for the JARVIS Desktop v3 side pane (Diff · Artifact · File).

The engine half (``WorkspaceIndex``, ``diff_workspace``, ``DiffReport``,
``artifact_candidates``) is exercised on real temporary workspaces and
checked against ``difflib`` directly.  The Tk half builds the real widgets on
a hidden root against a fake app that records every call, so the contract
with ``jarvis.ui`` (which app methods are called, with what) is pinned by
execution.
"""

from __future__ import annotations

import difflib
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

import tkinter as tk

import jarvis.ui as ui
from jarvis.ui_sidepane import (
    ARTIFACT_CODE_LINES,
    ARTIFACT_REPLY_CHARS,
    EMPTY_DIFF_TEXT,
    MAX_DIFF_TEXT_BYTES,
    REVERT_SENTENCE,
    SCRIPT_NOTICE,
    Artifact,
    DiffChip,
    DiffReport,
    SidePane,
    WorkspaceIndex,
    artifact_candidates,
    diff_workspace,
    unified_diff_text,
)


def _write(root: Path, relative: str, text: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(text)
    return path


def _expected_diff(relative: str, before: str | None, after: str | None) -> tuple[str, int, int]:
    old = before.splitlines(keepends=True) if before is not None else []
    new = after.splitlines(keepends=True) if after is not None else []
    lines = list(difflib.unified_diff(old, new, fromfile=f"a/{relative}", tofile=f"b/{relative}", n=3))
    added = sum(1 for line in lines[2:] if line.startswith("+"))
    removed = sum(1 for line in lines[2:] if line.startswith("-"))
    return "".join(line if line.endswith("\n") else line + "\n" for line in lines), added, removed


class WorkspaceCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="jx-sidepane-")
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()


class SnapshotTests(WorkspaceCase):
    def test_snapshot_indexes_text_and_skips_binaries_large_and_unreadable(self):
        _write(self.root, "src/app.py", "print('hi')\n")
        _write(self.root, "notes.md", "# notes\n")
        (self.root / "blob.bin").write_bytes(b"PNG\x00\x01\x02")
        (self.root / "big.txt").write_bytes(b"x" * 2_001)
        _write(self.root, "locked.txt", "secret\n")
        _write(self.root, ".git/config", "[core]\n")
        _write(self.root, "node_modules/pkg/index.js", "module.exports = 1\n")
        original = Path.read_bytes

        def read_bytes(path: Path) -> bytes:
            if path.name == "locked.txt":
                raise PermissionError("locked")
            return original(path)

        with mock.patch.object(Path, "read_bytes", read_bytes):
            index = WorkspaceIndex.snapshot(self.root, max_bytes=2_000)
        self.assertIsNone(index.unavailable)
        self.assertEqual(sorted(index.files), ["notes.md", "src/app.py"])
        self.assertEqual(index.skipped, 3)
        entry = index.files["src/app.py"]
        self.assertEqual(entry.content, "print('hi')\n")
        self.assertEqual(entry.size, len("print('hi')\n"))
        self.assertEqual(len(entry.sha1), 40)
        self.assertEqual(index.path_for("src/app.py"), str(self.root / "src" / "app.py"))

    def test_snapshot_of_missing_root_is_empty_not_an_error(self):
        index = WorkspaceIndex.snapshot(self.root / "nope")
        self.assertEqual(index.files, {})
        self.assertIsNone(index.unavailable)

    def test_snapshot_honours_max_files_and_does_not_partially_index(self):
        for number in range(5_001):
            (self.root / f"f{number:04d}.txt").write_bytes(b"x\n")
        index = WorkspaceIndex.snapshot(self.root)
        self.assertEqual(index.unavailable, "workspace too large")
        self.assertEqual(index.files, {})
        os.remove(self.root / "f5000.txt")
        index = WorkspaceIndex.snapshot(self.root)
        self.assertIsNone(index.unavailable)
        self.assertEqual(len(index.files), 5_000)
        report = diff_workspace(WorkspaceIndex(root=str(self.root), unavailable="workspace too large"), self.root)
        self.assertEqual(report.unavailable, "workspace too large")
        self.assertEqual(report.to_record(), "unavailable: workspace too large")
        self.assertEqual(report.summary, "unavailable: workspace too large")


class DiffTests(WorkspaceCase):
    def test_diff_kinds_and_line_counts_match_difflib(self):
        before_a = "one\ntwo\nthree\nfour\n"
        after_a = "one\n2\nthree\nfour\nfive\n"
        _write(self.root, "a.txt", before_a)
        _write(self.root, "gone.txt", "bye\nnow\n")
        _write(self.root, "same.txt", "unchanged\n")
        index = WorkspaceIndex.snapshot(self.root)
        _write(self.root, "a.txt", after_a)
        os.remove(self.root / "gone.txt")
        _write(self.root, "sub/new.py", "x = 1\ny = 2\n")
        report = diff_workspace(index, self.root)
        self.assertIsNone(report.unavailable)
        self.assertFalse(report.truncated)
        self.assertEqual([entry.relative for entry in report.entries], ["a.txt", "gone.txt", "sub/new.py"])
        self.assertEqual([entry.kind for entry in report.entries], ["modified", "deleted", "added"])
        expected = {
            "a.txt": _expected_diff("a.txt", before_a, after_a),
            "gone.txt": _expected_diff("gone.txt", "bye\nnow\n", None),
            "sub/new.py": _expected_diff("sub/new.py", None, "x = 1\ny = 2\n"),
        }
        for entry in report.entries:
            text, added, removed = expected[entry.relative]
            self.assertEqual(entry.unified, text, entry.relative)
            self.assertEqual((entry.added_lines, entry.removed_lines), (added, removed), entry.relative)
            self.assertEqual(entry.path, str(self.root / Path(entry.relative)))
        # a.txt: -two +2 +five; gone.txt: -bye -now; sub/new.py: +x +y.
        self.assertEqual(report.summary, "+4 −3 · 3 files")
        self.assertEqual(report.chip_text, "+4 −3")
        self.assertEqual(report.unified_text, "".join(expected[e.relative][0] for e in report.entries))

    def test_no_changes_is_an_empty_report(self):
        _write(self.root, "a.txt", "same\n")
        index = WorkspaceIndex.snapshot(self.root)
        report = diff_workspace(index, self.root)
        self.assertEqual(report.entries, [])
        self.assertEqual(report.summary, "no changes")
        self.assertEqual(report.chip_text, "no changes")
        self.assertEqual(report.to_record()["files"], [])

    def test_unified_text_over_the_cap_is_dropped_and_flagged(self):
        _write(self.root, "small.txt", "a\n")
        _write(self.root, "huge.txt", "start\n")
        index = WorkspaceIndex.snapshot(self.root)
        _write(self.root, "small.txt", "a\nb\n")
        big_lines = "\n".join(f"line {n} " + "x" * 60 for n in range(4_000)) + "\n"
        self.assertGreater(len(big_lines.encode()), MAX_DIFF_TEXT_BYTES)
        _write(self.root, "huge.txt", "start\n" + big_lines)
        report = diff_workspace(index, self.root)
        by_name = {entry.relative: entry for entry in report.entries}
        self.assertTrue(report.truncated)
        self.assertTrue(by_name["huge.txt"].truncated)
        self.assertEqual(by_name["huge.txt"].unified, "")
        self.assertEqual(by_name["huge.txt"].added_lines, 4_000)
        self.assertFalse(by_name["small.txt"].truncated)
        self.assertIn("+b\n", by_name["small.txt"].unified)
        self.assertLessEqual(len(report.unified_text.encode()), MAX_DIFF_TEXT_BYTES)

    def test_record_round_trip_is_json_safe_and_tolerant(self):
        _write(self.root, "a.txt", "1\n2\n")
        index = WorkspaceIndex.snapshot(self.root)
        _write(self.root, "a.txt", "1\n3\n4\n")
        report = diff_workspace(index, self.root)
        record = report.to_record()
        self.assertIsInstance(record, dict)
        self.assertEqual(record["version"], 1)
        self.assertEqual(record["summary"], "+2 −1 · 1 file")
        self.assertEqual((record["added"], record["removed"], record["files_changed"]), (2, 1, 1))
        self.assertEqual(sorted(record["files"][0]), ["added_lines", "kind", "path", "relative", "removed_lines", "truncated", "unified"])
        rebuilt = DiffReport.from_record(json.loads(json.dumps(record)))
        self.assertEqual(rebuilt.root, report.root)
        self.assertEqual([entry.to_record() for entry in rebuilt.entries], [entry.to_record() for entry in report.entries])
        self.assertEqual(rebuilt.summary, report.summary)
        self.assertEqual(DiffReport.from_record("unavailable: workspace too large").unavailable, "workspace too large")
        self.assertEqual(DiffReport.from_record(None).entries, [])
        self.assertEqual(DiffReport.from_record("garbage").entries, [])
        self.assertEqual(DiffReport.from_record({"files": "no"}).entries, [])
        odd = DiffReport.from_record({"files": [{"relative": "x", "kind": "weird", "added_lines": "3", "removed_lines": None, "unified": 7}]})
        self.assertEqual(odd.entries[0].kind, "modified")
        self.assertEqual((odd.entries[0].added_lines, odd.entries[0].removed_lines), (3, 0))
        self.assertEqual(odd.entries[0].unified, "")
        bloated = DiffReport.from_record({"files": [{"relative": "big", "kind": "added", "unified": "x" * (MAX_DIFF_TEXT_BYTES + 1)}]})
        self.assertTrue(bloated.entries[0].truncated)
        self.assertEqual(bloated.entries[0].unified, "")

    def test_unified_diff_text_handles_missing_trailing_newline(self):
        # difflib emits the last line without "\n" when the input lacks one;
        # the stored text still ends every line so the view and cap are exact.
        text, added, removed = unified_diff_text("f", "a", "b")
        self.assertEqual(text, "--- a/f\n+++ b/f\n@@ -1 +1 @@\n-a\n+b\n")
        self.assertEqual((added, removed), (1, 1))
        self.assertEqual(unified_diff_text("f", "same\n", "same\n"), ("", 0, 0))


class ArtifactTests(unittest.TestCase):
    def test_fence_thresholds(self):
        exactly = "```python\n" + "\n".join(f"x{n} = {n}" for n in range(ARTIFACT_CODE_LINES)) + "\n```\n"
        self.assertEqual(artifact_candidates(exactly), [])
        over = "Here:\n\n```python\n" + "\n".join(f"x{n} = {n}" for n in range(ARTIFACT_CODE_LINES + 1)) + "\n```\nDone."
        found = artifact_candidates(over)
        self.assertEqual(len(found), 1)
        artifact = found[0]
        self.assertEqual((artifact.kind, artifact.language), ("code", "python"))
        self.assertEqual(artifact.line_count, ARTIFACT_CODE_LINES + 1)
        self.assertEqual(artifact.title, "Python snippet")
        self.assertEqual(artifact.suggested_extension(), ".py")
        self.assertEqual(artifact.suggested_filename(), "Python_snippet.py")
        self.assertTrue(artifact.text.startswith("x0 = 0\nx1 = 1"))

    def test_filename_comment_becomes_the_title(self):
        body = "# tools/build.py\n" + "\n".join("pass" for _ in range(ARTIFACT_CODE_LINES + 5))
        artifact = artifact_candidates(f"```py\n{body}\n```")[0]
        self.assertEqual(artifact.title, "tools/build.py")
        self.assertEqual(artifact.suggested_filename(), "toolsbuild.py")
        self.assertEqual(artifact.language_name, "Python")

    def test_reply_length_threshold(self):
        short = "word " * (ARTIFACT_REPLY_CHARS // 5)
        self.assertLessEqual(len(short), ARTIFACT_REPLY_CHARS)
        self.assertEqual(artifact_candidates(short), [])
        long_reply = "## Plan\n\n" + "word " * (ARTIFACT_REPLY_CHARS // 5 + 10)
        found = artifact_candidates(long_reply)
        self.assertEqual(len(found), 1)
        self.assertEqual((found[0].kind, found[0].language, found[0].title), ("text", "markdown", "Plan"))
        self.assertEqual(found[0].text, long_reply)
        self.assertEqual(found[0].suggested_extension(), ".md")
        both = long_reply + "\n```sh\n" + "\n".join("echo" for _ in range(70)) + "\n```\n"
        kinds = [item.kind for item in artifact_candidates(both)]
        self.assertEqual(kinds, ["code", "text"])
        self.assertEqual(artifact_candidates(""), [])
        self.assertEqual(artifact_candidates(None), [])


# --------------------------------------------------------------------------
# Tk half
# --------------------------------------------------------------------------

class FakeComposer:
    def __init__(self, app: "FakeApp") -> None:
        self.app = app

    def add_files(self, paths: list[str]) -> None:
        self.app._record("add_files", list(paths))


class FakeApp:
    """Exactly the surface the side pane is allowed to touch."""

    def __init__(self, root: tk.Tk, theme: str = "midnight") -> None:
        self.root = root
        self.theme = ui.THEMES[theme]
        self.fonts = ui.Fonts(root)
        self.composer = FakeComposer(self)
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def px(self, value: float) -> int:
        return int(value)

    def _record(self, name: str, *args: Any, **kwargs: Any) -> None:
        self.calls.append((name, args, kwargs))

    def named(self, name: str) -> list[tuple[tuple[Any, ...], dict[str, Any]]]:
        return [(args, kwargs) for called, args, kwargs in self.calls if called == name]

    def toast(self, text: str, kind: str = "info") -> None:
        self._record("toast", text, kind=kind)

    def copy_text(self, text: str, quiet: bool = False) -> None:
        self._record("copy_text", text)

    def send_text(self, text: str) -> None:
        self._record("send_text", text)

    def quote_text(self, text: str) -> None:
        self._record("quote_text", text)

    def reveal_path(self, path: str) -> None:
        self._record("reveal_path", path)

    def open_path(self, path: str) -> None:
        self._record("open_path", path)

    def focus_composer(self) -> None:
        self._record("focus_composer")


def _labels(widget: tk.Misc) -> list[str]:
    found: list[str] = []
    stack = [widget]
    while stack:
        current = stack.pop(0)
        if isinstance(current, tk.Label):
            found.append(str(current.cget("text")))
        elif isinstance(current, ui.RoundButton):
            found.append(current.text)
        stack.extend(current.winfo_children())
    return found


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
        self._tmp = tempfile.TemporaryDirectory(prefix="jx-sidepane-tk-")
        self.workspace = Path(self._tmp.name)
        self.widgets: list[tk.Misc] = []

    def tearDown(self) -> None:
        time.sleep(0.12)
        self.root.update()
        for widget in self.widgets:
            try:
                widget.destroy()
            except tk.TclError:
                pass
        self.root.update()
        self._tmp.cleanup()

    def pane(self) -> SidePane:
        pane = SidePane(self.root, self.app)
        self.widgets.append(pane)
        return pane

    def sample_report(self) -> DiffReport:
        _write(self.workspace, "a.txt", "one\ntwo\nthree\n")
        _write(self.workspace, "gone.txt", "bye\n")
        index = WorkspaceIndex.snapshot(self.workspace)
        _write(self.workspace, "a.txt", "one\n2\nthree\nfour\n")
        os.remove(self.workspace / "gone.txt")
        _write(self.workspace, "new.py", "x = 1\n")
        return diff_workspace(index, self.workspace)


class SidePaneDiffTests(TkCase):
    def test_pane_visibility_and_tabs(self):
        pane = self.pane()
        self.assertFalse(pane.visible)
        pane.show("artifact")
        self.assertTrue(pane.visible)
        self.assertEqual(pane.tab, "artifact")
        self.assertEqual(pane.winfo_manager(), "pack")
        pane.toggle("artifact")
        self.assertFalse(pane.visible)
        pane.toggle()
        self.assertTrue(pane.visible)
        pane.toggle("file")
        self.assertTrue(pane.visible)
        self.assertEqual(pane.tab, "file")
        self.assertEqual(pane.tab_buttons["file"].kind, "active")
        self.assertEqual(pane.tab_buttons["diff"].kind, "subtle")
        self.assertEqual(sorted(pane.TAB_LABELS.values()), ["Artifact", "Diff", "File"])
        pane.hide()
        self.assertFalse(pane.visible)
        self.assertEqual(pane.winfo_manager(), "")
        self.assertGreaterEqual(int(pane.cget("width")), 360)

    def test_empty_state_and_unavailable_state(self):
        pane = self.pane()
        self.assertEqual(pane.hunk_view.plain_text(), EMPTY_DIFF_TEXT)
        self.assertEqual(pane.diff_summary.cget("text"), "No changes")
        self.assertFalse(pane.revert_button.enabled)
        pane.set_report("unavailable: workspace too large")
        self.assertIn("workspace too large", pane.hunk_view.plain_text())
        self.assertEqual(pane.diff_summary.cget("text"), "Changes not recorded")

    def test_report_renders_rows_and_selecting_a_file_tags_lines(self):
        pane = self.pane()
        report = self.sample_report()
        pane.set_report(report)
        self.root.update()
        self.assertEqual(pane.diff_summary.cget("text"), report.summary)
        self.assertEqual(len(pane._rows), 3)
        names = _labels(pane.file_list.inner)
        self.assertIn("a.txt", names)
        self.assertIn("gone.txt", names)
        self.assertIn("new.py", names)
        self.assertIn("+2 −1", names)
        self.assertEqual(pane.selected, 0)
        text = pane.hunk_view.text
        self.assertEqual(text.get("1.0", "end-1c") + "\n", report.entries[0].unified)
        self.assertTrue(text.tag_ranges("add"))
        self.assertTrue(text.tag_ranges("del"))
        self.assertTrue(text.tag_ranges("hunk"))
        self.assertTrue(text.tag_ranges("meta"))
        added_line = text.get(text.tag_ranges("add")[0], text.tag_ranges("add")[1])
        self.assertTrue(added_line.startswith("+"))
        pane.select(2)
        self.assertEqual(pane.selected_entry.relative, "new.py")
        self.assertIn("+x = 1", text.get("1.0", "end-1c"))
        self.assertFalse(text.tag_ranges("del"))
        self.assertEqual(str(text.cget("state")), "disabled")
        self.assertEqual(pane._rows[2].cget("bg"), self.app.theme.selection)
        self.assertEqual(pane._rows[0].cget("bg"), self.app.theme.surface)
        # A record round-trip renders identically.
        pane.set_report(DiffReport.from_record(json.loads(json.dumps(report.to_record()))))
        self.assertEqual(text.get("1.0", "end-1c") + "\n", report.entries[0].unified)

    def test_copy_diff_copies_the_whole_unified_text(self):
        pane = self.pane()
        report = self.sample_report()
        pane.set_report(report)
        pane.select(1)
        pane.copy_diff()
        self.assertEqual(self.app.named("copy_text"), [((report.unified_text,), {})])
        pane.set_report(DiffReport())
        pane.copy_diff()
        self.assertEqual(len(self.app.named("copy_text")), 1)
        self.assertEqual(self.app.named("toast")[-1][0][0], "No diff to copy.")

    def test_revert_asks_jarvis_with_the_exact_sentence(self):
        pane = self.pane()
        report = self.sample_report()
        pane.set_report(report)
        pane.select(0)
        pane.revert_button.invoke()
        self.assertEqual(
            self.app.named("send_text"),
            [(("Revert the file a.txt to how it was before your last change, and explain what you changed.",), {})],
        )
        self.assertEqual(REVERT_SENTENCE.format(relative="a.txt"), self.app.named("send_text")[0][0][0])
        before = (self.workspace / "a.txt").read_text(encoding="utf-8")
        self.assertEqual(before, "one\n2\nthree\nfour\n")

    def test_reveal_uses_parent_for_deleted_files(self):
        pane = self.pane()
        pane.set_report(self.sample_report())
        pane.select(0)
        pane.reveal_selected()
        pane.select(1)
        pane.reveal_selected()
        calls = [args[0] for args, _kw in self.app.named("reveal_path")]
        self.assertEqual(calls, [str(self.workspace / "a.txt"), str(self.workspace)])

    def test_f3_steps_hunks_then_files_and_escape_hides(self):
        pane = self.pane()
        pane.set_report(self.sample_report())
        pane.show("diff")
        self.assertEqual(pane.selected, 0)
        pane.next_hunk()
        self.assertTrue(pane.hunk_view.text.tag_ranges("hunk-current"))
        self.assertEqual(pane.selected, 0)
        pane.next_hunk()
        self.assertEqual(pane.selected, 1)
        pane.next_hunk()
        self.assertEqual(pane.selected, 2)
        pane.next_hunk()
        self.assertEqual(pane.selected, 0)
        self.assertEqual(pane._on_f3(), "break")
        self.assertTrue(pane.visible)
        self.assertEqual(pane._on_escape(), "break")
        self.assertFalse(pane.visible)

    def test_truncated_entry_shows_a_notice_instead_of_text(self):
        pane = self.pane()
        report = DiffReport.from_record({"files": [{"relative": "big.txt", "kind": "modified", "added_lines": 9, "removed_lines": 1, "unified": "", "truncated": True}]})
        pane.set_report(report)
        self.assertIn("200 KB", pane.hunk_view.plain_text())
        self.assertIn("diff text capped", pane.diff_summary.cget("text"))
        self.assertFalse(pane.copy_diff_button.enabled)


class SidePaneArtifactTests(TkCase):
    def test_artifact_renders_with_gutter_and_copy(self):
        pane = self.pane()
        text = "\n".join(f"line {n}" for n in range(1, 101))
        artifact = Artifact(kind="code", language="python", text=text, title="demo.py")
        pane.show_artifact(artifact)
        self.assertTrue(pane.visible)
        self.assertEqual(pane.tab, "artifact")
        self.assertEqual(pane.artifact_title.cget("text"), "demo.py")
        self.assertEqual(pane.artifact_chip.cget("text"), "Python")
        self.assertIn("100 lines", pane.artifact_meta.cget("text"))
        self.assertEqual(pane.artifact_view.plain_text(), text)
        gutter = pane.artifact_view.gutter.get("1.0", "end-1c").split("\n")
        self.assertEqual(len(gutter), 100)
        self.assertEqual(gutter[0].strip(), "1")
        self.assertEqual(gutter[-1].strip(), "100")
        self.assertEqual(str(pane.artifact_view.text.cget("state")), "disabled")
        pane.copy_artifact()
        self.assertEqual(self.app.named("copy_text"), [((text,), {})])

    def test_save_as_writes_the_artifact_text(self):
        pane = self.pane()
        text = "def f():\n    return 1\n"
        artifact = Artifact(kind="code", language="python", text=text, title="f.py")
        pane.show_artifact(artifact)
        target = self.workspace / "saved.py"
        with mock.patch("jarvis.ui_sidepane.filedialog.asksaveasfilename", return_value=str(target)) as ask:
            self.assertEqual(pane.save_artifact(), str(target))
        self.assertEqual(ask.call_count, 1)
        self.assertEqual(ask.call_args.kwargs["defaultextension"], ".py")
        self.assertEqual(ask.call_args.kwargs["initialfile"], "f.py")
        self.assertEqual(target.read_bytes(), text.encode("utf-8"))
        self.assertEqual(self.app.named("toast")[-1][0][0], "Saved saved.py.")
        with mock.patch("jarvis.ui_sidepane.filedialog.asksaveasfilename", return_value=""):
            self.assertIsNone(pane.save_artifact())
        self.assertEqual(len(list(self.workspace.iterdir())), 1)

    def test_quote_selection_needs_a_selection(self):
        pane = self.pane()
        pane.show_artifact(Artifact(kind="text", language="markdown", text="alpha\nbeta\ngamma\n", title="Full reply"))
        pane.quote_selection()
        self.assertEqual(self.app.named("quote_text"), [])
        self.assertIn("Select some text", self.app.named("toast")[-1][0][0])
        pane.artifact_view.text.tag_add("sel", "2.0", "3.0")
        pane.quote_selection()
        self.assertEqual(self.app.named("quote_text"), [(("beta\n",), {})])


class SidePaneFileTests(TkCase):
    def test_text_file_shows_with_open_attach_reveal_and_find(self):
        pane = self.pane()
        path = _write(self.workspace, "readme.md", "# Title\n\nfind me here\nand find me again\n")
        pane.show_file(path)
        self.assertEqual(pane.tab, "file")
        self.assertEqual(pane.file_title.cget("text"), "readme.md")
        self.assertEqual(pane.file_pathline.cget("text"), str(path))
        self.assertIn("4 lines", pane.file_meta.cget("text"))
        self.assertEqual(pane.file_view.plain_text(), "# Title\n\nfind me here\nand find me again\n")
        self.assertEqual(pane.file_notice.winfo_manager(), "")
        self.assertEqual(pane.file_open_button.winfo_manager(), "pack")
        pane.file_open_button.invoke()
        self.assertEqual(self.app.named("open_path"), [((str(path),), {})])
        pane.attach_button.invoke()
        self.assertEqual(self.app.named("add_files"), [(([str(path)],), {})])
        pane.file_reveal_button.invoke()
        self.assertEqual(self.app.named("reveal_path"), [((str(path),), {})])
        pane.open_find()
        self.assertEqual(pane.find_row.winfo_manager(), "pack")
        pane.find_row.entry.insert(0, "find me")
        pane.find_row.search()
        self.assertEqual(len(pane.find_row.matches), 2)
        self.assertEqual(pane.find_row.count.cget("text"), "1 of 2")
        pane._on_f3()
        self.assertEqual(pane.find_row.count.cget("text"), "2 of 2")
        pane.find_row.close()
        self.assertEqual(pane.find_row.winfo_manager(), "")
        self.assertFalse(pane.file_view.text.tag_ranges("find"))

    def test_bat_file_is_read_only_and_only_revealed(self):
        pane = self.pane()
        path = _write(self.workspace, "run.bat", "@echo off\r\necho hi\r\n")
        pane.show_file(path)
        self.assertEqual(pane.file_notice.winfo_manager(), "pack")
        self.assertTrue(pane.file_notice.cget("text").startswith(SCRIPT_NOTICE))
        self.assertEqual(pane.file_open_button.winfo_manager(), "")
        self.assertEqual(pane.file_reveal_button.winfo_manager(), "pack")
        self.assertTrue(pane.file_reveal_button.enabled)
        self.assertEqual(str(pane.file_view.text.cget("state")), "disabled")
        self.assertIn("echo hi", pane.file_view.plain_text())
        pane.open_file()
        self.assertEqual(self.app.named("open_path"), [])
        self.assertIn("never opened", self.app.named("toast")[-1][0][0])
        pane.file_reveal_button.invoke()
        self.assertEqual(self.app.named("reveal_path"), [((str(path),), {})])
        # A plain text file afterwards gets its Open button back.
        plain = _write(self.workspace, "notes.txt", "ok\n")
        pane.show_file(plain)
        self.assertEqual(pane.file_open_button.winfo_manager(), "pack")
        self.assertEqual(pane.file_notice.winfo_manager(), "")

    def test_binary_missing_and_large_files_are_bounded(self):
        pane = self.pane()
        binary = self.workspace / "blob.bin"
        binary.write_bytes(b"\x00\x01\x02")
        pane.show_file(binary)
        self.assertIn("Binary file", pane.file_notice.cget("text"))
        self.assertEqual(pane.file_view.plain_text(), "")
        self.assertFalse(pane.attach_button.enabled)
        pane.show_file(self.workspace / "missing.txt")
        self.assertEqual(pane.file_notice.cget("text"), "File not found.")
        self.assertFalse(pane.file_open_button.enabled)
        big = self.workspace / "big.log"
        big.write_bytes(b"y" * (1_000_000 + 10))
        pane.show_file(big)
        self.assertIn("Showing the first", pane.file_notice.cget("text"))
        self.assertLessEqual(len(pane.file_view.plain_text()), 1_000_000 + 64)


class DiffChipTests(TkCase):
    def test_chip_text_and_click(self):
        clicks: list[int] = []
        report = DiffReport.from_record({"files": [
            {"relative": "a", "kind": "modified", "added_lines": 12, "removed_lines": 3},
            {"relative": "b", "kind": "added", "added_lines": 0, "removed_lines": 0},
        ]})
        chip = DiffChip(self.root, self.app, report, lambda: clicks.append(1))
        self.widgets.append(chip)
        self.assertEqual(chip.text, "+12 −3")
        self.assertEqual(report.summary, "+12 −3 · 2 files")
        chip.invoke()
        self.assertEqual(clicks, [1])
        self.assertEqual(str(chip.cget("takefocus")), "1")
        empty = DiffChip(self.root, self.app, DiffReport())
        self.widgets.append(empty)
        self.assertEqual(empty.text, "no changes")
        unavailable = DiffChip(self.root, self.app, "unavailable: workspace too large")
        self.widgets.append(unavailable)
        self.assertEqual(unavailable.text, "diff unavailable")

    def test_scrollbars_have_real_thickness_and_hide_when_content_fits(self):
        # clam sizes the thumb from ``arrowsize``; zeroing it (the app's own
        # recipe) collapses the bar to 1 px.  The pane's styles must not.
        pane = self.pane()
        view = pane.artifact_view
        self.root.update()
        self.assertGreaterEqual(view.vbar.winfo_reqwidth(), 4)
        self.assertGreaterEqual(view.hbar.winfo_reqheight(), 4)
        # A hidden tab's Text reports transient fractions before layout, so
        # drive the yscroll callback directly: whole → hidden, partial → shown.
        pane.artifact_view._on_yscroll("0.0", "1.0")
        self.assertEqual(view.vbar.grid_info(), {})
        pane.show_artifact(Artifact(kind="text", language="", text="\n".join(str(n) for n in range(400)), title="long"))
        pane.artifact_view._on_yscroll("0.0", "0.2")
        self.assertEqual(view.vbar.grid_info().get("column"), 2)
        pane.artifact_view._on_yscroll("0.0", "1.0")
        self.assertEqual(view.vbar.grid_info(), {})

    def test_pane_builds_in_every_theme(self):
        for key in ui.THEMES:
            app = FakeApp(self.root, key)
            pane = SidePane(self.root, app, width=400)
            self.widgets.append(pane)
            pane.set_report(DiffReport.from_record({"files": [{"relative": "a", "kind": "modified", "added_lines": 1, "removed_lines": 1, "unified": "--- a/a\n+++ b/a\n@@ -1 +1 @@\n-x\n+y\n"}]}))
            self.assertEqual(pane.cget("bg"), app.theme.panel)
            self.assertEqual(pane.hunk_view.text.cget("bg"), app.theme.code_bg)


if __name__ == "__main__":
    unittest.main()
