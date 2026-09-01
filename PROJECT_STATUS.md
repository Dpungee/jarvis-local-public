# JARVIS Development Status

Updated: 2026-09-01

## Authoritative baseline

- Public source of truth: this repository's `main` branch.
- Published baseline commit: `4e747afd26dd116c28b306c80f266925aeb54e84`.
- Prepared secure-baseline branch: `codex/phase6-secure-baseline-candidate`.
- Tree-verified secure-baseline parent: `3b152321c7ff50c60dec214d321d655834504879`.
- Protected review vehicle: public PR #17 from
  `release/v0.6.4-phase6-baseline`; the exact recut head must be reported in the
  publication handoff because a commit cannot name its own hash.
- The candidate rewrites only the committer identity metadata of the published
  Phase 5 and post-Phase-5 commits. Their source trees, messages, author identity,
  and timestamps are preserved.
- Local project name: `JARVIS Development`.
- The earlier public-history line is retained locally only as an archive branch;
  it is not an implementation target.

## Completed roadmap foundation

| Phase | Demonstrated foundation | Published commit | Sanitized candidate commit |
| --- | --- | --- | --- |
| 1 | Architecture and observability | `493d467` | unchanged |
| 2 | Task contracts and public-release safety | `54ef3ad` | unchanged |
| 3 | Verified memory retrieval | `133b743` | unchanged |
| 4 | Bounded causal strategy transfer | `2cf147b` | unchanged |
| 5 | Restart-safe long-horizon coordination | `da3f05e` | `5c0e336` |
| Post-5 | Runtime and provider-boundary hardening | `4e747af` | `998ef51` |

These entries describe merged foundations, not unrestricted autonomy or proof of
general intelligence. The exact limits remain documented in `README.md`,
`CHANGELOG.md`, `docs/EVALUATION.md`, and the relevant threat models.

## Current state

- Phase 5 is complete and published on `main`.
- Phase 6 implementation remains paused pending publication of the verified,
  privacy-sanitized baseline. Its bounded scope, threat model, evaluation contract,
  exit criteria, and rollback plan are prepared separately and remain unpublished.
- The public-history privacy finding was reproduced without exposing the detected
  value. The two affected commits have been rebuilt with their existing GitHub
  noreply author identity also used as the committer identity.
- The rewritten Phase 5 tree remains `af311e62c545a7c05de51f50046b573954f4c0b8`.
- The rewritten post-Phase-5 tree remains
  `0f589db66eff7b331b2ec8abb2ff55cdfce0293e`.
- Commit `3b152321` added only `AGENTS.md` and this status record beyond the exact
  rewritten published source trees. The recut candidate additionally changes only
  the public CI workflow, publishing documentation, two release-check scripts, and
  their two focused test modules, plus one path-length-independent hardening-test
  assertion; runtime source and package functionality remain unchanged.
- The first hosted privacy job on PR #17 failed closed because the ordinary PR range
  requires the old public tip to be an ancestor. The recut adds a separately pinned
  history-replacement path that proves ordered commit/tree/metadata equivalence,
  scans reachable identity/message metadata plus every divergent tree and blob after
  the pinned trusted common ancestor, and emits only an exact new-hash, expected-old
  lease update from a standalone public-only clone.
- A separate read-only audit of the inherited common history found ten policy matches
  across five historical blob versions in two test modules. All are synthetic
  adversarial fixtures (path, identity, and secret-scrubbing negatives), their current
  file versions are clean, and no plausible operator or private data was observed.
  Rewriting that already-public common history would expand the repair unnecessarily;
  the rewrite gate therefore treats the exact pinned common ancestor as trusted and
  exhaustively scans the divergent replacement history after it.
- Public `main`, tags, releases, and protection settings remain unchanged. The
  review-only candidate branch and PR #17 have been created.
- A read-only advertised-ref audit found five other public heads outside the
  sanitized candidate history. Three reach the affected Phase 5 commit and are open
  pull-request heads; two are divergent but do not reach that commit. Main replacement
  alone is therefore insufficient, and no ref has been deleted or rewritten.

## Local verification record

Exact parent-candidate evidence on Python 3.13.7 and Node.js 22.19.0:

- `python -m unittest discover -s tests`: 2,237 tests passed; 1 skipped.
- Branch-aware coverage run: 2,237 tests passed; 1 skipped; 77% total
  coverage, above the 75% CI threshold.
- Public-release privacy scan: passed for the sanitized reachable history and
  staged candidate snapshot.
- Gitleaks 8.30.1 directory and 26-commit Git-history scans: no leaks found.
- Presence JavaScript syntax, the CI Ruff selection, and high-severity Bandit
  checks: passed.
- Source distribution and wheel build: passed. Isolated installed-wheel entry
  point, Phase 5 fixture/runtime, CLI, workflow CLI, and public-presence health
  smoke checks: passed.
- Clean CI-equivalent dependency audit: no known vulnerabilities. The local
  project is skipped by the audit service because this candidate is not obtained
  from PyPI.
- Rewritten Phase 5 and post-Phase-5 source-tree comparisons: byte-identical at
  the Git tree level.

Release-control recut evidence before final commit:

- Focused release/privacy/publishing suite: 64 tests passed with no skips.
- An interim complete deterministic run passed 2,252 tests with one expected skip.
  Final verification must repeat after the last review fixes and on the exact recut
  candidate; this interim result is not the publication attestation.
- CI-selection Ruff, high-severity Bandit, Python compilation, and `git diff --check`
  passed on the reviewed release-control modules.

The shared host Python environment has unrelated stale packages reported by the
dependency auditor. A fresh environment containing only CI tooling and
`jarvis-local[documents]` passes; publication CI must repeat the audit from a clean
runner.

## Next verified step

1. Complete final local verification, commit the recut, update PR #17 through a fresh
   guarded disposable clone, and obtain every required hosted check on that exact hash.
2. Prepare a separately reviewed exact disposition for all five divergent advertised
   heads. Deletion or rewrite is destructive and requires explicit operator approval.
3. Obtain explicit operator approval for any time-bounded protection-policy change,
   the exact expected-old-to-approved-new lease update, and the advertised-ref plan.
4. Restore and verify protection, confirm no advertised ref reaches the affected
   commits, and revalidate an anonymous clone before beginning Phase 6 implementation.

## Remaining external gates

- Required GitHub-hosted Python 3.11, 3.12, and 3.13 tests, quality/build/audit job,
  reachable-metadata and divergent-history privacy proof, exact-range Gitleaks scan,
  and CodeQL analyses
  must pass on the exact recut PR head; local verification does not replace them.
- Current branch protection applies to administrators and disallows force pushes.
  It must not be changed without a separate, explicit, time-bounded governance
  approval and an exact restoration check.
- Clean-Windows-user first launch, sanitized demo capture, homepage configuration,
  and the operator's credential-rotation attestation remain pending release gates
  from `docs/PUBLIC_RELEASE_CHECKLIST.md`.
