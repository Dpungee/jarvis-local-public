# JARVIS Autonomous Multi-Agent Expansion Plan

Status: planning specification only; implementation has not started

Prepared: 2026-09-15
Controlling source: operator-supplied `Jarvis Autonomous Multi-Agent Architecture & Memory Roadmap`

## 1. Exact target

Later operator-approved requirements (2026-09-16):
`AGENT_AUTONOMY_SECURITY_REQUIREMENTS.md` governs low-friction reusable permissions,
privacy/egress isolation and optional wallet authority. It preserves independent
agent decision-making while requiring deterministic operator security policy.
Where earlier wording could imply removal of those protections, this later
requirement takes precedence. These additions are not implemented capability claims.

JARVIS will become the durable runtime, memory substrate, communications layer, control plane, and operating environment for multiple real autonomous AI agents.

JARVIS is not the boss, central mind, collaboration router, or reasoning intermediary. Agents reason, plan, act, select tools, communicate, delegate, form groups, invite specialists, debate, resolve disagreements, create follow-up work, and decide when their tasks are complete for themselves.

Agent communication is direct:

```text
Agent A <-> Agent B
Agent A <-> room <-> Agent B / Agent C
```

JARVIS does not choose participants, approve conversations, decide who may speak next, interpret messages before delivery, or insert central LLM reasoning into agent communication. JARVIS supplies the communication infrastructure and persistence, not the conversational authority.

Existing JARVIS approval, mediation, or security boundaries are not carried forward as architectural constraints unless the controlling roadmap explicitly includes them. The target is the roadmap's own runtime-configurable authority, capability-policy, memory-scope, and owner-control model; no additional legacy gate may give JARVIS a say in agent communications or autonomous collaboration.

## 2. Control plane and agent data plane

### JARVIS control plane

JARVIS provides:

```text
Agent registry
Identity
Authentication
Authorization
Permissions
Memory service
Event history
Task ledger
Tool registry
Secrets management
Health checks
Resource limits
Observability
Audit logs
Model/provider configuration
Agent lifecycle
Capability policies
Persistence
Replay
```

### Autonomous agent data plane

Agents own:

```text
Reasoning
Planning
Goal pursuit
Task decomposition
Tool selection
Tool use
Research
Coding
Testing
Debugging
Delegation
Negotiation
Collaboration
Agent-to-agent messaging
Group formation
Requesting help
Reviewing each other's work
Creating follow-up tasks
Deciding when work is complete
```

JARVIS provides services. It does not micromanage agent behavior.

## 3. Definition of a real agent

Every persistent agent must support:

```text
Persistent identity
Personality
Role
Goals
State
Working memory
Long-term memory
Tools
Permissions
Mailbox
Task queue
Planning loop
Action loop
Lifecycle
Resource budget
Model/provider configuration
Specialties
Experience
Relationships
Performance history
```

An agent's identity, personality, memory, relationships, and history remain stable when its LLM provider or model changes.

The operating loop is:

```text
observe
-> retrieve relevant memory
-> understand current goals
-> form or revise plan
-> act
-> inspect the result
-> decide whether help is needed
-> contact agents / use tools / continue
-> evaluate completion
-> store useful memory
```

## 4. Required platform capabilities

### Communication

Provide agent-callable equivalents of:

```text
find_agents()
get_agent_profile()
message_agent()
reply_to_agent()
create_room()
invite_agent()
join_room()
leave_room()
broadcast_help()
share_artifact()
delegate_task()
request_review()
request_opinion()
request_vote()
request_evidence()
```

Support direct messages, task rooms, project rooms, temporary agent-created groups, specialty help channels, and broadcast channels. Agents create rooms and choose collaborators themselves.

### Discovery and learned expertise

The Agent Registry exposes identity, role, specialties, capabilities, and profile data. Agents query it directly and decide whom to contact.

The system learns which agent knows what, which agents work well together, which help was useful, which strategies succeeded or failed, which tools proved useful or unreliable, specialty-specific performance, and relationship/collaboration history. These signals inform agents; JARVIS is not the mandatory collaboration selector.

### Autonomy and authority

Autonomy and authority are independent runtime settings.

```text
Autonomy: LOW | NORMAL | HIGH | FULL
Authority: SANDBOX | STANDARD | POWER | OWNER / UNRESTRICTED
```

`OWNER / UNRESTRICTED` means all tools, filesystems, commands, processes, installations, agent creation, messaging, workflows, APIs, credentials, deployments, and tool creation that the JARVIS host and connected systems actually possess and expose.

Authority can change at runtime without recreating the agent or erasing its identity, memory, task, relationships, personality, or history.

### Capability leases and requests

Support permanent grants and temporary capability leases scoped by resource, task, and duration. Agents can identify missing authority and emit capability requests. Policy choices are:

```text
ASK_OWNER
AUTO_TRUSTED
AUTO_PROJECT
OWNER_AUTO
DENY
```

JARVIS enforces the configured policy mechanically; it does not become the reasoning bottleneck.

### Agent-created tools and agents

Selected agents can:

```text
create_tool()
write_tool()
install_dependency()
register_tool()
modify_tool()
spawn_agent()
create_specialist_agent()
create_service()
create_database()
create_api()
start_daemon()
deploy_application()
```

Agent-created specialists carry creator/parent, purpose, permissions, resource budget, memory scope, and lifecycle policy.

## 5. Memory model

Preserve and extend the existing JARVIS memory foundation.

### M0 — Working memory

Temporary task state: current task, plan, recent observations, hypotheses, open questions, and scratch context. Consolidate selectively rather than making it automatically permanent.

### M1 — Episodic/event memory

Append-only, timestamped, provenance-aware, replayable history of agent, task, message, room, tool, memory, artifact, capability, model, and permission events. This is the authoritative record of what happened.

### M2 — Semantic/structured memory

Entities, facts, projects, decisions, architecture, constraints, relationships, bugs, experiments, tool knowledge, lessons, preferences, and project state. Derive it from M1 where possible so it does not become an untraceable second source of truth.

### M3 — Agent memory

Persistent identity, personality, specialties, experience, skills, past tasks, successful and failed strategies, preferences, limitations, relationships, collaboration history, tool competency, and project expertise for each agent.

### M4 — Collective/organizational memory

Which agent knows what, which agents work well together, team outcomes, recurring successful and failed strategies, trusted and unreliable tools, shared discoveries, group decisions, room outcomes, project-wide lessons, expertise, and relationships.

### M5 — Memory learning/optimization

Consolidation, importance scoring, memory decay, retrieval tuning, duplicate reduction, confidence calibration, expertise learning, relationship learning, performance-based knowledge, summarization, cold storage, and archival policy.

### Required scopes

```text
GLOBAL
PROJECT
AGENT
TASK
ROOM
PRIVATE
```

Memory access follows its scope.

### Required memory behavior

- retain raw event/message history;
- maintain summaries, decisions, facts, unresolved questions, artifacts, and outcomes;
- attach provenance to durable memory;
- preserve temporal truth and superseded facts;
- represent conflicts instead of using last-write-wins;
- record conflict resolution as memory;
- support concurrent writers with atomic append, event IDs, idempotent consumers, causal metadata where useful, versioned projections, optimistic concurrency where needed, rebuildable indexes, and transaction boundaries;
- rank retrieval using semantic and lexical relevance, recency, importance, confidence, source quality, relationships, project/task/room relevance, temporal validity, scope, permission, past usefulness, and supersession state;
- make semantic memory writes through proposals that the memory service validates, deduplicates, annotates, scopes, checks for conflicts, and accepts, rejects, or leaves unresolved.

## 6. Task and organizational model

Tasks support parents, children, dependencies, serial and parallel work, ownership, delegation, review, and completion.

Agents can broadcast help requests. Other agents decide whether to respond. Agents can form temporary or persistent teams and create specialists when existing expertise is insufficient.

Agents, rooms, task graphs, mailboxes, relationships, memories, and unfinished work survive process restarts. Important state follows an event-sourced direction:

```text
append-only event log
        -> projections
        -> task state
           agent state
           room state
           memory indexes
           relationship graph
           expertise graph
```

## 7. Phased delivery plan

### Phase A — Audit the existing system

1. Inspect existing M1 and M2.
2. Run current memory tests.
3. Identify stubs and TODOs.
4. Verify replay.
5. Verify persistence.
6. Verify provenance.
7. Verify retrieval.
8. Document the actual current state.

Deliver `FULLY IMPLEMENTED`, `PARTIALLY IMPLEMENTED`, `MISSING`, `BUGS / RISKS`, `TECHNICAL DEBT`, `TEST GAPS`, and `NEXT STEPS`.

Exit: a source-backed architecture/status report exists before any major refactor.

### Phase B — Multi-agent foundations

1. Agent Registry.
2. Persistent agent identity.
3. Direct agent messaging.
4. Rooms and group chats.
5. Task ownership and delegation.
6. Agent discovery.
7. Capability model.
8. Autonomy/authority separation.

Exit: persistent agents can discover one another, communicate directly, create rooms, delegate work, and resume after restart without JARVIS directing the collaboration.

### Phase C — M3 agent memory

1. Agent-scoped memory.
2. Identity memory.
3. Experience history.
4. Skill knowledge.
5. Strategy memories.
6. Relationship memories.
7. Agent retrieval APIs.

Exit: agents retain individual identity, experience, strategies, relationships, limitations, and project knowledge across sessions and model changes.

### Phase D — M4 organizational memory

1. Collaboration history.
2. Expertise graph.
3. Who-knows-what.
4. Team outcomes.
5. Room summarization.
6. Collective discoveries.
7. Conflict-resolution records.
8. Organization-wide retrieval.

Exit: agents can use actual organizational experience to select collaborators and solve later tasks.

### Phase E — Advanced memory

1. Consolidation.
2. Deduplication.
3. Decay and archival.
4. Adaptive retrieval.
5. Performance learning.
6. Relationship learning.

Exit: memory growth remains usable across long-running operation while raw evidence remains available.

### Phase F — Reliability

1. Concurrency tests.
2. Crash tests.
3. Replay tests.
4. Permission tests.
5. Multi-agent stress tests.
6. Memory conflict tests.
7. Restart/resume tests.

Exit: the target multi-agent scenario works through concurrent activity, failure, restart, replay, conflicts, and resumed execution.

## 8. Final acceptance scenario

The architecture is complete when it supports all of the following:

1. The user gives JARVIS a large goal.
2. An appropriate autonomous agent accepts it.
3. The agent creates its own plan.
4. It identifies an expertise gap.
5. It searches the Agent Registry.
6. It directly contacts another agent.
7. The agents decide to create a group room.
8. They invite additional specialists.
9. They divide work themselves.
10. Each agent uses its own tools and memory.
11. They share artifacts.
12. They disagree on a technical decision.
13. Memory records competing claims without overwriting them.
14. The agents run tests or research.
15. They resolve the disagreement.
16. The resolution becomes collective memory.
17. The original agent completes the task.
18. JARVIS records the important events.
19. Relationship and expertise memories improve.
20. Weeks later, a similar task benefits automatically from the organizational memory.

Throughout this scenario JARVIS supplies infrastructure. It does not make the agents' collaboration decisions.

## 9. Non-goals from the controlling roadmap

Do not:

- make JARVIS choose every speaker or collaborator;
- route every agent message through central LLM reasoning;
- make JARVIS approve or mediate agent conversations;
- store every raw message directly in prompts forever;
- use last-write-wins as truth;
- bind identity permanently to an LLM provider;
- make permissions impossible to change at runtime;
- duplicate M1/M2 without auditing them;
- destroy raw history during summarization;
- mix memory scopes;
- treat personality prompts as complete agents.

## 10. Implementation readiness

No implementation begins from this planning task. The first implementation-stage action is Phase A: inspect the repository and map the real implementation against this exact specification. Implementation then proceeds in the smallest working vertical slices while preserving and extending existing memory infrastructure rather than replacing it without an audit.
