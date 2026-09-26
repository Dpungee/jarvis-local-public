# Autonomous upgrade preparation

## Scope and baseline

Prepared on `codex/upgrade-readiness` from upstream `main`
`93119c421cdea4fffafcd3d13caef3fccfe15a29`. No autonomous runtime implementation,
live database migration, commit, or publication is included. Claude's handoff is
prepared but has not been sent. Existing dirty worktrees are preserved.

The controlling roadmap is `AUTONOMOUS_MULTI_AGENT_SOURCE.md`; the implementation
sequence is `JARVIS_MULTI_AGENT_EXPANSION_PLAN.md`. Agents choose their peers and
communications autonomously. JARVIS provides infrastructure, not speaker selection,
permission to communicate, or mediation. Only scopes, owner control, runtime
authority, and capability concepts actually specified in the roadmap belong in
the target design; legacy central orchestration is not a target requirement.

## Readiness findings and disposition

| Finding | Disposition |
| --- | --- |
| Original checkout is dirty and behind upstream | Preserved; preparation uses an isolated main-based worktree. |
| Upstream memory schema is 50 | Recorded; no personal runtime database was opened or migrated. |
| Dependency fixture paths exceed Windows limits under long TEMP roots | Replaced method-name directories with bounded unique temporary names; focused regression passed under the previously problematic TEMP root. |
| Earlier tight-context failure | Reproduced only in the deeply nested audit checkout; the ordinary-depth preparation worktree passed unchanged. Finite context/path limits remain, not a universal arbitrary-path guarantee. |
| CodeQL alert 4 | Reviewed synthetic public test text, not a credential; dismissed as false positive on GitHub, verified dismissed. Detection remains enabled. |
| Google auth dependency PRs 15 and 16 | Exact versions 0.4.2 and 2.57.0 integrated locally and tested. Upstream PRs remain open until separately published/resolved. |
| Datasets PR 1 | Deferred: optional training dependency major-version expansion needs its own compatibility evidence; current upper bound remains intentionally unchanged. |
| Reproducibility | `requirements/upgrade-windows-py313.txt` records the prepared Windows Python 3.13.7 environment, not a universal or training lock. |
| SQLite ResourceWarnings | Observed in tests; fixture lifecycle debt, not evidence of a demonstrated production leak. |
| Archive extraction future behavior | Use the data filter when available; older supported patch releases extract only the locally generated Git test archive. |
| GitHub governance | Existing branch controls unchanged. No release or merge authorization implied. |

## First execution work packages: Phase A

1. Audit `specialists.py` definitions/contracts, `council.py` meeting/directive
   scheduling, `agent.py` execution loops, and `long_horizon.py` durable tasks.
   Document implemented/partial/missing with exact symbols, interfaces, migrations,
   and tests. Peer-blind specialists and centrally chaired Council meetings are
   replacement candidates, not the requested autonomous architecture.
2. Claude owns the memory audit and subsequent memory work described in
   `CLAUDE_MEMORY_HANDOFF_SPEC.md`. Existing VTMF milestone M3 does not mean roadmap
   M3 agent memory is complete. Roadmap taxonomy is M0 working, M1 events,
   M2 semantic, M3 agent, M4 organization, M5 optimization.
3. Agree stable agent/task/room identifiers, event envelopes, memory scopes,
   proposal/outcome records, causal/idempotency keys, and restart semantics across
   runtime and memory before implementing dependent phases.
4. Specify the smallest Phase B acceptance slice: two independent agents discover
   each other, directly message, use a shared room, delegate a task, and resume
   after restart without JARVIS choosing who speaks or mediating communication.

Do not dispatch Claude or start these upgrade work packages from this preparation
document alone. Operator approval and the required readback precede delegation.

## Verification evidence

Environment: Windows, Python 3.13.7. Provider calls, external access, execution, and
computer access disabled for deterministic tests. No live OAuth flow was tested.

Recreate the prepared environment with:

```powershell
python -m pip install -c requirements/upgrade-windows-py313.txt -e '.[documents,drive]' ruff bandit build pip-audit
```

- Focused dependency, benchmark, Drive, workspace gateway, and tight-context suite:
  271 tests passed. The dependency suite used a long external TEMP root.
- `python -m pip check`: passed.
- `python -m pip_audit --local`: no known vulnerabilities; editable local
  `jarvis-local` itself is skipped because it is not on PyPI.
- `python -m ruff check jarvis scripts tests --select E9,F63,F7,F82,F401,F841,B023,B033,PERF102,B018`: passed.
- `python -m bandit -q -r jarvis scripts -lll`: passed.
- `node --check jarvis/presence.js`: passed.
- `python -m build`: source distribution and wheel built successfully.
- Installed wheel in a clean environment outside the source tree: four entry
  points imported, packaged holdout fixture present, CLI and workflow help passed,
  and public Presence health returned healthy/offline/disabled as expected.
- `python scripts/check_public_release.py`: passed for the staged snapshot and
  working tree. An exact ranged-commit check is not applicable until committed;
  its index/ref equality check correctly rejected the uncommitted candidate.
- `git diff --cached --check`: passed after removing Markdown trailing spaces.
- All 17 other inventoried worktrees retained their commits, branches, and status
  entries, including detached audit worktrees. This is not a content-hash audit.
- `python -m unittest discover -s tests`: 4,230 tests run in 643.973 seconds,
  OK with 7 skips (4,223 non-skipped), no failures or errors.
- Timing gates were not enforced. Several learning-ladder cold/miss/sweep targets
  were exceeded; milestone retrieval cold/max was slightly over 10 ms. These are
  follow-up performance findings, not a clean performance certification.
- Dependency snapshot install dry-run resolved successfully in the prepared env.

Local checks cannot prove an absence of flaws or substitute for hosted Python
3.11/3.12/3.13, coverage, CodeQL, and secret scans on an exact published candidate.
Publication requires the protected PR process and remains a separate action.
The private preservation inventory is kept outside the public repository; it
records branch, commit, and status, not a byte-for-byte backup of all worktrees.
