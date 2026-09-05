#!/usr/bin/env python3
"""Recompute the runtime-hash pins in the sealed evaluation fixtures.

Two sealed evaluations bind their artifact to the exact runtime that produced
it by hashing a fixed set of source modules into one digest:

    strategy_transfer_runtime_sha256() -> agent.py, memory.py,
                                          strategy_transfer.py,
                                          strategy_transfer_trial.py
    long_horizon_runtime_sha256()      -> long_horizon.py, memory.py,
                                          long_horizon_eval_worker.py

Beyond those, every **per-file-pinned holdout family** is resealed the same
way. A family is any ``tests/fixtures/<stem>_holdout_v<N>.json`` whose
``runtime_sha256`` is an object of path -> sha256 rather than a single digest
-- the shape the M3 memory-graph holdout introduced and that later holdouts
reuse. For each family the newest version that still has a
``tests/test_<stem>_holdout_v<N>.py`` is resealed: the in-fixture pin first,
then ``FIXTURE_SHA256`` in its test, then the run token, which is derived and
printed but never stored. Families are discovered, not listed, so a new
holdout is picked up by dropping its fixture and test into the tree.

The sealed scorer is never re-sealed. If a family's scorer block no longer
matches its own ``SCORER_SHA256`` the tool stops: that is a changed scorer,
which is a rescore.

Because ``memory.py`` is in both module sets and in most per-file pins, any
edit to it invalidates every seal and the sealed tests fail with "runtime pin
mismatch". Restoring them is a mechanical recomputation of derived digests,
not a rescore: every value this tool touches is a SHA-256 of other fixture
content, and the cascade is fully determined by the fixture's own data.

    runtime_sha256 -> manifest_sha256 -> assignment_sha256
                                      -> prompt_receipt_sha256
                                      -> provider_dispatch_sha256
                                      -> outcome_sha256

THE SAFETY INVARIANT: this tool refuses to write anything if a field whose name
does not end in ``_sha256`` would change. No arm, outcome, exit criterion,
threshold, timestamp, or measured value may be altered. A reseal that changed
such a value would be a rescore, which is forbidden. Run the sealed evaluations
after applying and confirm the reported metrics are unchanged -- the digests
prove provenance, the metrics prove the result.

Usage:
    python scripts/reseal_runtime_pins.py .            # check only
    python scripts/reseal_runtime_pins.py . --apply    # write
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

ALLOWED_CHANGED_SUFFIX = "_sha256"
DEFAULT_ROOT = Path(__file__).resolve().parents[1]


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def without(mapping: dict[str, Any], field: str) -> dict[str, Any]:
    return {key: item for key, item in mapping.items() if key != field}


def deep_changes(old: Any, new: Any, path: str = "") -> list[tuple[str, Any, Any]]:
    """Return every leaf that differs, as (path, old, new)."""
    if isinstance(old, dict) and isinstance(new, dict):
        changes: list[tuple[str, Any, Any]] = []
        for key in sorted(set(old) | set(new)):
            changes += deep_changes(
                old.get(key, KeyError), new.get(key, KeyError), f"{path}/{key}"
            )
        return changes
    if isinstance(old, list) and isinstance(new, list):
        changes = []
        if len(old) != len(new):
            return [(path + "[len]", len(old), len(new))]
        for index, (left, right) in enumerate(zip(old, new)):
            changes += deep_changes(left, right, f"{path}[{index}]")
        return changes
    return [] if old == new else [(path, old, new)]


def reseal_trial(artifact: dict[str, Any], runtime_sha256: str) -> dict[str, Any]:
    """Recompute the strategy-transfer trial digest cascade in dependency order."""
    artifact = json.loads(json.dumps(artifact))
    manifest = artifact["manifest"]
    manifest["runtime_sha256"] = runtime_sha256
    manifest["manifest_sha256"] = digest(without(manifest, "manifest_sha256"))

    for row in artifact["rows"]:
        assignment = row["assignment"]
        assignment["manifest_sha256"] = manifest["manifest_sha256"]
        assignment["assignment_sha256"] = digest(
            without(assignment, "assignment_sha256")
        )
        assignment_digest = assignment["assignment_sha256"]

        prompt = row["prompt_receipt"]
        prompt["assignment_sha256"] = assignment_digest
        prompt["prompt_receipt_sha256"] = digest(
            without(prompt, "prompt_receipt_sha256")
        )
        prompt_digest = prompt["prompt_receipt_sha256"]

        dispatch = row["provider_dispatch"]
        dispatch["assignment_sha256"] = assignment_digest
        dispatch["prompt_receipt_sha256"] = prompt_digest
        dispatch["provider_dispatch_sha256"] = digest(
            without(dispatch, "provider_dispatch_sha256")
        )

        outcome = row["outcome"]
        outcome["assignment_sha256"] = assignment_digest
        outcome["prompt_receipt_sha256"] = prompt_digest
        outcome["outcome_sha256"] = digest(without(outcome, "outcome_sha256"))
    return artifact


def reseal_long_horizon(
    artifact: dict[str, Any], runtime_sha256: str
) -> dict[str, Any]:
    artifact = json.loads(json.dumps(artifact))
    artifact["runtime_sha256"] = runtime_sha256
    artifact["fixture_manifest_sha256"] = digest(
        without(artifact, "fixture_manifest_sha256")
    )
    return artifact


def serialize(
    artifact: dict[str, Any], *, sort_keys: bool, ensure_ascii: bool = False
) -> bytes:
    return (
        json.dumps(
            artifact, indent=2, sort_keys=sort_keys, ensure_ascii=ensure_ascii
        )
        + "\n"
    ).encode("utf-8")


def reseal_per_file_pin(artifact: dict[str, Any], root: Path) -> dict[str, Any]:
    """Fill a holdout's per-file runtime pin, whichever family it belongs to.

    The pin is an ordered object of path -> sha256 of the file bytes, exactly
    as the holdout test recomputes it; nothing else moves.
    """
    artifact = json.loads(json.dumps(artifact))
    pin = artifact["runtime_sha256"]
    for name in list(pin):
        path = root / name
        if not path.exists():
            raise SystemExit(f"pinned file missing: {name}")
        pin[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return artifact


def sealed_scorer_sha256(test_text: str) -> str:
    """Digest of the sealed scorer block, as the holdout test computes it.

    The block markers are read from the module's own SCORER_START /
    SCORER_END constants so every holdout version seals the same way.
    """
    normalized = test_text.replace("\r\n", "\n").replace("\r", "\n")
    start_marker = re.search(r'^SCORER_START = "([^"]+)"$', normalized, re.M)
    end_marker = re.search(r'^SCORER_END = "([^"]+)"$', normalized, re.M)
    if start_marker is None or end_marker is None:
        raise SystemExit("holdout test has no SCORER_START / SCORER_END constants")
    opening = start_marker.group(1) + "\n"
    closing = "\n" + end_marker.group(1)
    start = normalized.index(opening) + len(opening)
    end = normalized.index(closing, start)
    return hashlib.sha256(normalized[start:end].encode("utf-8")).hexdigest()


def detect_json_format(raw: bytes, data: dict[str, Any]) -> dict[str, Any] | None:
    """The json.dumps parameters that reproduce ``raw`` byte for byte, or None."""
    for indent in (2, 1, 4, None):
        for sort_keys in (False, True):
            for ensure_ascii in (True, False):
                for newline in ("\n", ""):
                    candidate = json.dumps(
                        data,
                        indent=indent,
                        sort_keys=sort_keys,
                        ensure_ascii=ensure_ascii,
                    ) + newline
                    if candidate.encode("utf-8") == raw:
                        return {
                            "indent": indent,
                            "sort_keys": sort_keys,
                            "ensure_ascii": ensure_ascii,
                            "newline": newline,
                        }
    return None


def serialize_like(data: dict[str, Any], fmt: dict[str, Any]) -> bytes:
    return (
        json.dumps(
            data,
            indent=fmt["indent"],
            sort_keys=fmt["sort_keys"],
            ensure_ascii=fmt["ensure_ascii"],
        )
        + fmt["newline"]
    ).encode("utf-8")


PLACEHOLDER_DIGEST = "0" * 64


def pinned_digest(test_text: str, name: str) -> str | None:
    """Read a holdout test's sealing constant, or None if it has none.

    Two spellings are accepted because a freshly authored holdout writes its
    placeholder as an expression: ``NAME = "<64 hex>"`` once sealed, and
    ``NAME = "0" * 64`` while it is still waiting to be.
    """
    literal = re.search(rf'^{re.escape(name)} = "([0-9a-f]{{64}})"$', test_text, re.M)
    if literal is not None:
        return literal.group(1)
    placeholder = re.search(
        rf'^{re.escape(name)} = "0" \* 64$', test_text, re.M
    )
    return PLACEHOLDER_DIGEST if placeholder is not None else None


def is_unsealed(test_text: str) -> bool:
    """True when a holdout has never been sealed, so there is nothing to RE-seal.

    Sealing a newly authored holdout is a one-time act with its own
    discipline: the author writes the fixture, the boss seals it once, runs it
    once, and records the score.  A reseal is the different, mechanical thing
    that follows a runtime change.  Doing the first as a side effect of the
    second would seal a holdout nobody had decided to commission.

    The distinction matters for the refusal below: a placeholder means "not
    sealed yet", while a real digest that disagrees with the scorer block
    means the scorer CHANGED after sealing, which is a rescore and is fatal.
    """
    return PLACEHOLDER_DIGEST in {
        pinned_digest(test_text, "SCORER_SHA256"),
        pinned_digest(test_text, "FIXTURE_SHA256"),
    }


def holdout_families(root: Path) -> list[tuple[str, Path, Path]]:
    """Every per-file-pinned holdout family: (stem, newest fixture, its test).

    A family is ``tests/fixtures/<stem>_holdout_v<N>.json`` whose
    ``runtime_sha256`` is an object of path -> sha256 -- the shape the M3
    memory-graph holdout introduced and that later holdouts reuse -- paired
    with ``tests/test_<stem>_holdout_v<N>.py``.

    Only the NEWEST version that still has a test is resealed. Older versions
    are quarantined out of the tree when a holdout is superseded, and a
    quarantined fixture must never be rescored or resealed; if its test is
    gone the version is skipped and the search falls back to the one below it.
    A fixture whose ``runtime_sha256`` is a single digest belongs to one of the
    two module-set cascades above and is not a family.

    Discovery rather than a list, so a new holdout joins the cascade by being
    added to the tree -- the thing that otherwise gets forgotten until a sealed
    test fails months later.
    """
    families: dict[str, list[tuple[int, Path]]] = {}
    for fixture in (root / "tests" / "fixtures").glob("*_holdout_v*.json"):
        match = re.search(r"^(.*)_holdout_v(\d+)\.json$", fixture.name)
        if match is None:
            continue
        try:
            data = json.loads(fixture.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # An unreadable or non-JSON fixture is not silently resealed.
            continue
        if not isinstance(data, dict) or not isinstance(
            data.get("runtime_sha256"), dict
        ):
            continue
        families.setdefault(match.group(1), []).append(
            (int(match.group(2)), fixture)
        )
    found: list[tuple[str, Path, Path]] = []
    for stem, versions in sorted(families.items()):
        for version, fixture in sorted(versions, reverse=True):
            test = root / "tests" / f"test_{stem}_holdout_v{version}.py"
            if test.exists():
                found.append((stem, fixture, test))
                break
    return found


def check_invariant(label: str, old: dict[str, Any], new: dict[str, Any]) -> int:
    changes = deep_changes(old, new)
    illegal = [
        item for item in changes
        if not any(
            segment.endswith(ALLOWED_CHANGED_SUFFIX)
            for segment in item[0].split("/")
        )
    ]
    if illegal:
        print(f"  REFUSING {label}: {len(illegal)} non-digest field(s) would change:")
        for path, before, after in illegal[:10]:
            print(f"    {path}: {before!r} -> {after!r}")
        raise SystemExit(
            "A reseal may only recompute *_sha256 digests. Changing any other "
            "field would be a rescore, which is not permitted."
        )
    fields: dict[str, int] = {}
    for path, _before, _after in changes:
        name = path.rsplit("/", 1)[-1]
        fields[name] = fields.get(name, 0) + 1
    print(f"  {label}: {len(changes)} digest value(s) recomputed")
    for name, count in sorted(fields.items(), key=lambda item: -item[1]):
        print(f"    {name}: {count}")
    return len(changes)


def replace_constant(text: str, name: str, value: str) -> tuple[str, bool]:
    pattern = re.compile(rf'^({re.escape(name)} = ")[0-9a-f]{{64}}(")$', re.M)
    if pattern.search(text) is None:
        raise SystemExit(f"constant {name} not found")
    new_text, count = pattern.subn(rf"\g<1>{value}\g<2>", text)
    return new_text, count > 0 and new_text != text


def main(argv: list[str] | None = None) -> None:
    arguments = list(sys.argv[1:] if argv is None else argv)
    apply = "--apply" in arguments
    positional = [item for item in arguments if not item.startswith("-")]
    root = Path(positional[0]).resolve() if positional else DEFAULT_ROOT
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))

    from jarvis.long_horizon_eval import long_horizon_runtime_sha256
    from jarvis.strategy_transfer_trial import strategy_transfer_runtime_sha256

    import jarvis.memory as memory_module
    if not Path(memory_module.__file__).resolve().is_relative_to(root):
        raise SystemExit(
            f"the imported jarvis package resolved outside {root}: "
            f"{memory_module.__file__}"
        )

    fixtures = root / "jarvis" / "evaluation_fixtures"
    trial_path = fixtures / "strategy_transfer_trial_holdout_v1.json"
    horizon_path = fixtures / "long_horizon_restart_holdout_v1.json"

    print(f"repo: {root}")
    print(f"strategy-transfer runtime: {strategy_transfer_runtime_sha256()}")
    print(f"long-horizon runtime:      {long_horizon_runtime_sha256()}")
    print("verifying reseal only touches *_sha256 digests:")

    trial_old = json.loads(trial_path.read_text(encoding="utf-8"))
    trial_new = reseal_trial(trial_old, strategy_transfer_runtime_sha256())
    trial_changes = check_invariant(
        "strategy_transfer_trial_holdout_v1.json", trial_old, trial_new
    )

    horizon_old = json.loads(horizon_path.read_text(encoding="utf-8"))
    horizon_new = reseal_long_horizon(horizon_old, long_horizon_runtime_sha256())
    horizon_changes = check_invariant(
        "long_horizon_restart_holdout_v1.json", horizon_old, horizon_new
    )

    # Third cascade and beyond: every per-file-pinned holdout family reseals
    # the same way -- the in-fixture pin, then FIXTURE_SHA256 in its test, then
    # the run token. The families are discovered, so adding a holdout to the
    # tree is all it takes to put it in the cascade.
    holdouts: list[tuple[str, Path, Path, dict[str, Any], bytes]] = []
    holdout_changes = 0
    for stem, fixture_path, test_path in holdout_families(root):
        label = stem.replace("_", "-")
        test_text = test_path.read_bytes().decode("utf-8")
        if is_unsealed(test_text):
            # Commissioned but not yet sealed.  Checked FIRST, before the
            # invariant report, so an unsealed holdout does not print a digest
            # count for a reseal that will not happen.
            print(
                f"{label} holdout: {fixture_path.name} is not sealed yet "
                "(placeholder digests); skipped"
            )
            continue
        old_artifact = json.loads(fixture_path.read_text(encoding="utf-8"))
        new_artifact = reseal_per_file_pin(old_artifact, root)
        holdout_changes += check_invariant(
            fixture_path.name, old_artifact, new_artifact
        )
        fixture_format = detect_json_format(fixture_path.read_bytes(), old_artifact)
        if fixture_format is None:
            raise SystemExit(
                f"{fixture_path.name} is not in a standard json.dumps byte "
                "format; refusing to rewrite it"
            )
        new_bytes = serialize_like(new_artifact, fixture_format)
        print(f"{label} fixture format: {fixture_format}")
        scorer_now = sealed_scorer_sha256(test_text)
        scorer_pinned = pinned_digest(test_text, "SCORER_SHA256")
        if scorer_pinned is None or scorer_pinned != scorer_now:
            raise SystemExit(
                f"the sealed scorer block of {test_path.name} does not match "
                "SCORER_SHA256; a reseal never re-seals a changed scorer"
            )
        fixture_sha256 = hashlib.sha256(new_bytes).hexdigest()
        token = hashlib.sha256(
            f"{fixture_sha256}:{scorer_now}".encode("ascii")
        ).hexdigest()
        print(f"{label} runtime pin: {new_artifact['runtime_sha256']}")
        token_name = re.search(
            r'^TOKEN_ENVIRONMENT_VARIABLE = "([A-Z0-9_]+)"$', test_text, re.M
        )
        token_label = token_name.group(1) if token_name else "run token"
        print(f"{label} holdout: {fixture_path.name}")
        print(f"{label} run token ({token_label}): {token}")
        holdouts.append((label, fixture_path, test_path, new_artifact, new_bytes))
    if not holdouts:
        print("no per-file-pinned holdout present: third cascade skipped")

    trial_bytes = serialize(trial_new, sort_keys=True)
    horizon_bytes = serialize(horizon_new, sort_keys=False)
    constants = {
        "tests/test_strategy_transfer_trial_eval.py": [
            ("ARTIFACT_SHA256", hashlib.sha256(trial_bytes).hexdigest()),
            ("MANIFEST_SHA256", trial_new["manifest"]["manifest_sha256"]),
        ],
        "tests/test_long_horizon_eval.py": [
            ("FIXTURE_SHA256", hashlib.sha256(horizon_bytes).hexdigest()),
            # The test also pins the evaluator and worker module bytes. They
            # only move when those modules change (for example when the
            # evaluator's runtime-hash module list grows); the evaluator diff
            # must then be provenance-only.
            (
                "EVALUATOR_SHA256",
                hashlib.sha256(
                    (root / "jarvis" / "long_horizon_eval.py").read_bytes()
                ).hexdigest(),
            ),
            (
                "WORKER_SHA256",
                hashlib.sha256(
                    (root / "jarvis" / "long_horizon_eval_worker.py").read_bytes()
                ).hexdigest(),
            ),
        ],
    }
    for _label, _fixture_path, test_path, _artifact, new_bytes in holdouts:
        constants[str(test_path.relative_to(root)).replace("\\", "/")] = [
            ("FIXTURE_SHA256", hashlib.sha256(new_bytes).hexdigest()),
        ]
    print("test constants:")
    for relative, pairs in constants.items():
        for name, value in pairs:
            print(f"  {relative}: {name} = {value}")

    if not apply:
        total = trial_changes + horizon_changes + holdout_changes
        if total == 0:
            print("\nno change: every runtime pin is already current.")
        else:
            print(f"\ncheck only; pass --apply to write {total} digest value(s).")
        return

    # Round-trip guard: the bytes we write must parse back to what we verified.
    artifacts = [
        (trial_path, trial_new, trial_bytes),
        (horizon_path, horizon_new, horizon_bytes),
    ]
    for _label, fixture_path, _test_path, new_artifact, new_bytes in holdouts:
        artifacts.append((fixture_path, new_artifact, new_bytes))
    for path, data, raw in artifacts:
        if json.loads(raw.decode("utf-8")) != data:
            raise SystemExit(f"serialization round-trip failed for {path.name}")
        path.write_bytes(raw)
        print(f"wrote {path.relative_to(root)}")

    for relative, pairs in constants.items():
        path = root / relative
        text = path.read_bytes().decode("utf-8")
        for name, value in pairs:
            text, _changed = replace_constant(text, name, value)
        path.write_bytes(text.encode("utf-8"))
        print(f"wrote {relative}")
    print("\nreseal applied. Run the sealed evaluations and confirm the metrics.")


if __name__ == "__main__":
    main()
