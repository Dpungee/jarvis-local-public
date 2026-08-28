from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from jarvis.distillation import export_reward_dataset, initialize_pack


class RewardExportTests(unittest.TestCase):
    def test_grpo_export_separates_prompt_from_hidden_reward_spec(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "pack"
            initialize_pack(root)
            output = root / "grpo"
            manifest = export_reward_dataset(root / "tasks.jsonl", output)
            self.assertEqual(manifest["total_tasks"], 6)
            self.assertTrue(manifest["requires_isolated_sandbox"])
            self.assertFalse(manifest["host_execution_safe"])
            total = 0
            for details in manifest["files"].values():
                content = (output / details["file"]).read_text(encoding="utf-8")
                self.assertEqual(details["sha256"], hashlib.sha256(content.encode()).hexdigest())
                records = [json.loads(line) for line in content.splitlines()]
                total += len(records)
                for record in records:
                    prompt = json.dumps(record["prompt"])
                    hidden = json.dumps(record["reward_spec"]["hidden_files"])
                    self.assertNotIn("test_solution.py", prompt)
                    self.assertIn("test_solution.py", hidden)
                    self.assertEqual(record["environment"], "jarvis_python")
            self.assertEqual(total, 6)


if __name__ == "__main__":
    unittest.main()
