# Non-Memory Autonomous Runtime Audit

Date: 2026-09-16

Branch: `codex/upgrade-readiness`

Baseline: `93119c421cdea4fffafcd3d13caef3fccfe15a29`

Scope: Phase A of `docs/JARVIS_MULTI_AGENT_EXPANSION_PLAN.md`. This audit covers
the non-memory runtime only. Claude owns the memory audit and M0-M5 memory
evolution described in `docs/CLAUDE_MEMORY_HANDOFF_SPEC.md`; this work neither
changes nor duplicates that boundary.

## User-visible outcome and exit criteria

The first implementation slice must let two independently addressable agents:

1. survive process restart under stable identifiers;
2. discover one another without JARVIS selecting a peer;
3. send direct messages without JARVIS relaying the content;
4. create and join a durable room;
5. delegate a durable task from its current owner to the chosen peer; and
6. reconstruct those facts from persistent state and an ordered event history.

JARVIS may provide identity, registry, persistence, authorization primitives,
and observability. It must not choose participants, schedule speakers, approve
ordinary peer messages, or insert model reasoning into peer communication.

## Audit summary

| Capability | Status | Evidence | Gap to target |
| --- | --- | --- | --- |
| Persistent agent identity | Partially implemented | `jarvis.specialists.SpecialistDefinition`, `SPECIALISTS`, and memory migration v12's `specialist_agents` projection | Specialists are seeded static personas, not model-independent autonomous identities with goals, lifecycle, mailbox, task queue, policy, or mutable provider binding. |
| Independent agent runtime | Partially implemented | `jarvis.agent.Agent`, `set_specialist`, and the generic task worker loop | One large process object owns model, toolbox, and request state. `Agent.run` rejects concurrent or nested calls. A specialist is a mode on the central runtime, not an independently resumable actor. |
| Agent discovery | Missing for agents | Operator/JARVIS-facing `Memory.list_specialist_agents` | The docstring and contracts explicitly keep the roster invisible to specialists. There is no agent-callable filtered discovery interface. |
| Direct messaging and mailbox | Missing | No runtime table or agent-callable interface | Existing specialist reports return to the owning JARVIS context. There is no peer-to-peer message, reply chain, durable inbox, delivery cursor, or idempotent send contract. |
| Rooms and membership | Partially implemented as transition evidence | `jarvis.council.CouncilMeeting`, `build_seats`, `item_script`, `next_directive`, and `CouncilRuntime` | Council is centrally chaired and scheduled. It has no durable room identity, invitation/join/leave state, member-selected dialogue, or restartable message stream. |
| Task ownership and delegation | Partially implemented | `Memory.add_task`, `claim_task`, lease renewal/recovery, `delegate_specialist_task`, and `jarvis.long_horizon.LongHorizonStore` | Existing task durability is useful, but specialist delegation is hard-coded JARVIS-to-specialist advice. There is no current-owner invariant, peer delegation edge, parent task graph, or agent inbox view. |
| Workflow reliability | Fully implemented for bounded single-workflow execution; missing for peer runtime | Long-horizon stage leases, checkpoints, receipts, reservations, retries, reconciliation, and final verification | These mechanisms do not establish agent, room, or message semantics. They should be reused as design patterns instead of forcing peer activity through the workflow orchestrator. |
| Per-agent autonomy and authority | Missing | Global `autonomous`/`readonly` configuration, specialist tool allowlists, connector gateway, and existing tool authority gates | Autonomy and authority are not independent per-agent profiles. There are no durable scoped grants or temporary leases attached to an agent/task/room. |
| Lifecycle and model independence | Missing | `Agent.set_specialist` plus process-local model/router configuration | No CREATED/RUNNING/PAUSED/STOPPED lifecycle, stable identity across model changes, restart attachment contract, or per-agent provider binding exists. |
| Event envelope and replay | Partially implemented in other domains | `jarvis.memory_spine` and long-horizon receipts | The memory spine intentionally accepts a closed memory-event vocabulary and is Claude-owned. Runtime agent/task/room events have no non-memory ordered ledger, causality keys, or projection integrity checks. |
| Relationships and reputation | Missing for peers | `jarvis.relationship_memory.RelationshipMemory` | That store is deliberately user-editable companion context and cannot become operational truth. It is not an agent relationship or reputation system. |
| Capability negotiation | Missing | Static specialist allowlists and connector manifests | No agent-originated scoped request, owner/policy decision, expiring lease, grant revocation, or capability event history exists. |

## Implemented foundations worth preserving

- SQLite authority preflight, application identifiers, exact schema markers, WAL,
  foreign keys, and `BEGIN IMMEDIATE` transaction patterns.
- Task lease, recovery, idempotency, and retry concepts in `jarvis.memory.Memory`.
- Long-horizon effect keys, mutation receipts, stage checkpoints, budgets, and
  crash reconciliation.
- Provider routing and tool gateways as replaceable runtime dependencies.
- The static specialist roster and Council as migration/compatibility evidence,
  not as the target collaboration architecture.

## Bugs and architectural risks

1. `specialist_contract()` and `orchestrator_contract()` intentionally make
   specialists peer-blind and JARVIS the sole delegator. Extending those strings
   without changing the deterministic runtime would create false capability.
2. `CouncilRuntime` chooses the next speaker and returns all conclusions through
   JARVIS. Treating it as a room would preserve the exact central bottleneck the
   target architecture removes.
3. `Memory.delegate_specialist_task` fixes `delegated_by` to `jarvis`. Reusing it
   for peer delegation would corrupt provenance and ownership semantics.
4. `Agent.run` is process-local and non-reentrant. Running multiple identities in
   the same instance would mix mutable request/tool state and is not a safe
   shortcut to independent agents.
5. The memory spine has a closed, memory-specific event vocabulary. Adding runtime
   event kinds there would cross Claude's ownership boundary and couple two active
   migrations.
6. Stable identity cannot be derived from display name, specialist key, model, or
   room membership. Each needs an immutable opaque identifier.
7. At-least-once command retry without a command digest can silently reuse an
   idempotency key for a different payload. The runtime must reject that conflict.
8. Read models without a causally linked ordered event record cannot prove restart
   behavior or support later memory integration.

## Technical debt

- Static specialist definitions combine identity, role, routing, and tool policy.
- Council planning, turn selection, rendering, model invocation, and persistence
  are coupled in one central runtime.
- Generic tasks, specialist reports, scheduled routing, and long-horizon plans use
  different ownership vocabularies.
- Existing scopes are domain-specific; the required `GLOBAL`, `PROJECT`, `AGENT`,
  `TASK`, `ROOM`, and `PRIVATE` runtime scopes do not share one closed contract.
- Agent-facing interfaces do not yet have a small transport-independent service
  boundary that CLI, local processes, or future network transports can share.

## Test gaps

- No restart acceptance test spans identity, discovery, direct message, room, and
  delegated task state.
- No test proves JARVIS is absent from peer selection or message content flow.
- No concurrent send/delegate test verifies uniqueness and current-owner rules.
- No retry test verifies same-key/same-command replay and same-key/different-command
  rejection.
- No lifecycle/model-swap test proves that identity remains stable.
- No event/projection integrity test verifies every material state transition has
  a causally linked event.
- Existing Council tests positively assert central chair scheduling and therefore
  cannot be relabeled as autonomous-room coverage.

## Stable contracts for the first slice

The non-memory runtime owns these contracts without changing Claude-owned files:

- Opaque identifiers: `agt_`, `msg_`, `room_`, `task_`, `dlg_`, `evt_`, and
  `cap_` prefixes followed by random lowercase hexadecimal payloads.
- Scope kinds: `GLOBAL`, `PROJECT`, `AGENT`, `TASK`, `ROOM`, `PRIVATE`.
- Agent lifecycle: `CREATED`, `RUNNING`, `PAUSED`, `STOPPED`.
- Task lifecycle: `OPEN`, `ASSIGNED`, `RUNNING`, `BLOCKED`, `COMPLETED`, `FAILED`,
  `CANCELLED`.
- Autonomy: `LOW`, `NORMAL`, `HIGH`, `FULL`.
- Authority: `SANDBOX`, `STANDARD`, `POWER`, `OWNER`.
- Every mutating command carries an idempotency key and a canonical command digest.
- Every material transition appends one immutable ordered event in the same SQL
  transaction as its projection update.
- Events include event/schema identifiers, actor, scope, subject, correlation,
  causation, idempotency, command digest, timestamp, and canonical JSON payload.
- A repeated key with the same digest returns the original command result; a
  repeated key with a different digest fails closed.
- Agent discovery is filtered data-plane access. The requesting agent chooses whom
  to contact; the store never ranks or selects a peer on its behalf.
- Room creators choose invitees. Joined agents choose when and what to send. The
  store enforces membership but never schedules speakers.
- Only the current task owner can delegate it. Delegation atomically records the
  edge, changes ownership, and appends the event.
- Reopening the same database reconstructs agents, messages, membership, tasks,
  delegation provenance, and the event order without model participation.

The event envelope is intentionally independent of the memory-spine schema. A
future Claude-owned integration may consume these envelopes through a versioned
adapter; this slice does not guess or modify that adapter.

## Ordered implementation slices

1. **Phase B0 — contracts and store:** add the non-memory runtime schema, opaque
   IDs, closed enums, event envelope, idempotent command receipt, agent registry,
   lifecycle, and filtered discovery.
2. **Phase B1 — two-agent acceptance path:** add direct mailbox, durable rooms,
   task ownership/delegation, bound agent contexts, restart tests, and assertions
   that peer choice/message bodies originate from agents rather than JARVIS.
3. **Phase B2 — capability leases:** add scoped grants, request policy, expiration,
   revocation, and effective-capability evaluation without embedding tool execution.
4. **Phase B3 — peer work exchange:** add acyclic task dependencies,
   content-addressed artifact references, help broadcasts, and targeted
   review/opinion/vote/evidence protocols.
5. **Phase B4 — consumption state:** add recipient-owned direct-message receipts
   and monotonic per-member room cursors.
6. **Phase C/D — runtime adapters:** bind independent model/tool runners to stable
   IDs and migrate Council and specialists behind compatibility adapters.
7. **Phase F — reliability:** concurrent writers, crash injection, replay/projection
   rebuild, backpressure, delivery cursors, dead-letter behavior, resource budgets,
   and restart soak tests.

## Phase A disposition

Phase A is complete for the non-memory runtime. The smallest sound next step is a
new, isolated runtime control-plane store plus a transport-independent agent-bound
API. Direct edits to `memory.py`, `memory_spine.py`, their schema versions, or
Claude's handoff artifacts are explicitly out of scope.

Implementation addendum: Phase B0-B4 are now represented by the separate runtime
module and focused tests. The identifier contract has expanded additively with
`art_` artifact, `req_` collaboration-request, and `rsp_`
collaboration-response identifiers plus `cur_` room cursors. This addendum records
implementation progress; it does not revise the observed Phase A source findings
or cross the memory boundary.
