# Runtime correction and memory replay handoff

Date: 2026-09-16. Branch: `codex/upgrade-readiness`, baseline
`93119c421cdea4fffafcd3d13caef3fccfe15a29`. Working-tree implementation only;
nothing committed or published. No memory implementation or Claude-owned file was
changed. This document is a review handoff, **not an assignment to Claude**.

## Status and authority

Implemented and tested on the runtime side: the task lifecycle correction below,
event schema 2 historical content snapshots, append-only SQL guards, and a bounded
event-only replay reader. Runtime database schema remains 1; new events use schema
2. Opening an existing runtime store installs the SQL guards without rewriting its
event history. No live user store has been opened or migrated by this work.

Claude's revised memory audit section 10.0 withdraws the duplicate memory-side
registry, rooms, messages, and task ownership. That aligns with the existing
runtime implementation. It is **not evidence that the two tasks have mutually
accepted this new replay contract**. No message or work assignment was sent to
Claude. The audit's section 16 still requests operator confirmation of ownership
and the integration choices. No mutual agreement is claimed here.

## Corrected task lifecycle

Only an owning, running agent can add a prerequisite, and only while the task is
`OPEN`, `ASSIGNED`, or `BLOCKED`. A `RUNNING` task rejects every new prerequisite,
including one already completed. The owner must explicitly transition the task
to `BLOCKED`, add dependencies, then resume after all prerequisites complete.
Terminal tasks still reject additions. A retry with the original idempotency key
for an already committed edge is a replay, not a new addition.

The status check, edge insertion, and event append are serialized in the same
`BEGIN IMMEDIATE` transaction. Racing a start against a prerequisite addition
cannot commit both. Rejection changes neither the task, edge set, nor event log.
`verify_integrity()` also rejects a legacy/corrupt `RUNNING` or `COMPLETED` task
with an unfinished prerequisite. It reports inconsistency; it does not silently
repair history or state.

## Implemented historical-content contract

Each schema 2 event retains its existing envelope and operation metadata and adds:

- `payload.record`: the full command-result record **at that event's append time**,
  copied in the same transaction as the mutation. The fields are explicitly
  allowlisted by result kind in `_EVENT_RECORDS`; future table columns are not
  automatically exported.
- `payload.record_sha256`: SHA-256 of that record's canonical JSON (sorted keys,
  compact separators, UTF-8, no ASCII escaping). This detects accidental content
  corruption; it is **not a signature, keyed chain, or proof against an attacker
  able to edit the database and checksums**.

The snapshot is a JSON object using the runtime's stored column names and scalar
values. Enum values are uppercase strings; timestamps are epoch-second numbers;
nullable fields remain null. `specialties_json` and `options_json` remain canonical
JSON **strings inside the object**, not decoded arrays. `result_kind` chooses the
record schema; its ID field must match `result_id`.

Envelope entity IDs keep the existing prefix plus 32 lowercase hex grammar.
`actor_id` is not always an entity reference: trusted operator/system events can
use the reserved strings `owner` and `system`. Do not misclassify these as agent
IDs or create fake agents for them. Correlation defaults to the event's own ID;
explicit correlation and causation IDs must already exist at command time.

| Event types (literal names) | Result kind / historical content |
| --- | --- |
| `agent.created`, `agent.running`, `agent.paused`, `agent.stopped`, `agent.model_changed`, `agent.policy_changed` | `agent`: identity, full profile and model/policy/lifecycle at that time |
| `message.direct_sent` | `direct_message`: full body, sender/recipient, reply and task references |
| `message.direct_acknowledged` | `direct_message_receipt`: delivered/read state |
| `room.created`, `room.agent_invited`, `room.agent_joined`, `room.agent_left` | `room`: room record; membership transition remains in event type, actor and operation metadata |
| `room.message_sent` | `room_message`: full body, room/sender/reply/task references |
| `room.cursor_advanced` | `room_message_cursor`: member's consumption position |
| `task.created`, `task.dependency_added`, `task.running`, `task.blocked`, `task.completed`, `task.failed`, `task.cancelled` | `task`: full title, description, owner, status and result, including original null results |
| `task.delegated` | `delegation`: task, from/to agents and full reason; task ownership transition is identified by this event |
| `capability.requested`, `capability.denied` | `capability_request`: reason, policy, scope and decision state |
| `capability.granted`, `capability.revoked` | `capability_grant`: grant record; grant decision reason is retained in operation metadata |
| `artifact.shared` | `artifact`: URI, media type, hash, byte count and sharing references; **not artifact bytes** |
| `collaboration.requested`, `collaboration.closed` | `collaboration_request`: full prompt/options and request state |
| `collaboration.responded` | `collaboration_response`: full body, choice and evidence reference |

This is a result snapshot plus operation event, **not a generic dump of every
affected table**. In particular, a delegation result is not a second task snapshot,
and a targeted collaboration response implicitly closes its referenced request
under the existing runtime protocol. A complete runtime-database rebuilding tool
is not delivered. Consumers must define which records they project, how they use
the operation metadata, and test those reductions before integration.

SQL triggers reject UPDATE, DELETE, and conflicting INSERT/REPLACE against event
rows. Process code holding the store/SQLite connection is trusted; these guards do
not defend against dropping triggers, replacing database files, or filesystem
attackers. Returned Python payload dictionaries are detached copies, not writable
handles into the event log.

## Replay and recovery

Use the trusted-store `replay_events(after_sequence=0, after_event_id=None,
limit=1000)` reader for content-dependent projections, not current-state getters.
Limits are 1 through 5000. Subsequent batches must supply **both** the last sequence
and its event ID. A missing/mismatched predecessor rejects a stale or replaced-store
cursor. Each returned batch is contiguous, schema 2, known-vocabulary, and has the
expected snapshot fields, result identity and checksum. There is no filtered
content-replay cursor: consume the global sequence before choosing projections.

The existing `events()` reader remains available for raw inspection and legacy
diagnostics; it does not provide the content-completeness guarantee. Integrity
checking still accepts known legacy schema 1 envelopes, but `replay_events()`
refuses a batch containing them. Unknown event versions/types also fail closed.
**No backfill from current rows is performed:** historical mutable content cannot
be honestly recovered that way. Existing schema 1 history needs an explicit
operator-approved migration/recovery boundary or authentic historical source;
do not silently skip it, advance a memory cursor past it, reset it, or delete it.
A nonzero replay cursor validates its predecessor and subsequent batch, not all
earlier history; obtaining a trusted initial cursor is the consumer's obligation.

Proposed memory-side use, pending Claude review:

1. Read the memory consumer's sequence/event-ID pair (initially 0/null).
2. Obtain a validated runtime batch. A runtime commit and a memory commit remain
   separate; there is no cross-file atomic transaction or cross-file FK.
3. In **one memory transaction**, check that the cursor has not changed, project
   the complete batch, and advance both cursor fields. Serialize competing
   consumers or use compare-and-set so a stale worker cannot regress the cursor.
4. On crash/error, roll back both projection changes and the cursor; retry the
   batch. Deduplicate by runtime event ID. If one event produces several rows in
   one projection, use a deterministic composite key such as
   `(runtime_event_id, projection_item_key)`, not an event-ID-only unique key.
5. On gaps, missing content, unknown schema/vocabulary, or a mismatched cursor,
   stop and surface the error. Do not route errors into invented memory facts.

The reader and event-only recovery tests are implemented. The above memory
transaction, idempotent reducer, cross-store crash recovery, and production adapter
are **not implemented or verified** here and remain Claude-owned work after
agreement/authorization.

## Project identity: proposal, not an imposed migration

Observed runtime contract: optional case-sensitive free-form TEXT `project_id`,
maximum 200 characters after input normalization; it is not an opaque entity ID
and is not restricted to decimal integers. Existing tests use `project-aurora`.
Memory's `agent_projects.id` is INTEGER. Neither can be silently substituted for
the other. Null is absence of a project, not a wildcard or a memory project ID.

Proposed integration: an explicit, validated mapping of exact runtime project
strings to existing memory project integers, owned at the bridge boundary. Reject
unmapped values and ambiguous mappings; do not coerce arbitrary strings with
`int()`, merge `01` with `1`, or invent projects. Claude's cheaper decimal-string
proposal is an alternative only if the operator chooses to constrain new runtime
IDs and explicitly migrate existing values. **Operator choice and both tasks'
acceptance are still required; no project-ID behavior changed in this patch.**

## Scope mapping: proposal, not an authorization shortcut

Preserve runtime `(scope_kind, scope_id)` plus `project_id` and the event's actor
and participants in provenance. Proposed memory rendering, after project mapping:

| Runtime scope | Proposed memory representation |
| --- | --- |
| `GLOBAL` / null | `global` |
| `PROJECT` / runtime project string | `project:<mapped memory integer>` |
| `AGENT` / opaque agent ID | `agent:<agt_id>` |
| `TASK` / opaque task ID | `task:<task_id>` |
| `ROOM` / opaque room ID | `room:<room_id>` |
| `PRIVATE` / opaque agent ID | `private:<agt_id>` |

This is a representation proposal only. Runtime direct-message events use PRIVATE
with the recipient as scope ID, but their sender is also a participant. A private
label alone cannot describe that ACL. Room membership changes over time, and
task ownership can change; scope labels do not automatically authorize historical
content access. Claude and the operator must settle participant visibility,
historical membership/ownership access, and retention/redaction policy before
exposing memory projections. Unknown or invalid mappings must fail closed, never
broaden to global/project visibility.

Raw `events()` and `replay_events()` expose the trusted store's complete history,
including private bodies and profile text. They are not on AgentRuntimeContext;
do not expose either as an ordinary agent tool, dump them into general logs,
publish them, or treat the snapshot as executable instructions. Runtime text is
untrusted content. This patch adds no new tool authority or relaxed memory gate.
Full snapshots increase local storage and retain historical sensitive content;
no encryption, compaction, erasure, or retention engine is added.

## Vocabulary ownership and open decisions

Runtime owns its event names/version and result schemas above. Recommended bridge
behavior is an explicit versioned allowlist/reducer; retain the source event ID,
sequence, version and type in provenance. Do not add runtime type strings directly
to memory `SPINE_KINDS` or weaken its closed CHECK. Claude owns the memory event
representation. Unknown runtime types must pause integration for review, not become
an untyped catch-all. Agreement on that mapping remains outstanding.

Operator/Claude decisions still open: control-plane ownership confirmation,
project-ID strategy, scope/participant ACLs, memory event mapping, and schema 1
history/retention handling. The independent audit decisions on agent fact writes,
skill promotion, peer-blindness, embeddings and Council are unchanged. This
correction is not authorization to start those workstreams or execution loops.

## Verification

- Before the correction, two targeted reproductions failed as expected: RUNNING
  accepted a new prerequisite; historical message events had no `record` content.
- Runtime suite: 30 tests, all passed with no skips (2.731 seconds); combined runtime/specialist/Council/long-horizon/
  memory-spine/authority suite: 176 tests, all passed, no skips (6.976 seconds).
- Tests cover explicit block/add/resume, completed-prerequisite rejection,
  idempotent retries, competing writers, legacy inconsistent-state detection,
  exact message/task/model history after restart and projection mutation, denial
  of all projection reads during replay, cursor boundaries, SQL UPDATE/DELETE/
  REPLACE guards, schema 1 refusal, corruption/unknown versions/types, and gaps.
- Complete suite: 4,260 tests run in 684.352 seconds, exit 0,
  `OK (skipped=7)`; 4,253 non-skipped tests passed, no failures or errors.
- Focused Ruff lint, high-severity Bandit scan, and whitespace checks passed.
- Seven unclosed-SQLite `ResourceWarning` diagnostics remain in existing test
  activity. Informational learning-ladder timings exceeded several budgets with
  enforcement disabled; passing tests do not mean all performance targets passed.

Commands used (the prepared worktree's `.venv` interpreter):

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_multi_agent_runtime
.\.venv\Scripts\python.exe -m unittest tests.test_multi_agent_runtime tests.test_specialists tests.test_council tests.test_long_horizon tests.test_memory_spine tests.test_agent_authority_gates
.\.venv\Scripts\python.exe -m unittest discover -s tests
.\.venv\Scripts\python.exe -m ruff check jarvis/multi_agent_runtime.py tests/test_multi_agent_runtime.py
.\.venv\Scripts\python.exe -m bandit -q jarvis/multi_agent_runtime.py -lll
git diff --check
```

Environment-only exclusions independently checked: one benchmark dataset-value
scan has no cached dataset, three live Docker tests have no daemon, and the three
sealed ladder/graph/retrieval evaluations lack their explicit run tokens. None of
those gates was weakened, supplied a fabricated token, or counted as passed.

Preservation checks: staged preparation diff compared byte-for-byte unchanged;
SHA-256 checks of the preparation files and Claude-owned audit/handoff unchanged;
no memory implementation diff. This correction modified the runtime module, its
tests, the runtime design document, `PROJECT_STATUS.md`, and `CHANGELOG.md`; it
added this contract and a shared collaboration-folder correction handoff. The
Phase A audit and all unrelated staged/unstaged preparation remain intact.

Stop point: corrections and this handoff only. No live memory integration,
autonomous-model collaboration, production migration, commit, push, publication,
or execution-loop expansion was performed.
