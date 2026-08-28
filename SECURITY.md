# Security policy

Jarvis can interact with models, local files, processes, browsers, and external
services. Security reports are therefore treated as product defects, not ordinary
feature requests.

## Reporting a vulnerability

Do not include vulnerability details, credentials, personal data, or working exploits
in a public issue.

Use GitHub's [private vulnerability report](https://github.com/Dpungee/jarvis-local-public/security/advisories/new).
If private vulnerability reporting is temporarily unavailable, open a public issue
titled **Security contact requested** without technical details so the maintainer can
establish a private channel.

Include the affected version or commit, operating system, configuration boundary,
reproduction conditions, observed impact, and whether the behavior crossed an approval
or containment boundary. Redact every token, credential, personal path, and private
record.

## Supported versions

JARVIS Local is currently an alpha public preview. Security fixes target the latest
`main` branch until tagged stable releases are published.

## Security boundaries

- `.env`, `data/`, `workspace/`, databases, logs, credentials, and local provider state
  must never be committed.
- `trusted-host` execution runs with the current Windows account and is not an OS
  sandbox.
- Computer access, external access, proactive work, remote Presence access, and
  self-inspection are opt-in.
- Approval, redaction, policy, verification, identity, and repair gates are protected
  from self-repair.
- Tests that weaken or bypass these boundaries will not be accepted.

See the README for the complete runtime threat model and approval behavior.
