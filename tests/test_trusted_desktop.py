from __future__ import annotations

import json
import hashlib
import os
import shutil
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from jarvis.config import Config
from jarvis.desktop import WindowsDesktopController, resolve_computer_path
from jarvis.memory import Memory
from jarvis.tools import ToolBox


TEMP_ROOT = Path(__file__).resolve().parent / ".tmp"
TEMP_ROOT.mkdir(exist_ok=True)


class TrustedDesktopTests(unittest.TestCase):
    def setUp(self):
        self.test_dir = TEMP_ROOT / f"desktop-{os.getpid()}-{self._testMethodName}"
        if self.test_dir.exists():
            shutil.rmtree(self.test_dir)
        self.profile = self.test_dir / "profile"
        self.workspace = self.test_dir / "workspace"
        self.data_dir = self.test_dir / "data"
        self.profile.mkdir(parents=True)
        self.workspace.mkdir()
        self.data_dir.mkdir()
        self.config = replace(
            Config.load(),
            workspace=self.workspace,
            data_dir=self.data_dir,
            execution_mode="trusted-host",
            computer_access="trusted-desktop",
            computer_root=self.profile,
            autonomy="autonomous",
        )
        self.memory = Memory(self.data_dir / "desktop.db")
        self.toolbox = ToolBox(self.config, self.memory)

    def tearDown(self):
        self.memory.close()
        shutil.rmtree(self.test_dir)

    def execute_approved(self, name, arguments):
        scope = "conversation:1"
        with self.toolbox.approval_context(scope):
            blocked = json.loads(self.toolbox.execute(name, arguments))
        self.assertFalse(blocked["ok"])
        self.assertTrue(blocked["approval_required"])
        self.assertTrue(
            self.memory.decide_approval(blocked["approval_id"], True, ttl_hours=2)
        )
        with self.toolbox.approval_context(scope):
            return json.loads(self.toolbox.execute(name, arguments))

    def test_desktop_capabilities_require_explicit_trusted_mode(self):
        names = set(self.toolbox.tools)
        self.assertTrue({
            "computer_list_files", "computer_read_file", "computer_write_file",
            "computer_search_files", "computer_storage_report", "system_snapshot",
            "launch_artifact",
            "windows_list_apps", "windows_open_apps", "windows_launch_app",
            "windows_open_url",
            "desktop_active_window", "desktop_interact",
            "photoshop_remove_background",
        }.issubset(names))
        disabled = ToolBox(replace(self.config, computer_access="disabled"), self.memory)
        self.assertTrue({
            "computer_list_files", "computer_read_file", "computer_write_file",
            "computer_search_files", "computer_storage_report", "system_snapshot",
            "launch_artifact",
            "windows_list_apps", "windows_open_apps", "windows_launch_app",
            "windows_open_url",
            "desktop_active_window", "desktop_interact",
            "photoshop_remove_background",
        }.isdisjoint(disabled.tools))

    def test_profile_paths_block_escape_credentials_and_repository_controls(self):
        with self.assertRaises(PermissionError):
            resolve_computer_path(self.profile, self.profile.parent / "outside.txt")
        for path in (
            ".env", ".ssh/id_rsa", ".git/config", ".aws/credentials",
            ".jarvis-skills/learned-code-fix/SKILL.md",
            "data/codex-cli-home/state.json",
        ):
            with self.subTest(path=path), self.assertRaises(PermissionError):
                resolve_computer_path(self.profile, path)

    def test_computer_write_requires_fresh_hash_and_creates_backup(self):
        created = self.execute_approved(
            "computer_write_file", {"path": "Documents/note.txt", "content": "one"}
        )
        self.assertTrue(created["ok"])
        refused = self.execute_approved(
            "computer_write_file", {"path": "Documents/note.txt", "content": "two"}
        )
        self.assertFalse(refused["ok"])
        read = self.execute_approved(
            "computer_read_file", {"path": "Documents/note.txt"}
        )
        updated = self.execute_approved("computer_write_file", {
            "path": "Documents/note.txt",
            "content": "two",
            "expected_sha256": read["result"]["sha256"],
        })
        self.assertTrue(updated["ok"])
        self.assertTrue(Path(updated["result"]["backup"]).is_file())
        self.assertEqual(
            (self.profile / "Documents" / "note.txt").read_text(encoding="utf-8"),
            "two",
        )

    def test_snapshot_is_live_and_launch_uses_no_shell(self):
        devices = [{
            "disk_number": 0,
            "model": "Test SSD",
            "media_type": "Fixed hard disk media",
            "interface_type": "NVMe",
            "size_bytes": 1_000_000,
            "status": "OK",
        }]
        with patch("jarvis.desktop._physical_storage_devices", return_value=devices):
            snapshot = json.loads(self.toolbox.execute("system_snapshot", {}))
        self.assertTrue(snapshot["ok"])
        self.assertIn("logical_cpu_count", snapshot["result"])
        self.assertEqual(snapshot["result"]["physical_storage"]["device_count"], 1)
        self.assertEqual(snapshot["result"]["physical_storage"]["devices"], devices)

        open_apps = {
            "available": True,
            "applications": [{"name": "notepad.exe"}],
            "count": 1,
            "truncated": False,
            "window_titles_read": False,
            "window_content_read": False,
        }
        with patch("jarvis.tools.open_windows_applications", return_value=open_apps):
            visible = json.loads(self.toolbox.execute("windows_open_apps", {"limit": 20}))
        self.assertTrue(visible["ok"])
        self.assertEqual(visible["result"], open_apps)
        self.assertFalse(visible["result"]["window_titles_read"])
        self.assertFalse(visible["result"]["window_content_read"])

        script = self.workspace / "monitor.py"
        script.write_text("print('healthy')\n", encoding="utf-8")
        process = Mock(pid=4242)
        with patch("jarvis.tools.subprocess.Popen", return_value=process) as popen:
            launched = json.loads(self.toolbox.execute(
                "launch_artifact", {
                    "path": "monitor.py",
                    "expected_sha256": hashlib.sha256(script.read_bytes()).hexdigest(),
                }
            ))
        self.assertTrue(launched["ok"])
        self.assertEqual(launched["result"]["pid"], 4242)
        self.assertEqual(
            launched["result"]["sha256"],
            hashlib.sha256(script.read_bytes()).hexdigest(),
        )
        positional, keyword = popen.call_args
        self.assertIsInstance(positional[0], list)
        self.assertNotIn("shell", keyword)

    def test_artifact_launch_rejects_stale_digest_and_hard_links(self):
        script = self.workspace / "bounded.py"
        script.write_text("print('approved')\n", encoding="utf-8")
        approved = hashlib.sha256(script.read_bytes()).hexdigest()
        script.write_text("print('changed')\n", encoding="utf-8")

        with patch("jarvis.tools.subprocess.Popen") as popen:
            stale = json.loads(self.toolbox.execute("launch_artifact", {
                "path": "bounded.py",
                "expected_sha256": approved,
            }))
        self.assertFalse(stale["ok"])
        self.assertIn("differs from the expected", stale["error"])
        popen.assert_not_called()

        hard_link = self.workspace / "linked.py"
        try:
            os.link(script, hard_link)
        except OSError:
            self.skipTest("hard links are unavailable")
        with patch("jarvis.tools.subprocess.Popen") as popen:
            linked = json.loads(self.toolbox.execute(
                "launch_artifact", {"path": "linked.py"}
            ))
        self.assertFalse(linked["ok"])
        self.assertIn("non-linked", linked["error"])
        popen.assert_not_called()

    def test_verified_powerpoint_opens_in_registered_desktop_application(self):
        deck = self.workspace / "research_deck.pptx"
        deck.write_bytes(b"verified-test-deck")
        office = self.test_dir / "POWERPNT.EXE"
        office.write_bytes(b"verified-test-executable")
        process = Mock(pid=5150)

        with (
            patch.object(
                self.toolbox.windows_apps,
                "catalog",
                return_value=[SimpleNamespace(
                    name="Microsoft PowerPoint",
                    executable=office,
                )],
            ),
            patch("jarvis.tools.subprocess.Popen", return_value=process) as popen,
            patch("jarvis.tools.os.startfile") as startfile,
        ):
            opened = json.loads(self.toolbox.execute(
                "launch_artifact", {"path": "research_deck.pptx"}
            ))

        self.assertTrue(opened["ok"])
        self.assertTrue(opened["result"]["launched"])
        self.assertEqual(opened["result"]["path"], "research_deck.pptx")
        self.assertEqual(opened["result"]["viewer"], "Microsoft PowerPoint")
        self.assertEqual(opened["result"]["pid"], 5150)
        startfile.assert_not_called()
        positional, keyword = popen.call_args
        self.assertEqual(positional[0], [str(office), str(deck)])
        self.assertNotIn("shell", keyword)

        rejected = json.loads(self.toolbox.execute(
            "launch_artifact",
            {"path": "research_deck.pptx", "arguments": ["--unsafe"]},
        ))
        self.assertFalse(rejected["ok"])
        self.assertIn("do not accept launch arguments", rejected["error"])

    def test_storage_report_is_approval_gated_bounded_and_reads_no_content(self):
        downloads = self.profile / "Downloads"
        documents = self.profile / "Documents"
        protected = self.profile / ".ssh"
        downloads.mkdir()
        documents.mkdir()
        protected.mkdir()
        (downloads / "large.iso").write_bytes(b"x" * 5000)
        (documents / "notes.txt").write_bytes(b"y" * 500)
        (protected / "secret.bin").write_bytes(b"z" * 9000)

        report = self.execute_approved(
            "computer_storage_report", {"path": ".", "limit": 10}
        )

        self.assertTrue(report["ok"])
        result = report["result"]
        self.assertEqual(result["scanned_files"], 2)
        self.assertEqual(result["scanned_bytes"], 5500)
        self.assertFalse(result["content_read"])
        self.assertEqual(result["files_deleted"], 0)
        self.assertTrue(result["largest_files"][0]["path"].endswith("large.iso"))
        self.assertNotIn("secret.bin", json.dumps(result))

    def test_installed_app_and_photoshop_actions_use_exact_approved_snapshots(self):
        app_snapshot = {
            "application": "Adobe Photoshop 2026",
            "resolved_executable": r"C:\Program Files\Adobe\Photoshop.exe",
            "executable_bytes": 100,
            "executable_mtime_ns": 123,
            "source": "test",
        }
        photoshop_snapshot = {
            "input_path": "Pictures/subject.jpg",
            "output_path": "Pictures/subject.png",
            "overwrite": False,
            "resolved_input_path": str(self.profile / "Pictures" / "subject.jpg"),
            "input_bytes": 10,
            "input_sha256": "a" * 64,
            "resolved_output_path": str(self.profile / "Pictures" / "subject.png"),
            "output_exists": False,
            "photoshop_application": "Adobe Photoshop 2026",
            "photoshop_executable": r"C:\Program Files\Adobe\Photoshop.exe",
            "photoshop_executable_bytes": 100,
            "photoshop_executable_mtime_ns": 123,
            "photoshop_prog_id": "Photoshop.Application.200",
        }
        controller = Mock()
        controller.launch_snapshot.return_value = app_snapshot
        controller.photoshop_snapshot.return_value = photoshop_snapshot
        controller.launch_app.return_value = {"launched": True, "pid": 42}
        controller.remove_photoshop_background.return_value = {
            "output_path": photoshop_snapshot["resolved_output_path"],
            "source_unchanged": True,
        }
        self.toolbox.windows_apps = controller

        launched = self.execute_approved(
            "windows_launch_app", {"application": "Photoshop"}
        )
        self.assertTrue(launched["ok"])
        controller.launch_app.assert_called_once_with(
            "Photoshop", approved=app_snapshot
        )

        edited = self.execute_approved("photoshop_remove_background", {
            "input_path": "Pictures/subject.jpg",
            "output_path": "Pictures/subject.png",
        })
        self.assertTrue(edited["ok"])
        controller.remove_photoshop_background.assert_called_once_with(
            "Pictures/subject.jpg",
            "Pictures/subject.png",
            overwrite=False,
            approved=photoshop_snapshot,
        )

    def test_desktop_batch_is_approval_gated_and_bound_to_one_foreground_window(self):
        foreground = {
            "application": "notepad.exe",
            "title": "Notes",
            "left": 10,
            "top": 20,
            "right": 810,
            "bottom": 620,
            "width": 800,
            "height": 600,
            "context_sha256": "a" * 64,
            "excluded": False,
            "exclusion_reason": None,
        }
        controller = Mock()
        controller.snapshot.return_value = foreground
        controller.interact.return_value = {
            "completed_actions": 2,
            "context_sha256": "a" * 64,
            "verified_before_each_action": True,
        }
        self.toolbox.desktop = controller
        actions = [
            {"type": "click", "x": 50, "y": 80},
            {"type": "type_text", "text": "Finish the outline"},
        ]

        result = self.execute_approved("desktop_interact", {"actions": actions})

        self.assertTrue(result["ok"])
        controller.validate_actions.assert_called_with(actions, context=foreground)
        controller.interact.assert_called_once_with(
            expected_context_sha256="a" * 64,
            actions=actions,
        )

    def test_desktop_approval_shows_every_action_in_the_bounded_batch(self):
        foreground = {
            "application": "notepad.exe",
            "title": "Notes",
            "left": 0,
            "top": 0,
            "right": 800,
            "bottom": 600,
            "width": 800,
            "height": 600,
            "context_sha256": "c" * 64,
            "excluded": False,
            "exclusion_reason": None,
        }
        controller = Mock()
        controller.snapshot.return_value = foreground
        self.toolbox.desktop = controller
        actions = [
            {"type": "type_text", "text": f"Visible approval step {index}"}
            for index in range(1, 13)
        ]

        with self.toolbox.approval_context("conversation:1"):
            blocked = json.loads(self.toolbox.execute(
                "desktop_interact", {"actions": actions}
            ))

        self.assertTrue(blocked["approval_required"])
        approval = self.memory.get_approval(blocked["approval_id"])
        resource = json.loads(approval["resource"])
        visible = resource["arguments"]
        self.assertEqual(visible["action_count"], 12)
        self.assertEqual(visible["foreground_application"], "notepad.exe")
        self.assertEqual(visible["foreground_title"], "Notes")
        for index, action in enumerate(actions, start=1):
            self.assertEqual(
                json.loads(visible[f"action_{index:02d}"]),
                action,
            )
        self.assertNotIn("preview", json.dumps(visible))

    def test_desktop_batch_fails_if_foreground_changes_after_authorization(self):
        first = {
            "application": "notepad.exe", "title": "Notes",
            "left": 0, "top": 0, "right": 800, "bottom": 600,
            "width": 800, "height": 600,
            "context_sha256": "a" * 64, "excluded": False,
            "exclusion_reason": None,
        }
        second = {**first, "title": "Email", "context_sha256": "b" * 64}
        controller = Mock()
        controller.snapshot.return_value = first
        self.toolbox.desktop = controller
        arguments = {"actions": [{"type": "click", "x": 10, "y": 10}]}
        scope = "conversation:1"
        with self.toolbox.approval_context(scope):
            blocked = json.loads(self.toolbox.execute("desktop_interact", arguments))
        self.assertTrue(blocked["approval_required"])
        self.assertTrue(self.memory.decide_approval(blocked["approval_id"], True))
        controller.snapshot.side_effect = [first, second]

        with self.toolbox.approval_context(scope):
            refused = json.loads(self.toolbox.execute("desktop_interact", arguments))

        self.assertFalse(refused["ok"])
        self.assertIn("changed during the final execution check", refused["error"])
        controller.interact.assert_not_called()

    def test_desktop_action_validation_blocks_secrets_and_out_of_bounds_clicks(self):
        controller = WindowsDesktopController(provider=SimpleNamespace(available=False))
        context = {"width": 100, "height": 100}
        with self.assertRaises(PermissionError):
            controller.validate_actions(
                [{"type": "type_text", "text": "API_KEY=sk-proj-" + "A" * 40}],
                context=context,
            )
        with self.assertRaises(ValueError):
            controller.validate_actions(
                [{"type": "click", "x": 100, "y": 10}], context=context
            )
        with self.assertRaises(ValueError):
            controller.validate_actions(
                [{"type": "hotkey", "keys": ["win", "r"]}], context=context
            )


if __name__ == "__main__":
    unittest.main()
