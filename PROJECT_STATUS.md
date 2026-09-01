# JARVIS Development Status

Updated: 2026-09-01

## Authoritative baseline

- Public source of truth: this repository's `main` branch.
- Published baseline commit: `4e747afd26dd116c28b306c80f266925aeb54e84`.
- Prepared secure-baseline branch: `codex/phase6-secure-baseline-candidate`.
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
  privacy-sanitized baseline and a separate bounded Phase 6 design, threat model,
  baseline evaluation, measurable exit criteria, and rollback plan.
- The public-history privacy finding was reproduced without exposing the detected
  value. The two affected commits have been rebuilt with their existing GitHub
  noreply author identity also used as the committer identity.
- The rewritten Phase 5 tree remains `af311e62c545a7c05de51f50046b573954f4c0b8`.
- The rewritten post-Phase-5 tree remains
  `0f589db66eff7b331b2ec8abb2ff55cdfce0293e`.
- `AGENTS.md` and this status record are the only candidate-tree additions beyond
  the exact rewritten published source trees.
- No public branch, tag, release, or remote has been changed by this preparation.

## Local verification record

Fresh local evidence on Python 3.13.7 and Node.js 22.19.0:

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

The shared host Python environment has unrelated stale packages reported by the
dependency auditor. A fresh environment containing only CI tooling and
`jarvis-local[documents]` passes; publication CI must repeat the audit from a clean
runner.

## Next verified step

1. Complete and record every local verification gate above.
2. Review the exact candidate commit and old-to-new hash mapping with the operator.
3. Obtain explicit operator approval for the one exact, lease-protected public
   history replacement; do not push or alter branch protection beforehand.
4. After publication and remote CI validation, begin Phase 6 only from the verified
   sanitized baseline and its separately reviewed design and threat model.

## Remaining external gates

- Required GitHub-hosted Python 3.11, 3.12, and 3.13 tests and CodeQL analyses can
  run only after a candidate ref is published; local verification does not replace
  them.
- Clean-Windows-user first launch, sanitized demo capture, homepage configuration,
  and the operator's credential-rotation attestation remain pending release gates
  from `docs/PUBLIC_RELEASE_CHECKLIST.md`.
