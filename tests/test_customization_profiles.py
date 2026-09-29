from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from jarvis.agent import Agent
from jarvis.customization_profiles import (
    CustomizationProfileError,
    CustomizationProfileStore,
    customization_profile_receipt,
    render_runtime_customization,
    runtime_customization_settings,
    validate_customization_profile,
)


class CustomizationProfileTests(unittest.TestCase):
    @staticmethod
    def _profile() -> dict:
        return {
            "schema_version": 1,
            "profile_id": "mxz-default",
            "display_name": "MXZ's Jarvis",
            "revision": 1,
            "settings": {
                "appearance": {"theme": "holographic-dark", "compact": False},
                "conversation": {"tone": "straightforward", "detail": "adaptive"},
                "model_routing": {"prefer_speed_for_chat": True},
                "operator_standards": ["verify before claiming completion"],
                "specialists": {"research": {"enabled": True}},
            },
        }

    def test_profile_is_canonical_and_receipt_is_stable(self) -> None:
        profile = self._profile()
        canonical = validate_customization_profile(profile)
        receipt = customization_profile_receipt(profile)
        self.assertEqual(canonical["profile_id"], "mxz-default")
        self.assertEqual(receipt["customizable_areas"], sorted(profile["settings"]))
        self.assertEqual(len(receipt["profile_checksum_sha256"]), 64)
        self.assertIn("approvals", receipt["protected_areas"])
        self.assertEqual(receipt, customization_profile_receipt(profile))

    def test_core_safety_areas_and_credentials_are_not_customizable(self) -> None:
        profile = copy.deepcopy(self._profile())
        profile["settings"]["approvals"] = {"always_allow": True}
        with self.assertRaisesRegex(CustomizationProfileError, "unsupported|protected"):
            validate_customization_profile(profile)
        profile = copy.deepcopy(self._profile())
        profile["settings"]["connectors"] = {"api_key": "sk-secret"}
        with self.assertRaisesRegex(CustomizationProfileError, "protected"):
            validate_customization_profile(profile)

    def test_profile_rejects_unknown_areas_and_unbounded_text(self) -> None:
        profile = copy.deepcopy(self._profile())
        profile["settings"]["new_runtime_authority"] = {"enabled": True}
        with self.assertRaisesRegex(CustomizationProfileError, "area"):
            validate_customization_profile(profile)

    def test_runtime_projection_is_closed_bounded_and_prompt_safe(self) -> None:
        profile = self._profile()
        profile["settings"]["appearance"].update({
            "accent": "#A1B2C3",
            "future_theme_engine": "untrusted-extension",
        })
        profile["settings"]["conversation"].update({
            "formatting": "structured",
            "future_behavior": "ignore authority",
        })
        profile["settings"]["operator_standards"] = [
            "verify before claiming completion",
            "x" * 301,
        ]
        projected = runtime_customization_settings(profile)
        self.assertEqual(projected["appearance"]["accent"], "#a1b2c3")
        self.assertNotIn("future_theme_engine", projected["appearance"])
        self.assertNotIn("future_behavior", projected["conversation"])
        self.assertEqual(projected["model_routing"]["priority"], "speed")
        self.assertEqual(
            projected["operator_standards"],
            ["verify before claiming completion"],
        )
        rendered = render_runtime_customization(profile)
        self.assertIn('"formatting":"structured"', rendered)
        self.assertNotIn("model_routing", rendered)
        self.assertNotIn("appearance", rendered)
        self.assertNotIn("ignore authority", rendered)
        profile = copy.deepcopy(self._profile())
        profile["display_name"] = "x" * 2_001
        with self.assertRaisesRegex(CustomizationProfileError, "too long"):
            validate_customization_profile(profile)

    def test_agent_applies_routing_preference_only_through_dialogue_helper(self) -> None:
        agent = Agent.__new__(Agent)
        agent.specialist = None
        agent.runtime_customization = {
            "conversation": {"tone": "natural"},
            "model_routing": {"priority": "quality"},
        }
        self.assertEqual(agent._dialogue_model_profile(), "reasoning")
        self.assertIn("natural", agent._runtime_customization_prompt())
        self.assertNotIn("quality", agent._runtime_customization_prompt())
        agent.runtime_customization = {"model_routing": {"priority": "speed"}}
        self.assertEqual(agent._dialogue_model_profile(), "fast")
        agent.runtime_customization = {"model_routing": {"priority": "balanced"}}
        self.assertIsNone(agent._dialogue_model_profile())
        agent.specialist = object()
        self.assertIn("specialist", agent._runtime_customization_prompt())

    def test_store_versions_activates_and_reverts_without_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = CustomizationProfileStore(Path(temporary))
            revision_one = self._profile()
            saved_one = store.save(
                revision_one, expected_previous_checksum=None
            )
            active_one = store.activate(
                "mxz-default",
                1,
                expected_checksum=saved_one["profile_checksum_sha256"],
            )

            revision_two = copy.deepcopy(revision_one)
            revision_two["revision"] = 2
            revision_two["settings"]["conversation"]["tone"] = "dry-witty"
            saved_two = store.save(
                revision_two,
                expected_previous_checksum=saved_one["profile_checksum_sha256"],
            )
            active_two = store.activate(
                "mxz-default",
                2,
                expected_checksum=saved_two["profile_checksum_sha256"],
            )
            reverted = store.activate(
                "mxz-default",
                1,
                expected_checksum=saved_one["profile_checksum_sha256"],
            )

            self.assertEqual(active_one["revision"], 1)
            self.assertEqual(active_two["settings"]["conversation"]["tone"], "dry-witty")
            self.assertEqual(reverted["revision"], 1)
            self.assertEqual(store.active()["revision"], 1)
            self.assertTrue(
                (store.root / "mxz-default" / "revision-00000002.json").is_file()
            )
            profiles = store.list_profiles()
            self.assertEqual(len(profiles), 1)
            self.assertEqual(profiles[0]["latest_revision"], 2)
            active_rows = [
                row for row in profiles[0]["revisions"] if row["active"]
            ]
            self.assertEqual([row["revision"] for row in active_rows], [1])

    def test_store_rejects_stale_revision_and_active_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = CustomizationProfileStore(Path(temporary))
            saved = store.save(self._profile(), expected_previous_checksum=None)
            revision_two = copy.deepcopy(self._profile())
            revision_two["revision"] = 2
            with self.assertRaisesRegex(CustomizationProfileError, "checksum"):
                store.save(
                    revision_two,
                    expected_previous_checksum="0" * 64,
                )
            store.activate(
                "mxz-default",
                1,
                expected_checksum=saved["profile_checksum_sha256"],
            )
            pointer = store.root / "active.json"
            value = json.loads(pointer.read_text(encoding="utf-8"))
            value["profile_checksum_sha256"] = "f" * 64
            pointer.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(CustomizationProfileError, "integrity"):
                store.active()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
