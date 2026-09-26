# Phase 2 evidence dossier

All four measurement packages are closed and reviewed. Every figure below is traced to an artifact
under [`evidence/`](evidence/) or to a command re-run against this tree; no number is carried over
from an earlier run or from a plan estimate.

Scope: roadmap Phase 2, *Prove generality, memory quality, and long-horizon recovery*. This records
what was measured, on what provider, and what remains unmet and why. **It is not a claim that Phase 2
is complete:** of the eight exit-gate bullets, three are met, four are unscored, one is the
operator's.

## Base identity

| Field | Value |
|---|---|
| Base | file manifest `6616688bfcc072eefffc2708c5846e53f556d8d597b972465f84e310b8f3f9b1` (hash8 `6616688b`), 364 files |
| Git | branch `claude/roadmap-phase1`, last commit `35d5344`, **working tree uncommitted** |
| Provider / model | Ollama, provider-reported `0.32.15`; `qwen3.5:9b` requested and served |
| Platform | Windows, CPython 3.13.7; live runs 2026-09-05 UTC, deterministic re-verification 2026-09-04 |

Phase 1 and Phase 2 are both uncommitted, so evidence is identified by the base **manifest** hash, not
a commit: a commit id would name a tree that does not contain the code these numbers describe. The
`claude-cli` provider cannot satisfy model attestation by deliberate design
(`jarvis/ollama_client.py:164,177`; `jarvis/model_client.py:2071-2078`, `:3760-3762`), so every live
run used the local attesting provider.

## Evidence index

| Artifact (`docs/evidence/`) | Package | sha256 | Bytes |
|---|---|---|---|
| `phase2_task_contract_resolver_ollama_6616688b.json` | WP-2 | `196d631de9d7219e6c59d212b7f9ad2a52da080ddc0216fd19a53247717ec29f` | 40,387 |
| `phase2_task_contract_outcomes_ollama_6616688b.json` | WP-4 | `0ce9eb83ba5b4281cc50e6a6571e7df509431d068caa5bb24ecccfc25d84114f` | 40,004 |
| `phase2_transfer_lift_ollama_6616688b.json` | WP-9 | `fc83209e7a90640f1321a77ea169f3f8a63e7d1ed216ed07ef855a902c7a4475` | 9,721 |
| `phase2_calibration_ollama_6616688b.json` | WP-10 | `22aafc2639b7e34fb3968bfa120b060311752ea8d8817265c6eb06ad79ceb95f` | 20,419 |

Digests as recorded here. The outcome artifact is being regenerated with explicit derived fields
and no change to any number, so re-verify that one digest before publication.

## Gate status

Roadmap-bullet order. Column two is the row number in the implementation plan's seven-row gate
table; bullet 6 has **no row there**, a plan defect recorded in §10.4.

| # | Plan row | Gate | Threshold | Status |
|---|---|---|---|---|
| 1 | 1 | Broad routing | ≥ 0.90 | **NOT SCORED** |
| 2 | 2 | Verified workflow completion | ≥ 0.85 | **NOT SCORED** |
| 3 | 3 | Material-ambiguity clarification recall | ≥ 0.90 | **NOT SCORED** |
| 4 | 4 | Unnecessary clarification | ≤ 0.10 | **NOT SCORED** |
| 5 | 5 | Memory precision@3 / recall@3 | ≥ 0.85 / ≥ 0.80 | **MET** |
| 6 | *(absent)* | Cross-domain transfer lift | ≥ 15 pp | **NOT MET** (inconclusive) |
| 7 | 6 | 20 workflows × ≥5 stages survive restart | 20 / 0 duplicates | **MET** |
| 8 | 7 | Brier ≤ 0.25, calibration error ≤ 0.15 | every promoted family | **OPERATOR-ONLY** |

"NOT SCORED" is neither a near miss nor a measured failure: the sealed scorer refuses an incomplete case
set (`jarvis/task_contract_eval.py:236-241`), so when any case yields no accepted contract, **no**
routing, ambiguity, false-positive or completion number is produced at all.

## 1. Broad routing — NOT SCORED

- **Artifact.** `phase2_task_contract_resolver_ollama_6616688b.json`
- **Command.** `python scripts/run_phase2_task_contract.py --model ollama:qwen3.5:9b --base-manifest <phase 2 base manifest> --allow-live`
- **Fixture.** `tests/fixtures/task_contract_holdout_v2.json`, 66 cases over six lanes, sealed by
  canonical-JSON digest `b81229d42790ec0be8a8f87d22cf6447feb21351859db6732a8e9d11395d9a8e`
  (`jarvis/task_contract_eval.py:24`; the digest is over canonical JSON, not file bytes).
- **Configuration.** Local attesting provider only; no tools, memory, workspace, training or
  fallback; one fresh conversation per case.

**Observed.** 59 of 66 cases resolved, 7 rejected; **0 provider errors, 0 unattested responses, 0
model mismatches**. Wall 120.9 s; p50 1,829.8 ms, p95 2,215.9 ms. `route_accuracy` and
`route_by_lane` are `null`; both gate rows read *not scored: the run did not resolve all cases.*

**Raw receipt counts — diagnostic only, never the gate number.** Lane-correct out of resolved out of
total: dialogue 11/11/12, research 10/11/12, creation 10/11/12, external_action 3/9/12, inspection
1/11/12, configuration 2/6/6; **total 37/59/66**. That is 0.561 over the fixture and 0.627 over
resolved cases. **Do not read the shortfall as a scoring artefact**: inspection and external_action
are the reason, and even a complete run would very likely miss 0.90 on this model.

**The rejections are real verdicts,** not transport or parse failures: they come from post-parse
reconciliation — `reconcile_task_contract_continuation` (`jarvis/task_contract.py:1502`) and the
`_validate_consistency` check (`:877`) it reaches through `bind_provided_material_continuation` —
which runs after `parse_task_contract` has already accepted the payload.

**Non-determinism.** Four passes at temperature 0.0 with a fixed seed (one here, three inside the
WP-4 run) resolved 59, 59, 60, 60; `p2_research_04` was rejected in two and accepted in two. Six
cases — `p2_creation_10`, `p2_dialogue_06`, `p2_external_09`, `p2_external_11`, `p2_external_12`,
`p2_inspection_05` — were rejected in **every** pass.

**Limitations.** One run, one model, one host; resolver-level only, so nothing is executed and nothing
is claimed about completion. Decoding is pinned (temperature 0.0, seed 0, think disabled, 8,192
context) and says nothing about other settings; the provider version is recorded, not verified.

## 2. Verified workflow completion — NOT SCORED

- **Artifact.** `phase2_task_contract_outcomes_ollama_6616688b.json`
- **Command.** `python scripts/run_phase2_outcomes.py --model ollama:qwen3.5:9b --resolver-passes 3 --base-manifest <phase 2 base manifest> --allow-live`

`verified_workflow_completion` is `null` and every gate row records no observation. The scorer
refused: *"Phase 2 predictions do not match the frozen cases (missing p2_creation_10,
p2_dialogue_06, p2_external_09, p2_external_11, p2_external_12, p2_inspection_05)"* — six cases that
yielded no accepted contract in any of the three resolver passes.

**Those six are genuine grounding verdicts,** each reproduced live against production
`jarvis/task_contract.py` — not transport, grammar or attestation failures: three are the explicit
cancellation guard, one "creation requires an artifact kind", one "missing input keys must never
request secrets or credentials", one "continuation target is not grounded in the current turn". The
paraphrased holdout wording is what trips them.

**What the run produced.** 60 agent runs (the cases with an accepted contract), 183 model calls,
wall 880.6 s (resolver 391.9 s, smoke 27.4 s, outcomes 461.3 s at 7.688 s/case). Final status 46
complete / 14 incomplete; TaskContract status 32 resolved, 27 not attempted, 1 fallback. **No case
reported `not_supported`**, which proves the production semantic lane actually ran. Every outcome
request named the pinned model; served-model attestation was verified for the resolver phase only.
The plan's 1.0 smoke gate was missed at 0.2 and the boss ruled to continue, because the request
recorder demonstrably worked (17 schemas observed on `p2_creation_01`) and the stronger wiring gate
passed.

**A second obstacle survives fixing the first.** Of the 40 executed exposure-required cases only **10**
observed any offered tool. Four misses are the deterministic zero-model-call fast path
(`jarvis/agent.py:14481-14487`); the other **26** are the `:15270` short-circuit that sets
`schemas = []` when the 23-flag `dialogue_only` classifier (`:13307-13331`) fires on a non-dialogue
lane — a classifier that takes no input from the resolved contract, descriptive by design
(`:8093-8095`). **Nine of those 26 finished "incomplete" after answering an action request out of model
weights** — a product gap with behavioural risk, not a benchmark artefact.

**Structural exclusions (8 of 66), scorable denominator 58,** from
`jarvis.phase2_report.structural_exclusions`: `p2_creation_02`, `_06`, `_11` (execution disabled by
ruling — a model-authored script would run with the host user's own authority and no throwaway
account exists here); configuration *write* cases `p2_configuration_02`, `_03`, `_06` (no capability
tool at all: `screen_companion_control` is in neither `MUTATING_TOOLS` nor
`EXTERNAL_MUTATION_TOOLS`, and `allow_screen_companion` is never passed `True`); and configuration
*read* cases `p2_configuration_01`, `_05` on the weaker ground that no tool matches semantically.

**What must change before a completion number can exist,** in order: (1) the resolver must satisfy the
grounding guards on those six paraphrased cases — prompt or grounding-rule work owned by WP-1 and
WP-2; (2) the exposure gates must let a resolved contract inform `dialogue_only` and
`_schemas_for_state`, a Codex decision; (3) the scorer's assumption that exposure is observable only as
an offered schema must be reconciled with production's deterministic dispatch, a design question.
**The sealed fixture and scorer are not to be edited to reach a number.**

**Toolbox scope.** The agent ran over `jarvis/task_contract_outcome_toolbox.py`, an isolated 36-tool
implementation composed from production helpers, never inherited from `ToolBox` and unreachable from
`jarvis.agent`; its six documented divergences — no approval gate, simulated external effects, web
reads, residual execution authority, pre-dispatch digests, the configuration lane — bound every
number here. **Contract-level metrics in this artifact are not gate evidence:** a prediction set may
span more than one resolver pass, so §1's single-pass artifact carries that. Latency is an upper
bound — another benchmark held the model resident at a different context length.

## 3. Material-ambiguity clarification recall — NOT SCORED

Same artifact and command as §1. `ambiguity_recall` is `null` for the reason above. Raw receipt counts
— not scorer metrics: **all 11** material-ambiguity cases resolved and **all 11** were correctly
clarified, 1.000 over resolved against a 0.90 threshold.

**Honest limitation.** The denominator is 11. One miss lands at 0.909 and two miss the threshold
outright. It is too thin to carry a gate on its own; widening it is the held follow-up package.

## 4. Unnecessary clarification — NOT SCORED

Same artifact and command as §1. `specified_false_positive_rate` is `null`. Raw receipt counts: of
55 specified (non-ambiguous) cases, 48 resolved and **13 drew an unnecessary clarification** — 0.271
over resolved, against a ≤0.10 threshold. **After routing, this is the weakest observed behaviour in
Phase 2.**

By lane: inspection 5 (`p2_inspection_02`, `_03`, `_07`, `_08`, `_10`), external_action 3
(`p2_external_04`, `_07`, `_10`), dialogue 2 (`p2_dialogue_02`, `_10`), one each in configuration,
creation and research. Inspection is also the worst routing lane (1/12); the two likely share a cause.

## 5. Memory precision@3 / recall@3 — MET

**Re-verified by execution on this tree**, not carried forward:
`python -m unittest tests.test_memory_retrieval_holdout_v3 tests.test_memory_retrieval_holdout_v5`
— 3 tests, **OK**, 1 skipped, 1.663 s. Sealed holdout V3
(`tests/fixtures/memory_retrieval_holdout_v3.json`, sha256
`956d28fce6bdf759973486ba2aead14b9337f40a239c4c5b19d23fc382eaa67f`), 85 cases over 76 records:

| Metric | Gate | Observed |
|---|---|---|
| precision | 0.85 | **1.0** |
| recall | 0.80 | **0.9286** |
| no-hit accuracy | 0.95 | **1.0** |
| leakage | 0 | **0** |

**Honest asterisk.** The aggregate passes because the claim and lesson channels are both at 1.0 recall;
the **general channel alone is 0.7727**, below the 0.80 gate, with five misses (`general-g02`, `g07`,
`g11`, `g15` paraphrase and `general-g17-substring-target`). The gate is met as defined, but that gap
is real and belongs to the memory modules' owner. **Second asterisk:** the V5 holdout (144 cases) is
token-gated and was **skipped** here — the run token was not supplied — so it is not part of this
dossier's evidence and must not be cited as passing in it.

## 6. Cross-domain transfer lift ≥ 15 pp — NOT MET (inconclusive)

- **Artifact.** `phase2_transfer_lift_ollama_6616688b.json`, attestation
  `370914caf8359bc8d68773f769fc40cce30802982557a526708696793ae2e13f`
- **Command.** `python scripts/run_phase2_transfer.py`
- **Fixture.** `tests/fixtures/transfer_lift_holdout_v1.json`, sealed, sha256
  `df65e2d67e7c12a0146bea1e91d67f0f8f8db5b638f2bc576677849cb1414b23`; 40 source/target pairs plus 12
  negatives; temperature 0.0, seed 20260904, think disabled.

| Arm | Passes / 40 | Rate |
|---|---|---|
| control | 28 | 0.700 |
| placebo | 27 | 0.675 |
| treatment | 31 | 0.775 |

- treatment − control **+7.5 pp** against a **15.0 pp** threshold → **gate fails**.
- treatment − placebo **+10.0 pp**; placebo − control **−2.5 pp**. Treatment beat placebo by more
  than it beat control, so what lift exists is content-attributable rather than a presence artefact.
- Exact sign test, treatment vs control: **p = 0.1875** on 5 discordant pairs (4 better, 1 worse);
  treatment vs placebo p = 0.1094 on 6. **1 regression** (`tl_pos_022`, `doc_authoring` family).
- Negative transfer: **12/12 rejected (100%)** with a live harmful arm that does carry the refused
  advice and produced **0 excess passes**. Leakage: output 0, selection 0, oracle 0; parse rate 1.0
  in all four arms.

**Why this is inconclusive, not a refutation.** Of the 12 cases control failed, only **4 were
solvable by any arm**, so the fixture's **arithmetic ceiling was 10.0 pp against a 15.0 pp
threshold** — unreachable before the model was consulted. Design power at the observed rates is
**0.081** (supplied by the WP-9 review, not derived by the harness). Two of the four wins over control
(`tl_pos_012`, `tl_pos_034`) were also placebo wins with byte-identical replies; only `tl_pos_025`
and `tl_pos_033` are content-attributable at case level.

**Scope.** `claim_scope` is `held_out_model_in_the_loop_benchmark_not_production_ab_activation`:
**not** production causal evidence, and it cannot activate advice. Verifiers are exact-match
oracles, so a correct answer in an unexpected shape scores as a failure. The artifact records that
its descriptive fields were regenerated by a later evaluator revision than the one producing the
sealed numbers, so `evaluator_sha256` is not the current digest of `jarvis/transfer_lift_eval.py` —
documented, not an error. The v2 fixture requirements distilled from this result are in
`PHASE2_TRANSFER_LIFT.md` §11; a re-run under them is what makes this gate answerable.

## 7. Twenty workflows of ≥5 stages survive restart — MET

**Re-verified by execution on this tree:** `python -m unittest tests.test_long_horizon_eval` —
7 tests, **OK**, 60.9 s, exit 0. Sealed holdout
`jarvis/evaluation_fixtures/long_horizon_restart_holdout_v1.json`: **24 workflows** from 6 templates
of **5 stages each** — above the roadmap's 20 × 5 — across three crash points (`before_mutation`,
`after_mutation_before_receipt`, `after_receipt_before_cursor`, 8 workflows each), with **10
negative controls** (six budget classes, cancellation, tampered checkpoint, cross-project
checkpoint, replayed effect) and **zero duplicated irreversible effects**.

**Scope limits — read before quoting.** This verifies the coordinator and recovery protocol only. The
benchmark's "executor" is a harness writing synthetic artifacts; no real work advances and no real
e-mail, drive or calendar API is exercised. The gate passes on the sealed protocol; the roadmap *work
item* ("finish TaskContract continuation across … multi-stage work") is a different question, open
pending **G-7**. Executor design increment 1 is approved with changes in
`docs/LONG_HORIZON_EXECUTOR_DESIGN.md` (non-mutating stages only; the mutation protocol is a separate,
unapproved draft), and a later run over real work would **extend** this evidence.

## 8. Brier ≤ 0.25 and calibration error ≤ 0.15 — OPERATOR-ONLY

- **Artifact.** `phase2_calibration_ollama_6616688b.json`
- **Commands.** `python scripts/run_phase2_calibration.py sandbox --root <sandbox> --per-family 20
  --run-out <run-1>`, a retry pass, `merge-run`, then `python scripts/run_phase2_calibration.py
  report --database <sandbox>/data/jarvis.db --run-file <merged> --evidence-out docs/evidence
  --manifest-file <phase 2 base manifest>`

**These are sandbox rows.** They come from an isolated data directory and **do not move the operator's
initiative gate**, which reads his own database. `Memory.competence()` counts only `interactive`,
`worker` and `proactive` outcomes and excludes `practice` and both `companion_*` origins, so
calibration cannot be manufactured by a harness. Closing this gate is **G-2/G-4, operator-only.**

Three families × 20 outcomes, 60 counted, 1 unresolved (a case killed at the per-case timeout had
already recorded its prediction); 1,575.2 s.

| Family | Attempts | Brier (≤0.25) | Calibration error (≤0.15) | Success | Evidence rate |
|---|---|---|---|---|---|
| conversation | 20 | 0.00125 | 0.025 | 1.000 | not applicable |
| file_ops | 20 | 0.2284 | 0.0869 | 0.700 | 0.800 |
| security_analysis | 20 | 0.0450 | **0.150** (exactly at tolerance) | 1.000 | not applicable |

All three clear the production gate after the boundary-comparison fix in §10.2; `security_analysis`
sits exactly on the 0.15 tolerance and was refused before it by floating-point representation alone.

**Read the 1.0 success rates with care.** For `conversation` and `security_analysis` the runtime's
verification is `not_applicable`, so a finished turn is recorded complete whether or not the answer
was correct — this is calibration about *completing the turn*, not about being right. The harness's
independent verification is reported separately: 54/60 runtime-complete, 51/60 independently
verified, 57/60 agreement with ground truth, and that agreement is an **upper bound** for those two
families. Only families needing no extra capability were exercised (coding needs host execution,
research needs external access). No drift signal fired — no family had enough history for one.

## 9. What only the operator can do

Seven items: **G-1** schema-bound sign-off (non-blocking), **G-2** calibration in his own database,
**G-3** retire-or-re-author decisions for quarantined legacy memories, **G-4** drift clearance, **G-5**
production transfer activation, **G-6** real external services, **G-7** executor go/no-go.

**G-1.** Pass §10.1's neutrality argument and bisection to Codex; work does not block on it, and if Codex objects the fallback is an Ollama upgrade and a re-probe.

**G-2.** Follow [`CALIBRATION_RUNBOOK.md`](CALIBRATION_RUNBOOK.md) §4 — roughly 20 ordinary tasks per
family for three families, run in the ordinary runtime with `python -m jarvis ask` — then:

```powershell
python -m jarvis competence
python -m jarvis competence --json
python -m jarvis status
python -m jarvis initiative
```

`competence` prints per family `n`, success rate, Brier, mean steps, and either `enabled` or `blocked`
with the exact unmet reasons. Days of ordinary use, not an afternoon.

> **Correction to the plan.** The implementation plan's §7 prints
> `python -m jarvis status --calibration`. **That flag does not exist** — `status` takes only
> `--json`. Use `python -m jarvis competence` for per-family gate detail.

**G-3.** The read-only report and the retire/re-author receipt design are a held follow-up; the write
path belongs to the owner of the memory modules on the M-series branch. **The report's output is
never committed** — aggregate counts only.

**G-4.** The same daily use as G-2 builds the baseline. `jarvis initiative` can still refuse with
`behavioral drift is currently unresolved`; a signal needs 10 recent and 10 baseline outcomes in a
family, and the remedy is to fix the regression it names, never to widen the window.

**G-5.** Only if the operator wants `advise` live: a randomized trial with assignment persisted before
outcomes exist, then a separate explicit promotion of that exact manifest. §6's held-out lift satisfies
the roadmap sentence and **must never be described as production causal evidence**.

**G-6.** Nothing in Phase 2 proves exactly-once behaviour against a real e-mail, drive or calendar API;
§7's result must not be quoted as if it did.

**G-7.** §7's gate already passes on the sealed protocol; the executor serves the roadmap *work
item*, costs roughly 1,400–1,900 lines, and would be the first component to advance durable stages
**unattended**. Build it in Phase 2, or record the work item as deliberately deferred. Until then
the design proceeds and the implementation does not start.

## 10. Production findings routed to Codex

1. **Structured-output schema bound (sign-off G-1).** `jarvis/task_contract.py:461`, `2_000 →
   2_001`, one literal. Behaviour-neutral: `parse_task_contract` independently enforces 2000 at
   `:940`, so a 2001-character goal is still rejected one layer up. The defect is a point failure at
   the literal 2000 in Ollama 0.32.15's grammar compiler — 1999 and 2001 both compile, nested
   occurrences fail identically, reproduced on three models, only with `think:false`, which is what
   production sends. Before the fix the agent caught the provider error and set the TaskContract
   status to `fallback`, so **every** local resolution failed silently and the semantic lane never
   engaged, invisibly to the operator. A named `OllamaGrammarRejected` and the capability probe in
   `jarvis/structured_output_compat.py` close that hole.
2. **Calibration boundary comparison.** `Memory.calibration_gate` in `jarvis/memory_predictions.py`
   now rounds the calibration error to nine decimals before its strict comparison. A family
   predicting 0.85 against an observed 1.0 has an error of exactly 0.15 — the documented tolerance —
   but in binary that is `0.15000000000000002`, so it was refused while printing `calibration error
   must be <= 0.15; is 0.150`: a verdict contradicting its own explanation. **Eight of the eleven
   gate-reachable exact-boundary pairs at the 20-outcome minimum were refused this way.** At that
   minimum both terms move in steps of 0.05, so nine decimals of tolerance cannot loosen the gate.
   The `>` operator and the thresholds in `jarvis/proactive.py` are untouched, and the reason string
   that printed `0.150` now shows four decimals so the message can no longer contradict the verdict.
   The module is sealed-pinned, so the fixtures were resealed with the digest-only reseal tool.
3. **Open policy question — what should Tier 1 count?** `initiative_eligibility` requires three
   calibrated families out of eleven and does not care which. `conversation` and `security_analysis`
   have no evidence to check and calibrate easily; `file_ops` and the coding and research families bind
   their outcome to a real effect and are much harder, so Tier 1 authority can be opened by three
   families that never touched anything. A policy decision, not a defect.
4. **Resolver rejections, and the prompt-refinement question.** Six of 66 cases were rejected in all
   four passes and one more was non-deterministic; every rejection is a genuine grounding verdict
   (§2), but an all-or-nothing scorer plus a ~91%-accepting resolver yields no number at all.
   **Question:** refine the resolver prompt, which changes what the benchmark measures, or give the
   scorer an explicit sealed treatment for resolver-rejected cases? Neither was decided
   unilaterally. Related: the plan's §1 gate table omits the transfer-lift bullet and needs a row.
5. **The `dialogue_only` short-circuit fires on action lanes.** `jarvis/agent.py:15270` sets
   `schemas = []` when the 23-flag classifier at `:13307-13331` fires; that classifier reads nothing
   from the resolved contract (descriptive by design, `:8093-8095`). It accounts for 26 of the 30
   exposure misses, and nine of those cases answered an action request out of model weights and
   finished "incomplete" — a product gap with behavioural risk, beyond the benchmark. **Design
   question:** should a resolved contract inform `dialogue_only` and `_schemas_for_state`? Separately,
   the scorer treats exposure as observable only as an offered schema, which production's
   deterministic dispatch paths contradict; the sealed fixture and scorer must not be edited for it.
6. **A dormant capability, and the configuration lane.** `screen_companion_control` is in neither
   `MUTATING_TOOLS` nor `EXTERNAL_MUTATION_TOOLS`, and `allow_screen_companion` defaults to `False`
   at `jarvis/agent.py:7975`, is filtered on at `:8010`, and is **never passed `True` by any
   caller**, so the tool is never offered. There is also no production tool at all for Public
   Presence or proactive control. Five configuration-lane cases are structurally unreachable. Per
   the boss ruling the classification was left alone and the limitation recorded instead.
7. **Attestation conflated with contract status.** A case attested on the wire whose contract was then
   rejected records `not_observed`, so `exact_model_only` is `false` whenever any case is rejected
   for a reason unrelated to attestation. Read attestation from `model_unattested` and
   `model_mismatch`, which count only genuine failures (both 0 here). Splitting attestation into its
   own axis is a recorded follow-up for the module owner.

## 11. Evidence-policy compliance

Measured against [`EVALUATION.md`](EVALUATION.md) *Evidence policy*:

- **Exact base, configuration class, command, platform, pass/fail, limitations** — all six required
  fields are in every artifact. Base is the manifest hash with file count, branch, last commit and
  an explicit `committed: false`; configuration class is a prose summary plus the exact switches;
  commands store local paths as placeholders; platform is `Windows` / `3.13.7` and nothing else.
  Every gate row carries `threshold`, `comparator`, `observed`, `passed`, and an unscored row
  carries `observed: null` with a note that the run was incomplete, never a fabricated number.
  Limitations lists hold 10, 11, 8 and 8 entries, reproduced in the section citing them.
- **Not committed.** No prompts, case text, model replies, conversation logs, usernames, local
  paths, hostnames, e-mail addresses or account data, and **no per-record operator database
  output**. The calibration artifact holds aggregate family statistics only, from a sandbox
  database, with an explicit `sandbox_disclaimer`.
- **Historical, not permanent.** Every number is one run, one model, one host at pinned decoding,
  refreshed after any code, model, provider or hardware change; runs are not bit-reproducible even at
  temperature 0.

Release scan: `python scripts/check_public_release.py` from the worktree, plus that scanner's
`_path_findings`/`_content_findings` applied directly to each Phase 2 document and artifact, since
untracked files fall outside its git walk.
