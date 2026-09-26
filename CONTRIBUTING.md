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

## Dependency locks

CI installs Python packages only from `requirements/*.txt`. Each file pins one exact
release per package with the SHA-256 digest of every file PyPI publishes for it, so
`pip install --require-hashes` resolves to identical bytes on every supported Python
version and platform. `pyproject.toml` still carries the user-facing version ranges.

Regenerate the locks after changing `pyproject.toml` or when a dependency update is
wanted, from a fresh virtual environment:

```powershell
python -m venv .lock-venv
.lock-venv\Scripts\python -m pip install ".[documents]"
.lock-venv\Scripts\python -m pip freeze --exclude-editable | Out-File -Encoding utf8 pins.txt
python scripts/lock_dependencies.py --pins pins.txt --require-python 3.11 --output requirements/runtime-documents.txt
python scripts/lock_dependencies.py --latest pip --latest setuptools --require-python 3.11 --output requirements/build-backend.txt
python scripts/lock_dependencies.py --check requirements/runtime-documents.txt
```

Always pass `--require-python 3.11` (the oldest interpreter in `pyproject.toml`): a
closure resolved on a newer Python can otherwise pin a release that publishes no wheel
for the oldest CI leg, and the generator refuses such a pin instead of writing it. If
the freeze came from a newer interpreter, lower the offending pin by hand and rerun.
Dependabot can bump one pinned release and recompute its hashes, but it never adds a
new transitive dependency or refreshes the header; such a change fails closed under
`--require-hashes` until the locks are regenerated here.

`requirements/ci-tools.txt` is produced the same way from `coverage build pip-audit
ruff bandit`. `tests/test_dependency_locks.py` fails when a lock no longer satisfies
`pyproject.toml` or when the workflow installs anything outside the locks.
