from __future__ import annotations

import queue
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from jarvis.ui import (
    MODEL_CHOICES,
    JarvisSession,
    compact_activity,
    model_override_for,
    safe_ui_text,
)


class _Result(str):
    status = "complete"
    reason = None
    approval_id = None


class _FakeMemory:
    def __init__(self, _path):
        self.closed = False
        self.conversation = 0
        self.decisions = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.closed = True

    def new_conversation(self, _title):
        self.conversation += 1
        return self.conversation

    def control_state(self):
        return {"state": "paused"}

    def activity_count_since(self, _category, _since):
        return 0

    def list_approvals(self, limit=100):
        del limit
        return [{"id": 4, "status": "pending", "reason": "test"}]

    def decide_approval(self, approval_id, approve, ttl_hours=24):
        self.decisions.append((approval_id, approve, ttl_hours))
        return True


class _FakeAgent:
    calls = []

    def __init__(self, _config, _memory, on_event):
        self.on_event = on_event

    def run(self, prompt, **kwargs):
        type(self).calls.append((prompt, kwargs))
        self.on_event("model - qwen3.5:9b - quick/general task")
        return _Result("Desktop response")


class UiHelpersTests(unittest.TestCase):
    def test_model_labels_resolve_only_to_bounded_profiles(self):
        self.assertEqual(MODEL_CHOICES[-1], "Deep 30B")
        self.assertEqual(model_override_for("Fast"), "fast")
        self.assertEqual(model_override_for("Deep 30B"), "deep")
        self.assertEqual(model_override_for("unknown/provider:model"), "auto")

    def test_ui_text_is_redacted_bounded_and_control_safe(self):
        secret = "sk-proj-" + "A" * 32
        rendered = safe_ui_text(f"token={secret}\x00\n" + "x" * 100, 60)

        self.assertNotIn(secret, rendered)
        self.assertNotIn("\x00", rendered)
        self.assertLessEqual(len(rendered), 60)
        self.assertIn("display truncated", rendered)

    def test_activity_is_single_line_and_bounded(self):
        rendered = compact_activity("processing\nstep 1 " + "x" * 200, 40)
        self.assertNotIn("\n", rendered)
        self.assertLessEqual(len(rendered), 40)

    def test_session_keeps_memory_and_agent_on_worker_thread(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = SimpleNamespace(
                data_dir=Path(temporary),
                fast_model="qwen3.5:9b",
                reasoning_model="gemma4:12b-it-qat",
                coding_model="gemma4:12b-it-qat",
                deep_model="qwen3-coder:30b",
                proactive_max_task_seconds=1800,
                daily_tool_limit=500,
                approval_ttl_hours=24,
            )
            _FakeAgent.calls.clear()
            with patch("jarvis.ui.Memory", _FakeMemory), patch("jarvis.ui.Agent", _FakeAgent):
                session = JarvisSession(config)
                session.start()
                ready = session.events.get(timeout=2)
                self.assertEqual(ready.kind, "ready")
                self.assertEqual(ready.payload["control_state"], "paused")

                session.submit("hello desktop", "Fast")
                received = []
                while not any(event.kind == "assistant" for event in received):
                    try:
                        received.append(session.events.get(timeout=2))
                    except queue.Empty as exc:
                        self.fail(f"desktop session did not answer: {exc}")

                answer = next(event for event in received if event.kind == "assistant")
                self.assertEqual(answer.payload["content"], "Desktop response")
                self.assertEqual(_FakeAgent.calls[0][0], "hello desktop")
                self.assertEqual(_FakeAgent.calls[0][1]["model_override"], "fast")
                self.assertEqual(_FakeAgent.calls[0][1]["conversation_id"], 1)

                session.shutdown()
                session.join(timeout=2)
                self.assertFalse(session.is_alive())


if __name__ == "__main__":
    unittest.main()
