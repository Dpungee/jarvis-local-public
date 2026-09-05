from __future__ import annotations

import hashlib
import io
import json
import random
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from jarvis import memory_spine, skill_evolution
from jarvis.agent import Agent
from jarvis.cli import _run_spine
from jarvis.config import Config
from jarvis.memory import Memory, SCHEMA_VERSION, now_iso
from jarvis.vault import Vault


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


class ModelResponse(dict):
    def __init__(self, content: str) -> None:
        super().__init__(role="assistant", content=content)
        self.done_reason = None
        self.done = True


class ScriptedModelClient:
    def __init__(self, replies: list[str] | None = None) -> None:
        self.replies = list(replies or [])
        self.requests: list[dict[str, object]] = []

    def models(self, refresh: bool = True) -> list[str]:
        del refresh
        return ["qwen3.5:9b", "gpt-oss:20b", "qwen3-coder:30b"]

    def chat(self, *args: object, **kwargs: object) -> object:
        self.requests.append({"args": args, "kwargs": kwargs})
        content = self.replies.pop(0) if self.replies else "Understood."
        return ModelResponse(content)


class _SpineStoreCase(unittest.TestCase):
    """Shared store fixture and helpers for the spine integration tests."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.workspace = root / "workspace"
        self.data_dir = root / "data"
        self.workspace.mkdir()
        self.data_dir.mkdir()
        # The CLI opens data_dir / "jarvis.db"; use the same file so the
        # `spine` commands exercise this store.
        self.db_path = self.data_dir / "jarvis.db"
        self.memory = Memory(self.db_path)
        self.events: list[str] = []

    def tearDown(self) -> None:
        self.memory.close()
        self.temp.cleanup()

    def _agent(self, replies: list[str] | None = None, **overrides) -> tuple[Agent, ScriptedModelClient]:
        fields: dict[str, object] = {
            "autonomy": "autonomous",
            "workspace": self.workspace,
            "data_dir": self.data_dir,
            "model": "auto",
            "fast_model": "qwen3.5:9b",
            "reasoning_model": "gpt-oss:20b",
            "coding_model": "qwen3-coder:30b",
            "ollama_preload": False,
            "vault_dir": None,
            "memory_embeddings": "disabled",
        }
        fields.update(overrides)
        config = replace(Config.load(), **fields)
        client = ScriptedModelClient(replies)
        agent = Agent(
            config, self.memory, self.events.append, client=client,
            coding_review=False, coding_planning=False,
        )
        return agent, client

    def _kinds(self) -> list[str]:
        return [
            str(row[0]) for row in self.memory.db.execute(
                "SELECT kind FROM memory_spine_events ORDER BY id"
            )
        ]

    def _event_count(self) -> int:
        return int(self.memory.db.execute(
            "SELECT COUNT(*) FROM memory_spine_events"
        ).fetchone()[0])

    def _graph_entity_ids(self) -> dict[tuple[str, str], int]:
        return {
            (str(row["scope"]), str(row["entity_key"])): int(row["id"])
            for row in self.memory.db.execute(
                "SELECT id, scope, entity_key FROM memory_graph_entities"
            )
        }

    def _assert_graph_equivalent(self, erased: set[tuple[str, str]]) -> None:
        """M3 exit test 7.1: the graph is a rebuildable projection of the live
        claim rows, carries exactly one edge per non-excluded claim, keeps no
        entity without an edge, has forgotten every erased key and every entity
        those erases orphaned, and does not renumber a survivor on a rebuild.

        Equivalence is over the design 3.4 tuple; ``label``, ``created_at``
        and the entity ``id`` are deliberately not compared by
        ``rebuild_graph_projection`` (display-only, and ids are allocated and
        never reused), which is why the id check below is a separate
        assertion.
        """
        verification = self.memory.verify_graph()
        self.assertTrue(verification["ok"], verification["problems"][:5])
        dry_run = self.memory.rebuild_graph_projection()
        self.assertTrue(dry_run["ok"], dry_run["divergences"][:5])
        claims = int(self.memory.db.execute(
            "SELECT COUNT(*) FROM memory_claims"
        ).fetchone()[0])
        excluded = sum(int(count) for count in verification["excluded"].values())
        self.assertEqual(int(verification["edges"]), claims - excluded)
        self.assertEqual(
            int(self.memory.db.execute(
                """SELECT COUNT(*) FROM memory_graph_entities AS n
                   WHERE NOT EXISTS (
                       SELECT 1 FROM memory_graph_edges AS e
                       WHERE e.src_entity_id=n.id OR e.dst_entity_id=n.id)"""
            ).fetchone()[0]),
            0,
            "an entity survived with no edge",
        )
        for subject, predicate in sorted(erased):
            key = self.memory._claim_identity(subject, predicate)
            self.assertEqual(
                int(self.memory.db.execute(
                    """SELECT COUNT(*) FROM memory_graph_edges
                       WHERE scope='project:1' AND claim_key=?""",
                    (key,),
                ).fetchone()[0]),
                0,
                f"an erased key kept an edge: {subject} / {predicate}",
            )
        for row in self.memory.db.execute(
            """SELECT payload_json FROM memory_spine_events
               WHERE kind='claim.tombstoned' AND payload_json IS NOT NULL"""
        ).fetchall():
            payload = json.loads(str(row[0]))
            for entity_id in payload.get("removed_entity_ids") or []:
                self.assertIsNone(
                    self.memory.db.execute(
                        "SELECT 1 FROM memory_graph_entities WHERE id=?",
                        (int(entity_id),),
                    ).fetchone(),
                    "a tombstoned entity id came back",
                )
        before = self._graph_entity_ids()
        applied = self.memory.rebuild_graph_projection(apply=True)
        self.assertTrue(applied["ok"], applied)
        self.assertEqual(self._graph_entity_ids(), before)

    def _verified_lesson(self, content: str, *, family: str = "code_fix") -> int:
        """The outcome-provenance chain that writes a lesson through
        ``remember_verified_lesson`` (as ``tests/test_lesson_provenance.py``)."""
        conversation_id = self.memory.new_conversation(f"{family} verified lesson")
        prediction_id = self.memory.record_prediction(
            family=family,
            profile="provenance-test",
            model="deterministic-test",
            predicted_success=0.8,
            predicted_steps=2,
            predicted_verification="tool_success",
            basis="prior",
            origin="interactive",
            conversation_id=conversation_id,
        )
        self.assertTrue(self.memory.resolve_prediction(
            prediction_id, actual_status="complete", actual_steps=2, evidence_ok=True,
        ))
        reflection_id = self.memory.record_reflection(
            status="complete",
            summary="Deterministic provenance fixture outcome.",
            improvements=content,
            conversation_id=conversation_id,
            prediction_id=prediction_id,
            tool_calls=2,
        )
        row = self.memory.db.execute(
            "SELECT id FROM memories WHERE kind='lesson' AND reflection_id=?",
            (reflection_id,),
        ).fetchone()
        self.assertIsNotNone(row, "the reflection did not produce a verified lesson")
        return int(row["id"])

    def _vault(self) -> tuple[Path, Vault]:
        vault_dir = Path(self.temp.name) / "vault"
        vault_dir.mkdir(exist_ok=True)
        self.memory.configure_vault(vault_dir)
        return vault_dir, Vault(vault_dir)

    def _sync_vault(self, **kwargs) -> dict[str, int]:
        assert self.memory.vault is not None
        return self.memory.sync_vault_notes(self.memory.vault.list_notes(), **kwargs)

    def _memory_lineage(self) -> dict[int, tuple[str, int | None]]:
        return {
            int(row["id"]): (str(row["kind"]), row["spine_event_id"])
            for row in self.memory.db.execute(
                "SELECT id, kind, spine_event_id FROM memories ORDER BY id"
            )
        }

    def _spine_memory_state(self) -> dict[int, dict[str, object]]:
        """Replay the memory events independently of ``memory_spine``: the
        latest (origin, eligible, content_digest) the spine implies per id."""
        state: dict[int, dict[str, object]] = {}
        for row in self.memory.db.execute(
            "SELECT kind, subject_id, payload_json FROM memory_spine_events ORDER BY id"
        ):
            if row["payload_json"] is None:
                continue
            payload = json.loads(str(row["payload_json"]))
            kind = str(row["kind"])
            if kind in {
                "memory.imported", "memory.created", "lesson.created",
                "memory.updated", "memory.reasserted",
            }:
                entry = state.setdefault(int(row["subject_id"]), {})
                for name in ("origin", "eligible", "content_digest", "provenance_sha256"):
                    if name in payload:
                        entry[name] = payload[name]
            elif kind == "memory.deleted":
                for memory_id in payload.get("ids") or []:
                    state.pop(int(memory_id), None)
        return state

    def _assert_memory_equivalence(self) -> None:
        """Every non-claim row has lineage, its eligibility equals what the
        spine implies, and its content matches the spine digest; a claim's
        backing row carries the claim's own event."""
        state = self._spine_memory_state()
        rows = self.memory.db.execute(
            """SELECT m.id, m.kind, m.content, m.spine_event_id,
                      omp.eligible AS provenance_eligible, omp.origin AS provenance_origin,
                      c.spine_event_id AS claim_event_id,
                      lp.provenance_sha256 AS lesson_digest
               FROM memories AS m
               LEFT JOIN ordinary_memory_provenance AS omp ON omp.memory_id=m.id
               LEFT JOIN memory_claims AS c ON c.memory_id=m.id
               LEFT JOIN lesson_provenance AS lp ON lp.memory_id=m.id
               ORDER BY m.id"""
        ).fetchall()
        self.assertTrue(rows)
        ordinary_ids: set[int] = set()
        for row in rows:
            self.assertIsNotNone(row["spine_event_id"], dict(row))
            if row["claim_event_id"] is not None:
                self.assertEqual(row["spine_event_id"], row["claim_event_id"], dict(row))
                continue
            memory_id = int(row["id"])
            ordinary_ids.add(memory_id)
            self.assertIn(memory_id, state, f"row {memory_id} has no spine history")
            entry = state[memory_id]
            self.assertEqual(
                entry.get("content_digest"),
                memory_spine.content_digest(self.memory._spine_key, str(row["content"])),
                f"row {memory_id}: content digest",
            )
            if str(row["kind"]) == "lesson":
                # Lessons have no ordinary provenance row; their eligibility
                # is the lesson_provenance chain, verified by digest.
                self.assertIsNone(entry.get("eligible"), dict(row))
                self.assertIsNone(entry.get("origin"), dict(row))
                self.assertIsNotNone(row["lesson_digest"], dict(row))
                self.assertEqual(entry.get("provenance_sha256"), row["lesson_digest"], dict(row))
            else:
                live_eligible = (
                    None if row["provenance_eligible"] is None
                    else bool(int(row["provenance_eligible"]))
                )
                self.assertEqual(entry.get("eligible"), live_eligible, dict(row))
                self.assertEqual(entry.get("origin"), row["provenance_origin"], dict(row))
        self.assertEqual(set(state), ordinary_ids, "spine history without a live row")
        next_memory_id = int(self.memory.db.execute(
            "SELECT next_id FROM memory_id_sequence WHERE id=1"
        ).fetchone()[0])
        highest = int(self.memory.db.execute(
            "SELECT COALESCE(MAX(id), 0) FROM memories"
        ).fetchone()[0])
        self.assertGreater(next_memory_id, highest)


class MemorySpineIntegrationTests(_SpineStoreCase):
    """M2 slice 1 exit test: delete-and-rebuild equivalence on the claim lane,
    lineage on every write path, erase with tombstone + redaction, receipts."""

    # --- schema, key, lineage -------------------------------------------------

    def test_fresh_store_is_at_48_with_a_key_sidecar_and_a_genesis_event(self) -> None:
        self.assertEqual(SCHEMA_VERSION, 50)
        self.assertEqual(
            self.memory.db.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION
        )
        self.assertTrue(Path(str(self.db_path) + memory_spine.KEY_SIDECAR_SUFFIX).exists())
        # Migration 48 projects the (empty) temporal graph and receipts it, so
        # a fresh store's spine is the genesis event plus that one receipt.
        self.assertEqual(self._kinds(), ["spine.genesis", "projection.rebuilt"])
        self.assertTrue(self.memory.verify_spine()["ok"])
        self.assertTrue(self.memory.rebuild_claim_projection()["ok"])
        self.assertTrue(self.memory.rebuild_memory_projection()["ok"])
        self.assertEqual(
            self.memory.db.execute("SELECT next_id FROM memory_id_sequence WHERE id=1").fetchone()[0],
            1,
        )

    def test_every_write_path_leaves_lineage_and_rebuilds(self) -> None:
        conversation = self.memory.new_conversation(project_id=1)
        self.memory.remember_explicit_project_claim(
            conversation, 1, _command("Kestrel relay", "listen port", "8080")
        )
        self.memory.remember_explicit_project_claim(
            conversation, 1, _command("Kestrel relay", "listen port", "9090")
        )
        self.memory.remember_claim(
            "Global node", "release channel", "stable", source="fixture", authority="verified",
            actor="worker", permission="worker",
        )
        # A global reassertion from a different source promotes confidence and
        # source in place: the after-image must reach the spine.
        self.memory.remember_claim(
            "Global node", "release channel", "stable", source="operator note",
            authority="operator", confidence=0.9,
        )
        self.memory.set_preference("theme", "dark", source="user")
        self.memory.retract_explicit_project_claim(
            conversation, 1, _forget("Kestrel relay", "listen port")
        )
        # Slice 2: ordinary memories, a lesson, and the vault re-index.
        self.memory.remember("An unverified aside.")
        self.memory.remember_verified(
            "The build uses ninja.", kind="fact", source="operator", origin="explicit_operator_memory",
        )
        self._verified_lesson("Reuse the measured parser boundary regression.")
        _vault_dir, vault = self._vault()
        vault.write_note("research", "Kestrel notes", "The relay listens on 9090.")
        self._sync_vault()
        unlinked = self.memory.db.execute(
            "SELECT COUNT(*) FROM memory_claims WHERE spine_event_id IS NULL"
        ).fetchone()[0]
        self.assertEqual(unlinked, 0)
        self.assertEqual(
            self.memory.db.execute(
                "SELECT COUNT(*) FROM memories WHERE spine_event_id IS NULL"
            ).fetchone()[0],
            0,
        )
        kinds = self._kinds()
        self.assertEqual(kinds[0], "spine.genesis")
        for kind in (
            "claim.created", "claim.superseded", "claim.reasserted", "claim.retracted",
            "memory.created", "lesson.created",
        ):
            self.assertIn(kind, kinds)
        actors = {
            str(row[0]) for row in self.memory.db.execute(
                "SELECT DISTINCT actor FROM memory_spine_events"
            )
        }
        self.assertEqual(actors, {"system", "operator", "worker", "runtime"})
        # Status events on the project lane carry the operator's context, not
        # a runtime default: the partial that threads it must actually be used.
        project_events = self.memory.db.execute(
            """SELECT kind, actor, conversation_id, permission FROM memory_spine_events
               WHERE scope='project:1' AND kind LIKE 'claim.%' ORDER BY id"""
        ).fetchall()
        self.assertTrue(project_events)
        for event in project_events:
            self.assertEqual(
                (event["actor"], event["conversation_id"], event["permission"]),
                ("operator", conversation, "operator:interactive"),
                dict(event),
            )
        self.assertIn("claim.superseded", [event["kind"] for event in project_events])
        verification = self.memory.verify_spine()
        self.assertTrue(verification["ok"], verification["problems"])
        self.assertTrue(verification["memory_lineage_ok"])
        rebuild = self.memory.rebuild_claim_projection()
        self.assertTrue(rebuild["ok"], rebuild["divergences"])
        self.assertEqual(rebuild["rows_live"], rebuild["rows_rebuilt"])
        self.assertGreaterEqual(rebuild["rows_live"], 4)
        memory_rebuild = self.memory.rebuild_memory_projection()
        self.assertTrue(memory_rebuild["ok"], memory_rebuild["divergences"])
        self._assert_memory_equivalence()

    def test_legacy_store_is_backfilled_on_upgrade(self) -> None:
        conversation = self.memory.new_conversation(project_id=1)
        self.memory.remember_explicit_project_claim(
            conversation, 1, _command("Kestrel relay", "listen port", "9090")
        )
        self.memory.remember_claim(
            "Global node", "release channel", "stable", source="fixture", authority="verified"
        )
        self.memory.remember_verified(
            "The build uses ninja.", source="operator", origin="explicit_operator_memory",
        )
        # Downgrade the copy: drop the spine and its lineage, as a v45 store
        # looks (memories keeps a stale lineage id that names nothing).
        memory_spine.drop_spine_triggers(self.memory.db)
        self.memory.db.execute("DROP TABLE memory_spine_events")
        self.memory.db.execute("DROP TABLE memory_claim_sequence")
        self.memory.db.execute("UPDATE memory_claims SET spine_event_id=NULL")
        self.memory.db.execute("PRAGMA user_version=45")
        self.memory.close()
        self.memory = Memory(self.db_path)
        self.assertEqual(
            self.memory.db.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION
        )
        kinds = self._kinds()
        self.assertEqual(kinds[0], "spine.genesis")
        self.assertEqual(kinds.count("claim.imported"), 2)
        self.assertEqual(kinds.count("memory.imported"), 1)
        for table in ("memory_claims", "memories"):
            self.assertEqual(
                self.memory.db.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE spine_event_id IS NULL"
                ).fetchone()[0],
                0,
                table,
            )
        self.assertTrue(self.memory.verify_spine()["ok"])
        self.assertTrue(self.memory.rebuild_claim_projection()["ok"])
        self.assertTrue(self.memory.rebuild_memory_projection()["ok"])
        self._assert_memory_equivalence()
        # New writes continue the chain with explicit ids after the backfill.
        conversation = self.memory.new_conversation(project_id=1)
        receipt = self.memory.remember_explicit_project_claim(
            conversation, 1, _command("Osprey relay", "listen port", "7070")
        )
        self.assertEqual(receipt["claim_id"], 3)
        self.assertTrue(self.memory.rebuild_claim_projection()["ok"])

    # --- the exit test: randomized operations, then delete-and-rebuild ------

    def test_randomized_history_rebuilds_equivalently(self) -> None:
        rng = random.Random(20260903)
        conversation = self.memory.new_conversation(project_id=1)
        subjects = ["Kestrel relay", "Osprey relay", "Harrier box", "Talon box"]
        predicates = ["listen port", "owner", "datacenter"]
        # Value chains, so the graph has something to traverse: a relay is
        # deployed on a box, a box sits in a datacenter, a datacenter is in a
        # region.  Every value here is also a subject elsewhere, which is the
        # reversed-triple join the graph exists for and the shape most likely
        # to break rebuild equivalence.
        chains = [
            ("Kestrel relay", "deployed on host", ["Harrier box", "Talon box"]),
            ("Osprey relay", "deployed on host", ["Harrier box", "Talon box"]),
            ("Harrier box", "datacenter", ["Fenwick", "Moss Hollow"]),
            ("Talon box", "datacenter", ["Fenwick", "Moss Hollow"]),
            ("Fenwick", "region", ["Northgate", "Southgate"]),
            ("Moss Hollow", "region", ["Northgate", "Southgate"]),
        ]
        erased: set[tuple[str, str]] = set()
        for step in range(120):
            if rng.random() < 0.25:
                subject, predicate, values = rng.choice(chains)
                if rng.random() < 0.8:
                    self.memory.remember_explicit_project_claim(
                        conversation, 1,
                        _command(subject, predicate, rng.choice(values)),
                    )
                    erased.discard((subject, predicate))
                else:
                    # A global reassertion of a chain link, at a differing
                    # authority: the project row must shadow it and the graph
                    # must carry both edges.
                    self.memory.remember_claim(
                        subject, predicate, rng.choice(values),
                        source=rng.choice(["fixture", "scan"]),
                        authority=rng.choice(["learned", "verified", "operator"]),
                        confidence=rng.choice([0.5, 0.9, 1.0]),
                    )
                continue
            subject = rng.choice(subjects)
            predicate = rng.choice(predicates)
            roll = rng.random()
            if roll < 0.55:
                value = f"v{rng.randint(1, 4)}"
                self.memory.remember_explicit_project_claim(
                    conversation, 1, _command(subject, predicate, value)
                )
                erased.discard((subject, predicate))
            elif roll < 0.7:
                self.memory.remember_claim(
                    subject, predicate, f"g{rng.randint(1, 3)}",
                    source=rng.choice(["fixture", "scan", "operator note"]),
                    authority=rng.choice(["external", "learned", "verified", "operator"]),
                    confidence=rng.choice([0.4, 0.7, 1.0]),
                )
            elif roll < 0.85:
                self.memory.retract_explicit_project_claim(
                    conversation, 1, _forget(subject, predicate)
                )
            else:
                self.memory.erase_explicit_project_claim(
                    conversation, 1, _erase(subject, predicate)
                )
                erased.add((subject, predicate))
        verification = self.memory.verify_spine()
        self.assertTrue(verification["ok"], verification["problems"])
        rebuild = self.memory.rebuild_claim_projection()
        self.assertTrue(rebuild["ok"], rebuild["divergences"][:5])
        self.assertEqual(rebuild["rows_live"], rebuild["rows_rebuilt"])
        # Every erased key is absent from the live projection and present as a
        # tombstone; every tombstoned id stays retired.
        for subject, predicate in erased:
            key = self.memory._claim_identity(subject, predicate)
            live = self.memory.db.execute(
                "SELECT COUNT(*) FROM memory_claims WHERE scope='project:1' AND claim_key=?",
                (key,),
            ).fetchone()[0]
            self.assertEqual(live, 0)
        tombstones = self.memory.db.execute(
            "SELECT COUNT(*) FROM memory_spine_events WHERE kind='claim.tombstoned'"
        ).fetchone()[0]
        self.assertGreaterEqual(tombstones, 1)
        self.assertTrue(verification["graph_ok"], verification)
        self._assert_graph_equivalent(erased)
        # The chains really were built, so the equivalence above is not
        # vacuous: several edges point at a value that is also a subject.
        joinable = int(self.memory.db.execute(
            """SELECT COUNT(*) FROM memory_graph_edges AS e
               JOIN memory_graph_entities AS destination
                 ON destination.id=e.dst_entity_id
               WHERE e.value_kind='entity'
                 AND EXISTS (
                     SELECT 1 FROM memory_graph_edges AS out_edge
                     WHERE out_edge.src_entity_id=destination.id)"""
        ).fetchone()[0])
        self.assertGreater(joinable, 0)

    # --- erase ---------------------------------------------------------------

    def test_erase_removes_every_trace_except_the_transcript_and_says_so(self) -> None:
        agent, client = self._agent(["ok"])
        stored = agent.run(_command("Kestrel relay", "listen port", "8080"))
        agent.run(_command("Kestrel relay", "listen port", "9090"), conversation_id=stored.conversation_id)
        agent.run(_command("Kestrel relay", "owner", "Dana"), conversation_id=stored.conversation_id)
        result = agent.run(_erase("Kestrel relay", "listen port"), conversation_id=stored.conversation_id)

        text = str(result)
        self.assertEqual(result.status, "complete", result.reason)
        self.assertEqual(client.requests, [])
        self.assertRegex(text, r"Erased project fact \(2 versions removed; tombstone #\d+\)\.")
        self.assertIn("transcript cop", text)
        self.assertNotIn("9090", text)
        self.assertIn("governed project memory - erased", self.events)
        claims = [tuple(row) for row in self.memory.db.execute(
            "SELECT subject, predicate, value FROM memory_claims ORDER BY id"
        )]
        self.assertEqual(claims, [("Kestrel relay", "owner", "Dana")])
        # No trace of the erased values in any memory-side table or spine payload.
        for table, column in (
            ("memories", "content"), ("memory_spine_events", "payload_json"),
        ):
            dump = " ".join(
                str(row[0] or "") for row in self.memory.db.execute(f"SELECT {column} FROM {table}")
            )
            self.assertNotIn("9090", dump, table)
            self.assertNotIn("8080", dump, table)
        self.assertEqual(
            self.memory.db.execute("SELECT COUNT(*) FROM memory_claim_events WHERE claim_id IN (1, 2)").fetchone()[0],
            0,
        )
        # The tombstone names the backing rows it removed, so their ids stay
        # retired and the memory rebuild never expects them.
        tombstone = json.loads(str(self.memory.db.execute(
            "SELECT payload_json FROM memory_spine_events WHERE kind='claim.tombstoned'"
        ).fetchone()[0]))
        self.assertEqual(len(tombstone["removed_memory_ids"]), 2)
        for memory_id in tombstone["removed_memory_ids"]:
            self.assertIsNone(self.memory.db.execute(
                "SELECT 1 FROM memories WHERE id=?", (memory_id,)
            ).fetchone())
        verification = self.memory.verify_spine()
        self.assertTrue(verification["ok"], verification["problems"])
        self.assertGreaterEqual(verification["redacted"], 3)
        self.assertTrue(self.memory.rebuild_claim_projection()["ok"])
        self.assertTrue(self.memory.rebuild_memory_projection()["ok"])
        # The transcript still holds the operator's own commands (reported, not hidden).
        transcript = " ".join(
            str(row[0]) for row in self.memory.db.execute("SELECT content FROM messages")
        )
        self.assertIn("9090", transcript)
        # Erasing again is a no-op with a receipt; a new fact for the key gets a fresh id.
        again = agent.run(_erase("Kestrel relay", "listen port"), conversation_id=stored.conversation_id)
        self.assertIn("No project fact matches", str(again))
        fresh = agent.run(_command("Kestrel relay", "listen port", "9191"), conversation_id=stored.conversation_id)
        self.assertIn("Stored project fact (claim record #4)", str(fresh))

    def test_erase_after_a_confirmed_proposal_removes_the_proposal_text(self) -> None:
        agent, client = self._agent(["Understood."])
        stored = agent.run(_command("Kestrel relay", "listen port", "9090"))
        agent.run(
            "By the way, the Kestrel relay now listens on port 9191, not 9090.",
            conversation_id=stored.conversation_id,
        )
        confirmed = agent.run("store it", conversation_id=stored.conversation_id)
        self.assertIn("Updated project fact", str(confirmed))
        result = agent.run(_erase("Kestrel relay", "listen port"), conversation_id=stored.conversation_id)
        self.assertRegex(str(result), r"Erased project fact \(2 versions removed; tombstone #\d+\)")
        rows = self.memory.db.execute(
            "SELECT command, claim_id, status FROM memory_fact_proposals"
        ).fetchall()
        self.assertEqual([tuple(row) for row in rows], [("[erased project fact]", None, "confirmed")])
        dump = " ".join(
            str(row[0] or "") for row in self.memory.db.execute(
                "SELECT command FROM memory_fact_proposals"
            )
        )
        self.assertNotIn("9191", dump)
        self.assertEqual(len(client.requests), 1)
        self.assertTrue(self.memory.verify_spine()["ok"])
        # The FTS index no longer carries the erased tokens.
        remaining = self.memory.db.execute(
            "SELECT COUNT(*) FROM memory_fts WHERE memory_fts MATCH '9191'"
        ).fetchone()[0]
        self.assertEqual(remaining, 0)

    def test_erased_values_are_not_recoverable_from_digests(self) -> None:
        agent, _client = self._agent(["Understood."])
        stored = agent.run(_command("Kestrel relay", "listen port", "47391"))
        agent.run(
            "By the way, the Kestrel relay now listens on port 47392, not 47391.",
            conversation_id=stored.conversation_id,
        )
        agent.run("store it", conversation_id=stored.conversation_id)
        command = _command("Kestrel relay", "listen port", "47392")
        unsalted = hashlib.sha256(command.encode("utf-8")).hexdigest()
        agent.run(_erase("Kestrel relay", "listen port"), conversation_id=stored.conversation_id)
        # No unsalted digest of the command anywhere, and the proposal receipts
        # are redacted by the tombstone like the claim events.
        dump = " ".join(
            f"{row[0]}|{row[1]}" for row in self.memory.db.execute(
                "SELECT command_sha256, command_salt FROM memory_fact_proposals"
            )
        )
        self.assertNotIn(unsalted, dump)
        self.assertEqual(
            [tuple(row) for row in self.memory.db.execute(
                "SELECT command_sha256, command_salt FROM memory_fact_proposals"
            )],
            [("0" * 64, None)],
        )
        proposal_rows = self.memory.db.execute(
            "SELECT payload_json, redacted_by_event_id FROM memory_spine_events WHERE kind LIKE 'proposal.%'"
        ).fetchall()
        self.assertTrue(proposal_rows)
        for row in proposal_rows:
            self.assertIsNone(row["payload_json"])
            self.assertIsNotNone(row["redacted_by_event_id"])
        whole = " ".join(
            str(row[0] or "") for row in self.memory.db.execute(
                "SELECT payload_json FROM memory_spine_events"
            )
        )
        self.assertNotIn(unsalted, whole)
        verification = self.memory.verify_spine()
        self.assertTrue(verification["ok"], verification["problems"])
        self.assertTrue(self.memory.rebuild_claim_projection()["ok"])

    def test_store_refuses_to_open_or_append_without_its_key(self) -> None:
        conversation = self.memory.new_conversation(project_id=1)
        self.memory.remember_explicit_project_claim(
            conversation, 1, _command("Kestrel relay", "listen port", "9090")
        )
        self.memory.close()
        sidecar = Path(str(self.db_path) + memory_spine.KEY_SIDECAR_SUFFIX)
        original = sidecar.read_bytes()
        sidecar.unlink()
        with self.assertRaises(RuntimeError):
            Memory(self.db_path)
        # A different key opens the store (the file is readable) but verify
        # says "key mismatch", not "tampered", and appends are refused.
        sidecar.write_bytes(memory_spine.load_spine_key(None).hex().encode("ascii"))
        other = Memory(self.db_path)
        try:
            verification = other.verify_spine()
            self.assertFalse(verification["ok"])
            self.assertFalse(verification["key_ok"])
            conversation = other.new_conversation(project_id=1)
            with self.assertRaises(RuntimeError):
                other.remember_explicit_project_claim(
                    conversation, 1, _command("Osprey relay", "listen port", "7070")
                )
            with self.assertRaises(RuntimeError):
                other.remember("An aside under the wrong key.")
        finally:
            other.close()
        sidecar.write_bytes(original)
        self.memory = Memory(self.db_path)
        self.assertTrue(self.memory.verify_spine()["ok"])

    def test_schema_downgrade_cannot_launder_tampered_claims(self) -> None:
        conversation = self.memory.new_conversation(project_id=1)
        self.memory.remember_explicit_project_claim(
            conversation, 1, _command("Kestrel relay", "listen port", "9090")
        )
        self.memory.close()
        raw = sqlite3.connect(str(self.db_path))
        raw.execute("UPDATE memory_claims SET value='TAMPERED'")
        raw.execute("PRAGMA user_version=45")
        raw.commit()
        raw.close()
        with self.assertRaises(RuntimeError):
            Memory(self.db_path)
        # Restoring the version reopens the store; the tamper is still visible.
        raw = sqlite3.connect(str(self.db_path))
        raw.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        raw.commit()
        raw.close()
        self.memory = Memory(self.db_path)
        report = self.memory.rebuild_claim_projection()
        self.assertFalse(report["ok"])
        self.assertTrue(any("value" in item["detail"] for item in report["divergences"]))

    def test_receipt_counts_goal_copies_and_secure_delete_is_on(self) -> None:
        self.assertEqual(self.memory.db.execute("PRAGMA secure_delete").fetchone()[0], 1)
        agent, _client = self._agent(["ok", "ok"])
        stored = agent.run(_command("Kestrel relay", "listen port", "47391"))
        agent.run("Please remember that 47391 matters to me.", conversation_id=stored.conversation_id)
        result = agent.run(_erase("Kestrel relay", "listen port"), conversation_id=stored.conversation_id)
        messages = self.memory.db.execute(
            "SELECT COUNT(*) FROM messages WHERE instr(content, '47391') > 0"
        ).fetchone()[0]
        goals = self.memory.db.execute(
            "SELECT COUNT(*) FROM conversation_goals WHERE instr(goal_text, '47391') > 0 OR instr(COALESCE(last_result_summary,''), '47391') > 0"
        ).fetchone()[0]
        self.assertGreaterEqual(goals, 1)
        self.assertIn(f"{messages + goals} transcript cop", str(result))

    def test_notes_without_a_proposal_are_receipted_with_a_variant(self) -> None:
        agent, _client = self._agent(["Done. This has been recorded in memory."])
        agent.run("Thanks, that helps a lot.")
        readonly_agent, _client = self._agent(["Noted."], autonomy="readonly")
        readonly_agent.run("By the way, the Kestrel relay now listens on port 9191.")
        rows = self.memory.db.execute(
            "SELECT outcome, payload_json FROM memory_spine_events WHERE kind='proposal.not_stored' ORDER BY id"
        ).fetchall()
        variants = [(row["outcome"], json.loads(str(row["payload_json"])).get("variant")) for row in rows]
        self.assertEqual(variants, [("noop", "fabricated"), ("noop", "readonly")])

    def test_confirmation_receipt_points_at_the_shown_proposal(self) -> None:
        agent, _client = self._agent(["Understood."])
        stored = agent.run(_command("Kestrel relay", "listen port", "9090"))
        agent.run(
            "By the way, the Kestrel relay now listens on port 9191, not 9090.",
            conversation_id=stored.conversation_id,
        )
        agent.run("store it", conversation_id=stored.conversation_id)
        shown = self.memory.db.execute(
            "SELECT id FROM memory_spine_events WHERE kind='proposal.not_stored' ORDER BY id DESC LIMIT 1"
        ).fetchone()[0]
        confirmed = self.memory.db.execute(
            "SELECT parent_event_id, permission, payload_json FROM memory_spine_events WHERE kind='proposal.confirmed'"
        ).fetchone()
        self.assertEqual(confirmed["parent_event_id"], shown)
        self.assertEqual(confirmed["permission"], "autonomous:interactive")
        linked = self.memory.db.execute(
            "SELECT spine_event_id FROM memory_fact_proposals ORDER BY id DESC LIMIT 1"
        ).fetchone()[0]
        self.assertEqual(linked, shown)
        payload = json.loads(str(confirmed["payload_json"]))
        self.assertEqual(payload["claim_key"], self.memory.claim_key_for("Kestrel relay", "listen port"))
        self.assertEqual(
            payload["command_sha256"],
            self.memory.db.execute("SELECT command_sha256 FROM memory_fact_proposals ORDER BY id DESC LIMIT 1").fetchone()[0],
        )

    def test_erase_wrappers_fail_closed_and_readonly_is_refused(self) -> None:
        agent, client = self._agent(["ok"])
        stored = agent.run(_command("Kestrel relay", "listen port", "9090"))
        malformed = agent.run("Erase this project fact: not-json", conversation_id=stored.conversation_id)
        self.assertEqual(malformed.status, "incomplete")
        self.assertIn("Not erased:", str(malformed))
        wrapper = agent.run(
            'erase the project fact: {"subject":"Kestrel relay","predicate":"listen port"}',
            conversation_id=stored.conversation_id,
        )
        self.assertEqual(wrapper.status, "incomplete")
        self.assertIn("retraction or erasure", str(wrapper))
        self.assertTrue(str(wrapper).startswith("Not erased:"), str(wrapper))
        self.assertEqual(client.requests, [])
        readonly_agent, _client = self._agent(["ok"], autonomy="readonly")
        refused = readonly_agent.run(_erase("Kestrel relay", "listen port"), conversation_id=stored.conversation_id)
        self.assertIn("Not erased: Durable memory writes are disabled in readonly mode", str(refused))
        self.assertEqual(
            self.memory.db.execute("SELECT COUNT(*) FROM memory_claims").fetchone()[0], 1
        )
        self.assertIn("proposal.not_stored", self._kinds())

    # --- receipts on the spine ---------------------------------------------------

    def test_proposal_and_confirmation_receipts_reach_the_spine(self) -> None:
        agent, _client = self._agent(["Understood."])
        stored = agent.run(_command("Kestrel relay", "listen port", "9090"))
        agent.run(
            "By the way, the Kestrel relay now listens on port 9191, not 9090.",
            conversation_id=stored.conversation_id,
        )
        agent.run("store it", conversation_id=stored.conversation_id)
        kinds = self._kinds()
        self.assertIn("proposal.not_stored", kinds)
        self.assertIn("proposal.confirmed", kinds)
        rows = self.memory.db.execute(
            "SELECT kind, payload_json FROM memory_spine_events WHERE kind LIKE 'proposal.%' ORDER BY id"
        ).fetchall()
        for row in rows:
            payload = json.loads(str(row["payload_json"]))
            self.assertNotIn("9191", json.dumps(payload))
            self.assertEqual(len(payload["command_sha256"]), 64)
        self.assertTrue(self.memory.verify_spine()["ok"])

    def test_conversation_deletion_is_receipted(self) -> None:
        conversation = self.memory.new_conversation(project_id=1)
        self.memory.add_message(conversation, "user", "hello")
        self.memory.add_message(conversation, "assistant", "hi")
        self.memory.delete_conversation(conversation)
        row = self.memory.db.execute(
            "SELECT payload_json, subject_id FROM memory_spine_events WHERE kind='conversation.deleted'"
        ).fetchone()
        # Schema 50: the receipt also names what compaction records went with
        # the transcript.  Counts, not lists, so no cap is needed (M-10); zero
        # here because this conversation was never compacted, and reported
        # rather than omitted so "none removed" and "not checked" stay
        # distinguishable at the payload level.
        self.assertEqual(
            json.loads(str(row["payload_json"])),
            {"messages_removed": 2, "milestones_removed": 0, "spans_removed": 0},
        )
        self.assertEqual(row["subject_id"], conversation)
        self.assertTrue(self.memory.verify_spine()["ok"])

    # --- tamper and the CLI ------------------------------------------------------

    def test_tamper_is_detected_and_the_cli_reports_it(self) -> None:
        conversation = self.memory.new_conversation(project_id=1)
        self.memory.remember_explicit_project_claim(
            conversation, 1, _command("Kestrel relay", "listen port", "9090")
        )
        with patch("jarvis.cli.Config.load", return_value=replace(
            Config.load(), data_dir=self.data_dir, workspace=self.workspace, vault_dir=None
        )):
            self.memory.close()
            output = io.StringIO()
            with redirect_stdout(output):
                code = _run_spine(type("Args", (), {"spine_command": "verify", "json": False})())
            self.assertEqual(code, 0)
            self.assertIn("Memory spine OK", output.getvalue())
            output = io.StringIO()
            with redirect_stdout(output):
                code = _run_spine(type("Args", (), {"spine_command": "tail", "json": False, "limit": 5})())
            self.assertEqual(code, 0)
            self.assertIn("claim.created", output.getvalue())
            self.assertNotIn("9090", output.getvalue())
            # Out-of-band edit with the triggers dropped: verify and rebuild both report it.
            raw = sqlite3.connect(str(self.db_path))
            raw.execute("DROP TRIGGER memory_spine_events_redaction_only")
            raw.execute("UPDATE memory_spine_events SET actor='model' WHERE kind='claim.created'")
            raw.execute("UPDATE memory_claims SET value='9999'")
            raw.commit()
            raw.close()
            output = io.StringIO()
            with redirect_stdout(output):
                code = _run_spine(type("Args", (), {"spine_command": "verify", "json": False})())
            self.assertEqual(code, 1)
            self.assertIn("keyed digest mismatch", output.getvalue())
            output = io.StringIO()
            with redirect_stdout(output):
                code = _run_spine(type("Args", (), {"spine_command": "rebuild-claims", "json": False})())
            self.assertEqual(code, 1)
            self.assertIn("DIVERGENT", output.getvalue())
            self.assertIn("value", output.getvalue())
            self.memory = Memory(self.db_path)


class MemorySpineSliceTwoTests(_SpineStoreCase):
    """M2 slice 2 exit tests (design 12.4 as amended by 12.6): ordinary
    memories, lessons, and the vault re-index on the spine; the apply step;
    migration 46 -> 47 -> 48; actor mappings; subject history."""

    # --- (a) randomized history over every writer ------------------------------

    def test_randomized_history_over_every_writer_rebuilds_equivalently(self) -> None:
        rng = random.Random(20260904)
        conversation = self.memory.new_conversation(project_id=1)
        _vault_dir, vault = self._vault()
        subjects = ["Kestrel relay", "Osprey relay", "Harrier box"]
        predicates = ["listen port", "owner"]
        notes_pool = ["alpha", "beta", "gamma", "delta"]
        note_paths: dict[str, Path] = {}
        aside_pool = [f"Aside {index} about the relay fleet." for index in range(6)]
        erased: set[tuple[str, str]] = set()
        counts: dict[str, int] = {}
        for step in range(60):
            roll = rng.random()
            if roll < 0.15:
                counts["remember"] = counts.get("remember", 0) + 1
                self.memory.remember(rng.choice(aside_pool), actor="model",
                                     permission="autonomous:interactive:explicit_memory_write",
                                     conversation_id=conversation)
            elif roll < 0.30:
                counts["remember_verified"] = counts.get("remember_verified", 0) + 1
                self.memory.remember_verified(
                    rng.choice(aside_pool), source=None, origin="explicit_operator_memory",
                )
            elif roll < 0.38:
                counts["lesson"] = counts.get("lesson", 0) + 1
                self._verified_lesson(f"Lesson {step}: reuse the measured parser boundary regression.")
            elif roll < 0.55:
                counts["vault"] = counts.get("vault", 0) + 1
                name = rng.choice(notes_pool)
                action = rng.random()
                if name not in note_paths or action < 0.5:
                    path = vault.write_note("research", f"{name} notes", f"Body of {name} at step {step}.")
                    assert path is not None
                    note_paths[name] = path
                elif action < 0.8:
                    path = note_paths[name]
                    path.write_text(
                        path.read_text(encoding="utf-8").replace("Body of", f"Revised {step} body of"),
                        encoding="utf-8",
                    )
                else:
                    note_paths.pop(name).unlink()
                self._sync_vault(
                    **(
                        {"actor": "operator", "permission": "operator:interactive"}
                        if rng.random() < 0.3 else {}
                    )
                )
            elif roll < 0.75:
                counts["claim"] = counts.get("claim", 0) + 1
                subject, predicate = rng.choice(subjects), rng.choice(predicates)
                self.memory.remember_explicit_project_claim(
                    conversation, 1, _command(subject, predicate, f"v{rng.randint(1, 3)}")
                )
                erased.discard((subject, predicate))
            elif roll < 0.85:
                counts["global"] = counts.get("global", 0) + 1
                self.memory.remember_claim(
                    rng.choice(subjects), rng.choice(predicates), f"g{rng.randint(1, 2)}",
                    source=rng.choice(["fixture", "scan"]),
                    authority=rng.choice(["learned", "verified", "operator"]),
                )
            elif roll < 0.93:
                counts["retract"] = counts.get("retract", 0) + 1
                self.memory.retract_explicit_project_claim(
                    conversation, 1, _forget(rng.choice(subjects), rng.choice(predicates))
                )
            else:
                counts["erase"] = counts.get("erase", 0) + 1
                subject, predicate = rng.choice(subjects), rng.choice(predicates)
                self.memory.erase_explicit_project_claim(
                    conversation, 1, _erase(subject, predicate)
                )
                erased.add((subject, predicate))
        # The seed exercised every writer at least once.
        for name in ("remember", "remember_verified", "lesson", "vault", "claim", "global", "retract", "erase"):
            self.assertGreater(counts.get(name, 0), 0, counts)
        kinds = set(self._kinds())
        for kind in ("memory.created", "memory.reasserted", "memory.updated", "memory.deleted",
                     "lesson.created", "claim.created", "claim.retracted", "claim.tombstoned"):
            self.assertIn(kind, kinds)
        verification = self.memory.verify_spine()
        self.assertTrue(verification["ok"], verification["problems"])
        self.assertTrue(verification["chain_ok"])
        self.assertTrue(verification["memory_lineage_ok"])
        self.assertTrue(verification["memory_sequence_ok"])
        memory_rebuild = self.memory.rebuild_memory_projection()
        self.assertTrue(memory_rebuild["ok"], memory_rebuild["divergences"][:5])
        self.assertEqual(memory_rebuild["rows_live"], memory_rebuild["rows_rebuilt"])
        claim_rebuild = self.memory.rebuild_claim_projection()
        self.assertTrue(claim_rebuild["ok"], claim_rebuild["divergences"][:5])
        self.assertEqual(claim_rebuild["rows_live"], claim_rebuild["rows_rebuilt"])
        self._assert_memory_equivalence()
        for subject, predicate in erased:
            key = self.memory._claim_identity(subject, predicate)
            self.assertEqual(self.memory.db.execute(
                "SELECT COUNT(*) FROM memory_claims WHERE scope='project:1' AND claim_key=?", (key,)
            ).fetchone()[0], 0)
        # Digest-only payloads: no memory content or vault text on the spine.
        dump = " ".join(
            str(row[0] or "") for row in self.memory.db.execute(
                "SELECT payload_json FROM memory_spine_events WHERE kind LIKE 'memory.%' OR kind LIKE 'lesson.%'"
            )
        )
        self.assertNotIn("Aside ", dump)
        self.assertNotIn("Body of", dump)
        self.assertNotIn("parser boundary", dump)
        self.assertTrue(verification["graph_ok"], verification)
        self._assert_graph_equivalent(erased)

    def test_duplicate_writes_are_receipted_and_never_downgrade_eligibility(self) -> None:
        self.memory.remember("The relay fleet is blue.")
        self.memory.remember("The relay fleet is blue.")
        self.memory.remember_verified(
            "The relay fleet is blue.", source=None, origin="explicit_operator_memory",
        )
        self.memory.remember("The relay fleet is blue.")
        rows = self.memory.db.execute(
            """SELECT kind, outcome, payload_json FROM memory_spine_events
               WHERE subject_kind='memory' ORDER BY id"""
        ).fetchall()
        shape = [
            (row["kind"], row["outcome"], json.loads(str(row["payload_json"]))["eligible"])
            for row in rows
        ]
        self.assertEqual(shape, [
            ("memory.created", "applied", False),
            ("memory.reasserted", "noop", False),
            ("memory.reasserted", "applied", True),
            ("memory.reasserted", "noop", True),
        ])
        self.assertEqual(
            self.memory.db.execute("SELECT COUNT(*) FROM memories").fetchone()[0], 1
        )
        self.assertEqual(
            self.memory.db.execute("SELECT eligible FROM ordinary_memory_provenance").fetchone()[0], 1
        )
        self.assertTrue(self.memory.rebuild_memory_projection()["ok"])
        self._assert_memory_equivalence()

    def test_out_of_band_memory_edit_is_reported_by_the_memory_rebuild(self) -> None:
        self.memory.remember_verified("The build uses ninja.", source="operator", origin="explicit_operator_memory")
        self.memory.db.execute("UPDATE memories SET content='The build uses make.' WHERE kind='fact'")
        report = self.memory.rebuild_memory_projection()
        self.assertFalse(report["ok"])
        self.assertTrue(report["divergences"])
        dump = json.dumps(report["divergences"])
        self.assertNotIn("ninja", dump)
        self.assertNotIn("make", dump)

    # --- (b) apply ------------------------------------------------------------------

    def _plant_three_divergences(self) -> tuple[int, int, int, int]:
        """Edit one claim, plant one claim row with no history, delete one
        claim row out of band (raw sqlite, foreign keys off, sequences bumped
        so verify's floor check stays happy)."""
        conversation = self.memory.new_conversation(project_id=1)
        self.memory.remember_explicit_project_claim(
            conversation, 1, _command("Kestrel relay", "listen port", "8080")
        )
        self.memory.remember_explicit_project_claim(
            conversation, 1, _command("Kestrel relay", "listen port", "9090")
        )
        self.memory.remember_explicit_project_claim(
            conversation, 1, _command("Kestrel relay", "owner", "Dana")
        )
        self.memory.remember_claim(
            "Global node", "release channel", "stable", source="fixture", authority="verified"
        )
        deleted = 1  # the superseded first version; its dependents stay behind
        edited = 3   # the owner fact
        rogue_key = self.memory.claim_key_for("Rogue node", "listen port")
        self.memory.close()
        stamp = now_iso()
        raw = sqlite3.connect(str(self.db_path))
        raw.execute("UPDATE memory_claims SET value='Eve' WHERE id=?", (edited,))
        raw.execute("DROP TRIGGER memory_claims_require_spine_event")
        raw.execute("DROP TRIGGER memories_require_spine_event")
        memory_next = int(raw.execute("SELECT next_id FROM memory_id_sequence WHERE id=1").fetchone()[0])
        claim_next = int(raw.execute("SELECT next_id FROM memory_claim_sequence WHERE id=1").fetchone()[0])
        raw.execute(
            """INSERT INTO memories(id, created_at, kind, content, source)
               VALUES (?, ?, 'claim', 'Rogue node listen port: 1', 'operator:planted')""",
            (memory_next, stamp),
        )
        raw.execute(
            """INSERT INTO memory_claims(
                   id, memory_id, created_at, updated_at, scope, claim_key, subject,
                   predicate, value, value_sha256, source, authority, confidence,
                   status, valid_from, valid_until, supersedes_id, spine_event_id
               ) VALUES (?, ?, ?, ?, 'global', ?, 'Rogue node', 'listen port', '1', ?,
                         'planted', 'operator', 1.0, 'active', ?, NULL, NULL, NULL)""",
            (claim_next, memory_next, stamp, stamp, rogue_key,
             hashlib.sha256(b"1").hexdigest(), stamp),
        )
        raw.execute("UPDATE memory_id_sequence SET next_id=next_id+1 WHERE id=1")
        raw.execute("UPDATE memory_claim_sequence SET next_id=next_id+1 WHERE id=1")
        memory_spine.create_spine_triggers(raw)
        raw.execute("DELETE FROM memory_claims WHERE id=?", (deleted,))
        raw.commit()
        raw.close()
        self.memory = Memory(self.db_path)
        return edited, claim_next, deleted, memory_next

    def test_apply_reconciles_three_out_of_band_divergences(self) -> None:
        edited, planted, deleted, planted_memory = self._plant_three_divergences()
        dry = self.memory.rebuild_claim_projection()
        self.assertFalse(dry["ok"])
        by_kind = {
            item["kind"]: item["claim_id"] for item in dry["divergences"]
            if item["kind"] in {"field", "missing_in_rebuild", "missing_in_live"}
        }
        self.assertEqual(
            by_kind,
            {"field": edited, "missing_in_rebuild": planted, "missing_in_live": deleted},
            dry["divergences"],
        )
        self.assertEqual(
            sum(1 for item in dry["divergences"] if item["kind"] in by_kind), 3
        )
        # The planted row is a lineage failure, not a chain failure.
        verification = self.memory.verify_spine()
        self.assertFalse(verification["ok"])
        self.assertTrue(verification["chain_ok"], verification["problems"])
        events_before = self._event_count()
        report = self.memory.rebuild_claim_projection(apply=True)
        self.assertTrue(report["ok"], report)
        self.assertTrue(report["applied"])
        self.assertIsNone(report["refusal"])
        self.assertEqual(report["removed_ids"], [planted])
        self.assertEqual(report["recreated_ids"], [deleted])
        self.assertEqual(report["updated_ids"], [edited])
        self.assertEqual(report["divergences"], [])
        self.assertEqual(report["rows_before"], 4)
        self.assertEqual(report["rows_after"], 4)
        self.assertEqual(report["event_id"], events_before + 1)
        self.assertEqual(self._kinds()[-1], "projection.rebuilt")
        self.assertEqual(
            self.memory.db.execute("SELECT value FROM memory_claims WHERE id=?", (edited,)).fetchone()[0],
            "Dana",
        )
        self.assertIsNone(self.memory.db.execute(
            "SELECT 1 FROM memory_claims WHERE id=?", (planted,)
        ).fetchone())
        self.assertIsNone(self.memory.db.execute(
            "SELECT 1 FROM memories WHERE id=?", (planted_memory,)
        ).fetchone())
        recreated = self.memory.db.execute(
            "SELECT status, value, spine_event_id FROM memory_claims WHERE id=?", (deleted,)
        ).fetchone()
        self.assertEqual((recreated["status"], recreated["value"]), ("superseded", "8080"))
        self.assertIsNotNone(recreated["spine_event_id"])
        self.assertGreaterEqual(self.memory.db.execute(
            "SELECT COUNT(*) FROM memory_claim_events WHERE claim_id=?", (deleted,)
        ).fetchone()[0], 2)
        clean = self.memory.rebuild_claim_projection()
        self.assertTrue(clean["ok"], clean["divergences"])
        verification = self.memory.verify_spine()
        self.assertTrue(verification["ok"], verification["problems"])
        self.assertTrue(self.memory.rebuild_memory_projection()["ok"])
        # A second apply has nothing to do and appends nothing.
        again = self.memory.rebuild_claim_projection(apply=True)
        self.assertTrue(again["ok"])
        self.assertFalse(again["applied"])
        self.assertIsNone(again["event_id"])
        self.assertEqual(self._event_count(), events_before + 1)

    def test_apply_refuses_on_a_spine_that_fails_verification(self) -> None:
        conversation = self.memory.new_conversation(project_id=1)
        self.memory.remember_explicit_project_claim(
            conversation, 1, _command("Kestrel relay", "listen port", "8080")
        )
        self.memory.close()
        raw = sqlite3.connect(str(self.db_path))
        raw.execute("DROP TRIGGER memory_spine_events_redaction_only")
        raw.execute("UPDATE memory_spine_events SET actor='model' WHERE kind='claim.created'")
        raw.execute("UPDATE memory_claims SET value='9999'")
        raw.commit()
        raw.close()
        self.memory = Memory(self.db_path)
        snapshot = (
            [tuple(row) for row in self.memory.db.execute("SELECT * FROM memory_claims ORDER BY id")],
            [tuple(row) for row in self.memory.db.execute("SELECT * FROM memory_spine_head")],
            self._event_count(),
        )
        report = self.memory.rebuild_claim_projection(apply=True)
        self.assertFalse(report["ok"])
        self.assertFalse(report["applied"])
        self.assertEqual(report["refusal"], "verify_failed")
        self.assertFalse(report["verification"]["chain_ok"])
        self.assertTrue(report["divergences"])
        self.assertEqual(
            (
                [tuple(row) for row in self.memory.db.execute("SELECT * FROM memory_claims ORDER BY id")],
                [tuple(row) for row in self.memory.db.execute("SELECT * FROM memory_spine_head")],
                self._event_count(),
            ),
            snapshot,
        )
        self.assertFalse(self.memory.db.in_transaction)

    def test_apply_on_a_clean_store_changes_nothing(self) -> None:
        conversation = self.memory.new_conversation(project_id=1)
        self.memory.remember_explicit_project_claim(
            conversation, 1, _command("Kestrel relay", "listen port", "8080")
        )
        before = self._event_count()
        report = self.memory.rebuild_claim_projection(apply=True)
        self.assertTrue(report["ok"])
        self.assertFalse(report["applied"])
        self.assertIsNone(report["refusal"])
        self.assertIsNone(report["event_id"])
        self.assertEqual(report["rows_before"], 1)
        self.assertEqual(report["rows_after"], 1)
        self.assertEqual(self._event_count(), before)
        # Only migration 48's receipt; this apply appended none of its own.
        self.assertEqual(self._kinds().count("projection.rebuilt"), 1)

    # --- (c) migration 46 -> 47 -> 48 ---------------------------------------------

    def _populate_for_migration(self) -> None:
        self.memory.remember("An unverified aside about the fleet.")
        self.memory.remember_verified(
            "The build uses ninja.", source="operator", origin="explicit_operator_memory",
        )
        self._verified_lesson("Reuse the measured parser boundary regression.")
        conversation = self.memory.new_conversation(project_id=1)
        self.memory.remember_explicit_project_claim(
            conversation, 1, _command("Kestrel relay", "listen port", "8080")
        )
        self.memory.remember_explicit_project_claim(
            conversation, 1, _command("Kestrel relay", "listen port", "9090")
        )
        self.memory.remember_claim(
            "Global node", "release channel", "stable", source="fixture", authority="verified"
        )

    def _downgrade_to_46(self, *, drop_column: bool) -> None:
        raw = sqlite3.connect(str(self.db_path))
        if drop_column:
            raw.execute("DROP TRIGGER memories_require_spine_event")
            raw.execute("DROP INDEX idx_memories_spine_event")
            raw.execute("ALTER TABLE memories DROP COLUMN spine_event_id")
            raw.execute("DROP TABLE memory_id_sequence")
        raw.execute("PRAGMA user_version=46")
        raw.commit()
        raw.close()

    def test_migration_46_to_47_relinks_existing_events_idempotently(self) -> None:
        self._populate_for_migration()
        lineage_before = self._memory_lineage()
        events_before = self._event_count()
        claim_events = {
            int(row["memory_id"]): int(row["spine_event_id"])
            for row in self.memory.db.execute("SELECT memory_id, spine_event_id FROM memory_claims")
        }
        self.assertEqual(len(claim_events), 3)
        self.memory.close()
        self._downgrade_to_46(drop_column=True)
        self.memory = Memory(self.db_path)
        self.assertEqual(self.memory.db.execute("PRAGMA user_version").fetchone()[0], 50)
        # The re-migration re-projects the graph and receipts exactly that:
        # one projection.rebuilt, and no re-import of any memory or claim.
        self.assertEqual(self._event_count(), events_before + 1)
        self.assertNotIn("memory.imported", self._kinds())
        self.assertEqual(self._memory_lineage(), lineage_before)
        for memory_id, event_id in claim_events.items():
            self.assertEqual(lineage_before[memory_id], ("claim", event_id))
        verification = self.memory.verify_spine()
        self.assertTrue(verification["ok"], verification["problems"])
        claim_rebuild = self.memory.rebuild_claim_projection()
        self.assertTrue(claim_rebuild["ok"], claim_rebuild["divergences"])
        memory_rebuild = self.memory.rebuild_memory_projection()
        self.assertTrue(memory_rebuild["ok"], memory_rebuild["divergences"])
        self._assert_memory_equivalence()
        # A second re-migration is a no-op.
        self.memory.close()
        self._downgrade_to_46(drop_column=False)
        self.memory = Memory(self.db_path)
        self.assertEqual(self._event_count(), events_before + 2)
        self.assertEqual(self._memory_lineage(), lineage_before)
        self.assertTrue(self.memory.verify_spine()["ok"])
        # Writes continue with explicit ids after the re-link.
        self.memory.remember_verified(
            "The deploy uses rsync.", source="operator", origin="explicit_operator_memory",
        )
        self._assert_memory_equivalence()

    def test_downgrade_with_an_edited_row_is_refused(self) -> None:
        self._populate_for_migration()
        edited = int(self.memory.db.execute(
            "SELECT id FROM memories WHERE kind='fact' ORDER BY id LIMIT 1"
        ).fetchone()[0])
        self.memory.close()
        raw = sqlite3.connect(str(self.db_path))
        raw.execute("UPDATE memories SET content='edited out of band' WHERE id=?", (edited,))
        raw.commit()
        raw.close()
        self._downgrade_to_46(drop_column=True)
        with self.assertRaises(RuntimeError):
            Memory(self.db_path)
        # Restoring the row's content lets the re-link succeed: the edit was
        # refused, never laundered into a fresh import.
        raw = sqlite3.connect(str(self.db_path))
        raw.execute(
            "UPDATE memories SET content='An unverified aside about the fleet.' WHERE id=?",
            (edited,),
        )
        raw.commit()
        raw.close()
        self.memory = Memory(self.db_path)
        self.assertEqual(self.memory.db.execute("PRAGMA user_version").fetchone()[0], 50)
        self.assertNotIn("memory.imported", self._kinds())
        verification = self.memory.verify_spine()
        self.assertTrue(verification["ok"], verification["problems"])
        self.assertTrue(self.memory.rebuild_memory_projection()["ok"])

    def test_legacy_migration_still_drops_stale_triggers_first(self) -> None:
        # A stripped store below 46 (the shape the legacy-migration tests use)
        # re-runs the claim backfills before the spine exists and lands at 49
        # with every row imported.
        from tests.legacy_store_fixture import strip_spine

        self._populate_for_migration()
        self.memory.remember("An unverified aside about the fleet.")
        strip_spine(self.memory.db)
        self.memory.db.execute("PRAGMA user_version=45")
        self.memory.close()
        self.memory = Memory(self.db_path)
        self.assertEqual(self.memory.db.execute("PRAGMA user_version").fetchone()[0], 50)
        kinds = self._kinds()
        self.assertEqual(kinds.count("claim.imported"), 3)
        self.assertEqual(kinds.count("memory.imported"), 3)
        self.assertEqual(
            self.memory.db.execute("SELECT COUNT(*) FROM memories WHERE spine_event_id IS NULL").fetchone()[0],
            0,
        )
        verification = self.memory.verify_spine()
        self.assertTrue(verification["ok"], verification["problems"])
        self.assertTrue(self.memory.rebuild_claim_projection()["ok"])
        self.assertTrue(self.memory.rebuild_memory_projection()["ok"])
        self._assert_memory_equivalence()

    # --- (d) actor / permission mappings (design 12.6 item 4) --------------------

    def _memory_events(self) -> list[dict[str, object]]:
        return [
            dict(row) for row in self.memory.db.execute(
                """SELECT kind, actor, permission, conversation_id, subject_id, payload_json
                   FROM memory_spine_events WHERE subject_kind='memory' ORDER BY id"""
            )
        ]

    def test_actor_and_permission_mappings_are_receipted(self) -> None:
        conversation = self.memory.new_conversation(project_id=1)
        # The model's memory tool: actor model, the admitting gate, the turn.
        self.memory.remember_verified(
            "The relay is blue.", source="operator", origin="explicit_operator_memory",
            actor="model", permission="autonomous:interactive:explicit_memory_write",
            conversation_id=conversation,
        )
        # The CLI.
        self.memory.remember_verified(
            "Feedback: shorter answers.", kind="feedback", source="explicit user feedback",
            origin="explicit_user_feedback", actor="operator", permission="operator:cli",
        )
        # Defaults are runtime.
        self.memory.remember("An aside.")
        self._verified_lesson("Reuse the measured parser boundary regression.")
        _vault_dir, vault = self._vault()
        path = vault.write_note("research", "Kestrel notes", "The relay listens on 9090.")
        assert path is not None
        self._sync_vault()
        path.write_text(path.read_text(encoding="utf-8").replace("9090", "9191"), encoding="utf-8")
        self._sync_vault(actor="operator", permission="operator:interactive", conversation_id=conversation)
        path.unlink()
        self._sync_vault()
        # An unknown actor falls back to runtime.
        self.memory.remember("Another aside.", actor="martian", permission="x")
        events = self._memory_events()
        shape = [(e["kind"], e["actor"], e["permission"], e["conversation_id"]) for e in events]
        self.assertEqual(shape, [
            ("memory.created", "model", "autonomous:interactive:explicit_memory_write", conversation),
            ("memory.created", "operator", "operator:cli", None),
            ("memory.created", "runtime", "runtime", None),
            ("lesson.created", "runtime", "runtime", None),
            ("memory.created", "runtime", "runtime:indexer", None),
            ("memory.updated", "operator", "operator:interactive", conversation),
            ("memory.deleted", "runtime", "runtime:indexer", None),
            ("memory.created", "runtime", "x", None),
        ])
        # Invariant: eligibility comes from ordinary_memory_provenance, never
        # from the spine actor.
        provenance = {
            str(row["content"]): (str(row["origin"]), bool(int(row["eligible"])))
            for row in self.memory.db.execute(
                """SELECT m.content, omp.origin, omp.eligible FROM memories AS m
                   JOIN ordinary_memory_provenance AS omp ON omp.memory_id=m.id"""
            )
        }
        self.assertEqual(provenance["The relay is blue."], ("explicit_operator_memory", True))
        self.assertEqual(provenance["An aside."], ("unverified", False))
        self.assertEqual(provenance["Another aside."], ("unverified", False))
        self.memory.remember("The relay is blue.", actor="operator", permission="operator:cli")
        self.assertEqual(
            self.memory.db.execute(
                "SELECT eligible FROM ordinary_memory_provenance WHERE memory_id=1"
            ).fetchone()[0],
            1,
        )
        self.assertTrue(self.memory.verify_spine()["ok"])
        self._assert_memory_equivalence()

    def test_legacy_seed_helper_plants_lineage_without_provenance(self) -> None:
        from tests.legacy_store_fixture import seed_legacy_memory_row

        memory_id = seed_legacy_memory_row(
            self.memory, kind="lesson", content="A legacy lesson.", source="legacy import",
            family="code_fix", outcome_status="complete",
        )
        row = self.memory.db.execute(
            "SELECT kind, spine_event_id FROM memories WHERE id=?", (memory_id,)
        ).fetchone()
        self.assertEqual(row["kind"], "lesson")
        event = self.memory.db.execute(
            "SELECT kind, actor, permission, subject_id FROM memory_spine_events WHERE id=?",
            (row["spine_event_id"],),
        ).fetchone()
        self.assertEqual(tuple(event), ("memory.imported", "system", "test:legacy-seed", memory_id))
        self.assertIsNone(self.memory.db.execute(
            "SELECT 1 FROM ordinary_memory_provenance WHERE memory_id=?", (memory_id,)
        ).fetchone())
        # A raw insert without lineage is what the trigger exists to stop.
        with self.assertRaises(sqlite3.IntegrityError):
            self.memory.db.execute(
                "INSERT INTO memories(created_at, kind, content, source) VALUES (?, 'fact', 'raw', NULL)",
                (now_iso(),),
            )
        self.assertTrue(self.memory.verify_spine()["ok"])
        self.assertTrue(self.memory.rebuild_memory_projection()["ok"])

    # --- H-1: large stale removals are receipted in bounded chunks --------------

    def test_large_vault_removal_is_receipted_in_bounded_chunks(self) -> None:
        _vault_dir, vault = self._vault()
        paths: list[Path] = []
        for index in range(300):
            if index % 16 == 0:
                vault.begin_task()
            path = vault.write_note("research", f"Note {index}", f"Body of note {index}.")
            assert path is not None
            paths.append(path)
        first = self._sync_vault()
        self.assertEqual(first["inserted"], 300)
        for path in paths:
            path.unlink()
        second = self._sync_vault()
        self.assertEqual(second["removed"], 300)
        self.assertEqual(
            self.memory.db.execute("SELECT COUNT(*) FROM memories WHERE kind='vault'").fetchone()[0],
            0,
        )
        receipts = [
            json.loads(str(row[0])) for row in self.memory.db.execute(
                "SELECT payload_json FROM memory_spine_events WHERE kind='memory.deleted' ORDER BY id"
            )
        ]
        chunk = max(1, int(getattr(memory_spine, "MEMORY_DELETED_MAX_IDS", 128)))
        self.assertEqual(len(receipts), -(-300 // chunk))
        self.assertTrue(all(len(receipt["ids"]) <= chunk for receipt in receipts))
        removed_ids = [memory_id for receipt in receipts for memory_id in receipt["ids"]]
        self.assertEqual(len(removed_ids), 300)
        self.assertEqual(len(set(removed_ids)), 300)
        verification = self.memory.verify_spine()
        self.assertTrue(verification["ok"], verification["problems"])
        rebuild = self.memory.rebuild_memory_projection()
        self.assertTrue(rebuild["ok"], rebuild["divergences"][:5])
        self.assertEqual(rebuild["rows_live"], 0)
        # The ids never come back.
        self.assertGreater(
            self.memory.db.execute("SELECT next_id FROM memory_id_sequence WHERE id=1").fetchone()[0],
            max(removed_ids),
        )

    # --- H-2: chain-middle delete under foreign keys ----------------------------

    def test_apply_recreates_a_chain_middle_delete_under_foreign_keys(self) -> None:
        conversation = self.memory.new_conversation(project_id=1)
        for value in ("8080", "9090", "9191"):
            self.memory.remember_explicit_project_claim(
                conversation, 1, _command("Kestrel relay", "listen port", value)
            )

        def chain() -> list[tuple[int, int | None]]:
            return [
                (int(row["id"]), row["supersedes_id"]) for row in self.memory.db.execute(
                    "SELECT id, supersedes_id FROM memory_claims ORDER BY id"
                )
            ]

        self.assertEqual(chain(), [(1, None), (2, 1), (3, 2)])
        self.memory.close()
        raw = sqlite3.connect(str(self.db_path))
        raw.execute("PRAGMA foreign_keys=ON")
        # An out-of-band deleter under foreign keys must detach every
        # reference to the middle claim before it can go.
        raw.execute("UPDATE memory_claims SET supersedes_id=NULL WHERE supersedes_id=2")
        raw.execute("UPDATE memory_claim_events SET related_claim_id=NULL WHERE related_claim_id=2")
        for table in (
            # The graph edge references the claim row, so an out-of-band
            # deleter has to detach it first too (schema 48).
            "memory_graph_edges",
            "memory_claim_clock_statistics", "memory_claim_observations",
            "memory_claim_evidence", "memory_claim_events",
        ):
            raw.execute(f"DELETE FROM {table} WHERE claim_id=2")
        raw.execute("DELETE FROM memory_claims WHERE id=2")
        raw.commit()
        raw.close()
        self.memory = Memory(self.db_path)
        self.assertEqual(self.memory.db.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        dry = self.memory.rebuild_claim_projection()
        self.assertEqual(
            sorted(
                (item["claim_id"], item["kind"]) for item in dry["divergences"]
                if item["kind"] in {"field", "missing_in_live", "missing_in_rebuild"}
            ),
            [(2, "missing_in_live"), (3, "field")],
            dry["divergences"],
        )
        report = self.memory.rebuild_claim_projection(apply=True)
        self.assertTrue(report["ok"], report)
        self.assertTrue(report["applied"])
        self.assertEqual(report["recreated_ids"], [2])
        self.assertIn(3, report["updated_ids"])
        self.assertEqual(report["divergences"], [])
        self.assertEqual(chain(), [(1, None), (2, 1), (3, 2)])
        middle = self.memory.db.execute(
            "SELECT status, value, spine_event_id FROM memory_claims WHERE id=2"
        ).fetchone()
        self.assertEqual((middle["status"], middle["value"]), ("superseded", "9090"))
        self.assertIsNotNone(middle["spine_event_id"])
        self.assertEqual(self.memory.db.execute("PRAGMA foreign_key_check").fetchall(), [])
        clean = self.memory.rebuild_claim_projection()
        self.assertTrue(clean["ok"], clean["divergences"])
        verification = self.memory.verify_spine()
        self.assertTrue(verification["ok"], verification["problems"])
        self.assertEqual(self._kinds()[-1], "projection.rebuilt")

    # --- M-3: the plan the operator saw is the plan that is applied -------------

    def test_apply_refuses_a_stale_plan_and_binds_a_plan_token(self) -> None:
        edited, planted, deleted, _planted_memory = self._plant_three_divergences()
        plan = self.memory.rebuild_claim_projection()
        token = plan["plan_token"]
        self.assertRegex(token, r"^[0-9a-f]{12}$")
        self.assertEqual(
            plan["head_event_id"],
            self.memory.db.execute("SELECT last_event_id FROM memory_spine_head").fetchone()[0],
        )
        # The token is a pure function of the head and the divergence set.
        self.assertEqual(self.memory.rebuild_claim_projection()["plan_token"], token)
        # The store changes after the operator saw the plan.
        conversation = self.memory.new_conversation(project_id=1)
        self.memory.remember_explicit_project_claim(
            conversation, 1, _command("Osprey relay", "listen port", "7070")
        )
        snapshot = (
            [tuple(row) for row in self.memory.db.execute("SELECT * FROM memory_claims ORDER BY id")],
            [tuple(row) for row in self.memory.db.execute("SELECT * FROM memory_spine_head")],
            self._event_count(),
        )
        stale = self.memory.rebuild_claim_projection(apply=True, plan=plan)
        self.assertFalse(stale["ok"])
        self.assertFalse(stale["applied"])
        self.assertEqual(stale["refusal"], "stale_plan")
        self.assertEqual(stale["plan_token"], token)
        self.assertTrue(stale["divergences"])
        self.assertFalse(self.memory.db.in_transaction)
        self.assertEqual(
            (
                [tuple(row) for row in self.memory.db.execute("SELECT * FROM memory_claims ORDER BY id")],
                [tuple(row) for row in self.memory.db.execute("SELECT * FROM memory_spine_head")],
                self._event_count(),
            ),
            snapshot,
        )
        # A plan without a token is compared by its divergence set.
        by_set = self.memory.rebuild_claim_projection(apply=True, plan={"divergences": []})
        self.assertEqual(by_set["refusal"], "stale_plan")
        self.assertEqual(self._event_count(), snapshot[2])
        # A fresh plan applies, and the apply report carries its token.
        fresh = self.memory.rebuild_claim_projection()
        self.assertNotEqual(fresh["plan_token"], token)
        applied = self.memory.rebuild_claim_projection(apply=True, plan=fresh)
        self.assertTrue(applied["ok"], applied)
        self.assertTrue(applied["applied"])
        self.assertEqual(applied["plan_token"], fresh["plan_token"])
        self.assertEqual(sorted(applied["removed_ids"]), [planted])
        self.assertEqual(applied["recreated_ids"], [deleted])
        self.assertIn(edited, applied["updated_ids"])
        self.assertTrue(self.memory.rebuild_claim_projection()["ok"])
        # Without a plan, apply takes its own dry run outside the lock; on a
        # clean store there is nothing to do and the token is still reported.
        again = self.memory.rebuild_claim_projection(apply=True)
        self.assertTrue(again["ok"])
        self.assertFalse(again["applied"])
        self.assertIsNone(again["refusal"])
        self.assertRegex(again["plan_token"], r"^[0-9a-f]{12}$")

    # --- L-2 / L-3: input validation happens before the spine ------------------

    def test_invalid_kind_and_conversation_ids_fail_before_the_spine(self) -> None:
        for kind in ("", "   "):
            with self.assertRaises(ValueError):
                self.memory.remember("An aside.", kind=kind)
        with self.assertRaises(ValueError):
            self.memory.remember("   ")
        self.assertFalse(self.memory.db.in_transaction)
        self.assertEqual(self._kinds(), ["spine.genesis", "projection.rebuilt"])
        self.assertEqual(self.memory.db.execute("SELECT COUNT(*) FROM memories").fetchone()[0], 0)
        # Out-of-range or non-integer conversation ids are dropped to NULL,
        # never raised out of a write; the largest signed 64-bit id is kept.
        self.memory.remember("Huge conversation id.", conversation_id=10**19)
        self.memory.remember("Boolean conversation id.", conversation_id=True)
        self.memory.remember("Zero conversation id.", conversation_id=0)
        self.memory.remember("Largest conversation id.", conversation_id=2**63 - 1)
        rows = [
            row[0] for row in self.memory.db.execute(
                "SELECT conversation_id FROM memory_spine_events WHERE kind='memory.created' ORDER BY id"
            )
        ]
        self.assertEqual(rows, [None, None, None, 2**63 - 1])
        self.assertTrue(self.memory.verify_spine()["ok"])

    # --- (e) subject_claim_history ------------------------------------------------

    def _seed_history(self) -> int:
        conversation = self.memory.new_conversation(project_id=1)
        for value in ("8080", "9090", "7070", "6060"):
            self.memory.remember_explicit_project_claim(
                conversation, 1, _command("Kestrel relay", "listen port", value)
            )
        for value in ("Dana", "Eve"):
            self.memory.remember_explicit_project_claim(
                conversation, 1, _command("Kestrel relay", "owner", value)
            )
        for value in ("Fenwick", "Larkhill"):
            self.memory.remember_claim(
                "Kestrel relay", "datacenter", value, source="scan", authority="verified",
                source_identity="scanner",
            )
        for value in ("1", "2"):
            self.memory.remember_explicit_project_claim(
                conversation, 1, _command("Kestrelnet relay", "listen port", value)
            )
        return conversation

    def test_subject_claim_history_returns_bounded_screened_versions(self) -> None:
        conversation = self._seed_history()
        history = self.memory.subject_claim_history("Kestrel relay", project_id=1)
        self.assertTrue(history)
        self.assertLessEqual(len(history), 6)
        for entry in history:
            self.assertEqual(entry["status"], "superseded")
            self.assertTrue(entry["superseded_at"])
            self.assertFalse(entry["retracted"])
            self.assertEqual(entry["subject"], "Kestrel relay")
            self.assertNotIn("claim_key", entry)
            for name in ("claim_id", "memory_id", "scope", "predicate", "value", "source",
                         "authority", "confidence", "valid_from", "valid_until", "updated_at",
                         "supersedes_id"):
                self.assertIn(name, entry)
        by_predicate: dict[str, list[str]] = {}
        for entry in history:
            by_predicate.setdefault(str(entry["predicate"]), []).append(str(entry["value"]))
        self.assertEqual(by_predicate["listen port"], ["7070", "9090", "8080"])
        self.assertEqual(by_predicate["owner"], ["Dana"])
        self.assertEqual(by_predicate["datacenter"], ["Fenwick"])
        # Ordering is valid_until DESC, id DESC.
        stamps = [(str(entry["valid_until"]), int(entry["claim_id"])) for entry in history]
        self.assertEqual(stamps, sorted(stamps, reverse=True))
        # The limit bounds the total.
        self.assertEqual(len(self.memory.subject_claim_history("Kestrel relay", project_id=1, limit=2)), 2)
        # Forget keeps the history and marks the key retracted; Erase removes it.
        self.memory.retract_explicit_project_claim(conversation, 1, _forget("Kestrel relay", "listen port"))
        after_forget = self.memory.subject_claim_history("Kestrel relay", project_id=1)
        ports = [entry for entry in after_forget if entry["predicate"] == "listen port"]
        self.assertEqual(len(ports), 3)
        self.assertTrue(all(entry["retracted"] for entry in ports))
        self.assertEqual([entry["value"] for entry in ports], ["6060", "7070", "9090"])
        self.assertTrue(all(
            not entry["retracted"] for entry in after_forget if entry["predicate"] != "listen port"
        ))
        self.memory.erase_explicit_project_claim(conversation, 1, _erase("Kestrel relay", "owner"))
        after_erase = self.memory.subject_claim_history("Kestrel relay", project_id=1)
        self.assertNotIn("owner", {entry["predicate"] for entry in after_erase})
        self.assertNotIn("Dana", {entry["value"] for entry in after_erase})

    def test_subject_claim_history_screens_and_shadows(self) -> None:
        self._seed_history()
        # Look-alike subjects never leak into the history.
        history = self.memory.subject_claim_history("Kestrel relay", project_id=1)
        self.assertTrue(history)
        self.assertNotIn("Kestrelnet relay", {entry["subject"] for entry in history})
        self.assertEqual(
            {entry["subject"] for entry in self.memory.subject_claim_history("Kestrelnet relay", project_id=1)},
            {"Kestrelnet relay"},
        )
        # Secret -> refuse; private identifier -> abstain; unknown or disabled
        # project -> abstain.
        with self.assertRaises(ValueError):
            self.memory.subject_claim_history(
                # Assembled so the scanned source never carries a token
                # matching the aws-access-token rule; runtime unchanged.
                "Kestrel relay " + "AKIA" + "ABCDEFGHIJKLMNOP",
                project_id=1,
            )
        self.assertEqual(self.memory.subject_claim_history("dana@example.com", project_id=1), [])
        self.assertEqual(self.memory.subject_claim_history("Kestrel relay", project_id=77), [])
        self.assertEqual(self.memory.subject_claim_history("", project_id=1), [])
        # Without a project only the global lane is visible.
        global_only = self.memory.subject_claim_history("Kestrel relay")
        self.assertEqual({entry["scope"] for entry in global_only}, {"global"})
        self.assertEqual([entry["value"] for entry in global_only], ["Fenwick"])
        # A project key shadows the global key's history.
        conversation = self.memory.new_conversation(project_id=1)
        self.memory.remember_explicit_project_claim(
            conversation, 1, _command("Kestrel relay", "datacenter", "Ashby")
        )
        self.memory.remember_explicit_project_claim(
            conversation, 1, _command("Kestrel relay", "datacenter", "Bexley")
        )
        shadowed = self.memory.subject_claim_history("Kestrel relay", project_id=1)
        datacenters = [entry for entry in shadowed if entry["predicate"] == "datacenter"]
        self.assertEqual([(entry["scope"], entry["value"]) for entry in datacenters], [("project:1", "Ashby")])
        # Three per key, six in total.
        for value in ("5050", "4040", "3030"):
            self.memory.remember_explicit_project_claim(
                conversation, 1, _command("Kestrel relay", "listen port", value)
            )
        capped = self.memory.subject_claim_history("Kestrel relay", project_id=1)
        self.assertEqual(len(capped), 5)
        ports = [entry["value"] for entry in capped if entry["predicate"] == "listen port"]
        self.assertEqual(ports, ["4040", "5050", "6060"])
        self.memory.db.execute("UPDATE agent_projects SET enabled=0 WHERE id=1")
        self.assertEqual(self.memory.subject_claim_history("Kestrel relay", project_id=1), [])
        self.assertFalse(self.memory.db.in_transaction)


if __name__ == "__main__":
    unittest.main()


class LadderRebuildIsolationTests(_SpineStoreCase):
    """M4 design 7.2 and invariant 11: a claim rebuild leaves the ladder
    alone.

    Neither record table is a projection.  A sealed calibration epoch and a
    skill promotion are *records of decisions*: nothing about them is
    derivable from the claim history, so ``rebuild-claims`` must not rebuild
    them, must not touch a row of either, and must not change the four
    counters ``spine verify`` reports for them.
    """

    LADDER_COUNTERS = ("ledger_rows", "ledger_events", "ladder_rows", "ladder_events")

    def _seed_claims_and_memories(self, rng: random.Random) -> None:
        conversation = self.memory.new_conversation(project_id=1)
        subjects = ["Kestrel relay", "Osprey relay", "Harrier box"]
        predicates = ["listen port", "owner"]
        agent, _client = self._agent()
        for step in range(24):
            roll = rng.random()
            if roll < 0.4:
                self.memory.remember_claim(
                    rng.choice(subjects), rng.choice(predicates),
                    f"value-{step}", source="fixture", authority="verified",
                )
            elif roll < 0.7:
                self.memory.remember_verified(
                    f"An aside {step} about the relay fleet.",
                    source="operator", origin="explicit_operator_memory",
                )
            else:
                self.memory.remember(
                    f"An unverified aside {step}.", actor="model",
                    permission="autonomous:interactive:explicit_memory_write",
                    conversation_id=conversation,
                )
        del agent

    def _ladder_outcome(self, *, complete: bool = True, lesson_id: int | None = None) -> int:
        conversation_id = self.memory.new_conversation(project_id=1)
        prediction_id = self.memory.record_prediction(
            family="code_fix", profile="rebuild", model="deterministic-test",
            predicted_success=0.8, predicted_steps=2,
            predicted_verification="tool_success", basis="prior",
            origin="interactive", conversation_id=conversation_id,
        )
        if lesson_id is not None:
            self.memory.record_lesson_applications(
                prediction_id, "code_fix", [lesson_id]
            )
        self.memory.resolve_prediction(
            prediction_id,
            actual_status="complete" if complete else "failed",
            actual_steps=2, evidence_ok=complete,
            failure_class=None if complete else "unknown",
            primary_tool="read_file",
        )
        return prediction_id

    def _seed_ladder(self) -> dict[str, object]:
        # rung 0: one lesson through the shipped path.
        conversation_id = self.memory.new_conversation(project_id=1)
        prediction_id = self.memory.record_prediction(
            family="code_fix", profile="rebuild", model="deterministic-test",
            predicted_success=0.8, predicted_steps=2,
            predicted_verification="tool_success", basis="prior",
            origin="interactive", conversation_id=conversation_id,
        )
        self.memory.resolve_prediction(
            prediction_id, actual_status="complete", actual_steps=2,
            evidence_ok=True, primary_tool="read_file",
        )
        reflection_id = self.memory.record_reflection(
            status="complete", summary="Rebuild fixture outcome.",
            improvements=(
                "Resolve the failing module path from the kestrel runner output."
            ),
            conversation_id=conversation_id, prediction_id=prediction_id,
            tool_calls=2,
        )
        lesson_id = int(self.memory.db.execute(
            "SELECT id FROM memories WHERE kind='lesson' AND reflection_id=?",
            (reflection_id,),
        ).fetchone()["id"])

        # a calibrated population with twelve verified reuses.
        applied = 0
        for position in range(1, 80):
            complete = position % 5 != 4
            attach = lesson_id if (complete and applied < 12) else None
            if attach is not None:
                applied += 1
            self._ladder_outcome(complete=complete, lesson_id=attach)

        # a pre-M4 document for the grandfather pass.
        skill_evolution.distill_verified_skill(
            self.workspace, family="code_test", successful_tools=["shell"],
            verification="tool_success",
        )
        self.memory.grandfather_ladder(self.workspace, project_id=1)

        sealed = self.memory.seal_calibration_epoch(
            "code_fix", workspace=self.workspace
        )
        self.assertGreaterEqual(len(sealed), 3)

        first = self.memory.stage_ladder_promotion(
            family="code_fix", project_id=1, workspace=self.workspace
        )
        self.assertTrue(first.get("staged"), first)
        approved = self.memory.apply_ladder_promotion(
            first["promotion_id"], approval_token=first["approval_token"],
            workspace=self.workspace,
        )
        self.assertTrue(approved.get("applied"), approved)
        rolled = self.memory.rollback_ladder_promotion(
            first["promotion_id"], workspace=self.workspace
        )
        self.assertTrue(rolled.get("rolled_back"), rolled)
        second = self.memory.stage_ladder_promotion(
            family="code_fix", project_id=1, workspace=self.workspace
        )
        self.assertTrue(second.get("staged"), second)
        return {"lesson_id": lesson_id, "promotions": [first, second]}

    def _ladder_snapshot(self) -> tuple[list[tuple], list[tuple]]:
        return (
            [
                tuple(row) for row in self.memory.db.execute(
                    "SELECT * FROM memory_calibration_ledger ORDER BY id"
                )
            ],
            [
                tuple(row) for row in self.memory.db.execute(
                    "SELECT * FROM ladder_promotions ORDER BY id"
                )
            ],
        )

    def test_a_claim_rebuild_changes_no_ladder_row_or_counter(self) -> None:
        rng = random.Random(20260904)
        self._seed_claims_and_memories(rng)
        self._seed_ladder()

        before_verify = self.memory.verify_spine()
        self.assertTrue(before_verify["ok"], before_verify["problems"])
        self.assertTrue(before_verify["ladder_lineage_ok"])
        before_counters = {
            key: before_verify[key] for key in self.LADDER_COUNTERS
        }
        self.assertGreaterEqual(before_counters["ledger_rows"], 3)
        self.assertGreaterEqual(before_counters["ladder_rows"], 3)
        before_ledger, before_promotions = self._ladder_snapshot()

        # A clean store's apply is a no-op, which proves nothing about
        # isolation, so a real divergence is planted out of band first: an
        # edited claim value the rebuild must rewrite from the spine.  The
        # question this test asks is whether an apply that genuinely changes
        # the claim projection can also reach the ladder.
        victim = self.memory.db.execute(
            "SELECT id, value FROM memory_claims ORDER BY id LIMIT 1"
        ).fetchone()
        self.assertIsNotNone(victim)
        self.memory.db.execute(
            "UPDATE memory_claims SET value='edited out of band' WHERE id=?",
            (int(victim["id"]),),
        )
        self.memory.db.commit()

        dry = self.memory.rebuild_claim_projection()
        self.assertFalse(dry["ok"])
        self.assertTrue(dry["divergences"])
        after_dry = self._ladder_snapshot()
        self.assertEqual(after_dry, (before_ledger, before_promotions))

        applied = self.memory.rebuild_claim_projection(
            apply=True, plan=dry, actor="operator", permission="operator:cli"
        )
        self.assertTrue(applied.get("applied"), applied)
        self.assertGreaterEqual(int(applied["divergences_fixed"]), 1)
        self.assertEqual(
            str(self.memory.db.execute(
                "SELECT value FROM memory_claims WHERE id=?", (int(victim["id"]),)
            ).fetchone()["value"]),
            str(victim["value"]),
        )

        after_ledger, after_promotions = self._ladder_snapshot()
        self.assertEqual(after_ledger, before_ledger)
        self.assertEqual(after_promotions, before_promotions)

        after_verify = self.memory.verify_spine()
        self.assertTrue(after_verify["ok"], after_verify["problems"])
        self.assertTrue(after_verify["ladder_lineage_ok"])
        # The apply appended its own projection.rebuilt receipt, so the chain
        # did move -- and the ladder counters did not, which is the point.
        self.assertGreater(after_verify["events"], before_verify["events"])
        self.assertEqual(
            {key: after_verify[key] for key in self.LADDER_COUNTERS},
            before_counters,
        )
        self.assertEqual(
            self.memory.verify_calibration_ledger()["problems"], []
        )
        # The ladder is not a projection and must never be listed as one.
        self.assertNotIn("ladder", memory_spine._REBUILT_PROJECTIONS)
        self.assertEqual(
            sorted(memory_spine._REBUILT_PROJECTIONS),
            ["claims", "graph", "milestones"],
        )

    def test_a_memory_rebuild_leaves_the_ladder_alone_too(self) -> None:
        self._seed_ladder()
        before = self._ladder_snapshot()
        report = self.memory.rebuild_memory_projection()
        self.assertTrue(report["ok"], report.get("divergences"))
        self.assertEqual(self._ladder_snapshot(), before)
