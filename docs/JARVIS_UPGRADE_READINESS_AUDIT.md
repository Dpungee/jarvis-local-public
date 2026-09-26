# JARVIS Upgrade and Transition Readiness Audit

Status: planning/readiness evidence only; no upgrade implementation started\
Audited: 2026-09-15\
Repository: `Dpungee/jarvis-local-public`

## Verdict

**NO-GO for beginning the autonomous multi-agent transition from the current checkout.**

GitHub `main` has a strong verified test baseline, but the active checkout is stale and dirty, one high-severity CodeQL finding remains open, dependency updates remain unmerged, status/release records lag the current memory merge, and Windows path sensitivity is reproducible in the actual long workspace layout.

This verdict does not mean upstream `main` is broadly broken. It means the repository and current local state cannot honestly be described as flaw-free, fully current, and transition-ready without the remediation/triage steps below.

## Verified upstream baseline

- Upstream `main`: `93119c421cdea4fffafcd3d13caef3fccfe15a29`.
- Latest upstream change: VTMF governed-memory merge through its M5 compaction milestone.
- Upstream schema version: 50.
- Required GitHub checks on that commit passed on 2026-09-05, including Windows Python 3.11, 3.12, and 3.13, coverage/distribution, secret/privacy, and CodeQL jobs.
- Current public release/tag remains prerelease `v0.6.3`; the VTMF merge has no corresponding release tag.
- The public-release scanner, Python compilation, JavaScript syntax check, selected CI Ruff rules, and high-severity Bandit check passed against the exact upstream commit during this audit.
- No open Dependabot vulnerability alerts or secret-scanning alerts were returned.

## Current-checkout mismatch

- Active branch: `codex/local-coding-library`.
- Active HEAD: `4e747afd26dd116c28b306c80f266925aeb54e84`.
- Divergence from upstream `main`: two local-only commits and five missing upstream commits.
- Active checkout schema: 41, versus upstream schema 50.
- Active worktree: 29 modified or untracked entries at audit time, including unrelated user/parallel work that must be preserved.
- Numerous additional worktrees exist, several with substantial uncommitted changes. They require an ownership and preservation inventory before any rebase, merge, cleanup, or branch retirement.

## Test evidence

The authoritative clean verification used:

```text
Commit: 93119c421cdea4fffafcd3d13caef3fccfe15a29
Checkout: isolated audit checkout of the public baseline
TEMP/TMP: C:\jtfull
Python: 3.13.7
Install: pip install -e ".[documents]"
Command: python -m unittest discover -s tests
Result: Ran 4230 tests in 666.193s — OK (skipped=8)
```

The same commit is path-sensitive on Windows:

- With a longer external TEMP, the full suite produced one `WinError 206` in `DependencySetupTests.test_node_manager_reads_only_staged_manifests_and_publishes_modules`; that isolated test passed when TEMP was shortened.
- In the actual long project path, `AgentLoopTests.test_tight_context_pins_real_json_context_and_coherent_current_tool_groups` failed on three consecutive runs with `Configured context is too small to preserve mandatory current context`, while the same test passed three consecutive times from the short checkout.
- A full run nested beneath the long workspace produced 15 failures, 52 errors, and 7 skips. Some of that count was caused by the audit TEMP being located inside the repository, so it is not treated as a pure product-failure count. It remains useful evidence that repository-relative temp detection and Windows path length can amplify failures in realistic local layouts.
- Repeated `ResourceWarning` messages showed unclosed SQLite connections. A Python 3.14 tar-extraction deprecation warning was also observed.

Conclusion: the code has a green short-path baseline, but the intended Windows workspace path does not yet have an equivalent clean baseline.

## GitHub and maintenance findings

### Blocking or unresolved

1. One open high-severity CodeQL alert exists for clear-text storage of sensitive data in `tests/test_benchmarks_runner.py`. The value appears to be a synthetic leakage-test marker, but the finding is still open and must be resolved or formally classified.
2. Three draft Dependabot pull requests are open and behind `main`: Google Auth, Google Auth HTTP transport, and the datasets version range.
3. Optional dependencies use broad ranges. A current clean install resolved newer document-stack packages than the successful 2026-09-05 GitHub run, so the historical green result is not a completely reproducible dependency lock.
4. `PROJECT_STATUS.md` is dated 2026-09-02 and describes an earlier memory state, not the 2026-09-05 VTMF merge.
5. The current Council/specialist model is JARVIS-chaired, deterministically speaker-scheduled, tool-free in the room, and peer-bounded. It is transition input, not the requested autonomous peer-to-peer target.

### Governance observations

- Branch protection requires the test, coverage, privacy, and CodeQL checks; enforces linear history and administrator coverage; blocks force pushes/deletions; and requires resolved conversations.
- Required approving reviews are set to zero, code-owner review is not required, and signed commits are not required. These are governance gaps to evaluate before a large architectural transition.

## Required pre-transition actions

1. Choose and document the authoritative baseline: exact upstream `main` commit or a later reviewed successor.
2. Inventory and preserve every dirty worktree/branch before integrating upstream; do not overwrite or discard parallel work.
3. Reconcile the active checkout with schema 50 and the already-merged VTMF memory implementation.
4. Resolve or formally dismiss the open CodeQL alert with documented reasoning.
5. Review, update, or close the three behind Dependabot pull requests and decide whether to lock the transition's dependency set.
6. Update `PROJECT_STATUS.md`, changelog/release state, and the memory/council architecture map to match current `main`.
7. Make the full suite pass from the actual intended Windows workspace path, including long-path dependency staging and context-compaction behavior.
8. Triage unclosed SQLite handles and the Python 3.14 archive-extraction warning.
9. Complete Phase A from `JARVIS_MULTI_AGENT_EXPANSION_PLAN.md` before implementing new multi-agent subsystems, so existing VTMF components are mapped rather than duplicated.

## Scope and non-actions

This audit did not change product code, schemas, runtime configuration, GitHub settings, alerts, pull requests, branches, commits, releases, or deployments. It did not dispatch Claude or begin the memory audit. Only planning and handoff documents were created or refined.
