"""The repository-owned dependency locks stay consistent with pyproject.toml and CI."""
from __future__ import annotations

import importlib.util
import re
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REQUIREMENTS = ROOT / "requirements"
LOCKS = {
    "build-backend": REQUIREMENTS / "build-backend.txt",
    "runtime-documents": REQUIREMENTS / "runtime-documents.txt",
    "ci-tools": REQUIREMENTS / "ci-tools.txt",
}
QUALITY_TOOLS = {"coverage", "build", "pip-audit", "ruff", "bandit"}


def _lock_module():
    spec = importlib.util.spec_from_file_location(
        "lock_dependencies", ROOT / "scripts" / "lock_dependencies.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _version_key(version: str) -> tuple[int, ...]:
    parts: list[int] = []
    for piece in version.split("."):
        digits = re.match(r"\d+", piece)
        if digits is None:
            break
        parts.append(int(digits.group(0)))
    return tuple(parts)


def _satisfies(version: str, specifier: str) -> bool:
    if not specifier.strip():
        return True
    have = _version_key(version)
    for clause in specifier.split(","):
        match = re.fullmatch(r"\s*(==|>=|<=|<|>)\s*([0-9A-Za-z.\-_]+)\s*", clause)
        if match is None:
            raise AssertionError(f"unsupported specifier clause {clause!r}")
        operator, target = match.groups()
        want = _version_key(target)
        width = max(len(have), len(want))
        left = have + (0,) * (width - len(have))
        right = want + (0,) * (width - len(want))
        passed = {
            "==": left == right,
            ">=": left >= right,
            "<=": left <= right,
            "<": left < right,
            ">": left > right,
        }[operator]
        if not passed:
            return False
    return True


def _requirements(entries) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for entry in entries:
        match = re.fullmatch(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*([^;]*?)\s*(?:;.*)?", str(entry))
        assert match is not None, entry
        parsed[_normalize(match.group(1))] = match.group(2).strip()
    return parsed


class DependencyLockTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.module = _lock_module()
        cls.pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        cls.locks = {name: cls.module.parse_lock(path) for name, path in LOCKS.items()}

    def test_every_lock_parses_and_pins_at_least_one_hash_per_release(self) -> None:
        for name, entries in self.locks.items():
            with self.subTest(lock=name):
                self.assertTrue(entries, "lock is empty")
                for package, (version, hashes) in entries.items():
                    self.assertEqual(package, _normalize(package))
                    self.assertTrue(version)
                    self.assertGreaterEqual(len(hashes), 1, package)

    def test_runtime_lock_covers_the_runtime_and_documents_requirements(self) -> None:
        project = self.pyproject["project"]
        required = _requirements(project["dependencies"])
        required.update(_requirements(project["optional-dependencies"]["documents"]))
        lock = self.locks["runtime-documents"]
        for name, specifier in required.items():
            with self.subTest(package=name):
                self.assertIn(name, lock, f"{name} is not pinned in requirements/runtime-documents.txt")
                self.assertTrue(
                    _satisfies(lock[name][0], specifier),
                    f"{name}=={lock[name][0]} does not satisfy {specifier!r}",
                )

    def test_build_backend_lock_pins_pip_and_a_supported_setuptools(self) -> None:
        lock = self.locks["build-backend"]
        self.assertIn("pip", lock)
        backend = _requirements(self.pyproject["build-system"]["requires"])
        self.assertIn("setuptools", backend)
        self.assertIn("setuptools", lock)
        self.assertTrue(_satisfies(lock["setuptools"][0], backend["setuptools"]))

    def test_ci_tools_lock_contains_the_quality_tooling(self) -> None:
        self.assertEqual(QUALITY_TOOLS - set(self.locks["ci-tools"]), set())

    def test_ci_installs_only_through_the_locks(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        lines = workflow.splitlines()
        allowed = (
            re.compile(r".*--require-hashes -r requirements/(build-backend|runtime-documents|ci-tools)\.txt\s*$"),
            re.compile(r".*pip install --disable-pip-version-check --no-deps --no-build-isolation -e \.\s*$"),
            # The locally built artifact is installed offline against locked dependencies.
            re.compile(r"\s*& \$python -m pip install --disable-pip-version-check --no-index --no-deps --no-build-isolation \$sdist\s*$"),
        )
        installs = [index for index, line in enumerate(lines) if "pip install" in line]
        self.assertTrue(installs, "no pip install steps found")
        for index in installs:
            line = lines[index]
            preceding = " ".join(lines[max(0, index - 3): index])
            if "intentionally unpinned" in preceding:
                continue
            with self.subTest(line=line.strip()):
                self.assertTrue(any(pattern.match(line) for pattern in allowed), line)
        self.assertNotIn('-e ".[documents]"', workflow)
        self.assertIn("python -m build --no-isolation", workflow)

    def test_shared_packages_are_pinned_identically_across_locks(self) -> None:
        seen: dict[str, tuple[str, str]] = {}
        for lock_name, entries in self.locks.items():
            for package, (version, _hashes) in entries.items():
                previous = seen.setdefault(package, (version, lock_name))
                self.assertEqual(
                    previous[0],
                    version,
                    f"{package} is {previous[0]} in {previous[1]} but {version} in {lock_name}",
                )

    def test_ci_measures_worker_processes_without_reducing_the_gate(self) -> None:
        run = self.pyproject["tool"]["coverage"]["run"]
        self.assertEqual(run["source"], ["jarvis", "scripts"])
        self.assertTrue(run["branch"])
        self.assertTrue(run["parallel"])
        self.assertEqual(run["patch"], ["subprocess"])
        self.assertNotIn("omit", run)
        workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        self.assertLess(workflow.index("coverage combine"), workflow.index("coverage report --fail-under=75"))
        self.assertIn("Subprocess coverage combine failed", workflow)

    def test_every_lock_was_validated_for_the_oldest_supported_python(self) -> None:
        requires = self.pyproject["project"]["requires-python"]
        oldest = re.search(r">=\s*(\d+\.\d+)", requires).group(1)
        for name, lock_path in LOCKS.items():
            with self.subTest(lock=name):
                header = lock_path.read_text(encoding="utf-8").splitlines()[1]
                self.assertIn(f"validated for Python {oldest}+", header)

    def test_ci_test_job_install_step_stops_on_the_first_failure(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        step = "      - name: Install Jarvis from the repository-owned locks\n        shell: bash\n"
        self.assertIn(step, workflow)

    def test_lock_parser_rejects_tampered_or_incomplete_entries(self) -> None:
        import tempfile

        module = self.module
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "broken.txt"
            path.write_text("example==1.0 \\\n", encoding="utf-8")
            with self.assertRaises(SystemExit):
                module.parse_lock(path)
            path.write_text("example==1.0 \\\n    --hash=sha256:" + "0" * 63 + "\n", encoding="utf-8")
            with self.assertRaises(SystemExit):
                module.parse_lock(path)
            path.write_text(
                "example==1.0 \\\n    --hash=sha256:" + "a" * 64 + " \\\n    --hash=sha256:" + "a" * 64 + "\n",
                encoding="utf-8",
            )
            with self.assertRaises(SystemExit):
                module.parse_lock(path)
            path.write_text("Example==1.0 \\\n    --hash=sha256:" + "b" * 64 + "\n", encoding="utf-8")
            with self.assertRaises(SystemExit):
                module.parse_lock(path)
            path.write_text("example==1.0 \\\n    --hash=sha256:" + "c" * 64 + "\n", encoding="utf-8")
            self.assertEqual(module.parse_lock(path), {"example": ("1.0", ["c" * 64])})

    def test_generator_refuses_non_pypi_urls_and_unpinned_lines(self) -> None:
        import tempfile

        module = self.module
        with self.assertRaises(SystemExit):
            module._fetch_json("https://example.com/pypi/x/json")
        with tempfile.TemporaryDirectory() as temporary:
            pins = Path(temporary) / "pins.txt"
            pins.write_text("requests>=2\n", encoding="utf-8")
            with self.assertRaises(SystemExit):
                module.read_pins(pins)
            pins.write_text("# comment\n\nlocal @ file:///tmp/x\nRequests==2.0.0\n", encoding="utf-8")
            self.assertEqual(module.read_pins(pins), [("requests", "2.0.0")])


if __name__ == "__main__":
    unittest.main()
