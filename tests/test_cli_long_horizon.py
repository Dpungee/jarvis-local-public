from __future__ import annotations

import io
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from jarvis import cli
from jarvis.long_horizon import LongHorizonStore
from jarvis.memory import Memory


SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64
SHA_E = "e" * 64


def _config(data_dir: Path) -> SimpleNamespace:
    return SimpleNamespace(data_dir=data_dir)


def _budget() -> dict[str, int]:
    return {
        "elapsed_seconds": 600,
        "tool_calls": 20,
        "model_calls": 10,
        "prompt_tokens": 20_000,
        "completion_tokens": 5_000,
        "retries": 2,
    }


def _manifest(project_id: int, conversation_id: int, task_id: int) -> dict:
    stages = []
    for ordinal, (stage_id, stage_type, mutation_kind) in enumerate((
        ("inspect", "inspect", "none"),
        ("plan", "plan", "none"),
        ("implement", "implement", "reversible"),
        ("verify", "verify", "none"),
        ("finalize", "finalize", "none"),
    ), start=1):
        stages.append({
            "stage_id": stage_id,
            "ordinal": ordinal,
            "stage_type": stage_type,
            "mutation_kind": mutation_kind,
            "budget": _budget(),
        })
    return {
        "schema": "jarvis.long-horizon.workflow-manifest.v1",
        "project_id": project_id,
        "conversation_id": conversation_id,
        "task_id": task_id,
        "goal_sha256": SHA_A,
        "contract_sha256": SHA_B,
        "constraints_sha256": SHA_C,
        "approval_scope_sha256": SHA_D,
        "artifact_set_sha256": SHA_E,
        "budget": _budget(),
        "stages": stages,
    }


class _FakeMemory:
    def __init__(self, _path: Path) -> None:
        pass

    def __enter__(self) -> "_FakeMemory":
        return self

    def __exit__(self, *_args) -> None:
        return None

    def get_project(self, project_id: int):
        if project_id == 3:
            return {"id": 3, "enabled": 1, "name": "Bound project"}
        return None


class _FakeStore:
    def __init__(self, *, project_id: int = 3) -> None:
        self.project_id = project_id
        self.created = None
        self.paused = None
        self.resumed = None
        self.cancelled = None
        self.list_calls: list[dict] = []
        self.row = {
            "schema": "jarvis.long-horizon.status.v1",
            "plan_id": 7,
            "project_id": project_id,
            "conversation_id": 11,
            "task_id": 12,
            "status": "active",
            "manifest_sha256": SHA_A,
            "stage_count": 5,
            "next_stage_ordinal": 2,
            "completed_stages": 1,
            "budget": {**_budget(), "prompt": "private prompt"},
            "usage": {key: 0 for key in _budget()},
            "remaining": _budget(),
            "current_claim": {
                "stage_id": 4,
                "stage_key": "plan",
                "ordinal": 2,
                "claim_owner": "private-user-name",
                "lease_expires_at": "2026-08-31T12:30:00+00:00",
            },
            "checkpoint_head_sha256": SHA_B,
            "mutation_state": {"implement": "not_started"},
            "final_verification": None,
            "quarantine_reason": None,
            "prompt": "secret task prose must never render",
            "notes": "private notes must never render",
        }

    def list_plans(self, **kwargs):
        self.list_calls.append(kwargs)
        return [dict(self.row)]

    def show_plan(self, plan_id: int):
        if plan_id != 7:
            return None
        return dict(self.row)

    def pause_plan(self, plan_id: int, reason_sha256: str):
        self.paused = (plan_id, reason_sha256)
        self.row["status"] = "paused"
        return dict(self.row)

    def resume_plan(self, plan_id: int):
        self.resumed = plan_id
        self.row["status"] = "active"
        return dict(self.row)

    def cancel_plan(self, plan_id: int, reason_sha256: str):
        self.cancelled = (plan_id, reason_sha256)
        self.row["status"] = "cancelled"
        return dict(self.row)

    def create_plan(self, manifest):
        self.created = manifest
        self.row["project_id"] = manifest.project_id
        return 7


class LongHorizonCliTests(unittest.TestCase):
    def _run(self, argv: list[str], store: _FakeStore):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            patch.object(cli.Config, "load", return_value=_config(Path("data"))),
            patch.object(cli, "Memory", _FakeMemory),
            patch.object(cli, "_workflow_store", return_value=store),
            patch.object(
                cli,
                "ensure_provider_ready",
                side_effect=AssertionError("workflow commands must stay prompt-free"),
            ),
            patch("sys.stdout", stdout),
            patch("sys.stderr", stderr),
        ):
            cli.main(argv)
        return stdout.getvalue(), stderr.getvalue()

    def test_parser_requires_exact_project_and_exposes_no_generic_run(self):
        parser = cli._parser()
        with patch("sys.stderr", io.StringIO()), self.assertRaises(SystemExit):
            parser.parse_args(["workflow", "status"])
        with patch("sys.stderr", io.StringIO()), self.assertRaises(SystemExit):
            parser.parse_args(["workflow", "status", "--project", "3", "--limit", "201"])
        with patch("sys.stderr", io.StringIO()), self.assertRaises(SystemExit):
            parser.parse_args(["workflow", "run", "--project", "3"])

    def test_list_json_is_project_scoped_prompt_free_and_whitelisted(self):
        store = _FakeStore()
        output, errors = self._run(
            ["workflow", "list", "--project", "3", "--limit", "9", "--json"],
            store,
        )
        payload = json.loads(output)

        self.assertEqual(errors, "")
        self.assertEqual(store.list_calls, [{"project_id": 3, "limit": 9}])
        self.assertEqual(payload["project_id"], 3)
        self.assertEqual(payload["plans"][0]["plan_id"], 7)
        self.assertNotIn("prompt", payload["plans"][0]["budget"])
        for private in (
            "secret task prose",
            "private prompt",
            "private notes",
            "private-user-name",
        ):
            self.assertNotIn(private, output)

    def test_status_json_contains_counts_not_plan_content(self):
        store = _FakeStore()
        output, _ = self._run(
            ["workflow", "status", "--project", "3", "--json"], store
        )
        payload = json.loads(output)
        self.assertEqual(payload["counts"], {"active": 1})
        self.assertEqual(payload["returned"], 1)
        self.assertNotIn("plans", payload)
        self.assertNotIn("secret task prose", output)

    def test_cross_project_show_and_control_fail_without_revealing_or_mutating(self):
        store = _FakeStore(project_id=4)
        stderr = io.StringIO()
        with (
            patch.object(cli.Config, "load", return_value=_config(Path("data"))),
            patch.object(cli, "Memory", _FakeMemory),
            patch.object(cli, "_workflow_store", return_value=store),
            patch("sys.stdout", io.StringIO()),
            patch("sys.stderr", stderr),
            self.assertRaises(SystemExit) as blocked,
        ):
            cli.main(["workflow", "pause", "7", "--project", "3"])

        self.assertEqual(blocked.exception.code, 1)
        self.assertIsNone(store.paused)
        self.assertNotIn("project #4", stderr.getvalue().casefold())
        self.assertNotIn("private", stderr.getvalue().casefold())

    def test_pause_resume_cancel_use_fixed_digest_reasons_and_confirm_state(self):
        store = _FakeStore()
        pause_output, _ = self._run(
            ["workflow", "pause", "7", "--project", "3", "--json"], store
        )
        self.assertEqual(json.loads(pause_output)["plan"]["status"], "paused")
        self.assertEqual(store.paused, (7, cli._workflow_reason_sha256("pause")))
        self.assertRegex(store.paused[1], r"[0-9a-f]{64}")

        resume_output, _ = self._run(
            ["workflow", "resume", "7", "--project", "3", "--json"], store
        )
        self.assertEqual(json.loads(resume_output)["plan"]["status"], "active")
        self.assertEqual(store.resumed, 7)

        cancel_output, _ = self._run(
            ["workflow", "cancel", "7", "--project", "3", "--json"], store
        )
        self.assertEqual(json.loads(cancel_output)["plan"]["status"], "cancelled")
        self.assertEqual(store.cancelled, (7, cli._workflow_reason_sha256("cancel")))

    def test_start_rejects_executable_unknown_duplicate_and_fractional_inputs(self):
        root = Path(__file__).parent
        manifest_path = root / f".cli-workflow-invalid-{uuid4().hex}.json"
        base = _manifest(3, 11, 12)
        cases: list[tuple[str, str]] = []

        executable = {**base, "shell": "remove everything"}
        cases.append(("executable", json.dumps(executable)))
        cases.append((
            "duplicate",
            json.dumps(base)[:-1] + ',"project_id":3}',
        ))
        fractional = json.loads(json.dumps(base))
        fractional["budget"]["tool_calls"] = 1.5
        cases.append(("fractional", json.dumps(fractional)))

        try:
            for name, content in cases:
                manifest_path.write_text(content, encoding="utf-8")
                store = _FakeStore()
                stderr = io.StringIO()
                with (
                    patch.object(cli.Config, "load", return_value=_config(root)),
                    patch.object(cli, "Memory", _FakeMemory),
                    patch.object(cli, "_workflow_store", return_value=store),
                    patch("sys.stdout", io.StringIO()),
                    patch("sys.stderr", stderr),
                    self.subTest(name=name),
                    self.assertRaises(SystemExit) as blocked,
                ):
                    cli.main([
                        "workflow", "start", "--project", "3",
                        "--manifest", str(manifest_path),
                    ])
                self.assertEqual(blocked.exception.code, 1)
                self.assertIsNone(store.created)
                self.assertNotIn("remove everything", stderr.getvalue())
        finally:
            manifest_path.unlink(missing_ok=True)

    def test_start_honestly_registers_without_claiming_execution(self):
        manifest_path = (
            Path(__file__).parent / f".cli-workflow-register-{uuid4().hex}.json"
        )
        try:
            manifest_path.write_text(
                json.dumps(_manifest(3, 11, 12)), encoding="utf-8"
            )
            store = _FakeStore()
            output, errors = self._run([
                "workflow", "start", "--project", "3",
                "--manifest", str(manifest_path),
            ], store)

            self.assertEqual(errors, "")
            self.assertIsNotNone(store.created)
            self.assertIn("Registered workflow plan #7", output)
            self.assertIn("only registered durable coordination state", output)
            self.assertIn("No shipped component advances stages", output)
            self.assertNotIn("Started workflow", output)
        finally:
            manifest_path.unlink(missing_ok=True)

    def test_real_start_persists_closed_project_bound_manifest(self):
        root = Path(__file__).parent
        token = uuid4().hex
        database = root / f".cli-workflow-{token}.db"
        manifest_path = root / f".cli-workflow-{token}.json"
        try:
            with Memory(database) as memory:
                conversation_id = memory.new_conversation("Phase 5", project_id=1)
                task_id = memory.add_task("bounded workflow", project_id=1)
            manifest_path.write_text(
                json.dumps(_manifest(1, conversation_id, task_id)), encoding="utf-8"
            )

            stdout = io.StringIO()
            with (
                patch.object(cli.Config, "load", return_value=_config(root)),
                patch.object(cli, "Memory", side_effect=lambda _path: Memory(database)),
                patch("sys.stdout", stdout),
            ):
                cli.main([
                    "workflow", "start", "--project", "1",
                    "--manifest", str(manifest_path), "--json",
                ])
            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload["plan"]["project_id"], 1)
            self.assertEqual(payload["plan"]["stage_count"], 5)
            self.assertRegex(payload["plan"]["manifest_sha256"], r"[0-9a-f]{64}")
            self.assertNotIn(str(manifest_path), stdout.getvalue())

            with Memory(database) as memory:
                persisted = LongHorizonStore(memory, project_id=1).show_plan(
                    payload["plan"]["plan_id"]
                )
            self.assertEqual(persisted["project_id"], 1)
            self.assertEqual(persisted["status"], "active")
        finally:
            manifest_path.unlink(missing_ok=True)
            for suffix in ("", "-wal", "-shm", ".memory-spine.key"):
                Path(f"{database}{suffix}").unlink(missing_ok=True)
            Path(f"{database}.long-horizon.key").unlink(missing_ok=True)

    def test_real_cli_controls_remain_atomic_and_project_scoped(self):
        root = Path(__file__).parent
        token = uuid4().hex
        database = root / f".cli-workflow-controls-{token}.db"
        manifest_one = root / f".cli-workflow-controls-one-{token}.json"
        manifest_two = root / f".cli-workflow-controls-two-{token}.json"

        def invoke(argv: list[str]) -> tuple[str, str]:
            stdout = io.StringIO()
            stderr = io.StringIO()
            with (
                patch.object(cli.Config, "load", return_value=_config(root)),
                patch.object(cli, "Memory", side_effect=lambda _path: Memory(database)),
                patch("sys.stdout", stdout),
                patch("sys.stderr", stderr),
            ):
                cli.main(argv)
            return stdout.getvalue(), stderr.getvalue()

        try:
            with Memory(database) as memory:
                project_two = memory.add_project("Second", "@projects/second")
                conversation_one = memory.new_conversation("one", project_id=1)
                task_one = memory.add_task("one", project_id=1)
                conversation_two = memory.new_conversation("two", project_id=project_two)
                task_two = memory.add_task("two", project_id=project_two)
            manifest_one.write_text(
                json.dumps(_manifest(1, conversation_one, task_one)), encoding="utf-8"
            )
            manifest_two.write_text(
                json.dumps(_manifest(project_two, conversation_two, task_two)),
                encoding="utf-8",
            )

            first_output, _ = invoke([
                "workflow", "start", "--project", "1", "--manifest",
                str(manifest_one), "--json",
            ])
            second_output, _ = invoke([
                "workflow", "start", "--project", str(project_two), "--manifest",
                str(manifest_two), "--json",
            ])
            first_id = json.loads(first_output)["plan"]["plan_id"]
            second_id = json.loads(second_output)["plan"]["plan_id"]

            listed, _ = invoke(["workflow", "list", "--project", "1", "--json"])
            listed_payload = json.loads(listed)
            self.assertEqual(
                [plan["plan_id"] for plan in listed_payload["plans"]], [first_id]
            )
            self.assertNotIn(str(manifest_one), listed)
            self.assertNotIn(str(manifest_two), listed)

            paused, _ = invoke([
                "workflow", "pause", str(first_id), "--project", "1", "--json",
            ])
            self.assertEqual(json.loads(paused)["plan"]["status"], "paused")
            resumed, _ = invoke([
                "workflow", "resume", str(first_id), "--project", "1", "--json",
            ])
            self.assertEqual(json.loads(resumed)["plan"]["status"], "active")
            cancelled, _ = invoke([
                "workflow", "cancel", str(first_id), "--project", "1", "--json",
            ])
            self.assertEqual(json.loads(cancelled)["plan"]["status"], "cancelled")

            with (
                patch.object(cli.Config, "load", return_value=_config(root)),
                patch.object(cli, "Memory", side_effect=lambda _path: Memory(database)),
                patch("sys.stdout", io.StringIO()),
                patch("sys.stderr", io.StringIO()),
                self.assertRaises(SystemExit) as blocked,
            ):
                cli.main([
                    "workflow", "show", str(second_id), "--project", "1", "--json",
                ])
            self.assertEqual(blocked.exception.code, 1)

            with Memory(database) as memory:
                first = LongHorizonStore(memory, project_id=1).show_plan(first_id)
                second = LongHorizonStore(
                    memory, project_id=project_two
                ).show_plan(second_id)
            self.assertEqual(first["status"], "cancelled")
            self.assertEqual(second["status"], "active")
        finally:
            manifest_one.unlink(missing_ok=True)
            manifest_two.unlink(missing_ok=True)
            for suffix in ("", "-wal", "-shm", ".memory-spine.key"):
                Path(f"{database}{suffix}").unlink(missing_ok=True)
            Path(f"{database}.long-horizon.key").unlink(missing_ok=True)

    def test_first_status_initializes_sidecar_without_disclosing_it(self):
        root = Path(__file__).parent
        database = root / f".cli-workflow-status-{uuid4().hex}.db"
        key_path = Path(f"{database}.long-horizon.key")
        try:
            with Memory(database):
                pass
            stdout = io.StringIO()
            with (
                patch.object(cli.Config, "load", return_value=_config(root)),
                patch.object(cli, "Memory", side_effect=lambda _path: Memory(database)),
                patch("sys.stdout", stdout),
            ):
                cli.main(["workflow", "status", "--project", "1", "--json"])
            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload["returned"], 0)
            self.assertTrue(key_path.is_file())
            key_bytes = key_path.read_bytes()
            self.assertEqual(len(key_bytes), 32)
            rendered = stdout.getvalue()
            self.assertNotIn(str(database), rendered)
            self.assertNotIn(str(key_path), rendered)
            self.assertNotIn(key_bytes.hex(), rendered)
        finally:
            for suffix in ("", "-wal", "-shm", ".memory-spine.key"):
                Path(f"{database}{suffix}").unlink(missing_ok=True)
            key_path.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
