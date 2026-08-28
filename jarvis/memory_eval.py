from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from pathlib import Path
from typing import Any, Mapping, Sequence


FROZEN_RETRIEVAL_FIXTURE_V1_NAME = "memory_retrieval_gold_v1.json"
FROZEN_RETRIEVAL_FIXTURE_V1_SHA256 = (
    "f303e3429ff01ca810defc3d830288713e4e0d633f12cfb3acbb8160631e61ed"
)


class RetrievalFixtureError(ValueError):
    """A frozen retrieval fixture is malformed or internally inconsistent."""


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def retrieval_fixture_sha256(fixture: Mapping[str, Any]) -> str:
    """Return a stable digest so benchmark results name their exact gold set."""
    return hashlib.sha256(_canonical_json(fixture).encode("utf-8")).hexdigest()


def load_retrieval_fixture(path: Path) -> dict[str, Any]:
    """Load and validate a bounded, model-independent retrieval gold set."""
    try:
        parsed = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RetrievalFixtureError(f"Could not load retrieval fixture: {exc}") from exc
    if not isinstance(parsed, dict) or parsed.get("schema_version") != 1:
        raise RetrievalFixtureError("Retrieval fixture schema_version must be 1")
    corpus = parsed.get("corpus")
    cases = parsed.get("cases")
    if not isinstance(corpus, list) or not isinstance(cases, list):
        raise RetrievalFixtureError("Retrieval fixture requires corpus and cases arrays")
    if not 12 <= len(corpus) <= 500 or not 12 <= len(cases) <= 500:
        raise RetrievalFixtureError("Retrieval corpus and cases must be meaningfully bounded")

    corpus_ids: set[str] = set()
    for record in corpus:
        if not isinstance(record, dict):
            raise RetrievalFixtureError("Every corpus record must be an object")
        record_id = str(record.get("id") or "").strip()
        content = str(record.get("content") or "").strip()
        kind = str(record.get("kind") or "").strip()
        if not record_id or record_id in corpus_ids:
            raise RetrievalFixtureError("Corpus IDs must be non-empty and unique")
        if not content or kind not in {"lesson", "claim", "memory"}:
            raise RetrievalFixtureError("Corpus records require content and a supported kind")
        if kind == "lesson":
            if not isinstance(record.get("valid_provenance"), bool):
                raise RetrievalFixtureError(
                    "Lesson corpus records require an explicit provenance label"
                )
            if str(record.get("outcome_status") or "") not in {
                "complete", "incomplete", "failed",
            }:
                raise RetrievalFixtureError(
                    "Lesson corpus records require a supported outcome status"
                )
        if kind == "memory":
            if not str(record.get("memory_kind") or "").strip():
                raise RetrievalFixtureError(
                    "General-memory corpus records require a memory_kind"
                )
            if not isinstance(record.get("valid_provenance"), bool):
                raise RetrievalFixtureError(
                    "General-memory corpus records require an explicit provenance label"
                )
        if "sentinel" in content.casefold():
            raise RetrievalFixtureError("Gold records must not use sentinel-token relevance")
        corpus_ids.add(record_id)
    case_ids: set[str] = set()
    categories: Counter[str] = Counter()
    forbidden_reasons: Counter[str] = Counter()
    positive_families: set[str] = set()
    for case in cases:
        if not isinstance(case, dict):
            raise RetrievalFixtureError("Every retrieval case must be an object")
        case_id = str(case.get("id") or "").strip()
        query = str(case.get("query") or "").strip()
        category = str(case.get("category") or "").strip()
        channel = str(case.get("channel") or "").strip()
        relevant = case.get("relevant_ids", [])
        forbidden = case.get("forbidden", [])
        if not case_id or case_id in case_ids or not query:
            raise RetrievalFixtureError("Case IDs must be unique and queries non-empty")
        if "sentinel" in query.casefold():
            raise RetrievalFixtureError("Gold queries must not use sentinel-token relevance")
        if category not in {"positive", "negative", "safety", "temporal"}:
            raise RetrievalFixtureError(f"Unsupported retrieval category: {category}")
        if channel not in {"lesson", "claim", "memory"}:
            raise RetrievalFixtureError(f"Unsupported retrieval channel: {channel}")
        if not isinstance(relevant, list) or not isinstance(forbidden, list):
            raise RetrievalFixtureError("Relevant and forbidden labels must be arrays")
        relevant_ids = [str(item) for item in relevant]
        if len(relevant_ids) != len(set(relevant_ids)):
            raise RetrievalFixtureError("Relevant IDs must be unique within a case")
        if not set(relevant_ids).issubset(corpus_ids):
            raise RetrievalFixtureError("A relevant ID is absent from the corpus")
        forbidden_ids: set[str] = set()
        for item in forbidden:
            if not isinstance(item, dict):
                raise RetrievalFixtureError("Forbidden labels must be objects")
            forbidden_id = str(item.get("id") or "").strip()
            reason = str(item.get("reason") or "").strip()
            allowed_statuses = item.get("allowed_statuses", [])
            if (
                not forbidden_id
                or forbidden_id not in corpus_ids
                or not reason
                or not isinstance(allowed_statuses, list)
            ):
                raise RetrievalFixtureError("Forbidden labels require an ID and reason")
            forbidden_ids.add(forbidden_id)
            forbidden_reasons[reason] += 1
        if set(relevant_ids).intersection(forbidden_ids):
            raise RetrievalFixtureError("A record cannot be both relevant and forbidden")
        if category == "positive" and not relevant_ids:
            raise RetrievalFixtureError("Positive cases require at least one relevant record")
        if category == "negative" and relevant_ids:
            raise RetrievalFixtureError("Negative cases cannot have relevant records")
        if channel == "lesson":
            family = str(case.get("family") or "").strip()
            if not family:
                raise RetrievalFixtureError("Lesson cases require a family")
            if category == "positive":
                positive_families.add(family)
        case_ids.add(case_id)
        categories[category] += 1

    if len(positive_families) < 6:
        raise RetrievalFixtureError("Gold positives must cover at least six task families")
    if categories["positive"] < 12 or categories["negative"] < 4:
        raise RetrievalFixtureError("Gold set needs substantive positive and negative coverage")
    if not categories["safety"] or not categories["temporal"]:
        raise RetrievalFixtureError("Gold set needs safety and temporal controls")
    required_reasons = {
        "invalid_provenance", "poisoning", "unsuccessful_outcome",
        "wrong_family", "stale_record",
    }
    missing_reasons = required_reasons.difference(forbidden_reasons)
    if missing_reasons:
        raise RetrievalFixtureError(
            "Gold set is missing safety labels: " + ", ".join(sorted(missing_reasons))
        )
    digest = retrieval_fixture_sha256(parsed)
    if (
        Path(path).name == FROZEN_RETRIEVAL_FIXTURE_V1_NAME
        and digest != FROZEN_RETRIEVAL_FIXTURE_V1_SHA256
    ):
        raise RetrievalFixtureError(
            "Frozen retrieval fixture digest does not match the code-pinned v1 corpus"
        )
    parsed["fixture_sha256"] = digest
    return parsed


def _global_safety_labels(fixture: Mapping[str, Any]) -> dict[str, str]:
    """Classify every unsafe corpus record independently of any one query.

    Per-query forbidden labels remain useful for contextual errors such as a
    wrong-family lesson. Invalid provenance, poisoned content, and unsuccessful
    outcomes are different: returning one is a safety leak for *every* query,
    even when the gold case author forgot to repeat that label on the case.
    """
    cases = fixture.get("cases")
    corpus = fixture.get("corpus")
    if not isinstance(cases, list) or not isinstance(corpus, list):
        raise RetrievalFixtureError("Fixture corpus and cases are unavailable")
    poisoned = {
        str(forbidden.get("id") or "")
        for case in cases
        if isinstance(case, Mapping)
        for forbidden in case.get("forbidden", [])
        if isinstance(forbidden, Mapping)
        and str(forbidden.get("reason") or "") == "poisoning"
    }
    labels: dict[str, str] = {}
    for record in corpus:
        if not isinstance(record, Mapping):
            continue
        record_id = str(record.get("id") or "")
        if record_id in poisoned:
            labels[record_id] = "poisoning"
        elif record.get("valid_provenance") is False:
            labels[record_id] = "invalid_provenance"
        elif (
            str(record.get("kind") or "") == "lesson"
            and str(record.get("outcome_status") or "") != "complete"
        ):
            labels[record_id] = "unsuccessful_outcome"
    return labels


def _normalise_result(value: Any) -> dict[str, str]:
    if isinstance(value, str):
        return {"id": value, "status": ""}
    if not isinstance(value, Mapping):
        raise RetrievalFixtureError("Ranked results must be IDs or result objects")
    result_id = str(value.get("id") or "").strip()
    if not result_id:
        raise RetrievalFixtureError("Every ranked result needs an ID")
    return {
        "id": result_id,
        "status": str(value.get("status") or "").strip().casefold(),
    }


def evaluate_retrieval_rankings(
    fixture: Mapping[str, Any],
    rankings: Mapping[str, Sequence[Any]],
    *,
    k: int | None = None,
) -> dict[str, Any]:
    """Score frozen relevance and safety labels without invoking a model.

    Precision@k uses the conventional fixed-k denominator, including when the
    retriever abstains or returns fewer than k results. Recall divides by the
    complete gold set. Safety leakage is scored independently so returning
    nothing cannot make a dangerous retriever look precise.
    """
    cases = fixture.get("cases")
    if not isinstance(cases, list):
        raise RetrievalFixtureError("Fixture cases are unavailable")
    cutoff = int(k if k is not None else fixture.get("k", 3))
    if not 1 <= cutoff <= 20:
        raise RetrievalFixtureError("Retrieval cutoff must be between 1 and 20")

    precision_values: list[float] = []
    recall_values: list[float] = []
    reciprocal_ranks: list[float] = []
    negative_results: list[bool] = []
    irrelevant_results: list[bool] = []
    leakage_by_reason: Counter[str] = Counter()
    leakage_by_corpus_kind: Counter[str] = Counter()
    per_case: list[dict[str, Any]] = []
    corpus_kind_by_id = {
        str(record.get("id") or ""): str(record.get("kind") or "")
        for record in fixture.get("corpus", [])
        if isinstance(record, Mapping)
    }
    corpus_ids = set(corpus_kind_by_id)
    global_safety = _global_safety_labels(fixture)

    for case in cases:
        case_id = str(case["id"])
        top = [_normalise_result(item) for item in rankings.get(case_id, ())[:cutoff]]
        ranked_ids = [item["id"] for item in top]
        unknown_ids = set(ranked_ids).difference(corpus_ids)
        if unknown_ids:
            raise RetrievalFixtureError(
                "Ranked result IDs are absent from the frozen corpus: "
                + ", ".join(sorted(unknown_ids))
            )
        relevant = {str(item) for item in case.get("relevant_ids", [])}
        hits = [record_id for record_id in ranked_ids if record_id in relevant]
        precision = None
        recall = None
        reciprocal_rank = None
        if relevant:
            precision = len(set(hits)) / cutoff
            recall = len(set(hits)) / len(relevant)
            reciprocal_rank = next(
                (1.0 / rank for rank, record_id in enumerate(ranked_ids, 1)
                 if record_id in relevant),
                0.0,
            )
            precision_values.append(precision)
            recall_values.append(recall)
            reciprocal_ranks.append(reciprocal_rank)
        if str(case.get("category")) == "negative":
            negative_results.append(not ranked_ids)
        if not relevant:
            # This includes negative, safety, and stale-only temporal controls.
            # A stale result may be honestly labelled and therefore not leak,
            # but it is still unrelated to the requested current answer.
            irrelevant_results.append(not ranked_ids)

        case_leaks: list[dict[str, str]] = []
        seen_leaks: set[tuple[str, str]] = set()
        for result in top:
            reason = global_safety.get(result["id"])
            if reason is None:
                continue
            key = (result["id"], reason)
            seen_leaks.add(key)
            leakage_by_reason[reason] += 1
            leakage_by_corpus_kind[corpus_kind_by_id[result["id"]]] += 1
            case_leaks.append({"id": result["id"], "reason": reason})
        for forbidden in case.get("forbidden", []):
            forbidden_id = str(forbidden["id"])
            reason = str(forbidden["reason"])
            if (forbidden_id, global_safety.get(forbidden_id, "")) in seen_leaks:
                continue
            allowed_statuses = {
                str(status).strip().casefold()
                for status in forbidden.get("allowed_statuses", [])
            }
            for result in top:
                if result["id"] != forbidden_id:
                    continue
                if result["status"] and result["status"] in allowed_statuses:
                    continue
                key = (forbidden_id, reason)
                if key in seen_leaks:
                    break
                seen_leaks.add(key)
                leakage_by_reason[reason] += 1
                leakage_by_corpus_kind[corpus_kind_by_id[forbidden_id]] += 1
                case_leaks.append({"id": forbidden_id, "reason": reason})
                break
        per_case.append({
            "id": case_id,
            "category": str(case.get("category")),
            "returned": ranked_ids,
            "relevant_hits": hits,
            "precision_at_k": precision,
            "recall_at_k": recall,
            "reciprocal_rank": reciprocal_rank,
            "irrelevant_no_hit": None if relevant else not ranked_ids,
            "leakage": case_leaks,
        })

    def mean(values: Sequence[float]) -> float:
        return sum(values) / len(values) if values else 0.0

    precision_at_k = mean(precision_values)
    recall_at_k = mean(recall_values)
    mrr = mean(reciprocal_ranks)
    no_hit_accuracy = (
        sum(negative_results) / len(negative_results) if negative_results else 0.0
    )
    irrelevant_no_hit_accuracy = (
        sum(irrelevant_results) / len(irrelevant_results)
        if irrelevant_results else 0.0
    )
    total_leakage = sum(leakage_by_reason.values())
    criteria = fixture.get("exit_criteria", {})
    criteria = criteria if isinstance(criteria, Mapping) else {}
    passes = {
        "precision_at_k": precision_at_k
        >= float(criteria.get("precision_at_3_min", -math.inf)),
        "recall_at_k": recall_at_k
        >= float(criteria.get("recall_at_3_min", -math.inf)),
        "mrr": mrr >= float(criteria.get("mrr_min", -math.inf)),
        "negative_no_hit": no_hit_accuracy
        >= float(criteria.get("negative_no_hit_min", -math.inf)),
        "irrelevant_no_hit": irrelevant_no_hit_accuracy
        >= float(criteria.get("irrelevant_no_hit_min", -math.inf)),
        "safety_leakage": total_leakage
        <= int(criteria.get("safety_leakage_max", total_leakage)),
    }
    return {
        "schema_version": 1,
        "fixture_sha256": str(
            fixture.get("fixture_sha256") or retrieval_fixture_sha256(fixture)
        ),
        "k": cutoff,
        "positive_cases": len(precision_values),
        "negative_cases": len(negative_results),
        "irrelevant_cases": len(irrelevant_results),
        "precision_at_3": round(precision_at_k, 6),
        "recall_at_3": round(recall_at_k, 6),
        "mrr": round(mrr, 6),
        "negative_no_hit_accuracy": round(no_hit_accuracy, 6),
        "irrelevant_no_hit_accuracy": round(irrelevant_no_hit_accuracy, 6),
        "safety_leakage_total": total_leakage,
        "leakage_by_reason": dict(sorted(leakage_by_reason.items())),
        "leakage_by_corpus_kind": dict(sorted(leakage_by_corpus_kind.items())),
        "passes": passes,
        "all_exit_criteria_passed": all(passes.values()),
        "cases": per_case,
    }
