# Changelog

All notable public changes to JARVIS Local are recorded here. The project follows
semantic versioning for tagged releases.

## [Unreleased]

No changes yet.

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

[Unreleased]: https://github.com/Dpungee/jarvis-local-public/compare/v0.6.2...HEAD
[0.6.2]: https://github.com/Dpungee/jarvis-local-public/compare/v0.6.1...v0.6.2
[0.6.1]: https://github.com/Dpungee/jarvis-local-public/compare/v0.6.0...v0.6.1
[0.6.0]: https://github.com/Dpungee/jarvis-local-public/releases/tag/v0.6.0
