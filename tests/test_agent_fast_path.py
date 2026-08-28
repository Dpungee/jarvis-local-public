import hashlib
import json
import os
import shutil
import subprocess
import sys
import textwrap
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from jarvis.agent import Agent
from jarvis.config import Config
from jarvis.memory import Memory


TEMP_ROOT = Path(__file__).resolve().parent / ".tmp"
TEMP_ROOT.mkdir(exist_ok=True)


def tool_call(name, arguments):
    return {"function": {"name": name, "arguments": arguments}}


class FakeResponse(dict):
    def __init__(self, content="", tool_calls=None):
        super().__init__(role="assistant", content=content)
        if tool_calls is not None:
            self["tool_calls"] = tool_calls
        self.done_reason = None
        self.done = True


class RecordingClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def models(self, refresh=True):
        return ["qwen3.5:9b", "gpt-oss:20b", "qwen3-coder:30b"]

    def chat(
        self,
        messages,
        tools,
        model,
        context_length,
        think=None,
        temperature=0.2,
        response_format=None,
        seed=None,
    ):
        self.requests.append({
            "messages": messages,
            "tools": tools,
            "model": model,
            "context_length": context_length,
            "think": think,
            "temperature": temperature,
            "response_format": response_format,
            "seed": seed,
        })
        if not self.responses:
            raise AssertionError(
                "A verified fast-path coding run made an unnecessary extra model call"
            )
        return self.responses.pop(0)


class StatefulToolBox:
    NAMES = (
        "list_files",
        "read_file",
        "write_file",
        "edit_file",
        "make_directory",
        "copy_path",
        "move_path",
        "trash_path",
        "search_files",
        "run_process",
        "start_process",
        "process_status",
        "process_logs",
        "stop_process",
        "http_health",
        "remember",
        "recall",
    )

    def __init__(self):
        self.calls = []
        self.files = {
            "README.md": (
                "Implement app.py. Numeric duration values must be finite and non-negative; "
                "boolean values are not valid numeric durations."
            ),
            "app.py": "def main():\n    return 'old'\n",
        }
        self.schemas = [
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": "test tool",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
            for name in self.NAMES
        ]

    @staticmethod
    def _success(result):
        return json.dumps({"ok": True, "result": result})

    def execute(self, name, arguments):
        arguments = dict(arguments)
        self.calls.append((name, arguments))
        if name == "list_files":
            return self._success({"files": sorted(self.files)})
        if name == "read_file":
            path = str(arguments.get("path", ""))
            content = self.files.get(path, "")
            return self._success({
                "path": path,
                "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                "content": content,
                "truncated": False,
            })
        if name == "write_file":
            path = str(arguments.get("path", ""))
            content = str(arguments.get("content", ""))
            self.files[path] = content
            return self._success({"path": path, "bytes": len(content.encode("utf-8"))})
        if name == "edit_file":
            path = str(arguments.get("path", ""))
            old_text = str(arguments.get("old_text", ""))
            new_text = str(arguments.get("new_text", ""))
            self.files[path] = self.files.get(path, "").replace(old_text, new_text, 1)
            return self._success({"path": path})
        if name == "run_process":
            return self._success({
                "exit_code": 0,
                "timed_out": False,
                "stdout": "Ran 1 test in 0.01s\nOK",
                "stderr": "",
            })
        if name == "search_files":
            return self._success({"matches": []})
        return self._success({"name": name})


class ExecutingProbeToolBox(StatefulToolBox):
    """Stateful fake that executes only JARVIS's generated temporary probes."""

    def __init__(self, workspace, files):
        super().__init__()
        self.workspace = Path(workspace)
        self.files = dict(files)
        self.probe_runs = 0
        for relative, content in self.files.items():
            target = self.workspace / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")

    def execute(self, name, arguments):
        raw_arguments = list(arguments.get("arguments", []))
        if (
            name == "run_process"
            and raw_arguments
            and Path(str(raw_arguments[0])).name.startswith(".jarvis-probe-")
        ):
            copied = dict(arguments)
            self.calls.append((name, copied))
            self.probe_runs += 1
            completed = subprocess.run(
                [sys.executable, *map(str, raw_arguments)],
                cwd=self.workspace,
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            return self._success({
                "exit_code": completed.returncode,
                "timed_out": False,
                "stdout": completed.stdout,
                "stderr": completed.stderr,
            })

        result = super().execute(name, arguments)
        if name in {"write_file", "edit_file"}:
            relative = str(arguments.get("path", ""))
            target = self.workspace / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(self.files[relative], encoding="utf-8")
        return result


EVENT_CONTRACT = (
    "Implement rollup_events from JSON lines containing event_id, account_id, kind, "
    "duration_ms, and timestamp. Reject invalid durations and naive timestamps. "
    "Deduplicate event_id by the earliest timestamp, keep the first input on an "
    "equal-instant tie, and calculate every aggregate from retained records only."
)


GOOD_EVENT_ROLLUP = textwrap.dedent(
    '''\
    import json
    import math
    from datetime import datetime, timezone


    def _mean_duration(rows):
        values = [row["duration_ms"] for row in rows]
        if not values:
            return 0.0
        if len(values) == 1:
            return values[0]
        return math.fsum(value / len(values) for value in values)


    def _summary(rows):
        total = len(rows)
        errors = sum(row["kind"] == "error" for row in rows)
        return {
            "total_events": total,
            "total_errors": errors,
            "error_rate": errors / total if total else 0.0,
            "mean_duration_ms": _mean_duration(rows),
        }


    def rollup_events(lines):
        valid = []
        for index, line in enumerate(lines):
            try:
                item = json.loads(line)
                duration = item["duration_ms"]
                if isinstance(duration, bool) or not isinstance(duration, (int, float)):
                    continue
                try:
                    if not math.isfinite(duration) or duration < 0:
                        continue
                except OverflowError:
                    if duration < 0:
                        continue
                raw_timestamp = item["timestamp"]
                if not isinstance(raw_timestamp, str):
                    continue
                parsed = datetime.fromisoformat(raw_timestamp.replace("Z", "+00:00"))
                if parsed.tzinfo is None or parsed.utcoffset() is None:
                    continue
                event_id = item["event_id"]
                account_id = item["account_id"]
                kind = item["kind"]
                if not all(isinstance(value, str) and value for value in (event_id, account_id, kind)):
                    continue
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                continue
            valid.append({
                "event_id": event_id,
                "account_id": account_id,
                "kind": kind,
                "duration_ms": duration,
                "_instant": parsed.astimezone(timezone.utc),
                "_index": index,
            })

        chosen = {}
        for row in valid:
            current = chosen.get(row["event_id"])
            if current is None or row["_instant"] < current["_instant"]:
                chosen[row["event_id"]] = row
        retained = list(chosen.values())
        result = _summary(retained)
        account_records = retained
        result["accounts"] = {
            account: _summary([row for row in account_records if row["account_id"] == account])
            for account in sorted({row["account_id"] for row in account_records})
        }
        return result
    '''
)


SAFE_PATH_CONTRACT = (
    "Implement safe_join with traversal protection, repeated percent decoding, and "
    "symlink rejection for both the root and every path component."
)


GOOD_SAFE_JOIN = textwrap.dedent(
    '''\
    from pathlib import Path, PurePosixPath, PureWindowsPath
    from urllib.parse import unquote


    def safe_join(root, value):
        root_path = Path(root)
        if not root_path.exists() or not root_path.is_dir() or root_path.is_symlink():
            raise ValueError("root must be a real existing directory")
        if not isinstance(value, str) or "\\x00" in value:
            raise ValueError("invalid path")
        decoded = value
        for _ in range(8):
            updated = unquote(decoded)
            if updated == decoded:
                break
            decoded = updated
        if "\\x00" in decoded:
            raise ValueError("decoded NUL")
        windows = PureWindowsPath(decoded)
        if windows.drive or windows.root or decoded.startswith(("/", "\\\\")):
            raise ValueError("absolute path")
        normalized = decoded.replace("\\\\", "/")
        parts = tuple(part for part in PurePosixPath(normalized).parts if part not in ("", "."))
        if ".." in parts:
            raise ValueError("traversal")
        current = root_path
        for part in parts:
            current = current / part
            if current.is_symlink():
                raise ValueError("symlink")
        resolved_root = root_path.resolve(strict=True)
        candidate = current.resolve(strict=False)
        try:
            candidate.relative_to(resolved_root)
        except ValueError as exc:
            raise ValueError("escape") from exc
        return candidate
    '''
)


def inspection_response():
    return FakeResponse(tool_calls=[
        tool_call("read_file", {"path": "README.md"}),
        tool_call("read_file", {"path": "app.py"}),
    ])


def implementation_response():
    return FakeResponse(tool_calls=[
        tool_call(
            "write_file",
            {
                "path": "app.py",
                "content": "def main():\n    return 'ok'\n",
            },
        ),
        tool_call("read_file", {"path": "app.py"}),
        tool_call(
            "run_process",
            {"program": "python", "arguments": ["-m", "unittest"]},
        ),
    ])


class AgentFastPathTests(unittest.TestCase):
    def setUp(self):
        self.test_dir = TEMP_ROOT / f"fast-path-{os.getpid()}-{self._testMethodName}"
        if self.test_dir.exists():
            shutil.rmtree(self.test_dir)
        self.workspace = self.test_dir / "workspace"
        self.data_dir = self.test_dir / "data"
        self.workspace.mkdir(parents=True)
        self.data_dir.mkdir(parents=True)
        self.config = replace(
            Config.load(),
            model="auto",
            workspace=self.workspace,
            data_dir=self.data_dir,
            vault_dir=None,
            max_steps=20,
            context_length=4096,
            fast_model="qwen3.5:9b",
            reasoning_model="gpt-oss:20b",
            coding_model="qwen3-coder:30b",
            reasoning_thinking=True,
        )
        self.memory = Memory(self.data_dir / "agent.db")

    def tearDown(self):
        self.memory.close()
        resolved = self.test_dir.resolve()
        self.assertEqual(resolved.parent, TEMP_ROOT.resolve())
        shutil.rmtree(resolved)

    def make_agent(self, responses):
        client = RecordingClient(responses)
        toolbox = StatefulToolBox()
        with patch("jarvis.agent.ToolBox", return_value=toolbox):
            agent = Agent(
                self.config,
                self.memory,
                client=client,
                record_training=False,
            )
        return agent, client

    def run_generated_probe(self, prompt, path, source):
        digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
        probe = Agent._build_adversarial_probe(
            prompt,
            {
                path: {
                    "path": path,
                    "sha256": digest,
                    "content": source,
                    "truncated": False,
                }
            },
        )
        self.assertIsNotNone(probe)
        label, script = probe
        target = self.workspace / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source, encoding="utf-8")
        probe_path = self.workspace / "generated_probe.py"
        probe_path.write_text(script, encoding="utf-8")
        completed = subprocess.run(
            [sys.executable, str(probe_path)],
            cwd=self.workspace,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        return label, script, completed

    @staticmethod
    def tool_names(request):
        return {
            schema["function"]["name"]
            for schema in request["tools"]
        }

    def test_production_defaults_use_local_plan_without_independent_review(self):
        agent, client = self.make_agent([
            inspection_response(),
            implementation_response(),
            FakeResponse(content="Implemented and verified."),
        ])

        result = agent.run("Build the Python application in app.py from README.md")

        self.assertFalse(agent.coding_review)
        self.assertTrue(agent.coding_planning)
        self.assertFalse(agent.model_coding_planning)
        self.assertEqual(result.status, "complete")
        self.assertNotIn("write_file", self.tool_names(client.requests[0]))
        self.assertIn("write_file", self.tool_names(client.requests[1]))
        implementation_context = json.dumps(
            client.requests[1]["messages"], ensure_ascii=False
        ).casefold()
        self.assertIn("untrusted_prewrite_reasoning_plan", implementation_context)
        self.assertIn("bool is a subclass of int", implementation_context)
        self.assertTrue(
            all(request["response_format"] is None for request in client.requests),
            "The default coding path unexpectedly invoked a structured model reviewer",
        )

    def test_coding_prompt_automatically_authorizes_build_and_test_tools(self):
        agent, client = self.make_agent([
            inspection_response(),
            implementation_response(),
            FakeResponse(content="Built successfully."),
        ])

        result = agent.run("Build a small Python application in app.py")

        initial_tools = self.tool_names(client.requests[0])
        implementation_tools = self.tool_names(client.requests[1])
        self.assertIn("run_process", initial_tools)
        self.assertNotIn("write_file", initial_tools)
        self.assertTrue({"write_file", "run_process"}.issubset(implementation_tools))
        self.assertIn("run_process", [name for name, _ in agent.toolbox.calls])
        self.assertEqual(result.status, "complete")

    def test_continuing_conversation_carries_recent_user_and_assistant_turns(self):
        conversation_id = self.memory.new_conversation("continuity")
        self.memory.add_message(
            conversation_id,
            "user",
            "HISTORY_USER_MARKER: prefer compact answers",
        )
        self.memory.add_message(
            conversation_id,
            "assistant",
            "HISTORY_ASSISTANT_MARKER: preference acknowledged",
        )
        agent, client = self.make_agent([
            FakeResponse(content="Continuing with the saved context."),
        ])

        result = agent.run(
            "Continue with that preference",
            conversation_id=conversation_id,
        )

        sent_messages = client.requests[0]["messages"]
        sent_history = "\n".join(
            str(message.get("content", "")) for message in sent_messages
        )
        self.assertIn("HISTORY_USER_MARKER", sent_history)
        self.assertIn("HISTORY_ASSISTANT_MARKER", sent_history)
        user_messages = [
            message["content"] for message in sent_messages if message.get("role") == "user"
        ]
        self.assertEqual(user_messages[-1], "Continue with that preference")
        self.assertEqual(result.status, "complete")

    def test_direct_server_lifecycle_request_exposes_managed_process_toolset(self):
        agent, client = self.make_agent([
            FakeResponse(content="The server lifecycle tools are ready."),
        ])

        result = agent.run("Start the app server and show its health")

        offered = self.tool_names(client.requests[0])
        self.assertTrue({
            "start_process",
            "process_status",
            "process_logs",
            "stop_process",
            "http_health",
            "run_process",
        }.issubset(offered))
        self.assertEqual(result.status, "complete")

    def test_direct_folder_mutation_request_exposes_reversible_file_tools(self):
        agent, client = self.make_agent([
            FakeResponse(content="The requested folder operations are available."),
        ])

        result = agent.run(
            "Copy folder alpha to beta, move folder gamma to delta, and trash folder old"
        )

        offered = self.tool_names(client.requests[0])
        self.assertTrue({
            "make_directory",
            "copy_path",
            "move_path",
            "trash_path",
        }.issubset(offered))
        self.assertEqual(result.status, "complete")

    def test_verified_coding_fast_path_does_not_make_final_narration_call(self):
        agent, client = self.make_agent([
            inspection_response(),
            implementation_response(),
        ])

        result = agent.run("Build a small Python application in app.py")

        self.assertEqual(result.status, "complete")
        self.assertEqual(len(client.requests), 2)
        self.assertEqual(
            [name for name, _arguments in agent.toolbox.calls],
            ["read_file", "read_file", "write_file", "read_file", "run_process"],
        )

    def test_event_rollup_probe_accepts_complete_implementation(self):
        label, script, completed = self.run_generated_probe(
            EVENT_CONTRACT,
            "rollup.py",
            GOOD_EVENT_ROLLUP,
        )

        self.assertEqual(label, "event-rollup validation/deduplication")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("adversarial contract passed", completed.stdout)
        self.assertIn("1e308", script)
        self.assertIn("10 ** 400", script)
        self.assertIn('"huge", "request"', script)

    def test_probe_recognizes_line_numbered_read_file_snapshot(self):
        numbered_snapshot = "\n".join(
            f"{line_number}: {line}"
            for line_number, line in enumerate(GOOD_EVENT_ROLLUP.splitlines(), 1)
        )
        probe = Agent._build_adversarial_probe(
            "Implement the specification in rollup.py",
            {
                "README.md": {
                    "path": "README.md",
                    "sha256": "a" * 64,
                    "content": EVENT_CONTRACT,
                    "truncated": False,
                },
                "rollup.py": {
                    "path": "rollup.py",
                    "sha256": "b" * 64,
                    "content": numbered_snapshot,
                    "truncated": False,
                },
            },
        )

        self.assertIsNotNone(probe)
        self.assertEqual(probe[0], "event-rollup validation/deduplication")
        self.assertIn('"rollup.py"', probe[1])

    def test_event_rollup_probe_rejects_each_shallow_hidden_test_bug(self):
        mutations = {
            "boolean numeric subtype": (
                "if isinstance(duration, bool) or not isinstance(duration, (int, float)):",
                "if not isinstance(duration, (int, float)):",
            ),
            "non-finite values": (
                "if not math.isfinite(duration) or duration < 0:",
                "if duration < 0:",
            ),
            "negative values": (
                "if not math.isfinite(duration) or duration < 0:",
                "if not math.isfinite(duration):",
            ),
            "finite aggregate overflow": (
                "return math.fsum(value / len(values) for value in values)",
                "return sum(values) / len(values)",
            ),
            "huge finite integer": (
                "except OverflowError:\n                if duration < 0:\n                    continue",
                "except OverflowError:\n                continue",
            ),
            "naive timestamps": (
                "if parsed.tzinfo is None or parsed.utcoffset() is None:\n                continue",
                "if False:\n                continue",
            ),
            "earliest record selection": (
                'row["_instant"] < current["_instant"]',
                'row["_instant"] > current["_instant"]',
            ),
            "equal-instant first-input tie": (
                'row["_instant"] < current["_instant"]',
                'row["_instant"] <= current["_instant"]',
            ),
            "retained-only account aggregates": (
                "account_records = retained",
                "account_records = valid",
            ),
        }
        for name, (old, new) in mutations.items():
            with self.subTest(name=name):
                self.assertIn(old, GOOD_EVENT_ROLLUP)
                bad_source = GOOD_EVENT_ROLLUP.replace(old, new, 1)
                _label, _script, completed = self.run_generated_probe(
                    EVENT_CONTRACT,
                    "rollup.py",
                    bad_source,
                )
                self.assertNotEqual(
                    completed.returncode,
                    0,
                    f"Probe missed the {name} defect",
                )
                self.assertIn("AssertionError", completed.stderr)

    def test_safe_join_probe_covers_encoding_platform_root_and_symlink_edges(self):
        label, script, completed = self.run_generated_probe(
            SAFE_PATH_CONTRACT,
            "safe_path.py",
            GOOD_SAFE_JOIN,
        )

        self.assertEqual(label, "path traversal/encoding/symlink")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("adversarial contract passed", completed.stdout)
        for sentinel in (
            "%25252e%25252e/secret",
            "C:drive-relative",
            "server",
            "bad\\x00name",
            "decoded%00name",
            "decoded%252500name",
            'base / "missing"',
            'Path("root")',
            "broken-link",
            "final containment",
            "os.symlink",
        ):
            self.assertIn(sentinel, script)

        bad_source = textwrap.dedent(
            '''\
            from pathlib import Path

            def safe_join(root, value):
                return (Path(root) / value).resolve()
            '''
        )
        _label, _script, bad = self.run_generated_probe(
            SAFE_PATH_CONTRACT,
            "safe_path.py",
            bad_source,
        )
        self.assertNotEqual(bad.returncode, 0)
        self.assertIn("AssertionError", bad.stderr)

    def test_safe_join_probe_rejects_new_decode_root_and_broken_link_gaps(self):
        decoded_nul_bug = GOOD_SAFE_JOIN.replace(
            'if "\\x00" in decoded:\n        raise ValueError("decoded NUL")',
            'decoded = decoded.replace("\\x00", "")',
            1,
        )
        relative_root_bug = GOOD_SAFE_JOIN.replace(
            "return candidate",
            "return current",
            1,
        )
        broken_link_bug = GOOD_SAFE_JOIN.replace(
            "if current.is_symlink():\n            raise ValueError(\"symlink\")",
            "if current.exists() and current.is_symlink():\n            raise ValueError(\"symlink\")",
            1,
        ).replace(
            "candidate.relative_to(resolved_root)",
            "Path(resolved_root)",
            1,
        )
        for name, bad_source in {
            "post-decode NUL": decoded_nul_bug,
            "relative root resolution": relative_root_bug,
            "broken symlink final containment": broken_link_bug,
        }.items():
            with self.subTest(name=name):
                self.assertNotEqual(bad_source, GOOD_SAFE_JOIN)
                _label, _script, completed = self.run_generated_probe(
                    SAFE_PATH_CONTRACT,
                    "safe_path.py",
                    bad_source,
                )
                self.assertNotEqual(completed.returncode, 0, f"Probe missed {name}")
                self.assertIn("AssertionError", completed.stderr)

    def test_bool_probe_failure_is_repaired_deterministically_without_model_turn(self):
        bool_bug = GOOD_EVENT_ROLLUP.replace(
            "if isinstance(duration, bool) or not isinstance(duration, (int, float)):",
            "if not isinstance(duration, (int, float)):",
            1,
        )
        files = {
            "README.md": EVENT_CONTRACT,
            "rollup.py": "def rollup_events(lines):\n    return {}\n",
        }
        toolbox = ExecutingProbeToolBox(self.workspace, files)
        client = RecordingClient([
            FakeResponse(tool_calls=[
                tool_call("read_file", {"path": "README.md"}),
                tool_call("read_file", {"path": "rollup.py"}),
            ]),
            FakeResponse(tool_calls=[
                tool_call("write_file", {"path": "rollup.py", "content": bool_bug}),
                tool_call("read_file", {"path": "rollup.py"}),
                tool_call(
                    "run_process",
                    {"program": "python", "arguments": ["-m", "unittest"]},
                ),
            ]),
        ])
        events = []
        with patch("jarvis.agent.ToolBox", return_value=toolbox):
            agent = Agent(
                self.config,
                self.memory,
                client=client,
                record_training=False,
                on_event=events.append,
            )

        result = agent.run(f"Implement rollup.py. {EVENT_CONTRACT}")

        self.assertEqual(result.status, "complete")
        self.assertEqual(len(client.requests), 2)
        self.assertEqual(toolbox.probe_runs, 2)
        deterministic_edits = [
            arguments
            for name, arguments in toolbox.calls
            if name == "edit_file"
        ]
        self.assertEqual(len(deterministic_edits), 1)
        self.assertIn(
            "if not isinstance(duration, (int, float)):",
            deterministic_edits[0]["old_text"],
        )
        self.assertIn(
            "not isinstance(duration, bool)",
            deterministic_edits[0]["new_text"],
        )
        self.assertIn(
            "applying deterministic subtype repair - rollup.py",
            events,
        )
        self.assertEqual(events[-2:], [
            "adversarial verification passed",
            "verified implementation complete - deterministic handoff",
        ])

    def test_probe_repair_rejects_test_churn_without_advancing_repair_state(self):
        both_ordering_bugs = GOOD_EVENT_ROLLUP.replace(
            'row["_instant"] < current["_instant"]',
            'row["_instant"] >= current["_instant"]',
            1,
        )
        tie_only_bug = GOOD_EVENT_ROLLUP.replace(
            'row["_instant"] < current["_instant"]',
            'row["_instant"] <= current["_instant"]',
            1,
        )
        files = {
            "README.md": EVENT_CONTRACT,
            "rollup.py": "def rollup_events(lines):\n    return {}\n",
        }
        toolbox = ExecutingProbeToolBox(self.workspace, files)
        client = RecordingClient([
            FakeResponse(tool_calls=[
                tool_call("read_file", {"path": "README.md"}),
                tool_call("read_file", {"path": "rollup.py"}),
            ]),
            FakeResponse(tool_calls=[
                tool_call(
                    "write_file",
                    {"path": "rollup.py", "content": both_ordering_bugs},
                ),
                tool_call("read_file", {"path": "rollup.py"}),
                tool_call(
                    "run_process",
                    {"program": "python", "arguments": ["-m", "unittest"]},
                ),
            ]),
            # The test-file write must be rejected. The remaining successful actions
            # change generic runtime state, but not source content. Together they must
            # not rerun the same failed probe or consume repair opportunity #2.
            FakeResponse(tool_calls=[
                tool_call(
                    "write_file",
                    {
                        "path": "debug_test.py",
                        "content": "def test_debug():\n    assert False\n",
                    },
                ),
                tool_call(
                    "run_process",
                    {
                        "program": "python",
                        "arguments": ["-m", "unittest", "discover"],
                    },
                ),
                tool_call("list_files", {}),
                tool_call("read_file", {"path": "README.md"}),
            ]),
            FakeResponse(tool_calls=[
                tool_call(
                    "write_file",
                    {"path": "rollup.py", "content": tie_only_bug},
                ),
                tool_call("read_file", {"path": "rollup.py"}),
                tool_call(
                    "run_process",
                    {"program": "python", "arguments": ["-m", "unittest"]},
                ),
            ]),
            FakeResponse(tool_calls=[
                tool_call(
                    "write_file",
                    {"path": "rollup.py", "content": GOOD_EVENT_ROLLUP},
                ),
                tool_call("read_file", {"path": "rollup.py"}),
                tool_call(
                    "run_process",
                    {"program": "python", "arguments": ["-m", "unittest"]},
                ),
            ]),
        ])
        events = []
        with patch("jarvis.agent.ToolBox", return_value=toolbox):
            agent = Agent(
                self.config,
                self.memory,
                client=client,
                record_training=False,
                on_event=events.append,
            )

        result = agent.run(f"Implement rollup.py. {EVENT_CONTRACT}")

        self.assertEqual(result.status, "complete")
        self.assertEqual(len(client.requests), 5)
        self.assertEqual(toolbox.probe_runs, 3)
        source_writes = [
            arguments
            for name, arguments in toolbox.calls
            if name == "write_file" and arguments.get("path") == "rollup.py"
        ]
        self.assertEqual(len(source_writes), 3)  # initial implementation + two repairs
        self.assertNotIn("debug_test.py", toolbox.files)
        self.assertFalse(any(
            arguments.get("path") == "debug_test.py"
            for _name, arguments in toolbox.calls
        ))
        first_repair_context = json.dumps(
            client.requests[2]["messages"], ensure_ascii=False
        )
        post_rejection_context = json.dumps(
            client.requests[3]["messages"], ensure_ascii=False
        )
        second_repair_context = json.dumps(
            client.requests[4]["messages"], ensure_ascii=False
        )
        self.assertIn("Executable adversarial verification failed", first_repair_context)
        self.assertIn("AssertionError", first_repair_context)
        self.assertIn("Repair opportunity 1 of 2", first_repair_context)
        self.assertIn(
            "Test files are immutable during executable-counterexample repair",
            post_rejection_context,
        )
        self.assertNotIn("Repair opportunity 2 of 2", post_rejection_context)
        self.assertIn("Repair opportunity 2 of 2", second_repair_context)
        self.assertEqual(toolbox.files["rollup.py"], GOOD_EVENT_ROLLUP)
        self.assertEqual(
            sum(event.startswith("adversarial verification failed - repair") for event in events),
            2,
        )


if __name__ == "__main__":
    unittest.main()
