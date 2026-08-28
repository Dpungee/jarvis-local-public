from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from jarvis.distillation import (
    _load_jsonl,
    _parse_completion,
    _safe_relative_path,
    _sft_record,
    distillation_status,
    export_sft_dataset,
    family_split,
    generate_candidates,
    initialize_pack,
    load_tasks,
    verify_candidates,
)
from jarvis.finetune import _load_records


class _Response(dict):
    pass


class _Teacher:
    def __init__(self, content: str) -> None:
        self.content = content
        self.calls = []

    def chat(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return _Response(content=self.content)


class DistillationTests(unittest.TestCase):
    def test_family_splits_are_deterministic_and_grouped(self):
        self.assertEqual(family_split("same-family"), family_split("same-family"))
        self.assertIn(family_split("same-family"), {"train", "validation", "test"})

    def test_relative_paths_reject_escape_and_ambiguous_forms(self):
        self.assertEqual(_safe_relative_path(r"src\app.py"), "src/app.py")
        for value in ("../x", "a/../x", "/x", "C:/x", "a//b", "./x", ""):
            with self.subTest(value=value), self.assertRaises(ValueError):
                _safe_relative_path(value)

    def test_pack_initialization_is_idempotent_and_has_all_splits(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "pack"
            first = initialize_pack(root)
            second = initialize_pack(root)
            self.assertTrue(first["created"])
            self.assertFalse(second["created"])
            tasks = load_tasks(root / "tasks.jsonl")
            self.assertEqual(len(tasks), 6)
            self.assertEqual({task["split"] for task in tasks}, {"train", "validation", "test"})
            for family in {task["family"] for task in tasks}:
                self.assertEqual(len({task["split"] for task in tasks if task["family"] == family}), 1)

    def test_teacher_prompt_does_not_include_hidden_tests(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "pack"
            initialize_pack(root)
            teacher = _Teacher(json.dumps({
                "summary": "Implemented the requested function.",
                "files": {"solution.py": "def clip_text(value, limit):\n return str(value)\n"},
            }))
            result = generate_candidates(
                root / "tasks.jsonl",
                root / "candidates.jsonl",
                teacher,
                "teacher:1",
                limit=1,
            )
            self.assertEqual(result["generated"], 1)
            messages = teacher.calls[0][0][0]
            serialized = json.dumps(messages)
            self.assertNotIn("test_solution.py", serialized)
            self.assertNotIn("test_clip", serialized)

    def test_candidate_schema_rejects_extra_paths_and_secrets(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "pack"
            initialize_pack(root)
            task = load_tasks(root / "tasks.jsonl")[0]
            with self.assertRaises(ValueError):
                _parse_completion(json.dumps({
                    "summary": "done",
                    "files": {"not-allowed.py": "pass"},
                }), task)
            with self.assertRaises(ValueError):
                _parse_completion(json.dumps({
                    "summary": "done",
                    "files": {"solution.py": "TOKEN='sk-proj-abcdefghijklmnop'"},
                }), task)

    def test_verification_requires_explicit_generated_code_execution(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "pack"
            initialize_pack(root)
            with self.assertRaises(PermissionError):
                verify_candidates(
                    root / "tasks.jsonl",
                    root / "candidates.jsonl",
                    root / "verified.jsonl",
                    allow_host_execution=False,
                )

    def test_hidden_tests_pass_known_good_candidate_and_export_tool_trace(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "pack"
            initialize_pack(root)
            tasks = load_tasks(root / "tasks.jsonl")
            task = next(item for item in tasks if item["task_id"] == "python-text.clip-001")
            completion = {
                "summary": "Implemented bounded clipping with an ellipsis.",
                "files": {"solution.py": (
                    "def clip_text(value, limit):\n"
                    "    if limit < 4:\n"
                    "        raise ValueError('limit must be at least 4')\n"
                    "    text = str(value)\n"
                    "    return text if len(text) <= limit else text[:limit - 3] + '...'\n"
                )},
            }
            candidate = {
                "format_version": 1,
                "task_id": task["task_id"],
                "family": task["family"],
                "split": task["split"],
                "teacher": "fixture",
                "accepted_schema": True,
                "completion": completion,
            }
            (root / "candidates.jsonl").write_text(json.dumps(candidate) + "\n", encoding="utf-8")
            result = verify_candidates(
                root / "tasks.jsonl",
                root / "candidates.jsonl",
                root / "verified.jsonl",
                allow_host_execution=True,
            )
            self.assertEqual(result, {"verified": 1, "passed": 1, "failed": 0})
            record = _load_jsonl(root / "verified.jsonl")[0]
            self.assertEqual(record["reward"], 1.0)
            manifest = export_sft_dataset(root / "verified.jsonl", root / "sft")
            self.assertEqual(manifest["total_examples"], 1)
            split_path = root / "sft" / f"{task['split']}.jsonl"
            split_record = _load_jsonl(split_path)[0]
            self.assertEqual(_load_records(split_path), [split_record])
            roles = [message["role"] for message in split_record["messages"]]
            self.assertEqual(roles[0:2], ["system", "user"])
            self.assertIn("tool", roles)
            self.assertEqual(roles[-1], "assistant")
            self.assertEqual(split_record["metadata"]["reward"], 1.0)

            tool_schemas = {
                tool["function"]["name"]: tool["function"]["parameters"]
                for tool in split_record["tools"]
            }
            self.assertEqual(
                set(tool_schemas),
                {"read_file", "write_file", "run_process"},
            )
            self.assertEqual(
                set(tool_schemas["run_process"]["properties"]),
                {"program", "arguments", "cwd", "timeout"},
            )
            self.assertEqual(
                set(tool_schemas["write_file"]["properties"]),
                {"path", "content", "expected_sha256"},
            )

            calls = [
                message["tool_calls"][0]
                for message in split_record["messages"]
                if message["role"] == "assistant" and message.get("tool_calls")
            ]
            self.assertEqual(
                [call["function"]["name"] for call in calls],
                ["read_file", "write_file", "run_process"],
            )
            self.assertNotIn("run_command", json.dumps(split_record))

            initial_content = task["initial_files"]["solution.py"]
            expected_hash = hashlib.sha256(initial_content.encode("utf-8")).hexdigest()
            self.assertEqual(
                calls[0]["function"]["arguments"],
                {"path": "solution.py"},
            )
            self.assertEqual(
                calls[1]["function"]["arguments"]["expected_sha256"],
                expected_hash,
            )
            self.assertEqual(
                calls[2]["function"]["arguments"],
                {
                    "program": "python",
                    "arguments": task["verifiers"][0]["argv"][1:],
                    "cwd": ".",
                    "timeout": task["verifiers"][0]["timeout_seconds"],
                },
            )
            for message in split_record["messages"]:
                if message["role"] == "tool":
                    self.assertTrue(json.loads(message["content"])["ok"])

    def test_sft_trace_uses_expected_hash_only_for_existing_files(self):
        initial = "print('old')\n"
        record = {
            "task_id": "fixture-001",
            "family": "fixture-family",
            "split": family_split("fixture-family"),
            "teacher": "fixture",
            "reward": 1.0,
            "prompt": "Update existing.py and create new.py.",
            "initial_files": {"existing.py": initial},
            "completion": {
                "summary": "Updated and verified both files.",
                "files": {
                    "existing.py": "print('new')\n",
                    "new.py": "print('created')\n",
                },
            },
            "completion_sha256": "fixture",
            "verification": [{
                "returncode": 0,
                "stdout": "ok",
                "stderr": "",
                "argv": ["$PYTHON", "-m", "unittest", "-q"],
                "timeout_seconds": 17,
            }],
        }

        trace = _sft_record(record)
        calls = [
            message["tool_calls"][0]["function"]
            for message in trace["messages"]
            if message["role"] == "assistant" and message.get("tool_calls")
        ]
        writes = {
            call["arguments"]["path"]: call["arguments"]
            for call in calls
            if call["name"] == "write_file"
        }
        self.assertEqual(
            writes["existing.py"]["expected_sha256"],
            hashlib.sha256(initial.encode("utf-8")).hexdigest(),
        )
        self.assertNotIn("expected_sha256", writes["new.py"])
        self.assertEqual(calls[0], {
            "name": "read_file",
            "arguments": {"path": "existing.py"},
        })
        self.assertEqual(
            next(call for call in calls if call["name"] == "run_process")["arguments"],
            {
                "program": "python",
                "arguments": ["-m", "unittest", "-q"],
                "cwd": ".",
                "timeout": 17,
            },
        )

    def test_failed_candidate_gets_zero_reward_and_is_not_exported(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "pack"
            initialize_pack(root)
            task = load_tasks(root / "tasks.jsonl")[0]
            candidate = {
                "task_id": task["task_id"],
                "family": task["family"],
                "split": task["split"],
                "teacher": "fixture",
                "accepted_schema": True,
                "completion": {"summary": "No-op.", "files": {"solution.py": "pass\n"}},
            }
            (root / "candidates.jsonl").write_text(json.dumps(candidate) + "\n", encoding="utf-8")
            result = verify_candidates(
                root / "tasks.jsonl",
                root / "candidates.jsonl",
                root / "verified.jsonl",
                allow_host_execution=True,
            )
            self.assertEqual(result["failed"], 1)
            record = _load_jsonl(root / "verified.jsonl")[0]
            self.assertEqual(record["reward"], 0.0)
            manifest = export_sft_dataset(root / "verified.jsonl", root / "sft")
            self.assertEqual(manifest["total_examples"], 0)

    def test_verifier_never_uses_shell(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "pack"
            initialize_pack(root)
            task = load_tasks(root / "tasks.jsonl")[0]
            candidate = {
                "task_id": task["task_id"], "family": task["family"],
                "split": task["split"], "teacher": "fixture", "accepted_schema": True,
                "completion": {"summary": "done", "files": {"solution.py": "pass\n"}},
            }
            (root / "candidates.jsonl").write_text(json.dumps(candidate) + "\n", encoding="utf-8")
            seen = []

            def runner(argv, **kwargs):
                seen.append((argv, kwargs))
                return subprocess.CompletedProcess(argv, 0, "ok", "")

            verify_candidates(
                root / "tasks.jsonl", root / "candidates.jsonl", root / "verified.jsonl",
                allow_host_execution=True, runner=runner,
            )
            self.assertEqual(len(seen), 1)
            self.assertIs(seen[0][1]["shell"], False)
            self.assertIsInstance(seen[0][0], list)

    def test_status_counts_pipeline_stages(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "pack"
            initialize_pack(root)
            status = distillation_status(root)
            self.assertEqual(status["tasks"], 6)
            self.assertEqual(status["candidate_attempts"], 0)
            self.assertEqual(status["passed"], 0)


if __name__ == "__main__":
    unittest.main()
