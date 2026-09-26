from __future__ import annotations

import unittest
from jarvis import companion_indicator
from jarvis.local_broker import PIPE_PREFIX, LocalBrokerServer
from unittest.mock import patch
import uuid
import os
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


@unittest.skipUnless(os.name == "nt", "named pipes exist only on Windows")
class CompanionIndicatorBrokerTests(unittest.TestCase):
    def test_start_indicator_process_passes_the_broker_pipe(self):
        captured: dict[str, list[str]] = {}

        def fake_popen(command, **_kwargs):
            captured["command"] = list(command)
            return SimpleNamespace(poll=lambda: None)

        name = PIPE_PREFIX + "jarvis-test-" + uuid.uuid4().hex[:16]
        with patch("jarvis.companion_indicator.subprocess.Popen", fake_popen):
            companion_indicator.start_indicator_process("127.0.0.1", 8787, pipe_name=name)
            command = captured["command"]
            self.assertIn("--pipe", command)
            self.assertEqual(command[command.index("--pipe") + 1], name)
            companion_indicator.start_indicator_process("127.0.0.1", 8787)
            self.assertNotIn("--pipe", captured["command"])
            with self.assertRaises(ValueError):
                companion_indicator.start_indicator_process("127.0.0.1", 8787, pipe_name="not-a-pipe")

    def test_client_uses_the_broker_for_every_indicator_operation(self):
        name = PIPE_PREFIX + "jarvis-test-" + uuid.uuid4().hex[:16]
        seen: list[tuple[str, dict]] = []
        state = {
            "mode": "observe",
            "paused": False,
            "available": True,
            "updated_at": "now",
            "suggestion": None,
        }

        def handler(request):
            seen.append((request["kind"], dict(request["payload"])))
            kind = request["kind"]
            if kind == "companion.indicator":
                return dict(state)
            if kind == "companion.control":
                return {"state": {**state, "mode": request["payload"].get("mode") or "observe"}}
            if kind == "companion.suggestion":
                return {"accepted": bool(request["payload"].get("accept"))}
            if kind == "companion.action":
                return {
                    "action": {
                        "job_id": request["payload"]["id"],
                        "state": "running",
                        "message": "Working",
                        "terminal": False,
                    }
                }
            raise LookupError(kind)

        server = LocalBrokerServer(name, handler)
        server.start()
        self.addCleanup(server.stop)
        client = CompanionIndicatorClient("127.0.0.1", 8787, pipe_name=name)
        self.assertEqual(client.status()["mode"], "observe")
        self.assertEqual(client.control("mode", mode="suggest")["mode"], "suggest")
        self.assertEqual(client.respond_suggestion("a" * 32, accept=True), {"accepted": True})
        self.assertEqual(client.action_status("b" * 32)["state"], "running")
        self.assertEqual(
            [kind for kind, _ in seen],
            ["companion.indicator", "companion.control", "companion.suggestion", "companion.action"],
        )
        self.assertEqual(seen[0][1], {})
        self.assertEqual(seen[1][1], {"action": "mode", "mode": "suggest"})
        self.assertEqual(seen[2][1], {"id": "a" * 32, "accept": True})
        self.assertEqual(seen[3][1], {"id": "b" * 32})
        self.assertEqual(server.stats["served"], 4)

    def test_broker_failures_surface_as_runtime_errors_without_http_fallback(self):
        missing = PIPE_PREFIX + "jarvis-test-" + uuid.uuid4().hex[:16]
        client = CompanionIndicatorClient("127.0.0.1", 8787, pipe_name=missing)
        with patch.object(client, "_opener", side_effect=AssertionError("HTTP must not be used")):
            with self.assertRaisesRegex(RuntimeError, "broker request failed"):
                client.status()

    def test_routes_outside_the_indicator_surface_never_reach_the_broker(self):
        for path, payload in (
            ("/api/status", None),
            ("/api/screen-companion/forget", {}),
            ("/api/screen-companion/rules", {"pattern": "x"}),
            ("/api/screen-companion/indicator", {}),
            ("/api/screen-companion/control", None),
        ):
            with self.subTest(path=path):
                with self.assertRaises(RuntimeError):
                    CompanionIndicatorClient._broker_route(path, payload)
