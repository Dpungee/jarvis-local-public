"""JARVIS Desktop — the native Windows chat interface.

Design goals (borrowed from the best of ChatGPT, Claude, Grok and Codex):

* a calm, centered conversation column with real message cards, streaming
  replies, rendered markdown and copyable code blocks;
* a chat sidebar with search, date grouping, rename and delete;
* a "working" timeline that shows what Jarvis is doing while it works;
* a command palette (Ctrl+K), keyboard shortcuts and three themes;
* crisp high-DPI rendering and a dark title bar on Windows 11.

Everything that touches SQLite or the Agent stays on one worker thread
(:class:`JarvisSession`); Tk only ever sees redacted, bounded text.
"""

from __future__ import annotations

import ctypes
import json
import math
import queue
import re
import sys
import threading
import time
import webbrowser
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import tkinter as tk
from tkinter import filedialog, font as tkfont, messagebox, ttk

from . import council
from .agent import Agent, AgentRunCancelled
from .attachments import MAX_IMAGE_ATTACHMENTS, ImageAttachment
from .cli import _ForegroundLease
from .config import Config
from .memory import Memory
from .model_client import user_model_error_message
from .ollama_client import OllamaError
from .proactive import RuntimeGuard
from .redaction import redact_secrets


APP_TITLE = "JARVIS Desktop"
MODEL_CHOICES = (
    "Auto",
    "Fast",
    "Reasoning",
    "Coding",
    "Deep 30B",
)
MODEL_OVERRIDES = {
    "Auto": "auto",
    "Fast": "fast",
    "Reasoning": "reasoning",
    "Coding": "coding",
    "Deep 30B": "deep",
}
MODEL_HINTS = {
    "Auto": "Task-aware routing",
    "Fast": "Low latency",
    "Reasoning": "Analysis and research",
    "Coding": "Build and verify",
    "Deep 30B": "Manual heavy mode",
}
MAX_PROMPT_CHARS = 50_000
DEFAULT_CHAT_TITLE = "New chat"
LEGACY_CHAT_TITLES = frozenset({"desktop chat", "new chat", "presence chat", "new task"})
SETTINGS_FILE = "desktop_ui.json"


def model_override_for(label: str) -> str:
    """Resolve one bounded operator-facing model label."""
    return MODEL_OVERRIDES.get(str(label).strip(), "auto")


def safe_ui_text(value: Any, limit: int = 100_000) -> str:
    """Redact control-plane secrets before text crosses into a UI widget."""
    text = redact_secrets(str(value), "[REDACTED]").replace("\x00", "")
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 24)] + "\n…[display truncated]"


def compact_activity(value: Any, limit: int = 140) -> str:
    text = " ".join(safe_ui_text(value, limit * 2).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def chat_title_from_prompt(prompt: str, limit: int = 56) -> str:
    """Derive a sidebar title from the first prompt, the way ChatGPT does."""
    text = " ".join(safe_ui_text(prompt, limit * 4).split())
    text = re.sub(r"^[#>*\-\s`]+", "", text)
    if not text:
        return DEFAULT_CHAT_TITLE
    if len(text) <= limit:
        return text
    cut = text[:limit]
    if " " in cut[limit // 2:]:
        cut = cut[: cut.rfind(" ")]
    return cut.rstrip(" ,;:.") + "…"


# --------------------------------------------------------------------------
# Markdown → blocks (pure, testable)
# --------------------------------------------------------------------------

_FENCE = re.compile(r"^\s*(```+|~~~+)\s*([A-Za-z0-9_+.#-]*)\s*$")
_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_HR = re.compile(r"^\s*([-*_])(?:\s*\1){2,}\s*$")
_UL = re.compile(r"^(\s*)[-*+]\s+(.*)$")
_OL = re.compile(r"^(\s*)(\d{1,3})[.)]\s+(.*)$")
_QUOTE = re.compile(r"^\s*>\s?(.*)$")
_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")
_TASK = re.compile(r"^\[([ xX])\]\s+(.*)$")


def _split_table_row(line: str) -> list[str]:
    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    return [cell.strip() for cell in stripped.split("|")]


def parse_markdown(text: str) -> list[dict[str, Any]]:
    """Convert markdown text into a small list of display blocks.

    The parser is deliberately conservative: anything it does not recognise is
    shown as a paragraph, never dropped.
    """
    lines = str(text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    blocks: list[dict[str, Any]] = []
    paragraph: list[str] = []

    def flush_paragraph() -> None:
        if paragraph:
            blocks.append({"type": "paragraph", "text": "\n".join(paragraph).strip("\n")})
            paragraph.clear()

    index = 0
    total = len(lines)
    while index < total:
        line = lines[index]
        fence = _FENCE.match(line)
        if fence:
            flush_paragraph()
            marker = fence.group(1)[0]
            language = fence.group(2).lower()
            code: list[str] = []
            index += 1
            while index < total:
                candidate = lines[index]
                closing = _FENCE.match(candidate)
                if closing and closing.group(1)[0] == marker and not closing.group(2):
                    index += 1
                    break
                code.append(candidate)
                index += 1
            blocks.append({"type": "code", "lang": language, "text": "\n".join(code)})
            continue
        if not line.strip():
            flush_paragraph()
            index += 1
            continue
        heading = _HEADING.match(line)
        if heading:
            flush_paragraph()
            blocks.append({
                "type": "heading",
                "level": len(heading.group(1)),
                "text": heading.group(2),
            })
            index += 1
            continue
        if _HR.match(line):
            flush_paragraph()
            blocks.append({"type": "hr"})
            index += 1
            continue
        if _QUOTE.match(line):
            flush_paragraph()
            quoted: list[str] = []
            while index < total:
                quote = _QUOTE.match(lines[index])
                if not quote:
                    break
                quoted.append(quote.group(1))
                index += 1
            blocks.append({"type": "quote", "text": "\n".join(quoted).strip()})
            continue
        if _UL.match(line) or _OL.match(line):
            flush_paragraph()
            ordered = bool(_OL.match(line))
            items: list[dict[str, Any]] = []
            while index < total:
                current = lines[index]
                match = _OL.match(current) if ordered else _UL.match(current)
                if match:
                    indent = len(match.group(1).replace("\t", "    "))
                    body = match.group(3) if ordered else match.group(2)
                    task = _TASK.match(body)
                    items.append({
                        "indent": indent // 2,
                        "text": task.group(2) if task else body,
                        "checked": (task.group(1).lower() == "x") if task else None,
                    })
                    index += 1
                    continue
                if items and current.strip() and (current.startswith("  ") or current.startswith("\t")):
                    items[-1]["text"] += "\n" + current.strip()
                    index += 1
                    continue
                break
            blocks.append({"type": "list", "ordered": ordered, "items": items})
            continue
        if "|" in line and index + 1 < total and _TABLE_SEP.match(lines[index + 1]):
            flush_paragraph()
            rows = [_split_table_row(line)]
            index += 2
            while index < total and "|" in lines[index] and lines[index].strip():
                rows.append(_split_table_row(lines[index]))
                index += 1
            blocks.append({"type": "table", "rows": rows})
            continue
        paragraph.append(line)
        index += 1
    flush_paragraph()
    return blocks


_INLINE = re.compile(
    r"(?P<code>`+)(?P<code_text>.+?)(?P=code)"
    r"|\[(?P<link_text>[^\]\n]{1,300})\]\((?P<link_url>https?://[^\s)]+)\)"
    r"|(?P<url>https?://[^\s<>\"')\]]+)"
    r"|\*\*(?P<bold>[^*\n]+?)\*\*"
    r"|__(?P<bold2>[^_\n]+?)__"
    r"|(?<![A-Za-z0-9*])\*(?P<italic>[^*\n]+?)\*(?![A-Za-z0-9*])"
    r"|(?<![A-Za-z0-9_])_(?P<italic2>[^_\n]+?)_(?![A-Za-z0-9_])"
    r"|~~(?P<strike>[^~\n]+?)~~",
)


def inline_runs(text: str) -> list[tuple[str, str, str | None]]:
    """Split inline markdown into ``(style, text, url)`` runs."""
    runs: list[tuple[str, str, str | None]] = []
    cursor = 0
    for match in _INLINE.finditer(text):
        if match.start() > cursor:
            runs.append(("text", text[cursor:match.start()], None))
        if match.group("code"):
            runs.append(("code", match.group("code_text"), None))
        elif match.group("link_text"):
            runs.append(("link", match.group("link_text"), match.group("link_url")))
        elif match.group("url"):
            url = match.group("url")
            while url and url[-1] in ".,;:!?":
                url = url[:-1]
            runs.append(("link", url, url))
            trailing = match.group("url")[len(url):]
            if trailing:
                runs.append(("text", trailing, None))
        elif match.group("bold") or match.group("bold2"):
            runs.append(("bold", match.group("bold") or match.group("bold2"), None))
        elif match.group("italic") or match.group("italic2"):
            runs.append(("italic", match.group("italic") or match.group("italic2"), None))
        elif match.group("strike"):
            runs.append(("strike", match.group("strike"), None))
        cursor = match.end()
    if cursor < len(text):
        runs.append(("text", text[cursor:], None))
    return runs


def safe_http_url(value: str | None) -> str | None:
    raw = str(value or "").strip()
    if not re.fullmatch(r"https?://[^\s/?#]+[^\s]*", raw) or "@" in raw.split("/")[2]:
        return None
    return raw


def render_table_text(rows: list[list[str]]) -> str:
    """Lay a markdown table out as aligned monospace text."""
    if not rows:
        return ""
    width = max(len(row) for row in rows)
    normalized = [row + [""] * (width - len(row)) for row in rows]
    columns = [
        min(48, max(len(row[column]) for row in normalized)) for column in range(width)
    ]
    lines = []
    for row_index, row in enumerate(normalized):
        cells = [cell[: columns[i]].ljust(columns[i]) for i, cell in enumerate(row)]
        lines.append("  ".join(cells).rstrip())
        if row_index == 0:
            lines.append("  ".join("─" * size for size in columns))
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Session (worker thread that owns SQLite + Agent)
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class SessionEvent:
    kind: str
    payload: Any = None


def _safe_call(target: Any, name: str, *args: Any, default: Any = None, **kwargs: Any) -> Any:
    method = getattr(target, name, None)
    if not callable(method):
        return default
    try:
        return method(*args, **kwargs)
    except Exception:
        return default


def _chat_rows(memory: Any, limit: int = 120) -> list[dict[str, Any]]:
    rows = _safe_call(memory, "list_conversations", limit=limit, default=None)
    if not isinstance(rows, list):
        return []
    internal = getattr(memory, "is_screen_companion_conversation", None)
    chats: list[dict[str, Any]] = []
    for row in rows:
        try:
            conversation_id = int(row.get("id"))
        except (TypeError, ValueError, AttributeError):
            continue
        if callable(internal):
            try:
                if internal(conversation_id):
                    continue
            except Exception:
                pass
        chats.append({
            "id": conversation_id,
            "title": compact_activity(row.get("title") or DEFAULT_CHAT_TITLE, 120),
            "created_at": str(row.get("created_at") or "")[:40],
            "message_count": int(row.get("message_count") or 0),
            "project_name": compact_activity(row.get("project_name") or "", 80),
        })
    return chats


def _chat_messages(memory: Any, conversation_id: int) -> list[dict[str, Any]]:
    db = getattr(memory, "db", None)
    if db is not None:
        try:
            rows = db.execute(
                "SELECT role, content, created_at FROM messages "
                "WHERE conversation_id=? ORDER BY id DESC LIMIT 300",
                (int(conversation_id),),
            ).fetchall()
            return [
                {
                    "role": str(row["role"]),
                    "content": str(row["content"]),
                    "created_at": str(row["created_at"] or ""),
                }
                for row in reversed(rows)
            ]
        except Exception:
            pass
    rows = _safe_call(memory, "recent_messages", conversation_id, limit=300, default=[])
    return [
        {"role": str(row.get("role", "assistant")), "content": str(row.get("content", "")), "created_at": ""}
        for row in rows or []
    ]


def _rename_chat(memory: Any, conversation_id: int, title: str) -> bool:
    db = getattr(memory, "db", None)
    clean = " ".join(safe_ui_text(title, 120).split())[:120]
    if db is None or not clean:
        return False
    try:
        db.execute(
            "UPDATE conversations SET title=? WHERE id=?",
            (clean, int(conversation_id)),
        )
        return True
    except Exception:
        return False


def _iso_to_epoch(value: str) -> float:
    try:
        return datetime.fromisoformat(str(value)).timestamp()
    except (TypeError, ValueError):
        return 0.0


class JarvisSession(threading.Thread):
    """Own SQLite and Agent on one thread; Tk remains isolated on its UI thread."""

    def __init__(self, config: Config) -> None:
        super().__init__(name="jarvis-desktop-session", daemon=True)
        self.config = config
        self.commands: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.events: queue.Queue[SessionEvent] = queue.Queue()
        self.cancel_event = threading.Event()
        self._shutdown = threading.Event()

    # -- API used by the UI thread ---------------------------------------

    def emit(self, kind: str, payload: Any = None) -> None:
        self.events.put(SessionEvent(kind, payload))

    def submit(
        self,
        prompt: str,
        model_label: str,
        attachments: list[str] | None = None,
    ) -> None:
        self.commands.put(
            ("send", (prompt, model_override_for(model_label), list(attachments or [])))
        )

    def new_chat(self) -> None:
        self.commands.put(("new_chat", None))

    def load_chat(self, conversation_id: int) -> None:
        self.commands.put(("load_chat", int(conversation_id)))

    def list_chats(self) -> None:
        self.commands.put(("list_chats", None))

    def rename_chat(self, conversation_id: int, title: str) -> None:
        self.commands.put(("rename_chat", (int(conversation_id), str(title))))

    def delete_chat(self, conversation_id: int) -> None:
        self.commands.put(("delete_chat", int(conversation_id)))

    def retry_provider(self) -> None:
        self.commands.put(("retry_provider", None))

    def request_approvals(self) -> None:
        self.commands.put(("approvals", None))

    def decide_approval(self, approval_id: int, approve: bool) -> None:
        self.commands.put(("decide_approval", (int(approval_id), bool(approve))))

    def cancel(self) -> None:
        self.cancel_event.set()

    def shutdown(self) -> None:
        self._shutdown.set()
        self.cancel_event.set()
        self.commands.put(("shutdown", None))

    # -- worker thread ----------------------------------------------------

    def _run_prompt(
        self,
        agent: Agent,
        memory: Memory,
        conversation_id: int,
        prompt: str,
        model_override: str,
        attachment_paths: list[str],
    ) -> None:
        self.cancel_event.clear()
        self.emit("busy", True)
        self.emit("activity", "Preparing request")
        started = time.monotonic()
        runtime_guard = RuntimeGuard(memory, self.config, background=False)

        def cancelled() -> bool:
            return self.cancel_event.is_set() or runtime_guard()

        def on_delta(text: str) -> None:
            fragment = safe_ui_text(text, 20_000)
            if fragment:
                self.emit("delta", {"conversation_id": conversation_id, "text": fragment})

        attachments: list[ImageAttachment] = []
        try:
            for path in attachment_paths[:MAX_IMAGE_ATTACHMENTS]:
                attachments.append(ImageAttachment.from_path(path))
        except ValueError as exc:
            self.emit("assistant", {
                "conversation_id": conversation_id,
                "content": f"I could not attach that image: {safe_ui_text(exc, 400)}",
                "status": "incomplete",
                "reason": "attachment rejected",
                "approval_id": None,
                "model": None,
                "elapsed": 0.0,
            })
            self.emit("busy", False)
            self.emit("activity", "Ready")
            return
        try:
            with _ForegroundLease(self.config.data_dir):
                run_kwargs: dict[str, Any] = {
                    "conversation_id": conversation_id,
                    "model_override": model_override,
                    "cancellation_guard": cancelled,
                    "prediction_origin": "interactive",
                    "stream_callback": on_delta,
                }
                if attachments:
                    run_kwargs["attachments"] = tuple(attachments)
                result = agent.run(prompt, **run_kwargs)
            metrics = getattr(result, "metrics", None)
            self.emit(
                "assistant",
                {
                    "conversation_id": conversation_id,
                    "content": safe_ui_text(result),
                    "status": str(getattr(result, "status", "complete")),
                    "reason": safe_ui_text(getattr(result, "reason", "") or "", 1_000),
                    "approval_id": getattr(result, "approval_id", None),
                    "model": compact_activity(getattr(result, "model", "") or "", 80) or None,
                    "tool_calls": int(getattr(result, "tool_calls", 0) or 0),
                    "elapsed": round(time.monotonic() - started, 2),
                    "metrics": dict(metrics) if isinstance(metrics, dict) else {},
                },
            )
        except AgentRunCancelled:
            self.emit("assistant", {
                "conversation_id": conversation_id,
                "content": "Request stopped.",
                "status": "cancelled",
                "reason": "",
                "approval_id": None,
                "model": None,
                "elapsed": round(time.monotonic() - started, 2),
            })
        except OllamaError as exc:
            self.emit("assistant", {
                "conversation_id": conversation_id,
                "content": user_model_error_message(exc),
                "status": "incomplete",
                "reason": "model provider unavailable after automatic fallbacks",
                "approval_id": None,
                "model": None,
                "elapsed": round(time.monotonic() - started, 2),
            })
        except Exception as exc:
            self.emit(
                "error",
                {
                    "conversation_id": conversation_id,
                    "message": f"Jarvis could not complete this request ({type(exc).__name__}): {exc}",
                },
            )
        finally:
            self.cancel_event.clear()
            self.emit("busy", False)
            self.emit("activity", "Ready")

    def _emit_chats(self, memory: Any) -> None:
        self.emit("chats", _chat_rows(memory))

    def _build_agent(self, memory: Any) -> tuple[Any | None, str | None]:
        """Create the Agent, returning ``(agent, error)`` instead of raising.

        The model provider may be offline when the window opens (Ollama not
        started yet, a CLI provider signed out). The desktop stays usable and
        retries on the next send instead of dying with a modal error.
        """
        try:
            agent = Agent(
                self.config,
                memory,
                lambda message: self.emit("activity", compact_activity(message)),
            )
        except OllamaError as exc:
            return None, user_model_error_message(exc)
        except Exception as exc:  # provider wiring problems are recoverable
            return None, f"Model provider unavailable ({type(exc).__name__}): {safe_ui_text(exc, 300)}"
        return agent, None

    def run(self) -> None:
        try:
            with Memory(self.config.data_dir / "jarvis.db") as memory:
                agent, provider_error = self._build_agent(memory)
                conversation_id = memory.new_conversation(DEFAULT_CHAT_TITLE)
                control = memory.control_state()
                self.emit("ready", {
                    "conversation_id": conversation_id,
                    "control_state": str(control.get("state", "running")),
                    "fast_model": getattr(self.config, "fast_model", ""),
                    "reasoning_model": getattr(self.config, "reasoning_model", ""),
                    "coding_model": getattr(self.config, "coding_model", ""),
                    "deep_model": getattr(self.config, "deep_model", ""),
                    "chats": _chat_rows(memory),
                    "provider_error": provider_error,
                })
                untitled = True

                while not self._shutdown.is_set():
                    command, payload = self.commands.get()
                    if command == "shutdown":
                        break
                    if command == "send":
                        prompt, model_override, attachment_paths = payload
                        if agent is None:
                            agent, provider_error = self._build_agent(memory)
                            self.emit("provider", {"error": provider_error})
                        if agent is None:
                            self.emit("busy", True)
                            self.emit("assistant", {
                                "conversation_id": conversation_id,
                                "content": provider_error or "The model provider is unavailable.",
                                "status": "incomplete",
                                "reason": "model provider unavailable",
                                "approval_id": None,
                                "model": None,
                                "elapsed": 0.0,
                            })
                            self.emit("busy", False)
                            self.emit("activity", "Ready")
                            continue
                        if untitled:
                            if _rename_chat(memory, conversation_id, chat_title_from_prompt(prompt)):
                                untitled = False
                                self._emit_chats(memory)
                        self._run_prompt(
                            agent,
                            memory,
                            conversation_id,
                            str(prompt),
                            str(model_override),
                            list(attachment_paths or []),
                        )
                        self._emit_chats(memory)
                    elif command == "retry_provider":
                        agent, provider_error = self._build_agent(memory)
                        self.emit("provider", {"error": provider_error})
                    elif command == "new_chat":
                        conversation_id = memory.new_conversation(DEFAULT_CHAT_TITLE)
                        untitled = True
                        self.emit("new_chat", {"conversation_id": conversation_id})
                        self._emit_chats(memory)
                    elif command == "load_chat":
                        target = int(payload)
                        exists = _safe_call(memory, "conversation_exists", target, default=True)
                        if not exists:
                            self.emit("error", {"message": "That chat no longer exists."})
                            self._emit_chats(memory)
                            continue
                        conversation_id = target
                        rows = _chat_messages(memory, target)
                        titles = {row["id"]: row["title"] for row in _chat_rows(memory)}
                        title = titles.get(target, DEFAULT_CHAT_TITLE)
                        untitled = title.strip().casefold() in LEGACY_CHAT_TITLES
                        self.emit("chat_loaded", {
                            "conversation_id": target,
                            "title": title,
                            "messages": [
                                {
                                    "role": row["role"],
                                    "content": safe_ui_text(row["content"]),
                                    "created_at": _iso_to_epoch(row.get("created_at", "")),
                                }
                                for row in rows
                            ],
                        })
                    elif command == "list_chats":
                        self._emit_chats(memory)
                    elif command == "rename_chat":
                        target, title = payload
                        if _rename_chat(memory, target, title):
                            if target == conversation_id:
                                untitled = False
                            self.emit("chat_renamed", {"conversation_id": target, "title": title})
                        self._emit_chats(memory)
                    elif command == "delete_chat":
                        target = int(payload)
                        deleted = _safe_call(memory, "delete_conversation", target, default=None)
                        self.emit("chat_deleted", {"conversation_id": target, "deleted": deleted is not None})
                        if target == conversation_id:
                            conversation_id = memory.new_conversation(DEFAULT_CHAT_TITLE)
                            untitled = True
                            self.emit("new_chat", {"conversation_id": conversation_id})
                        self._emit_chats(memory)
                    elif command == "approvals":
                        self.emit("approvals", memory.list_approvals(limit=100))
                    elif command == "decide_approval":
                        approval_id, approve = payload
                        changed = memory.decide_approval(
                            approval_id,
                            approve,
                            ttl_hours=self.config.approval_ttl_hours,
                        )
                        self.emit("approval_decided", {
                            "approval_id": approval_id,
                            "approved": approve,
                            "changed": changed,
                        })
                        self.emit("approvals", memory.list_approvals(limit=100))
        except Exception as exc:
            self.emit(
                "fatal",
                f"Jarvis Desktop could not start ({type(exc).__name__}): {exc}",
            )


# --------------------------------------------------------------------------
# Theme + settings
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Theme:
    key: str
    name: str
    dark: bool
    bg: str
    panel: str
    surface: str
    surface_alt: str
    surface_hover: str
    border: str
    border_strong: str
    text: str
    text_strong: str
    muted: str
    faint: str
    accent: str
    accent_hover: str
    accent_ink: str
    accent_soft: str
    user_bubble: str
    code_bg: str
    code_head: str
    success: str
    warning: str
    danger: str
    danger_soft: str
    info: str
    selection: str


THEMES: dict[str, Theme] = {
    "midnight": Theme(
        key="midnight", name="Midnight", dark=True,
        bg="#07090c", panel="#0b0e12", surface="#12161c", surface_alt="#181d24",
        surface_hover="#1f252d", border="#1c232b", border_strong="#2a333d",
        text="#e6edf3", text_strong="#ffffff", muted="#8b98a5", faint="#5c6875",
        accent="#3ecfb2", accent_hover="#62dcc3", accent_ink="#04110f", accent_soft="#0f2b27",
        user_bubble="#14202a", code_bg="#0b0f14", code_head="#121820",
        success="#3ecfb2", warning="#f0b84a", danger="#ff6b72", danger_soft="#2b1518",
        info="#8f7bff", selection="#1e3a44",
    ),
    "graphite": Theme(
        key="graphite", name="Graphite", dark=True,
        bg="#212121", panel="#171717", surface="#2a2a2a", surface_alt="#303030",
        surface_hover="#383838", border="#2e2e2e", border_strong="#424242",
        text="#ececec", text_strong="#ffffff", muted="#a8a8a8", faint="#737373",
        accent="#f2f2f2", accent_hover="#ffffff", accent_ink="#141414", accent_soft="#333333",
        user_bubble="#2f2f2f", code_bg="#0d0d0d", code_head="#1c1c1c",
        success="#7ed9a2", warning="#f0c36a", danger="#ff8080", danger_soft="#3a2323",
        info="#b3a1ff", selection="#3d4a57",
    ),
    "paper": Theme(
        key="paper", name="Paper", dark=False,
        bg="#f6f3ec", panel="#eeeae1", surface="#ffffff", surface_alt="#f3efe6",
        surface_hover="#e9e4d9", border="#e3ded2", border_strong="#cfc8b8",
        text="#2b2620", text_strong="#171310", muted="#6b6257", faint="#9a9184",
        accent="#c2603d", accent_hover="#a94f31", accent_ink="#ffffff", accent_soft="#f7e3da",
        user_bubble="#efe7dc", code_bg="#f4f1ea", code_head="#e8e2d6",
        success="#2f8f5b", warning="#a26f0b", danger="#c0392b", danger_soft="#f8e1de",
        info="#6b52c8", selection="#e2d7c6",
    ),
}
THEME_ORDER = ("midnight", "graphite", "paper")


class DesktopSettings:
    """Tiny JSON settings file under the data directory (never secrets)."""

    def __init__(self, data_dir: Path) -> None:
        self.path = Path(data_dir) / SETTINGS_FILE
        self.values: dict[str, Any] = {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                self.values = payload
        except (OSError, ValueError):
            self.values = {}

    def get(self, key: str, default: Any = None) -> Any:
        return self.values.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self.values[key] = value
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(self.values, indent=2, sort_keys=True), encoding="utf-8"
            )
        except OSError:
            pass


# --------------------------------------------------------------------------
# Tk helpers
# --------------------------------------------------------------------------

def _enable_high_dpi() -> None:
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def _apply_titlebar_theme(root: tk.Misc, dark: bool) -> None:
    """Ask DWM for a dark (or light) title bar on Windows 10 20H1+ / 11."""
    if sys.platform != "win32":
        return
    try:
        root.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
        value = ctypes.c_int(1 if dark else 0)
        for attribute in (20, 19):
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, attribute, ctypes.byref(value), ctypes.sizeof(value)
            ) == 0:
                break
    except Exception:
        pass


def rounded_points(x1: float, y1: float, x2: float, y2: float, radius: float) -> list[float]:
    radius = max(0.0, min(radius, (x2 - x1) / 2, (y2 - y1) / 2))
    return [
        x1 + radius, y1, x2 - radius, y1, x2, y1, x2, y1 + radius,
        x2, y2 - radius, x2, y2, x2 - radius, y2, x1 + radius, y2,
        x1, y2, x1, y2 - radius, x1, y1 + radius, x1, y1,
    ]


class Fonts:
    def __init__(self, root: tk.Misc) -> None:
        families = set(tkfont.families(root))
        ui = next((name for name in ("Segoe UI Variable Text", "Segoe UI", "Inter") if name in families), "TkDefaultFont")
        display = "Segoe UI Variable Display" if "Segoe UI Variable Display" in families else ui
        mono = next((name for name in ("Cascadia Code", "Cascadia Mono", "JetBrains Mono", "Consolas") if name in families), "TkFixedFont")
        symbol = "Segoe UI Symbol" if "Segoe UI Symbol" in families else ui
        self.family = ui
        self.body = tkfont.Font(root, family=ui, size=11)
        self.body_bold = tkfont.Font(root, family=ui, size=11, weight="bold")
        self.body_italic = tkfont.Font(root, family=ui, size=11, slant="italic")
        self.body_strike = tkfont.Font(root, family=ui, size=11, overstrike=True)
        self.small = tkfont.Font(root, family=ui, size=9)
        self.small_bold = tkfont.Font(root, family=ui, size=9, weight="bold")
        self.tiny = tkfont.Font(root, family=ui, size=8)
        self.label = tkfont.Font(root, family=ui, size=10)
        self.label_bold = tkfont.Font(root, family=ui, size=10, weight="bold")
        self.title = tkfont.Font(root, family=display, size=13, weight="bold")
        self.hero = tkfont.Font(root, family=display, size=22, weight="bold")
        self.h1 = tkfont.Font(root, family=display, size=16, weight="bold")
        self.h2 = tkfont.Font(root, family=display, size=14, weight="bold")
        self.h3 = tkfont.Font(root, family=display, size=12, weight="bold")
        self.mono = tkfont.Font(root, family=mono, size=10)
        self.mono_small = tkfont.Font(root, family=mono, size=9)
        self.inline_code = tkfont.Font(root, family=mono, size=10)
        self.icon = tkfont.Font(root, family=symbol, size=12)
        self.icon_small = tkfont.Font(root, family=symbol, size=10)
        self.avatar = tkfont.Font(root, family=display, size=10, weight="bold")


class Tooltip:
    def __init__(self, widget: tk.Widget, text: str, app: "JarvisDesktop") -> None:
        self.widget = widget
        self.text = text
        self.app = app
        self.window: tk.Toplevel | None = None
        self._after: str | None = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, _event: Any = None) -> None:
        self._after = self.widget.after(550, self._show)

    def _show(self) -> None:
        if self.window is not None:
            return
        theme = self.app.theme
        try:
            x = self.widget.winfo_rootx() + 10
            y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        except tk.TclError:
            return
        self.window = tk.Toplevel(self.widget)
        self.window.overrideredirect(True)
        self.window.attributes("-topmost", True)
        label = tk.Label(
            self.window, text=self.text, bg=theme.surface_alt, fg=theme.text,
            font=self.app.fonts.small, padx=9, pady=5,
            highlightbackground=theme.border_strong, highlightthickness=1,
        )
        label.pack()
        self.window.geometry(f"+{x}+{y}")

    def _hide(self, _event: Any = None) -> None:
        if self._after is not None:
            try:
                self.widget.after_cancel(self._after)
            except tk.TclError:
                pass
            self._after = None
        if self.window is not None:
            try:
                self.window.destroy()
            except tk.TclError:
                pass
            self.window = None


class RoundButton(tk.Canvas):
    """A rounded, hoverable button drawn on a canvas (Tk has no CSS)."""

    def __init__(
        self,
        master: tk.Misc,
        app: "JarvisDesktop",
        text: str,
        command: Callable[[], None] | None = None,
        *,
        kind: str = "ghost",
        font: tkfont.Font | None = None,
        padx: int = 14,
        pady: int = 7,
        width: int | None = None,
        radius: int = 9,
        icon: str | None = None,
        tooltip: str | None = None,
    ) -> None:
        self.app = app
        self.theme = app.theme
        self.kind = kind
        self.command = command
        self.text = text
        self.icon = icon
        self.font = font or app.fonts.label_bold if kind == "accent" else (font or app.fonts.label)
        self.radius = radius
        self.enabled = True
        self._hover = False
        self._active = False
        label_width = self.font.measure(text) + (self.font.measure(icon + " ") if icon else 0)
        height = self.font.metrics("linespace") + pady * 2
        total = width or (label_width + padx * 2)
        super().__init__(
            master, width=total, height=height, bd=0, highlightthickness=0,
            bg=self._parent_bg(master), cursor="hand2",
        )
        self._width = total
        self._height = height
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)
        self.bind("<Configure>", lambda _event: self.redraw())
        if tooltip:
            Tooltip(self, tooltip, app)
        self.redraw()

    @staticmethod
    def _parent_bg(master: tk.Misc) -> str:
        try:
            return str(master.cget("bg"))
        except tk.TclError:
            return "#000000"

    def _colors(self) -> tuple[str, str, str]:
        theme = self.theme
        if not self.enabled:
            return theme.surface, theme.faint, theme.border
        if self.kind == "accent":
            fill = theme.accent_hover if self._hover else theme.accent
            return fill, theme.accent_ink, fill
        if self.kind == "danger":
            fill = theme.danger if self._hover else theme.danger_soft
            return fill, (theme.accent_ink if self._hover else theme.danger), theme.danger
        if self.kind == "subtle":
            fill = theme.surface_hover if self._hover else self._parent_bg(self.master)
            return fill, theme.text if self._hover else theme.muted, fill
        if self.kind == "active":
            return theme.accent_soft, theme.accent, theme.accent
        fill = theme.surface_hover if self._hover else theme.surface
        return fill, theme.text, theme.border_strong if self._hover else theme.border

    def redraw(self) -> None:
        self.delete("all")
        width = max(self.winfo_width(), self._width)
        height = max(self.winfo_height(), self._height)
        fill, ink, outline = self._colors()
        self.create_polygon(
            rounded_points(1, 1, width - 1, height - 1, self.radius),
            smooth=True, splinesteps=24, fill=fill, outline=outline, width=1,
        )
        label = f"{self.icon}  {self.text}" if self.icon else self.text
        self.create_text(width / 2, height / 2, text=label, fill=ink, font=self.font)

    def set_text(self, text: str) -> None:
        self.text = text
        self.redraw()

    def set_kind(self, kind: str) -> None:
        self.kind = kind
        self.redraw()

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = bool(enabled)
        self.configure(cursor="hand2" if self.enabled else "arrow")
        self.redraw()

    def _on_enter(self, _event: Any) -> None:
        self._hover = True
        self.redraw()

    def _on_leave(self, _event: Any) -> None:
        self._hover = False
        self._active = False
        self.redraw()

    def _on_press(self, _event: Any) -> None:
        self._active = True

    def _on_release(self, event: Any) -> None:
        if self._active and self.enabled and self.command is not None:
            inside = 0 <= event.x <= self.winfo_width() and 0 <= event.y <= self.winfo_height()
            if inside:
                self.command()
        self._active = False


class IconButton(tk.Label):
    """Flat glyph button (the kind every chat app hides in the corner)."""

    def __init__(
        self,
        master: tk.Misc,
        app: "JarvisDesktop",
        glyph: str,
        command: Callable[[], None] | None,
        *,
        tooltip: str | None = None,
        size: int = 30,
        color: str | None = None,
    ) -> None:
        theme = app.theme
        self.app = app
        self.command = command
        self.base_bg = RoundButton._parent_bg(master)
        self.color = color or theme.muted
        super().__init__(
            master, text=glyph, font=app.fonts.icon, fg=self.color, bg=self.base_bg,
            width=2, cursor="hand2", padx=2, pady=2,
        )
        self.configure(width=2)
        self.bind("<Enter>", lambda _e: self.configure(bg=theme.surface_hover, fg=theme.text))
        self.bind("<Leave>", lambda _e: self.configure(bg=self.base_bg, fg=self.color))
        self.bind("<Button-1>", lambda _e: self.command() if self.command else None)
        if tooltip:
            Tooltip(self, tooltip, app)


class ScrollFrame(tk.Frame):
    """Canvas-backed vertical scroller with a thin, theme-aware scrollbar."""

    _owners: list["ScrollFrame"] = []
    _wheel_bound = False

    def __init__(self, master: tk.Misc, app: "JarvisDesktop", *, bg: str) -> None:
        super().__init__(master, bg=bg)
        self.app = app
        self.canvas = tk.Canvas(self, bd=0, highlightthickness=0, bg=bg)
        self.scrollbar = ttk.Scrollbar(
            self, orient="vertical", command=self.canvas.yview, style="Jarvis.Vertical.TScrollbar"
        )
        self.inner = tk.Frame(self.canvas, bg=bg)
        self.window = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.canvas.configure(yscrollcommand=self._on_scroll)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.scrollbar.pack(side="right", fill="y")
        self.inner.bind("<Configure>", self._on_inner_configure)
        self.canvas.bind("<Configure>", self._on_canvas_configure)
        self.stick_to_bottom = False
        ScrollFrame._owners.append(self)
        if not ScrollFrame._wheel_bound:
            ScrollFrame._wheel_bound = True
            master.winfo_toplevel().bind_all("<MouseWheel>", ScrollFrame._dispatch_wheel, add="+")

    def destroy(self) -> None:
        if self in ScrollFrame._owners:
            ScrollFrame._owners.remove(self)
        super().destroy()

    @staticmethod
    def _dispatch_wheel(event: Any) -> None:
        try:
            widget = event.widget.winfo_containing(event.x_root, event.y_root)
        except (tk.TclError, AttributeError):
            return
        while widget is not None:
            if isinstance(widget, ScrollFrame):
                widget.scroll_by(event.delta)
                return
            widget = getattr(widget, "master", None)

    def scroll_by(self, delta: int) -> None:
        if self.canvas.bbox("all") is None:
            return
        first, last = self.canvas.yview()
        if first <= 0.0 and last >= 1.0:
            return
        self.canvas.yview_scroll(int(-delta / 40), "units")
        self.stick_to_bottom = self.canvas.yview()[1] >= 0.995

    def _on_scroll(self, first: str, last: str) -> None:
        self.scrollbar.set(first, last)
        if float(last) >= 1.0 and float(first) <= 0.0:
            self.scrollbar.pack_forget()
        elif not self.scrollbar.winfo_ismapped():
            self.scrollbar.pack(side="right", fill="y")

    def _sync_region(self) -> None:
        """Keep the scroll region equal to the content and never show a void.

        Tk keeps the old scroll fraction when the region shrinks, which would
        leave the viewport below the new, shorter content. Clamp explicitly.
        """
        try:
            height = max(1, self.inner.winfo_reqheight())
            width = max(1, self.canvas.winfo_width())
            self.canvas.configure(scrollregion=(0, 0, width, height))
            if height <= self.canvas.winfo_height():
                self.canvas.yview_moveto(0.0)
            elif self.stick_to_bottom:
                self.canvas.yview_moveto(1.0)
        except tk.TclError:
            pass

    def _on_inner_configure(self, _event: Any) -> None:
        self._sync_region()

    def _on_canvas_configure(self, event: Any) -> None:
        self.canvas.itemconfigure(self.window, width=event.width)
        self._sync_region()

    def scroll_to_end(self) -> None:
        self.stick_to_bottom = True
        self.update_idletasks()
        self._sync_region()
        self.canvas.yview_moveto(1.0)

    def scroll_to_top(self) -> None:
        self.stick_to_bottom = False
        self.canvas.yview_moveto(0.0)


class AutoText(tk.Text):
    """Read-only text that grows to fit its content at the current width."""

    def __init__(self, master: tk.Misc, app: "JarvisDesktop", *, font: tkfont.Font, bg: str, fg: str, wrap: str = "word", **kwargs: Any) -> None:
        theme = app.theme
        super().__init__(
            master, wrap=wrap, bd=0, highlightthickness=0, relief="flat",
            padx=0, pady=0, height=1, font=font, bg=bg, fg=fg, cursor="arrow",
            selectbackground=theme.selection, selectforeground=theme.text_strong,
            insertwidth=0, spacing1=1, spacing3=1, **kwargs,
        )
        self.app = app
        self._last_width = 0
        self.bind("<Configure>", self._on_configure)
        self.configure(state="disabled")

    def _on_configure(self, event: Any) -> None:
        if event.width != self._last_width:
            self._last_width = event.width
            self.after_idle(self.fit)

    def fit(self) -> None:
        # Count to "end" (not "end-1c"): Tk reports display lines *crossed*,
        # so the final line is only included when the terminating newline is.
        try:
            result = self.count("1.0", "end", "displaylines")
        except tk.TclError:
            return
        count = result[0] if isinstance(result, (tuple, list)) else result
        lines = max(1, int(count or 1))
        try:
            if int(self.cget("height")) != lines:
                self.configure(height=lines)
        except tk.TclError:
            pass

    def settle(self) -> None:
        """Fit now and again shortly after, once geometry has propagated."""
        self.after_idle(self.fit)
        self.after(90, self.fit)

    def set_text(self, text: str) -> None:
        self.configure(state="normal")
        self.delete("1.0", "end")
        self.insert("1.0", text)
        self.configure(state="disabled")
        self.settle()

    def append_text(self, text: str) -> None:
        self.configure(state="normal")
        self.insert("end", text)
        self.configure(state="disabled")
        self.after_idle(self.fit)

    def plain_text(self) -> str:
        return self.get("1.0", "end-1c")


class GrowText(tk.Text):
    """Composer input that grows between ``min_lines`` and ``max_lines``."""

    def __init__(self, master: tk.Misc, app: "JarvisDesktop", *, min_lines: int = 1, max_lines: int = 9, **kwargs: Any) -> None:
        theme = app.theme
        super().__init__(
            master, wrap="word", bd=0, highlightthickness=0, relief="flat", padx=2, pady=4,
            height=min_lines, font=app.fonts.body, bg=theme.surface, fg=theme.text,
            insertbackground=theme.accent, insertwidth=2,
            selectbackground=theme.selection, selectforeground=theme.text_strong,
            undo=True, maxundo=200, **kwargs,
        )
        self.min_lines = min_lines
        self.max_lines = max_lines
        self.on_change: Callable[[], None] | None = None
        self.bind("<<Modified>>", self._on_modified)
        self.bind("<Configure>", lambda _e: self.after_idle(self.fit))

    def _on_modified(self, _event: Any) -> None:
        if self.edit_modified():
            self.edit_modified(False)
            self.after_idle(self.fit)
            if self.on_change:
                self.on_change()

    def fit(self) -> None:
        try:
            result = self.count("1.0", "end", "displaylines")
        except tk.TclError:
            return
        count = result[0] if isinstance(result, (tuple, list)) else result
        lines = min(self.max_lines, max(self.min_lines, int(count or 1)))
        try:
            if int(self.cget("height")) != lines:
                self.configure(height=lines)
        except tk.TclError:
            pass

    def value(self) -> str:
        return self.get("1.0", "end-1c")

    def set_value(self, text: str) -> None:
        self.delete("1.0", "end")
        self.insert("1.0", text)
        self.after_idle(self.fit)


# --------------------------------------------------------------------------
# Conversation model + message cards
# --------------------------------------------------------------------------

@dataclass
class Message:
    role: str
    content: str
    created_at: float = field(default_factory=time.time)
    status: str = "complete"
    model: str | None = None
    elapsed: float | None = None
    approval_id: int | None = None
    tool_calls: int = 0
    attachments: list[str] = field(default_factory=list)
    steps: list[str] = field(default_factory=list)
    streaming: bool = False
    working: bool = False
    error: bool = False


def format_clock(stamp: float) -> str:
    if not stamp:
        return ""
    try:
        moment = datetime.fromtimestamp(stamp)
    except (OverflowError, OSError, ValueError):
        return ""
    today = datetime.now().date()
    if moment.date() == today:
        return moment.strftime("%H:%M")
    return moment.strftime("%b %d, %H:%M")


def format_elapsed(seconds: float | None) -> str:
    if seconds is None:
        return ""
    if seconds < 1:
        return f"{int(seconds * 1000)} ms"
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, rest = divmod(int(seconds), 60)
    return f"{minutes}m {rest:02d}s"


def chat_group_label(created_at: str, now: datetime | None = None) -> str:
    moment = _iso_to_epoch(created_at)
    if not moment:
        return "Earlier"
    current = now or datetime.now()
    day = datetime.fromtimestamp(moment).date()
    delta = (current.date() - day).days
    if delta <= 0:
        return "Today"
    if delta == 1:
        return "Yesterday"
    if delta < 7:
        return "Previous 7 days"
    if delta < 30:
        return "Previous 30 days"
    return "Earlier"


class MessageCard(tk.Frame):
    """One conversation turn rendered as a card with header, body and actions."""

    def __init__(self, master: tk.Misc, app: "JarvisDesktop", message: Message) -> None:
        theme = app.theme
        super().__init__(master, bg=theme.bg)
        self.app = app
        self.message = message
        self.theme = theme
        self.column = tk.Frame(self, bg=theme.bg)
        self.column.pack(fill="x", padx=(app.px(28), app.px(28)))
        self.column.grid_columnconfigure(0, weight=1)
        self.body: tk.Frame
        self.stream_text: AutoText | None = None
        self.steps_label: tk.Label | None = None
        self.pulse: tk.Label | None = None
        self._pulse_after: str | None = None
        self._pulse_step = 0
        self.header: tk.Frame | None = None
        self.footer: tk.Frame | None = None
        if message.role == "user":
            self._build_user()
        else:
            self._build_assistant()

    # -- user turn --------------------------------------------------------

    def _build_user(self) -> None:
        theme = self.theme
        app = self.app
        row = tk.Frame(self.column, bg=theme.bg)
        row.pack(fill="x", pady=(app.px(6), app.px(2)))
        bubble = tk.Frame(
            row, bg=theme.user_bubble, highlightbackground=theme.border,
            highlightthickness=1, padx=app.px(14), pady=app.px(10),
        )
        bubble.pack(side="right", anchor="e")
        max_width = max(app.px(320), int(app.content_width() * 0.72))
        font = app.fonts.body
        average = max(4.0, font.measure("abcdefghijklmnopqrstuvwxyz ABCDEFGHIJ") / 37.0)
        longest = max((len(line) for line in self.message.content.splitlines()), default=1)
        columns = max(12, min(int(max_width / average), longest + 1))
        text = AutoText(bubble, app, font=font, bg=theme.user_bubble, fg=theme.text, width=columns)
        text.pack(fill="x")
        text.set_text(self.message.content)
        self.body = bubble
        if self.message.attachments:
            chips = tk.Frame(bubble, bg=theme.user_bubble)
            chips.pack(fill="x", pady=(app.px(6), 0))
            for name in self.message.attachments[:MAX_IMAGE_ATTACHMENTS]:
                tk.Label(
                    chips, text=f"🖼 {name}", bg=theme.surface_alt, fg=theme.muted,
                    font=app.fonts.tiny, padx=7, pady=2,
                ).pack(side="left", padx=(0, 5))
        footer = tk.Frame(self.column, bg=theme.bg)
        footer.pack(fill="x")
        self.footer = footer
        actions = tk.Frame(footer, bg=theme.bg)
        actions.pack(side="right")
        stamp = tk.Label(actions, text=format_clock(self.message.created_at), bg=theme.bg, fg=theme.faint, font=app.fonts.tiny)
        stamp.pack(side="right", padx=(6, 2))
        self._action(actions, "Copy", lambda: app.copy_text(self.message.content))
        self._action(actions, "Edit", lambda: app.edit_prompt(self.message.content))

    # -- assistant turn ---------------------------------------------------

    def _build_assistant(self) -> None:
        theme = self.theme
        app = self.app
        header = tk.Frame(self.column, bg=theme.bg)
        header.pack(fill="x", pady=(app.px(10), app.px(4)))
        self.header = header
        avatar = tk.Label(
            header, text="J", bg=theme.accent, fg=theme.accent_ink, font=app.fonts.avatar,
            width=2, height=1, padx=0, pady=1,
        )
        avatar.pack(side="left", padx=(0, 9))
        name = tk.Label(header, text="Jarvis", bg=theme.bg, fg=theme.text, font=app.fonts.small_bold)
        name.pack(side="left")
        self.meta_label = tk.Label(header, text="", bg=theme.bg, fg=theme.faint, font=app.fonts.tiny)
        self.meta_label.pack(side="left", padx=(8, 0))
        self.pulse = tk.Label(header, text="", bg=theme.bg, fg=theme.accent, font=app.fonts.small_bold)
        self.pulse.pack(side="left", padx=(8, 0))
        self.body = tk.Frame(self.column, bg=theme.bg)
        self.body.pack(fill="x", padx=(app.px(38), 0))
        self.steps_frame = tk.Frame(self.body, bg=theme.bg)
        self.footer = tk.Frame(self.column, bg=theme.bg)
        self.footer.pack(fill="x", padx=(app.px(38), 0), pady=(app.px(2), app.px(4)))
        if self.message.working:
            self.show_working()
        else:
            self.render_final()

    def _action(self, parent: tk.Misc, label: str, command: Callable[[], None]) -> tk.Label:
        theme = self.theme
        button = tk.Label(
            parent, text=label, bg=theme.bg, fg=theme.faint, font=self.app.fonts.tiny,
            cursor="hand2", padx=6, pady=2,
        )
        button.pack(side="right")
        button.bind("<Enter>", lambda _e: button.configure(fg=theme.text, bg=theme.surface_hover))
        button.bind("<Leave>", lambda _e: button.configure(fg=theme.faint, bg=theme.bg))
        button.bind("<Button-1>", lambda _e: command())
        return button

    def _clear_body(self) -> None:
        for child in self.body.winfo_children():
            if child is not self.steps_frame:
                child.destroy()
        for child in self.footer.winfo_children():
            child.destroy()
        self.stream_text = None

    # working / streaming ---------------------------------------------------

    def show_working(self) -> None:
        self.message.working = True
        self._clear_body()
        self.steps_frame.pack(fill="x", pady=(0, self.app.px(4)))
        self.refresh_steps()
        self._start_pulse()

    def refresh_steps(self) -> None:
        theme = self.theme
        for child in self.steps_frame.winfo_children():
            child.destroy()
        visible = self.message.steps[-6:]
        if not visible:
            visible = ["Starting request"]
        for index, step in enumerate(visible):
            last = index == len(visible) - 1
            tk.Label(
                self.steps_frame, text=("›  " if last else "·  ") + step,
                bg=theme.bg, fg=theme.text if last else theme.faint,
                font=self.app.fonts.small, anchor="w", justify="left",
            ).pack(fill="x")

    def _start_pulse(self) -> None:
        self._pulse_step = 0
        self._tick_pulse()

    def _tick_pulse(self) -> None:
        if not self.message.working or self.pulse is None:
            return
        frames = ("●  ", " ● ", "  ●", " ● ")
        try:
            self.pulse.configure(text=frames[self._pulse_step % len(frames)] + " Working")
        except tk.TclError:
            return
        self._pulse_step += 1
        self._pulse_after = self.after(260, self._tick_pulse)

    def _stop_pulse(self) -> None:
        if self._pulse_after is not None:
            try:
                self.after_cancel(self._pulse_after)
            except tk.TclError:
                pass
            self._pulse_after = None
        if self.pulse is not None:
            try:
                self.pulse.configure(text="")
            except tk.TclError:
                pass

    def append_delta(self, text: str) -> None:
        theme = self.theme
        if self.stream_text is None:
            self.stream_text = AutoText(self.body, self.app, font=self.app.fonts.body, bg=theme.bg, fg=theme.text)
            self.stream_text.pack(fill="x", pady=(0, 4))
            self.message.streaming = True
        self.stream_text.append_text(text)

    # final rendering -----------------------------------------------------

    def render_final(self) -> None:
        theme = self.theme
        app = self.app
        message = self.message
        message.working = False
        message.streaming = False
        self._stop_pulse()
        self._clear_body()
        if message.steps:
            self.steps_frame.pack(fill="x", pady=(0, app.px(4)))
            for child in self.steps_frame.winfo_children():
                child.destroy()
            summary = f"Worked for {format_elapsed(message.elapsed)} · {len(message.steps)} steps"
            if message.tool_calls:
                summary += f" · {message.tool_calls} tool calls"
            tk.Label(
                self.steps_frame, text=summary, bg=theme.bg, fg=theme.faint,
                font=app.fonts.tiny, anchor="w",
            ).pack(fill="x")
        else:
            self.steps_frame.pack_forget()
        if message.error:
            self._render_notice(message.content, kind="danger")
        else:
            self._render_markdown(message.content)
        if message.approval_id is not None:
            self._render_approval(message.approval_id)
        if message.status not in {"complete", "cancelled"} and not message.error:
            reason = message.status.replace("_", " ")
            self._render_notice(f"Status: {reason}", kind="warning")
        meta_bits = [bit for bit in (message.model, format_elapsed(message.elapsed)) if bit]
        if self.meta_label is not None:
            self.meta_label.configure(text="  ·  ".join([format_clock(message.created_at), *meta_bits]))
        self._action(self.footer, "Copy", lambda: app.copy_text(message.content))
        if message.status != "cancelled":
            self._action(self.footer, "Regenerate", app.regenerate_last)
        self._action(self.footer, "Quote", lambda: app.quote_text(message.content))

    def _render_notice(self, text: str, *, kind: str) -> None:
        theme = self.theme
        color = theme.danger if kind == "danger" else theme.warning
        frame = tk.Frame(self.body, bg=theme.surface, highlightbackground=color, highlightthickness=1, padx=12, pady=8)
        frame.pack(fill="x", pady=(0, 6))
        text_widget = AutoText(frame, self.app, font=self.app.fonts.small, bg=theme.surface, fg=theme.text)
        text_widget.pack(fill="x")
        text_widget.set_text(text)

    def _render_approval(self, approval_id: int) -> None:
        theme = self.theme
        app = self.app
        frame = tk.Frame(self.body, bg=theme.accent_soft, highlightbackground=theme.accent, highlightthickness=1, padx=12, pady=9)
        frame.pack(fill="x", pady=(4, 6))
        tk.Label(
            frame, text=f"Approval #{approval_id} is waiting for your review",
            bg=theme.accent_soft, fg=theme.text_strong, font=app.fonts.small_bold, anchor="w",
        ).pack(side="left")
        RoundButton(frame, app, "Review approvals", app.show_approvals, kind="accent", padx=12, pady=5, font=app.fonts.small_bold).pack(side="right")

    def _render_markdown(self, content: str) -> None:
        blocks = parse_markdown(content)
        if not blocks:
            blocks = [{"type": "paragraph", "text": content}]
        for block in blocks:
            kind = block["type"]
            if kind == "code":
                self._render_code(block.get("lang", ""), block.get("text", ""))
            elif kind == "heading":
                self._render_heading(int(block.get("level", 1)), block.get("text", ""))
            elif kind == "hr":
                tk.Frame(self.body, bg=self.theme.border_strong, height=1).pack(fill="x", pady=8)
            elif kind == "quote":
                self._render_quote(block.get("text", ""))
            elif kind == "list":
                self._render_list(bool(block.get("ordered")), block.get("items", []))
            elif kind == "table":
                self._render_code("table", render_table_text(block.get("rows", [])), copyable=False)
            else:
                self._render_paragraph(block.get("text", ""))

    def _make_rich_text(self, parent: tk.Misc, bg: str) -> AutoText:
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
        """Insert inline runs; bold/italic runs are re-parsed once so nested
        code spans and links inside them still render."""
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
        widget = self._make_rich_text(self.body, self.theme.bg)
        widget.pack(fill="x", pady=(0, self.app.px(8)))
        self._insert_inline(widget, text)

    def _render_heading(self, level: int, text: str) -> None:
        fonts = self.app.fonts
        font = fonts.h1 if level == 1 else fonts.h2 if level == 2 else fonts.h3
        widget = self._make_rich_text(self.body, self.theme.bg)
        widget.configure(font=font, fg=self.theme.text_strong)
        widget.pack(fill="x", pady=(self.app.px(8), self.app.px(4)))
        self._insert_inline(widget, text)

    def _render_quote(self, text: str) -> None:
        theme = self.theme
        wrapper = tk.Frame(self.body, bg=theme.bg)
        wrapper.pack(fill="x", pady=(0, self.app.px(8)))
        tk.Frame(wrapper, bg=theme.accent, width=3).pack(side="left", fill="y")
        inner = tk.Frame(wrapper, bg=theme.surface_alt, padx=12, pady=8)
        inner.pack(side="left", fill="x", expand=True)
        widget = self._make_rich_text(inner, theme.surface_alt)
        widget.configure(fg=theme.muted)
        widget.pack(fill="x")
        self._insert_inline(widget, text)

    def _render_list(self, ordered: bool, items: list[dict[str, Any]]) -> None:
        widget = self._make_rich_text(self.body, self.theme.bg)
        widget.pack(fill="x", pady=(0, self.app.px(8)))
        counters: dict[int, int] = {}
        widget.configure(state="normal")
        for index, item in enumerate(items):
            indent = int(item.get("indent", 0))
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
            pad = self.app.px(18) * indent
            tag = f"li-{indent}"
            widget.tag_configure(tag, lmargin1=pad, lmargin2=pad + self.app.px(20), spacing1=2)
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
        frame = tk.Frame(self.body, bg=theme.code_bg, highlightbackground=theme.border_strong, highlightthickness=1)
        frame.pack(fill="x", pady=(2, app.px(10)))
        head = tk.Frame(frame, bg=theme.code_head, padx=10, pady=4)
        head.pack(fill="x")
        tk.Label(head, text=(language or "code").upper(), bg=theme.code_head, fg=theme.faint, font=app.fonts.tiny).pack(side="left")
        if copyable:
            copy = tk.Label(head, text="Copy", bg=theme.code_head, fg=theme.muted, font=app.fonts.tiny, cursor="hand2", padx=6)
            copy.pack(side="right")

            def do_copy() -> None:
                app.copy_text(code, quiet=True)
                copy.configure(text="Copied ✓", fg=theme.success)
                copy.after(1400, lambda: copy.configure(text="Copy", fg=theme.muted))

            copy.bind("<Button-1>", lambda _e: do_copy())
            copy.bind("<Enter>", lambda _e: copy.configure(fg=theme.text))
            copy.bind("<Leave>", lambda _e: copy.configure(fg=theme.muted))
        body = AutoText(frame, app, font=app.fonts.mono, bg=theme.code_bg, fg=theme.text, wrap="char")
        body.configure(padx=12, pady=10, spacing1=0, spacing3=0)
        body.pack(fill="x")
        body.set_text(code)


class EmptyState(tk.Frame):
    """Claude-style greeting with suggestion cards for a fresh chat."""

    SUGGESTIONS = (
        ("✎", "Draft something", "Write a clear, friendly email declining a meeting invitation."),
        ("‹/›", "Build with code", "Create a Python script that renames files in a folder by date."),
        ("⌕", "Research a topic", "Compare three approaches to backing up a home NAS, with tradeoffs."),
        ("◷", "Plan my week", "Help me plan a focused week: I have two deadlines and one trip."),
    )

    def __init__(self, master: tk.Misc, app: "JarvisDesktop") -> None:
        theme = app.theme
        super().__init__(master, bg=theme.bg)
        hour = datetime.now().hour
        greeting = "Good morning" if hour < 12 else "Good afternoon" if hour < 18 else "Good evening"
        tk.Label(self, text=f"{greeting}.", bg=theme.bg, fg=theme.text_strong, font=app.fonts.hero).pack(pady=(app.px(70), 2))
        tk.Label(self, text="What are we working on?", bg=theme.bg, fg=theme.muted, font=app.fonts.title).pack(pady=(0, app.px(24)))
        grid = tk.Frame(self, bg=theme.bg)
        grid.pack()
        for index, (glyph, title, prompt) in enumerate(self.SUGGESTIONS):
            card = tk.Frame(grid, bg=theme.surface, highlightbackground=theme.border, highlightthickness=1, padx=14, pady=12, cursor="hand2", width=app.px(250))
            card.grid(row=index // 2, column=index % 2, padx=6, pady=6, sticky="nsew")
            card.grid_propagate(False)
            card.configure(height=app.px(96))
            glyph_label = tk.Label(card, text=glyph, bg=theme.surface, fg=theme.accent, font=app.fonts.icon)
            glyph_label.pack(anchor="w")
            title_label = tk.Label(card, text=title, bg=theme.surface, fg=theme.text_strong, font=app.fonts.label_bold)
            title_label.pack(anchor="w", pady=(4, 0))
            hint = tk.Label(card, text=prompt, bg=theme.surface, fg=theme.muted, font=app.fonts.small, wraplength=app.px(220), justify="left")
            hint.pack(anchor="w")
            widgets = (card, glyph_label, title_label, hint)

            def enter(_e: Any, items: tuple[tk.Widget, ...] = widgets) -> None:
                for item in items:
                    item.configure(bg=theme.surface_hover)
                items[0].configure(highlightbackground=theme.border_strong)

            def leave(_e: Any, items: tuple[tk.Widget, ...] = widgets) -> None:
                for item in items:
                    item.configure(bg=theme.surface)
                items[0].configure(highlightbackground=theme.border)

            for item in widgets:
                item.bind("<Enter>", enter)
                item.bind("<Leave>", leave)
                item.bind("<Button-1>", lambda _e, text=prompt: app.use_suggestion(text))
        tk.Label(
            self, text="Enter to send  ·  Shift+Enter for a new line  ·  Ctrl+K for the command palette",
            bg=theme.bg, fg=theme.faint, font=app.fonts.tiny,
        ).pack(pady=(app.px(26), 0))


# --------------------------------------------------------------------------
# Composer, palette, dialogs
# --------------------------------------------------------------------------

class Composer(tk.Frame):
    def __init__(self, master: tk.Misc, app: "JarvisDesktop") -> None:
        theme = app.theme
        super().__init__(master, bg=theme.bg)
        self.app = app
        self.theme = theme
        self.attachments: list[str] = []
        self.card = tk.Frame(self, bg=theme.surface, highlightbackground=theme.border_strong, highlightthickness=1, padx=app.px(12), pady=app.px(8))
        self.card.pack(fill="x")
        self.chips = tk.Frame(self.card, bg=theme.surface)
        self.input = GrowText(self.card, app)
        self.input.pack(fill="x")
        self.input.on_change = self._on_change
        self.input.bind("<Return>", self._enter_key)
        self.input.bind("<Shift-Return>", self._newline_key)
        self.input.bind("<Control-Return>", self._newline_key)
        self.input.bind("<FocusIn>", lambda _e: self.card.configure(highlightbackground=theme.accent))
        self.input.bind("<FocusOut>", lambda _e: self.card.configure(highlightbackground=theme.border_strong))
        row = tk.Frame(self.card, bg=theme.surface)
        row.pack(fill="x", pady=(app.px(4), 0))
        self.attach_button = IconButton(row, app, "＋", self.pick_attachments, tooltip="Attach images (PNG, JPEG, WebP, GIF)")
        self.attach_button.pack(side="left")
        self.model_label = tk.Label(row, text="", bg=theme.surface, fg=theme.faint, font=app.fonts.tiny, cursor="hand2")
        self.model_label.pack(side="left", padx=(8, 0))
        self.model_label.bind("<Button-1>", lambda _e: app.cycle_model())
        Tooltip(self.model_label, "Click to switch model profile (Ctrl+1…5)", app)
        self.counter = tk.Label(row, text="", bg=theme.surface, fg=theme.faint, font=app.fonts.tiny)
        self.counter.pack(side="left", padx=(10, 0))
        self.send_button = RoundButton(row, app, "↑", app.send, kind="accent", padx=11, pady=4, radius=10, font=app.fonts.icon, tooltip="Send (Enter)")
        self.send_button.pack(side="right")
        self.stop_button = RoundButton(row, app, "■", app.stop_request, kind="danger", padx=11, pady=4, radius=10, font=app.fonts.icon_small, tooltip="Stop (Esc)")
        self.hint = tk.Label(self, text="Jarvis can make mistakes. Sensitive actions still require your approval.", bg=theme.bg, fg=theme.faint, font=app.fonts.tiny)
        self.hint.pack(pady=(5, 0))
        self.refresh_model_label()

    def refresh_model_label(self) -> None:
        label = self.app.model_label
        self.model_label.configure(text=f"{label} · {MODEL_HINTS.get(label, '')}")

    def _on_change(self) -> None:
        length = len(self.input.value())
        if length > MAX_PROMPT_CHARS * 0.8:
            self.counter.configure(text=f"{length:,} / {MAX_PROMPT_CHARS:,}", fg=self.theme.warning if length <= MAX_PROMPT_CHARS else self.theme.danger)
        else:
            self.counter.configure(text="")

    def _enter_key(self, _event: Any) -> str:
        self.app.send()
        return "break"

    def _newline_key(self, _event: Any) -> str:
        self.input.insert("insert", "\n")
        return "break"

    def set_busy(self, busy: bool) -> None:
        if busy:
            self.send_button.pack_forget()
            self.stop_button.pack(side="right")
        else:
            self.stop_button.pack_forget()
            self.send_button.pack(side="right")
        self.attach_button.configure(state="disabled" if busy else "normal")

    def pick_attachments(self) -> None:
        if len(self.attachments) >= MAX_IMAGE_ATTACHMENTS:
            self.app.toast(f"At most {MAX_IMAGE_ATTACHMENTS} images per message.", kind="warning")
            return
        paths = filedialog.askopenfilenames(
            parent=self.app.root,
            title="Attach images",
            filetypes=[("Images", "*.png *.jpg *.jpeg *.webp *.gif"), ("All files", "*.*")],
        )
        for path in paths:
            if len(self.attachments) >= MAX_IMAGE_ATTACHMENTS:
                break
            if path not in self.attachments:
                self.attachments.append(path)
        self.render_chips()

    def render_chips(self) -> None:
        theme = self.theme
        for child in self.chips.winfo_children():
            child.destroy()
        if not self.attachments:
            self.chips.pack_forget()
            return
        self.chips.pack(fill="x", before=self.input, pady=(0, 6))
        for path in self.attachments:
            chip = tk.Frame(self.chips, bg=theme.surface_alt, highlightbackground=theme.border, highlightthickness=1, padx=8, pady=3)
            chip.pack(side="left", padx=(0, 6))
            tk.Label(chip, text=f"🖼 {Path(path).name}", bg=theme.surface_alt, fg=theme.text, font=app_font(self.app, "tiny")).pack(side="left")
            remove = tk.Label(chip, text="×", bg=theme.surface_alt, fg=theme.muted, font=app_font(self.app, "small"), cursor="hand2", padx=4)
            remove.pack(side="left")
            remove.bind("<Button-1>", lambda _e, target=path: self.remove_attachment(target))

    def remove_attachment(self, path: str) -> None:
        self.attachments = [item for item in self.attachments if item != path]
        self.render_chips()

    def take(self) -> tuple[str, list[str]]:
        text = self.input.value().strip()
        attachments = list(self.attachments)
        self.input.set_value("")
        self.attachments = []
        self.render_chips()
        self.counter.configure(text="")
        return text, attachments


def app_font(app: "JarvisDesktop", name: str) -> tkfont.Font:
    return getattr(app.fonts, name)


class CommandPalette(tk.Toplevel):
    """Ctrl+K — search chats and run actions from the keyboard."""

    def __init__(self, app: "JarvisDesktop", items: list[dict[str, Any]]) -> None:
        super().__init__(app.root)
        theme = app.theme
        self.app = app
        self.items = items
        self.filtered: list[dict[str, Any]] = []
        self.selected = 0
        self.rows: list[tk.Frame] = []
        self.overrideredirect(True)
        self.configure(bg=theme.border_strong)
        width = app.px(600)
        root = app.root
        x = root.winfo_rootx() + (root.winfo_width() - width) // 2
        y = root.winfo_rooty() + app.px(90)
        self.geometry(f"{width}x{app.px(420)}+{x}+{y}")
        shell = tk.Frame(self, bg=theme.panel)
        shell.pack(fill="both", expand=True, padx=1, pady=1)
        entry_row = tk.Frame(shell, bg=theme.panel)
        entry_row.pack(fill="x", padx=14, pady=(12, 8))
        tk.Label(entry_row, text="⌕", bg=theme.panel, fg=theme.accent, font=app.fonts.icon).pack(side="left", padx=(0, 8))
        self.entry = tk.Entry(entry_row, bg=theme.panel, fg=theme.text_strong, insertbackground=theme.accent, bd=0, highlightthickness=0, font=app.fonts.title)
        self.entry.pack(side="left", fill="x", expand=True)
        tk.Frame(shell, bg=theme.border, height=1).pack(fill="x")
        self.list = ScrollFrame(shell, app, bg=theme.panel)
        self.list.pack(fill="both", expand=True, padx=6, pady=6)
        foot = tk.Label(shell, text="↑↓ navigate   ·   Enter open   ·   Esc close", bg=theme.panel, fg=theme.faint, font=app.fonts.tiny)
        foot.pack(pady=(0, 8))
        self.entry.bind("<KeyRelease>", self._on_key)
        self.entry.bind("<Down>", lambda _e: self._move(1))
        self.entry.bind("<Up>", lambda _e: self._move(-1))
        self.entry.bind("<Return>", lambda _e: self._activate())
        self.entry.bind("<Escape>", lambda _e: self.close())
        self.bind("<FocusOut>", self._on_focus_out)
        self.refresh("")
        self.after(10, self.entry.focus_force)

    def _on_focus_out(self, _event: Any) -> None:
        self.after(120, self._close_if_unfocused)

    def _close_if_unfocused(self) -> None:
        try:
            focused = self.focus_get()
        except (KeyError, tk.TclError):
            focused = None
        if focused is None or str(focused).startswith(str(self)) is False:
            self.close()

    def close(self) -> None:
        if self.app.palette is self:
            self.app.palette = None
        try:
            self.destroy()
        except tk.TclError:
            pass

    def _on_key(self, event: Any) -> None:
        if event.keysym in {"Up", "Down", "Return", "Escape"}:
            return
        self.refresh(self.entry.get())

    @staticmethod
    def score(item: dict[str, Any], query: str) -> int:
        haystack = f"{item.get('label', '')} {item.get('detail', '')} {item.get('keywords', '')}".lower()
        if not query:
            return 1
        words = query.lower().split()
        total = 0
        for word in words:
            position = haystack.find(word)
            if position < 0:
                return 0
            total += 100 - min(90, position)
            if item.get("label", "").lower().startswith(word):
                total += 40
        return total

    def refresh(self, query: str) -> None:
        query = query.strip()
        scored = [(self.score(item, query), index, item) for index, item in enumerate(self.items)]
        ranked = [entry for entry in scored if entry[0] > 0]
        if query:
            ranked.sort(key=lambda entry: (-entry[0], entry[1]))
        self.filtered = [item for _score, _index, item in ranked][:40]
        self.selected = 0
        self._render()

    def _render(self) -> None:
        theme = self.theme = self.app.theme
        for child in self.list.inner.winfo_children():
            child.destroy()
        self.rows = []
        if not self.filtered:
            tk.Label(self.list.inner, text="Nothing matches. Try a chat title or an action.", bg=theme.panel, fg=theme.faint, font=self.app.fonts.small).pack(pady=18)
            return
        last_group = None
        for index, item in enumerate(self.filtered):
            group = item.get("group", "")
            if group != last_group:
                tk.Label(self.list.inner, text=group.upper(), bg=theme.panel, fg=theme.faint, font=self.app.fonts.tiny, anchor="w").pack(fill="x", padx=10, pady=(8, 2))
                last_group = group
            row = tk.Frame(self.list.inner, bg=theme.panel, padx=10, pady=6, cursor="hand2")
            row.pack(fill="x")
            glyph = tk.Label(row, text=item.get("icon", "·"), bg=theme.panel, fg=theme.accent, font=self.app.fonts.icon_small, width=2)
            glyph.pack(side="left")
            label = tk.Label(row, text=item.get("label", ""), bg=theme.panel, fg=theme.text, font=self.app.fonts.label, anchor="w")
            label.pack(side="left", fill="x", expand=True)
            detail = tk.Label(row, text=item.get("detail", ""), bg=theme.panel, fg=theme.faint, font=self.app.fonts.tiny)
            detail.pack(side="right")
            for widget in (row, glyph, label, detail):
                widget.bind("<Button-1>", lambda _e, target=index: self._activate(target))
                widget.bind("<Enter>", lambda _e, target=index: self._select(target))
            self.rows.append(row)
        self._paint_selection()

    def _paint_selection(self) -> None:
        theme = self.app.theme
        for index, row in enumerate(self.rows):
            color = theme.surface_hover if index == self.selected else theme.panel
            row.configure(bg=color)
            for child in row.winfo_children():
                child.configure(bg=color)

    def _select(self, index: int) -> None:
        self.selected = index
        self._paint_selection()

    def _move(self, delta: int) -> str:
        if self.rows:
            self.selected = (self.selected + delta) % len(self.rows)
            self._paint_selection()
        return "break"

    def _activate(self, index: int | None = None) -> str:
        target = self.selected if index is None else index
        if 0 <= target < len(self.filtered):
            item = self.filtered[target]
            self.close()
            item["run"]()
        return "break"


class ApprovalWindow(tk.Toplevel):
    def __init__(self, app: "JarvisDesktop", approvals: list[dict[str, Any]]) -> None:
        super().__init__(app.root)
        self.app = app
        theme = app.theme
        self.title("Jarvis approvals")
        self.geometry(f"{app.px(900)}x{app.px(600)}")
        self.minsize(app.px(700), app.px(420))
        self.configure(bg=theme.bg)
        self.transient(app.root)
        _apply_titlebar_theme(self, theme.dark)
        header = tk.Frame(self, bg=theme.bg, padx=app.px(22), pady=app.px(16))
        header.pack(fill="x")
        tk.Label(header, text="Action approvals", bg=theme.bg, fg=theme.text_strong, font=app.fonts.h1).pack(anchor="w")
        tk.Label(header, text="Review the exact target before authorizing it. Approvals are one-shot and scope-bound.", bg=theme.bg, fg=theme.muted, font=app.fonts.small).pack(anchor="w", pady=(3, 0))
        self.list = ScrollFrame(self, app, bg=theme.bg)
        self.list.pack(fill="both", expand=True, padx=app.px(22), pady=(0, app.px(18)))
        self.update_rows(approvals)

    def update_rows(self, approvals: list[dict[str, Any]]) -> None:
        theme = self.app.theme
        app = self.app
        for child in self.list.inner.winfo_children():
            child.destroy()
        pending = [row for row in approvals if row.get("status") == "pending"]
        decided = [row for row in approvals if row.get("status") != "pending"][:20]
        if not pending:
            tk.Label(self.list.inner, text="No sensitive actions are waiting for approval.", bg=theme.bg, fg=theme.muted, font=app.fonts.label, pady=24).pack(fill="x")
        for row in pending:
            self._card(row, pending=True)
        if decided:
            tk.Label(self.list.inner, text="RECENT DECISIONS", bg=theme.bg, fg=theme.faint, font=app.fonts.tiny, anchor="w").pack(fill="x", pady=(14, 4))
            for row in decided:
                self._card(row, pending=False)

    def _card(self, row: dict[str, Any], *, pending: bool) -> None:
        theme = self.app.theme
        app = self.app
        card = tk.Frame(self.list.inner, bg=theme.surface, highlightbackground=theme.accent if pending else theme.border, highlightthickness=1, padx=app.px(16), pady=app.px(12))
        card.pack(fill="x", pady=(0, 8))
        head = tk.Frame(card, bg=theme.surface)
        head.pack(fill="x")
        tk.Label(head, text=f"#{row.get('id')}  ·  {compact_activity(row.get('action', ''), 60)}", bg=theme.surface, fg=theme.text_strong, font=app.fonts.label_bold).pack(side="left")
        status = str(row.get("status", ""))
        tk.Label(head, text=status.upper(), bg=theme.accent_soft if pending else theme.surface_alt, fg=theme.accent if pending else theme.muted, font=app.fonts.tiny, padx=7, pady=2).pack(side="right")
        tk.Label(card, text=compact_activity(row.get("reason", ""), 400), bg=theme.surface, fg=theme.muted, font=app.fonts.small, wraplength=app.px(780), justify="left").pack(anchor="w", pady=(6, 4))
        scope = compact_activity(row.get("scope", ""), 120)
        if scope:
            tk.Label(card, text=f"Scope: {scope}", bg=theme.surface, fg=theme.faint, font=app.fonts.tiny).pack(anchor="w")
        resource = AutoText(card, app, font=app.fonts.mono_small, bg=theme.code_bg, fg=theme.text, wrap="char")
        resource.configure(padx=10, pady=8)
        resource.pack(fill="x", pady=(8, 0))
        resource.set_text(safe_ui_text(row.get("resource", ""), 12_000) or "(no resource details)")
        if pending:
            actions = tk.Frame(card, bg=theme.surface)
            actions.pack(fill="x", pady=(10, 0))
            approval_id = int(row["id"])
            RoundButton(actions, app, "Deny", lambda: self._decide(approval_id, False), kind="danger", padx=14, pady=6).pack(side="right")
            RoundButton(actions, app, "Approve once", lambda: self._decide(approval_id, True), kind="accent", padx=14, pady=6).pack(side="right", padx=(0, 8))

    def _decide(self, approval_id: int, approve: bool) -> None:
        verb = "approve" if approve else "deny"
        if not messagebox.askyesno(f"{verb.title()} approval #{approval_id}", f"Do you want to {verb} this exact sensitive action?", parent=self):
            return
        self.app.session.decide_approval(approval_id, approve)


SHORTCUTS: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (
    ("Conversation", (
        ("Enter", "Send message"),
        ("Shift + Enter", "New line"),
        ("Esc", "Stop the current request / close palette"),
        ("Ctrl + N", "New chat"),
        ("Ctrl + L", "Focus the composer"),
        ("Ctrl + Shift + C", "Copy the last reply"),
        ("Ctrl + R", "Regenerate the last reply"),
    )),
    ("Navigate", (
        ("Ctrl + K", "Command palette"),
        ("Ctrl + M", "Switch between Chat and Council"),
        ("Ctrl + B", "Toggle the sidebar"),
        ("Ctrl + Shift + A", "Review approvals"),
        ("Ctrl + E", "Export this chat as Markdown"),
        ("Ctrl + /", "This shortcut list"),
    )),
    ("Models & look", (
        ("Ctrl + 1 … 5", "Auto · Fast · Reasoning · Coding · Deep"),
        ("Ctrl + T", "Cycle theme (Midnight → Graphite → Paper)"),
        ("Ctrl + +/-", "Zoom text"),
    )),
)


class ShortcutsWindow(tk.Toplevel):
    def __init__(self, app: "JarvisDesktop") -> None:
        super().__init__(app.root)
        theme = app.theme
        self.title("Keyboard shortcuts")
        self.configure(bg=theme.bg)
        self.geometry(f"{app.px(520)}x{app.px(520)}")
        self.resizable(False, False)
        self.transient(app.root)
        _apply_titlebar_theme(self, theme.dark)
        body = tk.Frame(self, bg=theme.bg, padx=app.px(24), pady=app.px(18))
        body.pack(fill="both", expand=True)
        tk.Label(body, text="Keyboard shortcuts", bg=theme.bg, fg=theme.text_strong, font=app.fonts.h1).pack(anchor="w")
        for group, rows in SHORTCUTS:
            tk.Label(body, text=group.upper(), bg=theme.bg, fg=theme.faint, font=app.fonts.tiny).pack(anchor="w", pady=(14, 4))
            for keys, description in rows:
                line = tk.Frame(body, bg=theme.bg)
                line.pack(fill="x", pady=2)
                tk.Label(line, text=description, bg=theme.bg, fg=theme.text, font=app.fonts.small).pack(side="left")
                tk.Label(line, text=keys, bg=theme.surface_alt, fg=theme.muted, font=app.fonts.tiny, padx=7, pady=2).pack(side="right")
        self.bind("<Escape>", lambda _e: self.destroy())


# --------------------------------------------------------------------------
# The application
# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
# Council — colour helpers
# --------------------------------------------------------------------------

def _hex_to_rgb(color: str) -> tuple[int, int, int]:
    value = str(color).strip().lstrip("#")
    if len(value) == 3:
        value = "".join(ch * 2 for ch in value)
    if len(value) != 6:
        return (128, 128, 128)
    try:
        return (int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16))
    except ValueError:
        return (128, 128, 128)


def _rgb_to_hex(rgb: tuple[float, float, float]) -> str:
    return "#%02x%02x%02x" % tuple(max(0, min(255, int(round(part)))) for part in rgb)


def mix_colors(first: str, second: str, amount: float) -> str:
    """Blend ``first`` toward ``second``; ``amount`` 0 keeps first, 1 gives second."""
    ratio = max(0.0, min(1.0, float(amount)))
    a = _hex_to_rgb(first)
    b = _hex_to_rgb(second)
    return _rgb_to_hex(tuple(a[i] + (b[i] - a[i]) * ratio for i in range(3)))


def shade_color(color: str, amount: float) -> str:
    """Lighten (positive) or darken (negative) one colour."""
    return mix_colors(color, "#ffffff" if amount >= 0 else "#000000", abs(float(amount)))


# --------------------------------------------------------------------------
# Council — worker thread
# --------------------------------------------------------------------------

class CouncilSession(threading.Thread):
    """Run the council off the UI thread.

    This thread owns the model client and the meeting; Tk only ever receives
    bounded, redacted rows through :attr:`events`. It deliberately never opens
    SQLite — the meeting's only durable output is the document set under
    ``<data dir>/council``, so it cannot contend with the chat session's
    connection.

    It also keeps the night watch: when the operator has allowed it, the
    council convenes itself inside the night window once the desktop has been
    idle long enough, on a topic the chair picks, and folds each sitting into
    a morning digest. Any operator activity other than speaking to the council
    adjourns an unattended sitting so the room is quiet when they return.
    """

    IDLE_TICK_SECONDS = 2.0

    def __init__(self, config: Config, night: council.NightPlan | None = None) -> None:
        super().__init__(name="jarvis-council", daemon=True)
        self.config = config
        self.commands: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.events: queue.Queue[SessionEvent] = queue.Queue()
        self.cancel_event = threading.Event()
        self.paused = threading.Event()
        self._adjourn = threading.Event()
        self._shutdown = threading.Event()
        self.night_plan = night or council.NightPlan()
        self.last_touch = time.time()

    # -- API used by the UI thread ---------------------------------------

    def emit(self, kind: str, payload: Any = None) -> None:
        self.events.put(SessionEvent(kind, payload))

    def convene(self, topic: str, depth: str) -> None:
        self.commands.put(("convene", (str(topic), str(depth))))

    def interject(self, text: str) -> None:
        self.commands.put(("interject", str(text)))

    def set_night(self, plan: council.NightPlan) -> None:
        self.commands.put(("night", plan))

    def touch(self) -> None:
        """The operator did something; unattended sittings key off this."""
        self.last_touch = time.time()

    def pause(self) -> None:
        self.paused.set()
        self.emit("council_state", {"paused": True})

    def resume(self) -> None:
        self.paused.clear()
        self.emit("council_state", {"paused": False})

    def adjourn(self) -> None:
        self._adjourn.set()
        self.cancel_event.set()
        self.paused.clear()

    def shutdown(self) -> None:
        self._shutdown.set()
        self._adjourn.set()
        self.cancel_event.set()
        self.commands.put(("shutdown", None))

    # -- worker thread ----------------------------------------------------

    def _state(self, meeting: Any, models: Any, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "topic": meeting.topic,
            "status": meeting.status,
            "agenda": list(meeting.agenda),
            "item": meeting.item,
            "progress": meeting.progress(),
            "remaining": council.remaining_turns(meeting),
            "plan": meeting.plan.label,
            "models": models.note,
            "mode": models.mode,
            "decision": meeting.decision,
            "artifacts": dict(meeting.artifacts),
            "paused": self.paused.is_set(),
        }
        if extra:
            payload.update(extra)
        return payload

    def _badges(self, models: Any) -> dict[str, str]:
        badges: dict[str, str] = {}
        for seat in council.COUNCIL_SEATS:
            model, effort = models.for_seat(seat)
            badges[seat.key] = council.model_badge(model, effort)
        return badges

    def _night_state(self, reason: str, sat_tonight: int, night_id: str | None) -> dict[str, Any]:
        return {
            "plan": self.night_plan.as_dict(),
            "reason": reason,
            "sat_tonight": sat_tonight,
            "night": night_id or "",
        }

    def _drain_commands(self, meeting: Any) -> str | None:
        """Apply anything queued without blocking; return a control word."""
        while True:
            try:
                command, payload = self.commands.get_nowait()
            except queue.Empty:
                return None
            if command == "shutdown":
                return "shutdown"
            if command == "interject" and meeting is not None:
                turn = meeting.interject(payload)
                self.emit("council_turn", turn.as_row())
            elif command == "night":
                self.night_plan = payload
                self.emit("council_night", self._night_state("Updated", 0, None))
            elif command == "convene":
                self.commands.put((command, payload))
                return "convene"

    def _recent_titles(self, night_rows: list[dict[str, Any]]) -> list[str]:
        titles = [str(row.get("topic", "")) for row in night_rows]
        for row in council.list_meetings(self.config.data_dir, limit=12):
            title = str(row.get("title", ""))
            title = title.split(" - ", 1)[1] if " - " in title else title
            if title and title not in titles:
                titles.append(title)
        return titles[:16]

    def _file_digest(
        self, night_id: str | None, night_rows: list[dict[str, Any]]
    ) -> None:
        if not night_id or not night_rows:
            return
        try:
            path = council.write_night_digest(
                self.config.data_dir, night_id, night_rows, self.night_plan.focus
            )
        except OSError as exc:
            self.emit("council_error", f"The morning digest could not be written: {safe_ui_text(exc, 200)}")
            return
        digest = council.latest_night_digest(self.config.data_dir)
        if digest is not None and digest.get("path") == str(path):
            self.emit("council_digest", digest)

    def run(self) -> None:
        runtime: Any = None
        meeting: Any = None
        unattended = False
        night_rows: list[dict[str, Any]] = []
        night_id: str | None = None
        sat_tonight = 0
        last_reason = ""
        try:
            models = council.resolve_models(self.config)
            self.emit("council_ready", {
                "seats": [
                    {
                        "key": seat.key,
                        "name": seat.name,
                        "title": seat.title,
                        "mandate": seat.mandate,
                        "chair": seat.chair,
                        "accent": seat.accent,
                    }
                    for seat in council.COUNCIL_SEATS
                ],
                "badges": self._badges(models),
                "models": models.note,
                "mode": models.mode,
                "depths": list(council.DEPTH_ORDER),
                "history": council.list_meetings(self.config.data_dir, limit=12),
                "night": self._night_state("Night sessions are off" if not self.night_plan.enabled else "Armed", 0, None),
                "digest": council.latest_night_digest(self.config.data_dir),
            })
            while not self._shutdown.is_set():
                running = meeting is not None and meeting.status != "closed"
                command, payload = None, None
                if not running:
                    try:
                        command, payload = self.commands.get(
                            timeout=self.IDLE_TICK_SECONDS if self.night_plan.enabled else None
                        )
                    except queue.Empty:
                        pass
                else:
                    control = self._drain_commands(meeting)
                    if control == "shutdown":
                        break
                    if control == "convene":
                        command, payload = self.commands.get()

                if command == "shutdown":
                    break
                if command == "night":
                    self.night_plan = payload
                    last_reason = ""
                    self.emit("council_night", self._night_state(
                        "Armed" if payload.enabled else "Night sessions are off", sat_tonight, night_id,
                    ))
                    continue
                if command == "convene":
                    topic, depth = payload
                    plan = council.DEPTH_PLANS.get(depth, council.DEPTH_PLANS["Standard"])
                    models = council.resolve_models(self.config)
                    if runtime is None:
                        runtime = council.CouncilRuntime(self.config, models=models)
                    else:
                        runtime.models = models
                    self.emit("council_activity", "Checking the model tier")
                    note = runtime.verify_tier()
                    meeting = council.open_meeting(topic, plan)
                    unattended = False
                    self._adjourn.clear()
                    self.cancel_event.clear()
                    self.paused.clear()
                    self.emit("council_opened", {
                        "badges": self._badges(runtime.models),
                        "unattended": False,
                        **self._state(meeting, runtime.models),
                    })
                    if note:
                        turn = meeting.add_turn(council.CHAIR_KEY, council.OPERATOR_KEY, "notice", note)
                        self.emit("council_turn", turn.as_row())
                    continue
                if command == "interject" and meeting is not None:
                    turn = meeting.interject(payload)
                    self.emit("council_turn", turn.as_row())
                    continue

                if command is None and not running:
                    # Idle tick: the night watch.
                    now = datetime.now()
                    plan = self.night_plan
                    key = council.night_key(plan.window, now)
                    if night_id != key:
                        self._file_digest(night_id, night_rows)
                        night_rows = []
                        sat_tonight = 0
                        night_id = key
                    may_sit, reason = council.night_should_sit(
                        plan, now, time.time() - self.last_touch, False, sat_tonight,
                    )
                    if reason != last_reason:
                        last_reason = reason
                        self.emit("council_night", self._night_state(reason, sat_tonight, night_id))
                    if not may_sit:
                        continue
                    models = council.resolve_models(self.config)
                    if runtime is None:
                        runtime = council.CouncilRuntime(self.config, models=models)
                    else:
                        runtime.models = models
                    self.emit("council_activity", "The chair is choosing tonight's topic")
                    note = runtime.verify_tier()
                    try:
                        topic, spark = runtime.pick_topic(
                            plan, self._recent_titles(night_rows), cancelled=self._shutdown.is_set,
                        )
                    except Exception as exc:
                        topic, spark = council.bounded_text(plan.focus, 140), ""
                        note = note or f"The chair could not pick a topic ({safe_ui_text(exc, 160)}); sitting on the standing focus instead."
                    meeting = council.open_meeting(topic, council.DEPTH_PLANS[plan.depth])
                    unattended = True
                    sat_tonight += 1
                    self._adjourn.clear()
                    self.cancel_event.clear()
                    self.paused.clear()
                    self.emit("council_opened", {
                        "badges": self._badges(runtime.models),
                        "unattended": True,
                        "spark": spark,
                        **self._state(meeting, runtime.models),
                    })
                    self.emit("council_night", self._night_state(
                        f"Sitting {sat_tonight} of {plan.cap}", sat_tonight, night_id,
                    ))
                    if note:
                        turn = meeting.add_turn(council.CHAIR_KEY, council.OPERATOR_KEY, "notice", note)
                        self.emit("council_turn", turn.as_row())
                    continue

                if meeting is None or runtime is None:
                    continue

                # The operator came back during an unattended sitting: file it
                # and go quiet. Speaking to the council is not "coming back".
                if unattended and self.last_touch > meeting.started_at and not self._adjourn.is_set():
                    self._adjourn.set()
                    self.cancel_event.set()

                if self._adjourn.is_set():
                    self.emit("council_activity", "Filing the report")
                    artifacts = runtime.finalize(meeting, self.config.data_dir)
                    closed = self._state(meeting, runtime.models, {
                        "artifacts": artifacts,
                        "unattended": unattended,
                        "history": council.list_meetings(self.config.data_dir, limit=12),
                    })
                    if unattended:
                        night_rows.append(council.night_row(meeting))
                        self._file_digest(night_id, night_rows)
                    self.emit("council_closed", closed)
                    meeting = None
                    unattended = False
                    self._adjourn.clear()
                    self.cancel_event.clear()
                    continue

                if self.paused.is_set():
                    time.sleep(0.12)
                    continue

                directive = council.next_directive(meeting)
                self.emit("council_speaking", {
                    "speaker": directive.speaker,
                    "addressee": directive.addressee,
                    "label": directive.label,
                    "action": directive.action,
                })
                directive, turn = runtime.step(meeting, self.cancel_event.is_set)
                if turn is not None:
                    self.emit("council_turn", turn.as_row())
                self.emit("council_state", self._state(meeting, runtime.models))
                if meeting.status == "closed":
                    self.emit("council_activity", "Filing the report")
                    artifacts = runtime.finalize(meeting, self.config.data_dir)
                    closed = self._state(meeting, runtime.models, {
                        "artifacts": artifacts,
                        "unattended": unattended,
                        "history": council.list_meetings(self.config.data_dir, limit=12),
                    })
                    if unattended:
                        night_rows.append(council.night_row(meeting))
                        self._file_digest(night_id, night_rows)
                    self.emit("council_closed", closed)
                    meeting = None
                    unattended = False
        except Exception as exc:
            self.emit(
                "council_error",
                f"The council could not continue ({type(exc).__name__}): "
                f"{safe_ui_text(exc, 300)}",
            )
        finally:
            if runtime is not None:
                runtime.close()


@dataclass
class SeatLayout:
    key: str
    x: float
    y: float
    scale: float
    fade: float
    plate_x: float
    plate_y: float
    depth: float


# Hand-placed so the table reads as a composed room: JARVIS at the head, the
# five specialists on the far arc and both flanks, and the near edge left open
# for the operator's own chair.
SEAT_ANGLES: dict[str, float] = {
    "jarvis": -90.0,
    "coding": -115.0,
    "research": -65.0,
    "cybersecurity": -140.0,
    "network": -40.0,
    "operations": -165.0,
}
CHAIR_PRESENCE = 1.70
OPERATOR_ANGLE = 90.0


class CouncilTable(tk.Canvas):
    """The round table, drawn as a small shaded 3-D room.

    Tk has no gradients, shaders or z-buffer, so depth comes from three things
    done by hand: seats are ordered back-to-front and the table top is painted
    between the far seats and the near chair, every figure is scaled and faded
    by how far back it sits, and each solid is built from a few offset,
    stepped-tone shapes that stand in for a light from the upper left.
    """

    def __init__(self, master: tk.Misc, app: "JarvisDesktop") -> None:
        theme = app.theme
        super().__init__(
            master,
            bd=0,
            highlightthickness=0,
            bg=theme.bg,
            height=app.px(300),
        )
        self.app = app
        self.seats = council.COUNCIL_SEATS
        self.badges: dict[str, str] = {}
        self.speaking: str | None = None
        self.addressee: str | None = None
        self.operator_active = False
        self.phase = 0.0
        self._layout: dict[str, SeatLayout] = {}
        self._dynamic: dict[str, Any] = {}
        self._bob: dict[str, float] = {}
        self._size = (0, 0)
        self._job: str | None = None
        self.bind("<Configure>", self._on_configure)

    # -- lifecycle --------------------------------------------------------

    def start(self) -> None:
        if self._job is None:
            self._job = self.after(70, self._tick)

    def stop(self) -> None:
        if self._job is not None:
            try:
                self.after_cancel(self._job)
            except (tk.TclError, ValueError):
                pass
            self._job = None

    def destroy(self) -> None:
        self.stop()
        super().destroy()

    def set_badges(self, badges: dict[str, str]) -> None:
        self.badges = dict(badges or {})
        self.render()

    def set_speaking(self, speaker: str | None, addressee: str | None = None) -> None:
        self.speaking = speaker
        self.addressee = addressee
        self.operator_active = council.OPERATOR_KEY in {speaker, addressee}
        self._paint_states()

    # -- geometry ---------------------------------------------------------

    def _on_configure(self, event: Any) -> None:
        size = (int(event.width), int(event.height))
        if size == self._size or size[0] < 40 or size[1] < 40:
            return
        self._size = size
        self.render()

    def _compute_layout(self) -> None:
        width, height = self._size
        self.cx = width / 2.0
        self.cy = height * 0.60
        # Size the figures first, then fit the table to them. Deriving the table
        # from the pane instead put a huge ellipse around tiny people on a wide
        # monitor, because the figures stayed capped by the pane's height.
        self.unit = max(8.0, min(width * 0.055, height * 0.098))
        self.rx = max(
            40.0,
            min(self.unit * 5.8, width * 0.38, (width - self.app.px(48)) / 2.0),
        )
        self.ry = self.rx * 0.42
        self._layout = {}
        for seat in self.seats:
            angle = math.radians(SEAT_ANGLES.get(seat.key, -90.0))
            depth = (math.sin(angle) + 1.0) / 2.0          # 0 far, 1 near
            scale = 0.58 + 0.62 * depth
            fade = (1.0 - depth) * 0.42
            if seat.chair:
                scale *= CHAIR_PRESENCE
                fade *= 0.40
            x = self.cx + self.rx * math.cos(angle)
            y = self.cy + self.ry * math.sin(angle)
            plate_angle = angle
            self._layout[seat.key] = SeatLayout(
                key=seat.key,
                x=x,
                y=y - self.unit * 0.10,
                scale=scale,
                fade=fade,
                plate_x=x * 0.90 + self.cx * 0.10,
                plate_y=self.cy + self.ry * 0.86 * math.sin(plate_angle) + self.unit * 0.40,
                depth=depth,
            )

    # -- painting ---------------------------------------------------------

    def render(self) -> None:
        if self._size[0] < 40 or self._size[1] < 40:
            return
        theme = self.app.theme
        self.configure(bg=theme.bg)
        self.delete("all")
        self._dynamic = {}
        self._bob = {}
        self._compute_layout()
        self._draw_room()
        order = sorted(self.seats, key=lambda seat: self._layout[seat.key].depth)
        for seat in order:
            self._draw_person(seat, self._layout[seat.key])
        self._draw_table()
        for seat in order:
            self._draw_plate(seat, self._layout[seat.key])
        self.tag_raise("plate")
        self._draw_operator_chair()
        self._draw_beam_layer()
        self._paint_states()

    def _draw_room(self) -> None:
        theme = self.app.theme
        width, height = self._size
        wall_top = mix_colors(theme.bg, theme.panel, 0.85)
        floor = mix_colors(theme.bg, "#000000" if theme.dark else "#8a8172", 0.32)
        bands = 14
        horizon = self.cy - self.ry * 1.95
        for index in range(bands):
            ratio = index / max(1, bands - 1)
            y1 = horizon * ratio
            y2 = horizon * ((index + 1) / bands) + 1
            self.create_rectangle(
                0, y1, width, y2,
                fill=mix_colors(wall_top, theme.bg, ratio), outline="",
            )
        self.create_rectangle(0, max(0.0, horizon), width, height, fill=floor, outline="")
        self.create_line(
            0, max(0.0, horizon), width, max(0.0, horizon),
            fill=mix_colors(floor, theme.border_strong, 0.22),
        )
        # A soft pool of light over the table, faked with stacked ellipses.
        glow = mix_colors(floor, theme.accent, 0.16 if theme.dark else 0.10)
        for step in range(6, 0, -1):
            ratio = step / 6.0
            self.create_oval(
                self.cx - self.rx * (1.0 + 0.55 * ratio),
                self.cy - self.ry * (1.0 + 1.5 * ratio),
                self.cx + self.rx * (1.0 + 0.55 * ratio),
                self.cy + self.ry * (1.0 + 1.1 * ratio),
                fill=mix_colors(floor, glow, 1.0 - ratio * 0.82), outline="",
            )

    def _draw_table(self) -> None:
        theme = self.app.theme
        top = mix_colors(theme.surface, theme.bg, 0.35)
        rim = mix_colors(top, theme.border_strong, 0.7)
        thickness = max(3.0, self.unit * 0.30)
        # Table edge: the same ellipse dropped a few pixels, so the top face
        # sits on a visible band of side.
        self.create_oval(
            self.cx - self.rx, self.cy - self.ry + thickness,
            self.cx + self.rx, self.cy + self.ry + thickness,
            fill=shade_color(top, -0.34), outline="",
        )
        self.create_oval(
            self.cx - self.rx, self.cy - self.ry,
            self.cx + self.rx, self.cy + self.ry,
            fill=top, outline=rim,
        )
        # Lit far half and shaded near half of the top face.
        self.create_arc(
            self.cx - self.rx * 0.985, self.cy - self.ry * 0.97,
            self.cx + self.rx * 0.985, self.cy + self.ry * 0.97,
            start=0, extent=180, style="chord",
            fill=shade_color(top, 0.06 if theme.dark else 0.04), outline="",
        )
        self.create_arc(
            self.cx - self.rx * 0.985, self.cy - self.ry * 0.97,
            self.cx + self.rx * 0.985, self.cy + self.ry * 0.97,
            start=180, extent=180, style="chord",
            fill=shade_color(top, -0.08), outline="",
        )
        inlay = mix_colors(top, theme.accent, 0.16)
        self.create_oval(
            self.cx - self.rx * 0.52, self.cy - self.ry * 0.52,
            self.cx + self.rx * 0.52, self.cy + self.ry * 0.52,
            outline=inlay, fill="",
        )
        self._dynamic["emblem"] = [
            self.create_arc(
                self.cx - self.rx * radius, self.cy - self.ry * radius,
                self.cx + self.rx * radius, self.cy + self.ry * radius,
                start=start, extent=extent, style="arc",
                outline=mix_colors(top, theme.accent, alpha), width=1,
            )
            for radius, start, extent, alpha in (
                (0.30, 20, 140, 0.55), (0.30, 200, 140, 0.35),
                (0.19, 110, 150, 0.45), (0.19, 290, 150, 0.28),
            )
        ]
        self.create_text(
            self.cx, self.cy,
            text="JARVIS COUNCIL", fill=mix_colors(top, theme.accent, 0.42),
            font=self.app.fonts.tiny,
        )

    def _draw_operator_chair(self) -> None:
        """The empty chair nearest the camera — the operator's own seat.

        Drawn large and cropped by the bottom of the canvas: it is the one
        thing in the frame the viewer is behind, which is what puts them in
        the room rather than in front of a picture of one.
        """
        theme = self.app.theme
        angle = math.radians(OPERATOR_ANGLE)
        x = self.cx + self.rx * math.cos(angle)
        y = self.cy + self.ry * math.sin(angle) + self.unit * 0.95
        unit = self.unit * 1.55
        # Nearest object in the frame, so it is nearly a silhouette.
        back = mix_colors(theme.bg, theme.surface_alt, 0.30 if theme.dark else 0.55)
        for side in (-1, 1):
            self.create_polygon(
                x + side * unit * 1.62, y + unit * 2.20,
                x + side * unit * 1.50, y + unit * 0.30,
                x + side * unit * 1.16, y + unit * 0.24,
                x + side * unit * 1.24, y + unit * 2.20,
                fill=shade_color(back, -0.22), outline="", smooth=True,
            )
        self.create_polygon(
            rounded_points(
                x - unit * 1.22, y - unit * 0.92,
                x + unit * 1.22, y + unit * 2.20,
                unit * 0.34,
            ),
            fill=back, outline="", smooth=True,
        )
        self.create_polygon(
            rounded_points(
                x - unit * 0.98, y - unit * 0.70,
                x + unit * 0.98, y + unit * 0.72,
                unit * 0.26,
            ),
            fill=shade_color(back, 0.10), outline="", smooth=True,
        )
        self.create_line(
            x - unit * 1.06, y - unit * 0.86, x + unit * 1.06, y - unit * 0.86,
            fill=shade_color(back, 0.30), width=max(1, int(unit * 0.06)),
        )
        self._dynamic["operator_plate"] = self.create_text(
            x, y - unit * 1.16,
            text="OPERATOR  ·  YOUR SEAT",
            fill=theme.faint, font=self.app.fonts.tiny,
        )
        self._dynamic["operator_glow"] = self.create_polygon(
            x - unit * 1.30, y + unit * 2.20,
            x - unit * 1.16, y - unit * 0.86,
            x + unit * 1.16, y - unit * 0.86,
            x + unit * 1.30, y + unit * 2.20,
            fill="", outline="", width=2, smooth=True,
        )

    # -- one person -------------------------------------------------------

    def _draw_person(self, seat: Any, spot: SeatLayout) -> None:
        theme = self.app.theme
        tag = f"seat-{seat.key}"
        self._bob[seat.key] = 0.0
        unit = self.unit * spot.scale
        x = spot.x
        y = spot.y
        back = theme.bg

        suit = mix_colors(seat.suit, back, spot.fade)
        skin = mix_colors(seat.skin, back, spot.fade)
        hair = mix_colors(seat.hair, back, spot.fade)
        accent = mix_colors(seat.accent, back, spot.fade * 0.6)

        # Chair back.
        chair = mix_colors(theme.surface_alt, back, 0.30 + spot.fade)
        self.create_polygon(
            x - unit * 1.16, y + unit * 0.30,
            x - unit * 1.05, y - unit * 1.62,
            x + unit * 1.05, y - unit * 1.62,
            x + unit * 1.16, y + unit * 0.30,
            fill=chair, outline="", smooth=True, tags=(tag,),
        )
        self.create_line(
            x - unit * 1.00, y - unit * 1.58, x + unit * 1.00, y - unit * 1.58,
            fill=shade_color(chair, 0.16), width=max(1, int(unit * 0.07)), tags=(tag,),
        )

        # Torso, then a lit left edge and a shaded right flank over it.
        shoulder_y = y - unit * 1.36
        torso = [
            x - unit * 1.00, shoulder_y + unit * 0.16,
            x - unit * 0.86, shoulder_y - unit * 0.10,
            x - unit * 0.30, shoulder_y - unit * 0.22,
            x + unit * 0.30, shoulder_y - unit * 0.22,
            x + unit * 0.86, shoulder_y - unit * 0.10,
            x + unit * 1.00, shoulder_y + unit * 0.16,
            x + unit * 1.14, y + unit * 0.60,
            x - unit * 1.14, y + unit * 0.60,
        ]
        self.create_polygon(torso, fill=suit, outline="", smooth=True, tags=(tag,))
        self.create_polygon(
            x + unit * 0.18, shoulder_y - unit * 0.16,
            x + unit * 0.86, shoulder_y - unit * 0.08,
            x + unit * 1.00, shoulder_y + unit * 0.16,
            x + unit * 1.14, y + unit * 0.60,
            x + unit * 0.20, y + unit * 0.60,
            fill=shade_color(suit, -0.24), outline="", smooth=True, tags=(tag,),
        )
        self.create_polygon(
            x - unit * 1.00, shoulder_y + unit * 0.14,
            x - unit * 0.84, shoulder_y - unit * 0.08,
            x - unit * 0.62, shoulder_y - unit * 0.04,
            x - unit * 0.80, y + unit * 0.60,
            x - unit * 1.14, y + unit * 0.60,
            fill=shade_color(suit, 0.16), outline="", smooth=True, tags=(tag,),
        )
        # Collar and a band of the seat's own colour.
        self.create_polygon(
            x - unit * 0.30, shoulder_y - unit * 0.20,
            x, shoulder_y + unit * 0.46,
            x + unit * 0.30, shoulder_y - unit * 0.20,
            fill=shade_color(suit, 0.26), outline="", tags=(tag,),
        )
        self.create_line(
            x - unit * 0.26, shoulder_y - unit * 0.16,
            x, shoulder_y + unit * 0.34,
            x + unit * 0.26, shoulder_y - unit * 0.16,
            fill=accent, width=max(1, int(unit * 0.10)), tags=(tag,),
        )

        # Arms reaching toward the table; the table top will cut them off.
        for side in (-1, 1):
            self.create_polygon(
                x + side * unit * 0.94, shoulder_y + unit * 0.02,
                x + side * unit * 1.16, shoulder_y + unit * 0.46,
                x + side * unit * 1.02, y + unit * 0.60,
                x + side * unit * 0.70, y + unit * 0.60,
                fill=shade_color(suit, -0.10 if side > 0 else 0.06),
                outline="", smooth=True, tags=(tag,),
            )

        # Neck.
        neck_y = shoulder_y - unit * 0.08
        self.create_polygon(
            x - unit * 0.24, neck_y + unit * 0.10,
            x - unit * 0.22, neck_y - unit * 0.36,
            x + unit * 0.22, neck_y - unit * 0.36,
            x + unit * 0.24, neck_y + unit * 0.10,
            fill=shade_color(skin, -0.22), outline="", tags=(tag,),
        )

        # Head: four offset tones standing in for a sphere lit from upper-left.
        head_y = neck_y - unit * 0.76
        rw = unit * 0.46
        rh = unit * 0.54
        for ratio, offset in ((0.0, 0.0), (0.30, 0.10), (0.58, 0.19), (0.82, 0.27)):
            shrink = ratio * 0.42
            self.create_oval(
                x - rw * (1 - shrink) - rw * offset * 0.5,
                head_y - rh * (1 - shrink) - rh * offset * 0.5,
                x + rw * (1 - shrink) - rw * offset * 0.5,
                head_y + rh * (1 - shrink) - rh * offset * 0.5,
                fill=mix_colors(shade_color(skin, -0.26), shade_color(skin, 0.18), ratio),
                outline="", tags=(tag,),
            )
        # Jaw and ears keep it a face rather than a ball.
        self.create_oval(
            x - rw * 0.62, head_y + rh * 0.06,
            x + rw * 0.62, head_y + rh * 0.92,
            fill=mix_colors(skin, shade_color(skin, -0.10), 0.5), outline="", tags=(tag,),
        )
        for side in (-1, 1):
            self.create_oval(
                x + side * rw * 0.96 - rw * 0.13, head_y - rh * 0.06,
                x + side * rw * 0.96 + rw * 0.13, head_y + rh * 0.30,
                fill=shade_color(skin, -0.16 if side > 0 else -0.04), outline="", tags=(tag,),
            )
        # Hair.
        self.create_arc(
            x - rw * 1.04, head_y - rh * 1.10, x + rw * 1.04, head_y + rh * 0.52,
            start=8, extent=164, style="chord", fill=hair, outline="", tags=(tag,),
        )
        self.create_arc(
            x - rw * 0.92, head_y - rh * 1.02, x + rw * 0.30, head_y + rh * 0.10,
            start=40, extent=100, style="arc",
            outline=shade_color(hair, 0.22), width=max(1, int(unit * 0.09)), tags=(tag,),
        )
        # Rim light along the lit side.
        self.create_arc(
            x - rw * 0.99, head_y - rh * 0.99, x + rw * 0.99, head_y + rh * 0.99,
            start=96, extent=86, style="arc",
            outline=shade_color(skin, 0.45), width=max(1, int(unit * 0.07)), tags=(tag,),
        )
        # Brows, nose, eyes.
        for side in (-1, 1):
            self.create_line(
                x + side * rw * 0.52 - rw * 0.16, head_y - rh * 0.24,
                x + side * rw * 0.52 + rw * 0.16, head_y - rh * 0.28,
                fill=shade_color(hair, -0.05), width=max(1, int(unit * 0.07)), tags=(tag,),
            )
        self.create_line(
            x + rw * 0.04, head_y - rh * 0.02, x - rw * 0.10, head_y + rh * 0.24,
            fill=shade_color(skin, -0.22), width=max(1, int(unit * 0.06)), tags=(tag,),
        )
        eyes = []
        for side in (-1, 1):
            ex = x + side * rw * 0.40
            ey = head_y - rh * 0.04
            self.create_oval(
                ex - rw * 0.20, ey - rh * 0.12, ex + rw * 0.20, ey + rh * 0.12,
                fill=shade_color(skin, 0.55), outline="", tags=(tag,),
            )
            eyes.append(self.create_oval(
                ex - rw * 0.10, ey - rh * 0.09, ex + rw * 0.10, ey + rh * 0.09,
                fill=shade_color(seat.hair, -0.30), outline="", tags=(tag,),
            ))
        mouth = self.create_oval(
            x - rw * 0.24, head_y + rh * 0.50,
            x + rw * 0.24, head_y + rh * 0.58,
            fill=shade_color(skin, -0.42), outline="", tags=(tag,),
        )
        halo = self.create_oval(
            x - rw * 1.8, head_y - rh * 1.8, x + rw * 1.8, head_y + rh * 1.8,
            outline="", width=max(1, int(unit * 0.10)), tags=(tag,),
        )
        self.tag_lower(halo, tag)
        self._dynamic[seat.key] = {
            "mouth": mouth,
            "eyes": eyes,
            "halo": halo,
            "head": (x, head_y),
            "rw": rw,
            "rh": rh,
            "unit": unit,
            "tag": tag,
        }

    def _draw_plate(self, seat: Any, spot: SeatLayout) -> None:
        """The name card lying on the table in front of each seat."""
        theme = self.app.theme
        unit = self.unit * max(0.70, spot.scale * 0.82)
        width = unit * 1.02
        height = unit * 0.62
        top = mix_colors(theme.surface, theme.bg, 0.15)
        body = self.create_polygon(
            rounded_points(
                spot.plate_x - width, spot.plate_y - height,
                spot.plate_x + width, spot.plate_y + height,
                unit * 0.16,
            ),
            fill=shade_color(top, -0.18), outline="", smooth=True, tags=("plate",),
        )
        name = self.create_text(
            spot.plate_x, spot.plate_y - height * 0.34,
            text=seat.name, fill=theme.text,
            font=self.app.fonts.tiny, tags=("plate",),
        )
        badge = self.create_text(
            spot.plate_x, spot.plate_y + height * 0.36,
            text=self.badges.get(seat.key, ""),
            fill=mix_colors(theme.faint, seat.accent, 0.35),
            font=self.app.fonts.tiny, tags=("plate",),
        )
        self._dynamic.setdefault(seat.key, {}).update(
            {"plate": body, "plate_name": name, "plate_badge": badge}
        )

    def _draw_beam_layer(self) -> None:
        theme = self.app.theme
        self._dynamic["beam"] = self.create_line(
            0, 0, 0, 0, fill=theme.accent, width=1, smooth=True, state="hidden",
        )
        self._dynamic["pulse"] = self.create_oval(
            0, 0, 0, 0, fill=theme.accent, outline="", state="hidden",
        )

    # -- state and animation ----------------------------------------------

    def _seat_head(self, key: str) -> tuple[float, float] | None:
        """Where a speech trace starts or lands: the seat's card on the table.

        Anchoring on the cards rather than the heads keeps every trace on the
        table surface, so it can never cut across somebody's face.
        """
        if key == council.OPERATOR_KEY:
            angle = math.radians(OPERATOR_ANGLE)
            return (
                self.cx + self.rx * 0.86 * math.cos(angle),
                self.cy + self.ry * 0.86 * math.sin(angle),
            )
        spot = self._layout.get(key)
        if spot is not None:
            return (spot.plate_x, spot.plate_y)
        return None

    def _paint_states(self) -> None:
        theme = self.app.theme
        if not self._dynamic:
            return
        for seat in self.seats:
            entry = self._dynamic.get(seat.key)
            if not isinstance(entry, dict) or "halo" not in entry:
                continue
            live = seat.key == self.speaking
            heard = seat.key == self.addressee
            try:
                self.itemconfigure(
                    entry["halo"],
                    outline=seat.accent if live else (
                        mix_colors(theme.bg, seat.accent, 0.30) if heard else ""
                    ),
                )
                if "plate" in entry:
                    self.itemconfigure(
                        entry["plate"],
                        fill=mix_colors(
                            shade_color(mix_colors(theme.surface, theme.bg, 0.15), -0.18),
                            seat.accent,
                            0.30 if live else (0.12 if heard else 0.0),
                        ),
                    )
                    self.itemconfigure(
                        entry["plate_name"],
                        fill=seat.accent if live else theme.text,
                    )
                for eye in entry.get("eyes", ()):
                    self.itemconfigure(
                        eye,
                        fill=seat.accent if live else shade_color(seat.hair, -0.30),
                    )
            except tk.TclError:
                return
        operator_glow = self._dynamic.get("operator_glow")
        if operator_glow is not None:
            try:
                self.itemconfigure(
                    operator_glow,
                    outline=theme.accent if self.operator_active else "",
                )
            except tk.TclError:
                pass
        self._paint_beam()

    def _paint_beam(self) -> None:
        beam = self._dynamic.get("beam")
        pulse = self._dynamic.get("pulse")
        if beam is None or pulse is None:
            return
        start = self._seat_head(self.speaking or "")
        end = self._seat_head(self.addressee or "")
        if start is None or end is None or self.speaking == self.addressee:
            try:
                self.itemconfigure(beam, state="hidden")
                self.itemconfigure(pulse, state="hidden")
            except tk.TclError:
                pass
            return
        seat = council.SEAT_BY_KEY.get(self.speaking or "")
        colour = seat.accent if seat is not None else self.app.theme.accent
        control = self._beam_control(start, end)
        points = []
        for step in range(13):
            points.extend(self._bezier(start, control, end, step / 12.0))
        try:
            self.coords(beam, *points)
            self.itemconfigure(
                beam, state="normal",
                fill=mix_colors(self.app.theme.bg, colour, 0.45),
                width=max(1, int(self.unit * 0.08)),
            )
            self.tag_raise(beam)
            self.tag_raise(pulse)
        except tk.TclError:
            pass

    def _beam_control(
        self, start: tuple[float, float], end: tuple[float, float]
    ) -> tuple[float, float]:
        """Bow the trace through the middle of the table, never over a face."""
        midpoint = ((start[0] + end[0]) / 2.0, (start[1] + end[1]) / 2.0)
        return (
            midpoint[0] + (self.cx - midpoint[0]) * 0.55,
            midpoint[1] + (self.cy - midpoint[1]) * 0.55 + self.ry * 0.30,
        )

    @staticmethod
    def _bezier(
        start: tuple[float, float],
        control: tuple[float, float],
        end: tuple[float, float],
        t: float,
    ) -> tuple[float, float]:
        inverse = 1.0 - t
        return (
            inverse * inverse * start[0] + 2 * inverse * t * control[0] + t * t * end[0],
            inverse * inverse * start[1] + 2 * inverse * t * control[1] + t * t * end[1],
        )

    def _tick(self) -> None:
        self._job = None
        if not self.winfo_exists():
            return
        self.phase += 0.16
        try:
            self._animate()
        except tk.TclError:
            return
        self._job = self.after(70, self._tick)

    def _animate(self) -> None:
        if not self._dynamic:
            return
        # Idle breathing: move each seat's whole group by the frame delta.
        for index, seat in enumerate(self.seats):
            entry = self._dynamic.get(seat.key)
            if not isinstance(entry, dict) or "tag" not in entry:
                continue
            target = math.sin(self.phase * 0.55 + index * 1.7) * entry["unit"] * 0.035
            previous = self._bob.get(seat.key, 0.0)
            delta = target - previous
            if abs(delta) >= 0.35:
                self.move(entry["tag"], 0, delta)
                self._bob[seat.key] = target
        entry = self._dynamic.get(self.speaking or "")
        if isinstance(entry, dict) and "mouth" in entry:
            rw, rh = entry["rw"], entry["rh"]
            x, head_y = entry["head"]
            head_y += self._bob.get(self.speaking or "", 0.0)
            open_by = (math.sin(self.phase * 2.6) * 0.5 + 0.5) * rh * 0.22 + rh * 0.03
            self.coords(
                entry["mouth"],
                x - rw * (0.20 + open_by / max(1.0, rh) * 0.35),
                head_y + rh * 0.48,
                x + rw * (0.20 + open_by / max(1.0, rh) * 0.35),
                head_y + rh * 0.52 + open_by,
            )
            halo = 1.55 + math.sin(self.phase * 1.5) * 0.12
            self.coords(
                entry["halo"],
                x - rw * halo * 1.16, head_y - rh * halo * 1.16,
                x + rw * halo * 1.16, head_y + rh * halo * 1.16,
            )
        # A packet of light travelling from the speaker to whoever is addressed.
        pulse = self._dynamic.get("pulse")
        beam = self._dynamic.get("beam")
        if pulse is not None and beam is not None:
            start = self._seat_head(self.speaking or "")
            end = self._seat_head(self.addressee or "")
            if start is None or end is None or self.speaking == self.addressee:
                self.itemconfigure(pulse, state="hidden")
            else:
                control = self._beam_control(start, end)
                travel = (self.phase * 0.30) % 1.0
                px, py = self._bezier(start, control, end, travel)
                radius = max(2.0, self.unit * 0.16)
                seat = council.SEAT_BY_KEY.get(self.speaking or "")
                self.coords(pulse, px - radius, py - radius, px + radius, py + radius)
                self.itemconfigure(
                    pulse, state="normal",
                    fill=seat.accent if seat is not None else self.app.theme.accent,
                )
        emblem = self._dynamic.get("emblem")
        if isinstance(emblem, list):
            for index, item in enumerate(emblem):
                spin = (self.phase * (12 if index % 2 == 0 else -9)) % 360
                self.itemconfigure(item, start=spin + index * 70)


# --------------------------------------------------------------------------
# Council — the floor transcript
# --------------------------------------------------------------------------

_KIND_CHIPS = {
    "agenda": "AGENDA",
    "open_item": "OPENS ITEM",
    "member": "FLOOR",
    "crosstalk": "REPLY",
    "rule": "RULING",
    "answer_operator": "TO YOU",
    "operator": "YOU",
    "report": "REPORT",
    "notice": "NOTICE",
}


class CouncilTurnCard(tk.Frame):
    """One spoken turn: who spoke, who they answered, and what they said."""

    def __init__(self, master: tk.Misc, app: "JarvisDesktop", row: dict[str, Any]) -> None:
        theme = app.theme
        super().__init__(master, bg=theme.bg)
        self.app = app
        speaker = str(row.get("speaker", ""))
        addressee = str(row.get("addressee", ""))
        kind = str(row.get("kind", "member"))
        seat = council.SEAT_BY_KEY.get(speaker)
        accent = (
            seat.accent if seat is not None
            else (theme.info if speaker == council.OPERATOR_KEY else theme.muted)
        )
        surface = theme.surface if speaker != council.OPERATOR_KEY else theme.user_bubble

        card = tk.Frame(self, bg=surface, highlightbackground=theme.border, highlightthickness=1)
        card.pack(fill="x", pady=(0, app.px(6)))
        tk.Frame(card, bg=accent, width=app.px(3)).pack(side="left", fill="y")
        body = tk.Frame(card, bg=surface, padx=app.px(10), pady=app.px(7))
        body.pack(side="left", fill="both", expand=True)

        head = tk.Frame(body, bg=surface)
        head.pack(fill="x")
        tk.Label(
            head, text=council.seat_name(speaker), bg=surface, fg=accent,
            font=app.fonts.small_bold,
        ).pack(side="left")
        tk.Label(
            head, text=f"  →  {council.seat_name(addressee)}", bg=surface,
            fg=theme.muted, font=app.fonts.small,
        ).pack(side="left")
        tk.Label(
            head, text=_KIND_CHIPS.get(kind, kind.upper()), bg=surface,
            fg=theme.faint, font=app.fonts.tiny,
        ).pack(side="right")

        text = AutoText(
            body, app, font=app.fonts.body, bg=surface,
            fg=theme.text if kind != "notice" else theme.warning,
        )
        text.pack(fill="x", pady=(app.px(3), 0))
        text.set_text(safe_ui_text(row.get("text", ""), 4000))


class CouncilView(tk.Frame):
    """The COUNCIL section: the room on the left, the floor on the right."""

    def __init__(self, master: tk.Misc, app: "JarvisDesktop") -> None:
        theme = app.theme
        super().__init__(master, bg=theme.bg)
        self.app = app
        self.theme = theme
        self.depth = str(app.settings.get("council_depth", "Standard"))
        if self.depth not in council.DEPTH_ORDER:
            self.depth = "Standard"
        self.running = False
        self.paused = False
        self.artifacts: dict[str, str] = {}
        self.decision = ""
        self.depth_buttons: dict[str, RoundButton] = {}
        self.night_depth_buttons: dict[str, RoundButton] = {}
        self.night_plan = council.NightPlan.from_mapping(app.settings.get("council_night"))
        self.night_depth = self.night_plan.depth
        self.digest: dict[str, str] | None = None

        left = tk.Frame(self, bg=theme.bg)
        left.pack(side="left", fill="both", expand=True)
        right = tk.Frame(self, bg=theme.panel, width=app.px(390))
        right.pack_propagate(False)
        right.pack(side="right", fill="y")

        self._build_controls(left)
        self.table = CouncilTable(left, app)
        self.table.pack(fill="both", expand=True, padx=app.px(14), pady=(0, app.px(6)))
        self._build_agenda(left)
        self._build_night(left)
        self._build_floor(right)
        self.refresh_status()

    # -- left column ------------------------------------------------------

    def _build_controls(self, parent: tk.Misc) -> None:
        app = self.app
        theme = self.theme
        bar = tk.Frame(parent, bg=theme.bg, padx=app.px(14), pady=app.px(10))
        bar.pack(fill="x")

        title = tk.Frame(bar, bg=theme.bg)
        title.pack(fill="x")
        tk.Label(
            title, text="COUNCIL", bg=theme.bg, fg=theme.text_strong, font=app.fonts.h1,
        ).pack(side="left")
        self.models_label = tk.Label(
            title, text="", bg=theme.bg, fg=theme.faint, font=app.fonts.tiny,
            anchor="w", justify="left",
        )
        self.models_label.pack(side="left", padx=(app.px(10), 0))

        entry_row = tk.Frame(bar, bg=theme.bg)
        entry_row.pack(fill="x", pady=(app.px(8), 0))
        shell = tk.Frame(
            entry_row, bg=theme.surface, highlightbackground=theme.border_strong,
            highlightthickness=1, padx=app.px(10),
        )
        shell.pack(side="left", fill="x", expand=True)
        self.topic_var = tk.StringVar()
        self.topic_entry = tk.Entry(
            shell, textvariable=self.topic_var, bg=theme.surface, fg=theme.text,
            insertbackground=theme.accent, bd=0, highlightthickness=0, font=app.fonts.body,
        )
        self.topic_entry.pack(fill="x", ipady=app.px(6))
        app._placeholder(self.topic_entry, self.topic_var, "What should the council work on?")
        self.topic_entry.bind("<Return>", lambda _e: (self.convene(), "break")[1])

        self.convene_button = RoundButton(
            entry_row, app, "Convene", self.convene, kind="accent",
            icon="◎", padx=app.px(14), pady=app.px(7), tooltip="Open a meeting on this topic",
        )
        self.convene_button.pack(side="left", padx=(app.px(8), 0))
        self.pause_button = RoundButton(
            entry_row, app, "Pause", self.toggle_pause, kind="ghost",
            padx=app.px(12), pady=app.px(7), width=app.px(84),
            tooltip="Hold the floor without losing the meeting",
        )
        self.pause_button.pack(side="left", padx=(app.px(6), 0))
        self.pause_button.set_enabled(False)
        self.adjourn_button = RoundButton(
            entry_row, app, "Adjourn", self.adjourn, kind="danger",
            padx=app.px(12), pady=app.px(7), tooltip="End now and file the report",
        )
        self.adjourn_button.pack(side="left", padx=(app.px(6), 0))
        self.adjourn_button.set_enabled(False)

        depth_row = tk.Frame(bar, bg=theme.bg)
        depth_row.pack(fill="x", pady=(app.px(8), 0))
        tk.Label(
            depth_row, text="DEPTH", bg=theme.bg, fg=theme.faint, font=app.fonts.tiny,
        ).pack(side="left", padx=(0, app.px(6)))
        for name in council.DEPTH_ORDER:
            plan = council.DEPTH_PLANS[name]
            button = RoundButton(
                depth_row, app, name, lambda choice=name: self.set_depth(choice),
                kind="active" if name == self.depth else "ghost",
                padx=app.px(10), pady=app.px(4), font=app.fonts.tiny, radius=8,
                tooltip=plan.label,
            )
            button.pack(side="left", padx=(0, app.px(4)))
            self.depth_buttons[name] = button
        self.status_label = tk.Label(
            depth_row, text="", bg=theme.bg, fg=theme.muted, font=app.fonts.small,
        )
        self.status_label.pack(side="right")

    def _build_agenda(self, parent: tk.Misc) -> None:
        app = self.app
        theme = self.theme
        panel = tk.Frame(
            parent, bg=theme.panel, padx=app.px(14), pady=app.px(10),
        )
        panel.pack(fill="x", padx=app.px(14), pady=(0, app.px(12)))
        header = tk.Frame(panel, bg=theme.panel)
        header.pack(fill="x")
        tk.Label(
            header, text="AGENDA", bg=theme.panel, fg=theme.faint, font=app.fonts.tiny,
        ).pack(side="left")
        self.progress_label = tk.Label(
            header, text="", bg=theme.panel, fg=theme.muted, font=app.fonts.tiny,
        )
        self.progress_label.pack(side="right")
        self.agenda_body = tk.Frame(panel, bg=theme.panel)
        self.agenda_body.pack(fill="x", pady=(app.px(4), 0))
        self.set_agenda([], 0)

        self.artifact_row = tk.Frame(panel, bg=theme.panel)
        self.report_button = RoundButton(
            self.artifact_row, app, "Open report folder", self.open_artifacts,
            kind="ghost", icon="⇪", padx=app.px(10), pady=app.px(5), font=app.fonts.tiny,
        )
        self.report_button.pack(side="left")
        self.focus_button = RoundButton(
            self.artifact_row, app, "Take the decision to chat", self.send_focus_to_chat,
            kind="ghost", icon="→", padx=app.px(10), pady=app.px(5), font=app.fonts.tiny,
        )
        self.focus_button.pack(side="left", padx=(app.px(6), 0))

    def set_agenda(self, items: list[str], current: int) -> None:
        theme = self.theme
        app = self.app
        for child in self.agenda_body.winfo_children():
            child.destroy()
        if not items:
            tk.Label(
                self.agenda_body,
                text="No meeting yet. Give the council a topic and convene.",
                bg=theme.panel, fg=theme.faint, font=app.fonts.small, anchor="w",
            ).pack(fill="x")
            return
        for index, item in enumerate(items):
            live = index == current
            row = tk.Frame(self.agenda_body, bg=theme.panel)
            row.pack(fill="x", pady=1)
            tk.Label(
                row, text=("▶" if live else f"{index + 1}."), bg=theme.panel,
                fg=theme.accent if live else theme.faint, font=app.fonts.tiny, width=2,
            ).pack(side="left")
            tk.Label(
                row, text=safe_ui_text(item, 160), bg=theme.panel,
                fg=theme.text_strong if live else theme.muted,
                font=app.fonts.small_bold if live else app.fonts.small,
                anchor="w", justify="left", wraplength=app.px(520),
            ).pack(side="left", fill="x", expand=True)

    # -- right column -----------------------------------------------------

    def _build_floor(self, parent: tk.Misc) -> None:
        app = self.app
        theme = self.theme
        head = tk.Frame(parent, bg=theme.panel, padx=app.px(14), pady=app.px(12))
        head.pack(fill="x")
        tk.Label(
            head, text="THE FLOOR", bg=theme.panel, fg=theme.text_strong,
            font=app.fonts.title,
        ).pack(anchor="w")
        self.floor_hint = tk.Label(
            head, text="Every word, and who it was said to.", bg=theme.panel,
            fg=theme.faint, font=app.fonts.tiny, anchor="w", justify="left",
            wraplength=app.px(350),
        )
        self.floor_hint.pack(anchor="w")
        tk.Frame(parent, bg=theme.border, height=1).pack(fill="x")

        self.transcript = ScrollFrame(parent, app, bg=theme.bg)
        self.transcript.pack(fill="both", expand=True)
        self.empty_label = tk.Label(
            self.transcript.inner,
            text=(
                "The council is seated.\n\n"
                "JARVIS chairs, the five specialists hold one mandate each, and "
                "nothing said here executes — the meeting produces an agenda, "
                "minutes and a report, and JARVIS decides what Jarvis works on "
                "next.\n\nYou can interrupt at any time; the chair takes your "
                "point before the next speaker."
            ),
            bg=theme.bg, fg=theme.faint, font=app.fonts.small, anchor="w",
            justify="left", wraplength=app.px(330), padx=app.px(14), pady=app.px(14),
        )
        self.empty_label.pack(fill="x")

        composer = tk.Frame(parent, bg=theme.panel, padx=app.px(12), pady=app.px(10))
        composer.pack(fill="x")
        self.intervene_card = tk.Frame(
            composer, bg=theme.surface, highlightbackground=theme.border_strong,
            highlightthickness=1, padx=app.px(10), pady=app.px(6),
        )
        self.intervene_card.pack(fill="x")
        self.intervene = GrowText(self.intervene_card, app, min_lines=1, max_lines=5)
        self.intervene.pack(fill="x")
        self.intervene.bind("<Return>", lambda _e: (self.interject(), "break")[1])
        self.intervene.bind("<Shift-Return>", lambda _e: None)
        row = tk.Frame(composer, bg=theme.panel)
        row.pack(fill="x", pady=(app.px(5), 0))
        tk.Label(
            row, text="Interject — the chair answers you next", bg=theme.panel,
            fg=theme.faint, font=app.fonts.tiny,
        ).pack(side="left")
        RoundButton(
            row, app, "↑", self.interject, kind="accent", padx=app.px(10),
            pady=app.px(3), radius=10, font=app.fonts.icon_small,
            tooltip="Speak to the council (Enter)",
        ).pack(side="right")

    # -- actions ----------------------------------------------------------

    # -- night sessions ---------------------------------------------------

    def _build_night(self, parent: tk.Misc) -> None:
        app = self.app
        theme = self.theme
        plan = self.night_plan
        panel = tk.Frame(parent, bg=theme.panel, padx=app.px(14), pady=app.px(8))
        panel.pack(fill="x", padx=app.px(14), pady=(0, app.px(12)))
        header = tk.Frame(panel, bg=theme.panel)
        header.pack(fill="x")
        tk.Label(
            header, text="WHILE YOU ARE AWAY", bg=theme.panel, fg=theme.faint, font=app.fonts.tiny,
        ).pack(side="left")
        self.night_status = tk.Label(
            header, text="", bg=theme.panel, fg=theme.muted, font=app.fonts.tiny, anchor="e",
        )
        self.night_status.pack(side="right")

        row = tk.Frame(panel, bg=theme.panel)
        row.pack(fill="x", pady=(app.px(6), 0))
        self.night_toggle = RoundButton(
            row, app, "Let the council sit", self.night_toggle_enabled,
            kind="active" if plan.enabled else "ghost", icon="☾",
            padx=app.px(10), pady=app.px(4), font=app.fonts.tiny, radius=8,
            tooltip="Convene on its own while the desktop is idle inside the window",
        )
        self.night_toggle.pack(side="left")

        def field(label: str, variable: tk.StringVar, width: int) -> tk.Entry:
            tk.Label(row, text=label, bg=theme.panel, fg=theme.faint, font=app.fonts.tiny).pack(
                side="left", padx=(app.px(10), app.px(4)),
            )
            shell = tk.Frame(row, bg=theme.surface, highlightbackground=theme.border, highlightthickness=1, padx=6)
            shell.pack(side="left")
            entry = tk.Entry(
                shell, textvariable=variable, width=width, bg=theme.surface, fg=theme.text,
                insertbackground=theme.accent, bd=0, highlightthickness=0, font=app.fonts.tiny,
            )
            entry.pack(ipady=3)
            entry.bind("<Return>", lambda _e: (self.night_apply(), "break")[1])
            return entry

        self.night_window_var = tk.StringVar(value=plan.window)
        field("Window", self.night_window_var, 12)
        self.night_cap_var = tk.StringVar(value=str(plan.cap))
        field("Sittings", self.night_cap_var, 3)
        tk.Label(row, text="Depth", bg=theme.panel, fg=theme.faint, font=app.fonts.tiny).pack(
            side="left", padx=(app.px(10), app.px(4)),
        )
        for name in council.DEPTH_ORDER:
            button = RoundButton(
                row, app, name, lambda choice=name: self.night_set_depth(choice),
                kind="active" if name == self.night_depth else "ghost",
                padx=app.px(8), pady=app.px(3), font=app.fonts.tiny, radius=8,
                tooltip=council.DEPTH_PLANS[name].label,
            )
            button.pack(side="left", padx=(0, app.px(3)))
            self.night_depth_buttons[name] = button
        RoundButton(
            row, app, "Apply", self.night_apply, kind="subtle",
            padx=app.px(10), pady=app.px(4), font=app.fonts.tiny, radius=8,
        ).pack(side="right")

        focus_row = tk.Frame(panel, bg=theme.panel)
        focus_row.pack(fill="x", pady=(app.px(6), 0))
        tk.Label(focus_row, text="Focus", bg=theme.panel, fg=theme.faint, font=app.fonts.tiny).pack(
            side="left", padx=(0, app.px(6)),
        )
        shell = tk.Frame(focus_row, bg=theme.surface, highlightbackground=theme.border, highlightthickness=1, padx=8)
        shell.pack(side="left", fill="x", expand=True)
        self.night_focus_var = tk.StringVar(value=plan.focus)
        self.night_focus_entry = tk.Entry(
            shell, textvariable=self.night_focus_var, bg=theme.surface, fg=theme.text,
            insertbackground=theme.accent, bd=0, highlightthickness=0, font=app.fonts.small,
        )
        self.night_focus_entry.pack(fill="x", ipady=4)
        self.night_focus_entry.bind("<Return>", lambda _e: (self.night_apply(), "break")[1])

        digest_row = tk.Frame(panel, bg=theme.panel)
        digest_row.pack(fill="x", pady=(app.px(6), 0))
        self.digest_label = tk.Label(
            digest_row, text="No night reports yet.", bg=theme.panel, fg=theme.faint,
            font=app.fonts.tiny, anchor="w", justify="left", wraplength=app.px(560),
        )
        self.digest_label.pack(side="left", fill="x", expand=True)
        self.digest_button = RoundButton(
            digest_row, app, "Open the morning digest", self.open_digest, kind="ghost",
            icon="☀", padx=app.px(10), pady=app.px(4), font=app.fonts.tiny, radius=8,
        )
        self.apply_night_state({
            "plan": plan.as_dict(),
            "reason": "Armed" if plan.enabled else "Night sessions are off",
        })

    def _night_plan_from_widgets(self, enabled: bool) -> Any:
        return council.NightPlan.from_mapping({
            "enabled": enabled,
            "window": self.night_window_var.get(),
            "cap": self.night_cap_var.get(),
            "depth": self.night_depth,
            "focus": self.night_focus_var.get(),
            "idle_seconds": self.night_plan.idle_seconds,
        })

    def night_set_depth(self, name: str) -> None:
        if name not in council.DEPTH_PLANS:
            return
        self.night_depth = name
        for key, button in self.night_depth_buttons.items():
            button.set_kind("active" if key == name else "ghost")

    def night_toggle_enabled(self) -> None:
        self._commit_night(self._night_plan_from_widgets(not self.night_plan.enabled))

    def night_apply(self) -> None:
        self._commit_night(self._night_plan_from_widgets(self.night_plan.enabled))

    def _commit_night(self, plan: Any) -> None:
        if not council.valid_window(self.night_window_var.get().strip()):
            self.app.toast("The window must look like 23:30-07:00.", kind="warning")
        self.night_plan = plan
        self.night_window_var.set(plan.window)
        self.night_cap_var.set(str(plan.cap))
        self.night_focus_var.set(plan.focus)
        self.night_set_depth(plan.depth)
        self.night_toggle.set_kind("active" if plan.enabled else "ghost")
        self.app.council_set_night(plan)
        if plan.enabled:
            self.app.toast(
                f"Night sessions on: {plan.window}, up to {plan.cap} sittings, {plan.depth}."
            )
        else:
            self.app.toast("Night sessions off.")

    def apply_night_state(self, payload: Any) -> None:
        if not isinstance(payload, dict):
            return
        plan = payload.get("plan")
        if isinstance(plan, dict):
            try:
                self.night_toggle.set_kind("active" if bool(plan.get("enabled")) else "ghost")
            except tk.TclError:
                return
        reason = safe_ui_text(payload.get("reason", ""), 120)
        sat = payload.get("sat_tonight")
        if isinstance(sat, int) and sat > 0 and self.night_plan.enabled:
            reason = f"{reason} · {sat} sat tonight"
        self.night_status.configure(text=reason)

    def apply_digest(self, payload: Any) -> None:
        if not isinstance(payload, dict) or not payload.get("path"):
            return
        self.digest = {str(key): safe_ui_text(value, 20_000) for key, value in payload.items()}
        text = self.digest.get("text", "")
        first = next(
            (line.strip() for line in text.splitlines() if line.strip() and not line.startswith("#")),
            "",
        )
        count = sum(1 for line in text.splitlines() if line.startswith("## "))
        summary = f"Night of {self.digest.get('night', '')}: {count} sitting{'s' if count != 1 else ''} filed. {first}"
        self.digest_label.configure(text=summary[:220], fg=self.theme.text)
        self.digest_button.pack(side="right", padx=(self.app.px(8), 0))

    def open_digest(self) -> None:
        path = (self.digest or {}).get("path", "")
        if not path:
            self.app.toast("No digest yet.", kind="warning")
            return
        try:
            webbrowser.open(Path(path).as_uri())
        except (ValueError, OSError):
            self.app.copy_text(path, quiet=True)
            self.app.toast("Digest path copied to the clipboard.")

    def set_topic(self, topic: str, unattended: bool, spark: str) -> None:
        if unattended:
            self.topic_var.set(safe_ui_text(topic, 200))
            try:
                self.topic_entry.configure(fg=self.theme.text)
                self.topic_entry._placeholder_active = False  # type: ignore[attr-defined]
            except tk.TclError:
                pass
            hint = "Unattended sitting — the chair chose this topic."
            if spark:
                hint += f" Spark: {safe_ui_text(spark, 90)}."
            self.floor_hint.configure(text=hint)
        else:
            self.floor_hint.configure(text="Every word, and who it was said to.")

    def set_depth(self, name: str) -> None:
        if name not in council.DEPTH_PLANS:
            return
        self.depth = name
        self.app.settings.set("council_depth", name)
        for key, button in self.depth_buttons.items():
            button.set_kind("active" if key == name else "ghost")

    def convene(self) -> None:
        topic = self.topic_var.get().strip()
        if getattr(self.topic_entry, "_placeholder_active", False):
            topic = ""
        if not topic:
            self.app.toast("Give the council a topic first.", kind="warning")
            self.topic_entry.focus_set()
            return
        if self.running:
            self.app.toast("A meeting is already sitting. Adjourn it first.", kind="warning")
            return
        self.clear_floor()
        self.artifacts = {}
        self.decision = ""
        self.artifact_row.pack_forget()
        self.app.council_convene(topic, self.depth)

    def toggle_pause(self) -> None:
        self.app.council_pause(not self.paused)

    def adjourn(self) -> None:
        if not self.running:
            return
        self.app.council_adjourn()

    def interject(self) -> None:
        text = self.intervene.value().strip()
        if not text:
            return
        if not self.running:
            self.app.toast("Convene a meeting before speaking to it.", kind="warning")
            return
        self.intervene.set_value("")
        self.app.council_interject(text[:MAX_PROMPT_CHARS])

    def open_artifacts(self) -> None:
        folder = self.artifacts.get("folder", "")
        if not folder:
            self.app.toast("No report has been filed yet.", kind="warning")
            return
        try:
            webbrowser.open(Path(folder).as_uri())
        except (ValueError, OSError):
            self.app.copy_text(folder, quiet=True)
            self.app.toast("Report path copied to the clipboard.")

    def send_focus_to_chat(self) -> None:
        if not self.decision:
            self.app.toast("The chair has not decided yet.", kind="warning")
            return
        self.app.set_view("chat")
        self.app.edit_prompt(
            "The council decided what to work on next:\n\n"
            f"{self.decision}\n\nHelp me start on it."
        )

    # -- rendering --------------------------------------------------------

    def clear_floor(self) -> None:
        for child in self.transcript.inner.winfo_children():
            child.destroy()
        self.empty_label = None

    def add_turn(self, row: dict[str, Any]) -> None:
        if self.empty_label is not None:
            self.clear_floor()
        card = CouncilTurnCard(self.transcript.inner, self.app, row)
        card.pack(fill="x", padx=self.app.px(12), pady=(self.app.px(6), 0))
        self.transcript.scroll_to_end()

    def set_seats(self, badges: dict[str, str], note: str) -> None:
        self.table.set_badges(badges)
        self.models_label.configure(text=safe_ui_text(note, 180))

    def set_speaking(self, speaker: str | None, addressee: str | None, label: str) -> None:
        self.table.set_speaking(speaker, addressee)
        self.status_label.configure(text=safe_ui_text(label, 90))

    def refresh_status(self) -> None:
        theme = self.theme
        self.pause_button.set_enabled(self.running)
        self.adjourn_button.set_enabled(self.running)
        self.convene_button.set_enabled(not self.running)
        self.pause_button.set_text("Resume" if self.paused else "Pause")
        if not self.running:
            self.status_label.configure(text="No meeting sitting", fg=theme.faint)
            self.table.stop()
            self.table.set_speaking(None, None)
        else:
            self.status_label.configure(fg=theme.muted)
            self.table.start()

    def apply_state(self, payload: dict[str, Any]) -> None:
        # A partial payload (a pause, say) must not wipe the agenda, so every
        # section below is applied only when the worker actually sent it.
        if "agenda" in payload:
            agenda = [safe_ui_text(item, 160) for item in payload.get("agenda") or []]
            self.set_agenda(agenda, int(payload.get("item", 0) or 0))
        if "progress" in payload:
            progress = safe_ui_text(payload.get("progress", ""), 60)
            remaining = payload.get("remaining")
            if isinstance(remaining, int) and remaining > 0 and self.running:
                progress = f"{progress} · about {remaining} turns left"
            self.progress_label.configure(text=progress)
        if "paused" in payload:
            self.paused = bool(payload.get("paused"))
        decision = safe_ui_text(payload.get("decision", "") or "", 2000)
        if decision:
            self.decision = decision
        artifacts = payload.get("artifacts")
        if isinstance(artifacts, dict) and artifacts.get("folder"):
            self.artifacts = {
                str(key): safe_ui_text(value, 400) for key, value in artifacts.items()
            }
            self.artifact_row.pack(fill="x", pady=(self.app.px(8), 0))
        self.refresh_status()


class JarvisDesktop:
    def __init__(self, root: tk.Tk, config: Config) -> None:
        self.root = root
        self.config = config
        self.settings = DesktopSettings(Path(getattr(config, "data_dir", ".")))
        theme_key = str(self.settings.get("theme", "midnight"))
        self.theme = THEMES.get(theme_key, THEMES["midnight"])
        self.scale = 1.0
        try:
            self.scale = max(0.75, min(3.0, float(root.winfo_fpixels("1i")) / 96.0))
        except tk.TclError:
            pass
        self.fonts = Fonts(root)
        self.zoom = int(self.settings.get("zoom", 0) or 0)
        self._apply_zoom()
        self.model_label = str(self.settings.get("model", "Auto"))
        if self.model_label not in MODEL_CHOICES:
            self.model_label = "Auto"
        self.busy = False
        self.deep_confirmed = False
        self.ready = False
        self.conversation_id: int | None = None
        self.chat_title = DEFAULT_CHAT_TITLE
        self.messages: list[Message] = []
        self.cards: list[MessageCard] = []
        self.chats: list[dict[str, Any]] = []
        self.chat_filter = ""
        self.sidebar_visible = bool(self.settings.get("sidebar", True))
        self.active_card: MessageCard | None = None
        self.approval_window: ApprovalWindow | None = None
        self.palette: CommandPalette | None = None
        self.status_text = "Connecting…"
        self.activity_text = "Starting model services"
        self.provider_error: str | None = None
        self.control_state = "unknown"
        self.model_names: dict[str, str] = {}
        self.pending_approvals = 0
        self._closing = False
        self._close_deadline = 0.0
        self._toast_after: str | None = None
        self._last_user_prompt: str | None = None
        self.view = str(self.settings.get("view", "chat"))
        if self.view not in {"chat", "council"}:
            self.view = "chat"
        self.council: CouncilSession | None = None
        self.council_view: CouncilView | None = None
        self.council_turns: list[dict[str, Any]] = []
        self.nav_buttons: dict[str, RoundButton] = {}
        self.night_plan = council.NightPlan.from_mapping(self.settings.get("council_night"))
        self._last_touch = 0.0

        self.root.title(APP_TITLE)
        geometry = str(self.settings.get("geometry", ""))
        self.root.geometry(geometry if re.fullmatch(r"\d+x\d+\+-?\d+\+-?\d+", geometry) else f"{self.px(1280)}x{self.px(820)}")
        self.root.minsize(self.px(900), self.px(600))
        self.root.configure(bg=self.theme.bg)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self._configure_style()
        self.shell: tk.Frame | None = None
        self._build()
        self._bind_shortcuts()
        _apply_titlebar_theme(self.root, self.theme.dark)

        self.session = JarvisSession(config)
        self.session.start()
        if self.night_plan.enabled:
            # The night watch must run even if the Council view is never opened.
            self._ensure_council()
        self.root.after(40, self._poll_events)

    # -- sizing helpers ---------------------------------------------------

    def px(self, value: float) -> int:
        return int(round(value * self.scale))

    def content_width(self) -> int:
        try:
            width = self.messages_view.canvas.winfo_width()
        except (AttributeError, tk.TclError):
            width = self.px(820)
        return max(self.px(320), min(width, self.px(860)))

    def _apply_zoom(self) -> None:
        base = {"body": 11, "body_bold": 11, "body_italic": 11, "body_strike": 11, "inline_code": 10, "mono": 10}
        for name, size in base.items():
            getattr(self.fonts, name).configure(size=max(7, size + self.zoom))

    def _configure_style(self) -> None:
        theme = self.theme
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(
            "Jarvis.Vertical.TScrollbar",
            gripcount=0, background=theme.border_strong, troughcolor=theme.bg,
            bordercolor=theme.bg, lightcolor=theme.bg, darkcolor=theme.bg,
            arrowsize=0, width=self.px(8),
        )
        style.map("Jarvis.Vertical.TScrollbar", background=[("active", theme.faint)])
        style.layout("Jarvis.Vertical.TScrollbar", [("Vertical.Scrollbar.trough", {"children": [("Vertical.Scrollbar.thumb", {"expand": "1", "sticky": "nswe"})], "sticky": "ns"})])

    # -- build ------------------------------------------------------------

    def _build(self) -> None:
        theme = self.theme
        if self.shell is not None:
            self.shell.destroy()
        self.shell = tk.Frame(self.root, bg=theme.bg)
        self.shell.pack(fill="both", expand=True)
        self.sidebar = tk.Frame(self.shell, bg=theme.panel, width=self.px(276))
        self.sidebar.pack_propagate(False)
        if self.sidebar_visible:
            self.sidebar.pack(side="left", fill="y")
        tk.Frame(self.shell, bg=theme.border, width=1).pack(side="left", fill="y")
        self._build_sidebar()
        self.main = tk.Frame(self.shell, bg=theme.bg)
        self.main.pack(side="left", fill="both", expand=True)
        self._build_topbar()
        self.chat_area = tk.Frame(self.main, bg=theme.bg)
        self.messages_view = ScrollFrame(self.chat_area, self, bg=theme.bg)
        self.messages_view.pack(fill="both", expand=True)
        self.messages_view.canvas.bind("<Configure>", self._on_messages_resize, add="+")
        composer_area = tk.Frame(self.chat_area, bg=theme.bg)
        composer_area.pack(fill="x", padx=self.px(28), pady=(4, self.px(14)))
        self.composer = Composer(composer_area, self)
        self.composer.pack(fill="x")
        self.composer.card.configure(padx=self.px(12))
        self.council_view = CouncilView(self.main, self)
        for row in self.council_turns:
            self.council_view.add_turn(row)
        self._show_view()
        self.toast_label = tk.Label(self.root, text="", bg=theme.surface_alt, fg=theme.text, font=self.fonts.small, padx=14, pady=8, highlightbackground=theme.border_strong, highlightthickness=1)
        self.render_messages()
        self.refresh_chat_list()
        self.refresh_status()
        self.composer.set_busy(self.busy)
        self.root.after(60, self.focus_composer)

    def _build_sidebar(self) -> None:
        theme = self.theme
        side = self.sidebar
        for child in side.winfo_children():
            child.destroy()
        brand = tk.Frame(side, bg=theme.panel, padx=self.px(16), pady=self.px(16))
        brand.pack(fill="x")
        tk.Label(brand, text="J", bg=theme.accent, fg=theme.accent_ink, font=self.fonts.avatar, width=2, pady=2).pack(side="left")
        text = tk.Frame(brand, bg=theme.panel)
        text.pack(side="left", padx=(10, 0))
        tk.Label(text, text="JARVIS", bg=theme.panel, fg=theme.text_strong, font=self.fonts.title).pack(anchor="w")
        tk.Label(text, text="LOCAL INTELLIGENCE", bg=theme.panel, fg=theme.accent, font=self.fonts.tiny).pack(anchor="w")
        IconButton(brand, self, "☰", self.toggle_sidebar, tooltip="Hide sidebar (Ctrl+B)").pack(side="right")

        nav = tk.Frame(side, bg=theme.panel)
        nav.pack(fill="x", padx=self.px(16), pady=(0, self.px(10)))
        self.nav_buttons = {}
        for key, label, icon, hint in (
            ("chat", "Chat", "▣", "Talk to Jarvis (Ctrl+M switches)"),
            ("council", "Council", "◎", "Jarvis and his specialists in session (Ctrl+M)"),
        ):
            button = RoundButton(
                nav, self, label, lambda choice=key: self.set_view(choice),
                kind="active" if key == self.view else "subtle", icon=icon,
                padx=8, pady=6, width=self.px(118), font=self.fonts.small, tooltip=hint,
            )
            button.pack(side="left", padx=(0, self.px(6)))
            self.nav_buttons[key] = button

        RoundButton(side, self, "New chat", self.new_chat, kind="ghost", icon="＋", padx=12, pady=8, width=self.px(244), tooltip="Ctrl+N").pack(padx=self.px(16), pady=(0, 8))
        RoundButton(side, self, "Search or jump to…   Ctrl K", self.open_palette, kind="subtle", icon="⌕", padx=10, pady=6, width=self.px(244), font=self.fonts.small).pack(padx=self.px(16), pady=(0, 10))

        search_shell = tk.Frame(side, bg=theme.surface, highlightbackground=theme.border, highlightthickness=1, padx=8)
        search_shell.pack(fill="x", padx=self.px(16), pady=(0, 6))
        self.search_var = tk.StringVar(value=self.chat_filter)
        self.search_entry = tk.Entry(search_shell, textvariable=self.search_var, bg=theme.surface, fg=theme.text, insertbackground=theme.accent, bd=0, highlightthickness=0, font=self.fonts.small)
        self.search_entry.pack(fill="x", ipady=6)
        self._placeholder(self.search_entry, self.search_var, "Filter chats")
        self.search_var.trace_add("write", lambda *_args: self._on_search())

        self.chat_list = ScrollFrame(side, self, bg=theme.panel)
        self.chat_list.pack(fill="both", expand=True, padx=(self.px(10), self.px(6)))

        footer = tk.Frame(side, bg=theme.panel, padx=self.px(16), pady=self.px(12))
        footer.pack(fill="x", side="bottom")
        tk.Label(footer, text="MODEL PROFILE", bg=theme.panel, fg=theme.faint, font=self.fonts.tiny).pack(anchor="w")
        pills = tk.Frame(footer, bg=theme.panel)
        pills.pack(fill="x", pady=(4, 8))
        self.model_buttons: dict[str, RoundButton] = {}
        for index, label in enumerate(MODEL_CHOICES):
            button = RoundButton(pills, self, label, lambda choice=label: self.set_model(choice), kind="active" if label == self.model_label else "ghost", padx=9, pady=4, font=self.fonts.tiny, radius=8, tooltip=f"{MODEL_HINTS[label]} (Ctrl+{index + 1})")
            button.grid(row=index // 3, column=index % 3, padx=(0, 4), pady=(0, 4), sticky="w")
            self.model_buttons[label] = button
        self.model_detail = tk.Label(footer, text="", bg=theme.panel, fg=theme.faint, font=self.fonts.tiny, anchor="w", wraplength=self.px(236), justify="left")
        self.model_detail.pack(fill="x")
        self._refresh_model_detail()

        approvals = tk.Frame(footer, bg=theme.panel)
        approvals.pack(fill="x", pady=(10, 4))
        self.approvals_button = RoundButton(approvals, self, "Approvals", self.show_approvals, kind="ghost", icon="✓", padx=10, pady=6, width=self.px(178), font=self.fonts.small, tooltip="Ctrl+Shift+A")
        self.approvals_button.pack(side="left")
        self.approval_badge = tk.Label(approvals, text="0", bg=theme.surface_alt, fg=theme.muted, font=self.fonts.tiny, padx=7, pady=2)
        self.approval_badge.pack(side="left", padx=(6, 0))
        tools = tk.Frame(footer, bg=theme.panel)
        tools.pack(fill="x", pady=(2, 8))
        IconButton(tools, self, "◐", self.cycle_theme, tooltip="Switch theme (Ctrl+T)").pack(side="left")
        IconButton(tools, self, "⇪", self.export_chat, tooltip="Export chat as Markdown (Ctrl+E)").pack(side="left")
        IconButton(tools, self, "⌨", self.show_shortcuts, tooltip="Keyboard shortcuts (Ctrl+/)").pack(side="left")
        IconButton(tools, self, "◎", self.open_presence, tooltip="Open Presence in the browser").pack(side="left")

        status = tk.Frame(footer, bg=theme.surface, highlightbackground=theme.border, highlightthickness=1, padx=12, pady=10)
        status.pack(fill="x")
        line = tk.Frame(status, bg=theme.surface)
        line.pack(fill="x")
        self.status_dot = tk.Canvas(line, width=10, height=10, bg=theme.surface, highlightthickness=0)
        self.status_dot.pack(side="left")
        self.status_oval = self.status_dot.create_oval(1, 1, 9, 9, fill=theme.warning, outline="")
        self.status_label = tk.Label(line, text=self.status_text, bg=theme.surface, fg=theme.text, font=self.fonts.small_bold)
        self.status_label.pack(side="left", padx=(7, 0))
        self.control_label = tk.Label(line, text="", bg=theme.surface, fg=theme.faint, font=self.fonts.tiny)
        self.control_label.pack(side="right")
        self.activity_label = tk.Label(status, text=self.activity_text, bg=theme.surface, fg=theme.muted, font=self.fonts.tiny, wraplength=self.px(210), justify="left", anchor="w")
        self.activity_label.pack(fill="x", pady=(5, 0))

    def _placeholder(self, entry: tk.Entry, variable: tk.StringVar, text: str) -> None:
        theme = self.theme

        def show() -> None:
            if not variable.get():
                entry.configure(fg=theme.faint)
                entry.insert(0, text)
                entry._placeholder_active = True  # type: ignore[attr-defined]

        def hide(_event: Any = None) -> None:
            if getattr(entry, "_placeholder_active", False):
                entry.delete(0, "end")
                entry.configure(fg=theme.text)
                entry._placeholder_active = False  # type: ignore[attr-defined]

        def restore(_event: Any = None) -> None:
            if not variable.get():
                show()

        entry.bind("<FocusIn>", hide)
        entry.bind("<FocusOut>", restore)
        show()

    def _build_topbar(self) -> None:
        theme = self.theme
        bar = tk.Frame(self.main, bg=theme.bg, padx=self.px(24), pady=self.px(12))
        bar.pack(fill="x")
        tk.Frame(self.main, bg=theme.border, height=1).pack(fill="x")
        self.topbar = bar
        self.sidebar_button = IconButton(bar, self, "☰", self.toggle_sidebar, tooltip="Show sidebar (Ctrl+B)")
        if not self.sidebar_visible:
            self.sidebar_button.pack(side="left", padx=(0, 10))
        titles = tk.Frame(bar, bg=theme.bg)
        titles.pack(side="left", fill="x", expand=True)
        self.title_label = tk.Label(titles, text=self.chat_title, bg=theme.bg, fg=theme.text_strong, font=self.fonts.title, anchor="w", cursor="hand2")
        self.title_label.pack(anchor="w")
        self.title_label.bind("<Double-Button-1>", lambda _e: self.rename_current_chat())
        Tooltip(self.title_label, "Double-click to rename this chat", self)
        self.subtitle_label = tk.Label(titles, text="", bg=theme.bg, fg=theme.faint, font=self.fonts.tiny, anchor="w")
        self.subtitle_label.pack(anchor="w")
        self.stop_button = RoundButton(bar, self, "Stop", self.stop_request, kind="danger", padx=14, pady=6, icon="■", font=self.fonts.small_bold)
        self.stop_button.set_enabled(False)
        self.stop_button.pack(side="right")
        self.theme_chip = tk.Label(bar, text=self.theme.name, bg=theme.surface_alt, fg=theme.muted, font=self.fonts.tiny, padx=8, pady=3, cursor="hand2")
        self.theme_chip.pack(side="right", padx=(0, 10))
        self.theme_chip.bind("<Button-1>", lambda _e: self.cycle_theme())
        Tooltip(self.theme_chip, "Cycle theme (Ctrl+T)", self)

    def _bind_shortcuts(self) -> None:
        root = self.root
        root.bind_all("<Control-k>", lambda _e: (self.open_palette(), "break")[1])
        root.bind_all("<Control-K>", lambda _e: (self.open_palette(), "break")[1])
        root.bind_all("<Control-n>", lambda _e: (self.new_chat(), "break")[1])
        root.bind_all("<Control-l>", lambda _e: (self.focus_composer(), "break")[1])
        root.bind_all("<Control-b>", lambda _e: (self.toggle_sidebar(), "break")[1])
        root.bind_all("<Control-t>", lambda _e: (self.cycle_theme(), "break")[1])
        root.bind_all("<Control-m>", lambda _e: (self.toggle_view(), "break")[1])
        root.bind_all("<Control-e>", lambda _e: (self.export_chat(), "break")[1])
        root.bind_all("<Control-r>", lambda _e: (self.regenerate_last(), "break")[1])
        root.bind_all("<Control-slash>", lambda _e: (self.show_shortcuts(), "break")[1])
        root.bind_all("<F1>", lambda _e: (self.show_shortcuts(), "break")[1])
        root.bind_all("<Control-Shift-C>", lambda _e: (self.copy_last_reply(), "break")[1])
        root.bind_all("<Control-Shift-A>", lambda _e: (self.show_approvals(), "break")[1])
        root.bind_all("<Control-plus>", lambda _e: (self.zoom_text(1), "break")[1])
        root.bind_all("<Control-equal>", lambda _e: (self.zoom_text(1), "break")[1])
        root.bind_all("<Control-minus>", lambda _e: (self.zoom_text(-1), "break")[1])
        root.bind_all("<Escape>", self._escape)
        root.bind_all("<Key>", self._touch, add="+")
        root.bind_all("<Button-1>", self._touch, add="+")
        for index, label in enumerate(MODEL_CHOICES):
            root.bind_all(f"<Control-Key-{index + 1}>", lambda _e, choice=label: (self.set_model(choice), "break")[1])
        root.bind("<Configure>", self._on_root_configure)

    # -- rendering --------------------------------------------------------

    def render_messages(self) -> None:
        theme = self.theme
        for child in self.messages_view.inner.winfo_children():
            child.destroy()
        self.cards = []
        self.active_card = None
        if not self.messages:
            EmptyState(self.messages_view.inner, self).pack(fill="both", expand=True)
            self.messages_view.scroll_to_top()
            return
        column = tk.Frame(self.messages_view.inner, bg=theme.bg)
        column.pack(fill="x", pady=(self.px(10), self.px(20)))
        self.messages_column = column
        for message in self.messages:
            card = MessageCard(self._card_parent(), self, message)
            card.pack(fill="x")
            self.cards.append(card)
            if message.working or message.streaming:
                self.active_card = card
        self.messages_view.scroll_to_end()

    def _card_parent(self) -> tk.Misc:
        column = getattr(self, "messages_column", None)
        if column is None or not column.winfo_exists():
            column = tk.Frame(self.messages_view.inner, bg=self.theme.bg)
            column.pack(fill="x", pady=(self.px(10), self.px(20)))
            self.messages_column = column
        return column

    def append_message(self, message: Message) -> MessageCard:
        if not self.messages:
            for child in self.messages_view.inner.winfo_children():
                child.destroy()
        self.messages.append(message)
        card = MessageCard(self._card_parent(), self, message)
        card.pack(fill="x")
        self.cards.append(card)
        self.messages_view.scroll_to_end()
        self.root.after(160, self._refit_texts)
        return card

    def _on_messages_resize(self, _event: Any) -> None:
        if getattr(self, "_resize_after", None):
            try:
                self.root.after_cancel(self._resize_after)
            except tk.TclError:
                pass
        self._resize_after = self.root.after(120, self._refit_texts)

    def _refit_texts(self) -> None:
        self._resize_after = None
        for widget in self._all_autotexts(self.messages_view.inner):
            widget.fit()

    def _all_autotexts(self, root: tk.Misc) -> list[AutoText]:
        found: list[AutoText] = []
        stack = [root]
        while stack:
            current = stack.pop()
            for child in current.winfo_children():
                if isinstance(child, AutoText):
                    found.append(child)
                stack.append(child)
        return found

    def refresh_chat_list(self) -> None:
        theme = self.theme
        container = self.chat_list.inner
        for child in container.winfo_children():
            child.destroy()
        query = self.chat_filter.strip().lower()
        visible = [
            chat for chat in self.chats
            if (not query or query in chat["title"].lower())
            and (chat.get("message_count", 0) > 0 or chat["id"] == self.conversation_id)
        ]
        rank = {"Today": 0, "Yesterday": 1, "Previous 7 days": 2, "Previous 30 days": 3, "Earlier": 4}
        visible.sort(key=lambda chat: rank.get(chat_group_label(chat.get("created_at", "")), 5))
        if not visible:
            tk.Label(container, text="No chats yet." if not self.chats else "No chats match.", bg=theme.panel, fg=theme.faint, font=self.fonts.small, pady=10).pack(fill="x", padx=8)
            return
        last_group = None
        for chat in visible:
            group = chat_group_label(chat.get("created_at", ""))
            if group != last_group:
                tk.Label(container, text=group.upper(), bg=theme.panel, fg=theme.faint, font=self.fonts.tiny, anchor="w").pack(fill="x", padx=8, pady=(8, 2))
                last_group = group
            self._chat_row(container, chat)

    def _chat_row(self, container: tk.Misc, chat: dict[str, Any]) -> None:
        theme = self.theme
        active = chat["id"] == self.conversation_id
        bg = theme.surface_hover if active else theme.panel
        row = tk.Frame(container, bg=bg, padx=9, pady=5, cursor="hand2")
        row.pack(fill="x", pady=1)
        title = tk.Label(row, text=chat["title"], bg=bg, fg=theme.text_strong if active else theme.text, font=self.fonts.small_bold if active else self.fonts.small, anchor="w")
        title.pack(fill="x")
        count = chat.get("message_count", 0)
        meta_text = f"{count} message{'s' if count != 1 else ''}"
        if chat.get("project_name") and chat["project_name"].lower() not in {"default workspace", "default"}:
            meta_text += f" · {chat['project_name']}"
        meta = tk.Label(row, text=meta_text, bg=bg, fg=theme.faint, font=self.fonts.tiny, anchor="w")
        meta.pack(fill="x")
        widgets = (row, title, meta)

        def enter(_e: Any) -> None:
            if chat["id"] != self.conversation_id:
                for item in widgets:
                    item.configure(bg=theme.sidebar_hover if hasattr(theme, "sidebar_hover") else theme.surface_alt)

        def leave(_e: Any) -> None:
            if chat["id"] != self.conversation_id:
                for item in widgets:
                    item.configure(bg=theme.panel)

        for item in widgets:
            item.bind("<Enter>", enter)
            item.bind("<Leave>", leave)
            item.bind("<Button-1>", lambda _e, target=chat["id"]: self.open_chat(target))
            item.bind("<Button-3>", lambda event, target=chat: self._chat_menu(event, target))

    def _chat_menu(self, event: Any, chat: dict[str, Any]) -> None:
        theme = self.theme
        menu = tk.Menu(self.root, tearoff=0, bg=theme.surface, fg=theme.text, activebackground=theme.surface_hover, activeforeground=theme.text_strong, bd=0, font=self.fonts.small)
        menu.add_command(label="Open", command=lambda: self.open_chat(chat["id"]))
        menu.add_command(label="Rename…", command=lambda: self.rename_chat(chat))
        menu.add_command(label="Copy title", command=lambda: self.copy_text(chat["title"]))
        menu.add_separator()
        menu.add_command(label="Delete", command=lambda: self.delete_chat(chat))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def refresh_status(self) -> None:
        theme = self.theme
        online = self.ready and not self._closing and not self.provider_error
        color = theme.warning if self.busy else (theme.success if online else theme.danger if self.status_text in {"Offline", "Startup failed", "Provider offline"} else theme.warning)
        try:
            self.status_dot.itemconfigure(self.status_oval, fill=color)
            self.status_label.configure(text="Working" if self.busy else self.status_text)
            self.activity_label.configure(text=self.activity_text)
            self.control_label.configure(text=f"bg: {self.control_state}" if self.control_state != "unknown" else "")
            self.approval_badge.configure(text=str(self.pending_approvals), bg=theme.accent if self.pending_approvals else theme.surface_alt, fg=theme.accent_ink if self.pending_approvals else theme.muted)
            self.subtitle_label.configure(text=self.activity_text if self.busy else self._subtitle())
        except (AttributeError, tk.TclError):
            pass

    def _subtitle(self) -> str:
        parts = []
        if self.messages:
            parts.append(f"{len(self.messages)} message{'s' if len(self.messages) != 1 else ''}")
        name = self.model_names.get(model_override_for(self.model_label))
        parts.append(f"{self.model_label}{' · ' + name if name else ''}")
        return "  ·  ".join(parts)

    def _refresh_model_detail(self) -> None:
        name = self.model_names.get(model_override_for(self.model_label), "")
        detail = MODEL_HINTS.get(self.model_label, "")
        self.model_detail.configure(text=f"{detail}{' · ' + name if name else ''}")

    # -- actions ----------------------------------------------------------

    def focus_composer(self) -> None:
        try:
            self.composer.input.focus_set()
        except (AttributeError, tk.TclError):
            pass

    def send(self) -> None:
        if self.busy or not self.ready:
            if not self.ready:
                self.toast("Jarvis is still starting up.", kind="warning")
            return
        text, attachments = self.composer.take()
        if not text and not attachments:
            return
        if not text:
            text = "Describe the attached image."
        if len(text) > MAX_PROMPT_CHARS:
            self.toast(f"Jarvis accepts at most {MAX_PROMPT_CHARS:,} characters.", kind="warning")
            self.composer.input.set_value(text)
            return
        if self.model_label == "Deep 30B" and not self.deep_confirmed:
            if not messagebox.askyesno(
                "Load the 30B deep model?",
                "Deep mode is higher quality but will temporarily use substantial CPU, RAM, and GPU resources. It unloads after the request. Continue?",
                parent=self.root,
            ):
                self.composer.input.set_value(text)
                return
            self.deep_confirmed = True
        self._last_user_prompt = text
        if self.chat_title == DEFAULT_CHAT_TITLE:
            self.set_chat_title(chat_title_from_prompt(text))
        self.append_message(Message("user", text, attachments=[Path(item).name for item in attachments]))
        working = Message("assistant", "", working=True)
        self.active_card = self.append_message(working)
        self.session.submit(text, self.model_label, attachments)
        self.focus_composer()

    def use_suggestion(self, text: str) -> None:
        self.composer.input.set_value(text)
        self.focus_composer()

    def edit_prompt(self, text: str) -> None:
        self.composer.input.set_value(text)
        self.focus_composer()
        self.composer.input.mark_set("insert", "end")

    def quote_text(self, text: str) -> None:
        quoted = "\n".join(f"> {line}" for line in text.strip().splitlines()[:40])
        current = self.composer.input.value().rstrip()
        self.composer.input.set_value(f"{current}\n\n{quoted}\n\n" if current else f"{quoted}\n\n")
        self.focus_composer()
        self.composer.input.mark_set("insert", "end")

    def regenerate_last(self) -> None:
        if self.busy or not self._last_user_prompt:
            if self.busy:
                self.toast("Wait for the current reply to finish.", kind="warning")
            return
        self.composer.input.set_value(self._last_user_prompt)
        self.send()

    def copy_text(self, text: str, *, quiet: bool = False) -> None:
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
        except tk.TclError:
            return
        if not quiet:
            self.toast("Copied to clipboard.", kind="success")

    def copy_last_reply(self) -> None:
        for message in reversed(self.messages):
            if message.role == "assistant" and message.content and not message.working:
                self.copy_text(message.content)
                return
        self.toast("No reply to copy yet.", kind="warning")

    def stop_request(self) -> None:
        if self.busy:
            self.activity_text = "Stopping safely…"
            self.refresh_status()
            self.session.cancel()

    def new_chat(self) -> None:
        if self.busy:
            self.toast("Stop or finish the current request first.", kind="warning")
            return
        self.session.new_chat()

    def open_chat(self, conversation_id: int) -> None:
        if self.busy:
            self.toast("Stop or finish the current request first.", kind="warning")
            return
        if conversation_id == self.conversation_id:
            return
        self.session.load_chat(conversation_id)

    def rename_chat(self, chat: dict[str, Any]) -> None:
        title = self._ask_text("Rename chat", "New title", chat["title"])
        if title:
            self.session.rename_chat(chat["id"], title)
            if chat["id"] == self.conversation_id:
                self.set_chat_title(title)

    def rename_current_chat(self) -> None:
        if self.conversation_id is None:
            return
        self.rename_chat({"id": self.conversation_id, "title": self.chat_title})

    def delete_chat(self, chat: dict[str, Any]) -> None:
        if self.busy:
            self.toast("Stop or finish the current request first.", kind="warning")
            return
        if not messagebox.askyesno("Delete chat", f"Delete “{chat['title']}”? Project files stay untouched.", parent=self.root):
            return
        self.session.delete_chat(chat["id"])

    def set_chat_title(self, title: str) -> None:
        self.chat_title = compact_activity(title, 120) or DEFAULT_CHAT_TITLE
        try:
            self.title_label.configure(text=self.chat_title)
            self.root.title(f"{self.chat_title} — {APP_TITLE}" if self.chat_title != DEFAULT_CHAT_TITLE else APP_TITLE)
        except tk.TclError:
            pass

    def set_model(self, label: str) -> None:
        if label not in MODEL_CHOICES:
            return
        self.model_label = label
        self.settings.set("model", label)
        for name, button in self.model_buttons.items():
            button.set_kind("active" if name == label else "ghost")
        self._refresh_model_detail()
        self.composer.refresh_model_label()
        self.refresh_status()

    def cycle_model(self) -> None:
        index = MODEL_CHOICES.index(self.model_label)
        self.set_model(MODEL_CHOICES[(index + 1) % len(MODEL_CHOICES)])

    def cycle_theme(self) -> None:
        index = THEME_ORDER.index(self.theme.key)
        self.set_theme(THEME_ORDER[(index + 1) % len(THEME_ORDER)])

    def set_theme(self, key: str) -> None:
        theme = THEMES.get(key)
        if theme is None or theme.key == self.theme.key:
            return
        self.theme = theme
        self.settings.set("theme", key)
        self.root.configure(bg=theme.bg)
        self._configure_style()
        self._build()
        _apply_titlebar_theme(self.root, theme.dark)
        # Nudge the frame so DWM repaints the title bar immediately.
        try:
            geometry = self.root.geometry()
            width, rest = geometry.split("x", 1)
            height = rest.split("+", 1)[0]
            self.root.geometry(f"{int(width) + 1}x{height}")
            self.root.after(30, lambda: self.root.geometry(geometry))
        except (ValueError, tk.TclError):
            pass
        self.toast(f"Theme: {theme.name}")

    def zoom_text(self, delta: int) -> None:
        self.zoom = max(-3, min(6, self.zoom + delta))
        self.settings.set("zoom", self.zoom)
        self._apply_zoom()
        self.root.after(30, self._refit_texts)

    def toggle_sidebar(self) -> None:
        self.sidebar_visible = not self.sidebar_visible
        self.settings.set("sidebar", self.sidebar_visible)
        if self.sidebar_visible:
            self.sidebar.pack(side="left", fill="y", before=self.main)
            self.sidebar_button.pack_forget()
        else:
            self.sidebar.pack_forget()
            self.sidebar_button.pack(side="left", padx=(0, 10), before=self.topbar.winfo_children()[1])
        self.root.after(80, self._refit_texts)

    # -- views ------------------------------------------------------------

    def set_view(self, key: str) -> None:
        if key not in {"chat", "council"} or key == self.view:
            return
        self.view = key
        self.settings.set("view", key)
        self._show_view()
        if key == "council":
            self._ensure_council()

    def toggle_view(self) -> None:
        self.set_view("council" if self.view == "chat" else "chat")

    def _show_view(self) -> None:
        for name, button in self.nav_buttons.items():
            button.set_kind("active" if name == self.view else "subtle")
        chat = getattr(self, "chat_area", None)
        council_view = self.council_view
        if chat is None or council_view is None:
            return
        if self.view == "council":
            chat.pack_forget()
            council_view.pack(fill="both", expand=True)
            council_view.refresh_status()
            self.root.after(50, council_view.table.render)
        else:
            council_view.pack_forget()
            council_view.table.stop()
            chat.pack(fill="both", expand=True)
        try:
            self.title_label.configure(
                text="Council" if self.view == "council" else self.chat_title
            )
        except tk.TclError:
            pass
        self.refresh_status()

    # -- council ----------------------------------------------------------

    def _ensure_council(self) -> None:
        if self.council is None:
            self.council = CouncilSession(self.config, self.night_plan)
            self.council.start()

    def council_convene(self, topic: str, depth: str) -> None:
        self._ensure_council()
        self.council_turns = []
        if self.council is not None:
            self.council.convene(topic, depth)
        self.toast("The council is convening…")

    def council_pause(self, paused: bool) -> None:
        if self.council is None:
            return
        if paused:
            self.council.pause()
        else:
            self.council.resume()

    def council_adjourn(self) -> None:
        if self.council is None:
            return
        self.council.adjourn()
        self.toast("Adjourning — the chair is filing the report.")

    def council_interject(self, text: str) -> None:
        if self.council is None:
            return
        self.council.interject(text)

    def council_set_night(self, plan: Any) -> None:
        self.night_plan = plan
        self.settings.set("council_night", plan.as_dict())
        self._ensure_council()
        if self.council is not None:
            self.council.set_night(plan)

    def _touch(self, _event: Any = None) -> None:
        """Tell the council the operator is here; throttled, never for the interject box."""
        if self.council is None:
            return
        now = time.monotonic()
        if now - self._last_touch < 1.5:
            return
        view = self.council_view
        try:
            if view is not None and self.root.focus_get() is view.intervene:
                return
        except (tk.TclError, KeyError):
            pass
        self._last_touch = now
        self.council.touch()

    def _handle_council_event(self, event: SessionEvent) -> None:
        view = self.council_view
        if view is None:
            return
        kind = event.kind
        payload = event.payload if isinstance(event.payload, dict) else {}
        if kind == "council_ready":
            view.set_seats(payload.get("badges", {}), str(payload.get("models", "")))
            view.apply_night_state(payload.get("night"))
            view.apply_digest(payload.get("digest"))
        elif kind == "council_night":
            view.apply_night_state(payload)
        elif kind == "council_digest":
            view.apply_digest(payload)
        elif kind == "council_opened":
            view.running = True
            view.paused = False
            self.council_turns = []
            view.clear_floor()
            view.set_seats(payload.get("badges", {}), str(payload.get("models", "")))
            view.apply_state(payload)
            unattended = bool(payload.get("unattended"))
            view.set_topic(str(payload.get("topic", "")), unattended, str(payload.get("spark", "")))
            if unattended:
                self.toast(f"The council convened itself: {safe_ui_text(payload.get('topic', ''), 80)}")
        elif kind == "council_speaking":
            view.set_speaking(
                str(payload.get("speaker") or ""),
                str(payload.get("addressee") or ""),
                str(payload.get("label", "")),
            )
        elif kind == "council_turn":
            row = dict(payload)
            self.council_turns.append(row)
            if len(self.council_turns) > 400:
                del self.council_turns[:-400]
            view.add_turn(row)
        elif kind == "council_state":
            view.apply_state(payload)
        elif kind == "council_activity":
            view.set_speaking(
                council.CHAIR_KEY, council.OPERATOR_KEY,
                safe_ui_text(event.payload, 90),
            )
        elif kind == "council_closed":
            view.running = False
            view.paused = False
            view.set_speaking(None, None, "Meeting closed")
            view.apply_state(payload)
            self.toast("Unattended sitting filed to the night digest." if payload.get("unattended") else "Council report filed.", kind="success")
        elif kind == "council_error":
            view.running = False
            view.set_speaking(None, None, "Council stopped")
            view.refresh_status()
            self.toast(safe_ui_text(event.payload, 200), kind="error")

    def show_approvals(self) -> None:
        self.session.request_approvals()

    def show_shortcuts(self) -> None:
        ShortcutsWindow(self)

    def open_presence(self) -> None:
        port = int(getattr(self.config, "presence_port", 8787) or 8787)
        webbrowser.open(f"http://127.0.0.1:{port}/")
        self.toast("Opening Presence — start it with start_jarvis_presence.bat if it is not running.")

    def export_chat(self) -> None:
        exportable = [message for message in self.messages if not message.working]
        if not exportable:
            self.toast("Nothing to export yet.", kind="warning")
            return
        safe_title = re.sub(r"[^A-Za-z0-9 _-]+", "", self.chat_title).strip() or "jarvis-chat"
        path = filedialog.asksaveasfilename(
            parent=self.root, title="Export chat", defaultextension=".md",
            initialfile=f"{safe_title}.md", filetypes=[("Markdown", "*.md"), ("Text", "*.txt")],
        )
        if not path:
            return
        lines = [f"# {self.chat_title}", "", f"_Exported from JARVIS Desktop on {datetime.now():%Y-%m-%d %H:%M}_", ""]
        for message in exportable:
            who = "You" if message.role == "user" else "Jarvis"
            stamp = format_clock(message.created_at)
            lines.append(f"## {who}{' · ' + stamp if stamp else ''}")
            lines.append("")
            lines.append(message.content.strip())
            lines.append("")
        try:
            Path(path).write_text("\n".join(lines), encoding="utf-8")
        except OSError as exc:
            self.toast(f"Export failed: {exc}", kind="error")
            return
        self.toast(f"Exported to {Path(path).name}", kind="success")

    def open_palette(self) -> None:
        if self.palette is not None:
            self.palette.close()
            return
        items: list[dict[str, Any]] = [
            {"group": "Actions", "icon": "＋", "label": "New chat", "detail": "Ctrl+N", "run": self.new_chat, "keywords": "create start"},
            {"group": "Actions", "icon": "✓", "label": "Review approvals", "detail": f"{self.pending_approvals} pending", "run": self.show_approvals, "keywords": "approve deny sensitive"},
            {"group": "Actions", "icon": "■", "label": "Stop the current request", "detail": "Esc", "run": self.stop_request, "keywords": "cancel abort"},
            {"group": "Actions", "icon": "↻", "label": "Regenerate the last reply", "detail": "Ctrl+R", "run": self.regenerate_last, "keywords": "retry again"},
            {"group": "Actions", "icon": "⇪", "label": "Export chat as Markdown", "detail": "Ctrl+E", "run": self.export_chat, "keywords": "save download"},
            {"group": "Actions", "icon": "⌨", "label": "Keyboard shortcuts", "detail": "Ctrl+/", "run": self.show_shortcuts, "keywords": "help keys"},
            {"group": "Actions", "icon": "◎", "label": "Open the Council", "detail": "Ctrl+M", "run": lambda: self.set_view("council"), "keywords": "council meeting round table specialists agenda minutes"},
            {"group": "Actions", "icon": "▣", "label": "Back to chat", "detail": "Ctrl+M", "run": lambda: self.set_view("chat"), "keywords": "conversation chat"},
            {"group": "Actions", "icon": "◎", "label": "Open Presence in the browser", "detail": "", "run": self.open_presence, "keywords": "web browser presence"},
            {"group": "Actions", "icon": "☰", "label": "Toggle sidebar", "detail": "Ctrl+B", "run": self.toggle_sidebar, "keywords": "hide show"},
            {"group": "Actions", "icon": "⟳", "label": "Reconnect model provider", "detail": "offline" if self.provider_error else "connected", "run": self.retry_provider, "keywords": "ollama retry provider offline"},
        ]
        for label in MODEL_CHOICES:
            items.append({"group": "Model", "icon": "◇", "label": f"Use {label} model", "detail": MODEL_HINTS[label], "run": lambda choice=label: self.set_model(choice), "keywords": "model profile switch"})
        for key in THEME_ORDER:
            items.append({"group": "Theme", "icon": "◐", "label": f"{THEMES[key].name} theme", "detail": "dark" if THEMES[key].dark else "light", "run": lambda target=key: self.set_theme(target), "keywords": "appearance look colors"})
        for chat in [row for row in self.chats if row.get("message_count", 0) > 0][:60]:
            items.append({"group": "Chats", "icon": "◌", "label": chat["title"], "detail": chat_group_label(chat.get("created_at", "")), "run": lambda target=chat["id"]: self.open_chat(target), "keywords": "conversation"})
        self.palette = CommandPalette(self, items)

    def _ask_text(self, title: str, label: str, initial: str = "") -> str | None:
        theme = self.theme
        dialog = tk.Toplevel(self.root)
        dialog.title(title)
        dialog.configure(bg=theme.bg)
        dialog.transient(self.root)
        dialog.resizable(False, False)
        _apply_titlebar_theme(dialog, theme.dark)
        body = tk.Frame(dialog, bg=theme.bg, padx=self.px(22), pady=self.px(18))
        body.pack()
        tk.Label(body, text=label, bg=theme.bg, fg=theme.muted, font=self.fonts.small).pack(anchor="w")
        variable = tk.StringVar(value=initial)
        entry = tk.Entry(body, textvariable=variable, bg=theme.surface, fg=theme.text, insertbackground=theme.accent, bd=0, highlightthickness=1, highlightbackground=theme.border_strong, highlightcolor=theme.accent, font=self.fonts.body, width=44)
        entry.pack(fill="x", ipady=7, pady=(6, 12))
        result: dict[str, str | None] = {"value": None}

        def accept(_event: Any = None) -> None:
            result["value"] = variable.get().strip()
            dialog.destroy()

        actions = tk.Frame(body, bg=theme.bg)
        actions.pack(fill="x")
        RoundButton(actions, self, "Save", accept, kind="accent", padx=14, pady=6).pack(side="right")
        RoundButton(actions, self, "Cancel", dialog.destroy, kind="ghost", padx=14, pady=6).pack(side="right", padx=(0, 8))
        entry.bind("<Return>", accept)
        entry.bind("<Escape>", lambda _e: dialog.destroy())
        dialog.update_idletasks()
        x = self.root.winfo_rootx() + (self.root.winfo_width() - dialog.winfo_width()) // 2
        y = self.root.winfo_rooty() + self.px(160)
        dialog.geometry(f"+{x}+{y}")
        entry.focus_set()
        entry.selection_range(0, "end")
        dialog.grab_set()
        self.root.wait_window(dialog)
        return result["value"] or None

    def toast(self, text: str, *, kind: str = "info") -> None:
        theme = self.theme
        colors = {"info": theme.accent, "success": theme.success, "warning": theme.warning, "error": theme.danger}
        try:
            self.toast_label.configure(text=text, highlightbackground=colors.get(kind, theme.accent))
            self.toast_label.place(relx=1.0, rely=1.0, x=-self.px(20), y=-self.px(20), anchor="se")
            self.toast_label.lift()
        except tk.TclError:
            return
        if self._toast_after is not None:
            try:
                self.root.after_cancel(self._toast_after)
            except tk.TclError:
                pass
        self._toast_after = self.root.after(2600, self.toast_label.place_forget)

    def _escape(self, _event: Any) -> None:
        if self.palette is not None:
            self.palette.close()
            return
        if self.busy:
            self.stop_request()

    def _on_search(self) -> None:
        if getattr(self.search_entry, "_placeholder_active", False):
            return
        self.chat_filter = self.search_var.get()
        self.refresh_chat_list()

    def _on_root_configure(self, event: Any) -> None:
        if event.widget is self.root and not self._closing:
            geometry = self.root.geometry()
            if re.fullmatch(r"\d+x\d+\+-?\d+\+-?\d+", geometry):
                self._pending_geometry = geometry

    # -- session events ----------------------------------------------------

    def _apply_provider_state(self, error: Any) -> None:
        self.provider_error = safe_ui_text(error, 400) if error else None
        if self.provider_error:
            self.status_text = "Provider offline"
            self.activity_text = f"{self.provider_error} Jarvis will retry when you send a message."
        else:
            self.status_text = "Online"
            if self.activity_text.startswith(("Could not", "Model provider", "The model")):
                self.activity_text = "Ready"
        self.refresh_status()

    def retry_provider(self) -> None:
        self.activity_text = "Reconnecting to the model provider…"
        self.refresh_status()
        self.session.retry_provider()

    def _set_busy(self, busy: bool) -> None:
        self.busy = busy
        self.composer.set_busy(busy)
        self.stop_button.set_enabled(busy)
        for button in self.model_buttons.values():
            button.set_enabled(not busy)
        self.refresh_status()

    def _finish_active(self, payload: dict[str, Any], *, error: bool = False) -> None:
        card = self.active_card
        if card is None:
            message = Message("assistant", "", working=True)
            card = self.append_message(message)
        message = card.message
        message.content = safe_ui_text(payload.get("content") if not error else payload.get("message", ""))
        message.status = "error" if error else str(payload.get("status", "complete"))
        message.error = error
        message.model = payload.get("model")
        message.elapsed = payload.get("elapsed")
        message.approval_id = payload.get("approval_id")
        message.tool_calls = int(payload.get("tool_calls") or 0)
        message.created_at = time.time()
        card.render_final()
        self.active_card = None
        self.messages_view.scroll_to_end()
        self.root.after(160, self._refit_texts)
        self.root.after(400, self.messages_view.scroll_to_end)
        if message.approval_id is not None:
            self.session.request_approvals()
            self.toast(f"Approval #{message.approval_id} is waiting for review.", kind="warning")

    def _handle_event(self, event: SessionEvent) -> None:
        kind = event.kind
        payload = event.payload
        if kind == "ready":
            self.ready = True
            self.status_text = "Online"
            self.activity_text = "Ready"
            self.conversation_id = int(payload.get("conversation_id") or 0) or None
            self.control_state = str(payload.get("control_state", "running"))
            self.model_names = {
                "fast": str(payload.get("fast_model") or ""),
                "reasoning": str(payload.get("reasoning_model") or ""),
                "coding": str(payload.get("coding_model") or ""),
                "deep": str(payload.get("deep_model") or ""),
            }
            self.chats = list(payload.get("chats") or [])
            self._refresh_model_detail()
            self.refresh_chat_list()
            self._set_busy(False)
            self._apply_provider_state(payload.get("provider_error"))
        elif kind == "provider":
            self._apply_provider_state((payload or {}).get("error"))
            if not self.provider_error:
                self.toast("Model provider connected.", kind="success")
        elif kind == "busy":
            self._set_busy(bool(payload))
        elif kind == "activity":
            self.activity_text = compact_activity(payload)
            if self.busy and self.active_card is not None and self.active_card.message.working:
                steps = self.active_card.message.steps
                if not steps or steps[-1] != self.activity_text:
                    steps.append(self.activity_text)
                    self.active_card.refresh_steps()
                    self.messages_view.scroll_to_end()
            self.refresh_status()
        elif kind == "delta":
            if self.active_card is not None and isinstance(payload, dict):
                self.active_card.append_delta(str(payload.get("text", "")))
                if self.messages_view.stick_to_bottom:
                    self.messages_view.scroll_to_end()
        elif kind == "assistant":
            self._finish_active(payload)
        elif kind == "error":
            data = payload if isinstance(payload, dict) else {"message": str(payload)}
            if self.busy or self.active_card is not None:
                self._finish_active(data, error=True)
            else:
                self.toast(safe_ui_text(data.get("message", "Something went wrong."), 300), kind="error")
        elif kind == "fatal":
            self.ready = False
            self.status_text = "Offline"
            self.activity_text = safe_ui_text(payload, 300)
            self.refresh_status()
            messagebox.showerror("Jarvis Desktop", safe_ui_text(payload, 2_000), parent=self.root)
        elif kind == "new_chat":
            self.conversation_id = int(payload.get("conversation_id") or 0) or None
            self.messages = []
            self._last_user_prompt = None
            self.set_chat_title(DEFAULT_CHAT_TITLE)
            self.render_messages()
            self.refresh_chat_list()
            self.refresh_status()
            self.focus_composer()
        elif kind == "chat_loaded":
            self.conversation_id = int(payload.get("conversation_id") or 0) or None
            self.set_chat_title(str(payload.get("title") or DEFAULT_CHAT_TITLE))
            self.messages = []
            for row in payload.get("messages", []):
                role = "user" if row.get("role") == "user" else "assistant"
                self.messages.append(Message(role, str(row.get("content", "")), created_at=float(row.get("created_at") or 0)))
                if role == "user":
                    self._last_user_prompt = str(row.get("content", ""))
            self.render_messages()
            self.refresh_chat_list()
            self.refresh_status()
            self.focus_composer()
        elif kind == "chats":
            self.chats = list(payload or [])
            self.refresh_chat_list()
        elif kind == "chat_renamed":
            if payload.get("conversation_id") == self.conversation_id:
                self.set_chat_title(str(payload.get("title") or self.chat_title))
        elif kind == "chat_deleted":
            self.toast("Chat deleted." if payload.get("deleted") else "That chat could not be deleted.", kind="success" if payload.get("deleted") else "error")
        elif kind == "approvals":
            rows = list(payload or [])
            self.pending_approvals = sum(1 for row in rows if row.get("status") == "pending")
            self.refresh_status()
            if self.approval_window is None or not self.approval_window.winfo_exists():
                self.approval_window = ApprovalWindow(self, rows)
            else:
                self.approval_window.update_rows(rows)
                self.approval_window.lift()
        elif kind == "approval_decided":
            if payload.get("changed"):
                decision = "approved" if payload.get("approved") else "denied"
                self.toast(f"Approval #{payload.get('approval_id')} {decision}.", kind="success")
            else:
                self.toast("Approval was already decided or expired.", kind="warning")

    def _poll_events(self) -> None:
        try:
            while True:
                self._handle_event(self.session.events.get_nowait())
        except queue.Empty:
            pass
        except tk.TclError:
            pass
        if self.council is not None:
            try:
                while True:
                    self._handle_council_event(self.council.events.get_nowait())
            except queue.Empty:
                pass
            except tk.TclError:
                pass
        if self._closing:
            if not self.session.is_alive() or time.monotonic() >= self._close_deadline:
                self.root.destroy()
                return
        self.root.after(40, self._poll_events)

    def close(self) -> None:
        if self._closing:
            return
        self._closing = True
        self._close_deadline = time.monotonic() + 3.0
        geometry = getattr(self, "_pending_geometry", None)
        if geometry:
            self.settings.set("geometry", geometry)
        self.status_text = "Closing…"
        self.refresh_status()
        self.session.shutdown()
        if self.council is not None:
            self.council.shutdown()


def run_desktop_ui() -> int:
    root: tk.Tk | None = None
    _enable_high_dpi()
    try:
        config = Config.load()
        root = tk.Tk()
        try:
            root.tk.call("tk", "scaling", float(root.winfo_fpixels("1i")) / 72.0)
        except tk.TclError:
            pass
        JarvisDesktop(root, config)
        root.mainloop()
        return 0
    except Exception as exc:
        if root is not None:
            try:
                root.destroy()
            except tk.TclError:
                pass
        try:
            hidden = tk.Tk()
            hidden.withdraw()
            messagebox.showerror(
                APP_TITLE,
                safe_ui_text(f"Jarvis Desktop could not start ({type(exc).__name__}): {exc}", 2_000),
                parent=hidden,
            )
            hidden.destroy()
        except Exception:
            pass
        return 1


def main() -> int:
    from .provider_setup import ProviderSetupRequired, ensure_ready

    terminal = bool(getattr(sys.stdin, "isatty", lambda: False)())
    try:
        ensure_ready(interactive=terminal, stdin_isatty=terminal)
    except ProviderSetupRequired as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return run_desktop_ui()


if __name__ == "__main__":
    raise SystemExit(main())
