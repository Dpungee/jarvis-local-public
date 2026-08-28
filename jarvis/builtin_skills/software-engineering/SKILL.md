---
name: software-engineering
description: Implement, debug, refactor, review, and verify software changes. Use for repository, application, API, test, build, or code-quality work.
---

# Software engineering

## Workflow

1. Inspect the project structure, relevant source, configuration, and existing tests before editing.
2. State the concrete acceptance criteria and choose the smallest coherent change that meets them.
3. Preserve unrelated work and established project conventions. Do not replace broad files to avoid understanding them.
4. Make transactional edits with exact preconditions when supported.
5. Reread every changed artifact and inspect the resulting diff.
6. Run the narrowest meaningful verification, then the broader suite in proportion to risk.
7. Treat a zero-test run, truncated output, or an unverified claim as incomplete.

## Debugging

- Reproduce the failure before changing code when practical.
- Trace from observed evidence to root cause; distinguish the trigger, defect, and downstream symptom.
- Add a regression check that would have failed before the repair.
- Re-run the original reproducer after the fix.

## Verification

- Report exact files changed and the command or deterministic check that passed.
- Report remaining uncertainty or blocked checks explicitly.
- Never claim a build, test, launch, deploy, or external effect that a successful tool result did not establish.
