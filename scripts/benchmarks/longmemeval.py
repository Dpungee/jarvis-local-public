"""The LongMemEval runner.

Format, verified 2026-09-04: a JSON array of 500 instances, each
``question_id``, ``question_type``, ``question``, ``answer``,
``question_date``, ``haystack_session_ids``, ``haystack_dates``,
``haystack_sessions`` and ``answer_session_ids``; evidence turns carry
``has_answer: true``; the thirty abstention instances have ids suffixed
``_abs``; five abilities (information extraction, multi-session reasoning,
knowledge updates, temporal reasoning, abstention).

We use none of the upstream code -- only the JSON file -- so the runner adds no
dependency.  ``longmemeval_oracle`` (evidence sessions only) is the honest
control arm: **degradation = score(oracle) - score(s)**, both measured by us on
the same day with the same model.  Any other form of that number would be a
comparison we did not run.
"""

from __future__ import annotations

import random
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from .cache import DatasetError, iter_json_array
from .driver import Case, Instance, Session, Turn
from . import scoring

BENCHMARK = "longmemeval_s"
GROUP_KEY = "type"
ABSTENTION_SUFFIX = "_abs"
TEMPORAL_TYPES = frozenset({"temporal-reasoning", "temporal_reasoning"})


def _turns(raw_session: Any) -> tuple[Turn, ...]:
    turns: list[Turn] = []
    if not isinstance(raw_session, list):
        return ()
    for entry in raw_session:
        if not isinstance(entry, Mapping):
            continue
        role = str(entry.get("role") or "").strip().casefold()
        content = str(entry.get("content") or "").strip()
        if not content:
            continue
        # ``messages`` persists only user and assistant rows; anything else in
        # the source (a system preamble, say) is folded onto the user side
        # rather than dropped, so no haystack byte is silently lost.
        turns.append(Turn(role="assistant" if role == "assistant" else "user", content=content))
    return tuple(turns)


def to_instance(record: Mapping[str, Any]) -> Instance | None:
    """Map one upstream record onto the driver's shapes, or skip it."""

    question_id = str(record.get("question_id") or "").strip()
    question = str(record.get("question") or "").strip()
    if not question_id or not question:
        return None
    question_type = str(record.get("question_type") or "unknown").strip() or "unknown"
    gold = str(record.get("answer") or "").strip()
    gold_abstention = question_id.endswith(ABSTENTION_SUFFIX)
    raw_sessions = record.get("haystack_sessions") or []
    session_ids = list(record.get("haystack_session_ids") or [])
    dates = list(record.get("haystack_dates") or [])
    sessions: list[Session] = []
    for index, raw_session in enumerate(raw_sessions):
        turns = _turns(raw_session)
        if not turns:
            continue
        sessions.append(
            Session(
                session_id=str(session_ids[index]) if index < len(session_ids) else f"s{index}",
                date=str(dates[index]) if index < len(dates) else "",
                turns=turns,
            )
        )
    case = Case(
        case_id=question_id,
        question=question,
        gold=gold,
        kind=question_type,
        gold_abstention=gold_abstention,
        metadata={"question_date": str(record.get("question_date") or "")},
    )
    return Instance(
        instance_id=question_id,
        sessions=tuple(sessions),
        cases=(case,),
        metadata={"question_type": question_type},
    )


def load(path: Path, *, limit: int | None = None) -> list[Instance]:
    """Stream the dataset file and return its instances."""

    instances: list[Instance] = []
    for record in iter_json_array(Path(path)):
        if not isinstance(record, Mapping):
            raise DatasetError(
                "every LongMemEval element must be an object", code="dataset_malformed"
            )
        instance = to_instance(record)
        if instance is not None:
            instances.append(instance)
        if limit is not None and len(instances) >= limit:
            break
    if not instances:
        raise DatasetError(f"{path} yielded no usable instances", code="dataset_empty")
    return instances


def stratified_sample(
    instances: Sequence[Instance],
    *,
    n: int | None,
    seed: int,
    key: str = "question_type",
) -> list[Instance]:
    """A reproducible stratified subset, published with its seed and its ids.

    Round-robin over the strata after a seeded shuffle of each, so every
    ability appears before any ability appears twice and the selection is a
    pure function of ``(instances, n, seed)``.
    """

    ordered = sorted(instances, key=lambda instance: instance.instance_id)
    if n is None or n >= len(ordered):
        return ordered
    if n <= 0:
        return []
    strata: dict[str, list[Instance]] = {}
    for instance in ordered:
        strata.setdefault(str(instance.metadata.get(key, "unknown")), []).append(instance)
    rng = random.Random(seed)
    for bucket in strata.values():
        rng.shuffle(bucket)
    picked: list[Instance] = []
    names = sorted(strata)
    while len(picked) < n:
        progressed = False
        for name in names:
            bucket = strata[name]
            if not bucket:
                continue
            picked.append(bucket.pop())
            progressed = True
            if len(picked) >= n:
                break
        if not progressed:
            break
    return sorted(picked, key=lambda instance: instance.instance_id)


def iter_cases(instances: Sequence[Instance]) -> Iterator[tuple[Instance, Case]]:
    for instance in instances:
        for case in instance.cases:
            yield instance, case


def score_row(
    instance: Instance,
    case: Case,
    outcome: Any,
    *,
    judge_verdict: str | None = None,
    benchmark: str = BENCHMARK,
) -> dict[str, Any]:
    """One published row: ids, enums, numbers.  Never question or answer text.

    ``benchmark`` is passed in rather than taken from the module constant so a
    ``longmemeval-shape`` run is labelled with **its own** name.  A synthetic
    run is not a run of the benchmark it is shaped like, and a row that said
    otherwise would be the exact dishonesty rule 8 forbids.
    """

    verdict = scoring.deterministic_verdict(
        outcome.reply,
        case.gold,
        gold_abstention=case.gold_abstention,
        temporal=case.kind in TEMPORAL_TYPES,
    )
    row: dict[str, Any] = {
        "case_id": case.case_id,
        "instance_id": instance.instance_id,
        "benchmark": benchmark,
        "type": case.kind,
        "det": verdict.correct,
        "abstained": verdict.abstained,
        # Whether the reply still stated a value once its hedge was removed.
        # On an ``_abs`` id that is the whole measurement, so it is reported
        # rather than absorbed into the pass rate.
        "asserted": verdict.asserted,
        "gold_abstention": case.gold_abstention,
        "latency_ms": outcome.latency_ms,
        "prompt_tokens": outcome.prompt_tokens,
        "completion_tokens": outcome.completion_tokens,
        "tool_calls": outcome.tool_calls,
        "status": outcome.status,
        "model": outcome.model,
    }
    if judge_verdict is not None:
        row["judge"] = judge_verdict
    if outcome.error_code:
        row["error_code"] = outcome.error_code[:64]
    if outcome.model_reported:
        # The metrics table and AgentResult disagreed; publish both rather
        # than resolve it silently in favour of either.
        row["model_reported"] = outcome.model_reported[:64]
    return row


def degradation(main: Mapping[str, Any], control: Mapping[str, Any]) -> float | None:
    """score(oracle) - score(s), the only honest form of the degradation claim."""

    oracle = control.get("aggregate", {}).get("overall", {}).get("deterministic")
    full = main.get("aggregate", {}).get("overall", {}).get("deterministic")
    if oracle is None or full is None:
        return None
    return round(float(oracle) - float(full), 4)
