"""Module tests for the temporal memory graph (VTMF M3, schema 48).

Like ``tests/test_memory_spine.py`` these run against a bare schema on a
caller-supplied connection: ``Memory`` owns ``user_version``, transactions and
the query screens, and the integration tests cover that seam.  What is proved
here is the graph's own contract — admission, projection, the bounded walk,
the identity floors, the screens, verify and rebuild — plus the four places
where this module deliberately keeps a copy of something another module owns.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import unittest
from pathlib import Path

from jarvis import (
    governed_memory, memory, memory_graph as graph, memory_spine as spine, redaction,
)
from jarvis.agent import Agent, _CONFIGURED_VALUE_WORDS, _named_fact_subjects

_CLAIMS_SQL = """CREATE TABLE memory_claims (
    id INTEGER PRIMARY KEY, memory_id INTEGER NOT NULL UNIQUE,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    claim_key TEXT NOT NULL, subject TEXT NOT NULL,
    predicate TEXT NOT NULL, value TEXT NOT NULL,
    value_sha256 TEXT NOT NULL, source TEXT NOT NULL,
    authority TEXT NOT NULL, confidence REAL NOT NULL,
    status TEXT NOT NULL, valid_from TEXT NOT NULL, valid_until TEXT,
    supersedes_id INTEGER, scope TEXT NOT NULL DEFAULT 'global',
    FOREIGN KEY(memory_id) REFERENCES memories(id),
    FOREIGN KEY(supersedes_id) REFERENCES memory_claims(id)
)"""
_CLAIM_EVENTS_SQL = """CREATE TABLE memory_claim_events (
    id INTEGER PRIMARY KEY, claim_id INTEGER NOT NULL, created_at TEXT NOT NULL,
    status TEXT NOT NULL, reason TEXT NOT NULL, related_claim_id INTEGER,
    FOREIGN KEY(claim_id) REFERENCES memory_claims(id)
)"""
_MEMORIES_SQL = """CREATE TABLE memories (
    id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, kind TEXT NOT NULL,
    content TEXT NOT NULL, source TEXT, UNIQUE(kind, content)
)"""
_STAMP = "2026-09-03T12:00:00+00:00"


def _stamp(offset_seconds: int = 0) -> str:
    return f"2026-09-03T12:{offset_seconds // 60:02d}:{offset_seconds % 60:02d}+00:00"


class GraphTestCase(unittest.TestCase):
    """A bare store with the spine present and the graph created."""

    def setUp(self) -> None:
        self.db = sqlite3.connect(":memory:", isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        for statement in (_MEMORIES_SQL, _CLAIMS_SQL, _CLAIM_EVENTS_SQL):
            self.db.execute(statement)
        self.db.execute(
            "CREATE INDEX idx_memory_claims_key ON memory_claims(claim_key, status, id)"
        )
        self.key = spine.load_spine_key(None)
        self.db.execute("BEGIN IMMEDIATE")
        spine.migrate_memory_spine_v46(self.db, self.key, now=_STAMP)
        self.db.execute("COMMIT")
        # Lineage is the spine's contract, not the graph's; these tests insert
        # claim rows directly so the graph can be exercised on its own.
        self.db.execute("DROP TRIGGER IF EXISTS memory_claims_require_spine_event")
        self.next_claim_id = 0

    def tearDown(self) -> None:
        self.db.close()

    # --- fixtures -----------------------------------------------------------

    def create_graph(self) -> None:
        self.db.execute("BEGIN IMMEDIATE")
        graph.create_graph_tables(self.db)
        self.db.execute(
            "INSERT INTO memory_graph_entity_sequence(id, next_id) VALUES (1, 1)"
        )
        self.db.execute("COMMIT")

    def insert_claim(
        self,
        subject: str,
        predicate: str,
        value: str,
        *,
        scope: str = "project:1",
        status: str = "active",
        authority: str = "operator",
        valid_until: str | None = None,
        claim_key: str | None = None,
        offset: int = 0,
    ) -> int:
        self.next_claim_id += 1
        claim_id = self.next_claim_id
        self.db.execute(
            "INSERT INTO memories(id, created_at, kind, content, source) "
            "VALUES (?, ?, 'claim', ?, 'fixture')",
            (claim_id, _stamp(offset), f"{subject} {predicate}: {value} #{claim_id}"),
        )
        self.db.execute(
            """INSERT INTO memory_claims(id, memory_id, created_at, updated_at,
               claim_key, subject, predicate, value, value_sha256, source,
               authority, confidence, status, valid_from, valid_until,
               supersedes_id, scope)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'fixture', ?, 1.0, ?, ?, ?, NULL, ?)""",
            (
                claim_id, claim_id, _stamp(offset), _stamp(offset),
                claim_key or f"{subject.casefold()}|{predicate}", subject, predicate,
                value, spine.sha256_hex(value), authority, status, _stamp(offset),
                valid_until, scope,
            ),
        )
        return claim_id

    def claim_row(self, claim_id: int) -> sqlite3.Row:
        row = self.db.execute(
            f"SELECT {', '.join(graph.CLAIM_ROW_COLUMNS)} FROM memory_claims WHERE id=?",
            (claim_id,),
        ).fetchone()
        assert row is not None
        return row

    def project(self, claim_id: int) -> str | None:
        self.db.execute("BEGIN IMMEDIATE")
        try:
            return graph.project_claim(self.db, self.claim_row(claim_id), now=_STAMP)
        finally:
            self.db.execute("COMMIT")

    def add(self, subject: str, predicate: str, value: str, **kwargs: object) -> int:
        claim_id = self.insert_claim(subject, predicate, value, **kwargs)
        self.project(claim_id)
        return claim_id

    def ask(
        self,
        question: str,
        subjects: list[str],
        *,
        scopes: tuple[str, ...] = ("global", "project:1"),
        project_scope: str | None = "project:1",
        temporal: bool = False,
        as_of: str | None = None,
        exact_only: bool = False,
        seed_claims: list[dict[str, object]] | None = None,
        deadline: float | None = None,
        limit: int = graph.CHAIN_ROW_CAP,
    ) -> dict[str, object]:
        started = time.monotonic()
        if deadline is None:
            deadline = started + graph.TIME_BUDGET_MS / 1000.0
        scope_sql, scope_params = graph.default_scope_filter(scopes, project_scope)
        walk = graph.graph_walk(
            self.db, query=question, visible_scopes=scopes, scope_sql=scope_sql,
            scope_params=scope_params, project_scope=project_scope, subjects=subjects,
            seed_claims=seed_claims or [], temporal=temporal, as_of=as_of,
            exact_only=exact_only, deadline=deadline,
        )
        rows: dict[int, dict[str, object]] = {}
        if walk["claim_ids"]:
            placeholders = ",".join("?" for _ in walk["claim_ids"])
            rows = {
                int(row["id"]): dict(row)
                for row in self.db.execute(
                    f"SELECT * FROM memory_claims WHERE id IN ({placeholders})",
                    walk["claim_ids"],
                ).fetchall()
            }
        result = graph.assemble_rows(
            walk, rows, limit=limit, deadline=deadline, started=started
        )
        result["walk"] = walk
        return result

    def seed_bridge_store(self) -> None:
        """The design's §1.1 store: relay -> box -> datacenter -> region."""
        self.create_graph()
        self.add("Kestrel relay", "deployed on host", "Harrier box")
        self.add("Harrier box", "datacenter", "Fenwick")
        self.add("Fenwick", "region", "Northgate")
        self.add("Kestrel relay", "listen port", "9090")

    def triples(self, result: dict[str, object]) -> list[tuple[str, str, str]]:
        return [
            (str(row["subject"]), str(row["predicate"]), str(row["value"]))
            for row in result["rows"]  # type: ignore[index]
        ]


# --- the copies this module keeps, and the boundaries they must not cross ----

class BoundaryCopyTests(unittest.TestCase):
    def test_excluded_predicate_namespace_is_the_governed_regex_verbatim(self) -> None:
        # A Codex boundary: copied, never loosened.
        self.assertEqual(
            graph.EXCLUDED_PREDICATE_NAMESPACE.pattern,
            governed_memory._RESERVED_PREDICATE_NAMESPACE.pattern,
        )
        self.assertEqual(
            graph.EXCLUDED_PREDICATE_NAMESPACE.flags,
            governed_memory._RESERVED_PREDICATE_NAMESPACE.flags,
        )

    def test_authority_weight_matches_the_claims_lane(self) -> None:
        self.assertEqual(graph.AUTHORITY_WEIGHT, memory._CLAIM_AUTHORITY_WEIGHT)

    def test_asked_value_words_are_the_agent_set(self) -> None:
        self.assertEqual(graph.ASKED_VALUE_WORDS, _CONFIGURED_VALUE_WORDS)

    def test_an_activity_verb_alone_leaves_an_open_question(self) -> None:
        # An activity verb the store has no predicate for must not narrow the
        # walk to nothing; one the store DOES have is the attribute the
        # operator asked for and must narrow (holdout iden-lookalike-07).
        for word in graph.ASKED_OPEN_WORDS:
            with self.subTest(word=word):
                asked, unmatched = graph.narrow_asked_words(
                    graph.asked_predicate_words(f"What {word} there?"),
                    known=frozenset({"datacenter"}),
                )
                self.assertEqual(asked, frozenset())
                self.assertFalse(unmatched)
                asked, unmatched = graph.narrow_asked_words(
                    graph.asked_predicate_words(f"What {word} there?"),
                    known=frozenset({word}),
                )
                self.assertEqual(asked, frozenset({word}))
                self.assertFalse(unmatched)

    def test_alias_subject_agrees_with_the_agent_copy(self) -> None:
        table = (
            ("relay", ["Kestrel relay", "Harrier box"]),
            ("relay", ["Kestrel relay", "Osprey relay"]),
            ("relay", ["Harrier box"]),
            ("Kestrel", ["Kestrel relay"]),
            ("box", ["Harrier box"]),
            ("Kestrel relay", ["Kestrel relay", "Osprey relay"]),
            ("BOX", ["harrier BOX"]),
            ("relay", []),
            ("", ["Kestrel relay"]),
            ("relay", ["Kestrel  relay", "kestrel relay"]),
        )
        for subject, known in table:
            with self.subTest(subject=subject, known=known):
                self.assertEqual(
                    graph.alias_subject(subject, known),
                    Agent._alias_subject(subject, list(known)),
                )

    def test_subject_identity_conflict_agrees_with_the_claims_lane(self) -> None:
        table = (
            ("kestrel relay", {"kestrel relay 2", "harrier box"}),
            ("kestrel relay", {"kestrel relay"}),
            ("kestrel relay", {"harrier box", "fenwick"}),
            ("box", {"harrier box"}),
            ("kestrelrelay", {"kestrel relay"}),
            ("northgate", {"northgate region"}),
            ("fenwick", set()),
        )
        for head, others in table:
            with self.subTest(head=head):
                self.assertEqual(
                    graph.subject_identity_conflict(head, others),
                    memory._claim_subject_identity_conflict(head, set(others)),
                )

    def test_named_fact_subjects_never_emits_a_lowercase_one_word_token(self) -> None:
        # Why the lower-case alias path is gone (design recommendation 3).
        questions = (
            "Where is the relay hosted?",
            "What runs on the box?",
            "Which datacenter hosts the Kestrel relay?",
            "Where is Node7 deployed?",
            "what is the listen port?",
        )
        for question in questions:
            for subject in _named_fact_subjects(question):
                with self.subTest(question=question, subject=subject):
                    if len(subject.split()) == 1:
                        self.assertNotEqual(subject, subject.casefold())


class RuntimePinTests(unittest.TestCase):
    def test_the_pin_covers_exactly_the_four_files(self) -> None:
        self.assertEqual(
            graph.MEMORY_GRAPH_RUNTIME_FILES,
            ("memory.py", "memory_graph.py", "memory_retrieval.py", "redaction.py"),
        )
        self.assertNotIn("agent.py", graph.MEMORY_GRAPH_RUNTIME_FILES)

    def test_the_digest_is_the_canonical_json_of_the_four_file_digests(self) -> None:
        package = Path(graph.__file__).resolve().parent
        material = {
            name: hashlib.sha256((package / name).read_bytes()).hexdigest()
            for name in graph.MEMORY_GRAPH_RUNTIME_FILES
        }
        expected = hashlib.sha256(
            json.dumps(
                material, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        ).hexdigest()
        self.assertEqual(graph.memory_graph_runtime_sha256(), expected)

    def test_the_digest_moves_when_the_file_set_moves(self) -> None:
        original = graph.MEMORY_GRAPH_RUNTIME_FILES
        before = graph.memory_graph_runtime_sha256()
        graph.MEMORY_GRAPH_RUNTIME_FILES = ("memory_graph.py", "redaction.py")
        try:
            self.assertNotEqual(graph.memory_graph_runtime_sha256(), before)
        finally:
            graph.MEMORY_GRAPH_RUNTIME_FILES = original
        self.assertEqual(graph.memory_graph_runtime_sha256(), before)


# --- admission --------------------------------------------------------------

class AdmissionTests(unittest.TestCase):
    def test_entity_key_folds_case_whitespace_and_nfkc(self) -> None:
        self.assertEqual(graph.entity_key("  Kestrel   Relay "), "kestrel relay")
        self.assertEqual(graph.entity_key("\uff2bestrel relay"), "kestrel relay")
        self.assertEqual(graph.entity_key("KESTREL\xa0RELAY"), "kestrel relay")

    def test_a_cyrillic_lookalike_is_a_different_entity(self) -> None:
        # NFKC merges compatibility spellings; confusables are not merged.
        self.assertNotEqual(
            graph.entity_key("Аtlas node"), graph.entity_key("Atlas node")
        )

    def test_entity_label_falls_back_to_the_key_when_too_long(self) -> None:
        label = graph.entity_label("A" * 200)
        self.assertLessEqual(len(label), graph.ENTITY_LABEL_MAX_CHARS)
        self.assertEqual(graph.entity_label("  Harrier   box "), "Harrier box")

    def test_claim_exclusion_names_the_three_categories(self) -> None:
        self.assertIsNone(graph.claim_exclusion("Harrier box", "datacenter"))
        self.assertEqual(
            graph.claim_exclusion("user", "preference:tone"), "excluded_predicate"
        )
        self.assertEqual(
            graph.claim_exclusion("user", "identity"), "excluded_predicate"
        )
        self.assertEqual(graph.claim_exclusion("H" * 90, "datacenter"), "subject_too_long")
        self.assertEqual(
            graph.claim_exclusion("admin@10.0.0.7", "datacenter"), "subject_private"
        )

    def test_the_cheapest_test_wins_so_an_overlong_subject_is_never_screened(self) -> None:
        subject = "alice@example.com " + "x" * 200
        self.assertEqual(graph.claim_exclusion(subject, "datacenter"), "subject_too_long")

    def test_value_admission_separates_entities_from_literals(self) -> None:
        self.assertEqual(graph.value_admission("Harrier box"), "entity")
        for literal in (
            "", "9090", "1.0", "H" * 90,
            "the relay was moved to the Fenwick datacenter last week by ops",
            "Done. Next step is the relay.", "10.0.0.7", "alice@example.com",
        ):
            with self.subTest(value=literal):
                self.assertEqual(graph.value_admission(literal), "literal")

    def test_a_redaction_placeholder_is_a_literal(self) -> None:
        # The write path already removed the secret, so the placeholder passes
        # every screen; it must still never be a node.
        for placeholder in ("[REDACTED]", "[EMAIL]", "[USER]", "[HOST]",
                            "[PRIVATE]", "[NOT RECORDED]"):
            with self.subTest(placeholder=placeholder):
                self.assertFalse(graph.screen_endpoint(placeholder)[0])
                self.assertEqual(graph.value_admission(placeholder), "literal")

    def test_an_ordinary_bracketed_name_is_still_an_entity(self) -> None:
        # Only a whole-value placeholder token; a real name that happens to
        # carry brackets is unaffected.
        self.assertEqual(graph.value_admission("[Redacted] box"), "entity")
        self.assertEqual(graph.value_admission("Rack [A]"), "entity")

    def test_asked_predicate_words(self) -> None:
        self.assertEqual(
            graph.asked_predicate_words("Which datacenter hosts the Kestrel relay?"),
            frozenset({"datacenter", "relay", "hosts"}),
        )
        self.assertEqual(
            graph.asked_predicate_words("What runs in Fenwick?"), frozenset({"runs"})
        )
        self.assertIn("region", graph.asked_predicate_words("Which region is it in?"))

    def test_narrow_asked_words_drops_the_subjects_own_words(self) -> None:
        # holdout iden-lookalike-01: "probe" is the subject, not an attribute.
        asked, unmatched = graph.narrow_asked_words(
            graph.asked_predicate_words("Where is the Alder probe hosted?"),
            subjects=["Alder probe"], known=frozenset({"hosted", "in"}),
        )
        self.assertEqual(asked, frozenset({"hosted"}))
        self.assertFalse(unmatched)

    def test_narrow_asked_words_drops_a_verb_only_when_something_else_asked(self) -> None:
        # design 7.4: the attribute is the datacenter, so "host" must not
        # narrow to "deployed on host".
        asked, _unmatched = graph.narrow_asked_words(
            graph.asked_predicate_words(
                "Which datacenter used to host the Kestrel relay?"
            ),
            subjects=["Kestrel relay"],
            known=frozenset({"datacenter", "deployed", "on", "host", "region"}),
        )
        self.assertEqual(asked, frozenset({"datacenter"}))

    def test_a_word_the_store_has_never_heard_of_is_unanswerable(self) -> None:
        # design 7.8, and holdout v2's "almanac": a real attribute the store
        # has no predicate for and no other trace of.
        asked, unmatched = graph.narrow_asked_words(
            graph.asked_predicate_words("Which region is the Kestrel relay in?"),
            subjects=["Kestrel relay"], known=frozenset({"datacenter"}),
            vocabulary=frozenset({"kestrel", "relay", "harrier", "box", "fenwick"}),
        )
        self.assertEqual(asked, frozenset({graph.UNMATCHED_PREDICATE}))
        self.assertTrue(unmatched)

    def test_a_word_the_store_knows_as_a_thing_never_narrows(self) -> None:
        # holdout v1 iden-twosubjects-01: "hall" is a value word, not an
        # attribute, so the join is an open question.
        asked, unmatched = graph.narrow_asked_words(
            graph.asked_predicate_words(
                "Is the Alder probe hosted in the same hall as the Cinder probe?"
            ),
            subjects=["Alder probe", "Cinder probe"],
            known=frozenset({"channel"}),
            vocabulary=frozenset({"kelpwood", "hall", "oxbow", "alder", "probe"}),
        )
        self.assertEqual(asked, frozenset())
        self.assertFalse(unmatched)

    def test_a_trailing_plural_folds(self) -> None:
        asked, unmatched = graph.narrow_asked_words(
            frozenset({"relays"}), known=frozenset({"relay"})
        )
        self.assertEqual(asked, frozenset({"relays"}))
        self.assertFalse(unmatched)
        asked, unmatched = graph.narrow_asked_words(
            frozenset({"relays"}), known=frozenset({"datacenter"}),
            vocabulary=frozenset({"relay"}),
        )
        self.assertEqual(asked, frozenset())
        self.assertFalse(unmatched)

    def test_the_vocabulary_is_not_read_when_a_predicate_matches(self) -> None:
        # The expensive half of the query must stay off the common path.
        def explode() -> frozenset[str]:
            raise AssertionError("vocabulary read on the narrowing path")

        asked, unmatched = graph.narrow_asked_words(
            frozenset({"datacenter"}), known=frozenset({"datacenter"}),
            vocabulary=explode,
        )
        self.assertEqual(asked, frozenset({"datacenter"}))
        self.assertFalse(unmatched)


# --- projection, hooks and migration -----------------------------------------

class ProjectionTests(GraphTestCase):
    def test_project_claim_creates_one_edge_and_two_entities(self) -> None:
        self.create_graph()
        claim_id = self.add("Kestrel relay", "deployed on host", "Harrier box")
        edge = self.db.execute(
            "SELECT * FROM memory_graph_edges WHERE claim_id=?", (claim_id,)
        ).fetchone()
        self.assertEqual(edge["value_kind"], "entity")
        self.assertEqual(edge["predicate_key"], "deployed on host")
        self.assertEqual(edge["scope"], "project:1")
        keys = {
            str(row[0]) for row in self.db.execute(
                "SELECT entity_key FROM memory_graph_entities"
            )
        }
        self.assertEqual(keys, {"kestrel relay", "harrier box"})

    def test_a_literal_value_is_a_terminal_hop_with_no_node(self) -> None:
        self.create_graph()
        claim_id = self.add("Kestrel relay", "listen port", "9090")
        edge = self.db.execute(
            "SELECT * FROM memory_graph_edges WHERE claim_id=?", (claim_id,)
        ).fetchone()
        self.assertEqual(edge["value_kind"], "literal")
        self.assertIsNone(edge["dst_entity_id"])

    def test_excluded_claims_have_no_edge(self) -> None:
        self.create_graph()
        for subject, predicate, value, category in (
            ("user", "preference:tone", "brief", "excluded_predicate"),
            ("H" * 90, "datacenter", "Fenwick", "subject_too_long"),
            ("admin@10.0.0.7", "datacenter", "Fenwick", "subject_private"),
        ):
            with self.subTest(category=category):
                claim_id = self.insert_claim(subject, predicate, value)
                self.assertEqual(self.project(claim_id), category)
                self.assertIsNone(self.db.execute(
                    "SELECT 1 FROM memory_graph_edges WHERE claim_id=?", (claim_id,)
                ).fetchone())

    def test_update_edge_tracks_status_and_is_a_no_op_without_an_edge(self) -> None:
        self.create_graph()
        claim_id = self.add("Harrier box", "datacenter", "Fenwick")
        excluded = self.insert_claim("user", "preference:tone", "brief")
        self.project(excluded)
        self.db.execute("BEGIN IMMEDIATE")
        self.assertTrue(graph.update_edge(
            self.db, claim_id, status="superseded", valid_until=_stamp(60)
        ))
        self.assertFalse(graph.update_edge(self.db, excluded, status="superseded"))
        self.assertTrue(graph.update_edge(self.db, claim_id, confidence=0.5))
        self.db.execute("COMMIT")
        edge = self.db.execute(
            "SELECT * FROM memory_graph_edges WHERE claim_id=?", (claim_id,)
        ).fetchone()
        self.assertEqual(edge["status"], "superseded")
        self.assertEqual(edge["valid_until"], _stamp(60))
        self.assertAlmostEqual(float(edge["confidence"]), 0.5)

    def test_valid_until_is_ignored_without_a_status_change(self) -> None:
        self.create_graph()
        claim_id = self.add("Harrier box", "datacenter", "Fenwick")
        self.db.execute("BEGIN IMMEDIATE")
        graph.update_edge(self.db, claim_id, confidence=0.9, valid_until=_stamp(60))
        self.db.execute("COMMIT")
        self.assertIsNone(self.db.execute(
            "SELECT valid_until FROM memory_graph_edges WHERE claim_id=?", (claim_id,)
        ).fetchone()[0])

    def test_delete_edges_sweeps_only_orphaned_entities(self) -> None:
        self.seed_bridge_store()
        region_claim = 3
        self.db.execute("BEGIN IMMEDIATE")
        removed = graph.delete_edges(self.db, [region_claim])
        self.db.execute("COMMIT")
        surviving = {
            str(row[0]) for row in self.db.execute(
                "SELECT entity_key FROM memory_graph_entities"
            )
        }
        self.assertNotIn("northgate", surviving)     # only that edge referenced it
        self.assertIn("fenwick", surviving)          # still the box's datacenter
        self.assertTrue(removed)

    def test_an_entity_id_is_never_reused(self) -> None:
        self.seed_bridge_store()
        northgate = int(self.db.execute(
            "SELECT id FROM memory_graph_entities WHERE entity_key='northgate'"
        ).fetchone()[0])
        self.db.execute("BEGIN IMMEDIATE")
        graph.delete_edges(self.db, [3])
        self.db.execute("COMMIT")
        self.add("Fenwick", "region", "Northgate")
        reborn = int(self.db.execute(
            "SELECT id FROM memory_graph_entities WHERE entity_key='northgate'"
        ).fetchone()[0])
        self.assertGreater(reborn, northgate)


class MigrationTests(GraphTestCase):
    def _seed_mixed_store(self) -> None:
        self.insert_claim("Kestrel relay", "deployed on host", "Harrier box")
        self.insert_claim("Harrier box", "datacenter", "Fenwick")
        self.insert_claim("user", "preference:tone", "brief", scope="global")
        self.insert_claim("H" * 90, "datacenter", "Fenwick")
        self.insert_claim("admin@10.0.0.7", "datacenter", "Fenwick")

    def test_migration_projects_every_admitted_claim_and_records_the_categories(self) -> None:
        self._seed_mixed_store()
        self.db.execute("BEGIN IMMEDIATE")
        report = graph.migrate_memory_graph_v48(self.db, self.key, now=_stamp(30))
        self.db.execute("COMMIT")
        self.assertEqual(report["edges"], 2)
        self.assertEqual(report["excluded"], {
            "excluded_predicate": 1, "subject_private": 1, "subject_too_long": 1,
        })
        self.assertTrue(graph.graph_ready(self.db))
        payload = json.loads(self.db.execute(
            "SELECT payload_json FROM memory_spine_events WHERE id=?",
            (report["event_id"],),
        ).fetchone()[0])
        self.assertEqual(payload["projection"], "graph")
        self.assertEqual(payload["rows_before"], 0)
        self.assertEqual(payload["rows_after"], 2)
        self.assertEqual(payload["excluded"], report["excluded"])
        self.assertEqual(payload["entities"], report["entities"])

    def test_migration_is_idempotent_and_rebuilds_from_scratch(self) -> None:
        self._seed_mixed_store()
        self.db.execute("BEGIN IMMEDIATE")
        first = graph.migrate_memory_graph_v48(self.db, self.key, now=_stamp(30))
        self.db.execute("COMMIT")
        self.db.execute("BEGIN IMMEDIATE")
        second = graph.migrate_memory_graph_v48(self.db, self.key, now=_stamp(40))
        self.db.execute("COMMIT")
        self.assertEqual(first["edges"], second["edges"])
        self.assertEqual(first["excluded"], second["excluded"])
        self.assertTrue(graph.verify_graph(self.db)["ok"])

    def test_migration_writes_nothing_to_the_claim_rows(self) -> None:
        self._seed_mixed_store()
        before = [tuple(row) for row in self.db.execute(
            "SELECT * FROM memory_claims ORDER BY id"
        )]
        self.db.execute("BEGIN IMMEDIATE")
        graph.migrate_memory_graph_v48(self.db, self.key, now=_stamp(30))
        self.db.execute("COMMIT")
        after = [tuple(row) for row in self.db.execute(
            "SELECT * FROM memory_claims ORDER BY id"
        )]
        self.assertEqual(before, after)

    def test_drop_graph_tables_leaves_a_store_with_no_graph(self) -> None:
        self.create_graph()
        self.db.execute("BEGIN IMMEDIATE")
        graph.drop_graph_tables(self.db)
        self.db.execute("COMMIT")
        self.assertFalse(graph.graph_ready(self.db))
        self.assertFalse(graph.verify_graph(self.db)["ready"])


# --- traversal ---------------------------------------------------------------

class TraversalTests(GraphTestCase):
    def test_forward_two_hop(self) -> None:
        self.seed_bridge_store()
        result = self.ask(
            "Which datacenter hosts the Kestrel relay?", ["Kestrel relay"]
        )
        self.assertEqual(result["report"]["mode"], "complete")
        self.assertIn(("Harrier box", "datacenter", "Fenwick"), self.triples(result))
        answer = next(
            row for row in result["rows"] if row["value"] == "Fenwick"
        )
        self.assertEqual(answer["hop"], 2)
        self.assertEqual(answer["bridge_from"], "Kestrel relay / deployed on host")
        # Since 10.3 item 3 the walk may read on past the hop that answered;
        # what matters is that the answer is there, at its own hop.
        self.assertEqual(result["rows"][0]["hop"], 1)

    def test_the_reversed_triple_reads_the_relay_off_its_host(self) -> None:
        self.seed_bridge_store()
        result = self.ask("What runs on the Harrier box?", ["Harrier box"])
        self.assertIn(
            ("Kestrel relay", "deployed on host", "Harrier box"), self.triples(result)
        )

    def test_three_hops_reach_the_region(self) -> None:
        self.seed_bridge_store()
        result = self.ask("Which region is the Kestrel relay in?", ["Kestrel relay"])
        self.assertEqual(self.triples(result), [
            ("Kestrel relay", "deployed on host", "Harrier box"),
            ("Harrier box", "datacenter", "Fenwick"),
            ("Fenwick", "region", "Northgate"),
        ])
        self.assertEqual([row["hop"] for row in result["rows"]], [1, 2, 3])

    def test_a_fourth_hop_is_never_walked(self) -> None:
        self.seed_bridge_store()
        self.add("Northgate", "continent", "Meridia")
        result = self.ask("Which continent is the Kestrel relay in?", ["Kestrel relay"])
        self.assertNotIn(
            ("Northgate", "continent", "Meridia"), self.triples(result)
        )

    def test_an_unreachable_asked_predicate_is_not_answered_by_a_neighbour(self) -> None:
        self.create_graph()
        self.add("Kestrel relay", "deployed on host", "Harrier box")
        self.add("Harrier box", "datacenter", "Fenwick")
        result = self.ask("Which region is the Kestrel relay in?", ["Kestrel relay"])
        self.assertEqual(result["rows"], [])
        self.assertEqual(result["report"]["mode"], "no-answer")

    def test_an_unknown_name_is_no_start_not_no_answer(self) -> None:
        self.seed_bridge_store()
        result = self.ask("Where is the Vault box?", ["Vault box"])
        self.assertEqual(result["report"]["mode"], "no-start")

    def test_a_cycle_terminates(self) -> None:
        self.create_graph()
        self.add("Alpha node", "peers with", "Beta node")
        self.add("Beta node", "peers with", "Alpha node")
        self.add("Alpha node", "same as", "Alpha node")
        result = self.ask("What is Alpha node connected to?", ["Alpha node"])
        self.assertLessEqual(result["report"]["edges"], graph.EDGE_BUDGET)
        self.assertIn(result["report"]["mode"], graph.GRAPH_MODES)


class ScopeTests(GraphTestCase):
    def test_a_current_project_row_shadows_the_global_row_of_the_same_key(self) -> None:
        self.create_graph()
        self.add("Harrier box", "datacenter", "Old Fenwick", scope="global")
        result = self.ask("Which datacenter is the Harrier box in?", ["Harrier box"])
        self.assertIn(("Harrier box", "datacenter", "Old Fenwick"), self.triples(result))
        self.add("Harrier box", "datacenter", "Fenwick", scope="project:1")
        result = self.ask("Which datacenter is the Harrier box in?", ["Harrier box"])
        values = {value for _s, _p, value in self.triples(result)}
        self.assertIn("Fenwick", values)
        self.assertNotIn("Old Fenwick", values)

    def test_another_project_is_never_reachable(self) -> None:
        self.create_graph()
        self.add("Harrier box", "datacenter", "Fenwick", scope="project:2")
        result = self.ask("Which datacenter is the Harrier box in?", ["Harrier box"])
        self.assertEqual(result["rows"], [])

    def test_a_superseded_project_row_does_not_shadow_the_global_row(self) -> None:
        self.create_graph()
        self.add("Harrier box", "datacenter", "Old Fenwick", scope="global")
        claim_id = self.add(
            "Harrier box", "datacenter", "Fenwick", scope="project:1",
            status="superseded", valid_until=_stamp(60),
        )
        self.assertEqual(
            self.db.execute(
                "SELECT status FROM memory_claims WHERE id=?", (claim_id,)
            ).fetchone()[0],
            "superseded",
        )
        result = self.ask("Which datacenter is the Harrier box in?", ["Harrier box"])
        self.assertIn(("Harrier box", "datacenter", "Old Fenwick"), self.triples(result))


class TemporalTests(GraphTestCase):
    def _seed_moved_relay(self) -> None:
        self.create_graph()
        self.add(
            "Kestrel relay", "deployed on host", "Harrier box",
            status="superseded", valid_until=_stamp(60),
        )
        self.add("Harrier box", "datacenter", "Fenwick")
        self.add("Kestrel relay", "deployed on host", "Talon box", offset=120)
        self.add("Talon box", "datacenter", "Moss Hollow", offset=120)

    def test_now_mode_never_returns_a_superseded_edge(self) -> None:
        self._seed_moved_relay()
        result = self.ask(
            "Which datacenter hosts the Kestrel relay?", ["Kestrel relay"]
        )
        self.assertNotIn(("Harrier box", "datacenter", "Fenwick"), self.triples(result))
        self.assertIn(("Talon box", "datacenter", "Moss Hollow"), self.triples(result))

    def test_temporal_mode_chains_through_the_superseded_edge(self) -> None:
        self._seed_moved_relay()
        result = self.ask(
            "Which datacenter used to host the Kestrel relay?", ["Kestrel relay"],
            temporal=True,
        )
        self.assertIn(("Harrier box", "datacenter", "Fenwick"), self.triples(result))
        superseded = [row for row in result["rows"] if row["status"] == "superseded"]
        self.assertTrue(superseded)
        self.assertTrue(all("superseded_at" in row for row in superseded))

    def test_as_of_excludes_a_legacy_row_superseded_in_place(self) -> None:
        # A row superseded before schema 46 has a NULL valid_until; without the
        # third conjunct it would answer every dated question (review R9).
        self.create_graph()
        self.add(
            "Kestrel relay", "deployed on host", "Harrier box",
            status="superseded", valid_until=None,
        )
        result = self.ask(
            "Where was the Kestrel relay deployed?", ["Kestrel relay"],
            as_of=_stamp(30),
        )
        self.assertEqual(result["rows"], [])
        self.assertEqual(result["report"]["mode"], "no-answer")

    def test_as_of_distinguishes_no_answer_from_no_start(self) -> None:
        self.create_graph()
        self.add(
            "Kestrel relay", "deployed on host", "Harrier box",
            status="superseded", valid_until=None,
        )
        known = self.ask(
            "Where was the Kestrel relay?", ["Kestrel relay"], as_of=_stamp(30)
        )
        unknown = self.ask("Where was the Vault box?", ["Vault box"], as_of=_stamp(30))
        self.assertEqual(known["report"]["mode"], "no-answer")
        self.assertEqual(unknown["report"]["mode"], "no-start")


class OverflowTests(GraphTestCase):
    def test_an_inner_hub_overflows_whole_and_is_never_partially_expanded(self) -> None:
        self.create_graph()
        for index in range(40):
            self.add(f"Box{index}", "region", "Northgate")
        result = self.ask("Which boxes are in the Northgate region?", ["Northgate"])
        self.assertEqual(result["rows"], [])
        self.assertEqual(result["report"]["mode"], "overflow")
        self.assertEqual(len(result["overflow"]), 1)
        entry = result["overflow"][0]
        self.assertEqual(entry["status"], "overflow")
        self.assertEqual(entry["hop"], 1)
        self.assertIn("Ask about one by name", entry["note"])

    def test_a_hub_whose_edges_all_miss_the_asked_predicate_is_not_skipped(self) -> None:
        # The narrowed re-query matches nothing, so NONE of the hub's edges was
        # read.  Invariant 3 outranks "narrow and proceed": the node overflows,
        # and the chain that walked past it says it is incomplete.
        self.create_graph()
        self.add("Kestrel relay", "deployed on host", "Harrier box")
        for index in range(40):
            self.add(f"Cable{index}", "attached to", "Harrier box")
        self.add("Harrier box", "datacenter", "Fenwick")
        result = self.ask(
            "Which datacenter hosts the Kestrel relay?", ["Kestrel relay"]
        )
        self.assertIn(("Harrier box", "datacenter", "Fenwick"), self.triples(result))
        self.assertTrue(all(row.get("incomplete") for row in result["rows"]))
        self.assertGreaterEqual(result["report"]["overflow"], 1)
        entry = result["overflow"][0]
        self.assertEqual(entry["hop"], 2)
        # The cap that was actually exceeded is the unnarrowed one, not the
        # filtered cap the empty re-query ran under.
        self.assertIn(f"More than {graph.FANOUT_CAP} stored facts", entry["note"])
        self.assertIn("Ask about one by name", entry["note"])
        self.assertEqual(
            result["walk"]["overflow"][0]["cap"], graph.FANOUT_CAP
        )

    def test_a_hub_inside_the_filtered_cap_still_answers(self) -> None:
        # Seventeen in-edges: over the inner cap of 16, inside the filtered cap
        # of 32, and every one of them carries the asked predicate.
        self.create_graph()
        for index in range(17):
            self.add(f"Box{index}", "datacenter", "Fenwick")
        answered = self.ask("Which boxes are in the Fenwick datacenter?", ["Fenwick"])
        self.assertEqual(answered["report"]["mode"], "complete")
        self.assertEqual(answered["walk"]["overflow"], [])   # the hub was read
        self.assertTrue(answered["rows"])
        # Seventeen answers, eight rows: that is the block budget, and the
        # count note says so rather than the list being silently short.
        self.assertEqual(len(answered["rows"]), graph.CHAIN_ROW_CAP)
        note = answered["overflow"][0]
        self.assertIn("17 stored facts answer this", note["note"])
        # The note names the hub the answers share, not one of the answers.
        self.assertEqual(note["subject"], "Fenwick")

    def test_the_same_hub_overflows_for_an_open_question(self) -> None:
        self.create_graph()
        for index in range(17):
            self.add(f"Box{index}", "datacenter", "Fenwick")
        result = self.ask("", ["Fenwick"])
        self.assertEqual(result["report"]["mode"], "overflow")
        self.assertEqual(result["rows"], [])
        self.assertEqual(result["overflow"][0]["hop"], 1)

    def test_a_terminal_hub_of_forty_answers_with_the_strongest_eight(self) -> None:
        self.create_graph()
        self.add("Kestrel relay", "deployed on host", "Harrier box")
        self.add("Harrier box", "datacenter", "Fenwick")
        for index in range(40):
            self.add(f"Rack{index}", "datacenter", "Fenwick")
        result = self.ask(
            "Which racks are in the Fenwick datacenter?", ["Kestrel relay"]
        )
        self.assertEqual(result["report"]["mode"], "complete")
        self.assertLessEqual(len(result["rows"]), graph.CHAIN_ROW_CAP)
        terminals = [row for row in result["rows"] if row["hop"] == 3]
        self.assertTrue(terminals)
        # The cap read all forty; the block shows the strongest few and says so.
        self.assertTrue(all(row.get("incomplete") for row in terminals))
        notes = [entry["note"] for entry in result["overflow"]]
        self.assertTrue(any("stored facts answer this" in note for note in notes))
        self.assertTrue(any("40 stored facts" in note for note in notes))

    def test_a_hub_reached_at_hop_two_marks_the_chain_through_it_incomplete(self) -> None:
        # An open question (no asked predicate to narrow by), so the hub is
        # read whole or not at all.
        self.create_graph()
        self.add("Kestrel relay", "deployed on host", "Harrier box")
        for index in range(40):
            self.add(f"Service{index}", "deployed on host", "Harrier box")
        self.add("Harrier box", "datacenter", "Fenwick")
        result = self.ask("", ["Kestrel relay"])
        self.assertTrue(result["rows"])
        self.assertTrue(all(row.get("incomplete") for row in result["rows"]))
        self.assertGreaterEqual(result["report"]["overflow"], 1)
        hops = {entry["hop"] for entry in result["overflow"]}
        self.assertIn(2, hops)

    def test_at_most_two_overflow_notes_are_named_and_the_rest_are_counted(self) -> None:
        self.create_graph()
        for hub in ("Alpha hub", "Beta hub", "Gamma hub", "Delta hub"):
            for index in range(40):
                self.add(f"{hub[:3]}Box{index}", "attached to", hub)
        result = self.ask(
            "", ["Alpha hub", "Beta hub", "Gamma hub", "Delta hub"]
        )
        self.assertLessEqual(len(result["overflow"]), graph.OVERFLOW_NOTE_CAP)
        self.assertGreaterEqual(result["report"]["overflow"], 4)


class RedTeamRegressionTests(GraphTestCase):
    def test_the_chain_cap_says_how_many_chains_it_dropped(self) -> None:
        self.create_graph()
        for index in range(6):
            self.add(f"Hub{index}", "datacenter", f"Site{index}")
            self.add("Kestrel relay", f"link {index}", f"Hub{index}")
        result = self.ask("", ["Kestrel relay"])
        self.assertGreater(result["report"]["truncated_chains"], 0)
        notes = [entry["note"] for entry in result["overflow"]]
        self.assertTrue(any("found and not shown" in note for note in notes))

    def test_two_named_starts_do_not_merge_into_one_chain(self) -> None:
        # The correctness review: hop-1 edges from different starts share the
        # empty path prefix, so they were grouped as siblings — eight rows
        # about one subject, a note counting both, the other subject absent.
        self.create_graph()
        for index in range(9):
            self.add("Alpha probe", f"link {index}", f"AlphaSite{index}")
            self.add("Beta probe", f"link {index}", f"BetaSite{index}")
        result = self.ask("", ["Alpha probe", "Beta probe"])
        subjects = {subject for subject, _p, _v in self.triples(result)}
        self.assertEqual(subjects, {"Alpha probe", "Beta probe"})
        self.assertEqual(len({row["chain"] for row in result["rows"]}), 2)

    def test_the_design_store_gives_two_starts_two_chain_numbers(self) -> None:
        self.seed_bridge_store()
        result = self.ask("What runs on the Harrier box?", ["Harrier box"])
        hop_one = [row for row in result["rows"] if row["hop"] == 1]
        self.assertEqual(len(hop_one), 2)
        self.assertEqual(len({row["chain"] for row in hop_one}), 2)

    def test_an_open_question_into_a_terminal_hub_says_what_it_left_out(self) -> None:
        self.create_graph()
        self.add("Node7", "deployed on host", "Harrier box")
        self.add("Harrier box", "datacenter", "Fenwick")
        for index in range(40):
            self.add(f"Rack{index}", "datacenter", "Fenwick")
        result = self.ask("", ["Node7"])
        self.assertTrue(result["rows"])
        notes = [entry["note"] for entry in result["overflow"]]
        self.assertTrue(
            any("stored facts answer this" in note
                or "found and not shown" in note for note in notes),
            result["overflow"],
        )

    # --- exit test 7.3 row 2, the M3 motivating case ------------------------

    _SEED_FORWARD = {
        "id": 2, "subject": "Harrier box", "predicate": "datacenter",
        "value": "Fenwick",
    }

    def _reverse_two_hop(self, seeds: list[dict[str, object]]) -> dict[str, object]:
        return self.ask(
            "What runs on the Harrier box?", ["Harrier box"], seed_claims=seeds
        )

    def test_the_reverse_hop_answers_with_and_without_seed_claims(self) -> None:
        # The seed's own forward chain used to outrank the one-hop reverse
        # chain that answers, so passing the lane's rows changed the answer.
        self.seed_bridge_store()
        for label, seeds in (
            ("no seeds", []),
            ("with the forward seed", [self._SEED_FORWARD]),
        ):
            with self.subTest(seeds=label):
                result = self._reverse_two_hop(seeds)
                self.assertEqual(result["report"]["mode"], "complete")
                self.assertIn(
                    ("Kestrel relay", "deployed on host", "Harrier box"),
                    self.triples(result),
                )
                self.assertEqual(result["report"]["chains"], 2)

    def test_the_seed_changes_no_answer_of_the_named_start(self) -> None:
        # The invariant the exit test protects: what the named subject answers
        # is the same, in the same order and with the same chain and hop
        # numbers, whether or not the lane hands its rows over.  Since 10.3
        # item 3 the seeded run can differ in one way that is not a
        # displacement: a seed value becomes a start of its own, so a fact
        # that was a continuation of the named walk is a hop-1 fact about the
        # seed instead, and the walk cap may spend its second slot elsewhere.
        self.seed_bridge_store()
        without = self._reverse_two_hop([])
        with_seed = self._reverse_two_hop([self._SEED_FORWARD])
        named = [
            ("Harrier box", "datacenter", "Fenwick"),
            ("Kestrel relay", "deployed on host", "Harrier box"),
        ]
        for result in (without, with_seed):
            triples = self.triples(result)
            for expected in named:
                self.assertIn(expected, triples)
            self.assertEqual(triples[:1], named[:1])
        numbering = [
            (row["chain"], row["hop"]) for row in with_seed["rows"]
            if (row["subject"], row["predicate"], row["value"]) in named
        ]
        self.assertEqual(
            numbering,
            [
                (row["chain"], row["hop"]) for row in without["rows"]
                if (row["subject"], row["predicate"], row["value"]) in named
            ],
        )

    def test_a_seed_chain_never_takes_a_named_starts_slot(self) -> None:
        self.seed_bridge_store()
        result = self._reverse_two_hop([self._SEED_FORWARD])
        # Every emitted chain starts at the subject the operator named.
        self.assertNotIn(
            ("Fenwick", "region", "Northgate"), self.triples(result)
        )

    def test_an_open_question_keeps_one_chain_in_each_direction(self) -> None:
        self.seed_bridge_store()
        result = self._reverse_two_hop([self._SEED_FORWARD])
        subjects = {subject for subject, _p, _v in self.triples(result)}
        self.assertEqual(subjects, {"Harrier box", "Kestrel relay"})

    def test_nothing_is_reported_truncated_below_the_chain_cap(self) -> None:
        self.create_graph()
        self.add("Harrier box", "datacenter", "Fenwick")
        result = self._reverse_two_hop([self._SEED_FORWARD])
        self.assertLess(result["report"]["chains"], graph.CHAIN_CAP)
        self.assertEqual(result["report"]["truncated_chains"], 0)
        self.assertEqual(result["overflow"], [])

    def test_a_shared_hop_is_marked_when_a_later_chain_is_incomplete(self) -> None:
        # The tail-shrink can drop every row but the shared one, so the marker
        # has to be on it too.
        self.create_graph()
        self.add("Kestrel relay", "deployed on host", "Harrier box")
        self.add("Harrier box", "datacenter", "Fenwick")
        for index in range(40):
            self.add(f"Service{index}", "deployed on host", "Harrier box")
        result = self.ask("", ["Kestrel relay"])
        self.assertTrue(result["rows"])
        self.assertTrue(all(row.get("incomplete") for row in result["rows"]))

    def test_one_walk_is_one_chain_number(self) -> None:
        self.seed_bridge_store()
        result = self.ask("Which region is the Kestrel relay in?", ["Kestrel relay"])
        self.assertEqual({row["chain"] for row in result["rows"]}, {1})
        self.assertEqual([row["hop"] for row in result["rows"]], [1, 2, 3])

    def test_a_question_word_that_names_no_predicate_never_narrows(self) -> None:
        # "relays" is a subject-type word, not a predicate; it must not drop
        # the chain that answers through "region".
        self.seed_bridge_store()
        result = self.ask("Which relays are in the Northgate region?", ["Northgate"])
        self.assertEqual(result["report"]["mode"], "complete")
        self.assertNotIn("relays", result["walk"]["asked"])
        self.assertIn("region", result["walk"]["asked"])

    def test_asking_for_a_predicate_the_store_has_none_of_still_answers_nothing(self) -> None:
        # The other half of the same rule: emptying the asked set is not the
        # same as never having asked (§7.8).
        self.create_graph()
        self.add("Kestrel relay", "deployed on host", "Harrier box")
        self.add("Harrier box", "datacenter", "Fenwick")
        result = self.ask("Which region is the Kestrel relay in?", ["Kestrel relay"])
        self.assertEqual(result["rows"], [])
        self.assertEqual(result["report"]["mode"], "no-answer")
        self.assertTrue(result["walk"]["asked_unmatched"])


class ChainCapNoteTests(GraphTestCase):
    """The chain-cap note names what was cut (live battery, 2026-09-04).

    The old text read "N more stored chains answer this; ask about one by
    name" and a model rendered it as "at least 2 more stored facts about
    what is on the Harrier box did not fit in the context window": chains
    became facts, the count drifted from 1 to 2, and "one by name" named
    nothing the operator could ask about.  A cut chain is a continuation, so
    it never answers the question -- it continues from somewhere, and the
    note has to say where.
    """

    def _battery_store(self) -> None:
        self.create_graph()
        self.add("Kestrel relay", "deployed on host", "Harrier box")
        self.add("Harrier box", "datacenter", "Fenwick")
        self.add("Fenwick", "region", "Northgate")
        self.add("Kestrel relay", "listens on port", "9090")

    def test_the_note_names_the_node_the_cut_chain_continues_from(self) -> None:
        self._battery_store()
        result = self.ask("What runs on the Harrier box?", ["Harrier box"])
        self.assertEqual(result["report"]["truncated_chains"], 1)
        notes = [entry for entry in result["overflow"]
                 if "found and not shown" in entry["note"]]
        self.assertEqual(len(notes), 1)
        note = notes[0]
        self.assertEqual(note["subject"], "Kestrel relay")
        self.assertEqual(
            note["note"],
            "1 more chain found and not shown; the first continues from "
            "Kestrel relay. Ask about Kestrel relay by name.",
        )

    def test_the_note_never_says_a_continuation_answers_the_question(self) -> None:
        self._battery_store()
        result = self.ask("What runs on the Harrier box?", ["Harrier box"])
        for entry in result["overflow"]:
            if "found and not shown" in entry["note"]:
                self.assertNotIn("answer this", entry["note"])

    def test_the_note_stays_inside_the_length_bound(self) -> None:
        for count in (1, 2, 40):
            for label in ("Kestrel relay", "K" * graph.ENTITY_LABEL_MAX_CHARS):
                with self.subTest(count=count, label=len(label)):
                    note = graph._chain_truncation_note(count, label)
                    self.assertLess(len(note), 130)

    def test_the_count_is_singular_or_plural_correctly(self) -> None:
        self.assertEqual(
            graph._chain_truncation_note(1, "Kestrel relay"),
            "1 more chain found and not shown; the first continues from "
            "Kestrel relay. Ask about Kestrel relay by name.",
        )
        self.assertEqual(
            graph._chain_truncation_note(2, "Kestrel relay"),
            "2 more chains found and not shown; the first continues from "
            "Kestrel relay. Ask about Kestrel relay by name.",
        )

    def test_a_note_that_cannot_name_the_node_is_not_emitted(self) -> None:
        # An unnameable note is worse than none: the count still stands in
        # the report, and a label that fails the screen is dropped like any
        # other note subject.
        self._battery_store()
        self.db.execute(
            "UPDATE memory_graph_entities SET label='ops@example.com' "
            "WHERE entity_key='kestrel relay'"
        )
        result = self.ask("What runs on the Harrier box?", ["Harrier box"])
        self.assertEqual(result["report"]["truncated_chains"], 1)
        for entry in result["overflow"]:
            self.assertNotIn("found and not shown", entry["note"])
        self.assertNotIn("ops@example.com", str(result))

    def test_the_hub_sibling_count_note_is_unchanged(self) -> None:
        # The other note keeps its wording: it really does count facts that
        # answer the question, and "one by name" is actionable there because
        # the rows beside it carry the names.
        self.assertEqual(
            graph._sibling_note(40, 8),
            "40 stored facts answer this; the 8 strongest are shown. "
            "Ask about one by name for the rest.",
        )
        self.create_graph()
        for index in range(17):
            self.add(f"Box{index}", "datacenter", "Fenwick")
        result = self.ask("Which boxes are in the Fenwick datacenter?", ["Fenwick"])
        notes = [entry["note"] for entry in result["overflow"]]
        self.assertTrue(any("17 stored facts answer this" in note for note in notes))


class OverflowProbeTests(GraphTestCase):
    """The unordered probe must decide overflow exactly as the fetch did.

    Asking whether a fan-out exceeds the cap no longer sorts the fan-out, so
    the decision has to be proved identical at the boundary rather than
    assumed: a hub of three thousand edges was costing 3.8 ms of temp b-tree
    to choose seventeen rows it then discarded.
    """

    def _hub(self, size: int) -> None:
        self.create_graph()
        for index in range(size):
            self.add(f"Box{index}", "attached to", "Alpha hub")

    def test_the_probe_matches_the_fetch_at_the_cap_boundary(self) -> None:
        for size in (graph.FANOUT_CAP - 1, graph.FANOUT_CAP,
                     graph.FANOUT_CAP + 1, graph.FANOUT_CAP + 9):
            with self.subTest(size=size):
                self.setUp()
                self._hub(size)
                scope_sql, scope_params = graph.default_scope_filter(
                    ("global", "project:1"), "project:1"
                )
                rows = self.db.execute(
                    "SELECT id FROM memory_graph_entities WHERE entity_key='alpha hub'"
                ).fetchall()
                ids = [int(row[0]) for row in rows]
                probe = graph._expand_overflows(
                    self.db, cap=graph.FANOUT_CAP, ids=ids, direction="in",
                    scope_sql=scope_sql, scope_params=scope_params, mode="now",
                    as_of=None,
                )
                fetched = graph._expand(
                    self.db, ids=ids, direction="in", limit=graph.FANOUT_CAP + 1,
                    scope_sql=scope_sql, scope_params=scope_params, mode="now",
                    as_of=None,
                )
                self.assertEqual(probe, len(fetched) > graph.FANOUT_CAP)

    def test_a_hub_still_overflows_and_a_small_fan_out_still_answers(self) -> None:
        self._hub(graph.FANOUT_CAP + 5)
        overflowed = self.ask("", ["Alpha hub"])
        self.assertEqual(overflowed["report"]["mode"], "overflow")
        self.setUp()
        self._hub(3)
        answered = self.ask("", ["Alpha hub"])
        self.assertEqual(answered["report"]["mode"], "complete")
        self.assertEqual(len(answered["rows"]), 3)


class RankingTests(GraphTestCase):
    def test_a_chain_is_as_strong_as_its_weakest_hop(self) -> None:
        self.create_graph()
        self.add("Kestrel relay", "deployed on host", "Harrier box", authority="learned")
        self.add("Harrier box", "datacenter", "Fenwick", authority="operator")
        result = self.ask(
            "Which datacenter hosts the Kestrel relay?", ["Kestrel relay"]
        )
        weakest = [row for row in result["rows"] if row.get("weakest")]
        self.assertEqual(len(weakest), 1)
        self.assertEqual(weakest[0]["authority"], "learned")
        self.assertEqual(result["rows"][-1]["chain_authority"], "learned")

    def test_an_all_operator_chain_carries_no_chain_authority(self) -> None:
        self.seed_bridge_store()
        result = self.ask(
            "Which datacenter hosts the Kestrel relay?", ["Kestrel relay"]
        )
        self.assertTrue(all("chain_authority" not in row for row in result["rows"]))

    def test_a_shorter_chain_outranks_a_longer_one(self) -> None:
        self.seed_bridge_store()
        result = self.ask("What runs on the Harrier box?", ["Harrier box"])
        self.assertEqual(result["rows"][0]["hop"], 1)

    def _walk_and_rows(
        self, question: str, subjects: list[str]
    ) -> tuple[dict[str, object], dict[int, dict[str, object]]]:
        scope_sql, scope_params = graph.default_scope_filter(
            ("global", "project:1"), "project:1"
        )
        walk = graph.graph_walk(
            self.db, query=question, visible_scopes=("global", "project:1"),
            scope_sql=scope_sql, scope_params=scope_params, subjects=subjects,
        )
        rows = {
            int(row["id"]): dict(row) for row in self.db.execute(
                "SELECT * FROM memory_claims WHERE id IN "
                f"({','.join('?' for _ in walk['claim_ids'])})", walk["claim_ids"]
            )
        } if walk["claim_ids"] else {}
        return walk, rows

    def test_match_subject_never_tags_an_answering_chain(self) -> None:
        # The lead tells the model a match: subject entry is NOT the asked
        # fact; putting it on a row that does answer contradicts the block.
        self.seed_bridge_store()
        for question in (
            "What runs on the Harrier box?",              # open question
            "Which datacenter is the Harrier box in?",    # answered question
        ):
            with self.subTest(question=question):
                walk, rows = self._walk_and_rows(question, ["Harrier box"])
                result = graph.assemble_rows(
                    walk, rows, started=time.monotonic(),
                    match_subject_keys=["harrier box"],
                )
                self.assertTrue(result["rows"])
                self.assertTrue(all("match" not in row for row in result["rows"]))

    def test_no_emitted_row_or_note_carries_a_label(self) -> None:
        # label is display-only: nothing model-facing may read it (§7.18).
        self.seed_bridge_store()
        walk, rows = self._walk_and_rows(
            "Which region is the Kestrel relay in?", ["Kestrel relay"]
        )
        result = graph.assemble_rows(walk, rows, started=time.monotonic())
        for row in [*result["rows"], *result["overflow"]]:
            self.assertNotIn("label", row)
        self.assertNotIn("label", result["report"])


class ScreenTests(GraphTestCase):
    def test_a_private_value_is_a_literal_and_its_row_never_reaches_the_block(self) -> None:
        self.create_graph()
        self.add("Vault box", "management address", "10.0.0.7")
        edge = self.db.execute("SELECT * FROM memory_graph_edges").fetchone()
        self.assertEqual(edge["value_kind"], "literal")
        result = self.ask(
            "What is the management address of the Vault box?", ["Vault box"]
        )
        self.assertEqual(result["rows"], [])
        self.assertEqual(result["report"]["mode"], "screened-rows")
        self.assertEqual(result["report"]["excluded_by_screen"], 1)

    def test_two_redacted_values_share_no_node_and_cannot_reach_each_other(self) -> None:
        # remember_claim rewrites a secret-shaped value to "[REDACTED]", so two
        # different credentials arrive as one string.  As a node it would join
        # the facts about them.
        self.create_graph()
        self.add("Alpha probe", "deploy key", "[REDACTED]")
        self.add("Beta probe", "deploy key", "[REDACTED]")
        self.assertEqual(
            {str(row[0]) for row in self.db.execute(
                "SELECT value_kind FROM memory_graph_edges"
            )},
            {"literal"},
        )
        self.assertEqual(
            {str(row[0]) for row in self.db.execute(
                "SELECT entity_key FROM memory_graph_entities"
            )},
            {"alpha probe", "beta probe"},
        )
        result = self.ask(
            "What is the deploy key of the Alpha probe?", ["Alpha probe"]
        )
        # A placeholder is never a node (the two probes share nothing) and,
        # since holdout v2, never a row either: it answers nothing.
        self.assertEqual(result["rows"], [])
        self.assertNotIn("Beta probe", str(result["rows"]))

    def test_the_backfill_honours_the_placeholder_rule(self) -> None:
        self.insert_claim("Alpha probe", "deploy key", "[REDACTED]")
        self.insert_claim("Beta probe", "deploy key", "[REDACTED]")
        self.db.execute("BEGIN IMMEDIATE")
        report = graph.migrate_memory_graph_v48(self.db, self.key, now=_stamp(30))
        self.db.execute("COMMIT")
        self.assertEqual(report["edges"], 2)
        self.assertEqual(report["entities"], 2)
        self.assertTrue(graph.verify_graph(self.db)["ok"])

    def test_a_redaction_placeholder_row_is_never_emitted(self) -> None:
        # holdout v2 screen-secret-open: the write path rewrote two
        # credential values to "[REDACTED]" and both were emitted as the
        # chain's answer.  A placeholder passes every screen precisely
        # because the secret is already gone, and it answers nothing.
        self.create_graph()
        self.add("Tarn bay", "district", "Sedgely ward")
        self.add("Tarn bay", "curfew", "[REDACTED]")
        self.add("Tarn bay", "almanac", "[REDACTED]")
        result = self.ask("What is located at the Tarn bay?", ["Tarn bay"])
        self.assertNotIn("[REDACTED]", str(result))
        self.assertEqual(self.triples(result), [
            ("Tarn bay", "district", "Sedgely ward")
        ])
        self.assertEqual(result["report"]["excluded_by_screen"], 2)

    def test_every_placeholder_spelling_is_dropped(self) -> None:
        for placeholder in ("[REDACTED]", "[EMAIL]", "[USER]", "[HOST]"):
            with self.subTest(placeholder=placeholder):
                self.assertTrue(graph.is_redaction_placeholder(placeholder))
                self.assertTrue(graph.is_redaction_placeholder(f"  {placeholder} "))
        for real in ("Sedgely ward", "[Redacted] ward", "Rack [A]"):
            with self.subTest(real=real):
                self.assertFalse(graph.is_redaction_placeholder(real))

    def test_a_placeholder_does_not_break_the_chain_around_it(self) -> None:
        # It is dropped like a screened row, so the hop after it goes too;
        # a sibling that answers is unaffected.
        self.create_graph()
        self.add("Tarn bay", "curfew", "[REDACTED]")
        self.add("Tarn bay", "district", "Sedgely ward")
        self.add("Sedgely ward", "charter", "Halloway lodge")
        result = self.ask("", ["Tarn bay"])
        self.assertNotIn("[REDACTED]", str(result))
        self.assertIn(("Tarn bay", "district", "Sedgely ward"), self.triples(result))

    def test_a_secret_value_never_becomes_a_node(self) -> None:
        self.create_graph()
        self.add("Vault box", "deploy key", "sk-" + "a" * 32)
        self.assertEqual(
            self.db.execute(
                "SELECT value_kind FROM memory_graph_edges"
            ).fetchone()[0],
            "literal",
        )
        result = self.ask("What is the deploy key of the Vault box?", ["Vault box"])
        self.assertEqual(result["rows"], [])

    def test_a_private_subject_is_excluded_and_unreachable(self) -> None:
        self.create_graph()
        self.add("Harrier box", "datacenter", "Fenwick")
        claim_id = self.insert_claim("admin@10.0.0.7", "datacenter", "Fenwick")
        self.assertEqual(self.project(claim_id), "subject_private")
        result = self.ask("What is in the Fenwick datacenter?", ["Fenwick"])
        subjects = {subject for subject, _p, _v in self.triples(result)}
        self.assertNotIn("admin@10.0.0.7", subjects)

    def test_a_row_screened_at_read_time_breaks_its_chain(self) -> None:
        # A subject projected before the screen widened: the per-row screen is
        # the last gate before the model.
        self.seed_bridge_store()
        self.db.execute(
            "UPDATE memory_claims SET subject='ops@10.0.0.9' WHERE id=2"
        )
        result = self.ask("Which region is the Kestrel relay in?", ["Kestrel relay"])
        subjects = {subject for subject, _p, _v in self.triples(result)}
        self.assertNotIn("ops@10.0.0.9", subjects)
        self.assertNotIn("Northgate", {value for _s, _p, value in self.triples(result)})


class IdentityFloorTests(GraphTestCase):
    def _seed_lookalikes(self) -> None:
        self.create_graph()
        self.add("Kestrel relay", "datacenter", "Fenwick")
        self.add("Kestrel relay 2", "datacenter", "Moss Hollow")
        self.add("Kestrelrelay", "datacenter", "Talon Fields")

    def test_an_exact_key_answers_from_that_key_alone(self) -> None:
        self._seed_lookalikes()
        result = self.ask(
            "Which datacenter hosts the Kestrel relay?", ["Kestrel relay"]
        )
        self.assertEqual(self.triples(result), [
            ("Kestrel relay", "datacenter", "Fenwick")
        ])

    def test_a_misspelled_subject_abstains_with_identity_conflict(self) -> None:
        self._seed_lookalikes()
        result = self.ask("Where is the Kestrel rely hosted?", ["Kestrel rely"])
        self.assertEqual(result["rows"], [])
        self.assertEqual(result["report"]["mode"], "identity-conflict")

    def test_two_exactly_resolved_subjects_both_start(self) -> None:
        self.create_graph()
        self.add("Kestrel relay", "datacenter", "Fenwick")
        self.add("Harrier box", "datacenter", "Fenwick")
        result = self.ask(
            "Are the Kestrel relay and the Harrier box in the same datacenter?",
            ["Kestrel relay", "Harrier box"],
        )
        subjects = {subject for subject, _p, _v in self.triples(result)}
        self.assertEqual(subjects, {"Kestrel relay", "Harrier box"})

    def test_exact_only_disables_the_alias_rule(self) -> None:
        self.create_graph()
        self.add("Kestrel relay", "datacenter", "Fenwick")
        loose = self.ask("Where is the relay?", ["relay"])
        strict = self.ask("Where is the relay?", ["relay"], exact_only=True)
        self.assertEqual(loose["report"]["starts"], 1)
        self.assertEqual(strict["report"]["starts"], 0)
        self.assertEqual(strict["report"]["mode"], "no-start")

    def _seed_store(self) -> list[dict[str, object]]:
        """Surface's repro: two relays, so a seed value collides with an
        unrelated stored name under the look-alike floor."""
        self.create_graph()
        self.add("Kestrel relay", "deployed on host", "Harrier box")
        self.add("Harrier box", "datacenter", "Fenwick")
        self.add("Talon relay", "deployed on host", "Talon box")
        self.add("Talon box", "datacenter", "Moss Hollow")
        return [{
            "id": 1, "subject": "Kestrel relay",
            "predicate": "deployed on host", "value": "Harrier box",
        }]

    def test_a_seed_endpoint_resolves_by_exact_key_and_never_abstains(self) -> None:
        seeds = self._seed_store()
        # "harrier box" does conflict with the stored "talon box"; a seed
        # endpoint must never be taken down that path.
        self.assertTrue(
            graph.subject_identity_conflict("harrier box", {"talon box"})
        )
        result = self.ask(
            "Which datacenter hosts the Kestrel relay?", ["Kestrel relay"],
            seed_claims=seeds,
        )
        self.assertEqual(result["report"]["mode"], "complete")
        self.assertIn(("Harrier box", "datacenter", "Fenwick"), self.triples(result))
        self.assertGreaterEqual(result["report"]["starts"], 2)

    def test_a_seed_lookalike_of_an_exactly_spelled_subject_is_dropped(self) -> None:
        # The lane discovers rows by substring, so a question naming "Kestrel
        # relay" hands back rows about "Kestrel relay 2".  Each is an exact key
        # of its own; the exactly spelled name must still answer alone.
        self._seed_lookalikes()
        seeds = [
            {"id": 1, "subject": "Kestrel relay", "value": "Fenwick"},
            {"id": 2, "subject": "Kestrel relay 2", "value": "Moss Hollow"},
            {"id": 3, "subject": "Kestrelrelay", "value": "Talon Fields"},
        ]
        result = self.ask(
            "Which datacenter hosts the Kestrel relay?", ["Kestrel relay"],
            seed_claims=seeds,
        )
        self.assertEqual(result["report"]["mode"], "complete")
        self.assertEqual(self.triples(result), [
            ("Kestrel relay", "datacenter", "Fenwick")
        ])
        for forbidden in ("Kestrel relay 2", "Kestrelrelay", "Moss Hollow",
                          "Talon Fields"):
            self.assertNotIn(forbidden, str(result["rows"]))

    def test_an_unrelated_seed_endpoint_is_still_kept(self) -> None:
        # The look-alike drop is narrow: it compares only against the names the
        # operator spelled exactly, never against the whole store.
        seeds = self._seed_store()
        result = self.ask(
            "Which datacenter hosts the Kestrel relay?", ["Kestrel relay"],
            seed_claims=seeds,
        )
        self.assertEqual(result["report"]["mode"], "complete")
        self.assertGreaterEqual(result["report"]["starts"], 2)
        self.assertIn(("Harrier box", "datacenter", "Fenwick"), self.triples(result))

    def test_a_seed_endpoint_with_no_stored_entity_contributes_no_start(self) -> None:
        self._seed_store()
        result = self.ask(
            "Which datacenter is it in?", [],
            seed_claims=[{"id": 9, "subject": "Ghost relay", "value": "Ghost box"}],
        )
        self.assertEqual(result["report"]["mode"], "no-start")

    def test_a_typed_name_resolves_only_as_a_word_prefix(self) -> None:
        # The red team of 2026-09-03: under the first-word rule these all
        # resolved to "Kestrel relay" and the store answered with another
        # subject's value and no cue.
        self.create_graph()
        self.add("Kestrel relay", "datacenter", "Fenwick")
        for name in ("Kestrel payroll ledger", "Kestrel gateway",
                     "Kestrel database", "Kestrel node cluster", "Zephyr gadget"):
            with self.subTest(name=name):
                result = self.ask("Which datacenter hosts it?", [name])
                self.assertEqual(result["rows"], [])
                self.assertEqual(result["report"]["mode"], "no-start")

    def test_an_overlong_name_is_not_answered_from_its_shorter_neighbour(self) -> None:
        self.create_graph()
        neighbour = "N" * 80
        self.add(neighbour, "datacenter", "Fenwick")
        result = self.ask("Which datacenter hosts it?", ["N" * 81])
        self.assertEqual(result["rows"], [])
        self.assertIn(result["report"]["mode"], {"no-start", "identity-conflict"})

    def test_a_one_word_prefix_resolves_when_it_is_the_only_candidate(self) -> None:
        self.create_graph()
        self.add("Kestrel relay", "datacenter", "Fenwick")
        result = self.ask("Which datacenter hosts Kestrel?", ["Kestrel"])
        self.assertEqual(self.triples(result), [
            ("Kestrel relay", "datacenter", "Fenwick")
        ])

    def test_a_misspelling_abstains_but_an_unknown_name_does_not(self) -> None:
        # recommendation 10 keeps the two modes apart so an operator can tell
        # "I cannot tell which one you mean" from "I have never heard of that".
        self.create_graph()
        self.add("Kestrel relay", "datacenter", "Fenwick")
        self.add("Harrier box", "datacenter", "Fenwick")
        typo = self.ask("Where is the Kestrel rely?", ["Kestrel rely"])
        self.assertEqual(typo["report"]["mode"], "identity-conflict")
        for unknown in ("Merlin relay", "Zephyr gadget", "Osprey box"):
            with self.subTest(unknown=unknown):
                result = self.ask("Where is it?", [unknown])
                self.assertEqual(result["report"]["mode"], "no-start")

    def test_near_miss_is_one_edit_in_one_word(self) -> None:
        keys = {"kestrel relay", "harrier box"}
        for typed in ("kestrel rely", "kestrel relayy", "harrier bo", "kestrel relax"):
            with self.subTest(typed=typed):
                self.assertTrue(graph.near_miss_subject(typed, keys))
        for typed in ("merlin relay", "kestrel gateway", "kestrel payroll ledger",
                      "zephyr gadget", "kestrel relay"):
            with self.subTest(typed=typed):
                self.assertFalse(graph.near_miss_subject(typed, keys))

    def test_resolution_honours_the_whole_call_deadline(self) -> None:
        self.create_graph()
        self.add("Kestrel relay", "datacenter", "Fenwick")
        starts, mode, _unresolved = graph.resolve_starts(
            self.db, subjects=["Kestrel relay"], visible_scopes=("project:1",),
            deadline=time.monotonic() - 1.0,
        )
        self.assertEqual(starts, [])
        self.assertEqual(mode, "budget-exceeded")
        result = self.ask(
            "Which datacenter hosts the Kestrel relay?", ["Kestrel relay"],
            deadline=time.monotonic() - 1.0,
        )
        self.assertEqual(result["report"]["mode"], "budget-exceeded")
        self.assertEqual(result["report"]["budget"], "time")

    def test_a_named_subject_that_only_resolves_non_exactly_abstains_the_call(self) -> None:
        # Even beside a subject that resolved exactly: the operator named
        # something the store cannot identify, and a confident half-answer to
        # it is worse than silence (§1.4 two_subjects, §7.15).
        self._seed_lookalikes()
        self.add("Harrier box", "datacenter", "Fenwick")
        result = self.ask(
            "Which datacenter hosts the Harrier box and the Kestrel rely?",
            ["Harrier box", "Kestrel rely"],
        )
        self.assertEqual(result["rows"], [])
        self.assertEqual(result["report"]["mode"], "identity-conflict")

    def test_a_seed_endpoint_never_abstains_a_named_subject(self) -> None:
        # The other half of the same rule: only a name the operator typed can
        # abstain the call, never a row the lane happened to hand over.
        seeds = self._seed_store()
        result = self.ask(
            "Which datacenter hosts the Kestrel relay?", ["Kestrel relay"],
            seed_claims=[*seeds, {"id": 3, "subject": "Talon relay", "value": "Talon box"}],
        )
        self.assertEqual(result["report"]["mode"], "complete")

    def test_a_chain_started_from_a_seed_value_opens_with_that_seed_claim(self) -> None:
        seeds = self._seed_store()
        result = self.ask("Which datacenter is it in?", [], seed_claims=seeds)
        self.assertEqual(result["report"]["mode"], "complete")
        self.assertEqual(self.triples(result), [
            ("Kestrel relay", "deployed on host", "Harrier box"),
            ("Harrier box", "datacenter", "Fenwick"),
        ])
        first, second = result["rows"]
        self.assertEqual((first["chain"], first["hop"], first["claim_id"]), (1, 1, 1))
        self.assertEqual(second["hop"], 2)
        self.assertEqual(second["bridge_from"], "Kestrel relay / deployed on host")

    def test_the_seed_hop_is_not_emitted_twice(self) -> None:
        seeds = self._seed_store()
        result = self.ask(
            "Which datacenter hosts the Kestrel relay?", ["Kestrel relay"],
            seed_claims=seeds,
        )
        identifiers = [row["claim_id"] for row in result["rows"]]
        self.assertEqual(len(identifiers), len(set(identifiers)))

    def test_a_screened_seed_hop_is_dropped_and_the_answer_survives(self) -> None:
        # The seed hop is context the walk did not need: when its row fails the
        # screen the chain reverts to what it would have been with no seed at
        # all, rather than suppressing a stored answer over an unrelated row.
        seeds = self._seed_store()
        self.db.execute("UPDATE memory_claims SET value='10.0.0.7' WHERE id=1")
        result = self.ask("Which datacenter is it in?", [], seed_claims=seeds)
        self.assertEqual(self.triples(result), [
            ("Harrier box", "datacenter", "Fenwick")
        ])
        self.assertEqual(result["rows"][0]["hop"], 1)
        self.assertNotIn("10.0.0.7", str(result["rows"]))
        self.assertEqual(result["report"]["excluded_by_screen"], 1)


class HoldoutV1RegressionTests(GraphTestCase):
    """The eight cases the sealed holdout v1 failed, by kind.

    The fixture and its score are quarantined and are never rescored; these
    reproduce the shapes that failed, in this module's own vocabulary, so the
    development battery catches a regression the one-use holdout no longer
    can.
    """

    def _iden_store(self) -> None:
        # A hosted-in store with look-alikes, as the holdout's iden store is.
        self.create_graph()
        self.add("Alder probe", "hosted in", "Kelpwood hall")
        self.add("Alder probe 2", "hosted in", "Redgate hall")
        self.add("Alderprobe", "hosted in", "Marrowfen hall")
        self.add("Cinder probe", "hosted in", "Oxbow hall")
        self.add("Dornick probe", "channel", "Bellrock link")

    def test_lookalike_an_exactly_spelled_subject_answers(self) -> None:
        # iden-lookalike-01/-03/-06/-08: the only asked word was a word of the
        # question's own subject, so the walk answered nothing at all.
        self._iden_store()
        result = self.ask("Where is the Alder probe hosted?", ["Alder probe"])
        self.assertEqual(result["report"]["mode"], "complete")
        self.assertEqual(self.triples(result), [
            ("Alder probe", "hosted in", "Kelpwood hall")
        ])
        for forbidden in ("Alder probe 2", "Alderprobe", "Redgate", "Marrowfen"):
            self.assertNotIn(forbidden, str(result["rows"]))

    def test_lookalike_an_asked_attribute_the_subject_lacks_still_abstains(self) -> None:
        # iden-lookalike-07: the exact key has no hosted-in edge, so the
        # question must not be answered from its channel instead.
        self._iden_store()
        result = self.ask("Where is the Dornick probe hosted?", ["Dornick probe"])
        self.assertEqual(result["rows"], [])
        self.assertEqual(result["report"]["mode"], "no-answer")

    def test_two_subjects_join_answers_for_both(self) -> None:
        # iden-twosubjects-01/-02: "hall" is an ordinary noun, not an
        # attribute, so the question is open and both exact starts answer.
        self._iden_store()
        result = self.ask(
            "Is the Alder probe hosted in the same hall as the Cinder probe?",
            ["Alder probe", "Cinder probe"],
        )
        self.assertEqual(result["report"]["mode"], "complete")
        self.assertEqual(
            {subject for subject, _p, _v in self.triples(result)},
            {"Alder probe", "Cinder probe"},
        )
        self.assertEqual(len({row["chain"] for row in result["rows"]}), 2)

    def test_two_subjects_join_answers_under_the_lane_identity_floor(self) -> None:
        # The lane abstained; exact starts must still answer (design 2.3d).
        self._iden_store()
        result = self.ask(
            "Is the Alder probe hosted in the same hall as the Cinder probe?",
            ["Alder probe", "Cinder probe"], exact_only=True,
        )
        self.assertEqual(result["report"]["mode"], "complete")
        self.assertEqual(len({row["chain"] for row in result["rows"]}), 2)

    def test_private_in_chain_an_unseparated_card_number(self) -> None:
        # leak-private-05: the fixture writes a Luhn-valid PAN with no
        # separators, and only the grouped form was matched, so it reached a
        # chain row.
        self.create_graph()
        self.add("Sable probe", "contact", "4532015112830366")
        self.assertEqual(
            redaction.private_identifier_kind("4532015112830366"), "card"
        )
        result = self.ask("What contact does the Sable probe use?", ["Sable probe"])
        self.assertEqual(result["rows"], [])
        self.assertEqual(result["report"]["mode"], "screened-rows")
        self.assertNotIn("4532015112830366", str(result))

    def test_private_in_chain_an_over_long_value(self) -> None:
        # leak-private-12: past the scan cap the screen cannot see the whole
        # value, so it must not vouch for it (design 2.4, "over-long value").
        self.create_graph()
        note = "abcdefghij" * 60
        self.assertGreater(len(note), redaction.SCAN_LIMIT)
        self.add("Fallow probe", "notes", note)
        self.assertEqual(redaction.private_identifier_kind(note), "long_value")
        result = self.ask("What notes does the Fallow probe carry?", ["Fallow probe"])
        self.assertEqual(result["rows"], [])
        self.assertEqual(result["report"]["mode"], "screened-rows")
        self.assertNotIn(note, str(result))

    def test_an_over_long_value_is_never_a_node(self) -> None:
        self.create_graph()
        self.add("Fallow probe", "notes", "abcdefghij" * 60)
        self.assertEqual(
            {str(row[0]) for row in self.db.execute(
                "SELECT value_kind FROM memory_graph_edges"
            )},
            {"literal"},
        )


class DesignTenThreeTests(GraphTestCase):
    """Design 10.3: the lane gate, the vocabulary rule, any-hop matching."""

    def _alias_store(self) -> None:
        self.create_graph()
        self.add("Marrow kiln", "district", "Ottery ward")
        self.add("Ottery ward", "charter", "Zeller lodge")
        self.add("Wynter barge", "moorage", "Brackle wharf")
        self.add("Wynter lodge", "almanac", "Larkspur press")

    def _identity_store(self) -> None:
        self.create_graph()
        self.add("Sorrel rig", "moorage", "Ashcombe wharf")
        self.add("Sorrel rig 2", "moorage", "Netherby wharf")
        self.add("Sorrelrig", "moorage", "Quarry wharf")

    # --- item 1: no lane mode disables non-exact resolution ----------------

    def test_no_lane_mode_forces_exact_only(self) -> None:
        for mode in ("identity-conflict", "identity-overflow", "or", "ambiguous",
                     "overflow", None, *graph.LANE_SILENT_MODES):
            with self.subTest(mode=mode):
                self.assertFalse(graph.lane_forces_exact_only(mode))

    def test_the_silent_modes_are_the_security_abstentions(self) -> None:
        self.assertEqual(
            graph.LANE_SILENT_MODES,
            frozenset({"screened", "project-unavailable", "corrupt-strongest",
                       "error"}),
        )

    def test_a_word_prefix_resolves_though_the_lane_abstained(self) -> None:
        # holdout v2 alias-prefix-unique, which the gate turned into no-start.
        self._alias_store()
        result = self.ask("Where is Marrow hosted?", ["Marrow"])
        self.assertEqual(result["report"]["mode"], "complete")
        self.assertIn(
            ("Marrow kiln", "district", "Ottery ward"), self.triples(result)
        )

    def test_a_last_word_alias_resolves_though_the_lane_abstained(self) -> None:
        # holdout v2 alias-lastword-unique.
        self._alias_store()
        result = self.ask("Where is Kiln hosted?", ["Kiln"])
        self.assertEqual(result["report"]["mode"], "complete")
        self.assertIn(
            ("Marrow kiln", "district", "Ottery ward"), self.triples(result)
        )

    def test_an_ambiguous_prefix_abstains_identity_conflict(self) -> None:
        # holdout v2 alias-prefix-ambiguous: the rule that raises it can only
        # run now that the gate is gone.
        self._alias_store()
        result = self.ask("Where is Wynter hosted?", ["Wynter"])
        self.assertEqual(result["rows"], [])
        self.assertEqual(result["report"]["mode"], "identity-conflict")

    def test_a_bare_lookalike_abstains_identity_conflict(self) -> None:
        # holdout v2 identity-nonexact-bare.
        self._identity_store()
        result = self.ask("Where is the Sorrel hosted?", ["Sorrel"])
        self.assertEqual(result["rows"], [])
        self.assertEqual(result["report"]["mode"], "identity-conflict")

    def test_the_security_floor_still_silences_non_exact_resolution(self) -> None:
        # The caller does not consult the graph at all for a security
        # abstention; what this pins is that exact_only still does its job
        # when it is asked for, so that floor keeps working.
        self._alias_store()
        result = self.ask("Where is Marrow hosted?", ["Marrow"], exact_only=True)
        self.assertEqual(result["rows"], [])
        self.assertEqual(result["report"]["mode"], "no-start")

    # --- item 2: the visible vocabulary decides ----------------------------

    def _scope_store(self) -> None:
        # The holdout scope store, the half project 2 can see.
        self.create_graph()
        self.add("Ombersley wharf", "district", "Fennimore ward", scope="global")
        self.add("Fennimore ward", "charter", "Duskmere lodge", scope="global")
        self.add("Aldwin barge", "moorage", "Wexlow wharf")
        self.add("Wexlow wharf", "district", "Tarrow ward")
        self.add("Tarrow ward", "charter", "Keldmoor lodge")

    def test_an_asked_predicate_the_store_has_answers_through_it(self) -> None:
        # holdout v2 acceptance pair, first half.
        self._scope_store()
        result = self.ask(
            "Which charter lists the Aldwin barge?", ["Aldwin barge"]
        )
        self.assertEqual(result["report"]["mode"], "complete")
        self.assertIn(
            ("Tarrow ward", "charter", "Keldmoor lodge"), self.triples(result)
        )

    def test_an_asked_word_the_store_never_heard_answers_nothing(self) -> None:
        # holdout v2 acceptance pair, second half: "almanac" belongs to the
        # invisible project, and the channel was answering with a moorage.
        self._scope_store()
        result = self.ask(
            "Which almanac lists the Aldwin barge?", ["Aldwin barge"]
        )
        self.assertEqual(result["rows"], [])
        self.assertEqual(result["report"]["mode"], "no-answer")

    def test_a_value_word_keeps_the_question_open(self) -> None:
        # holdout v1 iden-twosubjects-01 stays answered: a hall is a thing.
        self.create_graph()
        self.add("Alder probe", "hosted in", "Kelpwood hall")
        self.add("Cinder probe", "hosted in", "Oxbow hall")
        result = self.ask(
            "Is the Alder probe hosted in the same hall as the Cinder probe?",
            ["Alder probe", "Cinder probe"],
        )
        self.assertEqual(result["report"]["mode"], "complete")
        self.assertEqual(
            {subject for subject, _p, _v in self.triples(result)},
            {"Alder probe", "Cinder probe"},
        )

    def test_an_unreachable_attribute_still_returns_no_chain(self) -> None:
        # design 7.8 unchanged.
        self.create_graph()
        self.add("Kestrel relay", "deployed on host", "Harrier box")
        self.add("Harrier box", "datacenter", "Fenwick")
        result = self.ask(
            "Which region is the Kestrel relay in?", ["Kestrel relay"]
        )
        self.assertEqual(result["rows"], [])
        self.assertEqual(result["report"]["mode"], "no-answer")

    def test_the_vocabulary_probe_finds_a_word_anywhere_in_a_key(self) -> None:
        # Alone, first, last and in the middle of an entity key, and folded
        # through a trailing plural.
        self.create_graph()
        self.add("Aldwin barge", "moorage", "Wexlow wharf")
        self.add("Tarrow ward", "charter", "Keldmoor lodge")
        scopes = ("global", "project:1")
        found = graph._vocabulary_hits(
            self.db, scopes,
            frozenset({"barge", "aldwin", "wharf", "barges", "almanac", "ward"}),
        )
        self.assertEqual(
            found, frozenset({"barge", "aldwin", "wharf", "barges", "ward"})
        )
        self.assertNotIn("almanac", found)

    def test_the_probe_agrees_with_the_whole_vocabulary(self) -> None:
        # The cheap form and the exhaustive one must not disagree.
        self._scope_store()
        scopes = ("global", "project:1")
        whole = graph._visible_entity_words(self.db, scopes)
        words = frozenset({"barge", "wharf", "almanac", "keldmoor", "ledger"})
        probed = graph._vocabulary_hits(self.db, scopes, words)
        self.assertEqual(
            probed, frozenset(word for word in words if word in whole)
        )

    # --- item 3: any hop answers -------------------------------------------

    def test_a_matching_hop_carries_the_hops_behind_it(self) -> None:
        # The design 1.1 case: Northgate is a stored value with two reverse
        # hops behind it, and the terminal-only rule dropped them.
        self.seed_bridge_store()
        result = self.ask(
            "Which relays are in the Northgate region?", ["Northgate"]
        )
        self.assertEqual(result["report"]["mode"], "complete")
        triples = self.triples(result)
        self.assertIn(("Fenwick", "region", "Northgate"), triples)
        self.assertIn(
            ("Kestrel relay", "deployed on host", "Harrier box"), triples
        )

    def test_a_matching_terminal_still_ranks_first(self) -> None:
        self.seed_bridge_store()
        result = self.ask(
            "Which relays are in the Northgate region?", ["Northgate"]
        )
        self.assertEqual(result["rows"][0]["predicate"], "region")

    def test_a_chain_with_no_matching_hop_is_still_dropped(self) -> None:
        self.create_graph()
        self.add("Kestrel relay", "listen port", "9090")
        self.add("Kestrel relay", "deployed on host", "Harrier box")
        result = self.ask(
            "Which datacenter hosts the Kestrel relay?", ["Kestrel relay"]
        )
        self.assertEqual(result["rows"], [])
        self.assertEqual(result["report"]["mode"], "no-answer")

    # --- item 4: rows carry scope ------------------------------------------

    def test_every_chain_row_carries_its_scope(self) -> None:
        self.create_graph()
        self.add("Ombersley wharf", "district", "Fennimore ward", scope="global")
        self.add("Aldwin barge", "moorage", "Wexlow wharf")
        result = self.ask("", ["Aldwin barge", "Ombersley wharf"])
        self.assertTrue(result["rows"])
        self.assertEqual(
            {row["scope"] for row in result["rows"]}, {"global", "project:1"}
        )


class DesignTenSevenTests(GraphTestCase):
    """Design 10.7: the four resolver and marker rulings after holdout v3."""

    # --- item 1: alias ambiguity abstains, never no-start -------------------

    def test_two_last_word_aliases_abstain_identity_conflict(self) -> None:
        # v3 c-al-03.  alias_subject returns the input unchanged when two keys
        # share the last word, which left the candidate list empty and fell
        # through to no-start; two candidates are an ambiguity.
        self.create_graph()
        self.add("Marchbank Loom8", "parish", "Alder ward")
        self.add("Pendreth Loom8", "parish", "Birch ward")
        result = self.ask("What is the parish of Loom8?", ["Loom8"])
        self.assertEqual(result["rows"], [])
        self.assertEqual(result["report"]["mode"], "identity-conflict")

    def test_a_single_last_word_alias_still_resolves(self) -> None:
        self.create_graph()
        self.add("Quarrenden Loom7", "parish", "Alder ward")
        result = self.ask("What is the parish of Loom7?", ["Loom7"])
        self.assertEqual(self.triples(result), [
            ("Quarrenden Loom7", "parish", "Alder ward")
        ])

    # --- item 3: seeds never answer for an unidentified name ----------------

    def _yealand_store(self) -> list[dict[str, object]]:
        self.create_graph()
        mill = self.add("Yealand mill", "parish", "Zennorly fold")
        self.add("Yealand croft", "pasture", "Zennorly fold")
        return [{
            "claim_id": mill, "subject": "Yealand mill",
            "predicate": "parish", "value": "Zennorly fold",
        }]

    def test_a_seed_cannot_answer_for_a_name_the_store_cannot_identify(self) -> None:
        # v3 c-al-05, the run's one leak: the lane OR-matched "Yealand" and
        # handed over a row about the mill for a question about an unknown
        # fold; both endpoints were exact keys and the graph answered.
        seeds = self._yealand_store()
        result = self.ask(
            "What is the parish of the Yealand fold?", ["Yealand fold"], seed_claims=seeds
        )
        self.assertEqual(result["rows"], [])
        self.assertEqual(result["report"]["mode"], "no-start")
        self.assertNotIn("Yealand mill", str(result))

    def test_a_seed_still_adds_a_start_beside_a_resolved_name(self) -> None:
        seeds = self._yealand_store()
        result = self.ask(
            "What is the parish of the Yealand mill?", ["Yealand mill"], seed_claims=seeds
        )
        self.assertEqual(result["report"]["mode"], "complete")
        self.assertIn(
            ("Yealand mill", "parish", "Zennorly fold"), self.triples(result)
        )

    def test_a_seed_still_adds_a_start_when_no_subject_was_named(self) -> None:
        seeds = self._yealand_store()
        result = self.ask("What is the parish?", [], seed_claims=seeds)
        self.assertEqual(result["report"]["mode"], "complete")
        self.assertIn(
            ("Yealand mill", "parish", "Zennorly fold"), self.triples(result)
        )

    # --- item 4: the unresolved report field --------------------------------

    def _tarnworth_store(self) -> None:
        self.create_graph()
        self.add("Thornbeck bolt", "parish", "Cinder ward")
        self.add("Tarnworth bolt", "parish", "Dowel ward")
        self.add("Tarnworth bolt 2", "parish", "Ember ward")
        self.add("Tarnworthbolt", "parish", "Flint ward")

    def test_one_unidentified_name_does_not_abstain_a_resolved_one(self) -> None:
        # v3 c-ts-03: answer from the resolved start and name the other.
        self._tarnworth_store()
        result = self.ask(
            "What is the parish of the Tarnworth mill and the Thornbeck bolt?",
            ["Tarnworth mill", "Thornbeck bolt"],
        )
        self.assertEqual(result["report"]["mode"], "complete")
        self.assertEqual(self.triples(result), [
            ("Thornbeck bolt", "parish", "Cinder ward")
        ])
        self.assertEqual(result["report"]["unresolved"], ["Tarnworth mill"])

    def test_a_word_prefix_of_nothing_is_no_start_not_identity_conflict(self) -> None:
        # v3 c-lk-02 / c-lk-04 and 10.7 item 2: the store has bolts and has
        # never heard of a mill.
        self._tarnworth_store()
        result = self.ask(
            "What is the parish of the Tarnworth mill?", ["Tarnworth mill"]
        )
        self.assertEqual(result["rows"], [])
        self.assertEqual(result["report"]["mode"], "no-start")
        self.assertEqual(result["report"]["unresolved"], ["Tarnworth mill"])

    def test_a_failed_non_exact_name_still_abstains_the_whole_call(self) -> None:
        # v3 c-ts-04: ambiguity is not the same as absence.
        self.create_graph()
        self.add("Marchbank Loom8", "parish", "Alder ward")
        self.add("Pendreth Loom8", "parish", "Birch ward")
        self.add("Thornbeck bolt", "parish", "Cinder ward")
        result = self.ask(
            "What is the parish of Loom8 and the Thornbeck bolt?",
            ["Loom8", "Thornbeck bolt"],
        )
        self.assertEqual(result["rows"], [])
        self.assertEqual(result["report"]["mode"], "identity-conflict")

    def test_unresolved_keeps_question_order_and_is_capped(self) -> None:
        self._tarnworth_store()
        result = self.ask(
            "Where are the Alpha mill, the Beta mill and the Gamma mill?",
            ["Alpha mill", "Beta mill", "Gamma mill"],
        )
        self.assertEqual(result["report"]["unresolved"], ["Alpha mill", "Beta mill"])

    def test_unresolved_is_screened_like_a_label(self) -> None:
        # A typed name can carry a private identifier as easily as a stored
        # one, and it goes back to the model in the not-recorded line.
        self._tarnworth_store()
        result = self.ask(
            "Where is it?", ["ops@example.com", "Alpha mill"]
        )
        self.assertEqual(result["report"]["unresolved"], ["Alpha mill"])
        self.assertNotIn("ops@example.com", str(result))

    def test_a_seed_about_a_lookalike_of_an_unresolved_name_is_dropped(self) -> None:
        # Live two-subject run: the lane OR-scans, so a question about an
        # unknown "Tarnworth mill" comes back with a row about "Tarnworth
        # bolt 2".  Printed beside the not-recorded line for the mill, that
        # row reads as the answer to it.
        self._tarnworth_store()
        vatworks = self.add("Thornbeck bolt", "vatworks", "Uplyme mill")
        bolt2 = self.db.execute(
            "SELECT id FROM memory_claims WHERE subject='Tarnworth bolt 2'"
        ).fetchone()[0]
        seeds = [
            {"claim_id": int(bolt2), "subject": "Tarnworth bolt 2",
             "predicate": "parish", "value": "Ember ward"},
            {"claim_id": int(vatworks), "subject": "Thornbeck bolt",
             "predicate": "vatworks", "value": "Uplyme mill"},
        ]
        result = self.ask(
            "What is the parish of the Tarnworth mill and the Thornbeck bolt?",
            ["Tarnworth mill", "Thornbeck bolt"], seed_claims=seeds,
        )
        self.assertEqual(result["report"]["mode"], "complete")
        self.assertEqual(self.triples(result), [
            ("Thornbeck bolt", "parish", "Cinder ward")
        ])
        self.assertNotIn("tarnworth", str(result["rows"]).casefold())
        self.assertEqual(result["report"]["unresolved"], ["Tarnworth mill"])

    def test_a_seed_is_kept_when_every_named_subject_resolved(self) -> None:
        # The drop keys on names that resolved NOTHING; with both names
        # resolved there is no not-recorded line for a seed to be read as.
        self._tarnworth_store()
        bolt = self.db.execute(
            "SELECT id FROM memory_claims WHERE subject='Tarnworth bolt'"
        ).fetchone()[0]
        seeds = [{"claim_id": int(bolt), "subject": "Tarnworth bolt",
                  "predicate": "parish", "value": "Dowel ward"}]
        result = self.ask(
            "What is the parish of the Tarnworth bolt and the Thornbeck bolt?",
            ["Tarnworth bolt", "Thornbeck bolt"], seed_claims=seeds,
        )
        self.assertEqual(result["report"]["mode"], "complete")
        self.assertEqual(result["report"]["unresolved"], [])
        self.assertIn(
            ("Tarnworth bolt", "parish", "Dowel ward"), self.triples(result)
        )

    def test_the_lookalike_seed_drop_uses_the_post_resolution_set(self) -> None:
        # A one-word name that resolves non-exactly is NOT unresolved, so a
        # seed sharing its first word survives.
        self.create_graph()
        self.add("Quarrenden Loom7", "parish", "Alder ward")
        seed_id = self.add("Quarrenden Loom7", "vatworks", "Uplyme mill")
        seeds = [{"claim_id": seed_id, "subject": "Quarrenden Loom7",
                  "predicate": "vatworks", "value": "Uplyme mill"}]
        starts, mode, unresolved = graph.resolve_starts(
            self.db, subjects=["Loom7"], seed_claims=seeds,
            visible_scopes=("global", "project:1"),
        )
        self.assertIsNone(mode)
        self.assertEqual(unresolved, [])
        self.assertIn(
            "uplyme mill", {str(start["entity_key"]) for start in starts}
        )

    # The boundary of the G5 drop: it removes seed-derived starts and nothing
    # else.  A named subject that is itself a look-alike of a name the store
    # could not identify must still answer -- that is the difference between a
    # safe over-drop and a real miss, and it is what a later narrowing of the
    # rule would be most likely to get wrong.

    def test_an_exact_named_start_survives_a_lookalike_unresolved_name(self) -> None:
        self._tarnworth_store()
        result = self.ask(
            "What is the parish of the Tarnworth bolt and the Tarnworth mill?",
            ["Tarnworth bolt", "Tarnworth mill"],
        )
        self.assertEqual(result["report"]["mode"], "complete")
        self.assertEqual(self.triples(result), [
            ("Tarnworth bolt", "parish", "Dowel ward")
        ])
        self.assertEqual(result["report"]["unresolved"], ["Tarnworth mill"])

    def test_a_non_exact_named_start_survives_a_lookalike_unresolved_name(self) -> None:
        # The same boundary for a start that resolved through the alias rule
        # rather than exactly: it is added outside the seed loop too.
        self.create_graph()
        self.add("Quarrenden Loom7", "parish", "Alder ward")
        result = self.ask(
            "What is the parish of Loom7 and the Quarrenden mill?",
            ["Loom7", "Quarrenden mill"],
        )
        self.assertEqual(result["report"]["mode"], "complete")
        self.assertEqual(self.triples(result), [
            ("Quarrenden Loom7", "parish", "Alder ward")
        ])
        self.assertEqual(result["report"]["unresolved"], ["Quarrenden mill"])

    def test_the_drop_bites_the_seed_and_spares_the_named_start(self) -> None:
        # The shape that proves the filter is reached and discriminating,
        # rather than simply never firing: one store, one unresolved name, a
        # seed dropped and a named chain kept.
        self._tarnworth_store()
        bolt2 = self.db.execute(
            "SELECT id FROM memory_claims WHERE subject='Tarnworth bolt 2'"
        ).fetchone()[0]
        seeds = [{"claim_id": int(bolt2), "subject": "Tarnworth bolt 2",
                  "predicate": "parish", "value": "Ember ward"}]
        result = self.ask(
            "What is the parish of the Tarnworth bolt and the Tarnworth mill?",
            ["Tarnworth bolt", "Tarnworth mill"], seed_claims=seeds,
        )
        self.assertEqual(result["report"]["mode"], "complete")
        self.assertEqual(self.triples(result), [
            ("Tarnworth bolt", "parish", "Dowel ward")
        ])
        self.assertNotIn("Ember ward", str(result["rows"]))
        self.assertEqual(result["report"]["unresolved"], ["Tarnworth mill"])

    # --- item 6: a chain that ENDS at an overflowing hub --------------------

    def test_a_chain_ending_at_an_overflowing_hub_is_incomplete(self) -> None:
        # v3 c-ic-03: the overflow entry was recorded and the chain returned
        # unmarked.  The sibling that ends elsewhere stays complete.
        self.create_graph()
        self.add("Ravensmere fold", "moot", "Kirkhollow hall")
        self.add("Ravensmere fold", "emblem", "Amber lozenge")
        for index in range(40):
            self.add(f"Parishioner{index}", "parish", "Kirkhollow hall")
        result = self.ask("", ["Ravensmere fold"])
        by_value = {row["value"]: row for row in result["rows"]}
        self.assertIn("Kirkhollow hall", by_value)
        self.assertIn("Amber lozenge", by_value)
        self.assertTrue(by_value["Kirkhollow hall"].get("incomplete"))
        self.assertFalse(by_value["Amber lozenge"].get("incomplete"))
        notes = [entry for entry in result["overflow"]
                 if entry["subject"] == "Kirkhollow hall"]
        self.assertTrue(notes)
        # The hub sits one hop past the row that ends at it.  Asserted
        # relatively, not as a bare 2: when the lane seeds the hub row the
        # hub is itself a start and the same overflow is reported at hop 1,
        # so a pinned number would be pinning what the lane happened to hand
        # over rather than the graph.
        self.assertEqual(
            notes[0]["hop"], by_value["Kirkhollow hall"]["hop"] + 1
        )

    def test_a_chain_passing_through_a_hub_is_still_wholly_marked(self) -> None:
        self.create_graph()
        self.add("Thornbeck bolt", "moot", "Yealand mill")
        self.add("Yealand mill", "parish", "Kirkhollow hall")
        for index in range(40):
            self.add(f"Parishioner{index}", "parish", "Kirkhollow hall")
        result = self.ask("", ["Thornbeck bolt"])
        self.assertTrue(result["rows"])
        self.assertTrue(all(row.get("incomplete") for row in result["rows"]))


class BudgetTests(GraphTestCase):
    def test_an_expired_deadline_stops_the_traversal_and_says_so(self) -> None:
        self.seed_bridge_store()
        result = self.ask(
            "Which region is the Kestrel relay in?", ["Kestrel relay"],
            deadline=time.monotonic() - 1.0,
        )
        self.assertEqual(result["report"]["mode"], "budget-exceeded")
        self.assertEqual(result["report"]["budget"], "time")
        self.assertEqual(result["rows"], [])

    def test_a_deadline_that_expires_in_the_screen_loop_returns_what_was_screened(self) -> None:
        self.seed_bridge_store()
        started = time.monotonic()
        scope_sql, scope_params = graph.default_scope_filter(
            ("global", "project:1"), "project:1"
        )
        walk = graph.graph_walk(
            self.db, query="Which region is the Kestrel relay in?",
            visible_scopes=("global", "project:1"), scope_sql=scope_sql,
            scope_params=scope_params, subjects=["Kestrel relay"],
        )
        rows = {
            int(row["id"]): dict(row) for row in self.db.execute(
                "SELECT * FROM memory_claims WHERE id IN "
                f"({','.join('?' for _ in walk['claim_ids'])})", walk["claim_ids"]
            )
        }
        result = graph.assemble_rows(
            walk, rows, deadline=time.monotonic() - 1.0, started=started
        )
        self.assertEqual(result["report"]["mode"], "budget-exceeded")
        self.assertEqual(result["report"]["budget"], "time")

    def test_the_row_cap_marks_what_it_cut(self) -> None:
        self.create_graph()
        self.add("Kestrel relay", "deployed on host", "Harrier box")
        for index in range(12):
            self.add(f"Rack{index}", "deployed on host", "Harrier box")
        result = self.ask(
            "What is deployed on the Harrier box?", ["Harrier box"], limit=4
        )
        self.assertLessEqual(len(result["rows"]), 4)
        self.assertTrue(all(row.get("incomplete") for row in result["rows"]))


# --- verify, rebuild and apply ------------------------------------------------

class VerifyTests(GraphTestCase):
    def test_a_clean_projection_verifies(self) -> None:
        self.seed_bridge_store()
        report = graph.verify_graph(self.db)
        self.assertTrue(report["ok"], report["problems"])
        self.assertEqual(report["edges"], report["edges_expected"])
        self.assertEqual(report["entities"], report["entities_expected"])

    def _kinds(self) -> set[str]:
        return {str(item["kind"]) for item in graph.verify_graph(self.db)["problems"]}

    def test_a_deleted_edge_is_a_missing_edge(self) -> None:
        self.seed_bridge_store()
        self.db.execute("DELETE FROM memory_graph_edges WHERE claim_id=2")
        self.assertIn("missing_edge", self._kinds())

    def test_an_edge_without_a_claim_is_an_extra_edge(self) -> None:
        self.seed_bridge_store()
        self.db.execute("PRAGMA foreign_keys=OFF")
        self.db.execute("DELETE FROM memory_claims WHERE id=3")
        self.assertIn("extra_edge", self._kinds())

    def test_an_edited_status_is_a_field_problem_naming_no_value(self) -> None:
        self.seed_bridge_store()
        self.db.execute(
            "UPDATE memory_graph_edges SET status='superseded' WHERE claim_id=2"
        )
        problems = [
            item for item in graph.verify_graph(self.db)["problems"]
            if item["kind"] == "field"
        ]
        self.assertTrue(problems)
        self.assertEqual(problems[0]["detail"], "status: differs")
        self.assertNotIn("Fenwick", problems[0]["detail"])

    def test_a_repointed_entity_is_an_entity_key_problem(self) -> None:
        self.seed_bridge_store()
        other = int(self.db.execute(
            "SELECT id FROM memory_graph_entities WHERE entity_key='northgate'"
        ).fetchone()[0])
        self.db.execute(
            "UPDATE memory_graph_edges SET src_entity_id=? WHERE claim_id=2", (other,)
        )
        self.assertIn("entity_key", self._kinds())

    def test_an_entity_with_no_edge_is_an_orphan(self) -> None:
        self.seed_bridge_store()
        self.db.execute("BEGIN IMMEDIATE")
        entity_id = graph.allocate_entity_id(self.db)
        self.db.execute(
            "INSERT INTO memory_graph_entities(id, scope, entity_key, label, created_at) "
            "VALUES (?, 'project:1', 'ghost box', 'ghost box', ?)",
            (entity_id, _STAMP),
        )
        self.db.execute("COMMIT")
        self.assertIn("orphan_entity", self._kinds())

    def test_a_behind_sequence_is_reported(self) -> None:
        self.seed_bridge_store()
        self.db.execute("UPDATE memory_graph_entity_sequence SET next_id=1 WHERE id=1")
        self.assertIn("sequence", self._kinds())

    def test_the_column_itself_refuses_an_overlong_label(self) -> None:
        self.seed_bridge_store()
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute(
                "UPDATE memory_graph_entities SET label=? WHERE entity_key='fenwick'",
                ("H" * 90,),
            )

    def test_a_hand_edited_label_is_a_label_problem(self) -> None:
        self.seed_bridge_store()
        for label in ("Not The Same Name", "ops@10.0.0.9"):
            with self.subTest(label=label):
                self.db.execute(
                    "UPDATE memory_graph_entities SET label=? WHERE entity_key='fenwick'",
                    (label,),
                )
                self.assertIn("label", self._kinds())
        self.db.execute(
            "UPDATE memory_graph_entities SET label='Fenwick' WHERE entity_key='fenwick'"
        )
        self.assertTrue(graph.verify_graph(self.db)["ok"])

    def test_a_different_first_spelling_is_not_a_divergence(self) -> None:
        # label is display-only: it is excluded from the equivalence tuple.
        self.seed_bridge_store()
        self.db.execute(
            "UPDATE memory_graph_entities SET label='FENWICK' WHERE entity_key='fenwick'"
        )
        self.assertTrue(graph.verify_graph(self.db)["ok"])

    def test_an_edge_for_an_excluded_claim_is_reported(self) -> None:
        self.seed_bridge_store()
        self.db.execute("UPDATE memory_claims SET subject='ops@10.0.0.9' WHERE id=2")
        self.assertIn("screen", self._kinds())


class RebuildTests(GraphTestCase):
    def test_the_dry_run_reports_every_tamper_and_apply_reconciles(self) -> None:
        self.seed_bridge_store()
        self.db.execute(
            "UPDATE memory_graph_edges SET status='superseded' WHERE claim_id=2"
        )
        self.db.execute("DELETE FROM memory_graph_edges WHERE claim_id=3")
        plan = graph.rebuild_graph_projection(self.db)
        self.assertFalse(plan["ok"])
        self.assertGreaterEqual(len(plan["divergences"]), 2)
        self.db.execute("BEGIN IMMEDIATE")
        applied = graph.apply_graph_projection(
            self.db, self.key, plan, now=_stamp(90)
        )
        self.db.execute("COMMIT")
        self.assertTrue(applied["ok"])
        self.assertTrue(graph.rebuild_graph_projection(self.db)["ok"])
        payload = json.loads(self.db.execute(
            "SELECT payload_json FROM memory_spine_events WHERE id=?",
            (applied["event_id"],),
        ).fetchone()[0])
        self.assertEqual(payload["projection"], "graph")
        self.assertGreaterEqual(payload["divergences_fixed"], 2)

    def test_a_rebuild_keeps_a_surviving_entity_id(self) -> None:
        self.seed_bridge_store()
        before = {
            str(row["entity_key"]): int(row["id"]) for row in self.db.execute(
                "SELECT id, entity_key FROM memory_graph_entities"
            )
        }
        self.db.execute(
            "UPDATE memory_graph_edges SET confidence=0.25 WHERE claim_id=2"
        )
        self.db.execute("BEGIN IMMEDIATE")
        graph.reproject(self.db, now=_stamp(90))
        self.db.execute("COMMIT")
        after = {
            str(row["entity_key"]): int(row["id"]) for row in self.db.execute(
                "SELECT id, entity_key FROM memory_graph_entities"
            )
        }
        self.assertEqual(before, after)

    def test_reprojecting_onto_a_new_entity_sweeps_the_one_it_left(self) -> None:
        # The correctness review: the old entity is orphaned by the repair
        # itself, so it is in no problem the dry run reported; sweeping only
        # the reported orphans left it behind and the residual verify rolled
        # the whole apply back, for ever.
        self.create_graph()
        self.add("Kestrel relay", "deployed on host", "Harrier box")
        self.add("Harrier box", "datacenter", "Fenwick")
        self.db.execute("UPDATE memory_claims SET value='Ravensbourne' WHERE id=2")
        self.db.execute("BEGIN IMMEDIATE")
        graph.reproject(self.db, now=_stamp(90))
        self.db.execute("COMMIT")
        report = graph.verify_graph(self.db)
        self.assertTrue(report["ok"], report["problems"])
        keys = {
            str(row[0]) for row in self.db.execute(
                "SELECT entity_key FROM memory_graph_entities"
            )
        }
        self.assertIn("ravensbourne", keys)
        self.assertNotIn("fenwick", keys)     # nothing points at it any more

    def test_the_four_tampers_reconcile_together(self) -> None:
        self.seed_bridge_store()
        other = int(self.db.execute(
            "SELECT id FROM memory_graph_entities WHERE entity_key='northgate'"
        ).fetchone()[0])
        self.db.execute(
            "UPDATE memory_graph_edges SET status='superseded' WHERE claim_id=1"
        )
        self.db.execute("DELETE FROM memory_graph_edges WHERE claim_id=3")
        self.db.execute(
            "UPDATE memory_graph_edges SET src_entity_id=? WHERE claim_id=2", (other,)
        )
        self.db.execute("UPDATE memory_claims SET value='Ravensbourne' WHERE id=2")
        plan = graph.rebuild_graph_projection(self.db)
        self.assertFalse(plan["ok"])
        self.db.execute("BEGIN IMMEDIATE")
        applied = graph.apply_graph_projection(self.db, self.key, plan, now=_stamp(95))
        self.db.execute("COMMIT")
        self.assertTrue(applied["ok"])
        self.assertTrue(graph.verify_graph(self.db)["ok"])

    def test_a_stale_plan_is_refused(self) -> None:
        self.seed_bridge_store()
        self.db.execute("DELETE FROM memory_graph_edges WHERE claim_id=3")
        plan = graph.rebuild_graph_projection(self.db)
        self.db.execute("DELETE FROM memory_graph_edges WHERE claim_id=2")
        self.db.execute("BEGIN IMMEDIATE")
        try:
            with self.assertRaises(graph.GraphError) as caught:
                graph.apply_graph_projection(self.db, self.key, plan, now=_stamp(90))
            self.assertEqual(caught.exception.code, "stale_plan")
        finally:
            self.db.execute("ROLLBACK")

    def test_apply_refuses_outside_a_transaction(self) -> None:
        self.seed_bridge_store()
        with self.assertRaises(graph.GraphError) as caught:
            graph.apply_graph_projection(self.db, self.key, None, now=_stamp(90))
        self.assertEqual(caught.exception.code, "not_in_transaction")

    def test_reproject_removes_an_edge_whose_claim_is_gone(self) -> None:
        self.seed_bridge_store()
        self.db.execute("PRAGMA foreign_keys=OFF")
        self.db.execute("DELETE FROM memory_claims WHERE id=3")
        self.db.execute("BEGIN IMMEDIATE")
        result = graph.reproject(self.db, now=_stamp(90))
        self.db.execute("COMMIT")
        self.assertEqual(result["removed_ids"], [3])
        self.assertTrue(graph.verify_graph(self.db)["ok"])

    def test_divergence_signature_separates_claim_and_entity_problems(self) -> None:
        self.seed_bridge_store()
        self.db.execute("DELETE FROM memory_graph_edges WHERE claim_id=3")
        signature = graph.divergence_signature(graph.rebuild_graph_projection(self.db))
        self.assertTrue(signature)
        self.assertTrue(all(len(item) == 3 for item in signature))


if __name__ == "__main__":  # pragma: no cover - convenience
    unittest.main()
