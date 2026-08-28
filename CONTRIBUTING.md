# Contributing

Thanks for helping improve Jarvis. Changes should make demonstrated capability,
reliability, usability, or safety better without turning model text into authority.

## Before opening a change

1. Search existing issues and pull requests.
2. Keep the change focused and preserve unrelated work.
3. Add or update deterministic tests for behavioral changes.
4. Run `python -m unittest discover -s tests`.
5. Remove credentials, private paths, generated databases, logs, and personal data.

## Pull requests

Explain the user-visible outcome, the failure or limitation being addressed, the exact
verification performed, and any remaining limitations. Screenshots are welcome for UI
changes when they contain no private conversation or machine data.

Changes involving approvals, redaction, policy, verification, memory provenance,
external accounts, desktop control, or self-repair require explicit adversarial tests.
Do not weaken a gate merely to make a test or model response pass.

## Design principles

- Prefer general behavior over growing lists of magic phrases.
- Prefer deterministic enforcement over prompt-only promises.
- Preserve provenance and distinguish observation from inference.
- Fail closed at authorization boundaries and recover gracefully elsewhere.
- Report measured results rather than aspirational capability claims.

## Development setup

```powershell
python -m pip install -e ".[documents]"
python -m unittest discover -s tests
python -m jarvis doctor
```

Optional provider, document, Drive, and training dependencies are documented in
`pyproject.toml` and the README.
