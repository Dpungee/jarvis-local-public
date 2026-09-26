<!-- Operator source. Only the machine-specific path in section 55 is replaced with a public placeholder. -->
# Jarvis Autonomous Multi-Agent Architecture & Memory Roadmap

## Purpose

This document is a handoff specification for evolving **Jarvis** from a basic AI harness into a **multi-agent runtime / control plane** that hosts multiple **real autonomous AI agents**.

The goal is **not** to make Jarvis the central intelligence that micromanages every agent.

Instead:

> **Jarvis should be the environment, infrastructure, memory substrate, and control plane in which autonomous agents operate.**

Each agent should be capable of independent reasoning, planning, tool use, collaboration, delegation, communication, and task execution.

Agents should be able to communicate directly with one another, request help, form temporary or persistent group chats, share work, debate solutions, assign subtasks, and learn which other agents are useful for specific kinds of problems.

The existing Jarvis memory system should be preserved as the foundation and extended for true multi-agent operation.

---

# 1. Core Architectural Principle

## Jarvis should not be the boss

Jarvis should **not** sit in the middle of every agent action.

Avoid this:

```text
User
  ↓
Jarvis
  ↓
Agent A
  ↓
Jarvis
  ↓
Agent B
  ↓
Jarvis
  ↓
Agent A
```

That architecture makes Jarvis:

- a reasoning bottleneck
- a communication bottleneck
- a scaling bottleneck
- a single point of failure
- the hidden "real agent" while all other agents become glorified tools

Instead use:

```text
                         USER
                          │
                          ▼
                 ┌─────────────────┐
                 │     JARVIS      │
                 │  CONTROL PLANE  │
                 └────────┬────────┘
                          │
              Infrastructure / Services
                          │
        ──────────────────┼──────────────────
                          │
                    AGENT NETWORK
                          │
       ┌──────────────┐    │    ┌──────────────┐
       │    ATLAS     │◄───┼───►│     NOVA     │
       │ architecture │    │    │   research   │
       └──────┬───────┘    │    └──────┬───────┘
              ↕            │           ↕
       ┌──────────────┐    │    ┌──────────────┐
       │    FORGE     │◄───┴───►│    SENTRY    │
       │    coding    │         │ security/test│
       └──────────────┘         └──────────────┘
```

Agents should communicate directly.

Jarvis provides the rails, memory, identities, permissions, observability, and infrastructure.

---

# 2. Control Plane vs Agent Data Plane

This separation is critical.

## Jarvis Control Plane

Jarvis should own infrastructure-level responsibilities:

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

Jarvis should know what exists and provide services.

It should **not need to approve or reason through every agent interaction**.

## Agent Data Plane

Agents themselves should own:

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
```

This design prevents Jarvis from becoming the intelligence bottleneck.

---

# 3. What Counts as a "Real Agent"

Do not implement agents as simple personas.

Bad model:

```text
"You are the coding agent."
```

A real Jarvis agent should have:

```text
Persistent identity
Personality
Role
Goals
State
Memory
Working context
Long-term memory
Tools
Permissions
Mailbox
Task queue
Planning loop
Action loop
Lifecycle
Resource budget
Model/provider
Specialties
Experience
Relationships with other agents
Performance history
```

Conceptually:

```text
observe
   ↓
retrieve relevant memory
   ↓
understand current goals
   ↓
form or revise plan
   ↓
take action
   ↓
inspect result
   ↓
decide whether help is needed
   ↓
contact agents / use tools / continue
   ↓
evaluate completion
   ↓
store useful memory
```

---

# 4. Agents Must Be Autonomous

Agents should be able to act without Jarvis micromanaging them.

An agent should be able to:

- create its own plan
- choose tools
- revise its plan
- create subtasks
- ask another agent for help
- ask multiple agents for help
- create a group chat
- invite specialists
- share files/artifacts
- assign work to another agent
- request review
- challenge another agent's conclusion
- continue a task after receiving help
- independently decide when a task is complete
- remember how similar problems were solved before

Example:

```text
Forge:
"I cannot explain this authentication race condition."

Forge checks agent memory:

Sentry:
- strong in authentication
- solved 12 previous security issues
- high success rate with Forge

Atlas:
- strong architecture skills
- helped resolve previous concurrency design problem

Forge decides:

→ message Sentry
→ message Atlas
→ create temporary group room
```

Jarvis should not need to select the participants.

---

# 5. Agent-to-Agent Communication

Communication should be a first-class platform feature.

## Required primitives

Agents should have access to interfaces similar to:

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

Names can differ, but these capabilities should exist.

---

# 6. Conversation Types

Jarvis should support multiple communication structures.

## Direct message

```text
agent://forge → agent://sentry
```

## Task room

```text
room://task/482
```

## Project room

```text
room://project/jarvis
```

## Temporary agent-created group

```text
room://generated/7f83a
```

## Help channel

```text
channel://help/security
```

## Broadcast channel

```text
channel://agents/research
```

Agents must be able to create these rooms themselves when policy allows.

---

# 7. Example Autonomous Collaboration

Example flow:

```text
Forge:
"I finished the authentication implementation.
Sentry, please review the security model."

Sentry:
"Refresh token rotation has a concurrency problem."

Forge:
"I don't see the failure case. Can you reproduce it?"

Sentry:
"Attached reproduction trace."

Forge:
"Atlas, this may be architectural.
Can you review our locking design?"

Atlas:
"This needs more than a DM.
Creating room://auth-race-review."
```

Room:

```text
Participants:
Forge
Sentry
Atlas

Topic:
Refresh-token rotation race condition

Shared artifacts:
- auth-design-v2.md
- failing-test.json
- trace.log

Outcome:
- token lock moved into credential service
- concurrency test added
- implementation patched
- Sentry re-ran test
```

That interaction should become part of the system's collective memory.

---

# 8. Agent Discovery

Agents need to discover who can help them.

Jarvis should maintain an Agent Registry.

Example:

```json
{
  "id": "sentry",
  "role": "security_engineer",
  "specialties": [
    "authentication",
    "authorization",
    "cryptography",
    "threat-modeling"
  ],
  "capabilities": [
    "code.read",
    "tests.run",
    "security.scan",
    "agent.message"
  ]
}
```

Agents should be able to query the registry directly.

Example:

```text
Forge needs:
OAuth security help

find_agents({
  specialty: "authentication"
})

Results:
Sentry
Nova
Atlas
```

Forge decides whom to contact.

---

# 9. Agents Should Learn Who Is Good at What

The system should gradually accumulate experience like:

```text
Sentry

Strengths:
- authentication
- authorization
- security review
- cryptography

Collaboration history:
- worked with Forge 31 times
- successful 27 times

Common help provided:
- OAuth
- permissions
- token design
```

Another example:

```text
Forge

Strengths:
- Go
- Python
- backend implementation

Observed weakness:
- occasionally misses concurrency edge cases

Common collaborators:
- Atlas
- Sentry
```

This information should help agents make their own collaboration decisions.

Jarvis should not have to route every request centrally.

---

# 10. Model Independence

Agent identity should not be tied permanently to one LLM.

An agent should be conceptually independent from its inference provider.

Example:

```text
Agent: Forge

Identity:
Forge

Personality:
persistent

Memory:
persistent

History:
persistent

Model:
GPT / Claude / Grok / local / other

Model may change without erasing Forge.
```

This allows:

- different models for different roles
- switching providers
- fallback models
- specialized coding models
- local/private models
- experimentation without destroying agent identity

---

# 11. Autonomy vs Authority

These must be separate settings.

## Autonomy

> What is the agent allowed to decide without asking permission?

Examples:

```text
LOW
NORMAL
HIGH
FULL
```

## Authority

> What resources is the agent technically allowed to access?

Examples:

```text
SANDBOX
STANDARD
POWER
OWNER
```

An agent can therefore be:

```text
Autonomy: FULL
Authority: STANDARD
```

or:

```text
Autonomy: FULL
Authority: OWNER
```

These are separate concerns.

---

# 12. Runtime-Configurable Capability Profiles

Do not permanently hard-code agents into restrictive permission levels.

An agent should be able to change capability profiles based on owner policy.

Suggested profiles:

## STANDARD

```text
normal filesystem scope
approved tools
normal network access
limited secret access
normal project permissions
```

## POWER

```text
full project filesystem
terminal/shell
install packages
spawn processes
expanded network access
broader credentials
container control
advanced tools
```

## OWNER / UNRESTRICTED

Conceptually:

```text
all Jarvis-exposed tools
full permitted filesystem
command execution
process creation
software installation
agent creation
agent messaging
workflow modification
connected APIs/services
authorized credentials
deployment capabilities
tool creation
```

Important:

"Unrestricted" means unrestricted **within what the host OS, accounts, APIs, credentials, and external systems actually permit**.

Jarvis cannot grant access that the host does not possess.

---

# 13. Dynamic Permission Switching

An agent should not need to be recreated to gain or lose capability.

Example:

```json
{
  "agent": "forge",
  "capability_profile": "owner",
  "autonomy": "full"
}
```

Later:

```json
{
  "agent": "forge",
  "capability_profile": "standard",
  "autonomy": "full"
}
```

Forge remains the same agent:

- same memories
- same identity
- same relationships
- same task
- same personality
- same history

Only authority changes.

---

# 14. Temporary Capability Leases

Support temporary elevation.

Example:

```text
Forge normally:
POWER

Forge needs:
container.admin

Reason:
reproduce deployment failure

Grant:
container.admin

Scope:
machine://dev-vm-3

Duration:
task-918
```

When the task finishes:

```text
container.admin revoked automatically
```

Also allow owner-configured permanent grants where appropriate.

---

# 15. Agents Can Request More Capability

Agents should be able to identify blocked actions.

Example:

```text
Forge:
"I need Docker daemon access."

Current:
container.read

Required:
container.admin
```

Forge emits:

```text
capability_request {
    agent: "forge",
    capability: "container.admin",
    reason: "Need to reproduce deployment issue",
    task: 883
}
```

Possible policies:

```text
ASK_OWNER
AUTO_TRUSTED
AUTO_PROJECT
OWNER_AUTO
DENY
```

Jarvis should enforce policy, but not become a reasoning bottleneck.

---

# 16. Agents Can Create Tools

Selected agents should be capable of extending the environment.

Potential capabilities:

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

Example:

```text
Forge:
"I need a binary inspection tool.
No suitable tool exists."

Forge:
→ writes tool
→ installs dependencies
→ registers tool
→ uses tool
→ records result
```

Future memory:

```text
Tool:
binary-inspector-v1

Created by:
Forge

Created for:
Task 928

Successful uses:
17
```

---

# 17. Memory Strategy

The memory system is central to this architecture.

The existing Jarvis memory design should be preserved and extended rather than replaced.

The full roadmap should conceptually look like:

```text
M0 - Working Memory
M1 - Episodic / Event Memory
M2 - Semantic / Structured Memory
M3 - Agent Memory
M4 - Collective / Organizational Memory
M5 - Memory Optimization / Learning
```

---

# 18. M0 — Working Memory

Temporary task-level state.

Purpose:

```text
current task
current plan
recent observations
active hypotheses
open questions
temporary scratch context
```

This should not automatically become permanent memory.

It should be consolidated selectively.

---

# 19. M1 — Episodic / Event Memory

M1 answers:

> What happened?

Store append-only events such as:

```text
Agent started task
Agent completed task
Agent failed
Agent retried
Agent called tool
Tool returned result
Agent created artifact
Agent sent message
Agent joined room
Agent left room
Agent made decision
User changed requirement
Permission changed
Model changed
Agent spawned another agent
```

Properties:

```text
append-only
timestamped
provenance-aware
replayable
auditable
deterministic where applicable
```

This event history should act as the authoritative source of what occurred.

---

# 20. M2 — Semantic / Structured Memory

M2 answers:

> What do we know?

Examples:

```text
entities
facts
projects
decisions
architecture
constraints
relationships
known bugs
experiments
tool knowledge
lessons learned
user preferences
project state
```

M2 should be derived from M1/events where possible rather than becoming an untraceable second truth source.

---

# 21. M3 — Agent Memory

M3 is needed for persistent autonomous agents.

Each agent should have its own long-term memory.

Store:

```text
agent identity
personality
specialties
experience
learned skills
past tasks
successful strategies
failed strategies
preferences
known limitations
relationships with other agents
collaboration history
tool competency
project expertise
```

Example:

```text
Agent:
Forge

Experience:
OAuth implementation
Go backend
PostgreSQL migrations

Known successful strategy:
Ask Sentry for auth review before completion

Known weakness:
Concurrency bugs

Preferred collaborators:
Sentry
Atlas
```

---

# 22. M4 — Collective / Organizational Memory

M4 answers:

> What does the entire agent organization know?

Store information such as:

```text
which agent knows what
which agents work well together
which team solved which problem
which strategies repeatedly succeed
which strategies repeatedly fail
which tools are trusted
which tools are unreliable
which agents are strongest in specific areas
shared discoveries
team decisions
group-chat outcomes
project-wide lessons
```

This is a major differentiator.

The system should remember not only individual knowledge but **organizational experience**.

---

# 23. M5 — Memory Learning / Optimization

M5 can come later.

Possible responsibilities:

```text
automatic consolidation
importance scoring
memory decay
retrieval tuning
duplicate reduction
confidence calibration
agent expertise learning
relationship learning
performance-based routing knowledge
automatic summarization
cold storage
archival policy
```

The system can gradually improve how it stores and retrieves memories.

---

# 24. Memory Scopes

Every memory should belong to a scope.

Suggested scopes:

```text
GLOBAL
PROJECT
AGENT
TASK
ROOM
PRIVATE
```

Examples:

```text
memory://global/tooling

memory://project/jarvis/architecture

memory://agent/forge/history

memory://task/891/context

memory://room/auth-race-review

memory://private/forge
```

Access control must respect memory scope.

---

# 25. Room / Group-Chat Memory

Group conversations need durable memory.

Example:

```text
room://auth-debug-918

participants:
- Forge
- Sentry
- Atlas

created_by:
Forge

reason:
OAuth race condition

decisions:
- changed token locking
- added concurrency test

artifacts:
- auth-design-v3.md
- test-results.json
```

Agents should not have to reread an unlimited raw transcript every time.

Keep:

1. raw event/message history
2. summaries
3. decisions
4. extracted facts
5. unresolved questions
6. resulting artifacts
7. final outcome

---

# 26. Provenance

Every durable memory should carry provenance.

Suggested fields:

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

Example:

```text
Claim:
"SQLite is preferred for local-first Jarvis deployment."

Source:
Atlas

Evidence:
benchmark-114.json

Decision event:
evt_8921

Timestamp:
2026-09-15

Confidence:
high
```

---

# 27. Supersession / Temporal Truth

Do not simply delete old facts when reality changes.

Example:

```text
Fact v1:
API uses v2
valid: March-August
superseded

Fact v2:
API uses v3
valid: September-current
```

This allows agents to understand why an old decision made sense at the time.

---

# 28. Conflict Handling

Multi-agent systems will produce conflicting claims.

Do not use last-write-wins as the default truth model.

Example:

```text
SUBJECT:
authentication architecture

Claim A
agent: Forge
confidence: 0.71

Claim B
agent: Nova
confidence: 0.84
evidence: docs

Claim C
agent: Sentry
confidence: 0.92
evidence: security-test.json

status:
UNRESOLVED
```

Agents should be able to notice the conflict.

Example:

```text
Forge:
"We disagree about auth.
Creating room with Nova + Sentry."
```

After resolution:

```text
resolution:
Claim C

evidence:
integration test
security test
documentation

participants:
Forge
Nova
Sentry

supersedes:
Claim A
Claim B
```

The resolution itself becomes memory.

---

# 29. Memory Consolidation

Autonomous agents will generate huge event volumes.

Do not dump all raw events directly into prompts.

Use a pipeline like:

```text
RAW EVENTS
    ↓
EPISODES
    ↓
SUMMARIES
    ↓
EXTRACTED FACTS
    ↓
STRUCTURED KNOWLEDGE
    ↓
LONG-TERM MEMORIES
```

Raw events remain available for audit and replay.

Higher layers exist for efficient retrieval.

---

# 30. Multi-Agent Concurrent Writes

Memory must support multiple simultaneous writers.

Example:

```text
Forge ─┐
Nova  ─┼──► MEMORY
Atlas ─┤
Sentry ┘
```

Protect against:

```text
race conditions
duplicate memories
conflicting facts
lost updates
stale projections
partial writes
out-of-order events
double processing
corrupt indexes
```

Recommended properties:

```text
atomic append
idempotent consumers
event IDs
causal metadata where useful
versioned projections
optimistic concurrency where needed
rebuildable indexes
transaction boundaries
```

---

# 31. Memory Retrieval

Retrieval should consider more than vector similarity.

Possible ranking signals:

```text
semantic relevance
lexical relevance
recency
importance
confidence
source quality
agent relationship
project relevance
task relevance
room relevance
temporal validity
memory scope
access permission
past usefulness
supersession state
```

Example:

Forge asks about OAuth.

Useful retrieval should prioritize:

```text
Sentry's previous OAuth finding
previous auth room decision
current project auth architecture
known security test failures
```

before irrelevant memories that merely contain the word "token."

---

# 32. Organizational Expertise Graph

Jarvis should eventually maintain a living map of expertise.

Conceptual graph:

```text
Forge
 ├─ strong_at → Go
 ├─ strong_at → Python
 ├─ weak_at → concurrency edge cases
 ├─ trusts_for_security → Sentry
 └─ collaborates_well_with → Atlas

Sentry
 ├─ strong_at → authentication
 ├─ strong_at → authorization
 └─ strong_at → cryptography
```

This graph should be learned from actual history, not only manually configured profiles.

---

# 33. Agent Relationships

Agents can maintain relationship memories.

Example:

```text
Forge → Sentry

interactions:
31

successful collaborations:
27

topics:
OAuth
permissions
tokens

confidence:
high

notes:
Sentry often finds security edge cases Forge misses.
```

Agents can use this to decide whom to contact.

---

# 34. Agent Reputation / Performance

Store objective operational signals where possible.

Examples:

```text
tasks attempted
tasks completed
review acceptance rate
test pass rate
bugs found
bugs introduced
rework rate
average retries
tool success rate
specialty-specific success
collaboration success
```

Avoid collapsing everything into one simplistic score.

The goal is better context and routing, not gamification.

---

# 35. Task Graphs

Jarvis should support explicit task relationships.

Example:

```text
TASK 100
Build authentication

├── Architect
│   └── design auth system
│
├── Forge
│   └── implement design
│
├── Tester
│   └── test implementation
│
└── Sentry
    └── security review
```

Dependencies:

```text
Architect
   ↓
Forge
   ↓
Tester
   ↓
Sentry
```

Parallel work should also be possible.

---

# 36. Decentralized Help Requests

Agents should be able to ask the network for help.

Example:

```text
broadcast_help({
    topic: "WebRTC NAT traversal",
    task: 882,
    urgency: "normal"
})
```

Interested or capable agents can respond.

This is preferable to making Jarvis choose every collaborator.

---

# 37. Agent-Created Specialist Agents

Trusted agents may be allowed to create temporary specialists.

Example:

```text
Forge needs:
Rust FFI expertise

No strong existing agent found.

Forge:
→ spawn specialist
→ assign narrow goal
→ invite specialist into task room
→ use result
→ terminate or retain agent
```

The spawned agent should have:

```text
parent/creator
purpose
permissions
resource budget
memory scope
lifecycle policy
```

---

# 38. Persistence Across Sessions

Agents should survive process restarts.

After restarting Jarvis:

```text
Forge still knows who Forge is.
Forge still has its relationships.
Forge still knows active tasks.
Forge can resume unfinished work.
Rooms still exist.
Task state still exists.
Relevant memories still exist.
```

Agent identity must not be equivalent to an in-memory Python/JS object.

---

# 39. Event-Sourced Direction

Prefer an architecture where important state can be reconstructed from authoritative events.

Example:

```text
append-only event log
        ↓
projections
        ↓
task state
agent state
room state
memory indexes
relationship graph
expertise graph
```

Derived indexes should be rebuildable.

This reduces hidden-state drift.

---

# 40. Example Event Types

Possible events:

```text
agent.created
agent.started
agent.stopped
agent.model_changed
agent.permission_changed

task.created
task.assigned
task.delegated
task.started
task.blocked
task.completed
task.failed

message.sent
message.received

room.created
room.joined
room.left
room.closed

tool.called
tool.completed
tool.failed
tool.created
tool.registered

memory.proposed
memory.accepted
memory.rejected
memory.superseded
memory.conflicted
memory.resolved

artifact.created
artifact.updated
artifact.shared

capability.requested
capability.granted
capability.revoked
```

---

# 41. Memory Write Pipeline

Agents should generally not directly mutate canonical semantic truth.

Preferred flow:

```text
Agent observation
      ↓
memory proposal
      ↓
Memory service
      ↓
validation
deduplication
provenance attachment
scope validation
conflict detection
supersession check
      ↓
accepted / rejected / unresolved
```

This prevents one hallucinating agent from corrupting shared truth.

---

# 42. Example Memory Conflict

Existing:

```text
Architecture decision:
SQLite selected for local-first runtime.
```

Forge proposes:

```text
Database is PostgreSQL.
```

Memory system should not silently overwrite.

Instead:

```text
MEMORY CONFLICT

Existing:
SQLite selected for local runtime.

Proposed:
PostgreSQL appears to be used.

Sources:
architecture decision
Forge observation

Status:
requires resolution
```

---

# 43. Human Owner Role

The user/owner should remain the highest-level authority.

The owner should be able to:

```text
create agents
delete agents
pause agents
resume agents
change permissions
change autonomy
change models
inspect conversations
inspect memory
override policies
grant unrestricted mode
revoke access
create projects
set project-wide capability policy
```

Jarvis is the platform enforcing these owner decisions.

---

# 44. Example Owner Commands

Potential interfaces:

```text
/jarvis agent forge autonomy full

/jarvis agent forge authority owner

/jarvis agent forge authority standard

/jarvis project phoenix authority owner

/jarvis room list

/jarvis task tree 918

/jarvis memory inspect agent forge

/jarvis agent pause forge
```

Natural-language commands can map to the same internal operations.

---

# 45. Suggested System Structure

One possible logical layout:

```text
jarvis/
│
├── control_plane/
│   ├── agents/
│   ├── identity/
│   ├── capabilities/
│   ├── policies/
│   ├── tasks/
│   └── lifecycle/
│
├── runtime/
│   ├── agent_loop/
│   ├── planning/
│   ├── models/
│   ├── tools/
│   └── execution/
│
├── messaging/
│   ├── direct/
│   ├── rooms/
│   ├── channels/
│   └── event_bus/
│
├── memory/
│   ├── m0_working/
│   ├── m1_events/
│   ├── m2_semantic/
│   ├── m3_agent/
│   ├── m4_organization/
│   ├── consolidation/
│   ├── retrieval/
│   ├── conflicts/
│   └── provenance/
│
├── registry/
│   ├── agents/
│   ├── tools/
│   └── models/
│
├── observability/
│   ├── logs/
│   ├── traces/
│   ├── audit/
│   └── metrics/
│
└── api/
```

Do not force this exact folder layout if the existing repository architecture uses different conventions.

Integrate with the existing architecture rather than creating duplicate subsystems.

---

# 46. Critical Rule for the Implementing Agent

**Inspect the existing Jarvis implementation before coding.**

Do not assume M1 or M2 is missing.

Determine:

```text
what already exists
what is fully implemented
what is partially implemented
what is stubbed
what is planned only
what tests exist
what schemas exist
what persistence layer exists
what event model exists
what replay guarantees exist
what retrieval already exists
```

Do not rewrite functioning memory infrastructure unnecessarily.

---

# 47. Required Audit of Existing Memory System

Before implementing M3/M4, inspect the codebase for:

## M1 / Event Memory

Verify:

```text
append-only event persistence
stable event IDs
timestamps
ordering
replay
idempotency
provenance
crash recovery
event validation
event schemas
tests
```

## M2 / Structured Memory

Verify:

```text
entity storage
fact storage
relationships
semantic retrieval
indexes
embeddings if used
metadata
provenance
confidence
temporal validity
supersession
conflict handling
rebuildability
tests
```

## Persistence

Verify:

```text
restart behavior
transaction safety
partial write behavior
migration strategy
concurrent writes
backup/recovery
```

## Retrieval

Verify:

```text
semantic ranking
metadata filters
scope filters
recency
importance
source tracking
superseded-memory filtering
```

---

# 48. Required Status Report Before Major Refactor

The implementing agent should produce a report containing:

```text
FULLY IMPLEMENTED
PARTIALLY IMPLEMENTED
MISSING
BUGS / RISKS
TECHNICAL DEBT
TEST GAPS
NEXT STEPS
```

for the current Jarvis memory system.

Do this before replacing any subsystem.

---

# 49. Recommended Implementation Order

Suggested next milestones:

## Phase A — Audit

1. Inspect existing M1/M2.
2. Run current memory tests.
3. Identify stubs/TODOs.
4. Verify replay.
5. Verify persistence.
6. Verify provenance.
7. Verify retrieval.
8. Document actual current state.

## Phase B — Multi-Agent Foundations

1. Agent Registry
2. persistent agent identity
3. direct agent messaging
4. rooms/group chats
5. task ownership/delegation
6. agent discovery
7. capability model
8. autonomy/authority separation

## Phase C — M3 Agent Memory

1. agent-scoped memory
2. identity memory
3. experience history
4. skill knowledge
5. strategy memories
6. relationship memories
7. agent retrieval APIs

## Phase D — M4 Organizational Memory

1. collaboration history
2. expertise graph
3. who-knows-what
4. team outcomes
5. room summarization
6. collective discoveries
7. conflict resolution records
8. organization-wide retrieval

## Phase E — Advanced Memory

1. consolidation
2. deduplication
3. decay/archive
4. adaptive retrieval
5. performance learning
6. relationship learning

## Phase F — Reliability

1. concurrency tests
2. crash tests
3. replay tests
4. permission tests
5. multi-agent stress tests
6. memory conflict tests
7. restart/resume tests

---

# 50. Acceptance Criteria

The architecture should eventually support this scenario:

1. User gives Jarvis a large goal.
2. An appropriate autonomous agent accepts it.
3. That agent forms its own plan.
4. It discovers that part of the task exceeds its expertise.
5. It searches the Agent Registry.
6. It contacts another agent directly.
7. Those agents decide to create a group room.
8. They invite additional specialists.
9. They divide work among themselves.
10. Each agent uses its own tools and memory.
11. They share artifacts.
12. They disagree on one technical decision.
13. Memory records conflicting claims instead of overwriting them.
14. Agents run tests/research.
15. They resolve the disagreement.
16. The resolution becomes collective memory.
17. The original agent finishes the task.
18. Jarvis records all important events.
19. The agents' relationship/expertise memories improve.
20. Weeks later, a similar task automatically benefits from what the group learned.

Jarvis should provide infrastructure throughout this process without having to make every decision.

---

# 51. Key Design Philosophy

The desired system is not:

```text
one Jarvis AI
+
several fake personas
```

It is:

```text
JARVIS PLATFORM

+
multiple persistent autonomous agents

+
peer-to-peer communication

+
group collaboration

+
shared memory

+
individual memory

+
organizational memory

+
dynamic tools

+
runtime-configurable authority

+
event-sourced history
```

The long-term target is effectively an **AI organization**.

Jarvis is the operating environment for that organization.

---

# 52. Memory Advantage

The memory architecture should provide more than simple conversation history or vector retrieval.

Its potential advantage comes from combining:

```text
append-only event history
deterministic/rebuildable state
provenance
semantic memory
structured knowledge
temporal truth
supersession
conflict tracking
agent-specific memory
relationship memory
collaboration memory
organizational memory
```

For short one-off tasks this may provide little advantage.

For agents operating for weeks or months, working repeatedly on the same projects and with the same agents, it can provide a large practical advantage because agents can remember:

```text
what happened
why decisions were made
what worked
what failed
who helped
who knows what
which tools worked
what the project looked like at that point in time
which beliefs were superseded
how disagreements were resolved
```

The goal is not merely "better recall."

The goal is **persistent organizational intelligence**.

---

# 53. Important Non-Goals

Do not:

- make Jarvis choose every speaker in every conversation
- route every agent message through central LLM reasoning
- store every raw message directly in prompts forever
- allow last-write-wins to define truth
- bind agent identity permanently to one LLM provider
- make agent permissions impossible to change at runtime
- duplicate existing M1/M2 systems without auditing them
- destroy raw event history when creating summaries
- mix private agent memory with global memory without scopes
- treat "personality prompts" as sufficient for real agents

---

# 54. Initial Deliverable Requested From Implementing Agent

When this document is handed to an agent inside the Jarvis repository, the first job should be:

> Inspect the existing Jarvis codebase and map the current implementation against this specification.

Produce:

```text
1. Current architecture map
2. Current M1 status
3. Current M2 status
4. Existing agent/runtime capabilities
5. Existing messaging capabilities
6. Existing permission/capability system
7. Gaps against this specification
8. Recommended implementation sequence
9. Exact files/modules likely to change
10. Tests required
```

Then begin implementation in the smallest safe vertical slices.

---

# 55. Project Location

Current Jarvis project path supplied by the owner:

```text
<operator-selected JARVIS repository>
```

The implementing agent should treat the code in that repository as authoritative and adapt this specification to the actual existing code.

---

# 56. Final Direction

The core direction is:

> **Do not turn Jarvis into the central mind controlling smaller agents.**

Instead:

> **Turn Jarvis into the durable runtime, memory substrate, communications layer, control plane, and operating environment that allows many autonomous AI agents to exist, collaborate, learn, and work independently.**

The agents should be able to:

```text
think independently
plan independently
act independently
communicate directly
request help
delegate work
create group chats
invite specialists
share artifacts
disagree
resolve disagreements
learn from outcomes
remember each other
remember projects
remember failures
remember successes
create tools
request more authority
resume work later
```

while Jarvis provides:

```text
identity
memory
persistence
events
permissions
tools
tasks
messaging
observability
replay
owner control
```

That is the target architecture.
