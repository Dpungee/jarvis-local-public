from __future__ import annotations

import sqlite3
import unittest
from pathlib import Path

import jarvis.memory as memory_facade
from jarvis.memory import Memory
from jarvis import memory_retrieval


class MemoryRetrievalModuleTests(unittest.TestCase):
    def test_memory_module_preserves_retrieval_compatibility_aliases(self) -> None:
        self.assertEqual(
            memory_facade.MAX_MEMORY_QUERY_TERMS,
            memory_retrieval.MAX_MEMORY_QUERY_TERMS,
        )
        self.assertEqual(
            memory_facade.MAX_MEMORY_SEARCH_CANDIDATES,
            memory_retrieval.MAX_MEMORY_SEARCH_CANDIDATES,
        )
        for name in (
            "_normalize_memory_token",
            "_memory_tokens",
            "_memory_query_terms",
            "_memory_like_terms",
            "_memory_fts_query",
            "_rank_memory_rows",
        ):
            self.assertIs(getattr(memory_facade, name), getattr(memory_retrieval, name))

    def test_query_terms_remain_bounded_ordered_and_wildcard_literal(self) -> None:
        query = "Please alpha% beta_beta policies policies gamma delta epsilon zeta eta theta"
        terms = memory_retrieval._memory_query_terms(query)

        self.assertEqual(
            terms,
            ["alpha", "beta", "policy", "gamma", "delta", "epsilon", "zeta", "theta"],
        )
        self.assertEqual(
            memory_retrieval._memory_like_terms(query, terms),
            [
                "alpha%",
                "beta_beta",
                "policy",
                "policies",
                "gamma",
                "delta",
                "epsilon",
                "zeta",
                "theta",
            ],
        )
        self.assertIsNone(memory_retrieval._memory_fts_query(query, terms))
        self.assertEqual(
            memory_retrieval._memory_fts_query("alpha beta", ["alpha", "beta"]),
            '"alpha" OR "alphas" OR "beta" OR "betas"',
        )

    def test_ranker_preserves_exact_phrase_utility_and_newer_tie_breaking(self) -> None:
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        try:
            connection.execute(
                """CREATE TABLE candidates(
                       id INTEGER PRIMARY KEY,
                       content TEXT NOT NULL,
                       utility_resolved INTEGER,
                       utility_successes INTEGER
                   )"""
            )
            connection.executemany(
                "INSERT INTO candidates(id, content, utility_resolved, utility_successes) "
                "VALUES (?, ?, ?, ?)",
                (
                    (1, "alpha beta", 10, 10),
                    (2, "alpha beta", 0, 0),
                    (3, "beta alpha", 10, 10),
                    (4, "alpha only", 10, 10),
                ),
            )
            rows = connection.execute(
                "SELECT id, content, utility_resolved, utility_successes FROM candidates"
            ).fetchall()

            ranked = memory_retrieval._rank_memory_rows(
                rows,
                ["alpha", "beta"],
                keep_id=True,
            )

            self.assertEqual([item["memory_id"] for item in ranked], [1, 2, 3, 4])
            self.assertNotIn("utility_resolved", ranked[0])
            self.assertNotIn("utility_successes", ranked[0])
        finally:
            connection.close()

    def test_memory_search_still_escapes_sql_wildcards(self) -> None:
        with Memory(Path(":memory:")) as memory:
            memory.remember_verified(
                "Literal project marker abc%xyz.",
                "fact",
                "retrieval compatibility fixture",
                origin="verified_import",
            )
            memory.remember_verified(
                "Different project marker abcQxyz.",
                "fact",
                "retrieval compatibility fixture",
                origin="verified_import",
            )

            results = memory.search("abc%xyz")

            self.assertEqual(
                [item["content"] for item in results],
                ["Literal project marker abc%xyz."],
            )


if __name__ == "__main__":
    unittest.main()
