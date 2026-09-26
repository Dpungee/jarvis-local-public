# Phase 6 workspace-evidence threat model

Status: design only; implementation and activation are not authorized.

This threat model applies only to the closed `workspace-evidence-v1` recipe
defined in [`PHASE_6_WORKSPACE_EVIDENCE.md`](PHASE_6_WORKSPACE_EVIDENCE.md). The
recipe is local, deterministic, model-free, and read-only with respect to the
target workspace. Internal registration, reservations, checkpoints, evidence,
and final receipts necessarily append to private coordinator state.

## Security objective

Given one operator-selected project and a bounded, explicit list of candidate
public-source files, JARVIS may inspect only those exact bytes and produce only
the pinned redacted evidence. Untrusted content must not gain authority, escape
the project, disclose private data, alter the workspace, consume an approval,
invoke another capability, evade resource accounting, or create a false
completion claim.

## Trust boundaries

```text
operator
   |
   v
closed specification validator -- creates terminal, non-queueable review anchor
   |
   v
Phase6PilotStore closed facade + Phase 5-compatible stop/lease/budget gates
   |
   v
Phase 6 positive-allowlist adapter
   |
   +--> sealed I/O broker --> bounded project-file gateway
   |                              |
   |                              v
   |                    untrusted named file bytes
   |
   +--> deterministic ruleset/normalizer --> redacted evidence receipts
   |
   v
separate evidence reviewer --> bounded gateway + second scan --> unsigned attestation
   |
   v
separate pinned final verifier --> canonical evidence only --> signed receipt
```

The operator is trusted to select the intended registered project and files, but
operator input is still schema-validated and cannot widen the recipe. The
coordinator is trusted only after its authenticated records and sidecar key pass
the Phase 5 integrity checks. Workspace bytes, file names, specifications,
environment variables, repository text, database fields, leases, receipts, model-
looking text, and error text are untrusted until the relevant deterministic check
passes.

The scanner, bounded gateway, evidence normalizer, coordinator adapter, and
verifier are security-critical code. Their exact distribution bytes are pinned in
the execution envelope and evaluation artifact. A file's syntactic form or
apparent source repository does not make it trusted.

## Assets

- operator intent, explicit activation state, pause/cancel/global-stop state, and
  the guarantee that registration alone does not execute;
- project, conversation, non-queueable review anchor, plan, and stage isolation;
- isolation of the dedicated pilot database/key pair from ordinary JARVIS state;
- workspace confidentiality and integrity, including personal identifiers,
  credentials, runtime data, and unlisted files;
- the sealed recipe, specification, file catalog, ruleset, runtime, gateway,
  scanner, report schema, and capability registry;
- leases, retry counters, reservations, observed-usage receipts, checkpoints,
  evidence chains, and terminal state;
- distinction between observed results, deterministic classifications, and
  unsupported inference;
- sidecar integrity material, verifier public-key configuration, and verifier
  private-key separation;
- truthful, redacted local and public reports; and
- availability without bypassing a gate when a file, clock, process, or database
  behaves unexpectedly.

## Adversaries and faults

The design assumes any selected file may be deliberately malicious. It also
accounts for a malformed or malicious specification, an incorrect executor,
operator selection error, concurrent workers, process termination at any boundary,
clock rollback, stale or corrupted state, forged or replayed receipts, path aliases,
symlink/reparse/hard-link substitution, file changes during a read, oversized
inputs, and resource exhaustion.

A same-user attacker who controls both the JARVIS process and its integrity or
verifier keys is outside the protection claim. Compromise of the operating system,
kernel, filesystem implementation, Python runtime, or cryptographic primitives is
also out of scope. Those limits do not permit the implementation to ignore normal
same-user path substitution, unsafe permissions, key co-location, or malformed
state.

## Mandatory invariants

1. **No authority from data.** A specification, file, finding, receipt, error,
   environment value, or model-like string cannot add a handler, stage, rule,
   file, budget, approval, or output field.
2. **One fixed recipe.** Every plan has the same five stages, every stage has
   `mutation_kind: none`, and each explicit advance performs at most one stage.
3. **Positive capability set.** The adapter never instantiates the ambient tool
   registry. Omitted capabilities have no executable dispatch path.
4. **No target mutation or external access.** No workspace write, arbitrary
   process, network, provider, connector, account, screen, desktop, device,
   scheduler, specialist, memory retrieval, or external mutation is reachable.
   Only the three pinned code-owned broker, reviewer, and final-verifier process
   entry points exist.
5. **Internally derived bindings.** Caller-supplied hashes are never authority.
   Project ownership, anchor, canonical records, catalog, recipe, ruleset, runtime,
   and policy bindings are derived and rechecked from live state.
6. **Exact file identity.** Only the normalized, ordered catalog may be opened.
   Containment, regular-file status, identity, link count, size, and digest hold
   before, during, and after every read.
7. **Content is inert.** File bytes enter only deterministic scanners inside the
   sealed broker and independent reviewer. The final signer sees canonical
   evidence only. Bytes are never evaluated, imported, rendered, used as a prompt,
   or interpreted as an instruction, schema, receipt, URL, or path.
8. **No raw-data egress.** Source text and matched values never enter persistent
   state, logs, exceptions, telemetry, serialized verifier requests or responses,
   or public output.
9. **Precharged boundaries.** A durable conservative charge covers every target
   read, scan, canonical-bundle write, worker launch, and verifier session before
   it occurs. Control/pre-reservation logical transitions, including reservation
   creation and emergency stop, use a separate closed class; subsequent attempt
   bookkeeping binds the reservation. The evaluator counts application-level
   transitions, not implementation-dependent SQLite page/WAL/syscall activity.
   Counters never decrease.
10. **Complete provenance.** Each checkpoint binds the execution envelope, lease
    attempt, inputs, receipts, evidence, charged and observed usage, and outcome.
11. **Honest outcomes.** A completed review may report `clean: false`. Invalid or
    drifting input is `rejected`, never a successful checkpoint and never a false
    claim that scanning completed.
12. **Stops dominate leases.** Pause, cancellation, global stop, quarantine,
    closed-reason rejection, drift, and integrity failure invalidate the
    generation-bound single-operation permit and dominate an otherwise valid
    lease. A stop acknowledges only after earlier permits drain or quarantine.
13. **Independent completion.** Completion requires all five valid checkpoints,
    a reproducible normalized attestation, and a receipt from a distinct pinned
    external verifier that holds no executor role.
14. **Fail closed.** Missing, ambiguous, duplicated, stale, inconsistent, or
    unverifiable state prevents the transition and preserves append-only evidence.
15. **Forbidden activity is an incident.** Any unexpected target write, process,
    outbound connection, private-data disclosure, approval use, or omitted-handler
    invocation sets the global stop and prevents completion.

## Threat register

| ID | Threat or failure | Required prevention or response | Exit evidence |
| --- | --- | --- | --- |
| P6-T01 | Unknown fields, duplicate JSON keys, alternate encodings, or schema smuggling widen the specification. | Strict UTF-8 closed-schema parsing; reject duplicate keys, noncanonical values, and unknown fields before registration. | Corpus includes every rejected encoding and field class; zero plans created. |
| P6-T02 | A caller forges Phase 5 goal, contract, constraints, approval, or artifact digests. | Derive bindings from the live project, fixed review anchor, canonical records, and canonical spec; recompute before every boundary. | Forged, stale, cross-plan, and substituted digest cases all reject. |
| P6-T03 | A normal queued task and the Phase 6 adapter both claim the same work. | Create a fixed-label terminal, non-queueable review anchor; never place it on the ordinary worker queue. | Synchronized ordinary-worker/adapter races yield zero ordinary claims. |
| P6-T04 | `readonly` leaves ambient read tools, commands, network, screens, accounts, or private files available. | Do not instantiate the ambient tool registry; construct one positive-allowlist adapter with only the three pinned broker/reviewer/verifier entry points and no dynamic registrations. | Capability inventory and expected process count by worker kind are exact; forbidden-handler canaries have zero invocations. |
| P6-T05 | Workspace text performs prompt injection or masquerades as a command, receipt, status, or policy record. | No model or interpreter; bytes enter only pinned deterministic functions; compare outcomes under instruction-like taint. | Tainted and neutral equivalents produce identical control flow and authority. |
| P6-T06 | A read-only loophole discloses or correlates data through logs, errors, report names, hashes, timing fields, or verifier IPC. | Redacted structured errors; bounded metadata; run-scoped keyed public file identifiers; no public content/spec/catalog/private-bundle hashes; no raw bytes in IPC; capture and canary-scan every JARVIS-owned sink. | Seeded canaries have zero occurrences and derived-value probes reveal no private commitment. |
| P6-T07 | A plan reads another project or an unlisted file. | Exact registered root, project ownership, ordered catalog, and handle-relative open; use the same status/reason/output shape for nonexistent and cross-project identifiers. | Cross-project and adjacent-file probes return zero bytes; no timing-indistinguishability claim is made. |
| P6-T08 | Absolute/UNC/device/alternate-stream/drive-relative/traversal syntax escapes containment. | Reject before normalization; accept only canonical project-relative names from the closed suffix/name allowlist. | Complete Windows path-class matrix rejects before open. |
| P6-T09 | Symlink, junction, reparse point, hard link, case alias, or time-of-check/time-of-use swap substitutes bytes. | No-follow handle open; reject reparse and multiple links; bind volume/file identity; recheck identity, size, and digest throughout every workspace read. | Barrier-controlled substitution trials all return `rejected`; zero substituted bytes classified as valid. |
| P6-T10 | Oversized, sparse, growing, binary, malformed UTF-8, or blocking input exhausts resources or leaks partial data. | Count/size limits before read; bounded streaming in a supervised broker; strict decode; parent deadline and teardown grace; quarantine on an unkillable OS stall; no partial report. | Boundary and simulated-stall corpus returns or quarantines within the tested ceiling; no completion is claimed for an OS-level unkillable stall. |
| P6-T11 | A protected file is explicitly named and therefore treated as public. | Code-owned protected-name and file-kind denylist overrides operator selection. | Every protected-name family rejects before content read. |
| P6-T12 | A prior or unrelated approval is laundered into read, completion, publication, or other authority. | Approval binding explicitly means no authority; no approval gateway calls; no approval fields in schema or receipts. | Logical approval rows/digests are unchanged and approval API calls are zero. |
| P6-T13 | A privacy finding is confused with executor failure, or invalid input is checkpointed as a successful clean review. | `clean` is evidence data; `rejected` uses a closed-reason authenticated rejection/quarantine transition and cannot finalize. | Dirty valid files complete with `clean: false`; invariant violations never complete. |
| P6-T14 | A process crashes before or after a read and retries work without accounting. | Commit the full reservation before the first executor boundary; keep full charge on pre-commit ambiguity and retry under a new lease/attempt/charge; replay a committed checkpoint/cursor transition idempotently without a new charge. | Every defined crash window reconciles exactly with no unreserved boundary, duplicate checkpoint, counter decrease, or post-commit double charge. |
| P6-T15 | The executor underreports elapsed time, reads, bytes, model use, or retries, or coordinator bookkeeping is mistaken for a charged boundary. | Fixed conservative executor charges; a sealed inventory of control/pre-reservation and post-reservation logical transitions; independently observed executor-boundary receipts; model counters fixed at zero. SQLite page/WAL/syscall counts are not denominators. | Exact equality by logical class and charged boundary; every event maps to one class; missing observation fails closed rather than refunding. |
| P6-T16 | Two workers obtain the same stage or create duplicate checkpoints. | Atomic Phase 5 lease and checkpoint transitions; bind worker and attempt; uniqueness and authenticated cursor checks. | Barrier races have exactly one winner and zero duplicate checkpoints. |
| P6-T17 | Pause, cancel, global stop, revoke, or feature disable races with an open/read/verify boundary. | Serialize permits, boundary starts/finishes, and controls on one authenticated monotonic sequence; issue one file per permit; invalidate generations; require dead-channel broker exit; drain/quarantine before acknowledgement. | Barrier tests show zero boundary-start sequence after the control acknowledgement and no false completion. |
| P6-T18 | Database, sidecar, cursor, receipt, rule, runtime, catalog, or system clock is modified or rolled back. | Phase 5 authenticated-state and monotonic-clock checks plus execution-envelope pinning; quarantine on mismatch. | Single-field tamper and rollback corpus has zero accepted transitions. |
| P6-T19 | A trusted executor forges the final attestation or receives verifier private-key material through the supported protocol. | Separate administrative provisioning and pinned verifier process; fixed DPAPI-protected owner-only store; no key path/bytes/handle in component inputs or outputs; authenticated public-authority catalog; domain-separated closed signing schemas; rotation/revocation stop rules. This is logical non-transmission, not same-user OS access denial. | Executor/broker/reviewer inputs, outputs, handles, environment, logs, and state contain zero key paths, bytes, or handles; missing/revoked/wrong/self-signed keys never complete and cannot block stop. A malicious substituted same-user process is explicitly outside the claim. |
| P6-T20 | A verifier signs without opening the real evidence, signs before settlement, or accepts stale/substituted evidence. | One live session opens and normalizes the canonical bundle once, retains that observation state, and returns an unsigned proposal; the coordinator authenticates the proposal record and settlement; the resulting challenge binds bundle, reservation, proposal, and settlement; only then may the same session compare those records with its retained state and sign. | Missing, stale, reordered, replayed, cross-session, pre-settlement, and substituted evidence has zero acceptance across all six crash windows. |
| P6-T21 | Reports differ across runs because of temporary paths, process IDs, locale, wall clock, or map ordering. | Canonical serialization; exclude unstable fields from the claim-bearing attestation payload; pin locale and rule ordering. | Two clean sealed runs produce the same normalized evaluation-attestation hash. |
| P6-T22 | A benchmark, install, config edit, or previous success silently activates or expands the feature. | Authenticated durable default `disabled`; activation is a separate maximum-24-hour promotion bound to exact digests and one pilot project; environment values cannot promote. | Fresh install, upgrade, benchmark, rollback, expiry, and restart remain disabled or return to disabled. |
| P6-T23 | Internal evidence writes escape the private runtime root or overwrite prior evidence. | Fixed internal destination derived from plan/attempt identifiers; no caller output path; create-only, bounded, append-only writes. | Traversal, collision, replay, and disk-full cases never alter workspace or prior receipts. |
| P6-T24 | Rollback loses evidence, resumes work, loses unrelated state, or lets the Phase 5 binary open schema-42 state. | Isolate the candidate evaluation store or production pilot; stop/revoke/quarantine; archive its complete schema-42 root; reinstall the baseline only against the untouched schema-41 shared store; never downgrade/open schema 42 in place. | Candidate state-set rollback matrix and the post-attestation production smoke preserve the archive, advance nothing after acknowledgement, assert exact schema versions, and leave the shared store unchanged. |
| P6-T25 | A UNC, mapped, remote, reparse-backed, offline, or cloud-placeholder project performs network retrieval under a local-read label. | Require and reattest a fixed local drive, stable volume/root identity, non-reparse ancestry, and no offline/recall attributes before permits or opens. | Every nonlocal or indeterminate root rejects before target content is read; captured process-tree outbound connections are zero. |
| P6-T26 | A successful evidence receipt is oversized, malformed, truncated, reordered, or crafted to exhaust report/recovery logic. | Closed bounded receipt schema; per-file/plan/serialized-size/parser caps; MAC and hash chain; atomic create-only write; cap overflow is `rejected`, never partial clean. | Every boundary/cap and malformed receipt has the predeclared result; accepted receipts stay within all caps. |
| P6-T27 | Workspace-controlled imports, working directory, environment, inherited handles, or descendants subvert a sealed worker. | Isolated fixed interpreter/module, private cwd, sanitized environment, enumerated handles, one-process Job Object, memory/deadline limits, and no network/descendants. | Launch-contract tamper corpus has zero module shadowing, credential inheritance, extra handles, descendants, or outbound connections. |
| P6-T28 | A shared JARVIS database is mislabeled as the pilot, auto-migrated, or overwritten during rollback. | A separate `Phase6PilotStore` and migrator alone advance a new authenticated pilot from its schema-41 foundation to 42; ordinary `Memory`/`LongHorizonStore` remain capped at 41 and refuse the pilot; rollback never restores over it. | Shared-store and wrong-marker rejection is 100%; the ordinary schema ceiling remains 41; apart from the declared metadata-only root identity handle, Phase 6 opens no shared database/key/WAL/content handle and preserves all unrelated changes. |
| P6-T29 | The pilot root is swapped, redirected, cloud-backed, or writable by another principal, exposing keys/evidence or redirecting internal writes. | Require fixed-local stable root/ancestor identity, no reparse/cloud attributes, restrictive owner ACL, handle-relative create, and root/store/key reattestation on every command. | Root-swap, junction, ancestry, ACL, owner, and sidecar negatives all stop before state access. |
| P6-T30 | A generic Phase 5 API or CLI mutates a pilot plan outside the evidence wrapper. | The pilot store exposes only a closed evidence facade; generic create/start, claim/renew, reserve, checkpoint, pause/cancel/resume, and verification calls reject schema 42 or a pilot marker. | Every direct API and CLI bypass attempt rejects without a durable state change or boundary permit. |
| P6-T31 | Reusing an identical specification returns stale evidence or silently starts a new run. | Bind a code-owned monotonic run generation into the anchor, all five manifests, envelope, receipts, and reports; active prepare is idempotent; only explicit `--new-run` after a terminal/expired generation increments it. | Active duplicates return one plan; terminal prepare returns the prior result; new-run races produce one next generation; stale-generation evidence never verifies. |
| P6-T32 | A control file, project, or protected root overlaps a pilot, verifier, shared-store, archive, backup, temporary, or peer-project root, or triggers remote/cloud retrieval. | Import a closed protected-root configuration; persist only encrypted locations plus authenticated identity chains/tokens; require the full root-pair matrix and every control file/project to be disjoint, fixed-local, non-reparse, non-cloud, stable, and reattested before read. | Every pair's ancestor/descendant, identity/alias, missing/swap, UNC, mapped, cloud, offline, and recall cases reject before content read. |
| P6-T33 | Public-authority rotation loses historical validation or lets the operator substitute a key during promotion. | Import verified proof-bearing public configurations into an authenticated multi-authority catalog; retain old entries; derive the selected authority/runtime from the signed candidate attestation and forbid raw overrides. | Import replay is idempotent; conflicting IDs, unknown keys, raw overrides, and cross-authority substitutions all reject; historical receipts remain valid. |
| P6-T34 | Requiring production promotion during candidate rollback makes attestation circular, or an evaluation permit is laundered into a pilot. | Candidate rollback uses only the authenticated evaluation-store marker, transient verifier, and fixture-bound permit; production stores reject all three. After final candidate attestation, a separate operator-authorized real-pilot rollback smoke precedes genuine-file use. | Candidate evaluation makes zero production promotion calls; marker/permit/key cross-replays reject; the real-pilot smoke uses the final attestation and actual promote/revoke path. |
| P6-T35 | Reusing ordinary schema bootstrap silently seeds a default project or other shared-runtime state into the pilot, while omitting the Phase 5 project FK makes pilot plans impossible. | Pilot bootstrap is schema-only and proves all content tables empty before writing the marker or migrating to 42. `project-add` atomically creates only a disabled fixed-label/opaque-sentinel `agent_projects` FK anchor plus its closed schema-42 binding; the real root exists only in the encrypted protected registry and binding. | Fresh stores contain zero default rows; transient seed rows prevent initialization; anchor/binding orphan, enable, sentinel, raw-root, FK, and field-tamper cases all reject through generic APIs or recovery. |

## Outcome handling

The evidence result and execution state are intentionally separate:

| Condition | Evidence result | Workflow response |
| --- | --- | --- |
| All pinned rules pass on stable allowed bytes | `clean: true` | May continue toward independently verified completion. |
| Stable allowed bytes contain a deterministic privacy or secret finding | `clean: false` | May continue; completion attests an accurate dirty result and grants no publication authority. |
| File missing before any read | `rejected` with a closed reason | Reject/quarantine the claim; do not complete. |
| Identity, size, or digest changes during or between reads | `changed` | Reject/quarantine the claim; do not interpret changed content. |
| Schema, containment, accounting, receipt, state, stop, or integrity invariant fails | `rejected` with a closed reason | Fail closed, preserve evidence, and stop or quarantine as specified. |
| Verifier evidence is absent or invalid | no completion result | Remain awaiting verification or quarantine; never report complete. |

Reason codes are a closed code-owned enumeration. Error text is constant and may
include only opaque project/plan/stage/file identifiers and bounded numeric facts.
It cannot interpolate file content, matched values, raw paths, environment data, or
exception representations from a lower layer.

## Residual risk and claim limits

The pilot reduces the attack surface by avoiding models, commands, external
services, dynamic tools, and workspace writes. It still depends on the local OS,
filesystem, Python runtime, cryptography, JARVIS data-directory permissions, and
correct implementation of the pinned scanner and gateway. A deterministic rule
can miss an unknown secret format or report a false positive. A local read can
have device- or filesystem-level effects outside the program's visibility. The
evaluation supports only the exact tested platforms, recipe, limits, ruleset, and
runtime digests.

The no-egress measurement covers JARVIS-owned persistence, captured stdout/stderr,
structured errors, IPC, generated reports, and the evaluated process tree. It does
not prove absence from operating-system cache, pagefile, antivirus, backup, access-
time, or crash-dump mechanisms; operators must configure the local host separately.

Phase 6 therefore cannot claim semantic correctness, exhaustive privacy detection,
side-effect-free reads, exactly-once reads, arbitrary-task execution, general
prompt-injection resistance, or protection after same-user process-and-key
compromise. Any request for a model, more files, new suffix, recursive discovery,
new rule source, network access, command execution, workspace mutation, or
automatic advancement is a later phase with a new threat model and exit gate.
