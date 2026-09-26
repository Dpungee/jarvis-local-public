# Phase 2 TaskContract resolver benchmark

This is the reproduction guide for the roadmap Phase 2 routing evidence: the
frozen 66-case TaskContract holdout, resolved live against one exact model, and
scored against the roadmap's own gate thresholds.

It answers three of the seven Phase 2 gate bullets and no others.

| Gate | Threshold | Scorer field |
|---|---|---|
| Broad routing | `route_accuracy` >= 0.90, and every lane in `route_by_lane` >= 0.90 | `score_task_contract_holdout_contracts` |
| Material-ambiguity clarification recall | `ambiguity_recall` >= 0.90 | same |
| Unnecessary clarification | `specified_false_positive_rate` <= 0.10 | same |

Verified end-to-end workflow completion is a different measurement with a
different runner and is not produced here.

## What the run does

`scripts/run_phase2_task_contract.py` drives
`jarvis.task_contract_benchmark.run_live_task_contract_benchmark` over
`tests/fixtures/task_contract_holdout_v2.json`.

- **One case, one call, one fresh conversation.** Each case is resolved by a
  single `client.chat` with an empty tool list. No case can see another case's
  prompt, contract, or output; there is no shared transcript to leak an answer
  into. No memory database is opened, no workspace is created, no training row
  is recorded, and there is no fallback model.
- **Decoding is pinned**: `temperature=0.0`, `seed=0`, `think=False`,
  `context_length=8192`, and the production
  `task_contract_response_schema()` as the structured-output grammar.

  Two of these deviate from the production resolver (`agent.py:8118-8129`) and
  the deviation is deliberate, so a run is comparable across hosts:
  production derives the context window per route with
  `self._context_length_for(route)` where the benchmark pins 8192, and
  production passes `keep_alive` when `_keep_alive_for(route)` returns one
  where the benchmark omits it entirely and lets the provider default apply.
  Temperature, seed, thinking, and the grammar are the production values
  unchanged. The benchmark tunes none of them to move a number.
- **Exact model only.** A case counts as resolved only when the provider both
  attests the served model and reports a name matching the requested one. A
  provider that cannot attest yields `model_unattested` and the run scores
  nothing.

## Prerequisites

- The model must be installed on the local provider. The runner checks this at
  startup and refuses rather than producing 66 error rows.
- Ollama must be reachable. The default base URL is `http://127.0.0.1:11434`;
  override with `--base-url`.

## Running it

```powershell
python scripts/run_phase2_task_contract.py `
  --model ollama:qwen3.5:9b `
  --base-manifest <path to the Phase 2 base manifest> `
  --allow-live
```

`--allow-live` is required. Without it nothing is called and the script exits
with status 2. Other flags: `--fixture` (default: the sealed v2 holdout),
`--out` (default:
`docs/evidence/phase2_task_contract_resolver_<provider>_<hash8>.json`), and
`--base-manifest-sha256` for declaring the base identity without reading the
manifest file.

Exit status is `0` when every gate row passes, `1` when a gate fails, and `2`
when the run was refused before any provider call.

Expect roughly 1.5 s per case on a local 9B model, so about 1.6 minutes for the
full 66. Offload the model afterwards if you are done with it.

## Choosing the model reference

`--model` takes an exact `provider:model` reference. `auto` is rejected, and so
is any provider that cannot attest which model actually served the response.

Only three providers can: `openai`, `anthropic`, and `ollama`. The CLI provider
adapters build their response envelope around the model name they were *asked*
for, which is routing telemetry rather than evidence of what ran, so
`claude-cli:` and `codex-cli:` references are refused at startup with that
reason. This is a deliberate boundary, not a gap to be loosened: an exact-model
receipt that cannot be traced to the provider's own report is not evidence.

Per the Phase 2 provider ruling, live runs use Ollama. `build_exact_model_
benchmark_client` constructs only the local Ollama client; `openai` and
`anthropic` clear the attestation gate and work through its `client_factory`
seam, but the function never reads an API key, an environment credential, or a
dotenv file to build one.

## The base identity

Phase 2 is uncommitted work, so evidence is anchored to the WP-0 file manifest
rather than to a commit. `--base-manifest` reads that manifest, recomputes its
recorded self-hash (the sha256 of the canonical JSON of its `files` mapping),
and refuses to write evidence if it does not verify. The first 8 characters of
that hash name the artifact.

## What the artifact contains

`docs/evidence/phase2_task_contract_resolver_<provider>_<hash8>.json` follows
`docs/EVALUATION.md`:

- base identity (manifest hash, verified), configuration class, the exact
  reproducing command, platform as **OS name and Python version only**,
  provider and reported version, and the model;
- a `gates` list: one row per roadmap gate with its threshold, comparator,
  observed value, and pass/fail, plus per-lane accuracy for the routing gate;
- the scorer's full aggregate metrics and the fixture's own
  `exit_criteria` result, reported side by side and unedited;
- `case_observations`: raw counts read off the receipt — which cases the
  parser rejected, which per-case checks failed, resolved-and-lane-correct
  counts per expected lane, and the clarification split. These are **not**
  scorer metrics and never stand in for one; they exist so an incomplete run
  is still diagnosable. Read a gate from `gates`, never from here;
- timing (p50, p95, total model latency, wall clock);
- attestation counts and known limitations;
- the prompt-free receipt, including per-case status and boolean checks.

It contains no prompt, no model output, no case text, no username, no local
path, and no hostname. Case identifiers and per-check booleans are retained on
purpose: they are what makes a failure diagnosable without reproducing the
operator's words.

## What it does not modify

The sealed fixture, its code-pinned digest constant
(`FROZEN_TASK_CONTRACT_HOLDOUT_V2_SHA256`), and the scorer in
`jarvis/task_contract_eval.py` are never edited or rescored. The gate rows in
the script restate the roadmap's thresholds so the artifact records what was
compared; the fixture's own criteria are reported separately and are in places
stricter than the roadmap's. A run can miss a fixture criterion while clearing
the roadmap gate it corresponds to, and the artifact shows both.

## The request-recording wrapper

`RequestRecordingBenchmarkClient` is instrumentation used by the *outcome*
runner, not by this resolver run. `_observed_tool_names` reads
`client.requests`; no production client keeps such a list, so an outcome run
against a real provider would observe zero offered tools for every case and
report tool exposure of zero — a measurement artefact, not agent behaviour.

The wrapper appends one dict per call (`messages`, `tools`, `model`,
`streamed`, and the remaining keyword arguments) and returns the provider's own
response object unchanged, so `model` and `model_attested` reach the receipt
exactly as the provider set them. Instrumentation must never become a place
where attestation is synthesised.

Two limits worth knowing before wiring it into an agent:

- It exposes `chat_stream` only when the wrapped client has one, because
  `Agent` selects streaming with `getattr(client, "chat_stream", None)`.
- It is not a `ModelClient` subclass, so an `Agent` that special-cases
  `isinstance(self.client, ModelClient)` (for example to pass a cancellation
  guard) will take the non-`ModelClient` branch. Wrap a bare provider client,
  or account for that branch.

Nothing in the shipped agent, CLI, or tool paths constructs the wrapper, and a
test asserts that no other module in `jarvis/` names it.

## All 66 or nothing

`score_task_contract_holdout_contracts` refuses a partial prediction set by
design. If even one case fails to produce a parseable contract, the run
computes no routing accuracy, no ambiguity recall, and no false-positive rate:
`contract_metrics` is `null` and every gate row reports `"observed": null` with
`passed: false` and a `not scored` note.

Read that as *the run was incomplete*, not as *the metric was measured and
missed*. The receipt's per-case `status` is where the incompleteness is
diagnosed.

## Reproducibility

Two runs against the same fixture always report the same `fixture_sha256`, and
`receipt_checksum_sha256` recomputes from the receipt as canonical JSON. The
*model's* output is a different matter: even at temperature 0.0 with a fixed
seed, the local runtime does not guarantee identical decoding, and a case
observed to resolve on one run can be rejected on the next. Treat a single run
as a bound on resolver behaviour, not a pin on it, and record the run you
actually observed.

## Interpreting a failure

The receipt's per-case `status` and `checks` separate the failure classes:

- `status: "contract_rejected"` — the resolver refused the model's contract.
  Not a routing error, and usually **not** a parse failure: `parse_task_contract`
  accepts the payload and the refusal comes from the post-parse reconciler,
  `reconcile_task_contract_continuation` (`task_contract.py:1502`), or from the
  `_validate_consistency` check at `:877` that it reaches through
  `bind_provided_material_continuation` (defined at `:1260`, called at
  `:1592`). Observed messages include
  "cancellation requires an explicit operator cancellation" (`:1535`),
  "continuation did not ground pending missing input: …" (`:1589`), and
  "acceptance omits the minimum evidence: …" (`:878`). The receipt records the
  class only, never the message.
- `status: "provider_error"` — a transport or grammar failure. The receipt
  records the class only, never the provider's message.
- `status: "resolved"` with `checks.lane` false — a genuine routing verdict on
  the wrong lane.
- `status: "resolved"` with `checks.clarification` false on a case the fixture
  marks ambiguous — a missed clarification, which is what gate 3 measures.

### `exact_model_only` is not the attestation result

Read attestation from `model_unattested` and `model_mismatch`. Those count
genuine attestation failures, and zero in both means every response that came
back was attested to the requested model.

`exact_model_only` is stricter than its name suggests: the per-case
`model_attestation` field is derived from the case's final `status`, so a case
whose response *was* attested on the wire but whose contract was then rejected
records `not_observed`. `exact_model_only` therefore goes false whenever any
case fails for a reason that has nothing to do with attestation. The receipt
shape is kept as-is because the outcome runner consumes it; separating
attestation into its own axis is a recorded follow-up for the module owner.

### Do not chase the number

A first run is expected to surface real resolver failures. Record what the
model does. Do not tune the prompt, the temperature, or the schema to move the
number — a rejection rate that falls because the prompt was reworded against
this fixture is a measurement of the rewording, not of the resolver.
