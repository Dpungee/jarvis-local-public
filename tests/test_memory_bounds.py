from __future__ import annotations

import unittest
from pathlib import Path

from jarvis.memory import Memory


class MemoryInputBoundsTests(unittest.TestCase):
    def test_persisted_inputs_are_rejected_or_clipped_at_documented_bounds(self):
        with Memory(Path(":memory:")) as memory:
            conversation = memory.new_conversation("t" * 500)
            title = memory.db.execute(
                "SELECT title FROM conversations WHERE id=?", (conversation,)
            ).fetchone()[0]
            self.assertEqual(len(title), 120)

            with self.assertRaises(ValueError):
                memory.add_message(conversation, "tool", "not persistable")
            memory.add_message(conversation, "user", "m" * 120_000)
            persisted = memory.recent_messages(conversation)[0]["content"]
            self.assertLessEqual(len(persisted), 100_000)
            self.assertTrue(persisted.endswith("[message clipped before persistence]"))

            memory.remember("c" * 9000, "k" * 100, "s" * 3000)
            saved = memory.list_memories()[0]
            self.assertEqual(len(saved["content"]), 8000)
            self.assertEqual(len(saved["kind"]), 40)
            self.assertEqual(len(saved["source"]), 2000)

            for prompt in ("", "   ", "p" * 50_001):
                with self.subTest(prompt_length=len(prompt)), self.assertRaises(ValueError):
                    memory.add_task(prompt)
            with self.assertRaises(ValueError):
                memory.add_task("valid", idempotency_key="k" * 501)
            low_task = memory.add_task("low attempts", max_attempts=0)
            high_task = memory.add_task("high attempts", max_attempts=1000)
            tasks = {row["id"]: row for row in memory.list_tasks()}
            self.assertEqual(tasks[low_task]["max_attempts"], 1)
            self.assertEqual(tasks[high_task]["max_attempts"], 100)

            for topic in ("", "   ", "t" * 501):
                with self.subTest(topic_length=len(topic)), self.assertRaises(ValueError):
                    memory.add_learning_topic(topic)
            memory.add_learning_topic("minimum interval", 0)
            memory.add_learning_topic("maximum interval", 100_000)
            topics = {
                row["topic"]: row["interval_hours"]
                for row in memory.list_learning_topics()
            }
            self.assertEqual(topics["minimum interval"], 1)
            self.assertEqual(topics["maximum interval"], 8760)

    def test_scheduled_learning_prompt_requires_current_authoritative_cited_research(self):
        with Memory(Path(":memory:")) as memory:
            topic_id = memory.add_learning_topic("durable local agents", 12)
            self.assertEqual(memory.queue_due_learning(), 1)
            task = memory.list_tasks()[0]
            run = memory.list_learning_runs()[0]

            self.assertEqual(
                task["prompt"],
                "Continuously learn about this topic: durable local agents. "
                "Research current, authoritative sources; compare the evidence; "
                "and return a concise dated brief with exact source URLs.",
            )
            self.assertEqual(
                task["idempotency_key"],
                f"learning:{topic_id}:{run['scheduled_for']}",
            )
            self.assertEqual(task["max_attempts"], 3)
            self.assertEqual(run["topic_id"], topic_id)
            self.assertEqual(run["task_id"], task["id"])


if __name__ == "__main__":
    unittest.main()
