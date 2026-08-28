from __future__ import annotations

import io
import importlib.util
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
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
        self.git(repository, "config", "user.email", "release@example.com")
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
    ) -> None:
        PUBLISH.check_public_publish_source(
            repository,
            expected_commit=head,
            expected_root=root or head,
            mode=mode,
            version_tag="v0.6.0",
            remote_url=self.PUBLIC_URL,
        )

    def test_accepts_exact_public_only_repository(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository, head = self.safe_repository(Path(temporary))
            self.check(repository, head)

    def test_accepts_candidate_without_a_release_tag(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository, head = self.safe_repository(Path(temporary), with_tag=False)
            self.check(repository, head, mode="candidate")

    def test_accepts_fast_forward_promotion_of_exact_checked_head(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository, root = self.safe_repository(Path(temporary), with_tag=False)
            (repository / "second.txt").write_text("checked\n", encoding="utf-8")
            self.git(repository, "add", "second.txt")
            self.git(repository, "commit", "-m", "checked candidate")
            head = self.git(repository, "rev-parse", "HEAD")
            original_git = PUBLISH._git

            def fake_git(repo, *arguments, allow_missing=False):
                if arguments == (
                    "ls-remote",
                    "--exit-code",
                    "public",
                    "refs/heads/main",
                ):
                    return f"{root}\trefs/heads/main"
                return original_git(repo, *arguments, allow_missing=allow_missing)

            with mock.patch.object(PUBLISH, "_git", side_effect=fake_git):
                self.check(repository, head, root=root, mode="promotion")

    def test_rejects_non_fast_forward_promotion(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository, head = self.safe_repository(Path(temporary), with_tag=False)
            tree = self.git(repository, "rev-parse", "HEAD^{tree}")
            alternate = subprocess.run(
                ["git", "-C", str(repository), "commit-tree", tree],
                input="unrelated public main\n",
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            original_git = PUBLISH._git

            def fake_git(repo, *arguments, allow_missing=False):
                if arguments == (
                    "ls-remote",
                    "--exit-code",
                    "public",
                    "refs/heads/main",
                ):
                    return f"{alternate}\trefs/heads/main"
                return original_git(repo, *arguments, allow_missing=allow_missing)

            with mock.patch.object(PUBLISH, "_git", side_effect=fake_git):
                with self.assertRaisesRegex(PUBLISH.PublishSourceError, "not an ancestor"):
                    self.check(repository, head, mode="promotion")

    def test_promotion_cli_prints_only_exact_fast_forward_command(self) -> None:
        arguments = SimpleNamespace(
            repository=Path("."),
            expected_commit="a" * 40,
            expected_root="b" * 40,
            mode="promotion",
            version_tag="v0.6.2",
            remote_url=self.PUBLIC_URL,
        )
        parser = mock.Mock()
        parser.parse_args.return_value = arguments
        output = io.StringIO()
        with (
            mock.patch.object(PUBLISH, "_parser", return_value=parser),
            mock.patch.object(PUBLISH, "check_public_publish_source"),
            redirect_stdout(output),
        ):
            self.assertEqual(PUBLISH.main(), 0)
        self.assertEqual(output.getvalue(), "git push public HEAD:refs/heads/main\n")

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
            with self.assertRaisesRegex(PUBLISH.PublishSourceError, "exactly one remote"):
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
            with self.assertRaisesRegex(PUBLISH.PublishSourceError, "mirror remotes"):
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
        for option in ("--all", "--tags", "--mirror", "--mirror=true"):
            with self.subTest(option=option):
                with self.assertRaisesRegex(PUBLISH.PublishSourceError, "forbidden"):
                    PUBLISH.validate_push_arguments(
                        ["public", option], "candidate", "v0.6.0"
                    )
        with self.assertRaisesRegex(PUBLISH.PublishSourceError, "must name only"):
            PUBLISH.validate_push_arguments(
                ["public", "main"], "candidate", "v0.6.0"
            )

    def test_exact_push_arguments_are_phase_specific(self) -> None:
        self.assertEqual(
            PUBLISH.expected_push_arguments("candidate", "v0.6.0"),
            ["public", "HEAD:refs/heads/release/v0.6.0"],
        )
        self.assertEqual(
            PUBLISH.expected_push_arguments("promotion", "v0.6.0"),
            ["public", "HEAD:refs/heads/main"],
        )
        self.assertEqual(
            PUBLISH.expected_push_arguments("tag", "v0.6.0"),
            ["public", "refs/tags/v0.6.0:refs/tags/v0.6.0"],
        )

    def test_documentation_contains_only_explicit_ref_guidance(self) -> None:
        publishing = (ROOT / "docs" / "PUBLISHING.md").read_text(encoding="utf-8")
        for forbidden in PUBLISH.FORBIDDEN_PUSH_OPTIONS:
            self.assertNotIn(forbidden, publishing)
        self.assertIn("HEAD:refs/heads/release/v0.6.2", publishing)
        self.assertIn("HEAD:refs/heads/main", publishing)
        self.assertIn("refs/tags/v0.6.2:refs/tags/v0.6.2", publishing)
        self.assertIn("Do not merge the pull request through", publishing)
        self.assertNotIn("Merge only after", publishing)


if __name__ == "__main__":
    unittest.main()
