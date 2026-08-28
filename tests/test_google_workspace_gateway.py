from __future__ import annotations

import unittest

from jarvis.gateway.google_workspace import (
    CalendarEventDraft,
    EmailDraft,
    google_workspace_readiness,
)


class GoogleWorkspaceGatewayTests(unittest.TestCase):
    def test_email_draft_is_bounded_deduplicated_and_reviewable_without_sending(self):
        draft = EmailDraft.prepare(
            ["User@Example.com", "user@example.com"], "Status", "Ready for review."
        )
        manifest = draft.review_manifest()
        self.assertEqual(manifest["to"], ("user@example.com",))
        self.assertFalse(manifest["external_mutation"])
        self.assertTrue(manifest["execution_requires_approval"])
        self.assertEqual(manifest["kind"], "gmail_draft")
        with self.assertRaisesRegex(ValueError, "invalid"):
            EmailDraft.prepare(["not-an-address"], "Status", "Body")

    def test_calendar_draft_requires_timezone_and_forward_duration(self):
        draft = CalendarEventDraft.prepare(
            "Review",
            "2026-08-27T10:00:00-04:00",
            "2026-08-27T10:30:00-04:00",
            attendees=["owner@example.com"],
        )
        self.assertEqual(draft.review_manifest()["kind"], "calendar_event_draft")
        with self.assertRaisesRegex(ValueError, "timezone"):
            CalendarEventDraft.prepare(
                "Review", "2026-08-27T10:00:00", "2026-08-27T10:30:00"
            )
        with self.assertRaisesRegex(ValueError, "end after"):
            CalendarEventDraft.prepare(
                "Review",
                "2026-08-27T11:00:00-04:00",
                "2026-08-27T10:30:00-04:00",
            )

    def test_readiness_is_credential_free_and_keeps_mutations_approval_gated(self):
        status = google_workspace_readiness(
            gmail_connected=True,
            calendar_connected=False,
            drive_status={"authenticated": True, "access_mode": "full"},
        )
        self.assertFalse(status["all_connected"])
        self.assertEqual(status["drive"]["access_mode"], "full")
        self.assertIn("send_email", status["gmail"]["requires_approval"])
        self.assertIn("create_event", status["calendar"]["requires_approval"])
        self.assertNotIn("token", repr(status).casefold())


if __name__ == "__main__":
    unittest.main()
