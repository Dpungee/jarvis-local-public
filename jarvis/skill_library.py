from __future__ import annotations

import hashlib
import os
import re
import stat
import tempfile
import threading
from pathlib import Path
from typing import Any

from .redaction import contains_secret


SKILL_ROOT = Path(__file__).resolve().with_name("builtin_skills")
LEARNED_SKILL_DIRECTORY = ".jarvis-skills"
#: The M4 staging root: a **sibling** of the learned root, never a child, so
#: ``list_available_skills`` -- which walks only ``LEARNED_SKILL_DIRECTORY`` --
#: cannot see a staged document however the catalog is later extended.  The
#: second half of the guarantee lives in ``tools._PROTECTED_PATH_COMPONENTS``,
#: which must name this directory so the model's file tools refuse it too.
STAGED_SKILL_DIRECTORY = ".jarvis-skills-staging"
#: A withdrawn document is parked under this prefix inside the staging root.
#: Red team R-1: withdrawing a *row* while leaving the *file* live made
#: ``ladder_unverified_promotions`` report it forever, which pinned
#: ``unverified_at_seal`` above zero, which made clause (4) regress every
#: future epoch, which refused the family every staging and approval
#: permanently -- with the only exit a flag that deletes the operator's work.
#: Moving the file breaks that loop at the first step, and moving rather than
#: deleting means the bytes survive for an operator who wants them back.
WITHDRAWN_SKILL_PREFIX = "withdrawn-"
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


def _refuse_hard_link(path: Path, label: str) -> None:
    """Refuse any skill document with more than one directory entry.

    ``_ordinary_skill_file`` screens symlinks and reparse points but not hard
    links, so a second name for the same inode would let a write into the
    staging root land in the live root (and the reverse).  Verified on this
    NTFS volume: ``os.link`` succeeds and ``st_nlink`` reads 2 on both sides.
    """
    details = os.lstat(path)
    if getattr(details, "st_nlink", 1) > 1:
        raise PermissionError(f"Hard-linked {label} documents cannot be used")


def _parse_skill_document(raw: bytes, folder: str) -> dict[str, Any]:
    text = raw.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
    if not text.startswith("---\n") or "\n---\n" not in text[4:]:
        raise ValueError(f"Malformed skill frontmatter: {folder}")
    header, content = text[4:].split("\n---\n", 1)
    metadata: dict[str, str] = {}
    for line in header.splitlines():
        if ":" not in line:
            raise ValueError(f"Malformed skill metadata: {folder}")
        key, value = line.split(":", 1)
        metadata[key.strip()] = value.strip()
    name = metadata.get("name", "")
    description = metadata.get("description", "")
    if not _SKILL_NAME.fullmatch(name) or name != folder:
        raise ValueError(f"Invalid skill name: {folder}")
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


def _parse_skill(path: Path) -> dict[str, Any]:
    return _parse_skill_document(_ordinary_skill_file(path), path.parent.name)


_BUILTIN_LOCK = threading.Lock()
_BUILTIN_CACHE: tuple[tuple[tuple[str, str], ...], list[dict[str, str]]] | None = None


def _builtin_signature() -> tuple[tuple[str, str], ...]:
    """``((directory, sha256), ...)`` over every bundled SKILL.md.

    A digest rather than a stat, for the same reason the learned catalog uses
    one: an edit in place that preserves size and mtime must still miss.  The
    bundled set ships in the package and does not change at runtime, so this is
    paid once per process in practice.
    """
    if not SKILL_ROOT.is_dir():
        return ()
    signature: list[tuple[str, str]] = []
    try:
        entries = sorted(SKILL_ROOT.iterdir(), key=lambda item: item.name)
    except OSError:
        return ()
    for directory in entries:
        try:
            raw = (directory / "SKILL.md").read_bytes()
        except OSError:
            continue
        signature.append((directory.name, hashlib.sha256(raw).hexdigest()))
    return tuple(signature)


def clear_builtin_cache() -> None:
    """Drop the bundled-catalog memo.  For tests."""
    global _BUILTIN_CACHE
    with _BUILTIN_LOCK:
        _BUILTIN_CACHE = None


def list_builtin_skills() -> list[dict[str, str]]:
    """The bundled catalog, memoized on a digest of the package directory.

    Measured at 2.03 ms unmemoized on this host -- thirteen frontmatter parses
    -- and it is called several times per learning-channel call, directly and
    through ``read_available_skill``.  Nothing in the bundled set changes at
    runtime, so the repeats were pure waste.
    """
    global _BUILTIN_CACHE
    signature = _builtin_signature()
    with _BUILTIN_LOCK:
        cached = _BUILTIN_CACHE
        if cached is not None and cached[0] == signature:
            return [dict(item) for item in cached[1]]
    skills = _read_builtin_skills()
    with _BUILTIN_LOCK:
        _BUILTIN_CACHE = (signature, [dict(item) for item in skills])
    return skills


def _read_builtin_skills() -> list[dict[str, str]]:
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


def _atomic_install(directory: Path, target: Path, raw: bytes) -> None:
    """Put exactly ``raw`` at ``target`` without ever leaving a partial file.

    The same temp-file-plus-fsync path ``update_learned_skill`` uses; it only
    also has to handle the create case, because a promotion may be the first
    document for a family.
    """
    if not os.path.lexists(target):
        _write_new_file(target, raw)
        return
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
        os.replace(temporary_name, target)
        temporary_name = None
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


def read_learned_documents(workspace: Path) -> dict[str, dict[str, Any]]:
    """Every auto-distilled live document, keyed by name, in ONE pass.

    The same shape ``Memory._ladder_live_documents`` builds, produced without
    its second pass: that one calls ``list_available_skills`` and then
    ``read_available_skill`` per entry, which re-walks the bundled catalog
    twice more for every learned document.  Callers hand the result to
    ``ladder_unverified_promotions(documents=...)`` so the walk is paid once
    per turn instead of once per consumer.

    Complete by construction -- every auto-distilled document in the live root,
    not one family's.  A partial index would make the store report
    ``live_document_missing`` for the families it omitted.
    """
    root = _learned_skill_root(workspace)
    if root is None:
        return {}
    known = {item["name"] for item in list_builtin_skills()}
    live: dict[str, dict[str, Any]] = {}
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
        if not parsed.get("auto_distilled"):
            continue
        live[parsed["name"]] = {
            "name": parsed["name"],
            "family": parsed["family"],
            "verified_outcomes": parsed["verified_outcomes"],
            "sha256": parsed["sha256"],
            "content": parsed["content"],
        }
    return live


# --- M4: the staging root -------------------------------------------------
#
# Every function below performs **no policy**.  Whether a document may be
# staged, approved, or restored is decided entirely by ``Memory``'s ladder
# methods; these functions only move bytes safely and report what they moved.


def _staged_skill_root(workspace: Path) -> Path | None:
    root = Path(workspace).resolve() / STAGED_SKILL_DIRECTORY
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


def _writable_staged_root(workspace: Path) -> Path:
    workspace_root = Path(workspace).resolve(strict=True)
    _ordinary_directory(workspace_root, "Workspace")
    root = workspace_root / STAGED_SKILL_DIRECTORY
    root.mkdir(mode=0o700, exist_ok=True)
    _ordinary_directory(root, "Staged skill root")
    if root.resolve(strict=True).parent != workspace_root:
        raise PermissionError("Staged skill root escaped the workspace")
    return root


def _staged_name(name: str) -> str:
    normalized = str(name).strip().casefold()
    if not _SKILL_NAME.fullmatch(normalized) or len(normalized) > 63:
        raise ValueError("Skill name must use lowercase letters, digits, and hyphens")
    if normalized.startswith(WITHDRAWN_SKILL_PREFIX):
        # A withdrawn parking is inert by construction; addressing one by name
        # would make it promotable, which ruling 16 forbids.
        raise PermissionError("Withdrawn documents are restorable only by a new promotion")
    if normalized in {item["name"] for item in list_builtin_skills()}:
        raise PermissionError("Operator-bundled skills cannot be staged or replaced")
    return normalized


def _sole_document(directory: Path, label: str) -> Path:
    _ordinary_directory(directory, label)
    entries = list(directory.iterdir())
    if len(entries) != 1 or entries[0].name != "SKILL.md":
        raise PermissionError(f"{label} contains unexpected files")
    return entries[0]


def stage_learned_skill(
    workspace: Path,
    name: str,
    description: str,
    content: str,
    *,
    family: str,
    verified_outcomes: int,
) -> dict[str, Any]:
    """Write one candidate document into the staging root.

    The staged root is a sibling of the live root, so nothing the skill
    catalog walks can reach it.  A staged document is derived state and is
    always re-derivable from the proof, so an existing one is replaced rather
    than refused; the governance question ("is there already a staged row?")
    belongs to ``Memory.stage_ladder_promotion``, not here.
    """
    normalized = _staged_name(name)
    raw = _skill_document(
        normalized,
        description,
        content,
        family=family,
        auto_distilled=True,
        verified_outcomes=verified_outcomes,
    )
    root = _writable_staged_root(workspace)
    directory = root / normalized
    directory.mkdir(mode=0o700, exist_ok=True)
    _ordinary_directory(directory, "Staged skill directory")
    target = directory / "SKILL.md"
    if os.path.lexists(target):
        _ordinary_skill_file(target)
        _refuse_hard_link(target, "staged skill")
    _atomic_install(directory, target, raw)
    parsed = _parse_skill(target)
    parsed["origin"] = "workspace-staged"
    parsed["trust"] = (
        "staged, unapproved guidance; never reaches the model and never grants "
        "authority or permission"
    )
    parsed["staged"] = True
    return parsed


def _staged_entry_stage(directory_name: str) -> tuple[str, str]:
    """``(skill name, stage)`` for a directory in the staging root."""
    if directory_name.startswith(WITHDRAWN_SKILL_PREFIX):
        return directory_name[len(WITHDRAWN_SKILL_PREFIX):], "withdrawn"
    return directory_name, "staged"


def list_staged_skills(workspace: Path) -> list[dict[str, Any]]:
    """Every readable document in the staging root, for operator surfaces only.

    Two stages share the root and neither is reachable by the model: ``staged``
    is a candidate awaiting the operator's confirmation code, ``withdrawn`` is
    a document the runtime pulled out of the live root.  A withdrawn entry is
    inert -- ``read_staged_skill``, ``discard_staged_skill`` and
    ``promote_staged_skill`` all address ``<name>`` and never
    ``withdrawn-<name>``, so it can only come back through a fresh promotion.
    """
    root = _staged_skill_root(workspace)
    if root is None:
        return []
    staged: list[dict[str, Any]] = []
    for directory in sorted(root.iterdir(), key=lambda item: item.name):
        try:
            details = directory.lstat()
            attributes = getattr(details, "st_file_attributes", 0)
            if (
                not directory.is_dir()
                or stat.S_ISLNK(details.st_mode)
                or attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
            ):
                continue
            name, stage = _staged_entry_stage(directory.name)
            parsed = _parse_skill_document(
                _ordinary_skill_file(directory / "SKILL.md"), name
            )
        except (OSError, UnicodeError, ValueError):
            continue
        staged.append({
            "name": parsed["name"],
            "description": parsed["description"],
            "version": parsed["version"],
            "origin": "workspace-staged",
            "stage": stage,
            "withdrawn": stage == "withdrawn",
            "directory": directory.name,
            "family": parsed["family"],
            "auto_distilled": parsed["auto_distilled"],
            "verified_outcomes": parsed["verified_outcomes"],
            "sha256": parsed["sha256"],
        })
    return staged


def list_withdrawn_skills(workspace: Path) -> list[dict[str, Any]]:
    """Only the withdrawn parkings, for ``ladder verify`` and ``ladder status``."""
    return [item for item in list_staged_skills(workspace) if item["withdrawn"]]


def withdraw_learned_skill(workspace: Path, name: str) -> dict[str, Any]:
    """Move a live learned document out of the catalog's reach, never delete it.

    Called by ``Memory.withdraw_ladder_promotion`` when the row it retires was
    live.  The document lands at
    ``<workspace>/.jarvis-skills-staging/withdrawn-<name>/SKILL.md``: outside
    the only directory ``list_available_skills`` walks, and inside the
    directory the model's file tools refuse, so it is unreachable two ways --
    the same pair of guarantees a staged document gets.

    **It never raises for an absent document, and never fails because a
    previous withdrawal is already parked there.** A withdrawal that can fail
    leaves the file live while the row says otherwise, which is exactly the
    R-1 loop this exists to break; so a second withdrawal of the same name
    replaces the parked copy and reports the digest it displaced in
    ``replaced_sha256`` rather than refusing. Nothing is lost silently: the
    displaced digest is in the return value, and the promotion row records
    ``withdrawn_sha256`` independently.

    The copy is written before the live document is removed, so a crash
    between the two leaves the document in both places -- recoverable -- rather
    than in neither.
    """
    normalized = _staged_name(name)
    live_root = Path(workspace).resolve() / LEARNED_SKILL_DIRECTORY
    live_directory = live_root / normalized
    live_target = live_directory / "SKILL.md"
    if not live_directory.exists() or not os.path.lexists(live_target):
        return {
            "name": normalized,
            "withdrawn_name": f"{WITHDRAWN_SKILL_PREFIX}{normalized}",
            "path": None,
            "sha256": None,
            "replaced_sha256": None,
            "withdrawn": False,
            "moved": False,
        }
    _ordinary_directory(live_directory, "Learned skill directory")
    raw = _ordinary_skill_file(live_target)
    _refuse_hard_link(live_target, "learned skill")
    digest = hashlib.sha256(raw).hexdigest()

    root = _writable_staged_root(workspace)
    parked_directory = root / f"{WITHDRAWN_SKILL_PREFIX}{normalized}"
    parked_directory.mkdir(mode=0o700, exist_ok=True)
    _ordinary_directory(parked_directory, "Withdrawn skill directory")
    parked_target = parked_directory / "SKILL.md"
    replaced: str | None = None
    if os.path.lexists(parked_target):
        replaced = hashlib.sha256(_ordinary_skill_file(parked_target)).hexdigest()
        _refuse_hard_link(parked_target, "withdrawn skill")
    _atomic_install(parked_directory, parked_target, raw)

    live_target.unlink()
    try:
        live_directory.rmdir()
    except OSError:
        pass
    return {
        "name": normalized,
        "withdrawn_name": f"{WITHDRAWN_SKILL_PREFIX}{normalized}",
        "path": str(parked_target),
        "sha256": digest,
        "replaced_sha256": replaced,
        "withdrawn": True,
        "moved": True,
    }


def read_staged_skill(name: str, workspace: Path) -> dict[str, Any]:
    """Read one staged document; operator surfaces only, never the prompt."""
    normalized = str(name).strip().casefold()
    if not _SKILL_NAME.fullmatch(normalized):
        raise ValueError("Skill name must use lowercase letters, digits, and hyphens")
    root = _staged_skill_root(workspace)
    if root is None or not (root / normalized / "SKILL.md").exists():
        raise KeyError(f"Unknown staged skill: {normalized}")
    parsed = _parse_skill(root / normalized / "SKILL.md")
    parsed["origin"] = "workspace-staged"
    parsed["trust"] = (
        "staged, unapproved guidance; never reaches the model and never grants "
        "authority or permission"
    )
    return parsed


def discard_staged_skill(workspace: Path, name: str) -> dict[str, Any]:
    """Remove exactly one staged document and its directory."""
    normalized = str(name).strip().casefold()
    if not _SKILL_NAME.fullmatch(normalized):
        raise ValueError("Skill name must use lowercase letters, digits, and hyphens")
    root = _staged_skill_root(workspace)
    if root is None:
        raise KeyError(f"Unknown staged skill: {normalized}")
    directory = root / normalized
    if not directory.exists():
        raise KeyError(f"Unknown staged skill: {normalized}")
    target = _sole_document(directory, "Staged skill directory")
    parsed = _parse_skill(target)
    _refuse_hard_link(target, "staged skill")
    target.unlink()
    directory.rmdir()
    return {
        "name": parsed["name"],
        "family": parsed["family"],
        "sha256": parsed["sha256"],
        "discarded": True,
    }


def promote_staged_skill(
    workspace: Path,
    name: str,
    *,
    expected_staged_sha256: str,
) -> dict[str, Any]:
    """Move the staged bytes into the live root and hand back the prior bytes.

    The **only** function that installs a learned document from the ladder,
    and it performs no policy: proof, gate, ledger, confirmation code and
    workspace are all checked by ``Memory.apply_ladder_promotion`` before this
    is called.  It refuses unless the staged document still hashes to
    ``expected_staged_sha256``, and unless both the staged and the live
    documents have exactly one directory entry.
    """
    normalized = _staged_name(name)
    expected = str(expected_staged_sha256).strip()
    if not _SKILL_DIGEST.fullmatch(expected):
        raise ValueError("expected_staged_sha256 must be a lowercase SHA-256 digest")
    staged_root = _staged_skill_root(workspace)
    if staged_root is None or not (staged_root / normalized).exists():
        raise KeyError(f"Unknown staged skill: {normalized}")
    staged_directory = staged_root / normalized
    staged_target = _sole_document(staged_directory, "Staged skill directory")
    raw = _ordinary_skill_file(staged_target)
    _refuse_hard_link(staged_target, "staged skill")
    if hashlib.sha256(raw).hexdigest() != expected:
        raise RuntimeError("Staged skill changed after it was read; stage it again")
    staged = _parse_skill_document(raw, normalized)

    live_root = _writable_learned_root(workspace)
    live_directory = live_root / normalized
    live_target = live_directory / "SKILL.md"
    prior_document: bytes | None = None
    prior_sha256: str | None = None
    if live_directory.exists():
        _ordinary_directory(live_directory, "Learned skill directory")
        if os.path.lexists(live_target):
            prior_document = _ordinary_skill_file(live_target)
            _refuse_hard_link(live_target, "learned skill")
            prior_sha256 = hashlib.sha256(prior_document).hexdigest()
    else:
        live_directory.mkdir(mode=0o700, exist_ok=False)
        _ordinary_directory(live_directory, "Learned skill directory")

    _atomic_install(live_directory, live_target, raw)
    installed = _parse_skill(live_target)
    if installed["sha256"] != expected:
        raise RuntimeError("Learned skill changed during the final promotion check")
    staged_target.unlink()
    try:
        staged_directory.rmdir()
    except OSError:
        pass
    return {
        "name": installed["name"],
        "approved_sha256": installed["sha256"],
        "prior_document": prior_document,
        "prior_sha256": prior_sha256,
        "family": staged["family"],
        "verified_outcomes": installed["verified_outcomes"],
    }


def restore_learned_skill(
    workspace: Path,
    name: str,
    document: bytes | None,
) -> dict[str, Any]:
    """Put back exactly the bytes a promotion replaced, or remove the document.

    ``document is None`` means "no learned document existed before", so the
    live one is removed with the checks ``forget_learned_skill`` applies.  The
    bytes are validated in memory before anything on disk moves, so a corrupt
    ``prior_document`` can never replace a good live document with garbage.
    """
    normalized = _staged_name(name)
    root_path = Path(workspace).resolve() / LEARNED_SKILL_DIRECTORY
    directory = root_path / normalized
    target = directory / "SKILL.md"

    if document is None:
        if not directory.exists():
            return {"name": normalized, "restored": False, "removed": False}
        removed = _sole_document(directory, "Learned skill directory")
        parsed = _parse_skill(removed)
        _refuse_hard_link(removed, "learned skill")
        removed.unlink()
        directory.rmdir()
        return {
            "name": parsed["name"],
            "family": parsed["family"],
            "removed_sha256": parsed["sha256"],
            "restored": False,
            "removed": True,
        }

    raw = bytes(document)
    if len(raw) > MAX_SKILL_BYTES:
        raise ValueError("Skill document exceeds the 32 KB bound")
    parsed = _parse_skill_document(raw, normalized)
    if contains_secret(parsed["description"]) or contains_secret(parsed["content"]):
        raise ValueError("Skill documents cannot contain credentials or secret-shaped values")
    live_root = _writable_learned_root(workspace)
    directory = live_root / normalized
    target = directory / "SKILL.md"
    if directory.exists():
        _ordinary_directory(directory, "Learned skill directory")
        if os.path.lexists(target):
            _ordinary_skill_file(target)
            _refuse_hard_link(target, "learned skill")
    else:
        directory.mkdir(mode=0o700, exist_ok=False)
        _ordinary_directory(directory, "Learned skill directory")
    _atomic_install(directory, target, raw)
    installed = _parse_skill(target)
    if installed["sha256"] != hashlib.sha256(raw).hexdigest():
        raise RuntimeError("Learned skill changed during the final restore check")
    return {
        "name": installed["name"],
        "family": installed["family"],
        "restored_sha256": installed["sha256"],
        "restored": True,
        "removed": False,
    }
