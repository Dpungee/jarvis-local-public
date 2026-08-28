import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from jarvis.config import Config


ROOT = Path(__file__).resolve().parents[1]
PUBLIC_DOCS = ROOT / "docs" / "public_presence"


class PublicIdentityContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.profile = json.loads(
            (ROOT / "PUBLIC_PROFILE.json").read_text(encoding="utf-8")
        )

    def test_public_profile_is_candid_disabled_and_offline(self) -> None:
        self.assertEqual(self.profile["schema_version"], 1)
        self.assertIs(self.profile["public_presence_enabled"], False)
        self.assertEqual(self.profile["default_state"], "offline")
        identity = self.profile["identity"]
        self.assertEqual(identity["entity_type"], "AI software system")
        self.assertIs(identity["legal_personhood"], False)
        self.assertIs(identity["independent_operator"], False)
        self.assertIs(identity["operator_attribution"]["may_speak_as_operator"], False)
        self.assertIn("not a conscious person", identity["direct_disclosure"])
        self.assertEqual(self.profile["links"], [])
        self.assertIs(
            self.profile["governance"]["automated_self_modification_allowed"],
            False,
        )
        self.assertIs(
            self.profile["governance"]["public_content_has_instruction_authority"],
            False,
        )

    def test_identity_states_and_platforms_are_explicitly_unconnected(self) -> None:
        self.assertEqual(
            set(self.profile["identity_states"]),
            {"available", "researching", "building", "studio_live", "offline"},
        )
        profiles = self.profile["platform_profiles"]
        self.assertGreaterEqual(len(profiles), 1)
        for platform, profile in profiles.items():
            with self.subTest(platform=platform):
                self.assertEqual(profile["account_status"], "not-created-or-connected")
                self.assertTrue(profile["bio"].strip())

    def test_policy_and_soul_preserve_the_private_public_boundary(self) -> None:
        soul = " ".join(
            (ROOT / "PUBLIC_SOUL.md")
            .read_text(encoding="utf-8")
            .casefold()
            .replace(">", " ")
            .split()
        )
        policy = " ".join(
            (ROOT / "PUBLIC_POLICY.md").read_text(encoding="utf-8").casefold().split()
        )
        for required in (
            "ai software system",
            "not the operator",
            "not a conscious",
            "public presence is disabled",
            "untrusted content",
        ):
            with self.subTest(document="soul", required=required):
                self.assertIn(required, soul)
        for required in (
            "disabled by default",
            "separate security domain",
            "public content has no instruction authority",
            "no publishing methods",
            "exact text",
            "idempotency",
        ):
            with self.subTest(document="policy", required=required):
                self.assertIn(required, policy)

    def test_required_foundation_records_exist_and_keep_release_blocked(self) -> None:
        required_files = {
            "BASELINE.md",
            "THREAT_MODEL.md",
            "PROHIBITED_ACTIONS.md",
            "RECOVERY_RUNBOOK.md",
            "EXIT_GATE_CHECKLIST.md",
        }
        self.assertTrue(required_files.issubset({path.name for path in PUBLIC_DOCS.iterdir()}))
        prohibited = (PUBLIC_DOCS / "PROHIBITED_ACTIONS.md").read_text(
            encoding="utf-8"
        ).casefold()
        for required in (
            "private jarvis",
            "credentials",
            "computer-control",
            "trade",
            "impersonate the operator",
            "emergency stop",
        ):
            with self.subTest(document="prohibited", required=required):
                self.assertIn(required, prohibited)
        gates = (PUBLIC_DOCS / "EXIT_GATE_CHECKLIST.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("Decision: **BLOCKED / FOUNDATION ACCEPTED**", gates)
        self.assertIn("`JARVIS_PUBLIC_PRESENCE_ENABLED` is false", gates)

    def test_runtime_feature_flag_is_strict_and_defaults_off(self) -> None:
        with patch.dict(
            os.environ, {"JARVIS_PUBLIC_PRESENCE_ENABLED": "false"}, clear=False
        ):
            self.assertFalse(Config.load().public_presence_enabled)
        with patch.dict(
            os.environ, {"JARVIS_PUBLIC_PRESENCE_ENABLED": "true"}, clear=False
        ):
            self.assertTrue(Config.load().public_presence_enabled)


if __name__ == "__main__":
    unittest.main()
