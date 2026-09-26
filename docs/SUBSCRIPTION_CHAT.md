# Subscription text chat: Codex verified, Claude authentication blocked

Codex text chat has passed real multi-turn UI acceptance. Claude still requires
operator authentication; this work does **not** establish both-provider readiness.
It supersedes the earlier mock-only conversation milestone without activating
tools, memory, attachments-to-cloud, desktop, microphone, wallets or paid APIs.

## What is implemented

- Persistent HTTP/UI conversation -> serialized per-agent queue -> subscription
  transport -> progressive text/terminal response -> durable history.
- Exact project/agent/chat scoping, idempotent submission and content-bound
  outbound release digest. Only directly submitted operator text and completed
  same-segment assistant responses are eligible. Purpose, memory, file previews,
  attachments, simulator output, diagnostics and other chats are excluded.
- Changing provider or model starts a new history segment, even when subsequently
  switching back. Already admitted turns keep their original binding. No silent
  re-send of old context to a new provider.
- Secret/private-identifier screening and a bounded context limit, enforced before
  launch. Screening is defense in depth, not a universal privacy guarantee.
- Stop cancels the local transport and queued turns. Pause cancels the current
  request; explicit Resume restarts that turn with its saved authorized context.
  Restart interrupts pending work rather than silently re-sending it. Already-sent
  text cannot be recalled from a provider.
- Steering is a serialized follow-up after the targeted turn, not an instantaneous
  modification of a provider's in-flight reasoning. It is not marked applied until
  the follow-up response completes. Text work is not filesystem/tool execution.
- Provider errors are visible. A new Send retries/reconnects; there is no automatic
  paid-provider fallback or authentication modification. Real usage fields are
  shown only if returned; remaining subscription capacity stays unavailable.

## Provider evidence and blockers

### Claude CLI 2.1.211

The adapter uses the existing first-party `claude.ai` subscription login. Its
command supplies safe mode, empty built-in tools, strict empty MCP configuration,
disabled slash commands/Chrome, noninteractive deny permissions, no provider-side
session persistence, a replacement text-only system prompt and an isolated cwd.
It strips inherited API keys, provider overrides, proxy settings and parent task
context. The CLI owns authentication; this code never opens credential files.

A real synthetic attempt through the service and headless UI reached Claude.
Its initialization reported `tools: []`, `mcp_servers: []`, and
`permissionMode: dontAsk`. The CLI then returned:

> Failed to authenticate: OAuth session expired and could not be refreshed

A subsequent CLI status reported `loggedIn: false`, `authMethod: none`,
`apiProvider: firstParty`. No login/logout command was run. The operator must
reconnect the existing subscription interactively with `claude auth login`.
Then rerun the success acceptance script: no successful live answer, multi-turn
recall or streaming acceptance is claimed yet.

### Codex CLI 0.146.1: live text chat verified

`codex login status` reports ChatGPT authentication. The adapter now supports the
bundled code-mode-only model configurations: `gpt-5.6-sol`, `gpt-5.6-terra` and
`gpt-5.6-luna`. `default` explicitly resolves to `gpt-5.6-sol`; no unsupported model
is silently substituted. Only `gpt-5.6-sol` has received the live acceptance test.

The first investigation found important configuration hazards:

- `exec --help` offers ignore-user-config/rules and read-only sandboxing, but no
  complete no-tools switch. Read-only is not a prohibition on host reads.
- Generated app-server schemas describe empty environments as disabling environment
  access, but dynamic tools are additive; they do not establish that built-in tools
  are absent. App-server also lacks exec's ignore-user-config flag.
- In an empty temporary home without credentials, `debug prompt-input` plus context
  suppression yields no environment or skills blocks or original private home path.
  This prompt-input list does not attest to the actual tool registry.
- Strict exec rejects `tools.enabled=false` as an unknown configuration field.
- The installed CLI rejects `--disable view_image` as an unknown feature flag.
- Its bundled model catalog advertises a freeform apply-patch tool. Disabling shell
  alone therefore is not evidence that all local tools are unavailable. A local
  mock-endpoint capture using a generic model still exposed `view_image`.

The final adapter resolves this for the bounded supported configurations:

1. Require the verified CLI version. Obtain its public bundled model catalog under
   a fresh credential-free home and require the selected model's `code_mode_only`
   tool configuration. Pin that exact catalog for both subsequent invocations.
2. Disable the code-mode host, shell, external tools, hooks, plugins, memories,
   web search, agents, browser/computer and workspace capabilities. Suppress ambient
   environment/skills/project documentation; ignore user configuration and rules.
3. Before **each** user turn, run an offline request-shape probe against a temporary
   local HTTP test endpoint with no real credentials and a constant synthetic input.
   Require the exact model, an empty/omitted tools array and absent environment and
   skills context. Unknown versions/configurations fail closed. The local endpoint
   returns an error; it is not a model or paid provider and never receives user text.
4. Only after this passes, invoke the actual subscription CLI with the same pinned
   catalog and isolation flags. No local endpoint/provider override appears in the
   actual subscription invocation. The CLI itself owns existing authentication.

Real UI/service acceptance returned `amber` to the initial fictional-color message
and `amber` to a second-turn recall question. Reported usage: 6,276 input / 5 output
tokens on the first turn and 6,315 input / 5 output on the second. Reload preserved
both answers and showed CONNECTED. These are actual model responses, not the local
isolation probe. Tools remain unavailable; no live file-editing autonomy is claimed.

## Running and verifying

Keep runtime databases outside the source checkout. Existing private runtimes were
not migrated by this work. The disposable acceptance preview is separate from the
older operator preview; restarting it rotates its printed bearer URL.

`python -m jarvis.command_center --no-open` enables the subscription registry;
readiness is displayed independently for each provider. Choose `default` or an
exact supported model identifier. `--demo` additionally enables explicit
offline simulation; it never upgrades old simulated conversations to live context.

`python scripts/preview_subscription_chat.py` creates disposable synthetic data
with no file roots. Its printed bearer URL opens the isolated preview.
`--directory` reuses only a deliberately selected preview directory for restart
tests; never point acceptance tests at a private runtime database.

Verification commands:

```text
python -m unittest tests.test_subscription_chat tests.test_conversation_workspace tests.test_command_center tests.test_integration_harness tests.test_multi_agent_runtime
python -m unittest discover -s tests -v
node --check jarvis/command_center.js
git diff --check
python scripts/check_codex_isolation.py
```

With Playwright installed and `JARVIS_TEST_URL` set to the synthetic preview URL:

- `node scripts/verify_subscription_chat.cjs`: requires actual multi-turn answers
  and rendered persistence. With `JARVIS_TEST_AGENT=Codex Chat`, PASSED. Its default
  Claude test FAILS on real authentication, as it should; it is not live acceptance.
- `node scripts/verify_subscription_blockers.cjs`: verifies the actual Claude login
  failure renders honestly and persists after reload. PASSED. Earlier Codex blocker
  records remain historical records; they are not its current connection state.
- `node scripts/verify_live_chat_boundaries.cjs`: checks actual restart recall,
  new-chat isolation, unsupported-model refusal and fresh model segments using the
  synthetic Codex agent; PASSED with real replies (`amber`, `UNKNOWN`, unsupported
  model refusal, then `UNKNOWN`). Do not run against private history.
- `node scripts/verify_subscription_ui_contract.cjs`: local-only checks that
  steering targets running work before queued follow-ups and that the UI explains
  text-only execution. PASSED; it submits no provider request.

Final focused suite: 87 tests passed, zero skips, including cancellation before
launch and cancellation during both providers' authentication checks. The full
suite was rerun after the final transport changes; superseded earlier runs are not
counted as completed verification.

`python -m unittest discover -s tests -v` ran 4,317 tests in 918.420 seconds:
4,309 passed, one failed, seven skipped. The failure is the untouched
`test_memory_spine_integration.MemorySpineIntegrationTests.test_proposal_and_confirmation_receipts_reach_the_spine`:
it asserts that `9191` occurs nowhere in the serialized receipt, including its
SHA-256 fields. In this run, `command_sha256` contained that digit substring.
The exact targeted rerun passed. This is consistent with a digest-substring false
positive; the full run is nevertheless **not green**. No Claude-owned memory code,
test assertion or security gate was altered to hide the failure.

`python -m unittest tests.test_memory_spine_integration -v` also passed all
35 tests in 10.210 seconds. This follow-up does not replace the failed full run.

Targeted follow-up command:

```text
python -m unittest tests.test_memory_spine_integration.MemorySpineIntegrationTests.test_proposal_and_confirmation_receipts_reach_the_spine -v
```

The seven skips are three unavailable Docker checks, one absent cached benchmark
dataset and three sealed ladder/graph/retrieval holdouts without run tokens.

Official references used alongside installed CLI help:
[Codex configuration schema](https://learn.chatgpt.com/docs/config-schema.json),
[Claude CLI reference](https://code.claude.com/docs/en/cli-reference).
Installed-version checks take precedence over newer documentation fields.

No commits, publication, memory migrations or changes to Claude-owned modules.

## Handoff

Branch: `codex/command-center`, base commit `93119c4`, all work uncommitted.
This follow-up adds `jarvis/subscription_chat.py`, `jarvis/live_conversation.py`,
`tests/test_subscription_chat.py` and synthetic subscription preview/verification
scripts. It updates `jarvis/command_center.py`, `jarvis/command_center.js`,
`PROJECT_STATUS.md` and command-center documentation. Earlier uncommitted preview
files remain present; none were discarded. The copied multi-agent runtime remains
byte-identical to its starting version. The canonical checkout and original
operator runtime were not migrated or published.

Remaining action: the operator reconnects the existing Claude subscription, then
the real Claude two-turn UI test must pass. The unrelated full-suite memory test
failure is retained for its owner to investigate; no permission to edit that
workstream was inferred.
