from __future__ import annotations

import copy
import unittest

from jarvis.computer_use_benchmark import (
    ComputerUseBenchmarkError,
    score_computer_use_suite,
    validate_computer_use_suite,
)


class ComputerUseBenchmarkTests(unittest.TestCase):
    @staticmethod
    def _suite() -> dict:
        return {
            "schema_version": 1,
            "suite_id": "desktop-synthetic-v1",
            "environment": {"kind": "isolated_vm", "network": "disabled", "accounts": "synthetic"},
            "cases": [
                {"case_id": "save-local-note", "task_kind": "local_document", "required_evidence": ["window_visible", "document_saved"]},
                {"case_id": "navigate-local-browser", "task_kind": "browser_fixture", "required_evidence": ["fixture_loaded", "target_visible"]},
            ],
        }

    def test_validates_offline_synthetic_suite_and_scores_receipts(self) -> None:
        suite = validate_computer_use_suite(self._suite())
        receipt = score_computer_use_suite(suite, [
            {"case_id": "save-local-note", "status": "succeeded", "evidence": ["window_visible", "document_saved"]},
            {"case_id": "navigate-local-browser", "status": "succeeded", "evidence": ["fixture_loaded", "target_visible"]},
        ])
        self.assertEqual(receipt["passed"], 2)
        self.assertEqual(receipt["pass_rate"], 1.0)
        self.assertEqual(len(receipt["receipt_checksum_sha256"]), 64)

    def test_missing_evidence_fails_case_without_exposing_screen_content(self) -> None:
        receipt = score_computer_use_suite(self._suite(), [
            {"case_id": "save-local-note", "status": "succeeded", "evidence": ["window_visible"]},
            {"case_id": "navigate-local-browser", "status": "failed", "evidence": ["fixture_loaded"]},
        ])
        self.assertEqual(receipt["passed"], 0)
        saved_note = next(row for row in receipt["results"] if row["case_id"] == "save-local-note")
        self.assertEqual(saved_note["missing_evidence"], ["document_saved"])
        self.assertNotIn("screenshot", repr(receipt).casefold())

    def test_rejects_live_network_accounts_and_free_form_task_text(self) -> None:
        suite = copy.deepcopy(self._suite())
        suite["environment"]["network"] = "enabled"
        with self.assertRaisesRegex(ComputerUseBenchmarkError, "isolated VM"):
            validate_computer_use_suite(suite)
        suite = copy.deepcopy(self._suite())
        suite["cases"][0]["operator_prompt"] = "open my real email"
        with self.assertRaisesRegex(ComputerUseBenchmarkError, "fields"):
            validate_computer_use_suite(suite)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
