from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from .redaction import contains_secret
from .subprocess_env import trusted_cli_environment


DEFAULT_TIMEOUT_SECONDS = 120.0
MAX_TIMEOUT_SECONDS = 600.0
DEFAULT_MAX_OUTPUT_BYTES = 64 * 1024
MAX_OUTPUT_BYTES = 1024 * 1024
MAX_REPOSITORIES = 100

_OWNER_PATTERN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})\Z")
_REPOSITORY_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}\Z")
_REMOTE_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_BRANCH_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,254}\Z")
_OBJECT_ID_PATTERN = re.compile(r"[0-9a-fA-F]{40,64}\Z")
_TOKEN_PATTERN = re.compile(
    r"(?i)\b(?:github_pat_[A-Za-z0-9_]{12,}|gh[pousr]_[A-Za-z0-9_]{12,})\b"
)
_CREDENTIAL_URL_PATTERN = re.compile(r"(?i)(https?://)[^\s/@:]+:[^\s/@]+@")
_URL_USERINFO_PATTERN = re.compile(r"(?i)https?://[^/\s]+@")
_GITHUB_HTTPS_REMOTE = re.compile(
    r"https://github\.com/[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/"
    r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}(?:\.git)?\Z",
    re.I,
)
_GITHUB_SCP_REMOTE = re.compile(
    r"git@github\.com:[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/"
    r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}(?:\.git)?\Z",
    re.I,
)
_GITHUB_SSH_REMOTE = re.compile(
    r"ssh://git@github\.com/[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/"
    r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}(?:\.git)?\Z",
    re.I,
)

Runner = Callable[..., subprocess.CompletedProcess[Any]]
Which = Callable[[str], str | None]


@dataclass(frozen=True)
class GitHubResult:
    """Bounded, serialization-friendly result returned by every provider operation."""

    operation: str
    ok: bool
    data: dict[str, Any] | list[dict[str, Any]] | None = None
    error: str | None = None
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    truncated: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "operation": self.operation,
            "ok": self.ok,
            "data": self.data,
            "error": self.error,
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "timed_out": self.timed_out,
            "truncated": self.truncated,
        }


@dataclass(frozen=True)
class _CommandResult:
    ok: bool
    exit_code: int | None
    stdout: str
    stderr: str
    error: str | None
    timed_out: bool
    truncated: bool


class GitHubProvider:
    """Non-interactive wrapper around the official ``gh`` and ``git`` CLIs.

    The provider deliberately has no login, delete, force-push, arbitrary command,
    or arbitrary environment interface. Callers must pass a repository path under
    ``workspace_root`` for every operation that can inspect or mutate local Git data.
    """

    def __init__(
        self,
        workspace_root: str | os.PathLike[str],
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
        runner: Runner | None = None,
        which: Which | None = None,
    ) -> None:
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)):
            raise TypeError("timeout_seconds must be a number")
        if not 0 < float(timeout_seconds) <= MAX_TIMEOUT_SECONDS:
            raise ValueError(f"timeout_seconds must be between 0 and {MAX_TIMEOUT_SECONDS:g}")
        if isinstance(max_output_bytes, bool) or not isinstance(max_output_bytes, int):
            raise TypeError("max_output_bytes must be an integer")
        if not 1024 <= max_output_bytes <= MAX_OUTPUT_BYTES:
            raise ValueError(
                f"max_output_bytes must be between 1024 and {MAX_OUTPUT_BYTES}"
            )

        root = _path_value(workspace_root, "workspace_root")
        try:
            resolved_root = root.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise ValueError("workspace_root must be an existing directory") from exc
        if not resolved_root.is_dir():
            raise ValueError("workspace_root must be an existing directory")
        if resolved_root == Path(resolved_root.anchor):
            raise ValueError("workspace_root must not be a filesystem root")

        executable_finder = which or shutil.which
        self.workspace_root = resolved_root
        self.timeout_seconds = float(timeout_seconds)
        self.max_output_bytes = max_output_bytes
        self._runner = runner or subprocess.run
        self._executables = {
            "gh": executable_finder("gh"),
            "git": executable_finder("git"),
        }

    def cli_status(self) -> GitHubResult:
        """Report whether both required official CLIs are discoverable."""
        data = {
            name: {"available": path is not None, "path": path}
            for name, path in self._executables.items()
        }
        missing = [name for name, path in self._executables.items() if path is None]
        return GitHubResult(
            operation="cli_status",
            ok=not missing,
            data=data,
            error=(f"Required CLI not found: {', '.join(missing)}" if missing else None),
        )

    def auth_status(self) -> GitHubResult:
        """Check the active github.com account without exposing or changing its token."""
        command = self._run(
            "gh",
            ["auth", "status", "--active", "--hostname", "github.com"],
            cwd=self.workspace_root,
        )
        return self._public_result(
            "auth_status",
            command,
            data={"hostname": "github.com", "authenticated": command.ok},
        )

    def repository_status(
        self,
        repository_path: str | os.PathLike[str],
    ) -> GitHubResult:
        """Return a bounded porcelain status for one contained repository root."""
        repository = self._repository_path(repository_path)
        command = self._run(
            "git",
            ["status", "--porcelain=v1", "--branch", "--untracked-files=normal"],
            cwd=repository,
        )
        lines = command.stdout.splitlines() if command.ok else []
        branch = None
        if lines and lines[0].startswith("## "):
            branch = lines.pop(0)[3:].strip()
        data = {
            "repository_path": str(repository),
            "branch": branch,
            "clean": command.ok and not lines,
            "changes": lines[:500],
            "complete": command.ok and not command.truncated,
        }
        return self._public_result("repository_status", command, data=data)

    def list_repositories(
        self,
        owner: str | None = None,
        *,
        limit: int = 30,
    ) -> GitHubResult:
        """List at most 100 repositories visible to the authenticated account."""
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise TypeError("limit must be an integer")
        if not 1 <= limit <= MAX_REPOSITORIES:
            raise ValueError(f"limit must be between 1 and {MAX_REPOSITORIES}")
        arguments = ["repo", "list"]
        if owner is not None:
            arguments.append(_validate_owner(owner))
        arguments.extend([
            "--limit",
            str(limit),
            "--json",
            (
                "name,nameWithOwner,description,url,visibility,isPrivate,"
                "isFork,isArchived,updatedAt"
            ),
        ])
        command = self._run("gh", arguments, cwd=self.workspace_root)
        if not command.ok:
            return self._public_result("list_repositories", command, data=[])
        if command.truncated:
            return self._public_result(
                "list_repositories",
                command,
                data=[],
                error="Repository list exceeded the provider output limit",
                ok=False,
            )
        try:
            payload = json.loads(command.stdout)
        except (json.JSONDecodeError, TypeError) as exc:
            return self._public_result(
                "list_repositories",
                command,
                data=[],
                error=f"gh returned invalid repository JSON: {type(exc).__name__}",
                ok=False,
            )
        if not isinstance(payload, list) or not all(isinstance(item, dict) for item in payload):
            return self._public_result(
                "list_repositories",
                command,
                data=[],
                error="gh returned an unexpected repository JSON shape",
                ok=False,
            )
        repositories = [dict(item) for item in payload[:limit]]
        return self._public_result("list_repositories", command, data=repositories)

    def create_repository(
        self,
        repository_path: str | os.PathLike[str],
        name: str,
        *,
        visibility: str = "private",
        description: str = "",
        remote: str = "origin",
        expected_approval_snapshot: dict[str, Any] | None = None,
    ) -> GitHubResult:
        """Create a remote for an existing local repository without pushing commits."""
        repository = self._repository_path(repository_path)
        slug = _validate_repository_slug(name)
        visibility_value = _text_value(visibility, "visibility").casefold()
        if visibility_value not in {"private", "public", "internal"}:
            raise ValueError("visibility must be private, public, or internal")
        remote_value = _validate_remote(remote)
        description_value = _validate_description(description)
        if expected_approval_snapshot is not None:
            if not isinstance(expected_approval_snapshot, dict):
                raise TypeError("expected_approval_snapshot must be a dictionary")
            current_snapshot = self.create_repository_approval_snapshot(
                repository,
                slug,
            )
            if current_snapshot != expected_approval_snapshot:
                raise PermissionError(
                    "GitHub account or repository destination changed after approval"
                )
            # Execute the approved fully-qualified owner/name, never the ambient
            # bare name that would be re-resolved by gh after the account check.
            slug = current_snapshot["repository_slug"]
        arguments = [
            "repo",
            "create",
            slug,
            f"--{visibility_value}",
            "--source",
            str(repository),
            "--remote",
            remote_value,
        ]
        if description_value:
            arguments.extend(["--description", description_value])
        command = self._run("gh", arguments, cwd=repository)
        return self._public_result(
            "create_repository",
            command,
            data={
                "repository_path": str(repository),
                "name": slug,
                "visibility": visibility_value,
                "remote": remote_value,
                "pushed": False,
            },
        )

    def create_repository_approval_snapshot(
        self,
        repository_path: str | os.PathLike[str],
        name: str,
    ) -> dict[str, Any]:
        """Resolve an unqualified repository name against the active GitHub login."""
        repository = self._repository_path(repository_path)
        slug = _validate_repository_slug(name)
        account_result = self._run(
            "gh",
            ["api", "--hostname", "github.com", "user", "--jq", ".login"],
            cwd=repository,
        )
        lines = [line.strip() for line in account_result.stdout.splitlines() if line.strip()]
        if (
            not account_result.ok
            or account_result.truncated
            or len(lines) != 1
        ):
            raise ValueError("The active GitHub login could not be resolved exactly")
        login = _validate_owner(lines[0])
        resolved_slug = slug if "/" in slug else f"{login}/{slug}"
        return {
            "resolved_path": str(repository),
            "authenticated_login": login,
            "repository_slug": resolved_slug,
        }

    def push(
        self,
        repository_path: str | os.PathLike[str],
        branch: str,
        *,
        remote: str = "origin",
        set_upstream: bool = True,
        expected_remote_url: str | None = None,
        expected_tip_sha: str | None = None,
    ) -> GitHubResult:
        """Push one explicit branch; force, mirror, tags, and arbitrary refspecs are unsupported."""
        repository = self._repository_path(repository_path)
        branch_value = _validate_branch(branch)
        remote_value = _validate_remote(remote)
        if not isinstance(set_upstream, bool):
            raise TypeError("set_upstream must be a boolean")
        if (expected_remote_url is None) != (expected_tip_sha is None):
            raise ValueError("Approved remote URL and branch tip must be supplied together")
        if expected_remote_url is not None:
            if (
                not isinstance(expected_remote_url, str)
                or not expected_remote_url
                or not isinstance(expected_tip_sha, str)
                or not _OBJECT_ID_PATTERN.fullmatch(expected_tip_sha)
            ):
                raise ValueError("Approved Git push snapshot is invalid")
            current = self.push_approval_snapshot(
                repository, branch_value, remote=remote_value
            )
            if (
                current["remote_url"] != expected_remote_url
                or current["tip_sha"] != expected_tip_sha.casefold()
            ):
                raise PermissionError(
                    "Git push destination or branch tip changed after approval"
                )
        arguments = ["push", "--no-verify"]
        if expected_remote_url is None:
            if set_upstream:
                arguments.append("--set-upstream")
            arguments.extend([remote_value, branch_value])
        else:
            # Use the approved concrete destination and object ID, not mutable
            # remote/branch aliases that Git would resolve again after the check.
            arguments.extend([
                expected_remote_url,
                f"{expected_tip_sha.casefold()}:refs/heads/{branch_value}",
            ])
        command = self._run("git", arguments, cwd=repository)
        upstream_configured = (
            command.ok if expected_remote_url is None and set_upstream else not set_upstream
        )
        if command.ok and set_upstream and expected_remote_url is not None:
            remote_config = self._run(
                "git",
                [
                    "config", "--local", "--replace-all",
                    f"branch.{branch_value}.remote", remote_value,
                ],
                cwd=repository,
            )
            merge_config = self._run(
                "git",
                [
                    "config", "--local", "--replace-all",
                    f"branch.{branch_value}.merge", f"refs/heads/{branch_value}",
                ],
                cwd=repository,
            )
            upstream_configured = remote_config.ok and merge_config.ok
        return self._public_result(
            "push",
            command,
            data={
                "repository_path": str(repository),
                "branch": branch_value,
                "remote": remote_value,
                "set_upstream": set_upstream,
                "upstream_configured": upstream_configured,
            },
        )

    def push_approval_snapshot(
        self,
        repository_path: str | os.PathLike[str],
        branch: str,
        *,
        remote: str = "origin",
    ) -> dict[str, Any]:
        """Resolve every push URL and the exact commit named by an explicit branch."""
        repository = self._repository_path(repository_path)
        branch_value = _validate_branch(branch)
        remote_value = _validate_remote(remote)
        urls_result = self._run(
            "git",
            ["remote", "get-url", "--push", "--all", remote_value],
            cwd=repository,
        )
        if not urls_result.ok or urls_result.truncated:
            raise ValueError("Git push remote could not be resolved exactly")
        remote_urls = [line.strip() for line in urls_result.stdout.splitlines() if line.strip()]
        if (
            len(remote_urls) != 1
            or any(
                len(value) > 160
                or "[redacted]" in value
                or _URL_USERINFO_PATTERN.search(value) is not None
                or contains_secret(value)
                or _unsafe_http_remote(value)
                or not _is_github_push_remote(value)
                or any(ord(character) < 32 or ord(character) == 127 for character in value)
                for value in remote_urls
            )
        ):
            raise ValueError(
                "Git push requires exactly one credential-free push destination"
            )
        tip_result = self._run(
            "git",
            ["rev-parse", "--verify", f"refs/heads/{branch_value}^{{commit}}"],
            cwd=repository,
        )
        tip_lines = [line.strip() for line in tip_result.stdout.splitlines() if line.strip()]
        if (
            not tip_result.ok
            or tip_result.truncated
            or len(tip_lines) != 1
            or not _OBJECT_ID_PATTERN.fullmatch(tip_lines[0])
        ):
            raise ValueError("Git push branch tip could not be resolved exactly")
        return {
            "resolved_path": str(repository),
            "branch": branch_value,
            "remote": remote_value,
            "remote_url": remote_urls[0],
            "tip_sha": tip_lines[0].casefold(),
        }

    def _repository_path(self, value: str | os.PathLike[str]) -> Path:
        candidate = _path_value(value, "repository_path")
        if not candidate.is_absolute():
            candidate = self.workspace_root / candidate
        try:
            resolved = candidate.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise ValueError("repository_path must be an existing directory") from exc
        if not _is_within(resolved, self.workspace_root):
            raise PermissionError("repository_path must stay inside workspace_root")
        if not resolved.is_dir():
            raise ValueError("repository_path must be an existing directory")
        self._validate_git_marker(resolved)
        return resolved

    def _validate_git_marker(self, repository: Path) -> None:
        marker = repository / ".git"
        if not os.path.lexists(marker):
            raise ValueError("repository_path must be the root of a Git repository")
        if marker.is_dir() or marker.is_symlink():
            try:
                target = marker.resolve(strict=True)
            except (OSError, RuntimeError) as exc:
                raise ValueError("repository .git marker is invalid") from exc
        elif marker.is_file():
            try:
                if marker.stat().st_size > 4096:
                    raise ValueError("repository .git marker is too large")
                first_line = marker.read_text(encoding="utf-8").splitlines()[0]
            except (OSError, UnicodeError, IndexError) as exc:
                raise ValueError("repository .git marker is invalid") from exc
            prefix, separator, raw_target = first_line.partition(":")
            if not separator or prefix.strip().casefold() != "gitdir" or not raw_target.strip():
                raise ValueError("repository .git marker is invalid")
            target_path = _path_value(raw_target.strip(), "gitdir")
            if not target_path.is_absolute():
                target_path = repository / target_path
            try:
                target = target_path.resolve(strict=True)
            except (OSError, RuntimeError) as exc:
                raise ValueError("repository gitdir must exist") from exc
        else:
            raise ValueError("repository .git marker is invalid")
        if not target.is_dir() or not _is_within(target, self.workspace_root):
            raise PermissionError("repository Git metadata must stay inside workspace_root")

    def _run(self, executable: str, arguments: list[str], *, cwd: Path) -> _CommandResult:
        executable_path = self._executables.get(executable)
        if not executable_path:
            return _CommandResult(
                ok=False,
                exit_code=None,
                stdout="",
                stderr="",
                error=f"{executable} CLI is not available",
                timed_out=False,
                truncated=False,
            )
        environment = trusted_cli_environment()
        unsafe_git_environment = {
            "GH_ENTERPRISE_TOKEN", "GH_FORCE_TTY", "GH_HOST", "GH_REPO", "GH_TOKEN",
            "GITHUB_ENTERPRISE_TOKEN", "GITHUB_TOKEN",
            "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_ASKPASS",
            "GIT_ATTR_NOSYSTEM", "GIT_CEILING_DIRECTORIES", "GIT_COMMON_DIR",
            "GIT_DIR", "GIT_DISCOVERY_ACROSS_FILESYSTEM", "GIT_EXEC_PATH",
            "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY", "GIT_OPTIONAL_LOCKS",
            "GIT_PROTOCOL_FROM_USER", "GIT_PROXY_COMMAND", "GIT_SSH",
            "GIT_SSH_COMMAND", "GIT_SSH_VARIANT", "GIT_TEMPLATE_DIR",
            "GIT_TRACE", "GIT_TRACE2", "GIT_TRACE2_EVENT", "GIT_TRACE2_PERF",
            "GIT_WORK_TREE", "SSH_ASKPASS",
        }
        for key in tuple(environment):
            normalized = key.upper()
            if normalized in unsafe_git_environment or normalized.startswith("GIT_CONFIG"):
                environment.pop(key, None)
        environment.update({
            "GH_HOST": "github.com",
            "GH_PROMPT_DISABLED": "1",
            "GH_PAGER": "cat",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_PAGER": "cat",
            "GIT_TERMINAL_PROMPT": "0",
            "GCM_INTERACTIVE": "Never",
            "NO_COLOR": "1",
            "PAGER": "cat",
        })
        argv = [str(executable_path), *arguments]
        try:
            completed = self._runner(
                argv,
                cwd=str(cwd),
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=self.timeout_seconds,
                check=False,
                shell=False,
            )
        except subprocess.TimeoutExpired as exc:
            stdout, stdout_truncated = _bounded_text(
                getattr(exc, "stdout", None) or getattr(exc, "output", None),
                self.max_output_bytes,
            )
            stderr, stderr_truncated = _bounded_text(
                getattr(exc, "stderr", None), self.max_output_bytes
            )
            return _CommandResult(
                ok=False,
                exit_code=None,
                stdout=stdout,
                stderr=stderr,
                error=f"{executable} command timed out",
                timed_out=True,
                truncated=stdout_truncated or stderr_truncated,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return _CommandResult(
                ok=False,
                exit_code=None,
                stdout="",
                stderr="",
                error=f"{executable} command failed to start: {type(exc).__name__}",
                timed_out=False,
                truncated=False,
            )

        stdout, stdout_truncated = _bounded_text(
            getattr(completed, "stdout", None), self.max_output_bytes
        )
        stderr, stderr_truncated = _bounded_text(
            getattr(completed, "stderr", None), self.max_output_bytes
        )
        exit_code = int(getattr(completed, "returncode", 1))
        ok = exit_code == 0
        error = None if ok else (stderr.strip() or stdout.strip() or f"{executable} exited with {exit_code}")
        return _CommandResult(
            ok=ok,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            error=error,
            timed_out=False,
            truncated=stdout_truncated or stderr_truncated,
        )

    @staticmethod
    def _public_result(
        operation: str,
        command: _CommandResult,
        *,
        data: dict[str, Any] | list[dict[str, Any]] | None,
        error: str | None = None,
        ok: bool | None = None,
    ) -> GitHubResult:
        return GitHubResult(
            operation=operation,
            ok=command.ok if ok is None else ok,
            data=data,
            error=command.error if error is None else error,
            exit_code=command.exit_code,
            stdout=command.stdout,
            stderr=command.stderr,
            timed_out=command.timed_out,
            truncated=command.truncated,
        )


def _path_value(value: str | os.PathLike[str], field: str) -> Path:
    try:
        raw = os.fspath(value)
    except TypeError as exc:
        raise TypeError(f"{field} must be a path") from exc
    if isinstance(raw, bytes):
        raise TypeError(f"{field} must be a text path")
    if not raw or "\x00" in raw:
        raise ValueError(f"{field} must be a non-empty path without NUL bytes")
    return Path(raw)


def _unsafe_http_remote(value: str) -> bool:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return True
    return parsed.scheme.casefold() in {"http", "https"} and bool(
        parsed.username or parsed.password or parsed.query or parsed.fragment
    )


def _is_github_push_remote(value: str) -> bool:
    return bool(
        _GITHUB_HTTPS_REMOTE.fullmatch(value)
        or _GITHUB_SCP_REMOTE.fullmatch(value)
        or _GITHUB_SSH_REMOTE.fullmatch(value)
    )


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _validate_owner(owner: str) -> str:
    value = _text_value(owner, "owner")
    if not _OWNER_PATTERN.fullmatch(value) or value.endswith("-") or "--" in value:
        raise ValueError("owner is not a valid conservative GitHub owner name")
    return value


def _validate_repository_slug(name: str) -> str:
    value = _text_value(name, "name")
    parts = value.split("/")
    if len(parts) == 2:
        owner = _validate_owner(parts[0])
        repository = parts[1]
    elif len(parts) == 1:
        owner = None
        repository = parts[0]
    else:
        raise ValueError("name must be REPOSITORY or OWNER/REPOSITORY")
    if (
        not _REPOSITORY_PATTERN.fullmatch(repository)
        or repository in {".", ".."}
        or repository.casefold().endswith(".git")
    ):
        raise ValueError("name contains an invalid GitHub repository name")
    return f"{owner}/{repository}" if owner else repository


def _validate_remote(remote: str) -> str:
    value = _text_value(remote, "remote")
    if not _REMOTE_PATTERN.fullmatch(value) or value in {".", ".."}:
        raise ValueError("remote contains unsupported characters")
    return value


def _validate_branch(branch: str) -> str:
    value = _text_value(branch, "branch")
    invalid_segments = any(segment in {"", ".", ".."} for segment in value.split("/"))
    if (
        not _BRANCH_PATTERN.fullmatch(value)
        or invalid_segments
        or ".." in value
        or "@{" in value
        or value.endswith(("/", ".", ".lock"))
    ):
        raise ValueError("branch must be one explicit, conservative branch name")
    return value


def _validate_description(description: str) -> str:
    if not isinstance(description, str):
        raise TypeError("description must be a string")
    value = description.strip()
    if "\x00" in value:
        raise ValueError("description must not contain NUL bytes")
    if len(value) > 350:
        raise ValueError("description must not exceed 350 characters")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError("description must not contain control characters")
    return value


def _text_value(value: str, field: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string")
    text = value.strip()
    if not text or "\x00" in text:
        raise ValueError(f"{field} must be a non-empty string without NUL bytes")
    return text


def _bounded_text(value: Any, limit: int) -> tuple[str, bool]:
    if value is None:
        return "", False
    if isinstance(value, bytes):
        text = value.decode("utf-8", errors="replace")
    else:
        text = str(value)
    text = _TOKEN_PATTERN.sub("[redacted]", text)
    text = _CREDENTIAL_URL_PATTERN.sub(r"\1[redacted]@", text)
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= limit:
        return text, False
    marker = b"\n...[truncated]"
    clipped = encoded[: max(0, limit - len(marker))] + marker
    return clipped.decode("utf-8", errors="ignore"), True
