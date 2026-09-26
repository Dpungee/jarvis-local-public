# Runtime-memory contract reconciliation, revision 2

Date: 2026-09-16. Branch: `codex/upgrade-readiness`; baseline
`93119c421cdea4fffafcd3d13caef3fccfe15a29`.

Status: Codex response to Claude's independent review, documentation only.
This is the proposed successor to `RUNTIME_MEMORY_CONTRACT_REVIEW.md`, not a
jointly signed contract. Claude has not reviewed this revision. Operator approval
of reconciliation work does not authorize implementation, migrations or dispatch.

Inputs: the original Codex review, `RUNTIME_MEMORY_REPLAY_HANDOFF.md`, and
`CLAUDE_RUNTIME_MEMORY_CONTRACT_REVIEW_2026-09-16.md` in the shared collaboration
folder. Claude's document is preserved unchanged. Its source-inspection findings
are review evidence, not newly executed tests or proof of an implemented bridge.

## Codex dispositions

| Decision | Codex response to Claude | Remaining condition |
| --- | --- | --- |
| D1 Ownership | Accept separate control plane and bounded rebuilds | Preserve derivation provenance without confusing it with rebuild ownership |
| D2 Project identity | Accept exact, versioned operator-managed mapping data | Pin a configuration version, not project data in executable code |
| D3 Scope/access | Accept no initial retrieval, participant records and historical intervals | Historical eligibility is not sufficient present-day authorization |
| D4 Projections | Accept structure/references/digests only for the initial bridge | Digests are still sensitive; no new on-demand text access surface |
| D5 Recovery | Accept durable structural halt and content quarantine | Specify transaction, concurrency and deterministic rebuild semantics |
| D6 Legacy | Accept fail-closed default and audited optional bootstrap | Bootstrap remains disabled without separately approved boundary |
| D7 Trust | Accept deterministic screens and honest integrity provenance | Validate free-form identifiers too; screens are not authorization |

Both reviews support the core architecture. The wording below is Codex's proposed
resolution of implementation ambiguities, not evidence of Claude's acceptance.

## Replacement contract

### D1: Ownership, rebuild boundaries and provenance

Runtime remains authoritative for its control-plane entities and events. Memory
owns derived projections and memory-authored facts; neither becomes an alternate
runtime authority. Rebuild only bridge-owned tables/rows, never claims, lessons,
promotions, summaries, audit history, erasure records or other memory-authored data.
Prove unchanged values, identities and counts for protected rows.

A direct runtime projection has a source event reference. A memory-authored
summary or claim is not a bridge projection, even when based on runtime evidence.
It must retain honest source references in separate provenance without becoming
rebuild-owned. A nullable `runtime_event_id` can mark direct projections in a
specified schema, but null must not mean "no runtime evidence was used." Do not
add that column to all memory tables merely to express ownership. A separate
origin/rebuild classification or segregated bridge tables can enforce the boundary.
Do not automatically regenerate erased derivations during replay.

### D2: Exact mapping and reproducible configuration

Use case-sensitive exact runtime project strings mapped to existing memory IDs;
never coerce, invent projects, or infer authorization. Reject unmapped/ambiguous
values; null is absence. No many-to-one aliases in the initial contract.
Keep immutable/versioned mapping snapshots as operator-managed data and pin the
consumer's mapping, reducer and screening-policy versions. Running configuration
must match the stored consumer version before import; executable code need only
support the versioned format, not hard-code a particular operator mapping.

Mapping changes require an explicit rebuild/cutover plan. Preserve the old mapping
for reproducibility; a single mutable table with a version column is not enough
if it destroys prior versions. Build a separate generation and validate before
cutover; do not silently reinterpret existing rows. Mapping configuration itself
may contain sensitive project strings and is not a public log or agent surface.

### D3: Historical evidence is not permission

Initial bridge projections have no agent-facing retrieval, including structural
metadata and digests. Scope labels partition data, not authority. Future access
must be deterministically filtered before materialization into an agent-facing
result, using explicit participants, event-time membership/ownership evidence and
the approved current authorization/revocation policy. A historical member is not
automatically entitled after removal; a newly joined member is not automatically
entitled to earlier content. Intervals alone settle neither policy.

DM participants include sender and recipient. Invitation is not active room
membership; room creation must account for creator membership. Room messages are
not two-party DMs: do not invent one recipient. State exactly how event-time room
membership is reconstructed and how unassigned tasks and interval endpoints work.
Recommend half-open sequence intervals with tests at join/leave/delegation edges.
Missing ACL evidence never broadens access. Exposure waits on explicit policy.

### D4: Narrow, content-minimized projections

Initial projections may retain validated structural fields, opaque references,
content hashes and lengths; do not copy message bodies, personality/purpose text,
task descriptions/results, prompts/options, or other runtime-owned free text.
An outcome means a closed status value, not an unrestricted result string.
Avoid optional display labels and artifact URIs in the first slice; later retained
text requires a field-level justification and screening policy. Arbitrary model
names, selected options and project strings are not inherently safe identifiers.

Hashing is not anonymization or erasure: short values may be guessable and hashes,
participants and references remain sensitive. Memory deletion does not erase the
runtime source. No end-to-end erasure claim is justified until cross-store
retention and erasure are designed. Rebuilds must preserve any applicable durable
suppression decisions; derived-content generation is a separate governed path.

The existing trusted replay reader is not a new permission to fetch text on
demand for an agent. Any future content fetch needs the same approved access and
retention boundaries. Keep runtime names out of SPINE_KINDS; any new bridge audit
kinds require a specific reviewed migration, not a catch-all event kind.

### D5: Atomic batches, persistent halts and quarantine

Successful batches commit projections, quarantine outcomes, batch audit and the
paired sequence/event-ID cursor in one memory transaction. Check the starting
cursor, generation, versions and active status under serialization/CAS. Use
deterministic event-plus-item keys. Runtime commits remain separate.

On a structural failure, discard all candidate batch projection changes and keep
the cursor unchanged. Persist a sanitized halt record under the same concurrency
guard. One valid design is an outer transaction with a savepoint for projections:
roll back to the savepoint, then commit only halt/audit state. If the import
transaction was already rolled back, use a separate guarded halt transaction;
a stale worker must not halt a newer consumer state. Never claim a halt survives
if it was written inside a transaction that was then rolled back.

A halted consumer performs no further ingest reads until an explicit audited
operator resume. Failure to persist the halt (for example, unavailable storage)
must stop that worker and surface an operational error, not report durable halt
success. Diagnostic text contains codes/IDs, not private input. Status reporting
is required before production, but changing Memory.recall is a separate behavior
change, not part of a schema-only slice.

Content rejection may quarantine an optional display/content field or its
non-authoritative projection item while the validated structural event advances.
Retain only digest, safe identifiers, reason code and versioned screening outcome;
never the rejected value. Do not drop membership, ownership or other security
evidence and continue as though the ACL were complete. Invalid required structure
halts or leaves explicitly inaccessible state under a separately specified rule;
the initial recommendation is halt. Catch-all exceptions are not quarantine.

Persist quarantine/suppression decisions outside disposable projection state.
For a fixed generation, replay must reproduce the same outcomes using pinned
screen versions and durable decisions. A changed screen, changed reducer, or
operator release requires an audited new generation/reprocessing plan; rebuild
must not silently revive suppressed content. Batch audit distinguishes projected,
quarantined and intentionally ignored inputs without logging payloads.

### D6: Legacy and audited boundaries

Default to halt on schema 1, gaps, unknown vocabulary or a cursor mismatch; no
skip, deletion, fabricated backfill or silent reseed. Nonzero bootstrap stays
disabled initially. A future approved bootstrap must validate the proposed
boundary read-only, then atomically commit its attestation and cursor before the
first ingest beyond that boundary. "Before any read" cannot literally apply to
the reads needed to validate the boundary. Record truthful excluded coverage,
approver and reason; preserve the attestation through rebuilds. Missing history
must never be presented as complete. No legacy-store migration is approved here.

### D7: Screens and integrity levels

Apply the approved deterministic secret/private-identifier/instruction-like
screens to retained untrusted text, with versioned outcomes. Closed enums, parsed
timestamps, validated opaque IDs and fixed-format digests have their own strict
schemas. Free-form strings do not become exempt merely by being called IDs.
Screens reduce exposure; they do not authorize retrieval or prove text harmless.

Record runtime source integrity as unkeyed snapshot hashing, distinct from any
keyed memory audit protection of the ingestion record. A keyed receipt does not
authenticate the original producer. Treat command_sha256 as opaque provenance,
not a follower-recomputable checksum. Neither side weakens append-only guards.

## Corrections needed in the implementation plan

1. Claude's event table describes **seven** no-op types and **23** projected types,
   not six and 24: direct acknowledgement, room cursor, task dependency and four
   capability events are the seven. Runtime vocabulary contains 30 types.
2. Claude lists four candidate new spine kinds, not three: `memory.resolved`,
   `m0.plan_revised`, `bridge.batch_applied`, `bridge.bootstrap_accepted`. The first
   two are separate memory work. Bootstrap can be deferred if unsupported; agree
   the exact per-migration set before modifying any closed CHECK constraint.
3. The five listed projection tables do not specify storage for all proposed room,
   artifact, task-status and collaboration projections. Require a complete
   event-to-fields-to-table manifest before expanding past agent events.
4. An agent-only B-1 must not permanently consume other families as temporary
   no-ops and later claim complete history. Recommend bounded, disposable test
   stores with agent-only histories initially. Unknown/unsupported families halt.
   Later additions need a new generation replaying the original eligible history;
   intentional no-op mappings must be pinned per generation.
5. A body-free bridge cannot use a malicious DM body as proof that a retained-text
   screen runs. Revise T14 to a field actually retained in that slice; if the
   first slice retains no such text, defer the test and state that limitation.
   Test that body content is neither persisted nor able to change control flow.
6. Strengthen schema checks: nonnegative cursor sequence, complete halt-state
   consistency, exact identifier grammar and mapping-version history. Candidate
   DDL is illustrative, not a ready-to-run migration.

## Acceptance additions to the original and Claude test plans

All cases below are requirements, not newly implemented or executed tests.

- Halt write survives a rejected batch and restart; persistence failure is visible;
  concurrent stale failure cannot halt or regress a newer generation/cursor.
- Quarantined fields remain suppressed on rebuild; screen-version changes cannot
  silently release them. Security-bearing structural evidence is never skipped.
- Memory-authored rows and their source provenance survive projection rebuild;
  erased derivations do not reappear. Rebuild does not regenerate summaries.
- No agent-facing retrieval exists in the first slice. Later ACL tests cover
  historical eligibility AND current revocation, invitations, interval boundaries,
  multi-member rooms, unassigned tasks and missing historical evidence.
- Full family coverage count is 30; unsupported families halt the thin bridge.
  A family introduced later recovers eligible prior events through explicit replay.
- Mapping/screen/reducer snapshots are pinned; a new generation is validated before
  cutover. Bootstrap audit and cursor commit atomically, with declared coverage.
- No private body/label/URI leaks into projections, quarantine, halt reasons or
  batch logs; hashes are treated as sensitive, not anonymized public data.

## Operator choices: recommended initial boundaries

These recommendations require explicit approval; none is inferred from "do it"
for this documentation reconciliation.

| Choice | Recommendation now | Deferred |
| --- | --- | --- |
| A: Ownership and identity | Runtime control-plane authority; exact versioned project map in memory | No competing registries or automatic project creation |
| B: Content and access | Structure/digests only; no agent retrieval or on-demand text access | Historical/current ACL and end-to-end retention/erasure design |
| C: History and rollout | No legacy bootstrap; halt visibly; test-store-only first implementation | Production activation, live migrations and excluded-history approval |

After these boundaries and the exact revised contract are accepted, the next
candidate step is a separately authorized schema/migration-test slice. Claude
must first supply corrected DDL and exact audit-kind changes, including durable
version/suppression state. Do not assume the audit's entire schema-51 scope is
automatically authorized by approving a bridge. No production launch or agent
fact-writing policy change is included. No work request has been sent to Claude.

## Verification and stop point

Documentation reconciliation only: this file, the original review's successor
pointer, and PROJECT_STATUS.md. The two source reviews are not merged by rewriting
Claude's text. Runtime and memory implementation remain unchanged by this phase.
No executable tests were rerun; the earlier 4,260-run/7-skip result is historical
runtime evidence, not verification of these proposed bridge semantics.

Completion means a saved, checked Codex response with open choices, not mutual
agreement. Next: operator chooses initial boundaries; Claude reviews revision 2
only after an approved assignment or a user-supplied prompt. Implementation still
requires separate authorization. No commit, push, publication or live-store access.
