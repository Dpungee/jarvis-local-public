# Cloud context release: first security slice

## Authorized direction

Later approved target: `AGENT_AUTONOMY_SECURITY_REQUIREMENTS.md`. Routine actions
should use scoped reusable permissions with automatic checks, not repeated human
approval of each step. The one-use release gate described below remains the
implemented first slice; it is not removed or bypassed by this new requirement.

The operator approved broad autonomy through mediated tools and sanitized cloud
context, initially using subscription CLI providers. Local models may be selected
later, without automatically gaining additional permissions. Working memory stays
local. Cloud models cannot receive unrestricted private memory. Credentials and
wallet/private keys must not enter model context. No absolute no-leak guarantee
is claimed; detection alone cannot establish that arbitrary text is safe.

This decision supersedes the earlier documentation-only stop for non-memory
security implementation. It does not constitute Claude's acceptance of the
reconciled memory contract, authorize a Claude assignment, or activate a production
migration. Memory implementation remains a separately owned workstream.

## Delivered boundary

`jarvis.cloud_release.CloudReleaseGate` provides host-only, short-lived approval
of an exact serialized message snapshot. Approval requires a matching digest from
the trusted review path; calculating a digest is not authorization. Only that
trusted host path may call approve. Never expose the gate to a model tool.

Tickets are opaque, bounded to one opaque agent ID and exact CLI model name,
expire after at most five minutes, can be revoked and are consumed once under a
lock. Restart invalidates them. Consumption precedes transport: ambiguous failure
requires a new approval, never restoration of the old ticket. Revocation prevents
future consumption; it cannot retract an already dispatched request.

Candidate messages use a closed text-only schema. Attachments, tool results,
extra fields and arbitrary request kwargs are excluded in this first slice.
Existing secret/private-identifier screens plus conservative private-key, seed,
IPv4 and MAC markers reject recognized sensitive context. These screens have
false positives and false negatives: they do not detect every personal name,
network identifier, encoded secret or wallet seed. The trusted operator must
review the full candidate, not merely accept a successful screen result.
Sanitized derivatives must be prepared locally before that review; this module
does not automatically redact arbitrary private memory into a safe prompt.

`ModelClient.chat_released` consumes the ticket and sends only its frozen messages,
with no tool schemas or attachments, through the existing bounded CLI adapters.
It gives each release an independent model-conversation scope to prevent inherited
continuation context. Returned model text is advisory; this method executes no
actions. Responses and any proposed tool operations require their own future
broker validation. Ordinary `chat` and streaming paths remain unchanged.

## Threat model and limitations

The trusted Python host and provider adapters are inside the trust boundary.
An attacker able to execute Python in that host can bypass this object. This is
not an OS process sandbox or a network firewall. Existing CLI isolation controls
are reused; live installed CLI behavior was not exercised by the unit tests.
No login, credential read, real cloud call, provider installation or live database
operation was performed for this slice.

Do not activate autonomous cloud execution on the strength of this module alone.
Raw provider clients must remain outside the eventual agent surface. All context
and tool-result routes must be mediated, tools must enforce per-action authority,
and filesystem/network containment must be verified. A private local model's
result cannot become cloud-safe merely by changing its model binding.

## Verification and remaining work

Verification on this worktree (2026-09-16): 104 release/provider tests passed;
150 combined release/provider/authority/runtime tests passed with no skips.
Complete suite: 4,270 run in 704.648 seconds, exit 0, `OK (skipped=7)`;
4,263 non-skipped tests passed. Seven unclosed-SQLite ResourceWarnings were
observed. Several informational timing budgets were exceeded with enforcement
disabled. Skips are not counted as passes; no timing or safety gate was weakened.

Commands (prepared worktree interpreter):

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_cloud_release tests.test_model_client
.\.venv\Scripts\python.exe -m unittest tests.test_cloud_release tests.test_model_client tests.test_agent_authority_gates tests.test_multi_agent_runtime
.\.venv\Scripts\python.exe -m unittest discover -s tests
.\.venv\Scripts\python.exe -m ruff check jarvis/cloud_release.py tests/test_cloud_release.py
.\.venv\Scripts\python.exe -m bandit -q jarvis/cloud_release.py -lll
git diff --check
```

New-file lint, high-severity Bandit and whitespace checks passed. Whole-module
Ruff for model_client.py reports 26 existing findings, matching baseline count
and code/message multiset; no new lint finding remains. Staged preparation diff
and hashes of the existing runtime module/tests and Claude review/handoff were
unchanged. No commit or publication.

Focused tests cover exact snapshot preservation, actor/model mismatch, expiry,
revocation/restart, recognized sensitive content, closed schema/size limits,
concurrent single consumption, no added tools/context, conversation separation
and failure without approval restoration. Test providers are mocks; passing these
tests is not evidence of end-to-end leak prevention.

Next steps: connect a restricted autonomous executor to this release path, add
trusted review/audit UX, mediate each tool/result transition, verify process and
network containment, and then integrate the accepted local-memory bridge. Do not
store approval tickets in prompts, general logs, memory or shared artifacts.
