from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts.check_public_release import (
    _content_findings,
    _path_findings,
    check_release,
)


class PublicReleaseCheckTests(unittest.TestCase):
    @staticmethod
    def _blocked_email() -> str:
        # Assemble the adversarial fixture at runtime so the release-checker's own
        # source remains publishable without exempting test files from inspection.
        return "maintainer" + "@" + "personal.invalid"

    def test_content_findings_reject_obfuscated_sensitive_assignments(self) -> None:
        opaque = "opaque" + "value" + "123"
        probes = (
            "passw" + chr(0x0301) + "ord=" + opaque,
            "pass" + "." + "word=" + opaque,
            "api" + "/" + "key=" + opaque,
        )
        for probe in probes:
            with self.subTest(probe=probe):
                self.assertIn(
                    "Unicode-obfuscated credential or secret material",
                    _content_findings(probe),
                )

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

    def _head(self) -> str:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=self.repo,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

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

    def test_dependabot_public_signoff_address_is_allowed_narrowly(self) -> None:
        email = "12345+release-tester@users.noreply.github.com"
        self._commit(
            email,
            message=(
                "Update one dependency\n\n"
                "Signed-off-by: dependabot[bot] <support@github.com>"
            ),
        )

        self.assertEqual(check_release(self.repo), [])

        ordinary_github_address = "person" + "@github.com"
        self._commit(
            email,
            message=(
                "Unsafe signoff fixture\n\n"
                f"Signed-off-by: Example Person <{ordinary_github_address}>"
            ),
        )
        self.assertTrue(
            any(
                "commit " in finding
                and "message: non-example email address" in finding
                for finding in check_release(self.repo)
            )
        )

    def test_ranged_scan_requires_checkout_of_the_exact_pr_head(self) -> None:
        safe_email = "12345+release-tester@users.noreply.github.com"
        self._commit(safe_email, message="trusted base")
        base = self._head()
        self._commit(safe_email, message="reviewed candidate")
        candidate = self._head()
        self._commit(self._blocked_email(), message="synthetic merge fixture")

        default_findings = check_release(self.repo)
        self.assertTrue(
            any("author identity: non-example email address" in item for item in default_findings),
            default_findings,
        )
        with self.assertRaisesRegex(ValueError, "checked-out HEAD"):
            check_release(
                self.repo,
                history_ref=candidate,
                history_base=base,
            )

    def test_history_ref_must_be_a_full_commit_or_head(self) -> None:
        self._commit("12345+release-tester@users.noreply.github.com")
        with self.assertRaisesRegex(ValueError, "full 40-character"):
            check_release(self.repo, history_ref="refs/heads/main")

    def test_validated_base_excludes_only_already_trusted_history(self) -> None:
        self._commit(self._blocked_email(), message="pre-existing public history")
        base = self._head()
        self._commit(
            "12345+release-tester@users.noreply.github.com",
            message="reviewed candidate",
        )
        candidate = self._head()

        self.assertEqual(
            check_release(
                self.repo,
                history_ref=candidate,
                history_base=base,
            ),
            [],
        )

    def test_validated_base_still_rejects_new_private_identity(self) -> None:
        self._commit("12345+release-tester@users.noreply.github.com")
        base = self._head()
        self._commit(self._blocked_email(), message="unsafe candidate")
        candidate = self._head()

        findings = check_release(
            self.repo,
            history_ref=candidate,
            history_base=base,
        )
        self.assertTrue(
            any("author identity: non-example email address" in item for item in findings),
            findings,
        )

    def test_history_base_must_be_full_and_ancestral(self) -> None:
        safe_email = "12345+release-tester@users.noreply.github.com"
        self._commit(safe_email, message="root")
        root = self._head()
        self._git("switch", "--quiet", "-c", "unrelated")
        self._commit(safe_email, message="unrelated base")
        unrelated = self._head()
        self._git("switch", "--quiet", "-c", "candidate", root)
        self._commit(safe_email, message="candidate")
        candidate = self._head()

        with self.assertRaisesRegex(ValueError, "full 40-character"):
            check_release(
                self.repo,
                history_ref=candidate,
                history_base="HEAD",
            )
        with self.assertRaisesRegex(ValueError, "precede"):
            check_release(
                self.repo,
                history_ref=candidate,
                history_base=candidate,
            )
        with self.assertRaisesRegex(ValueError, "all-zero"):
            check_release(
                self.repo,
                history_ref=candidate,
                history_base="0" * 40,
            )
        with self.assertRaisesRegex(ValueError, "ancestor"):
            check_release(
                self.repo,
                history_ref=candidate,
                history_base=unrelated,
            )

    def test_ranged_release_must_be_one_direct_non_merge_commit(self) -> None:
        safe_email = "12345+release-tester@users.noreply.github.com"
        self._commit(safe_email, message="trusted base")
        base = self._head()
        self._commit(safe_email, message="first candidate commit")
        self._commit(safe_email, message="second candidate commit")
        candidate = self._head()

        with self.assertRaisesRegex(ValueError, "exactly one reviewed"):
            check_release(
                self.repo,
                history_ref=candidate,
                history_base=base,
            )

    def test_ranged_release_index_must_match_candidate_commit(self) -> None:
        safe_email = "12345+release-tester@users.noreply.github.com"
        self._commit(safe_email, message="trusted base")
        base = self._head()
        self._commit(safe_email, message="reviewed candidate")
        candidate = self._head()
        (self.repo / "README.md").write_text(
            "different staged tree\n", encoding="utf-8"
        )
        self._git("add", "README.md")

        with self.assertRaisesRegex(ValueError, "index"):
            check_release(
                self.repo,
                history_ref=candidate,
                history_base=base,
            )

    def test_poisoned_path_git_is_rejected_without_execution(self) -> None:
        self._commit("12345+release-tester@users.noreply.github.com")
        poison = self.repo / ("git.exe" if os.name == "nt" else "git")
        poison.write_bytes(b"not a trusted executable")
        if os.name != "nt":
            poison.chmod(0o755)
        with (
            mock.patch.dict(os.environ, {"PATH": str(self.repo)}, clear=False),
            mock.patch("scripts.check_public_release.subprocess.run") as run,
            self.assertRaisesRegex(RuntimeError, "trusted OS-administered Git"),
        ):
            check_release(self.repo)
        run.assert_not_called()

    def test_all_release_inspection_uses_one_absolute_trusted_git(self) -> None:
        self._commit("12345+release-tester@users.noreply.github.com")
        trusted_git = Path(shutil.which("git") or shutil.which("git.exe") or "").resolve()
        real_run = subprocess.run
        with (
            mock.patch(
                "scripts.check_public_release._resolve_trusted_git",
                return_value=trusted_git,
            ),
            mock.patch(
                "scripts.check_public_release.subprocess.run", wraps=real_run
            ) as run,
        ):
            self.assertEqual(check_release(self.repo), [])
        self.assertTrue(run.call_args_list)
        for call in run.call_args_list:
            command = call.args[0]
            self.assertEqual(Path(command[0]), trusted_git)
            self.assertTrue(Path(command[0]).is_absolute())

    def test_workflow_passes_the_pr_head_to_the_history_scan(self) -> None:
        workflow = (
            Path(__file__).resolve().parents[1] / ".github" / "workflows" / "ci.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("github.event.pull_request.head.sha", workflow)
        self.assertIn("github.event.pull_request.base.sha", workflow)
        self.assertIn("github.event.before", workflow)
        self.assertIn("workflow_dispatch", workflow)
        self.assertIn("refs/heads/main", workflow)
        self.assertIn('--history-ref "$PUBLIC_RELEASE_HISTORY_REF"', workflow)
        self.assertIn('--history-base "$PUBLIC_RELEASE_HISTORY_BASE"', workflow)

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

    def test_spaced_home_segment_cannot_hide_behind_allowed_prefix(self) -> None:
        private_paths = "\n".join((
            "\\".join(("C:", "Users", "test private", "record.txt")),
            "/".join(("", "home", "test private", "record.txt")),
        ))
        (self.repo / "paths.txt").write_text(private_paths, encoding="utf-8")
        self._git("add", "paths.txt")
        self._commit("12345+release-tester@users.noreply.github.com")

        findings = check_release(self.repo)

        self.assertTrue(
            any("concrete Windows user-home path" in item for item in findings),
            findings,
        )
        self.assertTrue(
            any("concrete POSIX user-home path" in item for item in findings),
            findings,
        )

    def test_concrete_unc_user_home_blocks_release(self) -> None:
        private_path = (
            "\\\\" + "private-server" + "\\Users\\private-person\\record.txt"
        )
        (self.repo / "unc.txt").write_text(private_path, encoding="utf-8")
        self._git("add", "unc.txt")
        self._commit("12345+release-tester@users.noreply.github.com")

        findings = check_release(self.repo)

        self.assertTrue(
            any("concrete UNC user-home path" in item for item in findings),
            findings,
        )

    def test_internationalized_personal_email_blocks_release(self) -> None:
        private_email = "josé" + "@" + "personal.invalid"
        (self.repo / "contact.txt").write_text(private_email, encoding="utf-8")
        self._git("add", "contact.txt")
        self._commit("12345+release-tester@users.noreply.github.com")

        findings = check_release(self.repo)

        self.assertTrue(
            any("non-example email address" in item for item in findings),
            findings,
        )

    def test_decomposed_internationalized_email_blocks_release(self) -> None:
        private_email = "jose\u0301" + "@" + "personal.invalid"
        (self.repo / "contact.txt").write_text(private_email, encoding="utf-8")
        self._git("add", "contact.txt")
        self._commit("12345+release-tester@users.noreply.github.com")

        findings = check_release(self.repo)

        self.assertTrue(
            any("non-example email address" in item for item in findings),
            findings,
        )

    def test_unicode_email_obfuscation_blocks_release(self) -> None:
        safe_email = "12345+release-tester@users.noreply.github.com"
        variants = (
            "maintainer" + "\u200b" + "@" + "personal.invalid",
            "maintainer" + "@" + "\u200b" + "personal.invalid",
            "maintainer" + "\uff20" + "personal.invalid",
            "maintainer" + "@" + "personal" + "\u3002" + "invalid",
            "maintainer" + "@" + "personal" + "\uff0e" + "invalid",
            "maintainer" + "@" + "\u034f" + "personal.invalid",
            "maintainer" + "@" + "personal" + "\ufe0f" + ".invalid",
            "jose" + "\u200d\u0301" + "@" + "personal.invalid",
            "\u0909\u092a\u092f\u094b\u0917\u0915\u0930\u094d\u0924\u093e"
            + "@"
            + "\u0928\u093f\u091c\u0940.\u092d\u093e\u0930\u0924",
        )
        for index, private_email in enumerate(variants):
            with self.subTest(private_email=private_email):
                path = self.repo / f"contact-{index}.txt"
                path.write_text(private_email, encoding="utf-8")
                self._git("add", path.name)
                self._commit(safe_email, message=f"unicode fixture {index}")
                findings = check_release(self.repo)
                self.assertTrue(
                    any("non-example email address" in item for item in findings),
                    findings,
                )
                self._git("rm", "--quiet", path.name)
                self._commit(safe_email, message=f"remove unicode fixture {index}")

    def test_unicode_obfuscation_cannot_hide_private_paths_or_file_types(self) -> None:
        for path in (
            ".\uff45nv",
            "re\u200bports/private.txt",
            ".se\u034fcrets/token.txt",
            "private.p\ufe0fdf",
            ".еnv",
            "repоrts/private.txt",
            ".ѕecrets/token.txt",
            ".env.exam\u034fple",
            "data/.git\ufe0fkeep",
            ".env.exam\x01ple",
            "data/.git\x01keep",
        ):
            with self.subTest(path=path):
                self.assertTrue(_path_findings(path), path)

        safe_email = "12345+release-tester@users.noreply.github.com"
        paths = "\n".join((
            "C:\\U\u034fsers\\private-person\\record.txt",
            "/ho\u034fme/private-person/record.txt",
            "\\\\server\\U\ufe0fsers\\private-person\\record.txt",
        ))
        (self.repo / "obfuscated-paths.txt").write_text(paths, encoding="utf-8")
        self._git("add", "obfuscated-paths.txt")
        self._commit(safe_email, message="unicode path privacy fixture")

        findings = check_release(self.repo)
        self.assertTrue(
            any("user-home path" in finding for finding in findings),
            findings,
        )

    def test_unicode_obfuscation_cannot_hide_credentials_from_release_scan(self) -> None:
        opaque_value = "release-secret-fixture-value"
        fullwidth = lambda value: "".join(
            chr(ord(character) + 0xFEE0)
            if 0x21 <= ord(character) <= 0x7E
            else character
            for character in value
        )
        for value in (
            fullwidth("API_KEY=" + opaque_value),
            "API\u200b_KEY=" + opaque_value,
            "API_KEY" + fullwidth("=") + opaque_value,
        ):
            with self.subTest(value=value):
                findings = _content_findings(value)
                self.assertIn(
                    "Unicode-obfuscated credential or secret material",
                    findings,
                )
                self.assertNotIn(opaque_value, "\n".join(findings))

    def test_normalization_never_grants_an_identity_or_placeholder_exception(self) -> None:
        obfuscated_allowed_emails = (
            "git\u0301" + "@" + "github.com",
            "support" + "@" + "github\u0301.com",
            "12345+release-tester\u034f"
            + "@"
            + "users.noreply.github.com",
            "git\x01" + "@" + "github.com",
            "support" + "@" + "github\x01.com",
        )
        for value in obfuscated_allowed_emails:
            with self.subTest(value=value):
                self.assertIn("non-example email address", _content_findings(value))

        windows_home = "\\".join(("C:", "Users"))
        posix_home = "/" + "/".join(("home",))
        unc_home = "//" + "/".join(("ser\u034fver", "Users"))
        obfuscated_placeholders = (
            windows_home + "\\exa\u034fmple\\record.txt",
            posix_home + "/exa\ufe0fmple/record.txt",
            unc_home + "/exa\u034fmple/record.txt",
            windows_home + "\\exa\x01mple\\record.txt",
        )
        for value in obfuscated_placeholders:
            with self.subTest(value=value):
                self.assertTrue(
                    any(
                        "user-home path" in finding
                        for finding in _content_findings(value)
                    )
                )

        self.assertEqual(_content_findings("git" + "@" + "github.com"), [])
        self.assertEqual(
            _content_findings("C:\\Users\\example\\record.txt"),
            [],
        )

    def test_slash_prefixed_personal_email_blocks_release(self) -> None:
        private_text = "contacts/jose" + "@" + "personal.invalid"
        (self.repo / "contact-path.txt").write_text(private_text, encoding="utf-8")
        self._git("add", "contact-path.txt")
        self._commit("12345+release-tester@users.noreply.github.com")

        findings = check_release(self.repo)

        self.assertTrue(
            any("non-example email address" in item for item in findings),
            findings,
        )

    def test_package_versions_and_ip_userinfo_are_not_emails(self) -> None:
        safe_text = "\n".join((
            "vendor/package-with-a-long-version" + "@" + "1.2.3-beta.4",
            "example" + "@" + "1.0.0",
            "safe-scope/registry-package" + "@" + "1.2.3",
            "pass" + "@" + "192.168.50.2",
        ))
        (self.repo / "package-specs.txt").write_text(safe_text, encoding="utf-8")
        self._git("add", "package-specs.txt")
        self._commit("12345+release-tester@users.noreply.github.com")

        self.assertEqual(check_release(self.repo), [])

    def test_non_utf8_and_nul_blobs_are_blocked(self) -> None:
        for filename, payload in (
            ("opaque.bin", b"\xff\xfeprivate"),
            ("embedded-zero.txt", b"public\0private"),
            ("late-zero.txt", b"a" * 8192 + b"\0tail"),
        ):
            with self.subTest(filename=filename):
                path = self.repo / filename
                path.write_bytes(payload)
                self._git("add", filename)
                self._commit("12345+release-tester@users.noreply.github.com")
                findings = check_release(self.repo)
                self.assertTrue(
                    any(
                        "non-UTF-8 or NUL-containing tracked content is blocked"
                        in item
                        for item in findings
                    ),
                    findings,
                )
                self._git("rm", filename)
                self._commit("12345+release-tester@users.noreply.github.com")

    def test_redacted_home_placeholders_remain_publishable(self) -> None:
        safe_paths = "\n".join((
            "\\".join(("C:", "Users", "[USER]", "record.txt")),
            "/".join(("", "home", "[USER]", "record.txt")),
            "\\\\" + "[HOST]" + "\\Users\\[USER]\\record.txt",
        ))
        (self.repo / "safe-paths.txt").write_text(safe_paths, encoding="utf-8")
        self._git("add", "safe-paths.txt")
        self._commit("12345+release-tester@users.noreply.github.com")

        self.assertEqual(check_release(self.repo), [])

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
