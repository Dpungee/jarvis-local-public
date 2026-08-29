from __future__ import annotations

import hashlib
import json
import math
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence

from .model_client import split_model_reference
from .task_contract import (
    TaskContract,
    TaskContractError,
    build_task_contract_messages,
    grounding_texts_for_resolution,
    normalize_task_contract_response,
    parse_task_contract,
    reconcile_task_contract_continuation,
    task_contract_response_schema,
)
from .task_contract_eval import (
    load_task_contract_holdout,
    score_task_contract_holdout,
    score_task_contract_holdout_contracts,
)


_SERVED_MODEL_ATTESTATION_PROVIDERS = frozenset({"openai", "anthropic", "ollama"})


class TaskContractBenchmarkClient(Protocol):
    """The single exact-model call surface used by the isolated benchmark."""

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        model: str,
        **kwargs: Any,
    ) -> Mapping[str, Any]: ...


class IsolatedTaskContractAgentFactory(Protocol):
    """Construct a real Agent around runner-owned isolated state."""

    def __call__(
        self,
        case: Mapping[str, Any],
        memory: Any,
        workspace: Path,
        on_event: Callable[[str], None],
    ) -> Any: ...


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _receipt_checksum_sha256(value: Mapping[str, Any]) -> str:
    """Return an integrity checksum, not a signature or authenticity proof."""
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _exact_model_reference(model: str) -> tuple[str, str, str]:
    requested = str(model).strip()
    if not requested or ":" not in requested:
        raise ValueError("live TaskContract benchmark requires an exact provider:model reference")
    provider, provider_model = split_model_reference(requested)
    if not provider_model or provider_model.casefold() == "auto":
        raise ValueError("live TaskContract benchmark does not accept an auto model")
    return requested, provider, provider_model


def _pending_contract(case: Mapping[str, Any]) -> TaskContract | None:
    raw = case.get("pending_contract")
    if not isinstance(raw, Mapping):
        return None
    grounding = [
        str(raw.get("goal") or ""),
        str(raw.get("target") or ""),
        *(str(item) for item in raw.get("constraint_quotes") or []),
    ]
    return parse_task_contract(
        raw,
        grounding_texts=[item for item in grounding if item.strip()],
        has_pending_goal=False,
    )


def _resolve_one(
    case: Mapping[str, Any],
    *,
    client: TaskContractBenchmarkClient,
    requested_model: str,
) -> tuple[TaskContract, str | None, bool]:
    """Invoke the production resolver contract once with no tools or fallback."""
    pending = _pending_contract(case)
    prompt = str(case["operator_prompt"])
    recent = [str(item) for item in case.get("recent_user_turns") or []]
    messages = build_task_contract_messages(
        prompt,
        pending_contract=pending,
        recent_user_turns=recent,
        latest_assistant_context=(
            str(case["latest_assistant_context"])
            if case.get("latest_assistant_context") is not None
            else None
        ),
    )
    response = client.chat(
        messages,
        [],
        requested_model,
        context_length=8_192,
        think=False,
        temperature=0.0,
        response_format=task_contract_response_schema(),
        seed=0,
    )
    if not isinstance(response, Mapping):
        raise TaskContractError("benchmark provider returned a non-object response")
    raw_reported_model = response.get("model")
    if raw_reported_model is None:
        # Production ChatResponse keeps generation metadata as attributes while
        # remaining message-dict compatible. Read both shapes so an exact-model
        # receipt cannot silently discard the provider's reported model.
        raw_reported_model = getattr(response, "model", None)
    reported_model = (
        str(raw_reported_model).strip()
        if isinstance(raw_reported_model, str) and str(raw_reported_model).strip()
        else None
    )
    raw_model_attested = response.get("model_attested")
    if raw_model_attested is None:
        # Production ChatResponse exposes generation metadata as attributes.
        # Require the literal boolean True: copied request metadata, truthy
        # strings, integers, and absent fields are not provider proof.
        raw_model_attested = getattr(response, "model_attested", None)
    model_attested = raw_model_attested is True
    grounding = grounding_texts_for_resolution(
        prompt,
        pending_contract=pending,
        recent_user_turns=recent,
    )
    contract = parse_task_contract(
        normalize_task_contract_response(
            str(response.get("content") or ""),
            grounding_texts=grounding,
            canonical_goal=prompt,
            continued_goal=(pending.goal if pending is not None else None),
            operator_turn=prompt,
            pending_contract=pending,
        ),
        grounding_texts=grounding,
        has_pending_goal=pending is not None,
    )
    contract = reconcile_task_contract_continuation(
        contract,
        pending_contract=pending,
        operator_turn=prompt,
    )
    return contract, reported_model, model_attested


def _contract_prediction(case_id: str, contract: TaskContract) -> dict[str, Any]:
    return {
        "id": case_id,
        "lane": contract.lane,
        "clarification": contract.needs_clarification,
        "relation": contract.relation,
        "constraint_quotes": list(contract.constraint_quotes),
        "requested_effect": contract.requested_effect,
        "evidence_source": contract.evidence_source,
        "acceptance": list(contract.acceptance),
    }


def _prompt_free_checks(
    case: Mapping[str, Any],
    prediction: Mapping[str, Any],
) -> dict[str, bool]:
    expected = case["expected"]
    predicted_constraints = {
        str(item).casefold() for item in prediction["constraint_quotes"]
    }
    predicted_acceptance = set(prediction["acceptance"])
    return {
        "lane": prediction["lane"] == expected["lane"],
        "clarification": prediction["clarification"] is expected["clarification"],
        "relation": prediction["relation"] == expected["relation"],
        "constraints": all(
            str(item).casefold() in predicted_constraints
            for item in expected["retained_constraints"]
        ),
        "effect": prediction["requested_effect"] == expected["requested_effect"],
        "evidence": prediction["evidence_source"] == expected["evidence_source"],
        "acceptance": all(
            item in predicted_acceptance for item in expected["acceptance_contains"]
        ),
    }


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(percentile * len(ordered)) - 1))
    return round(ordered[index], 3)


def _prompt_free_metrics(metrics: Mapping[str, Any]) -> dict[str, Any]:
    """Remove per-case details; aggregate metrics contain no operator text."""
    return {
        key: value
        for key, value in metrics.items()
        if key not in {"case_checks", "fixture_sha256", "schema_version"}
    }


def run_live_task_contract_benchmark(
    fixture_path: Path,
    *,
    client: TaskContractBenchmarkClient,
    model: str,
    allow_live: bool = False,
    clock: Callable[[], float] = time.perf_counter,
    created_at: str | None = None,
) -> dict[str, Any]:
    """Run the frozen resolver holdout with one exact model and no side effects.

    This function is deliberately not wired into the ordinary CLI, agent, memory,
    training, or lesson pipelines.  The caller must opt in explicitly.  Each case
    receives exactly one direct ``client.chat`` call with an empty tool list.  The
    returned receipt contains case IDs, status, latency, and boolean checks only;
    prompts, model output, contracts, and provider error text are never retained.
    """
    if allow_live is not True:
        raise PermissionError("live TaskContract benchmark requires allow_live=True")
    requested_model, provider, provider_model = _exact_model_reference(model)
    provider_can_attest_served_model = (
        provider in _SERVED_MODEL_ATTESTATION_PROVIDERS
    )
    fixture = load_task_contract_holdout(Path(fixture_path))
    cases = list(fixture["cases"])
    predictions: list[dict[str, Any]] = []
    case_receipts: list[dict[str, Any]] = []
    latencies: list[float] = []
    for case in cases:
        started = clock()
        reported_model: str | None = None
        model_attested = False
        status = "resolver_error"
        checks: dict[str, bool] | None = None
        try:
            contract, reported_model, model_attested = _resolve_one(
                case,
                client=client,
                requested_model=requested_model,
            )
            if (
                not provider_can_attest_served_model
                or not model_attested
                or reported_model is None
            ):
                # An exact-model exit claim requires an explicit attestation
                # signal plus provider-reported response metadata.  A model name
                # copied from the request (as CLI exec adapters historically
                # supplied) is routing telemetry, not evidence of what ran.
                status = "model_unattested"
            elif reported_model not in {
                requested_model,
                provider_model,
            }:
                status = "model_mismatch"
            else:
                prediction = _contract_prediction(str(case["id"]), contract)
                predictions.append(prediction)
                checks = _prompt_free_checks(case, prediction)
                status = "resolved"
        except TaskContractError:
            status = "contract_rejected"
        except Exception:
            # Provider diagnostics may contain request fragments.  The live
            # receipt records only a bounded class, never exception text.
            status = "provider_error"
        elapsed_ms = round(max(0.0, (clock() - started) * 1_000.0), 3)
        latencies.append(elapsed_ms)
        case_receipt: dict[str, Any] = {
            "id": str(case["id"]),
            "status": status,
            "latency_ms": elapsed_ms,
            "requested_model": requested_model,
            # Never retain arbitrary provider-supplied model text.  A mismatch
            # can contain request fragments or secret-like diagnostics; only a
            # verified equality is safe and useful in this prompt-free receipt.
            "model_attestation": (
                "verified"
                if status == "resolved"
                else "missing"
                if status == "model_unattested"
                else "mismatch"
                if status == "model_mismatch"
                else "not_observed"
            ),
        }
        if checks is not None:
            case_receipt["checks"] = checks
        case_receipts.append(case_receipt)

    all_resolved = len(predictions) == len(cases)
    contract_metrics = (
        _prompt_free_metrics(
            score_task_contract_holdout_contracts(fixture, predictions)
        )
        if all_resolved
        else None
    )
    completed_at = created_at or datetime.now(timezone.utc).isoformat()
    receipt: dict[str, Any] = {
        "schema_version": 1,
        "benchmark": "task_contract_holdout_v2",
        "fixture_sha256": fixture["fixture_sha256"],
        "created_at": completed_at,
        "provider": provider,
        "provider_model_attestation": (
            "explicit_response_signal_required"
            if provider_can_attest_served_model
            else "unavailable_for_selected_provider"
        ),
        "requested_model": requested_model,
        "model_attestation_required": True,
        "exact_model_only": bool(
            case_receipts
            and all(item["model_attestation"] == "verified" for item in case_receipts)
        ),
        "fallback_count": 0,
        "tools_supplied": 0,
        "training_eligible": False,
        "memory_writes": 0,
        "operator_text_retained": False,
        "summary": {
            "case_count": len(cases),
            "resolved": sum(item["status"] == "resolved" for item in case_receipts),
            "contract_rejected": sum(
                item["status"] == "contract_rejected" for item in case_receipts
            ),
            "provider_error": sum(
                item["status"] == "provider_error" for item in case_receipts
            ),
            "model_mismatch": sum(
                item["status"] == "model_mismatch" for item in case_receipts
            ),
            "model_unattested": sum(
                item["status"] == "model_unattested" for item in case_receipts
            ),
            "p50_latency_ms": _percentile(latencies, 0.50),
            "p95_latency_ms": _percentile(latencies, 0.95),
            "total_latency_ms": round(sum(latencies), 3),
            "contract_metrics": contract_metrics,
        },
        "cases": case_receipts,
    }
    # This detects accidental corruption only.  It is intentionally named a
    # checksum because an unkeyed hash can be recomputed after tampering.
    receipt["receipt_checksum_sha256"] = _receipt_checksum_sha256(receipt)
    return receipt


def _observed_tool_names(agent: Any, *, start_index: int = 0) -> list[str]:
    """Read schemas actually sent by an instrumented benchmark client."""
    requests = getattr(getattr(agent, "client", None), "requests", None)
    if not isinstance(requests, list):
        return []
    names: set[str] = set()
    for request in requests[max(0, int(start_index)):]:
        if not isinstance(request, Mapping):
            continue
        tools = request.get("tools")
        if not isinstance(tools, list):
            continue
        for schema in tools:
            function = schema.get("function") if isinstance(schema, Mapping) else None
            name = function.get("name") if isinstance(function, Mapping) else None
            if isinstance(name, str) and name.strip():
                names.add(name.strip())
    return sorted(names)


def _constraints_preserved(
    pending: Mapping[str, Any] | None,
    goals: Sequence[Mapping[str, Any]],
) -> bool:
    if pending is None:
        return False
    expected = {
        str(item).casefold() for item in pending.get("constraint_quotes") or []
    }
    for goal in goals:
        contract = goal.get("contract")
        if not isinstance(contract, Mapping):
            continue
        observed = {
            str(item).casefold() for item in contract.get("constraint_quotes") or []
        }
        if expected.issubset(observed):
            return True
    return False


def _observed_tool_effect(name: str) -> str:
    """Classify every declared mutation without a permissive read fallback."""
    from .tools import (
        DOCUMENT_WRITE_TOOLS,
        EXECUTION_TOOLS,
        EXTERNAL_MUTATION_TOOLS,
        FEATURE_SETUP_READ_TOOLS,
        FEATURE_SETUP_TOOLS,
        FILE_WRITE_TOOLS,
        MUTATING_TOOLS,
        SKILL_WRITE_TOOLS,
    )

    normalized = str(name or "unknown")
    write_tools = frozenset({
        *FILE_WRITE_TOOLS,
        *DOCUMENT_WRITE_TOOLS,
        *SKILL_WRITE_TOOLS,
        *(FEATURE_SETUP_TOOLS - FEATURE_SETUP_READ_TOOLS),
    })
    if normalized in {"schedule_create", "delegate_specialist"}:
        return "queue"
    if normalized in EXTERNAL_MUTATION_TOOLS:
        return "external"
    if normalized in EXECUTION_TOOLS and normalized in MUTATING_TOOLS:
        return "execute"
    if normalized in write_tools or normalized in MUTATING_TOOLS:
        # Remaining declared mutations are local state changes.  Coarsening
        # them to write is conservative; silently calling them reads is not.
        return "write"
    return "read"


def run_isolated_task_contract_outcome_benchmark(
    fixture_path: Path,
    *,
    contract_predictions: Sequence[Mapping[str, Any]],
    agent_factory: IsolatedTaskContractAgentFactory,
    allow_run: bool = False,
) -> dict[str, Any]:
    """Observe Phase-2 outcomes through real Agent/SQLite/restart boundaries.

    The runner owns a fresh workspace and database for every case.  It seeds the
    frozen pending goal when applicable, performs a real close/reopen before a
    restart-tagged continuation, invokes ``Agent.run``, and then reopens SQLite
    again before collecting tool audit rows and durable queue records.  No model
    or fixture-authored boolean can claim that an effect, queue, or restart
    happened.

    ``agent_factory`` exists only to supply the model/provider and a bounded test
    toolbox.  It must return the production ``Agent`` bound to the exact
    runner-owned ``Memory`` object.  A live provider without request-schema
    instrumentation will intentionally fail tool-exposure coverage rather than
    silently count as evidence.
    """
    if allow_run is not True:
        raise PermissionError(
            "isolated TaskContract outcome benchmark requires allow_run=True"
        )
    from .agent import Agent
    from .memory import Memory
    from .tools import ToolBox as ProductionToolBox

    fixture = load_task_contract_holdout(Path(fixture_path))
    predictions_by_id = {
        str(item.get("id") or ""): dict(item)
        for item in contract_predictions
        if isinstance(item, Mapping)
    }
    observations: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="jarvis-task-contract-outcomes-") as raw_root:
        root = Path(raw_root).resolve()
        for case in fixture["cases"]:
            case_id = str(case["id"])
            contract_prediction = predictions_by_id.get(case_id)
            if contract_prediction is None:
                # Let the authenticated scorer report the complete missing-ID
                # set after all runner cleanup has completed.
                continue
            case_root = (root / case_id).resolve()
            if case_root.parent != root:
                raise RuntimeError("holdout case ID escaped the isolated benchmark root")
            workspace = case_root / "workspace"
            data_dir = case_root / "data"
            workspace.mkdir(parents=True)
            data_dir.mkdir()
            database = data_dir / "jarvis.db"
            memory = Memory(database)
            conversation_id = memory.new_conversation(f"Phase 2 {case_id}")
            for prior in case.get("recent_user_turns") or []:
                memory.add_message(conversation_id, "user", str(prior))
            latest_assistant = case.get("latest_assistant_context")
            if isinstance(latest_assistant, str) and latest_assistant.strip():
                memory.add_message(conversation_id, "assistant", latest_assistant)
            pending = case.get("pending_contract")
            if isinstance(pending, Mapping):
                memory.begin_conversation_goal(
                    conversation_id,
                    str(pending["goal"]),
                    "conversation",
                    contract=pending,
                )

            restart_expected = "restart" in set(case.get("tags") or [])
            pre_run_reloaded = False
            if restart_expected:
                memory.close()
                memory = Memory(database)
                pre_run_reloaded = memory.pending_conversation_goal(conversation_id) is not None

            baseline_activity_ids = {
                int(item["id"]) for item in memory.list_activity(limit=10_000)
            }
            baseline_task_ids = {
                int(item["id"]) for item in memory.list_tasks(limit=10_000)
            }
            baseline_schedule_ids = {
                int(item["id"]) for item in memory.list_scheduled_jobs(limit=200)
            }
            events: list[str] = []
            final_status = "error"
            final_text = ""
            offered_tools: list[str] = []
            try:
                agent = agent_factory(case, memory, workspace, events.append)
                if not isinstance(agent, Agent) or getattr(agent, "memory", None) is not memory:
                    raise TypeError(
                        "agent_factory must return the production Agent bound to runner-owned Memory"
                    )
                if Path(agent.config.workspace).resolve() != workspace.resolve():
                    raise PermissionError(
                        "outcome benchmark agent must use the runner-owned workspace"
                    )
                if isinstance(getattr(agent, "toolbox", None), ProductionToolBox):
                    raise PermissionError(
                        "outcome benchmark rejects the production live-capability toolbox"
                    )
                if getattr(
                    getattr(agent, "toolbox", None),
                    "task_contract_outcome_isolated",
                    False,
                ) is not True:
                    raise PermissionError(
                        "outcome benchmark requires an explicitly isolated, non-live toolbox"
                    )
                client_requests = getattr(getattr(agent, "client", None), "requests", None)
                request_baseline = (
                    len(client_requests) if isinstance(client_requests, list) else 0
                )
                result = agent.run(
                    str(case["operator_prompt"]),
                    conversation_id=conversation_id,
                    prediction_origin="interactive",
                )
                final_status = str(getattr(result, "status", "error"))
                final_text = str(result)
                offered_tools = _observed_tool_names(
                    agent, start_index=request_baseline
                )
                result_trace_id = str(
                    getattr(result, "metrics", {}).get("trace_id") or ""
                ).strip()
            finally:
                memory.close()

            # A second open proves that effects counted below are durable SQLite
            # state, not objects left alive in the Agent process.
            reopened = Memory(database)
            try:
                activity: list[tuple[Mapping[str, Any], Mapping[str, Any]]] = []
                for item in reopened.list_activity(limit=10_000):
                    if (
                        int(item["id"]) in baseline_activity_ids
                        or str(item.get("category") or "") != "tool"
                    ):
                        continue
                    try:
                        details = json.loads(str(item.get("details_json") or "{}"))
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(details, Mapping):
                        continue
                    # A row from factory setup, another task, or a background
                    # worker cannot prove the foreground benchmark outcome.
                    if (
                        not result_trace_id
                        or str(details.get("trace_id") or "") != result_trace_id
                    ):
                        continue
                    activity.append((item, details))
                tool_events = [
                    {
                        "name": str(item.get("action") or "unknown"),
                        "status": (
                            "complete"
                            if str(item.get("status") or "") == "complete"
                            else "blocked"
                            if str(item.get("status") or "") == "blocked"
                            else "failed"
                        ),
                        "handler_dispatched": bool(
                            details.get("handler_dispatched") is True
                        ),
                        "effect": _observed_tool_effect(
                            str(item.get("action") or "unknown")
                        ),
                        "target_sha256": (
                            str(details.get("target_sha256"))
                            if isinstance(details.get("target_sha256"), str)
                            else None
                        ),
                        "receipt_id": (
                            str(details.get("result_receipt_id"))
                            if isinstance(details.get("result_receipt_id"), (str, int))
                            and not isinstance(details.get("result_receipt_id"), bool)
                            else None
                        ),
                        "matched_constraint_sha256": (
                            list(details.get("matched_constraint_sha256"))
                            if isinstance(
                                details.get("matched_constraint_sha256"), list
                            )
                            else []
                        ),
                    }
                    for item, details in activity
                ]
                new_tasks = [
                    item for item in reopened.list_tasks(limit=10_000)
                    if int(item["id"]) not in baseline_task_ids
                ]
                new_schedules = [
                    item for item in reopened.list_scheduled_jobs(limit=200)
                    if int(item["id"]) not in baseline_schedule_ids
                ]
                queue_records = [
                    {
                        "kind": "task",
                        "id": str(item["id"]),
                        "state": (
                            str(item.get("status") or "queued")
                            if str(item.get("status") or "queued")
                            in {"queued", "running", "pending"}
                            else "created"
                        ),
                        "purpose": str(item.get("prompt") or "queued task"),
                    }
                    for item in new_tasks
                ] + [
                    {
                        "kind": "schedule",
                        "id": str(item["id"]),
                        "state": "scheduled" if bool(item.get("enabled")) else "created",
                        "purpose": " ".join((
                            str(item.get("name") or ""),
                            str(item.get("prompt") or ""),
                            str(item.get("interval_minutes") or ""),
                        )).strip(),
                    }
                    for item in new_schedules
                ]
                goals = reopened.list_conversation_goals(conversation_id, limit=20)
            finally:
                reopened.close()

            observations.append({
                **contract_prediction,
                "final_status": final_status,
                "final_text": final_text,
                "offered_tools": offered_tools,
                "tool_events": tool_events,
                "durable_queue_records": queue_records,
                "restart_observation": {
                    "performed": restart_expected,
                    "database_reopened": restart_expected,
                    "pending_goal_reloaded": pre_run_reloaded,
                    "constraints_preserved": _constraints_preserved(pending, goals),
                },
            })
    return score_task_contract_holdout(fixture, observations)
