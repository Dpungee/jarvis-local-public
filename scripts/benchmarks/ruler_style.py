"""A RULER-style long-context stress that generates everything it needs.

We do not run NVIDIA's harness, for three reasons and only one of them is the
licence (which is Apache-2.0 and fine):

* its dependency set (``nemo-toolkit[all]``, ``tritonclient[all]``,
  ``transformer_engine[pytorch]``, ``vllm==0.5.4``) is Linux/CUDA-only and does
  not install on this host;
* it drives a **served model's** context window, while M5 measures an
  **agent's memory** -- publishing a number about Claude Sonnet under Jarvis's
  name would be dishonest; and
* its ``essay`` haystack is Paul Graham's copyrighted prose, which is not ours
  to fetch and re-emit.

So the haystack here is a **seeded fictional generator**: no external corpus, no
network, no copyright question, and -- methodologically better -- the haystack
stays fixed while the length varies, which is what makes the depth sweep read
as a curve rather than as noise.

**Two arms, always side by side.**  ``direct`` puts the whole haystack in one
prompt and measures the provider; ``jarvis`` ingests it as transcript across
sessions and asks in a fresh conversation, measuring the memory stack.  The
interesting number is ``jarvis - direct`` as length grows.  Needle depth is
swept over {0, 0.25, 0.5, 0.75, 1.0} so the Lost-in-the-Middle U-curve is
visible or provably absent: the curve, not a scalar, is the claim.

Lengths are approximated as characters divided by four and are **stated as an
approximation** -- this package ships no tokenizer and will not pretend to one.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Any, Sequence

from .driver import Case, Instance, Session, Turn
from . import scoring

BENCHMARK = "ruler_style"
GROUP_KEY = "task"
ARMS = ("jarvis", "direct")
TASKS = ("niah_single", "niah_multikey", "niah_multivalue", "niah_multiquery", "vt", "cwe")
LENGTHS = (4096, 8192, 16384, 32768, 65536)
DEFAULT_LENGTHS = (4096, 8192, 16384, 32768)
DEPTHS = (0.0, 0.25, 0.5, 0.75, 1.0)
CHARS_PER_TOKEN = 4
TURN_CHARS = 900

_SYLLABLES = (
    "kes", "trel", "mor", "vane", "quil", "dra", "seln", "harb", "orl", "fen",
    "wick", "gal", "mir", "tos", "bren", "ald", "cyr", "veth", "nol", "pra",
    "skel", "tarn", "umb", "yarr", "zeph", "ond", "lir", "hask", "corv", "duns",
)
_FRAME = (
    "The {noun} inventory for the {other} district was reconciled without incident.",
    "A quarterly note records that the {noun} schedule now precedes the {other} rotation.",
    "Field staff logged the {noun} reading beside the {other} marker, as usual.",
    "No change was reported for the {noun} allocation or the {other} allowance.",
    "The {noun} register lists the {other} entry immediately after the seasonal audit.",
    "Routine maintenance on the {noun} array left the {other} circuit untouched.",
)


class RulerError(RuntimeError):
    """A closed-reason refusal from the RULER-style generator."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Sample:
    """One generated stress case with the material both arms need."""

    case: Case
    context: str
    task: str
    length: int
    depth: float
    values: tuple[str, ...]

    @property
    def approx_tokens(self) -> int:
        return len(self.context) // CHARS_PER_TOKEN


def _word(rng: random.Random) -> str:
    return "".join(rng.choice(_SYLLABLES) for _ in range(rng.randint(2, 3)))


def _filler(rng: random.Random, chars: int) -> list[str]:
    """Fictional prose, generated, of at least ``chars`` characters."""

    sentences: list[str] = []
    total = 0
    while total < chars:
        sentence = rng.choice(_FRAME).format(noun=_word(rng), other=_word(rng))
        sentences.append(sentence)
        total += len(sentence) + 1
    return sentences


def _insert(sentences: list[str], needles: Sequence[str], depth: float) -> list[str]:
    """Place the needles at ``depth`` through the haystack, in order."""

    if not 0.0 <= depth <= 1.0:
        raise RulerError(f"depth must be in [0, 1], not {depth}", code="bad_depth")
    position = int(round(depth * len(sentences)))
    position = max(0, min(len(sentences), position))
    return sentences[:position] + list(needles) + sentences[position:]


def generate_sample(
    *,
    task: str,
    length: int,
    depth: float,
    seed: int,
    index: int = 0,
) -> Sample:
    """Build one sample.  A pure function of its arguments -- reseed and repeat."""

    if task not in TASKS:
        raise RulerError(f"unknown task {task!r}; known: {TASKS}", code="unknown_task")
    rng = random.Random(f"{seed}\0{task}\0{length}\0{depth}\0{index}")
    target_chars = max(256, int(length) * CHARS_PER_TOKEN)
    needles: list[str] = []
    values: list[str] = []
    key = _word(rng)

    if task == "niah_single":
        value = str(rng.randint(100000, 999999))
        needles.append(f"The magic register number for {key} is {value}.")
        values.append(value)
        question = f"What is the magic register number for {key}?"
    elif task == "niah_multikey":
        value = str(rng.randint(100000, 999999))
        needles.append(f"The magic register number for {key} is {value}.")
        for _ in range(4):
            distractor = _word(rng)
            needles.append(
                f"The magic register number for {distractor} is {rng.randint(100000, 999999)}."
            )
        rng.shuffle(needles)
        values.append(value)
        question = f"What is the magic register number for {key}?"
    elif task == "niah_multivalue":
        values = [str(rng.randint(100000, 999999)) for _ in range(4)]
        for value in values:
            needles.append(f"One magic register number for {key} is {value}.")
        question = f"List every magic register number recorded for {key}."
    elif task == "niah_multiquery":
        keys = [key] + [_word(rng) for _ in range(3)]
        for name in keys:
            value = str(rng.randint(100000, 999999))
            values.append(value)
            needles.append(f"The magic register number for {name} is {value}.")
        question = (
            "What are the magic register numbers for "
            + ", ".join(keys[:-1])
            + f" and {keys[-1]}?"
        )
    elif task == "vt":
        # L-10: asking only for the last variable made "full-chain match" a
        # single containment test wearing a bigger name -- every variable holds
        # the same value.  RULER's own shape asks the other direction: given the
        # value, name **every** variable that carries it.  The gold is then the
        # whole chain, each link has to be followed to be named, and every gold
        # token really is in the haystack.
        seed_value = str(rng.randint(100000, 999999))
        variables = [f"{key}{step}" for step in range(1, 6)]
        needles.append(f"Variable {variables[0]} is set to {seed_value}.")
        for previous, current in zip(variables, variables[1:]):
            needles.append(f"Variable {current} takes the value of variable {previous}.")
        values = list(variables)
        question = (
            f"Which variables hold the value {seed_value}? Name every one of them."
        )
    else:  # cwe
        common = _word(rng)
        rare = [_word(rng) for _ in range(6)]
        for _ in range(9):
            needles.append(f"The audit word {common} was recorded again this cycle.")
        for word in rare:
            needles.append(f"The audit word {word} was recorded once this cycle.")
        rng.shuffle(needles)
        values = [common]
        question = "Which audit word was recorded most often?"

    needle_chars = sum(len(item) + 1 for item in needles)
    sentences = _filler(rng, max(0, target_chars - needle_chars))
    context = " ".join(_insert(sentences, needles, depth))
    case = Case(
        case_id=f"{task}-{length}-{int(depth * 100):03d}-{index:03d}",
        question=question,
        gold=values[0] if values else "",
        kind=task,
        metadata={"length": length, "depth": depth, "task": task, "values": tuple(values)},
    )
    return Sample(
        case=case,
        context=context,
        task=task,
        length=length,
        depth=depth,
        values=tuple(values),
    )


def generate(
    *,
    tasks: Sequence[str] = TASKS,
    lengths: Sequence[int] = DEFAULT_LENGTHS,
    depths: Sequence[float] = DEPTHS,
    samples_per_cell: int = 20,
    seed: int = 20260904,
) -> list[Sample]:
    """The full grid, in a stable order."""

    produced: list[Sample] = []
    for task in tasks:
        for length in lengths:
            for depth in depths:
                for index in range(samples_per_cell):
                    produced.append(
                        generate_sample(
                            task=task, length=length, depth=depth, seed=seed, index=index
                        )
                    )
    return produced


def as_instance(sample: Sample, *, turn_chars: int = TURN_CHARS) -> Instance:
    """Split one haystack into dated sessions for the ``jarvis`` arm."""

    text = sample.context
    chunks = [text[start : start + turn_chars] for start in range(0, len(text), turn_chars)] or [""]
    turns = [Turn(role="user" if index % 2 == 0 else "assistant", content=chunk)
             for index, chunk in enumerate(chunks)]
    sessions: list[Session] = []
    per_session = 8
    for start in range(0, len(turns), per_session):
        block = tuple(turns[start : start + per_session])
        sessions.append(
            Session(
                session_id=f"{sample.case.case_id}#{start // per_session}",
                date="",
                turns=block,
            )
        )
    return Instance(
        instance_id=sample.case.case_id,
        sessions=tuple(sessions),
        cases=(sample.case,),
        metadata={"task": sample.task, "length": sample.length, "depth": sample.depth},
    )


NOT_DELIVERED = "context_exceeded"


def score_row(sample: Sample, outcome: Any, *, arm: str) -> dict[str, Any]:
    """Exact string match; the full chain for ``vt``; every value for multivalue.

    A cell the provider was never shown whole is **not delivered**: ``det`` is
    ``None``, not ``False``.  Missing evidence is not evidence of failure, and
    counting a control we could not run as a control the model failed would
    inflate ``jarvis - direct`` exactly where the grid is most interesting.
    """

    delivered = str(getattr(outcome, "status", "")) != NOT_DELIVERED
    if not delivered:
        correct: bool | None = None
    elif sample.task in {"niah_multivalue", "niah_multiquery"}:
        correct = scoring.all_values_present(outcome.reply, sample.values)
    elif sample.task == "vt":
        correct = scoring.chain_match(outcome.reply, sample.values)
    else:
        correct = scoring.contains_answer(outcome.reply, sample.case.gold)
    row: dict[str, Any] = {
        "case_id": sample.case.case_id,
        "instance_id": sample.case.case_id,
        "benchmark": BENCHMARK,
        "task": sample.task,
        "type": sample.task,
        "arm": arm,
        "length": sample.length,
        "depth": sample.depth,
        "det": correct,
        "abstained": scoring.is_abstention(outcome.reply) if delivered else None,
        "gold_abstention": False,
        "latency_ms": outcome.latency_ms,
        "prompt_tokens": outcome.prompt_tokens,
        "completion_tokens": outcome.completion_tokens,
        "tool_calls": outcome.tool_calls,
        "status": outcome.status,
        "model": outcome.model,
        "delivered_fraction": getattr(outcome, "delivered_fraction", None),
        "prompt_chars": getattr(outcome, "prompt_chars", None),
    }
    if outcome.error_code:
        row["error_code"] = outcome.error_code[:64]
    if getattr(outcome, "model_reported", None):
        row["model_reported"] = str(outcome.model_reported)[:64]
    return row


def delivery_report(rows: Sequence[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Per (arm, length) cell: how much of the intended prompt was delivered."""

    cells: dict[str, dict[str, Any]] = {}
    for row in rows:
        if row.get("arm") is None or row.get("length") is None:
            continue
        key = f"{row['arm']}@{row['length']}"
        cell = cells.setdefault(key, {"n": 0, "not_delivered": 0, "min_fraction": None})
        cell["n"] += 1
        if row.get("status") == NOT_DELIVERED:
            cell["not_delivered"] += 1
        fraction = row.get("delivered_fraction")
        if fraction is not None:
            current = cell["min_fraction"]
            cell["min_fraction"] = fraction if current is None else min(current, fraction)
    return dict(sorted(cells.items()))


def depth_curve(rows: Sequence[dict[str, Any]], *, arm: str) -> dict[str, dict[str, float | None]]:
    """Accuracy per (length, depth) for one arm: the U-curve, not a scalar."""

    curve: dict[str, dict[str, float | None]] = {}
    for row in rows:
        if row.get("arm") != arm:
            continue
        bucket = curve.setdefault(str(row.get("length")), {})
        key = f"{float(row.get('depth', 0.0)):.2f}"
        bucket.setdefault(key, None)
    for length_key, bucket in curve.items():
        for depth_key in bucket:
            subset = [
                row
                for row in rows
                if row.get("arm") == arm
                and str(row.get("length")) == length_key
                and f"{float(row.get('depth', 0.0)):.2f}" == depth_key
            ]
            bucket[depth_key] = scoring.rate(subset, "det")
    return curve


def arm_delta(rows: Sequence[dict[str, Any]]) -> dict[str, float | None]:
    """``jarvis - direct`` per length.  The number this benchmark exists for."""

    lengths = sorted({str(row.get("length")) for row in rows if row.get("length") is not None})
    deltas: dict[str, float | None] = {}
    for length in lengths:
        jarvis = scoring.rate(
            [r for r in rows if r.get("arm") == "jarvis" and str(r.get("length")) == length], "det"
        )
        direct = scoring.rate(
            [r for r in rows if r.get("arm") == "direct" and str(r.get("length")) == length], "det"
        )
        deltas[length] = None if jarvis is None or direct is None else round(jarvis - direct, 4)
    return deltas
