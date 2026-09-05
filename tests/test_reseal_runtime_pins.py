from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = PROJECT_ROOT / "scripts" / "reseal_runtime_pins.py"
GRAPH_FIXTURE = "tests/fixtures/memory_graph_holdout_v4.json"
SYNTHETIC_START = "# -- BEGIN SEALED SYNTHETIC PROBE HOLDOUT V1 SCORER --"
SYNTHETIC_END = "# -- END SEALED SYNTHETIC PROBE HOLDOUT V1 SCORER --"
OPENING_DOCSTRING = '\"\"\"A synthetic per-file-pinned holdout, for these tests only.\"\"\"'


def _load_module():
    """Import the script as a module without running it."""
    spec = importlib.util.spec_from_file_location("_reseal_runtime_pins", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class ResealInvariantTests(unittest.TestCase):
    """The safety invariant, unit level: only ``*_sha256`` fields may move.

    A reseal recomputes digests of other fixture content.  The moment it would
    change an arm, an outcome, a threshold or a measured value it has stopped
    being a reseal and become a rescore, which is forbidden -- so the tool
    refuses to write at all rather than writing the digests and skipping the
    rest.
    """

    def setUp(self) -> None:
        self.module = _load_module()

    def test_recomputed_digests_are_allowed(self) -> None:
        old = {"runtime_sha256": "a" * 64, "rows": [{"outcome_sha256": "b" * 64}]}
        new = {"runtime_sha256": "c" * 64, "rows": [{"outcome_sha256": "d" * 64}]}
        self.assertEqual(self.module.check_invariant("fixture", old, new), 2)

    def test_a_non_digest_change_is_refused(self) -> None:
        old = {"runtime_sha256": "a" * 64, "thresholds": {"chain_precision": 1.0}}
        new = {"runtime_sha256": "c" * 64, "thresholds": {"chain_precision": 0.9}}
        with self.assertRaises(SystemExit) as caught:
            self.module.check_invariant("fixture", old, new)
        self.assertIn("only recompute", str(caught.exception))

    def test_a_changed_measurement_is_refused_even_beside_digests(self) -> None:
        # The dangerous shape: a legitimate digest move used as cover for a
        # score that also moved.
        old = {"runtime_sha256": "a" * 64, "store_p95_ms": 20.4}
        new = {"runtime_sha256": "c" * 64, "store_p95_ms": 25.0}
        with self.assertRaises(SystemExit):
            self.module.check_invariant("fixture", old, new)

    def test_a_removed_field_is_refused(self) -> None:
        old = {"runtime_sha256": "a" * 64, "thresholds": {"leakage": 0}}
        new = {"runtime_sha256": "a" * 64, "thresholds": {}}
        with self.assertRaises(SystemExit):
            self.module.check_invariant("fixture", old, new)

    def test_the_byte_format_of_the_live_graph_fixture_is_detected(self) -> None:
        # The tool refuses to rewrite a fixture whose exact bytes it cannot
        # reproduce, so this is what stands between a reseal and an
        # incidental reformat of a sealed file.
        path = PROJECT_ROOT / GRAPH_FIXTURE
        raw = path.read_bytes()
        data = json.loads(raw.decode("utf-8"))
        fmt = self.module.detect_json_format(raw, data)
        self.assertIsNotNone(fmt)
        self.assertEqual(self.module.serialize_like(data, fmt), raw)

    def test_the_scorer_digest_matches_the_sealed_block(self) -> None:
        test_path = PROJECT_ROOT / "tests" / "test_memory_graph_holdout_v4.py"
        text = test_path.read_bytes().decode("utf-8")
        computed = self.module.sealed_scorer_sha256(text)
        pinned = next(
            line.split('"')[1]
            for line in text.replace("\r\n", "\n").split("\n")
            if line.startswith("SCORER_SHA256 = ")
        )
        self.assertEqual(computed, pinned)


class ResealDryRunTests(unittest.TestCase):
    """The tool end to end, in a subprocess, never with ``--apply`` on the tree."""

    def _run(self, root: Path) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(SCRIPT), str(root)],
            capture_output=True, text=True, cwd=str(root), timeout=600,
        )

    def test_the_real_tree_reports_no_change(self) -> None:
        result = self._run(PROJECT_ROOT)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("no change: every runtime pin is already current", result.stdout)
        # Dry run means dry run.
        self.assertNotIn("wrote ", result.stdout)

    def test_the_real_tree_prints_the_run_token(self) -> None:
        result = self._run(PROJECT_ROOT)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("JARVIS_MEMORY_GRAPH_HOLDOUT_V4_TOKEN", result.stdout)
        token = next(
            line.rsplit(": ", 1)[1].strip()
            for line in result.stdout.splitlines()
            if "run token (" in line
        )
        self.assertRegex(token, r"^[0-9a-f]{64}$")

    #: Everything the tool needs to run against a copy of the tree.
    TREE_FILES = (
        GRAPH_FIXTURE,
        "tests/test_memory_graph_holdout_v4.py",
        "tests/test_long_horizon_eval.py",
        "tests/test_strategy_transfer_trial_eval.py",
    )

    def _build_tree(self, temp: str) -> Path:
        root = Path(temp) / "tree"
        root.mkdir()
        shutil.copytree(
            PROJECT_ROOT / "jarvis", root / "jarvis",
            ignore=shutil.ignore_patterns("__pycache__"),
        )
        (root / "tests" / "fixtures").mkdir(parents=True)
        for relative in self.TREE_FILES:
            shutil.copy2(PROJECT_ROOT / relative, root / relative)
        return root

    def _add_synthetic_family(self, root: Path, module) -> tuple[str, str]:
        """A second per-file-pinned holdout, built from nothing but the shape.

        Nothing about the graph family is special to the tool: a family is any
        fixture whose ``runtime_sha256`` is a path -> digest object with a test
        beside it.  Proving that with a fixture the tool has never seen is the
        only way to know the cascade is genuinely discovered, rather than a
        list of one that a glob happens to find.
        """
        stem = "synthetic_probe"
        token_variable = "JARVIS_SYNTHETIC_PROBE_HOLDOUT_V1_TOKEN"
        fixture = root / "tests" / "fixtures" / (stem + "_holdout_v1.json")
        test = root / "tests" / ("test_" + stem + "_holdout_v1.py")
        # Deliberately stale pins: the reseal must move both, and nothing else.
        artifact = {
            "schema": "jarvis.synthetic-probe-holdout.v1",
            "generator_seed": 7,
            "thresholds": {"precision": 1.0, "leakage": 0},
            "cases": [{"id": "one", "expect": "recorded"}],
            "runtime_sha256": {
                "jarvis/memory.py": "0" * 64,
                "jarvis/redaction.py": "0" * 64,
            },
        }
        fixture.write_bytes(
            (json.dumps(artifact, indent=2, ensure_ascii=True) + "\n").encode("utf-8")
        )
        body = "\n".join([
            OPENING_DOCSTRING,
            "",
            # A non-placeholder digest, so the family counts as SEALED and
            # the tool reseals it.  The value is stale on purpose -- the
            # reseal recomputes it -- but it must not be the all-zero
            # placeholder, which means "never sealed" and is skipped.
            'FIXTURE_SHA256 = "' + "a" * 64 + '"',
            'SCORER_SHA256 = "{scorer}"',
            'SCORER_START = "' + SYNTHETIC_START + '"',
            'SCORER_END = "' + SYNTHETIC_END + '"',
            'TOKEN_ENVIRONMENT_VARIABLE = "' + token_variable + '"',
            "",
            SYNTHETIC_START,
            "def score(observed, expected):",
            "    return observed == expected",
            SYNTHETIC_END,
            "",
        ])
        # The scorer digest must be the one the tool will recompute, or the
        # tool correctly refuses to reseal what looks like a changed scorer.
        scorer = module.sealed_scorer_sha256(body.format(scorer="0" * 64))
        test.write_bytes(body.format(scorer=scorer).encode("utf-8"))
        return stem, token_variable

    def test_two_families_are_both_resealed_and_both_tokens_printed(self) -> None:
        """The generalized third cascade: families are discovered, not listed.

        The graph holdout was the only per-file-pinned family when this tool
        was written, and the cascade was written around it.  A second family
        must need no code change at all -- so this drops one into a copy of the
        tree and asserts the tool finds it, reseals it exactly as it reseals
        the graph, and prints its own run token under its own label.
        """
        module = _load_module()
        with tempfile.TemporaryDirectory() as temp:
            root = self._build_tree(temp)
            stem, token_variable = self._add_synthetic_family(root, module)

            families = module.holdout_families(root)
            self.assertEqual(
                [name for name, _fixture, _test in families],
                ["memory_graph", stem],
            )

            result = self._run(root)
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            self.assertNotIn("REFUSING", result.stdout)
            self.assertNotIn("third cascade skipped", result.stdout)
            # A dry run writes nothing, however many families it found.
            self.assertNotIn("wrote ", result.stdout)

            # Both families reseal.  The graph copy is already current; the
            # synthetic one carries planted stale digests that must move.
            self.assertIn("memory_graph_holdout_v4.json: ", result.stdout)
            self.assertIn(
                stem + "_holdout_v1.json: 2 digest value(s) recomputed",
                result.stdout,
            )
            self.assertIn("pass --apply to write", result.stdout)

            # Both tokens print, each under its own label and its own
            # environment-variable name.
            tokens = {}
            for line in result.stdout.splitlines():
                if " run token (" in line:
                    tokens[line.split(" run token (", 1)[0]] = (
                        line.rsplit(": ", 1)[1].strip()
                    )
            self.assertEqual(
                sorted(tokens), ["memory-graph", stem.replace("_", "-")]
            )
            for label, token in tokens.items():
                with self.subTest(label=label):
                    self.assertRegex(token, r"^[0-9a-f]{64}$")
            self.assertEqual(
                len(set(tokens.values())), 2, "two families shared one token"
            )
            self.assertIn("JARVIS_MEMORY_GRAPH_HOLDOUT_V4_TOKEN", result.stdout)
            self.assertIn(token_variable, result.stdout)

            # Each family's FIXTURE_SHA256 is offered against its own test.
            self.assertIn(
                "tests/test_memory_graph_holdout_v4.py: FIXTURE_SHA256 = ",
                result.stdout,
            )
            self.assertIn(
                "tests/test_" + stem + "_holdout_v1.py: FIXTURE_SHA256 = ",
                result.stdout,
            )

    def test_a_family_whose_newest_version_lost_its_test_falls_back(self) -> None:
        """A superseded holdout is quarantined out of the tree, test first.

        The newest version WITH a test is the one that reseals; a fixture left
        behind without its test is skipped rather than resealed, because a
        quarantined fixture must never be rescored or resealed.
        """
        module = _load_module()
        with tempfile.TemporaryDirectory() as temp:
            root = self._build_tree(temp)
            orphan = root / "tests" / "fixtures" / "memory_graph_holdout_v9.json"
            orphan.write_bytes(
                (
                    json.dumps(
                        {"runtime_sha256": {"jarvis/memory.py": "0" * 64}}, indent=2
                    )
                    + "\n"
                ).encode("utf-8")
            )

            families = module.holdout_families(root)

            self.assertEqual(
                [(name, fixture.name) for name, fixture, _test in families],
                [("memory_graph", "memory_graph_holdout_v4.json")],
            )

    def test_a_single_digest_pin_is_not_a_family(self) -> None:
        """The two module-set cascades are resealed by their own code path.

        A fixture whose ``runtime_sha256`` is one digest rather than a
        path -> digest object belongs to the strategy-transfer or long-horizon
        cascade; picking it up here would reseal it twice, by two rules.
        """
        module = _load_module()
        with tempfile.TemporaryDirectory() as temp:
            root = self._build_tree(temp)
            flat = root / "tests" / "fixtures" / "flat_pin_holdout_v1.json"
            flat.write_bytes(
                (json.dumps({"runtime_sha256": "a" * 64}, indent=2) + "\n").encode(
                    "utf-8"
                )
            )
            (root / "tests" / "test_flat_pin_holdout_v1.py").write_bytes(b"x = 1\n")

            self.assertEqual(
                [name for name, _fixture, _test in module.holdout_families(root)],
                ["memory_graph"],
            )

    def test_an_unsealed_holdout_is_skipped_not_sealed(self) -> None:
        """A newly authored holdout is SKIPPED, and does not block the others.

        Sealing a fresh holdout is a one-time act with its own discipline -- the
        author writes the fixture, the boss seals it once, runs it once, records
        the score.  A reseal is the different, mechanical thing that follows a
        runtime change.  If the tool sealed a placeholder on the way past, it
        would commission a holdout nobody decided to run; if it merely died on
        one, a single unsealed holdout would block the reseal of every other
        family, which is what a fresh M4 fixture in the tree actually did.
        """
        module = _load_module()
        with tempfile.TemporaryDirectory() as temp:
            root = self._build_tree(temp)
            stem, _token = self._add_synthetic_family(root, module)
            test_path = root / "tests" / ("test_" + stem + "_holdout_v1.py")
            sealed = test_path.read_bytes().decode("utf-8")
            # Exactly how a freshly authored holdout writes its placeholder.
            unsealed = sealed.replace(
                'FIXTURE_SHA256 = "' + "a" * 64 + '"',
                'FIXTURE_SHA256 = "0" * 64',
            )
            self.assertNotEqual(unsealed, sealed)
            test_path.write_bytes(unsealed.encode("utf-8"))
            self.assertTrue(module.is_unsealed(unsealed))

            result = self._run(root)

            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            self.assertIn("is not sealed yet", result.stdout)
            self.assertIn(stem + "_holdout_v1.json is not sealed yet", result.stdout)
            # Skipped means skipped: no digest report, no token, no constant.
            self.assertNotIn(
                stem + "_holdout_v1.json: ", result.stdout.replace(
                    stem + "_holdout_v1.json is not sealed yet", ""
                )
            )
            self.assertNotIn(stem.replace("_", "-") + " run token", result.stdout)
            self.assertNotIn("test_" + stem + "_holdout_v1.py: FIXTURE_SHA256",
                             result.stdout)
            # And the graph family is resealed exactly as it would have been.
            self.assertIn("JARVIS_MEMORY_GRAPH_HOLDOUT_V4_TOKEN", result.stdout)
            self.assertNotIn("REFUSING", result.stdout)

    def test_a_sealed_holdout_with_a_changed_scorer_is_still_fatal(self) -> None:
        """The refusal the skip must not weaken.

        A placeholder means "never sealed".  A REAL digest that disagrees with
        the scorer block means the scorer was edited after sealing, which is a
        rescore -- and that must still stop the tool dead rather than quietly
        re-sealing the new scorer.
        """
        module = _load_module()
        with tempfile.TemporaryDirectory() as temp:
            root = self._build_tree(temp)
            stem, _token = self._add_synthetic_family(root, module)
            test_path = root / "tests" / ("test_" + stem + "_holdout_v1.py")
            sealed = test_path.read_bytes().decode("utf-8")
            self.assertFalse(module.is_unsealed(sealed))
            # Edit the sealed block; its pinned digest now disagrees.
            tampered = sealed.replace(
                "    return observed == expected",
                "    return True  # scored everything a pass",
            )
            self.assertNotEqual(tampered, sealed)
            test_path.write_bytes(tampered.encode("utf-8"))
            self.assertFalse(module.is_unsealed(tampered))

            result = self._run(root)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("does not match", result.stdout + result.stderr)
            self.assertIn(
                "never re-seals a changed scorer", result.stdout + result.stderr
            )
            self.assertNotIn("wrote ", result.stdout)

    def test_the_placeholder_reader_accepts_both_spellings(self) -> None:
        module = _load_module()
        self.assertEqual(
            module.pinned_digest('SCORER_SHA256 = "0" * 64\n', "SCORER_SHA256"),
            "0" * 64,
        )
        self.assertEqual(
            module.pinned_digest(
                'SCORER_SHA256 = "' + "a" * 64 + '"\n', "SCORER_SHA256"
            ),
            "a" * 64,
        )
        self.assertIsNone(module.pinned_digest("x = 1\n", "SCORER_SHA256"))
        # A missing constant is not "unsealed": it is unreadable, and the
        # caller must fail on it rather than skip.
        self.assertFalse(module.is_unsealed("x = 1\n"))

    def test_a_changed_pinned_file_moves_only_digest_fields(self) -> None:
        """Edit a pinned source file in a copy; only digests may move."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "tree"
            root.mkdir()
            shutil.copytree(
                PROJECT_ROOT / "jarvis", root / "jarvis",
                ignore=shutil.ignore_patterns("__pycache__"),
            )
            (root / "tests" / "fixtures").mkdir(parents=True)
            for relative in (
                GRAPH_FIXTURE,
                "tests/test_memory_graph_holdout_v4.py",
                "tests/test_long_horizon_eval.py",
                "tests/test_strategy_transfer_trial_eval.py",
            ):
                shutil.copy2(PROJECT_ROOT / relative, root / relative)

            pinned = root / "jarvis" / "memory_retrieval.py"
            pinned.write_bytes(
                pinned.read_bytes() + b"\n# a legitimate change to a pinned file\n"
            )

            before = json.loads((root / GRAPH_FIXTURE).read_text(encoding="utf-8"))
            result = self._run(root)
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            # A dry run still writes nothing, whatever it found.
            after = json.loads((root / GRAPH_FIXTURE).read_text(encoding="utf-8"))
            self.assertEqual(after, before)
            self.assertNotIn("wrote ", result.stdout)
            # And what it found is digests, nothing else.
            self.assertIn("digest value(s) recomputed", result.stdout)
            self.assertNotIn("REFUSING", result.stdout)
            self.assertIn("pass --apply to write", result.stdout)

            module = _load_module()
            resealed = module.reseal_per_file_pin(before, root)
            changes = module.deep_changes(before, resealed)
            self.assertTrue(changes)
            for path, _old, _new in changes:
                self.assertTrue(
                    path.rsplit("/", 1)[-1].endswith(".py")
                    and "runtime_sha256" in path,
                    f"a non-pin field moved: {path}",
                )

    def test_a_tampered_non_digest_field_is_refused(self) -> None:
        """The refusal path, through the tool rather than the helper."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "tree"
            root.mkdir()
            shutil.copytree(
                PROJECT_ROOT / "jarvis", root / "jarvis",
                ignore=shutil.ignore_patterns("__pycache__"),
            )
            (root / "tests" / "fixtures").mkdir(parents=True)
            for relative in (
                GRAPH_FIXTURE,
                "tests/test_memory_graph_holdout_v4.py",
                "tests/test_long_horizon_eval.py",
                "tests/test_strategy_transfer_trial_eval.py",
            ):
                shutil.copy2(PROJECT_ROOT / relative, root / relative)

            # A rescore disguised as a reseal: the long-horizon artifact's own
            # manifest digest is recomputed over its contents, so changing a
            # non-digest value there makes the cascade disagree with the file.
            horizon = (
                root / "jarvis" / "evaluation_fixtures"
                / "long_horizon_restart_holdout_v1.json"
            )
            data = json.loads(horizon.read_text(encoding="utf-8"))
            module = _load_module()
            key = next(
                name for name, value in data.items()
                if not name.endswith("_sha256") and isinstance(value, str)
            )
            tampered = dict(data)
            tampered[key] = str(data[key]) + "-rescored"
            with self.assertRaises(SystemExit) as caught:
                module.check_invariant("long_horizon", data, tampered)
            self.assertIn("not permitted", str(caught.exception))


class SourceHygieneTests(unittest.TestCase):
    """Two file-level invariants the tree relies on and had no test for.

    Both were found the hard way on 2026-09-04: a patch script written through
    a bash heredoc emitted a raw 0x00 where it meant the two-character escape,
    and `jarvis/memory.py` became unimportable -- which took down every suite
    in the tree for three agents at once, with an error that names the symptom
    and not the file that caused it.

    The LF rule is a CLAUDE.md convention that was being checked by hand.  A
    convention with no test is a convention that drifts.
    """

    #: Directories whose Python sources are ours to keep clean.
    ROOTS = ("jarvis", "tests", "scripts")

    def _sources(self):
        for root in self.ROOTS:
            base = PROJECT_ROOT / root
            if not base.is_dir():
                continue
            for path in sorted(base.rglob("*.py")):
                if "__pycache__" in path.parts:
                    continue
                yield path

    def test_no_source_file_contains_a_null_byte(self) -> None:
        """A single NUL makes a module unimportable, whatever else it says."""
        offenders = []
        for path in self._sources():
            raw = path.read_bytes()
            if b"\x00" in raw:
                index = raw.index(b"\x00")
                offenders.append(
                    f"{path.relative_to(PROJECT_ROOT)} at byte {index} "
                    f"(line {raw[:index].count(chr(10).encode()) + 1})"
                )
        self.assertEqual(offenders, [])

    def test_every_source_file_uses_lf_line_endings(self) -> None:
        """CLAUDE.md's LF rule, enforced rather than remembered.

        `Path.write_text` converts to CRLF on this host, which dirties an
        entire file in a diff and hides the real change.
        """
        offenders = [
            str(path.relative_to(PROJECT_ROOT))
            for path in self._sources()
            if b"\r\n" in path.read_bytes()
        ]
        self.assertEqual(offenders, [])

    def test_the_guard_would_actually_catch_one(self) -> None:
        """A guard nobody has seen fail is a guard nobody knows works.

        Planted in a temp directory rather than in the tree, so the guard
        is exercised against a file that really carries the byte.
        """
        with tempfile.TemporaryDirectory() as temp:
            planted = Path(temp) / 'planted.py'
            planted.write_bytes(b'x = 1' + bytes([0]) + bytes([10]))
            raw = planted.read_bytes()
            self.assertIn(bytes([0]), raw)
            with self.assertRaises((ValueError, SyntaxError)):
                compile(raw.decode('utf-8'), str(planted), 'exec')
            crlf = Path(temp) / 'crlf.py'
            crlf.write_bytes(b'x = 1' + bytes([13, 10]))
            self.assertIn(bytes([13, 10]), crlf.read_bytes())


if __name__ == "__main__":
    unittest.main()
