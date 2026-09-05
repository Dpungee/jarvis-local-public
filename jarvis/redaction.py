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
_SENSITIVE_KEY_PHRASE_PATTERN = (
    r"password|passwd|api[\s_.-]?key|access[\s_.-]?key|secret[\s_.-]?key|"
    r"private[\s_.-]?key|access[\s_.-]?token|refresh[\s_.-]?token|"
    r"session[\s_.-]?token|auth[\s_.-]?token|oauth[\s_.-]?token|"
    r"id[\s_.-]?token|client[\s_.-]?secret|authorization|credentials?|"
    r"session[\s_.-]?cookie|cookie|recovery[\s_.-]?code|mfa[\s_.-]?code"
)
SENSITIVE_KEY_PHRASE = re.compile(
    rf"(?<![A-Za-z0-9])(?:{_SENSITIVE_KEY_PHRASE_PATTERN})(?![A-Za-z0-9])",
    re.I,
)
_GENERIC_SECRET_DESCRIPTOR_PATTERN = (
    r"value|field|content|current|credentials?|auth|authentication|"
    r"authorization|login|account|key"
)
GENERIC_SECRET_DESCRIPTOR_PHRASE = re.compile(
    rf"(?<![A-Za-z0-9])(?:"
    rf"(?:token|secret)[\s._/\\-]+(?:{_GENERIC_SECRET_DESCRIPTOR_PATTERN})|"
    rf"(?:{_GENERIC_SECRET_DESCRIPTOR_PATTERN})[\s._/\\-]+(?:token|secret)"
    rf")(?![A-Za-z0-9])",
    re.I,
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


def _secret_detection_view_of_normalized(normalized: str) -> str:
    """The credential-detection view of an already normalized string.

    Split out of ``_secret_detection_view`` so a caller that has to run both
    the secret and the private-identifier screens can normalize once (M3
    §1.3: the migration-48 budget depends on it).  Behaviour is unchanged.
    """
    decomposed = unicodedata.normalize("NFKD", normalized)
    detection = unicodedata.normalize("NFKC", "".join(
        character
        for character in decomposed
        if not unicodedata.category(character).startswith("M")
    )).translate(_SECRET_CONFUSABLE_TRANSLATION)
    for pattern, canonical in _SENSITIVE_KEY_DETECTION_PATTERNS:
        detection = pattern.sub(canonical, detection)
    return detection


def _secret_detection_view(value: str) -> str:
    """Return a mark-free, separator-tolerant view for credential detection."""
    return _secret_detection_view_of_normalized(
        normalize_private_identifier_text(value)
    )


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


_USER_HOME_PATTERNS: tuple[tuple[re.Pattern[str], int], ...] = (
    (WINDOWS_USER_HOME, 2),
    (POSIX_USER_HOME, 2),
    (UNC_USER_HOME, 3),
)


def _pinned_identifier_kind(normalized: str) -> str | None:
    """``"email"`` / ``"user_home"`` for an already normalized string.

    This is the body of ``contains_private_identifier``: the pinned screen the
    sealed V3/V5 fixtures and the governed write gate run under.  It exists as
    one function so the widened scan of M3 §6.2 can never disagree with it.
    """
    if _private_email_spans(normalized):
        return "email"
    for pattern, user_group in _USER_HOME_PATTERNS:
        for match in pattern.finditer(normalized):
            if str(match.group(user_group)).casefold() != "[user]":
                return "user_home"
    return None


def contains_private_identifier(value: str) -> bool:
    """Return whether text contains an email or concrete user-home path."""
    return _pinned_identifier_kind(
        normalize_private_identifier_text(value)
    ) is not None


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


def contains_explicit_sensitive_key_phrase(value: str) -> bool:
    """Return whether canonical text contains an established credential key."""
    return SENSITIVE_KEY_PHRASE.search(_secret_detection_view(value)) is not None


def contains_sensitive_key_phrase(value: str) -> bool:
    """Return whether canonical text contains a bounded credential-key name."""
    detection = _secret_detection_view(value)
    return (
        SENSITIVE_KEY_PHRASE.search(detection) is not None
        or GENERIC_SECRET_DESCRIPTOR_PHRASE.search(detection) is not None
    )


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


# --- the widened private-identifier screen (VTMF M3 §6.2) -------------------
#
# Pure functions with no state: no module-level cache, no memoisation, no
# configuration read at call time, so the sealed holdout, the release-policy
# checker and the read path always agree on the same input.
# ``contains_private_identifier`` above is unchanged and stays the screen the
# live claims lane, the sealed V3/V5 fixtures and the governed write gate run
# under; the functions below are strictly wider and are used by the graph
# projection, the graph chain rows and the temporal history helpers.
#
# Every regex sees at most ``SCAN_LIMIT`` characters, so no pattern can be
# handed an unbounded input; a longer value is screened on its first
# ``SCAN_LIMIT`` characters plus a fixed, regex-free rule over the tail.

SCAN_LIMIT = 512
NEGATIVE_CONTEXT_WINDOW = 24
NEGATIVE_SUFFIX_WINDOW = 8
PRIVATE_IDENTIFIER_KINDS: tuple[str, ...] = (
    "email", "user_home", "ip_host_email", "phone", "ipv4", "ipv6",
    "ssn", "card", "street_address", "long_value",
)

_OCTET = r"(?:25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])"
_H16 = r"[0-9A-Fa-f]{1,4}"
_IPV6_FULL = rf"(?:{_H16}:){{7}}{_H16}"
_IPV6_COMPRESSED = rf"(?:{_H16}(?::{_H16}){{0,6}})?::(?:{_H16}(?::{_H16}){{0,6}})?"

# The lookbehind forbids a digit or a dot, not a letter: "v10.0.0.7" is a
# host with a letter glued to it, and the version exemption below is what
# separates it from "v1.2.3.4".
IPV4 = re.compile(rf"(?<![.\d]){_OCTET}(?:\.{_OCTET}){{3}}(?![\w.])")
# The widened e-mail pattern: the pinned PRIVATE_EMAIL above is unchanged.
PRIVATE_EMAIL_WIDE = re.compile(
    r"(?<![\w.+/-])[\w.!#$%&'*+/=?^`{|}~-]+@[\w-]+(?:\.[\w-]+)*"
    r"\.(?:xn--[\w-]+|[A-Za-z]{2,})(?!\w)",
    re.UNICODE,
)
IPV6 = re.compile(rf"(?<![\w:.])(?:{_IPV6_FULL}|{_IPV6_COMPRESSED})(?![\w:.])")
IP_HOST_EMAIL = re.compile(
    r"(?<![\w.+/-])[\w.!#$%&'*+/=?^`{|}~-]+@"
    rf"(?:\[(?:IPv6:)?(?:{_IPV6_FULL}|{_IPV6_COMPRESSED}|{_OCTET}(?:\.{_OCTET}){{3}})\]"
    rf"|{_OCTET}(?:\.{_OCTET}){{3}})(?![\w.-])",
    re.I,
)
SSN = re.compile(r"(?<![\d-])[0-9]{3}-[0-9]{2}-[0-9]{4}(?![\d-])")
# Separated groups OR one unbroken run: a PAN is usually written without
# separators, and the sealed holdout leaked a Luhn-valid 16-digit run
# straight into a chain row because only the grouped form was matched.
CARD = re.compile(
    r"(?<![\w-])(?:[0-9]{4}(?:[ -][0-9]{4}){2,3}(?:[ -][0-9]{1,3})?"
    r"|[0-9]{13,19})(?![\w-])"
)
# Every alternative starts with a distinct character class, so the engine has
# no ambiguity to backtrack over: one deterministic left-to-right pass.
PHONE_CANDIDATE = re.compile(r"(?<![\w+.])\+?[0-9](?:[0-9]|[ ()-]{1,2}[0-9])*(?![\w.])")
_STREET_TYPE = (
    r"Street|St|Avenue|Ave|Road|Rd|Boulevard|Blvd|Lane|Ln|Drive|Dr|Court|Ct"
    r"|Place|Pl|Way|Terrace|Ter|Highway|Hwy|Parkway|Pkwy|Circle|Cir"
)
STREET_ADDRESS = re.compile(
    rf"(?<![\w-])[0-9]{{1,6}}[A-Za-z]?\s+(?:[A-Z][A-Za-z'.-]{{0,19}}\s+){{1,3}}"
    rf"(?:{_STREET_TYPE})(?![\w'-])\.?"
)

# Negative context is NOT a general net.  The red team of 2026-09-03 showed
# that a generic keyword window exempts every kind at once, so "case
# 078-05-1120", "invoice 4111 1111 1111 1111" and "rack 10.0.0.7" all passed:
# a word in front of a credential does not make it stop being one.  Only two
# kinds have any exemption at all, each narrow and each justified by a real
# ambiguity in this codebase's vocabulary:
#
#   ipv4   a dotted quad is genuinely ambiguous with a version number, but
#          only for a PUBLIC address: no version number is 10.0.0.7.  So a
#          version word or a version suffix exempts a public quad and never a
#          private one, and a CIDR /0../31 exempts a network (never /32,
#          which is one host).
#   phone  a run of fewer than ten digits is an id, a range or a date in this
#          vocabulary, and an ISBN is not a telephone number.
#
# ssn, card, ipv6, street_address, e-mail and user-home paths have NO
# exemption: nothing that precedes or follows them makes them safe.
VERSION_PREFIX = re.compile(
    r"(?i)(?:^|[^\w])(?:v|ver|vers|version|rev|revision|release|build"
    r"|schema|semver|api|sdk|firmware|driver|kernel|patch|migration)"
    r"[\s:=#._-]{0,4}$"
)
VERSION_SUFFIX = re.compile(r"(?i)^(?:-rc[0-9]|-beta|-alpha|-dev|\+[\w.])")
# A network, not a host.  /32 is one host and is never exempt.
CIDR_SUFFIX = re.compile(r"^/([0-9]{1,3})(?![\w.])")
ISBN_WORD = re.compile(r"(?i)(?:^|[^\w])(?:isbn|isbn10|isbn13|issn|ismn)[\s:=#._-]{0,4}$")
ISBN_SHAPE = re.compile(r"^97[89][- ]")
_RUN_CHARACTERS = frozenset(
    "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz.:+_/-"
)
# Ranges that make a dotted quad a host on somebody's network rather than a
# version number, however it is introduced.
_PRIVATE_IPV4_RANGES: tuple[tuple[tuple[int, ...], int], ...] = (
    ((10,), 8), ((172, 16), 12), ((192, 168), 16), ((169, 254), 16), ((100, 64), 10),
)
_PHONE_EXEMPT_MAX_DIGITS = 10
_PHONE_MIN_DIGITS = 7
_PHONE_MAX_DIGITS = 15
_PHONE_SEPARATORS = frozenset(" ()-")
_CARD_MIN_DIGITS = 13
_CARD_MAX_DIGITS = 19
_LONG_VALUE_SEPARATORS = frozenset(" .-()+")
_LONG_VALUE_MIN_DIGITS = 7
_LONG_VALUE_MIN_HEX_GROUPS = 3
_HEX_RUN_CHARACTERS = frozenset("0123456789abcdefABCDEF:")


def _enclosing_run(text: str, start: int, end: int) -> str:
    """The maximal identifier-ish span containing ``text[start:end]``.

    Two while loops, no regex; used by the ISBN test.
    """
    left = start
    while left > 0 and text[left - 1] in _RUN_CHARACTERS:
        left -= 1
    right = end
    while right < len(text) and text[right] in _RUN_CHARACTERS:
        right += 1
    return text[left:right]


def _prefix_window(text: str, start: int) -> str:
    return text[max(0, start - NEGATIVE_CONTEXT_WINDOW):start]


def _ipv4_in_private_range(match: str) -> bool:
    try:
        octets = tuple(int(part) for part in match.split("."))
    except ValueError:
        return False
    for prefix, _bits in _PRIVATE_IPV4_RANGES:
        if octets[:len(prefix)] == prefix:
            if prefix == (172, 16):
                return 16 <= octets[1] <= 31
            if prefix == (100, 64):
                return 64 <= octets[1] <= 127
            return True
    return False


def _ipv4_exempt(text: str, start: int, end: int, match: str) -> bool:
    """A dotted quad that is a version number or a network, not a host."""
    cidr = CIDR_SUFFIX.match(text[end:end + NEGATIVE_SUFFIX_WINDOW])
    if cidr is not None:
        # /32 is a single host and stays screened; /128 on IPv6 is never
        # exempt because IPv6 has no exemption at all.
        return int(cidr.group(1)) <= 31
    if _ipv4_in_private_range(match):
        return False
    return (
        VERSION_PREFIX.search(_prefix_window(text, start)) is not None
        or VERSION_SUFFIX.match(text[end:end + NEGATIVE_SUFFIX_WINDOW]) is not None
    )


def _phone_exempt(text: str, start: int, end: int, match: str) -> bool:
    """Fewer than ten digits is an id, a range or a date here; an ISBN is not
    a telephone number."""
    if sum(1 for character in match if character.isdigit()) < _PHONE_EXEMPT_MAX_DIGITS:
        return True
    if ISBN_SHAPE.match(_enclosing_run(text, start, end)) is not None:
        return True
    return ISBN_WORD.search(_prefix_window(text, start)) is not None


def _accept_any(_text: str) -> bool:
    return True


def _luhn_ok(text: str) -> bool:
    digits = [int(character) for character in text if character.isdigit()]
    if not _CARD_MIN_DIGITS <= len(digits) <= _CARD_MAX_DIGITS:
        return False
    total = 0
    for index, digit in enumerate(reversed(digits)):
        if index % 2:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0


def _phone_is_plausible(text: str) -> bool:
    digits = sum(1 for character in text if character.isdigit())
    if not _PHONE_MIN_DIGITS <= digits <= _PHONE_MAX_DIGITS:
        return False
    # A bare digit run is an order number or an id in this vocabulary; a
    # phone number carries a separator or an international prefix.
    return text.startswith("+") or any(
        character in _PHONE_SEPARATORS for character in text
    )


def _ipv4_is_private_host(text: str) -> bool:
    """A host address, excluding loopback, unspecified, broadcast, the three
    documentation ranges and multicast (a network is not a host)."""
    parts = text.split(".")
    if len(parts) != 4:
        return False
    try:
        octets = tuple(int(part) for part in parts)
    except ValueError:
        return False
    if any(octet > 255 for octet in octets):
        return False
    if octets[0] == 127 or octets in {(0, 0, 0, 0), (255, 255, 255, 255)}:
        return False
    if 224 <= octets[0] <= 239:
        return False
    return octets[:3] not in {(192, 0, 2), (198, 51, 100), (203, 0, 113)}


def _ipv6_groups(text: str) -> list[str]:
    lowered = text.casefold()
    if "::" in lowered:
        head, _separator, tail = lowered.partition("::")
        pieces = head.split(":") + tail.split(":")
    else:
        pieces = lowered.split(":")
    return [piece for piece in pieces if piece]


def _ipv6_is_private_host(text: str) -> bool:
    """At least two groups, at least one of them carrying a decimal digit, and
    not an exempt address.  The digit test is why C++-style ``abc::def`` does
    not fire; the declared miss is an all-letter address such as
    ``dead::beef``."""
    groups = _ipv6_groups(text)
    if len(groups) < 2:
        return False
    if not any(
        any(character.isdigit() for character in group) for group in groups
    ):
        return False
    if groups[0] == "2001" and groups[1] == "db8":
        return False
    return True


def _is_compressed_hex_run(run: str) -> bool:
    """A ``::``-compressed hex run of at least three groups carrying a digit."""
    if "::" not in run:
        return False
    groups = _ipv6_groups(run)
    return len(groups) >= _LONG_VALUE_MIN_HEX_GROUPS and any(
        any(character.isdigit() for character in group) for group in groups
    )


def _no_exemption(_text: str, _start: int, _end: int, _match: str) -> bool:
    return False


# kind, pattern, plausibility test, context exemption.  Only ipv4 and phone
# have an exemption at all (see the note above the patterns).
_WIDENED_RULES: tuple[tuple[str, re.Pattern[str], Any, Any], ...] = (
    ("ip_host_email", IP_HOST_EMAIL, _accept_any, _no_exemption),
    ("ssn", SSN, _accept_any, _no_exemption),
    ("card", CARD, _luhn_ok, _no_exemption),
    ("ipv6", IPV6, _ipv6_is_private_host, _no_exemption),
    ("ipv4", IPV4, _ipv4_is_private_host, _ipv4_exempt),
    ("phone", PHONE_CANDIDATE, _phone_is_plausible, _phone_exempt),
    ("street_address", STREET_ADDRESS, _accept_any, _no_exemption),
)


def _widened_email_kind(text: str) -> str | None:
    """The widened e-mail check.

    Identical to the pinned one except for the trailing boundary: ``(?!\\w)``
    instead of ``(?![\\w-])``, so ``ops@example.com-rc1`` is caught here (the
    pinned pattern lets the greedy label swallow ``-rc1`` and then rejects the
    address because ``com-rc1`` is not alphabetic).  The pinned screen keeps
    its own boundary byte for byte: the sealed fixtures run under it.
    """
    detection, _indexes = _email_detection_view(text)
    for match in PRIVATE_EMAIL_WIDE.finditer(detection):
        if _private_email_match_is_identifier(match.group(0)):
            return "email"
    return None


def _scan(text: str) -> str | None:
    """Every pattern, in the fixed order of §6.2; the first match wins.

    ``text`` is never longer than ``SCAN_LIMIT``: it is the only place a
    regex runs, and the caller truncates.
    """
    if _widened_email_kind(text) is not None:
        return "email"
    pinned = _pinned_identifier_kind(text)
    if pinned is not None:
        return pinned
    for kind, pattern, accept, exempt in _WIDENED_RULES:
        for match in pattern.finditer(text):
            if not accept(match.group(0)):
                continue
            if exempt(text, match.start(), match.end(), match.group(0)):
                continue
            return kind
    return None


def _long_value_kind(tail: str) -> str | None:
    """``"long_value"`` when the unscanned tail of an over-long value holds
    either a maximal run of digits and the separators ``. - ( ) +`` and space
    with at least seven digits and at least one separator, or a
    ``::``-compressed hex run of at least three groups carrying a digit.
    Nothing else.

    One left-to-right pass with counters: no regex, no backtracking, O(n).  A
    value over ``SCAN_LIMIT`` characters is never an entity label
    (``ENTITY_LABEL_MAX_CHARS`` is 80) and is prose-shaped in practice, so a
    conservative digit-run rule closes the "hide the phone number at
    character 900" hole at no cost.
    """
    digits = 0
    separators = 0
    for character in tail:
        if character.isdigit():
            digits += 1
            if digits >= _LONG_VALUE_MIN_DIGITS and separators >= 1:
                return "long_value"
        elif character in _LONG_VALUE_SEPARATORS and digits:
            separators += 1
        else:
            digits = 0
            separators = 0
    run: list[str] = []
    for character in tail:
        if character in _HEX_RUN_CHARACTERS:
            run.append(character)
            continue
        if run and _is_compressed_hex_run("".join(run)):
            return "long_value"
        run = []
    if run and _is_compressed_hex_run("".join(run)):
        return "long_value"
    return None


# Every alternative of SECRET_VALUE needs one of these in the **detection
# view**: the PEM rule, a vendor token prefix, the word "bearer", or a
# canonical sensitive key name (every one of _CANONICAL_SENSITIVE_KEYS
# contains pass, key, token, secret, cookie, auth, cred or code, and the view
# is where a separator-split "a p i _ k e y" has already been canonicalised).
# A view holding none of them cannot match, so the expensive alternation never
# runs over it.  Conservative by construction: it can only skip a window no
# alternative could have matched.
_SECRET_PREFILTER = re.compile(
    r"(?i)-----|sk-|gh[pousr]_|github_pat_|xox|aiza|akia|eyj|bearer"
    r"|pass|key|token|secret|cookie|auth|cred|code"
)


def _scan_windows(value: str) -> tuple[tuple[str, ...], str]:
    """``(windows, middle)``: the bounded, normalized text every pattern in
    this module is allowed to see, and the raw middle nothing but the run
    rules see.

    The first and last ``SCAN_LIMIT`` characters are scanned with the **full**
    kind set; the middle of a value longer than ``2 * SCAN_LIMIT`` is covered
    only by the digit-run, compressed-hex and "@" rules, and is scanned
    un-normalized.  Both facts are stated in ``docs/MEMORY_GRAPH.md`` rather
    than implied: an identifier buried in the middle of a value over 1,024
    characters is reported as ``long_value`` when it carries a digit run or a
    compressed hex run, and is otherwise missed.

    Normalization is windowed too, with a margin, because it is the dominant
    cost at 4,000 characters (2.4 ms of a 5.3 ms call) and because
    ``SECRET_VALUE`` backtracks badly on long digit-and-dash runs — the
    correctness review measured 73–75 ms over a whole value, and 296 ms under
    load, which alone blew the graph's 25 ms read budget.
    """
    raw = str(value)
    if len(raw) <= SCAN_LIMIT:
        return (normalize_private_identifier_text(raw),), ""
    margin = SCAN_LIMIT * 2
    head = normalize_private_identifier_text(raw[:margin])[:SCAN_LIMIT]
    tail = normalize_private_identifier_text(raw[-margin:])[-SCAN_LIMIT:]
    return (head, tail), raw[SCAN_LIMIT:-SCAN_LIMIT]


def _middle_kind(middle: str) -> str | None:
    if not middle:
        return None
    if "@" in middle:
        return "long_value"
    return _long_value_kind(middle)


def _over_long(value: str) -> bool:
    """An over-long value is itself a private-identifier kind.

    Design 2.4 lists "over-long value" beside e-mail, phone and the rest as a
    value that must never be a node and must not reach a cue row, and 1.4's
    ``long_value`` directive is described as "the >512 scan-cap rule": past
    the scan cap the screen cannot see the whole value, so it must not vouch
    for it.  The sealed holdout leaked a 600-character value because only the
    digit-run and hex rules looked past the cap.
    """
    return len(str(value)) > SCAN_LIMIT


def private_identifier_kind(value: str) -> str | None:
    """The widened private-identifier kind of ``value``, or ``None``.

    Closed set: ``PRIVATE_IDENTIFIER_KINDS``.  Pure and total: any input is
    coerced with ``str`` and no input raises.
    """
    windows, middle = _scan_windows(value)
    for window in windows:
        kind = _scan(window)
        if kind is not None:
            return kind
    kind = _middle_kind(middle)
    if kind is not None:
        return kind
    return "long_value" if _over_long(normalize_private_identifier_text(value)) else None


def contains_private_identifier_extended(value: str) -> bool:
    """Whether ``value`` carries any widened private identifier."""
    return private_identifier_kind(value) is not None


def screen_endpoint(text: str) -> tuple[bool, str | None]:
    """``(is_screened, reason)`` for one claim endpoint, normalizing **once**.

    ``reason`` is ``"secret"``, one of ``PRIVATE_IDENTIFIER_KINDS``, or
    ``None``.  Every graph projection, node-admission and chain-row site calls
    this instead of two screens that would each re-normalize: the
    migration-48 budget of M3 §1.3 depends on the single normalization.
    """
    windows, middle = _scan_windows(text)
    for window in windows:
        detection = _secret_detection_view_of_normalized(window)
        if _SECRET_PREFILTER.search(detection) is None:
            continue
        if SECRET_VALUE.search(detection) is not None:
            return True, "secret"
    for window in windows:
        kind = _scan(window)
        if kind is not None:
            return True, kind
    kind = _middle_kind(middle)
    if kind is None and _over_long(normalize_private_identifier_text(text)):
        kind = "long_value"
    return kind is not None, kind
