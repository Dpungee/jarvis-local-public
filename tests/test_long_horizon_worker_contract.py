"""In-process worker contracts supplement, not replace, real restart tests.

The production worker's restricted environment and hard exits stay unchanged.
Only the hard-exit call is intercepted here so its branches are observable.
"""
from __future__ import annotations

import copy
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

from jarvis import long_horizon_eval as evaluator
from jarvis import long_horizon_eval_worker as worker


class SimulatedExit(BaseException):
    pass


class LongHorizonWorkerContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        fixture_path = Path(evaluator.__file__).with_name("evaluation_fixtures") / "long_horizon_restart_holdout_v1.json"
        self.fixture = json.loads(fixture_path.read_text(encoding="utf-8"))

    def request(self, crash="before_mutation", project=1, suffix="run"):
        workflow = next(item for item in self.fixture["workflows"]
                        if item["crash_point"] == crash and item["project_id"] == project)
        template = next(item for item in self.fixture["templates"]
                        if item["template_id"] == workflow["template_id"])
        root = self.root / suffix
        root.mkdir()
        return evaluator._subprocess_request(workflow, template, root)

    def test_each_crash_branch_recovers_one_effect_and_requires_independent_signature(self):
        points = {"before_mutation": worker.CRASH_BEFORE_MUTATION,
                  "after_mutation_before_receipt": worker.CRASH_AFTER_EFFECT,
                  "after_receipt_before_cursor": worker.CRASH_AFTER_CHECKPOINT}
        for index, (point, expected) in enumerate(points.items()):
            with self.subTest(point=point):
                request, verify_key, reconcile_key = self.request(point, 1 + index % 2, point)
                with patch.object(worker.os, "_exit", side_effect=SimulatedExit) as exiting:
                    with self.assertRaises(SimulatedExit):
                        worker.execute(request)
                    exiting.assert_called_once_with(expected)
                time.sleep(1.1)  # expire the real one-second lease, without altering the clock
                worker.recover(request)
                if point == "after_mutation_before_receipt":
                    with patch.dict(os.environ, {"JARVIS_PHASE5_AUTHORITY_PRIVATE_KEY": reconcile_key}):
                        worker.sign(request, reconciliation=True)
                        self.assertNotIn("JARVIS_PHASE5_AUTHORITY_PRIVATE_KEY", os.environ)
                    worker.recover(request)
                with worker._store(request, worker="auditor") as store:
                    plan = store.show_plan(worker._plan_id(request))
                    self.assertNotEqual(plan["status"], "complete")
                with patch.dict(os.environ, {"JARVIS_PHASE5_AUTHORITY_PRIVATE_KEY": verify_key}):
                    worker.sign(request, reconciliation=False)
                    self.assertNotIn("JARVIS_PHASE5_AUTHORITY_PRIVATE_KEY", os.environ)
                with worker._store(request, worker="auditor") as store:
                    self.assertEqual(store.show_plan(worker._plan_id(request))["status"], "complete")
                with closing(sqlite3.connect(request["ledger"])) as db:
                    self.assertEqual(db.execute("SELECT COUNT(*) FROM effects").fetchone()[0], 1)
                self.assertEqual(worker._verified_artifact_sha256(request), request["artifact_sha256"])

    def test_budget_and_cancel_controls_remain_effect_free_when_reopened(self):
        controls = ("cancelled", "budget_time", "budget_tools", "budget_model_calls",
                    "budget_prompt_tokens", "budget_completion_tokens", "budget_retries")
        for kind in controls:
            with self.subTest(kind=kind):
                request, _, _ = self.request(suffix=kind)
                request["control_kind"] = kind
                if kind == "budget_retries":
                    request["budget"]["retries"] = 0
                worker.control_attempt(request)
                worker.control_check(request)
                with closing(sqlite3.connect(request["ledger"])) as db:
                    self.assertEqual(db.execute("SELECT passed,operation_started FROM control_results").fetchall(), [(1, 0)])
                self.assertFalse(Path(request["artifact_path"]).exists())

    def test_artifact_digest_refuses_missing_tampered_and_wrong_material(self):
        request, _, _ = self.request()
        with self.assertRaisesRegex(RuntimeError, "unavailable"):
            worker._verified_artifact_sha256(request)
        invalid = copy.deepcopy(request)
        invalid["artifact_sha256"] = "0" * 64
        with self.assertRaisesRegex(RuntimeError, "sealed digest"):
            worker._write_artifact(invalid)
        self.assertFalse(Path(request["artifact_path"]).exists())
        worker._write_artifact(request)
        self.assertEqual(worker._verified_artifact_sha256(request), request["artifact_sha256"])
        Path(request["artifact_path"]).write_bytes(b"tampered")
        with self.assertRaisesRegex(RuntimeError, "does not match"):
            worker.sign(request, reconciliation=False)

    def test_invalid_secret_is_consumed_but_never_accepted(self):
        for raw in ("", "short", "g" * 64):
            with self.subTest(length=len(raw)), patch.dict(os.environ, {"JARVIS_PHASE5_AUTHORITY_PRIVATE_KEY": raw}):
                with self.assertRaises((RuntimeError, ValueError)):
                    worker._private_key()
                self.assertNotIn("JARVIS_PHASE5_AUTHORITY_PRIVATE_KEY", os.environ)

    def test_usage_caps_and_duplicate_effect_refusal(self):
        self.assertEqual(worker._usage({"budget": {"prompt_tokens": 3, "completion_tokens": 2}}),
                         {"elapsed_seconds": 1, "tool_calls": 1, "model_calls": 1,
                          "prompt_tokens": 3, "completion_tokens": 2})
        request, _, _ = self.request()
        claim = {"effect_key": "synthetic-effect"}
        digest = worker._ledger_effect(request, claim)
        self.assertEqual(digest, worker._sha({"effect_key": "synthetic-effect", "applied": True}))
        with self.assertRaises(sqlite3.IntegrityError):
            worker._ledger_effect(request, claim)
        with closing(sqlite3.connect(request["ledger"])) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM effects").fetchone()[0], 1)

    def test_main_dispatch_records_mode_without_executor_authority_secret(self):
        request, _, _ = self.request()
        path = self.root / "request.json"
        path.write_text(json.dumps(request), encoding="utf-8")
        modes = {"execute": "execute", "recover": "recover", "reconcile": "sign",
                 "verify": "sign", "control-attempt": "control_attempt", "control-check": "control_check"}
        with patch.dict(os.environ, {"JARVIS_PHASE5_AUTHORITY_PRIVATE_KEY": ""}):
            for mode, handler in modes.items():
                with self.subTest(mode=mode), patch.object(worker, handler) as action, patch.object(worker.os.sys, "argv", ["worker", mode, str(path)]):
                    self.assertEqual(worker.main(), 0)
                    if handler == "sign":
                        action.assert_called_once_with(request, reconciliation=mode == "reconcile")
                    else:
                        action.assert_called_once_with(request)
        with closing(sqlite3.connect(request["ledger"])) as db:
            rows = db.execute("SELECT mode,authority_secret_present FROM processes ORDER BY rowid").fetchall()
        self.assertEqual(rows, [(mode, 0) for mode in modes])


if __name__ == "__main__":
    unittest.main()
