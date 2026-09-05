# Changelog

All notable public changes to JARVIS Local are recorded here. The project follows
semantic versioning for tagged releases.

## [Unreleased]

### The learning ladder: governed skill promotion (VTMF M4)

- A single verified outcome no longer writes a live, model-visible learned
  skill. That ungoverned step is removed: promotion now runs on five rungs
  (lesson, verified reuse, candidate, staged, approved) and only an
  operator-typed command makes a document live. Two new governed verbs,
  `Approve skill promotion #N <code>` and `Roll back skill promotion #N`, are
  parsed from the raw operator turn before any model call, exactly as the four
  M1 verbs are, with fixed receipts and a near-miss refused as that verb.
- The value on an approval is a **confirmation code, not a capability**:
  sixteen random characters stored on the promotion row, shown only by
  `jarvis ladder list`, `jarvis ladder show` and `/ladder`, only while the row
  is staged, single use, and absent from every spine payload, activity log,
  run metric, Presence payload, prompt block and model reply. A rollback needs
  none, and is never refused for a regressed ledger: undo must always be
  available.
- A staged document is unreachable two ways. It lives outside the only
  directory the skill catalog walks, and `.jarvis-skills-staging` joins the
  file tools' protected components, so the model cannot read, write or list it
  or use it as a shell working directory.
- Schema 49 adds an append-only calibration ledger of sealed, fixed-size
  epochs (exactly twenty resolved outcomes in id order, so a boundary is never
  anyone's choice) with a keyed coverage digest, and a promotion record with
  spine lineage in both directions. Neither table is a projection; neither is
  rebuilt by `rebuild-claims`; a downgrade that would discard authentic
  spine-backed ladder state refuses to open the store. Staging and approval
  are refused while a family's ledger has regressed for two consecutive
  epochs, which `jarvis ladder status` distinguishes in those words from a
  single bad epoch that refuses nothing.
- `python -m jarvis ladder status | list | show | stage | approve | rollback |
  discard | seal | verify | ledger` and `/ladder` in chat are the operator
  surfaces; no subcommand takes a workspace, because every one derives it from
  the promotion's own project. `ladder verify` is also the one-time pass that
  adopts pre-M4 live documents at stage `unapproved_legacy`. Those documents
  **stay live and reach the model with no proof, gate or ledger check** until
  they are approved or rolled back, and `ladder status`, `jarvis doctor` and
  the Presence panel all say so in the same words: `N legacy skills live
  without approval`.
- Retrieval-time lesson injection now reaches the model on the dialogue lane,
  which is the lane most ordinary turns take; before this both learning blocks
  were rendered and then discarded there. When the channel consulted the store
  and came back with nothing it was allowed to use, the model is told so in one
  line instead of being left to fabricate — except when the store simply found
  nothing relevant, and except on a fresh install where the gate is shut but
  nothing was withheld. Nothing was added to the compact runtime contract.
- One additive read-only Presence route, `GET /api/memory/ladder`. There is no
  write route: a browser button that promotes a skill is the affordance this
  design exists to prevent. See [The learning ladder](docs/LEARNING_LADDER.md).

### The temporal graph, and erasing one ordinary memory (VTMF M3)

- Schema 48 projects every non-excluded claim row into a graph of entities and
  edges with validity intervals (`memory_graph_entities`,
  `memory_graph_edges`, and an explicit id sequence, in the same database file
  so an erase removes a fact and its edges in one transaction). A bounded,
  deterministic walk of at most three hops in both directions answers a
  question that spans two or three stored facts with no model call on the read
  path: "Which datacenter hosts the Kestrel relay?", "What runs on the Harrier
  box?", "Which relay is in the Fenwick datacenter?" and "Which region is the
  Kestrel relay in?" are all answered from the same three facts, and a stored
  value with facts behind it no longer receives a not-recorded cue. The
  one-hop bridge remains only for a store without the projection.
- The graph is derived, never authoritative, and adds no spine event kind:
  `spine rebuild-claims` proves the claims equal the spine and
  `python -m jarvis graph rebuild` proves the graph equals the claims, with the
  same plan-token discipline and exit codes (without `--yes` the plan and its
  token are printed and the exit status is 2; `--apply --yes --plan TOKEN`
  refuses with `stale_plan` when the store has moved). `graph status` and
  `graph verify` print ids, counts, and the three exclusion categories, never
  values; `graph paths "<subject>"` shows the screened chains the agent would
  see. `spine verify` reports the graph counts informationally and its exit
  code is unchanged by them.
- Chain rows join the `temporal_claims` block with a `chain` number and a
  1-based `hop`, and a bounded read is never presented as complete: a hub whose
  fan-out cap was hit is named with the hop at which the walk stopped, and
  every chain shortened by a cap, the time budget, or the block budget is
  marked incomplete. A chain is as strong as its weakest hop, which the block
  marks. Three guidance lines ride with the block in the per-turn wrapper;
  nothing is added to the compacted runtime contract.
- A live chain answer with an empty main memory lane is no longer announced as
  retracted: the "former values only" lead is now selected on the absence of a
  current entry, not on the absence of a main-lane row. A temporal answer built
  only from superseded edges still gets it.
- Past-tense questions traverse superseded edges; an ISO date or a month and
  year in the question sets an explicit instant. A row superseded in place
  before schema 46 has no end interval and is deliberately invisible to dated
  questions rather than matching all of them; migration 48 does not rewrite
  claim rows.
- `Erase memory #<id>` (also `Delete memory #<id>`) is a fourth governed verb,
  with `python -m jarvis memory list` and `memory erase <id> --yes` and the ids
  now shown by `/memory`. It deletes the row, every dependent row carrying its
  `memory_id` (a list derived from the live schema, not hand-maintained), and
  its FTS entry under `secure_delete`, and appends one digest-only
  `memory.deleted` receipt naming how many transcript copies remain. Three
  refusals change nothing: no such row, a row that backs a project fact, and a
  row that mirrors a vault note.
- The private-identifier screen is widened for the graph, the agent's
  superseded and retracted history helpers, and the memory listing preview: it
  now catches phone numbers, bare IPv4 and IPv6 addresses, IP-host e-mails,
  SSN-shaped strings, Luhn-valid card numbers, street addresses, and
  identifiers hidden past a 512-character scan cap, over both the subject and
  the value. Version numbers, port ranges, ISBNs, MACs, UUIDs, ISO dates, CIDR
  blocks and the loopback, documentation and multicast ranges are deliberately
  exempt. The live claims lane keeps the narrower screen, so no sealed
  evaluation moves.
- Two claim keys whose backing content renders identically no longer crash the
  writer: the second row carries a keyed suffix derived from the claim key, and
  eligibility, verification and rebuild all accept either variant from the
  row's own fields, so both facts stay recallable and byte-reproducible.
- Identity and abstention were tightened against a third independently
  authored holdout: a one-word name matching two stored keys abstains as
  ambiguous instead of reporting nothing recorded; a typed name resolves by
  whole-word prefix and, matching nothing, reports nothing recorded rather
  than ambiguity; the claims lane's broader guesses can no longer answer for a
  name the graph could not identify; a question naming two subjects where only
  one is known is answered for that one and says so for the other; and a chain
  that ends at a hub too large to read is marked incomplete like one that
  passes through it.
- Documentation: [The temporal graph](docs/MEMORY_GRAPH.md) is new.
  Older trees refuse to open a database at 48.

### The memory spine, slice 2: memories and lessons (VTMF M2)

- Schema 47: every `memories` row carries `spine_event_id`, ids come from
  `memory_id_sequence` and are never reused, and a `BEFORE INSERT` trigger
  requires the creating event. New kinds `memory.imported`, `memory.created`,
  `memory.reasserted`, `memory.updated`, `memory.deleted`, and
  `lesson.created` carry digest-only payloads (an HMAC under the spine key,
  never content). Migration 47 imports existing rows and links claim backing
  rows to their claim's event; it refuses to open a store only when a row
  whose lineage was nulled no longer matches its event's digest, or when a
  lineage-less row was planted (an edit that keeps its lineage is reported by
  `rebuild-memories`, not refused).
- The model's `remember` tool is receipted as `actor=model` with the gate that
  admitted it (`<autonomy>:<origin>:explicit_memory_write`) and the
  conversation id, set for exactly one dispatched call; the vault chat verb,
  the CLI feedback command, and the indexer loops carry their own actors.
- `python -m jarvis spine rebuild-claims --apply --yes` reconciles the live
  claim projection in place from the spine (deletions, field updates,
  recreations, in the erase order, with an in-transaction re-check and a
  `projection.rebuilt` receipt); without `--yes` it prints the plan and its
  plan token and exits 2, and `--apply --yes --plan TOKEN` refuses with
  `stale_plan` when the store no longer matches that plan; it refuses on a
  spine that fails verification. `spine rebuild-memories`
  is the dry-run memory rebuild; `spine verify` reports the memory lineage
  counts.
- A retired project fact (`Forget this project fact:`) still answers
  past-tense questions in later conversations: the agent reads the subject's
  history and surfaces the former values as `superseded` with `retracted:
  true`; the not-recorded cue is suppressed while such history exists, and
  only `Erase` removes a value from temporal answers.

### The memory spine (VTMF M2, slice 1)

- Add `memory_spine_events` (schema 46): one append-only, keyed, hash-chained
  event contract with actor, source, scope, permission, outcome, and lineage.
  Append-only is enforced by SQLite triggers; the only permitted change is a
  tombstone-backed redaction. The digest is an HMAC keyed by a sidecar
  `<database>.memory-spine.key`, so `spine verify` proves authenticity, not
  just self-consistency.
- Every claim row now carries the id of the event that produced it, claim ids
  are allocated explicitly and never reused, status changes carry the claim's
  after-image, and migration 46 backfills every existing claim.
- Add `Erase this project fact:` (delete every version, tombstone, redact
  earlier payloads, `secure_delete`, honest receipt about transcript copies),
  a `conversation.deleted` receipt, and spine receipts for shown, refused, and
  confirmed proposals.
- Add `python -m jarvis spine verify | rebuild-claims | tail`. The rebuild is a
  dry-run replay into a shadow projection with a per-claim divergence report;
  the M2 exit test runs a randomized history and asserts equivalence.
- Harden the spine against the laundering paths a red team found: a keyed
  head record makes tail removal visible and every append requires it; a
  `user_version` downgrade over an authentic spine is refused instead of
  re-importing the projection; the key sidecar is created only with the
  spine (a store with a spine and no sidecar refuses to open, and the genesis
  event records the key fingerprint); proposal digests are salted and
  redacted by the erase tombstone; the claim sequence is verified against
  the store; `secure_delete` is on for the whole connection.

### Governed project memory keystone (VTMF M1)

- A fact stated in ordinary conversation is never written silently. The turn
  receives a deterministic `Not stored` receipt with the exact governed
  command, proposed by a closed rule grammar over the operator's own words
  (structured forms, possessive, relational, "is now", "changed from … to",
  rename, pronoun clauses; questions, requests, reported speech, pronoun and
  personal-relation subjects, code, and anything the parser rejects yield no
  proposal). When the grammar cannot split a licensed statement, the local
  model may propose a triple grounded verbatim in the operator's words
  (`JARVIS_MEMORY_PROPOSER=assisted`, the default; `rules` disables it). The
  next turn may confirm with `store it`; the confirmation is resolved against
  the runtime's own persisted record of the proposal it showed (schema 45,
  `memory_fact_proposals`), re-derived from or grounded in the operator's
  message, never taken from assistant text. No model writes memory. `Forget this project fact:` retires a fact and
  `python -m jarvis facts` lists them.
- An operator-stored fact for a named subject outranks weak web intent such as
  "latest"; past-tense questions see superseded values as history; a question
  spanning two facts receives a bounded one-hop bridge; an unknown
  project-shaped subject gets an explicit not-recorded cue while world
  knowledge still answers; a known subject shows its own facts instead.
- Claim reads never take the write lock (telemetry is best-effort in a
  separate short transaction), candidate discovery narrows before abstaining
  at the bound, and `Memory.claim_recall_report()` makes every abstention
  observable.

### Restart-safe long-horizon coordination

- Add a project-scoped, closed-schema workflow store with ordered checkpoints,
  durable pre-operation usage reservations, retry accounting, clock rollback
  detection, pause/cancel controls, and keyed database-state integrity.
- Add append-only mutation intent, short-lived one-shot authorization,
  result, and signed reconciliation rounds. Ambiguous effects enter
  reconciliation after restart; confirmed applied effects cannot be downgraded
  or dispatched again.
- Require a separately pinned Ed25519 verifier before terminal completion and
  bind verification to the exact runtime, evidence, artifact, outcome, and
  executor/actor set.
- Add prompt-free `workflow status/list/show/start/pause/resume/cancel`
  commands. Registration never starts an executor, and Phase 5 intentionally
  exposes no generic callback or `workflow run` surface.
- Add a deterministic multi-project restart evaluation with real subprocess
  exits, a separate exactly-once effect ledger, negative controls, and
  fail-closed replay/tamper checks.

### Cross-domain strategy transfer

- Add explicit `strategy-transfer start/status/abort/promote` operator surfaces
  for a bounded project-scoped causal trial. Trial manifests accept only closed
  labels, counts, timestamps, and internally derived pinned digests; they cannot
  store task prose. Jarvis generates the assignment seed without displaying it.
- Add the fail-closed `trial` configuration mode. `advise` still requires both
  a valid pinned causal attestation and a separate explicit operator promotion;
  changing configuration alone cannot activate cross-domain advice.
- Bind four closed procedural strategy labels to exact successful task receipts
  and Phase 3 lesson provenance; free-form lesson prose cannot create evidence.
- Select only fresh, uncontradicted, same-project lessons from a different
  calibrated task family, with an idempotent application/outcome ledger.
- Default to observation-only operation. In this release, `advise` remains
  reporting-only and cannot change prompts; activation awaits a separate
  bounded trial that binds randomized arms before outcomes exist. Any future
  advisory remains unable to change tools, approvals, policy, scope, routing,
  or verification.
- Add prompt-free transfer telemetry and a deterministic paired outcome holdout
  covering positive transfer, negative transfer, provenance, and restart safety.

## [0.6.3] - 2026-08-31

### Routing and operator clarity

- Route structurally bounded, single-unit coding tasks through the fast model while
  retaining normal coding tools, verification, and automatic escalation to the full
  coding model after repeated failures. Broad, multi-file, architectural, migration,
  deployment, and integration work continues to start on the full coding profile.
- Add natural read-only aliases for `project create`, `memory status`, `reflection
  list`, and `control status` without weakening any mutation or approval boundary.
- Clarify that usage success is measured per provider transport call, so a failed
  attempt remains visible even when retry or failover later completes the request.
- Make disabled self-inspection errors name the exact opt-in setting required to run
  the isolated self-test.

### Capability and supply-chain hardening

- Treat bounded `research_question` excerpts as untrusted web evidence, block
  every subsequent mutation lane except constrained research/report notes, and
  prevent private local evidence from entering later outbound research calls.
- Require a single unambiguous schedule mutation in the current interactive
  operator message; quoted, remembered, background, companion, negated, advice,
  and multi-operation text cannot grant schedule authority.
- Bind dependency-install approval to exact manifest digests, refuse executable
  local Python build backends, require binary Python distributions, disable Node
  lifecycle scripts, and redact command output before it reaches logs or models.
- Resolve Windows system utilities from the OS-reported Windows directory rather
  than ambient search paths, reject link substitution, and prevent self-diagnosis
  from executing a workspace-controlled Git binary.
- Reject credential-forwarding redirects in the OpenAI Images, Telegram, and
  loopback Companion HTTP clients; strengthen Drive download TOCTOU checks and
  connector/provider error redaction.
- Bind artifact launch to an ordinary, non-linked file with a bounded size and
  rechecked SHA-256 identity, and prevent readonly Companion control from
  expanding observation authority.
- Add recursive tool-schema validation, direct wiring tests for every read-only
  GitHub/Drive/Vercel adapter, specialist compatibility checks for scheduled
  work, and exhaustive schema-to-runtime-signature contract tests.
- Add a Windows CI quality gate covering the full document extra, branch
  coverage, package build, JavaScript syntax, dependency audit, and public-
  release privacy scan.

### Installed-application recovery

- Added a profile-driven Windows application diagnosis contract that classifies
  connectivity, rendering/cache, authentication, process, update, and unknown
  failures from bounded evidence instead of model assertions.
- Added the first reversible repair adapter for Epic Games Launcher renderer
  caches: exact one-shot approval, graceful close only, metadata drift checks,
  backup moves with rollback, and an exact no-shell restart.
- The approval dialog shows every bounded source-to-backup move using relative
  paths, the total directory and byte counts, reversibility, and the exact plan
  digest before anything changes.
- Repair plans bind the trusted machine-wide install target to its full
  executable digest and recheck processes, cache metadata, and destination
  ancestry at execution time.
- Kept firewall, proxy, hosts, DNS, registry, credential, account, installer,
  update, force-kill, and deletion actions outside the repair authority.
- Repair application and repair verification are intentionally separate. A
  restart or window title cannot prove that the UI rendered, and an unverified
  repair cannot satisfy completion or become a reusable lesson.

### Current limits

- Executable repairs require a reviewed declarative application profile; the
  first profile covers only Epic Games Launcher's disposable renderer caches.
- Transient Screen Companion evidence is not yet bound to the repair receipt,
  so applied repairs remain incomplete until real visual and health evidence is
  available. Automatic repair-lesson persistence and recall therefore remain
  disabled; an applied-but-unverified repair may leave an audit reflection but
  cannot enter the reusable lesson store.

## [0.6.2] - 2026-08-28

Public-preview reliability release.

### First run and Presence lifecycle

- Made provider setup ignore ambient or empty credentials and copied templates, then
  require a bounded tool-free canary through every uniquely configured model route.
- Added explicit Presence `start`, `status`, `restart`, and `stop` actions with exact
  version, source-root, Python, process, installation, and runtime-epoch identity.
- Prevented stale or foreign processes from being stopped or mistaken for the current
  installation, including safe manual/scheduled-task takeover and uninstall handling.

### Task and document reliability

- Bound exact private-file reads and their approvals to the operator-stated file, blocked
  parent-directory or same-name substitution, and cleared denied pending goals.
- Kept task-contract fallback details in prompt-free telemetry instead of presenting an
  internal routing fallback as a user-visible failure.
- Added one bounded recovery turn when a provider promises a document without invoking
  the required tool, while still withholding completion without a verified artifact.
- Added structured spreadsheet rows and sheet names so generated XLSX workbooks contain
  the requested table rather than Markdown-like prose.
- Made natural Companion control requests accept harmless confirmation clauses such as
  “turn it off and confirm,” without widening the underlying command grammar.

### Evaluation foundation

- Added a frozen cross-domain strategy-transfer evaluation with provenance, freshness,
  contradiction, same-family, authority-leakage, and abstention checks. The selector is
  advisory-only and is not yet wired into runtime routing, tools, or approvals.

## [0.6.1] - 2026-08-28

Public-preview stabilization release.

### First-run and companion fixes

- Kept first-run provider setup required after a harmless status check creates an empty
  runtime database, while preserving migrations for installations with real user state.
- Ignored empty API-key environment variables when deciding whether setup is complete.
- Made the optional-feature review disclose prerequisites, reject unrecognized choices,
  and state clearly that the review itself performs no scans, pairing, or containment.
- Prevented the Companion indicator from flashing before its first real status while
  preserving its On/Resume controls when observation is off, paused, or unavailable.

### Security and release maintenance

- Expanded ignored credential, private-key, cloud-client, and SQLite sidecar paths.
- Replaced shell-based API-key examples with no-echo Windows UI and credential-rotation
  guidance.
- Extended the public-release checker to inspect author, committer, tagger, commit-message,
  and annotated-tag metadata without echoing detected personal values.
- Kept pull-request checks focused on the publishable head history instead of GitHub's
  temporary merge commit while still scanning the merged working snapshot.
- Added regression coverage for commit metadata and made CLI event output non-sensitive
  by construction while preserving detailed, sanitized Presence activity. Actions,
  JavaScript/TypeScript, and Python CodeQL analyses are required release gates.
- Added a fail-closed, two-phase public-publishing guard for protected candidate branches
  and release tags from disposable public-only clones.

## [0.6.0] - 2026-08-28

Public preview release.

### Highlights

- Local-first conversation, source-grounded research, coding, document, memory,
  specialist-agent, screen-companion, and bounded automation foundations.
- Automatic provider and model routing across configured local, API, Codex CLI, and
  Claude CLI backends.
- Provenance-aware memory, task calibration, approval receipts, resumable work, and
  research verification.
- Browser Presence, native UI, CLI, worker, and opt-in companion/runtime controls.
- Opt-in network and Bluetooth inventory, bounded defensive diagnostics, and
  operator-visible device risk assessments for networks the operator owns.
- Source-backed product comparisons, safe link rendering, specialist delegation, and
  lower-latency conversational/task-contract routing.

### Security and release hardening

- Private files, desktop control, external accounts, publishing, proactive work, and
  public presence remain disabled or approval-gated by default.
- Public Presence ships as a disabled, fail-closed foundation with no live publishing
  adapter.
- GitHub Actions use immutable action revisions and run the deterministic suite on
  Python 3.11, 3.12, and 3.13 for Windows.
- Source and distribution artifacts are licensed under Apache-2.0.

### Known limitations

- JARVIS Local is an alpha, single-operator Windows project, not an OS sandbox or a
  turnkey multi-user service.
- Optional providers and external connectors require the operator's own accounts and
  local configuration.
- Trusted-host execution inherits the permissions of the Windows account running
  JARVIS Local.
- Public Presence is a disconnected foundation only; it cannot publish or connect to
  a social account in this release.

[Unreleased]: https://github.com/Dpungee/jarvis-local-public/compare/v0.6.3...HEAD
[0.6.3]: https://github.com/Dpungee/jarvis-local-public/compare/v0.6.2...v0.6.3
[0.6.2]: https://github.com/Dpungee/jarvis-local-public/compare/v0.6.1...v0.6.2
[0.6.1]: https://github.com/Dpungee/jarvis-local-public/compare/v0.6.0...v0.6.1
[0.6.0]: https://github.com/Dpungee/jarvis-local-public/releases/tag/v0.6.0
