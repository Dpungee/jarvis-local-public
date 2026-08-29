# Public-release checklist

This checklist records verified evidence for the published `v0.6.2` public-preview
release and the operator or repository actions that remain pending. Do not infer
completion of an unchecked item from a clean source scan.

## Privacy and credentials

- [x] Remove or anonymize raw stress reports, conversations, screenshots, generated
      documents, postal codes, usernames, local paths, and account identifiers.
- [x] Confirm `.env`, databases, WAL files, logs, vault content, workspaces, backups,
      OAuth state, browser state, and credentials are not tracked.
- [x] Scan the full release Git history with a dedicated secret scanner, not only the current
      working tree.
- [x] Publish from a clean root commit because private material appeared in development
      and earlier public history.
- [ ] Rotate every credential that was ever pasted into a prompt, terminal, report, or
      tracked file. This is an operator attestation; the published source and artifacts
      contain no detected credential.
- [x] Ensure every reachable release commit and tag uses a GitHub no-reply address when
      the maintainer email should remain private. Public `main` and the published
      `v0.6.0`, `v0.6.1`, and `v0.6.2` tags resolve only through sanitized no-reply
      history.

## Product trust

- [x] Finish active changes and run the full deterministic suite on the published release.
- [x] Confirm the GitHub Actions matrix and privacy scan pass from a clean checkout of
      the exact `v0.6.2` release on Python 3.11, 3.12, and 3.13 for Windows.
- [x] Resolve or justify every open finding from Python, JavaScript/TypeScript, and
      GitHub Actions CodeQL analysis of the exact `v0.6.2` release. The published
      release completed the required CodeQL checks without an open finding.
- [ ] Verify setup and first launch on a clean Windows user account.
- [ ] Capture one sanitized Presence screenshot or short demo with synthetic content.
- [x] Publish measured capabilities and current limitations without aspirational claims.
- [x] Enable GitHub private vulnerability reporting and verify the repository API reports
      `private_vulnerability_reporting.enabled=true`.

## Repository presentation

- [x] Include the owner-approved Apache-2.0 license and NOTICE file.
- [x] Apply the prepared public description, summary, and topics.
- [ ] Set the homepage/documentation link on GitHub.
- [x] Preserve the historical `v0.6.0` and `v0.6.1` tagged prereleases, and publish the
      current `v0.6.2` tagged prerelease with release notes, distributions, source
      archives, and published SHA-256 checksums.
- [x] Retire older public preview refs and keep the development repository and archive
      private; anonymous checks return no older public branch, tag, release, or commit.
- [x] Keep raw internal queues and generated evidence excluded; publish only curated,
      anonymized summaries under `docs/`.
- [x] Review every tracked entry and exclude generated binary artifacts.

## Suggested GitHub metadata

**Name:** JARVIS Local

**Package:** `jarvis-local`

**Version:** 0.6.2 Public Preview (alpha)

**Description:** Windows-first, local-first personal AI agent with automatic model
routing, provenance-aware memory, bounded tools, and approval-gated automation.

**Summary:** JARVIS Local is an alpha personal-agent runtime for supervised Windows
workflows. It supports local and optional cloud or subscription model providers,
source-grounded research, purpose-bound specialists, provenance-aware memory, and
bounded
local or external tools. It is not an OS sandbox, unrestricted administrator,
professional security product, or conscious system.

**Topics:** `ai-agent`, `local-ai`, `ollama`, `python`, `personal-assistant`,
`multi-agent`, `memory`, `tool-use`, `windows`, `openai`, `anthropic`

The published source tree, distributions, and clean-root content history passed the
privacy and dedicated secret scans. Future releases must be pushed from a disposable
public-only clone with exact branch and tag refspecs; never use `--all`, `--tags`, or
`--mirror` from a clone that also contains private development refs.

## Remaining post-release gates

Credential rotation attestation, clean-Windows-user first launch, sanitized demo capture,
and a GitHub homepage/documentation link remain pending. Do not mark any of them complete
until their direct evidence exists.
