from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from jarvis.companion_indicator import (
    CompanionIndicatorClient,
    _NoRedirectHandler,
    _show_windows_no_activate,
    _tk_geometry,
    indicator_presentation,
    indicator_should_be_visible,
)


class _NativeCall:
    def __init__(self, result):
        self.result = result
        self.calls = []
        self.argtypes = None
        self.restype = None

    def __call__(self, *args):
        self.calls.append(args)
        return self.result


class CompanionIndicatorTests(unittest.TestCase):
    def test_signed_geometry_supports_secondary_monitor_origins(self):
        self.assertEqual(_tk_geometry(360, 140, -1900, 820), "360x140-1900+820")

    def test_windows_popup_maps_the_real_toplevel_without_activation(self):
        window = SimpleNamespace(
            winfo_id=lambda: 101,
            deiconify=mock.Mock(),
            update_idletasks=mock.Mock(),
        )
        user32 = SimpleNamespace(
            GetParent=_NativeCall(202),
            GetWindowLongW=_NativeCall(0x10),
            SetWindowLongW=_NativeCall(0),
            ShowWindow=_NativeCall(1),
            SetWindowPos=_NativeCall(1),
        )

        self.assertTrue(_show_windows_no_activate(window, user32))
        window.deiconify.assert_called_once_with()
        window.update_idletasks.assert_called_once_with()
        self.assertEqual(int(user32.ShowWindow.calls[0][0]), 202)
        self.assertEqual(user32.ShowWindow.calls[0][1], 4)
        self.assertEqual(int(user32.SetWindowPos.calls[0][0]), 202)
        self.assertEqual(user32.SetWindowPos.calls[0][-1] & 0x0010, 0x0010)

    def test_every_operator_visible_state_has_an_unambiguous_label(self):
        cases = [
            (None, "JARVIS OFFLINE"),
            ({"mode": "disabled", "paused": True, "available": True}, "JARVIS OFF"),
            ({"mode": "observe", "paused": False, "available": True}, "OBSERVING"),
            ({"mode": "suggest", "paused": False, "available": True}, "SUGGEST MODE"),
            ({"mode": "collaborate", "paused": False, "available": True}, "COLLABORATING"),
            ({"mode": "observe", "paused": True, "available": True}, "PAUSED · OBSERVE"),
            ({"mode": "observe", "paused": False, "available": False}, "JARVIS UNAVAILABLE"),
        ]
        for state, expected in cases:
            with self.subTest(state=state):
                self.assertEqual(indicator_presentation(state).label, expected)

    def test_indicator_is_visible_only_during_active_screen_observation(self):
        hidden_states = [
            None,
            {"mode": "disabled", "paused": True, "available": True},
            {"mode": "observe", "paused": True, "available": True},
            {"mode": "observe", "paused": False, "available": False},
        ]
        for state in hidden_states:
            with self.subTest(state=state):
                self.assertFalse(indicator_should_be_visible(state))

        for mode in ("observe", "suggest", "collaborate"):
            with self.subTest(mode=mode):
                self.assertTrue(indicator_should_be_visible({
                    "mode": mode,
                    "paused": False,
                    "available": True,
                }))

    def test_client_accepts_only_loopback_and_bounded_modes(self):
        with self.assertRaises(ValueError):
            CompanionIndicatorClient("example.com", 8787)
        client = CompanionIndicatorClient("127.0.0.1", 8787)
        with self.assertRaises(ValueError):
            client.control("delete")
        with self.assertRaises(ValueError):
            client.control("mode", mode="disabled")
        with self.assertRaises(RuntimeError):
            client._validated_state({"mode": "observe", "paused": "no"})
        state = client._validated_state({
            "mode": "suggest",
            "paused": False,
            "available": True,
            "suggestion": {
                "id": "a" * 32,
                "text": "Want me to organize this into three clear sections?",
                "expires_at": 2_000_000_000.0,
            },
        })
        self.assertEqual(state["suggestion"]["id"], "a" * 32)
        with self.assertRaises(RuntimeError):
            client._validated_state({
                "mode": "suggest",
                "paused": False,
                "suggestion": {
                    "id": "not-an-id",
                    "text": "Do it",
                    "expires_at": 2_000_000_000.0,
                },
            })
        with self.assertRaises(ValueError):
            client.respond_suggestion("not-an-id", accept=True)
        action = client._validated_action_status({
            "action": {
                "job_id": "b" * 32,
                "state": "running",
                "message": "Working on it…",
                "terminal": False,
            }
        })
        self.assertEqual(action["state"], "running")
        self.assertFalse(action["terminal"])
        with self.assertRaises(RuntimeError):
            client._validated_action_status({
                "action": {
                    "job_id": "b" * 32,
                    "state": "vanished",
                    "message": "Done",
                    "terminal": True,
                }
            })
        with self.assertRaises(ValueError):
            client.action_status("not-an-id")

    def test_loopback_client_never_follows_redirects(self):
        handler = _NoRedirectHandler()
        self.assertIsNone(handler.redirect_request(
            object(), None, 302, "Found", {}, "https://example.com/"
        ))


if __name__ == "__main__":
    unittest.main()
