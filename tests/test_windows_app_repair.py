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
from jarvis.agent import (
    Agent,
    _is_clear_tool_free_dialogue,
    _required_effect_tools,
    _requests_computer_access,
)
from jarvis.memory import Memory
from jarvis.proactive import record_result_reflection
from jarvis.tools import ToolBox
from jarvis.windows_app_repair import (
    AppRepairProfile,
    WindowsAppRepairController,
    _cache_manifest,
    _ensure_ordinary_directory_chain,
    _safe_cache_pattern,
)
from jarvis.windows_apps import InstalledApplication, WindowsAppController
from tests.test_agent import FakeResponse, FakeToolBox, ScriptedClient, tool_call


TEMP_ROOT = Path(__file__).resolve().parent / ".tmp"
TEMP_ROOT.mkdir(exist_ok=True)


class WindowsAppRepairControllerTests(unittest.TestCase):
    def setUp(self) -> None:
        method_key = hashlib.sha256(
            self._testMethodName.encode("utf-8")
        ).hexdigest()[:10]
        self.root = TEMP_ROOT / f"app-repair-{os.getpid()}-{method_key}"
        if self.root.exists():
            shutil.rmtree(self.root)
        self.profile = self.root / "profile"
        self.local = self.profile / "AppData" / "Local"
        self.local.mkdir(parents=True)
        self.executable = self.root / "EpicGamesLauncher.exe"
        self.executable.write_bytes(b"signed-fixture")
        self.repair_profile = AppRepairProfile(
            app_id="epic-games-launcher",
            launch_name="Epic Games Launcher",
            aliases=("epic",),
            executable_names=("epicgameslauncher.exe",),
            install_relative_paths=("EpicGamesLauncher.exe",),
            cache_patterns=(
                "EpicGamesLauncher/Saved/webcache*/Cache",
                "EpicGamesLauncher/Saved/webcache*/GPUCache",
            ),
            backup_root="EpicGamesLauncher/Saved/jarvis-repair-backups",
            process_relative_paths=("EpicGamesLauncher.exe",),
        )
        self.launcher = Mock(return_value=SimpleNamespace(pid=4242))
        self.apps = WindowsAppController(
            self.profile,
            self.root / "data",
            launcher=self.launcher,
            catalog=lambda: [InstalledApplication(
                "Epic Games Launcher", self.executable, "test"
            )],
        )
        self.running = True

        def probe(_executable: Path) -> list[int]:
            return [321] if self.running else []

        def close(_executable: Path, _process_ids: list[int]) -> bool:
            self.running = False
            return True

        self.controller = WindowsAppRepairController(
            self.profile,
            self.apps,
            profiles=(self.repair_profile,),
            process_probe=probe,
            network_probe=lambda _pids: 2,
            graceful_close=close,
            install_roots=(self.root,),
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.root)

    def _cache(self, name: str, content: bytes = b"cache") -> Path:
        target = (
            self.local / "EpicGamesLauncher" / "Saved" / "webcache_4430" / name
        )
        target.mkdir(parents=True)
        (target / "entry.bin").write_bytes(content)
        return target

    def test_diagnosis_is_profile_driven_and_reads_metadata_only(self) -> None:
        self._cache("Cache")
        self._cache("GPUCache")

        result = self.controller.diagnose("epic", "blank_or_unrendered")

        self.assertEqual(result["diagnosis"]["category"], "render_cache")
        self.assertTrue(result["repair_supported"])
        self.assertEqual(result["observations"]["cache_directories"], 2)
        self.assertEqual(result["observations"]["established_https_connections"], 2)
        self.assertFalse(result["observations"]["ui_pixels_inspected"])
        self.assertFalse(result["observations"]["cache_contents_read"])
        self.assertEqual(len(result["plan_id"]), 64)
        self.assertNotIn("entry.bin", repr(result))

    def test_unknown_profile_and_missing_cache_fail_closed(self) -> None:
        with self.assertRaises(FileNotFoundError):
            self.controller.diagnose("unprofiled app", "blank_or_unrendered")

        result = self.controller.diagnose("epic", "blank_or_unrendered")
        self.assertFalse(result["repair_supported"])
        self.assertIsNone(result["plan"])

    def test_too_many_cache_targets_fail_before_approval(self) -> None:
        for index in range(6):
            target = (
                self.local / "EpicGamesLauncher" / "Saved"
                / f"webcache_{index}" / "Cache"
            )
            target.mkdir(parents=True)

        with self.assertRaisesRegex(ValueError, "too many cache targets"):
            self.controller.diagnose("epic", "blank_or_unrendered")

    def test_render_cache_repair_moves_to_backup_restarts_and_stays_unverified(self) -> None:
        cache = self._cache("Cache")
        diagnosis = self.controller.diagnose("epic", "blank_or_unrendered")
        approved = self.controller.repair_snapshot(
            "epic", diagnosis["plan_id"], "blank_or_unrendered"
        )

        result = self.controller.apply(
            "epic",
            diagnosis["plan_id"],
            symptom="blank_or_unrendered",
            approved=approved,
        )

        self.assertTrue(result["repair_applied"])
        self.assertFalse(cache.exists())
        self.assertTrue(
            (self.local / result["backup_locations"][0]).is_dir()
        )
        self.assertFalse(result["source_deleted"])
        self.assertNotIn(str(self.profile), repr(result))
        self.assertNotIn("executable", result["restart"])
        self.assertEqual(
            result["verification_status"],
            "awaiting_visual_and_health_evidence",
        )
        self.assertEqual(result["outcome"]["status"], "incomplete")
        self.assertFalse(result["lesson_stored"])
        positional, keyword = self.launcher.call_args
        self.assertEqual(positional[0], [str(self.executable.resolve())])
        self.assertNotIn("shell", keyword)

    def test_graceful_close_failure_moves_nothing(self) -> None:
        cache = self._cache("Cache")
        controller = WindowsAppRepairController(
            self.profile,
            self.apps,
            profiles=(self.repair_profile,),
            process_probe=lambda _executable: [321],
            network_probe=lambda _pids: 1,
            graceful_close=lambda _executable, _pids: False,
            install_roots=(self.root,),
        )
        diagnosis = controller.diagnose("epic", "blank_or_unrendered")
        approved = controller.repair_snapshot(
            "epic", diagnosis["plan_id"], "blank_or_unrendered"
        )

        with self.assertRaises(RuntimeError):
            controller.apply(
                "epic",
                diagnosis["plan_id"],
                symptom="blank_or_unrendered",
                approved=approved,
            )

        self.assertTrue(cache.is_dir())
        self.launcher.assert_not_called()

    def test_same_size_same_mtime_executable_change_invalidates_plan(self) -> None:
        cache = self._cache("Cache")
        diagnosis = self.controller.diagnose("epic", "blank_or_unrendered")
        approved = self.controller.repair_snapshot(
            "epic", diagnosis["plan_id"], "blank_or_unrendered"
        )
        original = self.executable.stat()
        self.executable.write_bytes(b"x" * original.st_size)
        os.utime(
            self.executable,
            ns=(original.st_atime_ns, original.st_mtime_ns),
        )

        with self.assertRaises(PermissionError):
            self.controller.apply(
                "epic",
                diagnosis["plan_id"],
                symptom="blank_or_unrendered",
                approved=approved,
            )

        self.assertTrue(cache.is_dir())
        self.launcher.assert_not_called()

    def test_cache_drift_is_rejected_before_move(self) -> None:
        cache = self._cache("Cache")
        diagnosis = self.controller.diagnose("epic", "blank_or_unrendered")
        approved = self.controller.repair_snapshot(
            "epic", diagnosis["plan_id"], "blank_or_unrendered"
        )
        (cache / "new.bin").write_bytes(b"changed")

        with self.assertRaises(PermissionError):
            self.controller.apply(
                "epic",
                diagnosis["plan_id"],
                symptom="blank_or_unrendered",
                approved=approved,
            )

        self.assertTrue(cache.is_dir())
        self.launcher.assert_not_called()

    def test_catalog_alias_cannot_escape_declared_install_target(self) -> None:
        spoof = self.profile / "EpicGamesLauncher.exe"
        spoof.write_bytes(b"spoofed-user-writable-binary")
        spoofed_apps = WindowsAppController(
            self.profile,
            self.root / "spoof-data",
            launcher=self.launcher,
            catalog=lambda: [InstalledApplication(
                "Epic Games Launcher", spoof, "registry"
            )],
        )
        controller = WindowsAppRepairController(
            self.profile,
            spoofed_apps,
            profiles=(self.repair_profile,),
            process_probe=lambda _executable: [],
            network_probe=lambda _pids: 0,
            graceful_close=lambda _executable, _pids: True,
            install_roots=(self.root,),
        )

        with self.assertRaises(PermissionError):
            controller.diagnose("epic", "blank_or_unrendered")

    def test_source_junction_cannot_redirect_to_another_local_app(self) -> None:
        outside = self.local / "OtherApp"
        (outside / "Cache").mkdir(parents=True)
        saved = self.local / "EpicGamesLauncher" / "Saved"
        saved.mkdir(parents=True)
        link = saved / "webcache_4430"
        try:
            os.symlink(outside, link, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"Directory links are unavailable: {exc}")

        with self.assertRaises(PermissionError):
            self.controller.diagnose("epic", "blank_or_unrendered")

    def test_hard_linked_cache_file_is_rejected(self) -> None:
        cache = self._cache("Cache")
        linked = self.root / "linked-entry.bin"
        try:
            os.link(cache / "entry.bin", linked)
        except OSError as exc:
            self.skipTest(f"Hard links are unavailable: {exc}")

        with self.assertRaises(PermissionError):
            self.controller.diagnose("epic", "blank_or_unrendered")

    def test_invalid_profile_globs_are_rejected(self) -> None:
        for pattern in (
            "*/Saved/webcache*/Cache",
            "EpicGamesLauncher/**/Cache",
            "EpicGamesLauncher/Saved/webcache?/Cache",
            "EpicGamesLauncher/Saved/[ab]/Cache",
            "D:cache/Cache",
        ):
            with self.subTest(pattern=pattern), self.assertRaises(PermissionError):
                _safe_cache_pattern(pattern)

    def test_cache_manifest_is_stable_across_directory_order(self) -> None:
        cache = self._cache("Cache")
        (cache / "a.bin").write_bytes(b"a")
        (cache / "z.bin").write_bytes(b"z")
        expected = _cache_manifest(cache)
        real_scandir = os.scandir

        class ReversedScandir:
            def __init__(self, path: Path) -> None:
                self.iterator = real_scandir(path)

            def __enter__(self):
                return iter(reversed(list(self.iterator)))

            def __exit__(self, *_args):
                self.iterator.close()

        with patch(
            "jarvis.windows_app_repair.os.scandir",
            side_effect=lambda path: ReversedScandir(path),
        ):
            observed = _cache_manifest(cache)

        self.assertEqual(observed, expected)

    def test_launch_failure_restores_every_moved_cache(self) -> None:
        cache = self._cache("Cache")
        failing_apps = WindowsAppController(
            self.profile,
            self.root / "failing-data",
            launcher=Mock(side_effect=OSError("launch failed")),
            catalog=lambda: [InstalledApplication(
                "Epic Games Launcher", self.executable, "test"
            )],
        )

        def probe(_executable: Path) -> list[int]:
            return [321] if self.running else []

        def close(_executable: Path, _process_ids: list[int]) -> bool:
            self.running = False
            return True

        controller = WindowsAppRepairController(
            self.profile,
            failing_apps,
            profiles=(self.repair_profile,),
            process_probe=probe,
            network_probe=lambda _pids: 1,
            graceful_close=close,
            install_roots=(self.root,),
        )
        diagnosis = controller.diagnose("epic", "blank_or_unrendered")
        approved = controller.repair_snapshot(
            "epic", diagnosis["plan_id"], "blank_or_unrendered"
        )

        with self.assertRaisesRegex(RuntimeError, "cache backup was restored"):
            controller.apply(
                "epic",
                diagnosis["plan_id"],
                symptom="blank_or_unrendered",
                approved=approved,
            )

        self.assertTrue(cache.is_dir())
        self.assertFalse(Path(approved["moves"][0]["destination"]).exists())

    def test_launch_failure_does_not_overwrite_a_recreated_source(self) -> None:
        cache = self._cache("Cache")

        def fail_after_recreating_source(*_args, **_kwargs):
            cache.mkdir(parents=True)
            (cache / "new.bin").write_bytes(b"new application cache")
            raise OSError("launch failed")

        failing_apps = WindowsAppController(
            self.profile,
            self.root / "failing-race-data",
            launcher=Mock(side_effect=fail_after_recreating_source),
            catalog=lambda: [InstalledApplication(
                "Epic Games Launcher", self.executable, "test"
            )],
        )
        controller = WindowsAppRepairController(
            self.profile,
            failing_apps,
            profiles=(self.repair_profile,),
            process_probe=lambda _executable: [321] if self.running else [],
            network_probe=lambda _pids: 1,
            graceful_close=lambda _executable, _pids: setattr(
                self, "running", False
            ) is None,
            install_roots=(self.root,),
        )
        diagnosis = controller.diagnose("epic", "blank_or_unrendered")
        approved = controller.repair_snapshot(
            "epic", diagnosis["plan_id"], "blank_or_unrendered"
        )

        with self.assertRaisesRegex(RuntimeError, "rollback was incomplete"):
            controller.apply(
                "epic",
                diagnosis["plan_id"],
                symptom="blank_or_unrendered",
                approved=approved,
            )

        self.assertEqual((cache / "new.bin").read_bytes(), b"new application cache")
        self.assertTrue(Path(approved["moves"][0]["destination"]).is_dir())

    def test_orphaned_profile_helper_blocks_cache_move(self) -> None:
        cache = self._cache("Cache")
        helper = self.root / "EpicWebHelper.exe"
        helper.write_bytes(b"helper-fixture")
        profile = replace(
            self.repair_profile,
            process_relative_paths=(
                "EpicGamesLauncher.exe",
                "EpicWebHelper.exe",
            ),
        )
        main_calls = 0

        def probe(executable: Path) -> list[int]:
            nonlocal main_calls
            if executable.name.casefold() == "epicwebhelper.exe":
                return [222]
            main_calls += 1
            return [111] if main_calls <= 3 else []

        controller = WindowsAppRepairController(
            self.profile,
            self.apps,
            profiles=(profile,),
            process_probe=probe,
            network_probe=lambda _pids: 0,
            graceful_close=lambda _executable, _pids: True,
            install_roots=(self.root,),
        )
        diagnosis = controller.diagnose("epic", "blank_or_unrendered")
        approved = controller.repair_snapshot(
            "epic", diagnosis["plan_id"], "blank_or_unrendered"
        )

        with self.assertRaisesRegex(RuntimeError, "helper process"):
            controller.apply(
                "epic",
                diagnosis["plan_id"],
                symptom="blank_or_unrendered",
                approved=approved,
            )

        self.assertTrue(cache.is_dir())
        self.launcher.assert_not_called()

    @unittest.skipUnless(os.name == "nt", "Windows process identity check")
    def test_graceful_close_rechecks_pid_executable_identity(self) -> None:
        completed = SimpleNamespace(returncode=3, stdout="", stderr="")
        with patch(
            "jarvis.windows_app_repair.subprocess.run",
            return_value=completed,
        ) as run:
            closed = self.controller._default_graceful_close(
                self.executable.resolve(),
                [321],
            )

        self.assertFalse(closed)
        command = run.call_args.args[0]
        script = command[-1]
        self.assertIn("ProcessId=$id", script)
        self.assertIn("ExecutablePath", script)
        self.assertIn(str(self.executable.resolve()).replace("'", "''"), script)

    def test_backup_chain_rejects_non_directory_component(self) -> None:
        blocker = self.local / "repair-backups"
        blocker.write_text("not a directory", encoding="utf-8")

        with self.assertRaises(PermissionError):
            _ensure_ordinary_directory_chain(
                blocker / "plan" / "cache",
                self.local,
            )

        self.assertFalse((blocker / "plan").exists())

    def test_backup_chain_rejects_link_before_creating_descendants(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        link = self.local / "repair-backups"
        try:
            os.symlink(outside, link, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"Directory links are unavailable: {exc}")

        with self.assertRaises(PermissionError):
            _ensure_ordinary_directory_chain(
                link / "plan" / "cache",
                self.local,
            )

        self.assertFalse((outside / "plan").exists())


class WindowsAppRepairToolTests(unittest.TestCase):
    def setUp(self) -> None:
        method_key = hashlib.sha256(
            self._testMethodName.encode("utf-8")
        ).hexdigest()[:10]
        self.root = TEMP_ROOT / f"app-repair-tool-{os.getpid()}-{method_key}"
        if self.root.exists():
            shutil.rmtree(self.root)
        workspace = self.root / "workspace"
        data = self.root / "data"
        profile = self.root / "profile"
        workspace.mkdir(parents=True)
        data.mkdir()
        profile.mkdir()
        self.config = replace(
            Config.load(),
            workspace=workspace,
            data_dir=data,
            computer_root=profile,
            computer_access="trusted-desktop",
            execution_mode="trusted-host",
            autonomy="autonomous",
        )
        self.memory = Memory(data / "memory.db")

    def tearDown(self) -> None:
        self.memory.close()
        shutil.rmtree(self.root)

    def test_capability_exposure_respects_desktop_execution_and_autonomy(self) -> None:
        trusted = ToolBox(self.config, self.memory)
        self.assertIn("windows_app_diagnose", trusted.tools)
        self.assertIn("windows_app_repair", trusted.tools)

        disabled = ToolBox(
            replace(self.config, computer_access="disabled"), self.memory
        )
        self.assertNotIn("windows_app_diagnose", disabled.tools)
        self.assertNotIn("windows_app_repair", disabled.tools)

        readonly = ToolBox(replace(self.config, autonomy="readonly"), self.memory)
        self.assertIn("windows_app_diagnose", readonly.tools)
        self.assertNotIn("windows_app_repair", readonly.tools)

        sandboxed = ToolBox(
            replace(self.config, execution_mode="sandbox"), self.memory
        )
        self.assertIn("windows_app_diagnose", sandboxed.tools)
        self.assertNotIn("windows_app_repair", sandboxed.tools)

    def test_general_app_failure_semantics_route_without_brand_phrase_rules(self) -> None:
        cases = (
            (
                "Please diagnose why my photo catalog application is blank",
                "windows_app_diagnose",
            ),
            (
                "Fix my music client because it won't load",
                "windows_app_repair",
            ),
            (
                "My launcher is frozen, investigate it",
                "windows_app_diagnose",
            ),
            (
                "Epic won't load",
                "windows_app_diagnose",
            ),
            (
                "Fix Epic because it is frozen",
                "windows_app_repair",
            ),
        )
        for prompt, expected_tool in cases:
            with self.subTest(prompt=prompt):
                self.assertTrue(_requests_computer_access(prompt))
                tools, _description = _required_effect_tools(
                    prompt,
                    requires_coding=False,
                    allow_external_mutation=False,
                )
                self.assertEqual(tools, {expected_tool})
                self.assertFalse(_is_clear_tool_free_dialogue(prompt))

        explanation = "Explain how application caches work"
        self.assertFalse(_requests_computer_access(explanation))
        self.assertTrue(_is_clear_tool_free_dialogue(explanation))

    def test_repair_is_blocked_at_exact_approval_chokepoint(self) -> None:
        toolbox = ToolBox(self.config, self.memory)
        controller = Mock()
        exact = {
            "application": "epic-games-launcher",
            "plan_id": "a" * 64,
            "moves": [{"source": "cache", "destination": "backup"}],
            "display_name": "Epic Games Launcher",
            "operation": "graceful-close, backup-move, restart, verify",
            "approval_summary": {
                "sources": ["Epic/cache"],
                "backups": ["Epic/backups/cache"],
                "directories": 1,
                "bytes": 123,
                "reversible": True,
                "plan_sha256": "c" * 64,
            },
        }
        controller.repair_snapshot.return_value = exact
        controller.apply.return_value = {"repair_applied": True}
        toolbox.windows_app_repair = controller
        arguments = {"application": "epic", "plan_id": "a" * 64}

        with toolbox.approval_context("conversation:1"):
            blocked = json.loads(toolbox.execute("windows_app_repair", arguments))
        self.assertFalse(blocked["ok"])
        self.assertTrue(blocked["approval_required"])
        controller.apply.assert_not_called()
        approval = self.memory.get_approval(blocked["approval_id"])
        resource = json.loads(approval["resource"])
        self.assertEqual(
            resource["arguments"]["repair_move_01"],
            "Epic/cache -> Epic/backups/cache",
        )
        self.assertEqual(resource["arguments"]["repair_directories"], 1)
        self.assertEqual(resource["arguments"]["repair_bytes"], 123)
        self.assertTrue(resource["arguments"]["repair_reversible"])
        self.assertTrue(self.memory.decide_approval(blocked["approval_id"], True))

        with toolbox.approval_context("conversation:1"):
            applied = json.loads(toolbox.execute("windows_app_repair", arguments))
        self.assertTrue(applied["ok"])
        controller.apply.assert_called_once_with(
            "epic",
            "a" * 64,
            symptom="blank_or_unrendered",
            approved=exact,
        )

    def test_repair_approval_keeps_every_bounded_move_readable(self) -> None:
        toolbox = ToolBox(self.config, self.memory)
        controller = Mock()
        sources = [f"Epic/webcache-{index}/" + "s" * 45 for index in range(5)]
        backups = [f"Epic/backups/{index}/" + "b" * 45 for index in range(5)]
        exact = {
            "application": "epic-games-launcher",
            "display_name": "Epic Games Launcher",
            "plan_id": "e" * 64,
            "operation": "graceful-close, backup-move, restart, verify",
            "moves": [
                {"source": source, "destination": backup}
                for source, backup in zip(sources, backups, strict=True)
            ],
            "approval_summary": {
                "sources": sources,
                "backups": backups,
                "directories": 5,
                "bytes": 456,
                "reversible": True,
                "plan_sha256": "f" * 64,
            },
        }
        controller.repair_snapshot.return_value = exact
        toolbox.windows_app_repair = controller

        with toolbox.approval_context("conversation:4"):
            blocked = json.loads(toolbox.execute(
                "windows_app_repair",
                {"application": "epic", "plan_id": "e" * 64},
            ))

        self.assertTrue(blocked.get("approval_required"), blocked)
        resource_text = self.memory.get_approval(blocked["approval_id"])["resource"]
        resource = json.loads(resource_text)
        self.assertLessEqual(len(resource_text), 1_900)
        self.assertEqual(resource["arguments"]["repair_target"], "Epic Games Launcher")
        for index, (source, backup) in enumerate(
            zip(sources, backups, strict=True),
            start=1,
        ):
            self.assertEqual(
                resource["arguments"][f"repair_move_{index:02d}"],
                f"{source} -> {backup}",
            )

    def test_approval_snapshot_race_returns_no_private_path(self) -> None:
        toolbox = ToolBox(self.config, self.memory)
        controller = Mock()
        controller.repair_snapshot.side_effect = FileNotFoundError(
            str(self.root / "profile" / "private-cache")
        )
        toolbox.windows_app_repair = controller

        with toolbox.approval_context("conversation:race"):
            result = toolbox.execute(
                "windows_app_repair",
                {"application": "epic", "plan_id": "a" * 64},
            )

        self.assertNotIn(str(self.root / "profile"), result)
        self.assertIn("repair target is unavailable", result)

    def test_post_approval_plan_drift_is_rejected_before_apply(self) -> None:
        toolbox = ToolBox(self.config, self.memory)
        controller = Mock()
        exact = {
            "application": "epic-games-launcher",
            "display_name": "Epic Games Launcher",
            "plan_id": "b" * 64,
            "operation": "graceful-close, backup-move, restart, verify",
            "moves": [{"source": "cache", "destination": "backup"}],
            "approval_summary": {
                "sources": ["Epic/cache"],
                "backups": ["Epic/backups/cache"],
                "directories": 1,
                "bytes": 123,
                "reversible": True,
                "plan_sha256": "d" * 64,
            },
        }
        changed = {**exact, "moves": [{"source": "changed"}]}
        controller.repair_snapshot.side_effect = [exact, exact, changed]
        toolbox.windows_app_repair = controller
        arguments = {"application": "epic", "plan_id": "b" * 64}

        with toolbox.approval_context("conversation:2"):
            blocked = json.loads(toolbox.execute("windows_app_repair", arguments))
        self.assertTrue(self.memory.decide_approval(blocked["approval_id"], True))

        with toolbox.approval_context("conversation:2"):
            refused = json.loads(toolbox.execute("windows_app_repair", arguments))
        self.assertFalse(refused["ok"])
        self.assertIn("changed during the final execution check", refused["error"])
        controller.apply.assert_not_called()

    def test_applied_unverified_repair_is_incomplete_and_never_reapplied(self) -> None:
        class PendingVerificationToolBox(FakeToolBox):
            NAMES = FakeToolBox.NAMES + ("windows_app_repair",)

            def execute(self, name, arguments):
                if name == "windows_app_repair":
                    self.calls.append((name, arguments))
                    return json.dumps({
                        "ok": True,
                        "result": {
                            "repair_applied": True,
                            "verification_status": (
                                "awaiting_visual_and_health_evidence"
                            ),
                            "outcome": {"status": "incomplete"},
                        },
                    })
                return super().execute(name, arguments)

        toolbox = PendingVerificationToolBox()
        client = ScriptedClient([
            FakeResponse(tool_calls=[tool_call("windows_app_repair", {
                "application": "epic",
                "plan_id": "a" * 64,
            })]),
            FakeResponse(content="The repair is complete and the app is fixed."),
        ])
        agent = Agent(
            self.config,
            self.memory,
            client=client,
            coding_review=False,
            coding_planning=False,
        )
        agent.toolbox = toolbox

        result = agent.run("Fix my game launcher because its window is blank.")

        self.assertEqual(result.status, "incomplete")
        self.assertIn("visual and health verification", result.reason)
        self.assertFalse(result.lesson_eligible)
        self.assertNotIn("The repair is complete", str(result))
        self.assertEqual(
            [name for name, _arguments in toolbox.calls],
            ["windows_app_repair"],
        )
        self.assertEqual(len(client.requests), 2)
        reflection_id = record_result_reflection(self.memory, result)
        lesson = self.memory.db.execute(
            "SELECT id FROM memories WHERE kind='lesson' AND reflection_id=?",
            (reflection_id,),
        ).fetchone()
        self.assertIsNone(lesson)


if __name__ == "__main__":
    unittest.main()
