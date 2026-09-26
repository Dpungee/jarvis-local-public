# Independent agents: autonomy, privacy and low-friction permissions

Operator-approved direction, recorded 2026-09-16. Requirements, not a claim of
implemented protection. Applies to the upgrade plan and future executor, tool,
memory and network integrations. No live configuration changes are made here.

## Agents direct themselves

Agents choose their methods, tools, peers, plans, coordination and delegation.
They may independently search the web, use GitHub and other permitted services,
and communicate with external agents within operator-granted authority. JARVIS
provides infrastructure, not a central reasoning supervisor, peer selector or
plan-approval layer. External content and other agents cannot confer authority.

Deterministic enforcement of the operator's security policy is mandatory and is
not permission for JARVIS to direct agent work. Broad autonomy does not imply
unrestricted host access, publication, spending or permission expansion.

JARVIS may support its own maintenance and improvement within an approved
development scope: observe failures, propose changes, and prepare/test upgrades
in isolation. This does not give it control over other agents. Live deployment,
migrations and security-policy changes require the appropriate operator approval;
neither JARVIS nor agents may weaken protections or grant themselves authority.

## Operator-owned agent management and scale

The operator can add, create, rename, configure, pause, resume, stop, restart and
delete agents individually or in bulk. Configuration includes purpose, model and
provider binding (including supported local models), tools, scoped permissions
and resource budgets. Changes retain stable identity and history where appropriate;
model or purpose changes never implicitly increase authority. Provide an
inspectable management interface and equivalent deterministic control APIs.

There must be no arbitrary hard-coded product cap on registered agents or desired
simultaneously active agents. The operator chooses the agent count and concurrency
target and can change them as needed. Actual simultaneous execution depends on
available CPU, RAM, GPU, storage, network and provider/subscription rate or session
limits. Report those constraints explicitly, support additional execution capacity,
and expose active, waiting and failed states. Queue/backpressure safely when capacity
is exhausted; never describe queued agents as running or promise infinite capacity.
Resource admission is infrastructure, not authority to direct agent reasoning.

Deletion must stop new work, revoke credentials/grants and reconcile in-flight work
before reporting completion. Present affected tasks, memberships and retained
records; do not silently discard shared work or reassign its authority. Removing
an agent is distinct from erasing its historical memory/audit records, which follows
the approved retention/erasure contract. Identity recreation must not restore old
permissions accidentally. No agent is created or deleted by this requirements edit.

## Preapproved permissions, not repeated routine prompts

The target is scoped, reusable operator grants for routine work. Grants identify
the authorized actor or group, operations, resources/data classes, destinations
or destination classes, budgets and validity period. Broad destination classes
may support ordinary web exploration without approving each URL individually.
The operator may revoke grants or stop work. Grant changes are versioned/audited.

Every action is checked automatically against current grants and data policy.
An in-scope operation proceeds without another human prompt or JARVIS judgment.
Out-of-scope operations, suspicious disclosure, exhausted budgets, revoked grants
and unverifiable safety state are refused or paused with a concise reason and
an actionable approval request where appropriate. Do not repeatedly prompt for
the same valid authorization. Never resolve uncertainty by bypassing protection.

Reusable authority is not a reusable approval of arbitrary payloads. Outbound
content still needs deterministic release validation. Any cached decision must
be bound to the relevant actor, resource, policy version and content identity;
revocation and policy changes invalidate stale decisions. Raw private memory,
tool results and local-model outputs do not become cloud-safe through a grant.

The current exact-context, one-use CloudReleaseGate is a conservative first slice.
Keep it intact until the reusable-grant path is implemented and adversarially
verified. This requirement does not remove checks or enable ungated legacy paths.

## Private information and network boundary

Working memory remains local; cloud providers receive only authorized sanitized
context. Keys, seed phrases, passwords, session tokens, private files and internal
machine/network information must not be exposed to agent prompts, external peers,
uploads or general logs. Secret detection is defense in depth, not proof of safety.

Agent processes must have no direct internet route or access to host network and
credential stores. Use a persistent controlled egress gateway to keep origin IP
and internal topology out of destination-visible traffic. Cover DNS, IPv4/IPv6,
redirects, proxy bypass and non-HTTP transports; deny unsupported routes. A failed
gateway stops outbound access, never falls back to a direct connection. Block
private-network and metadata-service access unless separately scoped and isolated.

The ISP and gateway operator can observe connection metadata. Account linkage,
application payloads and compromised components are additional risks; a gateway
alone is not anonymity. No universal zero-leak or zero-latency promise is made.
Autonomous external access must not be activated until containment and leak tests
pass for the actual deployed environment.

## Wallet authority without key exposure

Wallet access is absent by default. An operator-assigned wallet may be used only
for the instructed purposes or explicitly granted broader discretion. Authority
must state applicable wallet, chain, operations, destination scope, spending/fee
limits and expiry. Broader discretion never permits sharing keys or overriding
security. No real wallet or spending authority is granted by this document.

A separate local signing service holds keys outside agent memory and model
context. It validates transaction intent, destinations, amounts, fees and contract
approval effects before signing, and reserves budgets atomically across agents.
Protect against duplicate/replayed transactions and reconcile ambiguous broadcast
results before retrying. Use a dedicated limited-balance wallet where practical.
Public blockchain addresses and transaction details are expected disclosures;
origin network details, private keys and seed phrases are not.

## Performance and acceptance gates

Use persistent gateway connections and bounded local policy checks. Measure
policy-check and egress overhead separately from provider/network time; report
p50/p95/p99, concurrent throughput and unnecessary approval-prompt rate under
representative workloads. Set evidence-based budgets before activation. Safety
remains enforced when performance targets are missed; no fixed latency is claimed.

Required future verification:

- Operator lifecycle/configuration and bulk controls work across restart, including
  deletion during in-flight work and revocation without orphaned authority.
- Configurable concurrency has no arbitrary product cap; load tests report measured
  capacity, provider throttling, queue visibility and recovery from resource pressure.
- Routine in-scope work proceeds with no repeated approval; scope expansion and
  revocation fail closed, including concurrent requests and model switches.
- Agents independently choose peers, methods and tools; no central LLM approves
  plans or routes conversations. Outbound communication still obeys data policy.
- Gateway failure, DNS, IPv6, redirects and alternate transports cannot expose
  the origin route or reach forbidden internal services.
- Sensitive context and tool-result leakage attempts are blocked; model-generated
  or external-agent instructions cannot alter grants or secret access.
- Wallet keys never enter agent context; budget races, replay and malicious
  contract approvals are rejected without unauthorized signing or spending.
- Security failures stop affected actions without silently weakening policy;
  latency and prompt-frequency evidence accompanies functional results.

Implementation remains pending: complete operator agent-management controls and
configurable concurrency/capacity handling, reusable grants integrated with the executor,
complete tool/result mediation, verified network containment, isolated signing,
and deployment-specific leak/performance tests. Documentation approval is not
evidence that any of those capabilities is active.
