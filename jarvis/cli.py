from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from dataclasses import is_dataclass, replace
from contextlib import redirect_stderr, redirect_stdout
import threading
import time
from pathlib import Path
from typing import Any, Callable, TextIO
from uuid import uuid4

from .agent import Agent, AgentResult
from .attachments import ImageAttachment
from .config import Config, create_project_workspace, resolve_project_workspace
from .constitutional import (
    constitutional_status,
    export_datasets as export_constitutional_datasets,
    generate_records as generate_constitutional_records,
    initialize_pack as initialize_constitutional_pack,
    verify_records as verify_constitutional_records,
)
from .distillation import (
    distillation_status,
    export_reward_dataset,
    export_sft_dataset,
    generate_candidates,
    initialize_pack,
    verify_candidates,
)
from .memory import DEFAULT_LEASE_SECONDS, Memory
from .memory_embeddings import EmbeddingError, run_memory_index_batch
from .model_client import ModelClient, build_model_client, split_model_reference
from .offline_documents import SUPPORTED_DOCUMENT_TYPES, build_offline_document
from .ollama_client import OllamaError
from .provider_setup import ProviderSetupRequired, ensure_ready as ensure_provider_ready
from .proactive import (
    RuntimeGuard,
    build_self_model,
    calibrated_meta_gate,
    initiative_cycle,
    initiative_eligibility,
    record_result_reflection,
)
from .redaction import (
    contains_secret, is_redacted_descriptor, is_sensitive_key, redact_secrets,
)
from .self_diagnosis import (
    run_capability_canaries,
    run_isolated_selftest,
    run_recovery_test,
)
from .skill_library import (
    forget_learned_skill,
    list_available_skills,
    read_available_skill,
)
from .vault import Vault
from .specialists import specialist_for_prompt
from .training import dataset_status, export_verified_dataset, parse_expected_terms
from .tools import ToolBox


MIN_POLL_SECONDS = 1
MAX_POLL_SECONDS = 3600
WORKER_LEASE_SECONDS = min(DEFAULT_LEASE_SECONDS, 300)
WORKER_HEARTBEAT_SECONDS = max(1.0, WORKER_LEASE_SECONDS / 3)
BASE_RETRY_SECONDS = 5
MAX_RETRY_SECONDS = 300
WORKER_LOG_MAX_BYTES = 5 * 1024 * 1024
WORKER_LOG_BACKUPS = 3
MAX_SERVICE_BACKOFF_SECONDS = 30
MIN_FREE_DISK_BYTES = 512 * 1024 * 1024
MAX_WAL_BYTES = 64 * 1024 * 1024
MAX_OPEN_PREDICTIONS = 100
WORKER_STATUS_HEARTBEAT_SECONDS = 30.0
FOREGROUND_LEASE_PREFIX = ".foreground-request-"
FOREGROUND_LEASE_SUFFIX = ".lease"
FOREGROUND_LEASE_HEARTBEAT_SECONDS = 2.0
FOREGROUND_LEASE_TTL_SECONDS = 12.0
FOREGROUND_LEASE_FUTURE_SKEW_SECONDS = 5.0
FOREGROUND_YIELD_SECONDS = 1.0
PREDICTION_FAMILY_CHOICES = tuple(sorted(Memory.PREDICTION_FAMILIES))

_MODEL_RUNTIME_COMMANDS = frozenset(
    {None, "ask", "gateway", "presence", "ui", "worker"}
)
_MODEL_TRAINING_COMMANDS = frozenset({
    "benchmark",
    "cai-generate",
    "distill-generate",
})


def _ensure_first_run_provider_setup(args: argparse.Namespace) -> None:
    """Run the provider chooser only for commands that can invoke a model.

    Background services must never wait on ``input()``. Foreground commands may
    show the one-time chooser when attached to a real terminal; Windows launchers
    perform the same check before starting their GUI or service entry points.
    """
    command = getattr(args, "command", None)
    if command not in _MODEL_RUNTIME_COMMANDS and not (
        command == "training"
        and getattr(args, "training_command", None) in _MODEL_TRAINING_COMMANDS
    ):
        return
    terminal = bool(getattr(sys.stdin, "isatty", lambda: False)())
    interactive = command != "worker" and terminal
    ensure_provider_ready(interactive=interactive, stdin_isatty=terminal)

def _runtime_state(memory: Any) -> str:
    if not hasattr(memory, "control_state"):
        return "running"
    try:
        return str(memory.control_state().get("state", "stopped"))
    except Exception:
        return "stopped"


def _record_reflection_safely(
    memory: Any,
    result: Any,
    *,
    task: dict[str, Any] | None = None,
    conversation_id: int | None = None,
) -> None:
    if not hasattr(memory, "record_reflection"):
        return
    try:
        record_result_reflection(
            memory, result, task=task, conversation_id=conversation_id
        )
    except Exception as exc:
        print(
            f"Reflection recording failed ({type(exc).__name__}); the task result is unchanged.",
            file=sys.stderr,
        )


def _configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass


def _safe_summary(value: Any, limit: int = 180) -> str:
    text = redact_secrets(str(value), "[redacted]").replace("\r", " ").replace("\n", " ")
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def _safe_detail(value: Any, limit: int) -> str:
    """Redact and bound full detail output without flattening its line structure."""
    text = redact_secrets(str(value), "[redacted]")
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)] + "..."


def _safe_resource(value: Any, limit: int = 2_000) -> str:
    """Render approval JSON safely without changing meaningful string whitespace."""
    def sanitized(item: Any, *, key: str = "") -> Any:
        normalized_key = key.casefold().replace("_", "").replace("-", "")
        sensitive = is_sensitive_key(key) or normalized_key in {
            "content", "newtext", "oldtext",
        }
        if sensitive and not is_redacted_descriptor(item):
            return "[redacted]"
        if isinstance(item, dict):
            return {str(child_key): sanitized(child, key=str(child_key)) for child_key, child in item.items()}
        if isinstance(item, list):
            return [sanitized(child) for child in item]
        if isinstance(item, str):
            return redact_secrets(item, "[redacted]")
        if isinstance(item, (bool, int, float)) or item is None:
            return item
        return f"[{type(item).__name__}]"

    raw = str(value)
    try:
        text = json.dumps(
            sanitized(json.loads(raw)),
            ensure_ascii=False,
            separators=(",", ":"),
        )
    except (TypeError, ValueError, json.JSONDecodeError):
        text = redact_secrets(raw, "[redacted]")
        text = text.replace("\r", "\\r").replace("\n", "\\n").replace("\t", "\\t")
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)] + "..."


def _supports_color(stream: TextIO) -> bool:
    return bool(
        os.getenv("NO_COLOR") is None
        and hasattr(stream, "isatty")
        and stream.isatty()
    )


def _styled(text: str, code: str, stream: TextIO | None = None) -> str:
    target = sys.stdout if stream is None else stream
    return f"\033[{code}m{text}\033[0m" if _supports_color(target) else text


_CLI_EVENT_LABELS: tuple[tuple[str, str], ...] = (
    ("processing -", "processing"),
    ("reasoning -", "reasoning"),
    ("tool -", "tool activity"),
    ("model -", "model activity"),
    ("failover -", "provider failover"),
    ("recovery -", "recovering request"),
    ("memory -", "memory recall"),
    ("specialist ", "specialist coordination"),
    ("image ", "image work"),
    ("learning ", "learning"),
    ("skill ", "skill management"),
    ("network ", "network analysis"),
    ("storage ", "storage analysis"),
    ("connector ", "connector activity"),
    ("implementation ", "implementation review"),
    ("repair ", "repairing"),
    ("adversarial ", "adversarial verification"),
    ("artifact ", "artifact launch"),
    ("gateway ", "gateway activity"),
    ("vault ", "vault activity"),
    ("research", "researching"),
    ("current ", "current-information lookup"),
    ("product ", "product lookup"),
    ("planning ", "planning"),
    ("review", "reviewing"),
    ("verif", "verifying"),
    ("acceptance correction", "correcting result"),
    ("task contract", "resolving task"),
    ("clarification requested", "clarification needed"),
    ("instant response", "instant response"),
    ("screen companion", "screen companion"),
    ("continuing ", "continuing request"),
    ("building ", "building capability"),
    ("synthesizing", "synthesizing"),
)


def _cli_event_label(message: str) -> str:
    normalized = str(message).strip().casefold()
    for prefix, label in _CLI_EVENT_LABELS:
        if normalized.startswith(prefix):
            return label
    return "working"


def event(message: str) -> None:
    # Terminal progress is deliberately selected from fixed labels. Presence has
    # its own sanitized structured event channel; no caller-provided text reaches
    # this CLI output sink.
    label = _cli_event_label(message)
    print(_styled(f"[{label}]", "2"), flush=True)


def _new_client(config: Config) -> ModelClient:
    return build_model_client(config)


def build() -> tuple[Config, Memory, Agent]:
    config = Config.load()
    memory = Memory(config.data_dir / "jarvis.db")
    try:
        agent = Agent(config, memory, event)
    except BaseException:
        memory.close()
        raise
    return config, memory, agent


def _directory_writable(path: Path) -> bool:
    if not path.is_dir():
        return False
    try:
        descriptor, name = tempfile.mkstemp(prefix=".jarvis-check-", dir=path)
    except OSError:
        return False

    cleanup_ok = True
    try:
        os.close(descriptor)
    except OSError:
        cleanup_ok = False
    try:
        if name:
            Path(name).unlink(missing_ok=True)
    except OSError:
        cleanup_ok = False
    return cleanup_ok


def _local_health_errors(config: Config) -> list[str]:
    errors: list[str] = []
    try:
        if not config.soul_path.is_file():
            errors.append("SOUL.md is missing")
        elif config.soul_path.stat().st_size > 1_000_000:
            errors.append("SOUL.md is unexpectedly large")
        else:
            with config.soul_path.open("r", encoding="utf-8") as soul:
                if not soul.read(8192).strip():
                    errors.append("SOUL.md is empty")
    except (OSError, UnicodeError):
        errors.append("SOUL.md could not be read as UTF-8")

    if not _directory_writable(config.workspace):
        errors.append("Workspace is not writable")
    if not _directory_writable(config.data_dir):
        errors.append("Data directory is not writable")

    try:
        free_bytes = shutil.disk_usage(config.data_dir).free
        if free_bytes < MIN_FREE_DISK_BYTES:
            errors.append(
                f"Data disk has less than {MIN_FREE_DISK_BYTES // (1024 * 1024)} MB free"
            )
    except OSError:
        errors.append("Data disk free space could not be checked")

    try:
        wal_path = config.data_dir / "jarvis.db-wal"
        if wal_path.is_file() and wal_path.stat().st_size > MAX_WAL_BYTES:
            errors.append("Memory database WAL exceeds 64 MB")
    except OSError:
        errors.append("Memory database WAL size could not be checked")

    try:
        with Memory(config.data_dir / "jarvis.db") as memory:
            quick_check = memory.db.execute("PRAGMA quick_check(1)").fetchone()
            if not quick_check or str(quick_check[0]).casefold() != "ok":
                errors.append("Memory database integrity check failed")
            if memory.db.execute("PRAGMA foreign_key_check").fetchone() is not None:
                errors.append("Memory database has broken references")
            if hasattr(memory, "health_indicators"):
                indicators = memory.health_indicators(
                    approval_ttl_hours=int(getattr(config, "approval_ttl_hours", 24))
                )
                if indicators["open_predictions"] > MAX_OPEN_PREDICTIONS:
                    errors.append(
                        f"Open task predictions exceed {MAX_OPEN_PREDICTIONS}"
                    )
                if indicators["stale_awaiting_approval_tasks"]:
                    errors.append(
                        "Stale awaiting-approval tasks exceed the approval TTL"
                    )
    except (OSError, sqlite3.Error, RuntimeError):
        errors.append("Memory database could not be opened safely")
    return errors


def _installed_model(wanted: str, installed: list[str]) -> bool:
    if wanted in installed:
        return True
    try:
        provider, provider_model = split_model_reference(wanted)
    except ValueError:
        return False
    if provider in {"openai", "anthropic", "codex-cli", "claude-cli"}:
        return bool(provider_model) and any(
            item.startswith(f"{provider}:") for item in installed
        )
    if wanted.startswith("ollama:"):
        return provider_model in installed
    if ":" not in wanted:
        return any(name.split(":", 1)[0] == wanted for name in installed)
    return False


def doctor(*, deep: bool = False) -> int:
    print("JARVIS system check")
    try:
        config = Config.load()
    except (OSError, ValueError) as exc:
        print(f"  Configuration: needs attention ({_safe_summary(exc)})")
        print("  Status: not ready")
        return 1

    errors = _local_health_errors(config)
    print(f"  Workspace: {config.workspace}")
    execution_mode = getattr(config, "execution_mode", "disabled")
    print(f"  Host execution: {execution_mode}")
    computer_access = getattr(config, "computer_access", "disabled")
    print(f"  Desktop access: {computer_access}")
    if computer_access == "trusted-desktop":
        print(f"  Desktop boundary: {getattr(config, 'computer_root', config.workspace)}")
    if execution_mode == "trusted-host":
        print(
            "  Safety note: trusted-host runs repository code with your Windows account permissions"
        )
    print(f"  Local files: {'ready' if not errors else 'needs attention'}")

    models: list[str] = []
    client = _new_client(config)
    try:
        models = client.models(refresh=True)
    except OllamaError:
        models = []

    provider_status = getattr(client, "provider_status", None)
    if isinstance(provider_status, dict):
        ollama_enabled = provider_status.get("ollama_enabled") is not False
        ollama_online = provider_status.get("ollama_online") is True
        local_count = int(provider_status.get("ollama_model_count") or 0)
        print(
            "  Ollama: disabled"
            if not ollama_enabled
            else (
                f"  Ollama: online ({local_count} model(s))"
                if ollama_online else "  Ollama: offline"
            )
        )
        print(
            "  OpenAI: API key configured"
            if provider_status.get("openai_configured")
            else "  OpenAI: not configured"
        )
        images_enabled = bool(
            getattr(config, "cloud_enabled", True)
            and getattr(config, "openai_images_enabled", False)
        )
        images_key_configured = bool(os.getenv("OPENAI_API_KEY", "").strip())
        if not images_enabled:
            print("  OpenAI Images: disabled")
        elif images_key_configured:
            print("  OpenAI Images: ready (gpt-image-2)")
        else:
            print("  OpenAI Images: needs OPENAI_API_KEY")
        print(
            "  Anthropic: API key configured"
            if provider_status.get("anthropic_configured")
            else "  Anthropic: not configured"
        )
        codex_configured = bool(provider_status.get("codex_cli_configured"))
        codex_auth_method = provider_status.get("codex_cli_auth_method")
        if not codex_configured:
            print("  Codex CLI: disabled or unavailable")
        elif codex_auth_method == "chatgpt":
            print("  Codex CLI: ChatGPT subscription verified")
        else:
            print("  Codex CLI: needs a verified ChatGPT sign-in")
            errors.append("Codex CLI is not authenticated with ChatGPT")
        print(
            "  Claude CLI: configured"
            if provider_status.get("claude_cli_configured")
            else "  Claude CLI: disabled or unavailable"
        )
    else:
        ollama_online = bool(models) or not isinstance(provider_status, dict)
        print(f"  Ollama: online ({len(models)} model(s))")
        if not models:
            errors.append("Ollama has no installed models")

    configured = (
        ("fast", config.fast_model),
        ("reasoning", config.reasoning_model),
        ("coding", config.coding_model),
    )
    for label, model in configured:
        ready = _installed_model(model, models)
        print(f"  {label.capitalize()}: {model} ({'ready' if ready else 'missing'})")
        if not ready:
            errors.append(f"Configured {label} model is missing")

    learning_model = getattr(config, "learning_model", None)
    if learning_model:
        ready = _installed_model(learning_model, models)
        print(f"  Learning: {learning_model} ({'ready' if ready else 'missing'})")
        if not ready:
            errors.append("Configured learning model is missing")

    aliases = {"auto", "fast", "reasoning", "coding", "deep"}
    if config.model.casefold() not in aliases:
        ready = _installed_model(config.model, models)
        print(f"  Selected: {config.model} ({'ready' if ready else 'missing'})")
        if not ready:
            errors.append("Explicitly selected model is missing")

    if deep:
        try:
            with Memory(config.data_dir / "jarvis.db") as memory:
                drift = memory.drift_report()
                canaries = run_capability_canaries(config, memory)
            passed = sum(item["status"] == "pass" for item in canaries)
            failed = [item for item in canaries if item["status"] == "fail"]
            skipped = sum(item["status"] == "skip" for item in canaries)
            print(
                f"  Capability canaries: {passed} passed, {len(failed)} failed, "
                f"{skipped} skipped"
            )
            for item in failed:
                print(
                    f"    FAIL {item['tool']}: {_safe_summary(item['reason'], 300)}"
                )
                errors.append(f"Capability canary failed: {item['tool']}")
            if drift:
                print(f"  Behavioral drift: {len(drift)} family finding(s)")
                for finding in drift:
                    signals = ", ".join(
                        item["signal"] for item in finding["signals"]
                    )
                    print(f"    {finding['family']}: {signals}")
            else:
                print("  Behavioral drift: no threshold crossings with sufficient data")
            print(f"  Self-inspection: {getattr(config, 'self_inspect', 'disabled')}")
        except (OSError, ValueError, sqlite3.Error, RuntimeError) as exc:
            errors.append(f"Deep diagnosis failed: {type(exc).__name__}")
            print(f"  Deep diagnosis: failed ({_safe_summary(exc, 300)})")

    if errors:
        for problem in dict.fromkeys(errors):
            print(f"  Check: {_safe_summary(problem)}")
        print("  Status: not ready")
        return 1
    print("  Status: ready")
    return 0


def _run_selftest(args: argparse.Namespace) -> int:
    config = Config.load()
    result = run_isolated_selftest(
        config,
        full=bool(args.full),
        anchors=bool(args.anchors),
    )
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    else:
        label = "anchors" if result.get("anchors") else (
            "full" if result["full"] else "core"
        )
        state = "passed" if result["passed"] else "failed"
        print(
            f"Isolated {label} self-test {state} in "
            f"{result['duration_seconds']:.3f}s."
        )
        if result["failing_test_ids"]:
            print("Failing tests:")
            for test_id in result["failing_test_ids"]:
                print(f"  - {test_id}")
        localization = result.get("localization") or {}
        suspects = localization.get("suspect_modules") or []
        if suspects:
            print("Ranked suspect modules:")
            for suspect in suspects[:5]:
                commit = f" commit {suspect['last_commit']}" if suspect["last_commit"] else ""
                print(
                    f"  - {suspect['module']} "
                    f"({suspect['imported_by_failing_tests']} failing import(s){commit})"
                )
        if not result["passed"]:
            detail = result["stderr"] or result["stdout"]
            if detail:
                print(_safe_detail(detail, 10_000))
    return 0 if result["passed"] else 3


def _display_memories(memory: Memory) -> None:
    records = memory.list_memories()
    if not records:
        print("No saved memories.")
        return
    for record in records:
        kind = _safe_summary(record.get("kind", "memory"), 30)
        content = _safe_summary(record.get("content", ""), 240)
        print(f"  {kind}: {content}")


def _display_tasks(memory: Memory) -> None:
    tasks = memory.list_tasks()
    if not tasks:
        print("No background tasks.")
        return
    for task in tasks:
        task_id = task.get("id", "?")
        status = _safe_summary(task.get("status", "unknown"), 20)
        attempt = int(task.get("attempt_count") or 0)
        maximum = int(task.get("max_attempts") or 0)
        preview = _safe_summary(task.get("prompt", ""), 120)
        specialist = (
            f" - specialist={_safe_summary(task.get('specialist_key'), 30)}"
            if task.get("specialist_key") else ""
        )
        print(
            f"  #{task_id} {status} - attempt {attempt}/{maximum}{specialist} - {preview}"
        )


def _display_learning(memory: Memory) -> None:
    topics = memory.list_learning_topics()
    if not topics:
        print("No recurring learning topics.")
        return
    for topic in topics:
        state = "on" if topic.get("enabled") else "off"
        preview = _safe_summary(topic.get("topic", ""), 140)
        print(f"  #{topic.get('id', '?')} every {topic.get('interval_hours', '?')}h ({state}) - {preview}")


def interactive() -> int:
    try:
        config = Config.load()
        memory = Memory(config.data_dir / "jarvis.db")
    except (OSError, ValueError, sqlite3.Error, RuntimeError) as exc:
        print(f"JARVIS could not start: {_safe_summary(exc)}", file=sys.stderr)
        return 1

    with memory:
        try:
            agent = Agent(config, memory, event)
        except OllamaError as exc:
            print(f"No configured model provider is ready: {_safe_summary(exc)}", file=sys.stderr)
            return 1

        conversation_id = memory.new_conversation()
        model_mode = config.model
        print(f"{_styled('JARVIS', '96')} - local-first - model {model_mode} - {config.autonomy}")
        print("Type /help for commands. Ctrl+C stops the current request.\n")
        while True:
            try:
                prompt = input(f"{_styled('You >', '96')} ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\nGoodbye.")
                return 0
            if not prompt:
                continue
            if prompt in {"/quit", "/exit"}:
                print("Goodbye.")
                return 0
            if prompt == "/help":
                print(
                    "/new  /model [auto|fast|reasoning|coding|deep|provider:name]  "
                    "/memory  /tasks  /quit"
                )
                continue
            if prompt == "/model":
                print(f"Model mode: {model_mode}")
                continue
            if prompt.startswith("/model "):
                model_mode = prompt.split(maxsplit=1)[1].strip()
                print(f"Model mode set to: {model_mode}")
                continue
            if prompt == "/new":
                conversation_id = memory.new_conversation()
                print("Started a new conversation.")
                continue
            if prompt == "/memory":
                _display_memories(memory)
                continue
            if prompt == "/tasks":
                _display_tasks(memory)
                continue
            try:
                if hasattr(memory, "log_activity"):
                    memory.log_activity(
                        "task", "foreground", "running",
                        details={"conversation_id": conversation_id},
                    )
                with _ForegroundLease(config.data_dir):
                    if hasattr(memory, "control_state"):
                        guard = RuntimeGuard(memory, config, background=False)
                        result = agent.run(
                            prompt, conversation_id, model_mode,
                            cancellation_guard=guard,
                            allow_companion_control=True,
                        )
                    else:
                        result = agent.run(
                            prompt, conversation_id, model_mode,
                            allow_companion_control=True,
                        )
                _record_reflection_safely(
                    memory, result, conversation_id=conversation_id
                )
                if hasattr(memory, "log_activity"):
                    memory.log_activity(
                        "task", "foreground", getattr(result, "status", "complete"),
                        details={"conversation_id": conversation_id},
                    )
                print(f"\n{_styled('JARVIS >', '96')} {result}\n")
                if getattr(result, "status", "complete") != "complete":
                    print(f"This request is incomplete: {_safe_summary(getattr(result, 'reason', ''))}\n")
            except KeyboardInterrupt:
                print("\nRequest stopped.\n")
            except OllamaError:
                print("\nOllama became unavailable. Start it and try again.\n", file=sys.stderr)


class _LeaseHeartbeat:
    def __init__(
        self,
        path: Path,
        task_id: int,
        worker_id: str,
        *,
        lease_seconds: int,
        interval_seconds: float,
        memory_factory: Callable[..., Memory] = Memory,
    ) -> None:
        self.path = path
        self.task_id = task_id
        self.worker_id = worker_id
        self.lease_seconds = lease_seconds
        self.interval_seconds = max(0.05, float(interval_seconds))
        self.memory_factory = memory_factory
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name=f"jarvis-lease-{task_id}", daemon=True)
        self.lost = False
        self.error_type: str | None = None

    def _run(self) -> None:
        try:
            with self.memory_factory(self.path, worker_id=self.worker_id) as memory:
                while not self._stop.wait(self.interval_seconds):
                    if not memory.renew_task_lease(
                        self.task_id,
                        worker_id=self.worker_id,
                        lease_seconds=self.lease_seconds,
                    ):
                        self.lost = True
                        return
        except Exception as exc:
            self.error_type = type(exc).__name__
            self.lost = True

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread.ident is not None:
            self._thread.join(timeout=max(2.0, min(self.interval_seconds + 2.0, 15.0)))


def _validate_poll(value: int) -> int:
    if isinstance(value, bool) or not MIN_POLL_SECONDS <= int(value) <= MAX_POLL_SECONDS:
        raise ValueError(f"poll interval must be between {MIN_POLL_SECONDS} and {MAX_POLL_SECONDS} seconds")
    return int(value)


def _poll_argument(value: str) -> int:
    try:
        return _validate_poll(int(value))
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError(
            f"must be between {MIN_POLL_SECONDS} and {MAX_POLL_SECONDS} seconds"
        ) from None


def _learning_interval(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a whole number of hours") from None
    if not 1 <= parsed <= 24 * 365:
        raise argparse.ArgumentTypeError("must be between 1 and 8760 hours")
    return parsed


def _retry_delay(attempt_count: int) -> int:
    exponent = max(0, min(int(attempt_count) - 1, 10))
    return min(MAX_RETRY_SECONDS, BASE_RETRY_SECONDS * (2**exponent))


def _wait(seconds: float, stop_event: threading.Event | None, sleep: Callable[[float], None]) -> None:
    if stop_event is None:
        sleep(seconds)
    else:
        stop_event.wait(seconds)


def _write_worker_heartbeat(config: Config, worker_id: str) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".worker-heartbeat-",
        dir=config.data_dir,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(f"{time.time():.6f} {os.getpid()} {worker_id}\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, config.data_dir / "worker.heartbeat")
    finally:
        if temporary.exists():
            temporary.unlink()


class _WorkerProcessLock:
    """Hold one kernel-backed lock for the lifetime of a continuous worker."""

    def __init__(self, data_dir: Path) -> None:
        self.path = Path(data_dir) / "worker.lock"
        self._stream: Any | None = None

    def acquire(self) -> bool:
        if self._stream is not None:
            return True
        stream = self.path.open("a+b")
        try:
            stream.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (ImportError, OSError):
            stream.close()
            return False
        self._stream = stream
        return True

    def close(self) -> None:
        stream, self._stream = self._stream, None
        if stream is None:
            return
        try:
            stream.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        except (ImportError, OSError):
            pass
        finally:
            stream.close()


def _write_foreground_lease(path: Path, token: str) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".foreground-heartbeat-",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(f"{time.time():.6f} {os.getpid()} {token}\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _foreground_request_active(data_dir: Path, *, now: float | None = None) -> bool:
    current = time.time() if now is None else float(now)
    active = False
    try:
        markers = data_dir.glob(
            f"{FOREGROUND_LEASE_PREFIX}*{FOREGROUND_LEASE_SUFFIX}"
        )
        for marker in markers:
            try:
                details = marker.lstat()
                attributes = getattr(details, "st_file_attributes", 0)
                if (
                    not stat.S_ISREG(details.st_mode)
                    or attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
                ):
                    continue
                with marker.open("r", encoding="utf-8") as stream:
                    payload = stream.read(257)
                if len(payload) > 256:
                    continue
                timestamp_text, pid_text, token = payload.strip().split()
                if not re.fullmatch(r"[1-9][0-9]*-[0-9a-f]{32}", token):
                    continue
                if marker.name != (
                    f"{FOREGROUND_LEASE_PREFIX}{token}{FOREGROUND_LEASE_SUFFIX}"
                ):
                    continue
                timestamp = float(timestamp_text)
                if int(pid_text) <= 0:
                    continue
                age = current - timestamp
                if -FOREGROUND_LEASE_FUTURE_SKEW_SECONDS <= age <= FOREGROUND_LEASE_TTL_SECONDS:
                    active = True
                elif age > FOREGROUND_LEASE_TTL_SECONDS:
                    marker.unlink(missing_ok=True)
            except (OSError, UnicodeError, ValueError):
                continue
    except OSError:
        return False
    return active


def _all_routed_models_cloud(config: Any) -> bool:
    models = (
        getattr(config, "fast_model", ""),
        getattr(config, "reasoning_model", ""),
        getattr(config, "coding_model", ""),
        getattr(config, "deep_model", getattr(config, "coding_model", "")),
        getattr(config, "learning_model", None),
    )
    configured = [str(model).casefold() for model in models if model]
    return bool(configured) and all(
        model.startswith(("openai:", "anthropic:", "codex-cli:", "claude-cli:"))
        for model in configured
    )


class _ForegroundLease:
    def __init__(self, data_dir: Path) -> None:
        self.token = f"{os.getpid()}-{uuid4().hex}"
        self.path = data_dir / (
            f"{FOREGROUND_LEASE_PREFIX}{self.token}{FOREGROUND_LEASE_SUFFIX}"
        )
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._active = False

    def _run(self) -> None:
        while not self._stop.wait(FOREGROUND_LEASE_HEARTBEAT_SECONDS):
            try:
                _write_foreground_lease(self.path, self.token)
            except OSError:
                return

    def __enter__(self) -> "_ForegroundLease":
        try:
            _foreground_request_active(self.path.parent)
            _write_foreground_lease(self.path, self.token)
        except OSError:
            return self
        self._active = True
        self._thread = threading.Thread(
            target=self._run,
            name=f"jarvis-foreground-{os.getpid()}",
            daemon=True,
        )
        self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=FOREGROUND_LEASE_HEARTBEAT_SECONDS + 1.0)
        if not self._active:
            return
        try:
            with self.path.open("r", encoding="utf-8") as stream:
                payload = stream.read(257)
            if len(payload) <= 256 and payload.strip().endswith(f" {self.token}"):
                self.path.unlink(missing_ok=True)
        except (OSError, UnicodeError):
            pass


def worker(
    poll_seconds: int,
    *,
    stop_event: threading.Event | None = None,
    max_cycles: int | None = None,
    sleep: Callable[[float], None] = time.sleep,
    memory_factory: Callable[..., Memory] = Memory,
    agent_factory: Callable[..., Agent] = Agent,
    heartbeat_factory: Callable[..., _LeaseHeartbeat] = _LeaseHeartbeat,
    manage_process_lock: bool = True,
    status_heartbeat: bool = True,
) -> int:
    poll_seconds = _validate_poll(poll_seconds)
    if max_cycles is not None and max_cycles < 1:
        raise ValueError("max_cycles must be positive")
    process_lock: _WorkerProcessLock | None = None
    try:
        config = Config.load()
        if max_cycles is None and manage_process_lock:
            process_lock = _WorkerProcessLock(config.data_dir)
            if not process_lock.acquire():
                print(
                    "Worker could not start: another continuous JARVIS worker is already running.",
                    file=sys.stderr,
                )
                return 2
        worker_id = f"worker:{os.getpid()}:{uuid4().hex}"
        memory = memory_factory(config.data_dir / "jarvis.db", worker_id=worker_id)
    except (OSError, ValueError, sqlite3.Error, RuntimeError) as exc:
        if process_lock is not None:
            process_lock.close()
        print(f"Worker could not start: {_safe_summary(exc)}", file=sys.stderr)
        return 1

    print("JARVIS worker is online. Press Ctrl+C to stop.")
    agent: Agent | None = None
    agent_project_id = 1
    agent_specialist_key: str | None = None
    service_failures = 0
    cycles = 0
    last_status_heartbeat = 0.0
    idle_since = 0.0
    try:
        if max_cycles is None and status_heartbeat:
            _write_worker_heartbeat(config, worker_id)
            last_status_heartbeat = time.monotonic()
            idle_since = last_status_heartbeat
        with memory:
            recovered = memory.recover_stale_tasks()
            if recovered["requeued"] or recovered["failed"]:
                print(
                    f"Recovered {recovered['requeued']} interrupted task(s); "
                    f"closed {recovered['failed']} exhausted task(s)."
                )

            while stop_event is None or not stop_event.is_set():
                if max_cycles is not None and cycles >= max_cycles:
                    break
                cycles += 1
                try:
                    if (
                        max_cycles is None
                        and status_heartbeat
                        and time.monotonic() - last_status_heartbeat
                        >= WORKER_STATUS_HEARTBEAT_SECONDS
                    ):
                        _write_worker_heartbeat(config, worker_id)
                        last_status_heartbeat = time.monotonic()
                    queued = memory.queue_due_learning()
                    if queued:
                        print(f"Queued {queued} scheduled learning task(s).")
                    scheduled = (
                        memory.queue_due_scheduled_jobs()
                        if hasattr(memory, "queue_due_scheduled_jobs")
                        else 0
                    )
                    if scheduled:
                        print(f"Queued {scheduled} recurring scheduled job(s).")

                    control_state = _runtime_state(memory)
                    if control_state == "stopped":
                        print("Emergency stop is active; worker exiting.")
                        return 0
                    if control_state == "paused":
                        _wait(poll_seconds, stop_event, sleep)
                        continue

                    if agent is None:
                        try:
                            agent = agent_factory(config, memory, event)
                            agent_project_id = 1
                            agent_specialist_key = None
                            service_failures = 0
                        except OllamaError as exc:
                            service_failures += 1
                            delay = min(
                                MAX_SERVICE_BACKOFF_SECONDS,
                                poll_seconds * (2 ** min(service_failures - 1, 5)),
                            )
                            print(
                                f"Model provider is unavailable ({_safe_summary(exc)}); waiting to reconnect.",
                                file=sys.stderr,
                            )
                            _wait(delay, stop_event, sleep)
                            continue
                        except Exception as exc:
                            service_failures += 1
                            delay = min(
                                MAX_SERVICE_BACKOFF_SECONDS,
                                poll_seconds * (2 ** min(service_failures - 1, 5)),
                            )
                            print(
                                "JARVIS agent initialization failed "
                                f"({type(exc).__name__}: {_safe_summary(exc)}); "
                                "waiting before retry.",
                                file=sys.stderr,
                            )
                            _wait(delay, stop_event, sleep)
                            continue

                    if (
                        _foreground_request_active(config.data_dir)
                        and not _all_routed_models_cloud(config)
                    ):
                        idle_since = time.monotonic()
                        _wait(FOREGROUND_YIELD_SECONDS, stop_event, sleep)
                        continue

                    task = memory.claim_task(
                        worker_id=worker_id,
                        lease_seconds=WORKER_LEASE_SECONDS,
                    )
                    if task is None:
                        if (
                            getattr(config, "proactive_enabled", False)
                            and hasattr(memory, "schedule_idle_activity")
                            and time.monotonic() - idle_since
                            >= getattr(config, "proactive_idle_seconds", 300)
                        ):
                            initiative = initiative_cycle(config, memory)
                            initiative_task = initiative.get("task_id")
                            if initiative_task is not None:
                                print(
                                    f"Initiative gate queued approved-domain task "
                                    f"#{initiative_task}."
                                )
                                idle_since = time.monotonic()
                                continue
                            scheduled = memory.schedule_idle_activity(
                                daily_limit=getattr(
                                    config, "proactive_daily_task_limit", 4
                                )
                            )
                            if scheduled is not None:
                                print(f"Idle scheduler queued proactive task #{scheduled}.")
                                idle_since = time.monotonic()
                                continue
                        idle_wait = (
                            min(poll_seconds, WORKER_STATUS_HEARTBEAT_SECONDS)
                            if max_cycles is None
                            else poll_seconds
                        )
                        _wait(idle_wait, stop_event, sleep)
                        continue

                    idle_since = time.monotonic()

                    task_id = int(task["id"])
                    task_project_id = int(task.get("project_id") or 1)
                    task_specialist_key = (
                        str(task.get("specialist_key") or "").strip().casefold() or None
                    )
                    project = (
                        memory.get_project(task_project_id)
                        if hasattr(memory, "get_project")
                        else {
                            "id": 1,
                            "enabled": 1,
                            "relative_path": ".",
                        }
                    )
                    if project is None or not bool(project.get("enabled")):
                        memory.fail_task(
                            task_id,
                            f"Project #{task_project_id} does not exist or is disabled",
                            worker_id=worker_id,
                            retry=False,
                        )
                        print(
                            f"Task #{task_id} cannot run because its project is unavailable.",
                            file=sys.stderr,
                        )
                        continue
                    try:
                        task_config = (
                            replace(
                                config,
                                workspace=resolve_project_workspace(
                                    config,
                                    str(project.get("relative_path") or ""),
                                ),
                            )
                            if hasattr(memory, "get_project") and is_dataclass(config)
                            else config
                        )
                    except (OSError, ValueError, PermissionError) as exc:
                        memory.fail_task(
                            task_id,
                            f"Project workspace rejected: {type(exc).__name__}",
                            worker_id=worker_id,
                            retry=False,
                        )
                        print(
                            f"Task #{task_id} project workspace was rejected.",
                            file=sys.stderr,
                        )
                        continue
                    if (
                        agent_project_id != task_project_id
                        or agent_specialist_key != task_specialist_key
                    ):
                        agent = agent_factory(task_config, memory, event)
                        agent_project_id = task_project_id
                        agent_specialist_key = task_specialist_key
                    set_specialist = getattr(agent, "set_specialist", None)
                    if callable(set_specialist):
                        set_specialist(task_specialist_key)
                    attempt = int(task.get("attempt_count") or 1)
                    maximum = int(task.get("max_attempts") or 1)
                    print(f"Running task #{task_id} (attempt {attempt}/{maximum}).")
                    if hasattr(memory, "log_activity"):
                        memory.log_activity(
                            "task", "start", "running", task_id=task_id,
                            details={
                                "attempt": attempt,
                                "maximum": maximum,
                                "goal_id": task.get("goal_id"),
                                "backlog_id": task.get("backlog_id"),
                                "specialist_key": task_specialist_key,
                            },
                        )
                    try:
                        heartbeat = heartbeat_factory(
                            config.data_dir / "jarvis.db",
                            task_id,
                            worker_id,
                            lease_seconds=WORKER_LEASE_SECONDS,
                            interval_seconds=WORKER_HEARTBEAT_SECONDS,
                            memory_factory=memory_factory,
                        )
                        heartbeat.start()
                    except Exception as exc:
                        delay = _retry_delay(attempt)
                        next_status = memory.fail_task(
                            task_id,
                            f"{type(exc).__name__} starting lease heartbeat",
                            worker_id=worker_id,
                            retry=True,
                            retry_delay_seconds=delay,
                        )
                        _record_reflection_safely(
                            memory,
                            AgentResult(
                                "Task could not start its lease heartbeat.",
                                status="incomplete",
                                reason=f"{type(exc).__name__} starting lease heartbeat",
                            ),
                            task=task,
                        )
                        if next_status == "queued":
                            print(f"Task #{task_id} will retry after a lease service problem.", file=sys.stderr)
                        elif next_status == "failed":
                            print(f"Task #{task_id} stopped after {attempt} attempt(s).", file=sys.stderr)
                        else:
                            print(f"Task #{task_id} lease was lost; result was not recorded.", file=sys.stderr)
                        continue
                    try:
                        task_prompt = str(task["prompt"])
                        learning_task = Agent._is_learning_task(task_prompt)
                        guard = (
                            RuntimeGuard(
                                memory,
                                task_config,
                                background=True,
                                upstream=lambda current=heartbeat, stop=stop_event: (
                                    current.lost
                                    or (stop is not None and stop.is_set())
                                ),
                            )
                            if hasattr(memory, "control_state")
                            else None
                        )
                        requested_model = str(task.get("requested_model") or "").strip() or None
                        if learning_task:
                            model_override = requested_model or getattr(task_config, "learning_model", None) or (
                                "reasoning"
                                if Agent._is_deep_research_task(task_prompt)
                                else task_config.background_model
                            )
                            result: AgentResult = agent.run(
                                task_prompt,
                                model_override=model_override,
                                cancellation_guard=(
                                    guard or (
                                        lambda current=heartbeat, stop=stop_event: (
                                            current.lost
                                            or (stop is not None and stop.is_set())
                                        )
                                    )
                                ),
                                task_id=task_id,
                            )
                        else:
                            run_kwargs: dict[str, Any] = {
                                "cancellation_guard": (
                                    guard or (
                                        lambda current=heartbeat, stop=stop_event: (
                                            current.lost
                                            or (stop is not None and stop.is_set())
                                        )
                                    )
                                ),
                                "task_id": task_id,
                            }
                            if requested_model is not None:
                                run_kwargs["model_override"] = requested_model
                            result = agent.run(task_prompt, **run_kwargs)
                    except KeyboardInterrupt:
                        heartbeat.stop()
                        try:
                            memory.fail_task(
                                task_id,
                                "Worker interrupted",
                                worker_id=worker_id,
                                retry=True,
                                retry_delay_seconds=0,
                            )
                        except Exception as exc:
                            print(
                                f"Task #{task_id} interruption cleanup hit {type(exc).__name__}.",
                                file=sys.stderr,
                            )
                        raise
                    except Exception as exc:
                        heartbeat.stop()
                        delay = _retry_delay(attempt)
                        next_status = memory.fail_task(
                            task_id,
                            f"{type(exc).__name__} while running task",
                            worker_id=worker_id,
                            retry=True,
                            retry_delay_seconds=delay,
                        )
                        _record_reflection_safely(
                            memory,
                            AgentResult(
                                "Task ended before a reliable result was produced.",
                                status="incomplete",
                                reason=f"{type(exc).__name__} while running task",
                            ),
                            task=task,
                        )
                        if next_status == "queued":
                            print(f"Task #{task_id} will retry after a temporary problem.", file=sys.stderr)
                        elif next_status == "failed":
                            print(f"Task #{task_id} stopped after {attempt} attempt(s).", file=sys.stderr)
                        else:
                            print(f"Task #{task_id} lease was lost; result was not recorded.", file=sys.stderr)
                        agent = None
                        agent_specialist_key = None
                        continue
                    finally:
                        heartbeat.stop()

                    if heartbeat.lost and not memory.renew_task_lease(
                        task_id,
                        worker_id=worker_id,
                        lease_seconds=WORKER_LEASE_SECONDS,
                    ):
                        print(f"Task #{task_id} lease was lost; result was not recorded.", file=sys.stderr)
                        agent = None
                        agent_specialist_key = None
                        continue

                    status = getattr(result, "status", "complete")
                    if status == "complete":
                        if memory.finish_task(task_id, str(result), worker_id=worker_id):
                            _record_reflection_safely(memory, result, task=task)
                            if hasattr(memory, "log_activity"):
                                memory.log_activity(
                                    "task", "finish", "complete", task_id=task_id,
                                    details={"tool_calls": getattr(result, "tool_calls", 0)},
                                )
                            print(f"Finished task #{task_id}.")
                            if learning_task:
                                try:
                                    export_kwargs: dict[str, Any] = {}
                                    constitution_hash = getattr(
                                        config, "constitution_sha256", None
                                    )
                                    if constitution_hash is not None:
                                        export_kwargs["constitution_sha256"] = constitution_hash
                                    manifest = export_verified_dataset(
                                        memory,
                                        config.data_dir / "training_export",
                                        **export_kwargs,
                                    )
                                    print(
                                        "Refreshed verified training export "
                                        f"({manifest['total_examples']} example(s))."
                                    )
                                except Exception as exc:
                                    print(
                                        "Training export refresh failed "
                                        f"({type(exc).__name__}); the finished task remains recorded.",
                                        file=sys.stderr,
                                    )
                        else:
                            print(f"Task #{task_id} lease was lost; result was not recorded.", file=sys.stderr)
                        continue

                    waiting_for_approval = bool(
                        getattr(result, "waiting_for_approval", False)
                    )
                    approval_id = getattr(result, "approval_id", None)
                    if waiting_for_approval and isinstance(approval_id, int):
                        approval_wait_status = memory.await_task_approval(
                            task_id,
                            approval_id,
                            worker_id=worker_id,
                        )
                        if hasattr(memory, "log_activity"):
                            memory.log_activity(
                                "task",
                                "approval_wait",
                                approval_wait_status or "failed",
                                task_id=task_id,
                                details={"approval_id": approval_id},
                            )
                        if approval_wait_status == "awaiting_approval":
                            print(
                                f"Task #{task_id} is awaiting approval #{approval_id}; "
                                "it will resume automatically after approval."
                            )
                        elif approval_wait_status == "queued":
                            print(
                                f"Approval #{approval_id} arrived while task #{task_id} was parking; "
                                "the task was requeued."
                            )
                        elif approval_wait_status == "failed":
                            print(
                                f"Task #{task_id} stopped because approval #{approval_id} was denied.",
                                file=sys.stderr,
                            )
                        else:
                            memory.fail_task(
                                task_id,
                                "Could not bind the pending approval to this task",
                                worker_id=worker_id,
                                retry=False,
                            )
                            print(
                                f"Task #{task_id} could not enter approval-wait state.",
                                file=sys.stderr,
                            )
                        continue

                    retryable = bool(getattr(result, "retryable", False))
                    reason = _safe_summary(
                        getattr(result, "reason", None) or "Agent did not satisfy completion checks",
                        500,
                    )
                    delay = _retry_delay(attempt) if retryable else 0
                    next_status = memory.fail_task(
                        task_id,
                        reason,
                        worker_id=worker_id,
                        retry=retryable,
                        retry_delay_seconds=delay,
                    )
                    _record_reflection_safely(memory, result, task=task)
                    if hasattr(memory, "log_activity"):
                        memory.log_activity(
                            "task", "finish", "incomplete", task_id=task_id,
                            details={"retryable": retryable, "next_status": next_status},
                        )
                    if next_status == "queued":
                        print(f"Task #{task_id} is incomplete and will retry.")
                    elif next_status == "failed":
                        print(f"Task #{task_id} is incomplete and needs attention.", file=sys.stderr)
                    else:
                        print(f"Task #{task_id} lease was lost; result was not recorded.", file=sys.stderr)
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    agent = None
                    agent_specialist_key = None
                    print(
                        f"Worker recovered from a {type(exc).__name__} service problem.",
                        file=sys.stderr,
                    )
                    _wait(poll_seconds, stop_event, sleep)
    except KeyboardInterrupt:
        print("\nWorker stopped.")
        return 0
    finally:
        if process_lock is not None:
            process_lock.close()
    return 0


def worker_pool(poll_seconds: int, concurrency: int | None = None) -> int:
    """Run bounded independent worker agents sharing only durable SQLite leases."""
    config = Config.load()
    count = int(
        getattr(config, "worker_concurrency", 3)
        if concurrency is None
        else concurrency
    )
    if not 1 <= count <= 8:
        raise ValueError("Worker concurrency must be between 1 and 8")
    if count == 1:
        return worker(poll_seconds)
    process_lock = _WorkerProcessLock(config.data_dir)
    if not process_lock.acquire():
        print(
            "Worker pool could not start: another continuous JARVIS worker is running.",
            file=sys.stderr,
        )
        return 2
    stop_event = threading.Event()
    results: list[int] = []
    results_lock = threading.Lock()

    def run_slot(index: int) -> None:
        result = worker(
            poll_seconds,
            stop_event=stop_event,
            manage_process_lock=False,
            status_heartbeat=index == 0,
        )
        with results_lock:
            results.append(result)
        if result:
            stop_event.set()

    threads = [
        threading.Thread(
            target=run_slot,
            args=(index,),
            name=f"jarvis-background-agent-{index + 1}",
            daemon=True,
        )
        for index in range(count)
    ]
    print(f"JARVIS worker pool is starting {count} isolated agent slots.")
    try:
        for thread in threads:
            thread.start()
        while any(thread.is_alive() for thread in threads):
            for thread in threads:
                thread.join(timeout=0.25)
    except KeyboardInterrupt:
        print("\nWorker pool stopping.")
        stop_event.set()
    finally:
        stop_event.set()
        for thread in threads:
            thread.join(timeout=max(2.0, poll_seconds + 1.0))
        process_lock.close()
    return max(results, default=0)


def _assert_regular_log_path(path: Path) -> None:
    if not os.path.lexists(path):
        return
    details = os.lstat(path)
    attributes = getattr(details, "st_file_attributes", 0)
    is_reparse = bool(
        attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )
    if (
        stat.S_ISLNK(details.st_mode)
        or is_reparse
        or not stat.S_ISREG(details.st_mode)
        or getattr(details, "st_nlink", 1) > 1
    ):
        raise PermissionError("Worker logs must be ordinary single-link files")


class _RotatingTextWriter:
    def __init__(
        self,
        path: Path,
        *,
        max_bytes: int = WORKER_LOG_MAX_BYTES,
        backups: int = WORKER_LOG_BACKUPS,
    ) -> None:
        if max_bytes < 1 or backups < 1:
            raise ValueError("Log rotation limits must be positive")
        self.path = path
        self.max_bytes = max_bytes
        self.backups = backups
        self._stream: TextIO | None = None
        self._lock = threading.Lock()
        self._size = 0
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._validate_paths()
        self._open()
        if self._size >= self.max_bytes:
            self._rotate()

    @property
    def encoding(self) -> str:
        return "utf-8"

    def _backup_path(self, index: int) -> Path:
        return self.path.with_name(f"{self.path.name}.{index}")

    def _validate_paths(self) -> None:
        _assert_regular_log_path(self.path)
        for index in range(1, self.backups + 1):
            _assert_regular_log_path(self._backup_path(index))

    def _open(self) -> None:
        self._stream = self.path.open(
            "a",
            encoding="utf-8",
            buffering=1,
            newline="\n",
        )
        self._size = self.path.stat().st_size

    def _rotate(self) -> None:
        self._validate_paths()
        if self._stream is not None:
            self._stream.flush()
            self._stream.close()
            self._stream = None
        for index in range(self.backups, 0, -1):
            source = self.path if index == 1 else self._backup_path(index - 1)
            target = self._backup_path(index)
            if source.exists():
                if target.exists():
                    target.unlink()
                os.replace(source, target)
        self._open()

    def write(self, value: str) -> int:
        with self._lock:
            if self._stream is None:
                raise ValueError("I/O operation on closed worker log")
            encoded_size = len(value.encode("utf-8", errors="replace"))
            if self._size and self._size + encoded_size > self.max_bytes:
                self._rotate()
            written = self._stream.write(value)
            self._stream.flush()
            self._size += encoded_size
            return written

    def flush(self) -> None:
        if self._stream is not None:
            self._stream.flush()

    def close(self) -> None:
        if self._stream is not None:
            self._stream.flush()
            self._stream.close()
            self._stream = None

    def __enter__(self) -> "_RotatingTextWriter":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def _run_logged_worker(
    poll_seconds: int,
    log_path: Path,
    concurrency: int | None = None,
) -> int:
    config = Config.load()
    expected = config.data_dir.resolve() / "worker.log"
    lexical = Path(os.path.abspath(log_path))
    if os.path.normcase(str(lexical)) != os.path.normcase(str(expected)):
        raise PermissionError("Worker log must be data/worker.log")
    with _RotatingTextWriter(expected) as stream:
        with redirect_stdout(stream), redirect_stderr(stream):
            return worker_pool(poll_seconds, concurrency)




def _run_ask(args: argparse.Namespace) -> int:
    config = Config.load()
    images = [
        ImageAttachment.from_path(path)
        for path in (getattr(args, "image", None) or [])
    ]
    with Memory(config.data_dir / "jarvis.db") as memory:
        agent = Agent(config, memory, event)
        if hasattr(memory, "log_activity"):
            memory.log_activity("task", "foreground", "running")
        with _ForegroundLease(config.data_dir):
            prompt = " ".join(args.prompt)
            if hasattr(memory, "control_state"):
                guard = RuntimeGuard(memory, config, background=False)
                run_kwargs: dict[str, Any] = {
                    "model_override": args.model,
                    "cancellation_guard": guard,
                    "allow_companion_control": True,
                }
                if images:
                    run_kwargs["attachments"] = images
                result: AgentResult = agent.run(prompt, **run_kwargs)
            else:
                run_kwargs = {
                    "model_override": args.model,
                    "allow_companion_control": True,
                }
                if images:
                    run_kwargs["attachments"] = images
                result = agent.run(prompt, **run_kwargs)
        _record_reflection_safely(memory, result)
        if hasattr(memory, "log_activity"):
            memory.log_activity(
                "task", "foreground", getattr(result, "status", "complete"),
                details={"tool_calls": getattr(result, "tool_calls", 0)},
            )
        print(result)
        return 0 if getattr(result, "status", "complete") == "complete" else 2


def _run_task(args: argparse.Namespace) -> int:
    config = Config.load()
    with Memory(config.data_dir / "jarvis.db") as memory:
        if args.task_command == "add":
            add_kwargs: dict[str, Any] = {}
            if getattr(args, "goal", None) is not None:
                add_kwargs["goal_id"] = args.goal
            if getattr(args, "project", None) is not None:
                add_kwargs["project_id"] = args.project
            if getattr(args, "model", None) is not None:
                add_kwargs["requested_model"] = args.model
            task_id = memory.add_task(" ".join(args.prompt), **add_kwargs)
            print(f"Queued task #{task_id}.")
        elif args.task_command == "show":
            tasks = [item for item in memory.list_tasks(limit=10_000) if item["id"] == args.task_id]
            if not tasks:
                raise ValueError(f"Task #{args.task_id} does not exist")
            item = tasks[0]
            print(f"Task #{item['id']}: {item['status']}")
            print(
                f"Project/model: #{item.get('project_id') or 1} / "
                f"{item.get('requested_model') or 'auto'}"
            )
            if item.get("specialist_key"):
                print(
                    f"Specialist: {item['specialist_key']} "
                    f"(delegated by {item.get('delegated_by') or 'unknown'})"
                )
            print(f"Prompt: {_safe_detail(item['prompt'], 50_000)}")
            if item.get("result"):
                print(f"Result:\n{_safe_detail(item['result'], 100_000)}")
            if item.get("last_error"):
                print(f"Last error: {_safe_detail(item['last_error'], 10_000)}")
        else:
            _display_tasks(memory)
    return 0


def _run_project(args: argparse.Namespace) -> int:
    config = Config.load()
    with Memory(config.data_dir / "jarvis.db") as memory:
        if args.project_command == "add":
            name = " ".join(args.name).strip()
            slug = re.sub(r"[^a-z0-9]+", "-", name.casefold()).strip("-")[:60]
            if not slug:
                raise ValueError("Project name must contain a letter or number")
            root, relative = create_project_workspace(config, slug)
            try:
                project_id = memory.add_project(name, relative)
            except Exception:
                try:
                    root.rmdir()
                except OSError:
                    pass
                raise
            print(f"Created project #{project_id}: {name} ({relative})")
        else:
            projects = memory.list_projects()
            for row in projects:
                print(
                    f"#{row['id']} {row['name']} [{row['relative_path']}] "
                    f"chats={row['conversation_count']} tasks={row['task_count']}"
                )
    return 0


def _run_agents(args: argparse.Namespace) -> int:
    config = Config.load()
    with Memory(config.data_dir / "jarvis.db") as memory:
        if args.agents_command == "list":
            rows = memory.list_specialist_agents()
            for row in rows:
                print(
                    f"{row['name']} [{row['agent_key']}] {row['status']} - "
                    f"{row['purpose']} - model={row['model_profile']} - "
                    f"completed={row['completed_tasks']} failed={row['failed_tasks']}"
                )
            return 0
        if args.agents_command == "delegate":
            prompt = " ".join(args.prompt).strip()
            selected = specialist_for_prompt(prompt)
            if selected is None:
                raise ValueError(
                    "No single-purpose specialist matches this assignment; clarify its purpose"
                )
            task_id = memory.delegate_specialist_task(
                prompt,
                specialist_key=selected.key,
                project_id=args.project,
                max_attempts=args.max_attempts,
            )
            print(
                f"JARVIS delegated task #{task_id} to {selected.name} "
                f"({selected.purpose}) using the {selected.model_profile} profile."
            )
            return 0
        rows = memory.specialist_task_reports(
            project_id=args.project,
            task_id=args.task_id,
            limit=args.limit,
        )
        if not rows:
            print("No specialist reports found for that project.")
            return 0
        for row in rows:
            result = _safe_summary(row.get("result") or row.get("last_error") or "pending", 500)
            print(
                f"#{row['id']} {row['specialist_name']} [{row['status']}] "
                f"model={row.get('requested_model') or 'auto'} - {result}"
            )
    return 0


def _run_learning(args: argparse.Namespace) -> int:
    config = Config.load()
    with Memory(config.data_dir / "jarvis.db") as memory:
        if args.learn_command == "add":
            topic_id = memory.add_learning_topic(" ".join(args.topic), args.every)
            print(f"Added recurring learning topic #{topic_id}.")
        elif args.learn_command in {"enable", "disable"}:
            enabled = args.learn_command == "enable"
            if not memory.set_learning_topic_enabled(args.topic_id, enabled):
                raise ValueError(f"Learning topic #{args.topic_id} does not exist")
            print(
                f"Learning topic #{args.topic_id} "
                f"{'enabled' if enabled else 'disabled'}."
            )
        else:
            _display_learning(memory)
    return 0


def _run_status(args: argparse.Namespace) -> int:
    config = Config.load()
    with Memory(config.data_dir / "jarvis.db") as memory:
        toolbox = ToolBox(config, memory)
        snapshot = build_self_model(config, memory, list(toolbox.tools))
        if args.json:
            print(json.dumps(snapshot, ensure_ascii=False, indent=2, default=str))
            return 0
        current = snapshot["current_status"]
        print(f"JARVIS: {current['control']['state']}")
        print(f"Self-model: {snapshot['identity']['awareness']}")
        print(f"Workspace: {config.workspace}")
        print(f"Proactive scheduler: {'enabled' if config.proactive_enabled else 'disabled'}")
        companion = current.get("screen_companion", {})
        print(
            "Screen Companion: "
            f"mode={companion.get('mode', 'disabled')} "
            f"paused={bool(companion.get('paused', True))}"
        )
        initiative_gate = initiative_eligibility(config, memory)
        print(
            f"Initiative: configured={initiative_gate['configured_mode']} "
            f"effective={initiative_gate['effective_mode']}"
        )
        print(f"Tasks: {current['task_counts']}")
        specialist_states = ", ".join(
            f"{item['name']}={item['status']}"
            for item in current.get("specialists", [])
        ) or "none"
        print(f"Specialists: {specialist_states}")
        print(f"Active goals: {sum(1 for item in current['goals'] if item['status'] == 'active')}")
        print(f"Pending approvals: {len(current['pending_approvals'])}")
        print(f"Memories/reflections: {current['memory_count']}/{current['reflection_count']}")
        print(f"Available tools: {len(snapshot['available_tools'])}")
        for bucket in ("demonstrated", "developing", "unknown"):
            entries = snapshot["capabilities"][bucket]
            labels = ", ".join(
                (
                    f"{item['family']} {item['success_rate']:.0%} (n={item['attempts']})"
                    if item["success_rate"] is not None
                    else f"{item['family']} (n={item['attempts']})"
                )
                for item in entries
            ) or "none"
            print(f"{bucket.title()}: {labels}")
        brier = snapshot["calibration"]["overall_brier"]
        print(
            "Calibration Brier: "
            + (f"{brier:.3f}" if brier is not None else "unknown (no resolved outcomes)")
        )
    return 0


def _run_competence(args: argparse.Namespace) -> int:
    config = Config.load()
    with Memory(config.data_dir / "jarvis.db") as memory:
        rows = memory.competence(args.family)
        calibration = memory.calibration()
        failures = memory.failure_histogram(args.family)
        lesson_effectiveness = memory.lesson_effectiveness(args.family)
        gates = [
            calibrated_meta_gate(memory, family)
            for family in (
                [args.family] if args.family else sorted(memory.PREDICTION_FAMILIES)
            )
        ]
        open_count = memory.open_prediction_count()
        if args.json:
            print(json.dumps({
                "competence": rows,
                "calibration": calibration,
                "failures": failures,
                "meta_gate": gates,
                "lesson_effectiveness": lesson_effectiveness,
                "open_predictions": open_count,
            }, ensure_ascii=False, indent=2, default=str))
            return 0
        if not rows:
            print("No resolved predictions yet.")
            if open_count:
                print(f"Open predictions: {open_count}")
            return 0
        print(
            f"{'family':20}{'n':>5}{'success':>9}{'brier':>8}"
            f"{'steps':>8}{'evidence':>10}  top failures"
        )
        for row in rows:
            top = memory.failure_histogram(row["family"], limit=3)
            summary = ", ".join(
                f"{item['failure_class']}x{item['n']}" for item in top
            ) or "-"
            evidence_rate = row.get("evidence_rate")
            evidence = "n/a" if evidence_rate is None else f"{evidence_rate:.0%}"
            mean_steps = row.get("mean_steps")
            steps = "n/a" if mean_steps is None else f"{mean_steps:.1f}"
            print(
                f"{row['family']:20}{row['attempts']:>5}"
                f"{row['success_rate']:>9.0%}{row['brier']:>8.3f}"
                f"{steps:>8}{evidence:>10}  {summary}"
            )
            gate = next(item for item in gates if item["family"] == row["family"])
            print(
                " " * 22
                + (
                    "calibrated authority: enabled"
                    if gate["allowed"]
                    else "calibrated authority: blocked - " + "; ".join(gate["reasons"])
                )
            )
        print("\nprior calibration (predicted -> observed):")
        for band in calibration:
            print(
                f"  {band['mean_predicted']:.2f} -> {band['observed']:.2f}"
                f"  (n={band['n']})"
            )
        if open_count:
            print(f"Open predictions: {open_count}")
        for item in lesson_effectiveness:
            print(
                f"Lesson outcomes {item['family']}: {item['resolved']}/"
                f"{item['applications']} resolved, success="
                + (
                    "n/a"
                    if item["success_rate"] is None
                    else f"{float(item['success_rate']):.0%}"
                )
            )
    return 0


def _run_usage(args: argparse.Namespace) -> int:
    config = Config.load()
    with Memory(config.data_dir / "jarvis.db") as memory:
        summary = memory.model_usage_summary(hours=None if args.all else args.hours)
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
        return 0


    label = "all recorded time" if summary["hours"] is None else f"last {summary['hours']}h"
    print(f"Model usage ({label}; prompt/response content is not stored)")
    if not summary["groups"]:
        print("No model calls recorded in this window.")
        return 0
    print(f"{'provider/model':42}{'profile':11}{'calls':>7}{'success':>9}{'input':>11}{'output':>11}{'avg ms':>9}{'p95 ms':>9}")
    for row in summary["groups"]:
        name = f"{row['provider']}/{row['model']}"[:41]
        print(
            f"{name:42}{row['profile'][:10]:11}{row['calls']:>7}"
            f"{row['success_rate']:>9.0%}{row['prompt_tokens']:>11}"
            f"{row['completion_tokens']:>11}{row['mean_latency_ms']:>9}"
            f"{row['p95_latency_ms']:>9}"
        )
    if summary["truncated"]:
        print("Warning: summary is limited to the newest 50,000 calls in this window.")
    return 0


def _run_memory_quality(args: argparse.Namespace) -> int:
    config = Config.load()
    indexed = 0
    if bool(getattr(args, "index", False)):
        owner = f"memory-index-cli:{os.getpid()}:{uuid4().hex}"
        while True:
            try:
                result = run_memory_index_batch(config, owner, limit=32)
            except (EmbeddingError, OSError, RuntimeError, ValueError) as exc:
                print(f"Memory indexing failed: {_safe_summary(exc)}", file=sys.stderr)
                return 2
            indexed += int(result.get("stored", 0))
            if not result.get("enabled"):
                print("Neural memory indexing is disabled by configuration.", file=sys.stderr)
                return 2
            if not result.get("claimed"):
                break
    with Memory(config.data_dir / "jarvis.db") as memory:
        quality = memory.memory_quality(limit=args.limit)
    payload = {
        "automatic_improvement": bool(config.memory_auto_improve),
        "neural_retrieval": config.memory_embeddings,
        "claim_clock": config.memory_claim_clock,
        "claim_stale_threshold": config.memory_claim_stale_threshold,
        "embedding_model": (
            config.memory_embedding_model
            if config.memory_embeddings != "disabled"
            else None
        ),
        **quality,
    }
    if args.json:
        if bool(getattr(args, "index", False)):
            payload["indexed_now"] = indexed
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    totals = payload["totals"]
    observed = totals.get("observed_utility")
    print(
        "Memory self-improvement: "
        + ("enabled" if payload["automatic_improvement"] else "disabled")
    )
    print(
        f"Neural retrieval: {payload['neural_retrieval']}"
        + (
            f" ({payload['embedding_model']})"
            if payload["embedding_model"]
            else ""
        )
    )
    print(f"Durable memories: {totals['memories']}")
    print(f"Cached neural embeddings: {totals['embeddings']}")
    print(f"Binary neural embeddings: {totals['binary_embeddings']}")
    eligible = int(totals.get("embedding_eligible") or 0)
    coverage = (
        1.0 if eligible == 0 else min(1.0, int(totals["embeddings"]) / eligible)
    )
    print(f"Neural index coverage: {coverage:.1%} ({totals['embeddings']}/{eligible})")
    print(f"Active embedding leases: {totals['active_embedding_leases']}")
    print(
        f"Cached semantic queries: {totals['cached_query_embeddings']} "
        f"({totals['query_embedding_cache_hits']} cache hit(s))"
    )
    if bool(getattr(args, "index", False)):
        print(f"Indexed now: {indexed}")
    print(
        "Versioned facts: "
        f"{totals['active_claims']} active, "
        f"{totals['disputed_claims']} disputed, "
        f"{totals['superseded_claims']} superseded "
        f"({totals['claim_events']} append-only events)"
    )
    print(
        f"Claim volatility clock: {payload['claim_clock']} at "
        f"{float(payload['claim_stale_threshold']):.0%} stale threshold; "
        f"{totals['claim_observations']} observation(s), "
        f"{totals['claim_clock_mature_predicates']}/"
        f"{totals['claim_clock_predicates']} predicate(s) mature, "
        f"{totals['claim_clock_stale_reads']}/"
        f"{totals['claim_clock_reads']} shadow/enforced read(s) stale"
    )
    print(
        f"Measured retrievals: {totals['resolved_retrievals']} resolved "
        f"of {totals['retrievals']}"
    )
    print(
        "Observed retrieval utility: "
        + ("unknown" if observed is None else f"{float(observed):.1%}")
    )
    for item in payload["measured_memories"]:
        print(
            f"  memory #{item['memory_id']} [{item['kind']}] - "
            f"utility {float(item['utility']):.1%} over "
            f"{item['resolved']} resolved use(s)"
        )
    return 0


def _run_control(args: argparse.Namespace) -> int:
    config = Config.load()
    state = {"pause": "paused", "resume": "running", "stop": "stopped"}[args.control_command]
    with Memory(config.data_dir / "jarvis.db") as memory:
        memory.set_control_state(state, getattr(args, "reason", None))
    print({"paused": "Background autonomy paused.", "running": "JARVIS resumed.", "stopped": "Emergency stop activated."}[state])
    return 0


def _run_goal(args: argparse.Namespace) -> int:
    config = Config.load()
    with Memory(config.data_dir / "jarvis.db") as memory:
        if args.goal_command == "add":
            goal_id = memory.add_goal(
                " ".join(args.title), args.description or "",
                kind=args.kind, priority=args.priority,
            )
            print(f"Created {args.kind} #{goal_id}.")
        elif args.goal_command == "set":
            if not memory.update_goal_status(args.goal_id, args.status):
                raise ValueError(f"Goal #{args.goal_id} does not exist")
            print(f"Goal #{args.goal_id} is now {args.status}.")
        else:
            goals = memory.list_goals()
            if not goals:
                print("No persistent goals or projects.")
            for item in goals:
                print(
                    f"#{item['id']} {item['kind']} {item['status']} "
                    f"p{item['priority']} - {_safe_summary(item['title'], 180)}"
                )
    return 0


def _run_journal(args: argparse.Namespace) -> int:
    config = Config.load()
    with Memory(config.data_dir / "jarvis.db") as memory:
        memory.configure_vault(config.vault_dir)
        if args.journal_command == "add":
            entry_id = memory.add_journal_entry(
                args.goal_id, " ".join(args.content), kind=args.kind
            )
            print(f"Added journal entry #{entry_id} to goal #{args.goal_id}.")
        else:
            entries = memory.list_journal(args.goal_id, args.limit)
            if not entries:
                print("No journal entries.")
            for item in reversed(entries):
                print(f"#{item['id']} {item['created_at']} {item['kind']}: {item['content']}")
    return 0


def _run_vault(args: argparse.Namespace) -> int:
    config = Config.load()
    vault = Vault(config.vault_dir)
    if not vault.enabled:
        print("Obsidian vault mirroring is disabled. Set JARVIS_VAULT to enable it.")
        return 0
    if args.vault_command == "status":
        notes = vault.list_notes()
        counts = {
            kind: sum(1 for note in notes if note.kind == kind)
            for kind in ("research", "lessons", "journal")
        }
        with Memory(config.data_dir / "jarvis.db") as memory:
            status = memory.vault_index_status(
                notes,
                model=(
                    config.memory_embedding_model
                    if config.memory_embeddings != "disabled"
                    else None
                ),
            )
        print(f"Vault: {vault.root}")
        print(
            f"Notes: {len(notes)} "
            f"(research {counts['research']}, lessons {counts['lessons']}, "
            f"journal {counts['journal']})"
        )
        print(
            f"Search index: {status['indexed']} record(s), "
            f"{'fresh' if status['fresh'] else 'reindex required'}"
        )
        if config.memory_embeddings != "disabled":
            print(f"Neural index: {status['semantic_indexed']} current embedding(s)")
        return 0
    owner = f"vault-index-cli:{os.getpid()}:{uuid4().hex}"
    total_stored = 0
    latest: dict[str, Any] = {}
    while True:
        latest = run_memory_index_batch(config, owner, limit=32)
        total_stored += int(latest.get("stored", 0))
        if not latest.get("enabled") or not latest.get("claimed"):
            break
    vault_result = latest.get("vault", {})
    if latest.get("vault_error"):
        print("Vault reindex could not read the configured vault.", file=sys.stderr)
        return 2
    print(
        f"Vault index synchronized {int(vault_result.get('notes', 0))} note(s); "
        f"{total_stored} neural embedding(s) stored."
    )
    return 0


def _run_doc(args: argparse.Namespace) -> int:
    config = Config.load()
    result = build_offline_document(
        config.workspace,
        args.source,
        args.output,
        args.document_type,
    )
    print(
        f"Created {result['type'].upper()} document: "
        f"{result['relative_path']} ({int(result['bytes'])} bytes)"
    )
    return 0


def _run_skill(args: argparse.Namespace) -> int:
    config = Config.load()
    if args.skill_command == "list":
        skills = list_available_skills(config.workspace)
        if not skills:
            print("No skills are available.")
            return 0
        for item in skills:
            if item.get("auto_distilled") is True:
                label = (
                    f"auto-distilled family={item.get('family') or 'unknown'} "
                    f"verified_outcomes={int(item.get('verified_outcomes') or 0)}"
                )
            elif item.get("origin") == "workspace-learned":
                label = "workspace-learned"
            else:
                label = "built-in"
            print(
                f"{item['name']} [{label}] - "
                f"{_safe_summary(item['description'], 300)}"
            )
        return 0
    if args.skill_command == "show":
        skill = read_available_skill(args.name, config.workspace)
        label = (
            "auto-distilled"
            if skill.get("auto_distilled") is True
            else skill.get("origin", "built-in")
        )
        print(f"{skill['name']} [{label}]")
        if skill.get("family"):
            print(
                f"Family: {skill['family']} · verified outcomes: "
                f"{int(skill.get('verified_outcomes') or 0)}"
            )
        print(f"SHA-256: {skill['sha256']}")
        print(f"Description: {_safe_summary(skill['description'], 300)}")
        print(_safe_detail(skill["content"], 32 * 1024))
        return 0
    forgotten = forget_learned_skill(config.workspace, args.name)
    print(f"Forgot learned skill: {forgotten['name']}")
    return 0


def _run_gateway(args: argparse.Namespace) -> int:
    from .gateway import GatewayRuntime

    config = Config.load()
    runtime = GatewayRuntime(config, event=lambda message: print(message, file=sys.stderr))
    if not runtime.enabled:
        print(
            "Private messaging gateway is disabled. Set JARVIS_GATEWAY_CHANNEL, "
            "JARVIS_GATEWAY_TOKEN, and JARVIS_GATEWAY_ALLOWED_IDS first."
        )
        return 0
    print(
        f"Jarvis private {config.gateway_channel} gateway online for "
        f"{len(config.gateway_allowed_ids)} allowlisted owner ID(s)."
    )
    if args.once:
        runtime.run_once()
    else:
        runtime.run_forever()
    return 0


def _run_preference(args: argparse.Namespace) -> int:
    config = Config.load()
    with Memory(config.data_dir / "jarvis.db") as memory:
        if args.preference_command == "set":
            value = " ".join(args.value)
            combined = f"{args.name}={value}"
            if contains_secret(combined):
                raise ValueError("Potential secret detected; preference was not stored")
            preference_id = memory.set_preference(args.name, value, source="user")
            print(f"Saved preference #{preference_id}.")
        else:
            preferences = memory.list_preferences()
            if not preferences:
                print("No explicit preferences.")
            for item in preferences:
                print(f"{item['name']} = {item['value']} ({item['source']})")
    return 0


def _run_feedback(args: argparse.Namespace) -> int:
    config = Config.load()
    content = " ".join(args.feedback)
    if contains_secret(content):
        raise ValueError("Potential secret detected; feedback was not stored")
    with Memory(config.data_dir / "jarvis.db") as memory:
        memory.remember_verified(
            content,
            kind="feedback",
            source="explicit user feedback",
            origin="explicit_user_feedback",
        )
        feedback_key = "feedback_" + hashlib.sha256(
            content.encode("utf-8")
        ).hexdigest()[:12]
        memory.set_preference(
            feedback_key, content, source="explicit user feedback", confidence=0.9
        )
        if args.preference_name and args.preference_value:
            memory.set_preference(
                args.preference_name, args.preference_value,
                source="explicit user feedback",
            )
    print("Feedback stored for future reflection and behavior.")
    return 0


def _run_subject(args: argparse.Namespace) -> int:
    config = Config.load()
    with Memory(config.data_dir / "jarvis.db") as memory:
        if args.subject_command == "approve":
            subject_id = memory.approve_subject(" ".join(args.subject), args.notes or "")
            print(f"Approved research subject #{subject_id}.")
        else:
            subjects = memory.list_subjects()
            if not subjects:
                print("No approved subjects.")
            for item in subjects:
                print(f"#{item['id']} {'enabled' if item['enabled'] else 'disabled'} - {item['subject']}")
    return 0


def _run_backlog(args: argparse.Namespace) -> int:
    config = Config.load()
    with Memory(config.data_dir / "jarvis.db") as memory:
        if args.backlog_command == "add":
            item_id = memory.add_backlog_item(
                args.kind, args.subject_id, " ".join(args.instructions or []),
                priority=args.priority, interval_hours=args.every, goal_id=args.goal,
            )
            print(f"Added proactive backlog item #{item_id}.")
        elif args.backlog_command in {"enable", "disable"}:
            enabled = args.backlog_command == "enable"
            if not memory.set_backlog_enabled(args.backlog_id, enabled):
                raise ValueError(f"Backlog item #{args.backlog_id} does not exist")
            print(f"Backlog item #{args.backlog_id} {'enabled' if enabled else 'disabled'}.")
        else:
            items = memory.list_backlog()
            if not items:
                print("No proactive backlog items.")
            for item in items:
                print(
                    f"#{item['id']} {item['kind']} p{item['priority']} "
                    f"{'on' if item['enabled'] else 'off'} every {item['interval_hours']}h "
                    f"- {_safe_summary(item['subject'], 160)}"
                )
    return 0


def _run_approval(args: argparse.Namespace) -> int:
    config = Config.load()
    with Memory(config.data_dir / "jarvis.db") as memory:
        if args.approval_command in {"approve", "deny"}:
            approved = args.approval_command == "approve"
            if not memory.decide_approval(
                args.approval_id, approved, ttl_hours=config.approval_ttl_hours
            ):
                raise ValueError("Approval request does not exist or was already decided")
            print(f"Approval #{args.approval_id} {'approved once' if approved else 'denied'}.")
        elif args.approval_command == "approve-always":
            grant_id = memory.decide_approval_always(args.approval_id)
            if grant_id is None:
                raise ValueError(
                    "Approval request is not pending or is not an eligible exact read-only action"
                )
            print(
                f"Approval #{args.approval_id} approved for this exact read-only "
                f"target until revoked (grant #{grant_id})."
            )
        elif args.approval_command == "approve-session":
            grant_id = memory.decide_approval_for_session(
                args.approval_id,
                ttl_hours=config.approval_ttl_hours,
            )
            if grant_id is None:
                raise ValueError(
                    "Approval request is not pending or is not an eligible exact "
                    "read-only conversation action"
                )
            print(
                f"Approval #{args.approval_id} approved for this exact read-only "
                f"target in this chat until expiry (grant #{grant_id})."
            )
        elif args.approval_command in {"revoke-grant", "revoke-always"}:
            if not memory.revoke_persistent_approval(args.grant_id):
                raise ValueError("Persistent approval grant does not exist or is already revoked")
            print(f"Persistent approval grant #{args.grant_id} revoked.")
        else:
            requests = memory.list_approvals(args.limit)
            if not requests:
                print("No approval requests.")
            for item in requests:
                print(
                    f"#{item['id']} {item['status']} {item['action']} - "
                    f"{_safe_summary(item['reason'], 180)}\n"
                    f"    scope: {_safe_summary(item.get('scope', 'legacy'), 200)}\n"
                    f"    resource: {_safe_resource(item['resource'])}"
                )
            grants = memory.list_persistent_approvals(
                args.limit, include_revoked=False
            )
            if grants:
                print("Active exact read-only grants:")
            for item in grants:
                grant_kind = str(item.get("grant_kind") or "always")
                lifetime = (
                    f"session scope={item.get('scope')} expires={item.get('expires_at')}"
                    if grant_kind == "session" else "always"
                )
                print(
                    f"  grant #{item['id']} {lifetime} {item['action']} - "
                    f"{_safe_summary(item['reason'], 180)}\n"
                    f"    resource: {_safe_resource(item['resource'])}"
                )
    return 0


def _run_activity(args: argparse.Namespace) -> int:
    config = Config.load()
    with Memory(config.data_dir / "jarvis.db") as memory:
        for item in reversed(memory.list_activity(args.limit)):
            task = f" task=#{item['task_id']}" if item["task_id"] is not None else ""
            print(f"{item['created_at']} {item['category']}/{item['action']} {item['status']}{task}")
    return 0


def _run_reflection(args: argparse.Namespace) -> int:
    config = Config.load()
    with Memory(config.data_dir / "jarvis.db") as memory:
        reflections = memory.list_reflections(args.limit)
        if not reflections:
            print("No reflections recorded.")
        for item in reversed(reflections):
            print(f"#{item['id']} {item['status']} task={item['task_id']}: {item['summary']}")
            if item["mistakes"]:
                print(f"  Mistakes/blockers: {item['mistakes']}")
            if item["improvements"]:
                print(f"  Improvement: {item['improvements']}")
    return 0


def _run_repair(args: argparse.Namespace) -> int:
    config = Config.load()
    with Memory(config.data_dir / "jarvis.db") as memory:
        if args.repair_command == "show":
            row = memory.get_repair_proposal(args.proposal_id)
            if row is None:
                raise ValueError(f"Repair proposal #{args.proposal_id} does not exist")
            print(f"Repair proposal #{row['id']}: {row['status']}")
            print(f"Created: {row['created_at']}")
            print(f"Trigger: {_safe_detail(row['trigger_text'], 4_000)}")
            print(f"Diff SHA-256: {row['diff_sha256']}")
            if row.get("void_reason"):
                print(f"Void reason: {_safe_detail(row['void_reason'], 4_000)}")
            print(f"Verification:\n{_safe_detail(row['verification_json'], 50_000)}")
            print(f"Diff:\n{_safe_detail(row['diff_text'], 200_000)}")
            print("Apply: unsupported by design; review and land changes through the normal code workflow.")
            return 0
        rows = memory.list_repair_proposals(args.limit)
        if not rows:
            print("No self-repair proposals recorded.")
        for row in reversed(rows):
            reason = f" - {row['void_reason']}" if row.get("void_reason") else ""
            print(f"#{row['id']} {row['status']} {row['created_at']}{reason}")
    return 0


def _run_recovery(args: argparse.Namespace) -> int:
    config = Config.load()
    with Memory(config.data_dir / "jarvis.db") as memory:
        if args.recovery_command == "test":
            result = run_recovery_test(config, memory)
            print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
            return 0 if result["passed"] else 3
        attestation = memory.latest_recovery_attestation()
        gate = initiative_eligibility(config, memory)
        print(json.dumps({
            "latest_attestation": attestation,
            "initiative_gate": gate,
        }, ensure_ascii=False, indent=2, default=str))
    return 0


def _run_domain(args: argparse.Namespace) -> int:
    config = Config.load()
    with Memory(config.data_dir / "jarvis.db") as memory:
        if args.domain_command == "approve":
            domain_id = memory.approve_work_domain(
                " ".join(args.name),
                kind=args.kind,
                project_id=args.project,
                max_tasks_per_day=args.max_per_day,
            )
            print(f"Approved bounded work domain #{domain_id}.")
        elif args.domain_command == "revoke":
            if not memory.revoke_work_domain(args.domain_id):
                raise ValueError(f"Work domain #{args.domain_id} does not exist")
            print(f"Revoked work domain #{args.domain_id}.")
        else:
            rows = memory.list_work_domains()
            if not rows:
                print("No work domains approved.")
            for row in rows:
                state = "approved" if row["enabled"] and row["standing_authorization"] else "revoked"
                print(
                    f"#{row['id']} {row['name']} [{row['kind']}] project=#{row['project_id']} "
                    f"max/day={row['max_tasks_per_day']} {state}"
                )
    return 0


def _run_initiative(args: argparse.Namespace) -> int:
    config = Config.load()
    with Memory(config.data_dir / "jarvis.db") as memory:
        result = initiative_eligibility(config, memory)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


def _run_brief(args: argparse.Namespace) -> int:
    config = Config.load()
    since = datetime.now(timezone.utc) - timedelta(hours=args.since)
    with Memory(config.data_dir / "jarvis.db") as memory:
        events = memory.list_initiative_events(since=since)
    if not events:
        print(f"No self-initiated activity in the last {args.since} hour(s).")
        return 0
    grouped: dict[str, list[dict[str, Any]]] = {}
    for event in reversed(events):
        grouped.setdefault(str(event.get("domain_name") or "Tier 0 observations"), []).append(event)
    for domain, items in grouped.items():
        print(f"\n{domain}")
        for item in items:
            task = f" task=#{item['task_id']}" if item.get("task_id") else ""
            print(
                f"  #{item['id']} tier={item['tier']} {item['signal_kind']} "
                f"{item['status']}{task}: {item['summary']}"
            )
            if item.get("result_summary"):
                print(f"    Result: {_safe_summary(item['result_summary'], 500)}")
    return 0


def _quality_argument(value: str) -> float:
    try:
        result = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("quality must be a number from 0 to 1") from None
    if not 0.0 <= result <= 1.0:
        raise argparse.ArgumentTypeError("quality must be a number from 0 to 1")
    return result


def _run_training(args: argparse.Namespace) -> int:
    config = Config.load()
    distillation_root = Path(
        getattr(args, "root", None) or (config.data_dir / "specialization")
    ).resolve()
    constitutional_root = Path(
        getattr(args, "root", None) or (config.data_dir / "constitutional")
    ).resolve()
    constitution_value = getattr(config, "constitution_path", None)
    fallback_root = Path(getattr(config, "root", Path(config.data_dir).parent))
    constitution_path = Path(
        constitution_value or (fallback_root / "CONSTITUTION.md")
    ).resolve()
    with Memory(config.data_dir / "jarvis.db") as memory:
        if args.training_command == "cai-init":
            result = initialize_constitutional_pack(
                constitutional_root, constitution_path
            )
            action = "Created" if result["created"] else "Found"
            print(
                f"{action} constitutional pack with {result['scenarios']} scenario(s) "
                f"at {constitutional_root}"
            )
            return 0
        if args.training_command == "cai-status":
            status = constitutional_status(constitutional_root, constitution_path)
            print(f"Scenarios: {status['scenarios']}")
            print(
                "Generated records: "
                f"{status['schema_accepted']} accepted of {status['generated_records']}"
            )
            print(
                f"Verified preference pairs: {status['accepted_pairs']} "
                f"across {status['accepted_splits']}"
            )
            ready = "yes" if status["data_volume_ready"] else "no"
            print(f"Constitutional data-volume gate passed: {ready}")
            for blocker in status["readiness_blockers"]:
                print(f"  - {blocker}")
            return 0
        if args.training_command == "cai-generate":
            initialize_constitutional_pack(constitutional_root, constitution_path)
            result = generate_constitutional_records(
                constitutional_root / "scenarios.jsonl",
                constitutional_root / "records.jsonl",
                constitution_path,
                _new_client(config),
                candidate_model=args.candidate_model or config.fast_model,
                critic_model=args.critic_model or "gpt-oss:20b",
                reviser_model=args.reviser_model or config.reasoning_model,
                samples=args.samples,
                limit=args.limit,
            )
            print(
                f"Constitutional generation: {result['generated']} accepted, "
                f"{result['schema_rejected']} schema-rejected "
                f"({result['total_records']} total record(s))."
            )
            return 0
        if args.training_command == "cai-verify":
            result = verify_constitutional_records(
                constitutional_root / "records.jsonl",
                constitutional_root / "verified.jsonl",
                constitution_path,
            )
            print(
                f"Constitutional hard checks: {result['passed']} passed, "
                f"{result['failed']} failed of {result['verified']} record(s)."
            )
            return 0 if not result["failed"] else 3
        if args.training_command == "cai-export":
            manifests = export_constitutional_datasets(
                constitutional_root / "verified.jsonl",
                constitutional_root / "export",
                constitution_path,
            )
            print(
                f"Exported {manifests['sft']['total_examples']} constitutional SFT "
                f"example(s) and {manifests['dpo']['total_examples']} preference pair(s)."
            )
            print("Automatic model promotion remains disabled.")
            return 0
        if args.training_command == "distill-init":
            result = initialize_pack(distillation_root)
            action = "Created" if result["created"] else "Found"
            print(f"{action} specialization pack with {result['tasks']} task(s) at {distillation_root}")
            return 0
        if args.training_command == "distill-status":
            status = distillation_status(distillation_root)
            print(f"Tasks: {status['tasks']} across {status['families']} family/families")
            print(f"Task splits: {status['task_splits']}")
            print(
                "Teacher candidates: "
                f"{status['schema_accepted']} accepted of {status['candidate_attempts']} attempted"
            )
            print(
                f"Hidden-test verification: {status['passed']} passed, "
                f"{status['failed']} failed"
            )
            return 0
        if args.training_command == "distill-generate":
            initialize_pack(distillation_root)
            model = args.model or config.coding_model
            result = generate_candidates(
                distillation_root / "tasks.jsonl",
                distillation_root / "candidates.jsonl",
                _new_client(config),
                model,
                limit=args.limit,
            )
            print(
                f"Teacher generation: {result['generated']} accepted, "
                f"{result['schema_rejected']} schema-rejected "
                f"({result['total_records']} total record(s))."
            )
            return 0
        if args.training_command == "distill-verify":
            result = verify_candidates(
                distillation_root / "tasks.jsonl",
                distillation_root / "candidates.jsonl",
                distillation_root / "verified.jsonl",
                allow_host_execution=args.allow_host_execution,
            )
            print(
                f"Hidden-test verification: {result['passed']} passed, "
                f"{result['failed']} failed of {result['verified']} candidate(s)."
            )
            return 0 if not result["failed"] else 3
        if args.training_command == "distill-export":
            manifest = export_sft_dataset(
                distillation_root / "verified.jsonl",
                distillation_root / "sft",
                constitution_sha256=config.constitution_sha256,
            )
            reward_manifest = export_reward_dataset(
                distillation_root / "tasks.jsonl",
                distillation_root / "grpo",
                constitution_sha256=config.constitution_sha256,
            )
            print(
                f"Exported {manifest['total_examples']} reward-verified SFT example(s) "
                f"to {distillation_root / 'sft'}"
            )
            print(
                f"Exported {reward_manifest['total_tasks']} isolated-reward task(s) "
                f"to {distillation_root / 'grpo'}"
            )
            return 0
        if args.training_command == "status":
            status = dataset_status(memory)
            print(f"Verified examples: {status['verified']} of {status['total']}")
            print(f"Splits: {status['splits']}")
            print(f"Task kinds: {status['task_kinds']}")
            print(f"Evaluation cases: {status['evaluation_cases']}")
            print(f"Training-eligible examples: {status['training_eligible']}")
            print(f"Source-quarantined examples: {status['source_quarantined']}")
            print(f"Quality-quarantined examples: {status['quality_quarantined']}")
            if status["quarantine_reasons"]:
                print(f"Quality quarantine reasons: {status['quarantine_reasons']}")
            ready = "yes" if status["ready_for_candidate_training"] else "no"
            print(f"Ready for candidate training: {ready}")
            for blocker in status["readiness_blockers"]:
                print(f"  - {blocker}")
            return 0
        if args.training_command == "export":
            output = args.output or (config.data_dir / "training_export")
            manifest = export_verified_dataset(
                memory,
                output,
                min_quality=args.min_quality,
                constitution_sha256=getattr(config, "constitution_sha256", None),
            )
            print(f"Exported {manifest['total_examples']} verified example(s) to {Path(output).resolve()}")
            return 0
        if args.training_command == "eval-add":
            case_id = memory.add_evaluation_case(
                args.name,
                " ".join(args.prompt),
                args.expected,
            )
            print(f"Saved evaluation case #{case_id}.")
            return 0
        if args.training_command == "eval-list":
            cases = memory.list_evaluation_cases()
            if not cases:
                print("No evaluation cases configured.")
            for case in cases:
                expected = parse_expected_terms(case["expected_contains_json"])
                print(f"#{case['id']} {case['name']}: expects {expected}")
            return 0
        if args.training_command == "benchmark":
            safe_config = replace(config, autonomy="readonly", execution_mode="disabled")
            agent = Agent(
                safe_config,
                memory,
                event,
                record_training=False,
                temperature=0.0,
            )
            cases = [case for case in memory.list_evaluation_cases() if case["enabled"]]
            if args.limit is not None:
                cases = cases[:args.limit]
            if not cases:
                print("No enabled evaluation cases configured.")
                return 1
            passed = 0
            for case in cases:
                expected = parse_expected_terms(case["expected_contains_json"])
                result = agent.run(
                    case["prompt"],
                    model_override=args.model,
                    prediction_origin="practice",
                )
                text = str(result).casefold()
                success = (
                    result.status == "complete"
                    and all(term.casefold() in text for term in expected)
                )
                passed += int(success)
                print(f"{'PASS' if success else 'FAIL'} - {case['name']}")
            print(f"Benchmark: {passed}/{len(cases)} passed")
            return 0 if passed == len(cases) else 3
    raise ValueError("Unknown training command")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jarvis",
        description="Bounded multi-provider autonomous agent",
    )
    sub = parser.add_subparsers(dest="command")
    doctor_parser = sub.add_parser(
        "doctor", help="check model providers and local readiness"
    )
    doctor_parser.add_argument(
        "--deep",
        action="store_true",
        help="also run safe capability canaries and behavioral drift checks",
    )
    selftest = sub.add_parser(
        "selftest", help="test a disposable copy of the Jarvis runtime"
    )
    selftest_mode = selftest.add_mutually_exclusive_group()
    selftest_mode.add_argument("--full", action="store_true")
    selftest_mode.add_argument(
        "--anchors",
        action="store_true",
        help="run the immutable Phase 5 behavioral anchor set",
    )
    selftest.add_argument("--json", action="store_true")
    sub.add_parser("ui", help="open the native Jarvis desktop chat interface")
    presence = sub.add_parser(
        "presence", help="run the always-on loopback web and voice interface"
    )
    presence.add_argument("--host", default=None, help="loopback host override")
    presence.add_argument("--port", type=int, default=None, help="local TCP port override")
    presence.add_argument("--no-browser", action="store_true")
    gateway = sub.add_parser(
        "gateway", help="run the owner-only private messaging bridge"
    )
    gateway.add_argument("--once", action="store_true", help=argparse.SUPPRESS)
    pairing = sub.add_parser(
        "pairing", help="create and revoke authenticated Presence device sessions"
    )
    pairing_sub = pairing.add_subparsers(dest="pairing_command", required=True)
    pairing_create = pairing_sub.add_parser("create", help="create a one-time device code")
    pairing_create.add_argument("--label", default="remote device")
    pairing_create.add_argument("--minutes", type=int, default=10)
    pairing_sub.add_parser("list", help="list paired device sessions")
    pairing_revoke = pairing_sub.add_parser("revoke", help="revoke one paired session")
    pairing_revoke.add_argument("session_id")
    pairing_sub.add_parser("revoke-all", help="revoke every paired session")
    status = sub.add_parser("status", help="show and persist the current self-model")
    status.add_argument("--json", action="store_true")
    competence = sub.add_parser(
        "competence",
        help="show measured prediction and outcome history",
    )
    memory_quality = sub.add_parser(
        "memory", help="inspect neural recall and automatic memory improvement"
    )
    memory_quality.add_argument("--json", action="store_true")
    memory_quality.add_argument("--limit", type=int, default=20)
    memory_quality.add_argument(
        "--index", action="store_true",
        help="safely finish the leased neural index before reporting quality",
    )
    competence.add_argument(
        "--family",
        choices=PREDICTION_FAMILY_CHOICES,
        default=None,
    )
    competence.add_argument("--json", action="store_true")
    usage = sub.add_parser(
        "usage",
        help="show prompt-free model token and latency measurements",
    )
    usage.add_argument("--hours", type=int, default=24)
    usage.add_argument("--all", action="store_true", help="include all recorded calls")
    usage.add_argument("--json", action="store_true")
    control = sub.add_parser("control", help="pause, resume, or emergency-stop JARVIS")
    control_sub = control.add_subparsers(dest="control_command", required=True)
    for name in ("pause", "resume", "stop"):
        item = control_sub.add_parser(name)
        item.add_argument("--reason", default=None)
    ask = sub.add_parser("ask", help="run one task")
    ask.add_argument("prompt", nargs="+")
    ask.add_argument(
        "--image",
        action="append",
        default=[],
        metavar="PATH",
        help="attach a PNG, JPEG, WebP, or GIF image (repeatable; max 4, 5 MiB each)",
    )
    ask.add_argument(
        "--model",
        default=None,
        help=(
            "auto, fast, reasoning, coding, deep, a local Ollama name, or "
            "openai:<model>/anthropic:<model>"
        ),
    )
    task = sub.add_parser("task", help="queue or list background tasks")
    task_sub = task.add_subparsers(dest="task_command", required=True)
    task_add = task_sub.add_parser("add")
    task_add.add_argument("prompt", nargs="+")
    task_add.add_argument("--goal", type=int, default=None)
    task_add.add_argument("--project", type=int, default=1)
    task_add.add_argument(
        "--model",
        default="auto",
        help="auto, fast, reasoning, coding, deep, or an explicit configured model",
    )
    task_sub.add_parser("list")
    task_show = task_sub.add_parser("show")
    task_show.add_argument("task_id", type=int)
    project = sub.add_parser("project", help="manage isolated agent project workspaces")
    project_sub = project.add_subparsers(dest="project_command", required=True)
    project_add = project_sub.add_parser("add")
    project_add.add_argument("name", nargs="+")
    project_sub.add_parser("list")
    agents = sub.add_parser(
        "agents", help="inspect and command Jarvis's isolated specialist agents"
    )
    agents_sub = agents.add_subparsers(dest="agents_command", required=True)
    agents_sub.add_parser("list")
    agents_delegate = agents_sub.add_parser("delegate")
    agents_delegate.add_argument("prompt", nargs="+")
    agents_delegate.add_argument("--project", type=int, default=1)
    agents_delegate.add_argument("--max-attempts", type=int, default=3)
    agents_reports = agents_sub.add_parser("reports")
    agents_reports.add_argument("--project", type=int, default=1)
    agents_reports.add_argument("--task-id", type=int, default=None)
    agents_reports.add_argument("--limit", type=int, default=20)
    work = sub.add_parser("worker", help="run queued tasks continuously")
    work.add_argument("--poll", type=_poll_argument, default=5)
    work.add_argument("--concurrency", type=int, default=None)
    work.add_argument("--log", type=Path, default=None, help=argparse.SUPPRESS)
    learn = sub.add_parser("learn", help="manage recurring learning topics")
    learn_sub = learn.add_subparsers(dest="learn_command", required=True)
    learn_add = learn_sub.add_parser("add")
    learn_add.add_argument("topic", nargs="+")
    learn_add.add_argument("--every", type=_learning_interval, default=24, metavar="HOURS")
    learn_sub.add_parser("list")
    for action in ("enable", "disable"):
        item = learn_sub.add_parser(action)
        item.add_argument("topic_id", type=int)
    goal = sub.add_parser("goal", help="manage persistent goals and projects")
    goal_sub = goal.add_subparsers(dest="goal_command", required=True)
    goal_add = goal_sub.add_parser("add")
    goal_add.add_argument("title", nargs="+")
    goal_add.add_argument("--description", default="")
    goal_add.add_argument("--kind", choices=("goal", "project"), default="goal")
    goal_add.add_argument("--priority", type=int, default=50)
    goal_sub.add_parser("list")
    goal_set = goal_sub.add_parser("set")
    goal_set.add_argument("goal_id", type=int)
    goal_set.add_argument("status", choices=("active", "paused", "completed", "cancelled"))
    journal = sub.add_parser("journal", help="manage project journals")
    journal_sub = journal.add_subparsers(dest="journal_command", required=True)
    journal_add = journal_sub.add_parser("add")
    journal_add.add_argument("goal_id", type=int)
    journal_add.add_argument("content", nargs="+")
    journal_add.add_argument("--kind", default="note")
    journal_list = journal_sub.add_parser("list")
    journal_list.add_argument("goal_id", type=int)
    journal_list.add_argument("--limit", type=int, default=100)
    vault = sub.add_parser("vault", help="inspect and rebuild the optional Obsidian mirror")
    vault_sub = vault.add_subparsers(dest="vault_command", required=True)
    vault_sub.add_parser("status")
    vault_sub.add_parser("reindex")
    document = sub.add_parser(
        "doc", help="build an office document offline from Markdown or JSON"
    )
    document.add_argument(
        "--type", dest="document_type", required=True,
        choices=SUPPORTED_DOCUMENT_TYPES,
    )
    document.add_argument("--from", dest="source", required=True, type=Path)
    document.add_argument("output", type=Path)
    skill = sub.add_parser("skill", help="inspect or forget bounded Jarvis skill guidance")
    skill_sub = skill.add_subparsers(dest="skill_command", required=True)
    skill_sub.add_parser("list")
    skill_show = skill_sub.add_parser("show")
    skill_show.add_argument("name")
    skill_forget = skill_sub.add_parser("forget")
    skill_forget.add_argument("name")
    preference = sub.add_parser("preference", help="manage durable user preferences")
    preference_sub = preference.add_subparsers(dest="preference_command", required=True)
    preference_set = preference_sub.add_parser("set")
    preference_set.add_argument("name")
    preference_set.add_argument("value", nargs="+")
    preference_sub.add_parser("list")
    feedback = sub.add_parser("feedback", help="store explicit feedback and optional preference")
    feedback.add_argument("feedback", nargs="+")
    feedback.add_argument("--preference-name", default=None)
    feedback.add_argument("--preference-value", default=None)
    subject = sub.add_parser("subject", help="manage subjects approved for proactive work")
    subject_sub = subject.add_subparsers(dest="subject_command", required=True)
    subject_approve = subject_sub.add_parser("approve")
    subject_approve.add_argument("subject", nargs="+")
    subject_approve.add_argument("--notes", default="")
    subject_sub.add_parser("list")
    backlog = sub.add_parser("backlog", help="manage the idle-time activity backlog")
    backlog_sub = backlog.add_subparsers(dest="backlog_command", required=True)
    backlog_add = backlog_sub.add_parser("add")
    backlog_add.add_argument("kind", choices=("research", "ideas", "prototype"))
    backlog_add.add_argument("subject_id", type=int)
    backlog_add.add_argument("instructions", nargs="*")
    backlog_add.add_argument("--priority", type=int, default=50)
    backlog_add.add_argument("--every", type=_learning_interval, default=168, metavar="HOURS")
    backlog_add.add_argument("--goal", type=int, default=None)
    backlog_sub.add_parser("list")
    for name in ("enable", "disable"):
        item = backlog_sub.add_parser(name)
        item.add_argument("backlog_id", type=int)
    approval = sub.add_parser("approval", help="review one-shot consequential-action approvals")
    approval_sub = approval.add_subparsers(dest="approval_command", required=True)
    approval_list = approval_sub.add_parser("list")
    approval_list.add_argument("--limit", type=int, default=100)
    for name in ("approve", "deny", "approve-session", "approve-always"):
        item = approval_sub.add_parser(name)
        item.add_argument("approval_id", type=int)
    for name in ("revoke-grant", "revoke-always"):
        approval_revoke = approval_sub.add_parser(name)
        approval_revoke.add_argument("grant_id", type=int)
    activity = sub.add_parser("activity", help="inspect the persistent activity log")
    activity.add_argument("--limit", type=int, default=100)
    reflection = sub.add_parser("reflection", help="inspect post-task reflections")
    reflection.add_argument("--limit", type=int, default=50)
    repair = sub.add_parser("repair", help="review isolated, never-applied self-repair drafts")
    repair_sub = repair.add_subparsers(dest="repair_command", required=True)
    repair_list = repair_sub.add_parser("list")
    repair_list.add_argument("--limit", type=int, default=50)
    repair_show = repair_sub.add_parser("show")
    repair_show.add_argument("proposal_id", type=int)
    recovery = sub.add_parser("recovery", help="test and inspect recovery readiness")
    recovery_sub = recovery.add_subparsers(dest="recovery_command", required=True)
    recovery_sub.add_parser("test")
    recovery_sub.add_parser("status")
    domain = sub.add_parser("domain", help="manage bounded standing work authorization")
    domain_sub = domain.add_subparsers(dest="domain_command", required=True)
    domain_approve = domain_sub.add_parser("approve")
    domain_approve.add_argument("name", nargs="+")
    domain_approve.add_argument(
        "--kind",
        choices=("research", "workspace_project", "maintenance"),
        required=True,
    )
    domain_approve.add_argument("--project", type=int, required=True)
    domain_approve.add_argument("--max-per-day", type=int, default=2)
    domain_sub.add_parser("list")
    domain_revoke = domain_sub.add_parser("revoke")
    domain_revoke.add_argument("domain_id", type=int)
    sub.add_parser("initiative", help="show the strict Phase 9 eligibility gate")
    brief = sub.add_parser("brief", help="summarize self-initiated activity")
    brief.add_argument("--since", type=_learning_interval, default=24, metavar="HOURS")
    training = sub.add_parser("training", help="curate data and run repeatable evaluations")
    training_sub = training.add_subparsers(dest="training_command", required=True)
    training_sub.add_parser("status")
    training_export = training_sub.add_parser("export")
    training_export.add_argument("--output", type=Path, default=None)
    training_export.add_argument("--min-quality", type=_quality_argument, default=0.8)
    eval_add = training_sub.add_parser("eval-add")
    eval_add.add_argument("name")
    eval_add.add_argument("prompt", nargs="+")
    eval_add.add_argument("--expected", action="append", required=True)
    training_sub.add_parser("eval-list")
    benchmark = training_sub.add_parser("benchmark")
    benchmark.add_argument("--model", default=None)
    benchmark.add_argument("--limit", type=int, default=None)
    for command in ("cai-init", "cai-status", "cai-verify", "cai-export"):
        constitutional = training_sub.add_parser(command)
        constitutional.add_argument("--root", type=Path, default=None)
    cai_generate = training_sub.add_parser("cai-generate")
    cai_generate.add_argument("--root", type=Path, default=None)
    cai_generate.add_argument("--candidate-model", default=None)
    cai_generate.add_argument("--critic-model", default=None)
    cai_generate.add_argument("--reviser-model", default=None)
    cai_generate.add_argument("--samples", type=int, default=2)
    cai_generate.add_argument("--limit", type=int, default=None)
    for command in ("distill-init", "distill-status", "distill-export"):
        distill = training_sub.add_parser(command)
        distill.add_argument("--root", type=Path, default=None)
    distill_generate = training_sub.add_parser("distill-generate")
    distill_generate.add_argument("--root", type=Path, default=None)
    distill_generate.add_argument("--model", default=None)
    distill_generate.add_argument("--limit", type=int, default=None)
    distill_verify = training_sub.add_parser("distill-verify")
    distill_verify.add_argument("--root", type=Path, default=None)
    distill_verify.add_argument(
        "--allow-host-execution",
        action="store_true",
        help="execute reviewed generated code against hidden tests on this computer",
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    _configure_stdio()
    args = _parser().parse_args(argv)
    try:
        _ensure_first_run_provider_setup(args)
        if args.command == "doctor":
            code = doctor(deep=bool(args.deep))
        elif args.command == "selftest":
            code = _run_selftest(args)
        elif args.command == "ui":
            from .ui import run_desktop_ui

            code = run_desktop_ui()
        elif args.command == "presence":
            from .presence import run_presence

            code = run_presence(
                host=args.host,
                port=args.port,
                open_browser=not args.no_browser,
            )
        elif args.command == "gateway":
            code = _run_gateway(args)
        elif args.command == "pairing":
            config = Config.load()
            with Memory(config.data_dir / "jarvis.db") as memory:
                if args.pairing_command == "create":
                    if config.presence_remote_access != "paired":
                        raise ValueError(
                            "Set JARVIS_PRESENCE_REMOTE_ACCESS=paired before creating a code"
                        )
                    result = memory.create_presence_pairing_code(
                        args.label, ttl_minutes=args.minutes
                    )
                    print(f"Pairing code: {result['code']}")
                    print(f"Expires: {result['expires_at']}")
                    print("Enter this only on the HTTPS Jarvis pairing screen.")
                elif args.pairing_command == "list":
                    sessions = memory.list_presence_sessions()
                    if not sessions:
                        print("No paired Presence sessions.")
                    for row in sessions:
                        state = "revoked" if row["revoked_at"] else "active"
                        print(
                            f"{row['session_id']}  {state:7}  {row['label']}  "
                            f"expires {row['expires_at']}"
                        )
                elif args.pairing_command == "revoke":
                    if not memory.revoke_presence_session(args.session_id):
                        raise ValueError("Active Presence session was not found")
                    print("Presence session revoked.")
                else:
                    print(
                        f"Revoked {memory.revoke_all_presence_sessions()} Presence session(s)."
                    )
            code = 0
        elif args.command == "status":
            code = _run_status(args)
        elif args.command == "competence":
            code = _run_competence(args)
        elif args.command == "memory":
            code = _run_memory_quality(args)
        elif args.command == "usage":
            code = _run_usage(args)
        elif args.command == "control":
            code = _run_control(args)
        elif args.command == "ask":
            code = _run_ask(args)
        elif args.command == "task":
            code = _run_task(args)
        elif args.command == "project":
            code = _run_project(args)
        elif args.command == "agents":
            code = _run_agents(args)
        elif args.command == "worker":
            code = (
                _run_logged_worker(args.poll, args.log, args.concurrency)
                if args.log
                else worker_pool(args.poll, args.concurrency)
            )
        elif args.command == "learn":
            code = _run_learning(args)
        elif args.command == "goal":
            code = _run_goal(args)
        elif args.command == "journal":
            code = _run_journal(args)
        elif args.command == "vault":
            code = _run_vault(args)
        elif args.command == "doc":
            code = _run_doc(args)
        elif args.command == "skill":
            code = _run_skill(args)
        elif args.command == "preference":
            code = _run_preference(args)
        elif args.command == "feedback":
            code = _run_feedback(args)
        elif args.command == "subject":
            code = _run_subject(args)
        elif args.command == "backlog":
            code = _run_backlog(args)
        elif args.command == "approval":
            code = _run_approval(args)
        elif args.command == "activity":
            code = _run_activity(args)
        elif args.command == "reflection":
            code = _run_reflection(args)
        elif args.command == "repair":
            code = _run_repair(args)
        elif args.command == "recovery":
            code = _run_recovery(args)
        elif args.command == "domain":
            code = _run_domain(args)
        elif args.command == "initiative":
            code = _run_initiative(args)
        elif args.command == "brief":
            code = _run_brief(args)
        elif args.command == "training":
            code = _run_training(args)
        else:
            code = interactive()
    except OllamaError as exc:
        print(f"Model provider is unavailable: {_safe_summary(exc)}", file=sys.stderr)
        code = 1
    except ProviderSetupRequired as exc:
        print(str(exc), file=sys.stderr)
        code = 2
    except (OSError, ValueError, sqlite3.Error, RuntimeError) as exc:
        print(f"JARVIS could not complete that command: {_safe_summary(exc)}", file=sys.stderr)
        code = 1
    if code:
        raise SystemExit(code)
