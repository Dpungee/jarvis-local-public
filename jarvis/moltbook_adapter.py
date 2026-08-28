from __future__ import annotations

import hashlib
import html
import re
import unicodedata
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable

from .public_bridge import sanitize_untrusted_public_text, validate_public_url
from .redaction import contains_secret, redact_secrets


_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
_CONTROL_OR_HIDDEN = re.compile(
    r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\u200b-\u200f\u202a-\u202e\u2060\u2066-\u2069]"
)
_MAX_FIXTURE_ITEMS = 500


def _bounded_identifier(value: Any, *, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    cleaned = value.strip()
    if _IDENTIFIER.fullmatch(cleaned) is None:
        raise ValueError(f"{field} is not a bounded public identifier")
    return cleaned


def _safe_public_url(value: Any, *, field: str = "source_url") -> str:
    return validate_public_url(value, field)


@dataclass(frozen=True)
class SanitizedPublicText:
    """Bounded external text which permanently retains its untrusted label."""

    text: str
    authority: str = "external_untrusted"
    truncated: bool = False
    secret_redacted: bool = False
    pii_redacted: bool = False
    quarantined: bool = False
    risk_labels: tuple[str, ...] = ()


def sanitize_public_text(value: Any, *, max_chars: int) -> SanitizedPublicText:
    if not isinstance(value, str):
        raise ValueError("public text must be a string")
    if not 1 <= max_chars <= 20_000:
        raise ValueError("public text bound is invalid")
    if _CONTROL_OR_HIDDEN.search(value):
        raise ValueError("public text contains hidden or directional control characters")
    normalized = unicodedata.normalize("NFKC", value)
    had_secret = contains_secret(normalized)
    cleaned = redact_secrets(normalized)
    cleaned, risk_labels = sanitize_untrusted_public_text(cleaned)
    pii_redacted = "pii" in risk_labels or "private_machine" in risk_labels
    # Defense in depth for any downstream UI that accidentally uses an HTML sink.
    cleaned = html.escape(cleaned, quote=False).strip()
    truncated = len(cleaned) > max_chars
    if truncated:
        cleaned = cleaned[:max_chars]
    if not cleaned:
        raise ValueError("public text must not be empty")
    return SanitizedPublicText(
        text=cleaned,
        truncated=truncated,
        secret_redacted=had_secret,
        pii_redacted=pii_redacted,
        quarantined=bool(had_secret or risk_labels),
        risk_labels=tuple(sorted(set(risk_labels) | ({"credential"} if had_secret else set()))),
    )


def _safe_outbound_draft(value: Any, *, max_chars: int) -> str:
    if not isinstance(value, str):
        raise ValueError("draft text must be a string")
    if contains_secret(value):
        raise PermissionError("public drafts may not contain sensitive material")
    cleaned = sanitize_public_text(value, max_chars=max_chars)
    if cleaned.truncated:
        raise ValueError(f"draft text exceeds {max_chars} characters")
    return cleaned.text


@dataclass(frozen=True)
class MoltbookProfile:
    profile_id: str
    display_name: SanitizedPublicText
    bio: SanitizedPublicText
    source_url: str


@dataclass(frozen=True)
class MoltbookPost:
    post_id: str
    thread_id: str
    author_profile_id: str
    body: SanitizedPublicText
    source_url: str
    created_at: str


@dataclass(frozen=True)
class MoltbookDraft:
    draft_id: str
    kind: str
    body: str
    reply_to_thread_id: str | None
    created_at: str
    publishable: bool = False
    approved: bool = False


def public_value(value: Any) -> Any:
    """Convert adapter records into JSON-compatible, provenance-preserving values."""

    if hasattr(value, "__dataclass_fields__"):
        return asdict(value)
    if isinstance(value, tuple):
        return [public_value(item) for item in value]
    return value


@runtime_checkable
class MoltbookAdapter(Protocol):
    """Phase-4 surface only. It intentionally has no external mutation methods."""

    @property
    def offline(self) -> bool: ...

    def status(self) -> Mapping[str, Any]: ...

    def read_feed(self, *, limit: int = 20) -> Sequence[MoltbookPost]: ...

    def read_thread(self, thread_id: str) -> Sequence[MoltbookPost]: ...

    def search(self, query: str, *, limit: int = 20) -> Sequence[MoltbookPost]: ...

    def get_profile(self, profile_id: str) -> MoltbookProfile | None: ...

    def draft_post(self, body: str) -> MoltbookDraft: ...

    def draft_reply(self, thread_id: str, body: str) -> MoltbookDraft: ...


class OfflineMoltbookAdapter:
    """Deterministic fixture adapter; it contains no network or credential code."""

    def __init__(self, fixture: Mapping[str, Any]) -> None:
        if not isinstance(fixture, Mapping):
            raise ValueError("offline Moltbook fixture must be an object")
        unexpected = set(fixture) - {"profiles", "posts"}
        if unexpected:
            raise ValueError(
                "unsupported offline Moltbook fixture fields: "
                + ", ".join(sorted(str(item) for item in unexpected))
            )
        raw_profiles = fixture.get("profiles", ())
        raw_posts = fixture.get("posts", ())
        if (
            not isinstance(raw_profiles, Sequence)
            or isinstance(raw_profiles, (str, bytes, bytearray))
            or not isinstance(raw_posts, Sequence)
            or isinstance(raw_posts, (str, bytes, bytearray))
        ):
            raise ValueError("offline Moltbook profiles and posts must be arrays")
        if len(raw_profiles) > _MAX_FIXTURE_ITEMS or len(raw_posts) > _MAX_FIXTURE_ITEMS:
            raise ValueError("offline Moltbook fixture is too large")

        profiles: dict[str, MoltbookProfile] = {}
        for raw in raw_profiles:
            if not isinstance(raw, Mapping) or set(raw) != {
                "profile_id", "display_name", "bio", "source_url"
            }:
                raise ValueError("offline Moltbook profile schema is invalid")
            profile_id = _bounded_identifier(raw["profile_id"], field="profile_id")
            if profile_id in profiles:
                raise ValueError("offline Moltbook profile IDs must be unique")
            profiles[profile_id] = MoltbookProfile(
                profile_id=profile_id,
                display_name=sanitize_public_text(raw["display_name"], max_chars=160),
                bio=sanitize_public_text(raw["bio"], max_chars=2_000),
                source_url=_safe_public_url(raw["source_url"]),
            )

        posts: list[MoltbookPost] = []
        post_ids: set[str] = set()
        for raw in raw_posts:
            if not isinstance(raw, Mapping) or set(raw) != {
                "post_id",
                "thread_id",
                "author_profile_id",
                "body",
                "source_url",
                "created_at",
            }:
                raise ValueError("offline Moltbook post schema is invalid")
            post_id = _bounded_identifier(raw["post_id"], field="post_id")
            thread_id = _bounded_identifier(raw["thread_id"], field="thread_id")
            author_id = _bounded_identifier(
                raw["author_profile_id"], field="author_profile_id"
            )
            if post_id in post_ids:
                raise ValueError("offline Moltbook post IDs must be unique")
            if author_id not in profiles:
                raise ValueError("offline Moltbook post references an unknown profile")
            if not isinstance(raw["created_at"], str) or len(raw["created_at"]) > 64:
                raise ValueError("created_at must be a bounded timestamp")
            posts.append(MoltbookPost(
                post_id=post_id,
                thread_id=thread_id,
                author_profile_id=author_id,
                body=sanitize_public_text(raw["body"], max_chars=8_000),
                source_url=_safe_public_url(raw["source_url"]),
                created_at=raw["created_at"],
            ))
            post_ids.add(post_id)

        self._profiles = MappingProxyType(profiles)
        self._posts = tuple(posts)

    @property
    def offline(self) -> bool:
        return True

    def status(self) -> Mapping[str, Any]:
        return MappingProxyType({
            "adapter": "moltbook",
            "mode": "offline_fixture",
            "connected": False,
            "credentials_loaded": False,
            "external_communication": False,
            "records": len(self._posts),
        })

    @staticmethod
    def _limit(value: int) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 50:
            raise ValueError("limit must be an integer from 1 to 50")
        return value

    def read_feed(self, *, limit: int = 20) -> Sequence[MoltbookPost]:
        return self._posts[:self._limit(limit)]

    def read_thread(self, thread_id: str) -> Sequence[MoltbookPost]:
        selected = _bounded_identifier(thread_id, field="thread_id")
        return tuple(post for post in self._posts if post.thread_id == selected)

    def search(self, query: str, *, limit: int = 20) -> Sequence[MoltbookPost]:
        if contains_secret(str(query)):
            raise PermissionError("public search queries may not contain sensitive material")
        needle = sanitize_public_text(query, max_chars=500).text.casefold()
        matches = tuple(post for post in self._posts if needle in post.body.text.casefold())
        return matches[:self._limit(limit)]

    def get_profile(self, profile_id: str) -> MoltbookProfile | None:
        selected = _bounded_identifier(profile_id, field="profile_id")
        return self._profiles.get(selected)

    @staticmethod
    def _draft_id(kind: str, body: str, reply_to: str | None = None) -> str:
        seed = f"{kind}\0{reply_to or ''}\0{body}".encode("utf-8")
        return "offline-" + hashlib.sha256(seed).hexdigest()[:24]

    def draft_post(self, body: str) -> MoltbookDraft:
        cleaned = _safe_outbound_draft(body, max_chars=4_000)
        return MoltbookDraft(
            draft_id=self._draft_id("post", cleaned),
            kind="post",
            body=cleaned,
            reply_to_thread_id=None,
            created_at=datetime.now(timezone.utc).isoformat(),
        )

    def draft_reply(self, thread_id: str, body: str) -> MoltbookDraft:
        selected = _bounded_identifier(thread_id, field="thread_id")
        if not any(post.thread_id == selected for post in self._posts):
            raise ValueError("reply target is not present in the offline fixture")
        cleaned = _safe_outbound_draft(body, max_chars=4_000)
        return MoltbookDraft(
            draft_id=self._draft_id("reply", cleaned, selected),
            kind="reply",
            body=cleaned,
            reply_to_thread_id=selected,
            created_at=datetime.now(timezone.utc).isoformat(),
        )
