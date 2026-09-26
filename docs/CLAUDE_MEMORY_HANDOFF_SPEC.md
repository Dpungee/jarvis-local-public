# Claude Memory Handoff Specification

Status: ready to hand off; not dispatched

Prepared: 2026-09-15
Assignment: audit and plan the memory expansion exactly as defined by the operator's autonomous multi-agent roadmap

## 1. Assignment

Claude will own the memory work because Claude has worked on JARVIS memory from the beginning.

For this first handoff, inspect the existing JARVIS codebase and map its current memory implementation against the required M0–M5 architecture. Preserve the working M1/M2 foundation and plan its extension into M3 agent memory and M4 organizational memory.

Do not implement yet. Produce the audit and implementation plan, then stop for operator review.

## 2. Controlling architecture

JARVIS is the infrastructure, memory substrate, communications layer, and control plane for real autonomous agents. It is not the central intelligence controlling them.

Agents communicate directly and decide for themselves whom to contact, when to create a room, whom to invite, how to divide work, how to debate, and when their work is complete. JARVIS has no reasoning or decision-making role in those communications. It supplies storage and infrastructure only.

Do not modify the requested architecture to preserve the current fixed-specialist or JARVIS-centered orchestration model. Map the current model, identify the gaps, and plan the transition to the requested autonomous model.

Do not preserve an existing JARVIS approval, mediation, or security boundary merely because it already exists. Retain only the authority, capability-policy, scope, and owner-control behavior required by the controlling roadmap, and do not give JARVIS any approval or reasoning role in agent-to-agent communication.

### Verified upstream baseline for this handoff

Begin the audit from GitHub `main` commit `93119c421cdea4fffafcd3d13caef3fccfe15a29` or a later operator-approved successor, not from the currently checked-out stale feature branch. That upstream commit already contains the VTMF governed-memory merge, database schema version 50, and substantial memory spine, graph, learning-ladder, compaction, benchmark, and Council work.

The VTMF release's labels `M1` through `M5` are milestone names for that existing implementation. They are not proof that the operator roadmap's distinct `M0` through `M5` memory-layer taxonomy is complete. Map the existing VTMF components to the operator taxonomy using code and tests; do not equate the two naming systems, duplicate working components, or assume a layer exists because a milestone has the same label.

Treat the current Council and specialist runtime as transition evidence, not the target architecture. The verified upstream Council is JARVIS-chaired, deterministically scheduled, tool-free, and peer-bounded, and the delegation runtime is peer-blind. The required target remains direct autonomous agent communication with no JARVIS reasoning, speaker selection, participant selection, or collaboration decisions.

Use the prepared `codex/upgrade-readiness` worktree based on upstream `93119c4`. Read `docs/UPGRADE_PREPARATION.md` for current verification, dependency decisions, and the exact remaining publication steps. The earlier deeply nested audit checkout exceeded Windows path/context limits; distinguish that from the normal project-depth baseline. Do not equate the old audit's aggregate failures with product defects.

## 3. Required startup reading

1. `AGENTS.md`
2. `docs/UPGRADE_PREPARATION.md`
3. `PROJECT_STATUS.md`
4. `CONTRIBUTING.md`
5. `docs/AUTONOMOUS_MULTI_AGENT_SOURCE.md`
6. `docs/JARVIS_MULTI_AGENT_EXPANSION_PLAN.md`
7. `docs/MEMORY_SPINE.md`, `docs/MEMORY_GRAPH.md`, `docs/LEARNING_LADDER.md`, and `docs/COMPACTION.md`
8. all relevant memory, retrieval, long-horizon, specialist, task, capability, persistence, and test files

Inspect current Git state, schema, code, and tests. Do not rely on older status claims when current evidence differs.

## 4. Required audit

### M0 — Working memory

Determine what currently stores current task, current plan, recent observations, active hypotheses, open questions, and temporary scratch context. Identify what is transient, what persists, and how selective consolidation currently works or is missing.

### M1 — Episodic/event memory

Verify append-only event persistence, stable event IDs, timestamps, ordering, replay, idempotency, provenance, crash recovery, event validation, event schemas, and tests. Map all event-like stores and determine what is actually authoritative for what happened.

Required future event coverage includes:

```text
agent.created / started / stopped / model_changed / permission_changed
task.created / assigned / delegated / started / blocked / completed / failed
message.sent / received
room.created / joined / left / closed
tool.called / completed / failed / created / registered
memory.proposed / accepted / rejected / superseded / conflicted / resolved
artifact.created / updated / shared
capability.requested / granted / revoked
```

### M2 — Semantic/structured memory

Verify entity storage, fact storage, relationships, semantic retrieval, indexes, embeddings, metadata, provenance, confidence, temporal validity, supersession, conflict handling, rebuildability, and tests. Determine which semantic state is derived from events and which is an independent source of truth.

### Persistence and retrieval

Verify restart behavior, transaction safety, partial-write behavior, migration strategy, concurrent writes, backup/recovery, semantic ranking, lexical ranking, scope filters, recency, importance, source tracking, and superseded-memory filtering.

## 5. Required M3 design

Plan persistent agent memory for identity, personality, specialties, experience, learned skills, past tasks, successful and failed strategies, preferences, known limitations, relationships, collaboration history, tool competency, and project expertise.

Agent memory must survive restarts and model/provider changes. Define agent-scoped write, retrieval, consolidation, supersession, conflict, and persistence behavior.

## 6. Required M4 design

Plan collective and organizational memory for:

```text
which agent knows what
which agents work well together
which team solved which problem
which strategies repeatedly succeed or fail
which tools are trusted or unreliable
which agents are strongest in specific areas
shared discoveries
team decisions
group-chat outcomes
project-wide lessons
```

Include collaboration history, an expertise graph, who-knows-what, team outcomes, room summarization, collective discoveries, conflict-resolution records, organization-wide retrieval, relationship histories, and multi-dimensional objective performance signals rather than one simplistic score.

Agents use this knowledge to choose collaborators themselves. JARVIS does not route collaboration centrally.

## 7. Memory scopes

Plan `GLOBAL`, `PROJECT`, `AGENT`, `TASK`, `ROOM`, and `PRIVATE` scopes. Memory retrieval and writing must respect the selected scope.

## 8. Room memory

Plan durable room memory containing raw event/message history, summaries, decisions, extracted facts, unresolved questions, resulting artifacts, and final outcomes. Agents must not need an unlimited raw transcript in every prompt.

## 9. Provenance, temporal truth, and conflict

Every durable memory design must cover:

```text
memory_id
claim
source_type
source_agent
source_artifact
source_event
timestamp
confidence
scope
project
task
room
valid_from
valid_until
superseded_by
contradicted_by
evidence
```

Do not delete old facts merely because reality changed. Preserve temporal validity and supersession. Do not use last-write-wins for conflicting claims. Store conflicts as unresolved, expose them to agents, allow agents to create a room and gather evidence, then persist the resolution and evidence as memory.

## 10. Memory write pipeline

Plan this flow:

```text
Agent observation
      -> memory proposal
      -> Memory service
      -> validation
         deduplication
         provenance attachment
         scope validation
         conflict detection
         supersession check
      -> accepted / rejected / unresolved
```

Agents do not directly overwrite canonical semantic truth.

## 11. Consolidation and concurrent writes

Plan `RAW EVENTS -> EPISODES -> SUMMARIES -> EXTRACTED FACTS -> STRUCTURED KNOWLEDGE -> LONG-TERM MEMORIES`. Raw events remain available for audit and replay.

Account for race conditions, duplicate memories, conflicting facts, lost updates, stale projections, partial writes, out-of-order events, double processing, and corrupt indexes. Use atomic append, idempotent consumers, event IDs, causal metadata where useful, versioned projections, optimistic concurrency where needed, rebuildable indexes, and transaction boundaries.

## 12. Retrieval design

Plan ranking using semantic relevance, lexical relevance, recency, importance, confidence, source quality, agent relationship, project/task/room relevance, temporal validity, scope, access permission, past usefulness, and supersession state.

Relevant experience, current project decisions, agent expertise, and known failures should rank ahead of superficial term matches.

## 13. M5 advanced memory plan

Plan automatic consolidation, importance scoring, memory decay, retrieval tuning, duplicate reduction, confidence calibration, agent expertise learning, relationship learning, performance-based routing knowledge, automatic summarization, cold storage, and archival policy.

## 14. Required report

Produce:

```text
1. Current architecture map
2. Current M0 status
3. Current M1 status
4. Current M2 status
5. Existing agent/runtime capabilities
6. Existing messaging capabilities
7. Existing permission/capability system
8. Persistence and concurrency findings
9. Gaps against the specification
10. M3 design
11. M4 design
12. M5 plan
13. Recommended implementation sequence
14. Exact files/modules likely to change
15. Tests required
```

For each current subsystem, report `FULLY IMPLEMENTED`, `PARTIALLY IMPLEMENTED`, `MISSING`, `BUGS / RISKS`, `TECHNICAL DEBT`, `TEST GAPS`, and `NEXT STEPS`. Cite exact files, functions, tables, migrations, tests, and commands.

## 15. Required implementation sequence

Recommend work in this order:

```text
Phase A — Audit
Phase B — Multi-agent foundations
Phase C — M3 agent memory
Phase D — M4 organizational memory
Phase E — Advanced memory
Phase F — Reliability
```

Preserve and extend functioning memory infrastructure instead of duplicating or unnecessarily replacing it.

## 16. Acceptance criteria

- The current M0/M1/M2 implementation is mapped to exact evidence.
- All missing foundations for M3/M4 are identified.
- Direct autonomous collaboration is the target, not JARVIS-centered orchestration.
- M3/M4 schemas, scopes, provenance, conflicts, retrieval, concurrency, and consolidation are designed.
- Exact modules and tests for every phase are listed.
- The smallest implementation slices are ordered.
- No implementation, schema migration, runtime configuration change, or data mutation has started.

## 17. Stop point

Stop after the audit and plan. Wait for the operator to authorize implementation.

## 18. Ready-to-send prompt

> Read `docs/CLAUDE_MEMORY_HANDOFF_SPEC.md` and perform the audit and memory planning assignment exactly as written. Use `docs/JARVIS_MULTI_AGENT_EXPANSION_PLAN.md` and the operator-supplied autonomous multi-agent roadmap as the controlling architecture. JARVIS is infrastructure, not the central mind: agents communicate directly and make their own collaboration decisions. Map the current implementation, design M3/M4/M5, list exact files and tests, and stop before implementation or any code, schema, configuration, or data change.
