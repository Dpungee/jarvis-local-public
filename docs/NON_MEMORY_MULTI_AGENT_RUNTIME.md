# Non-Memory Multi-Agent Runtime

Status: Phase B foundation implemented in `jarvis.multi_agent_runtime`.

This runtime is the persistence and policy substrate for autonomous peer agents.
It is intentionally not an orchestrator and not a model loop. Agent-bound callers
choose peers, compose messages, create rooms, invite participants, own and delegate
tasks, and request capabilities. The runtime supplies stable identity, atomic state,
scope checks, idempotency, causality, restart recovery, and observability.

## Boundary

- `MultiAgentRuntimeStore` is a separate non-memory SQLite authority with its own
  application identifier and schema version.
- It does not import, migrate, or write `memory.py`, `memory_spine.py`, M0-M5
  tables, claims, summaries, retrieval indexes, or Claude's memory contracts.
- Its versioned event envelopes are suitable input to a future Claude-owned memory
  adapter. No adapter is assumed or fabricated here.
- It does not call an LLM, pick a collaborator, schedule a room speaker, approve
  ordinary messages, or rewrite message content.
- Existing specialists and Council remain compatibility evidence. This module does
  not silently change their current central contracts.

## Durable contracts

Opaque identifiers are stable across process restart and model replacement:

| Entity | Prefix |
| --- | --- |
| Agent | `agt_` |
| Message | `msg_` |
| Room | `room_` |
| Task | `task_` |
| Delegation | `dlg_` |
| Event | `evt_` |
| Capability request or grant | `cap_` |
| Artifact reference | `art_` |
| Collaboration request | `req_` |
| Collaboration response | `rsp_` |
| Room-consumption cursor | `cur_` |

Closed runtime scopes are `GLOBAL`, `PROJECT`, `AGENT`, `TASK`, `ROOM`, and
`PRIVATE`. Agent lifecycle is `CREATED`, `RUNNING`, `PAUSED`, or `STOPPED`.
Autonomy (`LOW`, `NORMAL`, `HIGH`, `FULL`) is stored separately from authority
(`SANDBOX`, `STANDARD`, `POWER`, `OWNER`).

Every mutation requires an idempotency key. The store hashes the canonical command
and rejects reuse of a key for different content. Projection changes and their
event envelope commit in one `BEGIN IMMEDIATE` transaction. Events include ordered
sequence and event IDs, schema version, actor, project, scope, subject, correlation,
causation, idempotency key, command digest, canonical payload, and command result.

## Agent-bound surface

`store.bind_agent(agent_id)` returns an `AgentRuntimeContext` exposing operations
as that stable identity:

- start, pause, stop, inspect profile, and replace the bound model without changing
  identity;
- find visible running agents, optionally by declared specialty;
- send and reply to direct messages, read the agent's mailbox, and persist
  delivered/read acknowledgements;
- create rooms, invite or join peers, inspect joined members, send/read room
  messages, advance a monotonic per-agent consumption cursor, and leave;
- create tasks, delegate only tasks currently owned by the caller, list owned work,
  add acyclic dependencies, and make validated task-state transitions only after
  dependencies complete;
- share content-addressed artifact references with task/room ownership and
  membership checks;
- broadcast help to a project or room, request a targeted review, opinion, vote,
  or evidence, opt into eligible requests, attach evidence, and close broadcasts;
- request scoped capabilities and inspect currently effective grants.

The bound surface deliberately has no `select_best_agent`, `next_speaker`, or
`approve_message` operation.

Targeted collaboration requests name the peer chosen by the requesting agent and
close after that peer responds. Help requests are broadcasts: eligible peers
decide independently whether to respond, and the requester explicitly closes the
request. A room-scoped request is visible only to joined members. Vote responses
must select one of the request's closed options. The runtime persists the protocol
and enforces scope; it does not synthesize any response or decide who should act.

Artifact records contain a URI, media type, SHA-256 digest, optional byte count,
and task/room references. They are references and provenance, not blob storage.
Only the current task owner may publish a task artifact, and room publication
requires joined membership. Task dependencies are same-project, reject cycles,
and block both `RUNNING` and `COMPLETED` transitions until every prerequisite is
complete.

A `RUNNING` task cannot gain any new dependency, even an already completed one.
Its owner must explicitly block it before changing prerequisites, then resume
after every prerequisite completes. Retrying an already committed dependency
command is still idempotent. Integrity checks detect historical running/completed
tasks with unfinished prerequisites rather than silently repairing them.

Direct-message receipts distinguish delivered from read. Only the recipient can
advance that state, and unread-mailbox queries continue to return delivered but
unread messages. Each joined room member has an independent optional cursor over
the room's event order. Cursors can skip ahead but never move backward; unread-room
queries resume strictly after the persisted cursor following process restart.

## Capability policy

Agents have one request policy:

- `ASK_OWNER`: create a pending request.
- `DENY`: deterministically create a denied record; it cannot later be granted.
- `AUTO_PROJECT`: automatically grant a request only for the agent's exact project
  scope.
- `OWNER_AUTO`: automatically grant when the requesting agent already has `OWNER`
  authority.
- `AUTO_TRUSTED`: remain pending until a future relationship/reputation slice can
  provide deterministic trust input. The runtime does not invent trust.

An owner-authority decision may grant a pending request permanently or until an
explicit expiration. Grants may be revoked, and effective-capability reads exclude
revoked and expired grants. A global grant applies across scopes; otherwise the
scope kind and identifier must match exactly.

This capability service records authority. Tool and connector adapters must call
it before use; storing a grant alone does not execute a tool.

## Minimal use

```python
from pathlib import Path

from jarvis.multi_agent_runtime import MultiAgentRuntimeStore

with MultiAgentRuntimeStore(Path("runtime.db")) as store:
    researcher = store.create_agent(
        display_name="Researcher",
        role="Evidence specialist",
        specialties=("research",),
        idempotency_key="create-researcher",
    )
    builder = store.create_agent(
        display_name="Builder",
        role="Implementation specialist",
        specialties=("implementation",),
        idempotency_key="create-builder",
    )
    research = store.bind_agent(researcher.agent_id)
    build = store.bind_agent(builder.agent_id)
    research.start(idempotency_key="start-researcher")
    build.start(idempotency_key="start-builder")

    peer = research.find_agents(specialty="implementation")[0]
    research.send_message(
        peer.agent_id,
        "I verified the input. Can you implement the parser?",
        idempotency_key="ask-builder",
    )
```

Callers should retain each command's idempotency key until the result is known.
Retrying the same command with the same key is safe, including from concurrent
processes.

## Verification and current limits

`verify_integrity()` checks SQLite integrity, foreign keys, a contiguous ordered
event stream, event identifiers and links, canonical event payloads, command digest
shape, and creation-event provenance for every projection.

New events use schema 2 with complete command-result content snapshots, copied in
the mutation transaction and protected against SQL update/delete/replace. The
trusted-store `replay_events()` reader validates content, contiguous batches and
sequence/event-ID cursor pairs without consulting current projections. Legacy
schema 1 events remain inspectable but cannot be claimed as content-complete.
The precise guarantee, privacy limits, vocabulary, and **unagreed** project/scope
integration proposals are in `RUNTIME_MEMORY_REPLAY_HANDOFF.md`. No memory-side
adapter or full runtime-projection rebuilding tool is supplied.

The focused suite covers the full two-agent acceptance path, restart reconstruction,
model replacement, project visibility, permission refusals, room departure, task
completion, dependency ordering and cycle refusal, content-addressed task/room
artifacts, targeted and broadcast collaboration protocols, idempotency conflicts,
concurrent retry, direct delivery/read receipts, monotonic room cursors, transaction
rollback on a missing causation event, scoped capability expiration/revocation,
automatic project policy, denied policy, and foreign/newer database refusal.

Still outside this slice:

- model/tool execution loops bound to these identities;
- transport backpressure, retry queues, and dead-letter handling;
- replay-driven projection rebuilding, high-volume backpressure, dead-letter queues,
  resource-budget enforcement, and multi-process soak tests;
- compatibility adapters that migrate the current static specialist and centrally
  chaired Council surfaces;
- all M0-M5 agent and organizational memory, which remains Claude-owned.
