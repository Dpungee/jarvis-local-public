"""Tests for the isolated Phase-2 TaskContract outcome toolbox.

Every assertion here exists because the outcome benchmark
(``jarvis/task_contract_benchmark.py``) or its scorer
(``jarvis/task_contract_eval.py``) reads the thing being asserted:

* the runner rejects the production ``ToolBox`` by ``isinstance`` and requires
  ``task_contract_outcome_isolated is True`` (``task_contract_benchmark.py``
  lines 551-561);
* the scorer classifies observations by **production tool name** through
  ``_observed_tool_effect`` (lines 417-446) and reads ``handler_dispatched``,
  ``trace_id``, ``target_sha256``, ``result_receipt_id`` and
  ``matched_constraint_sha256`` out of the durable audit row (lines 611-641);
* ``duplicate_external_effects`` and ``durable_queue_receipt`` are derived from
  durable rows only (``task_contract_eval.py`` lines 1526-1560).
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
import unittest
import urllib.request
import zipfile
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from jarvis.config import Config
from jarvis.memory import Memory
from jarvis.policy import ALLOWED_PROGRAMS
from jarvis.task_contract_benchmark import _observed_tool_effect
from jarvis.task_contract_eval import load_task_contract_holdout
from jarvis.task_contract_outcome_toolbox import (
    EXTERNAL_LEDGER_FILENAME,
    LANE_TOOL_REQUIREMENTS,
    OUTCOME_TOOLBOX_TOOL_NAMES,
    RUN_PROCESS_ALLOWED_PROGRAMS,
    TaskContractOutcomeToolbox,
)
from jarvis.tool_specs import build_tool_specs
from jarvis.tools import (
    FEATURE_SPECS,
    MAX_BATCH_READ_FILES,
    MAX_RESEARCH_QUESTION_RESULTS,
    MAX_SCAN_HOSTS,
    MAX_TOOL_DEFINITION_BYTES,
    MAX_TOOL_OUTPUT,
    SUPPORTED_DOCUMENT_TYPES,
    ToolBox,
)

# The toolbox mirrors production's execution gates, so the benchmark
# configuration must open them explicitly; the ambient JARVIS_EXECUTION_MODE
# gating env var is intentionally overridden per test case in setUp.

TEMP_ROOT = Path(__file__).resolve().parent / ".tmp"
TEMP_ROOT.mkdir(exist_ok=True)
FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "task_contract_holdout_v2.json"
TRACE_ID = "0123456789abcdef0123456789abcdef"

#: The exact constraint list bound while the surface below is exercised, so
#: ``matched_constraint_sha256`` is non-empty for the calls that quote them.
CONTRACT_CONSTRAINTS = (
    "alex@example.com",
    "Crew Update",
    "helmets required",
    "crew-brief.pdf",
    "Every Monday at 7 AM",
)

#: One call per exposed tool, in dependency order (writes before the reads and
#: uploads that consume them).  ``_exercise_surface`` runs the whole list.
SURFACE_CALLS: tuple[tuple[str, dict], ...] = (
    ("tool_catalog", {"query": "write"}),
    ("web_search", {"query": "current IRS mileage rate", "max_results": 3}),
    ("web_fetch", {"url": "https://example.com/irs-mileage"}),
    ("research_question", {"query": "current OSHA heat guidance", "max_results": 2}),
    ("make_directory", {"path": "reports"}),
    (
        "write_file",
        {"path": "reports/notes.txt", "content": "arrival 7 AM\nhelmets required\n"},
    ),
    ("list_files", {"path": ".", "recursive": True}),
    ("read_file", {"path": "reports/notes.txt"}),
    ("search_files", {"pattern": "helmets", "path": "."}),
    ("detect_project", {"path": "."}),
    ("computer_list_files", {"path": "."}),
    ("computer_read_file", {"path": "reports/notes.txt"}),
    ("computer_storage_report", {"path": "."}),
    ("system_snapshot", {}),
    ("windows_list_apps", {"limit": 3}),
    ("screen_companion_status", {}),
    ("screen_companion_control", {"action": "pause"}),
    ("feature_setup_status", {}),
    (
        "feature_setup_decide",
        {"capability_id": "bluetooth-inventory", "decision": "skip"},
    ),
    ("connector_list", {}),
    ("github_repository_status", {"path": "."}),
    ("google_drive_list_files", {"folder_id": "root"}),
    ("vercel_status", {}),
    (
        "build_document",
        {
            "path": "reports/crew-brief.pdf",
            "document_type": "pdf",
            "content": "arrival 7 AM\nhelmets required",
        },
    ),
    (
        "build_document",
        {
            "path": "reports/night-shift.docx",
            "document_type": "docx",
            "content": "lock gate; charge radios; log fuel",
        },
    ),
    ("generate_image", {"prompt": "blue and simple logo", "output": "logo.png"}),
    ("run_process", {"program": "python", "arguments": ["-V"]}),
    (
        "connector_call",
        {
            "connector": "email",
            "action": "send_message",
            "arguments": {
                "to": "alex@example.com",
                "subject": "Crew Update",
                "body": "We start at seven tomorrow.",
            },
        },
    ),
    ("google_drive_create_folder", {"name": "Field Reports"}),
    (
        "google_drive_upload_file",
        {"local_path": "reports/crew-brief.pdf", "folder_id": "root"},
    ),
    ("github_create_repository", {"path": ".", "name": "benchmark-demo"}),
    ("github_push", {"path": ".", "branch": "main"}),
    ("vercel_deploy", {"project_path": ".", "target": "preview"}),
    (
        "schedule_create",
        {
            "name": "Weekly ops summary",
            "task": "Every Monday at 7 AM email ops@example.com the status",
            "interval_minutes": 10_080,
        },
    ),
    (
        "delegate_specialist",
        {"task": "Write and run unit tests for the invoice module in Python"},
    ),
    ("schedule_list", {"limit": 10}),
    (
        "edit_file",
        {
            "path": "reports/notes.txt",
            "old_text": "7 AM",
            "new_text": "6 AM",
            "expected_sha256": "",
        },
    ),
)

#: ``edit_file`` is exercised with a deliberately stale digest so the surface
#: sweep also covers the refusal path; it is the only expected failure.
EXPECTED_SURFACE_FAILURES = frozenset({"edit_file"})


def production_specs() -> dict[str, object]:
    return {
        spec.name: spec
        for spec in build_tool_specs(
            feature_specs=FEATURE_SPECS,
            max_batch_read_files=MAX_BATCH_READ_FILES,
            max_research_question_results=MAX_RESEARCH_QUESTION_RESULTS,
            max_scan_hosts=MAX_SCAN_HOSTS,
            max_tool_definition_bytes=MAX_TOOL_DEFINITION_BYTES,
            max_tool_output=MAX_TOOL_OUTPUT,
            supported_document_types=SUPPORTED_DOCUMENT_TYPES,
        )
    }


class OutcomeToolboxTestCase(unittest.TestCase):
    """Shared runner-owned workspace, database, and context wiring."""

    def setUp(self) -> None:
        self.test_dir = TEMP_ROOT / f"wp3-{os.getpid()}-{self._testMethodName}"
        if self.test_dir.exists():
            shutil.rmtree(self.test_dir)
        self.test_dir.mkdir()
        self.workspace = self.test_dir / "workspace"
        self.data_dir = self.test_dir / "data"
        self.workspace.mkdir()
        self.data_dir.mkdir()
        self.config = replace(
            Config.load(),
            workspace=self.workspace,
            data_dir=self.data_dir,
            execution_mode="trusted-host",
            autonomy="autonomous",
            external_access="disabled",
            self_inspect="disabled",
        )
        self.memory = Memory(self.data_dir / "jarvis.db")
        self.conversation_id = self.memory.new_conversation("phase 2 outcome toolbox")
        project = self.memory.conversation_project(self.conversation_id)
        self.project_id = int(project["id"])
        self.toolbox = TaskContractOutcomeToolbox(
            self.config, self.memory, workspace=self.workspace
        )

    def tearDown(self) -> None:
        self.memory.close()
        resolved = self.test_dir.resolve()
        self.assertEqual(resolved.parent, TEMP_ROOT.resolve())
        shutil.rmtree(resolved, ignore_errors=True)

    def bound_contexts(self, toolbox: object):
        """Enter the same seams the Agent enters around a dispatch."""

        from contextlib import ExitStack

        stack = ExitStack()
        stack.enter_context(toolbox.approval_context("conversation", task_id=None))
        stack.enter_context(
            toolbox.agent_context(
                self.project_id,
                conversation_id=self.conversation_id,
                trace_id=TRACE_ID,
            )
        )
        stack.enter_context(toolbox.effect_contract_context(CONTRACT_CONSTRAINTS))
        return stack

    def exercise_surface(self, toolbox: object | None = None) -> list[tuple[str, dict]]:
        target = self.toolbox if toolbox is None else toolbox
        results: list[tuple[str, dict]] = []
        with self.bound_contexts(target):
            for name, arguments in SURFACE_CALLS:
                results.append((name, json.loads(target.execute(name, arguments))))
        return results

    def tool_audit_rows(self) -> list[dict]:
        return [
            {**row, "details": json.loads(str(row["details_json"] or "{}"))}
            for row in reversed(self.memory.list_activity(limit=10_000))
            if str(row["category"]) == "tool"
        ]


class SeamContractTests(OutcomeToolboxTestCase):
    def test_runner_seam_checks_accept_this_toolbox_and_reject_production(self):
        # task_contract_benchmark.py:551-561 - both checks, in the runner's order.
        self.assertFalse(isinstance(self.toolbox, ToolBox))
        self.assertIs(
            getattr(self.toolbox, "task_contract_outcome_isolated", False), True
        )
        production = ToolBox(self.config, self.memory)
        self.assertIsInstance(production, ToolBox)
        self.assertIsNot(
            getattr(production, "task_contract_outcome_isolated", False), True
        )

    def test_agent_reachable_seams_exist_with_the_production_signatures(self):
        # agent.py:12157-12177 and :16374-16386 reach these five by getattr.
        for seam in (
            "schemas",
            "execute",
            "approval_context",
            "agent_context",
            "effect_contract_context",
            "image_attachment_context",
        ):
            self.assertTrue(hasattr(self.toolbox, seam), seam)
        with self.toolbox.approval_context("conversation", task_id=7):
            with self.toolbox.agent_context(
                self.project_id,
                conversation_id=self.conversation_id,
                specialist_key=None,
                model_budget_scope=None,
                trace_id=TRACE_ID,
            ):
                with self.toolbox.image_attachment_context(()):
                    with self.toolbox.effect_contract_context(["helmets required"]):
                        payload = json.loads(
                            self.toolbox.execute("system_snapshot", {})
                        )
        self.assertTrue(payload["ok"])
        row = self.tool_audit_rows()[-1]
        self.assertEqual(row["task_id"], 7)
        self.assertEqual(row["details"]["trace_id"], TRACE_ID)

    def test_module_import_does_not_pull_in_the_agent(self):
        code = (
            "import sys\n"
            "import jarvis.task_contract_outcome_toolbox\n"
            "print('jarvis.agent' in sys.modules)\n"
        )
        completed = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            check=True,
            cwd=str(Path(__file__).resolve().parent.parent),
        )
        self.assertEqual(completed.stdout.strip(), "False")


class AuditRowShapeTests(OutcomeToolboxTestCase):
    def test_audit_row_matches_the_production_toolbox_row_for_the_same_call(self):
        call = ("write_file", {"path": "audit.txt", "content": "helmets required\n"})
        production = ToolBox(self.config, self.memory)
        with self.bound_contexts(production):
            production_payload = json.loads(production.execute(*call))
        production_row = self.tool_audit_rows()[-1]
        with self.bound_contexts(self.toolbox):
            isolated_payload = json.loads(self.toolbox.execute(*call))
        isolated_row = self.tool_audit_rows()[-1]

        self.assertTrue(production_payload["ok"])
        self.assertTrue(isolated_payload["ok"])
        self.assertEqual(production_row["category"], isolated_row["category"])
        self.assertEqual(production_row["action"], isolated_row["action"])
        self.assertEqual(production_row["status"], isolated_row["status"])
        self.assertEqual(
            set(production_row["details"]), set(isolated_row["details"])
        )
        for key, value in production_row["details"].items():
            self.assertIsInstance(
                isolated_row["details"][key], type(value), f"type drift on {key}"
            )
        # The five fields the scorer reads must be identical, not merely shaped
        # alike: they are computed from the same tools.py helpers.
        for key in ("argument_names", "handler_dispatched", "trace_id",
                    "target_sha256", "matched_constraint_sha256"):
            self.assertEqual(
                production_row["details"][key],
                isolated_row["details"][key],
                f"scorer field drift on {key}",
            )

    def test_every_status_is_a_subset_of_production_statuses(self):
        results = self.exercise_surface()
        failed = {name for name, payload in results if payload.get("ok") is not True}
        self.assertEqual(failed, set(EXPECTED_SURFACE_FAILURES))
        statuses = {row["status"] for row in self.tool_audit_rows()}
        # tools.py:2386 emits only these two; "blocked" is mapped by the
        # benchmark but production can never produce it.
        self.assertTrue(statuses)
        self.assertTrue(statuses.issubset({"complete", "failed"}), statuses)
        self.assertNotIn("blocked", statuses)

    def test_receipt_and_constraint_fields_reach_the_audit_row(self):
        with self.bound_contexts(self.toolbox):
            payload = json.loads(self.toolbox.execute(
                "schedule_create",
                {
                    "name": "Every Monday at 7 AM ops digest",
                    "task": "Every Monday at 7 AM email ops@example.com",
                    "interval_minutes": 10_080,
                },
            ))
        self.assertTrue(payload["ok"])
        row = self.tool_audit_rows()[-1]
        self.assertEqual(row["details"]["result_receipt_id"], str(payload["result"]["id"]))
        self.assertTrue(row["details"]["target_sha256"])
        self.assertTrue(row["details"]["matched_constraint_sha256"])

    def test_an_unknown_tool_writes_no_audit_row(self):
        before = len(self.tool_audit_rows())
        with self.bound_contexts(self.toolbox):
            payload = json.loads(self.toolbox.execute("no_such_tool", {}))
        self.assertFalse(payload["ok"])
        self.assertEqual(len(self.tool_audit_rows()), before)


class NetworkDenialTests(OutcomeToolboxTestCase):
    """In-process network denial for the toolbox's own handlers.

    **Scope limit, stated so it is not over-read.**  These patches live in this
    interpreter, so they cannot see a *child process*.  ``run_process`` starts a
    real subprocess through the production pipeline, and a model-authored script
    running under it can open sockets with the user's own authority; the Windows
    Job Object bounds that process tree's lifetime, not its privilege.  See
    divergence 4 in ``jarvis/task_contract_outcome_toolbox.py`` and
    ``ChildProcessContainmentTests`` below for what is and is not contained.
    """

    def test_no_tool_attempts_a_network_connection(self):
        attempts: list[str] = []

        def refuse(label: str):
            def _refuse(*args, **kwargs):
                attempts.append(label)
                raise AssertionError(f"the isolated outcome toolbox called {label}")

            return _refuse

        with patch.object(socket, "socket", refuse("socket.socket")), \
                patch.object(socket, "create_connection", refuse("create_connection")), \
                patch.object(socket, "getaddrinfo", refuse("getaddrinfo")), \
                patch.object(urllib.request, "urlopen", refuse("urlopen")):
            # Guard against a vacuous pass: the patch must actually bite.
            with self.assertRaises(AssertionError):
                socket.create_connection(("example.com", 443))
            attempts.clear()
            results = self.exercise_surface()
        self.assertEqual(attempts, [])
        failed = {name for name, payload in results if payload.get("ok") is not True}
        self.assertEqual(failed, set(EXPECTED_SURFACE_FAILURES))
        for row in self.tool_audit_rows():
            self.assertIn(row["status"], {"complete", "failed"})


class ExactlyOnceExternalEffectTests(OutcomeToolboxTestCase):
    CALL = (
        "connector_call",
        {
            "connector": "email",
            "action": "send_message",
            "arguments": {
                "to": "alex@example.com",
                "subject": "Crew Update",
                "body": "We start at seven tomorrow.",
            },
        },
    )

    def test_replaying_an_external_call_creates_no_second_effect(self):
        with self.bound_contexts(self.toolbox):
            first = json.loads(self.toolbox.execute(*self.CALL))
            second = json.loads(self.toolbox.execute(*self.CALL))
        self.assertTrue(first["ok"])
        self.assertTrue(second["ok"])
        self.assertEqual(len(self.toolbox.external_effect_ledger), 1)
        self.assertFalse(first["result"]["replayed"])
        self.assertTrue(second["result"]["replayed"])
        self.assertEqual(
            first["result"]["receipt_id"], second["result"]["receipt_id"]
        )
        self.assertEqual(
            self.toolbox.external_effect_ledger[0].receipt_id,
            first["result"]["receipt_id"],
        )
        # Both dispatches are still audited: duplicate_external_effects
        # (task_contract_eval.py:1526-1539) must remain a genuine observation.
        rows = [row for row in self.tool_audit_rows() if row["action"] == "connector_call"]
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row["details"]["handler_dispatched"] for row in rows))

    def test_distinct_external_targets_create_distinct_effects(self):
        with self.bound_contexts(self.toolbox):
            self.toolbox.execute(*self.CALL)
            other = dict(self.CALL[1])
            other["arguments"] = {**other["arguments"], "subject": "Crew Update 2"}
            json.loads(self.toolbox.execute("connector_call", other))
        ledger = self.toolbox.external_effect_ledger
        self.assertEqual(len(ledger), 2)
        self.assertNotEqual(ledger[0].receipt_id, ledger[1].receipt_id)
        self.assertEqual([effect.sequence for effect in ledger], [1, 2])

    def test_every_external_tool_records_exactly_one_ledger_entry(self):
        self.exercise_surface()
        external_tools = [
            name
            for name in OUTCOME_TOOLBOX_TOOL_NAMES
            if _observed_tool_effect(name) == "external"
        ]
        recorded = {effect.tool for effect in self.toolbox.external_effect_ledger}
        self.assertEqual(recorded, set(external_tools))
        self.assertEqual(
            len(self.toolbox.external_effect_ledger), len(external_tools)
        )


class ContainmentTests(OutcomeToolboxTestCase):
    def refuse(self, name: str, arguments: dict) -> dict:
        with self.bound_contexts(self.toolbox):
            payload = json.loads(self.toolbox.execute(name, arguments))
        self.assertFalse(payload.get("ok"), f"{name} accepted {arguments}")
        return payload

    def test_parent_traversal_and_outside_absolute_paths_are_refused(self):
        outside = self.test_dir / "outside.txt"
        outside.write_bytes(b"secret\n")
        for name, arguments in (
            ("read_file", {"path": "../outside.txt"}),
            ("read_file", {"path": str(outside)}),
            ("write_file", {"path": "../escape.txt", "content": "no"}),
            ("write_file", {"path": str(self.test_dir / "escape.txt"), "content": "no"}),
            ("make_directory", {"path": "../escape-dir"}),
            ("list_files", {"path": ".."}),
            ("search_files", {"pattern": "secret", "path": ".."}),
            ("computer_read_file", {"path": str(outside)}),
            ("computer_list_files", {"path": ".."}),
            ("generate_image", {"prompt": "x", "output": "../escape.png"}),
            (
                "build_document",
                {"path": "../escape.pdf", "document_type": "pdf", "content": "x"},
            ),
            ("google_drive_upload_file", {"local_path": str(outside)}),
            ("github_push", {"path": "..", "branch": "main"}),
            ("vercel_deploy", {"project_path": ".."}),
            ("run_process", {"program": "python", "arguments": ["../escape.py"]}),
            ("run_process", {"program": "python", "cwd": ".."}),
        ):
            with self.subTest(tool=name, arguments=arguments):
                self.refuse(name, arguments)
        self.assertFalse((self.test_dir / "escape.txt").exists())
        self.assertFalse((self.test_dir / "escape-dir").exists())
        self.assertFalse((self.test_dir / "escape.png").exists())
        self.assertEqual(outside.read_bytes(), b"secret\n")

    def test_a_refusal_is_a_failed_audit_row_not_an_escaping_exception(self):
        self.refuse("read_file", {"path": "../outside.txt"})
        row = self.tool_audit_rows()[-1]
        self.assertEqual(row["action"], "read_file")
        self.assertEqual(row["status"], "failed")
        self.assertTrue(row["details"]["handler_dispatched"])
        self.assertNotIn("result_receipt_id", row["details"])

    def test_invalid_arguments_fail_before_dispatch(self):
        with self.bound_contexts(self.toolbox):
            payload = json.loads(self.toolbox.execute("read_file", {"path": 17}))
        self.assertFalse(payload["ok"])
        row = self.tool_audit_rows()[-1]
        self.assertEqual(row["status"], "failed")
        self.assertFalse(row["details"]["handler_dispatched"])
        self.assertNotIn("target_sha256", row["details"])

    def test_a_symlink_pointing_outside_the_workspace_is_refused(self):
        outside = self.test_dir / "outside-target.txt"
        outside.write_bytes(b"secret\n")
        link = self.workspace / "linked.txt"
        try:
            os.symlink(outside, link)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlink creation unavailable on this host: {exc}")
        self.refuse("read_file", {"path": "linked.txt"})
        self.refuse("write_file", {"path": "linked.txt", "content": "no"})
        self.assertEqual(outside.read_bytes(), b"secret\n")

    def test_alternate_data_streams_and_drive_relative_paths_are_refused(self):
        # policy.py:301-302 rejects ADS; _contained must do the same or a write
        # to "inside.txt:evil" lands in a stream the containment walk cannot see.
        for name, arguments in (
            ("write_file", {"path": "inside.txt:evil", "content": "no"}),
            ("write_file", {"path": "reports/inside.txt:$DATA", "content": "no"}),
            ("read_file", {"path": "inside.txt:evil"}),
            ("make_directory", {"path": "C:evil"}),
            ("generate_image", {"prompt": "x", "output": "logo.png:evil"}),
        ):
            with self.subTest(tool=name, arguments=arguments):
                self.refuse(name, arguments)
        self.assertEqual(list(self.workspace.rglob("*")), [])

    def test_execution_uses_the_production_policy_screen(self):
        # The allowlist is production's, reached through validate_process, not a
        # second list maintained in the benchmark toolbox.
        self.assertEqual(RUN_PROCESS_ALLOWED_PROGRAMS, ALLOWED_PROGRAMS)
        for program in ("cmd", "powershell", "bash", "curl", "pip"):
            with self.subTest(program=program):
                self.refuse("run_process", {"program": program})
        for arguments in (
            ["-c", "print(1)"],           # inline code
            ["-cprint(1)"],               # HIGH-1: the glued form
            ["-Xutf8", "-cprint(1)"],     # glued behind another flag
            ["-ic", "print(1)"],          # clustered interactive
            ["-"],                        # stdin program
            ["-m", "site"],               # HIGH-1: module outside the allowlist
            ["-m", "pip", "--version"],   # HIGH-1: pip
            ["-msite"],                   # glued module flag
            ["../escape.py"],             # workspace escape
            ["\\\\server\\share\\x.py"],  # UNC
            ["C:evil.py"],                # drive-relative
            ["@response.txt"],            # response file
            ["%APPDATA%/x.py"],           # environment expansion
            ["https://example.com/x.py"],  # URL
            ["inside.py:evil"],           # alternate data stream
        ):
            with self.subTest(arguments=arguments):
                self.refuse("run_process", {"program": "python", "arguments": arguments})
        with self.bound_contexts(self.toolbox):
            payload = json.loads(
                self.toolbox.execute("run_process", {"program": "python", "arguments": ["-V"]})
            )
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["result"]["exit_code"], 0)
        self.assertFalse(payload["result"]["timed_out"])
        self.assertEqual(payload["result"]["execution_backend"], "host")

    def test_execution_honours_the_production_configuration_gates(self):
        for overrides in (
            {"execution_mode": "disabled"},
            {"autonomy": "readonly"},
        ):
            with self.subTest(overrides=overrides):
                toolbox = TaskContractOutcomeToolbox(
                    replace(self.config, **overrides),
                    self.memory,
                    workspace=self.workspace,
                    data_dir=self.data_dir,
                )
                with self.bound_contexts(toolbox):
                    payload = json.loads(
                        toolbox.execute("run_process", {"program": "python", "arguments": ["-V"]})
                    )
                self.assertFalse(payload["ok"])

    def test_the_data_directory_may_not_sit_inside_the_scored_workspace(self):
        with self.assertRaises(ValueError):
            TaskContractOutcomeToolbox(
                self.config,
                self.memory,
                workspace=self.workspace,
                data_dir=self.workspace / "scratch",
            )
        self.assertFalse((self.workspace / "scratch").exists())


class ChildProcessContainmentTests(OutcomeToolboxTestCase):
    """What the execution pipeline does and does not contain.

    Two properties are proved here.  (1) A path-escape *argument* never reaches
    a child at all - ``policy.validate_process`` rejects it before anything is
    spawned, so a child cannot be pointed at a file outside the workspace.
    (2) A process tree, grandchildren included, does not outlive its wall-clock
    limit, because ``execution.HostBackend`` runs it inside a kill-on-close
    Windows Job Object.

    What is deliberately **not** claimed: that a model-authored script already
    inside the workspace is sandboxed.  It runs with the user's own authority
    and can reach the network and the wider filesystem, exactly as production's
    ``run_process`` allows.  The Job Object bounds lifetime, not privilege.
    """

    def run_process(self, arguments: list[str], timeout: int | None = None) -> dict:
        payload = {"program": "python", "arguments": arguments}
        if timeout is not None:
            payload["timeout"] = timeout
        with self.bound_contexts(self.toolbox):
            return json.loads(self.toolbox.execute("run_process", payload))

    def test_a_child_cannot_be_pointed_outside_the_workspace_by_an_argument(self):
        outside = self.test_dir / "child-escape.txt"
        self.assertFalse(outside.exists())
        # A real script that would write outside if it were ever given the path.
        script = self.workspace / "writer.py"
        script.write_bytes(
            b"import sys\n"
            b"from pathlib import Path\n"
            b"Path(sys.argv[1]).write_text('escaped', encoding='utf-8')\n"
        )
        for escape in (
            str(outside),
            "../child-escape.txt",
            "..\\child-escape.txt",
        ):
            with self.subTest(escape=escape):
                payload = self.run_process(["writer.py", escape])
                self.assertFalse(payload["ok"], escape)
                self.assertIn("workspace", str(payload["error"]).casefold())
        self.assertFalse(
            outside.exists(),
            "a child process wrote outside the runner workspace",
        )
        row = self.tool_audit_rows()[-1]
        self.assertEqual(row["action"], "run_process")
        self.assertEqual(row["status"], "failed")

    def test_a_detached_grandchild_does_not_outlive_the_timeout(self):
        marker = self.workspace / "grandchild-marker.txt"
        grandchild = self.workspace / "grandchild.py"
        grandchild.write_bytes(
            b"import time\n"
            b"from pathlib import Path\n"
            b"time.sleep(20)\n"
            b"Path(__file__).with_name('grandchild-marker.txt').write_text(\n"
            b"    'survived', encoding='utf-8')\n"
        )
        parent = self.workspace / "parent.py"
        parent.write_bytes(
            b"import subprocess\n"
            b"import sys\n"
            b"import time\n"
            b"from pathlib import Path\n"
            b"child = Path(__file__).with_name('grandchild.py')\n"
            b"subprocess.Popen([sys.executable, str(child)])\n"
            b"print('spawned', flush=True)\n"
            b"time.sleep(120)\n"
        )
        started = time.monotonic()
        payload = self.run_process(["parent.py"], timeout=3)
        elapsed = time.monotonic() - started
        self.assertTrue(payload["ok"] is False or payload["result"]["timed_out"])
        result = payload.get("result") if payload.get("ok") else None
        if result is not None:
            self.assertTrue(result["timed_out"])
            self.assertIn("process tree was terminated", str(result["error"]))
        # The parent slept for 120 s and the grandchild for 20 s; the job object
        # must reap both well before either could finish.
        self.assertLess(elapsed, 25.0, f"timeout enforcement took {elapsed:.1f}s")
        # Give a survivor ample time to write its marker, then prove none did.
        deadline = time.monotonic() + 22.0
        while time.monotonic() < deadline:
            if marker.exists():
                break
            time.sleep(0.5)
        self.assertFalse(
            marker.exists(),
            "a detached grandchild outlived the run_process wall-clock limit",
        )

    def test_a_contained_script_runs_and_reports_its_exit_code(self):
        script = self.workspace / "ok.py"
        script.write_bytes(b"print('contained run')\n")
        payload = self.run_process(["ok.py"])
        self.assertTrue(payload["ok"], payload)
        self.assertEqual(payload["result"]["exit_code"], 0)
        self.assertIn("contained run", payload["result"]["stdout"])
        failing = self.workspace / "fail.py"
        failing.write_bytes(b"raise SystemExit(3)\n")
        payload = self.run_process(["fail.py"])
        # A non-zero exit is a failed tool result via tools._tool_result_failed.
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["result"]["exit_code"], 3)

    def test_output_is_bounded_before_it_reaches_the_model(self):
        script = self.workspace / "loud.py"
        script.write_bytes(
            b"import sys\n"
            b"for _ in range(40000):\n"
            b"    sys.stdout.write('x' * 200 + chr(10))\n"
        )
        payload = self.run_process(["loud.py"], timeout=60)
        self.assertTrue(payload["ok"], payload)
        # Three bounds, none of them this module's invention: _BoundedCollector
        # caps the in-memory read at 1 MB (execution.py:21), _trim applies
        # MAX_TOOL_OUTPUT (tools.py:72, :296), and _serialize_tool_response
        # re-bounds the envelope.  8 MB of child output must not reach the model.
        self.assertTrue(payload.get("truncated"))
        self.assertLessEqual(len(payload["result"]["stdout"]), MAX_TOOL_OUTPUT)
        self.assertLessEqual(len(json.dumps(payload)), MAX_TOOL_OUTPUT)


class LedgerPersistenceTests(OutcomeToolboxTestCase):
    CALL = (
        "connector_call",
        {
            "connector": "email",
            "action": "send_message",
            "arguments": {"to": "alex@example.com", "subject": "Crew Update", "body": "x"},
        },
    )

    def test_a_re_instantiated_toolbox_replays_instead_of_re_effecting(self):
        with self.bound_contexts(self.toolbox):
            first = json.loads(self.toolbox.execute(*self.CALL))
        self.assertTrue(first["ok"])
        ledger_file = self.data_dir / EXTERNAL_LEDGER_FILENAME
        self.assertTrue(ledger_file.is_file())

        # A restart-tagged case re-enters the same case data directory.
        reborn = TaskContractOutcomeToolbox(
            self.config, self.memory, workspace=self.workspace, data_dir=self.data_dir
        )
        self.assertEqual(len(reborn.external_effect_ledger), 1)
        with self.bound_contexts(reborn):
            replay = json.loads(reborn.execute(*self.CALL))
        self.assertTrue(replay["ok"])
        self.assertTrue(replay["result"]["replayed"])
        self.assertEqual(
            replay["result"]["receipt_id"], first["result"]["receipt_id"]
        )
        self.assertEqual(len(reborn.external_effect_ledger), 1)
        document = json.loads(ledger_file.read_text(encoding="utf-8"))
        self.assertEqual(document["version"], 1)
        self.assertEqual(len(document["effects"]), 1)

    def test_the_ledger_never_lands_in_the_scored_workspace(self):
        with self.bound_contexts(self.toolbox):
            self.toolbox.execute(*self.CALL)
            self.toolbox.execute("run_process", {"program": "python", "arguments": ["-V"]})
        self.assertEqual(
            [item.name for item in self.workspace.rglob("*")],
            [],
            "benchmark scratch leaked into the scored workspace",
        )

    def test_a_different_case_directory_starts_with_an_empty_ledger(self):
        with self.bound_contexts(self.toolbox):
            self.toolbox.execute(*self.CALL)
        next_case = self.test_dir / "case-2"
        (next_case / "workspace").mkdir(parents=True)
        (next_case / "data").mkdir(parents=True)
        other = TaskContractOutcomeToolbox(
            self.config,
            self.memory,
            workspace=next_case / "workspace",
            data_dir=next_case / "data",
        )
        self.assertEqual(other.external_effect_ledger, ())


class NameAndSchemaCoverageTests(OutcomeToolboxTestCase):
    def test_every_exposed_tool_uses_the_production_name_and_schema(self):
        specs = production_specs()
        exposed = {
            schema["function"]["name"]: schema["function"]
            for schema in self.toolbox.schemas
        }
        self.assertEqual(set(exposed), set(OUTCOME_TOOLBOX_TOOL_NAMES))
        for name, function in exposed.items():
            with self.subTest(tool=name):
                self.assertIn(name, specs, f"{name} is not a production tool name")
                self.assertEqual(function["parameters"], specs[name].parameters)
                self.assertEqual(function["description"], specs[name].description)

    def test_every_fixture_lane_has_the_tools_its_cases_need(self):
        """Derive the lane->tools requirement from all 66 fixture cases.

        The mapping asserted here is:

        =============== ================================================
        lane            production tools this toolbox must expose
        =============== ================================================
        dialogue        (none - all 12 cases are action_timing "none",
                        where the scorer counts any dispatched effect as
                        unexpected)
        research        research_question, web_search, web_fetch,
                        read_file, list_files
        creation        write_file, edit_file, make_directory,
                        build_document, generate_image, run_process,
                        detect_project, read_file, list_files,
                        research_question
        inspection      list_files, read_file, search_files,
                        computer_list_files, computer_read_file,
                        computer_storage_report, system_snapshot,
                        windows_list_apps
        external_action connector_list, connector_call,
                        google_drive_authenticate,
                        google_drive_list_files,
                        google_drive_create_folder,
                        google_drive_upload_file,
                        github_repository_status,
                        github_create_repository, github_push,
                        vercel_status, vercel_deploy, schedule_create
        configuration   feature_setup_status, feature_setup_decide,
                        screen_companion_status,
                        screen_companion_control
        =============== ================================================
        """

        fixture = load_task_contract_holdout(FIXTURE_PATH)
        cases = fixture["cases"]
        self.assertEqual(len(cases), 66)
        exposed = {schema["function"]["name"] for schema in self.toolbox.schemas}
        lanes = {str(case["expected"]["lane"]) for case in cases}
        self.assertEqual(lanes, set(LANE_TOOL_REQUIREMENTS))

        effects_by_lane: dict[str, set[str]] = {
            lane: {
                _observed_tool_effect(name) for name in LANE_TOOL_REQUIREMENTS[lane]
            }
            for lane in lanes
        }
        for case in cases:
            expected = case["expected"]
            lane = str(expected["lane"])
            with self.subTest(case=case["id"]):
                required = LANE_TOOL_REQUIREMENTS[lane]
                self.assertTrue(
                    required.issubset(exposed),
                    f"{lane} is missing {sorted(required - exposed)}",
                )
                timing = str(expected["action_timing"])
                if timing == "immediate":
                    # immediate_action_evidence (task_contract_eval.py:1587-1599)
                    # needs a completed event whose effect equals this value.
                    self.assertIn(
                        str(expected["requested_effect"]),
                        effects_by_lane[lane],
                        f"{lane} exposes no {expected['requested_effect']} tool",
                    )
                elif timing == "future":
                    # durable_queue_receipt (task_contract_eval.py:1540-1560)
                    # needs a completed schedule_create event.
                    self.assertIn("queue", effects_by_lane[lane])
                    self.assertIn("schedule_create", required)

    def test_effect_classification_of_every_exposed_tool_is_recorded(self):
        classified: dict[str, list[str]] = {}
        for name in OUTCOME_TOOLBOX_TOOL_NAMES:
            classified.setdefault(_observed_tool_effect(name), []).append(name)
        self.assertEqual(
            sorted(classified),
            ["execute", "external", "queue", "read", "write"],
        )
        self.assertEqual(classified["queue"], ["schedule_create", "delegate_specialist"])
        self.assertEqual(classified["execute"], ["run_process"])
        self.assertEqual(
            sorted(classified["external"]),
            [
                "connector_call",
                "github_create_repository",
                "github_push",
                "google_drive_create_folder",
                "google_drive_upload_file",
                "vercel_deploy",
            ],
        )
        # google_drive_authenticate is deliberately NOT exposed: it is in both
        # MUTATING_TOOLS and EXTERNAL_MUTATION_TOOLS, so an ordinary
        # authenticate-then-upload workflow would register two external effects
        # and score a duplicate at task_contract_eval.py:1526-1539.
        self.assertNotIn("google_drive_authenticate", OUTCOME_TOOLBOX_TOOL_NAMES)
        self.assertEqual(_observed_tool_effect("google_drive_authenticate"), "external")
        # Documented gap: production's screen_companion_control is absent from
        # MUTATING_TOOLS, so the configuration lane's only write-classified tool
        # is feature_setup_decide.  This is a property of the production name
        # sets, asserted here so it cannot change silently.
        self.assertEqual(_observed_tool_effect("screen_companion_control"), "read")
        self.assertEqual(_observed_tool_effect("feature_setup_decide"), "write")


class DurableRowTests(OutcomeToolboxTestCase):
    def test_schedule_create_and_delegate_specialist_produce_durable_rows(self):
        database = self.data_dir / "jarvis.db"
        with self.bound_contexts(self.toolbox):
            schedule = json.loads(self.toolbox.execute(
                "schedule_create",
                {
                    "name": "Weekly backup upload",
                    "task": "each friday upload reports/week.pdf to Drive",
                    "interval_minutes": 10_080,
                },
            ))
            delegation = json.loads(self.toolbox.execute(
                "delegate_specialist",
                {"task": "Write and run unit tests for the invoice module in Python"},
            ))
        self.assertTrue(schedule["ok"])
        self.assertTrue(delegation["ok"])
        self.memory.close()

        # A second open proves the rows are durable SQLite state, exactly as the
        # runner proves it (task_contract_benchmark.py:585-640).
        reopened = Memory(database)
        try:
            jobs = reopened.list_scheduled_jobs(limit=200)
            tasks = reopened.list_tasks(limit=200)
        finally:
            reopened.close()
        self.memory = Memory(database)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(str(jobs[0]["id"]), str(schedule["result"]["id"]))
        self.assertIn("each friday", str(jobs[0]["prompt"]))
        self.assertEqual(len(tasks), 1)
        self.assertEqual(int(tasks[0]["id"]), int(delegation["result"]["task_id"]))
        self.assertEqual(str(tasks[0]["status"]), "queued")

    def test_written_artifacts_are_real_files_in_the_runner_workspace(self):
        self.exercise_surface()
        pdf = self.workspace / "reports" / "crew-brief.pdf"
        docx = self.workspace / "reports" / "night-shift.docx"
        png = self.workspace / "logo.png"
        self.assertTrue(pdf.is_file())
        self.assertTrue(docx.is_file())
        self.assertTrue(png.is_file())
        self.assertTrue(pdf.read_bytes().startswith(b"%PDF-"))
        self.assertTrue(pdf.read_bytes().rstrip().endswith(b"%%EOF"))
        self.assertTrue(png.read_bytes().startswith(b"\x89PNG\r\n\x1a\n"))
        with zipfile.ZipFile(docx) as archive:
            self.assertIn("[Content_Types].xml", archive.namelist())
            self.assertEqual(archive.testzip(), None)
        for item in self.workspace.rglob("*"):
            self.assertTrue(
                item.resolve().is_relative_to(self.workspace.resolve()), item
            )


class RunnerObservationTests(OutcomeToolboxTestCase):
    """Reproduce the runner's own collection code over these audit rows.

    ``run_isolated_task_contract_outcome_benchmark`` builds ``tool_events``
    inline (``task_contract_benchmark.py`` lines 611-641), so it cannot be
    imported.  The construction below is copied statement for statement from
    that block; if it ever drifts, this test is the place the drift shows up.
    """

    def collect_tool_events(self, database: Path, trace_id: str) -> list[dict]:
        reopened = Memory(database)
        try:
            events: list[dict] = []
            for item in reopened.list_activity(limit=10_000):
                if str(item.get("category") or "") != "tool":
                    continue
                try:
                    details = json.loads(str(item.get("details_json") or "{}"))
                except json.JSONDecodeError:
                    continue
                if str(details.get("trace_id") or "") != trace_id:
                    continue
                events.append({
                    "name": str(item.get("action") or "unknown"),
                    "status": (
                        "complete"
                        if str(item.get("status") or "") == "complete"
                        else "blocked"
                        if str(item.get("status") or "") == "blocked"
                        else "failed"
                    ),
                    "handler_dispatched": bool(details.get("handler_dispatched") is True),
                    "effect": _observed_tool_effect(str(item.get("action") or "unknown")),
                    "target_sha256": (
                        str(details.get("target_sha256"))
                        if isinstance(details.get("target_sha256"), str)
                        else None
                    ),
                    "receipt_id": (
                        str(details.get("result_receipt_id"))
                        if isinstance(details.get("result_receipt_id"), (str, int))
                        and not isinstance(details.get("result_receipt_id"), bool)
                        else None
                    ),
                    "matched_constraint_sha256": (
                        list(details.get("matched_constraint_sha256"))
                        if isinstance(details.get("matched_constraint_sha256"), list)
                        else []
                    ),
                })
            return list(reversed(events))
        finally:
            reopened.close()

    def test_the_runner_reads_a_usable_event_for_every_dispatch(self):
        database = self.data_dir / "jarvis.db"
        self.exercise_surface()
        self.memory.close()
        events = self.collect_tool_events(database, TRACE_ID)
        self.memory = Memory(database)

        self.assertEqual(len(events), len(SURFACE_CALLS))
        self.assertEqual(
            {event["name"] for event in events},
            {name for name, _ in SURFACE_CALLS},
        )
        # missing_target_receipts (task_contract_eval.py:1607-1612) must be zero
        # for every dispatched call.
        self.assertEqual(
            [
                event["name"]
                for event in events
                if (event["status"] == "complete" or event["handler_dispatched"])
                and event["target_sha256"] is None
            ],
            [],
        )
        self.assertTrue(all(event["status"] != "blocked" for event in events))
        by_effect = {event["effect"] for event in events}
        self.assertEqual(
            by_effect, {"read", "write", "execute", "external", "queue"}
        )
        queue_events = [
            event
            for event in events
            if event["effect"] == "queue" and event["status"] == "complete"
        ]
        self.assertTrue(queue_events)
        for event in queue_events:
            self.assertIsNotNone(event["receipt_id"], event["name"])
            self.assertTrue(event["matched_constraint_sha256"] or event["name"] != "schedule_create")
        external_events = [event for event in events if event["effect"] == "external"]
        self.assertTrue(external_events)
        self.assertTrue(all(event["status"] == "complete" for event in external_events))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
