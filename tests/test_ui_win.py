"""Tests for jarvis.ui_win.

Every Win32 entry point is faked: nothing here opens a window, touches the
tray, or needs a display. ``subprocess.Popen`` is patched to fail loudly in
the notification tests because the module must never spawn a process.
"""

from __future__ import annotations

import ast
import ctypes
import queue
import struct
import subprocess
import sys
import time
import types
import unittest
import zlib
from unittest import mock

from jarvis import ui_win


def _decode_png(data: bytes) -> tuple[int, int, int, list[bytes]]:
    """Return (width, height, color_type, rows) for the unfiltered PNGs the module writes."""
    assert data.startswith(b"\x89PNG\r\n\x1a\n")
    offset = 8
    width = height = color_type = 0
    idat = b""
    while offset < len(data):
        length, tag = struct.unpack_from(">I4s", data, offset)
        body = data[offset + 8:offset + 8 + length]
        if tag == b"IHDR":
            width, height, _depth, color_type = struct.unpack_from(">IIBB", body, 0)
        elif tag == b"IDAT":
            idat += body
        offset += 12 + length
    raw = zlib.decompress(idat)
    channels = 4 if color_type == 6 else 3
    stride = 1 + width * channels
    rows = [raw[i * stride + 1:(i + 1) * stride] for i in range(height)]
    assert all(raw[i * stride] == 0 for i in range(height))
    return width, height, color_type, rows


def _dib(width: int, height: int, bgra_rows: list[bytes], *, bits: int = 32) -> bytes:
    """Pack a bottom-up DIB from top-to-bottom BGRA/BGR rows."""
    row_bytes = ((width * bits + 31) // 32) * 4
    body = b"".join(row.ljust(row_bytes, b"\x00") for row in reversed(bgra_rows))
    header = struct.pack("<IiiHHIIiiII", 40, width, height, 1, bits, 0, len(body), 0, 0, 0, 0)
    return header + body


def _no_subprocess(*args, **kwargs):
    raise AssertionError(f"ui_win must never spawn a subprocess (Popen called with {args!r})")


class TextSafetyTests(unittest.TestCase):
    def test_bound_text_limits_and_strips_controls(self):
        title = ui_win.bound_text("T" * 500, ui_win.MAX_NOTIFY_TITLE)
        body = ui_win.bound_text("B" * 5000, ui_win.MAX_NOTIFY_BODY)
        self.assertEqual(len(title), 64)
        self.assertEqual(len(body), 200)
        self.assertTrue(title.endswith("…"))
        self.assertEqual(ui_win.bound_text("a\x00b\x07c\td\r\ne\ud800f", 100), "abc d\nef")
        self.assertEqual(ui_win.bound_text(None, 10), "")
        self.assertEqual(ui_win.bound_text("x" * 10, 5, ellipsis=""), "xxxxx")


class NotifyTests(unittest.TestCase):
    """notify() is balloon-only: no toast, no PowerShell, no subprocess."""

    def test_balloon_when_tray_helper_available(self):
        with mock.patch.object(subprocess, "Popen", side_effect=_no_subprocess), \
                mock.patch.object(ui_win, "_balloon_notify", return_value=True) as balloon:
            started = time.monotonic()
            result = ui_win.notify("T" * 100, "B" * 300, on_click=None)
            elapsed = time.monotonic() - started
        self.assertEqual(result, "balloon")
        self.assertLess(elapsed, 0.5)
        balloon.assert_called_once()
        title, body, on_click, timeout = balloon.call_args[0]
        self.assertEqual(len(title), 64)
        self.assertEqual(len(body), 200)
        self.assertIsNone(on_click)
        self.assertEqual(timeout, 8.0)

    def test_unsupported_when_tray_helper_unavailable(self):
        with mock.patch.object(subprocess, "Popen", side_effect=_no_subprocess), \
                mock.patch.object(ui_win, "_balloon_notify", return_value=False) as balloon:
            self.assertEqual(ui_win.notify("t", "b"), "unsupported")
        balloon.assert_called_once()

    def test_unsupported_when_ctypes_entry_points_unavailable(self):
        with mock.patch.object(subprocess, "Popen", side_effect=_no_subprocess), \
                mock.patch.object(ui_win, "_api", return_value=None), \
                mock.patch.object(ui_win, "_FALLBACK_TRAY", None):
            self.assertEqual(ui_win.notify("t", "b"), "unsupported")
            self.assertFalse(ui_win._balloon_notify("t", "b", None, 8.0))

    def test_balloon_path_reuses_one_process_wide_tray(self):
        trays: list[mock.Mock] = []

        def make_tray(**kwargs):
            tray = mock.Mock()
            tray.available = True
            tray.start.return_value = True
            tray.balloon.return_value = True
            trays.append(tray)
            return tray

        clicks: list[str] = []
        with mock.patch.object(subprocess, "Popen", side_effect=_no_subprocess), \
                mock.patch.object(ui_win, "TrayIcon", side_effect=make_tray), \
                mock.patch.object(ui_win, "_FALLBACK_TRAY", None):
            self.assertEqual(ui_win.notify("one", "1", on_click=lambda: clicks.append("x")), "balloon")
            self.assertEqual(ui_win.notify("two", "2", timeout_s=12.0), "balloon")
        self.assertEqual(len(trays), 1)
        tray = trays[0]
        tray.start.assert_called_once()
        self.assertEqual(
            [call.args for call in tray.balloon.call_args_list],
            [("one", "1", 8.0), ("two", "2", 12.0)],
        )
        self.assertIsNone(tray.on_balloon_click)  # second call cleared the first callback

    def test_exceptions_inside_balloon_path_do_not_escape(self):
        with mock.patch.object(subprocess, "Popen", side_effect=_no_subprocess), \
                mock.patch.object(ui_win, "_balloon_notify", side_effect=RuntimeError("boom")):
            self.assertEqual(ui_win.notify("t", "b"), "unsupported")

    def test_module_never_references_subprocess_or_powershell(self):
        with open(ui_win.__file__, encoding="utf-8") as handle:
            source = handle.read()
        tree = ast.parse(source)
        imported = {
            alias.name.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for alias in node.names
        }
        modules = {node.module.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module}
        self.assertNotIn("subprocess", imported | modules)
        for marker in ("powershell.exe", "EncodedCommand", "ToastNotification", "SpeechSynthesizer"):
            self.assertNotIn(marker, source, marker)
        for name in ("toast_script", "speak", "stop_speaking", "speech_script", "list_windows", "capture_window"):
            self.assertFalse(hasattr(ui_win, name), name)


class TrayIconTests(unittest.TestCase):
    def test_tray_icon_degrades_without_win32(self):
        with mock.patch.object(ui_win, "_api", return_value=None):
            tray = ui_win.TrayIcon()
            self.assertFalse(tray.start())
            self.assertFalse(tray.available)
            self.assertFalse(tray.balloon("t", "b"))
            tray.stop()  # no-op, must not raise
            self.assertTrue(tray.actions.empty())

    def test_wnd_proc_routes_tray_events_to_queue(self):
        actions: "queue.Queue[str]" = queue.Queue()
        clicks: list[str] = []
        tray = ui_win.TrayIcon(actions, on_balloon_click=lambda: clicks.append("clicked"))
        user32 = types.SimpleNamespace(
            DefWindowProcW=mock.Mock(return_value=7),
            PostQuitMessage=mock.Mock(),
        )
        tray._api = types.SimpleNamespace(user32=user32, shell32=mock.Mock(), gdi32=None)
        tray._show_menu = mock.Mock()  # type: ignore[method-assign]
        self.assertEqual(tray._wnd_proc(1, ui_win.WM_TRAY_CALLBACK, 0, ui_win.WM_LBUTTONUP), 0)
        self.assertEqual(tray._wnd_proc(1, ui_win.WM_TRAY_CALLBACK, 0, ui_win.NIN_BALLOONUSERCLICK), 0)
        self.assertEqual(tray._wnd_proc(1, ui_win.WM_TRAY_CALLBACK, 0, ui_win.WM_RBUTTONUP), 0)
        self.assertEqual(tray._wnd_proc(1, ui_win.WM_DESTROY, 0, 0), 0)
        self.assertEqual(tray._wnd_proc(1, 0x1234, 5, 6), 7)
        self.assertEqual([actions.get_nowait(), actions.get_nowait()], ["open", "balloon_click"])
        self.assertTrue(actions.empty())
        self.assertEqual(clicks, ["clicked"])
        tray._show_menu.assert_called_once()
        user32.PostQuitMessage.assert_called_once_with(0)
        user32.DefWindowProcW.assert_called_once_with(1, 0x1234, 5, 6)

    def test_balloon_queues_bounded_text_and_posts_to_tray_thread(self):
        tray = ui_win.TrayIcon()
        user32 = types.SimpleNamespace(PostMessageW=mock.Mock(return_value=1))
        tray._api = types.SimpleNamespace(user32=user32)
        tray._hwnd = 0x55
        tray._available = True
        tray._thread = types.SimpleNamespace(is_alive=lambda: True)
        self.assertTrue(tray.balloon("T" * 100, "B" * 300, timeout_s=99.0))
        user32.PostMessageW.assert_called_once_with(0x55, ui_win.WM_TRAY_BALLOON, 0, 0)
        title, body, millis = tray._pending[0]
        self.assertEqual(len(title), 63)
        self.assertEqual(len(body), 255)
        self.assertEqual(millis, 30_000)  # clamped to the shell's 10..30 s window

    def test_notifyicondata_layout_matches_the_sdk(self):
        if ctypes.sizeof(ctypes.c_void_p) == 8:
            self.assertEqual(ctypes.sizeof(ui_win.NOTIFYICONDATAW), 976)
        else:
            self.assertEqual(ctypes.sizeof(ui_win.NOTIFYICONDATAW), 956)
        self.assertEqual(ctypes.sizeof(ui_win.BITMAPINFOHEADER), 40)


class _FakeGdi:
    """Fake GetDC/CreateDIBSection/BitBlt with a real pixel buffer."""

    def __init__(self, *, blt_ok: bool = True, fill: bytes = b"\x01\x02\x03\x00"):
        self.blt_ok = blt_ok
        self.fill = fill
        self.buffer = None
        self.released = {"dc": 0, "memdc": 0, "bitmap": 0}
        self.user32 = types.SimpleNamespace(
            GetDC=lambda hwnd: 0x1001,
            ReleaseDC=self._release_dc,
        )
        self.gdi32 = types.SimpleNamespace(
            CreateCompatibleDC=lambda dc: 0x2002,
            DeleteDC=self._delete_dc,
            CreateDIBSection=self._dib,
            SelectObject=lambda dc, obj: 0x3003,
            DeleteObject=self._delete_object,
            BitBlt=self._blt,
            GdiFlush=lambda: 1,
        )

    def _dib(self, dc, info_ref, usage, bits_ref, section, offset):
        header = info_ref._obj.bmiHeader
        assert header.biBitCount == 32 and header.biHeight < 0 and header.biCompression == 0
        size = header.biWidth * -header.biHeight * 4
        self.buffer = ctypes.create_string_buffer(size)
        bits_ref._obj.value = ctypes.addressof(self.buffer)
        return 0x4004

    def _paint(self):
        assert self.buffer is not None
        pattern = (self.fill * (len(self.buffer) // 4 + 1))[: len(self.buffer)]
        ctypes.memmove(self.buffer, pattern, len(pattern))

    def _blt(self, *args):
        if not self.blt_ok:
            return 0
        self._paint()
        return 1

    def _release_dc(self, hwnd, dc):
        self.released["dc"] += 1
        return 1

    def _delete_dc(self, dc):
        self.released["memdc"] += 1
        return 1

    def _delete_object(self, obj):
        self.released["bitmap"] += 1
        return 1


class CaptureTests(unittest.TestCase):
    def test_capture_screen_region_returns_none_when_bitblt_fails(self):
        fake = _FakeGdi(blt_ok=False)
        with mock.patch.object(ui_win, "_api", return_value=fake):
            self.assertIsNone(ui_win.capture_screen_region(0, 0, 3, 2))
        self.assertEqual(fake.released, {"dc": 1, "memdc": 1, "bitmap": 1})

    def test_capture_screen_region_round_trips_pixels(self):
        fake = _FakeGdi(fill=b"\x00\x00\xff\x00")  # pure red in BGRA
        with mock.patch.object(ui_win, "_api", return_value=fake):
            png = ui_win.capture_screen_region(5, 5, 3, 2)
        self.assertIsNotNone(png)
        width, height, color_type, rows = _decode_png(png)
        self.assertEqual((width, height, color_type), (3, 2, 6))
        self.assertEqual(rows[1], b"\xff\x00\x00\xff" * 3)
        self.assertEqual(fake.released, {"dc": 1, "memdc": 1, "bitmap": 1})

    def test_capture_rejects_bad_sizes_and_missing_api(self):
        with mock.patch.object(ui_win, "_api", return_value=None):
            self.assertIsNone(ui_win.capture_screen_region(0, 0, 10, 10))
        fake = _FakeGdi()
        with mock.patch.object(ui_win, "_api", return_value=fake):
            self.assertIsNone(ui_win.capture_screen_region(0, 0, 0, 10))
            self.assertIsNone(ui_win.capture_screen_region(0, 0, 100_000, 100_000))
        self.assertEqual(fake.released, {"dc": 0, "memdc": 0, "bitmap": 0})


class DibToPngTests(unittest.TestCase):
    def test_bottom_up_32bit_dib(self):
        top = b"\x01\x02\x03\x00" + b"\x04\x05\x06\x00"
        bottom = b"\x07\x08\x09\x00" + b"\x0a\x0b\x0c\x00"
        png = ui_win.dib_to_png(_dib(2, 2, [top, bottom]))
        width, height, color_type, rows = _decode_png(png)
        self.assertEqual((width, height, color_type), (2, 2, 6))
        self.assertEqual(rows[0], b"\x03\x02\x01\xff\x06\x05\x04\xff")
        self.assertEqual(rows[1], b"\x09\x08\x07\xff\x0c\x0b\x0a\xff")

    def test_24bit_dib_and_rejections(self):
        png = ui_win.dib_to_png(_dib(1, 1, [b"\x01\x02\x03"], bits=24))
        width, height, color_type, rows = _decode_png(png)
        self.assertEqual((width, height, color_type), (1, 1, 2))
        self.assertEqual(rows[0], b"\x03\x02\x01")
        self.assertIsNone(ui_win.dib_to_png(b""))
        self.assertIsNone(ui_win.dib_to_png(_dib(2, 2, [b"\x00" * 8] * 2)[:-4]))
        bad_bits = struct.pack("<IiiHHIIiiII", 40, 1, 1, 1, 8, 0, 4, 0, 0, 0, 0) + b"\x00" * 4
        self.assertIsNone(ui_win.dib_to_png(bad_bits))


class ClipboardTests(unittest.TestCase):
    def test_wait_returns_none_on_timeout_with_static_sequence(self):
        with mock.patch.object(ui_win, "clipboard_sequence", return_value=5), \
                mock.patch.object(ui_win, "_clipboard_png") as png:
            started = time.monotonic()
            self.assertIsNone(ui_win.wait_for_clipboard_image(0.12, 0.02))
            elapsed = time.monotonic() - started
        png.assert_not_called()
        self.assertGreaterEqual(elapsed, 0.1)
        self.assertLess(elapsed, 2.0)

    def test_wait_returns_png_when_sequence_moves(self):
        sequences = iter([5, 5, 6, 6])
        with mock.patch.object(ui_win, "clipboard_sequence", side_effect=lambda: next(sequences, 6)), \
                mock.patch.object(ui_win, "_clipboard_png", return_value=b"\x89PNG"):
            self.assertEqual(ui_win.wait_for_clipboard_image(1.0, 0.01), b"\x89PNG")

    def test_wait_ignores_non_image_changes_and_uses_baseline(self):
        sequences = iter([7, 8, 8, 8])
        with mock.patch.object(ui_win, "clipboard_sequence", side_effect=lambda: next(sequences, 8)), \
                mock.patch.object(ui_win, "_clipboard_png", return_value=None) as png:
            self.assertIsNone(ui_win.wait_for_clipboard_image(0.05, 0.01, baseline_sequence=6))
        # 7 and 8 are both "new" relative to baseline 6 but carry no image.
        self.assertEqual(png.call_count, 2)

    def test_clipboard_sequence_without_api(self):
        with mock.patch.object(ui_win, "_api", return_value=None):
            self.assertEqual(ui_win.clipboard_sequence(), 0)


class SnipTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "win32", "os.startfile is Windows-only")
    def test_launch_snip_uses_ms_screenclip(self):
        with mock.patch.object(ui_win.os, "startfile", create=True) as startfile:
            self.assertTrue(ui_win.launch_snip())
        startfile.assert_called_once_with("ms-screenclip:")
        with mock.patch.object(ui_win.os, "startfile", side_effect=OSError("no handler"), create=True):
            self.assertFalse(ui_win.launch_snip())


class NonWindowsTests(unittest.TestCase):
    def test_every_function_degrades_off_windows(self):
        with mock.patch.object(ui_win.sys, "platform", "linux"), \
                mock.patch.object(subprocess, "Popen", side_effect=_no_subprocess), \
                mock.patch.object(ui_win, "_FALLBACK_TRAY", None):
            self.assertIsNone(ui_win._api())
            self.assertEqual(ui_win.notify("t", "b"), "unsupported")
            tray = ui_win.TrayIcon()
            self.assertFalse(tray.start())
            self.assertFalse(tray.available)
            self.assertFalse(tray.balloon("t", "b"))
            tray.stop()
            self.assertIsNone(ui_win.capture_screen_region(0, 0, 4, 4))
            self.assertFalse(ui_win.launch_snip())
            self.assertEqual(ui_win.clipboard_sequence(), 0)
            self.assertIsNone(ui_win.wait_for_clipboard_image(0.02, 0.01))
        # Pure helpers keep working everywhere.
        self.assertIsNotNone(ui_win.dib_to_png(_dib(1, 1, [b"\x01\x02\x03\x00"])))

    def test_module_has_no_windows_only_top_level_dependencies(self):
        # Structures and constants are defined with plain ctypes/wintypes so
        # the module imports on any platform; WINFUNCTYPE is only touched
        # inside _Win32Api.
        with open(ui_win.__file__, encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        windll_uses = [node for node in ast.walk(tree) if isinstance(node, ast.Attribute) and node.attr == "windll"]
        self.assertEqual(windll_uses, [])
        for name in ui_win.__all__:
            self.assertTrue(hasattr(ui_win, name), name)


if __name__ == "__main__":
    unittest.main()
