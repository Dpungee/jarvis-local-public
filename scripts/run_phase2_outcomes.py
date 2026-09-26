#!/usr/bin/env python3
"""Record roadmap Phase 2 verified-workflow-completion evidence.

This is the composed outcome run.  In one process it

1. resolves the sealed 66-case TaskContract holdout live against one exact,
   attesting ``provider:model`` reference (WP-2's
   ``run_live_task_contract_benchmark`` with ``return_predictions=True``), then
2. runs a per-lane smoke of six cases through the whole composition and reports
   the tools each case actually offered the model, then
3. drives ``run_isolated_task_contract_outcome_benchmark`` over all 66 cases
   with a factory that builds the **production** ``Agent`` around the isolated
   Phase-2 toolbox, the runner's own ``Memory`` object, and the runner's own
   workspace, and finally
4. writes one evidence artifact holding the sealed fixture's own outcome gates
   and, separately, the roadmap's "verified workflow completion >= 0.85" derived
   in ``jarvis/phase2_report.py`` from the scorer's per-case observations.

Nothing here edits, rescores, or reinterprets the sealed fixture, its code-pinned
digest constant, or ``jarvis/task_contract_eval.py``.

**The composition, stated plainly.**

* The agent's client is WP-2's ``RequestRecordingBenchmarkClient`` wrapped around
  an exact-model pin around the production ``ModelClient``.  The recorder exists
  because ``_observed_tool_names`` reads ``client.requests``; no production
  client keeps such a list, so without it every case would observe zero offered
  tools and tool exposure would read zero as a measurement artefact.  The
  recorder returns the provider's own response object unchanged.
* The pin reports exactly one installed model and refuses any other on the wire,
  so the router's failover cannot quietly measure a second model.
* The recorder is not a ``ModelClient`` subclass, so ``Agent`` would take the
  non-``ModelClient`` branch at its TaskContract seam and never attempt semantic
  resolution.  The recorder mirrors the wrapped client's capability, but the
  wrapped client here is the pin, and the production ``ModelClient`` carries no
  such attribute - so the pin is the layer that has to declare
  ``supports_task_contract``, and it declares it only when the object it wraps
  really is a ``ModelClient``.  This restores production behaviour that
  instrumentation removed; it synthesises nothing about the response.  Every
  case's resulting ``task_contract_status`` is recorded, and the smoke refuses
  the full run if any case reports ``not_supported``.
* ``config.model`` stays ``auto`` so the production router runs its real intent
  classification (its ``reason`` string gates semantic resolution), while all
  four profile models are pinned to the one benchmark model.  A single
  ``--model`` override would force ``Route("custom", ..., "manual model")`` and
  suppress the semantic lane, which is a measurement artefact, not the product.

Execution is disabled by ruling: a model-authored script would otherwise run
with the host user's own authority.  The three immediate-``execute`` cases are
recorded as structurally excluded for that reason, alongside the five
configuration-lane cases named in divergence 6 of the isolated toolbox.

The artifact follows ``docs/EVALUATION.md``: base identity, configuration class,
exact command, platform as OS name and Python version only, provider and
version, model, per-gate pass/fail, per-lane numbers, structural exclusions,
timing, and known limitations.  It carries no prompt, no model output, no case
text, no username, no local path, and no hostname.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from jarvis import phase2_report  # noqa: E402
from jarvis.task_contract_benchmark import (  # noqa: E402
    BenchmarkProviderError,
    LiveTaskContractRun,
    RequestRecordingBenchmarkClient,
    build_exact_model_benchmark_client,
    run_isolated_task_contract_outcome_benchmark,
    run_live_task_contract_benchmark,
)
from jarvis.task_contract_eval import (  # noqa: E402
    TaskContractFixtureError,
    load_task_contract_holdout,
)

DEFAULT_FIXTURE = PROJECT_ROOT / "tests" / "fixtures" / "task_contract_holdout_v2.json"
DEFAULT_MODEL = "ollama:qwen3.5:9b"
EVIDENCE_DIR = PROJECT_ROOT / "docs" / "evidence"

#: One case per lane for the mandatory pre-flight smoke, chosen to cover both
#: ``action_timing`` values that require tool exposure plus the dialogue lane
#: that requires none.  F-D: if the request-recording wrapper is missing or
#: mis-wired this fails in minutes instead of after the full run.
SMOKE_CASE_IDS = (
    "p2_dialogue_01",
    "p2_research_01",
    "p2_creation_01",
    "p2_inspection_02",
    "p2_external_10",
    "p2_configuration_01",
)

KNOWN_LIMITATIONS = (
    "One run, one model, one host. Benchmark numbers are historical "
    "observations, not permanent guarantees, and must be refreshed after any "
    "code, model, provider, or hardware change.",
    "Execution is disabled for this run by ruling. A model-authored script "
    "launched by the isolated toolbox would run with the host user's own "
    "authority, and no throwaway account exists on this host, so the three "
    "immediate-execute cases cannot be evidenced and are recorded as a "
    "structural exclusion rather than as agent failures.",
    "Five configuration-lane cases are structurally unevidenceable on this "
    "code base for the reasons recorded in divergence 6 of "
    "jarvis/task_contract_outcome_toolbox.py. They are excluded on the same "
    "terms and the rate over all 66 cases, counting them as failures, is "
    "reported alongside.",
    "Tool exposure is measured as schemas offered to the model, and two "
    "different things collapse into that one boolean. Only a minority of "
    "zero-exposure cases are an instrument limitation: a pre-loop deterministic "
    "path (agent.py:14481-14487 for an exact file target, agent.py:13756-13767 "
    "for live system status) dispatches a real tool and writes a real audit row "
    "with zero model calls, so no schema is ever offered for the instrument to "
    "see. The majority entered the model loop and were handed an EMPTY schema "
    "list, because agent.py:15270 short-circuits to schemas = [] whenever "
    "casual_greeting or dialogue_only is set; dialogue_only is computed at "
    "agent.py:13307-13331 from twenty-three flags, twenty-two of them "
    "deterministic prompt classifications and one "
    "(feature_configuration_requested) consulting the resolved contract only "
    "for lane == configuration. On a non-dialogue lane the resolved contract "
    "can therefore say the request needs an effect while that classifier still "
    "withholds every tool, and some of those cases did not finish. That is the "
    "agent declining to offer tools, not a measurement artefact. The split, the "
    "per-lane counts and the case ids are in tool_exposure_mechanisms; read it "
    "before attributing any exposure number.",
    "The isolated outcome toolbox is not the production ToolBox. Its six "
    "documented divergences - no approval gate, simulated external effects, "
    "fabricated web reads, real execution with production's residual "
    "authority, pre-dispatch audit digests, and the configuration lane - are "
    "recorded in that module's docstring and bound the meaning of every number "
    "here.",
    "The fixture's own exit criteria are in places stricter than the roadmap "
    "gates and are reported separately, unedited. immediate_evidence_rate_min "
    "is 1.0 against the roadmap's 0.85, and tool_exposure_rate must be exactly "
    "1.0, so a run can miss the fixture criteria while clearing the roadmap "
    "gate.",
    "Runs are not bit-reproducible. Even at temperature 0.0 with a fixed seed "
    "the local runtime does not guarantee identical decoding, and a case "
    "observed to resolve or complete on one run can differ on the next.",
    "The scorer refuses a partial case set. A resolver pass that cannot parse "
    "every case leaves the outcome run unscorable; the resolver passes are "
    "recorded individually so an incomplete run is diagnosable.",
    "Contract-level metrics in this artifact may be computed over a prediction "
    "set assembled from more than one independent resolver pass, because the "
    "scorer requires a contract for every case. They are therefore NOT the "
    "routing, ambiguity, or false-positive gate evidence; that is the separate "
    "single-pass resolver artifact. The completion verdict is unaffected: "
    "outcome_case_passes is derived only from the fixture's expected block and "
    "the observed run, never from the model's contract.",
    "The agent's client is an instrumentation wrapper, not a ModelClient "
    "subclass. agent.py gates semantic TaskContract resolution on that "
    "isinstance check or on a supports_task_contract attribute; the pin "
    "declares the attribute so the production lane still runs. Every case's "
    "task_contract_status is recorded, and a run in which any case reported "
    "'not_supported' is refused rather than reported.",
)

#: Appended when --skip-smoke was used, because the plan makes the smoke the
#: gate that proves the recorder is wired before a long run is trusted.
SKIPPED_SMOKE_LIMITATION = (
    "The mandatory per-lane smoke was skipped for this run. The smoke is what "
    "proves the request-recording wrapper is wired before the full run is "
    "trusted; without it a tool-exposure rate of zero cannot be separated from "
    "a mis-wired recorder, and the run's exposure numbers should not be read as "
    "agent behaviour."
)

#: Appended only when --note-provider-contention is set, after observing on the
#: provider that another benchmark held the same model resident.
PROVIDER_CONTENTION_LIMITATION = (
    "Another benchmark was driving the same local provider during this run, "
    "holding the same model resident at a different context length. The "
    "provider reloads the model when the requested context length changes, so "
    "the latency figures here include reload time and are an upper bound, not "
    "a measurement of the model's throughput. Correctness is unaffected: "
    "context length is a per-request parameter and every request in this run "
    "carried this runner's own."
)


class PinnedBenchmarkModelClient:
    """Expose exactly one model to the agent, and change nothing else.

    Two things this guards, both of which would otherwise silently corrupt an
    exact-model receipt:

    * ``models()`` reports only the benchmark model, so ``ModelRouter``'s
      failover has no second candidate to reach for after a provider error.
    * ``chat``/``chat_stream`` refuse any other model reference outright rather
      than measuring it.

    It also declares ``supports_task_contract`` when the wrapped object really is
    a production ``ModelClient``: the recording wrapper is not a ``ModelClient``
    subclass, and ``agent.py`` gates semantic TaskContract resolution on that
    ``isinstance`` check or on this attribute.  Declaring it restores production
    behaviour that instrumentation removed.  Nothing about a response is
    synthesised here; responses are returned exactly as the provider built them.
    """

    def __init__(self, client: Any, model: str, *, supports_task_contract: bool) -> None:
        self._client = client
        self._model = str(model)
        self.supports_task_contract = bool(supports_task_contract)
        self.refusals: list[str] = []
        # M-5: "the run used the pinned model" and "the provider attested which
        # model served the response" are different claims. The request side is
        # enforced by _checked; this counts the response side, so the artifact
        # can state attestation for the agent phase instead of borrowing the
        # resolver's.
        self.calls = 0
        self.attested_calls = 0
        if callable(getattr(client, "chat_stream", None)):
            self.chat_stream = self._pinned_chat_stream

    @property
    def wrapped(self) -> Any:
        return self._client

    def models(self, refresh: bool = False) -> list[str]:
        del refresh
        return [self._model]

    def _checked(self, model: Any) -> str:
        requested = str(model or "").strip()
        if requested != self._model:
            self.refusals.append(requested or "<empty>")
            raise BenchmarkProviderError(
                "the Phase 2 outcome run is pinned to one exact model and was "
                "asked for another; a run must never measure a model its "
                "receipt does not name"
            )
        return requested

    def _count(self, response: Any) -> Any:
        """Count the provider's own attestation bit. Never set it."""
        self.calls += 1
        attested = getattr(response, "model_attested", None)
        if attested is None and isinstance(response, Mapping):
            attested = response.get("model_attested")
        if attested is True:
            self.attested_calls += 1
        return response

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        model: str | None = None,
        **kwargs: Any,
    ) -> Any:
        return self._count(
            self._client.chat(messages, tools, self._checked(model), **kwargs)
        )

    def _pinned_chat_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        model: str | None,
        on_delta: Any,
        **kwargs: Any,
    ) -> Any:
        return self._count(
            self._client.chat_stream(
                messages, tools, self._checked(model), on_delta, **kwargs
            )
        )

    def __getattr__(self, name: str) -> Any:
        client = self.__dict__.get("_client")
        if client is None:
            raise AttributeError(name)
        return getattr(client, name)


def offered_tool_names(client: Any) -> list[str]:
    """Read offered schemas the way ``_observed_tool_names`` does."""
    names: set[str] = set()
    for request in getattr(client, "requests", None) or []:
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


class OutcomeAgentFactory:
    """Build the production ``Agent`` around the isolated Phase-2 toolbox.

    The runner asserts, after this returns, that the object is the production
    ``Agent``, that it is bound to the runner's own ``Memory`` instance and
    workspace, that its toolbox is not the production ``ToolBox``, and that the
    toolbox declares ``task_contract_outcome_isolated``.  None of those
    assertions is defeated here.
    """

    def __init__(
        self,
        base_config: Any,
        production_client: Any,
        *,
        provider_model: str,
        supports_task_contract: bool,
    ) -> None:
        self._base_config = base_config
        self._production_client = production_client
        self._provider_model = str(provider_model)
        self._supports_task_contract = bool(supports_task_contract)
        self.records: list[dict[str, Any]] = []

    def __call__(
        self,
        case: Mapping[str, Any],
        memory: Any,
        workspace: Path,
        on_event: Any,
    ) -> Any:
        from jarvis.agent import Agent
        from jarvis.task_contract_outcome_toolbox import TaskContractOutcomeToolbox

        workspace = Path(workspace).resolve()
        # The runner lays out <case root>/workspace and <case root>/data; the
        # isolated toolbox refuses a data directory inside the scored workspace
        # so its scratch never shows up in list_files or the storage report.
        data_dir = (workspace.parent / "data").resolve()
        data_dir.mkdir(parents=True, exist_ok=True)
        config = replace(self._base_config, workspace=workspace, data_dir=data_dir)
        # A pin per case, so the attestation and refusal counters it keeps are
        # per case rather than a single running total for the whole run.
        pin = PinnedBenchmarkModelClient(
            self._production_client,
            self._provider_model,
            supports_task_contract=self._supports_task_contract,
        )
        client = RequestRecordingBenchmarkClient(pin)
        events: list[str] = []

        def record_event(text: str) -> None:
            events.append(str(text))
            on_event(text)

        agent = Agent(config, memory, record_event, client=client)
        agent.toolbox = TaskContractOutcomeToolbox(
            config, memory, workspace=workspace, data_dir=data_dir
        )
        record: dict[str, Any] = {
            "id": str(case["id"]),
            "lane": str(case["expected"]["lane"]),
            "action_timing": str(case["expected"]["action_timing"]),
            "requested_effect": str(case["expected"]["requested_effect"]),
            "client": client,
            "pin": pin,
            "events": events,
            "task_contract_status": "not_observed",
            "final_status": "not_observed",
        }

        # ``Agent`` resets ``_active_task_contract_status`` to "not_attempted"
        # when a run finishes, so the live attribute cannot be read afterwards.
        # It survives on the sanitised run metrics, which is where this reads it.
        # A wrapper around the bound method observes the production run without
        # altering it; the runner still invokes ``agent.run`` exactly once.
        production_run = agent.run

        def observed_run(*call_args: Any, **call_kwargs: Any) -> Any:
            result = production_run(*call_args, **call_kwargs)
            metrics = getattr(result, "metrics", None) or {}
            record["task_contract_status"] = str(
                metrics.get("task_contract_status") or "not_observed"
            )
            record["final_status"] = str(getattr(result, "status", "not_observed"))
            return result

        agent.run = observed_run  # type: ignore[method-assign]
        self.records.append(record)
        return agent

    def case_rows(self) -> list[dict[str, Any]]:
        """Prompt-free per-case instrumentation, read after the run."""
        rows: list[dict[str, Any]] = []
        for record in self.records:
            client = record["client"]
            requests = list(getattr(client, "requests", None) or [])
            offered = offered_tool_names(client)
            pin = record["pin"]
            rows.append({
                "id": record["id"],
                "lane": record["lane"],
                "action_timing": record["action_timing"],
                "requested_effect": record["requested_effect"],
                "task_contract_status": record["task_contract_status"],
                "final_status": record["final_status"],
                "model_calls": len(requests),
                "provider_responses": int(getattr(pin, "calls", 0)),
                "attested_responses": int(getattr(pin, "attested_calls", 0)),
                "off_model_refusals": list(getattr(pin, "refusals", []) or []),
                "models_requested": sorted(
                    {str(item.get("model")) for item in requests}
                ),
                "offered_tools": offered,
                "tool_exposure_observed": bool(offered),
                "event_kinds": sorted({
                    str(text).split(" - ", 1)[0] for text in record["events"]
                }),
            })
        return rows


def build_clients(
    model: str, base_url: str | None
) -> tuple[Any, dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Return the resolver's exact-model client plus the evidence rows it fixes.

    ``(client, provider row, model row, {"provider_model": ...})``.  Building it
    first is also the startup attestation gate: an unattestable provider raises
    ``BenchmarkProviderError`` here, before any provider call.
    """
    exact = build_exact_model_benchmark_client(model, base_url=base_url)
    provider = {
        "name": exact.provider,
        "version": exact.provider_version,
    }
    model_row = {"requested": exact.requested_model, "served": exact.provider_model}
    return exact, provider, model_row, {"provider_model": exact.provider_model}


def base_config_for(provider_model: str, base_url: str | None) -> Any:
    """Load the host configuration and pin every routing profile to one model.

    ``model`` stays ``auto`` deliberately: the production router's intent
    classification, and the ``reason`` string that gates semantic TaskContract
    resolution, are part of what this run measures.  Overriding ``model``
    instead would force a manual route and suppress that lane.
    """
    from jarvis.config import Config

    config = Config.load()
    overrides: dict[str, Any] = {
        "model": "auto",
        "fast_model": provider_model,
        "reasoning_model": provider_model,
        "coding_model": provider_model,
        "deep_model": provider_model,
        "learning_model": provider_model,
        "cloud_enabled": False,
        "ollama_enabled": True,
        "ollama_preload": False,
    }
    if base_url:
        overrides["ollama_url"] = base_url
    return replace(config, **overrides)


def run_resolver_passes(
    fixture_path: Path,
    *,
    client: Any,
    model: str,
    max_passes: int,
    case_ids: Sequence[str],
    log: Any,
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """Resolve the holdout, repeating only to recover cases the parser rejected.

    Each pass is an independent single-shot run of the sealed resolver benchmark
    at pinned decoding; nothing is tuned between passes.  Later passes exist
    only so the outcome runner has a contract for every case - the scorer
    refuses a partial set - and they cannot flatter the completion number:
    ``outcome_case_passes`` is derived entirely from the fixture's ``expected``
    block and the observed run, never from the model's contract.

    Contract-level metrics computed over a multi-pass prediction set are
    therefore **not** routing-gate evidence; that is the separate resolver
    artifact, whose first pass is recorded here unmodified for comparison.
    """
    predictions: dict[str, dict[str, Any]] = {}
    receipts: list[dict[str, Any]] = []
    for attempt in range(1, max(1, int(max_passes)) + 1):
        started = time.monotonic()
        run = run_live_task_contract_benchmark(
            fixture_path,
            client=client,
            model=model,
            allow_live=True,
            return_predictions=True,
        )
        assert isinstance(run, LiveTaskContractRun)
        recovered: list[str] = []
        for prediction in run.predictions:
            case_id = str(prediction["id"])
            if case_id not in predictions:
                predictions[case_id] = dict(prediction)
                if attempt > 1:
                    recovered.append(case_id)
        summary = dict(run.receipt["summary"])
        summary.pop("contract_metrics", None)
        receipts.append({
            "pass": attempt,
            "fixture_sha256": run.receipt["fixture_sha256"],
            "exact_model_only": run.receipt["exact_model_only"],
            "receipt_checksum_sha256": run.receipt["receipt_checksum_sha256"],
            "summary": summary,
            "unresolved_case_ids": sorted(
                str(item["id"])
                for item in run.receipt["cases"]
                if item["status"] != "resolved"
            ),
            "recovered_case_ids": sorted(recovered),
            "wall_seconds": round(time.monotonic() - started, 3),
        })
        missing = [case_id for case_id in case_ids if case_id not in predictions]
        log(
            f"resolver pass {attempt}: resolved {summary['resolved']}/"
            f"{summary['case_count']} this pass, {len(predictions)} distinct "
            f"contracts held, {len(missing)} still missing"
        )
        if not missing:
            break
    return predictions, receipts


def plan_gate_fields(rate: float | None) -> dict[str, Any]:
    """The plan's smoke gate, stated the same way wherever it is reported.

    One definition, used by the live smoke and by a rebuild that reads a saved
    smoke, so the two can never disagree about whether the gate was met.
    """
    return {
        "plan_gate": "tool_exposure_rate == 1.0",
        "plan_gate_met": rate == 1.0,
        "plan_gate_note": (
            None
            if rate == 1.0
            else (
                "the plan's smoke gate (tool_exposure_rate == 1.0) was missed at "
                f"{rate}. The boss ruled the full run should proceed anyway: the "
                "gate exists to catch a mis-wired request recorder, and "
                "p2_creation_01 recording 17 offered schemas through the same "
                "wrapper disproves that failure mode, while the stronger wiring "
                "gate (no case reporting task_contract_status 'not_supported') "
                "passed. The remaining zero-exposure cases are agent behaviour, "
                "reported per lane and per mechanism in "
                "tool_exposure_mechanisms, not instrumentation."
            )
        ),
    }


def run_smoke(
    fixture_path: Path,
    *,
    predictions: Mapping[str, Mapping[str, Any]],
    factory: "OutcomeAgentFactory",
    log: Any,
) -> dict[str, Any]:
    """Per-lane smoke: six cases, one per lane, through the whole composition.

    The scorer refuses a partial set by design, so the smoke drives the real
    runner over six cases and reads the refusal as confirmation of that rule;
    the exposure it reports comes from the same request records the runner reads.
    """
    smoke_predictions = [
        dict(predictions[case_id])
        for case_id in SMOKE_CASE_IDS
        if case_id in predictions
    ]
    missing = [case_id for case_id in SMOKE_CASE_IDS if case_id not in predictions]
    started = time.monotonic()
    refusal: str | None = None
    try:
        run_isolated_task_contract_outcome_benchmark(
            fixture_path,
            contract_predictions=smoke_predictions,
            agent_factory=factory,
            allow_run=True,
        )
    except TaskContractFixtureError as error:
        # Expected: six of sixty-six is a partial set. The observations were
        # collected; only the sealed scoring step refuses.
        refusal = str(error)[:200]
    rows = factory.case_rows()
    required = [row for row in rows if row["action_timing"] in {"immediate", "future"}]
    observed = [row for row in required if row["tool_exposure_observed"]]
    # A client wrapper that is not a ModelClient and declares no
    # supports_task_contract makes agent.py fall to "not_supported" and skip
    # semantic resolution entirely, which routes the Agent differently from the
    # resolver benchmark and loses the artifact-acceptance path the outcome
    # scorer keys on.  That is a wiring defect, not a model verdict, so the
    # smoke fails on it rather than letting a full run record it as behaviour.
    unsupported = sorted(
        row["id"] for row in rows if row["task_contract_status"] == "not_supported"
    )
    for row in rows:
        log(
            f"  smoke {row['id']:<22s} {row['lane']:<16s} "
            f"{row['action_timing']:<9s} calls={row['model_calls']:<3d} "
            f"tools={len(row['offered_tools']):<3d} "
            f"contract={row['task_contract_status']}"
        )
    rate = round(len(observed) / len(required), 6) if required else None
    return {
        "case_ids": list(SMOKE_CASE_IDS),
        "missing_predictions": missing,
        "cases": rows,
        "tool_exposure_required": len(required),
        "tool_exposure_observed": len(observed),
        "tool_exposure_rate": rate,
        # M-4: the plan's smoke gate is tool_exposure_rate == 1.0. It was not
        # met. Recording it as met, or omitting it, would misstate what the
        # smoke proved.
        **plan_gate_fields(rate),
        "task_contract_status_counts": {
            status: sum(1 for row in rows if row["task_contract_status"] == status)
            for status in sorted({row["task_contract_status"] for row in rows})
        },
        "task_contract_not_supported_case_ids": unsupported,
        "task_contract_wiring_ok": not unsupported,
        "partial_set_refused": refusal is not None,
        "wall_seconds": round(time.monotonic() - started, 3),
    }


def command_line(args: argparse.Namespace) -> str:
    parts = [
        "python scripts/run_phase2_outcomes.py",
        f"--model {args.model}",
        f"--resolver-passes {args.resolver_passes}",
    ]
    if args.base_manifest is not None:
        parts.append("--base-manifest <phase 2 base manifest>")
    if args.base_manifest_sha256 is not None:
        parts.append(f"--base-manifest-sha256 {args.base_manifest_sha256}")
    if args.skip_smoke:
        parts.append("--skip-smoke")
    if args.smoke_only:
        parts.append("--smoke-only")
    parts.append("--allow-live")
    return " ".join(parts)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--base-manifest", type=Path, default=None)
    parser.add_argument("--base-manifest-sha256", default=None)
    parser.add_argument(
        "--resolver-passes",
        type=int,
        default=3,
        help=(
            "maximum independent resolver passes; later passes only recover "
            "cases the parser rejected, so the outcome runner has a contract "
            "for every sealed case"
        ),
    )
    parser.add_argument("--skip-smoke", action="store_true")
    parser.add_argument("--smoke-only", action="store_true")
    parser.add_argument(
        "--note-provider-contention",
        action="store_true",
        help=(
            "record that another benchmark was driving the same local provider "
            "during this run; set it only after observing that on the provider"
        ),
    )
    parser.add_argument(
        "--allow-live",
        action="store_true",
        help="required; without it nothing is called",
    )
    parser.add_argument(
        "--rebuild-from",
        type=Path,
        default=None,
        help=(
            "regenerate the artifact from an earlier run's saved evidence, "
            "making no provider call; the run's observations are reused "
            "verbatim and only the report around them is rebuilt"
        ),
    )
    return parser.parse_args(argv)


def rebuild(args: argparse.Namespace, log: Any) -> int:
    """Rebuild an artifact from a saved run without contacting the provider.

    Every observation - resolver receipts, the smoke, the per-case
    instrumentation, the timing and the scoring refusal - is carried over
    unchanged from the source artifact.  Only the reporting around it is
    recomputed, so a correction to the report cannot quietly become a
    correction to the measurement.
    """
    source = json.loads(Path(args.rebuild_from).read_bytes().decode("utf-8"))
    fixture = load_task_contract_holdout(args.fixture)
    if str(source["fixture"]["sha256"]) != str(fixture["fixture_sha256"]):
        log("refused: the saved run used a different fixture")
        return 2
    notes = list(source.get("tool_exposure_notes") or [])
    attested = [
        int(item["attested_responses"])
        for item in notes
        if isinstance(item.get("attested_responses"), int)
    ]
    calls = sum(int(item.get("model_calls") or 0) for item in notes)
    resolver = list(source.get("resolver_passes") or [])
    attestation = {
        "outcome_phase": {
            "model_calls": calls,
            "attested_responses": sum(attested) if attested else None,
            "requests_naming_the_pinned_model": calls,
            "note": (
                "every outcome request named the pinned model, which the pin "
                "enforces on the wire; served-model attestation was verified for "
                "the resolver phase only. This run predates the per-response "
                "attestation counter, so attested_responses is null rather than "
                "asserted."
                if not attested
                else "attested_responses counts responses whose provider set "
                "model_attested true; the pin never sets that bit."
            ),
        },
        "resolver_phase": {
            "passes": len(resolver),
            "exact_model_only": [bool(item["exact_model_only"]) for item in resolver],
            "model_unattested": [
                int(item["summary"]["model_unattested"]) for item in resolver
            ],
            "model_mismatch": [
                int(item["summary"]["model_mismatch"]) for item in resolver
            ],
            "note": (
                "exact_model_only is false on every pass as a consequence of the "
                "six parser rejections, not of an attestation failure: the "
                "receipt sets it only when every case reached "
                "model_attestation 'verified', and a rejected contract never "
                "gets that far. model_unattested and model_mismatch are zero on "
                "every pass, so every response the provider did return attested "
                "the pinned model."
            ),
        },
    }
    # The smoke's measurements are carried over untouched; only the gate
    # statement around them is (re)derived, through the shared helper.
    smoke = source.get("smoke")
    if isinstance(smoke, Mapping):
        smoke = {**smoke, **plan_gate_fields(smoke.get("tool_exposure_rate"))}
    # L-4: the saved artifact recorded the environment switches the original run
    # observed, but not which of them base_config_for overrode in code, nor the
    # model pinning no environment variable would show. Both are facts about
    # that run, so they are reconstructed from the saved values here rather than
    # by reading this process's environment, which may differ.
    served = str(source["model"]["served"])
    saved_config = dict(source["configuration_class"])
    saved_switches = dict(saved_config.get("switches") or {})
    forced_switches = {
        "JARVIS_CLOUD_ENABLED": "false",
        "JARVIS_OLLAMA_ENABLED": "true",
    }
    configuration = {
        "summary": saved_config.get("summary", ""),
        "switches": {**saved_switches, **forced_switches},
        "switch_sources": {
            name: (
                "forced_by_runner" if name in forced_switches else "environment"
            )
            for name in sorted({*saved_switches, *forced_switches})
        },
        "forced_runtime": {
            "config.model": "auto",
            "fast_model": served,
            "reasoning_model": served,
            "coding_model": served,
            "deep_model": served,
            "learning_model": served,
            "ollama_preload": False,
            "models_offered_to_the_router": [served],
        },
    }
    limitations = list(source.get("known_limitations") or [])
    rebuilt_note = (
        "This artifact was regenerated from the saved observations of the run "
        "recorded at "
        f"{source['created_at']}; no provider call was made during the rebuild. "
        "The measurements are that run's; the reporting around them is this "
        "code's."
    )
    evidence = phase2_report.build_outcome_evidence(
        fixture=fixture,
        scored=None,
        resolver_receipts=resolver,
        smoke=smoke,
        base=source["base"],
        configuration=configuration,
        provider=source["provider"],
        model=source["model"],
        command=str(source["command"]),
        timing=source["timing"],
        known_limitations=[
            item
            for item in limitations
            if not item.startswith("Tool exposure is measured")
        ]
        + [KNOWN_LIMITATIONS[3], rebuilt_note],
        created_at=datetime.now(timezone.utc).isoformat(),
        scoring_refusal=source.get("scoring_refusal"),
        exposure_notes=notes,
        attestation=attestation,
    )
    out = args.out or Path(args.rebuild_from)
    size = phase2_report.write_evidence(out, evidence)
    log(f"rebuilt {out.name} ({size} bytes) with no provider call")
    return report_exit_code(evidence, log)


def report_exit_code(evidence: Mapping[str, Any], log: Any) -> int:
    """Exit codes, documented.

    0  every gate row passed
    1  a sealed-fixture criterion failed, the roadmap gate held
    4  the roadmap verified-completion gate failed
    5  the run produced no score at all, so no gate was measured
    """
    rows = evidence["gates"]
    for row in rows:
        log(
            f"  gate {row['name']:<46s} {row['comparator']} "
            f"{row['threshold']!r:<8} observed {row['observed']!r:<10} "
            f"{'PASS' if row['passed'] else 'FAIL'}"
        )
    if evidence["gates_passed"]:
        return 0
    if evidence["verified_workflow_completion"] is None:
        log("no gate was measured: the run produced no score")
        return 5
    roadmap_failed = any(
        not row["passed"]
        for row in rows
        if row["source"] == "roadmap_gate_bullet_2"
    )
    return 4 if roadmap_failed else 1


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    started_wall = time.monotonic()

    def log(message: str) -> None:
        print(message, flush=True)

    if args.rebuild_from is not None:
        return rebuild(args, log)
    if not args.allow_live:
        log("refused: --allow-live is required before any provider call")
        return 2
    try:
        base = phase2_report.base_identity(
            args.base_manifest, args.base_manifest_sha256
        )
    except phase2_report.Phase2ReportError as error:
        log(f"refused: {error}")
        return 2
    try:
        exact_client, provider, model_row, extra = build_clients(
            args.model, args.base_url
        )
    except (BenchmarkProviderError, ValueError) as error:
        log(f"refused: {error}")
        return 2

    fixture = load_task_contract_holdout(args.fixture)
    case_ids = list(phase2_report.iter_case_ids(fixture))
    base_config = base_config_for(extra["provider_model"], args.base_url)

    from jarvis.model_client import ModelClient, build_model_client

    production_client = build_model_client(base_config)
    # The resolver phase uses WP-2's own exact-model adapter; the agent phase
    # gets a fresh pin per case, built by the factory.
    supports_task_contract = isinstance(production_client, ModelClient)

    resolver_started = time.monotonic()
    predictions, resolver_receipts = run_resolver_passes(
        args.fixture,
        client=exact_client,
        model=args.model,
        max_passes=args.resolver_passes,
        case_ids=case_ids,
        log=log,
    )
    resolver_seconds = round(time.monotonic() - resolver_started, 3)

    smoke: dict[str, Any] | None = None
    if not args.skip_smoke:
        log("per-lane smoke: six cases, one per lane")
        smoke = run_smoke(
            args.fixture,
            predictions=predictions,
            factory=OutcomeAgentFactory(
                base_config,
                production_client,
                provider_model=extra["provider_model"],
                supports_task_contract=supports_task_contract,
            ),
            log=log,
        )
        log(
            "smoke tool exposure: "
            f"{smoke['tool_exposure_observed']}/{smoke['tool_exposure_required']}"
            f" = {smoke['tool_exposure_rate']}"
        )
        if not smoke["task_contract_wiring_ok"]:
            log(
                "smoke refused the full run: the Agent reported "
                "task_contract_status 'not_supported' for "
                f"{smoke['task_contract_not_supported_case_ids']}. The client "
                "wrapper is not a ModelClient and declares no "
                "supports_task_contract, so semantic resolution was skipped and "
                "the run would measure a differently routed Agent."
            )
            return 3
        if smoke["tool_exposure_rate"] != 1.0:
            log(
                "smoke did not reach tool_exposure_rate == 1.0; recording the "
                "lanes that observed none and continuing to the full run per "
                "the ruling"
            )
    if args.smoke_only:
        log("smoke only: stopping before the full run")
        return 0

    missing = [case_id for case_id in case_ids if case_id not in predictions]
    log(f"full run: {len(case_ids) - len(missing)} of {len(case_ids)} cases have a contract")
    outcome_started = time.monotonic()
    factory = OutcomeAgentFactory(
        base_config,
        production_client,
        provider_model=extra["provider_model"],
        supports_task_contract=supports_task_contract,
    )
    scored: dict[str, Any] | None = None
    refusal: str | None = None
    try:
        scored = run_isolated_task_contract_outcome_benchmark(
            args.fixture,
            contract_predictions=[dict(predictions[key]) for key in predictions],
            agent_factory=factory,
            allow_run=True,
        )
    except TaskContractFixtureError as error:
        refusal = str(error)[:400]
        log(f"outcome scoring refused: {refusal}")
    except Exception as error:  # noqa: BLE001 - the run is long; keep its evidence
        # A single case that raises must not discard the per-case
        # instrumentation for every case that already ran. Only the exception's
        # class is recorded: its message can quote a prompt or a local path.
        refusal = f"outcome run aborted: {type(error).__name__}"
        log(refusal)
    outcome_seconds = round(time.monotonic() - outcome_started, 3)

    case_rows = factory.case_rows()
    exposure_notes = [
        {
            "id": row["id"],
            "lane": row["lane"],
            "action_timing": row["action_timing"],
            "task_contract_status": row["task_contract_status"],
            "final_status": row["final_status"],
            "model_calls": row["model_calls"],
            "models_requested": row["models_requested"],
            "offered_tool_count": len(row["offered_tools"]),
            "offered_tools": row["offered_tools"],
        }
        for row in case_rows
    ]
    off_model = sorted({
        name
        for row in case_rows
        for name in row["models_requested"]
        if name != extra["provider_model"]
    })
    if off_model:
        log(f"refusing to record evidence: off-model requests observed {off_model}")
        return 2
    evidence = phase2_report.build_outcome_evidence(
        fixture=fixture,
        scored=scored,
        resolver_receipts=resolver_receipts,
        smoke=smoke,
        base=base,
        configuration=phase2_report.configuration_class(
            "production Agent over the isolated Phase-2 outcome toolbox; one "
            "exact local model pinned across every routing profile with "
            "config.model left on auto; cloud providers disabled; execution, "
            "external access and computer access disabled; runner-owned "
            "workspace, data directory and SQLite database per case",
            forced_switches={
                "JARVIS_CLOUD_ENABLED": "false",
                "JARVIS_OLLAMA_ENABLED": "true",
            },
            forced_runtime={
                "config.model": "auto",
                "fast_model": extra["provider_model"],
                "reasoning_model": extra["provider_model"],
                "coding_model": extra["provider_model"],
                "deep_model": extra["provider_model"],
                "learning_model": extra["provider_model"],
                "ollama_preload": False,
                "models_offered_to_the_router": [extra["provider_model"]],
            },
        ),
        provider=provider,
        model=model_row,
        command=command_line(args),
        timing={
            "resolver_seconds": resolver_seconds,
            "smoke_seconds": None if smoke is None else smoke["wall_seconds"],
            "outcome_seconds": outcome_seconds,
            "wall_seconds": round(time.monotonic() - started_wall, 3),
            "outcome_seconds_per_case": (
                round(outcome_seconds / len(case_rows), 3) if case_rows else None
            ),
        },
        known_limitations=(
            list(KNOWN_LIMITATIONS)
            + ([PROVIDER_CONTENTION_LIMITATION] if args.note_provider_contention else [])
            + ([SKIPPED_SMOKE_LIMITATION] if args.skip_smoke else [])
        ),
        created_at=datetime.now(timezone.utc).isoformat(),
        scoring_refusal=refusal,
        exposure_notes=exposure_notes,
        attestation={
            "outcome_phase": {
                "model_calls": sum(row["model_calls"] for row in case_rows),
                "provider_responses": sum(
                    row["provider_responses"] for row in case_rows
                ),
                "attested_responses": sum(
                    row["attested_responses"] for row in case_rows
                ),
                "off_model_refusals": sum(
                    len(row["off_model_refusals"]) for row in case_rows
                ),
                "note": (
                    "attested_responses counts responses whose provider set "
                    "model_attested true; the pin never sets that bit, and it "
                    "refuses any request naming another model before it reaches "
                    "the wire."
                ),
            },
            "resolver_phase": {
                "passes": len(resolver_receipts),
                "exact_model_only": [
                    bool(item["exact_model_only"]) for item in resolver_receipts
                ],
                "model_unattested": [
                    int(item["summary"]["model_unattested"])
                    for item in resolver_receipts
                ],
                "model_mismatch": [
                    int(item["summary"]["model_mismatch"])
                    for item in resolver_receipts
                ],
                "note": (
                    "exact_model_only false on a pass is a consequence of that "
                    "pass's parser rejections, not of an attestation failure: "
                    "the receipt sets it only when every case reached "
                    "model_attestation 'verified', and a rejected contract never "
                    "gets that far. Read it together with model_unattested and "
                    "model_mismatch."
                ),
            },
        },
    )
    hash8 = str(base["manifest_sha256"])[:8]
    out = args.out or (
        EVIDENCE_DIR
        / f"phase2_task_contract_outcomes_{provider['name']}_{hash8}.json"
    )
    size = phase2_report.write_evidence(out, evidence)
    log(f"wrote {out.name} ({size} bytes)")
    completion = evidence["verified_workflow_completion"]
    if completion is not None:
        log(
            "verified workflow completion: "
            f"scorable {completion['scorable_cases']['passes']}/"
            f"{completion['scorable_cases']['total']} = "
            f"{completion['scorable_cases']['rate']}; all cases "
            f"{completion['all_cases']['passes']}/"
            f"{completion['all_cases']['total']} = "
            f"{completion['all_cases']['rate']}"
        )
    return report_exit_code(evidence, log)


if __name__ == "__main__":  # pragma: no cover - entry point
    raise SystemExit(main())
