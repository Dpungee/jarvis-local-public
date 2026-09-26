"""Calibration copies preserve keyed history and cannot regenerate missing keys."""
from pathlib import Path
import tempfile
import unittest

from jarvis.calibration_report import read_only_memory
from jarvis.memory import Memory
from jarvis.memory_spine import KEY_SIDECAR_SUFFIX, SpineError


class CalibrationSpineCopyTests(unittest.TestCase):
    def test_existing_key_and_source_bytes_are_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "memory.db"
            with Memory(database) as original:
                original_version = original.db.execute("PRAGMA user_version").fetchone()[0]
            key = Path(str(database) + KEY_SIDECAR_SUFFIX)
            before_database, before_key = database.read_bytes(), key.read_bytes()
            with read_only_memory(database) as memory:
                self.assertEqual(memory.db.execute("PRAGMA user_version").fetchone()[0], original_version)
            self.assertEqual(database.read_bytes(), before_database)
            self.assertEqual(key.read_bytes(), before_key)

    def test_missing_established_key_fails_closed_without_replacement(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "memory.db"
            with Memory(database):
                pass
            key = Path(str(database) + KEY_SIDECAR_SUFFIX)
            key.unlink()
            before_database = database.read_bytes()
            with self.assertRaises(SpineError):
                with read_only_memory(database):
                    self.fail("missing key was accepted")
            self.assertFalse(key.exists())
            self.assertEqual(database.read_bytes(), before_database)

    def test_malformed_key_fails_closed_without_source_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "memory.db"
            with Memory(database):
                pass
            key = Path(str(database) + KEY_SIDECAR_SUFFIX)
            key.write_bytes(b"not-a-valid-key")
            before_database = database.read_bytes()
            with self.assertRaises(SpineError):
                with read_only_memory(database):
                    self.fail("malformed key was accepted")
            self.assertEqual(key.read_bytes(), b"not-a-valid-key")
            self.assertEqual(database.read_bytes(), before_database)
