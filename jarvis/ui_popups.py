"""JARVIS Desktop v3 composer popups: ``/`` commands, ``@`` files, the ``+`` menu, Snip.

Everything here hangs off the composer's ``GrowText`` and talks back only
through the callbacks it was given. No popup touches SQLite, the Agent, or
the workspace on the Tk thread: the ``@`` picker reads a :class:`FileIndex`
that the app builds on its worker thread, and :func:`run_snip` only watches
the clipboard after the OS overlay was launched. Nothing is shown that the
OS or the store did not do (a failed launch says so; a timeout says so).

Widgets and helpers:

* :data:`SLASH_COMMANDS`, :func:`match_slash`, :func:`is_unknown_slash` — the
  command set behind ``JarvisDesktop.handle_slash`` and its pure ranking.
* :class:`SlashPopup` — the filtered list under the composer while the caret
  is in a leading ``/token``.
* :class:`FileIndex`, :class:`AtPopup` — a bounded workspace index and the
  picker that opens on ``@`` at a token start.
* :class:`AttachMenu`, :func:`run_snip` — the ``+`` menu and the Snipping
  Tool round trip.
* :data:`DICTATION_HINT` — the Win+H hint for the composer.

Key handling never fights the composer's own bindings: each popup inserts a
private bindtag *before* the widget's tag, so while it is open it sees
Up/Down/Return/Tab/Escape first and returns ``"break"``; while it is closed
every handler returns ``None`` and the composer's bindings run as before.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from typing import Any, Callable

import tkinter as tk

from . import ui as _ui
from . import ui_win
from .ui import (
    WORKSPACE_SKIP_DIRECTORIES,
    classify_attachment,
    compact_activity,
    safe_ui_text,
)


DICTATION_HINT = "Win+H dictates into any box"

POPUP_FOCUS_DEBOUNCE_MS = 150
MAX_VISIBLE_ROWS = 8
FILE_INDEX_MAX_ENTRIES = 2000
AT_RESULT_LIMIT = 12
SNIP_POLL_MS = 400
SNIP_TIMEOUT_S = 60.0
SNIP_LAUNCH_FAILED = "Snipping Tool could not be launched"
KIND_GLYPHS = {"image": "\U0001f5bc", "text": "\U0001f4c4", "other": "▫"}


# --------------------------------------------------------------------------
# 1. Slash commands (pure)
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class SlashCommand:
    name: str
    usage: str
    description: str
    aliases: tuple[str, ...] = ()

    @property
    def names(self) -> tuple[str, ...]:
        return (self.name, *self.aliases)

    def is_exact(self, token: str) -> bool:
        return token.strip().lower() in self.names

    @property
    def arguments(self) -> str:
        """The usage text after ``/name`` (empty when the command takes none)."""
        head, _, rest = self.usage.partition(" ")
        return rest.strip() if head == f"/{self.name}" else self.usage


SLASH_COMMANDS: tuple[SlashCommand, ...] = (
    SlashCommand("new", "/new", "Start a new chat", ("n",)),
    SlashCommand("model", "/model auto | fast | reasoning | coding | deep", "Switch the model profile"),
    SlashCommand("theme", "/theme midnight | graphite | paper", "Change the theme (no argument cycles)"),
    SlashCommand("project", "/project [name]", "Choose a project, or create one by name"),
    SlashCommand("task", "/task <what to do in the background>", "Queue a background task for the worker"),
    SlashCommand("remember", "/remember [fact]", "Store a project fact through the guided dialog"),
    SlashCommand("export", "/export", "Export this chat to Markdown"),
    SlashCommand("council", "/council", "Open the Council room"),
    SlashCommand("context", "/context", "Toggle the context panel", ("ctx",)),
    SlashCommand("settings", "/settings", "Open settings", ("prefs",)),
    SlashCommand("facts", "/facts", "Open the memory view of project facts", ("memory",)),
    SlashCommand("schedule", "/schedule <prompt>", "Open routines; a prompt prefills a new routine", ("routine", "routines")),
    SlashCommand("inbox", "/inbox", "Open the inbox"),
    SlashCommand("help", "/help", "Show keyboard shortcuts", ("?",)),
)

_SLASH_TOKEN = re.compile(r"\A/(\S*)")


def slash_token(text: str) -> str | None:
    """The first token after a leading ``/``; None when ``text`` is not a slash line."""
    match = _SLASH_TOKEN.match(str(text or ""))
    return match.group(1) if match else None


def find_slash(token: str) -> SlashCommand | None:
    """The command whose name or alias equals ``token`` (case-insensitive)."""
    wanted = str(token or "").strip().lstrip("/").lower()
    if not wanted:
        return None
    for command in SLASH_COMMANDS:
        if wanted in command.names:
            return command
    return None


def _subsequence_span(haystack: str, needle: str) -> int | None:
    """Length of the tightest left-to-right embedding of ``needle`` in ``haystack``."""
    position = -1
    start = -1
    for char in needle:
        position = haystack.find(char, position + 1)
        if position < 0:
            return None
        if start < 0:
            start = position
    return position - start + 1


def match_slash(text: str) -> list[SlashCommand]:
    """Rank commands for ``text`` (``"/mo"``, ``"mo"``): exact > prefix > substring > subsequence.

    Aliases count the same as names. Ties keep :data:`SLASH_COMMANDS` order.
    An empty query lists every command.
    """
    token = slash_token(text)
    if token is None:
        token = str(text or "").strip().split(" ", 1)[0]
    query = token.lower()
    if not query:
        return list(SLASH_COMMANDS)
    ranked: list[tuple[int, int, SlashCommand]] = []
    for order, command in enumerate(SLASH_COMMANDS):
        best = 0
        for name in command.names:
            if name == query:
                score = 1000
            elif name.startswith(query):
                score = 800 - len(name)
            elif query in name:
                score = 600 - name.index(query) - len(name)
            else:
                span = _subsequence_span(name, query)
                score = 0 if span is None else 400 - span * 4 - len(name)
            best = max(best, score)
        if best > 0:
            ranked.append((-best, order, command))
    ranked.sort(key=lambda entry: (entry[0], entry[1]))
    return [command for _score, _order, command in ranked]


def is_unknown_slash(text: str) -> bool:
    """True when ``text`` is a slash line whose first token is not a known command.

    ``"/"`` alone and ``"/nope"`` are unknown; ``"/model fast"`` and plain
    prose are not. The composer uses it to refuse the send.
    """
    token = slash_token(text)
    if token is None:
        return False
    return find_slash(token) is None


# --------------------------------------------------------------------------
# 2. Workspace file index (worker-thread built, immutable afterwards)
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class FileEntry:
    path: str  # relative to the index root, forward slashes
    kind: str  # "text" | "image" | "other"
    mtime: float
    size: int

    @property
    def name(self) -> str:
        return self.path.rsplit("/", 1)[-1]


def _entry_kind(path: str) -> str:
    kind = classify_attachment(path)
    return kind if kind in ("text", "image") else "other"


class FileIndex:
    """A bounded, immutable snapshot of the files under one root.

    Build it on the worker thread with :meth:`build`; :meth:`search` is pure
    and safe from any thread.
    """

    def __init__(self, root: str, entries: list[FileEntry] | tuple[FileEntry, ...] = (), *, truncated: bool = False, built_at: float | None = None) -> None:
        self.root = str(root)
        self.entries: tuple[FileEntry, ...] = tuple(entries)
        self.truncated = bool(truncated)
        self.built_at = float(time.time() if built_at is None else built_at)

    def __len__(self) -> int:
        return len(self.entries)

    @classmethod
    def build(cls, root: Any, *, max_entries: int = FILE_INDEX_MAX_ENTRIES, skip: Any = WORKSPACE_SKIP_DIRECTORIES) -> "FileIndex":
        """Walk ``root`` breadth-first, skipping ``skip`` directory names and
        symlinks, stopping after ``max_entries`` files. Never raises."""
        root_text = str(root or "")
        limit = max(0, int(max_entries))
        skip_names = {str(name).lower() for name in (skip or ())}
        entries: list[FileEntry] = []
        truncated = False
        try:
            if not root_text or not os.path.isdir(root_text):
                return cls(root_text, entries, truncated=False)
            pending: list[str] = [""]
            visited_dirs = 0
            max_dirs = max(64, limit * 4)
            while pending and not truncated:
                relative_dir = pending.pop(0)
                visited_dirs += 1
                if visited_dirs > max_dirs:
                    truncated = True
                    break
                absolute_dir = os.path.join(root_text, relative_dir) if relative_dir else root_text
                try:
                    with os.scandir(absolute_dir) as listing:
                        children = sorted(listing, key=lambda item: item.name.lower())
                except OSError:
                    continue
                for item in children:
                    try:
                        if item.is_symlink():
                            continue
                        if item.is_dir(follow_symlinks=False):
                            if item.name.lower() in skip_names:
                                continue
                            pending.append(f"{relative_dir}/{item.name}" if relative_dir else item.name)
                            continue
                        if not item.is_file(follow_symlinks=False):
                            continue
                        stat = item.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    if len(entries) >= limit:
                        truncated = True
                        break
                    relative = f"{relative_dir}/{item.name}" if relative_dir else item.name
                    entries.append(FileEntry(relative.replace("\\", "/"), _entry_kind(item.name), float(stat.st_mtime), int(stat.st_size)))
        except Exception:
            truncated = True
        return cls(root_text, entries, truncated=truncated)

    @staticmethod
    def score(entry: FileEntry, query: str) -> int:
        """Pure rank: basename prefix > basename substring > path substring > subsequence."""
        if not query:
            return 1
        path = entry.path.lower()
        name = entry.name.lower()
        if name.startswith(query):
            return 900 - len(name)
        position = name.find(query)
        if position >= 0:
            return 700 - position - len(name)
        position = path.find(query)
        if position >= 0:
            return 500 - min(200, position) - min(100, len(path))
        span = _subsequence_span(path, query)
        if span is None:
            return 0
        return max(1, 300 - span - min(100, len(path)))

    def search(self, query: str, limit: int = AT_RESULT_LIMIT) -> list[FileEntry]:
        """Fuzzy-rank entries for ``query``; an empty query lists the newest files."""
        wanted = " ".join(str(query or "").split()).lower().replace("\\", "/")
        cap = max(0, int(limit))
        if not wanted:
            return sorted(self.entries, key=lambda entry: (-entry.mtime, entry.path))[:cap]
        ranked: list[tuple[int, int, str, FileEntry]] = []
        for entry in self.entries:
            score = self.score(entry, wanted)
            if score > 0:
                ranked.append((-score, len(entry.path), entry.path.lower(), entry))
        ranked.sort(key=lambda item: (item[0], item[1], item[2]))
        return [item[3] for item in ranked[:cap]]

    def absolute(self, entry: FileEntry) -> str:
        return os.path.normpath(os.path.join(self.root, entry.path.replace("/", os.sep)))


# --------------------------------------------------------------------------
# 3. Shared popup machinery
# --------------------------------------------------------------------------

class _ComposerKeys:
    """A private bindtag placed before the widget's own tag.

    Handlers bound here run before the composer's bindings and stop them by
    returning ``"break"``; returning None lets the composer's own handler run.
    """

    def __init__(self, widget: tk.Misc, handlers: dict[str, Callable[[Any], Any]]) -> None:
        self.widget = widget
        self.tag = f"JarvisPopupKeys{id(self)}"
        self.sequences = tuple(handlers)
        for sequence, handler in handlers.items():
            widget.bind_class(self.tag, sequence, handler)
        widget.bindtags((self.tag, *widget.bindtags()))

    def release(self) -> None:
        try:
            self.widget.bindtags(tuple(tag for tag in self.widget.bindtags() if tag != self.tag))
        except tk.TclError:
            pass
        for sequence in self.sequences:
            try:
                self.widget.unbind_class(self.tag, sequence)
            except tk.TclError:
                pass


class _ListPopup(tk.Toplevel):
    """Borderless, theme-painted list under (or above) an anchor widget.

    Never takes keyboard focus: the composer keeps it and forwards keys.
    """

    footer_text = ""

    def __init__(self, app: Any, anchor: tk.Misc) -> None:
        super().__init__(app.root)
        self.app = app
        self.theme = app.theme
        self.anchor = anchor
        self.items: list[Any] = []
        self.selected = 0
        self.offset = 0
        self.rows: list[tk.Frame] = []
        self.is_open = False
        self._focus_after: str | None = None
        self.withdraw()
        self.overrideredirect(True)
        try:
            self.attributes("-topmost", True)
        except tk.TclError:
            pass
        self.configure(bg=self.theme.border_strong)
        self.shell = tk.Frame(self, bg=self.theme.panel)
        self.shell.pack(fill="both", expand=True, padx=1, pady=1)
        self.body = tk.Frame(self.shell, bg=self.theme.panel)
        self.body.pack(fill="both", expand=True, padx=4, pady=(4, 0))
        self.footer = tk.Label(self.shell, text=self.footer_text, bg=self.theme.panel, fg=self.theme.faint, font=app.fonts.tiny, anchor="w", padx=10)
        self.footer.pack(fill="x", pady=(2, 5))
        self.bind("<ButtonPress-1>", self._return_focus, add="+")

    # -- geometry ------------------------------------------------------------

    def place(self) -> None:
        """Under the anchor when that fits inside the anchor's window, else above it.

        The composer sits at the bottom of the window, so in practice the
        list opens upward; a composer near the top still gets it below.
        """
        try:
            self.update_idletasks()
            anchor_x = self.anchor.winfo_rootx()
            anchor_y = self.anchor.winfo_rooty()
            anchor_h = self.anchor.winfo_height()
            width = max(self.app.px(360), min(self.anchor.winfo_width(), self.app.px(720)))
            height = max(self.app.px(40), self.winfo_reqheight())
            window = self.anchor.winfo_toplevel()
            bottom_limit = min(window.winfo_rooty() + window.winfo_height(), self.winfo_screenheight())
        except tk.TclError:
            return
        below = anchor_y + anchor_h + 4
        y = below if below + height <= bottom_limit - 8 else anchor_y - height - 4
        self.geometry(f"{width}x{height}+{max(0, anchor_x)}+{max(0, y)}")

    def show(self) -> None:
        self.place()
        if not self.is_open:
            self.is_open = True
            try:
                self.deiconify()
                self.lift()
            except tk.TclError:
                pass

    def hide(self) -> None:
        if self.is_open:
            self.is_open = False
            try:
                self.withdraw()
            except tk.TclError:
                pass

    def close(self) -> None:
        self.hide()

    # -- rows ----------------------------------------------------------------

    def _clear_rows(self) -> None:
        for child in self.body.winfo_children():
            child.destroy()
        self.rows = []

    def _empty_row(self, text: str) -> None:
        tk.Label(self.body, text=text, bg=self.theme.panel, fg=self.theme.faint, font=self.app.fonts.small, anchor="w", padx=10, pady=8).pack(fill="x")

    def _row(self, index: int, glyph: str, label: str, detail: str, right: str = "") -> tk.Frame:
        theme = self.theme
        fonts = self.app.fonts
        row = tk.Frame(self.body, bg=theme.panel, padx=8, pady=4, cursor="hand2")
        row.pack(fill="x")
        widgets = [row]
        if glyph:
            widgets.append(tk.Label(row, text=glyph, bg=theme.panel, fg=theme.accent, font=fonts.icon_small, width=2, anchor="w"))
            widgets[-1].pack(side="left")
        widgets.append(tk.Label(row, text=label, bg=theme.panel, fg=theme.text_strong, font=fonts.label_bold, anchor="w"))
        widgets[-1].pack(side="left")
        if right:
            widgets.append(tk.Label(row, text=right, bg=theme.panel, fg=theme.muted, font=fonts.mono_small, anchor="e"))
            widgets[-1].pack(side="right")
        if detail:
            widgets.append(tk.Label(row, text=detail, bg=theme.panel, fg=theme.faint, font=fonts.small, anchor="w", padx=10))
            widgets[-1].pack(side="left", fill="x", expand=True)
        for widget in widgets:
            widget.bind("<Button-1>", lambda _e, target=index: self.choose(target))
            widget.bind("<Enter>", lambda _e, target=index: self.select(target))
        self.rows.append(row)
        return row

    def _paint_selection(self) -> None:
        for position, row in enumerate(self.rows):
            color = self.theme.surface_hover if position + self.offset == self.selected else self.theme.panel
            try:
                row.configure(bg=color)
                for child in row.winfo_children():
                    child.configure(bg=color)
            except tk.TclError:
                pass

    def render(self) -> None:  # pragma: no cover - subclasses draw their rows
        raise NotImplementedError

    def select(self, index: int) -> None:
        if 0 <= index < len(self.items):
            self.selected = index
            self._paint_selection()

    def move(self, delta: int) -> None:
        if not self.items:
            return
        self.selected = (self.selected + delta) % len(self.items)
        if self.selected < self.offset or self.selected >= self.offset + MAX_VISIBLE_ROWS:
            self.offset = max(0, min(self.selected - (MAX_VISIBLE_ROWS - 1 if delta > 0 else 0), max(0, len(self.items) - MAX_VISIBLE_ROWS)))
            self.render()
            self.place()
        else:
            self._paint_selection()

    def choose(self, index: int) -> None:  # pragma: no cover - subclasses decide
        raise NotImplementedError

    # -- focus ---------------------------------------------------------------

    def _schedule_focus_check(self) -> None:
        if self._focus_after is not None:
            try:
                self.after_cancel(self._focus_after)
            except tk.TclError:
                pass
        try:
            self._focus_after = self.after(POPUP_FOCUS_DEBOUNCE_MS, self._close_if_unfocused)
        except tk.TclError:
            self._focus_after = None

    def _close_if_unfocused(self) -> None:
        """Close unless the composer text still owns the keyboard.

        The popup never takes focus on purpose: a click inside it hands focus
        straight back to the text (see :meth:`_return_focus`), so any other
        focus owner means the user moved on.
        """
        self._focus_after = None
        try:
            focused = self.anchor.focus_get()
        except (KeyError, tk.TclError):
            focused = None
        if focused is None or str(focused) != str(self.anchor):
            self.hide()

    def _return_focus(self, _event: Any = None) -> None:
        try:
            self.anchor.after_idle(self.anchor.focus_set)
        except tk.TclError:
            pass


# --------------------------------------------------------------------------
# 4. Slash popup
# --------------------------------------------------------------------------

class SlashPopup(_ListPopup):
    """The ``/`` command list. Construct once per composer; it binds itself.

    ``on_choose(command)`` fires whenever the popup completes a command into
    the composer (Tab, click, or Enter on an exact match). On an exact match
    the popup closes and lets the composer's own Return binding send.
    """

    footer_text = "↑↓ choose   ·   Tab completes   ·   Enter runs   ·   Esc closes"

    def __init__(self, app: Any, text: tk.Text, on_choose: Callable[[SlashCommand], None] | None = None) -> None:
        super().__init__(app, text)
        self.text = text
        self.on_choose = on_choose
        self.query: str | None = None
        self.dismissed: str | None = None
        self._keys = _ComposerKeys(text, {
            "<KeyRelease>": self._on_key_release,
            "<ButtonRelease-1>": self._on_key_release,
            "<FocusOut>": self._on_focus_out,
            "<Up>": lambda _e: self._on_move(-1),
            "<Down>": lambda _e: self._on_move(1),
            "<Return>": self._on_return,
            "<KP_Enter>": self._on_return,
            "<Tab>": self._on_tab,
            "<Escape>": self._on_escape,
        })

    def destroy(self) -> None:
        try:
            self._keys.release()
        except Exception:
            pass
        super().destroy()

    # -- state ---------------------------------------------------------------

    def token(self) -> str | None:
        """The leading ``/token`` when the caret sits inside it, else None."""
        try:
            line = self.text.get("1.0", "1.end")
            row, column = self.text.index("insert").split(".")
        except (tk.TclError, ValueError):
            return None
        match = _SLASH_TOKEN.match(line)
        if match is None or row != "1" or int(column) > match.end():
            return None
        return match.group(1)

    def sync(self) -> None:
        """Re-read the composer; open, refilter, or close. Call on every key."""
        token = self.token()
        if token is None:
            self.query = None
            self.dismissed = None
            self.hide()
            return
        if self.dismissed is not None:
            if token == self.dismissed:
                return
            self.dismissed = None
        if token != self.query:
            self.query = token
            self.items = match_slash(token)
            self.selected = 0
            self.offset = 0
            self.render()
        self.show()

    def render(self) -> None:
        self._clear_rows()
        if not self.items:
            self._empty_row(f"No command named /{compact_activity(self.query or '', 40)} — Enter will not send it")
        for position, command in enumerate(self.items[self.offset:self.offset + MAX_VISIBLE_ROWS]):
            self._row(self.offset + position, "", f"/{command.name}", command.description, command.arguments)
        hidden = len(self.items) - min(len(self.items), self.offset + MAX_VISIBLE_ROWS)
        if hidden > 0:
            self._empty_row(f"… {hidden} more — keep typing or press ↓")
        self._paint_selection()

    # -- actions ---------------------------------------------------------------

    def complete(self, command: SlashCommand) -> None:
        """Replace the first token with ``/name`` + one space and move the caret after it."""
        try:
            line = self.text.get("1.0", "1.end")
            match = _SLASH_TOKEN.match(line)
            end = match.end() if match else 0
            self.text.delete("1.0", f"1.{end}")
            self.text.insert("1.0", f"/{command.name}")
            after = self.text.get(f"1.{len(command.name) + 1}", f"1.{len(command.name) + 2}")
            if after != " ":
                self.text.insert(f"1.{len(command.name) + 1}", " ")
            self.text.mark_set("insert", f"1.{len(command.name) + 2}")
        except tk.TclError:
            return
        self.hide()
        self.query = None
        if self.on_choose is not None:
            self.on_choose(command)
        self.sync()

    def choose(self, index: int) -> None:
        if 0 <= index < len(self.items):
            self.complete(self.items[index])

    def complete_selected(self) -> bool:
        if not self.items:
            return False
        self.complete(self.items[self.selected])
        return True

    def dismiss(self) -> None:
        self.dismissed = self.token()
        self.hide()

    # -- key handlers (private bindtag; None = let the composer handle it) ------

    def _on_key_release(self, _event: Any) -> None:
        self.sync()
        return None

    def _on_focus_out(self, _event: Any) -> None:
        if self.is_open:
            self._schedule_focus_check()
        return None

    def _on_move(self, delta: int) -> str | None:
        if not self.is_open:
            return None
        self.move(delta)
        return "break"

    def _on_return(self, _event: Any) -> str | None:
        if not self.is_open:
            return None
        token = self.token() or ""
        exact = next((command for command in self.items if command.is_exact(token)), None)
        if exact is not None:
            self.hide()
            self.query = None
            if self.on_choose is not None:
                self.on_choose(exact)
            return None  # the composer sends; handle_slash runs the command
        if self.items:
            self.complete(self.items[self.selected])
            return "break"
        return None  # unknown: the composer's guard refuses the send

    def _on_tab(self, _event: Any) -> str | None:
        if not self.is_open:
            return None
        self.complete_selected()
        return "break"

    def _on_escape(self, _event: Any) -> str | None:
        if not self.is_open:
            return None
        self.dismiss()
        return "break"


# --------------------------------------------------------------------------
# 5. @ file picker
# --------------------------------------------------------------------------

_AT_TOKEN = re.compile(r"(?:\A|(?<=\s))@(\S*)\Z")


def at_token(before_caret: str) -> tuple[int, str] | None:
    """``(offset, query)`` for an ``@token`` ending at the caret, else None."""
    match = _AT_TOKEN.search(str(before_caret or ""))
    if match is None:
        return None
    return match.start(), match.group(1)


class AtPopup(_ListPopup):
    """The ``@`` workspace-file picker.

    ``index_provider()`` returns the current :class:`FileIndex` or None while
    the worker is still building it. ``on_choose(path)`` receives the absolute
    path; call :meth:`remove_token` from it to delete the ``@token`` (the
    popup records the span and the composer owns the text).
    """

    footer_text = "↑↓ choose   ·   Enter / Tab attaches   ·   Esc closes"

    def __init__(self, app: Any, text: tk.Text, index_provider: Callable[[], FileIndex | None], on_choose: Callable[[str], None] | None = None) -> None:
        super().__init__(app, text)
        self.text = text
        self.index_provider = index_provider
        self.on_choose = on_choose
        self.query: str | None = None
        self.index: FileIndex | None = None
        self.token_span: tuple[str, str] | None = None
        self.dismissed: str | None = None
        self._keys = _ComposerKeys(text, {
            "<KeyRelease>": self._on_key_release,
            "<ButtonRelease-1>": self._on_key_release,
            "<FocusOut>": self._on_focus_out,
            "<Up>": lambda _e: self._on_move(-1),
            "<Down>": lambda _e: self._on_move(1),
            "<Return>": self._on_return,
            "<KP_Enter>": self._on_return,
            "<Tab>": self._on_tab,
            "<Escape>": self._on_escape,
        })

    def destroy(self) -> None:
        try:
            self._keys.release()
        except Exception:
            pass
        super().destroy()

    # -- state ---------------------------------------------------------------

    def token(self) -> tuple[tuple[str, str], str] | None:
        """``((start_index, end_index), query)`` for the ``@token`` at the caret."""
        try:
            before = self.text.get("1.0", "insert")
            after = self.text.get("insert", "end-1c")
        except tk.TclError:
            return None
        found = at_token(before)
        if found is None:
            return None
        offset, query = found
        tail = re.match(r"\S*", after)
        length = 1 + len(query) + (len(tail.group(0)) if tail else 0)
        return (f"1.0+{offset}c", f"1.0+{offset + length}c"), query

    def sync(self) -> None:
        found = self.token()
        if found is None:
            self.query = None
            self.token_span = None
            self.dismissed = None
            self.hide()
            return
        span, query = found
        self.token_span = span
        key = f"{span[0]}|{query}"
        if self.dismissed is not None:
            if key == self.dismissed:
                return
            self.dismissed = None
        index = None
        try:
            index = self.index_provider()
        except Exception:
            index = None
        changed = query != self.query or index is not self.index
        self.index = index
        if changed:
            self.query = query
            self.items = index.search(query, AT_RESULT_LIMIT) if index is not None else []
            self.selected = 0
            self.offset = 0
            self.render()
        self.show()

    def render(self) -> None:
        self._clear_rows()
        if self.index is None:
            self._empty_row("Workspace index not ready yet")
        elif not self.items:
            what = compact_activity(self.query or "", 40)
            self._empty_row(f"No workspace file matches “{what}”" if what else "No files in the workspace index")
        for position, entry in enumerate(self.items[self.offset:self.offset + MAX_VISIBLE_ROWS]):
            self._row(self.offset + position, KIND_GLYPHS.get(entry.kind, KIND_GLYPHS["other"]), entry.name, safe_ui_text(entry.path, 200), entry.kind)
        hidden = len(self.items) - min(len(self.items), self.offset + MAX_VISIBLE_ROWS)
        if hidden > 0:
            self._empty_row(f"… {hidden} more — keep typing or press ↓")
        if self.index is not None and self.index.truncated:
            self._empty_row(f"Index stopped at {len(self.index.entries):,} files — deeper paths are not listed")
        self._paint_selection()

    # -- actions ---------------------------------------------------------------

    def remove_token(self) -> None:
        """Delete the ``@token`` the last choice referred to (the composer's call)."""
        span = self.token_span
        if span is None:
            return
        try:
            self.text.delete(span[0], span[1])
            self.text.mark_set("insert", span[0])
        except tk.TclError:
            pass
        self.token_span = None

    def choose(self, index: int) -> None:
        if not (0 <= index < len(self.items)) or self.index is None:
            return
        entry = self.items[index]
        path = self.index.absolute(entry)
        self.hide()
        self.query = None
        if self.on_choose is not None:
            self.on_choose(path)
        found = self.token()
        if found is not None:
            # The composer left the @token in place: stay closed until it changes.
            self.dismissed = f"{found[0][0]}|{found[1]}"
        self.sync()

    def choose_selected(self) -> bool:
        if not self.items:
            return False
        self.choose(self.selected)
        return True

    def dismiss(self) -> None:
        found = self.token()
        self.dismissed = f"{found[0][0]}|{found[1]}" if found else None
        self.hide()

    # -- key handlers -----------------------------------------------------------

    def _on_key_release(self, _event: Any) -> None:
        self.sync()
        return None

    def _on_focus_out(self, _event: Any) -> None:
        if self.is_open:
            self._schedule_focus_check()
        return None

    def _on_move(self, delta: int) -> str | None:
        if not self.is_open:
            return None
        self.move(delta)
        return "break"

    def _on_return(self, _event: Any) -> str | None:
        if not self.is_open:
            return None
        if self.choose_selected():
            return "break"
        self.hide()
        return None  # nothing to attach: the composer sends the text as typed

    def _on_tab(self, _event: Any) -> str | None:
        if not self.is_open:
            return None
        self.choose_selected()
        return "break"

    def _on_escape(self, _event: Any) -> str | None:
        if not self.is_open:
            return None
        self.dismiss()
        return "break"


# --------------------------------------------------------------------------
# 6. The + menu and the Snip round trip
# --------------------------------------------------------------------------

ATTACH_ENTRIES: tuple[tuple[str, str, str, str], ...] = (
    ("files", "\U0001f4c1", "Files…", "Pick files or images from disk"),
    ("snip", "✂", "Snip screen", "Snipping Tool copies your selection; Jarvis attaches it"),
    ("paste", "\U0001f4cb", "Paste", "Clipboard image or copied file paths"),
)


class AttachMenu(tk.Toplevel):
    """The composer's ``+`` menu: a themed popup listing Files… · Snip screen · Paste.

    Shown on construction above (or below) ``anchor``; takes focus so
    Up/Down/Enter/Esc work; closes on Esc, on a choice, and on focus-out.
    Only the entries whose key is in ``callbacks`` are listed.
    """

    def __init__(self, app: Any, anchor: tk.Misc, callbacks: dict[str, Callable[[], None]]) -> None:
        super().__init__(app.root)
        self.app = app
        self.theme = theme = app.theme
        self.anchor = anchor
        self.callbacks = dict(callbacks)
        self.entries = [entry for entry in ATTACH_ENTRIES if entry[0] in self.callbacks]
        self.selected = 0
        self.rows: list[tk.Frame] = []
        self.closed = False
        self._focus_after: str | None = None
        self.withdraw()
        self.overrideredirect(True)
        try:
            self.attributes("-topmost", True)
        except tk.TclError:
            pass
        self.configure(bg=theme.border_strong)
        shell = tk.Frame(self, bg=theme.panel)
        shell.pack(fill="both", expand=True, padx=1, pady=1)
        self.body = tk.Frame(shell, bg=theme.panel)
        self.body.pack(fill="both", expand=True, padx=4, pady=4)
        for index, (_key, glyph, label, detail) in enumerate(self.entries):
            row = tk.Frame(self.body, bg=theme.panel, padx=8, pady=5, cursor="hand2")
            row.pack(fill="x")
            glyph_label = tk.Label(row, text=glyph, bg=theme.panel, fg=theme.accent, font=app.fonts.icon_small, width=2, anchor="w")
            glyph_label.pack(side="left")
            text_box = tk.Frame(row, bg=theme.panel)
            text_box.pack(side="left", fill="x", expand=True)
            title = tk.Label(text_box, text=label, bg=theme.panel, fg=theme.text_strong, font=app.fonts.label_bold, anchor="w")
            title.pack(fill="x")
            hint = tk.Label(text_box, text=detail, bg=theme.panel, fg=theme.faint, font=app.fonts.tiny, anchor="w")
            hint.pack(fill="x")
            for widget in (row, glyph_label, text_box, title, hint):
                widget.bind("<Button-1>", lambda _e, target=index: self.choose(target))
                widget.bind("<Enter>", lambda _e, target=index: self.select(target))
            self.rows.append(row)
        if not self.entries:
            tk.Label(self.body, text="Nothing to attach from here", bg=theme.panel, fg=theme.faint, font=app.fonts.small, padx=10, pady=8).pack()
        self.bind("<Up>", lambda _e: self.move(-1))
        self.bind("<Down>", lambda _e: self.move(1))
        self.bind("<Return>", lambda _e: self.activate())
        self.bind("<KP_Enter>", lambda _e: self.activate())
        self.bind("<space>", lambda _e: self.activate())
        self.bind("<Escape>", lambda _e: self.close())
        self.bind("<FocusOut>", self._on_focus_out)
        self._paint_selection()
        self._place()
        try:
            self.deiconify()
            self.lift()
            self.after(10, self._take_focus)
        except tk.TclError:
            pass

    def _take_focus(self) -> None:
        if self.closed:
            return
        try:
            self.focus_force()
        except tk.TclError:
            pass

    def _place(self) -> None:
        try:
            self.update_idletasks()
            width = max(self.app.px(280), self.winfo_reqwidth())
            height = self.winfo_reqheight()
            x = self.anchor.winfo_rootx()
            above = self.anchor.winfo_rooty() - height - 6
            below = self.anchor.winfo_rooty() + self.anchor.winfo_height() + 6
            screen_h = self.winfo_screenheight()
            y = above if above >= 0 else (below if below + height <= screen_h else 0)
            self.geometry(f"{width}x{height}+{max(0, x)}+{max(0, y)}")
        except tk.TclError:
            pass

    def _paint_selection(self) -> None:
        for index, row in enumerate(self.rows):
            color = self.theme.surface_hover if index == self.selected else self.theme.panel
            try:
                row.configure(bg=color)
                for child in row.winfo_children():
                    child.configure(bg=color)
                    for grandchild in child.winfo_children():
                        grandchild.configure(bg=color)
            except tk.TclError:
                pass

    def select(self, index: int) -> None:
        if 0 <= index < len(self.entries):
            self.selected = index
            self._paint_selection()

    def move(self, delta: int) -> str:
        if self.entries:
            self.selected = (self.selected + delta) % len(self.entries)
            self._paint_selection()
        return "break"

    def activate(self) -> str:
        self.choose(self.selected)
        return "break"

    def choose(self, target: int | str) -> None:
        """Close, then run the callback for the entry at ``target`` (index or key)."""
        if isinstance(target, str):
            index = next((position for position, entry in enumerate(self.entries) if entry[0] == target), -1)
        else:
            index = int(target)
        if not (0 <= index < len(self.entries)):
            return
        key = self.entries[index][0]
        callback = self.callbacks.get(key)
        self.close()
        if callback is not None:
            callback()

    def _on_focus_out(self, _event: Any) -> None:
        if self._focus_after is not None:
            try:
                self.after_cancel(self._focus_after)
            except tk.TclError:
                pass
        try:
            self._focus_after = self.after(POPUP_FOCUS_DEBOUNCE_MS, self._close_if_unfocused)
        except tk.TclError:
            self._focus_after = None

    def _close_if_unfocused(self) -> None:
        self._focus_after = None
        if self.closed:
            return
        try:
            focused = self.focus_get()
        except (KeyError, tk.TclError):
            focused = None
        if focused is None or not str(focused).startswith(str(self)):
            self.close()

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            self.destroy()
        except tk.TclError:
            pass


def snip_timeout_message(timeout_s: float) -> str:
    return f"No screenshot arrived within {float(timeout_s):g} s"


def run_snip(
    app: Any,
    on_png: Callable[[bytes], None],
    on_fail: Callable[[str], None],
    *,
    timeout_s: float = SNIP_TIMEOUT_S,
    poll_ms: int = SNIP_POLL_MS,
) -> bool:
    """Launch the Windows snipping overlay and hand the resulting PNG to ``on_png``.

    Records the clipboard sequence number, calls :func:`jarvis.ui_win.launch_snip`
    (``on_fail`` at once when it returns False), then polls on the Tk thread
    with ``app.root.after`` every ``poll_ms`` for a *new* clipboard image
    (sequence changed and :func:`jarvis.ui.clipboard_image_png` returns bytes).
    Sequence changes without an image (text copied) move the baseline. Never
    blocks; ``on_fail`` on timeout. Returns True when polling started.
    """
    try:
        baseline = int(ui_win.clipboard_sequence())
    except Exception:
        baseline = 0
    launched = False
    try:
        launched = bool(ui_win.launch_snip())
    except Exception:
        launched = False
    if not launched:
        on_fail(SNIP_LAUNCH_FAILED)
        return False
    deadline = time.monotonic() + max(0.0, float(timeout_s))
    interval = max(10, int(poll_ms))
    state = {"baseline": baseline}

    def poll() -> None:
        try:
            current = int(ui_win.clipboard_sequence())
        except Exception:
            current = state["baseline"]
        if current != state["baseline"]:
            try:
                png = _ui.clipboard_image_png()
            except Exception:
                png = None
            if png:
                on_png(bytes(png))
                return
            state["baseline"] = current
        if time.monotonic() >= deadline:
            on_fail(snip_timeout_message(timeout_s))
            return
        try:
            app.root.after(interval, poll)
        except tk.TclError:
            on_fail(snip_timeout_message(timeout_s))

    try:
        app.root.after(interval, poll)
    except tk.TclError:
        on_fail(snip_timeout_message(timeout_s))
        return False
    return True
