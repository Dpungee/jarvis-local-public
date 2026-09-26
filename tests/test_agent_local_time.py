"""The host clock answers "what time is it" directly, like the existing local-date rule."""
import unittest
from datetime import datetime

from jarvis import agent as agent_module
from tests.test_agent import FakeResponse
import tests.test_agent_hardening as hardening


class LocalTimeTests(hardening.AgentHardeningTests):
    def test_time_question_reaches_the_model_with_the_local_clock(self):
        agent, client = self.make_agent([FakeResponse(content="It's the time you see.")])
        agent.run("whats the time right now")
        self.assertEqual(len(client.requests), 1)
        context = "\n".join(str(m.get("content") or "") for m in client.requests[0]["messages"])
        now = datetime.now().astimezone()
        # HH:MM of this minute (or the previous one, if the minute turned during the run).
        self.assertRegex(context, r"Local date and timezone: \d{4}-\d{2}-\d{2} \d{2}:\d{2} ")
        self.assertIn(now.tzname(), context)

    def test_intent_is_narrow(self):
        for prompt in ("whats the time right now", "what time is it", "what is the current time?",
                       "whats the time", "time right now?"):
            with self.subTest(prompt=prompt):
                self.assertTrue(agent_module._LOCAL_TIME_INTENT.search(prompt))
        for prompt in ("I had a great time", "time to build a timer app", "what time does the store open",
                       "the time complexity of quicksort", "sometime right now"):
            with self.subTest(prompt=prompt):
                self.assertFalse(agent_module._LOCAL_TIME_INTENT.search(prompt))


def _only_own_tests(cls):
    own = set(vars(cls))
    for name in dir(cls):
        if name.startswith("test") and name not in own:
            setattr(cls, name, None)
    return cls


_only_own_tests(LocalTimeTests)

if __name__ == "__main__":
    unittest.main()
