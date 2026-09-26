# Long-horizon stage executor — design

**Status:** design only (roadmap Phase 2, WP-6), revision 2 after an adversarial review returned
REWORK. No code exists. Implementation (WP-7) and its proof (WP-8) are held pending the operator's
go/no-go decision. Written to be checked against the tree before any code, per `AGENTS.md:11-12` and
`AGENTS.md:15` (`AGENTS.md`, `PROJECT_STATUS.md` and `docs/JARVIS_MASTER_ROADMAP.md` are in the
canonical `jarvis-local-public-root` repository; every other path below is relative to this
worktree).

Every claim about current behaviour carries a `file:line` verified in this tree on 2026-09-04. Claims
verified by executing the coordinator against a throwaway database are marked **[probe]**. Section 15 is the
change register for the review that produced this revision.

## 1. Purpose, non-goals, and the two increments

**Purpose.** Advance an already-registered plan one stage at a time, unattended, through the existing
coordinator API and the existing approval and policy gateways — closing the roadmap work item "Finish
TaskContract continuation across research, creation, connectors, and multi-stage work"
(`docs/JARVIS_MASTER_ROADMAP.md:616`).

**Two increments (boss ruling).** The mutation protocol carries materially more risk than the stage loop, so
they ship separately.

**Increment 1 (this design, fully specified)** covers non-mutating stages: claim, reserve, adapter,
checkpoint, resume after a kill; it refuses any stage with `mutation_kind != "none"`, and WP-7 may
implement on this document. **Increment 2 (draft only, section 13)** is the five-step mutation protocol
and signed reconciliation — `reversible` first, `irreversible` later still — and is **not approved**;
it needs its own design review before any code.

Sections 2–12 are the acceptance surface for WP-7; section 13 records what the review established about the
mutation protocol so the work is not lost, but nothing in it is authorised.

**Non-goals.** Phase 5 states three times that no executor ships, and each constrains this design
rather than being reversed by it: `docs/LONG_HORIZON_WORKFLOWS.md:8-9` (no automatic executor, no new
tools, no background authority, no model-authored code); `:36-39` (deliberately no `workflow run`);
`:80-82` (no arbitrary Python callback, "because in-process callback code could perform unmetered work
and lie about usage"). Two become false when increment 1 lands, so **WP-7 amends both**: `:36-39` (the
command) and `:10-11` ("must derive real usage itself" — the executor pre-charges, section 5).

Accordingly: no new tool, no new approval action, no new capability, no reachable resource the interactive
agent cannot already reach; no arbitrary callback surface (3.3); no relaxation of approval, redaction,
policy, verification or identity boundaries (`docs/JARVIS_MASTER_ROADMAP.md:603`, `AGENTS.md:24-29`); no CLI
of its own in WP-7 (6.5).

## 2. Where the executor sits

**New module** `jarvis/long_horizon_executor.py`. A new file is on neither reseal pin list and cannot
move an existing digest. The sealed Phase 5 evaluation pins a fixed, enumerated module list
(`jarvis/long_horizon_eval.py:100-127`) naming `long_horizon.py` and `long_horizon_eval_worker.py`
but nothing else in this area, compared against the sealed fixture at
`jarvis/long_horizon_eval.py:1102-1103`. A module absent from that list leaves the pin untouched.
**The list must not be extended** to include the executor; that would invalidate the sealed fixture.

**Not constructible from the public process.** `jarvis.long_horizon` is already on the private deny list
(`tests/test_public_process_isolation.py:45`), and the executor imports it while its adapters import
`jarvis.model_client`, so a public-process import fails transitively. That list has no completeness
assertion (`:19-51`), so WP-7 hands WP-12 the note to add `jarvis.long_horizon_executor` rather than editing
that shared file. Construction also needs two things the public process lacks: a `Memory` handle and the
sidecar integrity key (`jarvis/long_horizon.py:645-685`).

**The CLI stays read-only.** `jarvis/cli.py:900-909` builds the store with **no** `approval_validator`
and **no** `authorities`, and `_approval_locked` raises when the validator is absent
(`jarvis/long_horizon.py:733-735`), so today's CLI store cannot record an intent, authorize an effect,
or consume a permit; the parser offers status/list/show/start/pause/resume/cancel only
(`jarvis/cli.py:3674-3725`). Increment 1 changes neither fact and supplies **no** validator either:
with no mutating stage, none of the three call sites (`:1155-1159`, `:1187-1191`, `:1224-1228`) is
reachable.

## 3. Increment 1 — construction, identity, adapters, tool authority

### 3.1 Identity

`worker_id` binds the stage **lease** (`jarvis/long_horizon.py:975-982`, checked `:1999-2012`) and may be
per-process — it defaults to a random token (`:609`). `executor_id` binds the checkpoint executor field
(`:1069`) and **must be stable across restarts**.

Stability is load-bearing in increment 2 (`jarvis/long_horizon.py:1050-1059` binds the checkpoint executor
to the latest intent actor) and is adopted now so the two agree. It is derived from the plan as
`executor:lh:<first 16 hex of manifest_sha256>` — bounded non-secret metadata accepted by
`_require_identity` (`:157-161`), reproducible after restart, and never a verifier id, since completion
requires a verifier distinct from every stage executor (`:1421-1431`, `:1853-1856`).

### 3.2 Stage filter

Increment 1 refuses any claim reporting `mutation_kind != "none"` (`jarvis/long_horizon.py:2332`) and halts
`mutation_not_supported` **before** reserving usage, so a plan whose stage 3 mutates runs stages 1–2 and
stops with nothing left claimed beyond the lease.

### 3.3 Adapters are a closed table, and they get no tool authority

The closed manifest has exactly eleven fields (`jarvis/long_horizon.py:327-333`) and each stage five
(`:254-258`): `stage_id`, `ordinal`, `stage_type`, `mutation_kind`, `budget`. **No manifest field can name
code**, and `stage_id` is bounded metadata (`:229-230`), not a selector. Adapter selection is made by the
executor from `(stage_type, mutation_kind)` against a module-local table over the closed stage vocabulary
(`:28-31`). Unmapped combinations fail closed. No caller-supplied callable is accepted, satisfying
`docs/LONG_HORIZON_WORKFLOWS.md:80-82`.

**Tool authority is off, and the mechanism matters (boss ruling Q4).** `allow_write`,
`allow_execution`, `allow_memory_write` and `allow_external_mutation` must all be off. They cannot be
passed off through `Agent.run`, which exposes no such parameter (`jarvis/agent.py:11993-12007`;
`allow_companion_control` at `:12004` is unrelated) — they are derived inside the run from a regex
over the prompt (`jarvis/agent.py:13080-13112`). Therefore **an increment-1 adapter does not use the
agent tool loop at all.** A model-backed adapter calls `ModelClient.chat(messages, tools, model, ...)`
(`jarvis/model_client.py:3961` class, `:4218-4224` signature) with `tools=[]`, so no tool schema is
offered and the four gates at `jarvis/agent.py:8028-8035` are moot rather than merely satisfied. All
file effects are performed by deterministic executor code confined to the runner-owned workspace; an
`implement` stage's model output is text that the executor writes, never a tool call the model makes.

**Bypassing `Agent.run` also bypasses two things it did for us (NEW-1).** Redaction and model-call
accounting live in `Agent`, not `ModelClient`: `jarvis/agent.py:71` imports `redact_secrets`,
`contains_secret` and `contains_private_identifier`; `_safe_text` redacts at `:3248`; and
`_record_model_call` writes durable telemetry via `memory.record_model_call(...)` with a
`budget_scope` at `:8885-8915`. `jarvis/model_client.py` contains **zero** references to either, so
the adapter carries both, and neither is optional:

every prompt is passed through `jarvis.redaction.redact_secrets` (`jarvis/redaction.py:221`) before it
reaches a provider, and the adapter **refuses the stage** when `contains_secret` (`:268`) or
`contains_private_identifier` (`:252`) flags the material, halting `stage_failed` / `unsafe_prompt`; and
every call is recorded via `Memory.record_model_call` with a plan-derived `budget_scope`
(`workflow:<plan_id>`), so unattended stages appear in the same accounting as foreground turns.

The cloud gate needs no duplication: `JARVIS_CLOUD_ENABLED` is enforced **ModelClient-side** in
`build_model_client` (`jarvis/model_client.py:4359`, gate at `:4372`, applied to every cloud key at
`:4373-4428`), not in `Agent`. The adapter's obligation is narrower but strict: **obtain its client only
from `build_model_client(config)`**, never by constructing a provider client directly. Note the gate reads
`getattr(config, "cloud_enabled", True)`: it **defaults to enabled** when the attribute is absent, so a stub
or duck-typed config silently re-enables cloud providers. WP-7 therefore constructs the client from the real
`Config`, with a test that a config lacking `cloud_enabled` is refused (11, test 17).

**Adapter table (NEW-2).** `_validate_stages` requires the last stage to be `verify` or `finalize`
(`jarvis/long_horizon.py:375-376`), so an incomplete table would halt every legal plan. All eight
closed stage types (`:28-30`) are mapped, and all are `mutation_kind: "none"` in increment 1:

| Stage type | Adapter | Kind |
|---|---|---|
| `inspect` | read declared workspace inputs, emit a bounded structured summary | model-backed |
| `plan` | order the remaining stage keys into a bounded checklist | deterministic |
| `research` | summarise workspace inputs only; no network, no tools | model-backed |
| `implement` | apply a declared transform and write the artifact | **deterministic** |
| `mutate` | none | refused by 3.2 |
| `verify` | recompute the artifact digest and check declared assertions | deterministic |
| `reconcile` | re-read prior stage outcomes and emit a consistency verdict | deterministic |
| `finalize` | emit the terminal outcome record | deterministic |

`implement` is deterministic on purpose: it writes the artifact, and a model-authored artifact would not be
reproducible (digest note below).

**Model and messages.** The model is the plan-scoped default resolved once per run through
`build_model_client(config)`, never read from the manifest (which has no such field,
`jarvis/long_horizon.py:327-333`) and never chosen by a model. `messages` come from three bounded sources
only: a fixed per-adapter system string, the claim payload's stage metadata, and declared workspace inputs
read by the executor — no conversation history, memory recall, or prior stage prose.

**Digests.** `artifact_sha256` is the SHA-256 of the artifact file's exact bytes as written; `outcome_sha256`
is the SHA-256 of a canonical JSON object of the stage's structured result (stage key, ordinal, adapter name,
declared assertions and their booleans) — never of model prose. **Digest
stability across restart holds only for deterministic adapters.** A model-backed stage that is re-run
after a kill may legitimately produce a different `outcome_sha256`; restart safety means each stage
checkpoints exactly once, **not** that a re-run stage reproduces its predecessor's digest. Artifacts
are written only by `implement`, which is deterministic, so artifact digests are stable.

**Runner-owned workspace.** One directory per plan, created by the executor at first claim under the
configured `JARVIS_WORKSPACE` root as `long_horizon/<plan_id>/` — never the repository and never the
operator's general workspace root. It is durable, not temporary: it survives process exit so a resumed
stage sees earlier artifacts, and only the operator removes it. Adapters read and write only beneath
it; a path outside is refused (11, test 12).

An adapter receives only: plan id, stage id, stage key, ordinal, stage type, `idempotency_key`, the reserved
per-attempt share, a wall-clock deadline, and the workspace path. It returns `outcome_sha256`,
`artifact_sha256` and metered actuals.

### 3.4 Signing keys stay out of process

Verifier and reconciler private keys "belong in separate minimal processes and must not be passed to an
executor" (`docs/LONG_HORIZON_THREAT_MODEL.md:56-58`); the sealed evaluation counts
`executor_authority_secret_leaks` (`jarvis/long_horizon_eval.py:1160-1162`) and the packaged worker pops the
secret from the environment (`jarvis/long_horizon_eval_worker.py:279-283`). The executor holds no private
key and calls neither `reconcile_mutation` nor `record_final_verification`.

## 4. Increment 1 — stage lifecycle and resume

`claim_next_stage(plan_id, worker_id=..., lease_seconds=L)` (`jarvis/long_horizon.py:927-986`) → check the
stage filter (3.2) → `reserve_stage_usage` with the per-attempt share (`:1107-1136`) → run the adapter under
the reserved deadline → `record_checkpoint` with the **identical** usage mapping (`:1014-1105`). `None` from
the claim is a halt condition, not an error. The checkpoint requires the stage ordinal to equal the plan's
`next_stage_ordinal` (`:1044-1045`), so out-of-order completion is impossible and the executor keeps no
cursor of its own.

**Resume is the same call again.** The executor keeps **no** durable state: the cursor
(`next_stage_ordinal`, advanced inside the checkpoint transaction, `jarvis/long_horizon.py:1099-1104`),
stage status, `attempt_count` and the reservation binding all live in the coordinator. Expired leases
resolve inside `claim_next_stage`: for a non-mutating stage the ambiguity branch at `:954-957` cannot fire,
so a retry is consumed and the stage returns to `pending` (`:964-971`). The executor never repairs stage
state by hand, writes no resume hint, and caches no lease token across processes.

**Only two crash windows exist in increment 1.** A kill before the checkpoint leaves a `claimed`
stage whose lease lapses, costing one retry and rerunning the stage from scratch with no external
effect. A kill after the checkpoint is clean: receipt, stage row and plan cursor are written in one
transaction (`jarvis/long_horizon.py:1079-1104`, under `_transaction` at `:752-778`), so there is no
torn state and the executor simply claims the next stage. The five-state crash table belongs to
increment 2 (13.3).

## 5. Increment 1 — usage is a pre-charged bound, not a measurement

### 5.1 Why measurement is impossible here

`reserve_stage_usage` charges plan and stage counters **before** any adapter runs — docstring at
`jarvis/long_horizon.py:1116`, counters incremented at `:2088-2109`. `record_checkpoint` then requires the
checkpoint usage to equal the reservation **exactly** (`:1039-1043`). Re-reserving a different value for the
same attempt is an integrity failure, not a correction (`:1123-1130`, reason `usage_replay`). Counters are
monotonic — only `used_* = used_* + ?` updates exist, with no decrement or reset, and integrity recomputes
each counter as the sum over durable reservation receipts (`:1606-1637`) — so a measured value can never be
reconciled into a reservation. Independent metering is unreliable anyway: provider token counts may be
`None` (5.4).

**Therefore:** reserve a value derived only from the declared `WorkflowStageSpec.budget`; checkpoint
that same value; meter actuals independently for reporting; fail the stage closed on over-run; never
re-reserve; never reconcile a measured value into a reservation.

### 5.2 The reserved value (boss ruling Q2)

Reserving the whole declared budget, as the plan of record says literally, makes retries impossible:
`_check_usage_locked` compares `stage.used_* + usage` against the stage budget
(`jarvis/long_horizon.py:2024-2036`) and `used_*` accumulates across attempts with no reset.

**[probe]** With `elapsed_seconds=60, retries=3`, reserving the full 60 on attempt 1 succeeds; after
lease expiry the stage is re-claimed as attempt 2 and every further reservation is refused with
`LongHorizonBudgetError: stage elapsed_seconds budget would be exceeded` — including a quarter-sized
one.

The adopted rule, derived only from the spec and never from a measurement:

- **Divisor** `d = min(stage.budget.retries, 3) + 1`, so a generous `retries` cannot shrink each
  attempt to uselessness; `retries` is already in the claim payload's budget mapping
  (`jarvis/long_horizon.py:2340` via `:197-198`), needing no new store method.
- **Ordinary attempt** reserves `share[key] = stage.budget[key] // d` for the five `USAGE_KEYS`
  (`:52`); the **final permitted attempt** (`attempt_count == d`) reserves
  `stage.budget[key] - stage.used_*[key]` from `export_evidence`'s per-stage usage (`:2407`), so
  integer division strands nothing.
- **Admission** reports the effective share beside the declared budget, and refuses the stage with
  `stage_budget_indivisible` when the share is `0` for any key whose declared budget is non-zero —
  a refusal before claiming, not a mid-run failure.

**[probe]** With `retries=3` the share `{elapsed_seconds: 15, tool_calls: 2, model_calls: 2, prompt_tokens:
250, completion_tokens: 250}` was accepted on all four attempts, and `record_checkpoint` accepted the
identical mapping on the final attempt.

**Operator sizing rule, for WP-7 to add to `docs/LONG_HORIZON_WORKFLOWS.md`:** a stage budget must be at
least `min(retries, 3) + 1` in every non-zero unit, because the executor divides the declared budget across
attempts; a stage failing that is refused at admission rather than run once and stranded.

### 5.3 The zero-usage invariant

`_validated_usage` accepts `0` for every key — the check is `value < 0` (`jarvis/long_horizon.py:2014-2022`,
condition at `:2019-2020`) — so an all-zero reservation is legal and would let any stage checkpoint having
reserved nothing. **Invariant: an all-zero mapping is legal only when the claim payload reports
`mutation_state == "reconciled_applied"`** (`:2339`), impossible in increment 1; test 6 asserts it.

### 5.4 Metering, and over-run

Observed consumption is recorded separately from the reservation (NEW-3): wall-clock seconds measured by
the executor, and the token counts on the provider's own reply. `ChatResponse.metrics`
(`jarvis/ollama_client.py:172-196`, built at `:189-196`) exposes `prompt_tokens` and `completion_tokens`,
either of which may be `None`. `Agent`'s metrics dict (`jarvis/agent.py:6393-6400`) is **not** available to
increment 1, which no longer runs that path. These never enter a reservation or checkpoint; they appear in
the run report and audit rows and drive exactly one decision — the over-run check.

**Telemetry is not enforcement.** `Memory.record_model_call` (`jarvis/memory_projects_budget.py:277-289`)
only records a call that already happened; the enforcing reservation path is the separate
`reserve_model_call`, which counts `model_call_budget_events` and raises `ModelBudgetExceeded` before
dispatch (`:192-244`). Increment 1 does not rely on it: **the enforcing bound on model calls is the
pre-charged `model_calls` key in the stage reservation** (5.2), checked by `_check_usage_locked` against the
stage and plan budgets before any adapter runs (`jarvis/long_horizon.py:2024-2036`). NEW-1's accounting
requirement is therefore about visibility and cost attribution, not about capping the stage.

If any metered actual exceeds the reserved share, or the wall-clock deadline passes, the executor aborts the
adapter, does **not** call `record_checkpoint` (which would be a lie about usage), writes a refusal audit
row, and halts with `stage_failed` / `usage_overrun`. The lease then lapses; the next claim consumes a retry
(`jarvis/long_horizon.py:964-971`); when retries are exhausted `_consume_retry_locked` durably sets the plan
to `failed` before raising (`:1955-1960`), a terminal state operator controls cannot promote back to active
(`docs/LONG_HORIZON_WORKFLOWS.md:92-93`). Repeated over-run therefore terminates the plan rather than
looping. Increment 1 does **not** pause on over-run; pausing is an increment-2 rule with its own costs
(13.6).

## 6. Increment 1 — `run(max_stages=...)` and the halt vocabulary

**Shape.** `run_stages(memory, *, project_id, plan_id, max_stages, executor_id=None,
lease_seconds=None, adapters=None) -> StageRunReport` — module-level, tested in WP-7. `max_stages` is
required, `1 <= max_stages <= MAX_STAGES` (64, `jarvis/long_horizon.py:42`). There is no unbounded
mode, no scheduler, no service, no auto-start.

**Halt conditions — a closed vocabulary.** Every line reference in this table is
`jarvis/long_horizon.py`. The loop stops at the first of:

| Reason | Trigger |
|---|---|
| `plan_complete` | `claim_next_stage` returned `None` and every stage is `complete` |
| `max_stages_reached` | the requested number of stages checkpointed |
| `mutation_not_supported` | the claimed stage declares `mutation_kind != "none"` (3.2) |
| `stage_budget_indivisible` | admission refusal per 5.2 |
| `stage_unavailable` | `claim_next_stage` returned `None` with a live foreign lease (`jarvis/long_horizon.py:950-953`) or lost the atomic claim race (`:983-984`) |
| `plan_not_runnable` | `LongHorizonStateError` from `_require_runnable_locked` — plan not active, or global runtime control not `running` (`:1922-1926`) |
| `budget_exhausted` | `LongHorizonBudgetError` — elapsed (`:1927-1928`), reservation (`:2024-2036`) or retries (`:1955-1960`) |
| `integrity_failure` | any `LongHorizonIntegrityError`; stop immediately, never retry |
| `stage_failed` | adapter refusal or usage over-run |
| `lease_lost` | `_claimed_stage_locked` reports the lease expired or changed owner (`:2006-2011`) |

`stage_unavailable` is distinct from `plan_complete` on purpose: both arise from a `None` claim, but
conflating "finished" with "another worker holds it" would report success for work that never ran. Every
reason is terminal for the call; resuming is a fresh call, with no retry loop or sleep-and-poll.

**Lease arithmetic.** `lease_seconds` defaults to `min(share["elapsed_seconds"], MAX_LEASE_SECONDS)`
(`jarvis/long_horizon.py:43`, bound checked at `:936-938`), and the reserved elapsed share is a hard
wall-clock deadline. A stage longer than one lease may renew (`:988-1012`) but only **within** that share;
renewing past it would make the reservation a fiction. The 30-second-permit interaction is increment-2 (13.5).

**CLI.** WP-7 ships the API and its tests only. A later `jarvis workflow run --max-stages N` would be
a thin shim — parse, call, print the prompt-free report — and lands with the
`docs/LONG_HORIZON_WORKFLOWS.md` amendments named in section 1.

## 7. Budgets and fail-closed behaviour

Four limits already exist and the executor adds none: the per-stage reservation, refused before any work
(`jarvis/long_horizon.py:2024-2036`); plan totals, checked against the manifest budget and re-checked on
read (`:1931-1937`); elapsed, checked on every runnable transition (`:1927-1928`); and retries, whose
exhaustion writes terminal `failed` state *before* raising, which the transaction helper deliberately
commits (`:766-772`).

Fail-closed rules: an unmapped `(stage_type, mutation_kind)` is refused and the stage does not run; any
`LongHorizonIntegrityError` halts and is never retried, matching the helper's refusal to re-MAC an
integrity-failed row (`:760-765`); global pause/stop dominates, since `_require_runnable_locked` reads
`runtime_control` on every transition (`:1922-1924`; written by `jarvis/memory_operator_state.py:48-57`); a
clock behind the durable floor is an integrity failure, not a warning (`:1917-1920`).

## 8. What is logged, and what is never logged

**Logged**, reusing the existing audit shape. The toolbox already writes one `activity_log` row per tool
call carrying only names and digests — `argument_names`, `duration_ms`, `handler_dispatched`, `trace_id`,
`approval_id`, `target_sha256`, `result_receipt_id`, `matched_constraint_sha256` — never values
(`jarvis/tools.py:2355-2391`). The executor writes one row per stage transition in the same style:
plan/stage/ordinal/stage type, attempt number, halt reason, adapter name, the `idempotency_key` and the
outcome, artifact, reservation and checkpoint digests, plus declared budget, effective share and metered
actuals as integers.

**Never logged, in any field, at any level:** prompts, model output, adapter input or output text, tool
arguments or results, file contents, credentials, tokens, API keys, the sidecar integrity key or its path,
any authority private key, absolute local paths, hostnames, or user identifiers. The run report is
prompt-free by construction so it can be pasted into evidence without a scrubbing step
(`docs/EVALUATION.md:48-49`; the CLI's `_workflow_safe_nested` filter at `jarvis/cli.py:1011` is the
precedent). Losing an audit row must not turn a completed side effect into a retryable failure
(`jarvis/tools.py:2392-2395`): audit failures are logged at error level and never roll back a receipt.

## 9. Does `jarvis/long_horizon.py` have to change?

**No.** Increment 1 uses `claim_next_stage` (`jarvis/long_horizon.py:927`), `reserve_stage_usage`
(`:1107`), `renew_stage_lease` (`:988`), `record_checkpoint` (`:1014`), `show_plan` (`:921`) and
`export_evidence` (`:1491`). Three facts that would otherwise force a change do not: the per-attempt share, the stage filter and the
final-attempt remainder are all readable from the claim payload or `export_evidence` (5.2, 3.2).

`jarvis/long_horizon.py` is on the reseal list, so WP-7 must confirm it is byte-identical to the WP-0
manifest at hand-off. If a reviewer finds a transition that genuinely cannot be expressed, WP-7 becomes the
exclusive owner of that pinned file and carries the reseal duty — a change of scope, not a detail.

## 10. What runs unapproved, and the security argument

### 10.1 The approval surface is narrower than it looks

`SENSITIVE_ACTIONS` is the complete set of tools requiring an approval decision, and it is a **named
allow-list of 24 entries** (`jarvis/approvals.py:12-35`), not a category rule. Many consequential tools are
absent and gated only by the four prompt-derived `allow_*` flags (`jarvis/agent.py:13080-13112`, enforced at
`:8028-8035`):

| Tool set | Members not in `SENSITIVE_ACTIONS` | Sole gate |
|---|---|---|
| `FILE_WRITE_TOOLS` (`jarvis/tools.py:112-117`) | `write_file`, `edit_file`, `make_directory`, `copy_path`, `move_path`, `trash_path`, `tool_create`, `build_document_preview`, `generate_image`, `edit_attached_image`, `build_document` (spliced in from `DOCUMENT_WRITE_TOOLS`, `jarvis/tools.py:111`, at `:116`) | `allow_write` |
| `EXECUTION_TOOLS` (`jarvis/tools.py:121-127`), incl. `PROCESS_LIFECYCLE_TOOLS` (`:118-120`) | `run_process`, `launch_artifact`, `start_process`, `stop_process`, `process_status`, `process_logs`, `http_health` | `allow_execution` |
| memory write | `remember` | `allow_memory_write` |

Only `computer_write_file` (`jarvis/approvals.py:16`) — writing *outside* the workspace — is approval-gated
among file writes. Workspace writes and local process execution run unapproved whenever the regex says the
prompt asked for them. **This is why ruling Q4 turns all four flags off and why increment 1 does not use the
agent tool loop at all (3.3):** relying on those flags would put a regex over adapter-supplied text in the
security path.

### 10.2 The argument

**No new authority.** With `tools=[]` the adapter offers no tool schema, so increment 1's model calls can
perform *no* tool effect — strictly less than a foreground turn, not merely equal to one. Every file effect
is deterministic executor code inside the runner-owned workspace. The executor defines no tool, no approval
action and no capability, and supplies no approval validator, so the coordinator's three mutation call sites
(`jarvis/long_horizon.py:1155-1159`, `:1187-1191`, `:1224-1228`) are unreachable.

**Redaction and accounting are carried, not dropped.** Leaving the agent loop removes two protections
`Agent` supplied incidentally — prompt redaction and durable model-call accounting (3.3) — so the adapter
performs both and refuses the stage on `contains_secret` / `contains_private_identifier`; test 16 catches an
adapter that skips either.

**No weakening.** Nothing here touches approval, redaction, policy, verification or identity code
(`docs/JARVIS_MASTER_ROADMAP.md:603`, `AGENTS.md:24-29`). WP-7 owns only new files plus
`docs/LONG_HORIZON_WORKFLOWS.md`, and must not also change an approval path.

**No model authority.** Adapter selection is a closed table over the closed stage vocabulary
(`jarvis/long_horizon.py:28-31`) and the manifest has no field that can name code (`:327-333`,
`:254-258`), so model output stays advisory (`AGENTS.md:22-23`): a model cannot choose what runs,
alter a budget, or report its own usage into a receipt (5.1).

**The residual increment, plainly.** The genuinely new thing is that stages advance without a human at the
keyboard for each one, bounded by an operator-registered manifest, a pre-charged budget that cannot rise at
run time, a retry ceiling that terminates the plan, a hard halt on any ambiguity, and a global stop that
dominates every transition (`jarvis/long_horizon.py:1922-1924`). It is still an increment — which is why it
is the operator's decision.

## 11. WP-7 test plan (increment 1)

`tests/test_long_horizon_executor.py`. No sealed fixture touched; each guarantee carries a negative test
that fails if it is removed.

**Unit.** 1 `test_stage_lifecycle_call_order` — a recording fake store asserts the section 4 sequence,
including that the adapter never runs before the reservation. 2 `test_mutating_stage_is_refused` — a
`mutation_kind != "none"` claim halts `mutation_not_supported` with no reservation written. 3
`test_full_budget_reservation_blocks_retry` — pins the 5.2 coordinator behaviour so a future change is
caught. 4 `test_capped_share_survives_every_permitted_attempt` — with `retries` of 0, 1, 3 and 9 the
divisor is `min(retries, 3) + 1` and every permitted attempt reserves successfully. 5
`test_final_attempt_reserves_the_remainder` — the last attempt's reservation equals declared minus
`export_evidence` per-stage usage, and the checkpoint accepts it. 6
`test_zero_usage_reservation_is_refused_on_a_non_reconciled_stage` — the 5.3 invariant; the executor
refuses rather than reserving nothing. 7 `test_indivisible_stage_budget_is_refused_at_admission` — the
refusal precedes the claim. 8 `test_effective_share_is_reported_at_admission` — declared budget and
effective share both appear in the report. 9 `test_over_run_fails_closed` — no checkpoint, no second
reservation, halt `usage_overrun`. 10 `test_executor_id_is_stable_across_processes` — pure function of
the manifest digest. 11 `test_adapter_receives_no_tool_schema` — the model call is made with
`tools=[]`; a non-empty list is a test failure. 12 `test_adapter_writes_stay_in_the_runner_workspace` —
a path outside it is refused. 16 `test_no_model_call_without_redaction_and_accounting` — a fake
`ModelClient` asserts every `chat` was preceded by `redact_secrets` and followed by
`Memory.record_model_call` with a `workflow:<plan_id>` scope, and that a prompt tripping
`contains_secret` or `contains_private_identifier` halts `unsafe_prompt` with no call at all. 17
`test_client_comes_from_build_model_client` — a directly constructed provider client, and a config lacking
`cloud_enabled`, are both refused, so the defaulted gate at `jarvis/model_client.py:4372` cannot be routed
around. 18
`test_max_stages_bounds_the_loop` and `test_halt_reasons_are_closed`.
19 `test_stage_unavailable_is_not_plan_complete` — a live foreign lease yields `stage_unavailable`. 20
`test_report_contains_no_free_text` — report and audit rows are digests, integers and closed enums.

**Kill mid-run**, in real child processes following `jarvis/long_horizon_eval.py:980-1052`. 21
`test_kill_before_checkpoint_reruns_the_stage` — one retry consumed, no artifact double-write, the
stage completes on resume. 22 `test_kill_after_checkpoint_advances_cursor` — resume claims the next
stage; chain and `next_stage_ordinal` agree. 23 `test_kill_does_not_double_charge_usage` — plan
`used_*` equals the sum of reservation receipts after every restart, the invariant at
`jarvis/long_horizon.py:1633-1637`. 24 `test_kill_during_reservation_leaves_no_orphan_charge` — either
the reservation and its counters both exist or neither does.

**Regression gate, executed by the reviewer.** 25 `python -m unittest -v tests.test_long_horizon_eval`
unchanged: 24 workflows, 10 negative controls, the same metrics (`jarvis/long_horizon_eval.py:69-80`,
`:1167-1173`). 26 `jarvis/long_horizon.py`, `jarvis/long_horizon_eval*.py` and both sealed fixtures
byte-identical to the WP-0 manifest.

## 12. WP-8 proof, without touching the sealed fixture

**Separation.** New evaluator `jarvis/long_horizon_workload_eval.py`; the sealed evaluator's module list is
**not** extended, for the reason given in section 2.

**Do not pin the executor into the new sealed fixture either.** Binding `long_horizon_executor.py`'s
digest into the fixture's own `runtime_sha256` would make every later executor edit require a fixture
reseal — exactly the pressure that corrupts a holdout. The fixture seals **workload content only**
(workflows, stages, restart points, expected outcomes) and the evaluator records the observed runtime
digest as **report evidence** compared against nothing. A stronger binding needs a written boss-only
reseal protocol naming who may reseal and on what evidence; absent it, report-only is the rule.

**Fixture.** `tests/fixtures/long_horizon_workload_v1.json`, authored by an agent that has not read the
implementation and sealed by the boss with a self-excluding manifest digest computed the way the Phase 5
fixture does (`jarvis/long_horizon_eval.py:325-327`): at least 20 workflows of at least 5 stages each
(`_validate_stages` requires 5 to 64 ordered stages ending in `verify`/`finalize`,
`jarvis/long_horizon.py:368-376`) across at least five families, with randomized restart points.

**Real work, increment-1 shaped.** Each workflow reads, transforms, writes an artifact and verifies it in
the runner-owned workspace, the artifact written by deterministic executor code. Restart is a real process
exit (`os._exit`), as in the sealed worker (`jarvis/long_horizon_eval_worker.py:238`, `:246`, `:250`), not an
exception, and state is re-read in a fresh process before counting. Restart evidence is **model free**, and
the report names any stage that used a model.

**Reported.** Workflows completed after restart; usage double-charges (0, by re-deriving plan `used_*` from
reservation receipts); checkpoint chain integrity; artifact digest stability for the deterministic
`implement` stage (3.3); per-restart-point breakdown. **The zero-duplicate-irreversible-effect number
belongs to increment 2** and is not claimed here: increment 1 performs no external effect, so this proves
restart safety of the stage loop, not exactly-once delivery. Gate 6 is already passed by the sealed holdout.

**WP-8's stated acceptance cannot be met by increment 1 (NEW-4).** The Phase 2 plan of record requires
of WP-8 "20/20 complete after restart with 0 duplicate irreversible effects, read from the reopened
ledger in a fresh process" plus forged-reconciliation, replayed-permit and downgraded-applied controls
(plan §WP-8, *Acceptance*, lines 691-693). Increment 1 performs no external effect and runs no
mutation protocol, so none of those five numbers is producible. **Boss ruling: WP-8 stays held with
increment 2.** A restart-safety-only proof for increment 1 may be proposed separately as **WP-8a**
with the reduced report above; it must not reuse WP-8's acceptance wording.

**Honest limits, in every artifact.** `docs/LONG_HORIZON_WORKFLOWS.md:79-80` already records that Phase 5
ships the protocol and a deterministic evaluation, "not an adapter that performs real external mutations",
and `docs/LONG_HORIZON_THREAT_MODEL.md:60-66` lists what stays out of scope. WP-8a removes neither.

## 13. Increment 2 — mutation protocol (DRAFT, not approved)

**Nothing here is authorised**; increment 2 needs its own design document and adversarial review.
Unqualified `:NNN` references below are `jarvis/long_horizon.py`.

**13.1 Approval is one-shot per stage attempt, not per protocol phase.** `Memory.authorize_or_request`
**consumes** the approved row on first success, flipping it to `status='consumed'` with an audit entry
(`jarvis/memory_approvals.py:274-281`), so one call per protocol phase would burn the grant at intent and
leave authorization and permit consumption unapproved. **Rule: exactly one `authorize_or_request` per stage
attempt**; the validator caches that decision and serves all three coordinator phases (`:1155-1159`,
`:1187-1191`, `:1224-1228`) from it, returning a `receipt_sha256` bound to the grant id so the three
receipts are provably one decision. A test must assert one call per attempt and identical receipts.

**13.2 Scope `task:<task_id>` is mandatory, and its cost is real.** `persistent_approval_eligible` returns
`False` whenever `task_id` is set (`jarvis/memory_approvals.py:74-83`), so no standing or session grant is
reachable and every stage attempt needs a **fresh operator decision** — the intended property, since
unattended execution must not inherit a standing grant, but a cost to state plainly. `foreground` is wrong
because no operator is present.

**13.3 A denial after the intent receipt is not "approve and re-run".** It leaves the stage in
`intent_recorded` or `effect_authorized`, both routed to `awaiting_reconciliation` by `claim_next_stage`
(`:954-957`). So `approval_required` is recoverable **only before the intent receipt exists**; after it the
halt is `needs_reconciliation` and recovery needs a signed authority. The executor must report which.

**13.4 Crash windows: five states, not three.** `record_mutation_result(outcome="applied")` leaves the
stage `claimed` — only the `uncertain` and `not_applied` branches touch status (`:2187-2203`) — and
`result_applied` is in the ambiguous set at `:956`.

| State at crash | On restart | Recovery |
|---|---|---|
| `none` | ordinary lease expiry | retry consumed, back to `pending` (`:964-971`); no effect happened |
| `intent_recorded` | ambiguous (`:954-957`) | `awaiting_reconciliation`; signed authority required |
| `effect_authorized` | ambiguous | as above; the permit may or may not have been consumed |
| `effect_in_progress` | ambiguous | as above; the effect may have landed |
| `result_applied` | ambiguous | as above, though the effect certainly landed and only a checkpoint is missing |

**Honest limit, corrected:** *a crash anywhere after the intent receipt halts for a signed reconciliation*
— not merely one inside the effect window. Increment 2 needs kill tests at all five states.

**13.5 Result branching, permits, leases.** Branch on the adapter's `effect_outcome`: `applied` →
`record_mutation_result` then checkpoint; `not_applied` → the store drops the lease, returns the stage to
`pending` and consumes a retry (`:2193-2203`), so the executor halts `effect_not_applied` rather than
looping; `uncertain` → `awaiting_reconciliation` (`:2187-2192`), halt `needs_reconciliation`. The permit
lasts 30 seconds (`:1195`) and a lease covering an authorized mutation cannot be extended (`:1004-1005`),
while increment 1's default lease can be shorter — 15 seconds in the 5.2 example — and would expire
mid-effect. **Rule: a mutating stage requires `lease_seconds >= 30 + slack`,** with a test asserting
`authorization_expires_at <= lease_expires_at`.

**13.6 Residual trust, and pause-on-over-run.** A destination rejecting a replayed `effect_key` as a
duplicate maps to **`applied`**, not to an error. The residual trust is the reconciler:
applied-monotonicity protects only `result_applied` and `reconciled_applied` (`:1299-1303`), so a wrong
`not_applied` verdict returns the stage to `pending` and causes a second dispatch — exactly-once rests on
the reconciler's correctness, which no code here can check. Over-run pauses the plan (`pause_plan`,
`:1376-1377`), at two costs: pausing consumes a retry per non-ambiguous claimed stage (`:2292-2301`) and can
itself raise `LongHorizonBudgetError` when retries are exhausted (`:1955-1960`), reported as
`budget_exhausted`; and a claimed stage in `result_applied` moves to `awaiting_reconciliation`
(`:2307-2315`), converting a missing checkpoint into a reconciliation.

## 14. Open questions for the operator

**Q1 — proceed with increment 1?** *Recommendation: yes.* "Multi-stage" continuation has nothing to
continue today, and increment 1 closes that with no external effect, no tool schema and no approval
surface of its own.

**Q2 — commission increment 2 now or later?** *Recommendation: later, after increment 1 has run real
workloads.* Section 13 shows its honest limit is stronger than first described — any crash after the
intent receipt needs a signed authority — so its value depends on an operator willing to run a
reconciler process.

**Q3 — who runs the reconciler, if increment 2 proceeds?** Unanswered, and the precondition for increment 2
being useful rather than merely correct.

## 15. Change register

| # | Severity | Finding | Resolution |
|---|---|---|---|
| Q1/Q3 | ruling | Split into two increments | Document restructured: 2–12 specify increment 1 (non-mutating only, stage filter at 3.2, halt `mutation_not_supported`); 13 is a separated unapproved draft |
| Q2 | ruling | Per-attempt share needs a cap and a remainder | 5.2: divisor `min(retries, 3) + 1`; final attempt reserves declared minus `used_*` via `export_evidence` (`jarvis/long_horizon.py:2407`); effective share reported at admission; `stage_budget_indivisible` is an admission refusal; operator sizing rule added for WP-7 to put in `LONG_HORIZON_WORKFLOWS.md`, which WP-7 amends at `:36-39` and `:10-11` |
| Q4 | ruling | No model-backed adapter on mutating stages; four `allow_*` flags off | 3.3: `Agent.run` has no such parameter (`agent.py:11993-12007`), so increment-1 adapters bypass the tool loop and call `ModelClient.chat` with `tools=[]`; `implement` confined to the runner-owned workspace |
| Q5 | ruling | Scope `task:<task_id>` mandatory | 13.2: mechanism stated (`persistent_approval_eligible` returns `False` when `task_id` is set, `memory_approvals.py:74-83`) and the one-shot cost stated |
| Q6 | ruling | Keep pause-on-over-run, state its costs | 13.6: pause consumes retries (`jarvis/long_horizon.py:2292-2301`) and can raise `LongHorizonBudgetError` reported as `budget_exhausted`; `result_applied` is pushed to `awaiting_reconciliation` (`:2307-2315`). Increment 1 does not pause (5.4) |
| HIGH-1 | high | Three validator calls burn the single approval grant | 13.1: one `authorize_or_request` per stage attempt, cached across all three phases, `receipt_sha256` bound to the grant id, with a required test. Verified: `memory_approvals.py:274-281` flips the row to `consumed` |
| HIGH-2 | high | Denial after intent is not recoverable by re-running | 13.3: `approval_required` only before the intent receipt; `needs_reconciliation` after (`long_horizon.py:954-957`) |
| HIGH-3 | high | Crash table missing three states | 13.4: five-state table; `record_mutation_result(applied)` leaves status `claimed` (`jarvis/long_horizon.py:2187-2203`) and `result_applied` is ambiguous (`:956`); honest limit rewritten to "a crash anywhere after the intent receipt"; increment-2 kill tests at all five states |
| HIGH-4 | high | All-zero reservation would complete any stage | 5.3: invariant that a zero reservation is legal only on `reconciled_applied`; negative test 6; verified `_validated_usage` checks `value < 0` (`jarvis/long_horizon.py:2014-2022`, `:2019-2020`) |
| HIGH-5 | high | `SENSITIVE_ACTIONS` does not gate workspace writes or local execution | 10.1: "what runs unapproved" table from `approvals.py:11-36`, `tools.py:112-127`, gated only by the prompt regex at `agent.py:13080-13112`; 3.3 turns the flags off by removing the tool surface entirely |
| MEDIUM-3 | medium | Default lease shorter than the 30 s permit | 13.5: `lease_seconds >= 30 + slack` for mutating stages, with an `authorization_expires_at <= lease_expires_at` test |
| MEDIUM-4 | medium | No halt reason for a foreign lease or claim race | 6: `stage_unavailable` added (`jarvis/long_horizon.py:950-953`, `:983-984`) and explicitly distinguished from `plan_complete`; test 14 |
| MEDIUM-5 | medium | Section 4 did not branch on the mutation result | 13.5: `applied` → checkpoint; `not_applied` → halt `effect_not_applied` (`jarvis/long_horizon.py:2193-2203`); `uncertain` → `needs_reconciliation` (`:2187-2192`) |
| MEDIUM-6 | medium | Duplicate-key response unmapped; residual trust unstated | 13.6: duplicate maps to `applied`; a wrong `not_applied` from the reconciler causes a second dispatch because monotonicity covers only `result_applied`/`reconciled_applied` (`jarvis/long_horizon.py:1299-1303`) |
| MEDIUM-8 | medium | WP-8 would pin the executor into a sealed fixture | 12: fixture seals workload content only; runtime digest recorded as report evidence; a boss-only reseal protocol is required for anything stronger |
| LOW-2 | low | Guarantees not falsifiable | 11: negative tests 2, 3, 6, 7, 14 and kill tests 16–19; increment-2 test obligations named in 13.1, 13.4 and 13.5 |
| NEW-1 | high | Bypassing `Agent.run` also bypasses redaction and model-call accounting | 3.3, 10.2: verified both live in `Agent` (`jarvis/agent.py:71`, `:3248`; `_record_model_call` at `:8885-8915`) and that `jarvis/model_client.py` has **zero** references to either. The adapter now redacts every prompt with `redact_secrets` (`jarvis/redaction.py:221`), refuses on `contains_secret` (`:268`) / `contains_private_identifier` (`:252`) as `unsafe_prompt`, and records each call via `Memory.record_model_call` with a `workflow:<plan_id>` scope; tests 16-17. The provider boundary needed no duplication: `JARVIS_CLOUD_ENABLED` is enforced **ModelClient-side** in `build_model_client` (`jarvis/model_client.py:4359`, gate `:4372`, applied `:4373-4428`), so the adapter must instead obtain its client only from `build_model_client(config)` |
| NEW-2 | medium | Adapter table incomplete; digests, sources and workspace unspecified | 3.3: all eight closed stage types mapped (`jarvis/long_horizon.py:28-30`), so the `verify`/`finalize` rule at `:375-376` cannot halt a legal plan; model and `messages` sources bounded to three; `artifact_sha256` defined over the artifact bytes and `outcome_sha256` over a canonical structured result, never model prose; **digest stability holds only for deterministic adapters**, reconciled by making `implement` deterministic executor code; runner-owned workspace specified as `long_horizon/<plan_id>/` under `JARVIS_WORKSPACE`, executor-created, durable across restart, operator-removed |
| NEW-3 | low | Metering cited Agent's metrics dict, a path increment 1 no longer uses | 5.4: now cites `ChatResponse.metrics` (`jarvis/ollama_client.py:172-196`, built at `:189-196`) exposing `prompt_tokens`/`completion_tokens`, either possibly `None`, and states that `jarvis/agent.py:6393-6400` is unavailable to increment 1. `jarvis/ollama_client.py` is WP-1-dirty, so a reviewer should re-verify `ChatResponse` by symbol if the lines have moved |
| NEW-4 | medium | WP-8's stated acceptance is unreachable for increment 1 | 12: states that the plan of record's WP-8 *Acceptance* (lines 691-693: 0 duplicate irreversible effects plus forged-reconciliation, replayed-permit and downgraded-applied controls) cannot be produced without the mutation protocol. **Boss ruling recorded: WP-8 stays held with increment 2**; a restart-safety-only proof may be proposed as **WP-8a** and must not reuse WP-8's acceptance wording |
| DATA-1 | data | Two §10.1 figures wrong | 10.1: `SENSITIVE_ACTIONS` is **24** entries, not 25, cited at `jarvis/approvals.py:12-35` (verified by parsing the dict); the `FILE_WRITE_TOOLS` row now includes `build_document`, spliced in via `DOCUMENT_WRITE_TOOLS` (`jarvis/tools.py:111`, at `:116`) and likewise unapproved |
| CIT-1 | citation | `jarvis.long_horizon` deny-list line | Corrected to `tests/test_public_process_isolation.py:45` |
| CIT-2 | citation | Benchmark close/reopen lines | Corrected to `jarvis/task_contract_benchmark.py:686-687` (docstring) and `:749-750` (the call). That file is WP-2-owned and was modified at 09:08 on 2026-09-04; a reviewer should re-verify by symbol if it has moved |
| CIT-3 | citation | Threat-model vs workflows-doc statements conflated | `docs/LONG_HORIZON_THREAT_MODEL.md:60-66` is the out-of-scope list; the simulation statement is `docs/LONG_HORIZON_WORKFLOWS.md:79-80`; the no-callback statement is `:80-82` |
| CIT-4 | citation | Bare `:NNN` references and unnamed repositories | Every citation now carries a filename on first use in its section; section 1 names `jarvis-local-public-root` as the home of `AGENTS.md`, `PROJECT_STATUS.md` and the roadmap |
