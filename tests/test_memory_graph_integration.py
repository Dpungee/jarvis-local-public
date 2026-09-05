"""M3 store-integration exit tests: the temporal graph at schema 48, ordinary
memory erasure with receipts, and the M-6 backing-content collision.

Every test seeds through public writers (``remember_explicit_project_claim``,
``remember_claim``, ``retract_...``, ``erase_...``, ``remember_verified``) except
where a tamper or a legacy shape is the point, which is stated at that site.
The design of record is the M3 design (see ``docs/MEMORY_GRAPH.md``); the
section numbers in the test names are its exit tests (§7).
"""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import time
import unittest
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from typing import Any

from jarvis import memory_graph, memory_spine
from jarvis.governed_memory import GovernedMemoryCommandError
from jarvis.memory import (
    Memory,
    SCHEMA_VERSION,
    backing_content_variants,
    now_iso,
)
from jarvis.redaction import (
    contains_private_identifier_extended,
    contains_secret,
    screen_endpoint,
)
from tests.legacy_store_fixture import strip_spine

# ``Memory.graph_chains`` takes its whole-call deadline from
# ``memory_graph.TIME_BUDGET_MS`` at entry, and honouring that deadline means
# returning fewer rows with mode ``budget-exceeded``.  That is the product
# behaving correctly, but it makes every assertion about a mode, a row count
# or a chain's contents a wall-clock race: under full-suite CPU load the real
# 25 ms deadline can expire inside the screen phase of a read that takes
# 0.15 ms on an idle machine.  So the shared fixture below runs every test
# under a generous budget, and only the tests whose subject *is* the budget
# put it back (or force it to zero).  The product default is captured at
# import, before any patch, so the budget tests never hard-code 25.
PRODUCT_TIME_BUDGET_MS = float(memory_graph.TIME_BUDGET_MS)
GENEROUS_TIME_BUDGET_MS = 5_000.0


def _user_version_on_disk(db_path) -> int:
    """The schema marker of the CLOSED store, read without opening ``Memory``.

    Opening ``Memory`` would migrate it, which is the thing under test -- so
    the precondition has to be measured on the file.  It is measured rather
    than assumed because the point is to catch a harness that stopped
    producing an old store: a downgrade helper that silently no-ops would
    otherwise leave every migration test passing while testing nothing.
    """
    raw = sqlite3.connect(str(db_path))
    try:
        return int(raw.execute("PRAGMA user_version").fetchone()[0])
    finally:
        raw.close()


@contextmanager
def time_budget(milliseconds: float) -> Iterator[None]:
    """Run the block with ``memory_graph.TIME_BUDGET_MS`` fixed."""
    with patch.object(memory_graph, "TIME_BUDGET_MS", float(milliseconds)):
        yield


# The 7.9 latency gates are wall clock, and wall clock is a property of the
# machine as much as of the code: on an idle host the backfill is 1.1 s
# against its 2 s budget, and under twenty-way contention the same code takes
# 4.8 s.  Boss ruling: the gates are enforced only when
# ``JARVIS_ENFORCE_TIMING_GATES=1`` is set, which is how the number for the
# record is produced -- once, on an idle host.  Every other run measures the
# same figures, prints them, and passes, so the suite never turns machine
# contention into a red test and never silently stops measuring.  Loosening
# the budgets themselves is a boss decision (design 1.3), not a test-side one.
ENFORCE_TIMING_GATES = os.environ.get("JARVIS_ENFORCE_TIMING_GATES") == "1"


# The ten tables that carry a ``memory_id`` column on a live store, minus
# ``memory_claims`` (design 6.1, review R10).  The test asserts the DERIVED
# list equals this one, so a table added later fails here instead of silently
# keeping a row that points at an erased memory.
DOCUMENTED_MEMORY_DEPENDENT_TABLES = [
    "lesson_applications",
    "lesson_controls",
    "lesson_provenance",
    "memory_embedding_leases",
    "memory_embeddings",
    "memory_retrievals",
    "memory_statistics",
    "ordinary_memory_provenance",
    "ordinary_memory_quality_assessments",
    "strategy_transfer_applications",
]

# Values for columns whose CHECK constraint is a closed enum, so the generic
# planter below can fill every dependent table.
_ENUM_COLUMN_VALUES = {
    "family": "code_fix",
    "source_family": "code_fix",
    # strategy_transfer_applications CHECKs source_family <> target_family.
    "target_family": "code_test",
    "lifecycle_status": "active",
    "channel": "lexical",
    "strategy": "verify_output",
    "mode": "observe",
    # ...and CHECKs mode IN ('advise','trial') OR applied=0.
    "applied": 0,
    "origin": "explicit_operator_memory",
}


def memory_lane_silencing_modes() -> frozenset[str]:
    """The claims-lane modes that silence the graph channel (design 5.6.1)."""
    from jarvis.memory import _LANE_SILENCING_MODES

    return _LANE_SILENCING_MODES


def _command(subject: str, predicate: str, value: str) -> str:
    return "Remember this project fact: " + json.dumps(
        {"subject": subject, "predicate": predicate, "value": value},
        ensure_ascii=False, separators=(",", ":"),
    )


def _forget(subject: str, predicate: str) -> str:
    return "Forget this project fact: " + json.dumps(
        {"subject": subject, "predicate": predicate}, separators=(",", ":")
    )


def _erase(subject: str, predicate: str) -> str:
    return "Erase this project fact: " + json.dumps(
        {"subject": subject, "predicate": predicate}, separators=(",", ":")
    )


class _GraphStoreCase(unittest.TestCase):
    """One temporary store per test, with its key sidecar removed on the way
    out (the sidecar lives beside the database and a leaked one would let a
    later test read a store it did not write)."""

    def setUp(self) -> None:
        # Content assertions must not race the wall clock (see the note on
        # GENEROUS_TIME_BUDGET_MS above); the budget-behaviour tests override
        # this locally, and a nested patch restores it on the way out.
        budget = patch.object(
            memory_graph, "TIME_BUDGET_MS", GENEROUS_TIME_BUDGET_MS
        )
        budget.start()
        self.addCleanup(budget.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp.name) / "data"
        self.data_dir.mkdir()
        self.db_path = self.data_dir / "jarvis.db"
        self.memory = Memory(self.db_path)

    def tearDown(self) -> None:
        self.memory.close()
        sidecar = Path(str(self.db_path) + memory_spine.KEY_SIDECAR_SUFFIX)
        if sidecar.exists():
            sidecar.unlink()
        self.temp.cleanup()

    # --- helpers -------------------------------------------------------------

    def _reopen(self) -> None:
        self.memory.close()
        self.memory = Memory(self.db_path)

    def _edges(self) -> list[tuple]:
        return [
            tuple(row) for row in self.memory.db.execute(
                """SELECT e.claim_id, e.scope, source_entity.entity_key,
                          e.predicate_key, destination.entity_key, e.value_kind,
                          e.status
                   FROM memory_graph_edges AS e
                   JOIN memory_graph_entities AS source_entity
                     ON source_entity.id=e.src_entity_id
                   LEFT JOIN memory_graph_entities AS destination
                     ON destination.id=e.dst_entity_id
                   ORDER BY e.claim_id"""
            )
        ]

    def _entity_keys(self) -> set[tuple[str, str]]:
        return {
            (str(row[0]), str(row[1])) for row in self.memory.db.execute(
                "SELECT scope, entity_key FROM memory_graph_entities"
            )
        }

    def _count(self, sql: str, parameters: tuple = ()) -> int:
        return int(self.memory.db.execute(sql, parameters).fetchone()[0])

    def _events(self, kind: str) -> list[dict]:
        return [
            json.loads(str(row[0])) for row in self.memory.db.execute(
                """SELECT payload_json FROM memory_spine_events
                   WHERE kind=? AND payload_json IS NOT NULL ORDER BY id""",
                (kind,),
            ).fetchall()
        ]

    def _seed_chain(self, conversation: int) -> None:
        """relay -> box -> datacenter -> region, plus one literal terminal."""
        for subject, predicate, value in (
            ("Kestrel relay", "deployed on host", "Harrier box"),
            ("Harrier box", "datacenter", "Fenwick"),
            ("Fenwick", "region", "Northgate"),
            ("Kestrel relay", "listen port", "9090"),
        ):
            self.memory.remember_explicit_project_claim(
                conversation, 1, _command(subject, predicate, value)
            )

    def _plant_dependent_row(self, table: str, memory_id: int) -> None:
        """Fill one dependent table's NOT NULL columns for ``memory_id``.

        A tamper by construction: several of these tables are only ever
        written by a pipeline with its own predictions and reflections, and
        the point of the test is that the erase clears the table whatever
        wrote it.  Foreign keys are off for the plant only.
        """
        columns: list[str] = []
        values: list[object] = []
        for row in self.memory.db.execute(f'PRAGMA table_info("{table}")'):
            name, declared, not_null, default = str(row[1]), str(row[2] or "").upper(), int(row[3]), row[4]
            if name == "memory_id":
                columns.append(name)
                values.append(int(memory_id))
                continue
            if not not_null or default is not None:
                continue
            if name in _ENUM_COLUMN_VALUES:
                values.append(_ENUM_COLUMN_VALUES[name])
            elif name.endswith("_sha256"):
                values.append("0" * 64)
            elif "INT" in declared:
                values.append(1)
            elif "REAL" in declared or "FLOA" in declared or "DOUB" in declared:
                values.append(0.5)
            elif name.endswith("_at") or name == "valid_until":
                values.append(now_iso())
            else:
                values.append("x")
            columns.append(name)
        placeholders = ", ".join("?" for _column in columns)
        self.memory.db.execute("PRAGMA foreign_keys=OFF")
        try:
            self.memory.db.execute(
                f'INSERT OR REPLACE INTO "{table}"({", ".join(columns)}) '
                f"VALUES ({placeholders})",
                values,
            )
        finally:
            self.memory.db.execute("PRAGMA foreign_keys=ON")


class MigrationTo48Tests(_GraphStoreCase):
    """Exit test 7.10 (and 7.16's no-write assertion): migration 47 -> 48.

    The class name is a PHASE MARKER recording which migration introduced
    these cases, and it stays: that is a true historical statement.  The
    method names below do not carry a version where they would be stating an
    OUTCOME, because the outcome is now whatever ``SCHEMA_VERSION`` is.
    """

    def _seed_the_three_exclusion_categories(self) -> None:
        conversation = self.memory.new_conversation(project_id=1)
        self._seed_chain(conversation)
        # A preference is a global claim ``user / preference:<name>``: the
        # reserved namespace, so the graph excludes it entirely.
        self.memory.set_preference("editor", "vim", source="user")
        # The global claim API screens a subject for secrets only, so both of
        # these are stored and the graph is the first gate they meet.
        self.memory.remember_claim(
            "10.0.0.7", "hosts", "Kestrel relay",
            source="fixture", authority="verified",
        )
        self.memory.remember_claim(
            "Kestrel relay " * 8, "owner", "Dana",
            source="fixture", authority="verified",
        )

    def test_a_fresh_store_is_current_with_a_projected_graph(self) -> None:
        self.assertEqual(SCHEMA_VERSION, 50)
        self.assertEqual(
            self._count("SELECT COUNT(*) FROM sqlite_master WHERE type='table' "
                        "AND name IN ('memory_graph_edges','memory_graph_entities',"
                        "'memory_graph_entity_sequence')"),
            3,
        )
        self.assertTrue(self.memory._graph_ready)
        conversation = self.memory.new_conversation(project_id=1)
        self._seed_chain(conversation)
        self.assertEqual(
            self._edges(),
            [
                (1, "project:1", "kestrel relay", "deployed on host",
                 "harrier box", "entity", "active"),
                (2, "project:1", "harrier box", "datacenter", "fenwick",
                 "entity", "active"),
                (3, "project:1", "fenwick", "region", "northgate", "entity",
                 "active"),
                (4, "project:1", "kestrel relay", "listen port", None,
                 "literal", "active"),
            ],
        )

    def test_migration_47_to_48_projects_every_non_excluded_claim(self) -> None:
        self._seed_the_three_exclusion_categories()
        claims_before = [
            tuple(row) for row in self.memory.db.execute(
                "SELECT * FROM memory_claims ORDER BY id"
            )
        ]
        live_claims = self._count("SELECT COUNT(*) FROM memory_claims")
        receipts_before = len(self._events("projection.rebuilt"))
        # Become a schema-47 store: the graph is the only thing 48 added.
        self.memory.close()
        raw = sqlite3.connect(str(self.db_path))
        for statement in memory_graph.DROP_GRAPH_SQL:
            raw.execute(statement)
        raw.execute("PRAGMA user_version=47")
        raw.commit()
        raw.close()

        started = _user_version_on_disk(self.db_path)
        self.assertLess(
            started, SCHEMA_VERSION,
            "PRECONDITION: the store must START below current, or "
            "'it ended current' says only that nothing needed doing",
        )
        self.memory = Memory(self.db_path)
        self.assertEqual(
            int(self.memory.db.execute("PRAGMA user_version").fetchone()[0]),
            SCHEMA_VERSION,
            "migration did not carry the store to the current schema",
        )
        # Migration 48 reads memory_claims and writes nothing to it (design
        # 3.2 / review R9): no valid_until backfill, no re-stamping.
        self.assertEqual(
            [tuple(row) for row in self.memory.db.execute(
                "SELECT * FROM memory_claims ORDER BY id"
            )],
            claims_before,
        )
        receipts = self._events("projection.rebuilt")
        self.assertEqual(len(receipts), receipts_before + 1)
        receipt = receipts[-1]
        self.assertEqual(receipt["projection"], "graph")
        self.assertEqual(receipt["rows_before"], 0)
        self.assertEqual(
            set(receipt["excluded"]),
            {"excluded_predicate", "subject_private", "subject_too_long"},
        )
        self.assertEqual(receipt["excluded"]["excluded_predicate"], 1)
        self.assertEqual(receipt["excluded"]["subject_private"], 1)
        self.assertEqual(receipt["excluded"]["subject_too_long"], 1)
        excluded = sum(int(count) for count in receipt["excluded"].values())
        self.assertEqual(int(receipt["rows_after"]), live_claims - excluded)
        self.assertEqual(
            self._count("SELECT COUNT(*) FROM memory_graph_edges"),
            live_claims - excluded,
        )
        verification = self.memory.verify_graph()
        self.assertTrue(verification["ok"], verification["problems"][:5])
        self.assertTrue(self.memory.verify_spine()["ok"])
        # Neither excluded subject became an entity.
        keys = {key for _scope, key in self._entity_keys()}
        self.assertNotIn("10.0.0.7", keys)
        self.assertNotIn(memory_graph.entity_key("Kestrel relay " * 8), keys)
        self.assertNotIn("user", keys)
        # A reopen at 48 runs nothing.
        edges = self._edges()
        self._reopen()
        self.assertEqual(len(self._events("projection.rebuilt")), receipts_before + 1)
        self.assertEqual(self._edges(), edges)

    def test_a_stale_graph_is_dropped_before_the_46_and_47_steps(self) -> None:
        """Recommendation 7: the DROP sits at the top of ``_migrate``.

        Without it a store re-migrated from below 48 runs 46 and 47 against a
        graph whose edges hold foreign keys into the very ``memory_claims``
        rows those steps recreate.
        """
        conversation = self.memory.new_conversation(project_id=1)
        self._seed_chain(conversation)
        self.memory.remember_verified(
            "The deploy uses rsync.", source="operator",
            origin="explicit_operator_memory",
        )
        # strip_spine drops the graph too (review R13b), so put a stale one
        # back deliberately: this is the shape the top-of-_migrate DROP exists
        # for, and no other test can produce it.
        strip_spine(self.memory.db)
        memory_graph.create_graph_tables(self.memory.db)
        self.memory.db.execute(
            "INSERT INTO memory_graph_entity_sequence(id, next_id) VALUES (1, 1)"
        )
        self.memory.db.execute(
            """INSERT INTO memory_graph_entities(
                   id, scope, entity_key, label, created_at
               ) VALUES (1, 'project:1', 'stale relay', 'Stale relay', ?)""",
            (now_iso(),),
        )
        self.memory.db.execute(
            """INSERT INTO memory_graph_edges(
                   claim_id, scope, claim_key, src_entity_id, predicate_key,
                   dst_entity_id, value_kind, status, authority, confidence,
                   valid_from, valid_until, spine_event_id, projected_at
               ) VALUES (1, 'project:1', 'stale', 1, 'stale', NULL, 'literal',
                         'active', 'operator', 1.0, ?, NULL, 1, ?)""",
            (now_iso(), now_iso()),
        )
        self.memory.db.execute("PRAGMA user_version=45")
        self.memory.close()

        started = _user_version_on_disk(self.db_path)
        self.assertLess(
            started, SCHEMA_VERSION,
            "PRECONDITION: the store must START below current, or "
            "'it ended current' says only that nothing needed doing",
        )
        self.memory = Memory(self.db_path)
        self.assertEqual(
            int(self.memory.db.execute("PRAGMA user_version").fetchone()[0]),
            SCHEMA_VERSION,
            "migration did not carry the store to the current schema",
        )
        self.assertNotIn(
            ("project:1", "stale relay"), self._entity_keys(),
            "a stale entity survived the re-migration",
        )
        self.assertEqual(
            self._count("SELECT COUNT(*) FROM memory_graph_edges WHERE claim_key='stale'"),
            0,
        )
        verification = self.memory.verify_spine()
        self.assertTrue(verification["ok"], verification["problems"])
        self.assertTrue(verification["graph_ok"], verification)
        self.assertTrue(self.memory.rebuild_claim_projection()["ok"])
        self.assertTrue(self.memory.verify_graph()["ok"])
        self.assertEqual(
            self._count("SELECT COUNT(*) FROM memory_graph_edges"), 4
        )

    def test_a_legacy_store_below_46_migrates_46_47_and_48_in_one_transaction(self) -> None:
        conversation = self.memory.new_conversation(project_id=1)
        self._seed_chain(conversation)
        self.memory.remember("An unverified aside about the fleet.")
        strip_spine(self.memory.db)
        self.memory.db.execute("PRAGMA user_version=44")
        self.memory.close()

        started = _user_version_on_disk(self.db_path)
        self.assertLess(
            started, SCHEMA_VERSION,
            "PRECONDITION: the store must START below current, or "
            "'it ended current' says only that nothing needed doing",
        )
        self.memory = Memory(self.db_path)
        self.assertEqual(
            int(self.memory.db.execute("PRAGMA user_version").fetchone()[0]),
            SCHEMA_VERSION,
            "migration did not carry the store to the current schema",
        )
        kinds = [
            str(row[0]) for row in self.memory.db.execute(
                "SELECT kind FROM memory_spine_events ORDER BY id"
            )
        ]
        self.assertEqual(kinds.count("claim.imported"), 4)
        self.assertIn("projection.rebuilt", kinds)
        self.assertEqual(
            self._count("SELECT COUNT(*) FROM memory_graph_edges"), 4
        )
        verification = self.memory.verify_spine()
        self.assertTrue(verification["ok"], verification["problems"])
        self.assertTrue(self.memory.verify_graph()["ok"])

    def test_the_below_46_refusal_over_an_authentic_head_is_unchanged(self) -> None:
        """Slice 1's laundering refusal still fires with the graph in place."""
        conversation = self.memory.new_conversation(project_id=1)
        self._seed_chain(conversation)
        self.memory.db.execute("PRAGMA user_version=45")
        self.memory.close()
        with self.assertRaises(RuntimeError):
            Memory(self.db_path)
        # Restore a usable store for tearDown.
        raw = sqlite3.connect(str(self.db_path))
        raw.execute("PRAGMA user_version=48")
        raw.commit()
        raw.close()
        self.memory = Memory(self.db_path)


class GraphEraseTests(_GraphStoreCase):
    """Exit test 7.5: erase removes the edges, sweeps the entities it orphans,
    and leaves no label in the file."""

    def test_erase_removes_edges_sweeps_orphans_and_keeps_live_entities(self) -> None:
        conversation = self.memory.new_conversation(project_id=1)
        self._seed_chain(conversation)
        before = self._entity_keys()
        self.assertIn(("project:1", "northgate"), before)

        result = self.memory.erase_explicit_project_claim(
            conversation, 1, _erase("Fenwick", "region")
        )
        self.assertEqual(result["action"], "erased")
        self.assertTrue(result["removed_entity_ids"])
        for claim_id in result["claim_ids"]:
            self.assertEqual(
                self._count(
                    "SELECT COUNT(*) FROM memory_graph_edges WHERE claim_id=?",
                    (int(claim_id),),
                ),
                0,
            )
        keys = self._entity_keys()
        # Northgate was only ever the value of the erased fact: swept.
        self.assertNotIn(("project:1", "northgate"), keys)
        # Fenwick still has the Harrier box's in-edge: kept.
        self.assertIn(("project:1", "fenwick"), keys)
        self.assertIn(("project:1", "kestrel relay"), keys)
        tombstone = self._events("claim.tombstoned")[-1]
        self.assertEqual(
            sorted(tombstone["removed_entity_ids"]),
            sorted(int(item) for item in result["removed_entity_ids"]),
        )
        for entity_id in tombstone["removed_entity_ids"]:
            self.assertIsNone(self.memory.db.execute(
                "SELECT 1 FROM memory_graph_entities WHERE id=?", (int(entity_id),)
            ).fetchone())
        self.assertTrue(self.memory.verify_graph()["ok"])
        self.assertTrue(self.memory.verify_spine()["ok"])

    def test_the_erased_label_is_gone_from_the_file_bytes(self) -> None:
        conversation = self.memory.new_conversation(project_id=1)
        self.memory.remember_explicit_project_claim(
            conversation, 1, _command("Harrier box", "datacenter", "Zephyrhold")
        )
        self.assertIn(("project:1", "zephyrhold"), self._entity_keys())
        self.memory.erase_explicit_project_claim(
            conversation, 1, _erase("Harrier box", "datacenter")
        )
        # The operator's own command is still in the transcript, which the
        # erase receipt discloses; deleting the conversation is the documented
        # way to remove that last copy (M2's right-to-forget rule).  What this
        # test is about is the graph: the entity label must not be the thing
        # that keeps an erased value alive in the file.
        self.memory.delete_conversation(conversation)
        self.memory.db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        self.memory.db.execute("VACUUM")
        blob = self.db_path.read_bytes()
        self.assertNotIn(b"Zephyrhold", blob)
        self.assertNotIn(b"zephyrhold", blob)

    def test_forget_keeps_the_edge_and_only_changes_its_status(self) -> None:
        conversation = self.memory.new_conversation(project_id=1)
        self.memory.remember_explicit_project_claim(
            conversation, 1, _command("Harrier box", "datacenter", "Fenwick")
        )
        self.memory.retract_explicit_project_claim(
            conversation, 1, _forget("Harrier box", "datacenter")
        )
        rows = [
            (str(row[0]), row[1] is not None) for row in self.memory.db.execute(
                "SELECT status, valid_until FROM memory_graph_edges ORDER BY claim_id"
            )
        ]
        self.assertEqual(rows, [("superseded", True)])
        self.assertIn(("project:1", "fenwick"), self._entity_keys())
        self.assertTrue(self.memory.verify_graph()["ok"])

    def test_a_supersession_moves_the_edge_status_with_the_claim(self) -> None:
        conversation = self.memory.new_conversation(project_id=1)
        self.memory.remember_explicit_project_claim(
            conversation, 1, _command("Kestrel relay", "deployed on host", "Harrier box")
        )
        self.memory.remember_explicit_project_claim(
            conversation, 1, _command("Kestrel relay", "deployed on host", "Talon box")
        )
        statuses = {
            int(row[0]): str(row[1]) for row in self.memory.db.execute(
                "SELECT claim_id, status FROM memory_graph_edges"
            )
        }
        claim_statuses = {
            int(row[0]): str(row[1]) for row in self.memory.db.execute(
                "SELECT id, status FROM memory_claims"
            )
        }
        self.assertEqual(statuses, claim_statuses)
        self.assertEqual(sorted(statuses.values()), ["active", "superseded"])
        # The superseded edge keeps both entities reachable for a temporal read.
        self.assertIn(("project:1", "harrier box"), self._entity_keys())
        self.assertIn(("project:1", "talon box"), self._entity_keys())
        self.assertTrue(self.memory.verify_graph()["ok"])


class OrdinaryMemoryEraseTests(_GraphStoreCase):
    """Exit test 7.11: ``Erase memory #N`` at the store boundary."""

    def _seed_verified(self, content: str) -> int:
        self.memory.remember_verified(
            content, source="operator", origin="explicit_operator_memory"
        )
        return int(self.memory.db.execute(
            "SELECT id FROM memories WHERE content=? ORDER BY id DESC LIMIT 1",
            (content,),
        ).fetchone()[0])

    def test_the_dependent_table_list_is_derived_and_matches_the_documented_ten(self) -> None:
        self.assertEqual(
            self.memory._memory_dependent_tables(),
            DOCUMENTED_MEMORY_DEPENDENT_TABLES,
        )
        # It is genuinely derived: a new table with a memory_id column joins it.
        self.memory.db.execute(
            "CREATE TABLE memory_extra_probe(memory_id INTEGER NOT NULL, note TEXT)"
        )
        self.assertIn("memory_extra_probe", self.memory._memory_dependent_tables())
        self.memory.db.execute("DROP TABLE memory_extra_probe")
        # And memory_claims is excluded on purpose (the claim_backing refusal
        # has already turned that case away).
        self.assertNotIn("memory_claims", self.memory._memory_dependent_tables())

    def test_erase_clears_every_dependent_table_and_writes_one_receipt(self) -> None:
        content = "The nightly deploy uses rsync over the Kestrel link."
        memory_id = self._seed_verified(content)
        tables = self.memory._memory_dependent_tables()
        # Two passes: planting a provenance row fires the quality triggers,
        # which clear derived rows for that memory, so the second pass puts
        # back anything the first pass's later inserts removed.
        for _pass in range(2):
            for table in tables:
                if self._count(
                    f'SELECT COUNT(*) FROM "{table}" WHERE memory_id=?', (memory_id,)
                ):
                    continue
                self._plant_dependent_row(table, memory_id)
        for table in tables:
            self.assertEqual(
                self._count(f'SELECT COUNT(*) FROM "{table}" WHERE memory_id=?',
                            (memory_id,)),
                1,
                f"{table} was not seeded",
            )
        conversation = self.memory.new_conversation(project_id=1)
        result = self.memory.erase_memory(
            conversation, memory_id, operator_prompt=f"Erase memory #{memory_id}"
        )

        self.assertEqual(result["action"], "erased")
        self.assertEqual(result["kind"], "fact")
        self.assertRegex(
            result["assistant_message"],
            rf"^Erased memory #{memory_id} \(kind: fact, created \d{{4}}-\d{{2}}-\d{{2}}\)\. "
            r"\d+ transcript cop(?:y|ies) remain until their conversations are deleted\.$",
        )
        self.assertNotIn("rsync", result["assistant_message"])
        self.assertEqual(
            self._count("SELECT COUNT(*) FROM memories WHERE id=?", (memory_id,)), 0
        )
        for table in DOCUMENTED_MEMORY_DEPENDENT_TABLES:
            self.assertEqual(
                self._count(f'SELECT COUNT(*) FROM "{table}" WHERE memory_id=?',
                            (memory_id,)),
                0,
                f"{table} kept a row pointing at an erased memory",
            )
        # The receipt counts the rows this erase deleted itself.  It can be a
        # proper subset of the ten: deleting ordinary_memory_provenance fires
        # the quality triggers, which take the assessment row with it.  What
        # the invariant asserts is the loop above — none of the ten keeps a
        # row pointing at the erased memory.
        self.assertLessEqual(
            set(result["dependent_rows_deleted"]),
            set(DOCUMENTED_MEMORY_DEPENDENT_TABLES),
        )
        self.assertGreaterEqual(len(result["dependent_rows_deleted"]), 9)
        deleted = self._events("memory.deleted")[-1]
        self.assertEqual(deleted["ids"], [memory_id])
        self.assertEqual(deleted["reason"], "explicit operator memory erasure")
        self.assertEqual(deleted["kind"], "fact")
        self.assertEqual(
            deleted["transcript_copies"], result["transcript_copies"]
        )
        self.assertEqual(len(deleted["content_digests"]), 1)
        self.assertNotIn("rsync", json.dumps(deleted))
        # The digest is keyed: without the sidecar it cannot be confirmed.
        self.assertEqual(
            deleted["content_digests"][0],
            memory_spine.content_digest(self.memory._spine_key, content),
        )
        # FTS no longer answers for the erased row.
        self.assertEqual(self.memory.search("rsync"), [])
        self.assertTrue(self.memory.verify_spine()["ok"])
        self.assertTrue(self.memory.rebuild_memory_projection()["ok"])

    def test_erase_counts_the_transcript_copies_it_cannot_remove(self) -> None:
        content = "The nightly deploy uses rsync over the Kestrel link."
        conversation = self.memory.new_conversation(project_id=1)
        self.memory.add_message(conversation, "user", f"Remember: {content}")
        memory_id = self._seed_verified(content)
        result = self.memory.erase_memory(conversation, memory_id)
        self.assertGreaterEqual(result["transcript_copies"], 1)
        self.assertIn(
            f"{result['transcript_copies']} transcript cop",
            result["assistant_message"],
        )

    def test_the_three_refusals_change_nothing(self) -> None:
        conversation = self.memory.new_conversation(project_id=1)
        before = self._count("SELECT COUNT(*) FROM memories")

        missing = self.memory.erase_memory(conversation, 987_654)
        self.assertEqual(missing["action"], "missing")
        self.assertEqual(
            missing["assistant_message"],
            "No memory #987654 exists; nothing changed.",
        )

        self.memory.remember_explicit_project_claim(
            conversation, 1, _command("Kestrel relay", "listen port", "9090")
        )
        claim_memory_id = int(self.memory.db.execute(
            "SELECT memory_id FROM memory_claims ORDER BY id DESC LIMIT 1"
        ).fetchone()[0])
        backing = self.memory.erase_memory(conversation, claim_memory_id)
        self.assertEqual(backing["action"], "claim_backing")
        self.assertEqual(
            backing["assistant_message"],
            f"Memory #{claim_memory_id} backs a project fact; use "
            "Erase this project fact: {\u2026} (see /facts) instead.",
        )
        self.assertEqual(
            self._count("SELECT COUNT(*) FROM memories WHERE id=?",
                        (claim_memory_id,)),
            1,
        )
        self.assertEqual(self._count("SELECT COUNT(*) FROM memory_claims"), 1)

        vault_id = self._seed_verified("A note mirrored from the vault.")
        self.memory.db.execute(
            "UPDATE ordinary_memory_provenance SET origin='verified_vault_note' "
            "WHERE memory_id=?",
            (vault_id,),
        )
        note = self.memory.erase_memory(conversation, vault_id)
        self.assertEqual(note["action"], "vault_note")
        self.assertEqual(
            note["assistant_message"],
            f"Memory #{vault_id} mirrors a vault note; delete the note in the "
            "vault and reindex.",
        )
        self.assertEqual(
            self._count("SELECT COUNT(*) FROM memories WHERE id=?", (vault_id,)), 1
        )
        self.assertGreaterEqual(
            self._count("SELECT COUNT(*) FROM memories"), before
        )
        self.assertEqual(self._events("memory.deleted"), [])
        self.assertTrue(self.memory.verify_spine()["ok"])

    def test_erase_without_a_conversation_writes_no_transcript_row(self) -> None:
        memory_id = self._seed_verified("The build uses ninja.")
        messages_before = self._count("SELECT COUNT(*) FROM messages")
        result = self.memory.erase_memory(None, memory_id, permission="operator:cli")
        self.assertEqual(result["action"], "erased")
        self.assertIsNone(result["assistant_message_id"])
        self.assertEqual(
            self._count("SELECT COUNT(*) FROM messages"), messages_before
        )
        self.assertEqual(
            self._count("SELECT COUNT(*) FROM memories WHERE id=?", (memory_id,)), 0
        )

    def test_an_out_of_range_or_zero_id_is_refused_before_any_write(self) -> None:
        for bad in (0, -1, 10**19, True):
            with self.assertRaises(ValueError):
                self.memory.erase_memory(None, bad)
        with self.assertRaises(ValueError):
            self.memory.erase_memory(10**19, 1)

    def test_list_memories_with_ids_names_the_row_the_verb_erases(self) -> None:
        memory_id = self._seed_verified("The build uses ninja.")
        listing = self.memory.list_memories(with_ids=True)
        self.assertEqual(listing[0]["id"], memory_id)
        self.assertEqual(listing[0]["origin"], "explicit_operator_memory")
        self.assertEqual(int(listing[0]["eligible"]), 1)
        # The claim backing row is not an ordinary memory and never appears.
        conversation = self.memory.new_conversation(project_id=1)
        self.memory.remember_explicit_project_claim(
            conversation, 1, _command("Kestrel relay", "listen port", "9090")
        )
        self.assertNotIn(
            "claim", {str(item["kind"]) for item in self.memory.list_memories(with_ids=True)}
        )
        # Without the flag the shape is exactly what it always was.
        self.assertEqual(
            sorted(self.memory.list_memories()[0]),
            ["content", "created_at", "kind", "source"],
        )


class BackingContentCollisionTests(_GraphStoreCase):
    """Exit test 7.13 (review R1): two claim keys whose backing content
    renders identically both store, both stay recallable, and the rebuild
    reproduces both rows byte for byte."""

    def _backing(self) -> list[tuple[str, str, str]]:
        return [
            (str(row[0]), str(row[1]), str(row[2]))
            for row in self.memory.db.execute(
                """SELECT c.subject, c.predicate, m.content
                   FROM memory_claims AS c JOIN memories AS m ON m.id=c.memory_id
                   WHERE c.scope='global' ORDER BY c.id"""
            )
        ]

    def _store_the_colliding_pair(self) -> tuple[int, int]:
        first = self.memory.remember_claim(
            "Kestrel relay", "port", "9090", source="fixture", authority="operator"
        )
        second = self.memory.remember_claim(
            "Kestrel", "relay port", "9090", source="fixture", authority="operator"
        )
        return int(first), int(second)

    def test_both_claims_store_and_both_stay_recallable(self) -> None:
        first, second = self._store_the_colliding_pair()
        self.assertNotEqual(first, second)
        backing = self._backing()
        self.assertEqual(len(backing), 2)
        self.assertEqual(backing[0][2], "Kestrel relay port: 9090")
        self.assertTrue(
            backing[1][2].startswith("Kestrel relay port: 9090 [jarvis claim "),
            backing[1][2],
        )
        self.assertNotEqual(backing[0][2], backing[1][2])
        # Distinct backing rows: the UNIQUE(memory_id) crash is gone.
        self.assertEqual(
            self._count("SELECT COUNT(DISTINCT memory_id) FROM memory_claims"), 2
        )

        # Review R1: the payload-only variant made the second claim fail the
        # canonical-content equality in _claim_recall_material_eligible and
        # current_claims answered 0 rows with mode corrupt-strongest.
        rows = [
            dict(row) for row in self.memory.db.execute(
                """SELECT c.id AS claim_id, c.memory_id, c.scope, c.claim_key,
                          c.created_at, c.updated_at, c.subject, c.predicate,
                          c.value, c.value_sha256, c.source, c.authority,
                          c.confidence, c.status
                   FROM memory_claims AS c ORDER BY c.id"""
            )
        ]
        self.assertEqual(
            self.memory._claim_rows_recall_eligible(rows), {first, second}
        )
        recalled = self.memory.current_claims("Kestrel relay port")
        report = self.memory.claim_recall_report()
        self.assertNotEqual(report["mode"], "corrupt-strongest")
        self.assertFalse(report["abstained"], report)
        self.assertTrue(recalled)
        self.assertEqual(str(recalled[0]["value"]), "9090")

    def test_the_keyed_variant_survives_the_8000_character_truncation(self) -> None:
        subject = "Kestrel " * 60          # 480 characters
        value = "v" * 4_000
        first = self.memory.remember_claim(
            subject.strip(), "manifest", value,
            source="fixture", authority="operator",
        )
        # A second key whose canonical content renders identically: the same
        # words, split differently between subject and predicate.
        second = self.memory.remember_claim(
            subject.strip()[: -len(" Kestrel")], "Kestrel manifest", value,
            source="fixture", authority="operator",
        )
        contents = {
            int(row[0]): str(row[1]) for row in self.memory.db.execute(
                """SELECT c.id, m.content FROM memory_claims AS c
                   JOIN memories AS m ON m.id=c.memory_id WHERE c.scope='global'"""
            )
        }
        self.assertEqual(len(contents), 2)
        for claim_id in (first, second):
            row = self.memory.db.execute(
                """SELECT subject, predicate, value, scope, created_at, claim_key
                   FROM memory_claims WHERE id=?""",
                (claim_id,),
            ).fetchone()
            variants = backing_content_variants(*[str(item) for item in row])
            self.assertIn(contents[claim_id], variants)
            self.assertLessEqual(len(contents[claim_id]), 8_000)
        keyed = contents[second]
        self.assertTrue(keyed.endswith("]"), keyed[-40:])
        self.assertIn(" [jarvis claim ", keyed[-40:])
        # Both rows still pass recall material eligibility at the boundary.
        rows = [
            dict(row) for row in self.memory.db.execute(
                """SELECT c.id AS claim_id, c.memory_id, c.scope, c.claim_key,
                          c.created_at, c.updated_at, c.subject, c.predicate,
                          c.value, c.value_sha256, c.source, c.authority,
                          c.confidence, c.status
                   FROM memory_claims AS c ORDER BY c.id"""
            )
        ]
        self.assertEqual(
            self.memory._claim_rows_recall_eligible(rows), {first, second}
        )

    def test_a_third_colliding_key_gets_its_own_suffix(self) -> None:
        """Design 9.5.10: the second and third both take a suffix and must not
        collide with each other — ``claim_key[:16]`` differs."""
        self._store_the_colliding_pair()
        self.memory.remember_claim(
            "Kestrel relay port", "is", "9090",
            source="fixture", authority="operator",
        )
        self.memory.remember_claim(
            "Kestrel relay", "port is", "9090",
            source="fixture", authority="operator",
        )
        contents = [content for _subject, _predicate, content in self._backing()]
        self.assertEqual(len(contents), 4)
        self.assertEqual(len(contents), len(set(contents)))
        self.assertEqual(
            self._count("SELECT COUNT(DISTINCT memory_id) FROM memory_claims"), 4
        )

    def test_erasing_the_first_key_lets_the_next_write_be_canonical_again(self) -> None:
        self._store_the_colliding_pair()
        # Remove the canonical row out of band, as an erase of the first key
        # would: the next colliding write derives "canonical" from the live
        # rows, never from a payload (design 9.5.10).
        self.memory.db.execute("PRAGMA foreign_keys=OFF")
        self.memory.db.execute(
            "DELETE FROM memory_graph_edges WHERE claim_id IN "
            "(SELECT id FROM memory_claims WHERE subject='Kestrel relay')"
        )
        memory_id = int(self.memory.db.execute(
            "SELECT memory_id FROM memory_claims WHERE subject='Kestrel relay'"
        ).fetchone()[0])
        self.memory.db.execute(
            "DELETE FROM memory_claims WHERE subject='Kestrel relay'"
        )
        self.memory.db.execute("DELETE FROM memories WHERE id=?", (memory_id,))
        self.memory.db.execute("PRAGMA foreign_keys=ON")
        self.memory.remember_claim(
            "Kestrel relay", "port", "9090", source="fixture", authority="operator"
        )
        contents = [content for _subject, _predicate, content in self._backing()]
        self.assertIn("Kestrel relay port: 9090", contents)
        self.assertEqual(len(contents), len(set(contents)))


class GraphReadPathTests(_GraphStoreCase):
    """The 1.1 verdicts the one-hop bridge could not answer, plus the temporal
    and as-of modes of 3.2."""

    def setUp(self) -> None:
        super().setUp()
        self.conversation = self.memory.new_conversation(project_id=1)
        self._seed_chain(self.conversation)

    def _chains(self, question: str, subjects: list[str], **kwargs) -> dict:
        return self.memory.graph_chains(
            question, project_id=1, subjects=subjects, seed_claims=[], **kwargs
        )

    def _triples(self, result: dict) -> list[tuple[str, str, str]]:
        return [
            (str(row["subject"]), str(row["predicate"]), str(row["value"]))
            for row in result["rows"]
        ]

    def test_the_forward_two_hop_question_answers(self) -> None:
        result = self._chains(
            "Which datacenter hosts the Kestrel relay?", ["Kestrel relay"]
        )
        self.assertEqual(result["report"]["mode"], "complete")
        self.assertIn(
            ("Harrier box", "datacenter", "Fenwick"), self._triples(result)
        )

    def test_the_reversed_triple_answers_in_both_directions(self) -> None:
        """1.1's "wrong direction" and "reversed-triple miss" rows."""
        runs = self._chains("What runs on the Harrier box?", ["Harrier box"])
        self.assertEqual(runs["report"]["mode"], "complete")
        self.assertIn(
            ("Kestrel relay", "deployed on host", "Harrier box"),
            self._triples(runs),
        )
        fenwick = self._chains("What runs in Fenwick?", ["Fenwick"])
        self.assertEqual(fenwick["report"]["mode"], "complete")
        self.assertIn(
            ("Harrier box", "datacenter", "Fenwick"), self._triples(fenwick)
        )

    def test_the_three_hop_question_reaches_the_region(self) -> None:
        result = self._chains(
            "Which region is the Kestrel relay in?", ["Kestrel relay"]
        )
        self.assertEqual(result["report"]["mode"], "complete")
        self.assertEqual(
            self._triples(result),
            [
                ("Kestrel relay", "deployed on host", "Harrier box"),
                ("Harrier box", "datacenter", "Fenwick"),
                ("Fenwick", "region", "Northgate"),
            ],
        )
        self.assertEqual([int(row["hop"]) for row in result["rows"]], [1, 2, 3])
        self.assertEqual({int(row["chain"]) for row in result["rows"]}, {1})
        self.assertIsNone(result["rows"][0].get("bridge_from"))
        self.assertEqual(
            result["rows"][1]["bridge_from"], "Kestrel relay / deployed on host"
        )
        self.assertEqual(
            result["rows"][2]["bridge_from"], "Harrier box / datacenter"
        )

    def test_a_stored_value_is_never_a_false_abstention(self) -> None:
        """1.1's Northgate row: a value with reverse hops behind it answers."""
        result = self._chains(
            "Which relays are in the Northgate region?", ["Northgate"]
        )
        self.assertEqual(result["report"]["mode"], "complete")
        self.assertTrue(result["rows"])

    def test_a_name_the_store_never_saw_is_no_start(self) -> None:
        result = self._chains("Where is the Merlin relay?", ["Merlin relay"])
        self.assertEqual(result["report"]["mode"], "no-start")
        self.assertEqual(result["rows"], [])
        self.assertTrue(result["report"]["abstained"])

    def test_a_superseded_hop_answers_only_in_temporal_mode(self) -> None:
        self.memory.remember_explicit_project_claim(
            self.conversation, 1,
            _command("Kestrel relay", "deployed on host", "Talon box"),
        )
        present = self._chains(
            "Which datacenter hosts the Kestrel relay?", ["Kestrel relay"]
        )
        self.assertEqual(
            [row for row in present["rows"] if row.get("status") == "superseded"],
            [],
        )
        past = self._chains(
            "Which datacenter used to host the Kestrel relay?",
            ["Kestrel relay"], temporal=True,
        )
        self.assertEqual(past["report"]["mode"], "complete")
        self.assertIn(
            ("Kestrel relay", "deployed on host", "Harrier box"),
            self._triples(past),
        )
        self.assertIn(
            ("Harrier box", "datacenter", "Fenwick"), self._triples(past)
        )

    def test_a_seed_claim_becomes_hop_one_of_the_chain_it_started(self) -> None:
        """The main lane's row is the first hop, not a silent premise.

        A chain that starts from a seed claim's **value** would otherwise be
        presented from the middle: the operator asked about the Kestrel relay
        and would be shown a fact about the Harrier box with nothing saying
        why the two are connected.
        """
        self.memory.remember_explicit_project_claim(
            self.conversation, 1,
            _command("Osprey relay", "deployed on host", "Talon box"),
        )
        self.memory.remember_explicit_project_claim(
            self.conversation, 1,
            _command("Talon box", "datacenter", "Moss Hollow"),
        )
        question = "Which datacenter hosts the Kestrel relay?"
        seeds = self.memory.current_claims(question, project_id=1)
        self.assertTrue(seeds)
        # The store side of the contract: a seed row carries an integer
        # claim_id, which is how the walk finds the hop.
        self.assertIsInstance(seeds[0]["claim_id"], int)
        result = self.memory.graph_chains(
            question, project_id=1, subjects=["Kestrel relay"],
            seed_claims=seeds[:4],
            lane_mode=self.memory.claim_recall_report()["mode"],
        )
        self.assertEqual(result["report"]["mode"], "complete")
        # The answer's ordered prefix, not the whole row list: since 10.3
        # item 3 a walk may also emit its continuation (here `Fenwick /
        # region / Northgate`), which is a further fact about the same walk
        # and not a competing chain.  What this test is about is that the
        # named subject's answer is present, in order, from its own hop 1.
        triples = self._triples(result)
        self.assertEqual(
            triples[:2],
            [
                ("Kestrel relay", "deployed on host", "Harrier box"),
                ("Harrier box", "datacenter", "Fenwick"),
            ],
            triples,
        )
        hops = [int(row["hop"]) for row in result["rows"]]
        self.assertEqual(hops[:2], [1, 2])
        # Every row is one walk, numbered in hop order: a continuation extends
        # it, so it can never renumber or reorder the answer above.
        self.assertEqual({int(row["chain"]) for row in result["rows"]}, {1})
        self.assertEqual(hops, sorted(hops))
        self.assertEqual(
            result["rows"][1]["bridge_from"], "Kestrel relay / deployed on host"
        )
        # The other relay's chain is not swept in with it.  This is the
        # assertion that actually catches the seed displacing the answer, and
        # it is unaffected by the continuation.
        body = json.dumps(result)
        self.assertNotIn("Moss Hollow", body)
        self.assertNotIn("Talon box", body)

    def test_every_chain_row_names_the_claim_it_came_from(self) -> None:
        result = self._chains(
            "Which region is the Kestrel relay in?", ["Kestrel relay"]
        )
        live = {
            int(row[0]): (str(row[1]), str(row[2]), str(row[3]))
            for row in self.memory.db.execute(
                "SELECT id, subject, predicate, value FROM memory_claims"
            )
        }
        self.assertTrue(result["rows"])
        for row in result["rows"]:
            claim_id = row["claim_id"]
            self.assertIsInstance(claim_id, int)
            self.assertEqual(
                live[int(claim_id)],
                (str(row["subject"]), str(row["predicate"]), str(row["value"])),
            )

    def test_a_forgotten_key_marks_its_chain_row_retracted(self) -> None:
        """Design 3.2: Forget leaves history and nothing current, and the cue
        has to say so or the answer reads as merely out of date."""
        self.memory.retract_explicit_project_claim(
            self.conversation, 1, _forget("Kestrel relay", "deployed on host")
        )
        result = self._chains(
            "Which datacenter used to host the Kestrel relay?",
            ["Kestrel relay"], temporal=True,
        )
        self.assertEqual(result["report"]["mode"], "complete")
        retired = [
            row for row in result["rows"]
            if str(row.get("status") or "") == "superseded"
        ]
        self.assertTrue(retired)
        self.assertTrue(all(row.get("retracted") for row in retired), retired)
        # A hop whose key still has a current value is superseded, not
        # retracted, so the two are never confused.
        self.memory.remember_explicit_project_claim(
            self.conversation, 1,
            _command("Harrier box", "datacenter", "Moss Hollow"),
        )
        again = self._chains(
            "Which datacenter used to host the Harrier box?",
            ["Harrier box"], temporal=True,
        )
        stale = [
            row for row in again["rows"]
            if str(row.get("status") or "") == "superseded"
            and str(row.get("value") or "") == "Fenwick"
        ]
        self.assertTrue(stale)
        self.assertFalse(any(row.get("retracted") for row in stale), stale)

    def test_the_query_screens_are_the_claims_lane_s(self) -> None:
        with self.assertRaises(ValueError):
            self.memory.graph_chains("x" * 5_001, project_id=1)
        with self.assertRaises(ValueError):
            self.memory.graph_chains(
                "the key is sk-abcdefghijklmnopqrstuvwxyz012345", project_id=1
            )
        screened = self.memory.graph_chains(
            "what did alice@example.com say", project_id=1,
            subjects=["Kestrel relay"],
        )
        self.assertEqual(screened["report"]["mode"], "screened")
        self.assertEqual(screened["rows"], [])

    def test_a_lane_that_abstained_for_a_security_reason_silences_the_graph(self) -> None:
        # Design 10.7 item 5: a silenced call reports ``screened``, except
        # ``project-unavailable``, which reports itself -- it is in the closed
        # mode set and tells an operator something a screen does not.
        for lane, expected in (
            ("screened", "screened"),
            ("corrupt-strongest", "screened"),
            ("error", "screened"),
            ("project-unavailable", "project-unavailable"),
        ):
            result = self._chains(
                "Which datacenter hosts the Kestrel relay?", ["Kestrel relay"],
                lane_mode=lane,
            )
            self.assertEqual(result["report"]["mode"], expected, lane)
            self.assertEqual(result["rows"], [], lane)
            self.assertTrue(result["report"]["abstained"], lane)

    def test_a_locked_or_unreadable_database_degrades_to_error(self) -> None:
        """Design 5.1: the channel never turns a busy store into an exception
        for the turn; it degrades to no rows with mode ``error``."""
        with patch.object(
            memory_graph, "graph_walk",
            side_effect=sqlite3.OperationalError("database is locked"),
        ):
            result = self._chains(
                "Which region is the Kestrel relay in?", ["Kestrel relay"]
            )
        self.assertEqual(result["report"]["mode"], "error")
        self.assertEqual(result["rows"], [])
        self.assertTrue(result["report"]["abstained"])
        self.assertEqual(self.memory.graph_recall_report()["mode"], "error")

    def test_a_store_without_a_graph_answers_error_rather_than_raising(self) -> None:
        self.memory._graph_ready = False
        try:
            result = self._chains(
                "Which region is the Kestrel relay in?", ["Kestrel relay"]
            )
        finally:
            self.memory._graph_ready = True
        self.assertEqual(result["report"]["mode"], "error")
        self.assertEqual(result["rows"], [])

    def test_graph_recall_report_mirrors_the_last_read(self) -> None:
        result = self._chains(
            "Which region is the Kestrel relay in?", ["Kestrel relay"]
        )
        self.assertEqual(self.memory.graph_recall_report(), result["report"])
        self.memory.graph_recall_report()["mode"] = "tampered"
        self.assertEqual(self.memory.graph_recall_report()["mode"], "complete")


class GraphScopeTests(_GraphStoreCase):
    """Exit test 7.6: a chain never crosses a project boundary, a project row
    shadows a global one, and a disabled project abstains."""

    def setUp(self) -> None:
        super().setUp()
        self.project_two = self.memory.add_project("Second", "@projects/second")
        self.conversation_one = self.memory.new_conversation(project_id=1)
        self.conversation_two = self.memory.new_conversation(
            project_id=self.project_two
        )

    def _values(self, project_id: int, subjects: list[str]) -> list[str]:
        result = self.memory.graph_chains(
            "Which datacenter is it in?", project_id=project_id,
            subjects=subjects, seed_claims=[],
        )
        return [str(row["value"]) for row in result["rows"]]

    def test_another_project_s_fact_is_never_reached(self) -> None:
        self.memory.remember_explicit_project_claim(
            self.conversation_two, self.project_two,
            _command("Harrier box", "datacenter", "Fenwick"),
        )
        self.assertEqual(self._values(1, ["Harrier box"]), [])
        self.assertEqual(
            self._values(self.project_two, ["Harrier box"]), ["Fenwick"]
        )

    def test_a_project_row_shadows_the_global_row_of_the_same_key(self) -> None:
        self.memory.remember_claim(
            "Harrier box", "datacenter", "Old Fenwick",
            source="fixture", authority="verified",
        )
        self.assertEqual(self._values(1, ["Harrier box"]), ["Old Fenwick"])
        self.memory.remember_explicit_project_claim(
            self.conversation_one, 1,
            _command("Harrier box", "datacenter", "New Fenwick"),
        )
        self.assertEqual(self._values(1, ["Harrier box"]), ["New Fenwick"])
        # The global edge is shadowed, not deleted: a project with no row of
        # its own still sees it.
        self.assertEqual(
            self._count(
                "SELECT COUNT(*) FROM memory_graph_edges WHERE scope='global'"
            ),
            1,
        )
        self.assertEqual(
            self._values(self.project_two, ["Harrier box"]), ["Old Fenwick"]
        )

    def test_a_disabled_project_abstains(self) -> None:
        self.memory.remember_explicit_project_claim(
            self.conversation_one, 1,
            _command("Harrier box", "datacenter", "Fenwick"),
        )
        self.memory.db.execute("UPDATE agent_projects SET enabled=0 WHERE id=1")
        result = self.memory.graph_chains(
            "Which datacenter is it in?", project_id=1,
            subjects=["Harrier box"], seed_claims=[],
        )
        self.assertEqual(result["report"]["mode"], "project-unavailable")
        self.assertEqual(result["rows"], [])


class GraphScreenTests(_GraphStoreCase):
    """Exit test 7.7: no secret or private identifier becomes a node or a cue
    row - subjects included (review R6)."""

    def setUp(self) -> None:
        super().setUp()
        self.conversation = self.memory.new_conversation(project_id=1)

    def test_a_private_value_is_a_literal_and_never_a_node(self) -> None:
        self.memory.remember_explicit_project_claim(
            self.conversation, 1,
            _command("Harrier box", "management address", "10.0.0.7"),
        )
        self.assertEqual(
            [str(row[0]) for row in self.memory.db.execute(
                "SELECT value_kind FROM memory_graph_edges"
            )],
            ["literal"],
        )
        self.assertNotIn(("project:1", "10.0.0.7"), self._entity_keys())
        result = self.memory.graph_chains(
            "What is the management address of the Harrier box?",
            project_id=1, subjects=["Harrier box"], seed_claims=[],
        )
        self.assertEqual(result["rows"], [])
        self.assertEqual(result["report"]["mode"], "screened-rows")
        self.assertEqual(result["report"]["excluded_by_screen"], 1)
        self.assertNotIn("10.0.0.7", json.dumps(result))

    def test_a_private_or_over_long_subject_is_excluded_from_the_projection(self) -> None:
        self.memory.remember_claim(
            "10.0.0.7", "hosts", "Kestrel relay",
            source="fixture", authority="verified",
        )
        self.memory.remember_claim(
            "alice@example.com", "owns", "Kestrel relay",
            source="fixture", authority="verified",
        )
        self.memory.remember_claim(
            "Kestrel relay " * 8, "owner", "Dana",
            source="fixture", authority="verified",
        )
        self.assertEqual(self._count("SELECT COUNT(*) FROM memory_claims"), 3)
        self.assertEqual(self._count("SELECT COUNT(*) FROM memory_graph_edges"), 0)
        self.assertEqual(self._count("SELECT COUNT(*) FROM memory_graph_entities"), 0)
        verification = self.memory.verify_graph()
        self.assertTrue(verification["ok"], verification["problems"][:5])
        self.assertEqual(verification["excluded"]["subject_private"], 2)
        self.assertEqual(verification["excluded"]["subject_too_long"], 1)

    def test_a_credential_never_reaches_the_graph_or_the_block(self) -> None:
        """Three gates, and the executed truth about each.

        The governed write gate refuses a credential outright; the global
        claim API redacts it before it is stored (so the design's "the global
        API accepts them today" is not true for a ``SECRET_VALUE`` shape); and
        a credential planted straight onto a claim row out of band is still
        dropped by the per-row screen of 5.5, which is the gate that matters.
        """
        token = "sk-" + "a" * 32
        with self.assertRaises(GovernedMemoryCommandError):
            self.memory.remember_explicit_project_claim(
                self.conversation, 1, _command("Harrier box", "deploy note", token)
            )
        self.memory.remember_claim(
            "Harrier box", "deploy note", f"token {token}",
            source="fixture", authority="verified",
        )
        stored = str(self.memory.db.execute(
            "SELECT value FROM memory_claims"
        ).fetchone()[0])
        self.assertNotIn(token, stored)

        # Plant the raw credential on the claim row, leaving its edge alone.
        self.memory.db.execute(
            "UPDATE memory_claims SET value=? WHERE id=1", (f"token {token}",)
        )
        result = self.memory.graph_chains(
            "What is the deploy note for the Harrier box?",
            project_id=1, subjects=["Harrier box"], seed_claims=[],
        )
        self.assertEqual(result["rows"], [])
        self.assertNotIn(token, json.dumps(result))
        # Here the recall-material check catches the edit first (the value no
        # longer matches its own digest and backing row), so the screen never
        # sees it.  The screen is the gate that fires for a value the store
        # accepted honestly, which
        # ``test_a_private_value_is_a_literal_and_never_a_node`` asserts with
        # ``excluded_by_screen``.

    def test_a_subject_projected_before_the_screen_widened_is_dropped_per_row(self) -> None:
        """Design 5.5: the per-row screen is the last gate, because a chain
        row's subject can arrive from a row projected by migration 48."""
        self.memory.remember_explicit_project_claim(
            self.conversation, 1,
            _command("Kestrel relay", "deployed on host", "Harrier box"),
        )
        claim_id = int(self.memory.db.execute(
            "SELECT id FROM memory_claims ORDER BY id LIMIT 1"
        ).fetchone()[0])
        # A tamper by construction: the subject becomes a private identifier
        # while its edge stays, which is the shape a store projected under the
        # old screen would have.
        self.memory.db.execute(
            "UPDATE memory_claims SET subject='10.0.0.7', claim_key=? WHERE id=?",
            (self.memory._claim_identity("10.0.0.7", "deployed on host"), claim_id),
        )
        result = self.memory.graph_chains(
            "What runs on the Harrier box?", project_id=1,
            subjects=["Harrier box"], seed_claims=[],
        )
        self.assertNotIn("10.0.0.7", json.dumps(result))

    def test_an_identifier_after_character_512_is_screened(self) -> None:
        value = ("filler " * 90) + "call the operator on 415 555 0199 today"
        self.assertGreater(len(value), 512)
        # A governed value is capped at 600 characters, so this length only
        # reaches the store through the global claim API (4,000 characters).
        self.memory.remember_claim(
            "Harrier box", "runbook", value, source="fixture", authority="verified"
        )
        result = self.memory.graph_chains(
            "What is the runbook for the Harrier box?", project_id=1,
            subjects=["Harrier box"], seed_claims=[],
        )
        self.assertEqual(result["rows"], [])
        self.assertNotIn("415 555 0199", json.dumps(result))


class GraphIncompletenessTests(_GraphStoreCase):
    """Exit test 7.8 (review R5, recommendation 2): a bounded result is never
    presented as complete."""

    def setUp(self) -> None:
        super().setUp()
        self.conversation = self.memory.new_conversation(project_id=1)

    def _hub(self, size: int, datacenter: str = "Fenwick", prefix: str = "Box") -> None:
        for index in range(size):
            self.memory.remember_explicit_project_claim(
                self.conversation, 1,
                _command(f"{prefix}{index:02d}", "datacenter", datacenter),
            )

    def test_an_inner_hub_overflows_rather_than_listing_part_of_itself(self) -> None:
        self._hub(17)
        result = self.memory.graph_chains(
            "What is in Fenwick?", project_id=1, subjects=["Fenwick"],
            seed_claims=[],
        )
        self.assertEqual(result["report"]["mode"], "overflow")
        self.assertEqual(result["rows"], [])
        self.assertEqual(len(result["overflow"]), 1)
        entry = result["overflow"][0]
        self.assertEqual(entry["status"], "overflow")
        self.assertEqual(entry["subject"], "Fenwick")
        self.assertEqual(int(entry["hop"]), 1)
        self.assertIn("Ask about one by name", str(entry["note"]))

    def test_naming_the_predicate_widens_the_cap_and_answers(self) -> None:
        self._hub(17)
        result = self.memory.graph_chains(
            "Which datacenter entries name Fenwick?", project_id=1,
            subjects=["Fenwick"], seed_claims=[],
        )
        self.assertEqual(result["report"]["mode"], "complete")
        self.assertEqual(len(result["rows"]), 8)
        # Eight of seventeen: every emitted row says so and the note carries
        # the true count (design 5.4).
        self.assertTrue(all(row.get("incomplete") for row in result["rows"]))
        self.assertEqual(len(result["overflow"]), 1)
        # The note names the hub every answer shares, not one arbitrary
        # answer, so "ask about one by name" is actionable.
        self.assertEqual(result["overflow"][0]["subject"], "Fenwick")
        self.assertIn("17 stored facts answer this", str(result["overflow"][0]["note"]))

    def test_a_terminal_hub_of_forty_answers_with_an_honest_count(self) -> None:
        """Recommendation 2: without the terminal cap of 64 the operator got
        nothing at all; with it they get the strongest rows and a true count."""
        self._hub(40)
        self.memory.remember_explicit_project_claim(
            self.conversation, 1,
            _command("Kestrel relay", "deployed on host", "Box00"),
        )
        result = self.memory.graph_chains(
            "Which boxes are in the datacenter of the Kestrel relay?",
            project_id=1, subjects=["Kestrel relay"], seed_claims=[],
        )
        self.assertEqual(result["report"]["mode"], "complete")
        self.assertEqual(len(result["rows"]), 8)
        terminals = [row for row in result["rows"] if int(row["hop"]) == 3]
        self.assertTrue(terminals)
        self.assertTrue(all(row.get("incomplete") for row in terminals))
        self.assertTrue(result["overflow"])
        self.assertIn(
            "stored facts answer this", str(result["overflow"][0]["note"])
        )

    def test_no_more_than_two_overflow_notes_are_emitted(self) -> None:
        for datacenter in ("Fenwick", "Moss Hollow", "Zephyrhold", "Northgate"):
            self._hub(17, datacenter, prefix=f"{datacenter} box")
        result = self.memory.graph_chains(
            "What is in Fenwick and Moss Hollow and Zephyrhold and Northgate?",
            project_id=1,
            subjects=["Fenwick", "Moss Hollow", "Zephyrhold", "Northgate"],
            seed_claims=[],
        )
        self.assertLessEqual(len(result["overflow"]), 2)
        self.assertGreaterEqual(int(result["report"]["overflow"]), 3)

    def test_a_deadline_that_expires_returns_what_was_screened(self) -> None:
        """Review R4: the budget is not an all-or-nothing abstention, and the
        deadline covers the screen phase, not only the traversal loop."""
        self._seed_chain(self.conversation)
        with time_budget(0.0):
            expired = self.memory.graph_chains(
                "Which region is the Kestrel relay in?", project_id=1,
                subjects=["Kestrel relay"], seed_claims=[],
            )
        self.assertEqual(expired["report"]["mode"], "budget-exceeded")
        self.assertEqual(expired["report"]["budget"], "time")
        self.assertEqual(expired["rows"], [])

        calls = {"count": 0}
        real_screen = memory_graph.screen_endpoint

        def slow_screen(text: str) -> tuple[bool, str | None]:
            calls["count"] += 1
            if calls["count"] > 2:
                time.sleep(0.04)
            return real_screen(text)

        # The real product budget, restored on purpose: this half asserts
        # that a screen phase alone can trip the one whole-call deadline.
        with time_budget(PRODUCT_TIME_BUDGET_MS), patch(
            "jarvis.memory.screen_endpoint", slow_screen
        ):
            slow = self.memory.graph_chains(
                "Which region is the Kestrel relay in?", project_id=1,
                subjects=["Kestrel relay"], seed_claims=[],
            )
        self.assertEqual(slow["report"]["mode"], "budget-exceeded")
        self.assertEqual(slow["report"]["budget"], "time")
        self.assertTrue(
            all(row.get("incomplete") for row in slow["rows"]), slow["rows"]
        )


class GraphIdentityFloorTests(_GraphStoreCase):
    """Exit test 7.15 (review R3): exact resolution supersedes the look-alike
    floor; every non-exact resolution carries it."""

    def setUp(self) -> None:
        super().setUp()
        conversation = self.memory.new_conversation(project_id=1)
        for subject, value in (
            ("Kestrel relay", "Fenwick"),
            ("Kestrel relay 2", "Moss Hollow"),
            ("Kestrelrelay", "Zephyrhold"),
        ):
            self.memory.remember_explicit_project_claim(
                conversation, 1, _command(subject, "datacenter", value)
            )

    def _ask(self, subjects: list[str], **kwargs) -> dict:
        return self.memory.graph_chains(
            "Which datacenter hosts it?", project_id=1, subjects=subjects,
            seed_claims=[], **kwargs
        )

    def test_an_exact_spelling_answers_from_that_key_alone(self) -> None:
        result = self._ask(["Kestrel relay"])
        self.assertEqual(result["report"]["mode"], "complete")
        self.assertEqual(
            [str(row["value"]) for row in result["rows"]], ["Fenwick"]
        )
        body = json.dumps(result)
        self.assertNotIn("Moss Hollow", body)
        self.assertNotIn("Zephyrhold", body)

    def test_a_non_exact_candidate_with_a_stored_look_alike_abstains(self) -> None:
        result = self._ask(["Kestrel rely"])
        self.assertEqual(result["report"]["mode"], "identity-conflict")
        self.assertEqual(result["rows"], [])

    def test_two_exactly_resolved_subjects_are_both_starts(self) -> None:
        result = self._ask(["Kestrel relay", "Kestrelrelay"])
        self.assertEqual(result["report"]["mode"], "complete")
        self.assertEqual(
            sorted(str(row["value"]) for row in result["rows"]),
            ["Fenwick", "Zephyrhold"],
        )

    def test_two_subjects_of_which_one_is_non_exact_abstain(self) -> None:
        result = self._ask(["Kestrel relay", "Kestrel rely"])
        self.assertEqual(result["report"]["mode"], "identity-conflict")
        self.assertEqual(result["rows"], [])

    def test_an_identity_floor_in_the_lane_leaves_exact_resolution_alone(self) -> None:
        exact = self._ask(["Kestrel relay"], lane_mode="identity-overflow")
        self.assertEqual(exact["report"]["mode"], "complete")
        self.assertEqual(
            [str(row["value"]) for row in exact["rows"]], ["Fenwick"]
        )
        # The surface gates the fixed lane-abstained clause on this flag.
        self.assertTrue(exact["report"]["lane_abstained"])
        non_exact = self._ask(["Kestrel rely"], lane_mode="identity-conflict")
        self.assertEqual(non_exact["rows"], [])
        self.assertFalse(non_exact["report"]["lane_abstained"])


class GraphTamperRebuildTests(_GraphStoreCase):
    """Exit test 7.2: an out-of-band graph tamper is reported and reconciled."""

    def setUp(self) -> None:
        super().setUp()
        self.conversation = self.memory.new_conversation(project_id=1)
        self._seed_chain(self.conversation)
        self.memory.set_preference("editor", "vim", source="user")

    def _tamper(self) -> None:
        self.memory.db.execute("PRAGMA foreign_keys=OFF")
        self.memory.db.execute(
            "UPDATE memory_graph_edges SET status='superseded' WHERE claim_id=1"
        )
        self.memory.db.execute("DELETE FROM memory_graph_edges WHERE claim_id=2")
        preference_id = int(self.memory.db.execute(
            "SELECT id FROM memory_claims WHERE scope='global' ORDER BY id LIMIT 1"
        ).fetchone()[0])
        entity_id = int(self.memory.db.execute(
            "SELECT id FROM memory_graph_entities ORDER BY id LIMIT 1"
        ).fetchone()[0])
        self.memory.db.execute(
            """INSERT INTO memory_graph_edges(
                   claim_id, scope, claim_key, src_entity_id, predicate_key,
                   dst_entity_id, value_kind, status, authority, confidence,
                   valid_from, valid_until, spine_event_id, projected_at
               ) VALUES (?, 'global', 'forged', ?, 'forged', NULL, 'literal',
                         'active', 'operator', 1.0, ?, NULL, 1, ?)""",
            (preference_id, entity_id, now_iso(), now_iso()),
        )
        # The fourth tamper of 7.2, and the one that catches a reproject whose
        # orphan sweep does not run: an entity whose key no longer matches the
        # subject its edges were projected from.
        self.memory.db.execute(
            "UPDATE memory_graph_entities SET entity_key='forged key' WHERE id=?",
            (entity_id,),
        )
        self.memory.db.execute("PRAGMA foreign_keys=ON")

    def test_a_dry_run_reports_the_tamper_and_apply_reconciles_it(self) -> None:
        self._tamper()
        dry = self.memory.rebuild_graph_projection()
        self.assertFalse(dry["ok"])
        kinds = {str(item["kind"]) for item in dry["divergences"]}
        self.assertLessEqual(
            {"field", "missing_edge", "extra_edge", "entity_key"},
            kinds,
            dry["divergences"],
        )
        self.assertTrue(dry["plan_token"])
        self.assertFalse(self.memory.verify_graph()["ok"])
        # A divergence detail names fields, never operator text (M-1 / F-1).
        detail = json.dumps(dry["divergences"])
        for text in ("Kestrel relay", "Harrier box", "Fenwick", "Northgate"):
            self.assertNotIn(text, detail)

        receipts_before = len(self._events("projection.rebuilt"))
        applied = self.memory.rebuild_graph_projection(apply=True, plan=dry)
        self.assertTrue(applied["ok"], applied)
        self.assertTrue(applied["applied"])
        self.assertIsNone(applied["refusal"])
        self.assertTrue(self.memory.rebuild_graph_projection()["ok"])
        self.assertTrue(self.memory.verify_graph()["ok"])
        receipts = self._events("projection.rebuilt")
        self.assertEqual(len(receipts), receipts_before + 1)
        self.assertEqual(receipts[-1]["projection"], "graph")
        self.assertTrue(self.memory.verify_spine()["ok"])

    def test_a_stale_plan_is_refused_and_changes_nothing(self) -> None:
        self._tamper()
        stale = self.memory.rebuild_graph_projection()
        self.memory.db.execute("PRAGMA foreign_keys=OFF")
        self.memory.db.execute("DELETE FROM memory_graph_edges WHERE claim_id=3")
        self.memory.db.execute("PRAGMA foreign_keys=ON")
        edges_before = self._edges()
        refused = self.memory.rebuild_graph_projection(apply=True, plan=stale)
        self.assertEqual(refused["refusal"], "stale_plan")
        self.assertFalse(refused["applied"])
        self.assertEqual(self._edges(), edges_before)

    def test_rebuild_claims_apply_reprojects_the_graph_in_the_same_receipt(self) -> None:
        """Design 4.5: the claim receipt records that the graph followed."""
        self.memory.db.execute("PRAGMA foreign_keys=OFF")
        self.memory.db.execute("DELETE FROM memory_graph_edges WHERE claim_id=2")
        self.memory.db.execute(
            "UPDATE memory_claims SET confidence=0.25 WHERE id=2"
        )
        self.memory.db.execute("PRAGMA foreign_keys=ON")
        dry = self.memory.rebuild_claim_projection()
        self.assertFalse(dry["ok"])
        applied = self.memory.rebuild_claim_projection(apply=True, plan=dry)
        self.assertTrue(applied["ok"], applied)
        self.assertTrue(applied["applied"])
        receipt = self._events("projection.rebuilt")[-1]
        self.assertTrue(receipt.get("graph_reprojected"))
        self.assertTrue(self.memory.verify_graph()["ok"])
        self.assertTrue(self.memory.rebuild_graph_projection()["ok"])


class GraphValueEditRepairTests(_GraphStoreCase):
    """An out-of-band edit to a claim's own value is a graph divergence too:
    the edge's destination entity is derived from that value, so repairing the
    claim projection without re-deriving the graph leaves a chain pointing at
    a name the store no longer holds."""

    def setUp(self) -> None:
        super().setUp()
        self.conversation = self.memory.new_conversation(project_id=1)
        self._seed_chain(self.conversation)

    def _edit_a_value_out_of_band(self) -> int:
        """Rewrite the chain's terminal link, ``Fenwick / region / Northgate``,
        to a new region, leaving the edge and its old destination behind.

        The terminal link is the one that matters here: ``Northgate`` is the
        only entity with no other edge, so it is the one a reproject has to
        sweep.  An inner link would leave its old destination legitimately
        alive on its own out-edge and prove nothing about the sweep.
        """
        claim_id = int(self.memory.db.execute(
            "SELECT id FROM memory_claims WHERE predicate='region'"
        ).fetchone()[0])
        self.memory.db.execute("PRAGMA foreign_keys=OFF")
        self.memory.db.execute(
            "UPDATE memory_claims SET value=?, value_sha256=? WHERE id=?",
            ("Southgate", "0" * 64, claim_id),
        )
        self.memory.db.execute("PRAGMA foreign_keys=ON")
        return claim_id

    def test_graph_rebuild_apply_repairs_a_value_edit_and_sweeps_the_orphan(self) -> None:
        claim_id = self._edit_a_value_out_of_band()
        self.assertIn(("project:1", "northgate"), self._entity_keys())

        dry = self.memory.rebuild_graph_projection()
        self.assertFalse(dry["ok"], dry)
        applied = self.memory.rebuild_graph_projection(apply=True, plan=dry)
        self.assertTrue(applied["ok"], applied)
        self.assertTrue(applied["applied"])

        clean = self.memory.rebuild_graph_projection()
        self.assertTrue(clean["ok"], clean["divergences"][:5])
        self.assertTrue(self.memory.verify_graph()["ok"])
        # The edge now points at the new name...
        destination = self.memory.db.execute(
            """SELECT destination.entity_key
               FROM memory_graph_edges AS e
               JOIN memory_graph_entities AS destination
                 ON destination.id=e.dst_entity_id
               WHERE e.claim_id=?""",
            (claim_id,),
        ).fetchone()
        self.assertEqual(str(destination[0]), "southgate")
        # ...and the name it used to point at is gone, because nothing else
        # referenced it.  A reproject that repairs edges but never sweeps
        # leaves "northgate" behind with no edge, which is the defect this
        # case exists for.
        self.assertNotIn(("project:1", "northgate"), self._entity_keys())
        self.assertEqual(
            self._count(
                """SELECT COUNT(*) FROM memory_graph_entities AS n
                   WHERE NOT EXISTS (
                       SELECT 1 FROM memory_graph_edges AS e
                       WHERE e.src_entity_id=n.id OR e.dst_entity_id=n.id)"""
            ),
            0,
        )

    def test_rebuild_claims_apply_repairs_the_graph_through_post_apply(self) -> None:
        """The same edit through the claim lane: ``rebuild-claims --apply``
        reconciles the claim row back to the spine and the post-apply hook
        re-derives the graph inside the same transaction, so the operator
        never has to know the graph existed."""
        self._edit_a_value_out_of_band()
        dry = self.memory.rebuild_claim_projection()
        self.assertFalse(dry["ok"], dry)
        applied = self.memory.rebuild_claim_projection(apply=True, plan=dry)
        self.assertTrue(applied["ok"], applied)
        self.assertTrue(applied["applied"])
        receipt = self._events("projection.rebuilt")[-1]
        self.assertTrue(receipt.get("graph_reprojected"))

        self.assertTrue(self.memory.rebuild_claim_projection()["ok"])
        graph = self.memory.rebuild_graph_projection()
        self.assertTrue(graph["ok"], graph["divergences"][:5])
        self.assertTrue(self.memory.verify_graph()["ok"])
        # The spine's value won, so the chain answers with it again.
        result = self.memory.graph_chains(
            "Which region is the Kestrel relay in?", project_id=1,
            subjects=["Kestrel relay"], seed_claims=[],
        )
        self.assertIn(
            "Northgate", {str(row["value"]) for row in result["rows"]}
        )
        self.assertNotIn(("project:1", "southgate"), self._entity_keys())


class GraphAsOfLegacyTests(_GraphStoreCase):
    """Exit test 7.16 (review R9): a row superseded in place before schema 46
    must not answer every dated question."""

    def test_a_legacy_superseded_row_is_invisible_to_as_of(self) -> None:
        conversation = self.memory.new_conversation(project_id=1)
        self.memory.remember_explicit_project_claim(
            conversation, 1, _command("Harrier box", "datacenter", "Fenwick")
        )
        # The legacy shape: superseded in place with no valid_until.  A tamper
        # by construction; no writer has produced it since schema 46.
        self.memory.db.execute(
            "UPDATE memory_claims SET status='superseded', valid_until=NULL WHERE id=1"
        )
        claims_before = [
            tuple(row) for row in self.memory.db.execute(
                "SELECT * FROM memory_claims ORDER BY id"
            )
        ]
        self.memory.close()
        raw = sqlite3.connect(str(self.db_path))
        for statement in memory_graph.DROP_GRAPH_SQL:
            raw.execute(statement)
        raw.execute("PRAGMA user_version=47")
        raw.commit()
        raw.close()
        self.memory = Memory(self.db_path)

        self.assertEqual(
            [tuple(row) for row in self.memory.db.execute(
                "SELECT * FROM memory_claims ORDER BY id"
            )],
            claims_before,
            "migration 48 wrote to memory_claims",
        )
        edge = self.memory.db.execute(
            "SELECT status, valid_until FROM memory_graph_edges WHERE claim_id=1"
        ).fetchone()
        self.assertEqual(str(edge[0]), "superseded")
        self.assertIsNone(edge[1])

        known = self.memory.graph_chains(
            "Which datacenter hosted the Harrier box?", project_id=1,
            subjects=["Harrier box"], seed_claims=[],
            as_of="2020-01-01T00:00:00+00:00",
        )
        self.assertEqual(known["rows"], [])
        self.assertEqual(known["report"]["mode"], "no-answer")
        unknown = self.memory.graph_chains(
            "Which datacenter hosted the Merlin relay?", project_id=1,
            subjects=["Merlin relay"], seed_claims=[],
            as_of="2020-01-01T00:00:00+00:00",
        )
        self.assertEqual(unknown["report"]["mode"], "no-start")


class GraphLatencyTests(_GraphStoreCase):
    """Exit test 7.9, slow: the budgets at 10,000 claim keys / 20,000 edges.

    Every figure is measured **warm and cold** (design 10.7 item 7).  Warm is
    best-of-N with the recall cache populated, which is what the gate used to
    record exclusively and which understates a real turn: ``_claim_rows_
    recall_eligible`` memoises through ``RecallCache``, so it costs 0.4 ms
    warm and 8.0 ms cold.  Cold is one first call per shape on a fresh store
    object, which is what a turn pays the first time it asks about a subject.

    **The measurement trap, recorded because it cost an hour and would cost
    it again:** any stage-level timing of this path taken *outside*
    ``Memory._recall_cache.activate()`` overstates the eligibility stage by
    roughly 7 ms, because the real call runs inside that context and the probe
    does not.  The tell is arithmetic that cannot be true -- summing the
    stages to more than the whole call (14.65 ms against 9.55 ms is what
    caught it).  Check the instrument before believing the reading.

    Coverage note (10.7 item 7): this store is **wide, not hub-heavy** -- its
    largest fan-out is 64 -- so the hub regime is exercised by the separate
    few-thousand-in-edge shape below, reported and never enforced, and at
    module level by ``tests/test_memory_graph.py``.
    """

    CLAIM_KEYS = 10_000
    # The hub shape of 10.7 item 7(d): large enough to be the regime that hid
    # an ordered-``LIMIT`` sort (measured 15.4 ms at 20,000 edges before the
    # unordered probe replaced it), small enough to seed in about a second.
    HUB_IN_EDGES = 3_000
    BIRDS = (
        "Kestrel", "Osprey", "Harrier", "Talon", "Merlin", "Falcon", "Kite",
        "Buzzard", "Goshawk", "Sparrow", "Hobby", "Caracara", "Eagle", "Owl",
        "Vulture", "Condor",
    )

    def _record(self, what: str, measured: float, unit: str = "ms") -> None:
        """A figure that goes on the record without a budget attached."""
        self._budget_report.append(f"{what} {measured:.2f}{unit}")

    def _check_budget(self, measured: float, budget: float, what: str) -> str:
        """Record one 7.9 figure, and enforce it only when asked to."""
        verdict = "ok" if measured <= budget else "OVER"
        self._budget_report.append(f"{what} {measured:.2f}/{budget:.2f} {verdict}")
        if measured > budget and ENFORCE_TIMING_GATES:
            self.fail(
                f"{what}: {measured:.2f} against a budget of {budget:.2f} "
                f"({measured / budget:.1f}x)"
            )
        return verdict

    def _seed_at_scale(self) -> None:
        with self.memory._immediate_transaction():
            for index in range(self.CLAIM_KEYS):
                bird = self.BIRDS[index % len(self.BIRDS)]
                subject = f"{bird}{index} relay"
                if index % 3 == 0:
                    predicate = "deployed on host"
                    value = f"{self.BIRDS[(index + 1) % len(self.BIRDS)]}{index % 97} box"
                elif index % 3 == 1:
                    predicate, value = "datacenter", f"Fenwick{index % 53}"
                else:
                    predicate, value = "listen port", str(9000 + index % 900)
                for suffix in ("", " v2"):
                    self.memory._remember_claim_locked(
                        subject, predicate, f"{value}{suffix}",
                        source="fixture", authority="operator", confidence=1.0,
                        stamp=now_iso(), scope="project:1",
                    )

    def test_the_claim_row_load_returns_identical_rows_to_the_indexed_form(self) -> None:
        """Design 10.7 item 7: the load may change shape only where a test
        asserts identical rows against the previous form, on this store.

        The change is ``index_scope=False`` -- SQLite's ``+`` on the scope
        test -- which alters no semantics and only stops the planner choosing
        the scope index.  With the scope index driving, the plan is
        ``SEARCH c USING INDEX idx_memory_claims_scope_key (scope=?)``: it
        walks the whole scope and filters twelve rows out of 20,005.  On the
        rowid it is ``SEARCH c USING INTEGER PRIMARY KEY (rowid=?)``.

        This asserts the thing that matters (identical rows, both orders and
        both scopes) and records the plans, rather than asserting a timing
        that would make the test a second latency gate.
        """
        self.memory.new_conversation(project_id=1)
        self._seed_at_scale()
        # A few global rows too: the global-only filter must be exercised with
        # ids it can actually see, or the comparison is vacuous on both sides.
        for index in range(4):
            self.memory.remember_claim(
                f"Global relay {index}", "datacenter", f"Fenwick{index}",
                source="fixture", authority="operator",
            )
        columns = (
            "c.id AS claim_id, c.memory_id, c.scope, c.claim_key, c.created_at,"
            " c.updated_at, c.subject, c.predicate, c.value, c.value_sha256,"
            " c.source, c.authority, c.confidence, c.status, c.valid_from,"
            " c.valid_until"
        )
        for project_id, project_scope in ((None, None), (1, "project:1")):
            visible = ("global",) if project_scope is None else ("global", project_scope)
            scopes = ",".join("?" for _scope in visible)
            claim_ids = [
                int(row[0]) for row in self.memory.db.execute(
                    f"SELECT id FROM memory_claims WHERE scope IN ({scopes}) "
                    f"ORDER BY id LIMIT 24",
                    list(visible),
                )
            ]
            placeholders = ",".join("?" for _id in claim_ids)
            with self.subTest(project=project_id):
                self.assertTrue(claim_ids)
                indexed_sql, indexed_params = self.memory._claim_scope_filter(
                    visible, project_scope
                )
                rowid_sql, rowid_params = self.memory._claim_scope_filter(
                    visible, project_scope, index_scope=False
                )
                # The only textual difference is the '+'.
                self.assertEqual(rowid_sql.replace("+", "", 1), indexed_sql)
                self.assertEqual(rowid_params, indexed_params)

                previous = [
                    tuple(row) for row in self.memory.db.execute(
                        f"SELECT {columns} FROM memory_claims AS c "
                        f"WHERE {indexed_sql} AND c.id IN ({placeholders})",
                        [*indexed_params, *claim_ids],
                    )
                ]
                current = [
                    tuple(row) for row in self.memory.db.execute(
                        f"SELECT {columns} FROM memory_claims AS c "
                        f"WHERE c.id IN ({placeholders}) AND {rowid_sql}",
                        [*claim_ids, *rowid_params],
                    )
                ]
                self.assertTrue(previous)
                self.assertEqual(sorted(previous), sorted(current))

                # And the plans really do differ in the way claimed, so a
                # future planner change that silently reverts the win is
                # visible here rather than only in the latency line.
                plan = " ".join(
                    str(row[3]) for row in self.memory.db.execute(
                        f"EXPLAIN QUERY PLAN SELECT {columns} FROM memory_claims AS c "
                        f"WHERE c.id IN ({placeholders}) AND {rowid_sql}",
                        [*claim_ids, *rowid_params],
                    )
                )
                self.assertIn("INTEGER PRIMARY KEY", plan, plan)

    def test_the_budgets_hold_at_twenty_thousand_edges(self) -> None:
        self._budget_report: list[str] = []
        self.memory.new_conversation(project_id=1)
        self._seed_at_scale()
        claims = self._count("SELECT COUNT(*) FROM memory_claims")
        edges = self._count("SELECT COUNT(*) FROM memory_graph_edges")
        self.assertGreaterEqual(claims, 20_000)
        self.assertEqual(edges, claims)

        questions = [
            ("Which datacenter hosts the Kestrel0 relay?", ["Kestrel0 relay"], {}),
            ("Where is the Kestrel0 relay deployed?", ["Kestrel0 relay"], {}),
            ("What runs on the Osprey1 box?", ["Osprey1 box"], {}),
            ("What is in Fenwick3?", ["Fenwick3"], {}),
            ("Which relays are in Fenwick3?", ["Fenwick3"], {}),
            ("Which listen port does the Harrier2 relay use?", ["Harrier2 relay"], {}),
            ("Which datacenter used to host the Kestrel0 relay?",
             ["Kestrel0 relay"], {"temporal": True}),
            ("Where was the Talon3 relay deployed?", ["Talon3 relay"],
             {"temporal": True}),
            ("Which datacenter hosts the Merlin4 relay?", ["Merlin4 relay"], {}),
            ("Which region is the Falcon5 relay in?", ["Falcon5 relay"], {}),
            ("Which datacenter hosts the Kestrel0 relay and the Osprey1 relay?",
             ["Kestrel0 relay", "Osprey1 relay"], {}),
            ("Where is the Kite6 relay?", ["Kite6 relay"], {}),
        ]
        # Best of three per shape, then p95 over the shapes: the design's own
        # probes are reported best-of-three for the same reason, and this test
        # runs inside a full suite where a descheduled thread would otherwise
        # decide the gate.  Best-of measures the work, not the scheduler, and
        # still fails honestly if the real cost crosses the budget.
        samples: list[float] = []
        for question, subjects, kwargs in questions:
            shape: list[float] = []
            for _repeat in range(3):
                started = time.perf_counter()
                self.memory.graph_chains(
                    question, project_id=1, subjects=subjects, seed_claims=[],
                    **kwargs
                )
                shape.append((time.perf_counter() - started) * 1000.0)
            samples.append(min(shape))
        samples.sort()
        p95 = samples[int(len(samples) * 0.95) - 1]
        self._check_budget(p95, 25.0, "graph_chains warm p95 ms")

        # Cold: one first call per shape on a fresh store object, so nothing
        # is memoised.  A second connection to the same file is a reader; the
        # open itself is not timed, only the call.
        cold: list[float] = []
        for question, subjects, kwargs in questions:
            fresh = Memory(self.db_path)
            try:
                started = time.perf_counter()
                fresh.graph_chains(
                    question, project_id=1, subjects=subjects, seed_claims=[],
                    **kwargs
                )
                cold.append((time.perf_counter() - started) * 1000.0)
            finally:
                fresh.close()
        cold.sort()
        self._record("cold p50", cold[len(cold) // 2])
        self._record("cold max", cold[-1])
        self._check_budget(
            cold[int(len(cold) * 0.95) - 1], 25.0, "graph_chains cold p95 ms"
        )

        # The dry run runs first: the hook-off writes below deliberately
        # leave claims with no edge, which migration 48 then re-projects.
        dry_samples: list[float] = []
        for _repeat in range(2):
            started = time.perf_counter()
            dry = self.memory.rebuild_graph_projection()
            dry_samples.append(time.perf_counter() - started)
            self.assertTrue(dry["ok"], dry["divergences"][:3])
        dry_seconds = min(dry_samples)
        self._check_budget(dry_seconds, 2.0, "graph dry run s")

        hook_off: list[float] = []
        hook_on: list[float] = []
        for index in range(20):
            for hooked, into in ((False, hook_off), (True, hook_on)):
                self.memory._graph_ready = hooked
                started = time.perf_counter()
                with self.memory._immediate_transaction():
                    self.memory._remember_claim_locked(
                        f"Probe{index}{int(hooked)} relay", "datacenter",
                        f"Fenwick{index % 53}", source="fixture",
                        authority="operator", confidence=1.0, stamp=now_iso(),
                        scope="project:1",
                    )
                into.append((time.perf_counter() - started) * 1000.0)
        self.memory._graph_ready = True
        # Best of twenty each: the question is what the hook costs, not what
        # the machine was doing at the time.
        marginal = min(hook_on) - min(hook_off)
        self._check_budget(marginal, 1.0, "write hook ms")

        # Best of two, for the same reason as the read-path figure above: the
        # gate is a property of the backfill, and this test runs inside a full
        # suite where a descheduled thread would otherwise decide it.  The
        # migration is idempotent, so re-running it measures the same work.
        migration_samples: list[float] = []
        for _repeat in range(2):
            self.memory.close()
            raw = sqlite3.connect(str(self.db_path))
            for statement in memory_graph.DROP_GRAPH_SQL:
                raw.execute(statement)
            raw.execute("PRAGMA user_version=47")
            raw.commit()
            raw.close()
            started = time.perf_counter()
            self.memory = Memory(self.db_path)
            migration_samples.append(time.perf_counter() - started)
            self.assertEqual(
                self._count("SELECT COUNT(*) FROM memory_graph_edges"), edges + 40
            )
        migration_seconds = min(migration_samples)
        # The whole store open is what an operator waits for, but it also
        # carries every other migration check; the gate belongs to the
        # backfill, so both are measured and both are gated when enforced.
        self._check_budget(migration_seconds, 2.0, "migration 48 open s")

        backfill_samples: list[float] = []
        for _repeat in range(2):
            memory_graph.drop_graph_tables(self.memory.db)
            self.memory.db.execute("BEGIN IMMEDIATE")
            started = time.perf_counter()
            memory_graph.migrate_memory_graph_v48(
                self.memory.db, self.memory._spine_key, now=now_iso()
            )
            backfill_samples.append(time.perf_counter() - started)
            self.memory.db.commit()
            self.assertEqual(
                self._count("SELECT COUNT(*) FROM memory_graph_edges"), edges + 40
            )
        backfill_seconds = min(backfill_samples)
        self._check_budget(backfill_seconds, 2.0, "migration 48 backfill s")

        # 10.7 item 7(d): the hub regime, closed at store level.  Reported and
        # never enforced -- it exists so a regression in the overflow decision
        # shows up as a number here instead of hiding until a real store has a
        # hub.  The ordered form this replaced cost 15.4 ms per expanded node
        # at 20,000 in-edges against a 25 ms whole-call deadline.
        hub_conversation = self.memory.new_conversation(project_id=1)
        del hub_conversation
        with self.memory._immediate_transaction():
            for index in range(self.HUB_IN_EDGES):
                self.memory._remember_claim_locked(
                    f"Hubbox{index:05d}", "datacenter", "Hubwick",
                    source="fixture", authority="operator", confidence=1.0,
                    stamp=now_iso(), scope="project:1",
                )
        self.assertGreaterEqual(
            self._count(
                """SELECT COUNT(*) FROM memory_graph_edges AS e
                   JOIN memory_graph_entities AS n ON n.id=e.dst_entity_id
                   WHERE n.entity_key='hubwick'"""
            ),
            self.HUB_IN_EDGES,
        )
        hub_shapes = [
            ("What is in Hubwick?", ["Hubwick"]),
            ("Which datacenter entries name Hubwick?", ["Hubwick"]),
        ]
        hub_cold: list[float] = []
        hub_warm: list[float] = []
        for question, subjects in hub_shapes:
            fresh = Memory(self.db_path)
            try:
                started = time.perf_counter()
                fresh.graph_chains(
                    question, project_id=1, subjects=subjects, seed_claims=[]
                )
                hub_cold.append((time.perf_counter() - started) * 1000.0)
            finally:
                fresh.close()
            shape: list[float] = []
            for _repeat in range(3):
                started = time.perf_counter()
                self.memory.graph_chains(
                    question, project_id=1, subjects=subjects, seed_claims=[]
                )
                shape.append((time.perf_counter() - started) * 1000.0)
            hub_warm.append(min(shape))
        self._record(f"hub({self.HUB_IN_EDGES}) cold max", max(hub_cold))
        self._record(f"hub({self.HUB_IN_EDGES}) warm max", max(hub_warm))
        # The figures are the point of this test even when nothing is
        # enforced, so they are always printed.
        print(
            "[design 7.9] "
            + " | ".join(self._budget_report)
            + f" | enforced={ENFORCE_TIMING_GATES}"
        )


class RedTeamFixTests(_GraphStoreCase):
    """The store side of the red team's D-1, C-1 and V-1."""

    def test_a_question_naming_many_subjects_is_bounded_and_says_so(self) -> None:
        """D-1: every extra start is another frontier root and, off the exact
        path, another store-wide look-alike comparison."""
        conversation = self.memory.new_conversation(project_id=1)
        subjects = [f"Relay{index:02d}" for index in range(12)]
        for index, subject in enumerate(subjects):
            self.memory.remember_explicit_project_claim(
                conversation, 1, _command(subject, "datacenter", f"Fenwick{index}")
            )
        result = self.memory.graph_chains(
            "Where are all of these?", project_id=1, subjects=subjects,
            seed_claims=[],
        )
        self.assertEqual(
            result["report"]["subjects_dropped"], len(subjects) - 3
        )
        self.assertLessEqual(int(result["report"]["starts"]), 3)
        # The first three still answer: the bound truncates the read, it does
        # not abstain from it.
        values = {str(row["value"]) for row in result["rows"]}
        self.assertTrue(values)
        self.assertLessEqual(values, {"Fenwick0", "Fenwick1", "Fenwick2"})
        # A question inside the bound reports nothing dropped.
        modest = self.memory.graph_chains(
            "Where are these?", project_id=1, subjects=subjects[:2],
            seed_claims=[],
        )
        self.assertEqual(modest["report"]["subjects_dropped"], 0)
        self.assertEqual(
            self.memory.graph_recall_report()["subjects_dropped"], 0
        )

    def test_describe_memory_names_a_row_the_listing_would_hide(self) -> None:
        """C-1: a confirmation prompt must never say "no such memory" about a
        row that exists, and ``list_memories`` hides exactly the rows the
        erase has fixed refusals for."""
        conversation = self.memory.new_conversation(project_id=1)
        self.memory.remember_explicit_project_claim(
            conversation, 1, _command("Kestrel relay", "listen port", "9090")
        )
        backing_id = int(self.memory.db.execute(
            "SELECT memory_id FROM memory_claims ORDER BY id DESC LIMIT 1"
        ).fetchone()[0])
        # The listing does not carry it...
        self.assertNotIn(
            backing_id,
            {int(item["id"]) for item in self.memory.list_memories(with_ids=True)},
        )
        # ...but the store can still describe it, and says why the erase will
        # refuse.
        described = self.memory.describe_memory(backing_id)
        assert described is not None
        self.assertEqual(described["id"], backing_id)
        self.assertEqual(described["kind"], "claim")
        self.assertTrue(described["is_claim_backing"])
        self.assertFalse(described["is_vault_note"])
        self.assertGreater(described["content_length"], 0)
        self.assertNotIn("content", described)
        self.assertEqual(
            sorted(described),
            ["content_length", "created_at", "eligible", "id",
             "is_claim_backing", "is_vault_note", "kind", "origin"],
        )
        # It agrees with what erase_memory actually does.
        self.assertEqual(
            self.memory.erase_memory(None, backing_id)["action"], "claim_backing"
        )

    def test_describe_memory_covers_the_ordinary_and_vault_cases(self) -> None:
        self.memory.remember_verified(
            "The deploy uses rsync.", source="operator",
            origin="explicit_operator_memory",
        )
        ordinary_id = int(self.memory.db.execute(
            "SELECT id FROM memories WHERE kind='fact' ORDER BY id DESC LIMIT 1"
        ).fetchone()[0])
        ordinary = self.memory.describe_memory(ordinary_id)
        assert ordinary is not None
        self.assertEqual(ordinary["kind"], "fact")
        self.assertEqual(ordinary["origin"], "explicit_operator_memory")
        self.assertIs(ordinary["eligible"], True)
        self.assertFalse(ordinary["is_claim_backing"])
        self.assertFalse(ordinary["is_vault_note"])

        self.memory.db.execute(
            "UPDATE ordinary_memory_provenance SET origin='verified_vault_note' "
            "WHERE memory_id=?",
            (ordinary_id,),
        )
        note = self.memory.describe_memory(ordinary_id)
        assert note is not None
        self.assertTrue(note["is_vault_note"])
        self.assertEqual(
            self.memory.erase_memory(None, ordinary_id)["action"], "vault_note"
        )

    def test_describe_memory_is_none_for_a_missing_or_invalid_id(self) -> None:
        self.assertIsNone(self.memory.describe_memory(987_654))
        for bad in (0, -1, 10**19, True):
            self.assertIsNone(self.memory.describe_memory(bad))

    def test_verify_graph_on_a_dropped_graph_returns_the_module_shape(self) -> None:
        """V-1: no invented problem kind, and ``ready`` is always present."""
        conversation = self.memory.new_conversation(project_id=1)
        self._seed_chain(conversation)
        healthy = self.memory.verify_graph()
        self.assertTrue(healthy["ok"])
        self.assertTrue(healthy["ready"])

        self.memory.db.execute("PRAGMA foreign_keys=OFF")
        for statement in memory_graph.DROP_GRAPH_SQL:
            self.memory.db.execute(statement)
        self.memory.db.execute("PRAGMA foreign_keys=ON")

        report = self.memory.verify_graph()
        self.assertEqual(sorted(report), sorted(memory_graph.verify_graph(self.memory.db)))
        self.assertFalse(report["ok"])
        self.assertIs(report["ready"], False)
        self.assertEqual(report["problems"], [])
        self.assertEqual(
            set(report["excluded"]),
            {"excluded_predicate", "subject_private", "subject_too_long"},
        )
        for problem in report["problems"]:
            self.assertIn(problem["kind"], memory_graph.VERIFY_PROBLEM_KINDS)
        # The dry run follows the same rule, and verify_spine stays honest.
        dry = self.memory.rebuild_graph_projection()
        self.assertIs(dry["ready"], False)
        self.assertEqual(dry["divergences"], [])
        spine = self.memory.verify_spine()
        self.assertTrue(spine["ok"], spine["problems"])
        self.assertFalse(spine["graph_ok"])

    def test_every_verify_graph_problem_kind_is_one_the_module_declares(self) -> None:
        conversation = self.memory.new_conversation(project_id=1)
        self._seed_chain(conversation)
        self.memory.db.execute("PRAGMA foreign_keys=OFF")
        self.memory.db.execute(
            "UPDATE memory_graph_edges SET status='superseded' WHERE claim_id=1"
        )
        self.memory.db.execute("DELETE FROM memory_graph_edges WHERE claim_id=2")
        self.memory.db.execute(
            "UPDATE memory_graph_entities SET entity_key='forged key' WHERE id=1"
        )
        self.memory.db.execute("PRAGMA foreign_keys=ON")
        report = self.memory.verify_graph()
        self.assertFalse(report["ok"])
        self.assertTrue(report["problems"])
        for problem in report["problems"]:
            self.assertIn(problem["kind"], memory_graph.VERIFY_PROBLEM_KINDS)


class GraphChainSelectionTests(_GraphStoreCase):
    """Chain selection: one chain per (start, direction) is reserved before
    any start takes a second in that direction, a subject the operator named
    outranks a seed-derived start, and nothing is reported as truncated below
    the cap.

    Every case passes the **real** ``current_claims`` rows as seeds, because
    the defect this pins only appeared with them: the seed's own forward chain
    was taking the row slot the asked subject's answer needed, which is how a
    reverse two-hop question came back without the relay that answers it.
    """

    def setUp(self) -> None:
        super().setUp()
        self.conversation = self.memory.new_conversation(project_id=1)

    def _ask(self, question: str, subjects: list[str]) -> dict:
        """The agent's own call shape: real seeds, real lane mode."""
        seeds = self.memory.current_claims(question, project_id=1)
        return self.memory.graph_chains(
            question, project_id=1, subjects=subjects, seed_claims=seeds[:4],
            lane_mode=self.memory.claim_recall_report()["mode"],
        )

    @staticmethod
    def _chain_heads(result: dict) -> dict[int, tuple[str, str]]:
        """``chain number -> (subject, value)`` of that chain's first hop."""
        heads: dict[int, tuple[str, str]] = {}
        for row in result["rows"]:
            if int(row["hop"]) == 1:
                heads[int(row["chain"])] = (
                    str(row["subject"]), str(row["value"])
                )
        return heads

    def test_an_open_question_returns_a_chain_in_each_direction(self) -> None:
        """The confirmed rule: for an open question, at least one chain in
        each direction that exists is emitted before the cap applies.

        ``Harrier box`` is a hub with one edge each way -- the relay deployed
        on it points in, its datacenter points out.  An open question names no
        predicate, so both chains answer and both are one hop; before the
        reservation the forward walk took both slots and the operator was told
        about the datacenter twice over instead of once each way.
        """
        self._seed_chain(self.conversation)
        result = self._ask("What runs on the Harrier box?", ["Harrier box"])
        self.assertEqual(result["report"]["mode"], "complete")
        heads = self._chain_heads(result)
        self.assertGreaterEqual(len(heads), 2, result["rows"])
        outward = {
            chain for chain, (subject, _value) in heads.items()
            if subject == "Harrier box"
        }
        inward = {
            chain for chain, (_subject, value) in heads.items()
            if value == "Harrier box"
        }
        self.assertTrue(outward, f"no out-edge chain: {heads}")
        self.assertTrue(inward, f"no in-edge chain: {heads}")
        self.assertFalse(outward & inward, heads)
        # The in-edge chain is the one that actually answers "what runs on it",
        # and it is the row a reader has to be able to find.
        self.assertIn(
            ("Kestrel relay", "deployed on host", "Harrier box"),
            [
                (str(row["subject"]), str(row["predicate"]), str(row["value"]))
                for row in result["rows"]
            ],
        )

    def test_the_reverse_direction_survives_at_a_value_hub_too(self) -> None:
        """The same rule read from the other end: ``Fenwick`` is a value with
        facts of its own, so an open question about it must not answer only
        forwards."""
        self._seed_chain(self.conversation)
        result = self._ask("What runs in Fenwick?", ["Fenwick"])
        self.assertEqual(result["report"]["mode"], "complete")
        heads = self._chain_heads(result)
        self.assertTrue(
            any(subject == "Fenwick" for subject, _value in heads.values()),
            heads,
        )
        self.assertTrue(
            any(value == "Fenwick" for _subject, value in heads.values()),
            heads,
        )

    def test_nothing_is_reported_truncated_below_the_cap(self) -> None:
        """Only the cap can truncate, so a store with one chain must not say a
        chain was left out -- an overflow note beside a complete answer tells
        the operator to ask a narrower question for facts that do not exist."""
        self.memory.remember_explicit_project_claim(
            self.conversation, 1, _command("Harrier box", "datacenter", "Fenwick")
        )
        result = self._ask("What runs on the Harrier box?", ["Harrier box"])
        self.assertEqual(result["report"]["mode"], "complete")
        self.assertEqual(int(result["report"]["chains"]), 1)
        self.assertEqual(int(result["report"]["truncated_chains"]), 0)
        self.assertEqual(result["overflow"], [])

    def test_a_named_start_outranks_a_seed_derived_forward_chain(self) -> None:
        """A subject the operator typed is answered before a start the main
        lane happened to seed.

        The lane seeds ``Kestrel relay / deployed on host``, whose value makes
        ``Harrier box`` a start too; the named subject's own two-hop chain has
        to win the slot, and the other relay's chain must not be swept in.
        """
        for subject, predicate, value in (
            ("Kestrel relay", "deployed on host", "Harrier box"),
            ("Harrier box", "datacenter", "Fenwick"),
            ("Osprey relay", "deployed on host", "Talon box"),
            ("Talon box", "datacenter", "Moss Hollow"),
        ):
            self.memory.remember_explicit_project_claim(
                self.conversation, 1, _command(subject, predicate, value)
            )
        question = "Which datacenter hosts the Kestrel relay?"
        seeds = self.memory.current_claims(question, project_id=1)
        # The seed is real and is the thing that used to take the slot.
        self.assertEqual(
            [(str(item["subject"]), str(item["predicate"])) for item in seeds[:4]],
            [("Kestrel relay", "deployed on host")],
        )
        result = self.memory.graph_chains(
            question, project_id=1, subjects=["Kestrel relay"],
            seed_claims=seeds[:4],
            lane_mode=self.memory.claim_recall_report()["mode"],
        )
        self.assertEqual(result["report"]["mode"], "complete")
        self.assertEqual(
            [
                (str(row["subject"]), str(row["predicate"]), str(row["value"]))
                for row in result["rows"]
            ],
            [
                ("Kestrel relay", "deployed on host", "Harrier box"),
                ("Harrier box", "datacenter", "Fenwick"),
            ],
        )
        self.assertEqual([int(row["hop"]) for row in result["rows"]], [1, 2])
        self.assertEqual(int(result["report"]["truncated_chains"]), 0)
        body = json.dumps(result)
        self.assertNotIn("Osprey relay", body)
        self.assertNotIn("Moss Hollow", body)


class LaneFloorAndSeedFidelityTests(_GraphStoreCase):
    """Holdout v1 triage, store side.

    The sealed holdout failed with four ``lookalike`` and two ``two_subjects``
    cases answering ``no-answer`` and two ``private_in_chain`` leaks.  These
    tests pin the three store-side properties that failure mode would have
    implicated, so that whatever the real cause turns out to be, a regression
    here is caught by the development battery instead of by a one-use holdout.
    """

    # Every lane mode the claims lane can report, and what 2.3d/5.6 say the
    # graph must do with it.
    SECURITY_ABSTENTIONS = ("screened", "project-unavailable", "corrupt-strongest",
                            "error")
    IDENTITY_FLOORS = ("identity-conflict", "identity-overflow")
    CAPACITY_OUTCOMES = (None, "or", "all-terms", "overflow", "ambiguous")

    def setUp(self) -> None:
        super().setUp()
        self.conversation = self.memory.new_conversation(project_id=1)
        for subject, value in (
            ("Kestrel relay", "Fenwick"),
            ("Kestrel relay 2", "Moss Hollow"),
            ("Kestrelrelay", "Zephyrhold"),
        ):
            self.memory.remember_explicit_project_claim(
                self.conversation, 1, _command(subject, "datacenter", value)
            )

    def _ask(self, subjects: list[str], lane_mode: str | None) -> dict:
        return self.memory.graph_chains(
            "Which datacenter hosts it?", project_id=1, subjects=subjects,
            seed_claims=[], lane_mode=lane_mode,
        )

    def test_an_identity_floor_restricts_nothing_but_still_says_so(self) -> None:
        """2.3d as design 10.3 item 1 left it, over every lane mode.

        An identity floor is the lane saying *its own* substring scan could
        not tell which subject was meant.  That is not evidence against the
        graph's rules, each of which carries its own floor -- and the sealed
        holdout showed the old gate turning four correct resolutions into
        ``no-start``, two of which should have abstained ``identity-conflict``
        and could not, because the rule that raises it never ran.  So the
        floor now restricts **nothing**; what survives is the honest part,
        the clause telling the operator the lane could not tell.
        """
        for lane in self.IDENTITY_FLOORS:
            exact = self._ask(["Kestrel relay"], lane)
            self.assertEqual(exact["report"]["mode"], "complete", lane)
            self.assertEqual(
                [str(row["value"]) for row in exact["rows"]], ["Fenwick"], lane
            )
            self.assertTrue(exact["report"]["lane_abstained"], lane)

            both = self._ask(["Kestrel relay", "Kestrelrelay"], lane)
            self.assertEqual(both["report"]["mode"], "complete", lane)
            self.assertEqual(
                sorted(str(row["value"]) for row in both["rows"]),
                ["Fenwick", "Zephyrhold"],
                lane,
            )

            # The load-bearing half of the ruling: a non-exact name behaves
            # exactly as it does under a capacity outcome -- it reaches its
            # own look-alike floor and abstains there, rather than being
            # refused a start it never got to earn.
            non_exact = self._ask(["Kestrel rely"], lane)
            self.assertEqual(
                non_exact["report"]["mode"], "identity-conflict", lane
            )
            self.assertEqual(non_exact["rows"], [], lane)
            self.assertFalse(non_exact["report"]["lane_abstained"], lane)

    def test_a_lane_mode_never_changes_what_resolves(self) -> None:
        """10.3 item 1 stated as the invariant a reader can check: for every
        mode that does not silence the channel, the same question resolves the
        same way.  Only the lane-abstained clause differs."""
        baseline = {
            label: self._ask(subjects, None)["report"]["mode"]
            for label, subjects in (
                ("exact", ["Kestrel relay"]),
                ("two exact", ["Kestrel relay", "Kestrelrelay"]),
                ("non-exact", ["Kestrel rely"]),
            )
        }
        for lane in self.IDENTITY_FLOORS + self.CAPACITY_OUTCOMES:
            for label, subjects in (
                ("exact", ["Kestrel relay"]),
                ("two exact", ["Kestrel relay", "Kestrelrelay"]),
                ("non-exact", ["Kestrel rely"]),
            ):
                self.assertEqual(
                    self._ask(subjects, lane)["report"]["mode"],
                    baseline[label],
                    f"lane={lane} {label}",
                )

    def test_a_capacity_outcome_places_no_restriction_on_the_graph(self) -> None:
        """``overflow`` and ``ambiguous`` are capacity, not identity (5.6.2),
        so non-exact resolution keeps working and the cue gains no clause."""
        for lane in self.CAPACITY_OUTCOMES:
            exact = self._ask(["Kestrel relay"], lane)
            self.assertEqual(exact["report"]["mode"], "complete", lane)
            self.assertFalse(exact["report"]["lane_abstained"], lane)
            non_exact = self._ask(["Kestrel rely"], lane)
            # Non-exact still runs, and abstains on its own floor rather than
            # because the lane said anything.
            self.assertEqual(
                non_exact["report"]["mode"], "identity-conflict", lane
            )

    def test_a_security_abstention_silences_the_channel(self) -> None:
        for lane in self.SECURITY_ABSTENTIONS:
            result = self._ask(["Kestrel relay"], lane)
            expected = (
                "project-unavailable" if lane == "project-unavailable" else "screened"
            )
            self.assertEqual(result["report"]["mode"], expected, lane)
            self.assertEqual(result["rows"], [], lane)
        # The set this test iterates is the one the store actually consults,
        # so a mode added to one and not the other cannot go unnoticed.
        self.assertEqual(
            set(self.SECURITY_ABSTENTIONS), set(memory_lane_silencing_modes())
        )

    def test_the_lane_s_rows_reach_the_walk_unchanged(self) -> None:
        """The seeds are the lane's own rows, not a re-derived copy.

        A seed carries the ``claim_id`` the walk uses to make the lane's row
        hop 1 of the chain, so anything that rebuilt or reordered them here
        would silently cost the chain its head.
        """
        question = "Which datacenter hosts the Kestrel relay?"
        seeds = self.memory.current_claims(question, project_id=1)
        self.assertTrue(seeds)
        captured: dict[str, Any] = {}
        real_walk = memory_graph.graph_walk

        def spy(db: Any, **kwargs: Any) -> Any:
            captured.update(kwargs)
            return real_walk(db, **kwargs)

        with patch.object(memory_graph, "graph_walk", spy):
            self.memory.graph_chains(
                question, project_id=1, subjects=["Kestrel relay"],
                seed_claims=seeds[:4],
                lane_mode=self.memory.claim_recall_report()["mode"],
            )
        passed = list(captured.get("seed_claims") or [])
        self.assertEqual(passed, seeds[:4])
        # Identity, not just equality: the same objects go through.
        self.assertTrue(
            all(a is b for a, b in zip(passed, seeds[:4])), "seeds were copied"
        )
        self.assertEqual(captured.get("subjects"), ["Kestrel relay"])

    def test_marking_retracted_rows_never_drops_one(self) -> None:
        """The retraction pass sets a flag and nothing else; a row that
        reached it must still be in the block afterwards."""
        self.memory.remember_explicit_project_claim(
            self.conversation, 1,
            _command("Kestrel relay", "deployed on host", "Harrier box"),
        )
        self.memory.retract_explicit_project_claim(
            self.conversation, 1, _forget("Kestrel relay", "deployed on host")
        )
        result = self.memory.graph_chains(
            "Where used the Kestrel relay to be deployed?", project_id=1,
            subjects=["Kestrel relay"], seed_claims=[], temporal=True,
        )
        rows = result["rows"]
        self.assertTrue(rows)
        before = [dict(row) for row in rows]
        # Idempotent, and it cannot shorten the list.
        self.memory._mark_retracted_chain_rows(rows, {})
        self.assertEqual(len(rows), len(before))
        self.memory._mark_retracted_chain_rows(rows, {})
        self.assertEqual(len(rows), len(before))


class ChainRowLeakTests(_GraphStoreCase):
    """Holdout v1 reported two ``private_in_chain`` leaks.  Leakage is scored
    as any returned **subject or value** failing ``contains_secret`` or
    ``contains_private_identifier_extended``, so this walks the whole returned
    payload -- rows, overflow entries, notes and ``bridge_from`` -- the way
    the scorer does, rather than checking the fields a test author remembered.
    """

    # One per 6.2 kind, as a chain's middle value and as a subject.
    PRIVATE_SHAPES = (
        ("phone", "+1 (415) 555-0199"),
        ("ipv4", "10.0.0.7"),
        ("ipv6", "fe80::1ff:fe23:4567:890a"),
        ("ssn", "123-45-6789"),
        ("card", "4111 1111 1111 1111"),
        ("street_address", "221B Baker Street"),
        ("email", "alice@example.com"),
        ("ip_host_email", "admin@" + "10.0.0.7"),
        # holdout v1 leak-r011: a Luhn-valid PAN with no separators at all.
        # The grouped-form pattern never saw a bare 16-digit run.
        ("card_unseparated", "4111111111111111"),
    )

    @staticmethod
    def _leaks(payload: Any) -> list[str]:
        found: list[str] = []

        def walk(node: Any, path: str) -> None:
            if isinstance(node, dict):
                for key, value in node.items():
                    walk(value, f"{path}.{key}")
            elif isinstance(node, list):
                for index, value in enumerate(node):
                    walk(value, f"{path}[{index}]")
            elif isinstance(node, str) and node:
                if contains_secret(node) or contains_private_identifier_extended(node):
                    found.append(f"{path} = {node[:90]!r}")

        walk(payload, "")
        return found

    def test_no_private_shape_reaches_the_block_from_any_position(self) -> None:
        for kind, private in self.PRIVATE_SHAPES:
            with self.subTest(kind=kind):
                # A store per shape, closed inline: tearDown removes the
                # temporary directory before addCleanup would run.
                store_path = self.data_dir / f"{kind}.db"
                memory = Memory(store_path)
                try:
                    # The private string sits in the middle of a chain, so a
                    # walk that failed to break at that hop would carry it
                    # into the next row's bridge_from as well as its own value.
                    memory.remember_claim(
                        "Kestrel relay", "managed by", private,
                        source="fixture", authority="verified",
                    )
                    memory.remember_claim(
                        private, "datacenter", "Fenwick",
                        source="fixture", authority="verified",
                    )
                    memory.remember_claim(
                        "Fenwick", "region", "Northgate",
                        source="fixture", authority="verified",
                    )
                    # It is never a node: value admission screens it to a
                    # literal, and as a subject the claim is excluded from the
                    # projection outright.
                    self.assertNotIn(
                        memory_graph.entity_key(private),
                        {
                            str(row[0]) for row in memory.db.execute(
                                "SELECT entity_key FROM memory_graph_entities"
                            )
                        },
                    )
                    for question, subjects in (
                        ("Who manages the Kestrel relay?", ["Kestrel relay"]),
                        ("Which datacenter hosts the Kestrel relay?",
                         ["Kestrel relay"]),
                        ("Which region is the Kestrel relay in?",
                         ["Kestrel relay"]),
                        ("What is in Fenwick?", ["Fenwick"]),
                        ("What runs in Northgate?", ["Northgate"]),
                    ):
                        result = memory.graph_chains(
                            question, project_id=1, subjects=subjects,
                            seed_claims=[],
                        )
                        self.assertEqual(
                            self._leaks(result), [], f"{kind}: {question}"
                        )
                finally:
                    memory.close()
                    Path(
                        str(store_path) + memory_spine.KEY_SIDECAR_SUFFIX
                    ).unlink(missing_ok=True)

    def test_an_identifier_past_the_scan_cap_never_reaches_the_block(self) -> None:
        """The 6.2 long-value rule, end to end: the row is dropped and an
        unrelated chain in the same store still answers."""
        conversation = self.memory.new_conversation(project_id=1)
        del conversation
        value = ("filler " * 90) + "reach the operator on 415 555 0199 today"
        self.assertGreater(len(value), 512)
        self.assertTrue(screen_endpoint(value)[0])
        self.memory.remember_claim(
            "Kestrel relay", "runbook", value, source="fixture", authority="verified"
        )
        self.memory.remember_claim(
            "Kestrel relay", "deployed on host", "Harrier box",
            source="fixture", authority="verified",
        )
        self.memory.remember_claim(
            "Harrier box", "datacenter", "Fenwick",
            source="fixture", authority="verified",
        )
        runbook = self.memory.graph_chains(
            "What is the runbook for the Kestrel relay?", project_id=1,
            subjects=["Kestrel relay"], seed_claims=[],
        )
        self.assertEqual(runbook["rows"], [])
        self.assertEqual(self._leaks(runbook), [])
        # The screened row does not take the rest of the store down with it.
        chain = self.memory.graph_chains(
            "Which datacenter hosts the Kestrel relay?", project_id=1,
            subjects=["Kestrel relay"], seed_claims=[],
        )
        self.assertEqual(chain["report"]["mode"], "complete")
        self.assertEqual(self._leaks(chain), [])

    def test_an_over_long_value_is_screened_even_with_nothing_to_match(self) -> None:
        """holdout v1 leak-r024: 600 characters of prose, no digits, no ``@``,
        no hex -- nothing for a run rule to find past the scan cap.

        Past 512 characters the screen cannot see the whole value, so it must
        not vouch for it: an over-long value is the ``long_value`` kind on its
        own (design 2.4, 1.4).  This is the store-side half -- the value is a
        literal and never a node either way, so what changes is whether the
        row reaches the block.
        """
        value = "lorem ipsum dolor sit amet consectetur adipiscing elit sed " * 11
        self.assertGreater(len(value), 512)
        self.assertNotIn("@", value)
        self.assertFalse(any(character.isdigit() for character in value))
        self.assertEqual(screen_endpoint(value), (True, "long_value"))

        self.memory.remember_claim(
            "Kestrel relay", "runbook", value,
            source="fixture", authority="verified",
        )
        self.memory.remember_claim(
            "Kestrel relay", "deployed on host", "Harrier box",
            source="fixture", authority="verified",
        )
        self.memory.remember_claim(
            "Harrier box", "datacenter", "Fenwick",
            source="fixture", authority="verified",
        )
        self.assertEqual(
            {
                str(row[0]) for row in self.memory.db.execute(
                    "SELECT DISTINCT value_kind FROM memory_graph_edges "
                    "WHERE predicate_key='runbook'"
                )
            },
            {"literal"},
        )
        runbook = self.memory.graph_chains(
            "What is the runbook for the Kestrel relay?", project_id=1,
            subjects=["Kestrel relay"], seed_claims=[],
        )
        self.assertEqual(runbook["rows"], [])
        self.assertEqual(self._leaks(runbook), [])
        # A value just under the cap is not swept up with it: the rule is the
        # scan cap, not a general dislike of long text.
        short = "lorem ipsum dolor sit amet consectetur adipiscing elit"
        self.assertFalse(screen_endpoint(short)[0])
        self.memory.remember_claim(
            "Osprey relay", "runbook", short,
            source="fixture", authority="verified",
        )
        readable = self.memory.graph_chains(
            "What is the runbook for the Osprey relay?", project_id=1,
            subjects=["Osprey relay"], seed_claims=[],
        )
        self.assertEqual(readable["report"]["mode"], "complete")
        self.assertEqual(
            [str(row["value"]) for row in readable["rows"]], [short]
        )

    def test_a_private_value_never_becomes_a_hub_an_overflow_note_can_name(self) -> None:
        """An overflow note names the hub's own spelling, so a private value
        that became an entity would leak through the note rather than a row."""
        for index in range(20):
            self.memory.remember_claim(
                f"Box{index:02d}", "managed by", "+1 (415) 555-0199",
                source="fixture", authority="verified",
            )
        self.assertEqual(
            {
                str(row[0]) for row in self.memory.db.execute(
                    "SELECT DISTINCT value_kind FROM memory_graph_edges"
                )
            },
            {"literal"},
        )
        for question, subjects in (
            ("Who manages Box00?", ["Box00"]),
            ("What is managed by that number?", ["Box00"]),
        ):
            result = self.memory.graph_chains(
                question, project_id=1, subjects=subjects, seed_claims=[]
            )
            self.assertEqual(self._leaks(result), [], question)


class AskedWordAcceptanceTests(_GraphStoreCase):
    """Design 10.3 item 2, on the shape holdout v2 quarantined over.

    The pair is one store and one question shape with opposite outcomes: an
    attribute the asking project **can** reach answers, and an attribute it
    **cannot** returns nothing.  Both facts in the failing case were true,
    which is what made the substitution hard for an operator to catch --
    "which almanac?" was answered with a moorage and a district.

    The store is the quarantined ``scope`` store's shape: two projects, global
    rows, and a project row shadowing a global one, with the almanac fact
    living in project one where project two cannot see it.
    """

    def setUp(self) -> None:
        super().setUp()
        self.project_two = self.memory.add_project("Project 2", "@projects/p2")
        one = self.memory.new_conversation(project_id=1)
        two = self.memory.new_conversation(project_id=self.project_two)
        for subject, predicate, value in (
            ("Ombersley wharf", "district", "Fennimore ward"),
            ("Halewood ward", "charter", "Bellrock lodge"),
            ("Fennimore ward", "charter", "Duskmere lodge"),
        ):
            self.memory.remember_claim(
                subject, predicate, value, source="fixture", authority="operator"
            )
        for subject, predicate, value in (
            ("Ombersley wharf", "district", "Halewood ward"),
            ("Aldwin barge", "moorage", "Ombersley wharf"),
            # The almanac lives here, in project one only.
            ("Halewood ward", "almanac", "Corvey press"),
        ):
            self.memory.remember_explicit_project_claim(
                one, 1, _command(subject, predicate, value)
            )
        for subject, predicate, value in (
            ("Aldwin barge", "moorage", "Wexlow wharf"),
            ("Wexlow wharf", "district", "Tarrow ward"),
            ("Tarrow ward", "charter", "Keldmoor lodge"),
        ):
            self.memory.remember_explicit_project_claim(
                two, self.project_two, _command(subject, predicate, value)
            )

    def _ask(self, question: str) -> dict:
        seeds = self.memory.current_claims(
            question, limit=8, project_id=self.project_two
        )[:4]
        return self.memory.graph_chains(
            question, project_id=self.project_two, subjects=["Aldwin barge"],
            seed_claims=seeds,
            lane_mode=self.memory.claim_recall_report().get("mode"),
            limit=8,
        )

    def test_an_asked_attribute_the_project_can_reach_answers(self) -> None:
        result = self._ask("Which charter lists the Aldwin barge?")
        self.assertEqual(result["report"]["mode"], "complete")
        triples = [
            (str(row["subject"]), str(row["predicate"]), str(row["value"]))
            for row in result["rows"]
        ]
        # It has to travel three hops to get there, through this project's own
        # wharf and ward, and end on the charter edge.
        self.assertIn(
            ("Tarrow ward", "charter", "Keldmoor lodge"), triples, triples
        )
        self.assertNotIn("Corvey press", json.dumps(result))

    def test_an_asked_attribute_the_project_cannot_reach_answers_nothing(self) -> None:
        """The quarantined case. ``almanac`` is a real asked attribute that
        project two genuinely cannot reach: it survives subject-word stripping
        (``barge`` is the subject's word, ``lists`` the verb), so the question
        is unanswerable rather than open, and answering it with the barge's
        moorage and district would be the neighbour substitution 5.4 forbids.
        """
        result = self._ask("Which almanac lists the Aldwin barge?")
        self.assertEqual(result["rows"], [])
        self.assertEqual(result["report"]["mode"], "no-answer")
        self.assertTrue(result["report"]["abstained"])
        # And nothing from project one reaches the payload by any path.
        body = json.dumps(result)
        for text in ("Corvey press", "Halewood ward", "Ombersley wharf"):
            self.assertNotIn(text, body)

    def test_the_pair_differs_only_in_the_asked_attribute(self) -> None:
        """Same store, same subject, same question shape: the only difference
        is whether the attribute exists in the asking project's vocabulary."""
        charter = self._ask("Which charter lists the Aldwin barge?")
        almanac = self._ask("Which almanac lists the Aldwin barge?")
        self.assertEqual(charter["report"]["mode"], "complete")
        self.assertEqual(almanac["report"]["mode"], "no-answer")
        self.assertTrue(charter["rows"])
        self.assertEqual(almanac["rows"], [])

    def test_every_returned_row_carries_a_visible_scope(self) -> None:
        """Design 10.3 item 4: chain rows now carry ``scope``, which is what
        lets a scope check be made on the row itself rather than inferred."""
        result = self._ask("Which charter lists the Aldwin barge?")
        self.assertTrue(result["rows"])
        visible = {"global", f"project:{self.project_two}"}
        for row in result["rows"]:
            self.assertIn("scope", row, row)
            self.assertIn(str(row["scope"]), visible, row)


class HoldoutV3RegressionTests(_GraphStoreCase):
    """The five v3 failures, driven through ``Memory.graph_chains`` with the
    **real** claims-lane mode and the lane's own seed rows.

    Every one of these passed at module level while failing through the store,
    or would have: the lane is what hands the graph a row about a subject the
    operator did not name, and the lane lives in ``memory.py``, which the
    holdout authors do not read.  So each case runs the whole seam --
    ``current_claims`` for the seeds, ``claim_recall_report()`` for the mode --
    rather than passing a hand-built seed list.
    """

    def setUp(self) -> None:
        super().setUp()
        self.conversation = self.memory.new_conversation(project_id=1)

    def _seed(self, *facts: tuple[str, str, str], scope: str = "project") -> None:
        for subject, predicate, value in facts:
            if scope == "global":
                self.memory.remember_claim(
                    subject, predicate, value,
                    source="fixture", authority="operator",
                )
            else:
                self.memory.remember_explicit_project_claim(
                    self.conversation, 1, _command(subject, predicate, value)
                )

    def _ask(self, question: str, subjects: list[str]) -> dict:
        """The agent's own call shape: real seeds, real lane mode."""
        seeds = self.memory.current_claims(question, project_id=1)
        return self.memory.graph_chains(
            question, project_id=1, subjects=subjects, seed_claims=seeds[:4],
            lane_mode=self.memory.claim_recall_report().get("mode"),
        )

    def test_an_ambiguous_one_word_alias_abstains_identity_conflict(self) -> None:
        """10.7 item 1: a one-word name whose last-word alias has two or more
        candidates abstains ``identity-conflict``, never ``no-start``.

        v3 c-al-03 returned ``no-start`` because §2.3 source 3 said "two or
        more -> no start"; §2.3(b).2 and §5.6.4 govern, and the sentence was
        withdrawn.  The distinction is what an operator is told: "I have never
        heard of that" versus "I cannot tell which one you mean".
        """
        self._seed(
            ("Marchbank Loom8", "parish", "Elder fold"),
            ("Pendreth Loom8", "parish", "Wicker fold"),
        )
        result = self._ask("What is the parish of Loom8?", ["Loom8"])
        self.assertEqual(result["report"]["mode"], "identity-conflict")
        self.assertEqual(result["rows"], [])
        self.assertTrue(result["report"]["abstained"])

    def test_a_seed_never_answers_for_a_name_the_store_cannot_identify(self) -> None:
        """10.7 item 3, and the run's one leak (v3 c-al-05).

        The lane's OR-scan matches on shared words, so a question about the
        unknown ``Yealand fold`` arrives with a row about ``Yealand mill``.
        Both of that row's endpoints are exact keys, so before item 3 the
        graph started from them and answered about the mill -- a forbidden
        record.  When the question names subjects and none resolves, seeds
        contribute nothing.
        """
        self._seed(
            ("Yealand mill", "parish", "Zennorly fold"),
            ("Yealand croft", "parish", "Brackenby fold"),
        )
        question = "What is the parish of the Yealand fold?"
        # The seed really is the mill: the case is only meaningful if the lane
        # hands the graph the row that used to answer.
        seeds = self.memory.current_claims(question, project_id=1)
        self.assertIn(
            "Yealand mill", {str(item["subject"]) for item in seeds[:4]}
        )
        self.assertEqual(self.memory.claim_recall_report().get("mode"), "or")

        result = self._ask(question, ["Yealand fold"])
        self.assertEqual(result["report"]["mode"], "no-start")
        self.assertEqual(result["rows"], [])
        # Nothing about the mill reaches the caller by any path -- not a row,
        # not an overflow note, not a bridge_from.
        body = json.dumps(result)
        for text in ("Yealand mill", "Zennorly", "Brackenby", "Yealand croft"):
            self.assertNotIn(text, body)

    def test_one_unidentified_name_does_not_abstain_a_resolved_one(self) -> None:
        """10.7 item 4 with the G5 refinement: the call answers from the
        resolved start, names the unidentified spelling in ``unresolved``, and
        carries **nothing** about that name's look-alikes.

        Before G5 the lane's OR-scan seeded ``Tarnworth bolt 2 / parish /
        Birch fold`` and the graph answered with it beside the Thornbeck
        chain.  Every row was honestly labelled, so it was not a substitution
        in the strict sense -- but an operator who asked about the Tarnworth
        *mill* was shown a parish for a Tarnworth *bolt*, which is the
        confusion the unresolved line exists to prevent, not to caption.  A
        seed row is now dropped whole when either endpoint look-alikes a typed
        name that resolved nothing.
        """
        self._seed(
            ("Tarnworth bolt", "parish", "Alder fold"),
            ("Tarnworth bolt 2", "parish", "Birch fold"),
            ("Tarnworthbolt", "parish", "Cedar fold"),
            ("Thornbeck bolt", "parish", "Dunmoor fold"),
        )
        question = "What is the parish of the Tarnworth mill and the Thornbeck bolt?"
        # The lane really does hand over the look-alike rows, so the case
        # cannot pass because the seeds were empty.
        seeds = self.memory.current_claims(question, project_id=1)
        self.assertTrue(
            any(str(item["subject"]).startswith("Tarnworth") for item in seeds[:4]),
            [str(item["subject"]) for item in seeds[:4]],
        )

        result = self._ask(question, ["Tarnworth mill", "Thornbeck bolt"])
        self.assertEqual(result["report"]["mode"], "complete")
        self.assertEqual(result["report"]["unresolved"], ["Tarnworth mill"])

        triples = [
            (str(row["subject"]), str(row["predicate"]), str(row["value"]))
            for row in result["rows"]
        ]
        self.assertEqual(triples, [("Thornbeck bolt", "parish", "Dunmoor fold")])
        self.assertNotIn(("Tarnworth bolt 2", "parish", "Birch fold"), triples)

        # Nothing about the unresolved name's family reaches the caller by any
        # path -- not a row, an overflow note or a bridge_from.  The one place
        # the name is allowed to appear is ``unresolved``, which is what tells
        # the operator that half the question went unanswered.
        self.assertNotIn("tarnworth", json.dumps(result["rows"]).casefold())
        self.assertNotIn("tarnworth", json.dumps(result["overflow"]).casefold())
        report_without_unresolved = {
            key: value for key, value in result["report"].items()
            if key != "unresolved"
        }
        self.assertNotIn(
            "tarnworth", json.dumps(report_without_unresolved).casefold()
        )

    def test_the_look_alike_drop_never_removes_a_named_subject_s_own_start(self) -> None:
        """The boundary of the G5 rule, at the seam where its inputs are made.

        G5 drops a seed row whose endpoint look-alikes a typed name that
        resolved nothing.  The rule must reach **seed-derived** starts only:
        a subject the operator actually typed, which resolved, must keep its
        chain even when it is itself a look-alike of the unresolved name --
        they share a first word by construction in these shapes.  Getting that
        wrong turns a safe over-drop into a real miss, and it is the thing a
        later narrowing of the rule could break silently.

        Verified here rather than only at ``resolve_starts`` level because the
        **lane** makes this rule's inputs: it matches on shared words, so it is
        the most likely source both of a droppable seed and of a false catch.
        Each shape asserts the lane genuinely seeds the look-alike family, so
        none of them can pass because the seeds happened to be empty.
        """
        shapes = (
            (
                "named resolves exactly",
                (
                    ("Tarnworth bolt", "parish", "Dowel ward"),
                    ("Tarnworth bolt 2", "parish", "Birch ward"),
                    ("Tarnworthbolt", "parish", "Cedar ward"),
                ),
                "What is the parish of the Tarnworth bolt and the Tarnworth mill?",
                ["Tarnworth bolt", "Tarnworth mill"],
                "Tarnworth bolt",
                "Tarnworth mill",
            ),
            (
                "named resolves through the one-word alias",
                (
                    ("Quarrenden Loom7", "parish", "Alder ward"),
                    ("Marchbank Spindle", "parish", "Birch ward"),
                ),
                "What is the parish of Loom7 and the Quarrenden mill?",
                ["Loom7", "Quarrenden mill"],
                "Quarrenden Loom7",
                "Quarrenden mill",
            ),
            (
                "and a look-alike seed is dropped in the same call",
                (
                    ("Tarnworth bolt", "parish", "Dowel ward"),
                    ("Tarnworth bolt 2", "parish", "Birch ward"),
                    ("Tarnworthbolt", "parish", "Cedar ward"),
                    ("Tarnworth bolt", "vatworks", "Uplyme mill"),
                ),
                "What is the parish of the Tarnworth bolt and the Tarnworth mill?",
                ["Tarnworth bolt", "Tarnworth mill"],
                "Tarnworth bolt",
                "Tarnworth mill",
            ),
        )
        for label, facts, question, subjects, kept, unresolved in shapes:
            with self.subTest(shape=label):
                store_path = self.data_dir / f"{abs(hash(label))}.db"
                memory = Memory(store_path)
                try:
                    conversation = memory.new_conversation(project_id=1)
                    for subject, predicate, value in facts:
                        memory.remember_explicit_project_claim(
                            conversation, 1, _command(subject, predicate, value)
                        )
                    seeds = memory.current_claims(question, project_id=1)
                    self.assertTrue(
                        seeds, f"{label}: the lane seeded nothing, case is vacuous"
                    )
                    result = memory.graph_chains(
                        question, project_id=1, subjects=subjects,
                        seed_claims=seeds[:4],
                        lane_mode=memory.claim_recall_report().get("mode"),
                    )
                    report = result["report"]
                    self.assertEqual(report["mode"], "complete", label)
                    self.assertEqual(report["unresolved"], [unresolved], label)
                    row_subjects = {str(row["subject"]) for row in result["rows"]}
                    # The named subject's own chain survives...
                    self.assertIn(kept, row_subjects, label)
                    # ...and the look-alikes it shares a first word with do not
                    # ride in on a seed.
                    for stranger in ("Tarnworth bolt 2", "Tarnworthbolt",
                                     "Marchbank Spindle"):
                        if stranger != kept:
                            self.assertNotIn(stranger, row_subjects, label)
                    # The unresolved name is reported, never answered.
                    self.assertNotIn(unresolved, row_subjects, label)
                finally:
                    memory.close()
                    Path(
                        str(store_path) + memory_spine.KEY_SIDECAR_SUFFIX
                    ).unlink(missing_ok=True)

    def test_a_screened_asked_fact_reports_the_lane_s_own_silence(self) -> None:
        """10.7 item 5 (v3 c-pv-06): when the *asked* fact's value is screened
        content the lane abstains ``corrupt-strongest``, which silences the
        channel and reports ``screened`` -- not ``screened-rows``, which is
        for a screened row that is not the lane's strongest match."""
        self._seed(
            # ``operator`` is one of the release gate's allowlisted placeholder
            # usernames, and the value still screens as ``user_home``:
            # the screen matches the shape, not who is in it.
            ("Uplyme bolt", "haulage",
             r"C:\\Users\\operator\\Documents\\haulage.txt"),
            scope="global",
        )
        question = "What is the haulage of the Uplyme bolt?"
        self.memory.current_claims(question, project_id=1)
        self.assertEqual(
            self.memory.claim_recall_report().get("mode"), "corrupt-strongest"
        )
        result = self._ask(question, ["Uplyme bolt"])
        self.assertEqual(result["report"]["mode"], "screened")
        self.assertEqual(result["rows"], [])
        self.assertNotIn("haulage.txt", json.dumps(result))

    def test_only_the_row_whose_terminal_overflowed_is_marked_incomplete(self) -> None:
        """10.7 item 6 as refined: inside a sibling group only the row whose
        own terminal is the overflowing hub is marked; a sibling ending
        elsewhere stays complete.

        The v3 scorer saw no marked row for this shape through
        ``graph_chains`` while it was marked at module level.  Measured here
        end to end, the flag survives the store path intact -- the miss was
        the module-level defect, not a store-side drop.
        """
        self._seed(
            ("Ravensmere fold", "moot", "Kirkhollow hall"),
            ("Ravensmere fold", "emblem", "Amber lozenge"),
        )
        for index in range(40):
            self._seed((f"Steading{index:02d}", "parish", "Kirkhollow hall"))

        result = self._ask(
            "What is in the Ravensmere fold?", ["Ravensmere fold"]
        )
        self.assertEqual(result["report"]["mode"], "complete")
        by_predicate = {
            str(row["predicate"]): row for row in result["rows"]
        }
        self.assertIn("moot", by_predicate)
        self.assertIn("emblem", by_predicate)
        # The moot row's terminal IS the overflowing hub.
        self.assertTrue(by_predicate["moot"].get("incomplete"), by_predicate["moot"])
        # Its sibling ends at a name with nothing behind it and stays complete.
        self.assertFalse(
            by_predicate["emblem"].get("incomplete"), by_predicate["emblem"]
        )
        self.assertEqual(int(result["report"]["incomplete"]), 1)

        self.assertEqual(len(result["overflow"]), 1)
        entry = result["overflow"][0]
        self.assertEqual(str(entry["subject"]), "Kirkhollow hall")
        self.assertIn("stored facts link to this name", str(entry["note"]))
        # The hub is reported at hop 1, not hop 2, and the reason is the lane:
        # its seed row `Ravensmere fold / moot / Kirkhollow hall` makes the
        # hub's own key a start (design 2.3 source 2), so its expansion is the
        # first hop rather than the second.  Asserted so the number is
        # explained rather than merely pinned.
        self.assertEqual(int(entry["hop"]), 1)


if __name__ == "__main__":  # pragma: no cover - convenience
    unittest.main()
