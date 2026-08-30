from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from typing import Any


FIXTURE_PATH = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "memory_retrieval_holdout_v5.json"
)
FIXTURE_SHA256 = "e6559643b59a2c359ade93d435d96b90a1bd78450488abc60f5b475899af8e19"
SCORER_SHA256 = "00aa15fc0fa9b8dcd161d55559e0a771f047490351b6e28348a5629a166307dc"
SCORER_START = "# -- BEGIN SEALED MEMORY RETRIEVAL HOLDOUT V5 SCORER --"
SCORER_END = "# -- END SEALED MEMORY RETRIEVAL HOLDOUT V5 SCORER --"
TOKEN_ENVIRONMENT_VARIABLE = "JARVIS_MEMORY_HOLDOUT_V5_TOKEN"


def _sealed_scorer_bytes() -> bytes:
    source = Path(__file__).read_text(encoding="utf-8")
    normalized = source.replace("\r\n", "\n").replace("\r", "\n")
    opening = SCORER_START + "\n"
    closing = "\n" + SCORER_END
    start = normalized.index(opening) + len(opening)
    end = normalized.index(closing, start)
    return normalized[start:end].encode("utf-8")


def _required_run_token() -> str:
    seals = f"{FIXTURE_SHA256}:{SCORER_SHA256}".encode("ascii")
    return hashlib.sha256(seals).hexdigest()


# -- BEGIN SEALED MEMORY RETRIEVAL HOLDOUT V5 SCORER --
def _load_fixture() -> dict[str, Any]:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def _memory_row_id(memory: Any, *, content: str, kind: str) -> int:
    row = memory.db.execute(
        """
        SELECT id
          FROM memories
         WHERE kind=? AND content=?
         ORDER BY id DESC
         LIMIT 1
        """,
        (kind, content),
    ).fetchone()
    if row is None:
        raise AssertionError(f"seeded {kind} record was not stored")
    return int(row["id"])


def _supported_lesson_family(fixture_family: str) -> str:
    return {
        "research": "deep_research",
    }.get(fixture_family, fixture_family)


def _seed_records(
    memory: Any,
    fixture: dict[str, Any],
) -> dict[str, dict[int, str]]:
    runtime_to_logical: dict[str, dict[int, str]] = {
        "general": {},
        "lesson": {},
        "claim": {},
    }
    for record in fixture["records"]:
        channel = record["channel"]
        if channel == "general":
            memory.remember_verified(
                record["content"],
                record["kind"],
                record["source"],
                origin="verified_import",
            )
            runtime_id = _memory_row_id(
                memory,
                content=record["content"],
                kind=record["kind"],
            )
        elif channel == "lesson":
            conversation_id = memory.new_conversation(
                f"synthetic v5 lesson {record['id']}"
            )
            prediction_id = memory.record_prediction(
                family=_supported_lesson_family(record["family"]),
                profile="synthetic-holdout-v5",
                model="deterministic-fixture",
                predicted_success=0.8,
                predicted_steps=2,
                predicted_verification="tool_success",
                basis="prior",
                origin="interactive",
                conversation_id=conversation_id,
            )
            completed = record["outcome"] == "complete"
            resolved = memory.resolve_prediction(
                prediction_id,
                actual_status=record["outcome"],
                actual_steps=2,
                evidence_ok=True if completed else False,
                failure_class=None if completed else "verification_absent",
            )
            if not resolved:
                raise AssertionError(f"prediction did not resolve for {record['id']}")
            reflection_id = memory.record_reflection(
                status=record["outcome"],
                summary=f"Synthetic fictional outcome for {record['id']}.",
                improvements=record["content"],
                conversation_id=conversation_id,
                prediction_id=prediction_id,
                tool_calls=2,
            )
            row = memory.db.execute(
                "SELECT id FROM memories WHERE reflection_id=?",
                (reflection_id,),
            ).fetchone()
            if row is None:
                raise AssertionError(f"lesson record was not stored for {record['id']}")
            runtime_id = int(row["id"])
        elif channel == "claim":
            runtime_id = int(memory.remember_claim(
                record["subject"],
                record["predicate"],
                record["value"],
                source=record["source"],
                authority=record["authority"],
                confidence=1.0 if record["authority"] != "external" else 0.8,
            ))
        else:
            raise AssertionError(f"unsupported fixture channel: {channel}")
        if runtime_id in runtime_to_logical[channel]:
            raise AssertionError(f"duplicate runtime id in {channel}: {runtime_id}")
        runtime_to_logical[channel][runtime_id] = record["id"]
    return runtime_to_logical


def _retrieve_case(memory: Any, case: dict[str, Any]) -> list[int]:
    if case["channel"] == "general":
        rows = memory.search(
            case["query"],
            limit=int(case["limit"]),
            include_id=True,
        )
        return [int(row["memory_id"]) for row in rows]
    if case["channel"] == "lesson":
        rows = memory.match_lessons(
            case["query"],
            _supported_lesson_family(case["family"]),
            limit=int(case["limit"]),
        )
        return [int(row["memory_id"]) for row in rows]
    if case["channel"] == "claim":
        rows = memory.current_claims(
            case["query"],
            limit=int(case["limit"]),
        )
        return [int(row["claim_id"]) for row in rows]
    raise AssertionError(f"unsupported case channel: {case['channel']}")


def _empty_counters() -> dict[str, int]:
    return {
        "cases": 0,
        "positive_cases": 0,
        "no_hit_cases": 0,
        "no_hit_passes": 0,
        "expected_hits": 0,
        "returned_hits": 0,
        "true_positives": 0,
        "leakage": 0,
    }


def _finalize_metrics(counters: dict[str, int]) -> dict[str, int | float]:
    returned = counters["returned_hits"]
    expected = counters["expected_hits"]
    no_hit_cases = counters["no_hit_cases"]
    return {
        **counters,
        "precision": (
            counters["true_positives"] / returned if returned else 1.0
        ),
        "recall": (
            counters["true_positives"] / expected if expected else 1.0
        ),
        "no_hit_rate": (
            counters["no_hit_passes"] / no_hit_cases if no_hit_cases else 1.0
        ),
    }


def _evaluate_holdout(
    memory: Any,
    fixture: dict[str, Any],
    runtime_to_logical: dict[str, dict[int, str]],
) -> dict[str, Any]:
    forbidden = {
        record["id"]
        for record in fixture["records"]
        if record["globally_forbidden"]
    }
    per_channel = {
        channel: _empty_counters()
        for channel in ("general", "lesson", "claim")
    }
    aggregate = _empty_counters()
    unexpected: list[dict[str, Any]] = []

    for case in fixture["cases"]:
        channel = case["channel"]
        expected = set(case["expected"])
        runtime_ids = _retrieve_case(memory, case)
        actual = [
            runtime_to_logical[channel].get(
                runtime_id,
                f"unmapped-{channel}-{runtime_id}",
            )
            for runtime_id in runtime_ids
        ]
        actual_set = set(actual)
        true_positives = len(expected & actual_set)
        leaked = sum(1 for logical_id in actual if logical_id in forbidden)

        for counters in (per_channel[channel], aggregate):
            counters["cases"] += 1
            counters["expected_hits"] += len(expected)
            counters["returned_hits"] += len(actual)
            counters["true_positives"] += true_positives
            counters["leakage"] += leaked
            if expected:
                counters["positive_cases"] += 1
            else:
                counters["no_hit_cases"] += 1
                if not actual:
                    counters["no_hit_passes"] += 1

        if expected != actual_set:
            unexpected.append({
                "case": case["id"],
                "expected": sorted(expected),
                "actual": actual,
            })

    return {
        "holdout": fixture["holdout"],
        "records": len(fixture["records"]),
        "cases": len(fixture["cases"]),
        "channels": {
            channel: _finalize_metrics(per_channel[channel])
            for channel in ("general", "lesson", "claim")
        },
        "aggregate": _finalize_metrics(aggregate),
        "unexpected": unexpected,
    }
# -- END SEALED MEMORY RETRIEVAL HOLDOUT V5 SCORER --


class MemoryRetrievalHoldoutV5IntegrityTests(unittest.TestCase):
    def test_fixture_scorer_seals_counts_and_public_safety(self) -> None:
        fixture_bytes = FIXTURE_PATH.read_bytes()
        self.assertEqual(
            hashlib.sha256(fixture_bytes).hexdigest(),
            FIXTURE_SHA256,
        )
        self.assertEqual(
            hashlib.sha256(_sealed_scorer_bytes()).hexdigest(),
            SCORER_SHA256,
        )

        fixture = json.loads(fixture_bytes.decode("utf-8"))
        records = fixture["records"]
        cases = fixture["cases"]
        expected_counts = fixture["expected_counts"]
        record_ids = [record["id"] for record in records]
        case_ids = [case["id"] for case in cases]
        forbidden = {
            record["id"] for record in records if record["globally_forbidden"]
        }
        positive = [case for case in cases if case["expected"]]
        no_hit = [case for case in cases if not case["expected"]]
        complete_lesson_families = {
            record["family"]
            for record in records
            if record["channel"] == "lesson"
            and record["outcome"] == "complete"
        }

        self.assertTrue(fixture["public_safe"])
        self.assertTrue(fixture["fictional_only"])
        self.assertEqual(fixture["schema_version"], 1)
        self.assertEqual(len(records), expected_counts["records"])
        self.assertEqual(len(cases), expected_counts["cases"])
        self.assertEqual(len(positive), expected_counts["positive_cases"])
        self.assertEqual(len(no_hit), expected_counts["no_hit_cases"])
        self.assertEqual(
            len(forbidden),
            expected_counts["globally_forbidden_records"],
        )
        self.assertEqual(
            len(complete_lesson_families),
            expected_counts["complete_lesson_families"],
        )
        self.assertGreaterEqual(len(records), 108)
        self.assertGreaterEqual(len(cases), 144)
        self.assertGreaterEqual(len(positive), 87)
        self.assertGreaterEqual(len(no_hit), 57)
        self.assertGreaterEqual(len(forbidden), 42)
        self.assertGreaterEqual(len(complete_lesson_families), 6)
        self.assertEqual(len(record_ids), len(set(record_ids)))
        self.assertEqual(len(case_ids), len(set(case_ids)))
        self.assertEqual(
            {record["channel"] for record in records},
            {"general", "lesson", "claim"},
        )
        self.assertEqual(
            {case["channel"] for case in cases},
            {"general", "lesson", "claim"},
        )

        records_by_id = {record["id"]: record for record in records}
        expected_ids = {
            logical_id
            for case in cases
            for logical_id in case["expected"]
        }
        self.assertTrue(expected_ids.isdisjoint(forbidden))
        for case in cases:
            self.assertIn(case["limit"], (1,))
            for logical_id in case["expected"]:
                self.assertIn(logical_id, records_by_id)
                self.assertEqual(
                    records_by_id[logical_id]["channel"],
                    case["channel"],
                )
        for record in records:
            self.assertTrue(
                record["source"].startswith(
                    "synthetic holdout v5 fictional"
                )
            )
            public_text = (
                record.get("content", "")
                + " "
                + record.get("subject", "")
            ).lower()
            self.assertTrue(
                "fictional" in public_text or "invented" in public_text
            )

        public_material = (
            fixture_bytes.decode("utf-8")
            + "\n"
            + _sealed_scorer_bytes().decode("utf-8")
        )
        sensitive_patterns = (
            r"(?i)https?://",
            r"(?i)[a-z]:[\\/](?:users|documents|desktop)[\\/]",
            r"(?i)\b[\w.+-]+@[\w.-]+\.[a-z]{2,}\b",
            r"\b(?:\d{1,3}\.){3}\d{1,3}\b",
            r"(?i)-----BEGIN [A-Z ]+-----",
            r"(?i)\bsk-[a-z0-9]{12,}\b",
            r"(?i)\b(?:password|passwd|api[_ -]?key|private[_ -]?key)\b",
        )
        for pattern in sensitive_patterns:
            self.assertIsNone(re.search(pattern, public_material))
        self.assertTrue(
            all(
                character in "\n\t" or 32 <= ord(character) <= 126
                for character in public_material
            )
        )


class MemoryRetrievalHoldoutV5ProductionTests(unittest.TestCase):
    def test_sealed_memory_retrieval_holdout_v5(self) -> None:
        if os.environ.get(TOKEN_ENVIRONMENT_VARIABLE) != _required_run_token():
            self.skipTest("sealed v5 hash token was not supplied")

        from jarvis.memory import Memory

        fixture = _load_fixture()
        with tempfile.TemporaryDirectory(
            prefix="jarvis-memory-holdout-v5-"
        ) as directory:
            database_path = Path(directory) / "holdout-v5.db"
            with Memory(database_path) as memory:
                runtime_to_logical = _seed_records(memory, fixture)
                report = _evaluate_holdout(
                    memory,
                    fixture,
                    runtime_to_logical,
                )

        print(json.dumps(report, sort_keys=True, indent=2))
        thresholds = fixture["thresholds"]
        aggregate = report["aggregate"]
        self.assertGreaterEqual(
            aggregate["precision"],
            thresholds["precision"],
        )
        self.assertGreaterEqual(
            aggregate["recall"],
            thresholds["recall"],
        )
        self.assertGreaterEqual(
            aggregate["no_hit_rate"],
            thresholds["no_hit_rate"],
        )
        self.assertEqual(aggregate["leakage"], thresholds["leakage"])


if __name__ == "__main__":
    unittest.main()
