from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import stat
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


MANIFEST_SCHEMA = "jarvis.long-horizon.workflow-manifest.v1"
EVIDENCE_SCHEMA = "jarvis.long-horizon.evidence.v1"
CHECKPOINT_SCHEMA = "jarvis.long-horizon.checkpoint.v1"
MUTATION_RECEIPT_SCHEMA = "jarvis.long-horizon.mutation-receipt.v1"
VERIFICATION_SCHEMA = "jarvis.long-horizon.final-verification.v1"

STAGE_TYPES = frozenset(
    {"inspect", "plan", "research", "implement", "mutate", "verify", "reconcile", "finalize"}
)
MUTATION_KINDS = frozenset({"none", "reversible", "irreversible"})
PLAN_STATUSES = frozenset({"active", "paused", "cancelled", "failed", "quarantined", "complete"})
STAGE_STATUSES = frozenset(
    {"pending", "claimed", "awaiting_reconciliation", "complete", "failed", "cancelled", "quarantined"}
)
MUTATION_OUTCOMES = frozenset({"applied", "not_applied", "uncertain"})
MUTATION_RECONCILIATIONS = frozenset({"applied", "not_applied", "uncertain"})
_STAGE_ID = re.compile(r"[a-z][a-z0-9_-]{0,63}")
_IDENTITY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,127}")
_SHA256 = re.compile(r"[0-9a-f]{64}")

MAX_STAGES = 64
MAX_LEASE_SECONDS = 3600
MAX_BUDGETS = {
    "elapsed_seconds": 1_209_600,
    "tool_calls": 10_000,
    "model_calls": 2_000,
    "prompt_tokens": 20_000_000,
    "completion_tokens": 5_000_000,
    "retries": 1_000,
}
USAGE_KEYS = ("elapsed_seconds", "tool_calls", "model_calls", "prompt_tokens", "completion_tokens")


class LongHorizonError(RuntimeError):
    pass


class LongHorizonValidationError(ValueError):
    pass


class LongHorizonStateError(LongHorizonError):
    pass


class LongHorizonBudgetError(LongHorizonError):
    pass


class LongHorizonIntegrityError(LongHorizonError):
    def __init__(self, message: str, *, plan_id: int | None = None, reason: str = "integrity_failure") -> None:
        super().__init__(message)
        self.plan_id = plan_id
        self.reason = reason


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None = None) -> str:
    current = value or _utc_now()
    if current.tzinfo is None:
        raise LongHorizonValidationError("timestamp must be timezone-aware")
    return current.astimezone(timezone.utc).isoformat()


def _parse_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise LongHorizonIntegrityError("stored timestamp is invalid") from exc
    if parsed.tzinfo is None:
        raise LongHorizonIntegrityError("stored timestamp is not timezone-aware")
    return parsed.astimezone(timezone.utc)


def _canonical(value: Mapping[str, Any] | Sequence[Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _strict_json_loads(value: str, label: str) -> Any:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in items:
            if key in result:
                raise LongHorizonValidationError(f"{label} contains duplicate keys")
            result[key] = item
        return result

    def constant(_value: str) -> Any:
        raise LongHorizonValidationError(f"{label} contains a non-finite number")

    try:
        return json.loads(value, object_pairs_hook=pairs, parse_constant=constant)
    except (json.JSONDecodeError, LongHorizonValidationError) as exc:
        if isinstance(exc, LongHorizonValidationError):
            raise
        raise LongHorizonValidationError(f"{label} JSON is invalid") from exc


def _digest(value: Mapping[str, Any] | Sequence[Any]) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _require_sha(value: Any, field: str) -> str:
    text = str(value).strip().casefold()
    if not _SHA256.fullmatch(text):
        raise LongHorizonValidationError(f"{field} must be one lowercase SHA-256 digest")
    return text


def _require_signature(value: Any, field: str) -> str:
    text = str(value).strip().casefold()
    if not re.fullmatch(r"[0-9a-f]{128}", text):
        raise LongHorizonValidationError(f"{field} must be one Ed25519 signature")
    return text


def _require_id(value: Any, field: str) -> int:
    if type(value) is not int:
        raise LongHorizonValidationError(f"{field} must be a positive integer")
    if value <= 0 or value > 9_223_372_036_854_775_807:
        raise LongHorizonValidationError(f"{field} must be a positive integer")
    return value


def _require_int(value: Any, field: str, *, minimum: int, maximum: int) -> int:
    if type(value) is not int or value < minimum or value > maximum:
        raise LongHorizonValidationError(
            f"{field} must be an integer between {minimum} and {maximum}"
        )
    return value


def _require_identity(value: Any, field: str) -> str:
    text = str(value).strip()
    if not _IDENTITY.fullmatch(text):
        raise LongHorizonValidationError(f"{field} must be bounded non-secret metadata")
    return text


def _closed_mapping(value: Mapping[str, Any], keys: frozenset[str], label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise LongHorizonValidationError(f"{label} must be an object")
    material = dict(value)
    unknown = set(material) - keys
    missing = keys - set(material)
    if unknown or missing:
        raise LongHorizonValidationError(
            f"{label} has invalid fields (missing={sorted(missing)}, unknown={sorted(unknown)})"
        )
    return material


@dataclass(frozen=True)
class WorkflowBudget:
    elapsed_seconds: int
    tool_calls: int
    model_calls: int
    prompt_tokens: int
    completion_tokens: int
    retries: int

    def __post_init__(self) -> None:
        for key, maximum in MAX_BUDGETS.items():
            value = getattr(self, key)
            if isinstance(value, bool) or not isinstance(value, int):
                raise LongHorizonValidationError(f"budget {key} must be an integer")
            minimum = 0 if key == "retries" else 1
            if value < minimum or value > maximum:
                raise LongHorizonValidationError(
                    f"budget {key} must be between {minimum} and {maximum}"
                )

    def to_payload(self) -> dict[str, int]:
        return {key: int(getattr(self, key)) for key in (*USAGE_KEYS, "retries")}

    @classmethod
    def from_value(cls, value: WorkflowBudget | Mapping[str, Any]) -> WorkflowBudget:
        if isinstance(value, cls):
            return value
        material = _closed_mapping(
            value,
            frozenset({*USAGE_KEYS, "retries"}),
            "workflow budget",
        )
        values = {
            key: _require_int(
                material[key], f"budget {key}",
                minimum=0 if key == "retries" else 1,
                maximum=MAX_BUDGETS[key],
            )
            for key in (*USAGE_KEYS, "retries")
        }
        return cls(**values)


@dataclass(frozen=True)
class WorkflowStageSpec:
    stage_id: str
    ordinal: int
    stage_type: str
    mutation_kind: str
    budget: WorkflowBudget

    def __post_init__(self) -> None:
        if not _STAGE_ID.fullmatch(str(self.stage_id)):
            raise LongHorizonValidationError("stage_id must be canonical bounded metadata")
        if isinstance(self.ordinal, bool) or not isinstance(self.ordinal, int) or self.ordinal <= 0:
            raise LongHorizonValidationError("stage ordinal must be a positive integer")
        if self.stage_type not in STAGE_TYPES:
            raise LongHorizonValidationError("stage_type is not in the closed stage vocabulary")
        if self.mutation_kind not in MUTATION_KINDS:
            raise LongHorizonValidationError("mutation_kind is not in the closed mutation vocabulary")
        if self.mutation_kind != "none" and self.stage_type not in {"implement", "mutate"}:
            raise LongHorizonValidationError("only implement/mutate stages may declare mutation")
        object.__setattr__(self, "budget", WorkflowBudget.from_value(self.budget))

    def to_payload(self) -> dict[str, Any]:
        return {
            "stage_id": self.stage_id,
            "ordinal": self.ordinal,
            "stage_type": self.stage_type,
            "mutation_kind": self.mutation_kind,
            "budget": self.budget.to_payload(),
        }

    @classmethod
    def from_value(cls, value: WorkflowStageSpec | Mapping[str, Any]) -> WorkflowStageSpec:
        if isinstance(value, cls):
            return value
        material = _closed_mapping(
            value,
            frozenset({"stage_id", "ordinal", "stage_type", "mutation_kind", "budget"}),
            "workflow stage",
        )
        return cls(
            stage_id=str(material["stage_id"]),
            ordinal=_require_int(material["ordinal"], "stage ordinal", minimum=1, maximum=MAX_STAGES),
            stage_type=str(material["stage_type"]),
            mutation_kind=str(material["mutation_kind"]),
            budget=WorkflowBudget.from_value(material["budget"]),
        )


@dataclass(frozen=True)
class WorkflowManifest:
    project_id: int
    conversation_id: int
    task_id: int
    goal_sha256: str
    contract_sha256: str
    constraints_sha256: str
    approval_scope_sha256: str
    artifact_set_sha256: str
    budget: WorkflowBudget
    stages: tuple[WorkflowStageSpec, ...] = ()
    schema: str = MANIFEST_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != MANIFEST_SCHEMA:
            raise LongHorizonValidationError("unsupported workflow manifest schema")
        for field in ("project_id", "conversation_id", "task_id"):
            object.__setattr__(self, field, _require_id(getattr(self, field), field))
        for field in (
            "goal_sha256", "contract_sha256", "constraints_sha256",
            "approval_scope_sha256", "artifact_set_sha256",
        ):
            object.__setattr__(self, field, _require_sha(getattr(self, field), field))
        object.__setattr__(self, "budget", WorkflowBudget.from_value(self.budget))
        stages = tuple(WorkflowStageSpec.from_value(item) for item in self.stages)
        if stages:
            _validate_stages(stages)
        object.__setattr__(self, "stages", stages)

    def to_payload(self, *, include_stages: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema": self.schema,
            "project_id": self.project_id,
            "conversation_id": self.conversation_id,
            "task_id": self.task_id,
            "goal_sha256": self.goal_sha256,
            "contract_sha256": self.contract_sha256,
            "constraints_sha256": self.constraints_sha256,
            "approval_scope_sha256": self.approval_scope_sha256,
            "artifact_set_sha256": self.artifact_set_sha256,
            "budget": self.budget.to_payload(),
        }
        if include_stages:
            payload["stages"] = [stage.to_payload() for stage in self.stages]
        return payload

    @classmethod
    def from_value(
        cls,
        value: WorkflowManifest | Mapping[str, Any],
        stages: Sequence[WorkflowStageSpec | Mapping[str, Any]] | None = None,
    ) -> WorkflowManifest:
        if isinstance(value, cls):
            if stages is not None:
                if value.stages:
                    raise LongHorizonValidationError("stages may be supplied only once")
                return cls(**{**value.to_payload(include_stages=False), "stages": tuple(stages)})
            return value
        allowed = frozenset(
            {
                "schema", "project_id", "conversation_id", "task_id", "goal_sha256",
                "contract_sha256", "constraints_sha256", "approval_scope_sha256",
                "artifact_set_sha256", "budget", "stages",
            }
        )
        material = dict(value)
        unknown = set(material) - allowed
        required = allowed - {"stages"}
        missing = required - set(material)
        if unknown or missing:
            raise LongHorizonValidationError(
                f"workflow manifest has invalid fields (missing={sorted(missing)}, unknown={sorted(unknown)})"
            )
        if stages is not None and "stages" in material:
            raise LongHorizonValidationError("stages may be supplied only once")
        stage_values = stages if stages is not None else material.get("stages", ())
        return cls(
            schema=str(material["schema"]),
            project_id=material["project_id"],
            conversation_id=material["conversation_id"],
            task_id=material["task_id"],
            goal_sha256=material["goal_sha256"],
            contract_sha256=material["contract_sha256"],
            constraints_sha256=material["constraints_sha256"],
            approval_scope_sha256=material["approval_scope_sha256"],
            artifact_set_sha256=material["artifact_set_sha256"],
            budget=WorkflowBudget.from_value(material["budget"]),
            stages=tuple(WorkflowStageSpec.from_value(item) for item in stage_values),
        )


def parse_manifest_json(value: str) -> WorkflowManifest:
    try:
        payload = _strict_json_loads(str(value), "manifest")
    except json.JSONDecodeError as exc:
        raise LongHorizonValidationError("manifest JSON is invalid") from exc
    return WorkflowManifest.from_value(payload)


def _validate_stages(stages: Sequence[WorkflowStageSpec]) -> None:
    if len(stages) < 5 or len(stages) > MAX_STAGES:
        raise LongHorizonValidationError(f"workflow must contain 5-{MAX_STAGES} stages")
    if [stage.ordinal for stage in stages] != list(range(1, len(stages) + 1)):
        raise LongHorizonValidationError("workflow stages must be ordered contiguously from 1")
    if len({stage.stage_id for stage in stages}) != len(stages):
        raise LongHorizonValidationError("stage_id values must be unique")
    if stages[-1].stage_type not in {"verify", "finalize"}:
        raise LongHorizonValidationError("the final stage must be verify or finalize")


def migrate_long_horizon_v40(db: sqlite3.Connection) -> None:
    """Create only the Phase 5 tables; Memory owns user_version and transactionality."""
    statements = (
        "DROP TABLE IF EXISTS long_horizon_final_verifications_v40_partial",
        "DROP TABLE IF EXISTS long_horizon_mutation_receipts_v40_partial",
        "DROP TABLE IF EXISTS long_horizon_checkpoints_v40_partial",
        "DROP TABLE IF EXISTS long_horizon_stages_v40_partial",
        "DROP TABLE IF EXISTS long_horizon_plans_v40_partial",
        """CREATE TABLE IF NOT EXISTS long_horizon_plans (
            id INTEGER PRIMARY KEY,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            clock_floor_at TEXT NOT NULL,
            project_id INTEGER NOT NULL,
            conversation_id INTEGER NOT NULL,
            task_id INTEGER NOT NULL,
            status TEXT NOT NULL CHECK(status IN
                ('active','paused','cancelled','failed','quarantined','complete')),
            manifest_json TEXT NOT NULL,
            manifest_sha256 TEXT NOT NULL UNIQUE CHECK(length(manifest_sha256)=64),
            manifest_mac_sha256 TEXT NOT NULL CHECK(length(manifest_mac_sha256)=64),
            stage_count INTEGER NOT NULL CHECK(stage_count BETWEEN 5 AND 64),
            next_stage_ordinal INTEGER NOT NULL DEFAULT 1,
            checkpoint_head_sha256 TEXT,
            retry_head_sha256 TEXT,
            usage_head_sha256 TEXT,
            final_verification_id INTEGER,
            quarantine_reason TEXT,
            pause_reason_sha256 TEXT,
            cancelled_reason_sha256 TEXT,
            used_elapsed_seconds INTEGER NOT NULL DEFAULT 0 CHECK(used_elapsed_seconds>=0),
            used_tool_calls INTEGER NOT NULL DEFAULT 0 CHECK(used_tool_calls>=0),
            used_model_calls INTEGER NOT NULL DEFAULT 0 CHECK(used_model_calls>=0),
            used_prompt_tokens INTEGER NOT NULL DEFAULT 0 CHECK(used_prompt_tokens>=0),
            used_completion_tokens INTEGER NOT NULL DEFAULT 0 CHECK(used_completion_tokens>=0),
            used_retries INTEGER NOT NULL DEFAULT 0 CHECK(used_retries>=0),
            state_mac_sha256 TEXT,
            FOREIGN KEY(project_id) REFERENCES agent_projects(id),
            FOREIGN KEY(conversation_id) REFERENCES conversations(id),
            FOREIGN KEY(task_id) REFERENCES tasks(id)
        )""",
        """CREATE TABLE IF NOT EXISTS long_horizon_stages (
            id INTEGER PRIMARY KEY,
            plan_id INTEGER NOT NULL,
            ordinal INTEGER NOT NULL,
            stage_key TEXT NOT NULL,
            stage_type TEXT NOT NULL,
            mutation_kind TEXT NOT NULL,
            stage_json TEXT NOT NULL,
            stage_sha256 TEXT NOT NULL CHECK(length(stage_sha256)=64),
            stage_mac_sha256 TEXT NOT NULL CHECK(length(stage_mac_sha256)=64),
            status TEXT NOT NULL CHECK(status IN
                ('pending','claimed','awaiting_reconciliation','complete','failed','cancelled','quarantined')),
            claim_owner TEXT,
            lease_token_sha256 TEXT,
            lease_expires_at TEXT,
            attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count>=0),
            idempotency_key TEXT NOT NULL UNIQUE CHECK(length(idempotency_key)=64),
            effect_key TEXT NOT NULL UNIQUE CHECK(length(effect_key)=64),
            executor_id TEXT,
            outcome_sha256 TEXT,
            artifact_sha256 TEXT,
            checkpoint_id INTEGER,
            active_reservation_id INTEGER,
            authorization_expires_at TEXT,
            authorization_consumed_at TEXT,
            mutation_state TEXT NOT NULL DEFAULT 'none',
            used_elapsed_seconds INTEGER NOT NULL DEFAULT 0 CHECK(used_elapsed_seconds>=0),
            used_tool_calls INTEGER NOT NULL DEFAULT 0 CHECK(used_tool_calls>=0),
            used_model_calls INTEGER NOT NULL DEFAULT 0 CHECK(used_model_calls>=0),
            used_prompt_tokens INTEGER NOT NULL DEFAULT 0 CHECK(used_prompt_tokens>=0),
            used_completion_tokens INTEGER NOT NULL DEFAULT 0 CHECK(used_completion_tokens>=0),
            state_mac_sha256 TEXT,
            UNIQUE(plan_id, ordinal),
            UNIQUE(plan_id, stage_key),
            FOREIGN KEY(plan_id) REFERENCES long_horizon_plans(id) ON DELETE CASCADE
        )""",
        """CREATE TABLE IF NOT EXISTS long_horizon_checkpoints (
            id INTEGER PRIMARY KEY,
            plan_id INTEGER NOT NULL,
            stage_id INTEGER NOT NULL,
            sequence INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            previous_sha256 TEXT,
            receipt_json TEXT NOT NULL,
            receipt_sha256 TEXT NOT NULL UNIQUE CHECK(length(receipt_sha256)=64),
            receipt_mac_sha256 TEXT NOT NULL CHECK(length(receipt_mac_sha256)=64),
            UNIQUE(plan_id, sequence),
            UNIQUE(plan_id, stage_id),
            FOREIGN KEY(plan_id) REFERENCES long_horizon_plans(id) ON DELETE CASCADE,
            FOREIGN KEY(stage_id) REFERENCES long_horizon_stages(id) ON DELETE CASCADE
        )""",
        """CREATE TABLE IF NOT EXISTS long_horizon_mutation_receipts (
            id INTEGER PRIMARY KEY,
            plan_id INTEGER NOT NULL,
            stage_id INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            generation INTEGER NOT NULL CHECK(generation>=1),
            reconciliation_round INTEGER NOT NULL DEFAULT 0 CHECK(reconciliation_round>=0),
            event_type TEXT NOT NULL CHECK(event_type IN ('intent','authorization','effect_permit','result','reconciliation')),
            outcome TEXT,
            effect_key TEXT NOT NULL CHECK(length(effect_key)=64),
            actor_id TEXT NOT NULL,
            evidence_sha256 TEXT,
            authority_id TEXT,
            runtime_sha256 TEXT,
            signature_sha256 TEXT,
            previous_sha256 TEXT,
            receipt_json TEXT NOT NULL,
            receipt_sha256 TEXT NOT NULL UNIQUE CHECK(length(receipt_sha256)=64),
            receipt_mac_sha256 TEXT NOT NULL CHECK(length(receipt_mac_sha256)=64),
            UNIQUE(stage_id, generation, event_type, reconciliation_round),
            FOREIGN KEY(plan_id) REFERENCES long_horizon_plans(id) ON DELETE CASCADE,
            FOREIGN KEY(stage_id) REFERENCES long_horizon_stages(id) ON DELETE CASCADE
        )""",
        """CREATE TABLE IF NOT EXISTS long_horizon_retry_receipts (
            id INTEGER PRIMARY KEY,
            plan_id INTEGER NOT NULL,
            stage_id INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            attempt_number INTEGER NOT NULL CHECK(attempt_number>=1),
            reason TEXT NOT NULL CHECK(reason IN
                ('lease_expired','pause_reclaim','mutation_not_applied','reconciliation_not_applied')),
            previous_sha256 TEXT,
            receipt_json TEXT NOT NULL,
            receipt_sha256 TEXT NOT NULL UNIQUE CHECK(length(receipt_sha256)=64),
            receipt_mac_sha256 TEXT NOT NULL CHECK(length(receipt_mac_sha256)=64),
            UNIQUE(stage_id, attempt_number),
            FOREIGN KEY(plan_id) REFERENCES long_horizon_plans(id) ON DELETE CASCADE,
            FOREIGN KEY(stage_id) REFERENCES long_horizon_stages(id) ON DELETE CASCADE
        )""",
        """CREATE TABLE IF NOT EXISTS long_horizon_usage_reservations (
            id INTEGER PRIMARY KEY,
            plan_id INTEGER NOT NULL,
            stage_id INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            attempt_number INTEGER NOT NULL CHECK(attempt_number>=1),
            usage_json TEXT NOT NULL,
            previous_sha256 TEXT,
            receipt_json TEXT NOT NULL,
            receipt_sha256 TEXT NOT NULL UNIQUE CHECK(length(receipt_sha256)=64),
            receipt_mac_sha256 TEXT NOT NULL CHECK(length(receipt_mac_sha256)=64),
            UNIQUE(stage_id, attempt_number),
            FOREIGN KEY(plan_id) REFERENCES long_horizon_plans(id) ON DELETE CASCADE,
            FOREIGN KEY(stage_id) REFERENCES long_horizon_stages(id) ON DELETE CASCADE
        )""",
        """CREATE TABLE IF NOT EXISTS long_horizon_final_verifications (
            id INTEGER PRIMARY KEY,
            plan_id INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            verifier_id TEXT NOT NULL,
            authority_id TEXT NOT NULL,
            verifier_runtime_sha256 TEXT NOT NULL CHECK(length(verifier_runtime_sha256)=64),
            passed INTEGER NOT NULL CHECK(passed IN (0,1)),
            evidence_sha256 TEXT NOT NULL CHECK(length(evidence_sha256)=64),
            signature_sha256 TEXT NOT NULL CHECK(length(signature_sha256)=128),
            verification_sha256 TEXT NOT NULL CHECK(length(verification_sha256)=64),
            checkpoint_head_sha256 TEXT NOT NULL CHECK(length(checkpoint_head_sha256)=64),
            receipt_json TEXT NOT NULL,
            receipt_sha256 TEXT NOT NULL UNIQUE CHECK(length(receipt_sha256)=64),
            receipt_mac_sha256 TEXT NOT NULL CHECK(length(receipt_mac_sha256)=64),
            UNIQUE(plan_id, verifier_id, verification_sha256),
            FOREIGN KEY(plan_id) REFERENCES long_horizon_plans(id) ON DELETE CASCADE
        )""",
        "CREATE INDEX IF NOT EXISTS idx_long_horizon_plan_status ON long_horizon_plans(status, project_id, id)",
        "CREATE INDEX IF NOT EXISTS idx_long_horizon_stage_claim ON long_horizon_stages(plan_id, status, ordinal)",
        "CREATE INDEX IF NOT EXISTS idx_long_horizon_lease ON long_horizon_stages(status, lease_expires_at)",
        "CREATE INDEX IF NOT EXISTS idx_long_horizon_mutation_stage ON long_horizon_mutation_receipts(stage_id, id)",
        "CREATE INDEX IF NOT EXISTS idx_long_horizon_retry_plan ON long_horizon_retry_receipts(plan_id, id)",
        "CREATE INDEX IF NOT EXISTS idx_long_horizon_usage_plan ON long_horizon_usage_reservations(plan_id, id)",
    )
    for statement in statements:
        db.execute(statement)


class LongHorizonStore:
    """Fail-closed durable state machine for bounded, long-horizon work."""

    def __init__(
        self,
        memory_or_path: Any,
        *,
        project_id: int,
        worker_id: str | None = None,
        busy_timeout_ms: int = 30_000,
        integrity_key: bytes | None = None,
        authorities: Mapping[str, Mapping[str, Any]] | None = None,
        approval_validator: Callable[[str, Mapping[str, Any]], Mapping[str, Any]] | None = None,
    ) -> None:
        self._owns_connection = False
        self.project_id = _require_id(project_id, "project_id")
        source_path: Path | None = None
        if hasattr(memory_or_path, "db"):
            self.db = memory_or_path.db
            raw_path = getattr(memory_or_path, "path", None)
            if raw_path is not None and str(raw_path) != ":memory:":
                source_path = Path(raw_path)
        elif isinstance(memory_or_path, sqlite3.Connection):
            self.db = memory_or_path
        else:
            source_path = Path(memory_or_path)
            from .memory import SCHEMA_VERSION
            from .sqlite_preflight import inspection_connection, validate_database_path
            try:
                path_exists = validate_database_path(source_path)
                if path_exists:
                    with inspection_connection(source_path) as preflight:
                        preflight_version = int(
                            preflight.execute("PRAGMA user_version").fetchone()[0]
                        )
            except (OSError, sqlite3.DatabaseError, TypeError, ValueError) as exc:
                raise LongHorizonStateError(
                    "Database could not be inspected safely"
                ) from exc
            if path_exists and preflight_version > SCHEMA_VERSION:
                raise LongHorizonStateError(
                    f"Database schema version {preflight_version} is newer than "
                    f"supported version {SCHEMA_VERSION}"
                )
            self.db = sqlite3.connect(
                str(source_path),
                timeout=max(0.1, min(int(busy_timeout_ms), 120_000) / 1000),
                isolation_level=None,
            )
            self.db.row_factory = sqlite3.Row
            self.db.execute("PRAGMA foreign_keys=ON")
            self.db.execute(f"PRAGMA busy_timeout={max(100, min(int(busy_timeout_ms), 120_000))}")
            self._owns_connection = True
        if self.db.row_factory is None:
            self.db.row_factory = sqlite3.Row
        self.worker_id = _require_identity(worker_id or f"worker:{secrets.token_hex(12)}", "worker_id")
        # Memory owns the database schema. A direct-path restart worker must
        # refuse an unknown future database before creating the integrity
        # sidecar or running any Phase 5 DDL against state it cannot understand.
        from .memory import SCHEMA_VERSION

        self._authorities = self._validated_authorities(authorities or {})
        self._approval_validator = approval_validator
        if not self.db.in_transaction:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                existing_version = int(
                    self.db.execute("PRAGMA user_version").fetchone()[0]
                )
                if existing_version > SCHEMA_VERSION:
                    raise LongHorizonStateError(
                        f"Database schema version {existing_version} is newer than "
                        f"supported version {SCHEMA_VERSION}"
                    )
                # Create/read the sidecar only after the locked authority check.
                self._integrity_key = self._load_integrity_key(source_path, integrity_key)
                self._verify_existing_state_macs_locked()
                migrate_long_horizon_v40(self.db)
                self._seal_all_states_locked()
                self.db.commit()
            except BaseException:
                if self.db.in_transaction:
                    self.db.rollback()
                if self._owns_connection:
                    self.db.close()
                    self._owns_connection = False
                raise
        else:
            self._integrity_key = self._load_integrity_key(source_path, integrity_key)

    @staticmethod
    def _load_integrity_key(path: Path | None, supplied: bytes | None) -> bytes:
        if supplied is not None:
            if not isinstance(supplied, bytes) or len(supplied) != 32:
                raise LongHorizonValidationError("integrity_key must be exactly 32 bytes")
            return supplied
        if path is None:
            raise LongHorizonValidationError(
                "in-memory long-horizon stores require an explicit 32-byte integrity_key"
            )
        key_path = Path(str(path) + ".long-horizon.key")
        if key_path.is_symlink():
            raise LongHorizonIntegrityError("external long-horizon key path may not be a symlink")
        if key_path.exists() and not stat.S_ISREG(key_path.stat().st_mode):
            raise LongHorizonIntegrityError("external long-horizon integrity key is not a regular file")
        try:
            raw = key_path.read_bytes()
        except FileNotFoundError:
            raw = secrets.token_bytes(32)
            try:
                descriptor = os.open(str(key_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
            except FileExistsError:
                if key_path.is_symlink():
                    raise LongHorizonIntegrityError("external long-horizon key path may not be a symlink")
                raw = key_path.read_bytes()
        except OSError as exc:
            raise LongHorizonIntegrityError("external long-horizon integrity key is unreadable") from exc
        try:
            metadata = key_path.stat()
        except OSError as exc:
            raise LongHorizonIntegrityError("external long-horizon integrity key is unavailable") from exc
        if not stat.S_ISREG(metadata.st_mode):
            raise LongHorizonIntegrityError("external long-horizon integrity key is not a regular file")
        if os.name != "nt" and metadata.st_mode & 0o077:
            raise LongHorizonIntegrityError("external long-horizon integrity key permissions are insecure")
        if len(raw) != 32:
            raise LongHorizonIntegrityError("external long-horizon integrity key is invalid")
        return raw

    @staticmethod
    def _validated_authorities(
        authorities: Mapping[str, Mapping[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for authority_id, raw in authorities.items():
            key = _require_identity(authority_id, "authority_id")
            material = _closed_mapping(
                raw,
                frozenset({"scope", "verifier_id", "runtime_sha256", "public_key"}),
                "authority",
            )
            scope = str(material["scope"])
            if scope not in {"final_verification", "mutation_reconciliation"}:
                raise LongHorizonValidationError("authority scope is invalid")
            public_key = material["public_key"]
            if not isinstance(public_key, bytes) or len(public_key) != 32:
                raise LongHorizonValidationError("authority public_key must be exactly 32 bytes")
            result[key] = {
                "scope": scope,
                "verifier_id": _require_identity(material["verifier_id"], "verifier_id"),
                "runtime_sha256": _require_sha(material["runtime_sha256"], "runtime_sha256"),
                "public_key": public_key,
            }
        return result

    def _mac(self, value: Mapping[str, Any] | Sequence[Any] | str) -> str:
        raw = value if isinstance(value, str) else _canonical(value)
        return hmac.new(self._integrity_key, raw.encode("utf-8"), hashlib.sha256).hexdigest()

    @staticmethod
    def _verify_authority_signature(public_key: bytes, challenge: Mapping[str, Any], signature: str) -> None:
        try:
            Ed25519PublicKey.from_public_bytes(public_key).verify(
                bytes.fromhex(signature), _canonical(challenge).encode("utf-8")
            )
        except (InvalidSignature, ValueError) as exc:
            raise LongHorizonStateError("authority signature is invalid") from exc

    def _authority_locked(self, authority_id: str, scope: str) -> dict[str, Any]:
        key = _require_identity(authority_id, "authority_id")
        authority = self._authorities.get(key)
        if authority is None or authority["scope"] != scope:
            raise LongHorizonStateError(f"no pinned {scope} authority is configured")
        return authority

    def _approval_locked(self, phase: str, context: Mapping[str, Any]) -> tuple[str, str]:
        if self._approval_validator is None:
            raise LongHorizonStateError("live mutation approval validator is unavailable")
        raw = self._approval_validator(phase, dict(context))
        material = _closed_mapping(raw, frozenset({"approved", "receipt_sha256"}), "approval receipt")
        if material["approved"] is not True:
            raise LongHorizonStateError(f"mutation {phase} approval was not granted")
        return phase, _require_sha(material["receipt_sha256"], "approval receipt_sha256")

    def close(self) -> None:
        if self._owns_connection:
            self.db.close()

    def __enter__(self) -> LongHorizonStore:
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()

    @contextmanager
    def _transaction(self):
        if self.db.in_transaction:
            raise LongHorizonStateError("long-horizon operation cannot nest a database transaction")
        self.db.execute("BEGIN IMMEDIATE")
        try:
            self._verify_existing_state_macs_locked()
            yield
        except LongHorizonIntegrityError:
            # Never MAC an integrity-failed row: doing so would launder the
            # attack on the next read. The invalid state remains permanently
            # unreadable until an operator restores it from trusted evidence.
            self.db.rollback()
            raise
        except LongHorizonBudgetError:
            # Retry exhaustion intentionally writes terminal failed state before
            # raising. Other budget checks are read-only, so committing here is
            # both safe and necessary for durable fail-closed behavior.
            self._seal_all_states_locked()
            self.db.commit()
            raise
        except BaseException:
            self.db.rollback()
            raise
        else:
            self._seal_all_states_locked()
            self.db.commit()

    def _verify_existing_state_macs_locked(self) -> None:
        try:
            plans = self.db.execute(
                "SELECT * FROM long_horizon_plans WHERE project_id=?", (self.project_id,)
            ).fetchall()
        except sqlite3.OperationalError:
            return
        for plan in plans:
            if not plan["state_mac_sha256"] or not secrets.compare_digest(
                str(plan["state_mac_sha256"]),
                self._mac(self._state_material(plan, frozenset({"state_mac_sha256"}))),
            ):
                raise LongHorizonIntegrityError(
                    "plan state keyed integrity mismatch", plan_id=int(plan["id"]),
                    reason="plan_state_mac_invalid",
                )
            for stage in self.db.execute(
                "SELECT * FROM long_horizon_stages WHERE plan_id=?", (int(plan["id"]),)
            ).fetchall():
                if not stage["state_mac_sha256"] or not secrets.compare_digest(
                    str(stage["state_mac_sha256"]),
                    self._mac(self._state_material(stage, frozenset({"state_mac_sha256"}))),
                ):
                    raise LongHorizonIntegrityError(
                        "stage state keyed integrity mismatch", plan_id=int(plan["id"]),
                        reason="stage_state_mac_invalid",
                    )

    @staticmethod
    def _state_material(row: sqlite3.Row, excluded: frozenset[str]) -> dict[str, Any]:
        return {key: row[key] for key in row.keys() if key not in excluded}

    def _seal_all_states_locked(self, plan_id: int | None = None) -> None:
        plan_where = " WHERE id=? AND project_id=?" if plan_id is not None else " WHERE project_id=?"
        params: tuple[Any, ...] = (plan_id, self.project_id) if plan_id is not None else (self.project_id,)
        try:
            plans = self.db.execute(
                "SELECT * FROM long_horizon_plans" + plan_where, params
            ).fetchall()
        except sqlite3.OperationalError:
            return
        for row in plans:
            mac = self._mac(self._state_material(row, frozenset({"state_mac_sha256"})))
            self.db.execute(
                "UPDATE long_horizon_plans SET state_mac_sha256=? WHERE id=?",
                (mac, int(row["id"])),
            )
            stages = self.db.execute(
                "SELECT * FROM long_horizon_stages WHERE plan_id=?", (int(row["id"]),)
            ).fetchall()
            for stage in stages:
                stage_mac = self._mac(
                    self._state_material(stage, frozenset({"state_mac_sha256"}))
                )
                self.db.execute(
                    "UPDATE long_horizon_stages SET state_mac_sha256=? WHERE id=?",
                    (stage_mac, int(stage["id"])),
                )

    def create_plan(
        self,
        manifest: WorkflowManifest | Mapping[str, Any],
        stages: Sequence[WorkflowStageSpec | Mapping[str, Any]] | None = None,
    ) -> int:
        parsed = WorkflowManifest.from_value(manifest, stages)
        if parsed.project_id != self.project_id:
            raise LongHorizonValidationError("manifest is outside this project-scoped store")
        _validate_stages(parsed.stages)
        payload = parsed.to_payload()
        manifest_json = _canonical(payload)
        manifest_sha = hashlib.sha256(manifest_json.encode("utf-8")).hexdigest()
        stamp = _iso()
        with self._transaction():
            self._validate_binding_locked(parsed.project_id, parsed.conversation_id, parsed.task_id)
            existing = self.db.execute(
                "SELECT id, manifest_json FROM long_horizon_plans WHERE manifest_sha256=?",
                (manifest_sha,),
            ).fetchone()
            if existing is not None:
                if str(existing["manifest_json"]) != manifest_json:
                    raise LongHorizonIntegrityError("manifest digest collision", plan_id=int(existing["id"]))
                return int(existing["id"])
            cursor = self.db.execute(
                """INSERT INTO long_horizon_plans(
                       created_at, updated_at, project_id, conversation_id, task_id,
                       clock_floor_at, status, manifest_json, manifest_sha256, manifest_mac_sha256,
                       stage_count
                   ) VALUES (?,?,?,?,?,?,'active',?,?,?,?)""",
                (
                    stamp, stamp, parsed.project_id, parsed.conversation_id, parsed.task_id,
                    stamp, manifest_json, manifest_sha, self._mac(manifest_json), len(parsed.stages),
                ),
            )
            plan_id = int(cursor.lastrowid)
            for stage in parsed.stages:
                stage_payload = stage.to_payload()
                stage_json = _canonical(stage_payload)
                stage_sha = hashlib.sha256(stage_json.encode("utf-8")).hexdigest()
                stable = {"manifest_sha256": manifest_sha, "stage": stage_payload}
                idempotency_key = _digest({**stable, "kind": "stage"})
                effect_key = _digest({**stable, "kind": "effect"})
                self.db.execute(
                    """INSERT INTO long_horizon_stages(
                           plan_id, ordinal, stage_key, stage_type, mutation_kind,
                           stage_json, stage_sha256, stage_mac_sha256, status,
                           idempotency_key, effect_key
                       ) VALUES (?,?,?,?,?,?,?,?,'pending',?,?)""",
                    (
                        plan_id, stage.ordinal, stage.stage_id, stage.stage_type,
                        stage.mutation_kind, stage_json, stage_sha,
                        self._mac(stage_json), idempotency_key, effect_key,
                    ),
                )
            self._seal_all_states_locked(plan_id)
        return plan_id

    def list_plans(
        self,
        *,
        project_id: int | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        limit = _require_int(limit, "limit", minimum=1, maximum=200)
        clauses: list[str] = ["project_id=?"]
        params: list[Any] = [self.project_id]
        if project_id is not None and _require_id(project_id, "project_id") != self.project_id:
            raise LongHorizonValidationError("requested project is outside this scoped store")
        if status is not None:
            normalized = str(status).strip().casefold()
            if normalized not in PLAN_STATUSES:
                raise LongHorizonValidationError("unknown plan status")
            clauses.append("status=?")
            params.append(normalized)
        where = " WHERE " + " AND ".join(clauses)
        rows = self.db.execute(
            "SELECT id FROM long_horizon_plans" + where + " ORDER BY id DESC LIMIT ?",
            (*params, limit),
        ).fetchall()
        return [self.show_plan(int(row["id"])) for row in rows]

    def show_plan(self, plan_id: int) -> dict[str, Any]:
        normalized = _require_id(plan_id, "plan_id")
        with self._transaction():
            plan, manifest, stages = self._validate_plan_locked(normalized)
            return self._status_locked(plan, manifest, stages)

    def claim_next_stage(
        self,
        plan_id: int,
        *,
        worker_id: str | None = None,
        lease_seconds: int = 60,
    ) -> dict[str, Any] | None:
        normalized = _require_id(plan_id, "plan_id")
        owner = _require_identity(worker_id or self.worker_id, "worker_id")
        lease_seconds = _require_int(
            lease_seconds, "lease_seconds", minimum=1, maximum=MAX_LEASE_SECONDS
        )
        now = _utc_now()
        with self._transaction():
            plan, manifest, stages = self._validate_plan_locked(normalized)
            self._require_runnable_locked(plan, manifest, now)
            incomplete = next((stage for stage in stages if stage["status"] != "complete"), None)
            if incomplete is None:
                return None
            stage_id = int(incomplete["id"])
            status = str(incomplete["status"])
            if status in {"failed", "cancelled", "quarantined", "awaiting_reconciliation"}:
                return None
            if status == "claimed":
                expires = _parse_time(str(incomplete["lease_expires_at"]))
                if expires > now:
                    return None
                if str(incomplete["mutation_state"]) in {
                    "intent_recorded", "effect_authorized", "effect_in_progress",
                    "result_uncertain", "result_applied",
                }:
                    self.db.execute(
                        "UPDATE long_horizon_stages SET status='awaiting_reconciliation', "
                        "claim_owner=NULL, lease_token_sha256=NULL, lease_expires_at=NULL WHERE id=?",
                        (stage_id,),
                    )
                    return None
                self._consume_retry_locked(
                    plan, manifest, incomplete, reason="lease_expired"
                )
                self.db.execute(
                    "UPDATE long_horizon_stages SET status='pending', claim_owner=NULL, "
                    "lease_token_sha256=NULL, lease_expires_at=NULL WHERE id=?",
                    (stage_id,),
                )
            token = secrets.token_hex(32)
            token_sha = hashlib.sha256(token.encode("ascii")).hexdigest()
            expires_at = _iso(now + timedelta(seconds=lease_seconds))
            updated = self.db.execute(
                """UPDATE long_horizon_stages
                   SET status='claimed', claim_owner=?, lease_token_sha256=?,
                       lease_expires_at=?, attempt_count=attempt_count+1,
                       active_reservation_id=NULL
                   WHERE id=? AND status='pending'""",
                (owner, token_sha, expires_at, stage_id),
            )
            if updated.rowcount != 1:
                return None
            row = self.db.execute("SELECT * FROM long_horizon_stages WHERE id=?", (stage_id,)).fetchone()
            return self._claim_payload(row, token)

    def renew_stage_lease(
        self,
        plan_id: int,
        stage_id: int,
        *,
        worker_id: str,
        lease_token: str,
        lease_seconds: int = 60,
    ) -> bool:
        lease_seconds = _require_int(
            lease_seconds, "lease_seconds", minimum=1, maximum=MAX_LEASE_SECONDS
        )
        with self._transaction():
            plan, manifest, _ = self._validate_plan_locked(_require_id(plan_id, "plan_id"))
            self._require_runnable_locked(plan, manifest, _utc_now())
            stage = self._claimed_stage_locked(plan_id, stage_id, worker_id, lease_token)
            if str(stage["mutation_state"]) in {"effect_authorized", "effect_in_progress"}:
                raise LongHorizonStateError("an authorized mutation lease cannot be extended")
            if _parse_time(str(stage["lease_expires_at"])) <= _utc_now():
                return False
            self.db.execute(
                "UPDATE long_horizon_stages SET lease_expires_at=? WHERE id=?",
                (_iso(_utc_now() + timedelta(seconds=lease_seconds)), int(stage["id"])),
            )
            return True

    def record_checkpoint(
        self,
        plan_id: int,
        stage_id: int,
        *,
        worker_id: str,
        lease_token: str,
        usage: Mapping[str, Any],
        outcome_sha256: str,
        artifact_sha256: str,
        executor_id: str,
    ) -> dict[str, Any]:
        normalized = _require_id(plan_id, "plan_id")
        outcome = _require_sha(outcome_sha256, "outcome_sha256")
        artifact = _require_sha(artifact_sha256, "artifact_sha256")
        executor = _require_identity(executor_id, "executor_id")
        consumed = self._validated_usage(usage)
        stamp = _iso()
        with self._transaction():
            plan, manifest, stages = self._validate_plan_locked(normalized)
            self._require_runnable_locked(plan, manifest, _utc_now())
            stage = self._claimed_stage_locked(normalized, stage_id, worker_id, lease_token)
            reservation = self._reservation_for_claim_locked(stage)
            if reservation is None:
                raise LongHorizonStateError("checkpoint requires a durable pre-operation usage reservation")
            reserved_usage = self._validated_usage(_strict_json_loads(str(reservation["usage_json"]), "reserved usage"))
            if reserved_usage != consumed:
                raise LongHorizonStateError(
                    "checkpoint usage must exactly match its pre-operation reservation"
                )
            if int(stage["ordinal"]) != int(plan["next_stage_ordinal"]):
                raise LongHorizonIntegrityError("checkpoint is out of order", plan_id=normalized, reason="out_of_order")
            mutation_kind = str(stage["mutation_kind"])
            mutation_state = str(stage["mutation_state"])
            if mutation_kind != "none" and mutation_state not in {"result_applied", "reconciled_applied"}:
                raise LongHorizonStateError("mutation stage requires a confirmed result or reconciliation")
            if mutation_kind != "none":
                intent_actor = self.db.execute(
                    "SELECT actor_id FROM long_horizon_mutation_receipts "
                    "WHERE stage_id=? AND event_type='intent' ORDER BY generation DESC LIMIT 1",
                    (int(stage["id"]),),
                ).fetchone()
                if intent_actor is None or str(intent_actor["actor_id"]) != executor:
                    raise LongHorizonStateError(
                        "mutation checkpoint executor must match the bound mutation actor"
                    )
            previous = plan["checkpoint_head_sha256"]
            receipt = {
                "schema": CHECKPOINT_SCHEMA,
                "plan_id": normalized,
                "manifest_sha256": str(plan["manifest_sha256"]),
                "stage_id": int(stage["id"]),
                "stage_key": str(stage["stage_key"]),
                "ordinal": int(stage["ordinal"]),
                "stage_sha256": str(stage["stage_sha256"]),
                "executor_id": executor,
                "outcome_sha256": outcome,
                "artifact_sha256": artifact,
                "usage": consumed,
                "usage_reservation_sha256": str(reservation["receipt_sha256"]),
                "previous_sha256": previous,
                "created_at": stamp,
            }
            receipt_json = _canonical(receipt)
            receipt_sha = hashlib.sha256(receipt_json.encode("utf-8")).hexdigest()
            checkpoint = self.db.execute(
                """INSERT INTO long_horizon_checkpoints(
                       plan_id, stage_id, sequence, created_at, previous_sha256,
                       receipt_json, receipt_sha256, receipt_mac_sha256
                   ) VALUES (?,?,?,?,?,?,?,?)""",
                (
                    normalized, int(stage["id"]), int(stage["ordinal"]), stamp,
                    previous, receipt_json, receipt_sha, self._mac(receipt_json),
                ),
            )
            checkpoint_id = int(checkpoint.lastrowid)
            self.db.execute(
                """UPDATE long_horizon_stages SET status='complete', claim_owner=NULL,
                       lease_token_sha256=NULL, lease_expires_at=NULL, executor_id=?,
                       outcome_sha256=?, artifact_sha256=?, checkpoint_id=?
                   WHERE id=?""",
                (
                    executor, outcome, artifact, checkpoint_id, int(stage["id"]),
                ),
            )
            self.db.execute(
                """UPDATE long_horizon_plans SET updated_at=?, next_stage_ordinal=?,
                       checkpoint_head_sha256=?
                   WHERE id=?""",
                (stamp, int(stage["ordinal"]) + 1, receipt_sha, normalized),
            )
            return {"checkpoint_id": checkpoint_id, "receipt_sha256": receipt_sha, "sequence": int(stage["ordinal"])}

    def reserve_stage_usage(
        self,
        plan_id: int,
        stage_id: int,
        *,
        worker_id: str,
        lease_token: str,
        usage: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Charge bounded usage before any model/tool callback can execute."""
        normalized = _require_id(plan_id, "plan_id")
        consumed = self._validated_usage(usage)
        with self._transaction():
            plan, manifest, _ = self._validate_plan_locked(normalized)
            self._require_runnable_locked(plan, manifest, _utc_now())
            stage = self._claimed_stage_locked(normalized, stage_id, worker_id, lease_token)
            existing = self._reservation_for_claim_locked(stage)
            if existing is not None:
                if self._validated_usage(_strict_json_loads(str(existing["usage_json"]), "reserved usage")) != consumed:
                    raise LongHorizonIntegrityError(
                        "usage reservation replay changes charged usage",
                        plan_id=normalized,
                        reason="usage_replay",
                    )
                return {
                    "reservation_id": int(existing["id"]),
                    "receipt_sha256": str(existing["receipt_sha256"]),
                    "usage": consumed,
                }
            return self._reserve_usage_locked(plan, manifest, stage, consumed)

    def record_mutation_intent(
        self,
        plan_id: int,
        stage_id: int,
        *,
        worker_id: str,
        lease_token: str,
        executor_id: str,
    ) -> dict[str, Any]:
        normalized = _require_id(plan_id, "plan_id")
        actor = _require_identity(executor_id, "executor_id")
        with self._transaction():
            plan, manifest, _ = self._validate_plan_locked(normalized)
            self._require_runnable_locked(plan, manifest, _utc_now())
            stage = self._claimed_stage_locked(normalized, stage_id, worker_id, lease_token)
            if self._reservation_for_claim_locked(stage) is None:
                raise LongHorizonStateError("mutation intent requires a durable usage reservation")
            _, approval = self._approval_locked("intent", {
                "plan_id": normalized, "project_id": self.project_id,
                "stage_id": int(stage["id"]), "effect_key": str(stage["effect_key"]),
                "actor_id": actor,
            })
            if str(stage["mutation_state"]) not in {"none", "reconciled_not_applied", "result_not_applied"}:
                raise LongHorizonStateError("mutation intent already exists")
            receipt = self._append_mutation_receipt_locked(
                normalized, stage, actor, "intent", None, approval,
            )
            self.db.execute("UPDATE long_horizon_stages SET mutation_state='intent_recorded' WHERE id=?", (int(stage["id"]),))
            return receipt

    def authorize_mutation_effect(
        self, plan_id: int, stage_id: int, *, worker_id: str,
        lease_token: str, executor_id: str,
    ) -> dict[str, Any]:
        normalized = _require_id(plan_id, "plan_id")
        actor = _require_identity(executor_id, "executor_id")
        with self._transaction():
            plan, manifest, _ = self._validate_plan_locked(normalized)
            self._require_runnable_locked(plan, manifest, _utc_now())
            stage = self._claimed_stage_locked(normalized, stage_id, worker_id, lease_token)
            if str(stage["mutation_state"]) != "intent_recorded":
                raise LongHorizonStateError("pre-effect authorization requires a durable intent")
            intent = self.db.execute(
                "SELECT actor_id FROM long_horizon_mutation_receipts WHERE stage_id=? "
                "AND generation=? AND event_type='intent'",
                (int(stage["id"]), int(stage["attempt_count"])),
            ).fetchone()
            if intent is None or str(intent["actor_id"]) != actor:
                raise LongHorizonStateError("pre-effect actor differs from intent actor")
            _, approval = self._approval_locked("pre_effect", {
                "plan_id": normalized, "project_id": self.project_id,
                "stage_id": int(stage["id"]), "effect_key": str(stage["effect_key"]),
                "actor_id": actor,
            })
            receipt = self._append_mutation_receipt_locked(
                normalized, stage, actor, "authorization", None, approval,
            )
            expires = _iso(_utc_now() + timedelta(seconds=30))
            self.db.execute(
                "UPDATE long_horizon_stages SET mutation_state='effect_authorized', authorization_expires_at=?, authorization_consumed_at=NULL WHERE id=?",
                (expires, int(stage["id"])),
            )
            receipt["expires_at"] = expires
            return receipt

    def consume_mutation_effect_authorization(
        self, plan_id: int, stage_id: int, *, worker_id: str,
        lease_token: str, executor_id: str,
    ) -> dict[str, Any]:
        """Consume the one-shot live permit immediately before an external effect."""
        normalized = _require_id(plan_id, "plan_id")
        actor = _require_identity(executor_id, "executor_id")
        with self._transaction():
            plan, manifest, _ = self._validate_plan_locked(normalized)
            self._require_runnable_locked(plan, manifest, _utc_now())
            stage = self._claimed_stage_locked(normalized, stage_id, worker_id, lease_token)
            if str(stage["mutation_state"]) != "effect_authorized" or stage["authorization_consumed_at"] is not None:
                raise LongHorizonStateError("mutation effect permit is unavailable or consumed")
            if _parse_time(str(stage["authorization_expires_at"])) <= _utc_now():
                raise LongHorizonStateError("mutation effect authorization expired")
            intent = self.db.execute(
                "SELECT actor_id FROM long_horizon_mutation_receipts WHERE stage_id=? AND generation=? AND event_type='intent'",
                (int(stage["id"]), int(stage["attempt_count"])),
            ).fetchone()
            if intent is None or str(intent["actor_id"]) != actor:
                raise LongHorizonStateError("effect actor differs from intent actor")
            _, approval = self._approval_locked("pre_effect", {
                "plan_id": normalized, "project_id": self.project_id,
                "stage_id": int(stage["id"]), "effect_key": str(stage["effect_key"]),
                "actor_id": actor,
            })
            receipt = self._append_mutation_receipt_locked(
                normalized, stage, actor, "effect_permit", None, approval,
            )
            stamp = _iso()
            self.db.execute(
                "UPDATE long_horizon_stages SET mutation_state='effect_in_progress', authorization_consumed_at=? WHERE id=?",
                (stamp, int(stage["id"])),
            )
            receipt["consumed_at"] = stamp
            return receipt

    def record_mutation_result(
        self,
        plan_id: int,
        stage_id: int,
        *,
        worker_id: str,
        lease_token: str,
        executor_id: str,
        outcome: str,
        evidence_sha256: str,
    ) -> dict[str, Any]:
        normalized = str(outcome).strip().casefold()
        if normalized not in MUTATION_OUTCOMES:
            raise LongHorizonValidationError("unknown mutation result")
        return self._record_mutation_event(
            plan_id, stage_id, worker_id=worker_id, lease_token=lease_token,
            actor_id=executor_id, event_type="result", outcome=normalized,
            evidence_sha256=_require_sha(evidence_sha256, "evidence_sha256"),
        )

    def reconcile_mutation(
        self,
        plan_id: int,
        stage_id: int,
        *,
        authority_id: str,
        reconciler_runtime_sha256: str,
        outcome: str,
        evidence_sha256: str,
        signature_sha256: str,
    ) -> dict[str, Any]:
        normalized = str(outcome).strip().casefold()
        if normalized not in MUTATION_RECONCILIATIONS:
            raise LongHorizonValidationError("unknown reconciliation outcome")
        plan_id = _require_id(plan_id, "plan_id")
        authority_key = _require_identity(authority_id, "authority_id")
        runtime = _require_sha(reconciler_runtime_sha256, "reconciler_runtime_sha256")
        evidence = _require_sha(evidence_sha256, "evidence_sha256")
        signature = _require_signature(signature_sha256, "signature_sha256")
        with self._transaction():
            _plan, manifest, _stages = self._validate_plan_locked(plan_id)
            authority = self._authority_locked(authority_key, "mutation_reconciliation")
            actor = str(authority["verifier_id"])
            if runtime != str(authority["runtime_sha256"]):
                raise LongHorizonStateError("reconciler runtime is not the pinned runtime")
            stage = self.db.execute(
                "SELECT * FROM long_horizon_stages WHERE id=? AND plan_id=?",
                (_require_id(stage_id, "stage_id"), plan_id),
            ).fetchone()
            if stage is None or str(stage["mutation_kind"]) == "none":
                raise LongHorizonStateError("mutation stage does not exist")
            if str(stage["status"]) not in {"claimed", "awaiting_reconciliation"}:
                raise LongHorizonStateError("stage is not awaiting mutation reconciliation")
            if str(stage["mutation_state"]) not in {
                "intent_recorded", "effect_authorized", "effect_in_progress",
                "result_uncertain", "result_applied", "reconciled_uncertain",
                "reconciled_applied",
            }:
                raise LongHorizonStateError("mutation state does not require reconciliation")
            if str(stage["mutation_state"]) in {"result_applied", "reconciled_applied"} and normalized != "applied":
                raise LongHorizonIntegrityError(
                    "an applied mutation cannot be downgraded", plan_id=plan_id,
                    reason="applied_effect_conflict",
                )
            prior_authority = self.db.execute(
                "SELECT authority_id FROM long_horizon_mutation_receipts WHERE stage_id=? "
                "AND generation=? AND event_type='reconciliation' ORDER BY reconciliation_round LIMIT 1",
                (int(stage["id"]), int(stage["attempt_count"])),
            ).fetchone()
            if prior_authority is not None and str(prior_authority["authority_id"]) != authority_key:
                raise LongHorizonStateError("reconciliation authority cannot change within a generation")
            existing = self.db.execute(
                "SELECT * FROM long_horizon_mutation_receipts WHERE stage_id=? AND generation=? "
                "AND event_type='reconciliation' AND outcome=? AND evidence_sha256=? "
                "AND authority_id=? AND runtime_sha256=? AND signature_sha256=?",
                (int(stage["id"]), int(stage["attempt_count"]), normalized, evidence,
                 authority_key, runtime, signature),
            ).fetchone()
            if existing is not None:
                return {
                    "receipt_id": int(existing["id"]),
                    "receipt_sha256": str(existing["receipt_sha256"]),
                    "effect_key": str(existing["effect_key"]),
                    "generation": int(existing["generation"]),
                    "reconciliation_round": int(existing["reconciliation_round"]),
                }
            reconciliation_round = int(self.db.execute(
                "SELECT COALESCE(MAX(reconciliation_round),0)+1 FROM long_horizon_mutation_receipts "
                "WHERE stage_id=? AND generation=? AND event_type='reconciliation'",
                (int(stage["id"]), int(stage["attempt_count"])),
            ).fetchone()[0])
            challenge = {
                "schema": "jarvis.long-horizon.mutation-reconciliation-challenge.v1",
                "plan_id": plan_id, "project_id": self.project_id,
                "manifest_sha256": str(_plan["manifest_sha256"]),
                "stage_id": int(stage["id"]), "stage_sha256": str(stage["stage_sha256"]),
                "effect_key": str(stage["effect_key"]), "generation": int(stage["attempt_count"]),
                "reconciliation_round": reconciliation_round,
                "authority_id": authority_key, "reconciler_id": actor,
                "reconciler_runtime_sha256": runtime, "outcome": normalized,
                "evidence_sha256": evidence,
            }
            self._verify_authority_signature(authority["public_key"], challenge, signature)
            receipt = self._append_mutation_receipt_locked(
                plan_id, stage, actor, "reconciliation", normalized, evidence,
                authority_id=authority_key, runtime_sha256=runtime,
                signature_sha256=signature,
            )
            if normalized == "applied":
                # Reconciliation never confers a lease. A worker must claim the
                # stage again, see reconciled_applied, and checkpoint only.
                status, state = (
                    "cancelled" if str(_plan["status"]) == "cancelled" else "pending",
                    "reconciled_applied",
                )
            elif normalized == "not_applied":
                self.db.execute(
                    "UPDATE long_horizon_stages SET mutation_state='reconciled_not_applied' WHERE id=?",
                    (int(stage["id"]),),
                )
                self._consume_retry_locked(
                    _plan, manifest, stage, reason="reconciliation_not_applied"
                )
                status, state = (
                    "cancelled" if str(_plan["status"]) == "cancelled" else "pending",
                    "reconciled_not_applied",
                )
            else:
                status, state = "awaiting_reconciliation", "reconciled_uncertain"
            self.db.execute(
                "UPDATE long_horizon_stages SET status=?, mutation_state=?, claim_owner=NULL, "
                "lease_token_sha256=NULL, lease_expires_at=NULL WHERE id=?",
                (status, state, int(stage["id"])),
            )
            return receipt

    def pause_plan(self, plan_id: int, reason_sha256: str) -> dict[str, Any]:
        return self._set_plan_control(plan_id, "paused", reason_sha256)

    def resume_plan(self, plan_id: int) -> dict[str, Any]:
        normalized = _require_id(plan_id, "plan_id")
        with self._transaction():
            plan, manifest, stages = self._validate_plan_locked(normalized)
            if str(plan["status"]) != "paused":
                raise LongHorizonStateError("only a paused plan can resume")
            control = self.db.execute("SELECT state FROM runtime_control WHERE id=1").fetchone()
            if control is None or str(control["state"]) != "running":
                raise LongHorizonStateError("global runtime control is not running")
            self.db.execute(
                "UPDATE long_horizon_plans SET status='active', pause_reason_sha256=NULL, updated_at=? WHERE id=?",
                (_iso(), normalized),
            )
            plan = self.db.execute("SELECT * FROM long_horizon_plans WHERE id=?", (normalized,)).fetchone()
            return self._status_locked(plan, manifest, stages)

    def cancel_plan(self, plan_id: int, reason_sha256: str) -> dict[str, Any]:
        return self._set_plan_control(plan_id, "cancelled", reason_sha256)

    def record_final_verification(
        self,
        plan_id: int,
        *,
        authority_id: str,
        verifier_runtime_sha256: str,
        evidence_sha256: str,
        signature_sha256: str,
        passed: bool,
    ) -> dict[str, Any]:
        normalized = _require_id(plan_id, "plan_id")
        authority_key = _require_identity(authority_id, "authority_id")
        runtime = _require_sha(verifier_runtime_sha256, "verifier_runtime_sha256")
        evidence = _require_sha(evidence_sha256, "evidence_sha256")
        signature = _require_signature(signature_sha256, "signature_sha256")
        if not isinstance(passed, bool):
            raise LongHorizonValidationError("passed must be boolean")
        with self._transaction():
            plan, manifest, stages = self._validate_plan_locked(normalized)
            authority = self._authority_locked(authority_key, "final_verification")
            verifier = str(authority["verifier_id"])
            if runtime != str(authority["runtime_sha256"]):
                raise LongHorizonStateError("final verifier runtime is not the pinned runtime")
            if any(str(stage["status"]) != "complete" for stage in stages):
                raise LongHorizonStateError("all stages must complete before final verification")
            executors = {str(stage["executor_id"]) for stage in stages if stage["executor_id"]}
            executors.update(
                str(row[0]) for row in self.db.execute(
                    "SELECT DISTINCT actor_id FROM long_horizon_mutation_receipts WHERE plan_id=?",
                    (normalized,),
                ).fetchall()
            )
            if verifier in executors:
                raise LongHorizonStateError("final verifier is not independent from workflow actors")
            head = str(plan["checkpoint_head_sha256"] or "")
            _require_sha(head, "checkpoint_head_sha256")
            challenge = {
                "schema": "jarvis.long-horizon.final-verification-challenge.v1",
                "plan_id": normalized, "project_id": self.project_id,
                "manifest_sha256": str(plan["manifest_sha256"]),
                "checkpoint_head_sha256": head,
                "authority_id": authority_key, "verifier_id": verifier,
                "verifier_runtime_sha256": runtime,
                "evidence_sha256": evidence, "passed": passed,
            }
            self._verify_authority_signature(authority["public_key"], challenge, signature)
            verification = _digest(challenge)
            existing = self.db.execute(
                "SELECT * FROM long_horizon_final_verifications WHERE plan_id=? "
                "AND authority_id=? AND verification_sha256=?",
                (normalized, authority_key, verification),
            ).fetchone()
            if existing is not None:
                if not secrets.compare_digest(str(existing["signature_sha256"]), signature):
                    raise LongHorizonIntegrityError("verification replay conflicts", plan_id=normalized, reason="verification_replay")
                return {"verification_id": int(existing["id"]), "receipt_sha256": str(existing["receipt_sha256"]), "passed": bool(existing["passed"]), "plan_complete": str(plan["status"]) == "complete"}
            if str(plan["status"]) != "active":
                raise LongHorizonStateError("plan is not active")
            stamp = _iso()
            receipt = {
                "schema": VERIFICATION_SCHEMA,
                "plan_id": normalized,
                "manifest_sha256": str(plan["manifest_sha256"]),
                "checkpoint_head_sha256": head,
                "authority_id": authority_key,
                "verifier_id": verifier,
                "verifier_runtime_sha256": runtime,
                "evidence_sha256": evidence,
                "signature_sha256": signature,
                "verification_sha256": verification,
                "passed": passed,
                "created_at": stamp,
            }
            receipt_json = _canonical(receipt)
            receipt_sha = hashlib.sha256(receipt_json.encode("utf-8")).hexdigest()
            cursor = self.db.execute(
                """INSERT INTO long_horizon_final_verifications(
                       plan_id, created_at, verifier_id, authority_id,
                       verifier_runtime_sha256, passed, evidence_sha256,
                       signature_sha256, verification_sha256, checkpoint_head_sha256,
                       receipt_json, receipt_sha256, receipt_mac_sha256
                   ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (normalized, stamp, verifier, authority_key, runtime, int(passed), evidence,
                 signature, verification, head, receipt_json, receipt_sha, self._mac(receipt_json)),
            )
            verification_id = int(cursor.lastrowid)
            if passed:
                self.db.execute(
                    "UPDATE long_horizon_plans SET status='complete', final_verification_id=?, updated_at=? WHERE id=?",
                    (verification_id, stamp, normalized),
                )
            return {"verification_id": verification_id, "receipt_sha256": receipt_sha, "passed": passed, "plan_complete": passed}

    def export_evidence(self, plan_id: int) -> dict[str, Any]:
        normalized = _require_id(plan_id, "plan_id")
        with self._transaction():
            plan, manifest, stages = self._validate_plan_locked(normalized)
            checkpoints = self.db.execute(
                "SELECT sequence, previous_sha256, receipt_sha256, receipt_json FROM long_horizon_checkpoints "
                "WHERE plan_id=? ORDER BY sequence",
                (normalized,),
            ).fetchall()
            mutations = self.db.execute(
                "SELECT stage_id, generation, event_type, outcome, effect_key, actor_id, evidence_sha256, "
                "previous_sha256, receipt_sha256, receipt_json FROM long_horizon_mutation_receipts "
                "WHERE plan_id=? ORDER BY id",
                (normalized,),
            ).fetchall()
            verifications = self.db.execute(
                "SELECT verifier_id, passed, verification_sha256, checkpoint_head_sha256, receipt_sha256, receipt_json "
                "FROM long_horizon_final_verifications WHERE plan_id=? ORDER BY id",
                (normalized,),
            ).fetchall()
            payload = {
                "schema": EVIDENCE_SCHEMA,
                "plan": self._status_locked(plan, manifest, stages),
                "manifest": manifest.to_payload(),
                "stages": [self._evidence_stage(stage) for stage in stages],
                "checkpoints": [dict(row) for row in checkpoints],
                "mutation_receipts": [dict(row) for row in mutations],
                "final_verifications": [dict(row) for row in verifications],
            }
            payload["evidence_sha256"] = _digest(payload)
            return payload

    # Internal state machine -------------------------------------------------

    def _validate_binding_locked(self, project_id: int, conversation_id: int, task_id: int) -> None:
        project = self.db.execute("SELECT enabled FROM agent_projects WHERE id=?", (project_id,)).fetchone()
        conversation = self.db.execute("SELECT project_id FROM conversations WHERE id=?", (conversation_id,)).fetchone()
        task = self.db.execute("SELECT project_id FROM tasks WHERE id=?", (task_id,)).fetchone()
        if project is None or int(project["enabled"]) != 1:
            raise LongHorizonValidationError("bound project is missing or disabled")
        if conversation is None or int(conversation["project_id"]) != project_id:
            raise LongHorizonValidationError("conversation is not bound to the exact project")
        if task is None or int(task["project_id"]) != project_id:
            raise LongHorizonValidationError("task is not bound to the exact project")

    def _validate_plan_locked(self, plan_id: int):
        plan = self.db.execute("SELECT * FROM long_horizon_plans WHERE id=?", (plan_id,)).fetchone()
        if plan is None:
            raise LongHorizonValidationError("plan does not exist")
        if int(plan["project_id"]) != self.project_id:
            raise LongHorizonValidationError("plan is outside this project-scoped store")
        if not plan["state_mac_sha256"] or not secrets.compare_digest(
            str(plan["state_mac_sha256"]),
            self._mac(self._state_material(plan, frozenset({"state_mac_sha256"}))),
        ):
            raise LongHorizonIntegrityError("plan state keyed integrity mismatch", plan_id=plan_id, reason="plan_state_mac_invalid")
        if not secrets.compare_digest(
            str(plan["manifest_mac_sha256"]), self._mac(str(plan["manifest_json"]))
        ):
            raise LongHorizonIntegrityError(
                "manifest keyed integrity mismatch", plan_id=plan_id,
                reason="manifest_mac_invalid",
            )
        try:
            manifest = parse_manifest_json(str(plan["manifest_json"]))
        except LongHorizonValidationError as exc:
            raise LongHorizonIntegrityError("stored manifest is invalid", plan_id=plan_id, reason="manifest_invalid") from exc
        if hashlib.sha256(str(plan["manifest_json"]).encode("utf-8")).hexdigest() != str(plan["manifest_sha256"]):
            raise LongHorizonIntegrityError("manifest digest mismatch", plan_id=plan_id, reason="manifest_tampered")
        if (
            manifest.project_id != int(plan["project_id"])
            or manifest.conversation_id != int(plan["conversation_id"])
            or manifest.task_id != int(plan["task_id"])
            or len(manifest.stages) != int(plan["stage_count"])
        ):
            raise LongHorizonIntegrityError("manifest binding mismatch", plan_id=plan_id, reason="binding_substitution")
        try:
            self._validate_binding_locked(manifest.project_id, manifest.conversation_id, manifest.task_id)
        except LongHorizonValidationError as exc:
            raise LongHorizonIntegrityError(str(exc), plan_id=plan_id, reason="binding_unavailable") from exc
        stages = self.db.execute(
            "SELECT * FROM long_horizon_stages WHERE plan_id=? ORDER BY ordinal", (plan_id,)
        ).fetchall()
        if len(stages) != len(manifest.stages):
            raise LongHorizonIntegrityError("stage count mismatch", plan_id=plan_id, reason="stage_substitution")
        for row, spec in zip(stages, manifest.stages):
            if not row["state_mac_sha256"] or not secrets.compare_digest(
                str(row["state_mac_sha256"]),
                self._mac(self._state_material(row, frozenset({"state_mac_sha256"}))),
            ):
                raise LongHorizonIntegrityError("stage state keyed integrity mismatch", plan_id=plan_id, reason="stage_state_mac_invalid")
            expected_json = _canonical(spec.to_payload())
            if (
                int(row["ordinal"]) != spec.ordinal
                or str(row["stage_key"]) != spec.stage_id
                or str(row["stage_json"]) != expected_json
                or str(row["stage_sha256"]) != hashlib.sha256(expected_json.encode("utf-8")).hexdigest()
            ):
                raise LongHorizonIntegrityError("stage integrity mismatch", plan_id=plan_id, reason="stage_tampered")
            if not secrets.compare_digest(
                str(row["stage_mac_sha256"]), self._mac(str(row["stage_json"]))
            ):
                raise LongHorizonIntegrityError(
                    "stage keyed integrity mismatch", plan_id=plan_id,
                    reason="stage_mac_invalid",
                )
            stable = {"manifest_sha256": str(plan["manifest_sha256"]), "stage": spec.to_payload()}
            if str(row["idempotency_key"]) != _digest({**stable, "kind": "stage"}) or str(row["effect_key"]) != _digest({**stable, "kind": "effect"}):
                raise LongHorizonIntegrityError("stage key integrity mismatch", plan_id=plan_id, reason="stage_key_tampered")
        self._validate_checkpoint_chain_locked(plan, stages)
        self._validate_mutation_chains_locked(plan_id, stages)
        self._validate_usage_retry_locked(plan, stages)
        self._validate_final_verifications_locked(plan, stages)
        return plan, manifest, stages

    def _validate_usage_retry_locked(self, plan: sqlite3.Row, stages: Sequence[sqlite3.Row]) -> None:
        usage_rows = self.db.execute(
            "SELECT * FROM long_horizon_usage_reservations WHERE plan_id=? ORDER BY id",
            (int(plan["id"]),),
        ).fetchall()
        previous: str | None = None
        totals = {key: 0 for key in USAGE_KEYS}
        by_stage = {int(stage["id"]): {key: 0 for key in USAGE_KEYS} for stage in stages}
        for row in usage_rows:
            raw = str(row["receipt_json"])
            if not secrets.compare_digest(str(row["receipt_mac_sha256"]), self._mac(raw)):
                raise LongHorizonIntegrityError("usage receipt keyed integrity mismatch", plan_id=int(plan["id"]), reason="usage_mac_invalid")
            if hashlib.sha256(raw.encode()).hexdigest() != str(row["receipt_sha256"]) or row["previous_sha256"] != previous:
                raise LongHorizonIntegrityError("usage receipt chain mismatch", plan_id=int(plan["id"]), reason="usage_chain_invalid")
            try:
                material = _strict_json_loads(raw, "usage receipt")
                parsed = self._validated_usage(material["usage"])
            except (KeyError, LongHorizonValidationError) as exc:
                raise LongHorizonIntegrityError("usage receipt is invalid", plan_id=int(plan["id"]), reason="usage_receipt_invalid") from exc
            if raw != _canonical(material) or material.get("plan_id") != int(plan["id"]) or material.get("stage_id") != int(row["stage_id"]) or material.get("attempt_number") != int(row["attempt_number"]) or material.get("previous_sha256") != previous:
                raise LongHorizonIntegrityError("usage receipt binding mismatch", plan_id=int(plan["id"]), reason="usage_receipt_invalid")
            if _canonical(parsed) != str(row["usage_json"]):
                raise LongHorizonIntegrityError("usage payload substitution", plan_id=int(plan["id"]), reason="usage_receipt_invalid")
            for key in USAGE_KEYS:
                totals[key] += parsed[key]
                by_stage[int(row["stage_id"])][key] += parsed[key]
            previous = str(row["receipt_sha256"])
        if (plan["usage_head_sha256"] or None) != previous or any(int(plan[f"used_{key}"]) != totals[key] for key in USAGE_KEYS):
            raise LongHorizonIntegrityError("plan usage counters do not match durable receipts", plan_id=int(plan["id"]), reason="usage_counter_tampered")
        for stage in stages:
            if any(int(stage[f"used_{key}"]) != by_stage[int(stage["id"])][key] for key in USAGE_KEYS):
                raise LongHorizonIntegrityError("stage usage counters do not match durable receipts", plan_id=int(plan["id"]), reason="usage_counter_tampered")

        retry_rows = self.db.execute(
            "SELECT * FROM long_horizon_retry_receipts WHERE plan_id=? ORDER BY id",
            (int(plan["id"]),),
        ).fetchall()
        previous = None
        for row in retry_rows:
            raw = str(row["receipt_json"])
            if not secrets.compare_digest(str(row["receipt_mac_sha256"]), self._mac(raw)) or hashlib.sha256(raw.encode()).hexdigest() != str(row["receipt_sha256"]) or row["previous_sha256"] != previous:
                raise LongHorizonIntegrityError("retry receipt chain mismatch", plan_id=int(plan["id"]), reason="retry_chain_invalid")
            material = _strict_json_loads(raw, "retry receipt")
            if raw != _canonical(material) or material.get("plan_id") != int(plan["id"]) or material.get("stage_id") != int(row["stage_id"]) or material.get("attempt_number") != int(row["attempt_number"]) or material.get("reason") != str(row["reason"]) or material.get("previous_sha256") != previous:
                raise LongHorizonIntegrityError("retry receipt binding mismatch", plan_id=int(plan["id"]), reason="retry_receipt_invalid")
            previous = str(row["receipt_sha256"])
        if int(plan["used_retries"]) != len(retry_rows) or (plan["retry_head_sha256"] or None) != previous:
            raise LongHorizonIntegrityError("retry counters do not match durable receipts", plan_id=int(plan["id"]), reason="retry_counter_tampered")

    def _validate_checkpoint_chain_locked(self, plan: sqlite3.Row, stages: Sequence[sqlite3.Row]) -> None:
        rows = self.db.execute(
            "SELECT * FROM long_horizon_checkpoints WHERE plan_id=? ORDER BY sequence", (int(plan["id"]),)
        ).fetchall()
        previous: str | None = None
        for index, row in enumerate(rows, 1):
            if int(row["sequence"]) != index or row["previous_sha256"] != previous:
                raise LongHorizonIntegrityError("checkpoint chain order mismatch", plan_id=int(plan["id"]), reason="checkpoint_order")
            if hashlib.sha256(str(row["receipt_json"]).encode("utf-8")).hexdigest() != str(row["receipt_sha256"]):
                raise LongHorizonIntegrityError("checkpoint digest mismatch", plan_id=int(plan["id"]), reason="checkpoint_tampered")
            if not secrets.compare_digest(str(row["receipt_mac_sha256"]), self._mac(str(row["receipt_json"]))):
                raise LongHorizonIntegrityError("checkpoint keyed integrity mismatch", plan_id=int(plan["id"]), reason="checkpoint_mac_invalid")
            try:
                receipt = _strict_json_loads(str(row["receipt_json"]), "checkpoint receipt")
            except json.JSONDecodeError as exc:
                raise LongHorizonIntegrityError("checkpoint JSON invalid", plan_id=int(plan["id"]), reason="checkpoint_tampered") from exc
            stage = stages[index - 1]
            expected_keys = {
                "schema", "plan_id", "manifest_sha256", "stage_id", "stage_key",
                "ordinal", "stage_sha256", "executor_id", "outcome_sha256",
                "artifact_sha256", "usage", "previous_sha256", "created_at",
                "usage_reservation_sha256",
            }
            try:
                self._validated_usage(receipt.get("usage", {}))
            except LongHorizonValidationError as exc:
                raise LongHorizonIntegrityError(
                    "checkpoint usage is invalid", plan_id=int(plan["id"]),
                    reason="checkpoint_tampered",
                ) from exc
            reservation_row = self.db.execute(
                "SELECT receipt_sha256 FROM long_horizon_usage_reservations WHERE id=?",
                (int(stage["active_reservation_id"] or 0),),
            ).fetchone()
            if reservation_row is None:
                raise LongHorizonIntegrityError(
                    "checkpoint usage reservation is missing", plan_id=int(plan["id"]),
                    reason="checkpoint_reservation_missing",
                )
            if (
                not isinstance(receipt, dict)
                or set(receipt) != expected_keys
                or str(row["receipt_json"]) != _canonical(receipt)
                or receipt.get("schema") != CHECKPOINT_SCHEMA
                or int(receipt.get("plan_id", 0)) != int(plan["id"])
                or int(receipt.get("stage_id", 0)) != int(stage["id"])
                or int(row["stage_id"]) != int(stage["id"])
                or int(row["sequence"]) != int(stage["ordinal"])
                or receipt.get("stage_key") != str(stage["stage_key"])
                or int(receipt.get("ordinal", 0)) != int(stage["ordinal"])
                or receipt.get("stage_sha256") != str(stage["stage_sha256"])
                or receipt.get("executor_id") != stage["executor_id"]
                or receipt.get("outcome_sha256") != stage["outcome_sha256"]
                or receipt.get("artifact_sha256") != stage["artifact_sha256"]
                or receipt.get("usage_reservation_sha256") != str(reservation_row["receipt_sha256"])
                or receipt.get("created_at") != str(row["created_at"])
                or receipt.get("previous_sha256") != previous
                or receipt.get("manifest_sha256") != str(plan["manifest_sha256"])
            ):
                raise LongHorizonIntegrityError("checkpoint substitution", plan_id=int(plan["id"]), reason="checkpoint_substitution")
            previous = str(row["receipt_sha256"])
        expected_head = previous
        if (plan["checkpoint_head_sha256"] or None) != expected_head:
            raise LongHorizonIntegrityError("checkpoint head mismatch", plan_id=int(plan["id"]), reason="checkpoint_head_tampered")
        complete_count = sum(str(stage["status"]) == "complete" for stage in stages)
        if complete_count != len(rows) or int(plan["next_stage_ordinal"]) != len(rows) + 1:
            raise LongHorizonIntegrityError("checkpoint/stage progress mismatch", plan_id=int(plan["id"]), reason="progress_tampered")

    def _validate_mutation_chains_locked(self, plan_id: int, stages: Sequence[sqlite3.Row]) -> None:
        for stage in stages:
            rows = self.db.execute(
                "SELECT * FROM long_horizon_mutation_receipts WHERE stage_id=? ORDER BY id", (int(stage["id"]),)
            ).fetchall()
            previous: str | None = None
            generations: dict[int, list[sqlite3.Row]] = {}
            for row in rows:
                generations.setdefault(int(row["generation"]), []).append(row)
                if int(row["plan_id"]) != plan_id or row["previous_sha256"] != previous:
                    raise LongHorizonIntegrityError("mutation chain mismatch", plan_id=plan_id, reason="mutation_chain_tampered")
                if str(row["effect_key"]) != str(stage["effect_key"]):
                    raise LongHorizonIntegrityError("mutation effect substitution", plan_id=plan_id, reason="effect_substitution")
                if hashlib.sha256(str(row["receipt_json"]).encode("utf-8")).hexdigest() != str(row["receipt_sha256"]):
                    raise LongHorizonIntegrityError("mutation receipt digest mismatch", plan_id=plan_id, reason="mutation_receipt_tampered")
                try:
                    receipt = _strict_json_loads(str(row["receipt_json"]), "mutation receipt")
                except json.JSONDecodeError as exc:
                    raise LongHorizonIntegrityError(
                        "mutation receipt JSON invalid", plan_id=plan_id,
                        reason="mutation_receipt_tampered",
                    ) from exc
                expected_keys = {
                    "schema", "plan_id", "stage_id", "stage_sha256", "generation",
                    "reconciliation_round",
                    "event_type", "outcome", "effect_key", "actor_id",
                    "evidence_sha256", "authority_id", "runtime_sha256",
                    "signature_sha256", "previous_sha256", "created_at",
                }
                if (
                    not isinstance(receipt, dict)
                    or set(receipt) != expected_keys
                    or str(row["receipt_json"]) != _canonical(receipt)
                    or receipt.get("schema") != MUTATION_RECEIPT_SCHEMA
                    or int(receipt.get("plan_id", 0)) != plan_id
                    or int(receipt.get("stage_id", 0)) != int(stage["id"])
                    or receipt.get("stage_sha256") != str(stage["stage_sha256"])
                    or int(receipt.get("generation", 0)) != int(row["generation"])
                    or receipt.get("reconciliation_round") != int(row["reconciliation_round"])
                    or receipt.get("event_type") != str(row["event_type"])
                    or receipt.get("outcome") != row["outcome"]
                    or receipt.get("effect_key") != str(row["effect_key"])
                    or receipt.get("actor_id") != str(row["actor_id"])
                    or receipt.get("evidence_sha256") != row["evidence_sha256"]
                    or receipt.get("authority_id") != row["authority_id"]
                    or receipt.get("runtime_sha256") != row["runtime_sha256"]
                    or receipt.get("signature_sha256") != row["signature_sha256"]
                    or receipt.get("previous_sha256") != row["previous_sha256"]
                    or receipt.get("created_at") != str(row["created_at"])
                ):
                    raise LongHorizonIntegrityError(
                        "mutation receipt binding mismatch", plan_id=plan_id,
                        reason="mutation_receipt_substitution",
                    )
                if not secrets.compare_digest(
                    str(row["receipt_mac_sha256"]), self._mac(str(row["receipt_json"]))
                ):
                    raise LongHorizonIntegrityError(
                        "mutation receipt keyed integrity mismatch", plan_id=plan_id,
                        reason="mutation_receipt_mac_invalid",
                    )
                if str(row["event_type"]) == "reconciliation":
                    authority = self._authority_locked(str(row["authority_id"]), "mutation_reconciliation")
                    challenge = {
                        "schema": "jarvis.long-horizon.mutation-reconciliation-challenge.v1",
                        "plan_id": plan_id, "project_id": self.project_id,
                        "manifest_sha256": str(self.db.execute("SELECT manifest_sha256 FROM long_horizon_plans WHERE id=?", (plan_id,)).fetchone()[0]),
                        "stage_id": int(stage["id"]), "stage_sha256": str(stage["stage_sha256"]),
                        "effect_key": str(stage["effect_key"]), "generation": int(row["generation"]),
                        "reconciliation_round": int(row["reconciliation_round"]),
                        "authority_id": str(row["authority_id"]), "reconciler_id": str(row["actor_id"]),
                        "reconciler_runtime_sha256": str(row["runtime_sha256"]),
                        "outcome": str(row["outcome"]), "evidence_sha256": str(row["evidence_sha256"]),
                    }
                    if str(row["actor_id"]) != authority["verifier_id"] or str(row["runtime_sha256"]) != authority["runtime_sha256"]:
                        raise LongHorizonIntegrityError("reconciler authority binding mismatch", plan_id=plan_id, reason="reconciliation_authority_invalid")
                    self._verify_authority_signature(authority["public_key"], challenge, str(row["signature_sha256"]))
                previous = str(row["receipt_sha256"])
            if str(stage["mutation_kind"]) == "none" and rows:
                raise LongHorizonIntegrityError("non-mutation stage has mutation receipts", plan_id=plan_id, reason="mutation_substitution")
            if str(stage["mutation_kind"]) == "none":
                if str(stage["mutation_state"]) != "none":
                    raise LongHorizonIntegrityError("non-mutation stage has mutation state", plan_id=plan_id, reason="mutation_state_tampered")
                continue
            derived = "none"
            actor: str | None = None
            for generation in sorted(generations):
                events = generations[generation]
                names = [str(row["event_type"]) for row in events]
                first_reconciliation = next(
                    (index for index, name in enumerate(names) if name == "reconciliation"),
                    len(names),
                )
                prefix, suffix = names[:first_reconciliation], names[first_reconciliation:]
                if not names or names[0] != "intent" or prefix not in (
                    ["intent"], ["intent", "authorization"],
                    ["intent", "authorization", "effect_permit"],
                    ["intent", "authorization", "effect_permit", "result"],
                ) or any(name != "reconciliation" for name in suffix):
                    raise LongHorizonIntegrityError("mutation event sequence is invalid", plan_id=plan_id, reason="mutation_sequence_invalid")
                actor = str(events[0]["actor_id"])
                if any(str(row["actor_id"]) != actor for row in events if str(row["event_type"]) in {"authorization", "effect_permit", "result"}):
                    raise LongHorizonIntegrityError("mutation actor changed within a generation", plan_id=plan_id, reason="mutation_actor_substitution")
                final = events[-1]
                event, outcome = str(final["event_type"]), final["outcome"]
                if event == "intent": derived = "intent_recorded"
                elif event == "authorization": derived = "effect_authorized"
                elif event == "effect_permit": derived = "effect_in_progress"
                elif event == "result": derived = f"result_{outcome}"
                else: derived = f"reconciled_{outcome}"
                first_applied = next((index for index, row in enumerate(events)
                    if str(row["event_type"]) in {"result", "reconciliation"}
                    and str(row["outcome"]) == "applied"), None)
                if first_applied is not None and any(
                    str(row["event_type"]) == "reconciliation"
                    and str(row["outcome"]) != "applied"
                    for row in events[first_applied + 1:]
                ):
                    raise LongHorizonIntegrityError("applied mutation was downgraded", plan_id=plan_id, reason="applied_effect_conflict")
                if generation != max(generations) and derived not in {"result_not_applied", "reconciled_not_applied"}:
                    raise LongHorizonIntegrityError("unfinished mutation generation was superseded", plan_id=plan_id, reason="mutation_generation_invalid")
            if str(stage["mutation_state"]) != derived:
                raise LongHorizonIntegrityError("mutation state does not match receipt chain", plan_id=plan_id, reason="mutation_state_tampered")
            if str(stage["status"]) == "complete" and derived not in {"result_applied", "reconciled_applied"}:
                raise LongHorizonIntegrityError("mutation completed without applied evidence", plan_id=plan_id, reason="mutation_completion_invalid")

    def _validate_final_verifications_locked(self, plan: sqlite3.Row, stages: Sequence[sqlite3.Row]) -> None:
        rows = self.db.execute(
            "SELECT * FROM long_horizon_final_verifications WHERE plan_id=? ORDER BY id", (int(plan["id"]),)
        ).fetchall()
        executors = {str(stage["executor_id"]) for stage in stages if stage["executor_id"]}
        for row in rows:
            if str(row["verifier_id"]) in executors:
                raise LongHorizonIntegrityError("verification is not independent", plan_id=int(plan["id"]), reason="verification_not_independent")
            if hashlib.sha256(str(row["receipt_json"]).encode("utf-8")).hexdigest() != str(row["receipt_sha256"]):
                raise LongHorizonIntegrityError("verification receipt digest mismatch", plan_id=int(plan["id"]), reason="verification_tampered")
            try:
                receipt = _strict_json_loads(str(row["receipt_json"]), "verification receipt")
            except json.JSONDecodeError as exc:
                raise LongHorizonIntegrityError(
                    "verification receipt JSON invalid", plan_id=int(plan["id"]),
                    reason="verification_tampered",
                ) from exc
            expected_keys = {
                "schema", "plan_id", "manifest_sha256", "checkpoint_head_sha256",
                "authority_id", "verifier_id", "verifier_runtime_sha256",
                "evidence_sha256", "signature_sha256", "verification_sha256",
                "passed", "created_at",
            }
            if (
                not isinstance(receipt, dict)
                or set(receipt) != expected_keys
                or str(row["receipt_json"]) != _canonical(receipt)
                or receipt.get("schema") != VERIFICATION_SCHEMA
                or int(receipt.get("plan_id", 0)) != int(plan["id"])
                or receipt.get("manifest_sha256") != str(plan["manifest_sha256"])
                or receipt.get("checkpoint_head_sha256") != str(row["checkpoint_head_sha256"])
                or receipt.get("verifier_id") != str(row["verifier_id"])
                or receipt.get("authority_id") != str(row["authority_id"])
                or receipt.get("verifier_runtime_sha256") != str(row["verifier_runtime_sha256"])
                or receipt.get("evidence_sha256") != str(row["evidence_sha256"])
                or receipt.get("signature_sha256") != str(row["signature_sha256"])
                or receipt.get("verification_sha256") != str(row["verification_sha256"])
                or bool(receipt.get("passed")) != bool(row["passed"])
                or receipt.get("created_at") != str(row["created_at"])
            ):
                raise LongHorizonIntegrityError(
                    "verification receipt binding mismatch", plan_id=int(plan["id"]),
                    reason="verification_substitution",
                )
            if not secrets.compare_digest(
                str(row["receipt_mac_sha256"]), self._mac(str(row["receipt_json"]))
            ):
                raise LongHorizonIntegrityError("verification keyed integrity mismatch", plan_id=int(plan["id"]), reason="verification_mac_invalid")
            if str(row["checkpoint_head_sha256"]) != str(plan["checkpoint_head_sha256"] or ""):
                raise LongHorizonIntegrityError("verification binds a different checkpoint head", plan_id=int(plan["id"]), reason="verification_substitution")
            authority = self._authority_locked(str(row["authority_id"]), "final_verification")
            challenge = {
                "schema": "jarvis.long-horizon.final-verification-challenge.v1",
                "plan_id": int(plan["id"]), "project_id": self.project_id,
                "manifest_sha256": str(plan["manifest_sha256"]),
                "checkpoint_head_sha256": str(row["checkpoint_head_sha256"]),
                "authority_id": str(row["authority_id"]), "verifier_id": str(row["verifier_id"]),
                "verifier_runtime_sha256": str(row["verifier_runtime_sha256"]),
                "evidence_sha256": str(row["evidence_sha256"]), "passed": bool(row["passed"]),
            }
            if str(row["verifier_id"]) != authority["verifier_id"] or str(row["verifier_runtime_sha256"]) != authority["runtime_sha256"]:
                raise LongHorizonIntegrityError("final verifier authority binding mismatch", plan_id=int(plan["id"]), reason="verification_authority_invalid")
            self._verify_authority_signature(authority["public_key"], challenge, str(row["signature_sha256"]))
        if str(plan["status"]) == "complete":
            match = next((row for row in rows if int(row["id"]) == int(plan["final_verification_id"] or 0)), None)
            if match is None or int(match["passed"]) != 1:
                raise LongHorizonIntegrityError("completed plan lacks passing independent verification", plan_id=int(plan["id"]), reason="completion_without_verification")

    def _require_runnable_locked(self, plan: sqlite3.Row, manifest: WorkflowManifest, now: datetime) -> None:
        floor = _parse_time(str(plan["clock_floor_at"]))
        if now < floor:
            raise LongHorizonIntegrityError("system clock moved behind the durable floor", plan_id=int(plan["id"]), reason="clock_rollback")
        self.db.execute("UPDATE long_horizon_plans SET clock_floor_at=? WHERE id=?", (_iso(now), int(plan["id"])))
        control = self.db.execute("SELECT state FROM runtime_control WHERE id=1").fetchone()
        if control is None or str(control["state"]) != "running":
            raise LongHorizonStateError("global pause/stop dominates workflow execution")
        if str(plan["status"]) != "active":
            raise LongHorizonStateError(f"plan is {plan['status']}")
        if (now - _parse_time(str(plan["created_at"]))).total_seconds() > manifest.budget.elapsed_seconds:
            raise LongHorizonBudgetError("workflow elapsed-time budget is exhausted")
        self._check_plan_totals(plan, manifest)

    def _check_plan_totals(self, plan: sqlite3.Row, manifest: WorkflowManifest) -> None:
        budget = manifest.budget.to_payload()
        for key in USAGE_KEYS:
            if int(plan[f"used_{key}"]) > int(budget[key]):
                raise LongHorizonIntegrityError("stored usage exceeds manifest budget", plan_id=int(plan["id"]), reason="budget_tampered")
        if int(plan["used_retries"]) > budget["retries"]:
            raise LongHorizonIntegrityError("stored retries exceed manifest budget", plan_id=int(plan["id"]), reason="budget_tampered")

    def _consume_retry_locked(
        self,
        plan: sqlite3.Row,
        manifest: WorkflowManifest,
        stage: sqlite3.Row,
        *,
        reason: str,
    ) -> dict[str, Any]:
        spec = WorkflowStageSpec.from_value(_strict_json_loads(str(stage["stage_json"]), "stage"))
        plan_retries = int(plan["used_retries"])
        stage_retries = int(
            self.db.execute(
                "SELECT COUNT(*) FROM long_horizon_retry_receipts WHERE stage_id=?",
                (int(stage["id"]),),
            ).fetchone()[0]
        )
        if plan_retries + 1 > manifest.budget.retries or stage_retries + 1 > spec.budget.retries:
            self.db.execute(
                "UPDATE long_horizon_plans SET status='failed', updated_at=? WHERE id=?", (_iso(), int(plan["id"])),
            )
            self.db.execute("UPDATE long_horizon_stages SET status='failed' WHERE id=?", (int(stage["id"]),))
            raise LongHorizonBudgetError("retry budget is exhausted")
        if reason not in {
            "lease_expired", "pause_reclaim", "mutation_not_applied",
            "reconciliation_not_applied",
        }:
            raise LongHorizonValidationError("retry reason is not in the closed vocabulary")
        previous = plan["retry_head_sha256"]
        stamp = _iso()
        material = {
            "schema": "jarvis.long-horizon.retry-receipt.v1",
            "plan_id": int(plan["id"]),
            "manifest_sha256": str(plan["manifest_sha256"]),
            "stage_id": int(stage["id"]),
            "stage_sha256": str(stage["stage_sha256"]),
            "attempt_number": int(stage["attempt_count"]),
            "reason": reason,
            "previous_sha256": previous,
            "created_at": stamp,
        }
        receipt_json = _canonical(material)
        receipt_sha = hashlib.sha256(receipt_json.encode("utf-8")).hexdigest()
        cursor = self.db.execute(
            """INSERT INTO long_horizon_retry_receipts(
                   plan_id, stage_id, created_at, attempt_number, reason,
                   previous_sha256, receipt_json, receipt_sha256, receipt_mac_sha256
               ) VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                int(plan["id"]), int(stage["id"]), stamp,
                int(stage["attempt_count"]), reason, previous, receipt_json, receipt_sha,
                self._mac(receipt_json),
            ),
        )
        self.db.execute(
            "UPDATE long_horizon_plans SET used_retries=used_retries+1, "
            "retry_head_sha256=?, updated_at=? WHERE id=?",
            (receipt_sha, stamp, int(plan["id"])),
        )
        return {"retry_id": int(cursor.lastrowid), "receipt_sha256": receipt_sha}

    def _claimed_stage_locked(self, plan_id: int, stage_id: int, worker_id: str, lease_token: str) -> sqlite3.Row:
        owner = _require_identity(worker_id, "worker_id")
        token_sha = hashlib.sha256(str(lease_token).encode("ascii")).hexdigest()
        stage = self.db.execute(
            "SELECT * FROM long_horizon_stages WHERE id=? AND plan_id=?",
            (_require_id(stage_id, "stage_id"), _require_id(plan_id, "plan_id")),
        ).fetchone()
        if stage is None or str(stage["status"]) != "claimed":
            raise LongHorizonStateError("stage is not claimed")
        if str(stage["claim_owner"]) != owner or not secrets.compare_digest(str(stage["lease_token_sha256"]), token_sha):
            raise LongHorizonStateError("stage lease does not belong to this worker")
        if _parse_time(str(stage["lease_expires_at"])) <= _utc_now():
            raise LongHorizonStateError("stage lease expired")
        return stage

    def _validated_usage(self, usage: Mapping[str, Any]) -> dict[str, int]:
        material = _closed_mapping(usage, frozenset(USAGE_KEYS), "stage usage")
        result: dict[str, int] = {}
        for key in USAGE_KEYS:
            value = material[key]
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise LongHorizonValidationError(f"usage {key} must be a nonnegative integer")
            result[key] = value
        return result

    def _check_usage_locked(
        self,
        plan: sqlite3.Row,
        manifest: WorkflowManifest,
        stage: sqlite3.Row,
        usage: Mapping[str, int],
    ) -> None:
        stage_spec = WorkflowStageSpec.from_value(_strict_json_loads(str(stage["stage_json"]), "stage"))
        for key in USAGE_KEYS:
            if int(stage[f"used_{key}"]) + usage[key] > int(stage_spec.budget.to_payload()[key]):
                raise LongHorizonBudgetError(f"stage {key} budget would be exceeded")
            if int(plan[f"used_{key}"]) + usage[key] > int(manifest.budget.to_payload()[key]):
                raise LongHorizonBudgetError(f"workflow {key} budget would be exceeded")

    def _reservation_for_claim_locked(self, stage: sqlite3.Row) -> sqlite3.Row | None:
        if stage["active_reservation_id"] is None:
            return None
        row = self.db.execute(
            "SELECT * FROM long_horizon_usage_reservations WHERE id=? AND stage_id=?",
            (int(stage["active_reservation_id"]), int(stage["id"])),
        ).fetchone()
        if row is None or int(row["attempt_number"]) != int(stage["attempt_count"]):
            raise LongHorizonIntegrityError(
                "active usage reservation binding is invalid",
                plan_id=int(stage["plan_id"]),
                reason="usage_reservation_tampered",
            )
        return row

    def _reserve_usage_locked(
        self,
        plan: sqlite3.Row,
        manifest: WorkflowManifest,
        stage: sqlite3.Row,
        usage: Mapping[str, int],
    ) -> dict[str, Any]:
        self._check_usage_locked(plan, manifest, stage, usage)
        previous = plan["usage_head_sha256"]
        stamp = _iso()
        material = {
            "schema": "jarvis.long-horizon.usage-reservation.v1",
            "plan_id": int(plan["id"]),
            "manifest_sha256": str(plan["manifest_sha256"]),
            "stage_id": int(stage["id"]),
            "stage_sha256": str(stage["stage_sha256"]),
            "attempt_number": int(stage["attempt_count"]),
            "usage": dict(usage),
            "previous_sha256": previous,
            "created_at": stamp,
        }
        receipt_json = _canonical(material)
        receipt_sha = hashlib.sha256(receipt_json.encode("utf-8")).hexdigest()
        cursor = self.db.execute(
            """INSERT INTO long_horizon_usage_reservations(
                   plan_id, stage_id, created_at, attempt_number, usage_json,
                   previous_sha256, receipt_json, receipt_sha256, receipt_mac_sha256
               ) VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                int(plan["id"]), int(stage["id"]), stamp, int(stage["attempt_count"]),
                _canonical(dict(usage)), previous, receipt_json, receipt_sha,
                self._mac(receipt_json),
            ),
        )
        reservation_id = int(cursor.lastrowid)
        self.db.execute(
            """UPDATE long_horizon_stages SET active_reservation_id=?,
                   used_elapsed_seconds=used_elapsed_seconds+?,
                   used_tool_calls=used_tool_calls+?,
                   used_model_calls=used_model_calls+?,
                   used_prompt_tokens=used_prompt_tokens+?,
                   used_completion_tokens=used_completion_tokens+?
               WHERE id=?""",
            (reservation_id, *(usage[key] for key in USAGE_KEYS), int(stage["id"])),
        )
        self.db.execute(
            """UPDATE long_horizon_plans SET usage_head_sha256=?,
                   used_elapsed_seconds=used_elapsed_seconds+?,
                   used_tool_calls=used_tool_calls+?,
                   used_model_calls=used_model_calls+?,
                   used_prompt_tokens=used_prompt_tokens+?,
                   used_completion_tokens=used_completion_tokens+?, updated_at=?
               WHERE id=?""",
            (
                receipt_sha, *(usage[key] for key in USAGE_KEYS), stamp, int(plan["id"]),
            ),
        )
        return {
            "reservation_id": reservation_id,
            "receipt_sha256": receipt_sha,
            "usage": dict(usage),
            "usage_json": _canonical(dict(usage)),
        }

    def _record_mutation_event(
        self,
        plan_id: int,
        stage_id: int,
        *,
        worker_id: str,
        lease_token: str,
        actor_id: str,
        event_type: str,
        outcome: str | None,
        evidence_sha256: str | None,
    ) -> dict[str, Any]:
        normalized = _require_id(plan_id, "plan_id")
        actor = _require_identity(actor_id, "actor_id")
        with self._transaction():
            plan, manifest, _ = self._validate_plan_locked(normalized)
            self._require_runnable_locked(plan, manifest, _utc_now())
            stage = self._claimed_stage_locked(normalized, stage_id, worker_id, lease_token)
            if str(stage["mutation_kind"]) == "none":
                raise LongHorizonStateError("stage is not a mutation stage")
            state = str(stage["mutation_state"])
            if event_type == "intent" and state not in {"none", "reconciled_not_applied", "result_not_applied"}:
                existing = self.db.execute(
                    "SELECT receipt_sha256, effect_key, generation, receipt_json FROM long_horizon_mutation_receipts "
                    "WHERE stage_id=? AND generation=? AND event_type='intent'",
                    (int(stage["id"]), int(stage["attempt_count"])),
                ).fetchone()
                if existing is not None:
                    material = _strict_json_loads(str(existing["receipt_json"]), "mutation receipt")
                    if material.get("actor_id") != actor:
                        raise LongHorizonIntegrityError(
                            "mutation intent replay changes executor",
                            plan_id=normalized,
                            reason="mutation_replay",
                        )
                    return dict(existing)
                raise LongHorizonStateError("mutation intent already exists")
            if event_type == "authorization" and state != "intent_recorded":
                raise LongHorizonStateError("effect authorization requires an intent receipt")
            if event_type == "result" and state != "effect_in_progress":
                if state == f"result_{outcome}":
                    existing = self.db.execute(
                        "SELECT receipt_sha256, effect_key, generation, receipt_json "
                        "FROM long_horizon_mutation_receipts WHERE stage_id=? AND generation=? "
                        "AND event_type='result'",
                        (int(stage["id"]), int(stage["attempt_count"])),
                    ).fetchone()
                    if existing is not None:
                        material = _strict_json_loads(str(existing["receipt_json"]), "mutation receipt")
                        if (
                            material.get("actor_id") == actor
                            and material.get("outcome") == outcome
                            and material.get("evidence_sha256") == evidence_sha256
                        ):
                            return dict(existing)
                        raise LongHorizonIntegrityError(
                            "mutation result replay changes bound evidence",
                            plan_id=normalized,
                            reason="mutation_replay",
                        )
                raise LongHorizonStateError("mutation result requires an intent receipt")
            receipt = self._append_mutation_receipt_locked(
                normalized, stage, actor, event_type, outcome, evidence_sha256,
            )
            if event_type == "intent":
                next_state = "intent_recorded"
            elif event_type == "authorization":
                next_state = "effect_authorized"
            else:
                next_state = f"result_{outcome}"
                if outcome == "uncertain":
                    self.db.execute(
                        "UPDATE long_horizon_stages SET status='awaiting_reconciliation', "
                        "claim_owner=NULL, lease_token_sha256=NULL, lease_expires_at=NULL WHERE id=?",
                        (int(stage["id"]),),
                    )
                elif outcome == "not_applied":
                    self.db.execute(
                        "UPDATE long_horizon_stages SET mutation_state='result_not_applied' WHERE id=?",
                        (int(stage["id"]),),
                    )
                    self._consume_retry_locked(plan, manifest, stage, reason="mutation_not_applied")
                    self.db.execute(
                        "UPDATE long_horizon_stages SET status='pending', claim_owner=NULL, "
                        "lease_token_sha256=NULL, lease_expires_at=NULL WHERE id=?",
                        (int(stage["id"]),),
                    )
            self.db.execute(
                "UPDATE long_horizon_stages SET mutation_state=? WHERE id=?",
                (next_state, int(stage["id"])),
            )
            return receipt

    def _append_mutation_receipt_locked(
        self,
        plan_id: int,
        stage: sqlite3.Row,
        actor: str,
        event_type: str,
        outcome: str | None,
        evidence_sha256: str | None,
        authority_id: str | None = None,
        runtime_sha256: str | None = None,
        signature_sha256: str | None = None,
    ) -> dict[str, Any]:
        generation = int(stage["attempt_count"])
        reconciliation_round = 0
        if event_type == "reconciliation":
            reconciliation_round = int(self.db.execute(
                "SELECT COALESCE(MAX(reconciliation_round),0)+1 FROM long_horizon_mutation_receipts "
                "WHERE stage_id=? AND generation=? AND event_type='reconciliation'",
                (int(stage["id"]), generation),
            ).fetchone()[0])
        previous_row = self.db.execute(
            "SELECT receipt_sha256 FROM long_horizon_mutation_receipts WHERE stage_id=? ORDER BY id DESC LIMIT 1",
            (int(stage["id"]),),
        ).fetchone()
        previous = None if previous_row is None else str(previous_row["receipt_sha256"])
        stamp = _iso()
        material = {
            "schema": MUTATION_RECEIPT_SCHEMA,
            "plan_id": plan_id,
            "stage_id": int(stage["id"]),
            "stage_sha256": str(stage["stage_sha256"]),
            "generation": generation,
            "reconciliation_round": reconciliation_round,
            "event_type": event_type,
            "outcome": outcome,
            "effect_key": str(stage["effect_key"]),
            "actor_id": actor,
            "evidence_sha256": evidence_sha256,
            "authority_id": authority_id,
            "runtime_sha256": runtime_sha256,
            "signature_sha256": signature_sha256,
            "previous_sha256": previous,
            "created_at": stamp,
        }
        receipt_json = _canonical(material)
        receipt_sha = hashlib.sha256(receipt_json.encode("utf-8")).hexdigest()
        try:
            cursor = self.db.execute(
                """INSERT INTO long_horizon_mutation_receipts(
                       plan_id, stage_id, created_at, generation, reconciliation_round, event_type, outcome,
                       effect_key, actor_id, evidence_sha256, previous_sha256,
                       authority_id, runtime_sha256, signature_sha256,
                       receipt_json, receipt_sha256, receipt_mac_sha256
                   ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    plan_id, int(stage["id"]), stamp, generation, reconciliation_round, event_type, outcome,
                    str(stage["effect_key"]), actor, evidence_sha256, previous,
                    authority_id, runtime_sha256, signature_sha256,
                    receipt_json, receipt_sha, self._mac(receipt_json),
                ),
            )
        except sqlite3.IntegrityError:
            existing = self.db.execute(
                "SELECT id, receipt_sha256, effect_key, generation FROM long_horizon_mutation_receipts "
                "WHERE stage_id=? AND generation=? AND event_type=? AND reconciliation_round=?",
                (int(stage["id"]), generation, event_type, reconciliation_round),
            ).fetchone()
            if existing is None or str(existing["receipt_sha256"]) != receipt_sha:
                raise LongHorizonIntegrityError("mutation receipt replay conflicts", plan_id=plan_id, reason="mutation_replay")
            return dict(existing)
        return {"receipt_id": int(cursor.lastrowid), "receipt_sha256": receipt_sha, "effect_key": str(stage["effect_key"]), "generation": generation, "reconciliation_round": reconciliation_round}

    def _set_plan_control(self, plan_id: int, state: str, reason_sha256: str) -> dict[str, Any]:
        normalized = _require_id(plan_id, "plan_id")
        reason = _require_sha(reason_sha256, "reason_sha256")
        with self._transaction():
            plan, manifest, stages = self._validate_plan_locked(normalized)
            current = str(plan["status"])
            if current in {"complete", "cancelled", "failed", "quarantined"}:
                if current == state:
                    return self._status_locked(plan, manifest, stages)
                raise LongHorizonStateError(f"terminal plan state {current} cannot change")
            if state == "paused":
                ambiguous = {
                    "intent_recorded", "effect_authorized", "effect_in_progress",
                    "result_applied", "result_uncertain", "reconciled_uncertain",
                }
                for claimed in stages:
                    if str(claimed["status"]) == "claimed" and str(claimed["mutation_state"]) not in ambiguous:
                        self._consume_retry_locked(
                            plan, manifest, claimed, reason="pause_reclaim"
                        )
            column = "pause_reason_sha256" if state == "paused" else "cancelled_reason_sha256"
            self.db.execute(
                f"UPDATE long_horizon_plans SET status=?, {column}=?, updated_at=? WHERE id=?",
                (state, reason, _iso(), normalized),
            )
            # A cancelled plan still needs its already-intended/authorized
            # mutation reconciled; cancellation must not erase ambiguity.
            terminal_stage_state = "awaiting_reconciliation"
            self.db.execute(
                "UPDATE long_horizon_stages SET status=?, claim_owner=NULL, lease_token_sha256=NULL, "
                "lease_expires_at=NULL WHERE plan_id=? AND status='claimed' AND mutation_state IN "
                "('intent_recorded','effect_authorized','effect_in_progress','result_applied','result_uncertain','reconciled_uncertain')",
                (terminal_stage_state, normalized),
            )
            self.db.execute(
                "UPDATE long_horizon_stages SET status=?, claim_owner=NULL, lease_token_sha256=NULL, "
                "lease_expires_at=NULL WHERE plan_id=? AND status IN ('pending','claimed')",
                ("cancelled" if state == "cancelled" else "pending", normalized),
            )
            plan = self.db.execute("SELECT * FROM long_horizon_plans WHERE id=?", (normalized,)).fetchone()
            stages = self.db.execute("SELECT * FROM long_horizon_stages WHERE plan_id=? ORDER BY ordinal", (normalized,)).fetchall()
            return self._status_locked(plan, manifest, stages)

    def _claim_payload(self, stage: sqlite3.Row, token: str) -> dict[str, Any]:
        return {
            "plan_id": int(stage["plan_id"]),
            "stage_id": int(stage["id"]),
            "stage_key": str(stage["stage_key"]),
            "ordinal": int(stage["ordinal"]),
            "stage_type": str(stage["stage_type"]),
            "mutation_kind": str(stage["mutation_kind"]),
            "status": str(stage["status"]),
            "lease_token": token,
            "lease_expires_at": str(stage["lease_expires_at"]),
            "idempotency_key": str(stage["idempotency_key"]),
            "effect_key": str(stage["effect_key"]),
            "attempt_count": int(stage["attempt_count"]),
            "mutation_state": str(stage["mutation_state"]),
            "budget": WorkflowStageSpec.from_value(_strict_json_loads(str(stage["stage_json"]), "stage")).budget.to_payload(),
        }

    def _status_locked(
        self,
        plan: sqlite3.Row,
        manifest: WorkflowManifest,
        stages: Sequence[sqlite3.Row],
    ) -> dict[str, Any]:
        budget = manifest.budget.to_payload()
        usage = {key: int(plan[f"used_{key}"]) for key in (*USAGE_KEYS, "retries")}
        remaining = {key: max(0, budget[key] - usage[key]) for key in budget}
        current = next((stage for stage in stages if str(stage["status"]) == "claimed"), None)
        verification = None
        if plan["final_verification_id"] is not None:
            row = self.db.execute(
                "SELECT verifier_id, passed, verification_sha256, receipt_sha256 FROM long_horizon_final_verifications WHERE id=?",
                (int(plan["final_verification_id"]),),
            ).fetchone()
            verification = dict(row) if row is not None else None
        mutation_states = {
            str(stage["stage_key"]): str(stage["mutation_state"])
            for stage in stages if str(stage["mutation_kind"]) != "none"
        }
        return {
            "schema": "jarvis.long-horizon.status.v1",
            "plan_id": int(plan["id"]),
            "project_id": int(plan["project_id"]),
            "conversation_id": int(plan["conversation_id"]),
            "task_id": int(plan["task_id"]),
            "status": str(plan["status"]),
            "manifest_sha256": str(plan["manifest_sha256"]),
            "stage_count": int(plan["stage_count"]),
            "next_stage_ordinal": int(plan["next_stage_ordinal"]),
            "completed_stages": sum(str(stage["status"]) == "complete" for stage in stages),
            "budget": budget,
            "usage": usage,
            "remaining": remaining,
            "current_claim": None if current is None else {
                "stage_id": int(current["id"]),
                "stage_key": str(current["stage_key"]),
                "ordinal": int(current["ordinal"]),
                "claim_owner": str(current["claim_owner"]),
                "lease_expires_at": str(current["lease_expires_at"]),
            },
            "checkpoint_head_sha256": plan["checkpoint_head_sha256"],
            "mutation_state": mutation_states,
            "final_verification": verification,
            "quarantine_reason": plan["quarantine_reason"],
        }

    def _evidence_stage(self, stage: sqlite3.Row) -> dict[str, Any]:
        return {
            "stage_id": int(stage["id"]),
            "stage_key": str(stage["stage_key"]),
            "ordinal": int(stage["ordinal"]),
            "stage_type": str(stage["stage_type"]),
            "mutation_kind": str(stage["mutation_kind"]),
            "stage_sha256": str(stage["stage_sha256"]),
            "status": str(stage["status"]),
            "attempt_count": int(stage["attempt_count"]),
            "idempotency_key": str(stage["idempotency_key"]),
            "effect_key": str(stage["effect_key"]),
            "executor_id": stage["executor_id"],
            "outcome_sha256": stage["outcome_sha256"],
            "artifact_sha256": stage["artifact_sha256"],
            "mutation_state": str(stage["mutation_state"]),
            "usage": {key: int(stage[f"used_{key}"]) for key in USAGE_KEYS},
        }


__all__ = [
    "WorkflowBudget", "WorkflowStageSpec", "WorkflowManifest", "LongHorizonStore",
    "LongHorizonError", "LongHorizonValidationError",
    "LongHorizonStateError", "LongHorizonBudgetError", "LongHorizonIntegrityError",
    "migrate_long_horizon_v40", "parse_manifest_json",
]
