from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable

from .ollama_client import OllamaClient


DISTILLATION_FORMAT_VERSION = 1
MAX_JSONL_BYTES = 64 * 1024 * 1024
MAX_FILE_BYTES = 512 * 1024
MAX_CANDIDATE_BYTES = 2 * 1024 * 1024
MAX_VERIFIER_SECONDS = 300
_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{2,119}$")
_SECRET = re.compile(
    r"(?is)(-----BEGIN [A-Z ]*PRIVATE KEY-----|"
    r"\bsk-(?:proj-)?[A-Za-z0-9_-]{12,}|"
    r"\bgh[pousr]_[A-Za-z0-9_-]{16,}|"
    r"\bxox[baprs]-[A-Za-z0-9-]{12,}|"
    r"\bAKIA[0-9A-Z]{16}\b|"
    r"\b(?:password|passwd|api.?key|access.?token|secret)\s*[:=]\s*[^&\s]+)"
)
_ALLOWED_IMPORTS = frozenset({
    "collections", "dataclasses", "decimal", "fractions", "functools", "hashlib",
    "heapq", "itertools", "json", "math", "operator", "re", "statistics", "string",
    "typing",
})
_BANNED_CALLS = frozenset({
    "breakpoint", "compile", "eval", "exec", "globals", "input", "locals", "open",
    "vars", "__import__",
})
_BANNED_ATTRIBUTES = frozenset({
    "chmod", "connect", "fork", "kill", "open", "popen", "remove", "rename",
    "rmdir", "rmtree", "send", "spawn", "start", "system", "unlink", "write_bytes",
    "write_text",
})


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _atomic_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        Path(temporary).unlink(missing_ok=True)
        raise


def _write_jsonl(path: Path, records: Iterable[dict[str, Any]]) -> None:
    content = "".join(_canonical_json(record) + "\n" for record in records)
    _atomic_text(path, content)


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    path = Path(path).resolve()
    if not path.is_file() or path.is_symlink():
        raise ValueError(f"JSONL file does not exist or is not an ordinary file: {path}")
    if path.stat().st_size > MAX_JSONL_BYTES:
        raise ValueError(f"JSONL file exceeds {MAX_JSONL_BYTES} bytes: {path}")
    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSON on line {line_number} of {path.name}") from exc
            if not isinstance(value, dict):
                raise ValueError(f"Line {line_number} of {path.name} must be a JSON object")
            records.append(value)
    return records


def _safe_relative_path(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("File paths must be non-empty strings")
    raw = value.strip().replace("\\", "/")
    path = PurePosixPath(raw)
    raw_parts = raw.split("/")
    if (
        path.is_absolute()
        or raw.startswith("//")
        or re.match(r"^[A-Za-z]:", raw)
        or any(part in {"", ".", ".."} for part in raw_parts)
    ):
        raise ValueError(f"Unsafe relative path: {value}")
    normalized = path.as_posix()
    if len(normalized) > 240:
        raise ValueError("Relative path is too long")
    return normalized


def family_split(family: str) -> str:
    bucket = int(_sha256_text(family)[:8], 16) % 100
    return "train" if bucket < 80 else "validation" if bucket < 90 else "test"


def _validated_files(value: Any, *, label: str) -> dict[str, str]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object mapping paths to text")
    result: dict[str, str] = {}
    total = 0
    for raw_path, raw_content in value.items():
        path = _safe_relative_path(raw_path)
        if not isinstance(raw_content, str):
            raise ValueError(f"{label} content for {path} must be text")
        size = len(raw_content.encode("utf-8"))
        if size > MAX_FILE_BYTES:
            raise ValueError(f"{label} file exceeds {MAX_FILE_BYTES} bytes: {path}")
        total += size
        result[path] = raw_content
    if total > MAX_CANDIDATE_BYTES:
        raise ValueError(f"{label} exceeds {MAX_CANDIDATE_BYTES} bytes in total")
    return result


def _validate_verifiers(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise ValueError("Every task requires at least one verifier")
    result: list[dict[str, Any]] = []
    for verifier in value:
        if not isinstance(verifier, dict) or verifier.get("type") != "python":
            raise ValueError("Only the restricted python verifier type is supported")
        argv = verifier.get("argv")
        if (
            not isinstance(argv, list)
            or not argv
            or argv[0] != "$PYTHON"
            or not all(isinstance(item, str) and item for item in argv)
        ):
            raise ValueError("Python verifier argv must start with $PYTHON")
        timeout = verifier.get("timeout_seconds", 30)
        if isinstance(timeout, bool) or not isinstance(timeout, int):
            raise ValueError("Verifier timeout must be an integer")
        if not 1 <= timeout <= MAX_VERIFIER_SECONDS:
            raise ValueError(f"Verifier timeout must be from 1 to {MAX_VERIFIER_SECONDS}")
        result.append({"type": "python", "argv": list(argv), "timeout_seconds": timeout})
    return result


def load_tasks(path: Path) -> list[dict[str, Any]]:
    records = _load_jsonl(path)
    tasks: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for record in records:
        task_id = record.get("task_id")
        family = record.get("family")
        prompt = record.get("prompt")
        if not isinstance(task_id, str) or not _ID.fullmatch(task_id):
            raise ValueError("Task IDs must use lowercase letters, digits, dots, dashes, or underscores")
        if task_id in seen_ids:
            raise ValueError(f"Duplicate task ID: {task_id}")
        if not isinstance(family, str) or not _ID.fullmatch(family):
            raise ValueError(f"Task {task_id} has an invalid family")
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 20_000:
            raise ValueError(f"Task {task_id} has an invalid prompt")
        initial_files = _validated_files(record.get("initial_files", {}), label="initial_files")
        hidden_files = _validated_files(record.get("hidden_files", {}), label="hidden_files")
        allowed = record.get("allowed_write_paths")
        if not isinstance(allowed, list) or not allowed:
            raise ValueError(f"Task {task_id} requires allowed_write_paths")
        allowed_paths = [_safe_relative_path(item) for item in allowed]
        if len(set(allowed_paths)) != len(allowed_paths):
            raise ValueError(f"Task {task_id} has duplicate allowed write paths")
        if set(allowed_paths) & set(hidden_files):
            raise ValueError(f"Task {task_id} exposes a hidden file as writable")
        tasks.append({
            "task_id": task_id,
            "family": family,
            "split": family_split(family),
            "kind": str(record.get("kind", "coding"))[:40],
            "prompt": prompt.strip(),
            "initial_files": initial_files,
            "hidden_files": hidden_files,
            "allowed_write_paths": allowed_paths,
            "verifiers": _validate_verifiers(record.get("verifiers")),
        })
        seen_ids.add(task_id)
    if not tasks:
        raise ValueError("Task pack is empty")
    family_splits: dict[str, str] = {}
    for task in tasks:
        previous = family_splits.setdefault(task["family"], task["split"])
        if previous != task["split"]:
            raise AssertionError("A task family crossed dataset splits")
    return tasks


def _seed_task(
    task_id: str,
    family: str,
    prompt: str,
    stub: str,
    tests: str,
) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "family": family,
        "kind": "coding",
        "prompt": prompt,
        "initial_files": {"solution.py": stub},
        "hidden_files": {".hidden/test_solution.py": tests},
        "allowed_write_paths": ["solution.py"],
        "verifiers": [{
            "type": "python",
            "argv": ["$PYTHON", "-I", "-B", "-m", "unittest", "discover", "-s", ".hidden", "-p", "test_*.py", "-q"],
            "timeout_seconds": 30,
        }],
    }


def seed_tasks() -> list[dict[str, Any]]:
    test_header = (
        "import sys, unittest\nfrom pathlib import Path\n"
        "sys.path.insert(0, str(Path(__file__).resolve().parents[1]))\n"
    )
    return [
        _seed_task(
            "python-text.clip-001", "python-text",
            "Implement clip_text(value, limit). It must return text no longer than limit, preserve short text, reject limits below 4, and use an ellipsis when clipping.",
            "def clip_text(value, limit):\n    raise NotImplementedError\n",
            test_header + "from solution import clip_text\nclass T(unittest.TestCase):\n def test_short(self): self.assertEqual(clip_text('abc', 5), 'abc')\n def test_clip(self): self.assertEqual(clip_text('abcdef', 5), 'ab...')\n def test_bound(self): self.assertEqual(len(clip_text(123456, 4)), 4)\n def test_limit(self):\n  with self.assertRaises(ValueError): clip_text('x', 3)\n",
        ),
        _seed_task(
            "safe-paths.relative-001", "safe-paths",
            "Implement safe_relative_path(value). Normalize backslashes to slashes and return a safe relative POSIX path. Reject absolute paths, drive paths, empty segments, dot segments, and parent traversal with ValueError.",
            "def safe_relative_path(value):\n    raise NotImplementedError\n",
            test_header + "from solution import safe_relative_path\nclass T(unittest.TestCase):\n def test_ok(self): self.assertEqual(safe_relative_path(r'src\\app.py'), 'src/app.py')\n def test_bad(self):\n  for p in ('../x','a/../x','/tmp/x','C:/x','a//b','./x',''):\n   with self.subTest(p=p), self.assertRaises(ValueError): safe_relative_path(p)\n",
        ),
        _seed_task(
            "retry-policy.delay-001", "retry-policy",
            "Implement retry_delay(attempt, base=5, maximum=300). Attempts are one-based, invalid inputs raise ValueError, and delay grows exponentially but is capped at maximum.",
            "def retry_delay(attempt, base=5, maximum=300):\n    raise NotImplementedError\n",
            test_header + "from solution import retry_delay\nclass T(unittest.TestCase):\n def test_values(self): self.assertEqual([retry_delay(i) for i in (1,2,3,20)], [5,10,20,300])\n def test_custom(self): self.assertEqual(retry_delay(4, 2, 10), 10)\n def test_bad(self):\n  for args in ((0,), (1,0,3), (1,5,4)):\n   with self.assertRaises(ValueError): retry_delay(*args)\n",
        ),
        _seed_task(
            "redaction.secrets-001", "redaction",
            "Implement redact_secrets(text). Replace OpenAI-style sk- tokens and case-insensitive password/api_key assignments with [REDACTED], while leaving ordinary text unchanged.",
            "def redact_secrets(text):\n    raise NotImplementedError\n",
            test_header + "from solution import redact_secrets\nclass T(unittest.TestCase):\n def test_tokens(self):\n  out=redact_secrets('token sk-proj-abcdefghijklmnop password=hunter2')\n  self.assertNotIn('abcdefghijklmnop', out); self.assertNotIn('hunter2', out); self.assertEqual(out.count('[REDACTED]'),2)\n def test_key(self): self.assertEqual(redact_secrets('API_KEY: abc123'), '[REDACTED]')\n def test_plain(self): self.assertEqual(redact_secrets('hello world'), 'hello world')\n",
        ),
        _seed_task(
            "tool-routing.intent-001", "tool-routing",
            "Implement route_intent(prompt). Return coding for requests to build, fix, debug, refactor, or edit code; research for explicit web/current/latest research; otherwise fast. Match whole words case-insensitively.",
            "def route_intent(prompt):\n    raise NotImplementedError\n",
            test_header + "from solution import route_intent\nclass T(unittest.TestCase):\n def test_routes(self):\n  self.assertEqual(route_intent('Fix this Python bug'), 'coding')\n  self.assertEqual(route_intent('research the latest Ollama release'), 'research')\n  self.assertEqual(route_intent('what up bro'), 'fast')\n def test_words(self): self.assertEqual(route_intent('show me a building'), 'fast')\n",
        ),
        _seed_task(
            "memory-bounds.topk-001", "memory-bounds",
            "Implement bounded_memories(items, limit=3, max_chars=120). Keep at most limit items in order. Clip each string as needed so total output characters never exceed max_chars. Reject nonpositive limits.",
            "def bounded_memories(items, limit=3, max_chars=120):\n    raise NotImplementedError\n",
            test_header + "from solution import bounded_memories\nclass T(unittest.TestCase):\n def test_bounds(self):\n  out=bounded_memories(['a'*100,'b'*100,'c'*100,'d'],3,120)\n  self.assertLessEqual(len(out),3); self.assertLessEqual(sum(map(len,out)),120); self.assertTrue(out[0].startswith('a'))\n def test_short(self): self.assertEqual(bounded_memories(['a','bb'],3,10), ['a','bb'])\n def test_bad(self):\n  with self.assertRaises(ValueError): bounded_memories([],0,10)\n",
        ),
    ]


def initialize_pack(root: Path) -> dict[str, Any]:
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    tasks_path = root / "tasks.jsonl"
    seeded = seed_tasks()
    if tasks_path.exists():
        existing = load_tasks(tasks_path)
        return {"created": False, "tasks": len(existing), "root": str(root)}
    _write_jsonl(tasks_path, seeded)
    _atomic_text(root / "candidates.jsonl", "")
    _atomic_text(root / "verified.jsonl", "")
    manifest = {
        "format_version": DISTILLATION_FORMAT_VERSION,
        "created_at": _now_iso(),
        "tasks": len(seeded),
        "family_split": "sha256(family), 80/10/10",
        "hidden_tests": True,
    }
    _atomic_text(root / "manifest.json", json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return {"created": True, "tasks": len(seeded), "root": str(root)}


def _teacher_prompt(task: dict[str, Any]) -> str:
    visible = {
        "task": task["prompt"],
        "initial_files": task["initial_files"],
        "allowed_write_paths": task["allowed_write_paths"],
    }
    return (
        "Solve this coding task. Return exactly one JSON object with keys summary and files. "
        "summary must be a short factual completion note. files must map every changed relative "
        "path to its complete final UTF-8 text. Do not use markdown, reveal chain-of-thought, "
        "or mention tests you cannot see. Do not write any unlisted path.\n"
        + _canonical_json(visible)
    )


def _parse_completion(content: str, task: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(content, str) or not content.strip():
        raise ValueError("Teacher returned no content")
    try:
        value = json.loads(content)
    except json.JSONDecodeError as exc:
        raise ValueError("Teacher response is not exact JSON") from exc
    if not isinstance(value, dict) or set(value) != {"summary", "files"}:
        raise ValueError("Teacher JSON must contain exactly summary and files")
    summary = value.get("summary")
    if not isinstance(summary, str) or not summary.strip() or len(summary) > 2_000:
        raise ValueError("Teacher summary is invalid")
    files = _validated_files(value.get("files"), label="candidate files")
    if not files:
        raise ValueError("Teacher candidate did not change any files")
    unexpected = set(files) - set(task["allowed_write_paths"])
    if unexpected:
        raise ValueError(f"Teacher candidate wrote unapproved paths: {sorted(unexpected)}")
    serialized = _canonical_json(value)
    if _SECRET.search(serialized):
        raise ValueError("Teacher candidate contains a possible secret")
    return {"summary": summary.strip(), "files": files}


def static_python_audit(files: dict[str, str]) -> list[str]:
    """Reject obvious host-access and dynamic-code primitives before any execution."""
    violations: list[str] = []
    for path, content in files.items():
        if not path.casefold().endswith(".py"):
            continue
        try:
            tree = ast.parse(content, filename=path)
        except SyntaxError as exc:
            violations.append(f"{path}: syntax error on line {exc.lineno or '?'}")
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root = alias.name.split(".", 1)[0]
                    if root not in _ALLOWED_IMPORTS:
                        violations.append(f"{path}:{node.lineno}: import {root} is not allowed")
            elif isinstance(node, ast.ImportFrom):
                root = (node.module or "").split(".", 1)[0]
                if node.level or root not in _ALLOWED_IMPORTS:
                    violations.append(f"{path}:{node.lineno}: import from {node.module!r} is not allowed")
            elif isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name) and node.func.id in _BANNED_CALLS:
                    violations.append(f"{path}:{node.lineno}: call to {node.func.id} is not allowed")
                if isinstance(node.func, ast.Attribute) and (
                    node.func.attr in _BANNED_ATTRIBUTES or node.func.attr.startswith("__")
                ):
                    violations.append(f"{path}:{node.lineno}: call to .{node.func.attr} is not allowed")
            elif isinstance(node, ast.Attribute) and node.attr.startswith("__"):
                violations.append(f"{path}:{node.lineno}: dunder attribute access is not allowed")
    return sorted(set(violations))


def generate_candidates(
    tasks_path: Path,
    output_path: Path,
    client: OllamaClient,
    model: str,
    *,
    limit: int | None = None,
) -> dict[str, Any]:
    tasks = load_tasks(tasks_path)
    output_path = Path(output_path).resolve()
    existing = _load_jsonl(output_path) if output_path.exists() and output_path.stat().st_size else []
    completed_ids = {
        record.get("task_id") for record in existing if record.get("accepted_schema") is True
    }
    pending = [task for task in tasks if task["task_id"] not in completed_ids]
    if limit is not None:
        pending = pending[: max(0, int(limit))]
    generated = 0
    rejected = 0
    records = list(existing)
    for task in pending:
        response = client.chat(
            [
                {"role": "system", "content": "You are a precise senior Python engineer producing verifiable artifacts."},
                {"role": "user", "content": _teacher_prompt(task)},
            ],
            [],
            model,
            context_length=16384,
            think=False,
            temperature=0.1,
        )
        raw_content = response.get("content", "")
        record: dict[str, Any] = {
            "format_version": DISTILLATION_FORMAT_VERSION,
            "task_id": task["task_id"],
            "family": task["family"],
            "split": task["split"],
            "teacher": model,
            "created_at": _now_iso(),
            "prompt_sha256": _sha256_text(_teacher_prompt(task)),
        }
        try:
            completion = _parse_completion(raw_content, task)
        except ValueError as exc:
            record.update({"accepted_schema": False, "rejection": str(exc)[:500]})
            rejected += 1
        else:
            record.update({
                "accepted_schema": True,
                "completion": completion,
                "completion_sha256": _sha256_text(_canonical_json(completion)),
            })
            generated += 1
        records.append(record)
        _write_jsonl(output_path, records)
    return {
        "attempted": len(pending),
        "generated": generated,
        "schema_rejected": rejected,
        "total_records": len(records),
    }


def _write_workspace_file(root: Path, relative: str, content: str) -> None:
    target = (root / Path(relative)).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        raise ValueError(f"Resolved file escaped verification workspace: {relative}") from None
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8", newline="\n")


def _run_verifier(
    verifier: dict[str, Any],
    workspace: Path,
    runner: Callable[..., subprocess.CompletedProcess[str]],
) -> dict[str, Any]:
    argv = [sys.executable if part == "$PYTHON" else part for part in verifier["argv"]]
    environment = {
        key: value
        for key, value in os.environ.items()
        if key.casefold() in {"systemroot", "windir", "path", "pathext", "tmp", "temp"}
    }
    started = datetime.now(timezone.utc)
    try:
        completed = runner(
            argv,
            cwd=workspace,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=verifier["timeout_seconds"],
            shell=False,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {"passed": False, "returncode": None, "error": "timeout", "duration_ms": verifier["timeout_seconds"] * 1000}
    duration = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
    return {
        "passed": completed.returncode == 0,
        "returncode": completed.returncode,
        "stdout": (completed.stdout or "")[-8_000:],
        "stderr": (completed.stderr or "")[-8_000:],
        "duration_ms": duration,
        "argv": verifier["argv"],
        "timeout_seconds": verifier["timeout_seconds"],
    }


def verify_candidates(
    tasks_path: Path,
    candidates_path: Path,
    output_path: Path,
    *,
    allow_host_execution: bool,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, Any]:
    if not allow_host_execution:
        raise PermissionError(
            "Candidate verification executes generated code; pass --allow-host-execution only after reviewing candidates.jsonl"
        )
    tasks = {task["task_id"]: task for task in load_tasks(tasks_path)}
    candidates = _load_jsonl(candidates_path)
    verified_records: list[dict[str, Any]] = []
    passed = 0
    failed = 0
    for candidate in candidates:
        if not candidate.get("accepted_schema"):
            continue
        task_id = candidate.get("task_id")
        task = tasks.get(task_id)
        record: dict[str, Any] = {
            "format_version": DISTILLATION_FORMAT_VERSION,
            "task_id": task_id,
            "verified_at": _now_iso(),
            "teacher": candidate.get("teacher"),
        }
        if task is None:
            record.update({"reward": 0.0, "passed": False, "rejection": "unknown task"})
            verified_records.append(record)
            failed += 1
            continue
        if candidate.get("family") != task["family"] or candidate.get("split") != task["split"]:
            record.update({"reward": 0.0, "passed": False, "rejection": "task metadata mismatch"})
            verified_records.append(record)
            failed += 1
            continue
        try:
            completion = _parse_completion(_canonical_json(candidate.get("completion")), task)
        except ValueError as exc:
            record.update({"reward": 0.0, "passed": False, "rejection": str(exc)[:500]})
            verified_records.append(record)
            failed += 1
            continue
        audit_violations = static_python_audit(completion["files"])
        if audit_violations:
            record.update({
                "family": task["family"],
                "split": task["split"],
                "kind": task["kind"],
                "reward": 0.0,
                "passed": False,
                "static_audit": audit_violations,
                "rejection": "static safety audit failed",
            })
            verified_records.append(record)
            failed += 1
            continue
        with tempfile.TemporaryDirectory(prefix="jarvis-verify-") as temporary:
            workspace = Path(temporary).resolve()
            for path, content in task["initial_files"].items():
                _write_workspace_file(workspace, path, content)
            for path, content in task["hidden_files"].items():
                _write_workspace_file(workspace, path, content)
            for path, content in completion["files"].items():
                _write_workspace_file(workspace, path, content)
            verification = [
                _run_verifier(verifier, workspace, runner)
                for verifier in task["verifiers"]
            ]
        success = bool(verification) and all(item["passed"] for item in verification)
        record.update({
            "family": task["family"],
            "split": task["split"],
            "kind": task["kind"],
            "prompt": task["prompt"],
            "initial_files": task["initial_files"],
            "allowed_write_paths": task["allowed_write_paths"],
            "completion": completion,
            "completion_sha256": _sha256_text(_canonical_json(completion)),
            "static_audit": [],
            "verification": verification,
            "passed": success,
            "reward": 1.0 if success else 0.0,
        })
        verified_records.append(record)
        passed += int(success)
        failed += int(not success)
    _write_jsonl(Path(output_path).resolve(), verified_records)
    return {"verified": len(verified_records), "passed": passed, "failed": failed}


_READ_TOOL = {
    "type": "function",
    "function": {
        "name": "read_file",
        "description": "Read a bounded text range with its file hash and truncation metadata.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "start_line": {"type": "integer"},
                "end_line": {"type": "integer"},
            },
            "required": ["path"],
        },
    },
}
_WRITE_TOOL = {
    "type": "function",
    "function": {
        "name": "write_file",
        "description": "Atomically create a file or replace a previously read file using its required SHA-256 hash.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
                "expected_sha256": {"type": "string"},
            },
            "required": ["path", "content"],
        },
    },
}
_RUN_TOOL = {
    "type": "function",
    "function": {
        "name": "run_process",
        "description": "Run one allowlisted build/test executable directly without a shell. Trusted-host mode is not a sandbox and repository code runs with the full user account authority.",
        "parameters": {
            "type": "object",
            "properties": {
                "program": {"type": "string"},
                "arguments": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": 256,
                },
                "cwd": {"type": "string"},
                "timeout": {"type": "integer", "minimum": 1, "maximum": 600},
            },
            "required": ["program"],
        },
    },
}


def _synthetic_read_result(path: str, content: str) -> dict[str, Any]:
    lines = content.splitlines()
    return {
        "path": path,
        "content": "\n".join(
            f"{index}: {line}" for index, line in enumerate(lines, 1)
        ),
        "sha256": _sha256_text(content),
        "encoding": "utf-8",
        "start_line": 1,
        "end_line": len(lines),
        "total_lines": len(lines),
        "truncated": False,
    }


def _synthetic_backup_path(path: str) -> str:
    target = PurePosixPath(path)
    return target.with_name(f".{target.name}.jarvis-backup").as_posix()


def _sft_record(record: dict[str, Any]) -> dict[str, Any]:
    completion = record["completion"]
    initial_files = record["initial_files"]
    initial_hashes = {
        path: _sha256_text(content) for path, content in initial_files.items()
    }
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": "You are JARVIS. Inspect first, make only requested workspace changes, verify them, and report only observed results."},
        {
            "role": "user",
            "content": (
                record["prompt"]
                + "\nExisting workspace file paths:\n"
                + _canonical_json(sorted(initial_files))
            ),
        },
    ]
    for index, path in enumerate(sorted(initial_files), 1):
        call_id = f"inspect_{index}"
        messages.append({
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": call_id,
                "type": "function",
                "function": {"name": "read_file", "arguments": {"path": path}},
            }],
        })
        messages.append({
            "role": "tool",
            "name": "read_file",
            "tool_call_id": call_id,
            "content": _canonical_json({
                "ok": True,
                "result": _synthetic_read_result(path, initial_files[path]),
            }),
        })
    for index, (path, content) in enumerate(completion["files"].items(), 1):
        call_id = f"write_{index}"
        arguments = {"path": path, "content": content}
        if path in initial_hashes:
            arguments["expected_sha256"] = initial_hashes[path]
        messages.append({
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": call_id,
                "type": "function",
                "function": {"name": "write_file", "arguments": arguments},
            }],
        })
        messages.append({
            "role": "tool",
            "name": "write_file",
            "tool_call_id": call_id,
            "content": _canonical_json({
                "ok": True,
                "result": {
                    "path": path,
                    "characters": len(content),
                    "sha256": _sha256_text(content),
                    "backup": (
                        _synthetic_backup_path(path) if path in initial_files else None
                    ),
                    "encoding": "utf-8",
                    "newline": "LF",
                },
            }),
        })
    for index, result in enumerate(record["verification"], 1):
        call_id = f"verify_{index}"
        argv = list(result.get("argv", []))
        if not argv or argv[0] != "$PYTHON":
            raise ValueError("Verified Python trace has an invalid verifier command")
        program = "python"
        arguments = [str(item) for item in argv[1:]]
        timeout = int(result.get("timeout_seconds", 30))
        messages.append({
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": call_id,
                "type": "function",
                "function": {
                    "name": "run_process",
                    "arguments": {
                        "program": program,
                        "arguments": arguments,
                        "cwd": ".",
                        "timeout": timeout,
                    },
                },
            }],
        })
        messages.append({
            "role": "tool",
            "name": "run_process",
            "tool_call_id": call_id,
            "content": _canonical_json({
                "ok": True,
                "result": {
                    "exit_code": result.get("returncode"),
                    "timed_out": False,
                    "stdout": result.get("stdout", ""),
                    "stderr": result.get("stderr", ""),
                },
            }),
        })
    messages.append({"role": "assistant", "content": completion["summary"]})
    return {
        "messages": messages,
        "tools": [_READ_TOOL, _WRITE_TOOL, _RUN_TOOL],
        "metadata": {
            "task_id": record["task_id"],
            "family": record["family"],
            "split": record["split"],
            "teacher": record.get("teacher"),
            "reward": record["reward"],
            "completion_sha256": record["completion_sha256"],
        },
    }


def export_sft_dataset(
    verified_path: Path,
    output_dir: Path,
    *,
    constitution_sha256: str | None = None,
) -> dict[str, Any]:
    if constitution_sha256 is not None and not re.fullmatch(
        r"[0-9a-f]{64}", constitution_sha256
    ):
        raise ValueError("Constitution SHA-256 must be 64 lowercase hex characters")
    records = _load_jsonl(verified_path)
    accepted = [
        record for record in records
        if record.get("passed") is True and float(record.get("reward", 0.0)) == 1.0
    ]
    rendered: dict[str, list[dict[str, Any]]] = {"train": [], "validation": [], "test": []}
    seen_hashes: set[str] = set()
    seen_task_ids: set[str] = set()
    family_locations: dict[str, str] = {}
    for record in accepted:
        family = str(record.get("family", ""))
        expected_split = family_split(family)
        if record.get("split") != expected_split:
            continue
        previous = family_locations.setdefault(family, expected_split)
        if previous != expected_split:
            raise ValueError(f"Family leakage detected for {family}")
        completion_hash = str(record.get("completion_sha256", ""))
        task_id = str(record.get("task_id", ""))
        if not task_id or task_id in seen_task_ids or not completion_hash or completion_hash in seen_hashes:
            continue
        seen_task_ids.add(task_id)
        seen_hashes.add(completion_hash)
        rendered[expected_split].append(_sft_record(record))
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    files: dict[str, Any] = {}
    for split in ("train", "validation", "test"):
        path = output_dir / f"{split}.jsonl"
        content = "".join(_canonical_json(item) + "\n" for item in rendered[split])
        _atomic_text(path, content)
        files[split] = {
            "file": path.name,
            "examples": len(rendered[split]),
            "sha256": _sha256_text(content),
        }
    manifest = {
        "format_version": DISTILLATION_FORMAT_VERSION,
        "created_at": _now_iso(),
        "selection": {"passed_only": True, "exact_reward": 1.0, "family_grouped_splits": True},
        "total_examples": sum(len(items) for items in rendered.values()),
        "families": dict(Counter(record["family"] for record in accepted if record.get("family"))),
        "files": files,
    }
    if constitution_sha256 is not None:
        manifest["constitution_sha256"] = constitution_sha256
    _atomic_text(output_dir / "manifest.json", json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return manifest


def export_reward_dataset(
    tasks_path: Path,
    output_dir: Path,
    *,
    constitution_sha256: str | None = None,
) -> dict[str, Any]:
    """Export GRPO inputs while keeping hidden checks out of the model prompt."""
    if constitution_sha256 is not None and not re.fullmatch(
        r"[0-9a-f]{64}", constitution_sha256
    ):
        raise ValueError("Constitution SHA-256 must be 64 lowercase hex characters")
    tasks = load_tasks(tasks_path)
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    rendered: dict[str, list[dict[str, Any]]] = {"train": [], "validation": [], "test": []}
    for task in tasks:
        rendered[task["split"]].append({
            "prompt": [
                {
                    "role": "system",
                    "content": (
                        "Return exactly one JSON object with keys summary and files. "
                        "Write only approved paths and provide complete file contents."
                    ),
                },
                {
                    "role": "user",
                    "content": task["prompt"] + "\nInitial files:\n" + _canonical_json(task["initial_files"]),
                },
            ],
            "environment": "jarvis_python",
            "reward_spec": {
                "initial_files": task["initial_files"],
                "hidden_files": task["hidden_files"],
                "allowed_write_paths": task["allowed_write_paths"],
                "verifiers": task["verifiers"],
            },
            "task_id": task["task_id"],
            "family": task["family"],
            "split": task["split"],
        })
    files: dict[str, Any] = {}
    for split in ("train", "validation", "test"):
        path = output_dir / f"{split}.jsonl"
        content = "".join(_canonical_json(record) + "\n" for record in rendered[split])
        _atomic_text(path, content)
        files[split] = {
            "file": path.name,
            "tasks": len(rendered[split]),
            "sha256": _sha256_text(content),
        }
    manifest = {
        "format_version": DISTILLATION_FORMAT_VERSION,
        "created_at": _now_iso(),
        "environment": "jarvis_python",
        "reward": "1.0 only when every hidden verifier passes; otherwise 0.0",
        "host_execution_safe": False,
        "requires_isolated_sandbox": True,
        "total_tasks": len(tasks),
        "files": files,
    }
    if constitution_sha256 is not None:
        manifest["constitution_sha256"] = constitution_sha256
    _atomic_text(output_dir / "manifest.json", json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return manifest


def distillation_status(root: Path) -> dict[str, Any]:
    root = Path(root).resolve()
    tasks = load_tasks(root / "tasks.jsonl") if (root / "tasks.jsonl").exists() else []
    candidates = _load_jsonl(root / "candidates.jsonl") if (root / "candidates.jsonl").exists() and (root / "candidates.jsonl").stat().st_size else []
    verified = _load_jsonl(root / "verified.jsonl") if (root / "verified.jsonl").exists() and (root / "verified.jsonl").stat().st_size else []
    return {
        "tasks": len(tasks),
        "task_splits": dict(Counter(task["split"] for task in tasks)),
        "families": len({task["family"] for task in tasks}),
        "candidate_attempts": len(candidates),
        "schema_accepted": sum(record.get("accepted_schema") is True for record in candidates),
        "verified": len(verified),
        "passed": sum(record.get("passed") is True for record in verified),
        "failed": sum(record.get("passed") is False for record in verified),
    }
