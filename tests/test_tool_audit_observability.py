from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from jarvis.config import Config
from jarvis.memory import Memory
from jarvis.tools import ToolBox


class ToolAuditObservabilityTests(unittest.TestCase):
    def test_tool_activity_carries_only_opaque_run_correlation(self):
        with tempfile.TemporaryDirectory(prefix="jarvis-tool-trace-") as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            data = root / "data"
            workspace.mkdir()
            data.mkdir()
            private_argument = "private-directory-name"
            (workspace / private_argument).mkdir()
            config = replace(
                Config.load(),
                workspace=workspace,
                data_dir=data,
                memory_embeddings="disabled",
                vault_dir=None,
            )
            with Memory(data / "jarvis.db") as memory:
                toolbox = ToolBox(config, memory)
                trace_id = "d" * 32
                with self.assertRaises(ValueError):
                    with toolbox.agent_context(1, trace_id="not-a-trace"):
                        pass
                self.assertIsNone(toolbox._agent_execution_context.get())
                self.assertIsNone(toolbox._run_trace_id.get())
                with toolbox.agent_context(1, trace_id=trace_id):
                    result = json.loads(
                        toolbox.execute("list_files", {"path": private_argument})
                    )
                row = memory.list_activity(limit=1)[0]
                details = json.loads(row["details_json"])

        self.assertTrue(result["ok"])
        self.assertEqual(details["trace_id"], trace_id)
        self.assertEqual(details["argument_names"], ["path"])
        self.assertNotIn(private_argument, row["details_json"])

    def test_activity_log_failure_is_visible_without_retrying_completed_tool(self):
        with tempfile.TemporaryDirectory(prefix="jarvis-tool-audit-") as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            data = root / "data"
            workspace.mkdir()
            data.mkdir()
            config = replace(
                Config.load(),
                workspace=workspace,
                data_dir=data,
                memory_embeddings="disabled",
                vault_dir=None,
            )
            with Memory(data / "jarvis.db") as memory:
                toolbox = ToolBox(config, memory)
                with (
                    patch.object(
                        memory, "log_activity", side_effect=RuntimeError("database busy")
                    ),
                    self.assertLogs("jarvis.tools", level="ERROR") as captured,
                ):
                    result = json.loads(toolbox.execute("list_files", {"path": "."}))

        self.assertTrue(result["ok"])
        self.assertIn("Tool activity audit write failed for list_files", captured.output[0])
        self.assertNotIn("database busy", captured.output[0])


if __name__ == "__main__":
    unittest.main()
