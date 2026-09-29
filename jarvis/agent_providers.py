"""Provider readiness and per-agent model clients for Command Center agents.

Agents run through JARVIS's own tool loop: the CLI provider is used strictly as a
tool-less model backend (see ``ClaudeCLIClient``/``CodexCLIClient``), and every tool call
executes inside JARVIS. This module adds three things the agent runtime needs:

* readiness: installed version, sign-in state and verified model identifiers, using the
  documented status commands (``provider_setup.detect_provider``), never credential files;
* executable choice: among the candidates JARVIS already trusts (native, signed by the
  publisher, launchable), prefer the newest, so a stale package-manager copy cannot block a
  model that a newer trusted copy supports;
* one client per run, bound to exactly one provider, so no failover target on another
  provider exists and private context cannot silently move elsewhere.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from . import model_client as mc
from .provider_setup import detect_provider

PROVIDERS = ("claude-cli", "codex-cli", "openrouter")
PROVIDER_LABELS = {"claude-cli": "Claude CLI subscription", "codex-cli": "Codex CLI subscription",
                   "openrouter": "OpenRouter (API key)"}
# The operator's requested default for new agents. It is only *applied* once verified.
REQUESTED_DEFAULT = ("claude-cli", "claude-opus-5-5")
KNOWN_MODELS = {
    "claude-cli": ("claude-opus-5-5", "claude-sonnet-5", "opus", "sonnet"),
    "codex-cli": ("gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.5"),
    "openrouter": ("stealth/space-bunny-alpha",),
}
_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]{0,79}$")
_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")


def valid_model_name(value: str, provider: str | None = None) -> bool:
    from .openrouter import valid_model_id

    if provider == "openrouter":
        return valid_model_id(value)
    return bool(_MODEL_RE.match(value or "")) or (provider is None and valid_model_id(value))


def _version_tuple(text: str) -> tuple[int, int, int]:
    match = _VERSION_RE.search(text or "")
    return tuple(int(x) for x in match.groups()) if match else (0, 0, 0)  # type: ignore[return-value]


def _run(args: list[str], timeout: float = 30.0, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        args, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL,
        env=env, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=False,
    )


# Validating a CLI executable costs seconds on Windows: an Authenticode publisher check
# through PowerShell (~1.7 s) plus a ``--version`` launch per candidate. The Hub used to pay
# that on every message. Results are cached per executable *file identity* (resolved path,
# size, modification and change times, file index), so replacing or updating the binary
# forces a fresh check, and every entry expires after ``_VALIDATION_TTL`` seconds regardless.
# Failures are cached only briefly, so a transient PowerShell hiccup cannot pin a provider
# down. The cheap structural checks (native PE/ELF, no links or reparse points) still run
# on every call.
_VALIDATION_TTL = 600.0
_FAILED_VALIDATION_TTL = 30.0
_validation_lock = threading.Lock()
_validation_cache: dict[tuple[str, str], tuple[tuple[int, ...], float, dict[str, Any] | None]] = {}


def _file_identity(path: Path) -> tuple[int, ...] | None:
    try:
        details = os.stat(path)
    except OSError:
        return None
    return (details.st_size, details.st_mtime_ns, getattr(details, "st_ctime_ns", 0),
            getattr(details, "st_ino", 0))


def _validated_cli(candidate: Path | str, publisher: str,
                   launchable: Any) -> dict[str, Any] | None:
    """Return ``{path, version, key}`` for a trusted native CLI, or ``None``; cached."""
    resolved = mc._validated_native_executable(str(candidate))
    if resolved is None:
        return None
    identity = _file_identity(resolved)
    if identity is None:
        return None
    cache_key = (str(resolved), publisher)
    now = time.monotonic()
    with _validation_lock:
        hit = _validation_cache.get(cache_key)
    if hit is not None and hit[0] == identity:
        ttl = _VALIDATION_TTL if hit[2] is not None else _FAILED_VALIDATION_TTL
        if now - hit[1] < ttl:
            return dict(hit[2]) if hit[2] is not None else None
    info: dict[str, Any] | None = None
    if mc._windows_cli_publisher_matches(resolved, publisher) and launchable(resolved):
        try:
            version = _run([str(resolved), "--version"], timeout=20).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            version = None
        if version is not None:
            info = {"path": resolved, "version": version, "key": _version_tuple(version)}
    # Only cache when the file did not change while it was being checked.
    if _file_identity(resolved) == identity:
        with _validation_lock:
            _validation_cache[cache_key] = (identity, now, info)
    return dict(info) if info is not None else None


def clear_validation_cache() -> None:
    with _validation_lock:
        _validation_cache.clear()


def claude_candidates() -> list[dict[str, Any]]:
    """Every Claude executable JARVIS already trusts, with its version; newest first."""
    raw: list[Path | None] = [mc._resolved_winget_link("claude.exe")]
    if os.name == "nt" and os.environ.get("APPDATA"):
        raw.append(Path(os.environ["APPDATA"]) / "npm" / "node_modules" / "@anthropic-ai"
                   / "claude-code" / "bin" / "claude.exe")
    raw.append(mc.trusted_path_executable("claude.exe" if os.name == "nt" else "claude"))
    found: dict[str, dict[str, Any]] = {}
    for candidate in raw:
        if candidate is None:
            continue
        info = _validated_cli(candidate, "Anthropic, PBC", mc._claude_cli_launchable)
        if info is None or str(info["path"]) in found:
            continue
        found[str(info["path"])] = info
    return sorted(found.values(), key=lambda item: item["key"], reverse=True)


def newest_claude_executable() -> Path | None:
    candidates = claude_candidates()
    return candidates[0]["path"] if candidates else None


def codex_executable() -> Path | None:
    """``resolve_codex_cli_executable``'s candidates and checks, with the checks cached."""
    for candidate in mc.codex_cli_candidates():
        info = _validated_cli(candidate, "OpenAI OpCo, LLC", mc._codex_cli_launchable)
        if info is not None:
            return info["path"]
    return None


@dataclass
class ProviderState:
    provider: str
    installed: bool = False
    authenticated: bool = False
    version: str = ""
    checked_at: float = 0.0
    detail: str = ""
    verified_models: dict[str, dict[str, Any]] = field(default_factory=dict)

    def public(self) -> dict[str, Any]:
        if self.provider == "openrouter":
            state, reason = (("READY", "OpenRouter key accepted.") if self.authenticated else
                             ("LOGIN_REQUIRED", "Add your OpenRouter API key in the Hub's settings."))
        elif not self.installed:
            state, reason = "UNAVAILABLE", "CLI is not installed on the backend host."
        elif not self.authenticated:
            state, reason = "LOGIN_REQUIRED", (
                "Signed out. On the backend host run `claude auth login`."
                if self.provider == "claude-cli" else
                "JARVIS's isolated Codex profile is signed out. On the backend host run "
                "`python -X utf8 -m jarvis.provider_setup --login codex`.")
        else:
            state, reason = "READY", "Signed in with the existing subscription."
        return {
            "provider": self.provider, "label": PROVIDER_LABELS[self.provider], "state": state,
            "reason": self.detail or reason, "installed": self.installed,
            "authenticated": self.authenticated, "version": self.version,
            "checked_at": self.checked_at, "known_models": list(KNOWN_MODELS[self.provider]),
            "verified_models": self.verified_models,
        }


class ProviderRegistry:
    """Readiness for the two subscription CLIs, refreshed on demand and on a timer."""

    def __init__(self, profile_dir: Path, *, refresh_seconds: float = 120.0) -> None:
        self.profile_dir = Path(profile_dir).resolve()
        self.refresh_seconds = refresh_seconds
        self._lock = threading.Lock()
        self._states = {name: ProviderState(name) for name in PROVIDERS}

    def _environment(self) -> dict[str, str]:
        keep = ("PATH", "SystemRoot", "LOCALAPPDATA", "USERPROFILE", "APPDATA", "HOMEDRIVE",
                "HOMEPATH", "TEMP", "TMP", "ProgramFiles", "ProgramData", "WINDIR")
        env = {k: os.environ[k] for k in keep if k in os.environ}
        env["JARVIS_DATA"] = str(self.profile_dir)
        return env

    def refresh(self, force: bool = False) -> dict[str, dict[str, Any]]:
        now = time.time()
        for name in PROVIDERS:
            with self._lock:
                state = self._states[name]
                if not force and now - state.checked_at < self.refresh_seconds:
                    continue
            if name == "openrouter":
                from .openrouter import KeyStore, check_key

                key = KeyStore(self.profile_dir).get()
                outcome = check_key(key) if key else {"ok": False, "error": ""}
                with self._lock:
                    state.installed, state.authenticated = True, bool(outcome.get("ok"))
                    state.version, state.checked_at = "", now
                    state.detail = str(outcome.get("error") or "")
                continue
            probe_name = "claude" if name == "claude-cli" else "codex"
            installed = authenticated = False
            version, detail = "", ""
            try:
                probe = detect_provider(probe_name, environ=self._environment())
                installed, authenticated = bool(probe.installed), bool(probe.authenticated)
            except Exception as exc:  # readiness must never crash the backend
                detail = f"Status check failed: {type(exc).__name__}."
            if name == "claude-cli":
                newest = claude_candidates()
                version = newest[0]["version"] if newest else ""
            else:
                exe = mc.resolve_codex_cli_executable()
                if exe is not None:
                    try:
                        version = _run([str(exe), "--version"], timeout=20).stdout.strip()
                    except (OSError, subprocess.SubprocessError):
                        version = ""
            with self._lock:
                state.installed, state.authenticated = installed, authenticated
                state.version, state.detail, state.checked_at = version, detail, now
        return self.snapshot()

    def snapshot(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return {name: state.public() for name, state in self._states.items()}

    def ready(self, provider: str) -> tuple[bool, str]:
        self.refresh()
        public = self.snapshot().get(provider)
        if public is None:
            return False, "Unknown provider."
        return public["state"] == "READY", public["reason"]

    def record_verification(self, provider: str, requested: str, outcome: dict[str, Any]) -> None:
        with self._lock:
            self._states[provider].verified_models[requested] = outcome

    def verify_model(self, provider: str, model: str) -> dict[str, Any]:
        """One tiny tool-less call; report the model identifier the provider says it ran."""
        if provider not in PROVIDERS or not valid_model_name(model, provider):
            raise ValueError("Unsupported provider or model identifier.")
        started = time.time()
        if provider == "claude-cli":
            outcome = self._verify_claude(model)
        else:
            outcome = self._verify_codex(model, provider)
        outcome.update(requested=model, checked_at=started, seconds=round(time.time() - started, 1))
        self.record_verification(provider, model, outcome)
        return outcome

    def _verify_claude(self, model: str) -> dict[str, Any]:
        exe = newest_claude_executable()
        if exe is None:
            return {"ok": False, "error": "Claude CLI is not installed."}
        cwd = tempfile.mkdtemp(prefix="jarvis-model-check-")
        try:
            done = _run([str(exe), "--print", "--output-format", "json", "--no-session-persistence",
                         "--safe-mode", "--disable-slash-commands", "--strict-mcp-config",
                         "--tools", "", "--model", model, "Reply with exactly the word OK."],
                        timeout=180, env=mc.trusted_cli_environment(include_ssh_agent=False))
        except (OSError, subprocess.SubprocessError) as exc:
            return {"ok": False, "error": f"Could not run the CLI: {type(exc).__name__}."}
        finally:
            shutil.rmtree(cwd, ignore_errors=True)
        try:
            payload = json.loads(done.stdout)
        except json.JSONDecodeError:
            return {"ok": False, "error": _bounded(done.stderr or done.stdout or "No output from the CLI.")}
        if payload.get("is_error"):
            return {"ok": False, "error": _bounded(str(payload.get("result") or "Provider error."))}
        used = [name for name in (payload.get("modelUsage") or {}) if not name.startswith("claude-haiku")]
        return {"ok": True, "models_reported": list(payload.get("modelUsage") or {}),
                "resolved": used[0] if len(used) == 1 else None, "executable_version":
                _run([str(exe), "--version"], timeout=20).stdout.strip()}

    def _verify_codex(self, model: str, provider: str = "codex-cli") -> dict[str, Any]:
        client = None
        try:
            client = build_agent_client(self.profile_dir, provider, model)
            response = client.chat([{"role": "user", "content": "Reply with exactly the word OK."}],
                                   [], f"{provider}:{model}")
        except mc.ModelProviderError as exc:
            return {"ok": False, "error": _bounded(str(exc))}
        except Exception as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {_bounded(str(exc))}"}
        finally:
            if client is not None:
                client.close()
        # The Codex CLI binds the requested model exactly; JARVIS rejects unsupported ones.
        return {"ok": True, "resolved": model, "models_reported": [model],
                "reply": _bounded(str((response.get("message") if isinstance(response.get("message"), dict)
                                       else response).get("content", "")), 40)}


def _bounded(text: str, limit: int = 300) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def build_agent_client(profile_dir: Path, provider: str, model: str,
                       effort: str | None = None) -> mc.ModelClient:
    """A model client bound to exactly one provider, over the shared signed-in profile.

    The profile directory only supplies the provider login (Codex's isolated profile);
    every agent's memory, workspace and runtime files live in its own directories.
    Executable validation is cached (see ``_validated_cli``); each call still builds a
    fresh client, so no conversation, memory or permission state is shared between runs.
    ``effort`` pins one effort for every call (``None`` keeps the per-route mapping).
    """
    if provider not in PROVIDERS:
        raise ValueError("Unsupported provider.")
    ref = f"{provider}:{model}"
    profile = _ProfileConfig(
        data_dir=Path(profile_dir).resolve(), model=ref, fast_model=ref, reasoning_model=ref,
        coding_model=ref, deep_model=ref, claude_cli_enabled=False, codex_cli_enabled=False,
    )
    # No provider is enabled here, so this starts no subprocess; the one provider is
    # attached below from the cached, validated executable.
    client = mc.build_model_client(profile)
    if provider == "openrouter":
        from .openrouter import KeyStore, OpenRouterClient

        key = KeyStore(profile_dir).get()
        if key is None:
            raise mc.ModelProviderError("OpenRouter", "is not configured; add an OpenRouter API key",
                                        status_code=401, provider_unavailable=True)
        client.openrouter = OpenRouterClient(
            key, generation_timeout=profile.cloud_generation_timeout,
            max_output_tokens=32768, max_response_bytes=profile.cloud_max_response_bytes,
            max_retries=profile.cloud_max_retries, retry_backoff=profile.cloud_retry_backoff)
        client.openrouter.set_fixed_effort(effort)
        return client
    if provider == "claude-cli":
        executable = newest_claude_executable()
        if executable is not None:
            owner = tempfile.TemporaryDirectory(prefix="jarvis-claude-cli-")
            client.claude_cli = mc.ClaudeCLIClient(
                executable, working_directory=owner.name, working_directory_owner=owner,
                generation_timeout=profile.cloud_generation_timeout,
                max_response_bytes=profile.cloud_max_response_bytes,
                max_retries=profile.cloud_max_retries, retry_backoff=profile.cloud_retry_backoff,
                # JARVIS routes that ask for no extended thinking (``think=False``) get the
                # lowest Claude effort, as the Codex adapter maps them to ``none``. Without
                # this the CLI's own default applied, and a 500-word answer cost ~3,000
                # hidden reasoning tokens (~30 s). Routes that request thinking are unchanged.
                unthinking_effort="low",
            )
            client.claude_cli.set_fixed_effort(effort)
    else:
        executable = codex_executable()
        if executable is not None:
            owner = tempfile.TemporaryDirectory(prefix="jarvis-codex-cli-")
            codex = mc.CodexCLIClient(
                executable, working_directory=owner.name, working_directory_owner=owner,
                codex_home=mc.isolated_codex_cli_home(profile.data_dir),
                generation_timeout=profile.cloud_generation_timeout,
                max_response_bytes=profile.cloud_max_response_bytes,
                max_retries=profile.cloud_max_retries, retry_backoff=profile.cloud_retry_backoff,
            )
            # The per-client isolation and sign-in probes are safety checks; they still run
            # for every client, exactly as ``build_model_client`` runs them.
            codex.verify_context_isolation()
            codex.probe_authentication()
            codex.set_fixed_effort(effort)
            client.codex_cli = codex
    return client


@dataclass(frozen=True)
class _ProfileConfig:
    """The fields ``build_model_client`` reads; every other provider is off."""

    data_dir: Path
    model: str
    fast_model: str
    reasoning_model: str
    coding_model: str
    deep_model: str
    claude_cli_enabled: bool
    codex_cli_enabled: bool
    learning_model: str | None = None
    cloud_enabled: bool = True
    openai_api_enabled: bool = False
    anthropic_api_enabled: bool = False
    cloud_generation_timeout: float = 600.0
    cloud_max_output_tokens: int = 8192
    cloud_max_response_bytes: int = 8 * 1024 * 1024
    # Bounded: one retry per model call, then the run reports a recoverable failure.
    cloud_max_retries: int = 1
    cloud_retry_backoff: float = 1.0
    ollama_url: str = "http://127.0.0.1:11434"
    ollama_allow_remote: bool = False
    ollama_health_timeout: float = 1.0
    ollama_generation_timeout: float = 30.0
    ollama_max_output_tokens: int = 256
    ollama_max_response_bytes: int = 1024 * 1024
    ollama_max_retries: int = 0
    ollama_retry_backoff: float = 0.1
    ollama_keep_alive: str = "0"
    ollama_num_thread: int | None = None
    ollama_enabled: bool = False

    def with_model(self, provider: str, model: str) -> "_ProfileConfig":
        ref = f"{provider}:{model}"
        return replace(self, model=ref, fast_model=ref, reasoning_model=ref, coding_model=ref, deep_model=ref)
