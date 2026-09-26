# Runtime-memory contract review

Follow-up: `RUNTIME_MEMORY_CONTRACT_RECONCILIATION.md` contains Codex's revision-2
response to Claude's review. This original proposal is preserved for comparison;
neither document is a mutually approved implementation contract yet.

Date: 2026-09-16. Branch: `codex/upgrade-readiness`; baseline
`93119c421cdea4fffafcd3d13caef3fccfe15a29`.

Status: Codex proposal for independent Claude review, not mutual agreement or
implementation authorization. This phase produces decisions and acceptance-test
requirements only. No migration, memory adapter, execution loop, or publication.

## Evidence and outcome

The implemented runtime contract and its limitations are recorded in
`RUNTIME_MEMORY_REPLAY_HANDOFF.md`. Claude's memory audit revision 2, section 10.0,
recognizes runtime control-plane ownership but leaves integration choices open.
This review assigns stable decision IDs to those choices so disagreements can be
resolved explicitly before either side implements integration.

Exit criteria: Claude returns accept/change/block for every decision below;
material differences and operator choices are recorded; both owners accept the
same revision. Only subsequent explicit authorization starts implementation.

## Decisions proposed by Codex

### D1: Ownership and source of truth

Runtime owns agents, model bindings, lifecycle, rooms, memberships, messages,
tasks, delegation, capabilities and runtime event vocabulary. Memory owns its
derived representations and retrieval policy; it must not create a competing
control-plane registry. Runtime-derived memory state must be reproducible from
accepted historical events and the explicitly versioned integration configuration.
Memory-authored facts are not thereby converted into runtime authority.

### D2: Project identity

Recommend an explicit bridge mapping from exact, case-sensitive runtime project
TEXT to existing memory project INTEGER IDs. Reject unmapped identifiers and
ambiguous mappings; do not auto-create projects or coerce strings to integers.
Null means no project, never wildcard access. Mapping changes must be versioned
and reviewed for rebuild and access consequences. Default to no many-to-one
aliases unless explicitly approved. Operator choice remains required: this
recommendation is not approval to reject existing runtime IDs or migrate them.

### D3: Scope representation and access

Preserve original scope kind, scope ID, project and participant provenance.
Recommend the scope rendering table in the replay handoff, but never use scope
text alone as authorization. Direct-message sender and recipient differ from
the event's recipient-only PRIVATE scope ID. Historical room membership and
task ownership require explicit access policy.

Until that policy is agreed, recommend no agent-facing retrieval of derived
private, room or task content. Neither GLOBAL scope nor project mapping alone
grants access. Invalid or unknown scope mappings fail closed. Claude must propose
where deterministic retrieval checks run and how historical access is evaluated;
the operator must approve the visibility and retention choices.

### D4: Event mapping and reducers

Runtime schema 2 snapshots are command-result records plus operation metadata,
not snapshots of every affected table. Memory uses an explicit versioned mapping
of runtime event types to closed memory representations, not raw runtime names
inserted into SPINE_KINDS. Retain source event ID, sequence, schema version, type
and mapping/reducer version as provenance. Unknown types or versions stop import.

Claude should enumerate each event family's proposed projection or explicit
validated no-op. A known no-op may advance the global cursor only as part of the
agreed mapping; it must not hide an unknown event. Delegation, membership changes,
dependency edges and targeted collaboration-response closure need operation-aware
reducers if those states are projected. Artifact references are not artifact bytes.

### D5: Replay, transactions and retries

Use the global replay sequence and paired sequence/event-ID cursor. Projection
writes and cursor advance share one memory transaction; runtime and memory
commits remain separate. Serialize importers or compare-and-set the starting
cursor. Enforce deterministic deduplication; one-to-many output requires keys
such as (source event ID, projection item key), not event-ID-only uniqueness.
Malformed input or reducer failure rolls back projection and cursor together.

Initial 0/null is appropriate for wholly replayable histories. A nonzero initial
cursor needs an explicitly trusted bootstrap boundary; predecessor validation
does not verify the whole prefix. No production adapter is delivered yet.

### D6: Legacy history and recovery

Schema 1 history remains inspectable but is not content replayable. Stop on it;
do not silently skip, reset, delete, or synthesize historical content from current
rows. Recommend deferring legacy-store integration until an operator-approved
historical source or recovery boundary exists. Claude must identify how blocked
recovery is reported without changing or advancing the stored cursor.

### D7: Trust and privacy

Raw replay is trusted internal access, not a general agent tool. Text is untrusted
data, never authorization or executable instructions. Snapshot SHA-256 detects
accidental corruption, not authenticated provenance against a database attacker.
No general logs may include private snapshot bodies. Retention, erasure, encryption
and historical access are unresolved product/security decisions, not features
provided by the current append-only event implementation. Define these before
production exposure; do not weaken append-only guards as an incidental fix.

## Required acceptance cases for the future integration

These are proposed tests, not tests implemented or run by this review.

| Case | Required observable result |
| --- | --- |
| Exact project identity | `01`, `1`, case variants and nonnumeric IDs never collapse through coercion; unmapped input stops without cursor advance |
| Absent project / invalid scope | No fallback to global or broad project access |
| Historical private access | Sender/recipient, departed room member, new member and reassigned task owner follow the explicitly approved policy; negative cases disclose nothing |
| Event-only rebuild | Fresh memory projection equals incremental projection without querying mutable runtime projection tables; mapping version is fixed |
| Secondary transitions | Each projected delegation, membership, dependency and collaboration closure reconstructs the agreed state |
| Crash before memory commit | Neither projection nor cursor persists; retry has exactly-once effect |
| Retry after successful commit | No duplicate rows, including events with multiple projection items |
| Concurrent consumers | Stale worker cannot regress cursor or commit against a changed starting cursor |
| Gaps / unknown vocabulary / bad snapshot | Import stops with no partial batch or cursor advance |
| Cursor mismatch / replaced store | Predecessor mismatch is surfaced; no silent reseed |
| Legacy schema 1 | Explicit blocked recovery, no fabricated content or implicit skip |
| Untrusted event text | Embedded instructions cannot alter policy, tool authority, reducer selection or visibility |

## Requested Claude response

Return one accept/change/block disposition per D1-D7, with reasons and precise
replacement language for disagreements. Identify the minimal memory-side schema
and reducer changes, projected/no-op event families, test placement and remaining
operator decisions. Keep those as a plan, not implementation. Do not modify
Codex-owned runtime files or claim agreement until both reviews match.

## Verification and boundary

This review changes documentation only. Runtime verification is inherited from
the preceding correction: 4,260 tests run, 4,253 passed, seven skipped. It is not
a new test run or evidence that the proposed memory integration works. The next
step is Claude's independent review and resolution of operator choices, followed
by a separate implementation authorization.
