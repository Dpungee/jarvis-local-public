"""Windows desktop integration for JARVIS Desktop.

Notifications through a tray balloon (the only notification mechanism), an
optional tray icon with a small menu, screen-region capture, the snipping
overlay plus clipboard polling.

Rules the module keeps:

* stdlib + ``ctypes`` only; no third-party packages, and no subprocesses:
  nothing in this module ever spawns PowerShell or any other process.
* Imports cleanly on every platform. Every Win32 entry point is reached only
  through :func:`_api`, which returns ``None`` off Windows or when the DLLs
  cannot be loaded, and every public function then degrades to
  ``False`` / ``None`` / ``0`` / ``"unsupported"``.
* Own ``ctypes.WinDLL`` instances are used instead of ``ctypes.windll`` so the
  ``argtypes`` set here never clobber the ones ``jarvis.ui`` sets on the shared
  ``ctypes.windll.user32`` object.

Helpers from :mod:`jarvis.ui` (``encode_png``, ``clipboard_image_png``) are
imported lazily inside functions; a top-level import would be circular. The
DIB-to-PNG conversion in ``jarvis.ui`` is inline in ``clipboard_image_png``
and not reusable on its own, so :func:`dib_to_png` here is a small standalone
equivalent for packed 24/32-bit DIBs.
"""

from __future__ import annotations

import atexit
import collections
import ctypes
import os
import queue
import struct
import sys
import threading
import time
import zlib
from ctypes import wintypes
from typing import Any, Callable

__all__ = [
    "MAX_NOTIFY_BODY",
    "MAX_NOTIFY_TITLE",
    "TrayIcon",
    "bound_text",
    "capture_screen_region",
    "clipboard_sequence",
    "dib_to_png",
    "launch_snip",
    "notify",
    "wait_for_clipboard_image",
]

# ---------------------------------------------------------------------------
# Limits and constants
# ---------------------------------------------------------------------------

MAX_NOTIFY_TITLE = 64
MAX_NOTIFY_BODY = 200
MAX_CAPTURE_PIXELS = 40_000_000

# Window messages and shell constants.
WM_NULL = 0x0000
WM_DESTROY = 0x0002
WM_CLOSE = 0x0010
WM_COMMAND = 0x0111
WM_CONTEXTMENU = 0x007B
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205
WM_USER = 0x0400
WM_APP = 0x8000
WM_TRAY_CALLBACK = WM_APP + 1
WM_TRAY_BALLOON = WM_APP + 2
NIN_SELECT = WM_USER + 0
NIN_KEYSELECT = WM_USER + 1
NIN_BALLOONUSERCLICK = WM_USER + 5

NIM_ADD = 0x0
NIM_MODIFY = 0x1
NIM_DELETE = 0x2
NIF_MESSAGE = 0x01
NIF_ICON = 0x02
NIF_TIP = 0x04
NIF_INFO = 0x10
NIIF_INFO = 0x01

IDI_APPLICATION = 32512
WS_OVERLAPPED = 0x00000000

MF_STRING = 0x0000
MF_SEPARATOR = 0x0800
TPM_LEFTALIGN = 0x0000
TPM_RIGHTBUTTON = 0x0002
TPM_BOTTOMALIGN = 0x0020
TPM_RETURNCMD = 0x0100

SRCCOPY = 0x00CC0020
DIB_RGB_COLORS = 0
BI_RGB = 0
BI_BITFIELDS = 3


# ---------------------------------------------------------------------------
# Structures (plain ctypes, safe to define on every platform)
# ---------------------------------------------------------------------------


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 1)]


class WNDCLASSW(ctypes.Structure):
    # lpfnWndProc is stored as a raw pointer so the structure can be declared
    # without ctypes.WINFUNCTYPE (which only exists on Windows).
    _fields_ = [
        ("style", wintypes.UINT),
        ("lpfnWndProc", ctypes.c_void_p),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT),
        ("hIcon", wintypes.HICON),
        ("szTip", wintypes.WCHAR * 128),
        ("dwState", wintypes.DWORD),
        ("dwStateMask", wintypes.DWORD),
        ("szInfo", wintypes.WCHAR * 256),
        ("uTimeout", wintypes.UINT),  # union with uVersion
        ("szInfoTitle", wintypes.WCHAR * 64),
        ("dwInfoFlags", wintypes.DWORD),
        ("guidItem", ctypes.c_byte * 16),
        ("hBalloonIcon", wintypes.HICON),
    ]


# ---------------------------------------------------------------------------
# Win32 access (lazy, cached, None off Windows)
# ---------------------------------------------------------------------------


class _Win32Api:
    """Own DLL handles with argtypes/restype set for x64 correctness."""

    def __init__(self) -> None:
        self.wt = wintypes
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        self.WNDPROC = ctypes.WINFUNCTYPE(wintypes.LPARAM, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
        self._prototype()

    def _prototype(self) -> None:
        u, g, k, s = self.user32, self.gdi32, self.kernel32, self.shell32
        HWND, HDC, UINT, DWORD, BOOL, INT = wintypes.HWND, wintypes.HDC, wintypes.UINT, wintypes.DWORD, wintypes.BOOL, ctypes.c_int
        HANDLE, LPCWSTR, LPARAM, WPARAM = wintypes.HANDLE, wintypes.LPCWSTR, wintypes.LPARAM, wintypes.WPARAM

        def sig(fn: Any, restype: Any, *argtypes: Any) -> None:
            fn.restype = restype
            fn.argtypes = argtypes

        # Capture.
        sig(u.GetDC, HDC, HWND)
        sig(u.ReleaseDC, INT, HWND, HDC)
        sig(g.CreateCompatibleDC, HDC, HDC)
        sig(g.DeleteDC, BOOL, HDC)
        sig(g.CreateDIBSection, wintypes.HBITMAP, HDC, ctypes.POINTER(BITMAPINFO), UINT, ctypes.POINTER(ctypes.c_void_p), HANDLE, DWORD)
        sig(g.SelectObject, wintypes.HGDIOBJ, HDC, wintypes.HGDIOBJ)
        sig(g.DeleteObject, BOOL, wintypes.HGDIOBJ)
        sig(g.BitBlt, BOOL, HDC, INT, INT, INT, INT, HDC, INT, INT, DWORD)
        sig(g.GdiFlush, BOOL)

        # Clipboard.
        sig(u.GetClipboardSequenceNumber, DWORD)

        # Tray icon plumbing.
        sig(k.GetModuleHandleW, wintypes.HMODULE, LPCWSTR)
        sig(u.LoadIconW, wintypes.HICON, wintypes.HINSTANCE, ctypes.c_void_p)
        sig(u.RegisterClassW, wintypes.ATOM, ctypes.POINTER(WNDCLASSW))
        sig(u.UnregisterClassW, BOOL, LPCWSTR, wintypes.HINSTANCE)
        sig(u.CreateWindowExW, HWND, DWORD, LPCWSTR, LPCWSTR, DWORD, INT, INT, INT, INT, HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID)
        sig(u.DestroyWindow, BOOL, HWND)
        sig(u.DefWindowProcW, LPARAM, HWND, UINT, WPARAM, LPARAM)
        sig(u.GetMessageW, INT, ctypes.POINTER(wintypes.MSG), HWND, UINT, UINT)
        sig(u.TranslateMessage, BOOL, ctypes.POINTER(wintypes.MSG))
        sig(u.DispatchMessageW, LPARAM, ctypes.POINTER(wintypes.MSG))
        sig(u.PostMessageW, BOOL, HWND, UINT, WPARAM, LPARAM)
        sig(u.PostQuitMessage, None, INT)
        sig(u.RegisterWindowMessageW, UINT, LPCWSTR)
        sig(u.CreatePopupMenu, wintypes.HMENU)
        sig(u.DestroyMenu, BOOL, wintypes.HMENU)
        sig(u.AppendMenuW, BOOL, wintypes.HMENU, UINT, ctypes.c_size_t, LPCWSTR)
        sig(u.TrackPopupMenu, INT, wintypes.HMENU, UINT, INT, INT, INT, HWND, ctypes.c_void_p)
        sig(u.SetForegroundWindow, BOOL, HWND)
        sig(u.GetCursorPos, BOOL, ctypes.POINTER(wintypes.POINT))
        sig(s.Shell_NotifyIconW, BOOL, DWORD, ctypes.POINTER(NOTIFYICONDATAW))


_API: _Win32Api | None = None
_API_FAILED = False
_API_LOCK = threading.Lock()


def _api() -> _Win32Api | None:
    """Return the cached Win32 bindings, or None off Windows / when unavailable."""
    global _API, _API_FAILED
    if sys.platform != "win32":
        return None
    if _API is not None:
        return _API
    if _API_FAILED:
        return None
    with _API_LOCK:
        if _API is None and not _API_FAILED:
            try:
                _API = _Win32Api()
            except Exception:
                _API_FAILED = True
                return None
    return _API


# ---------------------------------------------------------------------------
# Text safety helpers
# ---------------------------------------------------------------------------


def bound_text(text: Any, limit: int, *, ellipsis: str = "\u2026") -> str:
    """Normalise text for display: drop control characters and lone surrogates,
    fold CR/LF to ``\\n``, strip, and cut to ``limit`` characters (ellipsised)."""
    text = "" if text is None else str(text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    kept: list[str] = []
    for ch in text:
        code = ord(ch)
        if ch == "\n":
            kept.append(ch)
        elif ch == "\t":
            kept.append(" ")
        elif code < 0x20 or code == 0x7F or 0xD800 <= code <= 0xDFFF:
            continue
        else:
            kept.append(ch)
    text = "".join(kept).strip()
    if limit >= 0 and len(text) > limit:
        cut = max(0, limit - len(ellipsis))
        text = text[:cut].rstrip() + ellipsis if ellipsis and limit >= len(ellipsis) else text[:limit]
    return text


# ---------------------------------------------------------------------------
# 1. notify
# ---------------------------------------------------------------------------


_FALLBACK_TRAY: "TrayIcon | None" = None
_FALLBACK_LOCK = threading.Lock()


def _balloon_notify(title: str, body: str, on_click: Callable[[], None] | None, timeout_s: float) -> bool:
    """Show a tray balloon through a lazily created, process-wide TrayIcon."""
    global _FALLBACK_TRAY
    with _FALLBACK_LOCK:
        tray = _FALLBACK_TRAY
        if tray is None or not tray.available:
            tray = TrayIcon(tooltip="JARVIS Desktop")
            if not tray.start():
                return False
            _FALLBACK_TRAY = tray
    tray.on_balloon_click = on_click
    return tray.balloon(title, body, timeout_s)


def notify(
    title: str,
    body: str,
    *,
    on_click: Callable[[], None] | None = None,
    timeout_s: float = 8.0,
) -> str:
    """Show a Windows notification and return the path used.

    The tray balloon (``Shell_NotifyIconW`` NIF_INFO on a helper thread) is the
    only mechanism: the result is ``"balloon"`` when it was queued, else
    ``"unsupported"``. No subprocess is ever spawned.

    ``"balloon"`` means the balloon was handed to the tray thread, not that the
    shell rendered it (Focus Assist or disabled notifications can still hide
    it). ``on_click`` runs on the tray thread when NIN_BALLOONUSERCLICK
    arrives, and ``"balloon_click"`` is also queued on the tray's action queue.
    """
    title = bound_text(title, MAX_NOTIFY_TITLE)
    body = bound_text(body, MAX_NOTIFY_BODY)
    try:
        if _balloon_notify(title, body, on_click, timeout_s):
            return "balloon"
    except Exception:
        pass
    return "unsupported"


# ---------------------------------------------------------------------------
# 2. TrayIcon
# ---------------------------------------------------------------------------


class TrayIcon:
    """Notification-area icon with a small menu, run on its own daemon thread.

    Actions are delivered as strings on :attr:`actions` (``"open"``,
    ``"new_chat"``, ``"quit"``, ``"balloon_click"``); the owner drains the
    queue on its own thread (for Tk, an ``after`` poll). The default
    application icon is used, so no icon file is needed. Creation failure
    never raises: :meth:`start` returns False and :attr:`available` stays
    False.
    """

    MENU: tuple[tuple[str | None, str | None], ...] = (
        ("open", "Open JARVIS"),
        ("new_chat", "New chat"),
        (None, None),
        ("quit", "Quit"),
    )

    def __init__(
        self,
        actions: "queue.Queue[str] | None" = None,
        *,
        tooltip: str = "JARVIS Desktop",
        on_balloon_click: Callable[[], None] | None = None,
    ) -> None:
        self.actions: "queue.Queue[str]" = actions if actions is not None else queue.Queue()
        self.tooltip = bound_text(tooltip, 127, ellipsis="")
        self.on_balloon_click = on_balloon_click
        self._api: _Win32Api | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._available = False
        self._icon_added = False
        self._hwnd = 0
        self._hinstance: Any = None
        self._class_name = f"JarvisTrayIcon_{os.getpid()}_{id(self):x}"
        self._class_registered = False
        self._wndproc: Any = None
        self._nid: NOTIFYICONDATAW | None = None
        self._taskbar_created = 0
        self._pending: "collections.deque[tuple[str, str, int]]" = collections.deque()
        self._lock = threading.Lock()
        self._atexit_registered = False

    # -- public -------------------------------------------------------------

    @property
    def available(self) -> bool:
        """True while the icon is in the notification area."""
        return self._available and self._thread is not None and self._thread.is_alive()

    def start(self, timeout_s: float = 3.0) -> bool:
        """Create the icon on a daemon thread; True if it appeared."""
        if self._thread is not None and self._thread.is_alive():
            return self.available
        api = _api()
        if api is None:
            return False
        self._api = api
        self._ready.clear()
        self._available = False
        self._thread = threading.Thread(target=self._run, name="jarvis-tray-icon", daemon=True)
        self._thread.start()
        self._ready.wait(timeout_s)
        if self.available and not self._atexit_registered:
            self._atexit_registered = True
            atexit.register(self.stop)
        return self.available

    def stop(self, timeout_s: float = 2.0) -> None:
        """Remove the icon and end the message loop (idempotent)."""
        hwnd, api, thread = self._hwnd, self._api, self._thread
        if hwnd and api is not None:
            try:
                api.user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
            except Exception:
                pass
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout_s)

    def balloon(self, title: str, body: str, timeout_s: float = 8.0) -> bool:
        """Queue a balloon (shown by the tray thread); False if unavailable."""
        if not self.available or not self._hwnd or self._api is None:
            return False
        millis = int(max(10.0, min(30.0, float(timeout_s))) * 1000)
        with self._lock:
            self._pending.append((bound_text(title, 63), bound_text(body, 255), millis))
        try:
            return bool(self._api.user32.PostMessageW(self._hwnd, WM_TRAY_BALLOON, 0, 0))
        except Exception:
            return False

    # -- tray thread ----------------------------------------------------------

    def _run(self) -> None:
        api = self._api
        if api is None:
            self._ready.set()
            return
        u = api.user32
        try:
            self._hinstance = api.kernel32.GetModuleHandleW(None)
            self._wndproc = api.WNDPROC(self._wnd_proc)
            wc = WNDCLASSW()
            wc.lpfnWndProc = ctypes.cast(self._wndproc, ctypes.c_void_p).value
            wc.hInstance = self._hinstance
            wc.lpszClassName = self._class_name
            if not u.RegisterClassW(ctypes.byref(wc)):
                return
            self._class_registered = True
            self._taskbar_created = int(u.RegisterWindowMessageW("TaskbarCreated") or 0)
            hwnd = u.CreateWindowExW(
                0, self._class_name, "JARVIS tray", WS_OVERLAPPED, 0, 0, 0, 0, None, None, self._hinstance, None,
            )
            if not hwnd:
                return
            self._hwnd = int(hwnd)
            self._available = self._add_icon()
            self._ready.set()
            msg = wintypes.MSG()
            while True:
                result = u.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if result == 0 or result == -1:
                    break
                u.TranslateMessage(ctypes.byref(msg))
                u.DispatchMessageW(ctypes.byref(msg))
        except Exception:
            pass
        finally:
            self._remove_icon()
            self._available = False
            hwnd = self._hwnd
            self._hwnd = 0
            try:
                if hwnd:
                    u.DestroyWindow(hwnd)
            except Exception:
                pass
            try:
                if self._class_registered:
                    u.UnregisterClassW(self._class_name, self._hinstance)
                    self._class_registered = False
            except Exception:
                pass
            self._ready.set()

    def _base_nid(self) -> NOTIFYICONDATAW:
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = self._hwnd
        nid.uID = 1
        return nid

    def _add_icon(self) -> bool:
        api = self._api
        if api is None or not self._hwnd:
            return False
        nid = self._base_nid()
        nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        nid.uCallbackMessage = WM_TRAY_CALLBACK
        nid.hIcon = api.user32.LoadIconW(None, IDI_APPLICATION)
        nid.szTip = self.tooltip
        self._nid = nid
        if self._icon_added:
            api.shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(nid))
            self._icon_added = False
        self._icon_added = bool(api.shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid)))
        return self._icon_added

    def _remove_icon(self) -> None:
        api = self._api
        if api is None or not self._icon_added or self._nid is None:
            return
        try:
            api.shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._nid))
        except Exception:
            pass
        self._icon_added = False

    def _flush_balloons(self) -> None:
        api = self._api
        if api is None or not self._icon_added:
            return
        while True:
            with self._lock:
                if not self._pending:
                    return
                title, body, millis = self._pending.popleft()
            nid = self._base_nid()
            nid.uFlags = NIF_INFO
            nid.szInfoTitle = title
            nid.szInfo = body
            nid.dwInfoFlags = NIIF_INFO
            nid.uTimeout = millis
            api.shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))

    def _show_menu(self) -> None:
        api = self._api
        if api is None or not self._hwnd:
            return
        u = api.user32
        menu = u.CreatePopupMenu()
        if not menu:
            return
        try:
            for index, (action, label) in enumerate(self.MENU, start=1):
                if action is None:
                    u.AppendMenuW(menu, MF_SEPARATOR, 0, None)
                else:
                    u.AppendMenuW(menu, MF_STRING, index, label)
            point = wintypes.POINT()
            u.GetCursorPos(ctypes.byref(point))
            u.SetForegroundWindow(self._hwnd)  # required so the menu closes on focus loss
            command = u.TrackPopupMenu(
                menu, TPM_RETURNCMD | TPM_RIGHTBUTTON | TPM_BOTTOMALIGN | TPM_LEFTALIGN,
                point.x, point.y, 0, self._hwnd, None,
            )
            u.PostMessageW(self._hwnd, WM_NULL, 0, 0)
            if command and 1 <= command <= len(self.MENU):
                action = self.MENU[command - 1][0]
                if action:
                    self.actions.put(action)
        finally:
            u.DestroyMenu(menu)

    def _wnd_proc(self, hwnd: Any, message: int, wparam: int, lparam: int) -> int:
        api = self._api
        try:
            if message == WM_TRAY_CALLBACK:
                event = int(lparam) & 0xFFFF
                if event in (WM_LBUTTONUP, WM_LBUTTONDBLCLK, NIN_SELECT, NIN_KEYSELECT):
                    if event != WM_LBUTTONDBLCLK:  # the up-click already opened
                        self.actions.put("open")
                elif event in (WM_RBUTTONUP, WM_CONTEXTMENU):
                    self._show_menu()
                elif event == NIN_BALLOONUSERCLICK:
                    self.actions.put("balloon_click")
                    callback = self.on_balloon_click
                    if callback is not None:
                        try:
                            callback()
                        except Exception:
                            pass
                return 0
            if message == WM_TRAY_BALLOON:
                self._flush_balloons()
                return 0
            if message == WM_DESTROY:
                self._remove_icon()
                if api is not None:
                    api.user32.PostQuitMessage(0)
                return 0
            if self._taskbar_created and message == self._taskbar_created:
                self._available = self._add_icon()  # Explorer restarted
                return 0
        except Exception:
            return 0
        if api is None:
            return 0
        return int(api.user32.DefWindowProcW(hwnd, message, wparam, lparam))


# ---------------------------------------------------------------------------
# PNG helpers
# ---------------------------------------------------------------------------


def _encode_png(width: int, height: int, rows: list[bytes], alpha: bool) -> bytes:
    """Reuse ``jarvis.ui.encode_png`` lazily; fall back to a local writer when
    ``jarvis.ui`` cannot be imported (for example without tkinter)."""
    try:
        from . import ui  # lazy: jarvis.ui imports tkinter and the agent

        return ui.encode_png(width, height, rows, alpha)
    except Exception:
        pass

    def chunk(tag: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + tag + body + struct.pack(">I", zlib.crc32(tag + body) & 0xFFFFFFFF)

    raw = b"".join(b"\x00" + row for row in rows)
    header = struct.pack(">IIBBBBB", width, height, 8, 6 if alpha else 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b"")


def dib_to_png(dib: bytes) -> bytes | None:
    """Convert a packed DIB (BITMAPINFOHEADER + pixels, 24/32-bit, BI_RGB or
    BI_BITFIELDS, top-down or bottom-up) to PNG bytes; None if unsupported.

    32-bit sources are emitted as RGBA with alpha forced opaque, because
    screen captures usually carry alpha 0.
    """
    if len(dib) < 40:
        return None
    header_size, width, height, _planes, bits, compression, _image_size = struct.unpack_from("<IiiHHII", dib, 0)
    if header_size < 40 or bits not in (24, 32) or compression not in (BI_RGB, BI_BITFIELDS) or width <= 0 or height == 0:
        return None
    if width * abs(height) > MAX_CAPTURE_PIXELS:
        return None
    top_down = height < 0
    height = abs(height)
    offset = header_size
    if compression == BI_BITFIELDS and header_size == 40:
        offset += 12
    row_bytes = ((width * bits + 31) // 32) * 4
    if len(dib) < offset + row_bytes * height:
        return None
    channels = 4 if bits == 32 else 3
    rows: list[bytes] = []
    order = range(height) if top_down else range(height - 1, -1, -1)
    for index in order:
        start = offset + index * row_bytes
        row = dib[start:start + width * channels]
        if bits == 32:
            pixels = bytearray(width * 4)
            pixels[0::4] = row[2::4]
            pixels[1::4] = row[1::4]
            pixels[2::4] = row[0::4]
            pixels[3::4] = b"\xff" * width
        else:
            pixels = bytearray(width * 3)
            pixels[0::3] = row[2::3]
            pixels[1::3] = row[1::3]
            pixels[2::3] = row[0::3]
        rows.append(bytes(pixels))
    return _encode_png(width, height, rows, bits == 32)


# ---------------------------------------------------------------------------
# 3. capture_screen_region
# ---------------------------------------------------------------------------


def _capture_into_dib(api: _Win32Api, width: int, height: int, render: Callable[[Any, Any], bool]) -> bytes | None:
    """Render into a 32-bit top-down DIB section and return PNG bytes.

    ``render(memory_dc, screen_dc)`` draws into the memory DC and returns
    success. Every GDI object is released on every path.
    """
    if width <= 0 or height <= 0 or width * height > MAX_CAPTURE_PIXELS:
        return None
    u, g = api.user32, api.gdi32
    screen = u.GetDC(None)
    if not screen:
        return None
    try:
        memory = g.CreateCompatibleDC(screen)
        if not memory:
            return None
        try:
            info = BITMAPINFO()
            info.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
            info.bmiHeader.biWidth = width
            info.bmiHeader.biHeight = -height  # top-down
            info.bmiHeader.biPlanes = 1
            info.bmiHeader.biBitCount = 32
            info.bmiHeader.biCompression = BI_RGB
            bits = ctypes.c_void_p()
            bitmap = g.CreateDIBSection(memory, ctypes.byref(info), DIB_RGB_COLORS, ctypes.byref(bits), None, 0)
            if not bitmap or not bits.value:
                return None
            try:
                previous = g.SelectObject(memory, bitmap)
                try:
                    if not render(memory, screen):
                        return None
                    g.GdiFlush()
                    raw = ctypes.string_at(bits.value, width * height * 4)
                finally:
                    g.SelectObject(memory, previous)
            finally:
                g.DeleteObject(bitmap)
        finally:
            g.DeleteDC(memory)
    finally:
        u.ReleaseDC(None, screen)
    header = struct.pack("<IiiHHIIiiII", 40, width, -height, 1, 32, BI_RGB, width * height * 4, 0, 0, 0, 0)
    return dib_to_png(header + raw)


def capture_screen_region(left: int, top: int, width: int, height: int) -> bytes | None:
    """PNG of a screen region via ``GetDC(0)`` + ``BitBlt``; None on failure.

    In a process that is not DPI aware the coordinates are virtualised, so set
    DPI awareness before capturing on scaled displays.
    """
    api = _api()
    if api is None:
        return None
    try:
        left, top, width, height = int(left), int(top), int(width), int(height)

        def render(memory: Any, screen: Any) -> bool:
            return bool(api.gdi32.BitBlt(memory, 0, 0, width, height, screen, left, top, SRCCOPY))

        return _capture_into_dib(api, width, height, render)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 4. Snipping overlay and clipboard polling
# ---------------------------------------------------------------------------


def launch_snip() -> bool:
    """Open the Windows snipping overlay (``ms-screenclip:``); True if launched.

    The overlay copies the selection to the clipboard; pair with
    :func:`wait_for_clipboard_image`.
    """
    if sys.platform != "win32":
        return False
    try:
        os.startfile("ms-screenclip:")  # type: ignore[attr-defined]
        return True
    except Exception:
        return False


def clipboard_sequence() -> int:
    """``GetClipboardSequenceNumber``; 0 when unavailable."""
    api = _api()
    if api is None:
        return 0
    try:
        return int(api.user32.GetClipboardSequenceNumber())
    except Exception:
        return 0


def _clipboard_png() -> bytes | None:
    try:
        from . import ui  # lazy, see module docstring

        return ui.clipboard_image_png()
    except Exception:
        return None


def wait_for_clipboard_image(
    timeout_s: float,
    poll_s: float = 0.4,
    *,
    baseline_sequence: int | None = None,
) -> bytes | None:
    """Poll the clipboard sequence number and return PNG bytes once a NEW
    CF_DIB arrives; None on timeout.

    Take ``baseline_sequence = clipboard_sequence()`` before launching the
    snip so an image that lands quickly is not missed. Sequence changes that
    carry no image (text copied) move the baseline and polling continues.
    """
    baseline = clipboard_sequence() if baseline_sequence is None else int(baseline_sequence)
    poll_s = max(0.01, float(poll_s))
    deadline = time.monotonic() + max(0.0, float(timeout_s))
    while True:
        current = clipboard_sequence()
        if current != baseline:
            png = _clipboard_png()
            if png:
                return png
            baseline = current
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        time.sleep(min(poll_s, remaining))
