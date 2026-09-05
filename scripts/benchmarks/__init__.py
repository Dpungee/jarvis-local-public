"""Public benchmark runners for Jarvis (VTMF M5, half B).

These benchmarks **demonstrate**; they never gate.  The release authority is the
sealed one-use holdout set, scored once per phase against a frozen runtime pin.
Nothing in this package may be imported by a sealed evaluation, a development
battery, or any module under ``jarvis/``: the dependency runs one way only.

The package deliberately lives outside the shipped wheel
(``pyproject.toml`` ships ``["jarvis", "jarvis.gateway"]``) for three reasons:
a wheel must not carry a network-fetching module, ``jarvis/`` stays
offline-capable product code, and ``self_diagnosis.runtime_manifest_sha256``
hashes every ``.py`` under ``jarvis/`` and ``tests/`` -- a benchmark edit must
not move the runtime manifest.

Entry point::

    python scripts/benchmarks/run.py --help

Only :mod:`scripts.benchmarks.driver` imports ``jarvis``, and it does so
lazily, so the scoring, caching and reporting layers stay pure standard
library and testable without a provider.
"""

from __future__ import annotations

__all__ = ["RUNNER_VERSION"]

# Bumped whenever a scoring path changes, so an old report cannot be silently
# compared with a new one.  It is part of every published config hash.
RUNNER_VERSION = "1.0.0"
