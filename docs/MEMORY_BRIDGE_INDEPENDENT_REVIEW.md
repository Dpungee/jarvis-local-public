# Independent bridge review: activation blocked

Date: 2026-09-16. Branch: `codex/upgrade-readiness`.
Base: `93119c421cdea4fffafcd3d13caef3fccfe15a29`.
Scope: Claude's schema-51/52 bridge, worker integration and operator surfaces.
Review only; no implementation repair, live migration, activation or dispatch.

## Independently executed verification

Command:

```powershell
.venv/Scripts/python.exe -m unittest tests.test_memory_bridge tests.test_memory_bridge_worker tests.test_memory_bridge_recovery tests.test_multi_agent_runtime tests.test_cloud_release
```

117 tests passed in 5.993 seconds, zero skips. The full suite was not independently
rerun during this review; Claude's 4,347-run result remains attributed evidence.
Four additional disposable-store diagnostic scenarios reproduced the issues below.
These scenarios demonstrate defects, not successful safety acceptance tests.

## Findings requiring correction before activation

### P1: Real replay-reader failures do not persist a halt

`ingest_once` invokes the reader outside the `import_batch` failure-to-halt path.
The real runtime reader raises `RuntimeConflictError` or `RuntimeStoreError` for
cursor/history and content failures. `_run_bridge_pass` only handles bridge
exceptions, and opens the runtime store before its try block.

Reproduction: import one valid synthetic event into disposable memory, then ingest
from a separate empty real runtime store using `replay_events`. Result:
`RuntimeConflictError`, with memory cursor status still `active`. Subsequent passes
can read again without an explicit operator resume. This contradicts durable halt
semantics; rejection itself remains fail-closed, but the operational state is false.

Required correction: translate structural reader failures at the trusted adapter
boundary to sanitized durable halt state under generation/cursor guards. Distinguish
operational I/O failures; do not claim a durable halt when persistence fails. Test
actual legacy/gapped/corrupt/mismatched stores through the worker path.

### P1: Lease takeover does not fence the previous worker's commits

`ingest_once` does not validate lease token or expiry inside the batch transaction
and does not renew its lease. Token validation occurs only on release; its return
value is ignored. Cursor CAS prevents competing updates to the same cursor but
does not prevent an expired holder committing before the new holder advances it.

Reproduction: during the reader callback, acquire the lease as `replacement` using
a timestamp 121 seconds later. The expired worker still commits one event, leaves
the replacement lease held, and reports `lease: released`.

Required correction: validate ownership/token/expiry transactionally before every
commit, renew safely for long passes, and report lease loss truthfully. Add a
deterministically synchronized takeover test with unchanged cursor, not only a
test that two processes eventually produce one history.

### P1: Failed generation validation replaces the active generation

`open_generation` marks the previous generation superseded before replay validation.
`rebuild_generation` switches/resets the consumer cursor before `import_batch`.
The latter returns a halted result rather than raising, so the transaction commits
the unsuccessful cutover.

Reproduction: generation 1 contains valid sequence 1; open a generation with a
history beginning at sequence 2. Result is halted, but active generation becomes
2 and cursor becomes 0. The previous valid generation no longer serves the consumer.

Required correction: validate a candidate separately and atomically cut over only
after success. Preserve active generation/cursor and audit history on failed rebuild.
Also review deletion of batch/quarantine history by `rebuild_generation` against
the contract requiring durable audit and quarantine decisions.

### P1: Unmapped project text is copied into a halt record

`resolve_project` embeds the raw runtime project string in its exception.
`import_batch` passes that exception text to `_write_halt`, which truncates but
does not sanitize it. Runtime project strings are explicitly private/free-form
under the contract, not safe opaque identifiers.

Reproduction: import an event whose project is `SYNTHETIC_PRIVATE_PROJECT` without
a mapping. That exact string is present in persisted `halt_reason`.
This is a local diagnostic-retention defect; no external leak was demonstrated.

Required correction: closed diagnostic codes plus validated opaque identifiers
only. Adversarial tests should inspect all halt/audit/status outputs for private
project strings and unknown event-type text, not just message bodies.

## Accepted direction and remaining limits

Separate projection tables, no new spine kinds, record-project validation and
invitee-based membership attribution are reasonable choices for this narrow slice.
The focused tests confirm substantial integration/recovery coverage, not readiness
for live activation. Default disabled configuration remains present. No agent-facing
projection retrieval is exposed by the reviewed integration.

Further work remains: measured ingest/lock budgets, full security review of future
retrieval, historical/current authorization, and cross-store retention/erasure.
Deleting a projection does not erase append-only runtime source content.
The runtime reader currently opens a write-capable store constructor, including
migration/WAL setup; assess a genuinely read-only replay adapter before deployment.

## Handoff

Only this review and the current status milestone were edited by this review.
Claude's code, fixtures and tests were preserved. Nothing committed or published.
Next step: repair the four reproduced defects and add regression tests, then rerun
focused and full suites before reconsidering activation. Any assignment to Claude
still requires the operator's post-readback approval; none was sent by this review.
