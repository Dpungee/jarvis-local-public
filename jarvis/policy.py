from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Iterable


ALLOWED_PROGRAMS = frozenset({
    "cargo",
    "cmake",
    "ctest",
    "dotnet",
    "git",
    "go",
    "java",
    "javac",
    "mypy",
    "node",
    "npm",
    "py",
    "pytest",
    "python",
    "python3",
    "ruff",
    "rustc",
})

_WINDOWS_DEVICES = frozenset({
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
})
_ENV_REFERENCE = re.compile(
    r"%[^%\r\n]+%|\$(?:env:)?[A-Za-z_][A-Za-z0-9_]*|\$\{[^}\r\n]+\}",
    re.I,
)
_URL_REFERENCE = re.compile(r"[a-z][a-z0-9+.-]*://", re.I)
_PYTHON_INLINE = frozenset({"-c", "-"})
_NODE_INLINE = frozenset({
    "-",
    "-e",
    "-p",
    "-r",
    "--cpu-prof",
    "--eval",
    "--experimental-loader",
    "--heap-prof",
    "--import",
    "--inspect",
    "--inspect-brk",
    "--inspect-port",
    "--loader",
    "--print",
    "--prof",
    "--require",
    "--watch",
})
_PYTHON_MODULES = frozenset({"compileall", "pytest", "unittest"})
_GIT_READ_ONLY = frozenset({"diff", "log", "ls-files", "rev-parse", "show", "status"})
_NPM_SCRIPTS = frozenset({"build", "check", "lint", "test", "typecheck"})
_CARGO_ACTIONS = frozenset({"build", "check", "clippy", "test"})
_GO_ACTIONS = frozenset({"build", "test", "vet"})
_DOTNET_ACTIONS = frozenset({"build", "test"})
_DRIVE_RELATIVE = re.compile(r"^[A-Za-z]:[^/\\]")

_GIT_DANGEROUS = (
    "-c",
    "--config-env",
    "--exec-path",
    "--upload-pack",
    "--receive-pack",
)
_GIT_OUTPUT_OR_HOOKS = frozenset({"--paginate", "--output", "--ext-diff", "--textconv"})
_NPM_DANGEROUS_OPTIONS = frozenset({
    "-g",
    "--ca",
    "--cafile",
    "--cert",
    "--config",
    "--dry-run",
    "--global",
    "--globalconfig",
    "--https-proxy",
    "--if-present",
    "--ignore-scripts",
    "--key",
    "--location",
    "--noproxy",
    "--proxy",
    "--registry",
    "--script-shell",
    "--strict-ssl",
    "--userconfig",
})
_CARGO_DANGEROUS_OPTIONS = frozenset({
    "--allow-dirty",
    "--allow-staged",
    "--broken-code",
    "--config",
    "--fix",
    "--index",
    "--no-run",
    "--registry",
})
_GO_DANGEROUS_OPTIONS = frozenset({
    "-c", "--c", "-exec", "--exec", "-list", "--list",
    "-overlay", "--overlay",
    "-toolexec", "--toolexec",
})
_RUFF_DANGEROUS_OPTIONS = frozenset({
    "--add-noqa",
    "--diff",
    "--exit-zero",
    "--fix-only",
    "--fix",
    "--help",
    "--output-file",
    "--show-files",
    "--show-settings",
    "--unsafe-fixes",
    "--version",
    "--watch",
})
_MYPY_DANGEROUS_OPTIONS = frozenset({"--help", "--install-types", "--version"})
_CTEST_DANGEROUS_OPTIONS = frozenset({
    "-d",
    "-h",
    "-m",
    "-n",
    "-s",
    "-sp",
    "-t",
    "--dashboard",
    "--extra-submit",
    "--help",
    "--http-header",
    "--overwrite",
    "--print-labels",
    "--script",
    "--show-only",
    "--submit-index",
    "--output-junit",
    "--output-log",
    "--test-action",
    "--upload-file",
    "--version",
})
_DOTNET_DANGEROUS_OPTIONS = frozenset({
    "-t", "--list-tests", "--list-tests=true",
})
_PYTEST_DANGEROUS_OPTIONS = frozenset({
    "--basetemp",
    "--cache-clear",
    "--cache-show",
    "--co",
    "--collect-only",
    "--fixtures",
    "--fixtures-per-test",
    "--help",
    "--junit-xml",
    "--junitxml",
    "--markers",
    "--result-log",
    "--setup-only",
    "--setup-plan",
    "--trace-config",
    "--version",
})
_JAVA_HOOK_OPTIONS = frozenset({
    "-agentlib",
    "-agentpath",
    "-javaagent",
    "-xrun",
})
def _strip_paired_quotes(value: str) -> str:
    """Remove balanced quote layers for security inspection only."""
    inspected = value.strip()
    for _ in range(4):
        if len(inspected) < 2 or inspected[0] != inspected[-1] or inspected[0] not in {"'", '"'}:
            break
        inspected = inspected[1:-1].strip()
    return inspected


def _inspection_values(value: str) -> tuple[str, ...]:
    inspected = _strip_paired_quotes(value)
    if inspected.startswith("-") and "=" in inspected:
        return inspected, _strip_paired_quotes(inspected.split("=", 1)[1])
    return (inspected,)


def _option_name(value: str) -> str:
    return _strip_paired_quotes(value).split("=", 1)[0].casefold()



def _program_name(program: str) -> str:
    name = Path(program).name.casefold()
    for suffix in (".exe", ".cmd", ".bat", ".com"):
        if name.endswith(suffix):
            name = name[:-len(suffix)]
            break
    return name


def _has_workspace_escape(value: str, workspace: Path) -> bool:
    candidate = _strip_paired_quotes(value)
    if "=" in candidate and candidate.startswith("-"):
        candidate = _strip_paired_quotes(candidate.split("=", 1)[1])
    if not candidate:
        return False
    normalized = candidate.replace("/", os.sep).replace("\\", os.sep)
    path = Path(normalized)
    looks_like_path = (
        path.is_absolute()
        or candidate.startswith((".", "~", "/", "\\"))
        or "/" in candidate
        or "\\" in candidate
    )
    if not looks_like_path:
        return False
    try:
        workspace_root = workspace.resolve()
        resolved = (workspace_root / path).resolve() if not path.is_absolute() else path.resolve()
        resolved.relative_to(workspace_root)
    except (OSError, ValueError):
        return True
    return False


def _has_windows_device_or_ads(value: str) -> bool:
    candidate = _strip_paired_quotes(value)
    if candidate.startswith("-") and "=" in candidate:
        candidate = _strip_paired_quotes(candidate.split("=", 1)[1])
    if not candidate:
        return False

    _drive, tail = os.path.splitdrive(candidate)
    if ":" in tail:
        return True
    for part in re.split(r"[/\\]", tail):
        cleaned = _strip_paired_quotes(part).rstrip(" .")
        if not cleaned:
            continue
        if cleaned.split(".", 1)[0].upper() in _WINDOWS_DEVICES:
            return True
    return False


def validate_process(
    workspace: Path,
    program: str,
    arguments: Iterable[str],
) -> tuple[bool, str]:
    """Validate a direct process invocation without accepting shell syntax."""
    if not isinstance(program, str) or not program.strip():
        return False, "program is required"
    if any(char in program for char in "\x00\r\n"):
        return False, "control characters are blocked"
    if ":" in program or _DRIVE_RELATIVE.match(program):
        return False, "drive-relative and alternate-stream executable names are blocked"
    name = _program_name(program)
    if name not in ALLOWED_PROGRAMS:
        return False, f"program '{name or program}' is not on the build-tool allowlist"

    raw_program = Path(program)
    if raw_program.is_absolute() or any(sep in program for sep in ("/", "\\")):
        return False, "explicit executable paths are blocked; use an allowlisted program name"

    if isinstance(arguments, (str, bytes)):
        return False, "arguments must be a sequence of strings"
    try:
        args = list(arguments)
    except TypeError:
        return False, "arguments must be a sequence of strings"
    if any(not isinstance(item, str) for item in args):
        return False, "process arguments must be strings"
    if len(args) > 256 or sum(len(item) for item in args) > 16_000:
        return False, "process argument limit exceeded"
    inspected_args = [_strip_paired_quotes(item) for item in args]
    lowered = [item.casefold() for item in inspected_args]
    option_names = [_option_name(item) for item in args]

    for arg in args:
        if any(char in arg for char in "\x00\r\n"):
            return False, "control characters are blocked"
        for inspected in _inspection_values(arg):
            if any(char in inspected for char in "\x00\r\n"):
                return False, "control characters are blocked"
            if inspected.startswith("@"):
                return False, "response-file arguments are blocked"
            if _ENV_REFERENCE.search(inspected) or inspected.startswith("~"):
                return False, "environment and home-directory expansion is blocked"
            if _URL_REFERENCE.search(inspected):
                return False, "direct process network URLs are blocked"
            if _DRIVE_RELATIVE.match(inspected):
                return False, "drive-relative paths are blocked"
            if inspected.startswith(("\\\\", "//", "\\\\.\\", "\\\\?\\")):
                return False, "UNC and Windows device paths are blocked"
            if _has_windows_device_or_ads(inspected):
                return False, "alternate data streams and Windows device paths are blocked"
            if _has_workspace_escape(inspected, workspace):
                return False, "process paths must stay inside the workspace"

    python_module: str | None = None
    python_module_option_names: list[str] = []
    if name in {"python", "python3", "py"}:
        if not lowered:
            return False, "interactive Python execution is blocked"
        for inspected, item in zip(inspected_args, lowered, strict=True):
            interactive_cluster = (
                inspected.startswith("-")
                and not inspected.startswith(("--", "-W", "-X"))
                and "i" in inspected[1:]
            )
            if interactive_cluster:
                return False, "interactive Python execution is blocked"
            if item in _PYTHON_INLINE or item.startswith("-c="):
                return False, "inline Python and stdin execution are blocked"
            if item.startswith("-") and not item.startswith("--") and item != "-m":
                if any(flag in item[1:] for flag in ("c", "m")):
                    return False, "clustered Python command and module flags are blocked"
        if "-m" in lowered:
            module_index = lowered.index("-m") + 1
            if module_index >= len(lowered) or lowered[module_index] not in _PYTHON_MODULES:
                return False, "only unittest, pytest, and compileall Python modules are allowed"
            python_module = lowered[module_index]
            python_module_option_names = [
                _option_name(item) for item in args[module_index + 1:]
            ]
        elif not any(not item.startswith("-") for item in lowered) and not any(
            item in {"-V", "--version"} for item in inspected_args
        ):
            return False, "Python requires a local script, approved module, or version query"
        elif any(item.startswith("-m") for item in lowered):
            return False, "invalid Python module syntax is blocked"

    pytest_options = (
        python_module_option_names
        if python_module == "pytest"
        else option_names if name == "pytest" else []
    )
    if any(option in _PYTEST_DANGEROUS_OPTIONS for option in pytest_options):
        return False, "pytest collection, cache mutation, output-file, and metadata-only modes are blocked"

    if name == "node":
        if not lowered:
            return False, "interactive Node.js execution is blocked"
        if any(option in _NODE_INLINE for option in option_names):
            return False, "inline Node.js execution and preload hooks are blocked"
        if any(
            item.startswith("-")
            and not item.startswith("--")
            and any(flag in item[1:] for flag in ("e", "p", "r"))
            for item in lowered
        ):
            return False, "clustered Node.js execution and preload flags are blocked"

    if name == "git":
        if any(option in _GIT_DANGEROUS for option in option_names):
            return False, "Git config and executable-hook overrides are blocked"
        if lowered and lowered[0] in {"credential", "credential-cache", "credential-store"}:
            return False, "Git credential access is blocked"
        if not lowered or lowered[0] not in _GIT_READ_ONLY:
            return False, "only read-only Git inspection subcommands are allowed"
        if any(option in _GIT_OUTPUT_OR_HOOKS for option in option_names):
            return False, "Git output files, pagers, and external diff hooks are blocked"

    if name == "npm":
        allowed_npm = bool(lowered) and (
            lowered[0] == "test"
            or lowered[0] == "run" and len(lowered) > 1 and lowered[1] in _NPM_SCRIPTS
        )
        if not allowed_npm:
            return False, "only approved local npm build, lint, typecheck, and test scripts are allowed"
        if any(option in _NPM_DANGEROUS_OPTIONS for option in option_names):
            return False, "npm shell, registry, proxy, certificate, and no-op overrides are blocked"

    if name == "cargo":
        if not lowered or lowered[0] not in _CARGO_ACTIONS:
            return False, "only local Cargo build and verification actions are allowed"
        if any(option in _CARGO_DANGEROUS_OPTIONS for option in option_names):
            return False, "Cargo config, registry, index, and no-run modes are blocked"

    if name == "go":
        if not lowered or lowered[0] not in _GO_ACTIONS:
            return False, "only local Go build and verification actions are allowed"
        if any(option in _GO_DANGEROUS_OPTIONS for option in option_names):
            return False, "Go hooks, overlays, compilation-only, and list-only modes are blocked"
        for index, item in enumerate(lowered[1:], start=1):
            option, separator, attached = item.partition("=")
            if option not in {"-count", "--count"}:
                continue
            value = attached if separator else (
                lowered[index + 1] if index + 1 < len(lowered) else ""
            )
            if value.strip() == "0":
                return False, "Go zero-count test mode is blocked"

    if name == "dotnet":
        if not lowered or lowered[0] not in _DOTNET_ACTIONS:
            return False, "only local dotnet build and test actions are allowed"
        if any(option in _DOTNET_DANGEROUS_OPTIONS for option in option_names):
            return False, "dotnet list-only test mode is blocked"

    if name == "java" and any(
        item == hook
        or item.startswith(f"{hook}:")
        or item.startswith(f"{hook}=")
        for item in lowered
        for hook in _JAVA_HOOK_OPTIONS
    ):
        return False, "Java agent and native hook options are blocked"

    if name == "cmake":
        cmake_dangerous = {"-e", "-p", "--find-package", "--install", "--open", "--workflow"}
        if any(option in cmake_dangerous for option in option_names):
            return False, "CMake command, script, install, GUI, and workflow modes are blocked"
        for index, option in enumerate(option_names):
            if option in {"-t", "--target"}:
                target = lowered[index + 1] if index + 1 < len(lowered) else ""
                if target in {"clean", "install"}:
                    return False, "mutating CMake clean and install targets are blocked"
            if option in {"-t", "--target"} and "=" in inspected_args[index]:
                target = _strip_paired_quotes(inspected_args[index].split("=", 1)[1]).casefold()
                if target in {"clean", "install"}:
                    return False, "mutating CMake clean and install targets are blocked"
        has_source = any(
            item == "-S" or (item.startswith("-S") and len(item) > 2)
            for item in inspected_args
        )
        has_build_dir = any(
            item == "-B" or (item.startswith("-B") and len(item) > 2)
            for item in inspected_args
        )
        if not (lowered and lowered[0] == "--build") and not (has_source and has_build_dir):
            return False, "only CMake build mode or explicit -S/-B configure mode is allowed"

    if name == "ctest":
        if any(option in _CTEST_DANGEROUS_OPTIONS for option in option_names):
            return False, "CTest scripts, dashboards, uploads, and list-only modes are blocked"
        if any(
            item.startswith(("-d", "-m", "-s", "-t")) and not item.startswith("--")
            for item in lowered
        ):
            return False, "clustered CTest script and dashboard modes are blocked"

    if name == "ruff":
        if not lowered or lowered[0] != "check":
            return False, "only Ruff check mode is allowed"
        if any(option in _RUFF_DANGEROUS_OPTIONS for option in option_names):
            return False, "Ruff mutation and no-op output modes are blocked"

    if name == "mypy":
        if not lowered:
            return False, "mypy requires an explicit local target"
        if any(option in _MYPY_DANGEROUS_OPTIONS for option in option_names):
            return False, "mypy install/network and no-op modes are blocked"

    return True, "allowed"


def validate_command(command: str) -> tuple[bool, str]:
    """Compatibility shim: arbitrary shell strings are never safe to execute."""
    return False, "raw shell commands are disabled; use a structured run_process call"


def resolve_workspace_path(workspace: Path, user_path: str | Path) -> Path:
    raw_text = os.fspath(user_path)
    if not raw_text or "\x00" in raw_text:
        raise PermissionError("Invalid workspace path")
    if raw_text.startswith(("\\\\", "//", "\\\\.\\", "\\\\?\\")):
        raise PermissionError("UNC and Windows device paths are blocked")
    _drive, tail = os.path.splitdrive(raw_text)
    if ":" in tail:
        raise PermissionError("Alternate data streams and device paths are blocked")
    for part in Path(tail).parts:
        stem = part.rstrip(" .").split(".", 1)[0].upper()
        if stem in _WINDOWS_DEVICES:
            raise PermissionError("Windows device paths are blocked")

    raw = Path(raw_text)
    candidate = (workspace / raw).resolve() if not raw.is_absolute() else raw.resolve()
    try:
        candidate.relative_to(workspace.resolve())
    except ValueError as exc:
        raise PermissionError(f"Path must stay inside the Jarvis workspace: {workspace}") from exc
    return candidate

