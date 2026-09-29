from __future__ import annotations

import copy
import unittest

from jarvis.verified_routines import (
    VerifiedRoutineError,
    validate_verified_routine,
    verified_routine_receipt,
)


class VerifiedRoutineTests(unittest.TestCase):
    @staticmethod
    def _routine() -> dict:
        return {
            "schema_version": 1,
            "routine_id": "save-local-note",
            "start_state": "editor-ready",
            "source": {"suite_id": "desktop-synthetic-v1", "case_id": "save-local-note", "environment_fingerprint": "a" * 64},
            "verification": {"independent_replay_passed": True, "success_count": 2, "failure_count": 0},
            "states": [
                {"state_id": "editor-ready", "expected_signals": ["window_visible"], "action_kind": "hotkey", "action_ref": "save-command", "next_state": "document-saved"},
                {"state_id": "document-saved", "expected_signals": ["document_saved"], "action_kind": "wait", "action_ref": "confirm-save", "next_state": "complete"},
            ],
            "operator_approval_required": True,
        }

    def test_validated_routine_is_non_executable_and_deterministic(self) -> None:
        routine = validate_verified_routine(self._routine())
        receipt = verified_routine_receipt(self._routine())
        self.assertEqual(routine["routine_id"], "save-local-note")
        self.assertTrue(receipt["operator_approval_required"])
        self.assertEqual(len(receipt["routine_checksum_sha256"]), 64)
        self.assertNotIn("coordinate", repr(receipt).casefold())
        self.assertNotIn("screenshot", repr(receipt).casefold())

    def test_rejects_unverified_or_autoapproved_routines(self) -> None:
        routine = copy.deepcopy(self._routine())
        routine["verification"]["independent_replay_passed"] = False
        with self.assertRaisesRegex(VerifiedRoutineError, "independent"):
            validate_verified_routine(routine)
        routine = copy.deepcopy(self._routine())
        routine["operator_approval_required"] = False
        with self.assertRaisesRegex(VerifiedRoutineError, "approval"):
            validate_verified_routine(routine)

    def test_rejects_failed_replays_and_unknown_transitions(self) -> None:
        routine = copy.deepcopy(self._routine())
        routine["verification"]["failure_count"] = 1
        with self.assertRaisesRegex(VerifiedRoutineError, "failed"):
            validate_verified_routine(routine)
        routine = copy.deepcopy(self._routine())
        routine["states"][0]["next_state"] = "not-a-real-state"
        with self.assertRaisesRegex(VerifiedRoutineError, "transition"):
            validate_verified_routine(routine)

    def test_rejects_cycles_unreachable_states_and_unknown_start(self) -> None:
        routine = copy.deepcopy(self._routine())
        routine["start_state"] = "missing"
        with self.assertRaisesRegex(VerifiedRoutineError, "start"):
            validate_verified_routine(routine)

        routine = copy.deepcopy(self._routine())
        routine["states"][1]["next_state"] = "editor-ready"
        with self.assertRaisesRegex(VerifiedRoutineError, "cycle"):
            validate_verified_routine(routine)

        routine = copy.deepcopy(self._routine())
        routine["states"].append({
            "state_id": "orphan",
            "expected_signals": ["orphan.visible"],
            "action_kind": "wait",
            "action_ref": "wait.short",
            "next_state": "complete",
        })
        with self.assertRaisesRegex(VerifiedRoutineError, "unreachable"):
            validate_verified_routine(routine)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
