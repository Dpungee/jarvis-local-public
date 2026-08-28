from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from jarvis.finetune import (
    _load_records,
    _training_bundle_blockers,
    main as finetune_main,
)


def _basic_record(index: int = 0):
    return {
        "messages": [
            {"role": "user", "content": f"question {index}"},
            {"role": "assistant", "content": f"answer {index}"},
        ]
    }


class FinetuneHardeningTests(unittest.TestCase):
    def test_multiturn_tool_trace_is_accepted(self):
        record = {
            "messages": [
                {"role": "system", "content": "Use tools."},
                {"role": "user", "content": "Fix it."},
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{
                        "id": "write_1", "type": "function",
                        "function": {"name": "write_file", "arguments": {"path": "x.py", "content": "pass"}},
                    }],
                },
                {"role": "tool", "tool_call_id": "write_1", "name": "write_file", "content": "Wrote x.py."},
                {"role": "assistant", "content": "Implemented and verified."},
            ],
            "tools": [{"type": "function", "function": {"name": "write_file"}}],
        }
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "train.jsonl"
            path.write_text(json.dumps(record) + "\n", encoding="utf-8")
            self.assertEqual(_load_records(path), [record])

    def test_chat_trace_must_end_with_nonempty_assistant_answer(self):
        record = {
            "messages": [
                {"role": "user", "content": "Fix it."},
                {"role": "tool", "tool_call_id": "x", "content": "done"},
            ]
        }
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "train.jsonl"
            path.write_text(json.dumps(record) + "\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                _load_records(path)

    def test_training_bundle_gate_checks_counts_and_hash(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            train_path = root / "train.jsonl"
            content = "".join(json.dumps(_basic_record(index)) + "\n" for index in range(70))
            train_path.write_text(content, encoding="utf-8")
            validation_path = root / "validation.jsonl"
            validation_content = "".join(
                json.dumps(_basic_record(index + 100)) + "\n" for index in range(10)
            )
            validation_path.write_text(validation_content, encoding="utf-8")
            test_path = root / "test.jsonl"
            test_content = "".join(
                json.dumps(_basic_record(index + 200)) + "\n" for index in range(20)
            )
            test_path.write_text(test_content, encoding="utf-8")
            manifest = {
                "total_examples": 100,
                "constitution_sha256": "a" * 64,
                "selection": {
                    "passed_only": True,
                    "exact_reward": 1.0,
                    "family_grouped_splits": True,
                },
                "files": {
                    "train": {
                        "file": "train.jsonl", "examples": 70,
                        "sha256": hashlib.sha256(train_path.read_bytes()).hexdigest(),
                    },
                    "validation": {
                        "file": "validation.jsonl", "examples": 10,
                        "sha256": hashlib.sha256(validation_path.read_bytes()).hexdigest(),
                    },
                    "test": {
                        "file": "test.jsonl", "examples": 20,
                        "sha256": hashlib.sha256(test_path.read_bytes()).hexdigest(),
                    },
                },
            }
            (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            records = _load_records(train_path)
            self.assertEqual(_training_bundle_blockers(train_path, records), [])
            with redirect_stdout(StringIO()), self.assertRaisesRegex(
                SystemExit, "immutable 40-character"
            ):
                finetune_main([
                    "--dataset", str(train_path), "--output", str(root / "run")
                ])
            train_path.write_text(content + json.dumps(_basic_record(71)) + "\n", encoding="utf-8")
            blockers = " ".join(_training_bundle_blockers(train_path, _load_records(train_path)))
            self.assertIn("hash", blockers)
            self.assertIn("count", blockers)

    def test_real_training_refuses_unready_unmanifested_data(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dataset = root / "train.jsonl"
            dataset.write_text(json.dumps(_basic_record()) + "\n", encoding="utf-8")
            with redirect_stdout(StringIO()), self.assertRaises(SystemExit) as raised:
                finetune_main(["--dataset", str(dataset), "--output", str(root / "run")])
            self.assertIn("not ready", str(raised.exception).lower())

    def test_dry_run_reports_gate_without_loading_training_libraries(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dataset = root / "train.jsonl"
            dataset.write_text(json.dumps(_basic_record()) + "\n", encoding="utf-8")
            output = StringIO()
            with redirect_stdout(output):
                finetune_main([
                    "--dataset", str(dataset), "--output", str(root / "run"), "--dry-run"
                ])
            self.assertIn("gated", output.getvalue().lower())


if __name__ == "__main__":
    unittest.main()
