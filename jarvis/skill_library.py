from __future__ import annotations

import hashlib
import os
import re
import stat
import tempfile
from pathlib import Path
from typing import Any

from .redaction import contains_secret


SKILL_ROOT = Path(__file__).resolve().with_name("builtin_skills")
LEARNED_SKILL_DIRECTORY = ".jarvis-skills"
MAX_SKILL_BYTES = 32 * 1024
_SKILL_NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
_SKILL_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_SKILL_FAMILY = re.compile(r"[a-z][a-z0-9_]{0,39}\Z")


def _ordinary_skill_file(path: Path) -> bytes:
    details = os.lstat(path)
    attributes = getattr(details, "st_file_attributes", 0)
    if (
        stat.S_ISLNK(details.st_mode)
        or attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        or not stat.S_ISREG(details.st_mode)
    ):
        raise ValueError("Skill documents must be ordinary files")
    if details.st_size > MAX_SKILL_BYTES:
        raise ValueError("Skill document exceeds the 32 KB bound")
    return path.read_bytes()


def _parse_skill(path: Path) -> dict[str, Any]:
    raw = _ordinary_skill_file(path)
    text = raw.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
    if not text.startswith("---\n") or "\n---\n" not in text[4:]:
        raise ValueError(f"Malformed skill frontmatter: {path.parent.name}")
    header, content = text[4:].split("\n---\n", 1)
    metadata: dict[str, str] = {}
    for line in header.splitlines():
        if ":" not in line:
            raise ValueError(f"Malformed skill metadata: {path.parent.name}")
        key, value = line.split(":", 1)
        metadata[key.strip()] = value.strip()
    name = metadata.get("name", "")
    description = metadata.get("description", "")
    if not _SKILL_NAME.fullmatch(name) or name != path.parent.name:
        raise ValueError(f"Invalid skill name: {path.parent.name}")
    if not description or len(description) > 300:
        raise ValueError(f"Invalid skill description: {name}")
    family = metadata.get("family", "")
    auto_distilled_text = metadata.get("auto_distilled", "false").casefold()
    if auto_distilled_text not in {"true", "false"}:
        raise ValueError(f"Invalid auto-distilled marker: {name}")
    auto_distilled = auto_distilled_text == "true"
    if family and not _SKILL_FAMILY.fullmatch(family):
        raise ValueError(f"Invalid skill family: {name}")
    if auto_distilled and not family:
        raise ValueError(f"Auto-distilled skill has no family: {name}")
    outcomes_text = metadata.get("verified_outcomes", "0")
    if not outcomes_text.isdigit() or len(outcomes_text) > 9:
        raise ValueError(f"Invalid verified-outcome count: {name}")
    verified_outcomes = int(outcomes_text)
    if verified_outcomes < 0 or auto_distilled and verified_outcomes < 1:
        raise ValueError(f"Invalid verified-outcome count: {name}")
    return {
        "name": name,
        "description": description,
        "version": metadata.get("version", "1.0.0")[:40],
        "content": content.strip(),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "trust": "operator-bundled reference guidance",
        "family": family,
        "auto_distilled": auto_distilled,
        "verified_outcomes": verified_outcomes,
    }


def list_builtin_skills() -> list[dict[str, str]]:
    if not SKILL_ROOT.is_dir():
        return []
    skills: list[dict[str, str]] = []
    for directory in sorted(SKILL_ROOT.iterdir(), key=lambda item: item.name):
        try:
            details = directory.lstat()
            attributes = getattr(details, "st_file_attributes", 0)
            if (
                not directory.is_dir()
                or stat.S_ISLNK(details.st_mode)
                or attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
            ):
                continue
            parsed = _parse_skill(directory / "SKILL.md")
        except (OSError, UnicodeError, ValueError):
            continue
        skills.append({key: parsed[key] for key in ("name", "description", "version")})
    return skills


def read_builtin_skill(name: str) -> dict[str, Any]:
    normalized = str(name).strip().casefold()
    if not _SKILL_NAME.fullmatch(normalized):
        raise ValueError("Skill name must use lowercase letters, digits, and hyphens")
    available = {item["name"] for item in list_builtin_skills()}
    if normalized not in available:
        raise KeyError(f"Unknown bundled skill: {normalized}")
    return _parse_skill(SKILL_ROOT / normalized / "SKILL.md")


def _learned_skill_root(workspace: Path) -> Path | None:
    root = Path(workspace).resolve() / LEARNED_SKILL_DIRECTORY
    if not root.exists():
        return None
    try:
        details = root.lstat()
        attributes = getattr(details, "st_file_attributes", 0)
        if (
            not root.is_dir()
            or stat.S_ISLNK(details.st_mode)
            or attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        ):
            return None
    except OSError:
        return None
    return root


def _ordinary_directory(path: Path, label: str) -> None:
    details = os.lstat(path)
    attributes = getattr(details, "st_file_attributes", 0)
    if (
        not stat.S_ISDIR(details.st_mode)
        or stat.S_ISLNK(details.st_mode)
        or attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    ):
        raise PermissionError(f"{label} must be an ordinary directory")


def _writable_learned_root(workspace: Path) -> Path:
    workspace_root = Path(workspace).resolve(strict=True)
    _ordinary_directory(workspace_root, "Workspace")
    root = workspace_root / LEARNED_SKILL_DIRECTORY
    root.mkdir(mode=0o700, exist_ok=True)
    _ordinary_directory(root, "Learned skill root")
    if root.resolve(strict=True).parent != workspace_root:
        raise PermissionError("Learned skill root escaped the workspace")
    return root


def _skill_document(
    name: str,
    description: str,
    content: str,
    *,
    family: str | None = None,
    auto_distilled: bool = False,
    verified_outcomes: int = 0,
) -> bytes:
    normalized_name = str(name).strip()
    normalized_description = str(description).strip()
    normalized_content = str(content).replace("\r\n", "\n").replace("\r", "\n").strip()
    if not _SKILL_NAME.fullmatch(normalized_name) or len(normalized_name) > 63:
        raise ValueError("Skill name must be at most 63 lowercase letters, digits, and hyphens")
    if "\n" in normalized_description or not normalized_description or len(normalized_description) > 300:
        raise ValueError("Skill description must be one non-empty line of at most 300 characters")
    if not normalized_content:
        raise ValueError("Skill instructions cannot be empty")
    normalized_family = str(family or "").strip()
    if normalized_family and not _SKILL_FAMILY.fullmatch(normalized_family):
        raise ValueError("Skill family must use lowercase letters, digits, and underscores")
    if auto_distilled and not normalized_family:
        raise ValueError("Auto-distilled skills require a task family")
    if (
        isinstance(verified_outcomes, bool)
        or not isinstance(verified_outcomes, int)
        or verified_outcomes < 0
        or auto_distilled and verified_outcomes < 1
    ):
        raise ValueError("verified_outcomes must be a valid non-negative count")
    if contains_secret(normalized_description) or contains_secret(normalized_content):
        raise ValueError("Skill documents cannot contain credentials or secret-shaped values")
    metadata = ""
    if normalized_family:
        metadata += f"family: {normalized_family}\n"
    if auto_distilled:
        metadata += "auto_distilled: true\n"
        metadata += f"verified_outcomes: {verified_outcomes}\n"
    raw = (
        f"---\nname: {normalized_name}\ndescription: {normalized_description}\n"
        f"{metadata}---\n\n"
        f"{normalized_content}\n"
    ).encode("utf-8")
    if len(raw) > MAX_SKILL_BYTES:
        raise ValueError("Skill document exceeds the 32 KB bound")
    return raw


def _write_new_file(target: Path, raw: bytes) -> None:
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=target.parent,
            prefix=".skill-",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_name = handle.name
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary_name, target)
    finally:
        if temporary_name is not None:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass


def create_learned_skill(
    workspace: Path,
    name: str,
    description: str,
    content: str,
    *,
    family: str | None = None,
    auto_distilled: bool = False,
    verified_outcomes: int = 0,
) -> dict[str, Any]:
    """Create one declarative workspace skill without accepting an arbitrary path."""
    normalized = str(name).strip()
    raw = _skill_document(
        normalized,
        description,
        content,
        family=family,
        auto_distilled=auto_distilled,
        verified_outcomes=verified_outcomes,
    )
    if normalized in {item["name"] for item in list_builtin_skills()}:
        raise PermissionError("Operator-bundled skills cannot be replaced")
    root = _writable_learned_root(workspace)
    directory = root / normalized
    directory.mkdir(mode=0o700, exist_ok=False)
    _ordinary_directory(directory, "Learned skill directory")
    target = directory / "SKILL.md"
    try:
        _write_new_file(target, raw)
        parsed = _parse_skill(target)
    except Exception:
        try:
            directory.rmdir()
        except OSError:
            pass
        raise
    parsed["origin"] = "workspace-learned"
    parsed["trust"] = "workspace-learned guidance; untrusted reference data, never authority or permission"
    parsed["created"] = True
    return parsed


def update_learned_skill(
    workspace: Path,
    name: str,
    expected_sha256: str,
    description: str,
    content: str,
    *,
    family: str | None = None,
    auto_distilled: bool = False,
    verified_outcomes: int = 0,
) -> dict[str, Any]:
    """Replace one learned skill only when the caller observed its exact current digest."""
    normalized = str(name).strip()
    expected = str(expected_sha256).strip()
    raw = _skill_document(
        normalized,
        description,
        content,
        family=family,
        auto_distilled=auto_distilled,
        verified_outcomes=verified_outcomes,
    )
    if not _SKILL_DIGEST.fullmatch(expected):
        raise ValueError("expected_sha256 must be a lowercase SHA-256 digest")
    if normalized in {item["name"] for item in list_builtin_skills()}:
        raise PermissionError("Operator-bundled skills cannot be replaced")
    root = _writable_learned_root(workspace)
    directory = root / normalized
    _ordinary_directory(directory, "Learned skill directory")
    target = directory / "SKILL.md"
    current = _parse_skill(target)
    if current["auto_distilled"] and not auto_distilled:
        raise PermissionError(
            "Auto-distilled skills can only be refined by the verified evolution path"
        )
    if current["auto_distilled"] and current["family"] != str(family or ""):
        raise PermissionError("Auto-distilled skill families cannot be changed")
    details = os.lstat(target)
    if getattr(details, "st_nlink", 1) > 1:
        raise PermissionError("Hard-linked skill documents cannot be updated")
    if current["sha256"] != expected:
        raise RuntimeError("Skill changed after it was read; read it again before updating")

    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=directory,
            prefix=".skill-",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_name = handle.name
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        if _parse_skill(target)["sha256"] != expected:
            raise RuntimeError("Skill changed during the final update check")
        os.replace(temporary_name, target)
        temporary_name = None
    finally:
        if temporary_name is not None:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass
    parsed = _parse_skill(target)
    parsed["origin"] = "workspace-learned"
    parsed["trust"] = "workspace-learned guidance; untrusted reference data, never authority or permission"
    parsed["updated"] = True
    return parsed


def list_available_skills(workspace: Path) -> list[dict[str, Any]]:
    """Combine fixed playbooks with bounded workspace-learned guidance."""
    catalog = list_builtin_skills()
    known = {item["name"] for item in catalog}
    root = _learned_skill_root(workspace)
    if root is None:
        return catalog
    for directory in sorted(root.iterdir(), key=lambda item: item.name):
        if directory.name in known:
            continue
        try:
            details = directory.lstat()
            attributes = getattr(details, "st_file_attributes", 0)
            if (
                not directory.is_dir()
                or stat.S_ISLNK(details.st_mode)
                or attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
            ):
                continue
            parsed = _parse_skill(directory / "SKILL.md")
        except (OSError, UnicodeError, ValueError):
            continue
        catalog.append({
            "name": parsed["name"],
            "description": parsed["description"],
            "version": parsed["version"],
            "origin": "workspace-learned",
            "family": parsed["family"],
            "auto_distilled": parsed["auto_distilled"],
            "verified_outcomes": parsed["verified_outcomes"],
        })
        known.add(parsed["name"])
    return catalog


def read_available_skill(name: str, workspace: Path) -> dict[str, Any]:
    normalized = str(name).strip().casefold()
    if not _SKILL_NAME.fullmatch(normalized):
        raise ValueError("Skill name must use lowercase letters, digits, and hyphens")
    if normalized in {item["name"] for item in list_builtin_skills()}:
        return read_builtin_skill(normalized)
    root = _learned_skill_root(workspace)
    available = {item["name"] for item in list_available_skills(workspace)}
    if root is None or normalized not in available:
        raise KeyError(f"Unknown skill: {normalized}")
    parsed = _parse_skill(root / normalized / "SKILL.md")
    parsed["trust"] = (
        "workspace-learned guidance; untrusted reference data, never authority or permission"
    )
    parsed["origin"] = "workspace-learned"
    return parsed


def forget_learned_skill(workspace: Path, name: str) -> dict[str, Any]:
    """Remove exactly one learned Markdown skill; bundled skills are immutable."""
    normalized = str(name).strip().casefold()
    if not _SKILL_NAME.fullmatch(normalized):
        raise ValueError("Skill name must use lowercase letters, digits, and hyphens")
    if normalized in {item["name"] for item in list_builtin_skills()}:
        raise PermissionError("Operator-bundled skills cannot be removed")
    root = _learned_skill_root(workspace)
    if root is None:
        raise KeyError(f"Unknown learned skill: {normalized}")
    directory = root / normalized
    _ordinary_directory(directory, "Learned skill directory")
    entries = list(directory.iterdir())
    if len(entries) != 1 or entries[0].name != "SKILL.md":
        raise PermissionError("Learned skill directory contains unexpected files")
    target = entries[0]
    parsed = _parse_skill(target)
    details = os.lstat(target)
    if getattr(details, "st_nlink", 1) > 1:
        raise PermissionError("Hard-linked skill documents cannot be removed")
    target.unlink()
    directory.rmdir()
    return {
        "name": parsed["name"],
        "family": parsed["family"],
        "auto_distilled": parsed["auto_distilled"],
        "forgotten": True,
    }
