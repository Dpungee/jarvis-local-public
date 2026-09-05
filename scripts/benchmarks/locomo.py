"""The LoCoMo runner.  Measurements only -- never one word of the dataset.

LoCoMo is **CC BY-NC 4.0**: redistribution with attribution, commercial use
prohibited.  Publishing measurements about a dataset is not redistributing it;
publishing its questions is.  So every row this module emits carries
``sample_id``, the question index, the category and the scores, and **no
question text, answer text, dialogue turn, persona, caption or URL**.  The
refusal that keeps a use-restricted dataset out of the cache under a declared
commercial use lives in :func:`scripts.benchmarks.cache.ensure_dataset`, keyed
on the licence class rather than on this benchmark's name, so a future
restricted dataset inherits it for free.

Format, verified 2026-09-04: a JSON array of 10 samples, each ``sample_id`` and
a ``conversation`` object holding ``speaker_a``, ``speaker_b``, ``session_<n>``
turn lists and ``session_<n>_date_time`` stamps, plus a ``qa`` array of
``question`` / ``answer`` / ``category`` (1-5) / ``evidence``.  Multimodal
fields (``img_url``, ``blip_caption``) are ignored and never fetched.

Category 5 is the adversarial category, and it is scored as **abstention**:
correct iff Jarvis declines.  That is the category where the abstention
machinery should show, and the one worth leading with.
"""

from __future__ import annotations

import random
import re
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from .cache import DatasetError, iter_json_array
from .driver import Case, Instance, Session, Turn
from . import scoring

BENCHMARK = "locomo10"
GROUP_KEY = "category"
ADVERSARIAL_CATEGORY = 5
LICENCE_ATTRIBUTION = (
    "LoCoMo (snap-research/locomo) is licensed CC BY-NC 4.0. It was not "
    "redistributed here; only measurements about it are published."
)

_SESSION_RE = re.compile(r"\Asession_(\d+)\Z")


def _session_turns(raw: Any, speaker_a: str) -> tuple[Turn, ...]:
    """Map a LoCoMo session onto persistable rows.

    ``messages`` accepts only ``user`` and ``assistant``, so speaker A becomes
    the user side and speaker B the assistant side, and each turn keeps its
    speaker name as a prefix so the identity survives ingestion.  That is a
    transcript mapping, not a rewrite: no turn is dropped, merged or reordered.
    """

    if not isinstance(raw, list):
        return ()
    turns: list[Turn] = []
    for entry in raw:
        if not isinstance(entry, Mapping):
            continue
        text = str(entry.get("text") or "").strip()
        if not text:
            continue
        speaker = str(entry.get("speaker") or "").strip() or "speaker"
        role = "user" if speaker == speaker_a else "assistant"
        turns.append(Turn(role=role, content=f"{speaker}: {text}"))
    return tuple(turns)


def to_instance(record: Mapping[str, Any]) -> Instance | None:
    sample_id = str(record.get("sample_id") or "").strip()
    conversation = record.get("conversation")
    questions = record.get("qa")
    if not sample_id or not isinstance(conversation, Mapping) or not isinstance(questions, list):
        return None
    speaker_a = str(conversation.get("speaker_a") or "").strip()
    numbered: list[tuple[int, str]] = []
    for key in conversation:
        match = _SESSION_RE.match(str(key))
        if match:
            numbered.append((int(match.group(1)), str(key)))
    sessions: list[Session] = []
    for number, key in sorted(numbered):
        turns = _session_turns(conversation.get(key), speaker_a)
        if not turns:
            continue
        sessions.append(
            Session(
                session_id=f"{sample_id}#{number}",
                date=str(conversation.get(f"{key}_date_time") or ""),
                turns=turns,
            )
        )
    cases: list[Case] = []
    for index, entry in enumerate(questions):
        if not isinstance(entry, Mapping):
            continue
        question = str(entry.get("question") or "").strip()
        if not question:
            continue
        try:
            category = int(entry.get("category"))
        except (TypeError, ValueError):
            category = 0
        gold = str(entry.get("answer") or entry.get("adversarial_answer") or "").strip()
        cases.append(
            Case(
                case_id=f"{sample_id}#{index}",
                question=question,
                gold=gold,
                kind=str(category),
                gold_abstention=category == ADVERSARIAL_CATEGORY,
                metadata={"qa_index": index, "sample_id": sample_id, "category": category},
            )
        )
    if not cases:
        return None
    return Instance(
        instance_id=sample_id,
        sessions=tuple(sessions),
        cases=tuple(cases),
        metadata={"sample_id": sample_id},
    )


def load(path: Path, *, limit: int | None = None) -> list[Instance]:
    instances: list[Instance] = []
    for record in iter_json_array(Path(path)):
        if not isinstance(record, Mapping):
            raise DatasetError("every LoCoMo element must be an object", code="dataset_malformed")
        instance = to_instance(record)
        if instance is not None:
            instances.append(instance)
        if limit is not None and len(instances) >= limit:
            break
    if not instances:
        raise DatasetError(f"{path} yielded no usable samples", code="dataset_empty")
    return instances


def question_count(instances: Sequence[Instance]) -> int:
    """Read the QA count from the file rather than guessing it."""

    return sum(len(instance.cases) for instance in instances)


def stratified_cases(
    instances: Sequence[Instance],
    *,
    n: int | None,
    seed: int,
) -> list[tuple[Instance, Case]]:
    """Select questions round-robin over category, reproducibly from the seed."""

    pairs = [
        (instance, case)
        for instance in sorted(instances, key=lambda item: item.instance_id)
        for case in instance.cases
    ]
    if n is None or n >= len(pairs):
        return pairs
    if n <= 0:
        return []
    strata: dict[str, list[tuple[Instance, Case]]] = {}
    for pair in pairs:
        strata.setdefault(pair[1].kind, []).append(pair)
    rng = random.Random(seed)
    for bucket in strata.values():
        rng.shuffle(bucket)
    picked: list[tuple[Instance, Case]] = []
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
    return sorted(picked, key=lambda pair: pair[1].case_id)


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
    """Ids, categories and scores only.  Enforced again by report.validate_row.

    ``benchmark`` is passed in so a ``locomo-shape`` row is labelled with its
    own name and never with LoCoMo's.
    """

    # For category 5 the dataset's own ``adversarial_answer`` is the gold, and a
    # reply that contains it while declining is a dataset-grounded
    # contradiction -- no heuristic needed on the category the design says to
    # lead with.
    forbidden = (case.gold,) if case.gold_abstention and case.gold else ()
    verdict = scoring.deterministic_verdict(
        outcome.reply,
        case.gold,
        gold_abstention=case.gold_abstention,
        forbidden=forbidden,
    )
    row: dict[str, Any] = {
        "case_id": case.case_id,
        "sample_id": str(instance.instance_id),
        "qa_index": int(case.metadata.get("qa_index", 0)),
        "benchmark": benchmark,
        "category": case.kind,
        "type": case.kind,
        "det": verdict.correct,
        "em": 1.0 if scoring.exact_match(outcome.reply, case.gold) else 0.0,
        "f1": round(scoring.token_f1(outcome.reply, case.gold), 4),
        "abstained": verdict.abstained,
        "asserted": verdict.asserted,
        "gold_abstention": case.gold_abstention,
        "latency_ms": outcome.latency_ms,
        "prompt_tokens": outcome.prompt_tokens,
        "completion_tokens": outcome.completion_tokens,
        "tool_calls": outcome.tool_calls,
        "status": outcome.status,
        "model": outcome.model,
    }
    if case.gold_abstention:
        # An adversarial question has no answer to overlap with; reporting an
        # F1 against an empty or evasive gold would be a number about nothing.
        row["em"] = None
        row["f1"] = None
    if judge_verdict is not None:
        row["judge"] = judge_verdict
    if outcome.error_code:
        row["error_code"] = outcome.error_code[:64]
    if outcome.model_reported:
        row["model_reported"] = outcome.model_reported[:64]
    return row
