from __future__ import annotations

import re
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
SENSITIVE_KEY = re.compile(rf"^(?:{_SENSITIVE_KEY_PATTERN})$", re.I)
SECRET_VALUE = re.compile(
    r"(?i)(-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----|"
    r"\bsk-(?:proj-)?[A-Za-z0-9_-]{12,}|"
    r"\bgh[pousr]_[A-Za-z0-9_-]{12,}|"
    r"\bgithub_pat_[A-Za-z0-9_]{12,}|"
    r"\bxox[baprs]-[A-Za-z0-9-]{12,}|\bAIza[A-Za-z0-9_-]{20,}|"
    r"\bAKIA[0-9A-Z]{16}\b|"
    r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}|"
    rf"(?:[\"'](?:{_SENSITIVE_KEY_PATTERN})[\"']|"
    rf"\b(?:{_SENSITIVE_KEY_PATTERN})\b)\s*[:=]\s*"
    r"(?:\"[^\"]*\"|'[^']*'|\S+)|"
    r"\bbearer\s+[A-Za-z0-9._~-]{8,})",
    re.S,
)


def redact_secrets(value: str, replacement: str = "[REDACTED]") -> str:
    return SECRET_VALUE.sub(replacement, CONTROL_CODES.sub("", str(value)))


def contains_secret(value: str) -> bool:
    return SECRET_VALUE.search(CONTROL_CODES.sub("", str(value))) is not None


def is_sensitive_key(value: str) -> bool:
    return SENSITIVE_KEY.fullmatch(str(value).strip().strip("\"'")) is not None


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
