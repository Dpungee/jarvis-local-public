"""The leakage firewall between public benchmarks and sealed evidence.

A public benchmark is re-runnable and gates nothing; a sealed holdout is scored
once against a frozen runtime pin and decides whether a phase ships. The failure
mode this file exists to prevent is the two touching: a benchmark case reaching a
sealed fixture, a dataset byte reaching the repository, or a public number
reaching a gate.

Six assertions, and one of them is deliberately inert on a clean checkout --
see :class:`DatasetValuesAreAbsentTests`, which says so out loud rather than
letting a green CI imply a coverage it does not have.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Iterable

from scripts.benchmarks import cache, driver, locomo, longmemeval, report, ruler_style
from scripts.check_public_release import MAX_TRACKED_FILE_BYTES

ROOT = cache.repository_root()
SEALED_FIXTURE_DIRECTORIES = (ROOT / "tests" / "fixtures", ROOT / "jarvis" / "evaluation_fixtures")
BENCHMARK_PACKAGE = ROOT / "scripts" / "benchmarks"


def _git_paths(*args: str) -> list[str]:
    output = subprocess.run(
        ["git", *args, "-z"],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    ).stdout
    return [entry.decode("utf-8", errors="surrogateescape") for entry in output.split(b"\0") if entry]


def tracked_files() -> list[str]:
    """Every path a ``git add -A`` and a commit from this tree would publish.

    Three deliberate differences from a plain ``git ls-files``, and the first
    two make this **stronger**, not weaker:

    * untracked-but-not-ignored files are included, so a dataset dropped into
      the tree and waiting for ``git add`` is caught -- the plain index listing
      missed exactly that;
    * ignored files are excluded, because the benchmark cache and
      ``reports/benchmarks/`` are generated artefacts that no commit carries;
    * a path deleted in the working tree is excluded, because ``git add -A``
      would remove it. That is what a rename looks like before it is staged.
      The index as it stands is inspected at release time by
      ``scripts/check_public_release.py``, which is the commit-time authority.
    """

    listed = _git_paths("ls-files", "--cached", "--others", "--exclude-standard")
    deleted = set(_git_paths("ls-files", "--deleted"))
    return sorted(set(listed) - deleted)


def dataset_filenames() -> set[str]:
    """Every filename that would mean a dataset had been vendored."""

    forbidden = {spec.filename.casefold() for spec in cache.DATASETS.values()}
    forbidden.update(
        {
            "longmemeval_s_cleaned.json",
            "longmemeval_m_cleaned.json",
            "longmemeval_oracle.json",
            "locomo10.json",
            "msc_personas_all.json",
        }
    )
    return forbidden


def dataset_filename_offenders(paths: Iterable[str]) -> list[str]:
    """Paths whose basename is, or contains, a dataset filename.

    Containment rather than equality: a vendored dataset saved as
    ``copy-of-locomo10.json`` is still a vendored dataset.  The rule is also
    the reason the shipped config templates are named ``<benchmark>_config.json``
    rather than after the dataset -- ``locomo10_config.json`` neither equals
    nor contains ``locomo10.json``, so the guard cannot confuse a template for
    the thing it describes, and it no longer has to.
    """

    forbidden = dataset_filenames()
    offenders: list[str] = []
    for path in paths:
        name = Path(path).name.casefold()
        if any(bad in name for bad in forbidden):
            offenders.append(path)
    return sorted(offenders)


class NoDatasetInTheRepositoryTests(unittest.TestCase):
    """Assertion 1: no dataset is vendored, and the size guard is untouched."""

    def test_no_publishable_file_carries_a_dataset_filename(self) -> None:
        self.assertEqual(dataset_filename_offenders(tracked_files()), [])

    def test_every_shipped_config_template_passes_that_guard(self) -> None:
        """The rename is checked, not argued.

        The guard cannot tell a template from the dataset it configures, so the
        templates carry names the rule provably does not match -- under
        equality, containment, stem and first-dotted-component alike.
        """

        templates = sorted((ROOT / "scripts" / "benchmarks" / "configs").glob("*.json"))
        self.assertGreaterEqual(len(templates), 6)
        self.assertEqual(dataset_filename_offenders(str(path) for path in templates), [])
        stems = {Path(name).stem for name in dataset_filenames()}
        for path in templates:
            with self.subTest(template=path.name):
                name = path.name.casefold()
                self.assertTrue(name.endswith("_config.json"))
                self.assertNotIn(name, dataset_filenames())
                self.assertNotIn(path.stem.casefold(), stems)
                self.assertNotIn(name.split(".")[0], stems)

    def test_the_guard_still_catches_a_real_vendored_dataset(self) -> None:
        # The rename must not have been achieved by blunting the rule.
        for probe in (
            "scripts/benchmarks/configs/locomo10.json",
            "data/locomo10.json",
            "copy-of-locomo10.json",
            "longmemeval_oracle.json",
            "vendor/longmemeval_s_cleaned.json",
        ):
            with self.subTest(probe=probe):
                self.assertEqual(dataset_filename_offenders([probe]), [probe])

    def test_no_working_tree_file_under_the_package_carries_a_dataset_filename(self) -> None:
        present = [
            str(path.relative_to(ROOT).as_posix())
            for path in (ROOT / "scripts" / "benchmarks").rglob("*")
            if path.is_file() and "__pycache__" not in path.parts
        ]
        self.assertEqual(dataset_filename_offenders(present), [])

    def test_the_tracked_file_size_guard_is_still_five_mebibytes(self) -> None:
        # Read the constant rather than restating it, so lowering the guard
        # fails here as loudly as raising it.
        self.assertEqual(MAX_TRACKED_FILE_BYTES, 5 * 1024 * 1024)

    def test_no_tracked_file_is_anywhere_near_the_guard(self) -> None:
        oversized = [
            path
            for path in tracked_files()
            if (ROOT / path).is_file() and (ROOT / path).stat().st_size > MAX_TRACKED_FILE_BYTES
        ]
        self.assertEqual(oversized, [])

    def test_the_benchmark_package_tracks_no_data_file(self) -> None:
        tracked_here = [
            path
            for path in tracked_files()
            if path.startswith("scripts/benchmarks/") and not path.endswith((".py", ".md"))
        ]
        # Only the small config templates may be tracked here, and each of them
        # is a handful of lines of settings, never dataset content.
        for path in tracked_here:
            with self.subTest(path=path):
                self.assertTrue(path.startswith("scripts/benchmarks/configs/"))
                self.assertLess((ROOT / path).stat().st_size, 8 * 1024)


class CacheIsOutsideTheRepositoryTests(unittest.TestCase):
    """Assertion 2: the cache directory can never resolve inside the tree."""

    def test_the_resolver_refuses_a_directory_under_the_repository(self) -> None:
        for candidate in (
            ROOT,
            ROOT / "reports",
            ROOT / "tests" / "fixtures" / "bench",
            ROOT / "jarvis" / ".." / "jarvis" / "cache",
        ):
            with self.subTest(candidate=candidate):
                with self.assertRaises(cache.DatasetError) as caught:
                    cache.resolve_cache_dir(candidate)
                self.assertEqual(caught.exception.code, "cache_inside_repository")

    def test_ensure_dataset_refuses_before_it_touches_the_network(self) -> None:
        spec = cache.DATASETS["longmemeval_s"]
        with self.assertRaises(cache.DatasetError) as caught:
            cache.ensure_dataset(
                spec,
                cache_dir=ROOT / "reports" / "cache",
                env={},
                allow_fetch=True,
                fetcher=lambda *_: self.fail("no fetch may be attempted"),
            )
        self.assertEqual(caught.exception.code, "cache_inside_repository")

    def test_the_default_cache_is_outside_the_repository_on_this_host(self) -> None:
        resolved = cache.resolve_cache_dir()
        self.assertNotEqual(resolved, ROOT)
        self.assertNotIn(ROOT, resolved.parents)


class SealedFixturesKeepTheirStructuralPropertiesTests(unittest.TestCase):
    """Assertion 3: structural properties, deliberately **not** digests.

    Pinning fixture digests here would put six sealed fixtures on a reseal
    treadmill inside a non-sealed test whose owner has no part in the reseal.
    Digest integrity stays with ``claude-reseal-runtime-pins.py``'s own
    invariant, which is the tool that actually performs the cascade.
    """

    def _fixtures(self) -> list[Path]:
        found: list[Path] = []
        for directory in SEALED_FIXTURE_DIRECTORIES:
            if directory.is_dir():
                found.extend(sorted(directory.glob("*.json")))
        return found

    def test_there_are_sealed_fixtures_to_check(self) -> None:
        self.assertGreater(len(self._fixtures()), 0)

    def test_declared_safety_properties_are_still_true(self) -> None:
        declared = 0
        for path in self._fixtures():
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                continue
            for key in ("fictional_only", "public_safe"):
                if key in payload:
                    declared += 1
                    with self.subTest(fixture=path.name, key=key):
                        self.assertIs(payload[key], True)
        self.assertGreaterEqual(declared, 2)

    def test_no_sealed_fixture_names_a_public_benchmark(self) -> None:
        markers = (
            "longmemeval",
            "locomo",
            "haystack_session_ids",
            "blip_caption",
            "adversarial_answer",
            "ruler_style",
        )
        for path in self._fixtures():
            text = path.read_text(encoding="utf-8").casefold()
            for marker in markers:
                with self.subTest(fixture=path.name, marker=marker):
                    self.assertNotIn(marker, text)


class DatasetValuesAreAbsentTests(unittest.TestCase):
    """Assertion 4, and the honest statement that it is inert without a cache.

    This is the strongest assertion in the file and the one that cannot run in
    CI, because CI has no dataset cache. Two compensations carry it instead:
    ``run.py fetch`` invokes the same scan and refuses to report success if it
    fails, and the date of its last real run is written into
    ``docs/BENCHMARKS.md`` so a reader can see when it last happened rather
    than assume a green CI covered it.
    """

    def test_no_sampled_dataset_value_appears_in_the_tracked_tree(self) -> None:
        checked = 0
        for name, spec in sorted(cache.DATASETS.items()):
            try:
                handle = cache.ensure_dataset(spec, env={}, allow_fetch=False, scored=False)
            except cache.DatasetError as exc:
                if exc.code in {"dataset_not_cached", "cache_inside_repository", "licence_not_cached"}:
                    continue
                raise
            checked += 1
            scan = cache.scan_cached_dataset_for_leakage(handle, root=ROOT)
            with self.subTest(dataset=name):
                self.assertGreater(scan.values_sampled, 0)
                self.assertTrue(scan.full_file, "the scan must cover the whole file")
                self.assertEqual(list(scan.findings), [])
        if checked == 0:
            self.skipTest(
                "no dataset is cached on this host, so the value scan is inert here; "
                "it runs after every `run.py fetch`, and the date of its last real "
                "run is recorded in docs/BENCHMARKS.md"
            )

    def test_the_scan_would_catch_a_planted_value_end_to_end(self) -> None:
        """The compensating proof, and it runs the **sampler**, not just the comparator.

        The previous version handed a phrase it had chosen straight to
        ``dataset_value_leakage_findings``.  That showed the comparator worked
        and said nothing about whether the sampler would ever produce that
        phrase -- which, with an 8 MiB prefix and no unescaping, it usually
        would not.  This writes a dataset, samples it, and scans with what the
        sampler actually returned.
        """

        forms = (
            "The Harrier box calibration offset was revised again in March.",
            'She said "the gate count is four" during the survey call today.',
            "Le reglage du Harrier box a été modifie en mars dernier ici.",
            "The Harrier box offset changed.\nIt was revised again in March.",
        )
        with tempfile.TemporaryDirectory() as workspace:
            root = Path(workspace)
            dataset = root / "dataset.json"
            dataset.write_text(
                json.dumps([{"question": form} for form in forms], ensure_ascii=True),
                encoding="utf-8",
            )
            values = cache.sample_dataset_values(dataset)
            self.assertEqual(len(values), len(forms))
            for form in forms:
                with self.subTest(form=form[:32]):
                    planted = root / "fixture.md"
                    planted.write_text(f"before\n{form}\nafter\n", encoding="utf-8")
                    findings = cache.dataset_value_leakage_findings(
                        values, root=root, files=[planted]
                    )
                    self.assertEqual(len(findings), 1)
                    self.assertNotIn(form[:32], findings[0])

    def test_the_scan_reads_the_whole_file_rather_than_a_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as workspace:
            dataset = Path(workspace) / "dataset.json"
            tail = "the final instance question about the Millrace weir gate count"
            dataset.write_text(
                json.dumps(
                    [{"text": f"padding sentence number {index} " * 10} for index in range(300)]
                    + [{"question": tail}]
                ),
                encoding="utf-8",
            )
            values = cache.sample_dataset_values(dataset, sample_size=4096)
        self.assertIn(tail, values)

    def test_the_runners_may_still_name_the_datasets_own_field_names(self) -> None:
        # scripts/benchmarks/*.py must contain the field names in order to parse
        # or deliberately ignore them; the assertion is about dataset *values*.
        source = (BENCHMARK_PACKAGE / "longmemeval.py").read_text(encoding="utf-8")
        self.assertIn("haystack_sessions", source)


class ReportsCarryNoCaseTextTests(unittest.TestCase):
    """Assertion 5: a published row is ids, enums, booleans and numbers."""

    def test_the_row_key_set_is_closed(self) -> None:
        with self.assertRaises(report.ReportError) as caught:
            report.validate_row({"question": "what is the answer?"})
        self.assertEqual(caught.exception.code, "row_key_not_allowed")

    def test_a_string_longer_than_the_limit_fails(self) -> None:
        report.validate_row({"case_id": "x" * report.MAX_ROW_STRING_CHARS})
        with self.assertRaises(report.ReportError) as caught:
            report.validate_row({"case_id": "x" * (report.MAX_ROW_STRING_CHARS + 1)})
        self.assertEqual(caught.exception.code, "row_carries_case_text")

    def test_no_row_key_could_hold_prose(self) -> None:
        for key in report.ROW_KEYS:
            with self.subTest(key=key):
                self.assertNotIn(key, {"question", "answer", "reply", "gold", "content", "text"})

    def test_a_locomo_row_carries_no_dataset_text(self) -> None:
        instance = driver.Instance(
            instance_id="conv-9",
            sessions=(),
            cases=(
                driver.Case(
                    "conv-9#0",
                    "What did the speaker say about the ledger?",
                    "a private-sounding answer",
                    "1",
                    metadata={"qa_index": 0, "sample_id": "conv-9"},
                ),
            ),
        )
        outcome = driver.Outcome("conv-9#0", "some reply text", "claude-cli:claude-sonnet-4-5",
                                 "complete", 0, 5)
        row = locomo.score_row(instance, instance.cases[0], outcome)
        report.validate_row(row)
        serialised = json.dumps(row)
        for fragment in ("ledger", "private-sounding", "some reply text"):
            self.assertNotIn(fragment, serialised)


class PublicNumbersNeverGateTests(unittest.TestCase):
    """Assertion 6: the dependency runs one way, and no gate can reach it."""

    def _python_sources(self, *directories: Path) -> list[Path]:
        found: list[Path] = []
        for directory in directories:
            if directory.is_dir():
                found.extend(sorted(directory.rglob("*.py")))
        return found

    def test_no_product_module_imports_the_benchmark_package(self) -> None:
        offenders = [
            path.relative_to(ROOT).as_posix()
            for path in self._python_sources(ROOT / "jarvis")
            if "scripts.benchmarks" in path.read_text(encoding="utf-8")
            or "scripts/benchmarks" in path.read_text(encoding="utf-8")
        ]
        self.assertEqual(offenders, [])

    def test_no_sealed_evaluation_imports_the_benchmark_package(self) -> None:
        sealed = [
            ROOT / "tests" / "test_memory_retrieval_holdout_v3.py",
            ROOT / "tests" / "test_memory_retrieval_holdout_v5.py",
            ROOT / "tests" / "test_memory_graph_holdout_v4.py",
            ROOT / "tests" / "test_strategy_transfer_trial_eval.py",
            ROOT / "tests" / "test_long_horizon_eval.py",
        ]
        present = [path for path in sealed if path.exists()]
        self.assertGreaterEqual(len(present), 3)
        for path in present:
            with self.subTest(sealed=path.name):
                self.assertNotIn("benchmarks", path.read_text(encoding="utf-8"))

    def test_only_the_driver_reaches_into_the_product(self) -> None:
        for path in sorted(BENCHMARK_PACKAGE.glob("*.py")):
            text = path.read_text(encoding="utf-8")
            with self.subTest(module=path.name):
                if path.name in {"driver.py", "run.py"}:
                    continue
                self.assertNotIn("from jarvis", text)
                self.assertNotIn("import jarvis", text)

    def test_the_benchmark_docs_carry_no_superlative(self) -> None:
        text = (ROOT / "docs" / "BENCHMARKS.md").read_text(encoding="utf-8")
        self.assertEqual(report.banned_claim_findings(text), [])

    def test_the_benchmark_docs_state_that_the_numbers_do_not_gate(self) -> None:
        text = (ROOT / "docs" / "BENCHMARKS.md").read_text(encoding="utf-8")
        self.assertIn("They are not release gates.", text)
        self.assertIn("non-commercial", text)
        self.assertIn("Leakage check last run against a fetched dataset", text)

    def test_the_generated_stress_uses_no_external_corpus(self) -> None:
        source = (BENCHMARK_PACKAGE / "ruler_style.py").read_text(encoding="utf-8")
        self.assertNotIn("http://", source.replace("https://", ""))
        self.assertNotIn("urllib", source)
        sample = ruler_style.generate_sample(task="niah_single", length=1024, depth=0.5, seed=1)
        self.assertTrue(sample.context)

    def test_the_longmemeval_runner_never_synthesises_a_governed_command(self) -> None:
        source = (BENCHMARK_PACKAGE / "longmemeval.py").read_text(encoding="utf-8")
        self.assertNotIn('"Remember this project fact:', source)
        instance = longmemeval.to_instance(
            {
                "question_id": "q1",
                "question_type": "information-extraction",
                "question": "what?",
                "answer": "a",
                "haystack_sessions": [[{"role": "user", "content": "the value is a"}]],
            }
        )
        for session in instance.sessions:
            for turn in session.turns:
                self.assertNotIn("Remember this project fact:", turn.content)


if __name__ == "__main__":  # pragma: no cover - manual invocation
    unittest.main()
