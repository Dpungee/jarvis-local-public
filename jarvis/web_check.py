"""Bounded headless-browser verification of a local web app.

``http_health`` proves that a port answers. It cannot show that a page renders, runs
without script errors, or reacts to the keyboard and mouse. This module loads one loopback
URL in a disposable headless Edge/Chrome profile, drives it through the DevTools protocol
with a small list of input actions, and reports what it observed.

Boundaries:

* only ``http`` URLs on a loopback address (127.0.0.0/8, ::1, ``localhost``) are opened;
* every request the page makes is intercepted, and anything that is not a loopback HTTP(S)
  resource or an inline scheme (``data:``/``blob:``/``about:``) is refused and reported, so
  a check never sends traffic to the public internet;
* the browser runs with a fresh temporary profile, no extensions, no sync, no background
  networking, inside a kill-on-close job, and is removed afterwards;
* ``inspect`` actions evaluate with V8's side-effect guard (``throwOnSideEffect``), so they
  can read page state but cannot change it;
* the whole check, the action count, text excerpts and saved screenshots are bounded.

Input attribution: games animate on their own (gravity, timers), so "the screen changed after
a key press" proves little by itself. Before the page loads, the check installs a
deterministic page clock: timers, ``requestAnimationFrame``, ``performance.now`` and ``Date``
advance only when the check advances them. For each input the check first finds a quiet
control window (a span of page time with no input and no change), then dispatches the
input and renders at the same clock instant; a screen or canvas change counts as a response even
for a page that animates. If a page replaces the clock, the check falls back to real time,
where an animating page must show a text or ``inspect`` change instead.

The DevTools connection is a local WebSocket to the browser this module started; the
standard library is enough for it, so no dependency is added.
"""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import tempfile
import time
import urllib.parse
import uuid
import zlib
from pathlib import Path
from typing import Any, Callable

MAX_ACTIONS = 40
MAX_KEY_REPEAT = 20
MAX_TOTAL_SECONDS = 90
MAX_TEXT_EXCERPT = 1_200
MAX_INSPECT_CHARS = 1_000
MAX_REPORTED_ITEMS = 12
THUMB_SCALE = 0.1
CHANGE_THRESHOLD = 0.002  # fraction of thumbnail pixels that must differ

_KEYS: dict[str, tuple[str, str, int]] = {
    "arrowleft": ("ArrowLeft", "ArrowLeft", 37),
    "arrowup": ("ArrowUp", "ArrowUp", 38),
    "arrowright": ("ArrowRight", "ArrowRight", 39),
    "arrowdown": ("ArrowDown", "ArrowDown", 40),
    "left": ("ArrowLeft", "ArrowLeft", 37),
    "up": ("ArrowUp", "ArrowUp", 38),
    "right": ("ArrowRight", "ArrowRight", 39),
    "down": ("ArrowDown", "ArrowDown", 40),
    "space": (" ", "Space", 32),
    " ": (" ", "Space", 32),
    "spacebar": (" ", "Space", 32),
    "enter": ("Enter", "Enter", 13),
    "return": ("Enter", "Enter", 13),
    "escape": ("Escape", "Escape", 27),
    "esc": ("Escape", "Escape", 27),
    "tab": ("Tab", "Tab", 9),
    "backspace": ("Backspace", "Backspace", 8),
    "shift": ("Shift", "ShiftLeft", 16),
    "control": ("Control", "ControlLeft", 17),
    "alt": ("Alt", "AltLeft", 18),
}

_OBSERVE_JS = r"""
(() => {
  const text = ((document.body && document.body.innerText) || '').replace(/\s+/g, ' ').trim();
  let visible = 0;
  for (const el of document.querySelectorAll('body *')) {
    const r = el.getBoundingClientRect();
    if (r.width > 1 && r.height > 1 && r.bottom > 0 && r.right > 0) { visible++; if (visible >= 2000) break; }
  }
  const canvases = [...document.querySelectorAll('canvas')].slice(0, 8).map(c => {
    let signature = 'unavailable';
    try {
      const data = c.toDataURL();
      let h = 2166136261;
      for (let i = 0; i < data.length; i += 5) { h ^= data.charCodeAt(i); h = Math.imul(h, 16777619); }
      signature = (h >>> 0).toString(16) + ':' + data.length;
    } catch (e) { signature = 'tainted'; }
    return {width: c.width, height: c.height, signature};
  });
  return {title: document.title, text: text.slice(0, 4000), text_length: text.length,
          visible_elements: visible, canvases, ready_state: document.readyState};
})()
"""


class WebCheckError(RuntimeError):
    """The check could not run (no browser, protocol failure); not a verdict on the app."""


# ---------------------------------------------------------------------------- targets
def validate_local_url(url: str) -> tuple[str, str, int]:
    """Return (url, host, port) for a loopback http URL, or raise ``PermissionError``."""
    if not isinstance(url, str) or url != url.strip() or not url or len(url) > 2048:
        raise ValueError("url must be a non-empty local HTTP URL")
    if any(ord(char) < 32 or ord(char) == 127 for char in url):
        raise ValueError("url contains control characters")
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "http" or not parsed.hostname:
        raise PermissionError("Browser checks support only local plain-HTTP URLs")
    if parsed.username is not None or parsed.password is not None:
        raise PermissionError("Credentials in browser-check URLs are blocked")
    try:
        port = parsed.port or 80
    except ValueError as exc:
        raise ValueError("Invalid browser-check URL port") from exc
    if not _is_loopback_host(parsed.hostname):
        raise PermissionError("Browser checks are limited to this computer (loopback addresses)")
    return url, parsed.hostname, port


def _is_loopback_host(hostname: str | None) -> bool:
    if not hostname:
        return False
    if hostname.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname.strip("[]").split("%", 1)[0]).is_loopback
    except ValueError:
        return False


def _request_allowed(url: str) -> bool:
    scheme = urllib.parse.urlsplit(url).scheme.casefold()
    if scheme in {"data", "blob", "about"}:
        return True
    if scheme not in {"http", "https", "ws", "wss"}:
        return False
    return _is_loopback_host(urllib.parse.urlsplit(url).hostname)


def browser_executable() -> Path | None:
    """A native Edge or Chrome from its fixed per-machine install location."""
    from .model_client import _validated_native_executable

    roots = [os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"),
             os.environ.get("ProgramW6432")]
    relative = (("Microsoft", "Edge", "Application", "msedge.exe"),
                ("Google", "Chrome", "Application", "chrome.exe"))
    for parts in relative:
        for root in roots:
            if not root:
                continue
            resolved = _validated_native_executable(Path(root).joinpath(*parts))
            if resolved is not None:
                return resolved
    return None


# ------------------------------------------------------------------------- websocket
class _WebSocket:
    """Minimal RFC 6455 client for one local DevTools endpoint."""

    def __init__(self, host: str, port: int, path: str, timeout: float) -> None:
        self.sock = socket.create_connection((host, port), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        request = (f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\n"
                   f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n")
        self.sock.sendall(request.encode("ascii"))
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise WebCheckError("DevTools handshake closed")
            data += chunk
            if len(data) > 65536:
                raise WebCheckError("DevTools handshake too large")
        head, _, self.buffer = data.partition(b"\r\n\r\n")
        expected = base64.b64encode(hashlib.sha1(
            (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode(),
            usedforsecurity=False).digest()).decode()
        lines = head.decode("latin-1").split("\r\n")
        if " 101 " not in lines[0] + " " or not any(
                line.lower().startswith("sec-websocket-accept:") and line.split(":", 1)[1].strip() == expected
                for line in lines[1:]):
            raise WebCheckError("DevTools handshake was refused")
        self._fragments: list[bytes] = []

    def send(self, text: str) -> None:
        payload = text.encode("utf-8")
        header = bytearray([0x81])
        length = len(payload)
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", length)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", length)
        mask = os.urandom(4)
        masked = bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload))
        self.sock.sendall(bytes(header) + mask + masked)

    def _read(self, count: int, deadline: float) -> bytes:
        while len(self.buffer) < count:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            self.sock.settimeout(min(remaining, 1.0))
            try:
                chunk = self.sock.recv(1 << 16)
            except socket.timeout:
                continue
            if not chunk:
                raise WebCheckError("DevTools connection closed")
            self.buffer += chunk
        data, self.buffer = self.buffer[:count], self.buffer[count:]
        return data

    def recv(self, timeout: float) -> str | None:
        """One text message, or ``None`` when nothing arrived before the timeout."""
        deadline = time.monotonic() + timeout
        while True:
            try:
                first, second = self._read(2, deadline)
            except TimeoutError:
                return None
            # Once a frame has started, read it to the end even past the soft timeout.
            hard = time.monotonic() + 30
            opcode, length = first & 0x0F, second & 0x7F
            if length == 126:
                length = struct.unpack(">H", self._read(2, hard))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._read(8, hard))[0]
            if length > 64 * 1024 * 1024:
                raise WebCheckError("DevTools message too large")
            mask = self._read(4, hard) if second & 0x80 else b""
            payload = self._read(length, hard)
            if mask:
                payload = bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload))
            if opcode == 0x8:
                raise WebCheckError("DevTools connection closed")
            if opcode == 0x9:  # ping
                self.sock.sendall(bytes([0x8A, 0x80]) + os.urandom(4))
                continue
            if opcode in (0x1, 0x2, 0x0):
                self._fragments.append(payload)
                if first & 0x80:
                    message = b"".join(self._fragments)
                    self._fragments = []
                    return message.decode("utf-8", errors="replace")

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


class _DevTools:
    def __init__(self, socket_: _WebSocket, on_event: Callable[[dict[str, Any]], None]) -> None:
        self.ws = socket_
        self.on_event = on_event
        self.next_id = 0

    def send(self, method: str, params: dict[str, Any] | None = None, session: str | None = None) -> int:
        self.next_id += 1
        message: dict[str, Any] = {"id": self.next_id, "method": method, "params": params or {}}
        if session:
            message["sessionId"] = session
        self.ws.send(json.dumps(message))
        return self.next_id

    def call(self, method: str, params: dict[str, Any] | None = None, session: str | None = None,
             timeout: float = 15.0) -> dict[str, Any]:
        wanted = self.send(method, params, session)
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise WebCheckError(f"DevTools call timed out: {method}")
            raw = self.ws.recv(remaining)
            if raw is None:
                continue
            message = json.loads(raw)
            if message.get("id") == wanted:
                if "error" in message:
                    raise WebCheckError(f"{method}: {str(message['error'].get('message'))[:200]}")
                return message.get("result") or {}
            if "method" in message:
                self.on_event(message)

    def pump(self, seconds: float) -> None:
        deadline = time.monotonic() + max(0.0, seconds)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            raw = self.ws.recv(remaining)
            if raw is None:
                return
            message = json.loads(raw)
            if "method" in message:
                self.on_event(message)


# ------------------------------------------------------------------------------- png
def _png_pixels(data: bytes) -> tuple[int, int, list[bytes]]:
    """Decode an 8-bit, non-interlaced RGB/RGBA PNG into rows of pixels (small images)."""
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG")
    offset, width, height, color, idat = 8, 0, 0, 0, b""
    while offset < len(data):
        length = struct.unpack(">I", data[offset:offset + 4])[0]
        kind = data[offset + 4:offset + 8]
        body = data[offset + 8:offset + 8 + length]
        if kind == b"IHDR":
            width, height, depth, color, _, _, interlace = struct.unpack(">IIBBBBB", body)
            if depth != 8 or color not in (2, 6) or interlace or width * height > 400_000:
                raise ValueError("unsupported PNG")
        elif kind == b"IDAT":
            idat += body
        offset += 12 + length
    channels = 4 if color == 6 else 3
    raw = zlib.decompress(idat)
    stride = width * channels
    rows: list[bytes] = []
    previous = bytearray(stride)
    position = 0
    for _ in range(height):
        kind = raw[position]
        line = bytearray(raw[position + 1:position + 1 + stride])
        position += 1 + stride
        for i in range(stride):
            left = line[i - channels] if i >= channels else 0
            up = previous[i]
            corner = previous[i - channels] if i >= channels else 0
            if kind == 1:
                line[i] = (line[i] + left) & 0xFF
            elif kind == 2:
                line[i] = (line[i] + up) & 0xFF
            elif kind == 3:
                line[i] = (line[i] + ((left + up) >> 1)) & 0xFF
            elif kind == 4:
                estimate = left + up - corner
                pa, pb, pc = abs(estimate - left), abs(estimate - up), abs(estimate - corner)
                predictor = left if pa <= pb and pa <= pc else up if pb <= pc else corner
                line[i] = (line[i] + predictor) & 0xFF
        rows.append(bytes(b for j in range(0, stride, channels) for b in line[j:j + 3]))
        previous = line
    return width, height, rows


def _thumb_stats(png: bytes) -> dict[str, Any]:
    width, height, rows = _png_pixels(png)
    pixels = b"".join(rows)
    colors = {pixels[i:i + 3] for i in range(0, len(pixels), 3)}
    return {"width": width, "height": height, "pixels": pixels, "distinct_colors": len(colors)}


def _pixel_change(before: dict[str, Any] | None, after: dict[str, Any] | None) -> float:
    if not before or not after or len(before["pixels"]) != len(after["pixels"]):
        return 1.0 if before is not after else 0.0
    a, b = before["pixels"], after["pixels"]
    total = len(a) // 3
    changed = sum(1 for i in range(0, len(a), 3) if a[i:i + 3] != b[i:i + 3])
    return changed / max(1, total)


# ----------------------------------------------------------------------------- check
def _clip(text: Any, limit: int) -> str:
    value = str(text)
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _normalize_actions(actions: Any) -> list[dict[str, Any]]:
    if actions is None:
        return []
    if not isinstance(actions, list) or len(actions) > MAX_ACTIONS:
        raise ValueError(f"actions must be a list of at most {MAX_ACTIONS} steps")
    normalized: list[dict[str, Any]] = []
    for raw in actions:
        if not isinstance(raw, dict):
            raise ValueError("each action must be an object")
        kind = str(raw.get("type") or "").strip().casefold()
        if kind == "key":
            key = str(raw.get("key") or "")
            if not key or len(key) > 20:
                raise ValueError("key actions need a key name such as ArrowLeft, Space, Enter or a letter")
            repeat = raw.get("repeat", 1)
            if isinstance(repeat, bool) or not isinstance(repeat, int) or not 1 <= repeat <= MAX_KEY_REPEAT:
                raise ValueError(f"repeat must be an integer between 1 and {MAX_KEY_REPEAT}")
            _key_definition(key)
            normalized.append({"type": "key", "key": key, "repeat": repeat})
        elif kind == "click":
            selector = raw.get("selector")
            if selector is not None:
                if not isinstance(selector, str) or not selector.strip() or len(selector) > 300:
                    raise ValueError("selector must be a short CSS selector")
                normalized.append({"type": "click", "selector": selector})
            else:
                x, y = raw.get("x"), raw.get("y")
                if not all(isinstance(v, (int, float)) and not isinstance(v, bool) and 0 <= v <= 4000 for v in (x, y)):
                    raise ValueError("click needs a selector or x/y coordinates")
                normalized.append({"type": "click", "x": float(x), "y": float(y)})
        elif kind == "drag":
            points = [raw.get(k) for k in ("x", "y", "to_x", "to_y")]
            if not all(isinstance(v, (int, float)) and not isinstance(v, bool) and 0 <= v <= 4000 for v in points):
                raise ValueError("drag needs x, y, to_x and to_y page coordinates")
            normalized.append({"type": "drag", "x": float(points[0]), "y": float(points[1]),
                               "to_x": float(points[2]), "to_y": float(points[3])})
        elif kind == "type":
            text = raw.get("text")
            if not isinstance(text, str) or not text or len(text) > 500:
                raise ValueError("type actions need 1-500 characters of text")
            normalized.append({"type": "type", "text": text})
        elif kind == "wait":
            ms = raw.get("ms", 500)
            if isinstance(ms, bool) or not isinstance(ms, int) or not 0 <= ms <= 5000:
                raise ValueError("wait ms must be an integer between 0 and 5000")
            normalized.append({"type": "wait", "ms": ms})
        elif kind == "inspect":
            expression = raw.get("expression")
            if not isinstance(expression, str) or not expression.strip() or len(expression) > 500:
                raise ValueError("inspect needs a read-only JavaScript expression (up to 500 characters)")
            normalized.append({"type": "inspect", "expression": expression})
        else:
            raise ValueError("action type must be key, click, drag, type, wait or inspect")
    return normalized


def _key_definition(name: str) -> tuple[str, str, int, str | None]:
    folded = name.casefold()
    if folded in _KEYS:
        key, code, vk = _KEYS[folded]
        return key, code, vk, " " if key == " " else ("\r" if key == "Enter" else None)
    if len(name) == 1 and name.isascii() and name.isprintable():
        if name.isalpha():
            return name, f"Key{name.upper()}", ord(name.upper()), name
        if name.isdigit():
            return name, f"Digit{name}", ord(name), name
        return name, "", 0, name
    raise ValueError(f"unsupported key: {name}")


_CLOCK_JS = r"""(() => {
  if (window.__jarvisClock) return;
  const realSetTimeout = window.setTimeout.bind(window);
  const RealDate = Date, epoch = RealDate.now(), perfStart = performance.now();
  let now = 0, seq = 0, timers = new Map(), frames = new Map();
  const rethrow = e => realSetTimeout(() => { throw e; }, 0);
  const run = (fn, args) => { try { typeof fn === 'function' ? fn(...args) : (0, eval)(String(fn)); } catch (e) { rethrow(e); } };
  window.setTimeout = (fn, ms, ...args) => { const id = ++seq; timers.set(id, {fn, args, due: now + Math.max(0, +ms || 0), every: 0}); return id; };
  window.setInterval = (fn, ms, ...args) => { const id = ++seq; const every = Math.max(1, +ms || 0); timers.set(id, {fn, args, due: now + every, every}); return id; };
  window.clearTimeout = window.clearInterval = id => { timers.delete(id); };
  window.requestAnimationFrame = fn => { const id = ++seq; frames.set(id, fn); return id; };
  window.cancelAnimationFrame = id => { frames.delete(id); };
  performance.now = () => perfStart + now;
  function FakeDate(...a) { return a.length ? new RealDate(...a) : new RealDate(epoch + now); }
  FakeDate.prototype = RealDate.prototype; FakeDate.now = () => epoch + now;
  FakeDate.parse = RealDate.parse; FakeDate.UTC = RealDate.UTC; window.Date = FakeDate;
  window.__jarvisClock = { flushFrames() {
    // Render input-driven state without advancing timers or the page clock.
    const due = [...frames.values()]; frames = new Map();
    for (const fn of due) run(fn, [perfStart + now]);
    return due.length;
  }, advance(ms) {
    const end = now + ms; let fired = 0;
    for (let guard = 0; guard < 100000; guard++) {
      let next = null;
      for (const [id, t] of timers) if (t.due <= end && (!next || t.due < next[1].due)) next = [id, t];
      const frameAt = Math.floor(now / 16) * 16 + 16;
      if (next && next[1].due <= frameAt) {
        const [id, t] = next; now = Math.max(now, t.due);
        if (t.every) t.due += t.every; else timers.delete(id);
        fired++; run(t.fn, t.args); continue;
      }
      if (frameAt > end) break;
      now = frameAt; const due = [...frames.values()]; frames = new Map();
      for (const fn of due) run(fn, [perfStart + now]);
    }
    now = end; return fired;
  } };
})()"""
CONTROL_WINDOW_MS = 48  # three 16 ms animation frames of page time


def _wait_for_devtools_port(port_file: Path, deadline: float) -> tuple[int, str]:
    """Wait for a complete startup marker, including Windows writer sharing races."""
    while time.monotonic() < deadline:
        try:
            lines = port_file.read_text(encoding="utf-8").split()
        except (FileNotFoundError, PermissionError, UnicodeDecodeError):
            lines = []
        if (len(lines) == 2 and re.fullmatch(r"[0-9]{1,5}", lines[0])
                and 1 <= int(lines[0]) <= 65535
                and re.fullmatch(r"/devtools/browser/[A-Za-z0-9-]+", lines[1])):
            # Even a successful read must not extend the caller's startup budget.
            if time.monotonic() < deadline:
                return int(lines[0]), lines[1]
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(0.05, remaining))
    raise WebCheckError("The headless browser did not start.")


def run_web_check(url: str, *, actions: Any = None, settle_ms: int = 800, timeout_seconds: int = 60,
                  artifact_dir: Path | None = None, browser: Path | None = None) -> dict[str, Any]:
    """Load ``url`` headlessly, run ``actions`` and return a bounded observation report."""
    url, host, port = validate_local_url(url)
    steps_in = _normalize_actions(actions)
    for name, value, low, high in (("settle_ms", settle_ms, 0, 5000), ("timeout_seconds", timeout_seconds, 5, MAX_TOTAL_SECONDS)):
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise ValueError(f"{name} must be an integer between {low} and {high}")
    executable = browser or browser_executable()
    if executable is None:
        raise WebCheckError("No Microsoft Edge or Google Chrome installation was found for browser checks.")
    started = time.monotonic()
    deadline = started + timeout_seconds
    profile = Path(tempfile.mkdtemp(prefix="jarvis-webcheck-"))
    process: subprocess.Popen[bytes] | None = None
    job = None
    socket_: _WebSocket | None = None
    state: dict[str, Any] = {"errors": [], "console_errors": [], "blocked": [], "failed": [],
                             "status": None, "loaded": False, "main_frame": None}

    def on_event(message: dict[str, Any]) -> None:
        method, params = message.get("method"), message.get("params") or {}
        session = message.get("sessionId")
        if method == "Fetch.requestPaused":
            request_url = str((params.get("request") or {}).get("url") or "")
            if _request_allowed(request_url):
                devtools.send("Fetch.continueRequest", {"requestId": params["requestId"]}, session)
            else:
                devtools.send("Fetch.failRequest", {"requestId": params["requestId"],
                                                    "errorReason": "BlockedByClient"}, session)
                if len(state["blocked"]) < MAX_REPORTED_ITEMS:
                    state["blocked"].append(_clip(request_url, 200))
        elif method == "Runtime.exceptionThrown":
            details = params.get("exceptionDetails") or {}
            text = (details.get("exception") or {}).get("description") or details.get("text") or "Uncaught error"
            if len(state["errors"]) < MAX_REPORTED_ITEMS:
                state["errors"].append(_clip(text, 300))
        elif method == "Runtime.consoleAPICalled" and params.get("type") in {"error", "assert"}:
            text = " ".join(str(arg.get("value", arg.get("description", ""))) for arg in params.get("args") or [])
            if len(state["console_errors"]) < MAX_REPORTED_ITEMS:
                state["console_errors"].append(_clip(text or "console.error", 300))
        elif method == "Log.entryAdded":
            entry = params.get("entry") or {}
            text = str(entry.get("text") or "")
            # Requests this check refused are reported under blocked_non_local_requests, and a
            # missing favicon is browser boilerplate; neither says anything about the app.
            if "ERR_BLOCKED_BY_CLIENT" in text or urllib.parse.urlsplit(
                    str(entry.get("url") or "")).path == "/favicon.ico":
                return
            if entry.get("level") == "error" and len(state["console_errors"]) < MAX_REPORTED_ITEMS:
                state["console_errors"].append(_clip(f"{entry.get('text', '')} {entry.get('url', '')}".strip(), 300))
        elif method == "Network.responseReceived" and params.get("type") == "Document":
            if state["status"] is None:
                state["status"] = (params.get("response") or {}).get("status")
        elif method == "Network.loadingFailed" and not params.get("canceled"):
            if params.get("blockedReason") is None and len(state["failed"]) < MAX_REPORTED_ITEMS:
                state["failed"].append(_clip(params.get("errorText") or "failed", 120))
        elif method == "Page.loadEventFired":
            state["loaded"] = True

    try:
        # Start suspended, contain in a kill-on-close job, then resume: the launcher hands off
        # to child processes within milliseconds, and every one of them must be in the job.
        flags = (getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                 | (0x00000004 if os.name == "nt" else 0))
        process = subprocess.Popen(
            [str(executable), "--headless=new", "--remote-debugging-port=0",
             f"--user-data-dir={profile}", "--no-first-run", "--no-default-browser-check",
             "--disable-extensions", "--disable-sync", "--disable-background-networking",
             "--disable-component-update", "--disable-default-apps", "--mute-audio",
             "--window-size=1280,900", "about:blank"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=flags if os.name == "nt" else 0)
        if os.name == "nt":
            from .execution import WindowsJob, _resume_windows_process
            job = WindowsJob(process)
            if job.handle is None:
                raise WebCheckError("The headless browser could not be contained; the check did not run.")
            _resume_windows_process(process)
        port_file = profile / "DevToolsActivePort"
        # The launcher may exit after handing off. Existence alone does not mean
        # its marker is complete or that Windows has released the writer's handle.
        debug_port, debug_path = _wait_for_devtools_port(port_file, min(deadline, started + 20))
        socket_ = _WebSocket("127.0.0.1", debug_port, debug_path, timeout=10)
        devtools = _DevTools(socket_, on_event)
        target = devtools.call("Target.createTarget", {"url": "about:blank"})["targetId"]
        session = devtools.call("Target.attachToTarget", {"targetId": target, "flatten": True})["sessionId"]
        for method, params in (("Fetch.enable", {"patterns": [{"urlPattern": "*", "requestStage": "Request"}]}),
                               ("Page.enable", {}), ("Runtime.enable", {}), ("Log.enable", {}),
                               ("Network.enable", {}),
                               ("Emulation.setDeviceMetricsOverride",
                                {"width": 1280, "height": 900, "deviceScaleFactor": 1, "mobile": False})):
            devtools.call(method, params, session)
        devtools.call("Page.addScriptToEvaluateOnNewDocument", {"source": _CLOCK_JS}, session)
        navigation = devtools.call("Page.navigate", {"url": url}, session, timeout=20)
        if navigation.get("errorText"):
            raise _NavigationFailed(str(navigation["errorText"]))
        load_deadline = min(deadline, time.monotonic() + 15)
        while not state["loaded"] and time.monotonic() < load_deadline:
            devtools.pump(0.1)

        def advance(ms: int) -> bool:
            """Advance the page clock by ``ms``; False when the page replaced the clock."""
            fired = devtools.call("Runtime.evaluate", {
                "expression": f"window.__jarvisClock ? window.__jarvisClock.advance({int(ms)}) : -1",
                "returnByValue": True}, session).get("result", {}).get("value")
            devtools.pump(0.02)  # deliver any exceptions or console errors the callbacks raised
            return isinstance(fired, int) and fired >= 0

        def elapse(ms: int) -> None:
            if not (clocked and advance(ms)):
                devtools.pump(ms / 1000)

        def flush_frames() -> None:
            devtools.call("Runtime.evaluate", {
                "expression": "window.__jarvisClock && window.__jarvisClock.flushFrames()",
                "returnByValue": True}, session)
            devtools.pump(0.02)

        devtools.pump(0.2)  # real time for scripts and assets still arriving
        clocked = advance(settle_ms)
        if not clocked:
            devtools.pump(settle_ms / 1000)

        def observe() -> dict[str, Any]:
            value = devtools.call("Runtime.evaluate", {"expression": _OBSERVE_JS, "returnByValue": True},
                                  session).get("result", {}).get("value") or {}
            shot = devtools.call("Page.captureScreenshot", {
                "format": "png", "clip": {"x": 0, "y": 0, "width": 1280, "height": 900, "scale": THUMB_SCALE}},
                session)
            try:
                value["thumb"] = _thumb_stats(base64.b64decode(shot.get("data") or ""))
            except (ValueError, zlib.error, struct.error):
                value["thumb"] = None
            return value

        def screenshot(label: str) -> str | None:
            if artifact_dir is None:
                return None
            shot = devtools.call("Page.captureScreenshot", {"format": "png"}, session, timeout=20)
            artifact_dir.mkdir(parents=True, exist_ok=True)
            path = artifact_dir / f"{label}.png"
            path.write_bytes(base64.b64decode(shot.get("data") or ""))
            return str(path)

        def same(a: dict[str, Any], b: dict[str, Any]) -> bool:
            return (_pixel_change(a.get("thumb"), b.get("thumb")) <= CHANGE_THRESHOLD
                    and a.get("text") == b.get("text")
                    and [c["signature"] for c in a.get("canvases", [])]
                    == [c["signature"] for c in b.get("canvases", [])])

        first = observe()
        shots = [screenshot("loaded")]
        elapse(700)
        idle = observe()
        idle_change = _pixel_change(first.get("thumb"), idle.get("thumb"))
        animating = idle_change > CHANGE_THRESHOLD or [c["signature"] for c in first.get("canvases", [])] != \
            [c["signature"] for c in idle.get("canvases", [])]
        previous = idle
        steps: list[dict[str, Any]] = []
        last_inspect: dict[str, tuple[str, int]] = {}
        input_count = 0
        responded_by: list[str] = []
        for index, action in enumerate(steps_in):
            if time.monotonic() > deadline - 3:
                steps.append({"action": action, "skipped": "time limit reached"})
                continue
            kind = action["type"]
            record: dict[str, Any] = {"action": action}
            quiet = False
            if clocked and kind in {"key", "click", "drag", "type"}:
                # Control window: the same span of page time with no input. Periodic motion
                # leaves quiet windows between ticks; only continuous animation stays ambiguous.
                base = previous
                advance(CONTROL_WINDOW_MS)
                # advance() services RAF at 16-ms boundaries; its endpoint can
                # fall between frames. Render that endpoint before comparing
                # observations, or the later baseline flush can cross a gravity
                # threshold that the control screenshot has not seen yet.
                flush_frames()
                control = observe()
                for _ in range(5):
                    if same(base, control):
                        break
                    base = control
                    advance(CONTROL_WINDOW_MS)
                    flush_frames()
                    control = observe()
                quiet = same(base, control)
                # A quiet interval does not imply the NEXT interval is quiet:
                # advancing after input can cross an unrelated gravity/timer tick.
                # Compare input rendering at one fixed page-clock instant instead.
                # A second no-input flush at that same instant must also be quiet:
                # frame-count-driven motion is not evidence of input response.
                flush_frames()
                baseline = observe()
                quiet = quiet and same(control, baseline)
                record["control_window_quiet"] = quiet
                previous = baseline
            if kind == "key":
                key, code, vk, text = _key_definition(action["key"])
                for _ in range(action["repeat"]):
                    down: dict[str, Any] = {"type": "keyDown" if text else "rawKeyDown", "key": key, "code": code,
                                            "windowsVirtualKeyCode": vk, "nativeVirtualKeyCode": vk}
                    if text:
                        down["text"] = text
                    devtools.call("Input.dispatchKeyEvent", down, session)
                    devtools.call("Input.dispatchKeyEvent", {"type": "keyUp", "key": key, "code": code,
                                                             "windowsVirtualKeyCode": vk,
                                                             "nativeVirtualKeyCode": vk}, session)
                    devtools.pump(0.03)
                input_count += 1
            elif kind == "click":
                if "selector" in action:
                    box = devtools.call("Runtime.evaluate", {
                        "expression": "(() => { const e = document.querySelector(" + json.dumps(action["selector"])
                                      + "); if (!e) return null; const r = e.getBoundingClientRect();"
                                      " return [r.left + r.width / 2, r.top + r.height / 2]; })()",
                        "returnByValue": True}, session).get("result", {}).get("value")
                    if not box:
                        record["error"] = "selector matched nothing"
                        steps.append(record)
                        continue
                    x, y = float(box[0]), float(box[1])
                else:
                    x, y = action["x"], action["y"]
                for event in ("mousePressed", "mouseReleased"):
                    devtools.call("Input.dispatchMouseEvent", {"type": event, "x": x, "y": y, "button": "left",
                                                               "clickCount": 1}, session)
                input_count += 1
            elif kind == "drag":
                # Press, move in steps with the button held, release: painting, sliders, drag-and-drop.
                x0, y0, x1, y1 = action["x"], action["y"], action["to_x"], action["to_y"]
                devtools.call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x0, "y": y0}, session)
                devtools.call("Input.dispatchMouseEvent", {"type": "mousePressed", "x": x0, "y": y0,
                                                           "button": "left", "buttons": 1, "clickCount": 1}, session)
                for step in range(1, 11):
                    devtools.call("Input.dispatchMouseEvent", {
                        "type": "mouseMoved", "x": x0 + (x1 - x0) * step / 10, "y": y0 + (y1 - y0) * step / 10,
                        "button": "left", "buttons": 1}, session)
                devtools.call("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": x1, "y": y1,
                                                           "button": "left", "buttons": 0, "clickCount": 1}, session)
                input_count += 1
            elif kind == "type":
                devtools.call("Input.insertText", {"text": action["text"]}, session)
                input_count += 1
            elif kind == "wait":
                elapse(action["ms"])
            elif kind == "inspect":
                evaluation = devtools.call("Runtime.evaluate", {
                    "expression": action["expression"], "returnByValue": True, "throwOnSideEffect": True,
                    "timeout": 1000}, session)
                if evaluation.get("exceptionDetails"):
                    details = evaluation["exceptionDetails"]
                    record["error"] = _clip((details.get("exception") or {}).get("description")
                                            or details.get("text") or "evaluation failed", 300)
                else:
                    value = json.dumps((evaluation.get("result") or {}).get("value"), ensure_ascii=False,
                                       default=str)
                    record["value"] = _clip(value, MAX_INSPECT_CHARS)
                    earlier = last_inspect.get(action["expression"])
                    if earlier is not None and earlier[1] < input_count and earlier[0] != value:
                        record["changed_since_input"] = True
                        responded_by.append(f"inspect {index + 1}")
                    last_inspect[action["expression"]] = (value, input_count)
                steps.append(record)
                continue
            if kind in {"key", "click", "drag", "type"}:
                if clocked:
                    flush_frames()
                else:
                    devtools.pump(0.25)
            current = observe()
            change = _pixel_change(previous.get("thumb"), current.get("thumb"))
            text_changed = current.get("text") != previous.get("text")
            canvas_changed = [c["signature"] for c in current.get("canvases", [])] != \
                [c["signature"] for c in previous.get("canvases", [])]
            record.update(screen_change=round(change, 4), text_changed=text_changed,
                          canvas_changed=canvas_changed)
            if text_changed:
                record["text_excerpt"] = _clip(current.get("text") or "", 300)
            if kind in {"key", "click", "drag", "type"}:
                # A screen change proves a response when it is measured against a quiet
                # no-input control window (deterministic clock), or when the page does not
                # animate at all. Otherwise its text (or an inspect value) must change.
                attributable = quiet if clocked else not animating
                if text_changed or (attributable and (change > CHANGE_THRESHOLD or canvas_changed)):
                    record["responded"] = True
                    responded_by.append(f"step {index + 1}")
            steps.append(record)
            previous = current
        final = observe()
        shots.append(screenshot("final"))
        thumb = final.get("thumb") or {}
        rendered = bool(final.get("visible_elements")) and (thumb.get("distinct_colors", 0) >= 2
                                                            or bool(final.get("text")))
        status = state["status"]
        reasons: list[str] = []
        if status is None or not 200 <= int(status) < 400:
            reasons.append(f"The page did not load successfully (HTTP status {status}).")
        if not state["loaded"]:
            reasons.append("The page load event never fired.")
        if state["errors"] or state["console_errors"]:
            reasons.append("The page reported script or console errors.")
        if any(step.get("error") or step.get("skipped") for step in steps):
            reasons.append("One or more requested checks failed or were skipped; input response alone is insufficient.")
        if not rendered:
            reasons.append("Nothing visible rendered (blank or single-colour page).")
        if input_count == 0:
            reasons.append("No input was tested; add key, click or type actions.")
        elif not responded_by:
            reasons.append(
                "No observable response to input." + (
                    " The page animates continuously, so screen changes cannot prove input response; "
                    "show state in the page text or read it with inspect actions before and after input."
                    if animating else ""))
        return {
            "url": url,
            "verified": not reasons,
            "reasons_not_verified": reasons,
            "http_status": status,
            "loaded": state["loaded"],
            "title": _clip(final.get("title") or "", 200),
            "rendered": rendered,
            "visible_elements": final.get("visible_elements"),
            "canvases": [{"width": c["width"], "height": c["height"]} for c in final.get("canvases", [])],
            "animating_without_input": animating,
            "page_clock": "deterministic" if clocked else "real time (the page replaced its timers)",
            "input_actions": input_count,
            "responded_to_input": responded_by[:MAX_REPORTED_ITEMS],
            "errors": state["errors"],
            "console_errors": state["console_errors"],
            "blocked_non_local_requests": state["blocked"],
            "failed_requests": state["failed"],
            "steps": steps,
            "text_excerpt": _clip(final.get("text") or "", MAX_TEXT_EXCERPT),
            "screenshots": [shot for shot in shots if shot],
            "duration_ms": int((time.monotonic() - started) * 1000),
            "browser": executable.stem,
        }
    except _NavigationFailed as exc:
        return {"url": url, "verified": False, "loaded": False, "http_status": None,
                "reasons_not_verified": [f"Navigation failed: {_clip(exc, 200)}"],
                "duration_ms": int((time.monotonic() - started) * 1000)}
    except (OSError, ValueError, KeyError, IndexError, json.JSONDecodeError) as exc:
        raise WebCheckError(f"Browser check failed: {type(exc).__name__}: {_clip(exc, 200)}") from exc
    finally:
        if socket_ is not None:
            socket_.close()
        if process is not None:
            if job is not None:
                from .execution import _terminate_process_tree
                _terminate_process_tree(process, job)  # closing the job ends every browser process
            elif process.poll() is None:
                process.kill()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
        # The browser may hold profile files briefly after exit on Windows.
        for _ in range(20):
            shutil.rmtree(profile, ignore_errors=True)
            if not profile.exists():
                break
            time.sleep(0.1)


class _NavigationFailed(Exception):
    pass


def new_check_id() -> str:
    return uuid.uuid4().hex[:12]
