#!/usr/bin/env python3
"""Emit a sanitized, exact-revision Phase 0 release-baseline record."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
import tempfile
import tomllib
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from jarvis.trusted_executables import trusted_path_executable


_FULL_SHA_RE = re.compile(r"[0-9a-f]{40}\Z")
_IDENTITY_POLICY_FILES = (
    "SOUL.md",
    "CONSTITUTION.md",
    "PUBLIC_SOUL.md",
    "PUBLIC_PROFILE.json",
    "PUBLIC_POLICY.md",
    "docs/public_presence/THREAT_MODEL.md",
    "docs/public_presence/PROHIBITED_ACTIONS.md",
)
_MIGRATION_SOURCE_FILES = (
    "jarvis/memory.py",
    "jarvis/memory_schema_migrations.py",
    "jarvis/public_presence_store.py",
)
_PROHIBITED_PUBLIC_TOOL_TOKENS = frozenset({
    "publish",
    "send",
    "delete",
    "follow",
    "message",
    "shell",
    "process",
    "filesystem",
    "browser",
    "credential",
    "payment",
    "trade",
    "wallet",
    "deploy",
    "email",
    "calendar",
    "drive",
    "computer",
})


class BaselineError(RuntimeError):
    """Raised when exact release-baseline evidence cannot be established."""


def _git(repo: Path, *arguments: str) -> str:
    try:
        executable = trusted_path_executable("git")
    except (FileNotFoundError, PermissionError, ValueError) as exc:
        raise BaselineError("A trusted Git executable is required") from exc
    completed = subprocess.run(
        [str(executable), "-c", "core.fsmonitor=false", *arguments],
        cwd=repo,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        env={
            key: value
            for key, value in os.environ.items()
            if not key.startswith("GIT_")
        }
        | {"GIT_NO_REPLACE_OBJECTS": "1", "GIT_OPTIONAL_LOCKS": "0"},
    )
    if completed.returncode != 0:
        raise BaselineError("Git could not establish the exact release baseline")
    return completed.stdout.decode("utf-8", errors="strict").strip()


def _git_clean(repo: Path, *arguments: str) -> bool:
    try:
        executable = trusted_path_executable("git")
    except (FileNotFoundError, PermissionError, ValueError) as exc:
        raise BaselineError("A trusted Git executable is required") from exc
    completed = subprocess.run(
        [str(executable), "-c", "core.fsmonitor=false", *arguments],
        cwd=repo,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        check=False,
        env={
            key: value
            for key, value in os.environ.items()
            if not key.startswith("GIT_")
        }
        | {"GIT_NO_REPLACE_OBJECTS": "1", "GIT_OPTIONAL_LOCKS": "0"},
    )
    if completed.returncode not in {0, 1}:
        raise BaselineError("Git could not verify the release snapshot")
    return completed.returncode == 0


def _canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def _safe_repo_file(repo: Path, relative: str) -> Path:
    pure = PurePosixPath(relative)
    if pure.is_absolute() or ".." in pure.parts:
        raise BaselineError("baseline file path is not repository-relative")
    path = repo.joinpath(*pure.parts)
    if path.is_symlink() or not path.is_file():
        raise BaselineError(f"required baseline file is unavailable: {relative}")
    try:
        path.resolve(strict=True).relative_to(repo)
    except ValueError as exc:
        raise BaselineError("baseline file resolves outside the repository") from exc
    return path


def _bundle_sha256(repo: Path, relatives: Iterable[str]) -> str:
    digest = hashlib.sha256()
    for relative in sorted(relatives):
        path = _safe_repo_file(repo, relative)
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _repository_snapshot(repo: Path, *, require_clean: bool) -> tuple[str, tuple[str, ...]]:
    status = _git(repo, "status", "--porcelain=v1", "--untracked-files=all")
    flags = _git(repo, "ls-files", "-v", "-z")
    flagged = [record for record in flags.split("\0") if record and record[0] != "H"]
    worktree_clean = _git_clean(repo, "diff-files", "--quiet", "--")
    index_clean = _git_clean(repo, "diff-index", "--cached", "--quiet", "HEAD", "--")
    clean = not status and not flagged and worktree_clean and index_clean
    if require_clean and not clean:
        raise BaselineError(
            "working tree, index, and tracked-file flags must be clean before baseline capture"
        )
    commit = _git(repo, "rev-parse", "HEAD").casefold()
    if not _FULL_SHA_RE.fullmatch(commit):
        raise BaselineError("Git did not return a full commit ID")
    tracked_output = _git(repo, "ls-files", "-z")
    tracked = tuple(item for item in tracked_output.split("\0") if item)
    if not tracked:
        raise BaselineError("repository has no tracked files")
    return commit, tracked


def _literal_integer_assignment(path: Path, name: str) -> int:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=path.name)
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if not any(isinstance(target, ast.Name) and target.id == name for target in targets):
            continue
        value = ast.literal_eval(node.value)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            break
        return value
    raise BaselineError(f"{name} is not a literal non-negative integer")


def _private_tool_manifest() -> list[dict[str, Any]]:
    from jarvis.feature_onboarding import FEATURE_SPECS
    from jarvis.tool_specs import build_tool_specs
    from jarvis.tools import (
        MAX_BATCH_READ_FILES,
        MAX_RESEARCH_QUESTION_RESULTS,
        MAX_SCAN_HOSTS,
        MAX_TOOL_DEFINITION_BYTES,
        MAX_TOOL_OUTPUT,
        SUPPORTED_DOCUMENT_TYPES,
    )

    specs = build_tool_specs(
        feature_specs=FEATURE_SPECS,
        max_batch_read_files=MAX_BATCH_READ_FILES,
        max_research_question_results=MAX_RESEARCH_QUESTION_RESULTS,
        max_scan_hosts=MAX_SCAN_HOSTS,
        max_tool_definition_bytes=MAX_TOOL_DEFINITION_BYTES,
        max_tool_output=MAX_TOOL_OUTPUT,
        supported_document_types=SUPPORTED_DOCUMENT_TYPES,
    )
    manifest = [
        {
            "name": spec.name,
            "description": spec.description,
            "parameters": spec.parameters,
            "handler": spec.handler_name,
        }
        for spec in specs
    ]
    if len({item["name"] for item in manifest}) != len(manifest):
        raise BaselineError("private tool manifest contains duplicate names")
    return manifest


def _public_tool_manifest() -> list[dict[str, Any]]:
    from jarvis.public_tools import PUBLIC_TOOL_NAMES, _SPECS as PUBLIC_TOOL_SPECS

    manifest = [spec.schema()["function"] for spec in PUBLIC_TOOL_SPECS]
    names = tuple(item["name"] for item in manifest)
    if names != PUBLIC_TOOL_NAMES:
        raise BaselineError("public tool manifest differs from its closed allowlist")
    for name in names:
        tokens = set(re.split(r"[^a-z0-9]+", name.casefold()))
        if tokens & _PROHIBITED_PUBLIC_TOOL_TOKENS:
            raise BaselineError("public tool manifest contains a prohibited capability")
    return manifest


def _offline_public_status() -> dict[str, Any]:
    from jarvis.public_presence_service import (
        PublicPresenceService,
        durable_control_reader,
        environment_enabled,
    )
    from jarvis.public_presence_store import PublicPresenceStore

    with tempfile.TemporaryDirectory(prefix="jarvis-public-baseline-") as temporary:
        database_path = Path(temporary) / "public_presence.db"
        # Initialize only the disposable public database. The process remains
        # disabled and one-shot; no listener, adapter, credential, or network is used.
        PublicPresenceStore(database_path)
        service = PublicPresenceService(
            enabled=environment_enabled(),
            control_reader=durable_control_reader(database_path),
        )
        status = service.status()
    if status["enabled"] is not False:
        raise BaselineError("Public Presence must be disabled for a Phase 0 baseline")
    if (
        status["running"] is not False
        or status["mode"] != "offline"
        or status["external_communication"] is not False
        or status["connected_platforms"] != []
    ):
        raise BaselineError("Public Presence is not safely offline")
    return status


def _runtime_evidence_in_process() -> dict[str, Any]:
    from jarvis.public_presence_store import PUBLIC_PRESENCE_SCHEMA_VERSION
    from jarvis.self_diagnosis import runtime_manifest_sha256

    private_tools = _private_tool_manifest()
    public_tools = _public_tool_manifest()
    return {
        "runtime_manifest_sha256": runtime_manifest_sha256(PROJECT_ROOT),
        "public_schema_version": PUBLIC_PRESENCE_SCHEMA_VERSION,
        "private_tools": private_tools,
        "public_tools": public_tools,
        "public_status": _offline_public_status(),
    }


def _fresh_runtime_evidence(repo: Path) -> dict[str, Any]:
    allowed_environment = {
        key: value
        for key, value in os.environ.items()
        if key.upper()
        in {
            "APPDATA",
            "COMSPEC",
            "LOCALAPPDATA",
            "PATH",
            "PATHEXT",
            "PROGRAMDATA",
            "SYSTEMDRIVE",
            "SYSTEMROOT",
            "TEMP",
            "TMP",
            "WINDIR",
        }
    }
    allowed_environment.update({
        "JARVIS_PUBLIC_PRESENCE_ENABLED": "false",
        "JARVIS_EXTERNAL_ACCESS": "disabled",
        "JARVIS_CLOUD_ENABLED": "false",
        "JARVIS_OLLAMA_ENABLED": "false",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
    })
    completed = subprocess.run(
        [sys.executable, "-I", str(Path(__file__).resolve()), "--runtime-evidence"],
        cwd=repo,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        env=allowed_environment,
    )
    if completed.returncode != 0:
        raise BaselineError("fresh runtime evidence process failed")
    try:
        evidence = json.loads(completed.stdout.decode("utf-8", errors="strict"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise BaselineError("fresh runtime evidence is invalid") from exc
    required = {
        "runtime_manifest_sha256",
        "public_schema_version",
        "private_tools",
        "public_tools",
        "public_status",
    }
    if not isinstance(evidence, dict) or set(evidence) != required:
        raise BaselineError("fresh runtime evidence has an unexpected schema")
    return evidence


def collect_baseline(repo: Path, *, require_clean: bool = True) -> dict[str, Any]:
    if os.environ.get("JARVIS_PUBLIC_PRESENCE_ENABLED", "false").strip().casefold() in {
        "1",
        "true",
        "yes",
        "on",
    }:
        raise BaselineError("Public Presence must be disabled for a Phase 0 baseline")
    root = Path(repo).resolve(strict=True)
    if root != PROJECT_ROOT.resolve(strict=True):
        raise BaselineError("baseline code must run from the repository being measured")
    commit, tracked = _repository_snapshot(root, require_clean=require_clean)
    commit_time = _git(root, "show", "-s", "--format=%ct", commit)
    if not commit_time.isdecimal():
        raise BaselineError("Git commit time is invalid")

    pyproject = tomllib.loads(
        _safe_repo_file(root, "pyproject.toml").read_text(encoding="utf-8")
    )
    package_version = str(pyproject["project"]["version"])
    init_tree = ast.parse(
        _safe_repo_file(root, "jarvis/__init__.py").read_text(encoding="utf-8")
    )
    init_version = None
    for node in init_tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "__version__"
            for target in node.targets
        ):
            init_version = ast.literal_eval(node.value)
    if package_version != init_version:
        raise BaselineError("package version declarations do not match")

    private_schema = _literal_integer_assignment(
        _safe_repo_file(root, "jarvis/memory.py"), "SCHEMA_VERSION"
    )
    public_schema = _literal_integer_assignment(
        _safe_repo_file(root, "jarvis/public_presence_store.py"),
        "PUBLIC_PRESENCE_SCHEMA_VERSION",
    )
    runtime_evidence = _fresh_runtime_evidence(root)
    if public_schema != runtime_evidence["public_schema_version"]:
        raise BaselineError("loaded public schema differs from source")

    private_tools = runtime_evidence["private_tools"]
    public_tools = runtime_evidence["public_tools"]
    public_status = runtime_evidence["public_status"]
    safe_configuration = {
        "public_presence_enabled": public_status["enabled"],
        "public_mode": public_status["mode"],
        "public_running": public_status["running"],
        "external_communication": public_status["external_communication"],
        "connected_platform_count": len(public_status["connected_platforms"]),
        "publishing_method_count": 0,
    }
    baseline = {
        "schema": "jarvis.phase0-release-baseline.v1",
        "captured_at_utc": datetime.now(timezone.utc).isoformat(),
        "repository": {
            "commit": commit,
            "clean": require_clean,
            "source_date_epoch": int(commit_time),
        },
        "runtime": {
            "package_version": package_version,
            "python_version": platform.python_version(),
            "os_family": platform.system(),
            "private_schema_version": private_schema,
            "public_schema_version": public_schema,
        },
        "digests": {
            "runtime_manifest_sha256": runtime_evidence["runtime_manifest_sha256"],
            "release_tree_content_sha256": _bundle_sha256(root, tracked),
            "migration_set_sha256": _bundle_sha256(root, _MIGRATION_SOURCE_FILES),
            "identity_policy_bundle_sha256": _bundle_sha256(
                root, _IDENTITY_POLICY_FILES
            ),
            "redacted_configuration_sha256": _canonical_sha256(safe_configuration),
        },
        "private_tool_manifest": {
            "scope": "complete declarative built-in catalog before runtime filtering",
            "count": len(private_tools),
            "sha256": _canonical_sha256(private_tools),
        },
        "public_tool_manifest": {
            "scope": "closed offline registry",
            "count": len(public_tools),
            "sha256": _canonical_sha256(public_tools),
            "publishing_methods": [],
        },
        "safe_configuration": safe_configuration,
    }

    # Recompute every derived source/runtime digest before the final Git check.
    # This turns concurrent edit-and-restore races into a refusal rather than a
    # mixed baseline assembled from different observations.
    verification_runtime_evidence = _fresh_runtime_evidence(root)
    verification_private_tools = verification_runtime_evidence["private_tools"]
    verification_public_tools = verification_runtime_evidence["public_tools"]
    verification_public_status = verification_runtime_evidence["public_status"]
    verification_safe_configuration = {
        "public_presence_enabled": verification_public_status["enabled"],
        "public_mode": verification_public_status["mode"],
        "public_running": verification_public_status["running"],
        "external_communication": verification_public_status["external_communication"],
        "connected_platform_count": len(
            verification_public_status["connected_platforms"]
        ),
        "publishing_method_count": 0,
    }
    verification_digests = {
        "runtime_manifest_sha256": verification_runtime_evidence[
            "runtime_manifest_sha256"
        ],
        "release_tree_content_sha256": _bundle_sha256(root, tracked),
        "migration_set_sha256": _bundle_sha256(root, _MIGRATION_SOURCE_FILES),
        "identity_policy_bundle_sha256": _bundle_sha256(
            root, _IDENTITY_POLICY_FILES
        ),
        "redacted_configuration_sha256": _canonical_sha256(
            verification_safe_configuration
        ),
    }
    if (
        verification_digests != baseline["digests"]
        or verification_safe_configuration != safe_configuration
        or _canonical_sha256(verification_private_tools)
        != baseline["private_tool_manifest"]["sha256"]
        or len(verification_private_tools)
        != baseline["private_tool_manifest"]["count"]
        or _canonical_sha256(verification_public_tools)
        != baseline["public_tool_manifest"]["sha256"]
        or len(verification_public_tools)
        != baseline["public_tool_manifest"]["count"]
    ):
        raise BaselineError("release evidence changed during baseline capture")
    final_commit, final_tracked = _repository_snapshot(
        root, require_clean=require_clean
    )
    if final_commit != commit or final_tracked != tracked:
        raise BaselineError("repository changed during baseline capture")
    return baseline


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build a sanitized exact-revision JARVIS Phase 0 baseline"
    )
    parser.add_argument("--repo", type=Path, default=PROJECT_ROOT)
    parser.add_argument(
        "--runtime-evidence",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    args = parser.parse_args(argv)
    if args.runtime_evidence:
        print(
            json.dumps(
                _runtime_evidence_in_process(),
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            )
        )
        return 0
    try:
        baseline = collect_baseline(args.repo)
    except (BaselineError, OSError, UnicodeError, ValueError) as exc:
        print(f"PHASE 0 BASELINE ERROR: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(baseline, ensure_ascii=True, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
