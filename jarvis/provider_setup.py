from __future__ import annotations

import argparse
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TextIO

from .config import MAX_DOTENV_BYTES, ROOT
from .model_client import (
    CODEX_CLI_AUTH_OVERRIDES,
    ModelProviderError,
    isolated_codex_cli_home,
    resolve_claude_cli_executable,
    resolve_codex_cli_executable,
)
from .subprocess_env import trusted_cli_environment


SETUP_NEEDED_MESSAGE = (
    "Jarvis model-provider setup is required. Run "
    "`python -m jarvis.provider_setup --interactive` in an interactive terminal."
)

_MANAGED_KEYS = (
    "JARVIS_CODEX_CLI_ENABLED",
    "JARVIS_CLAUDE_CLI_ENABLED",
    "JARVIS_OPENAI_API_ENABLED",
    "JARVIS_ANTHROPIC_API_ENABLED",
    "JARVIS_CLOUD_ENABLED",
    "JARVIS_OLLAMA_ENABLED",
    "JARVIS_MODEL",
    "JARVIS_FAST_MODEL",
    "JARVIS_REASONING_MODEL",
    "JARVIS_CODING_MODEL",
    "JARVIS_DEEP_MODEL",
    "JARVIS_BACKGROUND_MODEL",
    "JARVIS_LEARNING_MODEL",
)
_MODEL_ENVIRONMENT_KEYS = frozenset({
    "JARVIS_MODEL",
    "JARVIS_FAST_MODEL",
    "JARVIS_REASONING_MODEL",
    "JARVIS_CODING_MODEL",
    "JARVIS_DEEP_MODEL",
    "JARVIS_BACKGROUND_MODEL",
    "JARVIS_LEARNING_MODEL",
    "JARVIS_CODEX_CLI_ENABLED",
    "JARVIS_CLAUDE_CLI_ENABLED",
    "JARVIS_OLLAMA_ENABLED",
})
_KEY_PATTERN = re.compile(r"^\s*([A-Z][A-Z0-9_]*)\s*=")
_WINDOWS_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_CHOICES = frozenset({"codex", "claude", "both"})
_AUTH_STATUS_MAX_BYTES = 4096


class ProviderSetupError(RuntimeError):
    """The one-time provider setup could not be completed safely."""


class ProviderSetupRequired(ProviderSetupError):
    """A foreground operator must complete provider setup before Jarvis starts."""


@dataclass(frozen=True)
class CLIProbe:
    provider: str
    installed: bool
    runnable: bool
    authenticated: bool
    executable: Path | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True)
class ProviderSetupResult:
    state: str
    choice: str | None = None
    env_path: Path | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True)
class _ProviderSpec:
    label: str
    command: str
    package_id: str
    auth_status: tuple[str, ...]
    auth_login: tuple[str, ...]
    documentation: str


_PROVIDERS = {
    "codex": _ProviderSpec(
        label="Codex CLI (ChatGPT subscription)",
        command="codex",
        package_id="OpenAI.Codex",
        auth_status=("login", "status"),
        auth_login=("login",),
        documentation="https://developers.openai.com/codex/cli/",
    ),
    "claude": _ProviderSpec(
        label="Claude CLI (Claude subscription)",
        command="claude",
        package_id="Anthropic.ClaudeCode",
        auth_status=("auth", "status", "--json"),
        auth_login=("auth", "login"),
        documentation="https://code.claude.com/docs/en/setup",
    ),
}


def _root_path(root: Path | None) -> Path:
    return Path(ROOT if root is None else root).resolve()


def _codex_home(
    root: Path | None,
    environ: Mapping[str, str],
) -> Path:
    configured_data = str(environ.get("JARVIS_DATA", "")).strip()
    data_dir = (
        Path(configured_data).expanduser()
        if configured_data
        else _root_path(root) / "data"
    )
    try:
        return isolated_codex_cli_home(data_dir)
    except ModelProviderError as exc:
        raise ProviderSetupError(str(exc)) from exc


def _provider_environment(
    provider: str,
    root: Path | None,
    environ: Mapping[str, str],
) -> dict[str, str]:
    environment = trusted_cli_environment(environ, include_ssh_agent=False)
    if provider == "codex":
        environment["CODEX_HOME"] = str(_codex_home(root, environ))
    return environment


def _ordinary_file(path: Path) -> bool:
    try:
        details = os.lstat(path)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise ProviderSetupError("Jarvis could not inspect its provider configuration.") from exc
    attributes = getattr(details, "st_file_attributes", 0)
    if (
        stat.S_ISLNK(details.st_mode)
        or attributes & _WINDOWS_REPARSE_POINT
        or not stat.S_ISREG(details.st_mode)
    ):
        raise ProviderSetupError("Jarvis .env must be an ordinary non-link file.")
    if details.st_size > MAX_DOTENV_BYTES:
        raise ProviderSetupError(f"Jarvis .env exceeds {MAX_DOTENV_BYTES} bytes.")
    return True


def is_setup_complete(
    root: Path | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> bool:
    """Return whether first-run setup is complete, preserving historical installs.

    Existing .env files, explicit process-level model settings, and an existing
    Jarvis database are migration evidence. Their contents are never rewritten by
    the automatic first-run path.
    """
    base = _root_path(root)
    env_path = base / ".env"
    if os.path.lexists(env_path):
        _ordinary_file(env_path)
        return True

    values = os.environ if environ is None else environ
    if any(str(values.get(key, "")).strip() for key in _MODEL_ENVIRONMENT_KEYS):
        return True
    if any(key in values for key in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY")):
        return True

    configured_data = str(values.get("JARVIS_DATA", "")).strip()
    data_dir = Path(configured_data).expanduser() if configured_data else base / "data"
    return (data_dir / "jarvis.db").is_file()


def _provider_values(choice: str) -> dict[str, str]:
    normalized = str(choice).strip().casefold()
    if normalized not in _CHOICES:
        raise ValueError("provider choice must be codex, claude, or both")
    if normalized == "codex":
        profiles = {
            "JARVIS_FAST_MODEL": "codex-cli:gpt-5.6-luna",
            "JARVIS_REASONING_MODEL": "codex-cli:gpt-5.6-terra",
            "JARVIS_CODING_MODEL": "codex-cli:gpt-5.6-sol",
            "JARVIS_DEEP_MODEL": "codex-cli:gpt-5.6-sol",
            "JARVIS_BACKGROUND_MODEL": "codex-cli:gpt-5.6-luna",
            "JARVIS_LEARNING_MODEL": "codex-cli:gpt-5.6-luna",
        }
    elif normalized == "claude":
        profiles = {
            "JARVIS_FAST_MODEL": "claude-cli:haiku",
            "JARVIS_REASONING_MODEL": "claude-cli:sonnet",
            "JARVIS_CODING_MODEL": "claude-cli:sonnet",
            "JARVIS_DEEP_MODEL": "claude-cli:sonnet",
            "JARVIS_BACKGROUND_MODEL": "claude-cli:haiku",
            "JARVIS_LEARNING_MODEL": "claude-cli:haiku",
        }
    else:
        profiles = {
            "JARVIS_FAST_MODEL": "claude-cli:haiku",
            "JARVIS_REASONING_MODEL": "claude-cli:sonnet",
            "JARVIS_CODING_MODEL": "codex-cli:gpt-5.6-sol",
            "JARVIS_DEEP_MODEL": "codex-cli:gpt-5.6-sol",
            "JARVIS_BACKGROUND_MODEL": "claude-cli:haiku",
            "JARVIS_LEARNING_MODEL": "claude-cli:haiku",
        }
    return {
        "JARVIS_CODEX_CLI_ENABLED": "true" if normalized in {"codex", "both"} else "false",
        "JARVIS_CLAUDE_CLI_ENABLED": "true" if normalized in {"claude", "both"} else "false",
        # Subscription choices must not silently fail over to separately billed
        # API keys that happen to exist in the parent process environment.
        "JARVIS_OPENAI_API_ENABLED": "false",
        "JARVIS_ANTHROPIC_API_ENABLED": "false",
        "JARVIS_CLOUD_ENABLED": "true",
        "JARVIS_OLLAMA_ENABLED": "false",
        "JARVIS_MODEL": "auto",
        **profiles,
    }


def _render_env(existing: str, updates: Mapping[str, str]) -> str:
    newline = "\r\n" if "\r\n" in existing else "\n"
    had_final_newline = existing.endswith(("\n", "\r"))
    lines = existing.splitlines()
    rendered: list[str] = []
    written: set[str] = set()
    for line in lines:
        match = _KEY_PATTERN.match(line)
        key = match.group(1) if match else None
        if key not in updates:
            rendered.append(line)
            continue
        if key not in written:
            rendered.append(f"{key}={updates[key]}")
            written.add(key)
    missing = [key for key in _MANAGED_KEYS if key in updates and key not in written]
    if missing:
        if rendered and rendered[-1].strip():
            rendered.append("")
        rendered.append("# Model providers selected by the Jarvis first-run setup.")
        rendered.extend(f"{key}={updates[key]}" for key in missing)
    output = newline.join(rendered)
    if output and (had_final_newline or missing):
        output += newline
    return output


def persist_provider_choice(choice: str, root: Path | None = None) -> Path:
    """Atomically update only Jarvis provider-routing keys in .env."""
    base = _root_path(root)
    base.mkdir(parents=True, exist_ok=True)
    env_path = base / ".env"
    existing = ""
    existing_mode: int | None = None
    if os.path.lexists(env_path):
        _ordinary_file(env_path)
        try:
            existing = env_path.read_text(encoding="utf-8")
            existing_mode = stat.S_IMODE(env_path.stat().st_mode)
        except (OSError, UnicodeError) as exc:
            raise ProviderSetupError("Jarvis could not read its existing .env safely.") from exc
    rendered = _render_env(existing, _provider_values(choice))
    encoded = rendered.encode("utf-8")
    if len(encoded) > MAX_DOTENV_BYTES:
        raise ProviderSetupError(f"Updated Jarvis .env would exceed {MAX_DOTENV_BYTES} bytes.")

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".jarvis-provider-", suffix=".tmp", dir=base
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as destination:
            destination.write(encoded)
            destination.flush()
            os.fsync(destination.fileno())
        if existing_mode is not None:
            os.chmod(temporary, existing_mode)
        os.replace(temporary, env_path)
    except BaseException:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return env_path


def _native_candidates(command: str, environ: Mapping[str, str]) -> list[Path]:
    names = [f"{command}.exe"] if os.name == "nt" else [command]
    candidates: list[Path] = []
    seen: set[str] = set()
    # Keep first-run detection aligned with the runtime's stricter native-binary
    # resolution. In particular, the Codex desktop app's PATH alias can be
    # non-launchable while its private, signed app-server binary is usable.
    if environ is os.environ:
        resolved_by_runtime = (
            resolve_codex_cli_executable()
            if command == "codex"
            else resolve_claude_cli_executable()
        )
        if resolved_by_runtime is not None:
            candidates.append(resolved_by_runtime)
    path_value = str(environ.get("PATH", ""))
    for name in names:
        found = shutil.which(name, path=path_value)
        if found:
            candidates.append(Path(found))
        for directory in path_value.split(os.pathsep):
            if directory.strip():
                candidates.append(Path(directory.strip().strip('"')) / name)
    if os.name == "nt":
        appdata = str(environ.get("APPDATA", "")).strip()
        localappdata = str(environ.get("LOCALAPPDATA", "")).strip()
        if command == "claude" and appdata:
            candidates.insert(
                0,
                Path(appdata)
                / "npm"
                / "node_modules"
                / "@anthropic-ai"
                / "claude-code"
                / "bin"
                / "claude.exe",
            )
        if localappdata:
            candidates.append(Path(localappdata) / "Microsoft" / "WinGet" / "Links" / names[0])

    resolved_candidates: list[Path] = []
    for candidate in candidates:
        try:
            resolved = candidate.resolve(strict=True)
            details = os.lstat(resolved)
        except OSError:
            continue
        attributes = getattr(details, "st_file_attributes", 0)
        if not stat.S_ISREG(details.st_mode) or attributes & _WINDOWS_REPARSE_POINT:
            continue
        if os.name == "nt" and resolved.suffix.casefold() != ".exe":
            continue
        if os.name != "nt" and not os.access(resolved, os.X_OK):
            continue
        identity = str(resolved).casefold()
        if identity not in seen:
            seen.add(identity)
            resolved_candidates.append(resolved)
    return resolved_candidates


def detect_provider(
    provider: str,
    *,
    root: Path | None = None,
    environ: Mapping[str, str] | None = None,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> CLIProbe:
    """Detect a CLI and authentication solely through its documented status command."""
    normalized = str(provider).strip().casefold()
    try:
        spec = _PROVIDERS[normalized]
    except KeyError as exc:
        raise ValueError("unknown provider") from exc
    values = os.environ if environ is None else environ
    candidates = _native_candidates(spec.command, values)
    for executable in candidates:
        arguments = [str(executable)]
        if normalized == "codex":
            arguments.extend((
                *(item for override in CODEX_CLI_AUTH_OVERRIDES for item in ("--config", override)),
            ))
        arguments.extend(spec.auth_status)
        options = {
            "stdin": subprocess.DEVNULL,
            "timeout": 20,
            "check": False,
            "env": _provider_environment(normalized, root, values),
            "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0),
        }
        try:
            if normalized == "codex":
                status_output: bytes | None
                if runner is subprocess.run:
                    # File-backed capture keeps authentication text out of logs and
                    # bounds what is ever loaded into memory. Oversized output is
                    # rejected rather than interpreted.
                    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
                        completed = runner(
                            arguments,
                            stdout=stdout,
                            stderr=stderr,
                            **options,
                        )
                        stdout_size = os.fstat(stdout.fileno()).st_size
                        stderr_size = os.fstat(stderr.fileno()).st_size
                        if stdout_size + stderr_size > _AUTH_STATUS_MAX_BYTES:
                            status_output = None
                        else:
                            stdout.seek(0)
                            stderr.seek(0)
                            status_output = stdout.read() + b"\n" + stderr.read()
                else:
                    completed = runner(
                        arguments,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        **options,
                    )
                    raw_stdout = completed.stdout if isinstance(completed.stdout, (str, bytes)) else b""
                    raw_stderr = completed.stderr if isinstance(completed.stderr, (str, bytes)) else b""
                    stdout_bytes = (
                        raw_stdout.encode("utf-8", errors="replace")
                        if isinstance(raw_stdout, str)
                        else raw_stdout
                    )
                    stderr_bytes = (
                        raw_stderr.encode("utf-8", errors="replace")
                        if isinstance(raw_stderr, str)
                        else raw_stderr
                    )
                    combined = stdout_bytes + b"\n" + stderr_bytes
                    status_output = (
                        combined if len(combined) <= _AUTH_STATUS_MAX_BYTES else None
                    )
                authenticated = bool(
                    completed.returncode == 0
                    and status_output is not None
                    and b"logged in using chatgpt"
                    in {line.strip().lower() for line in status_output.splitlines()}
                )
            else:
                completed = runner(
                    arguments,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    **options,
                )
                authenticated = completed.returncode == 0
        except (OSError, subprocess.SubprocessError):
            continue
        return CLIProbe(
            provider=normalized,
            installed=True,
            runnable=True,
            authenticated=authenticated,
            executable=executable,
        )
    return CLIProbe(
        provider=normalized,
        installed=bool(candidates),
        runnable=False,
        authenticated=False,
    )


def _answer_yes(prompt: str, *, input_fn: Callable[[str], str]) -> bool:
    try:
        return input_fn(prompt).strip().casefold() in {"y", "yes"}
    except (EOFError, KeyboardInterrupt) as exc:
        raise ProviderSetupRequired(SETUP_NEEDED_MESSAGE) from exc


def _install_provider(
    provider: str,
    *,
    environ: Mapping[str, str],
    runner: Callable[..., subprocess.CompletedProcess],
) -> bool:
    if os.name != "nt":
        return False
    winget = shutil.which("winget.exe", path=str(environ.get("PATH", ""))) or shutil.which(
        "winget", path=str(environ.get("PATH", ""))
    )
    if not winget:
        return False
    spec = _PROVIDERS[provider]
    try:
        completed = runner(
            [
                winget,
                "install",
                "--id",
                spec.package_id,
                "--exact",
                "--source",
                "winget",
                "--accept-package-agreements",
                "--accept-source-agreements",
            ],
            timeout=600,
            check=False,
            env=trusted_cli_environment(environ, include_ssh_agent=False),
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0


def _login_provider(
    probe: CLIProbe,
    *,
    root: Path | None = None,
    environ: Mapping[str, str],
    runner: Callable[..., subprocess.CompletedProcess],
) -> bool:
    if probe.executable is None:
        return False
    spec = _PROVIDERS[probe.provider]
    try:
        arguments = [str(probe.executable)]
        if probe.provider == "codex":
            arguments.extend(
                item
                for override in CODEX_CLI_AUTH_OVERRIDES
                for item in ("--config", override)
            )
        arguments.extend(spec.auth_login)
        completed = runner(
            arguments,
            timeout=600,
            check=False,
            env=_provider_environment(probe.provider, root, environ),
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0


def _selected_providers(choice: str) -> tuple[str, ...]:
    if choice == "both":
        return ("codex", "claude")
    return (choice,)


def _prepare_provider(
    provider: str,
    *,
    root: Path | None = None,
    input_fn: Callable[[str], str],
    output: TextIO,
    environ: Mapping[str, str],
    runner: Callable[..., subprocess.CompletedProcess],
) -> None:
    spec = _PROVIDERS[provider]
    probe = detect_provider(provider, root=root, environ=environ, runner=runner)
    if not probe.runnable:
        output.write(f"\n{spec.label} is not installed and runnable.\n")
        if os.name == "nt" and _answer_yes(
            f"Install {spec.label} with Windows Package Manager now? [y/N] ",
            input_fn=input_fn,
        ):
            if not _install_provider(provider, environ=environ, runner=runner):
                raise ProviderSetupRequired(
                    f"{spec.label} installation did not complete. See {spec.documentation} and rerun setup."
                )
            probe = detect_provider(provider, root=root, environ=environ, runner=runner)
        if not probe.runnable:
            raise ProviderSetupRequired(
                f"Install {spec.label} from {spec.documentation}, then rerun provider setup."
            )
    if probe.authenticated:
        output.write(f"{spec.label}: installed and signed in.\n")
        return
    output.write(f"{spec.label} is installed but is not signed in.\n")
    if not _answer_yes(f"Start the official {spec.label} sign-in now? [y/N] ", input_fn=input_fn):
        raise ProviderSetupRequired(SETUP_NEEDED_MESSAGE)
    if not _login_provider(probe, root=root, environ=environ, runner=runner):
        raise ProviderSetupRequired(f"{spec.label} sign-in did not complete. Rerun provider setup.")
    verified = detect_provider(provider, root=root, environ=environ, runner=runner)
    if not verified.authenticated:
        raise ProviderSetupRequired(f"{spec.label} still reports that it is not signed in.")
    output.write(f"{spec.label}: sign-in verified.\n")


def _prompt_choice(input_fn: Callable[[str], str], output: TextIO) -> str:
    output.write(
        "\nChoose how Jarvis should access subscription models:\n"
        "  1. Codex CLI (your ChatGPT subscription)\n"
        "  2. Claude CLI (your Claude subscription)\n"
        "  3. Both (Claude for fast/reasoning; Codex for coding/deep)\n"
    )
    mapping = {"1": "codex", "codex": "codex", "2": "claude", "claude": "claude", "3": "both", "both": "both"}
    for _attempt in range(3):
        try:
            answer = input_fn("Provider [1/2/3]: ").strip().casefold()
        except (EOFError, KeyboardInterrupt) as exc:
            raise ProviderSetupRequired(SETUP_NEEDED_MESSAGE) from exc
        if answer in mapping:
            return mapping[answer]
        output.write("Enter 1, 2, or 3.\n")
    raise ProviderSetupRequired(SETUP_NEEDED_MESSAGE)


def configure_provider(
    choice: str,
    root: Path | None = None,
    *,
    require_ready: bool = True,
    environ: Mapping[str, str] | None = None,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> ProviderSetupResult:
    normalized = str(choice).strip().casefold()
    if normalized not in _CHOICES:
        raise ValueError("provider choice must be codex, claude, or both")
    values = os.environ if environ is None else environ
    if require_ready:
        unavailable = [
            provider
            for provider in _selected_providers(normalized)
            if not detect_provider(
                provider, root=root, environ=values, runner=runner
            ).authenticated
        ]
        if unavailable:
            raise ProviderSetupRequired(
                "Selected provider CLI is not installed and signed in: " + ", ".join(unavailable)
            )
    env_path = persist_provider_choice(normalized, root)
    return ProviderSetupResult("configured", normalized, env_path)


def ensure_ready(
    interactive: bool,
    root: Path | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    input_fn: Callable[[str], str] = input,
    output: TextIO | None = None,
    stdin_isatty: bool | None = None,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> ProviderSetupResult:
    """Ensure first-run provider setup without ever prompting a headless caller.

    Already-configured and migrated installations return ``state='existing'``.
    A headless first run raises :class:`ProviderSetupRequired` before ``input_fn``
    is called. Interactive setup persists a verified CLI choice atomically.
    """
    if is_setup_complete(root, environ=environ):
        return ProviderSetupResult("existing")
    if not interactive:
        raise ProviderSetupRequired(SETUP_NEEDED_MESSAGE)
    terminal = sys.stdin.isatty() if stdin_isatty is None else bool(stdin_isatty)
    if not terminal:
        raise ProviderSetupRequired(SETUP_NEEDED_MESSAGE)
    destination = sys.stdout if output is None else output
    values = os.environ if environ is None else environ
    choice = _prompt_choice(input_fn, destination)
    for provider in _selected_providers(choice):
        _prepare_provider(
            provider,
            root=root,
            input_fn=input_fn,
            output=destination,
            environ=values,
            runner=runner,
        )
    result = configure_provider(
        choice,
        root,
        require_ready=False,
        environ=values,
        runner=runner,
    )
    destination.write("Provider setup complete. Jarvis will not ask again on normal launches.\n")
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Configure Jarvis subscription-backed model CLIs")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--interactive", action="store_true", help="run the first-use wizard when needed")
    mode.add_argument("--ensure", action="store_true", help="check readiness without prompting")
    mode.add_argument("--configure", choices=sorted(_CHOICES), help="persist an already-ready provider choice")
    mode.add_argument(
        "--login",
        choices=sorted(_CHOICES),
        help="install/sign in and select a provider even on an existing installation",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.login:
            if not sys.stdin.isatty():
                raise ProviderSetupRequired(SETUP_NEEDED_MESSAGE)
            for provider in _selected_providers(args.login):
                _prepare_provider(
                    provider,
                    root=None,
                    input_fn=input,
                    output=sys.stdout,
                    environ=os.environ,
                    runner=subprocess.run,
                )
            result = configure_provider(args.login)
            print(f"Jarvis provider login and selection saved: {result.choice}.")
        elif args.configure:
            result = configure_provider(args.configure)
            print(f"Jarvis provider selection saved: {result.choice}.")
        else:
            ensure_ready(interactive=bool(args.interactive))
        return 0
    except ProviderSetupRequired as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except (ProviderSetupError, OSError) as exc:
        print(f"Jarvis provider setup failed safely: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
