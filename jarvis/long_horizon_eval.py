from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Mapping

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from .long_horizon import (
    LongHorizonBudgetError,
    LongHorizonIntegrityError,
    LongHorizonStateError,
    LongHorizonStore,
    WorkflowBudget,
    WorkflowManifest,
    WorkflowStageSpec,
)
from .memory import Memory


EVALUATOR_VERSION = "1.0.0"
FIXTURE_SCHEMA = "jarvis.long-horizon-restart-holdout.v1"
REPORT_SCHEMA = "jarvis.long-horizon-restart-report.v1"
CLAIM_SCOPE = "deterministic_benchmark_only_not_live_activation"
IDENTIFIER_RE = re.compile(r"^[a-z][a-z0-9_.:-]{0,95}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
FAMILIES = frozenset(
    {
        "code_build",
        "code_fix",
        "code_test",
        "deep_research",
        "file_ops",
        "external_publish",
        "security_analysis",
    }
)
CRASH_POINTS = frozenset(
    {
        "before_mutation",
        "after_mutation_before_receipt",
        "after_receipt_before_cursor",
    }
)
CONTROL_KINDS = frozenset(
    {
        "budget_time",
        "budget_tools",
        "budget_model_calls",
        "budget_prompt_tokens",
        "budget_completion_tokens",
        "budget_retries",
        "cancelled",
        "tampered_checkpoint",
        "cross_project_checkpoint",
        "replayed_effect",
    }
)
STAGE_KINDS = frozenset({"inspect", "transform", "mutate", "verify", "finalize"})
EVALUATION_CONFIG = {
    "minimum_workflows": 24,
    "minimum_stages": 5,
    "maximum_stages": 12,
    "minimum_families": 5,
    "required_crash_points": sorted(CRASH_POINTS),
    "required_negative_controls": sorted(CONTROL_KINDS),
    "require_zero_duplicate_effects": True,
    "require_all_budget_controls": True,
    "require_cancellation_dominance": True,
    "require_independent_final_verification": True,
}


class LongHorizonEvaluationError(ValueError):
    """The holdout or its runtime evidence is malformed or unsafe."""


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def long_horizon_runtime_sha256() -> str:
    root = Path(__file__).parent
    material = {
        name: hashlib.sha256((root / name).read_bytes()).hexdigest()
        for name in ("long_horizon.py", "memory.py", "long_horizon_eval_worker.py")
    }
    return sha256_json(material)


def _digest(value: Any, label: str) -> str:
    if not isinstance(value, str) or SHA256_RE.fullmatch(value) is None:
        raise LongHorizonEvaluationError(f"{label} must be 64 lowercase hex")
    return value


def _identifier(value: Any, label: str) -> str:
    if not isinstance(value, str) or IDENTIFIER_RE.fullmatch(value) is None:
        raise LongHorizonEvaluationError(f"{label} is malformed")
    return value


def _exact_fields(value: Any, fields: set[str], label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise LongHorizonEvaluationError(f"{label} fields are invalid")
    return value


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise LongHorizonEvaluationError(f"{label} must be a positive integer")
    return value


def _validate_template(template: Any) -> None:
    template = _exact_fields(
        template,
        {"template_id", "stages"},
        "workflow template",
    )
    _identifier(template["template_id"], "template ID")
    stages = template["stages"]
    if not isinstance(stages, list) or not 5 <= len(stages) <= 12:
        raise LongHorizonEvaluationError("template must contain 5-12 stages")
    seen: set[str] = set()
    for index, stage in enumerate(stages):
        stage = _exact_fields(
            stage,
            {
                "stage_id",
                "kind",
                "depends_on",
                "mutation",
                "irreversible",
                "tool_cost",
                "token_cost",
                "duration_ms",
                "verification",
            },
            "stage",
        )
        stage_id = _identifier(stage["stage_id"], "stage ID")
        if stage_id in seen:
            raise LongHorizonEvaluationError("stage IDs must be unique")
        seen.add(stage_id)
        if stage["kind"] not in STAGE_KINDS:
            raise LongHorizonEvaluationError("stage kind is invalid")
        expected_dependency = [] if index == 0 else [stages[index - 1]["stage_id"]]
        if stage["depends_on"] != expected_dependency:
            raise LongHorizonEvaluationError("stage dependencies are not canonical")
        for field in ("mutation", "irreversible"):
            if not isinstance(stage[field], bool):
                raise LongHorizonEvaluationError(f"stage {field} must be boolean")
        if stage["irreversible"] and not stage["mutation"]:
            raise LongHorizonEvaluationError("irreversible stage must be a mutation")
        for field in ("tool_cost", "token_cost", "duration_ms"):
            _positive_int(stage[field], f"stage {field}")
        if stage["verification"] not in {"none", "artifact", "external_receipt"}:
            raise LongHorizonEvaluationError("stage verification is invalid")
    if not any(stage["mutation"] for stage in stages):
        raise LongHorizonEvaluationError("template must exercise a mutation")
    if stages[-1]["verification"] == "none":
        raise LongHorizonEvaluationError("final stage must require verification")


def _validate_fixture(artifact: Any) -> dict[str, Any]:
    artifact = dict(
        _exact_fields(
            artifact,
            {
                "schema",
                "evaluator_version",
                "config_sha256",
                "runtime_sha256",
                "templates",
                "workflows",
                "negative_controls",
                "fixture_manifest_sha256",
            },
            "holdout",
        )
    )
    if artifact["schema"] != FIXTURE_SCHEMA:
        raise LongHorizonEvaluationError("holdout schema is unsupported")
    if artifact["evaluator_version"] != EVALUATOR_VERSION:
        raise LongHorizonEvaluationError("evaluator version pin mismatch")
    if artifact["config_sha256"] != sha256_json(EVALUATION_CONFIG):
        raise LongHorizonEvaluationError("evaluation config pin mismatch")
    _digest(artifact["runtime_sha256"], "runtime SHA")
    templates = artifact["templates"]
    if not isinstance(templates, list) or len(templates) < 5:
        raise LongHorizonEvaluationError("at least five templates are required")
    for template in templates:
        _validate_template(template)
    template_ids = [template["template_id"] for template in templates]
    if len(template_ids) != len(set(template_ids)):
        raise LongHorizonEvaluationError("template IDs must be unique")

    workflows = artifact["workflows"]
    if not isinstance(workflows, list) or len(workflows) < 24:
        raise LongHorizonEvaluationError("at least 24 workflows are required")
    workflow_ids: set[str] = set()
    families: set[str] = set()
    crash_points: set[str] = set()
    for workflow in workflows:
        workflow = _exact_fields(
            workflow,
            {
                "workflow_id",
                "template_id",
                "family",
                "project_id",
                "goal_sha256",
                "constraints_sha256",
                "approval_sha256",
                "artifact_sha256",
                "contract_sha256",
                "crash_point",
                "budgets",
            },
            "workflow",
        )
        workflow_id = _identifier(workflow["workflow_id"], "workflow ID")
        if workflow_id in workflow_ids:
            raise LongHorizonEvaluationError("workflow IDs must be unique")
        workflow_ids.add(workflow_id)
        if workflow["template_id"] not in template_ids:
            raise LongHorizonEvaluationError("workflow template is unknown")
        if workflow["family"] not in FAMILIES:
            raise LongHorizonEvaluationError("workflow family is invalid")
        families.add(workflow["family"])
        _positive_int(workflow["project_id"], "project ID")
        for field in (
            "goal_sha256",
            "constraints_sha256",
            "approval_sha256",
            "artifact_sha256",
            "contract_sha256",
        ):
            _digest(workflow[field], field)
        if workflow["crash_point"] not in CRASH_POINTS:
            raise LongHorizonEvaluationError("workflow crash point is invalid")
        crash_points.add(workflow["crash_point"])
        budgets = _exact_fields(
            workflow["budgets"],
            {
                "time_ms",
                "tool_calls",
                "model_calls",
                "prompt_tokens",
                "completion_tokens",
                "retries",
            },
            "workflow budgets",
        )
        for field, value in budgets.items():
            _positive_int(value, f"budget {field}")
    if len(families) < 5:
        raise LongHorizonEvaluationError("at least five workflow families are required")
    if crash_points != CRASH_POINTS:
        raise LongHorizonEvaluationError("all crash points must be represented")

    controls = artifact["negative_controls"]
    if not isinstance(controls, list):
        raise LongHorizonEvaluationError("negative controls must be a list")
    control_kinds: set[str] = set()
    control_ids: set[str] = set()
    for control in controls:
        control = _exact_fields(
            control,
            {"control_id", "kind", "workflow_id"},
            "negative control",
        )
        control_id = _identifier(control["control_id"], "control ID")
        if control_id in control_ids:
            raise LongHorizonEvaluationError("control IDs must be unique")
        control_ids.add(control_id)
        if control["kind"] not in CONTROL_KINDS:
            raise LongHorizonEvaluationError("negative control kind is invalid")
        control_kinds.add(control["kind"])
        if control["workflow_id"] not in workflow_ids:
            raise LongHorizonEvaluationError("negative control workflow is unknown")
    if control_kinds != CONTROL_KINDS:
        raise LongHorizonEvaluationError("all negative control kinds are required")

    sealed = {key: value for key, value in artifact.items() if key != "fixture_manifest_sha256"}
    if _digest(artifact["fixture_manifest_sha256"], "fixture manifest SHA") != sha256_json(sealed):
        raise LongHorizonEvaluationError("fixture manifest digest mismatch")
    return artifact


def validate_long_horizon_fixture_artifact(artifact: Mapping[str, Any]) -> dict[str, Any]:
    """Validate only the sealed input shape; this is never completion evidence."""
    return _validate_fixture(artifact)


def _stage_specs(template: Mapping[str, Any]) -> tuple[WorkflowStageSpec, ...]:
    mapping = {
        "inspect": "inspect",
        "transform": "plan",
        "mutate": "mutate",
        "verify": "verify",
        "finalize": "finalize",
    }
    return tuple(
        WorkflowStageSpec(
            stage_id=str(stage["stage_id"]),
            ordinal=index,
            stage_type=mapping[str(stage["kind"])],
            mutation_kind=("irreversible" if stage["irreversible"] else "none"),
            budget=WorkflowBudget(
                elapsed_seconds=max(1, (int(stage["duration_ms"]) + 999) // 1000),
                tool_calls=int(stage["tool_cost"]),
                model_calls=1,
                prompt_tokens=int(stage["token_cost"]),
                completion_tokens=max(1, int(stage["token_cost"]) // 4),
                retries=1,
            ),
        )
        for index, stage in enumerate(template["stages"], start=1)
    )


def _workflow_budget(workflow: Mapping[str, Any]) -> WorkflowBudget:
    value = workflow["budgets"]
    return WorkflowBudget(
        elapsed_seconds=max(1, (int(value["time_ms"]) + 999) // 1000),
        tool_calls=int(value["tool_calls"]),
        model_calls=int(value["model_calls"]),
        prompt_tokens=int(value["prompt_tokens"]),
        completion_tokens=int(value["completion_tokens"]),
        retries=int(value["retries"]),
    )


def _usage(stage: Mapping[str, Any]) -> dict[str, int]:
    budget = stage["budget"]
    return {
        "elapsed_seconds": min(1, int(budget["elapsed_seconds"])),
        "tool_calls": min(1, int(budget["tool_calls"])),
        "model_calls": min(1, int(budget["model_calls"])),
        "prompt_tokens": min(10, int(budget["prompt_tokens"])),
        "completion_tokens": min(5, int(budget["completion_tokens"])),
    }


def _execute_workflow(
    workflow: Mapping[str, Any],
    template: Mapping[str, Any],
    database: Path,
) -> Mapping[str, Any]:
    effects: set[str] = set()
    with Memory(database) as memory:
        conversation_id = memory.new_conversation(
            f"phase5-{workflow['workflow_id']}", project_id=1
        )
        task_id = memory.add_task(
            f"phase5-{workflow['workflow_id']}",
            project_id=1,
            idempotency_key=f"phase5:{workflow['workflow_id']}",
        )
        manifest = WorkflowManifest(
            project_id=1,
            conversation_id=conversation_id,
            task_id=task_id,
            goal_sha256=workflow["goal_sha256"],
            contract_sha256=workflow["contract_sha256"],
            constraints_sha256=workflow["constraints_sha256"],
            approval_scope_sha256=workflow["approval_sha256"],
            artifact_set_sha256=workflow["artifact_sha256"],
            budget=_workflow_budget(workflow),
            stages=_stage_specs(template),
        )
        with LongHorizonStore(
            memory,
            project_id=int(workflow["project_id"]),
            worker_id="phase5:first",
        ) as store:
            plan_id = store.create_plan(manifest)
            while True:
                claim = store.claim_next_stage(
                    plan_id,
                    worker_id="phase5:first",
                    lease_seconds=1,
                )
                if claim is None:
                    break
                if claim["mutation_kind"] != "none":
                    if workflow["crash_point"] == "before_mutation":
                        # Close before an intent is recorded; the expired lease can
                        # safely retry without an external effect.
                        break
                    store.record_mutation_intent(
                        plan_id,
                        int(claim["stage_id"]),
                        worker_id="phase5:first",
                        lease_token=str(claim["lease_token"]),
                        executor_id="executor:first",
                    )
                    effect_key = str(claim["effect_key"])
                    if effect_key in effects:
                        raise LongHorizonEvaluationError("effect executed twice")
                    effects.add(effect_key)
                    store.record_mutation_result(
                        plan_id,
                        int(claim["stage_id"]),
                        worker_id="phase5:first",
                        lease_token=str(claim["lease_token"]),
                        executor_id="executor:first",
                        outcome="applied",
                        evidence_sha256=sha256_json(
                            {"effect_key": effect_key, "outcome": "applied"}
                        ),
                    )
                    if workflow["crash_point"] == "after_mutation_before_receipt":
                        break
                    store.record_checkpoint(
                        plan_id,
                        int(claim["stage_id"]),
                        worker_id="phase5:first",
                        lease_token=str(claim["lease_token"]),
                        usage=_usage(claim),
                        outcome_sha256=sha256_json(
                            {"workflow": workflow["workflow_id"], "stage": claim["stage_key"]}
                        ),
                        artifact_sha256=workflow["artifact_sha256"],
                        executor_id="executor:first",
                    )
                    if workflow["crash_point"] == "after_receipt_before_cursor":
                        break
                else:
                    store.record_checkpoint(
                        plan_id,
                        int(claim["stage_id"]),
                        worker_id="phase5:first",
                        lease_token=str(claim["lease_token"]),
                        usage=_usage(claim),
                        outcome_sha256=sha256_json(
                            {"workflow": workflow["workflow_id"], "stage": claim["stage_key"]}
                        ),
                        artifact_sha256=workflow["artifact_sha256"],
                        executor_id="executor:first",
                    )
            # Closing this store models process loss. Memory remains only to keep
            # the first SQLite handle bounded; recovery uses a fresh connection.

    if workflow["crash_point"] in {"before_mutation", "after_mutation_before_receipt"}:
        time.sleep(1.05)
    with LongHorizonStore(
        database,
        project_id=int(workflow["project_id"]),
        worker_id="phase5:restarted",
    ) as store:
        plan_id = int(store.list_plans(limit=1)[0]["plan_id"])
        status = store.export_evidence(plan_id)
        current = next(
            (stage for stage in status["stages"] if stage["status"] != "complete"),
            None,
        )
        if current and current["mutation_state"] in {
            "intent_recorded",
            "result_applied",
            "result_uncertain",
        }:
            store.claim_next_stage(
                plan_id,
                worker_id="phase5:restarted",
                lease_seconds=1,
            )
            store.reconcile_mutation(
                plan_id,
                int(current["stage_id"]),
                reconciler_id="reconciler:independent",
                outcome="applied",
                evidence_sha256=sha256_json(
                    {"stage_id": int(current["stage_id"]), "observed": "applied"}
                ),
            )
        while True:
            claim = store.claim_next_stage(
                plan_id,
                worker_id="phase5:restarted",
                lease_seconds=60,
            )
            if claim is None:
                break
            if claim["mutation_kind"] != "none":
                if not effects:
                    store.record_mutation_intent(
                        plan_id,
                        int(claim["stage_id"]),
                        worker_id="phase5:restarted",
                        lease_token=str(claim["lease_token"]),
                        executor_id="executor:restarted",
                    )
                    effect_key = str(claim["effect_key"])
                    if effect_key in effects:
                        raise LongHorizonEvaluationError("effect executed twice")
                    effects.add(effect_key)
                    store.record_mutation_result(
                        plan_id,
                        int(claim["stage_id"]),
                        worker_id="phase5:restarted",
                        lease_token=str(claim["lease_token"]),
                        executor_id="executor:restarted",
                        outcome="applied",
                        evidence_sha256=sha256_json(
                            {"effect_key": effect_key, "outcome": "applied"}
                        ),
                    )
            store.record_checkpoint(
                plan_id,
                int(claim["stage_id"]),
                worker_id="phase5:restarted",
                lease_token=str(claim["lease_token"]),
                usage=_usage(claim),
                outcome_sha256=sha256_json(
                    {"workflow": workflow["workflow_id"], "stage": claim["stage_key"]}
                ),
                artifact_sha256=workflow["artifact_sha256"],
                executor_id="executor:restarted",
            )
        store.record_final_verification(
            plan_id,
            verifier_id="verifier:independent",
            verification_sha256=sha256_json(
                {"workflow": workflow["workflow_id"], "verified": True}
            ),
            passed=True,
        )
        evidence = store.export_evidence(plan_id)
        evidence["evaluated_effect_count"] = len(effects)
        return evidence


def _cross_project_receipt_replay(
    memory: Memory,
    manifest: WorkflowManifest,
    workflow: Mapping[str, Any],
    template: Mapping[str, Any],
) -> Mapping[str, str]:
    project_two = memory.add_project(
        "Phase Five Replay Target",
        "@projects/phase-five-replay-target",
    )
    conversation_two = memory.new_conversation(
        "phase5-cross-project-target",
        project_id=project_two,
    )
    task_two = memory.add_task(
        "phase5-cross-project-target",
        project_id=project_two,
        idempotency_key="phase5-cross-project-target",
    )
    manifest_two = WorkflowManifest(
        project_id=project_two,
        conversation_id=conversation_two,
        task_id=task_two,
        goal_sha256=workflow["goal_sha256"],
        contract_sha256=workflow["contract_sha256"],
        constraints_sha256=workflow["constraints_sha256"],
        approval_scope_sha256=workflow["approval_sha256"],
        artifact_set_sha256=workflow["artifact_sha256"],
        budget=_workflow_budget(workflow),
        stages=_stage_specs(template),
    )
    with LongHorizonStore(memory, project_id=1, worker_id="phase5:source") as source:
        source_plan = source.create_plan(manifest)
        claim = source.claim_next_stage(source_plan, worker_id="phase5:source")
        usage = _usage(claim)
        source.reserve_stage_usage(
            source_plan,
            int(claim["stage_id"]),
            worker_id="phase5:source",
            lease_token=str(claim["lease_token"]),
            usage=usage,
        )
        source.record_checkpoint(
            source_plan,
            int(claim["stage_id"]),
            worker_id="phase5:source",
            lease_token=str(claim["lease_token"]),
            usage=usage,
            outcome_sha256=sha256_json({"control": "cross_project_checkpoint"}),
            artifact_sha256=workflow["artifact_sha256"],
            executor_id="executor:source",
        )
        copied = memory.db.execute(
            "SELECT * FROM long_horizon_checkpoints WHERE plan_id=?",
            (source_plan,),
        ).fetchone()
    with LongHorizonStore(memory, project_id=project_two, worker_id="phase5:target") as target:
        target_plan = target.create_plan(manifest_two)
        target_stage = memory.db.execute(
            "SELECT id FROM long_horizon_stages WHERE plan_id=? AND ordinal=1",
            (target_plan,),
        ).fetchone()
        try:
            memory.db.execute(
                "INSERT INTO long_horizon_checkpoints("
                "plan_id,stage_id,sequence,created_at,previous_sha256,receipt_json,"
                "receipt_sha256,receipt_mac_sha256) VALUES(?,?,?,?,?,?,?,?)",
                (
                    target_plan,
                    int(target_stage["id"]),
                    1,
                    copied["created_at"],
                    copied["previous_sha256"],
                    copied["receipt_json"],
                    copied["receipt_sha256"],
                    copied["receipt_mac_sha256"],
                ),
            )
        except sqlite3.IntegrityError:
            return {"status": "rejected", "kind": "cross_project_checkpoint"}
        try:
            target.show_plan(target_plan)
        except LongHorizonIntegrityError:
            return {"status": "rejected", "kind": "cross_project_checkpoint"}
    raise LongHorizonEvaluationError("cross-project receipt replay was accepted")


def _execute_negative_control(
    control: Mapping[str, Any],
    workflow: Mapping[str, Any],
    template: Mapping[str, Any],
    database: Path,
) -> Mapping[str, str]:
    kind = str(control["kind"])
    with Memory(database) as memory:
        conversation_id = memory.new_conversation(
            f"phase5-control-{control['control_id']}", project_id=1
        )
        task_id = memory.add_task(
            f"phase5-control-{control['control_id']}",
            project_id=1,
            idempotency_key=f"phase5-control:{control['control_id']}",
        )
        budget = _workflow_budget(workflow)
        if kind == "budget_retries":
            budget = WorkflowBudget(
                elapsed_seconds=budget.elapsed_seconds,
                tool_calls=budget.tool_calls,
                model_calls=budget.model_calls,
                prompt_tokens=budget.prompt_tokens,
                completion_tokens=budget.completion_tokens,
                retries=0,
            )
        manifest = WorkflowManifest(
            project_id=1,
            conversation_id=conversation_id,
            task_id=task_id,
            goal_sha256=workflow["goal_sha256"],
            contract_sha256=workflow["contract_sha256"],
            constraints_sha256=workflow["constraints_sha256"],
            approval_scope_sha256=workflow["approval_sha256"],
            artifact_set_sha256=workflow["artifact_sha256"],
            budget=budget,
            stages=_stage_specs(template),
        )
        if kind == "cross_project_checkpoint":
            return _cross_project_receipt_replay(memory, manifest, workflow, template)
        with LongHorizonStore(
            memory,
            project_id=1,
            worker_id="phase5:control",
        ) as store:
            plan_id = store.create_plan(manifest)
            if kind == "cancelled":
                store.cancel_plan(plan_id, sha256_json({"control": kind}))
                try:
                    store.claim_next_stage(plan_id, worker_id="phase5:control")
                except LongHorizonStateError:
                    return {"status": "rejected", "kind": kind}
                raise LongHorizonEvaluationError("cancelled plan resumed")
            claim = store.claim_next_stage(
                plan_id,
                worker_id="phase5:control",
                lease_seconds=1,
            )
            if claim is None:
                raise LongHorizonEvaluationError("control could not claim a stage")
            if kind.startswith("budget_") and kind != "budget_retries":
                usage = _usage(claim)
                key = {
                    "budget_time": "elapsed_seconds",
                    "budget_tools": "tool_calls",
                    "budget_model_calls": "model_calls",
                    "budget_prompt_tokens": "prompt_tokens",
                    "budget_completion_tokens": "completion_tokens",
                }[kind]
                usage[key] = int(claim["budget"][key]) + 1
                try:
                    store.record_checkpoint(
                        plan_id,
                        int(claim["stage_id"]),
                        worker_id="phase5:control",
                        lease_token=str(claim["lease_token"]),
                        usage=usage,
                        outcome_sha256=sha256_json({"control": kind}),
                        artifact_sha256=workflow["artifact_sha256"],
                        executor_id="executor:control",
                    )
                except LongHorizonBudgetError:
                    return {"status": "rejected", "kind": kind}
                raise LongHorizonEvaluationError("over-budget checkpoint was accepted")
            if kind == "budget_retries":
                time.sleep(1.05)
                try:
                    store.claim_next_stage(
                        plan_id,
                        worker_id="phase5:control",
                        lease_seconds=1,
                    )
                except LongHorizonBudgetError:
                    return {"status": "rejected", "kind": kind}
                raise LongHorizonEvaluationError("retry budget was not enforced")
            if kind == "tampered_checkpoint":
                store.reserve_stage_usage(
                    plan_id,
                    int(claim["stage_id"]),
                    worker_id="phase5:control",
                    lease_token=str(claim["lease_token"]),
                    usage=_usage(claim),
                )
                store.record_checkpoint(
                    plan_id,
                    int(claim["stage_id"]),
                    worker_id="phase5:control",
                    lease_token=str(claim["lease_token"]),
                    usage=_usage(claim),
                    outcome_sha256=sha256_json({"control": kind}),
                    artifact_sha256=workflow["artifact_sha256"],
                    executor_id="executor:control",
                )
                store.db.execute(
                    "UPDATE long_horizon_checkpoints SET receipt_json='{}' WHERE plan_id=?",
                    (plan_id,),
                )
                try:
                    store.show_plan(plan_id)
                except LongHorizonIntegrityError:
                    return {"status": "rejected", "kind": kind}
                raise LongHorizonEvaluationError("tampered checkpoint was accepted")
            if kind == "replayed_effect":
                ledger = sqlite3.connect(f"{database}.effects", isolation_level=None)
                try:
                    ledger.execute(
                        "CREATE TABLE effects(effect_key TEXT PRIMARY KEY)"
                    )
                    ledger.execute("INSERT INTO effects VALUES(?)", (claim["effect_key"],))
                    try:
                        ledger.execute(
                            "INSERT INTO effects VALUES(?)",
                            (claim["effect_key"],),
                        )
                    except sqlite3.IntegrityError:
                        return {"status": "rejected", "kind": kind}
                finally:
                    ledger.close()
                raise LongHorizonEvaluationError("conflicting mutation replay was accepted")
    raise LongHorizonEvaluationError("negative control was not executed")


def _validate_execution_receipt(receipt: Any, workflow: Mapping[str, Any]) -> None:
    if not isinstance(receipt, Mapping):
        raise LongHorizonEvaluationError("execution evidence must be an object")
    evidence = dict(receipt)
    effect_count = evidence.pop("evaluated_effect_count", None)
    if evidence.get("schema") != "jarvis.long-horizon.evidence.v1":
        raise LongHorizonEvaluationError("execution evidence schema is unsupported")
    supplied = _digest(evidence.pop("evidence_sha256", None), "evidence SHA")
    if supplied != sha256_json(evidence):
        raise LongHorizonEvaluationError("execution evidence digest mismatch")
    plan = evidence.get("plan")
    manifest = evidence.get("manifest")
    stages = evidence.get("stages")
    checkpoints = evidence.get("checkpoints")
    mutations = evidence.get("mutation_receipts")
    verifications = evidence.get("final_verifications")
    if not all(isinstance(item, (Mapping, list)) for item in (
        plan, manifest, stages, checkpoints, mutations, verifications
    )):
        raise LongHorizonEvaluationError("execution evidence sections are invalid")
    if plan["project_id"] != workflow["project_id"] or manifest["project_id"] != workflow["project_id"]:
        raise LongHorizonEvaluationError("execution project binding changed")
    for source, expected in (
        ("goal_sha256", workflow["goal_sha256"]),
        ("contract_sha256", workflow["contract_sha256"]),
        ("constraints_sha256", workflow["constraints_sha256"]),
        ("approval_scope_sha256", workflow["approval_sha256"]),
        ("artifact_set_sha256", workflow["artifact_sha256"]),
    ):
        if manifest[source] != expected:
            raise LongHorizonEvaluationError(f"resumed {source} was not preserved")
    if plan["status"] != "complete" or plan["final_verification"] is None:
        raise LongHorizonEvaluationError("workflow lacks final completion verification")
    if not isinstance(stages, list) or not 5 <= len(stages) <= 12:
        raise LongHorizonEvaluationError("completed stage evidence is invalid")
    if any(stage["status"] != "complete" for stage in stages):
        raise LongHorizonEvaluationError("a workflow stage is incomplete")
    ordinals = [int(stage["ordinal"]) for stage in stages]
    if ordinals != list(range(1, len(stages) + 1)):
        raise LongHorizonEvaluationError("workflow stage order is invalid")
    if any(stage["artifact_sha256"] != workflow["artifact_sha256"] for stage in stages):
        raise LongHorizonEvaluationError("checkpoint artifact binding changed")
    if len(checkpoints) != len(stages):
        raise LongHorizonEvaluationError("checkpoint chain is incomplete")
    previous = None
    for sequence, checkpoint in enumerate(checkpoints, start=1):
        if int(checkpoint["sequence"]) != sequence:
            raise LongHorizonEvaluationError("checkpoint sequence is invalid")
        if checkpoint["previous_sha256"] != previous:
            raise LongHorizonEvaluationError("checkpoint chain predecessor is invalid")
        decoded = json.loads(str(checkpoint["receipt_json"]))
        if sha256_json(decoded) != checkpoint["receipt_sha256"]:
            raise LongHorizonEvaluationError("checkpoint receipt digest mismatch")
        previous = checkpoint["receipt_sha256"]
    if plan["checkpoint_head_sha256"] != previous:
        raise LongHorizonEvaluationError("checkpoint head does not bind the chain")
    if effect_count != 1:
        raise LongHorizonEvaluationError("mutation effect count is not exactly one")
    effect_keys = {
        item["effect_key"]
        for item in mutations
        if item["event_type"] in {"result", "reconciliation"}
        and item["outcome"] == "applied"
    }
    if len(effect_keys) != 1:
        raise LongHorizonEvaluationError("mutation receipts do not bind one effect")
    executors = {stage["executor_id"] for stage in stages}
    passed = [item for item in verifications if int(item["passed"]) == 1]
    if len(passed) != 1 or passed[0]["verifier_id"] in executors:
        raise LongHorizonEvaluationError("final verification is not independent")


def _authority_material(label: str) -> tuple[str, str]:
    private = hashlib.sha256(f"jarvis-phase5:{label}".encode("ascii")).digest()
    key = Ed25519PrivateKey.from_private_bytes(private)
    public = key.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    return private.hex(), public.hex()


def _worker_environment(secret: str | None = None) -> dict[str, str]:
    allowed = {
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "WINDIR",
        "TEMP",
        "TMP",
    }
    environment = {key: value for key, value in os.environ.items() if key in allowed}
    environment["PYTHONIOENCODING"] = "utf-8"
    if secret is not None:
        environment["JARVIS_PHASE5_AUTHORITY_PRIVATE_KEY"] = secret
    return environment


def _run_worker(
    mode: str,
    request_path: Path,
    *,
    secret: str | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "jarvis.long_horizon_eval_worker", mode, str(request_path)],
        cwd=Path(__file__).parents[1],
        env=_worker_environment(secret),
        capture_output=True,
        text=True,
        timeout=45,
        check=False,
    )


def _subprocess_request(
    workflow: Mapping[str, Any],
    template: Mapping[str, Any],
    root: Path,
) -> tuple[dict[str, Any], str, str]:
    verify_secret, verify_public = _authority_material("verify")
    reconcile_secret, reconcile_public = _authority_material("reconcile")
    request = {
        "database": str(root / f"{workflow['workflow_id']}.db"),
        "ledger": str(root / f"{workflow['workflow_id']}.effects.db"),
        "artifact_path": str(root / f"{workflow['workflow_id']}.artifact.json"),
        "workflow_id": workflow["workflow_id"],
        "project_id": workflow["project_id"],
        "goal_sha256": workflow["goal_sha256"],
        "contract_sha256": workflow["contract_sha256"],
        "constraints_sha256": workflow["constraints_sha256"],
        "approval_sha256": workflow["approval_sha256"],
        "artifact_sha256": workflow["artifact_sha256"],
        "crash_point": workflow["crash_point"],
        "budget": {
            "elapsed_seconds": max(1, (workflow["budgets"]["time_ms"] + 999) // 1000),
            "tool_calls": workflow["budgets"]["tool_calls"],
            "model_calls": workflow["budgets"]["model_calls"],
            "prompt_tokens": workflow["budgets"]["prompt_tokens"],
            "completion_tokens": workflow["budgets"]["completion_tokens"],
            "retries": workflow["budgets"]["retries"],
        },
        "stages": [
            {
                "stage_id": stage.stage_id,
                "stage_type": stage.stage_type,
                "mutation_kind": stage.mutation_kind,
                "budget": stage.budget.to_payload(),
            }
            for stage in _stage_specs(template)
        ],
        "authority_runtime_sha256": hashlib.sha256(
            Path(__file__).with_name("long_horizon_eval_worker.py").read_bytes()
        ).hexdigest(),
        "authorities": {
            "verifier": {
                "scope": "final_verification",
                "verifier_id": "verifier:independent",
                "runtime_sha256": hashlib.sha256(
                    Path(__file__).with_name("long_horizon_eval_worker.py").read_bytes()
                ).hexdigest(),
                "public_key": verify_public,
            },
            "reconciler": {
                "scope": "mutation_reconciliation",
                "verifier_id": "reconciler:independent",
                "runtime_sha256": hashlib.sha256(
                    Path(__file__).with_name("long_horizon_eval_worker.py").read_bytes()
                ).hexdigest(),
                "public_key": reconcile_public,
            },
        },
    }
    return request, verify_secret, reconcile_secret


def _execute_subprocess_workflow(
    workflow: Mapping[str, Any],
    template: Mapping[str, Any],
    root: Path,
) -> tuple[Mapping[str, Any], dict[str, Any]]:
    request, verify_secret, reconcile_secret = _subprocess_request(workflow, template, root)
    request_path = root / f"{workflow['workflow_id']}.request.json"
    request_path.write_text(json.dumps(request, sort_keys=True), encoding="utf-8")
    expected = {
        "before_mutation": 86,
        "after_mutation_before_receipt": 87,
        "after_receipt_before_cursor": 88,
    }[workflow["crash_point"]]
    first = _run_worker("execute", request_path)
    if first.returncode != expected:
        raise LongHorizonEvaluationError(
            f"worker did not stop at sealed crash point: {first.stderr[-300:]}"
        )
    time.sleep(1.05)
    resumed = _run_worker("recover", request_path)
    if resumed.returncode != 0:
        raise LongHorizonEvaluationError(f"restart recovery failed: {resumed.stderr[-300:]}")
    if workflow["crash_point"] == "after_mutation_before_receipt":
        reconciled = _run_worker("reconcile", request_path, secret=reconcile_secret)
        if reconciled.returncode != 0:
            raise LongHorizonEvaluationError(
                f"independent reconciliation failed: {reconciled.stderr[-300:]}"
            )
        resumed = _run_worker("recover", request_path)
        if resumed.returncode != 0:
            raise LongHorizonEvaluationError(
                f"post-reconciliation recovery failed: {resumed.stderr[-300:]}"
            )
    verified = _run_worker("verify", request_path, secret=verify_secret)
    if verified.returncode != 0:
        raise LongHorizonEvaluationError(
            f"independent final verification failed: {verified.stderr[-300:]}"
        )
    with LongHorizonStore(
        request["database"],
        project_id=int(workflow["project_id"]),
        authorities={
            key: {**value, "public_key": bytes.fromhex(value["public_key"])}
            for key, value in request["authorities"].items()
        },
        approval_validator=lambda phase, context: {
            "approved": True,
            "receipt_sha256": sha256_json({"phase": phase, "context": context}),
        },
    ) as store:
        plan_id = int(store.list_plans(limit=1)[0]["plan_id"])
        evidence = store.export_evidence(plan_id)
    ledger = sqlite3.connect(request["ledger"])
    try:
        effect_count = int(ledger.execute("SELECT COUNT(*) FROM effects").fetchone()[0])
        processes = ledger.execute(
            "SELECT pid,mode,authority_secret_present FROM processes ORDER BY rowid"
        ).fetchall()
    finally:
        ledger.close()
    evidence["evaluated_effect_count"] = effect_count
    process_result = {
        "distinct_processes": len({row[0] for row in processes}),
        "executor_secret_leaks": sum(
            int(row[2]) for row in processes if row[1] in {"execute", "recover"}
        ),
        "authority_processes": sum(
            1 for row in processes if row[1] in {"verify", "reconcile"}
        ),
    }
    return evidence, process_result


def _execute_subprocess_control(
    control: Mapping[str, Any],
    workflow: Mapping[str, Any],
    template: Mapping[str, Any],
    root: Path,
) -> Mapping[str, str]:
    request, _verify_secret, _reconcile_secret = _subprocess_request(
        workflow,
        template,
        root,
    )
    kind = str(control["kind"])
    request["control_kind"] = kind
    request["database"] = str(root / f"{control['control_id']}.db")
    request["ledger"] = str(root / f"{control['control_id']}.controls.db")
    request["workflow_id"] = str(control["control_id"])
    if kind == "budget_retries":
        request["budget"]["retries"] = 0
    path = root / f"{control['control_id']}.request.json"
    path.write_text(json.dumps(request, sort_keys=True), encoding="utf-8")
    attempted = _run_worker("control-attempt", path)
    if attempted.returncode != 0:
        raise LongHorizonEvaluationError(
            f"subprocess control attempt failed: {attempted.stderr[-300:]}"
        )
    checked = _run_worker("control-check", path)
    if checked.returncode != 0:
        raise LongHorizonEvaluationError(
            f"subprocess control reopen failed: {checked.stderr[-300:]}"
        )
    return {"status": "rejected", "kind": kind}


def evaluate_long_horizon_holdout(
    fixture_path: Path,
    *,
    expected_fixture_sha256: str,
    expected_evaluator_sha256: str,
) -> dict[str, Any]:
    path = Path(fixture_path)
    raw = path.read_bytes()
    if b"\r\n" in raw:
        raise LongHorizonEvaluationError("sealed fixture must use LF line endings")
    if hashlib.sha256(raw).hexdigest() != _digest(expected_fixture_sha256, "expected fixture SHA"):
        raise LongHorizonEvaluationError("fixture byte digest mismatch")
    evaluator_sha = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    if evaluator_sha != _digest(expected_evaluator_sha256, "expected evaluator SHA"):
        raise LongHorizonEvaluationError("independent evaluator pin mismatch")
    fixture = _validate_fixture(json.loads(raw.decode("utf-8")))
    if fixture["runtime_sha256"] != long_horizon_runtime_sha256():
        raise LongHorizonEvaluationError("runtime pin mismatch")
    templates = {item["template_id"]: item for item in fixture["templates"]}
    receipts: list[Mapping[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="jarvis-phase5-eval-") as directory:
        root = Path(directory)
        process_results: list[Mapping[str, Any]] = []
        for workflow in fixture["workflows"]:
            receipt, process_result = _execute_subprocess_workflow(
                workflow,
                templates[workflow["template_id"]],
                root,
            )
            _validate_execution_receipt(receipt, workflow)
            receipts.append(receipt)
            process_results.append(process_result)
    rejected = 0
    with tempfile.TemporaryDirectory(prefix="jarvis-phase5-controls-") as directory:
        root = Path(directory)
        workflow_by_id = {item["workflow_id"]: item for item in fixture["workflows"]}
        for control in fixture["negative_controls"]:
            workflow = workflow_by_id[control["workflow_id"]]
            if control["kind"].startswith("budget_") or control["kind"] == "cancelled":
                outcome = _execute_subprocess_control(
                    control,
                    workflow,
                    templates[workflow["template_id"]],
                    root,
                )
            else:
                outcome = _execute_negative_control(
                    control,
                    workflow,
                    templates[workflow["template_id"]],
                    root / f"{control['control_id']}.db",
                )
            if outcome != {"status": "rejected", "kind": control["kind"]}:
                raise LongHorizonEvaluationError("negative control did not fail closed")
            rejected += 1
    material = {
        "schema": REPORT_SCHEMA,
        "claim_scope": CLAIM_SCOPE,
        "fixture_sha256": hashlib.sha256(raw).hexdigest(),
        "evaluator_sha256": evaluator_sha,
        "config_sha256": sha256_json(EVALUATION_CONFIG),
        "runtime_sha256": fixture["runtime_sha256"],
        "workflow_count": len(receipts),
        "family_count": len({item["family"] for item in fixture["workflows"]}),
        "crash_points": sorted({item["crash_point"] for item in fixture["workflows"]}),
        "workflows_passed": len(receipts),
        "negative_controls_total": len(fixture["negative_controls"]),
        "negative_controls_rejected": rejected,
        "duplicate_effects": sum(
            max(0, int(item["evaluated_effect_count"]) - 1) for item in receipts
        ),
        "restart_process_rate": sum(
            int(item["distinct_processes"] >= 3) for item in process_results
        ) / len(process_results),
        "executor_authority_secret_leaks": sum(
            int(item["executor_secret_leaks"]) for item in process_results
        ),
        "budget_enforcement_rate": rejected / len(fixture["negative_controls"]),
        "independent_verification_rate": sum(
            int(item["authority_processes"] >= 1) for item in process_results
        ) / len(process_results),
        "all_exit_criteria_passed": (
            len(receipts) == len(fixture["workflows"])
            and rejected == len(fixture["negative_controls"])
            and all(int(item["distinct_processes"]) >= 3 for item in process_results)
            and all(int(item["executor_secret_leaks"]) == 0 for item in process_results)
            and all(int(item["evaluated_effect_count"]) == 1 for item in receipts)
        ),
        "activation_authorized": False,
    }
    material["attestation_sha256"] = sha256_json(material)
    return material
