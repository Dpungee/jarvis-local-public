from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import PurePosixPath
from typing import Any, Mapping


_DIAGNOSIS_CATEGORIES = frozenset({
    "connectivity",
    "render_cache",
    "authentication",
    "process",
    "update",
    "unknown",
})
_ALLOWED_REPAIR_KINDS = frozenset({"backup_move", "restart", "verify"})
_MUTATING_REPAIR_KINDS = frozenset({"backup_move", "restart"})
_SAFE_IDENTIFIER = re.compile(r"[a-z0-9](?:[a-z0-9._-]{0,198}[a-z0-9])?\Z")
_MAX_CACHE_PATHS = 8
_LESSON_TTL = timedelta(days=30)
_VERIFICATION_KEYS = (
    "backup_created",
    "source_moved",
    "restart_observed",
    "ui_rendered",
    "health_check_passed",
    "network_reachable",
    "authentication_succeeded",
    "process_healthy",
    "application_updated",
)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _parse_time(value: Any, *, fallback: datetime | None = None) -> datetime:
    if isinstance(value, datetime):
        return _utc(value)
    if isinstance(value, str) and value.strip():
        try:
            return _utc(datetime.fromisoformat(value.strip().replace("Z", "+00:00")))
        except ValueError:
            pass
    if fallback is not None:
        return _utc(fallback)
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return _utc(value).isoformat()


def _strict_time(value: Any, label: str) -> datetime:
    if isinstance(value, datetime):
        return _utc(value)
    if isinstance(value, str) and value.strip():
        try:
            return _utc(datetime.fromisoformat(value.strip().replace("Z", "+00:00")))
        except ValueError as exc:
            raise ValueError(f"{label} is not a valid timestamp") from exc
    raise ValueError(f"{label} is required")


def _stable_sha256(payload: Mapping[str, Any]) -> str:
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8", errors="strict")
    return hashlib.sha256(serialized).hexdigest()


def _outcome_receipt_material(outcome: "RepairOutcome") -> dict[str, Any]:
    return {
        "application": outcome.application,
        "application_version": outcome.application_version,
        "diagnosis_category": outcome.diagnosis_category,
        "status": outcome.status,
        "completed_actions": list(outcome.completed_actions),
        "verification": dict(outcome.verification),
        "rollback_available": outcome.rollback_available,
    }


def _plain_relative_path(value: Any, label: str) -> str:
    text = str(value or "").strip().replace("\\", "/")
    path = PurePosixPath(text)
    if (
        not text
        or len(text) > 500
        or text.startswith("/")
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
        or any(character in text for character in "\x00\r\n")
    ):
        raise ValueError(f"{label} must be a canonical relative path")
    return path.as_posix()


@dataclass(frozen=True)
class RepairAction:
    kind: str
    source: str | None = None
    destination: str | None = None
    verifier: str | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "source": self.source,
            "destination": self.destination,
            "verifier": self.verifier,
        }


@dataclass(frozen=True)
class Diagnosis:
    category: str
    confidence: float
    evidence: tuple[str, ...]
    alternatives: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()

    def to_payload(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "confidence": self.confidence,
            "evidence": list(self.evidence),
            "alternatives": list(self.alternatives),
            "limitations": list(self.limitations),
        }


@dataclass(frozen=True)
class RepairPlan:
    application: str
    application_version: str
    diagnosis: Diagnosis
    actions: tuple[RepairAction, ...]
    requires_approval: bool
    reversible: bool = True

    def to_payload(self) -> dict[str, Any]:
        return {
            "application": self.application,
            "application_version": self.application_version,
            "diagnosis": self.diagnosis.to_payload(),
            "actions": [action.to_payload() for action in self.actions],
            "requires_approval": self.requires_approval,
            "reversible": self.reversible,
        }


@dataclass(frozen=True)
class RepairOutcome:
    application: str
    application_version: str
    diagnosis_category: str
    status: str
    completed_actions: tuple[str, ...]
    verification: dict[str, Any]
    rollback_available: bool
    receipt_sha256: str

    def to_payload(self) -> dict[str, Any]:
        return {
            "application": self.application,
            "application_version": self.application_version,
            "diagnosis_category": self.diagnosis_category,
            "status": self.status,
            "completed_actions": list(self.completed_actions),
            "verification": dict(self.verification),
            "rollback_available": self.rollback_available,
            "receipt_sha256": self.receipt_sha256,
        }


@dataclass(frozen=True)
class AppRepairLesson:
    application: str
    application_version: str
    diagnosis_category: str
    repair_kinds: tuple[str, ...]
    verification_kinds: tuple[str, ...]
    observed_at: datetime
    valid_until: datetime
    outcome_sha256: str
    contradicted_by: tuple[str, ...] = ()
    advisory_only: bool = True

    def to_payload(self) -> dict[str, Any]:
        return {
            "application": self.application,
            "application_version": self.application_version,
            "diagnosis_category": self.diagnosis_category,
            "repair_kinds": list(self.repair_kinds),
            "verification_kinds": list(self.verification_kinds),
            "observed_at": _iso(self.observed_at),
            "valid_until": _iso(self.valid_until),
            "outcome_sha256": self.outcome_sha256,
            "contradicted_by": list(self.contradicted_by),
            "advisory_only": self.advisory_only,
        }


def classify_app_failure(evidence: Mapping[str, Any]) -> Diagnosis:
    """Classify bounded app-health evidence without granting repair authority."""
    if not isinstance(evidence, Mapping):
        raise TypeError("Application evidence must be an object")

    process_running = evidence.get("process_running")
    network_reachable = evidence.get("network_reachable")
    dns_ok = evidence.get("dns_ok")
    authentication_ok = evidence.get("authentication_ok")
    ui_rendered = evidence.get("ui_rendered")
    javascript_errors = evidence.get("javascript_errors")
    cache_bytes = evidence.get("cache_bytes")

    # A stopped or repeatedly crashing process is the most immediate measured
    # failure boundary. It is not evidence of a network failure.
    if process_running is False:
        details = ["The application process was not running."]
        if int(evidence.get("crash_count") or 0) > 0:
            details.append("One or more application crashes were observed.")
        return Diagnosis(
            "process",
            0.94,
            tuple(details),
            alternatives=("update", "unknown"),
            limitations=("No crash dump or private process memory was inspected.",),
        )

    if (
        evidence.get("update_required") is True
        or evidence.get("reported_update_required") is True
    ):
        return Diagnosis(
            "update",
            0.93 if evidence.get("update_required") is True else 0.65,
            ((
                "The installed application reported that an update is required."
                if evidence.get("update_required") is True
                else "The operator reported an update-required symptom."
            ),),
            alternatives=("process", "unknown"),
            limitations=("No installer is authorized or executed by this diagnosis.",),
        )

    if (
        authentication_ok is False
        or evidence.get("authentication_error")
        or evidence.get("reported_authentication_failure") is True
    ):
        measured = authentication_ok is False or bool(evidence.get("authentication_error"))
        return Diagnosis(
            "authentication",
            0.92 if measured else 0.65,
            ((
                "Authentication did not succeed in the bounded health evidence."
                if measured
                else "The operator reported an authentication failure."
            ),),
            alternatives=("connectivity", "unknown"),
            limitations=("Credentials, tokens, cookies, and account contents were not inspected.",),
        )

    render_signal = (
        (ui_rendered is False or evidence.get("reported_render_failure") is True)
        and (
            bool(javascript_errors)
            or isinstance(cache_bytes, (int, float))
            or evidence.get("renderer_error") is True
            or evidence.get("blank_window") is True
        )
    )
    if render_signal and network_reachable is True:
        details = ["The application had positive network reachability evidence."]
        if authentication_ok is True:
            details.append("Authentication was observed as healthy.")
        details.append("The user interface did not render and render/cache evidence was present.")
        return Diagnosis(
            "render_cache",
            0.96,
            tuple(details),
            alternatives=("update", "process"),
            limitations=(
                "A visible window title alone is not proof that pixels rendered correctly.",
            ),
        )

    if (
        network_reachable is False
        or dns_ok is False
        or evidence.get("reported_connectivity_failure") is True
    ):
        details = []
        if network_reachable is False:
            details.append("The application's required network endpoint was not reachable.")
        if dns_ok is False:
            details.append("Name resolution did not succeed.")
        if (
            network_reachable is not False
            and dns_ok is not False
            and evidence.get("reported_connectivity_failure") is True
        ):
            details.append("The operator reported a connectivity failure.")
        return Diagnosis(
            "connectivity",
            0.9 if network_reachable is False or dns_ok is False else 0.65,
            tuple(details),
            alternatives=("authentication", "process"),
            limitations=(
                "Diagnosis does not change firewall, proxy, hosts, DNS, router, or security settings.",
            ),
        )

    if render_signal:
        return Diagnosis(
            "render_cache",
            0.72,
            ("The user interface did not render and render/cache evidence was present.",),
            alternatives=("connectivity", "update"),
            limitations=("Current network reachability was not positively established.",),
        )

    return Diagnosis(
        "unknown",
        0.2,
        ("The available evidence does not isolate a supported failure family.",),
        alternatives=tuple(sorted(_DIAGNOSIS_CATEGORIES - {"unknown"})),
        limitations=("More bounded health evidence is required before proposing a repair.",),
    )


def build_repair_plan(
    application: Mapping[str, Any],
    diagnosis: Diagnosis,
    state: Mapping[str, Any],
) -> RepairPlan:
    """Create a reversible, declarative plan; it performs no host action."""
    if not isinstance(application, Mapping) or not isinstance(state, Mapping):
        raise TypeError("Application and state must be objects")
    app_id = str(application.get("id") or "").strip().casefold()
    version = str(application.get("version") or "").strip()
    if _SAFE_IDENTIFIER.fullmatch(app_id) is None:
        raise ValueError("Application id must be a stable, non-secret identifier")
    if not version or len(version) > 100 or any(char in version for char in "\x00\r\n"):
        raise ValueError("Application version is required for a repair plan")
    if diagnosis.category not in _DIAGNOSIS_CATEGORIES:
        raise ValueError("Unsupported application diagnosis category")

    actions: list[RepairAction] = []
    if diagnosis.category == "render_cache":
        raw_paths = state.get("cache_paths") or []
        if not isinstance(raw_paths, list) or not raw_paths:
            raise ValueError("A render/cache repair requires an exact cache path")
        if len(raw_paths) > _MAX_CACHE_PATHS:
            raise ValueError("Too many cache paths in one repair plan")
        backup_root = _plain_relative_path(
            state.get("backup_root"), "Backup root"
        )
        for index, raw_path in enumerate(raw_paths):
            source = _plain_relative_path(raw_path, "Cache path")
            destination = (
                PurePosixPath(backup_root)
                / app_id
                / f"{index + 1:02d}-{PurePosixPath(source).name}"
            ).as_posix()
            if source == destination:
                raise ValueError("Repair backup must differ from its source")
            actions.append(RepairAction(
                "backup_move",
                source=source,
                destination=destination,
                verifier="backup_created+source_moved",
            ))
        actions.extend((
            RepairAction("restart", verifier="restart_observed"),
            RepairAction("verify", verifier="ui_rendered+health_check_passed"),
        ))
    elif diagnosis.category == "process":
        actions.extend((
            RepairAction("restart", verifier="restart_observed"),
            RepairAction("verify", verifier="ui_rendered+health_check_passed"),
        ))
    else:
        # Connectivity, authentication, update, and unknown failures have no
        # generally safe automatic mutation. Keep diagnosis useful, but do not
        # smuggle account, installer, firewall, proxy, hosts, or registry work
        # into a generic repair plan.
        actions.append(RepairAction(
            "verify", verifier="ui_rendered+health_check_passed"
        ))

    plan = RepairPlan(
        application=app_id,
        application_version=version,
        diagnosis=diagnosis,
        actions=tuple(actions),
        requires_approval=any(
            action.kind in _MUTATING_REPAIR_KINDS for action in actions
        ),
        reversible=all(action.kind != "backup_move" or action.destination for action in actions),
    )
    validate_repair_plan(plan)
    return plan


def validate_repair_plan(plan: RepairPlan) -> None:
    """Reject every operation outside the deliberately tiny repair vocabulary."""
    if not isinstance(plan, RepairPlan):
        raise TypeError("Repair plan has an unsupported type")
    if _SAFE_IDENTIFIER.fullmatch(plan.application) is None:
        raise ValueError("Repair plan application id is invalid")
    if plan.diagnosis.category not in _DIAGNOSIS_CATEGORIES:
        raise ValueError("Repair plan diagnosis category is invalid")
    if not plan.actions or len(plan.actions) > _MAX_CACHE_PATHS + 2:
        raise ValueError("Repair plan action count is invalid")
    if not plan.reversible:
        raise ValueError("Application repair plans must remain reversible")

    for action in plan.actions:
        if action.kind not in _ALLOWED_REPAIR_KINDS:
            raise ValueError(f"Unsupported or unsafe repair action: {action.kind}")
        if action.kind == "backup_move":
            source = _plain_relative_path(action.source, "Repair source")
            destination = _plain_relative_path(
                action.destination, "Repair destination"
            )
            if source == destination:
                raise ValueError("Repair source and destination must differ")
            if not action.verifier:
                raise ValueError("Backup moves require an exact verifier")
        elif action.source is not None or action.destination is not None:
            raise ValueError("Only backup moves may name filesystem paths")
        if not action.verifier:
            raise ValueError("Every repair action requires outcome verification")

    actual_approval = any(
        action.kind in _MUTATING_REPAIR_KINDS for action in plan.actions
    )
    if plan.requires_approval is not actual_approval:
        raise ValueError("Repair approval flag does not match the action authority")


def approval_required(plan: RepairPlan) -> bool:
    validate_repair_plan(plan)
    return any(action.kind in _MUTATING_REPAIR_KINDS for action in plan.actions)


def complete_repair(
    plan: RepairPlan,
    evidence: Mapping[str, Any],
) -> RepairOutcome:
    """Resolve a plan from allowlisted evidence only; never infer visual success."""
    validate_repair_plan(plan)
    if not isinstance(evidence, Mapping):
        raise TypeError("Repair completion evidence must be an object")
    if approval_required(plan) and evidence.get("approval_authorized") is not True:
        raise PermissionError("Exact operator approval is required before app repair")

    verification: dict[str, Any] = {
        key: evidence.get(key) is True for key in _VERIFICATION_KEYS
    }
    observed_at = _parse_time(evidence.get("observed_at"))
    verification["observed_at"] = _iso(observed_at)

    completed: list[str] = []
    category_requirements = {
        "connectivity": ("network_reachable",),
        "authentication": ("authentication_succeeded",),
        "process": ("process_healthy",),
        "update": ("application_updated", "process_healthy"),
        "render_cache": (),
        "unknown": (),
    }[plan.diagnosis.category]
    # Unknown means the cause is unresolved. Generic healthy-looking UI
    # evidence cannot turn an unknown diagnosis into a verified repair.
    category_verified = plan.diagnosis.category != "unknown" and all(
        evidence.get(key) is True for key in category_requirements
    )
    for action in plan.actions:
        if action.kind == "backup_move":
            if verification["backup_created"] and verification["source_moved"]:
                completed.append(action.kind)
        elif action.kind == "restart":
            if verification["restart_observed"]:
                completed.append(action.kind)
        elif action.kind == "verify":
            if (
                verification["ui_rendered"]
                and verification["health_check_passed"]
                and category_verified
            ):
                completed.append(action.kind)

    status = "verified" if len(completed) == len(plan.actions) else "incomplete"
    rollback_available = bool(verification["backup_created"])
    outcome = RepairOutcome(
        application=plan.application,
        application_version=plan.application_version,
        diagnosis_category=plan.diagnosis.category,
        status=status,
        completed_actions=tuple(completed),
        verification=verification,
        rollback_available=rollback_available,
        receipt_sha256="",
    )
    return RepairOutcome(
        **{
            **outcome.__dict__,
            "receipt_sha256": _stable_sha256(_outcome_receipt_material(outcome)),
        }
    )


def build_verified_lesson(
    outcome: RepairOutcome,
    *,
    application_version: str,
    now: datetime | None = None,
) -> AppRepairLesson:
    """Build an advisory, exact-version lesson only from a verified outcome."""
    if outcome.status != "verified":
        raise ValueError("Only verified application repairs may become lessons")
    if (
        _SAFE_IDENTIFIER.fullmatch(outcome.application) is None
        or not outcome.application_version
        or outcome.diagnosis_category not in _DIAGNOSIS_CATEGORIES - {"unknown"}
        or any(kind not in _ALLOWED_REPAIR_KINDS for kind in outcome.completed_actions)
    ):
        raise ValueError("Verified repair outcome identity is invalid")
    if not re.fullmatch(r"[a-f0-9]{64}", outcome.receipt_sha256):
        raise ValueError("Verified repair receipt is malformed")
    if _stable_sha256(_outcome_receipt_material(outcome)) != outcome.receipt_sha256:
        raise ValueError("Verified repair receipt does not match its outcome")
    if not (
        outcome.verification.get("ui_rendered") is True
        and outcome.verification.get("health_check_passed") is True
        and "verify" in outcome.completed_actions
    ):
        raise ValueError("Verified repair lacks independent health evidence")
    required_category_evidence = {
        "connectivity": ("network_reachable",),
        "authentication": ("authentication_succeeded",),
        "process": ("process_healthy",),
        "update": ("application_updated", "process_healthy"),
        "render_cache": (),
    }[outcome.diagnosis_category]
    if not all(
        outcome.verification.get(key) is True
        for key in required_category_evidence
    ):
        raise ValueError("Verified repair lacks category-specific evidence")
    if outcome.diagnosis_category == "render_cache" and not {
        "backup_move", "restart", "verify",
    }.issubset(outcome.completed_actions):
        raise ValueError("Verified render repair lacks its complete action receipt")
    version = str(application_version or "").strip()
    if version != outcome.application_version:
        raise ValueError("Lesson version must match the verified application version")
    current = _utc(now or datetime.now(timezone.utc))
    observed_at = _strict_time(
        outcome.verification.get("observed_at"),
        "Repair observation time",
    )
    if observed_at > current:
        raise ValueError("Repair observation time may not be in the future")
    valid_until = observed_at + _LESSON_TTL
    if current > valid_until:
        raise ValueError("Verified repair outcome is already too old to learn from")
    verification_kinds = tuple(
        key for key in _VERIFICATION_KEYS if outcome.verification.get(key) is True
    )
    return AppRepairLesson(
        application=outcome.application,
        application_version=version,
        diagnosis_category=outcome.diagnosis_category,
        repair_kinds=outcome.completed_actions,
        verification_kinds=verification_kinds,
        observed_at=observed_at,
        valid_until=valid_until,
        outcome_sha256=outcome.receipt_sha256,
        contradicted_by=(),
        advisory_only=True,
    )


def lesson_is_applicable(
    lesson: AppRepairLesson,
    application: str,
    application_version: str | None,
    *,
    now: datetime | None = None,
) -> bool:
    """Return whether a verified advisory still matches this exact app build."""
    if not isinstance(lesson, AppRepairLesson):
        return False
    current = _utc(now or datetime.now(timezone.utc))
    return bool(
        lesson.advisory_only is True
        and not lesson.contradicted_by
        and str(application or "").strip().casefold() == lesson.application.casefold()
        and application_version is not None
        and str(application_version).strip() == lesson.application_version
        and current <= _utc(lesson.valid_until)
        and current >= _utc(lesson.observed_at)
        and re.fullmatch(r"[a-f0-9]{64}", lesson.outcome_sha256) is not None
    )
