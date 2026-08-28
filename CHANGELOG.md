# Changelog

All notable public changes to JARVIS Local are recorded here. The project follows
semantic versioning for tagged releases.

## [Unreleased]

No changes yet.

## [0.6.0] - 2026-08-28

Public preview candidate.

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

[Unreleased]: https://github.com/Dpungee/jarvis-local-public/compare/v0.6.0...HEAD
[0.6.0]: https://github.com/Dpungee/jarvis-local-public/releases/tag/v0.6.0
