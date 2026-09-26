# Phase 6: deterministic workspace-evidence pilot

Status: design only; implementation and activation are not authorized.

Phase 6 is the smallest safe execution adapter that can exercise the Phase 5
restart-safe coordinator without turning it into a generic autonomous runner. It
is one model-free, prompt-free, read-only recipe for inspecting an operator-named
set of public-source candidate files. It produces deterministic, redacted
integrity and privacy evidence. It does not review semantics, edit files, run
commands, use a provider, or decide what should be published.

The Phase 6 implementation must remain paused until the privacy-sanitized
pre-Phase-6 baseline has been explicitly approved, published through the protected
release path, and validated by remote CI. Preparing or reviewing this design does
not begin implementation.

## User-visible outcome

For one explicit project and one explicit list of files, the operator can obtain:

- a local per-file result of `clean`, `findings`, `changed`, or `rejected`;
- finding classes and locations without the matching source text;
- private local exact content, recipe, ruleset, runtime, and evidence digests;
- redacted aggregate counts suitable for a public evaluation report; and
- an independently signed completion attestation after every invariant passes.

`clean` means only that the pinned deterministic rules found no disallowed
condition in the exact inspected bytes. It is not a semantic code review, a proof
that a file is safe, or permission to publish. `findings` means stable allowed
bytes produced at least one deterministic finding; the review may still complete
honestly with aggregate `clean: false`. `changed` means the pinned identity, size,
or digest drifted and the claimed stage is rejected. `rejected` means an input or
execution invariant failed before a faithful result could be produced. Neither
`changed` nor `rejected` may complete.

## Entry gate

Phase 6 code work may start only when all of the following are true:

1. The sanitized pre-Phase-6 history has replaced the affected public history
   through the protected, operator-approved publication procedure.
2. The published baseline's hosted tests and security analyses pass on Python
   3.11, 3.12, and 3.13.
3. The implementation branch starts from the exact published sanitized commit,
   not from an old release-preparation or phase worktree.
4. This scope, the Phase 6 threat model, and the Phase 6 exit gate are reviewed as
   one change.
5. The feature remains compiled or configured in `disabled` mode by default.

## Closed capability

The only Phase 6 recipe is `workspace-evidence-v1`. Its definition is code-owned,
versioned, sealed into the distribution, and included in every runtime and
evaluation digest. A manifest, file, model, plugin, environment variable, or
operator argument cannot add a stage, handler, rule, capability, or output field.

All Phase 6 plans, review anchors, controls, and evidence live in a dedicated
private pilot data root with its own database and long-horizon sidecar key. Evidence
commands refuse the ordinary shared JARVIS database. The pilot store contains only
explicit pilot project registrations and code-owned review records; it never
copies conversations, tasks, memory, approvals, credentials, or runtime state from
the shared store. This isolation is part of the capability boundary and rollback
contract, not an optional deployment convention.

`pilot-init` accepts only a new or empty private local directory plus one private
`PROTECTED_ROOTS.json` control file, creates the database/key pair, and writes an authenticated
`jarvis.phase6.pilot-store.v1` kind marker with a random store identifier and
default `disabled` mode. It refuses an existing shared JARVIS database or an
unverifiable/nonempty destination. `project-add` validates and privately records
one local fixed-drive project root and returns an opaque project identifier; it
does not enumerate or read target files. Every evidence command requires the
explicit pilot root and verifies the kind marker under its sidecar key. A path
naming convention or environment variable is not proof that a store is a pilot.
Neither command prints the host data/root path.

`PROTECTED_ROOTS.json` is a closed, maximum-16-KiB local control file containing
only schema ID `jarvis.phase6.protected-roots.v1`, one ordinary shared-store root,
one archive root, zero to eight designated backup roots, and one to four temporary
roots. Duplicate roots/fields and unknown fields reject. The private file must stay
outside the repository. The verifier-store root is derived from its fixed known-folder
location and the pilot root is the explicit destination. `pilot-init` opens only
root-directory metadata handles, proves every root local and stable, rejects every
ancestor/descendant, identity, or alias overlap in the full pairwise matrix, and
stores an authenticated protected-root catalog containing roles, stable identity
chains, keyed component tokens, and pilot-key-encrypted canonical locations—never
plaintext paths. Every command decrypts the private catalog, reattests every root
and pairwise relationship, and fails before state or target content access on
drift. It never opens the ordinary shared database, sidecar key, or content files.
Unconfigured operating-system backup/cache/pagefile behavior remains outside the
claim.

Phase 6 uses a new `Phase6PilotStore` with its own constructor, migrator, and
version ceiling. It does **not** raise `Memory.SCHEMA_VERSION`: ordinary `Memory`
and `LongHorizonStore` remain capped at schema 41, never auto-migrate a shared
store to 42, and refuse a schema-42 database or pilot kind marker. `pilot-init`
alone may bootstrap the schema-41 foundation in a new empty database, create and
authenticate the pilot kind marker, and then invoke the pilot-only migrator to
advance that marked database to schema 42. After initialization,
`Phase6PilotStore` refuses schema 41, an absent or wrong marker, and every version
other than 42. No generic constructor may perform this transition. Its schema-41
bootstrap is schema-only: it must not create the ordinary default project, agent,
conversation, task, memory, or configuration rows. If the implementation reuses a
foundation migration that transiently seeds defaults, those rows must be removed
and all content tables proved empty in the same exclusive initialization
transaction before the marker or schema-42 state exists.

The pilot store exposes a closed evidence-specific facade rather than the generic
Phase 5 plan API. Generic plan creation/start, claim or lease renewal,
reservation, checkpoint, pause, cancel, resume, and final-verification entry
points are unavailable through that facade. The ordinary Phase 5 CLI and direct
`Memory`/`LongHorizonStore` calls reject a pilot root or pilot-bound identifier;
only the dedicated evidence commands may invoke schema-42 transitions. Direct
API and CLI bypass attempts are mandatory negative tests.

The pilot root itself must be on a fixed local drive with stable volume/root
identity, no symlink/junction/reparse/cloud/offline ancestor, and an ACL owned by
the current operator that grants no untrusted principal write access. Creation and
later writes are handle-relative. Every command reattests the root identity,
store ID, marker MAC, and ACL before touching state; a swap, permissive ACL, or
indeterminate attribute asserts stop. The same rules protect the sidecar key and
private evidence subdirectory.

`project-add` is the only project-creation path. In one transaction it inserts an
inert Phase 5 compatibility row in `agent_projects` and one closed
`jarvis.phase6.project-binding.v1` record. The compatibility row is disabled, uses
a fixed label `Phase 6 inert project` and the single-component code-owned sentinel
`__jarvis_phase6_inert_<32-lowercase-hex>__` in `relative_path`, and exists
only to satisfy the existing plan/conversation/task foreign keys; it never stores
or resolves the real root. Normal project and task APIs cannot enable, resolve, or
use it. The schema-42 binding maps its numeric foreign key to an opaque project ID,
encrypted root-registry reference, stable volume/root/ancestor identities, keyed
canonical component tokens, protected-root-catalog digest, creation generation,
active flag, and record MAC. It contains no plaintext path or caller-supplied
digest. The pair is inseparable and orphan rows fail initialization and recovery.
The project root and all of its ancestors must be disjoint by identity and ancestry
from every protected root and existing pilot project. An identical root
registration is idempotent; an alias, overlap, or conflicting identity rejects.

The planned operator surface is deliberately manual:

```text
jarvis-evidence-verifier provision
jarvis-evidence-verifier attest-evaluation --authority AUTHORITY_ID --report EVALUATION_REPORT
jarvis-evidence-verifier revoke AUTHORITY_ID
jarvis workflow evidence pilot-init --pilot-data PILOT_DATA --roots PROTECTED_ROOTS.json
jarvis workflow evidence authority-add --pilot-data PILOT_DATA --public-config PUBLIC_AUTHORITY.json
jarvis workflow evidence project-add --pilot-data PILOT_DATA --root PROJECT_ROOT
jarvis workflow evidence project-list --pilot-data PILOT_DATA
jarvis workflow evidence plan-list --pilot-data PILOT_DATA --project PROJECT_ID
jarvis workflow evidence show --pilot-data PILOT_DATA --project PROJECT_ID PLAN_ID
jarvis workflow evidence status --pilot-data PILOT_DATA --project PROJECT_ID PLAN_ID
jarvis workflow evidence mode --pilot-data PILOT_DATA --set disabled|preview
jarvis workflow evidence preview --pilot-data PILOT_DATA --project PROJECT_ID --spec SPEC.json
jarvis workflow evidence promote --pilot-data PILOT_DATA --project PROJECT_ID --promotion PROMOTION.json --attestation CANDIDATE_ATTESTATION.json
jarvis workflow evidence revoke --pilot-data PILOT_DATA --project PROJECT_ID
jarvis workflow evidence stop --pilot-data PILOT_DATA
jarvis workflow evidence prepare --pilot-data PILOT_DATA --project PROJECT_ID --spec SPEC.json [--new-run]
jarvis workflow evidence advance --pilot-data PILOT_DATA --project PROJECT_ID --spec SPEC.json PLAN_ID
jarvis workflow evidence pause --pilot-data PILOT_DATA --project PROJECT_ID PLAN_ID
jarvis workflow evidence resume --pilot-data PILOT_DATA --project PROJECT_ID --spec SPEC.json PLAN_ID
jarvis workflow evidence cancel --pilot-data PILOT_DATA --project PROJECT_ID PLAN_ID
jarvis workflow evidence verify --pilot-data PILOT_DATA --project PROJECT_ID --spec SPEC.json PLAN_ID
jarvis workflow evidence report --pilot-data PILOT_DATA --project PROJECT_ID --spec SPEC.json PLAN_ID
```

`prepare` validates and registers state but reads no target-file content. Each
`advance` invocation may advance at most one stage. `report` is read-only and
cannot advance or complete a plan. The exact specification file must be supplied
for `prepare`, `preview`, `advance`, `resume`, `verify`, and `report`; its canonical
digest and every live binding are recomputed each time. Authority-reducing pause,
cancel, revoke, and emergency stop are the only spec-free transitions. There is no
automatic loop, background worker, generic `workflow run`, callback, arbitrary
recipe, or model-generated dispatch.

`verify` is available only after five valid checkpoints. One explicit invocation
uses this ordered two-phase live-verifier protocol: (1) durably reserve the full
final-verification charge; (2) start one live verifier observation session, which
reopens canonical evidence and returns an unsigned observation-proposal digest,
then append that proposal as an authenticated pilot record; (3) validate the
operating-system observations and append an authenticated settlement that binds
that proposal; (4) construct the Phase 5 completion
challenge whose evidence digest binds the bundle, reservation, settlement, and
proposal; (5) have the same live verifier recheck those records and sign the
private completion challenge and, when requested, the detached public-redacted
payload; and (6) ingest the receipts. A crash or failure never auto-retries: the
plan remains incomplete, the full attempt stays charged, and any pre-commit retry
requires a new chained full reservation. Replay after a committed completion is
idempotent. `mode`, `promote`, `revoke`, and `stop` are raw authenticated
control-plane operations described below; none invokes a model.

The generic Phase 5 API and CLI must refuse every schema-42 Phase 6 plan even when
called directly. Only the closed pilot facade may create, start, claim, renew,
reserve, checkpoint, pause, cancel, resume, or verify one. `evidence resume` may
reactivate a plan only after re-reading the exact specification and revalidating
the promotion, control epoch, bindings, charges, and evidence chain. Dedicated
pause, cancel, revoke, and stop operations reduce authority and invalidate
boundary permits; they do not expose the generic mutator surface.

The specification itself follows the Phase 5 manifest-ingestion boundary: one
regular, non-link, UTF-8 JSON control-plane file no larger than 64 KiB. Its host
path must be outside the registered project and every protected root and is never
persisted or printed. A dedicated stable reader rejects reparse points, symlinks,
hard links, non-regular files, identity/size changes, invalid UTF-8, and duplicate
JSON keys before returning canonical bytes. `prepare` therefore performs no
target-file content read, rather than no filesystem read at all. Bounded
specification ingestion and internal coordinator/database I/O are control-plane
bookkeeping, not executor tool use; their stable-reader calls and authenticated
logical transitions are nevertheless counted in separate closed evaluator
classes. SQLite page, WAL-frame, and filesystem-syscall counts are implementation
details, not security denominators. The charged
usage claim begins at the target-workspace and verifier boundaries after
successful registration.

Every control file named by this design—`PROTECTED_ROOTS.json`, `SPEC.json`,
`PROMOTION.json`, `CANDIDATE_ATTESTATION.json`, `PUBLIC_AUTHORITY.json`, and the
administrative `EVALUATION_REPORT`—must be a regular single-link UTF-8 file on a
fixed local drive
with stable volume identity, non-reparse ancestry, and no offline, recall, mapped,
UNC, remote, or cloud-placeholder attribute. Each must be pairwise disjoint from
the project, pilot, ordinary shared-store, verifier-store, archive, backup, and
temporary roots. `project-add` and every later command reattest that no protected
root is an ancestor or descendant of the project or a control file. Indeterminate
locality, identity, or overlap fails before content is read.

The specification is a closed UTF-8 JSON object containing only:

- schema identifier `jarvis.phase6.workspace-evidence-spec.v1`;
- recipe identifier `workspace-evidence-v1`;
- exact pilot-project identifier;
- an ordered list of normalized project-relative file names; and
- the operator-selected report visibility, either `local` or `public-redacted`.

Unknown and duplicate fields are rejected. The specification contains no prompt,
command, URL, credential, module, callback, output path, approval token, raw hash,
budget override, rule override, or executable value.

`prepare` creates one code-owned empty review conversation and task anchor because
Phase 5 plans require both bindings. The anchor uses a fixed public label rather
than an operator- or model-authored prompt, is born terminal and non-queueable, is
bound to the exact pilot project, and can never be claimed by the ordinary task
worker. Its immutable record is the source of the goal binding; the operator
cannot supply a conversation, task, prompt, or substitute goal digest.

The schema-42 helper allocates a code-owned, authenticated, monotonically
increasing run generation. The first `prepare` for a canonical specification uses
generation 1. While that generation is active and nonterminal, another identical
`prepare` is idempotent and returns the same plan. After completion, rejection, or
expiry, `prepare` without `--new-run` returns the prior terminal result and never
silently starts a fresh inspection. An explicit `--new-run` is accepted only when
the prior generation is terminal or expired; it atomically increments the
generation and creates a new empty conversation, terminal task, review anchor, and
plan. `--new-run` while a generation is active rejects.

The generation is bound into the anchor, goal, plan, all five Phase 5 manifest
digests, execution envelope, receipts, evidence, and reports. The creation
transaction prevents normal APIs from adding content or updating, scheduling,
claiming, reparenting, or deleting its anchor rows. Every later Phase 6 transition
recomputes the bound fields, generation, and anchor MAC/digest. A mismatch
quarantines the plan. This pilot helper is the only permitted exception to the
ordinary conversation/task creation paths.

## Fixed five-stage plan

Every plan has exactly these stages and every stage has `mutation_kind: none`:

| Ordinal | Stage | Type | Permitted work |
| --- | --- | --- | --- |
| 1 | `binding` | `inspect` | Recompute the canonical project, task, recipe, runtime, ruleset, and specification bindings. |
| 2 | `snapshot` | `inspect` | Use the sealed killable I/O broker to open each allowed file through the bounded gateway and record its identity, size, and SHA-256 digest. |
| 3 | `privacy_scan` | `verify` | Use the sealed broker to reopen the same identities, verify no drift, and apply only the pinned deterministic privacy and integrity rules. |
| 4 | `evidence_review` | `verify` | In a separate model-free process, reopen the pinned files, repeat the deterministic scan, and compare the independently normalized result and receipt chain. |
| 5 | `finalize` | `finalize` | Assemble the unsigned, normalized attestation and verify that every fail-closed condition is satisfied. |

After all five checkpoints, Phase 5's existing completion transition still
requires a pinned external Ed25519 verifier. The final verifier is not a sixth
executor stage and must be distinct from every stage executor. Its private key is
never transmitted to or used by a supported executor through the sealed protocol
or state, and is not stored in the repository, workflow database, specification,
evidence bundle, log, or report.

The final verifier receives the exact specification and authenticated evidence
references, reopens the real canonical evidence bundle, reconstructs the
normalized attestation, and validates the independent stage-4 comparison. Stage
4, not the key-holding final signer, performs the independent second workspace read
and scan. Raw workspace bytes are never serialized into an interprocess verifier
request or response.

After provisioning, the verifier accepts exactly three domain-separated closed
signing schemas: the existing
`jarvis.long-horizon.final-verification-challenge.v1` in the live runtime session,
`jarvis.phase6.public-report-envelope.v1` in that same session when a public report
was selected, and `jarvis.phase6.candidate-evaluation.v1` only in the
administrative `attest-evaluation` mode. Provisioning itself additionally signs
only `jarvis.phase6.authority-provision-proof.v1`. Each parser rejects unknown or
duplicate fields and cross-domain replay; no command exposes a generic signing
operation. Runtime completion and public-report signatures cannot authorize
candidate evaluation, and an evaluation signature cannot activate a pilot.

## Verifier key boundary

`jarvis-evidence-verifier provision` is a separate raw administrative entry point,
not an executor tool. It generates a fresh operational Ed25519 key inside a fixed
per-user verifier store obtained from the operating-system known-folder API. The
store is local-fixed-drive, non-reparse, owner-only, and outside the pilot,
workspace, repository, environment-selected paths, and their backups. The private
key is DPAPI-protected at rest in a create-only, regular, single-link file; among
the supported pinned components, only the verifier process decrypts it.
Provisioning emits only an authority ID and
public configuration plus a proof-of-possession signature over the closed
provisioning challenge.

The coordinator launches the verifier without a key path or key bytes in
arguments, environment, inherited handles, IPC, database, evidence, or
specification. The verifier derives its fixed store internally, reattests
identity/ACL, and loads only the key matching the pinned authority. The trusted,
pinned broker, reviewer, coordinator adapter, and evaluator are designed never to
receive or transmit that path, key bytes, or key handle. Owner-only ACL and
user-scoped DPAPI protect the blob at rest from other principals; they do **not**
isolate it from malicious code already running as the same operator. The supported
boundary is therefore logical non-transmission plus pinned-process separation, not
an operating-system access denial between sibling same-user processes. A same-user
attacker that substitutes those components or locates and decrypts the blob is
outside the protection claim.

`PUBLIC_AUTHORITY.json` is a closed public configuration produced by provisioning.
It contains only its schema/version, authority ID, verifier-runtime ID, Ed25519
public key, creation generation, and provisioning proof-of-possession. The
idempotent `authority-add` command stable-reads and verifies it, then retains it in
the pilot's authenticated public-authority catalog, which begins empty at
`pilot-init`. The catalog may hold multiple
authorities so historical receipts remain verifiable after rotation. Every
evidence read opens the pilot store with the complete configured catalog; an
unknown, conflicting, or unverifiable authority fails closed. Emergency stop does
not depend on any verifier key.

Rotation provisions and imports a new authority and requires a new promotion;
existing receipts retain their old public key for validation. A signed candidate
attestation selects the exact authority and runtime used by promotion; the
operator cannot supply a raw authority override. Old public configurations remain
read-only and may not be replaced by the same ID. The separate
`jarvis-evidence-verifier revoke` command appends an idempotent authority-revocation
receipt in the verifier store and prevents new signatures; it is distinct from
project-promotion `workflow evidence revoke`. A verifier response indicating
revocation makes the coordinator assert pilot stop, while emergency `evidence
stop` remains independently available. Private key files are never automatically
deleted; recoverable deletion requires a later explicit operator action after no
live promotion or plan references the authority. A missing, changed, locked, or
revoked key leaves a plan honestly incomplete. Synthetic deterministic derivation
exists only in an ephemeral marked evaluation store and can never provision an
operational key.

## File boundary

The recipe accepts between 1 and 32 files. Each file is limited to 256 KiB and the
combined declared and observed size is limited to 2 MiB. Files must be regular,
non-symlink, non-reparse-point, single-link, valid UTF-8 files beneath the exact
registered project root.

The project root must be on a local fixed drive. Registration and every stage bind
the volume and root identity and reject UNC paths, mapped or remote drives, device
paths, reparse-backed roots or ancestors, and cloud/offline/recall-on-access
placeholders. A project whose locality cannot be proved is unavailable. This is
the basis for the no-network claim; selecting a path never authorizes retrieval
from a share or cloud filesystem.

One plan expires 24 hours after registration. A stage lease is at most 60 seconds.
Each stage permits at most one charged retry and the plan permits at most five;
there is no automatic retry. Expiry, retry exhaustion, or a boundary that cannot
finish within its fixed deadline fails closed.

The initial suffix allowlist is:

```text
.css .html .ini .js .json .md .ps1 .py .toml .ts .txt .xml .yaml .yml
```

The only suffix-free names are `LICENSE` and `NOTICE`. The only dotfiles are
`.gitattributes` and `.gitignore`. Matching is case-normalized and collisions are
rejected. Expanding this list is a new reviewed capability change, not a data or
configuration update.

The gateway rejects:

- absolute, UNC, device, alternate-data-stream, drive-relative, empty, dot, and
  parent-traversal paths;
- non-NFC text; backslashes; control characters; trailing dots/spaces; Windows
  reserved device basenames; and components that change after canonical
  normalization;
- recursive discovery, directories, globs, aliases, case collisions, and
  duplicate identities;
- symlinks, junctions, reparse points, hard links, sockets, pipes, devices, and
  other non-regular inputs;
- protected or private names such as environment files, credentials, keys,
  cookies, runtime databases, logs, screenshots, conversations, and generated
  user data;
- binary or invalid UTF-8 content, oversized files, and any file whose identity,
  size, or digest changes during an operation.

Containment is checked before opening and again against the opened handle. File
identity, link count, size, and digest are checked before, during, and after each
bounded read. Snapshot and scan are separate reads; a difference is `changed` and
rejects the claimed stage without interpreting the changed content.

The canonical catalog uses `/` separators and NFC components. Case collisions are
tested with the Windows ordinal case-insensitive comparison, not locale rules.
The long name returned for the opened handle must match the requested canonical
name, preventing 8.3 or other alias acceptance, and no two entries may resolve to
the same volume/file identity.

No stage may enumerate the project, follow an import, open a referenced file, or
read a file that was not named in the sealed specification.

Stages 2 and 3 run file operations in a pinned minimal broker process so the
coordinator can terminate and quarantine a cooperative or simulated blocking read
at the charged deadline. The broker receives only fixed mode, inherited bounded
handles/control bytes, and one permitted catalog entry at a time; it returns only identity,
digest, observed-count, and redacted-finding records. An operating-system or
filesystem failure that prevents process termination is outside the latency claim:
the coordinator must stop issuing permits and report unavailable, not claim timely
completion.

## Deterministic rules and output

Phase 6 requires a new packaged scanner with a closed class-and-line result schema;
the existing boolean redaction helpers and public-release privacy scanner are not
silently repurposed as this engine. The scanner operates only on bytes returned by
the bounded gateway. The initial ruleset checks:

- strict UTF-8 for every file, duplicate-key JSON validity for `.json`, and
  standard-library TOML validity for `.toml`; other suffixes receive text checks
  only and make no syntax-validity claim;
- identity, length, and digest drift;
- public-release personal-identifier and local-path patterns;
- secret, credential, token, private-key, cookie, and private-URL patterns; and
- runtime, generated-data, conversation, screenshot, log, database, and device or
  network identifier patterns.

A logical line is limited to 64 KiB. JSON nesting is limited to 64 and 50,000
nodes; TOML is limited to 10,000 keys/tables. Code-owned line matchers prohibit
backreferences and ambiguous nested quantifiers and run only inside the supervised
worker. The finding and serialized-evidence caps below are part of the ruleset.
Any cap or worker deadline is `rejected`, not a partial clean result.

Workspace content is untrusted data, including text that resembles instructions,
tool calls, schemas, receipts, status records, or Markdown. It is never parsed as
a request and cannot select rules or control flow. The recipe invokes no model, so
prompt and completion token use is exactly zero.

The private local report may display the validated project-relative name, exact
content digest, finding class, and line number. It must never display the matched
value or surrounding source text. A public-redacted report replaces names with
run-scoped keyed opaque file identifiers, omits every per-file content digest, and
contains only bounded counts, statuses, component pins, and one aggregate
attestation digest. Raw file bytes and match values are never placed in the
database, evidence store, logs, exceptions, telemetry, serialized verifier request
or response, or public output.

The public aggregate digest commits only to a canonical
`jarvis.phase6.public-report-payload.v1` projection containing the redacted counts,
statuses, public component pins, run-scoped opaque identifiers, and bounded public
provenance. The projection excludes `report_sha256`, all signatures, receipt
fields, and envelope metadata, so it is not self-referential. A detached
`jarvis.phase6.public-report-envelope.v1` contains that projection, its SHA-256,
the public authority/runtime identifiers, and the domain-separated verifier
signature. It is not a hash of the private evidence bundle, specification,
catalog, file identities, or content hashes. Exact include/exclude projection and
one-field substitution tests are part of the gate. The private completion receipt
is not exported as the public report.

## Authority and binding

Caller-supplied Phase 5 manifest digests are claims, not authority. `prepare`
derives a canonical execution envelope from live project, conversation, and task
records plus the exact sealed recipe and specification. Every later operation
recomputes those bindings and rejects any mismatch before acquiring a file handle.

The five existing manifest digest fields have one exact mapping; `prepare` fills
them and accepts no caller values:

| Phase 5 field | Canonical Phase 6 payload |
| --- | --- |
| `goal_sha256` | Schema, pilot project, run generation, generated empty conversation, review-anchor identity, and the fixed code-owned review label. |
| `contract_sha256` | Run generation, recipe identifier, exact ordered five-stage graph, outcome schema, and completion semantics. |
| `constraints_sha256` | Run generation, file policy, limits, charge schedule, capability set, feature mode/promotion, ruleset, gateway, scanner, reviewer, verifier, and runtime digests. |
| `approval_scope_sha256` | Run generation and a fixed closed object declaring `authority: none`, an empty approval set, and no mutation or publication permission. |
| `artifact_set_sha256` | Run generation, canonical specification digest, and the exact ordered normalized target-file catalog. |

Each payload has its own versioned schema identifier and canonical JSON encoding.
The execution-envelope digest binds all five payloads together with the plan,
checkpoint head, control epoch, and public-authority configuration. Hashing a
caller-provided digest string is not a valid derivation.

The envelope binds at least:

- project, conversation, and task identities and current project ownership;
- canonical goal, contract, constraint, and artifact-set records;
- the exact ordered file catalog and its canonical specification digest;
- recipe, ruleset, scanner, gateway, report-schema, coordinator, and runtime
  digests; and
- feature mode, stop epoch, plan cursor, lease attempt, and executor identity.

An approval-scope digest records only that the recipe has no approval authority.
Historical approvals cannot be consumed, inherited, refreshed, or interpreted as
permission. A valid lease coordinates one attempt; it is never continuing
authority. Pause, cancellation, global stop, quarantine, binding drift, and
integrity failure dominate a lease and are rechecked immediately before every
workspace or verifier boundary.

An ordinary privacy finding is not an executor failure: the deterministic review
may complete with `clean: false`, and the verifier attests that the finding was
faithfully produced. Invalid input, identity drift, containment failure, receipt
tampering, or another invariant violation is instead `rejected`; it cannot be
checkpointed as a successful review. Phase 6 must add a narrow authenticated
claimed-stage rejection/quarantine transition with a closed reason code and an
evidence digest because Phase 5 does not currently expose that adapter-facing
transition. The transition records no raw content and grants no generic status
mutation API.

Phase 6 requires one reviewed additive migration owned only by the
`Phase6PilotStore` migrator from its newly bootstrapped schema-41 foundation to
schema 42. The existing Phase 5 API has no durable reservation after the fifth checkpoint, closed adapter-facing
rejection transition, immutable review anchor, feature promotion/epoch state, or
boundary-permit interlock. Schema 42 adds authenticated Phase 6 control and
promotion records, protected-root catalogs, project bindings, review anchors,
plan bindings, single-boundary permits,
successful evidence receipts, claimed-stage rejection receipts, and final-
verification reservation/proposal/settlement receipts. The append-only records
store only
closed reason codes, identities, digests, fixed charges, observed counts,
expiry/generation values, bounded redacted findings, and hash-chain links; they
store no raw path or content. Existing Phase 5 records are not rewritten. Any
other schema change is outside this design.

Every successful stage writes one authenticated create-only pilot-store evidence receipt
before its checkpoint, and the checkpoint artifact digest binds that exact receipt.
Receipts are hash-chained per plan and bind the execution envelope, stage, attempt,
reservation, observed usage, ordered file indexes/opaque identifiers, private
content digests, outcome, finding class/line/count records, and previous receipt.
They are limited to 64 location records per file, 512 per plan, 128 KiB per stage,
and 512 KiB per bundle. Exceeding a finding, parser, report, or evidence cap is
`rejected` with a closed resource-limit reason; it is never truncated into an
apparently complete result. Recovery validates every MAC, hash, order, cap, and
checkpoint binding before use. Local paths are reconstructed transiently from the
re-supplied specification.

## Capability boundary

Phase 6 must not instantiate the ambient tool registry. Its executor is a new,
minimal adapter with a positive allowlist containing only:

1. authenticated Phase 5 state transitions;
2. the bounded project-file gateway;
3. the deterministic scanner and normalizer;
4. append-only evidence writes beneath the private JARVIS runtime-data root; and
5. the three exact code-owned process entry points for the stage-2/3 I/O broker,
   stage-4 reviewer, and final verifier.

Those workers launch only pinned package entry points with closed arguments; they
accept no executable path or command from the specification, file content,
environment, or operator. The adapter has no route to a shell, arbitrary process,
dynamic import, network, web, provider, connector, account, screen, desktop,
clipboard, microphone, camera, device, scheduler, specialist, memory retrieval,
arbitrary database query, external mutation, or workspace write. Environment and
configuration cannot add handlers. An attempted invocation of any omitted
capability is an integrity incident, sets the global stop, and prevents completion.

Workers use the fixed installed interpreter in isolated mode and fixed installed
module entry points, with a private non-workspace working directory and a minimal
environment allowlist that excludes module paths, proxies, credentials, and
provider configuration. The launcher passes no workspace-controlled argument,
inherits only explicitly listed control/file handles, and places each worker in a
kill-on-close Windows Job Object with one active process, no descendants, a
256 MiB memory ceiling, the declared operation deadline, and a 5-second teardown
grace. The workspace is never the import path or working directory. Network APIs
remain denied. Evaluation harness processes are test infrastructure and are not
included in the production adapter's capability set.

## Resource accounting

Phase 5 requires an exact reservation before a checkpoint but cannot know actual
elapsed time in advance. Phase 6 therefore uses conservative, deterministic
charges and labels them as charges, never as observed use:

- the full stage charge is durably reserved before its first charged executor
  boundary;
- `model_calls`, `prompt_tokens`, and `completion_tokens` are always zero;
- `tool_calls` equals the maximum physical reads or verification boundaries
  allowed for that exact file count and stage;
- the stage's fixed elapsed-time ceiling is charged in full, even if the attempt
  exits early; and
- actual bytes, physical operations, and elapsed milliseconds are separately
  recorded as bounded observations in the authenticated receipt.

For `F` registered files, the initial fixed charge schedule is:

| Boundary | Charged tool calls | Charged elapsed ceiling |
| --- | --- | --- |
| `binding` | 0 | 5 seconds |
| `snapshot` | `F + 1` | 30 seconds |
| `privacy_scan` | `F + 1` | 30 seconds |
| `evidence_review` | `F + 1` | 30 seconds |
| `finalize` | 1 | 5 seconds |
| final-verifier invocation | 2 in the verification-charge ledger | 15 seconds |

Each workspace file open-and-bounded-read is one charged call. The extra call in
stages 2, 3, and 4 is the exact pinned worker-process boundary. `finalize` covers
the canonical evidence-bundle write. The final-verifier charge covers one bundle
read and one signer-process boundary. A ruleset implementation may do less work
but may not add a physical boundary. Changing this schedule is a reviewed recipe
version change.

Accounting has exactly three disjoint classes:

1. **Control/pre-reservation.** Stable control-file reads and authenticated logical
   transactions for bootstrap, project/authority registration, discovery, mode,
   preview, prepare, pause/cancel/resume, revoke/stop, and reservation creation do
   not require a prior reservation and are not executor `tool_calls`.
2. **Post-reservation bookkeeping.** Permit, successful/rejection evidence,
   checkpoint, proposal, settlement, usage-head, and terminal transitions belong to
   the exact reserved attempt but are not recursively charged as executor calls.
3. **Charged executor boundaries.** Target-file reads, pinned worker/session
   launches, the create-only canonical-bundle write, and verifier bundle read are
   the `tool_calls` in the fixed table above.

The sealed operation inventory maps every command/state outcome to an exact number
of logical stable-reader and authenticated-transition events; each event is one
application-level committed transition regardless of SQLite pages, WAL frames, or
system calls. Read-only commands commit zero transitions. Rejected or incident
outcomes may commit only their single predeclared rejection/stop transition.
Reservation creation is itself a class-1 transition and need only commit before
the first class-3 boundary; later attempt records are class 2. The evaluator
compares this logical inventory and the separately observed class-3 boundaries.
Anything outside the three classes is an incident.

The post-stage final-verifier boundary uses the schema-42 append-only reservation
created before the live verifier starts. The reservation binds the project, plan,
run generation, execution envelope, checkpoint head, authority, verifier runtime,
fixed charge, attempt, and previous receipt. The verifier returns an unsigned
observation proposal; the coordinator appends it as an authenticated pilot record,
then validates operating-system observations and appends a settlement binding that
proposal; only then does it construct the
completion challenge binding bundle, reservation, proposal, and settlement. The
same live session compares those authenticated objects with its retained
observation state before signing; it does not perform a second bundle read. A
crashed, missing, or failed proposal or settlement keeps the full charge. Each run
generation permits at most two final-verification attempts (the initial attempt
plus one explicit retry); every retry requires a new chained full reservation, and
exhaustion leaves the plan incomplete and quarantined. Phase 6 completion is
impossible without the matching authenticated reservation, proposal, settlement,
and receipt. The schema-42 completion wrapper also rechecks global running state,
plan state, promotion, control epoch, boundary permit, authority, and evidence
immediately before the closed pilot facade invokes the Phase 5-compatible terminal
transition; direct generic API use cannot bypass those checks.

A stage is cancelled before exceeding its charged ceiling. Missing, ambiguous, or
crashed settlement keeps the full charge; counters never decrease and no refund
is issued. A retry is a new attempt with a new full reservation. The pilot claims
accounted retries, not exactly-once reads. No unreserved target-workspace read,
scan, verifier call, or execution evidence write is allowed.

The sealed evaluator must verify both the Phase 5 charges and the Phase 6 observed
dimensions independently. A later model-backed phase would require a separately
reviewed allowance-and-settlement protocol; Phase 6 does not create one.

## Feature modes and rollback

The only feature modes are:

- `disabled`: reject prepare and advance; permit authenticated status/report reads;
- `preview`: validate a specification and show bounded metadata without creating
  a plan or reading workspace files; and
- `manual`: permit one operator-requested stage per `advance` invocation.

`mode --set preview` and `mode --set disabled` are the only direct mode changes;
`manual` can be reached only by a valid `promote` receipt. Setting `disabled` is
authority-reducing and invalidates permits. Setting `preview` after a global stop
is forbidden; rollback or a separately reviewed recovery action is required.

The default is `disabled`. No install, upgrade, benchmark, passing test, workflow
registration, or previous activation changes the mode. Promotion to `manual`
requires a separate operator action bound to the exact runtime, recipe, ruleset,
one pilot project, and expiry.

Mode is authenticated durable schema-42 state, not an environment-only flag. A
`promote` receipt binds the exact pilot project, implementation, recipe, ruleset,
scanner, broker/reviewer/verifier runtimes, verifier public authority, evaluation
digest, current control epoch, and an expiry no later than 24 hours. It cannot bind
a plan execution envelope that does not exist yet; each later plan envelope instead
binds the exact promotion receipt identifier and digest. `revoke` invalidates that
project's promotion; `stop` invalidates all promotions and increments a monotonic
control epoch. Every dedicated evidence command in the isolated pilot environment,
including `project-list`, `plan-list`, `show`, and `status`, loads the authenticated
public-authority catalog through `Phase6PilotStore` so completed receipts remain
verifiable after restart. The ordinary Phase 5 commands refuse schema 42. A
missing or changed authority referenced by any promotion, plan, or receipt fails
closed with constant redacted output; an empty catalog is valid only before such
state exists and cannot enter `manual` mode.

`PROMOTION.json` is one stable-read regular UTF-8 JSON file no larger than 16 KiB.
Its closed schema contains only its schema/version, the exact pilot-project ID, a
passed candidate-evaluation attestation ID and SHA-256, and a requested duration in
seconds no greater than 86,400. It contains no implementation pin, key, approval,
command, path, plan, or authority field. `promote` reopens the installed sealed
artifacts and derives every component pin itself, verifies that the attestation
matches them and passed the exit gate, and records the operator action under the
current epoch. `activation_authorized: false` in the benchmark remains true: the
benchmark cannot call production `promote`, target a pilot store, or self-activate.
An identical replay in the same epoch is idempotent; replay after revoke, expiry,
component change, or epoch change is rejected. Clock rollback asserts stop rather
than extending a promotion.

`CANDIDATE_ATTESTATION.json` is a closed public-redacted document no larger than
128 KiB. After the full source, wheel, hosted, and rollback gates pass, the separate
verifier's administrative `attest-evaluation --authority AUTHORITY_ID` mode
requires one exact imported, nonrevoked authority ID, recomputes the canonical core
and component pins from the report and installed artifacts, checks every hard
threshold, and signs a candidate-evaluation payload that embeds that authority and
runtime ID and still contains
`activation_authorized: false`. `promote` stable-reads that document, verifies its
pinned public-authority signature and exact ID/hash match, then repeats the
component comparisons. The signature proves evaluation integrity; only the later
explicit operator `promote` grants the bounded pilot activation.

To avoid a circular attestation dependency, the sealed evaluator uses a distinct
domain-separated `jarvis.phase6.evaluation-store.v1` marker and evaluation-only
promotion permit. The permit binds the sealed fixture/evaluator/design digests and
temporary synthetic project roots, exercises the same downstream epoch, permit,
stage, and revocation code, and is rejected by every pilot store. It cannot name an
operator project or survive the ephemeral evaluation root. The resulting candidate
attestation still says `activation_authorized: false`; only a later raw operator
`promote` with a passing candidate attestation can activate one pilot project.

The evaluation store is bootstrapped from an empty schema-41 foundation through
the same sealed schema-42 tables and migration logic, but its authenticated marker,
integrity key, transient verifier authority, and constructor are evaluation-only.
The candidate rollback matrix runs against that disposable store and its
evaluation permit, then archives it exactly as specified below. A marker or permit
replay into `Phase6PilotStore` rejects. Thus candidate rollback evidence exists
before `CANDIDATE_ATTESTATION.json` is signed without granting production
promotion authority.

Emergency `evidence stop` is the exception: it must durably increment the epoch
and assert global stop using the pilot integrity key even when verifier public
configuration is missing or corrupt. It then drains or quarantines cached workers;
no process may rely on a previously loaded authority to retain a boundary permit.

Every target-file, evidence-worker, evidence-write, and verifier boundary requires
one short-lived, single-operation permit bound to the current epoch, plan, stage,
attempt, operation kind, and reservation. Permits are acquired in the authenticated
coordinator transaction immediately before the boundary and consumed exactly once.
Permit issuance, boundary start/finish, and control actions share one authenticated
monotonic sequence, which is the linearization order; wall-clock timestamps do not
decide races. A broker receives only one file entry after each fresh parent permit,
never the whole catalog, and exits and closes its handles if the control channel
dies. A stalled worker cannot continue with another file under an earlier permit.
`stop`, `revoke`, pause, and cancel first advance the epoch or plan generation,
block new permits, and enter `draining`; they acknowledge completion only after
already-started permits close or are quarantined at the bounded deadline. Thus the
supported claim is zero new boundary starts after a stop acknowledgement, not that
an already-open file read was undone.

Before any production promotion, attest that the ordinary shared JARVIS store is a
separate schema-41 database/key pair and record only its private root identity and
schema version without copying or opening it from a Phase 6 process. Preserve the
public-verifier configuration required to validate receipts; never place verifier
private signing material in the pilot, backup, archive, or repository.

Rollback is tested behavior, not a source-control suggestion:

1. Set the Phase 6 mode to `disabled` and assert the Phase 5 global stop.
2. Refuse new claims and quarantine every nonterminal Phase 6 plan without
   deleting or rewriting its append-only evidence.
3. Revoke the exact promotion record and active final-verifier configuration while
   preserving the public configuration with the quarantined evidence.
4. Archive the entire quarantined schema-42 Phase 6 root—database, sidecar key,
   private evidence, store marker, and public-verifier configuration—as one
   verifiable set; never auto-resume a plan.
5. Make the pilot root unavailable to ordinary commands, reinstall the sanitized
   Phase 5 no-executor baseline, and point it only to the untouched schema-41
   shared store.

Rollback is quarantine-based, not an in-place code downgrade or database restore.
The Phase 5 binary must never open the schema-42 Phase 6 database. Exact schema
assertions before and after every drill prove that the archived Phase 6 root
remains 42 and the shared baseline store remains 41. Rollback does not mark incomplete work
complete, erase charges, retry ambiguous reads, or restore authority. The exit
evaluation covers every reachable durable state and crash window.

Protected roots remain disjoint throughout active operation. The sole intentional
ancestry change is the terminal archive transfer after stop acknowledgement,
permit drain, promotion revocation, and source quarantine. It consumes a one-shot
archive permit, makes the active source unavailable, verifies a create-only
manifest at the destination, and leaves the archived database inaccessible to
ordinary or Phase 6 execution commands. A recovery-only validator may inspect its
public validation material and manifest; it cannot advance state.

After the final candidate attestation exists, a separate operator-authorized
production-boundary smoke must exercise the real `Phase6PilotStore`, operational
public-authority import, actual `promote`, a sealed synthetic-file workflow,
supported verifier, stop/revoke, quarantine/archive sequence, and return to the
untouched schema-41 baseline. It is required before genuine operator-selected
files or any operational-use claim, but it is not copied backward into the
candidate evaluation and cannot change its frozen thresholds.

## Explicit non-goals

Phase 6 does not provide or claim:

- a generic workflow, task, agent, tool, callback, plugin, or model executor;
- semantic code, design, legal, medical, financial, or safety review;
- builds, tests, dependency installation, target or operator-supplied code
  execution, repair, or file edits;
- recursive repository scanning or assurance about unlisted files;
- web research, network or cloud access, private-file discovery, account access,
  publication, deployment, purchases, trading, desktop control, or device control;
- automatic approvals, initiative, scheduling, delegation, or background work;
- exactly-once filesystem reads, side-effect-free reads, or protection from a
  same-user attacker controlling both the process and its integrity keys; or
- AGI, consciousness, universal prompt-injection resistance, flawless operation,
  or general safety.

## Allowed completion claim

If and only if the pinned exit gate passes, the project may say:

> Under the pinned synthetic evaluation, JARVIS advanced only
> closed-schema, allowlisted deterministic workspace-evidence workflows through
> the sealed fixture-bound evaluation permit and explicit one-stage advances
> across the tested restarts and concurrency races, with precharged
> operations, project-scoped provenance, fail-closed controls, and independent
> completion verification.

No broader wording is supported by Phase 6 evidence.
