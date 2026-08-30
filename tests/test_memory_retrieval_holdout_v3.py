"""Frozen, independently authored synthetic holdout for memory retrieval v3."""

FROZEN_FIXTURE_SHA256 = "956d28fce6bdf759973486ba2aead14b9337f40a239c4c5b19d23fc382eaa67f"
FROZEN_SCORER_REGION_SHA256 = "15f332ffd66c077f260707bc50797f441b2760041e55b9c7261dde3c6710cf80"
SCORER_BEGIN_MARKER = "# === BEGIN FROZEN MEMORY RETRIEVAL HOLDOUT V3 SCORER ==="
SCORER_END_MARKER = "# === END FROZEN MEMORY RETRIEVAL HOLDOUT V3 SCORER ==="

# === BEGIN FROZEN MEMORY RETRIEVAL HOLDOUT V3 SCORER ===
import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from jarvis.memory import Memory
from jarvis.memory_retrieval import evaluate_response_conditioned_retrieval


FIXTURE_PATH = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "memory_retrieval_holdout_v3.json"
)


def _normalized_scorer_region_sha256() -> str:
    source = Path(__file__).read_bytes().replace(b"\r\n", b"\n")
    begin = (SCORER_BEGIN_MARKER + "\n").encode("utf-8")
    end = ("\n" + SCORER_END_MARKER).encode("utf-8")
    start_index = source.index(begin) + len(begin)
    end_index = source.index(end, start_index)
    return hashlib.sha256(source[start_index:end_index]).hexdigest()


def _load_frozen_fixture() -> dict[str, Any]:
    fixture_bytes = FIXTURE_PATH.read_bytes()
    fixture_sha256 = hashlib.sha256(fixture_bytes).hexdigest()
    assert fixture_sha256 == FROZEN_FIXTURE_SHA256
    fixture = json.loads(fixture_bytes)
    assert fixture["seal"]["scorer_region_sha256"] == FROZEN_SCORER_REGION_SHA256
    assert _normalized_scorer_region_sha256() == FROZEN_SCORER_REGION_SHA256
    return fixture


def _payload_text(fixture: dict[str, Any]) -> str:
    record_fields = (
        "content",
        "summary",
        "mistakes",
        "improvements",
        "subject",
        "predicate",
        "value",
    )
    pieces = [
        str(record.get(field, ""))
        for record in fixture["records"]
        for field in record_fields
    ]
    pieces.extend(str(case["query"]) for case in fixture["cases"])
    return "\n".join(pieces)


def _validate_fixture_contract(fixture: dict[str, Any]) -> None:
    assert fixture["schema"] == "jarvis.memory-retrieval-holdout.v3"
    records = fixture["records"]
    cases = fixture["cases"]
    assert len(records) >= 45
    assert len(cases) >= 60

    record_ids = [str(record["id"]) for record in records]
    case_ids = [str(case["id"]) for case in cases]
    assert len(record_ids) == len(set(record_ids))
    assert len(case_ids) == len(set(case_ids))
    by_id = {str(record["id"]): record for record in records}
    assert set(record["channel"] for record in records) == {
        "general",
        "lesson",
        "claim",
    }
    assert set(case["channel"] for case in cases) == {
        "general",
        "lesson",
        "claim",
    }
    lesson_families = {
        str(record["family"])
        for record in records
        if record["channel"] == "lesson"
    }
    assert len(lesson_families) >= 6
    feature_names = {
        str(feature)
        for case in cases
        for feature in case.get("features", [])
    }
    assert set(fixture["metadata"]["required_features"]) <= feature_names
    assert any(len(case["relevant_ids"]) > 1 for case in cases)
    assert any(not case["relevant_ids"] for case in cases)
    assert any(not bool(record["allowed"]) for record in records)

    for record in records:
        assert isinstance(record["allowed"], bool)
        source = record.get("source")
        if source is not None:
            parsed = urlparse(str(source))
            assert parsed.scheme == "https"
            assert parsed.hostname is not None
            assert parsed.hostname.endswith(".example.invalid")
    for case in cases:
        assert isinstance(case["query"], str) and case["query"].strip()
        assert 1 <= int(case["response_limit"]) <= 3
        for relevant_id in case["relevant_ids"]:
            assert relevant_id in by_id
            assert by_id[relevant_id]["channel"] == case["channel"]
            assert bool(by_id[relevant_id]["allowed"])
        if case["channel"] == "lesson":
            assert case["family"] in lesson_families

    payload = _payload_text(fixture)
    lowered = payload.casefold()
    prohibited_markers = (
        "c:\\users\\",
        "/home/",
        "zip code",
        "postal code",
        "username",
        "hardware preference",
        "provider configuration",
        "spending budget",
        "previous conversation",
        "prior conversation",
        "you told me earlier",
        "my preference",
    )
    assert not any(marker in lowered for marker in prohibited_markers)
    assert re.search(r"\b[0-9]{5}(?:-[0-9]{4})?\b", payload) is None
    assert re.search(r"\b(?:gpu|cpu|vram|ram)\b", lowered) is None

    scoring = fixture["scoring"]
    assert scoring == {
        "response_conditioned": True,
        "aggregation": "micro over case-qualified record identifiers",
        "precision_gate": 0.85,
        "recall_gate": 0.8,
        "no_hit_accuracy_gate": 0.95,
        "leakage_gate": 0,
    }


def _memory_id_for_content(memory: Memory, *, kind: str, content: str) -> int:
    row = memory.db.execute(
        "SELECT id FROM memories WHERE kind=? AND content=?",
        (kind, content),
    ).fetchone()
    assert row is not None
    return int(row["id"])


def _insert_general(memory: Memory, record: dict[str, Any]) -> int:
    if record["write_trust"] == "verified":
        memory.remember_verified(
            record["content"],
            kind=record["kind"],
            source=record["source"],
            origin="verified_import",
        )
    else:
        assert record["write_trust"] == "unverified"
        memory.remember(
            record["content"],
            kind=record["kind"],
            source=record["source"],
        )
    return _memory_id_for_content(
        memory,
        kind=str(record["kind"]),
        content=str(record["content"]),
    )


def _insert_lesson(memory: Memory, record: dict[str, Any]) -> int:
    conversation_id = memory.new_conversation(
        f"Synthetic holdout lesson {record['id']}"
    )
    verification = (
        "cited_sources"
        if record["family"] == "deep_research"
        else "tool_success"
    )
    prediction_id = memory.record_prediction(
        family=record["family"],
        profile="synthetic-holdout",
        model="synthetic-evaluator",
        predicted_success=0.5,
        predicted_steps=1,
        predicted_verification=verification,
        origin="interactive",
        conversation_id=conversation_id,
    )
    outcome_status = str(record["outcome_status"])
    complete = outcome_status == "complete"
    assert memory.resolve_prediction(
        prediction_id,
        actual_status=outcome_status,
        actual_steps=1,
        evidence_ok=complete,
        failure_class=None if complete else "verification_absent",
    )
    reflection_id = memory.record_reflection(
        status=outcome_status,
        summary=record["summary"],
        mistakes=record["mistakes"],
        improvements=record["improvements"],
        conversation_id=conversation_id,
        prediction_id=prediction_id,
        tool_calls=1,
    )
    row = memory.db.execute(
        "SELECT id FROM memories WHERE kind='lesson' AND reflection_id=?",
        (reflection_id,),
    ).fetchone()
    assert row is not None
    return int(row["id"])


def _insert_claim(memory: Memory, record: dict[str, Any]) -> int:
    claim_id = memory.remember_claim(
        record["subject"],
        record["predicate"],
        record["value"],
        source=record["source"],
        authority=record["authority"],
        confidence=float(record["confidence"]),
        source_identity=record["source"],
    )
    row = memory.db.execute(
        "SELECT memory_id FROM memory_claims WHERE id=?",
        (claim_id,),
    ).fetchone()
    assert row is not None
    return int(row["memory_id"])


def _populate_corpus(
    memory: Memory,
    records: list[dict[str, Any]],
) -> tuple[dict[int, str], set[str]]:
    db_to_fixture: dict[int, str] = {}
    forbidden_ids: set[str] = set()
    for record in records:
        channel = str(record["channel"])
        if channel == "general":
            memory_id = _insert_general(memory, record)
        elif channel == "lesson":
            memory_id = _insert_lesson(memory, record)
        else:
            assert channel == "claim"
            memory_id = _insert_claim(memory, record)
        assert memory_id not in db_to_fixture
        fixture_id = str(record["id"])
        db_to_fixture[memory_id] = fixture_id
        if not bool(record["allowed"]):
            forbidden_ids.add(fixture_id)
    assert len(db_to_fixture) == len(records)
    return db_to_fixture, forbidden_ids


def _conditioned_fixture_ids(
    memory: Memory,
    case: dict[str, Any],
    db_to_fixture: dict[int, str],
) -> list[str]:
    limit = int(case["response_limit"])
    channel = str(case["channel"])
    if channel == "general":
        items = memory.search(case["query"], limit=limit, include_id=True)
    elif channel == "lesson":
        items = memory.match_lessons(
            case["query"],
            case["family"],
            limit=limit,
            project_id=1,
        )
    else:
        assert channel == "claim"
        items = memory.current_claims(case["query"], limit=limit)

    conditioned: list[str] = []
    for item in items[:limit]:
        raw_memory_id = item.get("memory_id")
        assert isinstance(raw_memory_id, int)
        conditioned.append(
            db_to_fixture.get(raw_memory_id, f"unknown-memory-{raw_memory_id}")
        )
    return conditioned


def _qualified(case_id: str, record_ids: list[str]) -> list[str]:
    return [f"{case_id}::{record_id}" for record_id in record_ids]


def _score_case_subset(
    case_results: list[dict[str, Any]],
) -> dict[str, Any]:
    relevant: list[str] = []
    conditioned: list[str] = []
    no_hit_total = 0
    no_hit_correct = 0
    for result in case_results:
        case_id = str(result["case_id"])
        relevant.extend(_qualified(case_id, result["relevant_ids"]))
        conditioned.extend(_qualified(case_id, result["conditioned_ids"]))
        if not result["relevant_ids"]:
            no_hit_total += 1
            no_hit_correct += int(not result["conditioned_ids"])
    score = evaluate_response_conditioned_retrieval(relevant, conditioned)
    return {
        "precision": score["response_conditioned_precision"],
        "recall": score["response_conditioned_recall"],
        "hits": score["hit_count"],
        "relevant": score["relevant_count"],
        "conditioned": score["conditioned_count"],
        "no_hit_accuracy": (
            no_hit_correct / no_hit_total if no_hit_total else None
        ),
        "no_hit_correct": no_hit_correct,
        "no_hit_total": no_hit_total,
    }


def _score_response_conditioned_holdout(
    fixture: dict[str, Any],
    case_results: list[dict[str, Any]],
    forbidden_ids: set[str],
) -> dict[str, Any]:
    aggregate = _score_case_subset(case_results)
    leakage_occurrences = [
        {
            "case_id": result["case_id"],
            "record_id": record_id,
        }
        for result in case_results
        for record_id in result["conditioned_ids"]
        if record_id in forbidden_ids or record_id.startswith("unknown-memory-")
    ]
    misses = [
        {
            "case_id": result["case_id"],
            "missing_ids": sorted(
                set(result["relevant_ids"]) - set(result["conditioned_ids"])
            ),
            "conditioned_ids": result["conditioned_ids"],
        }
        for result in case_results
        if set(result["relevant_ids"]) - set(result["conditioned_ids"])
    ]
    unexpected_no_hits = [
        {
            "case_id": result["case_id"],
            "conditioned_ids": result["conditioned_ids"],
        }
        for result in case_results
        if not result["relevant_ids"] and result["conditioned_ids"]
    ]
    channels = {
        channel: _score_case_subset(
            [result for result in case_results if result["channel"] == channel]
        )
        for channel in ("general", "lesson", "claim")
    }
    aggregate["leakage"] = len(leakage_occurrences)
    aggregate["leakage_occurrences"] = leakage_occurrences
    aggregate["misses"] = misses
    aggregate["unexpected_no_hits"] = unexpected_no_hits
    aggregate["channels"] = channels
    aggregate["gates"] = {
        "precision": float(fixture["scoring"]["precision_gate"]),
        "recall": float(fixture["scoring"]["recall_gate"]),
        "no_hit_accuracy": float(fixture["scoring"]["no_hit_accuracy_gate"]),
        "leakage": int(fixture["scoring"]["leakage_gate"]),
    }
    return aggregate


def test_frozen_synthetic_response_conditioned_holdout_v3(tmp_path: Path) -> None:
    fixture = _load_frozen_fixture()
    _validate_fixture_contract(fixture)
    with Memory(tmp_path / "holdout-v3.db") as memory:
        db_to_fixture, forbidden_ids = _populate_corpus(
            memory,
            fixture["records"],
        )
        case_results = [
            {
                "case_id": str(case["id"]),
                "channel": str(case["channel"]),
                "relevant_ids": [str(item) for item in case["relevant_ids"]],
                "conditioned_ids": _conditioned_fixture_ids(
                    memory,
                    case,
                    db_to_fixture,
                ),
            }
            for case in fixture["cases"]
        ]

    metrics = _score_response_conditioned_holdout(
        fixture,
        case_results,
        forbidden_ids,
    )
    report = {
        "seal": {
            "fixture_sha256": FROZEN_FIXTURE_SHA256,
            "scorer_region_sha256": FROZEN_SCORER_REGION_SHA256,
        },
        "corpus_records": len(fixture["records"]),
        "cases": len(fixture["cases"]),
        "case_features": dict(sorted(Counter(
            feature
            for case in fixture["cases"]
            for feature in case.get("features", [])
        ).items())),
        "metrics": metrics,
    }
    print("HOLDOUT_V3_FIRST_RUN=" + json.dumps(report, sort_keys=True))

    gates = metrics["gates"]
    assert metrics["precision"] is not None
    assert metrics["recall"] is not None
    assert metrics["no_hit_accuracy"] is not None
    assert metrics["precision"] >= gates["precision"], metrics
    assert metrics["recall"] >= gates["recall"], metrics
    assert metrics["no_hit_accuracy"] >= gates["no_hit_accuracy"], metrics
    assert metrics["leakage"] == gates["leakage"], metrics

# === END FROZEN MEMORY RETRIEVAL HOLDOUT V3 SCORER ===

import tempfile
import unittest


class FrozenMemoryRetrievalHoldoutV3Tests(unittest.TestCase):
    def test_frozen_response_conditioned_holdout_v3(self) -> None:
        self.assertTrue(
            __debug__,
            "the frozen V3 seal and scoring gates require non-optimized Python",
        )
        with tempfile.TemporaryDirectory(prefix="jarvis-holdout-v3-") as directory:
            test_frozen_synthetic_response_conditioned_holdout_v3(Path(directory))


if __name__ == "__main__":
    unittest.main()
