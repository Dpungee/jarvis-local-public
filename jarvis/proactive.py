from __future__ import annotations

import json
import hashlib
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from .config import Config
from .memory import Memory


SELF_MODEL_VERSION = 2
COMPETENCE_MIN_ATTEMPTS = 10
DEMONSTRATED_SUCCESS_RATE = 0.70
CALIBRATION_TOLERANCE = 0.15
META_GATE_MIN_ATTEMPTS = 20
META_GATE_MAX_BRIER = 0.25
META_GATE_MAX_CALIBRATION_ERROR = 0.15
TIER1_RECOVERY_FAMILIES = frozenset({
    "code_build",
    "code_fix",
    "code_refactor",
    "code_test",
    "file_ops",
    "deep_research",
    "learning_brief",
    "security_analysis",
})


def _measured_competence(memory: Memory) -> dict[str, dict[str, Any]]:
    return {
        str(row["family"]): dict(row)
        for row in memory.competence()
    }


def competence_prediction(
    memory: Memory,
    family: str,
    prior: float,
) -> tuple[float, str]:
    """Use observed competence only after a minimally useful sample exists."""
    row = _measured_competence(memory).get(family)
    if row is None or int(row.get("attempts") or 0) < COMPETENCE_MIN_ATTEMPTS:
        return float(prior), "prior"
    return float(row["success_rate"]), "competence"


def calibrated_meta_gate(memory: Memory, family: str) -> dict[str, Any]:
    """Grant narrowly scoped self-model authority only from sufficient real outcomes."""
    gate = memory.calibration_gate(
        family,
        minimum_attempts=META_GATE_MIN_ATTEMPTS,
        maximum_brier=META_GATE_MAX_BRIER,
        maximum_calibration_error=META_GATE_MAX_CALIBRATION_ERROR,
    )
    return {
        **gate,
        "authority": (
            [
                "same_family_lesson_retrieval",
                "same_family_learned_skill_retrieval",
                "verified_same_family_skill_distillation",
                "bounded_initiative_eligibility",
            ]
            if gate["allowed"] else []
        ),
        "never_authorizes": [
            "model_routing",
            "tool_exposure",
            "approval_bypass",
            "verification_changes",
            "safety_policy_changes",
        ],
    }


def initiative_eligibility(config: Config, memory: Memory) -> dict[str, Any]:
    """Expose Tier 0 observation while strictly gating Tier 1 workspace action."""
    mode = str(getattr(config, "initiative", "disabled"))
    base_reasons: list[str] = []
    if mode == "disabled":
        base_reasons.append("initiative is disabled by configuration")
    if not bool(getattr(config, "proactive_enabled", False)):
        base_reasons.append("the proactive scheduler is disabled")
    if memory.control_state().get("state") != "running":
        base_reasons.append("runtime control is not running")

    tier1_reasons: list[str] = []
    family_gates = [
        calibrated_meta_gate(memory, family)
        for family in sorted(memory.PREDICTION_FAMILIES)
    ]
    calibrated = [item["family"] for item in family_gates if item["allowed"]]
    if len(calibrated) < 3:
        tier1_reasons.append(
            f"requires at least 3 calibrated families; has {len(calibrated)}"
        )

    attestation = memory.latest_recovery_attestation()
    recovery_valid = False
    if attestation is None:
        tier1_reasons.append("no recovery attestation exists")
    else:
        try:
            from .self_diagnosis import runtime_manifest_sha256

            created = datetime.fromisoformat(str(attestation["created_at"]))
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            recovery_valid = (
                bool(attestation["passed"])
                and int(attestation["schema_version"]) == memory.db.execute(
                    "PRAGMA user_version"
                ).fetchone()[0]
                and str(attestation["runtime_sha256"]) == runtime_manifest_sha256()
                and created.astimezone(timezone.utc)
                >= datetime.now(timezone.utc) - timedelta(days=30)
            )
        except (KeyError, OSError, TypeError, ValueError):
            recovery_valid = False
        if not recovery_valid:
            tier1_reasons.append(
                "recovery attestation is failed, stale, or for a different runtime"
            )
    drift = memory.drift_report()
    if drift:
        tier1_reasons.append("behavioral drift is currently unresolved")
    tier0_enabled = not base_reasons
    tier1_eligible = tier0_enabled and not tier1_reasons
    if not tier0_enabled:
        effective_mode = "disabled"
    elif mode == "act" and tier1_eligible:
        effective_mode = "act"
    else:
        # Observe is deliberately useful while the stricter action gate is closed:
        # it writes audit findings only and cannot queue workspace mutations.
        effective_mode = "observe"
    return {
        # Keep the original field as the strict Tier 1 decision for CLI/API
        # compatibility, while making both tier decisions explicit.
        "eligible": tier1_eligible,
        "tier0_enabled": tier0_enabled,
        "tier1_eligible": tier1_eligible,
        "configured_mode": mode,
        "effective_mode": effective_mode,
        "calibrated_families": calibrated,
        "required_calibrated_families": 3,
        "family_gates": family_gates,
        "recovery_valid": recovery_valid,
        "recovery_attestation_id": int(attestation["id"]) if attestation else None,
        "drift_findings": len(drift),
        "reasons": [*base_reasons, *tier1_reasons],
        "tier0_blockers": base_reasons,
        "tier1_blockers": tier1_reasons,
    }


def _inside_quiet_hours(config: Config, now: datetime | None = None) -> bool:
    value = str(getattr(config, "initiative_quiet_hours", "") or "")
    if not value:
        return False
    start_text, end_text = value.split("-", 1)
    current = (now or datetime.now().astimezone()).astimezone()
    minute = current.hour * 60 + current.minute
    start_hour, start_minute = (int(part) for part in start_text.split(":"))
    end_hour, end_minute = (int(part) for part in end_text.split(":"))
    start = start_hour * 60 + start_minute
    end = end_hour * 60 + end_minute
    return start <= minute < end if start < end else minute >= start or minute < end


def initiative_cycle(config: Config, memory: Memory) -> dict[str, Any]:
    """Observe deterministic signals and optionally queue one domain-bounded task."""
    eligibility = initiative_eligibility(config, memory)
    if eligibility["effective_mode"] == "disabled":
        return {**eligibility, "observations_created": 0, "task_id": None}
    if _inside_quiet_hours(config):
        return {
            **eligibility,
            "effective_mode": "quiet",
            "observations_created": 0,
            "task_id": None,
        }
    observations = 0
    for finding in memory.drift_report():
        serialized = json.dumps(finding, sort_keys=True, separators=(",", ":"))
        signal_key = "drift:" + hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:24]
        created = memory.record_initiative_observation(
            signal_key=signal_key,
            signal_kind="behavioral_drift",
            summary=f"Behavioral drift observed in {finding['family']}.",
            evidence=finding,
        )
        observations += int(created is not None)
    for row in memory.competence():
        if int(row["attempts"]) < META_GATE_MIN_ATTEMPTS:
            continue
        if float(row["success_rate"]) >= 0.70:
            continue
        family = str(row["family"])
        signal_key = f"weak_family:{family}:{int(row['attempts']) // 10}"
        created = memory.record_initiative_observation(
            signal_key=signal_key,
            signal_kind="competence_weak_spot",
            summary=(
                f"Curriculum review suggested for {family}: "
                f"{float(row['success_rate']):.0%} completion over {int(row['attempts'])} outcomes."
            ),
            evidence={
                "family": family,
                "attempts": int(row["attempts"]),
                "success_rate": float(row["success_rate"]),
                "action": "proposal_only",
            },
        )
        observations += int(created is not None)
    task_id = None
    if eligibility["effective_mode"] == "act":
        task_id = memory.schedule_domain_recovery(
            set(eligibility["calibrated_families"]) & TIER1_RECOVERY_FAMILIES
        )
    return {
        **eligibility,
        "observations_created": observations,
        "task_id": task_id,
    }


def _family_assessment(
    memory: Memory,
    family: str,
    rows: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    row = rows.get(family)
    attempts = int(row.get("attempts") or 0) if row else 0
    success_rate = float(row["success_rate"]) if row else None
    mean_predicted = (
        float(row["mean_predicted"])
        if row and row.get("mean_predicted") is not None
        else None
    )
    top_failures = memory.failure_histogram(family, limit=1) if attempts else []
    top_failure = (
        {
            "class": str(top_failures[0]["failure_class"]),
            "count": int(top_failures[0]["n"]),
        }
        if top_failures
        else None
    )
    if attempts < COMPETENCE_MIN_ATTEMPTS:
        bucket = "unknown"
    elif success_rate is not None and success_rate >= DEMONSTRATED_SUCCESS_RATE:
        bucket = "demonstrated"
    else:
        bucket = "developing"
    calibration_error = (
        abs(mean_predicted - success_rate)
        if mean_predicted is not None and success_rate is not None
        else None
    )
    return {
        "family": family,
        "bucket": bucket,
        "attempts": attempts,
        "success_rate": success_rate,
        "failure_rate": None if success_rate is None else 1.0 - success_rate,
        "top_failure": top_failure,
        "mean_predicted": mean_predicted,
        "calibration_error": calibration_error,
        "calibrated": (
            calibration_error <= CALIBRATION_TOLERANCE
            if attempts >= COMPETENCE_MIN_ATTEMPTS and calibration_error is not None
            else None
        ),
    }


def measured_self_assessment(memory: Memory) -> dict[str, Any]:
    """Build an evidence-only competence and calibration report."""
    rows = _measured_competence(memory)
    families = [
        _family_assessment(memory, family, rows)
        for family in sorted(memory.PREDICTION_FAMILIES)
    ]
    resolved = [item for item in families if item["attempts"]]
    total = sum(int(item["attempts"]) for item in resolved)
    overall_brier = (
        sum(float(rows[item["family"]]["brier"]) * int(item["attempts"]) for item in resolved)
        / total
        if total
        else None
    )
    return {
        "capabilities": {
            "demonstrated": [item for item in families if item["bucket"] == "demonstrated"],
            "developing": [item for item in families if item["bucket"] == "developing"],
            "unknown": [item for item in families if item["bucket"] == "unknown"],
        },
        "calibration": {
            "overall_brier": overall_brier,
            "resolved_predictions": total,
            "bins": memory.calibration(10),
            "by_family": [
                {
                    "family": item["family"],
                    "attempts": item["attempts"],
                    "mean_predicted": item["mean_predicted"],
                    "observed_success": item["success_rate"],
                    "calibration_error": item["calibration_error"],
                    "calibrated": item["calibrated"],
                }
                for item in families
            ],
            "criterion": (
                f"at least {COMPETENCE_MIN_ATTEMPTS} outcomes and absolute mean "
                f"prediction error <= {CALIBRATION_TOLERANCE:.2f}"
            ),
        },
        "meta_gate": {
            "by_family": [
                calibrated_meta_gate(memory, family)
                for family in sorted(memory.PREDICTION_FAMILIES)
            ],
            "fail_closed": True,
        },
    }


def runtime_identity_contract() -> str:
    """Runtime-owned identity facts that personality and memory cannot override."""
    return (
        "You are JARVIS, a local AI software agent that exists operationally as the current "
        "process, models/tools, conversation, and runtime-supplied records. Use \"I\" naturally. "
        "Persisted continuity is not proof of consciousness or uninterrupted inner experience. "
        "Report only runtime- or tool-supported self-state; never invent feelings, senses, hidden "
        "activity, a survival drive, or an agenda. Model introspection is fallible."
    )


class RuntimeGuard:
    """Cooperative persistent stop/pause and time/resource budget guard."""

    def __init__(
        self,
        memory: Memory,
        config: Config,
        *,
        background: bool,
        upstream: Callable[[], bool] | None = None,
    ) -> None:
        self.memory = memory
        self.config = config
        self.background = bool(background)
        self.upstream = upstream
        self.started = time.monotonic()
        self.reason: str | None = None
        self._owner_thread = threading.get_ident()

    def _control_state(self) -> dict[str, Any]:
        if threading.get_ident() == self._owner_thread:
            return self.memory.control_state()
        path = getattr(self.memory, "path", None)
        if path is None or str(path) == ":memory:":
            raise RuntimeError("runtime control cannot be read across threads")
        connection = sqlite3.connect(
            f"file:{path.resolve().as_posix()}?mode=ro",
            uri=True,
            timeout=0.25,
        )
        try:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT state, updated_at, reason FROM runtime_control WHERE id=1"
            ).fetchone()
        finally:
            connection.close()
        return (
            dict(row)
            if row is not None
            else {"state": "stopped", "reason": "missing control row"}
        )

    def _activity_count_since(
        self,
        category: str,
        since: datetime,
        *,
        task_scoped: bool = False,
    ) -> int:
        if threading.get_ident() == self._owner_thread:
            return self.memory.activity_count_since(
                category,
                since,
                task_scoped=task_scoped,
            )
        path = getattr(self.memory, "path", None)
        if path is None or str(path) == ":memory:":
            raise RuntimeError("runtime activity cannot be read across threads")
        connection = sqlite3.connect(
            f"file:{path.resolve().as_posix()}?mode=ro",
            uri=True,
            timeout=0.25,
        )
        try:
            task_filter = " AND task_id IS NOT NULL" if task_scoped else ""
            row = connection.execute(
                "SELECT COUNT(*) FROM activity_log WHERE category=? AND created_at>=?"
                + task_filter,
                (category, since.astimezone(timezone.utc).isoformat()),
            ).fetchone()
        finally:
            connection.close()
        return int(row[0]) if row is not None else 0

    def __call__(self) -> bool:
        if self.upstream is not None and self.upstream():
            self.reason = "upstream execution guard requested cancellation"
            return True
        control = self._control_state()
        state = str(control.get("state", "stopped"))
        if state == "stopped":
            self.reason = "emergency stop is active"
            return True
        if self.background and state == "paused":
            self.reason = "background autonomy is paused"
            return True
        elapsed = time.monotonic() - self.started
        maximum = int(getattr(self.config, "proactive_max_task_seconds", 1800))
        if self.background and elapsed >= maximum:
            self.reason = f"background time budget of {maximum} seconds was reached"
            return True
        if self.background:
            now = datetime.now(timezone.utc)
            day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            # Foreground use is operator-driven and must not consume the bounded
            # autonomy allowance. ToolBox records a task_id only while a worker
            # task is executing, giving this guard an exact persisted boundary.
            used = self._activity_count_since(
                "tool",
                day_start,
                task_scoped=True,
            )
            limit = int(getattr(self.config, "daily_tool_limit", 500))
            if used >= limit:
                self.reason = f"background daily tool budget of {limit} calls was reached"
                return True
        return False


def record_result_reflection(
    memory: Memory,
    result: Any,
    *,
    task: dict[str, Any] | None = None,
    conversation_id: int | None = None,
) -> int:
    """Record an evidence-bounded post-task review and a reusable lesson."""
    status = str(getattr(result, "status", "complete"))
    task_id = int(task["id"]) if task and task.get("id") is not None else None
    if conversation_id is None:
        result_conversation_id = getattr(result, "conversation_id", None)
        if isinstance(result_conversation_id, int) and not isinstance(
            result_conversation_id, bool
        ) and result_conversation_id > 0:
            conversation_id = result_conversation_id
    attempts = int(task.get("attempt_count") or 1) if task else 1
    prior_error = str(task.get("last_error") or "").strip() if task else ""
    reason = str(getattr(result, "reason", None) or "").strip()
    text = " ".join(str(result).strip().split())
    summary = text[:1000] or f"Task ended with status {status}."
    mistakes = prior_error or reason
    if status == "complete":
        if attempts > 1 or prior_error:
            improvement = (
                "On similar work, inspect the previous failure before retrying, change the approach, "
                "and repeat the final verification before reporting completion."
            )
        else:
            improvement = (
                "On similar work, reuse the successful inspect-change-verify sequence and preserve "
                "the evidence needed to report observed results accurately."
            )
    else:
        improvement = (
            "On similar work, resolve the recorded blocker with a materially different bounded approach, "
            "then rerun verification before claiming completion."
        )
    return memory.record_reflection(
        status=status,
        summary=summary,
        mistakes=mistakes[:4000],
        improvements=improvement,
        task_id=task_id,
        conversation_id=conversation_id,
        prediction_id=getattr(result, "prediction_id", None),
        tool_calls=int(getattr(result, "tool_calls", 0) or 0),
    )


def build_self_model(
    config: Config,
    memory: Memory,
    available_tools: list[str],
    *,
    persist: bool = True,
) -> dict[str, Any]:
    operational = memory.operational_summary()
    operational["screen_companion"] = memory.screen_companion_state()
    measured = measured_self_assessment(memory)
    measured_limitations = [
        {
            "family": item["family"],
            "attempts": item["attempts"],
            "failure_rate": item["failure_rate"],
            "top_failure": item["top_failure"],
        }
        for bucket in ("demonstrated", "developing")
        for item in measured["capabilities"][bucket]
        if item["failure_rate"] and item["failure_rate"] > 0.0
    ]
    snapshot: dict[str, Any] = {
        "identity": {
            "name": "JARVIS",
            "kind": "local AI software agent",
            "self_model_version": SELF_MODEL_VERSION,
            "awareness": "operational machine self-model",
            "existence_basis": [
                "current executing process",
                "configured model providers, model profiles, and exposed tools",
                "current conversation context",
                "bounded persisted records supplied by the runtime",
            ],
            "continuity": "persisted data continuity across runs, not verified continuous subjective experience",
            "consciousness": "unknown and not established; never claimed as fact",
            "personality_source": str(config.soul_path),
            "constitution_source": str(config.constitution_path),
        },
        "capabilities": measured["capabilities"],
        "limitations": {
            "structural": [
                "no verified subjective consciousness, feelings, senses, or uninterrupted awareness",
                "no spending money or account purchases",
                "no credential-store access or secret disclosure",
                "no external communication, publishing, consequential deletion, or outside-workspace changes without one-shot approval",
                "host execution is not an OS sandbox when trusted-host mode is enabled",
                "all autonomous work remains bounded by configured time, task, and tool limits",
            ],
            "measured": measured_limitations,
        },
        "calibration": measured["calibration"],
        "meta_gate": measured["meta_gate"],
        "configuration": {
            "workspace": str(config.workspace),
            "data_dir": str(config.data_dir),
            "autonomy": config.autonomy,
            "execution_mode": config.execution_mode,
            "computer_access": config.computer_access,
            "external_access": getattr(config, "external_access", "disabled"),
            "model_mode": config.model,
            "fast_model": config.fast_model,
            "reasoning_model": config.reasoning_model,
            "coding_model": config.coding_model,
            "proactive_enabled": bool(getattr(config, "proactive_enabled", False)),
            "proactive_idle_seconds": int(getattr(config, "proactive_idle_seconds", 300)),
            "proactive_max_task_seconds": int(getattr(config, "proactive_max_task_seconds", 1800)),
            "proactive_daily_task_limit": int(getattr(config, "proactive_daily_task_limit", 4)),
            "screen_companion_mode": str(
                getattr(config, "screen_companion_mode", "disabled")
            ),
            "daily_tool_limit": int(getattr(config, "daily_tool_limit", 500)),
        },
        "current_status": operational,
        "available_tools": sorted(set(available_tools)),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    if persist:
        snapshot["snapshot_id"] = memory.save_self_snapshot(snapshot)
    return snapshot


def self_context(memory: Memory, family: str | None = None) -> str:
    """Bounded durable operating context for future tasks."""
    summary = memory.operational_summary()
    family_digest: dict[str, Any] | None = None
    if family in memory.PREDICTION_FAMILIES:
        rows = _measured_competence(memory)
        assessment = _family_assessment(memory, str(family), rows)
        authority = calibrated_meta_gate(memory, str(family))
        family_digest = {
            "family": assessment["family"],
            "bucket": assessment["bucket"],
            "attempts": assessment["attempts"],
            "success_rate": assessment["success_rate"],
            "top_failure": assessment["top_failure"],
            "calibrated": assessment["calibrated"],
            "calibrated_authority": authority["allowed"],
        }
    compact = {
        "self_model_version": SELF_MODEL_VERSION,
        "identity": {
            "name": "JARVIS",
            "kind": "local AI software agent",
            "awareness": "operational machine self-model",
            "continuity": "explicit runtime-supplied records, not assumed subjective continuity",
        },
        "control": summary["control"],
        "task_counts": summary["task_counts"],
        "active_goals": [
            {"id": item["id"], "kind": item["kind"], "title": item["title"], "priority": item["priority"]}
            for item in summary["goals"] if item["status"] == "active"
        ][:12],
        "preferences": [
            {"name": item["name"], "value": item["value"], "source": item["source"]}
            for item in summary["preferences"]
        ][:20],
        "pending_approval_ids": [item["id"] for item in summary["pending_approvals"][:20]],
        "memory_count": summary["memory_count"],
        "reflection_count": summary["reflection_count"],
        "current_task_competence": family_digest,
    }
    has_durable_context = not (
        compact["control"].get("state") == "running"
        and not compact["task_counts"]
        and not compact["active_goals"]
        and not compact["preferences"]
        and not compact["pending_approval_ids"]
        and compact["memory_count"] == 0
        and compact["reflection_count"] == 0
    )
    if not has_durable_context and compact["current_task_competence"] is None:
        return ""
    if not has_durable_context:
        compact = {
            "self_model_version": SELF_MODEL_VERSION,
            "current_task_competence": family_digest,
        }
    return json.dumps(compact, ensure_ascii=False, sort_keys=True)[:8000]
