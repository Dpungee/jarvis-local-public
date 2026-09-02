from __future__ import annotations

import unittest

from jarvis.memory import _redacted_json_value
from jarvis.redaction import (
    contains_obfuscated_secret,
    contains_private_identifier,
    contains_sensitive_key_phrase,
    contains_secret,
    is_redacted_descriptor,
    is_sensitive_key,
    redact_private_identifiers,
    redact_secrets,
)


class SharedRedactionTests(unittest.TestCase):
    def test_namespaced_sensitive_assignments_use_the_same_policy(self):
        keys = (
            "OPENAI_API_KEY",
            "MY_PASSWORD",
            "JARVIS_MEMORY_HOLDOUT_V6_TOKEN",
            "service.oauth-token",
        )
        for key in keys:
            with self.subTest(key=key):
                value = f"{key}=hunter2"
                self.assertTrue(contains_secret(value))
                self.assertEqual(redact_secrets(value), "[REDACTED]")

    def test_namespaced_sensitive_keys_redact_structured_values(self):
        for key in (
            "OPENAI_API_KEY",
            "MY_PASSWORD",
            "JARVIS_MEMORY_HOLDOUT_V6_TOKEN",
            "service.oauth-token",
        ):
            with self.subTest(key=key):
                self.assertTrue(is_sensitive_key(key))
                self.assertEqual(
                    _redacted_json_value({key: "hunter2", "safe": "ordinary"}),
                    {key: "[REDACTED]", "safe": "ordinary"},
                )

    def test_short_sensitive_assignments_share_one_key_policy(self):
        keys = (
            "password",
            "api_key",
            "access_key",
            "secret_key",
            "private_key",
            "access_token",
            "refresh_token",
            "session_token",
            "auth_token",
            "oauth_token",
            "id_token",
            "client_secret",
            "authorization",
            "credential",
            "credentials",
            "cookie",
            "session_cookie",
            "recovery_code",
            "mfa_code",
            "token",
            "secret",
        )
        for key in keys:
            with self.subTest(key=key):
                value = f"{key}=hunter2"
                self.assertTrue(is_sensitive_key(key))
                self.assertTrue(contains_secret(value))
                self.assertEqual(redact_secrets(value), "[REDACTED]")

    def test_ordinary_words_are_not_sensitive_assignment_keys(self):
        for key in (
            "tokenization",
            "secretary",
            "authorization_notes",
            "TEAM_SECRETARY",
            "TOKENIZATION_MODE",
            "accessibility",
        ):
            with self.subTest(key=key):
                self.assertFalse(is_sensitive_key(key))
                self.assertFalse(contains_secret(f"{key}=ordinary"))

    def test_generic_token_and_secret_descriptors_are_bounded(self):
        for phrase in (
            "token value",
            "secret value",
            "authentication token value",
            "current token",
            "secret field",
            "account secret",
            "login token",
            "\uff54\uff4f\uff4b\uff45\uff4e\u3000\uff56\uff41\uff4c\uff55\uff45",
        ):
            with self.subTest(phrase=phrase):
                self.assertTrue(contains_sensitive_key_phrase(phrase))

        for phrase in (
            "review token",
            "handoff token",
            "token budget",
            "token bucket algorithm",
            "secret sharing algorithm",
            "secretary value",
        ):
            with self.subTest(phrase=phrase):
                self.assertFalse(contains_sensitive_key_phrase(phrase))

    def test_unicode_obfuscation_cannot_hide_secret_assignments_or_keys(self):
        opaque_value = "ordinary-looking-secret-value"
        fullwidth = lambda value: "".join(
            chr(ord(character) + 0xFEE0)
            if 0x21 <= ord(character) <= 0x7E
            else character
            for character in value
        )
        variants = (
            fullwidth("API_KEY=" + opaque_value),
            "API\u200b_KEY=" + opaque_value,
            "API_KEY" + fullwidth("=") + opaque_value,
            fullwidth("sk-proj-" + "A" * 20),
        )
        for value in variants:
            with self.subTest(value=value):
                self.assertTrue(contains_secret(value))
                self.assertTrue(contains_obfuscated_secret(value))
                redacted = redact_secrets(value)
                self.assertNotIn(opaque_value, redacted)
                self.assertFalse(contains_secret(redacted))

        self.assertTrue(is_sensitive_key(fullwidth("PASSWORD")))
        self.assertTrue(is_sensitive_key("api\u200b_key"))

    def test_combining_marks_and_key_separators_cannot_hide_assignments(self):
        opaque = "opaque" + "value" + "123"
        combining_variants = (
            "passw" + chr(0x0301) + "ord=" + opaque,
            "passw" + chr(0x0327) + "ord=" + opaque,
            "passw" + chr(0x20DD) + "ord=" + opaque,
        )
        separator_variants = tuple(
            key + "=" + opaque
            for key in (
                "pass" + "." + "word",
                "pass" + "_" + "word",
                "pass" + "-" + "word",
                "pass" + " " + "word",
                "api" + "/" + "key",
                "api" + "__" + "key",
            )
        )
        for value in (*combining_variants, *separator_variants):
            with self.subTest(value=value):
                self.assertTrue(contains_secret(value))
                self.assertTrue(contains_obfuscated_secret(value))
                self.assertEqual(redact_secrets(value), "[REDACTED]")

        for key in (
            "pass" + "." + "word",
            "pass" + " " + "word",
            "api" + "/" + "key",
        ):
            with self.subTest(key=key):
                self.assertTrue(is_sensitive_key(key))

    def test_only_exact_digest_descriptors_are_treated_as_already_redacted(self):
        valid = {"redacted": True, "bytes": 7, "sha256": "a" * 64}
        self.assertTrue(is_redacted_descriptor(valid))
        invalid = (
            {**valid, "raw": "hunter2"},
            {**valid, "sha256": "not-a-digest"},
            {**valid, "bytes": True},
            {"redacted": True, "sha256": "a" * 64},
        )
        for value in invalid:
            with self.subTest(value=value):
                self.assertFalse(is_redacted_descriptor(value))


class PrivateIdentifierRedactionTests(unittest.TestCase):
    def test_package_versions_ip_userinfo_and_templates_are_not_emails(self) -> None:
        for value in (
            "vendor/package-with-a-long-version" + "@" + "1.2.3-beta.4",
            "example" + "@" + "1.0.0",
            "safe-scope/registry-package" + "@" + "1.2.3",
            "{token}" + "@" + "packages.example",
            "pass" + "@" + "192.168.50.2",
        ):
            with self.subTest(value=value):
                self.assertFalse(contains_private_identifier(value))
                self.assertEqual(redact_private_identifiers(value), value)

    def test_internationalized_email_addresses_are_fully_redacted(self) -> None:
        for original in (
            "josé@example.com",
            "jose\u0301@example.com",
            "δοκιμή@παράδειγμα.example",
            "/jane.doe" + "@" + "corp.com",
            "contacts/jose" + "@" + "personal.invalid",
        ):
            with self.subTest(original=original):
                self.assertTrue(contains_private_identifier(original))
                redacted = redact_private_identifiers(original)
                self.assertEqual(redacted, "[EMAIL]")
                self.assertFalse(contains_private_identifier(redacted))

    def test_format_controls_and_unicode_email_separators_cannot_evade_redaction(self) -> None:
        local = "alice"
        domain = "personal.invalid"
        for original in (
            local + "\u200b@" + domain,
            local + "@\u200b" + domain,
            local + "\uff20personal.invalid",
            local + "@personal\u3002invalid",
            local + "@personal\uff0einvalid",
        ):
            with self.subTest(original=original):
                self.assertTrue(contains_private_identifier(original))
                self.assertEqual(redact_private_identifiers(original), "[EMAIL]")

    def test_default_ignorables_and_combining_scripts_cannot_evade_redaction(self) -> None:
        local = "maintainer"
        domain = "personal.invalid"
        for original in (
            local + "@\u034f" + domain,
            local + "@personal\ufe0f.invalid",
            "jose\u200d\u0301" + "@" + domain,
            "\u0909\u092a\u092f\u094b\u0917\u0915\u0930\u094d\u0924\u093e"
            + "@"
            + "\u0928\u093f\u091c\u0940.\u092d\u093e\u0930\u0924",
        ):
            with self.subTest(original=original):
                self.assertTrue(contains_private_identifier(original))
                redacted = redact_private_identifiers(original)
                self.assertEqual(redacted, "[EMAIL]")
                self.assertFalse(contains_private_identifier(redacted))

    def test_default_ignorables_cannot_hide_user_home_paths(self) -> None:
        for original in (
            "C:\\U\u034fsers\\private-person\\record.txt",
            "/ho\u034fme/private-person/record.txt",
            "\\\\server\\U\ufe0fsers\\private-person\\record.txt",
        ):
            with self.subTest(original=original):
                self.assertTrue(contains_private_identifier(original))
                self.assertNotIn(
                    "private-person", redact_private_identifiers(original)
                )

    def test_user_home_segments_with_spaces_are_fully_redacted(self) -> None:
        for original in (
            r"C:\Users" + r"\Test User\secret.txt",
            "/Users" + "/Test User/secret.txt",
            "/home" + "/Test User/secret.txt",
        ):
            with self.subTest(original=original):
                self.assertTrue(contains_private_identifier(original))
                redacted = redact_private_identifiers(original)
                self.assertNotIn("Test User", redacted)
                self.assertIn("[USER]", redacted)
                self.assertFalse(contains_private_identifier(redacted))

    def test_redirected_unc_user_homes_are_fully_redacted(self) -> None:
        for original in (
            r"\\" + r"example-server\Users\Test User\secret.txt",
            "//" + "example-server/homes/Test User/secret.txt",
        ):
            with self.subTest(original=original):
                self.assertTrue(contains_private_identifier(original))
                redacted = redact_private_identifiers(original)
                self.assertNotIn("Test User", redacted)
                self.assertNotIn("example-server", redacted)
                self.assertIn("[HOST]", redacted)
                self.assertIn("[USER]", redacted)
                self.assertFalse(contains_private_identifier(redacted))

    def test_redacted_placeholders_remain_nonprivate(self) -> None:
        for value in (
            r"C:\Users" + r"\[USER]\workspace",
            "/home" + "/[USER]/workspace",
            r"\\" + r"[HOST]\Users\[USER]\workspace",
        ):
            with self.subTest(value=value):
                self.assertEqual(redact_private_identifiers(value), value)
                self.assertFalse(contains_private_identifier(value))


if __name__ == "__main__":
    unittest.main()
