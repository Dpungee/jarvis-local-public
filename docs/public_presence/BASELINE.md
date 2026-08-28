# Public Presence release-baseline template

This template records reproducible, sanitized evidence for a Public Presence
foundation release. Create a new dated record for each candidate; never overwrite an
approved record.

Do not record hostnames, usernames, IP addresses, private paths, credentials, account
state, conversation content, raw database records, or screen contents. Public Presence
must remain disabled, externally disconnected, and fail closed for this release.

## Repository

- Capture time (UTC):
- Commit SHA:
- Branch:
- `git status --porcelain` result (must be empty):
- Release identifier:
- Reviewer:
- Review time (UTC):

## Runtime and schemas

- Package version:
- Python version:
- OS family (no device identifiers):
- Private schema version:
- Public schema version:
- Migration-set digest:
- Redacted runtime-configuration digest:
- Identity-policy bundle digest:
- Private tool-manifest count and digest:
- Public tool-manifest count and digest:
- Confirm the public registry contains no private, shell, filesystem, browser,
  credential, account, payment, trading, deployment, or publishing tool:

## Safe configuration

- Public Presence enabled value (must be false):
- Public mode (must be offline):
- Public listener state (must be stopped):
- Connected platform accounts (must be none):
- Publishing methods (must be none):
- Social-pause state:
- Independent emergency-stop state:
- Private and public database paths verified distinct:
- Private and public workspace paths verified distinct:

## Test evidence

- Exact test command:
- Started/finished (UTC):
- Passed/failed/skipped:
- Output digest and retained report path:
- Environment-only skips and justification:
- Hostile public-input cases passed:
- Seeded private-data leak cases passed:
- Approval replay/substitution cases passed:
- Duplicate-publication crash cases passed:
- Pause/stop propagation measurement:
- Independent reviewer and result:

## Backup and isolated restore evidence

- Source database digest before backup:
- Backup artifact digest (do not record a private absolute path):
- Backup creation time (UTC):
- Restore target (must be temporary and not live):
- `PRAGMA integrity_check` result:
- `PRAGMA foreign_key_check` result:
- Restored schema version:
- Required state/restart assertions:
- Temporary restore disposition:
- Operator/reviewer signatures:

## Decision

- [ ] Working tree and revision are reproducible.
- [ ] Full suite is green or every skip is documented and accepted.
- [ ] Backup and isolated restore were demonstrated.
- [ ] Threat model and prohibited actions were reviewed.
- [ ] Public Presence remains disabled and externally disconnected.
- [ ] Foundation release accepted.

**Decision:** BLOCKED until every checked item has retained evidence.
