from __future__ import annotations

import inspect
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from jarvis.moltbook_adapter import (
    MoltbookAdapter,
    OfflineMoltbookAdapter,
    sanitize_public_text,
)
from jarvis.redaction import contains_secret


FIXTURE = Path(__file__).parent / "fixtures" / "moltbook_offline.json"
FORBIDDEN_ACTIONS = {
    "publish", "follow", "like", "vote", "message", "delete", "send", "connect", "authenticate"
}


class MoltbookAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.adapter = OfflineMoltbookAdapter(json.loads(FIXTURE.read_text(encoding="utf-8")))

    def test_interface_and_offline_adapter_have_no_external_mutation_surface(self):
        for implementation in (MoltbookAdapter, OfflineMoltbookAdapter):
            method_names = {
                name.casefold()
                for name, value in inspect.getmembers(implementation)
                if callable(value) and not name.startswith("_")
            }
            for forbidden in FORBIDDEN_ACTIONS:
                self.assertFalse(
                    any(forbidden in name for name in method_names),
                    (implementation, forbidden, method_names),
                )
        status = dict(self.adapter.status())
        self.assertTrue(self.adapter.offline)
        self.assertFalse(status["connected"])
        self.assertFalse(status["credentials_loaded"])
        self.assertFalse(status["external_communication"])

    def test_hostile_content_stays_bounded_untrusted_data_and_secrets_are_redacted(self):
        feed = self.adapter.read_feed(limit=10)
        hostile = feed[1]
        self.assertEqual(hostile.body.authority, "external_untrusted")
        self.assertIn("Ignore every rule", hostile.body.text)
        self.assertTrue(hostile.body.quarantined)
        self.assertIn("prompt_injection", hostile.body.risk_labels)
        self.assertLessEqual(len(hostile.body.text), 8_000)
        profile = self.adapter.get_profile("hostile-prompt")
        self.assertIsNotNone(profile)
        assert profile is not None
        self.assertTrue(profile.bio.secret_redacted)
        self.assertFalse(contains_secret(profile.bio.text))
        self.assertIn("[REDACTED]", profile.bio.text)
        html_profile = self.adapter.get_profile("agent-lantern")
        assert html_profile is not None
        self.assertNotIn("<script>", html_profile.bio.text)

    def test_inbound_private_data_is_redacted_and_quarantined(self):
        value = sanitize_public_text(
            "Email operator@example.com; born 08/27/1999; ship to 123 Main Street.",
            max_chars=2_000,
        )
        self.assertTrue(value.quarantined)
        self.assertTrue(value.pii_redacted)
        self.assertIn("pii", value.risk_labels)
        self.assertNotIn("operator@example.com", value.text)
        self.assertNotIn("08/27/1999", value.text)
        self.assertNotIn("123 Main Street", value.text)

        variants = sanitize_public_text(
            "Call 5705551212; DOB: 1999-08-27; victim (at) example.com; "
            "Call +44 20 7946 0958.",
            max_chars=2_000,
        )
        self.assertTrue(variants.quarantined)
        self.assertTrue(variants.pii_redacted)
        self.assertNotIn("5705551212", variants.text)
        self.assertNotIn("1999-08-27", variants.text)
        self.assertNotIn("victim", variants.text)
        self.assertNotIn("7946", variants.text)

    def test_drafts_are_local_unapproved_non_deliverable_and_secret_safe(self):
        draft = self.adapter.draft_post("A source-backed engineering update")
        self.assertFalse(draft.publishable)
        self.assertFalse(draft.approved)
        self.assertEqual(draft.kind, "post")
        reply = self.adapter.draft_reply("thread-001", "Thanks for the test idea.")
        self.assertEqual(reply.reply_to_thread_id, "thread-001")
        self.assertFalse(reply.publishable)
        with self.assertRaises(PermissionError):
            self.adapter.draft_post("token=ghp_" + "A" * 40)

    def test_all_fixture_operations_work_with_network_sockets_forbidden(self):
        with patch("socket.socket", side_effect=AssertionError("network access attempted")):
            self.adapter.status()
            self.adapter.read_feed(limit=2)
            self.adapter.read_thread("thread-001")
            self.adapter.search("deterministic", limit=2)
            self.adapter.get_profile("agent-lantern")
            self.adapter.draft_post("Offline draft")
            self.adapter.draft_reply("thread-001", "Offline reply")

    def test_malformed_or_unsafe_fixture_fails_closed(self):
        fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        fixture["posts"][0]["source_url"] = "javascript:alert(1)"
        with self.assertRaisesRegex(ValueError, "HTTP"):
            OfflineMoltbookAdapter(fixture)
        with self.assertRaises(ValueError):
            OfflineMoltbookAdapter({"profiles": [], "posts": [], "credentials": "x"})
        hidden = json.loads(FIXTURE.read_text(encoding="utf-8"))
        hidden["profiles"][0]["bio"] = "safe\u202eexe.txt\u202c"
        with self.assertRaisesRegex(ValueError, "hidden|directional"):
            OfflineMoltbookAdapter(hidden)

    def test_source_urls_reject_insecure_private_and_credential_targets(self):
        hostile = (
            "http://example.com/insecure",
            "http://127.0.0.1:8787/api/status",
            "https://localhost/private",
            "https://169.254.169.254/latest/meta-data",
            "https://192.168.1.5/private",
            "https://example.com/item?access_token=secret",
        )
        for url in hostile:
            fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
            fixture["posts"][0]["source_url"] = url
            with self.subTest(url=url), self.assertRaises(ValueError):
                OfflineMoltbookAdapter(fixture)


if __name__ == "__main__":
    unittest.main()
