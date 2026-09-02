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
    if not isinstance(value, str):
        raise GovernedMemoryCommandError(
            f"Project fact {name} must be a string"
        )
    if _contains_unsupported_unicode(value):
        raise GovernedMemoryCommandError(
            f"Project fact {name} contains unsupported control characters"
        )
    normalized = " ".join(unicodedata.normalize("NFKC", value).split())
    if not normalized or len(normalized) > _PROJECT_FACT_LIMITS[name]:
        raise GovernedMemoryCommandError(
            f"Project fact {name} must contain 1-{_PROJECT_FACT_LIMITS[name]} characters"
        )
    for inspected in _inspection_forms(normalized):
        if _contains_unsupported_unicode(inspected):
            raise GovernedMemoryCommandError(
                f"Project fact {name} contains unsupported control characters"
            )
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


def project_claim_scope(project_id: int) -> str:
    if isinstance(project_id, bool) or not isinstance(project_id, int) or project_id <= 0:
        raise ValueError("project_id must be a positive integer")
    if project_id > 9_223_372_036_854_775_807:
        raise ValueError("project_id is out of range")
    return f"project:{project_id}"
