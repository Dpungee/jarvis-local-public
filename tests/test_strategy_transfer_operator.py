from __future__ import annotations

import io
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from jarvis import cli
from jarvis.memory import Memory as DurableMemory
from jarvis.strategy_transfer_operator import (
    StrategyTransferOperatorError,
    build_trial_manifest_input,
    sanitized_trial_status,
)


SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64
SHA_E = "e" * 64


def _config(mode: str) -> SimpleNamespace:
    return SimpleNamespace(
        data_dir=Path("data"),
        strategy_transfer=mode,
    )


class _TrialMemory:
    instances: list["_TrialMemory"] = []

    def __init__(self, path: Path) -> None:
        self.path = path
        self.created: dict | None = None
        self.aborted: tuple[int, str] | None = None
        self.promoted: tuple[int, bool] | None = None
        type(self).instances.append(self)

    def __enter__(self) -> "_TrialMemory":
        return self

    def __exit__(self, *_args) -> None:
        return None

    def create_strategy_transfer_trial_manifest(self, **kwargs):
        self.created = kwargs
        return {"id": 7, "status": "active", **kwargs}

    def strategy_transfer_trial_pins(self):
        return {
            "evaluator_version": "2.0.0",
            "evaluator_sha256": SHA_B,
            "fixture_sha256": SHA_C,
            "config_sha256": SHA_D,
            "runtime_sha256": SHA_E,
        }

    def strategy_transfer_trial_status(self, manifest_id=None):
        return [{
            "manifest_id": manifest_id or 7,
            "available": True,
            "status": "active",
            "project_id": 3,
            "target_families": ["code_fix", "deep_research"],
            "strategies": ["verify_output"],
            "sample_cap": 40,
            "assignment_count": 8,
            "causal_attestation_valid": True,
            "promotion_ready": True,
            "prompt": "private operator prompt must never render",
            "notes": "private notes must never render",
        }]

    def abort_strategy_transfer_trial(self, manifest_id, *, reason_code):
        self.aborted = (manifest_id, reason_code)
        return True

    def promote_strategy_transfer_trial(self, manifest_id, *, operator_confirmed):
        self.promoted = (manifest_id, operator_confirmed)
        return {
            "manifest_id": manifest_id,
            "status": "promoted",
            "promoted": True,
            "operator_promoted": True,
            "attestation_sha256": SHA_A,
        }


class StrategyTransferOperatorTests(unittest.TestCase):
    def setUp(self) -> None:
        _TrialMemory.instances = []
        provider_gate = patch.object(cli, "_ensure_first_run_provider_setup")
        provider_gate.start()
        self.addCleanup(provider_gate.stop)

    def test_manifest_is_bounded_prompt_free_and_project_scoped(self):
        manifest = build_trial_manifest_input(
            project_id=3,
            target_families=["code_fix", "deep_research"],
            allowed_families=["code_fix", "deep_research", "conversation"],
            strategies=["inspect_before_change", "verify_output"],
            sample_cap=40,
            duration_days=14,
            seed=SHA_A,
            evaluator_version="2.0.0",
            evaluator_sha256=SHA_B,
            fixture_sha256=SHA_C,
            config_sha256=SHA_D,
            runtime_sha256=SHA_E,
            now=datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc),
        )

        self.assertEqual(manifest["project_id"], 3)
        self.assertEqual(manifest["sample_cap"], 40)
        self.assertEqual(manifest["expires_at"], "2026-09-13T12:00:00Z")
        self.assertTrue(manifest["operator_confirmed"])
        self.assertFalse(
            {"prompt", "goal", "task", "notes", "path", "url"} & set(manifest)
        )

    def test_manifest_rejects_duplicates_ranges_unknowns_and_non_digest_seed(self):
        base = dict(
            project_id=3,
            target_families=["code_fix"],
            allowed_families=["code_fix", "deep_research"],
            strategies=["verify_output"],
            sample_cap=40,
            duration_days=7,
            seed=SHA_A,
            evaluator_version="2.0.0",
            evaluator_sha256=SHA_B,
            fixture_sha256=SHA_C,
            config_sha256=SHA_D,
            runtime_sha256=SHA_E,
        )
        changes = (
            {"target_families": ["code_fix", "code_fix"]},
            {"target_families": ["not_a_family"]},
            {"strategies": ["grant_shell"]},
            {"sample_cap": 42},
            {"sample_cap": 40.0},
            {"sample_cap": 204},
            {"duration_days": 15},
            {"seed": "operator supplied prose"},
            {"evaluator_version": "../../escape"},
        )
        for change in changes:
            with self.subTest(change=change), self.assertRaises(
                StrategyTransferOperatorError
            ):
                build_trial_manifest_input(**{**base, **change})

    def test_status_filter_never_echoes_unknown_or_private_fields(self):
        rows = sanitized_trial_status(
            [{
                "manifest_id": 7,
                "status": "active",
                "project_id": 3,
                "target_families": ["code_fix", "private_family_name"],
                "strategies": ["verify_output", "steal_credentials"],
                "reason_codes": ["pin_mismatch", "customer_name_alice"],
                "prompt": "API_KEY=must-not-render",
                "notes": "private notes",
            }],
            allowed_families=["code_fix"],
        )

        rendered = json.dumps(rows)
        self.assertIn("code_fix", rendered)
        self.assertIn("verify_output", rendered)
        self.assertIn("pin_mismatch", rendered)
        for secret in (
            "must-not-render",
            "private notes",
            "private_family_name",
            "steal_credentials",
            "customer_name_alice",
        ):
            self.assertNotIn(secret, rendered)

    def test_cli_start_requires_trial_mode_and_calls_closed_memory_api(self):
        argv = [
            "strategy-transfer", "start",
            "--project", "3",
            "--family", "code_fix",
            "--family", "deep_research",
            "--strategy", "verify_output",
            "--sample-cap", "40",
            "--duration-days", "7",
        ]
        with (
            patch.object(cli.Config, "load", return_value=_config("observe")),
            patch.object(cli, "Memory", _TrialMemory),
            patch("sys.stdout", io.StringIO()),
            self.assertRaises(SystemExit) as blocked,
        ):
            cli.main(argv)
        self.assertEqual(blocked.exception.code, 1)
        self.assertEqual(_TrialMemory.instances[-1].created, None)

        stdout = io.StringIO()
        with (
            patch.object(cli.Config, "load", return_value=_config("trial")),
            patch.object(cli, "Memory", _TrialMemory),
            patch.object(cli.secrets, "token_hex", return_value=SHA_A),
            patch("sys.stdout", stdout),
        ):
            cli.main(argv)
        created = _TrialMemory.instances[-1].created
        self.assertIsNotNone(created)
        self.assertEqual(created["project_id"], 3)
        self.assertEqual(created["sample_cap"], 40)
        self.assertEqual(created["seed"], SHA_A)
        self.assertEqual(created["evaluator_sha256"], SHA_B)
        self.assertNotIn("prompt", created)
        self.assertIn("Started bounded Phase 4B trial #7", stdout.getvalue())
        self.assertNotIn(SHA_A, stdout.getvalue())

    def test_cli_status_is_sanitized_and_abort_uses_fixed_reason(self):
        stdout = io.StringIO()
        with (
            patch.object(cli.Config, "load", return_value=_config("observe")),
            patch.object(cli, "Memory", _TrialMemory),
            patch("sys.stdout", stdout),
        ):
            cli.main(["strategy-transfer", "status", "--json"])
            cli.main(["strategy-transfer", "abort", "7"])

        output = stdout.getvalue()
        self.assertNotIn("private operator prompt", output)
        self.assertNotIn("private notes", output)
        self.assertIn('"project_id": 3', output)
        self.assertEqual(_TrialMemory.instances[-1].aborted, (7, "operator_abort"))

    def test_cli_promotion_requires_advise_and_explicitly_confirms(self):
        with (
            patch.object(cli.Config, "load", return_value=_config("trial")),
            patch.object(cli, "Memory", _TrialMemory),
            patch("sys.stdout", io.StringIO()),
            self.assertRaises(SystemExit) as blocked,
        ):
            cli.main(["strategy-transfer", "promote", "7"])
        self.assertEqual(blocked.exception.code, 1)
        self.assertIsNone(_TrialMemory.instances[-1].promoted)

        stdout = io.StringIO()
        with (
            patch.object(cli.Config, "load", return_value=_config("advise")),
            patch.object(cli, "Memory", _TrialMemory),
            patch("sys.stdout", stdout),
        ):
            cli.main(["strategy-transfer", "promote", "7"])
        self.assertEqual(_TrialMemory.instances[-1].promoted, (7, True))
        self.assertIn("Explicitly promoted Phase 4B trial #7", stdout.getvalue())

    def test_parser_rejects_unbounded_trial_inputs_before_storage(self):
        parser = cli._parser()
        for option, value in (
            ("--sample-cap", "42"),
            ("--duration-days", "15"),
            ("--seed", "not-a-digest"),
        ):
            argv = [
                "strategy-transfer", "start",
                "--project", "3",
                "--family", "code_fix",
                "--strategy", "verify_output",
                "--sample-cap", "40",
                "--duration-days", "7",
            ]
            if option in argv:
                index = argv.index(option)
                argv[index + 1] = value
            else:
                argv.extend((option, value))
            with self.subTest(option=option), patch(
                "sys.stderr", io.StringIO()
            ), self.assertRaises(SystemExit) as raised:
                parser.parse_args(argv)
            self.assertEqual(raised.exception.code, 2)

    def test_real_persistence_path_derives_pins_and_fails_closed_before_promotion(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = _config("trial")
            config.data_dir = Path(temporary)
            database = config.data_dir / "jarvis.db"
            with DurableMemory(database) as memory:
                benchmark = memory.build_strategy_transfer_benchmark_attestation(
                    run_id="operator-cli-persistence"
                )
                self.assertTrue(memory.record_strategy_transfer_attestation(
                    "sealed_benchmark",
                    benchmark,
                    evaluator_version=benchmark["evaluator_version"],
                    evaluator_sha256=benchmark["evaluator_sha256"],
                    config_sha256=benchmark["config_sha256"],
                ))

            start = [
                "strategy-transfer", "start",
                "--project", "1",
                "--family", "code_fix",
                "--strategy", "verify_output",
                "--sample-cap", "40",
                "--duration-days", "1",
            ]
            stdout = io.StringIO()
            with (
                patch.object(cli.Config, "load", return_value=config),
                patch.object(cli.secrets, "token_hex", return_value=SHA_A),
                patch("sys.stdout", stdout),
            ):
                cli.main(start)
            self.assertIn("Phase 4B trial #1", stdout.getvalue())
            self.assertNotIn(SHA_A, stdout.getvalue())

            with DurableMemory(database) as memory:
                status = memory.strategy_transfer_trial_status(1)
                pins = memory.strategy_transfer_trial_pins()
            self.assertEqual(status["project_id"], 1)
            self.assertEqual(status["sample_cap"], 40)
            self.assertEqual(status["status"], "active")
            self.assertEqual(status["evaluator_sha256"], pins["evaluator_sha256"])
            self.assertEqual(status["runtime_sha256"], pins["runtime_sha256"])
            self.assertFalse(status["promotion_ready"])

            config.strategy_transfer = "advise"
            with (
                patch.object(cli.Config, "load", return_value=config),
                patch("sys.stdout", io.StringIO()),
                patch("sys.stderr", io.StringIO()),
                self.assertRaises(SystemExit) as blocked,
            ):
                cli.main(["strategy-transfer", "promote", "1"])
            self.assertEqual(blocked.exception.code, 1)

            config.strategy_transfer = "trial"
            with (
                patch.object(cli.Config, "load", return_value=config),
                patch("sys.stdout", io.StringIO()),
            ):
                cli.main(["strategy-transfer", "abort", "1"])
            with DurableMemory(database) as memory:
                self.assertEqual(
                    memory.strategy_transfer_trial_status(1)["status"],
                    "aborted",
                )


if __name__ == "__main__":
    unittest.main()
