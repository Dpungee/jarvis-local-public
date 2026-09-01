"""Bounded, workspace-scoped access to an installed official Vercel CLI.

The provider intentionally does not install the CLI, invoke a shell, accept tokens on
the command line, or perform interactive login.  Command shapes follow Vercel's
official CLI references:

* https://vercel.com/docs/cli
* https://vercel.com/docs/cli/deploy
* https://vercel.com/docs/cli/logs
* https://vercel.com/docs/cli/integration

The module is standalone so it can be wired into JARVIS separately from the general
computer tool surface.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import subprocess
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .redaction import is_sensitive_key, redact_secrets
from .subprocess_env import trusted_cli_environment
from .trusted_executables import trusted_path_executable


Runner = Callable[..., subprocess.CompletedProcess[str]]
ExecutableFinder = Callable[[str], str | None]


def _trusted_vercel_executable(workspace: Path) -> str | None:
    """Resolve Vercel only from an ordinary OS-administered native binary."""
    name = "vercel.exe" if os.name == "nt" else "vercel"
    candidate = trusted_path_executable(name, prohibited_roots=(workspace,))
    return str(candidate) if candidate is not None else None


_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_TARGET = re.compile(r"[a-z0-9][a-z0-9-]{0,47}\Z")
_INTEGRATION_PRODUCT = re.compile(
    r"[a-z0-9][a-z0-9-]{0,63}(?:/[a-z0-9][a-z0-9-]{0,63})?\Z"
)
_METADATA_KEY = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,63}\Z")
_REFERENCE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}\Z")
_RELATIVE_TIME = re.compile(r"[1-9][0-9]{0,3}[smhdw]\Z", re.I)
_ISO_TIME = re.compile(r"[0-9]{4}-[0-9T:+.Z-]{5,48}\Z", re.I)
_DATABASE_TERMS = re.compile(
    r"(?i)\b(?:database|data\s*store|postgres(?:ql)?|mysql|mariadb|sqlite|sql|"
    r"redis|mongodb|dynamodb|cockroach(?:db)?|planetscale|neon|supabase|upstash|"
    r"turso|fauna|edgedb|convex|kv|vector\s*(?:database|store|db))\b"
)
_NON_DATABASE_STORAGE = re.compile(r"(?i)\b(?:blob|object|file|image|media)\s+storage\b")
_TRUNCATION_MARKER = "\n...[output truncated by JARVIS]"
_LINK_ID = re.compile(r"[A-Za-z0-9_-]{1,256}\Z")
MAX_LINK_FILE_BYTES = 64 * 1024
MAX_DEPLOY_SNAPSHOT_FILES = 25_000
MAX_DEPLOY_SNAPSHOT_DIRECTORIES = 5_000
MAX_DEPLOY_SNAPSHOT_ENTRIES = 30_000
MAX_DEPLOY_SNAPSHOT_BYTES = 100 * 1024 * 1024
MAX_DEPLOY_SNAPSHOT_FILE_BYTES = MAX_DEPLOY_SNAPSHOT_BYTES


class VercelProviderError(RuntimeError):
    """Base error for invalid provider setup or requests."""


def _redact_output_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            redact_secrets(str(key)): (
                "[REDACTED]" if is_sensitive_key(str(key))
                else _redact_output_value(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_output_value(item) for item in value]
    if isinstance(value, str):
        return redact_secrets(value)
    return value


def _redact_output_text(value: str) -> str:
    """Redact CLI output while keeping JSON and JSON Lines parseable."""
    stripped = value.strip()
    if not stripped:
        return value
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        lines = value.splitlines()
        parsed_lines: list[Any] = []
        try:
            for line in lines:
                if line.strip():
                    parsed_lines.append(json.loads(line))
        except json.JSONDecodeError:
            return redact_secrets(value)
        if parsed_lines and len(parsed_lines) == len([line for line in lines if line.strip()]):
            return "\n".join(
                json.dumps(_redact_output_value(item), ensure_ascii=False)
                for item in parsed_lines
            )
        return redact_secrets(value)
    return json.dumps(_redact_output_value(parsed), ensure_ascii=False)


class VercelCLIUnavailableError(VercelProviderError):
    """Raised when the official Vercel CLI is not installed."""


class VercelWorkspaceError(VercelProviderError):
    """Raised when a project path escapes or is invalid within the workspace."""


@dataclass(frozen=True, slots=True)
class VercelResult:
    """A bounded result from one Vercel CLI operation."""

    operation: str
    ok: bool
    command: tuple[str, ...]
    cwd: str
    returncode: int | None
    stdout: str = ""
    stderr: str = ""
    data: Any = None
    error: str | None = None
    timed_out: bool = False
    truncated: bool = False
    duration_seconds: float = 0.0


@dataclass(frozen=True, slots=True)
class VercelStatus:
    """Installed CLI and read-only authentication status."""

    available: bool
    cli_path: str | None
    version: str | None
    authenticated: bool
    user: str | None
    error: str | None = None


class VercelProvider:
    """Use an installed Vercel CLI without leaving a configured workspace."""

    def __init__(
        self,
        workspace: str | os.PathLike[str],
        *,
        cli_path: str | os.PathLike[str] | None = None,
        scope: str | None = None,
        command_timeout_seconds: float = 30.0,
        deploy_timeout_seconds: float = 300.0,
        max_output_chars: int = 64_000,
        runner: Runner | None = None,
        executable_finder: ExecutableFinder | None = None,
    ) -> None:
        try:
            root = Path(workspace).resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise VercelWorkspaceError("The Vercel workspace does not exist.") from exc
        if not root.is_dir():
            raise VercelWorkspaceError("The Vercel workspace must be a directory.")
        if not 0.1 <= float(command_timeout_seconds) <= 120.0:
            raise ValueError("command_timeout_seconds must be between 0.1 and 120")
        if not 0.1 <= float(deploy_timeout_seconds) <= 900.0:
            raise ValueError("deploy_timeout_seconds must be between 0.1 and 900")
        if isinstance(max_output_chars, bool) or not 1_024 <= int(max_output_chars) <= 1_000_000:
            raise ValueError("max_output_chars must be between 1024 and 1000000")

        if (cli_path is not None or executable_finder is not None) and runner is None:
            raise ValueError("A custom Vercel executable requires a custom runner")
        if cli_path is not None:
            found = os.fspath(cli_path)
        elif executable_finder is not None:
            found = executable_finder("vercel")
        else:
            found = _trusted_vercel_executable(root)
        if found is not None and (not found.strip() or "\x00" in found):
            raise ValueError("cli_path is invalid")
        self.workspace = root
        self.cli_path = found
        self.scope = self._validated_name(scope, "scope") if scope is not None else None
        self.command_timeout_seconds = float(command_timeout_seconds)
        self.deploy_timeout_seconds = float(deploy_timeout_seconds)
        self.max_output_chars = int(max_output_chars)
        self._runner = runner or subprocess.run

    @property
    def available(self) -> bool:
        return self.cli_path is not None

    @staticmethod
    def _validated_name(value: str, label: str) -> str:
        text = str(value).strip()
        if not _NAME.fullmatch(text):
            raise ValueError(f"{label} contains unsupported characters")
        return text

    @staticmethod
    def _validated_target(value: str) -> str:
        target = str(value).strip().casefold()
        if not _TARGET.fullmatch(target):
            raise ValueError("target must be a production, preview, or custom environment slug")
        return target

    @staticmethod
    def _validated_reference(value: str) -> str:
        reference = str(value).strip()
        if not reference or len(reference) > 2_048 or "\x00" in reference:
            raise ValueError("deployment reference is invalid")
        if reference.startswith("https://"):
            parsed = urlsplit(reference)
            if (
                parsed.scheme != "https"
                or not parsed.netloc
                or parsed.username is not None
                or parsed.password is not None
                or any(char.isspace() for char in reference)
            ):
                raise ValueError("deployment reference must be a safe HTTPS URL")
            return reference
        if not _REFERENCE.fullmatch(reference):
            raise ValueError("deployment reference must be an ID, hostname, or HTTPS URL")
        return reference

    @staticmethod
    def _validated_time(value: str) -> str:
        text = str(value).strip()
        if not (_RELATIVE_TIME.fullmatch(text) or _ISO_TIME.fullmatch(text)):
            raise ValueError("time filters must be compact relative values or ISO-8601 timestamps")
        return text

    def _project_directory(self, value: str | os.PathLike[str] | None) -> Path:
        raw = "." if value is None else os.fspath(value)
        if not raw or "\x00" in raw or len(raw) > 4_096:
            raise VercelWorkspaceError("The project path is invalid.")
        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = self.workspace / candidate
        try:
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(self.workspace)
        except (OSError, RuntimeError, ValueError) as exc:
            raise VercelWorkspaceError(
                "The project path must resolve to an existing directory inside the workspace."
            ) from exc
        if not resolved.is_dir():
            raise VercelWorkspaceError("The project path must be a directory.")
        return resolved

    @staticmethod
    def _text(value: str | bytes | None) -> str:
        if value is None:
            return ""
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        return str(value)

    def _bounded(self, value: str | bytes | None) -> tuple[str, bool]:
        text = _redact_output_text(self._text(value).replace("\x00", ""))
        if len(text) <= self.max_output_chars:
            return text, False
        keep = max(0, self.max_output_chars - len(_TRUNCATION_MARKER))
        return text[:keep] + _TRUNCATION_MARKER, True

    def _bounded_file(self, stream: Any) -> tuple[str, bool]:
        stream.flush()
        stream.seek(0)
        raw = stream.read(self.max_output_chars + 1)
        text, clipped = self._bounded(raw)
        return text, clipped or len(raw) > self.max_output_chars

    def _command(self, parts: Sequence[str], cwd: Path) -> list[str]:
        if self.cli_path is None:
            raise VercelCLIUnavailableError(
                "The official Vercel CLI is not installed or is not on PATH."
            )
        values = [self.cli_path, *(str(part) for part in parts)]
        values.extend(("--no-color", "--cwd", str(cwd)))
        if self.scope is not None:
            values.extend(("--scope", self.scope))
        if any("\x00" in value or len(value) > 8_192 for value in values):
            raise ValueError("Vercel command contains an invalid argument")
        return values

    def _execute(
        self,
        operation: str,
        parts: Sequence[str],
        *,
        cwd: Path,
        timeout_seconds: float | None = None,
        environment_overrides: Mapping[str, str] | None = None,
    ) -> VercelResult:
        command = self._command(parts, cwd)
        timeout = self.command_timeout_seconds if timeout_seconds is None else float(timeout_seconds)
        environment = trusted_cli_environment()
        for key in tuple(environment):
            if key.upper() in {
                "VERCEL_ORG_ID", "VERCEL_PROJECT_ID", "VERCEL_SCOPE",
                "VERCEL_TEAM_ID",
            }:
                environment.pop(key, None)
        environment.update({
            "CI": "1",
            "NO_COLOR": "1",
            "VERCEL_TELEMETRY_DISABLED": "1",
        })
        if environment_overrides:
            environment.update({str(key): str(value) for key, value in environment_overrides.items()})
        started = time.monotonic()
        with tempfile.TemporaryFile(mode="w+b") as stdout_file, tempfile.TemporaryFile(
            mode="w+b"
        ) as stderr_file:
            try:
                completed = self._runner(
                    command,
                    cwd=str(cwd),
                    stdin=subprocess.DEVNULL,
                    stdout=stdout_file,
                    stderr=stderr_file,
                    timeout=timeout,
                    check=False,
                    shell=False,
                    env=environment,
                )
            except subprocess.TimeoutExpired as exc:
                file_stdout, file_stdout_truncated = self._bounded_file(stdout_file)
                file_stderr, file_stderr_truncated = self._bounded_file(stderr_file)
                stdout, stdout_truncated = (
                    self._bounded(exc.stdout)
                    if exc.stdout is not None
                    else (file_stdout, file_stdout_truncated)
                )
                stderr, stderr_truncated = (
                    self._bounded(exc.stderr)
                    if exc.stderr is not None
                    else (file_stderr, file_stderr_truncated)
                )
                return VercelResult(
                    operation=operation,
                    ok=False,
                    command=tuple(command),
                    cwd=str(cwd),
                    returncode=None,
                    stdout=stdout,
                    stderr=stderr,
                    error=f"Vercel CLI timed out after {timeout:g} seconds.",
                    timed_out=True,
                    truncated=stdout_truncated or stderr_truncated,
                    duration_seconds=round(time.monotonic() - started, 6),
                )
            except OSError as exc:
                return VercelResult(
                    operation=operation,
                    ok=False,
                    command=tuple(command),
                    cwd=str(cwd),
                    returncode=None,
                    error=(
                        "Vercel CLI could not be started: "
                        f"{redact_secrets(str(exc))}"
                    ),
                    duration_seconds=round(time.monotonic() - started, 6),
                )

            file_stdout, file_stdout_truncated = self._bounded_file(stdout_file)
            file_stderr, file_stderr_truncated = self._bounded_file(stderr_file)
            stdout, stdout_truncated = (
                self._bounded(completed.stdout)
                if completed.stdout is not None
                else (file_stdout, file_stdout_truncated)
            )
            stderr, stderr_truncated = (
                self._bounded(completed.stderr)
                if completed.stderr is not None
                else (file_stderr, file_stderr_truncated)
            )
        returncode = int(completed.returncode)
        return VercelResult(
            operation=operation,
            ok=returncode == 0,
            command=tuple(command),
            cwd=str(cwd),
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
            error=None if returncode == 0 else (stderr.strip() or "Vercel CLI command failed."),
            truncated=stdout_truncated or stderr_truncated,
            duration_seconds=round(time.monotonic() - started, 6),
        )

    @staticmethod
    def _json_result(result: VercelResult) -> VercelResult:
        if not result.ok:
            return result
        if result.truncated:
            return replace(
                result,
                ok=False,
                error="Vercel JSON output exceeded the configured output bound.",
            )
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            return replace(
                result,
                ok=False,
                error=f"Vercel CLI returned malformed JSON: {exc.msg}.",
            )
        return replace(result, data=payload)

    def auth_status(self) -> VercelResult:
        """Return the current CLI user; this never starts an interactive login."""
        result = self._execute("auth_status", ("whoami",), cwd=self.workspace)
        username = result.stdout.strip().splitlines()[-1] if result.ok and result.stdout.strip() else None
        return replace(
            result,
            data={"authenticated": bool(username), "user": username},
        )

    def status(self) -> VercelStatus:
        """Report CLI availability, version, and read-only authentication status."""
        if not self.available:
            return VercelStatus(False, None, None, False, None, "Vercel CLI is not installed.")
        version_result = self._execute("version", ("--version",), cwd=self.workspace)
        version = version_result.stdout.strip().splitlines()[-1] if version_result.ok else None
        auth = self.auth_status()
        auth_data = auth.data if isinstance(auth.data, dict) else {}
        errors = [value for value in (version_result.error, auth.error) if value]
        return VercelStatus(
            available=True,
            cli_path=self.cli_path,
            version=version,
            authenticated=bool(auth_data.get("authenticated")),
            user=str(auth_data["user"]) if auth_data.get("user") else None,
            error="; ".join(errors) or None,
        )

    def list_projects(self) -> VercelResult:
        """List accessible Vercel projects in official JSON format."""
        result = self._execute(
            "list_projects",
            ("project", "ls", "--json"),
            cwd=self.workspace,
        )
        return self._json_result(result)

    def project_status(
        self,
        project_name: str | None = None,
        *,
        project_path: str | os.PathLike[str] | None = None,
    ) -> VercelResult:
        """Inspect a named or workspace-linked project."""
        cwd = self._project_directory(project_path)
        parts = ["project", "inspect"]
        if project_name is not None:
            parts.append(self._validated_name(project_name, "project_name"))
        result = self._execute("project_status", parts, cwd=cwd)
        return replace(result, data={"details": result.stdout} if result.ok else None)

    def deploy(
        self,
        project_path: str | os.PathLike[str] | None = None,
        *,
        production: bool = False,
        target: str | None = None,
        prebuilt: bool = False,
        wait: bool = False,
        expected_approval_snapshot: Mapping[str, Any] | None = None,
    ) -> VercelResult:
        """Create one explicitly targeted, non-interactive deployment."""
        cwd = self._project_directory(project_path)
        if production and target not in (None, "production"):
            raise ValueError("production cannot be combined with another deployment target")
        selected_target = "production" if production else self._validated_target(target or "preview")
        pinned_environment: dict[str, str] | None = None
        if expected_approval_snapshot is not None:
            if not isinstance(expected_approval_snapshot, Mapping):
                raise TypeError("expected_approval_snapshot must be a mapping")
            current_snapshot = self.deployment_approval_snapshot(cwd, prebuilt=prebuilt)
            if dict(expected_approval_snapshot) != current_snapshot:
                raise PermissionError(
                    "Vercel linked destination or deployable tree changed after approval"
                )
            pinned_environment = {
                "VERCEL_ORG_ID": current_snapshot["org_id"],
                "VERCEL_PROJECT_ID": current_snapshot["project_id"],
            }
            if current_snapshot["account_scope"] is not None:
                pinned_environment["VERCEL_SCOPE"] = current_snapshot["account_scope"]
        parts = ["deploy", "--yes"]
        if selected_target == "production":
            parts.append("--prod")
        else:
            parts.append(f"--target={selected_target}")
        if prebuilt:
            parts.append("--prebuilt")
        if not wait:
            parts.append("--no-wait")
        result = self._execute(
            "deploy",
            parts,
            cwd=cwd,
            timeout_seconds=self.deploy_timeout_seconds,
            environment_overrides=pinned_environment,
        )
        if not result.ok:
            return result
        deployment_url = next(
            (
                line.strip()
                for line in result.stdout.splitlines()
                if line.strip().startswith("https://")
            ),
            None,
        )
        if deployment_url is None:
            return replace(result, ok=False, error="Vercel deploy returned no deployment URL.")
        return replace(
            result,
            data={"deployment_url": deployment_url, "target": selected_target},
        )

    def deployment_approval_snapshot(
        self,
        project_path: str | os.PathLike[str] | None = None,
        *,
        prebuilt: bool = False,
    ) -> dict[str, Any]:
        """Bind a deployment to its local Vercel link and a bounded tree digest."""
        if not isinstance(prebuilt, bool):
            raise TypeError("prebuilt must be a boolean")
        cwd = self._project_directory(project_path)
        link_directory = cwd / ".vercel"
        try:
            link_directory_stat = os.lstat(link_directory)
        except OSError:
            raise VercelWorkspaceError("The project is not linked to Vercel.") from None
        if (
            stat.S_ISLNK(link_directory_stat.st_mode)
            or _is_reparse_point(link_directory_stat)
            or not stat.S_ISDIR(link_directory_stat.st_mode)
        ):
            raise VercelWorkspaceError(
                "The Vercel project link directory must be an ordinary non-link directory."
            )
        link_path = link_directory / "project.json"
        link_bytes = _read_stable_regular_file(
            link_path,
            max_bytes=MAX_LINK_FILE_BYTES,
            label="Vercel project link",
        )
        try:
            link = json.loads(link_bytes.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError):
            raise VercelWorkspaceError("The Vercel project link is invalid.") from None
        if not isinstance(link, dict):
            raise VercelWorkspaceError("The Vercel project link is invalid.")
        project_id = link.get("projectId")
        org_id = link.get("orgId")
        if (
            not isinstance(project_id, str)
            or not _LINK_ID.fullmatch(project_id)
            or not isinstance(org_id, str)
            or not _LINK_ID.fullmatch(org_id)
        ):
            raise VercelWorkspaceError(
                "The Vercel project link must contain exact projectId and orgId values."
            )
        tree_root = cwd / ".vercel" / "output" if prebuilt else cwd
        digest, file_count, total_bytes = _deployment_tree_digest(
            tree_root,
            source_project=not prebuilt,
        )
        return {
            "resolved_project_path": str(cwd),
            "project_id": project_id,
            "org_id": org_id,
            "account_scope": self.scope,
            "project_link_sha256": hashlib.sha256(link_bytes).hexdigest(),
            "prebuilt": prebuilt,
            "deploy_tree_sha256": digest,
            "deploy_file_count": file_count,
            "deploy_total_bytes": total_bytes,
        }

    def deployment_status(
        self,
        deployment: str,
        *,
        project_path: str | os.PathLike[str] | None = None,
    ) -> VercelResult:
        """Inspect an existing deployment without waiting for state changes."""
        cwd = self._project_directory(project_path)
        reference = self._validated_reference(deployment)
        result = self._execute("deployment_status", ("inspect", reference), cwd=cwd)
        return replace(
            result,
            data={"deployment": reference, "details": result.stdout} if result.ok else None,
        )

    def build_logs(
        self,
        deployment: str,
        *,
        project_path: str | os.PathLike[str] | None = None,
    ) -> VercelResult:
        """Retrieve bounded build logs for an existing deployment."""
        cwd = self._project_directory(project_path)
        reference = self._validated_reference(deployment)
        result = self._execute(
            "build_logs",
            ("inspect", reference, "--logs"),
            cwd=cwd,
        )
        return replace(result, data={"lines": result.stdout.splitlines()} if result.ok else None)

    def deployment_logs(
        self,
        deployment: str | None = None,
        *,
        project_name: str | None = None,
        project_path: str | os.PathLike[str] | None = None,
        limit: int = 100,
        since: str = "1h",
        level: str | None = None,
        environment: str | None = None,
    ) -> VercelResult:
        """Retrieve bounded, non-following runtime logs as parsed JSON Lines."""
        if isinstance(limit, bool) or not 1 <= int(limit) <= 200:
            raise ValueError("limit must be between 1 and 200")
        if deployment is not None and project_name is not None:
            raise ValueError("choose deployment or project_name, not both")
        cwd = self._project_directory(project_path)
        parts = [
            "logs",
            "--json",
            "--no-follow",
            "--limit",
            str(int(limit)),
            "--since",
            self._validated_time(since),
        ]
        if deployment is not None:
            parts.extend(("--deployment", self._validated_reference(deployment)))
        if project_name is not None:
            parts.extend(("--project", self._validated_name(project_name, "project_name")))
        if level is not None:
            normalized_level = str(level).casefold()
            if normalized_level not in {"error", "warning", "info", "fatal"}:
                raise ValueError("level is not supported by Vercel logs")
            parts.extend(("--level", normalized_level))
        if environment is not None:
            normalized_environment = str(environment).casefold()
            if normalized_environment not in {"production", "preview"}:
                raise ValueError("environment must be production or preview")
            parts.extend(("--environment", normalized_environment))

        result = self._execute("deployment_logs", parts, cwd=cwd)
        if not result.ok:
            return result
        entries: list[Any] = []
        lines = [line for line in result.stdout.splitlines() if line.strip()]
        for index, line in enumerate(lines):
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError as exc:
                if result.truncated and index == len(lines) - 1:
                    break
                return replace(
                    result,
                    ok=False,
                    error=f"Vercel logs returned malformed JSON Lines: {exc.msg}.",
                )
        return replace(
            result,
            data={"entries": entries, "complete": not result.truncated},
        )

    def logs(self, *args: Any, **kwargs: Any) -> VercelResult:
        """Alias for :meth:`deployment_logs`."""
        return self.deployment_logs(*args, **kwargs)

    def list_integration_resources(
        self,
        project_name: str | None = None,
        *,
        project_path: str | os.PathLike[str] | None = None,
    ) -> VercelResult:
        """List installed Marketplace resources without changing connections."""
        cwd = self._project_directory(project_path)
        parts = ["integration", "list"]
        if project_name is not None:
            parts.append(self._validated_name(project_name, "project_name"))
        parts.append("--format=json")
        return self._json_result(
            self._execute("list_integration_resources", parts, cwd=cwd)
        )

    @staticmethod
    def _validated_integration_product(value: str) -> str:
        slug = str(value).strip().casefold()
        if not _INTEGRATION_PRODUCT.fullmatch(slug):
            raise ValueError(
                "integration_product_slug must be an explicit integration or integration/product slug"
            )
        return slug

    @staticmethod
    def _validated_metadata(
        metadata: Mapping[str, str | int | float | bool] | None,
    ) -> list[tuple[str, str]]:
        if metadata is None:
            return []
        if not isinstance(metadata, Mapping):
            raise TypeError("metadata must be a mapping")
        if len(metadata) > 16:
            raise ValueError("metadata accepts at most 16 entries")
        normalized: list[tuple[str, str]] = []
        total_size = 0
        for raw_key, raw_value in metadata.items():
            if not isinstance(raw_key, str) or not _METADATA_KEY.fullmatch(raw_key):
                raise ValueError("metadata keys must be bounded CLI-safe identifiers")
            if isinstance(raw_value, bool):
                value = "true" if raw_value else "false"
            elif isinstance(raw_value, int):
                value = str(raw_value)
            elif isinstance(raw_value, float):
                if not math.isfinite(raw_value):
                    raise ValueError("metadata values must be finite")
                value = str(raw_value)
            elif isinstance(raw_value, str):
                value = raw_value
            else:
                raise TypeError("metadata values must be strings, numbers, or booleans")
            if (
                not value.strip()
                or len(value) > 512
                or len(value.splitlines()) != 1
                or any(ord(character) < 32 or ord(character) == 127 for character in value)
            ):
                raise ValueError("metadata values must be non-empty bounded single-line text")
            total_size += len(raw_key) + len(value)
            if total_size > 4_096:
                raise ValueError("metadata exceeds the 4096-character aggregate bound")
            normalized.append((raw_key, value))
        return sorted(normalized)

    @staticmethod
    def _validated_environments(environments: Sequence[str]) -> list[str]:
        if isinstance(environments, (str, bytes)) or not isinstance(environments, Sequence):
            raise TypeError("environments must be a sequence")
        if len(environments) > 3:
            raise ValueError("environments accepts at most three entries")
        normalized = [str(environment).strip().casefold() for environment in environments]
        if any(value not in {"production", "preview", "development"} for value in normalized):
            raise ValueError("environments may contain production, preview, and development only")
        if len(normalized) != len(set(normalized)):
            raise ValueError("environments must not contain duplicates")
        return normalized

    def provision_database(
        self,
        integration_product_slug: str,
        *,
        resource_name: str,
        plan: str,
        metadata: Mapping[str, str | int | float | bool] | None = None,
        environments: Sequence[str] = ("production", "preview", "development"),
        connect: bool = True,
        project_path: str | os.PathLike[str] | None = None,
    ) -> VercelResult:
        """Provision one explicitly selected database Marketplace resource.

        This is deliberately non-interactive.  A plan is always required, environment
        pulling is always disabled, and callers must explicitly choose whether the new
        resource is connected to the workspace-linked project.
        """
        cwd = self._project_directory(project_path)
        slug = self._validated_integration_product(integration_product_slug)
        name = self._validated_name(resource_name, "resource_name")
        selected_plan = self._validated_name(plan, "plan")
        if not isinstance(connect, bool):
            raise TypeError("connect must be a boolean")
        selected_metadata = self._validated_metadata(metadata)
        selected_environments = self._validated_environments(environments)
        if connect and not selected_environments:
            raise ValueError("connected resources require at least one explicit environment")

        parts = [
            "integration",
            "add",
            slug,
            "--name",
            name,
            "--plan",
            selected_plan,
        ]
        for key, value in selected_metadata:
            parts.extend(("--metadata", f"{key}={value}"))
        if connect:
            for environment in selected_environments:
                parts.extend(("--environment", environment))
        else:
            parts.append("--no-connect")
        parts.extend(("--no-env-pull", "--json", "--non-interactive"))
        return self._json_result(
            self._execute(
                "provision_database",
                parts,
                cwd=cwd,
                timeout_seconds=self.deploy_timeout_seconds,
            )
        )

    def add_integration(self, *args: Any, **kwargs: Any) -> VercelResult:
        """Alias for the explicit database integration provisioning operation."""
        return self.provision_database(*args, **kwargs)

    def discover_integrations(self) -> VercelResult:
        """Browse current Marketplace integrations using read-only JSON output."""
        return self._json_result(
            self._execute(
                "discover_integrations",
                ("integration", "discover", "--format=json"),
                cwd=self.workspace,
            )
        )

    @staticmethod
    def _records(payload: Any) -> list[dict[str, Any]]:
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        if not isinstance(payload, dict):
            return []
        for key in ("resources", "integrations", "products", "items", "data", "results"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
            if isinstance(value, dict):
                nested = VercelProvider._records(value)
                if nested:
                    return nested
        return [payload] if any(key in payload for key in ("name", "slug", "id")) else []

    @staticmethod
    def _is_database_record(record: dict[str, Any]) -> bool:
        encoded = json.dumps(record, ensure_ascii=False, sort_keys=True, default=str)
        if _DATABASE_TERMS.search(encoded):
            return True
        categories = " ".join(
            str(record.get(key, "")) for key in ("category", "categories", "type", "tags")
        )
        return "storage" in categories.casefold() and not _NON_DATABASE_STORAGE.search(encoded)

    def discover_database_integrations(self) -> VercelResult:
        """Return current Marketplace entries whose metadata identifies database storage."""
        result = self.discover_integrations()
        if not result.ok:
            return replace(result, operation="discover_database_integrations")
        records = self._records(result.data)
        candidates = [record for record in records if self._is_database_record(record)]
        return replace(
            result,
            operation="discover_database_integrations",
            data=candidates,
        )

    def list_database_integrations(
        self,
        project_name: str | None = None,
        *,
        project_path: str | os.PathLike[str] | None = None,
    ) -> VercelResult:
        """Filter installed Marketplace resources to database-like integrations."""
        result = self.list_integration_resources(
            project_name,
            project_path=project_path,
        )
        if not result.ok:
            return replace(result, operation="list_database_integrations")
        records = self._records(result.data)
        candidates = [record for record in records if self._is_database_record(record)]
        return replace(
            result,
            operation="list_database_integrations",
            data=candidates,
        )


def _is_reparse_point(details: os.stat_result) -> bool:
    attributes = getattr(details, "st_file_attributes", 0)
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _file_identity(details: os.stat_result) -> tuple[int, int, int, int]:
    return (
        int(details.st_dev),
        int(details.st_ino),
        int(details.st_size),
        int(details.st_mtime_ns),
    )


def _read_stable_regular_file(path: Path, *, max_bytes: int, label: str) -> bytes:
    try:
        before = os.lstat(path)
    except OSError:
        raise VercelWorkspaceError(f"{label} is missing or inaccessible.") from None
    if (
        stat.S_ISLNK(before.st_mode)
        or _is_reparse_point(before)
        or not stat.S_ISREG(before.st_mode)
    ):
        raise VercelWorkspaceError(f"{label} must be an ordinary non-link file.")
    if before.st_size > max_bytes:
        raise VercelWorkspaceError(f"{label} exceeds its {max_bytes}-byte limit.")
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        with os.fdopen(descriptor, "rb") as stream:
            opened = os.fstat(stream.fileno())
            if _file_identity(before) != _file_identity(opened) or not stat.S_ISREG(opened.st_mode):
                raise VercelWorkspaceError(f"{label} changed while it was opened.")
            payload = stream.read(max_bytes + 1)
            after = os.fstat(stream.fileno())
    except VercelWorkspaceError:
        raise
    except OSError:
        raise VercelWorkspaceError(f"{label} could not be read safely.") from None
    if len(payload) > max_bytes:
        raise VercelWorkspaceError(f"{label} exceeds its {max_bytes}-byte limit.")
    if _file_identity(opened) != _file_identity(after) or len(payload) != after.st_size:
        raise VercelWorkspaceError(f"{label} changed while it was read.")
    return payload


def _deployment_tree_digest(
    root: Path,
    *,
    source_project: bool,
) -> tuple[str, int, int]:
    try:
        root_stat = os.lstat(root)
    except OSError:
        raise VercelWorkspaceError("The deployable tree is missing or inaccessible.") from None
    if (
        stat.S_ISLNK(root_stat.st_mode)
        or _is_reparse_point(root_stat)
        or not stat.S_ISDIR(root_stat.st_mode)
    ):
        raise VercelWorkspaceError(
            "The deployable tree must be an ordinary non-link directory."
        )

    digest = hashlib.sha256()
    file_count = 0
    visited_directories = 1
    visited_entries = 0
    total_bytes = 0

    def walk_error(_error: OSError) -> None:
        raise VercelWorkspaceError("The deployable tree could not be inspected safely.")

    for current_raw, directory_names, file_names in os.walk(
        root, topdown=True, followlinks=False, onerror=walk_error
    ):
        current = Path(current_raw)
        visited_directories += len(directory_names)
        visited_entries += len(directory_names) + len(file_names)
        if visited_directories > MAX_DEPLOY_SNAPSHOT_DIRECTORIES:
            raise VercelWorkspaceError("The deployable tree contains too many directories.")
        if visited_entries > MAX_DEPLOY_SNAPSHOT_ENTRIES:
            raise VercelWorkspaceError("The deployable tree contains too many entries.")
        kept_directories: list[str] = []
        for name in sorted(directory_names):
            relative = (current / name).relative_to(root)
            if source_project and (
                name in {".git", "node_modules"}
                or relative.parts == (".vercel",)
                or relative.parts[-2:] == (".next", "cache")
            ):
                continue
            try:
                details = os.lstat(current / name)
            except OSError:
                raise VercelWorkspaceError(
                    "The deployable tree changed while it was inspected."
                ) from None
            if (
                stat.S_ISLNK(details.st_mode)
                or _is_reparse_point(details)
                or not stat.S_ISDIR(details.st_mode)
            ):
                raise VercelWorkspaceError(
                    "The deployable tree must not contain linked directories."
                )
            kept_directories.append(name)
        directory_names[:] = kept_directories

        for name in sorted(file_names):
            path = current / name
            relative_text = path.relative_to(root).as_posix()
            try:
                before = os.lstat(path)
            except OSError:
                raise VercelWorkspaceError(
                    "The deployable tree changed while it was inspected."
                ) from None
            if (
                stat.S_ISLNK(before.st_mode)
                or _is_reparse_point(before)
                or not stat.S_ISREG(before.st_mode)
            ):
                raise VercelWorkspaceError(
                    "The deployable tree must contain only ordinary non-link files."
                )
            file_count += 1
            if file_count > MAX_DEPLOY_SNAPSHOT_FILES:
                raise VercelWorkspaceError("The deployable tree contains too many files.")
            if before.st_size > MAX_DEPLOY_SNAPSHOT_FILE_BYTES:
                raise VercelWorkspaceError("A deployable file exceeds the snapshot size limit.")
            total_bytes += before.st_size
            if total_bytes > MAX_DEPLOY_SNAPSHOT_BYTES:
                raise VercelWorkspaceError("The deployable tree exceeds the snapshot byte limit.")
            try:
                descriptor = os.open(
                    path,
                    os.O_RDONLY
                    | getattr(os, "O_BINARY", 0)
                    | getattr(os, "O_NOFOLLOW", 0),
                )
                with os.fdopen(descriptor, "rb") as stream:
                    opened = os.fstat(stream.fileno())
                    if _file_identity(before) != _file_identity(opened):
                        raise VercelWorkspaceError(
                            "A deployable file changed while it was opened."
                        )
                    encoded_path = relative_text.encode("utf-8")
                    digest.update(len(encoded_path).to_bytes(8, "big"))
                    digest.update(encoded_path)
                    digest.update(int(opened.st_size).to_bytes(8, "big"))
                    read_bytes = 0
                    remaining_global = MAX_DEPLOY_SNAPSHOT_BYTES - (
                        total_bytes - before.st_size
                    )
                    while True:
                        remaining = min(
                            int(opened.st_size) - read_bytes,
                            MAX_DEPLOY_SNAPSHOT_FILE_BYTES - read_bytes,
                            remaining_global - read_bytes,
                        )
                        chunk = stream.read(min(1024 * 1024, max(0, remaining) + 1))
                        if not chunk:
                            break
                        read_bytes += len(chunk)
                        if (
                            read_bytes > opened.st_size
                            or read_bytes > MAX_DEPLOY_SNAPSHOT_FILE_BYTES
                            or read_bytes > remaining_global
                        ):
                            raise VercelWorkspaceError(
                                "A deployable file grew beyond its bounded snapshot while being read."
                            )
                        digest.update(chunk)
                    after = os.fstat(stream.fileno())
            except VercelWorkspaceError:
                raise
            except OSError:
                raise VercelWorkspaceError(
                    "A deployable file could not be read safely."
                ) from None
            if _file_identity(opened) != _file_identity(after) or read_bytes != after.st_size:
                raise VercelWorkspaceError(
                    "A deployable file changed while its fingerprint was computed."
                )
    return digest.hexdigest(), file_count, total_bytes


__all__ = [
    "VercelCLIUnavailableError",
    "VercelProvider",
    "VercelProviderError",
    "VercelResult",
    "VercelStatus",
    "VercelWorkspaceError",
]
