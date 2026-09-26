"""Runtime coverage may expand after extraction; evaluation data may not change."""
import hashlib
import importlib.util
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "reseal_runtime_pins.py"
SPEC = importlib.util.spec_from_file_location("reseal_registry_subject", SCRIPT)
assert SPEC and SPEC.loader
reseal = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(reseal)


class RuntimeRegistryResealTests(unittest.TestCase):
    def test_new_registered_body_is_pinned_without_dropping_existing_coverage(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "jarvis").mkdir()
            (root / "jarvis" / "memory.py").write_bytes(b"# facade\n")
            (root / "jarvis" / "memory_governance.py").write_bytes(b"# body\n")
            before = {
                "runtime_sha256": {"jarvis/memory.py": "0" * 64},
                "thresholds": {"leakage": 0},
                "cases": [{"expected": "abstain"}],
            }
            files = ("jarvis/memory_governance.py",)
            after = reseal.reseal_per_file_pin(before, root, runtime_files=files)
            self.assertEqual(set(after["runtime_sha256"]), {
                "jarvis/memory.py", "jarvis/memory_governance.py",
            })
            self.assertEqual(after["runtime_sha256"][files[0]],
                             hashlib.sha256(b"# body\n").hexdigest())
            self.assertEqual(after["thresholds"], before["thresholds"])
            self.assertEqual(after["cases"], before["cases"])
            self.assertEqual(before["runtime_sha256"], {"jarvis/memory.py": "0" * 64})
            self.assertEqual(reseal.check_invariant("registered bodies", before, after), 2)
            self.assertEqual(reseal.reseal_per_file_pin(after, root, runtime_files=files), after)

    def test_missing_registered_file_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(SystemExit, "pinned file missing"):
                reseal.reseal_per_file_pin(
                    {"runtime_sha256": {}}, Path(directory),
                    runtime_files=("jarvis/memory_governance.py",),
                )

    def test_noncanonical_or_out_of_scope_registered_paths_are_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            for name in (
                "../outside.py", "jarvis/../outside.py", "/outside.py",
                "jarvis\\body.py", "jarvis//body.py", "scripts/body.py",
                "jarvis/body.txt",
            ):
                with self.subTest(path=name), self.assertRaises(SystemExit):
                    reseal.reseal_per_file_pin(
                        {"runtime_sha256": {}}, Path(directory), runtime_files=(name,),
                    )

    def test_unknown_holdout_family_does_not_invent_runtime_coverage(self):
        self.assertEqual(reseal.registered_runtime_files("unknown_fixture"), ())


if __name__ == "__main__":
    unittest.main()
