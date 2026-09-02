from __future__ import annotations

import io
import importlib.util
import os
import shutil
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "check_public_publish_source.py"
SPEC = importlib.util.spec_from_file_location("check_public_publish_source", SCRIPT_PATH)
assert SPEC and SPEC.loader
PUBLISH = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PUBLISH)


class PublicPublishSourceTests(unittest.TestCase):
    PUBLIC_URL = "https://github.com/example/jarvis-local-public.git"

    def git(self, repository: Path, *arguments: str) -> str:
        completed = subprocess.run(
            ["git", "-C", str(repository), *arguments],
            check=True,
            capture_output=True,
            text=True,
        )
        return completed.stdout.strip()

    def safe_repository(self, root: Path, *, with_tag: bool = True) -> tuple[Path, str]:
        repository = root / "public-clone"
        repository.mkdir()
        self.git(repository, "init", "-b", "main")
        self.git(repository, "config", "user.name", "Public Release Test")
        self.git(repository, "config", "user.email", "release" + "@" + "example.com")
        (repository / "release.txt").write_text("reviewed\n", encoding="utf-8")
        self.git(repository, "add", "release.txt")
        self.git(repository, "commit", "-m", "public snapshot")
        head = self.git(repository, "rev-parse", "HEAD")
        if with_tag:
            self.git(repository, "tag", "v0.6.0", head)
        self.git(repository, "remote", "add", "public", self.PUBLIC_URL)
        return repository, head

    def check(
        self,
        repository: Path,
        head: str,
        *,
        root: str | None = None,
        mode: str = "tag",
        expected_remote_main: str | None = None,
    ) -> None:
        PUBLISH.check_public_publish_source(
            repository,
            expected_commit=head,
            expected_root=root or head,
            mode=mode,
            version_tag="v0.6.0",
            remote_url=self.PUBLIC_URL,
            expected_remote_main=expected_remote_main,
        )

    def test_accepts_exact_public_only_repository(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository, head = self.safe_repository(Path(temporary))
            self.check(repository, head)

    def test_accepts_candidate_without_a_release_tag(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository, head = self.safe_repository(Path(temporary), with_tag=False)
            self.check(repository, head, mode="candidate")

    def test_poisoned_repository_path_git_is_rejected_without_execution(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository, head = self.safe_repository(Path(temporary))
            poison = repository / ("git.exe" if os.name == "nt" else "git")
            poison.write_bytes(b"not a trusted executable")
            if os.name != "nt":
                poison.chmod(0o755)
            with (
                mock.patch.dict(os.environ, {"PATH": str(repository)}, clear=False),
                mock.patch.object(PUBLISH.subprocess, "run") as run,
                self.assertRaisesRegex(
                    PUBLISH.PublishSourceError, "trusted OS-administered Git"
                ),
            ):
                self.check(repository, head)
            run.assert_not_called()

    def test_all_publish_inspection_uses_one_absolute_trusted_git(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository, head = self.safe_repository(Path(temporary))
            trusted_git = Path(
                shutil.which("git") or shutil.which("git.exe") or ""
            ).resolve()
            real_run = subprocess.run
            with (
                mock.patch.object(
                    PUBLISH, "_resolve_trusted_git", return_value=trusted_git
                ),
                mock.patch.object(PUBLISH.subprocess, "run", wraps=real_run) as run,
            ):
                self.check(repository, head)
            self.assertTrue(run.call_args_list)
            for call in run.call_args_list:
                command = call.args[0]
                self.assertEqual(Path(command[0]), trusted_git)
                self.assertTrue(Path(command[0]).is_absolute())

    def test_rejects_extra_local_ref(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository, head = self.safe_repository(Path(temporary))
            self.git(repository, "branch", "private-history")
            with self.assertRaisesRegex(PUBLISH.PublishSourceError, "extra refs"):
                self.check(repository, head)

    def test_rejects_remote_tracking_ref(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository, head = self.safe_repository(Path(temporary))
            self.git(repository, "update-ref", "refs/remotes/public/old", head)
            with self.assertRaisesRegex(PUBLISH.PublishSourceError, "extra refs"):
                self.check(repository, head)

    def test_rejects_source_or_extra_remote(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository, head = self.safe_repository(Path(temporary))
            self.git(repository, "remote", "add", "source", "https://github.com/example/private.git")
            with self.assertRaisesRegex(
                PUBLISH.PublishSourceError,
                "non-allowlisted|exactly one remote",
            ):
                self.check(repository, head)

    def test_rejects_wrong_public_destination(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository, head = self.safe_repository(Path(temporary))
            self.git(
                repository,
                "remote",
                "set-url",
                "public",
                "https://github.com/example/different-public-repository.git",
            )
            with self.assertRaisesRegex(PUBLISH.PublishSourceError, "does not match"):
                self.check(repository, head)

    def test_rejects_mirror_remote_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository, head = self.safe_repository(Path(temporary))
            self.git(repository, "config", "remote.public.mirror", "true")
            with self.assertRaisesRegex(
                PUBLISH.PublishSourceError,
                "non-allowlisted|mirror remotes",
            ):
                self.check(repository, head)

    def test_rejects_unreachable_objects(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository, head = self.safe_repository(Path(temporary))
            subprocess.run(
                ["git", "-C", str(repository), "hash-object", "-w", "--stdin"],
                input="unreviewed private object\n",
                check=True,
                capture_output=True,
                text=True,
            )
            with self.assertRaisesRegex(PUBLISH.PublishSourceError, "unreachable Git objects"):
                self.check(repository, head)

    def test_rejects_shallow_partial_or_grafted_repository(self) -> None:
        cases = ("shallow", "partial", "graft")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                repository, head = self.safe_repository(Path(temporary))
                git_dir = Path(self.git(repository, "rev-parse", "--absolute-git-dir"))
                if case == "shallow":
                    (git_dir / "shallow").write_text(head + "\n", encoding="ascii")
                    expected = "shallow"
                elif case == "partial":
                    self.git(repository, "config", "extensions.partialClone", "public")
                    expected = "partial|non-allowlisted"
                else:
                    (git_dir / "info" / "grafts").write_text(
                        head + "\n", encoding="ascii"
                    )
                    expected = "grafts"
                with self.assertRaisesRegex(PUBLISH.PublishSourceError, expected):
                    self.check(repository, head)

    def test_rejects_git_topology_environment_override(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository, head = self.safe_repository(Path(temporary))
            with (
                mock.patch.dict(
                    os.environ,
                    {"GIT_REPLACE_REF_BASE": "refs/private-replacements/"},
                    clear=False,
                ),
                self.assertRaisesRegex(
                    PUBLISH.PublishSourceError,
                    "prohibited override",
                ),
            ):
                self.check(repository, head)

    def test_rejects_linked_worktree_as_publish_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository, head = self.safe_repository(root)
            self.git(repository, "branch", "-m", "source")
            self.git(repository, "switch", "--detach")
            linked = root / "linked-public"
            self.git(repository, "worktree", "add", "-b", "main", str(linked), head)
            self.git(repository, "branch", "-D", "source")

            with self.assertRaisesRegex(
                PUBLISH.PublishSourceError,
                "standalone disposable clone",
            ):
                self.check(linked, head)

    def test_accepts_linear_update_history_rooted_at_approved_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository, head = self.safe_repository(Path(temporary))
            self.git(repository, "tag", "-d", "v0.6.0")
            (repository / "second.txt").write_text("history\n", encoding="utf-8")
            self.git(repository, "add", "second.txt")
            self.git(repository, "commit", "-m", "second commit")
            second = self.git(repository, "rev-parse", "HEAD")
            self.git(repository, "tag", "v0.6.0", second)
            self.check(repository, second, root=head)
            self.assertNotEqual(head, second)

    def test_rejects_history_with_an_unapproved_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository, head = self.safe_repository(Path(temporary))
            wrong_root = "0" * 40
            with self.assertRaisesRegex(PUBLISH.PublishSourceError, "sanitized root"):
                self.check(repository, head, root=wrong_root)

    def test_rejects_merge_history(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository, root = self.safe_repository(Path(temporary))
            self.git(repository, "tag", "-d", "v0.6.0")
            self.git(repository, "switch", "-c", "side")
            (repository / "side.txt").write_text("side\n", encoding="utf-8")
            self.git(repository, "add", "side.txt")
            self.git(repository, "commit", "-m", "side")
            self.git(repository, "switch", "main")
            (repository / "main.txt").write_text("main\n", encoding="utf-8")
            self.git(repository, "add", "main.txt")
            self.git(repository, "commit", "-m", "main")
            self.git(repository, "merge", "--no-ff", "side", "-m", "merge")
            head = self.git(repository, "rev-parse", "HEAD")
            self.git(repository, "branch", "-D", "side")
            self.git(repository, "tag", "v0.6.0", head)
            with self.assertRaisesRegex(PUBLISH.PublishSourceError, "linear"):
                self.check(repository, head, root=root)

    def test_rejects_broad_or_implicit_push_arguments(self) -> None:
        approved_commit = "c" * 40
        for option in ("--all", "--tags", "--mirror", "--mirror=true"):
            with self.subTest(option=option):
                with self.assertRaisesRegex(PUBLISH.PublishSourceError, "forbidden"):
                    PUBLISH.validate_push_arguments(
                        ["public", option], "candidate", "v0.6.0", None,
                        approved_commit, self.PUBLIC_URL,
                    )
        with self.assertRaisesRegex(PUBLISH.PublishSourceError, "must name only"):
            PUBLISH.validate_push_arguments(
                ["public", "main"], "candidate", "v0.6.0", None,
                approved_commit, self.PUBLIC_URL,
            )

    def test_exact_push_arguments_are_phase_specific(self) -> None:
        approved_commit = "c" * 40
        safety = [
            "--no-verify",
            "--no-follow-tags",
            "--no-push-option",
            "--recurse-submodules=no",
            "--signed=false",
        ]
        self.assertEqual(
            PUBLISH.expected_push_arguments(
                "candidate", "v0.6.0", None, approved_commit, self.PUBLIC_URL
            ),
            [
                *safety,
                "--force-with-lease=refs/heads/release/v0.6.0:",
                self.PUBLIC_URL,
                f"{approved_commit}:refs/heads/release/v0.6.0",
            ],
        )
        self.assertEqual(
            PUBLISH.expected_push_arguments(
                "tag", "v0.6.0", None, approved_commit, self.PUBLIC_URL
            ),
            [
                *safety,
                "--force-with-lease=refs/tags/v0.6.0:",
                self.PUBLIC_URL,
                f"{approved_commit}:refs/tags/v0.6.0",
            ],
        )
        for removed_mode in ("promotion", "history-replacement"):
            with self.subTest(mode=removed_mode), self.assertRaisesRegex(
                PUBLISH.PublishSourceError, "candidate or tag"
            ):
                PUBLISH.expected_push_arguments(
                    removed_mode, "v0.6.0", None, approved_commit, self.PUBLIC_URL
                )

    def test_release_tag_grammar_accepts_bounded_ascii_semver(self) -> None:
        accepted = (
            "v0.0.0",
            "v0.6.3",
            "v1.2.3-rc.1",
            "v2.3.4-phase6-baseline",
            "v10.20.30+build.5",
            "v1.2.3-alpha.1+build-7.sha",
        )
        for version_tag in accepted:
            with self.subTest(version_tag=version_tag):
                self.assertEqual(
                    PUBLISH.expected_push_arguments(
                        "candidate", version_tag, None, "b" * 40, self.PUBLIC_URL
                    )[-1],
                    f"{'b' * 40}:refs/heads/release/{version_tag}",
                )

    def test_shell_unsafe_release_tags_are_rejected_for_every_mode(self) -> None:
        unsafe_tags = (
            "v1.2.3;whoami",
            "v1.2.3&whoami",
            "v1.2.3&&whoami",
            "v1.2.3|whoami",
            "v1.2.3||whoami",
            "v1.2.3$(whoami)",
            "v1.2.3${HOME}",
            "v1.2.3`whoami`",
            "v1.2.3>output.txt",
            "v1.2.3<input.txt",
            "v1.2.3^whoami",
            "v1.2.3%COMSPEC%",
            'v1.2.3"command"',
            "v1.2.3'command'",
            "v1.2.3/../../main",
            "v1.2.3 command",
            "v1.2.3\tcommand",
            "v1.2.3\ncommand",
            "v1.2.3\rcommand",
            "v1.2.3；command",
            "v1.2.3💥",
            "V1.2.3",
            "v01.2.3",
            "v1.02.3",
            "v1.2.03",
            "v1.2",
            "v1.2.3.",
            "v" + "1" * PUBLISH.MAX_VERSION_TAG_LENGTH,
        )
        approved_commit = "b" * 40
        for mode in sorted(PUBLISH.PUBLISH_MODES):
            for version_tag in unsafe_tags:
                with self.subTest(mode=mode, version_tag=version_tag):
                    with self.assertRaisesRegex(
                        PUBLISH.PublishSourceError,
                        "shell-safe ASCII",
                    ):
                        PUBLISH.expected_push_arguments(
                            mode,
                            version_tag,
                            None,
                            approved_commit,
                            self.PUBLIC_URL,
                        )

    def test_cli_never_prints_an_unsafe_release_tag_for_any_mode(self) -> None:
        unsafe_tag = "v1.2.3;Write-Output injected"
        for mode in sorted(PUBLISH.PUBLISH_MODES):
            arguments = SimpleNamespace(
                repository=Path("."),
                expected_commit="b" * 40,
                expected_root="c" * 40,
                mode=mode,
                version_tag=unsafe_tag,
                remote_url=self.PUBLIC_URL,
                expected_remote_main=None,
            )
            parser = mock.Mock()
            parser.parse_args.return_value = arguments
            stdout = io.StringIO()
            stderr = io.StringIO()
            with (
                self.subTest(mode=mode),
                mock.patch.object(PUBLISH, "_parser", return_value=parser),
                mock.patch.object(PUBLISH, "check_public_publish_source"),
                redirect_stdout(stdout),
                redirect_stderr(stderr),
            ):
                self.assertEqual(PUBLISH.main(), 1)
            self.assertEqual(stdout.getvalue(), "")
            self.assertNotIn(unsafe_tag, stderr.getvalue())
            self.assertIn("shell-safe ASCII", stderr.getvalue())

    def test_documentation_contains_only_explicit_ref_guidance(self) -> None:
        publishing = (ROOT / "docs" / "PUBLISHING.md").read_text(encoding="utf-8")
        for forbidden in PUBLISH.FORBIDDEN_PUSH_OPTIONS:
            self.assertNotIn(forbidden, publishing)
        self.assertIn("refs/heads/release/v1.2.3", publishing)
        self.assertIn("refs/tags/v1.2.3", publishing)
        self.assertNotIn("release/v0.6.3", publishing)
        self.assertIn("--mode candidate", publishing)
        self.assertIn("--mode tag", publishing)
        self.assertNotIn("HEAD:refs/heads/main", publishing)
        self.assertNotIn("--force-with-lease=refs/heads/main:", publishing)
        self.assertIn("Keep my email addresses private", publishing)
        self.assertIn("squash-only", publishing)
        self.assertIn("six strict contexts", publishing)
        self.assertIn("Squash and merge", publishing)
        self.assertIn("$candidateTree", publishing)
        self.assertIn("$preMergeMain", publishing)
        self.assertIn("$mergedCommit", publishing)
        self.assertIn("The squash commit tree differs", publishing)
        self.assertIn("all six hosted contexts", publishing)
        self.assertIn("Tag only the green squash commit", publishing)
        self.assertNotIn("history-replacement", publishing)
        self.assertNotIn("promotion", publishing.casefold())


if __name__ == "__main__":
    unittest.main()
