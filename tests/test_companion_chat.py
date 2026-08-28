import unittest

from jarvis.companion_chat import (
    CompanionChatIntent,
    public_screen_companion_state,
    render_screen_companion_learning_state,
    render_screen_companion_state,
    screen_companion_chat_intent,
)


class CompanionChatIntentTests(unittest.TestCase):
    def test_status_and_control_speech_acts_are_resolved(self):
        cases = {
            "could you tell me whether Screen Companion is active?": CompanionChatIntent("status"),
            "is Screen Companion paused?": CompanionChatIntent("status"),
            "turn Screen Companion on": CompanionChatIntent("on"),
            "please turn Screen Companion off": CompanionChatIntent("off"),
            "could you pause Screen Companion?": CompanionChatIntent("pause"),
            "would you resume Screen Companion?": CompanionChatIntent("resume"),
            "set Screen Companion mode to observe": CompanionChatIntent("mode", "observe"),
            "switch Screen Companion to suggest mode": CompanionChatIntent("mode", "suggest"),
            "put Screen Companion into collaborate mode": CompanionChatIntent(
                "mode", "collaborate"
            ),
            "can you see my screen right now?": CompanionChatIntent("status"),
            "what mode is companion in?": CompanionChatIntent("status"),
            "what has Screen Companion learned from recent feedback?": (
                CompanionChatIntent("learning_status")
            ),
            "what has Screen Companion learned?": CompanionChatIntent(
                "learning_status"
            ),
        }
        for prompt, expected in cases.items():
            with self.subTest(prompt=prompt):
                self.assertEqual(screen_companion_chat_intent(prompt), expected)

    def test_short_referential_control_requires_recent_companion_context(self):
        recent = [{
            "role": "assistant",
            "content": "Screen Companion is on in Observe mode.",
        }]
        self.assertEqual(
            screen_companion_chat_intent("turn it off", recent),
            CompanionChatIntent("off"),
        )
        self.assertEqual(
            screen_companion_chat_intent("switch to suggest mode", recent),
            CompanionChatIntent("mode", "suggest"),
        )
        self.assertIsNone(screen_companion_chat_intent("turn it off"))

    def test_non_commands_never_become_control_actions(self):
        cases = (
            "don't turn Screen Companion off",
            'How would I say "turn Screen Companion off"?',
            "what happens if I turn Screen Companion off?",
            "should I turn Screen Companion off?",
            "is companion set to observe mode?",
            "tell me how to turn Screen Companion on",
            "can Screen Companion turn itself off?",
            "why did you turn companion off?",
            "What does companion mode mean in this game?",
            "what do you think of companion animals?",
            "is my companion animal learning yet?",
            "I'm observing that he seems off today—what do you think?",
            "turn companion off and summarize the repository",
        )
        for prompt in cases:
            with self.subTest(prompt=prompt):
                self.assertIsNone(screen_companion_chat_intent(prompt))

    def test_conflicting_and_invalid_modes_request_clarification(self):
        self.assertEqual(
            screen_companion_chat_intent("turn Screen Companion on and off"),
            CompanionChatIntent("ambiguous"),
        )
        self.assertEqual(
            screen_companion_chat_intent("set Screen Companion mode to turbo"),
            CompanionChatIntent("invalid_mode", "turbo"),
        )

    def test_public_state_is_bounded_and_reports_liveness_honestly(self):
        public = public_screen_companion_state({
            "mode": "suggest",
            "paused": False,
            "auto_suggest": True,
            "available": False,
            "last_error": "provider unavailable",
            "window_title": "private title",
            "rules": [{"action_prompt": "private"}],
            "excluded_apps": ["private.exe"],
            "pixels": "secret",
            "learning": {
                "feedback": "4",
                "accepted": 2,
                "dismissed": 2,
                "verified_outcomes": 1,
                "reusable_outcomes": 1,
                "private_text": "do not expose",
            },
        })
        self.assertEqual(public["mode"], "suggest")
        self.assertFalse(public["available"])
        self.assertTrue(public["has_runtime_error"])
        for private_key in ("window_title", "rules", "excluded_apps", "pixels"):
            self.assertNotIn(private_key, public)
        rendered = render_screen_companion_state(public, changed=False)
        self.assertIn("configured for Suggest mode", rendered)
        self.assertIn("unavailable", rendered)
        self.assertNotIn("private", rendered)
        learned = render_screen_companion_learning_state(public)
        self.assertIn("4 explicit feedback signals", learned)
        self.assertIn("1 verified reusable category signal", learned)
        self.assertNotIn("do not expose", learned)

    def test_empty_learning_state_is_explicit_about_observe_mode(self):
        rendered = render_screen_companion_learning_state({"mode": "observe"})
        self.assertIn("Not yet", rendered)
        self.assertIn("Observe mode intentionally does not train", rendered)


if __name__ == "__main__":
    unittest.main()
