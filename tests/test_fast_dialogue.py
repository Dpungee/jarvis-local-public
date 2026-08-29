from __future__ import annotations

import unittest
from datetime import datetime, timezone

from jarvis import agent
from jarvis.fast_dialogue import (
    instant_casual_reply,
    instant_local_time_reply,
    is_local_time_request,
    simple_fraction_comparison_reply,
)


class FastDialogueExtractionTests(unittest.TestCase):
    def test_agent_compatibility_exports_are_preserved(self):
        self.assertIs(agent._instant_casual_reply, instant_casual_reply)
        self.assertIs(agent._instant_local_time_reply, instant_local_time_reply)
        self.assertIs(agent._is_local_time_request, is_local_time_request)
        self.assertIs(
            agent._simple_fraction_comparison_reply,
            simple_fraction_comparison_reply,
        )

    def test_instant_outputs_remain_deterministic(self):
        fixed = datetime(2026, 8, 29, 9, 5, tzinfo=timezone.utc)
        self.assertEqual(
            instant_local_time_reply("what time is it", now=fixed),
            "It’s 9:05 AM UTC on Saturday, August 29, 2026.",
        )
        self.assertEqual(
            simple_fraction_comparison_reply("which is larger: 2/3 or 3/5"),
            "2/3 > 3/5 because 2×5 = 10 > 9 = 3×3.",
        )
        self.assertEqual(
            instant_casual_reply("yo jarvis whats good"),
            "What's up, bro? Ready when you are.",
        )

    def test_non_clock_requests_stay_out_of_the_fast_path(self):
        self.assertFalse(is_local_time_request("what time is it in Tokyo"))
        self.assertIsNone(instant_local_time_reply("what time is it in Tokyo"))


if __name__ == "__main__":
    unittest.main()
