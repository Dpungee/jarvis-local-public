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

## Credential exposure and rotation

Treat a credential as exposed if it was pasted into a chat, prompt, recorded terminal
command, report, issue, screenshot, log, tracked file, or Git history. Removing the text,
rewriting Git history, or deleting a local file does not revoke a copied credential.

1. Revoke the exposed API key, token, OAuth grant, bot token, or session at the service
   that issued it. Do not ask JARVIS to retrieve or display the old value.
2. Review the provider's recent activity and authorized applications for unexpected use.
3. Create a replacement with the narrowest practical scope. For OAuth services such as
   Google Drive, remove the local token and complete a fresh authorization using the
   intended access mode. For GitHub CLI, revoke the affected authorization and sign in
   again through the official `gh` flow.
4. Store the replacement only in the documented Windows user environment, official CLI
   credential store, or dedicated local credential directory. An ignored `.env` file is
   still plaintext and must never be committed, synced publicly, or attached to a report.
5. Restart affected JARVIS processes, verify the old credential no longer works, and
   record only the rotation date and provider—not the secret—in the incident record.

OpenAI, Anthropic, Ollama, Telegram, Home Assistant, GitHub, and Google credentials are
independent; rotate every service whose value or authorization may have been exposed.
Credential rotation is an operator action and cannot be inferred from a clean source
scan.

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
