"""Versioned, safe-to-preview customization profiles for Jarvis.

Profiles hold operator preferences rather than executable code.  They are meant
to make Jarvis deeply configurable without allowing a theme, persona, connector,
or convenience setting to override approvals, redaction, verification, or other
safety-critical runtime authority.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping, Sequence
from copy import deepcopy
from pathlib import Path
from typing import Any
from uuid import uuid4

from .redaction import contains_secret, is_sensitive_key, redact_secrets


SCHEMA_VERSION = 1
MAX_PROFILE_DEPTH = 5
MAX_TEXT_LENGTH = 2_000
MAX_COLLECTION_ITEMS = 128
_PROFILE_ID = re.compile(r"[a-z][a-z0-9-]{0,63}")
_SETTING_KEY = re.compile(r"[a-z][a-z0-9_.-]{0,63}")
_CUSTOMIZABLE_AREAS = frozenset(
    {
        "appearance",
        "automation",
        "companion",
        "connectors",
        "conversation",
        "model_routing",
        "operator_standards",
        "skills",
        "specialists",
    }
)
_IMMUTABLE_AREAS = frozenset(
    {
        "access_control",
        "approval",
        "approvals",
        "constitution",
        "policy",
        "policies",
        "redaction",
        "self_repair",
        "tool_authority",
        "verification",
    }
)

_RUNTIME_THEMES = frozenset({"dark", "midnight", "holographic-dark", "high-contrast"})
_RUNTIME_TONES = frozenset(
    {"natural", "straightforward", "direct", "professional", "friendly", "dry-witty"}
)
_RUNTIME_DETAIL = frozenset({"concise", "adaptive", "thorough"})
_RUNTIME_FORMATTING = frozenset({"plain", "balanced", "structured"})
_RUNTIME_ROUTING_PRIORITY = frozenset({"speed", "balanced", "quality"})
_ACCENT_COLOR = re.compile(r"#[0-9a-fA-F]{6}")


class CustomizationProfileError(ValueError):
    """Raised when a profile exceeds the declarative customization boundary."""


def _canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _safe_text(value: Any, *, label: str) -> str:
    if not isinstance(value, str):
        raise CustomizationProfileError(f"{label} must be text")
    cleaned = redact_secrets(value, "[REDACTED]")
    if not cleaned or len(cleaned) > MAX_TEXT_LENGTH or contains_secret(cleaned):
        raise CustomizationProfileError(f"{label} is empty, too long, or contains a secret")
    return cleaned


def _validate_setting(value: Any, *, depth: int = 0) -> Any:
    if depth > MAX_PROFILE_DEPTH:
        raise CustomizationProfileError("customization nesting is too deep")
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        if not -(2**31) <= value <= 2**31 - 1:
            raise CustomizationProfileError("customization integer is out of bounds")
        return value
    if isinstance(value, str):
        return _safe_text(value, label="customization text")
    if isinstance(value, Mapping):
        if len(value) > MAX_COLLECTION_ITEMS:
            raise CustomizationProfileError("customization mapping is too large")
        clean: dict[str, Any] = {}
        for raw_key, raw_value in value.items():
            if not isinstance(raw_key, str) or _SETTING_KEY.fullmatch(raw_key) is None:
                raise CustomizationProfileError("customization key is invalid")
            if raw_key in _IMMUTABLE_AREAS or is_sensitive_key(raw_key):
                raise CustomizationProfileError("customization cannot change a protected setting")
            clean[raw_key] = _validate_setting(raw_value, depth=depth + 1)
        return dict(sorted(clean.items()))
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        if len(value) > MAX_COLLECTION_ITEMS:
            raise CustomizationProfileError("customization list is too large")
        return [_validate_setting(item, depth=depth + 1) for item in value]
    raise CustomizationProfileError("customization value type is unsupported")


def validate_customization_profile(value: Mapping[str, Any]) -> dict[str, Any]:
    """Return the canonical form of a declarative, non-secret profile."""

    if not isinstance(value, Mapping):
        raise TypeError("customization profile must be a mapping")
    required = {"schema_version", "profile_id", "display_name", "revision", "settings"}
    if set(value) != required:
        raise CustomizationProfileError("customization profile fields are invalid")
    if value.get("schema_version") != SCHEMA_VERSION:
        raise CustomizationProfileError("customization profile schema is unsupported")
    profile_id = value.get("profile_id")
    if not isinstance(profile_id, str) or _PROFILE_ID.fullmatch(profile_id) is None:
        raise CustomizationProfileError("customization profile id is invalid")
    revision = value.get("revision")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        raise CustomizationProfileError("customization profile revision is invalid")
    settings = value.get("settings")
    if not isinstance(settings, Mapping) or not settings:
        raise CustomizationProfileError("customization profile settings are required")
    if any(not isinstance(key, str) or key not in _CUSTOMIZABLE_AREAS for key in settings):
        raise CustomizationProfileError("customization profile area is unsupported or protected")
    return {
        "schema_version": SCHEMA_VERSION,
        "profile_id": profile_id,
        "display_name": _safe_text(value.get("display_name"), label="display name"),
        "revision": revision,
        "settings": {key: _validate_setting(settings[key]) for key in sorted(settings)},
    }


def customization_profile_receipt(value: Mapping[str, Any]) -> dict[str, Any]:
    """Return a stable preview receipt without applying profile settings."""

    profile = validate_customization_profile(value)
    digest = hashlib.sha256(_canonical_json(profile).encode("utf-8")).hexdigest()
    return {
        "profile_id": profile["profile_id"],
        "revision": profile["revision"],
        "customizable_areas": sorted(profile["settings"]),
        "protected_areas": sorted(_IMMUTABLE_AREAS),
        "profile_checksum_sha256": digest,
        "profile": deepcopy(profile),
    }


def runtime_customization_settings(value: Mapping[str, Any] | None) -> dict[str, Any]:
    """Project a stored profile onto the small set the live runtime understands.

    The profile format is intentionally extensible, while runtime behavior must
    remain closed and predictable. Unknown settings are therefore retained in
    the versioned profile but do not gain authority merely by being present.
    """

    if not isinstance(value, Mapping):
        return {}
    settings = value.get("settings") if "settings" in value else value
    if not isinstance(settings, Mapping):
        return {}
    projected: dict[str, Any] = {}

    appearance = settings.get("appearance")
    if isinstance(appearance, Mapping):
        clean_appearance: dict[str, Any] = {}
        theme = appearance.get("theme")
        if isinstance(theme, str) and theme in _RUNTIME_THEMES:
            clean_appearance["theme"] = theme
        accent = appearance.get("accent")
        if isinstance(accent, str) and _ACCENT_COLOR.fullmatch(accent):
            clean_appearance["accent"] = accent.lower()
        compact = appearance.get("compact")
        if isinstance(compact, bool):
            clean_appearance["compact"] = compact
        if clean_appearance:
            projected["appearance"] = clean_appearance

    conversation = settings.get("conversation")
    if isinstance(conversation, Mapping):
        clean_conversation: dict[str, str] = {}
        for key, allowed in (
            ("tone", _RUNTIME_TONES),
            ("detail", _RUNTIME_DETAIL),
            ("formatting", _RUNTIME_FORMATTING),
        ):
            selected = conversation.get(key)
            if isinstance(selected, str) and selected in allowed:
                clean_conversation[key] = selected
        if clean_conversation:
            projected["conversation"] = clean_conversation

    routing = settings.get("model_routing")
    if isinstance(routing, Mapping):
        priority = routing.get("priority")
        if isinstance(priority, str) and priority in _RUNTIME_ROUTING_PRIORITY:
            projected["model_routing"] = {"priority": priority}
        elif isinstance(routing.get("prefer_speed_for_chat"), bool):
            projected["model_routing"] = {
                "priority": "speed" if routing["prefer_speed_for_chat"] else "balanced"
            }

    standards = settings.get("operator_standards")
    if isinstance(standards, Sequence) and not isinstance(standards, (str, bytes)):
        clean_standards = [
            item.strip()
            for item in standards[:6]
            if isinstance(item, str) and item.strip() and len(item.strip()) <= 180
        ]
        if clean_standards:
            projected["operator_standards"] = clean_standards
    return projected


def render_runtime_customization(value: Mapping[str, Any] | None) -> str:
    """Return bounded, declarative prompt context for operator preferences."""

    projected = runtime_customization_settings(value)
    prompt_projection = {
        key: projected[key]
        for key in ("conversation", "operator_standards")
        if key in projected
    }
    if not prompt_projection:
        return "No active runtime customization profile."
    return json.dumps(
        prompt_projection,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


class CustomizationProfileStore:
    """Append-only profile revisions with an atomic active-profile pointer.

    Profiles never contain executable code or authority settings.  Updating a
    profile requires the previous revision's exact checksum, while activation
    requires the selected revision's checksum.  This makes customization
    versioned, reversible, and safe against stale-editor overwrites.
    """

    def __init__(self, data_dir: Path | str) -> None:
        self.root = Path(data_dir).resolve() / "customization_profiles"

    def _ensure_root(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        if self.root.is_symlink() or not self.root.is_dir():
            raise CustomizationProfileError(
                "customization profile root must be an ordinary directory"
            )

    def _revision_path(self, profile_id: str, revision: int) -> Path:
        if _PROFILE_ID.fullmatch(profile_id) is None:
            raise CustomizationProfileError("customization profile id is invalid")
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
            raise CustomizationProfileError("customization profile revision is invalid")
        return self.root / profile_id / f"revision-{revision:08d}.json"

    @staticmethod
    def _read_profile(path: Path) -> dict[str, Any]:
        if path.is_symlink() or not path.is_file():
            raise CustomizationProfileError("customization profile revision is missing")
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CustomizationProfileError(
                "customization profile revision is unreadable"
            ) from exc
        return validate_customization_profile(value)

    def preview(self, value: Mapping[str, Any]) -> dict[str, Any]:
        return customization_profile_receipt(value)

    def list_profiles(self) -> list[dict[str, Any]]:
        """Return every verified revision without trusting directory metadata."""

        if not self.root.exists():
            return []
        if self.root.is_symlink() or not self.root.is_dir():
            raise CustomizationProfileError(
                "customization profile root must be an ordinary directory"
            )
        active = self.active()
        rows: list[dict[str, Any]] = []
        for directory in sorted(self.root.iterdir(), key=lambda item: item.name):
            if (
                directory.is_symlink()
                or not directory.is_dir()
                or _PROFILE_ID.fullmatch(directory.name) is None
            ):
                continue
            revisions: list[dict[str, Any]] = []
            for path in sorted(directory.glob("revision-*.json")):
                match = re.fullmatch(r"revision-([0-9]{8})\.json", path.name)
                if match is None:
                    continue
                profile = self._read_profile(path)
                receipt = customization_profile_receipt(profile)
                revisions.append({
                    "revision": int(profile["revision"]),
                    "profile_checksum_sha256": receipt["profile_checksum_sha256"],
                    "active": bool(
                        active
                        and active["profile_id"] == profile["profile_id"]
                        and active["revision"] == profile["revision"]
                    ),
                    "profile": deepcopy(profile),
                })
            if revisions:
                latest = max(revisions, key=lambda item: int(item["revision"]))
                rows.append({
                    "profile_id": directory.name,
                    "display_name": latest["profile"]["display_name"],
                    "latest_revision": latest["revision"],
                    "revisions": revisions,
                })
        return rows

    def save(
        self,
        value: Mapping[str, Any],
        *,
        expected_previous_checksum: str | None,
    ) -> dict[str, Any]:
        receipt = customization_profile_receipt(value)
        profile = receipt["profile"]
        profile_id = str(profile["profile_id"])
        revision = int(profile["revision"])
        self._ensure_root()
        directory = self.root / profile_id
        if directory.exists() and (directory.is_symlink() or not directory.is_dir()):
            raise CustomizationProfileError("customization profile path is unsafe")

        if revision == 1:
            if expected_previous_checksum is not None or directory.exists():
                raise CustomizationProfileError(
                    "new customization profiles must begin at revision 1"
                )
        else:
            previous_path = self._revision_path(profile_id, revision - 1)
            previous = customization_profile_receipt(self._read_profile(previous_path))
            if expected_previous_checksum != previous["profile_checksum_sha256"]:
                raise CustomizationProfileError(
                    "previous customization revision checksum does not match"
                )
            if self._revision_path(profile_id, revision).exists():
                raise CustomizationProfileError(
                    "customization profile revision already exists"
                )

        directory.mkdir(exist_ok=True)
        target = self._revision_path(profile_id, revision)
        document = _canonical_json(profile).encode("utf-8")
        try:
            with target.open("xb") as output:
                output.write(document)
                output.flush()
                os.fsync(output.fileno())
        except FileExistsError as exc:
            raise CustomizationProfileError(
                "customization profile revision already exists"
            ) from exc
        reread = customization_profile_receipt(self._read_profile(target))
        if reread["profile_checksum_sha256"] != receipt["profile_checksum_sha256"]:
            raise CustomizationProfileError(
                "customization profile failed its integrity readback"
            )
        return {
            "saved": True,
            "profile_id": profile_id,
            "revision": revision,
            "profile_checksum_sha256": receipt["profile_checksum_sha256"],
        }

    def activate(
        self,
        profile_id: str,
        revision: int,
        *,
        expected_checksum: str,
    ) -> dict[str, Any]:
        self._ensure_root()
        profile = self._read_profile(self._revision_path(profile_id, revision))
        receipt = customization_profile_receipt(profile)
        if receipt["profile_checksum_sha256"] != expected_checksum:
            raise CustomizationProfileError(
                "customization activation checksum does not match"
            )
        pointer = {
            "profile_id": profile_id,
            "revision": revision,
            "profile_checksum_sha256": expected_checksum,
        }
        temporary = self.root / f".active-{uuid4().hex}.tmp"
        try:
            with temporary.open("xb") as output:
                output.write(_canonical_json(pointer).encode("utf-8"))
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.root / "active.json")
        finally:
            temporary.unlink(missing_ok=True)
        return {"activated": True, **pointer, "settings": deepcopy(profile["settings"])}

    def active(self) -> dict[str, Any] | None:
        pointer_path = self.root / "active.json"
        if not pointer_path.exists():
            return None
        if pointer_path.is_symlink() or not pointer_path.is_file():
            raise CustomizationProfileError("active customization pointer is unsafe")
        try:
            pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CustomizationProfileError(
                "active customization pointer is unreadable"
            ) from exc
        if not isinstance(pointer, dict) or set(pointer) != {
            "profile_id", "revision", "profile_checksum_sha256"
        }:
            raise CustomizationProfileError("active customization pointer is invalid")
        profile = self._read_profile(
            self._revision_path(pointer["profile_id"], pointer["revision"])
        )
        receipt = customization_profile_receipt(profile)
        if receipt["profile_checksum_sha256"] != pointer["profile_checksum_sha256"]:
            raise CustomizationProfileError(
                "active customization profile failed its integrity check"
            )
        return {
            **pointer,
            "display_name": profile["display_name"],
            "settings": deepcopy(profile["settings"]),
            "runtime_settings": runtime_customization_settings(profile),
        }


__all__ = [
    "CustomizationProfileStore",
    "CustomizationProfileError",
    "SCHEMA_VERSION",
    "customization_profile_receipt",
    "render_runtime_customization",
    "runtime_customization_settings",
    "validate_customization_profile",
]
