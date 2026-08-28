from __future__ import annotations

import ctypes
import json
import os
import platform
import stat
import subprocess
import threading
import time
from ctypes import wintypes
from pathlib import Path
from typing import Any

from .redaction import contains_secret
from .screen_companion import DEFAULT_EXCLUDED_APPS, WindowsForegroundProvider


_PROTECTED_COMPONENTS = frozenset({
    ".aws", ".azure", ".git", ".gnupg", ".jarvis-skills", ".kube", ".ssh",
    "codex-cli-home", "credentialmanager", "credentials", "gateway", "secrets", "vault",
})
_PROTECTED_FILENAMES = frozenset({
    ".npmrc", ".pypirc", "constitution.md", "soul.md", "cookies", "credentials",
    "evaluation-cases.json", "evaluation-cases.jsonl",
    "evaluation_cases.json", "evaluation_cases.jsonl",
    "id_dsa", "id_ecdsa", "id_ed25519", "id_rsa", "login data",
    "policy.py", "promotion-gate.json", "promotion_gate.json", "tokens.json",
    "web data",
})
_WINDOWS_DEVICES = frozenset({
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
})
_STORAGE_CACHE_LOCK = threading.Lock()
_STORAGE_CACHE_AT = 0.0
_STORAGE_CACHE: list[dict[str, Any]] | None = None


def _protected_part(part: str) -> bool:
    folded = part.rstrip(" .").casefold()
    stem = folded.split(".", 1)[0].upper()
    return (
        folded in _PROTECTED_COMPONENTS
        or folded in _PROTECTED_FILENAMES
        or folded == ".env"
        or folded.startswith(".env.")
        or stem in _WINDOWS_DEVICES
    )


def resolve_computer_path(root: Path, user_path: str | Path) -> Path:
    """Resolve a user-profile path without following credential or link escapes."""
    raw_text = os.fspath(user_path)
    if not raw_text or "\x00" in raw_text or any(ord(char) < 32 for char in raw_text):
        raise PermissionError("Invalid computer path")
    if raw_text.startswith(("\\\\", "//", "\\\\.\\", "\\\\?\\")):
        raise PermissionError("Network shares and Windows device paths are blocked")
    drive, tail = os.path.splitdrive(raw_text)
    if ":" in tail:
        raise PermissionError("Alternate data streams are blocked")
    root = root.resolve()
    raw = Path(raw_text)
    candidate = (root / raw).resolve() if not raw.is_absolute() else raw.resolve()
    try:
        relative = candidate.relative_to(root)
    except ValueError as exc:
        raise PermissionError(f"Computer access is limited to {root}") from exc
    current = root
    for part in relative.parts:
        if _protected_part(part):
            raise PermissionError("Credential, secret, and runtime-control paths are protected")
        current = current / part
        if not os.path.lexists(current):
            continue
        details = os.lstat(current)
        attributes = getattr(details, "st_file_attributes", 0)
        if (
            stat.S_ISLNK(details.st_mode)
            or attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        ):
            raise PermissionError("Links and reparse points are blocked in computer paths")
    return candidate


class _MemoryStatus(ctypes.Structure):
    _fields_ = [
        ("dwLength", wintypes.DWORD),
        ("dwMemoryLoad", wintypes.DWORD),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


def _memory_status() -> dict[str, int] | None:
    if os.name != "nt":
        return None
    status = _MemoryStatus()
    status.dwLength = ctypes.sizeof(status)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        return None
    return {
        "load_percent": int(status.dwMemoryLoad),
        "total_bytes": int(status.ullTotalPhys),
        "available_bytes": int(status.ullAvailPhys),
    }


def _filetime_value(value: wintypes.FILETIME) -> int:
    return (int(value.dwHighDateTime) << 32) | int(value.dwLowDateTime)


def _cpu_sample() -> tuple[int, int] | None:
    if os.name != "nt":
        return None
    idle = wintypes.FILETIME()
    kernel = wintypes.FILETIME()
    user = wintypes.FILETIME()
    if not ctypes.windll.kernel32.GetSystemTimes(
        ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)
    ):
        return None
    return _filetime_value(idle), _filetime_value(kernel) + _filetime_value(user)


def _cpu_percent() -> float | None:
    first = _cpu_sample()
    if first is None:
        return None
    time.sleep(0.05)
    second = _cpu_sample()
    if second is None:
        return None
    idle_delta = second[0] - first[0]
    total_delta = second[1] - first[1]
    if total_delta <= 0:
        return None
    return round(max(0.0, min(100.0, 100.0 * (1.0 - idle_delta / total_delta))), 1)


def _windows_directory() -> Path | None:
    if os.name != "nt":
        return None
    buffer = ctypes.create_unicode_buffer(32_768)
    length = ctypes.windll.kernel32.GetWindowsDirectoryW(buffer, len(buffer))
    if length <= 0 or length >= len(buffer):
        return None
    return Path(buffer.value)


def _physical_storage_devices() -> list[dict[str, Any]] | None:
    """Return bounded physical-disk metadata using a fixed, non-interactive query."""
    global _STORAGE_CACHE_AT, _STORAGE_CACHE
    if os.name != "nt":
        return None
    current = time.monotonic()
    with _STORAGE_CACHE_LOCK:
        if _STORAGE_CACHE is not None and current - _STORAGE_CACHE_AT < 60:
            return [dict(item) for item in _STORAGE_CACHE]
    windows = _windows_directory()
    powershell = (
        windows / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
        if windows is not None else None
    )
    if powershell is None or not powershell.is_file():
        return None
    command = (
        "$ErrorActionPreference='Stop';"
        "[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false);"
        "@(Get-CimInstance -ClassName Win32_DiskDrive | "
        "Select-Object Index,Model,MediaType,InterfaceType,Size,Status) | "
        "ConvertTo-Json -Compress"
    )
    try:
        completed = subprocess.run(
            [
                str(powershell), "-NoLogo", "-NoProfile", "-NonInteractive",
                "-Command", command,
            ],
            capture_output=True,
            check=False,
            timeout=8,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0 or len(completed.stdout) > 131_072:
        return None
    try:
        decoded = completed.stdout.decode("utf-8-sig", errors="strict").strip()
        raw = json.loads(decoded or "[]")
    except (UnicodeError, ValueError, json.JSONDecodeError):
        return None
    rows = raw if isinstance(raw, list) else [raw]
    devices: list[dict[str, Any]] = []

    def metadata(value: Any, limit: int) -> str:
        return " ".join(str(value or "Unknown").split())[:limit]

    for row in rows[:32]:
        if not isinstance(row, dict):
            continue
        try:
            index = int(row.get("Index"))
            size = max(0, int(row.get("Size") or 0))
        except (TypeError, ValueError, OverflowError):
            continue
        devices.append({
            "disk_number": index,
            "model": metadata(row.get("Model"), 160),
            "media_type": metadata(row.get("MediaType"), 80),
            "interface_type": metadata(row.get("InterfaceType"), 40),
            "size_bytes": size,
            "status": metadata(row.get("Status"), 40),
        })
    devices.sort(key=lambda item: int(item["disk_number"]))
    with _STORAGE_CACHE_LOCK:
        _STORAGE_CACHE_AT = current
        _STORAGE_CACHE = [dict(item) for item in devices]
    return devices


def system_snapshot(root: Path) -> dict[str, Any]:
    root = root.resolve()
    disk = os.statvfs(root) if os.name != "nt" else None
    if disk is None:
        import shutil
        disk_usage = shutil.disk_usage(root)
        disk_result = {
            "path": str(root),
            "total_bytes": disk_usage.total,
            "used_bytes": disk_usage.used,
            "free_bytes": disk_usage.free,
        }
    else:
        total = disk.f_blocks * disk.f_frsize
        free = disk.f_bavail * disk.f_frsize
        disk_result = {
            "path": str(root), "total_bytes": total,
            "used_bytes": total - free, "free_bytes": free,
        }
    physical_storage = _physical_storage_devices()
    return {
        "timestamp": time.time(),
        "computer": platform.node(),
        "operating_system": platform.platform(),
        "processor": platform.processor(),
        "logical_cpu_count": os.cpu_count(),
        "cpu_percent": _cpu_percent(),
        "memory": _memory_status(),
        "disk": disk_result,
        "physical_storage": {
            "available": physical_storage is not None,
            "device_count": len(physical_storage or []),
            "devices": physical_storage or [],
        },
    }


def open_windows_applications(limit: int = 50) -> dict[str, Any]:
    """Return executable names that own visible top-level Windows windows.

    This intentionally does not call ``GetWindowText`` or capture pixels.  The
    result is system-status metadata only: bounded executable basenames, with
    duplicate windows collapsed to one application name.
    """
    bounded_limit = max(1, min(int(limit), 100))
    if os.name != "nt":
        return {
            "available": False,
            "platform": os.name,
            "applications": [],
            "count": 0,
            "truncated": False,
            "source": "visible_top_level_windows",
            "window_titles_read": False,
            "window_content_read": False,
            "reason": "Visible Windows application enumeration is unavailable on this platform.",
        }

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    process_query_limited_information = 0x1000
    max_discovered = 512
    names: dict[str, str] = {}
    ignored_shell_processes = {
        "dwm.exe",
        "searchhost.exe",
        "shellexperiencehost.exe",
        "startmenuexperiencehost.exe",
        "textinputhost.exe",
    }
    ignored_shell_window_classes = {
        "notifyiconoverflowwindow",
        "progman",
        "shell_secondarytraywnd",
        "shell_traywnd",
        "workerw",
    }

    def process_name(process_id: int) -> str | None:
        handle = kernel32.OpenProcess(
            process_query_limited_information, False, int(process_id)
        )
        if not handle:
            return None
        try:
            buffer = ctypes.create_unicode_buffer(32_768)
            size = wintypes.DWORD(len(buffer))
            if not kernel32.QueryFullProcessImageNameW(
                handle, 0, buffer, ctypes.byref(size)
            ):
                return None
            name = Path(buffer.value).name.strip()
            if (
                not name
                or len(name) > 260
                or any(ord(character) < 32 for character in name)
                or name.casefold() in ignored_shell_processes
            ):
                return None
            return name
        finally:
            kernel32.CloseHandle(handle)

    callback_type = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HWND, wintypes.LPARAM
    )

    @callback_type
    def visit(window: int, _parameter: int) -> bool:
        if len(names) >= max_discovered or not user32.IsWindowVisible(window):
            return True
        class_name = ctypes.create_unicode_buffer(256)
        if user32.GetClassNameW(window, class_name, len(class_name)) and (
            class_name.value.casefold() in ignored_shell_window_classes
        ):
            return True
        rectangle = wintypes.RECT()
        if not user32.GetWindowRect(window, ctypes.byref(rectangle)):
            return True
        if rectangle.right <= rectangle.left or rectangle.bottom <= rectangle.top:
            return True
        process_id = wintypes.DWORD()
        user32.GetWindowThreadProcessId(window, ctypes.byref(process_id))
        if not process_id.value:
            return True
        name = process_name(int(process_id.value))
        if name is not None:
            names.setdefault(name.casefold(), name)
        return True

    try:
        user32.EnumWindows(visit, 0)
    except (AttributeError, OSError, TypeError, ValueError):
        return {
            "available": False,
            "platform": "windows",
            "applications": [],
            "count": 0,
            "truncated": False,
            "source": "visible_top_level_windows",
            "window_titles_read": False,
            "window_content_read": False,
            "reason": "Windows did not provide a visible-application inventory.",
        }

    ordered = sorted(names.values(), key=str.casefold)
    return {
        "available": True,
        "platform": "windows",
        "applications": [{"name": name} for name in ordered[:bounded_limit]],
        "count": min(len(ordered), bounded_limit),
        "truncated": len(ordered) > bounded_limit,
        "source": "visible_top_level_windows",
        "window_titles_read": False,
        "window_content_read": False,
        "observed_at": time.time(),
    }


class WindowsDesktopController:
    """Bounded foreground input with a fresh context check before every action."""

    _KEYS = {
        "backspace": 0x08,
        "tab": 0x09,
        "enter": 0x0D,
        "shift": 0x10,
        "ctrl": 0x11,
        "alt": 0x12,
        "escape": 0x1B,
        "space": 0x20,
        "pageup": 0x21,
        "pagedown": 0x22,
        "end": 0x23,
        "home": 0x24,
        "left": 0x25,
        "up": 0x26,
        "right": 0x27,
        "down": 0x28,
        "delete": 0x2E,
        **{f"f{index}": 0x6F + index for index in range(1, 13)},
    }

    def __init__(self, provider: Any | None = None) -> None:
        self.provider = provider or WindowsForegroundProvider()
        self.available = bool(getattr(self.provider, "available", os.name == "nt"))
        self._user32 = ctypes.windll.user32 if os.name == "nt" else None

    def snapshot(self) -> dict[str, Any]:
        if not self.available:
            raise RuntimeError("Windows desktop control is unavailable")
        context = self.provider.active_context(
            excluded_apps=set(DEFAULT_EXCLUDED_APPS)
        )
        if context is None:
            raise RuntimeError("No foreground window is available")
        public = {
            key: context[key]
            for key in (
                "application", "title", "left", "top", "right", "bottom",
                "width", "height", "context_sha256", "excluded",
                "exclusion_reason",
            )
        }
        return public

    def _verified_context(self, expected_context_sha256: str) -> dict[str, Any]:
        expected = str(expected_context_sha256).strip().casefold()
        if len(expected) != 64 or any(char not in "0123456789abcdef" for char in expected):
            raise ValueError("expected_context_sha256 must be a SHA-256 digest")
        context = self.provider.active_context(
            excluded_apps=set(DEFAULT_EXCLUDED_APPS)
        )
        if context is None:
            raise RuntimeError("No foreground window is available")
        if context.get("excluded"):
            raise PermissionError("The foreground window is sensitive or excluded")
        if str(context.get("context_sha256")) != expected:
            raise PermissionError(
                "The foreground window changed; inspect it again before controlling it"
            )
        return context

    @classmethod
    def _virtual_key(cls, value: Any) -> int:
        key = str(value or "").strip().casefold()
        if len(key) == 1 and (key.isascii() and key.isalnum()):
            return ord(key.upper())
        if key not in cls._KEYS:
            raise ValueError(f"Unsupported desktop key: {key or '<empty>'}")
        return cls._KEYS[key]

    def _key_event(self, key: int, key_up: bool = False) -> None:
        if self._user32 is None:
            raise RuntimeError("Windows input is unavailable")
        self._user32.keybd_event(key, 0, 0x0002 if key_up else 0, 0)

    def _hotkey(self, keys: list[Any]) -> None:
        if not isinstance(keys, list) or not 1 <= len(keys) <= 4:
            raise ValueError("hotkey keys must contain 1-4 entries")
        codes = [self._virtual_key(key) for key in keys]
        for code in codes:
            self._key_event(code)
        for code in reversed(codes):
            self._key_event(code, True)

    def _unicode_unit(self, unit: int, key_up: bool = False) -> None:
        if self._user32 is None:
            raise RuntimeError("Windows input is unavailable")

        class _KeyboardInput(ctypes.Structure):
            _fields_ = [
                ("wVk", wintypes.WORD),
                ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.c_size_t),
            ]

        class _MouseInput(ctypes.Structure):
            _fields_ = [
                ("dx", wintypes.LONG),
                ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.c_size_t),
            ]

        class _HardwareInput(ctypes.Structure):
            _fields_ = [
                ("uMsg", wintypes.DWORD),
                ("wParamL", wintypes.WORD),
                ("wParamH", wintypes.WORD),
            ]

        class _InputUnion(ctypes.Union):
            _fields_ = [
                ("mi", _MouseInput),
                ("ki", _KeyboardInput),
                ("hi", _HardwareInput),
            ]

        class _Input(ctypes.Structure):
            _fields_ = [("type", wintypes.DWORD), ("union", _InputUnion)]

        event = _Input(
            type=1,
            union=_InputUnion(
                ki=_KeyboardInput(
                    0,
                    int(unit),
                    0x0004 | (0x0002 if key_up else 0),
                    0,
                    0,
                )
            ),
        )
        if self._user32.SendInput(1, ctypes.byref(event), ctypes.sizeof(event)) != 1:
            raise OSError("Windows rejected synthetic text input")

    def _type_text(self, text: Any) -> None:
        value = str(text)
        if not value or len(value) > 2_000:
            raise ValueError("desktop text must contain 1-2000 characters")
        if contains_secret(value):
            raise PermissionError("Potential credentials or secrets cannot be typed by Jarvis")
        for offset in range(0, len(value.encode("utf-16-le")), 2):
            unit = int.from_bytes(value.encode("utf-16-le")[offset:offset + 2], "little")
            self._unicode_unit(unit)
            self._unicode_unit(unit, True)

    def _click(self, context: dict[str, Any], x: Any, y: Any) -> None:
        if (
            isinstance(x, bool) or isinstance(y, bool)
            or not isinstance(x, int) or not isinstance(y, int)
        ):
            raise TypeError("click coordinates must be integers")
        if not 0 <= x < int(context["width"]) or not 0 <= y < int(context["height"]):
            raise ValueError("click coordinates must be inside the foreground window")
        if self._user32 is None:
            raise RuntimeError("Windows input is unavailable")
        absolute_x = int(context["left"]) + x
        absolute_y = int(context["top"]) + y
        if not self._user32.SetCursorPos(absolute_x, absolute_y):
            raise OSError("Windows rejected the cursor position")
        self._user32.mouse_event(0x0002, 0, 0, 0, 0)
        self._user32.mouse_event(0x0004, 0, 0, 0, 0)

    def _scroll(self, delta: Any) -> None:
        if isinstance(delta, bool) or not isinstance(delta, int) or not -1_200 <= delta <= 1_200:
            raise ValueError("scroll delta must be an integer from -1200 to 1200")
        if delta == 0:
            raise ValueError("scroll delta cannot be zero")
        if self._user32 is None:
            raise RuntimeError("Windows input is unavailable")
        self._user32.mouse_event(0x0800, 0, 0, ctypes.c_uint32(delta).value, 0)

    def validate_actions(
        self,
        actions: list[dict[str, Any]],
        *,
        context: dict[str, Any] | None = None,
    ) -> None:
        if not isinstance(actions, list) or not 1 <= len(actions) <= 12:
            raise ValueError("desktop actions must contain 1-12 entries")
        allowed = {
            "click": {"type", "x", "y"},
            "type_text": {"type", "text"},
            "hotkey": {"type", "keys"},
            "scroll": {"type", "delta"},
        }
        for action in actions:
            if not isinstance(action, dict):
                raise TypeError("Every desktop action must be an object")
            kind = str(action.get("type") or "").strip().casefold()
            if kind not in allowed:
                raise ValueError(
                    "desktop action type must be click, type_text, hotkey, or scroll"
                )
            if set(action) != allowed[kind]:
                raise ValueError(f"desktop {kind} action has missing or unknown fields")
            if kind == "click":
                x, y = action["x"], action["y"]
                if (
                    isinstance(x, bool) or isinstance(y, bool)
                    or not isinstance(x, int) or not isinstance(y, int)
                ):
                    raise TypeError("click coordinates must be integers")
                if context is not None and (
                    not 0 <= x < int(context["width"])
                    or not 0 <= y < int(context["height"])
                ):
                    raise ValueError("click coordinates must be inside the foreground window")
            elif kind == "type_text":
                value = str(action["text"])
                if not value or len(value) > 2_000:
                    raise ValueError("desktop text must contain 1-2000 characters")
                if contains_secret(value):
                    raise PermissionError(
                        "Potential credentials or secrets cannot be typed by Jarvis"
                    )
            elif kind == "hotkey":
                keys = action["keys"]
                if not isinstance(keys, list) or not 1 <= len(keys) <= 4:
                    raise ValueError("hotkey keys must contain 1-4 entries")
                for key in keys:
                    self._virtual_key(key)
            else:
                delta = action["delta"]
                if (
                    isinstance(delta, bool) or not isinstance(delta, int)
                    or delta == 0 or not -1_200 <= delta <= 1_200
                ):
                    raise ValueError(
                        "scroll delta must be a nonzero integer from -1200 to 1200"
                    )

    def interact(
        self,
        *,
        expected_context_sha256: str,
        actions: list[dict[str, Any]],
    ) -> dict[str, Any]:
        self.validate_actions(actions)
        completed = 0
        for action in actions:
            if not isinstance(action, dict):
                raise TypeError("Every desktop action must be an object")
            kind = str(action.get("type") or "").strip().casefold()
            allowed = {
                "click": {"type", "x", "y"},
                "type_text": {"type", "text"},
                "hotkey": {"type", "keys"},
                "scroll": {"type", "delta"},
            }
            if kind not in allowed:
                raise ValueError("desktop action type must be click, type_text, hotkey, or scroll")
            if set(action) != allowed[kind]:
                raise ValueError(f"desktop {kind} action has missing or unknown fields")
            context = self._verified_context(expected_context_sha256)
            if kind == "click":
                self._click(context, action["x"], action["y"])
            elif kind == "type_text":
                self._type_text(action["text"])
            elif kind == "hotkey":
                self._hotkey(action["keys"])
            else:
                self._scroll(action["delta"])
            completed += 1
        return {
            "completed_actions": completed,
            "foreground_application": context["application"],
            "context_sha256": expected_context_sha256,
            "verified_before_each_action": True,
        }
