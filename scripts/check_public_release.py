#!/usr/bin/env python3
"""Fail closed when a proposed public Git snapshot contains private artifacts.

The check inspects the stage-0 Git index (the exact snapshot a commit would
publish) and the corresponding tracked working-tree files. Ignored and
untracked local files are deliberately out of scope until they are staged.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath


MAX_TRACKED_FILE_BYTES = 5 * 1024 * 1024

_ALLOWED_PLACEHOLDER_USERS = {
    "codex",
    "example",
    "example-user",
    "exampleuser",
    "operator",
    "public",
    "runneradmin",
    "test",
    "tester",
    "user",
    "username",
    "victim",
    "your-name",
}

_DISALLOWED_TOP_LEVEL_DIRECTORIES = {
    ".secrets",
    "backups",
    "codex-queue",
    "reports",
    "workspace-projects",
}

_RUNTIME_DIRECTORIES_WITH_PLACEHOLDERS = {"data", "workspace"}

_DISALLOWED_SUFFIXES = {
    ".db",
    ".db-shm",
    ".db-wal",
    ".der",
    ".docx",
    ".jks",
    ".key",
    ".keystore",
    ".log",
    ".p12",
    ".pdf",
    ".pem",
    ".pfx",
    ".sqlite",
    ".sqlite3",
    ".wal",
    ".xls",
    ".xlsm",
    ".xlsx",
}

_DISALLOWED_EXACT_NAMES = {
    ".env",
    ".netrc",
    ".npmrc",
    ".pypirc",
    "id_ed25519",
    "id_rsa",
}

_CREDENTIAL_FILE_RE = re.compile(
    r"(?i)^(?:client[_-]?secret|credentials?|oauth[_-]?token|refresh[_-]?token|token)(?:[._-].*)?\.json$"
)
_ENV_FILE_RE = re.compile(r"(?i)^\.env(?:\..+)?$")
_CODEX_TRANSCRIPT_RE = re.compile(r"(?i)^jarvis-codex-.*\.(?:schema|response)\.json$")

_WINDOWS_HOME_RE = re.compile(
    r"(?i)\b[A-Z]:(?:[\\/]+)Users(?:[\\/]+)([^\\/\s\"'<>]+)"
)
_POSIX_HOME_RE = re.compile(r"(?i)(?:^|[^\w])/(?:home|Users)/([^/\s\"'<>]+)")
_EMAIL_RE = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")

_ALLOWED_EMAIL_DOMAINS = {
    "example.com",
    "example.net",
    "example.org",
    "users.noreply.github.com",
}
_ALLOWED_EMAIL_ADDRESSES = {"git@github.com"}


def _git(repo: Path, *args: str, input_bytes: bytes | None = None) -> bytes:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo,
        input=input_bytes,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"git {' '.join(args)} failed: {detail}")
    return completed.stdout


def _indexed_files(repo: Path) -> list[tuple[str, str, str]]:
    records = _git(repo, "ls-files", "--stage", "-z").split(b"\0")
    files: list[tuple[str, str, str]] = []
    for record in records:
        if not record:
            continue
        metadata, raw_path = record.split(b"\t", 1)
        mode, object_id, stage = metadata.decode("ascii").split()
        if stage != "0":
            raise RuntimeError(
                "the Git index contains unresolved merge entries; public release is blocked"
            )
        path = raw_path.decode("utf-8", errors="surrogateescape").replace("\\", "/")
        files.append((path, object_id, mode))
    return sorted(files)


def _path_findings(path: str) -> list[str]:
    normalized = str(PurePosixPath(path))
    parts = PurePosixPath(normalized).parts
    if not parts:
        return ["empty tracked path"]

    findings: list[str] = []
    top = parts[0].casefold()
    name = parts[-1].casefold()

    if top in _DISALLOWED_TOP_LEVEL_DIRECTORIES:
        findings.append(f"private/generated directory is not publishable: {parts[0]}/")
    if top in _RUNTIME_DIRECTORIES_WITH_PLACEHOLDERS and not (
        len(parts) == 2 and parts[1] == ".gitkeep"
    ):
        findings.append(f"runtime directory may contain local state: {parts[0]}/")
    credential_parts = {part.casefold() for part in parts[:-1]}
    for credential_directory in (".aws", ".secrets", ".ssh"):
        if credential_directory in credential_parts:
            findings.append(
                f"credential directory is not publishable: {credential_directory}/"
            )

    if name in _DISALLOWED_EXACT_NAMES:
        findings.append(f"credential or local configuration filename: {parts[-1]}")
    if _ENV_FILE_RE.fullmatch(name) and name != ".env.example":
        findings.append("only .env.example may be tracked")
    if _CREDENTIAL_FILE_RE.fullmatch(name):
        findings.append(f"credential-shaped JSON filename: {parts[-1]}")
    if _CODEX_TRANSCRIPT_RE.fullmatch(name):
        findings.append("generated Codex transcript/schema file")

    lower_path = normalized.casefold()
    for suffix in _DISALLOWED_SUFFIXES:
        if lower_path.endswith(suffix):
            findings.append(f"private/generated file type is not publishable: {suffix}")
            break
    return findings


def _decode_text(data: bytes) -> str | None:
    if b"\0" in data[:8192]:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _content_findings(text: str) -> list[str]:
    findings: list[str] = []

    for pattern, label in (
        (_WINDOWS_HOME_RE, "Windows user-home path"),
        (_POSIX_HOME_RE, "POSIX user-home path"),
    ):
        for match in pattern.finditer(text):
            username = match.group(1).casefold().rstrip(".,;:)")
            if username not in _ALLOWED_PLACEHOLDER_USERS:
                findings.append(f"concrete {label} for user {username!r}")

    for match in _EMAIL_RE.finditer(text):
        address = match.group(0).casefold()
        domain = address.rsplit("@", 1)[1]
        is_reserved_example = domain == "example" or domain.endswith(".example")
        if (
            address not in _ALLOWED_EMAIL_ADDRESSES
            and domain not in _ALLOWED_EMAIL_DOMAINS
            and not is_reserved_example
        ):
            findings.append(f"non-example email address: {address}")

    return sorted(set(findings))


def check_release(repo: Path) -> list[str]:
    findings: list[str] = []
    for path, object_id, mode in _indexed_files(repo):
        for reason in _path_findings(path):
            findings.append(f"{path}: {reason}")

        if mode not in {"100644", "100755"}:
            findings.append(
                f"{path}: tracked mode {mode} is not a regular file; "
                "symlinks and submodules are blocked"
            )
            continue

        indexed = _git(repo, "cat-file", "blob", object_id)
        if len(indexed) > MAX_TRACKED_FILE_BYTES:
            findings.append(
                f"{path}: tracked file is {len(indexed)} bytes; limit is "
                f"{MAX_TRACKED_FILE_BYTES} bytes"
            )
        else:
            indexed_text = _decode_text(indexed)
            if indexed_text is not None:
                for reason in _content_findings(indexed_text):
                    findings.append(f"{path} [index]: {reason}")

        worktree_path = repo.joinpath(*PurePosixPath(path).parts)
        if worktree_path.is_file():
            worktree = worktree_path.read_bytes()
            if worktree != indexed:
                if len(worktree) > MAX_TRACKED_FILE_BYTES:
                    findings.append(
                        f"{path} [worktree]: file is {len(worktree)} bytes; limit is "
                        f"{MAX_TRACKED_FILE_BYTES} bytes"
                    )
                else:
                    worktree_text = _decode_text(worktree)
                    if worktree_text is not None:
                        for reason in _content_findings(worktree_text):
                            findings.append(f"{path} [worktree]: {reason}")

    return sorted(set(findings))


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Check the proposed Git snapshot for public-release privacy risks."
    )
    parser.add_argument(
        "--repo",
        type=Path,
        default=Path.cwd(),
        help="repository root (default: current directory)",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    repo = args.repo.resolve()
    try:
        findings = check_release(repo)
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"PUBLIC RELEASE CHECK ERROR: {exc}", file=sys.stderr)
        return 2

    if findings:
        print("PUBLIC RELEASE CHECK FAILED")
        for finding in findings:
            print(f"- {finding}")
        print(f"\n{len(findings)} blocking finding(s).")
        return 1

    print("PUBLIC RELEASE CHECK PASSED")
    print("No blocked paths, concrete user-home paths, or non-example emails were found.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
