# JARVIS Development Status

## Protected credential follow-up candidate (2026-09-28, not yet published)

- PR #31 published the reviewed 132-file candidate on a release branch, not `main`.
  Its final local suite passed 5,793 tests in 1,781.896 seconds with eight skips and
  77% displayed branch-aware coverage. Repeat distributions were byte-identical;
  installed wheel/source checks, privacy, and source/history secret scans passed.
- Hosted CodeQL then identified clear-text connector credential persistence. The
  finding was not dismissed and PR #31 was not merged. Review also found the new
  OpenRouter key store used plaintext. Both stores now use authenticated encryption
  with Windows-account key protection, path/purpose binding, ciphertext-only scratch
  files, verified atomic replacement, and fail-closed migration. Independent review
  caught a short-write durability bug; its repair preserves legacy files on failure.
- Independent synthetic storage/connector/provider verification passed 103 tests;
  additional native Windows protection and short-write checks passed. The original
  connector-authorization regressions passed 23 tests after removing an obsolete
  permissions-only fixture mock. No real credentials or running Hub state were read,
  migrated, or modified. Credential enforcement is protected from automated repair.
- Windows-only storage and path binding require reconnecting after a profile move
  or account change. Same-account/privileged access, whole-file rollback, old backups,
  and previously written disk remnants are not covered by this protection.
- This follow-up requires a fresh full suite, coverage, package/install, privacy,
  and hosted acceptance before protected publication. No gates are waived.

## Earlier clean publication candidate (2026-09-28)

- The candidate starts from public `main` commit `0b3efca` and integrates the
  newest 98-path source snapshot: conversational Hub updates, connectors,
  uploads/images/team workflows, provider support, onboarding, reliability tools,
  and their tests. Original development checkouts and the running Hub are retained.
- Review found connector authorization, ambiguous-retry, redirect, and stored-
  permission migration defects. Repairs and adversarial regression tests are now
  implemented; these capabilities are not release-approved yet. No live services,
  external model calls, or operator desktop control are used by this review.
- Previously verified promotion-transition and test-synchronization corrections
  are included. Hosted failures also require a deterministic schedule fixture and
  bounded browser startup-marker recovery without changing runtime deadlines.
- The new-feature batch passed 176 tests. Selected Ruff, JavaScript syntax,
  compilation, high-severity Bandit, and 15 package/lock contract tests passed.
  A combined Hub/permission/connector/startup run passed 135 tests; the connector
  review separately passed 35 tests, and the scheduling/preview/clock/startup review
  passed 87. Source and wheel distributions built successfully. Synthetic example
  privacy findings were corrected without scanner exemptions; the whole privacy
  check must run again. These are focused checks, not final-tree full-suite or
  hosted acceptance. The diagnostic full suite completed 5,747 tests with two
  permission-expectation failures and eight skips. The outdated expectations now
  require explicit grants; their 27-test group passed. A final exact-tree run is
  required after remaining integration finishes.
- The operator chose to retain the newer guided installer and integrate compatible
  older improvements. The old virtual-environment-only installer and obsolete lock
  set will not replace the current setup and dependency policy.
- Ported archive normalization and sanitized release-baseline tooling. Their
  package/lock/baseline group passed 33 tests; repeat wheel and source builds were
  byte-identical. Both artifacts installed outside source and exposed all six
  expected commands and Hub resources. The staged privacy check and clean-source
  Gitleaks scan passed before the subsequent recovery port.
- Ported online SQLite backup/isolated restore checks and sanitized recovery
  receipts against current memory. A staging-file collision now preserves files
  not created by the drill; recovery enforcement is immutable to self-repair.
  Final review passed 36 recovery/diagnosis tests and 63 memory-spine tests, including
  full-table preservation of existing memory and authority state. CLI retention
  wiring is complete. Execution-boundary integration passed 98 focused tests with
  actual-handle/result receipts and honest direct-process training-export labels.
  The original structural manifest is retained with three narrowly pinned reviewed
  process-method exceptions. Root CLI/package/baseline checks passed 103 tests.
- Current locked runtime dependencies have no known vulnerabilities in the audit.
  The 98 source-snapshot files were rechecked and have not drifted. Older launcher,
  scheduled-task and runtime-lock files coupled to the rejected venv-only installer
  remain superseded rather than being copied over the current guided setup.
- The first assembled coverage run completed 5,792 tests in 1,923.460 seconds:
  three graph-semantic failures, eight environment-dependent skips, and 77%
  displayed branch-aware coverage. Two failures explicitly exhausted the query
  deadline; all 175 graph-module tests passed in isolation under coverage. The
  three content-contract cases now use a controlled clock, with an additional
  regression checking completion before 25 ms and refusal at/after 25 ms. Runtime
  deadlines and real-clock integration measurements are unchanged. Full exact-tree
  acceptance must be repeated; this failed run is not release approval.
- Next: run final exact-tree coverage, privacy and package/install checks, then
  publish through a new protected pull request and verify its hosted checks.
  Failed PR #30 is not merge-approved. No gates or coverage thresholds are waived.

## Public integration and release verification (2026-09-26)

- The reviewed 274-path integration was published through protected PR #26 as
  `63d3f82`. The exact candidate passed 5,481 local tests (nine skips), all three
  hosted Windows Python versions (ten skips each), 76% displayed branch-aware
  coverage, package/install checks, dependency audit, privacy, and CodeQL.
  A fresh public-only clone matched the reviewed tree and passed privacy/secret
  scans. The repository URL is unchanged and commit email identities are no-reply.
- Post-merge CodeQL identified a medium-severity all-interface listener in a
  synthetic network-exposure test. The test now retains real loopback-only
  validation and uses mocked connection boundaries for reachable/refused non-
  loopback addresses and resolution failure. No LAN listener is opened by that
  fixture; production detection, query scope and security gates are unchanged.
  This follow-up still requires its own complete checks and alert-resolution
  verification; no finding was dismissed.
- The published integration's Python 3.12 post-merge run hit a recurring 30-second
  Node digest-fixture timeout. The log did not identify the stalled phase; 100
  consecutive local invocations passed. The fixture now writes phase/result data
  to an owned temporary file, waits directly on the process rather than pipe EOF,
  and explicitly exits after synchronous result persistence. Real WebCrypto, the
  30-second limit and all four payload-binding cases are retained. Additional
  contracts reject incomplete/invalid results and preserve timeout diagnostics.
  The 17-test browser-security group and 100 real digest invocations passed locally;
  hosted acceptance remains required, and the original timeout cause is unconfirmed.

- This candidate is based on published commit `93119c4`. Original development
  checkouts and the running Hub were not changed by this integration.
- Combined the current agent-first Hub with local coding context and streaming,
  provider settings, native desktop/transcript work, Council worker safeguards,
  companion/privacy work, bridge/calibration tooling, and dependency locks.
- Browser approvals now bind the complete payload; sensitive field values are
  suppressed, current tool grants are rechecked, and wrapped tool execution cannot
  bypass host enforcement through concurrent fetch dispatch. These are regression-
  tested controls, not deployment-specific network-containment acceptance.
- A 528-file source snapshot passed Gitleaks and built both source and wheel
  distributions. Focused suites passed: agent/compaction/learning (254), Hub browser
  security (35), concurrent dispatch (4), context/provider UI (8), and bridge/UI/
  cloud-release fixtures (118). Later desktop/UI/bridge checks passed 207 tests,
  the agent authority/governance/hardening group passed 352, and benchmark command
  checks passed 28. The installed wheel passed entry-point/resource/CLI smoke tests
  outside the source tree. High-severity Bandit, selected Ruff, the staged public
  privacy check, and the locked runtime dependency vulnerability audit passed.
- The initial complete run executed 5,377 tests in 919.189 seconds with 5 failures,
  18 errors, and 9 skips. Reporting/benchmark regressions were repaired and focused
  checks passed; source line endings were normalized. Changes made during that run
  invalidated sealed runtime fingerprints, so the fingerprints were regenerated
  without changing scorer/outcome/threshold fields. All 62 affected reseal,
  strategy-transfer, operator, and long-horizon checks then passed in 82.954
  seconds. A fresh complete run is still required; this is not final-tree
  full-suite acceptance.
- The Agent, Memory, and ToolBox structural split is now integrated by extracting
  current implementations, not copying older behavior over governed memory. AST
  comparisons cover 508 Memory methods, 152 ToolBox methods, 12 Agent helpers, 11
  stages, and 121 unchanged Agent methods. Runtime fingerprint registrations and
  immutable repair boundaries include the new domains. Sealed fixtures were
  regenerated with digest-only checks; scoring rules and thresholds are unchanged.
- Independent review reproduced a preexisting browser-check false positive: input
  response could conceal failed app-state inspections or missing controls. The
  verifier now rejects failed or skipped requested checks, with adversarial tests.
- Product title: **JARVIS — Local AI Agent Platform**. The GitHub description now
  describes persistent chats, multi-agent coordination, governed memory, and scoped
  tools. The repository URL and `jarvis-local` package name are unchanged.
- Both GitHub email-privacy settings were checked and enabled; publication uses the
  approved no-reply author and committer identity. This does not replace code,
  history, package, privacy, or hosted CI checks.
- Hosted CodeQL on the initial candidate found two Hub route-to-HTML flows. Route
  segments now use URL encoding before sidebar/composer link rendering; three
  adversarial/compatibility tests and the 26-test focused Hub group passed. The
  corrected candidate still requires its own hosted checks; no alert was dismissed.
- The initial candidate was uploaded as pull request #22, but is not approved for
  merge: its hosted CodeQL findings require a corrected candidate. Public `main`
  remains unchanged. The corrected route tests now cover four cases.
- A prior full run completed 5,377 tests with one browser input-attribution failure
  and nine skips. Input verification now flushes animation frames without advancing
  timers after a quiet control interval; all 24 preview/clock regressions pass,
  including unbound keys at several timer phases.
- Coverage now collects ordinary Python subprocesses and combines only the current
  run's data. Sources, branch measurement, and the 75% gate are unchanged. Restricted
  worker environments remain unchanged. Six isolated worker contract tests, ten
  offline outcome-runner tests, 23 runtime contracts, and 16 bridge/workload tests
  passed before integration. CI timeouts allow collection and package checks
  to complete without relaxing correctness or security gates.
- Review exposed legacy-runtime request-ID collisions across agents and invalid
  truncated event JSON. Retries now bind agent/chat and explicit request settings;
  oversized details become a bounded, valid truncation record. Added regressions
  retain same-scope idempotence and fail closed on changed scope/settings.
- Hosted multi-version checks exposed wide Windows file IDs that exceed SQLite's
  integer range, Tcl object rendering differences, and version-dependent AST dumps.
  Wide identity values now retain exact tagged decimal text, with substitution and
  malformed-storage tests. AST tests preserve every recorded digest through one
  stable formatter; no method-body baseline was regenerated. Windows ACL tests
  compare the complete canonical DACL rather than assuming a textual SID spelling.
- A 336-test learning-ladder/graph/structural run passed (one skipped) after removing
  leaked inherited-method patches and controlling the fan-out test clock. Existing
  deadline-enforcement tests and runtime time budgets remain intact. Final combined
  regression, coverage, packaging, and hosted checks are still pending.
- Corrected candidate `f0bce7f` (PR #23) passed the local complete suite: 5,474 tests
  in 1,492.306 seconds, nine skipped, and 75.867% combined statement/branch coverage.
  Current-run coverage combined 54 files (22 duplicate shards skipped). Hosted
  privacy and all CodeQL analyses passed, clearing the initial route-to-HTML findings.
  Its Python 3.11 hosted run still failed: a Node fixture startup timeout, unstable
  message ordering when timestamps tie, and an unstable browser quiet-frame check.
  This is not merge acceptance; follow-up fixes require a new exact-tree run.
- Follow-up corrections preserve creation order for equal-timestamp messages using
  their persisted event sequence (no schema migration), and render browser control
  observations at their actual virtual-clock endpoint. Targeted regressions reject
  unbound keys and frame-count motion; ordering tests force reverse-sorted IDs and
  verify restart, filtering, acknowledgement, and room-cursor behavior. The 32-test
  multi-agent and 162-test bridge suites passed in isolation. The Node fixture has
  bounded cold-start headroom without retries or changed payload assertions. The
  outcome-semantic test now controls its clock and explicitly checks deadline refusal.
- Candidate `c76bec2` (PR #24) passed all three hosted Windows Python versions,
  privacy, and CodeQL. Its local run executed 5,479 tests with nine skips and one
  attachment-menu test failure; combined coverage was 75.864%. The test assumed
  no real-pointer hover during the initial UI event pump. Its keyboard setup now
  selects the starting row explicitly; production hover behavior is unchanged.
  All three focused attachment-menu tests passed. A new exact-tree complete run
  and hosted checks are required; prior results are not final acceptance.
- Candidate `9a2a426` (PR #25) passed 5,479 local tests in 1,040.475 seconds
  (nine skipped), hosted Python 3.11/3.13, privacy, and CodeQL. Hosted Python 3.12
  exposed a subscription pause race: transport cancellation could record FAILED
  before operator intent reached storage. Pause/Stop now commit their state before
  signaling cancellation, and cancelled or already-ended turns do not mark the
  provider broken. Two forced-interleaving regressions failed on the old code and
  pass with the fix; the complete 89-test conversation/runtime group passed.
  This is not final-tree or publication acceptance; full checks remain required.
- Next: final-tree full regression and coverage, package/install smoke tests, privacy
  scans, then a corrected protected publication candidate. Synthetic browser checks do not
  establish live provider, live desktop, or production deployment acceptance.

## Agent-first chat correction (2026-09-25, unpublished)

- Active operator scope: agent capabilities behind a normal chat interface, not
  tool-free conversation. The previous interpretation below is superseded.
- Chat messages now enter the existing permission-enforced Agent executor. An
  additive `hub_chat_turns` table atomically binds message request IDs to runs;
  identical retries cannot duplicate execution. Context is scoped to the chat,
  includes earlier text-only exchanges and is reconstructed when a queued turn runs.
- UI keeps folders and multi-turn chats, shows tool activity/files/approvals inline,
  exposes Stop/Continue, and accepts new messages while the agent is busy. Existing
  grants, model bindings and records are preserved; no provider-native tools enabled.
- Fixed the privacy false positive by scanning conversation prose in overlapping
  bounded windows instead of passing the whole conversation to a 512-character
  entity-identifier screen. Secret, private-path and opaque-value refusals remain.
- Port 8790 was restarted while idle after backing up state. Existing records
  compared unchanged. Authenticated live browser inspection confirms the AI-agent
  interface and actual grant labels. No failed user message was automatically replayed.
- Focused executor/context/idempotency/isolation/restart checks and privacy edge
  tests passed individually. Combined focused and fresh full regression are running.
  Live external tool execution has not been tested by this change; execution tests
  use mocks. The earlier tool-free baseline full run passed 4,372 tests (7 skipped)
  in 987.150 seconds and is not evidence for this newer executor change.

## Superseded tool-free chat interpretation (2026-09-25, unpublished)

- Latest scope supersedes the earlier task-style conversation: the main composer
  now sends persistent, scoped, tool-free subscription chat, never task submissions.
  Saved chats support follow-ups and reload; project folders and original task
  history remain. Chat settings preserve existing task permissions and instructions.
- Port 8790 runs the new chat routes against the original state. Pre/post-restart
  agent, settings, task, chat, message and project records matched the backup.
- Synthetic browser checks passed at four widths, including multi-turn submission,
  reload, draft retention and absence of task controls/API calls. Authenticated live
  page checked independently; one real Claude text-only response succeeded.
- Focused regression: `python -m unittest tests.test_agent_hub_chat
  tests.test_subscription_chat tests.test_agent_hub_recovery` passed all 69 tests
  in 176.449 seconds. That baseline's full regression later ran 4,372 tests,
  with 7 skipped and no failures. No publication,
  delegation, paid API fallback, credential inspection or tool grants were added.

## Agent Hub recovery (2026-09-24, unpublished)

- Restored the original-state-compatible runtime under `agent_hub_runtime.py` and
  explicitly bound the Hub to it. The conflicting `agent_runtime.py` and provider
  module remain untouched. No original-state conversion or replacement was needed.
- Reconciled provider executable/profile selection and explicit tool-group payloads;
  retained authenticated audit identity and fail-closed permissions. Added schema
  refusal and artifact integrity verification.
- Port 8790 is restored against the original saved state. Authenticated browser
  access and the original saved agent were verified in the new chat-first UI.
  Saved records and permission flags compare unchanged; no live task was submitted.
- Recovery tests: 12 passed; final combined focused regression: 51 passed.
  Full suite: 4,343 run in 748.639 seconds, 4,336 passed, 7 skipped, no failures.
  The two tests added after full discovery passed in the later focused run. See
  `docs/AGENT_HUB_RECOVERY.md` for exact scope, limitations and verification.
- No commit, push, publication, provider reauthentication or permission expansion.

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

## Command-center workstream (unpublished)

- A dedicated command-center branch contains a loopback-only operator UI,
  persistent multi-agent control-plane foundation, per-agent queued executor, and
  inert typed integration contracts for MCP, plugins, desktop/browser, API
  brokering and wallet intent validation.
- Codex CLI, Claude CLI, external integrations, memory bridge, tools, credentials,
  wallets and network access remain disabled. The executor is verified only with
  an injected offline provider; this is not a claim of live operational autonomy.
- Before activation: independently review provider adapters, add controlled egress
  and broker/signer implementations, enforce reusable grants at every adapter,
  reconcile deletion/retention, and complete adversarial containment testing.
- Current verification: the focused command-center, integration-policy and
  multi-agent runtime run passed 37 tests with no skips. The complete deterministic
  repository suite passed 4,267 tests with 7 expected skips. A disposable loopback
  preview was rendered headlessly at 1440 by 1100, and its create/start/assign/run
  path completed through the explicitly simulated offline adapter.

## Conversational workspace revision (unpublished)

- Replaced the primary task form with agent-first navigation and persistent
  per-agent/project chats. Added independent chat/draft restoration, explicit
  discussion/new-work/steering/reply handling, checkpoint acknowledgements,
  pause/resume/stop, and interrupted-work recovery after restart.
- Composer includes local text attachments, scoped local permission grants and
  revocation, future model selection, unavailable token-capacity reporting and an
  explicitly unavailable dictation control. No microphone or remote speech service
  is used. Files require an explicit startup root and an agent/project grant.
- Delegation requests record an exact readback and later approval but do not
  dispatch. All responses, clarification and work execution remain an explicitly
  labeled offline simulation. Codex/Claude live chat and external tools remain gated.
- `python -m unittest tests.test_conversation_workspace tests.test_command_center
  tests.test_integration_harness tests.test_multi_agent_runtime`: 57 tests passed,
  zero skips. `python -m unittest discover -s tests -v`: 4,287 tests run in
  815.375 seconds, 4,280 passed and 7 skipped, no failures. Skips: three unavailable
  Docker checks, one absent cached dataset, three sealed holdouts without tokens.
- Both `node scripts/verify_conversation_preview.cjs` and
  `node scripts/verify_composer_boundaries.cjs` passed against disposable synthetic
  data in headless Edge. They cover active work, clarification, steering, pause,
  reload, resume, stop, agent/chat/draft restoration, project/file navigation,
  attachment staging/removal/revocation, grant isolation, model selection and
  unavailable dictation without microphone access. Responsive overflow checks
  passed at 1440, 900, 760 and 430 pixels; the desktop rendering was inspected.
- Targeted Ruff, JavaScript syntax and `git diff --check` passed. No commits,
  publication, live provider activation or Claude-owned source changes were made.
- Architecture, limitations, startup and exact verification commands are recorded
  in `docs/CONVERSATIONAL_WORKSPACE.md`. Next gate: a reviewed, authorized live
  conversational adapter with payload release and deployed containment evidence.

## Subscription text-chat follow-up (unpublished, Claude authentication blocked)

- Added a durable text-only live queue, progressive response persistence, scoped
  history release, provider/model epochs, cancellation, queued steering and explicit
  restart/resume semantics. UI displays provider errors and real usage when supplied.
  No arbitrary tools, private memory, attachments-to-cloud or paid API fallback.
- Claude CLI's actual synthetic service/UI attempt initialized with zero tools and
  zero MCP servers, then failed: OAuth session expired and could not be refreshed.
  Subsequent status reports no active subscription login. Operator reconnection is
  required; no successful live answer or multi-turn acceptance is claimed.
- Codex now passes actual two-turn UI/service acceptance and reload persistence.
  A pinned bundled code-mode-only catalog with the execution host disabled is
  verified against a credential-free local tool-registry probe before every turn.
  Unsupported versions/models fail closed. Default resolves to `gpt-5.6-sol`.
  This is subscription text chat, not filesystem or external-tool execution.
- Final focused command covering subscription chat, conversations, command center,
  integration policy and runtime: 87 tests passed, zero skips. Targeted Ruff,
  JavaScript syntax and diff whitespace checks pass. Final full regression command
  ran 4,317 tests in 918.420 seconds: 4,309 passed, one failed, seven skipped.
  The untouched memory-spine receipt test rejected `9191` occurring inside its
  `command_sha256` digest. Its exact targeted rerun passes; no memory test or gate
  was changed. The full run is not reported as green.
  `python -m unittest tests.test_memory_spine_integration -v` then passed all
  35 tests in 10.210 seconds; that rerun does not erase the original failure.
  Skips: three unavailable Docker checks, one absent cached dataset, three sealed
  ladder/graph/retrieval holdouts without their run tokens.
- Headless real blocker-path and actual service-restart/reload checks pass.
  Codex success-path live acceptance passes. Claude remains blocked on authentication.
- Additional real Codex UI checks pass: recall after service restart, no history in
  a fresh chat, refusal of an unsupported model, fresh context after switching back
  to default, and reload persistence. The original operator runtime was not migrated.
- See `docs/SUBSCRIPTION_CHAT.md` for exact evidence, controls, commands and next
  gates. No commits/publication, live memory migrations or Claude-owned edits.
