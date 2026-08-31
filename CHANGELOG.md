# Changelog

All notable public changes to JARVIS Local are recorded here. The project follows
semantic versioning for tagged releases.

## [Unreleased]

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
