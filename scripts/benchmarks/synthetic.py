"""Seeded offline fallbacks: ``longmemeval-shape`` and ``locomo-shape``.

These exist for two situations: a host that cannot reach the dataset hosts, and
a smoke run that must exercise the whole pipeline without fetching 264 MiB.
They generate a **fictional** domain from a seed -- no external corpus, no
copyrighted prose, no personal data.

They are reported under **their own names**, never under LongMemEval's or
LoCoMo's.  A synthetic run is not a run of the benchmark it is shaped like, and
saying so is the whole reason the shapes carry a different name.
"""

from __future__ import annotations

import random
from typing import Sequence

from .driver import Case, Instance, Session, Turn

LONGMEMEVAL_SHAPE = "longmemeval-shape"
LOCOMO_SHAPE = "locomo-shape"

ABILITIES = (
    "information-extraction",
    "multi-session-reasoning",
    "knowledge-update",
    "temporal-reasoning",
    "abstention",
)

_SUBJECTS = (
    "Kestrel relay", "Millrace weir", "Harrier box", "Fenwick vault",
    "Talon bridge", "Corvid gate", "Duns ledger", "Orlan mast",
)
_PREDICATES = ("listen port", "gate count", "shift window", "spare quota")
_TOPICS = (
    "the winter rota", "the spare-parts run", "the culvert survey",
    "the relay handover", "the quarterly reconciliation",
)


def _distractor_turns(rng: random.Random, count: int) -> list[Turn]:
    turns: list[Turn] = []
    for index in range(count):
        topic = rng.choice(_TOPICS)
        turns.append(Turn(role="user", content=f"Anything outstanding on {topic}?"))
        turns.append(
            Turn(
                role="assistant",
                content=f"Nothing outstanding on {topic}; the log closed cleanly on pass {index + 1}.",
            )
        )
    return turns


def longmemeval_shape(*, n: int = 25, seed: int = 20260904, sessions_per_case: int = 4) -> list[Instance]:
    """``n`` instances spread evenly across the five abilities."""

    rng = random.Random(seed)
    instances: list[Instance] = []
    for index in range(max(0, int(n))):
        ability = ABILITIES[index % len(ABILITIES)]
        subject = rng.choice(_SUBJECTS)
        predicate = rng.choice(_PREDICATES)
        first = str(rng.randint(1000, 4999))
        second = str(rng.randint(5000, 9999))
        case_id = f"shape-{index:04d}-{ability}"
        sessions: list[Session] = []
        for step in range(sessions_per_case):
            turns = _distractor_turns(rng, 2)
            if step == 0:
                turns.insert(
                    0,
                    Turn(role="user", content=f"For the record, the {subject} {predicate} is {first}."),
                )
                turns.insert(1, Turn(role="assistant", content="Noted."))
            if step == sessions_per_case - 1 and ability == "knowledge-update":
                turns.append(
                    Turn(role="user", content=f"Update: the {subject} {predicate} is now {second}.")
                )
                turns.append(Turn(role="assistant", content="Understood."))
            sessions.append(
                Session(
                    session_id=f"{case_id}#{step}",
                    date=f"2026-0{(step % 9) + 1}-1{step % 9}",
                    turns=tuple(turns),
                )
            )
        if ability == "abstention":
            case = Case(
                case_id=f"{case_id}_abs",
                question=f"What is the {rng.choice(_SUBJECTS)} calibration offset?",
                gold="",
                kind=ability,
                gold_abstention=True,
            )
        elif ability == "knowledge-update":
            case = Case(
                case_id=case_id,
                question=f"What is the {subject} {predicate}?",
                gold=second,
                kind=ability,
            )
        elif ability == "temporal-reasoning":
            case = Case(
                case_id=case_id,
                question=f"What was the {subject} {predicate} first recorded as?",
                gold=first,
                kind=ability,
            )
        else:
            case = Case(
                case_id=case_id,
                question=f"What is the {subject} {predicate}?",
                gold=first,
                kind=ability,
            )
        instances.append(
            Instance(
                instance_id=case.case_id,
                sessions=tuple(sessions),
                cases=(case,),
                metadata={"question_type": ability},
            )
        )
    return instances


def locomo_shape(*, samples: int = 3, questions: int = 8, seed: int = 20260904) -> list[Instance]:
    """A LoCoMo-shaped fallback: two speakers, dated sessions, five categories."""

    rng = random.Random(seed)
    instances: list[Instance] = []
    for sample_index in range(max(0, int(samples))):
        sample_id = f"shape-conv-{sample_index:02d}"
        speaker_a, speaker_b = "Wren", "Alder"
        facts: list[tuple[str, str]] = []
        sessions: list[Session] = []
        for session_index in range(4):
            turns: list[Turn] = []
            for _ in range(3):
                subject = rng.choice(_SUBJECTS)
                value = str(rng.randint(10, 99))
                facts.append((subject, value))
                turns.append(Turn(role="user", content=f"{speaker_a}: I set the {subject} dial to {value}."))
                turns.append(
                    Turn(role="assistant", content=f"{speaker_b}: Right, {subject} at {value}, logged.")
                )
            sessions.append(
                Session(
                    session_id=f"{sample_id}#{session_index}",
                    date=f"2026-0{session_index + 1}-05 09:00:00",
                    turns=tuple(turns),
                )
            )
        cases: list[Case] = []
        for question_index in range(max(0, int(questions))):
            category = (question_index % 5) + 1
            if category == 5:
                cases.append(
                    Case(
                        case_id=f"{sample_id}#{question_index}",
                        question="What did Wren say about the Osprey ledger?",
                        gold="",
                        kind="5",
                        gold_abstention=True,
                        metadata={"qa_index": question_index, "sample_id": sample_id, "category": 5},
                    )
                )
                continue
            subject, value = facts[question_index % len(facts)]
            cases.append(
                Case(
                    case_id=f"{sample_id}#{question_index}",
                    question=f"What did Wren set the {subject} dial to?",
                    gold=value,
                    kind=str(category),
                    metadata={
                        "qa_index": question_index,
                        "sample_id": sample_id,
                        "category": category,
                    },
                )
            )
        instances.append(
            Instance(
                instance_id=sample_id,
                sessions=tuple(sessions),
                cases=tuple(cases),
                metadata={"sample_id": sample_id},
            )
        )
    return instances


def shape_names() -> Sequence[str]:
    return (LONGMEMEVAL_SHAPE, LOCOMO_SHAPE)
