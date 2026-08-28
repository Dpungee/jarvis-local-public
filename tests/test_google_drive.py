from __future__ import annotations

import inspect
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from jarvis.google_drive import (
    APP_FILES_SCOPE,
    DRIVE_FOLDER_MIME_TYPE,
    FULL_DRIVE_SCOPE,
    GoogleDriveCredentialError,
    GoogleDriveAPIError,
    GoogleDriveDependencyError,
    GoogleDriveProvider,
    GoogleDriveTransferLimitError,
    GoogleDriveValidationError,
)


class FakeRequest:
    def __init__(self, response=None, *, error: Exception | None = None, payload=b""):
        self.response = response
        self.error = error
        self.payload = payload
        self.retries = None

    def execute(self, *, num_retries=0):
        self.retries = num_retries
        if self.error:
            raise self.error
        return self.response


class FakeFilesResource:
    def __init__(self):
        self.calls = []
        self.last_request = None
        self.list_response = {"files": []}
        self.folder_response = {
            "id": "folder123",
            "name": "Reports",
            "mimeType": DRIVE_FOLDER_MIME_TYPE,
            "parents": ["parent123"],
        }
        self.upload_response = {
            "id": "file123",
            "name": "report.txt",
            "mimeType": "text/plain",
            "size": "6",
            "parents": ["parent123"],
        }
        self.metadata_response = {
            "id": "file123",
            "name": "report.txt",
            "mimeType": "text/plain",
            "size": "7",
            "parents": ["parent123"],
        }
        self.download_payload = b"payload"
        self.destination_response = {
            "id": "folder123",
            "mimeType": DRIVE_FOLDER_MIME_TYPE,
        }

    def list(self, **kwargs):
        self.calls.append(("list", kwargs))
        self.last_request = FakeRequest(self.list_response)
        return self.last_request

    def create(self, **kwargs):
        self.calls.append(("create", kwargs))
        response = self.upload_response if "media_body" in kwargs else self.folder_response
        self.last_request = FakeRequest(response)
        return self.last_request

    def get(self, **kwargs):
        self.calls.append(("get", kwargs))
        response = (
            self.destination_response
            if kwargs.get("fields") == "id,mimeType"
            else self.metadata_response
        )
        self.last_request = FakeRequest(response)
        return self.last_request

    def get_media(self, **kwargs):
        self.calls.append(("get_media", kwargs))
        return FakeRequest(payload=self.download_payload)

    def export_media(self, **kwargs):
        self.calls.append(("export_media", kwargs))
        return FakeRequest(payload=self.download_payload)

    def update(self, **kwargs):
        self.calls.append(("update", kwargs))
        response = dict(self.metadata_response)
        response.update(kwargs.get("body") or {})
        if kwargs.get("addParents"):
            response["parents"] = [kwargs["addParents"]]
        self.last_request = FakeRequest(response)
        return self.last_request


class FakeService:
    def __init__(self):
        self.resource = FakeFilesResource()
        self.about_resource = SimpleNamespace(
            get=lambda **_kwargs: FakeRequest({
                "user": {"permissionId": "account123"}
            })
        )

    def files(self):
        return self.resource

    def about(self):
        return self.about_resource


class FakeMediaUpload:
    instances = []

    def __init__(self, stream, *, mimetype, chunksize, resumable):
        self.payload = stream.read()
        stream.seek(0)
        self.mimetype = mimetype
        self.chunksize = chunksize
        self.resumable = resumable
        self.__class__.instances.append(self)


class FakeMediaDownload:
    def __init__(self, stream, request, *, chunksize):
        self.stream = stream
        self.request = request
        self.chunksize = chunksize
        self.done = False

    def next_chunk(self, *, num_retries=0):
        if not self.done:
            self.stream.write(self.request.payload)
            self.done = True
        return None, self.done


class FakeCredentials:
    def __init__(
        self,
        *,
        valid=True,
        expired=False,
        refresh_token="refresh",
        scopes=(APP_FILES_SCOPE,),
    ):
        self.valid = valid
        self.expired = expired
        self.refresh_token = refresh_token
        self.scopes = scopes
        self.granted_scopes = None
        self.refresh_calls = 0

    def refresh(self, request):
        self.refresh_calls += 1
        self.valid = True
        self.expired = False

    def to_json(self):
        return json.dumps({"token": "fake-local-test-token"})


class FakeCredentialsType:
    loaded = None
    calls = []

    @classmethod
    def from_authorized_user_info(cls, info):
        cls.calls.append(info)
        if isinstance(cls.loaded, Exception):
            raise cls.loaded
        return cls.loaded


class FakeFlow:
    def __init__(self, credentials):
        self.credentials = credentials
        self.run_calls = []

    def run_local_server(self, **kwargs):
        self.run_calls.append(kwargs)
        return self.credentials


class FakeFlowType:
    flow = None
    calls = []

    @classmethod
    def from_client_config(cls, config, scopes):
        cls.calls.append((config, scopes))
        return cls.flow


def fake_dependencies(*, credentials=None, flow=None, service=None):
    FakeCredentialsType.loaded = credentials
    FakeCredentialsType.calls = []
    FakeFlowType.flow = flow
    FakeFlowType.calls = []
    built = []

    def build(*args, **kwargs):
        built.append((args, kwargs))
        return service or FakeService()

    return SimpleNamespace(
        credentials_type=FakeCredentialsType,
        request_type=lambda: object(),
        flow_type=FakeFlowType,
        build=build,
        media_upload_type=FakeMediaUpload,
        media_download_type=FakeMediaDownload,
        built=built,
    )


class GoogleDriveProviderTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="jarvis-drive-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.workspace = self.root / "workspace"
        self.credentials = self.root / "credentials"
        self.workspace.mkdir()
        FakeMediaUpload.instances = []

    def provider(self, **kwargs):
        return GoogleDriveProvider(
            self.workspace,
            credential_directory=self.credentials,
            **kwargs,
        )

    def test_status_is_local_and_does_not_load_dependencies_when_unconfigured(self):
        provider = self.provider()
        with patch(
            "jarvis.google_drive._google_dependencies",
            side_effect=AssertionError("dependencies should not load"),
        ):
            status = provider.auth_status()

        self.assertEqual(status["state"], "not_configured")
        self.assertFalse(status["authenticated"])
        self.assertFalse(status["client_configured"])
        self.assertFalse(status["token_present"])

    def test_status_explains_exact_setup_and_full_drive_visibility(self):
        provider = self.provider(access_mode="full")

        status = provider.status()

        self.assertEqual(status["state"], "not_configured")
        self.assertTrue(status["whole_drive_visible"])
        self.assertEqual(
            Path(status["client_secrets_path"]),
            self.credentials / "client_secret.json",
        )
        self.assertIn("google_drive_authenticate", status["next_action"])

    def test_status_reports_missing_optional_dependencies_without_secret_details(self):
        self.credentials.mkdir()
        (self.credentials / "token.json").write_text("{}", encoding="utf-8")
        provider = self.provider()
        with patch(
            "jarvis.google_drive._google_dependencies",
            side_effect=GoogleDriveDependencyError("install dependencies"),
        ):
            status = provider.status()
        self.assertEqual(status["state"], "dependencies_missing")

    def test_explicit_authentication_uses_desktop_loopback_and_saves_token(self):
        self.credentials.mkdir()
        (self.credentials / "client_secret.json").write_text(
            '{"installed":{"client_id":"fake.apps.googleusercontent.com"}}',
            encoding="utf-8",
        )
        credentials = FakeCredentials(valid=True)
        flow = FakeFlow(credentials)
        service = FakeService()
        dependencies = fake_dependencies(flow=flow, service=service)
        provider = self.provider()

        with (
            patch("jarvis.google_drive._google_dependencies", return_value=dependencies),
            patch.object(subprocess, "run") as subprocess_run,
        ):
            status = provider.authenticate()

        self.assertEqual(status["state"], "ready")
        self.assertTrue(status["authenticated"])
        self.assertEqual(len(flow.run_calls), 1)
        self.assertEqual(flow.run_calls[0]["host"], "127.0.0.1")
        self.assertEqual(flow.run_calls[0]["port"], 0)
        self.assertTrue(flow.run_calls[0]["open_browser"])
        self.assertEqual(flow.run_calls[0]["access_type"], "offline")
        self.assertEqual(flow.run_calls[0]["prompt"], "consent")
        self.assertIn("Never paste", flow.run_calls[0]["authorization_prompt_message"])
        self.assertEqual(json.loads((self.credentials / "token.json").read_text()), {
            "token": "fake-local-test-token"
        })
        self.assertEqual(dependencies.built[0][0], ("drive", "v3"))
        self.assertIsInstance(FakeFlowType.calls[0][0], dict)
        subprocess_run.assert_not_called()
        self.assertEqual(
            set(inspect.signature(GoogleDriveProvider.authenticate).parameters),
            {"self", "open_browser"},
        )

    def test_expired_local_token_refreshes_without_browser_flow(self):
        self.credentials.mkdir()
        (self.credentials / "token.json").write_text("{}", encoding="utf-8")
        credentials = FakeCredentials(valid=False, expired=True)
        dependencies = fake_dependencies(credentials=credentials, service=FakeService())
        provider = self.provider()

        with patch("jarvis.google_drive._google_dependencies", return_value=dependencies):
            status = provider.authenticate(open_browser=False)

        self.assertEqual(status["state"], "ready")
        self.assertEqual(credentials.refresh_calls, 1)
        self.assertEqual(FakeFlowType.calls, [])

    def test_token_scope_must_exactly_match_configured_access_mode(self):
        self.credentials.mkdir()
        (self.credentials / "token.json").write_text("{}", encoding="utf-8")
        credentials = FakeCredentials(scopes=(FULL_DRIVE_SCOPE,))
        dependencies = fake_dependencies(credentials=credentials)
        provider = self.provider(access_mode="app_files")

        with patch("jarvis.google_drive._google_dependencies", return_value=dependencies):
            self.assertEqual(provider.auth_status()["state"], "credentials_invalid")
            with self.assertRaises(GoogleDriveCredentialError):
                provider.authenticate()

    def test_browser_authorization_requires_persistable_refresh_token(self):
        self.credentials.mkdir()
        (self.credentials / "client_secret.json").write_text(
            '{"installed":{"client_id":"fake.apps.googleusercontent.com"}}',
            encoding="utf-8",
        )
        credentials = FakeCredentials(refresh_token=None)
        flow = FakeFlow(credentials)
        dependencies = fake_dependencies(flow=flow)
        provider = self.provider()

        with (
            patch("jarvis.google_drive._google_dependencies", return_value=dependencies),
            self.assertRaises(GoogleDriveCredentialError),
        ):
            provider.authenticate()

        self.assertFalse((self.credentials / "token.json").exists())

    def test_list_is_folder_scoped_paginated_and_bounded(self):
        service = FakeService()
        service.resource.list_response = {
            "files": [
                {
                    "id": "folder123",
                    "name": "Reports",
                    "mimeType": DRIVE_FOLDER_MIME_TYPE,
                    "modifiedTime": "2026-08-14T00:00:00Z",
                    "parents": ["parent123"],
                },
                {
                    "id": "file123",
                    "name": "report.txt",
                    "mimeType": "text/plain",
                    "size": "12",
                    "parents": ["folder123"],
                },
            ],
            "nextPageToken": "next_token",
        }
        dependencies = fake_dependencies(service=service)
        provider = self.provider(service=service)

        with patch("jarvis.google_drive._google_dependencies", return_value=dependencies):
            result = provider.list_files("folder123", page_size=999, page_token="page_token")

        self.assertEqual([item["id"] for item in result["items"]], ["folder123", "file123"])
        self.assertTrue(result["items"][0]["is_folder"])
        self.assertEqual(result["next_page_token"], "next_token")
        _, arguments = service.resource.calls[0]
        self.assertEqual(arguments["pageSize"], 100)
        self.assertEqual(arguments["pageToken"], "page_token")
        self.assertEqual(arguments["q"], "'folder123' in parents and trashed = false")
        self.assertEqual(service.resource.last_request.retries, 2)

    def test_inventory_is_account_wide_bounded_and_reports_scope(self):
        service = FakeService()
        service.resource.list_response = {
            "files": [{
                "id": "file123",
                "name": "report.txt",
                "mimeType": "text/plain",
                "size": "12",
                "parents": ["folder123"],
                "trashed": False,
            }]
        }
        dependencies = fake_dependencies(service=service)
        provider = self.provider(service=service, access_mode="full")

        with patch("jarvis.google_drive._google_dependencies", return_value=dependencies):
            result = provider.inventory(max_items=25)

        self.assertEqual(result["item_count"], 1)
        self.assertTrue(result["whole_drive_visible"])
        self.assertEqual(result["items"][0]["id"], "file123")
        _, arguments = service.resource.calls[0]
        self.assertEqual(arguments["q"], "trashed = false")
        self.assertEqual(arguments["pageSize"], 25)

    def test_organize_batch_binds_state_then_renames_moves_and_trashes(self):
        service = FakeService()
        dependencies = fake_dependencies(service=service)
        provider = self.provider(service=service, access_mode="full")
        operations = [{
            "file_id": "file123",
            "new_name": "Archived report.txt",
            "folder_id": "folder123",
            "trash": True,
        }]

        with patch("jarvis.google_drive._google_dependencies", return_value=dependencies):
            approved = provider.organize_approval_snapshot(operations)
            result = provider.organize_files(
                operations,
                expected_approval_snapshot=approved,
            )

        self.assertEqual(result["applied_count"], 1)
        self.assertTrue(result["recoverable"])
        update = [arguments for name, arguments in service.resource.calls if name == "update"]
        self.assertEqual(len(update), 1)
        self.assertEqual(update[0]["fileId"], "file123")
        self.assertEqual(update[0]["body"], {
            "name": "Archived report.txt",
            "trashed": True,
        })
        self.assertEqual(update[0]["addParents"], "folder123")
        self.assertEqual(update[0]["removeParents"], "parent123")
        self.assertEqual(service.resource.last_request.retries, 0)

    def test_organize_batch_rejects_changed_item_before_update(self):
        service = FakeService()
        dependencies = fake_dependencies(service=service)
        provider = self.provider(service=service, access_mode="full")
        operations = [{"file_id": "file123", "new_name": "Sorted.txt"}]
        with patch("jarvis.google_drive._google_dependencies", return_value=dependencies):
            approved = provider.organize_approval_snapshot(operations)
            service.resource.metadata_response["name"] = "changed.txt"
            with self.assertRaisesRegex(PermissionError, "changed after approval"):
                provider.organize_files(
                    operations,
                    expected_approval_snapshot=approved,
                )

        self.assertFalse(any(name == "update" for name, _ in service.resource.calls))

    def test_create_folder_validates_name_and_parent(self):
        service = FakeService()
        dependencies = fake_dependencies(service=service)
        provider = self.provider(service=service)
        with patch("jarvis.google_drive._google_dependencies", return_value=dependencies):
            result = provider.create_folder("Reports", parent_id="root")
            with self.assertRaises(GoogleDriveValidationError):
                provider.create_folder("../escape", parent_id="root")

        self.assertEqual(result["id"], "folder123")
        _, arguments = service.resource.calls[0]
        self.assertEqual(arguments["body"], {
            "name": "Reports",
            "mimeType": DRIVE_FOLDER_MIME_TYPE,
            "parents": ["root"],
        })
        self.assertEqual(service.resource.calls[0][0], "create")
        self.assertEqual(service.resource.last_request.retries, 0)

    def test_drive_destination_snapshot_binds_account_and_resolved_folder(self):
        service = FakeService()
        dependencies = fake_dependencies(service=service)
        provider = self.provider(service=service)

        with patch("jarvis.google_drive._google_dependencies", return_value=dependencies):
            snapshot = provider.approval_destination_snapshot("root")

        self.assertEqual(snapshot, {
            "drive_account_permission_id": "account123",
            "resolved_folder_id": "folder123",
        })
        self.assertEqual(service.resource.calls[0][1]["fileId"], "root")

    def test_create_folder_executes_resolved_parent_and_rejects_account_mismatch(self):
        service = FakeService()
        dependencies = fake_dependencies(service=service)
        provider = self.provider(service=service)
        with patch("jarvis.google_drive._google_dependencies", return_value=dependencies):
            approved = provider.approval_destination_snapshot("root")
            created = provider.create_folder(
                "Reports",
                "root",
                expected_account_permission_id=approved[
                    "drive_account_permission_id"
                ],
                expected_parent_folder_id=approved["resolved_folder_id"],
            )

        self.assertEqual(created["id"], "folder123")
        create_calls = [arguments for name, arguments in service.resource.calls if name == "create"]
        self.assertEqual(create_calls[-1]["body"]["parents"], ["folder123"])

        service.about_resource.get = lambda **_kwargs: FakeRequest({
            "user": {"permissionId": "differentAccount"}
        })
        with (
            patch("jarvis.google_drive._google_dependencies", return_value=dependencies),
            self.assertRaisesRegex(PermissionError, "changed after approval"),
        ):
            provider.create_folder(
                "Blocked",
                "root",
                expected_account_permission_id=approved[
                    "drive_account_permission_id"
                ],
                expected_parent_folder_id=approved["resolved_folder_id"],
            )
        self.assertEqual(
            len([name for name, _ in service.resource.calls if name == "create"]),
            1,
        )

    def test_credentials_and_workspace_must_be_disjoint(self):
        with self.assertRaises(GoogleDriveValidationError):
            GoogleDriveProvider(
                self.workspace,
                credential_directory=self.workspace / "credentials",
            )
        with self.assertRaises(GoogleDriveValidationError):
            GoogleDriveProvider(
                self.workspace,
                credential_directory=self.workspace.parent,
            )

    def test_linked_credential_directory_is_not_resolved_away(self):
        real_credentials = self.root / "real-credentials"
        real_credentials.mkdir()
        linked_credentials = self.root / "linked-credentials"
        try:
            linked_credentials.symlink_to(real_credentials, target_is_directory=True)
        except OSError:
            self.skipTest("directory symlinks are unavailable")
        provider = GoogleDriveProvider(
            self.workspace,
            credential_directory=linked_credentials,
        )

        self.assertEqual(provider.auth_status()["state"], "configuration_invalid")

    def test_upload_uses_workspace_file_handle_and_enforces_size(self):
        source = self.workspace / "report.txt"
        source.write_bytes(b"report")
        service = FakeService()
        dependencies = fake_dependencies(service=service)
        provider = self.provider(service=service, max_transfer_bytes=6)

        with patch("jarvis.google_drive._google_dependencies", return_value=dependencies):
            result = provider.upload_file("report.txt", folder_id="parent123")

        self.assertEqual(result["id"], "file123")
        self.assertEqual(FakeMediaUpload.instances[0].payload, b"report")
        self.assertEqual(FakeMediaUpload.instances[0].mimetype, "text/plain")
        _, arguments = service.resource.calls[0]
        self.assertEqual(arguments["body"], {
            "name": "report.txt",
            "parents": ["parent123"],
        })
        self.assertEqual(service.resource.last_request.retries, 0)
        source.write_bytes(b"too large")
        with self.assertRaises(GoogleDriveTransferLimitError):
            provider.upload_file("report.txt")

    def test_upload_approval_snapshot_is_bounded_before_open_or_hash(self):
        source = self.workspace / "oversized.bin"
        source.write_bytes(b"1234567")
        provider = self.provider(service=FakeService(), max_transfer_bytes=6)

        with (
            patch("jarvis.google_drive.os.open") as opened,
            patch("jarvis.google_drive.hashlib.sha256") as sha256,
            self.assertRaises(GoogleDriveTransferLimitError),
        ):
            provider.upload_approval_snapshot("oversized.bin")

        opened.assert_not_called()
        sha256.assert_not_called()

    def test_upload_rechecks_approved_size_and_hash_before_remote_request(self):
        source = self.workspace / "report.txt"
        source.write_bytes(b"approved")
        service = FakeService()
        dependencies = fake_dependencies(service=service)
        provider = self.provider(service=service)
        with patch("jarvis.google_drive._google_dependencies", return_value=dependencies):
            approved = provider.upload_approval_snapshot("report.txt")
        source.write_bytes(b"tampered")

        with (
            patch("jarvis.google_drive._google_dependencies", return_value=dependencies),
            self.assertRaisesRegex(GoogleDriveValidationError, "approved bytes"),
        ):
            provider.upload_file(
                "report.txt",
                expected_size_bytes=approved["local_size_bytes"],
                expected_sha256=approved["local_sha256"],
                expected_account_permission_id=approved[
                    "drive_account_permission_id"
                ],
                expected_folder_id=approved["resolved_folder_id"],
            )

        self.assertFalse(any(name == "create" for name, _ in service.resource.calls))

    def test_upload_and_download_reject_workspace_escape(self):
        outside = self.root / "outside.txt"
        outside.write_text("private", encoding="utf-8")
        provider = self.provider(service=FakeService())

        with self.assertRaises(GoogleDriveValidationError):
            provider.upload_file(outside)
        with self.assertRaises(GoogleDriveValidationError):
            provider.download_file("file123", outside)

    def test_workspace_link_components_are_rejected_when_supported(self):
        real_directory = self.workspace / "real"
        real_directory.mkdir()
        (real_directory / "source.txt").write_text("data", encoding="utf-8")
        linked_directory = self.workspace / "linked"
        try:
            linked_directory.symlink_to(real_directory, target_is_directory=True)
        except OSError:
            self.skipTest("directory symlinks are unavailable")
        provider = self.provider(service=FakeService())

        with self.assertRaises(GoogleDriveValidationError):
            provider.upload_file("linked/source.txt")
        with self.assertRaises(GoogleDriveValidationError):
            provider.download_file("file123", "linked/output.txt")

    def test_download_is_atomic_bounded_and_does_not_overwrite_by_default(self):
        service = FakeService()
        dependencies = fake_dependencies(service=service)
        provider = self.provider(service=service, max_transfer_bytes=7)

        with patch("jarvis.google_drive._google_dependencies", return_value=dependencies):
            result = provider.download_file("file123", "downloads/report.txt")

        destination = self.workspace / "downloads" / "report.txt"
        self.assertEqual(destination.read_bytes(), b"payload")
        self.assertEqual(result["bytes_written"], 7)
        self.assertEqual(Path(result["local_path"]), destination)
        self.assertEqual([call[0] for call in service.resource.calls], ["get", "get_media"])
        with self.assertRaises(GoogleDriveValidationError):
            provider.download_file("file123", "downloads/report.txt")

    def test_streaming_download_limit_removes_partial_file(self):
        service = FakeService()
        service.resource.metadata_response.pop("size")
        service.resource.download_payload = b"12345"
        dependencies = fake_dependencies(service=service)
        provider = self.provider(service=service, max_transfer_bytes=4)
        destination = self.workspace / "too-large.bin"

        with (
            patch("jarvis.google_drive._google_dependencies", return_value=dependencies),
            self.assertRaises(GoogleDriveTransferLimitError),
        ):
            provider.download_file("file123", "too-large.bin")

        self.assertFalse(destination.exists())
        self.assertEqual(list(self.workspace.glob("*.part")), [])

    def test_google_native_download_requires_explicit_supported_export(self):
        service = FakeService()
        service.resource.metadata_response.update({
            "name": "Plan",
            "mimeType": "application/vnd.google-apps.document",
        })
        service.resource.metadata_response.pop("size")
        dependencies = fake_dependencies(service=service)
        provider = self.provider(service=service)

        with patch("jarvis.google_drive._google_dependencies", return_value=dependencies):
            with self.assertRaises(GoogleDriveValidationError):
                provider.download_file("file123", "plan.pdf")
            result = provider.download_file(
                "file123", "plan.pdf", export_mime_type="application/pdf"
            )

        self.assertEqual(result["bytes_written"], 7)
        self.assertEqual(service.resource.calls[-1], (
            "export_media",
            {"fileId": "file123", "mimeType": "application/pdf"},
        ))

    def test_remote_error_message_is_sanitized(self):
        with self.assertRaises(GoogleDriveAPIError) as caught:
            GoogleDriveProvider._execute(
                FakeRequest(error=RuntimeError("secret-token-value")),
                "list",
            )
        self.assertNotIn("secret-token-value", str(caught.exception))
        self.assertIn("RuntimeError", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
