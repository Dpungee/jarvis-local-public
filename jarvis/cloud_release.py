"""Trusted-host approval of exact cloud context; never expose this API to agents.

Screens are defense in depth, not a classifier proving that arbitrary text is
public. The operator must review the complete candidate before approval.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import secrets
import threading
import time
from dataclasses import dataclass, field

from .redaction import contains_private_identifier, contains_secret

_ACTOR = re.compile(r"agt_[0-9a-f]{32}\Z")
_MODEL = re.compile(r"(?:codex-cli|claude-cli):[A-Za-z0-9][A-Za-z0-9._-]{0,99}\Z")
_SENSITIVE = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----|"
    r"\b(?:seed phrase|recovery phrase|mnemonic|private key)\b|"
    r"\b(?:\d{1,3}\.){3}\d{1,3}\b|"
    r"\b(?:[0-9a-f]{2}:){5}[0-9a-f]{2}\b",
    re.IGNORECASE,
)


class CloudReleaseDenied(ValueError):
    """Payload-free denial, safe to report without echoing candidate content."""


def _snapshot(messages: list[dict[str, str]]) -> str:
    if type(messages) is not list or not 1 <= len(messages) <= 64:
        raise CloudReleaseDenied("invalid_context")
    for message in messages:
        if type(message) is not dict or set(message) != {"role", "content"}:
            raise CloudReleaseDenied("invalid_context")
        if type(message["role"]) is not str or message["role"] not in {
            "system",
            "user",
            "assistant",
        }:
            raise CloudReleaseDenied("invalid_context")
        value = message["content"]
        if type(value) is not str or len(value) > 32768:
            raise CloudReleaseDenied("invalid_context")
        if any(ord(char) < 32 and char not in "\n\r\t" for char in value):
            raise CloudReleaseDenied("invalid_context")
    try:
        encoded = json.dumps(
            messages, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        raw = encoded.encode("utf-8")
    except (ValueError, UnicodeError):
        raise CloudReleaseDenied("invalid_context") from None
    if len(raw) > 131072:
        raise CloudReleaseDenied("context_too_large")
    # Check a frozen serialization, not caller-owned mutable dictionaries.
    for message in json.loads(encoded):
        value = message["content"]
        if (
            contains_secret(value)
            or contains_private_identifier(value)
            or _SENSITIVE.search(value)
        ):
            raise CloudReleaseDenied("sensitive_context")
    return encoded


def context_digest(messages: list[dict[str, str]]) -> str:
    """Digest for trusted review UI; a digest is not itself approval."""
    return hashlib.sha256(_snapshot(messages).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class _Release:
    actor: str
    model: str
    snapshot: str = field(repr=False)
    expires: float


class CloudReleaseGate:
    """In-process, bounded, one-use approvals. Restart invalidates all tickets.

    Host code is trusted. This is not an OS sandbox and cannot protect against
    arbitrary Python executing in the host process. Never serialize tickets into
    prompts or make approval, revocation or raw ModelClient methods agent tools.
    """

    def __init__(self) -> None:
        self._releases: dict[str, _Release] = {}
        self._lock = threading.Lock()

    def approve(
        self,
        *,
        actor: str,
        model: str,
        messages: list[dict[str, str]],
        reviewed_sha256: str,
        ttl_seconds: float = 60,
    ) -> str:
        """Trusted operator path only; approval binds exact reviewed bytes."""
        if type(actor) is not str or not _ACTOR.fullmatch(actor):
            raise CloudReleaseDenied("invalid_actor")
        if type(model) is not str or not _MODEL.fullmatch(model):
            raise CloudReleaseDenied("invalid_model")
        if (
            type(ttl_seconds) not in {int, float}
            or not math.isfinite(ttl_seconds)
            or not 0 < ttl_seconds <= 300
        ):
            raise CloudReleaseDenied("invalid_expiry")
        snapshot = _snapshot(messages)
        digest = hashlib.sha256(snapshot.encode("utf-8")).hexdigest()
        if type(reviewed_sha256) is not str or reviewed_sha256 != digest:
            raise CloudReleaseDenied("review_mismatch")
        with self._lock:
            now = time.monotonic()
            self._releases = {
                key: item for key, item in self._releases.items() if item.expires > now
            }
            if len(self._releases) >= 256:
                raise CloudReleaseDenied("release_capacity")
            ticket = secrets.token_hex(32)
            self._releases[ticket] = _Release(actor, model, snapshot, now + ttl_seconds)
            return ticket

    def revoke(self, ticket: str) -> None:
        with self._lock:
            self._releases.pop(ticket, None)

    def consume(self, *, ticket: str, actor: str, model: str) -> list[dict[str, str]]:
        if type(ticket) is not str:
            raise CloudReleaseDenied("invalid_release")
        with self._lock:
            # Burn before dispatch, even on failure. No ambiguous automatic retry.
            release = self._releases.pop(ticket, None)
            if (
                release is None
                or release.expires <= time.monotonic()
                or release.actor != actor
                or release.model != model
            ):
                raise CloudReleaseDenied("invalid_release")
        return json.loads(release.snapshot)
