#!/usr/bin/env python3
"""Fail closed when a proposed public Git release contains private artifacts.

The check inspects reachable commit/tag metadata, the stage-0 Git index (the
exact snapshot a commit would publish), and corresponding tracked working-tree
files. Ignored and untracked local files are deliberately out of scope until
they are staged.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from jarvis.redaction import (
    contains_obfuscated_secret,
    normalize_private_identifier_text,
    private_email_addresses,
    private_identifier_text_was_obfuscated,
)
from jarvis.trusted_executables import trusted_path_executable


MAX_TRACKED_FILE_BYTES = 5 * 1024 * 1024

_ALLOWED_PLACEHOLDER_USERS = {
    "[user]",
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

_ALLOWED_PLACEHOLDER_HOSTS = {
    "[host]",
    "example",
    "example-server",
    "server",
    "test",
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
    r"(?i)\b[A-Z]:(?:[\\/]+)Users(?:[\\/]+)([^\\/:\"'<>|\r\n\t]+)"
)
_POSIX_HOME_RE = re.compile(
    r"(?i)(?:^|[^\w])/(?:home|Users)/([^/\"'<>|\r\n\t]+)"
)
_UNC_HOME_RE = re.compile(
    r"(?i)(?:^|[^\w])(?:\\\\|//)([^\\/\s:\"'<>|]+)[\\/]"
    r"+(?:Users|home|homes)[\\/]+([^\\/:\"'<>|\r\n\t]+)"
)
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
_FULL_COMMIT_RE = re.compile(r"[0-9a-fA-F]{40}\Z")


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
    path_was_obfuscated = private_identifier_text_was_obfuscated(path)
    normalized = str(PurePosixPath(normalize_private_identifier_text(path)))
    parts = PurePosixPath(normalized).parts
    if not parts:
        return ["empty tracked path"]

    findings: list[str] = []
    if path_was_obfuscated or any(
        not character.isascii() for character in normalized
    ):
        findings.append("Unicode-obfuscated tracked path is not publishable")
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
    if b"\0" in data:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _content_findings(text: str) -> list[str]:
    text_was_obfuscated = private_identifier_text_was_obfuscated(text)
    obfuscated_secret = contains_obfuscated_secret(text)
    text = normalize_private_identifier_text(text)
    findings: list[str] = []

    if obfuscated_secret:
        # Report only the finding class. Never echo matched secret material to
        # release logs, CI annotations, or terminal output. Ordinary ASCII
        # credentials remain the dedicated secret scanner's responsibility;
        # this closes the Unicode-normalization gap without classifying source
        # examples and placeholder configuration as live credentials.
        findings.append("Unicode-obfuscated credential or secret material")

    for pattern, label in (
        (_WINDOWS_HOME_RE, "Windows user-home path"),
        (_POSIX_HOME_RE, "POSIX user-home path"),
    ):
        for match in pattern.finditer(text):
            username = match.group(1).casefold().rstrip(".,;:)")
            if (
                username not in _ALLOWED_PLACEHOLDER_USERS
                or text_was_obfuscated
            ):
                findings.append(f"concrete {label}")

    for match in _UNC_HOME_RE.finditer(text):
        hostname = match.group(1).casefold().rstrip(".,;:)")
        username = match.group(2).casefold().rstrip(".,;:)")
        if (
            hostname not in _ALLOWED_PLACEHOLDER_HOSTS
            or username not in _ALLOWED_PLACEHOLDER_USERS
            or text_was_obfuscated
        ):
            findings.append("concrete UNC user-home path")

    for candidate in private_email_addresses(text):
        address = candidate.casefold()
        local_part, domain = address.rsplit("@", 1)
        final_label = domain.rsplit(".", 1)[-1]
        if re.fullmatch(r"\{[a-z0-9_-]+\}", local_part) is not None:
            continue
        if not (final_label.isalpha() or final_label.startswith("xn--")):
            # Package versions and IPv4 user-info look email-shaped but do not
            # have a syntactically plausible alphabetic/IDN top-level label.
            continue
        is_reserved_example = domain == "example" or domain.endswith(".example")
        if (
            address not in _ALLOWED_EMAIL_ADDRESSES
            and domain not in _ALLOWED_EMAIL_DOMAINS
            and not is_reserved_example
        ):
            findings.append("non-example email address")
        elif text_was_obfuscated:
            # Canonicalization is for detection only. It must never transform
            # an attacker-controlled lookalike into an allowlisted identity.
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


def _commit_structure(
    git_executable: Path,
    repo: Path,
    commit_id: str,
) -> tuple[str, tuple[str, ...], bytes, bytes, tuple[bytes, ...], bytes]:
    """Return privacy-safe comparable parts of one raw commit object."""

    raw = _git(git_executable, repo, "cat-file", "-p", commit_id)
    header_block, separator, message = raw.partition(b"\n\n")
    if not separator:
        raise ValueError("history rewrite contains a malformed commit object")

    records: list[bytes] = []
    for line in header_block.splitlines():
        if line.startswith(b" "):
            if not records:
                raise ValueError("history rewrite contains a malformed commit header")
            records[-1] += b"\n" + line
        else:
            records.append(line)

    keys = [record.partition(b" ")[0] for record in records]
    cursor = 0
    if not keys or keys[cursor] != b"tree":
        raise ValueError("history rewrite contains non-canonical commit headers")
    cursor += 1
    while cursor < len(keys) and keys[cursor] == b"parent":
        cursor += 1
    if cursor >= len(keys) or keys[cursor] != b"author":
        raise ValueError("history rewrite contains non-canonical commit headers")
    cursor += 1
    if cursor >= len(keys) or keys[cursor] != b"committer":
        raise ValueError("history rewrite contains non-canonical commit headers")
    cursor += 1
    if any(key in {b"tree", b"parent", b"author", b"committer"} for key in keys[cursor:]):
        raise ValueError("history rewrite contains non-canonical commit headers")

    trees: list[str] = []
    parents: list[str] = []
    authors: list[bytes] = []
    committers: list[bytes] = []
    extras: list[bytes] = []
    for record in records:
        key, separator, value = record.partition(b" ")
        if not separator:
            raise ValueError("history rewrite contains a malformed commit header")
        if key == b"tree":
            trees.append(value.decode("ascii", errors="strict"))
        elif key == b"parent":
            parents.append(value.decode("ascii", errors="strict"))
        elif key == b"author":
            authors.append(value)
        elif key == b"committer":
            committers.append(value)
        else:
            extras.append(record)
    if len(trees) != 1 or len(authors) != 1 or len(committers) != 1:
        raise ValueError("history rewrite contains malformed identity metadata")
    return (
        trees[0],
        tuple(parents),
        authors[0],
        committers[0],
        tuple(extras),
        message,
    )


def _identity_timestamp(identity: bytes) -> bytes:
    """Extract a commit identity timestamp without returning the identity value."""

    fields = identity.rsplit(b" ", 2)
    if (
        len(fields) != 3
        or re.fullmatch(rb"-?\d+", fields[1]) is None
        or re.fullmatch(rb"[+-]\d{4}", fields[2]) is None
    ):
        raise ValueError("history rewrite contains malformed identity timing")
    return fields[1] + b" " + fields[2]


def _identity_subject(identity: bytes) -> bytes:
    """Extract a name/mailbox pair without ever logging it."""

    fields = identity.rsplit(b" ", 2)
    if (
        len(fields) != 3
        or re.fullmatch(rb"-?\d+", fields[1]) is None
        or re.fullmatch(rb"[+-]\d{4}", fields[2]) is None
    ):
        raise ValueError("history rewrite contains malformed identity timing")
    return fields[0]


def _validated_history_rewrite_base(
    git_executable: Path,
    repo: Path,
    history_rewrite_base: str,
    history_commit: str,
    expected_common: str,
    expected_replacement_tip: str,
) -> str:
    """Prove a non-fast-forward rewrite preserved the replaced commit segment."""

    candidate = str(history_rewrite_base).strip()
    if _FULL_COMMIT_RE.fullmatch(candidate) is None:
        raise ValueError("history rewrite base must be a full 40-character commit ID")
    if set(candidate) == {"0"}:
        raise ValueError("history rewrite base may not be the all-zero Git sentinel")
    base_commit = _git(
        git_executable,
        repo,
        "rev-parse",
        "--verify",
        f"{candidate}^{{commit}}",
    ).decode("ascii", errors="strict").strip()
    head_commit = _git(
        git_executable, repo, "rev-parse", "--verify", "HEAD^{commit}"
    ).decode("ascii", errors="strict").strip()
    if head_commit != history_commit:
        raise ValueError("history rewrite ref must equal the checked-out HEAD")
    if _FULL_COMMIT_RE.fullmatch(expected_common) is None:
        raise ValueError("history rewrite common must be a full 40-character commit ID")
    if _FULL_COMMIT_RE.fullmatch(expected_replacement_tip) is None:
        raise ValueError("history rewrite tip must be a full 40-character commit ID")
    if _git(
        git_executable,
        repo,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
    ):
        raise ValueError("history replacement requires a clean exact checkout")
    object_topology_overrides = (
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_COMMON_DIR",
        "GIT_DIR",
        "GIT_GRAFT_FILE",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_REPLACE_REF_BASE",
        "GIT_SHALLOW_FILE",
        "GIT_WORK_TREE",
    )
    if any(os.environ.get(name) for name in object_topology_overrides):
        raise ValueError("history replacement rejects Git topology environment overrides")
    fsck = subprocess.run(
        [
            str(git_executable),
            "fsck",
            "--strict",
            "--no-reflogs",
            "--no-dangling",
            history_commit,
        ],
        cwd=repo,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if fsck.returncode != 0:
        raise ValueError("history replacement failed strict Git object validation")

    shallow = _git(
        git_executable, repo, "rev-parse", "--is-shallow-repository"
    ).decode("ascii", errors="strict").strip()
    if shallow != "false":
        raise ValueError("history replacement requires a complete non-shallow repository")
    if _git(git_executable, repo, "replace", "-l"):
        raise ValueError("history replacement rejects Git replace refs")
    graft_path_text = _git(
        git_executable, repo, "rev-parse", "--git-path", "info/grafts"
    ).decode("utf-8", errors="strict").strip()
    graft_path = Path(graft_path_text)
    if not graft_path.is_absolute():
        graft_path = repo / graft_path
    if graft_path.exists():
        raise ValueError("history replacement rejects Git grafts")
    partial = subprocess.run(
        [
            str(git_executable),
            "config",
            "--get-regexp",
            r"^(extensions\.partialclone|remote\..*\.promisor)$",
        ],
        cwd=repo,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if partial.returncode not in {0, 1}:
        raise ValueError("history replacement clone completeness could not be verified")
    if partial.stdout.strip():
        raise ValueError("history replacement rejects partial clones")

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
    if ancestor.returncode == 0:
        raise ValueError("history rewrite base is already an ancestor of the history ref")
    if ancestor.returncode != 1:
        raise ValueError("history rewrite ancestry could not be verified")
    rollback = subprocess.run(
        [
            str(git_executable),
            "merge-base",
            "--is-ancestor",
            history_commit,
            base_commit,
        ],
        cwd=repo,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        check=False,
    )
    if rollback.returncode == 0:
        raise ValueError("history replacement cannot roll public history backward")
    if rollback.returncode != 1:
        raise ValueError("history rewrite ancestry could not be verified")

    if _git(git_executable, repo, "rev-list", "--merges", history_commit):
        raise ValueError("history replacement must remain linear")
    roots = list(
        filter(
            None,
            _git(
                git_executable,
                repo,
                "rev-list",
                "--max-parents=0",
                history_commit,
            )
            .decode("ascii", errors="strict")
            .splitlines(),
        )
    )
    if len(roots) != 1:
        raise ValueError("history replacement must have exactly one root")

    base_tree = _git(
        git_executable, repo, "rev-parse", f"{base_commit}^{{tree}}"
    ).decode("ascii", errors="strict").strip()
    replacement_matches: list[str] = []
    new_history = _git(
        git_executable, repo, "rev-list", history_commit
    ).decode("ascii", errors="strict").splitlines()
    for commit_id in new_history:
        tree = _git(
            git_executable, repo, "rev-parse", f"{commit_id}^{{tree}}"
        ).decode("ascii", errors="strict").strip()
        if tree == base_tree:
            replacement_matches.append(commit_id)
    if len(replacement_matches) != 1:
        raise ValueError(
            "history replacement must contain exactly one tree-equivalent old-tip counterpart"
        )
    replacement_tip = replacement_matches[0]
    if replacement_tip != expected_replacement_tip.casefold():
        raise ValueError("history replacement tip does not match the reviewed commit")

    merge_bases = list(
        filter(
            None,
            _git(
                git_executable,
                repo,
                "merge-base",
                "--all",
                base_commit,
                replacement_tip,
            )
            .decode("ascii", errors="strict")
            .splitlines(),
        )
    )
    if len(merge_bases) != 1:
        raise ValueError("history replacement must have one unambiguous common ancestor")
    common = merge_bases[0]
    if common != expected_common.casefold():
        raise ValueError("history replacement common ancestor does not match review")
    old_commits = list(
        filter(
            None,
            _git(
                git_executable,
                repo,
                "rev-list",
                "--reverse",
                f"{common}..{base_commit}",
            )
            .decode("ascii", errors="strict")
            .splitlines(),
        )
    )
    new_commits = list(
        filter(
            None,
            _git(
                git_executable,
                repo,
                "rev-list",
                "--reverse",
                f"{common}..{replacement_tip}",
            )
            .decode("ascii", errors="strict")
            .splitlines(),
        )
    )
    if not old_commits or len(old_commits) != len(new_commits):
        raise ValueError("history replacement commit counts do not match")

    for index, (old_commit, new_commit) in enumerate(zip(old_commits, new_commits)):
        old_parts = _commit_structure(git_executable, repo, old_commit)
        new_parts = _commit_structure(git_executable, repo, new_commit)
        expected_old_parent = common if index == 0 else old_commits[index - 1]
        expected_new_parent = common if index == 0 else new_commits[index - 1]
        if old_parts[1] != (expected_old_parent,) or new_parts[1] != (
            expected_new_parent,
        ):
            raise ValueError("history replacement parent structure does not match")
        if old_parts[0] != new_parts[0]:
            raise ValueError("history replacement changed a source tree")
        if old_parts[2] != new_parts[2]:
            raise ValueError("history replacement changed author metadata")
        if _identity_timestamp(old_parts[3]) != _identity_timestamp(new_parts[3]):
            raise ValueError("history replacement changed committer timing")
        if _identity_subject(new_parts[3]) != _identity_subject(new_parts[2]):
            raise ValueError(
                "history replacement committer must match the approved author identity"
            )
        if old_parts[4] != new_parts[4]:
            raise ValueError("history replacement changed protected commit headers")
        if old_parts[5] != new_parts[5]:
            raise ValueError("history replacement changed a commit message")

    history_tree = _git(
        git_executable, repo, "rev-parse", f"{history_commit}^{{tree}}"
    ).decode("ascii", errors="strict").strip()
    index_tree = _git(git_executable, repo, "write-tree").decode(
        "ascii", errors="strict"
    ).strip()
    if index_tree != history_tree:
        raise ValueError("Git index must exactly match the history rewrite ref")
    return base_commit


def _history_identity_findings(
    git_executable: Path,
    repo: Path,
    history_ref: str = "HEAD",
    history_base: str | None = None,
    history_rewrite_base: str | None = None,
    history_rewrite_common: str | None = None,
    history_rewrite_tip: str | None = None,
) -> list[str]:
    findings: list[str] = []
    if history_base is not None and history_rewrite_base is not None:
        raise ValueError("history base modes are mutually exclusive")
    if history_rewrite_base is None and (
        history_rewrite_common is not None or history_rewrite_tip is not None
    ):
        raise ValueError("history rewrite pins require a history rewrite base")
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
    elif history_rewrite_base is not None:
        if _FULL_COMMIT_RE.fullmatch(str(history_ref).strip()) is None:
            raise ValueError(
                "history rewrite ref must be a full 40-character commit ID"
            )
        if history_rewrite_common is None or history_rewrite_tip is None:
            raise ValueError(
                "history rewrite requires exact common and rewritten-tip pins"
            )
        _validated_history_rewrite_base(
            git_executable,
            repo,
            history_rewrite_base,
            history_commit,
            history_rewrite_common,
            history_rewrite_tip,
        )
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


def _historical_tree_findings(
    git_executable: Path,
    repo: Path,
    history_commit: str,
    trusted_common: str,
) -> list[str]:
    """Scan every path/blob introduced after the pinned trusted common history."""

    findings: list[str] = []
    seen_entries: set[tuple[str, str, str]] = set()
    seen_blobs: set[str] = set()
    commits = _git(
        git_executable, repo, "rev-list", f"{trusted_common}..{history_commit}"
    ).decode("ascii", errors="strict").splitlines()
    for commit_id in commits:
        tree = _git(
            git_executable,
            repo,
            "ls-tree",
            "-r",
            "-z",
            "--full-tree",
            commit_id,
        )
        for record in tree.split(b"\0"):
            if not record:
                continue
            try:
                metadata, raw_path = record.split(b"\t", 1)
                mode, object_type, object_id = metadata.decode(
                    "ascii", errors="strict"
                ).split()
                path = raw_path.decode(
                    "utf-8", errors="surrogateescape"
                ).replace("\\", "/")
            except (UnicodeError, ValueError) as error:
                raise RuntimeError("Git returned a malformed historical tree") from error
            entry = (path, object_id, mode)
            if entry in seen_entries:
                continue
            seen_entries.add(entry)
            if _path_findings(path):
                findings.append(
                    f"commit {commit_id} historical tracked path violates publishability rules"
                )
            if object_type != "blob" or mode not in {"100644", "100755"}:
                findings.append(
                    f"commit {commit_id} historical entry is not a regular file"
                )
                continue
            if object_id in seen_blobs:
                continue
            seen_blobs.add(object_id)
            blob = _git(git_executable, repo, "cat-file", "blob", object_id)
            if len(blob) > MAX_TRACKED_FILE_BYTES:
                findings.append(
                    f"commit {commit_id} historical tracked file exceeds the size limit"
                )
                continue
            text = _decode_text(blob)
            if text is None:
                findings.append(
                    f"commit {commit_id} historical tracked content is not publishable text"
                )
                continue
            for reason in _content_findings(text):
                findings.append(
                    f"commit {commit_id} historical tracked content: {reason}"
                )
    return sorted(set(findings))


def check_release(
    repo: Path,
    *,
    history_ref: str = "HEAD",
    history_base: str | None = None,
    history_rewrite_base: str | None = None,
    history_rewrite_common: str | None = None,
    history_rewrite_tip: str | None = None,
) -> list[str]:
    git_executable = _resolve_trusted_git(repo)
    findings: list[str] = _history_identity_findings(
        git_executable,
        repo,
        history_ref,
        history_base,
        history_rewrite_base,
        history_rewrite_common,
        history_rewrite_tip,
    )
    if history_rewrite_base is not None:
        assert history_rewrite_common is not None
        history_commit = _validated_history_commit(
            git_executable,
            repo,
            history_ref,
        )
        findings.extend(
            _historical_tree_findings(
                git_executable,
                repo,
                history_commit,
                history_rewrite_common,
            )
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
            if indexed_text is None:
                findings.append(
                    f"{path} [index]: non-UTF-8 or NUL-containing tracked content "
                    "is blocked"
                )
            else:
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
                    if worktree_text is None:
                        findings.append(
                            f"{path} [worktree]: non-UTF-8 or NUL-containing "
                            "tracked content is blocked"
                        )
                    else:
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
    parser.add_argument(
        "--history-rewrite-base",
        default=None,
        help=(
            "full pre-rewrite public tip; proves the divergent segment is tree-, "
            "message-, author-, and timestamp-equivalent before scanning reachable "
            "metadata and all divergent history after the pinned common ancestor"
        ),
    )
    parser.add_argument(
        "--history-rewrite-common",
        default=None,
        help="exact reviewed common ancestor for a history replacement",
    )
    parser.add_argument(
        "--history-rewrite-tip",
        default=None,
        help="exact reviewed rewritten counterpart of the old public tip",
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
            history_rewrite_base=args.history_rewrite_base,
            history_rewrite_common=args.history_rewrite_common,
            history_rewrite_tip=args.history_rewrite_tip,
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
