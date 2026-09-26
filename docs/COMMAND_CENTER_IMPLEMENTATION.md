# JARVIS Command Center: first usable slice

## Active live-chat scope (supersedes earlier preview-only gates)

Implement subscription-authenticated Codex CLI and Claude CLI text chat, with
durable per-agent/project/chat history, progressive responses, exact model binding,
cancellation and safe queued steering. Verify synthetic multi-turn responses
through the HTTP service and rendered UI. Existing login may be used by the CLIs;
no credential extraction, paid API substitution or auth/billing changes.

Only explicitly submitted text and same-binding authorized conversation history
may leave the service. Provider/model changes establish a new history segment.
Tools, ambient repository instructions, memories, attachments, microphone,
external MCP/plugins, desktop, wallets and arbitrary file access stay disabled.
If isolation cannot be established, show a concrete provider blocker. No further
delegation, commits, publication or live memory-store migration. Preserve sibling
Claude/computer-use ownership. Earlier simulation evidence is not live acceptance.

Current evidence: Codex text chat passes real multi-turn, restart and isolation
checks in the synthetic UI. Claude is blocked by expired subscription login.
The final full suite has one untouched memory-test digest-substring failure;
focused transport tests pass. See `SUBSCRIPTION_CHAT.md` for exact evidence and
remaining gates. Do not describe the entire system as fully ready.

## Approved conversational revision

The primary surface becomes a persistent project/agent conversation. Discussion,
new work, steering, clarification replies and delegation proposals have explicit
message types; only New work creates an execution. A composer remains available
during execution. Delivery and application are separate durable acknowledgements.
An offline checkpoint simulator exercises this contract without calling a model.
Each simulated reply is labeled; live conversational providers remain unavailable.

The acceptance scenario starts work, answers an in-thread clarification, steers
the existing run without resetting its checkpoint, pauses/resumes/stops, switches
projects, inspects a synthetic artifact, and reloads/restarts with history intact.
Restart interrupts active work and requires explicit resume. Delegation proposals
require an exact readback followed by approval; dispatch remains unavailable.

The approved layout now starts with an agent selector, New Agent, and a distinct
New Chat. Each agent restores its selected project/chat/draft independently.
Composer controls include model binding, explicit attachment staging/removal,
scoped local permissions, Send, and truthful usage/dictation status. No tokenizer
or model capacity is configured, so token usage is unavailable; character counts
are labeled as such. Dictation is unavailable without a verified local adapter;
the browser never requests a microphone or calls remote speech recognition.
Attachments are explicit bounded text selection, locally staged under the exact
project/agent/chat, screened for secrets, and never sent to a provider. Local
file-preview and attachment grants are operator toggles enforced by the API;
cloud release, shell, network, wallet and delegation remain non-grantable here.

Projects have explicitly registered read-only roots, configured at startup only.
No roots are inferred from project names or accepted from HTTP. Browse/read paths
reject traversal, links/reparse points, credential paths and unsupported file types;
secret screening is additional protection, not a universal privacy guarantee.
Synthetic artifacts are local database records. File changes remain unavailable
until a scoped change adapter exists. No memory bridge or Claude-owned file changes.

Visual generation remains authorized for non-private UI assets through available
connected providers; this revision uses local CSS and makes no asset-provider call.

## Outcome and exit criteria

Build a loopback-only command center that lets an operator create persistent
agents, select a supported subscription provider/model label, start/pause/stop
each identity, assign work, inspect queued/running/terminal task state, and read
truthful tool/permission/provider status. The UI must never represent queued
work as running or simulated data as live.

The slice is usable when:

1. lifecycle and task mutations persist through restart in the non-memory runtime;
2. each running identity owns a separate serialized execution queue and receives
   only its own task/prompt plus its declared purpose;
3. execution can be proved with an injected offline provider, while Codex CLI and
   Claude CLI remain explicitly disabled until a later operator-approved live test;
4. loopback HTTP requires an unguessable bearer token for state-changing and API
   reads, rejects foreign origins, and does not expose the token in rendered HTML;
5. focused tests, the full suite, and a browser-level HTTP smoke check pass.

## Architecture

- `MultiAgentRuntimeStore`: stable identities, lifecycle, model binding, tasks,
  event audit, and deterministic policy records. It remains separate from M0-M5
  memory and does not activate the memory bridge.
- `CommandCenterService`: operator API and execution admission. It exposes no
  arbitrary shell or filesystem tool. Provider output is advisory text stored as
  a task result only after the bounded adapter returns.
- `AgentExecutorPool`: one logical FIFO lane per identity over a configurable
  worker pool. There is no fixed agent-count cap; concurrency is a runtime capacity
  setting and excess work is visibly `QUEUED`.
- `CommandCenterHTTPServer`: loopback-only preview with bearer-token and Origin
  checks, closed JSON schemas, bounded request bodies, and security headers.
- Static HUD UI: reads real API state, labels disabled/demo states, and uses motion
  only for navigation and state changes. It has no invented utilization metrics.

## Provider and security gates

The provider registry recognizes `codex-cli` and `claude-cli`, but this preview
does not invoke either. Their status explains that live subscription execution
requires a separate operator-approved activation/test. `offline-demo` exists only
when the server is started with `--demo`; it is visibly marked simulated and is
not the default. Tests inject an in-process provider and make no network call.

No agent process receives host shell, private memory, credentials, direct network,
wallet, publication, deployment, migration, or security-policy authority. Tool
status is `none granted` in this slice. Provider activation, tool adapters, memory
bridge activation, deletion/retention reconciliation, reusable grants, and network
containment remain later gates.

## General-purpose harness foundation

The first slice also publishes closed descriptors for MCP servers, plugins,
desktop applications, browser/web work, credential-backed APIs, and an isolated
wallet signer. Registry entries are inert by default: registration is not discovery,
connection, installation, activation, authority, or execution.

`CredentialBrokerPolicy` accepts only opaque credential references, HTTPS service
destinations, an exact agent/capability/resource grant, and an explicit hostname
allowlist. Raw credential material, private/local addresses, redirects and arbitrary
destinations are rejected before any future transport adapter could run.

`WalletSignerPolicy` accepts only an opaque wallet reference under an exact grant,
allowlisted destination, bounded amount and fee, and no token-approval side effects.
It validates intent only; this milestone has no key custody, signing or broadcast
implementation. Atomic cross-agent budget reservation remains a required gate.

## Verification plan

Run command-center unit/integration tests first, then the inherited runtime suite,
then the complete repository suite. Start the preview against disposable databases,
exercise `/api/state` and lifecycle/task mutations over HTTP, and capture a local
screenshot only through a permitted non-UI automation path if available.
