# JARVIS Development Status

Updated: 2026-09-02

## Authoritative baseline

- Public source of truth: this repository's protected `main` branch.
- Current published release: `v0.6.3` public preview.
- The published baseline includes the completed Phase 1-5 foundations,
  post-Phase-5 runtime/provider hardening, and the first governed project-memory
  M1 slice described in `docs/GOVERNED_PROJECT_MEMORY.md`.
- These are measured, bounded foundations. They are not unrestricted autonomy,
  proof of general intelligence, or a claim that the memory architecture is novel.

## Current cleanup milestone

- `codex/release-gate-hardening` is a preservation-first successor to the stale
  pre-Phase-6 release-hardening review. It starts from the current `main` tip and
  carries the reusable release/privacy controls forward without reverting governed
  project-memory work.
- The earlier review branch and pull request remain intact until this successor has
  been safely published, reviewed, and verified. They are not a source of truth for
  runtime development.
- The successor changes repository guidance, CI/release controls, publishing
  documentation, privacy/publish-source checks, and adversarial tests. It does not
  change packaged runtime behavior.
- The publish-source guard binds candidate and tag operations to exact refs in
  disposable public-only clones. It rejects unexpected roots, refs, remotes,
  alternates, unreachable objects, broad push modes, configured push refspecs, and
  mismatched destinations.
- Standing CI uses only the ordinary exact range for protected pull requests and
  `main` pushes. Manual verification is accepted only for `main`. No prior incident
  branch or commit pin and no divergent-history path remain wired into normal CI.
- No generic history-replacement or force-push mode is shipped. A future incident
  requires fresh one-off reviewed tooling and explicit authorization for each ref.
- The current `main` handling for vendor-managed no-reply co-author trailers and the
  exact-file path parsing regression remain present after the port.
- Repository housekeeping has removed 18 stale workflow runs and 12 stale CodeQL
  analysis caches. All currently open pull requests are drafts and therefore remain
  review-only work, not merge authority.

## Local verification record

Verification was performed on Windows with Python 3.13.7 and Node.js 22.19.0:

- Focused public-release and publish-source pair: 56 tests passed, no skips.
- Focused release/privacy/agent-hardening suite: 201 tests passed, no skips.
- Complete deterministic suite: 2,370 tests passed, 4 expected skips.
- Exact-commit privacy, Gitleaks, static-analysis, YAML, diff, and Git-object evidence
  belongs in the task handoff and successor pull request rather than this rolling
  status record.
- Local results do not replace the required hosted checks on the exact pushed commit.

## Publication prerequisite

- GitHub's account-level private-email protection and command-line private-email push
  block were both verified enabled on 2026-09-02 before successor publication.

## Next verified step

1. Push only the successor branch and open a protected pull request to `main`.
2. Require all six protected hosted contexts on that exact head before merge
   consideration: secret/privacy, Windows Python 3.11, 3.12, and 3.13,
   quality/distribution/dependency audit, and the aggregate CodeQL gate backed by the
   Actions, JavaScript/TypeScript, and Python analyses.
3. Use only squash merge after review, then verify the new commit's tree and identity
   and obtain all six checks again on post-merge `main`.
4. Preserve the earlier review until the successor is green; only then decide its
   explicit close/delete disposition.

## Remaining external follow-up

- GitHub-hosted checks on the exact successor head remain required.
- Clean-Windows-user first launch, a sanitized demonstration capture, and the
  operator's credential-rotation attestation remain pending follow-up items from
  `docs/PUBLIC_RELEASE_CHECKLIST.md`; their status is not implied by publication of
  `v0.6.3`.
- Removal of historical GitHub objects that are no longer reachable from advertised
  refs requires provider-side support; source scans cannot attest to provider garbage
  collection or credential rotation.
