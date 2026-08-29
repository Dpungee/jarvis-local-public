#!/usr/bin/env python3
"""Fail closed when a proposed public Git release contains private artifacts.

The check inspects reachable commit/tag metadata, the stage-0 Git index (the
exact snapshot a commit would publish), and corresponding tracked working-tree
files. Ignored and untracked local files are deliberately out of scope until
they are staged.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from jarvis.trusted_executables import trusted_path_executable


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
_ALLOWED_EMAIL_ADDRESSES = {
    "git@github.com",
    "noreply@github.com",
    # Dependabot adds this exact public GitHub-managed signoff to its commits.
    # Keep the exception address-specific so ordinary github.com mailboxes are
    # still rejected by the public-release privacy boundary.
    "support@github.com",
}
_HISTORY_REF_RE = re.compile(r"(?:HEAD|[0-9a-fA-F]{40})\Z")


def _resolve_trusted_git(repo: Path) -> Path:
    executable = trusted_path_executable(
        "git.exe" if sys.platform == "win32" else "git",
        prohibited_roots=(repo,),
    )
    if executable is None:
        raise RuntimeError("a trusted OS-administered Git executable is unavailable")
    return executable


def _git(
    git_executable: Path,
    repo: Path,
    *args: str,
    input_bytes: bytes | None = None,
) -> bytes:
    completed = subprocess.run(
        [str(git_executable), *args],
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


def _indexed_files(git_executable: Path, repo: Path) -> list[tuple[str, str, str]]:
    records = _git(git_executable, repo, "ls-files", "--stage", "-z").split(b"\0")
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
                findings.append(f"concrete {label}")

    for match in _EMAIL_RE.finditer(text):
        address = match.group(0).casefold()
        domain = address.rsplit("@", 1)[1]
        is_reserved_example = domain == "example" or domain.endswith(".example")
        if (
            address not in _ALLOWED_EMAIL_ADDRESSES
            and domain not in _ALLOWED_EMAIL_DOMAINS
            and not is_reserved_example
        ):
            findings.append("non-example email address")

    return sorted(set(findings))


def _validated_history_commit(
    git_executable: Path, repo: Path, history_ref: str
) -> str:
    candidate = str(history_ref).strip()
    if _HISTORY_REF_RE.fullmatch(candidate) is None:
        raise ValueError("history ref must be HEAD or a full 40-character commit ID")
    commit_id = _git(
        git_executable, repo, "rev-parse", "--verify", f"{candidate}^{{commit}}"
    ).decode(
        "ascii", errors="strict"
    ).strip()
    ancestor = subprocess.run(
        [str(git_executable), "merge-base", "--is-ancestor", commit_id, "HEAD"],
        cwd=repo,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        check=False,
    )
    if ancestor.returncode != 0:
        raise ValueError("history ref must be an ancestor of the checked-out snapshot")
    return commit_id


def _validated_history_base(
    git_executable: Path,
    repo: Path,
    history_base: str,
    history_commit: str,
) -> str:
    candidate = str(history_base).strip()
    if re.fullmatch(r"[0-9a-fA-F]{40}", candidate) is None:
        raise ValueError("history base must be a full 40-character commit ID")
    if set(candidate) == {"0"}:
        raise ValueError("history base may not be the all-zero Git sentinel")
    base_commit = _git(
        git_executable, repo, "rev-parse", "--verify", f"{candidate}^{{commit}}"
    ).decode(
        "ascii", errors="strict"
    ).strip()
    if base_commit == history_commit:
        raise ValueError("history base must precede the history ref")
    ancestor = subprocess.run(
        [
            str(git_executable),
            "merge-base",
            "--is-ancestor",
            base_commit,
            history_commit,
        ],
        cwd=repo,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        check=False,
    )
    if ancestor.returncode != 0:
        raise ValueError("history base must be an ancestor of the history ref")

    head_commit = _git(
        git_executable, repo, "rev-parse", "--verify", "HEAD^{commit}"
    ).decode("ascii", errors="strict").strip()
    if head_commit != history_commit:
        raise ValueError("ranged history ref must equal the checked-out HEAD")

    parent_record = _git(
        git_executable,
        repo,
        "rev-list",
        "--parents",
        "-n",
        "1",
        history_commit,
    ).decode("ascii", errors="strict").strip().split()
    if len(parent_record) != 2 or parent_record[1] != base_commit:
        raise ValueError(
            "public release range must contain exactly one reviewed non-merge commit"
        )

    history_tree = _git(
        git_executable, repo, "rev-parse", f"{history_commit}^{{tree}}"
    ).decode("ascii", errors="strict").strip()
    index_tree = _git(git_executable, repo, "write-tree").decode(
        "ascii", errors="strict"
    ).strip()
    if index_tree != history_tree:
        raise ValueError("Git index must exactly match the ranged history ref")
    return base_commit


def _history_identity_findings(
    git_executable: Path,
    repo: Path,
    history_ref: str = "HEAD",
    history_base: str | None = None,
) -> list[str]:
    findings: list[str] = []
    if history_base is not None and re.fullmatch(
        r"[0-9a-fA-F]{40}", str(history_ref).strip()
    ) is None:
        raise ValueError(
            "ranged history ref must be a full 40-character commit ID"
        )
    history_commit = _validated_history_commit(git_executable, repo, history_ref)
    revision = history_commit
    if history_base is not None:
        base_commit = _validated_history_base(
            git_executable,
            repo,
            history_base,
            history_commit,
        )
        revision = f"{base_commit}..{history_commit}"
    history = _git(
        git_executable,
        repo,
        "log",
        revision,
        "--format=%H%x1f%an%x1f%ae%x1f%cn%x1f%ce%x1e",
    )
    for raw_record in history.split(b"\x1e"):
        record = raw_record.strip(b"\r\n")
        if not record:
            continue
        fields = record.decode("utf-8", errors="replace").split("\x1f")
        if len(fields) != 5:
            raise RuntimeError("Git returned malformed commit identity metadata")
        commit_id, author_name, author_email, committer_name, committer_email = fields
        for role, name, email in (
            ("author", author_name, author_email),
            ("committer", committer_name, committer_email),
        ):
            for reason in _content_findings(f"{name} <{email}>"):
                findings.append(
                    f"commit {commit_id} {role} identity: {reason}"
                )
        message = _git(
            git_executable, repo, "show", "-s", "--format=%B", commit_id
        ).decode(
            "utf-8", errors="replace"
        )
        for reason in _content_findings(message):
            findings.append(f"commit {commit_id} message: {reason}")

    tags = _git(
        git_executable,
        repo,
        "tag",
        "--merged",
        history_commit,
        "--format=%(refname)%00%(taggername)%00%(taggeremail)",
    )
    for raw_record in tags.splitlines():
        fields = raw_record.decode("utf-8", errors="replace").split("\0")
        if len(fields) != 3:
            raise RuntimeError("Git returned malformed tag identity metadata")
        refname, tagger_name, tagger_email = fields
        if tagger_email:
            for reason in _content_findings(f"{tagger_name} {tagger_email}"):
                findings.append(f"{refname} tagger identity: {reason}")
        message = _git(
            git_executable, repo, "for-each-ref", "--format=%(contents)", refname
        ).decode(
            "utf-8", errors="replace"
        )
        for reason in _content_findings(message):
            findings.append(f"{refname} message: {reason}")
    return sorted(set(findings))


def check_release(
    repo: Path,
    *,
    history_ref: str = "HEAD",
    history_base: str | None = None,
) -> list[str]:
    git_executable = _resolve_trusted_git(repo)
    findings: list[str] = _history_identity_findings(
        git_executable,
        repo,
        history_ref,
        history_base,
    )
    for path, object_id, mode in _indexed_files(git_executable, repo):
        for reason in _path_findings(path):
            findings.append(f"{path}: {reason}")

        if mode not in {"100644", "100755"}:
            findings.append(
                f"{path}: tracked mode {mode} is not a regular file; "
                "symlinks and submodules are blocked"
            )
            continue

        indexed = _git(git_executable, repo, "cat-file", "blob", object_id)
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
    parser.add_argument(
        "--history-ref",
        default="HEAD",
        help=(
            "reachable history commit to inspect; CI pull requests should pass the "
            "full head commit instead of GitHub's synthetic merge commit"
        ),
    )
    parser.add_argument(
        "--history-base",
        default=None,
        help=(
            "trusted full commit ID immediately before the proposed release range; "
            "the base must be an ancestor of --history-ref"
        ),
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    repo = args.repo.resolve()
    try:
        findings = check_release(
            repo,
            history_ref=args.history_ref,
            history_base=args.history_base,
        )
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
    print(
        "No blocked paths, concrete user-home paths, non-example emails, "
        "or private Git identities were found."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
