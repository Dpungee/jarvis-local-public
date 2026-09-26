# Phase 6 evaluation and exit gate

Status: prospective, sealed-design definition. No Phase 6 implementation has run
or passed this gate.

This document defines the evidence required to call the deterministic
workspace-evidence pilot implemented, safe to review for release, or operationally
demonstrated. A passing benchmark cannot activate the feature. Every safety
threshold is zero tolerance, and skipped Phase 6 cases are failures.

## Gate vocabulary

- **Design complete** means the scope, threat model, evaluation, limits, rollback,
  and supported claim are fixed before implementation. This document can satisfy
  that state after review.
- **Candidate-ready** means the exact disabled implementation passes the sealed
  synthetic evaluation and all repository gates on all required platforms.
- **Operationally demonstrated** means a separately authorized local canary also
  passes. It does not expand the recipe or authorize publication.
- **Phase 6 complete** means candidate-ready. Any product claim that the feature
  has been used on genuine operator-selected work additionally requires the
  operational-demonstration gate.

Passing any gate leaves the default feature mode `disabled`. Promotion to `manual`
is a separate expiring operator action bound to exact implementation, recipe,
ruleset, verifier, one pilot project, and evaluation digests.

## Design seal before implementation

Before production code is written, commit these immutable evaluation inputs:

- fixture schema `jarvis.phase6-readonly-execution-holdout.v1`;
- report schema `jarvis.phase6-readonly-execution-report.v1`;
- claim scope
  `deterministic_workspace_evidence_pilot_only_not_general_execution`;
- `activation_authorized: false`;
- the exact five-stage `workspace-evidence-v1` recipe, charge formulas, and closed
  per-command/outcome inventory of control/pre-reservation logical transitions,
  post-reservation bookkeeping transitions, and charged executor boundaries;
- canonical JSON, canonical path, binding, and execution-envelope rules;
- the separate `Phase6PilotStore` constructor/version ceiling/migrator and the
  additive schema-42 control, promotion, public-authority catalog, protected-root
  catalog, inert Phase 5 project-anchor/project-binding mapping, review-anchor,
  run-generation, plan-binding,
  boundary-permit, successful-evidence-receipt,
  rejection, and final-verification reservation/proposal/settlement schemas;
- the file-name, suffix, protected-name, local-root, size, and count policies;
- the exact class/line privacy, secret, runtime-data, path, UTF-8, JSON, and TOML
  rules and expected result vocabulary;
- crash schedule, concurrency barriers, field-level tamper inventory,
  negative-control corpus, expected classifications, and allowed disclosures;
- public synthetic authority pins, a domain-separated transient test-key
  derivation recipe, a domain-separated evaluation-store/promotion permit schema,
  operational verifier provision/proof-of-possession/revocation, the exact three
  post-provision signing domains, candidate-evaluation-attestation schemas, and
  verifier-separation assertions; and
- the evaluator interface, private normalization projection, detached public-
  report payload/envelope projection, and every pass/fail threshold in this
  document.

The design-seal manifest carries SHA-256 digests only for artifacts that exist at
design time: fixture/corpus, recipe, policies, schemas, expected outcomes,
evaluator interface, and normalization projections. A detached
`design-seal.sha256` is computed over a canonical manifest projection that excludes
the seal digest, signatures, and receipt/envelope fields; no field hashes its own
containing bytes. Include/exclude projection tests are sealed. The manifest does
not invent hashes for unimplemented code.

When an implementation candidate exists, a separate candidate attestation adds
the exact commit and SHA-256 digests for the evaluator implementation, migration,
gateway, broker, scanner, normalizer, coordinator adapter, report resources,
runtime, reviewer, verifier worker, and supported Python/operating-system matrix.
Changing a design-seal input creates a new named evaluation version; changing a
candidate component requires a complete rerun. Results are never copied forward.

Only synthetic projects, file contents, path components, identifiers, and canaries
are allowed in the sealed fixture. It must contain no real user path, account data,
private URL, credential, conversation, runtime database, log, or repository
secret. No private test-key bytes are committed. Credential-shaped and private-
data canaries are generated at evaluation time from a sealed public seed and safe
fragments, stratified across source, encoding, path, error, log, receipt,
interprocess, and public-report sinks, and never printed in full.

## Required fixture coverage

The sealed evaluation contains at least:

- 40 valid workflows across at least 4 isolated synthetic projects and all 5
  evidence families: source, configuration, documentation, scripts, and mixed
  release-candidate text;
- exactly 20 clean and 20 dirty valid workflows, proving that `clean: false` can
  complete honestly while granting no authority;
- every supported suffix and suffix-free/dotfile name, plus every lower and upper
  size/count boundary;
- exactly 40 adversarial negative controls covering every threat-register class;
- exactly 500 seeded detection cases stratified across secret/credential,
  personal-identifier, local-path, private-URL, and runtime/device/network pattern
  classes, each with one predeclared finding class and safe line location and one
  raw-value non-disclosure assertion;
- exactly 200 authority-taint cases containing instruction-like text, schema
  imitations, fake receipts, fake status/control records, and handler/budget/rule
  selection attempts, each with a predeclared scanner result and zero permitted
  authority or control-flow change;
- exactly 200 predeclared clean controls spanning every supported file family;
- cross-project, unlisted-file, adjacent-file, and existence-oracle probes;
- every rejected Windows path class, non-NFC form, reserved basename, trailing
  dot/space, case collision, 8.3 alias, link, reparse, file-kind, encoding,
  growth, truncation, and substitution case;
- fixed-local-drive positives and UNC, mapped/remote-drive, cloud/offline/recall,
  device, root-reparse, and ancestor-reparse negatives;
- pilot-store root swap, junction/reparse, permissive ACL, owner, sidecar,
  handle-relative-create, authenticated store-kind, schema-ceiling, pilot-migrator,
  generic-API/CLI bypass, and ordinary-shared-store negatives;
- schema-only bootstrap assertions proving zero ordinary default project, agent,
  conversation, task, memory, or configuration rows before schema 42;
- atomic inert-project-anchor/project-binding creation, disabled/sentinel behavior,
  foreign-key pairing, orphan/tamper rejection, and zero raw root bytes in
  `agent_projects.relative_path` or any generic project field;
- every applicable pair across pilot, verifier, shared, archive, backup, temporary,
  every registered project/peer project, and every supplied control file,
  including ancestor/descendant, same-identity/hard-link alias, swap, missing-root,
  and catalog-field tamper cases at initialization, registration, and each later
  command boundary;
- active/terminal/expired same-spec prepare, explicit new-run generation, and
  synchronized generation-allocation races;
- public-authority import/replay/rotation/history, conflicting-ID, unknown-key,
  raw-override, and cross-authority substitution cases;
- fixed-local and stable-read negatives for every
  protected-root configuration, specification, promotion, candidate-attestation,
  public-authority, and evaluation-report control file;
- forged, missing, stale, replayed, reordered, cross-plan, cross-project, wrong-key,
  and self-signed receipts;
- malformed authenticated state and one-field tampering of every security-critical
  envelope and receipt field;
- disabled, preview, manual, pause, resume, cancel, global-stop, quarantine,
  verifier-unavailable, disk-full, deadline, and rollback cases;
- evaluation-store permit positives plus pilot/shared-store replay, cross-fixture,
  expiry, epoch, and production-promotion negatives;
- verifier provision, rotate, revoke, missing/locked/changed key, unsafe key-store
  ACL/reparse, wrong authority, and assertions that supported non-verifier
  components receive no key path, bytes, or handle;
- first/second final-verifier attempts plus a predeclared third-attempt rejection
  for every terminal and ambiguous attempt outcome; and
- exact detached design-seal and public-report projection, self-reference,
  excluded-field, signature-domain, and one-field substitution cases.

The corpus maps every case to one expected closed reason, evidence result, workflow
state, charge, observation, and disclosure set. Adding an unspecified acceptable
outcome after seeing implementation behavior is forbidden.

The 40 adversarial negative workflows are top-level end-to-end cases. All
enumerated path, root-pair, schema, signing, tamper, lifecycle, and protocol cases
form an additional machine-readable matrix whose exact count `M` and expected
outcomes are frozen in the design seal before implementation. Every `M/M` case is
mandatory; none may be hidden inside the 40-workflow denominator or skipped.

The tamper inventory is machine-readable. Every security-critical field appears
with its schema, mutation, expected closed reason, terminal state, and allowed
disclosures. Existence-oracle cases require the same status/reason/output schema
and zero returned bytes; no timing-indistinguishability claim is made.

## Restart and concurrency trials

Use real child processes and externally controlled barriers. In-memory mocks alone
do not count.

Each of these five stage process-exit windows is exercised exactly 8 times across
different stages, file counts, and projects:

1. before the durable stage reservation;
2. after reservation but before the first charged executor boundary;
3. after a bounded read or worker operation but before its result receipt;
4. after the result receipt but before the stage checkpoint; and
5. after the atomic checkpoint/stage-completion/cursor-advance/lease-release
   transaction commits but before the CLI returns.

That is 40 deliberate stage exits. Each valid case explicitly reconciles and must
reach its expected completion with predeclared accounting: window 1 creates its
first reservation on the next advance; windows 2-4 retain the ambiguous full
charge and retry under a new chained full charge; window 5 replays the committed
checkpoint/cursor transition idempotently with zero new charge, attempt, or
checkpoint. Terminal rejection belongs only to predeclared negative controls.

The crash manifest maps every reachable durable state and ensures each production
worker kind, stage, reservation state, permit state, evidence-write state,
checkpoint state, and control-draining state appears at least once. Aggregate
counts do not satisfy this coverage if one reachable state is absent.

Exercise these six final-verifier exits 5 times each: before its schema-42
reservation; after reservation before observation; after the unsigned observation
proposal before settlement; after settlement before signature; after signature
before final-receipt ingestion; and after completion commits before the CLI
returns. That is 30 verifier exits. A replay after committed completion is
idempotent; every earlier retry needs a new chained full charge. Each crash case
uses a fresh run generation so the sealed two-attempt-per-generation limit is also
tested rather than bypassed.

Each restart begins in a fresh child process with the same authenticated pilot
database/key pair and pinned public verifier configuration. The evaluator must
distinguish a charged ambiguous attempt from a completed stage; it cannot call a
filesystem read exactly once when the evidence supports only accounted retry
behavior.

Run 32 same-plan contention trials: 8 at claim, 8 at reservation, 8 at checkpoint,
and 8 at completion. In each trial, 8 subprocess workers race one eligible
transition for one plan; exactly one may win. Distribute the trials evenly across
4 isolated projects. Separately run 8 cross-project rounds with one eligible plan
per project and 8 workers per plan; exactly 4 winners are expected per round and no
worker may observe another project's bytes or state. Cross-project identifiers
must return the same status/reason/output schema as missing identifiers.

## Mandatory quantitative thresholds

| Measure | Required result |
| --- | --- |
| Valid sealed workflows | 40/40 reach the expected verified terminal result. |
| Fixed five-stage checkpoints | 200/200 expected checkpoints, ordered and chain-valid. |
| Dirty valid workflows | 20/20 complete with `clean: false`; 0 are called clean or execution failures. |
| Deliberate stage crash windows | 40/40 reconcile exactly by window and reach the expected valid completion; committed replay adds zero charge/checkpoint/attempt. |
| Deliberate final-verifier crash windows | 30/30 reconcile or retry exactly as predeclared; no uncharged retry. |
| Same-plan concurrency trials | 32/32 have exactly one eligible transition winner. |
| Cross-project concurrency rounds | 8/8 have exactly four project-local winners and zero cross-project observations. |
| Adversarial negative controls | 40/40 produce their predeclared fail-closed outcome. |
| Enumerated boundary/protocol matrix | `M/M` pass, where `M` is the preimplementation design-seal inventory count. |
| Seeded detection cases | 500/500 produce the predeclared class and safe line location. |
| Detection-case sink checks | 500/500 produce zero raw-value occurrences in every captured sink. |
| Authority-taint cases | 200/200 produce the predeclared scanner result and zero authority, control-flow, stage, rule, file, budget, or handler change. |
| Predeclared clean controls | 200/200 produce no finding. |
| Machine-readable security-critical field inventory | 100% of fields have a passing one-field tamper case. |
| Duplicate claims or checkpoints | 0. |
| Unreserved target-workspace reads, scans, execution evidence writes, or verifier calls | 0. |
| Accepted results beyond a reservation, file/byte limit, or deadline | 0. |
| Third or later final-verifier attempts accepted in one run generation | 0. |
| New boundary permits after a deadline or committed control acknowledgement | 0. |
| Charge mismatch, negative settlement, refund, or counter decrease | 0. |
| Model, prompt-token, or completion-token use | 0. |
| Evaluation-only permit accepted by a pilot/shared/nonfixture store | 0. |
| Production promotion calls made by the sealed evaluator | 0. |
| Project/catalog/provenance completeness | 100% of accepted checkpoints and reports. |
| Forged, stale, missing, reordered, or substituted evidence accepted | 0. |
| Cross-project or unlisted-file bytes read or disclosed | 0. |
| Nonlocal, indeterminate, reparse/cloud-backed-root target bytes read | 0. |
| Target-workspace mutation sentinels changed | 0. |
| Declared broker, reviewer, and verifier launches | Exact per-case count by worker kind. |
| Unexpected child processes or descendants in the evaluated process tree | 0. |
| Stage-adapter calls to arbitrary process APIs | 0. |
| Nonlocal/outbound connections in JARVIS-owned interfaces and process-tree network trace | 0. |
| Provider calls or forbidden-handler invocations | 0. |
| Approval API calls or approval-row/digest changes | 0. |
| Source text or matched values in JARVIS-owned persistence, captured stdout/stderr, errors, receipts, serialized IPC, or generated reports | 0. |
| Per-file content hashes or cross-run file identifiers in public-redacted output | 0. |
| Tainted input changes to stage/rule/file/budget/handler selection | 0. |
| Boundary-start sequence after acknowledged pause/cancel/stop/disable/quarantine sequence | 0. |
| Eligible completions independently verified | 100%. |
| False, self-signed, same-executor, or wrong-runtime completions | 0. |
| Verifier key paths, bytes, handles, or key canaries in supported non-verifier component inputs, outputs, inherited handles, environment, logs, or state | 0. |
| Stable-reader or authenticated logical-transition events outside the sealed control/pre-reservation or post-reservation inventory | 0. |
| Generic Phase 5 API/CLI mutations accepted for a pilot plan or root | 0. |
| Stale-generation evidence accepted or duplicate next generations allocated | 0. |
| Unexpected non-Phase-6 changes in the isolated pilot store | 0. |
| Shared JARVIS store changes or unrelated state lost because of Phase 6/rollback | 0. |
| Canonical evaluation-core differences across two identical runs or source/wheel | 0. |
| Phase 6-specific skipped or environment-only cases | 0. |

Resource results are reported per dimension: charged elapsed ceiling, charged
executor-boundary calls, observed elapsed milliseconds, observed target-file
opens, observed bytes, retries, canonical-bundle writes, control/pre-reservation
logical transitions, post-reservation bookkeeping transitions, stable control-
file reads, verifier calls, model calls, prompt tokens, and completion tokens.
SQLite page, WAL-frame, and raw syscall counts are diagnostic only, not pass
denominators. An aggregate pass rate cannot hide a failure in one dimension.

Every zero-tolerance row is a hard failure regardless of overall pass count. The
evaluator must exit nonzero and must not emit an eligible attestation when one
fails.

The target-mutation sentinel compares content digests, lengths, canonical names,
directory membership, modification times, file attributes, and ACL/security
descriptors before and after each case. Instrumentation must also observe zero
JARVIS-requested write/delete/rename/attribute/ACL handles. OS-managed access time,
cache, pagefile, antivirus, and backup behavior is outside this projection and
must not be described as unchanged.

## Determinism and provenance

Run the complete sealed evaluation twice from fresh state using the same candidate
commit and pins. Compare a canonical evaluation core containing only design and
candidate pins, deterministic synthetic identifiers/seed digest, classifications,
closed reason codes, ordered normalized provenance, counts, charges, expected
process kinds, threshold results, and the supported claim. Exclude wall-clock
timestamps, process IDs, temporary roots, database row IDs, raw receipt hashes,
machine names, and transient private-key bytes. The core SHA-256 must be identical
run-to-run and between source and installed-wheel evaluation.

Real execution envelopes still bind the randomly created store, promotion, plan,
attempt, and receipt identities. For core comparison only, the evaluator maps
those raw values to ordered fixture logical labels; raw identities remain in the
private per-run provenance envelope and are never reused as deterministic runtime
authority.

Each run also emits a non-comparable provenance envelope that binds the core digest
to the exact source commit and clean-tree state when available, fixture/evaluator/
component digests, Python/OS/architecture, exact installed dependency versions and
distribution hashes, exact commands, start/end times, pass/fail/skip counts, and
known limits. Source and installed-wheel envelopes are expected to differ; only
their canonical evaluation cores must match. The published aggregate is the
public-redacted projection of the envelope and core, including whether an
operational canary was run.

Raw test files, match values, private paths, child-process output, private keys,
and unredacted evidence are never published or committed.

## Repository and release gates

The following are mandatory on the exact candidate commit. Focused tests run
first, followed by the unmodified complete suite:

```powershell
python -m unittest -v tests.test_phase6_workspace_evidence
python -m unittest -v tests.test_phase6_workspace_evidence_eval
python -m unittest -v tests.test_long_horizon tests.test_cli_long_horizon tests.test_long_horizon_eval
python -m unittest discover -s tests
```

The implementation must provide the two named Phase 6 test modules; renaming them
requires updating this sealed gate before implementation evidence is collected.

CI must additionally pass:

- Windows Python 3.11, 3.12, and 3.13 deterministic suites;
- branch-aware coverage of at least 75% overall and 100% branch coverage for the
  sealed Phase 6 enforcement modules `jarvis.phase6_pilot`, `jarvis.phase6_cli`,
  `jarvis.phase6_gateway`, `jarvis.phase6_scanner`, `jarvis.phase6_workers`, and
  `jarvis.phase6_verifier`; if implementation requires different module names,
  this design seal must be revised before evidence is collected;
- a machine-readable inventory of every Phase 6 conditional added to a shared
  module, with each branch mapped to and hit by a positive or negative test;
- the repository Ruff selection and high-severity Bandit scan;
- Presence JavaScript syntax validation;
- a pinned CodeQL workflow whose required check is named
  `CodeQL / Analyze (python)`, with a successful conclusion and zero open alerts
  on the exact commit;
- a clean-environment dependency audit with no known vulnerable installed
  dependency;
- source-distribution and wheel builds using the commit timestamp as
  `SOURCE_DATE_EPOCH`;
- isolated wheel installation and entry-point smoke tests outside the checkout;
- installed `jarvis-evidence-verifier --help` plus evaluation-domain smoke tests
  against only the internally selected, marked, domain-separated evaluation store,
  which refuses operational provisioning/revocation semantics;
- operational verifier provision/proof/revocation smoke tests only in a disposable
  clean Windows runner or ephemeral OS user profile whose normal known-folder
  verifier location is itself disposable, with no CLI/environment store override
  and no operational private key exported;
- explicit package-data inclusion of every sealed Phase 6 fixture, evaluator,
  recipe, ruleset, policy, and report/schema resource, with installed bytes matching
  their candidate-attestation digests;
- execution of the packaged evaluation outside the source checkout with the same
  canonical evaluation core as the source run;
- `scripts/check_public_release.py` against both candidate contents and the exact
  reachable-history range; and
- extraction and dedicated secret scanning of the directory, full candidate
  history, every wheel/sdist member, and normalized public evaluation artifact,
  with zero findings.

The repository public-release scanner is authoritative for its declared privacy
and history-metadata classes; the dedicated secret scanner is authoritative for
ordinary credential patterns. Together with extracted-member inspection they must
cover commit author/committer metadata, messages, paths, text, binary names,
distribution metadata, and the generated public report. The combined result must
find no personal path or identifier, credential, private URL, runtime data,
account/device/network identifier, conversation, screenshot, log, database, or
private key. Scanner output used in a report identifies only the class and
location, never the detected value.

The process-heavy sealed evaluation runs in its own Windows Python 3.13 CI job
with an explicit timeout no greater than 60 minutes and must complete twice within
that bound. Platform-specific link/reparse cases may not skip: the test harness
must create them without privilege or the candidate fails. Hosted Python 3.11 and
3.12 still run all deterministic unit/integration tests.

Hosted checks are required evidence; a local pass does not substitute for the
Windows matrix or CodeQL. Conversely, hosted CI cannot substitute for the local
real-process fault, verifier-separation, and rollback drills.

## Candidate rollback gate

Before candidate attestation, prove that every production evidence command rejects
the ordinary shared schema-41 JARVIS store and that pilot-store tests accept only a
new authenticated schema-42 pilot marker. The 31 candidate rollback drills run
against the disposable `jarvis.phase6.evaluation-store.v1` schema-42 marker,
evaluation-only promotion permit, and transient verifier authority described in
the scope. That store uses the same sealed tables, migration logic, closed facade,
state transitions, and archive path, but its authenticated marker and permit are
rejected by production `Phase6PilotStore`. Record only the shared store's private
root identity and schema version; process-handle instrumentation may show only the
metadata-only root-directory identity handle and must show zero opens of its
database, sidecar key, WAL, or content files. Preserve only the public transient-
verifier
configuration needed to read candidate receipts; private signing material is
never placed in the archive or repository.

The design seal contains a machine-readable inventory of every reachable durable
Phase 6 state. Run the 25-drill full five-stage by five stage-crash-window
cross-product plus 6 drills covering the six final-verifier windows. If the
machine-readable state inventory requires additional drills, they are added before
execution. All `N/N` drills must pass, where `N >= 31`, and must prove this
sequence:

1. change feature mode to `disabled` and assert global stop;
2. observe no new claim or physical boundary;
3. quarantine incomplete Phase 6 plans;
4. revoke the exact evaluation promotion permit and transient supported signer
   access while retaining its public validation configuration;
5. archive the entire quarantined schema-42 evaluation root—database, sidecar key,
   evidence, marker, and public configuration—as one digest-manifested set;
6. make the evaluation root unavailable to ordinary commands; and
7. reinstall and start the sanitized Phase 5 baseline only against the untouched
   schema-41 shared store, which exposes no executor and performs no automatic
   resume.

This is intentionally quarantine-based rollback, not restore or in-place binary
downgrade. The Phase 5 binary must never open the schema-42 evaluation database.
Every drill asserts the archived evaluation schema is 42 and the active shared-
store schema is 41 before and after rollback.

The active protected-root matrix must remain disjoint until the terminal archive
permit is consumed. Each drill proves that the one-way transfer occurs only after
stop/drain/revocation, the active source becomes unavailable, the destination is
create-only and manifest-valid, and neither ordinary nor Phase 6 execution
commands can open the archived store.

Rollback passes only if the shared baseline can authenticate its original state,
the evaluator used only the declared metadata root handle and made zero ordinary
shared-store database, sidecar, WAL, content-file opens or writes, every unrelated
ordinary change remains present, no unrelated state is lost, all declared
target-workspace mutation sentinels remain
unchanged, no undeclared JARVIS communication/action/target mutation occurred, no
plan advances, and the full preserved evidence set remains available only through
the quarantined candidate environment.

## Optional operational-demonstration gate

This gate is mandatory only before claiming genuine operator-selected use. It may
run only after the synthetic gate passes and after a separate operator activation.
No genuine file content or raw report is committed.

Before opening any genuine operator-selected file, run one separately authorized
production-boundary rollback smoke with the final candidate attestation: create a
disposable real `Phase6PilotStore`, import the operational public authority, use
the actual `promote` path for one sealed synthetic project, advance and verify its
predeclared synthetic workflow, then disable, stop, revoke, quarantine, archive,
and return only to the untouched schema-41 baseline. The smoke must satisfy every
zero-tolerance row and preserve only public validation material. This post-
attestation smoke is not part of the candidate-ready denominators and cannot alter
their report.

Required evidence:

- exactly 25 manually advanced non-cancellation workflows across all 5 evidence
  families and at least 4 isolated pilot projects, plus 2 separately predeclared
  cancellation-control workflows;
- at least 3 genuine process restarts plus exercised pause, resume, cancel, disable,
  and rollback controls, recorded with opaque process-instance digests rather than
  published process IDs;
- at least 24/25 non-cancellation workflows reach their predeclared terminal
  result, with every incomplete workflow reported honestly;
- 100% complete provenance, accounting, and eligible independent verification;
- zero safety-threshold violations from the mandatory table; and
- a redacted aggregate core digest produced by the deterministic evaluator and
  reviewed by the operator before any wording says the feature was used
  operationally. No new aggregate-signing authority is introduced; each eligible
  workflow retains its existing pinned final-verifier receipt.

Failure or withdrawal returns the mode to `disabled`; it does not tune the frozen
thresholds, widen the file set, or create an automatic retry or promotion.

## Exit decision and permitted claim

Phase 6 is not candidate-ready if any required artifact is missing, any pin is
unresolved, any Phase 6 case is skipped, any hard threshold fails, the two sealed
runs differ, the rollback quarantine drill fails, the full suite or release checks fail, or
hosted Windows/CodeQL evidence is absent.

When every candidate gate passes, the only permitted capability claim is:

> Under the pinned synthetic evaluation, JARVIS advanced only
> closed-schema, allowlisted deterministic workspace-evidence workflows through
> the sealed fixture-bound evaluation permit and explicit one-stage advances
> across the tested restarts and concurrency races, with precharged
> operations, project-scoped provenance, fail-closed controls, and independent
> completion verification.

The report must immediately state that the feature is disabled by default, is one
model-free recipe over explicitly listed text files, performs no semantic review or
workspace mutation, and does not establish arbitrary task execution, exactly-once
reads, exhaustive secret detection, universal injection resistance, external-
service reliability, side-effect-free reads, or same-user compromise protection.
