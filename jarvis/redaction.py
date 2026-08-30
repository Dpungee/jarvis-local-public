from __future__ import annotations

import re
import unicodedata
from typing import Any


CONTROL_CODES = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_SENSITIVE_KEY_PATTERN = (
    r"password|passwd|api[\s_.-]?key|access[\s_.-]?key|secret[\s_.-]?key|"
    r"private[\s_.-]?key|access[\s_.-]?token|refresh[\s_.-]?token|"
    r"session[\s_.-]?token|auth[\s_.-]?token|oauth[\s_.-]?token|"
    r"id[\s_.-]?token|client[\s_.-]?secret|authorization|credentials?|"
    r"session[\s_.-]?cookie|cookie|recovery[\s_.-]?code|mfa[\s_.-]?code|"
    r"token|secret"
)
_NAMESPACED_SENSITIVE_KEY_PATTERN = (
    rf"(?:[A-Za-z0-9]+[_.-]+)*(?:{_SENSITIVE_KEY_PATTERN})"
)
SENSITIVE_KEY = re.compile(
    rf"^(?:{_NAMESPACED_SENSITIVE_KEY_PATTERN})$", re.I
)
SECRET_VALUE = re.compile(
    r"(?i)(-----BEGIN [A-Z _.-]*PRIVATE[ _.-]*KEY-----.*?"
    r"-----END [A-Z _.-]*PRIVATE[ _.-]*KEY-----|"
    r"\bsk-(?:proj-)?[A-Za-z0-9_-]{12,}|"
    r"\bgh[pousr]_[A-Za-z0-9_-]{12,}|"
    r"\bgithub_pat_[A-Za-z0-9_]{12,}|"
    r"\bxox[baprs]-[A-Za-z0-9-]{12,}|\bAIza[A-Za-z0-9_-]{20,}|"
    r"\bAKIA[0-9A-Z]{16}\b|"
    r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}|"
    rf"(?:[\"'](?:{_NAMESPACED_SENSITIVE_KEY_PATTERN})[\"']|"
    rf"(?<![A-Za-z0-9_])(?:{_NAMESPACED_SENSITIVE_KEY_PATTERN})"
    rf"(?![A-Za-z0-9_]))\s*[:=]\s*"
    r"(?:\"[^\"]*\"|'[^']*'|\S+)|"
    r"\bbearer\s+[A-Za-z0-9._~-]{8,})",
    re.S,
)

# Durable lessons can be reused long after the originating task.  Secrets are
# already removed above, but ordinary identifiers such as an email address or
# a concrete user-home path are private too and must not become reusable
# guidance.  Keep these patterns deliberately narrow so normal prose and
# workspace-relative paths remain useful.
PRIVATE_EMAIL = re.compile(
    r"(?<![\w.+/-])[\w.!#$%&'*+/=?^`{|}~-]+@[\w-]+(?:\.[\w-]+)+(?![\w-])",
    re.UNICODE,
)
WINDOWS_USER_HOME = re.compile(
    r"(?i)(?<![A-Z0-9_])([A-Z]:[\\/]+Users[\\/]+)([^\\/:\"'<>|\r\n\t]+)"
)
POSIX_USER_HOME = re.compile(
    r"(?<![A-Za-z0-9_])(/(?:home|Users)/)([^/\"'<>|\r\n\t]+)"
)
UNC_USER_HOME = re.compile(
    r"(?i)(?<![A-Z0-9_])((?:\\\\|//))[^\\/\s:\"'<>|]+"
    r"([\\/]+(?:Users|home|homes)[\\/]+)([^\\/:\"'<>|\r\n\t]+)"
)
_EMAIL_DOT_TRANSLATION = str.maketrans({
    "\u3002": ".",  # ideographic full stop
    "\uff0e": ".",  # fullwidth full stop
    "\uff61": ".",  # halfwidth ideographic full stop
})
_SECRET_CONFUSABLE_TRANSLATION = str.maketrans({
    # A deliberately small detection-only map for common Cyrillic/Greek Latin
    # lookalikes.  It is never used to rewrite ordinary persisted prose.
    "\u0430": "a", "\u0410": "A", "\u03b1": "a", "\u0391": "A",
    "\u0435": "e", "\u0415": "E", "\u03b5": "e", "\u0395": "E",
    "\u043e": "o", "\u041e": "O", "\u03bf": "o", "\u039f": "O",
    "\u0440": "p", "\u0420": "P", "\u03c1": "p", "\u03a1": "P",
    "\u0441": "c", "\u0421": "C",
    "\u0445": "x", "\u0425": "X", "\u03c7": "x", "\u03a7": "X",
    "\u0443": "y", "\u0423": "Y", "\u03c5": "y", "\u03a5": "Y",
    "\u0456": "i", "\u0406": "I", "\u03b9": "i", "\u0399": "I",
})
_CANONICAL_SENSITIVE_KEYS = (
    "session_cookie", "client_secret", "authorization", "refresh_token",
    "session_token", "access_token", "private_key", "recovery_code",
    "credentials", "credential", "secret_key", "access_key", "oauth_token",
    "password", "passwd", "session_cookie", "auth_token", "cookie",
    "api_key", "id_token", "mfa_code", "token", "secret",
)


def _separator_tolerant_key_pattern(canonical: str) -> re.Pattern[str]:
    letters = canonical.replace("_", "")
    body = r"[\s._/\\-]*".join(re.escape(character) for character in letters)
    return re.compile(r"(?<![A-Za-z0-9])" + body + r"(?![A-Za-z0-9])", re.I)


_SENSITIVE_KEY_DETECTION_PATTERNS = tuple(
    (_separator_tolerant_key_pattern(canonical), canonical)
    for canonical in _CANONICAL_SENSITIVE_KEYS
)


def _is_default_ignorable(character: str) -> bool:
    """Recognize invisible Unicode code points useful for identifier spoofing."""
    codepoint = ord(character)
    return (
        unicodedata.category(character) == "Cf"
        or codepoint == 0x034F  # COMBINING GRAPHEME JOINER
        or 0x115F <= codepoint <= 0x1160  # Hangul fillers
        or 0x17B4 <= codepoint <= 0x17B5  # Khmer inherent vowels
        or 0x180B <= codepoint <= 0x180F  # Mongolian selectors/separator
        or codepoint == 0x2065  # reserved default-ignorable
        or codepoint == 0x3164  # Hangul filler
        or 0xFE00 <= codepoint <= 0xFE0F  # variation selectors
        or codepoint == 0xFFA0  # halfwidth Hangul filler
        or 0xFFF0 <= codepoint <= 0xFFF8  # specials
        or 0xE0000 <= codepoint <= 0xE0FFF  # tags/selectors supplement
    )


def normalize_private_identifier_text(value: str) -> str:
    """Canonicalize text before privacy/security identifier inspection."""
    normalized = unicodedata.normalize("NFKC", CONTROL_CODES.sub("", str(value)))
    normalized = "".join(
        character
        for character in normalized
        if not _is_default_ignorable(character)
    )
    # Removing a joiner can make formerly separated combining marks compose.
    return unicodedata.normalize("NFKC", normalized).translate(
        _EMAIL_DOT_TRANSLATION
    )


def _secret_detection_view(value: str) -> str:
    """Return a mark-free, separator-tolerant view for credential detection."""
    decomposed = unicodedata.normalize(
        "NFKD", normalize_private_identifier_text(value)
    )
    detection = unicodedata.normalize("NFKC", "".join(
        character
        for character in decomposed
        if not unicodedata.category(character).startswith("M")
    )).translate(_SECRET_CONFUSABLE_TRANSLATION)
    for pattern, canonical in _SENSITIVE_KEY_DETECTION_PATTERNS:
        detection = pattern.sub(canonical, detection)
    return detection


def private_identifier_text_was_obfuscated(value: str) -> bool:
    """Return whether canonical identifier scanning had to change the text."""
    raw = str(value)
    return (
        normalize_private_identifier_text(raw) != raw
        or any(
            _is_default_ignorable(character)
            or unicodedata.category(character).startswith("M")
            for character in raw
        )
    )


def _email_detection_view(value: str) -> tuple[str, list[int]]:
    """Return a mark-free email scan view plus indexes into ``value``."""
    characters: list[str] = []
    indexes: list[int] = []
    for index, character in enumerate(value):
        if unicodedata.category(character).startswith("M"):
            continue
        characters.append(character)
        indexes.append(index)
    return "".join(characters), indexes


def _private_email_match_is_identifier(address: str) -> bool:
    address = str(address).casefold()
    local_part, domain = address.rsplit("@", 1)
    final_label = domain.rsplit(".", 1)[-1]
    if re.fullmatch(r"\{[a-z0-9_-]+\}", local_part) is not None:
        return False
    return final_label.isalpha() or final_label.startswith("xn--")


def _private_email_spans(value: str) -> list[tuple[int, int, str]]:
    detection, indexes = _email_detection_view(value)
    spans: list[tuple[int, int, str]] = []
    for match in PRIVATE_EMAIL.finditer(detection):
        address = match.group(0)
        if not _private_email_match_is_identifier(address):
            continue
        start = indexes[match.start()]
        end = indexes[match.end() - 1] + 1
        while end < len(value) and unicodedata.category(value[end]).startswith("M"):
            end += 1
        spans.append((start, end, address))
    return spans


def private_email_addresses(value: str) -> tuple[str, ...]:
    """Return canonical private email candidates for release-policy checks."""
    normalized = normalize_private_identifier_text(value)
    return tuple(address for _start, _end, address in _private_email_spans(normalized))


def redact_secrets(value: str, replacement: str = "[REDACTED]") -> str:
    # Credential matching must operate on the same canonical Unicode view as
    # private-identifier detection.  Otherwise fullwidth separators, invisible
    # joiners, or compatibility characters can hide an assignment from the
    # scanner and become ordinary model-facing memory after later NFKC use.
    normalized = normalize_private_identifier_text(value)
    detection = _secret_detection_view(normalized)
    if SECRET_VALUE.search(detection) and not SECRET_VALUE.search(normalized):
        # Mapping arbitrary detection spans back through removed marks and
        # separators is error-prone.  Fail closed by redacting the whole value.
        return replacement
    return SECRET_VALUE.sub(replacement, normalized)


def redact_private_identifiers(value: str) -> str:
    """Redact identity-bearing text before it can become durable guidance."""
    normalized = normalize_private_identifier_text(redact_secrets(value))
    spans = _private_email_spans(normalized)
    if spans:
        pieces: list[str] = []
        cursor = 0
        for start, end, _address in spans:
            pieces.extend((normalized[cursor:start], "[EMAIL]"))
            cursor = end
        pieces.append(normalized[cursor:])
        normalized = "".join(pieces)
    normalized = WINDOWS_USER_HOME.sub(r"\1[USER]", normalized)
    normalized = POSIX_USER_HOME.sub(r"\1[USER]", normalized)
    return UNC_USER_HOME.sub(r"\1[HOST]\2[USER]", normalized)


def contains_private_identifier(value: str) -> bool:
    """Return whether text contains an email or concrete user-home path."""
    normalized = normalize_private_identifier_text(value)
    if _private_email_spans(normalized):
        return True
    for pattern, user_group in (
        (WINDOWS_USER_HOME, 2),
        (POSIX_USER_HOME, 2),
        (UNC_USER_HOME, 3),
    ):
        for match in pattern.finditer(normalized):
            if str(match.group(user_group)).casefold() != "[user]":
                return True
    return False


def contains_secret(value: str) -> bool:
    return SECRET_VALUE.search(_secret_detection_view(value)) is not None


def contains_obfuscated_secret(value: str) -> bool:
    """Return whether canonicalization reveals an additional secret match."""
    raw = CONTROL_CODES.sub("", str(value))
    normalized = _secret_detection_view(value)
    raw_matches = sum(1 for _match in SECRET_VALUE.finditer(raw))
    normalized_matches = sum(
        1 for _match in SECRET_VALUE.finditer(normalized)
    )
    return normalized_matches > raw_matches


def is_sensitive_key(value: str) -> bool:
    normalized = _secret_detection_view(value).strip().strip("\"'")
    return SENSITIVE_KEY.fullmatch(normalized) is not None


def is_redacted_descriptor(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == {"redacted", "bytes", "sha256"}
        and value.get("redacted") is True
        and isinstance(value.get("bytes"), int)
        and not isinstance(value.get("bytes"), bool)
        and value["bytes"] >= 0
        and isinstance(value.get("sha256"), str)
        and re.fullmatch(r"[0-9a-f]{64}", value["sha256"]) is not None
    )
