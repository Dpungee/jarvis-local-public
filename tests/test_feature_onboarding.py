from __future__ import annotations

import json
import io
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from jarvis.approvals import SENSITIVE_ACTIONS
from jarvis.config import Config
from jarvis.feature_onboarding import (
    FEATURE_SPECS,
    FeatureOnboardingConflict,
    FeatureOnboardingError,
    FeatureOnboardingStore,
    run_interactive,
)
from jarvis.memory import Memory
from jarvis.tools import ToolBox


EXPECTED_FEATURE_IDS = (
    "private-lan-inventory",
    "private-lan-monitoring",
    "network-defense-alerts",
    "network-defense-safe-readonly",
    "bluetooth-inventory",
    "bluetooth-monitoring",
    "network-security-alerts-ui",
)

ALLOWED_MANAGED_ENV_KEYS = frozenset(
    {
        "JARVIS_NETWORK_ACCESS",
        "JARVIS_NETWORK_MONITOR_ENABLED",
        "JARVIS_NETWORK_DEFENSE_MODE",
        "JARVIS_BLUETOOTH_ACCESS",
        "JARVIS_BLUETOOTH_MONITOR_ENABLED",
        "JARVIS_NETWORK_INCIDENT_POPUPS_ENABLED",
    }
)


def _feature_id(spec: object) -> str:
    if isinstance(spec, dict):
        return str(spec["capability_id"])
    return str(getattr(spec, "capability_id"))


def _by_id(rows: list[dict[str, object]]) -> dict[str, dict[str, object]]:
    return {str(row["capability_id"]): row for row in rows}


def _features(status: dict[str, object]) -> list[dict[str, object]]:
    return list(status["features"])  # type: ignore[arg-type]


def _env_assignments(path: Path) -> list[tuple[str, str]]:
    assignments: list[tuple[str, str]] = []
    if not path.exists():
        return assignments
    for raw in path.read_text(encoding="utf-8").splitlines():
        if not raw or raw.lstrip().startswith("#") or "=" not in raw:
            continue
        key, value = raw.split("=", 1)
        assignments.append((key.strip(), value))
    return assignments


class FeatureOnboardingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="jarvis-feature-onboarding-test-"
        )
        self.root = Path(self.temporary.name) / "jarvis"
        self.data_dir = self.root / "data"
        self.root.mkdir(parents=True)
        self.store = FeatureOnboardingStore(self.root, self.data_dir)

    def tearDown(self) -> None:
        close = getattr(self.store, "close", None)
        if callable(close):
            close()
        self.temporary.cleanup()

    def test_catalog_uses_the_exact_stable_feature_ids_once_each(self) -> None:
        ids = tuple(_feature_id(spec) for spec in FEATURE_SPECS)
        self.assertEqual(ids, EXPECTED_FEATURE_IDS)
        self.assertEqual(len(ids), len(set(ids)))

        status = self.store.list_status()
        rows = _features(status)
        self.assertEqual(tuple(row["capability_id"] for row in rows), ids)
        self.assertFalse(status["complete"])
        self.assertEqual(status["pending_count"], len(EXPECTED_FEATURE_IDS))
        for row in rows:
            self.assertEqual(row["decision"], "pending")
            self.assertTrue(row["setup_available"])

    def test_interactive_first_run_reviews_every_pending_feature_once(self) -> None:
        answers = Mock(
            side_effect=["setup", "skip", "disable", "s", "n", "d", ""]
        )
        output = io.StringIO()

        completed = run_interactive(
            self.store, input_fn=answers, output=output
        )

        self.assertTrue(completed["complete"])
        self.assertEqual(completed["pending_count"], 0)
        self.assertEqual(answers.call_count, len(EXPECTED_FEATURE_IDS))
        rendered = output.getvalue()
        for spec in FEATURE_SPECS:
            self.assertIn(spec.title, rendered)
        self.assertIn("can be changed later", rendered)

        no_prompt = Mock(side_effect=AssertionError("completed review must not reprompt"))
        rerun = run_interactive(
            FeatureOnboardingStore(self.root, self.data_dir),
            input_fn=no_prompt,
            output=io.StringIO(),
        )
        self.assertTrue(rerun["complete"])
        no_prompt.assert_not_called()

    def test_windows_installer_runs_optional_review_after_provider_setup(self) -> None:
        setup = (Path(__file__).resolve().parents[1] / "setup.ps1").read_text(
            encoding="utf-8"
        )
        provider = setup.index('"jarvis.provider_setup", "--interactive"')
        features = setup.index('"jarvis.feature_onboarding", "--interactive"')
        self.assertLess(provider, features)

    def test_skip_is_durable_and_can_be_changed_to_setup_or_disable_later(self) -> None:
        first = self.store.decide("private-lan-inventory", "skip")
        self.assertEqual(first["capability_id"], "private-lan-inventory")
        self.assertEqual(first["decision"], "skip")
        self.assertIsInstance(first["restart_required"], bool)

        reopened = FeatureOnboardingStore(self.root, self.data_dir)
        try:
            self.assertEqual(
                _by_id(_features(reopened.list_status()))["private-lan-inventory"]["decision"],
                "skip",
            )

            configured = reopened.decide("private-lan-inventory", "setup")
            self.assertEqual(configured["decision"], "setup")
            self.assertEqual(
                dict(_env_assignments(self.root / ".env"))["JARVIS_NETWORK_ACCESS"],
                "private-lan",
            )

            disabled = reopened.decide("private-lan-inventory", "disable")
            self.assertEqual(disabled["decision"], "disable")
            self.assertEqual(
                dict(_env_assignments(self.root / ".env"))["JARVIS_NETWORK_ACCESS"],
                "disabled",
            )
        finally:
            close = getattr(reopened, "close", None)
            if callable(close):
                close()

        final = FeatureOnboardingStore(self.root, self.data_dir)
        try:
            self.assertEqual(
                _by_id(_features(final.list_status()))["private-lan-inventory"]["decision"],
                "disable",
            )
        finally:
            close = getattr(final, "close", None)
            if callable(close):
                close()

    def test_every_feature_can_be_skipped_then_set_up_later(self) -> None:
        for capability_id in EXPECTED_FEATURE_IDS:
            skipped = self.store.decide(capability_id, "skip")
            self.assertEqual(skipped["decision"], "skip")
        self.assertEqual(
            {row["decision"] for row in _features(self.store.list_status())}, {"skip"}
        )
        self.assertTrue(self.store.list_status()["complete"])

        for capability_id in EXPECTED_FEATURE_IDS:
            configured = self.store.decide(capability_id, "setup")
            self.assertEqual(configured["decision"], "setup")
        self.assertEqual(
            {row["decision"] for row in _features(self.store.list_status())}, {"setup"}
        )

    def test_setup_plans_are_declarative_and_never_download_or_probe(self) -> None:
        for capability_id in EXPECTED_FEATURE_IDS:
            with self.subTest(capability_id=capability_id):
                plan = self.store.setup_plan(capability_id)
                self.assertEqual(plan["capability_id"], capability_id)
                self.assertEqual(plan["downloads"], [])
                self.assertEqual(plan["active_probes"], [])
                self.assertEqual(plan["commands"], [])
                self.assertEqual(plan["network_calls"], [])
                self.assertEqual(plan["containment_actions"], [])
                self.assertIsInstance(plan["managed_settings"], dict)
                self.assertLessEqual(
                    set(plan["managed_settings"]), ALLOWED_MANAGED_ENV_KEYS
                )

                serialized = json.dumps(plan, sort_keys=True).casefold()
                for forbidden in (
                    "ping-sweep",
                    "safe-service-inventory",
                    "winget install",
                    "choco install",
                    "pip install",
                    "powershell -command",
                    "cmd.exe",
                    "download_url",
                ):
                    self.assertNotIn(forbidden, serialized)

    def test_dependency_setup_and_disable_cascade_remain_consistent(self) -> None:
        enabled = self.store.decide("network-defense-safe-readonly", "setup")
        self.assertEqual(
            enabled["also_changed"],
            ["private-lan-inventory", "network-defense-alerts"],
        )
        status = _by_id(_features(self.store.list_status()))
        for capability_id in (
            "private-lan-inventory",
            "network-defense-alerts",
            "network-defense-safe-readonly",
        ):
            self.assertEqual(status[capability_id]["decision"], "setup")
            self.assertTrue(status[capability_id]["configured"])

        disabled = self.store.decide("private-lan-inventory", "disable")
        self.assertEqual(
            disabled["also_changed"],
            [
                "private-lan-monitoring",
                "network-defense-alerts",
                "network-defense-safe-readonly",
            ],
        )
        values = dict(_env_assignments(self.root / ".env"))
        self.assertEqual(values["JARVIS_NETWORK_ACCESS"], "disabled")
        self.assertEqual(values["JARVIS_NETWORK_MONITOR_ENABLED"], "0")
        self.assertEqual(values["JARVIS_NETWORK_DEFENSE_MODE"], "disabled")
        status = _by_id(_features(self.store.list_status()))
        for capability_id in (
            "private-lan-inventory",
            "private-lan-monitoring",
            "network-defense-alerts",
            "network-defense-safe-readonly",
        ):
            self.assertEqual(status[capability_id]["decision"], "disable")
            self.assertFalse(status[capability_id]["configured"])

    def test_decisions_do_not_launch_processes_or_make_network_requests(self) -> None:
        with (
            patch.object(
                subprocess,
                "run",
                side_effect=AssertionError("onboarding must not run a process"),
            ),
            patch.object(
                subprocess,
                "Popen",
                side_effect=AssertionError("onboarding must not start a process"),
            ),
            patch.object(
                urllib.request,
                "urlopen",
                side_effect=AssertionError("onboarding must not use the network"),
            ),
        ):
            for capability_id in EXPECTED_FEATURE_IDS:
                self.store.setup_plan(capability_id)
                self.store.decide(capability_id, "setup")
                self.store.decide(capability_id, "disable")

    def test_only_managed_env_keys_change_and_chat_safe_results_hide_secrets(self) -> None:
        env_path = self.root / ".env"
        secret = "sk-test-this-must-never-be-returned"
        env_path.write_text(
            "# operator-owned line\n"
            f"OPENAI_API_KEY={secret}\n"
            "JARVIS_COMMAND_TIMEOUT=77\n"
            "JARVIS_NETWORK_ACCESS=old-value\n"
            "JARVIS_NETWORK_ACCESS=another-old-value\n",
            encoding="utf-8",
        )

        result = self.store.decide("private-lan-inventory", "setup")
        rows = self.store.list_status()
        plan = self.store.setup_plan("private-lan-inventory")
        rendered = json.dumps(
            {"result": result, "status": rows, "plan": plan}, sort_keys=True
        )
        self.assertNotIn(secret, rendered)
        self.assertNotIn(str(self.root), rendered)

        saved = env_path.read_text(encoding="utf-8")
        self.assertIn("# operator-owned line", saved)
        self.assertIn(f"OPENAI_API_KEY={secret}", saved)
        self.assertIn("JARVIS_COMMAND_TIMEOUT=77", saved)
        assignments = _env_assignments(env_path)
        self.assertEqual(
            [value for key, value in assignments if key == "JARVIS_NETWORK_ACCESS"],
            ["private-lan"],
        )

        before_unmanaged = {
            key: value
            for key, value in assignments
            if key not in ALLOWED_MANAGED_ENV_KEYS
        }
        for capability_id in EXPECTED_FEATURE_IDS:
            self.store.decide(capability_id, "setup")
            self.store.decide(capability_id, "disable")
        after_unmanaged = {
            key: value
            for key, value in _env_assignments(env_path)
            if key not in ALLOWED_MANAGED_ENV_KEYS
        }
        self.assertEqual(after_unmanaged, before_unmanaged)

    def test_exact_capability_and_decision_enums_fail_closed(self) -> None:
        with self.assertRaises((KeyError, ValueError)):
            self.store.setup_plan("private-lan-inventory; whoami")
        with self.assertRaises((KeyError, ValueError)):
            self.store.decide("private-lan-inventory; whoami", "setup")

        for decision in (
            "yes",
            "install",
            "enable-active-probes",
            "setup && whoami",
            "",
        ):
            with self.subTest(decision=decision):
                with self.assertRaises((KeyError, ValueError)):
                    self.store.decide("private-lan-inventory", decision)

    def test_safe_readonly_setup_never_enables_active_network_authority(self) -> None:
        plan = self.store.setup_plan("network-defense-safe-readonly")
        self.assertEqual(
            plan["managed_settings"], {
                "JARVIS_NETWORK_ACCESS": "private-lan",
                "JARVIS_NETWORK_DEFENSE_MODE": "safe-readonly",
            }
        )
        self.store.decide("network-defense-safe-readonly", "setup")
        values = dict(_env_assignments(self.root / ".env"))
        self.assertEqual(values["JARVIS_NETWORK_DEFENSE_MODE"], "safe-readonly")
        combined = json.dumps(
            {"plan": plan, "status": self.store.list_status()}, sort_keys=True
        ).casefold()
        self.assertNotIn("bounded_active_probe", combined)
        self.assertNotIn("state_changing_containment", combined)

    def test_final_hash_recheck_rejects_a_noncooperating_env_editor(self) -> None:
        env_path = self.root / ".env"
        env_path.write_text("OPERATOR_SETTING=before\n", encoding="utf-8")
        original_writer = self.store._atomic_write_env

        def race_writer(encoded: bytes, **kwargs: object) -> None:
            env_path.write_text(
                "OPERATOR_SETTING=changed-outside-lock\n", encoding="utf-8"
            )
            original_writer(encoded, **kwargs)  # type: ignore[arg-type]

        with patch.object(self.store, "_atomic_write_env", side_effect=race_writer):
            with self.assertRaises(FeatureOnboardingConflict):
                self.store.decide("private-lan-inventory", "setup")

        saved = env_path.read_text(encoding="utf-8")
        self.assertIn("OPERATOR_SETTING=changed-outside-lock", saved)
        self.assertNotIn("JARVIS_NETWORK_ACCESS", saved)
        connection = sqlite3.connect(self.store.db_path)
        try:
            self.assertEqual(
                connection.execute(
                    "SELECT status FROM feature_change_journal"
                ).fetchone()[0],
                "conflict",
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM feature_decisions").fetchone()[0],
                0,
            )
        finally:
            connection.close()

    def test_database_finalization_failure_restores_exact_env_and_keeps_journal(self) -> None:
        env_path = self.root / ".env"
        secret = "sk-test-never-store-this-secret"
        original = (
            f"OPENAI_API_KEY={secret}\n"
            "OPERATOR_SETTING=preserve-this\n"
        ).encode("utf-8")
        env_path.write_bytes(original)

        with patch.object(
            self.store,
            "_finalize_change",
            side_effect=sqlite3.OperationalError("injected commit failure"),
        ):
            with self.assertRaisesRegex(FeatureOnboardingError, "restored"):
                self.store.decide("private-lan-inventory", "setup")

        self.assertEqual(env_path.read_bytes(), original)
        connection = sqlite3.connect(self.store.db_path)
        try:
            row = connection.execute(
                "SELECT status, error_code, before_configuration_sha256, "
                "after_configuration_sha256 FROM feature_change_journal"
            ).fetchone()
            self.assertEqual(row[0], "compensated")
            self.assertEqual(row[1], "database_finalization_failed")
            self.assertNotEqual(row[2], row[3])
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM feature_decisions").fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM feature_decision_receipts"
                ).fetchone()[0],
                0,
            )
        finally:
            connection.close()
        self.assertNotIn(secret.encode("utf-8"), self.store.db_path.read_bytes())

    def test_restart_recovers_crash_after_env_replace_before_db_finalization(self) -> None:
        child = r'''
import os
import sys
from pathlib import Path
from jarvis.feature_onboarding import FeatureOnboardingStore

class CrashAfterReplace(FeatureOnboardingStore):
    def _finalize_change(self, operation_id: str) -> None:
        os._exit(73)

store = CrashAfterReplace(Path(sys.argv[1]), Path(sys.argv[2]))
store.decide("bluetooth-inventory", "setup")
'''
        result = subprocess.run(
            [sys.executable, "-c", child, str(self.root), str(self.data_dir)],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        self.assertEqual(result.returncode, 73, result.stderr)
        self.assertEqual(
            dict(_env_assignments(self.root / ".env"))["JARVIS_BLUETOOTH_ACCESS"],
            "paired-readonly",
        )

        recovered = FeatureOnboardingStore(self.root, self.data_dir)
        try:
            row = _by_id(_features(recovered.list_status()))["bluetooth-inventory"]
            self.assertEqual(row["decision"], "setup")
            self.assertTrue(row["configured"])
            connection = sqlite3.connect(recovered.db_path)
            try:
                self.assertEqual(
                    connection.execute(
                        "SELECT status FROM feature_change_journal"
                    ).fetchone()[0],
                    "finalized",
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM feature_decision_receipts"
                    ).fetchone()[0],
                    1,
                )
            finally:
                connection.close()
        finally:
            close = getattr(recovered, "close", None)
            if callable(close):
                close()

    def test_concurrent_process_decisions_preserve_both_independent_changes(self) -> None:
        barrier = self.root / "start-concurrent-decisions"
        child = r'''
import json
import sys
import time
from pathlib import Path
from jarvis.feature_onboarding import FeatureOnboardingStore

root = Path(sys.argv[1])
data = Path(sys.argv[2])
barrier = Path(sys.argv[3])
capability = sys.argv[4]
store = FeatureOnboardingStore(root, data)
deadline = time.monotonic() + 10
while not barrier.exists():
    if time.monotonic() >= deadline:
        raise SystemExit(90)
    time.sleep(0.01)
print(json.dumps(store.decide(capability, "setup")), flush=True)
'''
        processes = [
            subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    child,
                    str(self.root),
                    str(self.data_dir),
                    str(barrier),
                    capability,
                ],
                cwd=Path(__file__).resolve().parents[1],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for capability in ("private-lan-inventory", "bluetooth-inventory")
        ]
        time.sleep(0.1)
        barrier.touch()
        outputs: list[str] = []
        for process in processes:
            stdout, stderr = process.communicate(timeout=20)
            self.assertEqual(process.returncode, 0, stderr)
            outputs.append(stdout)
        self.assertTrue(all(json.loads(output)["decision"] == "setup" for output in outputs))
        values = dict(_env_assignments(self.root / ".env"))
        self.assertEqual(values["JARVIS_NETWORK_ACCESS"], "private-lan")
        self.assertEqual(values["JARVIS_BLUETOOTH_ACCESS"], "paired-readonly")
        connection = sqlite3.connect(self.store.db_path)
        try:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM feature_change_journal WHERE status='finalized'"
                ).fetchone()[0],
                2,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM feature_decision_receipts"
                ).fetchone()[0],
                2,
            )
        finally:
            connection.close()


class FeatureOnboardingToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="jarvis-feature-onboarding-tool-test-"
        )
        self.root = Path(self.temporary.name) / "jarvis"
        self.workspace = self.root / "workspace"
        self.data_dir = self.root / "data"
        self.workspace.mkdir(parents=True)
        self.data_dir.mkdir(parents=True)
        self.config = replace(
            Config.load(),
            root=self.root,
            workspace=self.workspace,
            data_dir=self.data_dir,
            autonomy="autonomous",
            execution_mode="disabled",
            computer_access="disabled",
            network_access="disabled",
            bluetooth_access="disabled",
        )
        self.memory = Memory(self.data_dir / "tools.db")
        self.toolbox = ToolBox(self.config, self.memory)

    def tearDown(self) -> None:
        self.memory.close()
        self.temporary.cleanup()

    def test_toolbox_exposes_two_read_only_tools_and_one_exact_approval_gate(self) -> None:
        names = {
            "feature_setup_status",
            "feature_setup_plan",
            "feature_setup_decide",
        }
        self.assertTrue(names.issubset(self.toolbox.tools))
        self.assertEqual(
            SENSITIVE_ACTIONS["feature_setup_decide"][0], "extend_capability"
        )
        self.assertNotIn("feature_setup_status", SENSITIVE_ACTIONS)
        self.assertNotIn("feature_setup_plan", SENSITIVE_ACTIONS)

        status = json.loads(self.toolbox.execute("feature_setup_status", {}))
        plan = json.loads(
            self.toolbox.execute(
                "feature_setup_plan",
                {"capability_id": "network-defense-safe-readonly"},
            )
        )
        self.assertTrue(status["ok"])
        self.assertTrue(plan["ok"])
        self.assertEqual(plan["result"]["commands"], [])
        self.assertEqual(plan["result"]["downloads"], [])
        self.assertEqual(plan["result"]["network_calls"], [])
        self.assertEqual(plan["result"]["active_probes"], [])

    def test_tool_schemas_accept_only_exact_ids_and_decisions(self) -> None:
        status = self.toolbox.tools["feature_setup_status"].parameters
        plan = self.toolbox.tools["feature_setup_plan"].parameters
        decide = self.toolbox.tools["feature_setup_decide"].parameters
        self.assertEqual(status.get("properties"), {})
        self.assertFalse(status.get("additionalProperties", True))
        self.assertEqual(
            tuple(plan["properties"]["capability_id"]["enum"]),
            EXPECTED_FEATURE_IDS,
        )
        self.assertEqual(plan["required"], ["capability_id"])
        self.assertFalse(plan.get("additionalProperties", True))
        self.assertEqual(
            tuple(decide["properties"]["capability_id"]["enum"]),
            EXPECTED_FEATURE_IDS,
        )
        self.assertEqual(
            tuple(decide["properties"]["decision"]["enum"]),
            ("setup", "skip", "disable"),
        )
        self.assertEqual(decide["required"], ["capability_id", "decision"])
        self.assertFalse(decide.get("additionalProperties", True))

    def test_decision_tool_requires_exact_scoped_approval_before_mutation(self) -> None:
        arguments = {
            "capability_id": "private-lan-inventory",
            "decision": "setup",
        }
        before = (self.root / ".env").read_bytes() if (self.root / ".env").exists() else None

        unscoped = json.loads(
            self.toolbox.execute("feature_setup_decide", arguments)
        )
        self.assertFalse(unscoped["ok"])
        self.assertTrue(unscoped["approval_required"])
        self.assertIsNone(unscoped["approval_id"])
        self.assertEqual(
            (self.root / ".env").read_bytes() if (self.root / ".env").exists() else None,
            before,
        )

        scope = "conversation:123"
        with self.toolbox.approval_context(scope):
            blocked = json.loads(
                self.toolbox.execute("feature_setup_decide", arguments)
            )
        self.assertFalse(blocked["ok"])
        self.assertTrue(blocked["approval_required"])
        request = next(
            item
            for item in self.memory.list_approvals()
            if item["id"] == blocked["approval_id"]
        )
        self.assertEqual(request["action"], "extend_capability")
        self.assertEqual(request["scope"], scope)
        self.assertIn("private-lan-inventory", request["resource"])
        self.assertIn("setup", request["resource"])
        self.assertIn("expected_configuration_sha256", request["resource"])
        self.assertTrue(self.memory.decide_approval(blocked["approval_id"], True))

        with self.toolbox.approval_context(scope):
            allowed = json.loads(
                self.toolbox.execute("feature_setup_decide", arguments)
            )
        self.assertTrue(allowed["ok"])
        self.assertEqual(allowed["result"]["decision"], "setup")
        self.assertEqual(
            dict(_env_assignments(self.root / ".env"))["JARVIS_NETWORK_ACCESS"],
            "private-lan",
        )

    def test_setup_approval_is_invalidated_when_configuration_changes(self) -> None:
        arguments = {
            "capability_id": "private-lan-inventory",
            "decision": "setup",
        }
        scope = "conversation:125"
        with self.toolbox.approval_context(scope):
            first = json.loads(
                self.toolbox.execute("feature_setup_decide", arguments)
            )
        self.assertTrue(first["approval_required"])
        self.assertTrue(self.memory.decide_approval(first["approval_id"], True))

        (self.root / ".env").write_text(
            "JARVIS_NETWORK_INCIDENT_POPUPS_ENABLED=1\n",
            encoding="utf-8",
        )
        with self.toolbox.approval_context(scope):
            stale = json.loads(
                self.toolbox.execute("feature_setup_decide", arguments)
            )

        self.assertFalse(stale["ok"])
        self.assertTrue(stale["approval_required"])
        self.assertNotEqual(stale["approval_id"], first["approval_id"])
        values = dict(_env_assignments(self.root / ".env"))
        self.assertEqual(values["JARVIS_NETWORK_INCIDENT_POPUPS_ENABLED"], "1")
        self.assertNotIn("JARVIS_NETWORK_ACCESS", values)

    def test_skip_and_disable_reduce_authority_without_an_approval_loop(self) -> None:
        skipped = json.loads(
            self.toolbox.execute(
                "feature_setup_decide",
                {"capability_id": "bluetooth-inventory", "decision": "skip"},
            )
        )
        self.assertTrue(skipped["ok"])
        self.assertEqual(skipped["result"]["decision"], "skip")

        disabled = json.loads(
            self.toolbox.execute(
                "feature_setup_decide",
                {"capability_id": "bluetooth-inventory", "decision": "disable"},
            )
        )
        self.assertTrue(disabled["ok"])
        self.assertEqual(disabled["result"]["decision"], "disable")
        self.assertEqual(
            dict(_env_assignments(self.root / ".env"))["JARVIS_BLUETOOTH_ACCESS"],
            "disabled",
        )
        self.assertEqual(self.memory.list_approvals(), [])

    def test_approval_for_one_capability_cannot_authorize_another(self) -> None:
        setup = {
            "capability_id": "bluetooth-inventory",
            "decision": "setup",
        }
        scope = "conversation:124"
        with self.toolbox.approval_context(scope):
            first = json.loads(
                self.toolbox.execute("feature_setup_decide", setup)
            )
        self.assertTrue(self.memory.decide_approval(first["approval_id"], True))

        with self.toolbox.approval_context(scope):
            changed = json.loads(
                self.toolbox.execute(
                    "feature_setup_decide",
                    {
                        "capability_id": "bluetooth-monitoring",
                        "decision": "setup",
                    },
                )
            )
        self.assertFalse(changed["ok"])
        self.assertTrue(changed["approval_required"])
        self.assertNotEqual(changed["approval_id"], first["approval_id"])
        self.assertNotIn(
            "JARVIS_BLUETOOTH_ACCESS",
            dict(_env_assignments(self.root / ".env")),
        )


if __name__ == "__main__":
    unittest.main()
