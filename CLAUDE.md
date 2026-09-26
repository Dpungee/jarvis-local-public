# Claude Code project guidance

This repository is the canonical public source for JARVIS Local. Follow
`AGENTS.md` for repository-wide engineering, verification, coordination, and
public-release rules.

## Required startup context

Before planning or editing JARVIS work, read these files in order:

1. `AGENTS.md`
2. `PROJECT_STATUS.md`
3. `docs/JARVIS_MASTER_ROADMAP.md`
4. The design, threat-model, and runbook documents relevant to the requested
   phase or subsystem

The operator may call `docs/JARVIS_MASTER_ROADMAP.md` the **16-phase plan**.
Treat that phrase as referring to this roadmap. The dated roadmap snapshot
numbers its main phases from 0 through 12 and includes a parallel Holographic
Spatial Workbench track; do not invent, remove, or renumber phases to reconcile
the informal name.

## Source-of-truth rules

- The roadmap is authoritative for product direction, sequencing, phase scope,
  and exit gates.
- `PROJECT_STATUS.md` is authoritative for what is currently complete, active,
  paused, blocked, or uncommitted.
- Git state, test output, release checks, and runtime evidence are authoritative
  for claims about implementation or completion.
- When the dated roadmap's status text conflicts with newer verified evidence,
  preserve the roadmap's intended scope and report the current status from
  `PROJECT_STATUS.md`.
- Never mark a phase complete merely because files exist. Require its stated
  exit gate and record the evidence.
- Preserve the deterministic approval, policy, redaction, verification,
  isolation, pause, emergency-stop, and public-release boundaries described by
  the repository.

## Coordination

Inspect the branch, working tree, recent commits, and both status documents
before changing anything. Preserve unrelated work. For substantial work, use a
dedicated branch or worktree and record the milestone and next verified step in
`PROJECT_STATUS.md`.
