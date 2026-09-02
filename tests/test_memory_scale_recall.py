"""Recall at scale: staged lexical discovery, claim-anchor derivation,
rank-ordered semantic eligibility, and the per-store recall cache.

These tests pin three properties.  A growing store must never turn a
well-formed query into a silent empty answer merely because it contains an
everyday word.  Narrowing must never substitute a look-alike or generic record
for an identity the store has not seen, and rows outside the caller's project
must not be able to change what the caller recalls.  And every optimization is
result-preserving: the abstention rules, the identity-conflict shadowing, and
the claim matcher return exactly what the slower code returned.
"""
from __future__ import annotations

import json
import random
import unittest
from pathlib import Path
from unittest.mock import patch

from jarvis import memory as memory_module
from jarvis import memory_retrieval
from jarvis.memory import (
    _CLAIM_COMPOUND_PREFIXES,
    Memory,
    _claim_matched_query_terms,
    _claim_term_root,
)
from jarvis.memory_retrieval import RecallCache
from jarvis.vault import VaultNote


def _remember(memory: Memory, content: str) -> str:
    return memory.remember_verified(
        content, "fact", "verified scale fixture", origin="verified_import"
    )


def _project_fact_command(subject: str, predicate: str, value: str) -> str:
    payload = {"subject": subject, "predicate": predicate, "value": value}
    return "Remember this project fact: " + json.dumps(
        payload, ensure_ascii=False, separators=(",", ":")
    )


class StagedLexicalDiscoveryTests(unittest.TestCase):
    def test_everyday_word_no_longer_silences_a_unique_record(self) -> None:
        with Memory(Path(":memory:")) as memory:
            for index in range(6):
                _remember(memory, f"The project printer was serviced on day {index}.")
            _remember(memory, "The zephyr calibration constant is 88 microns.")
            with patch("jarvis.memory.MAX_MEMORY_SEARCH_CANDIDATES", 4):
                results = memory.search("zephyr project calibration", limit=3)
                report = memory.recall_report()
        self.assertEqual(
            [item["content"] for item in results],
            ["The zephyr calibration constant is 88 microns."],
        )
        self.assertEqual(report["mode"], "narrowed")
        self.assertEqual(report["dropped_terms"], [])
        self.assertEqual(report["unknown_terms"], [])
        self.assertEqual(report["candidates"], 1)
        self.assertFalse(report["abstained"])

    def test_all_everyday_words_fall_back_to_records_matching_every_term(self) -> None:
        with Memory(Path(":memory:")) as memory:
            for index in range(5):
                _remember(memory, f"The project budget was reviewed on day {index}.")
                _remember(memory, f"The schedule board was refreshed on day {index}.")
            _remember(memory, "The project schedule moves to Thursday.")
            with patch("jarvis.memory.MAX_MEMORY_SEARCH_CANDIDATES", 4):
                results = memory.search("project schedule", limit=3)
                report = memory.recall_report()
        self.assertEqual(
            [item["content"] for item in results],
            ["The project schedule moves to Thursday."],
        )
        self.assertEqual(report["mode"], "all-terms")
        self.assertEqual(sorted(report["dropped_terms"]), ["project", "schedule"])
        self.assertFalse(report["abstained"])

    def test_single_everyday_word_still_abstains_when_it_cannot_discriminate(self) -> None:
        with Memory(Path(":memory:")) as memory:
            for index in range(6):
                _remember(memory, f"The project printer was serviced on day {index}.")
            with patch("jarvis.memory.MAX_MEMORY_SEARCH_CANDIDATES", 4):
                self.assertEqual(memory.search("project", limit=3), [])
                report = memory.recall_report()
        self.assertEqual(report["mode"], "overflow")
        self.assertTrue(report["abstained"])
        self.assertEqual(report["dropped_terms"], ["project"])

    def test_unknown_identity_abstains_instead_of_substituting_under_narrowing(self) -> None:
        # The look-alike record is lower-case, so the capitalised-identity
        # sibling rule does not fire and only the ranker's conflict shadow
        # could catch it - which it cannot when narrowing drops that record.
        with Memory(Path(":memory:")) as memory:
            for index in range(6):
                _remember(memory, f"The project ledger was balanced on day {index}.")
            _remember(memory, "the northalderwick project ratio is one to four.")
            _remember(memory, "The project ratio is one to four.")
            with patch("jarvis.memory.MAX_MEMORY_SEARCH_CANDIDATES", 4):
                self.assertEqual(
                    memory.search("SouthAlderwick project ratio", limit=3), []
                )
                report = memory.recall_report()
                self.assertEqual(
                    memory.hybrid_memory_search(
                        "SouthAlderwick project ratio",
                        [1.0, 0.0],
                        "scale-test",
                        limit=3,
                    ),
                    [],
                )
        self.assertEqual(report["mode"], "identity-unbound")
        self.assertTrue(report["abstained"])
        self.assertEqual(report["unknown_terms"], ["southalderwick"])
        self.assertEqual(report["dropped_terms"], [])

    def test_real_cap_unknown_identity_never_returns_a_generic_record(self) -> None:
        # No patched cap: exercise the production candidate limit.
        nouns = ["printer", "kitchen", "archive", "garden", "server", "invoice"]
        rng = random.Random(3)
        with Memory(Path(":memory:")) as memory:
            for index in range(memory_module.MAX_MEMORY_SEARCH_CANDIDATES + 50):
                _remember(
                    memory,
                    f"The project {rng.choice(nouns)} was updated on day {index}.",
                )
            _remember(memory, "the northalderwick project ratio is one to four.")
            _remember(memory, "The project ratio is one to four.")
            for query in (
                "SouthAlderwick project ratio",
                "SouthAlderwick project ratio one four",
                "project ratio for CASE-9931",
            ):
                with self.subTest(query=query):
                    self.assertEqual(memory.search(query, limit=5), [])
                    report = memory.recall_report()
                    self.assertEqual(report["mode"], "identity-unbound")
                    self.assertTrue(report["abstained"])
            # The same store still answers a query whose identity it knows.
            results = memory.search("northalderwick project ratio", limit=5)
            self.assertEqual(
                [item["content"] for item in results],
                ["the northalderwick project ratio is one to four."],
            )
            self.assertEqual(memory.recall_report()["mode"], "narrowed")

    def test_hidden_project_rows_cannot_change_unscoped_narrowing(self) -> None:
        with Memory(Path(":memory:")) as memory:
            for index in range(7):
                _remember(memory, f"The project printer was serviced on day {index}.")
            _remember(memory, "The zephyr calibration constant is 88 microns.")
            with patch("jarvis.memory.MAX_MEMORY_SEARCH_CANDIDATES", 6):
                before = memory.search("zephyr project calibration", limit=3)
                before_report = memory.recall_report()
                conversation = memory.new_conversation(project_id=1)
                for index in range(7):
                    memory.remember_explicit_project_claim(
                        conversation,
                        1,
                        _project_fact_command(
                            f"zephyr calibration hidden {index}",
                            "project note",
                            f"value {index}",
                        ),
                    )
                after = memory.search("zephyr project calibration", limit=3)
                after_report = memory.recall_report()
        self.assertEqual(
            [item["content"] for item in before],
            ["The zephyr calibration constant is 88 microns."],
        )
        self.assertEqual(before_report["mode"], "narrowed")
        self.assertEqual(before_report["dropped_terms"], [])
        # Seven project-scoped rows that contain "zephyr" and "calibration"
        # are invisible to an unscoped query and must not change its
        # discriminating terms, its mode, or its answer.
        self.assertEqual(after, before)
        self.assertEqual(after_report, before_report)

    def test_hybrid_lexical_stage_uses_staged_discovery(self) -> None:
        with Memory(Path(":memory:")) as memory:
            for index in range(6):
                _remember(memory, f"The project printer was serviced on day {index}.")
            _remember(memory, "The zephyr calibration constant is 88 microns.")
            with patch("jarvis.memory.MAX_MEMORY_SEARCH_CANDIDATES", 4):
                results = memory.hybrid_memory_search(
                    "zephyr project calibration",
                    [1.0, 0.0],
                    "scale-test",
                    limit=3,
                )
                report = memory.recall_report()
        self.assertEqual(
            [item["content"] for item in results],
            ["The zephyr calibration constant is 88 microns."],
        )
        self.assertEqual(results[0]["retrieval_channel"], "lexical")
        self.assertEqual(report["mode"], "narrowed")

    def test_recall_report_describes_the_unnarrowed_fast_path(self) -> None:
        with Memory(Path(":memory:")) as memory:
            self.assertEqual(memory.recall_report()["mode"], "idle")
            _remember(memory, "The zephyr calibration constant is 88 microns.")
            results = memory.search("zephyr calibration")
            report = memory.recall_report()
        self.assertEqual(len(results), 1)
        self.assertEqual(
            report,
            {
                "channel": "lexical",
                "mode": "or",
                "candidate_limit": memory_module.MAX_MEMORY_SEARCH_CANDIDATES,
                "discovery_terms": 2,
                "dropped_terms": [],
                "unknown_terms": [],
                "candidates": 1,
                "abstained": False,
            },
        )

    def test_recall_report_is_reset_by_screened_and_empty_queries(self) -> None:
        with Memory(Path(":memory:")) as memory:
            _remember(memory, "The zephyr calibration constant is 88 microns.")
            self.assertEqual(len(memory.search("zephyr calibration")), 1)
            self.assertEqual(memory.recall_report()["mode"], "or")
            # A private identifier is screened before discovery runs.
            self.assertEqual(memory.search("zephyr for mira@example.com"), [])
            screened = memory.recall_report()
            self.assertEqual(screened["mode"], "screened")
            self.assertTrue(screened["abstained"])
            self.assertEqual(screened["candidates"], 0)
            self.assertEqual(memory.search("the of and"), [])
            self.assertEqual(memory.recall_report()["mode"], "empty")
            self.assertEqual(
                memory.hybrid_memory_search("the of and", [1.0, 0.0], "scale-test"),
                [],
            )
            self.assertEqual(memory.recall_report()["mode"], "empty")
            # The accessor returns a copy; mutating it cannot leak into the store.
            copy_of_report = memory.recall_report()
            copy_of_report["dropped_terms"].append("tampered")
            self.assertEqual(memory.recall_report()["dropped_terms"], [])

    def test_wildcard_queries_keep_the_literal_like_path(self) -> None:
        with Memory(Path(":memory:")) as memory:
            _remember(memory, "Rate limit is 50% of the burst budget.")
            results = memory.search("50% burst")
            report = memory.recall_report()
        self.assertEqual(len(results), 1)
        self.assertEqual(report["mode"], "like")
        self.assertFalse(report["abstained"])

    def test_realistic_store_resolves_identifier_behind_common_words(self) -> None:
        rng = random.Random(1)
        nouns = ["printer", "kitchen", "archive", "garden", "server", "schedule"]
        with Memory(Path(":memory:")) as memory:
            for index in range(240):
                noun = rng.choice(nouns)
                _remember(
                    memory,
                    f"The project {noun} was checked on day {index} "
                    f"with code {noun[:3]}{index:05d}.",
                )
            with patch("jarvis.memory.MAX_MEMORY_SEARCH_CANDIDATES", 100):
                results = memory.search("project code ser00042", limit=3)
                report = memory.recall_report()
        self.assertEqual(report["mode"], "narrowed")
        # Both everyday words appear in every record; only the identifier can
        # discriminate, and it is kept.
        self.assertEqual(report["dropped_terms"], [])
        self.assertEqual(len(results), 1)
        self.assertIn("ser00042", results[0]["content"])

    def test_identity_elsewhere_never_authorizes_a_generic_fact(self) -> None:
        with Memory(Path(":memory:")) as memory:
            _remember(
                memory,
                "SouthAlderwick office uses a cedar reception desk.",
            )
            _remember(memory, "the northalderwick project ratio is one to four.")
            _remember(memory, "The project ratio is one to four.")
            for method in ("search", "hybrid"):
                with self.subTest(size="below-cap", method=method):
                    result = (
                        memory.search("SouthAlderwick project ratio", limit=5)
                        if method == "search"
                        else memory.hybrid_memory_search(
                            "SouthAlderwick project ratio",
                            [1.0, 0.0],
                            "scale-test",
                            limit=5,
                        )
                    )
                    self.assertEqual(result, [])
                    self.assertEqual(memory.recall_report()["mode"], "identity-unbound")

            for index in range(memory_module.MAX_MEMORY_SEARCH_CANDIDATES + 1):
                _remember(memory, f"The project filler observation is {index}.")
            for method in ("search", "hybrid"):
                with self.subTest(size="above-cap", method=method):
                    result = (
                        memory.search("SouthAlderwick project ratio", limit=5)
                        if method == "search"
                        else memory.hybrid_memory_search(
                            "SouthAlderwick project ratio",
                            [1.0, 0.0],
                            "scale-test",
                            limit=5,
                        )
                    )
                    self.assertEqual(result, [])
                    self.assertEqual(memory.recall_report()["mode"], "identity-unbound")

            exact = "SouthAlderwick project ratio is three to seven."
            _remember(memory, exact)
            self.assertEqual(
                [item["content"] for item in memory.search(
                    "southalderwick project ratio", limit=5
                )],
                [exact],
            )

    def test_over_cap_ineligible_identity_rows_never_prove_association(self) -> None:
        with Memory(Path(":memory:")) as memory:
            for index in range(6):
                memory.remember(
                    f"SouthAlderwick project observation {index}", "fact"
                )
            _remember(memory, "The project ratio is one to four.")
            with patch("jarvis.memory.MAX_MEMORY_SEARCH_CANDIDATES", 4):
                self.assertEqual(
                    memory.search("SouthAlderwick project ratio", limit=3), []
                )
                self.assertEqual(
                    memory.recall_report()["mode"], "identity-unbound"
                )
                exact = "SouthAlderwick project ratio is one to nine."
                _remember(memory, exact)
                self.assertEqual(
                    [item["content"] for item in memory.search(
                        "SouthAlderwick project ratio", limit=3
                    )],
                    [exact],
                )

        with Memory(Path(":memory:")) as memory:
            for index in range(6):
                memory.remember(
                    f"SouthAlderwick project ratio observation {index}", "fact"
                )
            with patch("jarvis.memory.MAX_MEMORY_SEARCH_CANDIDATES", 4):
                self.assertEqual(
                    memory.search("SouthAlderwick project ratio", limit=3), []
                )
                self.assertEqual(
                    memory.recall_report()["mode"], "identity-overflow"
                )

    def test_natural_framing_words_are_not_identities(self) -> None:
        with Memory(Path(":memory:")) as memory:
            for index in range(6):
                _remember(memory, f"The project printer was serviced on day {index}.")
            exact = "The zephyr calibration constant is 88 microns."
            _remember(memory, exact)
            with patch("jarvis.memory.MAX_MEMORY_SEARCH_CANDIDATES", 4):
                for query in (
                    "Please retrieve zephyr project calibration recently",
                    "zephyr project calibration urgently",
                    "Retrieve Zephyr project calibration recently",
                ):
                    with self.subTest(query=query):
                        self.assertEqual(
                            [item["content"] for item in memory.search(query, limit=3)],
                            [exact],
                        )
                        report = memory.recall_report()
                        self.assertNotIn("retrieve", report["unknown_terms"])
                        self.assertNotIn("recently", report["unknown_terms"])
                        self.assertNotIn("urgently", report["unknown_terms"])

    def test_quality_quarantined_learning_rows_do_not_consume_candidate_cap(self) -> None:
        quality_tag = "jarvis-quality-contract:1"
        with Memory(Path(":memory:")) as memory:
            memory.remember_verified(
                "OFFICIAL_SCALE_SENTINEL Ollama fact",
                "learning",
                f"{quality_tag}\nhttps://docs.ollama.com/context-length",
                origin="verified_import",
            )
            for index in range(9):
                memory.remember_verified(
                    f"BLOG_SCALE_SENTINEL_{index} Ollama claim",
                    "learning",
                    f"{quality_tag}\nhttps://independent-notes.example/{index}/",
                    origin="verified_import",
                )
            with patch("jarvis.memory.MAX_MEMORY_SEARCH_CANDIDATES", 4):
                results = memory.search("Explain Ollama context", limit=3)
        self.assertEqual(len(results), 1)
        self.assertIn("OFFICIAL_SCALE_SENTINEL", results[0]["content"])

    def test_quality_quarantine_cannot_starve_embedding_batches(self) -> None:
        quality_tag = "jarvis-quality-contract:1"
        with Memory(Path(":memory:")) as memory:
            for index in range(130):
                memory.remember_verified(
                    f"BLOG_EMBED_SENTINEL_{index} Ollama claim",
                    "learning",
                    f"{quality_tag}\nhttps://independent-notes.example/{index}/",
                    origin="verified_import",
                )
            _remember(memory, "The zephyr embedding calibration is verified.")
            pending = memory.pending_memory_embeddings("scale-test", limit=1)
            leased = memory.claim_pending_memory_embeddings(
                "scale-test", "scale-worker", limit=1
            )
        self.assertEqual(len(pending), 1)
        self.assertIn("zephyr embedding calibration", pending[0]["content"])
        self.assertEqual(len(leased), 1)
        self.assertIn("zephyr embedding calibration", leased[0]["content"])

    def test_maximum_query_uses_bounded_frequency_statements(self) -> None:
        def token(index: int) -> str:
            letters = []
            value = index
            for _ in range(5):
                letters.append(chr(ord("a") + value % 26))
                value //= 26
            return "q" + "".join(reversed(letters))

        query = "project " + " ".join(token(index) for index in range(699))
        self.assertLessEqual(len(query), memory_module.MAX_SEARCH_QUERY_CHARS)
        with Memory(Path(":memory:")) as memory:
            for index in range(5):
                _remember(memory, f"The project overflow fixture is {index}.")
            statements: list[str] = []
            memory.db.set_trace_callback(statements.append)
            try:
                with patch("jarvis.memory.MAX_MEMORY_SEARCH_CANDIDATES", 4):
                    self.assertEqual(memory.search(query, limit=3), [])
                    self.assertEqual(
                        memory.recall_report()["discovery_terms"],
                        memory_retrieval._MAX_MEMORY_QUERY_TERM_CANDIDATES,
                    )
            finally:
                memory.db.set_trace_callback(None)
        fts_statements = [
            statement for statement in statements
            if "memory_fts" in statement.casefold()
            and not statement.lstrip().startswith("--")
        ]
        frequency_statements = [
            statement for statement in fts_statements
            if statement.lstrip().casefold().startswith("with q0 as")
        ]
        self.assertLessEqual(len(fts_statements), 4, fts_statements)
        self.assertEqual(len(frequency_statements), 1, frequency_statements)


class FtsQueryBuilderTests(unittest.TestCase):
    def test_require_all_intersects_every_term_with_its_spellings(self) -> None:
        self.assertEqual(
            memory_retrieval._memory_fts_query(
                "alpha beta", ["alpha", "beta"], require_all=True
            ),
            '("alpha" OR "alphas") AND ("beta" OR "betas")',
        )
        self.assertIsNone(
            memory_retrieval._memory_fts_query(
                "alpha% beta", ["alpha", "beta"], require_all=True
            )
        )

    def test_term_groups_flatten_to_the_or_query(self) -> None:
        cases = [
            ("Policies establishes endpoints", ["policy", "establish", "endpoint"]),
            ("alpha beta", ["alpha", "beta"]),
            ("CASE-123 status", memory_retrieval._memory_query_terms("CASE-123 status")),
            (
                "the suite executed real cases",
                memory_retrieval._memory_query_terms("the suite executed real cases"),
            ),
        ]
        for query, terms in cases:
            groups = memory_retrieval._memory_fts_term_groups(query, terms)
            flattened = [
                memory_retrieval._memory_fts_literal(term)
                for _canonical, spellings in groups
                for term in spellings
            ]
            self.assertEqual(
                " OR ".join(flattened),
                memory_retrieval._memory_fts_query(query, terms),
                query,
            )
            surfaces = memory_retrieval._memory_surface_terms(query, terms) or terms
            self.assertEqual(
                [canonical for canonical, _spellings in groups],
                list(dict.fromkeys(
                    memory_retrieval._normalize_memory_token(term)
                    for term in surfaces
                )),
                query,
            )

    def test_identity_capable_terms_require_identifier_structure(self) -> None:
        capable = memory_retrieval._memory_identity_capable_term
        self.assertFalse(capable("southalderwick"))
        self.assertTrue(capable("CASE-123"))
        self.assertTrue(capable("ser00042"))
        self.assertFalse(capable("zephyr"))
        self.assertFalse(capable("ratio"))
        self.assertFalse(capable("recently"))
        self.assertFalse(capable("retrieve"))
        self.assertFalse(capable("42"))


def _reference_claim_matched_query_terms(
    query_terms: set[str], record_terms: set[str]
) -> set[str]:
    """The pairwise implementation this module replaced, kept as an oracle."""
    matches: set[str] = set()
    rooted_record_terms = {
        term: _claim_term_root(term) for term in record_terms
    }
    for query_term in query_terms:
        query_root = _claim_term_root(query_term)
        for record_term, record_root in rooted_record_terms.items():
            if query_term == record_term:
                matches.add(query_term)
                break
            if query_root == record_root and (
                query_root != query_term or record_root != record_term
            ):
                matches.add(query_term)
                break
            shorter, longer = sorted((query_term, record_term), key=len)
            if len(shorter) >= 5 and any(
                longer == prefix + shorter for prefix in _CLAIM_COMPOUND_PREFIXES
            ):
                matches.add(query_term)
                break
    return matches


class ClaimTermMatchingTests(unittest.TestCase):
    def test_set_matching_equals_pairwise_reference_on_random_vocabularies(self) -> None:
        rng = random.Random(20260901)
        stems = [
            "inspect", "plan", "deploy", "rotate", "config", "cluster",
            "solvent", "ratio", "port", "mission", "pass", "vers", "install",
            "review", "budget", "token", "node", "one", "four", "case",
        ]
        suffixes = [
            "", "s", "ed", "ing", "ion", "ions", "ation", "ator", "ment",
            "er", "ers", "age", "e", "123",
        ]
        prefixes = ["", *sorted(_CLAIM_COMPOUND_PREFIXES)]

        def word() -> str:
            return rng.choice(prefixes) + rng.choice(stems) + rng.choice(suffixes)

        for use_cache in (False, True):
            cache = RecallCache() if use_cache else None
            for _ in range(600):
                query = {word() for _ in range(rng.randint(1, 6))}
                record = {word() for _ in range(rng.randint(0, 8))}
                expected = _reference_claim_matched_query_terms(query, record)
                if cache is None:
                    actual = _claim_matched_query_terms(query, record)
                else:
                    with cache.activate():
                        actual = _claim_matched_query_terms(query, record)
                self.assertEqual(actual, expected, (query, record, use_cache))

    def test_each_query_term_is_decided_independently(self) -> None:
        # ``current_claims`` derives its per-term anchor sets from one
        # whole-query match per claim; that is only valid if terms never
        # influence each other.
        rng = random.Random(7)
        vocabulary = [
            "inspect", "inspection", "reinspect", "plan", "planning", "port",
            "ports", "portion", "cluster", "clusters", "subcluster", "ratio",
            "solvent", "node", "nodes", "budget", "budgets", "overbudget",
        ]
        for _ in range(300):
            query = set(rng.sample(vocabulary, rng.randint(1, 5)))
            record = set(rng.sample(vocabulary, rng.randint(1, 6)))
            whole = _claim_matched_query_terms(query, record)
            per_term = {
                term for term in query
                if _claim_matched_query_terms({term}, record)
            }
            self.assertEqual(whole, per_term, (query, record))

    def test_documented_rules_hold(self) -> None:
        self.assertEqual(
            _claim_matched_query_terms({"inspection"}, {"inspect"}), {"inspection"}
        )
        self.assertEqual(
            _claim_matched_query_terms({"cluster"}, {"subcluster"}), {"cluster"}
        )
        self.assertEqual(
            _claim_matched_query_terms({"subcluster"}, {"cluster"}), {"subcluster"}
        )
        # Four-letter stems never bridge through a compound prefix.
        self.assertEqual(_claim_matched_query_terms({"port"}, {"report"}), set())
        # ``-ion`` stays conservative for short stems (mission is not miss).
        self.assertEqual(_claim_matched_query_terms({"mission"}, {"miss"}), set())
        self.assertEqual(_claim_matched_query_terms(set(), {"cluster"}), set())
        self.assertEqual(_claim_matched_query_terms({"cluster"}, set()), set())


class SemanticEligibilityTests(unittest.TestCase):
    def test_eligibility_is_checked_only_until_the_requested_count_is_filled(self) -> None:
        with Memory(Path(":memory:")) as memory:
            for index in range(12):
                _remember(
                    memory,
                    f"Semantic scale record {index} for eligibility batching.",
                )
            pending = memory.pending_memory_embeddings("scale-test", limit=20)
            self.assertEqual(len(pending), 12)
            memory.store_memory_embeddings(
                "scale-test", pending, [[1.0, 0.0] for _item in pending]
            )
            with patch.object(
                memory,
                "_ordinary_memory_recall_eligible",
                wraps=memory._ordinary_memory_recall_eligible,
            ) as eligibility:
                results = memory.semantic_memory_search(
                    [1.0, 0.0], "scale-test", limit=3
                )
        self.assertEqual(eligibility.call_count, 3)
        self.assertEqual(
            [item["content"] for item in results],
            [
                f"Semantic scale record {index} for eligibility batching."
                for index in (11, 10, 9)
            ],
        )

    def test_ineligible_rows_are_still_skipped_never_returned(self) -> None:
        with Memory(Path(":memory:")) as memory:
            _remember(memory, "Semantic eligible anchor record.")
            pending = memory.pending_memory_embeddings("scale-test", limit=20)
            memory.store_memory_embeddings(
                "scale-test", pending, [[1.0, 0.0] for _item in pending]
            )
            memory.db.execute(
                "UPDATE memories SET content=? WHERE content=?",
                ("Semantic tampered anchor record.", "Semantic eligible anchor record."),
            )
            self.assertEqual(
                memory.semantic_memory_search([1.0, 0.0], "scale-test", limit=3),
                [],
            )


class RecallCacheTests(unittest.TestCase):
    def test_cache_is_per_store_digest_keyed_and_cleared_on_close(self) -> None:
        private_text = "Vault note: contact ledger for northalderwick is private."
        memory = Memory(Path(":memory:"))
        try:
            _remember(memory, private_text)
            _remember(memory, "The zephyr calibration constant is 88 microns.")
            self.assertEqual(len(memory.search("northalderwick ledger")), 1)
            cache = memory._recall_cache
            self.assertGreater(len(cache), 0)
            for key in cache.keys():
                rendered = repr(key)
                self.assertNotIn("northalderwick", rendered)
                self.assertNotIn("Vault note", rendered)
            # Nothing is active outside a recall call.
            self.assertIsNone(memory_retrieval._ACTIVE_RECALL_CACHE.get())
        finally:
            memory.close()
        self.assertEqual(len(memory._recall_cache), 0)
        self.assertEqual(memory._recall_cache.size_bytes, 0)

    def test_two_stores_never_share_a_cache(self) -> None:
        with Memory(Path(":memory:")) as first, Memory(Path(":memory:")) as second:
            _remember(first, "The zephyr calibration constant is 88 microns.")
            first.search("zephyr calibration")
            self.assertGreater(len(first._recall_cache), 0)
            self.assertEqual(len(second._recall_cache), 0)
            self.assertIsNot(first._recall_cache, second._recall_cache)

    def test_cache_is_byte_bounded_with_oldest_first_eviction(self) -> None:
        cache = RecallCache(max_bytes=2_500, max_entries=100)
        for index in range(20):
            cache.put(("tokens", True, bytes([index])), ("t" * 40,), 40)
        self.assertLessEqual(cache.size_bytes, 2_500)
        self.assertLess(len(cache), 20)
        # The newest entries survive; the oldest were evicted first.
        self.assertIsNotNone(cache.get(("tokens", True, bytes([19]))))
        self.assertIsNone(cache.get(("tokens", True, bytes([0]))))
        # An entry larger than the whole budget is never stored.
        cache.put(("tokens", True, b"huge"), ("x" * 10_000,), 1)
        self.assertIsNone(cache.get(("tokens", True, b"huge")))
        cache.clear()
        self.assertEqual((len(cache), cache.size_bytes), (0, 0))

    def test_claim_cache_never_retains_ineligible_structured_fields(self) -> None:
        payload = "q" * 40
        credential = "sk-proj-" + payload
        with Memory(Path(":memory:")) as memory:
            claim_id = memory.remember_claim(
                "ReleaseService",
                "deployment status",
                "ready",
                source="cache privacy fixture",
                authority="verified",
            )
            memory.db.execute(
                "UPDATE memory_claims SET subject=? WHERE id=?",
                (credential, claim_id),
            )
            self.assertEqual(memory.current_claims("deployment status ready"), [])
            rendered = repr(memory._recall_cache._entries)
            self.assertNotIn(payload, rendered)
            self.assertNotIn(credential, rendered)

    def test_ordinary_hard_shadow_never_retains_sensitive_candidate_tokens(self) -> None:
        payload = "x" * 40
        credential = "ghp_" + payload
        with Memory(Path(":memory:")) as memory:
            _remember(memory, "Release sentinel anchor is verified.")
            self.assertEqual(len(memory.search("release sentinel anchor")), 1)
            memory.db.execute(
                "UPDATE memories SET content=? WHERE content=?",
                (
                    f"Release sentinel anchor contains {credential}.",
                    "Release sentinel anchor is verified.",
                ),
            )
            self.assertEqual(memory.search("release sentinel anchor"), [])
            self.assertGreater(len(memory._recall_cache), 0)
            rendered = repr(memory._recall_cache._entries)
            self.assertNotIn(payload, rendered)
            self.assertNotIn(credential, rendered)

    def test_private_ordinary_candidate_never_enters_persistent_cache(self) -> None:
        private = "rowan.unique" + "@" + "personal.invalid"
        with Memory(Path(":memory:")) as memory:
            memory.remember_verified(
                f"Atlas contact is {private}.",
                "fact",
                "private cache fixture",
                origin="verified_import",
            )
            self.assertEqual(memory.search("Atlas contact"), [])
            rendered = repr(memory._recall_cache._entries)
            for fragment in ("rowan", "unique", "personal", "invalid", private):
                self.assertNotIn(fragment, rendered)

    def test_structured_and_hybrid_hard_shadow_never_cache_secret(self) -> None:
        payload = "y" * 40
        credential = "ghp_" + payload
        with Memory(Path(":memory:")) as memory:
            _remember(memory, "CASE-9931 credential status is ready.")
            memory.db.execute(
                "UPDATE memories SET content=? WHERE content=?",
                (
                    f"CASE-9931 credential status is {credential}.",
                    "CASE-9931 credential status is ready.",
                ),
            )
            self.assertEqual(memory.search("CASE-9931 credential status"), [])
            self.assertEqual(
                memory.hybrid_memory_search(
                    "CASE-9931 credential status",
                    [1.0, 0.0],
                    "cache-privacy-test",
                ),
                [],
            )
            rendered = repr(memory._recall_cache._entries)
            self.assertNotIn(payload, rendered)
            self.assertNotIn(credential, rendered)

    def test_tampered_provenance_field_is_digest_only_in_cache_key(self) -> None:
        payload = "z" * 40
        credential = "sk-proj-" + payload
        with Memory(Path(":memory:")) as memory:
            _remember(memory, "Atlas provenance marker is verified.")
            memory_id = int(
                memory.db.execute(
                    "SELECT id FROM memories WHERE content=?",
                    ("Atlas provenance marker is verified.",),
                ).fetchone()[0]
            )
            memory.db.execute(
                "UPDATE ordinary_memory_provenance SET origin=? WHERE memory_id=?",
                (credential, memory_id),
            )
            self.assertEqual(memory.search("Atlas provenance marker"), [])
            rendered = repr(memory._recall_cache._entries)
            self.assertNotIn(payload, rendered)
            self.assertNotIn(credential, rendered)

    def test_cached_eligibility_distinguishes_null_from_empty_source(self) -> None:
        with Memory(Path(":memory:")) as memory:
            memory.remember_verified(
                "Atlas source marker is verified.",
                "fact",
                None,
                origin="verified_import",
            )
            self.assertEqual(len(memory.search("Atlas source marker")), 1)

            # NULL and the empty string hash to the same text bytes, but they
            # are distinct fields in the signed provenance receipt. An
            # out-of-band mutation therefore must not reuse the cached verdict.
            memory.db.execute(
                "UPDATE memories SET source='' WHERE content=?",
                ("Atlas source marker is verified.",),
            )
            self.assertEqual(memory.search("Atlas source marker"), [])

    def test_later_identity_proof_row_is_revalidated_before_text_caching(self) -> None:
        payload = "r" * 40
        credential = "ghp_" + payload
        original = "Atlas kiln coolant inspection is scheduled weekly."
        tampered = f"Atlas kiln coolant inspection contains {credential}."
        with Memory(Path(":memory:")) as memory:
            _remember(memory, original)
            query_count = 0
            query_rows = memory._generic_recall_query_rows

            def race_update(sql: str, parameters: object):
                nonlocal query_count
                query_count += 1
                if query_count == 2:
                    memory.db.execute(
                        "UPDATE memories SET content=? WHERE content=?",
                        (tampered, original),
                    )
                return query_rows(sql, parameters)

            with patch.object(
                memory,
                "_generic_recall_query_rows",
                side_effect=race_update,
            ):
                self.assertEqual(
                    memory.search("Atlas kiln coolant inspection"),
                    [],
                )

            rendered = repr(memory._recall_cache._entries)
            self.assertNotIn(payload, rendered)
            self.assertNotIn(credential, rendered)

    def test_cached_claim_eligibility_rechecks_out_of_band_tampering(self) -> None:
        payload = "z" * 40
        credential = "sk-proj-" + payload
        with Memory(Path(":memory:")) as memory:
            claim_id = memory.remember_claim(
                "ReleaseService",
                "deployment status",
                "ready",
                source="cache tamper fixture",
                authority="verified",
            )
            self.assertEqual(len(memory.current_claims("deployment status ready")), 1)
            memory.db.execute(
                "UPDATE memory_claims SET subject=? WHERE id=?",
                (credential, claim_id),
            )
            self.assertEqual(memory.current_claims("deployment status ready"), [])
            rendered = repr(memory._recall_cache._entries)
            self.assertNotIn(payload, rendered)
            self.assertNotIn(credential, rendered)

    def test_vault_replacement_and_deletion_clear_derived_cache_values(self) -> None:
        def note(body: str) -> VaultNote:
            return VaultNote(
                title="Retention",
                kind="research",
                created="2026-09-01T00:00:00+00:00",
                source=None,
                tags=(),
                body=body,
                path=Path("Retention.md"),
                relative_path="research/retention.md",
                modified_at=0.0,
            )

        old_marker = "oldnorthalderwick"
        with Memory(Path(":memory:")) as memory:
            memory.sync_vault_notes([note(f"{old_marker} retention marker")])
            self.assertEqual(len(memory.search(old_marker)), 1)
            self.assertGreater(len(memory._recall_cache), 0)
            changed = memory.sync_vault_notes([note("replacement retention marker")])
            self.assertEqual(changed["updated"], 1)
            self.assertEqual(len(memory._recall_cache), 0)
            self.assertNotIn(old_marker, repr(memory._recall_cache._entries))
            self.assertEqual(memory.search(old_marker), [])
            self.assertEqual(len(memory.search("replacement retention")), 1)
            self.assertGreater(len(memory._recall_cache), 0)
            removed = memory.sync_vault_notes([])
            self.assertEqual(removed["removed"], 1)
            self.assertEqual(len(memory._recall_cache), 0)

    def test_deletion_clears_the_store_cache(self) -> None:
        with Memory(Path(":memory:")) as memory:
            _remember(memory, "The zephyr calibration constant is 88 microns.")
            memory.search("zephyr calibration")
            self.assertGreater(len(memory._recall_cache), 0)
            conversation = memory.new_conversation()
            memory.delete_conversation(conversation)
            self.assertEqual(len(memory._recall_cache), 0)

    def test_helpers_are_pure_without_an_active_store(self) -> None:
        first = memory_retrieval._memory_tokens(
            "Policies establish endpoints", meaningful_only=True
        )
        first.append("mutated")
        second = memory_retrieval._memory_tokens(
            "Policies establish endpoints", meaningful_only=True
        )
        self.assertIsInstance(second, list)
        self.assertEqual(second, ["policy", "establish", "endpoint"])
        cache = RecallCache()
        with cache.activate():
            cached = memory_retrieval._memory_tokens(
                "Policies establish endpoints", meaningful_only=True
            )
            cached.append("mutated")
            again = memory_retrieval._memory_tokens(
                "Policies establish endpoints", meaningful_only=True
            )
        self.assertEqual(again, ["policy", "establish", "endpoint"])
        self.assertEqual(cache.hits, 1)

    def test_long_inputs_bypass_the_cache_with_identical_results(self) -> None:
        text = ("alpha beta CASE-123 " * 200).strip()
        self.assertGreater(
            len(text), memory_retrieval._MEMORY_TOKEN_CACHE_MAX_CHARS
        )
        cache = RecallCache()
        with cache.activate():
            self.assertEqual(
                memory_retrieval._memory_tokens(text, meaningful_only=True),
                list(memory_retrieval._memory_tokenize(text, meaningful_only=True)),
            )
            self.assertEqual(
                memory_retrieval._memory_tokens(text, meaningful_only=False),
                list(memory_retrieval._memory_tokenize(text, meaningful_only=False)),
            )
        self.assertEqual(len(cache), 0)


if __name__ == "__main__":
    unittest.main()
