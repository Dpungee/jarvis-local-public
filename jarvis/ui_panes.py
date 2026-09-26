"""JARVIS Desktop v3 panes: Memory, Routines, the Inbox popover and the Companion.

Every widget here is fed by the app (:class:`jarvis.ui.JarvisDesktop`) through
``on_event(kind, payload)`` and talks back only through the worker commands on
``app.session`` and the app methods named in the desktop v3 contract. Nothing
in this module touches SQLite or the Agent, and no pane ever shows a state the
store did not report: an empty list is rendered as empty, a missing field is
left out, and the receipts for governed-memory commands stay in the chat.

The four panes:

* :class:`MemoryView` — the ``/facts`` view over governed project facts.
* :class:`RoutinesView` — scheduled jobs with an inline creation form.
* :class:`InboxPopover` — the borderless list under the bell.
* :class:`CompanionWindow` — the always-on-top quick-ask box.

Plus :class:`MarkdownBox`, the compact markdown renderer the companion uses so
a reply looks the same there as in the main chat.

Every ``after`` a pane schedules goes through :class:`AfterMixin` and is
cancelled when the widget is destroyed, so closing the root never leaves a
timer behind to fire on a dead window (Tk would print a background error).
"""

from __future__ import annotations

import json
import re
import time
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import tkinter as tk
from tkinter import ttk

from .ui import (
    ApprovalCard,
    AutoText,
    GrowText,
    IconButton,
    RoundButton,
    STANDING_UNAVAILABLE_TEXT,
    ScrollFrame,
    Tooltip,
    _apply_titlebar_theme,
    _iso_to_epoch,
    clipboard_image_png,
    compact_activity,
    decision_from_row,
    format_clock,
    format_elapsed,
    format_until,
    inline_runs,
    parse_markdown,
    render_table_text,
    safe_http_url,
    safe_ui_text,
)


ERASE_FACT_PREFIX = "Erase this project fact: "
FILTER_DEBOUNCE_MS = 250
POPOVER_FOCUS_DEBOUNCE_MS = 150
ROUTINE_DELETE_UNDO_MS = 5000
COPY_FEEDBACK_MS = 1400
CADENCE_PRESETS: tuple[tuple[str, int | None], ...] = (
    ("Every hour", 60),
    ("Every 6 h", 360),
    ("Every day", 1440),
    ("Every week", 10080),
    ("Custom minutes", None),
)
CUSTOM_CADENCE_LABEL = "Custom minutes"
INBOX_SECTIONS: tuple[tuple[str, frozenset[str]], ...] = (
    ("Needs you", frozenset({"approval"})),
    ("Unread replies", frozenset({"unread"})),
    ("Finished tasks & routine runs", frozenset({"task", "routine"})),
    ("Errors", frozenset({"error", "worker"})),
)
HISTORY_GLYPHS = {"current": "●", "superseded": "○", "retracted": "✕"}
WORKER_OFFLINE_TEXT = "Runs will not start until `python -m jarvis worker` is running."
WORKER_UNKNOWN_TEXT = "Runs will not start until `python -m jarvis worker` is running (no worker heartbeat found)."
COMPANION_MAX_REPLY_LINES = 12
COMPANION_WIDTH = 640
COMPANION_HEIGHT = 160
COMPANION_MAX_IMAGES = 4
COMPANION_BUSY_TEXT = "Stop or finish the current request first."
COMPANION_NOTHING_TO_PASTE = "Nothing to paste: the clipboard holds neither text nor an image."
COMPANION_APPROVAL_UNDECIDED_TEXT = "Already decided"
COMPANION_OPEN_APPROVAL_TEXT = "Open in Jarvis"
COMPANION_UNSEEN_TARGET_TEXT = "The exact target can't be shown here. Open it in Jarvis to approve, or deny."
COMBOBOX_STYLE = "Jarvis.TCombobox"
# The shared card's compact wrap is sized for the 250 px context panel; the
# companion is COMPANION_WIDTH wide, so its labels are re-wrapped to this.
COMPANION_CARD_WRAP = COMPANION_WIDTH - 72
_GEOMETRY_RE = re.compile(r"^\d+x\d+[+-]\d+[+-]\d+$")
_MODEL_SOURCE_MARKERS = ("propos", "model", "assist", "learned", "extract", "chat")
_OPERATOR_SOURCE_MARKERS = ("operator", "explicit", "verified")


# --------------------------------------------------------------------------
# Pure helpers
# --------------------------------------------------------------------------

def format_interval(minutes: Any) -> str:
    """Render a routine cadence the way the form offers it."""
    try:
        value = int(minutes)
    except (TypeError, ValueError):
        return "Every ? min"
    if value == 60:
        return "Every hour"
    if value == 360:
        return "Every 6 h"
    if value == 1440:
        return "Every day"
    if value == 10080:
        return "Every week"
    return f"Every {value} min"


def group_claims(rows: list[dict[str, Any]]) -> list[tuple[str, list[dict[str, Any]]]]:
    """Group claim rows by subject, keeping the store's order of first appearance."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        subject = str(row.get("subject", "") or "")
        groups.setdefault(subject, []).append(row)
    return list(groups.items())


def erase_fact_command(subject: str, predicate: str) -> str:
    """The exact governed-memory erase command (subject and predicate only)."""
    payload = json.dumps(
        {"subject": str(subject), "predicate": str(predicate)},
        ensure_ascii=False, separators=(", ", ": "),
    )
    return ERASE_FACT_PREFIX + payload


def _now() -> float:
    """The wall clock every relative time in this module is measured against.

    Module-level so tests can freeze it; the ``now`` parameters below take
    precedence for a single call.
    """
    return time.time()


def approval_decision_text(
    approved: bool,
    scope: str,
    *,
    until: float | None = None,
    reason_sent: bool = False,
    now: float | None = None,
) -> str:
    """The §4 decided-state line, worded exactly as the main window words it
    (``JarvisDesktop._decision_label``): ``Approved once · 14:02`` /
    ``Allowed in this chat until tomorrow 14:02`` / ``Always allowed`` /
    ``Denied`` (``· reason sent``)."""
    when = _now() if now is None else now
    clock = format_clock(when)
    if not approved:
        return "Denied" + (" · reason sent" if reason_sent else "")
    if scope == "session":
        if until:
            return f"Allowed in this chat until {format_until(until, datetime.fromtimestamp(when))}"
        return f"Allowed in this chat until tomorrow {clock}"
    if scope == "always":
        return "Always allowed"
    return f"Approved once · {clock}"


def relative_age(stamp: Any, now: float | None = None) -> str:
    """``just now`` / ``5m ago`` / ``3h ago`` / ``2d ago`` for an epoch value."""
    try:
        moment = float(stamp or 0.0)
    except (TypeError, ValueError):
        return ""
    if moment <= 0:
        return ""
    reference = _now() if now is None else float(now)
    delta = max(0.0, reference - moment)
    if delta < 60:
        return "just now"
    if delta < 3600:
        return f"{int(delta // 60)}m ago"
    if delta < 86400:
        return f"{int(delta // 3600)}h ago"
    return f"{int(delta // 86400)}d ago"


def _to_epoch(value: Any) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return _iso_to_epoch(str(value or ""))


def _relative_since(value: Any, now: float | None = None) -> str:
    """``relative_age`` over the ISO-8601 (or epoch) stamps the store emits."""
    return relative_age(_to_epoch(value), now)


def format_next_run(value: Any, now: float | None = None) -> str:
    """A future time as ``in 5m`` / ``in 3h`` / a clock, or ``not scheduled``."""
    stamp = _to_epoch(value)
    if not stamp:
        return "not scheduled"
    reference = _now() if now is None else float(now)
    delta = stamp - reference
    if delta <= 0:
        return "due now"
    minutes = int(round(delta / 60))
    if minutes < 60:
        return f"in {max(1, minutes)}m"
    if delta < 86400:
        return f"in {max(1, int(round(delta / 3600)))}h"
    return format_clock(stamp) or "later"


def cadence_minutes(label: str, custom: str) -> tuple[int | None, str]:
    """Map a cadence choice to minutes; the second item is an inline error."""
    choice = str(label or "").strip()
    for name, minutes in CADENCE_PRESETS:
        if name == choice and minutes is not None:
            return minutes, ""
    if choice != CUSTOM_CADENCE_LABEL:
        return None, "Choose a cadence."
    text = str(custom or "").strip()
    if not text.isdigit() or int(text) < 1:
        return None, "Custom cadence must be a whole number of minutes (at least 1)."
    return int(text), ""


def fact_source_label(row: dict[str, Any]) -> tuple[str, str]:
    """``(display, raw)`` for a claim row's provenance.

    The display collapses the store's ``source`` / ``actor`` (authority)
    enums into the contract's two words — ``operator`` or ``model-proposed``
    (plus `` · confirmed`` when the row says the proposal was confirmed) — and
    the raw string keeps every enum verbatim for a tooltip. A row whose
    provenance matches neither family shows the raw source so nothing is
    silently relabelled.
    """
    source = str(row.get("source") or "").strip()
    actor = str(row.get("actor") or row.get("authority") or "").strip()
    status = str(row.get("status") or "").strip()
    haystack = f"{source} {actor}".lower()
    if any(marker in haystack for marker in _MODEL_SOURCE_MARKERS):
        label = "model-proposed"
        if _fact_confirmed(row, source, status):
            label += " · confirmed"
    elif any(marker in haystack for marker in _OPERATOR_SOURCE_MARKERS):
        label = "operator"
    else:
        label = compact_activity(source or actor, 40)
    raw = " · ".join(
        f"{name}: {compact_activity(value, 80)}"
        for name, value in (("source", source), ("actor", actor), ("status", status))
        if value
    )
    return label, raw


def _fact_confirmed(row: dict[str, Any], source: str, status: str) -> bool:
    if row.get("confirmed") is True:
        return True
    confirmation = str(row.get("confirmation") or "").strip().lower()
    return "confirm" in status.lower() or "confirm" in source.lower() or confirmation == "confirmed"


def companion_image_path(folder: str, stamp: float | None = None) -> Path:
    """A fresh ``companion-<timestamp>.png`` path under ``folder`` (never overwrites)."""
    moment = datetime.fromtimestamp(_now() if stamp is None else float(stamp))
    base = Path(str(folder or "."))
    stem = f"companion-{moment:%Y%m%d-%H%M%S}"
    candidate = base / f"{stem}.png"
    counter = 2
    while candidate.exists():
        candidate = base / f"{stem}-{counter}.png"
        counter += 1
    return candidate


def save_companion_image(folder: str) -> str | None:
    """Write the clipboard bitmap as a PNG for the companion; ``None`` when the
    clipboard holds no image (blocking; run off the Tk thread when possible)."""
    png = clipboard_image_png()
    if not png:
        return None
    target = companion_image_path(folder)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(png)
    return str(target)


def _project_name(app: Any, project_id: Any) -> str:
    if project_id is None:
        return ""
    for project in getattr(app, "projects", None) or []:
        if isinstance(project, dict) and project.get("id") == project_id:
            return str(project.get("name") or "")
    return ""


def _cancel_after(widget: tk.Misc, handle: str | None) -> None:
    if handle is None:
        return
    try:
        widget.after_cancel(handle)
    except (tk.TclError, ValueError):
        pass


def _match(row: dict[str, Any], query: str) -> bool:
    if not query:
        return True
    needle = query.lower()
    for key in ("subject", "predicate", "value"):
        if needle in str(row.get(key, "") or "").lower():
            return True
    return False


# --------------------------------------------------------------------------
# Small shared widgets
# --------------------------------------------------------------------------

def themed_combobox(master: tk.Misc, **kwargs: Any) -> ttk.Combobox:
    """A ``ttk.Combobox`` in the desktop's ``Jarvis.TCombobox`` style (field,
    text, arrow and list colours follow the theme). The style is defined by
    ``JarvisDesktop._configure_style``; ttk tolerates the name before it is
    defined (the box just looks plain), and should an interpreter reject it
    the box is built plain rather than failing."""
    try:
        return ttk.Combobox(master, style=COMBOBOX_STYLE, **kwargs)
    except tk.TclError:
        return ttk.Combobox(master, **kwargs)


class AfterMixin:
    """Track every ``after`` a widget schedules so ``destroy`` can cancel them.

    Use ``self._later(ms, callback)`` instead of ``self.after`` and
    ``self._cancel_later(handle)`` instead of ``after_cancel``; a subclass's
    ``destroy`` calls ``self._cancel_all_later()`` before the widget goes.
    """

    _afters: set[str]

    def _init_afters(self) -> None:
        self._afters = set()

    def _later(self, ms: int, callback: Callable[[], None]) -> str | None:
        handles = self.__dict__.setdefault("_afters", set())
        holder: dict[str, str] = {}

        def fire() -> None:
            handle = holder.get("handle")
            if handle is not None:
                handles.discard(handle)
            callback()

        try:
            handle = self.after(ms, fire)  # type: ignore[attr-defined]
        except tk.TclError:
            return None
        holder["handle"] = handle
        handles.add(handle)
        return handle

    def _cancel_later(self, handle: str | None) -> None:
        if handle is None:
            return
        self.__dict__.setdefault("_afters", set()).discard(handle)
        _cancel_after(self, handle)  # type: ignore[arg-type]

    def _cancel_all_later(self) -> None:
        handles = self.__dict__.setdefault("_afters", set())
        for handle in list(handles):
            _cancel_after(self, handle)  # type: ignore[arg-type]
        handles.clear()

    def pending_afters(self) -> int:
        """How many scheduled callbacks are still outstanding (for tests)."""
        return len(self.__dict__.get("_afters", ()))


class ActionLink(tk.Label):
    """A flat text action for dense rows (lighter than a canvas button)."""

    def __init__(
        self,
        master: tk.Misc,
        app: Any,
        text: str,
        command: Callable[[], None] | None,
        *,
        color: str | None = None,
        tooltip: str | None = None,
    ) -> None:
        theme = app.theme
        self.command = command
        self.base_bg = RoundButton._parent_bg(master)
        self.color = color or theme.muted
        super().__init__(
            master, text=text, font=app.fonts.small, fg=self.color, bg=self.base_bg,
            cursor="hand2", padx=6, pady=2, takefocus=1,
            highlightthickness=1, highlightbackground=self.base_bg, highlightcolor=theme.accent,
        )
        self.bind("<Enter>", lambda _e: self.configure(bg=theme.surface_hover, fg=theme.text))
        self.bind("<Leave>", lambda _e: self.configure(bg=self.base_bg, fg=self.color))
        self.bind("<Button-1>", lambda _e: self.invoke())
        self.bind("<Return>", lambda _e: self.invoke())
        self.bind("<space>", lambda _e: self.invoke())
        if tooltip:
            Tooltip(self, tooltip, app)

    def invoke(self) -> str:
        if self.command is not None:
            self.command()
        return "break"


class PlaceholderEntry(tk.Entry):
    """An Entry with a muted placeholder that never leaks into ``value()``."""

    def __init__(self, master: tk.Misc, app: Any, placeholder: str, **kwargs: Any) -> None:
        theme = app.theme
        self.placeholder = placeholder
        self.text_color = theme.text
        self.placeholder_color = theme.faint
        self._showing_placeholder = False
        super().__init__(
            master, bg=theme.surface, fg=theme.text, insertbackground=theme.accent, bd=0,
            highlightthickness=1, highlightbackground=theme.border_strong,
            highlightcolor=theme.accent, font=app.fonts.body, **kwargs,
        )
        self.bind("<FocusIn>", self._on_focus_in, add="+")
        self.bind("<FocusOut>", self._on_focus_out, add="+")
        self._show_placeholder()

    def _show_placeholder(self) -> None:
        if not super().get():
            self._showing_placeholder = True
            self.configure(fg=self.placeholder_color)
            super().insert(0, self.placeholder)

    def _clear_placeholder(self) -> None:
        if self._showing_placeholder:
            self._showing_placeholder = False
            super().delete(0, "end")
            self.configure(fg=self.text_color)

    def _on_focus_in(self, _event: Any) -> None:
        self._clear_placeholder()

    def _on_focus_out(self, _event: Any) -> None:
        self._show_placeholder()

    def value(self) -> str:
        return "" if self._showing_placeholder else super().get()

    def set_value(self, text: str) -> None:
        self._clear_placeholder()
        super().delete(0, "end")
        if text:
            super().insert(0, text)
        else:
            try:
                focused = self.focus_get() is self
            except (KeyError, tk.TclError):
                focused = False
            if not focused:
                self._show_placeholder()


class ReplyScroller(tk.Frame):
    """A viewport that is sized to its content up to a ceiling, then scrolls.

    Unlike :class:`jarvis.ui.ScrollFrame` the scrollbar is never toggled from
    a scroll callback: ``fit`` decides once, from the measured content, whether
    the bar is needed. Toggling it inside ``yscrollcommand`` re-wraps the text
    (the canvas narrows), which flips the decision again and can spin forever
    inside ``update_idletasks``.
    """

    def __init__(self, master: tk.Misc, app: Any, *, bg: str) -> None:
        super().__init__(master, bg=bg)
        self.app = app
        self.canvas = tk.Canvas(self, bd=0, highlightthickness=0, bg=bg)
        self.scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview, style="Jarvis.Vertical.TScrollbar")
        self.inner = tk.Frame(self.canvas, bg=bg)
        self.window = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.canvas.bind("<Configure>", self._on_canvas_configure)
        self.scroll_visible = False
        self.content_height = 0
        self.viewport_height = 0

    def _on_canvas_configure(self, event: Any) -> None:
        try:
            self.canvas.itemconfigure(self.window, width=max(1, int(event.width)))
        except tk.TclError:
            pass

    def scrollable(self) -> bool:
        return self.scroll_visible

    def scroll_by(self, delta: int) -> None:
        if not self.scroll_visible:
            return
        try:
            self.canvas.yview_scroll(int(-delta / 40) or (-1 if delta > 0 else 1), "units")
        except tk.TclError:
            pass

    def fit(self, max_height: int, *, to_end: bool = True) -> int:
        """Size the viewport to the content (at most ``max_height``) and return
        the height used. Bounded to two passes: the second only runs when
        showing or hiding the bar changed the wrap width."""
        ceiling = max(1, int(max_height))
        needed = 0
        for _pass in range(2):
            self.update_idletasks()
            needed = max(1, int(self.inner.winfo_reqheight()))
            show = needed > ceiling
            if show == self.scroll_visible:
                break
            self.scroll_visible = show
            if show:
                self.scrollbar.pack(side="right", fill="y")
            else:
                self.scrollbar.pack_forget()
        height = min(needed, ceiling)
        width = max(1, int(self.canvas.winfo_width()))
        self.canvas.configure(height=height, scrollregion=(0, 0, width, needed))
        self.canvas.yview_moveto(1.0 if (self.scroll_visible and to_end) else 0.0)
        self.content_height = needed
        self.viewport_height = height
        return height


class InlineConfirm(tk.Frame):
    """``<question> [Confirm] · [Cancel]`` rendered in place of the row actions."""

    def __init__(
        self,
        master: tk.Misc,
        app: Any,
        question: str,
        confirm_text: str,
        on_confirm: Callable[[], None],
        on_cancel: Callable[[], None],
    ) -> None:
        theme = app.theme
        bg = RoundButton._parent_bg(master)
        super().__init__(master, bg=bg)
        tk.Label(self, text=question, bg=bg, fg=theme.danger, font=app.fonts.small_bold).pack(side="left", padx=(0, 8))
        self.confirm_button = RoundButton(self, app, confirm_text, on_confirm, kind="danger", padx=10, pady=3, font=app.fonts.small_bold)
        self.confirm_button.pack(side="left")
        self.cancel_button = RoundButton(self, app, "Cancel", on_cancel, kind="ghost", padx=10, pady=3, font=app.fonts.small)
        self.cancel_button.pack(side="left", padx=(6, 0))


# --------------------------------------------------------------------------
# Compact markdown renderer (the companion's reply area)
# --------------------------------------------------------------------------

class MarkdownBox(AfterMixin, tk.Frame):
    """Markdown rendered with the main window's block/inline grammar but
    without card chrome: paragraphs, headings, lists, quotes, rules, tables
    (as aligned text) and fenced code in a ``code_bg`` frame with **Copy**.

    While a reply is streaming, ``append_plain`` keeps one growing text block;
    ``set_markdown`` replaces it with the parsed blocks once the reply lands.
    """

    def __init__(self, master: tk.Misc, app: Any, bg: str | None = None) -> None:
        theme = app.theme
        self.app = app
        self.theme = theme
        self.bg = bg or theme.bg
        super().__init__(master, bg=self.bg)
        self._init_afters()
        self.content = ""
        self.blocks: list[str] = []
        self._stream: AutoText | None = None

    # -- lifecycle ----------------------------------------------------------
    def destroy(self) -> None:
        self._cancel_all_later()
        super().destroy()

    def clear(self) -> None:
        for child in self.winfo_children():
            child.destroy()
        self._stream = None
        self.content = ""
        self.blocks = []

    # -- content ------------------------------------------------------------
    def set_plain(self, text: str) -> None:
        self.clear()
        self.content = str(text or "")
        self._stream = self._rich_text(self, self.bg)
        self._stream.pack(fill="x")
        self._stream.set_text(self.content)
        self.blocks = ["stream"]

    def append_plain(self, text: str) -> None:
        if self._stream is None:
            self.set_plain(text)
            return
        self.content += str(text or "")
        self._stream.append_text(str(text or ""))

    def set_markdown(self, text: str) -> None:
        self.clear()
        self.content = str(text or "")
        blocks = parse_markdown(self.content)
        if not blocks:
            blocks = [{"type": "paragraph", "text": self.content}]
        for block in blocks:
            kind = str(block.get("type") or "paragraph")
            if kind == "code":
                self._render_code(str(block.get("lang") or ""), str(block.get("text") or ""))
            elif kind == "heading":
                self._render_heading(int(block.get("level") or 1), str(block.get("text") or ""))
            elif kind == "hr":
                tk.Frame(self, bg=self.theme.border_strong, height=1).pack(fill="x", pady=6)
            elif kind == "quote":
                self._render_quote(str(block.get("text") or ""))
            elif kind == "list":
                self._render_list(bool(block.get("ordered")), list(block.get("items") or []))
            elif kind == "table":
                self._render_code("table", render_table_text(block.get("rows") or []), copyable=False)
            else:
                kind = "paragraph"
                self._render_paragraph(str(block.get("text") or ""))
            self.blocks.append(kind)

    def block_kinds(self) -> list[str]:
        return list(self.blocks)

    def text_widgets(self) -> list[AutoText]:
        found: list[AutoText] = []
        stack: list[tk.Misc] = [self]
        while stack:
            current = stack.pop(0)
            for child in current.winfo_children():
                if isinstance(child, AutoText):
                    found.append(child)
                stack.append(child)
        return found

    def plain_text(self) -> str:
        return "\n".join(widget.plain_text() for widget in self.text_widgets())

    def refit(self) -> None:
        for widget in self.text_widgets():
            widget.fit()

    # -- blocks -------------------------------------------------------------
    def _rich_text(self, parent: tk.Misc, bg: str) -> AutoText:
        theme = self.theme
        fonts = self.app.fonts
        widget = AutoText(parent, self.app, font=fonts.body, bg=bg, fg=theme.text)
        widget.tag_configure("bold", font=fonts.body_bold, foreground=theme.text_strong)
        widget.tag_configure("italic", font=fonts.body_italic)
        widget.tag_configure("strike", font=fonts.body_strike, foreground=theme.muted)
        widget.tag_configure("code", font=fonts.inline_code, background=theme.code_bg, foreground=theme.accent if theme.dark else theme.accent_hover)
        widget.tag_configure("link", foreground=theme.accent, underline=True)
        widget.tag_configure("muted", foreground=theme.muted)
        return widget

    def _insert_inline(self, widget: AutoText, text: str, base: str | None = None) -> None:
        widget.configure(state="normal")
        self._insert_runs(widget, text, (base,) if base else (), depth=0)
        widget.configure(state="disabled")
        widget.settle()

    def _insert_runs(self, widget: AutoText, text: str, tags: tuple[str, ...], *, depth: int) -> None:
        for style, run, url in inline_runs(text):
            if style == "link":
                href = safe_http_url(url)
                if href:
                    tag = f"link-{id(widget)}-{widget.index('end')}"
                    widget.tag_configure(tag, foreground=self.theme.accent, underline=True)
                    widget.tag_bind(tag, "<Button-1>", lambda _e, target=href: webbrowser.open(target))
                    widget.tag_bind(tag, "<Enter>", lambda _e: widget.configure(cursor="hand2"))
                    widget.tag_bind(tag, "<Leave>", lambda _e: widget.configure(cursor="arrow"))
                    widget.insert("end", run, (*tags, tag))
                else:
                    widget.insert("end", run, tags)
            elif style == "text":
                widget.insert("end", run, tags)
            elif style == "code":
                widget.insert("end", run, (*tags, "code"))
            elif depth == 0 and any(marker in run for marker in ("`", "[", "http", "*", "_", "~~")):
                self._insert_runs(widget, run, (*tags, style), depth=depth + 1)
            else:
                widget.insert("end", run, (*tags, style))

    def _render_paragraph(self, text: str) -> None:
        widget = self._rich_text(self, self.bg)
        widget.pack(fill="x", pady=(0, self.app.px(6)))
        self._insert_inline(widget, text)

    def _render_heading(self, level: int, text: str) -> None:
        fonts = self.app.fonts
        font = fonts.h1 if level == 1 else fonts.h2 if level == 2 else fonts.h3
        widget = self._rich_text(self, self.bg)
        widget.configure(font=font, fg=self.theme.text_strong)
        widget.pack(fill="x", pady=(self.app.px(6), self.app.px(3)))
        self._insert_inline(widget, text)

    def _render_quote(self, text: str) -> None:
        theme = self.theme
        wrapper = tk.Frame(self, bg=self.bg)
        wrapper.pack(fill="x", pady=(0, self.app.px(6)))
        tk.Frame(wrapper, bg=theme.accent, width=3).pack(side="left", fill="y")
        inner = tk.Frame(wrapper, bg=theme.surface_alt, padx=10, pady=6)
        inner.pack(side="left", fill="x", expand=True)
        widget = self._rich_text(inner, theme.surface_alt)
        widget.configure(fg=theme.muted)
        widget.pack(fill="x")
        self._insert_inline(widget, text)

    def _render_list(self, ordered: bool, items: list[dict[str, Any]]) -> None:
        widget = self._rich_text(self, self.bg)
        widget.pack(fill="x", pady=(0, self.app.px(6)))
        counters: dict[int, int] = {}
        widget.configure(state="normal")
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            indent = int(item.get("indent", 0) or 0)
            counters[indent] = counters.get(indent, 0) + 1
            for deeper in list(counters):
                if deeper > indent:
                    counters.pop(deeper)
            checked = item.get("checked")
            if checked is not None:
                marker = "☑" if checked else "☐"
            elif ordered:
                marker = f"{counters[indent]}."
            else:
                marker = "•" if indent == 0 else "◦"
            pad = self.app.px(16) * indent
            tag = f"li-{indent}"
            widget.tag_configure(tag, lmargin1=pad, lmargin2=pad + self.app.px(18), spacing1=1)
            widget.insert("end", f"{marker}  ", (tag, "muted" if checked is not None else tag))
            widget.configure(state="disabled")
            self._insert_inline(widget, str(item.get("text", "")), base=tag)
            widget.configure(state="normal")
            if index < len(items) - 1:
                widget.insert("end", "\n", (tag,))
        widget.configure(state="disabled")
        widget.settle()

    def _render_code(self, language: str, code: str, *, copyable: bool = True) -> None:
        theme = self.theme
        app = self.app
        frame = tk.Frame(self, bg=theme.code_bg, highlightbackground=theme.border_strong, highlightthickness=1)
        frame.pack(fill="x", pady=(2, app.px(8)))
        frame.code = code  # type: ignore[attr-defined]
        head = tk.Frame(frame, bg=theme.code_head, padx=8, pady=3)
        head.pack(fill="x")
        tk.Label(head, text=(language or "code").upper(), bg=theme.code_head, fg=theme.faint, font=app.fonts.tiny).pack(side="left")
        if copyable:
            copy = ActionLink(head, app, "Copy", None, tooltip="Copy the whole block")
            copy.configure(font=app.fonts.tiny, pady=0)
            copy.command = lambda: self._copy_block(code, copy)
            copy.pack(side="right")
            frame.copy_link = copy  # type: ignore[attr-defined]
        body = AutoText(frame, app, font=app.fonts.mono_small, bg=theme.code_bg, fg=theme.text, wrap="char")
        body.configure(padx=8, pady=6, spacing1=0, spacing3=0)
        body.pack(fill="x")
        body.set_text(code)

    def _copy_block(self, code: str, label: ActionLink) -> None:
        try:
            self.app.copy_text(code, quiet=True)
        except TypeError:
            self.app.copy_text(code)
        try:
            label.color = self.theme.success
            label.configure(text="Copied ✓", fg=self.theme.success)
        except tk.TclError:
            return

        def restore() -> None:
            try:
                label.color = self.theme.muted
                label.configure(text="Copy", fg=self.theme.muted)
            except tk.TclError:
                pass

        self._later(COPY_FEEDBACK_MS, restore)

    def code_frames(self) -> list[tk.Frame]:
        return [child for child in self.winfo_children() if isinstance(child, tk.Frame) and hasattr(child, "code")]


# --------------------------------------------------------------------------
# Memory view (/facts)
# --------------------------------------------------------------------------

class MemoryView(AfterMixin, tk.Frame):
    """Governed project facts, grouped by subject, with update/erase/copy."""

    def __init__(self, master: tk.Misc, app: Any) -> None:
        theme = app.theme
        super().__init__(master, bg=theme.bg)
        self._init_afters()
        self.app = app
        self.theme = theme
        self.claims: list[dict[str, Any]] = []
        self.project_id: Any = None
        self.query = ""
        # The app refreshes the view when it is shown; until the filter reaches
        # two characters the store is never re-asked (client-side filter only).
        self.last_requested_query: str = ""
        self.history: dict[tuple[str, str], list[dict[str, Any]] | None] = {}
        self.expanded: set[tuple[str, str]] = set()
        self.confirming: tuple[str, str] | None = None
        self.rows: list[tk.Frame] = []
        self._wrap_labels: list[tuple[tk.Label, int]] = []
        self._filter_after: str | None = None

        header = tk.Frame(self, bg=theme.bg)
        header.pack(fill="x", padx=app.px(20), pady=(app.px(14), app.px(8)))
        tk.Label(header, text="Memory", bg=theme.bg, fg=theme.text_strong, font=app.fonts.h1).pack(side="left")
        self.refresh_button = RoundButton(header, app, "Refresh", self.refresh, kind="ghost", padx=12, pady=5, font=app.fonts.small)
        self.refresh_button.pack(side="right")
        self.remember_button = RoundButton(header, app, "Remember a fact", self._remember, kind="accent", padx=12, pady=5, font=app.fonts.small_bold)
        self.remember_button.pack(side="right", padx=(0, 8))
        self.filter_entry = PlaceholderEntry(header, app, "Filter facts", width=28)
        self.filter_entry.pack(side="right", padx=(12, 12), ipady=4)
        self.filter_entry.bind("<KeyRelease>", self._on_filter_key, add="+")

        self.list = ScrollFrame(self, app, bg=theme.bg)
        self.list.pack(fill="both", expand=True, padx=app.px(12))
        self.list.canvas.bind("<Configure>", self._on_width, add="+")

        footer = tk.Frame(self, bg=theme.bg)
        footer.pack(fill="x", padx=app.px(20), pady=(app.px(6), app.px(12)))
        self.count_label = tk.Label(footer, text="", bg=theme.bg, fg=theme.muted, font=app.fonts.small, anchor="w")
        self.count_label.pack(side="left")
        tk.Label(
            footer, text="Facts come from the governed memory store; receipts appear in the chat.",
            bg=theme.bg, fg=theme.faint, font=app.fonts.tiny, anchor="e",
        ).pack(side="right")
        self.render()

    # -- data ---------------------------------------------------------------
    def server_query(self) -> str:
        text = self.query.strip()
        return text if len(text) >= 2 else ""

    def refresh(self) -> None:
        query = self.server_query()
        self.last_requested_query = query
        self.app.session.request_facts(query)

    def on_event(self, kind: str, payload: Any) -> None:
        payload = payload if isinstance(payload, dict) else {}
        if kind == "facts":
            rows = payload.get("claims")
            self.claims = [row for row in (rows if isinstance(rows, list) else []) if isinstance(row, dict)]
            self.project_id = payload.get("project_id")
            # Values may have changed under us: forget cached history so an
            # opened toggle asks the store again instead of showing stale rows.
            self.history.clear()
            self.expanded.clear()
            self.confirming = None
            self.render()
        elif kind == "claim_history":
            key = (str(payload.get("subject", "") or ""), str(payload.get("predicate", "") or ""))
            versions = payload.get("versions")
            self.history[key] = [row for row in (versions if isinstance(versions, list) else []) if isinstance(row, dict)]
            self.render()

    def set_filter(self, text: str) -> None:
        self.filter_entry.set_value(str(text or ""))
        self._apply_filter(str(text or ""))

    def _on_filter_key(self, _event: Any) -> None:
        self._apply_filter(self.filter_entry.value())

    def _apply_filter(self, text: str) -> None:
        if text == self.query:
            return
        self.query = text
        self.render()
        self._cancel_later(self._filter_after)
        self._filter_after = self._later(FILTER_DEBOUNCE_MS, self._request_filtered)

    def destroy(self) -> None:
        self._cancel_all_later()
        self._filter_after = None
        super().destroy()

    def _request_filtered(self) -> None:
        self._filter_after = None
        query = self.server_query()
        if query != self.last_requested_query:
            self.refresh()

    def visible_claims(self) -> list[dict[str, Any]]:
        return [row for row in self.claims if _match(row, self.query.strip())]

    # -- rendering ----------------------------------------------------------
    def _on_width(self, event: Any) -> None:
        for label, reserve in self._wrap_labels:
            try:
                label.configure(wraplength=max(120, event.width - reserve))
            except tk.TclError:
                pass

    def _wrap(self, label: tk.Label, reserve: int) -> None:
        self._wrap_labels.append((label, reserve))
        width = self.list.canvas.winfo_width()
        label.configure(wraplength=max(120, (width if width > 1 else self.app.px(720)) - reserve))

    def render(self) -> None:
        theme = self.theme
        app = self.app
        for child in self.list.inner.winfo_children():
            child.destroy()
        self.rows = []
        self._wrap_labels = []
        visible = self.visible_claims()
        self._render_footer(len(visible))
        if not self.claims:
            tk.Label(
                self.list.inner,
                text="No governed facts yet. Say `Remember this project fact: …` or use Remember a fact.",
                bg=theme.bg, fg=theme.faint, font=app.fonts.body, justify="left",
            ).pack(anchor="w", padx=app.px(10), pady=app.px(24))
            return
        if not visible:
            tk.Label(
                self.list.inner, text=f"No facts match “{compact_activity(self.query, 60)}”.",
                bg=theme.bg, fg=theme.faint, font=app.fonts.body,
            ).pack(anchor="w", padx=app.px(10), pady=app.px(24))
            return
        for subject, rows in group_claims(visible):
            head = tk.Frame(self.list.inner, bg=theme.bg)
            head.pack(fill="x", padx=app.px(8), pady=(app.px(10), app.px(2)))
            tk.Label(head, text=safe_ui_text(subject, 200) or "(no subject)", bg=theme.bg, fg=theme.text_strong, font=app.fonts.h3).pack(side="left")
            tk.Label(head, text=f"{len(rows)}", bg=theme.bg, fg=theme.faint, font=app.fonts.small).pack(side="left", padx=(8, 0))
            for row in rows:
                self._render_row(row)

    def _render_footer(self, visible_count: int) -> None:
        total = len(self.claims)
        noun = "fact" if total == 1 else "facts"
        count = f"{visible_count} of {total} {noun}" if visible_count != total else f"{total} {noun}"
        name = _project_name(self.app, self.project_id)
        if name:
            count += f" · project {compact_activity(name, 60)}"
        elif self.project_id is not None:
            count += f" · project #{self.project_id}"
        self.count_label.configure(text=count)

    def _meta_text(self, row: dict[str, Any]) -> tuple[str, str]:
        """``(meta line, raw provenance)``; the raw part feeds the tooltip."""
        parts: list[str] = []
        label, raw = fact_source_label(row)
        if label:
            parts.append(label)
        scope = compact_activity(row.get("scope") or "", 40)
        if scope:
            parts.append(scope)
        since = _relative_since(row.get("created_at"))
        if since:
            parts.append(f"since {since}")
        return " · ".join(parts), raw

    def _render_row(self, row: dict[str, Any]) -> None:
        theme = self.theme
        app = self.app
        subject = str(row.get("subject", "") or "")
        predicate = str(row.get("predicate", "") or "")
        key = (subject, predicate)
        card = tk.Frame(self.list.inner, bg=theme.surface, padx=app.px(12), pady=app.px(8), highlightthickness=1, highlightbackground=theme.border)
        card.pack(fill="x", padx=app.px(8), pady=(0, app.px(4)))
        card.claim = row  # type: ignore[attr-defined]
        self.rows.append(card)
        top = tk.Frame(card, bg=theme.surface)
        top.pack(fill="x")
        text_col = tk.Frame(top, bg=theme.surface)
        text_col.pack(side="left", fill="x", expand=True)
        actions = tk.Frame(top, bg=theme.surface)
        actions.pack(side="right", anchor="n")
        card.actions = actions  # type: ignore[attr-defined]
        line = tk.Frame(text_col, bg=theme.surface)
        line.pack(fill="x", anchor="w")
        tk.Label(line, text=safe_ui_text(predicate, 200), bg=theme.surface, fg=theme.text, font=app.fonts.body).pack(side="left")
        tk.Label(line, text="·", bg=theme.surface, fg=theme.faint, font=app.fonts.body).pack(side="left", padx=6)
        value_label = tk.Label(line, text=safe_ui_text(row.get("value", ""), 2_000), bg=theme.surface, fg=theme.text_strong, font=app.fonts.body_bold, justify="left", anchor="w")
        value_label.pack(side="left", fill="x", expand=True)
        card.value_label = value_label  # type: ignore[attr-defined]
        self._wrap(value_label, app.px(360))
        meta, raw = self._meta_text(row)
        meta_row = tk.Frame(text_col, bg=theme.surface)
        meta_row.pack(fill="x", anchor="w", pady=(2, 0))
        if meta:
            meta_label = tk.Label(meta_row, text=meta, bg=theme.surface, fg=theme.muted, font=app.fonts.small)
            meta_label.pack(side="left")
            card.meta_label = meta_label  # type: ignore[attr-defined]
            card.meta_raw = raw  # type: ignore[attr-defined]
            if raw:
                card.meta_tooltip = Tooltip(meta_label, raw, app)  # type: ignore[attr-defined]
        try:
            superseded = int(row.get("superseded_count") or 0)
        except (TypeError, ValueError):
            superseded = 0
        if superseded > 0:
            expanded = key in self.expanded
            toggle = ActionLink(meta_row, app, ("▾" if expanded else "▸") + f" history ({superseded})", lambda k=key: self._toggle_history(k), color=theme.accent)
            toggle.pack(side="left", padx=(8, 0))
            card.history_toggle = toggle  # type: ignore[attr-defined]
            if expanded:
                self._render_history(card, key)
        if self.confirming == key:
            InlineConfirm(actions, app, "Erase this fact?", "Erase", lambda k=key: self._erase(k), self._cancel_confirm).pack(anchor="e")
        else:
            update = ActionLink(actions, app, "Update…", lambda k=key: self._update(k), tooltip="Remember a new value for this fact")
            update.pack(side="left")
            erase = ActionLink(actions, app, "Erase…", lambda k=key: self._ask_erase(k), color=theme.danger, tooltip="Erase through the governed memory command")
            erase.pack(side="left")
            copy = ActionLink(actions, app, "Copy", lambda r=row: self._copy(r), tooltip="Copy subject, predicate and value")
            copy.pack(side="left")
            card.update_link = update  # type: ignore[attr-defined]
            card.erase_link = erase  # type: ignore[attr-defined]
            card.copy_link = copy  # type: ignore[attr-defined]

    def _render_history(self, card: tk.Frame, key: tuple[str, str]) -> None:
        theme = self.theme
        app = self.app
        box = tk.Frame(card, bg=theme.surface_alt, padx=app.px(10), pady=app.px(6))
        box.pack(fill="x", pady=(app.px(6), 0))
        card.history_box = box  # type: ignore[attr-defined]
        versions = self.history.get(key)
        if versions is None:
            tk.Label(box, text="Loading history…", bg=theme.surface_alt, fg=theme.faint, font=app.fonts.small).pack(anchor="w")
            return
        if not versions:
            tk.Label(box, text="The store reported no earlier versions.", bg=theme.surface_alt, fg=theme.faint, font=app.fonts.small).pack(anchor="w")
            return
        for version in versions:
            status = str(version.get("status") or "").strip().lower()
            glyph = HISTORY_GLYPHS.get(status, "·")
            color = theme.success if status == "current" else (theme.danger if status == "retracted" else theme.muted)
            line = tk.Frame(box, bg=theme.surface_alt)
            line.pack(fill="x")
            tk.Label(line, text=glyph, bg=theme.surface_alt, fg=color, font=app.fonts.small_bold, width=2).pack(side="left")
            tk.Label(line, text=safe_ui_text(version.get("value", ""), 400), bg=theme.surface_alt, fg=theme.text if status == "current" else theme.muted, font=app.fonts.small).pack(side="left")
            bits = [status] if status else []
            when = _relative_since(version.get("created_at"))
            if when:
                bits.append(when)
            label, raw = fact_source_label(version)
            if label:
                bits.append(label)
            trail = tk.Label(line, text=" · ".join(bits), bg=theme.surface_alt, fg=theme.faint, font=app.fonts.tiny)
            trail.pack(side="right")
            if raw:
                Tooltip(trail, raw, app)

    # -- actions ------------------------------------------------------------
    def _remember(self) -> None:
        self.app.remember_fact_dialog()

    def _toggle_history(self, key: tuple[str, str]) -> None:
        if key in self.expanded:
            self.expanded.discard(key)
        else:
            self.expanded.add(key)
            if key not in self.history:
                self.history[key] = None
                self.app.session.request_claim_history(key[0], key[1])
        self.render()

    def _update(self, key: tuple[str, str]) -> None:
        self.app.remember_fact_dialog(seed=f"{key[0]} {key[1]} ")

    def _ask_erase(self, key: tuple[str, str]) -> None:
        self.confirming = key
        self.render()

    def _cancel_confirm(self) -> None:
        self.confirming = None
        self.render()

    def _erase(self, key: tuple[str, str]) -> None:
        self.confirming = None
        self.app.send_text(erase_fact_command(key[0], key[1]))
        self.render()

    def _copy(self, row: dict[str, Any]) -> None:
        text = " ".join(str(row.get(field, "") or "") for field in ("subject", "predicate", "value")).strip()
        self.app.copy_text(text)


# --------------------------------------------------------------------------
# Routines view
# --------------------------------------------------------------------------

class RoutinesView(AfterMixin, tk.Frame):
    """Scheduled jobs: list, inline creation form, pause/resume, delete with Undo.

    Delete hides the row and shows ``Routine deleted · Undo`` for five seconds;
    only when that window closes does the store hear ``delete_routine``. Undo
    restores the row without any store call. A pending deletion is committed
    if the view is destroyed first, so a Delete the operator did not undo is
    never silently dropped.
    """

    def __init__(self, master: tk.Misc, app: Any) -> None:
        theme = app.theme
        super().__init__(master, bg=theme.bg)
        self._init_afters()
        self.app = app
        self.theme = theme
        self.jobs: list[dict[str, Any]] = []
        self.tasks: dict[str, dict[str, Any]] = {}
        self.worker_alive: bool | None = getattr(app, "worker_alive", None)
        self.pending_delete: dict[Any, str | None] = {}
        self.form_visible = False
        self.rows: list[tk.Frame] = []
        self.undo_strips: dict[Any, tk.Frame] = {}

        header = tk.Frame(self, bg=theme.bg)
        header.pack(fill="x", padx=app.px(20), pady=(app.px(14), app.px(8)))
        tk.Label(header, text="Routines", bg=theme.bg, fg=theme.text_strong, font=app.fonts.h1).pack(side="left")
        self.new_button = RoundButton(header, app, "New routine", self.toggle_form, kind="accent", padx=12, pady=5, font=app.fonts.small_bold)
        self.new_button.pack(side="right")
        self.refresh_button = RoundButton(header, app, "Refresh", self.refresh, kind="ghost", padx=12, pady=5, font=app.fonts.small)
        self.refresh_button.pack(side="right", padx=(0, 8))

        self.form = self._build_form()

        self.undo_area = tk.Frame(self, bg=theme.bg)
        self.undo_area.pack(fill="x", padx=app.px(20))

        self.list = ScrollFrame(self, app, bg=theme.bg)
        self.list.pack(fill="both", expand=True, padx=app.px(12))

        self.footer = tk.Label(
            self, text=WORKER_OFFLINE_TEXT,
            bg=theme.bg, fg=theme.warning, font=app.fonts.small, anchor="w", justify="left",
        )
        self.render()

    def destroy(self) -> None:
        self._cancel_all_later()
        for job_id in list(self.pending_delete):
            self.pending_delete.pop(job_id, None)
            try:
                self.app.session.delete_routine(job_id)
            except Exception:
                pass
        super().destroy()

    # -- form ---------------------------------------------------------------
    def _build_form(self) -> tk.Frame:
        theme = self.theme
        app = self.app
        form = tk.Frame(self, bg=theme.surface, padx=app.px(16), pady=app.px(12), highlightthickness=1, highlightbackground=theme.border_strong)
        tk.Label(form, text="New routine", bg=theme.surface, fg=theme.text_strong, font=app.fonts.h3).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 6))
        entry_style = dict(bg=theme.surface_alt, fg=theme.text, insertbackground=theme.accent, bd=0, highlightthickness=1, highlightbackground=theme.border, highlightcolor=theme.accent, font=app.fonts.body)
        tk.Label(form, text="Name", bg=theme.surface, fg=theme.text, font=app.fonts.small_bold).grid(row=1, column=0, sticky="w")
        self.name_entry = tk.Entry(form, width=40, **entry_style)
        self.name_entry.grid(row=1, column=1, sticky="ew", ipady=4, pady=2)
        tk.Label(form, text="Prompt", bg=theme.surface, fg=theme.text, font=app.fonts.small_bold).grid(row=2, column=0, sticky="nw", pady=(4, 0))
        self.prompt_text = tk.Text(form, height=3, wrap="word", undo=True, **entry_style)
        self.prompt_text.grid(row=2, column=1, sticky="ew", pady=2)
        tk.Label(form, text="Project", bg=theme.surface, fg=theme.text, font=app.fonts.small_bold).grid(row=3, column=0, sticky="w")
        self.project_var = tk.StringVar(value="")
        self.project_box = themed_combobox(form, textvariable=self.project_var, state="readonly", font=app.fonts.body)
        self.project_box.grid(row=3, column=1, sticky="w", pady=2)
        tk.Label(form, text="Cadence", bg=theme.surface, fg=theme.text, font=app.fonts.small_bold).grid(row=4, column=0, sticky="w")
        cadence_row = tk.Frame(form, bg=theme.surface)
        cadence_row.grid(row=4, column=1, sticky="w", pady=2)
        self.cadence_var = tk.StringVar(value=CADENCE_PRESETS[0][0])
        self.cadence_box = themed_combobox(cadence_row, textvariable=self.cadence_var, values=[name for name, _m in CADENCE_PRESETS], state="readonly", width=16, font=app.fonts.body)
        self.cadence_box.pack(side="left")
        self.cadence_box.bind("<<ComboboxSelected>>", lambda _e: self._sync_custom())
        self.custom_entry = tk.Entry(cadence_row, width=8, **entry_style)
        self.custom_label = tk.Label(cadence_row, text="minutes", bg=theme.surface, fg=theme.muted, font=app.fonts.small)
        self.validation_label = tk.Label(form, text="", bg=theme.surface, fg=theme.danger, font=app.fonts.small, anchor="w", justify="left")
        self.validation_label.grid(row=5, column=0, columnspan=2, sticky="w", pady=(6, 0))
        buttons = tk.Frame(form, bg=theme.surface)
        buttons.grid(row=6, column=0, columnspan=2, sticky="e", pady=(8, 0))
        self.create_button = RoundButton(buttons, app, "Create", self.create_routine, kind="accent", padx=14, pady=5, font=app.fonts.small_bold)
        self.create_button.pack(side="right")
        self.cancel_button = RoundButton(buttons, app, "Cancel", self.hide_form, kind="ghost", padx=14, pady=5, font=app.fonts.small)
        self.cancel_button.pack(side="right", padx=(0, 8))
        form.columnconfigure(1, weight=1)
        return form

    def _sync_projects(self) -> None:
        names = [str(project.get("name") or f"#{project.get('id')}") for project in (getattr(self.app, "projects", None) or []) if isinstance(project, dict)]
        self.project_box.configure(values=names)
        current = _project_name(self.app, getattr(self.app, "project_id", None))
        if current and current in names:
            self.project_var.set(current)
        elif names and self.project_var.get() not in names:
            self.project_var.set(names[0])
        elif not names:
            self.project_var.set("")

    def _sync_custom(self) -> None:
        if self.cadence_var.get() == CUSTOM_CADENCE_LABEL:
            self.custom_entry.pack(side="left", padx=(8, 4), ipady=4)
            self.custom_label.pack(side="left")
            self.custom_entry.focus_set()
        else:
            self.custom_entry.pack_forget()
            self.custom_label.pack_forget()

    def set_cadence(self, label: str, custom: str = "") -> None:
        self.cadence_var.set(label)
        self.custom_entry.delete(0, "end")
        if custom:
            self.custom_entry.insert(0, custom)
        self._sync_custom()

    def toggle_form(self) -> None:
        if self.form_visible:
            self.hide_form()
        else:
            self.show_form()

    def show_form(self) -> None:
        self.form_visible = True
        self._sync_projects()
        self._sync_custom()
        self.validation_label.configure(text="")
        self.form.pack(fill="x", padx=self.app.px(20), pady=(0, self.app.px(10)), before=self.undo_area)
        self.new_button.set_text("Close form")
        self.name_entry.focus_set()

    def hide_form(self) -> None:
        self.form_visible = False
        self.form.pack_forget()
        self.new_button.set_text("New routine")
        self.validation_label.configure(text="")

    def prefill(self, prompt: str) -> None:
        """``/schedule <text>``: open the form with the prompt filled in; the
        operator still names the routine and picks the cadence before Create."""
        self.show_form()
        self.prompt_text.delete("1.0", "end")
        text = str(prompt or "").strip()
        if text:
            self.prompt_text.insert("1.0", safe_ui_text(text, 4_000))
        if not self.name_entry.get().strip():
            self.name_entry.focus_set()

    def _selected_project_id(self) -> Any:
        name = self.project_var.get()
        for project in getattr(self.app, "projects", None) or []:
            if isinstance(project, dict) and (str(project.get("name") or f"#{project.get('id')}") == name):
                return project.get("id")
        return getattr(self.app, "project_id", None)

    def create_routine(self) -> None:
        name = self.name_entry.get().strip()
        prompt = self.prompt_text.get("1.0", "end-1c").strip()
        if not name:
            self.validation_label.configure(text="Give the routine a name.")
            self.name_entry.focus_set()
            return
        if not prompt:
            self.validation_label.configure(text="Write the prompt Jarvis should run.")
            self.prompt_text.focus_set()
            return
        minutes, error = cadence_minutes(self.cadence_var.get(), self.custom_entry.get())
        if minutes is None:
            self.validation_label.configure(text=error)
            return
        self.validation_label.configure(text="")
        self.app.session.add_routine(name, prompt, minutes, self._selected_project_id())
        self.name_entry.delete(0, "end")
        self.prompt_text.delete("1.0", "end")
        self.custom_entry.delete(0, "end")
        self.hide_form()

    # -- data ---------------------------------------------------------------
    def refresh(self) -> None:
        self.app.session.request_routines()

    def on_event(self, kind: str, payload: Any) -> None:
        if kind != "routines":
            return
        payload = payload if isinstance(payload, dict) else {}
        jobs = payload.get("jobs")
        self.jobs = [job for job in (jobs if isinstance(jobs, list) else []) if isinstance(job, dict)]
        tasks = payload.get("tasks")
        self.tasks = {str(key): value for key, value in (tasks.items() if isinstance(tasks, dict) else []) if isinstance(value, dict)}
        if "worker_alive" in payload:
            self.worker_alive = payload.get("worker_alive")
        else:
            self.worker_alive = getattr(self.app, "worker_alive", None)
        # A deletion still in its Undo window stays pending unless the store
        # no longer lists the job (then there is nothing left to delete).
        present = {job.get("id") for job in self.jobs}
        for job_id in list(self.pending_delete):
            if job_id not in present:
                self._cancel_later(self.pending_delete.pop(job_id, None))
        self.render()

    # -- rendering ----------------------------------------------------------
    def _last_text(self, job: dict[str, Any]) -> str:
        task_id = job.get("last_task_id")
        if task_id in (None, ""):
            return "never"
        task = self.tasks.get(str(task_id))
        text = f"task #{task_id}"
        status = compact_activity(task.get("status") or "", 40) if isinstance(task, dict) else ""
        if status:
            text += f" · {status}"
        return text

    def worker_footer_text(self) -> str:
        """The honesty line to show, or ``""`` when the worker is known to be alive.

        ``False`` means the heartbeat is stale; ``None`` means no heartbeat file
        exists at all, which on a fresh data dir is the common first-run case,
        so it counts as offline whenever any routine is enabled.
        """
        if self.worker_alive is False:
            return WORKER_OFFLINE_TEXT
        if self.worker_alive is None and any(bool(job.get("enabled")) for job in self.jobs):
            return WORKER_UNKNOWN_TEXT
        return ""

    def render(self) -> None:
        theme = self.theme
        app = self.app
        for child in self.list.inner.winfo_children():
            child.destroy()
        self.rows = []
        footer_text = self.worker_footer_text()
        if footer_text:
            self.footer.configure(text=footer_text)
            self.footer.pack(fill="x", padx=app.px(20), pady=(app.px(6), app.px(12)), side="bottom")
        else:
            self.footer.pack_forget()
        self._render_undo_strips()
        visible = [job for job in self.jobs if job.get("id") not in self.pending_delete]
        if not visible:
            if not self.pending_delete:
                tk.Label(
                    self.list.inner, text="No routines yet. Create one with New routine.",
                    bg=theme.bg, fg=theme.faint, font=app.fonts.body,
                ).pack(anchor="w", padx=app.px(10), pady=app.px(24))
            return
        for job in visible:
            self._render_job(job)

    def _render_undo_strips(self) -> None:
        theme = self.theme
        app = self.app
        for child in self.undo_area.winfo_children():
            child.destroy()
        self.undo_strips = {}
        for job_id in self.pending_delete:
            strip = tk.Frame(self.undo_area, bg=theme.surface_alt, padx=app.px(12), pady=app.px(6), highlightthickness=1, highlightbackground=theme.border)
            strip.pack(fill="x", pady=(0, app.px(6)))
            tk.Label(strip, text="Routine deleted", bg=theme.surface_alt, fg=theme.text, font=app.fonts.small).pack(side="left")
            tk.Label(strip, text="·", bg=theme.surface_alt, fg=theme.faint, font=app.fonts.small).pack(side="left", padx=6)
            undo = ActionLink(strip, app, "Undo", lambda j=job_id: self._undo_delete(j), color=theme.accent, tooltip="Keep this routine (nothing has been deleted yet)")
            undo.pack(side="left")
            strip.undo_link = undo  # type: ignore[attr-defined]
            strip.job_id = job_id  # type: ignore[attr-defined]
            self.undo_strips[job_id] = strip

    def _render_job(self, job: dict[str, Any]) -> None:
        theme = self.theme
        app = self.app
        job_id = job.get("id")
        enabled = bool(job.get("enabled"))
        card = tk.Frame(self.list.inner, bg=theme.surface, padx=app.px(12), pady=app.px(8), highlightthickness=1, highlightbackground=theme.border)
        card.pack(fill="x", padx=app.px(8), pady=(0, app.px(4)))
        card.job = job  # type: ignore[attr-defined]
        self.rows.append(card)
        top = tk.Frame(card, bg=theme.surface)
        top.pack(fill="x")
        tk.Label(top, text=safe_ui_text(job.get("name", ""), 200) or "(unnamed)", bg=theme.surface, fg=theme.text_strong, font=app.fonts.body_bold).pack(side="left")
        pill_text = "Active" if enabled else "Paused"
        pill = tk.Label(
            top, text=pill_text, bg=theme.accent_soft if enabled else theme.surface_alt,
            fg=theme.accent if enabled else theme.muted, font=app.fonts.tiny, padx=8, pady=1,
        )
        pill.pack(side="left", padx=(10, 0))
        card.pill = pill  # type: ignore[attr-defined]
        actions = tk.Frame(top, bg=theme.surface)
        actions.pack(side="right")
        card.actions = actions  # type: ignore[attr-defined]
        prompt = str(job.get("prompt", "") or "")
        queue = ActionLink(actions, app, "Queue a run now", lambda p=prompt: self._queue_now(p), tooltip="Queue a one-off task with this prompt (does not run the routine itself)")
        queue.pack(side="left")
        pause = ActionLink(actions, app, "Pause" if enabled else "Resume", lambda j=job_id, e=enabled: self._set_enabled(j, not e))
        pause.pack(side="left")
        delete = ActionLink(actions, app, "Delete", lambda j=job_id: self._delete_with_undo(j), color=theme.danger, tooltip="Removes the routine after 5 s unless you press Undo")
        delete.pack(side="left")
        card.queue_link = queue  # type: ignore[attr-defined]
        card.pause_link = pause  # type: ignore[attr-defined]
        card.delete_link = delete  # type: ignore[attr-defined]
        detail = " · ".join([
            format_interval(job.get("interval_minutes")),
            "Next: " + ("paused" if not enabled else format_next_run(job.get("next_run_at"))),
            "Last: " + self._last_text(job),
        ])
        meta = tk.Label(card, text=detail, bg=theme.surface, fg=theme.muted, font=app.fonts.small, anchor="w")
        meta.pack(fill="x", pady=(2, 0))
        card.meta = meta  # type: ignore[attr-defined]
        prompt_preview = compact_activity(job.get("prompt") or "", 160)
        if prompt_preview:
            tk.Label(card, text=prompt_preview, bg=theme.surface, fg=theme.faint, font=app.fonts.small, anchor="w").pack(fill="x", pady=(2, 0))
        task = self.tasks.get(str(job.get("last_task_id"))) if job.get("last_task_id") not in (None, "") else None
        error = compact_activity(task.get("last_error") or "", 200) if isinstance(task, dict) else ""
        if error:
            tk.Label(card, text=f"Last error: {error}", bg=theme.surface, fg=theme.danger, font=app.fonts.small, anchor="w").pack(fill="x", pady=(2, 0))

    # -- actions ------------------------------------------------------------
    def _queue_now(self, prompt: str) -> None:
        self.app.queue_task_text(prompt)

    def _set_enabled(self, job_id: Any, enabled: bool) -> None:
        self.app.session.set_routine_enabled(job_id, bool(enabled))

    def _delete_with_undo(self, job_id: Any) -> None:
        if job_id in self.pending_delete:
            return
        self.pending_delete[job_id] = self._later(ROUTINE_DELETE_UNDO_MS, lambda j=job_id: self._finish_delete(j))
        self.render()

    def _undo_delete(self, job_id: Any) -> None:
        if job_id not in self.pending_delete:
            return
        self._cancel_later(self.pending_delete.pop(job_id))
        self.render()

    def _finish_delete(self, job_id: Any) -> None:
        if job_id not in self.pending_delete:
            return
        self.pending_delete.pop(job_id, None)
        self.app.session.delete_routine(job_id)
        self.render()


# --------------------------------------------------------------------------
# Inbox popover
# --------------------------------------------------------------------------

class InboxPopover(AfterMixin, tk.Toplevel):
    """Borderless list under the bell; closes on Escape or when focus leaves."""

    def __init__(self, app: Any) -> None:
        super().__init__(app.root)
        self._init_afters()
        theme = app.theme
        self.app = app
        self.theme = theme
        self.closed = False
        self.rows: list[tk.Frame] = []
        self._focus_after: str | None = None
        self.overrideredirect(True)
        self.configure(bg=theme.border_strong)
        self.withdraw()
        self.width = app.px(380)
        # Text inside a row must wrap before the popover's right edge:
        # frame border (1) + list padding (6) + row padding (10) on each side.
        self.text_width = max(120, self.width - 2 * (1 + 6 + 10) - app.px(16))
        shell = tk.Frame(self, bg=theme.panel)
        shell.pack(fill="both", expand=True, padx=1, pady=1)
        head = tk.Frame(shell, bg=theme.panel)
        head.pack(fill="x", padx=12, pady=(10, 4))
        tk.Label(head, text="Inbox", bg=theme.panel, fg=theme.text_strong, font=app.fonts.title).pack(side="left")
        self.list = ScrollFrame(shell, app, bg=theme.panel)
        self.list.pack(fill="both", expand=True, padx=6, pady=(0, 4))
        self.list.canvas.configure(height=app.px(360), width=self.width - 16)
        foot = tk.Frame(shell, bg=theme.panel)
        foot.pack(fill="x", padx=12, pady=(0, 10))
        self.mark_read_button = RoundButton(foot, app, "Mark all read", self._mark_all_read, kind="ghost", padx=10, pady=4, font=app.fonts.small)
        self.mark_read_button.pack(side="right")
        self.bind("<Escape>", self._on_escape)
        self.bind("<FocusOut>", self._on_focus_out, add="+")
        self.refresh()

    def destroy(self) -> None:
        self.closed = True
        self._cancel_all_later()
        self._focus_after = None
        super().destroy()

    def _on_escape(self, _event: Any = None) -> str:
        self.close()
        return "break"

    def open_at(self, x: int, y: int) -> None:
        self.geometry(f"{self.width}x{self.app.px(440)}+{int(x)}+{int(y)}")
        self.deiconify()
        self.lift()
        self.attributes("-topmost", True)
        self._later(10, self._grab_focus)

    def _grab_focus(self) -> None:
        if self.closed:
            return
        try:
            self.focus_force()
        except tk.TclError:
            pass

    def _on_focus_out(self, _event: Any) -> None:
        self._cancel_later(self._focus_after)
        self._focus_after = self._later(POPOVER_FOCUS_DEBOUNCE_MS, self._close_if_unfocused)

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
        self._cancel_all_later()
        self._focus_after = None
        if getattr(self.app, "inbox_popover", None) is self:
            try:
                self.app.inbox_popover = None
            except AttributeError:
                pass
        try:
            self.destroy()
        except tk.TclError:
            pass

    def items(self) -> list[dict[str, Any]]:
        raw = getattr(self.app, "inbox_items", None) or []
        return [item for item in raw if isinstance(item, dict)]

    def refresh(self) -> None:
        if self.closed:
            return
        theme = self.theme
        app = self.app
        for child in self.list.inner.winfo_children():
            child.destroy()
        self.rows = []
        items = self.items()
        rendered = 0
        for title, kinds in INBOX_SECTIONS:
            section = [item for item in items if str(item.get("kind", "")) in kinds]
            if not section:
                continue
            tk.Label(self.list.inner, text=title.upper(), bg=theme.panel, fg=theme.faint, font=app.fonts.tiny, anchor="w").pack(fill="x", padx=10, pady=(8, 2))
            for item in section:
                self._render_item(item)
                rendered += 1
        if rendered == 0:
            tk.Label(self.list.inner, text="Nothing needs you.", bg=theme.panel, fg=theme.faint, font=app.fonts.small).pack(pady=24)
        unread = any(not item.get("read") for item in items)
        self.mark_read_button.set_enabled(unread)

    def _render_item(self, item: dict[str, Any]) -> None:
        theme = self.theme
        app = self.app
        read = bool(item.get("read"))
        row = tk.Frame(self.list.inner, bg=theme.panel, padx=10, pady=6, cursor="hand2")
        row.pack(fill="x")
        row.item = item  # type: ignore[attr-defined]
        title = tk.Label(
            row, text=compact_activity(item.get("title") or "", 90), bg=theme.panel,
            fg=theme.muted if read else theme.text_strong, font=app.fonts.label_bold if not read else app.fonts.label,
            anchor="w", justify="left", wraplength=self.text_width,
        )
        title.pack(fill="x")
        bits = [compact_activity(item.get("detail") or "", 120)]
        age = relative_age(item.get("created_at"))
        if age:
            bits.append(age)
        detail = tk.Label(
            row, text=" · ".join(bit for bit in bits if bit), bg=theme.panel, fg=theme.faint,
            font=app.fonts.tiny, anchor="w", justify="left", wraplength=self.text_width,
        )
        detail.pack(fill="x")
        row.title_label = title  # type: ignore[attr-defined]
        row.detail_label = detail  # type: ignore[attr-defined]
        for widget in (row, title, detail):
            widget.bind("<Button-1>", lambda _e, target=item: self._open(target))
            widget.bind("<Enter>", lambda _e, r=row: self._paint(r, theme.surface_hover))
            widget.bind("<Leave>", lambda _e, r=row: self._paint(r, theme.panel))
        self.rows.append(row)

    @staticmethod
    def _paint(row: tk.Frame, color: str) -> None:
        try:
            row.configure(bg=color)
            for child in row.winfo_children():
                child.configure(bg=color)
        except tk.TclError:
            pass

    def _open(self, item: dict[str, Any]) -> None:
        self.close()
        self.app.open_inbox_item(item)

    def _mark_all_read(self) -> None:
        self.app.mark_all_read()
        self.refresh()


# --------------------------------------------------------------------------
# Companion window
# --------------------------------------------------------------------------

class CompanionWindow(AfterMixin, tk.Toplevel):
    """Always-on-top quick-ask box: streams the reply as text, re-renders it as
    markdown when the assistant event lands, pastes clipboard images, and shows
    the shared :class:`jarvis.ui.ApprovalCard` (compact) when a turn stops for
    an approval — the same header, buttons and decided lines as the chat, the
    Approvals window and the context panel, plus an *Open in Jarvis* link.

    The card is fed by the store's row (the main window's cached copy, else an
    ``approval_detail`` request) and decides nothing itself: every button goes
    through ``app.decide_context_approval`` and the decided line is taken from
    the ``approval_decided`` event the app forwards."""

    def __init__(self, app: Any) -> None:
        super().__init__(app.root)
        self._init_afters()
        theme = app.theme
        self.app = app
        self.theme = theme
        self.busy = False
        self.activity = ""
        self.started_at: float | None = None
        self.approval_id: int | None = None
        self.approval_decision: str | None = None
        self.approval_detail: dict[str, Any] | None = None
        self.approval_grant_id: int | None = None
        self.approval_card: ApprovalCard | None = None
        self._approval_reason_pending = False
        self._last_request: tuple[str, list[str]] | None = None
        self.conversation_id: Any = None
        self.images: list[str] = []
        self._timer: str | None = None
        self._geometry_after: str | None = None
        self._visible = False
        self.title("Jarvis")
        self.configure(bg=theme.bg)
        for name, value in (("-toolwindow", True), ("-topmost", True)):
            try:
                self.wm_attributes(name, value)
            except tk.TclError:
                pass
        _apply_titlebar_theme(self, theme.dark)
        self.protocol("WM_DELETE_WINDOW", self.hide)
        self.withdraw()

        body = tk.Frame(self, bg=theme.bg, padx=app.px(12), pady=app.px(10))
        body.pack(fill="both", expand=True)
        self.body = body
        head = tk.Frame(body, bg=theme.bg)
        head.pack(fill="x")
        self.head_label = tk.Label(head, text=f"Jarvis · {getattr(app, 'model_label', '') or ''}", bg=theme.bg, fg=theme.text_strong, font=app.fonts.label_bold, anchor="w")
        self.head_label.pack(side="left")
        self.close_button = IconButton(head, app, "✕", self.hide, tooltip="Hide (Esc)")
        self.close_button.pack(side="right")
        self.open_button = RoundButton(head, app, "⤢ Open in Jarvis", self._open_main, kind="ghost", padx=10, pady=3, font=app.fonts.small)
        self.open_button.pack(side="right", padx=(0, 6))
        self.new_chat_button = RoundButton(head, app, "New chat", self._new_chat, kind="ghost", padx=10, pady=3, font=app.fonts.small)
        self.new_chat_button.pack(side="right", padx=(0, 6))

        composer = tk.Frame(body, bg=theme.surface, highlightthickness=1, highlightbackground=theme.border_strong, padx=app.px(8), pady=app.px(4))
        composer.pack(fill="x", pady=(app.px(8), 0))
        self.composer = composer
        self.input = GrowText(composer, app, min_lines=1, max_lines=4)
        self.input.pack(fill="x")
        self.input.bind("<Return>", self._on_return)
        self.input.bind("<Shift-Return>", lambda _e: None)
        self.input.bind("<Escape>", lambda _e: (self.hide(), "break")[1])
        self.input.bind("<Control-v>", self._paste)
        self.input.bind("<Control-V>", self._paste)
        self.bind("<Escape>", lambda _e: self.hide())
        self.chips_row = tk.Frame(composer, bg=theme.surface)
        self.chips: list[tk.Frame] = []

        # Pack order matters: the status line and the approval card claim
        # their space from the bottom first, so a reply can never push them
        # off the window; the reply viewport takes what remains (up to its
        # own ceiling, then scrolls).
        self.status_row = tk.Frame(body, bg=theme.bg)
        self.status_row.pack(side="bottom", fill="x", pady=(app.px(6), 0))
        self.status_label = tk.Label(self.status_row, text="", bg=theme.bg, fg=theme.muted, font=app.fonts.small, anchor="w", justify="left")
        self.status_label.pack(side="left", fill="x", expand=True)
        # Holds the shared ApprovalCard; packed at the bottom only while a
        # card is showing, so the reply can never push it off the window.
        self.approval_row = tk.Frame(body, bg=theme.bg)

        self.reply_frame = ReplyScroller(body, app, bg=theme.bg)
        self.reply = MarkdownBox(self.reply_frame.inner, app, bg=theme.bg)
        self.reply.pack(fill="x")
        self._line_height = max(12, int(app.fonts.body.metrics("linespace")) + 2)
        self.reply_frame.canvas.configure(height=self._line_height)
        self.bind("<MouseWheel>", self._on_wheel, add="+")

        self._place_initial()
        self.bind("<Configure>", self._on_configure, add="+")

    # -- scheduling ---------------------------------------------------------
    def destroy(self) -> None:
        # Unmapping raises one last <Configure>; mark hidden first so it does
        # not re-arm the geometry save on a window that is going away.
        self._visible = False
        self._stop_timer()
        self._cancel_all_later()
        self._geometry_after = None
        super().destroy()

    # -- geometry -----------------------------------------------------------
    def _place_initial(self) -> None:
        saved = None
        try:
            saved = self.app.settings.get("companion_geometry")
        except Exception:
            saved = None
        if isinstance(saved, str) and _GEOMETRY_RE.match(saved.strip()):
            self.geometry(saved.strip())
            return
        width = self.app.px(COMPANION_WIDTH)
        height = self.app.px(COMPANION_HEIGHT)
        try:
            screen_w = self.winfo_screenwidth()
            screen_h = self.winfo_screenheight()
        except tk.TclError:
            screen_w, screen_h = 1280, 800
        x = max(0, (screen_w - width) // 2)
        y = max(0, screen_h - height - self.app.px(90))
        self.geometry(f"{width}x{height}+{x}+{y}")

    def _on_configure(self, event: Any) -> None:
        if event.widget is not self or not self._visible:
            return
        self._cancel_later(self._geometry_after)
        self._geometry_after = self._later(400, self.save_geometry)

    def save_geometry(self) -> None:
        # Called directly from hide() too: drop the debounce timer either way
        # so nothing fires on a window that may be destroyed by then.
        self._cancel_later(self._geometry_after)
        self._geometry_after = None
        try:
            geometry = self.geometry()
        except tk.TclError:
            return
        if _GEOMETRY_RE.match(geometry) and not geometry.startswith("1x1"):
            try:
                self.app.settings.set("companion_geometry", geometry)
            except Exception:
                pass

    def _on_wheel(self, event: Any) -> None:
        try:
            under = self.winfo_containing(event.x_root, event.y_root)
        except (tk.TclError, AttributeError):
            return
        widget = under
        while widget is not None:
            if widget is self.reply_frame:
                self.reply_frame.scroll_by(int(getattr(event, "delta", 0) or 0))
                return
            widget = getattr(widget, "master", None)

    def _fit_reply(self) -> None:
        if not self.reply_frame.winfo_manager():
            self.reply_frame.pack(side="top", fill="x", pady=(self.app.px(8), 0), after=self.composer)
        self._refit()
        # AutoText only measures once it has a width; measure again after layout.
        self._later(0, self._refit)
        self._later(90, self._refit)

    def reply_ceiling(self) -> int:
        return COMPANION_MAX_REPLY_LINES * self._line_height

    def _refit(self) -> None:
        try:
            self.reply.refit()
            self.reply_frame.fit(self.reply_ceiling())
        except tk.TclError:
            return
        self._fit_window()

    def _fit_window(self, *, shrink: bool = False) -> None:
        """Grow the window to its packed content (and shrink back when asked,
        e.g. after New chat) so the reply, the card and the status line all fit."""
        if not self._visible:
            return
        try:
            self.update_idletasks()
            needed = int(self.body.winfo_reqheight())
            current = self.geometry()
        except tk.TclError:
            return
        match = re.match(r"^(\d+)x(\d+)([+-])(\d+)([+-])(\d+)$", current)
        if not match:
            return
        width, height = int(match.group(1)), int(match.group(2))
        x_sign, x, y_sign, y = match.group(3), int(match.group(4)), match.group(5), int(match.group(6))
        floor = self.app.px(COMPANION_HEIGHT)
        target = max(floor, needed)
        if needed > height or (shrink and target < height):
            if y_sign == "+":
                # The box lives at the bottom of the screen: grow upward and
                # shrink downward so its bottom edge (and the composer under
                # the operator's eyes) stays where it is instead of sliding
                # off the screen. A "-y" geometry is bottom-anchored already.
                y = max(0, y + height - target)
            self.geometry(f"{width}x{target}{x_sign}{x}{y_sign}{y}")

    # -- visibility ---------------------------------------------------------
    def show(self) -> None:
        self._visible = True
        self.head_label.configure(text=f"Jarvis · {getattr(self.app, 'model_label', '') or ''}")
        try:
            self.deiconify()
            self.lift()
            self.attributes("-topmost", True)
        except tk.TclError:
            return
        self._later(10, self._focus_input)

    def _focus_input(self) -> None:
        try:
            self.input.focus_force()
        except tk.TclError:
            pass

    def hide(self) -> None:
        if self._visible:
            self.save_geometry()
        self._visible = False
        try:
            self.withdraw()
        except tk.TclError:
            pass

    # -- composer: images ---------------------------------------------------
    def _clipboard_text(self) -> str:
        try:
            return str(self.clipboard_get() or "")
        except tk.TclError:
            return ""

    def _paste(self, _event: Any) -> str | None:
        """Ctrl+V: text pastes normally; with no text on the clipboard the
        bitmap (if any) is saved as ``companion-<timestamp>.png`` and chipped."""
        if self._clipboard_text().strip():
            return None
        if len(self.images) >= COMPANION_MAX_IMAGES:
            self.status_label.configure(text=f"Up to {COMPANION_MAX_IMAGES} images per message.")
            return "break"
        folder = str(Path(str(getattr(self.app, "data_dir", "") or ".")) / "attachments")
        run_job = getattr(self.app, "run_job", None)
        if callable(run_job):
            run_job("companion_clipboard", save_companion_image, folder, on_done=self._pasted_image)
        else:
            try:
                result: Any = save_companion_image(folder)
                error: str | None = None
            except Exception as exc:
                result, error = None, f"{type(exc).__name__}: {exc}"
            self._pasted_image(result, error)
        return "break"

    def _pasted_image(self, result: Any, error: str | None) -> None:
        if error:
            self.status_label.configure(text=f"Could not save the image: {compact_activity(error, 120)}")
            return
        if not result:
            self.status_label.configure(text=COMPANION_NOTHING_TO_PASTE)
            return
        self._add_image(str(result))

    def _add_image(self, path: str) -> None:
        theme = self.theme
        app = self.app
        if path in self.images:
            return
        self.images.append(path)
        chip = tk.Frame(self.chips_row, bg=theme.surface_alt, padx=6, pady=2)
        chip.path = path  # type: ignore[attr-defined]
        tk.Label(chip, text=f"🖼 {compact_activity(Path(path).name, 40)}", bg=theme.surface_alt, fg=theme.text, font=app.fonts.tiny).pack(side="left")
        remove = ActionLink(chip, app, "×", lambda p=path: self._remove_image(p), tooltip="Remove this image")
        remove.configure(font=app.fonts.tiny, padx=4, pady=0)
        remove.pack(side="left")
        chip.remove_link = remove  # type: ignore[attr-defined]
        chip.pack(side="left", padx=(0, 6), pady=(4, 0))
        self.chips.append(chip)
        if not self.chips_row.winfo_manager():
            self.chips_row.pack(fill="x", after=self.input)
        self.status_label.configure(text="Image attached · sends with your next message")
        self._later(0, self._fit_window)

    def _remove_image(self, path: str) -> None:
        self.images = [item for item in self.images if item != path]
        for chip in list(self.chips):
            if getattr(chip, "path", None) == path:
                chip.destroy()
                self.chips.remove(chip)
        if not self.chips:
            self.chips_row.pack_forget()

    def _clear_images(self) -> None:
        for chip in self.chips:
            chip.destroy()
        self.chips = []
        self.images = []
        self.chips_row.pack_forget()

    # -- sending ------------------------------------------------------------
    def _on_return(self, _event: Any) -> str:
        self.send()
        return "break"

    def send(self) -> None:
        text = self.input.value().strip()
        images = list(self.images)
        if not text and not images:
            return
        self.input.set_value("")
        if images:
            self._clear_images()
        self._last_request = (text, images)
        self._dispatch(text, images)

    def _dispatch(self, text: str, images: list[str]) -> None:
        self.reply.clear()
        self._hide_approval()
        self.status_label.configure(text="Sending…")
        if images:
            self.app.companion_send(text, images=list(images))
        else:
            self.app.companion_send(text)

    def _retry_after_approval(self) -> None:
        """Retry with approval: the last request goes again as a new turn
        (nothing resumes on its own, exactly as in the main window)."""
        request = self._last_request
        if request is None:
            return
        text, images = request
        self._dispatch(text, list(images))

    def _new_chat(self) -> None:
        if getattr(self.app, "busy", False):
            # The main window's toast is invisible from here; say it in place.
            self.status_label.configure(text=COMPANION_BUSY_TEXT)
            return
        self.app.new_chat()

    def _open_main(self) -> None:
        self.app.companion_open_main()

    def _open_approval(self) -> None:
        if self.approval_id is not None:
            self.app.scroll_to_approval(self.approval_id)

    # -- approval card ------------------------------------------------------
    def _cached_detail(self, approval_id: int) -> dict[str, Any] | None:
        """The main window's cached store row for this approval, or None."""
        hook = getattr(self.app, "approval_detail_for", None)
        if not callable(hook):
            return None
        try:
            detail = hook(approval_id)
        except Exception:
            return None
        return detail if isinstance(detail, dict) and detail else None

    def _request_detail(self, approval_id: int) -> None:
        """Ask the store for the row the same way a reply card does; the
        ``approval_detail`` event re-renders the card."""
        session = getattr(self.app, "session", None)
        hook = getattr(session, "approval_detail", None)
        if callable(hook):
            try:
                hook(int(approval_id))
            except Exception:
                pass

    def _hide_approval(self) -> None:
        self.approval_id = None
        self.approval_decision = None
        self.approval_detail = None
        self.approval_grant_id = None
        self._approval_reason_pending = False
        self._destroy_card()
        self.approval_row.pack_forget()

    def _destroy_card(self) -> None:
        card = self.approval_card
        self.approval_card = None
        if card is not None:
            try:
                if card.winfo_exists():
                    card.destroy()
            except tk.TclError:
                pass

    def _show_approval(self, approval_id: int) -> None:
        self.approval_id = int(approval_id)
        self.approval_decision = None
        self.approval_grant_id = None
        self._approval_reason_pending = False
        self.approval_detail = self._cached_detail(self.approval_id)
        if self.approval_detail is None:
            self._request_detail(self.approval_id)
        self._render_approval()

    def _render_approval(self) -> None:
        """(Re)build the shared card from what is known: the store's row, a
        loading placeholder until it arrives, or the decided line."""
        app = self.app
        self._destroy_card()
        if self.approval_id is None:
            self.approval_row.pack_forget()
            return
        row = self.approval_detail
        missing = bool(row) and bool(row.get("missing"))
        decided = self.approval_decision
        approved = bool(decided) and decided.startswith(("Approved", "Allowed", "Always")) and "recording" not in decided
        resumable = approved and self._last_request is not None and not getattr(app, "busy", False)
        card = ApprovalCard(
            self.approval_row, app, row if row and not missing else None,
            approval_id=self.approval_id,
            on_decide=self._decide,
            decision=decided,
            loading=row is None and not decided,
            missing=missing and not decided,
            resumable=resumable,
            on_retry=self._retry_after_approval,
            grant_id=self.approval_grant_id,
            on_revoke=getattr(app, "revoke_grant", None),
            compact=True,
        )
        self._widen_card(card)
        if row is not None and not decided and not self._target_shown(row, missing):
            self._restrict_to_deny(card)
        card.pack(fill="x")
        self.approval_card = card
        # The card's Tab moves to the main window's next pending card; here it
        # goes back to the composer, and Esc on a button still reveals Deny.
        for button in (card.approve_button, card.deny_button):
            if button is not None:
                button.bind("<Tab>", lambda _e: (self.input.focus_set(), "break")[1])
        head = card.headline.master
        self.open_approval_link = ActionLink(head, app, COMPANION_OPEN_APPROVAL_TEXT, self._open_approval, tooltip="Show this approval in the main window")
        self.open_approval_link.pack(side="right", padx=(8, 0))
        self.approval_row.pack(side="bottom", fill="x", pady=(app.px(6), 0))
        if not decided and card.approve_button is not None and not self.input.value().strip():
            self._later(80, card.focus)

    @staticmethod
    def _target_shown(row: dict[str, Any], missing: bool) -> bool:
        """True when the card can render the exact target (the sanitised resource)."""
        if missing:
            return False
        return bool(safe_ui_text(row.get("resource") or "", 4_000).strip())

    def _restrict_to_deny(self, card: ApprovalCard) -> None:
        """Never offer Approve for a target the operator cannot see here: keep
        Deny (and the Open in Jarvis link), drop Approve once / This chat /
        Always and the standing-approval note, and say why."""
        for child in list(card.winfo_children()):
            if isinstance(child, tk.Label) and str(child.cget("text")) == STANDING_UNAVAILABLE_TEXT:
                child.destroy()
                continue
            for button in list(child.winfo_children()):
                if isinstance(button, RoundButton) and button is not card.deny_button:
                    button.destroy()
        card.approve_button = None
        tk.Label(card, text=COMPANION_UNSEEN_TARGET_TEXT, bg=card.bg, fg=self.theme.faint, font=self.app.fonts.tiny, anchor="w", wraplength=self.app.px(COMPANION_CARD_WRAP), justify="left").pack(fill="x", pady=(4, 0))

    def _widen_card(self, card: ApprovalCard) -> None:
        narrow = self.app.px(230)
        wrap = self.app.px(COMPANION_CARD_WRAP)
        for child in card.winfo_children():
            if not isinstance(child, tk.Label):
                continue
            try:
                if int(str(child.cget("wraplength"))) == narrow:
                    child.configure(wraplength=wrap)
            except (tk.TclError, ValueError):
                continue

    def _decide(self, approval_id: int, approve: bool, scope: str = "once", reason: str = "") -> None:
        """Every card button lands here: the same app call the Approvals
        window and the context panel make; the store answers with
        ``approval_decided``."""
        if self.approval_id is None or int(approval_id) != self.approval_id or self.approval_decision:
            return
        reason = str(reason or "")
        self._approval_reason_pending = bool(reason.strip()) and not approve
        self.app.decide_context_approval(self.approval_id, approve, scope=scope, reason=reason)
        self.approval_decision = approval_decision_text(approve, scope) + " · recording…"
        self._render_approval()

    def _on_approval_decided(self, payload: dict[str, Any]) -> None:
        if self.approval_id is None or payload.get("approval_id") != self.approval_id:
            return
        approved = bool(payload.get("approved"))
        scope = str(payload.get("scope") or "once")
        if payload.get("changed"):
            until = _iso_to_epoch(str(payload.get("until") or "")) or None
            grant_id = payload.get("grant_id")
            self.approval_grant_id = grant_id if isinstance(grant_id, int) and not isinstance(grant_id, bool) else None
            reason_sent = self._approval_reason_pending and not approved and not getattr(self.app, "busy", False)
            self.approval_decision = approval_decision_text(approved, scope, until=until, reason_sent=reason_sent)
            # The turn is no longer waiting: the status line says what was decided.
            self.status_label.configure(text=self.approval_decision)
        else:
            row = payload.get("row") if isinstance(payload.get("row"), dict) else None
            read_back = decision_from_row(row) if row else None
            note = compact_activity(payload.get("note") or "", 160)
            if row:
                self.approval_detail = row
            if read_back:
                self.approval_decision = read_back
            elif note:
                # The store declined without deciding (not eligible, not
                # recorded): say why in the status line and keep the card
                # pending so the operator can approve once instead.
                self.approval_decision = None
                self.status_label.configure(text=note)
            else:
                self.approval_decision = COMPANION_APPROVAL_UNDECIDED_TEXT
        self._approval_reason_pending = False
        self._render_approval()

    def _on_approval_detail(self, payload: dict[str, Any]) -> None:
        try:
            approval_id = int(payload.get("approval_id") or 0)
        except (TypeError, ValueError):
            return
        if self.approval_id is None or approval_id != self.approval_id:
            return
        row = payload.get("approval")
        self.approval_detail = row if isinstance(row, dict) and row else {"missing": True}
        status = str(self.approval_detail.get("status") or "")
        if status and status != "pending" and not self.approval_decision:
            # A row that arrives already decided tells the card its outcome.
            self.approval_decision = decision_from_row(self.approval_detail)
        self._render_approval()

    # -- events -------------------------------------------------------------
    def on_event(self, kind: str, payload: Any) -> None:
        if kind == "busy":
            self._set_busy(bool(payload))
        elif kind == "activity":
            self.activity = compact_activity(payload or "", 200)
            self._tick_status()
        elif kind == "delta":
            text = payload.get("text") if isinstance(payload, dict) else payload
            if text:
                self.reply.append_plain(safe_ui_text(text, 200_000))
                self._fit_reply()
        elif kind == "assistant":
            self._on_assistant(payload if isinstance(payload, dict) else {})
        elif kind == "approval_decided":
            self._on_approval_decided(payload if isinstance(payload, dict) else {})
        elif kind == "approval_detail":
            self._on_approval_detail(payload if isinstance(payload, dict) else {})
        elif kind == "chat_created":
            # A chat the app created for this box without switching the main
            # view to it: keep its id so follow-up Enters continue it.
            if isinstance(payload, dict) and payload.get("conversation_id") is not None:
                self.conversation_id = payload.get("conversation_id")
        elif kind == "new_chat":
            self.conversation_id = payload.get("conversation_id") if isinstance(payload, dict) else None
            self.reply.clear()
            self.reply_frame.pack_forget()
            self._hide_approval()
            self.status_label.configure(text="")
            self._later(0, lambda: self._fit_window(shrink=True))

    def _set_busy(self, busy: bool) -> None:
        if busy and not self.busy:
            self.started_at = _now()
            self.activity = ""
        self.busy = busy
        if busy:
            self._tick_status()
        else:
            self._stop_timer()

    def _stop_timer(self) -> None:
        self._cancel_later(self._timer)
        self._timer = None

    def _tick_status(self) -> None:
        self._stop_timer()
        if not self.busy:
            return
        seconds = int(max(0.0, _now() - (self.started_at or _now())))
        lowered = self.activity.lower()
        if "warming" in lowered or "loading" in lowered:
            text = f"Loading model · {seconds} s"
        else:
            text = f"Working · {seconds} s"
        self.status_label.configure(text=text)
        self._timer = self._later(1000, self._tick_status)

    def _on_assistant(self, payload: dict[str, Any]) -> None:
        self.busy = False
        self._stop_timer()
        content = payload.get("content")
        if isinstance(content, str) and content:
            self.reply.set_markdown(safe_ui_text(content, 200_000))
            self._fit_reply()
        status = str(payload.get("status") or "").strip().lower()
        reason = compact_activity(payload.get("reason") or "", 160)
        approval_id = payload.get("approval_id")
        if approval_id is not None:
            self.status_label.configure(text="Waiting for your approval above")
        elif status and status not in {"complete", "ok", "done"}:
            self.status_label.configure(text=f"Stopped early: {reason or status}")
        elif reason:
            self.status_label.configure(text=f"Stopped early: {reason}")
        else:
            bits: list[str] = []
            model = compact_activity(payload.get("model") or "", 60)
            if model:
                bits.append(model)
            elapsed = payload.get("elapsed")
            if isinstance(elapsed, (int, float)) and not isinstance(elapsed, bool):
                bits.append(format_elapsed(float(elapsed)))
            self.status_label.configure(text=" · ".join(bits))
        if approval_id is not None:
            self._show_approval(approval_id)
        else:
            self._hide_approval()
        self._later(0, self._fit_window)


__all__ = [
    "AfterMixin",
    "CompanionWindow",
    "approval_decision_text",
    "InboxPopover",
    "MarkdownBox",
    "MemoryView",
    "RoutinesView",
    "cadence_minutes",
    "companion_image_path",
    "themed_combobox",
    "erase_fact_command",
    "fact_source_label",
    "format_interval",
    "format_next_run",
    "group_claims",
    "relative_age",
    "save_companion_image",
]
