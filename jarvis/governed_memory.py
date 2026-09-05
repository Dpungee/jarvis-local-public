from __future__ import annotations

import base64
import binascii
import json
import re
import unicodedata
import urllib.parse
from typing import Any

from .redaction import (
    _is_default_ignorable,
    _secret_detection_view,
    contains_explicit_sensitive_key_phrase,
    contains_private_identifier,
    contains_sensitive_key_phrase,
    contains_secret,
    is_sensitive_key,
)


PROJECT_FACT_COMMAND_PREFIX = re.compile(
    r"\A\s*(?:please\s+)?remember\s+this\s+project\s+fact\s*:\s*",
    re.IGNORECASE,
)
_PROJECT_FACT_FIELDS = frozenset({"subject", "predicate", "value"})
_PROJECT_FACT_LIMITS = {"subject": 200, "predicate": 160, "value": 600}
_MAX_PROJECT_FACT_COMMAND_CHARS = 8_192
_MAX_URL_DECODE_PASSES = 8
_MAX_BASE64_DECODE_PASSES = 3
_BASE64_TEXT = re.compile(r"\A[A-Za-z0-9+/_-]{8,}={0,2}\Z")
_EMBEDDED_BASE64_TEXT = re.compile(
    r"(?<![A-Za-z0-9+/_-])([A-Za-z0-9+/_-]{8,}={0,2})"
    r"(?![A-Za-z0-9+/_=-])"
)
_RESERVED_PREDICATE_NAMESPACE = re.compile(
    r"\A(?:identity|permissions?|preferences?|safety)(?:\b|[_:./-])",
    re.IGNORECASE,
)
_INSTRUCTION_LIKE = re.compile(
    r"(?is)\b(?:ignore|override|disregard|bypass|disable).{0,60}"
    r"\b(?:approval|instruction|policy|safety|system)\b|"
    r"\byou\s+are\s+now\b|"
    r"\b(?:run|execute|invoke|call).{0,40}"
    r"\b(?:command|powershell|shell|tool)\b|"
    r"(?:^|\s)(?:assistant|developer|system|tool|user)\s*:|"
    r"<\s*/?\s*(?:assistant|developer|system|temporal_claims|tool|user)\b|"
    r"<<\s*sys\s*>>|"
    r"<\|\s*(?:assistant|developer|system|tool|user)\s*\|>"
)
_NONFACTUAL_AUTHORITY = re.compile(
    r"(?is)\b(?:approval|authori[sz](?:e|ed|ation)?|credentials?|permission|"
    r"policy|system\s+prompt|developer\s+message|instruction|tool\s+(?:call|use)|"
    r"stored\s+secrets?)\b|"
    r"\b(?:always|never|must|shall|should|ought\s+to|required\s+to|"
    r"permitted\s+to|allowed\s+to)\b|"
    r"\A\s*(?:please\s+)?(?:treat|follow|comply|reveal|expose|grant|write|"
    r"delete|modify|execute|run|invoke|call|ignore|override|bypass|disable|"
    r"send|upload)\b"
)
_CONTROL_DISCOURSE_ACTOR = re.compile(
    r"(?is)\b(?:agent|assistant|model|system|runtime|memory|record|"
    r"recall(?:ed)?|retriev(?:al|ed)|response|prompt|command|directive|"
    r"instruction|safeguard|policy|rule|tool)\b"
)
_CONTROL_DISCOURSE_ACTION = re.compile(
    r"(?is)\b(?:obey|comply|perform|act|respond|disclos\w*|expos\w*|"
    r"eras\w*|delet\w*|execut\w*|invok\w*|follow|override|ignore|bypass|"
    r"disable|outrank\w*|precedence)\b|"
    r"\b(?:is|are|be)\s+(?:expected|required|supposed)\s+to\b|"
    r"\bhighest[-\s]+priority\b"
)
_CONTROL_DISCOURSE_IMPERATIVE = re.compile(
    r"(?is)\A\s*(?:please\s+)?(?:obey|comply|perform|act|respond|disclose|"
    r"expose|erase|delete|execute|invoke|follow|override|ignore|bypass|"
    r"disable|use)\b"
)


class GovernedMemoryCommandError(ValueError):
    """An explicit memory command was recognized but rejected safely."""


def _looks_like_reserved_project_fact_intent(value: str) -> bool:
    """Recognize direct noncanonical wrappers without owning ordinary discussion."""
    canonical_text = _secret_detection_view(str(value))[:320]
    remember = re.search(r"\bremember\b", canonical_text, re.IGNORECASE)
    if remember is None:
        return False
    leading = canonical_text[:remember.start()]
    if (
        any(marker in leading for marker in ('"', "'", "`", ":"))
        or re.search(
            r"\b(?:say|quote|quoted|documentation|documented|example|phrase)\b",
            leading,
            re.IGNORECASE,
        )
    ):
        return False
    tail = canonical_text[remember.end():remember.end() + 160]
    direct_imperative = not leading.strip() or re.fullmatch(
        r"\s*(?:please\s*,?|kindly|do)\s*",
        leading,
        re.IGNORECASE,
    ) is not None

    phrase = re.search(r"\b(?:this\s+)?project\s+fact\b", tail, re.IGNORECASE)
    if phrase is not None:
        suffix = tail[phrase.end():].lstrip()
        # A wrapper with a payload marker is still owned and rejected instead
        # of falling through to a broader model-mediated memory lane.  A plain
        # question or recollection about "the project fact" has no such marker
        # and remains ordinary conversation.
        return direct_imperative or suffix.startswith((":", "：", "{", "[", "```"))

    words = list(re.finditer(r"[^\W\d_]+", tail, flags=re.UNICODE))
    for index, word_match in enumerate(words):
        if word_match.group(0).casefold() != "fact" or index == 0:
            continue
        candidate = unicodedata.normalize(
            "NFKC", words[index - 1].group(0)
        ).casefold()
        suffix = tail[word_match.end():].lstrip()
        payload_marked = suffix.startswith((":", "：", "{", "[", "```"))
        if candidate == "project":
            return direct_imperative or payload_marked
        if not 5 <= len(candidate) <= 9:
            continue
        positional_matches = sum(
            left == right for left, right in zip(candidate, "project", strict=False)
        )
        if positional_matches >= 5 and (
            any(ord(character) > 127 for character in candidate)
            or len(candidate) == len("project")
        ):
            return direct_imperative or payload_marked
    return False


def _pairs_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    parsed: dict[str, Any] = {}
    for key, value in pairs:
        if key in parsed:
            raise GovernedMemoryCommandError(
                "Project fact JSON contains a duplicate field"
            )
        parsed[key] = value
    return parsed


def _decoded_forms(value: str) -> tuple[str, ...]:
    forms: list[str] = []
    current = str(value)
    for depth in range(_MAX_URL_DECODE_PASSES + 1):
        forms.append(current)
        decoded = urllib.parse.unquote_plus(current)
        if decoded == current:
            return tuple(forms)
        if "\ufffd" in decoded and "\ufffd" not in current:
            raise GovernedMemoryCommandError(
                "Project fact contains malformed encoded text"
            )
        if depth == _MAX_URL_DECODE_PASSES:
            raise GovernedMemoryCommandError(
                "Project fact contains excessively nested encoded text"
            )
        current = decoded
    raise GovernedMemoryCommandError("Project fact decoding failed safely")


def _base64_text(value: str) -> str | None:
    """Decode one unmistakable, printable base64/base64url text value."""
    encoded = str(value)
    if len(encoded) > _MAX_PROJECT_FACT_COMMAND_CHARS or not _BASE64_TEXT.fullmatch(
        encoded
    ):
        return None
    canonical = encoded.replace("-", "+").replace("_", "/")
    if len(canonical) % 4 == 1:
        return None
    canonical += "=" * ((-len(canonical)) % 4)
    try:
        decoded = base64.b64decode(canonical, validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None
    if not decoded or any(
        not character.isprintable() and character not in "\r\n\t"
        for character in decoded
    ):
        return None
    return decoded


def _inspection_forms(value: str) -> tuple[str, ...]:
    """Return bounded URL/base64-decoded views used only for safety checks."""
    forms: list[str] = []
    seen: set[str] = set()
    frontier = list(_decoded_forms(value))
    for _depth in range(_MAX_BASE64_DECODE_PASSES + 1):
        next_frontier: list[str] = []
        for candidate in frontier:
            for decoded_url in _decoded_forms(candidate):
                if decoded_url not in seen:
                    seen.add(decoded_url)
                    forms.append(decoded_url)
                encoded_candidates = {
                    decoded_url,
                    *(
                        match.group(1)
                        for match in _EMBEDDED_BASE64_TEXT.finditer(decoded_url)
                    ),
                }
                for encoded_candidate in encoded_candidates:
                    decoded_base64 = _base64_text(encoded_candidate)
                    if decoded_base64 is not None and decoded_base64 not in seen:
                        next_frontier.append(decoded_base64)
        if not next_frontier:
            break
        if _depth == _MAX_BASE64_DECODE_PASSES:
            raise GovernedMemoryCommandError(
                "Project fact contains excessively nested encoded text"
            )
        frontier = next_frontier
    return tuple(forms)


def _contains_unsupported_unicode(value: str) -> bool:
    return any(
        unicodedata.category(character) in {"Cc", "Cs", "Co"}
        or _is_default_ignorable(character)
        for character in value
    )


def _normalize_field(name: str, value: Any) -> str:
    """Normalize and screen one governed field, or refuse it.

    The control-character refusal applies to **what the operator actually
    typed** - the raw string and its NFKC form - and to nothing else.  The
    decoded inspection forms are a search for hidden secrets, not a second
    opinion on the operator's characters: ``_inspection_forms`` base64-decodes
    any base64-shaped word, so an ordinary ASCII subject like ``Meadow01
    crock`` decodes to arbitrary bytes and was refused for containing a
    carriage return that the operator never wrote.  (``_base64_text`` counts
    ``\r``, ``\n`` and ``\t`` as printable, so the two checks disagreed by
    construction.)  Decoded forms are therefore screened for secrets and
    private identifiers only, which is what they exist for.
    """
    if not isinstance(value, str):
        raise GovernedMemoryCommandError(
            f"Project fact {name} must be a string"
        )
    if _contains_unsupported_unicode(value):
        raise GovernedMemoryCommandError(
            f"Project fact {name} contains unsupported control characters"
        )
    normalized = " ".join(unicodedata.normalize("NFKC", value).split())
    if _contains_unsupported_unicode(normalized):
        raise GovernedMemoryCommandError(
            f"Project fact {name} contains unsupported control characters"
        )
    if not normalized or len(normalized) > _PROJECT_FACT_LIMITS[name]:
        raise GovernedMemoryCommandError(
            f"Project fact {name} must contain 1-{_PROJECT_FACT_LIMITS[name]} characters"
        )
    for inspected in _inspection_forms(normalized):
        if contains_secret(inspected) or contains_private_identifier(inspected):
            raise GovernedMemoryCommandError(
                "Project fact contains sensitive or private data"
            )
    return normalized


def parse_explicit_project_fact(prompt: str) -> dict[str, str] | None:
    """Parse one exact, standalone operator-authored project-fact command.

    The return value is the only triple that may cross the governed write
    boundary.  A recognized prefix with malformed or unsafe JSON raises rather
    than falling through to a model or a free-form memory tool.
    """
    text = str(prompt)
    match = PROJECT_FACT_COMMAND_PREFIX.match(text)
    if match is None:
        # An NFKC/confusable/invisible spelling of the reserved prefix must not
        # fall through to ordinary model routing, where a broader memory-intent
        # classifier could accidentally grant a different write lane.
        canonical_prefix_view = _secret_detection_view(text)
        if PROJECT_FACT_COMMAND_PREFIX.match(canonical_prefix_view) is not None:
            raise GovernedMemoryCommandError(
                "Project fact command prefix contains non-canonical characters"
            )
        if _looks_like_reserved_project_fact_intent(text):
            raise GovernedMemoryCommandError(
                "This looks like a project-fact command but is not in the exact required form"
            )
        return None
    if len(text) > _MAX_PROJECT_FACT_COMMAND_CHARS:
        raise GovernedMemoryCommandError("Project fact command is too large")
    encoded = text[match.end():].strip()
    if not encoded:
        raise GovernedMemoryCommandError(
            "Project fact command requires a JSON object"
        )
    try:
        payload = json.loads(encoded, object_pairs_hook=_pairs_without_duplicates)
    except GovernedMemoryCommandError:
        raise
    except (json.JSONDecodeError, RecursionError, TypeError, ValueError):
        raise GovernedMemoryCommandError(
            "Project fact command requires one valid JSON object and no trailing text"
        ) from None
    if not isinstance(payload, dict) or set(payload) != _PROJECT_FACT_FIELDS:
        raise GovernedMemoryCommandError(
            "Project fact JSON must contain exactly subject, predicate, and value"
        )
    normalized = {
        name: _normalize_field(name, payload[name])
        for name in ("subject", "predicate", "value")
    }
    decoded_fields = {
        name: _inspection_forms(value)
        for name, value in normalized.items()
    }
    predicate_views = {
        view
        for form in decoded_fields["predicate"]
        for view in (form, _secret_detection_view(form))
    }
    subject_views = {
        view
        for form in decoded_fields["subject"]
        for view in (form, _secret_detection_view(form))
    }
    combined_key_views = {
        f"{subject} {predicate}"
        for subject in subject_views
        for predicate in predicate_views
    }
    value_views = {
        view
        for form in decoded_fields["value"]
        for view in (form, _secret_detection_view(form))
    }
    individual_key_views = subject_views | predicate_views
    key_views = individual_key_views | combined_key_views
    split_secret = any(
        contains_secret(f"{key}: {value}")
        for key in key_views
        for value in value_views
    )
    if (
        any(
            is_sensitive_key(view) or contains_sensitive_key_phrase(view)
            for view in individual_key_views
        )
        or any(
            is_sensitive_key(view) or contains_explicit_sensitive_key_phrase(view)
            for view in combined_key_views
        )
        or any(_RESERVED_PREDICATE_NAMESPACE.match(view) for view in predicate_views)
        or split_secret
    ):
        raise GovernedMemoryCommandError(
            "Project fact subject or predicate is reserved or sensitive"
        )
    semantic_views = {
        view
        for forms in decoded_fields.values()
        for form in forms
        for view in (form, _secret_detection_view(form))
    }
    fully_decoded = "\n".join(
        _secret_detection_view(decoded_fields[name][-1])
        for name in ("subject", "predicate", "value")
    )
    if any(
        _INSTRUCTION_LIKE.search(view)
        or _NONFACTUAL_AUTHORITY.search(view)
        or _CONTROL_DISCOURSE_IMPERATIVE.search(view)
        or (
            _CONTROL_DISCOURSE_ACTOR.search(view)
            and _CONTROL_DISCOURSE_ACTION.search(view)
        )
        for view in semantic_views
    ) or (
        _INSTRUCTION_LIKE.search(fully_decoded)
        or _NONFACTUAL_AUTHORITY.search(fully_decoded)
        or _CONTROL_DISCOURSE_IMPERATIVE.search(fully_decoded)
        or (
            _CONTROL_DISCOURSE_ACTOR.search(fully_decoded)
            and _CONTROL_DISCOURSE_ACTION.search(fully_decoded)
        )
    ):
        raise GovernedMemoryCommandError(
            "Instruction-like project facts are not stored"
        )
    return normalized


PROJECT_FACT_RETRACTION_PREFIX = re.compile(
    r"\A\s*(?:please\s+)?(?:forget|retract)\s+this\s+project\s+fact\s*:\s*",
    re.IGNORECASE,
)
PROJECT_FACT_ERASURE_PREFIX = re.compile(
    r"\A\s*(?:please\s+)?(?:erase|delete)\s+this\s+project\s+fact\s*:\s*",
    re.IGNORECASE,
)
_PROJECT_FACT_RETRACTION_FIELDS = frozenset({"subject", "predicate"})


def _looks_like_reserved_project_fact_retraction_intent(value: str) -> bool:
    """Recognize direct noncanonical retraction or erasure wrappers, not
    ordinary talk."""
    canonical_text = _secret_detection_view(str(value))[:320]
    match = re.search(
        r"\b(?:forget|retract|erase|delete)\b[^.?!\n]{0,60}?\b(?:this\s+)?project\s+fact\b",
        canonical_text,
        re.IGNORECASE,
    )
    if match is None:
        return False
    leading = canonical_text[:match.start()]
    if (
        any(marker in leading for marker in ('"', "'", "`", ":"))
        or re.search(
            r"\b(?:say|quote|quoted|documentation|documented|example|phrase)\b",
            leading,
            re.IGNORECASE,
        )
    ):
        return False
    suffix = canonical_text[match.end():].lstrip()
    direct_imperative = not leading.strip() or re.fullmatch(
        r"\s*(?:please\s*,?|kindly|do)\s*",
        leading,
        re.IGNORECASE,
    ) is not None
    return direct_imperative or suffix.startswith((":", "：", "{", "[", "```"))


def _parse_project_fact_key_command(
    prompt: str, prefix: re.Pattern[str], label: str, *, detect_intent: bool
) -> dict[str, str] | None:
    """Shared exact parser for the retraction and erasure commands.

    Like the store command, a recognized prefix with malformed JSON raises
    instead of falling through to a model.  ``detect_intent`` lets one of the
    two parsers own the shared near-command detector so a wrapper such as
    "erase the project fact: {...}" fails closed exactly once.
    """
    text = str(prompt)
    match = prefix.match(text)
    if match is None:
        canonical_prefix_view = _secret_detection_view(text)
        if prefix.match(canonical_prefix_view) is not None:
            raise GovernedMemoryCommandError(
                f"Project fact {label} prefix contains non-canonical characters"
            )
        if detect_intent and _looks_like_reserved_project_fact_retraction_intent(text):
            raise GovernedMemoryCommandError(
                "This looks like a project-fact retraction or erasure but is not in "
                "the exact required form"
            )
        return None
    if len(text) > _MAX_PROJECT_FACT_COMMAND_CHARS:
        raise GovernedMemoryCommandError(f"Project fact {label} is too large")
    encoded = text[match.end():].strip()
    if not encoded:
        raise GovernedMemoryCommandError(
            f"Project fact {label} requires a JSON object"
        )
    try:
        payload = json.loads(encoded, object_pairs_hook=_pairs_without_duplicates)
    except GovernedMemoryCommandError:
        raise
    except (json.JSONDecodeError, RecursionError, TypeError, ValueError):
        raise GovernedMemoryCommandError(
            f"Project fact {label} requires one valid JSON object and no trailing text"
        ) from None
    if not isinstance(payload, dict) or set(payload) != _PROJECT_FACT_RETRACTION_FIELDS:
        raise GovernedMemoryCommandError(
            f"Project fact {label} JSON must contain exactly subject and predicate"
        )
    return {
        name: _normalize_field(name, payload[name])
        for name in ("subject", "predicate")
    }


def parse_explicit_project_fact_retraction(prompt: str) -> dict[str, str] | None:
    """Parse one exact, standalone operator-authored project-fact retraction.

    ``Forget this project fact: {"subject":"...","predicate":"..."}`` names the
    claim identity to retire (the versions stay as history).
    """
    return _parse_project_fact_key_command(
        prompt, PROJECT_FACT_RETRACTION_PREFIX, "retraction", detect_intent=True
    )


def parse_explicit_project_fact_erasure(prompt: str) -> dict[str, str] | None:
    """Parse one exact, standalone operator-authored project-fact erasure.

    ``Erase this project fact: {"subject":"...","predicate":"..."}`` names the
    claim identity whose every version is deleted and tombstoned on the spine.
    The near-command detector is owned by the retraction parser, which the
    agent consults first, so an erasure wrapper still fails closed there.
    """
    return _parse_project_fact_key_command(
        prompt, PROJECT_FACT_ERASURE_PREFIX, "erasure", detect_intent=False
    )


MEMORY_ERASURE_PREFIX = re.compile(
    r"\A\s*(?:please\s+)?(?:erase|delete)\s+memory\s*#\s*"
    r"([1-9][0-9]{0,17})\s*[.!]?\s*\Z",
    re.IGNORECASE,
)
MEMORY_ERASURE_INTENT = re.compile(
    r"\b(?:erase|delete|forget)\s+memory\b", re.IGNORECASE
)
MEMORY_ERASURE_SHAPE = "Erase memory #<id>"


def _looks_like_memory_erasure_intent(value: str) -> bool:
    """Recognize a direct noncanonical memory-erasure wrapper, not ordinary talk.

    The same discipline as the project-fact detectors: a quoted or
    documentation mention is left alone, while a direct imperative or a
    payload marker owns the turn so it fails closed instead of reaching a
    model.
    """
    canonical_text = _secret_detection_view(str(value))[:320]
    match = MEMORY_ERASURE_INTENT.search(canonical_text)
    if match is None:
        return False
    leading = canonical_text[:match.start()]
    if (
        any(marker in leading for marker in ('"', "'", "`", ":"))
        or re.search(
            r"\b(?:say|quote|quoted|documentation|documented|example|phrase)\b",
            leading,
            re.IGNORECASE,
        )
    ):
        return False
    suffix = canonical_text[match.end():].lstrip()
    direct_imperative = not leading.strip() or re.fullmatch(
        r"\s*(?:please\s*,?|kindly|do)\s*",
        leading,
        re.IGNORECASE,
    ) is not None
    return direct_imperative or suffix.startswith(("#", ":", "：", "{", "["))


def looks_like_memory_erasure(prompt: str) -> bool:
    """True when a turn is shaped like the memory-erasure verb.

    Matched on the canonical view, exactly as the parser's own near-command
    detector is.  A confusable spelling (``\uff25rase memory #1``) reaches the
    parser as an error, and the caller then has to decide WHICH verb owns the
    turn: without canonicalizing here it looks like no verb at all and the
    refusal quotes another verb's shape, telling the operator to fix a command
    they did not send.
    """
    canonical = _secret_detection_view(str(prompt))[:320]
    return MEMORY_ERASURE_INTENT.search(canonical) is not None


def parse_explicit_memory_erasure(prompt: str) -> dict[str, int] | None:
    """Parse one exact, standalone operator-authored ordinary-memory erasure.

    ``Erase memory #<id>`` (also ``Delete memory #<id>``, a leading
    ``please``, a trailing ``.`` or ``!``) names the ``memories`` row to
    delete.  ``memories.id`` is explicit and never reused since schema 47, so
    the id is the operator-facing identity.  Nothing else may share the turn:
    a recognized near-command raises rather than falling through to a model or
    to the broader model-visible memory tool.
    """
    text = str(prompt)
    match = MEMORY_ERASURE_PREFIX.match(text)
    if match is None:
        # An NFKC/confusable/invisible spelling of the command must not fall
        # through to ordinary model routing.
        canonical_view = _secret_detection_view(text)
        if MEMORY_ERASURE_PREFIX.match(canonical_view) is not None:
            raise GovernedMemoryCommandError(
                "Memory erasure command contains non-canonical characters"
            )
        if _looks_like_memory_erasure_intent(text):
            raise GovernedMemoryCommandError(
                "This looks like a memory erasure but is not in the exact "
                f"required form: {MEMORY_ERASURE_SHAPE}"
            )
        return None
    if len(text) > _MAX_PROJECT_FACT_COMMAND_CHARS:
        raise GovernedMemoryCommandError("Memory erasure command is too large")
    return {"memory_id": int(match.group(1))}


# The learning ladder's two operator verbs (VTMF M4 design 6.1).  Both are
# parsed from the raw operator turn before any model call, exactly as the four
# M1 verbs above are, so a model reply carrying the same words does nothing.
#
# The trailing value on an approval is a CONFIRMATION CODE, not a capability:
# sixteen url-safe characters generated at staging, stored in cleartext on the
# promotion row, and shown only by ``ladder list``, ``ladder show`` and
# ``/ladder``.  Its job is to prove the operator looked at the staged document
# before making it live.  It is single use, consumed by a successful approval,
# and a rollback needs none.
SKILL_PROMOTION_CODE_LENGTH = 16  # len(secrets.token_urlsafe(12))
SKILL_PROMOTION_CODE_ALPHABET = "A-Za-z0-9_-"  # secrets.token_urlsafe's output
SKILL_PROMOTION_APPROVAL_PREFIX = re.compile(
    r"\A\s*(?:please\s+)?(?:approve|promote)\s+skill\s+promotion\s*#\s*"
    r"([1-9][0-9]{0,17})\s+"
    rf"([{SKILL_PROMOTION_CODE_ALPHABET}]{{{SKILL_PROMOTION_CODE_LENGTH}}})"
    r"\s*[.!]?\s*\Z",
    re.IGNORECASE,
)
SKILL_PROMOTION_ROLLBACK_PREFIX = re.compile(
    r"\A\s*(?:please\s+)?(?:roll\s*back|revert)\s+skill\s+promotion\s*#\s*"
    r"([1-9][0-9]{0,17})\s*[.!]?\s*\Z",
    re.IGNORECASE,
)
# The near-miss detectors are deliberately looser than the exact prefixes
# above: they run over the canonical view, where stripping a zero-width
# character JOINS two words ("skill<ZWSP>promotion" -> "skillpromotion"), so
# `skill\s*promotion` is what actually catches a confusable paste.  Catching it
# matters more here than for the M1 verbs: an unrecognized approval would carry
# the operator's confirmation code into a model prompt (design 7.11).
SKILL_PROMOTION_APPROVAL_INTENT = re.compile(
    r"\b(?:approve|approving|promote|promoting)\s+"
    r"(?:my\s+|that\s+|the\s+|this\s+)?"
    r"(?:skill\s*promotions?\b|promotions?\s*#)",
    re.IGNORECASE,
)
SKILL_PROMOTION_ROLLBACK_INTENT = re.compile(
    r"\b(?:roll(?:ing)?\s*back|revert(?:ing)?|undo(?:ing)?)\s+"
    r"(?:my\s+|that\s+|the\s+|this\s+)?"
    r"(?:skill\s*promotions?\b|promotions?\s*#)",
    re.IGNORECASE,
)
SKILL_PROMOTION_APPROVAL_SHAPE = "Approve skill promotion #<id> <confirmation code>"
SKILL_PROMOTION_ROLLBACK_SHAPE = "Roll back skill promotion #<id>"


def _looks_like_skill_promotion_intent(value: str, intent: re.Pattern[str]) -> bool:
    """Recognize a direct noncanonical ladder wrapper, not ordinary talk.

    The same discipline as ``_looks_like_memory_erasure_intent``: a quoted or
    documentation mention is left alone so the operator can ask how the verb
    works, while a direct imperative or a payload marker owns the turn and
    fails closed instead of reaching a model.
    """
    canonical_text = _secret_detection_view(str(value))[:320]
    match = intent.search(canonical_text)
    if match is None:
        return False
    leading = canonical_text[:match.start()]
    if (
        any(marker in leading for marker in ('"', "'", "`", ":"))
        or re.search(
            r"\b(?:say|quote|quoted|documentation|documented|example|phrase)\b",
            leading,
            re.IGNORECASE,
        )
    ):
        return False
    suffix = canonical_text[match.end():].lstrip()
    direct_imperative = not leading.strip() or re.fullmatch(
        r"\s*(?:please\s*,?|kindly|do)\s*",
        leading,
        re.IGNORECASE,
    ) is not None
    return direct_imperative or suffix.startswith(("#", ":", "：", "{", "["))


def looks_like_skill_promotion_command(prompt: str) -> bool:
    """True when a turn is shaped like either ladder verb.

    Matched on the canonical view, exactly as ``looks_like_memory_erasure``
    is, so a confusable spelling reaches the caller as THIS verb rather than
    looking like no verb at all and being refused with another verb's shape
    (design 6.1, L-6).
    """
    canonical = _secret_detection_view(str(prompt))[:320]
    return (
        SKILL_PROMOTION_APPROVAL_INTENT.search(canonical) is not None
        or SKILL_PROMOTION_ROLLBACK_INTENT.search(canonical) is not None
        # R-3: a turn of the right SHAPE counts even when its middle words are
        # a confusable or hyphenated spelling no intent regex recognizes.
        or skill_promotion_shape_guard(prompt) is not None
    )


# Red team R-3 / design ruling 18: the LOOSE shape, checked before routing.
#
# The two intent regexes above need the literal `skill promotion` (or
# `promotion #`) to survive `_secret_detection_view`.  A homoglyph NFKC does
# not fold -- Cyrillic U+0455 in "skill" -- or a hyphen in "skill-promotion"
# defeats the exact parser AND the near-miss detector, and the turn falls
# through to ordinary model routing carrying the operator's confirmation code
# into the prompt and the transcript.  Cyrillic o/p/a and Greek omicron are
# already caught, because the surviving literal anchors the intent regex; it is
# a confusable inside "skill" itself, and punctuation between the two words,
# that are not.
#
# So this matches on the SHAPE alone and ignores the middle words entirely: an
# approve or roll-back verb near the start, then `#<digits>`, then -- for the
# approval direction -- a 16-character url-safe run.  Anything of that shape is
# refused as the verb rather than routed, whatever the words between.
SKILL_PROMOTION_APPROVAL_SHAPE_GUARD = re.compile(
    r"\A\s*(?:please\s+)?"
    r"(?:approve|approving|promote|promoting|apply|applying|confirm|"
    r"confirming|accept|accepting|authorise|authorize|ok|okay)\b"
    r".{0,40}?#\s*[0-9]{1,18}\s+"
    rf"[{SKILL_PROMOTION_CODE_ALPHABET}]{{{SKILL_PROMOTION_CODE_LENGTH}}}\b",
    re.IGNORECASE | re.DOTALL,
)
SKILL_PROMOTION_ROLLBACK_SHAPE_GUARD = re.compile(
    r"\A\s*(?:please\s+)?"
    r"(?:roll(?:ing)?\s*back|revert(?:ing)?|undo(?:ing)?|unapprove|"
    r"unapproving|withdraw(?:ing)?)\b"
    r".{0,40}?#\s*[0-9]{1,18}",
    re.IGNORECASE | re.DOTALL,
)
#: An id-first approval: `Skill promotion #12 approve <code>`.  A plausible
#: typo, and with no leading verb the shape guards above do not see it.
SKILL_PROMOTION_ID_FIRST_GUARD = re.compile(
    r"promot\w*\s*#\s*[0-9]{1,18}\s+"
    r"(?:approve|approving|promote|promoting|apply|applying|confirm|"
    r"confirming|accept|accepting|authorise|authorize|ok|okay)\b\s+"
    rf"[{SKILL_PROMOTION_CODE_ALPHABET}]{{{SKILL_PROMOTION_CODE_LENGTH}}}\b",
    re.IGNORECASE,
)
#: A confirmation code NEAR a promotion id, however the turn is worded.
#:
#: Used for MASKING only, and deliberately looser than the refusal guards: it
#: allows a few words between the id and the code.  Masking one ordinary
#: sixteen-character word in a transcript costs almost nothing; refusing a
#: legitimate turn costs the operator their sentence, so the refusal path keeps
#: the tighter verb-led shapes above and only this one is permissive.
SKILL_PROMOTION_CODE_BESIDE_ID = re.compile(
    r"promot\w*\s*#\s*[0-9]{1,18}(?:\s+[\w-]+){0,3}\s+"
    rf"([{SKILL_PROMOTION_CODE_ALPHABET}]{{{SKILL_PROMOTION_CODE_LENGTH}}})\b",
    re.IGNORECASE,
)


def skill_promotion_shape_guard(prompt: str) -> str | None:
    """The verb a turn is SHAPED like, ignoring the words in the middle.

    Returns ``"approve"``, ``"rollback"`` or None.  Runs over the canonical
    view, so a confusable or zero-width spelling is judged on what it reduces
    to.  This is deliberately looser than the intent detectors: its job is to
    make sure no turn carrying a promotion id and a confirmation code can ever
    reach a model, even when the wording is one nobody anticipated.
    """
    canonical = _secret_detection_view(str(prompt))[:320]
    if SKILL_PROMOTION_APPROVAL_SHAPE_GUARD.search(canonical) is not None:
        return "approve"
    if SKILL_PROMOTION_ROLLBACK_SHAPE_GUARD.search(canonical) is not None:
        return "rollback"
    if SKILL_PROMOTION_ID_FIRST_GUARD.search(canonical) is not None:
        return "approve"
    return None


def mask_skill_promotion_code(text: str) -> str:
    """Mask a confirmation code that follows ``promotion #<digits>``.

    Ruling 18's second half.  ``redact_secrets`` leaves a bare sixteen-character
    url-safe value untouched -- correctly, it looks like an ordinary word -- so
    a turn the parser did not claim would carry the code into ``messages`` and
    from there into the next prompt.  This masks it on the way past, keyed on
    the promotion id that precedes it so ordinary prose is never touched.

    It lives here and NOT in ``redaction.py``: that module is inside the memory
    graph holdout's runtime pin, and widening a pinned screen for one verb's
    grammar would reseal a sealed evaluation for a reason unrelated to it.
    """
    return SKILL_PROMOTION_CODE_BESIDE_ID.sub(
        lambda match: match.group(0)[: match.start(1) - match.start(0)]
        + "<confirmation code>",
        str(text),
    )


def skill_promotion_verb_of(prompt: str) -> str:
    """Which ladder verb a near-miss turn was reaching for.

    The M3 C-4 lesson: a malformed wrapper must be refused AS the verb the
    operator meant, quoting that verb's shape.  Telling someone who mistyped a
    rollback to fix an approval sends them to correct a command they never
    sent.  Both parsers return None for a near-miss of the OTHER verb, so the
    caller cannot tell them apart from the parse alone -- it asks here.
    """
    canonical = _secret_detection_view(str(prompt))[:320]
    if SKILL_PROMOTION_ROLLBACK_INTENT.search(canonical) is not None:
        return "rollback"
    return "approve"


def parse_explicit_skill_promotion_approval(prompt: str) -> dict[str, Any] | None:
    """Parse one exact, standalone operator-authored skill-promotion approval.

    ``Approve skill promotion #<id> <code>`` (also ``Promote ...``, a leading
    ``please``, a trailing ``.`` or ``!``) names the ``ladder_promotions`` row
    to make live and carries the confirmation code shown by ``ladder list``.
    The verb is case-insensitive; the id and the code are exact.  Nothing else
    may share the turn: a recognized near-command raises rather than falling
    through to a model, so a mistyped code is never handed to a provider.
    """
    text = str(prompt)
    match = SKILL_PROMOTION_APPROVAL_PREFIX.match(text)
    if match is None:
        # An NFKC/confusable/invisible spelling of the command must not fall
        # through to ordinary model routing, and must be refused AS this verb.
        canonical_view = _secret_detection_view(text)
        if SKILL_PROMOTION_APPROVAL_PREFIX.match(canonical_view) is not None:
            raise GovernedMemoryCommandError(
                "Skill promotion approval contains non-canonical characters"
            )
        if _looks_like_skill_promotion_intent(text, SKILL_PROMOTION_APPROVAL_INTENT):
            raise GovernedMemoryCommandError(
                "This looks like a skill promotion approval but is not in the "
                f"exact required form: {SKILL_PROMOTION_APPROVAL_SHAPE}"
            )
        if skill_promotion_shape_guard(text) == "approve":
            # R-3: the words did not match any spelling we know, but the SHAPE
            # is an approval carrying a promotion id and a confirmation code.
            # Routing it to a model would hand over the code, so it is refused
            # as this verb even though we cannot name what was mistyped.
            raise GovernedMemoryCommandError(
                "This looks like a skill promotion approval but is not in the "
                f"exact required form: {SKILL_PROMOTION_APPROVAL_SHAPE}"
            )
        return None
    if len(text) > _MAX_PROJECT_FACT_COMMAND_CHARS:
        raise GovernedMemoryCommandError("Skill promotion approval is too large")
    # No NFKC check on the captured code, deliberately: SKILL_PROMOTION_CODE
    # is the url-safe alphabet, every character of which is NFKC-invariant, so
    # a captured code can never fold into a different one.  A confusable
    # spelling of the code fails the alphabet and is refused above, as a
    # non-canonical spelling of THIS verb.  A guard here would be unreachable;
    # the property is pinned in tests/test_governed_memory.py instead.
    return {"promotion_id": int(match.group(1)), "token": match.group(2)}


def parse_explicit_skill_promotion_rollback(prompt: str) -> dict[str, int] | None:
    """Parse one exact, standalone operator-authored skill-promotion rollback.

    ``Roll back skill promotion #<id>`` (also ``Rollback ...``, ``Revert
    ...``, a leading ``please``, a trailing ``.`` or ``!``).  A rollback
    carries no confirmation code: it only ever restores bytes the ladder
    itself replaced, so requiring a code would make the safe direction the
    harder one (design 3.6).
    """
    text = str(prompt)
    match = SKILL_PROMOTION_ROLLBACK_PREFIX.match(text)
    if match is None:
        canonical_view = _secret_detection_view(text)
        if SKILL_PROMOTION_ROLLBACK_PREFIX.match(canonical_view) is not None:
            raise GovernedMemoryCommandError(
                "Skill promotion rollback contains non-canonical characters"
            )
        if _looks_like_skill_promotion_intent(text, SKILL_PROMOTION_ROLLBACK_INTENT):
            raise GovernedMemoryCommandError(
                "This looks like a skill promotion rollback but is not in the "
                f"exact required form: {SKILL_PROMOTION_ROLLBACK_SHAPE}"
            )
        if skill_promotion_shape_guard(text) == "rollback":
            raise GovernedMemoryCommandError(
                "This looks like a skill promotion rollback but is not in the "
                f"exact required form: {SKILL_PROMOTION_ROLLBACK_SHAPE}"
            )
        return None
    if len(text) > _MAX_PROJECT_FACT_COMMAND_CHARS:
        raise GovernedMemoryCommandError("Skill promotion rollback is too large")
    return {"promotion_id": int(match.group(1))}


# The thirteen fixed receipts of design 6.1.  One table, used by the governed
# verb in agent.py AND by `jarvis ladder approve|rollback` in cli.py, so the
# two operator surfaces can never drift into telling the operator different
# things about the same refusal.  None of them repeats document content, and
# none of them ever contains the confirmation code.
SKILL_PROMOTION_APPROVAL_RECEIPTS: dict[str, str] = {
    "approved": (
        "Approved skill promotion #{id} for {family} (document {digest}). "
        "The previous version is kept for rollback."
    ),
    "approved_first": (
        "Approved skill promotion #{id} for {family} (document {digest}). "
        "No previous version existed; a rollback removes it."
    ),
    "approved_over_legacy": (
        "Approved skill promotion #{id} for {family} (document {digest}). "
        "The unapproved legacy document it replaced is kept for rollback."
    ),
    "missing": "No staged skill promotion matches that id; nothing changed.",
    "not_staged": "No staged skill promotion matches that id; nothing changed.",
    "token_mismatch": (
        "That approval token does not match the staged promotion; nothing changed."
    ),
    "proof_stale": (
        "Skill promotion #{id} no longer has a valid outcome proof; nothing changed."
    ),
    "gate_closed": (
        "The {family} calibration gate is closed; skill promotion #{id} cannot "
        "be approved."
    ),
    "ledger_regressed": (
        "The {family} calibration ledger regressed in its newest sealed epoch; "
        "skill promotion #{id} cannot be approved."
    ),
    "workspace_mismatch": (
        "Skill promotion #{id} belongs to another project; nothing changed."
    ),
    "workspace_unavailable": (
        "Skill promotion #{id} belongs to another project; nothing changed."
    ),
}
SKILL_PROMOTION_ROLLBACK_RECEIPTS: dict[str, str] = {
    "rolled_back": (
        "Rolled back skill promotion #{id} for {family}. "
        "The previous version is restored."
    ),
    "rolled_back_removed": (
        "Rolled back skill promotion #{id} for {family}. "
        "The learned skill is removed."
    ),
    "missing": "Skill promotion #{id} is not approved; nothing changed.",
    "not_approved": "Skill promotion #{id} is not approved; nothing changed.",
    "not_newest": (
        "Skill promotion #{id} is not the newest live promotion for {family}; "
        "roll back #{newest} first."
    ),
    "workspace_mismatch": (
        "Skill promotion #{id} belongs to another project; nothing changed."
    ),
    "workspace_unavailable": (
        "Skill promotion #{id} belongs to another project; nothing changed."
    ),
}
_SKILL_PROMOTION_FALLBACK = (
    "Skill promotion #{id} could not be {verb} ({reason}); nothing changed."
)


def skill_promotion_receipt(
    outcome: str,
    *,
    promotion_id: int,
    verb: str = "approve",
    family: str | None = None,
    digest: str | None = None,
    newest_id: int | None = None,
) -> str:
    """One fixed receipt for a ladder verb's outcome (design 6.1).

    ``outcome`` is a key of the table for ``verb`` -- a success key, or one of
    the store's closed refusal reasons.  An unrecognized reason still produces
    a receipt naming it rather than raising: a refusal the operator never hears
    about is worse than an ugly sentence, and the store's refusal set is closed
    but may gain a member before this table does.

    ``digest`` is the document's sha256; only its first twelve hex characters
    are printed.  Nothing here ever carries the confirmation code, document
    text, or lesson text.
    """
    table = (
        SKILL_PROMOTION_ROLLBACK_RECEIPTS
        if str(verb).strip().casefold() in {"rollback", "roll back", "revert"}
        else SKILL_PROMOTION_APPROVAL_RECEIPTS
    )
    template = table.get(str(outcome))
    if template is None:
        return _SKILL_PROMOTION_FALLBACK.format(
            id=int(promotion_id),
            verb=(
                "rolled back"
                if table is SKILL_PROMOTION_ROLLBACK_RECEIPTS
                else "approved"
            ),
            reason=str(outcome)[:64],
        )
    return template.format(
        id=int(promotion_id),
        family=str(family or "that family"),
        digest=str(digest or "")[:12],
        newest=int(newest_id) if newest_id is not None else 0,
    )


def redact_skill_promotion_command(prompt: str) -> str:
    """Replace a typed confirmation code with a placeholder for the transcript.

    Every governed verb persists the operator's raw turn to ``messages``, and
    conversation history is replayed into later prompts -- so storing the code
    verbatim would put it in front of the model, which design 7.11 forbids.
    Redaction is the CALLER's job (``Memory.apply_ladder_promotion`` writes
    what it is given, verbatim, and knows nothing of this grammar), so both
    the agent and the CLI route the operator's turn through here.

    The id survives, because the id is the operator-facing identity and the
    transcript must still show what the operator did.  Nothing is lost: the
    code is single use and is consumed by the approval.
    """
    text = str(prompt)
    match = SKILL_PROMOTION_APPROVAL_PREFIX.match(text)
    if match is None:
        # R-3: the exact parser did not claim this turn, but it may still
        # carry a code beside a promotion id -- a confusable or hyphenated
        # spelling, or a wording nobody anticipated.  Mask on the way past so
        # a fall-through can never persist the code into `messages`.
        return mask_skill_promotion_code(text)
    start, end = match.span(2)
    return f"{text[:start]}<confirmation code>{text[end:]}"


def project_claim_scope(project_id: int) -> str:
    if isinstance(project_id, bool) or not isinstance(project_id, int) or project_id <= 0:
        raise ValueError("project_id must be a positive integer")
    if project_id > 9_223_372_036_854_775_807:
        raise ValueError("project_id is out of range")
    return f"project:{project_id}"
