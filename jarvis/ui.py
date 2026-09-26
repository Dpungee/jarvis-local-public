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

import copy
import ctypes
import itertools
import json
import math
import os
import queue
import socket
import struct
import subprocess
import zlib
import re
import sys
import threading
import time
import webbrowser
from concurrent.futures import ThreadPoolExecutor
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
from .config import Config, create_project_workspace, resolve_project_workspace
from .governed_memory import parse_explicit_project_fact
from .memory import Memory
from .model_client import user_model_error_message
from .ollama_client import OllamaError
from .proactive import RuntimeGuard
from .provider_setup import ProviderSetupError, config_with_provider_choice, configure_provider, provider_choice_from_config
from .redaction import StreamingRedactor, redact_secrets


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


MAX_TABLE_COLUMNS = 8


def plain_inline(text: str) -> str:
    """Inline markdown reduced to its visible text (bold/italic markers and
    backticks dropped, link text kept) — for table cells and exports."""
    return "".join(run for _style, run, _url in inline_runs(str(text or "")))


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
            "project_id": int(row.get("project_id") or 1),
            "project_name": compact_activity(row.get("project_name") or "", 80),
        })
    return chats


CHAT_PAGE_ROWS = 300


def _chat_messages(memory: Any, conversation_id: int, *, before_id: int | None = None, limit: int = CHAT_PAGE_ROWS) -> tuple[list[dict[str, Any]], bool]:
    """The newest ``limit`` rows of a conversation (or the ``limit`` rows before
    ``before_id``) oldest-first, plus whether older rows remain (Load earlier)."""
    db = getattr(memory, "db", None)
    bounded = max(1, min(int(limit), 1_000))
    if db is not None:
        try:
            if before_id is not None:
                rows = db.execute(
                    "SELECT role, content, created_at, id FROM messages "
                    "WHERE conversation_id=? AND id<? ORDER BY id DESC LIMIT ?",
                    (int(conversation_id), int(before_id), bounded + 1),
                ).fetchall()
            else:
                rows = db.execute(
                    "SELECT role, content, created_at, id FROM messages "
                    "WHERE conversation_id=? ORDER BY id DESC LIMIT ?",
                    (int(conversation_id), bounded + 1),
                ).fetchall()
            rows = list(rows)
            has_more = len(rows) > bounded
            rows = rows[:bounded]
            return [
                {
                    "role": str(row["role"]),
                    "content": str(row["content"]),
                    "created_at": str(row["created_at"] or ""),
                    "id": _row_int(row, "id"),
                }
                for row in reversed(rows)
            ], has_more
        except Exception:
            pass
    if before_id is not None:
        return [], False
    rows = _safe_call(memory, "recent_messages", conversation_id, limit=bounded, default=[])
    return [
        {"role": str(row.get("role", "assistant")), "content": str(row.get("content", "")), "created_at": ""}
        for row in rows or []
    ], False


def _row_int(row: Any, key: str) -> int | None:
    try:
        value = row[key]
    except (KeyError, IndexError, TypeError):
        return None
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _latest_message_id(memory: Any, conversation_id: int, role: str = "assistant") -> int | None:
    """Id of the newest persisted row for one role (the agent writes the assistant row)."""
    db = getattr(memory, "db", None)
    if db is None:
        return None
    try:
        row = db.execute(
            "SELECT id FROM messages WHERE conversation_id=? AND role=? ORDER BY id DESC LIMIT 1",
            (int(conversation_id), str(role)),
        ).fetchone()
    except Exception:
        return None
    return _row_int(row, "id") if row is not None else None


# Tool trace: what a tool was given and what it returned, bounded and redacted.
TOOL_ARGUMENT_LIMIT = 400
TOOL_PREVIEW_LIMIT = 700
TOOL_LOG_LIMIT = 80
PATH_ARGUMENT_KEYS = (
    "path", "file", "file_path", "filename", "target", "destination", "source",
    "directory", "folder", "cwd", "workspace_path", "output_path",
)
HEADLINE_ARGUMENT_KEYS = PATH_ARGUMENT_KEYS + ("command", "query", "url", "pattern", "name", "prompt", "text", "content", "subject")


def tool_argument_summary(arguments: Any) -> dict[str, str]:
    """Redacted, bounded view of tool arguments for display, never the raw payload."""
    if not isinstance(arguments, dict):
        return {}
    summary: dict[str, str] = {}
    for key in list(arguments)[:10]:
        value = arguments[key]
        if isinstance(value, (dict, list, tuple)):
            try:
                rendered = json.dumps(value, ensure_ascii=False, default=str)
            except Exception:
                rendered = str(value)
        else:
            rendered = str(value)
        summary[str(key)[:40]] = safe_ui_text(rendered, TOOL_ARGUMENT_LIMIT)
    return summary


def tool_headline(name: str, arguments: Any) -> str:
    """The one argument worth showing on the collapsed row (a path, command or query)."""
    if isinstance(arguments, dict):
        for key in HEADLINE_ARGUMENT_KEYS:
            value = arguments.get(key)
            if isinstance(value, (str, int, float)) and str(value).strip():
                return compact_activity(str(value), 96)
        for value in arguments.values():
            if isinstance(value, (str, int, float)) and str(value).strip():
                return compact_activity(str(value), 96)
    return ""


_URI_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]{1,}:")


def is_local_path_text(value: str) -> bool:
    """Reject anything that is not a plain local path: URLs, URI schemes
    (``shell:``, ``ms-screenclip:``, …) and UNC ``\\\\host`` shares."""
    text = str(value or "").strip()
    if not text or "\n" in text or "://" in text:
        return False
    if text.startswith(("\\\\", "//")):
        return False
    if _URI_SCHEME.match(text) and not re.match(r"^[A-Za-z]:[\\/]", text):
        return False
    return True


def tool_paths(arguments: Any, result_value: Any, *, workspace: Any = None) -> list[str]:
    """File-system paths mentioned by the call or its result: only absolute local
    paths, or relative paths that exist under the workspace; never URLs or UNC."""
    found: list[str] = []
    root = Path(str(workspace)) if workspace else None

    def keep(candidate: str) -> str | None:
        if not is_local_path_text(candidate):
            return None
        try:
            path = Path(candidate)
        except (TypeError, ValueError):
            return None
        if path.is_absolute():
            return candidate
        if root is not None:
            try:
                resolved = (root / path).resolve()
                if resolved.exists() and str(resolved).lower().startswith(str(root.resolve()).lower()):
                    return str(resolved)
            except (OSError, ValueError):
                return None
        return None

    def add(value: Any) -> None:
        if isinstance(value, str):
            candidate = value.strip()
            if 1 < len(candidate) <= 300 and ("/" in candidate or "\\" in candidate or "." in candidate):
                kept = keep(candidate)
                if kept and kept not in found:
                    found.append(kept)
        elif isinstance(value, (list, tuple)):
            for item in list(value)[:8]:
                add(item)

    if isinstance(arguments, dict):
        for key in PATH_ARGUMENT_KEYS:
            if key in arguments:
                add(arguments[key])
    if isinstance(result_value, dict):
        for key in ("path", "paths", "written", "files", "created", "target", "output_path"):
            if key in result_value:
                add(result_value[key])
    return [safe_ui_text(item, 300) for item in found[:8]]


def tool_result_summary(raw: Any) -> dict[str, Any]:
    """Classify a tool response and produce a bounded preview of its value."""
    payload: Any = raw
    if isinstance(raw, (str, bytes)):
        try:
            payload = json.loads(raw)
        except (ValueError, TypeError):
            payload = None
    if not isinstance(payload, dict):
        # A non-JSON result is what the tool returned; an empty one is still a
        # completed call, not a failure (the agent reports failures as ok=false).
        preview = safe_ui_text(raw if raw is not None else "", TOOL_PREVIEW_LIMIT)
        return {"status": "ok", "preview": preview, "truncated": len(preview) >= TOOL_PREVIEW_LIMIT, "value": None, "approval_id": None}
    approval_required = payload.get("approval_required") is True
    ok = bool(payload.get("ok", True))
    value: Any = None
    for key, item in payload.items():
        if key not in {"ok", "truncated", "original_chars"}:
            value = item
            break
    if isinstance(value, dict) and value.get("approval_required") is True:
        approval_required = True
    if isinstance(value, (dict, list)):
        try:
            rendered = json.dumps(value, ensure_ascii=False, indent=1, default=str)
        except Exception:
            rendered = str(value)
    else:
        rendered = "" if value is None else str(value)
    status = "approval" if approval_required else ("ok" if ok else "error")
    approval_id = None
    for source in (payload, value if isinstance(value, dict) else {}):
        candidate = source.get("approval_id") if isinstance(source, dict) else None
        if isinstance(candidate, int):
            approval_id = candidate
    return {
        "status": status,
        "preview": safe_ui_text(rendered, TOOL_PREVIEW_LIMIT),
        "truncated": bool(payload.get("truncated")) or len(rendered) > TOOL_PREVIEW_LIMIT,
        "value": value if isinstance(value, dict) else None,
        "approval_id": approval_id,
    }


# Timelines: per-reply steps, tool rows, metrics and receipts survive reopening a chat.
TIMELINE_DIRECTORY = "desktop_timelines"
TIMELINE_RECORD_LIMIT = 400
TIMELINE_RECORD_BYTES = 64_000
TIMELINE_FILE_BYTES = 2_000_000
TIMELINE_FIELDS = ("steps", "tools", "metrics", "status", "reason", "model", "elapsed", "receipt", "versions", "tool_calls", "retryable", "approval_id", "diff")
# The diff has its own budget, outside the record cap: ui_sidepane keeps up to
# MAX_DIFF_TEXT_BYTES (200 KB) of unified text per reply and the sidecar must too.
TIMELINE_DIFF_BYTES = 200_000
DIFF_RECORD_FIELDS = ("version", "root", "summary", "added", "removed", "files_changed", "truncated", "unavailable")
DIFF_ENTRY_FIELDS = ("path", "relative", "kind", "added_lines", "removed_lines", "truncated")


def _timeline_path(data_dir: Any, conversation_id: int) -> Path:
    return Path(str(data_dir or ".")) / TIMELINE_DIRECTORY / f"{int(conversation_id)}.json"


TIMELINE_VERSION = 1


def content_fingerprint(content: Any) -> str:
    import hashlib
    return hashlib.sha1(
        str(content or "").encode("utf-8", "replace"), usedforsecurity=False
    ).hexdigest()[:8]


def bounded_timeline_record(record: Any) -> dict[str, Any]:
    if not isinstance(record, dict):
        return {}
    clean: dict[str, Any] = {"version": TIMELINE_VERSION}
    if record.get("fingerprint"):
        clean["fingerprint"] = str(record["fingerprint"])[:8]
    steps = record.get("steps")
    if isinstance(steps, list):
        clean["steps"] = [compact_activity(step, 140) for step in steps[-60:]]
    tools = record.get("tools")
    if isinstance(tools, list):
        kept = []
        for entry in tools[-TOOL_LOG_LIMIT:]:
            if not isinstance(entry, dict):
                continue
            kept.append({
                "seq": int(entry.get("seq") or 0),
                "name": compact_activity(entry.get("name") or "", 60),
                "headline": compact_activity(entry.get("headline") or "", 96),
                "arguments": {str(k)[:40]: safe_ui_text(v, TOOL_ARGUMENT_LIMIT) for k, v in (entry.get("arguments") or {}).items()} if isinstance(entry.get("arguments"), dict) else {},
                "status": str(entry.get("status") or "ok")[:12],
                "ms": int(entry.get("ms") or 0),
                "preview": safe_ui_text(entry.get("preview") or "", TOOL_PREVIEW_LIMIT),
                "truncated": bool(entry.get("truncated")),
                "paths": [safe_ui_text(p, 300) for p in (entry.get("paths") or [])[:8] if isinstance(p, str)],
                "approval_id": entry.get("approval_id") if isinstance(entry.get("approval_id"), int) else None,
            })
        clean["tools"] = kept
    metrics = record.get("metrics")
    if isinstance(metrics, dict):
        clean["metrics"] = {str(k)[:40]: (v if isinstance(v, (int, float, bool)) or v is None else safe_ui_text(v, 120)) for k, v in list(metrics.items())[:60]}
    for key in ("status", "reason", "model"):
        if record.get(key):
            clean[key] = safe_ui_text(record.get(key), 300)
    if isinstance(record.get("elapsed"), (int, float)):
        clean["elapsed"] = float(record["elapsed"])
    if isinstance(record.get("tool_calls"), int):
        clean["tool_calls"] = int(record["tool_calls"])
    clean["retryable"] = bool(record.get("retryable"))
    receipt = record.get("receipt")
    if isinstance(receipt, dict):
        clean["receipt"] = {
            "action": safe_ui_text(receipt.get("action") or "", 60),
            "fact": {str(k)[:40]: safe_ui_text(v, 200) for k, v in (receipt.get("fact") or {}).items()} if isinstance(receipt.get("fact"), dict) else None,
            "previous": {str(k)[:40]: safe_ui_text(v, 200) for k, v in (receipt.get("previous") or {}).items()} if isinstance(receipt.get("previous"), dict) else None,
            "claim_id": receipt.get("claim_id") if isinstance(receipt.get("claim_id"), int) else None,
            "event_kind": safe_ui_text(receipt.get("event_kind") or "", 60) or None,
        }
    versions = record.get("versions")
    if isinstance(versions, list) and len(versions) > 1:
        clean["versions"] = [safe_ui_text(item, 20_000) for item in versions[-4:] if isinstance(item, str)]
    diff = record.get("diff")
    if isinstance(diff, dict):
        clean["diff"] = bounded_diff_record(diff)  # a fresh copy: the live chip and pane keep theirs
    elif isinstance(diff, str) and diff:
        clean["diff"] = safe_ui_text(diff, 200)
    approval_id = record.get("approval_id")
    if isinstance(approval_id, int) and not isinstance(approval_id, bool) and approval_id > 0:
        clean["approval_id"] = approval_id
    return shrink_timeline_record(clean)


def _record_size(record: dict[str, Any]) -> int:
    try:
        return len(json.dumps(record, ensure_ascii=False).encode("utf-8"))
    except (TypeError, ValueError):
        return TIMELINE_RECORD_BYTES + 1


def _record_size_without_diff(record: dict[str, Any]) -> int:
    return _record_size({key: value for key, value in record.items() if key != "diff"})


def bounded_diff_record(diff: Any, budget: int = TIMELINE_DIFF_BYTES) -> dict[str, Any]:
    """A fresh copy of a DiffReport record with at most ``budget`` bytes of unified
    text in total (matching ui_sidepane.MAX_DIFF_TEXT_BYTES). The caller's dict is
    never touched; entries past the budget lose their text and are marked truncated."""
    if not isinstance(diff, dict):
        return {}

    def scalar(value: Any, limit: int) -> Any:
        if value is None or isinstance(value, (bool, int, float)):
            return value
        return safe_ui_text(value, limit)

    clean: dict[str, Any] = {key: scalar(diff.get(key), 300) for key in DIFF_RECORD_FIELDS if key in diff}
    files = diff.get("files")
    kept: list[dict[str, Any]] = []
    remaining = int(budget)
    truncated_any = False
    for raw in (files if isinstance(files, list) else [])[:1000]:
        if not isinstance(raw, dict):
            continue
        entry: dict[str, Any] = {key: scalar(raw.get(key), 300) for key in DIFF_ENTRY_FIELDS if key in raw}
        unified = raw.get("unified")
        unified = unified if isinstance(unified, str) else ""
        cost = len(unified.encode("utf-8", "replace"))
        if cost > remaining:
            unified = ""
            entry["truncated"] = True
            truncated_any = True
        else:
            remaining -= cost
        entry["unified"] = unified
        kept.append(entry)
    clean["files"] = kept
    if truncated_any:
        clean["truncated"] = True
    return clean


def shrink_timeline_record(record: dict[str, Any], limit: int = TIMELINE_RECORD_BYTES) -> dict[str, Any]:
    """Return a deep copy kept under ``limit`` bytes *excluding* the diff, which
    has its own budget (``TIMELINE_DIFF_BYTES``): tool previews go first, then
    arguments, then earlier versions, then old tool rows. The caller's record is
    never mutated, so the live ``Message.diff`` behind a DiffChip stays intact."""
    record = copy.deepcopy(record)
    if _record_size_without_diff(record) <= limit:
        return record
    for entry in record.get("tools") or []:
        if isinstance(entry, dict):
            entry["preview"] = ""
            entry["truncated"] = True
    if _record_size_without_diff(record) <= limit:
        return record
    for entry in record.get("tools") or []:
        if isinstance(entry, dict):
            entry["arguments"] = {}
    if _record_size_without_diff(record) <= limit:
        return record
    record.pop("versions", None)
    while _record_size_without_diff(record) > limit and record.get("tools"):
        record["tools"] = record["tools"][1:]
    return record


def load_timelines(data_dir: Any, conversation_id: int) -> dict[str, dict[str, Any]]:
    try:
        payload = json.loads(_timeline_path(data_dir, conversation_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(payload, dict):
        return {}
    return {str(key): value for key, value in payload.items() if isinstance(value, dict)}


def _write_json_atomic(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    os.replace(temporary, path)


def save_timeline(data_dir: Any, conversation_id: int, message_id: int, record: Any) -> bool:
    """Merge one reply's timeline into the conversation sidecar (bounded, never
    secrets): ordinary record data ≤ 64 KB, diff text separately ≤ 200 KB,
    the file ≤ 2 MB (oldest ids pruned first),
    written atomically so a crash never loses the history."""
    clean = bounded_timeline_record(record)
    if not clean:
        return False
    path = _timeline_path(data_dir, conversation_id)
    existing = load_timelines(data_dir, conversation_id)
    existing[str(int(message_id))] = clean

    def oldest_first() -> list[str]:
        return sorted(existing, key=lambda item: int(item) if item.isdigit() else 0)

    if len(existing) > TIMELINE_RECORD_LIMIT:
        for key in oldest_first()[: len(existing) - TIMELINE_RECORD_LIMIT]:
            existing.pop(key, None)
    while len(existing) > 1 and _record_size(existing) > TIMELINE_FILE_BYTES:
        existing.pop(oldest_first()[0], None)
    try:
        _write_json_atomic(path, existing)
    except OSError:
        return False
    return True


# Spine events that name the claim a receipt is about. ``claim.superseded`` is
# deliberately absent: an update writes ``claim.created`` (new row) and then
# ``claim.superseded`` (old row), so the newest event names the value that just
# stopped being true.
RECEIPT_EVENT_KINDS = ("claim.created", "claim.reasserted", "proposal.confirmed", "claim.retracted", "claim.tombstoned")
_RECEIPT_EVENT_COLUMNS = "id, kind, subject_id, outcome, created_at"


def _claim_fact(db: Any, claim_id: int | None) -> dict[str, str] | None:
    if claim_id is None:
        return None
    try:
        claim = db.execute("SELECT subject, predicate, value, status FROM memory_claims WHERE id=?", (int(claim_id),)).fetchone()
    except Exception:
        return None
    if claim is None:
        return None
    try:
        return {
            "subject": compact_activity(claim["subject"], 200),
            "predicate": compact_activity(claim["predicate"], 200),
            "value": safe_ui_text(claim["value"], 200),
            "status": compact_activity(claim["status"], 24),
        }
    except (KeyError, IndexError, TypeError):
        return None


def _newest_receipt_event(db: Any, conversation_id: int) -> Any:
    placeholders = ", ".join("?" for _ in RECEIPT_EVENT_KINDS)
    try:
        return db.execute(
            f"""SELECT {_RECEIPT_EVENT_COLUMNS} FROM memory_spine_events
               WHERE conversation_id=? AND subject_kind='claim' AND kind IN ({placeholders})
               ORDER BY id DESC LIMIT 1""",
            (int(conversation_id), *RECEIPT_EVENT_KINDS),
        ).fetchone()
    except Exception:
        return None


def _successor_claim_id(db: Any, claim_id: int) -> int | None:
    try:
        row = db.execute(
            "SELECT id FROM memory_claims WHERE supersedes_id=? ORDER BY id DESC LIMIT 1",
            (int(claim_id),),
        ).fetchone()
    except Exception:
        return None
    return _row_int(row, "id") if row is not None else None


def _receipt_from_store(memory: Any, conversation_id: int) -> dict[str, Any] | None:
    """The newest claim event the spine recorded for this conversation, with the
    claim row it points at — the receipt's fact, read from the store itself.

    When the newest event is ``claim.superseded`` the successor is resolved (the
    claim whose ``supersedes_id`` is the old row, else the newest create/confirm
    event of the conversation) and the receipt carries both: ``fact`` is the
    value now current and ``previous`` the one it replaced, so the chip reads
    ``new (was old)`` instead of naming the value that just stopped being true."""
    db = getattr(memory, "db", None)
    if db is None:
        return None
    try:
        event = db.execute(
            f"""SELECT {_RECEIPT_EVENT_COLUMNS} FROM memory_spine_events
               WHERE conversation_id=? AND subject_kind='claim' ORDER BY id DESC LIMIT 1""",
            (int(conversation_id),),
        ).fetchone()
    except Exception:
        return None
    if event is None:
        return None
    kind = compact_activity(event["kind"], 60)
    claim_id = _row_int(event, "subject_id")
    previous: dict[str, str] | None = None
    if kind == "claim.superseded" and claim_id is not None:
        old_fact = _claim_fact(db, claim_id)
        successor_id = _successor_claim_id(db, claim_id)
        if successor_id is None:
            newest = _newest_receipt_event(db, conversation_id)
            successor_id = _row_int(newest, "subject_id") if newest is not None else None
        if successor_id is not None and successor_id != claim_id:
            previous = old_fact
            claim_id = successor_id
    elif kind not in RECEIPT_EVENT_KINDS:
        newest = _newest_receipt_event(db, conversation_id)
        if newest is not None:
            event = newest
            kind = compact_activity(event["kind"], 60)
            claim_id = _row_int(event, "subject_id")
    fact = _claim_fact(db, claim_id)
    if previous is not None and fact is not None and previous.get("value") == fact.get("value"):
        previous = None
    return {
        "event_id": _row_int(event, "id"),
        "kind": kind,
        "outcome": compact_activity(event["outcome"], 40),
        "created_at": str(event["created_at"] or ""),
        "claim_id": claim_id,
        "fact": fact,
        "previous": previous,
    }


def receipt_summary(receipt: Any) -> str:
    """``subject · predicate = value``, with ``(was old)`` after an update."""
    if not isinstance(receipt, dict):
        return ""
    fact = receipt.get("fact")
    if not isinstance(fact, dict) or not fact.get("subject"):
        return ""
    summary = f"{fact.get('subject')} · {fact.get('predicate')} = {fact.get('value')}"
    previous = receipt.get("previous")
    if isinstance(previous, dict) and previous.get("value") and previous.get("value") != fact.get("value"):
        summary += f" (was {previous.get('value')})"
    return summary


# -- N-6: the context length a local route runs with ------------------------------
REMOTE_MODEL_PREFIXES = ("openai:", "anthropic:", "codex-cli:", "claude-cli:")


def local_context_length(config: Any, model: Any, profile: Any) -> int | None:
    """The ``num_ctx`` the Ollama client sends for this route: the profile's
    configured context length, else the global one (``agent._context_length_for``).
    None for cloud/CLI routes, which do not use the local allocation, and when
    nothing is configured — the pill then shows tokens only, never a guess."""
    name = str(model or "").strip()
    if not name or name.casefold().startswith(REMOTE_MODEL_PREFIXES):
        return None
    key = str(profile or "").strip().lower()
    value = getattr(config, f"{key}_context_length", None) if key else None
    if not value:
        value = getattr(config, "context_length", None)
    try:
        length = int(value)
    except (TypeError, ValueError):
        return None
    return length if length > 0 else None


# -- N-9 / N-11 / N-19 / J-items: small pure helpers behind widgets -------------------
def queue_strip_status(last: str | None, waiting: bool) -> tuple[str, bool]:
    """The queue strip's line while idle: ``(text, warning)``."""
    if waiting:
        return "Waiting for your decision above — send when ready", True
    if last is None:
        return "Queued for this chat — Send now, or ↑ takes it back", False
    if last == "complete":
        return "Queued — sends after the next reply", False
    if last == "cancelled":
        return "Not sent: you stopped the last reply", True
    return f"Not sent: the last reply ended {last.replace('_', ' ')}", True


def reply_footer_actions(status: str, *, error: bool, retryable: bool, is_last: bool, approval_id: int | None) -> dict[str, bool]:
    """Which recovery actions a finished reply offers: Continue only after a stop
    by the operator, Retry only when the agent said the failure is retryable."""
    return {
        "continue": status == "cancelled" and not error,
        "retry": bool(retryable) and is_last and status != "cancelled" and approval_id is None,
    }


def table_markdown(rows: list[list[str]]) -> str:
    """A markdown table from parsed rows (first row is the header)."""
    if not rows:
        return ""
    width = max(len(row) for row in rows)
    normalized = [[str(cell).replace("|", "\\|").replace("\n", " ") for cell in row] + [""] * (width - len(row)) for row in rows]
    lines = ["| " + " | ".join(normalized[0]) + " |", "|" + "|".join(" --- " for _ in range(width)) + "|"]
    for row in normalized[1:]:
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


def fact_dialog_prefill(seed: Any = "", *, subject: str = "", predicate: str = "", value: str = "") -> dict[str, str]:
    """Explicit fields win over the legacy seed split (a multi-word predicate
    can only arrive explicitly)."""
    fields = split_fact_seed(seed)
    for name, given in (("subject", subject), ("predicate", predicate), ("value", value)):
        if given:
            fields[name] = " ".join(str(given).split())
    return fields


def branch_title(base: str) -> str:
    """The title of a branch: the source title itself (the sidebar's ⑂ glyph marks
    the branch), never a stacked ``Branch · Branch ·`` prefix."""
    text = " ".join(str(base or "").split())
    while text.startswith("Branch · "):
        text = text[len("Branch · "):]
    return compact_activity(text or "chat", 120)


def context_task_meta(row: dict[str, Any]) -> str:
    """``status · model · 8h ago`` — every missing field is left out, never ``None``."""
    bits = []
    for value in (row.get("status"), row.get("model"), relative_or_clock(row.get("updated_at"))):
        text = str(value or "").strip()
        if text and text.lower() != "none":
            bits.append(text)
    if row.get("awaiting_approval_id"):
        bits.append(f"needs approval #{row.get('awaiting_approval_id')}")
    return " · ".join(bits)


# Memory kinds the context panel shows: the operator's own facts, explicit memories
# and receipts. Internal learning telemetry (lessons, strategies, calibration
# rows) belongs to the Memory view's diagnostics, not beside the chat.
CONTEXT_HIDDEN_MEMORY_KINDS = frozenset({"lesson", "strategy", "calibration", "ladder", "ladder.candidate", "ladder.grandfathered", "telemetry", "trace", "reflection", "eval"})


def context_memory_rows(rows: Any, limit: int = 6) -> list[dict[str, Any]]:
    kept: list[dict[str, Any]] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        kind = str(row.get("kind") or "memory").strip().lower()
        if kind in CONTEXT_HIDDEN_MEMORY_KINDS or kind.startswith(("lesson", "ladder")):
            continue
        kept.append(row)
        if len(kept) >= limit:
            break
    return kept


PALETTE_GROUP_ORDER = ("Actions", "Chats", "Messages", "Projects", "Model", "Theme")


def order_palette_items(scored: list[tuple[int, int, dict[str, Any]]], extra: list[dict[str, Any]], *, ranked: bool) -> tuple[list[dict[str, Any]], int]:
    """Rank within each group, lay the groups out in ``PALETTE_GROUP_ORDER`` (one
    header each) and preselect the best hit overall. ``scored`` is
    ``(score, original_index, item)`` for the built-in items; ``extra`` holds the
    store's message hits (already ordered)."""
    order = {name: index for index, name in enumerate(PALETTE_GROUP_ORDER)}
    entries: list[tuple[int, int, int, dict[str, Any]]] = []
    for score, index, item in scored[:40]:
        entries.append((order.get(str(item.get("group") or ""), len(order)), -score if ranked else 0, index, item))
    for index, item in enumerate(extra[:20]):
        entries.append((order.get(str(item.get("group") or "Messages"), len(order)), 0, index, item))
    entries.sort(key=lambda entry: (entry[0], entry[1], entry[2]))
    filtered = [entry[3] for entry in entries]
    selected = 0
    if ranked and scored:
        best = max(scored[:40], key=lambda entry: (entry[0], -entry[1]))[2]
        selected = next((index for index, item in enumerate(filtered) if item is best), 0)
    return filtered, selected


LAYOUT_SIDEBAR_MIN = 1100
LAYOUT_PANE_CONTEXT_MIN = 1250
CHAT_COLUMN_MIN = 480
SIDEBAR_WIDTH = 276
CONTEXT_PANEL_WIDTH = 312
READING_COLUMN_MAX = 880


def layout_plan(root_width: int, *, sidebar_wanted: bool, context_wanted: bool, pane_visible: bool, pane_width: int, scale: float = 1.0) -> dict[str, Any]:
    """The space budget for one window width (contract J1): below 1100 px the
    sidebar collapses to ☰; with the side pane open below 1250 px of main area
    the context panel steps aside; the chat column never drops under 480 px, the
    pane shrinking (to 360 px) before it does. Nothing here persists a setting."""
    def px(value: float) -> int:
        return int(round(value * scale))

    sidebar = bool(sidebar_wanted) and root_width >= px(LAYOUT_SIDEBAR_MIN)
    main = root_width - (px(SIDEBAR_WIDTH) + 1 if sidebar else 0)
    context = bool(context_wanted) and not (pane_visible and main < px(LAYOUT_PANE_CONTEXT_MIN))
    pane = int(pane_width) if pane_visible else 0
    if pane_visible:
        room = main - (px(CONTEXT_PANEL_WIDTH) + 1 if context else 0) - px(CHAT_COLUMN_MIN)
        if room < pane:
            context = False
            room = main - px(CHAT_COLUMN_MIN)
            pane = max(px(360), min(int(pane_width), room))
    return {"sidebar": sidebar, "context": context, "pane_width": pane, "main": main}


def reading_column_padding(width: int, scale: float = 1.0) -> int:
    """Side padding that centres a column of at most READING_COLUMN_MAX px."""
    base = int(round(28 * scale))
    limit = int(round(READING_COLUMN_MAX * scale))
    return max(base, (int(width) - limit) // 2)


def composer_footer_plan(width: int, scale: float = 1.0) -> dict[str, bool]:
    """What the composer footer keeps at this chat-column width: the hint line
    goes first, then the model id, before anything could clip."""
    return {"hint": width >= int(round(720 * scale)), "model": width >= int(round(560 * scale))}


def resolve_label_collisions(boxes: list[tuple[str, float, float, float, float]], step: float) -> dict[str, tuple[float, float]]:
    """Seat labels on the council ring: ``(key, x, y, width, height)`` boxes are
    pushed apart vertically (alternating outward) until no two overlap."""
    placed: list[list[Any]] = [[key, x, y, w, h] for key, x, y, w, h in sorted(boxes, key=lambda box: (box[1], box[2]))]
    for _round in range(12):
        moved = False
        for index in range(len(placed)):
            for other in range(index):
                a = placed[index]
                b = placed[other]
                if abs(a[1] - b[1]) < (a[3] + b[3]) / 2 and abs(a[2] - b[2]) < (a[4] + b[4]) / 2:
                    direction = 1.0 if a[2] >= b[2] else -1.0
                    a[2] += direction * step
                    moved = True
        if not moved:
            break
    return {key: (x, y) for key, x, y, _w, _h in placed}


def _proposal_from_store(memory: Any, conversation_id: int) -> dict[str, Any] | None:
    """The fact proposal the store still offers for this conversation (M1 keystone),
    parsed from the store's own command text — the chip that says *Store it*."""
    row = _safe_call(memory, "pending_fact_proposal", int(conversation_id), default=None)
    if not isinstance(row, dict):
        return None
    command = str(row.get("command") or "")
    try:
        fact = parse_explicit_project_fact(command)
    except Exception:
        fact = None
    if not isinstance(fact, dict):
        return None
    return {
        "proposal_id": _row_int(row, "id"),
        "assistant_message_id": _row_int(row, "assistant_message_id"),
        "assisted": bool(row.get("assisted")),
        "fact": {str(key)[:40]: safe_ui_text(value, 200) for key, value in fact.items()},
    }


def _claim_rows(memory: Any, query: str, project_id: int | None) -> list[dict[str, Any]]:
    """Governed facts as the store reports them; only fields the row carries are shown."""
    try:
        rows = memory.current_claims(query, 200, project_id=project_id) if project_id else memory.current_claims(query, 200)
    except TypeError:
        rows = _safe_call(memory, "current_claims", query, default=[]) or []
    except Exception:
        rows = []
    kept: list[dict[str, Any]] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        scope = str(row.get("scope") or "global")
        kept.append({
            "id": int(row.get("claim_id") or row.get("id") or 0),
            "subject": compact_activity(row.get("subject") or "", 120),
            "predicate": compact_activity(row.get("predicate") or "", 120),
            "value": safe_ui_text(row.get("value") or "", 600),
            "scope": "global" if scope == "global" else "project",
            "source": compact_activity(row.get("source") or "", 60),
            "actor": compact_activity(row.get("authority") or row.get("actor") or "", 40),
            "status": compact_activity(row.get("status") or "", 24),
            "created_at": str(row.get("created_at") or row.get("valid_from") or ""),
            "updated_at": str(row.get("updated_at") or ""),
            "confidence": float(row["confidence"]) if isinstance(row.get("confidence"), (int, float)) else None,
            "superseded_count": int(row.get("superseded_count") or (1 if row.get("supersedes_id") else 0)),
        })
    return kept[:200]


def _claim_history_rows(memory: Any, subject: str, predicate: str, project_id: int | None) -> list[dict[str, Any]]:
    versions: list[dict[str, Any]] = []
    for scoped in ((project_id,) if project_id else ()) + (None,):
        try:
            rows = memory.claim_history(subject, predicate, project_id=scoped)
        except Exception:
            rows = []
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            status = str(row.get("status") or "")
            versions.append({
                "value": safe_ui_text(row.get("value") or "", 600),
                "status": "current" if status in {"active", "disputed"} else ("retracted" if status == "retracted" else "superseded"),
                "created_at": str(row.get("created_at") or row.get("valid_from") or ""),
                "actor": compact_activity(row.get("authority") or "", 40),
                "source": compact_activity(row.get("source") or "", 60),
            })
        if versions:
            break
    return versions[:50]


def _routine_payload(memory: Any, config: Any) -> dict[str, Any]:
    jobs = []
    for row in _safe_call(memory, "list_scheduled_jobs", limit=50, default=[]) or []:
        if not isinstance(row, dict):
            continue
        jobs.append({
            "id": int(row.get("id") or 0),
            "name": compact_activity(row.get("name") or "", 120),
            "prompt": safe_ui_text(row.get("prompt") or "", 2_000),
            "interval_minutes": int(row.get("interval_minutes") or 0),
            "next_run_at": str(row.get("next_run_at") or ""),
            "last_run_at": str(row.get("last_run_at") or ""),
            "last_task_id": int(row["last_task_id"]) if isinstance(row.get("last_task_id"), int) else None,
            "enabled": bool(row.get("enabled")),
            "project_id": int(row.get("project_id") or 0) or None,
            "created_at": str(row.get("created_at") or ""),
        })
    tasks: dict[str, dict[str, Any]] = {}
    for row in _safe_call(memory, "list_tasks", limit=80, default=[]) or []:
        if isinstance(row, dict) and row.get("id") is not None:
            tasks[str(row["id"])] = {
                "id": int(row.get("id") or 0),
                "status": compact_activity(row.get("status") or "", 30),
                "updated_at": str(row.get("updated_at") or ""),
                "last_error": compact_activity(row.get("last_error") or "", 200),
                "result": safe_ui_text(row.get("result") or "", 4_000),
            }
    return {"jobs": jobs, "tasks": tasks, "worker_alive": _worker_alive(getattr(config, "data_dir", "."))}


def _grant_rows(memory: Any) -> list[dict[str, Any]]:
    rows = _safe_call(memory, "list_persistent_approvals", limit=100, include_revoked=False, default=[]) or []
    kept = []
    for row in rows:
        kept.append({
            "id": int(row.get("id") or 0),
            "action": compact_activity(row.get("action") or "", 60),
            "resource": safe_ui_text(row.get("resource") or "", 400),
            "reason": compact_activity(row.get("reason") or "", 160),
            "kind": str(row.get("grant_kind") or "always"),
            "scope": compact_activity(row.get("scope") or "", 80),
            "created_at": str(row.get("created_at") or ""),
            "expires_at": str(row.get("expires_at") or ""),
        })
    return kept


def _worker_alive(data_dir: Any, max_age_seconds: float = 120.0) -> bool | None:
    """True when the background worker wrote a recent heartbeat; False when it
    never wrote one or the beat is stale; None only when the file is unreadable
    or malformed (the one case where nothing can be said)."""
    beat = Path(str(data_dir)) / "worker.heartbeat"
    try:
        if not beat.exists():
            return False
        raw = beat.read_text(encoding="utf-8").strip().split(maxsplit=1)
        written = float(raw[0])
    except (OSError, ValueError, IndexError):
        return None
    return (time.time() - written) <= max_age_seconds


def _branch_rows(memory: Any, conversation_id: int, upto_message_id: int | None, upto_count: int) -> list[dict[str, str]]:
    """Transcript rows to copy into a branch: everything up to one message id, or the
    first ``upto_count`` rows when the id is unknown (a live, not yet reloaded turn)."""
    db = getattr(memory, "db", None)
    rows: list[dict[str, str]] = []
    if db is not None:
        try:
            if upto_message_id is not None:
                fetched = db.execute(
                    "SELECT role, content FROM messages WHERE conversation_id=? AND id<=? ORDER BY id ASC LIMIT 2000",
                    (int(conversation_id), int(upto_message_id)),
                ).fetchall()
            else:
                fetched = db.execute(
                    "SELECT role, content FROM messages WHERE conversation_id=? ORDER BY id ASC LIMIT ?",
                    (int(conversation_id), max(0, int(upto_count))),
                ).fetchall()
            rows = [{"role": str(row["role"]), "content": str(row["content"])} for row in fetched]
            if upto_message_id is None:
                rows = rows[: max(0, int(upto_count))]
        except Exception:
            rows = []
    if not rows and upto_message_id is None:
        fallback = _safe_call(memory, "recent_messages", conversation_id, limit=2000, default=[]) or []
        rows = [{"role": str(row.get("role", "assistant")), "content": str(row.get("content", ""))} for row in fallback][: max(0, int(upto_count))]
    return [row for row in rows if row["role"] in {"user", "assistant"}]


def _conversation_title(memory: Any, conversation_id: int) -> str | None:
    """The stored title of one conversation by id (never via the bounded sidebar list)."""
    db = getattr(memory, "db", None)
    if db is not None:
        try:
            row = db.execute("SELECT title FROM conversations WHERE id=?", (int(conversation_id),)).fetchone()
        except Exception:
            row = None
        if row is not None:
            try:
                return str(row["title"] if row["title"] is not None else "")
            except (KeyError, IndexError, TypeError):
                try:
                    return str(row[0] or "")
                except (IndexError, TypeError):
                    return None
    for chat in _chat_rows(memory, limit=2000):
        if chat["id"] == int(conversation_id):
            return chat["title"]
    return None


def _all_conversation_ids(memory: Any) -> set[int] | None:
    """Every conversation id the store holds, or None when that cannot be read."""
    db = getattr(memory, "db", None)
    if db is None:
        return None
    try:
        rows = db.execute("SELECT id FROM conversations").fetchall()
    except Exception:
        return None
    ids: set[int] = set()
    for row in rows:
        value = _row_int(row, "id")
        if value is None:
            try:
                value = int(row[0])
            except (IndexError, TypeError, ValueError):
                continue
        ids.add(value)
    return ids


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


PROJECT_FOLDERS = ("code", "research", "documents", "images", "datasets", "exports")
PROJECT_MANIFEST = ".jarvis-project.json"
WORKSPACE_SKIP_DIRECTORIES = frozenset({
    ".git", ".idea", ".venv", ".vscode", "__pycache__", "node_modules", "target", "data",
})
TEXT_ATTACHMENT_EXTENSIONS = frozenset({
    ".txt", ".md", ".markdown", ".rst", ".csv", ".tsv", ".json", ".jsonl", ".yaml",
    ".yml", ".toml", ".ini", ".cfg", ".xml", ".html", ".htm", ".css", ".js", ".ts",
    ".tsx", ".jsx", ".py", ".pyi", ".rb", ".go", ".rs", ".java", ".kt", ".c", ".h",
    ".cc", ".cpp", ".hpp", ".cs", ".swift", ".sh", ".ps1", ".bat", ".cmd", ".sql",
    ".log", ".env.example", ".gitignore", ".dockerfile", ".php", ".lua", ".r",
})
IMAGE_ATTACHMENT_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif"})
MAX_TEXT_ATTACHMENT_BYTES = 200_000


def _project_rows(memory: Any) -> list[dict[str, Any]]:
    rows = _safe_call(memory, "list_projects", default=None)
    if not isinstance(rows, list):
        return []
    projects: list[dict[str, Any]] = []
    for row in rows:
        try:
            project_id = int(row.get("id"))
        except (TypeError, ValueError, AttributeError):
            continue
        if not bool(row.get("enabled", 1)):
            continue
        projects.append({
            "id": project_id,
            "name": compact_activity(row.get("name") or f"Project {project_id}", 80),
            "relative_path": str(row.get("relative_path") or "."),
            "conversation_count": int(row.get("conversation_count") or 0),
            "task_count": int(row.get("task_count") or 0),
        })
    return projects


def _conversation_project_id(memory: Any, conversation_id: int) -> int:
    project = _safe_call(memory, "conversation_project", int(conversation_id), default=None)
    try:
        return int(project.get("id")) if project else 1
    except (TypeError, ValueError, AttributeError):
        return 1


def _create_project(config: Any, memory: Any, name: str) -> dict[str, Any]:
    """Create an isolated project workspace the same way Presence does."""
    safe_name = " ".join(safe_ui_text(name, 120).split())
    if not safe_name:
        raise ValueError("Project name must not be empty")
    slug = re.sub(r"[^a-z0-9]+", "-", safe_name.casefold()).strip("-")[:60]
    if not slug:
        raise ValueError("Project name must contain a letter or number")
    root, relative = create_project_workspace(config, slug)
    for folder in PROJECT_FOLDERS:
        (root / folder).mkdir(exist_ok=True)
    manifest = root / PROJECT_MANIFEST
    if not manifest.exists():
        manifest.write_text(
            json.dumps({
                "version": 1, "name": safe_name, "kind": "general",
                "description": "", "folders": list(PROJECT_FOLDERS),
            }, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    overview = root / "PROJECT.md"
    if not overview.exists():
        overview.write_text(
            f"# {safe_name}\n\nDedicated project workspace managed by Jarvis.\n",
            encoding="utf-8",
        )
    try:
        project_id = int(memory.add_project(safe_name, relative))
    except Exception:
        # Do not leave an orphan folder that would block the slug forever.
        for folder in PROJECT_FOLDERS:
            try:
                (root / folder).rmdir()
            except OSError:
                pass
        for leftover in (manifest, overview):
            try:
                leftover.unlink()
            except OSError:
                pass
        try:
            root.rmdir()
        except OSError:
            pass
        raise
    return {"id": project_id, "name": safe_name, "relative_path": relative}


def _search_messages(memory: Any, query: str, limit: int = 30) -> list[dict[str, Any]]:
    """Case-insensitive substring search over every operator conversation."""
    text = " ".join(str(query).split())
    db = getattr(memory, "db", None)
    if db is None or len(text) < 2:
        return []
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    pattern = "%" + escaped + "%"
    try:
        rows = db.execute(
            """SELECT m.id, m.conversation_id, m.role, m.content, m.created_at, c.title
               FROM messages AS m JOIN conversations AS c ON c.id = m.conversation_id
               WHERE m.content LIKE ? ESCAPE '\\'
               ORDER BY m.id DESC LIMIT ?""",
            (pattern, max(1, min(int(limit), 100))),
        ).fetchall()
    except Exception:
        return []
    internal = getattr(memory, "is_screen_companion_conversation", None)
    results: list[dict[str, Any]] = []
    for row in rows:
        conversation_id = int(row["conversation_id"])
        if callable(internal):
            try:
                if internal(conversation_id):
                    continue
            except Exception:
                pass
        content = str(row["content"] or "")
        index = content.casefold().find(text.casefold())
        start = max(0, index - 60) if index >= 0 else 0
        snippet = " ".join(content[start:start + 180].split())
        results.append({
            "conversation_id": conversation_id,
            "message_id": _row_int(row, "id"),
            "title": compact_activity(row["title"] or DEFAULT_CHAT_TITLE, 90),
            "role": str(row["role"] or "assistant"),
            "snippet": safe_ui_text(("…" if start else "") + snippet, 200),
            "created_at": str(row["created_at"] or "")[:40],
        })
    return results


def highlight_runs(text: str, query: str, limit: int = 90) -> list[tuple[str, bool]]:
    """Split an excerpt into ``(text, is_hit)`` runs around every case-insensitive
    occurrence of the query, keeping the first hit inside the bounded excerpt."""
    excerpt = " ".join(str(text or "").split())
    needle = " ".join(str(query or "").split())
    if not excerpt:
        return []
    if not needle:
        return [(excerpt[:limit], False)]
    folded = excerpt.casefold()
    first = folded.find(needle.casefold())
    if first > limit // 2 and len(excerpt) > limit:
        cut = max(0, first - limit // 3)
        excerpt = "…" + excerpt[cut:]
        folded = excerpt.casefold()
    if len(excerpt) > limit:
        excerpt = excerpt[: limit - 1] + "…"
        folded = excerpt.casefold()
    runs: list[tuple[str, bool]] = []
    cursor = 0
    key = needle.casefold()
    while True:
        position = folded.find(key, cursor)
        if position < 0:
            break
        if position > cursor:
            runs.append((excerpt[cursor:position], False))
        runs.append((excerpt[position:position + len(needle)], True))
        cursor = position + len(needle)
    if cursor < len(excerpt):
        runs.append((excerpt[cursor:], False))
    return runs


def _workspace_files(root: Path, since_epoch: float = 0.0, limit: int = 40) -> list[dict[str, Any]]:
    """Recently modified regular files under a workspace root, newest first."""
    found: list[dict[str, Any]] = []
    pending = [Path(root)]
    scanned = 0
    while pending and scanned < 4_000:
        directory = pending.pop()
        try:
            entries = list(os.scandir(directory))
        except OSError:
            continue
        for entry in entries:
            scanned += 1
            if scanned > 4_000:
                break
            name = entry.name
            if name.startswith(".") or name.casefold() in WORKSPACE_SKIP_DIRECTORIES:
                continue
            try:
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    pending.append(Path(entry.path))
                    continue
                if not entry.is_file(follow_symlinks=False):
                    continue
                details = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if details.st_mtime < since_epoch:
                continue
            try:
                relative = Path(entry.path).relative_to(root).as_posix()
            except ValueError:
                continue
            found.append({
                "path": str(entry.path),
                "relative": relative,
                "size": int(details.st_size),
                "modified_at": float(details.st_mtime),
            })
    found.sort(key=lambda item: item["modified_at"], reverse=True)
    return found[:limit]


TASK_RESULT_LIMIT = 4_000
TASK_ERROR_LIMIT = 600


def _task_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    try:
        return int(str(value).strip()) if str(value).strip() else None
    except (TypeError, ValueError):
        return None


def _task_rows(memory: Any, limit: int = 30) -> list[dict[str, Any]]:
    """Background tasks as the store reports them: bounded, redacted, detail-ready."""
    rows = _safe_call(memory, "list_tasks", limit=limit, default=None)
    if not isinstance(rows, list):
        return []
    result = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        result.append({
            "id": int(row.get("id") or 0),
            "status": str(row.get("status") or "unknown")[:30],
            "prompt": compact_activity(row.get("prompt") or "", 160),
            "full_prompt": safe_ui_text(row.get("prompt") or "", 6_000),
            "updated_at": str(row.get("updated_at") or "")[:40],
            "created_at": str(row.get("created_at") or "")[:40],
            "model": str(row.get("requested_model") or "auto")[:40],
            "project_id": row.get("project_id"),
            "result": safe_ui_text(row.get("result") or "", TASK_RESULT_LIMIT),
            "last_error": safe_ui_text(row.get("last_error") or "", TASK_ERROR_LIMIT),
            "attempt_count": _task_int(row.get("attempt_count")) or 0,
            "max_attempts": _task_int(row.get("max_attempts")) or 0,
            "awaiting_approval_id": _task_int(row.get("awaiting_approval_id")),
        })
    return result


def _memory_rows(memory: Any, limit: int = 20) -> list[dict[str, Any]]:
    rows = _safe_call(memory, "list_memories", limit=limit, default=None)
    if not isinstance(rows, list):
        return []
    return [
        {
            "created_at": str(row.get("created_at") or "")[:40],
            "kind": str(row.get("kind") or "memory")[:30],
            "content": safe_ui_text(row.get("content") or "", 600),
        }
        for row in rows
    ]


def split_fact_seed(seed: Any) -> dict[str, str]:
    """Where a seed for the remember-fact dialog belongs.

    The Memory view's *Update…* action seeds ``"<subject> <predicate> "`` (a
    trailing space, predicate last): that prefills subject and predicate. Any
    other text is a value seed, as before.
    """
    raw = str(seed or "")
    if raw.endswith(" ") and len(raw.split()) >= 2:
        words = raw.split()
        return {"subject": " ".join(words[:-1]), "predicate": words[-1], "value": ""}
    return {"subject": "", "predicate": "", "value": raw.strip()}


def read_text_attachment(path: str) -> tuple[str, str]:
    """Return ``(name, fenced_block)`` for a text/code file, bounded and redacted."""
    target = Path(path)
    data = target.read_bytes()
    limit_chars = MAX_PROMPT_CHARS // 2 - 400
    text = data[:MAX_TEXT_ATTACHMENT_BYTES].decode("utf-8", errors="replace").replace("\x00", "")
    truncated = len(data) > MAX_TEXT_ATTACHMENT_BYTES or len(text) > limit_chars
    text = text[:limit_chars]
    language = target.suffix.lstrip(".").lower() or "text"
    note = f"\n[… truncated: Jarvis reads at most {limit_chars:,} characters per attached file …]" if truncated else ""
    block = f"File `{target.name}` ({language}):\n```{language}\n{text}{note}\n```"
    return target.name, safe_ui_text(block, MAX_PROMPT_CHARS // 2)


def classify_attachment(path: str) -> str:
    suffix = Path(path).suffix.lower()
    if suffix in IMAGE_ATTACHMENT_EXTENSIONS:
        return "image"
    if suffix in TEXT_ATTACHMENT_EXTENSIONS or suffix == "":
        return "text"
    return "unsupported"


# --------------------------------------------------------------------------
# Off-thread I/O: a small pool whose results arrive as ``ui_job`` session events
# --------------------------------------------------------------------------

_PATH_LINE = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\[^\\/\s]|/|~[\\/])[^\r\n]{1,1000}$")


def looks_like_path(line: str) -> bool:
    """Cheap, filesystem-free test for a pasted line that may be a file path."""
    return bool(_PATH_LINE.match(str(line or "").strip()))


def probe_paths(lines: list[str]) -> list[str]:
    """The subset of lines that are regular files (blocking; run off the Tk thread)."""
    found: list[str] = []
    for line in list(lines)[:64]:
        try:
            if Path(str(line)).is_file():
                found.append(str(line))
        except (OSError, ValueError):
            continue
    return found


def read_attachments(paths: list[str]) -> tuple[list[str], list[str]]:
    """Read text attachments into fenced blocks; returns ``(blocks, errors)``."""
    blocks: list[str] = []
    errors: list[str] = []
    for path in list(paths)[:16]:
        try:
            _name, block = read_text_attachment(str(path))
        except (OSError, ValueError) as exc:
            errors.append(f"Could not read {Path(str(path)).name}: {safe_ui_text(exc, 160)}")
            continue
        blocks.append(block)
    return blocks, errors


def save_clipboard_image(folder: str) -> str | None:
    """Decode the clipboard bitmap and write it as PNG; the path, or None when
    the clipboard holds no image (blocking; run off the Tk thread)."""
    png = clipboard_image_png()
    if not png:
        return None
    target_dir = Path(str(folder or "."))
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"clipboard-{datetime.now():%Y%m%d-%H%M%S-%f}.png"
    target.write_bytes(png)
    return str(target)


def write_text_file(path: str, text: str) -> str:
    Path(str(path)).write_text(str(text), encoding="utf-8")
    return str(path)


def write_bytes_file(path: str, data: bytes) -> str:
    target = Path(str(path))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(bytes(data))
    return str(target)


def build_file_index(root: Any) -> Any:
    """The ``@`` picker's workspace snapshot (bounded, immutable); worker thread only."""
    from . import ui_popups
    return ui_popups.FileIndex.build(str(root or ""))


class UiJobs:
    """Run blocking work on a tiny thread pool and hand the outcome back to the
    Tk thread through the session's event queue as ``ui_job`` events
    (``{"job": name, "token": n, "result": ..., "error": ...}``)."""

    def __init__(self, events: "queue.Queue[SessionEvent]", max_workers: int = 2) -> None:
        self.events = events
        self.pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="jarvis-ui-io")
        self.callbacks: dict[int, Callable[[Any, str | None], None]] = {}
        self._sequence = 0
        self._lock = threading.Lock()
        self.in_flight = 0
        self.closed = False

    def submit(self, name: str, fn: Callable[..., Any], *args: Any, on_done: Callable[[Any, str | None], None] | None = None) -> int:
        with self._lock:
            self._sequence += 1
            token = self._sequence
            if on_done is not None:
                self.callbacks[token] = on_done
            self.in_flight += 1

        def run() -> None:
            result: Any = None
            error: str | None = None
            try:
                result = fn(*args)
            except Exception as exc:  # the job must report, never raise, on the pool
                error = f"{type(exc).__name__}: {safe_ui_text(exc, 300)}"
            with self._lock:
                self.in_flight = max(0, self.in_flight - 1)
            self.events.put(SessionEvent("ui_job", {"job": str(name), "token": token, "result": result, "error": error}))

        try:
            self.pool.submit(run)
        except RuntimeError:  # pool already shut down: report instead of vanishing
            with self._lock:
                self.in_flight = max(0, self.in_flight - 1)
            self.events.put(SessionEvent("ui_job", {"job": str(name), "token": token, "result": None, "error": "RuntimeError: the desktop is closing"}))
        return token

    def dispatch(self, payload: Any) -> bool:
        """Deliver one ``ui_job`` payload to its callback (Tk thread)."""
        if not isinstance(payload, dict):
            return False
        try:
            token = int(payload.get("token") or 0)
        except (TypeError, ValueError):
            return False
        with self._lock:
            callback = self.callbacks.pop(token, None)
        if callback is None:
            return False
        callback(payload.get("result"), payload.get("error"))
        return True

    def shutdown(self) -> None:
        self.closed = True
        try:
            self.pool.shutdown(wait=False, cancel_futures=True)
        except TypeError:  # very old Python without cancel_futures
            self.pool.shutdown(wait=False)


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
        conversation_id: int | None = None,
    ) -> None:
        """Send a prompt; ``conversation_id`` names the chat it was typed for, so a
        chat switch queued in between never routes it into the wrong conversation."""
        self.commands.put(
            ("send", (prompt, model_override_for(model_label), list(attachments or []), conversation_id))
        )

    def new_chat(self, project_id: int | None = None, *, activate: bool = True) -> None:
        """Start a conversation. ``activate=False`` creates it without switching the
        session's current conversation (the companion's own chat): the worker answers
        with ``chat_created`` and the caller submits with ``conversation_id=``."""
        self.commands.put(("new_chat", (project_id, bool(activate))))

    def list_projects(self) -> None:
        self.commands.put(("projects", None))

    def create_project(self, name: str) -> None:
        self.commands.put(("create_project", str(name)))

    def search_messages(self, query: str) -> None:
        self.commands.put(("search_messages", str(query)))

    def request_context(self, since_epoch: float = 0.0) -> None:
        self.commands.put(("context", float(since_epoch)))

    def queue_task(self, prompt: str, model_label: str) -> None:
        self.commands.put(("queue_task", (str(prompt), model_override_for(model_label))))

    def approval_detail(self, approval_id: int) -> None:
        self.commands.put(("approval_detail", int(approval_id)))

    def load_chat(self, conversation_id: int) -> None:
        self.commands.put(("load_chat", int(conversation_id)))

    def load_earlier(self, conversation_id: int, before_id: int) -> None:
        self.commands.put(("load_earlier", (int(conversation_id), int(before_id))))

    def list_chats(self) -> None:
        self.commands.put(("list_chats", None))

    def rename_chat(self, conversation_id: int, title: str) -> None:
        self.commands.put(("rename_chat", (int(conversation_id), str(title))))

    def delete_chat(self, conversation_id: int) -> None:
        self.commands.put(("delete_chat", int(conversation_id)))

    def branch_chat(self, conversation_id: int, upto_message_id: int | None, upto_count: int, title: str) -> None:
        self.commands.put(("branch_chat", (int(conversation_id), upto_message_id, int(upto_count), str(title))))

    def retry_provider(self) -> None:
        self.commands.put(("retry_provider", None))

    def request_approvals(self) -> None:
        self.commands.put(("approvals", None))

    def decide_approval(self, approval_id: int, approve: bool, scope: str = "once") -> None:
        self.commands.put(("decide_approval", (int(approval_id), bool(approve), str(scope or "once"))))

    def request_grants(self) -> None:
        self.commands.put(("grants", None))

    def request_facts(self, query: str = "") -> None:
        self.commands.put(("facts", str(query or "")))

    def request_claim_history(self, subject: str, predicate: str) -> None:
        self.commands.put(("claim_history", (str(subject), str(predicate))))

    def request_routines(self) -> None:
        self.commands.put(("routines", None))

    def add_routine(self, name: str, prompt: str, interval_minutes: int, project_id: int | None = None) -> None:
        self.commands.put(("add_routine", (str(name), str(prompt), int(interval_minutes), project_id)))

    def set_routine_enabled(self, job_id: int, enabled: bool) -> None:
        self.commands.put(("set_routine_enabled", (int(job_id), bool(enabled))))

    def delete_routine(self, job_id: int) -> None:
        self.commands.put(("delete_routine", int(job_id)))

    def revoke_grant(self, grant_id: int) -> None:
        self.commands.put(("revoke_grant", int(grant_id)))

    def request_file_index(self) -> None:
        self.commands.put(("file_index", None))

    def cancel(self) -> None:
        self.cancel_event.set()

    def shutdown(self) -> None:
        self._shutdown.set()
        self.cancel_event.set()
        self.commands.put(("shutdown", None))

    # -- worker thread ----------------------------------------------------

    def save_timeline(self, conversation_id: int, message_id: int, record: dict[str, Any]) -> None:
        self.commands.put(("save_timeline", (int(conversation_id), int(message_id), dict(record or {}))))

    def _on_agent_event(self, message: Any) -> None:
        text = compact_activity(message)
        if text.startswith("governed project memory - "):
            self._memory_action = text[len("governed project memory - "):].strip()
        if text.startswith("tool - ") and getattr(self, "_tool_trace_active", False):
            return  # the traced call reports itself with arguments and a result
        self.emit("activity", text)

    def _traced_tool(self, original: Callable[..., Any], name: str, arguments: Any, conversation_id: int) -> Any:
        """Run one tool call and report what it was given and what it returned.

        The agent runs some tools on a thread pool, so the sequence number and
        the log are guarded by a lock and the entry is built from locals only.
        """
        lock = getattr(self, "_tool_lock", None)
        if lock is None:
            lock = self._tool_lock = threading.Lock()
        with lock:
            counter = getattr(self, "_tool_counter", None)
            if counter is None:
                counter = self._tool_counter = itertools.count(1)
            seq = next(counter)
        workspace = getattr(self, "_workspace_root_path", None)
        entry: dict[str, Any] = {
            "seq": seq,
            "name": compact_activity(name, 60),
            "headline": tool_headline(str(name), arguments),
            "arguments": tool_argument_summary(arguments),
            "status": "running",
            "ms": 0,
            "preview": "",
            "truncated": False,
            "paths": tool_paths(arguments, None, workspace=workspace),
            "approval_id": None,
            "started": time.time(),  # the row's live timer while it runs (not persisted)
        }
        self.emit("step", {"conversation_id": conversation_id, **entry})
        started = time.monotonic()
        try:
            result = original(name, arguments)
        except Exception as exc:
            entry.update({
                "status": "error",
                "ms": int((time.monotonic() - started) * 1000),
                "preview": safe_ui_text(f"{type(exc).__name__}: {exc}", TOOL_PREVIEW_LIMIT),
            })
            self._record_tool(entry, conversation_id)
            raise
        summary = tool_result_summary(result)
        entry.update({
            "status": summary["status"],
            "ms": int((time.monotonic() - started) * 1000),
            "preview": summary["preview"],
            "truncated": summary["truncated"],
            "paths": tool_paths(arguments, summary.get("value"), workspace=workspace),
            "approval_id": summary.get("approval_id"),
        })
        self._record_tool(entry, conversation_id)
        return result

    def _record_tool(self, entry: dict[str, Any], conversation_id: int) -> None:
        lock = getattr(self, "_tool_lock", None)
        if lock is None:
            lock = self._tool_lock = threading.Lock()
        with lock:
            log = getattr(self, "_tool_log", None)
            if log is None:
                log = self._tool_log = []
            if len(log) < TOOL_LOG_LIMIT:
                log.append(dict(entry))
        self.emit("step", {"conversation_id": conversation_id, **entry})

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
        self._memory_action = None
        self.emit("busy", True)
        self.emit("activity", "Preparing request")
        started = time.monotonic()
        workspace_root = self._workspace_root(memory, conversation_id)
        self._workspace_root_path = str(workspace_root)
        workspace_before = self._snapshot_workspace(workspace_root)
        message_id_before = _latest_message_id(memory, conversation_id)
        runtime_guard = RuntimeGuard(memory, self.config, background=False)
        self._tool_lock = threading.Lock()
        self._tool_counter = itertools.count(1)
        self._tool_log = []
        toolbox = getattr(agent, "toolbox", None)
        original_execute = getattr(toolbox, "execute", None)
        self._tool_trace_active = toolbox is not None and callable(original_execute)
        if self._tool_trace_active:
            toolbox.execute = (
                lambda name, arguments, _orig=original_execute, _cid=conversation_id: self._traced_tool(_orig, name, arguments, _cid)
            )

        stream_redactor = StreamingRedactor("[REDACTED]")

        def cancelled() -> bool:
            return self.cancel_event.is_set() or runtime_guard()

        def on_delta(text: str) -> None:
            fragment = safe_ui_text(stream_redactor.feed(text), 20_000)
            if fragment:
                self.emit("delta", {"conversation_id": conversation_id, "text": fragment})

        def finish_stream() -> None:
            fragment = safe_ui_text(stream_redactor.finish(), 20_000)
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
            finish_stream()
            metrics = getattr(result, "metrics", None)
            metrics = dict(metrics) if isinstance(metrics, dict) else {}
            new_message_id = self._new_message_id(memory, conversation_id, message_id_before)
            payload: dict[str, Any] = {
                "conversation_id": conversation_id,
                "content": safe_ui_text(result),
                "status": str(getattr(result, "status", "complete")),
                "reason": safe_ui_text(getattr(result, "reason", "") or "", 1_000),
                "approval_id": getattr(result, "approval_id", None),
                "model": compact_activity(getattr(result, "model", "") or "", 80) or None,
                "tool_calls": int(getattr(result, "tool_calls", 0) or 0),
                "elapsed": round(time.monotonic() - started, 2),
                "metrics": metrics,
                "tools": list(self._tool_log),
                "retryable": bool(getattr(result, "retryable", False)),
                "message_id": new_message_id,
                "diff": self._diff_record(workspace_before, workspace_root),
            }
            # The context pill's denominator: the num_ctx a local route ran with
            # (config per profile, as agent._context_length_for derives it); cloud
            # and CLI routes report tokens only.
            context_length = local_context_length(
                self.config, metrics.get("model") or getattr(result, "model", None), metrics.get("profile"),
            )
            if context_length:
                payload["context_length"] = context_length
            self.emit("assistant", payload)
            action = getattr(self, "_memory_action", None)
            if action:
                # The receipt's fact comes from the store's own newest claim event
                # for this conversation — never from re-parsing the prompt.
                stored = _receipt_from_store(memory, conversation_id)
                self.emit("memory_receipt", {
                    "conversation_id": conversation_id,
                    "message_id": new_message_id,
                    "action": safe_ui_text(action, 60),
                    "fact": stored.get("fact") if stored else None,
                    "previous": stored.get("previous") if stored else None,
                    "claim_id": stored.get("claim_id") if stored else None,
                    "event_kind": stored.get("kind") if stored else None,
                })
            proposal = _proposal_from_store(memory, conversation_id)
            if proposal:
                self.emit("fact_proposal", {"conversation_id": conversation_id, "message_id": new_message_id, **proposal})
        except AgentRunCancelled:
            self.emit("assistant", {
                "conversation_id": conversation_id,
                "content": "",
                "status": "cancelled",
                "reason": "stopped by you",
                "approval_id": None,
                "model": None,
                "elapsed": round(time.monotonic() - started, 2),
                "tools": list(self._tool_log),
                "message_id": self._new_message_id(memory, conversation_id, message_id_before),
                "diff": self._diff_record(workspace_before, workspace_root),
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
                "tools": list(self._tool_log),
                "retryable": True,
                "diff": self._diff_record(workspace_before, workspace_root),
            })
        except Exception as exc:
            self.emit(
                "error",
                {
                    "conversation_id": conversation_id,
                    "message": f"Jarvis could not complete this request ({type(exc).__name__}): {exc}",
                    "diff": self._diff_record(workspace_before, workspace_root),
                },
            )
        finally:
            if self._tool_trace_active and toolbox is not None:
                try:
                    del toolbox.execute
                except AttributeError:
                    pass
            self._tool_trace_active = False

            # Release no raw overlap: on exceptional exits the retained suffix
            # is still passed through the stateful redactor before disposal.
            stream_redactor.finish()
            self.cancel_event.clear()
            self.emit("busy", False)
            self.emit("activity", "Ready")

    def _emit_chats(self, memory: Any) -> None:
        self.emit("chats", _chat_rows(memory))
        ids = _all_conversation_ids(memory)
        if ids is not None:
            self.emit("chat_ids", sorted(ids))

    @staticmethod
    def _message_payloads(rows: list[dict[str, Any]], timelines: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {
                "role": row["role"],
                "content": safe_ui_text(row["content"]),
                "created_at": _iso_to_epoch(row.get("created_at", "")),
                "id": row.get("id"),
                "timeline": timelines.get(str(row.get("id"))) if row.get("id") is not None else None,
            }
            for row in rows
        ]

    def _workspace_root(self, memory: Any, conversation_id: Any) -> Path:
        project_id = _conversation_project_id(memory, conversation_id) if conversation_id is not None else 1
        project = _safe_call(memory, "get_project", project_id, default=None) or {}
        try:
            return resolve_project_workspace(self.config, str(project.get("relative_path") or "."))
        except Exception:
            return Path(str(getattr(self.config, "workspace", ".")))

    @staticmethod
    def _new_message_id(memory: Any, conversation_id: int, before: int | None) -> int | None:
        """The assistant row this turn wrote — None when the agent wrote none, so
        a sidecar record never lands on the previous reply's id."""
        latest = _latest_message_id(memory, conversation_id)
        if latest is None or latest == before:
            return None
        return latest

    @staticmethod
    def _snapshot_workspace(root: Path) -> Any:
        """Index the workspace's text files before a turn (bounded; never raises)."""
        try:
            from . import ui_sidepane
            return ui_sidepane.WorkspaceIndex.snapshot(root)
        except Exception:
            return None

    @staticmethod
    def _diff_record(before: Any, root: Path) -> Any:
        """The turn's workspace diff as a sidecar record; an unavailable index's
        string passes through unchanged; None when the engine is missing."""
        if before is None:
            return None
        try:
            from . import ui_sidepane
            return ui_sidepane.diff_workspace(before, root).to_record()
        except Exception:
            return None

    def _emit_file_index(self, memory: Any, conversation_id: Any) -> None:
        """Snapshot the workspace for the ``@`` picker (bounded, immutable, worker-built)."""
        root = self._workspace_root(memory, conversation_id)
        try:
            index = build_file_index(root)
        except Exception as exc:
            self.emit("file_index", {"root": str(root), "index": None, "error": safe_ui_text(exc, 200)})
            return
        self.emit("file_index", {"root": str(root), "index": index, "error": None})

    def _build_agent(self, memory: Any) -> tuple[Any | None, str | None]:
        """Create the Agent, returning ``(agent, error)`` instead of raising.

        The model provider may be offline when the window opens (Ollama not
        started yet, a CLI provider signed out). The desktop stays usable and
        retries on the next send instead of dying with a modal error.
        """
        try:
            agent = Agent(self.config, memory, self._on_agent_event)
        except OllamaError as exc:
            return None, user_model_error_message(exc)
        except Exception as exc:  # provider wiring problems are recoverable
            return None, f"Model provider unavailable ({type(exc).__name__}): {safe_ui_text(exc, 300)}"
        return agent, None

    @staticmethod
    def _close_agent(agent: Any | None) -> None:
        closer = getattr(getattr(agent, "client", None), "close", None)
        if callable(closer):
            try:
                closer()
            except Exception:
                pass

    def run(self) -> None:
        agent = None
        try:
            with Memory(self.config.data_dir / "jarvis.db") as memory:
                agent, provider_error = self._build_agent(memory)
                conversation_id = memory.new_conversation(DEFAULT_CHAT_TITLE)
                control = memory.control_state()
                self.emit("ready", {
                    "conversation_id": conversation_id,
                    "project_id": _conversation_project_id(memory, conversation_id),
                    "control_state": str(control.get("state", "running")),
                    "fast_model": getattr(self.config, "fast_model", ""),
                    "reasoning_model": getattr(self.config, "reasoning_model", ""),
                    "coding_model": getattr(self.config, "coding_model", ""),
                    "deep_model": getattr(self.config, "deep_model", ""),
                    "chats": _chat_rows(memory),
                    "projects": _project_rows(memory),
                    "workspace": str(getattr(self.config, "workspace", "") or ""),
                    "data_dir": str(getattr(self.config, "data_dir", "") or ""),
                    "provider_error": provider_error,
                })
                untitled = True
                detached: set[int] = set()  # chats created with activate=False (companion)
                all_ids = _all_conversation_ids(memory)
                if all_ids is not None:
                    self.emit("chat_ids", sorted(all_ids))
                self._emit_file_index(memory, conversation_id)

                while not self._shutdown.is_set():
                    command, payload = self.commands.get()
                    if command == "shutdown":
                        break
                    if command == "send":
                        prompt, model_override, attachment_paths, wanted = (tuple(payload) + (None,))[:4]
                        target = int(wanted) if wanted is not None else int(conversation_id)
                        if target != int(conversation_id) and target not in detached:
                            self.emit("send_dropped", {"conversation_id": target, "current": int(conversation_id), "prompt": safe_ui_text(prompt, 2_000)})
                            continue
                        if agent is None:
                            agent, provider_error = self._build_agent(memory)
                            self.emit("provider", {"error": provider_error})
                        if agent is None:
                            self.emit("busy", True)
                            self.emit("assistant", {
                                "conversation_id": target,
                                "content": provider_error or "The model provider is unavailable.",
                                "status": "incomplete",
                                "reason": "model provider unavailable",
                                "approval_id": None,
                                "model": None,
                                "elapsed": 0.0,
                                "retryable": True,
                                "retry_provider": True,
                            })
                            self.emit("busy", False)
                            self.emit("activity", "Ready")
                            continue
                        if target == int(conversation_id):
                            if untitled and _rename_chat(memory, conversation_id, chat_title_from_prompt(prompt)):
                                untitled = False
                                self._emit_chats(memory)
                        elif (_conversation_title(memory, target) or DEFAULT_CHAT_TITLE) == DEFAULT_CHAT_TITLE:
                            # A companion chat is titled from its first prompt like any other.
                            _rename_chat(memory, target, chat_title_from_prompt(prompt))
                        self._run_prompt(
                            agent,
                            memory,
                            target,
                            str(prompt),
                            str(model_override),
                            list(attachment_paths or []),
                        )
                        self._emit_chats(memory)
                        if target == int(conversation_id):
                            self._emit_file_index(memory, conversation_id)
                    elif command == "file_index":
                        self._emit_file_index(memory, conversation_id)
                    elif command == "retry_provider":
                        self._close_agent(agent)
                        agent, provider_error = self._build_agent(memory)
                        self.emit("provider", {"error": provider_error})
                    elif command == "switch_provider":
                        self._close_agent(agent)
                        self.config = payload
                        agent, provider_error = self._build_agent(memory)
                        self.emit("provider_switched", {
                            "error": provider_error,
                            **{f"{key}_model": getattr(self.config, f"{key}_model", "")
                               for key in ("fast", "reasoning", "coding", "deep")},
                        })
                    elif command == "new_chat":
                        project, activate = payload if isinstance(payload, tuple) else (payload, True)
                        created = None
                        if project is not None:
                            try:
                                created = memory.new_conversation(DEFAULT_CHAT_TITLE, project_id=int(project))
                            except (TypeError, ValueError) as exc:
                                self.emit("error", {"message": f"Could not start a chat in that project: {safe_ui_text(exc, 200)}"})
                        if created is None:
                            created = memory.new_conversation(DEFAULT_CHAT_TITLE)
                        if activate:
                            conversation_id = created
                            untitled = True
                            self.emit("new_chat", {
                                "conversation_id": conversation_id,
                                "project_id": _conversation_project_id(memory, conversation_id),
                            })
                        else:
                            # The companion's chat: listed in the sidebar, never switched to.
                            detached.add(int(created))
                            self.emit("chat_created", {
                                "conversation_id": int(created),
                                "project_id": _conversation_project_id(memory, created),
                            })
                        self._emit_chats(memory)
                    elif command == "projects":
                        self.emit("projects", _project_rows(memory))
                    elif command == "create_project":
                        try:
                            created = _create_project(self.config, memory, str(payload))
                        except Exception as exc:
                            self.emit("error", {"message": f"Project was not created: {safe_ui_text(exc, 300)}"})
                        else:
                            self.emit("project_created", created)
                            self.emit("projects", _project_rows(memory))
                    elif command == "search_messages":
                        self.emit("search_results", {
                            "query": str(payload),
                            "results": _search_messages(memory, str(payload)),
                        })
                    elif command == "context":
                        project_id = _conversation_project_id(memory, conversation_id)
                        project = _safe_call(memory, "get_project", project_id, default=None) or {}
                        try:
                            root = resolve_project_workspace(
                                self.config, str(project.get("relative_path") or ".")
                            )
                        except Exception:
                            root = Path(str(getattr(self.config, "workspace", ".")))
                        approvals = [
                            row for row in (_safe_call(memory, "list_approvals", limit=50, default=[]) or [])
                            if row.get("status") == "pending"
                        ]
                        self.emit("context", {
                            "conversation_id": conversation_id,
                            "project_id": project_id,
                            "project_name": compact_activity(project.get("name") or "Default workspace", 80),
                            "workspace": str(root),
                            "files": _workspace_files(root, float(payload or 0.0)),
                            "tasks": _task_rows(memory),
                            "memories": _memory_rows(memory),
                            "pending_approvals": [
                                {
                                    "id": int(row.get("id") or 0),
                                    "action": compact_activity(row.get("action") or "", 60),
                                    "reason": compact_activity(row.get("reason") or "", 200),
                                    "resource": safe_ui_text(row.get("resource") or "", 600),
                                    "scope": compact_activity(row.get("scope") or "", 80),
                                    "expires_at": str(row.get("expires_at") or ""),
                                    "persistent_eligible": bool(row.get("persistent_eligible")),
                                }
                                for row in approvals[:10]
                            ],
                            "worker_alive": _worker_alive(self.config.data_dir),
                            "control_state": str((_safe_call(memory, "control_state", default={}) or {}).get("state", "unknown")),
                        })
                    elif command == "queue_task":
                        prompt_text, model_override = payload
                        try:
                            task_id = memory.add_task(
                                str(prompt_text),
                                project_id=_conversation_project_id(memory, conversation_id),
                                requested_model=None if model_override == "auto" else model_override,
                            )
                        except Exception as exc:
                            self.emit("error", {"message": f"Task was not queued: {safe_ui_text(exc, 300)}"})
                        else:
                            self.emit("task_queued", {"task_id": int(task_id)})
                    elif command == "approval_detail":
                        match = _safe_call(memory, "get_approval", int(payload), default=None)
                        if match is None:
                            rows = _safe_call(memory, "list_approvals", limit=200, default=[]) or []
                            match = next((row for row in rows if int(row.get("id") or 0) == int(payload)), None)
                        self.emit("approval_detail", {"approval_id": int(payload), "approval": dict(match) if match else {"missing": True}})
                    elif command == "load_chat":
                        target = int(payload)
                        exists = _safe_call(memory, "conversation_exists", target, default=True)
                        if not exists:
                            self.emit("error", {"message": "That chat no longer exists."})
                            self._emit_chats(memory)
                            continue
                        conversation_id = target
                        rows, has_more = _chat_messages(memory, target)
                        stored_title = _conversation_title(memory, target)
                        title = compact_activity(stored_title or DEFAULT_CHAT_TITLE, 120)
                        untitled = not (stored_title or "").strip() or (stored_title or "").strip().casefold() in LEGACY_CHAT_TITLES
                        timelines = load_timelines(self.config.data_dir, target)
                        self.emit("chat_loaded", {
                            "conversation_id": target,
                            "project_id": _conversation_project_id(memory, target),
                            "title": title,
                            "has_more": bool(has_more),
                            "messages": self._message_payloads(rows, timelines),
                        })
                    elif command == "load_earlier":
                        target, before_id = payload
                        rows, has_more = _chat_messages(memory, int(target), before_id=int(before_id))
                        timelines = load_timelines(self.config.data_dir, int(target))
                        self.emit("earlier_messages", {
                            "conversation_id": int(target),
                            "before_id": int(before_id),
                            "has_more": bool(has_more),
                            "messages": self._message_payloads(rows, timelines),
                        })
                    elif command == "save_timeline":
                        target, message_id, record = payload
                        save_timeline(self.config.data_dir, int(target), int(message_id), record)
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
                        home_project = _conversation_project_id(memory, target)
                        deleted = _safe_call(memory, "delete_conversation", target, default=None)
                        self.emit("chat_deleted", {"conversation_id": target, "deleted": deleted is not None})
                        if target == conversation_id:
                            try:
                                conversation_id = memory.new_conversation(DEFAULT_CHAT_TITLE, project_id=home_project)
                            except (TypeError, ValueError):
                                conversation_id = memory.new_conversation(DEFAULT_CHAT_TITLE)
                            untitled = True
                            self.emit("new_chat", {
                                "conversation_id": conversation_id,
                                "project_id": _conversation_project_id(memory, conversation_id),
                            })
                        self._emit_chats(memory)
                    elif command == "branch_chat":
                        source, upto_id, upto_count, title = payload
                        try:
                            rows = _branch_rows(memory, int(source), upto_id, int(upto_count))
                            home = _conversation_project_id(memory, int(source))
                            try:
                                new_id = memory.new_conversation(title or DEFAULT_CHAT_TITLE, project_id=home)
                            except (TypeError, ValueError):
                                new_id = memory.new_conversation(title or DEFAULT_CHAT_TITLE)
                            for row in rows:
                                memory.add_message(new_id, row["role"], row["content"])
                        except Exception as exc:
                            self.emit("error", {"message": f"The chat could not be branched: {safe_ui_text(exc, 300)}"})
                        else:
                            self.emit("chat_branched", {"conversation_id": int(new_id), "source": int(source), "copied": len(rows)})
                            self._emit_chats(memory)
                            self.commands.put(("load_chat", int(new_id)))
                    elif command == "approvals":
                        self.emit("approvals", memory.list_approvals(limit=100))
                    elif command == "decide_approval":
                        approval_id, approve, scope = payload
                        changed: Any = False
                        grant_id: int | None = None
                        note = ""
                        try:
                            if approve and scope == "session":
                                grant_id = memory.decide_approval_for_session(
                                    approval_id, ttl_hours=self.config.approval_ttl_hours
                                )
                                changed = grant_id is not None
                                if not changed:
                                    note = "This action is not eligible for a chat-wide grant; approve it once instead."
                            elif approve and scope == "always":
                                grant_id = memory.decide_approval_always(approval_id)
                                changed = grant_id is not None
                                if not changed:
                                    note = "This action is not eligible for a standing grant; approve it once instead."
                            else:
                                changed = memory.decide_approval(
                                    approval_id,
                                    approve,
                                    ttl_hours=self.config.approval_ttl_hours,
                                )
                        except Exception as exc:
                            changed = False
                            note = f"The decision was not recorded: {safe_ui_text(exc, 200)}"
                        decided_payload: dict[str, Any] = {
                            "approval_id": approval_id,
                            "approved": approve,
                            "changed": bool(changed),
                            "scope": scope if approve else "deny",
                            "grant_id": grant_id,
                            "note": note,
                        }
                        if changed and approve and scope == "session":
                            ttl = float(getattr(self.config, "approval_ttl_hours", 24) or 24)
                            decided_payload["until"] = datetime.fromtimestamp(time.time() + ttl * 3600).isoformat(timespec="seconds")
                        if not changed:
                            # Read the row back so the card can say Already decided / Expired truthfully.
                            row = _safe_call(memory, "get_approval", int(approval_id), default=None)
                            if isinstance(row, dict):
                                decided_payload["row"] = {key: (safe_ui_text(value, 600) if isinstance(value, str) else value) for key, value in row.items()}
                                if not note and str(row.get("status") or "") != "pending":
                                    decided_payload["note"] = ""
                        self.emit("approval_decided", decided_payload)
                        self.emit("approvals", memory.list_approvals(limit=100))
                        if grant_id is not None:
                            self.emit("grants", _grant_rows(memory))
                    elif command == "grants":
                        self.emit("grants", _grant_rows(memory))
                    elif command == "facts":
                        project_id = _conversation_project_id(memory, conversation_id)
                        self.emit("facts", {
                            "query": str(payload or ""),
                            "project_id": project_id,
                            "claims": _claim_rows(memory, str(payload or ""), project_id),
                        })
                    elif command == "claim_history":
                        subject, predicate = payload
                        project_id = _conversation_project_id(memory, conversation_id)
                        self.emit("claim_history", {
                            "subject": subject,
                            "predicate": predicate,
                            "versions": _claim_history_rows(memory, subject, predicate, project_id),
                        })
                    elif command == "routines":
                        self.emit("routines", _routine_payload(memory, self.config))
                    elif command == "add_routine":
                        name, prompt_text, interval_minutes, project_id = payload
                        try:
                            memory.add_scheduled_job(name, prompt_text, int(interval_minutes), project_id=project_id)
                        except Exception as exc:
                            self.emit("error", {"message": f"The routine was not created: {safe_ui_text(exc, 300)}"})
                        self.emit("routines", _routine_payload(memory, self.config))
                    elif command == "set_routine_enabled":
                        job_id, enabled = payload
                        changed = _safe_call(memory, "set_scheduled_job_enabled", int(job_id), bool(enabled), default=False)
                        if not changed:
                            self.emit("error", {"message": "That routine could not be updated (it may have been deleted)."})
                        self.emit("routines", _routine_payload(memory, self.config))
                    elif command == "delete_routine":
                        deleted = _safe_call(memory, "delete_scheduled_job", int(payload), default=False)
                        if not deleted:
                            self.emit("error", {"message": "That routine could not be deleted."})
                        self.emit("routines", _routine_payload(memory, self.config))
                    elif command == "revoke_grant":
                        revoked = _safe_call(memory, "revoke_persistent_approval", int(payload), default=False)
                        self.emit("grant_revoked", {"grant_id": int(payload), "revoked": bool(revoked)})
                        self.emit("grants", _grant_rows(memory))
        except Exception as exc:
            self.emit(
                "fatal",
                f"Jarvis Desktop could not start ({type(exc).__name__}): {exc}",
            )
        finally:
            self._close_agent(agent)

    def switch_provider(self, config: Config) -> None:
        self.commands.put(("switch_provider", config))


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
        text="#e6edf3", text_strong="#ffffff", muted="#9aa7b4", faint="#828e9b",
        accent="#3ecfb2", accent_hover="#62dcc3", accent_ink="#04110f", accent_soft="#0f2b27",
        user_bubble="#14202a", code_bg="#0b0f14", code_head="#121820",
        success="#3ecfb2", warning="#f0b84a", danger="#ff6b72", danger_soft="#2b1518",
        info="#8f7bff", selection="#1e3a44",
    ),
    "graphite": Theme(
        key="graphite", name="Graphite", dark=True,
        bg="#212121", panel="#171717", surface="#2a2a2a", surface_alt="#303030",
        surface_hover="#383838", border="#2e2e2e", border_strong="#424242",
        text="#ececec", text_strong="#ffffff", muted="#b5b5b5", faint="#a1a1a1",
        accent="#f2f2f2", accent_hover="#ffffff", accent_ink="#141414", accent_soft="#333333",
        user_bubble="#2f2f2f", code_bg="#0d0d0d", code_head="#1c1c1c",
        success="#7ed9a2", warning="#f0c36a", danger="#ff8080", danger_soft="#3a2323",
        info="#b3a1ff", selection="#3d4a57",
    ),
    "paper": Theme(
        key="paper", name="Paper", dark=False,
        bg="#f6f3ec", panel="#eeeae1", surface="#ffffff", surface_alt="#f3efe6",
        surface_hover="#e9e4d9", border="#e3ded2", border_strong="#cfc8b8",
        text="#2b2620", text_strong="#171310", muted="#5c544b", faint="#6d6457",
        accent="#c2603d", accent_hover="#a94f31", accent_ink="#ffffff", accent_soft="#f7e3da",
        user_bubble="#efe7dc", code_bg="#f4f1ea", code_head="#e8e2d6",
        success="#2f8f5b", warning="#a26f0b", danger="#c0392b", danger_soft="#f8e1de",
        info="#6b52c8", selection="#e2d7c6",
    ),
}
THEME_ORDER = ("midnight", "graphite", "paper")
# Backgrounds that faint text is drawn on; every theme's ``faint`` must reach
# WCAG AA (4.5:1) against each of them (see contrast_ratio and the test).
THEME_TEXT_SURFACES = ("bg", "panel", "surface", "surface_alt", "surface_hover", "code_bg", "code_head", "user_bubble", "accent_soft")
COMBOBOX_STYLE = "Jarvis.TCombobox"


def configure_combobox_style(style: Any, root: Any, theme: Any) -> None:
    """One themed ttk combobox for every dropdown in the desktop (J10): field,
    text, arrow and the popdown listbox follow the theme. ui_panes uses the same
    style name."""
    style.configure(
        COMBOBOX_STYLE,
        fieldbackground=theme.surface, background=theme.surface_alt, foreground=theme.text,
        arrowcolor=theme.muted, bordercolor=theme.border_strong, lightcolor=theme.surface,
        darkcolor=theme.surface, selectbackground=theme.surface, selectforeground=theme.text,
        insertcolor=theme.accent, padding=(6, 3),
    )
    style.map(
        COMBOBOX_STYLE,
        fieldbackground=[("readonly", theme.surface), ("disabled", theme.surface_alt)],
        foreground=[("disabled", theme.faint)],
        arrowcolor=[("active", theme.accent), ("pressed", theme.accent)],
        bordercolor=[("focus", theme.accent)],
    )
    try:
        root.option_add("*TCombobox*Listbox*Background", theme.surface)
        root.option_add("*TCombobox*Listbox*Foreground", theme.text)
        root.option_add("*TCombobox*Listbox*selectBackground", theme.surface_hover)
        root.option_add("*TCombobox*Listbox*selectForeground", theme.text_strong)
    except tk.TclError:
        pass


def relative_luminance(color: str) -> float:
    value = str(color).strip().lstrip("#")
    if len(value) == 3:
        value = "".join(ch * 2 for ch in value)
    try:
        channels = [int(value[i:i + 2], 16) / 255.0 for i in (0, 2, 4)]
    except ValueError:
        return 0.0

    def linear(part: float) -> float:
        return part / 12.92 if part <= 0.03928 else ((part + 0.055) / 1.055) ** 2.4

    red, green, blue = (linear(part) for part in channels)
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def contrast_ratio(first: str, second: str) -> float:
    """WCAG contrast ratio between two hex colours (1.0 … 21.0)."""
    a = relative_luminance(first)
    b = relative_luminance(second)
    high, low = max(a, b), min(a, b)
    return (high + 0.05) / (low + 0.05)


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


CHAT_META_FILE = "desktop_chat_meta.json"
CHAT_META_FIELDS = ("pinned", "archived", "unread", "branched_from")


class ChatMeta:
    """Desktop-owned per-chat flags (pinned, archived, unread, branched_from).

    Kept in a JSON sidecar so the memory schema never changes for a UI feature.
    """

    def __init__(self, data_dir: Path) -> None:
        self.path = Path(data_dir) / CHAT_META_FILE
        self.values: dict[str, dict[str, Any]] = {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                self.values = {str(key): dict(value) for key, value in payload.items() if isinstance(value, dict)}
        except (OSError, ValueError):
            self.values = {}

    def get(self, conversation_id: Any) -> dict[str, Any]:
        return dict(self.values.get(str(conversation_id), {}))

    def flag(self, conversation_id: Any, field: str) -> bool:
        return bool(self.values.get(str(conversation_id), {}).get(field))

    def set(self, conversation_id: Any, **fields: Any) -> None:
        key = str(conversation_id)
        current = dict(self.values.get(key, {}))
        for name, value in fields.items():
            if name not in CHAT_META_FIELDS:
                continue
            if value in (False, None):
                current.pop(name, None)
            else:
                current[name] = value
        if current:
            self.values[key] = current
        else:
            self.values.pop(key, None)
        self._write()

    def remove(self, conversation_id: Any) -> None:
        if self.values.pop(str(conversation_id), None) is not None:
            self._write()

    def prune(self, live_ids: set[int]) -> None:
        stale = [key for key in self.values if not key.isdigit() or int(key) not in live_ids]
        if stale:
            for key in stale:
                self.values.pop(key, None)
            self._write()

    def _write(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.values, indent=1, sort_keys=True), encoding="utf-8")
        except OSError:
            pass


SIDEBAR_TITLE_CHARS = 34
CHAT_FILTERS = ("active", "archived", "all")
CHAT_FILTER_LABELS = {"active": "Active", "archived": "Archived", "all": "All chats"}


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


def _window_handle(root: tk.Misc) -> int:
    """The top-level HWND of a Tk window with 64-bit-safe prototypes (a bare
    ``GetParent`` call truncates handles to 32 bits on x64)."""
    user32 = ctypes.windll.user32
    user32.GetParent.restype = ctypes.c_void_p
    user32.GetParent.argtypes = (ctypes.c_void_p,)
    inner = int(root.winfo_id())
    outer = user32.GetParent(ctypes.c_void_p(inner))
    return int(outer or inner)


def _apply_titlebar_theme(root: tk.Misc, dark: bool) -> None:
    """Ask DWM for a dark (or light) title bar on Windows 10 20H1+ / 11."""
    if sys.platform != "win32":
        return
    try:
        root.update_idletasks()
        hwnd = ctypes.c_void_p(_window_handle(root))
        value = ctypes.c_int(1 if dark else 0)
        for attribute in (20, 19):
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, attribute, ctypes.byref(value), ctypes.sizeof(value)
            ) == 0:
                break
    except Exception:
        pass


class FileDropTarget:
    """Accept Explorer drag-and-drop (WM_DROPFILES) on a Tk window, Windows only.

    Tk has no native drop support without tkdnd, so the Tk toplevel's window
    procedure is subclassed and every message except WM_DROPFILES is passed
    straight through. Any failure leaves the window exactly as it was.
    """

    WM_DROPFILES = 0x0233
    GWLP_WNDPROC = -4

    def __init__(self, root: tk.Misc, callback: Callable[[list[str]], None]) -> None:
        self.root = root
        self.callback = callback
        self.active = False
        self._old_proc = None
        self._proc = None
        if sys.platform != "win32":
            return
        try:
            self._install()
        except Exception:
            self.active = False

    def _install(self) -> None:
        user32 = ctypes.windll.user32
        shell32 = ctypes.windll.shell32
        wintypes = ctypes.wintypes
        proc_type = ctypes.WINFUNCTYPE(
            ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
        )
        user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
        user32.SetWindowLongPtrW.argtypes = (wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t)
        user32.CallWindowProcW.restype = ctypes.c_ssize_t
        user32.CallWindowProcW.argtypes = (
            ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
        )
        shell32.DragQueryFileW.restype = wintypes.UINT
        shell32.DragQueryFileW.argtypes = (wintypes.HANDLE, wintypes.UINT, wintypes.LPWSTR, wintypes.UINT)
        shell32.DragAcceptFiles.argtypes = (wintypes.HWND, wintypes.BOOL)
        shell32.DragFinish.argtypes = (wintypes.HANDLE,)
        self.root.update_idletasks()
        hwnd = int(self.root.winfo_id())
        outer = _window_handle(self.root)

        def handler(window: int, message: int, wparam: int, lparam: int) -> int:
            if message == self.WM_DROPFILES:
                paths: list[str] = []
                try:
                    count = shell32.DragQueryFileW(wparam, 0xFFFFFFFF, None, 0)
                    for index in range(count):
                        length = shell32.DragQueryFileW(wparam, index, None, 0) + 1
                        buffer = ctypes.create_unicode_buffer(length)
                        shell32.DragQueryFileW(wparam, index, buffer, length)
                        if buffer.value:
                            paths.append(buffer.value)
                finally:
                    shell32.DragFinish(wparam)
                if paths:
                    try:
                        self.root.after_idle(lambda: self.callback(paths))
                    except tk.TclError:
                        pass
                return 0
            return user32.CallWindowProcW(self._old_proc, window, message, wparam, lparam)

        self._proc = proc_type(handler)
        previous = user32.SetWindowLongPtrW(hwnd, self.GWLP_WNDPROC, ctypes.cast(self._proc, ctypes.c_void_p).value)
        if not previous:
            raise OSError("SetWindowLongPtrW failed")
        self._old_proc = previous
        shell32.DragAcceptFiles(hwnd, True)
        shell32.DragAcceptFiles(outer, True)
        self.active = True


class GlobalHotkey(threading.Thread):
    """Ctrl+Alt+J summons the window from anywhere (Windows only).

    Hotkeys are delivered to the thread that registered them, so a helper
    thread runs a tiny message loop and hands hits to the Tk thread through a
    queue that ``JarvisDesktop`` already polls.
    """

    MOD_ALT = 0x0001
    MOD_CONTROL = 0x0002
    MOD_SHIFT = 0x0004
    MOD_NOREPEAT = 0x4000
    WM_HOTKEY = 0x0312
    WM_QUIT = 0x0012

    def __init__(self, hits: "queue.Queue[str]", key: int = 0x4A) -> None:
        super().__init__(name="jarvis-desktop-hotkey", daemon=True)
        self.hits = hits
        self.key = key
        self.registered = False
        self.failed = False
        self._thread_id = 0

    def run(self) -> None:
        if sys.platform != "win32":
            self.hits.put("unsupported")
            return
        try:
            user32 = ctypes.windll.user32
            kernel32 = ctypes.windll.kernel32
            wintypes = ctypes.wintypes
            self._thread_id = int(kernel32.GetCurrentThreadId())
            if not user32.RegisterHotKey(None, 1, self.MOD_CONTROL | self.MOD_ALT | self.MOD_NOREPEAT, self.key):
                self.failed = True
                self.hits.put("failed")  # the desktop raises its notice from this event only
                return
            self.registered = True
            self.hits.put("registered")
            message = wintypes.MSG()
            while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
                if message.message == self.WM_HOTKEY:
                    self.hits.put("summon")
            user32.UnregisterHotKey(None, 1)
        except Exception:
            if not self.registered:
                self.failed = True
                self.hits.put("failed")
            self.registered = False

    def stop(self) -> None:
        if sys.platform == "win32" and self._thread_id:
            try:
                ctypes.windll.user32.PostThreadMessageW(self._thread_id, self.WM_QUIT, 0, 0)
            except Exception:
                pass


def encode_png(width: int, height: int, rows: list[bytes], alpha: bool) -> bytes:
    """Minimal PNG writer (8-bit RGB/RGBA, no interlace) built on zlib."""
    def chunk(tag: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + tag + body + struct.pack(">I", zlib.crc32(tag + body) & 0xFFFFFFFF)

    raw = b"".join(b"\x00" + row for row in rows)
    header = struct.pack(">IIBBBBB", width, height, 8, 6 if alpha else 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b"")


def clipboard_image_png() -> bytes | None:
    """Return the clipboard bitmap (CF_DIB) as PNG bytes, or None (Windows only)."""
    if sys.platform != "win32":
        return None
    CF_DIB = 8
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    wintypes = ctypes.wintypes
    # 64-bit handles: without these, GetClipboardData is truncated to 32 bits.
    user32.IsClipboardFormatAvailable.argtypes = (wintypes.UINT,)
    user32.OpenClipboard.argtypes = (wintypes.HWND,)
    user32.GetClipboardData.argtypes = (wintypes.UINT,)
    user32.GetClipboardData.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = (ctypes.c_void_p,)
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = (ctypes.c_void_p,)
    kernel32.GlobalSize.argtypes = (ctypes.c_void_p,)
    kernel32.GlobalSize.restype = ctypes.c_size_t
    if not user32.IsClipboardFormatAvailable(CF_DIB):
        return None
    if not user32.OpenClipboard(None):
        return None
    try:
        handle = user32.GetClipboardData(CF_DIB)
        if not handle:
            return None
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            return None
        try:
            size = int(kernel32.GlobalSize(handle))
            data = ctypes.string_at(pointer, size)
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()
    if len(data) < 40:
        return None
    header_size, width, height, _planes, bits, compression, _image_size = struct.unpack_from("<IiiHHII", data, 0)
    if bits not in (24, 32) or compression not in (0, 3) or width <= 0 or height == 0:
        return None
    if width * abs(height) > 40_000_000:
        return None
    top_down = height < 0
    height = abs(height)
    offset = header_size
    if compression == 3 and header_size == 40:
        offset += 12
    row_bytes = ((width * bits + 31) // 32) * 4
    needed = offset + row_bytes * height
    if len(data) < needed:
        return None
    channels = 4 if bits == 32 else 3
    rows: list[bytes] = []
    order = range(height) if top_down else range(height - 1, -1, -1)
    for index in order:
        start = offset + index * row_bytes
        row = data[start:start + width * channels]
        if bits == 32:
            # BGRA -> RGBA; Windows screenshots often carry alpha 0, treat as opaque.
            pixels = bytearray(width * 4)
            pixels[0::4] = row[2::4]
            pixels[1::4] = row[1::4]
            pixels[2::4] = row[0::4]
            pixels[3::4] = b"\xff" * width
            rows.append(bytes(pixels))
        else:
            pixels = bytearray(width * 3)
            pixels[0::3] = row[2::3]
            pixels[1::3] = row[1::3]
            pixels[2::3] = row[0::3]
            rows.append(bytes(pixels))
    return encode_png(width, height, rows, alpha=(bits == 32))


def presence_is_listening(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=0.35):
            return True
    except OSError:
        return False


def flash_window(root: tk.Misc, count: int = 3) -> None:
    """Flash the taskbar button until the window is focused (Windows only)."""
    if sys.platform != "win32":
        return
    try:
        wintypes = ctypes.wintypes

        class FLASHWINFO(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.UINT), ("hwnd", wintypes.HWND), ("dwFlags", wintypes.DWORD),
                ("uCount", wintypes.UINT), ("dwTimeout", wintypes.DWORD),
            ]

        hwnd = _window_handle(root)
        info = FLASHWINFO(ctypes.sizeof(FLASHWINFO), hwnd, 0x00000003 | 0x0000000C, count, 0)
        ctypes.windll.user32.FlashWindowEx(ctypes.byref(info))
    except Exception:
        pass


def bring_window_forward(root: tk.Misc) -> None:
    try:
        root.deiconify()
        root.lift()
        root.attributes("-topmost", True)
        root.after(120, lambda: root.attributes("-topmost", False))
        root.focus_force()
    except tk.TclError:
        pass
    if sys.platform == "win32":
        try:
            user32 = ctypes.windll.user32
            user32.SetForegroundWindow.argtypes = (ctypes.c_void_p,)
            user32.SetForegroundWindow(ctypes.c_void_p(_window_handle(root)))
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
        if self.window is not None or not str(self.text or "").strip():
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
        self.padx = padx
        self.enabled = True
        self._hover = False
        self._active = False
        label_width = self.font.measure(text) + (self.font.measure(icon + " ") if icon else 0)
        height = self.font.metrics("linespace") + pady * 2
        total = width or (label_width + padx * 2)
        super().__init__(
            master, width=total, height=height, bd=0, highlightthickness=0,
            bg=self._parent_bg(master), cursor="hand2", takefocus=1,
        )
        self._width = total
        self._height = height
        self._focused = False
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)
        self.bind("<Configure>", lambda _event: self.redraw())
        self.bind("<FocusIn>", lambda _event: self._set_focus(True))
        self.bind("<FocusOut>", lambda _event: self._set_focus(False))
        self.bind("<Return>", lambda _event: self.invoke())
        self.bind("<space>", lambda _event: self.invoke())
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
        if self._focused and self.enabled:
            self.create_polygon(
                rounded_points(2, 2, width - 2, height - 2, max(2, self.radius - 1)),
                smooth=True, splinesteps=24, fill="", outline=self.theme.accent, width=2,
            )

    def _set_focus(self, focused: bool) -> None:
        self._focused = bool(focused)
        try:
            self.redraw()
        except tk.TclError:
            pass

    def invoke(self) -> str:
        if self.enabled and self.command is not None:
            self.command()
        return "break"

    def set_text(self, text: str) -> None:
        self.text = text
        self.redraw()

    def set_label(self, text: str) -> None:
        """Change the text and resize the button to fit it (set_text keeps the width)."""
        self.text = text
        label_width = self.font.measure(text) + (self.font.measure(self.icon + " ") if self.icon else 0)
        self._width = label_width + self.padx * 2
        try:
            self.configure(width=self._width)
        except tk.TclError:
            return
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
            width=2, cursor="hand2", padx=2, pady=2, takefocus=1,
            highlightthickness=1, highlightbackground=self.base_bg, highlightcolor=theme.accent,
        )
        self.configure(width=2)
        self.bind("<Enter>", lambda _e: self.configure(bg=theme.surface_hover, fg=theme.text))
        self.bind("<Leave>", lambda _e: self.configure(bg=self.base_bg, fg=self.color))
        self.bind("<Button-1>", lambda _e: self.invoke())
        self.bind("<Return>", lambda _e: self.invoke())
        self.bind("<space>", lambda _e: self.invoke())
        if tooltip:
            Tooltip(self, tooltip, app)
        self.enabled = True

    def set_enabled(self, enabled: bool) -> None:
        """A Label has no working ``state``: disable the command and dim the glyph."""
        self.enabled = bool(enabled)
        try:
            self.configure(fg=self.color if self.enabled else self.app.theme.faint, cursor="hand2" if self.enabled else "arrow")
        except tk.TclError:
            pass

    def invoke(self) -> str:
        if self.command is not None and getattr(self, "enabled", True):
            self.command()
        return "break"


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
        self._bar_check: str | None = None
        self.bar_toggles = 0
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

    BAR_DEAD_BAND = 16

    def _on_scroll(self, first: str, last: str) -> None:
        self.scrollbar.set(first, last)
        self._schedule_bar_check()

    def _schedule_bar_check(self) -> None:
        """Decide the bar's visibility outside the scroll callback, once per idle
        cycle: packing it from inside yscrollcommand narrowed the canvas, the
        texts re-wrapped, the height changed and the bar flipped back — an idle
        loop whenever the content sat near the viewport height."""
        if self._bar_check is not None:
            return
        try:
            self._bar_check = self.after_idle(self._check_bar)
        except tk.TclError:
            self._bar_check = None

    def _check_bar(self) -> None:
        self._bar_check = None
        try:
            if not self.winfo_exists():
                return
            content = int(self.inner.winfo_reqheight())
            visible = int(self.canvas.winfo_height())
            shown = bool(self.scrollbar.winfo_ismapped())
        except tk.TclError:
            return
        if visible <= 1:
            return
        band = self.BAR_DEAD_BAND
        if shown and content <= visible - band:
            self.scrollbar.pack_forget()
            self.bar_toggles += 1
        elif not shown and content > visible + band:
            self.scrollbar.pack(side="right", fill="y")
            self.bar_toggles += 1

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
            self._schedule_bar_check()
        except tk.TclError:
            pass

    def _on_inner_configure(self, _event: Any) -> None:
        self._sync_region()

    def _on_canvas_configure(self, event: Any) -> None:
        self.canvas.itemconfigure(self.window, width=event.width)
        self._sync_region()

    def scroll_to_end(self, light: bool = False) -> None:
        self.stick_to_bottom = True
        if not light:
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
            if self.winfo_width() <= 1:
                # Not laid out yet: counting now wraps every character and
                # requests a giant height that can strand the scroll region.
                return
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


CODE_PREVIEW_LINES = 20


def gutter_text(display_lines: list[int]) -> str:
    """Line numbers for a gutter beside wrapped code: one number per logical
    line, padded with blank display lines so the two columns stay aligned."""
    parts: list[str] = []
    for number, count in enumerate(display_lines, start=1):
        parts.append(str(number) + "\n" * max(0, int(count or 1) - 1))
    return "\n".join(parts)


class CodeText(AutoText):
    """Code block body that keeps a line-number gutter aligned with its wrapping."""

    def __init__(self, master: tk.Misc, app: "JarvisDesktop", *, gutter: AutoText | None = None, **kwargs: Any) -> None:
        self.gutter = gutter
        super().__init__(master, app, **kwargs)

    def display_line_counts(self) -> list[int]:
        try:
            total = int(self.index("end-1c").split(".")[0])
        except (tk.TclError, ValueError):
            return [1]
        counts: list[int] = []
        for line in range(1, total + 1):
            try:
                result = self.count(f"{line}.0", f"{line + 1}.0", "displaylines")
            except tk.TclError:
                result = 1
            value = result[0] if isinstance(result, (tuple, list)) else result
            counts.append(max(1, int(value or 1)))
        return counts

    def fit(self) -> None:
        super().fit()
        gutter = self.gutter
        if gutter is None:
            return
        try:
            if self.winfo_width() <= 1 or not gutter.winfo_exists():
                return
            counts = self.display_line_counts()
            text = gutter_text(counts)
            if gutter.plain_text() != text:
                gutter.configure(state="normal")
                gutter.delete("1.0", "end")
                gutter.insert("1.0", text, ("right",))
                gutter.configure(state="disabled")
            height = int(self.cget("height"))
            if int(gutter.cget("height")) != height:
                gutter.configure(height=height)
        except tk.TclError:
            return


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
    stream_text: str = ""
    approval: dict[str, Any] | None = None
    approval_decision: str | None = None
    reason: str = ""
    receipt: dict[str, Any] | None = None
    versions: list[str] = field(default_factory=list)
    version_index: int = 0
    started_at: float = field(default_factory=time.time)
    retry_provider: bool = False
    tools: list[dict[str, Any]] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    retryable: bool = False
    message_id: int | None = None
    approval_resumable: bool = False
    prompt: str = ""
    diff: Any = None  # ui_sidepane DiffReport record (dict), "unavailable: …" or None
    retried_after: int | None = None  # approval id this turn was re-sent after
    proposal: dict[str, Any] | None = None  # pending fact proposal from the store (M1 chip)
    approval_grant_id: int | None = None  # standing grant created by an Always / This chat decision
    approval_reason_sent: bool = False

    def timeline_record(self) -> dict[str, Any]:
        return {
            "fingerprint": content_fingerprint(self.content),
            "steps": list(self.steps), "tools": list(self.tools), "metrics": dict(self.metrics),
            "status": self.status, "reason": self.reason, "model": self.model, "elapsed": self.elapsed,
            "receipt": self.receipt, "versions": list(self.versions), "tool_calls": self.tool_calls,
            "retryable": self.retryable, "diff": self.diff, "approval_id": self.approval_id,
        }

    def restore_timeline(self, record: Any) -> None:
        clean = bounded_timeline_record(record)
        if not clean:
            return
        expected = clean.get("fingerprint")
        if expected and expected != content_fingerprint(self.content):
            return  # the row changed under the sidecar; show nothing rather than a wrong history
        self.steps = list(clean.get("steps") or [])
        self.tools = list(clean.get("tools") or [])
        self.metrics = dict(clean.get("metrics") or {})
        self.status = str(clean.get("status") or self.status)
        self.reason = str(clean.get("reason") or "")
        self.model = clean.get("model") or self.model
        if clean.get("elapsed") is not None:
            self.elapsed = float(clean["elapsed"])
        self.receipt = clean.get("receipt")
        self.tool_calls = int(clean.get("tool_calls") or len(self.tools))
        self.retryable = bool(clean.get("retryable"))
        self.diff = clean.get("diff")
        approval_id = clean.get("approval_id")
        if isinstance(approval_id, int) and approval_id > 0:
            self.approval_id = approval_id  # the card re-asks the store for the row and its decision
        versions = clean.get("versions") or []
        if versions:
            self.versions = list(versions)
            self.version_index = max(0, len(self.versions) - 1)


def decision_from_row(row: Any) -> str | None:
    """The decided-state line for an approval row read back from the store."""
    if not isinstance(row, dict):
        return None
    status = str(row.get("status") or "").lower()
    when = format_clock(_iso_to_epoch(str(row.get("decided_at") or row.get("updated_at") or ""))) or ""
    if status == "approved":
        return f"Approved once · {when}" if when else "Approved once"
    if status == "denied":
        return f"Denied · {when}" if when else "Denied"
    if status == "expired":
        return "Expired"
    if status and status != "pending":
        return "Already decided"
    return None


def _export_receipt_line(message: "Message") -> str:
    receipt = message.receipt or {}
    if not isinstance(receipt, dict) or not receipt.get("action"):
        return ""
    fact = receipt.get("fact") or {}
    if isinstance(fact, dict) and fact.get("subject"):
        return f"Memory {receipt['action']}: {fact.get('subject')} · {fact.get('predicate')} = {fact.get('value')}"
    return f"Memory {receipt['action']}"


def render_markdown_export(title: str, messages: list["Message"], *, exported_at: datetime | None = None) -> str:
    """The Markdown export: every turn with its time, attachments and receipt."""
    stamp = (exported_at or datetime.now()).strftime("%Y-%m-%d %H:%M")
    lines = [f"# {title}", "", f"_Exported from JARVIS Desktop on {stamp}_", ""]
    for message in messages:
        who = "You" if message.role == "user" else "Jarvis"
        clock = format_clock(message.created_at)
        lines.append(f"## {who}{' · ' + clock if clock else ''}")
        lines.append("")
        if message.attachments:
            lines.append("Attachments: " + ", ".join(f"`{name}`" for name in message.attachments))
            lines.append("")
        lines.append(message.content.strip())
        receipt = _export_receipt_line(message)
        if receipt:
            lines.append("")
            lines.append(f"> {receipt}")
        if message.status == "cancelled":
            lines.append("")
            lines.append("> Stopped by you · partial reply kept")
        lines.append("")
    return "\n".join(lines)


_HTML_CSS = """
:root { color-scheme: light dark; }
body { margin: 0; padding: 32px 16px; background: #f6f3ec; color: #2b2620; font: 15px/1.5 -apple-system, 'Segoe UI', Roboto, sans-serif; }
@media (prefers-color-scheme: dark) { body { background: #07090c; color: #e6edf3; } .turn { background: #12161c; border-color: #2a333d; } .user { background: #14202a; } pre, code, td, th { background: #0b0f14; } th { background: #121820; } .meta { color: #828e9b; } blockquote { border-color: #3ecfb2; color: #9aa7b4; } }
main { max-width: 860px; margin: 0 auto; }
h1.title { font-size: 22px; margin: 0 0 4px; }
.exported { color: #6d6457; font-size: 13px; margin: 0 0 24px; }
.turn { border: 1px solid #e3ded2; border-radius: 12px; padding: 14px 18px; margin: 0 0 14px; background: #ffffff; }
.user { background: #efe7dc; }
.meta { color: #6d6457; font-size: 12px; margin: 0 0 6px; }
.who { font-weight: 600; margin-right: 8px; }
pre { background: #f4f1ea; padding: 12px; border-radius: 8px; overflow-x: auto; font: 13px/1.45 Consolas, 'Cascadia Mono', monospace; }
code { font: 13px Consolas, 'Cascadia Mono', monospace; background: #f4f1ea; padding: 1px 4px; border-radius: 4px; }
pre code { padding: 0; background: transparent; }
table { border-collapse: collapse; margin: 8px 0; max-width: 100%; }
th, td { border: 1px solid #cfc8b8; padding: 6px 10px; text-align: left; vertical-align: top; }
th { background: #e8e2d6; }
blockquote { margin: 8px 0; padding: 4px 12px; border-left: 3px solid #c2603d; color: #5c544b; }
hr { border: 0; border-top: 1px solid #cfc8b8; }
ul, ol { padding-left: 24px; }
.attachments, .receipt, .note { font-size: 13px; color: #6d6457; }
a { color: #c2603d; }
"""


def _html_inline(text: str) -> str:
    import html as _html
    parts: list[str] = []
    for style, run, url in inline_runs(str(text or "")):
        escaped = _html.escape(run, quote=True)
        if style == "code":
            parts.append(f"<code>{escaped}</code>")
        elif style == "bold":
            parts.append(f"<strong>{_html_inline(run) if any(m in run for m in ('`', '_', '*')) else escaped}</strong>")
        elif style == "italic":
            parts.append(f"<em>{escaped}</em>")
        elif style == "strike":
            parts.append(f"<s>{escaped}</s>")
        elif style == "link":
            href = safe_http_url(url)
            parts.append(f'<a href="{_html.escape(href, quote=True)}" rel="noopener noreferrer">{escaped}</a>' if href else escaped)
        else:
            parts.append(escaped)
    return "".join(parts)


def render_html_blocks(markdown: str) -> str:
    """Markdown → HTML with the same block model the cards use; no external resources."""
    import html as _html
    out: list[str] = []
    blocks = parse_markdown(markdown)
    if not blocks and markdown.strip():
        blocks = [{"type": "paragraph", "text": markdown}]
    for block in blocks:
        kind = block.get("type")
        if kind == "code":
            language = re.sub(r"[^a-z0-9+#.-]", "", str(block.get("lang") or "").lower())
            attr = f' class="language-{_html.escape(language, quote=True)}"' if language else ""
            out.append(f"<pre><code{attr}>{_html.escape(str(block.get('text') or ''))}</code></pre>")
        elif kind == "heading":
            level = max(1, min(6, int(block.get("level") or 1))) + 1  # the title is h1
            out.append(f"<h{level}>{_html_inline(block.get('text', ''))}</h{level}>")
        elif kind == "hr":
            out.append("<hr>")
        elif kind == "quote":
            out.append(f"<blockquote>{_html_inline(block.get('text', '')).replace(chr(10), '<br>')}</blockquote>")
        elif kind == "list":
            tag = "ol" if block.get("ordered") else "ul"
            items = []
            for item in block.get("items", []):
                indent = int(item.get("indent") or 0)
                checked = item.get("checked")
                marker = ("☑ " if checked else "☐ ") if checked is not None else ""
                style = f' style="margin-left:{indent * 18}px"' if indent else ""
                items.append(f"<li{style}>{marker}{_html_inline(str(item.get('text', ''))).replace(chr(10), '<br>')}</li>")
            out.append(f"<{tag}>{''.join(items)}</{tag}>")
        elif kind == "table":
            rows = block.get("rows", [])
            if rows:
                columns = max(len(row) for row in rows)
                head = "".join(f"<th>{_html_inline(cell)}</th>" for cell in (rows[0] + [''] * (columns - len(rows[0]))))
                body = "".join("<tr>" + "".join(f"<td>{_html_inline(cell)}</td>" for cell in (row + [''] * (columns - len(row)))) + "</tr>" for row in rows[1:])
                out.append(f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>")
        else:
            out.append(f"<p>{_html_inline(block.get('text', '')).replace(chr(10), '<br>')}</p>")
    return "\n".join(out)


def render_html_export(title: str, messages: list["Message"], *, exported_at: datetime | None = None) -> str:
    """A self-contained HTML page of the chat: inline CSS, no scripts, no external resources."""
    import html as _html
    stamp = (exported_at or datetime.now()).strftime("%Y-%m-%d %H:%M")
    turns: list[str] = []
    for message in messages:
        who = "You" if message.role == "user" else "Jarvis"
        clock = format_clock(message.created_at)
        extra = ""
        if message.attachments:
            extra += "<p class=\"attachments\">Attachments: " + ", ".join(f"<code>{_html.escape(name)}</code>" for name in message.attachments) + "</p>"
        receipt = _export_receipt_line(message)
        if receipt:
            extra += f"<p class=\"receipt\">{_html.escape(receipt)}</p>"
        if message.status == "cancelled":
            extra += "<p class=\"note\">Stopped by you · partial reply kept</p>"
        turns.append(
            f'<section class="turn {"user" if message.role == "user" else "assistant"}">'
            f'<p class="meta"><span class="who">{who}</span>{_html.escape(clock)}</p>'
            f"{render_html_blocks(message.content.strip())}{extra}</section>"
        )
    return (
        "<!doctype html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">"
        f"<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\"><title>{_html.escape(title)}</title>"
        f"<style>{_HTML_CSS}</style></head><body><main>"
        f"<h1 class=\"title\">{_html.escape(title)}</h1><p class=\"exported\">Exported from JARVIS Desktop on {_html.escape(stamp)}</p>"
        + "\n".join(turns) + "</main></body></html>\n"
    )


def format_tokens(value: Any) -> str:
    if not isinstance(value, (int, float)):
        return "?"
    value = int(value)
    if value < 1000:
        return str(value)
    if value < 100_000:
        return f"{value / 1000:.1f}k"
    return f"{value // 1000}k"


def format_k(value: Any, *, coarse: bool = False) -> str:
    """``0.4k`` / ``6.2k`` / ``16k`` (coarse for context windows: ``16k``, ``128k``)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "?"
    number = int(value)
    if coarse:
        return f"{number // 1000}k" if number >= 1000 else str(number)
    if number < 100:
        return str(number)
    return f"{number / 1000:.1f}k"


CONTEXT_LENGTH_KEYS = ("num_ctx", "context_length", "context_window")
CONTEXT_AMBER = 75.0
CONTEXT_RED = 90.0
CONTEXT_LONG_HINT = "Long chat — consider a new one"


def context_pill(metrics: Any) -> tuple[str, str]:
    """The top-bar context pill from a reply's metrics: ``6.2k in · 0.4k out``
    whenever tokens were reported, ``/ 16k (39 %)`` only when the metrics name
    the context length; returns ``(text, level)`` with level ok/amber/red."""
    if not isinstance(metrics, dict):
        return "", "ok"

    def number(key: str) -> int | None:
        value = metrics.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        return int(value)

    tokens_in = number("prompt_tokens")
    tokens_out = number("completion_tokens")
    if tokens_in is None and tokens_out is None:
        return "", "ok"
    text = f"{format_k(tokens_in)} in · {format_k(tokens_out)} out"
    context = next((number(key) for key in CONTEXT_LENGTH_KEYS if number(key)), None)
    level = "ok"
    if context and tokens_in is not None:
        percent = tokens_in / context * 100.0
        text += f" / {format_k(context, coarse=True)} ({percent:.0f} %)"
        level = "red" if percent >= CONTEXT_RED else "amber" if percent >= CONTEXT_AMBER else "ok"
    return text, level


def tool_status_glyph(status: str) -> str:
    return {"running": "⟳", "ok": "✓", "error": "✕", "approval": "⏸"}.get(str(status), "·")


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


MAX_MESSAGE_STEPS = 200
MESSAGE_WINDOW = 40


def message_window_start(total: int, target_index: int | None = None, window: int = MESSAGE_WINDOW) -> int:
    """Index of the first message to build: the newest ``window`` messages, or a
    window that contains the target with a few messages of context above it."""
    start = max(0, int(total) - int(window))
    if target_index is not None and 0 <= int(target_index) < int(total):
        start = min(start, max(0, int(target_index) - 5))
    return start


def render_digest(value: Any) -> str:
    """A cheap fingerprint of the data behind a list so unchanged lists are not rebuilt."""
    import hashlib
    try:
        text = json.dumps(value, sort_keys=True, default=str, ensure_ascii=False)
    except (TypeError, ValueError):
        text = repr(value)
    return hashlib.sha1(text.encode("utf-8", "replace"), usedforsecurity=False).hexdigest()


VIEW_KEYS = ("chat", "council", "memory", "routines")
VIEW_TITLES = {"chat": None, "council": "Council", "memory": "Memory", "routines": "Routines"}
TIMELINE_MODES = ("normal", "verbose", "summary")
TIMELINE_MODE_HINTS = {
    "normal": "Normal — tool calls listed, details on click",
    "verbose": "Verbose — every tool call opened with arguments and results",
    "summary": "Summary — only the one-line 'Worked for…' unless something failed",
}


def approval_expiry_text(expires_at: Any, now: float | None = None) -> str:
    """``expires in 23 min`` / ``expires in 2 h`` / ``expired`` from the store's ``expires_at``."""
    stamp = _iso_to_epoch(str(expires_at or ""))
    if not stamp:
        return ""
    remaining = stamp - (time.time() if now is None else now)
    if remaining <= 0:
        return "expired"
    if remaining < 90:
        return "expires in 1 min"
    if remaining < 3600:
        return f"expires in {int(remaining // 60)} min"
    if remaining < 86400:
        hours = remaining / 3600
        return f"expires in {hours:.0f} h" if hours >= 2 else "expires in 1 h"
    return f"expires in {int(remaining // 86400)} d"


def format_until(stamp: float, now: datetime | None = None) -> str:
    """``14:02`` today, ``tomorrow 14:02``, else ``Sep 05, 14:02``."""
    try:
        moment = datetime.fromtimestamp(stamp)
    except (OverflowError, OSError, ValueError):
        return ""
    current = now or datetime.now()
    delta = (moment.date() - current.date()).days
    if delta <= 0:
        return moment.strftime("%H:%M")
    if delta == 1:
        return "tomorrow " + moment.strftime("%H:%M")
    return moment.strftime("%b %d, %H:%M")


def approval_scope_text(scope: Any) -> str:
    text = str(scope or "").strip()
    if re.fullmatch(r"conversation:[1-9][0-9]*", text):
        return "This chat"
    match = re.fullmatch(r"task:([1-9][0-9]*)", text)
    if match:
        return f"Task #{match.group(1)}"
    return compact_activity(text, 80)


def deny_instruction_text(approval_id: int, action: str, resource: str, instruction: str) -> str:
    """The exact sentence sent when the operator denies with an instruction."""
    short = compact_activity(resource, 80) or compact_activity(action, 40)
    return f"Denied approval #{int(approval_id)}{f' ({short})' if short else ''}. Instead: {instruction.strip()}"


STANDING_UNAVAILABLE_TEXT = "Standing approval isn't available for this action — Jarvis only remembers exact read-only file requests."
APPROVAL_KEY_HINT = "Enter approves · Esc denies · Tab next"
DENY_KEY_HINT = "Enter sends · Esc cancels"
UNSEEN_TARGET_TEXT = "The exact target can't be shown here. Open All approvals to review it, or deny."
MISSING_RECORD_TEXT = "The approval record is no longer available here; open All approvals to review it, or deny."
THIS_CHAT_TOOLTIP = "Allow this exact request again in this chat for 24 hours."
ALWAYS_TOOLTIP = "Allow this exact read-only request every time until you revoke it in Settings → Standing approvals."


class ApprovalCard(tk.Frame):
    """The one approval card (contract §4), reused by the reply card, the
    Approvals window and the context panel. It renders what the store's row says
    and decides nothing itself: every button calls ``on_decide``.

    ``row`` is the store's approval row (``action, reason, resource, scope,
    expires_at, persistent_eligible, status``) or None while it loads;
    ``decision`` is the decided-state line when the approval is no longer
    pending (``Approved once · 14:02`` …).
    """

    def __init__(
        self,
        master: tk.Misc,
        app: "JarvisDesktop",
        row: dict[str, Any] | None,
        *,
        approval_id: int,
        on_decide: Callable[[int, bool, str, str], None],
        decision: str | None = None,
        loading: bool = False,
        missing: bool = False,
        resumable: bool = False,
        on_retry: Callable[[], None] | None = None,
        grant_id: int | None = None,
        on_revoke: Callable[[int], None] | None = None,
        compact: bool = False,
        bg: str | None = None,
    ) -> None:
        theme = app.theme
        self.app = app
        self.theme = theme
        self.approval_id = int(approval_id)
        self.row = dict(row or {})
        self.on_decide = on_decide
        self.decision = decision
        self.compact = compact
        self.bg = bg or theme.accent_soft
        self.approve_button: RoundButton | None = None
        self.deny_button: RoundButton | None = None
        self.deny_box: tk.Frame | None = None
        self.key_hint: tk.Label | None = None
        # The unseen-target rule (every surface): without the store's row, or with
        # an empty resource, nothing here may approve what it cannot show.
        self.unseen_target = bool(missing) or (not loading and not str((row or {}).get("resource") or "").strip())
        super().__init__(master, bg=self.bg, highlightbackground=theme.accent if not decision else theme.border, highlightthickness=1, padx=app.px(8 if compact else 12), pady=app.px(6 if compact else 9))
        fonts = app.fonts
        small = fonts.tiny if compact else fonts.small
        small_bold = fonts.small_bold if compact else fonts.label_bold
        action = compact_activity(self.row.get("action") or "", 60)
        head = tk.Frame(self, bg=self.bg)
        head.pack(fill="x")
        if decision:
            headline = f"Approval #{self.approval_id}" + (f" · {action}" if action else "")
        else:
            bits = ["Needs your approval"]
            if action:
                bits.append(action)
            expiry = approval_expiry_text(self.row.get("expires_at"))
            if expiry:
                bits.append(expiry)
            headline = " · ".join(bits)
        self.headline = tk.Label(head, text=headline, bg=self.bg, fg=theme.danger if "expired" in headline else theme.text_strong, font=small_bold, anchor="w")
        self.headline.pack(side="left", fill="x", expand=True)
        wrap = app.px(230 if compact else 640)
        if loading:
            tk.Label(self, text="Loading the exact target…", bg=self.bg, fg=theme.muted, font=fonts.tiny, anchor="w").pack(fill="x", pady=(4, 0))
        elif missing or (self.unseen_target and not decision):
            text = MISSING_RECORD_TEXT if missing else UNSEEN_TARGET_TEXT
            tk.Label(self, text=text, bg=self.bg, fg=theme.muted, font=fonts.tiny, anchor="w", wraplength=wrap, justify="left").pack(fill="x", pady=(4, 0))
        else:
            resource = safe_ui_text(self.row.get("resource") or "", 4_000)
            if resource:
                if compact:
                    box = tk.Label(self, text=compact_activity(resource, 160), bg=theme.code_bg, fg=theme.text, font=fonts.mono_small, anchor="w", wraplength=wrap, justify="left", padx=6, pady=3)
                    box.pack(fill="x", pady=(4, 0))
                    Tooltip(box, safe_ui_text(resource, 600), app)
                else:
                    box = AutoText(self, app, font=fonts.mono_small, bg=theme.code_bg, fg=theme.text, wrap="char")
                    box.configure(padx=10, pady=8)
                    box.pack(fill="x", pady=(6, 0))
                    box.set_text(resource)
            reason = compact_activity(self.row.get("reason") or "", 300)
            if reason:
                tk.Label(self, text=reason, bg=self.bg, fg=theme.text, font=small, wraplength=wrap, justify="left", anchor="w").pack(fill="x", pady=(4, 0))
            scope = approval_scope_text(self.row.get("scope"))
            if scope:
                tk.Label(self, text=f"Scope: {scope}", bg=self.bg, fg=theme.muted, font=fonts.tiny, anchor="w").pack(fill="x", pady=(3, 0))
        if decision:
            line = tk.Frame(self, bg=self.bg)
            line.pack(fill="x", pady=(6, 0))
            tk.Label(line, text=decision, bg=self.bg, fg=theme.success if decision.startswith(("Approved", "Allowed", "Always")) else theme.muted, font=small_bold, anchor="w").pack(side="left")
            if decision.startswith("Always allowed") and grant_id and on_revoke is not None:
                RoundButton(line, app, "Revoke", lambda: on_revoke(int(grant_id)), kind="ghost", padx=8, pady=2, font=fonts.tiny, tooltip="Withdraw the standing approval (Settings → Standing approvals lists them all)").pack(side="left", padx=(8, 0))
            if resumable and on_retry is not None:
                retry = tk.Frame(self, bg=self.bg)
                retry.pack(fill="x", pady=(8, 0))
                RoundButton(retry, app, "Retry with approval", on_retry, kind="accent", padx=12, pady=5, font=fonts.small_bold, tooltip="Send the same request again as a new turn, now that the action is allowed").pack(side="right")
                tk.Label(retry, text="Jarvis stopped at the approval. Retrying re-sends the same request as a new turn; nothing resumes on its own.", bg=self.bg, fg=theme.muted, font=fonts.tiny, anchor="w", wraplength=app.px(420), justify="left").pack(side="left")
            return
        if loading:
            return
        eligible = bool(self.row.get("persistent_eligible"))
        chat_scoped = bool(re.fullmatch(r"conversation:[1-9][0-9]*", str(self.row.get("scope") or "")))
        actions = tk.Frame(self, bg=self.bg)
        actions.pack(fill="x", pady=(8 if not compact else 5, 0))
        pad = (8, 2) if compact else (12, 5)
        font = fonts.tiny if compact else fonts.small_bold
        self.deny_button = RoundButton(actions, app, "Deny", self.show_deny, kind="ghost", padx=pad[0], pady=pad[1], font=font, tooltip="Deny — optionally tell Jarvis what to do instead (Esc)")
        self.deny_button.pack(side="right")
        if self.unseen_target:
            # Only Deny and the way to the full record: never Approve for an unseen target.
            link = tk.Label(actions, text="All approvals", bg=self.bg, fg=theme.accent, font=fonts.tiny, cursor="hand2", padx=4)
            link.pack(side="left")
            link.bind("<Button-1>", lambda _e: app.show_approvals())
            self.deny_button.bind("<Tab>", lambda _e: (self.app.focus_next_pending_card(self), "break")[1])
            return
        self.approve_button = RoundButton(actions, app, "Approve once", lambda: self._decide(True, "once"), kind="accent", padx=pad[0], pady=pad[1], font=font, tooltip="Allow this exact action one time (Enter)")
        self.approve_button.pack(side="left")
        if eligible and chat_scoped:
            RoundButton(actions, app, "This chat · 24 h", lambda: self._decide(True, "session"), kind="ghost", padx=pad[0] - 2, pady=pad[1], font=font, tooltip=THIS_CHAT_TOOLTIP).pack(side="left", padx=(6, 0))
        if eligible:
            RoundButton(actions, app, "Always", lambda: self._decide(True, "always"), kind="ghost", padx=pad[0] - 2, pady=pad[1], font=font, tooltip=ALWAYS_TOOLTIP).pack(side="left", padx=(6, 0))
        else:
            tk.Label(self, text=STANDING_UNAVAILABLE_TEXT, bg=self.bg, fg=theme.faint, font=fonts.tiny, anchor="w", wraplength=wrap, justify="left").pack(fill="x", pady=(4, 0))
        if not compact:
            self.key_hint = tk.Label(self, text=APPROVAL_KEY_HINT, bg=self.bg, fg=theme.faint, font=fonts.tiny, anchor="w")
            self.key_hint.pack(fill="x", pady=(4, 0))
        for button in (self.approve_button, self.deny_button):
            button.bind("<Escape>", lambda _e: (self.show_deny(), "break")[1])
            button.bind("<Tab>", lambda _e: (self.app.focus_next_pending_card(self), "break")[1])

    def _decide(self, approve: bool, scope: str, reason: str = "") -> None:
        self.on_decide(self.approval_id, approve, scope, reason)

    def show_deny(self) -> None:
        """Reveal the one-line instruction entry: Send · Just deny."""
        if self.deny_box is not None and self.deny_box.winfo_exists() and self.deny_box.winfo_ismapped():
            entry = getattr(self, "deny_entry", None)
            if entry is not None:
                entry.focus_set()
            return
        theme = self.theme
        app = self.app
        box = tk.Frame(self, bg=self.bg)
        box.pack(fill="x", pady=(8, 0))
        self.deny_box = box
        deny_var = tk.StringVar()
        entry = tk.Entry(box, textvariable=deny_var, bg=theme.surface, fg=theme.text, insertbackground=theme.accent, bd=0, highlightthickness=1, highlightbackground=theme.border, highlightcolor=theme.accent, font=app.fonts.small)
        entry.pack(side="left", fill="x", expand=True, ipady=5)
        app._placeholder(entry, deny_var, "Tell Jarvis what to do instead (optional)")
        self.deny_entry = entry

        def instruction() -> str:
            return "" if getattr(entry, "_placeholder_active", False) else deny_var.get()

        RoundButton(box, app, "Just deny", lambda: self._decide(False, "once"), kind="ghost", padx=10, pady=4, font=app.fonts.small_bold).pack(side="right")
        RoundButton(box, app, "Send", lambda: self._decide(False, "once", instruction()), kind="danger", padx=12, pady=4, font=app.fonts.small_bold, tooltip="Deny and send your instruction as the next message").pack(side="right", padx=(8, 6))
        entry.bind("<Return>", lambda _e: (self._decide(False, "once", instruction()), "break")[1])
        entry.bind("<Escape>", lambda _e: (box.pack_forget(), self._set_key_hint(APPROVAL_KEY_HINT), "break")[2])
        entry.bind("<FocusIn>", lambda _e: self._set_key_hint(DENY_KEY_HINT))
        entry.bind("<FocusOut>", lambda _e: self._set_key_hint(APPROVAL_KEY_HINT))
        entry.focus_set()
        try:
            app.root.after(60, app._refit_texts)
        except tk.TclError:
            pass

    def _set_key_hint(self, text: str) -> None:
        """While the deny entry has focus the keys mean send/cancel, and the hint says so."""
        label = self.key_hint
        if label is None:
            return
        try:
            if label.winfo_exists():
                label.configure(text=text)
        except tk.TclError:
            pass

    def focus(self) -> None:  # type: ignore[override]
        button = self.approve_button or self.deny_button
        if button is not None and button.winfo_exists():
            button.focus_set()


class MessageCard(tk.Frame):
    """One conversation turn rendered as a card with header, body and actions."""

    def __init__(self, master: tk.Misc, app: "JarvisDesktop", message: Message) -> None:
        theme = app.theme
        super().__init__(master, bg=theme.bg)
        self.app = app
        self.message = message
        self.theme = theme
        self.body_bg = theme.bg
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
            for name in self.message.attachments[:12]:
                glyph = "🖼" if Path(name).suffix.lower() in IMAGE_ATTACHMENT_EXTENSIONS else "📄"
                tk.Label(
                    chips, text=f"{glyph} {name}", bg=theme.surface_alt, fg=theme.muted,
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
        self._action(actions, "Branch", lambda: app.branch_from(self), tooltip="Start a new chat that contains everything up to this message")
        self._action(actions, "Edit", lambda: app.edit_prompt(self.message.content, self), tooltip="Edit and resend in a branch; this chat stays as it is")

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

    def _action(self, parent: tk.Misc, label: str, command: Callable[[], None], *, tooltip: str = "") -> tk.Label:
        theme = self.theme
        button = tk.Label(
            parent, text=label, bg=theme.bg, fg=theme.faint, font=self.app.fonts.tiny,
            cursor="hand2", padx=6, pady=2, takefocus=1,
            highlightthickness=1, highlightbackground=theme.bg, highlightcolor=theme.accent,
        )
        button.pack(side="right")
        button.bind("<Enter>", lambda _e: button.configure(fg=theme.text, bg=theme.surface_hover))
        button.bind("<Leave>", lambda _e: button.configure(fg=theme.faint, bg=theme.bg))
        button.bind("<Button-1>", lambda _e: command())
        button.bind("<Return>", lambda _e: (command(), "break")[1])
        button.bind("<space>", lambda _e: (command(), "break")[1])
        if tooltip:
            Tooltip(button, tooltip, self.app)
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
        visible = self.message.steps[-4:]
        if not visible and not self.message.tools:
            visible = ["Starting request"]
        running_tool = any(entry.get("status") == "running" for entry in self.message.tools)
        for index, step in enumerate(visible):
            last = index == len(visible) - 1 and not running_tool
            tk.Label(
                self.steps_frame, text=("›  " if last else "·  ") + step,
                bg=theme.bg, fg=theme.text if last else theme.faint,
                font=self.app.fonts.small, anchor="w", justify="left",
            ).pack(fill="x")
        self._render_tool_rows(self.steps_frame, self.message.tools[-8:], expanded=False)

    def _tool_status_color(self, status: str) -> str:
        theme = self.theme
        return {"running": theme.accent, "ok": theme.success, "error": theme.danger, "approval": theme.warning}.get(status, theme.muted)

    def _render_tool_rows(self, parent: tk.Misc, entries: list[dict[str, Any]], *, expanded: bool) -> None:
        """Rows for each tool call; three or more consecutive calls of one tool with the
        same outcome fold into one summary row that expands to the individual calls."""
        index = 0
        total = len(entries)
        while index < total:
            entry = entries[index]
            run_end = index + 1
            while (
                run_end < total
                and entries[run_end].get("name") == entry.get("name")
                and entries[run_end].get("status") == entry.get("status")
                and entry.get("status") != "running"
            ):
                run_end += 1
            if run_end - index >= 3 and not expanded:
                self._folded_tool_rows(parent, entries[index:run_end])
            else:
                for item in entries[index:run_end]:
                    self._tool_row(parent, item, expanded=expanded)
            index = run_end

    def _folded_tool_rows(self, parent: tk.Misc, entries: list[dict[str, Any]]) -> None:
        theme = self.theme
        app = self.app
        status = str(entries[0].get("status") or "ok")
        color = self._tool_status_color(status)
        row = tk.Frame(parent, bg=theme.bg)
        row.pack(fill="x", pady=(1, 0))
        head = tk.Frame(row, bg=theme.bg, cursor="hand2")
        head.pack(fill="x")
        total_ms = sum(int(item.get("ms") or 0) for item in entries)
        glyph = tk.Label(head, text=tool_status_glyph(status), bg=theme.bg, fg=color, font=app.fonts.small_bold, width=2, anchor="w")
        glyph.pack(side="left")
        name = tk.Label(head, text=f"{entries[0].get('name')} ×{len(entries)}", bg=theme.bg, fg=theme.text, font=app.fonts.small_bold, anchor="w")
        name.pack(side="left")
        tk.Label(head, text=format_elapsed(total_ms / 1000) if total_ms >= 1000 else f"{total_ms} ms", bg=theme.bg, fg=theme.faint, font=app.fonts.tiny, anchor="e").pack(side="right")
        detail = tk.Frame(row, bg=theme.bg)

        def toggle(_event: Any = None) -> str:
            if detail.winfo_ismapped():
                detail.pack_forget()
            else:
                if not detail.winfo_children():
                    for item in entries:
                        self._tool_row(detail, item, expanded=False)
                detail.pack(fill="x", padx=(app.px(12), 0))
            app.root.after(60, app._refit_texts)
            return "break"

        for widget in (head, glyph, name):
            widget.bind("<Button-1>", toggle)
        Tooltip(name, "Repeated calls of the same tool — click to list each one", app)

    def _tool_row(self, parent: tk.Misc, entry: dict[str, Any], *, expanded: bool) -> tk.Frame:
        """One tool call: glyph · name · headline · elapsed; click for arguments and result."""
        theme = self.theme
        app = self.app
        status = str(entry.get("status") or "ok")
        color = self._tool_status_color(status)
        row = tk.Frame(parent, bg=theme.bg)
        row.pack(fill="x", pady=(1, 0))
        head = tk.Frame(row, bg=theme.bg, cursor="hand2")
        head.pack(fill="x")
        # ▸ name  headline   ✓ 120 ms — the caret is the expand affordance; the
        # outcome glyph and time sit right after the headline, not across the column.
        caret = tk.Label(head, text="▾" if expanded else "▸", bg=theme.bg, fg=theme.faint, font=app.fonts.small, width=2, anchor="w")
        caret.pack(side="left")
        name = tk.Label(head, text=str(entry.get("name") or "tool"), bg=theme.bg, fg=theme.text, font=app.fonts.small_bold, anchor="w")
        name.pack(side="left")
        headline = str(entry.get("headline") or "")
        detail_label = None
        if headline:
            detail_label = tk.Label(head, text=headline, bg=theme.bg, fg=theme.muted, font=app.fonts.small, anchor="w")
            detail_label.pack(side="left", padx=(8, 0))
        glyph = tk.Label(head, text=tool_status_glyph(status) if status != "running" else "●", bg=theme.bg, fg=color, font=app.fonts.small_bold, anchor="w")
        glyph.pack(side="left", padx=(10, 0))
        elapsed = int(entry.get("ms") or 0)
        right = f"{elapsed} ms" if elapsed < 1000 else format_elapsed(elapsed / 1000)
        if status == "approval":
            right = "needs approval"
        elif status == "error":
            first_line = next((line.strip() for line in str(entry.get("preview") or "").splitlines() if line.strip()), "")
            right = "failed · " + right + (f' · "{compact_activity(first_line, 60)}"' if first_line else "")
        elif status == "running":
            right = f"{max(0.0, time.time() - float(entry.get('started') or time.time())):.1f} s"
        timer = tk.Label(head, text=right, bg=theme.bg, fg=color if status in {"error", "approval"} else theme.faint, font=app.fonts.tiny, anchor="w")
        timer.pack(side="left", padx=(4, 0))
        if status == "running":
            started = float(entry.get("started") or time.time())

            def tick() -> None:
                try:
                    if not timer.winfo_exists() or entry.get("status") != "running":
                        return
                    timer.configure(text=f"{max(0.0, time.time() - started):.1f} s")
                    timer.after(1000, tick)
                except tk.TclError:
                    return

            timer.after(1000, tick)
        detail = tk.Frame(row, bg=theme.bg)
        built = {"done": False}

        def build_detail() -> None:
            if built["done"]:
                return
            built["done"] = True
            box = tk.Frame(detail, bg=theme.code_bg, highlightbackground=theme.border, highlightthickness=1, padx=app.px(10), pady=app.px(6))
            box.pack(fill="x", padx=(app.px(18), 0), pady=(2, 4))
            arguments = entry.get("arguments") or {}
            if arguments:
                tk.Label(box, text="ARGUMENTS", bg=theme.code_bg, fg=theme.faint, font=app.fonts.tiny, anchor="w").pack(fill="x")
                args_text = AutoText(box, app, font=app.fonts.mono_small, bg=theme.code_bg, fg=theme.text, wrap="char")
                args_text.pack(fill="x", pady=(0, 4))
                args_text.set_text("\n".join(f"{key}: {value}" for key, value in arguments.items()))
            preview = str(entry.get("preview") or "")
            preview_lines = preview.splitlines()
            if len(preview_lines) > 12:
                preview = "\n".join(preview_lines[:12]) + "\n…"
            if preview:
                label = "RESULT" + (" (truncated)" if entry.get("truncated") or len(preview_lines) > 12 else "")
                tk.Label(box, text=label, bg=theme.code_bg, fg=theme.faint, font=app.fonts.tiny, anchor="w").pack(fill="x")
                result_text = AutoText(box, app, font=app.fonts.mono_small, bg=theme.code_bg, fg=theme.text, wrap="char")
                result_text.pack(fill="x")
                result_text.set_text(preview)
            paths = [path for path in (entry.get("paths") or []) if isinstance(path, str)]
            if paths:
                links = tk.Frame(box, bg=theme.code_bg)
                links.pack(fill="x", pady=(4, 0))
                for path in paths[:6]:
                    line = tk.Frame(links, bg=theme.code_bg)
                    line.pack(fill="x")
                    link = tk.Label(line, text=compact_activity(path, 80), bg=theme.code_bg, fg=theme.accent, font=app.fonts.tiny, cursor="hand2", anchor="w")
                    link.pack(side="left")
                    link.bind("<Button-1>", lambda _e, target=path: app.open_path(target))
                    link.bind("<Button-3>", lambda event, target=path: app.file_menu(event, {"path": target, "relative": Path(target).name}))
                    Tooltip(link, "Open · right-click to reveal, attach or insert the path", app)
                    view = tk.Label(line, text="View", bg=theme.code_bg, fg=theme.muted, font=app.fonts.tiny, cursor="hand2", padx=6)
                    view.pack(side="left")
                    view.bind("<Button-1>", lambda _e, target=path: app.view_file_in_pane(target))
                    Tooltip(view, "Read it in the side pane (Ctrl+Shift+P)", app)
            if not arguments and not preview and not paths:
                tk.Label(box, text="No arguments or output were reported for this call.", bg=theme.code_bg, fg=theme.faint, font=app.fonts.tiny, anchor="w").pack(fill="x")

        def toggle(_event: Any = None) -> str:
            if detail.winfo_ismapped():
                detail.pack_forget()
                caret.configure(text="▸")
            else:
                build_detail()
                detail.pack(fill="x")
                caret.configure(text="▾")
            app.root.after(60, app._refit_texts)
            return "break"

        for widget in (head, caret, glyph, name, timer) + ((detail_label,) if detail_label is not None else ()):
            widget.bind("<Button-1>", toggle)
        Tooltip(name, "Show what this tool was given and what it returned", app)
        if expanded:
            build_detail()
            detail.pack(fill="x")
        return row

    def _meta_bits(self) -> list[str]:
        message = self.message
        metrics = message.metrics or {}
        bits: list[str] = []
        initial = metrics.get("initial_model")
        final = metrics.get("final_model") or message.model
        if final and initial and str(initial) != str(final):
            bits.append(f"{compact_activity(initial, 40)} → {compact_activity(final, 40)}")
        elif final:
            bits.append(compact_activity(final, 60))
        elapsed = format_elapsed(message.elapsed)
        if elapsed:
            bits.append(elapsed)
        tokens_in = metrics.get("prompt_tokens")
        tokens_out = metrics.get("completion_tokens")
        if isinstance(tokens_in, (int, float)) or isinstance(tokens_out, (int, float)):
            bits.append(f"{format_tokens(tokens_in)} in · {format_tokens(tokens_out)} out")
        elif metrics and metrics.get("token_measurement") not in (None, "", "measured"):
            bits.append("tokens unmeasured")
        ttft = metrics.get("time_to_first_token_ms")
        if isinstance(ttft, (int, float)) and ttft > 0 and metrics.get("streamed", True):
            bits.append(f"first token {format_elapsed(float(ttft) / 1000)}")
        failovers = metrics.get("failovers")
        if isinstance(failovers, int) and failovers > 0:
            bits.append(f"{failovers} failover{'s' if failovers != 1 else ''}")
        return bits

    def _metrics_tooltip(self) -> str:
        metrics = self.message.metrics or {}
        if not metrics:
            return "Timing comes from the agent's own run metrics."
        lines = []
        for key in ("profile", "provider", "initial_model", "final_model", "model_calls", "retries", "failure_kind", "prompt_tokens", "completion_tokens", "token_measurement", "context_chars", "time_to_first_token_ms", "model_latency_ms", "agent_total_ms", "trace_id"):
            value = metrics.get(key)
            if value not in (None, "", 0, False):
                lines.append(f"{key.replace('_', ' ')}: {value}")
        return "\n".join(lines) if lines else "No run metrics were reported."

    def _start_pulse(self) -> None:
        self._pulse_step = 0
        self._tick_pulse()

    def _tick_pulse(self) -> None:
        if not self.message.working or self.pulse is None:
            return
        try:
            if not self.winfo_exists():
                return
        except tk.TclError:
            return
        frames = ("●  ", " ● ", "  ●", " ● ")
        elapsed = max(0.0, time.time() - self.message.started_at)
        last_step = self.message.steps[-1].lower() if self.message.steps else ""
        phase = "Loading model" if "warming" in last_step or "loading" in last_step else "Working"
        try:
            self.pulse.configure(text=f"{frames[self._pulse_step % len(frames)]} {phase} · {format_elapsed(elapsed)}")
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
        """Stream text in; re-render as markdown at most every 220 ms."""
        self.message.streaming = True
        self.message.stream_text += str(text)
        if self.stream_text is None:
            theme = self.theme
            self.stream_text = AutoText(self.body, self.app, font=self.app.fonts.body, bg=theme.bg, fg=theme.text)
            self.stream_text.pack(fill="x", pady=(0, 4))
        if getattr(self, "_stream_after", None) is None:
            self._stream_after = self.after(400, self._render_stream)

    def _render_stream(self) -> None:
        self._stream_after = None
        if not self.message.streaming or not self.winfo_exists():
            return
        text = self.message.stream_text
        # Rebuild widgets only while the reply is small; long replies stream as
        # plain text and get their final markdown once, when the reply lands.
        rich = len(text) < 6_000 and any(marker in text for marker in ("```", "\n#", "\n- ", "\n* ", "\n1. ", "\n|", "**"))
        if rich:
            for child in self.body.winfo_children():
                if child is not self.steps_frame:
                    child.destroy()
            self.stream_text = None
            self._render_markdown(text)
        elif self.stream_text is not None:
            self.stream_text.set_text(text)
        if self.app.messages_view.stick_to_bottom:
            self.app.messages_view.scroll_to_end(light=True)

    # final rendering -----------------------------------------------------

    def render_final(self) -> None:
        theme = self.theme
        app = self.app
        message = self.message
        message.working = False
        message.streaming = False
        self._stop_pulse()
        self._clear_body()
        if message.steps or message.tools:
            self.steps_frame.pack(fill="x", pady=(0, app.px(4)))
            for child in self.steps_frame.winfo_children():
                child.destroy()
            tool_count = len(message.tools) or message.tool_calls
            failed = sum(1 for entry in message.tools if entry.get("status") == "error")
            summary = f"Worked for {format_elapsed(message.elapsed)}" if message.elapsed else "Worked"
            if message.steps:
                summary += f" · {len(message.steps)} step{'s' if len(message.steps) != 1 else ''}"
            if tool_count:
                summary += f" · {tool_count} tool call{'s' if tool_count != 1 else ''}"
            if failed:
                summary += f" · {failed} failed"
            mode = getattr(app, "timeline_mode", "normal")
            toggle = tk.Label(
                self.steps_frame, text=f"▸ {summary}", bg=theme.bg, fg=theme.danger if failed else theme.faint,
                font=app.fonts.tiny, anchor="w", cursor="hand2",
            )
            toggle.pack(fill="x")
            detail = tk.Frame(self.steps_frame, bg=theme.bg)

            def open_detail(verbose: bool) -> None:
                for child in detail.winfo_children():
                    child.destroy()
                for step in message.steps[-40:]:
                    tk.Label(detail, text=f"·  {step}", bg=theme.bg, fg=theme.faint, font=app.fonts.tiny, anchor="w", justify="left").pack(fill="x")
                self._render_tool_rows(detail, message.tools[-TOOL_LOG_LIMIT:], expanded=verbose)
                trace_id = str((message.metrics or {}).get("trace_id") or "")
                if trace_id:
                    trace_row = tk.Frame(detail, bg=theme.bg)
                    trace_row.pack(fill="x", pady=(2, 0))
                    tk.Label(trace_row, text=f"trace id {compact_activity(trace_id, 40)}", bg=theme.bg, fg=theme.faint, font=app.fonts.tiny, anchor="w").pack(side="left")
                    copy_trace = tk.Label(trace_row, text="Copy", bg=theme.bg, fg=theme.muted, font=app.fonts.tiny, cursor="hand2", padx=6)
                    copy_trace.pack(side="left")
                    copy_trace.bind("<Button-1>", lambda _e, value=trace_id: app.copy_text(value, quiet=True))
                detail.pack(fill="x", padx=(app.px(10), 0))
                toggle.configure(text=f"▾ {summary}")

            def toggle_steps(_event: Any = None) -> None:
                if detail.winfo_ismapped():
                    detail.pack_forget()
                    toggle.configure(text=f"▸ {summary}")
                else:
                    open_detail(getattr(app, "timeline_mode", "normal") == "verbose")
                app.root.after(80, app._refit_texts)
            toggle.bind("<Button-1>", toggle_steps)
            Tooltip(toggle, "What Jarvis did during this reply — every tool call with its arguments and result (Ctrl+O changes how much is shown)", app)
            if mode == "verbose" or (failed and mode != "summary"):
                open_detail(mode == "verbose")
        else:
            self.steps_frame.pack_forget()
        if message.error or message.retry_provider:
            if message.retry_provider:
                self._render_notice(message.content, kind="danger", title="Model provider offline", actions=[("Reconnect", app.retry_provider, "accent")])
            else:
                kind_text = self.humanise_failure((message.metrics or {}).get("failure_kind"))
                title = "Couldn't finish" + (f" · {kind_text}" if kind_text else "")
                actions: list[tuple[str, Callable[[], None], str]] = []
                if message.retryable and app.is_last_card(self):
                    actions.append(("Retry", lambda: app.regenerate_card(self), "accent"))
                actions.append(("Copy details", self._copy_error_details, "ghost"))
                self._render_notice(message.content, kind="danger", title=title, actions=actions)
        elif message.status == "cancelled":
            if message.content:
                self._render_markdown(message.content)
            self._render_notice("", kind="warning", title="Stopped by you · partial reply kept")
        else:
            self._render_markdown(message.content)
        if message.receipt:
            self._render_receipt(message.receipt)
        if message.proposal:
            self._render_proposal(message.proposal)
        if message.approval_id is not None:
            self._render_approval(message.approval_id)
            if not message.approval_decision and message.status not in {"complete", "cancelled"}:
                self.waiting_label = tk.Label(self.body, text="Waiting for your approval above", bg=self.body_bg, fg=theme.warning, font=app.fonts.small_bold, anchor="w")
                self.waiting_label.pack(fill="x", pady=(0, 6))
        elif message.status not in {"complete", "cancelled"} and not message.error and not message.retry_provider:
            reason = message.reason or message.status.replace("_", " ")
            actions = [("Retry", lambda: app.regenerate_card(self), "accent")] if (message.retryable and app.is_last_card(self)) else []
            actions.append(("Copy details", self._copy_error_details, "ghost"))
            self._render_notice("", kind="warning", title=f"Stopped early: {compact_activity(reason, 200)}", actions=actions)
        meta_bits = self._meta_bits()
        if message.retried_after:
            meta_bits.append(f"Retried after approval #{message.retried_after}")
        if self.meta_label is not None:
            self.meta_label.configure(text="  ·  ".join([format_clock(message.created_at), *meta_bits]))
            tooltip = getattr(self, "_meta_tooltip", None)
            if tooltip is None:
                self._meta_tooltip = Tooltip(self.meta_label, self._metrics_tooltip(), app)
            else:
                tooltip.text = self._metrics_tooltip()
        self._action(self.footer, "Copy", lambda: app.copy_text(message.content))
        if not message.retry_provider:
            if app.is_last_card(self):
                regenerate = self._action(self.footer, "Regenerate ▾", lambda: app.regenerate_menu(self, regenerate), tooltip="Regenerate this reply, optionally with another model (Ctrl+R)")
            else:
                self._action(self.footer, "Branch from here", lambda: app.branch_from(self), tooltip="Start a new chat that contains everything up to this reply")
        if reply_footer_actions(message.status, error=message.error, retryable=message.retryable, is_last=app.is_last_card(self), approval_id=message.approval_id)["continue"]:
            self._action(self.footer, "Continue", app.continue_last)
        self._action(self.footer, "Quote", lambda: app.quote_text(message.content))
        if len(message.versions) > 1:
            self._render_version_pager()
        self._render_diff_chip()
        self._render_reply_artifact()

    def _render_diff_chip(self) -> None:
        """``+N −M`` in the footer when the turn changed workspace files (side pane Diff tab)."""
        record = self.message.diff
        if not isinstance(record, dict) or int(record.get("files_changed") or 0) <= 0:
            return
        app = self.app
        try:
            from . import ui_sidepane
            chip = ui_sidepane.DiffChip(self.footer, app, record, on_click=lambda: app.show_diff_record(record))
        except Exception:
            return
        chip.pack(side="left", padx=(0, 8))

    def _render_reply_artifact(self) -> None:
        """A long reply (> 6k chars) gets an "Open full reply in pane" action."""
        artifacts = getattr(self, "_artifacts", None) or {}
        full = next((item for item in artifacts.values() if getattr(item, "kind", "") == "text"), None)
        if full is None:
            return
        app = self.app
        self._action(self.footer, "Open in pane", lambda: app.show_artifact(full), tooltip="Read the whole reply in the side pane (Ctrl+Shift+P)")

    def _render_receipt(self, receipt: dict[str, Any]) -> None:
        theme = self.theme
        app = self.app
        action = str(receipt.get("action") or "")
        stored = action in {"created", "superseded", "reasserted", "retracted", "confirmed proposal", "stored", "updated"}
        color = theme.success if stored else theme.warning
        row = tk.Frame(self.body, bg=theme.bg)
        row.pack(fill="x", pady=(0, 6))
        pill = tk.Label(row, text=("✓ Memory " + action) if stored else ("Memory " + action), bg=theme.surface_alt, fg=color, font=app.fonts.tiny, padx=8, pady=2, cursor="hand2")
        pill.pack(side="left")
        pill.bind("<Button-1>", lambda _e: app.set_view("memory"))
        summary = receipt_summary(receipt)
        if summary:
            tk.Label(row, text=compact_activity(summary, 160), bg=self.body_bg, fg=theme.muted, font=app.fonts.tiny, anchor="w").pack(side="left", padx=(8, 0))
        Tooltip(pill, "The outcome the memory store reported for this turn; click to open the Memory view.", app)

    def _render_proposal(self, proposal: dict[str, Any]) -> None:
        """M1: the store still offers a fact for confirmation — Store it sends the
        literal ``store it``; Dismiss only hides the chip (the offer lapses on its own)."""
        theme = self.theme
        app = self.app
        fact = proposal.get("fact") or {}
        row = tk.Frame(self.body, bg=theme.surface_alt, highlightbackground=theme.border, highlightthickness=1, padx=10, pady=6)
        row.pack(fill="x", pady=(0, 6))
        self.proposal_frame = row
        summary = f"Proposed fact: {fact.get('subject', '')} · {fact.get('predicate', '')} · {fact.get('value', '')}"
        tk.Label(row, text=compact_activity(summary, 160), bg=theme.surface_alt, fg=theme.text, font=app.fonts.small, anchor="w").pack(side="left", fill="x", expand=True)

        def dismiss() -> None:
            self.message.proposal = None
            try:
                row.destroy()
            except tk.TclError:
                pass

        RoundButton(row, app, "Dismiss", dismiss, kind="ghost", padx=9, pady=3, font=app.fonts.tiny, tooltip="Hide this offer; nothing is stored").pack(side="right")
        RoundButton(row, app, "Store it", lambda: (dismiss(), app.send_text("store it")), kind="accent", padx=10, pady=3, font=app.fonts.tiny, tooltip="Send “store it” — the store confirms the proposal and answers with a receipt").pack(side="right", padx=(0, 6))

    def _render_version_pager(self) -> None:
        theme = self.theme
        app = self.app
        message = self.message
        pager = tk.Frame(self.footer, bg=theme.bg)
        pager.pack(side="left")
        total = len(message.versions)

        def show(index: int) -> None:
            index = max(0, min(total - 1, index))
            message.version_index = index
            message.content = message.versions[index]
            self.render_final()
            app.root.after(120, app._refit_texts)

        prev_button = tk.Label(pager, text="‹", bg=theme.bg, fg=theme.muted, font=app.fonts.small, cursor="hand2", padx=4)
        prev_button.pack(side="left")
        prev_button.bind("<Button-1>", lambda _e: show(message.version_index - 1))
        tk.Label(pager, text=f"{message.version_index + 1}/{total}", bg=theme.bg, fg=theme.faint, font=app.fonts.tiny).pack(side="left")
        next_button = tk.Label(pager, text="›", bg=theme.bg, fg=theme.muted, font=app.fonts.small, cursor="hand2", padx=4)
        next_button.pack(side="left")
        next_button.bind("<Button-1>", lambda _e: show(message.version_index + 1))
        Tooltip(pager, "Earlier versions of this reply", app)

    def _render_notice(self, text: str, *, kind: str, title: str = "", actions: list[tuple[str, Callable[[], None], str]] | None = None) -> tk.Frame:
        theme = self.theme
        app = self.app
        color = theme.danger if kind == "danger" else theme.warning
        frame = tk.Frame(self.body, bg=theme.surface, highlightbackground=color, highlightthickness=1, padx=12, pady=8)
        frame.pack(fill="x", pady=(0, 6))
        if title:
            tk.Label(frame, text=title, bg=theme.surface, fg=color, font=app.fonts.small_bold, anchor="w").pack(fill="x")
        if text:
            text_widget = AutoText(frame, app, font=app.fonts.small, bg=theme.surface, fg=theme.text)
            text_widget.pack(fill="x", pady=(2 if title else 0, 0))
            text_widget.set_text(text)
        if actions:
            row = tk.Frame(frame, bg=theme.surface)
            row.pack(fill="x", pady=(6, 0))
            for label, command, button_kind in actions:
                RoundButton(row, app, label, command, kind=button_kind, padx=10, pady=4, font=app.fonts.small_bold).pack(side="left", padx=(0, 6))
        return frame

    @staticmethod
    def humanise_failure(kind: Any) -> str:
        text = str(kind or "").strip().lower()
        known = {
            "timeout": "Timed out", "provider": "Model provider error", "provider_error": "Model provider error",
            "tool": "A tool failed", "tool_error": "A tool failed", "budget": "Budget exhausted",
            "cancelled": "Stopped", "network": "Network error", "rate_limit": "Rate limited", "approval": "Waiting for approval",
        }
        if text in known:
            return known[text]
        return text.replace("_", " ").capitalize() if text else ""

    def _copy_error_details(self) -> None:
        message = self.message
        metrics = message.metrics or {}
        lines = [
            f"kind: {metrics.get('failure_kind') or message.status}",
            f"reason: {message.reason or message.content}",
            f"model: {metrics.get('final_model') or message.model or ''}",
            f"trace id: {metrics.get('trace_id') or ''}",
        ]
        self.app.copy_text("\n".join(lines), quiet=True)
        self.app.toast("Error details copied.", kind="success")

    def _render_approval(self, approval_id: int) -> None:
        """The §4 approval card for this turn, fed by the store's row."""
        app = self.app
        message = self.message
        detail = message.approval
        loading = detail is None
        if loading:
            message.approval = {}
            app.session.approval_detail(approval_id)
        missing = bool(detail) and bool(detail.get("missing"))
        decided = message.approval_decision
        card = ApprovalCard(
            self.body, app, detail if detail and not missing else None,
            approval_id=approval_id,
            on_decide=lambda aid, approve, scope, reason: app.decide_inline_approval(self, aid, approve, scope=scope, reason=reason),
            decision=decided, loading=loading and not decided, missing=missing and not decided,
            resumable=bool(decided) and decided.startswith(("Approved", "Allowed", "Always")) and message.approval_resumable,
            on_retry=lambda: app.resume_after_approval(self),
            grant_id=message.approval_grant_id, on_revoke=app.revoke_grant,
        )
        card.pack(fill="x", pady=(4, 6))
        self.approval_frame = card
        self.approve_button = card.approve_button
        if not decided:
            if app.active_card is None and not app.busy and not app.composer.input.value().strip():
                app.root.after(80, card.focus)
            elif app.composer.input.value().strip():
                app.composer.show_review_hint()

    def refresh_approval(self) -> None:
        frame = getattr(self, "approval_frame", None)
        if frame is None or not frame.winfo_exists() or self.message.approval_id is None:
            return
        waiting = getattr(self, "waiting_label", None)
        frame.destroy()
        self.approval_frame = None
        self._render_approval(self.message.approval_id)
        card = self.approval_frame
        if waiting is not None:
            try:
                if waiting.winfo_exists():
                    if self.message.approval_decision:
                        waiting.destroy()
                        self.waiting_label = None
                    elif card is not None:
                        card.pack_configure(before=waiting)
            except tk.TclError:
                pass
        self.app.root.after(120, self.app._refit_texts)

    def _artifacts_for(self, content: str) -> dict[str, Any]:
        """Fenced blocks > 60 lines and replies > 6k chars, keyed by their text."""
        if self.message.role != "assistant":
            return {}
        try:
            from . import ui_sidepane
            return {item.text: item for item in ui_sidepane.artifact_candidates(content)}
        except Exception:
            return {}

    def _render_markdown(self, content: str) -> None:
        blocks = parse_markdown(content)
        if not blocks:
            blocks = [{"type": "paragraph", "text": content}]
        self._artifacts = self._artifacts_for(content)
        for block in blocks:
            kind = block["type"]
            if kind == "code":
                code_text = block.get("text", "")
                self._render_code(block.get("lang", ""), code_text, artifact=self._artifacts.get(code_text))
            elif kind == "heading":
                self._render_heading(int(block.get("level", 1)), block.get("text", ""))
            elif kind == "hr":
                tk.Frame(self.body, bg=self.theme.border_strong, height=1).pack(fill="x", pady=8)
            elif kind == "quote":
                self._render_quote(block.get("text", ""))
            elif kind == "list":
                self._render_list(bool(block.get("ordered")), block.get("items", []))
            elif kind == "table":
                self._render_table(block.get("rows", []))
            else:
                self._render_paragraph(block.get("text", ""))

    def _render_table(self, rows: list[list[str]]) -> None:
        """A real grid: header bold, zebra rows, every cell wrapped to its share
        of the content width. Wider than MAX_TABLE_COLUMNS falls back to monospace."""
        theme = self.theme
        app = self.app
        columns = max((len(row) for row in rows), default=0)
        if not rows or columns == 0 or columns > MAX_TABLE_COLUMNS:
            self._render_code("table", render_table_text(rows), copyable=False)
            return
        outer = tk.Frame(self.body, bg=self.body_bg)
        outer.pack(fill="x", pady=(2, app.px(10)))
        head = tk.Frame(outer, bg=self.body_bg)
        head.pack(fill="x")
        tk.Label(head, text=f"TABLE · {len(rows) - 1} row{'s' if len(rows) != 2 else ''}", bg=self.body_bg, fg=theme.faint, font=app.fonts.tiny, anchor="w").pack(side="left")
        self._action(head, "Copy as Markdown", lambda: app.copy_text(table_markdown(rows)), tooltip="Copy this table as a markdown table")
        grid = tk.Frame(outer, bg=theme.border_strong, padx=1, pady=1)
        grid.pack(fill="x")
        self.table_grid = grid
        cells: list[tk.Label] = []
        for row_index, row in enumerate(rows):
            header = row_index == 0
            bg = theme.code_head if header else (theme.surface if row_index % 2 else theme.surface_alt)
            for column in range(columns):
                text = plain_inline(row[column]) if column < len(row) else ""
                cell = tk.Label(
                    grid, text=text, bg=bg, fg=theme.text_strong if header else theme.text,
                    font=app.fonts.label_bold if header else app.fonts.body, anchor="nw", justify="left", padx=8, pady=4,
                )
                cell.grid(row=row_index, column=column, sticky="nsew", padx=(0, 1), pady=(0, 1))
                cells.append(cell)
        for column in range(columns):
            grid.grid_columnconfigure(column, weight=1, uniform="table")
        state = {"width": 0}

        def rewrap(width: int) -> None:
            wrap = max(app.px(60), width // columns - app.px(18))
            for cell in cells:
                try:
                    cell.configure(wraplength=wrap)
                except tk.TclError:
                    return

        def on_configure(event: Any) -> None:
            if abs(int(event.width) - state["width"]) < 4:
                return
            state["width"] = int(event.width)
            rewrap(int(event.width))

        rewrap(max(app.px(320), app.content_width() - app.px(120)))
        grid.bind("<Configure>", on_configure)

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
        widget = self._make_rich_text(self.body, self.body_bg)
        widget.pack(fill="x", pady=(0, self.app.px(8)))
        self._insert_inline(widget, text)

    def _render_heading(self, level: int, text: str) -> None:
        fonts = self.app.fonts
        font = fonts.h1 if level == 1 else fonts.h2 if level == 2 else fonts.h3
        widget = self._make_rich_text(self.body, self.body_bg)
        widget.configure(font=font, fg=self.theme.text_strong)
        widget.pack(fill="x", pady=(self.app.px(8), self.app.px(4)))
        self._insert_inline(widget, text)

    def _render_quote(self, text: str) -> None:
        theme = self.theme
        wrapper = tk.Frame(self.body, bg=self.body_bg)
        wrapper.pack(fill="x", pady=(0, self.app.px(8)))
        tk.Frame(wrapper, bg=theme.accent, width=3).pack(side="left", fill="y")
        inner = tk.Frame(wrapper, bg=theme.surface_alt, padx=12, pady=8)
        inner.pack(side="left", fill="x", expand=True)
        widget = self._make_rich_text(inner, theme.surface_alt)
        widget.configure(fg=theme.muted)
        widget.pack(fill="x")
        self._insert_inline(widget, text)

    def _render_list(self, ordered: bool, items: list[dict[str, Any]]) -> None:
        widget = self._make_rich_text(self.body, self.body_bg)
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

    def _head_action(self, head: tk.Misc, label: str, command: Callable[[], None], *, tooltip: str = "") -> tk.Label:
        theme = self.theme
        widget = tk.Label(head, text=label, bg=theme.code_head, fg=theme.muted, font=self.app.fonts.tiny, cursor="hand2", padx=6, takefocus=1, highlightthickness=1, highlightbackground=theme.code_head, highlightcolor=theme.accent)
        widget.pack(side="right")
        widget.bind("<Button-1>", lambda _e: command())
        widget.bind("<Return>", lambda _e: (command(), "break")[1])
        widget.bind("<space>", lambda _e: (command(), "break")[1])
        widget.bind("<Enter>", lambda _e: widget.configure(fg=theme.text))
        widget.bind("<Leave>", lambda _e: widget.configure(fg=theme.muted))
        if tooltip:
            Tooltip(widget, tooltip, self.app)
        return widget

    def _render_code(self, language: str, code: str, *, copyable: bool = True, artifact: Any = None) -> None:
        """A fenced block: header with Copy · Save as…, a line-number gutter and
        the code. Blocks over 60 lines show a 20-line preview with Open in pane."""
        theme = self.theme
        app = self.app
        frame = tk.Frame(self.body, bg=theme.code_bg, highlightbackground=theme.border_strong, highlightthickness=1)
        frame.pack(fill="x", pady=(2, app.px(10)))
        head = tk.Frame(frame, bg=theme.code_head, padx=10, pady=4)
        head.pack(fill="x")
        tk.Label(head, text=(language or "code").upper(), bg=theme.code_head, fg=theme.faint, font=app.fonts.tiny).pack(side="left")
        lines = code.split("\n")
        preview = artifact is not None and len(lines) > CODE_PREVIEW_LINES
        shown = "\n".join(lines[:CODE_PREVIEW_LINES]) if preview else code
        if copyable:
            copy = self._head_action(head, "Copy", lambda: None, tooltip="Copy the whole block")

            def do_copy() -> None:
                app.copy_text(code, quiet=True)
                copy.configure(text="Copied ✓", fg=theme.success)
                copy.after(1400, lambda: copy.configure(text="Copy", fg=theme.muted))

            copy.bind("<Button-1>", lambda _e: do_copy())
            copy.bind("<Return>", lambda _e: (do_copy(), "break")[1])
            self._head_action(head, "Save as…", lambda: app.save_code_as(code, language, artifact), tooltip="Save this block to a file you choose")
            if artifact is not None:
                self._head_action(head, "Open in pane", lambda: app.show_artifact(artifact), tooltip="Read the whole block in the side pane (Ctrl+Shift+P)")
        row = tk.Frame(frame, bg=theme.code_bg)
        row.pack(fill="x")
        gutter = AutoText(row, app, font=app.fonts.mono, bg=theme.code_bg, fg=theme.faint, wrap="none")
        gutter.configure(padx=8, pady=10, spacing1=0, spacing3=0, width=max(2, len(str(max(1, len(shown.split(chr(10)))))) + 1))
        gutter.tag_configure("right", justify="right")
        gutter.pack(side="left", fill="y")
        body = CodeText(row, app, font=app.fonts.mono, bg=theme.code_bg, fg=theme.text, wrap="char", gutter=gutter)
        body.configure(padx=6, pady=10, spacing1=0, spacing3=0)
        body.pack(side="left", fill="x", expand=True)
        body.set_text(shown)
        if preview:
            more = tk.Frame(frame, bg=theme.code_head, padx=10, pady=4)
            more.pack(fill="x")
            tk.Label(more, text=f"… {len(lines) - CODE_PREVIEW_LINES} more lines — Open in pane shows everything", bg=theme.code_head, fg=theme.faint, font=app.fonts.tiny, anchor="w").pack(side="left")


class MarkdownBox(MessageCard):
    """Markdown rendered with the message-card renderer but without the card
    chrome (no avatar, header, footer or actions) — for sheets and panes."""

    def __init__(self, master: tk.Misc, app: "JarvisDesktop", text: str = "", *, bg: str | None = None) -> None:
        theme = app.theme
        background = bg or theme.bg
        tk.Frame.__init__(self, master, bg=background)
        self.app = app
        self.theme = theme
        self.body_bg = background
        self.message = Message("assistant", "")
        self.column = self
        self.body = tk.Frame(self, bg=background)
        self.body.pack(fill="x")
        self.steps_frame = tk.Frame(self.body, bg=background)
        self.footer = tk.Frame(self, bg=background)
        self.stream_text = None
        self.steps_label = None
        self.pulse = None
        self.meta_label = None
        self.header = None
        self._pulse_after = None
        self._pulse_step = 0
        if text:
            self.set_markdown(text)

    def set_markdown(self, text: str) -> None:
        self.message.content = str(text or "")
        self._clear_body()
        self._render_markdown(self.message.content)

    def refit(self) -> None:
        for widget in self.app._all_autotexts(self):
            widget.fit()


class TaskDetailSheet(tk.Toplevel):
    """One background task: prompt, status, attempts, last error and result.

    Everything shown comes from the store's task row; the result renders as
    markdown and **Bring into chat** quotes it into the composer.
    """

    def __init__(self, app: "JarvisDesktop", task: dict[str, Any]) -> None:
        super().__init__(app.root)
        theme = app.theme
        self.app = app
        self.task = dict(task or {})
        task_id = int(self.task.get("id") or 0)
        self.title(f"Task #{task_id}")
        self.configure(bg=theme.bg)
        self.transient(app.root)
        self.resizable(True, True)
        _apply_titlebar_theme(self, theme.dark)
        self.minsize(app.px(520), app.px(360))
        screen_h = work_area_height(self)
        width = app.px(760)
        height = settings_window_height(app.px(640), screen_h)
        x = app.root.winfo_rootx() + max(0, (app.root.winfo_width() - width) // 2)
        y = max(0, min(app.root.winfo_rooty() + app.px(60), screen_h - height - app.px(40)))
        self.geometry(f"{width}x{height}+{x}+{y}")
        self.bind("<Escape>", lambda _e: self.destroy())
        self._build()

    def _build(self) -> None:
        app = self.app
        theme = app.theme
        task = self.task
        task_id = int(task.get("id") or 0)
        status = str(task.get("status") or "unknown")
        header = tk.Frame(self, bg=theme.bg, padx=app.px(22), pady=app.px(14))
        header.pack(fill="x")
        head_row = tk.Frame(header, bg=theme.bg)
        head_row.pack(fill="x")
        tk.Label(head_row, text=f"Task #{task_id}", bg=theme.bg, fg=theme.text_strong, font=app.fonts.h1).pack(side="left")
        lowered = status.lower()
        pill_color = theme.success if lowered in {"done", "complete", "completed"} else theme.danger if lowered in {"failed", "error", "cancelled"} else theme.warning
        tk.Label(head_row, text=status.upper(), bg=theme.surface_alt, fg=pill_color, font=app.fonts.tiny, padx=8, pady=2).pack(side="left", padx=(10, 0))
        bits = [f"model {task.get('model') or 'auto'}"]
        attempts = int(task.get("attempt_count") or 0)
        max_attempts = int(task.get("max_attempts") or 0)
        if attempts or max_attempts:
            bits.append(f"attempts {attempts}/{max_attempts}" if max_attempts else f"attempts {attempts}")
        when = relative_or_clock(task.get("updated_at"))
        if when:
            bits.append(f"updated {when}")
        tk.Label(header, text="  ·  ".join(bits), bg=theme.bg, fg=theme.muted, font=app.fonts.small, anchor="w").pack(fill="x", pady=(4, 0))
        approval_id = task.get("awaiting_approval_id")
        if isinstance(approval_id, int) and approval_id > 0:
            link = tk.Label(header, text=f"⏸ Waiting for approval #{approval_id} — show it in the chat", bg=theme.bg, fg=theme.warning, font=app.fonts.small_bold, cursor="hand2", anchor="w")
            link.pack(fill="x", pady=(6, 0))
            link.bind("<Button-1>", lambda _e, target=approval_id: self._show_approval(target))
        self.scroller = ScrollFrame(self, app, bg=theme.bg)
        self.scroller.pack(fill="both", expand=True)
        body = tk.Frame(self.scroller.inner, bg=theme.bg, padx=app.px(22), pady=app.px(4))
        body.pack(fill="both", expand=True)

        def section(title: str) -> tk.Frame:
            tk.Label(body, text=title.upper(), bg=theme.bg, fg=theme.faint, font=app.fonts.tiny, anchor="w").pack(fill="x", pady=(10, 4))
            frame = tk.Frame(body, bg=theme.surface, highlightbackground=theme.border, highlightthickness=1, padx=app.px(12), pady=app.px(8))
            frame.pack(fill="x")
            return frame

        prompt_box = section("Prompt")
        prompt_text = AutoText(prompt_box, app, font=app.fonts.body, bg=theme.surface, fg=theme.text)
        prompt_text.pack(fill="x")
        prompt_text.set_text(str(task.get("full_prompt") or task.get("prompt") or "(no prompt recorded)"))
        error_text = str(task.get("last_error") or "")
        if error_text:
            error_box = section("Last error")
            error_box.configure(highlightbackground=theme.danger)
            error_widget = AutoText(error_box, app, font=app.fonts.mono_small, bg=theme.surface, fg=theme.danger, wrap="char")
            error_widget.pack(fill="x")
            error_widget.set_text(error_text)
        result_box = section("Result")
        result_text = str(task.get("result") or "")
        self.result_text = result_text
        if result_text:
            self.result_box = MarkdownBox(result_box, app, result_text, bg=theme.surface)
            self.result_box.pack(fill="x")
        else:
            self.result_box = None
            tk.Label(result_box, text="No result yet — the worker has not finished this task." if lowered in {"queued", "running", "leased", "retry", "pending"} else "The task recorded no result.", bg=theme.surface, fg=theme.faint, font=app.fonts.small, anchor="w").pack(fill="x")
        actions = tk.Frame(self, bg=theme.bg, padx=app.px(22), pady=app.px(12))
        actions.pack(fill="x")
        RoundButton(actions, app, "Close", self.destroy, kind="ghost", padx=12, pady=5, font=app.fonts.small_bold).pack(side="right")
        if result_text:
            RoundButton(actions, app, "Copy result", lambda: app.copy_text(result_text), kind="ghost", padx=12, pady=5, font=app.fonts.small_bold).pack(side="right", padx=(0, 8))
            RoundButton(actions, app, "Bring into chat", self.bring_into_chat, kind="accent", padx=12, pady=5, font=app.fonts.small_bold, tooltip="Quote the result into the composer").pack(side="right", padx=(0, 8))
        self.after(120, self._refit)

    def _refit(self) -> None:
        try:
            for widget in self.app._all_autotexts(self.scroller.inner):
                widget.fit()
            self.scroller._sync_region()
        except tk.TclError:
            pass

    def _show_approval(self, approval_id: int) -> None:
        self.destroy()
        self.app.scroll_to_approval(int(approval_id))

    def bring_into_chat(self) -> None:
        text = self.result_text
        self.destroy()
        if text:
            self.app.set_view("chat")
            self.app.quote_text(text)


class EmptyState(tk.Frame):
    """Claude-style greeting with suggestion cards for a fresh chat."""

    SUGGESTIONS = (
        ("◫", "What changed here", "List the files in my workspace that changed in the last day and summarize what each one is."),
        ("‹/›", "Build with code", "Create a Python script in code/ that renames files in a folder by date, with tests."),
        ("⌕", "Research and verify", "Research this and tell me only what you could verify, with sources: "),
        ("◍", "Remember a fact", 'Remember this project fact: {"subject":"","predicate":"","value":""}'),
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
        self.cards: list[tk.Frame] = []
        for index, (glyph, title, prompt) in enumerate(self.SUGGESTIONS):
            card = tk.Frame(grid, bg=theme.surface, highlightbackground=theme.border, highlightcolor=theme.accent, highlightthickness=1, padx=14, pady=12, cursor="hand2", width=app.px(250), takefocus=1)
            card.grid(row=index // 2, column=index % 2, padx=6, pady=6, sticky="nsew")
            card.bind("<Return>", lambda _e, text=prompt: (app.use_suggestion(text), "break")[1])
            card.bind("<space>", lambda _e, text=prompt: (app.use_suggestion(text), "break")[1])
            self.cards.append(card)
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

MODE_CHIPS = (
    ("Think", "Reasoning", "Reason carefully before answering (reasoning model)"),
    ("Code", "Coding", "Build, fix, test and inspect code (coding model)"),
    ("Deep", "Deep 30B", "Heavy 30B model for the hardest questions"),
)
MAX_PROMPT_HISTORY = 60


class Composer(tk.Frame):
    """The message box: mode chips, any-file attachments, history, slash commands."""

    def __init__(self, master: tk.Misc, app: "JarvisDesktop") -> None:
        theme = app.theme
        super().__init__(master, bg=theme.bg)
        self.app = app
        self.theme = theme
        self.attachments: list[dict[str, str]] = []
        self.history: list[str] = [str(item) for item in (app.settings.get("prompt_history") or []) if isinstance(item, str)][-MAX_PROMPT_HISTORY:]
        self.history_index: int | None = None
        self.history_draft = ""
        self.queue_frame = tk.Frame(self, bg=theme.bg)
        self.card = tk.Frame(self, bg=theme.surface, highlightbackground=theme.border_strong, highlightthickness=1, padx=app.px(12), pady=app.px(8))
        self.card.pack(fill="x")
        self.ready = True
        self.reading = False
        self.chips = tk.Frame(self.card, bg=theme.surface)
        self.input = GrowText(self.card, app)
        self.input.pack(fill="x")
        self.input.on_change = self._on_change
        self.input.bind("<Return>", self._enter_key)
        self.input.bind("<Shift-Return>", self._newline_key)
        self.input.bind("<Control-Return>", self._newline_key)
        self.input.bind("<Up>", self._history_up)
        self.input.bind("<Down>", self._history_down)
        self.input.bind("<Tab>", self._tab_key)
        self.input.bind("<Control-v>", self._paste, add="+")
        self.input.bind("<FocusIn>", lambda _e: self.card.configure(highlightbackground=theme.accent))
        self.input.bind("<FocusOut>", lambda _e: self.card.configure(highlightbackground=theme.border_strong))
        # "/" command list and "@" file picker: they bind themselves ahead of the
        # widget's own tag and only answer while open (see jarvis.ui_popups).
        self.slash: Any = None
        self.at: Any = None
        try:
            from . import ui_popups
            self.slash = ui_popups.SlashPopup(app, self.input, on_choose=None)
            self.at = ui_popups.AtPopup(app, self.input, lambda: app.file_index, self._attach_from_at)
        except Exception as exc:  # the composer must work even if the popups cannot
            self.slash = None
            self.at = None
            try:
                print(f"[jarvis-desktop] composer popups unavailable: {type(exc).__name__}: {exc}", file=sys.stderr)
            except Exception:
                pass
        row = tk.Frame(self.card, bg=theme.surface)
        row.pack(fill="x", pady=(app.px(4), 0))
        self.attach_button = IconButton(row, app, "＋", self.open_attach_menu, tooltip="Files… · Snip screen · Paste — or drop files anywhere on the window")
        self.attach_button.pack(side="left")
        self.mode_buttons: dict[str, RoundButton] = {}
        for chip, model_label, hint in MODE_CHIPS:
            button = RoundButton(row, app, chip, lambda target=model_label: self.toggle_mode(target), kind="subtle", padx=9, pady=3, font=app.fonts.tiny, radius=999, tooltip=hint)
            button.pack(side="left", padx=(4, 0))
            self.mode_buttons[model_label] = button
        self.model_label = tk.Label(row, text="", bg=theme.surface, fg=theme.faint, font=app.fonts.tiny, cursor="hand2")
        self.model_label.pack(side="left", padx=(10, 0))
        self.model_label.bind("<Button-1>", lambda _e: app.cycle_model())
        Tooltip(self.model_label, "Active model profile — click to cycle (Ctrl+1…5)", app)
        self.counter = tk.Label(row, text="", bg=theme.surface, fg=theme.faint, font=app.fonts.tiny)
        self.counter.pack(side="left", padx=(10, 0))
        self.send_button = RoundButton(row, app, "↑", app.send, kind="accent", padx=11, pady=4, radius=10, font=app.fonts.icon, tooltip="Send (Enter)")
        self.send_button.pack(side="right")
        self.stop_button = RoundButton(row, app, "■", app.stop_request, kind="danger", padx=11, pady=4, radius=10, font=app.fonts.icon_small, tooltip="Stop (Esc)")
        self.queue_button = IconButton(row, app, "◷", app.queue_current_prompt, tooltip="Queue this prompt as a background task instead of sending it")
        self.queue_button.pack(side="right", padx=(0, 6))
        self.hint = tk.Label(self, text=self.HINT_IDLE, bg=theme.bg, fg=theme.faint, font=app.fonts.tiny)
        self.hint.pack(pady=(5, 0))
        self._hint_offset = 0
        self._hint_after: str | None = self.after(self.HINT_ROTATE_MS, self._rotate_hint)
        self.refresh_model_label()

    # -- model chips -------------------------------------------------------

    def refresh_model_label(self) -> None:
        label = self.app.model_label
        name = self.app.model_names.get(model_override_for(label), "")
        self.model_label.configure(text=f"{label}{' · ' + name if name else ''}")
        for model_label, button in self.mode_buttons.items():
            button.set_kind("active" if model_label == label else "subtle")

    def toggle_mode(self, model_label: str) -> None:
        self.app.set_model("Auto" if self.app.model_label == model_label else model_label)
        self.input.focus_set()

    # -- typing --------------------------------------------------------------

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

    def _paste(self, _event: Any) -> str | None:
        """Paste file paths as attachments, a screenshot when there is no text, else text.

        The filesystem probes and the clipboard bitmap decode run on the I/O pool;
        only the decision of what to do with the outcome happens on the Tk thread.
        """
        try:
            clip = self.app.root.clipboard_get()
        except tk.TclError:
            clip = ""
        if clip.strip():
            lines = [line.strip().strip('"') for line in clip.splitlines() if line.strip()]
            if lines and len(lines) <= MAX_IMAGE_ATTACHMENTS + 8 and all(looks_like_path(line) for line in lines):
                self.app.run_job("probe_paths", probe_paths, lines, on_done=lambda result, error, text=clip, wanted=lines: self._pasted_paths(text, wanted, result, error))
                return "break"
            return None
        folder = str(Path(self.app.data_dir or ".") / "attachments")
        self.app.run_job("clipboard_image", save_clipboard_image, folder, on_done=self._pasted_image)
        return "break"

    def _pasted_paths(self, text: str, wanted: list[str], result: Any, error: str | None) -> None:
        found = list(result) if isinstance(result, list) else []
        if error or len(found) != len(wanted):
            try:
                self.input.insert("insert", text)
            except tk.TclError:
                pass
            return
        self.add_files(found)

    def _pasted_image(self, result: Any, error: str | None) -> None:
        if error:
            self.app.toast(f"Could not save the screenshot: {error}", kind="error")
            return
        if result:
            self.add_files([str(result)])

    def _history_up(self, _event: Any) -> str | None:
        current = self.input.value()
        if not current.strip() and self.app.queued:
            item = self.app.queued.pop()
            self.input.set_value(item.get("text", ""))
            self.input.mark_set("insert", "end")
            self.render_queue()
            return "break"
        if not self.history:
            return None
        if self.history_index is None:
            if current.strip() and "\n" in current:
                return None
            self.history_draft = current
            self.history_index = len(self.history)
        if self.history_index > 0:
            self.history_index -= 1
            self.input.set_value(self.history[self.history_index])
            self.input.mark_set("insert", "end")
        return "break"

    def _history_down(self, _event: Any) -> str | None:
        if self.history_index is None:
            return None
        self.history_index += 1
        if self.history_index >= len(self.history):
            self.history_index = None
            self.input.set_value(self.history_draft)
        else:
            self.input.set_value(self.history[self.history_index])
        self.input.mark_set("insert", "end")
        return "break"

    def remember(self, prompt: str) -> None:
        text = prompt.strip()
        if not text:
            return
        text = redact_secrets(text, "[REDACTED]")
        if self.history and self.history[-1] == text:
            return
        self.history.append(text)
        self.history = self.history[-MAX_PROMPT_HISTORY:]
        self.history_index = None
        self.app.settings.set("prompt_history", [item for item in self.history if len(item) <= 2_000][-MAX_PROMPT_HISTORY:])

    HINT_ITEMS = (
        "Enter to send", "Shift+Enter for a new line", "↑ recalls earlier prompts",
        "type / for commands", "@ picks a file", "drop files to attach", "Win+H dictates",
    )
    HINT_ROTATE_MS = 9_000
    HINT_IDLE = " · ".join(HINT_ITEMS[:3])
    HINT_BUSY = "Jarvis is working — Enter or Tab queues a follow-up for when it finishes · Esc stops"
    HINT_STARTING = "Starting Jarvis…"
    HINT_REVIEW = "Tab to review the approval"

    @classmethod
    def hint_text(cls, offset: int = 0) -> str:
        """Three of the idle hints at a time, rotating (J12: never the whole list)."""
        items = cls.HINT_ITEMS
        start = offset % len(items)
        window = [items[(start + index) % len(items)] for index in range(3)]
        return " · ".join(window)

    def _rotate_hint(self) -> None:
        self._hint_after = None
        try:
            if not self.winfo_exists():
                return
            if self.ready and not self.app.busy and self.hint.cget("text") in {self.HINT_IDLE, self.hint_text(self._hint_offset)}:
                self._hint_offset = (self._hint_offset + 3) % len(self.HINT_ITEMS)
                self.hint.configure(text=self.hint_text(self._hint_offset), fg=self.theme.faint)
            self._hint_after = self.after(self.HINT_ROTATE_MS, self._rotate_hint)
        except tk.TclError:
            self._hint_after = None

    def apply_width_budget(self, width: int) -> None:
        """Degrade the footer before anything clips: the hint line goes first, then
        the model id (J1)."""
        plan = composer_footer_plan(int(width), self.app.scale)
        try:
            if plan["hint"]:
                if not self.hint.winfo_ismapped():
                    self.hint.pack(pady=(5, 0))
            else:
                self.hint.pack_forget()
            if plan["model"]:
                if not self.model_label.winfo_ismapped():
                    self.model_label.pack(side="left", padx=(10, 0), after=list(self.mode_buttons.values())[-1] if self.mode_buttons else None)
            else:
                self.model_label.pack_forget()
        except tk.TclError:
            pass

    def show_review_hint(self) -> None:
        try:
            self.hint.configure(text=self.HINT_REVIEW, fg=self.theme.warning)
        except tk.TclError:
            pass

    def _tab_key(self, _event: Any) -> str | None:
        """Tab: review a pending approval card, else queue the draft while busy."""
        if self.app.focus_pending_approval():
            return "break"
        if self.app.busy and self.input.value().strip():
            self.app.queue_follow_up_from_composer()
            return "break"
        return "break"

    def set_busy(self, busy: bool) -> None:
        """The one Stop control (J8): ↑ becomes ■ while Jarvis works."""
        if busy:
            self.send_button.pack_forget()
            self.stop_button.set_text("■")
            self.stop_button.set_enabled(True)
            self.stop_button.pack(side="right")
        else:
            self.stop_button.pack_forget()
            self.send_button.pack(side="right")
        self.attach_button.set_enabled(not busy)
        try:
            self.hint.configure(text=self.HINT_BUSY if busy else (self.HINT_IDLE if self.ready else self.HINT_STARTING), fg=self.theme.faint)
        except tk.TclError:
            pass
        self.render_queue()

    def set_ready(self, ready: bool) -> None:
        """Until the worker reports ready, the box is visibly disabled."""
        self.ready = bool(ready)
        try:
            self.input.configure(state="normal" if self.ready else "disabled")
            self.send_button.set_enabled(self.ready)
            self.hint.configure(text=self.HINT_IDLE if self.ready else self.HINT_STARTING)
        except tk.TclError:
            pass

    def render_queue(self) -> None:
        """Follow-ups typed while Jarvis works wait here, honestly labelled."""
        theme = self.theme
        app = self.app
        for child in self.queue_frame.winfo_children():
            child.destroy()
        queued = list(app.queued)
        if not queued:
            self.queue_frame.pack_forget()
            return
        self.queue_frame.pack(fill="x", before=self.card, pady=(0, 6))
        if not app.busy:
            last = app.last_turn_status()
            waiting = any(card.message.approval_id is not None and not card.message.approval_decision for card in app.cards[-2:])
            status, warning = queue_strip_status(last, waiting)
            tk.Label(self.queue_frame, text=status, bg=theme.bg, fg=theme.warning if warning else theme.muted, font=app.fonts.tiny, anchor="w").pack(fill="x")
        for index, item in enumerate(queued):
            row = tk.Frame(self.queue_frame, bg=theme.surface_alt, highlightbackground=theme.border, highlightthickness=1, padx=8, pady=4)
            row.pack(fill="x", pady=(0, 3))
            names = item.get("names") or []
            label = compact_activity(item.get("text") or ("Attached files" if names else ""), 110)
            if names:
                label += f"  ·  {len(names)} file{'s' if len(names) != 1 else ''}"
            tk.Label(row, text=f"⏳ Queued · {label}", bg=theme.surface_alt, fg=theme.text, font=app.fonts.small, anchor="w").pack(side="left", fill="x", expand=True)
            IconButton(row, app, "×", lambda target=index: app.remove_queued(target), tooltip="Drop this follow-up").pack(side="right")
            if not app.busy:
                RoundButton(row, app, "Send now", lambda target=index: app.send_queued(target), kind="accent", padx=9, pady=2, font=app.fonts.tiny).pack(side="right", padx=(0, 6))

    # -- popups: "+" menu, "@" picker, Snip ---------------------------------

    def close_popups(self) -> None:
        for name in ("slash", "at"):
            popup = getattr(self, name, None)
            setattr(self, name, None)
            if popup is None:
                continue
            try:
                popup.destroy()
            except Exception:
                pass

    def destroy(self) -> None:
        self.close_popups()
        super().destroy()

    def _attach_from_at(self, path: str) -> None:
        if self.at is not None:
            try:
                self.at.remove_token()
            except tk.TclError:
                pass
        self.add_files([str(path)])

    def open_attach_menu(self) -> None:
        try:
            from . import ui_popups
            ui_popups.AttachMenu(self.app, self.attach_button, {
                "files": self.pick_attachments,
                "snip": self._snip,
                "paste": lambda: self._paste(None),
            })
        except Exception:
            self.pick_attachments()

    def _snip(self) -> None:
        try:
            from . import ui_popups
        except Exception as exc:
            self.app.toast(f"Snip is unavailable ({type(exc).__name__}).", kind="warning")
            return
        ui_popups.run_snip(self.app, on_png=self._snipped, on_fail=lambda message: self.app.toast(str(message), kind="warning"))

    def _snipped(self, png: bytes) -> None:
        target = str(Path(self.app.data_dir or ".") / "attachments" / f"snip-{datetime.now():%Y%m%d-%H%M%S-%f}.png")

        def saved(result: Any, error: str | None) -> None:
            if error:
                self.app.toast(f"Could not save the snip: {error}", kind="error")
                return
            self.add_files([str(result)])

        self.app.run_job("save_snip", write_bytes_file, target, bytes(png), on_done=saved)

    # -- attachments ---------------------------------------------------------

    def pick_attachments(self) -> None:
        paths = filedialog.askopenfilenames(
            parent=self.app.root,
            title="Attach files",
            filetypes=[
                ("Files Jarvis can read", "*.png *.jpg *.jpeg *.webp *.gif *.txt *.md *.py *.js *.ts *.json *.csv *.yaml *.yml *.toml *.html *.css *.log *.sql *.sh *.ps1"),
                ("Images", "*.png *.jpg *.jpeg *.webp *.gif"),
                ("All files", "*.*"),
            ],
        )
        self.add_files(list(paths))

    def add_files(self, paths: list[str]) -> None:
        added = 0
        for raw in paths:
            path = str(raw)
            if not Path(path).is_file():
                continue
            if any(item["path"] == path for item in self.attachments):
                continue
            kind = classify_attachment(path)
            if kind == "unsupported":
                self.app.toast(f"{Path(path).name}: only text, code and image files can be attached.", kind="warning")
                continue
            if kind == "image" and sum(1 for item in self.attachments if item["kind"] == "image") >= MAX_IMAGE_ATTACHMENTS:
                self.app.toast(f"At most {MAX_IMAGE_ATTACHMENTS} images per message.", kind="warning")
                continue
            if kind == "text" and sum(1 for item in self.attachments if item["kind"] == "text") >= 8:
                self.app.toast("At most 8 text files per message.", kind="warning")
                continue
            self.attachments.append({"path": path, "kind": kind, "name": Path(path).name})
            added += 1
        if added:
            self.render_chips()
            self.app.toast(f"Attached {added} file{'s' if added != 1 else ''}.", kind="success")
            self.input.focus_set()

    def render_chips(self) -> None:
        theme = self.theme
        for child in self.chips.winfo_children():
            child.destroy()
        if not self.attachments:
            self.chips.pack_forget()
            return
        self.chips.pack(fill="x", before=self.input, pady=(0, 6))
        for item in self.attachments:
            chip = tk.Frame(self.chips, bg=theme.surface_alt, highlightbackground=theme.border, highlightthickness=1, padx=8, pady=3)
            chip.pack(side="left", padx=(0, 6), pady=(0, 4))
            glyph = "🖼" if item["kind"] == "image" else "📄"
            tk.Label(chip, text=f"{glyph} {item['name']}", bg=theme.surface_alt, fg=theme.text, font=app_font(self.app, "tiny")).pack(side="left")
            remove = tk.Label(chip, text="×", bg=theme.surface_alt, fg=theme.muted, font=app_font(self.app, "small"), cursor="hand2", padx=4)
            remove.pack(side="left")
            remove.bind("<Button-1>", lambda _e, target=item["path"]: self.remove_attachment(target))

    def remove_attachment(self, path: str) -> None:
        self.attachments = [item for item in self.attachments if item["path"] != path]
        self.render_chips()

    def take(self, on_ready: Callable[[str, list[str], list[str], list[str]], None]) -> None:
        """Empty the box and hand ``(text, image_paths, context_blocks, attachment_names)``
        to ``on_ready``. Text attachments are read on the I/O pool; while that is
        in flight the send button reads "Reading files…"."""
        text = self.input.value().strip()
        images = [item["path"] for item in self.attachments if item["kind"] == "image"]
        names = [item["name"] for item in self.attachments]
        text_paths = [item["path"] for item in self.attachments if item["kind"] == "text"]
        self.input.set_value("")
        self.attachments = []
        self.render_chips()
        self.counter.configure(text="")
        if not text_paths:
            on_ready(text, images, [], names)
            return
        self.set_reading(True)
        self.app.run_job("read_attachments", read_attachments, text_paths, on_done=lambda result, error: self._took(on_ready, text, images, names, result, error))

    def _took(self, on_ready: Callable[[str, list[str], list[str], list[str]], None], text: str, images: list[str], names: list[str], result: Any, error: str | None) -> None:
        self.set_reading(False)
        blocks: list[str] = []
        if error:
            self.app.toast(f"Could not read the attached files: {error}", kind="error")
        elif isinstance(result, tuple) and len(result) == 2:
            blocks = [str(block) for block in result[0]]
            for problem in result[1]:
                self.app.toast(str(problem), kind="error")
        on_ready(text, images, blocks, names)

    def set_reading(self, reading: bool) -> None:
        self.reading = bool(reading)
        try:
            self.send_button.set_label("Reading files…" if self.reading else "↑")
            self.send_button.set_enabled(self.ready and not self.reading)
        except tk.TclError:
            pass


def app_font(app: "JarvisDesktop", name: str) -> tkfont.Font:
    return getattr(app.fonts, name)


class CommandPalette(tk.Toplevel):
    """Ctrl+K — search chats and run actions from the keyboard."""

    def __init__(self, app: "JarvisDesktop", items: list[dict[str, Any]], on_query: Callable[[str], None] | None = None) -> None:
        super().__init__(app.root)
        theme = app.theme
        self.app = app
        self.items = items
        self.extra: list[dict[str, Any]] = []
        self.on_query = on_query
        self.query = ""
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
        if query != self.query:
            self.query = query
            self.extra = []
            if self.on_query is not None:
                self.on_query(query)
        self.filtered, self.selected = self._ordered(query)
        self._render()

    def _ordered(self, query: str) -> tuple[list[dict[str, Any]], int]:
        scored = [(self.score(item, query), index, item) for index, item in enumerate(self.items)]
        ranked = [entry for entry in scored if entry[0] > 0]
        if query:
            ranked.sort(key=lambda entry: (-entry[0], entry[1]))
        return order_palette_items(ranked, list(self.extra), ranked=bool(query))

    def set_extra(self, query: str, items: list[dict[str, Any]]) -> None:
        if query.strip() != self.query or not self.winfo_exists():
            return
        self.extra = list(items)
        keep = self.filtered[self.selected] if 0 <= self.selected < len(self.filtered) else None
        self.filtered, best = self._ordered(self.query)
        self.selected = next((index for index, item in enumerate(self.filtered) if item is keep), best)
        self._render()

    def _render(self) -> None:
        theme = self.theme = self.app.theme
        digest = render_digest([(item.get("group"), item.get("icon"), item.get("label"), item.get("detail"), item.get("runs")) for item in self.filtered])
        if getattr(self, "_digest", None) == digest and self.rows:
            self._paint_selection()  # same rows: only the selection moved
            return
        self._digest = digest
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
            detail = tk.Label(row, text=item.get("detail", ""), bg=theme.panel, fg=theme.faint, font=self.app.fonts.tiny)
            detail.pack(side="right")
            widgets: list[tk.Widget] = [row, glyph, detail]
            runs = item.get("runs")
            if isinstance(runs, list) and runs:
                # A message-search hit: the excerpt with the matched text in bold.
                for text, hit in runs:
                    part = tk.Label(row, text=text, bg=theme.panel, fg=theme.text_strong if hit else theme.text, font=self.app.fonts.label_bold if hit else self.app.fonts.label, anchor="w")
                    part.pack(side="left")
                    widgets.append(part)
            else:
                label = tk.Label(row, text=item.get("label", ""), bg=theme.panel, fg=theme.text, font=self.app.fonts.label, anchor="w")
                label.pack(side="left", fill="x", expand=True)
                widgets.append(label)
            for widget in widgets:
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
        tk.Label(header, text="Review the exact target before allowing it. Approve once is one-shot; This chat and Always are standing grants you can revoke in Settings → Standing approvals.", bg=theme.bg, fg=theme.muted, font=app.fonts.small, wraplength=app.px(820), justify="left").pack(anchor="w", pady=(3, 0))
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
        """The same ApprovalCard the reply uses, fed by the store's row."""
        app = self.app
        card = ApprovalCard(
            self.list.inner, app, row, approval_id=int(row.get("id") or 0),
            on_decide=self._decide, decision=None if pending else decision_from_row(row),
            bg=app.theme.surface if not pending else None,
        )
        card.pack(fill="x", pady=(0, 8))

    def _decide(self, approval_id: int, approve: bool, scope: str = "once", reason: str = "") -> None:
        self.app.decide_context_approval(approval_id, approve, scope=scope, reason=reason)


# The single keymap. The Shortcuts window and the /help view are generated from
# this table; every chord bound in JarvisDesktop._bind_shortcuts appears here.
SHORTCUTS: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (
    ("Conversation", (
        ("Enter", "Send message · on a focused approval card: Approve once"),
        ("Shift + Enter", "New line"),
        ("Tab", "While Jarvis is working: queue the draft as a follow-up"),
        ("↑ in an empty box", "Pull the last queued follow-up back, else recall earlier prompts"),
        ("Esc", "Focused approval card: Deny · palette or popup: close · while working: Stop"),
        ("Ctrl + N", "New chat"),
        ("Ctrl + L", "Focus the composer"),
        ("Ctrl + Shift + C", "Copy the last reply"),
        ("Ctrl + R", "Regenerate the last reply"),
        ("Ctrl + O", "Timeline detail: Normal → Verbose → Summary"),
        ("F2 / double-click the title", "Rename this chat inline (Enter saves, Esc cancels)"),
        ("Win + H", "Windows dictation into the composer"),
    )),
    ("Navigate", (
        ("Ctrl + K", "Command palette: chats, actions, message search"),
        ("Ctrl + M", "Switch between Chat and Council"),
        ("Ctrl + Shift + M", "Memory view: governed facts"),
        ("Ctrl + Shift + R", "Routines view: scheduled runs"),
        ("Ctrl + Tab / Ctrl + Shift + Tab", "Next / previous chat in sidebar order"),
        ("↑ / ↓ · Enter · F2 · Delete in the chat list", "Move the selection · open · rename · delete (with Undo)"),
        ("Ctrl + I", "Context panel: project, changed files, approvals, tasks, memory"),
        ("Ctrl + Shift + P", "Side pane: Diff · Artifact · File"),
        ("Ctrl + F", "Find in this chat"),
        ("F3 / Shift + F3", "Next / previous Find match"),
        ("Ctrl + U (or Ctrl + Alt + U)", "Inbox: approvals, unread replies, finished tasks, errors"),
        ("Ctrl + Shift + U", "Mark this chat unread / read"),
        ("Ctrl + Alt + P", "Pin / unpin this chat"),
        ("Ctrl + Shift + A", "Review approvals"),
        ("Ctrl + Shift + N", "New project"),
        ("Ctrl + E", "Export this chat (Markdown or HTML)"),
        ("Ctrl + B", "Toggle the sidebar"),
        ("Ctrl + ,", "Settings"),
        ("Ctrl + / or F1", "This shortcut list"),
        ("/", "Slash commands: /new /model /theme /project /task /remember /export /facts /schedule /inbox /help"),
        ("Ctrl + Alt + J (global)", "Quick-ask window; press again to raise the main window"),
    )),
    ("Models & look", (
        ("Ctrl + 1 … 5", "Auto · Fast · Reasoning · Coding · Deep"),
        ("Ctrl + T", "Cycle theme (Midnight → Graphite → Paper)"),
        ("Ctrl + +/-", "Zoom text"),
    )),
)


class ShortcutsWindow(tk.Toplevel):
    """The keymap, generated from SHORTCUTS, in a resizable scrollable window."""

    def __init__(self, app: "JarvisDesktop") -> None:
        super().__init__(app.root)
        theme = app.theme
        self.app = app
        self.title("Keyboard shortcuts")
        self.configure(bg=theme.bg)
        self.resizable(True, True)
        self.transient(app.root)
        _apply_titlebar_theme(self, theme.dark)
        header = tk.Frame(self, bg=theme.bg, padx=app.px(24), pady=app.px(14))
        header.pack(fill="x")
        tk.Label(header, text="Keyboard shortcuts", bg=theme.bg, fg=theme.text_strong, font=app.fonts.h1).pack(anchor="w")
        tk.Label(header, text="Chords work anywhere in the window except inside dialogs; rebinding is not available.", bg=theme.bg, fg=theme.faint, font=app.fonts.small).pack(anchor="w", pady=(2, 0))
        self.scroller = ScrollFrame(self, app, bg=theme.bg)
        self.scroller.pack(fill="both", expand=True)
        body = tk.Frame(self.scroller.inner, bg=theme.bg, padx=app.px(24), pady=app.px(6))
        body.pack(fill="both", expand=True)
        self.rows: list[tuple[str, str]] = []
        for group, rows in SHORTCUTS:
            tk.Label(body, text=group.upper(), bg=theme.bg, fg=theme.faint, font=app.fonts.tiny).pack(anchor="w", pady=(12, 4))
            for keys, description in rows:
                self.rows.append((keys, description))
                line = tk.Frame(body, bg=theme.bg)
                line.pack(fill="x", pady=2)
                tk.Label(line, text=keys, bg=theme.surface_alt, fg=theme.muted, font=app.fonts.tiny, padx=7, pady=2).pack(side="right", padx=(12, 0))
                tk.Label(line, text=description, bg=theme.bg, fg=theme.text, font=app.fonts.small, anchor="w", justify="left", wraplength=app.px(380)).pack(side="left", fill="x", expand=True)
        self.bind("<Escape>", lambda _e: self.destroy())
        self.update_idletasks()
        screen_height = work_area_height(self)
        width = max(app.px(600), self.winfo_reqwidth())
        height = settings_window_height(self.scroller.inner.winfo_reqheight() + header.winfo_reqheight() + app.px(12), screen_height)
        self.minsize(app.px(480), app.px(300))
        x = app.root.winfo_rootx() + max(0, (app.root.winfo_width() - width) // 2)
        y = max(0, min(app.root.winfo_rooty() + app.px(40), screen_height - height - app.px(40)))
        self.geometry(f"{width}x{height}+{x}+{y}")


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
        self._meeting_lock = threading.Lock()
        self._convene_pending = False
        self._meeting_active = False
        self._runtime: Any = None
        self.night_plan = night or council.NightPlan()
        self.last_touch = time.time()

    # -- API used by the UI thread ---------------------------------------

    def emit(self, kind: str, payload: Any = None) -> None:
        self.events.put(SessionEvent(kind, payload))

    def convene(self, topic: str, depth: str) -> bool:
        """Queue one manual sitting, synchronously rejecting duplicates."""
        with self._meeting_lock:
            if self._shutdown.is_set() or self._convene_pending or self._meeting_active:
                self.emit(
                    "council_error",
                    "A Council meeting is already active or waiting to start.",
                )
                return False
            self._convene_pending = True
        self.commands.put(("convene", (str(topic), str(depth))))
        return True

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
        # Closing a client is the one bounded cross-thread action that can
        # release a provider blocked in I/O. The worker also closes it in its
        # finally block, and CouncilRuntime.close is idempotent.
        with self._meeting_lock:
            runtime = self._runtime
        if runtime is not None:
            runtime.close()

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
                # A second queued convene can arrive through a stale UI event
                # or a direct queue write. Never defer it until the active
                # sitting closes: that would execute an old request later.
                self.emit(
                    "council_error",
                    "A Council meeting is already active; the duplicate was rejected.",
                )

    def _recent_titles(
        self, ledger: council.NightLedger, night_id: str
    ) -> list[str]:
        titles = [str(row.get("topic", "")) for row in ledger.rows(night_id)]
        for row in council.list_meetings(self.config.data_dir, limit=12):
            title = str(row.get("title", ""))
            title = title.split(" - ", 1)[1] if " - " in title else title
            if title and title not in titles:
                titles.append(title)
        return titles[:16]

    def _file_digest(
        self, night_id: str | None, ledger: council.NightLedger
    ) -> None:
        if not night_id:
            return
        rows = ledger.rows(night_id)
        if not rows:
            return
        try:
            path = council.write_night_digest(
                self.config.data_dir,
                night_id,
                rows,
                ledger.focus(night_id, self.night_plan.focus),
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
        control_memory: Memory | None = None
        ledger: council.NightLedger | None = None
        night_id: str | None = None
        sat_tonight = 0
        last_reason = ""
        interruption_reason: str | None = None
        try:
            control_memory = Memory(self.config.data_dir / "jarvis.db")
            ledger = council.NightLedger(self.config.data_dir)
            ledger.recover_incomplete()
            for recovered_night in ledger.nights():
                self._file_digest(recovered_night, ledger)
            night_id = council.night_key(self.night_plan.window, datetime.now())
            sat_tonight = ledger.count(night_id)
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
                "night": self._night_state(
                    "Night sessions are off" if not self.night_plan.enabled else "Armed",
                    sat_tonight,
                    night_id,
                ),
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
                    if not self._claim_manual_start():
                        self.emit(
                            "council_error",
                            "A Council meeting is already active; the duplicate was rejected.",
                        )
                        continue
                    topic, depth = payload
                    plan = council.DEPTH_PLANS.get(depth, council.DEPTH_PLANS["Standard"])
                    models = council.resolve_models(self.config)
                    if runtime is None:
                        runtime = council.CouncilRuntime(
                            self.config, models=models, memory=control_memory
                        )
                        self._set_runtime(runtime)
                    else:
                        runtime.models = models
                    self._adjourn.clear()
                    self.cancel_event.clear()
                    self.paused.clear()
                    manual_guard = RuntimeGuard(
                        control_memory,
                        self.config,
                        background=False,
                        upstream=lambda: self._shutdown.is_set()
                        or self.cancel_event.is_set(),
                    )
                    runtime.bind_execution(control_memory, manual_guard)
                    self.emit("council_activity", "Checking the model tier")
                    try:
                        note = runtime.verify_tier(self.cancel_event.is_set)
                    except council.CouncilCallBlocked as exc:
                        self.emit(
                            "council_error",
                            f"The Council did not convene: {safe_ui_text(exc, 200)}.",
                        )
                        self._release_meeting()
                        continue
                    meeting = council.open_meeting(topic, plan)
                    unattended = False
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
                        self._file_digest(night_id, ledger)
                        night_id = key
                        sat_tonight = ledger.count(night_id)
                    may_sit, reason = council.night_should_sit(
                        plan, now, time.time() - self.last_touch, False, sat_tonight,
                    )
                    night_started = time.time()
                    night_guard = RuntimeGuard(
                        control_memory,
                        self.config,
                        background=True,
                        upstream=lambda started=night_started: (
                            self._shutdown.is_set()
                            or self.cancel_event.is_set()
                            or self.last_touch > started
                        ),
                    )
                    if may_sit and night_guard():
                        may_sit = False
                        reason = night_guard.reason or "Background autonomy is paused"
                    if reason != last_reason:
                        last_reason = reason
                        self.emit("council_night", self._night_state(reason, sat_tonight, night_id))
                    if not may_sit:
                        continue
                    if not self._claim_night_start():
                        continue
                    sitting_id = ledger.reserve(
                        night_id, plan.cap, plan.focus, created_at=night_started
                    )
                    if sitting_id is None:
                        self._release_meeting()
                        sat_tonight = ledger.count(night_id)
                        continue
                    sat_tonight = ledger.count(night_id)
                    models = council.resolve_models(self.config)
                    if runtime is None:
                        runtime = council.CouncilRuntime(
                            self.config, models=models, memory=control_memory
                        )
                        self._set_runtime(runtime)
                    else:
                        runtime.models = models
                    self._adjourn.clear()
                    self.cancel_event.clear()
                    self.paused.clear()
                    runtime.bind_execution(control_memory, night_guard)
                    self.emit("council_activity", "The chair is choosing tonight's topic")
                    try:
                        note = runtime.verify_tier(self.cancel_event.is_set)
                    except council.CouncilCallBlocked as exc:
                        ledger.finalize(
                            sitting_id,
                            {
                                "topic": "Interrupted Council sitting",
                                "decision": safe_ui_text(exc, 300),
                                "proposals": [],
                                "turns": 0,
                                "folder": "",
                                "report": "",
                            },
                            interrupted=True,
                        )
                        self._file_digest(night_id, ledger)
                        self._release_meeting()
                        continue
                    try:
                        topic, spark = runtime.pick_topic(
                            plan,
                            self._recent_titles(ledger, night_id),
                            cancelled=self.cancel_event.is_set,
                            budget_scope=f"council:{sitting_id}",
                        )
                    except Exception as exc:
                        if night_guard():
                            ledger.finalize(
                                sitting_id,
                                {
                                    "topic": "Interrupted Council sitting",
                                    "decision": night_guard.reason or "Council sitting interrupted",
                                    "proposals": [],
                                    "turns": 0,
                                    "folder": "",
                                    "report": "",
                                },
                                interrupted=True,
                            )
                            self._file_digest(night_id, ledger)
                            self._release_meeting()
                            continue
                        topic, spark = council.bounded_text(plan.focus, 140), ""
                        note = note or f"The chair could not pick a topic ({safe_ui_text(exc, 160)}); sitting on the standing focus instead."
                    ledger.set_topic(sitting_id, topic)
                    meeting = council.open_meeting(
                        topic,
                        council.DEPTH_PLANS[plan.depth],
                        meeting_id=sitting_id,
                    )
                    unattended = True
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

                execution_guard = runtime.execution_guard
                if callable(execution_guard) and execution_guard():
                    interruption_reason = str(
                        getattr(execution_guard, "reason", "")
                        or "Council execution was stopped"
                    )
                    self._adjourn.set()
                    self.cancel_event.set()

                # The operator came back during an unattended sitting: file it
                # and go quiet. Speaking to the council is not "coming back".
                if unattended and self.last_touch > meeting.started_at and not self._adjourn.is_set():
                    interruption_reason = "The unattended sitting ended when the operator returned."
                    self._adjourn.set()
                    self.cancel_event.set()

                if self._adjourn.is_set():
                    self._finalize_meeting(
                        runtime,
                        meeting,
                        unattended=unattended,
                        ledger=ledger,
                        night_id=night_id,
                        interruption=interruption_reason,
                    )
                    meeting = None
                    unattended = False
                    interruption_reason = None
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
                    self._finalize_meeting(
                        runtime,
                        meeting,
                        unattended=unattended,
                        ledger=ledger,
                        night_id=night_id,
                    )
                    meeting = None
                    unattended = False
        except Exception as exc:
            self.emit(
                "council_error",
                f"The council could not continue ({type(exc).__name__}): "
                f"{safe_ui_text(exc, 300)}",
            )
        finally:
            if meeting is not None and runtime is not None and ledger is not None:
                reason = (
                    "The Council sitting was interrupted while the desktop shut down."
                    if self._shutdown.is_set()
                    else "The Council sitting was interrupted by an internal runtime error."
                )
                try:
                    self._finalize_meeting(
                        runtime,
                        meeting,
                        unattended=unattended,
                        ledger=ledger,
                        night_id=night_id,
                        interruption=reason,
                    )
                except Exception as exc:
                    self.emit(
                        "council_error",
                        "The interrupted Council sitting could not be filed "
                        f"({type(exc).__name__}): {safe_ui_text(exc, 200)}",
                    )
            if runtime is not None:
                runtime.close()
            self._set_runtime(None)
            if ledger is not None:
                ledger.close()
            if control_memory is not None:
                control_memory.close()
            with self._meeting_lock:
                self._convene_pending = False
                self._meeting_active = False

    def _finalize_meeting(
        self,
        runtime: council.CouncilRuntime,
        meeting: council.CouncilMeeting,
        *,
        unattended: bool,
        ledger: council.NightLedger,
        night_id: str | None,
        interruption: str | None = None,
    ) -> None:
        if interruption and not any(
            turn.kind == "notice" and interruption in turn.text
            for turn in meeting.turns
        ):
            meeting.add_turn(
                council.CHAIR_KEY,
                council.OPERATOR_KEY,
                "notice",
                interruption,
                meeting.item,
            )
        self.emit("council_activity", "Filing the report")
        artifacts = runtime.finalize(meeting, self.config.data_dir)
        if unattended:
            ledger.finalize(
                meeting.meeting_id,
                council.night_row(meeting),
                interrupted=bool(interruption),
            )
            self._file_digest(night_id, ledger)
        self.emit(
            "council_closed",
            self._state(
                meeting,
                runtime.models,
                {
                    "artifacts": artifacts,
                    "unattended": unattended,
                    "history": council.list_meetings(
                        self.config.data_dir, limit=12
                    ),
                },
            ),
        )
        self._release_meeting()

    def _set_runtime(self, runtime: Any) -> None:
        with self._meeting_lock:
            self._runtime = runtime

    def _release_meeting(self) -> None:
        with self._meeting_lock:
            self._meeting_active = False

    def _claim_night_start(self) -> bool:
        with self._meeting_lock:
            if self._meeting_active or self._convene_pending or self._shutdown.is_set():
                return False
            self._meeting_active = True
            return True

    def _claim_manual_start(self) -> bool:
        with self._meeting_lock:
            self._convene_pending = False
            if self._meeting_active or self._shutdown.is_set():
                return False
            self._meeting_active = True
            return True


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
        self._labels: dict[str, tuple[float, float]] = {}
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
        self._compute_labels()
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
        label_x, label_y = self._labels.get(seat.key, (spot.plate_x, spot.plate_y))
        name = self.create_text(
            label_x, label_y - self.unit * 0.30,
            text=seat.name, fill=theme.text,
            font=self.app.fonts.small_bold, tags=("plate",),
        )
        badge = self.create_text(
            label_x, label_y + self.unit * 0.14,
            text=self.badges.get(seat.key, ""),
            fill=mix_colors(theme.faint, seat.accent, 0.35),
            font=self.app.fonts.tiny, tags=("plate",),
        )
        self._dynamic.setdefault(seat.key, {}).update(
            {"plate": body, "plate_name": name, "plate_badge": badge}
        )

    def _compute_labels(self) -> None:
        """Seat labels sit on a ring outside the avatars, at the seat's own angle,
        and are pushed apart until none overlap (J9)."""
        font = self.app.fonts.small_bold
        boxes: list[tuple[str, float, float, float, float]] = []
        for seat in self.seats:
            spot = self._layout[seat.key]
            angle = math.radians(SEAT_ANGLES.get(seat.key, -90.0))
            reach = self.unit * (2.9 if seat.chair else 2.2) * max(0.7, spot.scale)
            x = self.cx + (self.rx + reach) * math.cos(angle)
            y = self.cy + (self.ry + reach * 0.85) * math.sin(angle) - self.unit * 0.55
            width = max(font.measure(seat.name), font.measure(self.badges.get(seat.key, ""))) + self.unit * 0.6
            boxes.append((seat.key, x, y, width, self.unit * 0.95))
        self._labels = resolve_label_collisions(boxes, self.unit * 0.6)

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
            title, text="", bg=theme.surface_alt, fg=theme.faint, font=app.fonts.tiny,
            anchor="w", justify="left", padx=7, pady=1,
        )
        self.models_label.pack(side="left", padx=(app.px(10), 0))
        self.models_tooltip = Tooltip(self.models_label, "", app)

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
        self._floor_labels: list[tk.Label] = [self.floor_hint, self.empty_label]
        parent.bind("<Configure>", self._fit_floor, add="+")

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

    def _fit_floor(self, event: Any = None) -> None:
        """Floor labels wrap to the column's real width — never clipped mid-word (J9)."""
        try:
            width = int(event.width) if event is not None else self.transcript.winfo_width()
        except (AttributeError, TypeError, ValueError, tk.TclError):
            return
        wrap = max(self.app.px(160), width - self.app.px(52))
        for label in list(getattr(self, "_floor_labels", [])):
            try:
                if label.winfo_exists():
                    label.configure(wraplength=wrap)
            except tk.TclError:
                continue

    def set_seats(self, badges: dict[str, str], note: str) -> None:
        self.table.set_badges(badges)
        # The setup sentence lives in the badge's tooltip and Settings → Storage
        # (J9); the badge itself names the models in use.
        note = safe_ui_text(note, 300)
        self.app.council_note = note
        chair = badges.get("jarvis") or next(iter(badges.values()), "") if badges else ""
        short = compact_activity(note.split(" — ")[0].split(". ")[0], 48) if note else ""
        self.models_label.configure(text=short or compact_activity(chair, 40) or "Models")
        self.models_tooltip.text = note or "Council models: see Settings → Storage."

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
            # The room only animates while it is on screen; a sitting that runs
            # behind the chat view costs nothing until the view comes back.
            if getattr(self.app, "view", "chat") == "council":
                self.table.start()
            else:
                self.table.stop()

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


class ContextPanel(tk.Frame):
    """What Jarvis is working with right now: project, files, approvals, tasks, memory."""

    def __init__(self, master: tk.Misc, app: "JarvisDesktop") -> None:
        theme = app.theme
        super().__init__(master, bg=theme.panel, width=app.px(312))
        self.app = app
        self.theme = theme
        self.pack_propagate(False)
        self.data: dict[str, Any] = {}
        head = tk.Frame(self, bg=theme.panel, padx=app.px(14), pady=app.px(12))
        head.pack(fill="x")
        tk.Label(head, text="CONTEXT", bg=theme.panel, fg=theme.faint, font=app.fonts.tiny).pack(side="left")
        IconButton(head, app, "×", app.toggle_context, tooltip="Hide context (Ctrl+I)").pack(side="right")
        IconButton(head, app, "⟳", app.refresh_context, tooltip="Refresh").pack(side="right")
        self.scroller = ScrollFrame(self, app, bg=theme.panel)
        self.scroller.pack(fill="both", expand=True)
        self.render()

    def _section(self, title: str, action: tuple[str, Callable[[], None]] | None = None) -> tk.Frame:
        theme = self.theme
        app = self.app
        box = tk.Frame(self.scroller.inner, bg=theme.panel, padx=app.px(12), pady=app.px(6))
        box.pack(fill="x")
        head = tk.Frame(box, bg=theme.panel)
        head.pack(fill="x", pady=(0, 4))
        tk.Label(head, text=title.upper(), bg=theme.panel, fg=theme.faint, font=app.fonts.tiny, anchor="w").pack(side="left")
        if action is not None:
            label, command = action
            link = tk.Label(head, text=label, bg=theme.panel, fg=theme.accent, font=app.fonts.tiny, cursor="hand2")
            link.pack(side="right")
            link.bind("<Button-1>", lambda _e: command())
        body = tk.Frame(box, bg=theme.surface, highlightbackground=theme.border, highlightthickness=1, padx=app.px(10), pady=app.px(8))
        body.pack(fill="x")
        return body

    def _row(self, parent: tk.Misc, title: str, meta: str = "", command: Callable[[], None] | None = None, *, tooltip: str = "", actions: list[tuple[str, Callable[[], None]]] | None = None, wrap: bool = False) -> None:
        theme = self.theme
        app = self.app
        row = tk.Frame(parent, bg=theme.surface, cursor="hand2" if command else "arrow", takefocus=1 if command else 0, highlightthickness=1, highlightbackground=theme.surface, highlightcolor=theme.accent)
        row.pack(fill="x", pady=1)
        label = tk.Label(row, text=title, bg=theme.surface, fg=theme.text, font=app.fonts.small, anchor="w", justify="left", wraplength=app.px(262) if wrap else 0)
        label.pack(fill="x")
        if command:
            row.bind("<Return>", lambda _e: (command(), "break")[1])
            row.bind("<space>", lambda _e: (command(), "break")[1])
        widgets = [row, label]
        if meta or actions:
            meta_row = tk.Frame(row, bg=theme.surface)
            meta_row.pack(fill="x")
            widgets.append(meta_row)
            for name, action in list(actions or [])[:3]:
                link = tk.Label(meta_row, text=name, bg=theme.surface, fg=theme.accent, font=app.fonts.tiny, cursor="hand2", padx=4)
                link.pack(side="right")
                link.bind("<Button-1>", lambda _e, run=action: (run(), "break")[1])
            info = tk.Label(meta_row, text=meta, bg=theme.surface, fg=theme.faint, font=app.fonts.tiny, anchor="w")
            info.pack(side="left", fill="x", expand=True)
            widgets.append(info)
        if command:
            for widget in widgets:
                widget.bind("<Button-1>", lambda _e: command())
                widget.bind("<Enter>", lambda _e, items=tuple(widgets): [item.configure(bg=theme.surface_hover) for item in items])
                widget.bind("<Leave>", lambda _e, items=tuple(widgets): [item.configure(bg=theme.surface) for item in items])
        if tooltip:
            Tooltip(label, tooltip, app)

    def set_data(self, data: dict[str, Any]) -> None:
        self.data = dict(data or {})
        self.render()

    def render(self, *, force: bool = False) -> None:
        theme = self.theme
        app = self.app
        data = self.data
        digest = render_digest([data, app.current_project_name(), app.workspace])
        if not force and getattr(self, "_digest", None) == digest and self.scroller.inner.winfo_children():
            return  # unchanged store data: keep the panel as it is
        self._digest = digest
        for child in self.scroller.inner.winfo_children():
            child.destroy()
        project = self._section("Project", ("Switch", app.choose_project))
        tk.Label(project, text=data.get("project_name") or app.current_project_name(), bg=theme.surface, fg=theme.text_strong, font=app.fonts.label_bold, anchor="w").pack(fill="x")
        workspace = str(data.get("workspace") or app.workspace or "")
        if workspace:
            path = tk.Label(project, text=workspace, bg=theme.surface, fg=theme.faint, font=app.fonts.tiny, anchor="w", wraplength=app.px(250), justify="left", cursor="hand2")
            path.pack(fill="x", pady=(2, 4))
            path.bind("<Button-1>", lambda _e: app.open_path(workspace))
            Tooltip(path, "Open the workspace folder", app)
        actions = tk.Frame(project, bg=theme.surface)
        actions.pack(fill="x")
        RoundButton(actions, app, "Open folder", lambda: app.open_path(workspace), kind="ghost", padx=9, pady=3, font=app.fonts.tiny).pack(side="left")
        RoundButton(actions, app, "Remember a fact", app.remember_fact_dialog, kind="ghost", padx=9, pady=3, font=app.fonts.tiny, tooltip="Store a governed project fact Jarvis will use in this project").pack(side="left", padx=(6, 0))

        files = data.get("files") or []
        section = self._section("Changed in this chat" if data.get("conversation_id") else "Recent files", ("Open folder", lambda: app.open_path(workspace)))
        if not files:
            tk.Label(section, text="No files have changed since this chat started.", bg=theme.surface, fg=theme.faint, font=app.fonts.tiny, anchor="w", wraplength=app.px(250), justify="left").pack(fill="x")
        for item in files[:12]:
            when = format_clock(float(item.get("modified_at") or 0))
            self._row(
                section, item.get("relative") or item.get("path", ""), f"{when} · {item.get('size', 0):,} B",
                lambda target=item.get("path", ""): app.open_path(target), tooltip="Open · right-click for more",
                actions=[("View", lambda target=item.get("path", ""): app.view_file_in_pane(target))],
            )
            last = section.winfo_children()[-1]
            for widget in (last, *last.winfo_children()):
                widget.bind("<Button-3>", lambda event, target=item: app.file_menu(event, target))

        approvals = data.get("pending_approvals") or []
        section = self._section("Needs you", ("All approvals", app.show_approvals))
        if not approvals:
            tk.Label(section, text="Nothing is waiting for approval.", bg=theme.surface, fg=theme.faint, font=app.fonts.tiny, anchor="w").pack(fill="x")
        for row in approvals:
            card = ApprovalCard(
                section, app, row, approval_id=int(row.get("id") or 0), compact=True,
                on_decide=lambda aid, approve, scope, reason: app.decide_context_approval(aid, approve, scope=scope, reason=reason),
            )
            card.pack(fill="x", pady=2)
            card.headline.configure(cursor="hand2")
            card.headline.bind("<Button-1>", lambda _e, target=row: app.scroll_to_approval(int(target.get("id") or 0)))
            Tooltip(card.headline, "Show this approval in the chat", app)

        tasks = data.get("tasks") or []
        section = self._section("Background tasks", ("Queue…", app.queue_task_dialog))
        open_tasks = [row for row in tasks if str(row.get("status", "")).lower() in {"queued", "running", "leased", "retry", "pending"}]
        if open_tasks and data.get("worker_alive") is False:
            tk.Label(section, text="Worker offline — queued tasks will not run until `python -m jarvis worker` (or the worker service) is started.", bg=theme.surface, fg=theme.warning, font=app.fonts.tiny, anchor="w", wraplength=app.px(250), justify="left").pack(fill="x", pady=(0, 4))
        if not tasks:
            tk.Label(section, text="No background tasks yet. Queue one to have the Jarvis worker do it while you keep chatting.", bg=theme.surface, fg=theme.faint, font=app.fonts.tiny, anchor="w", wraplength=app.px(250), justify="left").pack(fill="x")
        for row in (open_tasks + [row for row in tasks if row not in open_tasks])[:8]:
            self._row(section, f"#{row.get('id')} · {row.get('prompt')}", context_task_meta(row), lambda target=row: app.open_task_detail(target), tooltip="Open this task: prompt, attempts, error and result")

        memories = context_memory_rows(data.get("memories") or [])
        section = self._section("Memory", ("Search", lambda: app.open_palette_with("memory ")))
        if not memories:
            tk.Label(section, text="Nothing stored yet.", bg=theme.surface, fg=theme.faint, font=app.fonts.tiny, anchor="w").pack(fill="x")
        for row in memories:
            meta = " · ".join(bit for bit in (str(row.get("kind") or ""), relative_or_clock(row.get("created_at"))) if bit)
            self._row(section, safe_ui_text(row.get("content", ""), 600), meta, lambda text=row.get("content", ""): app.copy_text(text), tooltip="Click to copy", wrap=True)


def relative_or_clock(value: Any) -> str:
    stamp = _iso_to_epoch(str(value or ""))
    if not stamp:
        return ""
    delta = max(0.0, time.time() - stamp)
    if delta < 60:
        return "just now"
    if delta < 3600:
        return f"{int(delta // 60)}m ago"
    if delta < 86400:
        return f"{int(delta // 3600)}h ago"
    return f"{int(delta // 86400)}d ago"


class FindBar(tk.Frame):
    """Ctrl+F: highlight matches across every text block in the chat."""

    def __init__(self, master: tk.Misc, app: "JarvisDesktop") -> None:
        theme = app.theme
        super().__init__(master, bg=theme.surface_alt, padx=app.px(12), pady=app.px(6))
        self.app = app
        self.matches: list[tuple[AutoText, str]] = []
        self.index = -1
        tk.Label(self, text="Find", bg=theme.surface_alt, fg=theme.muted, font=app.fonts.small).pack(side="left")
        self.entry = tk.Entry(self, bg=theme.surface, fg=theme.text, insertbackground=theme.accent, bd=0, highlightthickness=1, highlightbackground=theme.border, highlightcolor=theme.accent, font=app.fonts.small, width=34)
        self.entry.pack(side="left", padx=(8, 8), ipady=4)
        self.entry.bind("<KeyRelease>", lambda _e: self.search())
        self.entry.bind("<Return>", lambda _e: self.step(1))
        self.entry.bind("<Shift-Return>", lambda _e: self.step(-1))
        self.entry.bind("<Escape>", lambda _e: app.toggle_find())
        self.count = tk.Label(self, text="", bg=theme.surface_alt, fg=theme.faint, font=app.fonts.tiny)
        self.count.pack(side="left")
        IconButton(self, app, "×", app.toggle_find, tooltip="Close (Esc)").pack(side="right")
        IconButton(self, app, "›", lambda: self.step(1), tooltip="Next (Enter)").pack(side="right")
        IconButton(self, app, "‹", lambda: self.step(-1), tooltip="Previous (Shift+Enter)").pack(side="right")

    def clear(self) -> None:
        for widget in self.app._all_autotexts(self.app.messages_view.inner):
            try:
                widget.tag_remove("find", "1.0", "end")
                widget.tag_remove("find-current", "1.0", "end")
            except tk.TclError:
                pass
        self.matches = []
        self.index = -1
        self.count.configure(text="")

    def search(self) -> None:
        query = self.entry.get()
        self.clear()
        if len(query) < 2:
            return
        theme = self.app.theme
        for widget in self.app._all_autotexts(self.app.messages_view.inner):
            widget.tag_configure("find", background=theme.selection)
            widget.tag_configure("find-current", background=theme.accent, foreground=theme.accent_ink)
            start = "1.0"
            while True:
                position = widget.search(query, start, stopindex="end", nocase=True)
                if not position:
                    break
                end = f"{position}+{len(query)}c"
                widget.tag_add("find", position, end)
                self.matches.append((widget, position))
                start = end
        self.count.configure(text=f"{len(self.matches)} match{'es' if len(self.matches) != 1 else ''}" if self.matches else "No matches")
        if self.matches:
            self.step(1)

    def step(self, delta: int) -> str:
        if not self.matches:
            return "break"
        if 0 <= self.index < len(self.matches):
            widget, position = self.matches[self.index]
            widget.tag_remove("find-current", position, f"{position}+{len(self.entry.get())}c")
        self.index = (self.index + delta) % len(self.matches)
        widget, position = self.matches[self.index]
        widget.tag_add("find-current", position, f"{position}+{len(self.entry.get())}c")
        self.count.configure(text=f"{self.index + 1} of {len(self.matches)}")
        self.app.scroll_widget_into_view(widget)
        return "break"


SETTINGS_MIN_WIDTH = 620
SETTINGS_SCREEN_FRACTION = 0.8


def settings_window_height(required: int, screen_height: int, fraction: float = SETTINGS_SCREEN_FRACTION, minimum: int = 320) -> int:
    """The settings window shows everything it can, capped at a fraction of the screen."""
    cap = int(max(1, screen_height) * fraction)
    return max(minimum, min(int(required), cap))


def work_area_height(widget: tk.Misc) -> int:
    """The usable screen height (Windows work area, i.e. without the taskbar);
    the full screen height elsewhere or when the query fails."""
    try:
        fallback = int(widget.winfo_screenheight())
    except tk.TclError:
        fallback = 900
    if sys.platform != "win32":
        return fallback
    try:
        rect = ctypes.wintypes.RECT()
        if ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0):  # SPI_GETWORKAREA
            height = int(rect.bottom - rect.top)
            if 200 < height <= fallback:
                return height
    except Exception:
        pass
    return fallback


class SettingsWindow(tk.Toplevel):
    """Settings in a scrollable, resizable window that never runs off the screen.

    The sections live inside a :class:`ScrollFrame`; the window opens at the
    height of its content capped at 80 % of the screen. Theme buttons re-skin
    the window in place instead of closing it.
    """

    def __init__(self, app: "JarvisDesktop") -> None:
        super().__init__(app.root)
        self.app = app
        self.title("Jarvis settings")
        self.transient(app.root)
        self.resizable(True, True)
        self.scroller: ScrollFrame | None = None
        self.grants_box: tk.Frame | None = None
        self._populate()
        self.bind("<Escape>", lambda _e: self.destroy())
        self.update_idletasks()
        self.minsize(app.px(SETTINGS_MIN_WIDTH), app.px(320))
        width = max(app.px(SETTINGS_MIN_WIDTH), self.winfo_reqwidth())
        screen_height = work_area_height(self)
        inner_height = self.scroller.inner.winfo_reqheight() if self.scroller is not None else 0
        chrome = self.winfo_reqheight() - (self.scroller.winfo_reqheight() if self.scroller is not None else 0)
        height = settings_window_height(inner_height + chrome + app.px(8), screen_height)
        x = app.root.winfo_rootx() + (app.root.winfo_width() - width) // 2
        y = max(0, app.root.winfo_rooty() + app.px(40))
        if y + height > screen_height:
            y = max(0, screen_height - height - app.px(40))
        self.geometry(f"{width}x{height}+{x}+{y}")

    def _restyle(self, key: str) -> None:
        """Apply a theme and re-skin this window in place; the window stays open."""
        self.app.set_theme(key)
        try:
            if self.winfo_exists():
                self._populate()
                self.lift()
        except tk.TclError:
            pass

    def _populate(self) -> None:
        app = self.app
        theme = app.theme
        self.configure(bg=theme.bg)
        _apply_titlebar_theme(self, theme.dark)
        for child in self.winfo_children():
            child.destroy()
        header = tk.Frame(self, bg=theme.bg, padx=app.px(24), pady=app.px(14))
        header.pack(fill="x")
        tk.Label(header, text="Settings", bg=theme.bg, fg=theme.text_strong, font=app.fonts.h1).pack(anchor="w")
        tk.Label(header, text="Everything here is stored locally in data/desktop_ui.json.", bg=theme.bg, fg=theme.faint, font=app.fonts.small).pack(anchor="w", pady=(2, 0))
        self.scroller = ScrollFrame(self, app, bg=theme.bg)
        self.scroller.pack(fill="both", expand=True)
        body = tk.Frame(self.scroller.inner, bg=theme.bg, padx=app.px(24), pady=app.px(6))
        body.pack(fill="both", expand=True)

        def section(title: str) -> tk.Frame:
            tk.Label(body, text=title.upper(), bg=theme.bg, fg=theme.faint, font=app.fonts.tiny).pack(anchor="w", pady=(10, 4))
            frame = tk.Frame(body, bg=theme.surface, highlightbackground=theme.border, highlightthickness=1, padx=app.px(12), pady=app.px(8))
            frame.pack(fill="x")
            return frame

        def row(parent: tk.Misc, label: str, detail: str = "") -> tk.Frame:
            line = tk.Frame(parent, bg=theme.surface)
            line.pack(fill="x", pady=3)
            text = tk.Frame(line, bg=theme.surface)
            text.pack(side="left", fill="x", expand=True)
            tk.Label(text, text=label, bg=theme.surface, fg=theme.text, font=app.fonts.small_bold, anchor="w").pack(fill="x")
            if detail:
                tk.Label(text, text=detail, bg=theme.surface, fg=theme.faint, font=app.fonts.tiny, anchor="w", wraplength=app.px(360), justify="left").pack(fill="x")
            return line

        self.section_titles: list[str] = []

        def titled(title: str) -> tk.Frame:
            self.section_titles.append(title)
            return section(title)

        def check(parent: tk.Misc, label: str, detail: str, key: str, *, default: bool = True, apply: Callable[[bool], None] | None = None) -> tk.BooleanVar:
            line = row(parent, label, detail)
            variable = tk.BooleanVar(value=bool(app.settings.get(key, default)))
            setattr(self, f"{key}_var", variable)

            def _apply(setting=key, var=variable) -> None:
                app.settings.set(setting, var.get())
                if apply is not None:
                    apply(bool(var.get()))

            tk.Checkbutton(line, variable=variable, bg=theme.surface, activebackground=theme.surface, selectcolor=theme.surface_alt, fg=theme.text, command=_apply).pack(side="right")
            return variable

        def topmost(value: bool) -> None:
            try:
                app.root.wm_attributes("-topmost", value)
            except tk.TclError:
                pass

        # General · Appearance · Notifications · Standing approvals · Archived chats · Shortcuts · Storage (contract B8)
        general = titled("General")
        line = row(general, "Default model profile", "Used for new messages until you pick a chip in the composer.")
        self.model_var = tk.StringVar(value=app.model_label)
        model_box = ttk.Combobox(line, textvariable=self.model_var, values=MODEL_CHOICES, state="readonly", width=14, style=COMBOBOX_STYLE)
        model_box.pack(side="right")
        model_box.bind("<<ComboboxSelected>>", lambda _e: app.set_model(self.model_var.get()))
        check(general, "Global hotkey", f"{app.hotkey_label()} brings Jarvis to the front from any app. Restart to apply a change.", "hotkey")
        check(general, "Quick-ask window on the global hotkey", f"{app.hotkey_label()} opens a small box; press again to raise the main window.", "companion")
        check(general, "Keep the window on top", "Also in the palette.", "topmost", default=False, apply=topmost)
        line = row(general, "Context panel", "Show project, changed files, approvals, tasks and memory beside the chat.")
        self.context_var = tk.BooleanVar(value=app.context_visible)
        tk.Checkbutton(line, variable=self.context_var, bg=theme.surface, activebackground=theme.surface, selectcolor=theme.surface_alt, fg=theme.text, command=lambda: app.set_context_visible(self.context_var.get())).pack(side="right")
        check(general, "Flash the taskbar when a reply finishes", "Only when the window is not focused.", "flash")

        look = titled("Appearance")
        line = row(look, "Theme", "Midnight is black and teal, Graphite is neutral grey, Paper is warm light.")
        self.theme_buttons: list[RoundButton] = []
        for key in THEME_ORDER:  # left to right, in the order the description names them
            button = RoundButton(line, app, THEMES[key].name, lambda target=key: self._restyle(target), kind="active" if key == theme.key else "ghost", padx=9, pady=3, font=app.fonts.tiny)
            button.pack(side="left", padx=(0, 4))
            self.theme_buttons.append(button)
        line = row(look, "Text size", f"Zoom {app.zoom:+d} · Ctrl + / Ctrl −")
        RoundButton(line, app, "A+", lambda: app.zoom_text(1), kind="ghost", padx=9, pady=3, font=app.fonts.tiny).pack(side="right")
        RoundButton(line, app, "A−", lambda: app.zoom_text(-1), kind="ghost", padx=9, pady=3, font=app.fonts.tiny).pack(side="right", padx=(0, 4))

        notify = titled("Notifications")
        line = row(notify, "Reply finished", "Windows notification when a reply lands (tray balloon, or a toast when the tray icon is off).")
        self.notify_reply_var = tk.StringVar(value=str(app.settings.get("notify_reply", "unfocused")))
        reply_box = ttk.Combobox(line, textvariable=self.notify_reply_var, values=("never", "unfocused", "always"), state="readonly", width=11, style=COMBOBOX_STYLE)
        reply_box.pack(side="right")
        reply_box.bind("<<ComboboxSelected>>", lambda _e: app.settings.set("notify_reply", self.notify_reply_var.get()))
        check(notify, "Approval needed", "Always worth a notification; it blocks the request.", "notify_approval")
        check(notify, "Background task finished", "Reported from the task store when the context refreshes.", "notify_task")
        check(notify, "Tray icon", "Notification-area icon with Open · New chat · Quit; carries the balloons. Restart to apply.", "tray")

        grants = titled("Standing approvals")
        self.grants_box = grants
        self.render_grants(app.grants)

        self.archived_box = titled("Archived chats")
        self.render_archived()

        shortcuts = titled("Shortcuts")
        tk.Label(shortcuts, text="Rebinding is not available; the full list opens with Ctrl+/ or F1.", bg=theme.surface, fg=theme.faint, font=app.fonts.tiny, anchor="w").pack(fill="x")
        RoundButton(shortcuts, app, "Show all shortcuts", app.show_shortcuts, kind="ghost", padx=9, pady=3, font=app.fonts.tiny).pack(anchor="w", pady=(4, 0))

        storage = titled("Storage")
        for label, value in (("Workspace", app.workspace), ("Data", app.data_dir)):
            line = row(storage, label, value or "unknown")
            RoundButton(line, app, "Open", lambda target=value: app.open_path(target), kind="ghost", padx=9, pady=3, font=app.fonts.tiny).pack(side="right")
        for profile, name in sorted(app.model_names.items()):
            if name:
                row(storage, f"{profile.title()} model", name)
        note = str(getattr(app, "council_note", "") or "")
        if note:
            row(storage, "Council models", note)

    def render_archived(self) -> None:
        """Every archived chat with an Unarchive button (B8)."""
        app = self.app
        theme = app.theme
        box = getattr(self, "archived_box", None)
        if box is None or not box.winfo_exists():
            return
        for child in box.winfo_children():
            child.destroy()
        rows = [chat for chat in app.chats if app.chat_meta.flag(chat["id"], "archived") and chat["id"] not in app._hidden_chats]
        if not rows:
            tk.Label(box, text="No archived chats. Archive hides a chat from the sidebar without deleting it.", bg=theme.surface, fg=theme.faint, font=app.fonts.tiny, anchor="w", wraplength=app.px(420), justify="left").pack(fill="x")
            return
        for chat in rows[:60]:
            line = tk.Frame(box, bg=theme.surface)
            line.pack(fill="x", pady=3)
            tk.Label(line, text=compact_activity(chat.get("title") or "Untitled", 60), bg=theme.surface, fg=theme.text, font=app.fonts.small, anchor="w").pack(side="left", fill="x", expand=True)

            def unarchive(target: int = int(chat["id"])) -> None:
                app.toggle_archive(target)
                self.render_archived()

            RoundButton(line, app, "Unarchive", unarchive, kind="ghost", padx=9, pady=3, font=app.fonts.tiny).pack(side="right")

    def render_grants(self, rows: list[dict[str, Any]]) -> None:
        """Every standing 'This chat' / 'Always' grant, each with a Revoke button."""
        app = self.app
        theme = app.theme
        box = getattr(self, "grants_box", None)
        if box is None or not box.winfo_exists():
            return
        for child in box.winfo_children():
            child.destroy()
        if not rows:
            tk.Label(box, text="No standing approvals. Approve once stays one-shot; This chat / Always grants appear here and can be revoked.", bg=theme.surface, fg=theme.faint, font=app.fonts.tiny, anchor="w", wraplength=app.px(420), justify="left").pack(fill="x")
            return
        for grant in rows[:40]:
            line = tk.Frame(box, bg=theme.surface)
            line.pack(fill="x", pady=3)
            column = tk.Frame(line, bg=theme.surface)
            column.pack(side="left", fill="x", expand=True)
            kind = "always" if grant.get("kind") == "always" else f"this chat ({grant.get('scope') or 'conversation'})"
            tk.Label(column, text=f"{grant.get('action')} · {kind}", bg=theme.surface, fg=theme.text, font=app.fonts.small_bold, anchor="w").pack(fill="x")
            tk.Label(column, text=compact_activity(grant.get("resource") or "", 120), bg=theme.surface, fg=theme.faint, font=app.fonts.mono_small, anchor="w", wraplength=app.px(360), justify="left").pack(fill="x")
            when = relative_or_clock(grant.get("created_at")) or ""
            expires = grant.get("expires_at") or ""
            meta = f"granted {when}" + (f" · expires {relative_or_clock(expires) or expires}" if expires else "")
            tk.Label(column, text=meta, bg=theme.surface, fg=theme.faint, font=app.fonts.tiny, anchor="w").pack(fill="x")
            RoundButton(line, app, "Revoke", lambda target=grant: app.revoke_grant(int(target.get("id") or 0)), kind="danger", padx=9, pady=3, font=app.fonts.tiny).pack(side="right")


PROVIDER_CHOICES = (
    ("ollama", "Ollama", "Local models on this computer", "No account or cloud connection"),
    ("claude", "Claude", "Claude subscription through Claude CLI", "Requires Claude CLI sign-in"),
    ("codex", "ChatGPT", "ChatGPT subscription through Codex CLI", "Requires Codex CLI sign-in"),
    ("both", "Smart split", "Claude for dialogue, ChatGPT for coding", "Requires both CLI sign-ins"),
)

class ProviderSettingsWindow(tk.Toplevel):
    """Provider picker that stores routing only; credentials stay in vendor CLIs."""

    def __init__(self, app: "JarvisDesktop") -> None:
        super().__init__(app.root)
        self.app = app
        self.theme = app.theme
        self.title("Model provider settings")
        self.configure(bg=self.theme.bg)
        self.geometry(f"{app.px(640)}x{app.px(660)}")
        self.minsize(app.px(560), app.px(620))
        self.transient(app.root)
        _apply_titlebar_theme(self, self.theme.dark)
        self.choice = tk.StringVar(value=app.provider_choice)
        self.status = tk.StringVar(value="")

        body = tk.Frame(self, bg=self.theme.bg, padx=app.px(24), pady=app.px(20))
        body.pack(fill="both", expand=True)
        tk.Label(
            body, text="Model provider", bg=self.theme.bg,
            fg=self.theme.text_strong, font=app.fonts.h1,
        ).pack(anchor="w")
        tk.Label(
            body,
            text="Choose where Jarvis runs model requests. Ollama stays the local default; subscription choices use the official CLIs.",
            bg=self.theme.bg, fg=self.theme.muted, font=app.fonts.small,
            wraplength=app.px(570), justify="left",
        ).pack(anchor="w", pady=(4, 14))

        for key, name, detail, requirement in PROVIDER_CHOICES:
            card = tk.Frame(
                body, bg=self.theme.surface,
                highlightbackground=self.theme.border, highlightthickness=1,
                padx=app.px(12), pady=app.px(9), cursor="hand2",
            )
            card.pack(fill="x", pady=(0, 7))
            radio = tk.Radiobutton(
                card, variable=self.choice, value=key, text=name,
                bg=self.theme.surface, fg=self.theme.text_strong,
                activebackground=self.theme.surface,
                activeforeground=self.theme.text_strong,
                selectcolor=self.theme.surface_alt,
                font=app.fonts.label_bold, anchor="w", cursor="hand2",
                highlightthickness=0, bd=0,
            )
            radio.pack(fill="x")
            detail_label = tk.Label(
                card, text=detail, bg=self.theme.surface, fg=self.theme.text,
                font=app.fonts.small, anchor="w",
            )
            detail_label.pack(fill="x", padx=(app.px(24), 0))
            requirement_label = tk.Label(
                card, text=requirement, bg=self.theme.surface, fg=self.theme.faint,
                font=app.fonts.tiny, anchor="w",
            )
            requirement_label.pack(fill="x", padx=(app.px(24), 0), pady=(2, 0))
            for widget in (card, detail_label, requirement_label):
                widget.bind("<Button-1>", lambda _event, target=key: self.choice.set(target))

        tk.Label(
            body,
            text="Jarvis never stores your Claude or ChatGPT password, session cookie, or API key. Sign-in remains owned by Claude CLI or Codex CLI.",
            bg=self.theme.accent_soft, fg=self.theme.text,
            font=app.fonts.tiny, wraplength=app.px(560), justify="left",
            padx=10, pady=7,
        ).pack(fill="x", pady=(3, 10))
        self.status_label = tk.Label(
            body, textvariable=self.status, bg=self.theme.bg,
            fg=self.theme.warning, font=app.fonts.tiny,
            wraplength=app.px(560), justify="left", anchor="w",
        )
        self.status_label.pack(fill="x")
        actions = tk.Frame(body, bg=self.theme.bg)
        actions.pack(fill="x", side="bottom", pady=(12, 0))
        self.save_button = RoundButton(
            actions, app, "Save and switch", self._save,
            kind="accent", padx=15, pady=7,
        )
        self.save_button.pack(side="right")
        RoundButton(
            actions, app, "Cancel", self.destroy,
            kind="ghost", padx=15, pady=7,
        ).pack(side="right", padx=(0, 8))
        self.bind("<Escape>", lambda _event: self.destroy())

    def _save(self) -> None:
        self.status.set("Checking provider readiness…")
        self.status_label.configure(fg=self.theme.warning)
        self.save_button.set_enabled(False)
        self.app.configure_provider_choice(self.choice.get(), self._complete)

    def _complete(self, error: str | None) -> None:
        if not self.winfo_exists():
            return
        if error:
            self.status.set(error)
            self.status_label.configure(fg=self.theme.danger)
            self.save_button.set_enabled(True)
            return
        self.destroy()


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
        self.stopping = False
        self.deep_confirmed = bool(self.settings.get("deep_confirmed", False))
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
        self.provider_choice = provider_choice_from_config(config)
        self.provider_window: ProviderSettingsWindow | None = None
        self.control_state = "unknown"
        self.model_names: dict[str, str] = {}
        self.pending_approvals = 0
        self._closing = False
        self._close_deadline = 0.0
        self._toast_after: str | None = None
        self._last_user_prompt: str | None = None
        self.view = str(self.settings.get("view", "chat"))
        if self.view not in VIEW_KEYS:
            self.view = "chat"
        self.council: CouncilSession | None = None
        self.council_view: CouncilView | None = None
        self.council_turns: list[dict[str, Any]] = []
        self.nav_buttons: dict[str, RoundButton] = {}
        self.night_plan = council.NightPlan.from_mapping(self.settings.get("council_night"))
        self._last_touch = 0.0
        self.project_id = 1
        self.projects: list[dict[str, Any]] = []
        self.project_filter: int | None = None
        self.workspace = str(getattr(config, "workspace", "") or "")
        self.data_dir = str(getattr(config, "data_dir", "") or "")
        self.context_visible = bool(self.settings.get("context_panel", True))
        self.context_data: dict[str, Any] = {}
        self.chat_started_at = time.time()
        self.hotkey_hits: queue.Queue[str] = queue.Queue()
        self.hotkey: GlobalHotkey | None = None
        self.drop_target: FileDropTarget | None = None
        self.find_visible = False
        self._search_after: str | None = None
        self._context_after: str | None = None
        self._approvals_requested = False
        self._regenerating_message: Message | None = None
        self._deny_reasons: dict[int, str] = {}
        self._queued_by_chat: dict[int, list[dict[str, Any]]] = {}
        self._last_turn_status: dict[int, str] = {}
        self.notices: list[dict[str, Any]] = []
        self._notice_sequence = 0
        self.drafts: dict[int, dict[str, Any]] = {}
        self._worker_notice_shown = False
        self.grants: list[dict[str, Any]] = []
        self.chat_meta = ChatMeta(Path(getattr(config, "data_dir", ".")))
        self.chat_list_filter = "active"  # never persisted (J4): a launch always shows live chats
        self._chat_rows: dict[int, tk.Frame] = {}
        self._sidebar_selection: int | None = None
        self._chat_list_after: str | None = None
        self._layout_after: str | None = None
        self._context_auto_hidden = False
        self._sidebar_auto_hidden = False
        self._reading_padding = 0
        self.council_note = ""
        self._visible_chats: list[int] = []
        self.inbox_items: list[dict[str, Any]] = []
        self._finished_tasks: dict[int, str] = {}
        self._task_statuses: dict[int, str] = {}
        self._read_inbox: set[str] = set()
        self.tray: Any = None
        self.tray_actions: queue.Queue[str] = queue.Queue()
        self._last_notified_chat: int | None = None
        self.inbox_popover: Any = None
        self.memory_view: Any = None
        self.routines_view: Any = None
        self.companion: Any = None
        self._companion_conversation: int | None = None
        self._companion_pending: str | None = None
        self._pending_edit: str | None = None
        self._branch_pending: dict[str, Any] | None = None
        self.settings_window: SettingsWindow | None = None
        self.worker_alive: bool | None = None
        self.timeline_mode = str(self.settings.get("timeline_mode", "normal"))
        if self.timeline_mode not in TIMELINE_MODES:
            self.timeline_mode = "normal"
        self._pending_deletes: dict[int, str] = {}
        self._hidden_chats: set[int] = set()
        self.sheet: tk.Toplevel | None = None
        self.task_sheet: TaskDetailSheet | None = None
        self._scroll_target: int | None = None
        self._scroll_target_approval: int | None = None
        self._sidebar_pulse_on = False
        self._sidebar_pulse_after: str | None = None
        self._render_start = 0
        self._has_more = False
        self._loading_earlier = False
        self.earlier_button: RoundButton | None = None
        self._stale_cards: set[Any] = set()
        self._stale_refit_after: str | None = None
        self._last_metrics: dict[str, Any] | None = None
        self.file_index: Any = None  # ui_popups.FileIndex, worker-built; None while pending

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
        self.jobs = UiJobs(self.session.events)
        self.session.start()
        if self.night_plan.enabled:
            # The night watch must run even if the Council view is never opened.
            self._ensure_council()
        if bool(self.settings.get("hotkey", True)):
            self.hotkey = GlobalHotkey(self.hotkey_hits)
            self.hotkey.start()
        self.drop_target = FileDropTarget(self.root, lambda paths: self.composer.add_files(paths))
        if bool(self.settings.get("tray", True)):
            self._start_tray()
        self.root.after(40, self._poll_events)
        self.root.after(45_000, self._context_tick)

    # -- off-thread work ----------------------------------------------------

    def run_job(self, name: str, fn: Callable[..., Any], *args: Any, on_done: Callable[[Any, str | None], None] | None = None) -> int:
        """Run blocking I/O on the pool; ``on_done(result, error)`` runs on the Tk
        thread when the ``ui_job`` event comes back through the session queue."""
        return self.jobs.submit(name, fn, *args, on_done=on_done)

    # -- sizing helpers ---------------------------------------------------

    def px(self, value: float) -> int:
        return int(round(value * self.scale))

    def content_width(self) -> int:
        try:
            width = self.messages_view.canvas.winfo_width()
        except (AttributeError, tk.TclError):
            width = self.px(820)
        return max(self.px(320), min(width, self.px(860)))

    ZOOM_FONT_SIZES = {
        "body": 11, "body_bold": 11, "body_italic": 11, "body_strike": 11, "inline_code": 10, "mono": 10,
        "mono_small": 9, "tiny": 8, "small": 9, "small_bold": 9, "label": 10, "label_bold": 10,
    }

    def _apply_zoom(self) -> None:
        for name, size in self.ZOOM_FONT_SIZES.items():
            getattr(self.fonts, name).configure(size=max(7, size + self.zoom))

    def _configure_style(self) -> None:
        theme = self.theme
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        # On clam the thumb's thickness follows ``arrowsize`` (not ``width``);
        # arrowsize=0 requested a 1 px bar, so every ScrollFrame bar was invisible.
        style.configure(
            "Jarvis.Vertical.TScrollbar",
            gripcount=0, background=theme.border_strong, troughcolor=theme.bg,
            bordercolor=theme.bg, lightcolor=theme.bg, darkcolor=theme.bg,
            arrowsize=self.px(8), width=self.px(8),
        )
        style.map("Jarvis.Vertical.TScrollbar", background=[("active", theme.faint)])
        style.layout("Jarvis.Vertical.TScrollbar", [("Vertical.Scrollbar.trough", {"children": [("Vertical.Scrollbar.thumb", {"expand": "1", "sticky": "nswe"})], "sticky": "ns"})])
        configure_combobox_style(style, self.root, theme)

    # -- build ------------------------------------------------------------

    def _build(self) -> None:
        theme = self.theme
        if self.shell is not None:
            composer = getattr(self, "composer", None)
            if composer is not None:
                try:
                    composer.close_popups()
                except Exception:
                    pass
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
        self.shell.bind("<Configure>", self._on_shell_configure, add="+")
        self._build_topbar()
        self.chat_area = tk.Frame(self.main, bg=theme.bg)
        self.context_panel = ContextPanel(self.chat_area, self)
        self.context_separator = tk.Frame(self.chat_area, bg=theme.border, width=1)
        if self.context_visible:
            self.context_panel.pack(side="right", fill="y")
            self.context_separator.pack(side="right", fill="y")
        self.chat_column = tk.Frame(self.chat_area, bg=theme.bg)
        self.chat_column.pack(side="left", fill="both", expand=True)
        self.side_pane: Any = None
        try:
            from . import ui_sidepane
            self.side_pane = ui_sidepane.SidePane(self.chat_area, self, pack_options={"side": "right", "fill": "y", "before": self.chat_column})
            if bool(self.settings.get("side_pane", False)):
                self.side_pane.show()
        except Exception as exc:
            self.side_pane = None
            try:
                print(f"[jarvis-desktop] side pane unavailable: {type(exc).__name__}: {exc}", file=sys.stderr)
            except Exception:
                pass
        self.find_bar = FindBar(self.chat_column, self)
        if self.find_visible:
            self.find_bar.pack(fill="x")
        self.messages_view = ScrollFrame(self.chat_column, self, bg=theme.bg)
        self.messages_view.pack(fill="both", expand=True)
        self.messages_view.canvas.bind("<Configure>", self._on_messages_resize, add="+")
        self.messages_view.canvas.configure(yscrollcommand=self._on_messages_scroll)
        self.jump_button = RoundButton(self.messages_view, self, "↓ Latest", lambda: self.messages_view.scroll_to_end(), kind="ghost", padx=10, pady=4, font=self.fonts.tiny, radius=999)
        self.notice_frame = tk.Frame(self.chat_column, bg=theme.bg)
        composer_area = tk.Frame(self.chat_column, bg=theme.bg)
        composer_area.pack(fill="x", padx=self.px(28), pady=(4, self.px(14)))
        self.composer_area = composer_area
        self.chat_column.bind("<Configure>", self._on_chat_column_configure, add="+")
        self.composer = Composer(composer_area, self)
        self.composer.pack(fill="x")
        self.composer.card.configure(padx=self.px(12))
        self.composer.set_ready(self.ready)
        self._bind_composer_shortcuts()
        self.render_notices()
        if self.context_data:
            self.context_panel.set_data(self.context_data)
        self.council_view = CouncilView(self.main, self)
        for row in self.council_turns:
            self.council_view.add_turn(row)
        self.memory_view = None
        self.routines_view = None
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

        self.nav_buttons = {}
        for row_items in (
            (("chat", "Chat", "▣", "Talk to Jarvis (Ctrl+M switches)"), ("council", "Council", "◎", "Jarvis and his specialists in session (Ctrl+M)")),
            (("memory", "Memory", "◍", "Governed facts Jarvis remembers for this project (Ctrl+Shift+M)"), ("routines", "Routines", "◷", "Recurring background runs (Ctrl+Shift+R)")),
        ):
            nav = tk.Frame(side, bg=theme.panel)
            nav.pack(fill="x", padx=self.px(16), pady=(0, self.px(6)))
            for key, label, icon, hint in row_items:
                button = RoundButton(
                    nav, self, label, lambda choice=key: self.set_view(choice),
                    kind="active" if key == self.view else "subtle", icon=icon,
                    padx=8, pady=6, width=self.px(118), font=self.fonts.small, tooltip=hint,
                )
                button.pack(side="left", padx=(0, self.px(6)))
                self.nav_buttons[key] = button
        tk.Frame(side, bg=theme.panel, height=self.px(4)).pack()

        RoundButton(side, self, "New chat", self.new_chat, kind="ghost", icon="＋", padx=12, pady=8, width=self.px(244), tooltip="Ctrl+N").pack(padx=self.px(16), pady=(0, 8))
        RoundButton(side, self, "Search or jump to…   Ctrl K", self.open_palette, kind="subtle", icon="⌕", padx=10, pady=6, width=self.px(244), font=self.fonts.small).pack(padx=self.px(16), pady=(0, 10))

        search_row = tk.Frame(side, bg=theme.panel)
        search_row.pack(fill="x", padx=self.px(16), pady=(0, 6))
        search_shell = tk.Frame(search_row, bg=theme.surface, highlightbackground=theme.border, highlightthickness=1, padx=8)
        search_shell.pack(side="left", fill="x", expand=True)
        self.search_var = tk.StringVar(value=self.chat_filter)
        self.search_entry = tk.Entry(search_shell, textvariable=self.search_var, bg=theme.surface, fg=theme.text, insertbackground=theme.accent, bd=0, highlightthickness=0, font=self.fonts.small)
        self.search_entry.pack(fill="x", ipady=6)
        self._placeholder(self.search_entry, self.search_var, "Filter chats")
        self.search_var.trace_add("write", lambda *_args: self._on_search())
        self.chat_filter_button = RoundButton(search_row, self, CHAT_FILTER_LABELS[self.chat_list_filter], self.cycle_chat_filter, kind="subtle", padx=8, pady=5, font=self.fonts.tiny, tooltip="Active · Archived · All chats")
        self.chat_filter_button.pack(side="left", padx=(6, 0))

        self.chat_list = ScrollFrame(side, self, bg=theme.panel)
        self.chat_list.pack(fill="both", expand=True, padx=(self.px(10), self.px(6)))
        self._bind_chat_list_keys()

        footer = tk.Frame(side, bg=theme.panel, padx=self.px(16), pady=self.px(12))
        footer.pack(fill="x", side="bottom")
        tk.Label(footer, text="PROJECT", bg=theme.panel, fg=theme.faint, font=self.fonts.tiny).pack(anchor="w")
        self.project_button = RoundButton(footer, self, self.current_project_name(), self.choose_project, kind="ghost", icon="▰", padx=10, pady=6, width=self.px(244), font=self.fonts.small, tooltip="Switch project or create one (Ctrl+Shift+N)")
        self.project_button.pack(pady=(4, 2))
        self.project_detail = tk.Label(footer, text="", bg=theme.panel, fg=theme.faint, font=self.fonts.tiny, anchor="w", wraplength=self.px(236), justify="left")
        self.project_detail.pack(fill="x", pady=(0, 6))
        self.model_buttons: dict[str, RoundButton] = {}
        self._refresh_project_detail()

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
        IconButton(tools, self, "⚙", self.open_settings, tooltip="Settings (Ctrl+,)").pack(side="left")

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
        self.titles_frame = titles
        self.title_editor: tk.Entry | None = None
        self.title_label = tk.Label(titles, text=self.chat_title, bg=theme.bg, fg=theme.text_strong, font=self.fonts.title, anchor="w", cursor="hand2")
        self.title_label.pack(anchor="w")
        self.title_label.bind("<Double-Button-1>", lambda _e: self.begin_title_edit())
        Tooltip(self.title_label, "Double-click or F2 to rename this chat", self)
        subrow = tk.Frame(titles, bg=theme.bg)
        subrow.pack(anchor="w", fill="x")
        self.subrow = subrow
        self.subtitle_label = tk.Label(subrow, text="", bg=theme.bg, fg=theme.faint, font=self.fonts.tiny, anchor="w")
        self.subtitle_label.pack(side="left")
        self.context_pill_label = tk.Label(subrow, text="", bg=theme.surface_alt, fg=theme.muted, font=self.fonts.tiny, padx=7, pady=1)
        self._context_pill_tooltip = Tooltip(self.context_pill_label, "", self)
        self.refresh_context_pill()
        # One Stop control (J8): the composer's ↑ becomes ■ while Jarvis works.
        IconButton(bar, self, "◨", self.toggle_context, tooltip="Context panel (Ctrl+I)").pack(side="right", padx=(0, 6))
        bell = tk.Frame(bar, bg=theme.bg)
        bell.pack(side="right", padx=(0, 6))
        self.bell_button = IconButton(bell, self, "🔔", self.open_inbox, tooltip="Inbox: approvals, unread replies, finished tasks, errors (Ctrl+U)")
        self.bell_button.pack(side="left")
        self.bell_badge = tk.Label(bell, text="", bg=theme.bg, fg=theme.warning, font=self.fonts.tiny)
        self.bell_badge.pack(side="left")
        IconButton(bar, self, "⌕", self.toggle_find, tooltip="Find in chat (Ctrl+F)").pack(side="right", padx=(0, 2))
        self.theme_chip = tk.Label(bar, text=self.theme.name, bg=theme.surface_alt, fg=theme.muted, font=self.fonts.tiny, padx=8, pady=3, cursor="hand2")
        self.theme_chip.pack(side="right", padx=(0, 10))
        self.theme_chip.bind("<Button-1>", lambda _e: self.cycle_theme())
        Tooltip(self.theme_chip, "Cycle theme (Ctrl+T)", self)

    def _bind_composer_shortcuts(self) -> None:
        """Tk's Text class binds emacs chords (Ctrl+K kills the line, Ctrl+I inserts
        a tab, Ctrl+F/B/N/E move the caret). Intercept them on the composer so the
        app shortcuts win and the class binding never fires."""
        chords = {
            "k": self.open_palette, "i": self.toggle_context, "f": self.toggle_find,
            "n": self.new_chat, "b": self.toggle_sidebar, "e": self.export_chat,
            "t": self.cycle_theme, "r": self.regenerate_last, "m": self.toggle_view,
            "l": self.focus_composer, "comma": self.open_settings, "slash": self.show_shortcuts,
            "o": self.cycle_timeline_mode, "u": self.open_inbox,
        }
        for key, command in chords.items():
            self.composer.input.bind(f"<Control-{key}>", lambda _e, run=command: (run(), "break")[1])

    def chord_allowed(self, event: Any) -> bool:
        """One rule for every bind_all chord: text fields other than the composer
        (the palette entry, Find, the deny entry, dialog boxes, other windows)
        keep their keys; the chord only fires from the main window elsewhere."""
        widget = getattr(event, "widget", None)
        if widget is None or not isinstance(widget, tk.Misc):
            return True
        try:
            if widget.winfo_toplevel() is not self.root:
                return False
        except (tk.TclError, AttributeError):
            return False
        composer_input = getattr(getattr(self, "composer", None), "input", None)
        if widget is composer_input:
            return True
        if isinstance(widget, (tk.Entry, tk.Text, ttk.Combobox, ttk.Entry)):
            return False
        return True

    def _chord(self, command: Callable[[], Any]) -> Callable[[Any], str | None]:
        def handler(event: Any) -> str | None:
            if not self.chord_allowed(event):
                return None
            command()
            return "break"
        return handler

    def _bind_shortcuts(self) -> None:
        root = self.root
        chords: dict[str, Callable[[], Any]] = {
            "<Control-k>": self.open_palette, "<Control-K>": self.open_palette,
            "<Control-n>": self.new_chat, "<Control-l>": self.focus_composer,
            "<Control-b>": self.toggle_sidebar, "<Control-t>": self.cycle_theme,
            "<Control-m>": self.toggle_view, "<Control-e>": self.export_chat,
            "<Control-r>": self.regenerate_last, "<Control-slash>": self.show_shortcuts,
            "<F1>": self.show_shortcuts,
            "<Control-Shift-C>": self.copy_last_reply, "<Control-Shift-A>": self.show_approvals,
            "<Control-Shift-N>": self.new_project, "<Control-i>": self.toggle_context,
            "<Control-f>": self.toggle_find, "<Control-comma>": self.open_settings,
            "<Control-o>": self.cycle_timeline_mode,
            "<Control-Tab>": lambda: self.cycle_chat(1), "<Control-Shift-Tab>": lambda: self.cycle_chat(-1),
            "<Control-Shift-ISO_Left_Tab>": lambda: self.cycle_chat(-1),
            "<Control-Alt-p>": self.toggle_pin, "<Control-Shift-U>": self.toggle_unread,
            "<Control-u>": self.open_inbox, "<Control-Alt-u>": self.open_inbox,
            "<Control-Shift-M>": lambda: self.set_view("memory"), "<Control-Shift-R>": lambda: self.set_view("routines"),
            "<Control-Shift-P>": self.toggle_side_pane,
            "<Control-plus>": lambda: self.zoom_text(1), "<Control-equal>": lambda: self.zoom_text(1),
            "<Control-minus>": lambda: self.zoom_text(-1),
        }
        for index, label in enumerate(MODEL_CHOICES):
            chords[f"<Control-Key-{index + 1}>"] = lambda choice=label: self.set_model(choice)
        for sequence, command in chords.items():
            root.bind_all(sequence, self._chord(command))
        root.bind_all("<F2>", self._f2)
        root.bind_all("<F3>", lambda event: self._find_step(event, 1))
        root.bind_all("<Shift-F3>", lambda event: self._find_step(event, -1))
        root.bind_all("<Escape>", self._escape)
        root.bind_all("<Key>", self._touch, add="+")
        root.bind_all("<Button-1>", self._touch, add="+")
        root.bind("<Configure>", self._on_root_configure)

    # -- rendering --------------------------------------------------------

    def render_messages(self, target_message_id: int | None = None) -> None:
        """Build cards for the newest MESSAGE_WINDOW messages only; "Show earlier"
        at the top prepends the rest (and asks the store for older rows)."""
        theme = self.theme
        for child in self.messages_view.inner.winfo_children():
            child.destroy()
        self.cards = []
        self.active_card = None
        self.earlier_button = None
        self._stale_cards = set()
        if not self.messages:
            self._render_start = 0
            EmptyState(self.messages_view.inner, self).pack(fill="both", expand=True)
            self.messages_view.scroll_to_top()
            return
        column = tk.Frame(self.messages_view.inner, bg=theme.bg)
        column.pack(fill="x", pady=(self.px(10), self.px(20)))
        self.messages_column = column
        target_index = next((index for index, message in enumerate(self.messages) if target_message_id is not None and message.message_id == target_message_id), None)
        self._render_start = message_window_start(len(self.messages), target_index)
        self._build_earlier_control()
        for message in self.messages[self._render_start:]:
            card = MessageCard(column, self, message)
            card.pack(fill="x")
            self.cards.append(card)
            if message.working or message.streaming:
                self.active_card = card
        self.messages_view.scroll_to_end()

    def _build_earlier_control(self) -> None:
        """The control above the first built card: hidden messages first, then the store."""
        column = getattr(self, "messages_column", None)
        button = getattr(self, "earlier_button", None)
        if button is not None:
            try:
                button.destroy()
            except tk.TclError:
                pass
            self.earlier_button = None
        if column is None or not column.winfo_exists():
            return
        hidden = int(getattr(self, "_render_start", 0))
        more = bool(getattr(self, "_has_more", False))
        if hidden <= 0 and not more:
            return
        if hidden > 0:
            label = f"Show {min(MESSAGE_WINDOW, hidden)} earlier · {hidden} hidden"
        else:
            label = "Load earlier messages"
        button = RoundButton(column, self, label, self.show_earlier, kind="ghost", padx=12, pady=4, font=self.fonts.tiny, tooltip="Older messages are kept out of the window until you ask, so long chats open fast")
        first = next((card for card in self.cards if card.winfo_exists() and card.master is column), None)
        if first is not None:
            button.pack(pady=(0, self.px(8)), before=first)  # always above the oldest built card
        else:
            button.pack(pady=(0, self.px(8)))
        self.earlier_button = button

    def show_earlier(self) -> None:
        """Prepend up to MESSAGE_WINDOW hidden cards; when none are hidden ask the
        store for the page before the oldest known row."""
        start = int(getattr(self, "_render_start", 0))
        column = getattr(self, "messages_column", None)
        if column is None or not column.winfo_exists():
            return
        if start <= 0:
            if getattr(self, "_has_more", False) and self.conversation_id and not getattr(self, "_loading_earlier", False):
                oldest = next((message.message_id for message in self.messages if message.message_id is not None), None)
                if oldest is not None:
                    self._loading_earlier = True
                    if self.earlier_button is not None:
                        self.earlier_button.set_text("Loading…")
                    self.session.load_earlier(self.conversation_id, oldest)
            return
        new_start = max(0, start - MESSAGE_WINDOW)
        anchor = self.cards[0] if self.cards else None
        built: list[MessageCard] = []
        for message in self.messages[new_start:start]:
            card = MessageCard(column, self, message)
            if anchor is not None:
                card.pack(fill="x", before=anchor)
            else:
                card.pack(fill="x")
            built.append(card)
        self.cards = built + self.cards
        self._render_start = new_start
        self._build_earlier_control()
        if anchor is not None:
            self.root.after_idle(lambda: self.scroll_widget_into_view(anchor))
        self.root.after(160, self._refit_texts)

    def _card_parent(self) -> tk.Misc:
        column = getattr(self, "messages_column", None)
        if column is None or not column.winfo_exists() or column.master is not self.messages_view.inner:
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

    def _on_messages_resize(self, event: Any) -> None:
        width = int(getattr(event, "width", 0) or 0)
        if width > 1:
            self._apply_reading_column(width)
        if getattr(self, "_resize_after", None):
            try:
                self.root.after_cancel(self._resize_after)
            except tk.TclError:
                pass
        self._resize_after = self.root.after(120, self._refit_texts)

    def _apply_reading_column(self, width: int) -> int:
        """Centre the message column at READING_COLUMN_MAX (J2): the padding grows
        with the canvas so a 1900 px window never shows 1,250 px lines."""
        pad = reading_column_padding(int(width), self.scale)
        if pad == self._reading_padding:
            return pad
        self._reading_padding = pad
        column = getattr(self, "messages_column", None)
        try:
            if column is not None and column.winfo_exists() and column.winfo_manager() == "pack":
                column.pack_configure(padx=pad - self.px(28) if pad > self.px(28) else 0)
        except tk.TclError:
            pass
        return pad

    def _on_chat_column_configure(self, event: Any) -> None:
        width = int(getattr(event, "width", 0) or 0)
        if width <= 1:
            return
        pad = reading_column_padding(width, self.scale)
        try:
            self.composer_area.pack_configure(padx=pad)
            frame = getattr(self, "notice_frame", None)
            if frame is not None and frame.winfo_manager() == "pack":
                frame.pack_configure(padx=pad)
            self.composer.apply_width_budget(width - 2 * pad)
        except (AttributeError, tk.TclError):
            pass

    # -- layout budget (J1) ---------------------------------------------------

    def _on_shell_configure(self, event: Any) -> None:
        if getattr(event, "widget", None) is not self.shell:
            return
        if self._layout_after is not None:
            try:
                self.root.after_cancel(self._layout_after)
            except tk.TclError:
                pass
        self._layout_after = self.root.after(90, self._apply_layout_budget)

    def _apply_layout_budget(self, width: int | None = None) -> dict[str, Any]:
        """Fit the sidebar, context panel and side pane to the window (J1). Auto
        changes never touch the persisted settings and undo themselves when the
        space comes back."""
        self._layout_after = None
        try:
            root_width = int(width) if width else int(self.shell.winfo_width())
        except (AttributeError, tk.TclError):
            return {}
        if root_width <= 1:
            return {}
        pane = getattr(self, "side_pane", None)
        pane_visible = bool(pane is not None and getattr(pane, "visible", False))
        pane_width = int(getattr(pane, "pane_width", self.px(520)) or self.px(520)) if pane is not None else 0
        plan = layout_plan(
            root_width, sidebar_wanted=bool(self.settings.get("sidebar", True)),
            context_wanted=bool(self.settings.get("context_panel", True)),
            pane_visible=pane_visible, pane_width=pane_width, scale=self.scale,
        )
        try:
            wanted_sidebar = bool(self.settings.get("sidebar", True))
            if plan["sidebar"] != self.sidebar_visible:
                self._show_sidebar(plan["sidebar"])
                self._sidebar_auto_hidden = wanted_sidebar and not plan["sidebar"]
            elif plan["sidebar"] and self._sidebar_auto_hidden:
                self._sidebar_auto_hidden = False
            wanted_context = bool(self.settings.get("context_panel", True))
            if plan["context"] != self.context_visible:
                self._show_context(plan["context"])
                self._context_auto_hidden = wanted_context and not plan["context"]
            elif plan["context"] and self._context_auto_hidden:
                self._context_auto_hidden = False
            if pane is not None and pane_visible:
                target = int(plan["pane_width"])
                if target != int(pane.cget("width") or 0):
                    pane.configure(width=target)
        except tk.TclError:
            return plan
        return plan

    def _show_sidebar(self, visible: bool) -> None:
        self.sidebar_visible = bool(visible)
        try:
            if self.sidebar_visible:
                self.sidebar.pack(side="left", fill="y", before=self.main)
                self.sidebar_button.pack_forget()
            else:
                self.sidebar.pack_forget()
                self.sidebar_button.pack(side="left", padx=(0, 10), before=self.topbar.winfo_children()[1])
        except tk.TclError:
            pass
        self.root.after(80, self._refit_texts)

    def _show_context(self, visible: bool) -> None:
        self.context_visible = bool(visible)
        try:
            if self.context_visible:
                pane = self.side_pane
                anchor = pane if (pane is not None and getattr(pane, "visible", False)) else self.chat_column
                self.context_panel.pack(side="right", fill="y", before=anchor)
                self.context_separator.pack(side="right", fill="y", before=anchor)
                self.context_panel.set_data(self.context_data)
            else:
                self.context_panel.pack_forget()
                self.context_separator.pack_forget()
        except tk.TclError:
            pass

    def _visible_canvas_span(self) -> tuple[int, int] | None:
        """The inner-frame y range currently on screen, widened by one viewport."""
        try:
            canvas = self.messages_view.canvas
            first, last = canvas.yview()
            total = max(1, int(self.messages_view.inner.winfo_height()))
            height = max(1, int(canvas.winfo_height()))
        except (tk.TclError, ValueError):
            return None
        top = int(first * total) - height
        bottom = int(last * total) + height
        return top, bottom

    def _refit_texts(self) -> None:
        """Refit the AutoTexts of cards that intersect the visible region; cards
        off screen are marked stale and refitted when they scroll into view."""
        self._resize_after = None
        span = self._visible_canvas_span()
        column = getattr(self, "messages_column", None)
        stale: set[MessageCard] = set()
        if span is None or column is None or not column.winfo_exists() or len(self.cards) <= 12:
            for widget in self._all_autotexts(self.messages_view.inner):
                widget.fit()
        else:
            top, bottom = span
            try:
                column_y = int(column.winfo_y())
            except tk.TclError:
                column_y = 0
            seen: set[int] = set()
            for card in self.cards:
                try:
                    card_top = column_y + int(card.winfo_y())
                    card_bottom = card_top + max(1, int(card.winfo_height()))
                except tk.TclError:
                    continue
                if card_bottom < top or card_top > bottom:
                    stale.add(card)
                    continue
                seen.add(id(card))
                for widget in self._all_autotexts(card):
                    widget.fit()
            for widget in self._all_autotexts(self.messages_view.inner):
                parent = widget
                inside_card = False
                while parent is not None:
                    if isinstance(parent, MessageCard):
                        inside_card = True
                        break
                    parent = getattr(parent, "master", None)
                if not inside_card:
                    widget.fit()
        self._stale_cards = stale
        try:
            self.root.after_idle(self.messages_view._sync_region)
        except tk.TclError:
            pass

    def _refit_stale_cards(self) -> None:
        """After a scroll: fit the stale cards that are now within the visible span."""
        self._stale_refit_after = None
        stale = getattr(self, "_stale_cards", None)
        if not stale:
            return
        span = self._visible_canvas_span()
        column = getattr(self, "messages_column", None)
        if span is None or column is None or not column.winfo_exists():
            return
        top, bottom = span
        try:
            column_y = int(column.winfo_y())
        except tk.TclError:
            return
        done: set[MessageCard] = set()
        for card in list(stale):
            try:
                if not card.winfo_exists():
                    done.add(card)
                    continue
                card_top = column_y + int(card.winfo_y())
                card_bottom = card_top + max(1, int(card.winfo_height()))
            except tk.TclError:
                done.add(card)
                continue
            if card_bottom < top or card_top > bottom:
                continue
            for widget in self._all_autotexts(card):
                widget.fit()
            done.add(card)
        stale.difference_update(done)

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

    def needs_you_chats(self) -> set[int]:
        """Chats with a pending approval, read from the store's approval scope."""
        found: set[int] = set()
        for row in self.context_data.get("pending_approvals") or []:
            match = re.fullmatch(r"conversation:([1-9][0-9]*)", str(row.get("scope") or ""))
            if match:
                found.add(int(match.group(1)))
        return found

    def visible_chats(self) -> list[dict[str, Any]]:
        query = self.chat_filter.strip().lower()
        meta = self.chat_meta
        mode = self.chat_list_filter
        visible = []
        for chat in self.chats:
            cid = chat["id"]
            if cid in self._hidden_chats:
                continue
            if query and query not in chat["title"].lower():
                continue
            current = cid == self.conversation_id or cid == self._companion_conversation
            if chat.get("message_count", 0) <= 0 and not current:
                continue
            if self.project_filter is not None and chat.get("project_id") != self.project_filter and not current:
                continue
            archived = meta.flag(cid, "archived")
            if mode == "active" and archived and cid != self.conversation_id:
                continue
            if mode == "archived" and not archived and cid != self.conversation_id:
                continue  # the open conversation is always listed
            visible.append(chat)
        rank = {"Today": 0, "Yesterday": 1, "Previous 7 days": 2, "Previous 30 days": 3, "Earlier": 4}
        needs = self.needs_you_chats()
        visible.sort(key=lambda chat: (
            0 if meta.flag(chat["id"], "pinned") else 1,
            rank.get(chat_group_label(chat.get("created_at", "")), 5),
            0 if chat["id"] in needs else 1,
        ))
        return visible

    def refresh_chat_list(self) -> None:
        theme = self.theme
        container = self.chat_list.inner
        visible = self.visible_chats()
        self._visible_chats = [chat["id"] for chat in visible]
        try:
            self.chat_filter_button.set_text(CHAT_FILTER_LABELS[self.chat_list_filter])
        except (AttributeError, tk.TclError):
            pass
        needs_digest = self.needs_you_chats()
        digest = render_digest([
            [(chat["id"], chat["title"], chat.get("message_count"), chat.get("project_name"), self.chat_meta.get(chat["id"]), chat["id"] in needs_digest) for chat in visible],
            self.conversation_id, self.busy, self.chat_list_filter, bool(self.chats),
        ])
        if getattr(self.chat_list, "_digest", None) == digest:
            return  # nothing behind the list changed: keep the rows (and their hover state)
        self.chat_list._digest = digest  # type: ignore[attr-defined]
        for child in container.winfo_children():
            child.destroy()
        self._chat_rows = {}
        if not visible:
            empty = "No chats yet." if not self.chats else ("No archived chats." if self.chat_list_filter == "archived" else "No chats match.")
            tk.Label(container, text=empty, bg=theme.panel, fg=theme.faint, font=self.fonts.small, pady=10).pack(fill="x", padx=8)
            return
        last_group = None
        needs = self.needs_you_chats()
        for chat in visible:
            group = "Pinned" if self.chat_meta.flag(chat["id"], "pinned") else chat_group_label(chat.get("created_at", ""))
            if group != last_group:
                tk.Label(container, text=group.upper(), bg=theme.panel, fg=theme.faint, font=self.fonts.tiny, anchor="w").pack(fill="x", padx=8, pady=(8, 2))
                last_group = group
            self._chat_row(container, chat, needs_you=chat["id"] in needs)
        self._paint_sidebar_selection()

    # -- sidebar keyboard (N-20) --------------------------------------------

    def _sidebar_chat(self, conversation_id: int | None) -> dict[str, Any] | None:
        return next((chat for chat in self.chats if chat["id"] == conversation_id), None)

    def _paint_sidebar_selection(self) -> None:
        """A visible ring on the selected row while the chat list has the keyboard."""
        theme = self.theme
        try:
            focused = self.root.focus_displayof() is self.chat_list.canvas
        except (AttributeError, KeyError, tk.TclError):
            focused = False
        for cid, outer in list(self._chat_rows.items()):
            try:
                if not outer.winfo_exists():
                    continue
                selected = focused and cid == self._sidebar_selection
                outer.configure(highlightthickness=1 if selected else 0, highlightbackground=theme.accent if selected else theme.panel)
            except tk.TclError:
                continue

    def _sidebar_key(self, action: str) -> str:
        order = list(self._visible_chats)
        if action in {"up", "down"}:
            if not order:
                return "break"
            current = self._sidebar_selection if self._sidebar_selection in order else (self.conversation_id if self.conversation_id in order else None)
            if current is None:
                index = 0 if action == "down" else len(order) - 1
            else:
                index = max(0, min(len(order) - 1, order.index(current) + (1 if action == "down" else -1)))
            self._sidebar_selection = order[index]
            self._paint_sidebar_selection()
            row = self._chat_rows.get(self._sidebar_selection)
            if row is not None:
                self._scroll_sidebar_to(row)
            return "break"
        chat = self._sidebar_chat(self._sidebar_selection)
        if chat is None:
            return "break"
        if action == "open":
            self.open_chat(int(chat["id"]))
        elif action == "rename":
            self.rename_chat(chat)
        elif action == "delete":
            self.delete_chat(chat)
        return "break"

    def _scroll_sidebar_to(self, row: tk.Misc) -> None:
        try:
            canvas = self.chat_list.canvas
            inner = self.chat_list.inner
            total = max(1, inner.winfo_height())
            top = row.winfo_y()
            bottom = top + row.winfo_height()
            first, last = canvas.yview()
            if top < first * total:
                canvas.yview_moveto(max(0.0, top / total))
            elif bottom > last * total:
                canvas.yview_moveto(max(0.0, (bottom - canvas.winfo_height()) / total))
        except tk.TclError:
            pass

    def focus_chat_list(self) -> None:
        try:
            self.chat_list.canvas.focus_set()
        except (AttributeError, tk.TclError):
            return
        if self._sidebar_selection not in self._visible_chats:
            self._sidebar_selection = self.conversation_id if self.conversation_id in self._visible_chats else (self._visible_chats[0] if self._visible_chats else None)
        self._paint_sidebar_selection()

    def _bind_chat_list_keys(self) -> None:
        canvas = self.chat_list.canvas
        canvas.configure(takefocus=1)
        canvas.bind("<Up>", lambda _e: self._sidebar_key("up"))
        canvas.bind("<Down>", lambda _e: self._sidebar_key("down"))
        canvas.bind("<Return>", lambda _e: self._sidebar_key("open"))
        canvas.bind("<space>", lambda _e: self._sidebar_key("open"))
        canvas.bind("<F2>", lambda _e: self._sidebar_key("rename"))
        canvas.bind("<Delete>", lambda _e: self._sidebar_key("delete"))
        canvas.bind("<FocusIn>", lambda _e: self.focus_chat_list())
        canvas.bind("<FocusOut>", lambda _e: self._paint_sidebar_selection())

    def _chat_row(self, container: tk.Misc, chat: dict[str, Any], *, needs_you: bool = False) -> None:
        theme = self.theme
        cid = chat["id"]
        meta_flags = self.chat_meta.get(cid)
        active = cid == self.conversation_id
        unread = bool(meta_flags.get("unread")) and not active
        bg = theme.surface_hover if active else theme.panel
        outer = tk.Frame(container, bg=bg, cursor="hand2", highlightthickness=0, highlightbackground=theme.accent)
        outer.pack(fill="x", pady=1)
        self._chat_rows[int(cid)] = outer
        bar = tk.Frame(outer, bg=theme.accent if (active and self.busy) else bg, width=3)
        bar.pack(side="left", fill="y")
        row = tk.Frame(outer, bg=bg, padx=7, pady=5)
        row.pack(side="left", fill="x", expand=True)
        top = tk.Frame(row, bg=bg)
        top.pack(fill="x")
        prefix = ""
        if meta_flags.get("pinned"):
            prefix += "📌 "
        if meta_flags.get("branched_from"):
            prefix += "⑂ "
        archived = bool(meta_flags.get("archived"))
        title_fg = theme.text_strong if (active or unread) else (theme.faint if archived else theme.text)
        title = tk.Label(top, text=prefix + compact_activity(chat["title"], SIDEBAR_TITLE_CHARS), bg=bg, fg=title_fg, font=self.fonts.small_bold if (active or unread) else self.fonts.small, anchor="w")
        title.pack(side="left", fill="x", expand=True)
        if active and self.busy:
            self.sidebar_bar = bar
            self._start_sidebar_pulse()
        badges = tk.Frame(top, bg=bg)
        badges.pack(side="right")
        if needs_you:
            tk.Label(badges, text="!", bg=bg, fg=theme.warning, font=self.fonts.small_bold).pack(side="right")
        elif unread:
            tk.Label(badges, text="●", bg=bg, fg=theme.accent, font=self.fonts.tiny).pack(side="right")
        tools = tk.Frame(top, bg=bg)
        pin_glyph = "⚲" if meta_flags.get("pinned") else "📌"
        IconButton(tools, self, "🗑" if meta_flags.get("archived") else "▤", lambda: self.toggle_archive(cid), tooltip="Unarchive" if meta_flags.get("archived") else "Archive (hide without deleting)").pack(side="right")
        IconButton(tools, self, pin_glyph, lambda: self.toggle_pin(cid), tooltip="Unpin" if meta_flags.get("pinned") else "Pin to the top").pack(side="right")
        count = chat.get("message_count", 0)
        meta_text = f"{count} message{'s' if count != 1 else ''}"
        if chat.get("project_name") and chat["project_name"].lower() not in {"default workspace", "default"}:
            meta_text += f" · {chat['project_name']}"
        if meta_flags.get("archived"):
            meta_text += " · archived"
        # One line per chat (J11): the count and project live in the tooltip; an
        # archived row keeps its tag beside the title.
        Tooltip(title, f"{chat['title']}\n{meta_text}", self)
        if meta_flags.get("archived"):
            tk.Label(badges, text="archived", bg=bg, fg=theme.faint, font=self.fonts.tiny).pack(side="right", padx=(4, 0))
        widgets = (outer, row, top, title, badges)

        def hovered() -> bool:
            try:
                pointer = self.root.winfo_containing(*self.root.winfo_pointerxy())
            except (tk.TclError, TypeError):
                return False
            while pointer is not None:
                if pointer is outer:
                    return True
                pointer = getattr(pointer, "master", None)
            return False

        def enter(_e: Any) -> None:
            if not active:
                hover_bg = theme.surface_alt
                for item in widgets:
                    item.configure(bg=hover_bg)
                tools.configure(bg=hover_bg)
                bar.configure(bg=hover_bg if not (active and self.busy) else theme.accent)
            if not tools.winfo_ismapped():
                badges.pack_forget()
                tools.pack(side="right")

        def leave(_e: Any) -> None:
            if hovered():
                return
            if not active:
                for item in widgets:
                    item.configure(bg=theme.panel)
                tools.configure(bg=theme.panel)
                bar.configure(bg=theme.panel)
            tools.pack_forget()
            badges.pack(side="right")

        def clicked(_event: Any, target: int = cid) -> None:
            self._sidebar_selection = target
            self.open_chat(target)

        for item in widgets:
            item.bind("<Enter>", enter)
            item.bind("<Leave>", leave)
            item.bind("<Button-1>", clicked)
            item.bind("<Button-3>", lambda event, target=chat: self._chat_menu(event, target))
        tools.bind("<Leave>", leave)

    def _start_sidebar_pulse(self) -> None:
        if self._sidebar_pulse_after is None:
            self._sidebar_pulse_after = self.root.after(520, self._tick_sidebar_pulse)

    def _tick_sidebar_pulse(self) -> None:
        """The working chat's 3-px bar breathes between accent and its soft tone."""
        self._sidebar_pulse_after = None
        bar = getattr(self, "sidebar_bar", None)
        if not self.busy or bar is None:
            return
        try:
            if not bar.winfo_exists():
                return
            self._sidebar_pulse_on = not self._sidebar_pulse_on
            bar.configure(bg=self.theme.accent if self._sidebar_pulse_on else self.theme.accent_soft)
        except tk.TclError:
            return
        self._sidebar_pulse_after = self.root.after(520, self._tick_sidebar_pulse)

    def _chat_menu(self, event: Any, chat: dict[str, Any]) -> None:
        theme = self.theme
        cid = chat["id"]
        flags = self.chat_meta.get(cid)
        menu = tk.Menu(self.root, tearoff=0, bg=theme.surface, fg=theme.text, activebackground=theme.surface_hover, activeforeground=theme.text_strong, bd=0, font=self.fonts.small)
        menu.add_command(label="Open", command=lambda: self.open_chat(cid))
        menu.add_command(label="Rename…", command=lambda: self.rename_chat(chat))
        menu.add_command(label="Unpin" if flags.get("pinned") else "Pin", command=lambda: self.toggle_pin(cid))
        menu.add_command(label="Unarchive" if flags.get("archived") else "Archive", command=lambda: self.toggle_archive(cid))
        menu.add_command(label="Mark as read" if flags.get("unread") else "Mark as unread", command=lambda: self.toggle_unread(cid))
        menu.add_command(label="Copy title", command=lambda: self.copy_text(chat["title"]))
        menu.add_separator()
        menu.add_command(label="Delete", command=lambda: self.delete_chat(chat))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    # -- chat flags ----------------------------------------------------------

    def toggle_pin(self, conversation_id: int | None = None) -> None:
        cid = int(conversation_id or self.conversation_id or 0)
        if not cid:
            return
        pinned = not self.chat_meta.flag(cid, "pinned")
        self.chat_meta.set(cid, pinned=pinned)
        self.refresh_chat_list()
        self.toast("Pinned to the top of the sidebar." if pinned else "Unpinned.")

    def toggle_archive(self, conversation_id: int | None = None) -> None:
        cid = int(conversation_id or self.conversation_id or 0)
        if not cid:
            return
        archived = not self.chat_meta.flag(cid, "archived")
        self.chat_meta.set(cid, archived=archived, pinned=False if archived else self.chat_meta.flag(cid, "pinned"))
        self.refresh_chat_list()
        window = self.settings_window
        if window is not None:
            try:
                if window.winfo_exists():
                    window.render_archived()
            except tk.TclError:
                pass
        self.toast("Archived — find it under the Archived filter. Nothing was deleted." if archived else "Unarchived.")

    def toggle_unread(self, conversation_id: int | None = None) -> None:
        cid = int(conversation_id or self.conversation_id or 0)
        if not cid:
            return
        self.chat_meta.set(cid, unread=not self.chat_meta.flag(cid, "unread"))
        self.refresh_chat_list()

    def mark_unread(self, conversation_id: int) -> None:
        if conversation_id and conversation_id != self.conversation_id:
            self.chat_meta.set(conversation_id, unread=True)
            self.refresh_chat_list()

    def cycle_chat_filter(self) -> None:
        index = CHAT_FILTERS.index(self.chat_list_filter) if self.chat_list_filter in CHAT_FILTERS else 0
        self.chat_list_filter = CHAT_FILTERS[(index + 1) % len(CHAT_FILTERS)]
        self.refresh_chat_list()

    def reset_chat_filter(self) -> None:
        """A new chat, branch or companion chat must be visible: back to Active (J4)."""
        if self.chat_list_filter != "active":
            self.chat_list_filter = "active"
            self.refresh_chat_list()

    def cycle_chat(self, delta: int) -> None:
        order = list(self._visible_chats)
        if not order:
            return
        if self.conversation_id in order:
            index = (order.index(self.conversation_id) + delta) % len(order)
        else:
            index = 0 if delta > 0 else len(order) - 1
        self.open_chat(order[index])

    # -- branching and regeneration ----------------------------------------

    def is_last_card(self, card: MessageCard) -> bool:
        """True for the newest reply of the conversation — judged by the message
        list, so a card being built during a reload counts too."""
        replies = [message for message in self.messages if message.role == "assistant"]
        return bool(replies) and replies[-1] is card.message

    def branch_from(self, card: MessageCard, *, include: bool = True, pending_edit: str | None = None) -> None:
        """New chat holding everything up to (and optionally including) this message."""
        if self.busy:
            self.toast("Stop or finish the current request first.", kind="warning")
            return
        if self.conversation_id is None or card.message not in self.messages:
            return
        index = self.messages.index(card.message)
        count = index + 1 if include else index
        if count <= 0:
            self._pending_edit = pending_edit
            self.new_chat()
            return
        anchor = self.messages[count - 1]
        upto_id = anchor.message_id if isinstance(anchor.message_id, int) else None
        base = self.chat_title if self.chat_title != DEFAULT_CHAT_TITLE else "chat"
        title = branch_title(base)  # the sidebar's ⑂ glyph marks the branch (J11)
        self._pending_edit = pending_edit
        self._branch_pending = {"source": self.conversation_id}
        self.session.branch_chat(self.conversation_id, upto_id, count, title)

    def regenerate_menu(self, card: MessageCard, anchor: tk.Misc) -> None:
        theme = self.theme
        menu = tk.Menu(self.root, tearoff=0, bg=theme.surface, fg=theme.text, activebackground=theme.surface_hover, activeforeground=theme.text_strong, bd=0, font=self.fonts.small)
        menu.add_command(label=f"Regenerate with {self.model_label}", command=lambda: self.regenerate_card(card))
        menu.add_separator()
        for label in MODEL_CHOICES:
            if label != self.model_label:
                menu.add_command(label=f"Regenerate with {label} · {MODEL_HINTS[label]}", command=lambda choice=label: self.regenerate_card(card, choice))
        try:
            menu.tk_popup(anchor.winfo_rootx(), anchor.winfo_rooty() + anchor.winfo_height())
        finally:
            menu.grab_release()

    def refresh_status(self) -> None:
        theme = self.theme
        online = self.ready and not self._closing and not self.provider_error
        color = theme.warning if self.busy else (theme.success if online else theme.danger if self.status_text in {"Offline", "Startup failed", "Provider offline"} else theme.warning)
        try:
            self.status_dot.itemconfigure(self.status_oval, fill=color)
            self.status_label.configure(text=self.status_text if self._closing else ("Working" if self.busy else self.status_text))
            self.activity_label.configure(text=self.activity_text)
            self.control_label.configure(text=f"background {self.control_state}" if self.control_state != "unknown" else "")
            self.approval_badge.configure(text=str(self.pending_approvals), bg=theme.accent if self.pending_approvals else theme.surface_alt, fg=theme.accent_ink if self.pending_approvals else theme.muted)
            needs = sum(1 for item in self.inbox_items if item.get("kind") in {"approval", "worker"} and not item.get("read"))
            unread = sum(1 for item in self.inbox_items if not item.get("read"))
            self.bell_badge.configure(text=str(needs) if needs else ("●" if unread else ""), fg=theme.warning if needs else theme.accent)
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

    def refresh_context_pill(self) -> None:
        """C3: tokens of the last reply and, when the route reported its context
        length, how much of that window the prompt used."""
        label = getattr(self, "context_pill_label", None)
        if label is None:
            return
        theme = self.theme
        text, level = context_pill(getattr(self, "_last_metrics", None))
        try:
            if not text or self.view != "chat":
                label.pack_forget()
                return
            colour = {"amber": theme.warning, "red": theme.danger}.get(level, theme.muted)
            label.configure(text=text, fg=colour)
            tooltip = getattr(self, "_context_pill_tooltip", None)
            if tooltip is not None:
                tooltip.text = CONTEXT_LONG_HINT if level != "ok" else "Tokens of the last reply: prompt in · completion out, and the model's context window when the route reports it."
            if not label.winfo_ismapped():
                label.pack(side="left", padx=(10, 0))
        except tk.TclError:
            pass

    def _remember_metrics(self, metrics: Any) -> None:
        self._last_metrics = dict(metrics) if isinstance(metrics, dict) else None
        self.refresh_context_pill()

    def _refresh_model_detail(self) -> None:
        try:
            self.composer.refresh_model_label()
        except (AttributeError, tk.TclError):
            pass

    def current_project_name(self) -> str:
        for project in self.projects:
            if project["id"] == self.project_id:
                return project["name"]
        return "Default workspace"

    def _refresh_project_detail(self) -> None:
        try:
            self.project_button.set_text(compact_activity(self.current_project_name(), 30))
            scope = "all projects" if self.project_filter is None else "this project only"
            counts = next((project for project in self.projects if project["id"] == self.project_id), None)
            extra = f" · {counts['conversation_count']} chats" if counts else ""
            self.project_detail.configure(text=f"New chats start here · sidebar shows {scope}{extra}")
        except (AttributeError, tk.TclError):
            pass

    # -- actions ----------------------------------------------------------

    def focus_composer(self) -> None:
        try:
            self.composer.input.focus_set()
        except (AttributeError, tk.TclError):
            pass

    def send(self) -> None:
        if not self.ready:
            self.toast("Jarvis is still starting up.", kind="warning")
            return
        raw = self.composer.input.value().strip()
        if raw.startswith("/"):
            try:
                from . import ui_popups
                unknown = ui_popups.is_unknown_slash(raw)
            except Exception:
                unknown = False
            if unknown:
                token = raw.split(maxsplit=1)[0]
                self.add_notice(f"Unknown command {compact_activity(token, 40)} — type / to see the list.", kind="warning")
                return
            if not self.busy and self.handle_slash(raw):
                return
        if self.composer.reading:
            self.toast("Still reading the attached files…", kind="warning")
            return
        if self.busy:
            self.queue_follow_up_from_composer()
            return
        self.composer.take(self._send_ready)

    def _send_ready(self, text: str, attachments: list[str], blocks: list[str], names: list[str]) -> None:
        if not text and not attachments and not blocks:
            return
        if self.busy:  # a reply started while the attachments were being read
            self.queue_follow_up(text, attachments, blocks, names)
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
            self.settings.set("deep_confirmed", True)
        self._submit_prompt(text, attachments, blocks, names)

    def _submit_prompt(self, text: str, attachments: list[str], blocks: list[str], names: list[str], *, retitle: bool = True) -> bool:
        if not text:
            text = "Describe the attached image." if attachments else "Review the attached files."
        prompt = text if not blocks else text + "\n\n" + "\n\n".join(blocks)
        if len(prompt) > MAX_PROMPT_CHARS:
            self.toast(f"Jarvis accepts at most {MAX_PROMPT_CHARS:,} characters including attached files.", kind="warning")
            self.composer.input.set_value(text)
            return False
        self._last_user_prompt = prompt
        self.composer.remember(text)
        if retitle and self.chat_title == DEFAULT_CHAT_TITLE:
            self.set_chat_title(chat_title_from_prompt(text))
        self.append_message(Message("user", text, attachments=list(names)))
        working = Message("assistant", "", working=True, prompt=prompt)
        self.active_card = self.append_message(working)
        self.session.submit(prompt, self.model_label, attachments, conversation_id=self.conversation_id)
        self.focus_composer()
        return True

    # -- queued follow-ups (per conversation, like drafts) -------------------

    @property
    def queued(self) -> list[dict[str, Any]]:
        """The follow-ups typed for the open conversation only."""
        key = int(self.conversation_id or 0)
        return self._queued_by_chat.setdefault(key, [])

    def last_turn_status(self) -> str | None:
        return self._last_turn_status.get(int(self.conversation_id or 0))

    def queue_follow_up_from_composer(self) -> None:
        def ready(text: str, attachments: list[str], blocks: list[str], names: list[str]) -> None:
            if text or attachments or blocks:
                self.queue_follow_up(text, attachments, blocks, names)

        self.composer.take(ready)

    def queue_follow_up(self, text: str, images: list[str] | None = None, blocks: list[str] | None = None, names: list[str] | None = None) -> None:
        if len(self.queued) >= 8:
            self.toast("At most 8 queued follow-ups.", kind="warning")
            return
        self.queued.append({"text": str(text), "images": list(images or []), "blocks": list(blocks or []), "names": list(names or [])})
        self.composer.render_queue()
        self.toast("Queued — it will be sent when this reply finishes. ↑ takes it back.", kind="success")

    def remove_queued(self, index: int) -> None:
        if 0 <= index < len(self.queued):
            self.queued.pop(index)
        self.composer.render_queue()

    def send_queued(self, index: int) -> None:
        if self.busy or not (0 <= index < len(self.queued)):
            return
        item = self.queued.pop(index)
        self.composer.render_queue()
        self._submit_prompt(item["text"], item["images"], item["blocks"], item["names"])

    def _drain_queue(self, status: str) -> None:
        """Send the next queued follow-up only after a turn that finished normally,
        and only into the conversation it was typed for."""
        cid = int(self.conversation_id or 0)
        self._last_turn_status[cid] = str(status)
        if status != "complete" or not self.queued:
            self.composer.render_queue()
            return
        item = self.queued.pop(0)
        self.composer.render_queue()

        def send_next() -> None:
            if int(self.conversation_id or 0) != cid:
                self._queued_by_chat.setdefault(cid, []).insert(0, item)  # the chat moved on; keep it for that chat
                return
            if self.busy:
                self.queued.insert(0, item)
                self.composer.render_queue()
                return
            self._submit_prompt(item["text"], item["images"], item["blocks"], item["names"])

        self.root.after(120, send_next)

    # -- notices (persistent, dismissible; toasts are for confirmations) ---

    def add_notice(self, text: str, *, kind: str = "error", details: str = "", action: tuple[str, Callable[[], None]] | None = None, ttl: float | None = None, tag: str = "") -> int:
        """A dismissible strip above the composer; ``action`` adds one button
        (e.g. Undo) and ``ttl`` removes the notice on its own after that many seconds."""
        self._notice_sequence += 1
        notice_id = self._notice_sequence
        if tag:
            self.notices = [item for item in self.notices if item.get("tag") != tag]
        self.notices.append({"id": notice_id, "text": safe_ui_text(text, 400), "kind": kind, "details": safe_ui_text(details, 4_000), "created_at": time.time(), "action": action, "tag": tag})
        self.notices = self.notices[-3:]
        self.render_notices()
        if ttl is not None:
            try:
                self.root.after(int(max(0.2, float(ttl)) * 1000), lambda: self.dismiss_notice(notice_id))
            except tk.TclError:
                pass
        return notice_id

    def dismiss_notice(self, notice_id: int) -> None:
        self.notices = [item for item in self.notices if item.get("id") != notice_id]
        self.render_notices()

    def dismiss_notice_tag(self, tag: str) -> None:
        if any(item.get("tag") == tag for item in self.notices):
            self.notices = [item for item in self.notices if item.get("tag") != tag]
            self.render_notices()

    def render_notices(self) -> None:
        frame = getattr(self, "notice_frame", None)
        if frame is None or not frame.winfo_exists():
            return
        theme = self.theme
        for child in frame.winfo_children():
            child.destroy()
        if not self.notices:
            frame.pack_forget()
            return
        frame.pack(fill="x", padx=self.px(28), pady=(0, 2), before=self.composer_area)
        colors = {"error": theme.danger, "warning": theme.warning, "info": theme.accent, "success": theme.success}
        try:
            column_width = int(self.chat_column.winfo_width())
        except (AttributeError, tk.TclError):
            column_width = 0
        if column_width > 1:
            frame.pack_configure(padx=reading_column_padding(column_width, self.scale))
        wrap = max(self.px(200), (column_width if column_width > 1 else self.content_width()) - 2 * self._reading_padding - self.px(240))
        for item in self.notices:
            row = tk.Frame(frame, bg=theme.surface, highlightbackground=colors.get(item.get("kind"), theme.accent), highlightthickness=1, padx=10, pady=6)
            row.pack(fill="x", pady=(0, 4))
            # Buttons first so the ✕ keeps its place; the text wraps in what is left.
            IconButton(row, self, "×", lambda target=item["id"]: self.dismiss_notice(target), tooltip="Dismiss").pack(side="right")
            action = item.get("action")
            if isinstance(action, tuple) and len(action) == 2 and callable(action[1]):
                RoundButton(row, self, str(action[0]), action[1], kind="accent", padx=10, pady=2, font=self.fonts.tiny).pack(side="right", padx=(0, 6))
            if item.get("details"):
                RoundButton(row, self, "Copy details", lambda target=item: self.copy_text(target["details"], quiet=True), kind="ghost", padx=8, pady=2, font=self.fonts.tiny).pack(side="right", padx=(0, 6))
            tk.Label(row, text=item["text"], bg=theme.surface, fg=theme.text, font=self.fonts.small, anchor="w", justify="left", wraplength=wrap).pack(side="left", fill="x", expand=True)

    # -- drafts per conversation -----------------------------------------

    # -- inbox and notifications ------------------------------------------

    def rebuild_inbox(self) -> None:
        """Everything that needs the operator, derived from store-reported state."""
        items: list[dict[str, Any]] = []
        titles = {chat["id"]: chat["title"] for chat in self.chats}
        for row in self.context_data.get("pending_approvals") or []:
            match = re.fullmatch(r"conversation:([1-9][0-9]*)", str(row.get("scope") or ""))
            cid = int(match.group(1)) if match else None
            items.append({"id": f"approval:{row.get('id')}", "kind": "approval", "title": f"Approval #{row.get('id')} · {row.get('action')}", "detail": compact_activity(row.get("reason") or row.get("resource") or "", 120), "conversation_id": cid, "approval_id": int(row.get("id") or 0), "task_id": None, "created_at": time.time(), "read": False})
        for key, flags in self.chat_meta.values.items():
            if flags.get("unread") and key.isdigit():
                cid = int(key)
                items.append({"id": f"unread:{cid}", "kind": "unread", "title": titles.get(cid, f"Chat {cid}"), "detail": "Reply ready", "conversation_id": cid, "approval_id": None, "task_id": None, "created_at": time.time(), "read": False})
        for task_id, status in list(self._finished_tasks.items())[-20:]:
            items.append({"id": f"task:{task_id}", "kind": "task", "title": f"Task #{task_id} {status}", "detail": "Background task finished — open the context panel for its result", "conversation_id": None, "approval_id": None, "task_id": task_id, "created_at": time.time(), "read": f"task:{task_id}" in self._read_inbox})
        for notice in self.notices:
            items.append({"id": f"notice:{notice['id']}", "kind": "error", "title": compact_activity(notice["text"], 80), "detail": "", "conversation_id": None, "approval_id": None, "task_id": None, "created_at": notice.get("created_at", time.time()), "read": f"notice:{notice['id']}" in self._read_inbox})
        if self.worker_alive is False and any(str(row.get("status", "")).lower() in {"queued", "running", "leased", "retry", "pending"} for row in self.context_data.get("tasks") or []):
            items.append({"id": "worker", "kind": "worker", "title": "Worker offline", "detail": "Queued tasks will not run until `python -m jarvis worker` is running", "conversation_id": None, "approval_id": None, "task_id": None, "created_at": time.time(), "read": "worker" in self._read_inbox})
        self.inbox_items = items
        popover = self.inbox_popover
        if popover is not None:
            try:
                if popover.winfo_exists():
                    popover.refresh()
            except tk.TclError:
                pass
        self.refresh_status()

    def open_inbox(self) -> None:
        self.rebuild_inbox()
        popover = self.inbox_popover
        try:
            if popover is not None and popover.winfo_exists():
                popover.destroy()
                self.inbox_popover = None
                return
        except tk.TclError:
            pass
        try:
            from . import ui_panes
            popover = ui_panes.InboxPopover(self)
        except Exception as exc:
            self.toast(f"Inbox unavailable ({type(exc).__name__}). Pending approvals: {self.pending_approvals}.", kind="warning")
            self.show_approvals()
            return
        self.inbox_popover = popover
        try:
            anchor = self.bell_button
            popover.open_at(anchor.winfo_rootx() - self.px(300), anchor.winfo_rooty() + anchor.winfo_height() + 4)
        except tk.TclError:
            pass

    def open_inbox_item(self, item: dict[str, Any]) -> None:
        kind = str(item.get("kind") or "")
        self._read_inbox.add(str(item.get("id")))
        if kind == "approval":
            cid = item.get("conversation_id")
            approval_id = int(item.get("approval_id") or 0)
            if cid and cid != self.conversation_id:
                self.open_chat(int(cid), target_approval_id=approval_id)  # scrolls once the chat has loaded
            else:
                self.set_view("chat")
                self.scroll_to_approval(approval_id)
        elif kind == "unread" and item.get("conversation_id"):
            self.set_view("chat")
            self.open_chat(int(item["conversation_id"]))
        elif kind == "task":
            task_id = item.get("task_id")
            row = next((task for task in self.context_data.get("tasks") or [] if int(task.get("id") or 0) == task_id), None)
            if row is not None:
                self.open_task_detail(row)
            else:
                self.set_view("chat")
                self.set_context_visible(True)
        elif kind == "worker":
            self.toast("Start the worker with `python -m jarvis worker` (or the installed service).", kind="warning")
        self.rebuild_inbox()

    def mark_all_read(self) -> None:
        for item in self.inbox_items:
            self._read_inbox.add(str(item.get("id")))
        for key, flags in list(self.chat_meta.values.items()):
            if flags.get("unread"):
                self.chat_meta.set(int(key), unread=False)
        self.refresh_chat_list()
        self.rebuild_inbox()

    def _start_tray(self) -> None:
        try:
            from . import ui_win
            tray = ui_win.TrayIcon(self.tray_actions, tooltip="JARVIS Desktop")
            if tray.start(timeout_s=2.0):
                self.tray = tray
        except Exception:
            self.tray = None

    def notify_event(self, kind: str, title: str, body: str, *, conversation_id: int | None = None) -> None:
        """OS notification per the Notifications settings; never for the focused chat unless asked."""
        try:
            focused = self.root.focus_displayof() is not None
        except (KeyError, tk.TclError):
            focused = False
        if kind == "reply":
            mode = str(self.settings.get("notify_reply", "unfocused"))
            if mode == "never" or (mode == "unfocused" and focused and conversation_id == self.conversation_id):
                return
        elif kind == "approval" and not bool(self.settings.get("notify_approval", True)):
            return
        elif kind == "task" and not bool(self.settings.get("notify_task", True)):
            return
        self._last_notified_chat = conversation_id
        delivered = False
        tray = self.tray
        if tray is not None:
            try:
                delivered = bool(tray.balloon(compact_activity(title, 60), compact_activity(body, 180)))
            except Exception:
                delivered = False
        if not delivered:
            try:
                from . import ui_win
                delivered = ui_win.notify(compact_activity(title, 60), compact_activity(body, 180)) != "unsupported"
            except Exception:
                delivered = False
        if not delivered and not focused:
            flash_window(self.root)

    def _drain_tray_actions(self) -> None:
        while True:
            try:
                action = self.tray_actions.get_nowait()
            except queue.Empty:
                return
            if action == "open" or action == "balloon_click":
                bring_window_forward(self.root)
                target = self._last_notified_chat if action == "balloon_click" else None
                if target and target != self.conversation_id and not self.busy:
                    self.open_chat(int(target))
                self.set_view("chat")
            elif action == "new_chat":
                bring_window_forward(self.root)
                self.new_chat()
            elif action == "quit":
                self.close()

    def _track_tasks(self) -> None:
        """Notice tasks that finished since the last context refresh (store-reported)."""
        current: dict[int, str] = {}
        for row in self.context_data.get("tasks") or []:
            try:
                current[int(row.get("id"))] = str(row.get("status") or "").lower()
            except (TypeError, ValueError):
                continue
        for task_id, status in current.items():
            previous = self._task_statuses.get(task_id)
            if previous and previous != status and status in {"done", "complete", "completed", "failed", "error", "cancelled"}:
                self._finished_tasks[task_id] = status
                self.notify_event("task", f"Task #{task_id} {status}", compact_activity(next((row.get("prompt") for row in self.context_data.get("tasks") or [] if int(row.get("id") or 0) == task_id), ""), 160))
        self._task_statuses = current

    # -- companion (quick-ask window) --------------------------------------

    def open_companion(self) -> None:
        companion = self.companion
        try:
            if companion is not None and companion.winfo_exists():
                companion.show()
                return
        except tk.TclError:
            pass
        try:
            from . import ui_panes
            self.companion = ui_panes.CompanionWindow(self)
            self.companion.show()
        except Exception as exc:
            self.companion = None
            bring_window_forward(self.root)
            self.add_notice(f"The quick-ask window could not open ({type(exc).__name__}); the main window was raised instead.", kind="warning")

    def companion_send(self, text: str, images: list[str] | None = None) -> None:
        text = str(text or "").strip()
        image_paths = [str(path) for path in (images or []) if path]
        if not text and not image_paths:
            return
        if not self.ready:
            self.toast("Jarvis is still starting up.", kind="warning")
            return
        if self.busy:
            self.queue_follow_up(text, image_paths, [], [Path(path).name for path in image_paths])
            return
        target = self._companion_conversation
        if target is not None and target != self.conversation_id:
            # Subsequent Enters continue the companion's own chat; the main window stays put.
            self.session.submit(text, self.model_label, image_paths, conversation_id=target)
            return
        if self.messages and target != self.conversation_id:
            # The main chat is in use: ask for a detached chat, submit on chat_created.
            self._companion_pending = text
            self._companion_pending_images = image_paths
            self.session.new_chat(self.project_id, activate=False)
            return
        self._companion_conversation = self.conversation_id
        self._submit_prompt(text, image_paths, [], [Path(path).name for path in image_paths])

    def approval_detail_for(self, approval_id: int) -> dict[str, Any] | None:
        """The cached store row for one approval (from the matching card), or None."""
        for card in self.cards:
            if card.message.approval_id == approval_id and isinstance(card.message.approval, dict) and card.message.approval and not card.message.approval.get("missing"):
                return dict(card.message.approval)
        return None

    def companion_open_main(self) -> None:
        bring_window_forward(self.root)
        self.set_view("chat")
        self.focus_composer()

    def _forward_view_event(self, kind: str, payload: Any) -> None:
        if kind in {"facts", "claim_history"} and self.memory_view is not None:
            try:
                self.memory_view.on_event(kind, payload)
            except Exception as exc:
                self._report_ui_error("memory view", exc)
        elif kind == "routines" and self.routines_view is not None:
            try:
                self.routines_view.on_event(kind, payload)
            except Exception as exc:
                self._report_ui_error("routines view", exc)
        companion = self.companion
        if companion is not None and kind in {"busy", "activity", "delta", "assistant", "new_chat", "chat_created", "approval_decided", "approval_detail"}:
            try:
                if companion.winfo_exists():
                    companion.on_event(kind, payload)
            except Exception as exc:
                self._report_ui_error("companion", exc)

    def _apply_pending_edit(self) -> None:
        text = self._pending_edit
        self._pending_edit = None
        self._branch_pending = None
        if text:
            self.composer.input.set_value(text)
            self.composer.input.mark_set("insert", "end")

    def _stash_draft(self) -> None:
        composer = getattr(self, "composer", None)
        if composer is None or self.conversation_id is None:
            return
        try:
            text = composer.input.value()
        except tk.TclError:
            return
        if text.strip() or composer.attachments:
            self.drafts[int(self.conversation_id)] = {"text": text, "attachments": list(composer.attachments)}
        else:
            self.drafts.pop(int(self.conversation_id), None)

    def _restore_draft(self) -> None:
        composer = getattr(self, "composer", None)
        if composer is None or self.conversation_id is None:
            return
        conversation_id = int(self.conversation_id)
        draft = self.drafts.pop(conversation_id, None)
        if not draft:
            return
        try:
            composer.input.set_value(str(draft.get("text", "")))
        except tk.TclError:
            return
        items = [item for item in draft.get("attachments", []) if isinstance(item, dict) and item.get("path")]
        if not items:
            return

        def restore(found: Any, error: str | None) -> None:
            if error or self.conversation_id != conversation_id:
                return
            kept = set(found or [])
            try:
                composer.attachments = [item for item in items if item.get("path") in kept]
                composer.render_chips()
            except tk.TclError:
                pass

        # The file probes block, so they run on the pool and come back as a ui_job.
        self.run_job("draft-attachments", probe_paths, [str(item.get("path")) for item in items], on_done=restore)

    def handle_slash(self, raw: str) -> bool:
        """Slash commands: quick, discoverable, never sent to the model by accident."""
        command, _, rest = raw[1:].partition(" ")
        command = command.strip().lower()
        rest = rest.strip()
        if command in {"new", "n"}:
            self.composer.input.set_value("")
            self.new_chat()
        elif command == "model":
            wanted = rest.lower()
            match = next((label for label in MODEL_CHOICES if label.lower().startswith(wanted)), None) if wanted else None
            if match:
                self.set_model(match)
                self.composer.input.set_value("")
                self.toast(f"Model: {match}")
            else:
                self.toast("Use /model auto | fast | reasoning | coding | deep", kind="warning")
        elif command == "theme":
            wanted = rest.lower()
            match = next((key for key in THEME_ORDER if key.startswith(wanted)), None) if wanted else None
            self.composer.input.set_value("")
            if match:
                self.set_theme(match)
            else:
                self.cycle_theme()
        elif command == "project":
            self.composer.input.set_value("")
            if rest:
                self.session.create_project(rest)
            else:
                self.choose_project()
        elif command == "task":
            if not rest:
                self.toast("Use /task <what to do in the background>", kind="warning")
                return True
            self.composer.input.set_value("")
            self.session.queue_task(rest, self.model_label)
        elif command == "remember":
            self.composer.input.set_value("")
            self.remember_fact_dialog(rest)
        elif command == "export":
            self.composer.input.set_value("")
            self.export_chat("html" if rest.lower().startswith("html") else None)
        elif command == "council":
            self.composer.input.set_value("")
            self.set_view("council")
        elif command in {"context", "ctx"}:
            self.composer.input.set_value("")
            self.toggle_context()
        elif command in {"settings", "prefs"}:
            self.composer.input.set_value("")
            self.open_settings()
        elif command in {"facts", "memory"}:
            self.composer.input.set_value("")
            self.set_view("memory")
        elif command in {"schedule", "routine", "routines"}:
            self.composer.input.set_value("")
            self.set_view("routines")
            if rest and self.routines_view is not None:
                self.routines_view.prefill(rest)
        elif command == "inbox":
            self.composer.input.set_value("")
            self.open_inbox()
        elif command in {"help", "?"}:
            self.composer.input.set_value("")
            self.show_shortcuts()
        else:
            return False
        return True

    def queue_current_prompt(self) -> None:
        text = self.composer.input.value().strip()
        if not text:
            self.queue_task_dialog()
            return
        self.composer.input.set_value("")
        self.composer.remember(text)
        self.session.queue_task(text, self.model_label)

    def open_task_detail(self, task: dict[str, Any]) -> None:
        """Task rows in the context panel and inbox open this sheet."""
        existing = getattr(self, "task_sheet", None)
        if existing is not None:
            try:
                if existing.winfo_exists():
                    existing.destroy()
            except tk.TclError:
                pass
        try:
            self.task_sheet = TaskDetailSheet(self, task)
        except tk.TclError as exc:
            self.task_sheet = None
            self.toast(f"Could not open the task: {exc}", kind="error")

    def queue_task_dialog(self) -> None:
        self._ask_sheet(
            "Queue a background task", "What should the Jarvis worker do while you keep chatting?", "",
            lambda text: self.session.queue_task(text, self.model_label), accept_label="Queue",
        )

    def continue_last(self) -> None:
        if self.busy:
            self.toast("Wait for the current reply to finish.", kind="warning")
            return
        self.composer.input.set_value("Continue exactly where you left off.")
        self.send()

    def regenerate_card(self, card: MessageCard, model_label: str | None = None) -> None:
        """Regenerate the last reply in place, keeping earlier versions in a pager."""
        if self.busy or not self._last_user_prompt:
            if self.busy:
                self.toast("Wait for the current reply to finish.", kind="warning")
            return
        label = model_label if model_label in MODEL_CHOICES else self.model_label
        if card.message.role == "assistant" and self.is_last_card(card) and self.messages and self.messages[-1] is card.message:
            previous = card.message
            self.messages = [message for message in self.messages if message is not previous]
            self.cards = [item for item in self.cards if item is not card]
            card.destroy()
            working = Message("assistant", "", working=True, prompt=previous.prompt or self._last_user_prompt, versions=list(previous.versions) or ([previous.content] if previous.content else []))
            if label != self.model_label:
                working.steps.append(f"regenerating with {label}")
            self._regenerating_message = working
            self.active_card = self.append_message(working)
            self.session.submit(working.prompt, label, [], conversation_id=self.conversation_id)
            self.focus_composer()
            return
        if label != self.model_label:
            self.set_model(label)
        self.regenerate_last()

    def decide_inline_approval(self, card: MessageCard, approval_id: int, approve: bool, *, scope: str = "once", reason: str = "") -> None:
        card.message.approval_decision = self._decision_label(approve, scope) + " · recording…"
        card.refresh_approval()
        if not approve and reason.strip():
            self._deny_reasons[int(approval_id)] = reason.strip()
        self.session.decide_approval(approval_id, approve, scope)

    def decide_context_approval(self, approval_id: int, approve: bool, *, scope: str = "once", reason: str = "") -> None:
        """Decisions from the Approvals window and the context panel (same card, same store call)."""
        if not approve and reason.strip():
            self._deny_reasons[int(approval_id)] = reason.strip()
        self.session.decide_approval(approval_id, approve, scope)
        for card in self.cards:
            if card.message.approval_id == approval_id:
                card.message.approval_decision = self._decision_label(approve, scope) + " · recording…"
                card.refresh_approval()

    @staticmethod
    def _decision_label(approve: bool, scope: str, *, until: float | None = None, reason_sent: bool = False, when: float | None = None) -> str:
        """The §4 decided-state line."""
        clock = format_clock(when if when is not None else time.time())
        if not approve:
            return "Denied" + (" · reason sent" if reason_sent else "")
        if scope == "session":
            return f"Allowed in this chat until {format_until(until) if until else 'tomorrow ' + clock}"
        if scope == "always":
            return "Always allowed"
        return f"Approved once · {clock}"

    def focus_next_pending_card(self, current: Any = None) -> None:
        """Tab on an approval card: move to the next card still waiting for a decision."""
        pending = [card for card in self.cards if card.message.approval_id is not None and not card.message.approval_decision and getattr(card, "approval_frame", None) is not None]
        if not pending:
            self.focus_composer()
            return
        frames = [getattr(card, "approval_frame", None) for card in pending]
        index = 0
        if current in frames:
            index = (frames.index(current) + 1) % len(frames)
        target = frames[index]
        try:
            self.scroll_widget_into_view(target)
            target.focus()
        except tk.TclError:
            pass

    def focus_pending_approval(self) -> bool:
        """Tab in the composer: jump to the first approval card waiting for a decision."""
        for card in self.cards:
            frame = getattr(card, "approval_frame", None)
            if card.message.approval_id is not None and not card.message.approval_decision and frame is not None:
                try:
                    self.scroll_widget_into_view(frame)
                    frame.focus()
                    return True
                except tk.TclError:
                    return False
        return False

    def resume_after_approval(self, card: MessageCard) -> None:
        """Re-send the request that stopped at an approval as a new turn."""
        if self.busy:
            self.toast("Wait for the current reply to finish.", kind="warning")
            return
        prompt = card.message.prompt or self._last_user_prompt
        if not prompt:
            self.toast("There is no request to retry.", kind="warning")
            return
        card.message.approval_resumable = False
        card.refresh_approval()
        self._last_user_prompt = prompt
        self.append_message(Message("user", prompt))
        working = Message("assistant", "", working=True, prompt=prompt)
        working.retried_after = int(card.message.approval_id or 0) or None
        self.active_card = self.append_message(working)
        self.session.submit(prompt, self.model_label, [], conversation_id=self.conversation_id)
        self.focus_composer()

    def send_text(self, text: str) -> None:
        """Send a command sentence (deny instruction, Erase…, store it) as the
        user's own message; such sentences never become the chat's title."""
        if not self.ready:
            self.toast("Jarvis is still starting up.", kind="warning")
            return
        if self.busy:
            self.queue_follow_up(text)
            return
        self._submit_prompt(str(text), [], [], [], retitle=False)

    def queue_task_text(self, prompt: str) -> None:
        self.session.queue_task(str(prompt), self.model_label)

    def revoke_grant(self, grant_id: int) -> None:
        self.session.revoke_grant(int(grant_id))

    def scroll_to_approval(self, approval_id: int) -> None:
        for card in self.cards:
            if card.message.approval_id == approval_id:
                self.set_view("chat")
                self.scroll_widget_into_view(card)
                button = getattr(card, "approve_button", None)
                if button is not None and button.winfo_exists():
                    button.focus_set()
                return
        self.show_approvals()

    # -- context panel / projects -------------------------------------------

    def toggle_topmost(self) -> None:
        value = not bool(self.settings.get("topmost", False))
        self.settings.set("topmost", value)
        try:
            self.root.wm_attributes("-topmost", value)
        except tk.TclError:
            pass
        self.toast("Jarvis stays on top of other windows." if value else "Jarvis no longer stays on top.")

    def cycle_timeline_mode(self) -> None:
        index = TIMELINE_MODES.index(self.timeline_mode) if self.timeline_mode in TIMELINE_MODES else 0
        self.set_timeline_mode(TIMELINE_MODES[(index + 1) % len(TIMELINE_MODES)])

    def set_timeline_mode(self, mode: str) -> None:
        if mode not in TIMELINE_MODES:
            return
        self.timeline_mode = mode
        self.settings.set("timeline_mode", mode)
        self.toast(f"Timeline: {TIMELINE_MODE_HINTS[mode]}")
        for card in self.cards[-12:]:
            if card.message.role == "assistant" and not card.message.working:
                try:
                    card.render_final()
                except tk.TclError:
                    pass
        self.root.after(120, self._refit_texts)

    def _apply_step(self, payload: Any) -> None:
        """Merge one structured tool event into the working reply."""
        if not isinstance(payload, dict) or self.active_card is None:
            return
        message = self.active_card.message
        seq = int(payload.get("seq") or 0)
        entry = {key: value for key, value in payload.items() if key != "conversation_id"}
        for index, existing in enumerate(message.tools):
            if int(existing.get("seq") or 0) == seq:
                message.tools[index] = entry
                break
        else:
            if len(message.tools) < TOOL_LOG_LIMIT:
                message.tools.append(entry)
        if message.working:
            self.active_card.refresh_steps()
            if self.messages_view.stick_to_bottom:
                self.messages_view.scroll_to_end(light=True)

    def persist_timeline(self, message: Message) -> None:
        if message.role != "assistant" or message.working or message.message_id is None or not self.conversation_id:
            return
        try:
            self.session.save_timeline(self.conversation_id, message.message_id, message.timeline_record())
        except Exception:
            pass

    # -- side pane (Diff · Artifact · File, Ctrl+Shift+P) --------------------

    def toggle_side_pane(self, tab: str | None = None) -> None:
        pane = self.side_pane
        if pane is None:
            self.toast("The side pane is unavailable in this build.", kind="warning")
            return
        pane.toggle(tab)
        self._persist_side_pane()
        self._apply_layout_budget()

    def _persist_side_pane(self) -> None:
        pane = self.side_pane
        if pane is None:
            return
        visible = bool(getattr(pane, "visible", False))
        if bool(self.settings.get("side_pane", False)) != visible:
            self.settings.set("side_pane", visible)

    def show_diff_record(self, record: Any) -> None:
        """A reply's DiffChip: load its report and show the Diff tab."""
        pane = self.side_pane
        if pane is None:
            return
        pane.set_report(record)
        self.toggle_side_pane("diff")

    def show_artifact(self, artifact: Any) -> None:
        pane = self.side_pane
        if pane is None:
            self.toast("The side pane is unavailable in this build.", kind="warning")
            return
        pane.show_artifact(artifact)
        self._persist_side_pane()

    def view_file_in_pane(self, path: str | None) -> None:
        pane = self.side_pane
        if not path:
            return
        if pane is None:
            self.open_path(path)
            return
        pane.show_file(path)
        pane.show("file")
        self._persist_side_pane()

    def save_code_as(self, code: str, language: str = "", artifact: Any = None) -> None:
        """User-initiated save of one code block (same footing as export)."""
        if artifact is not None:
            extension = str(artifact.suggested_extension())
            initial = str(artifact.suggested_filename())
        else:
            key = re.sub(r"[^a-z0-9]", "", str(language or "").lower())[:8]
            extension = f".{key}" if key else ".txt"
            initial = f"snippet{extension}"
        try:
            target = filedialog.asksaveasfilename(
                parent=self.root, title="Save code as", defaultextension=extension, initialfile=initial,
                filetypes=[(f"{extension} files", f"*{extension}"), ("All files", "*.*")],
            )
        except tk.TclError:
            target = ""
        if not target:
            return
        self._write_export(str(target), str(code), verb="Saved")

    def toggle_context(self) -> None:
        self.set_context_visible(not self.context_visible)

    def set_context_visible(self, visible: bool) -> None:
        self.settings.set("context_panel", bool(visible))
        self._context_auto_hidden = False
        try:
            self._show_context(bool(visible))
            if self.context_visible:
                self.refresh_context()
        except tk.TclError:
            self._build()
        self._apply_layout_budget()
        self.root.after(80, self._refit_texts)

    def refresh_context(self) -> None:
        if not self.ready or not self.context_visible:
            return
        self.session.request_context(self.chat_started_at)

    def _context_tick(self) -> None:
        try:
            if self.context_visible and self.ready and not self.busy:
                self.refresh_context()
        except Exception as exc:
            self._report_ui_error("context refresh", exc)
        try:
            self.root.after(45_000, self._context_tick)
        except tk.TclError:
            pass

    def choose_project(self) -> None:
        theme = self.theme
        menu = tk.Menu(self.root, tearoff=0, bg=theme.surface, fg=theme.text, activebackground=theme.surface_hover, activeforeground=theme.text_strong, bd=0, font=self.fonts.small)
        menu.add_command(label="Show chats from all projects" if self.project_filter is not None else "✓ Showing chats from all projects", command=lambda: self.set_project_filter(None))
        menu.add_separator()
        for project in self.projects:
            marker = "● " if project["id"] == self.project_id else "   "
            menu.add_command(label=f"{marker}{project['name']}  ({project['conversation_count']} chats)", command=lambda target=project["id"]: self.select_project(target))
        menu.add_separator()
        menu.add_command(label="New project…", command=self.new_project)
        menu.add_command(label="Open workspace folder", command=lambda: self.open_path(self.context_data.get("workspace") or self.workspace))
        try:
            x = self.project_button.winfo_rootx()
            y = self.project_button.winfo_rooty()
            menu.tk_popup(x, y - 8)
        finally:
            menu.grab_release()

    def set_project_filter(self, project_id: int | None) -> None:
        self.project_filter = project_id
        self._refresh_project_detail()
        self.refresh_chat_list()

    def select_project(self, project_id: int) -> None:
        """Make a project current: new chats go there and the list narrows to it."""
        if self.busy:
            self.toast("Stop or finish the current request first.", kind="warning")
            return
        self.project_id = int(project_id)
        self.project_filter = int(project_id)
        self._refresh_project_detail()
        self.refresh_chat_list()
        self.file_index = None  # the "@" picker waits for the new workspace's index
        self.session.new_chat(self.project_id)
        self.session.request_file_index()
        self.toast(f"Project: {self.current_project_name()}")

    def new_project(self) -> None:
        self._ask_sheet(
            "New project", "Project name — it gets its own isolated workspace folder", "",
            lambda name: self.session.create_project(name), accept_label="Create",
        )

    EXECUTABLE_SUFFIXES = frozenset({
        ".bat", ".cmd", ".com", ".exe", ".hta", ".js", ".jse", ".lnk", ".msi", ".ps1",
        ".py", ".pyw", ".reg", ".scr", ".vbs", ".vbe", ".wsf", ".wsh", ".jar", ".sh",
        ".url", ".website", ".appref-ms", ".pif", ".cpl", ".msc", ".inf", ".application",
        ".docm", ".xlsm", ".pptm", ".psm1", ".vb", ".wsc", ".sct", ".ps1xml", ".msp",
        ".ps2", ".ps2xml", ".psc1", ".psc2", ".gadget", ".mst", ".diagcab", ".vbscript",
        ".settingcontent-ms", ".scf", ".library-ms", ".searchconnector-ms", ".theme", ".themepack",
        ".deskthemepack", ".chm", ".xbap", ".msh", ".mshxml", ".ws", ".iso", ".img", ".vhd", ".vhdx",
    })
    # Rendered by the browser, which runs their scripts: opened only after a one-line confirm.
    BROWSER_CONFIRM_SUFFIXES = frozenset({".html", ".htm", ".svg"})
    DOCUMENT_SUFFIXES = frozenset({
        ".txt", ".md", ".markdown", ".rst", ".log", ".csv", ".tsv", ".json", ".jsonl", ".yaml",
        ".yml", ".toml", ".ini", ".cfg", ".xml", ".html", ".htm", ".css", ".pdf", ".png",
        ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp", ".docx", ".xlsx", ".pptx", ".odt",
        ".ods", ".odp", ".rtf", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".kt", ".c",
        ".h", ".cc", ".cpp", ".hpp", ".cs", ".swift", ".sql", ".php", ".lua", ".r", ".ipynb",
        ".mp3", ".mp4", ".wav", ".zip",
    })

    def _trusted_roots(self) -> list[Path]:
        roots = []
        for value in (self.context_data.get("workspace") if isinstance(self.context_data, dict) else None, self.workspace, self.data_dir):
            if value:
                try:
                    roots.append(Path(str(value)).resolve())
                except (OSError, ValueError):
                    continue
        return roots

    def _inside_trusted_root(self, target: Path) -> bool:
        try:
            resolved = target.resolve()
        except (OSError, ValueError):
            return False
        for root in self._trusted_roots():
            try:
                resolved.relative_to(root)
                return True
            except ValueError:
                continue
        return False

    def open_path(self, path: str | None) -> None:
        """Open folders and known document types that exist locally; everything
        else is revealed in Explorer, wherever it sits (a model can write any file
        into the workspace, so the workspace earns no exemption). HTML and SVG open
        in the browser only after a one-line confirm; URLs, URI schemes and UNC
        shares are refused outright (they came from tool output)."""
        if not path:
            return
        text = str(path).strip()
        if not is_local_path_text(text):
            self.toast("That is not a local file path, so it was not opened.", kind="warning")
            return
        target = Path(text)
        try:
            exists = target.exists()
        except (OSError, ValueError):
            exists = False
        if not exists:
            self.toast(f"{compact_activity(text, 80)} does not exist.", kind="warning")
            return
        if target.is_file():
            suffix = target.suffix.lower()
            if suffix in self.EXECUTABLE_SUFFIXES:
                self.reveal_path(text)
                self.toast(f"{target.name} is a script or program — revealed in Explorer instead of running it.", kind="warning")
                return
            if suffix in self.BROWSER_CONFIRM_SUFFIXES:
                self.add_notice(
                    f"Open {compact_activity(target.name, 60)} in your browser? It can run scripts.",
                    kind="warning", action=("Open", lambda target=target: self._start_file(target)), tag=f"open:{text}", ttl=60,
                )
                return
            if suffix not in self.DOCUMENT_SUFFIXES:
                self.reveal_path(text)
                self.toast(f"{target.name} is not a known document type — revealed in Explorer instead of opening it.", kind="warning")
                return
        self._start_file(target)

    def _start_file(self, target: Path) -> None:
        self.dismiss_notice_tag(f"open:{target}")
        try:
            os.startfile(str(target))  # type: ignore[attr-defined]
        except (OSError, AttributeError) as exc:
            self.toast(f"Could not open {compact_activity(str(target), 80)}: {exc}", kind="error")

    def reveal_path(self, path: str | None) -> None:
        if not path or not is_local_path_text(str(path)):
            return
        try:
            if sys.platform == "win32":
                subprocess.Popen(["explorer", "/select,", str(Path(path))])
            else:
                os.startfile(str(Path(path).parent))  # type: ignore[attr-defined]
        except (OSError, AttributeError) as exc:
            self.toast(f"Could not reveal {path}: {exc}", kind="error")

    def file_menu(self, event: Any, item: dict[str, Any]) -> None:
        theme = self.theme
        path = str(item.get("path") or "")
        menu = tk.Menu(self.root, tearoff=0, bg=theme.surface, fg=theme.text, activebackground=theme.surface_hover, activeforeground=theme.text_strong, bd=0, font=self.fonts.small)
        menu.add_command(label="View in side pane", command=lambda: self.view_file_in_pane(path))
        menu.add_command(label="Open", command=lambda: self.open_path(path))
        menu.add_command(label="Reveal in Explorer", command=lambda: self.reveal_path(path))
        menu.add_command(label="Attach to next message", command=lambda: self.composer.add_files([path]))
        menu.add_command(label="Insert path into message", command=lambda: self.composer.input.insert("insert", f"`{item.get('relative') or path}` "))
        menu.add_command(label="Copy path", command=lambda: self.copy_text(path))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def remember_fact_dialog(self, seed: str = "", *, subject: str = "", predicate: str = "", value: str = "") -> None:
        """Build the exact governed-memory command so it is never mistyped.
        ``subject``/``predicate``/``value`` prefill explicitly (a multi-word predicate
        cannot survive the legacy space-split seed); ``seed`` stays for callers."""
        theme = self.theme
        dialog = tk.Toplevel(self.root)
        dialog.title("Remember a project fact")
        dialog.configure(bg=theme.bg)
        dialog.transient(self.root)
        dialog.resizable(False, False)
        _apply_titlebar_theme(dialog, theme.dark)
        body = tk.Frame(dialog, bg=theme.bg, padx=self.px(22), pady=self.px(18))
        body.pack()
        tk.Label(body, text="Remember a project fact", bg=theme.bg, fg=theme.text_strong, font=self.fonts.h2).pack(anchor="w")
        tk.Label(body, text=f"Stored for {self.current_project_name()} through the governed memory command. Jarvis will use it in this project and confirm with a receipt from the store.", bg=theme.bg, fg=theme.muted, font=self.fonts.small, wraplength=self.px(420), justify="left").pack(anchor="w", pady=(2, 10))
        fields: dict[str, tk.Entry] = {}
        for name, hint in (("subject", "what the fact is about, e.g. deploy target"), ("predicate", "the relation, e.g. hostname"), ("value", "the value, e.g. atlas.local")):
            tk.Label(body, text=name.title(), bg=theme.bg, fg=theme.text, font=self.fonts.small_bold).pack(anchor="w")
            entry = tk.Entry(body, bg=theme.surface, fg=theme.text, insertbackground=theme.accent, bd=0, highlightthickness=1, highlightbackground=theme.border_strong, highlightcolor=theme.accent, font=self.fonts.body, width=48)
            entry.pack(fill="x", ipady=6, pady=(2, 8))
            Tooltip(entry, hint, self)
            fields[name] = entry
        seeded = fact_dialog_prefill(seed, subject=subject, predicate=predicate, value=value)
        for name, entry in fields.items():
            if seeded.get(name):
                entry.insert(0, seeded[name])
        first_empty = next((name for name in ("subject", "predicate", "value") if not seeded.get(name)), "subject")

        def accept(_event: Any = None) -> None:
            values = {name: entry.get().strip() for name, entry in fields.items()}
            if not all(values.values()):
                self.toast("Subject, predicate and value are all required.", kind="warning")
                return
            command = "Remember this project fact: " + json.dumps(values, ensure_ascii=False)
            dialog.destroy()
            self.composer.input.set_value(command)
            self.send()

        actions = tk.Frame(body, bg=theme.bg)
        actions.pack(fill="x", pady=(4, 0))
        RoundButton(actions, self, "Remember", accept, kind="accent", padx=14, pady=6).pack(side="right")
        RoundButton(actions, self, "Cancel", dialog.destroy, kind="ghost", padx=14, pady=6).pack(side="right", padx=(0, 8))
        for entry in fields.values():
            entry.bind("<Return>", accept)
        dialog.bind("<Escape>", lambda _e: dialog.destroy())
        dialog.update_idletasks()
        dialog.geometry(f"+{self.root.winfo_rootx() + (self.root.winfo_width() - dialog.winfo_width()) // 2}+{self.root.winfo_rooty() + self.px(140)}")
        fields[first_empty].focus_set()

    def _find_step(self, event: Any, delta: int) -> str | None:
        """F3 / Shift+F3: step through Find matches; opens Find when it is hidden."""
        widget = getattr(event, "widget", None)
        try:
            if widget is not None and widget.winfo_toplevel() is not self.root:
                return None
        except (tk.TclError, AttributeError):
            return None
        if self.view != "chat":
            return None
        if not self.find_visible:
            self.toggle_find()
            return "break"
        self.find_bar.step(delta)
        return "break"

    def toggle_find(self) -> None:
        self.find_visible = not self.find_visible
        if self.find_visible:
            self.find_bar.pack(fill="x", before=self.messages_view)
            self.find_bar.entry.focus_set()
            self.find_bar.entry.select_range(0, "end")
        else:
            self.find_bar.clear()
            self.find_bar.pack_forget()
            self.focus_composer()

    def scroll_widget_into_view(self, widget: tk.Misc) -> None:
        try:
            self.messages_view.update_idletasks()
            top = widget.winfo_rooty() - self.messages_view.inner.winfo_rooty()
            total = max(1, self.messages_view.inner.winfo_height())
            self.messages_view.stick_to_bottom = False
            self.messages_view.canvas.yview_moveto(max(0.0, (top - 80) / total))
        except tk.TclError:
            pass

    def _on_messages_scroll(self, first: str, last: str) -> None:
        self.messages_view._on_scroll(first, last)
        try:
            at_bottom = float(last) >= 0.995
            if at_bottom or not self.messages:
                self.jump_button.place_forget()
            else:
                self.jump_button.place(relx=0.5, rely=1.0, y=-self.px(12), anchor="s")
                self.jump_button.lift()
        except (tk.TclError, ValueError):
            pass
        if getattr(self, "_stale_cards", None) and getattr(self, "_stale_refit_after", None) is None:
            try:
                self._stale_refit_after = self.root.after(100, self._refit_stale_cards)
            except tk.TclError:
                self._stale_refit_after = None

    def open_settings(self) -> None:
        if self.settings_window is not None and self.settings_window.winfo_exists():
            self.settings_window.lift()
            self.settings_window.focus_set()
            return
        self.settings_window = SettingsWindow(self)
        if self.ready:
            self.session.request_grants()

    def hotkey_label(self) -> str:
        return "Ctrl+Alt+J"

    def open_palette_with(self, text: str) -> None:
        self.open_palette()
        if self.palette is not None:
            self.palette.entry.insert(0, text)
            self.palette.refresh(text)

    def use_suggestion(self, text: str) -> None:
        self.composer.input.set_value(text)
        self.focus_composer()

    def edit_prompt(self, text: str, card: MessageCard | None = None) -> None:
        """Edit a past prompt: the resend happens in a branch, the original chat stays."""
        if card is not None and card.message in self.messages and len(self.messages) > 1:
            self.branch_from(card, include=False, pending_edit=text)
            return
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
        """Ctrl+R: regenerate the newest reply in place (same pager as the card's menu)."""
        if self.busy or not self._last_user_prompt:
            if self.busy:
                self.toast("Wait for the current reply to finish.", kind="warning")
            return
        last = self.cards[-1] if self.cards else None
        if last is not None and last.message.role == "assistant" and not last.message.working and self.is_last_card(last):
            self.regenerate_card(last)
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
        if self.busy and not self.stopping:
            self.stopping = True
            self.activity_text = "Stopping safely… finishing the current step"
            try:
                self.composer.stop_button.set_text("Stopping…")
                self.composer.stop_button.set_enabled(False)
            except (AttributeError, tk.TclError):
                pass
            self.refresh_status()
            self.session.cancel()

    def new_chat(self) -> None:
        if self.busy:
            self.toast("Stop or finish the current request first.", kind="warning")
            return
        self.session.new_chat(self.project_id)

    def open_chat(self, conversation_id: int, *, target_message_id: int | None = None, target_approval_id: int | None = None) -> None:
        """Open a chat; with a target the view scrolls to that message (a palette
        search hit) or approval card (an inbox row) once the chat has loaded."""
        if self.busy:
            self.toast("Stop or finish the current request first.", kind="warning")
            return
        if conversation_id == self.conversation_id:
            if target_message_id is not None:
                self.set_view("chat")
                self.jump_to_message(target_message_id)
            elif target_approval_id is not None:
                self.set_view("chat")
                self.scroll_to_approval(target_approval_id)
            return
        self._scroll_target = target_message_id
        self._scroll_target_approval = target_approval_id
        self.session.load_chat(conversation_id)

    def jump_to_message(self, message_id: int | None) -> None:
        """Scroll to the card for one stored message and flash it; fall back to the end."""
        card = None
        if message_id is not None:
            card = self.ensure_card_for_message(int(message_id))
        if card is None:
            self.messages_view.scroll_to_end()
            return
        self.scroll_widget_into_view(card)
        self.flash_card(card)

    def ensure_card_for_message(self, message_id: int) -> "MessageCard | None":
        for card in self.cards:
            if card.message.message_id == message_id:
                return card
        return None

    def _card_for_turn(self, conversation_id: Any, message_id: Any) -> "MessageCard | None":
        """The reply card a store event belongs to: matched by conversation and
        message id; the newest finished reply only when the id is unknown."""
        if isinstance(conversation_id, int) and conversation_id != self.conversation_id:
            return None
        if isinstance(message_id, int):
            for card in reversed(self.cards):
                if card.message.role == "assistant" and card.message.message_id == message_id:
                    return card
        for card in reversed(self.cards):
            if card.message.role == "assistant" and not card.message.working:
                if isinstance(message_id, int) and card.message.message_id not in (None, message_id):
                    return None
                return card
        return None

    def flash_card(self, card: "MessageCard", duration_ms: int = 1400) -> None:
        theme = self.theme
        try:
            card.configure(highlightbackground=theme.accent, highlightthickness=2)
        except tk.TclError:
            return

        def restore() -> None:
            try:
                if card.winfo_exists():
                    card.configure(highlightthickness=0)
            except tk.TclError:
                pass

        self.root.after(duration_ms, restore)

    def rename_chat(self, chat: dict[str, Any]) -> None:
        """Rename a chat: inline in the title bar for the open chat, a sheet otherwise."""
        cid = int(chat["id"])
        if cid == self.conversation_id and self.view == "chat":
            self.begin_title_edit()
            return
        self._ask_sheet("Rename chat", "New title", chat.get("title", ""), lambda title, target=cid: self._apply_rename(target, title))

    def _apply_rename(self, conversation_id: int, title: str) -> None:
        clean = " ".join(safe_ui_text(title, 120).split())
        if not clean:
            return
        self.session.rename_chat(conversation_id, clean)
        if conversation_id == self.conversation_id:
            self.set_chat_title(clean)
        for chat in self.chats:
            if chat["id"] == conversation_id:
                chat["title"] = compact_activity(clean, 120)
        self.refresh_chat_list()

    def rename_current_chat(self) -> None:
        if self.conversation_id is None:
            return
        self.begin_title_edit()

    # -- inline title editing (double-click / F2; Enter commits, Esc cancels) --

    def begin_title_edit(self) -> None:
        if self.conversation_id is None or self.view != "chat":
            return
        if self.title_editor is not None and self.title_editor.winfo_exists():
            self.title_editor.focus_set()
            return
        theme = self.theme
        editor = tk.Entry(
            self.titles_frame, bg=theme.surface, fg=theme.text_strong, insertbackground=theme.accent, bd=0,
            highlightthickness=1, highlightbackground=theme.accent, highlightcolor=theme.accent, font=self.fonts.title, width=44,
        )
        editor.insert(0, self.chat_title if self.chat_title != DEFAULT_CHAT_TITLE else "")
        self.title_label.pack_forget()
        editor.pack(anchor="w", before=self.subrow, ipady=2)
        editor._committed = False  # type: ignore[attr-defined]
        editor.bind("<Return>", lambda _e: (self.commit_title_edit(), "break")[1])
        editor.bind("<Escape>", lambda _e: (self.cancel_title_edit(), "break")[1])
        editor.bind("<FocusOut>", lambda _e: self.commit_title_edit())
        editor.focus_set()
        editor.selection_range(0, "end")
        self.title_editor = editor

    def _end_title_edit(self) -> str | None:
        editor = self.title_editor
        if editor is None:
            return None
        self.title_editor = None
        try:
            value = editor.get() if editor.winfo_exists() else None
            if getattr(editor, "_committed", False):
                value = None
            editor._committed = True  # type: ignore[attr-defined]
            editor.destroy()
        except tk.TclError:
            value = None
        try:
            # ``before=`` must name a sibling in ``titles``: the subtitle row, not the
            # label inside it (Tk would silently re-pack the title into that row).
            if self.view == "chat" and not self.title_label.winfo_ismapped():
                self.title_label.pack(anchor="w", before=self.subrow)
        except tk.TclError:
            pass
        return value

    def commit_title_edit(self) -> None:
        value = self._end_title_edit()
        if value is None:
            return
        clean = " ".join(safe_ui_text(value, 120).split())
        if clean and clean != self.chat_title and self.conversation_id is not None:
            self._apply_rename(self.conversation_id, clean)
        self.focus_composer()

    def cancel_title_edit(self) -> None:
        self._end_title_edit()
        self.focus_composer()

    def _f2(self, event: Any) -> str | None:
        widget = getattr(event, "widget", None)
        try:
            if widget is not None and widget.winfo_toplevel() is not self.root:
                return None
        except (tk.TclError, AttributeError):
            return None
        if isinstance(widget, tk.Entry) and widget is not getattr(self, "search_entry", None):
            return None
        if widget is getattr(getattr(self, "chat_list", None), "canvas", None):
            return self._sidebar_key("rename")
        if self.view != "chat":
            return None
        self.begin_title_edit()
        return "break"

    # -- delete with undo ---------------------------------------------------

    UNDO_DELETE_SECONDS = 5.0

    def delete_chat(self, chat: dict[str, Any]) -> None:
        """Hide the row now; the store deletes after a 5 s Undo window."""
        if self.busy:
            self.toast("Stop or finish the current request first.", kind="warning")
            return
        cid = int(chat["id"])
        if cid in self._pending_deletes:
            return
        self._hidden_chats.add(cid)
        if cid == self.conversation_id:
            self.session.new_chat(self.project_id)
        self.refresh_chat_list()
        handle = self.root.after(int(self.UNDO_DELETE_SECONDS * 1000), lambda target=cid: self._commit_delete(target))
        self._pending_deletes[cid] = handle
        self.add_notice(
            f"Chat deleted · {compact_activity(chat.get('title') or 'Untitled', 60)}",
            kind="info", action=("Undo", lambda target=cid: self.undo_delete(target)), ttl=self.UNDO_DELETE_SECONDS, tag=f"delete:{cid}",
        )

    def undo_delete(self, conversation_id: int) -> None:
        handle = self._pending_deletes.pop(int(conversation_id), None)
        if handle is not None:
            try:
                self.root.after_cancel(handle)
            except tk.TclError:
                pass
        self._hidden_chats.discard(int(conversation_id))
        self.dismiss_notice_tag(f"delete:{conversation_id}")
        self.refresh_chat_list()
        self.toast("Chat restored.", kind="success")

    def _commit_delete(self, conversation_id: int) -> None:
        handle = self._pending_deletes.pop(int(conversation_id), None)
        if handle is not None:
            try:
                self.root.after_cancel(handle)
            except tk.TclError:
                pass
        self.dismiss_notice_tag(f"delete:{conversation_id}")
        self.session.delete_chat(int(conversation_id))

    def flush_pending_deletes(self) -> None:
        for cid in list(self._pending_deletes):
            self._commit_delete(cid)

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
        self._refresh_model_detail()
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
        draft_text = ""
        draft_files: list[dict[str, str]] = []
        try:
            draft_text = self.composer.input.value()
            draft_files = list(self.composer.attachments)
        except (AttributeError, tk.TclError):
            pass
        self.root.configure(bg=theme.bg)
        self._configure_style()
        self._build()
        try:
            if draft_text:
                self.composer.input.set_value(draft_text)
            if draft_files:
                self.composer.attachments = draft_files
                self.composer.render_chips()
        except tk.TclError:
            pass
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
        self.settings.set("sidebar", not self.sidebar_visible)
        self._sidebar_auto_hidden = False
        self._show_sidebar(not self.sidebar_visible)
        self._apply_layout_budget()

    # -- views ------------------------------------------------------------

    def set_view(self, key: str) -> None:
        if key not in VIEW_KEYS or key == self.view:
            return
        if key in {"memory", "routines"} and self._pane_view(key) is None:
            return
        self.view = key
        self.settings.set("view", key)
        self._show_view()
        if key == "council":
            self._ensure_council()
        elif key == "memory" and self.memory_view is not None:
            self.memory_view.refresh()
        elif key == "routines" and self.routines_view is not None:
            self.routines_view.refresh()

    def toggle_view(self) -> None:
        self.set_view("council" if self.view == "chat" else "chat")

    def _pane_view(self, key: str) -> Any:
        """Lazily build the Memory / Routines views from jarvis.ui_panes."""
        attribute = "memory_view" if key == "memory" else "routines_view"
        existing = getattr(self, attribute, None)
        if existing is not None:
            try:
                if existing.winfo_exists():
                    return existing
            except tk.TclError:
                pass
        try:
            from . import ui_panes
            cls = ui_panes.MemoryView if key == "memory" else ui_panes.RoutinesView
            view = cls(self.main, self)
        except Exception as exc:
            self.add_notice(f"The {key} view could not open ({type(exc).__name__}: {safe_ui_text(exc, 160)}).", kind="error")
            return None
        setattr(self, attribute, view)
        return view

    def _show_view(self) -> None:
        if getattr(self, "title_editor", None) is not None:
            self._end_title_edit()
        for name, button in self.nav_buttons.items():
            button.set_kind("active" if name == self.view else "subtle")
        chat = getattr(self, "chat_area", None)
        council_view = self.council_view
        if chat is None or council_view is None:
            return
        panes = [council_view] + [view for view in (self.memory_view, self.routines_view) if view is not None]
        for pane in panes:
            try:
                pane.pack_forget()
            except tk.TclError:
                pass
        council_view.table.stop()
        if self.view == "council":
            chat.pack_forget()
            council_view.pack(fill="both", expand=True)
            council_view.refresh_status()
            self.root.after(50, council_view.table.render)
        elif self.view in {"memory", "routines"}:
            chat.pack_forget()
            view = self._pane_view(self.view)
            if view is not None:
                view.pack(fill="both", expand=True)
            else:
                self.view = "chat"
                chat.pack(fill="both", expand=True)
        else:
            chat.pack(fill="both", expand=True)
        try:
            self.title_label.configure(text=VIEW_TITLES.get(self.view) or self.chat_title)
        except tk.TclError:
            pass
        self.refresh_context_pill()
        self.refresh_status()

    # -- council ----------------------------------------------------------

    def _ensure_council(self) -> None:
        if self.council is None:
            self.council = CouncilSession(self.config, self.night_plan)
            self.council.start()

    def council_convene(self, topic: str, depth: str) -> None:
        self._ensure_council()
        if self.council is not None and self.council.convene(topic, depth):
            self.council_turns = []
            self.toast("The council is convening…")
        else:
            self.toast("A Council meeting is already active.", kind="warning")

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
        self._approvals_requested = True
        self.session.request_approvals()

    def show_shortcuts(self) -> None:
        ShortcutsWindow(self)

    def open_presence(self) -> None:
        port = int(getattr(self.config, "presence_port", 8787) or 8787)

        def opened(result: Any, error: str | None) -> None:
            if error or not result:
                self.toast(f"Presence is not running on port {port}. Start it with start_jarvis_presence.bat, then try again.", kind="warning")
                return
            webbrowser.open(f"http://127.0.0.1:{port}/")

        self.run_job("presence_probe", presence_is_listening, port, on_done=opened)

    def export_chat(self, fmt: str | None = None) -> None:
        """Export the chat as Markdown or HTML — the save dialog's type filter
        (or the extension typed) picks the format when none is given."""
        exportable = [message for message in self.messages if not message.working]
        if not exportable:
            self.toast("Nothing to export yet.", kind="warning")
            return
        safe_title = re.sub(r"[^A-Za-z0-9 _-]+", "", self.chat_title).strip() or "jarvis-chat"
        wanted = (fmt or "").lower().strip(". ")
        types = [("Markdown", "*.md"), ("HTML page", "*.html"), ("Text", "*.txt")]
        if wanted == "html":
            types = [types[1], types[0], types[2]]
        chosen = tk.StringVar(value=types[0][0])
        try:
            path = filedialog.asksaveasfilename(
                parent=self.root, title="Export chat", defaultextension=".html" if wanted == "html" else ".md",
                initialfile=f"{safe_title}.{'html' if wanted == 'html' else 'md'}", filetypes=types, typevariable=chosen,
            )
        except tk.TclError:
            path = ""
        if not path:
            return
        suffix = Path(path).suffix.lower()
        as_html = suffix in {".html", ".htm"} or (suffix == "" and chosen.get() == "HTML page") or (wanted == "html" and suffix not in {".md", ".txt"})
        if as_html and suffix not in {".html", ".htm"}:
            path = str(Path(path).with_suffix(".html"))
        text = render_html_export(self.chat_title, exportable) if as_html else render_markdown_export(self.chat_title, exportable)
        self._write_export(path, text)

    def _write_export(self, path: str, text: str, *, verb: str = "Exported") -> None:
        """Write an export on the I/O pool and report the outcome as a toast."""
        name = Path(path).name

        def done(_result: Any, error: str | None) -> None:
            if error:
                self.toast(f"{verb.rstrip('d')} failed: {error}" if verb == "Exported" else f"Save failed: {error}", kind="error")
            else:
                self.toast(f"{verb} {'to ' if verb == 'Exported' else ''}{name}", kind="success")

        self.run_job("export", write_text_file, str(path), text, on_done=done)

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
            {"group": "Actions", "icon": "⇪", "label": "Export chat as HTML", "detail": "", "run": lambda: self.export_chat("html"), "keywords": "save download html page web"},
            {"group": "Actions", "icon": "⌨", "label": "Keyboard shortcuts", "detail": "Ctrl+/", "run": self.show_shortcuts, "keywords": "help keys"},
            {"group": "Actions", "icon": "◎", "label": "Open the Council", "detail": "Ctrl+M", "run": lambda: self.set_view("council"), "keywords": "council meeting round table specialists agenda minutes"},
            {"group": "Actions", "icon": "▣", "label": "Back to chat", "detail": "Ctrl+M", "run": lambda: self.set_view("chat"), "keywords": "conversation chat"},
            {"group": "Actions", "icon": "◎", "label": "Open Presence in the browser", "detail": "", "run": self.open_presence, "keywords": "web browser presence"},
            {"group": "Actions", "icon": "☰", "label": "Toggle sidebar", "detail": "Ctrl+B", "run": self.toggle_sidebar, "keywords": "hide show"},
            {"group": "Actions", "icon": "⚙", "label": "Model provider settings", "detail": self.provider_choice, "run": self.show_provider_settings, "keywords": "ollama claude chatgpt codex provider settings"},
            {"group": "Actions", "icon": "⟳", "label": "Reconnect model provider", "detail": "offline" if self.provider_error else "connected", "run": self.retry_provider, "keywords": "ollama retry provider offline"},
            {"group": "Actions", "icon": "◨", "label": "Toggle context panel", "detail": "Ctrl+I", "run": self.toggle_context, "keywords": "files project approvals tasks memory"},
            {"group": "Actions", "icon": "▥", "label": "Side pane: Diff · Artifact · File", "detail": "Ctrl+Shift+P", "run": self.toggle_side_pane, "keywords": "diff changes artifact file view pane"},
            {"group": "Actions", "icon": "⌕", "label": "Find in this chat", "detail": "Ctrl+F", "run": self.toggle_find, "keywords": "search highlight"},
            {"group": "Actions", "icon": "≡", "label": f"Timeline detail: {self.timeline_mode} → next", "detail": "Ctrl+O", "run": self.cycle_timeline_mode, "keywords": "tool calls verbose summary steps"},
            {"group": "Actions", "icon": "⚙", "label": "Settings", "detail": "Ctrl+,", "run": self.open_settings, "keywords": "preferences options hotkey"},
            {"group": "Actions", "icon": "✓", "label": "Standing approvals", "detail": f"{len(self.grants)} active", "run": self.open_settings, "keywords": "grants always allow revoke permissions"},
            {"group": "Actions", "icon": "🔔", "label": "Inbox", "detail": "Ctrl+U", "run": self.open_inbox, "keywords": "notifications needs you unread tasks"},
            {"group": "Actions", "icon": "◍", "label": "Memory: governed facts", "detail": "Ctrl+Shift+M", "run": lambda: self.set_view("memory"), "keywords": "facts remember claims erase"},
            {"group": "Actions", "icon": "◷", "label": "Routines: scheduled runs", "detail": "Ctrl+Shift+R", "run": lambda: self.set_view("routines"), "keywords": "schedule recurring jobs worker"},
            {"group": "Actions", "icon": "⤢", "label": "Quick-ask window", "detail": self.hotkey_label(), "run": self.open_companion, "keywords": "companion floating small"},
            {"group": "Actions", "icon": "▲", "label": "Keep on top: " + ("on → off" if self.settings.get("topmost") else "off → on"), "detail": "", "run": self.toggle_topmost, "keywords": "always on top float"},
            {"group": "Actions", "icon": "▰", "label": "New project", "detail": "Ctrl+Shift+N", "run": self.new_project, "keywords": "workspace folder"},
            {"group": "Actions", "icon": "◷", "label": "Queue a background task", "detail": "", "run": self.queue_task_dialog, "keywords": "worker later async"},
            {"group": "Actions", "icon": "◍", "label": "Remember a project fact", "detail": "", "run": self.remember_fact_dialog, "keywords": "memory store governed"},
            {"group": "Actions", "icon": "▮", "label": "Open workspace folder", "detail": "", "run": lambda: self.open_path(self.context_data.get("workspace") or self.workspace), "keywords": "explorer files"},
        ]
        for project in self.projects:
            items.append({"group": "Projects", "icon": "▰", "label": f"Switch to {project['name']}", "detail": f"{project['conversation_count']} chats", "run": lambda target=project["id"]: self.select_project(target), "keywords": "project workspace"})
        for label in MODEL_CHOICES:
            items.append({"group": "Model", "icon": "◇", "label": f"Use {label} model", "detail": MODEL_HINTS[label], "run": lambda choice=label: self.set_model(choice), "keywords": "model profile switch"})
        for key in THEME_ORDER:
            items.append({"group": "Theme", "icon": "◐", "label": f"{THEMES[key].name} theme", "detail": "dark" if THEMES[key].dark else "light", "run": lambda target=key: self.set_theme(target), "keywords": "appearance look colors"})
        for chat in [row for row in self.chats if row.get("message_count", 0) > 0 and (self.chat_list_filter != "active" or not self.chat_meta.flag(row["id"], "archived"))][:60]:
            flags = self.chat_meta.get(chat["id"])
            detail = chat_group_label(chat.get("created_at", "")) + (" · pinned" if flags.get("pinned") else "") + (" · archived" if flags.get("archived") else "")
            items.append({"group": "Chats", "icon": "📌" if flags.get("pinned") else "◌", "label": chat["title"], "detail": detail, "run": lambda target=chat["id"]: self.open_chat(target), "keywords": "conversation"})
        if self.conversation_id:
            items.append({"group": "Actions", "icon": "📌", "label": "Unpin this chat" if self.chat_meta.flag(self.conversation_id, "pinned") else "Pin this chat", "detail": "Ctrl+Alt+P", "run": self.toggle_pin, "keywords": "pin favourite top"})
            items.append({"group": "Actions", "icon": "▤", "label": "Unarchive this chat" if self.chat_meta.flag(self.conversation_id, "archived") else "Archive this chat", "detail": "", "run": self.toggle_archive, "keywords": "archive hide"})
            items.append({"group": "Actions", "icon": "⑂", "label": "Branch this chat from the last message", "detail": "", "run": lambda: self.branch_from(self.cards[-1]) if self.cards else None, "keywords": "branch fork copy"})
        items.append({"group": "Actions", "icon": "☷", "label": f"Chat list filter: {CHAT_FILTER_LABELS[self.chat_list_filter]} → next", "detail": "", "run": self.cycle_chat_filter, "keywords": "archived active all filter"})
        self.palette = CommandPalette(self, items, on_query=self._palette_query)

    def _palette_query(self, query: str) -> None:
        if self._search_after is not None:
            try:
                self.root.after_cancel(self._search_after)
            except tk.TclError:
                pass
            self._search_after = None
        if len(query.strip()) < 3 or not self.ready:
            return
        self._search_after = self.root.after(280, lambda: self.session.search_messages(query))

    def _ask_sheet(self, title: str, label: str, initial: str, on_accept: Callable[[str], None], *, accept_label: str = "Save") -> "tk.Toplevel":
        """A palette-style prompt: a small window with one entry, no grab, no
        wait_window. Enter (or the accept button) calls ``on_accept`` with the
        text; Esc, Cancel or focus loss simply closes it."""
        theme = self.theme
        previous = getattr(self, "sheet", None)
        if previous is not None:
            try:
                if previous.winfo_exists():
                    previous.destroy()
            except tk.TclError:
                pass
        sheet = tk.Toplevel(self.root)
        self.sheet = sheet
        sheet.title(title)
        sheet.configure(bg=theme.panel)
        sheet.transient(self.root)
        sheet.resizable(False, False)
        try:
            sheet.attributes("-toolwindow", True)
        except tk.TclError:
            pass
        _apply_titlebar_theme(sheet, theme.dark)
        shell = tk.Frame(sheet, bg=theme.panel, padx=self.px(18), pady=self.px(14))
        shell.pack(fill="both", expand=True)
        tk.Label(shell, text=label, bg=theme.panel, fg=theme.muted, font=self.fonts.small, wraplength=self.px(440), justify="left").pack(anchor="w", pady=(0, 6))
        variable = tk.StringVar(value=str(initial or ""))
        entry = tk.Entry(shell, textvariable=variable, bg=theme.surface, fg=theme.text, insertbackground=theme.accent, bd=0, highlightthickness=1, highlightbackground=theme.border_strong, highlightcolor=theme.accent, font=self.fonts.body, width=48)
        entry.pack(fill="x", ipady=7, pady=(0, 10))
        sheet.entry = entry  # type: ignore[attr-defined]
        done = {"closed": False}

        def close(_event: Any = None) -> str:
            if not done["closed"]:
                done["closed"] = True
                if getattr(self, "sheet", None) is sheet:
                    self.sheet = None
                try:
                    sheet.destroy()
                except tk.TclError:
                    pass
                self.focus_composer()
            return "break"

        def accept(_event: Any = None) -> str:
            value = variable.get().strip()
            if not value:
                self.toast("Type something first, or press Esc to close.", kind="warning")
                return "break"
            close()
            on_accept(value)
            return "break"

        def focus_lost(_event: Any = None) -> None:
            sheet.after(150, lambda: close() if sheet.winfo_exists() and not str(sheet.focus_get() or "").startswith(str(sheet)) else None)

        actions = tk.Frame(shell, bg=theme.panel)
        actions.pack(fill="x")
        tk.Label(actions, text="Enter to confirm · Esc to close", bg=theme.panel, fg=theme.faint, font=self.fonts.tiny).pack(side="left")
        RoundButton(actions, self, accept_label, accept, kind="accent", padx=14, pady=5, font=self.fonts.small_bold).pack(side="right")
        RoundButton(actions, self, "Cancel", close, kind="ghost", padx=12, pady=5, font=self.fonts.small_bold).pack(side="right", padx=(0, 8))
        entry.bind("<Return>", accept)
        entry.bind("<Escape>", close)
        sheet.bind("<Escape>", close)
        sheet.bind("<FocusOut>", focus_lost)
        sheet.accept = accept  # type: ignore[attr-defined]
        sheet.close = close  # type: ignore[attr-defined]
        sheet.update_idletasks()
        x = self.root.winfo_rootx() + (self.root.winfo_width() - sheet.winfo_reqwidth()) // 2
        y = self.root.winfo_rooty() + self.px(120)
        sheet.geometry(f"+{x}+{y}")
        entry.focus_set()
        sheet.after(10, entry.focus_force)
        entry.selection_range(0, "end")
        return sheet

    def toast_position(self) -> tuple[int, int]:
        """Bottom-centre of the chat column, just above the composer, so a toast
        never sits on the Send button; the window's bottom-centre otherwise."""
        try:
            self.root.update_idletasks()
            column = self.chat_column if self.view == "chat" else self.main
            x = column.winfo_rootx() - self.root.winfo_rootx() + column.winfo_width() // 2
            strip = getattr(self, "notice_frame", None)
            if self.view == "chat" and strip is not None and strip.winfo_ismapped() and self.notices:
                y = strip.winfo_rooty() - self.root.winfo_rooty() - self.px(10)
            elif self.view == "chat" and self.composer_area.winfo_ismapped():
                y = self.composer_area.winfo_rooty() - self.root.winfo_rooty() - self.px(10)
            else:
                y = self.root.winfo_height() - self.px(24)
            return max(self.px(120), x), max(self.px(60), y)
        except (AttributeError, tk.TclError):
            return self.px(400), self.px(500)

    def toast(self, text: str, *, kind: str = "info") -> None:
        theme = self.theme
        colors = {"info": theme.accent, "success": theme.success, "warning": theme.warning, "error": theme.danger}
        try:
            self.toast_label.configure(text=text, highlightbackground=colors.get(kind, theme.accent))
            x, y = self.toast_position()
            self.toast_label.place(x=x, y=y, anchor="s")
            self.toast_label.lift()
        except tk.TclError:
            return
        if self._toast_after is not None:
            try:
                self.root.after_cancel(self._toast_after)
            except tk.TclError:
                pass
        self._toast_after = self.root.after(2600, self.toast_label.place_forget)

    def _escape(self, event: Any) -> None:
        if self.palette is not None:
            self.palette.close()
            return
        widget = getattr(event, "widget", None)
        try:
            toplevel = widget.winfo_toplevel() if widget is not None else self.root
        except (tk.TclError, AttributeError):
            toplevel = self.root
        if toplevel is not self.root:
            return  # dialogs and secondary windows own their Escape
        if self.find_visible and widget is getattr(getattr(self, "find_bar", None), "entry", None):
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

    def restart_session(self) -> None:
        """After a fatal startup error: start a fresh worker thread on the same queues."""
        old = self.session
        try:
            if old.is_alive():
                old.shutdown()
        except Exception:
            pass
        self.dismiss_notice_tag("fatal")
        self.status_text = "Connecting…"
        self.activity_text = "Starting model services"
        self.refresh_status()
        fresh = JarvisSession(self.config)
        fresh.events = old.events  # keep the pump and the I/O pool on the same queue
        self.session = fresh
        fresh.start()

    def _set_busy(self, busy: bool) -> None:
        self.busy = busy
        self.stopping = False
        self.composer.set_busy(busy)
        for button in self.model_buttons.values():
            button.set_enabled(not busy)
        self.refresh_status()

    def _finish_active(self, payload: dict[str, Any], *, error: bool = False) -> None:
        card = self.active_card
        if card is None:
            message = Message("assistant", "", working=True)
            card = self.append_message(message)
        message = card.message
        content = safe_ui_text(payload.get("content") if not error else payload.get("message", ""))
        status = "error" if error else str(payload.get("status", "complete"))
        if status == "cancelled" and not content.strip():
            content = message.stream_text.strip()
        message.content = content
        message.status = status
        message.error = error
        message.reason = safe_ui_text(payload.get("reason") or "", 300)
        message.model = payload.get("model")
        message.elapsed = payload.get("elapsed")
        message.approval_id = payload.get("approval_id")
        message.tool_calls = int(payload.get("tool_calls") or 0)
        message.created_at = time.time()
        metrics = payload.get("metrics")
        message.metrics = dict(metrics) if isinstance(metrics, dict) else {}
        context_length = payload.get("context_length")
        if isinstance(context_length, int) and not isinstance(context_length, bool) and context_length > 0:
            message.metrics.setdefault("context_length", context_length)  # the pill's denominator (N-6)
        tools = payload.get("tools")
        if isinstance(tools, list) and tools:
            message.tools = [dict(entry) for entry in tools if isinstance(entry, dict)][-TOOL_LOG_LIMIT:]
        message.retryable = bool(payload.get("retryable"))
        diff = payload.get("diff")
        message.diff = diff if isinstance(diff, (dict, str)) else None
        message_id = payload.get("message_id")
        message.message_id = int(message_id) if isinstance(message_id, int) else None
        if message is self._regenerating_message:
            self._regenerating_message = None
            if content and content not in message.versions:
                message.versions.append(content)
            message.version_index = max(0, len(message.versions) - 1)
        card.render_final()
        self.active_card = None
        self.persist_timeline(message)
        if message.metrics:
            self._remember_metrics(message.metrics)
        self.messages_view.scroll_to_end()
        self.root.after(160, self._refit_texts)
        self.root.after(400, self.messages_view.scroll_to_end)
        if message.approval_id is not None:
            self.session.request_approvals()
        # Staggered after the card paints (N-13): the sidebar and context rebuilds
        # follow on their own idle ticks instead of tearing this frame.
        self.root.after_idle(self.refresh_context)
        try:
            focused = self.root.focus_displayof()
        except (KeyError, tk.TclError):
            focused = None
        if focused is None and bool(self.settings.get("flash", True)):
            flash_window(self.root)
        landed_in = payload.get("conversation_id")
        if isinstance(landed_in, int) and landed_in != self.conversation_id:
            self.mark_unread(landed_in)
        if message.approval_id is not None:
            self.notify_event("approval", "Jarvis needs your approval", compact_activity(message.content or f"Approval #{message.approval_id}", 160), conversation_id=landed_in if isinstance(landed_in, int) else self.conversation_id)
        elif not error and status == "complete":
            self.notify_event("reply", f"Reply ready · {compact_activity(self.chat_title, 40)}", compact_activity(message.content, 160), conversation_id=landed_in if isinstance(landed_in, int) else self.conversation_id)
        self.rebuild_inbox()
        self._drain_queue(status if not error else "error")

    def _finish_detached(self, payload: dict[str, Any], *, error: bool = False) -> None:
        """A reply that landed in a chat other than the open one (the companion's):
        no card here — the sidebar row goes unread, the inbox and notification fire."""
        cid = payload.get("conversation_id")
        status = "error" if error else str(payload.get("status", "complete"))
        content = safe_ui_text(payload.get("content") if not error else payload.get("message", ""))
        if isinstance(cid, int):
            self._last_turn_status[cid] = status
            self.mark_unread(cid)
        target = cid if isinstance(cid, int) else self.conversation_id
        approval_id = payload.get("approval_id")
        if approval_id is not None:
            self.session.request_approvals()
            self.notify_event("approval", "Jarvis needs your approval", compact_activity(content or f"Approval #{approval_id}", 160), conversation_id=target)
        elif not error and status == "complete":
            chat = self._sidebar_chat(cid)
            title = chat["title"] if chat else "Quick ask"
            self.notify_event("reply", f"Reply ready · {compact_activity(title, 40)}", compact_activity(content, 160), conversation_id=target)
        self.rebuild_inbox()
        self.root.after_idle(self.refresh_context)
        self._drain_queue(status)

    def _schedule_chat_list_refresh(self) -> None:
        """Coalesce sidebar rebuilds onto one idle tick (N-13)."""
        if self._chat_list_after is not None:
            return

        def run() -> None:
            self._chat_list_after = None
            try:
                self.refresh_chat_list()
            except tk.TclError:
                pass

        try:
            self._chat_list_after = self.root.after_idle(run)
        except tk.TclError:
            self._chat_list_after = None
            self.refresh_chat_list()

    def _handle_event(self, event: SessionEvent) -> None:
        kind = event.kind
        payload = event.payload
        if kind == "ui_job":
            self.jobs.dispatch(payload)
            return
        self._forward_view_event(kind, payload)
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
            self.projects = list(payload.get("projects") or [])
            self.project_id = int(payload.get("project_id") or 1)
            self.workspace = str(payload.get("workspace") or self.workspace)
            self.data_dir = str(payload.get("data_dir") or self.data_dir)
            self.chat_started_at = time.time()
            self._refresh_model_detail()
            self._refresh_project_detail()
            self.refresh_chat_list()
            self._set_busy(False)
            self.composer.set_ready(True)
            self._apply_provider_state(payload.get("provider_error"))
            if self.provider_error and not self.messages:
                notice = Message("assistant", f"{self.provider_error} Jarvis will retry when you send a message.", error=True, retry_provider=True)
                self.append_message(notice)
            self.refresh_context()
        elif kind == "provider_switched":
            self.model_names = {key: str(payload.get(f"{key}_model") or "")
                                for key in ("fast", "reasoning", "coding", "deep")}
            self._refresh_model_detail()
            self.composer.refresh_model_label()
            self._apply_provider_state(payload.get("error"))
            if not self.provider_error:
                self.activity_text = "Ready"
                self.refresh_status()
                self.toast("Model provider connected.", kind="success")
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
                    if len(steps) > MAX_MESSAGE_STEPS:
                        del steps[: len(steps) - MAX_MESSAGE_STEPS]
                    self.active_card.refresh_steps()
                    self.messages_view.scroll_to_end()
            self.refresh_status()
        elif kind == "delta":
            if self.active_card is not None and isinstance(payload, dict):
                self.active_card.append_delta(str(payload.get("text", "")))
                if self.messages_view.stick_to_bottom:
                    self.messages_view.scroll_to_end(light=True)
        elif kind == "step":
            self._apply_step(payload)
        elif kind == "assistant":
            landed = payload.get("conversation_id") if isinstance(payload, dict) else None
            if isinstance(landed, int) and self.conversation_id is not None and landed != self.conversation_id and self.active_card is None:
                self._finish_detached(payload)
            else:
                self._finish_active(payload)
        elif kind == "chat_created":
            # A detached chat (the companion's): listed, never switched to.
            created = int(payload.get("conversation_id") or 0) or None
            if created is not None:
                self._companion_conversation = created
                self.reset_chat_filter()
                pending = self._companion_pending or ""
                images = list(getattr(self, "_companion_pending_images", None) or [])
                self._companion_pending = None
                self._companion_pending_images = []
                if pending or images:
                    self.session.submit(pending or "Describe the attached image.", self.model_label, images, conversation_id=created)
        elif kind == "error":
            data = payload if isinstance(payload, dict) else {"message": str(payload)}
            if self.busy or self.active_card is not None:
                self._finish_active(data, error=True)
            else:
                self.add_notice(safe_ui_text(data.get("message", "Something went wrong."), 300), kind="error", details=str(data.get("message", "")))
        elif kind == "fatal":
            # No nested messagebox inside the event pump: the strip carries the
            # error, the composer stays disabled, Retry restarts the worker.
            self.ready = False
            self.status_text = "Startup failed"
            self.activity_text = safe_ui_text(payload, 300)
            self.composer.set_ready(False)
            self.refresh_status()
            self.add_notice(f"Jarvis could not start: {safe_ui_text(payload, 300)}", kind="error", details=safe_ui_text(payload, 2_000), action=("Retry", self.restart_session), tag="fatal")
        elif kind == "earlier_messages":
            if payload.get("conversation_id") == self.conversation_id:
                self._loading_earlier = False
                self._has_more = bool(payload.get("has_more"))
                older: list[Message] = []
                for row in payload.get("messages", []):
                    role = "user" if row.get("role") == "user" else "assistant"
                    message = Message(role, str(row.get("content", "")), created_at=float(row.get("created_at") or 0))
                    row_id = row.get("id")
                    message.message_id = int(row_id) if isinstance(row_id, int) else None
                    if role == "assistant" and isinstance(row.get("timeline"), dict):
                        message.restore_timeline(row.get("timeline"))
                    older.append(message)
                if older:
                    self.messages = older + self.messages
                    self._render_start = int(getattr(self, "_render_start", 0)) + len(older)
                    self.show_earlier()
                else:
                    self._build_earlier_control()
                self.refresh_status()
        elif kind == "new_chat":
            self._stash_draft()
            self._scroll_target = None
            self._has_more = False
            self.reset_chat_filter()
            self.conversation_id = int(payload.get("conversation_id") or 0) or None
            self.project_id = int(payload.get("project_id") or self.project_id or 1)
            self.messages = []
            self._last_user_prompt = None
            self.chat_started_at = time.time()
            self._remember_metrics(None)
            self.set_chat_title(DEFAULT_CHAT_TITLE)
            self.render_messages()
            self._refresh_project_detail()
            self.refresh_chat_list()
            self.refresh_status()
            self.refresh_context()
            self._restore_draft()
            self._apply_pending_edit()
            self.focus_composer()
            if self._companion_pending or getattr(self, "_companion_pending_images", None):
                pending = self._companion_pending or ""
                images = list(getattr(self, "_companion_pending_images", None) or [])
                self._companion_pending = None
                self._companion_pending_images = []
                self._companion_conversation = self.conversation_id
                self._submit_prompt(pending, images, [], [Path(path).name for path in images])
        elif kind == "chat_loaded":
            self._stash_draft()
            self.conversation_id = int(payload.get("conversation_id") or 0) or None
            self.project_id = int(payload.get("project_id") or self.project_id or 1)
            self.set_chat_title(str(payload.get("title") or DEFAULT_CHAT_TITLE))
            self.messages = []
            first_stamp = 0.0
            for row in payload.get("messages", []):
                role = "user" if row.get("role") == "user" else "assistant"
                stamp = float(row.get("created_at") or 0)
                if stamp and not first_stamp:
                    first_stamp = stamp
                message = Message(role, str(row.get("content", "")), created_at=stamp)
                row_id = row.get("id")
                message.message_id = int(row_id) if isinstance(row_id, int) else None
                if role == "assistant" and isinstance(row.get("timeline"), dict):
                    message.restore_timeline(row.get("timeline"))
                self.messages.append(message)
                if role == "user":
                    self._last_user_prompt = str(row.get("content", ""))
            self.chat_started_at = first_stamp or time.time()
            self._has_more = bool(payload.get("has_more"))
            self._loading_earlier = False
            self._remember_metrics(next((message.metrics for message in reversed(self.messages) if message.role == "assistant" and message.metrics), None))
            if self.conversation_id and self.chat_meta.flag(self.conversation_id, "unread"):
                self.chat_meta.set(self.conversation_id, unread=False)
            target = getattr(self, "_scroll_target", None)
            self._scroll_target = None
            target_approval = getattr(self, "_scroll_target_approval", None)
            self._scroll_target_approval = None
            if target is None and target_approval is not None:
                target = next((message.message_id for message in self.messages if message.approval_id == target_approval and message.message_id is not None), None)
            self.render_messages(target_message_id=target)
            self._refresh_project_detail()
            self.refresh_chat_list()
            self.refresh_status()
            self.refresh_context()
            self._restore_draft()
            self._apply_pending_edit()
            self.focus_composer()
            if target is not None:
                self.set_view("chat")
                self.root.after(200, lambda: self.jump_to_message(target))
            elif target_approval is not None:
                self.set_view("chat")
                self.root.after(200, lambda: self.scroll_to_approval(target_approval))
        elif kind == "projects":
            self.projects = list(payload or [])
            self._refresh_project_detail()
        elif kind == "project_created":
            self.toast(f"Project {payload.get('name')} created with its own workspace.", kind="success")
            self.select_project(int(payload.get("id") or 1))
        elif kind == "context":
            self.context_data = dict(payload or {})
            self.worker_alive = self.context_data.get("worker_alive")
            open_tasks = [row for row in (self.context_data.get("tasks") or []) if str(row.get("status", "")).lower() in {"queued", "running", "leased", "retry", "pending"}]
            if self.worker_alive is False and open_tasks and not self._worker_notice_shown:
                self._worker_notice_shown = True
                self.add_notice("Worker offline — queued background tasks will not run until `python -m jarvis worker` (or the worker service) is running.", kind="warning")
            state = str(self.context_data.get("control_state") or "")
            if state and state != "unknown":
                self.control_state = state
            pending = self.context_data.get("pending_approvals")
            if isinstance(pending, list):
                self.pending_approvals = len(pending)
            self._track_tasks()
            self.rebuild_inbox()
            self.refresh_status()
            if self.context_visible:
                self.context_panel.set_data(self.context_data)
        elif kind == "search_results":
            if self.palette is not None:
                query = str((payload or {}).get("query", ""))
                items = [
                    {
                        "group": "Messages", "icon": "❝",
                        "label": compact_activity(row.get("snippet", ""), 90),
                        "runs": highlight_runs(row.get("snippet", ""), query),
                        "detail": compact_activity(row.get("title", ""), 40),
                        "run": lambda target=row.get("conversation_id"), mid=row.get("message_id"): self.open_chat(int(target or 0), target_message_id=mid if isinstance(mid, int) else None),
                    }
                    for row in (payload or {}).get("results", [])
                    if self.chat_list_filter != "active" or not self.chat_meta.flag(row.get("conversation_id"), "archived")
                ]
                self.palette.set_extra(query, items)
        elif kind == "task_queued":
            self.toast(f"Task #{payload.get('task_id')} queued for the Jarvis worker.", kind="success")
            self.refresh_context()
        elif kind == "approval_detail":
            approval_id = int(payload.get("approval_id") or 0)
            row = payload.get("approval") or {"missing": True}
            status = str(row.get("status") or "") if isinstance(row, dict) else ""
            for card in self.cards:
                if card.message.approval_id == approval_id:
                    card.message.approval = row
                    if status and status != "pending" and not card.message.approval_decision:
                        # A reloaded card learns its outcome from the store row itself.
                        card.message.approval_decision = decision_from_row(row)
                    card.refresh_approval()
        elif kind in {"memory_receipt", "fact_proposal"}:
            data = dict(payload or {})
            card = self._card_for_turn(data.get("conversation_id"), data.get("message_id"))
            if card is not None:
                if kind == "memory_receipt":
                    card.message.receipt = {"action": data.get("action"), "fact": data.get("fact"), "previous": data.get("previous"), "claim_id": data.get("claim_id"), "event_kind": data.get("event_kind")}
                else:
                    card.message.proposal = {"fact": data.get("fact") or {}, "proposal_id": data.get("proposal_id"), "assisted": bool(data.get("assisted"))}
                card.render_final()
                self.persist_timeline(card.message)
                self.root.after(120, self._refit_texts)
            self.refresh_context()
        elif kind == "chats":
            self.chats = list(payload or [])
            self._schedule_chat_list_refresh()
        elif kind == "chat_ids":
            # Every conversation the store holds — flags are pruned against this,
            # never against the bounded sidebar list.
            live = {int(value) for value in (payload or []) if isinstance(value, int)}
            if live:
                self.chat_meta.prune(live)
                self._hidden_chats = {cid for cid in self._hidden_chats if cid in live}
                self.refresh_chat_list()
        elif kind == "chat_renamed":
            if payload.get("conversation_id") == self.conversation_id:
                self.set_chat_title(str(payload.get("title") or self.chat_title))
        elif kind == "chat_deleted":
            cid = int(payload.get("conversation_id") or 0)
            if payload.get("deleted"):
                self.chat_meta.remove(cid)
            else:
                self._hidden_chats.discard(cid)
                self.refresh_chat_list()
                self.add_notice("That chat could not be deleted.", kind="error")
        elif kind == "chat_branched":
            self.chat_meta.set(int(payload.get("conversation_id") or 0), branched_from=int(payload.get("source") or 0) or None)
            self.reset_chat_filter()
            self.toast(f"Branched — {payload.get('copied', 0)} messages copied; the original chat is untouched.", kind="success")
        elif kind == "approvals":
            rows = list(payload or [])
            self.pending_approvals = sum(1 for row in rows if row.get("status") == "pending")
            self.refresh_status()
            if self._approvals_requested:
                self._approvals_requested = False
                if self.approval_window is None or not self.approval_window.winfo_exists():
                    self.approval_window = ApprovalWindow(self, rows)
                else:
                    self.approval_window.update_rows(rows)
                    self.approval_window.lift()
            elif self.approval_window is not None and self.approval_window.winfo_exists():
                self.approval_window.update_rows(rows)
            if self.context_visible:
                self.refresh_context()
        elif kind == "approval_decided":
            approval_id = int(payload.get("approval_id") or 0)
            approved = bool(payload.get("approved"))
            scope = str(payload.get("scope") or "once")
            reason = self._deny_reasons.pop(approval_id, "")
            if payload.get("changed"):
                until = _iso_to_epoch(str(payload.get("until") or "")) or None
                grant_id = payload.get("grant_id") if isinstance(payload.get("grant_id"), int) else None
                send_reason = bool(reason) and not approved and not self.busy
                label = self._decision_label(approved, scope, until=until, reason_sent=send_reason)
                self.toast(f"Approval #{approval_id}: {label}.", kind="success")
                for card in self.cards:
                    if card.message.approval_id == approval_id:
                        card.message.approval_decision = label
                        card.message.approval_resumable = approved and not self.busy
                        card.message.approval_grant_id = grant_id
                        card.message.approval_reason_sent = send_reason
                        card.refresh_approval()
                        self.persist_timeline(card.message)
                if send_reason:
                    action, resource = "", ""
                    for card in self.cards:
                        if card.message.approval_id == approval_id and isinstance(card.message.approval, dict):
                            action = str(card.message.approval.get("action") or "")
                            resource = str(card.message.approval.get("resource") or "")
                            break
                    self.send_text(deny_instruction_text(approval_id, action, resource, reason))
            else:
                row = payload.get("row") if isinstance(payload.get("row"), dict) else None
                read_back = decision_from_row(row) if row else None
                note = str(payload.get("note") or "") or (f"Approval #{approval_id}: {read_back}." if read_back else "Approval was already decided or expired.")
                self.toast(note, kind="warning")
                for card in self.cards:
                    if card.message.approval_id == approval_id:
                        card.message.approval_decision = None if payload.get("note") and not read_back else (read_back or "Already decided")
                        if row:
                            card.message.approval = row
                        card.refresh_approval()
        elif kind == "send_dropped":
            data = payload if isinstance(payload, dict) else {}
            prompt = str(data.get("prompt") or "")
            self._set_busy(False)
            card = self.active_card
            if card is not None and card.message.working:
                self.messages = [message for message in self.messages if message is not card.message]
                self.cards = [item for item in self.cards if item is not card]
                try:
                    card.destroy()
                except tk.TclError:
                    pass
                self.active_card = None
            self.add_notice("That message was typed for a chat that is no longer open — it was not sent. It is back in the composer.", kind="warning")
            if prompt:
                self.composer.input.set_value(prompt)
        elif kind == "file_index":
            data = payload if isinstance(payload, dict) else {}
            index = data.get("index")
            self.file_index = index if index is not None else self.file_index
            if data.get("error"):
                self.file_index = None
        elif kind == "grants":
            self.grants = list(payload or [])
            window = self.settings_window
            if window is not None and window.winfo_exists():
                window.render_grants(self.grants)
        elif kind == "grant_revoked":
            self.toast("Standing approval revoked." if payload.get("revoked") else "That grant was already revoked.", kind="success" if payload.get("revoked") else "warning")

    def _on_global_hotkey(self) -> None:
        """First press opens the quick-ask box; a second press raises the main window."""
        companion = self.companion
        try:
            visible = companion is not None and companion.winfo_exists() and companion.state() == "normal"
        except tk.TclError:
            visible = False
        if visible or not bool(self.settings.get("companion", True)):
            bring_window_forward(self.root)
            self.set_view("chat")
            self.root.after(150, self.focus_composer)
            if visible:
                try:
                    companion.hide()
                except tk.TclError:
                    pass
            return
        self.open_companion()

    def _drain_session_events(self, budget: int = 400) -> None:
        """Handle queued session events, merging consecutive deltas so a fast
        provider costs one layout pass per tick instead of one per token."""
        events = self.session.events
        pending_delta: dict[str, Any] | None = None
        handled = 0
        while handled < budget:
            try:
                event = events.get_nowait()
            except queue.Empty:
                break
            handled += 1
            if event.kind == "delta" and isinstance(event.payload, dict):
                conversation = event.payload.get("conversation_id")
                if pending_delta is not None and pending_delta.get("conversation_id") == conversation:
                    pending_delta["text"] += str(event.payload.get("text", ""))
                    continue
                if pending_delta is not None:
                    self._safe_handle(SessionEvent("delta", pending_delta))
                pending_delta = {"conversation_id": conversation, "text": str(event.payload.get("text", ""))}
                continue
            if pending_delta is not None:
                self._safe_handle(SessionEvent("delta", pending_delta))
                pending_delta = None
            self._safe_handle(event)
        if pending_delta is not None:
            self._safe_handle(SessionEvent("delta", pending_delta))

    def _safe_handle(self, event: SessionEvent) -> None:
        """One bad payload must never kill the event pump for the whole session."""
        try:
            self._handle_event(event)
        except tk.TclError:
            pass
        except Exception as exc:
            self._report_ui_error(f"{event.kind} event", exc)

    def _report_ui_error(self, where: str, exc: BaseException) -> None:
        detail = f"{type(exc).__name__}: {safe_ui_text(exc, 300)}"
        try:
            print(f"[jarvis-desktop] {where} failed: {detail}", file=sys.stderr)
        except Exception:
            pass
        try:
            self.toast(f"A display update failed ({where}). {detail}", kind="error")
        except Exception:
            pass

    def _poll_events(self) -> None:
        try:
            self._drain_session_events()
            try:
                while True:
                    hit = self.hotkey_hits.get_nowait()
                    if hit == "summon":
                        self._on_global_hotkey()
                    elif hit == "failed":
                        self.add_notice(f"{self.hotkey_label()} is taken by another app, so the global hotkey is off this session.", kind="warning")
            except queue.Empty:
                pass
            self._drain_tray_actions()
            if self.council is not None:
                while True:
                    try:
                        council_event = self.council.events.get_nowait()
                    except queue.Empty:
                        break
                    try:
                        self._handle_council_event(council_event)
                    except tk.TclError:
                        pass
                    except Exception as exc:
                        self._report_ui_error("council event", exc)
        except Exception as exc:
            self._report_ui_error("event pump", exc)
        if self._closing:
            council_stopped = self.council is None or not self.council.is_alive()
            if (not self.session.is_alive() and council_stopped) or time.monotonic() >= self._close_deadline:
                try:
                    self.root.destroy()
                except tk.TclError:
                    pass
                return
        try:
            self.root.after(40, self._poll_events)
        except tk.TclError:
            pass

    CLOSE_GRACE_IDLE = 3.0
    CLOSE_GRACE_BUSY = 10.0

    def close(self) -> None:
        """Shut down; while a reply is mid-run the window shows "Finishing…" and
        waits up to ten seconds for the worker to stop before it is destroyed."""
        if self._closing:
            return
        self._closing = True
        mid_run = bool(self.busy)
        self._close_deadline = time.monotonic() + (self.CLOSE_GRACE_BUSY if mid_run else self.CLOSE_GRACE_IDLE)
        geometry = getattr(self, "_pending_geometry", None)
        if geometry:
            self.settings.set("geometry", geometry)
        self.status_text = "Finishing…" if mid_run else "Closing…"
        self.activity_text = "Stopping the current reply safely before closing" if mid_run else "Closing"
        try:
            self.composer.set_ready(False)
            self.composer.hint.configure(text="Finishing…" if mid_run else "Closing…")
            self.title_label.configure(text="Finishing…" if mid_run else "Closing…")
        except (AttributeError, tk.TclError):
            pass
        self.refresh_status()
        self.flush_pending_deletes()
        self.jobs.shutdown()
        self.session.shutdown()
        if self.hotkey is not None:
            self.hotkey.stop()
        if self.tray is not None:
            try:
                self.tray.stop()
            except Exception:
                pass
        companion = self.companion
        if companion is not None:
            try:
                companion.destroy()
            except tk.TclError:
                pass
        if self.council is not None:
            self.council.shutdown()

    def show_provider_settings(self) -> None:
        if self.busy:
            self.toast("Stop or finish the current request before switching providers.", kind="warning")
            return
        if self.provider_window is not None and self.provider_window.winfo_exists():
            self.provider_window.lift()
            self.provider_window.focus_set()
            return
        self.provider_window = ProviderSettingsWindow(self)

    def configure_provider_choice(
        self,
        choice: str,
        callback: Callable[[str | None], None],
    ) -> None:
        if self.busy:
            callback("Stop or finish the current request before switching providers.")
            return

        def apply_choice() -> None:
            try:
                configure_provider(
                    choice,
                    root=Path(getattr(self.config, "root", ".")),
                    require_ready=True,
                )
                updated = config_with_provider_choice(self.config, choice)
            except (ProviderSetupError, OSError, ValueError) as exc:
                message = safe_ui_text(exc, 500)
                self.root.after(0, lambda: callback(message))
                return

            def finish() -> None:
                self.config = updated
                self.provider_choice = choice
                self.status_text = "Connecting…"
                self.activity_text = "Switching model provider…"
                self.refresh_status()
                self.session.switch_provider(updated)
                callback(None)
                display = next(
                    (name for key, name, _detail, _requirement in PROVIDER_CHOICES if key == choice),
                    choice,
                )
                self.toast(f"Provider saved: {display}.", kind="success")

            self.root.after(0, finish)

        threading.Thread(
            target=apply_choice,
            name="jarvis-provider-settings",
            daemon=True,
        ).start()


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
