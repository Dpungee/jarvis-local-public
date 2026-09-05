from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import shutil
import sqlite3
import stat
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import is_dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, TextIO
from uuid import uuid4

from . import learning_ladder, memory_compaction, memory_graph
from .agent import Agent, AgentResult
from .attachments import ImageAttachment
from .config import Config, create_project_workspace, resolve_project_workspace
from .constitutional import (
    constitutional_status,
)
from .constitutional import (
    export_datasets as export_constitutional_datasets,
)
from .constitutional import (
    generate_records as generate_constitutional_records,
)
from .constitutional import (
    initialize_pack as initialize_constitutional_pack,
)
from .constitutional import (
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
from .governed_memory import (
    redact_skill_promotion_command,
    skill_promotion_receipt,
)
from .memory import DEFAULT_LEASE_SECONDS, Memory
from .memory_embeddings import EmbeddingError, run_memory_index_batch
from .memory_spine import SpineError
from .model_client import ModelClient, build_model_client, split_model_reference
from .offline_documents import SUPPORTED_DOCUMENT_TYPES, build_offline_document
from .ollama_client import OllamaError
from .proactive import (
    RuntimeGuard,
    build_self_model,
    calibrated_meta_gate,
    initiative_cycle,
    initiative_eligibility,
    record_result_reflection,
)
from .provider_setup import ProviderSetupRequired
from .provider_setup import ensure_ready as ensure_provider_ready
from .redaction import (
    contains_private_identifier_extended,
    contains_secret,
    is_redacted_descriptor,
    is_sensitive_key,
    redact_private_identifiers,
    redact_secrets,
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
from .specialists import specialist_for_prompt
from .strategy_transfer import STRATEGY_VOCABULARY
from .strategy_transfer_operator import (
    StrategyTransferOperatorError,
    build_trial_manifest_input,
    sanitized_trial_status,
    trial_status_line,
)
from .tools import ToolBox
from .training import dataset_status, export_verified_dataset, parse_expected_terms
from .vault import Vault

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


#: Distinguishes an ABSENT key from a present ``None``.  The two mean
#: different things for the equivalence number and must not collapse.
_UNSET = object()


def _compaction_health(db_path: Path) -> dict[str, Any]:
    """Run `verify_compaction` for `doctor`, or say why it could not.

    Read-only and never raises.  Three refusal shapes have to be distinguished
    from health, and the reason each one exists is that an operator would
    otherwise be told nothing at all:

    * the store file is absent -- nothing is created to look at it;
    * the store refuses to OPEN.  `Memory.__init__` itself raises on a
      downgraded store (`compaction_downgrade_refused`) and on one whose key
      sidecar was deleted (VTMF M5 N-4: losing the sidecar makes the whole
      store unopenable, which is the largest hazard in the phase, and
      `doctor` is where an operator should learn it rather than at a failed
      rehydrate months later).  `SpineError` is a `RuntimeError` subclass and
      carries `code=None` on that raise, so nothing here depends on a code;
    * the store opens but predates the compaction methods.

    A refusal is never health.  `checked` is the field callers must read
    before saying anything about the store: "no problems found" and "nothing
    was looked at" are different facts, and only the first is good news.
    """
    unchecked = {
        "checked": False,
        "ok": False,
        "chain_verified": None,
        "refusal": "no_store",
        "refusal_detail": None,
        "milestones_checked": 0,
        "counts": {},
        "problems": [],
    }
    try:
        if not db_path.exists():
            return unchecked
    except OSError as exc:
        return {**unchecked, "refusal": "store_unreadable",
                "refusal_detail": type(exc).__name__}
    try:
        with Memory(db_path) as memory:
            verify = getattr(memory, "verify_compaction", None)
            if not callable(verify):
                return {**unchecked, "refusal": "compaction_unavailable"}
            return dict(verify())
    except (RuntimeError, sqlite3.Error, OSError, TypeError, ValueError) as exc:
        # RuntimeError covers memory_spine.SpineError, which is how both the
        # deleted-sidecar and the downgrade refusals arrive.
        return {
            **unchecked,
            "refusal": "store_unavailable",
            "refusal_detail": _safe_summary(exc, 200),
        }


def _print_compaction_health(health: dict[str, Any], indent: str = "  ") -> None:
    """One line, and the qualifier rides ON it rather than beside it.

    Two adjacent lines can be read independently, and an operator scanning for
    red sees a green compaction line and moves on -- the absence-implies-status
    error in a UI rather than in a field.  So:

    * the chain warning is keyed off `chain_verified`, and the reason off
      `refusal`, never off `refusal` alone: a real refusal such as
      `schema_too_old` outranks the caller-supplied `spine_unverified`, so the
      two do not co-vary and keying on the reason would silently drop the
      warning in exactly the state that needs it;
    * an empty problem list is NOT health when the chain did not verify.  On a
      forged chain every receipt is present and every recorded digest matches
      its record, so `problems` comes back empty and the warning can only come
      from the qualifier;
    * what is withheld in that state is the verdict, not the detail: the
      problem list is still printed, because an operator whose chain is broken
      is precisely the one who needs to see what the compaction records say.
    """
    counts = health.get("counts") or {}
    chain_verified = health.get("chain_verified")
    chain_failed = chain_verified is False
    spine_clause = (
        "; spine chain did not verify - run 'jarvis spine verify', "
        "this result is downstream of it"
        if chain_failed else ""
    )

    if not health.get("checked"):
        reason = str(health.get("refusal") or "unknown")
        detail = str(health.get("refusal_detail") or "").strip()
        suffix = f" - {detail}" if detail else ""
        print(f"{indent}Compaction: not checked ({reason}){suffix}{spine_clause}")
        return

    milestones = int(counts.get("milestones") or 0)
    spans = int(counts.get("spans") or 0)
    problems = list(health.get("problems") or [])
    parts = [f"{milestones} milestone(s)", f"{spans} span(s)"]
    if chain_failed:
        # Deliberately no "verified" count here: the number would read as a
        # clean bill over a chain that does not verify.
        parts.append(f"{len(problems)} problem(s)")
    else:
        parts.append(f"{int(counts.get('verified') or 0)} verified")
        if problems:
            parts.append(f"{len(problems)} problem(s)")
    print(f"{indent}Compaction: {', '.join(parts)}{spine_clause}")

    equivalence = health.get("rebuild_equivalence_derived", _UNSET)
    if equivalence is not _UNSET:
        # Never a tick, a zero or a dash for an absent number: an operator
        # reading a dash concludes "nothing to report" when the truth is "the
        # comparison could not be made".
        if equivalence is None:
            why = str(health.get("equivalence_reason") or "not derivable")
            print(f"{indent}  derived equivalence: NOT COMPARED ({why})")
        else:
            print(f"{indent}  derived equivalence: {equivalence}")

    for problem in problems[:20]:
        try:
            milestone_id, kind, detail = problem[0], problem[1], problem[2]
        except (IndexError, KeyError, TypeError):
            continue
        # Fields, never values -- the _graph_problem_lines convention.
        print(f"{indent}  {milestone_id} {kind}: {_safe_summary(str(detail), 120)}")
    if len(problems) > 20:
        print(f"{indent}  ... and {len(problems) - 20} more")


#: Design 2.12 / H-7d: the one sentence an operator must read before the
#: bytes stop being plain rows.  It leads docs/COMPACTION.md too.
_COMPACTION_KEY_HAZARD = (
    "After compaction these turns can be read back only with "
    "<database>.memory-spine.key. Losing that file makes them permanently "
    "unreadable."
)


def _compaction_milestone_row(memory: Memory, handle: str) -> dict[str, Any] | None:
    """The milestone a handle names, or ``None``.

    The handle carries its own conversation and sequence, so this needs no
    lookup table: parse it, ask that conversation for its milestones, and
    match the sequence.  A handle whose 12 hex characters disagree with the
    stored span is rejected by ``rehydrate``, not here -- this is metadata.
    """
    try:
        parsed = memory_compaction.parse_handle(handle)
    except (AttributeError, TypeError, ValueError):
        return None
    try:
        report = memory.conversation_milestones(
            int(parsed.conversation_id), limit=1_000
        )
    except (RuntimeError, sqlite3.Error, TypeError, ValueError):
        return None
    for row in list(report.get("rows") or []):
        if int(row.get("seq") or 0) == int(parsed.seq):
            return dict(row)
    return None


def _print_compaction_plan(report: dict[str, Any]) -> None:
    """Counts, ids and handles.  Never a message body."""
    refusal = report.get("refusal")
    if refusal:
        print(
            f"Compaction refused: {refusal}"
            + (f" - {_safe_summary(report.get('refusal_detail'), 200)}"
               if report.get("refusal_detail") else "")
        )
        return
    spans = list(report.get("spans") or [])
    print(
        f"Compaction plan for conversation {report.get('conversation_id')}: "
        f"{report.get('candidate_rows', 0)} candidate row(s), "
        f"{report.get('candidate_chars', 0)} chars, {len(spans)} span(s)."
    )
    held = list(report.get("held_back_messages") or [])
    if held:
        print(
            f"  held back for live proposals: {len(held)} message(s) "
            "(they stay live and readable)"
        )
    for span in spans:
        print(
            f"  seq {span.get('seq')}  messages "
            f"{span.get('first_message_id')}-{span.get('last_message_id')}  "
            f"{span.get('message_count')} row(s)  "
            f"{span.get('source_chars')} chars  "
            f"summary {span.get('summary_chars')} chars"
        )
    for skipped in list(report.get("skipped") or [])[:20]:
        print(f"  skipped: {_safe_summary(str(skipped), 160)}")


def _run_compaction_run(memory: Memory, args: argparse.Namespace) -> int:
    """``compaction run --conversation N [--apply [--yes [--plan TOKEN]]]``.

    The exit codes of ``graph rebuild``: 0 planned or applied, 1 a refusal
    with nothing written, 2 a flag combination that would have changed
    something without the operator saying so.
    """
    json_output = bool(getattr(args, "json", False))
    apply = bool(getattr(args, "apply", False))
    yes = bool(getattr(args, "yes", False))
    requested_token = str(getattr(args, "plan", None) or "").strip()
    if yes and not apply:
        print("--yes requires --apply; nothing changed.")
        return 2
    if requested_token and not (apply and yes):
        print("--plan requires --apply --yes; nothing changed.")
        return 2

    plan = memory.compact_conversation(int(args.conversation))
    if not apply:
        if json_output:
            print(json.dumps(plan, ensure_ascii=False, indent=2, default=str))
        else:
            _print_compaction_plan(plan)
            token = plan.get("plan_token")
            if token and plan.get("spans"):
                print(f"  plan token: {token}")
                print(f"\n{_COMPACTION_KEY_HAZARD}")
                print(
                    f"Nothing applied. Re-run with --apply --yes --plan {token}"
                )
        return 1 if plan.get("refusal") else 0
    if not yes:
        # The plan is printed again rather than remembered: the operator
        # confirms the pass in front of them, not one from a previous run.
        if not json_output:
            _print_compaction_plan(plan)
            print(f"\n{_COMPACTION_KEY_HAZARD}")
        token = plan.get("plan_token")
        hint = f" --plan {token}" if token else ""
        print(f"Re-run with --apply --yes{hint} to compact exactly this plan.")
        return 2

    applied = memory.compact_conversation(
        int(args.conversation), apply=True, plan_token=requested_token or None
    )
    if json_output:
        print(json.dumps(applied, ensure_ascii=False, indent=2, default=str))
        return 1 if applied.get("refusal") else 0
    refusal = applied.get("refusal")
    if refusal:
        print(f"Compaction refused: {refusal}; nothing was written.")
        return 1
    spans = list(applied.get("spans") or [])
    print(
        f"Compacted conversation {applied.get('conversation_id')}: "
        f"{len(spans)} span(s) written."
    )
    for span in spans:
        print(f"  {span.get('handle')}")
    return 0


def _run_compaction_show(memory: Memory, args: argparse.Namespace) -> int:
    """``compaction show --handle H [--rehydrate]``.

    Metadata and summary by default.  The original bytes only with
    ``--rehydrate``, and only from a terminal with a typed confirmation --
    the posture of ``spine tail``, which prints payload keys and never
    values.
    """
    json_output = bool(getattr(args, "json", False))
    handle = str(getattr(args, "handle", "") or "").strip()
    row = _compaction_milestone_row(memory, handle)
    if row is None:
        print("No milestone for that handle in this store.")
        return 1
    if json_output and not getattr(args, "rehydrate", False):
        print(json.dumps(row, ensure_ascii=False, indent=2, default=str))
        return 0
    if not json_output:
        ids = dict(row.get("message_ids") or {})
        print(f"Milestone {row.get('handle')}")
        print(
            f"  messages {ids.get('first')}-{ids.get('last')} "
            f"({ids.get('count')} rows)   outcome {row.get('outcome')}"
        )
        print(f"  summary: {_safe_summary(str(row.get('summary') or ''), 1200)}")
        for field in ("claim_keys", "files_touched"):
            values = list(row.get(field) or [])
            if values:
                print(f"  {field}: {len(values)}")
    if not getattr(args, "rehydrate", False):
        return 0

    if not sys.stdin.isatty():
        print(
            "--rehydrate prints the original conversation text and is refused "
            "outside a terminal; nothing was read."
        )
        return 2
    print(
        "\n--rehydrate prints the ORIGINAL message text of this span, "
        "including anything the operator typed."
    )
    try:
        confirmation = input("Type the word rehydrate to continue: ")
    except (EOFError, KeyboardInterrupt):
        # A closed or interrupted stdin is a refusal, never a crash -- and
        # never a silent yes.  isatty() is not sufficient on its own: it is
        # true under some runners whose stdin still reads EOF immediately.
        print(chr(10) + "Not confirmed; nothing was read.")
        return 2
    if confirmation.strip() != "rehydrate":
        print("Not confirmed; nothing was read.")
        return 2
    try:
        span = memory.rehydrate(handle)
    except RuntimeError as exc:
        code = getattr(exc, "code", None) or "store_unavailable"
        print(f"Rehydration refused: {code}; nothing was returned.")
        return 1
    for message in list(span.get("messages") or []):
        print(
            f"  [{message.get('id')}] {message.get('created_at')} "
            f"{message.get('role')}: {message.get('content')}"
        )
    return 0


def _run_compaction(args: argparse.Namespace) -> int:
    """Operator surfaces over transcript compaction (VTMF M5 half A).

    ``status``, ``milestones`` and ``verify`` print ids, counts and handles
    only.  ``show`` adds the deterministic summary.  Original message text
    appears only behind ``show --rehydrate`` with a typed confirmation.
    """
    config = Config.load()
    json_output = bool(getattr(args, "json", False))
    db_path = config.data_dir / "jarvis.db"
    with Memory(db_path) as memory:
        if args.compaction_command == "status":
            counts = memory_compaction.compaction_row_counts(memory.db)
            payload = {
                "ready": bool(counts),
                "milestones": int(counts.get("memory_milestones", 0) or 0),
                "spans": int(counts.get("memory_compacted_spans", 0) or 0),
            }
            if json_output:
                print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
            elif not payload["ready"]:
                print("This store has no compaction tables (schema below 50).")
            else:
                print(
                    f"Compaction: {payload['milestones']} milestone(s), "
                    f"{payload['spans']} span(s) stored."
                )
                print("  run compaction verify to check them against the spine")
            return 0
        if args.compaction_command == "verify":
            health = _compaction_health(db_path)
            if json_output:
                print(json.dumps(health, ensure_ascii=False, indent=2, default=str))
            else:
                _print_compaction_health(health, indent="")
            if not health.get("checked"):
                return 1
            return 0 if health.get("ok") else 1
        if args.compaction_command == "milestones":
            report = memory.conversation_milestones(
                int(args.conversation), limit=int(getattr(args, "limit", 50) or 50)
            )
            if json_output:
                print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
                return 0
            rows = list(report.get("rows") or [])
            mode = str((report.get("report") or {}).get("mode") or "unknown")
            print(
                f"Conversation {args.conversation}: {len(rows)} milestone(s) "
                f"[{mode}]"
            )
            for row in rows:
                ids = dict(row.get("message_ids") or {})
                print(
                    f"  seq {row.get('seq')}  {row.get('handle')}  "
                    f"messages {ids.get('first')}-{ids.get('last')} "
                    f"({ids.get('count')} rows)  outcome {row.get('outcome')}"
                )
            if rows:
                print("  run compaction show --handle H for a summary")
            return 0
        if args.compaction_command == "show":
            return _run_compaction_show(memory, args)
        if args.compaction_command == "run":
            return _run_compaction_run(memory, args)
    return 2


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

    # VTMF M5 design 2.12: informational, and it must NOT change the exit
    # code.  It is deliberately outside the `deep` branch: a deleted key
    # sidecar makes the whole store unopenable, and an operator should
    # learn that from an ordinary `doctor` run.
    _print_compaction_health(_compaction_health(config.data_dir / "jarvis.db"))

    if deep:
        try:
            with Memory(config.data_dir / "jarvis.db") as memory:
                drift = memory.drift_report()
                canaries = run_capability_canaries(config, memory)
                # Design 3.7 and R-5 / ruling 20: doctor prints both
                # ladder counts and the ledger's coverage.  A non-zero
                # unverified count is a warning; a legacy count is
                # informational; a coverage problem is neither -- it means
                # the ledger no longer re-derives, which is the tamper
                # design 2.4 calls the important one.
                ladder_coverage = _ladder_coverage(memory)
                try:
                    ladder_rows = [
                        dict(row) for row in memory.ladder_promotions()
                    ]
                except (AttributeError, RuntimeError, sqlite3.Error, ValueError):
                    ladder_rows = []
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
            legacy_live = sum(
                1 for row in ladder_rows
                if str(row.get("stage")) == "unapproved_legacy"
            )
            if legacy_live:
                print(f"  {legacy_live} legacy skills live without approval")
            if ladder_coverage.get("checked") and int(
                ladder_coverage.get("rows") or 0
            ):
                _print_ladder_coverage(ladder_coverage, indent="  ")
                if not ladder_coverage.get("coverage_intact", True):
                    errors.append(
                        "Calibration ledger does not re-derive"
                    )
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
    """``/memory``: the newest rows with the id ``Erase memory #<id>`` names.

    ``memories.id`` is explicit and never reused since schema 47, so it is the
    operator-facing identity of an ordinary memory (design 6.1).
    """
    try:
        records = memory.list_memories(with_ids=True)
    except TypeError:
        records = memory.list_memories()
    if not records:
        print("No saved memories.")
        return
    for record in records:
        kind = _safe_summary(record.get("kind", "memory"), 30)
        content = _safe_summary(record.get("content", ""), 240)
        identifier = record.get("id")
        prefix = f"#{int(identifier)} " if isinstance(identifier, int) else ""
        print(f"  {prefix}{kind}: {content}")


def _display_project_facts(memory: Memory, project_id: int, subject: str = "") -> None:
    """List active governed project facts through the same screened read path."""
    try:
        facts = memory.current_claims(
            str(subject or ""),
            limit=50,
            clock_mode="disabled",
            project_id=int(project_id),
        )
    except (RuntimeError, ValueError) as exc:
        print(f"Project facts unavailable: {_safe_summary(exc)}")
        return
    facts = [item for item in facts if str(item.get("scope") or "") != "global"]
    if not facts:
        report = memory.claim_recall_report() if hasattr(memory, "claim_recall_report") else {}
        if report.get("abstained") and str(report.get("mode") or "") not in {
            "screened", "project-unavailable"
        }:
            # An abstention is not "no facts": the pool overflowed or was
            # ambiguous. Say so and offer the narrower read.
            reason = _safe_summary(report.get("reason") or report.get("mode"), 80)
            print(
                f"Project fact listing abstained for project {int(project_id)}: "
                f"{reason}. Filter by subject words to narrow the read."
            )
            return
        reason = report.get("reason") if report.get("abstained") else None
        suffix = f" ({_safe_summary(reason, 80)})" if reason else ""
        print(f"No active project facts for project {int(project_id)}{suffix}.")
        return
    print(f"Active project facts for project {int(project_id)} (newest first):")
    for item in facts:
        subject_text = _safe_summary(item.get("subject", ""), 60)
        predicate_text = _safe_summary(item.get("predicate", ""), 60)
        value_text = _safe_summary(item.get("value", ""), 160)
        status = _safe_summary(item.get("status", ""), 12)
        print(f"  {subject_text} | {predicate_text} | {value_text} [{status}]")
    print(
        'Update with: Remember this project fact: {"subject":"...","predicate":"...","value":"..."}'
    )
    print('Retire with: Forget this project fact: {"subject":"...","predicate":"..."}')


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
                    "/memory  /facts  /ladder  /tasks  /quit"
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
            if prompt == "/facts":
                chat_project_id = 1
                try:
                    conversation_project = memory.conversation_project(conversation_id)
                    if conversation_project is not None:
                        chat_project_id = int(conversation_project["id"])
                except (AttributeError, KeyError, RuntimeError, TypeError, ValueError):
                    chat_project_id = 1
                _display_project_facts(memory, chat_project_id)
                continue
            if prompt == "/ladder":
                # Rendered by the CLI BEFORE any model call, exactly as
                # /memory and /facts are.  This is one of only three
                # surfaces that print a staged promotion's confirmation
                # code, and the code never travels through a prompt or a
                # reply to get here (design 6.2 item 4, 6.3).
                chat_project_id = 1
                try:
                    conversation_project = memory.conversation_project(
                        conversation_id
                    )
                    if conversation_project is not None:
                        chat_project_id = int(conversation_project["id"])
                except (
                    AttributeError, KeyError, RuntimeError, TypeError, ValueError,
                ):
                    chat_project_id = 1
                _display_ladder(config, memory, chat_project_id)
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


def _trial_positive_integer(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a positive integer") from None
    if not 1 <= parsed <= 2_147_483_647:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def _trial_sample_cap(value: str) -> int:
    parsed = _trial_positive_integer(value)
    if not 40 <= parsed <= 200 or parsed % 4:
        raise argparse.ArgumentTypeError(
            "must be 40-200 inclusive and divisible by 4"
        )
    return parsed


def _trial_duration_days(value: str) -> int:
    parsed = _trial_positive_integer(value)
    if parsed > 14:
        raise argparse.ArgumentTypeError("must be between 1 and 14 days")
    return parsed


def _trial_sha256(value: str) -> str:
    if re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise argparse.ArgumentTypeError(
            "must be exactly 64 lowercase hexadecimal characters"
        )
    return value


_WORKFLOW_STATUS_FIELDS = (
    "schema",
    "plan_id",
    "project_id",
    "conversation_id",
    "task_id",
    "status",
    "manifest_sha256",
    "stage_count",
    "next_stage_ordinal",
    "completed_stages",
    "budget",
    "usage",
    "remaining",
    "current_claim",
    "checkpoint_head_sha256",
    "mutation_state",
    "final_verification",
    "quarantine_reason",
)
_WORKFLOW_NESTED_FIELDS = frozenset({
    "attempt_count",
    "claim_sha256",
    "completion_tokens",
    "elapsed_seconds",
    "elapsed_ms",
    "expires_at",
    "intent_sha256",
    "lease_expires_at",
    "model_calls",
    "ordinal",
    "outcome_sha256",
    "prompt_tokens",
    "receipt_sha256",
    "reconciled",
    "result_sha256",
    "retries",
    "stage_id",
    "stage_key",
    "stage_ordinal",
    "status",
    "time_ms",
    "tool_calls",
    "verified",
    "verified_at",
    "verification_sha256",
    "verifier_kind",
    "passed",
})
_WORKFLOW_CODE = re.compile(r"[a-z0-9][a-z0-9_.:-]{0,99}\Z")
_WORKFLOW_TIMESTAMP = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:.+-]{8,35}Z?\Z"
)


def _workflow_limit(value: str) -> int:
    parsed = _trial_positive_integer(value)
    if parsed > 200:
        raise argparse.ArgumentTypeError("must be between 1 and 200")
    return parsed


def _workflow_store(memory: Any, project_id: int) -> Any:
    """Load the Phase 5 store without making ordinary CLI startup depend on it."""

    try:
        from .long_horizon import LongHorizonStore
    except (ImportError, AttributeError) as exc:
        raise RuntimeError(
            "Phase 5 workflow storage is unavailable; no workflow state was changed"
        ) from exc
    return LongHorizonStore(memory, project_id=int(project_id))


def _workflow_manifest_from_path(path_value: Path) -> Any:
    """Read one bounded closed manifest without accepting executable content."""

    path = Path(path_value)
    if path.is_symlink() or not path.is_file():
        raise ValueError("Workflow manifest must be one regular non-symlink file")
    try:
        with path.open("rb") as handle:
            raw = handle.read(128 * 1024 + 1)
    except OSError as exc:
        raise ValueError("Workflow manifest could not be read") from exc
    if len(raw) > 128 * 1024:
        raise ValueError("Workflow manifest exceeds the 128 KiB limit")
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ValueError("Workflow manifest must be UTF-8 JSON") from exc

    def reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Workflow manifest contains a duplicate field")
            result[key] = value
        return result

    def reject_constant(_value: str) -> None:
        raise ValueError("Workflow manifest contains a non-finite number")

    try:
        payload = json.loads(
            text,
            object_pairs_hook=reject_duplicate_pairs,
            parse_constant=reject_constant,
        )
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("Workflow manifest is not valid closed JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("Workflow manifest must be one JSON object")

    manifest_fields = {
        "schema", "project_id", "conversation_id", "task_id", "goal_sha256",
        "contract_sha256", "constraints_sha256", "approval_scope_sha256",
        "artifact_set_sha256", "budget", "stages",
    }
    budget_fields = {
        "elapsed_seconds", "tool_calls", "model_calls", "prompt_tokens",
        "completion_tokens", "retries",
    }
    stage_fields = {"stage_id", "ordinal", "stage_type", "mutation_kind", "budget"}
    if set(payload) != manifest_fields:
        raise ValueError("Workflow manifest fields do not match the closed schema")
    for key in ("project_id", "conversation_id", "task_id"):
        value = payload[key]
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"Workflow manifest {key} must be a positive integer")
    for key in (
        "goal_sha256", "contract_sha256", "constraints_sha256",
        "approval_scope_sha256", "artifact_set_sha256",
    ):
        if not isinstance(payload[key], str) or re.fullmatch(
            r"[0-9a-f]{64}", payload[key]
        ) is None:
            raise ValueError(f"Workflow manifest {key} must be one SHA-256 digest")
    budget = payload["budget"]
    if not isinstance(budget, dict) or set(budget) != budget_fields:
        raise ValueError("Workflow manifest budget fields do not match the closed schema")
    for value in budget.values():
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("Workflow manifest budgets must be integers")
    stages = payload["stages"]
    if not isinstance(stages, list):
        raise ValueError("Workflow manifest stages must be an array")
    for stage in stages:
        if not isinstance(stage, dict) or set(stage) != stage_fields:
            raise ValueError("Workflow stage fields do not match the closed schema")
        if (
            not isinstance(stage["stage_id"], str)
            or isinstance(stage["ordinal"], bool)
            or not isinstance(stage["ordinal"], int)
            or not isinstance(stage["stage_type"], str)
            or not isinstance(stage["mutation_kind"], str)
        ):
            raise ValueError("Workflow stage metadata has invalid types")
        stage_budget = stage["budget"]
        if not isinstance(stage_budget, dict) or set(stage_budget) != budget_fields:
            raise ValueError("Workflow stage budget fields do not match the closed schema")
        if any(isinstance(value, bool) or not isinstance(value, int) for value in stage_budget.values()):
            raise ValueError("Workflow stage budgets must be integers")
    try:
        from .long_horizon import WorkflowManifest

        return WorkflowManifest.from_value(payload)
    except (ImportError, AttributeError) as exc:
        raise RuntimeError(
            "Phase 5 manifest validation is unavailable; no workflow state was changed"
        ) from exc


def _workflow_safe_nested(value: Any) -> Any:
    """Keep only prompt-free receipt fields from nested workflow status."""

    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return value if 0 <= value <= 9_223_372_036_854_775_807 else None
    if isinstance(value, str):
        text = value.strip().casefold()
        if re.fullmatch(r"[0-9a-f]{64}", text):
            return text
        if _WORKFLOW_CODE.fullmatch(text) or _WORKFLOW_TIMESTAMP.fullmatch(value.strip()):
            return value.strip()
        return None
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, child in value.items():
            name = str(key)
            if name not in _WORKFLOW_NESTED_FIELDS:
                continue
            safe = _workflow_safe_nested(child)
            if safe is not None:
                result[name] = safe
        return result
    return None


def _workflow_status_row(value: Any, *, project_id: int) -> dict[str, Any]:
    """Validate project binding and whitelist one prompt-free workflow receipt."""

    if not isinstance(value, dict):
        raise RuntimeError("Workflow storage returned a malformed status receipt")
    row_project = value.get("project_id")
    if (
        isinstance(row_project, bool)
        or not isinstance(row_project, int)
        or row_project != int(project_id)
    ):
        # Do not reveal whether a plan ID belongs to another project.
        raise ValueError("Workflow was not found in the selected project")
    plan_id = value.get("plan_id", value.get("id"))
    if isinstance(plan_id, bool) or not isinstance(plan_id, int) or plan_id < 1:
        raise RuntimeError("Workflow storage returned an invalid plan receipt")
    status = str(value.get("status") or "").strip().casefold()
    if _WORKFLOW_CODE.fullmatch(status) is None:
        raise RuntimeError("Workflow storage returned an invalid status code")

    result: dict[str, Any] = {
        "plan_id": plan_id,
        "project_id": row_project,
        "status": status,
    }
    for key in _WORKFLOW_STATUS_FIELDS:
        if key in result or key not in value:
            continue
        child = value[key]
        if key in {"conversation_id", "task_id", "stage_count", "next_stage_ordinal", "completed_stages"}:
            if child is None:
                result[key] = None
            elif isinstance(child, int) and not isinstance(child, bool) and child >= 0:
                result[key] = child
            else:
                raise RuntimeError(f"Workflow storage returned invalid {key}")
        elif key in {"manifest_sha256", "checkpoint_head_sha256"}:
            if child is None:
                result[key] = None
            else:
                digest = str(child).strip().casefold()
                if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
                    raise RuntimeError(f"Workflow storage returned invalid {key}")
                result[key] = digest
        elif key in {"schema", "quarantine_reason"}:
            code = str(child or "").strip().casefold()
            if code and _WORKFLOW_CODE.fullmatch(code):
                result[key] = code
        elif key == "mutation_state":
            if not isinstance(child, dict):
                raise RuntimeError("Workflow storage returned invalid mutation state")
            mutation_state: dict[str, str] = {}
            for stage_key, state in child.items():
                safe_stage = str(stage_key).strip().casefold()
                safe_state = str(state).strip().casefold()
                if (
                    _WORKFLOW_CODE.fullmatch(safe_stage) is None
                    or _WORKFLOW_CODE.fullmatch(safe_state) is None
                ):
                    raise RuntimeError("Workflow storage returned invalid mutation state")
                mutation_state[safe_stage] = safe_state
            result[key] = mutation_state
        else:
            safe = _workflow_safe_nested(child)
            if safe is not None:
                result[key] = safe
    return result


def _workflow_plan_for_project(store: Any, plan_id: int, project_id: int) -> dict[str, Any]:
    row = store.show_plan(int(plan_id))
    if row is None:
        raise ValueError("Workflow was not found in the selected project")
    return _workflow_status_row(row, project_id=int(project_id))


def _workflow_reason_sha256(action: str) -> str:
    """Use a fixed audit reason without accepting or persisting operator prose."""

    return hashlib.sha256(f"jarvis.workflow.operator-{action}.v1".encode("ascii")).hexdigest()


def _workflow_require_project(
    memory: Any,
    project_id: int,
    *,
    require_enabled: bool = False,
) -> dict[str, Any]:
    getter = getattr(memory, "get_project", None)
    if not callable(getter):
        raise RuntimeError("Project storage is unavailable; no workflow state was changed")
    project = getter(int(project_id))
    if not isinstance(project, dict) or int(project.get("id") or 0) != int(project_id):
        raise ValueError(f"Project #{int(project_id)} does not exist")
    if require_enabled and not bool(project.get("enabled")):
        raise ValueError(f"Project #{int(project_id)} is disabled")
    return project


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

    def __enter__(self) -> _ForegroundLease:
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
                    control_state = _runtime_state(memory)
                    if control_state == "stopped":
                        print("Emergency stop is active; worker exiting.")
                        return 0
                    if control_state == "paused":
                        _wait(poll_seconds, stop_event, sleep)
                        continue

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


                    # Materializers enforce the same control state inside their
                    # transactions. Re-check before claiming to honor a control
                    # change that raced the scheduler calls.
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
                        # The learning ladder's consolidation pass
                        # (correctness review HIGH-2), here and not earlier.
                        #
                        # It runs only once the claim above has already
                        # returned nothing, for two reasons.  A maintenance
                        # pass must never compete with foreground work for the
                        # write lock; and nothing it does can then precede or
                        # prevent a claim.  Placed before the claim, an
                        # AttributeError from a store without the ladder
                        # surface reached the worker's `except Exception` and
                        # abandoned the whole iteration -- the task was never
                        # claimed, and sixteen unrelated tests failed on
                        # `IndexError` with no visible link to the ladder.
                        #
                        # The handler is deliberately broader than the pass's
                        # own capability check: whatever goes wrong in a
                        # background pass, the worker's job is the task loop.
                        try:
                            _report_ladder_pass(
                                run_ladder_consolidation(config, memory)
                            )
                        except Exception as exc:  # noqa: BLE001 - never fatal
                            _record_ladder_pass_failure(memory, exc)
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

    def __enter__(self) -> _RotatingTextWriter:
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
        print(
            f"Strategy transfer: configured={config.strategy_transfer}; "
            "trial/promotion status is available via `jarvis strategy-transfer status`"
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
    print(f"{'provider/model':42}{'profile':11}{'calls':>7}{'call ok':>9}{'input':>11}{'output':>11}{'avg ms':>9}{'p95 ms':>9}")
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
    print(
        "Call ok is transport-level model-call success. Failed attempts that later "
        "recover through retry or failover remain counted as failed calls."
    )
    return 0


def _memory_preview(content: Any, limit: int = 120) -> str:
    """A listing preview with secrets redacted and identifiers screened.

    The widened screen of design 6.2 decides; a row that trips it prints
    ``[PRIVATE]`` rather than its text, so a listing can never be the leak.
    """
    text = redact_private_identifiers(_safe_summary(content, limit))
    if contains_private_identifier_extended(text):
        return "[PRIVATE]"
    return text


def _run_memory_list(args: argparse.Namespace) -> int:
    """``memory list [--limit N] [--json]``: ids, provenance, a screened preview."""
    config = Config.load()
    with Memory(config.data_dir / "jarvis.db") as memory:
        try:
            records = memory.list_memories(limit=int(args.limit), with_ids=True)
        except TypeError:
            records = memory.list_memories(limit=int(args.limit))
    if bool(getattr(args, "json", False)):
        payload = [
            {
                "id": record.get("id"),
                "kind": record.get("kind"),
                "created_at": record.get("created_at"),
                "origin": record.get("origin"),
                "eligible": record.get("eligible"),
                "preview": _memory_preview(record.get("content", "")),
            }
            for record in records
        ]
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        return 0
    if not records:
        print("No saved memories.")
        return 0
    print("Saved memories (newest first):")
    for record in records:
        identifier = record.get("id")
        marker = f"#{int(identifier)}" if isinstance(identifier, int) else "#?"
        print(
            f"  {marker}  {_safe_summary(record.get('kind', 'memory'), 20)}  "
            f"{_safe_summary(record.get('created_at', ''), 19)}  "
            f"{_safe_summary(record.get('origin', '-'), 30)}  "
            f"{'eligible' if record.get('eligible') else 'ineligible'}  "
            f"{_memory_preview(record.get('content', ''))}"
        )
    print("Erase one with: python -m jarvis memory erase <id> --yes")
    return 0


def _run_memory_erase(args: argparse.Namespace) -> int:
    """``memory erase <id> [--yes]``.

    Exit 2 without ``--yes``: the kind and created date are printed and
    nothing is read or written beyond that lookup.  Exit 0 on an erase, 1 on
    a refusal (a missing id, a claim backing row, or a vault-note mirror),
    which changes nothing.  No content is echoed on any path.
    """
    config = Config.load()
    identifier = int(args.memory_id)
    json_output = bool(getattr(args, "json", False))
    with Memory(config.data_dir / "jarvis.db") as memory:
        if not bool(getattr(args, "yes", False)):
            # By primary key, never through list_memories: that is a listing,
            # it hides claim backing rows and stops at its limit, so it says
            # "no such memory" about exactly the rows the erase has fixed
            # refusals for.
            described = memory.describe_memory(identifier)
            if described is None:
                reason, message = "missing", (
                    f"No memory #{identifier} exists; nothing changed."
                )
            elif described.get("is_claim_backing"):
                reason, message = "claim_backing", (
                    f"Memory #{identifier} backs a project fact; use "
                    'Erase this project fact: {...} (see /facts) instead.'
                )
            elif described.get("is_vault_note"):
                reason, message = "vault_note", (
                    f"Memory #{identifier} mirrors a vault note; delete the "
                    "note in the vault and reindex."
                )
            else:
                reason, message = "", ""
            if reason:
                # A refusal is settled: --yes would not change it.
                if json_output:
                    print(json.dumps({"erased": False, "reason": reason}))
                else:
                    print(message)
                return 1
            if json_output:
                print(json.dumps({
                    "erased": False,
                    "reason": "confirmation required",
                    "id": identifier,
                    "kind": described.get("kind"),
                    "created_at": described.get("created_at"),
                    "origin": described.get("origin"),
                    "eligible": described.get("eligible"),
                    "content_length": described.get("content_length"),
                }, ensure_ascii=False, default=str))
            else:
                origin = described.get("origin")
                print(
                    f"Would erase memory #{identifier} "
                    f"(kind: {_safe_summary(described.get('kind', 'memory'), 20)}, "
                    f"created {_safe_summary(described.get('created_at', ''), 19)}, "
                    f"{int(described.get('content_length') or 0)} characters"
                    # A legacy import has no provenance row at all.
                    + (f", origin {_safe_summary(origin, 30)}" if origin else "")
                    + ")."
                )
                print("Re-run with --yes to erase it; nothing changed.")
            return 2
        receipt = memory.erase_memory(
            None, identifier, permission="operator:cli"
        )
    if json_output:
        print(json.dumps(receipt, ensure_ascii=False, indent=2, default=str))
    else:
        # The store owns the fixed receipt text; the CLI never re-renders it.
        print(_safe_detail(str(receipt.get("assistant_message", "")), 2_000))
    return 0 if str(receipt.get("action")) == "erased" else 1


def _run_memory(args: argparse.Namespace) -> int:
    """``memory [status|list|erase]``; ``status`` stays the default."""
    command = str(getattr(args, "memory_command", "status") or "status")
    if command == "list":
        return _run_memory_list(args)
    if command == "erase":
        if getattr(args, "memory_id", None) is None:
            print("memory erase requires an id: python -m jarvis memory erase <id>")
            return 2
        return _run_memory_erase(args)
    return _run_memory_quality(args)


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
    if args.control_command == "status":
        with Memory(config.data_dir / "jarvis.db") as memory:
            control = memory.control_state()
        state = _safe_summary(control.get("state") or "unknown", 30)
        print(f"JARVIS background control: {state}.")
        if control.get("reason"):
            print(f"Reason: {_safe_summary(control['reason'], 500)}")
        if control.get("updated_at"):
            print(f"Updated: {_safe_summary(control['updated_at'], 100)}")
        return 0
    state = {"pause": "paused", "resume": "running", "stop": "stopped"}[args.control_command]
    with Memory(config.data_dir / "jarvis.db") as memory:
        memory.set_control_state(state, getattr(args, "reason", None))
    print({"paused": "Background autonomy paused.", "running": "JARVIS resumed.", "stopped": "Emergency stop activated."}[state])
    return 0


def _strategy_trial_method(memory: Any, name: str) -> Callable[..., Any]:
    method = getattr(memory, name, None)
    if not callable(method):
        raise RuntimeError(
            "Phase 4B trial storage is unavailable; no trial state was changed"
        )
    return method


def _run_strategy_transfer(args: argparse.Namespace) -> int:
    """Operate the bounded Phase 4B trial without accepting task prose."""
    config = Config.load()
    command = args.strategy_transfer_command
    with Memory(config.data_dir / "jarvis.db") as memory:
        if command == "status":
            status = _strategy_trial_method(
                memory, "strategy_transfer_trial_status"
            )(args.manifest_id)
            rows = sanitized_trial_status(
                status, allowed_families=PREDICTION_FAMILY_CHOICES
            )
            if any(row.get("available") is False for row in rows):
                raise RuntimeError(
                    "Phase 4B trial status is unavailable or failed integrity checks"
                )
            if args.json:
                print(json.dumps({
                    "configured_mode": config.strategy_transfer,
                    "activation_requires_explicit_promotion": True,
                    "trials": rows,
                }, ensure_ascii=False, indent=2))
                return 0
            print(f"Strategy transfer mode: {config.strategy_transfer}")
            print(
                "Advice activation: requires a valid pinned causal attestation "
                "and explicit operator promotion."
            )
            if not rows:
                print("No Phase 4B trial manifests.")
                return 0
            for row in rows:
                print(trial_status_line(row))
            return 0

        if command == "start":
            if config.strategy_transfer != "trial":
                raise ValueError(
                    "Set JARVIS_STRATEGY_TRANSFER=trial before explicitly "
                    "starting a Phase 4B trial"
                )
            pins = _strategy_trial_method(
                memory, "strategy_transfer_trial_pins"
            )()
            required_pins = {
                "evaluator_version",
                "evaluator_sha256",
                "fixture_sha256",
                "config_sha256",
                "runtime_sha256",
            }
            if not isinstance(pins, dict) or set(pins) != required_pins:
                raise RuntimeError(
                    "Installed Phase 4B benchmark pins are unavailable or malformed"
                )
            manifest = build_trial_manifest_input(
                project_id=args.project,
                target_families=args.family,
                allowed_families=PREDICTION_FAMILY_CHOICES,
                strategies=args.strategy,
                sample_cap=args.sample_cap,
                duration_days=args.duration_days,
                seed=args.seed or secrets.token_hex(32),
                evaluator_version=pins["evaluator_version"],
                evaluator_sha256=pins["evaluator_sha256"],
                fixture_sha256=pins["fixture_sha256"],
                config_sha256=pins["config_sha256"],
                runtime_sha256=pins["runtime_sha256"],
            )
            created = _strategy_trial_method(
                memory, "create_strategy_transfer_trial_manifest"
            )(**manifest)
            if not isinstance(created, dict):
                raise RuntimeError("Trial storage did not return a manifest receipt")
            manifest_id = created.get("manifest_id", created.get("id"))
            if (
                isinstance(manifest_id, bool)
                or not isinstance(manifest_id, int)
                or manifest_id < 1
            ):
                raise RuntimeError("Trial storage returned an invalid manifest receipt")
            print(
                f"Started bounded Phase 4B trial #{manifest_id}. "
                "Assignments remain project-scoped and pre-outcome."
            )
            return 0

        if command == "abort":
            aborted = _strategy_trial_method(
                memory, "abort_strategy_transfer_trial"
            )(args.manifest_id, reason_code="operator_abort")
            if aborted is False:
                print(
                    f"Phase 4B trial #{args.manifest_id} was already aborted. "
                    "It cannot issue new assignments."
                )
                return 0
            if aborted is not True:
                raise RuntimeError("Trial storage returned an invalid abort receipt")
            print(
                f"Aborted Phase 4B trial #{args.manifest_id}. "
                "It cannot issue new assignments."
            )
            return 0

        if command == "promote":
            if config.strategy_transfer != "advise":
                raise ValueError(
                    "Set JARVIS_STRATEGY_TRANSFER=advise before explicitly "
                    "promoting a completed Phase 4B trial"
                )
            current = _strategy_trial_method(
                memory, "strategy_transfer_trial_status"
            )(args.manifest_id)
            current_rows = sanitized_trial_status(
                current, allowed_families=PREDICTION_FAMILY_CHOICES
            )
            already_promoted = bool(
                len(current_rows) == 1
                and current_rows[0].get("status") == "promoted"
                and current_rows[0].get("causal_attestation_valid") is True
            )
            if len(current_rows) != 1 or not (
                current_rows[0].get("promotion_ready") is True
                or already_promoted
            ):
                raise ValueError(
                    "The trial is not ready for promotion: completed balanced "
                    "pre-outcome assignments and every safety gate are required"
                )
            promoted = _strategy_trial_method(
                memory, "promote_strategy_transfer_trial"
            )(args.manifest_id, operator_confirmed=True)
            if not isinstance(promoted, dict):
                raise RuntimeError("Trial storage did not return a promotion receipt")
            promotion_rows = sanitized_trial_status(
                promoted, allowed_families=PREDICTION_FAMILY_CHOICES
            )
            if len(promotion_rows) != 1 or (
                not isinstance(promotion_rows[0].get("promoted"), bool)
                or promotion_rows[0].get("status") != "promoted"
                or "attestation_sha256" not in promotion_rows[0]
            ):
                raise RuntimeError(
                    "Trial storage did not confirm a pinned causal promotion"
                )
            if promotion_rows[0]["promoted"] is False:
                print(
                    f"Phase 4B trial #{args.manifest_id} was already promoted "
                    "with a valid pinned causal attestation."
                )
                return 0
            print(
                f"Explicitly promoted Phase 4B trial #{args.manifest_id}. "
                "Runtime advice remains bound to its causal attestation, pins, "
                "project scope, drift checks, quarantine, and ledger health."
            )
            return 0
    raise StrategyTransferOperatorError("unsupported strategy-transfer command")


def _workflow_json(command: str, project_id: int, **payload: Any) -> None:
    print(json.dumps({
        "schema": f"jarvis.workflow-cli-{command}.v1",
        "project_id": int(project_id),
        **payload,
    }, ensure_ascii=False, indent=2, sort_keys=True))


def _run_workflow(args: argparse.Namespace) -> int:
    """Inspect and control durable workflows without invoking a model."""

    config = Config.load()
    command = args.workflow_command
    project_id = int(args.project)
    with Memory(config.data_dir / "jarvis.db") as memory:
        _workflow_require_project(
            memory,
            project_id,
            require_enabled=command in {"start", "resume"},
        )
        store = _workflow_store(memory, project_id)

        if command in {"status", "list"}:
            raw_rows = store.list_plans(
                project_id=project_id,
                limit=int(getattr(args, "limit", 50)),
            )
            if not isinstance(raw_rows, list):
                raise RuntimeError("Workflow storage returned a malformed plan list")
            rows = [
                _workflow_status_row(row, project_id=project_id)
                for row in raw_rows
            ]
            if command == "status":
                counts: dict[str, int] = {}
                for row in rows:
                    state = str(row["status"])
                    counts[state] = counts.get(state, 0) + 1
                payload = {
                    "counts": dict(sorted(counts.items())),
                    "returned": len(rows),
                    "limit": int(getattr(args, "limit", 50)),
                }
                if args.json:
                    _workflow_json("status", project_id, **payload)
                else:
                    print(f"Project #{project_id} workflows: {len(rows)} returned.")
                    if counts:
                        print("States: " + ", ".join(
                            f"{name}={count}" for name, count in sorted(counts.items())
                        ))
                    else:
                        print("No workflows in this project.")
                return 0

            if args.json:
                _workflow_json("list", project_id, plans=rows)
            elif not rows:
                print(f"No workflows in project #{project_id}.")
            else:
                for row in rows:
                    complete = row.get("completed_stages")
                    total = row.get("stage_count")
                    progress = (
                        f" stages={complete}/{total}"
                        if isinstance(complete, int) and isinstance(total, int)
                        else ""
                    )
                    print(f"#{row['plan_id']} {row['status']}{progress}")
            return 0

        if command == "show":
            row = _workflow_plan_for_project(
                store, int(args.plan_id), project_id
            )
            if args.json:
                _workflow_json("show", project_id, plan=row)
            else:
                print(f"Workflow #{row['plan_id']}: {row['status']}")
                if "completed_stages" in row and "stage_count" in row:
                    print(
                        f"Stages: {row['completed_stages']}/{row['stage_count']} "
                        f"(next={row.get('next_stage_ordinal')})"
                    )
                if row.get("manifest_sha256"):
                    print(f"Manifest: {row['manifest_sha256']}")
                if row.get("checkpoint_head_sha256"):
                    print(f"Checkpoint head: {row['checkpoint_head_sha256']}")
                if row.get("quarantine_reason"):
                    print(f"Quarantine: {row['quarantine_reason']}")
                if "budget" in row:
                    print("Budget: " + json.dumps(row["budget"], sort_keys=True))
                if "usage" in row:
                    print("Usage: " + json.dumps(row["usage"], sort_keys=True))
                if "remaining" in row:
                    print("Remaining: " + json.dumps(row["remaining"], sort_keys=True))
                if "final_verification" in row:
                    print(
                        "Final verification: "
                        + json.dumps(row["final_verification"], sort_keys=True)
                    )
            return 0

        if command in {"pause", "resume", "cancel"}:
            plan_id = int(args.plan_id)
            _workflow_plan_for_project(store, plan_id, project_id)
            if command == "pause":
                changed = store.pause_plan(
                    plan_id, _workflow_reason_sha256("pause")
                )
            elif command == "resume":
                changed = store.resume_plan(plan_id)
            else:
                changed = store.cancel_plan(
                    plan_id, _workflow_reason_sha256("cancel")
                )
            if changed is None or changed is False:
                raise RuntimeError(
                    f"Workflow #{plan_id} did not accept the operator {command} request"
                )
            row = _workflow_plan_for_project(store, plan_id, project_id)
            if command == "pause" and row["status"] != "paused":
                raise RuntimeError("Workflow storage did not confirm the pause")
            if command == "cancel" and row["status"] != "cancelled":
                raise RuntimeError("Workflow storage did not confirm cancellation")
            if command == "resume" and row["status"] in {"paused", "cancelled"}:
                raise RuntimeError("Workflow storage did not confirm the resume")
            if args.json:
                _workflow_json(command, project_id, plan=row)
            else:
                verb = {
                    "pause": "Paused",
                    "resume": "Resumed",
                    "cancel": "Cancelled",
                }[command]
                print(f"{verb} workflow #{plan_id} in project #{project_id}.")
            return 0

        if command == "start":
            manifest = _workflow_manifest_from_path(args.manifest)
            if int(manifest.project_id) != project_id:
                raise ValueError(
                    "Workflow manifest is not bound to the selected project"
                )
            plan_id = store.create_plan(manifest)
            if isinstance(plan_id, bool) or not isinstance(plan_id, int) or plan_id < 1:
                raise RuntimeError(
                    "Workflow storage did not return a valid durable plan receipt"
                )
            row = _workflow_plan_for_project(store, plan_id, project_id)
            if row.get("manifest_sha256") is None:
                raise RuntimeError(
                    "Workflow storage did not return a bound manifest receipt"
                )
            if args.json:
                _workflow_json("start", project_id, plan=row)
            else:
                print(
                    f"Registered workflow plan #{plan_id} in project #{project_id}. "
                    "This command only registered durable coordination state. "
                    "No shipped component advances stages or performs tool, model, "
                    "or external work. Future adapters require separate review and "
                    "integration with policy, approval, measured-usage, "
                    "reconciliation, and verifier boundaries."
                )
            return 0
    raise ValueError("Unknown workflow command")


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


# The divergence kinds `--apply` refuses on (the spine history itself is
# inconsistent); a failed chain is counted from the verification report.
_SPINE_SIDE_DIVERGENCES = frozenset({"payload", "order", "redaction"})


def _print_claim_rebuild_report(report: dict[str, Any], *, heading: str) -> None:
    """Dry-run report lines: counts, then one line per divergence.  The
    store's ``detail`` names fields and digests, never a value, a subject, a
    predicate, or a source text; the CLI still bounds it with
    ``_safe_summary``."""
    state = "equivalent" if report.get("ok") else "DIVERGENT"
    print(
        f"{heading}: {state}; live rows {int(report.get('rows_live', 0))}, "
        f"rebuilt rows {int(report.get('rows_rebuilt', 0))}."
    )
    for item in list(report.get("divergences") or [])[:50]:
        print(
            f"  claim {item.get('claim_id')}: {item.get('kind')}: "
            f"{_safe_summary(item.get('detail'), 160)}"
        )


def _claim_rebuild_plan(report: dict[str, Any]) -> dict[str, int]:
    """What ``--apply`` would do with a dry-run report, by divergence kind."""
    plan = {"updates": 0, "recreations": 0, "removals": 0, "spine_side": 0}
    verification = report.get("verification")
    if isinstance(verification, dict) and not bool(
        verification.get("chain_ok", verification.get("ok", True))
    ):
        # The chain, head, key, triggers, redactions, or sequences fail:
        # the spine is wrong, and apply refuses with verify_failed.
        plan["spine_side"] += 1
    updated: set[Any] = set()
    for item in list(report.get("divergences") or []):
        kind = str(item.get("kind") or "")
        if kind in _SPINE_SIDE_DIVERGENCES:
            plan["spine_side"] += 1
        elif kind == "verify":
            # A verification problem is either the chain (counted above) or
            # a lineage problem, which apply repairs through the row-level
            # kinds below; it is never a change of its own.
            continue
        elif kind == "missing_in_live":
            plan["recreations"] += 1
        elif kind == "missing_in_rebuild":
            plan["removals"] += 1
        else:
            claim_id = item.get("claim_id")
            if claim_id not in updated:
                updated.add(claim_id)
                plan["updates"] += 1
    return plan


def _plan_token_of(report: dict[str, Any]) -> str:
    """The store's plan token (12 hex characters over the head event id and
    the sorted (claim id, kind) divergences); empty when the store has none."""
    token = report.get("plan_token")
    return str(token).strip() if isinstance(token, str) and token.strip() else ""


def _print_claim_rebuild_plan(report: dict[str, Any], plan: dict[str, int]) -> None:
    """The plan an operator confirms: the dry-run lines, what apply would do,
    and the token that binds a later ``--apply --yes --plan`` to exactly it."""
    _print_claim_rebuild_report(report, heading="Claim projection rebuild (plan)")
    line = (
        f"Would change: {plan['updates']} field updates, "
        f"{plan['recreations']} recreations, {plan['removals']} removals"
    )
    if plan["spine_side"]:
        line += (
            f"; {plan['spine_side']} spine-side divergences would make "
            "apply refuse"
        )
    print(line + ".")
    token = _plan_token_of(report)
    if token:
        print(f"plan token: {token}")


def _id_list(values: Any) -> list[int]:
    return [
        int(value)
        for value in (values or [])
        if isinstance(value, int) and not isinstance(value, bool)
    ]


def _run_spine_rebuild_claims(memory: Memory, args: argparse.Namespace) -> int:
    """``spine rebuild-claims [--apply [--yes [--plan TOKEN]]] [--json]``.

    Exit 0: equivalent, applied, or nothing to apply.  Exit 1: divergent dry
    run, or ``--apply --yes`` refused (the spine failed verification, a
    spine-side divergence, a divergence remained after the in-transaction
    re-check, or ``--plan TOKEN`` no longer matches the store: ``stale_plan``)
    with nothing changed.  Exit 2: ``--apply`` without ``--yes`` when
    something would change (the plan and its token are printed), ``--yes``
    without ``--apply``, or ``--plan`` without ``--apply --yes``.

    The plan token binds the apply to the plan the operator saw: with a
    token the dry run is re-run and must produce the same token; without one
    the fresh plan is printed and applied.  Either way the dry-run report is
    handed to the store as ``plan=`` so its in-transaction check is real.
    """
    json_output = bool(getattr(args, "json", False))
    apply = bool(getattr(args, "apply", False))
    yes = bool(getattr(args, "yes", False))
    requested_token = str(getattr(args, "plan", None) or "").strip()
    if yes and not apply:
        if json_output:
            print(json.dumps({"ok": False, "applied": False, "error": "--yes requires --apply"}))
        else:
            print("--yes requires --apply; nothing changed.")
        return 2
    if requested_token and not (apply and yes):
        if json_output:
            print(json.dumps({"ok": False, "applied": False, "error": "--plan requires --apply --yes"}))
        else:
            print("--plan requires --apply --yes; nothing changed.")
        return 2
    report = memory.rebuild_claim_projection()
    if not apply:
        if json_output:
            print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
        else:
            _print_claim_rebuild_report(report, heading="Claim projection rebuild (dry run)")
        return 0 if report.get("ok") else 1
    if report.get("ok"):
        if json_output:
            print(json.dumps(
                {"applied": False, "reason": "nothing to apply", **report},
                ensure_ascii=False, indent=2, default=str,
            ))
        else:
            print("Nothing to apply: the live claim projection is equivalent to the spine.")
        return 0
    plan = _claim_rebuild_plan(report)
    fresh_token = _plan_token_of(report)
    if not yes:
        if json_output:
            print(json.dumps(
                {"applied": False, "would_change": plan, **report},
                ensure_ascii=False, indent=2, default=str,
            ))
        else:
            _print_claim_rebuild_plan(report, plan)
            hint = f" --plan {fresh_token}" if fresh_token else ""
            print(f"Re-run with --apply --yes{hint} to reconcile exactly this plan.")
        return 2
    if requested_token:
        if not fresh_token or requested_token != fresh_token:
            # The store changed since the plan was printed, or the token is
            # not this store's: refuse before touching anything.
            if json_output:
                print(json.dumps({
                    "ok": False, "applied": False, "refusal": "stale_plan",
                    "requested_plan_token": requested_token,
                    "plan_token": fresh_token or None,
                }))
            else:
                current = fresh_token or "unavailable"
                print(
                    "Claim projection rebuild refused: stale_plan; the store no "
                    "longer matches the plan you confirmed (current plan token: "
                    f"{current}); nothing changed."
                )
            return 1
    elif not json_output:
        # No token given: the plan being applied is shown first, token
        # included, so the operator sees exactly what changes.
        _print_claim_rebuild_plan(report, plan)
    try:
        applied = memory.rebuild_claim_projection(apply=True, plan=report)
    except SpineError as exc:
        reason = str(getattr(exc, "code", None) or _safe_summary(exc, 120))
        if json_output:
            print(json.dumps({"ok": False, "applied": False, "refusal": reason}))
        else:
            print(f"Claim projection rebuild refused: {reason}; nothing changed.")
        return 1
    refusal = applied.get("refusal")
    if refusal or not applied.get("ok"):
        reason = _safe_summary(refusal or "divergences remain", 120)
        if json_output:
            print(json.dumps(applied, ensure_ascii=False, indent=2, default=str))
        else:
            print(f"Claim projection rebuild refused: {reason}; nothing changed.")
            for item in list(applied.get("divergences") or [])[:50]:
                print(
                    f"  claim {item.get('claim_id')}: {item.get('kind')}: "
                    f"{_safe_summary(item.get('detail'), 160)}"
                )
        return 1
    if json_output:
        print(json.dumps(applied, ensure_ascii=False, indent=2, default=str))
        return 0
    if not applied.get("applied", True):
        print("Nothing to apply: the live claim projection is equivalent to the spine.")
        return 0
    print(
        f"Claim projection rebuilt: rows before {int(applied.get('rows_before', 0))}, "
        f"rows after {int(applied.get('rows_after', 0))}, divergences fixed "
        f"{int(applied.get('divergences_fixed', 0))}."
    )
    for label, key in (
        ("removed", "removed_ids"),
        ("updated", "updated_ids"),
        ("recreated", "recreated_ids"),
        ("evidence lost for", "lost_evidence_claim_ids"),
    ):
        ids = _id_list(applied.get(key))
        if ids:
            print(f"  {label}: {', '.join(str(value) for value in ids[:200])}")
    event_id = applied.get("event_id")
    if isinstance(event_id, int) and not isinstance(event_id, bool):
        print(f"  receipt: projection.rebuilt event #{event_id}")
    return 0


def _graph_problem_lines(items: Any, limit: int = 50) -> None:
    """One line per graph problem: ids, kind, and a detail that names fields.

    ``verify_graph`` and the rebuild report name fields, never values (the
    M-1 / F-1 rule); the CLI bounds the text anyway.
    """
    for item in list(items or [])[:limit]:
        if not isinstance(item, dict):
            continue
        claim_id = item.get("claim_id")
        entity_id = item.get("entity_id")
        subject = (
            f"claim {claim_id}" if claim_id is not None else f"entity {entity_id}"
        )
        print(
            f"  {subject}: {item.get('kind')}: "
            f"{_safe_summary(item.get('detail'), 160)}"
        )


def _graph_excluded_line(excluded: Any) -> str:
    """The three exclusion categories of design 2.2, or a legacy integer."""
    if isinstance(excluded, dict):
        return (
            f"{int(excluded.get('excluded_predicate', 0) or 0)} reserved-predicate, "
            f"{int(excluded.get('subject_private', 0) or 0)} private-subject, "
            f"{int(excluded.get('subject_too_long', 0) or 0)} over-long-subject"
        )
    if isinstance(excluded, int) and not isinstance(excluded, bool):
        return f"{excluded} excluded"
    return "0 excluded"


def _last_graph_projection_event(memory: Memory) -> int | None:
    """The newest ``projection.rebuilt`` event id, or None.

    Payload values never leave the store, so this cannot say whether the
    newest one re-projected the claims or the graph; the caller labels it as
    the last projection receipt, not as the graph's own.
    """
    try:
        events = memory.spine_tail(limit=200)
    except (AttributeError, RuntimeError, sqlite3.Error, TypeError, ValueError):
        return None
    for item in events:
        if str(item.get("kind") or "") == "projection.rebuilt":
            identifier = item.get("id")
            if isinstance(identifier, int) and not isinstance(identifier, bool):
                return identifier
    return None


def _print_graph_report(report: dict[str, Any], *, heading: str) -> None:
    state = "equivalent" if report.get("ok") else "DIVERGENT"
    print(
        f"{heading}: {state}; live edges "
        f"{int(report.get('edges_live', report.get('edges', 0)) or 0)}, "
        f"expected edges "
        f"{int(report.get('edges_expected', report.get('edges', 0)) or 0)}; "
        f"live entities {int(report.get('entities_live', 0) or 0)}, "
        f"expected entities {int(report.get('entities_expected', 0) or 0)}."
    )
    _graph_problem_lines(report.get("divergences") or report.get("problems"))


def _graph_rebuild_plan(report: dict[str, Any]) -> dict[str, int]:
    """What ``--apply`` would do with a graph dry-run report, by kind."""
    plan = {"reprojections": 0, "removals": 0, "entity_sweeps": 0}
    for item in list(report.get("divergences") or report.get("problems") or []):
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or "")
        if kind in {"extra_edge"}:
            plan["removals"] += 1
        elif kind in {"orphan_entity"}:
            plan["entity_sweeps"] += 1
        else:
            plan["reprojections"] += 1
    return plan


def _print_graph_rebuild_plan(report: dict[str, Any], plan: dict[str, int]) -> None:
    _print_graph_report(report, heading="Graph projection rebuild (plan)")
    print(
        f"Would change: {plan['reprojections']} re-projections, "
        f"{plan['removals']} edge removals, {plan['entity_sweeps']} orphan "
        "entity sweeps."
    )
    token = _plan_token_of(report)
    if token:
        print(f"plan token: {token}")


def _run_graph_rebuild(memory: Memory, args: argparse.Namespace) -> int:
    """``graph rebuild [--apply [--yes [--plan TOKEN]]] [--json]``.

    The exit codes of ``spine rebuild-claims``: 0 equivalent, applied, or
    nothing to apply; 1 a divergent dry run or a refused apply (including a
    ``stale_plan``) with nothing changed; 2 ``--apply`` without ``--yes`` when
    something would change, ``--yes`` without ``--apply``, or ``--plan``
    without ``--apply --yes``.
    """
    json_output = bool(getattr(args, "json", False))
    apply = bool(getattr(args, "apply", False))
    yes = bool(getattr(args, "yes", False))
    requested_token = str(getattr(args, "plan", None) or "").strip()
    if yes and not apply:
        if json_output:
            print(json.dumps({"ok": False, "applied": False, "error": "--yes requires --apply"}))
        else:
            print("--yes requires --apply; nothing changed.")
        return 2
    if requested_token and not (apply and yes):
        if json_output:
            print(json.dumps({"ok": False, "applied": False, "error": "--plan requires --apply --yes"}))
        else:
            print("--plan requires --apply --yes; nothing changed.")
        return 2
    report = memory.rebuild_graph_projection()
    if not apply:
        if json_output:
            print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
        else:
            _print_graph_report(report, heading="Graph projection rebuild (dry run)")
        return 0 if report.get("ok") else 1
    if report.get("ok"):
        if json_output:
            print(json.dumps(
                {"applied": False, "reason": "nothing to apply", **report},
                ensure_ascii=False, indent=2, default=str,
            ))
        else:
            print("Nothing to apply: the graph projection is equivalent to the claims.")
        return 0
    plan = _graph_rebuild_plan(report)
    fresh_token = _plan_token_of(report)
    if not yes:
        if json_output:
            print(json.dumps(
                {"applied": False, "would_change": plan, **report},
                ensure_ascii=False, indent=2, default=str,
            ))
        else:
            _print_graph_rebuild_plan(report, plan)
            hint = f" --plan {fresh_token}" if fresh_token else ""
            print(f"Re-run with --apply --yes{hint} to reconcile exactly this plan.")
        return 2
    if requested_token:
        if not fresh_token or requested_token != fresh_token:
            # The store changed since the plan was printed, or the token is
            # not this store's: refuse before touching anything.
            if json_output:
                print(json.dumps({
                    "ok": False, "applied": False, "refusal": "stale_plan",
                    "requested_plan_token": requested_token,
                    "plan_token": fresh_token or None,
                }))
            else:
                current = fresh_token or "unavailable"
                print(
                    "Graph projection rebuild refused: stale_plan; the store no "
                    "longer matches the plan you confirmed (current plan token: "
                    f"{current}); nothing changed."
                )
            return 1
    elif not json_output:
        _print_graph_rebuild_plan(report, plan)
    try:
        applied = memory.rebuild_graph_projection(
            apply=True, plan=report, actor="operator", permission="operator:cli"
        )
    except SpineError as exc:
        reason = str(getattr(exc, "code", None) or _safe_summary(exc, 120))
        if json_output:
            print(json.dumps({"ok": False, "applied": False, "refusal": reason}))
        else:
            print(f"Graph projection rebuild refused: {reason}; nothing changed.")
        return 1
    refusal = applied.get("refusal")
    if refusal or not applied.get("ok"):
        reason = _safe_summary(refusal or "divergences remain", 120)
        if json_output:
            print(json.dumps(applied, ensure_ascii=False, indent=2, default=str))
        else:
            print(f"Graph projection rebuild refused: {reason}; nothing changed.")
            _graph_problem_lines(
                applied.get("divergences") or applied.get("problems")
            )
        return 1
    if json_output:
        print(json.dumps(applied, ensure_ascii=False, indent=2, default=str))
        return 0
    if not applied.get("applied", True):
        print("Nothing to apply: the graph projection is equivalent to the claims.")
        return 0
    print(
        f"Graph projection rebuilt: edges before "
        f"{int(applied.get('edges_before', 0) or 0)}, edges after "
        f"{int(applied.get('edges_after', 0) or 0)}, divergences fixed "
        f"{int(applied.get('divergences_fixed', 0) or 0)}."
    )
    for label, key in (
        ("removed", "removed_ids"),
        ("updated", "updated_ids"),
        ("recreated", "recreated_ids"),
    ):
        ids = _id_list(applied.get(key))
        if ids:
            print(f"  {label}: {', '.join(str(value) for value in ids[:200])}")
    removed_entities = _id_list(applied.get("removed_entity_ids"))
    if removed_entities:
        print(
            "  entities swept: "
            f"{', '.join(str(value) for value in removed_entities[:200])}"
        )
    event_id = applied.get("event_id")
    if isinstance(event_id, int) and not isinstance(event_id, bool):
        print(f"  receipt: projection.rebuilt event #{event_id}")
    return 0


def _print_graph_chain_rows(rows: list[dict[str, Any]], hops: int) -> None:
    """The screened chains the agent would see, in hop order.

    Values are shown, exactly as ``facts`` shows them: this goes through
    ``Memory.graph_chains``, so every row already passed the eligibility check
    and both endpoint screens.  Raw tables are never read here.
    """
    for row in rows:
        hop = row.get("hop")
        if isinstance(hop, int) and not isinstance(hop, bool) and hop > hops:
            continue
        chain = row.get("chain")
        marker = f"chain {chain} hop {hop}" if chain is not None else "row"
        flags = []
        if row.get("incomplete"):
            flags.append("incomplete")
        if row.get("weakest"):
            flags.append("weakest")
        if row.get("retracted"):
            flags.append("retracted")
        suffix = f" [{', '.join(flags)}]" if flags else ""
        print(
            f"  {marker}: {_safe_summary(row.get('subject', ''), 60)} | "
            f"{_safe_summary(row.get('predicate', ''), 60)} | "
            f"{_safe_summary(row.get('value', ''), 160)} "
            f"[{_safe_summary(row.get('status', ''), 12)}]{suffix}"
        )
        note = row.get("note")
        if note:
            print(f"      note: {_safe_summary(note, 200)}")


def _print_unresolved_names(names: list[str]) -> None:
    """Names the walk could not identify (design 10.7 item 4).

    A chain that answers for one name while silently ignoring another reads as
    a complete answer, so the names that resolved to nothing are printed
    whether or not anything was found.  They are already screened store side.
    """
    if not names:
        return
    print(f"  no stored fact identifies: {', '.join(names)}")


# ---------------------------------------------------------------------------
# The learning ladder (VTMF M4 design 6.3): ten subcommands over the
# calibration ledger and the promotion record.
#
# `ladder list`, `ladder show` and the chat surface `/ladder` are the ONLY
# three places the confirmation code of a staged promotion is printed.  It is
# not a capability: it is sixteen random characters stored in cleartext on the
# row whose whole job is to prove the operator looked at the staged document
# before making it live.  It reaches no spine payload, no activity_log row, no
# run metric, no Presence payload and no model prompt (design S-1, 6.2, 7.11).
# ---------------------------------------------------------------------------


#: The confirmation code's alphabet, as `ladder_promotions.approval_token`'s
#: own CHECK names it.  Shape-checked before `hmac.compare_digest`, which
#: raises TypeError rather than returning False on a non-ASCII operand (R-4).
APPROVAL_CODE_SHAPE = re.compile(r"\A[A-Za-z0-9_-]{16,43}\Z")


class LadderWorkspaceUnavailable(RuntimeError):
    """The project a promotion belongs to has no reachable workspace (S-8)."""


#: Set once when a store turns out to have no ladder surface.
_LADDER_SURFACE_WARNED = False


def _report_ladder_pass(outcomes: list[dict[str, Any]]) -> None:
    """Print only what an operator watching a worker log needs to see."""
    for outcome in outcomes:
        if not outcome.get("ok", True):
            print(
                "Learning ladder pass for project "
                f"{outcome.get('project_id')} did not run: "
                f"{outcome.get('reason')}.",
                file=sys.stderr,
            )
            continue
        sealed = int(outcome.get("sealed") or 0)
        staged = int(outcome.get("staged") or 0)
        if sealed or staged:
            print(
                f"Learning ladder: sealed {sealed} epoch(s), "
                f"staged {staged} promotion(s)."
            )


def _record_ladder_pass_failure(memory: Memory, exc: BaseException) -> None:
    """A failed background pass is receipted, then forgotten.

    Both halves matter.  Receipted, because a refusal nobody hears about is
    the defect the old `except Exception: pass` distiller had.  Forgotten,
    because the worker's job is the task loop and a background pass must never
    cost it that.
    """
    detail = f"{type(exc).__name__}: {_safe_summary(exc, 200)}"
    print(
        f"Learning ladder pass failed and was skipped ({detail}); "
        "the task loop is unaffected.",
        file=sys.stderr,
    )
    logger = getattr(memory, "log_activity", None)
    if not callable(logger):
        return
    try:
        logger("ladder", "worker", "failed", details={"error": detail})
    except Exception:  # noqa: BLE001 - the receipt is best-effort
        pass


def _ladder_pass_projects(memory: Memory) -> list[int]:
    """Every enabled project the ladder should consider this cycle.

    Returns an empty list rather than guessing.  An earlier version fell back
    to ``[1]`` when enumeration failed or found nothing enabled, which invents
    a fact: if the store cannot say which projects exist, consolidating "the
    one that probably does" is a guess, and if every project is disabled then
    consolidating one of them is wrong outright.  A real store always carries
    project 1 (``Default workspace``, relative path ``.``), so the guess only
    ever fired in the abnormal cases where it was least defensible.

    Doing nothing costs nothing: the pass runs again on the next idle cycle.
    """
    try:
        projects = list(memory.list_projects())
    except (AttributeError, RuntimeError, sqlite3.Error, TypeError, ValueError):
        return []
    return [
        int(project["id"])
        for project in projects
        if project.get("id") is not None
        and project.get("enabled", 1) not in (0, False)
    ]


def run_ladder_consolidation(
    config: Config, memory: Memory, *, source: str = "worker"
) -> list[dict[str, Any]]:
    """One learning-ladder pass over every enabled project.

    The correctness review found the ladder had no runtime driver: with the
    ungoverned distiller removed, nothing sealed an epoch or staged a
    candidate outside the CLI, so the whole mechanism was inert in the
    product.  This is the driver's call site.

    It never approves -- approval is a typed operator command and nothing
    else -- and it is idempotent, so a second cycle over an unchanged store
    does nothing.  Every outcome, including a refusal, is written to
    `activity_log` under category `ladder`, and an exception is logged rather
    than swallowed: a refusal nobody hears about is the defect the old
    `except Exception: pass` distiller had.

    **A store without the ladder surface is not an error.**  Not every object
    a caller passes here is a full `Memory`: the worker tests drive `worker()`
    with a stub that implements only the task-lease methods, and a store below
    schema 49 has no ladder at all.  Asking such a store for `get_project`
    raised `AttributeError` out of this function, through the worker's
    `except Exception` recovery, and abandoned the whole cycle -- so the task
    was never claimed and sixteen tests failed on a symptom (`IndexError` on
    an empty list) with no visible connection to the ladder.  The lesson is
    the general one: a BACKGROUND pass must never cost the foreground loop its
    work, so this returns quietly instead.
    """
    outcomes: list[dict[str, Any]] = []
    seen: set[tuple[int, str]] = set()
    required = (
        "list_projects", "get_project", "ladder_candidates",
        "seal_calibration_epoch",
    )
    missing = [name for name in required if not hasattr(memory, name)]
    if missing:
        # Logged ONCE per process, not once per cycle: a store that will never
        # grow the ladder surface would otherwise fill the worker log with a
        # line that never changes.
        global _LADDER_SURFACE_WARNED
        if not _LADDER_SURFACE_WARNED:
            _LADDER_SURFACE_WARNED = True
            print(
                "Learning ladder: this store has no ladder surface "
                f"({', '.join(missing)} absent); consolidation is skipped.",
                file=sys.stderr,
            )
        return outcomes
    for project_id in _ladder_pass_projects(memory):
        try:
            workspace = _ladder_workspace(config, memory, project_id)
        except LadderWorkspaceUnavailable:
            outcomes.append({
                "project_id": project_id, "ok": False,
                "reason": "workspace_unavailable",
            })
            continue
        # Once per (project, workspace) per cycle: two projects can resolve to
        # one workspace, and sealing it twice would be wasted lock time.
        key = (int(project_id), str(workspace))
        if key in seen:
            continue
        seen.add(key)
        try:
            result = dict(learning_ladder.run_ladder_pass(
                memory=memory, workspace=workspace, project_id=int(project_id)
            ))
            result["project_id"] = int(project_id)
            result.setdefault("ok", True)
        except Exception as exc:  # noqa: BLE001 - logged, never swallowed
            result = {
                "project_id": int(project_id), "ok": False,
                "reason": f"{type(exc).__name__}",
                "detail": _safe_summary(exc, 200),
            }
        outcomes.append(result)
        _log_ladder_outcome(memory, result, source=source)
    return outcomes


def _log_ladder_outcome(
    memory: Memory, result: dict[str, Any], *, source: str
) -> None:
    """Every pass leaves a receipt, including the ones that did nothing."""
    logger = getattr(memory, "log_activity", None)
    if not callable(logger):
        return
    status = "ok" if result.get("ok", True) else "failed"
    try:
        logger(
            "ladder", source, status,
            details={
                "project_id": result.get("project_id"),
                "sealed": int(result.get("sealed") or 0),
                "staged": int(result.get("staged") or 0),
                "refusals": int(result.get("refusals") or 0),
                "errors": len(result.get("errors") or {}),
                "reason": result.get("reason"),
            },
        )
    except (AttributeError, RuntimeError, sqlite3.Error, TypeError, ValueError):
        # The receipt is best-effort; losing it must not stop the worker.
        print(
            f"Learning ladder: receipt not recorded for project "
            f"{result.get('project_id')}.",
            file=sys.stderr,
        )


def _ladder_workspace(config: Config, memory: Memory, project_id: int) -> Path:
    """Derive a promotion's workspace from its project, never from a flag.

    Two steps, because ``resolve_project_workspace`` takes a canonical
    relative path (``"."`` or ``"@projects/<slug>"``) and not an id -- the same
    derivation this module already performs for a task.  Projects have distinct
    workspaces and a learned document's name carries no project component, so
    approving promotion #7 of project 3 from the default workspace would write
    into the wrong ``.jarvis-skills`` (design 3.1, M-10).  A vanished project
    directory raises rather than resolving somewhere else (S-8).
    """
    project = memory.get_project(int(project_id))
    if not project:
        raise LadderWorkspaceUnavailable("workspace_unavailable")
    try:
        return resolve_project_workspace(
            config, str(project.get("relative_path") or "")
        )
    except (OSError, PermissionError, ValueError) as exc:
        raise LadderWorkspaceUnavailable("workspace_unavailable") from exc


def _ladder_code_of(row: Any) -> str:
    """The confirmation code, shown only while the row is still staged.

    A row that has been approved, rolled back, withdrawn or discarded has spent
    or lost its code, and printing it afterwards would suggest it still does
    something (design 7.11: none of the three surfaces prints it for a
    non-staged row).
    """
    if str(_row_get(row, "stage") or "") != "staged":
        return ""
    return str(_row_get(row, "approval_token") or "")


def _row_get(row: Any, key: str) -> Any:
    try:
        return row[key]
    except (IndexError, KeyError, TypeError):
        return None


def _ladder_row_line(row: Any) -> str:
    parts = [
        f"#{int(_row_get(row, 'id') or 0)}",
        f"{_row_get(row, 'family') or '?'}",
        f"[{_row_get(row, 'stage') or '?'}]",
        f"{_row_get(row, 'skill_name') or '?'}",
    ]
    digest = str(
        _row_get(row, "approved_sha256") or _row_get(row, "staged_sha256") or ""
    )
    if digest:
        parts.append(f"document {digest[:12]}")
    reason = str(_row_get(row, "stage_reason") or "")
    if reason:
        parts.append(f"({reason})")
    line = "  ".join(parts)
    code = _ladder_code_of(row)
    if code:
        line += f"\n      confirmation code: {code}"
    return line


def _ladder_monotone_line(verdict: Any) -> str:
    """Say what the operator can act on, not just "regressed".

    ``newest_regressed`` means the last epoch looked bad; only
    ``currently_regressed`` refuses staging and approval, and that needs
    ``LADDER_REGRESSION_STREAK`` consecutive bad epochs.  An operator shown one
    word for both states, who then cannot stage, reasonably concludes the
    surface is lying to them.
    """
    streak = int(_row_get(verdict, "consecutive_regressed") or 0)
    needed = int(getattr(learning_ladder, "LADDER_REGRESSION_STREAK", 2))
    if _row_get(verdict, "currently_regressed"):
        return (
            f"regressed for {streak} consecutive epochs - staging and approval "
            "refused"
        )
    if _row_get(verdict, "newest_regressed"):
        return f"last epoch regressed ({streak} of {needed} needed to refuse)"
    if not _row_get(verdict, "monotone"):
        return "monotone now; an earlier epoch regressed (see violations)"
    return "monotone"


def _ladder_coverage(memory: Memory, family: str | None = None) -> dict[str, Any]:
    """Run `verify_calibration_ledger` and reduce it to what a surface prints.

    Red team R-5 / ruling 20: the method implemented all five checks correctly
    and had NO caller anywhere in the product.  A re-cut epoch -- the tamper
    2.4 calls "the important one" -- was detected by nobody, and
    `ladder status` went on printing `monotone=True coverage_intact=True` over
    a ledger whose keyed digest no longer matched its rows.

    `coverage_intact` here is false on ANY problem, not only a gap: the
    monotonicity verdict's own flag reads only the gap check, so a
    `coverage_digest_mismatch` left it true.
    """
    try:
        report = dict(memory.verify_calibration_ledger(family))
    except (AttributeError, RuntimeError, sqlite3.Error, TypeError, ValueError):
        return {
            "checked": False, "rows": 0, "re_derivable": 0,
            "coverage_intact": True, "problems": [], "coverage_gaps": [],
        }
    problems = [dict(item) for item in (report.get("problems") or [])]
    gaps = [dict(item) for item in (report.get("coverage_gaps") or [])]
    rows = int(report.get("rows") or 0)
    # A row is re-derivable when nothing named it in either list.
    unhealthy = {
        (str(item.get("family")), int(item.get("epoch") or 0))
        for item in problems + gaps
        if item.get("epoch") is not None
    }
    return {
        "checked": True,
        "rows": rows,
        "re_derivable": max(0, rows - len(unhealthy)),
        "coverage_intact": bool(
            report.get("coverage_intact", True) and not problems and not gaps
        ),
        "chain_ok": bool(report.get("chain_ok", True)),
        "sequence_ok": bool(report.get("sequence_ok", True)),
        "lineage_ok": bool(report.get("lineage_ok", True)),
        "problems": problems,
        "coverage_gaps": gaps,
    }


def _print_ladder_coverage(coverage: dict[str, Any], indent: str = "  ") -> None:
    """The 6.3 line, printed wherever an operator looks after a warning."""
    if not coverage.get("checked"):
        return
    rows = int(coverage.get("rows") or 0)
    print(
        f"{indent}coverage: {int(coverage.get('re_derivable') or 0)} of {rows} "
        "epochs re-derivable"
    )
    for problem in (coverage.get("problems") or [])[:5]:
        print(
            f"{indent}  PROBLEM {problem.get('kind')}: "
            f"{problem.get('family')} epoch {problem.get('epoch')}"
        )
    for gap in (coverage.get("coverage_gaps") or [])[:5]:
        print(
            f"{indent}  gap: {gap.get('family')} epoch {gap.get('epoch')} "
            f"({gap.get('missing')} covered row(s) gone)"
        )


def _ladder_status_payload(
    config: Config, memory: Memory, project_id: int | None
) -> dict[str, Any]:
    coverage = _ladder_coverage(memory)
    rows = [dict(row) for row in memory.ladder_promotions(project_id=project_id)]
    legacy = [row for row in rows if str(row.get("stage")) == "unapproved_legacy"]
    unverified: list[dict[str, Any]] = []
    workspace_error: str | None = None
    for candidate in sorted({int(row.get("project_id") or 0) for row in rows}):
        if not candidate:
            continue
        try:
            workspace = _ladder_workspace(config, memory, candidate)
        except LadderWorkspaceUnavailable:
            workspace_error = "workspace_unavailable"
            continue
        unverified.extend(
            memory.ladder_unverified_promotions(
                workspace=workspace, project_id=candidate
            )
        )
    per_family: list[dict[str, Any]] = []
    for family in sorted(learning_ladder.LADDER_FAMILIES):
        gate = memory.calibration_gate(
            family, **learning_ladder.LADDER_GATE_THRESHOLDS
        )
        verdict = memory.calibration_ledger_monotonicity(family)
        epochs = memory.calibration_ledger(family=family)
        newest = dict(epochs[-1]) if epochs else {}
        stage = next(
            (
                str(row.get("stage"))
                for row in reversed(rows)
                if str(row.get("family")) == family
                and str(row.get("stage"))
                in {"staged", "approved", "unapproved_legacy"}
            ),
            "none",
        )
        per_family.append({
            "family": family,
            "gate_allowed": bool(gate.get("allowed")),
            "attempts": gate.get("attempts"),
            "brier": gate.get("brier"),
            "calibration_error": gate.get("calibration_error"),
            "epochs": len(epochs),
            "monotone": bool(verdict.get("monotone")),
            "newest_regressed": bool(verdict.get("newest_regressed")),
            "currently_regressed": bool(verdict.get("currently_regressed")),
            "consecutive_regressed": int(verdict.get("consecutive_regressed") or 0),
            "violations": list(verdict.get("violations") or []),
            # R-5: the verdict's own flag reads only the gap check, so a
            # re-cut epoch left it true.  A ledger with ANY problem is
            # not intact, and the two surfaces must agree about that.
            "coverage_intact": bool(
                verdict.get("coverage_intact", True)
                and coverage.get("coverage_intact", True)
            ),
            "lift_pp": verdict.get("lift_pp"),
            "applied_n": verdict.get("applied_n"),
            "unapplied_n": verdict.get("unapplied_n"),
            "refused_stagings": newest.get("refused_stagings"),
            "refused_approvals": newest.get("refused_approvals"),
            "withdrawals": newest.get("withdrawals"),
            "screened_components": newest.get("screened_components"),
            "unverified_at_seal": newest.get("unverified_at_seal"),
            "artefact_stage": stage,
        })
    return {
        "project_id": project_id,
        "coverage": coverage,
        "families": per_family,
        "unverified_promotions": len(unverified),
        "unverified": unverified,
        "legacy_documents": len(legacy),
        "workspace_error": workspace_error,
    }


def _print_ladder_status(payload: dict[str, Any]) -> None:
    for family in payload.get("families") or []:
        gate = "open" if family["gate_allowed"] else "closed"
        brier = family.get("brier")
        error = family.get("calibration_error")
        print(
            f"{family['family']}: gate {gate} (attempts {family.get('attempts')}, "
            f"brier {'n/a' if brier is None else format(float(brier), '.3f')}, "
            "calibration error "
            f"{'n/a' if error is None else format(float(error), '.3f')})"
        )
        print(
            f"  sealed epochs: {family['epochs']}; {_ladder_monotone_line(family)}"
        )
        for violation in (family.get("violations") or [])[:3]:
            print(
                f"    epoch {violation.get('epoch')} failed clause "
                f"{violation.get('clause')}"
            )
        if not family.get("coverage_intact", True):
            print("  coverage: NOT re-derivable for every epoch (run ladder verify)")
        lift = family.get("lift_pp")
        if lift is not None:
            print(
                f"  applied vs unapplied: {float(lift):+.1f} pp over "
                f"{family.get('applied_n')} applied / "
                f"{family.get('unapplied_n')} unapplied outcomes "
                "(observational, not randomized)"
            )
        if family.get("unverified_at_seal") is not None:
            print(
                f"  newest epoch: {family.get('refused_stagings')} refused "
                f"stagings, {family.get('refused_approvals')} refused approvals, "
                f"{family.get('withdrawals')} withdrawals, "
                f"{family.get('screened_components')} screened components; "
                f"unverified at seal {family.get('unverified_at_seal')}"
            )
        print(f"  artefact: {family['artefact_stage']}")
    _print_ladder_coverage(dict(payload.get("coverage") or {}))
    print(f"Unverified promotions: {int(payload.get('unverified_promotions') or 0)}")
    legacy = int(payload.get("legacy_documents") or 0)
    if legacy:
        # Design ruling 2 / S-4: their own bucket, in their own words.  These
        # are live documents no operator ever approved -- the pre-M4 status quo
        # made visible, not a fault, and not unverified promotions.
        print(f"{legacy} legacy skills live without approval")
        print(
            "  approve or roll back each one; see "
            "ladder list --stage unapproved_legacy"
        )
    if payload.get("workspace_error"):
        print(
            "  at least one project workspace is unavailable; its documents "
            "were not checked"
        )


def _ladder_refusal(
    result: dict[str, Any], *, json_output: bool, verb: str, promotion_id: int
) -> int:
    reason = str(result.get("reason") or result.get("refusal") or "refused")
    if json_output:
        print(json.dumps(
            {"ok": False, "promotion_id": promotion_id, "refusal": reason},
            ensure_ascii=False, indent=2, default=str,
        ))
    else:
        print(skill_promotion_receipt(
            reason,
            promotion_id=promotion_id,
            verb=verb,
            family=result.get("family"),
            newest_id=result.get("newest_id"),
        ))
    return 1


def _print_ladder_pass(
    outcomes: list[dict[str, Any]], *, json_output: bool
) -> int:
    """Report a consolidation pass: what it sealed, staged and refused."""
    if json_output:
        print(json.dumps(outcomes, ensure_ascii=False, indent=2, default=str))
        return 0 if all(item.get("ok", True) for item in outcomes) else 1
    if not outcomes:
        print("No enabled project has a reachable workspace; nothing ran.")
        return 1
    failed = False
    for outcome in outcomes:
        project = outcome.get("project_id")
        if not outcome.get("ok", True):
            failed = True
            print(f"project {project}: pass did not run ({outcome.get('reason')})")
            continue
        sealed = int(outcome.get("sealed") or 0)
        staged = int(outcome.get("staged") or 0)
        refusals = int(outcome.get("refusals") or 0)
        print(
            f"project {project}: sealed {sealed} epoch(s), "
            f"staged {staged} promotion(s), {refusals} refusal(s)"
        )
        for promotion_id in list(outcome.get("staged_promotions") or [])[:10]:
            # The code is NOT printed here: `ladder list` and `ladder show`
            # are the surfaces that show it, and a worker pass writes to a log.
            print(
                f"  staged #{promotion_id} -- see ladder list for its "
                "confirmation code"
            )
        for family, reason in sorted(
            (outcome.get("refusals_by_family") or {}).items()
        )[:10]:
            print(f"  {family}: {reason}")
        for family, detail in sorted((outcome.get("errors") or {}).items())[:10]:
            failed = True
            print(f"  {family}: ERROR {detail}")
    return 1 if failed else 0


def _run_ladder_stage(
    config: Config, memory: Memory, args: argparse.Namespace
) -> int:
    """``ladder stage --family F [--project N] [--yes]``.

    Stage ONE family by hand.  The consolidation worker's own pass
    (`run_ladder_consolidation`, also reachable as `ladder run`) stages every
    qualifying family each cycle; this is the single-family form.  It refuses
    with a closed reason rather than raising, so an operator sees WHY a family
    did not qualify instead of a traceback.
    """
    json_output = bool(getattr(args, "json", False))
    family = str(getattr(args, "family", "") or "").strip()
    project_id = int(getattr(args, "project", None) or 1)
    if not bool(getattr(args, "yes", False)):
        print("ladder stage writes a staged document; re-run with --yes.")
        return 2
    if family not in learning_ladder.LADDER_FAMILIES:
        reason = (
            "family_excluded"
            if family in learning_ladder.LADDER_EXCLUDED_FAMILIES
            else "family_unsupported"
        )
        if json_output:
            print(json.dumps({"ok": False, "staged": False, "reason": reason}))
        else:
            print(f"{family or '(none)'} cannot be staged: {reason}; nothing changed.")
        return 1
    try:
        workspace = _ladder_workspace(config, memory, project_id)
    except LadderWorkspaceUnavailable:
        if json_output:
            print(json.dumps(
                {"ok": False, "staged": False, "reason": "workspace_unavailable"}
            ))
        else:
            print(
                f"Project {project_id} has no reachable workspace; nothing changed."
            )
        return 1
    result = dict(memory.stage_ladder_promotion(
        family=family, project_id=project_id, workspace=workspace
    ))
    if not result.get("staged"):
        reason = str(result.get("reason") or "refused")
        if json_output:
            print(json.dumps({"ok": False, "staged": False, "reason": reason}))
        else:
            print(f"Nothing staged for {family}: {reason}.")
        return 1
    promotion_id = int(result.get("promotion_id") or 0)
    code = str(result.get("approval_token") or "")
    if json_output:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    else:
        print(f"Staged skill promotion #{promotion_id} for {family}.")
        print(f"  confirmation code: {code}")
        print(
            f"  approve with: Approve skill promotion #{promotion_id} {code}"
        )
        print("  the staged document is not visible to the model until approved")
    return 0


def _run_ladder_transition(
    config: Config, memory: Memory, args: argparse.Namespace, command: str
) -> int:
    """``ladder approve|rollback|discard <id> --yes``.

    Every one of them derives its workspace from the row's project, never from
    a flag, and prints the design 6.1 receipt the governed verb prints, from
    the same table.
    """
    json_output = bool(getattr(args, "json", False))
    promotion_id = int(args.id)
    if not bool(getattr(args, "yes", False)):
        print(f"ladder {command} changes the live workspace; re-run with --yes.")
        return 2
    row = memory.ladder_promotion(promotion_id)
    if row is None:
        return _ladder_refusal(
            {"reason": "missing"},
            json_output=json_output,
            verb=command,
            promotion_id=promotion_id,
        )
    row = dict(row)
    try:
        workspace = _ladder_workspace(config, memory, int(row.get("project_id") or 0))
    except LadderWorkspaceUnavailable:
        return _ladder_refusal(
            {"reason": "workspace_unavailable", "family": row.get("family")},
            json_output=json_output,
            verb=command,
            promotion_id=promotion_id,
        )
    if command == "approve":
        code = str(getattr(args, "token", "") or "")
        if not code or APPROVAL_CODE_SHAPE.fullmatch(code) is None:
            # Red team R-4 / ruling 19: `hmac.compare_digest` raises TypeError
            # on a non-ASCII str, and `cli.main` does not catch TypeError, so a
            # fullwidth, en-dashed, Cyrillic or zero-width --token gave the
            # operator a raw traceback instead of a refusal.  The alphabet is
            # the one the column's own CHECK names.
            return _ladder_refusal(
                {
                    "reason": "token_malformed" if code else "token_mismatch",
                    "family": row.get("family"),
                },
                json_output=json_output,
                verb=command,
                promotion_id=promotion_id,
            )
        result = dict(memory.apply_ladder_promotion(
            promotion_id,
            approval_token=code,
            workspace=workspace,
            actor="operator",
            permission="operator:cli",
            # Redacted at the CALLER: memory.py writes operator_prompt to
            # `messages` verbatim and knows nothing of this grammar, and an
            # unredacted turn would carry the code into a later prompt.
            operator_prompt=redact_skill_promotion_command(
                f"Approve skill promotion #{promotion_id} {code}"
            ),
        ))
    elif command == "rollback":
        result = dict(memory.rollback_ladder_promotion(
            promotion_id,
            workspace=workspace,
            actor="operator",
            permission="operator:cli",
            operator_prompt=f"Roll back skill promotion #{promotion_id}",
        ))
    else:
        result = dict(memory.discard_ladder_promotion(
            promotion_id,
            workspace=workspace,
            actor="operator",
            permission="operator:cli",
        ))
    if result.get("reason") or result.get("refusal"):
        return _ladder_refusal(
            result, json_output=json_output, verb=command, promotion_id=promotion_id
        )
    if json_output:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return 0
    if command == "discard":
        print(f"Discarded staged skill promotion #{promotion_id}; nothing was live.")
        return 0
    if command == "approve":
        # R-7 / ruling 22: `retired_legacy` and `restored` are the names the
        # store returns; the CLI and the governed verb read the same two.
        if result.get("retired_legacy"):
            outcome = "approved_over_legacy"
        elif result.get("prior_sha256"):
            outcome = "approved"
        else:
            outcome = "approved_first"
    else:
        outcome = "rolled_back" if result.get("restored") else "rolled_back_removed"
    print(skill_promotion_receipt(
        outcome,
        promotion_id=promotion_id,
        verb=command,
        family=result.get("family") or row.get("family"),
        digest=result.get("approved_sha256") or row.get("approved_sha256"),
    ))
    return 0


def _run_ladder_verify(
    config: Config, memory: Memory, args: argparse.Namespace
) -> int:
    """``ladder verify [--apply --yes --plan TOKEN] [--json]``.

    The reconciler AND the one-time grandfather pass, on the exact discipline
    ``graph rebuild`` and ``spine rebuild-claims`` use:

      0  nothing to reconcile, or applied
      1  a refused apply, including ``stale_plan``, with nothing changed
      2  a dry run that WOULD change something, or misuse of the flags

    The database is the record and the filesystem is reconciled to it, never
    the other way round: a live document whose digest has drifted is withdrawn
    rather than restored, so an operator's own edit is never overwritten.
    """
    json_output = bool(getattr(args, "json", False))
    apply = bool(getattr(args, "apply", False))
    yes = bool(getattr(args, "yes", False))
    requested_token = str(getattr(args, "plan", None) or "").strip()
    if yes and not apply:
        print("--yes requires --apply; nothing changed.")
        return 2
    if requested_token and not (apply and yes):
        print("--plan requires --apply --yes; nothing changed.")
        return 2
    if apply and yes and not requested_token:
        # Red team R-11 / ruling 22: a deliberate departure from the
        # `graph rebuild` and `spine rebuild-claims` shape, where --plan is
        # optional.  `ladder verify` is the only reconciler whose apply path
        # moves an operator's live learned skill out of the live root, so the
        # plan the operator read is the plan that gets applied, always.
        print(
            "--apply --yes requires --plan TOKEN for ladder verify: it is the "
            "one reconciler that can remove a live learned skill, so it "
            "applies only the plan you read.\n"
            "Run it without --apply first and pass the token it prints."
        )
        return 2
    project_id = getattr(args, "project", None)
    project_id = int(project_id) if project_id is not None else 1
    try:
        workspace = _ladder_workspace(config, memory, project_id)
    except LadderWorkspaceUnavailable:
        # S-8: a project whose directory is gone is reported and skipped, not
        # silently resolved to some other workspace.
        if json_output:
            print(json.dumps({"ok": False, "refusal": "workspace_unavailable"}))
        else:
            print(
                f"Project {project_id} has no reachable workspace "
                "(workspace_unavailable); nothing changed."
            )
        return 1
    plan = dict(memory.ladder_reconciliation_plan(workspace, project_id=project_id))
    actions = list(plan.get("actions") or [])
    # R-5: the reconciler is where an operator looks after a coverage
    # warning, so the ledger's own integrity is reported here too.
    plan["coverage"] = _ladder_coverage(memory)
    fresh_token = str(plan.get("plan_token") or "")
    if not apply:
        if json_output:
            print(json.dumps(plan, ensure_ascii=False, indent=2, default=str))
        else:
            _print_ladder_verify(plan)
            if actions:
                hint = f" --plan {fresh_token}" if fresh_token else ""
                print(
                    f"Re-run with --apply --yes{hint} to reconcile exactly this plan."
                )
        return 2 if actions else 0
    if not actions:
        if json_output:
            print(json.dumps(
                {"applied": False, "reason": "nothing to apply", **plan},
                ensure_ascii=False, indent=2, default=str,
            ))
        else:
            print("Nothing to apply: the ladder record matches the workspace.")
        return 0
    if not yes:
        _print_ladder_verify(plan)
        hint = f" --plan {fresh_token}" if fresh_token else ""
        print(f"Re-run with --apply --yes{hint} to reconcile exactly this plan.")
        return 2
    applied = dict(memory.reconcile_ladder(
        workspace,
        project_id=project_id,
        apply=True,
        plan_token=requested_token or None,
        actor="operator",
        permission="operator:cli",
    ))
    refusal = str(applied.get("refusal") or applied.get("reason") or "")
    if refusal:
        if json_output:
            print(json.dumps(
                {"ok": False, "applied": False, "refusal": refusal,
                 "requested_plan_token": requested_token or None,
                 "plan_token": fresh_token or None},
                ensure_ascii=False, indent=2, default=str,
            ))
        elif refusal == "stale_plan":
            print(
                "Ladder reconciliation refused: stale_plan; the store no longer "
                "matches the plan you confirmed (current plan token: "
                f"{fresh_token or 'unavailable'}); nothing changed."
            )
        else:
            print(f"Ladder reconciliation refused: {refusal}; nothing changed.")
        return 1
    if json_output:
        print(json.dumps(applied, ensure_ascii=False, indent=2, default=str))
    else:
        _print_ladder_verify(applied, heading="Ladder reconciliation (applied)")
    return 0


def _print_ladder_verify(
    plan: dict[str, Any], heading: str = "Ladder reconciliation (dry run)"
) -> None:
    print(heading)
    actions = list(plan.get("actions") or [])
    if not actions:
        print("  nothing to reconcile")
        _print_ladder_coverage(dict(plan.get("coverage") or {}))
        return
    for action in actions[:20]:
        line = (
            f"  {action.get('action')}: {action.get('skill_name') or '?'} "
            f"({action.get('reason') or 'no reason recorded'})"
        )
        # An action the store planned but did not perform comes back with
        # `done: False` and a note saying why.  Printing it as though it had
        # happened would tell an operator a live document moved when it is
        # still sitting there -- the one thing this surface must never get
        # wrong.
        if "done" in action and not action.get("done"):
            line += "  [not performed]"
        note = str(action.get("note") or "")
        if note:
            line += f" -- {note}"
        print(line)
    if len(actions) > 20:
        print(f"  ... and {len(actions) - 20} more")
    if "changed" in plan:
        changed = int(plan.get("changed") or 0)
        print(
            f"  {changed} of {len(actions)} action(s) performed"
            if changed != len(actions)
            else f"  {changed} action(s) performed"
        )
    _print_ladder_coverage(dict(plan.get("coverage") or {}))
    token = str(plan.get("plan_token") or "")
    if token:
        print(f"  plan token: {token}")


def _display_ladder(config: Config, memory: Memory, project_id: int) -> None:
    """`/ladder` in chat: staged and live promotions, with their codes.

    Rendered before any model call, like `/facts` and `/memory`, so the
    confirmation code of a staged row never passes through a prompt or a reply
    to reach the operator (design 6.2 item 4).
    """
    try:
        rows = [
            dict(row)
            for row in memory.ladder_promotions(
                project_id=project_id, include_token=True
            )
        ]
    except (AttributeError, RuntimeError, sqlite3.Error, TypeError, ValueError):
        print("This store has no learning ladder.")
        return
    live = [
        row
        for row in rows
        if str(row.get("stage")) in {"staged", "approved", "unapproved_legacy"}
    ]
    if not live:
        print("No skill promotions are staged or live for this project.")
    for row in live:
        print(_ladder_row_line(row))
    legacy = sum(1 for row in rows if str(row.get("stage")) == "unapproved_legacy")
    if legacy:
        print(f"{legacy} legacy skills live without approval")
    try:
        report = memory.lesson_recall_report()
    except (AttributeError, RuntimeError, sqlite3.Error, ValueError):
        report = None
    if isinstance(report, dict) and str(report.get("mode") or "idle") != "idle":
        # The operator-visible answer to "why did my lesson go quiet"; never
        # shown to the model (design 5.4).
        line = f"Last lesson read: {report.get('mode')}"
        if report.get("reason"):
            line += f" ({report.get('reason')})"
        shadowed = report.get("superseded_shadowed")
        if shadowed:
            line += f"; {shadowed} row(s) shadowed by a lifecycle change"
        print(line)


def _run_ladder(args: argparse.Namespace) -> int:
    """Operator surfaces over the learning ladder (VTMF M4 design 6.3)."""
    config = Config.load()
    json_output = bool(getattr(args, "json", False))
    command = str(getattr(args, "ladder_command", "") or "")
    project_id = getattr(args, "project", None)
    project_id = int(project_id) if project_id is not None else None
    with Memory(config.data_dir / "jarvis.db") as memory:
        if command == "status":
            payload = _ladder_status_payload(config, memory, project_id)
            if json_output:
                print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
            else:
                _print_ladder_status(payload)
            return 0
        if command == "list":
            stage = str(getattr(args, "stage", "") or "").strip() or None
            rows = [
                dict(row)
                for row in memory.ladder_promotions(
                    project_id=project_id,
                    stages=[stage] if stage else None,
                    include_token=True,
                )
            ]
            if json_output:
                # The code rides in --json too: this is one of the three
                # surfaces that may show it, and an operator scripting an
                # approval needs it.  It is still absent for a non-staged row.
                print(json.dumps(
                    [
                        {**row, "approval_token": _ladder_code_of(row) or None}
                        for row in rows
                    ],
                    ensure_ascii=False, indent=2, default=str,
                ))
            elif not rows:
                print("No skill promotions are recorded.")
            else:
                for row in rows:
                    print(_ladder_row_line(row))
            return 0
        if command == "show":
            row = memory.ladder_promotion(int(args.id), include_token=True)
            if row is None:
                print(f"No skill promotion #{int(args.id)} exists.")
                return 1
            row = dict(row)
            if json_output:
                print(json.dumps(
                    {**row, "approval_token": _ladder_code_of(row) or None},
                    ensure_ascii=False, indent=2, default=str,
                ))
                return 0
            print(_ladder_row_line(row))
            print(
                f"  project: {row.get('project_id')}   "
                f"created: {row.get('created_at')}"
            )
            print(
                f"  proof {str(row.get('proof_sha256') or '')[:12]} over "
                f"{row.get('reuse_count')} verified reuses in "
                f"{row.get('context_count')} distinct contexts"
            )
            # Design 3.3 / M-1, in the operator's own words: this number is a
            # usage threshold, not a significance test, and an application row
            # is filed when the lesson MATCHED, not when the model used it.
            print(
                "  (a usage threshold, not a significance test: an application "
                "row is filed when the lesson matched the turn, not when the "
                "model used it)"
            )
            if row.get("approved_at"):
                print(f"  approved: {row.get('approved_at')}")
            if row.get("prior_sha256"):
                print(f"  previous version kept: {str(row['prior_sha256'])[:12]}")
            elif str(row.get("stage")) in {"approved", "unapproved_legacy"}:
                print("  no previous version existed; a rollback removes the document")
            return 0
        if command == "ledger":
            family = str(getattr(args, "family", "") or "").strip() or None
            rows = [dict(row) for row in memory.calibration_ledger(family=family)]
            coverage = _ladder_coverage(memory, family)
            if json_output:
                print(json.dumps(rows, ensure_ascii=False, indent=2, default=str))
            elif not rows:
                print("No calibration epochs are sealed.")
            else:
                for row in rows:
                    print(
                        f"{row.get('family')} epoch {row.get('epoch')}: "
                        f"n={row.get('n')} successes={row.get('successes')} "
                        f"brier={float(row.get('brier') or 0.0):.3f} "
                        "calibration_error="
                        f"{float(row.get('calibration_error') or 0.0):.3f} "
                        f"unverified_at_seal={row.get('unverified_at_seal')}"
                    )
                _print_ladder_coverage(coverage)
            return 0
        if command == "seal":
            family = str(getattr(args, "family", "") or "").strip()
            everything = bool(getattr(args, "all", False))
            if bool(family) == everything:
                print("Give exactly one of --family F or --all; nothing changed.")
                return 2
            if everything:
                # HIGH-2: --all is the same pass the worker runs, so the
                # two can never drift into sealing by different rules.
                outcomes = run_ladder_consolidation(
                    config, memory, source="operator"
                )
                return _print_ladder_pass(outcomes, json_output=json_output)
            families = [family]
            sealed: list[dict[str, Any]] = []
            for name in families:
                sealed.extend(
                    dict(row)
                    for row in memory.seal_calibration_epoch(
                        name, actor="operator", permission="operator:cli"
                    )
                )
            if json_output:
                print(json.dumps(sealed, ensure_ascii=False, indent=2, default=str))
            elif not sealed:
                print("No whole epoch was ready to seal; nothing changed.")
            else:
                for row in sealed:
                    print(
                        f"Sealed {row.get('family')} epoch {row.get('epoch')} "
                        f"(n={row.get('n')})."
                    )
                if everything:
                    # L-5: many short write locks, not one long one.  Say so,
                    # because an operator watching a bulk catch-up should know
                    # the turn path is not blocked for its whole duration.
                    print(
                        f"{len(sealed)} epochs sealed, each in its own brief "
                        "write transaction."
                    )
            return 0
        if command == "run":
            # The same pass the consolidation worker runs each cycle.
            return _print_ladder_pass(
                run_ladder_consolidation(config, memory, source="operator"),
                json_output=json_output,
            )
        if command == "stage":
            return _run_ladder_stage(config, memory, args)
        if command in {"approve", "rollback", "discard"}:
            return _run_ladder_transition(config, memory, args, command)
        if command == "verify":
            return _run_ladder_verify(config, memory, args)
    return 2


def _run_graph(args: argparse.Namespace) -> int:
    """Operator surfaces over the temporal graph projection (VTMF M3).

    ``status`` and ``verify`` print ids and counts only; ``paths`` shows
    values because it is the same screened read the agent performs.
    """
    config = Config.load()
    json_output = bool(getattr(args, "json", False))
    with Memory(config.data_dir / "jarvis.db") as memory:
        if args.graph_command == "status":
            # Counts only: verify_graph compares every claim to its edge and
            # costs seconds on a large store, which is not what "status" is.
            counts = memory_graph.graph_counts(memory.db)
            payload = {
                "ready": bool(counts.get("ready", True)),
                "edges": int(counts.get("edges", 0) or 0),
                "entities": int(counts.get("entities", 0) or 0),
                "excluded": counts.get("excluded"),
                "last_projection_event_id": _last_graph_projection_event(memory),
            }
            if json_output:
                print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
            elif not payload["ready"]:
                print("This store has no graph projection.")
            else:
                print(
                    f"Memory graph: {payload['edges']} edges, "
                    f"{payload['entities']} entities; excluded "
                    f"{_graph_excluded_line(payload['excluded'])}."
                )
                event_id = payload["last_projection_event_id"]
                if isinstance(event_id, int) and not isinstance(event_id, bool):
                    print(f"  last projection receipt: event #{event_id}")
                print("  run graph verify to check it against the claims")
            return 0
        if args.graph_command == "verify":
            report = memory.verify_graph()
            if json_output:
                print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
            elif not report.get("ready", True):
                print("This store has no graph projection; nothing to verify.")
            else:
                state = "OK" if report.get("ok") else "FAILED"
                print(
                    f"Memory graph {state}: {int(report.get('edges', 0) or 0)} edges, "
                    f"{int(report.get('entities', 0) or 0)} entities; excluded "
                    f"{_graph_excluded_line(report.get('excluded'))}."
                )
                _graph_problem_lines(report.get("problems"))
            return 0 if report.get("ok") else 1
        if args.graph_command == "rebuild":
            return _run_graph_rebuild(memory, args)
        subject = " ".join(args.subject or []).strip()
        requested_hops = int(getattr(args, "hops", memory_graph.MAX_HOPS))
        if not 1 <= requested_hops <= memory_graph.MAX_HOPS:
            # Silently widening --hops 0 to the full walk would print more
            # than the operator asked to see.
            print(
                f"--hops must be between 1 and {memory_graph.MAX_HOPS}; "
                "nothing was read."
            )
            return 2
        hops = requested_hops
        if contains_secret(subject):
            raise ValueError("Potential secret detected; no subject was read")
        # The subject is a start, not a question: passed as the query its own
        # words would act as an asked-predicate filter and no chain would
        # terminate on them.  An empty query is the open read, which is what
        # "the chains the agent would see for that subject" means.
        result = memory.graph_chains(
            "",
            project_id=int(args.project),
            subjects=[subject] if subject else [],
            seed_claims=[],
            temporal=bool(getattr(args, "temporal", False)),
        )
        rows = [row for row in (result.get("rows") or []) if isinstance(row, dict)]
        overflow = [
            row for row in (result.get("overflow") or []) if isinstance(row, dict)
        ]
        report = result.get("report") or {}
        if json_output:
            print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
            return 0
        mode = _safe_summary(report.get("mode"), 40)
        unresolved = [
            _safe_summary(name, 60)
            for name in (report.get("unresolved") or [])
            if isinstance(name, str) and name.strip()
        ]
        if not rows:
            print(f"No chain answers \"{_safe_summary(subject, 60)}\" ({mode}).")
            _print_unresolved_names(unresolved)
            return 0
        print(
            f"Chains from \"{_safe_summary(subject, 60)}\" in project "
            f"{int(args.project)} ({mode}):"
        )
        _print_graph_chain_rows(rows, hops)
        for entry in overflow:
            print(
                f"  overflow at hop {entry.get('hop')}: "
                f"{_safe_summary(entry.get('subject', ''), 60)} - "
                f"{_safe_summary(entry.get('note'), 200)}"
            )
        _print_unresolved_names(unresolved)
        return 0


def _run_spine(args: argparse.Namespace) -> int:
    """Operator surfaces over the memory spine; prints keys and counts only."""
    config = Config.load()
    json_output = bool(getattr(args, "json", False))
    with Memory(config.data_dir / "jarvis.db") as memory:
        if args.spine_command == "verify":
            report = memory.verify_spine()
            if json_output:
                print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
            else:
                state = "OK" if report.get("ok") else "FAILED"
                claim_backing = report.get(
                    "claim_backing_rows", report.get("claim_rows", 0)
                )
                print(
                    f"Memory spine {state}: {int(report.get('events', 0))} events, "
                    f"{int(report.get('redacted', 0))} redacted; "
                    f"{int(report.get('memory_rows', 0))} memories, "
                    f"{int(claim_backing or 0)} claim backing rows, "
                    f"{int(report.get('memory_events', 0))} memory events."
                )
                if "graph_edges" in report:
                    # Informational only: a drifted projection is a rebuild
                    # matter, so it never changes this exit code (design 4.6).
                    print(
                        f"Memory graph: {int(report.get('graph_edges', 0) or 0)} "
                        f"edges, {int(report.get('graph_entities', 0) or 0)} "
                        "entities; projection "
                        f"{'OK' if report.get('graph_ok') else 'DIVERGENT'}."
                    )
                for text in list(report.get("problems") or [])[:50]:
                    print(f"  problem: {_safe_summary(text, 160)}")
                # R-5 / ruling 20: the calibration ledger's integrity is
                # reported beside the chain's.  Informational here -- a
                # ledger problem is a ladder matter, not a chain fault --
                # so it never changes this exit code.
                _print_ladder_coverage(_ladder_coverage(memory))
            return 0 if report.get("ok") else 1
        if args.spine_command == "rebuild-milestones":
            # E-2's surface.  `rebuild_equivalence_derived` can legitimately be
            # None -- a partial or empty derivation -- and None must never
            # render as a tick, a zero or a dash: an operator reading a dash
            # concludes "nothing to report" when the truth is "the comparison
            # could not be made".
            report = memory.rebuild_milestones()
            if json_output:
                print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
                return 0 if report.get("ok") else 1
            equivalence = report.get("rebuild_equivalence_derived")
            if equivalence is None:
                reason = str(report.get("equivalence_reason") or "not derivable")
                print(f"Milestone rebuild: NOT COMPARED ({reason}).")
            else:
                state = "equivalent" if report.get("ok") else "DIVERGENT"
                print(
                    f"Milestone rebuild: {state}; derived equivalence "
                    f"{equivalence}."
                )
            if not report.get("chain_verified", True):
                print(
                    "  spine chain did not verify - run 'jarvis spine verify', "
                    "this result is downstream of it"
                )
            for milestone_id in list(report.get("equivalence_mismatched") or [])[:50]:
                print(f"  milestone {milestone_id}: derived block diverged")
            return 0 if report.get("ok") else 1
        if args.spine_command == "rebuild-claims":
            return _run_spine_rebuild_claims(memory, args)
        if args.spine_command == "rebuild-memories":
            report = memory.rebuild_memory_projection()
            if json_output:
                print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
            else:
                state = "equivalent" if report.get("ok") else "DIVERGENT"
                print(
                    f"Memory projection rebuild (dry run): {state}; live rows "
                    f"{int(report.get('rows_live', 0))}, rebuilt rows "
                    f"{int(report.get('rows_rebuilt', 0))}."
                )
                for item in list(report.get("divergences") or [])[:50]:
                    print(
                        f"  memory {item.get('memory_id')}: {item.get('kind')}: "
                        f"{_safe_summary(item.get('detail'), 160)}"
                    )
            return 0 if report.get("ok") else 1
        items = memory.spine_tail(limit=int(args.limit))
        if json_output:
            print(json.dumps(items, ensure_ascii=False, indent=2))
        else:
            if not items:
                print("Memory spine is empty.")
            for item in items:
                flags = " redacted" if item.get("redacted") else ""
                print(
                    f"  #{item['id']} {item['created_at'][:19]} {item['kind']} "
                    f"[{item['actor']}/{item['outcome']}] {item['scope']} "
                    f"{item.get('subject_kind') or '-'}:{item.get('subject_id') or '-'} "
                    f"keys={','.join(item.get('payload_keys') or [])}{flags}"
                )
        return 0


def _run_facts(args: argparse.Namespace) -> int:
    config = Config.load()
    with Memory(config.data_dir / "jarvis.db") as memory:
        _display_project_facts(
            memory, int(args.project), " ".join(args.subject or [])
        )
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
            actor="operator",
            permission="operator:cli",
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
    since = datetime.now(UTC) - timedelta(hours=args.since)
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
    compaction = sub.add_parser(
        "compaction", help="inspect and run transcript compaction"
    )
    compaction_sub = compaction.add_subparsers(
        dest="compaction_command", required=True
    )
    compaction_status = compaction_sub.add_parser(
        "status", help="stored milestone and span counts"
    )
    compaction_status.add_argument("--json", action="store_true")
    compaction_milestones = compaction_sub.add_parser(
        "milestones", help="list one conversation's milestones"
    )
    compaction_milestones.add_argument("--conversation", type=int, required=True)
    compaction_milestones.add_argument("--limit", type=int, default=50)
    compaction_milestones.add_argument("--json", action="store_true")
    compaction_show = compaction_sub.add_parser(
        "show", help="one milestone's metadata and summary"
    )
    compaction_show.add_argument("--handle", required=True)
    compaction_show.add_argument(
        "--rehydrate", action="store_true",
        help="also print the ORIGINAL message text (terminal only, confirmed)",
    )
    compaction_show.add_argument("--json", action="store_true")
    compaction_run = compaction_sub.add_parser(
        "run", help="plan or apply one compaction pass"
    )
    compaction_run.add_argument("--conversation", type=int, required=True)
    compaction_run.add_argument("--apply", action="store_true")
    compaction_run.add_argument("--yes", action="store_true")
    compaction_run.add_argument("--plan")
    compaction_run.add_argument("--json", action="store_true")
    compaction_verify = compaction_sub.add_parser(
        "verify", help="check every milestone against its span and receipt"
    )
    compaction_verify.add_argument("--json", action="store_true")

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
        help="run the immutable self-repair behavioral anchor set",
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
        "memory",
        help=(
            "inspect neural recall and automatic memory improvement; list "
            "ordinary memories by id, or erase one"
        ),
    )
    memory_quality.add_argument(
        "memory_command",
        nargs="?",
        choices=("status", "list", "erase"),
        default="status",
        help=argparse.SUPPRESS,
    )
    memory_quality.add_argument(
        "memory_id",
        nargs="?",
        type=int,
        default=None,
        help="with erase: the memory id shown by memory list or /memory",
    )
    memory_quality.add_argument(
        "--yes",
        action="store_true",
        help=(
            "confirm memory erase; without it the kind and created date are "
            "printed and the exit status is 2"
        ),
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
    control_sub.add_parser("status", help="show the current background-control state")
    for name in ("pause", "resume", "stop"):
        item = control_sub.add_parser(name)
        item.add_argument("--reason", default=None)
    strategy_transfer = sub.add_parser(
        "strategy-transfer",
        help="start, inspect, abort, or explicitly promote a bounded Phase 4B trial",
    )
    strategy_transfer_sub = strategy_transfer.add_subparsers(
        dest="strategy_transfer_command", required=True
    )
    strategy_transfer_status = strategy_transfer_sub.add_parser(
        "status", help="show prompt-free Phase 4B trial status"
    )
    strategy_transfer_status.add_argument(
        "--manifest", dest="manifest_id", type=_trial_positive_integer, default=None
    )
    strategy_transfer_status.add_argument("--json", action="store_true")
    strategy_transfer_start = strategy_transfer_sub.add_parser(
        "start", help="start one explicitly scoped, pinned causal trial"
    )
    strategy_transfer_start.add_argument(
        "--project", type=_trial_positive_integer, required=True
    )
    strategy_transfer_start.add_argument(
        "--family",
        action="append",
        choices=PREDICTION_FAMILY_CHOICES,
        required=True,
        help="target family (repeat 1-3 times)",
    )
    strategy_transfer_start.add_argument(
        "--strategy",
        action="append",
        choices=STRATEGY_VOCABULARY,
        required=True,
        help="closed procedural strategy (repeatable)",
    )
    strategy_transfer_start.add_argument(
        "--sample-cap", type=_trial_sample_cap, required=True
    )
    strategy_transfer_start.add_argument(
        "--duration-days", type=_trial_duration_days, required=True
    )
    strategy_transfer_start.add_argument(
        "--seed",
        type=_trial_sha256,
        default=None,
        help=argparse.SUPPRESS,
    )
    strategy_transfer_abort = strategy_transfer_sub.add_parser(
        "abort", help="permanently stop new assignments for one trial"
    )
    strategy_transfer_abort.add_argument(
        "manifest_id", type=_trial_positive_integer
    )
    strategy_transfer_promote = strategy_transfer_sub.add_parser(
        "promote",
        help="explicitly promote a completed trial after causal validation",
    )
    strategy_transfer_promote.add_argument(
        "manifest_id", type=_trial_positive_integer
    )
    workflow = sub.add_parser(
        "workflow",
        help="inspect and control restart-safe registered workflow state",
    )
    workflow_sub = workflow.add_subparsers(
        dest="workflow_command", required=True
    )
    workflow_status = workflow_sub.add_parser(
        "status", help="show prompt-free workflow counts for one exact project"
    )
    workflow_status.add_argument(
        "--project", type=_trial_positive_integer, required=True
    )
    workflow_status.add_argument("--limit", type=_workflow_limit, default=50)
    workflow_status.add_argument("--json", action="store_true")
    workflow_list = workflow_sub.add_parser(
        "list", help="list prompt-free workflow receipts for one exact project"
    )
    workflow_list.add_argument(
        "--project", type=_trial_positive_integer, required=True
    )
    workflow_list.add_argument("--limit", type=_workflow_limit, default=50)
    workflow_list.add_argument("--json", action="store_true")
    workflow_show = workflow_sub.add_parser(
        "show", help="show one prompt-free project-bound workflow receipt"
    )
    workflow_show.add_argument("plan_id", type=_trial_positive_integer)
    workflow_show.add_argument(
        "--project", type=_trial_positive_integer, required=True
    )
    workflow_show.add_argument("--json", action="store_true")
    workflow_start = workflow_sub.add_parser(
        "start", help="persist one reviewed closed Phase 5 manifest"
    )
    workflow_start.add_argument(
        "--project", type=_trial_positive_integer, required=True
    )
    workflow_start.add_argument(
        "--manifest", type=Path, required=True,
        help="bounded UTF-8 JSON manifest; executable fields are not accepted",
    )
    workflow_start.add_argument("--json", action="store_true")
    for action, help_text in (
        ("pause", "mark a plan paused so the coordinator rejects future claims"),
        ("resume", "mark one coherently paused plan active again"),
        ("cancel", "terminally cancel one registered plan"),
    ):
        workflow_action = workflow_sub.add_parser(action, help=help_text)
        workflow_action.add_argument("plan_id", type=_trial_positive_integer)
        workflow_action.add_argument(
            "--project", type=_trial_positive_integer, required=True
        )
        workflow_action.add_argument("--json", action="store_true")
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
    project_add = project_sub.add_parser("add", aliases=("create",))
    project_add.set_defaults(project_command="add")
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
    spine = sub.add_parser(
        "spine",
        help=(
            "inspect the append-only memory spine (verify, rebuild-claims, "
            "rebuild-memories, tail)"
        ),
    )
    spine_sub = spine.add_subparsers(dest="spine_command", required=True)
    spine_verify = spine_sub.add_parser(
        "verify", help="recompute the keyed event chain and claim lineage"
    )
    spine_verify.add_argument("--json", action="store_true")
    spine_rebuild = spine_sub.add_parser(
        "rebuild-claims",
        help=(
            "replay the spine into a shadow claim projection and report divergences; "
            "--apply --yes reconciles the live rows in place"
        ),
    )
    spine_rebuild.add_argument(
        "--apply",
        action="store_true",
        help="reconcile the live claim projection from the spine (requires --yes)",
    )
    spine_rebuild.add_argument(
        "--yes",
        action="store_true",
        help=(
            "confirm --apply; without it the plan and its plan token are printed "
            "and the exit status is 2"
        ),
    )
    spine_rebuild.add_argument(
        "--plan",
        metavar="TOKEN",
        default=None,
        help=(
            "with --apply --yes: apply only the plan whose token was printed by "
            "--apply without --yes; a store that changed since then is refused "
            "with stale_plan and nothing is changed"
        ),
    )
    spine_rebuild.add_argument("--json", action="store_true")
    spine_rebuild_memories = spine_sub.add_parser(
        "rebuild-memories",
        help=(
            "replay memory and lesson events into a shadow projection and report "
            "divergences (dry run; digests and ids, never content)"
        ),
    )
    spine_rebuild_memories.add_argument("--json", action="store_true")
    spine_rebuild_milestones = spine_sub.add_parser(
        "rebuild-milestones",
        help="re-derive every milestone's derived block from the spine",
    )
    spine_rebuild_milestones.add_argument("--json", action="store_true")
    spine_tail = spine_sub.add_parser(
        "tail", help="recent spine events (payload keys only, never values)"
    )
    spine_tail.add_argument("--limit", type=int, default=20)
    spine_tail.add_argument("--json", action="store_true")
    ladder = sub.add_parser(
        "ladder",
        help=(
            "the learning ladder: calibration epochs and governed skill "
            "promotions (status, list, show, stage, approve, rollback, "
            "discard, seal, verify, ledger)"
        ),
    )
    ladder_sub = ladder.add_subparsers(dest="ladder_command", required=True)
    ladder_status = ladder_sub.add_parser(
        "status",
        help=(
            "per family: the gate, the sealed epochs and their monotonicity, "
            "the artefact stage, unverified promotions, and legacy documents"
        ),
    )
    ladder_status.add_argument("--project", type=int)
    ladder_status.add_argument("--json", action="store_true")
    ladder_list = ladder_sub.add_parser(
        "list",
        help=(
            "every recorded skill promotion; prints the confirmation code of "
            "a staged one (one of only three surfaces that ever do)"
        ),
    )
    ladder_list.add_argument("--project", type=int)
    ladder_list.add_argument(
        "--stage",
        choices=[
            "staged", "approved", "unapproved_legacy", "rolled_back",
            "withdrawn", "discarded",
        ],
    )
    ladder_list.add_argument("--json", action="store_true")
    ladder_show = ladder_sub.add_parser(
        "show", help="one promotion, its proof counts, and its confirmation code"
    )
    ladder_show.add_argument("id", type=int)
    ladder_show.add_argument("--json", action="store_true")
    ladder_stage = ladder_sub.add_parser(
        "stage",
        help=(
            "derive an outcome proof and stage a skill document the model "
            "cannot read; refuses with a reason rather than raising"
        ),
    )
    ladder_stage.add_argument("--family", required=True)
    ladder_stage.add_argument("--project", type=int, default=1)
    ladder_stage.add_argument("--yes", action="store_true")
    ladder_stage.add_argument("--json", action="store_true")
    ladder_approve = ladder_sub.add_parser(
        "approve",
        help=(
            "make a staged document live; requires the confirmation code from "
            "ladder list or ladder show"
        ),
    )
    ladder_approve.add_argument("id", type=int)
    ladder_approve.add_argument("--token", required=True)
    ladder_approve.add_argument("--yes", action="store_true")
    ladder_approve.add_argument("--json", action="store_true")
    ladder_rollback = ladder_sub.add_parser(
        "rollback",
        help=(
            "restore the exact bytes a promotion replaced, or remove the "
            "document when nothing was live before; needs no code"
        ),
    )
    ladder_rollback.add_argument("id", type=int)
    ladder_rollback.add_argument("--yes", action="store_true")
    ladder_rollback.add_argument("--json", action="store_true")
    ladder_discard = ladder_sub.add_parser(
        "discard", help="throw away a staged document that was never approved"
    )
    ladder_discard.add_argument("id", type=int)
    ladder_discard.add_argument("--yes", action="store_true")
    ladder_discard.add_argument("--json", action="store_true")
    ladder_seal = ladder_sub.add_parser(
        "seal",
        help=(
            "seal every whole unsealed block of resolved outcomes; boundaries "
            "are mechanical, so --all is a catch-up and never a choice"
        ),
    )
    ladder_seal.add_argument("--family")
    ladder_seal.add_argument("--all", action="store_true")
    ladder_seal.add_argument("--json", action="store_true")
    ladder_verify = ladder_sub.add_parser(
        "verify",
        help=(
            "reconcile the ladder record against the workspace, and run the "
            "one-time grandfather pass; --apply --yes reconciles in place"
        ),
    )
    ladder_verify.add_argument("--project", type=int)
    ladder_verify.add_argument("--apply", action="store_true")
    ladder_verify.add_argument("--yes", action="store_true")
    ladder_verify.add_argument("--plan")
    ladder_verify.add_argument("--json", action="store_true")
    ladder_run = ladder_sub.add_parser(
        "run",
        help=(
            "run one consolidation pass now: seal every complete epoch, "
            "then stage every candidate. Never approves."
        ),
    )
    ladder_run.add_argument("--json", action="store_true")
    ladder_ledger = ladder_sub.add_parser(
        "ledger", help="the sealed calibration epochs, oldest first"
    )
    ladder_ledger.add_argument("--family")
    ladder_ledger.add_argument("--json", action="store_true")
    graph = sub.add_parser(
        "graph",
        help=(
            "inspect the temporal graph projection (status, verify, rebuild, "
            "paths)"
        ),
    )
    graph_sub = graph.add_subparsers(dest="graph_command", required=True)
    graph_status = graph_sub.add_parser(
        "status", help="edge, entity, and exclusion counts for the projection"
    )
    graph_status.add_argument("--json", action="store_true")
    graph_verify = graph_sub.add_parser(
        "verify",
        help=(
            "check every edge against its claim row and every entity against "
            "its edges (fields only, never values)"
        ),
    )
    graph_verify.add_argument("--json", action="store_true")
    graph_rebuild = graph_sub.add_parser(
        "rebuild",
        help=(
            "re-project the graph from the live claim rows and report "
            "divergences; --apply --yes reconciles it in place"
        ),
    )
    graph_rebuild.add_argument(
        "--apply",
        action="store_true",
        help="reconcile the live graph projection (requires --yes)",
    )
    graph_rebuild.add_argument(
        "--yes",
        action="store_true",
        help=(
            "confirm --apply; without it the plan and its plan token are "
            "printed and the exit status is 2"
        ),
    )
    graph_rebuild.add_argument(
        "--plan",
        metavar="TOKEN",
        default=None,
        help=(
            "with --apply --yes: apply only the plan whose token was printed "
            "by --apply without --yes; a store that changed since then is "
            "refused with stale_plan and nothing is changed"
        ),
    )
    graph_rebuild.add_argument("--json", action="store_true")
    graph_paths = graph_sub.add_parser(
        "paths",
        help="the screened chains the agent would see for one subject",
    )
    graph_paths.add_argument("subject", nargs="+")
    graph_paths.add_argument("--project", type=int, default=1)
    graph_paths.add_argument(
        "--hops",
        type=int,
        default=3,
        help="show only hops up to this depth (1-3); the store walks three",
    )
    graph_paths.add_argument(
        "--temporal",
        action="store_true",
        help="include superseded versions, as a past-tense question does",
    )
    graph_paths.add_argument("--json", action="store_true")
    facts = sub.add_parser(
        "facts", help="list active governed project facts for one project"
    )
    facts.add_argument("--project", type=int, default=1)
    facts.add_argument(
        "subject", nargs="*", help="optional subject or predicate words to filter by"
    )
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
    reflection.add_argument(
        "reflection_command",
        nargs="?",
        choices=("list",),
        default="list",
        help=argparse.SUPPRESS,
    )
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
            code = _run_memory(args)
        elif args.command == "usage":
            code = _run_usage(args)
        elif args.command == "control":
            code = _run_control(args)
        elif args.command == "strategy-transfer":
            code = _run_strategy_transfer(args)
        elif args.command == "workflow":
            code = _run_workflow(args)
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
        elif args.command == "facts":
            code = _run_facts(args)
        elif args.command == "ladder":
            code = _run_ladder(args)
        elif args.command == "compaction":
            code = _run_compaction(args)
        elif args.command == "graph":
            code = _run_graph(args)
        elif args.command == "spine":
            code = _run_spine(args)
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
