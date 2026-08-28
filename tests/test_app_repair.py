from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from jarvis.app_repair import (
    RepairAction,
    approval_required,
    build_repair_plan,
    build_verified_lesson,
    classify_app_failure,
    complete_repair,
    lesson_is_applicable,
    validate_repair_plan,
)


NOW = datetime(2026, 8, 28, 16, 0, tzinfo=timezone.utc)


def _render_cache_diagnosis():
    return classify_app_failure(
        {
            "process_running": True,
            "network_reachable": True,
            "authentication_ok": True,
            "ui_rendered": False,
            "javascript_errors": ["embedded browser render failed"],
            "cache_bytes": 389 * 1024 * 1024,
        }
    )


def _render_cache_plan():
    return build_repair_plan(
        {
            "id": "epic-games-launcher",
            "name": "Epic Games Launcher",
            "version": "18.8.0",
        },
        _render_cache_diagnosis(),
        {
            "process_running": True,
            "cache_paths": ["app-data/webcache_4430"],
            "backup_root": "app-data/jarvis-repair-backups",
        },
    )


def _verified_evidence() -> dict[str, object]:
    return {
        "approval_authorized": True,
        "backup_created": True,
        "source_moved": True,
        "restart_observed": True,
        "ui_rendered": True,
        "health_check_passed": True,
        "observed_at": NOW.isoformat(),
    }


class AppRepairTests(unittest.TestCase):
    def test_diagnosis_classifies_each_supported_failure_family(self) -> None:
        cases = (
            (
                "connectivity",
                {
                    "process_running": True,
                    "network_reachable": False,
                    "dns_ok": False,
                },
            ),
            (
                "render_cache",
                {
                    "process_running": True,
                    "network_reachable": True,
                    "authentication_ok": True,
                    "ui_rendered": False,
                    "javascript_errors": ["renderer crashed"],
                    "cache_bytes": 10_000,
                },
            ),
            (
                "authentication",
                {
                    "process_running": True,
                    "network_reachable": True,
                    "authentication_ok": False,
                    "authentication_error": "invalid_grant",
                },
            ),
            (
                "process",
                {
                    "process_running": False,
                    "crash_count": 2,
                },
            ),
            (
                "update",
                {
                    "process_running": True,
                    "update_required": True,
                    "installed_version": "1.0.0",
                    "minimum_version": "2.0.0",
                },
            ),
            ("unknown", {"process_running": True}),
        )

        for expected, evidence in cases:
            with self.subTest(expected=expected):
                payload = classify_app_failure(evidence).to_payload()
                self.assertEqual(payload["category"], expected)
                self.assertGreaterEqual(payload["confidence"], 0.0)
                self.assertLessEqual(payload["confidence"], 1.0)
                self.assertIsInstance(payload["evidence"], list)
                self.assertIsInstance(payload["alternatives"], list)
                self.assertIsInstance(payload["limitations"], list)

    def test_positive_connectivity_and_auth_evidence_selects_render_cache(self) -> None:
        payload = _render_cache_diagnosis().to_payload()

        self.assertEqual(payload["category"], "render_cache")
        joined = " ".join(payload["evidence"]).casefold()
        self.assertIn("network", joined)
        self.assertTrue("render" in joined or "cache" in joined)

    def test_render_cache_plan_is_reversible_and_never_deletes_data(self) -> None:
        plan = _render_cache_plan()
        payload = plan.to_payload()

        self.assertEqual(payload["diagnosis"]["category"], "render_cache")
        self.assertTrue(payload["reversible"])
        self.assertTrue(payload["requires_approval"])
        self.assertEqual(
            [action["kind"] for action in payload["actions"]],
            ["backup_move", "restart", "verify"],
        )
        backup = payload["actions"][0]
        self.assertEqual(backup["source"], "app-data/webcache_4430")
        self.assertNotEqual(backup["source"], backup["destination"])
        serialized = repr(payload).casefold()
        for forbidden in (
            "delete",
            "remove-item",
            "firewall",
            "proxy",
            "hosts file",
            "registry",
            "disable security",
        ):
            self.assertNotIn(forbidden, serialized)

    def test_plan_validator_rejects_deletion_and_system_security_mutations(self) -> None:
        valid = _render_cache_plan()
        validate_repair_plan(valid)

        for forbidden_kind in (
            "delete",
            "firewall_change",
            "proxy_change",
            "hosts_change",
            "registry_change",
            "security_change",
        ):
            with self.subTest(kind=forbidden_kind):
                forged = replace(
                    valid,
                    actions=(
                        RepairAction(
                            kind=forbidden_kind,
                            source="system",
                            destination=None,
                            verifier=None,
                        ),
                    ),
                )
                with self.assertRaises(ValueError):
                    validate_repair_plan(forged)

    def test_mutating_plan_requires_exact_operator_approval(self) -> None:
        plan = _render_cache_plan()
        self.assertTrue(approval_required(plan))

        evidence = _verified_evidence()
        evidence["approval_authorized"] = False
        with self.assertRaises(PermissionError):
            complete_repair(plan, evidence)

        evidence.pop("approval_authorized")
        with self.assertRaises(PermissionError):
            complete_repair(plan, evidence)

    def test_completion_requires_backup_restart_and_observed_health_evidence(self) -> None:
        plan = _render_cache_plan()

        for missing in (
            "backup_created",
            "source_moved",
            "restart_observed",
            "ui_rendered",
            "health_check_passed",
        ):
            with self.subTest(missing=missing):
                evidence = _verified_evidence()
                evidence.pop(missing)
                outcome = complete_repair(plan, evidence).to_payload()
                self.assertNotEqual(outcome["status"], "verified")

        title_only = _verified_evidence()
        title_only.pop("ui_rendered")
        title_only["window_title"] = "Epic Games Launcher - Store"
        self.assertNotEqual(
            complete_repair(plan, title_only).to_payload()["status"],
            "verified",
        )

        verified = complete_repair(plan, _verified_evidence()).to_payload()
        self.assertEqual(verified["status"], "verified")
        self.assertEqual(
            set(verified),
            {
                "application",
                "application_version",
                "diagnosis_category",
                "status",
                "completed_actions",
                "verification",
                "rollback_available",
                "receipt_sha256",
            },
        )
        for key in (
            "backup_created",
            "source_moved",
            "restart_observed",
            "ui_rendered",
            "health_check_passed",
        ):
            self.assertIs(verified["verification"][key], True)
        self.assertTrue(verified["rollback_available"])
        self.assertEqual(
            verified["completed_actions"],
            ["backup_move", "restart", "verify"],
        )
        self.assertEqual(len(verified["receipt_sha256"]), 64)

        private_evidence = _verified_evidence()
        private_evidence.update(
            {
                "raw_log": "access_token=must-not-enter-a-repair-receipt",
                "window_title": "Private account - Epic Games Launcher",
                "action_completed": True,
            }
        )
        bounded = complete_repair(plan, private_evidence).to_payload()
        self.assertNotIn("raw_log", bounded["verification"])
        self.assertNotIn("window_title", bounded["verification"])
        self.assertNotIn("action_completed", bounded["verification"])
        self.assertNotIn("must-not-enter", repr(bounded))

    def test_each_failure_family_requires_category_specific_success_evidence(self) -> None:
        cases = (
            (
                {"process_running": True, "network_reachable": False},
                "network_reachable",
            ),
            (
                {
                    "process_running": True,
                    "network_reachable": True,
                    "authentication_ok": False,
                },
                "authentication_succeeded",
            ),
            (
                {"process_running": False, "crash_count": 1},
                "process_healthy",
            ),
            (
                {"process_running": True, "update_required": True},
                "application_updated",
            ),
        )

        for diagnosis_evidence, required_key in cases:
            with self.subTest(required_key=required_key):
                diagnosis = classify_app_failure(diagnosis_evidence)
                plan = build_repair_plan(
                    {
                        "id": "example-application",
                        "name": "Example Application",
                        "version": "4.2.0",
                    },
                    diagnosis,
                    {},
                )
                evidence: dict[str, object] = {
                    "approval_authorized": True,
                    "restart_observed": True,
                    "ui_rendered": True,
                    "health_check_passed": True,
                    "process_healthy": True,
                    "observed_at": NOW.isoformat(),
                }
                evidence.pop(required_key, None)
                if diagnosis.category == "update":
                    evidence.pop("process_healthy")

                incomplete = complete_repair(plan, evidence).to_payload()
                self.assertNotEqual(incomplete["status"], "verified")

                evidence[required_key] = True
                if diagnosis.category == "update":
                    evidence["process_healthy"] = True
                verified = complete_repair(plan, evidence).to_payload()
                self.assertEqual(verified["status"], "verified")
                self.assertIs(verified["verification"][required_key], True)
                if diagnosis.category == "update":
                    self.assertIs(verified["verification"]["process_healthy"], True)

    def test_only_verified_outcomes_can_become_reusable_lessons(self) -> None:
        plan = _render_cache_plan()
        incomplete_evidence = _verified_evidence()
        incomplete_evidence["ui_rendered"] = False
        incomplete = complete_repair(plan, incomplete_evidence)

        with self.assertRaises(ValueError):
            build_verified_lesson(
                incomplete,
                application_version="18.8.0",
                now=NOW,
            )

        verified = complete_repair(plan, _verified_evidence())
        lesson = build_verified_lesson(
            verified,
            application_version="18.8.0",
            now=NOW,
        )
        payload = lesson.to_payload()
        self.assertEqual(
            set(payload),
            {
                "application",
                "application_version",
                "diagnosis_category",
                "repair_kinds",
                "verification_kinds",
                "observed_at",
                "valid_until",
                "outcome_sha256",
                "contradicted_by",
                "advisory_only",
            },
        )
        self.assertEqual(payload["application"], "epic-games-launcher")
        self.assertEqual(payload["application_version"], "18.8.0")
        self.assertEqual(payload["diagnosis_category"], "render_cache")
        self.assertEqual(
            payload["repair_kinds"], ["backup_move", "restart", "verify"]
        )
        self.assertTrue(payload["verification_kinds"])
        self.assertEqual(len(payload["outcome_sha256"]), 64)
        self.assertLess(payload["observed_at"], payload["valid_until"])
        self.assertEqual(payload["contradicted_by"], [])
        self.assertIs(payload["advisory_only"], True)
        serialized = repr(payload).casefold()
        for forbidden in (
            "approval_authorized",
            "bypass",
            "tool_grant",
            "capability_grant",
        ):
            self.assertNotIn(forbidden, serialized)

    def test_lessons_are_exact_application_version_and_expiry_bounded(self) -> None:
        outcome = complete_repair(_render_cache_plan(), _verified_evidence())
        lesson = build_verified_lesson(
            outcome,
            application_version="18.8.0",
            now=NOW,
        )
        valid_until = datetime.fromisoformat(lesson.to_payload()["valid_until"])

        self.assertTrue(
            lesson_is_applicable(
                lesson,
                "epic-games-launcher",
                "18.8.0",
                now=NOW + timedelta(seconds=1),
            )
        )
        self.assertFalse(
            lesson_is_applicable(
                lesson,
                "different-application",
                "18.8.0",
                now=NOW + timedelta(seconds=1),
            )
        )
        self.assertFalse(
            lesson_is_applicable(
                lesson,
                "epic-games-launcher",
                "19.0.0",
                now=NOW + timedelta(seconds=1),
            )
        )
        self.assertFalse(
            lesson_is_applicable(
                lesson,
                "epic-games-launcher",
                None,
                now=NOW + timedelta(seconds=1),
            )
        )
        self.assertFalse(
            lesson_is_applicable(
                lesson,
                "epic-games-launcher",
                "18.8.0",
                now=valid_until + timedelta(microseconds=1),
            )
        )
        self.assertFalse(
            lesson_is_applicable(
                replace(lesson, contradicted_by=("newer verified failure",)),
                "epic-games-launcher",
                "18.8.0",
                now=NOW + timedelta(seconds=1),
            )
        )
        self.assertFalse(
            lesson_is_applicable(
                replace(lesson, advisory_only=False),
                "epic-games-launcher",
                "18.8.0",
                now=NOW + timedelta(seconds=1),
            )
        )
        self.assertFalse(
            lesson_is_applicable(
                replace(lesson, outcome_sha256="z" * 64),
                "epic-games-launcher",
                "18.8.0",
                now=NOW + timedelta(seconds=1),
            )
        )

    def test_lesson_builder_rejects_forged_future_and_stale_outcomes(self) -> None:
        verified = complete_repair(_render_cache_plan(), _verified_evidence())
        with self.assertRaisesRegex(ValueError, "receipt"):
            build_verified_lesson(
                replace(verified, receipt_sha256="0" * 64),
                application_version="18.8.0",
                now=NOW,
            )

        future_evidence = _verified_evidence()
        future_evidence["observed_at"] = (NOW + timedelta(days=1)).isoformat()
        future = complete_repair(_render_cache_plan(), future_evidence)
        with self.assertRaisesRegex(ValueError, "future"):
            build_verified_lesson(
                future,
                application_version="18.8.0",
                now=NOW,
            )

        with self.assertRaisesRegex(ValueError, "too old"):
            build_verified_lesson(
                verified,
                application_version="18.8.0",
                now=NOW + timedelta(days=31),
            )


if __name__ == "__main__":
    unittest.main()
