from __future__ import annotations

import ast
import difflib
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from pathlib import PurePosixPath
from typing import Any
from uuid import uuid4

from .approvals import SENSITIVE_ACTIONS
from .config import SOURCE_ROOT, Config
from .memory import SCHEMA_VERSION, Memory
from .tools import ToolBox, _minimal_environment


SELFTEST_OUTPUT_LIMIT = 24_000
_COPY_EXCLUDES = frozenset({
    ".env", ".git", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tmp",
    ".venv", "__pycache__", "backups", "build", "data", "dist", "htmlcov",
    "workspace", "workspace-projects",
})
_IMMUTABLE_REPAIR_FILES = frozenset({
    "jarvis/agent.py",
    "jarvis/approvals.py",
    "jarvis/constitutional.py",
    "jarvis/config.py",
    "jarvis/memory.py",
    "jarvis/policy.py",
    "jarvis/proactive.py",
    "jarvis/public_bridge.py",
    "jarvis/public_presence_service.py",
    "jarvis/public_presence_store.py",
    "jarvis/public_tools.py",
    "jarvis/moltbook_adapter.py",
    "jarvis/redaction.py",
    "jarvis/self_diagnosis.py",
    "jarvis/specialists.py",
    "jarvis/tools.py",
    "constitution.md",
    "jarvis/constitution.md",
    "soul.md",
    "jarvis/soul.md",
    "promotion_gate.json",
    "promotion-gate.json",
    "public_policy.md",
    "public_profile.json",
    "public_soul.md",
})
MAX_REPAIR_FILES = 5
MAX_REPAIR_CHANGED_LINES = 400
ANCHOR_TEST_IDS = (
    "tests.test_redaction.SharedRedactionTests."
    "test_short_sensitive_assignments_share_one_key_policy",
    "tests.test_tools_hardening.ToolCapabilityHardeningTests."
    "test_credential_and_repository_control_paths_are_protected_everywhere",
    "tests.test_agent_hardening.AgentHardeningTests."
    "test_test_verification_rejects_skipped_and_incidental_runner_phrases",
    "tests.test_proactive.ProactiveSystemTests."
    "test_approval_is_bound_to_requesting_task_and_exact_resource",
    "tests.test_constitution_runtime.ConstitutionRuntimeTests."
    "test_prompt_order_is_contract_then_constitution_then_soul",
    "tests.test_config_security.ConfigSecurityTests."
    "test_self_repair_is_proposal_only_and_depends_on_read_only_inspection",
)


def _bounded_output(value: str, limit: int = SELFTEST_OUTPUT_LIMIT) -> str:
    if len(value) <= limit:
        return value
    half = max(1, (limit - 80) // 2)
    return value[:half] + "\n...[isolated self-test output clipped]...\n" + value[-half:]


def _copy_ignore(directory: str, names: list[str]) -> set[str]:
    ignored: set[str] = set()
    parent = Path(directory)
    for name in names:
        if name in _COPY_EXCLUDES or name.endswith((".pyc", ".pyo")):
            ignored.add(name)
            continue
        candidate = parent / name
        try:
            details = candidate.lstat()
        except OSError:
            ignored.add(name)
            continue
        attributes = getattr(details, "st_file_attributes", 0)
        if stat.S_ISLNK(details.st_mode) or attributes & getattr(
            stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400
        ):
            ignored.add(name)
    return ignored


def _selftest_environment(runtime_root: Path) -> dict[str, str]:
    # Start from the same isolated-home allowlist as workspace execution. This
    # prevents copied tests from reading ambient provider/connector credentials
    # through either environment variables or the operator's real home folders.
    environment = _minimal_environment(runtime_root)
    environment.update({
        "JARVIS_CLOUD_ENABLED": "false",
        "JARVIS_MODEL": "auto",
        "JARVIS_FAST_MODEL": "qwen3.5:9b",
        "JARVIS_REASONING_MODEL": "gpt-oss:20b",
        "JARVIS_CODING_MODEL": "qwen3-coder:30b",
        "JARVIS_DEEP_MODEL": "qwen3-coder:30b",
        "JARVIS_EXECUTION_MODE": "disabled",
        "JARVIS_COMPUTER_ACCESS": "disabled",
        "JARVIS_EXTERNAL_ACCESS": "disabled",
        "JARVIS_PROACTIVE_ENABLED": "false",
        "JARVIS_SELF_INSPECT": "disabled",
        "JARVIS_SELF_REPAIR": "disabled",
        "JARVIS_INITIATIVE": "disabled",
        "JARVIS_INITIATIVE_QUIET_HOURS": "",
        "PYTHONNOUSERSITE": "1",
    })
    return environment


def _failing_test_ids(output: str) -> list[str]:
    found: set[str] = set()
    for match in re.finditer(
        r"(?m)^(?:FAIL|ERROR):\s+\S+\s+\(([^)]+)\)\s*$",
        output,
    ):
        found.add(match.group(1).strip())
    return sorted(found)


def _test_file(source_root: Path, test_id: str) -> Path | None:
    parts = test_id.split(".")
    if not parts:
        return None
    if parts[0] == "tests" and len(parts) >= 2:
        candidate = source_root / "tests" / f"{parts[1]}.py"
    else:
        candidate = source_root / "tests" / f"{parts[0]}.py"
    return candidate if candidate.is_file() else None


def _imported_runtime_files(source_root: Path, test_file: Path) -> set[Path]:
    try:
        tree = ast.parse(test_file.read_text(encoding="utf-8"), filename=str(test_file))
    except (OSError, UnicodeError, SyntaxError):
        return set()
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names if alias.name.startswith("jarvis"))
        elif isinstance(node, ast.ImportFrom) and (node.module or "").startswith("jarvis"):
            modules.add(str(node.module))
    files: set[Path] = set()
    for module in modules:
        relative = Path(*module.split("."))
        module_file = source_root / relative.with_suffix(".py")
        package_file = source_root / relative / "__init__.py"
        if module_file.is_file():
            files.add(module_file.resolve())
        elif package_file.is_file():
            files.add(package_file.resolve())
    return files


def _last_commit(source_root: Path, candidate: Path) -> str | None:
    if not (source_root / ".git").exists():
        return None
    try:
        relative = candidate.relative_to(source_root)
        result = subprocess.run(
            ["git", "-C", str(source_root), "log", "-1", "--format=%h", "--", str(relative)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None
    value = result.stdout.strip()
    return value[:64] if result.returncode == 0 and value else None


def localize_failures(
    failing_tests: list[str],
    *,
    source_root: Path = SOURCE_ROOT,
) -> dict[str, Any]:
    """Rank runtime modules imported by failing tests without a model call."""
    root = Path(source_root).resolve()
    counts: dict[Path, int] = {}
    mapped = 0
    for test_id in sorted(set(failing_tests)):
        test_file = _test_file(root, test_id)
        if test_file is None:
            continue
        imported = _imported_runtime_files(root, test_file)
        if imported:
            mapped += 1
        for candidate in imported:
            counts[candidate] = counts.get(candidate, 0) + 1
    ranked = sorted(
        counts.items(),
        key=lambda item: (-item[1], -item[0].stat().st_mtime_ns, str(item[0])),
    )
    suspects = [
        {
            "module": str(path.relative_to(root)).replace("\\", "/"),
            "imported_by_failing_tests": count,
            "last_modified_ns": path.stat().st_mtime_ns,
            "last_commit": _last_commit(root, path),
        }
        for path, count in ranked[:10]
    ]
    return {
        "failing_tests": sorted(set(failing_tests)),
        "mapped_failing_tests": mapped,
        "suspect_modules": suspects,
        "confidence": (
            suspects[0]["imported_by_failing_tests"] / len(set(failing_tests))
            if suspects and failing_tests else 0.0
        ),
    }


def run_isolated_selftest(
    config: Config,
    *,
    full: bool = False,
    anchors: bool = False,
    timeout: int = 900,
) -> dict[str, Any]:
    """Copy the runtime, remove secrets/state, and test only the disposable copy."""
    if config.self_inspect != "read-only":
        raise PermissionError("Read-only self-inspection is disabled")
    if full and anchors:
        raise ValueError("Full and anchor self-test modes are mutually exclusive")
    timeout = max(30, min(int(timeout), 3_600))
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="jarvis-selftest-") as temporary:
        isolated = Path(temporary) / "runtime"
        shutil.copytree(SOURCE_ROOT, isolated, ignore=_copy_ignore)
        if anchors:
            command = [sys.executable, "-B", "-m", "unittest", *ANCHOR_TEST_IDS]
        elif full:
            command = [
                sys.executable, "-B", "-m", "unittest", "discover", "-s", "tests",
            ]
        else:
            command = [
                sys.executable, "-B", "-m", "unittest",
                "tests.test_config_security",
                "tests.test_memory",
                "tests.test_proactive",
                "tests.test_agent_fast_path",
            ]
        try:
            completed = subprocess.run(
                command,
                cwd=isolated,
                env=_selftest_environment(Path(temporary) / "process"),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                check=False,
            )
            timed_out = False
            return_code = int(completed.returncode)
            stdout = completed.stdout
            stderr = completed.stderr
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            return_code = None
            stdout = str(exc.stdout or "")
            stderr = str(exc.stderr or "")
    combined = f"{stdout}\n{stderr}"
    failures = _failing_test_ids(combined)
    localization = localize_failures(failures) if failures else None
    return {
        "passed": return_code == 0 and not timed_out,
        "full": bool(full),
        "anchors": bool(anchors),
        "return_code": return_code,
        "timed_out": timed_out,
        "duration_seconds": round(time.monotonic() - started, 3),
        "failing_test_ids": failures,
        "localization": localization,
        "stdout": _bounded_output(stdout),
        "stderr": _bounded_output(stderr),
        "isolation": {
            "copied_runtime": True,
            "live_tree_writable": False,
            "dotenv_copied": False,
            "provider_keys_inherited": False,
        },
    }


def run_capability_canaries(config: Config, memory: Memory) -> list[dict[str, Any]]:
    """Exercise safe registered handlers; every other tool is explicitly skipped."""
    toolbox = ToolBox(config, memory)
    descriptor, canary_name = tempfile.mkstemp(
        prefix=".jarvis-canary-", suffix=".txt", dir=config.workspace
    )
    os.close(descriptor)
    canary = Path(canary_name)
    canary.write_text("jarvis-canary\n", encoding="utf-8")
    relative = str(canary.relative_to(config.workspace)).replace("\\", "/")
    probes: dict[str, dict[str, Any]] = {
        "list_files": {"path": ".", "recursive": False},
        "read_file": {"path": relative, "start_line": 1, "end_line": 5},
        "search_files": {"pattern": "jarvis-canary", "path": "."},
        "detect_project": {"path": "."},
        "system_snapshot": {},
        "github_cli_status": {},
        "google_drive_status": {},
        "vercel_status": {},
    }
    if config.execution_mode == "trusted-host" and config.autonomy != "readonly":
        probes["run_process"] = {
            "program": "python", "arguments": ["--version"], "cwd": ".", "timeout": 30,
        }
    if set(probes) & set(SENSITIVE_ACTIONS):
        raise RuntimeError("A capability canary was assigned to a sensitive action")
    results: list[dict[str, Any]] = []
    try:
        for name in sorted(toolbox.tools):
            if name not in probes:
                results.append({
                    "tool": name,
                    "status": "skip",
                    "reason": "no non-mutating context-free canary",
                })
                continue
            payload = json.loads(toolbox.execute(name, probes[name]))
            approval_requested = bool(payload.get("approval_required"))
            results.append({
                "tool": name,
                "status": "pass" if payload.get("ok") and not approval_requested else "fail",
                "reason": None if payload.get("ok") else str(payload.get("error", "unknown failure"))[:500],
                "approval_requested": approval_requested,
            })
    finally:
        canary.unlink(missing_ok=True)
    return results


def _repair_path_reason(path: str) -> str | None:
    normalized = str(PurePosixPath(str(path).replace("\\", "/"))).casefold()
    parts = PurePosixPath(normalized).parts
    if ".." in parts:
        return "repair path may not contain parent-directory segments"
    if not normalized or normalized.startswith("../") or normalized.startswith("/"):
        return "repair path is not a canonical relative path"
    if "tests" in parts or any(part in {"test", "evaluation", "evaluations"} for part in parts):
        return "tests and evaluation fixtures are permanently immutable to self-repair"
    if normalized in _IMMUTABLE_REPAIR_FILES:
        return "approval, redaction, policy, verification, identity, or repair gates are permanently immutable"
    if not normalized.startswith("jarvis/") or not normalized.endswith(".py"):
        return "self-repair drafts are limited to non-protected Jarvis Python modules"
    return None


def _ordinary_repair_source(path: str) -> Path:
    """Resolve a repair source without accepting links or outside aliases."""
    root = SOURCE_ROOT.resolve(strict=True)
    relative = PurePosixPath(path)
    candidate = SOURCE_ROOT
    for index, part in enumerate(relative.parts):
        candidate = candidate / part
        try:
            details = os.lstat(candidate)
        except OSError as exc:
            raise OSError("repair source is unavailable") from exc
        attributes = getattr(details, "st_file_attributes", 0)
        if (
            stat.S_ISLNK(details.st_mode)
            or attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        ):
            raise PermissionError("repair source must not contain links or reparse points")
        leaf = index == len(relative.parts) - 1
        if leaf:
            if not stat.S_ISREG(details.st_mode) or getattr(details, "st_nlink", 1) != 1:
                raise PermissionError("repair source must be an ordinary single-link file")
        elif not stat.S_ISDIR(details.st_mode):
            raise PermissionError("repair source parents must be ordinary directories")
    try:
        candidate.resolve(strict=True).relative_to(root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise PermissionError("repair source must stay inside the source root") from exc
    return candidate


def _candidate_test(candidate: Path, timeout: int) -> dict[str, Any]:
    started = time.monotonic()
    try:
        completed = subprocess.run(
            [sys.executable, "-B", "-m", "unittest", "discover", "-s", "tests"],
            cwd=candidate,
            env=_selftest_environment(candidate.parent / "process"),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=max(30, min(int(timeout), 3_600)),
            check=False,
        )
        timed_out = False
        return_code = int(completed.returncode)
        stdout, stderr = completed.stdout, completed.stderr
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        return_code = None
        stdout, stderr = str(exc.stdout or ""), str(exc.stderr or "")
    combined = f"{stdout}\n{stderr}"
    return {
        "passed": return_code == 0 and not timed_out,
        "return_code": return_code,
        "timed_out": timed_out,
        "duration_seconds": round(time.monotonic() - started, 3),
        "failing_test_ids": _failing_test_ids(combined),
        "stdout": _bounded_output(stdout),
        "stderr": _bounded_output(stderr),
    }


def _candidate_execution_available() -> bool:
    """Fail closed until candidate code can run inside a real OS security boundary.

    A copied directory and a scrubbed environment protect Jarvis state and keys,
    but they do not remove the subprocess's current-user filesystem or network
    authority. Model-authored source must never be executed on that basis alone.
    """
    return False


def _candidate_anchor_test(candidate: Path, timeout: int) -> dict[str, Any]:
    """Run the immutable Phase 5 behavioral anchors in an isolated candidate."""
    started = time.monotonic()
    try:
        completed = subprocess.run(
            [sys.executable, "-B", "-m", "unittest", *ANCHOR_TEST_IDS],
            cwd=candidate,
            env=_selftest_environment(candidate.parent / "anchor-process"),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=max(30, min(int(timeout), 3_600)),
            check=False,
        )
        timed_out = False
        return_code = int(completed.returncode)
        stdout, stderr = completed.stdout, completed.stderr
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        return_code = None
        stdout, stderr = str(exc.stdout or ""), str(exc.stderr or "")
    combined = f"{stdout}\n{stderr}"
    return {
        "passed": return_code == 0 and not timed_out,
        "return_code": return_code,
        "timed_out": timed_out,
        "duration_seconds": round(time.monotonic() - started, 3),
        "cases": list(ANCHOR_TEST_IDS),
        "failing_test_ids": _failing_test_ids(combined),
        "stdout": _bounded_output(stdout),
        "stderr": _bounded_output(stderr),
    }


def create_repair_draft(
    config: Config,
    memory: Memory,
    *,
    trigger: str,
    edits: list[dict[str, str]],
    failing_tests: list[str] | None = None,
    timeout: int = 900,
) -> dict[str, Any]:
    """Create a static review draft in a private copy; never execute or apply it."""
    if getattr(config, "self_inspect", "disabled") != "read-only":
        raise PermissionError("Read-only self-inspection is disabled")
    if getattr(config, "self_repair", "disabled") != "propose":
        raise PermissionError("Self-repair proposal mode is disabled")
    raw_edits = list(edits)
    if not raw_edits or len(raw_edits) > MAX_REPAIR_FILES:
        reason = f"repair drafts require 1-{MAX_REPAIR_FILES} bounded file edits"
        proposal_id = memory.record_repair_proposal(
            trigger=trigger,
            failing_tests=failing_tests or [],
            diff_text="",
            verification={"passed": False, "protected_paths_unchanged": True},
            status="voided",
            void_reason=reason,
            candidate_path="",
        )
        return {"proposal_id": proposal_id, "status": "voided", "void_reason": reason}

    normalized_edits: list[dict[str, str]] = []
    seen: set[str] = set()
    void_reason: str | None = None
    for edit in raw_edits:
        path = str(edit.get("path") or "").replace("\\", "/")
        reason = _repair_path_reason(path)
        if reason:
            void_reason = f"{path or '<empty>'}: {reason}"
            break
        folded = path.casefold()
        if folded in seen:
            void_reason = f"{path}: duplicate repair path"
            break
        seen.add(folded)
        old_text = str(edit.get("old_text") or "")
        new_text = str(edit.get("new_text") or "")
        if not old_text or not new_text or old_text == new_text:
            void_reason = f"{path}: repair replacement must be non-empty and different"
            break
        if len(old_text) > 40_000 or len(new_text) > 40_000:
            void_reason = f"{path}: repair replacement exceeds the bounded draft size"
            break
        normalized_edits.append({"path": path, "old_text": old_text, "new_text": new_text})

    proposal_root = config.data_dir / "self_repair" / uuid4().hex
    candidate = proposal_root / "candidate"
    diff_text = ""
    verification: dict[str, Any] = {
        "passed": False,
        "protected_paths_unchanged": void_reason is None,
        "live_tree_written": False,
        "tests_modified": False,
    }
    if void_reason is None:
        proposal_root.mkdir(parents=True, exist_ok=False)
        shutil.copytree(SOURCE_ROOT, candidate, ignore=_copy_ignore)
        diff_parts: list[str] = []
        changed_lines = 0
        for edit in normalized_edits:
            candidate_path = candidate / PurePosixPath(edit["path"])
            try:
                live_path = _ordinary_repair_source(edit["path"])
                original = live_path.read_text(encoding="utf-8")
            except PermissionError as exc:
                void_reason = f"{edit['path']}: {exc}"
                break
            except OSError as exc:
                void_reason = f"{edit['path']}: source unavailable ({type(exc).__name__})"
                break
            if original.count(edit["old_text"]) != 1:
                void_reason = f"{edit['path']}: old_text must match exactly once"
                break
            revised = original.replace(edit["old_text"], edit["new_text"], 1)
            try:
                ast.parse(revised, filename=edit["path"])
            except SyntaxError as exc:
                void_reason = f"{edit['path']}: candidate is not valid Python ({exc.msg})"
                break
            candidate_path.write_text(revised, encoding="utf-8", newline="\n")
            lines = list(difflib.unified_diff(
                original.splitlines(), revised.splitlines(),
                fromfile=f"a/{edit['path']}", tofile=f"b/{edit['path']}", lineterm="",
            ))
            changed_lines += sum(
                line.startswith(("+", "-")) and not line.startswith(("+++", "---"))
                for line in lines
            )
            diff_parts.extend(lines)
        diff_text = "\n".join(diff_parts)
        verification["changed_files"] = len(normalized_edits)
        verification["changed_lines"] = changed_lines
        if void_reason is None and changed_lines > MAX_REPAIR_CHANGED_LINES:
            void_reason = (
                f"repair changes {changed_lines} lines; maximum is {MAX_REPAIR_CHANGED_LINES}"
            )
        if void_reason is None and not _candidate_execution_available():
            verification["execution_blocked"] = True
            verification["execution_security"] = "os_sandbox_required"
            void_reason = (
                "candidate execution is disabled until a real OS sandbox is available"
            )
        if void_reason is None:
            verification.update(_candidate_test(candidate, timeout))
            if not verification["passed"]:
                void_reason = "isolated full test suite did not pass"
        if void_reason is None:
            anchor_eval = _candidate_anchor_test(candidate, timeout)
            verification["anchor_eval"] = anchor_eval
            verification["passed"] = bool(
                verification["passed"] and anchor_eval["passed"]
            )
            if not anchor_eval["passed"]:
                void_reason = "immutable Phase 5 anchor evaluation did not pass"

    status = "voided" if void_reason else "proposed"
    proposal_id = memory.record_repair_proposal(
        trigger=trigger,
        failing_tests=failing_tests or [],
        diff_text=diff_text,
        verification=verification,
        status=status,
        void_reason=void_reason,
        candidate_path=str(candidate) if candidate.exists() else "",
    )
    stored = memory.get_repair_proposal(proposal_id)
    if stored is None:
        raise RuntimeError("Repair draft was not persisted")
    return {
        "proposal_id": proposal_id,
        "status": status,
        "void_reason": void_reason,
        # Bind the result to the redacted, reviewable diff that was persisted.
        "diff_sha256": str(stored["diff_sha256"]),
        "verification": verification,
        "candidate_path": str(candidate) if candidate.exists() else "",
        "apply_supported": False,
    }


def runtime_manifest_sha256(source_root: Path = SOURCE_ROOT) -> str:
    root = Path(source_root).resolve()
    digest = hashlib.sha256()
    candidates = [
        *sorted((root / "jarvis").rglob("*.py")),
        *sorted((root / "tests").rglob("*.py")),
        root / "pyproject.toml",
        root / "CONSTITUTION.md",
    ]
    for path in candidates:
        if not path.is_file():
            continue
        relative = str(path.relative_to(root)).replace("\\", "/")
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def run_recovery_test(config: Config, memory: Memory) -> dict[str, Any]:
    """Attest restart, lease, approval, and SQLite-backup recovery without live mutations."""
    checks: dict[str, bool] = {}
    with tempfile.TemporaryDirectory(prefix="jarvis-recovery-") as temporary:
        temporary_root = Path(temporary)
        recovery_db = temporary_root / "recovery.db"
        with Memory(recovery_db) as first:
            checks["schema_current"] = (
                int(first.db.execute("PRAGMA user_version").fetchone()[0])
                == SCHEMA_VERSION
            )
            first.set_control_state("stopped", "recovery canary")
            checks["stop_persisted"] = first.control_state()["state"] == "stopped"
            first.set_control_state("running")
            roster = first.list_specialist_agents()
            checks["specialist_roster_seeded"] = {
                str(item["agent_key"]) for item in roster
            } == {"coding", "research", "cybersecurity", "network", "operations"}
            task_id = first.delegate_specialist_task(
                "Fix and verify the recovery lease canary.",
                specialist_key="coding",
                project_id=1,
                max_attempts=3,
            )
            base = datetime.now(timezone.utc) + timedelta(milliseconds=10)
            claimed = first.claim_task("recovery-worker", lease_seconds=5, now=base)
            checks["task_claimed"] = claimed is not None and claimed["id"] == task_id
            active_specialist = first.get_specialist_agent("coding")
            checks["specialist_claim_bound"] = (
                active_specialist is not None
                and active_specialist["status"] == "working"
                and active_specialist["active_task_id"] == task_id
            )
            first.authorize_or_request(
                "publish_external", "recovery-canary-resource", "Recovery canary.",
                approval_scope=f"task:{task_id}", task_id=task_id,
            )
        with Memory(recovery_db) as reopened:
            checks["restart_opened"] = reopened.db.execute("PRAGMA quick_check").fetchone()[0] == "ok"
            persisted_specialist = reopened.get_specialist_agent("coding")
            checks["specialist_assignment_survived_restart"] = (
                persisted_specialist is not None
                and persisted_specialist["status"] == "working"
                and persisted_specialist["active_task_id"] == task_id
            )
            recovered = reopened.recover_stale_tasks(now=base + timedelta(seconds=6))
            checks["expired_lease_requeued"] = recovered == {"requeued": 1, "failed": 0}
            recovered_specialist = reopened.get_specialist_agent("coding")
            checks["specialist_lease_recovered"] = (
                recovered_specialist is not None
                and recovered_specialist["status"] == "ready"
                and recovered_specialist["active_task_id"] is None
            )
            pending = next(
                item for item in reopened.list_approvals()
                if item["status"] == "pending"
            )
            checks["approval_survived_restart"] = pending["task_id"] == task_id
            checks["approval_decision_persisted"] = reopened.decide_approval(
                int(pending["id"]), True
            )
            claimed_again = reopened.claim_task(
                "recovery-worker", now=base + timedelta(seconds=6)
            )
            checks["approved_task_resumed"] = (
                claimed_again is not None and claimed_again["id"] == task_id
            )
            allowed, consumed_id = reopened.authorize_or_request(
                "publish_external", "recovery-canary-resource", "Recovery canary.",
                approval_scope=f"task:{task_id}", task_id=task_id,
            )
            checks["approval_consumed_once"] = allowed and consumed_id == pending["id"]
            allowed_again, _ = reopened.authorize_or_request(
                "publish_external", "recovery-canary-resource", "Recovery canary.",
                approval_scope=f"task:{task_id}", task_id=task_id,
            )
            checks["second_effect_blocked"] = not allowed_again
        backup_path = temporary_root / "live-backup.db"
        import sqlite3
        backup = sqlite3.connect(backup_path)
        try:
            memory.db.backup(backup)
            checks["live_backup_quick_check"] = backup.execute(
                "PRAGMA quick_check"
            ).fetchone()[0] == "ok"
        finally:
            backup.close()
    runtime_hash = runtime_manifest_sha256()
    passed = all(checks.values())
    attestation_id = memory.record_recovery_attestation(
        runtime_sha256=runtime_hash,
        passed=passed,
        evidence={
            "checks": checks,
            "isolated": True,
            "live_operational_state_mutated": False,
        },
    )
    return {
        "attestation_id": attestation_id,
        "passed": passed,
        "runtime_sha256": runtime_hash,
        "checks": checks,
    }
