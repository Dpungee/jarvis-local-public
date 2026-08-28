from __future__ import annotations

import tempfile
import unittest
import hashlib
import time
from pathlib import Path

from jarvis.embodied_presence import (
    ContextClass,
    EmbodiedPresence,
    EmbodimentIntent,
    PresenceEvent,
    PresenceMode,
    VoicePresenceLoop,
)
from jarvis.relationship_memory import RelationshipMemory
from jarvis.presence_bridge import ScreenPresenceBridge
from jarvis.screen_companion import ScreenObservation


class _Driver:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def apply_intent(self, intent: str, payload: dict) -> None:
        self.events.append((intent, payload))


class EmbodiedPresenceTests(unittest.TestCase):
    def test_studio_requires_confirmation_and_accepts_public_context_only(self):
        driver = _Driver()
        presence = EmbodiedPresence(driver)
        with self.assertRaises(PermissionError):
            presence.set_mode(PresenceMode.STUDIO)
        presence.set_mode(PresenceMode.STUDIO, operator_confirmed=True)
        presence.dispatch(PresenceEvent(
            EmbodimentIntent.ACKNOWLEDGE,
            ContextClass.PUBLIC,
            {"emotion": "warm"},
        ))
        with self.assertRaises(PermissionError):
            presence.dispatch(PresenceEvent(
                EmbodimentIntent.SPEAK,
                ContextClass.RELATIONSHIP,
                {"text": "private shared memory"},
            ))
        self.assertEqual(driver.events, [("acknowledge", {"emotion": "warm"})])

    def test_raw_screen_credentials_and_joint_controls_are_rejected(self):
        presence = EmbodiedPresence(_Driver(), mode=PresenceMode.OPERATOR)
        with self.assertRaises(PermissionError):
            presence.dispatch(PresenceEvent(
                EmbodimentIntent.THINK,
                ContextClass.RAW_SCREEN,
                {},
            ))
        with self.assertRaises(PermissionError):
            presence.dispatch(PresenceEvent(
                EmbodimentIntent.POINT,
                ContextClass.PUBLIC,
                {"joints": "head:90"},
            ))
        with self.assertRaises(PermissionError):
            presence.dispatch(PresenceEvent(
                EmbodimentIntent.SPEAK,
                ContextClass.PUBLIC,
                {"text": "API_KEY=sk-proj-" + "A" * 40},
            ))

    def test_voice_barge_in_cancels_speech_before_listening(self):
        driver = _Driver()
        cancelled: list[bool] = []
        voice = VoicePresenceLoop(
            EmbodiedPresence(driver),
            cancel_speech=lambda: cancelled.append(True),
        )
        voice.speaking("Hello")
        voice.listening()
        self.assertEqual(cancelled, [True])
        self.assertEqual(voice.state, "listening")
        self.assertEqual([event[0] for event in driver.events], ["speak", "listen"])

    def test_screen_bridge_forwards_summary_but_never_pixels(self):
        driver = _Driver()
        presence = EmbodiedPresence(driver, mode=PresenceMode.COMPANION)
        bridge = ScreenPresenceBridge(presence)
        observation = ScreenObservation(
            application="code.exe",
            title="Outline",
            observed_at=time.time(),
            context_sha256=hashlib.sha256(b"outline").hexdigest(),
            image=object(),
        )
        bridge.speak_summary(observation, "The outline could use a clearer thesis.")
        self.assertEqual(driver.events[0][0], "speak")
        self.assertNotIn("image", driver.events[0][1])
        self.assertNotIn("pixels", driver.events[0][1])

    def test_screen_bridge_rejects_excluded_windows_and_studio_mode(self):
        observation = ScreenObservation(
            application="vault.exe",
            title="Sensitive window hidden",
            observed_at=time.time(),
            context_sha256=hashlib.sha256(b"hidden").hexdigest(),
            excluded=True,
            exclusion_reason="application is excluded",
        )
        companion = ScreenPresenceBridge(
            EmbodiedPresence(_Driver(), mode=PresenceMode.COMPANION)
        )
        with self.assertRaises(PermissionError):
            companion.speak_summary(observation, "Do not reveal this")
        studio_presence = EmbodiedPresence(_Driver())
        studio_presence.set_mode(PresenceMode.STUDIO, operator_confirmed=True)
        visible = ScreenObservation(
            application="code.exe",
            title="Outline",
            observed_at=time.time(),
            context_sha256=hashlib.sha256(b"visible").hexdigest(),
        )
        with self.assertRaises(PermissionError):
            ScreenPresenceBridge(studio_presence).speak_summary(
                visible, "Private work summary"
            )


class RelationshipMemoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "relationship.db"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_relationship_memory_is_separate_and_not_visible_to_studio(self):
        with RelationshipMemory(self.path) as memory:
            memory.remember(
                kind="tone_preference",
                subject="conversation",
                value="Keep casual conversations relaxed",
            )
            self.assertEqual(len(memory.list_for_mode(PresenceMode.COMPANION)), 1)
            self.assertEqual(memory.list_for_mode(PresenceMode.STUDIO), [])

    def test_public_memory_requires_confirmation(self):
        with RelationshipMemory(self.path) as memory:
            with self.assertRaises(PermissionError):
                memory.remember(
                    kind="joke",
                    subject="public callback",
                    value="The approved running joke",
                    visibility="studio",
                )
            memory.remember(
                kind="joke",
                subject="public callback",
                value="The approved running joke",
                visibility="studio",
                confirmed_public=True,
            )
            visible = memory.list_for_mode(PresenceMode.STUDIO)
            self.assertEqual(visible[0]["value"], "The approved running joke")

    def test_superseding_is_visible_and_forgetting_removes_content(self):
        with RelationshipMemory(self.path) as memory:
            first = memory.remember(
                kind="address_preference",
                subject="operator",
                value="Call me Max",
            )
            second = memory.remember(
                kind="address_preference",
                subject="operator",
                value="Call me M",
            )
            visible = memory.list_for_mode(PresenceMode.COMPANION)
            self.assertEqual([item["value"] for item in visible], ["Call me M"])
            self.assertEqual(len(memory.history("address_preference", "operator")), 2)
            self.assertTrue(memory.forget(second))
            self.assertTrue(memory.forget(first))
            self.assertEqual(memory.history("address_preference", "operator"), [])

    def test_secrets_and_operational_claims_are_rejected(self):
        with RelationshipMemory(self.path) as memory:
            with self.assertRaises(ValueError):
                memory.remember(
                    kind="credential",
                    subject="github",
                    value="not allowed",
                )
            with self.assertRaises(ValueError):
                memory.remember(
                    kind="important_project",
                    subject="deploy",
                    value="token=ghp_" + "A" * 40,
                )


if __name__ == "__main__":
    unittest.main()
