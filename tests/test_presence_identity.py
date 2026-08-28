from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jarvis.presence_identity import (
    normalized_install_path,
    presence_installation_id,
    presence_process_identity,
)


class PresenceIdentityTests(unittest.TestCase):
    def test_installation_id_binds_source_and_python(self):
        with tempfile.TemporaryDirectory(prefix="jarvis-presence-identity-") as directory:
            root = Path(directory)
            source_a = root / "source-a"
            source_b = root / "source-b"
            python_a = root / "python-a.exe"
            python_b = root / "python-b.exe"
            for path in (source_a, source_b):
                path.mkdir()
            python_a.touch()
            python_b.touch()

            base = presence_installation_id(
                source_root=source_a, python_executable=python_a
            )
            self.assertRegex(base, r"^[0-9a-f]{64}$")
            self.assertEqual(
                base,
                presence_installation_id(
                    source_root=source_a, python_executable=python_a
                ),
            )
            self.assertNotEqual(
                base,
                presence_installation_id(
                    source_root=source_b, python_executable=python_a
                ),
            )
            self.assertNotEqual(
                base,
                presence_installation_id(
                    source_root=source_a, python_executable=python_b
                ),
            )

    def test_normalized_path_is_absolute(self):
        normalized = normalized_install_path(Path("jarvis") / "presence.py")
        self.assertTrue(Path(normalized).is_absolute())

    def test_process_identity_reports_manual_mode_and_exact_epoch(self):
        epoch = "a" * 32
        with patch.dict(os.environ, {"JARVIS_PRESENCE_LAUNCH_MODE": "manual"}):
            identity = presence_process_identity(epoch)
        self.assertEqual(identity["runtime_epoch"], epoch)
        self.assertEqual(identity["launch_mode"], "manual")
        self.assertEqual(identity["process_id"], os.getpid())

    def test_unrecognized_launch_mode_fails_to_direct(self):
        with patch.dict(os.environ, {"JARVIS_PRESENCE_LAUNCH_MODE": "spoofed"}):
            identity = presence_process_identity("b" * 32)
        self.assertEqual(identity["launch_mode"], "direct")


if __name__ == "__main__":
    unittest.main()
