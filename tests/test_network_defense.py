from __future__ import annotations

import json
import hashlib
import unittest
from datetime import datetime, timedelta, timezone

from jarvis.network_defense import (
    MAX_ASSESSMENT_DEVICES,
    MAX_ASSESSMENT_EVENTS,
    MAX_ASSESSMENT_SIGNALS,
    assess_network_defense,
    verify_assessment_receipt,
)


NOW = datetime(2026, 8, 28, 16, 0, tzinfo=timezone.utc)


def opaque(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]


def device(
    device_id: str,
    *,
    trust: str = "recognized",
    visible: bool = True,
    identity: str = "moderate",
) -> dict:
    return {
        "device_id": opaque(device_id),
        "trust_state": trust,
        "visible_now": visible,
        "presence_state": "reachable" if visible else "unobserved",
        "identity_confidence": identity,
        "last_seen": NOW.isoformat(),
        "ipv4": "192.168.50.9",
        "mac": "aa:bb:cc:dd:ee:ff",
        "hostname": "private-device",
    }


def inventory(*devices: dict, last_scan_at: datetime | None = NOW) -> dict:
    return {
        "devices": list(devices),
        "last_scan_at": last_scan_at.isoformat() if last_scan_at else None,
        "last_scan_id": 17 if last_scan_at else None,
        "last_scan_scope_id": opaque("home-scope") if last_scan_at else opaque("home-scope"),
        "coverage_complete_for_selected_range": True,
    }


class NetworkDefenseAssessmentTests(unittest.TestCase):
    def test_missing_observation_fails_closed_without_claiming_compromise(self):
        result = assess_network_defense(inventory(last_scan_at=None), now=NOW)
        self.assertEqual(result["posture"], "review_required")
        self.assertEqual(result["signals"][0]["rule_id"], "monitoring_not_established")
        self.assertFalse(result["signals"][0]["compromise_established"])
        self.assertFalse(result["automatic_containment"]["enabled"])

    def test_baseline_review_and_randomized_identity_are_information_not_threats(self):
        result = assess_network_defense(
            inventory(device("dev-a", trust="unreviewed", identity="limited")),
            now=NOW,
        )
        rules = {item["rule_id"]: item for item in result["signals"]}
        self.assertEqual(result["posture"], "monitor")
        self.assertEqual(rules["inventory_classification_incomplete"]["severity"], "informational")
        self.assertEqual(rules["limited_device_identity"]["severity"], "informational")
        self.assertEqual(result["attention_signal_count"], 0)

    def test_recent_new_unreviewed_device_is_reviewable_hypothesis(self):
        result = assess_network_defense(
            inventory(device("dev-new", trust="unreviewed")),
            [{
                "event_id": 7,
                "device_id": opaque("dev-new"),
                "event_type": "new_device_observed",
                "observed_at": (NOW - timedelta(minutes=5)).isoformat(),
            }],
            now=NOW,
        )
        signal = next(item for item in result["signals"] if item["rule_id"] == "new_unreviewed_device")
        self.assertEqual(signal["severity"], "medium")
        self.assertEqual(signal["confidence"], "medium")
        self.assertGreaterEqual(len(signal["benign_explanations"]), 2)
        self.assertFalse(signal["compromise_established"])

    def test_retired_and_watched_devices_are_prioritized_without_containment(self):
        result = assess_network_defense(
            inventory(
                device("dev-retired", trust="retired"),
                device("dev-watch", trust="watch"),
            ),
            now=NOW,
        )
        self.assertEqual(result["posture"], "urgent_review")
        self.assertEqual(result["signals"][0]["rule_id"], "retired_device_reappeared")
        self.assertIn("watched_device_observed", {row["rule_id"] for row in result["signals"]})
        self.assertFalse(result["automatic_containment"]["enabled"])

    def test_stale_and_truncated_visibility_are_explicit(self):
        source = inventory(
            device("dev-a"),
            last_scan_at=NOW - timedelta(days=2),
        )
        source["coverage_complete_for_selected_range"] = False
        result = assess_network_defense(source, now=NOW)
        rules = {row["rule_id"] for row in result["signals"]}
        self.assertIn("monitoring_stale", rules)
        self.assertIn("observation_range_incomplete", rules)
        self.assertEqual(result["posture"], "review_required")

    def test_output_is_deterministic_and_never_copies_network_identifiers(self):
        source = inventory(device("dev-a", trust="watch"))
        first = assess_network_defense(source, now=NOW)
        second = assess_network_defense(source, now=NOW)
        self.assertEqual(first, second)
        self.assertTrue(verify_assessment_receipt(first))
        rendered = json.dumps(first, sort_keys=True)
        for private_value in ("192.168.50.9", "aa:bb:cc:dd:ee:ff", "private-device"):
            self.assertNotIn(private_value, rendered)

        tampered = dict(first)
        tampered["conclusion"] = "safe"
        self.assertFalse(verify_assessment_receipt(tampered))

    def test_old_new_device_event_does_not_remain_an_active_alert(self):
        result = assess_network_defense(
            inventory(device("dev-old", trust="unreviewed")),
            [{
                "event_id": 3,
                "device_id": opaque("dev-old"),
                "event_type": "new_device_observed",
                "observed_at": (NOW - timedelta(days=3)).isoformat(),
            }],
            now=NOW,
        )
        self.assertNotIn("new_unreviewed_device", {row["rule_id"] for row in result["signals"]})
        self.assertEqual(result["posture"], "monitor")

    def test_reordered_duplicate_events_produce_one_identical_receipt(self):
        source = inventory(device("dev-order", trust="unreviewed"))
        events = [
            {
                "event_id": 10,
                "device_id": opaque("dev-order"),
                "event_type": "new_device_observed",
                "observed_at": (NOW - timedelta(minutes=10)).isoformat(),
            },
            {
                "event_id": 11,
                "device_id": opaque("dev-order"),
                "event_type": "new_device_observed",
                "observed_at": (NOW - timedelta(minutes=5)).isoformat(),
            },
        ]
        first = assess_network_defense(source, events, now=NOW)
        second = assess_network_defense(source, reversed(events), now=NOW)
        self.assertEqual(first, second)
        signal = next(
            item for item in first["signals"]
            if item["rule_id"] == "new_unreviewed_device"
        )
        self.assertEqual(signal["evidence"]["event_id"], 11)

    def test_freshness_transition_changes_receipt_identity_once(self):
        source = inventory(device("dev-fresh"))
        fresh = assess_network_defense(source, now=NOW)
        stale = assess_network_defense(source, now=NOW + timedelta(days=2))
        later_stale = assess_network_defense(source, now=NOW + timedelta(days=3))
        self.assertNotEqual(fresh["assessment_id"], stale["assessment_id"])
        self.assertEqual(stale, later_stale)
        self.assertIn(
            "monitoring_stale", {item["rule_id"] for item in stale["signals"]}
        )

    def test_profile_state_is_not_applied_retroactively(self):
        source = inventory(device("dev-retro", trust="retired"))
        source["devices"][0]["last_active_seen"] = NOW.isoformat()
        source["devices"][0]["profile_updated_at"] = (
            NOW + timedelta(hours=1)
        ).isoformat()
        before_reobservation = assess_network_defense(
            source, now=NOW + timedelta(hours=1)
        )
        self.assertNotIn(
            "retired_device_reappeared",
            {item["rule_id"] for item in before_reobservation["signals"]},
        )
        source["devices"][0]["last_active_seen"] = (
            NOW + timedelta(hours=2)
        ).isoformat()
        after_reobservation = assess_network_defense(
            source, now=NOW + timedelta(hours=2)
        )
        signal = next(
            item for item in after_reobservation["signals"]
            if item["rule_id"] == "retired_device_reappeared"
        )
        self.assertEqual(signal["confidence"], "medium")

    def test_future_event_and_initial_baseline_do_not_create_device_alerts(self):
        source = inventory(device("dev-future", trust="unreviewed"))
        future_event = {
            "event_id": 22,
            "device_id": opaque("dev-future"),
            "event_type": "new_device_observed",
            "observed_at": (NOW + timedelta(minutes=5)).isoformat(),
        }
        future = assess_network_defense(source, [future_event], now=NOW)
        self.assertNotIn(
            "new_unreviewed_device", {item["rule_id"] for item in future["signals"]}
        )
        source["baseline_scan"] = True
        baseline_event = dict(future_event)
        baseline_event["observed_at"] = NOW.isoformat()
        baseline = assess_network_defense(source, [baseline_event], now=NOW)
        self.assertNotIn(
            "new_unreviewed_device", {item["rule_id"] for item in baseline["signals"]}
        )

    def test_malformed_identifiers_and_safety_field_tampering_fail_closed(self):
        secret = "192.168.50.77"
        source = inventory(device("dev-valid", trust="watch"))
        source["devices"].append({
            "device_id": secret,
            "trust_state": "retired<script>",
            "visible_now": True,
            "identity_confidence": "certain",
        })
        result = assess_network_defense(source, now=NOW)
        self.assertTrue(verify_assessment_receipt(result))
        self.assertNotIn(secret, json.dumps(result, sort_keys=True))

        tampered = json.loads(json.dumps(result))
        tampered["automatic_containment"]["enabled"] = True
        payload = dict(tampered)
        payload.pop("receipt_sha256", None)
        tampered["receipt_sha256"] = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        self.assertFalse(verify_assessment_receipt(tampered))

    def test_canonical_order_is_selected_before_bounded_input_caps(self):
        devices = [
            device(f"bounded-device-{index}", trust="unreviewed")
            for index in range(MAX_ASSESSMENT_DEVICES + 1)
        ]
        events = [
            {
                "event_id": index + 1,
                "device_id": row["device_id"],
                "event_type": "new_device_observed",
                "observed_at": (NOW - timedelta(minutes=5)).isoformat(),
            }
            for index, row in enumerate(devices[: MAX_ASSESSMENT_EVENTS + 1])
        ]
        first = assess_network_defense(inventory(*devices), events, now=NOW)
        second = assess_network_defense(
            inventory(*reversed(devices)), reversed(events), now=NOW
        )

        self.assertEqual(first, second)
        self.assertEqual(first["coverage"]["devices_omitted"], 1)
        self.assertEqual(first["coverage"]["events_omitted"], 1)
        self.assertLessEqual(len(first["signals"]), MAX_ASSESSMENT_SIGNALS)
        self.assertTrue(verify_assessment_receipt(first))

    def test_unparseable_timestamp_text_never_reaches_a_receipt(self):
        source = inventory(device("timestamp-device", trust="watch"))
        private_text = "API_KEY=TEST_SECRET_DO_NOT_STORE"
        source["devices"][0].update({
            "last_seen": private_text,
            "last_active_seen": f"{private_text}-active",
            "profile_updated_at": f"{private_text}-profile",
        })
        result = assess_network_defense(source, now=NOW)
        rendered = json.dumps(result, sort_keys=True)

        self.assertNotIn(private_text, rendered)
        saved = result["evidence_snapshot"]["devices"][0]
        self.assertIsNone(saved["last_seen"])
        self.assertIsNone(saved["last_active_seen"])
        self.assertIsNone(saved["profile_updated_at"])
        self.assertTrue(verify_assessment_receipt(result))


if __name__ == "__main__":
    unittest.main()
