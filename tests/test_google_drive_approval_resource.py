from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path

from jarvis.approvals import approval_resource
from jarvis.presence import _safe_approval


class GoogleDriveDownloadApprovalResourceTests(unittest.TestCase):
    def _arguments(self) -> dict[str, object]:
        return {
            "file_id": "drive-file-123",
            "local_path": "downloads/report.pdf",
            "resolved_local_path": "C:/workspace/downloads/report.pdf",
            "overwrite": False,
            "export_mime_type": "application/pdf",
            "drive_account_permission_id": "private-account-binding",
            "download_item": {
                "id": "drive-file-123",
                "name": "Quarterly report.pdf",
                "mime_type": "application/vnd.google-apps.document",
                "is_folder": False,
                "trashed": False,
                "size": 12_345,
                "modified_time": "2026-08-28T12:34:56Z",
                "parents": ["private-parent-id"],
            },
            "resolved_export_mime_type": "application/pdf",
        }

    def test_resource_shows_exact_bounded_download_target_and_keeps_digest(self):
        arguments = self._arguments()
        resource = approval_resource("google_drive_download_file", arguments)
        payload = json.loads(resource)

        expected_digest = hashlib.sha256(
            json.dumps(
                arguments,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8", errors="replace")
        ).hexdigest()
        self.assertEqual(payload["arguments_sha256"], expected_digest)
        self.assertEqual(payload["arguments"], {
            "remote_file_id": "drive-file-123",
            "remote_name": "Quarterly report.pdf",
            "remote_mime_type": "application/vnd.google-apps.document",
            "remote_size_bytes": 12_345,
            "remote_modified_time": "2026-08-28T12:34:56Z",
            "remote_trashed": False,
            "remote_is_folder": False,
            "destination": "C:/workspace/downloads/report.pdf",
            "overwrite": False,
            "export_mime_type": "application/pdf",
        })
        self.assertNotIn("private-account-binding", resource)
        self.assertNotIn("private-parent-id", resource)

    def test_secret_shaped_visible_values_are_redacted_but_still_fingerprinted(self):
        arguments = self._arguments()
        secret = "sk-proj-" + "S" * 24
        arguments["download_item"] = {
            **arguments["download_item"],
            "name": f"report-{secret}.pdf",
        }
        resource = approval_resource("google_drive_download_file", arguments)
        payload = json.loads(resource)

        self.assertNotIn(secret, resource)
        self.assertIs(payload["arguments"]["remote_name"]["redacted"], True)
        changed = self._arguments()
        changed["download_item"] = {
            **changed["download_item"],
            "name": f"report-{secret[:-1]}T.pdf",
        }
        self.assertNotEqual(
            payload["arguments_sha256"],
            json.loads(approval_resource("google_drive_download_file", changed))[
                "arguments_sha256"
            ],
        )

    def test_malformed_or_hidden_remote_metadata_fails_closed(self):
        cases: list[dict[str, object]] = []
        missing = self._arguments()
        missing.pop("download_item")
        cases.append(missing)
        mismatched = self._arguments()
        mismatched["download_item"] = {
            **mismatched["download_item"], "id": "different-file",
        }
        cases.append(mismatched)
        hidden = self._arguments()
        hidden["download_item"] = {
            **hidden["download_item"], "raw_private_metadata": "not-visible",
        }
        cases.append(hidden)
        bad_state = self._arguments()
        bad_state["download_item"] = {
            **bad_state["download_item"], "trashed": "false",
        }
        cases.append(bad_state)
        oversized = self._arguments()
        oversized["download_item"] = {
            **oversized["download_item"], "name": "n" * 1_000,
        }
        oversized["resolved_local_path"] = "C:/" + ("d" * 997)
        cases.append(oversized)

        for arguments in cases:
            with self.subTest(arguments=arguments):
                with self.assertRaises(ValueError):
                    approval_resource("google_drive_download_file", arguments)

    def test_presence_preserves_resource_as_text_without_html_interpretation(self):
        arguments = self._arguments()
        arguments["download_item"] = {
            **arguments["download_item"],
            "name": '<img src=x onerror="alert(1)">.pdf',
        }
        resource = approval_resource("google_drive_download_file", arguments)
        safe = _safe_approval({"id": 7, "resource": resource})

        self.assertEqual(safe["resource"], resource)
        self.assertIn("<img src=x", json.loads(resource)["arguments"]["remote_name"])
        script = (
            Path(__file__).resolve().parents[1] / "jarvis" / "presence.js"
        ).read_text(encoding="utf-8")
        self.assertIn("resource.textContent = row.resource", script)
        self.assertNotIn("resource.innerHTML = row.resource", script)


if __name__ == "__main__":
    unittest.main()
