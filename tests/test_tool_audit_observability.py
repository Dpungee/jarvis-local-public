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
