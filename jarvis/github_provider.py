from __future__ import annotations

import configparser
import contextlib
import ctypes
import io
import json
import os
import re
import stat
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from .redaction import contains_secret
from .subprocess_env import trusted_cli_environment
from .trusted_executables import (
    trusted_path_executable,
    windows_directory,
    windows_system_executable,
)


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
_GIT_CONFIG_MAX_BYTES = 256 * 1024
_WINDOWS_REPARSE_POINT = 0x400
_SAFE_CORE_CONFIG = frozenset({
    "repositoryformatversion", "filemode", "bare", "logallrefupdates",
    "symlinks", "ignorecase", "precomposeunicode",
})
_SAFE_EXTENSION_CONFIG = frozenset({"objectformat", "refstorage"})
_SAFE_REMOTE_CONFIG = frozenset({"url", "pushurl", "fetch"})
_SAFE_BRANCH_CONFIG = frozenset({"remote", "merge", "description", "rebase"})
_SAFE_USER_CONFIG = frozenset({"name", "email", "signingkey"})
_GIT_REMOTE_SECTION = re.compile(r'remote\s+"([^"\\\r\n]{1,64})"\Z', re.I)
_GIT_BRANCH_SECTION = re.compile(r'branch\s+"([^"\\\r\n]{1,255})"\Z', re.I)
_GIT_FETCH_REFSPEC = re.compile(
    r"\+?refs/heads/\*:refs/remotes/([A-Za-z0-9][A-Za-z0-9._-]{0,63})/\*\Z"
)
_SAFE_REBASE_VALUES = frozenset({"false", "true", "merges", "interactive"})
_GIT_METADATA_MAX_ENTRIES = 250_000
_GIT_METADATA_MAX_DEPTH = 64
_GIT_METADATA_MAX_BYTES = 256 * 1024 * 1024 * 1024

Runner = Callable[..., subprocess.CompletedProcess[Any]]
Which = Callable[[str], str | None]


@dataclass(frozen=True)
class _GitRepositoryEnvelope:
    repository: Path
    git_dir: Path
    config_path: Path
    sections: dict[str, dict[str, str]]


def _trusted_provider_executable(name: str, workspace_root: Path) -> str | None:
    """Resolve a provider CLI only from an ordinary OS-administered file."""
    candidate = trusted_path_executable(name, prohibited_roots=(workspace_root,))
    return str(candidate) if candidate is not None else None


def _link_or_reparse(details: os.stat_result) -> bool:
    return stat.S_ISLNK(details.st_mode) or bool(
        getattr(details, "st_file_attributes", 0) & _WINDOWS_REPARSE_POINT
    )


def _safe_config_text(value: str, *, maximum: int = 1024) -> bool:
    return (
        len(value) <= maximum
        and "\x00" not in value
        and not any(ord(character) < 32 or ord(character) == 127 for character in value)
        and not contains_secret(value)
    )


def _validate_git_config(sections: dict[str, dict[str, str]]) -> None:
    """Reject every local Git setting outside the provider's inert allowlist.

    Git configuration is an executable input surface: fsmonitor, hooks, filters,
    textconv, credential helpers, URL rewrites, includes, and transport settings can
    all start programs or redirect network traffic.  This parser therefore allows
    only the small amount of inert repository metadata needed by status and push.
    """
    for raw_section, values in sections.items():
        section = raw_section.casefold()
        if section == "core":
            if not set(values).issubset(_SAFE_CORE_CONFIG):
                raise PermissionError("Git core configuration contains an unsafe setting")
            if values.get("repositoryformatversion", "0") not in {"0", "1"}:
                raise PermissionError("Git repository format is unsupported")
            if values.get("bare", "false").casefold() not in {"false", "no", "off", "0"}:
                raise PermissionError("Bare Git repositories are unsupported")
            for key in ("filemode", "logallrefupdates", "symlinks", "ignorecase", "precomposeunicode"):
                if key in values and values[key].casefold() not in {
                    "true", "false", "yes", "no", "on", "off", "1", "0",
                }:
                    raise PermissionError(f"Git core.{key} has an unsupported value")
        elif section == "extensions":
            if not set(values).issubset(_SAFE_EXTENSION_CONFIG):
                raise PermissionError("Git extensions contain an unsafe setting")
            if values.get("objectformat", "sha1").casefold() not in {"sha1", "sha256"}:
                raise PermissionError("Git object format is unsupported")
            # Reftable stores references in a separate metadata tree.  The
            # contained provider deliberately supports only the traditional
            # files backend whose refs are recursively inspected below.
            if values.get("refstorage", "files").casefold() != "files":
                raise PermissionError("Git reference storage is unsupported")
        elif section == "user":
            if not set(values).issubset(_SAFE_USER_CONFIG):
                raise PermissionError("Git user configuration contains an unsafe setting")
            if any(not _safe_config_text(value, maximum=512) for value in values.values()):
                raise PermissionError("Git user configuration contains an unsafe value")
        elif match := _GIT_REMOTE_SECTION.fullmatch(raw_section):
            remote = _validate_remote(match.group(1))
            if not set(values).issubset(_SAFE_REMOTE_CONFIG):
                raise PermissionError("Git remote configuration contains an unsafe setting")
            for key in ("url", "pushurl"):
                if key in values and not _is_github_push_remote(values[key]):
                    raise PermissionError("Git remotes must use credential-free GitHub HTTPS URLs")
            if "fetch" in values:
                fetch_match = _GIT_FETCH_REFSPEC.fullmatch(values["fetch"])
                if fetch_match is None or fetch_match.group(1).casefold() != remote.casefold():
                    raise PermissionError("Git remote fetch mapping is unsupported")
        elif match := _GIT_BRANCH_SECTION.fullmatch(raw_section):
            branch = _validate_branch(match.group(1))
            if not set(values).issubset(_SAFE_BRANCH_CONFIG):
                raise PermissionError("Git branch configuration contains an unsafe setting")
            if "remote" in values and values["remote"] != ".":
                _validate_remote(values["remote"])
            if "merge" in values and values["merge"] != f"refs/heads/{branch}":
                raise PermissionError("Git branch merge target is unsupported")
            if "rebase" in values and values["rebase"].casefold() not in _SAFE_REBASE_VALUES:
                raise PermissionError("Git branch rebase setting is unsupported")
            if "description" in values and not _safe_config_text(values["description"]):
                raise PermissionError("Git branch description contains an unsafe value")
        else:
            raise PermissionError(
                f"Git configuration section {raw_section!r} is outside the safe allowlist"
            )


def _parse_git_config(payload: bytes) -> dict[str, dict[str, str]]:
    if not payload or len(payload) > _GIT_CONFIG_MAX_BYTES:
        raise PermissionError("Git configuration is empty or exceeds the safe size limit")
    try:
        text = payload.decode("utf-8")
    except UnicodeError as exc:
        raise PermissionError("Git configuration must be UTF-8") from exc
    parser = configparser.RawConfigParser(
        interpolation=None,
        strict=True,
        empty_lines_in_values=False,
    )
    parser.optionxform = str.casefold
    try:
        parser.read_string(text)
    except configparser.Error as exc:
        raise PermissionError("Git configuration is not canonical") from exc
    sections = {
        section: {key.casefold(): value.strip() for key, value in parser.items(section)}
        for section in parser.sections()
    }
    _validate_git_config(sections)
    return sections


@contextlib.contextmanager
def _locked_config_file(config_path: Path, *, writable: bool = False):
    """Open config with a Windows deny-write/delete share and hold it to process exit."""
    details = os.lstat(config_path)
    if (
        not stat.S_ISREG(details.st_mode)
        or _link_or_reparse(details)
        or int(getattr(details, "st_nlink", 1)) != 1
        or int(details.st_size) > _GIT_CONFIG_MAX_BYTES
    ):
        raise PermissionError("Git configuration must be one ordinary private file")

    if os.name == "nt":
        import msvcrt
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_file = kernel32.CreateFileW
        create_file.argtypes = (
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
            wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
        )
        create_file.restype = wintypes.HANDLE
        handle = create_file(
            str(config_path),
            0x80000000 | (0x40000000 if writable else 0),  # READ [+ WRITE]
            0 if writable else 0x00000001,  # writer is exclusive; reader denies write/delete
            None,
            3,  # OPEN_EXISTING
            0x00200000,  # FILE_FLAG_OPEN_REPARSE_POINT
            None,
        )
        invalid_handle = wintypes.HANDLE(-1).value
        if handle == invalid_handle:
            raise PermissionError("Git configuration could not be locked")
        descriptor: int | None = None
        handle_owned = True
        try:
            open_flags = (os.O_RDWR if writable else os.O_RDONLY) | os.O_BINARY
            descriptor = msvcrt.open_osfhandle(int(handle), open_flags)
            handle_owned = False
            mode = "r+b" if writable else "rb"
            with os.fdopen(descriptor, mode, closefd=True) as stream:
                descriptor = None
                opened = os.fstat(stream.fileno())
                if (
                    not stat.S_ISREG(opened.st_mode)
                    or _link_or_reparse(opened)
                    or int(getattr(opened, "st_nlink", 1)) != 1
                    or (opened.st_dev, opened.st_ino) != (details.st_dev, details.st_ino)
                ):
                    raise PermissionError("Git configuration identity changed before locking")
                yield stream
        finally:
            if descriptor is not None:
                os.close(descriptor)
            elif handle_owned:
                kernel32.CloseHandle(handle)
    else:
        flags = (os.O_RDWR if writable else os.O_RDONLY) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(config_path, flags)
        try:
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_EX if writable else fcntl.LOCK_SH)
            with os.fdopen(descriptor, "r+b" if writable else "rb", closefd=False) as stream:
                opened = os.fstat(stream.fileno())
                if (
                    not stat.S_ISREG(opened.st_mode)
                    or _link_or_reparse(opened)
                    or int(getattr(opened, "st_nlink", 1)) != 1
                    or (opened.st_dev, opened.st_ino) != (details.st_dev, details.st_ino)
                ):
                    raise PermissionError("Git configuration identity changed before locking")
                yield stream
        finally:
            os.close(descriptor)


def _git_directory(repository: Path, workspace_root: Path) -> Path:
    marker = repository / ".git"
    if not os.path.lexists(marker):
        raise ValueError("repository_path must be the root of a Git repository")
    details = os.lstat(marker)
    if not stat.S_ISDIR(details.st_mode) or _link_or_reparse(details):
        if stat.S_ISREG(details.st_mode):
            try:
                line = marker.read_text(encoding="utf-8").splitlines()[0]
                prefix, separator, raw_target = line.partition(":")
                target_path = Path(raw_target.strip())
                if not target_path.is_absolute():
                    target_path = repository / target_path
                target = target_path.resolve(strict=True)
            except (OSError, RuntimeError, UnicodeError, IndexError):
                target = marker
            if not _is_within(target, workspace_root):
                raise PermissionError("repository Git metadata must stay inside workspace_root")
        raise PermissionError("linked Git worktrees are not supported by the contained provider")
    git_dir = marker.resolve(strict=True)
    if not _is_within(git_dir, workspace_root):
        raise PermissionError("repository Git metadata must stay inside workspace_root")
    for forbidden in (
        git_dir / "commondir",
        git_dir / "config.worktree",
        git_dir / "objects" / "info" / "alternates",
        git_dir / "objects" / "info" / "http-alternates",
        git_dir / "info" / "grafts",
    ):
        if os.path.lexists(forbidden):
            raise PermissionError("Git common, alternate, or graft metadata is unsupported")
    for critical in (git_dir / "objects", git_dir / "refs"):
        _validate_git_metadata_tree(critical)
    for critical_file in (
        git_dir / "HEAD", git_dir / "index", git_dir / "packed-refs", git_dir / "shallow",
    ):
        if not os.path.lexists(critical_file):
            continue
        file_details = os.lstat(critical_file)
        if (
            not stat.S_ISREG(file_details.st_mode)
            or _link_or_reparse(file_details)
            or int(getattr(file_details, "st_nlink", 1)) != 1
        ):
            raise PermissionError("Git HEAD, index, and reference files must be ordinary")
    return git_dir


def _validate_git_metadata_tree(root: Path) -> None:
    if not os.path.lexists(root):
        return
    pending: list[tuple[Path, int]] = [(root, 0)]
    entries = 0
    total_bytes = 0
    while pending:
        path, depth = pending.pop()
        if depth > _GIT_METADATA_MAX_DEPTH:
            raise PermissionError("Git metadata tree exceeds the safe depth limit")
        details = os.lstat(path)
        entries += 1
        total_bytes += max(0, int(getattr(details, "st_size", 0)))
        if entries > _GIT_METADATA_MAX_ENTRIES or total_bytes > _GIT_METADATA_MAX_BYTES:
            raise PermissionError("Git metadata tree exceeds the safe scan limit")
        if _link_or_reparse(details):
            raise PermissionError("Git object and reference trees may not contain links")
        if stat.S_ISDIR(details.st_mode):
            with os.scandir(path) as children:
                pending.extend((Path(child.path), depth + 1) for child in children)
        elif not stat.S_ISREG(details.st_mode) or int(getattr(details, "st_nlink", 1)) != 1:
            raise PermissionError("Git object and reference trees must contain ordinary files")


@contextlib.contextmanager
def _locked_git_envelope(repository: Path, workspace_root: Path):
    git_dir = _git_directory(repository, workspace_root)
    config_path = git_dir / "config"
    with _locked_config_file(config_path) as stream:
        before_details = os.fstat(stream.fileno())
        before = stream.read(_GIT_CONFIG_MAX_BYTES + 1)
        sections = _parse_git_config(before)
        envelope = _GitRepositoryEnvelope(repository, git_dir, config_path, sections)
        yield envelope
        stream.seek(0)
        after = stream.read(_GIT_CONFIG_MAX_BYTES + 1)
        after_details = os.fstat(stream.fileno())
        if (
            after != before
            or (after_details.st_dev, after_details.st_ino) !=
            (before_details.st_dev, before_details.st_ino)
        ):
            raise PermissionError("Git configuration changed while the command was running")


def _render_git_config(sections: dict[str, dict[str, str]]) -> bytes:
    _validate_git_config(sections)
    parser = configparser.RawConfigParser(interpolation=None)
    parser.optionxform = str.casefold
    for section, values in sections.items():
        parser.add_section(section)
        for key, value in values.items():
            parser.set(section, key, value)
    output = io.StringIO()
    parser.write(output, space_around_delimiters=True)
    payload = output.getvalue().encode("utf-8")
    if len(payload) > _GIT_CONFIG_MAX_BYTES:
        raise PermissionError("Git configuration exceeds the safe size limit")
    return payload


def _update_git_config(
    repository: Path,
    workspace_root: Path,
    update: Callable[[dict[str, dict[str, str]]], None],
) -> None:
    """Apply one bounded inert config update without starting Git or a helper."""
    git_dir = _git_directory(repository, workspace_root)
    config_path = git_dir / "config"
    with _locked_config_file(config_path) as stream:
        before = stream.read(_GIT_CONFIG_MAX_BYTES + 1)
        before_details = os.fstat(stream.fileno())
        sections = _parse_git_config(before)
    mutable = {section: dict(values) for section, values in sections.items()}
    update(mutable)
    after = _render_git_config(mutable)
    lock_path = git_dir / "config.lock"
    descriptor: int | None = None
    created_lock = False
    lock_identity: tuple[int, int] | None = None
    try:
        descriptor = os.open(
            lock_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
            0o600,
        )
        created_lock = True
        lock_details = os.fstat(descriptor)
        lock_identity = (lock_details.st_dev, lock_details.st_ino)
        with os.fdopen(descriptor, "wb", closefd=True) as output:
            descriptor = None
            output.write(after)
            output.flush()
            os.fsync(output.fileno())
        with _locked_config_file(config_path) as current:
            current_details = os.fstat(current.fileno())
            if (
                current.read(_GIT_CONFIG_MAX_BYTES + 1) != before
                or (current_details.st_dev, current_details.st_ino) !=
                (before_details.st_dev, before_details.st_ino)
            ):
                raise PermissionError("Git configuration changed before the atomic update")
        replacement = os.lstat(lock_path)
        if (
            not stat.S_ISREG(replacement.st_mode)
            or _link_or_reparse(replacement)
            or int(getattr(replacement, "st_nlink", 1)) != 1
            or (replacement.st_dev, replacement.st_ino) != lock_identity
        ):
            raise PermissionError("Git configuration lock identity changed")
        os.replace(lock_path, config_path)
        created_lock = False
        with _locked_config_file(config_path) as verified:
            if verified.read(_GIT_CONFIG_MAX_BYTES + 1) != after:
                raise OSError("Atomic Git configuration update did not verify")
    except FileExistsError as exc:
        raise PermissionError("Git configuration is already locked") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if created_lock:
            try:
                current = os.lstat(lock_path)
                if (current.st_dev, current.st_ino) == lock_identity:
                    os.unlink(lock_path)
            except FileNotFoundError:
                pass


def _section_key(sections: dict[str, dict[str, str]], expected: str) -> str | None:
    folded = expected.casefold()
    return next((key for key in sections if key.casefold() == folded), None)


def _configure_github_remote(
    repository: Path,
    workspace_root: Path,
    remote: str,
    remote_url: str,
) -> None:
    if not _is_github_push_remote(remote_url):
        raise PermissionError("Local Git remotes must use credential-free GitHub HTTPS URLs")

    def update(sections: dict[str, dict[str, str]]) -> None:
        expected = f'remote "{remote}"'
        existing_key = _section_key(sections, expected)
        desired = {
            "url": remote_url,
            "fetch": f"+refs/heads/*:refs/remotes/{remote}/*",
        }
        if existing_key is not None and sections[existing_key] != desired:
            raise PermissionError("Existing Git remote differs from the approved destination")
        sections[existing_key or expected] = desired

    _update_git_config(repository, workspace_root, update)


def _configure_branch_upstream(
    repository: Path,
    workspace_root: Path,
    branch: str,
    remote: str,
) -> None:
    def update(sections: dict[str, dict[str, str]]) -> None:
        expected = f'branch "{branch}"'
        existing_key = _section_key(sections, expected)
        values = dict(sections.get(existing_key, {})) if existing_key else {}
        desired = {"remote": remote, "merge": f"refs/heads/{branch}"}
        for key, value in desired.items():
            if key in values and values[key] != value:
                raise PermissionError("Existing Git upstream differs from the approved push")
            values[key] = value
        sections[existing_key or expected] = values

    _update_git_config(repository, workspace_root, update)


def _fixed_git_path(
    git_executable: str,
    credential_helper: str | None,
) -> str:
    directories: list[Path] = [Path(git_executable).parent]
    if credential_helper:
        directories.append(Path(credential_helper).parent)
    if os.name == "nt":
        try:
            directories.append(windows_directory() / "System32")
        except (OSError, PermissionError):
            pass
        git_path = Path(git_executable)
        for parent in git_path.parents:
            if parent.name.casefold() == "git":
                directories.extend([
                    parent / "cmd", parent / "bin", parent / "mingw64" / "bin",
                    parent / "usr" / "bin",
                ])
                break
    else:
        directories.extend([Path("/usr/bin"), Path("/usr/sbin")])
    output: list[str] = []
    for directory in directories:
        value = str(directory)
        if value not in output and directory.is_dir():
            output.append(value)
    return os.pathsep.join(output)


def _git_environment(
    base: dict[str, str],
    envelope: _GitRepositoryEnvelope,
    git_executable: str,
    credential_helper: str | None,
) -> dict[str, str]:
    unsafe_names = {
        "ALL_PROXY", "COMSPEC", "CURL_CA_BUNDLE", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_ASKPASS", "GIT_ATTR_NOSYSTEM", "GIT_CEILING_DIRECTORIES",
        "GIT_COMMON_DIR", "GIT_DIR", "GIT_DISCOVERY_ACROSS_FILESYSTEM",
        "GIT_EXEC_PATH", "GIT_EXTERNAL_DIFF", "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY", "GIT_OPTIONAL_LOCKS", "GIT_PROTOCOL_FROM_USER",
        "GIT_PROXY_COMMAND", "GIT_SSH", "GIT_SSH_COMMAND", "GIT_SSH_VARIANT",
        "GIT_TEMPLATE_DIR", "GIT_WORK_TREE", "HTTP_PROXY", "HTTPS_PROXY",
        "NODE_EXTRA_CA_CERTS", "NO_PROXY", "PATH", "PATHEXT", "REQUESTS_CA_BUNDLE",
        "SSH_ASKPASS", "SSH_AUTH_SOCK", "SSL_CERT_DIR", "SSL_CERT_FILE",
    }
    environment = {
        key: value for key, value in base.items()
        if key.upper() not in unsafe_names
        and not key.upper().startswith("GIT_CONFIG")
        and not key.upper().startswith("GIT_TRACE")
    }
    environment.update({
        "GIT_ALLOW_PROTOCOL": "https",
        "GIT_ATTR_NOSYSTEM": "1",
        "GIT_CEILING_DIRECTORIES": str(envelope.repository),
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_DIR": str(envelope.git_dir),
        "GIT_COMMON_DIR": str(envelope.git_dir),
        "GIT_LFS_SKIP_SMUDGE": "1",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_OBJECT_DIRECTORY": str(envelope.git_dir / "objects"),
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_PAGER": "cat",
        "GIT_PROTOCOL_FROM_USER": "0",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_WORK_TREE": str(envelope.repository),
        "GCM_INTERACTIVE": "Never",
        "NO_COLOR": "1",
        "PAGER": "cat",
        "PATH": _fixed_git_path(git_executable, credential_helper),
    })
    if os.name == "nt":
        canonical_windows = windows_directory()
        environment.update({
            "COMSPEC": str(windows_system_executable("System32", "cmd.exe")),
            "PATHEXT": ".COM;.EXE;.BAT;.CMD",
            "SYSTEMROOT": str(canonical_windows),
            "WINDIR": str(canonical_windows),
        })
    overrides = [
        ("core.fsmonitor", "false"),
        ("core.hooksPath", "NUL" if os.name == "nt" else "/dev/null"),
        ("core.worktree", str(envelope.repository)),
        ("credential.helper", ""),
        ("credential.interactive", "false"),
        ("protocol.allow", "never"),
        ("protocol.https.allow", "always"),
        ("http.followRedirects", "false"),
        ("http.sslVerify", "true"),
        ("submodule.recurse", "false"),
        ("fetch.recurseSubmodules", "false"),
        ("push.recurseSubmodules", "no"),
        ("gc.auto", "0"),
        ("maintenance.auto", "false"),
    ]
    if credential_helper:
        safe_path = credential_helper.replace("\\", "/").replace('"', "")
        overrides.append(
            ("credential.https://github.com.helper", f'!"{safe_path}"')
        )
    environment["GIT_CONFIG_COUNT"] = str(len(overrides))
    for index, (key, value) in enumerate(overrides):
        environment[f"GIT_CONFIG_KEY_{index}"] = key
        environment[f"GIT_CONFIG_VALUE_{index}"] = value
    return environment


def _gh_environment(base: dict[str, str], gh_executable: str) -> dict[str, str]:
    unsafe = {
        "ALL_PROXY", "COMSPEC", "CURL_CA_BUNDLE", "GH_ENTERPRISE_TOKEN",
        "GH_FORCE_TTY", "GH_HOST", "GH_REPO", "GH_TOKEN",
        "GITHUB_ENTERPRISE_TOKEN", "GITHUB_TOKEN", "HTTP_PROXY", "HTTPS_PROXY",
        "NODE_EXTRA_CA_CERTS", "NO_PROXY", "PATH", "PATHEXT",
        "REQUESTS_CA_BUNDLE", "SSH_AUTH_SOCK", "SSL_CERT_DIR", "SSL_CERT_FILE",
    }
    environment = {
        key: value for key, value in base.items()
        if key.upper() not in unsafe and not key.upper().startswith("GIT_")
    }
    environment.update({
        "GH_HOST": "github.com",
        "GH_PAGER": "",
        "GH_PROMPT_DISABLED": "1",
        "NO_COLOR": "1",
        "PAGER": "",
        "PATH": _fixed_git_path(gh_executable, None),
    })
    if os.name == "nt":
        canonical_windows = windows_directory()
        environment.update({
            "COMSPEC": str(windows_system_executable("System32", "cmd.exe")),
            "PATHEXT": ".COM;.EXE;.BAT;.CMD",
            "SYSTEMROOT": str(canonical_windows),
            "WINDIR": str(canonical_windows),
        })
    return environment


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

        self.workspace_root = resolved_root
        self.timeout_seconds = float(timeout_seconds)
        self.max_output_bytes = max_output_bytes
        self._runner = runner or subprocess.run
        self._git_lock = threading.RLock()
        if which is not None:
            if runner is None:
                raise ValueError("A custom executable finder requires a custom runner")
            self._executables = {
                "gh": which("gh"),
                "git": which("git"),
            }
            self._git_credential_helper = which("git-credential-manager")
        else:
            self._executables = {
                name: _trusted_provider_executable(name, resolved_root)
                for name in ("gh", "git")
            }
            self._git_credential_helper = _trusted_provider_executable(
                "git-credential-manager", resolved_root
            )

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
            [
                "status", "--porcelain=v1", "--branch", "--untracked-files=normal",
                "--ignore-submodules=all", "--no-ahead-behind",
            ],
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
        if "/" not in slug:
            raise PermissionError(
                "An unqualified repository name requires an exact approval snapshot"
            )
        arguments = [
            "repo",
            "create",
            slug,
            f"--{visibility_value}",
        ]
        if description_value:
            arguments.extend(["--description", description_value])
        # Never give gh --source: that path makes gh start an ambient Git child
        # which can consume executable repository configuration outside this
        # provider's locked Git envelope. Local remote setup remains a separate,
        # explicitly approved operation.
        command = self._run("gh", arguments, cwd=self.workspace_root)
        remote_configured = False
        configuration_error = None
        if command.ok:
            try:
                with self._git_lock:
                    _configure_github_remote(
                        repository,
                        self.workspace_root,
                        remote_value,
                        f"https://github.com/{slug}.git",
                    )
                remote_configured = True
            except (OSError, ValueError, PermissionError) as exc:
                configuration_error = (
                    "GitHub repository was created, but the bounded local remote "
                    f"could not be configured: {type(exc).__name__}"
                )
        return self._public_result(
            "create_repository",
            command,
            data={
                "repository_path": str(repository),
                "name": slug,
                "visibility": visibility_value,
                "remote": remote_value,
                "remote_configured": remote_configured,
                "pushed": False,
            },
            error=configuration_error,
            ok=command.ok and remote_configured,
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
        if expected_remote_url is None:
            raise PermissionError("Git push requires an exact approved destination snapshot")
        if (
            not isinstance(expected_remote_url, str)
            or not _is_github_push_remote(expected_remote_url)
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
        # Use the approved concrete HTTPS destination and object ID. Never let Git
        # re-resolve a named remote or branch after authorization.
        arguments = [
            "push", "--no-verify", expected_remote_url,
            f"{expected_tip_sha.casefold()}:refs/heads/{branch_value}",
        ]
        command = self._run("git", arguments, cwd=repository)
        upstream_error = None
        if command.ok and set_upstream:
            try:
                with self._git_lock:
                    _configure_branch_upstream(
                        repository,
                        self.workspace_root,
                        branch_value,
                        remote_value,
                    )
            except (OSError, ValueError, PermissionError) as exc:
                upstream_error = type(exc).__name__
        with self._git_lock, _locked_git_envelope(
            repository, self.workspace_root
        ) as envelope:
            key = _section_key(envelope.sections, f'branch "{branch_value}"')
            branch_section = envelope.sections.get(key, {}) if key else {}
            upstream_configured = (
                branch_section.get("remote") == remote_value
                and branch_section.get("merge") == f"refs/heads/{branch_value}"
            )
        return self._public_result(
            "push",
            command,
            data={
                "repository_path": str(repository),
                "branch": branch_value,
                "remote": remote_value,
                "set_upstream": set_upstream,
                "upstream_configured": upstream_configured,
                "upstream_error": upstream_error,
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
        with self._git_lock, _locked_git_envelope(
            repository, self.workspace_root
        ) as envelope:
            key = _section_key(envelope.sections, f'remote "{remote_value}"')
            remote_section = envelope.sections.get(key) if key else None
            if remote_section is None:
                raise ValueError("Git push remote could not be resolved exactly")
            remote_url = remote_section.get("pushurl", remote_section.get("url", ""))
        if not _is_github_push_remote(remote_url):
            raise ValueError("Git push requires exactly one credential-free push destination")
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
            "remote_url": remote_url,
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
        with self._git_lock, _locked_git_envelope(resolved, self.workspace_root):
            pass
        return resolved

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
        environment = trusted_cli_environment(include_ssh_agent=False)
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
        actual_cwd = cwd
        if executable == "gh":
            environment = _gh_environment(environment, str(executable_path))
            install_directory = Path(str(executable_path)).parent
            if install_directory.is_dir():
                actual_cwd = install_directory
        try:
            with contextlib.ExitStack() as stack:
                if executable == "git":
                    stack.enter_context(self._git_lock)
                    envelope = stack.enter_context(
                        _locked_git_envelope(cwd, self.workspace_root)
                    )
                    environment = _git_environment(
                        environment,
                        envelope,
                        str(executable_path),
                        self._git_credential_helper,
                    )
                completed = self._runner(
                    argv,
                    cwd=str(actual_cwd),
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
        except (OSError, subprocess.SubprocessError, ValueError, PermissionError) as exc:
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
        isinstance(value, str)
        and len(value) <= 160
        and "[redacted]" not in value
        and _URL_USERINFO_PATTERN.search(value) is None
        and not contains_secret(value)
        and not _unsafe_http_remote(value)
        and _GITHUB_HTTPS_REMOTE.fullmatch(value)
        and not any(ord(character) < 32 or ord(character) == 127 for character in value)
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
