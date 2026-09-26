"""JARVIS Desktop v3 side pane: Diff · Artifact · File (Ctrl+Shift+P).

Two layers live here.  The first is a pure, Tk-free engine that the worker
thread runs around every agent turn; the second is the right-hand pane the
desktop shows beside the chat.  Nothing in this module writes to the
workspace, touches SQLite or the Agent, or decides anything on the client:
"revert" is a sentence handed to the agent, whose own gates apply.

Contract with :mod:`jarvis.ui` (the owner of ``JarvisDesktop`` wires this):

* Before each run the worker calls ``WorkspaceIndex.snapshot(workspace)``
  and, after the run, ``diff_workspace(index, workspace)``.  Both are bounded
  (``max_files`` regular files, ``max_bytes`` per file, text files only) and
  never raise on unreadable files.  A workspace above ``max_files`` is not
  partially indexed: the index carries ``unavailable = "workspace too large"``
  and the report inherits it.
* ``report.to_record()`` is stored in the reply's timeline record under the
  key ``"diff"``.  For an unavailable workspace that value is the string
  ``"unavailable: workspace too large"``; otherwise it is the JSON-safe dict
  described on :meth:`DiffReport.to_record` (unified text ≤ 200 KB per turn).
  ``DiffReport.from_record`` accepts either form, or ``None``.
* ``app.side_pane`` holds one :class:`SidePane`; ``app.toggle_side_pane(tab)``
  calls ``SidePane.toggle(tab)``.  When a reply card is rendered the owner
  calls ``side_pane.set_report(DiffReport.from_record(record.get("diff")))``
  and places a :class:`DiffChip` in the card footer whose ``on_click`` shows
  the Diff tab.  ``artifact_candidates(reply_markdown)`` tells the owner which
  replies deserve an "Open in pane" control; ``side_pane.show_artifact(a)``
  and ``side_pane.show_file(path)`` fill the other two tabs.
* App methods the pane calls: ``copy_text``, ``send_text``, ``quote_text``,
  ``reveal_path``, ``open_path``, ``toast``, ``px``, ``composer.add_files``.
  Scripts and programs (``EXECUTABLE_SUFFIXES``) are only ever revealed.
* Keys: F3 steps to the next hunk / file (Diff) or find match (File); Esc
  hides the pane when the focus is inside it (in the find entry it closes the
  find row first); Ctrl+F opens the File tab's local find.  The owner keeps
  the global chords and may check ``side_pane.has_focus()`` before dispatch.
"""

from __future__ import annotations

import difflib
import hashlib
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

import tkinter as tk
from tkinter import filedialog, ttk

from .ui import (
    WORKSPACE_SKIP_DIRECTORIES,
    IconButton,
    JarvisDesktop,
    RoundButton,
    ScrollFrame,
    Tooltip,
    mix_colors,
    parse_markdown,
    rounded_points,
    safe_ui_text,
)


EXECUTABLE_SUFFIXES = JarvisDesktop.EXECUTABLE_SUFFIXES
WORKSPACE_TOO_LARGE = "workspace too large"
UNAVAILABLE_PREFIX = "unavailable: "
DEFAULT_MAX_FILES = 5000
DEFAULT_MAX_BYTES = 1_000_000
MAX_DIFF_TEXT_BYTES = 200_000
MAX_REPORT_FILES = 1000
MAX_FILE_VIEW_BYTES = 1_000_000
ARTIFACT_CODE_LINES = 60
ARTIFACT_REPLY_CHARS = 6_000
RECORD_VERSION = 1
DIFF_KINDS = ("added", "modified", "deleted")
EMPTY_DIFF_TEXT = "No workspace changes recorded for this reply."
REVERT_SENTENCE = "Revert the file {relative} to how it was before your last change, and explain what you changed."
SCRIPT_NOTICE = "Script or program — shown read-only. Reveal finds it in Explorer; Jarvis Desktop never runs it."
LANGUAGE_EXTENSIONS = {
    "python": ".py", "py": ".py", "javascript": ".js", "js": ".js", "typescript": ".ts",
    "ts": ".ts", "tsx": ".tsx", "jsx": ".jsx", "json": ".json", "markdown": ".md",
    "md": ".md", "html": ".html", "css": ".css", "bash": ".sh", "sh": ".sh",
    "shell": ".sh", "zsh": ".sh", "powershell": ".ps1", "ps1": ".ps1", "pwsh": ".ps1",
    "sql": ".sql", "yaml": ".yml", "yml": ".yml", "toml": ".toml", "rust": ".rs",
    "rs": ".rs", "go": ".go", "java": ".java", "kotlin": ".kt", "kt": ".kt", "c": ".c",
    "h": ".h", "cpp": ".cpp", "c++": ".cpp", "cc": ".cpp", "csharp": ".cs", "cs": ".cs",
    "xml": ".xml", "text": ".txt", "txt": ".txt", "plain": ".txt", "csv": ".csv",
    "ini": ".ini", "cfg": ".cfg", "batch": ".bat", "bat": ".bat", "cmd": ".cmd",
    "lua": ".lua", "ruby": ".rb", "rb": ".rb", "php": ".php", "swift": ".swift",
    "r": ".r", "dockerfile": ".dockerfile", "diff": ".diff", "patch": ".patch",
}
LANGUAGE_NAMES = {
    "py": "Python", "python": "Python", "js": "JavaScript", "javascript": "JavaScript",
    "ts": "TypeScript", "typescript": "TypeScript", "sh": "Shell", "bash": "Shell",
    "ps1": "PowerShell", "powershell": "PowerShell", "cs": "C#", "csharp": "C#",
    "cpp": "C++", "c++": "C++", "md": "Markdown", "markdown": "Markdown",
    "yml": "YAML", "yaml": "YAML", "json": "JSON", "html": "HTML", "css": "CSS",
    "sql": "SQL", "rs": "Rust", "rust": "Rust", "go": "Go", "toml": "TOML",
}
_FILENAME_COMMENT = re.compile(r"^\s*(?:#|//|--|/\*|<!--|;|rem\s)\s*([\w][\w.\-/\\ ]*\.[A-Za-z0-9]{1,8})\b", re.IGNORECASE)
_UNSAFE_NAME = re.compile(r"[^\w.\- ]+")


# --------------------------------------------------------------------------
# Engine (no Tk)
# --------------------------------------------------------------------------

@dataclass
class FileEntry:
    mtime: float
    size: int
    sha1: str
    content: str


@dataclass
class WorkspaceIndex:
    """Text files under a workspace root, keyed by POSIX relative path."""

    root: str = ""
    files: dict[str, FileEntry] = field(default_factory=dict)
    unavailable: str | None = None
    skipped: int = 0
    max_files: int = DEFAULT_MAX_FILES
    max_bytes: int = DEFAULT_MAX_BYTES
    skip: frozenset[str] = WORKSPACE_SKIP_DIRECTORIES

    @classmethod
    def snapshot(
        cls,
        root: Path | str,
        *,
        max_files: int = DEFAULT_MAX_FILES,
        max_bytes: int = DEFAULT_MAX_BYTES,
        skip: Iterable[str] = WORKSPACE_SKIP_DIRECTORIES,
    ) -> "WorkspaceIndex":
        """Index every readable text file below ``root``.

        Directories named in ``skip`` (case-insensitive) and dot-directories
        are not entered; symlinks are never followed.  Files above
        ``max_bytes`` and files containing a NUL byte are skipped (counted in
        ``skipped``).  When the number of regular files seen exceeds
        ``max_files`` the walk stops and the index is returned with
        ``unavailable`` set and no files, so the report never lies about a
        partial view.  Unreadable files are skipped, never raised.
        """
        skip_set = frozenset(str(name).casefold() for name in skip)
        base = Path(root)
        index = cls(root=str(base), max_files=int(max_files), max_bytes=int(max_bytes), skip=skip_set)
        if not base.is_dir():
            return index
        pending = [base]
        seen = 0
        while pending:
            directory = pending.pop()
            try:
                entries = list(os.scandir(directory))
            except OSError:
                continue
            for entry in entries:
                name = entry.name
                try:
                    if entry.is_symlink():
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        if name.startswith(".") or name.casefold() in skip_set:
                            continue
                        pending.append(Path(entry.path))
                        continue
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    details = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                seen += 1
                if seen > index.max_files:
                    return cls(root=str(base), unavailable=WORKSPACE_TOO_LARGE, max_files=index.max_files, max_bytes=index.max_bytes, skip=skip_set)
                if details.st_size > index.max_bytes:
                    index.skipped += 1
                    continue
                try:
                    data = Path(entry.path).read_bytes()
                except OSError:
                    index.skipped += 1
                    continue
                if b"\x00" in data:
                    index.skipped += 1
                    continue
                try:
                    relative = Path(entry.path).relative_to(base).as_posix()
                except ValueError:
                    continue
                index.files[relative] = FileEntry(
                    mtime=float(details.st_mtime), size=int(details.st_size),
                    sha1=hashlib.sha1(data, usedforsecurity=False).hexdigest(),
                    content=data.decode("utf-8", errors="replace"),
                )
        return index

    def path_for(self, relative: str) -> str:
        return str(Path(self.root) / Path(relative))


@dataclass
class DiffEntry:
    path: str
    relative: str
    kind: str
    added_lines: int = 0
    removed_lines: int = 0
    unified: str = ""
    truncated: bool = False

    @property
    def counts(self) -> str:
        return f"+{self.added_lines} −{self.removed_lines}"

    def to_record(self) -> dict[str, Any]:
        return {
            "path": self.path, "relative": self.relative, "kind": self.kind,
            "added_lines": int(self.added_lines), "removed_lines": int(self.removed_lines),
            "unified": self.unified, "truncated": bool(self.truncated),
        }


@dataclass
class DiffReport:
    entries: list[DiffEntry] = field(default_factory=list)
    root: str = ""
    unavailable: str | None = None
    truncated: bool = False

    @property
    def total_added(self) -> int:
        return sum(entry.added_lines for entry in self.entries)

    @property
    def total_removed(self) -> int:
        return sum(entry.removed_lines for entry in self.entries)

    @property
    def summary(self) -> str:
        if self.unavailable:
            return UNAVAILABLE_PREFIX + self.unavailable
        if not self.entries:
            return "no changes"
        count = len(self.entries)
        return f"+{self.total_added} −{self.total_removed} · {count} file{'s' if count != 1 else ''}"

    @property
    def chip_text(self) -> str:
        if self.unavailable:
            return "diff unavailable"
        if not self.entries:
            return "no changes"
        return f"+{self.total_added} −{self.total_removed}"

    @property
    def unified_text(self) -> str:
        return "".join(entry.unified for entry in self.entries if entry.unified)

    def to_record(self) -> dict[str, Any] | str:
        """JSON-safe sidecar value.

        ``"unavailable: workspace too large"`` for an unavailable workspace;
        otherwise ``{"version": 1, "root", "summary", "added", "removed",
        "files_changed", "truncated", "files": [DiffEntry.to_record()...]}``.
        """
        if self.unavailable:
            return UNAVAILABLE_PREFIX + self.unavailable
        return {
            "version": RECORD_VERSION,
            "root": self.root,
            "summary": self.summary,
            "added": self.total_added,
            "removed": self.total_removed,
            "files_changed": len(self.entries),
            "truncated": bool(self.truncated),
            "files": [entry.to_record() for entry in self.entries[:MAX_REPORT_FILES]],
        }

    @classmethod
    def from_record(cls, record: Any) -> "DiffReport":
        """Rebuild a report from a sidecar value; anything malformed → empty."""
        if isinstance(record, DiffReport):
            return record
        if isinstance(record, str):
            text = record.strip()
            if text.startswith(UNAVAILABLE_PREFIX):
                return cls(unavailable=text[len(UNAVAILABLE_PREFIX):].strip() or WORKSPACE_TOO_LARGE)
            return cls()
        if not isinstance(record, dict):
            return cls()
        report = cls(root=str(record.get("root") or ""), truncated=bool(record.get("truncated", False)))
        unavailable = record.get("unavailable")
        if unavailable:
            report.unavailable = str(unavailable)
            return report
        budget = MAX_DIFF_TEXT_BYTES
        files = record.get("files")
        for raw in (files if isinstance(files, list) else [])[:MAX_REPORT_FILES]:
            if not isinstance(raw, dict):
                continue
            kind = str(raw.get("kind") or "modified")
            if kind not in DIFF_KINDS:
                kind = "modified"
            unified = raw.get("unified")
            unified = unified if isinstance(unified, str) else ""
            truncated = bool(raw.get("truncated", False))
            cost = len(unified.encode("utf-8", errors="replace"))
            if cost > budget:
                unified, truncated = "", True
            else:
                budget -= cost
            entry = DiffEntry(
                path=str(raw.get("path") or ""), relative=str(raw.get("relative") or raw.get("path") or ""),
                kind=kind, added_lines=_as_int(raw.get("added_lines")),
                removed_lines=_as_int(raw.get("removed_lines")), unified=unified, truncated=truncated,
            )
            report.entries.append(entry)
            report.truncated = report.truncated or truncated
        return report


def _as_int(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def unified_diff_text(relative: str, before: str | None, after: str | None) -> tuple[str, int, int]:
    """``difflib.unified_diff`` of two texts → (text, added_lines, removed_lines)."""
    old = before.splitlines(keepends=True) if before is not None else []
    new = after.splitlines(keepends=True) if after is not None else []
    lines = list(difflib.unified_diff(old, new, fromfile=f"a/{relative}", tofile=f"b/{relative}", n=3))
    added = removed = 0
    for line in lines[2:]:
        if line.startswith("+"):
            added += 1
        elif line.startswith("-"):
            removed += 1
    text = "".join(line if line.endswith("\n") else line + "\n" for line in lines)
    return text, added, removed


def diff_workspace(before: WorkspaceIndex, root: Path | str) -> DiffReport:
    """Rescan ``root`` with the index's limits and diff it against ``before``."""
    report = DiffReport(root=str(root))
    if before.unavailable:
        report.unavailable = before.unavailable
        return report
    after = WorkspaceIndex.snapshot(root, max_files=before.max_files, max_bytes=before.max_bytes, skip=before.skip)
    if after.unavailable:
        report.unavailable = after.unavailable
        return report
    names = sorted(set(before.files) | set(after.files))
    budget = MAX_DIFF_TEXT_BYTES
    for relative in names:
        old = before.files.get(relative)
        new = after.files.get(relative)
        if old is not None and new is not None:
            if old.sha1 == new.sha1:
                continue
            kind = "modified"
        elif new is not None:
            kind = "added"
        else:
            kind = "deleted"
        text, added, removed = unified_diff_text(
            relative, old.content if old else None, new.content if new else None
        )
        if not text and kind == "modified":
            continue
        entry = DiffEntry(path=after.path_for(relative), relative=relative, kind=kind, added_lines=added, removed_lines=removed)
        cost = len(text.encode("utf-8", errors="replace"))
        if cost <= budget:
            entry.unified = text
            budget -= cost
        else:
            entry.truncated = True
            report.truncated = True
        report.entries.append(entry)
        if len(report.entries) >= MAX_REPORT_FILES:
            report.truncated = True
            break
    return report


@dataclass
class Artifact:
    kind: str
    language: str
    text: str
    title: str

    @property
    def line_count(self) -> int:
        return len(self.text.splitlines()) if self.text else 0

    @property
    def language_name(self) -> str:
        key = (self.language or "").lower()
        if not key:
            return "Markdown" if self.kind == "text" else "Text"
        return LANGUAGE_NAMES.get(key, key.upper() if len(key) <= 3 else key.capitalize())

    def suggested_extension(self) -> str:
        key = (self.language or "").lower()
        if key in LANGUAGE_EXTENSIONS:
            return LANGUAGE_EXTENSIONS[key]
        return ".md" if self.kind == "text" else ".txt"

    def suggested_filename(self) -> str:
        extension = self.suggested_extension()
        title = _UNSAFE_NAME.sub("", self.title or "").strip().strip(".")
        if title and "." in title and title.rsplit(".", 1)[1].isalnum():
            return title
        stem = title.replace(" ", "_")[:48] or "artifact"
        return f"{stem}{extension}"


def artifact_candidates(markdown: str) -> list[Artifact]:
    """Fenced blocks over 60 lines and replies over 6,000 chars become artifacts."""
    source = str(markdown or "")
    found: list[Artifact] = []
    blocks = parse_markdown(source)
    for block in blocks:
        if block.get("type") != "code":
            continue
        text = str(block.get("text") or "")
        if len(text.splitlines()) <= ARTIFACT_CODE_LINES:
            continue
        language = str(block.get("lang") or "")
        found.append(Artifact(kind="code", language=language, text=text, title=_code_title(text, language)))
    if len(source) > ARTIFACT_REPLY_CHARS:
        title = next((str(block.get("text") or "").strip() for block in blocks if block.get("type") == "heading"), "") or "Full reply"
        found.append(Artifact(kind="text", language="markdown", text=source, title=title[:80]))
    return found


def _code_title(text: str, language: str) -> str:
    for line in text.splitlines()[:3]:
        match = _FILENAME_COMMENT.match(line)
        if match:
            return match.group(1).strip()
    if language:
        name = LANGUAGE_NAMES.get(language.lower(), language.upper() if len(language) <= 3 else language.capitalize())
        return f"{name} snippet"
    return "Code snippet"


# --------------------------------------------------------------------------
# Tk helpers
# --------------------------------------------------------------------------

def ensure_scrollbar_styles(widget: tk.Misc, theme: Any, px: Callable[[float], int]) -> None:
    """Configure the pane's own thin ttk scrollbar styles from the theme.

    ``JarvisPane.<orient>.TScrollbar`` is always (re)configured here so the
    pane looks the same inside the real app and in a bare test root.  In the
    clam theme the thumb's cross-axis size comes from ``arrowsize`` (the
    element has no ``width`` option), so the arrow-less layout keeps
    ``arrowsize`` at the wanted thickness instead of zeroing it.  The app's
    own ``Jarvis.Vertical.TScrollbar`` (used by :class:`ScrollFrame`) is only
    created when nobody defined it, never overridden.
    """
    style = ttk.Style(widget)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass
    thickness = max(4, px(8))
    for orient in ("Vertical", "Horizontal"):
        names = [f"JarvisPane.{orient}.TScrollbar"]
        # ``layout(name)`` silently falls back to the base style, so probe the
        # style's own option table instead: ``None`` means nobody defined it.
        if orient == "Vertical" and not style.configure("Jarvis.Vertical.TScrollbar"):
            names.append("Jarvis.Vertical.TScrollbar")
        for name in names:
            style.configure(
                name, gripcount=0, background=theme.border_strong, troughcolor=theme.bg,
                bordercolor=theme.bg, lightcolor=theme.bg, darkcolor=theme.bg, arrowsize=thickness,
            )
            style.map(name, background=[("active", theme.faint)])
            thumb = f"{orient}.Scrollbar.thumb"
            sticky = "ns" if orient == "Vertical" else "ew"
            style.layout(name, [(f"{orient}.Scrollbar.trough", {"children": [(thumb, {"expand": "1", "sticky": "nswe"})], "sticky": sticky})])


def _walk(widget: tk.Misc) -> Iterable[tk.Misc]:
    stack = [widget]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(current.winfo_children())


class NumberedText(tk.Frame):
    """Read-only monospace view with a line-number gutter and both scrollbars."""

    def __init__(self, master: tk.Misc, app: Any, *, wrap: str = "none") -> None:
        theme = app.theme
        super().__init__(master, bg=theme.code_bg, highlightthickness=1, highlightbackground=theme.border)
        self.app = app
        self.theme = theme
        font = app.fonts.mono_small
        self.vbar = ttk.Scrollbar(self, orient="vertical", command=self._yview, style="JarvisPane.Vertical.TScrollbar")
        self.hbar = ttk.Scrollbar(self, orient="horizontal", style="JarvisPane.Horizontal.TScrollbar")
        self.gutter = tk.Text(
            self, width=4, bd=0, highlightthickness=0, padx=app.px(6), pady=app.px(6), font=font,
            bg=theme.code_bg, fg=theme.faint, cursor="arrow", takefocus=0, wrap="none",
            state="disabled", spacing1=0, spacing3=0,
        )
        self.text = tk.Text(
            self, bd=0, highlightthickness=0, padx=app.px(8), pady=app.px(6), font=font,
            bg=theme.code_bg, fg=theme.text, insertwidth=0, cursor="arrow", wrap=wrap,
            selectbackground=theme.selection, selectforeground=theme.text_strong,
            state="disabled", spacing1=0, spacing3=0, undo=False,
            yscrollcommand=self._on_yscroll, xscrollcommand=self._on_xscroll,
        )
        self.hbar.configure(command=self.text.xview)
        self.gutter.grid(row=0, column=0, sticky="ns")
        self.text.grid(row=0, column=1, sticky="nsew")
        self.vbar.grid(row=0, column=2, sticky="ns")
        self.hbar.grid(row=1, column=0, columnspan=2, sticky="ew")
        self.vbar.grid_remove()
        self.hbar.grid_remove()
        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(1, weight=1)
        self.text.tag_configure("find", background=theme.selection)
        self.text.tag_configure("find-current", background=theme.accent, foreground=theme.accent_ink)
        self.gutter.bind("<MouseWheel>", self._wheel)
        self.text.bind("<MouseWheel>", self._wheel)
        self.text.bind("<Control-a>", self._select_all)

    def _yview(self, *args: Any) -> None:
        self.text.yview(*args)
        self.gutter.yview(*args)

    def _on_yscroll(self, first: str, last: str) -> None:
        self.vbar.set(first, last)
        self.gutter.yview_moveto(float(first))
        self._toggle_bar(self.vbar, first, last)

    def _on_xscroll(self, first: str, last: str) -> None:
        self.hbar.set(first, last)
        self._toggle_bar(self.hbar, first, last)

    @staticmethod
    def _toggle_bar(bar: ttk.Scrollbar, first: str, last: str) -> None:
        """Show a scrollbar only while the content overflows (like ScrollFrame)."""
        try:
            whole = float(first) <= 0.0 and float(last) >= 1.0
            managed = bool(bar.grid_info())  # ismapped() lies under a withdrawn root
            if whole and managed:
                bar.grid_remove()
            elif not whole and not managed:
                bar.grid()
        except (tk.TclError, ValueError):
            pass

    def _wheel(self, event: Any) -> str:
        self._yview("scroll", int(-event.delta / 40) or (-1 if event.delta > 0 else 1), "units")
        return "break"

    def _select_all(self, _event: Any) -> str:
        self.text.tag_add("sel", "1.0", "end-1c")
        return "break"

    def set_text(self, text: str, *, numbered: bool = True) -> None:
        lines = text.split("\n")
        if text.endswith("\n"):
            lines = lines[:-1]
        count = max(1, len(lines))
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.insert("1.0", text)
        self.text.configure(state="disabled")
        self.gutter.configure(state="normal")
        self.gutter.delete("1.0", "end")
        if numbered:
            width = max(2, len(str(count)))
            self.gutter.configure(width=width + 1)
            self.gutter.insert("1.0", "\n".join(str(number).rjust(width) for number in range(1, count + 1)))
        else:
            self.gutter.configure(width=1)
        self.gutter.configure(state="disabled")
        self._yview("moveto", 0.0)

    def selected_text(self) -> str:
        try:
            return self.text.get("sel.first", "sel.last")
        except tk.TclError:
            return ""

    def plain_text(self) -> str:
        return self.text.get("1.0", "end-1c")


class FindRow(tk.Frame):
    """Local find over one :class:`NumberedText` (Entry + prev/next + count)."""

    def __init__(self, master: tk.Misc, app: Any, target: NumberedText, on_close: Callable[[], None]) -> None:
        theme = app.theme
        super().__init__(master, bg=theme.surface_alt, padx=app.px(8), pady=app.px(4))
        self.app = app
        self.target = target
        self.on_close = on_close
        self.matches: list[str] = []
        self.index = -1
        tk.Label(self, text="Find", bg=theme.surface_alt, fg=theme.muted, font=app.fonts.small).pack(side="left")
        self.entry = tk.Entry(
            self, bg=theme.surface, fg=theme.text, insertbackground=theme.accent, bd=0,
            highlightthickness=1, highlightbackground=theme.border, highlightcolor=theme.accent,
            font=app.fonts.small, width=22,
        )
        self.entry.pack(side="left", padx=(8, 8), ipady=3)
        self.entry.bind("<KeyRelease>", lambda _e: self.search())
        self.entry.bind("<Return>", lambda _e: self.step(1))
        self.entry.bind("<Shift-Return>", lambda _e: self.step(-1))
        self.entry.bind("<F3>", lambda _e: self.step(1))
        self.entry.bind("<Escape>", lambda _e: self.close())
        self.count = tk.Label(self, text="", bg=theme.surface_alt, fg=theme.faint, font=app.fonts.tiny)
        self.count.pack(side="left")
        IconButton(self, app, "×", self.close, tooltip="Close (Esc)").pack(side="right")
        IconButton(self, app, "›", lambda: self.step(1), tooltip="Next (Enter, F3)").pack(side="right")
        IconButton(self, app, "‹", lambda: self.step(-1), tooltip="Previous (Shift+Enter)").pack(side="right")

    def open(self) -> None:
        if not self.winfo_manager():
            # Sit directly above the text it searches, whatever was packed first.
            self.pack(fill="x", side="top", before=self.target)
        self.entry.focus_set()
        self.entry.select_range(0, "end")

    def close(self) -> str:
        self.clear()
        self.pack_forget()
        self.on_close()
        return "break"

    def clear(self) -> None:
        text = self.target.text
        text.tag_remove("find", "1.0", "end")
        text.tag_remove("find-current", "1.0", "end")
        self.matches = []
        self.index = -1
        self.count.configure(text="")

    def search(self) -> None:
        query = self.entry.get()
        self.clear()
        if len(query) < 1:
            return
        text = self.target.text
        start = "1.0"
        while len(self.matches) < 5000:
            position = text.search(query, start, stopindex="end", nocase=True)
            if not position:
                break
            end = f"{position}+{len(query)}c"
            text.tag_add("find", position, end)
            self.matches.append(position)
            start = end
        self.count.configure(text=f"{len(self.matches)} match{'es' if len(self.matches) != 1 else ''}" if self.matches else "No matches")
        if self.matches:
            self.step(1)

    def step(self, delta: int) -> str:
        if not self.matches:
            return "break"
        text = self.target.text
        length = len(self.entry.get())
        if 0 <= self.index < len(self.matches):
            position = self.matches[self.index]
            text.tag_remove("find-current", position, f"{position}+{length}c")
        self.index = (self.index + delta) % len(self.matches)
        position = self.matches[self.index]
        text.tag_add("find-current", position, f"{position}+{length}c")
        text.see(position)
        self.count.configure(text=f"{self.index + 1} of {len(self.matches)}")
        return "break"


# --------------------------------------------------------------------------
# The pane
# --------------------------------------------------------------------------

class SidePane(tk.Frame):
    """Right-hand pane with the Diff · Artifact · File tabs."""

    TABS = ("diff", "artifact", "file")
    TAB_LABELS = {"diff": "Diff", "artifact": "Artifact", "file": "File"}
    KIND_GLYPHS = {"added": "A", "modified": "M", "deleted": "D"}

    def __init__(self, master: tk.Misc, app: Any, *, width: int | None = None, pack_options: dict[str, Any] | None = None) -> None:
        theme = app.theme
        self.app = app
        self.theme = theme
        pane_width = max(app.px(360), int(width) if width else app.px(520))
        super().__init__(master, bg=theme.panel, width=pane_width, highlightthickness=0)
        self.pane_width = pane_width
        self.pack_propagate(False)
        ensure_scrollbar_styles(self, theme, app.px)
        self._pack_options = dict(pack_options or {"side": "right", "fill": "y"})
        self._visible = False
        self.tab = "diff"
        self.report = DiffReport()
        self.artifact: Artifact | None = None
        self.file_path: str | None = None
        self.file_executable = False
        self.selected = -1
        self._rows: list[tk.Frame] = []
        self._hunk_cursor = "1.0"
        self._build()
        self._show_tab("diff")
        for widget in _walk(self):
            self._bind_keys(widget)

    # -- visibility -------------------------------------------------------

    @property
    def visible(self) -> bool:
        return self._visible

    def show(self, tab: str | None = None) -> None:
        if tab:
            self._show_tab(tab)
        if not self._visible:
            self.pack(**self._pack_options)
            self._visible = True

    def hide(self) -> None:
        if self._visible:
            self.pack_forget()
            self._visible = False
        try:
            focus = self.focus_get()
        except (tk.TclError, KeyError):
            focus = None
        if focus is not None and self._owns(focus):
            focus_target = getattr(self.app, "focus_composer", None)
            if callable(focus_target):
                focus_target()

    def toggle(self, tab: str | None = None) -> None:
        if self._visible and (tab is None or tab == self.tab):
            self.hide()
        else:
            self.show(tab or self.tab)

    def has_focus(self) -> bool:
        try:
            focus = self.focus_get()
        except (tk.TclError, KeyError):
            return False
        return focus is not None and self._owns(focus)

    def _owns(self, widget: tk.Misc) -> bool:
        current: Any = widget
        while current is not None:
            if current is self:
                return True
            current = getattr(current, "master", None)
        return False

    # -- build ------------------------------------------------------------

    def _build(self) -> None:
        theme = self.theme
        app = self.app
        tk.Frame(self, bg=theme.border, width=1).pack(side="left", fill="y")
        header = tk.Frame(self, bg=theme.panel, padx=app.px(10), pady=app.px(8))
        header.pack(side="top", fill="x")
        self.tab_buttons: dict[str, RoundButton] = {}
        for key in self.TABS:
            button = RoundButton(header, app, self.TAB_LABELS[key], lambda k=key: self._show_tab(k), kind="subtle", padx=12, pady=5, radius=8)
            button.pack(side="left", padx=(0, 4))
            self.tab_buttons[key] = button
        self.close_button = IconButton(header, app, "×", self.hide, tooltip="Close (Esc)")
        self.close_button.pack(side="right")
        tk.Frame(self, bg=theme.border, height=1).pack(side="top", fill="x")
        self.body = tk.Frame(self, bg=theme.panel)
        self.body.pack(side="top", fill="both", expand=True)
        self.frames = {
            "diff": self._build_diff_tab(),
            "artifact": self._build_artifact_tab(),
            "file": self._build_file_tab(),
        }

    def _show_tab(self, tab: str) -> None:
        if tab not in self.TABS:
            tab = "diff"
        self.tab = tab
        for key, button in self.tab_buttons.items():
            button.set_kind("active" if key == tab else "subtle")
        for key, frame in self.frames.items():
            if key == tab:
                frame.pack(fill="both", expand=True)
            else:
                frame.pack_forget()

    def _toolbar(self, parent: tk.Misc) -> tk.Frame:
        bar = tk.Frame(parent, bg=self.theme.panel, padx=self.app.px(10), pady=self.app.px(6))
        bar.pack(side="top", fill="x")
        return bar

    def _button(self, parent: tk.Misc, text: str, command: Callable[[], None], *, tooltip: str | None = None, kind: str = "ghost") -> RoundButton:
        button = RoundButton(parent, self.app, text, command, kind=kind, font=self.app.fonts.small, padx=10, pady=4, radius=7, tooltip=tooltip)
        button.pack(side="right", padx=(6, 0))
        return button

    # -- diff tab ---------------------------------------------------------

    def _build_diff_tab(self) -> tk.Frame:
        theme = self.theme
        app = self.app
        frame = tk.Frame(self.body, bg=theme.panel)
        bar = self._toolbar(frame)
        self.diff_summary = tk.Label(bar, text="", bg=theme.panel, fg=theme.muted, font=app.fonts.small, anchor="w")
        self.diff_summary.pack(side="left")
        self.reveal_button = self._button(bar, "Reveal", self.reveal_selected, tooltip="Show this file in Explorer")
        self.revert_button = self._button(bar, "Ask Jarvis to revert this file", self.ask_revert, tooltip="Sends a message; Jarvis decides under its own approval gates")
        self.copy_diff_button = self._button(bar, "Copy diff", self.copy_diff, tooltip="Copy the whole diff for this reply")
        self.paned = tk.PanedWindow(
            frame, orient="horizontal", bg=theme.border, bd=0, sashwidth=app.px(3), sashpad=0,
            sashrelief="flat", showhandle=False, opaqueresize=True,
        )
        self.paned.pack(fill="both", expand=True, padx=(app.px(10), app.px(10)), pady=(0, app.px(10)))
        list_shell = tk.Frame(self.paned, bg=theme.surface, highlightthickness=1, highlightbackground=theme.border)
        self.file_list = ScrollFrame(list_shell, app, bg=theme.surface)
        self.file_list.pack(fill="both", expand=True)
        self.hunk_view = NumberedText(self.paned, app)
        text = self.hunk_view.text
        text.tag_configure("add", foreground=theme.success, background=mix_colors(theme.code_bg, theme.success, 0.14))
        text.tag_configure("del", foreground=theme.danger, background=mix_colors(theme.code_bg, theme.danger, 0.16))
        text.tag_configure("hunk", foreground=theme.accent, background=mix_colors(theme.code_bg, theme.accent, 0.10))
        text.tag_configure("hunk-current", background=mix_colors(theme.code_bg, theme.accent, 0.26))
        text.tag_configure("meta", foreground=theme.faint)
        text.tag_configure("notice", foreground=theme.muted, font=app.fonts.small)
        self.paned.add(list_shell, minsize=app.px(140), width=app.px(200), stretch="never")
        self.paned.add(self.hunk_view, minsize=app.px(160), stretch="always")
        self.set_report(self.report)
        return frame

    def set_report(self, report: DiffReport | Any) -> None:
        self.report = report if isinstance(report, DiffReport) else DiffReport.from_record(report)
        self.selected = -1
        self._hunk_cursor = "1.0"
        for row in self._rows:
            row.destroy()
        self._rows = []
        summary = self.report.summary
        if self.report.unavailable:
            self.diff_summary.configure(text="Changes not recorded")
        elif not self.report.entries:
            self.diff_summary.configure(text="No changes")
        else:
            self.diff_summary.configure(text=summary + (" · diff text capped" if self.report.truncated else ""))
        for index, entry in enumerate(self.report.entries):
            self._rows.append(self._make_row(index, entry))
        has_files = bool(self.report.entries)
        self.reveal_button.set_enabled(has_files)
        self.revert_button.set_enabled(has_files)
        self.copy_diff_button.set_enabled(bool(self.report.unified_text))
        if has_files:
            self.select(0)
        else:
            self._render_notice(
                f"Workspace changes weren't recorded for this reply: {self.report.unavailable}."
                if self.report.unavailable else EMPTY_DIFF_TEXT
            )
        self.file_list.scroll_to_top()

    def _make_row(self, index: int, entry: DiffEntry) -> tk.Frame:
        theme = self.theme
        app = self.app
        row = tk.Frame(self.file_list.inner, bg=theme.surface, padx=app.px(8), pady=app.px(4), cursor="hand2")
        row.pack(fill="x")
        colour = {"added": theme.success, "deleted": theme.danger}.get(entry.kind, theme.warning)
        glyph = tk.Label(row, text=self.KIND_GLYPHS.get(entry.kind, "?"), bg=theme.surface, fg=colour, font=app.fonts.small_bold, width=2, anchor="w")
        glyph.pack(side="left")
        counts = tk.Label(row, text=entry.counts, bg=theme.surface, fg=theme.faint, font=app.fonts.tiny)
        counts.pack(side="right", padx=(6, 0))
        name = tk.Label(row, text=entry.relative, bg=theme.surface, fg=theme.text, font=app.fonts.small, anchor="w")
        name.pack(side="left", fill="x", expand=True)
        for widget in (row, glyph, counts, name):
            widget.bind("<Button-1>", lambda _e, i=index: self.select(i))
            widget.bind("<Enter>", lambda _e, i=index: self._hover_row(i, True))
            widget.bind("<Leave>", lambda _e, i=index: self._hover_row(i, False))
        if entry.truncated:
            Tooltip(name, "Diff text for this file was over the 200 KB per-reply cap and was not stored.", app)
        return row

    def _paint_row(self, index: int, bg: str) -> None:
        if not 0 <= index < len(self._rows):
            return
        row = self._rows[index]
        try:
            row.configure(bg=bg)
            for child in row.winfo_children():
                child.configure(bg=bg)
        except tk.TclError:
            pass

    def _hover_row(self, index: int, inside: bool) -> None:
        if index == self.selected:
            return
        self._paint_row(index, self.theme.surface_hover if inside else self.theme.surface)

    def select(self, index: int) -> None:
        if not 0 <= index < len(self.report.entries):
            return
        if 0 <= self.selected < len(self._rows):
            self._paint_row(self.selected, self.theme.surface)
        self.selected = index
        self._paint_row(index, self.theme.selection)
        self._render_entry(self.report.entries[index])

    @property
    def selected_entry(self) -> DiffEntry | None:
        if 0 <= self.selected < len(self.report.entries):
            return self.report.entries[self.selected]
        return None

    def _render_notice(self, text: str) -> None:
        self.hunk_view.set_text("", numbered=False)
        view = self.hunk_view.text
        view.configure(state="normal")
        view.insert("1.0", text, ("notice",))
        view.configure(state="disabled")

    def _render_entry(self, entry: DiffEntry) -> None:
        view = self.hunk_view.text
        self._hunk_cursor = "1.0"
        if not entry.unified:
            self._render_notice(
                "Diff text for this file was over the 200 KB per-reply cap and was not stored."
                if entry.truncated else f"{entry.kind.capitalize()} · no line changes recorded."
            )
            return
        self.hunk_view.set_text("", numbered=False)
        view.configure(state="normal")
        lines = safe_ui_text(entry.unified, MAX_DIFF_TEXT_BYTES + 64).splitlines()
        last = len(lines) - 1
        for number, line in enumerate(lines):
            if number < 2 and (line.startswith("---") or line.startswith("+++")):
                tag = "meta"
            elif line.startswith("@@"):
                tag = "hunk"
            elif line.startswith("+"):
                tag = "add"
            elif line.startswith("-"):
                tag = "del"
            elif line.startswith("\\"):
                tag = "meta"
            else:
                tag = ""
            view.insert("end", line + ("\n" if number < last else ""), (tag,) if tag else ())
        view.configure(state="disabled")
        view.yview_moveto(0.0)

    def next_hunk(self) -> str:
        """F3 in the Diff tab: next hunk in this file, then the next file."""
        view = self.hunk_view.text
        ranges = view.tag_ranges("hunk")
        starts = [view.index(ranges[i]) for i in range(0, len(ranges), 2)]
        view.tag_remove("hunk-current", "1.0", "end")
        for start in starts:
            if view.compare(start, ">", self._hunk_cursor):
                self._hunk_cursor = start
                view.tag_add("hunk-current", start, f"{start} lineend")
                view.see(start)
                return "break"
        if self.report.entries:
            self.select((self.selected + 1) % len(self.report.entries))
            first = view.tag_ranges("hunk")
            if first:
                start = view.index(first[0])
                self._hunk_cursor = start
                view.tag_add("hunk-current", start, f"{start} lineend")
                view.see(start)
        return "break"

    def copy_diff(self) -> None:
        text = self.report.unified_text
        if not text:
            self.app.toast("No diff to copy.", kind="warning")
            return
        self.app.copy_text(text)

    def ask_revert(self) -> None:
        entry = self.selected_entry
        if entry is None:
            self.app.toast("Select a file first.", kind="warning")
            return
        self.app.send_text(REVERT_SENTENCE.format(relative=entry.relative))

    def reveal_selected(self) -> None:
        entry = self.selected_entry
        if entry is None:
            self.app.toast("Select a file first.", kind="warning")
            return
        target = entry.path
        if entry.kind == "deleted" and target:
            target = str(Path(target).parent)
        self.app.reveal_path(target)

    # -- artifact tab -----------------------------------------------------

    def _build_artifact_tab(self) -> tk.Frame:
        theme = self.theme
        app = self.app
        frame = tk.Frame(self.body, bg=theme.panel)
        bar = self._toolbar(frame)
        self.artifact_title = tk.Label(bar, text="No artifact", bg=theme.panel, fg=theme.text_strong, font=app.fonts.label_bold, anchor="w")
        self.artifact_title.pack(side="left")
        self.artifact_chip = tk.Label(bar, text="", bg=theme.accent_soft, fg=theme.accent, font=app.fonts.tiny, padx=6, pady=1)
        self.artifact_meta = tk.Label(bar, text="", bg=theme.panel, fg=theme.faint, font=app.fonts.tiny)
        actions = tk.Frame(frame, bg=theme.panel, padx=app.px(10))
        actions.pack(side="top", fill="x", pady=(0, app.px(6)))
        self.artifact_hint = tk.Label(actions, text="Read-only", bg=theme.panel, fg=theme.faint, font=app.fonts.tiny)
        self.artifact_hint.pack(side="left")
        self.quote_button = self._button(actions, "Quote selection", self.quote_selection, tooltip="Put the selected lines in the composer as a quote")
        self.save_button = self._button(actions, "Save as…", self.save_artifact, tooltip="Save this artifact to a file you choose")
        self.copy_artifact_button = self._button(actions, "Copy", self.copy_artifact, tooltip="Copy the whole artifact")
        self.artifact_view = NumberedText(frame, app)
        self.artifact_view.pack(fill="both", expand=True, padx=app.px(10), pady=(0, app.px(10)))
        self.artifact_view.set_text("")
        for button in (self.quote_button, self.save_button, self.copy_artifact_button):
            button.set_enabled(False)
        return frame

    def show_artifact(self, artifact: Artifact) -> None:
        self.artifact = artifact
        self.artifact_title.configure(text=safe_ui_text(artifact.title or "Artifact", 120))
        self.artifact_chip.configure(text=artifact.language_name)
        self.artifact_chip.pack(side="left", padx=(8, 0))
        size = len(artifact.text.encode("utf-8", errors="replace"))
        self.artifact_meta.configure(text=f"{artifact.line_count} lines · {_format_size(size)}")
        self.artifact_meta.pack(side="left", padx=(8, 0))
        self.artifact_view.set_text(safe_ui_text(artifact.text, MAX_FILE_VIEW_BYTES + 64))
        for button in (self.quote_button, self.save_button, self.copy_artifact_button):
            button.set_enabled(True)
        self.show("artifact")

    def copy_artifact(self) -> None:
        if self.artifact is None:
            return
        self.app.copy_text(self.artifact.text)

    def quote_selection(self) -> None:
        selected = self.artifact_view.selected_text()
        if not selected.strip():
            self.app.toast("Select some text in the artifact first.", kind="warning")
            return
        self.app.quote_text(selected)

    def save_artifact(self) -> str | None:
        """User-initiated save (same footing as export): a dialog, then one write."""
        if self.artifact is None:
            return None
        extension = self.artifact.suggested_extension()
        try:
            target = filedialog.asksaveasfilename(
                parent=self.winfo_toplevel(), title="Save artifact as",
                defaultextension=extension, initialfile=self.artifact.suggested_filename(),
                filetypes=[(f"{extension} files", f"*{extension}"), ("All files", "*.*")],
            )
        except tk.TclError:
            target = ""
        if not target:
            return None
        try:
            with open(target, "w", encoding="utf-8", newline="") as handle:
                handle.write(self.artifact.text)
        except OSError as exc:
            self.app.toast(f"Could not save {Path(target).name}: {exc}", kind="error")
            return None
        self.app.toast(f"Saved {Path(target).name}.", kind="success")
        return str(target)

    # -- file tab ---------------------------------------------------------

    def _build_file_tab(self) -> tk.Frame:
        theme = self.theme
        app = self.app
        frame = tk.Frame(self.body, bg=theme.panel)
        bar = self._toolbar(frame)
        self.file_title = tk.Label(bar, text="No file", bg=theme.panel, fg=theme.text_strong, font=app.fonts.label_bold, anchor="w")
        self.file_title.pack(side="left")
        self.file_meta = tk.Label(bar, text="", bg=theme.panel, fg=theme.faint, font=app.fonts.tiny)
        self.file_meta.pack(side="left", padx=(8, 0))
        wrap = max(app.px(200), self.pane_width - app.px(44))
        self.file_pathline = tk.Label(frame, text="", bg=theme.panel, fg=theme.muted, font=app.fonts.tiny, anchor="w", justify="left", wraplength=wrap, padx=app.px(10))
        self.file_pathline.pack(side="top", fill="x")
        self.file_notice = tk.Label(
            frame, text="", bg=theme.surface_alt, fg=theme.warning, font=app.fonts.small,
            anchor="w", justify="left", wraplength=wrap - app.px(20), padx=app.px(10), pady=app.px(5),
        )
        actions = tk.Frame(frame, bg=theme.panel, padx=app.px(10))
        actions.pack(side="top", fill="x", pady=(app.px(4), app.px(6)))
        self.file_hint = tk.Label(actions, text="Read-only · Ctrl+F to find", bg=theme.panel, fg=theme.faint, font=app.fonts.tiny)
        self.file_hint.pack(side="left")
        self.file_reveal_button = self._button(actions, "Reveal", self.reveal_file, tooltip="Show this file in Explorer")
        self.attach_button = self._button(actions, "Attach", self.attach_file, tooltip="Attach this file to the next message")
        self.file_open_button = self._button(actions, "Open", self.open_file, tooltip="Open with the default app")
        self.find_button = IconButton(actions, app, "⌕", self.open_find, tooltip="Find (Ctrl+F)")
        self.find_button.pack(side="right", padx=(6, 0))
        self.file_view = NumberedText(frame, app)
        self.find_row = FindRow(frame, app, self.file_view, on_close=self._focus_file_view)
        self.file_view.pack(fill="both", expand=True, padx=app.px(10), pady=(0, app.px(10)))
        self.file_view.set_text("")
        for button in (self.file_reveal_button, self.attach_button, self.file_open_button):
            button.set_enabled(False)
        return frame

    def _focus_file_view(self) -> None:
        try:
            self.file_view.text.focus_set()
        except tk.TclError:
            pass

    def open_find(self) -> str:
        if self.tab != "file":
            self._show_tab("file")
        self.find_row.open()
        return "break"

    def show_file(self, path: str | Path) -> None:
        """Bounded, read-only view of one file; scripts are only ever revealed."""
        target = Path(str(path))
        self.file_path = str(target)
        self.file_executable = target.suffix.lower() in EXECUTABLE_SUFFIXES
        if self.find_row.winfo_manager():
            self.find_row.close()
        self.file_title.configure(text=safe_ui_text(target.name or str(target), 120))
        self.file_pathline.configure(text=safe_ui_text(str(target), 400))
        notice = ""
        text = ""
        meta = ""
        exists = target.is_file()
        if not exists:
            notice = "File not found."
        else:
            try:
                size = target.stat().st_size
                with open(target, "rb") as handle:
                    data = handle.read(MAX_FILE_VIEW_BYTES + 1)
            except OSError as exc:
                notice = f"Could not read the file: {exc}"
                data = b""
                size = 0
            if data:
                if b"\x00" in data:
                    notice = "Binary file — contents not shown."
                    data = b""
                elif len(data) > MAX_FILE_VIEW_BYTES:
                    data = data[:MAX_FILE_VIEW_BYTES]
                    notice = f"Showing the first {_format_size(MAX_FILE_VIEW_BYTES)} of {_format_size(size)}."
            text = data.decode("utf-8", errors="replace")
            lines = text.count("\n") + (1 if text and not text.endswith("\n") else 0)
            meta = f"{lines} lines · {_format_size(size)}"
        if self.file_executable:
            notice = SCRIPT_NOTICE + (f" {notice}" if notice else "")
        self.file_meta.configure(text=meta)
        if notice:
            self.file_notice.configure(text=notice)
            self.file_notice.pack(side="top", fill="x", padx=self.app.px(10), pady=(0, self.app.px(4)), after=self.file_pathline)
        else:
            self.file_notice.pack_forget()
        self.file_view.set_text(safe_ui_text(text, MAX_FILE_VIEW_BYTES + 64))
        self.file_reveal_button.set_enabled(True)
        self.attach_button.set_enabled(exists and not (notice.startswith("Binary")))
        if self.file_executable:
            self.file_open_button.pack_forget()
        else:
            if not self.file_open_button.winfo_manager():
                self.file_open_button.pack(side="right", padx=(6, 0), before=self.attach_button)
            self.file_open_button.set_enabled(exists)
        self.show("file")

    def reveal_file(self) -> None:
        if self.file_path:
            self.app.reveal_path(self.file_path)

    def attach_file(self) -> None:
        if self.file_path:
            self.app.composer.add_files([self.file_path])

    def open_file(self) -> None:
        if not self.file_path or self.file_executable:
            self.app.toast("Scripts and programs are only revealed, never opened from here.", kind="warning")
            return
        self.app.open_path(self.file_path)

    # -- keys -------------------------------------------------------------

    def _bind_keys(self, widget: tk.Misc) -> None:
        if isinstance(widget, FindRow) or (isinstance(widget.master, FindRow) and isinstance(widget, tk.Entry)):
            return
        widget.bind("<F3>", self._on_f3, add="+")
        widget.bind("<Escape>", self._on_escape, add="+")
        widget.bind("<Control-f>", self._on_find, add="+")
        widget.bind("<Control-F>", self._on_find, add="+")

    def _on_f3(self, _event: Any = None) -> str:
        if self.tab == "diff":
            return self.next_hunk()
        if self.tab == "file" and self.find_row.matches:
            return self.find_row.step(1)
        return "break"

    def _on_escape(self, _event: Any = None) -> str:
        self.hide()
        return "break"

    def _on_find(self, _event: Any = None) -> str:
        if self.tab == "file":
            return self.open_find()
        return "break"


def _format_size(size: int) -> str:
    value = float(max(0, int(size)))
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{int(value)} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


class DiffChip(tk.Canvas):
    """Small ``+12 −3`` pill for a reply card footer; click shows the Diff tab."""

    def __init__(self, master: tk.Misc, app: Any, report: DiffReport | Any, on_click: Callable[[], None] | None = None, *, tooltip: str = "Show the workspace changes for this reply") -> None:
        theme = app.theme
        self.app = app
        self.theme = theme
        self.report = report if isinstance(report, DiffReport) else DiffReport.from_record(report)
        self.on_click = on_click
        self.text = self.report.chip_text
        self.font = app.fonts.small_bold
        self._hover = False
        self._focused = False
        parts = self._parts()
        self.padx = app.px(9)
        width = sum(self.font.measure(text) for text, _colour in parts) + self.font.measure(" ") * max(0, len(parts) - 1) + self.padx * 2
        height = self.font.metrics("linespace") + app.px(6)
        super().__init__(master, width=width, height=height, bd=0, highlightthickness=0, bg=RoundButton._parent_bg(master), cursor="hand2", takefocus=1)
        self._width, self._height = width, height
        self.bind("<Enter>", lambda _e: self._set_hover(True))
        self.bind("<Leave>", lambda _e: self._set_hover(False))
        self.bind("<Button-1>", lambda _e: self.invoke())
        self.bind("<Return>", lambda _e: self.invoke())
        self.bind("<space>", lambda _e: self.invoke())
        self.bind("<FocusIn>", lambda _e: self._set_focus(True))
        self.bind("<FocusOut>", lambda _e: self._set_focus(False))
        if tooltip:
            Tooltip(self, tooltip, app)
        self.redraw()

    def _parts(self) -> list[tuple[str, str]]:
        theme = self.theme
        if self.report.unavailable or not self.report.entries:
            return [(self.text, theme.faint)]
        return [(f"+{self.report.total_added}", theme.success), (f"−{self.report.total_removed}", theme.danger)]

    def redraw(self) -> None:
        self.delete("all")
        theme = self.theme
        fill = theme.surface_hover if self._hover else theme.surface_alt
        outline = theme.accent if self._focused else theme.border
        self.create_polygon(rounded_points(1, 1, self._width - 1, self._height - 1, self._height // 2), smooth=True, splinesteps=24, fill=fill, outline=outline, width=1)
        x = self.padx
        for text, colour in self._parts():
            self.create_text(x, self._height / 2, text=text, fill=colour, font=self.font, anchor="w")
            x += self.font.measure(text) + self.font.measure(" ")

    def _set_hover(self, inside: bool) -> None:
        self._hover = inside
        self.redraw()

    def _set_focus(self, focused: bool) -> None:
        self._focused = focused
        try:
            self.redraw()
        except tk.TclError:
            pass

    def invoke(self) -> str:
        if self.on_click is not None:
            self.on_click()
        return "break"
