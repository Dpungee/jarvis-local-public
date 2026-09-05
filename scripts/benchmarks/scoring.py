"""Deterministic scoring, the abstention detector, and the optional judge column.

Every function here is a pure function of its arguments: the same reply and the
same gold answer always produce the same verdict, on any host, in any order, and
with no clock, no randomness and no network.  That is what lets a published
number be re-derived from the per-case JSONL months later.

The judged column is published **beside** the deterministic one, never instead
of it, and always with :func:`judge_prompt_sha256`.  The judge sees the
question, the gold answer and the reply -- never the haystack and never the
store.  The paper judged LongMemEval with GPT-4o, so our judged column is not
numerically comparable with theirs and the report says so.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from typing import Callable, Iterable, Mapping, Sequence

# The detector the M1 and M3 live batteries already use, kept verbatim so a
# benchmark's abstention column and the batteries' abstention probes agree.
ABSTENTION_PATTERN = re.compile(
    r"not recorded"
    r"|no (?:stored )?(?:project )?fact"
    r"|don't have|do not have"
    r"|no record"
    r"|isn't recorded|is not recorded"
    r"|not stored"
    r"|no information"
    r"|nothing (?:is )?(?:stored|recorded)"
    r"|i don't know|i do not know"
    r"|unable to (?:find|answer)",
    re.IGNORECASE,
)

_ARTICLES = frozenset({"a", "an", "the"})
_PUNCTUATION_RE = re.compile(r"[^\w\s]", re.UNICODE)
_WHITESPACE_RE = re.compile(r"\s+")
_ISO_DATE_RE = re.compile(r"\b(\d{4})[-/](\d{1,2})[-/](\d{1,2})\b")
_US_DATE_RE = re.compile(r"\b(\d{1,2})[/](\d{1,2})[/](\d{4})\b")
_MONTHS = {
    name: index
    for index, name in enumerate(
        (
            "january", "february", "march", "april", "may", "june",
            "july", "august", "september", "october", "november", "december",
        ),
        start=1,
    )
}
_MONTH_NAME_RE = re.compile(
    r"\b(" + "|".join(_MONTHS) + r")\w*\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\b",
    re.IGNORECASE,
)
_DAY_MONTH_RE = re.compile(
    r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(" + "|".join(_MONTHS) + r")\w*\.?,?\s+(\d{4})\b",
    re.IGNORECASE,
)


def normalise(text: str) -> str:
    """NFKC, casefold, strip punctuation and articles, collapse whitespace."""

    folded = unicodedata.normalize("NFKC", str(text)).casefold()
    stripped = _PUNCTUATION_RE.sub(" ", folded)
    words = [word for word in _WHITESPACE_RE.split(stripped) if word and word not in _ARTICLES]
    return " ".join(words)


def tokens(text: str) -> list[str]:
    """The normalised token list both F1 and containment work over."""

    normalised = normalise(text)
    return normalised.split() if normalised else []


def normalise_dates(text: str) -> str:
    """Rewrite every recognisable date into ``YYYY-MM-DD`` before comparison.

    Temporal-reasoning answers are the category where a correct reply loses to
    formatting, so the deterministic column canonicalises dates rather than
    leaving them to the judge.
    """

    def _iso(match: re.Match[str]) -> str:
        year, month, day = match.group(1), int(match.group(2)), int(match.group(3))
        return f"{year}-{month:02d}-{day:02d}"

    def _us(match: re.Match[str]) -> str:
        month, day, year = int(match.group(1)), int(match.group(2)), match.group(3)
        return f"{year}-{month:02d}-{day:02d}"

    def _named(match: re.Match[str]) -> str:
        month = _MONTHS[match.group(1).casefold()]
        return f"{match.group(3)}-{month:02d}-{int(match.group(2)):02d}"

    def _day_named(match: re.Match[str]) -> str:
        month = _MONTHS[match.group(2).casefold()]
        return f"{match.group(3)}-{month:02d}-{int(match.group(1)):02d}"

    rewritten = _MONTH_NAME_RE.sub(_named, str(text))
    rewritten = _DAY_MONTH_RE.sub(_day_named, rewritten)
    rewritten = _US_DATE_RE.sub(_us, rewritten)
    return _ISO_DATE_RE.sub(_iso, rewritten)


def is_abstention(reply: str) -> bool:
    """Whether a reply declines rather than answers."""

    return bool(ABSTENTION_PATTERN.search(str(reply)))


def contains_answer(reply: str, gold: str, *, temporal: bool = False) -> bool:
    """Normalised containment: the gold answer appears in the reply.

    Containment rather than exact match is the standard LongMemEval-style
    deterministic column, because a correct reply is a sentence and the gold is
    a fragment.  A gold answer of a single short token is matched on a word
    boundary so ``9090`` does not match ``19090``.
    """

    if not str(gold).strip():
        return False
    reply_text = str(reply)
    gold_text = str(gold)
    if temporal:
        reply_text = normalise_dates(reply_text)
        gold_text = normalise_dates(gold_text)
    haystack_tokens = normalise(reply_text).split()
    needle_tokens = normalise(gold_text).split()
    if not needle_tokens:
        return False
    # Token-subsequence rather than raw substring, so gold "9090 main" no
    # longer matches reply "19090 maine road": both ends of a multi-token
    # needle now respect a word boundary, as the single-token path already did.
    span = len(needle_tokens)
    for start in range(0, len(haystack_tokens) - span + 1):
        if haystack_tokens[start : start + span] == needle_tokens:
            return True
    return False


def exact_match(reply: str, gold: str) -> bool:
    """Normalised equality, for the string-exact tasks."""

    return normalise(reply) == normalise(gold)


def token_f1(reply: str, gold: str) -> float:
    """Token-overlap F1 over the normalised token lists."""

    predicted = tokens(reply)
    truth = tokens(gold)
    if not predicted or not truth:
        return 1.0 if predicted == truth else 0.0
    overlap = Counter(predicted) & Counter(truth)
    shared = sum(overlap.values())
    if shared == 0:
        return 0.0
    precision = shared / len(predicted)
    recall = shared / len(truth)
    return 2 * precision * recall / (precision + recall)


def all_values_present(reply: str, values: Sequence[str]) -> bool:
    """Every listed value must appear.  The ``niah_multivalue`` shape."""

    return bool(values) and all(contains_answer(reply, value) for value in values)


def chain_match(reply: str, chain: Sequence[str]) -> bool:
    """Every link of a variable-tracking chain must appear, in any order."""

    return all_values_present(reply, chain)


# ---------------------------------------------------------------------------
# The deterministic column
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DeterministicVerdict:
    correct: bool
    abstained: bool
    reason: str
    asserted: bool = False


# A declarative value left behind once the hedge is removed: a run of digits, a
# quoted span, or a copula followed by a content word.  Deliberately narrow --
# it decides an abstention case, so a false positive costs a correct decline.
_DIGIT_RUN_RE = re.compile(r"\b\d[\d,.:/-]*\b")
_QUOTED_SPAN_RE = re.compile(r"[\"“‘']([^\"”’']{2,80})[\"”’']")
_COPULA_RE = re.compile(
    r"\b(?:is|was|are|were|equals?)\s+(?!not\b|n't\b)([A-Za-z0-9][\w'-]*)",
    re.IGNORECASE,
)
_HEDGE_ONLY_WORDS = frozenset(
    {
        "recorded", "stored", "known", "available", "unknown", "unclear", "nothing",
        "none", "no", "not", "unable", "unavailable", "uncertain", "unsure", "that",
        "it", "there", "this", "anything", "something", "sure", "certain", "clear",
        "mentioned", "documented", "logged", "captured", "present", "absent",
    }
)
# A bare answer left standing beside a hedge -- "Paris. I don't know if that is
# stored, but Paris." -- carries no digit, no quotation and no copula, so it
# needs its own signal: a very short remainder whose content word is a name or
# a number.  Bounded to four words so ordinary prose around a decline ("I
# checked the project facts.") is not mistaken for an answer.
_SHORT_ANSWER_MAX_WORDS = 4
_FILLER_WORDS = frozenset(
    {
        "sorry", "apologies", "unfortunately", "hmm", "ok", "okay", "yes", "no",
        "none", "nothing", "unknown", "unclear", "i", "we", "thanks", "regrettably",
    }
)


# Clause boundaries, not sentence boundaries: the interesting replies put the
# answer and the hedge in one sentence -- "The answer is Paris, though I do not
# have a record of that." -- and a sentence-level split throws the answer away
# with the hedge.
_CLAUSE_SPLIT_RE = re.compile(
    r"[.!?;]+\s+|\n+|\s+--+\s+|\s+[–—]\s+"
    r"|,\s+(?:though|but|although|however|so|and|yet|while|whereas)\s+",
    re.IGNORECASE,
)


def strip_abstention_clauses(reply: str) -> str:
    """Remove the clauses that carry the decline, leaving whatever else was said."""

    clauses = _CLAUSE_SPLIT_RE.split(str(reply))
    kept = [
        clause.strip(" .!?;,")
        for clause in clauses
        if clause and not ABSTENTION_PATTERN.search(clause)
    ]
    return " ".join(part for part in kept if part)


def asserts_value(reply: str) -> bool:
    """Whether a reply still states a value once its hedge is taken away.

    This is what makes the abstention column honest.  "The value is four.
    Nothing is stored about it." is not a decline: it is an answer with a
    disclaimer bolted on, and scoring it as a correct abstention rewards
    exactly the behaviour the abstention cases exist to detect.
    """

    remainder = strip_abstention_clauses(reply)
    if not remainder:
        return False
    if _DIGIT_RUN_RE.search(remainder):
        return True
    if _QUOTED_SPAN_RE.search(remainder):
        return True
    for match in _COPULA_RE.finditer(remainder):
        if match.group(1).casefold() not in _HEDGE_ONLY_WORDS:
            return True
    words = [word.strip(".,;:!?\"'()[]") for word in remainder.split()]
    words = [word for word in words if word]
    if 0 < len(words) <= _SHORT_ANSWER_MAX_WORDS:
        for word in words:
            if word.casefold() in _FILLER_WORDS or word.casefold() in _HEDGE_ONLY_WORDS:
                continue
            if word[0].isupper() or any(character.isdigit() for character in word):
                return True
    return False


def deterministic_verdict(
    reply: str,
    gold: str,
    *,
    gold_abstention: bool = False,
    temporal: bool = False,
    values: Sequence[str] | None = None,
    forbidden: Sequence[str] | None = None,
) -> DeterministicVerdict:
    """Score one reply without a model.

    The contradiction rule is **symmetric**.  An answerable case is correct iff
    the reply contains the gold answer and does not decline.  An abstention
    case is correct iff the reply declines **and does not also state a value**:
    a reply that both names a value and says nothing is recorded is not a
    correct abstention, it is the same contradiction seen from the other side,
    and it is precisely what the 30 LongMemEval ``_abs`` ids and LoCoMo
    category 5 exist to catch.  The judge prompt has always stated this rule;
    the deterministic column now applies it too.

    ``forbidden`` lets a caller supply a dataset-grounded contradiction signal
    -- LoCoMo's ``adversarial_answer``, say -- so category 5 needs no heuristic
    at all when the dataset already says what a wrong answer looks like.
    """

    abstained = is_abstention(reply)
    if gold_abstention:
        asserted = asserts_value(reply)
        if not asserted and forbidden:
            asserted = any(contains_answer(reply, item) for item in forbidden if item)
        return DeterministicVerdict(
            correct=bool(abstained and not asserted),
            abstained=abstained,
            reason="abstention-case",
            asserted=asserted,
        )
    if values:
        present = all_values_present(reply, values)
    else:
        present = contains_answer(reply, gold, temporal=temporal)
    return DeterministicVerdict(
        correct=bool(present and not abstained),
        abstained=abstained,
        reason="multivalue" if values else "containment",
        asserted=bool(present),
    )


# ---------------------------------------------------------------------------
# The judge column
# ---------------------------------------------------------------------------

JUDGE_VERDICTS = ("CORRECT", "INCORRECT", "ABSTAINED")
JUDGE_TEMPERATURE = 0.0
JUDGE_SEED = 20260904

# Frozen text.  Editing a single character changes judge_prompt_sha256() and
# therefore the published config hash, which is the point: a judged number is
# only comparable with another judged number produced by the same prompt.
JUDGE_PROMPT_TEMPLATE = """You are grading one answer against a reference answer.

Your entire reply must be one line of exactly this form, with nothing before or
after it:

VERDICT: CORRECT
VERDICT: INCORRECT
VERDICT: ABSTAINED

Choose CORRECT when the answer states the same fact as the reference answer.
Choose INCORRECT when it states something different, or states nothing useful.
Choose ABSTAINED when it declines to answer and does not assert a fact.

Wording, order and extra detail do not matter. Only the asserted fact matters.
An answer that asserts the reference fact and also declines is INCORRECT.
Do not explain your choice. Do not add a reason. One line only.

Question: {question}
Reference answer: {gold}
Answer to grade: {reply}

VERDICT:"""

# M-2: only a line of the exact requested form counts.  Scanning loose prose for
# the first verdict word resolved "INCORRECT. The answer is not correct." to
# CORRECT, biasing the published judged column upward by however often the
# judge explained itself -- which is what a model does when it is uncertain.
_JUDGE_VERDICT_LINE_RE = re.compile(
    r"^\s*(?:VERDICT\s*:)?\s*(CORRECT|INCORRECT|ABSTAINED)\s*\.?\s*$",
    re.IGNORECASE | re.MULTILINE,
)


def judge_prompt_sha256() -> str:
    """The digest every judged number is published with."""

    return hashlib.sha256(JUDGE_PROMPT_TEMPLATE.encode("utf-8")).hexdigest()


def flatten_field(text: str) -> str:
    """Flatten one interpolated field to a single line.

    Every field is untrusted -- the reply is model output and the question and
    gold are dataset text -- and the verdict parser keys on a line of a fixed
    shape, so a field spanning lines could plant a line that looks like one.
    """

    return " ".join(str(text).split())


def build_judge_prompt(question: str, gold: str, reply: str, *, reply_limit: int = 4000) -> str:
    """Assemble the judge prompt.  The judge never sees a haystack or a store."""

    clipped = flatten_field(reply)
    if len(clipped) > reply_limit:
        clipped = clipped[:reply_limit] + " ...[clipped]"
    return JUDGE_PROMPT_TEMPLATE.format(
        question=flatten_field(question),
        gold=flatten_field(gold),
        reply=clipped or "(the model returned nothing)",
    )


def parse_judge_verdict(text: str) -> str:
    """Accept only a verdict line of the exact form the prompt requests.

    Anything else -- an explanation, two verdicts, a hedge -- is ``UNPARSED``,
    which is counted and reported rather than silently dropped out of the
    judged denominator.  Refusing to guess is the only way the judged column
    can be honest about how often the judge did not answer the question asked.
    """

    matched = {
        match.group(1).upper() for match in _JUDGE_VERDICT_LINE_RE.finditer(str(text))
    }
    if len(matched) == 1:
        return matched.pop()
    return "UNPARSED"


JudgeFn = Callable[[str], str]


def judge_case(
    question: str,
    gold: str,
    reply: str,
    judge_fn: JudgeFn | None,
) -> str | None:
    """Run the judge column, or return ``None`` when it is switched off."""

    if judge_fn is None:
        return None
    prompt = build_judge_prompt(question, gold, reply)
    try:
        raw = judge_fn(prompt)
    except Exception:  # noqa: BLE001 - a judge failure must not lose the case
        return "UNPARSED"
    return parse_judge_verdict(raw)


# ---------------------------------------------------------------------------
# Aggregation helpers shared by every runner
# ---------------------------------------------------------------------------


def mean(values: Iterable[float]) -> float | None:
    collected = [float(value) for value in values]
    if not collected:
        return None
    return round(sum(collected) / len(collected), 4)


def percentile(values: Sequence[float], fraction: float) -> float | None:
    """A nearest-rank percentile.  No interpolation, so it is reproducible."""

    collected = sorted(float(value) for value in values)
    if not collected:
        return None
    if not 0.0 < fraction <= 1.0:
        raise ValueError("percentile fraction must be in (0, 1]")
    index = max(0, min(len(collected) - 1, int(round(fraction * len(collected))) - 1))
    return collected[index]


def rate(rows: Sequence[Mapping[str, object]], key: str) -> float | None:
    """The fraction of rows whose ``key`` is truthy, or ``None`` for no rows."""

    present = [row for row in rows if row.get(key) is not None]
    if not present:
        return None
    return round(sum(1 for row in present if row.get(key)) / len(present), 4)
