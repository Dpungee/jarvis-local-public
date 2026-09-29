"""Git and GitHub steps Hub agents need for Codex-style work, plus the project's Git status.

Run-program policy allows only read-only Git (status, diff, log, show), so a Hub agent could
inspect a repository but never branch, commit or open a pull request. These helpers add those
steps with fixed, validated argument lists (no shell), through ``GitHubProvider``'s locked Git
envelope: repository config is checked against an inert allowlist, hooks, fsmonitor, filters,
credential helpers and system/global config are disabled, and the repository must be inside
the agent's project.

Opening a pull request publishes to GitHub, so the Hub asks the operator for an exact approval
first (see ``AgentRuntime``); the approved request names the repository, both branches, the
title and a digest of the body, and exactly that is what runs.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

MAX_TITLE_CHARS = 256
MAX_BODY_CHARS = 8_000
MAX_COMMIT_MESSAGE_CHARS = 2_000
MAX_COMMIT_PATHS = 200
# Used when the repository has no user.name/user.email of its own. A reserved .example address,
# so a commit is never attributed to a real person's account by accident.
FALLBACK_IDENTITY = ("JARVIS agent", "jarvis-agent@jarvis.example")
WORKSPACE_TTL = 10.0
_REPO_URL = re.compile(r"^https://github\.com/([A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))/"
                       r"([A-Za-z0-9][A-Za-z0-9._-]{0,99}?)(?:\.git)?/?$", re.IGNORECASE)
_PR_URL = re.compile(r"https://github\.com/[A-Za-z0-9-]+/[A-Za-z0-9._-]+/pull/\d+")


class GitStepError(ValueError):
    """A refused or failed Git step, in words the agent can act on."""


def _provider(root: Path, timeout: float = 120.0) -> Any:
    from .github_provider import GitHubProvider

    return GitHubProvider(Path(root), timeout_seconds=timeout)


def _text(value: Any, field: str, limit: int, *, multiline: bool = False) -> str:
    if not isinstance(value, str):
        raise GitStepError(f"{field} must be text.")
    text = value.replace("\r\n", "\n").strip()
    if not text or len(text) > limit:
        raise GitStepError(f"{field} must be 1-{limit} characters.")
    bad = [c for c in text if (ord(c) < 32 and not (multiline and c in "\n\t")) or ord(c) == 127]
    if bad:
        raise GitStepError(f"{field} must not contain control characters.")
    return text


def _branch(value: Any, field: str) -> str:
    from .github_provider import _validate_branch

    try:
        return _validate_branch(value)
    except (TypeError, ValueError):
        raise GitStepError(f"{field} must be one plain branch name such as feature/login-form.") from None


def _repository(provider: Any, path: Any) -> Path:
    try:
        return provider._repository_path(str(path or "."))
    except (TypeError, ValueError, PermissionError, OSError) as exc:
        raise GitStepError(f"repository_path: {exc}") from None


# Project folders can sit deep under the Hub's state folder; Windows' 260-character path
# limit then breaks writes into .git/objects ("Filename too long"). The setting is passed per
# command because the hardened runner refuses it in a repository's own config.
_LONG_PATHS = ["-c", "core.longpaths=true"]


def _run(provider: Any, repository: Path, arguments: list[str]) -> Any:
    result = provider._run("git", [*_LONG_PATHS, *arguments], cwd=repository)
    return result


def _identity(provider: Any, repository: Path) -> list[str]:
    from .github_provider import _locked_git_envelope, _section_key

    with provider._git_lock, _locked_git_envelope(repository, provider.workspace_root) as envelope:
        key = _section_key(envelope.sections, "user")
        user = envelope.sections.get(key, {}) if key else {}
    if user.get("name") and user.get("email"):
        return []
    return ["-c", f"user.name={FALLBACK_IDENTITY[0]}", "-c", f"user.email={FALLBACK_IDENTITY[1]}"]


def _failure(result: Any, what: str) -> GitStepError:
    return GitStepError(f"{what} failed: {(result.error or result.stderr or result.stdout or '').strip()[:400]}")


# ---------------------------------------------------------------------- local steps
def git_init(root: Path, path: Any = ".", branch: Any = "main") -> dict[str, Any]:
    """Create a new repository in a project folder (no network, no hooks run)."""
    from .github_provider import _trusted_provider_executable
    from .subprocess_env import trusted_cli_environment

    base = Path(root).resolve(strict=True)
    name = _branch(branch or "main", "branch")
    target = (base / str(path or ".")).resolve()
    try:
        target.relative_to(base)
    except ValueError:
        raise GitStepError("path must be a folder inside this project.") from None
    if any(part.startswith(".jarvis") or part == ".git" for part in target.relative_to(base).parts):
        raise GitStepError("path must be an ordinary project folder.")
    if (target / ".git").exists():
        raise GitStepError("That folder is already a Git repository.")
    target.mkdir(parents=True, exist_ok=True)
    git = _trusted_provider_executable("git", base)
    if not git:
        raise GitStepError("Git is not installed on the backend host.")
    environment = trusted_cli_environment(include_ssh_agent=False)
    environment = {k: v for k, v in environment.items() if not k.upper().startswith("GIT_")}
    environment.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_TERMINAL_PROMPT": "0"})
    done = subprocess.run([git, *_LONG_PATHS, "init", "--quiet", f"--initial-branch={name}", str(target)],
                          cwd=str(target),
                          env=environment, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                          timeout=30, check=False, shell=False,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if done.returncode != 0:
        raise GitStepError(f"git init failed: {(done.stderr or done.stdout).strip()[:300]}")
    return {"repository_path": target.relative_to(base).as_posix() or ".", "branch": name, "created": True}


def git_branch(root: Path, repository_path: Any, branch: Any, create: bool = True) -> dict[str, Any]:
    """Create and switch to a branch (or switch to an existing one)."""
    if not isinstance(create, bool):
        raise GitStepError("create must be true or false.")
    provider = _provider(root)
    repository = _repository(provider, repository_path)
    name = _branch(branch, "branch")
    result = _run(provider, repository, ["switch", "--no-guess", *(["-c"] if create else []), name])
    if not result.ok:
        raise _failure(result, "git switch")
    return {"repository_path": repository.relative_to(provider.workspace_root).as_posix() or ".",
            "branch": name, "created": create}


def git_commit(root: Path, repository_path: Any, message: Any, paths: Any = None) -> dict[str, Any]:
    """Stage the given paths (or every change) and commit them."""
    provider = _provider(root)
    repository = _repository(provider, repository_path)
    text = _text(message, "message", MAX_COMMIT_MESSAGE_CHARS, multiline=True)
    if paths in (None, []):
        pathspec = ["."]
    else:
        if not isinstance(paths, list) or len(paths) > MAX_COMMIT_PATHS:
            raise GitStepError(f"paths must be a list of up to {MAX_COMMIT_PATHS} project paths.")
        pathspec = []
        for item in paths:
            value = _text(item, "path", 400)
            candidate = (repository / value).resolve()
            try:
                candidate.relative_to(repository)
            except ValueError:
                raise GitStepError(f"{value} is outside the repository.") from None
            if value.startswith((":", "-")):
                raise GitStepError(f"{value} is not a plain path.")
            pathspec.append(value)
    added = _run(provider, repository, ["add", "--all", "--", *pathspec])
    if not added.ok:
        raise _failure(added, "git add")
    staged = _run(provider, repository, ["diff", "--cached", "--name-only"])
    files = [line for line in staged.stdout.splitlines() if line.strip()] if staged.ok else []
    if staged.ok and not files:
        raise GitStepError("Nothing to commit: those paths have no changes.")
    committed = _run(provider, repository, [*_identity(provider, repository), "commit", "--no-verify",
                                            "--quiet", "-m", text])
    if not committed.ok:
        raise _failure(committed, "git commit")
    head = _run(provider, repository, ["rev-parse", "HEAD"])
    branch = _run(provider, repository, ["rev-parse", "--abbrev-ref", "HEAD"])
    return {"repository_path": repository.relative_to(provider.workspace_root).as_posix() or ".",
            "commit": head.stdout.strip()[:64] if head.ok else None,
            "branch": branch.stdout.strip()[:255] if branch.ok else None,
            "files": files[:100], "file_count": len(files)}


# -------------------------------------------------------------------- pull requests
def pull_request_plan(root: Path, arguments: dict[str, Any]) -> dict[str, Any]:
    """Validate a pull-request request and resolve its GitHub repository from the remote.

    The result is exactly what will run and what the operator approves."""
    from .github_provider import _locked_git_envelope, _section_key, _validate_remote

    allowed = {"repository_path", "base", "head", "title", "body", "draft", "remote"}
    if not isinstance(arguments, dict) or set(arguments) - allowed:
        raise GitStepError("github_create_pull_request takes repository_path, base, head, title, body, "
                           "draft and remote.")
    provider = _provider(root)
    repository = _repository(provider, arguments.get("repository_path") or ".")
    try:
        remote = _validate_remote(arguments.get("remote") or "origin")
    except (TypeError, ValueError):
        raise GitStepError("remote must be a plain remote name such as origin.") from None
    base_branch = _branch(arguments.get("base"), "base")
    head_branch = _branch(arguments.get("head"), "head")
    if base_branch == head_branch:
        raise GitStepError("base and head must be different branches.")
    title = " ".join(_text(arguments.get("title"), "title", MAX_TITLE_CHARS).split())
    body_value = arguments.get("body")
    body = "" if body_value in (None, "") else _text(body_value, "body", MAX_BODY_CHARS, multiline=True)
    draft = arguments.get("draft", False)
    if not isinstance(draft, bool):
        raise GitStepError("draft must be true or false.")
    try:
        with provider._git_lock, _locked_git_envelope(repository, provider.workspace_root) as envelope:
            key = _section_key(envelope.sections, f'remote "{remote}"')
            url = (envelope.sections.get(key) or {}).get("url", "") if key else ""
    except (OSError, ValueError, PermissionError) as exc:
        raise GitStepError(f"The repository could not be read safely: {exc}") from None
    match = _REPO_URL.match(url or "")
    if match is None:
        raise GitStepError(f"Remote {remote} is not a plain https://github.com/OWNER/REPO address.")
    return {"repository": f"{match.group(1)}/{match.group(2)}", "repository_path":
            repository.relative_to(provider.workspace_root).as_posix() or ".", "base": base_branch,
            "head": head_branch, "title": title, "body": body, "draft": draft,
            "body_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest()}


def approval_resource(plan: dict[str, Any]) -> dict[str, Any]:
    """What the operator sees and approves (bounded: the body is shown in part, bound by digest)."""
    return {"tool": "github_create_pull_request", "repository": plan["repository"], "base": plan["base"],
            "head": plan["head"], "title": plan["title"], "draft": plan["draft"],
            "body_sha256": plan["body_sha256"], "body_preview": plan["body"][:400]}


def create_pull_request(root: Path, plan: dict[str, Any], *, provider: Any = None) -> dict[str, Any]:
    """Run ``gh pr create`` with the approved plan: every value is its own argument, no shell."""
    provider = provider or _provider(root)
    arguments = ["pr", "create", f"--repo={plan['repository']}", f"--base={plan['base']}",
                 f"--head={plan['head']}", f"--title={plan['title']}", f"--body={plan['body']}"]
    if plan["draft"]:
        arguments.append("--draft")
    result = provider._run("gh", arguments, cwd=provider.workspace_root)
    if not result.ok:
        raise GitStepError(f"GitHub did not open the pull request: "
                           f"{(result.error or result.stderr or '').strip()[:400]}")
    link = _PR_URL.search(result.stdout or "")
    return {"opened": True, "url": link.group(0) if link else None, "repository": plan["repository"],
            "base": plan["base"], "head": plan["head"], "title": plan["title"], "draft": plan["draft"]}


# ---------------------------------------------------------------- workspace status
_workspace_lock = threading.Lock()
_workspace_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_workspace_refreshing: set[str] = set()


def workspace_state(root: Path, *, ttl: float = WORKSPACE_TTL, background: bool = True) -> dict[str, Any]:
    """The project folder's name, current branch and whether it has uncommitted changes.

    Cached per folder for ``ttl`` seconds; once a folder has been read, a stale entry is
    returned at once and refreshed in the background, so a large repository never slows the
    agent view down more than once. A folder that is not a repository, or any Git failure,
    gives null values; this never raises."""
    folder = Path(root)
    key = str(folder)
    with _workspace_lock:
        cached = _workspace_cache.get(key)
        if cached and time.monotonic() - cached[0] < ttl:
            return dict(cached[1])
        refresh_later = bool(cached) and background and key not in _workspace_refreshing
        if refresh_later:
            _workspace_refreshing.add(key)
    if refresh_later:
        def refresh() -> None:
            try:
                _read_workspace(folder, ttl)
            finally:
                with _workspace_lock:
                    _workspace_refreshing.discard(key)

        threading.Thread(target=refresh, name="hub-workspace-status", daemon=True).start()
        return dict(cached[1])
    if cached and background:
        return dict(cached[1])  # a refresh is already running
    return _read_workspace(folder, ttl)


def _read_workspace(folder: Path, ttl: float) -> dict[str, Any]:
    key = str(folder)
    state: dict[str, Any] = {"folder": folder.name, "git_branch": None, "git_dirty": None}
    try:
        if (folder / ".git").is_dir():
            head = (folder / ".git" / "HEAD").read_text(encoding="utf-8", errors="replace").strip()
            if head.startswith("ref: refs/heads/"):
                state["git_branch"] = head[len("ref: refs/heads/"):][:255] or None
            provider = _provider(folder, timeout=3.0)
            status = provider._run("git", ["status", "--porcelain=v1", "--untracked-files=normal",
                                           "--ignore-submodules=all"], cwd=provider.workspace_root)
            if status.ok:
                state["git_dirty"] = bool(status.stdout.strip())
    except Exception:  # noqa: BLE001 - a status read must never break the agent view
        state.update(git_branch=None, git_dirty=None)
    now = time.monotonic()
    with _workspace_lock:
        _workspace_cache[key] = (now, dict(state))
        for stale in [k for k, (at, _) in _workspace_cache.items() if now - at > 10 * ttl]:
            _workspace_cache.pop(stale, None)
    return state
