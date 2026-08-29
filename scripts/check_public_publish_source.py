#!/usr/bin/env python3
"""Fail-closed guard for a disposable, public-only release clone.

This script never pushes or changes Git state. It verifies that the repository
contains only the exact public branch and release tag intended for publication.
"""

from __future__ import annotations

import argparse
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
PUBLISH_MODES = frozenset({"candidate", "promotion", "tag"})


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


def _git(
    git_executable: Path,
    repository: Path,
    *arguments: str,
    allow_missing: bool = False,
) -> str:
    completed = subprocess.run(
        [str(git_executable), "-C", str(repository), *arguments],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        if allow_missing:
            return ""
        detail = completed.stderr.strip() or completed.stdout.strip() or "git failed"
        raise PublishSourceError(detail)
    return completed.stdout.strip()


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


def expected_push_arguments(mode: str, version_tag: str) -> list[str]:
    """Return the only accepted exact-ref push arguments for one publish phase."""

    if mode == "candidate":
        return ["public", f"HEAD:refs/heads/release/{version_tag}"]
    if mode == "promotion":
        return ["public", "HEAD:refs/heads/main"]
    if mode == "tag":
        return ["public", f"refs/tags/{version_tag}:refs/tags/{version_tag}"]
    raise PublishSourceError("publish mode must be candidate, promotion, or tag")


def validate_push_arguments(arguments: list[str], mode: str, version_tag: str) -> None:
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
    expected = expected_push_arguments(mode, version_tag)
    if arguments != expected:
        raise PublishSourceError(
            "push arguments must name only the intended candidate branch, main promotion, "
            "or release tag"
        )


def _check_promotion_is_fast_forward(
    git_executable: Path, repository: Path, head: str
) -> None:
    """Require the live public main tip to be present and ancestral to HEAD."""

    remote_output = _git(
        git_executable,
        repository,
        "ls-remote",
        "--exit-code",
        "public",
        "refs/heads/main",
    )
    lines = [line for line in remote_output.splitlines() if line.strip()]
    fields = lines[0].split() if len(lines) == 1 else []
    if len(fields) != 2 or fields[1] != "refs/heads/main":
        raise PublishSourceError("could not resolve exactly one public main tip")
    remote_main = fields[0].casefold()
    if len(remote_main) != 40 or any(
        character not in "0123456789abcdef" for character in remote_main
    ):
        raise PublishSourceError("public main did not resolve to a full commit SHA-1")
    try:
        _git(
            git_executable, repository, "cat-file", "-e", f"{remote_main}^{{commit}}"
        )
    except PublishSourceError as error:
        raise PublishSourceError(
            "public main is not present in the exact checked local history"
        ) from error

    completed = subprocess.run(
        [
            str(git_executable),
            "-C",
            str(repository),
            "merge-base",
            "--is-ancestor",
            remote_main,
            head,
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode == 1:
        raise PublishSourceError(
            "public main is not an ancestor of the approved release commit"
        )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or "git failed"
        raise PublishSourceError(f"could not verify promotion ancestry: {detail}")


def check_public_publish_source(
    repository: Path,
    *,
    expected_commit: str,
    expected_root: str,
    mode: str,
    version_tag: str,
    remote_url: str,
) -> None:
    """Validate a disposable clone before an explicit-ref public push."""

    repository = repository.resolve()
    git_executable = _resolve_trusted_git(repository)
    if mode not in PUBLISH_MODES:
        raise PublishSourceError("publish mode must be candidate, promotion, or tag")
    if not version_tag.startswith("v") or any(
        character.isspace() for character in version_tag
    ):
        raise PublishSourceError("version tag must be a compact v-prefixed name")
    completed = subprocess.run(
        [str(git_executable), "check-ref-format", f"refs/tags/{version_tag}"],
        check=False,
        capture_output=True,
    )
    if completed.returncode != 0:
        raise PublishSourceError("version tag is not a valid Git ref name")

    expected_commit = expected_commit.strip().casefold()
    if len(expected_commit) != 40 or any(
        character not in "0123456789abcdef" for character in expected_commit
    ):
        raise PublishSourceError("expected commit must be a full 40-character SHA-1")
    expected_root = expected_root.strip().casefold()
    if len(expected_root) != 40 or any(
        character not in "0123456789abcdef" for character in expected_root
    ):
        raise PublishSourceError("expected root must be a full 40-character SHA-1")

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

    if mode == "promotion":
        _check_promotion_is_fast_forward(git_executable, repository, head)

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

    git_dir = Path(
        _git(git_executable, repository, "rev-parse", "--absolute-git-dir")
    )
    if (git_dir / "objects" / "info" / "alternates").exists():
        raise PublishSourceError("alternate object databases are forbidden")
    unreachable = _git(
        git_executable, repository, "fsck", "--full", "--no-reflogs", "--unreachable"
    )
    if unreachable:
        raise PublishSourceError("the disposable clone contains unreachable Git objects")

    validate_push_arguments(expected_push_arguments(mode, version_tag), mode, version_tag)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, default=Path.cwd())
    parser.add_argument("--expected-commit", required=True)
    parser.add_argument("--expected-root", required=True)
    parser.add_argument("--mode", choices=sorted(PUBLISH_MODES), required=True)
    parser.add_argument("--version-tag", required=True)
    parser.add_argument("--remote-url", required=True)
    return parser


def main() -> int:
    arguments = _parser().parse_args()
    try:
        check_public_publish_source(
            arguments.repository,
            expected_commit=arguments.expected_commit,
            expected_root=arguments.expected_root,
            mode=arguments.mode,
            version_tag=arguments.version_tag,
            remote_url=arguments.remote_url,
        )
    except PublishSourceError as error:
        print(f"PUBLIC PUBLISH SOURCE REJECTED: {error}", file=sys.stderr)
        return 1
    push_arguments = " ".join(
        expected_push_arguments(arguments.mode, arguments.version_tag)
    )
    if arguments.mode == "promotion":
        print(f"git push {push_arguments}")
    else:
        print("PUBLIC PUBLISH SOURCE PASSED")
        print(f"Reviewed exact-ref command: git push {push_arguments}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
