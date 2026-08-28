from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from scripts.check_public_release import check_release


class PublicReleaseCheckTests(unittest.TestCase):
    @staticmethod
    def _blocked_email() -> str:
        # Assemble the adversarial fixture at runtime so the release-checker's own
        # source remains publishable without exempting test files from inspection.
        return "maintainer" + "@" + "personal.invalid"

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="jarvis-public-release-check-"
        )
        self.repo = Path(self.temporary.name)
        self._git("init", "--quiet")
        (self.repo / "README.md").write_text("public fixture\n", encoding="utf-8")
        self._git("add", "README.md")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _git(
        self, *arguments: str, environment: dict[str, str] | None = None
    ) -> None:
        process_environment = os.environ.copy()
        if environment:
            process_environment.update(environment)
        completed = subprocess.run(
            ["git", *arguments],
            cwd=self.repo,
            env=process_environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if completed.returncode != 0:
            self.fail(completed.stderr.decode("utf-8", errors="replace"))

    def _commit(
        self,
        author_email: str,
        *,
        committer_email: str | None = None,
        message: str = "fixture",
    ) -> None:
        self._git(
            "commit",
            "--quiet",
            "--allow-empty",
            "-m",
            message,
            environment={
                "GIT_AUTHOR_NAME": "Release Tester",
                "GIT_AUTHOR_EMAIL": author_email,
                "GIT_COMMITTER_NAME": "Release Tester",
                "GIT_COMMITTER_EMAIL": committer_email or author_email,
            },
        )

    def test_github_no_reply_commit_and_tag_identities_are_allowed(self) -> None:
        email = "12345+release-tester@users.noreply.github.com"
        self._commit(email)
        self._git(
            "-c",
            "user.name=Release Tester",
            "-c",
            f"user.email={email}",
            "tag",
            "-a",
            "v1.0.0",
            "-m",
            "fixture tag",
        )
        self.assertEqual(check_release(self.repo), [])

    def test_personal_commit_email_blocks_release(self) -> None:
        blocked_email = self._blocked_email()
        self._commit(blocked_email)
        findings = check_release(self.repo)
        self.assertTrue(
            any(
                "author identity: non-example email address" in finding
                for finding in findings
            ),
            findings,
        )
        self.assertNotIn(blocked_email, "\n".join(findings))

    def test_personal_committer_email_blocks_release(self) -> None:
        self._commit(
            "12345+release-tester@users.noreply.github.com",
            committer_email=self._blocked_email(),
        )
        findings = check_release(self.repo)
        self.assertTrue(
            any(
                "committer identity: non-example email address" in finding
                for finding in findings
            ),
            findings,
        )
        self.assertFalse(
            any(
                "author identity: non-example email address" in finding
                for finding in findings
            ),
            findings,
        )
        self.assertNotIn(self._blocked_email(), "\n".join(findings))

    def test_commit_message_private_data_blocks_release(self) -> None:
        self._commit(
            "12345+release-tester@users.noreply.github.com",
            message="Do not publish " + "\\".join(
                ("C:", "Users", "private-person", "record.txt")
            ),
        )
        findings = check_release(self.repo)
        self.assertTrue(
            any(
                "message: concrete Windows user-home path" in finding
                for finding in findings
            ),
            findings,
        )
        self.assertNotIn("private-person", "\n".join(findings))

    def test_personal_annotated_tag_email_blocks_release(self) -> None:
        self._commit("12345+release-tester@users.noreply.github.com")
        self._git(
            "-c",
            "user.name=Release Tester",
            "-c",
            f"user.email={self._blocked_email()}",
            "tag",
            "-a",
            "v1.0.0",
            "-m",
            "fixture tag",
        )
        findings = check_release(self.repo)
        self.assertTrue(
            any(
                "refs/tags/v1.0.0 tagger identity: non-example email address"
                in finding
                for finding in findings
            ),
            findings,
        )
        self.assertNotIn(self._blocked_email(), "\n".join(findings))

    def test_annotated_tag_message_private_data_blocks_release(self) -> None:
        email = "12345+release-tester@users.noreply.github.com"
        self._commit(email)
        self._git(
            "-c",
            "user.name=Release Tester",
            "-c",
            f"user.email={email}",
            "tag",
            "-a",
            "v1.0.0",
            "-m",
            f"Contact {self._blocked_email()}",
        )
        findings = check_release(self.repo)
        self.assertTrue(
            any(
                "refs/tags/v1.0.0 message: non-example email address" in finding
                for finding in findings
            ),
            findings,
        )
        self.assertNotIn(self._blocked_email(), "\n".join(findings))


if __name__ == "__main__":
    unittest.main()
