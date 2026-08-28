---
name: computer-use-operator
description: Operate supported Windows desktop applications through bounded, visible, verified actions. Use for application launch, GUI workflows, and media editing.
---

# Computer-use operator

## Workflow

1. Identify the exact application, input artifact, requested output, and whether overwrite is allowed.
2. Prefer a purpose-built adapter or documented application automation interface over screen coordinates.
3. Snapshot or hash the input before mutation and preserve the original unless the operator explicitly requests replacement.
4. Perform one bounded state transition at a time and observe the resulting UI or artifact.
5. Stop when dialogs, targets, or state differ from the expected workflow; do not click blindly.
6. Verify the output by reopening, hashing, or inspecting it independently.

## Limits

- Never enter, reveal, or scrape credentials from a password field or credential store.
- Do not launch shells, installers, system-management tools, or arbitrary application arguments through a generic GUI path.
- Require normal approval for consequential desktop and filesystem effects.

## Verification

- Report the exact input, application, output, and verification result.
- A launched app is not proof that the requested edit completed.
