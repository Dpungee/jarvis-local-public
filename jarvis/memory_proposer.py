"""Model-assisted proposal of one project fact, grounded in the operator's words.

The deterministic grammar in :mod:`jarvis.memory_extractor` proposes first.
When it finds a licensed statement it cannot split, the local model may be
asked for a triple.  Nothing the model returns is trusted as such: the
``source_span`` must be a whole-token substring of the statement, the subject
and value must be whole-token substrings of that span and of a clause that is
not negated or ruled out, a one-word subject must be a whole noun phrase (not
the tail of a longer name), every predicate word must come from the statement
or from a predicate already stored for that subject, the stored spelling is
copied from the operator's characters, and the result must pass the governed
parser.  The operator then confirms exactly what was shown, and the runtime
keeps its own record of what it showed.  No model writes memory.
"""
from __future__ import annotations

import json
import re
import unicodedata
from typing import Any, Mapping, Sequence

from .memory_extractor import grounding_clauses, predicate_stems, validate_proposal

MAX_PROPOSER_RESPONSE_CHARS = 4_000
PROPOSER_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["subject", "predicate", "value", "source_span"],
    "properties": {
        "subject": {"anyOf": [{"type": "null"}, {"type": "string", "maxLength": 200}]},
        "predicate": {"anyOf": [{"type": "null"}, {"type": "string", "maxLength": 160}]},
        "value": {"anyOf": [{"type": "null"}, {"type": "string", "maxLength": 600}]},
        "source_span": {
            "anyOf": [{"type": "null"}, {"type": "string", "maxLength": 600}]
        },
    },
}
_SYSTEM_PROMPT = (
    "You extract at most one durable project fact from ONE operator sentence. "
    "Reply with JSON only, with exactly the keys subject, predicate, value, "
    "source_span. subject and value must be copied verbatim from the sentence, "
    "and subject must be the whole name of the thing (\"Osprey relay\", not "
    "\"relay\"). predicate is a short attribute noun phrase, preferably reusing "
    "words from the sentence or one of the known predicates. source_span is the "
    "verbatim part of the sentence that states the fact. If the sentence does not "
    "state a durable fact about a named project thing (a service, host, team, "
    "setting, schedule, owner, version, location), or it is about a person's "
    "private life, set every key to null. Never invent, normalize, or infer values."
)
_DETERMINERS = frozenset({
    "the", "a", "an", "our", "my", "this", "that", "these", "those", "its",
})


def proposer_response_schema() -> dict[str, Any]:
    return json.loads(json.dumps(PROPOSER_RESPONSE_SCHEMA))


def build_proposer_messages(
    statement: str,
    *,
    known_subjects: Sequence[str] = (),
    known_predicates: Sequence[str] = (),
) -> list[dict[str, str]]:
    """Messages for one bounded, tool-free proposal call."""
    hints: list[str] = []
    subjects = [str(item) for item in known_subjects if str(item).strip()][:8]
    predicates = [str(item) for item in known_predicates if str(item).strip()][:12]
    if subjects:
        hints.append("Known subjects: " + "; ".join(subjects))
    if predicates:
        hints.append("Known predicates: " + "; ".join(predicates))
    hint_text = ("\n" + "\n".join(hints)) if hints else ""
    return [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {
            "role": "user",
            "content": f"Sentence: {' '.join(str(statement).split())}{hint_text}",
        },
    ]


def _norm(text: Any) -> str:
    """NFKC then lower-case, the same view the governed parser inspects, so a
    character that only casefolding would merge (ß/ss) cannot make two
    spellings look equal here and different there."""
    return " ".join(unicodedata.normalize("NFKC", str(text or "")).lower().split())


def _phrase_pattern(phrase: str) -> re.Pattern[str] | None:
    normalized = _norm(phrase)
    if not normalized:
        return None
    return re.compile(r"(?<![\w])" + re.escape(normalized) + r"(?![\w])")


def _contains_phrase(text: str, phrase: str) -> bool:
    """Whole-token containment: "relay" is not found inside "relays", and
    "arrier" is not found inside "Harrier"."""
    pattern = _phrase_pattern(phrase)
    return pattern is not None and pattern.search(_norm(text)) is not None


def subject_is_whole_phrase(subject: str, text: str) -> bool:
    """A subject counts only where it is the whole noun phrase: whole-token
    match, and the token before it is a determiner, punctuation, or the start
    of the clause, never another name token ("relay" inside "Osprey relay")."""
    pattern = _phrase_pattern(subject)
    if pattern is None:
        return False
    normalized = _norm(text)
    for match in pattern.finditer(normalized):
        before = normalized[: match.start()].rstrip()
        if not before or before[-1] in ",;:.!?(\"'":
            return True
        previous = before.split()[-1]
        if previous in _DETERMINERS:
            return True
    return False


def operator_spelling(field: str, text: str) -> str | None:
    """Return the operator's own characters for a grounded field, so the
    stored text never carries the model's casing or normalization."""
    pattern = _phrase_pattern(field)
    if pattern is None:
        return None
    nfkc = unicodedata.normalize("NFKC", str(text or ""))
    collapsed = " ".join(nfkc.split())
    match = re.search(
        r"(?<![\w])" + re.escape(_norm(field)) + r"(?![\w])", collapsed, re.IGNORECASE
    )
    if match is None:
        return None
    return match.group(0)


def predicate_grounded(
    predicate: str,
    statement: str,
    known_predicates: Sequence[str] = (),
) -> bool:
    """Every meaningful predicate word comes from the statement or from a
    predicate already stored for the subject (stem-compared both ways)."""
    needed = predicate_stems(str(predicate))
    if not needed:
        return False
    allowed = set(predicate_stems(str(statement)))
    for known in known_predicates:
        allowed |= predicate_stems(str(known))
    return needed <= allowed


def grounding_clause(proposal: Mapping[str, Any], statement: str) -> str | None:
    """The clause of ``statement`` that grounds the proposal: subject as a
    whole noun phrase and value as a whole-token phrase, both inside one
    clause that is not negated and not a ruled-out alternative."""
    subject = str(proposal.get("subject") or "")
    value = str(proposal.get("value") or "")
    if not _norm(subject) or not _norm(value):
        return None
    for clause in grounding_clauses(statement):
        if subject_is_whole_phrase(subject, clause) and _contains_phrase(clause, value):
            return clause
    return None


def proposal_grounded(
    proposal: Mapping[str, Any],
    statement: str,
    *,
    known_predicates: Sequence[str] = (),
) -> bool:
    """True when the proposal is grounded in a clause of the statement and the
    predicate is grounded.  Used both when a proposal is made and when a
    shown command is confirmed, so the two checks can never disagree."""
    clause = grounding_clause(proposal, statement)
    if clause is None:
        return False
    return predicate_grounded(
        str(proposal.get("predicate") or ""), statement, known_predicates
    )


def parse_proposer_response(
    raw: str | Mapping[str, Any], statement: str
) -> dict[str, str] | None:
    """Parse a model response into grounded fields, or ``None``.

    Fails closed on anything that is not a JSON object with the four string
    keys, on a span that is not a whole-token substring of the statement, on a
    subject or value outside that span, on a subject that is only the tail of
    a longer name, and on a value that only appears in a negated or ruled-out
    clause.  The returned subject and value carry the operator's own spelling.
    Predicate grounding and parser validation follow in :func:`ground_proposal`,
    after the caller has resolved the subject's stored alias.
    """
    if isinstance(raw, str):
        if len(raw) > MAX_PROPOSER_RESPONSE_CHARS:
            return None
        text = raw.strip()
        fence = re.match(r"\A```(?:json)?\s*(.*?)\s*```\Z", text, re.S)
        if fence is not None:
            text = fence.group(1)
        try:
            payload = json.loads(text)
        except (json.JSONDecodeError, RecursionError, TypeError, ValueError):
            return None
    else:
        payload = raw
    if not isinstance(payload, dict):
        return None
    fields: dict[str, str] = {}
    for key, limit in (
        ("subject", 200), ("predicate", 160), ("value", 600), ("source_span", 600)
    ):
        item = payload.get(key)
        if not isinstance(item, str):
            return None
        item = " ".join(item.split())
        if not item or len(item) > limit:
            return None
        fields[key] = item
    if not _contains_phrase(statement, fields["source_span"]):
        return None
    if not _contains_phrase(fields["source_span"], fields["subject"]):
        return None
    if not _contains_phrase(fields["source_span"], fields["value"]):
        return None
    clause = grounding_clause(fields, statement)
    if clause is None:
        return None
    subject = operator_spelling(fields["subject"], clause)
    value = operator_spelling(fields["value"], clause)
    if subject is None or value is None:
        return None
    fields["subject"] = subject
    fields["value"] = value
    return fields


def ground_proposal(
    raw: str | Mapping[str, Any],
    statement: str,
    *,
    known_predicates: Sequence[str] = (),
) -> dict[str, str] | None:
    """Turn a model response into a parser-validated proposal, or ``None``.

    ``parse_proposer_response`` plus predicate grounding against the statement
    and ``known_predicates``, plus the governed parser.
    """
    fields = parse_proposer_response(raw, statement)
    if fields is None:
        return None
    if not predicate_grounded(fields["predicate"], statement, known_predicates):
        return None
    return validate_proposal(fields["subject"], fields["predicate"], fields["value"])
