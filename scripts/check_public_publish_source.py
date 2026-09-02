#!/usr/bin/env python3
"""Fail-closed guard for a disposable, public-only release clone.

This script never pushes or changes Git state. It verifies that the repository
contains only the exact public branch and release tag intended for publication.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from jarvis.trusted_executables import trusted_path_executable


class PublishSourceError(RuntimeError):
    """Raised when a repository is unsafe to use as a public push source."""


FORBIDDEN_PUSH_OPTIONS = frozenset({"--all", "--tags", "--mirror"})
PUBLISH_MODES = frozenset({"candidate", "tag"})
MAX_VERSION_TAG_LENGTH = 64
_PUSH_SAFETY_ARGUMENTS = (
    "--no-verify",
    "--no-follow-tags",
    "--no-push-option",
    "--recurse-submodules=no",
    "--signed=false",
)
_PUSH_CONFIG_ARGUMENTS = (
    "-c",
    "core.fsmonitor=false",
    "-c",
    "push.followTags=false",
    "-c",
    "push.pushOption=",
    "-c",
    "push.recurseSubmodules=no",
)
_GIT_INSPECTION_ARGUMENTS = (
    "-c",
    "core.fsmonitor=false",
    "-c",
    "credential.helper=",
    "-c",
    "core.askPass=",
    "-c",
    "http.extraHeader=",
    "-c",
    "push.followTags=false",
    "-c",
    "push.pushOption=",
    "-c",
    "push.recurseSubmodules=no",
)
_DANGEROUS_ENVIRONMENT_NAMES = frozenset(
    {
        "ALL_PROXY",
        "CURL_CA_BUNDLE",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_ASKPASS",
        "GIT_COMMON_DIR",
        "GIT_CONFIG_PARAMETERS",
        "GIT_DIR",
        "GIT_EXEC_PATH",
        "GIT_GRAFT_FILE",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_PROXY_COMMAND",
        "GIT_REPLACE_REF_BASE",
        "GIT_SHALLOW_FILE",
        "GIT_SSH",
        "GIT_SSH_COMMAND",
        "GIT_SSL_NO_VERIFY",
        "GIT_WORK_TREE",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "SSH_ASKPASS",
    }
)
_ALLOWED_LOCAL_CONFIG_KEYS = frozenset(
    {
        "core.bare",
        "core.filemode",
        "core.ignorecase",
        "core.logallrefupdates",
        "core.repositoryformatversion",
        "core.symlinks",
        "branch.main.merge",
        "branch.main.remote",
        "lfs.repositoryformatversion",
        "remote.public.fetch",
        "remote.public.url",
        "user.email",
        "user.name",
        "user.useconfigonly",
    }
)
_SEMVER_IDENTIFIER = r"(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*)"
_VERSION_TAG_RE = re.compile(
    rf"v(?:0|[1-9][0-9]*)\."
    rf"(?:0|[1-9][0-9]*)\."
    rf"(?:0|[1-9][0-9]*)"
    rf"(?:-{_SEMVER_IDENTIFIER}(?:\.{_SEMVER_IDENTIFIER})*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?\Z"
)


def _validated_version_tag(value: str) -> str:
    """Return one bounded ASCII SemVer tag safe for unquoted shell display."""

    if (
        not isinstance(value, str)
        or len(value) > MAX_VERSION_TAG_LENGTH
        or _VERSION_TAG_RE.fullmatch(value) is None
    ):
        raise PublishSourceError(
            "version tag must be a shell-safe ASCII vMAJOR.MINOR.PATCH release tag"
        )
    return value


def _resolve_trusted_git(repository: Path) -> Path:
    executable = trusted_path_executable(
        "git.exe" if sys.platform == "win32" else "git",
        prohibited_roots=(repository,),
    )
    if executable is None:
        raise PublishSourceError(
            "a trusted OS-administered Git executable is unavailable"
        )
    return executable


def _sanitized_git_environment() -> dict[str, str]:
    """Build one non-interactive Git environment after rejecting overrides."""

    for name in os.environ:
        normalized = name.upper()
        if (
            normalized in _DANGEROUS_ENVIRONMENT_NAMES
            or normalized.startswith("GIT_CONFIG_")
        ):
            raise PublishSourceError(
                "Git execution environment contains a prohibited override"
            )
    environment = {
        name: value
        for name, value in os.environ.items()
        if not name.upper().startswith("GIT_")
        and name.upper() not in _DANGEROUS_ENVIRONMENT_NAMES
    }
    environment["GIT_TERMINAL_PROMPT"] = "0"
    environment["GCM_INTERACTIVE"] = "Never"
    return environment


def _git(
    git_executable: Path,
    repository: Path,
    *arguments: str,
    environment: dict[str, str] | None = None,
    allow_missing: bool = False,
) -> str:
    if environment is None:
        environment = _sanitized_git_environment()
    completed = subprocess.run(
        [
            str(git_executable),
            *_GIT_INSPECTION_ARGUMENTS,
            "-C",
            str(repository),
            *arguments,
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    if completed.returncode != 0:
        if allow_missing and completed.returncode == 1:
            return ""
        detail = completed.stderr.strip() or completed.stdout.strip() or "git failed"
        raise PublishSourceError(detail)
    return completed.stdout.strip()


def _check_local_config(
    git_executable: Path,
    repository: Path,
    environment: dict[str, str],
) -> None:
    """Reject repository-controlled Git behavior before worktree inspection."""

    raw = _git(
        git_executable,
        repository,
        "config",
        "--local",
        "--no-includes",
        "--null",
        "--name-only",
        "--list",
        environment=environment,
    )
    keys = [key.casefold() for key in raw.split("\0") if key]
    if len(keys) != len(set(keys)):
        raise PublishSourceError("duplicate local Git configuration is forbidden")
    if any(key not in _ALLOWED_LOCAL_CONFIG_KEYS for key in keys):
        raise PublishSourceError("non-allowlisted local Git configuration is forbidden")


def _normalized_https_github_url(value: str) -> str:
    candidate = value.strip().rstrip("/")
    parsed = urlsplit(candidate)
    if (
        parsed.scheme.casefold() != "https"
        or (parsed.hostname or "").casefold() != "github.com"
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise PublishSourceError(
            "the public remote must be a credential-free https://github.com URL"
        )
    path_parts = [part for part in parsed.path.split("/") if part]
    if len(path_parts) != 2:
        raise PublishSourceError("the public remote must identify one GitHub repository")
    repository_name = path_parts[1]
    if repository_name.casefold().endswith(".git"):
        repository_name = repository_name[:-4]
    if not path_parts[0] or not repository_name:
        raise PublishSourceError("the public remote owner and repository are required")
    return f"https://github.com/{path_parts[0]}/{repository_name}.git".casefold()


def expected_push_arguments(
    mode: str,
    version_tag: str,
    expected_remote_main: str | None = None,
    expected_commit: str | None = None,
    remote_url: str | None = None,
) -> list[str]:
    """Return the only accepted exact-ref push arguments for one publish phase."""

    if mode not in PUBLISH_MODES:
        raise PublishSourceError("publish mode must be candidate or tag")
    if expected_remote_main is not None:
        raise PublishSourceError("history replacement is not a supported publish mode")
    version_tag = _validated_version_tag(version_tag)
    safe_commit = _validated_commit_id(expected_commit, "expected commit")
    destination = _normalized_https_github_url(str(remote_url or ""))
    if mode == "candidate":
        return [
            *_PUSH_SAFETY_ARGUMENTS,
            f"--force-with-lease=refs/heads/release/{version_tag}:",
            destination,
            f"{safe_commit}:refs/heads/release/{version_tag}",
        ]
    if mode == "tag":
        return [
            *_PUSH_SAFETY_ARGUMENTS,
            f"--force-with-lease=refs/tags/{version_tag}:",
            destination,
            f"{safe_commit}:refs/tags/{version_tag}",
        ]
    raise AssertionError("validated publish mode was not handled")


def validate_push_arguments(
    arguments: list[str],
    mode: str,
    version_tag: str,
    expected_remote_main: str | None = None,
    expected_commit: str | None = None,
    remote_url: str | None = None,
) -> None:
    """Reject broad push modes and anything except the phase's explicit ref."""

    broad_options = {
        argument.split("=", 1)[0]
        for argument in arguments
        if argument.startswith("--")
    }
    rejected = sorted(broad_options & FORBIDDEN_PUSH_OPTIONS)
    if rejected:
        raise PublishSourceError(
            f"broad push option is forbidden: {', '.join(rejected)}"
        )
    expected = expected_push_arguments(
        mode,
        version_tag,
        expected_remote_main,
        expected_commit,
        remote_url,
    )
    if arguments != expected:
        raise PublishSourceError(
            "push arguments must name only the immutable approved commit and intended "
            "new candidate branch or release tag"
        )


def _validated_commit_id(value: str | None, label: str) -> str:
    candidate = str(value or "").strip().casefold()
    if len(candidate) != 40 or any(
        character not in "0123456789abcdef" for character in candidate
    ):
        raise PublishSourceError(f"{label} must be a full 40-character SHA-1")
    return candidate


def check_public_publish_source(
    repository: Path,
    *,
    expected_commit: str,
    expected_root: str,
    mode: str,
    version_tag: str,
    remote_url: str,
    expected_remote_main: str | None = None,
) -> Path:
    """Validate a disposable clone before an explicit-ref public push."""

    repository = repository.resolve()
    if mode not in PUBLISH_MODES:
        raise PublishSourceError("publish mode must be candidate or tag")
    if expected_remote_main is not None:
        raise PublishSourceError("history replacement is not a supported publish mode")
    version_tag = _validated_version_tag(version_tag)
    environment = _sanitized_git_environment()
    git_executable = _resolve_trusted_git(repository)
    completed = subprocess.run(
        [
            str(git_executable),
            *_GIT_INSPECTION_ARGUMENTS,
            "check-ref-format",
            f"refs/tags/{version_tag}",
        ],
        check=False,
        capture_output=True,
        env=environment,
    )
    if completed.returncode != 0:
        raise PublishSourceError("version tag is not a valid Git ref name")

    expected_commit = _validated_commit_id(expected_commit, "expected commit")
    expected_root = _validated_commit_id(expected_root, "expected root")
    _check_local_config(git_executable, repository, environment)

    inside = _git(git_executable, repository, "rev-parse", "--is-inside-work-tree")
    if inside != "true":
        raise PublishSourceError("repository is not a Git working tree")
    if _git(
        git_executable, repository, "status", "--porcelain=v1", "--untracked-files=all"
    ):
        raise PublishSourceError("public publishing source must have a clean worktree")

    head = _git(git_executable, repository, "rev-parse", "HEAD").casefold()
    if head != expected_commit:
        raise PublishSourceError("HEAD does not match the approved release commit")
    if _git(git_executable, repository, "branch", "--show-current") != "main":
        raise PublishSourceError("the only local branch must be main")
    roots = list(
        filter(
            None,
            _git(
                git_executable, repository, "rev-list", "--max-parents=0", "HEAD"
            ).splitlines(),
        )
    )
    if [root.casefold() for root in roots] != [expected_root]:
        raise PublishSourceError(
            "public history must descend from the single approved sanitized root"
        )
    if _git(git_executable, repository, "rev-list", "--merges", "HEAD"):
        raise PublishSourceError("public release history must remain linear")

    expected_refs = {"refs/heads/main"}
    if mode == "tag":
        expected_refs.add(f"refs/tags/{version_tag}")
    actual_refs = set(
        filter(
            None,
            _git(
                git_executable, repository, "for-each-ref", "--format=%(refname)"
            ).splitlines(),
        )
    )
    if actual_refs != expected_refs:
        extra = sorted(actual_refs - expected_refs)
        missing = sorted(expected_refs - actual_refs)
        detail = []
        if extra:
            detail.append(f"extra refs: {', '.join(extra)}")
        if missing:
            detail.append(f"missing refs: {', '.join(missing)}")
        raise PublishSourceError("; ".join(detail))

    if mode == "tag":
        tag_target = _git(
            git_executable, repository, "rev-parse", f"{version_tag}^{{commit}}"
        ).casefold()
        if tag_target != expected_commit:
            raise PublishSourceError("the intended version tag does not point to HEAD")
        if _git(git_executable, repository, "cat-file", "-t", version_tag) != "commit":
            raise PublishSourceError(
                "use a lightweight release tag so no unreviewed tag identity is published"
            )

    remotes = list(filter(None, _git(git_executable, repository, "remote").splitlines()))
    if remotes != ["public"]:
        raise PublishSourceError("the disposable clone must have exactly one remote: public")

    expected_remote = _normalized_https_github_url(remote_url)
    fetch_urls = list(
        filter(
            None,
            _git(
                git_executable,
                repository,
                "remote",
                "get-url",
                "--all",
                "public",
            ).splitlines(),
        )
    )
    push_urls = list(
        filter(
            None,
            _git(
                git_executable,
                repository,
                "remote",
                "get-url",
                "--push",
                "--all",
                "public",
            ).splitlines(),
        )
    )
    if len(fetch_urls) != 1 or len(push_urls) != 1:
        raise PublishSourceError("public must have exactly one fetch URL and one push URL")
    if _normalized_https_github_url(fetch_urls[0]) != expected_remote:
        raise PublishSourceError("public fetch URL does not match the approved repository")
    if _normalized_https_github_url(push_urls[0]) != expected_remote:
        raise PublishSourceError("public push URL does not match the approved repository")

    if _git(
        git_executable,
        repository,
        "config",
        "--get",
        "remote.public.mirror",
        allow_missing=True,
    ):
        raise PublishSourceError("mirror remotes are forbidden for public publishing")
    if _git(
        git_executable,
        repository,
        "config",
        "--get-all",
        "remote.public.push",
        allow_missing=True,
    ):
        raise PublishSourceError("configured push refspecs are forbidden; use explicit refs")

    if _git(
        git_executable,
        repository,
        "rev-parse",
        "--is-shallow-repository",
    ) != "false":
        raise PublishSourceError("shallow repositories are forbidden for public publishing")
    if _git(git_executable, repository, "replace", "-l"):
        raise PublishSourceError("Git replace refs are forbidden for public publishing")
    partial = subprocess.run(
        [
            str(git_executable),
            *_GIT_INSPECTION_ARGUMENTS,
            "-C",
            str(repository),
            "config",
            "--get-regexp",
            r"^(extensions\.partialclone|remote\..*\.promisor)$",
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    if partial.returncode not in {0, 1}:
        raise PublishSourceError("could not verify clone completeness")
    if partial.stdout.strip():
        raise PublishSourceError("partial clones are forbidden for public publishing")

    git_dir = Path(
        _git(git_executable, repository, "rev-parse", "--absolute-git-dir")
    ).resolve()
    common_dir_text = _git(
        git_executable, repository, "rev-parse", "--git-common-dir"
    )
    common_dir = Path(common_dir_text)
    if not common_dir.is_absolute():
        common_dir = repository / common_dir
    common_dir = common_dir.resolve()
    expected_git_dir = (repository / ".git").resolve()
    if (
        git_dir != common_dir
        or git_dir != expected_git_dir
        or not expected_git_dir.is_dir()
    ):
        raise PublishSourceError(
            "public publishing requires a standalone disposable clone"
        )

    graft_path = Path(
        _git(git_executable, repository, "rev-parse", "--git-path", "info/grafts")
    )
    if not graft_path.is_absolute():
        graft_path = repository / graft_path
    if graft_path.exists():
        raise PublishSourceError("Git grafts are forbidden for public publishing")
    alternates_path = Path(
        _git(
            git_executable,
            repository,
            "rev-parse",
            "--git-path",
            "objects/info/alternates",
        )
    )
    if not alternates_path.is_absolute():
        alternates_path = repository / alternates_path
    if alternates_path.exists():
        raise PublishSourceError("alternate object databases are forbidden")
    unreachable = _git(
        git_executable, repository, "fsck", "--full", "--no-reflogs", "--unreachable"
    )
    if unreachable:
        raise PublishSourceError("the disposable clone contains unreachable Git objects")

    validate_push_arguments(
        expected_push_arguments(
            mode,
            version_tag,
            expected_remote_main,
            expected_commit,
            remote_url,
        ),
        mode,
        version_tag,
        expected_remote_main,
        expected_commit,
        remote_url,
    )
    return git_executable


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, default=Path.cwd())
    parser.add_argument("--expected-commit", required=True)
    parser.add_argument("--expected-root", required=True)
    parser.add_argument("--mode", choices=sorted(PUBLISH_MODES), required=True)
    parser.add_argument("--version-tag", required=True)
    parser.add_argument("--remote-url", required=True)
    parser.add_argument("--expected-remote-main")
    parser.add_argument(
        "--execute-push",
        action="store_true",
        help="execute the exact validated creation push instead of only printing it",
    )
    return parser


def main() -> int:
    arguments = _parser().parse_args()
    try:
        git_executable = check_public_publish_source(
            arguments.repository,
            expected_commit=arguments.expected_commit,
            expected_root=arguments.expected_root,
            mode=arguments.mode,
            version_tag=arguments.version_tag,
            remote_url=arguments.remote_url,
            expected_remote_main=arguments.expected_remote_main,
        )
        push_arguments = expected_push_arguments(
            arguments.mode,
            arguments.version_tag,
            arguments.expected_remote_main,
            arguments.expected_commit,
            arguments.remote_url,
        )
    except PublishSourceError as error:
        print(f"PUBLIC PUBLISH SOURCE REJECTED: {error}", file=sys.stderr)
        return 1
    if getattr(arguments, "execute_push", False):
        environment = _sanitized_git_environment()
        hook_target = "NUL" if sys.platform == "win32" else "/dev/null"
        completed = subprocess.run(
            [
                str(git_executable),
                *_PUSH_CONFIG_ARGUMENTS,
                "-c",
                f"core.hooksPath={hook_target}",
                "-C",
                str(arguments.repository.resolve()),
                "push",
                *push_arguments,
            ],
            check=False,
            env=environment,
        )
        if completed.returncode != 0:
            print("PUBLIC PUBLISH PUSH FAILED", file=sys.stderr)
            return 1
        print("PUBLIC PUBLISH PUSH COMPLETED")
    else:
        print("PUBLIC PUBLISH SOURCE PASSED")
        print(f"Reviewed exact-ref arguments: {' '.join(push_arguments)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
