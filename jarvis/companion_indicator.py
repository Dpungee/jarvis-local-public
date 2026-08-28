from __future__ import annotations

import argparse
import ctypes
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .screen_companion import (
    COMPANION_INDICATOR_TITLE,
    COMPANION_SUGGESTION_TTL_SECONDS,
)

INDICATOR_WINDOW_TITLE = COMPANION_INDICATOR_TITLE
_ALLOWED_MODES = frozenset({"disabled", "observe", "suggest", "collaborate"})
_ALLOWED_ACTIONS = frozenset({"on", "pause", "resume", "off", "mode"})
_ALLOWED_ACTION_STATES = frozenset({
    "queued", "running", "completed", "incomplete", "needs_approval",
    "failed", "cancelled",
})


class _MonitorInfo(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
    ]


def _tk_geometry(width: int, height: int, x: int, y: int) -> str:
    """Return Tk geometry that remains valid on monitors with negative origins."""
    return f"{int(width)}x{int(height)}{int(x):+d}{int(y):+d}"


def _foreground_work_area(user32: Any) -> tuple[int, int, int, int] | None:
    """Return the active monitor's usable work area without reading screen content."""
    try:
        user32.GetForegroundWindow.argtypes = []
        user32.GetForegroundWindow.restype = wintypes.HWND
        user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
        user32.MonitorFromWindow.restype = wintypes.HANDLE
        user32.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
        user32.GetMonitorInfoW.restype = wintypes.BOOL
        foreground = user32.GetForegroundWindow()
        monitor = user32.MonitorFromWindow(foreground, 2)  # nearest monitor
        info = _MonitorInfo()
        info.cbSize = ctypes.sizeof(info)
        if not monitor or not user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
            return None
        return (
            int(info.rcWork.left),
            int(info.rcWork.top),
            int(info.rcWork.right),
            int(info.rcWork.bottom),
        )
    except (AttributeError, OSError, TypeError, ValueError):
        return None


def _show_windows_no_activate(window: Any, user32: Any) -> bool:
    """Map a withdrawn Tk toplevel without taking focus from the operator."""
    try:
        child = wintypes.HWND(int(window.winfo_id()))
        user32.GetParent.argtypes = [wintypes.HWND]
        user32.GetParent.restype = wintypes.HWND
        parent = user32.GetParent(child)
        hwnd = parent or child
        user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.GetWindowLongW.restype = ctypes.c_long
        user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_long]
        user32.SetWindowLongW.restype = ctypes.c_long
        user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.ShowWindow.restype = wintypes.BOOL
        user32.SetWindowPos.argtypes = [
            wintypes.HWND,
            wintypes.HWND,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.UINT,
        ]
        user32.SetWindowPos.restype = wintypes.BOOL
        style = int(user32.GetWindowLongW(hwnd, -20))
        user32.SetWindowLongW(hwnd, -20, style | 0x00000080 | 0x08000000)
        # Keep Tk's window state synchronized so withdraw() reliably hides it later.
        window.deiconify()
        window.update_idletasks()
        user32.ShowWindow(hwnd, 4)  # SW_SHOWNOACTIVATE
        return bool(
            user32.SetWindowPos(
                hwnd,
                wintypes.HWND(-1),  # HWND_TOPMOST
                0,
                0,
                0,
                0,
                0x0001 | 0x0002 | 0x0010 | 0x0040,
            )
        )
    except (AttributeError, OSError, TypeError, ValueError):
        return False


@dataclass(frozen=True)
class IndicatorPresentation:
    label: str
    detail: str
    color: str
    mode: str
    paused: bool
    online: bool


def indicator_presentation(state: dict[str, Any] | None) -> IndicatorPresentation:
    if not isinstance(state, dict):
        return IndicatorPresentation(
            "JARVIS OFFLINE", "Companion unavailable", "#ff7b82", "disabled", True, False
        )
    mode = str(state.get("mode") or "disabled").strip().casefold()
    paused = bool(state.get("paused", True))
    available = bool(state.get("available", False))
    if mode not in _ALLOWED_MODES or not available:
        return IndicatorPresentation(
            "JARVIS UNAVAILABLE", "Screen observation unavailable", "#ff7b82",
            "disabled", True, True,
        )
    if mode == "disabled":
        return IndicatorPresentation(
            "JARVIS OFF", "Screen Companion is off", "#8a8a8a", mode, True, True
        )
    if paused:
        return IndicatorPresentation(
            f"PAUSED · {mode.upper()}", "No screen observation is happening",
            "#f6c86b", mode, True, True,
        )
    labels = {
        "observe": ("OBSERVING", "Active app metadata only", "#65d99b"),
        "suggest": ("SUGGEST MODE", "Transient visual suggestions enabled", "#57c7ef"),
        "collaborate": (
            "COLLABORATING", "Approved routines may run", "#b794f6"
        ),
    }
    label, detail, color = labels[mode]
    return IndicatorPresentation(label, detail, color, mode, False, True)


def indicator_should_be_visible(state: dict[str, Any] | None) -> bool:
    """Show controls after Presence returns a real Companion status.

    A missing status remains hidden so startup and connection failures do not flash
    an unactionable window.  Off, paused, and unavailable states remain visible:
    those labels tell the operator that observation is not happening and preserve
    the On/Resume controls needed to change that state.
    """

    view = indicator_presentation(state)
    return isinstance(state, dict) and view.online


class CompanionIndicatorClient:
    """Minimal loopback client that never requests titles, rules, or screen data."""

    def __init__(self, host: str, port: int, *, timeout: float = 2.0) -> None:
        normalized_host = str(host).strip().casefold()
        if normalized_host not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("Companion indicator host must be loopback")
        if isinstance(port, bool) or not isinstance(port, int) or not 1024 <= port <= 65535:
            raise ValueError("Companion indicator port is invalid")
        authority = f"[{normalized_host}]" if ":" in normalized_host else normalized_host
        self.base_url = f"http://{authority}:{port}"
        self.timeout = max(0.25, min(float(timeout), 10.0))

    def _request(self, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        body = None
        headers = {"Accept": "application/json"}
        method = "GET"
        if payload is not None:
            body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
            method = "POST"
        request = urllib.request.Request(
            self.base_url + path,
            data=body,
            headers=headers,
            method=method,
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            if response.status != 200:
                raise RuntimeError(f"Presence returned HTTP {response.status}")
            decoded = json.loads(response.read(65_536).decode("utf-8"))
        if not isinstance(decoded, dict):
            raise RuntimeError("Presence returned an invalid indicator response")
        return decoded

    @staticmethod
    def _validated_state(payload: dict[str, Any]) -> dict[str, Any]:
        mode = payload.get("mode")
        paused = payload.get("paused")
        available = payload.get("available")
        if mode not in _ALLOWED_MODES or not isinstance(paused, bool):
            raise RuntimeError("Presence returned an invalid Companion state")
        if available is not None and not isinstance(available, bool):
            raise RuntimeError("Presence returned an invalid Companion availability state")
        suggestion = payload.get("suggestion")
        safe_suggestion = None
        if suggestion is not None:
            if not isinstance(suggestion, dict):
                raise RuntimeError("Presence returned an invalid Companion suggestion")
            suggestion_id = str(suggestion.get("id") or "")
            text = str(suggestion.get("text") or "").strip()
            expires_at = suggestion.get("expires_at")
            if (
                len(text) > 180
                or not text
                or re.fullmatch(r"[0-9a-f]{32}", suggestion_id) is None
                or isinstance(expires_at, bool)
                or not isinstance(expires_at, (int, float))
            ):
                raise RuntimeError("Presence returned an invalid Companion suggestion")
            safe_suggestion = {
                "id": suggestion_id,
                "text": text,
                "expires_at": float(expires_at),
            }
        return {
            "mode": mode,
            "paused": paused,
            "available": True if available is None else available,
            "updated_at": str(payload.get("updated_at") or "")[:80],
            "suggestion": safe_suggestion,
        }

    def status(self) -> dict[str, Any]:
        return self._validated_state(
            self._request("/api/screen-companion/indicator")
        )

    def control(self, action: str, *, mode: str | None = None) -> dict[str, Any]:
        normalized_action = str(action).strip().casefold()
        if normalized_action not in _ALLOWED_ACTIONS:
            raise ValueError("Companion indicator action is invalid")
        payload: dict[str, Any] = {"action": normalized_action}
        if normalized_action == "mode":
            normalized_mode = str(mode or "").strip().casefold()
            if normalized_mode not in {"observe", "suggest", "collaborate"}:
                raise ValueError("Companion indicator mode is invalid")
            payload["mode"] = normalized_mode
        elif mode is not None:
            raise ValueError("mode is only accepted with the mode action")
        response = self._request("/api/screen-companion/control", payload)
        state = response.get("state")
        if not isinstance(state, dict):
            raise RuntimeError("Presence did not return the changed Companion state")
        state = dict(state)
        state.setdefault("available", True)
        return self._validated_state(state)

    def respond_suggestion(self, suggestion_id: str, *, accept: bool) -> dict[str, Any]:
        normalized = str(suggestion_id).strip().casefold()
        if re.fullmatch(r"[0-9a-f]{32}", normalized) is None:
            raise ValueError("Companion suggestion ID is invalid")
        result = self._request(
            f"/api/screen-companion/suggestions/{normalized}/"
            + ("accept" if accept else "dismiss"),
            {},
        )
        if not isinstance(result.get("accepted"), bool):
            raise RuntimeError("Presence returned an invalid suggestion decision")
        return result

    @staticmethod
    def _validated_action_status(payload: dict[str, Any]) -> dict[str, Any]:
        action = payload.get("action")
        if not isinstance(action, dict):
            raise RuntimeError("Presence returned an invalid Companion action status")
        job_id = str(action.get("job_id") or "").strip().casefold()
        state = str(action.get("state") or "").strip().casefold()
        message = str(action.get("message") or "").strip()
        terminal = action.get("terminal")
        if (
            re.fullmatch(r"[0-9a-f]{32}", job_id) is None
            or state not in _ALLOWED_ACTION_STATES
            or not message
            or len(message) > 700
            or not isinstance(terminal, bool)
        ):
            raise RuntimeError("Presence returned an invalid Companion action status")
        return {
            "job_id": job_id,
            "state": state,
            "message": message,
            "terminal": terminal,
        }

    def action_status(self, job_id: str) -> dict[str, Any]:
        normalized = str(job_id).strip().casefold()
        if re.fullmatch(r"[0-9a-f]{32}", normalized) is None:
            raise ValueError("Companion action job ID is invalid")
        return self._validated_action_status(
            self._request(f"/api/screen-companion/actions/{normalized}")
        )


def _parent_is_alive(parent_pid: int) -> bool:
    if parent_pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(parent_pid, 0)
        except OSError:
            return False
        return True
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
    kernel32.GetExitCodeProcess.restype = ctypes.c_int
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel32.OpenProcess(0x1000, False, int(parent_pid))
    if not handle:
        return False
    try:
        exit_code = ctypes.c_ulong()
        return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))) and (
            exit_code.value == 259
        )
    finally:
        kernel32.CloseHandle(handle)


def _claim_single_instance() -> int | None:
    if os.name != "nt":
        return 1
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = int(kernel32.CreateMutexW(None, False, "Local\\JarvisCompanionIndicator") or 0)
    if not handle or ctypes.get_last_error() == 183:
        if handle:
            kernel32.CloseHandle(handle)
        return None
    return handle


class CompanionIndicatorApp:
    def __init__(self, client: CompanionIndicatorClient, parent_pid: int) -> None:
        import tkinter as tk

        self._tk = tk
        self.client = client
        self.parent_pid = parent_pid
        self.root = tk.Tk(className="JarvisCompanionIndicator")
        # Do not flash a connecting indicator before the first bounded status poll.
        # The window becomes visible after Presence returns a real Companion state.
        self.root.withdraw()
        self.root.title(INDICATOR_WINDOW_TITLE)
        self.root.configure(bg="#171717")
        self.root.resizable(False, False)
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        try:
            self.root.attributes("-toolwindow", True)
            self.root.attributes("-alpha", 0.97)
        except tk.TclError:
            pass
        self.root.focusmodel("passive")
        self._drag_origin: tuple[int, int, int, int] | None = None
        self._busy = False
        self._polling = False
        self._state: dict[str, Any] | None = None
        self._mode_var = tk.StringVar(value="observe")
        self._suggestion_id: str | None = None
        self._suggestion_busy = False
        self._suggestion_timer: str | None = None
        self._suggestion_job_id: str | None = None
        self._suggestion_action_polling = False
        self._suggestion_action_deadline = 0.0

        shell = tk.Frame(
            self.root, bg="#171717", highlightbackground="#444444",
            highlightthickness=1, padx=8, pady=7,
        )
        shell.pack(fill="both", expand=True)
        header = tk.Frame(shell, bg="#171717", cursor="fleur")
        header.grid(row=0, column=0, columnspan=5, sticky="ew", pady=(0, 5))
        self._dot = tk.Label(header, text="●", bg="#171717", fg="#8a8a8a", font=("Segoe UI", 10))
        self._dot.pack(side="left")
        self._label = tk.Label(
            header, text="CONNECTING", bg="#171717", fg="#f3f3f3",
            font=("Segoe UI Semibold", 9), padx=5,
        )
        self._label.pack(side="left")
        self._detail = tk.Label(
            header, text="", bg="#171717", fg="#909090", font=("Segoe UI", 8)
        )
        self._detail.pack(side="left", padx=(4, 0))
        for widget in (header, self._dot, self._label, self._detail):
            widget.bind("<ButtonPress-1>", self._start_drag)
            widget.bind("<B1-Motion>", self._drag)

        self._on = tk.Button(shell, text="On", command=lambda: self._control("on"), **self._button_style())
        self._on.grid(row=1, column=0, padx=(0, 5))
        mode_menu = tk.OptionMenu(
            shell, self._mode_var, "observe", "suggest", "collaborate",
            command=self._select_mode,
        )
        mode_menu.configure(
            bg="#242424", fg="#eeeeee", activebackground="#343434",
            activeforeground="#ffffff", relief="flat", bd=0,
            highlightthickness=0, font=("Segoe UI", 8), width=10,
        )
        mode_menu["menu"].configure(bg="#242424", fg="#eeeeee", font=("Segoe UI", 8))
        mode_menu.grid(row=1, column=1, padx=(0, 5))
        self._pause = tk.Button(shell, text="Pause", command=self._toggle_pause, **self._button_style())
        self._pause.grid(row=1, column=2, padx=(0, 5))
        self._off = tk.Button(
            shell, text="Off", command=lambda: self._control("off"),
            **self._button_style(danger=True),
        )
        self._off.grid(row=1, column=3)
        shell.columnconfigure(4, weight=1)

        self._build_suggestion_window()

        self.root.update_idletasks()
        width = self.root.winfo_reqwidth()
        x = max(12, self.root.winfo_screenwidth() - width - 22)
        self.root.geometry(f"+{x}+52")
        self.root.after(50, self._poll)

    def _build_suggestion_window(self) -> None:
        tk = self._tk
        self._suggestion_window = tk.Toplevel(self.root)
        self._suggestion_window.withdraw()
        self._suggestion_window.title(INDICATOR_WINDOW_TITLE)
        self._suggestion_window.configure(bg="#171717")
        self._suggestion_window.resizable(False, False)
        self._suggestion_window.overrideredirect(True)
        self._suggestion_window.attributes("-topmost", True)
        try:
            self._suggestion_window.attributes("-toolwindow", True)
            self._suggestion_window.attributes("-alpha", 0.96)
        except tk.TclError:
            pass
        self._suggestion_window.focusmodel("passive")
        card = tk.Frame(
            self._suggestion_window,
            bg="#1d1d1d",
            highlightbackground="#4a4a4a",
            highlightthickness=1,
            padx=14,
            pady=12,
        )
        card.pack(fill="both", expand=True)
        tk.Label(
            card,
            text="JARVIS",
            bg="#1d1d1d",
            fg="#67d8f5",
            font=("Segoe UI Semibold", 9),
        ).pack(anchor="w")
        self._suggestion_text = tk.Label(
            card,
            text="",
            bg="#1d1d1d",
            fg="#f4f4f4",
            font=("Segoe UI", 11),
            justify="left",
            wraplength=330,
            padx=0,
            pady=9,
        )
        self._suggestion_text.pack(anchor="w", fill="x")
        self._suggestion_actions = tk.Frame(card, bg="#1d1d1d")
        self._suggestion_actions.pack(anchor="e", fill="x")
        self._suggestion_dismiss = tk.Button(
            self._suggestion_actions,
            text="Dismiss",
            command=lambda: self._respond_to_suggestion(False),
            **self._button_style(),
        )
        self._suggestion_dismiss.pack(side="right")
        self._suggestion_accept = tk.Button(
            self._suggestion_actions,
            text="Do it",
            command=lambda: self._respond_to_suggestion(True),
            bg="#175f73",
            fg="#ffffff",
            activebackground="#217f98",
            activeforeground="#ffffff",
            relief="flat",
            bd=0,
            padx=12,
            pady=5,
            font=("Segoe UI Semibold", 9),
            cursor="hand2",
        )
        self._suggestion_accept.pack(side="right", padx=(0, 7))

    def _show_without_activation(self) -> None:
        window = self._suggestion_window
        window.update_idletasks()
        width = max(360, window.winfo_reqwidth())
        height = window.winfo_reqheight()
        work_area = None
        user32 = None
        if os.name == "nt":
            try:
                user32 = ctypes.WinDLL("user32", use_last_error=True)
                work_area = _foreground_work_area(user32)
            except (AttributeError, OSError):
                user32 = None
        if work_area is None:
            left, top = 0, 0
            right, bottom = window.winfo_screenwidth(), window.winfo_screenheight()
        else:
            left, top, right, bottom = work_area
        x = max(left + 12, right - width - 22)
        y = max(top + 12, bottom - height - 22)
        window.geometry(_tk_geometry(width, height, x, y))
        if os.name != "nt":
            window.deiconify()
            return
        if user32 is None or not _show_windows_no_activate(window, user32):
            window.deiconify()

    def _hide_suggestion(self) -> None:
        self._suggestion_window.withdraw()
        self._suggestion_id = None
        self._suggestion_busy = False
        self._suggestion_job_id = None
        self._suggestion_action_polling = False
        self._suggestion_action_deadline = 0.0
        if self._suggestion_timer is not None:
            try:
                self.root.after_cancel(self._suggestion_timer)
            except self._tk.TclError:
                pass
            self._suggestion_timer = None

    def _render_suggestion(self, suggestion: dict[str, Any] | None) -> None:
        if self._suggestion_busy:
            return
        if not isinstance(suggestion, dict) or float(suggestion.get("expires_at") or 0) <= time.time():
            if self._suggestion_id is not None:
                self._hide_suggestion()
            return
        suggestion_id = str(suggestion["id"])
        if suggestion_id == self._suggestion_id:
            return
        self._suggestion_id = suggestion_id
        self._suggestion_text.configure(text=str(suggestion["text"]))
        self._suggestion_dismiss.configure(
            text="Dismiss",
            command=lambda: self._respond_to_suggestion(False),
        )
        if not self._suggestion_accept.winfo_manager():
            self._suggestion_accept.pack(side="right", padx=(0, 7))
        if not self._suggestion_dismiss.winfo_manager():
            self._suggestion_dismiss.pack(side="right")
        self._suggestion_accept.configure(state="normal")
        self._suggestion_dismiss.configure(state="normal")
        self._suggestion_actions.pack(anchor="e", fill="x")
        self._show_without_activation()
        if self._suggestion_timer is not None:
            self.root.after_cancel(self._suggestion_timer)
        remaining_ms = max(
            1_000,
            min(
                int(COMPANION_SUGGESTION_TTL_SECONDS * 1_000),
                int((float(suggestion["expires_at"]) - time.time()) * 1_000),
            ),
        )
        self._suggestion_timer = self.root.after(remaining_ms, self._hide_suggestion)

    def _button_style(self, *, danger: bool = False) -> dict[str, Any]:
        return {
            "bg": "#2b2021" if danger else "#242424",
            "fg": "#ffb2b6" if danger else "#eeeeee",
            "activebackground": "#4a292b" if danger else "#343434",
            "activeforeground": "#ffffff",
            "relief": "flat",
            "bd": 0,
            "padx": 9,
            "pady": 4,
            "font": ("Segoe UI", 8),
            "cursor": "hand2",
        }

    def _start_drag(self, event: Any) -> None:
        self._drag_origin = (
            int(event.x_root), int(event.y_root), self.root.winfo_x(), self.root.winfo_y()
        )

    def _drag(self, event: Any) -> None:
        if self._drag_origin is None:
            return
        start_x, start_y, window_x, window_y = self._drag_origin
        self.root.geometry(
            f"+{window_x + int(event.x_root) - start_x}"
            f"+{window_y + int(event.y_root) - start_y}"
        )

    def _render(self, state: dict[str, Any] | None) -> None:
        self._state = state
        view = indicator_presentation(state)
        self._dot.configure(fg=view.color)
        self._label.configure(text=view.label)
        self._detail.configure(text=view.detail)
        if view.mode != "disabled":
            self._mode_var.set(view.mode)
        usable = view.online and not self._busy
        active = usable and view.mode != "disabled"
        self._on.configure(state="disabled" if active and not view.paused else "normal" if usable else "disabled")
        self._pause.configure(
            text="Resume" if view.paused and view.mode != "disabled" else "Pause",
            state="normal" if active else "disabled",
        )
        self._off.configure(state="normal" if usable and view.mode != "disabled" else "disabled")
        if indicator_should_be_visible(state):
            self.root.deiconify()
            self._render_suggestion(
                state.get("suggestion") if isinstance(state, dict) else None
            )
        else:
            self.root.withdraw()
            self._hide_suggestion()

    def _poll(self) -> None:
        if not _parent_is_alive(self.parent_pid):
            self.root.destroy()
            return
        if not self._busy and not self._polling:
            self._polling = True

            def perform() -> None:
                try:
                    state = self.client.status()
                except (
                    OSError,
                    RuntimeError,
                    ValueError,
                    urllib.error.URLError,
                    json.JSONDecodeError,
                ):
                    state = None
                self.root.after(0, lambda: self._finish_poll(state))

            threading.Thread(
                target=perform,
                name="jarvis-indicator-status",
                daemon=True,
            ).start()
        self.root.after(1_000, self._poll)

    def _finish_poll(self, state: dict[str, Any] | None) -> None:
        self._polling = False
        if not self._busy:
            self._render(state)

    def _respond_to_suggestion(self, accept: bool) -> None:
        if self._suggestion_busy or self._suggestion_id is None:
            return
        suggestion_id = self._suggestion_id
        self._suggestion_busy = True
        self._suggestion_accept.configure(state="disabled")
        self._suggestion_dismiss.configure(state="disabled")
        if self._suggestion_timer is not None:
            try:
                self.root.after_cancel(self._suggestion_timer)
            except self._tk.TclError:
                pass
            self._suggestion_timer = None

        def perform() -> None:
            try:
                result = self.client.respond_suggestion(
                    suggestion_id,
                    accept=accept,
                )
            except (
                OSError,
                RuntimeError,
                ValueError,
                urllib.error.URLError,
                json.JSONDecodeError,
            ):
                result = None
            self.root.after(
                0,
                lambda: self._finish_suggestion_response(accept, result),
            )

        threading.Thread(
            target=perform,
            name="jarvis-indicator-suggestion",
            daemon=True,
        ).start()

    def _finish_suggestion_response(
        self,
        accept: bool,
        result: dict[str, Any] | None,
    ) -> None:
        if result is None:
            self._suggestion_text.configure(text="That suggestion expired. I won’t do anything.")
            self._suggestion_timer = self.root.after(2_500, self._hide_suggestion)
            return
        if not accept:
            self._hide_suggestion()
            return
        job_id = str(result.get("job_id") or "").strip().casefold()
        if re.fullmatch(r"[0-9a-f]{32}", job_id) is None:
            self._suggestion_text.configure(
                text="I couldn't start that. Nothing was changed."
            )
            self._show_suggestion_close_button()
            return
        self._suggestion_job_id = job_id
        self._suggestion_action_deadline = time.time() + 180.0
        self._suggestion_text.configure(text="Working on it…")
        self._suggestion_actions.pack_forget()
        self._suggestion_timer = self.root.after(250, self._poll_suggestion_action)

    def _show_suggestion_close_button(self) -> None:
        self._suggestion_accept.pack_forget()
        self._suggestion_dismiss.configure(
            text="Close",
            state="normal",
            command=self._hide_suggestion,
        )
        if not self._suggestion_dismiss.winfo_manager():
            self._suggestion_dismiss.pack(side="right")
        self._suggestion_actions.pack(anchor="e", fill="x")
        self._show_without_activation()
        self._suggestion_timer = self.root.after(15_000, self._hide_suggestion)

    def _poll_suggestion_action(self) -> None:
        self._suggestion_timer = None
        job_id = self._suggestion_job_id
        if job_id is None or self._suggestion_action_polling:
            return
        self._suggestion_action_polling = True

        def perform() -> None:
            try:
                status = self.client.action_status(job_id)
            except (
                OSError,
                RuntimeError,
                ValueError,
                urllib.error.URLError,
                json.JSONDecodeError,
            ):
                status = None
            self.root.after(
                0,
                lambda: self._finish_suggestion_action_poll(job_id, status),
            )

        threading.Thread(
            target=perform,
            name="jarvis-indicator-action-status",
            daemon=True,
        ).start()

    def _finish_suggestion_action_poll(
        self,
        job_id: str,
        status: dict[str, Any] | None,
    ) -> None:
        self._suggestion_action_polling = False
        if job_id != self._suggestion_job_id:
            return
        if status is None:
            if time.time() < self._suggestion_action_deadline:
                self._suggestion_text.configure(text="Still working…")
                self._suggestion_timer = self.root.after(
                    1_000, self._poll_suggestion_action
                )
                return
            self._suggestion_text.configure(
                text="I lost the live result. Open Jarvis to check the request."
            )
            self._show_suggestion_close_button()
            return
        self._suggestion_text.configure(text=str(status["message"]))
        self._show_without_activation()
        if bool(status["terminal"]):
            self._show_suggestion_close_button()
            return
        self._suggestion_timer = self.root.after(750, self._poll_suggestion_action)

    def _select_mode(self, selected: str) -> None:
        if self._state is not None and selected != self._state.get("mode"):
            self._control("mode", mode=selected)

    def _toggle_pause(self) -> None:
        if not isinstance(self._state, dict):
            return
        self._control("resume" if self._state.get("paused") else "pause")

    def _control(self, action: str, *, mode: str | None = None) -> None:
        if self._busy:
            return
        self._busy = True
        self._render(self._state)

        def perform() -> None:
            try:
                changed = self.client.control(action, mode=mode)
            except (OSError, RuntimeError, ValueError, urllib.error.URLError, json.JSONDecodeError):
                changed = None
            self.root.after(0, lambda: self._finish_control(changed))

        threading.Thread(target=perform, name="jarvis-indicator-control", daemon=True).start()

    def _finish_control(self, state: dict[str, Any] | None) -> None:
        self._busy = False
        self._render(state)

    def run(self) -> None:
        self.root.mainloop()


def start_indicator_process(host: str, port: int) -> subprocess.Popen[bytes] | None:
    if os.name != "nt":
        return None
    executable = Path(sys.executable)
    pythonw = executable.with_name("pythonw.exe")
    command = [
        str(pythonw if pythonw.is_file() else executable),
        "-m", "jarvis.companion_indicator",
        "--host", str(host),
        "--port", str(port),
        "--parent-pid", str(os.getpid()),
    ]
    try:
        return subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except OSError:
        return None


def stop_indicator_process(process: subprocess.Popen[bytes] | None) -> None:
    if process is None or process.poll() is not None:
        return
    try:
        process.terminate()
        process.wait(timeout=3)
    except (OSError, subprocess.SubprocessError):
        try:
            process.kill()
        except OSError:
            pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jarvis-companion-indicator")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--parent-pid", type=int, required=True)
    args = parser.parse_args(argv)
    if os.name != "nt" or not _parent_is_alive(args.parent_pid):
        return 0
    mutex = _claim_single_instance()
    if mutex is None:
        return 0
    try:
        client = CompanionIndicatorClient(args.host, args.port)
        CompanionIndicatorApp(client, args.parent_pid).run()
    except (ImportError, RuntimeError, ValueError):
        return 1
    finally:
        if os.name == "nt" and mutex not in {None, 1}:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
            kernel32.CloseHandle(ctypes.c_void_p(mutex))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
