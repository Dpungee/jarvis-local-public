from __future__ import annotations

import unittest
from pathlib import Path

from jarvis.memory import Memory


class MemoryRelevanceTests(unittest.TestCase):
    @staticmethod
    def _remember(memory: Memory, content: str, *, kind: str = "fact") -> None:
        memory.remember_verified(
            content,
            kind,
            "verified relevance fixture",
            origin="verified_import",
        )

    def test_broad_coverage_outranks_newer_partial_matches(self):
        with Memory(Path(":memory:")) as memory:
            self._remember(
                memory,
                "Python services use SQLite busy retries with bounded exponential backoff.",
                kind="fact",
            )
            for index in range(20):
                self._remember(
                    memory,
                    f"Newer Python note {index} with unrelated formatting advice.",
                    kind="fact",
                )

            results = memory.search("Python SQLite retry backoff", limit=3)

            self.assertEqual(
                results[0]["content"],
                "Python services use SQLite busy retries with bounded exponential backoff.",
            )
            self.assertEqual(len(results), 3)

    def test_exact_phrase_outranks_newer_reordered_full_match(self):
        with Memory(Path(":memory:")) as memory:
            self._remember(memory, "Ollama context window configuration", kind="fact")
            self._remember(
                memory,
                "Configuration guidance for the context used by an Ollama window.",
                kind="fact",
            )

            results = memory.search("Ollama context window configuration")

            self.assertEqual(results[0]["content"], "Ollama context window configuration")

    def test_case_punctuation_and_plural_forms_are_normalized(self):
        with Memory(Path(":memory:")) as memory:
            self._remember(
                memory,
                "Agent memory retrieval policy for several local models.",
                kind="fact",
            )
            self._remember(memory, "A newer retrieval note.", kind="fact")

            results = memory.search("AGENTS' memories, retrieval policies; models?")

            self.assertEqual(
                results[0]["content"],
                "Agent memory retrieval policy for several local models.",
            )

            self._remember(
                memory,
                "Durable memories retain user policies across sessions.",
                kind="fact",
            )
            singular_query = memory.search("durable memory policy")
            self.assertEqual(
                singular_query[0]["content"],
                "Durable memories retain user policies across sessions.",
            )

    def test_equal_relevance_still_prefers_the_newer_memory_and_honors_limit(self):
        with Memory(Path(":memory:")) as memory:
            for index in range(5):
                self._remember(
                    memory, f"Ollama retrieval sentinel {index}", kind="fact"
                )

            results = memory.search("Ollama retrieval", limit=2)

            self.assertEqual(
                [item["content"] for item in results],
                ["Ollama retrieval sentinel 4", "Ollama retrieval sentinel 3"],
            )


if __name__ == "__main__":
    unittest.main()
