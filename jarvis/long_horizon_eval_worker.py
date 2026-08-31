from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
from pathlib import Path
from typing import Any, Mapping

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from .long_horizon import (
    LongHorizonBudgetError,
    LongHorizonStateError,
    LongHorizonStore,
    WorkflowBudget,
    WorkflowManifest,
    WorkflowStageSpec,
)
from .memory import Memory


CRASH_BEFORE_MUTATION = 86
CRASH_AFTER_EFFECT = 87
CRASH_AFTER_CHECKPOINT = 88


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _sha(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _artifact_bytes(request: Mapping[str, Any]) -> bytes:
    return _canonical(
        {
            "schema": "jarvis.long-horizon-eval-artifact.v1",
            "workflow_id": request["workflow_id"],
            "project_id": int(request["project_id"]),
        }
    )


def _write_artifact(request: Mapping[str, Any]) -> None:
    payload = _artifact_bytes(request)
    if hashlib.sha256(payload).hexdigest() != request["artifact_sha256"]:
        raise RuntimeError("artifact material does not match the sealed digest")
    path = Path(request["artifact_path"])
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    with temporary.open("wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _verified_artifact_sha256(request: Mapping[str, Any]) -> str:
    path = Path(request["artifact_path"])
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise RuntimeError("final artifact is unavailable") from exc
    observed = hashlib.sha256(payload).hexdigest()
    if observed != request["artifact_sha256"]:
        raise RuntimeError("final artifact digest does not match the sealed workflow")
    return observed


def _approval(phase: str, context: Mapping[str, Any]) -> dict[str, Any]:
    return {"approved": True, "receipt_sha256": _sha({"phase": phase, "context": context})}


def _authorities(request: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        key: {
            **value,
            "public_key": bytes.fromhex(value["public_key"]),
        }
        for key, value in request["authorities"].items()
    }


def _store(request: Mapping[str, Any], *, worker: str) -> LongHorizonStore:
    return LongHorizonStore(
        request["database"],
        project_id=int(request["project_id"]),
        worker_id=worker,
        authorities=_authorities(request),
        approval_validator=_approval,
    )


def _usage(claim: Mapping[str, Any]) -> dict[str, int]:
    budget = claim["budget"]
    return {
        "elapsed_seconds": 1,
        "tool_calls": 1,
        "model_calls": 1,
        "prompt_tokens": min(10, int(budget["prompt_tokens"])),
        "completion_tokens": min(5, int(budget["completion_tokens"])),
    }


def _checkpoint(
    store: LongHorizonStore,
    plan_id: int,
    claim: Mapping[str, Any],
    request: Mapping[str, Any],
    *,
    reserve: bool = True,
) -> None:
    usage = _usage(claim) if reserve else {
        "elapsed_seconds": 0,
        "tool_calls": 0,
        "model_calls": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
    }
    store.reserve_stage_usage(
        plan_id,
        int(claim["stage_id"]),
        worker_id="executor",
        lease_token=str(claim["lease_token"]),
        usage=usage,
    )
    store.record_checkpoint(
        plan_id,
        int(claim["stage_id"]),
        worker_id="executor",
        lease_token=str(claim["lease_token"]),
        usage=usage,
        outcome_sha256=_sha({"workflow": request["workflow_id"], "stage": claim["stage_key"]}),
        artifact_sha256=request["artifact_sha256"],
        executor_id="executor",
    )


def _ledger_effect(request: Mapping[str, Any], claim: Mapping[str, Any]) -> str:
    path = Path(request["ledger"])
    db = sqlite3.connect(path, isolation_level=None)
    try:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
        db.execute(
            "CREATE TABLE IF NOT EXISTS effects (effect_key TEXT PRIMARY KEY, workflow_id TEXT NOT NULL, project_id INTEGER NOT NULL, artifact_sha256 TEXT NOT NULL)"
        )
        db.execute("BEGIN IMMEDIATE")
        db.execute(
            "INSERT INTO effects(effect_key,workflow_id,project_id,artifact_sha256) VALUES(?,?,?,?)",
            (claim["effect_key"], request["workflow_id"], request["project_id"], request["artifact_sha256"]),
        )
        db.commit()
        _write_artifact(request)
        return _sha({"effect_key": claim["effect_key"], "applied": True})
    finally:
        db.close()


def _record_process(request: Mapping[str, Any], mode: str) -> None:
    db = sqlite3.connect(request["ledger"], isolation_level=None)
    try:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
        db.execute(
            "CREATE TABLE IF NOT EXISTS processes ("
            "pid INTEGER NOT NULL, mode TEXT NOT NULL, authority_secret_present INTEGER NOT NULL)"
        )
        db.execute(
            "INSERT INTO processes(pid,mode,authority_secret_present) VALUES(?,?,?)",
            (
                os.getpid(),
                mode,
                int(bool(os.environ.get("JARVIS_PHASE5_AUTHORITY_PRIVATE_KEY"))),
            ),
        )
    finally:
        db.close()


def _bootstrap(request: Mapping[str, Any]) -> int:
    stages = tuple(
        WorkflowStageSpec(
            stage_id=item["stage_id"],
            ordinal=index,
            stage_type=item["stage_type"],
            mutation_kind=item["mutation_kind"],
            budget=WorkflowBudget(**item["budget"]),
        )
        for index, item in enumerate(request["stages"], start=1)
    )
    with Memory(Path(request["database"])) as memory:
        if int(request["project_id"]) == 2:
            memory.add_project("Phase Five Beta", "@projects/phase-five-beta")
        conversation = memory.new_conversation("phase5", project_id=int(request["project_id"]))
        task = memory.add_task("phase5", project_id=int(request["project_id"]), idempotency_key=request["workflow_id"])
        manifest = WorkflowManifest(
            project_id=int(request["project_id"]),
            conversation_id=conversation,
            task_id=task,
            goal_sha256=request["goal_sha256"],
            contract_sha256=request["contract_sha256"],
            constraints_sha256=request["constraints_sha256"],
            approval_scope_sha256=request["approval_sha256"],
            artifact_set_sha256=request["artifact_sha256"],
            budget=WorkflowBudget(**request["budget"]),
            stages=stages,
        )
        with LongHorizonStore(
            memory,
            project_id=int(request["project_id"]),
            worker_id="executor",
            authorities=_authorities(request),
            approval_validator=_approval,
        ) as store:
            return store.create_plan(manifest)


def _plan_id(request: Mapping[str, Any]) -> int:
    with _store(request, worker="executor") as store:
        return int(store.list_plans(limit=1)[0]["plan_id"])


def execute(request: Mapping[str, Any]) -> None:
    plan_id = _bootstrap(request)
    with _store(request, worker="executor") as store:
        while True:
            claim = store.claim_next_stage(plan_id, worker_id="executor", lease_seconds=1)
            if claim is None:
                return
            if claim["mutation_kind"] == "none":
                _checkpoint(store, plan_id, claim, request)
                continue
            if request["crash_point"] == "before_mutation":
                os._exit(CRASH_BEFORE_MUTATION)
            usage = _usage(claim)
            store.reserve_stage_usage(plan_id, int(claim["stage_id"]), worker_id="executor", lease_token=str(claim["lease_token"]), usage=usage)
            store.record_mutation_intent(plan_id, int(claim["stage_id"]), worker_id="executor", lease_token=str(claim["lease_token"]), executor_id="executor")
            store.authorize_mutation_effect(plan_id, int(claim["stage_id"]), worker_id="executor", lease_token=str(claim["lease_token"]), executor_id="executor")
            store.consume_mutation_effect_authorization(plan_id, int(claim["stage_id"]), worker_id="executor", lease_token=str(claim["lease_token"]), executor_id="executor")
            evidence = _ledger_effect(request, claim)
            if request["crash_point"] == "after_mutation_before_receipt":
                os._exit(CRASH_AFTER_EFFECT)
            store.record_mutation_result(plan_id, int(claim["stage_id"]), worker_id="executor", lease_token=str(claim["lease_token"]), executor_id="executor", outcome="applied", evidence_sha256=evidence)
            store.record_checkpoint(plan_id, int(claim["stage_id"]), worker_id="executor", lease_token=str(claim["lease_token"]), usage=usage, outcome_sha256=_sha({"workflow": request["workflow_id"], "stage": claim["stage_key"]}), artifact_sha256=request["artifact_sha256"], executor_id="executor")
            if request["crash_point"] == "after_receipt_before_cursor":
                os._exit(CRASH_AFTER_CHECKPOINT)


def recover(request: Mapping[str, Any]) -> None:
    plan_id = _plan_id(request)
    with _store(request, worker="executor") as store:
        while True:
            claim = store.claim_next_stage(plan_id, worker_id="executor", lease_seconds=60)
            if claim is None:
                return
            if claim["mutation_kind"] == "none" or claim.get("mutation_state") == "reconciled_applied":
                _checkpoint(
                    store,
                    plan_id,
                    claim,
                    request,
                    reserve=claim.get("mutation_state") != "reconciled_applied",
                )
                continue
            usage = _usage(claim)
            store.reserve_stage_usage(plan_id, int(claim["stage_id"]), worker_id="executor", lease_token=str(claim["lease_token"]), usage=usage)
            store.record_mutation_intent(plan_id, int(claim["stage_id"]), worker_id="executor", lease_token=str(claim["lease_token"]), executor_id="executor")
            store.authorize_mutation_effect(plan_id, int(claim["stage_id"]), worker_id="executor", lease_token=str(claim["lease_token"]), executor_id="executor")
            store.consume_mutation_effect_authorization(plan_id, int(claim["stage_id"]), worker_id="executor", lease_token=str(claim["lease_token"]), executor_id="executor")
            evidence = _ledger_effect(request, claim)
            store.record_mutation_result(plan_id, int(claim["stage_id"]), worker_id="executor", lease_token=str(claim["lease_token"]), executor_id="executor", outcome="applied", evidence_sha256=evidence)
            store.record_checkpoint(plan_id, int(claim["stage_id"]), worker_id="executor", lease_token=str(claim["lease_token"]), usage=usage, outcome_sha256=_sha({"workflow": request["workflow_id"], "stage": claim["stage_key"]}), artifact_sha256=request["artifact_sha256"], executor_id="executor")


def _private_key() -> Ed25519PrivateKey:
    raw = os.environ.pop("JARVIS_PHASE5_AUTHORITY_PRIVATE_KEY", "")
    if len(raw) != 64:
        raise RuntimeError("authority secret is unavailable")
    return Ed25519PrivateKey.from_private_bytes(bytes.fromhex(raw))


def sign(request: Mapping[str, Any], *, reconciliation: bool) -> None:
    artifact_sha256 = None
    if not reconciliation:
        artifact_sha256 = _verified_artifact_sha256(request)
    plan_id = _plan_id(request)
    key = _private_key()
    with _store(request, worker="authority") as store:
        evidence = store.export_evidence(plan_id)
        stage = next((item for item in evidence["stages"] if item["status"] != "complete"), None)
        runtime = request["authority_runtime_sha256"]
        if reconciliation:
            ledger = sqlite3.connect(request["ledger"])
            row = ledger.execute("SELECT effect_key FROM effects").fetchone()
            ledger.close()
            observed = _sha({"effect_key": row[0], "applied": True})
            challenge = {
                "schema": "jarvis.long-horizon.mutation-reconciliation-challenge.v1",
                "plan_id": plan_id,
                "project_id": int(request["project_id"]),
                "manifest_sha256": evidence["plan"]["manifest_sha256"],
                "stage_id": int(stage["stage_id"]),
                "stage_sha256": stage["stage_sha256"],
                "effect_key": stage["effect_key"],
                "generation": int(stage["attempt_count"]),
                "reconciliation_round": 1,
                "authority_id": "reconciler",
                "reconciler_id": "reconciler:independent",
                "reconciler_runtime_sha256": runtime,
                "outcome": "applied",
                "evidence_sha256": observed,
            }
            signature = key.sign(_canonical(challenge)).hex()
            store.reconcile_mutation(plan_id, int(stage["stage_id"]), authority_id="reconciler", reconciler_runtime_sha256=runtime, outcome="applied", evidence_sha256=observed, signature_sha256=signature)
        else:
            ledger = sqlite3.connect(request["ledger"])
            try:
                rows = ledger.execute(
                    "SELECT effect_key,workflow_id,project_id,artifact_sha256 "
                    "FROM effects ORDER BY effect_key"
                ).fetchall()
            finally:
                ledger.close()
            ledger_sha = _sha({"rows": rows})
            bound = _sha({"evidence_sha256": evidence["evidence_sha256"], "artifact_sha256": artifact_sha256, "ledger_sha256": ledger_sha})
            challenge = {
                "schema": "jarvis.long-horizon.final-verification-challenge.v1",
                "plan_id": plan_id,
                "project_id": int(request["project_id"]),
                "manifest_sha256": evidence["plan"]["manifest_sha256"],
                "checkpoint_head_sha256": evidence["plan"]["checkpoint_head_sha256"],
                "authority_id": "verifier",
                "verifier_id": "verifier:independent",
                "verifier_runtime_sha256": runtime,
                "evidence_sha256": bound,
                "passed": True,
            }
            signature = key.sign(_canonical(challenge)).hex()
            store.record_final_verification(plan_id, authority_id="verifier", verifier_runtime_sha256=runtime, evidence_sha256=bound, signature_sha256=signature, passed=True)


def _record_control(request: Mapping[str, Any], control: str, passed: bool) -> None:
    db = sqlite3.connect(request["ledger"], isolation_level=None)
    try:
        db.execute(
            "CREATE TABLE IF NOT EXISTS control_results ("
            "control TEXT PRIMARY KEY, passed INTEGER NOT NULL, operation_started INTEGER NOT NULL)"
        )
        db.execute(
            "INSERT INTO control_results(control,passed,operation_started) VALUES(?,?,0)",
            (control, int(passed)),
        )
    finally:
        db.close()


def control_attempt(request: Mapping[str, Any]) -> None:
    kind = str(request["control_kind"])
    plan_id = _bootstrap(request)
    with _store(request, worker="executor") as store:
        if kind == "cancelled":
            store.cancel_plan(plan_id, _sha({"control": kind}))
            _record_control(request, kind, True)
            return
        claim = store.claim_next_stage(plan_id, worker_id="executor", lease_seconds=1)
        if claim is None:
            raise RuntimeError("control stage was unavailable")
        if kind == "budget_retries":
            import time

            time.sleep(1.05)
            try:
                store.claim_next_stage(plan_id, worker_id="executor", lease_seconds=1)
            except LongHorizonBudgetError:
                _record_control(request, kind, True)
                return
            raise RuntimeError("retry budget was not enforced")
        if not kind.startswith("budget_"):
            raise RuntimeError("unsupported subprocess control")
        key = {
            "budget_time": "elapsed_seconds",
            "budget_tools": "tool_calls",
            "budget_model_calls": "model_calls",
            "budget_prompt_tokens": "prompt_tokens",
            "budget_completion_tokens": "completion_tokens",
        }.get(kind)
        if key is None:
            raise RuntimeError("unsupported reservation control")
        usage = _usage(claim)
        usage[key] = int(claim["budget"][key]) + 1
        try:
            store.reserve_stage_usage(
                plan_id,
                int(claim["stage_id"]),
                worker_id="executor",
                lease_token=str(claim["lease_token"]),
                usage=usage,
            )
        except LongHorizonBudgetError:
            _record_control(request, kind, True)
            return
        raise RuntimeError("over-budget operation reservation was accepted")


def control_check(request: Mapping[str, Any]) -> None:
    kind = str(request["control_kind"])
    plan_id = _plan_id(request)
    with _store(request, worker="auditor") as store:
        if kind == "cancelled":
            if store.show_plan(plan_id)["status"] != "cancelled":
                raise RuntimeError("cancellation was not durable")
            try:
                store.claim_next_stage(plan_id, worker_id="auditor")
            except LongHorizonStateError:
                return
            raise RuntimeError("cancelled plan resumed after restart")
        ledger = sqlite3.connect(request["ledger"])
        try:
            row = ledger.execute(
                "SELECT passed,operation_started FROM control_results WHERE control=?",
                (kind,),
            ).fetchone()
        finally:
            ledger.close()
        if row != (1, 0):
            raise RuntimeError("reservation control did not remain effect-free")


def main() -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument(
        "mode",
        choices=(
            "execute",
            "recover",
            "reconcile",
            "verify",
            "control-attempt",
            "control-check",
        ),
    )
    parser.add_argument("request")
    args = parser.parse_args()
    request = json.loads(Path(args.request).read_text(encoding="utf-8"))
    _record_process(request, args.mode)
    if args.mode == "execute":
        execute(request)
    elif args.mode == "recover":
        recover(request)
    elif args.mode in {"reconcile", "verify"}:
        sign(request, reconciliation=args.mode == "reconcile")
    elif args.mode == "control-attempt":
        control_attempt(request)
    else:
        control_check(request)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
