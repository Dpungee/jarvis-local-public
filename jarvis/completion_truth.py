"""Pure completion-truth checks for final assistant responses.

This module deliberately has no queue, tool, model, or storage authority.  It
only identifies a narrow failure mode: a final response that says work will
happen later or off-turn without naming a durable task receipt.  Callers remain
responsible for proving that a referenced receipt really exists.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable


_SCRIPT_OR_STYLE = re.compile(
    r"<\s*(script|style)\b[^>]*>.*?<\s*/\s*\1\s*>",
    re.IGNORECASE | re.DOTALL,
)
_FENCED_BLOCK = re.compile(r"```.*?```|~~~.*?~~~", re.DOTALL)
_INLINE_CODE = re.compile(r"`[^`\r\n]*`")
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_HTML_TAG = re.compile(r"<[^>\r\n]*>")
_URL = re.compile(r"https?://[^\s<>\]\[(){}]+", re.IGNORECASE)
_DOUBLE_QUOTED = re.compile(r'"[^"\r\n]*"|\u201c[^\u201d\r\n]*\u201d')
_CURLY_SINGLE_QUOTED = re.compile(r"\u2018[^\u2019\r\n]*\u2019")
_BLOCKQUOTE_LINE = re.compile(r"(?m)^\s*>.*$")

_RECEIPT_ID = r"[a-z0-9][a-z0-9._:-]{0,63}"
_ACTIVE_RECEIPT_STATE = r"queued|scheduled|created|running|active|pending"
_RECEIPT_PATTERNS = (
    re.compile(
        rf"\b(?:{_ACTIVE_RECEIPT_STATE})\s+(?:durable\s+)?(?:background\s+)?"
        rf"(?:task|job|schedule|automation)"
        rf"(?:(?:\s+(?:id|receipt))\s*(?:#|:|=)?|\s*(?:#|:|=))"
        rf"\s*(?P<id>{_RECEIPT_ID})\b",
        re.IGNORECASE,
    ),
    re.compile(
        rf"\b(?:task|job|schedule|automation)\s+"
        rf"(?:(?:id)\s*(?:#|:|=)?|#)\s*(?P<id>{_RECEIPT_ID})\b"
        rf"\s+(?:(?:is|was|remains|has\s+been)\s+)?(?:successfully\s+)?"
        rf"(?:{_ACTIVE_RECEIPT_STATE})\b",
        re.IGNORECASE,
    ),
    re.compile(
        rf"\b(?:task|job|schedule|automation)\s*#\s*"
        rf"(?P<id>{_RECEIPT_ID})\b\s+"
        rf"(?:(?:is|was|remains|has\s+been)\s+)?(?:successfully\s+)?"
        rf"(?:{_ACTIVE_RECEIPT_STATE})\b",
        re.IGNORECASE,
    ),
)
_RECEIPT_NEGATED_OR_MISBOUND = re.compile(
    rf"\b(?:not|never|no\s+longer)\s+(?:actually\s+)?(?:{_ACTIVE_RECEIPT_STATE})\b|"
    r"\b(?:if|whether|unless)\b|"
    r"\b(?:cannot|can't|could\s+not|couldn't|did\s+not|didn't)\s+"
    r"(?:verify|confirm|prove)\b|"
    r"\b(?:alleged|allegedly|hypothetical|supposed|supposedly|unverified)\b|"
    r"\b(?:false|incorrect)\s+that\b|"
    r"\bdo\s+not\s+(?:assume|believe|treat)\b|"
    r"\bonly\s+as\s+(?:an?\s+)?(?:example|hypothetical)\b|"
    r"\b(?:unrelated|previous|other|another|different)\b[^.!?]{0,55}"
    r"\b(?:task|job|schedule|request|conversation|run)\b|"
    r"\b(?:not\s+for|belongs?\s+to|for)\s+(?:an?\s+)?"
    r"(?:unrelated|previous|other|another|different)\b|"
    r"\bnot\s+for\s+(?:this|the\s+current|your)\s+"
    r"(?:task|job|schedule|request|conversation|run)\b|"
    r"\b(?:is|was|has\s+been|got|became)\s+(?:already\s+|immediately\s+)?"
    r"(?:canceled|cancelled|complete|completed|deleted|disabled|done|expired|"
    r"failed|finished)\b",
    re.IGNORECASE,
)
_RECEIPT_MENTION = re.compile(
    rf"\b(?:"
    rf"(?:task|job|schedule|automation)\s+"
    rf"(?:(?:id)\s*(?:#|:|=)?|#)|"
    rf"receipt(?:\s+id)?\s*(?:#|:|=)?|"
    rf"(?:queue\s+ticket|work\s+item|run|confirmation)\s*#\s*|"
    rf"tracking\s+id\s*(?:#|:|=)?|"
    rf"queued\s+task\s+ref(?:erence)?\s*(?:#|:|=)?"
    rf")\s*(?P<id>{_RECEIPT_ID})\b",
    re.IGNORECASE,
)
_RECEIPT_PLACEHOLDERS = frozenset(
    {
        "created",
        "later",
        "none",
        "pending",
        "provided",
        "queued",
        "scheduled",
        "tbd",
        "unknown",
        "will",
    }
)

_PROMISE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "report_back",
        re.compile(
            r"\b(?:i|we)\s*(?:'ll|will)\s+(?:get\s+back(?:\s+to\s+you)?|"
            r"report\s+back|follow\s+up|circle\s+back|be\s+back|return\s+with|"
            r"ping\s+you|notify\s+you|"
            r"update\s+you|let\s+you\s+know)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "going_to_report",
        re.compile(
            r"\b(?:i|we)\s*(?:'m|am|'re|are)\s+going\s+to\s+"
            r"(?:check|research|investigate|work\s+on|build|create|finish|"
            r"complete|process|run)\b[^.!?\r\n]{0,180}\b(?:and\s+)?"
            r"(?:get\s+back(?:\s+to\s+you)?|report\s+back|follow\s+up|circle\s+back|"
            r"ping\s+you|update\s+you|let\s+you\s+know)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "active_background_commitment",
        re.compile(
            r"\b(?:i|we)\s*(?:'m|am|'re|are)\s+(?:still\s+)?"
            r"(?:continuing|working|researching|checking|building|processing|running)"
            r"\b[^.!?\r\n]{0,140}\b(?:in\s+the\s+background|after\s+this\s+turn|"
            r"while\s+you(?:'re|\s+are)\s+away)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "deliver_later",
        re.compile(
            r"\b(?:i|we)\s*(?:'ll|will)\s+(?:send|deliver|share|post|email|"
            r"upload|publish|ping)\b[^.!?\r\n]{0,160}\b(?:later|soon|tomorrow|"
            r"when|once|after)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "action_then_report",
        re.compile(
            r"\b(?:i|we)\s*(?:'ll|will)\s+(?:check|research|investigate|"
            r"look\s+into|work\s+on|build|create|finish|complete|process|run)"
            r"\b[^.!?\r\n]{0,180}\b(?:and\s+)?(?:get\s+back(?:\s+to\s+you)?|"
            r"report\s+back|follow\s+up|update\s+you|let\s+you\s+know)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "action_then_deferred_response",
        re.compile(
            r"\b(?:i|we)\s*(?:'ll|will)\s+(?:check|research|investigate|"
            r"look\s+into|work\s+on|build|create|finish|complete|process|run)"
            r"\b[^.!?\r\n]{0,180}\b(?:and|then)\s+"
            r"(?:answer|reply|respond|message|write)\b[^.!?\r\n]{0,100}\b"
            r"(?:later|soon|shortly|tomorrow|"
            r"in\s+(?:another|a\s+later|the\s+next)\s+"
            r"(?:message|reply|response|turn))\b",
            re.IGNORECASE,
        ),
    ),
    (
        "delayed_work",
        re.compile(
            r"\b(?:i|we)\s*(?:'ll|will)\s+(?:keep\s+working|continue\s+"
            r"working|do|handle|finish|complete|build|create|research|check|"
            r"investigate|process|run)\b[^.!?\r\n]{0,120}\b(?:later|soon|"
            r"tomorrow|overnight|in\s+the\s+background|while\s+you(?:'re|\s+are)"
            r"\s+away|after\s+this\s+turn)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "conditional_future",
        re.compile(
            r"\b(?:once|when)\s+[^.!?\r\n]{1,100}\b(?:finish(?:es|ed)?|"
            r"complete(?:s|d)?|done|ready)\b[^.!?\r\n]{0,100}\b"
            r"(?:i|we)\s*(?:'ll|will)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "wait_for_future_result",
        re.compile(
            r"\b(?:give\s+me\s+(?:a\s+few|some|\d+)\s+(?:seconds?|minutes?|"
            r"hours?)|sit\s+tight|check\s+back\s+later|come\s+back\s+later|"
            r"this\s+will\s+take\s+(?:a\s+few|some|\d+)\s+(?:minutes?|hours?))\b",
            re.IGNORECASE,
        ),
    ),
    (
        "future_ready",
        re.compile(
            r"\b(?:(?:i|we)\s*(?:'ll|will)\s+have\s+[^.!?\r\n]{1,100}|"
            r"(?:the\s+)?(?:report|result|file|app|build|research|analysis)\s+will\s+be)"
            r"\s+ready\b[^.!?\r\n]{0,80}\b(?:later|soon|tomorrow|within|in\s+"
            r"(?:a\s+few|some|\d+)\s+(?:minutes?|hours?|days?))\b",
            re.IGNORECASE,
        ),
    ),
    (
        "active_commitment",
        re.compile(
            r"\b(?:leave\s+it\s+with\s+me|expect\s+(?:my|an|the)\s+update\b|"
            r"(?:results?|an?\s+update)\s+(?:will\s+)?(?:follow|be\s+coming)\b)",
            re.IGNORECASE,
        ),
    ),
    (
        "deferred_communication",
        re.compile(
            r"\b(?:"
            r"(?:i|we)\s*(?:'ll|will)\s+(?:come\s+back|message|tell|answer|"
            r"reply|respond|return|post|send|write)\b[^.!?\r\n]{0,160}\b"
            r"(?:later|soon|shortly|tomorrow|when|once|after|"
            r"in\s+(?:another|a\s+later|the\s+next)\s+"
            r"(?:message|reply|response|turn))\b|"
            r"you\s+will\s+hear\s+from\s+(?:me|us)\b[^.!?\r\n]{0,100}\b"
            r"(?:later|soon|shortly|tomorrow|after)\b|"
            r"expect\s+to\s+hear\s+from\s+(?:me|us)\b[^.!?\r\n]{0,100}\b"
            r"(?:later|soon|shortly|tomorrow)\b|"
            r"(?:the\s+)?(?:next\s+|more\s+)?"
            r"(?:results?|updates?|answers?|responses?|reports?)\s+"
            r"(?:is|are|will\s+be)?\s*(?:coming|to\s+follow)\b"
            r"[^.!?\r\n]{0,80}\b(?:later|soon|shortly|tomorrow)\b|"
            r"(?:i(?:'m|\s+am)|we(?:'re|\s+are))\s+"
            r"(?:continuing|working|researching|checking|building|processing|running)"
            r"\b[^.!?\r\n]{0,100}\band\s+(?:(?:i|we)\s+)?will\s+"
            r"(?:message|tell|answer|reply|respond|return|post|send|share|write)\b"
            r"[^.!?\r\n]{0,100}\b(?:later|soon|shortly|tomorrow|when|once|after)\b"
            r")",
            re.IGNORECASE,
        ),
    ),
)

_PROGRESS_ONLY = re.compile(
    r"^\s*(?:(?:sure|okay|ok|absolutely)[,!.]?\s*)?"
    r"(?:(?:i(?:'m|\s+am)|we(?:'re|\s+are))\s+(?:on\s+it|working\s+on\s+"
    r"(?:it|that|this|your\s+request)|researching\s+(?:it|that|this)|"
    r"checking\s+(?:it|that|this|now)|building\s+(?:it|that|this)|"
    r"handling\s+(?:it|that|this))(?:\s+(?:right\s+)?now)?|"
    r"(?:i\s*(?:'ll|will)|will)\s+(?:do|handle|take\s+care\s+of|look\s+into|"
    r"check|research|investigate|build|create)\s+(?:it|that|this|the\s+rest)"
    r"(?:\s+for\s+you)?|(?:i(?:'ve|\s+have)\s+(?:started|begun)|"
    r"i(?:'m|\s+am)\s+starting\s+now)|"
    r"(?:the\s+)?(?:work|research|analysis|build|task)\s+(?:is|remains)\s+"
    r"(?:underway|in\s+progress)|leave\s+it\s+with\s+me)"
    r"[.!\s]*$",
    re.IGNORECASE,
)
_QUOTED_DISCUSSION = re.compile(
    r"\bbad\s+(?:response|wording)\b|"
    r"\b(?:do\s+not|don't|never)\s+"
    r"(?:answer|claim|promise|reply|respond|say|use|write)\b|"
    r"\bavoid\s+(?:that|this|such|the\s+(?:phrase|response|wording))\b|"
    r"\b(?:you|the\s+(?:operator|user))\s+(?:said|wrote)\s*:|"
    r"\b(?:phrase|quote|quoted|response|wording)\b[^.!?]{0,80}\b"
    r"(?:is|was|would\s+be)\s+"
    r"(?:bad|disallowed|incorrect|not\s+allowed|unsafe)\b",
    re.IGNORECASE,
)
_QUOTED_DELIVERY_WRAPPER = re.compile(
    r"^\s*(?:as\s+(?:requested|asked)[,:]?\s*)?(?:"
    r"here(?:'s|\s+is)(?:\s+(?:my|the))?\s+(?:response|answer)|"
    r"my\s+(?:response|answer)\s+is|this\s+is\s+(?:my|the)\s+"
    r"(?:response|answer))\s*[:.!-]*\s*$",
    re.IGNORECASE,
)
_QUOTED_DELIVERY_CLAIM = re.compile(
    r"\b(?:"
    r"here(?:'s|\s+is)\s+(?:(?:my|the)\s+)?"
    r"(?:(?:actual|final|exact)\s+)?(?:answer|response|reply)|"
    r"(?:this|that|it)\s+is\s+(?:still\s+|actually\s+)?"
    r"(?:(?:my|the)\s+)?(?:(?:actual|final|exact)\s+)?"
    r"(?:answer|response|reply)|"
    r"my\s+(?:(?:actual|final|exact)\s+)?(?:answer|response|reply)\s+"
    r"(?:is|remains|follows|appears|below)|"
    r"(?:answer|response|reply)\s+(?:is|remains|follows|appears|below)"
    r")\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class CompletionTruthAssessment:
    """Deterministic classification of one candidate final response."""

    promises_future_work: bool
    has_durable_receipt: bool
    violates_completion_truth: bool
    promise_signals: tuple[str, ...]
    receipt_references: tuple[str, ...]


def _asserted_prose(text: str) -> str:
    """Remove text that the assistant is displaying rather than asserting."""

    value = _SCRIPT_OR_STYLE.sub(" ", text)
    value = _HTML_COMMENT.sub(" ", value)
    value = _FENCED_BLOCK.sub(" ", value)
    value = _BLOCKQUOTE_LINE.sub(" ", value)
    value = _INLINE_CODE.sub(" ", value)
    value = _DOUBLE_QUOTED.sub(" ", value)
    value = _CURLY_SINGLE_QUOTED.sub(" ", value)
    value = _URL.sub(" ", value)
    value = _HTML_TAG.sub(" ", value)
    # Canonical apostrophes make contractions deterministic without changing
    # any identifier or granting authority based on model prose.
    value = value.replace("\u2019", "'").replace("\u2018", "'")
    return re.sub(r"\s+", " ", value).strip()


def _displayed_prose(text: str) -> str:
    """Return visible inert prose while retaining quote/code presentation."""

    value = _SCRIPT_OR_STYLE.sub(" ", text)
    value = _HTML_COMMENT.sub(" ", value)
    value = _URL.sub(" ", value)
    value = _HTML_TAG.sub(" ", value)
    value = value.replace("\u2019", "'").replace("\u2018", "'")
    return re.sub(r"\s+", " ", value).strip()


def _asserted_discussion_is_substantive(text: str) -> bool:
    # Length is not evidence that surrounding prose repudiates a quoted
    # commitment.  Delivery wrappers such as "the requested wording follows"
    # can be arbitrarily long while still presenting the promise as Jarvis's
    # answer.  Only explicit attribution, warning, or repudiation language may
    # keep inert quoted text out of the promise classifier.
    return bool(_QUOTED_DISCUSSION.search(text))


def extract_receipt_references(text: str) -> tuple[str, ...]:
    """Return affirmative active receipt IDs outside quoted/code text.

    Merely mentioning an identifier is not a receipt.  The same sentence must
    assert an active durable state, and must not say the work is negated,
    finished, failed, or belongs to another request.
    """

    if not isinstance(text, str):
        raise TypeError("text must be a string")
    asserted = _asserted_prose(text)
    found: list[str] = []
    seen: set[str] = set()
    for pattern in _RECEIPT_PATTERNS:
        for match in pattern.finditer(asserted):
            left = max(
                asserted.rfind(".", 0, match.start()),
                asserted.rfind("!", 0, match.start()),
                asserted.rfind("?", 0, match.start()),
            )
            right_candidates = [
                index
                for delimiter in ".!?"
                if (index := asserted.find(delimiter, match.end())) >= 0
            ]
            right = min(right_candidates) if right_candidates else len(asserted)
            clause = asserted[left + 1:right]
            if _RECEIPT_NEGATED_OR_MISBOUND.search(clause):
                continue
            reference = match.group("id").strip("`'\".,;:()[]{}")
            folded = reference.casefold()
            if folded in _RECEIPT_PLACEHOLDERS or folded in seen:
                continue
            seen.add(folded)
            found.append(reference)
    return tuple(found)


def _affirmative_receipt_mentions(text: str) -> tuple[str, ...]:
    """Return active receipt-shaped IDs, including unsupported label aliases.

    Only the canonical task/job/schedule/automation forms can prove a durable
    receipt.  Broader labels are still tracked as claims so one real receipt
    cannot launder an invented ``queue ticket`` or ``tracking ID`` in the same
    answer.  Negated, completed, hypothetical, and misbound clauses remain
    ordinary discussion.
    """

    found: list[str] = []
    seen: set[str] = set()
    active_state = re.compile(rf"\b(?:{_ACTIVE_RECEIPT_STATE})\b", re.IGNORECASE)
    for match in _RECEIPT_MENTION.finditer(text):
        left = max(
            text.rfind(".", 0, match.start()),
            text.rfind("!", 0, match.start()),
            text.rfind("?", 0, match.start()),
        )
        right_candidates = [
            index
            for delimiter in ".!?"
            if (index := text.find(delimiter, match.end())) >= 0
        ]
        right = min(right_candidates) if right_candidates else len(text)
        clause = text[left + 1:right]
        canonical_label = re.match(
            r"\s*(?:task|job|schedule|automation|receipt)\b",
            match.group(0),
            re.IGNORECASE,
        ) is not None
        if _RECEIPT_NEGATED_OR_MISBOUND.search(clause) or (
            not canonical_label and active_state.search(clause) is None
        ):
            continue
        reference = match.group("id").strip("`'\".,;:()[]{}")
        folded = reference.casefold()
        if folded in _RECEIPT_PLACEHOLDERS or folded in seen:
            continue
        seen.add(folded)
        found.append(reference)
    return tuple(found)


def assess_completion_truth(
    text: str,
    *,
    known_receipt_ids: Iterable[str] | None = None,
) -> CompletionTruthAssessment:
    """Assess whether ``text`` promises later work without a real receipt.

    Only a reference matching ``known_receipt_ids`` counts as durable. The
    caller-provided set must contain only active future-work receipts created
    for this exact operator request; automatic specialist IDs, historical jobs,
    and unrelated task IDs are not eligible. Omitting the set is deliberately
    equivalent to supplying an empty set: receipt-shaped prose is not storage
    or request-scope evidence.
    """

    if not isinstance(text, str):
        raise TypeError("text must be a string")
    asserted = _asserted_prose(text)
    # Quotation must not become a bypass: when the whole visible answer is only
    # a quote/code/block quote, classify the displayed commitment itself. A
    # substantive asserted discussion (for example, explaining that the quoted
    # phrase is disallowed) remains ordinary safe discussion.
    promise_text = (
        _displayed_prose(text)
        if (
            not _asserted_discussion_is_substantive(asserted)
            or _QUOTED_DELIVERY_WRAPPER.fullmatch(asserted)
            or _QUOTED_DELIVERY_CLAIM.search(asserted)
        )
        else asserted
    )
    signals = [
        name for name, pattern in _PROMISE_PATTERNS
        if pattern.search(promise_text)
    ]
    if _PROGRESS_ONLY.fullmatch(promise_text):
        signals.append("progress_only")
    # Preserve deterministic order if two expressions identify the same class.
    promise_signals = tuple(dict.fromkeys(signals))
    references = extract_receipt_references(text)
    known = {
        str(value).strip().casefold()
        for value in (known_receipt_ids or ())
        if str(value).strip()
    }
    mentioned = {
        reference.casefold()
        for reference in _affirmative_receipt_mentions(asserted)
    }
    # A valid ID cannot launder a second invented or unrelated ID in the same
    # promise. Every receipt mention must be in the caller's exact request-
    # bound eligibility set.
    has_receipt = bool(references) and all(
        reference.casefold() in known for reference in references
    ) and mentioned.issubset(known)
    promises = bool(promise_signals)
    # An affirmative receipt assertion is itself a completion claim.  Do not
    # require the model to also say "I'll report back" before checking that the
    # claimed ID belongs to the caller's exact durable eligibility set.
    invalid_receipt_assertion = bool(mentioned) and not has_receipt
    return CompletionTruthAssessment(
        promises_future_work=promises,
        has_durable_receipt=has_receipt,
        violates_completion_truth=(
            (promises and not has_receipt) or invalid_receipt_assertion
        ),
        promise_signals=promise_signals,
        receipt_references=references,
    )


def has_unreceipted_future_promise(
    text: str,
    *,
    known_receipt_ids: Iterable[str] | None = None,
) -> bool:
    """Convenience predicate for completion-gate integration."""

    return assess_completion_truth(
        text,
        known_receipt_ids=known_receipt_ids,
    ).violates_completion_truth


def completion_truth_correction_prompt(*, durable_queue_available: bool) -> str:
    """Return a fixed corrective instruction without echoing untrusted prose."""

    options = (
        "If the operator explicitly requested future execution and a durable queue is "
        "available, create the real queued task and report its exact task ID. "
        if durable_queue_available
        else "Do not claim that work was queued because no durable queue is available. "
    )
    return (
        "The prior draft promised future or off-turn work without a verified durable "
        "receipt. Correct it now: complete the requested work in this turn with the "
        "currently authorized tools and report only verified results. If completion is "
        "genuinely blocked, state the exact blocker and ask for at most one necessary "
        "input. "
        + options
        + "Never say you will keep working, report back, notify the operator, or finish "
        "later unless that real receipt already exists. Never invent a task ID."
    )
