# Public-release checklist

Do not publish a tagged release until every required item below is complete. Items that
depend on the new commit being present on GitHub remain intentionally pending until the
operator authorizes the first push.

## Privacy and credentials

- [x] Remove or anonymize raw stress reports, conversations, screenshots, generated
      documents, postal codes, usernames, local paths, and account identifiers.
- [x] Confirm `.env`, databases, WAL files, logs, vault content, workspaces, backups,
      OAuth state, browser state, and credentials are not tracked.
- [x] Scan the full candidate Git history with a dedicated secret scanner, not only the current
      working tree.
- [x] Publish from a clean root commit because private material appeared in development
      and earlier public history.
- [ ] Rotate every credential that was ever pasted into a prompt, terminal, report, or
      tracked file. This is an operator attestation; the candidate contains no detected
      credential.
- [x] Use a GitHub no-reply address for commits if the maintainer email should remain
      private.

## Product trust

- [x] Finish active changes and run the full deterministic suite on the release candidate.
- [ ] Confirm the GitHub Actions matrix passes from a clean checkout.
- [ ] Verify setup and first launch on a clean Windows user account.
- [ ] Capture one sanitized Presence screenshot or short demo with synthetic content.
- [x] Publish measured capabilities and current limitations without aspirational claims.
- [x] Enable GitHub private vulnerability reporting.

## Repository presentation

- [x] Include the owner-approved Apache-2.0 license and NOTICE file.
- [x] Prepare a concise public description, summary, and topics.
- [ ] Apply the prepared metadata and set the homepage/documentation link on GitHub.
- [ ] Create a tagged release with release notes and a reproducible commit.
- [x] Keep raw internal queues and generated evidence excluded; publish only curated,
      anonymized summaries under `docs/`.
- [x] Review every tracked entry and exclude generated binary artifacts.

## Suggested GitHub metadata

**Name:** JARVIS Local

**Package:** `jarvis-local`

**Version:** 0.6.0 Public Preview (alpha)

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

The candidate's local clean-root history scan and privacy cleanup are complete. Before
publication, replace or retire every older public branch, tag, release asset, or other
reachable ref that does not meet the current privacy standard.

## Intentionally pending after push authorization

The local candidate is ready for its final exact-commit checks. GitHub Actions, the
clean-user installation smoke test, sanitized demo capture, GitHub homepage update,
retirement of older preview refs that do not meet the current standard, and the
`v0.6.0` tag remain release gates.
Do not mark them complete or create a release until their evidence exists.
