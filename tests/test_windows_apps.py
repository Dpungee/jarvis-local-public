from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from jarvis.windows_apps import InstalledApplication, WindowsAppController


TEMP_ROOT = Path(__file__).resolve().parent / ".tmp"
TEMP_ROOT.mkdir(exist_ok=True)


class WindowsAppControllerTests(unittest.TestCase):
    def setUp(self):
        self.root = TEMP_ROOT / f"windows-apps-{os.getpid()}-{self._testMethodName}"
        if self.root.exists():
            shutil.rmtree(self.root)
        self.profile = self.root / "profile"
        self.data = self.root / "data"
        self.apps = self.root / "apps"
        self.profile.mkdir(parents=True)
        self.data.mkdir()
        self.apps.mkdir()
        self.photoshop = self.apps / "Photoshop.exe"
        self.photoshop.write_bytes(b"synthetic photoshop executable")
        self.catalog = lambda: [
            InstalledApplication("Adobe Photoshop 2026", self.photoshop, "test"),
        ]

    def tearDown(self):
        shutil.rmtree(self.root)

    def controller(self, **kwargs):
        return WindowsAppController(
            self.profile,
            self.data,
            catalog=self.catalog,
            com_server_lookup=lambda _prog_id: self.photoshop.resolve(),
            **kwargs,
        )

    def test_catalog_filters_and_launches_exact_app_without_secrets(self):
        process = SimpleNamespace(pid=4321)
        calls = []

        def launcher(*args, **kwargs):
            calls.append((args, kwargs))
            return process

        controller = self.controller(launcher=launcher)
        listed = controller.list_apps("photo")
        self.assertEqual(listed["count"], 1)
        approved = controller.launch_snapshot("Photoshop")
        with patch.dict(
            os.environ,
            {"OPENAI_API_KEY": "synthetic-secret", "SYSTEMROOT": r"C:\Windows"},
            clear=True,
        ):
            launched = controller.launch_app("Photoshop", approved=approved)
        self.assertEqual(launched["pid"], 4321)
        positional, keyword = calls[0]
        self.assertEqual(positional[0], [str(self.photoshop.resolve())])
        self.assertNotIn("OPENAI_API_KEY", keyword["env"])
        self.assertEqual(keyword["env"]["SYSTEMROOT"], r"C:\Windows")

    def test_shells_and_ambiguous_apps_fail_closed(self):
        shell = self.apps / "cmd.exe"
        shell.write_bytes(b"shell")
        controller = WindowsAppController(
            self.profile,
            self.data,
            catalog=lambda: [InstalledApplication("Command Prompt", shell, "test")],
        )
        with self.assertRaises(PermissionError):
            controller.launch_snapshot("Command Prompt")

        controller = WindowsAppController(
            self.profile,
            self.data,
            catalog=lambda: [
                InstalledApplication("Example One", self.photoshop, "test"),
                InstalledApplication("Example Two", self.photoshop, "test"),
            ],
        )
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            controller.launch_snapshot("Example")

    def test_packaged_start_app_is_snapshotted_and_launched_exactly(self):
        activation_id = "Microsoft.WindowsCalculator_8wekyb3d8bbwe!App"
        explorer = self.apps / "explorer.exe"
        explorer.write_bytes(b"synthetic explorer")
        calls = []

        def launcher(*args, **kwargs):
            calls.append((args, kwargs))
            return SimpleNamespace(pid=9876)

        controller = WindowsAppController(
            self.profile,
            self.data,
            catalog=lambda: [
                InstalledApplication(
                    "Calculator", None, "start-apps", activation_id=activation_id
                )
            ],
            launcher=launcher,
        )
        approved = controller.launch_snapshot("Calculator")
        self.assertEqual(approved["activation_id"], activation_id)

        with patch("jarvis.windows_apps._regular_file", return_value=explorer.resolve()):
            launched = controller.launch_app("Calculator", approved=approved)

        self.assertEqual(launched["pid"], 9876)
        self.assertEqual(launched["activation_id"], activation_id)
        self.assertEqual(
            calls[0][0][0],
            [str(explorer.resolve()), f"shell:AppsFolder\\{activation_id}"],
        )

    def test_public_url_launch_is_bound_to_the_approved_url(self):
        explorer = self.apps / "explorer.exe"
        explorer.write_bytes(b"synthetic explorer")
        calls = []

        def launcher(*args, **kwargs):
            calls.append((args, kwargs))
            return SimpleNamespace(pid=2468)

        controller = self.controller(launcher=launcher)
        approved = controller.url_snapshot("https://example.com/weather?q=10001")
        with patch("jarvis.windows_apps._regular_file", return_value=explorer.resolve()):
            launched = controller.open_url(
                "https://example.com/weather?q=10001", approved=approved
            )
        self.assertTrue(launched["opened"])
        self.assertEqual(
            calls[0][0][0],
            [str(explorer.resolve()), "https://example.com/weather?q=10001"],
        )

        with self.assertRaisesRegex(PermissionError, "changed"):
            controller.open_url("https://example.com/other", approved=approved)

    def test_photoshop_background_removal_is_target_bound_and_source_preserving(self):
        source = self.profile / "Pictures" / "subject.jpg"
        output = self.profile / "Pictures" / "subject-transparent.png"
        source.parent.mkdir()
        source.write_bytes(b"synthetic image bytes")
        observed = {}

        def runner(argv, **_kwargs):
            command = base64.b64decode(argv[-1]).decode("utf-16le")
            observed["command"] = command
            encoded_jsx = re.search(r"FromBase64String\('([^']+)'\)", command).group(1)
            jsx = base64.b64decode(encoded_jsx).decode("utf-8")
            observed["jsx"] = jsx
            encoded_path = re.search(r"var outputFile = new File\((\".*\")\);", jsx).group(1)
            temporary = Path(json.loads(encoded_path))
            self.assertTrue(temporary.parent.is_dir(), repr(str(temporary)))
            temporary.write_bytes(b"\x89PNG\r\n\x1a\nsynthetic pixels")
            return subprocess.CompletedProcess(argv, 0, "", "")

        controller = self.controller(runner=runner)
        approved = controller.photoshop_snapshot(
            "Pictures/subject.jpg",
            "Pictures/subject-transparent.png",
        )
        result = controller.remove_photoshop_background(
            "Pictures/subject.jpg",
            "Pictures/subject-transparent.png",
            approved=approved,
        )
        self.assertTrue(output.is_file())
        self.assertTrue(result["source_unchanged"])
        self.assertIsNone(result["backup"])
        self.assertIn("removeBackground", observed["jsx"])
        self.assertIn("Photoshop.Application.200", observed["command"])
        self.assertEqual(source.read_bytes(), b"synthetic image bytes")

        source.write_bytes(b"changed after approval")
        with self.assertRaisesRegex(PermissionError, "changed"):
            controller.remove_photoshop_background(
                "Pictures/subject.jpg",
                "Pictures/another.png",
                approved=approved,
            )

    def test_photoshop_output_must_be_png_and_overwrite_is_explicit(self):
        pictures = self.profile / "Pictures"
        pictures.mkdir()
        (pictures / "subject.jpg").write_bytes(b"image")
        (pictures / "existing.png").write_bytes(b"existing")
        controller = self.controller()
        with self.assertRaisesRegex(ValueError, "png"):
            controller.photoshop_snapshot("Pictures/subject.jpg", "Pictures/output.jpg")
        with self.assertRaises(FileExistsError):
            controller.photoshop_snapshot("Pictures/subject.jpg", "Pictures/existing.png")
        snapshot = controller.photoshop_snapshot(
            "Pictures/subject.jpg", "Pictures/existing.png", overwrite=True
        )
        self.assertTrue(snapshot["output_exists"])
        self.assertIn("existing_output_sha256", snapshot)


if __name__ == "__main__":
    unittest.main()
