# The memory spine (VTMF M2, slices 1 and 2)

Every durable memory write is an event on one append-only spine, and every
derived row carries the id of the event that produced it. Slice 1 put the
claim projection (structured facts, including governed project facts) on the
spine at schema 46; slice 2 (schema 47) adds lineage and digest-only receipts
for ordinary memories and lessons, the memory rebuild, and the claim
rebuild's apply step. The long-horizon ledger keeps its own hash-chained
table.

Schema 48 (VTMF M3) adds a second projection derived from the first, the
temporal graph, and the ordinary-memory erase this document listed as missing.
The graph adds no event kind of its own: its only receipt is the existing
`projection.rebuilt` with `projection: "graph"`. See
[The temporal graph](MEMORY_GRAPH.md).

## The contract

`memory_spine_events` (schema 46, extended at 47) is append-only in SQLite
itself: a `BEFORE DELETE` trigger aborts every delete, and a `BEFORE UPDATE`
trigger permits exactly one change, a tombstone-backed redaction that nulls
`payload_json` and `payload_salt` together and records the tombstone's id.
Every other column is immutable.

| column | meaning |
|---|---|
| `id` | the spine order; explicit, contiguous, never reused |
| `created_at` | UTC; never earlier than the previous event (bumped by 1 µs when needed) |
| `kind` | closed enum: `spine.genesis`, `claim.imported`, `claim.created`, `claim.reasserted`, `claim.superseded`, `claim.disputed`, `claim.retracted`, `claim.tombstoned`, `proposal.not_stored`, `proposal.confirmed`, `conversation.deleted`, `projection.rebuilt`, and since slice 2 `memory.imported`, `memory.created`, `memory.reasserted`, `memory.updated`, `memory.deleted`, `lesson.created` |
| `actor` | `operator`, `runtime`, `model`, `worker`, `companion`, `system` |
| `source` | the same bounded non-secret source the derived row carries |
| `scope` | `global` or `project:N` |
| `permission` | the autonomy mode and turn origin in force (`autonomous:interactive`, `runtime`, `migration`) |
| `conversation_id` | the conversation the operator was in, if any (no foreign key: events outlive conversations) |
| `subject_kind` / `subject_id` | forward lineage to the derived row (`claim`, `conversation`, `proposal`, `projection`, `memory`) |
| `parent_event_id` | backward lineage inside the spine |
| `outcome` | `applied`, `rejected`, `noop` |
| `payload_json` / `payload_salt` / `payload_sha256` | closed per-kind payload; the digest is over the salt and the canonical payload, so a redacted event's surviving digest is unconfirmable for a low-entropy value such as a port number |
| `prev_sha256` / `event_sha256` | the chain: `event_sha256` is an HMAC-SHA256, keyed by a 32-byte key kept in the sidecar `<database>.memory-spine.key`, over the canonical JSON of every immutable column including `prev_sha256` |

A one-row `memory_spine_head` table names the newest event (`last_event_id`,
`last_event_sha256`) under its own keyed MAC and is updated in the same
transaction as every append, so removing the tail of the chain is detected as
well as altering, inserting, or reordering events.

The key sidecar is created only when a store gets its spine (migration 46),
is 64 hex characters, and must travel with backups of the database: a store
that has a spine but no sidecar **refuses to open**; a malformed sidecar
refuses to open; a sidecar that is not the key the spine was written with
opens the store read-only in effect (`verify` reports `key mismatch` against
the key fingerprint recorded in the genesis event, and every append is
refused) rather than silently re-keying. An in-memory store gets an
ephemeral key. On Windows the sidecar has exactly the database file's
protection (there is no 0600 mode); it protects against a database copied
elsewhere without its directory, not against a writer on this machine.

Because the digests are keyed, `spine verify` means *authentic and complete*
against anyone who has the database file but not the sidecar. The laundering
paths a reviewer found are closed: a manual `user_version` downgrade cannot
re-import tampered claims over an authentic head (the migration refuses), the
runtime refuses to append onto a truncated tail or a missing head, an emptied
spine never verifies, and every tombstone must still cover its redactions (an
un-redaction restored from a backup is reported). The honest bound: a writer
who also holds the key can rewrite the chain undetectably, and restoring an
older full backup (database plus sidecar) is undetectable; the spine is
tamper-evidence for a local single-user store, not a remote notary.
`permission` values seen in practice: `operator:interactive`, `operator`,
`runtime`, `runtime:transcript`, `worker`, `migration`.

## Lineage

`memory_claims.spine_event_id` (unique) names the `claim.imported` or
`claim.created` event that produced the row; a `BEFORE INSERT` trigger aborts a
claim row without a matching event. Claim ids are allocated explicitly from
`memory_claim_sequence`, so an erased id is never reused. Status changes
(`claim.superseded`, `claim.reasserted`, `claim.disputed`, `claim.retracted`)
carry the claim's after-image (`status`, `valid_until`, `confidence`,
`authority`, `source`), which is what makes the projection rebuildable: the
matching path of a claim write mutates confidence, authority, and source in
place, and the spine records the result.

Migration 45 → 46 backfills every existing claim as `claim.imported` after a
`spine.genesis` event and sets the lineage column. A store that is
re-migrated from below 46 drops the stale spine triggers first so the legacy
claim backfills can run, then recreates the spine; that path is only taken
when no authentic keyed head exists, because a real store below 46 never had
a spine and a downgrade over an authentic one is refused (see above).

## Deletion receipts and the right to forget

- `Forget this project fact:` retires a fact (`claim.retracted`); versions stay
  as history.
- `Erase this project fact: {"subject":…,"predicate":…}` deletes every version
  of one project fact: the claim rows, their evidence, clock statistics,
  observations, and claim events, the backing memory rows and their retrieval,
  statistics, embedding, lease, and provenance rows (in foreign-key order,
  with `secure_delete` on and a WAL checkpoint afterwards), appends
  `claim.tombstoned` naming the removed ids, and redacts every earlier spine
  payload about that key. The receipt is fixed:
  `Erased project fact (N versions removed; tombstone #E). M transcript copies
  remain until their conversations are deleted.` M counts messages and
  conversation-goal rows, in any conversation, that contain the erased value.
  The operator's own commands and the receipts that showed the value stay in
  the transcript, which is why the count is reported rather than hidden;
  deleting the conversation removes them and appends `conversation.deleted`.
  Proposal records for the erased key are unlinked, expired, their command
  text blanked, and their salted digests zeroed; the proposal receipts on the
  spine are redacted by the same tombstone. The full-text indexes scrub the
  deleted tokens (`secure-delete`, SQLite 3.43+), and `secure_delete` is on
  for the whole connection so freed pages are zeroed; run `VACUUM` to release
  them to the file system. What erase cannot reach: database backups taken
  earlier, and copies the operator made elsewhere.
- Every governed receipt is on the spine: `proposal.not_stored` for a shown
  proposal (digest of the command only, `outcome=applied`), for a governed-gate
  refusal (`outcome=rejected`), and for a note without a proposal (a corrected
  write claim or readonly mode, `outcome=noop` with a `variant`); and
  `proposal.confirmed`, whose `parent_event_id` points at the shown proposal's
  event.

## Rebuild

`python -m jarvis spine rebuild-claims` replays the spine in id order into a
shadow projection and compares it with the live claim rows on `(scope,
claim_key, subject, predicate, value, value_sha256, status, authority,
confidence, source, valid_from, valid_until, supersedes_id)`, checks that each
live claim still has its backing memory row, and reports every divergence with
its claim id. A divergence detail never carries a value: for `claim_key`,
`subject`, `predicate`, `value`, `value_sha256`, and `source` it is exactly
`<field>: differs`, only the metadata fields (`scope`, `status`, `authority`,
`confidence`, `valid_from`, `valid_until`, `supersedes_id`) show
`live=<x> rebuilt=<y>`, and an edited backing row reads `backing memory
content differs from the spine`; the CLI prints details verbatim, in text
and in `--json`. Without `--apply --yes` it never changes the live tables
(see "Slice 2" below for the apply step).
The M2 exit test in `tests/test_memory_spine_integration.py` runs sixty
randomized creates, global reassertions with differing source and confidence,
retractions, and erasures, then asserts zero divergences and that every erased
key is absent and tombstoned.

## Operator surfaces

```text
python -m jarvis spine verify [--json]
python -m jarvis spine rebuild-claims [--apply [--yes [--plan TOKEN]]] [--json]
python -m jarvis spine rebuild-memories [--json]
python -m jarvis spine tail [--limit N] [--json]
python -m jarvis memory list [--limit N] [--json]
python -m jarvis memory erase <id> [--yes]
```

`verify` exits 1 on any broken link, keyed digest, head record, clock,
redaction whose tombstone is missing, of the wrong kind, of another scope, or
does not name the redacted claim, lineage, tombstoned-id reuse, or missing or
altered trigger, a claim sequence behind the store, a key that does not match
the genesis fingerprint, or a tombstone whose redactions were undone; it also
reports how many best-effort receipts this process dropped under lock (a
per-process counter, not a store property). Since slice 2 the text line also
prints the memory lineage counts (`memory_rows`, `claim_backing_rows`,
`memory_events`; `--json` adds `memory_lineage_ok` and `memory_sequence_ok`),
and a memory row without a creating event, a claim backing row whose lineage
is not its claim's event, a live memory id named by a `memory.deleted` event,
or a memory sequence behind the store also fails verification. Since schema 48
it also prints the graph's edge and entity counts and whether that projection
is equivalent, but the graph never changes the exit code: the spine is
authentic whether or not a derived projection drifted, and drift is a rebuild
matter (`python -m jarvis graph rebuild`). `tail` prints payload keys, never
values. Verification and the dry-run rebuilds are
read-only; the claim rebuild also checks each live claim's backing memory
row against the spine. Exit codes: `0` verified, equivalent, applied, or
nothing to apply; `1` failed, divergent, or refused (including a stale plan
token); `2` `--apply` without `--yes` when something would change, `--yes`
without `--apply`, or `--plan` without `--apply --yes`.

## Slice 2: ordinary memories and lessons (schema 47)

### Memory kinds and digest-only payloads

Every `memories` row now carries `spine_event_id` (unique index
`idx_memories_spine_event`), and a `BEFORE INSERT` trigger,
`memories_require_spine_event`, aborts a row without one. The trigger accepts
either a memory-creating event (`memory.imported`, `memory.created`,
`lesson.created`) whose `subject_kind` is `memory` and whose `subject_id` is
the row, or, for a `kind='claim'` backing row, a claim-creating event; `verify`
cross-checks a claim's backing row against the claim's own event afterwards.
Memory ids are allocated explicitly from `memory_id_sequence` (only after the
existence check, so an idempotent duplicate write allocates nothing), and an
erased or re-indexed id never returns; `verify` asserts that no live memory id
appears in any `memory.deleted` event and that the sequence is ahead of the
store.

Memory payloads never carry content. `memory.imported` / `memory.created` /
`memory.updated` and `lesson.created` carry `{kind, content_digest,
content_length, source, family, outcome_status, reflection_id, origin,
eligible}` (`lesson.created` adds `provenance_sha256`; `memory.imported` may),
`memory.reasserted` carries `{origin, eligible, content_digest}` (`applied`
when the provenance row changed, `noop` otherwise, because a duplicate
`remember_verified` upgrades recall eligibility in place), and
`memory.deleted` carries `{ids, content_digests, reason}` for at most 128 rows
(`MEMORY_DELETED_MAX_IDS`; the vault re-index chunks a larger delete into
several receipts). `content_digest`
is an HMAC-SHA256 under the spine key, so a low-entropy erased value cannot be
confirmed from the digest without the sidecar. "Rebuildable" for memories
therefore means verifiable: lineage, tamper evidence (a digest mismatch is an
out-of-band edit), and deletion receipts; the row, and for vault rows the vault
file, stays the content authority. Recall eligibility is still decided by
`ordinary_memory_provenance`, never by the spine's actor.

Actors: the model's `remember` tool is receipted as `model`, with the gate
that admitted it as `permission` (`<autonomy>:<origin>:explicit_memory_write`,
for example `autonomous:interactive:explicit_memory_write`) and the
conversation id; `remember_verified` from the runtime and lessons are
`runtime`; the vault re-index is `runtime` / `runtime:indexer` from the
Presence and CLI indexer loops and `operator` / `operator:interactive` from
the chat verb; the CLI feedback command writes as `operator` / `operator:cli`.
The agent sets the tool's context for exactly one dispatched call and clears
it in a `finally`, so no later tool inherits it.

### Migration 46 to 47

Migration 47 requires the schema 46 spine and a head that verifies under the
key (a head that does not verify is refused, never discarded). It widens the
events table's closed kind list, adds `memories.spine_event_id` and
`memory_id_sequence`, links each claim backing row to its claim's creating
event by join, imports every other row as `memory.imported` (actor
`system`, permission `migration`, digest-only payload) while counting
orphan `kind='claim'` rows (a backing row whose claim is gone) separately,
and recreates every trigger. It is idempotent and laundering-proof. A
genuine schema 46 store, or a stripped legacy store, imports every
lineage-less row; once the spine records any memory event, a lineage-less
row whose id already has exactly one creating event is re-linked to it only
when the row's keyed content digest equals the latest digest the spine
knows for that id, and everything else refuses to open the store with a
fixed code: `head_unverified` (the head does not verify under the key),
`digest_mismatch` (the content differs from its spine history),
`duplicate_creating_event`, `deleted_id_live` (an id named by a deletion
receipt is live again), and `lineage_missing` (no lineage and no creating
event on a spine that already records memories: a planted row). Those
shapes are a `user_version` downgrade over edited rows. An edit that keeps
its lineage is not a migration matter: `rebuild-memories` reports it as a
digest divergence.
`Memory._migrate` drops every spine trigger below 47 so the legacy backfills
can run, and a legacy store below the spine gets migrations 46 and 47 in one
transaction; the below-46 refusal over an authentic keyed head is unchanged.

### Memory rebuild

`python -m jarvis spine rebuild-memories` replays the `memory.*` and
`lesson.*` events into a shadow projection keyed by memory id and compares
`(kind, content_digest, content_length, source, family, outcome_status,
reflection_id, origin, eligible)` with the live rows, plus the presence and
digest of the `lesson_provenance` row for lessons. Divergence kinds: `verify`,
`payload`, `order`, `lineage`, `missing_in_rebuild`, `missing_in_live`,
`field`, `provenance`; `detail` names fields and digests, never content. Claim
backing rows are checked by the claim rebuild, not here. The memory rebuild
is a dry run only: there is no apply step for memories, because the spine
holds digests, not content.

### `rebuild-claims --apply`

After the dry run, `--apply --yes` reconciles the live claim projection **in
place** under the write lock, never by table swap. It refuses, rolling back
with nothing changed and a fixed reason code, when the chain, head, key,
triggers, redactions, or sequences fail verification (`verify_failed`: the
spine, not the projection, is wrong), when the history has `payload`,
`order`, or `redaction` divergences (`history_inconsistent`), when the store
changed since the dry run (`stale_plan`), on an integrity error while writing
(`write_conflict`), or when the dry run re-run inside the transaction still
diverges (`residual_divergence`); `spine_unavailable` and
`transaction_already_open` are the wrapper's own refusals
(`not_in_transaction` is raised by the module itself when apply is called
outside a write transaction). Lineage problems are what apply fixes, so
they never refuse, and the dropped-receipt counter never blocks.
Ordering: deletions first (rows without spine history and rows whose
immutable scope diverges, in the erase order: proposal unlink, clock
statistics, observations, evidence, claim events, `supersedes_id` nulling,
the claim row, the backing row's dependents, the memory row), then field
updates from the shadow (claim columns, `memories.source`, and the backing
content), then recreations with the backing row's `created_at` equal to the
claim's, the creating event as lineage, and the key's status events replayed
into `memory_claim_events`; evidence rows are reported as lost
(`lost_evidence_claim_ids`), never recreated. `projection.rebuilt`
(`{rows_before, rows_after, divergences_fixed, removed_ids,
removed_memory_ids, recreated_ids, updated_ids, lost_evidence_claim_ids}`) is
appended last, and the recall cache is cleared. The CLI prints the counts and
the id lists, never a value; without `--yes` it prints the plan (field
updates, recreations, removals, and the spine-side problems that would make
apply refuse) together with its **plan token** (12 hex characters the store
derives from the head event id and the sorted (claim id, kind) divergences)
and exits `2`. `--apply --yes --plan TOKEN` re-runs the dry run and refuses
with `stale_plan` (exit `1`, nothing changed) when the fresh token differs,
so an operator can only reconcile the divergences they saw; `--apply --yes`
without a token prints the fresh plan and its token and applies that plan.
In both cases the dry-run report is handed to the store, which re-checks it
inside the write transaction. When the projection is already equivalent the
command prints "Nothing to apply" and exits `0`; every other refusal exits
`1` with its reason code.

### Retracted history

`Forget this project fact:` retires a fact but keeps its versions, and a
past-tense question in any later conversation still answers from them through
`Memory.subject_claim_history` (see
[Governed project memory](GOVERNED_PROJECT_MEMORY.md)); only `Erase` removes a
value from temporal answers.

### Rows still without lineage after slice 2 (declared)

`memory_claim_events` (nullable, legacy `NULL`), `memory_claim_evidence` (not
rebuildable, reported as lost by apply), claim observations, clock statistics,
retrievals and statistics (telemetry, excluded), `memory_embeddings` (derived,
deleted with the row, rebuilt by re-index), `lesson_provenance` (bound to rows
not on the spine; presence and digest verified only), and `training_examples`
(out of scope).

## Erasing one ordinary memory (schema 48)

`memories.id` has been explicit and never reused since schema 47, so it is an
operator-facing identity. `Erase memory #<id>` as a standalone turn, or
`python -m jarvis memory erase <id> --yes`, deletes the row, every dependent
row that carries its `memory_id`, and its FTS entry under `secure_delete`, and
appends one `memory.deleted` event carrying the keyed digest and the number of
transcript copies that remain. No content is echoed on any path.

The dependent-table list is **derived from the live schema**, not typed out: the
implementation reads every table with a `memory_id` column from
`PRAGMA table_info` (minus `memory_claims`, which the claim-backing refusal
already excludes), and a test asserts the derived set equals the documented ten,
so a table added later fails that test instead of silently keeping a row that
points at an erased memory.

Three cases refuse and change nothing, each with a fixed reason: no such row;
a row that backs a project fact, which points the operator at
`Erase this project fact:` because erasing it alone would leave the claim
projection inconsistent; and a row that mirrors a vault note, which the indexer
would re-create on its next pass.

The verb is parsed with the exact-parser discipline of the three claim verbs:
`Delete memory #<id>`, a leading `please`, and a trailing `.` or `!` are
accepted, the id is 1 to 18 digits with no leading zero, nothing else may share
the turn, and a near-command such as `forget memory 12` owns the turn and fails
closed quoting `Erase memory #<id>` rather than reaching a model. As with the
three claim verbs, an invisible character *inside* a word (`Erase<ZWSP>memory
#12`) leaves a string no prefix matches, so the turn becomes ordinary text and
nothing is stored or deleted.

`python -m jarvis memory list` and `/memory` in chat show the ids, with a
120-character preview passed through the widened private-identifier screen;
a row that trips it previews as `[PRIVATE]`.

## The learning ladder (spine schema 48)

Seven new kinds record every transition of a governed skill promotion and
every sealed calibration epoch, all **digest-only**, with two new subject
kinds `ladder` and `calibration`:

| kind | actor | what it records |
|---|---|---|
| `ladder.calibration_sealed` | `runtime` / `operator` | one epoch's counts, its Brier and calibration error, the ladder's own refusal counters, `unverified_at_seal`, and the keyed coverage digest |
| `ladder.candidate` | `runtime` | the moment a proof was met with the gate open and the ledger not regressed |
| `ladder.staged` | `runtime` | a document written to the staging root |
| `ladder.grandfathered` | `runtime` | a pre-M4 live document adopted at stage `unapproved_legacy` |
| `ladder.approved` | **`operator`** | a document made live |
| `ladder.rolled_back` | **`operator`** | prior bytes restored, or the document removed |
| `ladder.withdrawn` | `runtime` | an artefact pulled because its proof or its ledger stopped holding |

**Two of the sealed epoch's four refusal counters are not spine-derived.**
`withdrawals` and `screened_components` are counted from `ladder.*` events and
inherit the chain's guarantees. `refused_stagings` and `refused_approvals` are
read from `activity_log`, category `ladder`, which is neither append-only nor
chained — a refusal that never reached the log, or a log an operator trimmed,
undercounts silently. Both pairs are frozen into the epoch and both are
**reported, never gated**; only `unverified_at_seal` feeds the monotonicity
predicate. Do not read a zero in the second pair as the chain's word.

Payloads carry integers, family names, derived skill names, timestamps,
digests, booleans and closed-set reason strings — **never** document text,
lesson text, or operator prose. `ladder.approved` and `ladder.rolled_back`
accept no actor but `operator`, structurally, so a verifier can assert it over
the whole ladder chain.

**No token material of any kind appears in any payload.** `ladder.staged`
carries the boolean `token_required` and nothing else about the confirmation
code; the code lives only on the promotion row, and only three operator
surfaces read it back. Neither `proof_sha256` nor `staged_sha256` is published
beside a promotion id anywhere a browser can reach, because publishing digests
next to an id is how an earlier draft's token became derivable.

The two record tables are **not projections**. `rebuild-claims` leaves both
untouched, `_REBUILT_PROJECTIONS` does not name them, and a claim erase does
not cascade into them — a promotion whose proof rows are later erased is
reported `proof_stale` and withdrawn, which is the correct fail-closed answer.
`spine verify` gains four counters (`ledger_rows`, `ledger_events`,
`ladder_rows`, `ladder_events`) and distinguishes a ladder lineage fault from
a chain fault.

**`SPINE_SCHEMA_VERSION` is recorded, never enforced.** It appears only in the
genesis payload and nothing checks it on open, so the 47 → 48 bump documents
the kind-set change and is not a guard. A store whose genesis says 47 will
accept `ladder.*` events silently. The guards are `SPINE_KINDS` membership and
the payload validator. See [The learning ladder](LEARNING_LADDER.md).

## Boundaries

Long-horizon rows keep their own hash-chained, receipted ledger; the
volatility clock's hazard fit is not refit after an erase; the memory
projection is never applied (digests, not content); nothing here changes recall
ranking or any sealed benchmark. Older trees refuse to open a database at 48.
