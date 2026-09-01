"""Closed, privacy-preserving objects for the Private -> Public JARVIS bridge.

This module intentionally depends only on the Python standard library.  It does
not retrieve private memory, accept model prompts, or perform I/O.  Callers may
construct one of the explicitly supported payloads and serialize it into a
digest-bound record after every field has passed the public-data checks below.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import math
import re
import time
import unicodedata
from dataclasses import dataclass
from typing import Any, Mapping, Union
from urllib.parse import parse_qsl, unquote_to_bytes, urlsplit, urlunsplit


PUBLIC_BRIDGE_SCHEMA_VERSION = 1
MAX_BRIDGE_LIFETIME_SECONDS = 366 * 24 * 60 * 60

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_SECRET = re.compile(
    r"(?is)(?:"
    r"-----BEGIN (?P<pem_kind>[A-Z0-9 ]{0,64}PRIVATE KEY)-----"
    r".*?(?:-----END (?P=pem_kind)-----|\Z)|"
    r"\bsk-(?:proj-)?[A-Za-z0-9_-]{12,}|"
    r"\bgh[pousr]_[A-Za-z0-9_-]{12,}|"
    r"\bgithub_pat_[A-Za-z0-9_]{12,}|"
    r"\bxox[baprs]-[A-Za-z0-9-]{12,}|"
    r"\bAKIA[0-9A-Z]{16}\b|"
    r"\bAIza[A-Za-z0-9_-]{20,}|"
    r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}|"
    r"\bbearer\s+[A-Za-z0-9._~-]{8,}|"
    r"\b(?:password|passwd|api[_ .-]?key|access[_ .-]?token|refresh[_ .-]?token|"
    r"client[_ .-]?secret|session[_ .-]?cookie|oauth[_ .-]?token|credentials?)"
    r"\s*[:=]\s*(?:\"[^\"]+\"|'[^']+'|\S+)"
    r")"
)
_EMAIL = re.compile(r"(?i)(?<![\w.+-])[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}(?![\w.-])")
_PHONE = re.compile(
    r"(?<!\d)(?:\+?1[ .-]?)?(?:\(\d{3}\)|\d{3})[ .-]\d{3}[ .-]\d{4}(?!\d)"
)
_PHONE_CONTEXTUAL = re.compile(
    r"(?i)\b(?:call|phone|telephone|mobile|contact)\s*(?:me|us|at)?\s*[:=]?\s*"
    r"\+?\d(?:[\d .()-]{6,18}\d)"
)
_OBFUSCATED_EMAIL = re.compile(
    r"(?i)\b[A-Z0-9._%+-]+\s*(?:\(|\[)?\s*at\s*(?:\)|\])?\s*"
    r"[A-Z0-9.-]+\.[A-Z]{2,}\b"
)
_SSN = re.compile(r"(?<!\d)\d{3}-\d{2}-\d{4}(?!\d)")
_DATE_OF_BIRTH = re.compile(
    r"(?i)\b(?:date\s+of\s+birth|d\.?o\.?b\.?|born)\s*(?:is|:|=)?\s*"
    r"(?:\d{1,2}[/-]\d{1,2}[/-](?:\d{2}|\d{4})|"
    r"\d{4}[/-]\d{1,2}[/-]\d{1,2}|"
    r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
    r"jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|"
    r"dec(?:ember)?)\s+\d{1,2},?\s+\d{4})\b"
)
_STREET_ADDRESS = re.compile(
    r"(?i)(?<!\w)\d{1,6}\s+(?:[A-Z0-9][A-Z0-9.'-]*\s+){0,5}"
    r"(?:street|st\.?|avenue|ave\.?|road|rd\.?|boulevard|blvd\.?|"
    r"lane|ln\.?|drive|dr\.?|court|ct\.?|circle|cir\.?|way|parkway|pkwy\.?)\b"
)
_WINDOWS_PRIVATE_PATH = re.compile(
    r"(?ix)(?:"
    r'"(?:[A-Z]:[\\/]|\\\\[^\\/\s\"]+[\\/])[^\"\r\n]*"|'
    r"'(?:[A-Z]:[\\/]|\\\\[^\\/\s']+[\\/])[^'\r\n]*'|"
    r"(?:(?<![\w])[A-Z]:[\\/]|(?<![\\])\\\\[^\\/\s\"'<>|?*]+[\\/])"
    r"[^\s\"'<>|?*\r\n]+"
    r")"
)
_UNIX_PRIVATE_PATH = re.compile(
    r"(?x)(?:"
    r'"/(?!/)[^\"\r\n]+"|'
    r"'/(?!/)[^'\r\n]+'|"
    r"(?<![:/\w])/(?!/)[^\s\"'<>|?*\r\n]+"
    r")"
)
_UNQUOTED_PRIVATE_PATH_TO_EOL = re.compile(
    r"(?im)(?:"
    r"(?<![\w\"'])[A-Z]:[\\/]|"
    r"(?<![\\\"'])\\\\[^\\/\s\"'<>|?*]+[\\/]|"
    r"(?<![:/\w\"'])/(?!/)"
    r")[^\r\n]*"
)
_IP_LITERAL = re.compile(
    r"(?<![\w:])(?:"
    r"\[(?:[0-9A-Fa-f:.]+)(?:%[A-Za-z0-9_.-]+)?\]|"
    r"[0-9A-Fa-f]*:[0-9A-Fa-f:.]*(?:%[A-Za-z0-9_.-]+)?|"
    r"(?:\d{1,3}\.){3}\d{1,3}"
    r")(?![\w:])"
)
_PROMPT_INJECTION = re.compile(
    r"(?is)(?:"
    r"\bignore\s+(?:all\s+|any\s+|the\s+)?(?:previous|prior|above|system|developer)\s+instructions?\b|"
    r"\b(?:reveal|print|dump|exfiltrate|leak)\s+(?:the\s+)?(?:system prompt|private memory|credentials?|secrets?)\b|"
    r"<\s*/?\s*(?:system|developer|tool|assistant)\b|"
    r"\[\s*(?:INST|/?SYS)\s*\]|"
    r"\b(?:system|developer)[_ -]?prompt\s*[:=]|"
    r"\btool[_ -]?(?:call|result)\s*[:=]|"
    r"\b(?:run|execute)\s+(?:powershell|cmd(?:\.exe)?|bash|shell)\b"
    r")"
)
_SENSITIVE_QUERY_KEY = re.compile(
    r"(?i)(?:token|secret|password|passwd|auth|signature|sig|api[_-]?key|access[_-]?key)"
)

_SOURCE_KINDS = frozenset(
    {
        "operator_approval",
        "approved_artifact",
        "verified_project_result",
        "verified_public_source",
    }
)
_AVAILABILITY_STATES = frozenset(
    {"available", "researching", "building", "studio", "offline"}
)


class PublicBridgeError(ValueError):
    """A proposed bridge record was not safe or did not match its closed schema."""


class PrivateDataRejected(PublicBridgeError):
    """The bridge detected private, credential, PII, or instruction content."""


def _contains_hidden_or_control(value: str) -> bool:
    """Cover every Unicode control/format character, not a fragile codepoint list."""
    return any(
        character not in {"\t", "\n", "\r"}
        and unicodedata.category(character) in {"Cc", "Cf"}
        for character in value
    )


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _strict_keys(value: Mapping[str, Any], allowed: set[str], label: str) -> None:
    if type(value) is not dict:
        raise PublicBridgeError(f"{label} must be a plain object")
    extras = set(value) - allowed
    missing = allowed - set(value)
    if extras or missing:
        raise PublicBridgeError(
            f"{label} fields do not match the closed schema; "
            f"missing={sorted(missing)}, extra={sorted(extras)}"
        )


def _timestamp(value: Any, label: str) -> float:
    if isinstance(value, bool):
        raise PublicBridgeError(f"{label} must be a Unix timestamp")
    try:
        normalized = float(value)
    except (TypeError, ValueError) as exc:
        raise PublicBridgeError(f"{label} must be a Unix timestamp") from exc
    if not math.isfinite(normalized) or normalized < 0:
        raise PublicBridgeError(f"{label} must be a finite non-negative timestamp")
    return normalized


def _identifier(value: Any, label: str) -> str:
    if type(value) is not str:
        raise PublicBridgeError(f"{label} must be a string")
    normalized = value.strip()
    if _IDENTIFIER.fullmatch(normalized) is None:
        raise PublicBridgeError(f"{label} is not a bounded public identifier")
    return normalized


def _digest(value: Any, label: str) -> str:
    if type(value) is not str:
        raise PublicBridgeError(f"{label} must be a lowercase SHA-256 digest")
    normalized = value.strip().casefold()
    if _SHA256.fullmatch(normalized) is None:
        raise PublicBridgeError(f"{label} must be a lowercase SHA-256 digest")
    return normalized


def _private_ip_in_text(value: str) -> bool:
    for match in _IP_LITERAL.finditer(value):
        candidate = match.group(0).strip("[]")
        candidate = candidate.split("%", 1)[0]
        try:
            address = ipaddress.ip_address(candidate)
        except ValueError:
            continue
        if (
            address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_reserved
        ):
            return True
    return False


def sanitize_public_text(
    value: Any,
    label: str,
    maximum: int,
    *,
    allow_empty: bool = False,
    allow_unix_url_path: bool = False,
) -> str:
    """Normalize benign formatting and reject content unsafe for the bridge.

    This deliberately rejects instead of silently replacing detected sensitive
    data: silent replacement could change the meaning of an approved public
    statement while making it appear approved.
    """

    if type(value) is not str:
        raise PublicBridgeError(f"{label} must be a string")
    if _contains_hidden_or_control(value):
        raise PrivateDataRejected(f"{label} contains control or hidden-direction characters")
    normalized = unicodedata.normalize("NFKC", value)
    if _contains_hidden_or_control(normalized):
        raise PrivateDataRejected(f"{label} contains control or hidden-direction characters")
    normalized = normalized.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized and not allow_empty:
        raise PublicBridgeError(f"{label} must not be empty")
    if len(normalized) > maximum:
        raise PublicBridgeError(f"{label} exceeds {maximum} characters")
    if _SECRET.search(normalized):
        raise PrivateDataRejected(f"{label} contains a credential or secret")
    if (
        _EMAIL.search(normalized)
        or _OBFUSCATED_EMAIL.search(normalized)
        or _PHONE.search(normalized)
        or _PHONE_CONTEXTUAL.search(normalized)
        or _SSN.search(normalized)
        or _DATE_OF_BIRTH.search(normalized)
        or _STREET_ADDRESS.search(normalized)
    ):
        raise PrivateDataRejected(f"{label} contains personally identifying data")
    if (
        _WINDOWS_PRIVATE_PATH.search(normalized)
        or (not allow_unix_url_path and _UNIX_PRIVATE_PATH.search(normalized))
        or _private_ip_in_text(normalized)
    ):
        raise PrivateDataRejected(f"{label} contains private machine information")
    if _PROMPT_INJECTION.search(normalized):
        raise PrivateDataRejected(f"{label} contains instruction-like hostile content")
    return normalized


def sanitize_untrusted_public_text(value: str) -> tuple[str, tuple[str, ...]]:
    """Redact private fields and classify risks in hostile public input.

    Unlike the outbound bridge scanner, inbound social content is retained as
    untrusted evidence. Sensitive substrings are removed before any model or UI
    can receive them, and the caller gets durable risk labels for quarantine.
    """

    if type(value) is not str:
        raise PublicBridgeError("untrusted public text must be a string")
    if _contains_hidden_or_control(value):
        raise PrivateDataRejected(
            "untrusted public text contains control or hidden-direction characters"
        )
    normalized = unicodedata.normalize("NFKC", value)
    if _contains_hidden_or_control(normalized):
        raise PrivateDataRejected(
            "untrusted public text contains control or hidden-direction characters"
        )
    normalized = normalized.replace("\r\n", "\n").replace("\r", "\n").strip()
    labels: list[str] = []
    if _SECRET.search(normalized):
        labels.append("credential")
    if any(pattern.search(normalized) for pattern in (
        _EMAIL, _OBFUSCATED_EMAIL, _PHONE, _PHONE_CONTEXTUAL,
        _SSN, _DATE_OF_BIRTH, _STREET_ADDRESS
    )):
        labels.append("pii")
    if (
        _WINDOWS_PRIVATE_PATH.search(normalized)
        or _UNIX_PRIVATE_PATH.search(normalized)
        or _private_ip_in_text(normalized)
    ):
        labels.append("private_machine")
    if _PROMPT_INJECTION.search(normalized):
        labels.append("prompt_injection")

    cleaned = _SECRET.sub("[REDACTED CREDENTIAL]", normalized)
    for pattern in (
        _EMAIL, _OBFUSCATED_EMAIL, _PHONE, _PHONE_CONTEXTUAL,
        _SSN, _DATE_OF_BIRTH, _STREET_ADDRESS,
    ):
        cleaned = pattern.sub("[REDACTED PRIVATE DATA]", cleaned)
    # An unquoted local path can legally contain spaces, so token boundaries
    # are unknowable. Consume the rest of that line rather than retain a
    # project/file tail. Quoted paths are excluded here and bounded precisely
    # by the path patterns below.
    cleaned = _UNQUOTED_PRIVATE_PATH_TO_EOL.sub(
        "[REDACTED PRIVATE DATA]", cleaned
    )
    cleaned = _WINDOWS_PRIVATE_PATH.sub("[REDACTED PRIVATE DATA]", cleaned)
    cleaned = _UNIX_PRIVATE_PATH.sub("[REDACTED PRIVATE DATA]", cleaned)

    def redact_private_ip(match: re.Match[str]) -> str:
        candidate = match.group(0)
        return "[REDACTED PRIVATE DATA]" if _private_ip_in_text(candidate) else candidate

    cleaned = _IP_LITERAL.sub(redact_private_ip, cleaned)
    return cleaned, tuple(labels)


def validate_public_url(value: Any, label: str = "url") -> str:
    text = sanitize_public_text(value, label, 2_048)
    if "\\" in text or any(character.isspace() for character in text):
        raise PublicBridgeError(f"{label} contains invalid URL characters")
    parsed = urlsplit(text)
    if parsed.scheme.casefold() != "https" or not parsed.hostname:
        raise PublicBridgeError(f"{label} must be an absolute HTTPS URL")
    if parsed.username is not None or parsed.password is not None:
        raise PrivateDataRejected(f"{label} may not include user information")

    for component_name, component in (
        ("path", parsed.path),
        ("query", parsed.query),
        ("fragment", parsed.fragment),
    ):
        if re.search(r"%(?![0-9A-Fa-f]{2})", component):
            raise PublicBridgeError(
                f"{label} contains malformed percent encoding"
            )
        try:
            decoded = unquote_to_bytes(component).decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise PublicBridgeError(
                f"{label} contains invalid encoded text"
            ) from exc
        if re.search(r"%[0-9A-Fa-f]{2}", decoded):
            raise PublicBridgeError(
                f"{label} contains ambiguous nested percent encoding"
            )
        sanitize_public_text(
            decoded,
            f"{label} {component_name}",
            2_048,
            allow_empty=True,
            allow_unix_url_path=component_name == "path",
        )
    hostname = parsed.hostname.casefold().rstrip(".")
    if hostname in {"localhost", "localhost.localdomain"} or hostname.endswith(".local"):
        raise PrivateDataRejected(f"{label} may not reference a private host")
    try:
        address = ipaddress.ip_address(hostname.strip("[]"))
    except ValueError:
        address = None
    numeric_host = re.fullmatch(
        r"(?:0x[0-9a-f]+|\d+)(?:\.(?:0x[0-9a-f]+|\d+))*",
        hostname,
        re.I,
    )
    if numeric_host is not None and address is None:
        raise PrivateDataRejected(
            f"{label} may not use a non-canonical numeric host"
        )
    try:
        validated_port = parsed.port
    except ValueError as exc:
        raise PublicBridgeError(f"{label} contains an invalid port") from exc
    del validated_port
    if address is None and re.fullmatch(r"[a-z0-9.-]{1,253}", hostname) is None:
        raise PublicBridgeError(f"{label} host must be an ASCII domain or IP address")
    if address is not None and (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
    ):
        raise PrivateDataRejected(f"{label} may not reference a private address")
    for key, _item in parse_qsl(parsed.query, keep_blank_values=True):
        if _SENSITIVE_QUERY_KEY.search(key):
            raise PrivateDataRejected(f"{label} contains a credential-like query parameter")
    return urlunsplit(("https", parsed.netloc, parsed.path or "/", parsed.query, parsed.fragment))


@dataclass(frozen=True, slots=True)
class PublicProvenance:
    source_kind: str
    source_id: str
    observed_at: float
    content_sha256: str
    source_url: str | None = None

    def __post_init__(self) -> None:
        source_kind = str(self.source_kind).strip().casefold()
        if source_kind not in _SOURCE_KINDS:
            raise PublicBridgeError("unsupported public provenance kind")
        object.__setattr__(self, "source_kind", source_kind)
        object.__setattr__(self, "source_id", _identifier(self.source_id, "source_id"))
        object.__setattr__(self, "observed_at", _timestamp(self.observed_at, "observed_at"))
        object.__setattr__(
            self, "content_sha256", _digest(self.content_sha256, "content_sha256")
        )
        if self.source_url is not None:
            object.__setattr__(self, "source_url", validate_public_url(self.source_url, "source_url"))

    def to_record(self) -> dict[str, Any]:
        return {
            "source_kind": self.source_kind,
            "source_id": self.source_id,
            "observed_at": self.observed_at,
            "content_sha256": self.content_sha256,
            "source_url": self.source_url,
        }

    @classmethod
    def from_record(cls, value: Mapping[str, Any]) -> PublicProvenance:
        _strict_keys(
            value,
            {"source_kind", "source_id", "observed_at", "content_sha256", "source_url"},
            "provenance",
        )
        return cls(**value)


@dataclass(frozen=True, slots=True)
class PublicCitation:
    title: str
    url: str
    observed_at: float
    content_sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "title", sanitize_public_text(self.title, "citation title", 300))
        object.__setattr__(self, "url", validate_public_url(self.url, "citation url"))
        object.__setattr__(
            self, "observed_at", _timestamp(self.observed_at, "citation observed_at")
        )
        object.__setattr__(
            self,
            "content_sha256",
            _digest(self.content_sha256, "citation content_sha256"),
        )

    def to_record(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "url": self.url,
            "observed_at": self.observed_at,
            "content_sha256": self.content_sha256,
        }

    @classmethod
    def from_record(cls, value: Mapping[str, Any]) -> PublicCitation:
        _strict_keys(
            value,
            {"title", "url", "observed_at", "content_sha256"},
            "citation",
        )
        return cls(**value)


@dataclass(frozen=True, slots=True)
class ApprovedProjectSummary:
    project_id: str
    title: str
    summary: str
    public_url: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "project_id", _identifier(self.project_id, "project_id"))
        object.__setattr__(self, "title", sanitize_public_text(self.title, "title", 300))
        object.__setattr__(self, "summary", sanitize_public_text(self.summary, "summary", 4_000))
        if self.public_url is not None:
            object.__setattr__(self, "public_url", validate_public_url(self.public_url, "public_url"))

    def to_payload(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "title": self.title,
            "summary": self.summary,
            "public_url": self.public_url,
        }

    @classmethod
    def from_payload(cls, value: Mapping[str, Any]) -> ApprovedProjectSummary:
        _strict_keys(value, {"project_id", "title", "summary", "public_url"}, "project summary")
        return cls(**value)


@dataclass(frozen=True, slots=True)
class ApprovedPublicArtifactLink:
    artifact_id: str
    title: str
    url: str
    description: str
    sha256: str
    media_type: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "artifact_id", _identifier(self.artifact_id, "artifact_id"))
        object.__setattr__(self, "title", sanitize_public_text(self.title, "title", 300))
        object.__setattr__(self, "url", validate_public_url(self.url, "artifact url"))
        object.__setattr__(
            self, "description", sanitize_public_text(self.description, "description", 2_000)
        )
        object.__setattr__(self, "sha256", _digest(self.sha256, "artifact sha256"))
        media_type = str(self.media_type).strip().casefold()
        if re.fullmatch(r"[a-z0-9.+-]{1,64}/[a-z0-9.+-]{1,64}", media_type) is None:
            raise PublicBridgeError("media_type must be a bounded MIME type")
        object.__setattr__(self, "media_type", media_type)

    def to_payload(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "title": self.title,
            "url": self.url,
            "description": self.description,
            "sha256": self.sha256,
            "media_type": self.media_type,
        }

    @classmethod
    def from_payload(cls, value: Mapping[str, Any]) -> ApprovedPublicArtifactLink:
        _strict_keys(
            value,
            {"artifact_id", "title", "url", "description", "sha256", "media_type"},
            "artifact link",
        )
        return cls(**value)


@dataclass(frozen=True, slots=True)
class ApprovedFactAnnouncement:
    fact_id: str
    headline: str
    body: str
    source_urls: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "fact_id", _identifier(self.fact_id, "fact_id"))
        object.__setattr__(self, "headline", sanitize_public_text(self.headline, "headline", 300))
        object.__setattr__(self, "body", sanitize_public_text(self.body, "body", 4_000))
        urls = tuple(validate_public_url(item, "source_url") for item in self.source_urls)
        if not 1 <= len(urls) <= 12 or len(set(urls)) != len(urls):
            raise PublicBridgeError("source_urls must contain 1-12 unique public URLs")
        object.__setattr__(self, "source_urls", urls)

    def to_payload(self) -> dict[str, Any]:
        return {
            "fact_id": self.fact_id,
            "headline": self.headline,
            "body": self.body,
            "source_urls": list(self.source_urls),
        }

    @classmethod
    def from_payload(cls, value: Mapping[str, Any]) -> ApprovedFactAnnouncement:
        _strict_keys(value, {"fact_id", "headline", "body", "source_urls"}, "announcement")
        urls = value["source_urls"]
        if type(urls) is not list:
            raise PublicBridgeError("source_urls must be a list")
        return cls(
            fact_id=value["fact_id"],
            headline=value["headline"],
            body=value["body"],
            source_urls=tuple(urls),
        )


@dataclass(frozen=True, slots=True)
class SanitizedResearchBrief:
    brief_id: str
    title: str
    abstract: str
    findings: tuple[str, ...]
    citations: tuple[PublicCitation, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "brief_id", _identifier(self.brief_id, "brief_id"))
        object.__setattr__(self, "title", sanitize_public_text(self.title, "title", 300))
        object.__setattr__(self, "abstract", sanitize_public_text(self.abstract, "abstract", 4_000))
        findings = tuple(
            sanitize_public_text(item, "finding", 2_000) for item in self.findings
        )
        if not 1 <= len(findings) <= 20:
            raise PublicBridgeError("findings must contain 1-20 bounded items")
        if len(self.citations) < 1 or len(self.citations) > 30:
            raise PublicBridgeError("citations must contain 1-30 entries")
        citations = tuple(self.citations)
        if not all(type(item) is PublicCitation for item in citations):
            raise PublicBridgeError("citations must be PublicCitation objects")
        if len({item.url for item in citations}) != len(citations):
            raise PublicBridgeError("citation URLs must be unique")
        object.__setattr__(self, "findings", findings)
        object.__setattr__(self, "citations", citations)

    def to_payload(self) -> dict[str, Any]:
        return {
            "brief_id": self.brief_id,
            "title": self.title,
            "abstract": self.abstract,
            "findings": list(self.findings),
            "citations": [item.to_record() for item in self.citations],
        }

    @classmethod
    def from_payload(cls, value: Mapping[str, Any]) -> SanitizedResearchBrief:
        _strict_keys(
            value,
            {"brief_id", "title", "abstract", "findings", "citations"},
            "research brief",
        )
        if type(value["findings"]) is not list or type(value["citations"]) is not list:
            raise PublicBridgeError("findings and citations must be lists")
        return cls(
            brief_id=value["brief_id"],
            title=value["title"],
            abstract=value["abstract"],
            findings=tuple(value["findings"]),
            citations=tuple(PublicCitation.from_record(item) for item in value["citations"]),
        )


@dataclass(frozen=True, slots=True)
class PublicAvailability:
    state: str
    message: str
    available_until: float | None = None

    def __post_init__(self) -> None:
        state = str(self.state).strip().casefold()
        if state not in _AVAILABILITY_STATES:
            raise PublicBridgeError("unsupported public availability state")
        object.__setattr__(self, "state", state)
        object.__setattr__(
            self,
            "message",
            sanitize_public_text(self.message, "availability message", 500, allow_empty=True),
        )
        if self.available_until is not None:
            object.__setattr__(
                self,
                "available_until",
                _timestamp(self.available_until, "available_until"),
            )

    def to_payload(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "message": self.message,
            "available_until": self.available_until,
        }

    @classmethod
    def from_payload(cls, value: Mapping[str, Any]) -> PublicAvailability:
        _strict_keys(value, {"state", "message", "available_until"}, "availability")
        return cls(**value)


PublicBridgePayload = Union[
    ApprovedProjectSummary,
    ApprovedPublicArtifactLink,
    ApprovedFactAnnouncement,
    SanitizedResearchBrief,
    PublicAvailability,
]

_PAYLOAD_KIND: dict[type[Any], str] = {
    ApprovedProjectSummary: "approved_project_summary",
    ApprovedPublicArtifactLink: "approved_public_artifact_link",
    ApprovedFactAnnouncement: "approved_fact_announcement",
    SanitizedResearchBrief: "sanitized_research_brief",
    PublicAvailability: "public_availability",
}
_KIND_PARSER = {
    "approved_project_summary": ApprovedProjectSummary.from_payload,
    "approved_public_artifact_link": ApprovedPublicArtifactLink.from_payload,
    "approved_fact_announcement": ApprovedFactAnnouncement.from_payload,
    "sanitized_research_brief": SanitizedResearchBrief.from_payload,
    "public_availability": PublicAvailability.from_payload,
}


def public_bridge_payload_digest(payload: PublicBridgePayload) -> str:
    """Digest the exact typed payload an operator approval must authorize."""

    if type(payload) not in _PAYLOAD_KIND:
        raise PublicBridgeError("unsupported private-to-public bridge payload")
    return _sha256_text(_canonical_json({
        "kind": _PAYLOAD_KIND[type(payload)],
        "payload": payload.to_payload(),
    }))


@dataclass(frozen=True, slots=True)
class PublicBridgeObject:
    bridge_id: str
    payload: PublicBridgePayload
    provenance: tuple[PublicProvenance, ...]
    confidence: float
    created_at: float
    expires_at: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "bridge_id", _identifier(self.bridge_id, "bridge_id"))
        if type(self.payload) not in _PAYLOAD_KIND:
            raise PublicBridgeError("unsupported private-to-public bridge payload")
        provenance = tuple(self.provenance)
        if not 1 <= len(provenance) <= 20 or not all(
            type(item) is PublicProvenance for item in provenance
        ):
            raise PublicBridgeError("provenance must contain 1-20 typed records")
        operator_approvals = tuple(
            item for item in provenance if item.source_kind == "operator_approval"
        )
        if len(operator_approvals) != 1:
            raise PublicBridgeError(
                "public bridge objects require exactly one operator approval provenance"
            )
        expected_payload_digest = public_bridge_payload_digest(self.payload)
        if not hmac.compare_digest(
            operator_approvals[0].content_sha256, expected_payload_digest
        ):
            raise PublicBridgeError(
                "operator approval provenance is not bound to the exact public payload"
            )
        object.__setattr__(self, "provenance", provenance)
        if isinstance(self.confidence, bool):
            raise PublicBridgeError("confidence must be a number between 0 and 1")
        confidence = float(self.confidence)
        if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
            raise PublicBridgeError("confidence must be between 0 and 1")
        object.__setattr__(self, "confidence", confidence)
        created = _timestamp(self.created_at, "created_at")
        expires = _timestamp(self.expires_at, "expires_at")
        if expires <= created or expires - created > MAX_BRIDGE_LIFETIME_SECONDS:
            raise PublicBridgeError("bridge expiry must be after creation and within one year")
        object.__setattr__(self, "created_at", created)
        object.__setattr__(self, "expires_at", expires)

    @property
    def kind(self) -> str:
        return _PAYLOAD_KIND[type(self.payload)]

    @property
    def payload_digest(self) -> str:
        return public_bridge_payload_digest(self.payload)

    def assert_current(self, *, now: float | None = None) -> None:
        moment = time.time() if now is None else _timestamp(now, "now")
        if self.expires_at <= moment:
            raise PublicBridgeError("public bridge object has expired")
        if self.created_at > moment + 300:
            raise PublicBridgeError("public bridge object creation time is in the future")
        for record in self.provenance:
            if record.observed_at > moment + 300:
                raise PublicBridgeError("public provenance observation time is in the future")

    def _unsigned_record(self) -> dict[str, Any]:
        return {
            "schema_version": PUBLIC_BRIDGE_SCHEMA_VERSION,
            "bridge_id": self.bridge_id,
            "kind": self.kind,
            "payload": self.payload.to_payload(),
            "provenance": [item.to_record() for item in self.provenance],
            "visibility": "public",
            "confidence": self.confidence,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
        }

    @property
    def digest(self) -> str:
        return _sha256_text(_canonical_json(self._unsigned_record()))

    def to_record(self) -> dict[str, Any]:
        record = self._unsigned_record()
        record["digest"] = self.digest
        return record

    @classmethod
    def from_record(
        cls,
        value: Mapping[str, Any],
        *,
        now: float | None = None,
    ) -> PublicBridgeObject:
        _strict_keys(
            value,
            {
                "schema_version",
                "bridge_id",
                "kind",
                "payload",
                "provenance",
                "visibility",
                "confidence",
                "created_at",
                "expires_at",
                "digest",
            },
            "bridge object",
        )
        if value["schema_version"] != PUBLIC_BRIDGE_SCHEMA_VERSION:
            raise PublicBridgeError("unsupported public bridge schema version")
        if value["visibility"] != "public":
            raise PublicBridgeError("public bridge visibility must be exactly 'public'")
        kind = value["kind"]
        if kind not in _KIND_PARSER:
            raise PublicBridgeError("unsupported private-to-public bridge kind")
        if type(value["payload"]) is not dict or type(value["provenance"]) is not list:
            raise PublicBridgeError("payload and provenance must use their closed object schemas")
        instance = cls(
            bridge_id=value["bridge_id"],
            payload=_KIND_PARSER[kind](value["payload"]),
            provenance=tuple(
                PublicProvenance.from_record(item) for item in value["provenance"]
            ),
            confidence=value["confidence"],
            created_at=value["created_at"],
            expires_at=value["expires_at"],
        )
        supplied_digest = _digest(value["digest"], "bridge digest")
        if not hmac.compare_digest(instance.digest, supplied_digest):
            raise PublicBridgeError("public bridge object digest does not match its contents")
        instance.assert_current(now=now)
        return instance


def bridge_object_from_json(value: str, *, now: float | None = None) -> PublicBridgeObject:
    if type(value) is not str or len(value) > 100_000:
        raise PublicBridgeError("bridge JSON must be a bounded string")
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise PublicBridgeError("bridge JSON is invalid") from exc
    if type(parsed) is not dict:
        raise PublicBridgeError("bridge JSON must contain one object")
    return PublicBridgeObject.from_record(parsed, now=now)


def bridge_object_to_json(value: PublicBridgeObject) -> str:
    if type(value) is not PublicBridgeObject:
        raise PublicBridgeError("only a PublicBridgeObject may cross the bridge")
    value.assert_current()
    return _canonical_json(value.to_record())
