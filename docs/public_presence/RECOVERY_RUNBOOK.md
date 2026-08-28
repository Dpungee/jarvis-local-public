# Public Presence recovery and backup/restore runbook v0.1

## Purpose

Use this runbook for a suspected public-data leak, unauthorized or duplicate
publication, compromised/revoked account, policy or identity drift, corrupted
public database, stuck listener, failed deployment, or release rollback.

The foundation release has no live listener, credentials, accounts, or outbound
methods. Any evidence of external activity while it is the active release is an
incident: stop and investigate rather than assuming it is expected.

## Roles and safety rules

- The operator owns stop, credential revocation, restore, and re-enable decisions.
- JARVIS may collect bounded diagnostics and draft a report, but may not clear an
  incident, change safety controls, restore over a live database, or re-enable
  Public Presence.
- Preserve evidence before cleanup. Do not paste tokens, private records, raw
  database content, or public users' personal data into tickets or model prompts.
- Restore only to a new temporary path. Never overwrite a live private or public
  database during validation.
- A restored public database never grants permission to reconnect or publish.

## Immediate containment

1. Activate the **independent Public Presence emergency stop** in the local
   operator control plane. Record the UTC time and observed state.
2. Activate **social pause**. It is independent of the ordinary worker pause.
3. Confirm the public listener/process is stopped and cannot auto-restart. Do not
   stop or alter Private JARVIS unless the incident crosses that boundary.
4. If any platform account exists in a later phase, revoke its token/session from
   the platform's official account security page using a trusted operator device.
   Do not ask JARVIS to retrieve or display the token.
5. If publication may have occurred, record platform, account ID, item ID/URL,
   visible timestamp, and a screenshot. Do not delete or edit the item until the
   incident owner decides whether evidence preservation or harm containment has
   priority. Any corrective external action requires a fresh exact approval.
6. Preserve public process logs, audit receipts, database/WAL/SHM files, deployed
   revision, configuration digest, public-tool manifest, and platform response
   identifiers. Store them in a restricted incident directory.
7. Start no fallback connector, browser session, alternate account, or retry.

Target: stop propagation is independently measured at under two seconds. Failure
to confirm stop is a severity-one incident requiring OS/service-level shutdown
and credential revocation.

## Triage questions

- Was Public Presence enabled, and by which local operator event?
- Which exact release revision, public schema, policy digest, and adapter ran?
- Did public content cross the private bridge, and if so which closed object ID?
- Was an approval present? Did its content, destination, account, media, expiry,
  and digest match the attempted action?
- Does an idempotency record exist, and is the platform outcome known, failed, or
  uncertain?
- Did any secret scanner, policy check, pause, stop, rate limit, or account
  identity check fail or get bypassed?
- Could private data or credentials have reached prompts, logs, memory, URLs,
  drafts, platform payloads, or error output?
- Is the event isolated, repeated, concurrent, or associated with restart/retry?

## Database backup procedure

Back up a live SQLite database through SQLite's online backup API, not by copying
only the main file while WAL mode may be active. Use an explicit source and a new
timestamped destination inside the approved backup directory. Never reuse or
overwrite a backup filename.

Before a future public release, the implemented backup control must record:

- source path identifier (not exposed publicly), schema version, and file digest;
- backup path, UTC time, digest, and creating runtime revision;
- `PRAGMA quick_check` or `integrity_check` result;
- `PRAGMA foreign_key_check` result; and
- an append-only backup receipt.

For the existing private runtime, run its supported recovery evaluation from the
repository root:

```powershell
python -m jarvis recovery test
```

This is evidence for Private JARVIS only. The future public database must have an
independent backup receipt and restore proof. Never place either database or a
backup into the public workspace, source control, a model prompt, or a platform
upload.

## Isolated restore validation

1. Keep Public Presence stopped, paused, and disabled.
2. Create a new restricted temporary restore directory outside both live
   workspaces. Resolve and record the exact path.
3. Copy or restore the selected backup into a new filename in that directory.
   Never target `data/jarvis.db`, `data/public_presence.db`, or any live path.
4. Open only the temporary restored database with the matching known-good
   runtime revision. Refuse a schema newer than the runtime supports.
5. Verify `PRAGMA integrity_check` returns exactly `ok` and
   `PRAGMA foreign_key_check` returns no rows.
6. Verify public identity version, pause/stop state, audit-ledger continuity,
   approval one-shot state, idempotency records, and receipt referential
   integrity. Do not contact a provider during restore tests.
7. Exercise restart recovery against simulated adapters only. Confirm no queued,
   successful, uncertain, expired, or already-approved action can publish.
8. Compare the restored evidence counts/digests with the backup receipt. Record
   every mismatch; do not repair data to make the test pass.
9. Close all database handles. Retain the temporary restore until review is
   complete, then remove it through an approved recoverable cleanup procedure.

Restoration passes only when a second reviewer can reproduce the checks and no
external network or account access occurred.

## Release rollback

1. Keep the public kill switch asserted and platform credentials revoked.
2. Roll back code/configuration to the last signed, approved revision; never use
   a dirty working tree as a recovery release.
3. Start with Public Presence disabled, mode offline, listener stopped, and all
   adapters disconnected.
4. Run the complete regression suite, hostile-input suite, private-data canary
   suite, approval/idempotency suite, and stop-propagation test.
5. Restore the public database only if the isolated restore passed and incident
   review determines its records are trustworthy. Otherwise start a quarantined
   new public database and retain the old one as evidence.
6. Reconnect no account until the operator has reviewed the incident, rotated
   credentials through the platform, verified exact account identity/scopes,
   and signed a new exit-gate record.
7. Resume in the lowest previously certified tier. A prior higher tier is not
   automatically restored.

## Incident record

Retain a dated, append-only record with:

- incident ID, detected/contained UTC times, detector, and severity;
- revision/config/policy/public-tool manifest digests;
- affected platform/account/item identifiers (no credentials);
- inbound event IDs, bridge object IDs, approval IDs, idempotency keys, and
  publication receipts;
- verified private-data/credential exposure assessment;
- containment, revocation, backup, restore, and rollback evidence;
- root cause, violated invariant, corrective tests, residual uncertainty;
- operator decision and independent reviewer sign-off.

Do not re-enable Public Presence while any affected outcome is uncertain or any
exit gate lacks current evidence.
