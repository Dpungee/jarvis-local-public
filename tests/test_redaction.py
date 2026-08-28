import unittest

from jarvis.redaction import (
    contains_secret, is_redacted_descriptor, is_sensitive_key, redact_secrets,
)


class SharedRedactionTests(unittest.TestCase):
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
        for key in ("tokenization", "secretary", "authorization_notes", "accessibility"):
            with self.subTest(key=key):
                self.assertFalse(is_sensitive_key(key))
                self.assertFalse(contains_secret(f"{key}=ordinary"))

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


if __name__ == "__main__":
    unittest.main()
