from __future__ import annotations

import base64
import hashlib
import ctypes
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

from jarvis.attachments import ImageAttachment
from jarvis.memory import Memory
from jarvis.screen_companion import (
    COMPANION_INDICATOR_TITLE,
    ScreenCompanion,
    ScreenObservation,
    WindowsForegroundProvider,
)


class _FakeProvider:
    available = True

    def __init__(self, observation: ScreenObservation) -> None:
        self.observation = observation
        self.capture_requests: list[bool] = []

    def observe(self, *, capture_pixels: bool, excluded_apps: set[str]):
        self.capture_requests.append(capture_pixels)
        if self.observation.application in excluded_apps:
            return ScreenObservation(
                application=self.observation.application,
                title="Sensitive window hidden",
                observed_at=time.time(),
                context_sha256=self.observation.context_sha256,
                excluded=True,
                exclusion_reason="application is excluded",
            )
        return self.observation


class ScreenCompanionMemoryTests(unittest.TestCase):
    def test_state_is_opt_in_and_disabling_forces_pause(self):
        with Memory(Path(":memory:")) as memory:
            self.assertEqual(memory.screen_companion_state()["mode"], "disabled")
            state = memory.set_screen_companion_state(
                mode="suggest",
                paused=False,
                auto_suggest=True,
                excluded_apps=["Vault.EXE", "vault.exe"],
            )
            self.assertFalse(state["paused"])
            self.assertTrue(state["auto_suggest"])
            self.assertEqual(state["excluded_apps"], ["vault.exe"])
            state = memory.set_screen_companion_state(
                mode="disabled",
                paused=False,
                auto_suggest=True,
                excluded_apps=[],
            )
            self.assertTrue(state["paused"])
            self.assertFalse(state["auto_suggest"])

    def test_rules_reject_secrets_and_claim_once_during_cooldown(self):
        with Memory(Path(":memory:")) as memory:
            with self.assertRaises(ValueError):
                memory.add_screen_companion_rule(
                    trigger_app="chrome.exe",
                    action_prompt="Use API_KEY=sk-proj-" + "A" * 40,
                )
            rule_id = memory.add_screen_companion_rule(
                trigger_app="chrome.exe",
                title_contains="Gmail",
                action_prompt="Summarize unread mail",
                cooldown_seconds=300,
            )
            digest = hashlib.sha256(b"context").hexdigest()
            first = memory.claim_screen_companion_rule(
                rule_id, application="chrome.exe", context_sha256=digest
            )
            second = memory.claim_screen_companion_rule(
                rule_id, application="chrome.exe", context_sha256=digest
            )
            self.assertIsInstance(first, int)
            self.assertIsNone(second)
            self.assertTrue(
                memory.finish_screen_companion_receipt(
                    first, status="queued", job_id="safe-job"
                )
            )

    def test_atomic_controls_preserve_settings_and_fail_closed(self):
        with Memory(Path(":memory:")) as memory:
            memory.set_screen_companion_state(
                mode="suggest",
                paused=False,
                auto_suggest=True,
                excluded_apps=["private.exe"],
            )
            paused = memory.control_screen_companion_state(action="pause")
            self.assertEqual(paused["mode"], "suggest")
            self.assertTrue(paused["paused"])
            self.assertTrue(paused["auto_suggest"])
            self.assertEqual(paused["excluded_apps"], ["private.exe"])
            resumed = memory.control_screen_companion_state(action="resume")
            self.assertFalse(resumed["paused"])
            off = memory.control_screen_companion_state(action="off")
            self.assertEqual(off["mode"], "disabled")
            self.assertTrue(off["paused"])
            self.assertFalse(off["auto_suggest"])
            on = memory.control_screen_companion_state(action="on")
            self.assertEqual(on["mode"], "observe")
            self.assertFalse(on["paused"])
            self.assertEqual(on["excluded_apps"], ["private.exe"])
            changed = memory.control_screen_companion_state(
                action="mode", mode="collaborate"
            )
            self.assertEqual(changed["mode"], "collaborate")
            self.assertFalse(changed["paused"])
            with self.assertRaises(ValueError):
                memory.control_screen_companion_state(action="launch")
            with self.assertRaises(ValueError):
                memory.control_screen_companion_state(action="mode", mode="disabled")

    def test_receipts_never_store_titles_or_pixels(self):
        with Memory(Path(":memory:")) as memory:
            columns = {
                row["name"] for row in memory.db.execute(
                    "PRAGMA table_info(screen_companion_receipts)"
                )
            }
            self.assertFalse({"title", "window_title", "image", "pixels"} & columns)


class ScreenCompanionRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / "jarvis.db"
        self.observation = ScreenObservation(
            application="chrome.exe",
            title="Outline - Docs",
            observed_at=time.time(),
            context_sha256=hashlib.sha256(b"chrome\0outline").hexdigest(),
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_observe_mode_uses_metadata_only_and_never_dispatches(self):
        provider = _FakeProvider(self.observation)
        dispatched: list[tuple[dict, ScreenObservation]] = []

        def on_action(rule, observation):
            dispatched.append((rule, observation))
            return "job-1"

        with Memory(self.database) as memory:
            memory.set_screen_companion_state(
                mode="observe",
                paused=False,
                auto_suggest=False,
                excluded_apps=[],
            )
            memory.add_screen_companion_rule(
                trigger_app="chrome.exe",
                title_contains="outline",
                action_prompt="Research the outline topic and offer tips",
                action_mode="collaborate",
                cooldown_seconds=300,
            )
        companion = ScreenCompanion(
            self.database,
            provider=provider,
            on_action=on_action,
            poll_seconds=0.25,
            stable_seconds=0,
        )
        companion.start()
        try:
            time.sleep(0.6)
        finally:
            companion.stop()
        self.assertTrue(provider.capture_requests)
        self.assertFalse(any(provider.capture_requests))
        self.assertEqual(dispatched, [])

    def test_suggest_mode_downgrades_collaboration_rule(self):
        provider = _FakeProvider(self.observation)
        dispatched: list[tuple[dict, ScreenObservation]] = []
        ready = threading.Event()

        def on_action(rule, observation):
            dispatched.append((rule, observation))
            ready.set()
            return "job-1"

        with Memory(self.database) as memory:
            memory.set_screen_companion_state(
                mode="suggest",
                paused=False,
                auto_suggest=False,
                excluded_apps=[],
            )
            memory.add_screen_companion_rule(
                trigger_app="chrome.exe",
                title_contains="outline",
                action_prompt="Research the outline topic and offer tips",
                action_mode="collaborate",
                cooldown_seconds=300,
            )
        companion = ScreenCompanion(
            self.database,
            provider=provider,
            on_action=on_action,
            poll_seconds=0.25,
            stable_seconds=0,
        )
        companion.start()
        try:
            self.assertTrue(ready.wait(2))
        finally:
            companion.stop()
        self.assertEqual(dispatched[0][0]["action_mode"], "suggest")
        self.assertEqual(provider.capture_requests.count(True), 1)
        self.assertGreaterEqual(provider.capture_requests.count(False), 1)

    def test_idle_suggest_mode_polls_metadata_without_repeated_screenshots(self):
        provider = _FakeProvider(self.observation)
        with Memory(self.database) as memory:
            memory.set_screen_companion_state(
                mode="suggest",
                paused=False,
                auto_suggest=False,
                excluded_apps=[],
            )
        companion = ScreenCompanion(
            self.database,
            provider=provider,
            poll_seconds=0.25,
            stable_seconds=0,
        )
        companion.start()
        try:
            time.sleep(0.7)
        finally:
            companion.stop()
        self.assertTrue(provider.capture_requests)
        self.assertFalse(any(provider.capture_requests))

    def test_manual_suggestion_requires_opt_in_and_requests_active_window_pixels(self):
        provider = _FakeProvider(ScreenObservation(
            application=self.observation.application,
            title=self.observation.title,
            observed_at=self.observation.observed_at,
            context_sha256=self.observation.context_sha256,
            image=ImageAttachment(
                "image/png",
                base64.b64decode(
                    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
                ),
                "active.png",
            ),
        ))
        companion = ScreenCompanion(
            self.database,
            provider=provider,
            on_action=lambda _rule, _observation: "job-2",
        )
        with Memory(self.database) as memory:
            with self.assertRaises(PermissionError):
                companion.suggest_now()
            memory.set_screen_companion_state(
                mode="suggest",
                paused=False,
                auto_suggest=False,
                excluded_apps=[],
            )
        self.assertEqual(companion.suggest_now(), "job-2")
        self.assertEqual(provider.capture_requests, [True])

    def test_manual_suggestion_does_not_promise_work_without_visible_pixels(self):
        provider = _FakeProvider(self.observation)
        companion = ScreenCompanion(
            self.database,
            provider=provider,
            on_action=lambda *_args: self.fail("unseen work must not be offered"),
        )
        with Memory(self.database) as memory:
            memory.set_screen_companion_state(
                mode="suggest",
                paused=False,
                auto_suggest=False,
                excluded_apps=[],
            )
        with self.assertRaisesRegex(RuntimeError, "could not be read"):
            companion.suggest_now()

    def test_excluded_application_cannot_be_suggested(self):
        provider = _FakeProvider(self.observation)
        companion = ScreenCompanion(self.database, provider=provider)
        with Memory(self.database) as memory:
            memory.set_screen_companion_state(
                mode="suggest",
                paused=False,
                auto_suggest=False,
                excluded_apps=["chrome.exe"],
            )
        with self.assertRaises(PermissionError):
            companion.suggest_now()

    def test_sensitive_window_title_is_hidden_before_capture(self):
        provider = WindowsForegroundProvider()
        provider._window_identity = lambda: (1, "chrome.exe", "Password login")
        def get_rect(_handle, pointer):
            rect = ctypes.cast(
                pointer, ctypes.POINTER(ctypes.c_long * 4)
            ).contents
            rect[:] = (0, 0, 800, 600)
            return 1
        provider._user32 = type("User32", (), {"GetWindowRect": staticmethod(get_rect)})()
        provider._capture_window = lambda _handle: self.fail("pixels must not be captured")
        observation = provider.observe(capture_pixels=True, excluded_apps=set())
        self.assertTrue(observation.excluded)
        self.assertEqual(observation.title, "Sensitive window hidden")
        self.assertIsNone(observation.image)

    def test_native_indicator_window_is_never_observed(self):
        provider = WindowsForegroundProvider()
        provider._window_identity = lambda: (1, "pythonw.exe", COMPANION_INDICATOR_TITLE)
        def get_rect(_handle, pointer):
            rect = ctypes.cast(pointer, ctypes.POINTER(ctypes.c_long * 4)).contents
            rect[:] = (0, 0, 400, 90)
            return 1
        provider._user32 = type("User32", (), {"GetWindowRect": staticmethod(get_rect)})()
        provider._capture_window = lambda _handle: self.fail("indicator pixels must not be captured")
        observation = provider.observe(capture_pixels=True, excluded_apps=set())
        self.assertTrue(observation.excluded)
        self.assertEqual(observation.title, "Sensitive window hidden")
        self.assertEqual(
            observation.exclusion_reason,
            "Jarvis Companion indicator is excluded",
        )

    def test_window_capture_uses_hwnd_target_not_desktop_rectangle(self):
        with mock.patch(
            "PIL.ImageGrab.grab",
            return_value=Image.new("RGB", (32, 24), "white"),
        ) as grab:
            attachment = WindowsForegroundProvider._capture_window(123)
        self.assertIsNotNone(attachment)
        grab.assert_called_once_with(window=123)

    def test_uniform_black_window_capture_fails_closed(self):
        with mock.patch(
            "PIL.ImageGrab.grab",
            return_value=Image.new("RGB", (32, 24), "black"),
        ):
            self.assertIsNone(WindowsForegroundProvider._capture_window(123))

    def test_capture_is_discarded_when_foreground_changes_during_capture(self):
        provider = WindowsForegroundProvider()
        safe = {
            "handle": 1,
            "application": "editor.exe",
            "title": "Outline",
            "context_sha256": "1" * 64,
            "excluded": False,
            "exclusion_reason": None,
        }
        changed = {
            "handle": 2,
            "application": "vault.exe",
            "title": "Sensitive window hidden",
            "context_sha256": "2" * 64,
            "excluded": True,
            "exclusion_reason": "application is excluded",
        }
        contexts = iter((safe, changed))
        provider.active_context = lambda **_kwargs: next(contexts)
        provider._capture_window = lambda _handle: ImageAttachment(
            "image/png", b"\x89PNG\r\n\x1a\nplaceholder", "window.png"
        )
        observation = provider.observe(capture_pixels=True, excluded_apps=set())
        self.assertIsNotNone(observation)
        self.assertIsNone(observation.image)

    def test_clear_current_drops_observation_immediately(self):
        companion = ScreenCompanion(self.database, provider=_FakeProvider(self.observation))
        with companion._lock:
            companion._current = self.observation
            companion._stable_since = time.time()
        companion.clear_current()
        with companion._lock:
            self.assertIsNone(companion._current)
            self.assertEqual(companion._stable_since, 0.0)


if __name__ == "__main__":
    unittest.main()
