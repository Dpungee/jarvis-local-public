"""Current ToolBox methods, mechanically extracted by domain.

Shared globals are resolved through jarvis.tools to preserve runtime patches.
"""
from __future__ import annotations


def _tools():
    from . import tools

    return tools


class ExternalServiceToolsMixin:
    def github_cli_status(self) -> dict[str, _tools().Any]:
        return self.github.cli_status().as_dict()

    def github_auth_status(self) -> dict[str, _tools().Any]:
        return self.github.auth_status().as_dict()

    def github_repository_status(self, path: str = ".") -> dict[str, _tools().Any]:
        return self.github.repository_status(path).as_dict()

    def github_list_repositories(
        self, owner: str | None = None, limit: int = 30
    ) -> dict[str, _tools().Any]:
        return self.github.list_repositories(owner, limit=limit).as_dict()

    def github_create_repository(
        self,
        path: str,
        name: str,
        visibility: str = "private",
        description: str = "",
        remote: str = "origin",
    ) -> dict[str, _tools().Any]:
        approved = self._approved_arguments_for("github_create_repository")
        expected_snapshot = (
            {
                key: approved[key]
                for key in ("resolved_path", "authenticated_login", "repository_slug")
            }
            if all(
                key in approved
                for key in ("resolved_path", "authenticated_login", "repository_slug")
            )
            else None
        )
        return self.github.create_repository(
            path,
            name,
            visibility=visibility,
            description=description,
            remote=remote,
            expected_approval_snapshot=expected_snapshot,
        ).as_dict()

    def github_push(
        self,
        path: str,
        branch: str,
        remote: str = "origin",
        set_upstream: bool = True,
    ) -> dict[str, _tools().Any]:
        approved = self._approved_arguments_for("github_push")
        return self.github.push(
            path,
            branch,
            remote=remote,
            set_upstream=set_upstream,
            expected_remote_url=approved.get("remote_url"),
            expected_tip_sha=approved.get("tip_sha"),
        ).as_dict()

    def google_drive_status(self) -> dict[str, _tools().Any]:
        if self.google_drive is None:
            return {"state": "disabled", "error": "Google Drive credential storage overlaps the workspace"}
        return self.google_drive.status()

    def google_workspace_status(self) -> dict[str, _tools().Any]:
        from .gateway.google_workspace import google_workspace_readiness

        installed = self.connectors.list_connectors()

        def configured(service: str) -> bool:
            for connector in installed:
                credential = connector.get("credential", {})
                if not isinstance(credential, dict) or not credential.get("configured"):
                    continue
                identity = " ".join((
                    str(connector.get("id", "")),
                    str(connector.get("name", "")),
                    str(connector.get("description", "")),
                )).casefold()
                actions = " ".join(
                    str(action.get("name", ""))
                    for action in connector.get("actions", [])
                    if isinstance(action, dict)
                ).casefold()
                if service == "gmail" and (
                    "gmail" in identity
                    or "google" in identity and any(
                        word in actions for word in ("email", "mail", "send_message")
                    )
                ):
                    return True
                if service == "calendar" and (
                    "calendar" in identity
                    or "google" in identity and any(
                        word in actions for word in ("calendar", "event")
                    )
                ):
                    return True
            return False

        return google_workspace_readiness(
            gmail_connected=configured("gmail"),
            calendar_connected=configured("calendar"),
            drive_status=self.google_drive_status(),
        )

    def prepare_email_draft(
        self,
        to: list[str],
        subject: str,
        body: str,
    ) -> dict[str, _tools().Any]:
        from .gateway.google_workspace import EmailDraft

        del self
        return EmailDraft.prepare(to, subject, body).review_manifest()

    def prepare_calendar_event(
        self,
        title: str,
        start: str,
        end: str,
        attendees: list[str] | None = None,
        description: str = "",
    ) -> dict[str, _tools().Any]:
        from .gateway.google_workspace import CalendarEventDraft

        del self
        return CalendarEventDraft.prepare(
            title,
            start,
            end,
            attendees=attendees or (),
            description=description,
        ).review_manifest()

    def google_drive_authenticate(self, open_browser: bool = True) -> dict[str, _tools().Any]:
        if self.google_drive is None:
            raise PermissionError("Google Drive is disabled for this workspace/data layout")
        return self.google_drive.authenticate(open_browser=open_browser)

    def google_drive_list_files(
        self,
        folder_id: str = "root",
        page_size: int = 50,
        page_token: str | None = None,
        include_trashed: bool = False,
    ) -> dict[str, _tools().Any]:
        if self.google_drive is None:
            raise PermissionError("Google Drive is disabled for this workspace/data layout")
        return self.google_drive.list_files(
            folder_id, page_size=page_size, page_token=page_token,
            include_trashed=include_trashed,
        )

    def google_drive_inventory(
        self,
        max_items: int = 500,
        include_trashed: bool = False,
    ) -> dict[str, _tools().Any]:
        if self.google_drive is None:
            raise PermissionError("Google Drive is disabled for this workspace/data layout")
        return self.google_drive.inventory(
            max_items=max_items,
            include_trashed=include_trashed,
        )

    def google_drive_create_folder(
        self, name: str, parent_id: str = "root"
    ) -> dict[str, _tools().Any]:
        if self.google_drive is None:
            raise PermissionError("Google Drive is disabled for this workspace/data layout")
        approved = self._approved_arguments_for("google_drive_create_folder")
        return self.google_drive.create_folder(
            name,
            parent_id,
            expected_account_permission_id=approved.get(
                "drive_account_permission_id"
            ),
            expected_parent_folder_id=approved.get("resolved_folder_id"),
        )

    def google_drive_upload_file(
        self,
        local_path: str,
        folder_id: str = "root",
        drive_name: str | None = None,
        mime_type: str | None = None,
    ) -> dict[str, _tools().Any]:
        if self.google_drive is None:
            raise PermissionError("Google Drive is disabled for this workspace/data layout")
        approved = self._approved_arguments_for("google_drive_upload_file")
        return self.google_drive.upload_file(
            local_path,
            folder_id=folder_id,
            drive_name=drive_name,
            mime_type=mime_type,
            expected_size_bytes=approved.get("local_size_bytes"),
            expected_sha256=approved.get("local_sha256"),
            expected_account_permission_id=approved.get(
                "drive_account_permission_id"
            ),
            expected_folder_id=approved.get("resolved_folder_id"),
        )

    def google_drive_download_file(
        self,
        file_id: str,
        local_path: str,
        overwrite: bool = False,
        export_mime_type: str | None = None,
    ) -> dict[str, _tools().Any]:
        if self.google_drive is None:
            raise PermissionError("Google Drive is disabled for this workspace/data layout")
        approved = self._approved_arguments_for("google_drive_download_file")
        expected = {
            "drive_account_permission_id": approved.get(
                "drive_account_permission_id"
            ),
            "download_item": approved.get("download_item"),
            "resolved_export_mime_type": approved.get(
                "resolved_export_mime_type"
            ),
        }
        return self.google_drive.download_file(
            file_id,
            local_path,
            overwrite=overwrite,
            export_mime_type=export_mime_type,
            expected_approval_snapshot=expected,
        )

    def google_drive_organize_files(
        self,
        operations: list[dict[str, _tools().Any]],
    ) -> dict[str, _tools().Any]:
        if self.google_drive is None:
            raise PermissionError("Google Drive is disabled for this workspace/data layout")
        approved = self._approved_arguments_for("google_drive_organize_files")
        expected = (
            {
                "drive_account_permission_id": approved.get(
                    "drive_account_permission_id"
                ),
                "organize_items": approved.get("organize_items"),
            }
            if approved
            else None
        )
        return self.google_drive.organize_files(
            operations,
            expected_approval_snapshot=expected,
        )

    def vercel_status(self) -> dict[str, _tools().Any]:
        return _tools().asdict(self.vercel.status())

    def vercel_list_projects(self) -> dict[str, _tools().Any]:
        return _tools().asdict(self.vercel.list_projects())

    def vercel_project_status(
        self, project_name: str | None = None, project_path: str | None = None
    ) -> dict[str, _tools().Any]:
        return _tools().asdict(self.vercel.project_status(project_name, project_path=project_path))

    def vercel_deploy(
        self,
        project_path: str | None = None,
        production: bool = False,
        target: str | None = None,
        prebuilt: bool = False,
        wait: bool = False,
    ) -> dict[str, _tools().Any]:
        approved = self._approved_arguments_for("vercel_deploy")
        snapshot_keys = (
            "resolved_project_path", "project_id", "org_id", "account_scope",
            "project_link_sha256", "prebuilt", "deploy_tree_sha256",
            "deploy_file_count", "deploy_total_bytes",
        )
        expected_snapshot = (
            {key: approved[key] for key in snapshot_keys}
            if all(key in approved for key in snapshot_keys)
            else None
        )
        return _tools().asdict(self.vercel.deploy(
            project_path, production=production, target=target,
            prebuilt=prebuilt, wait=wait,
            expected_approval_snapshot=expected_snapshot,
        ))

    def vercel_deployment_status(
        self, deployment: str, project_path: str | None = None
    ) -> dict[str, _tools().Any]:
        return _tools().asdict(self.vercel.deployment_status(deployment, project_path=project_path))

    def vercel_build_logs(
        self, deployment: str, project_path: str | None = None
    ) -> dict[str, _tools().Any]:
        return _tools().asdict(self.vercel.build_logs(deployment, project_path=project_path))

    def vercel_runtime_logs(
        self,
        deployment: str | None = None,
        project_name: str | None = None,
        project_path: str | None = None,
        limit: int = 100,
        since: str = "1h",
        level: str | None = None,
        environment: str | None = None,
    ) -> dict[str, _tools().Any]:
        return _tools().asdict(self.vercel.deployment_logs(
            deployment, project_name=project_name, project_path=project_path,
            limit=limit, since=since, level=level, environment=environment,
        ))

    def vercel_discover_databases(self) -> dict[str, _tools().Any]:
        return _tools().asdict(self.vercel.discover_database_integrations())

    def vercel_list_databases(
        self, project_name: str | None = None, project_path: str | None = None
    ) -> dict[str, _tools().Any]:
        return _tools().asdict(self.vercel.list_database_integrations(
            project_name, project_path=project_path
        ))

    def connector_list(self) -> list[dict[str, _tools().Any]]:
        return self.connectors.list_connectors()

    def connector_describe(self, connector: str) -> dict[str, _tools().Any]:
        return self.connectors.describe(connector)

    def connector_validate(self, path: str) -> dict[str, _tools().Any]:
        return self.connectors.validate_workspace_manifest(path)

    def connector_install(self, path: str) -> dict[str, _tools().Any]:
        approved = self._approved_arguments_for("connector_install")
        snapshot_keys = (
            "path", "id", "name", "version", "description", "actions",
            "credential_reference", "manifest_sha256", "valid",
        )
        expected = (
            {key: approved[key] for key in snapshot_keys}
            if all(key in approved for key in snapshot_keys)
            else None
        )
        return self.connectors.install(path, expected_snapshot=expected)

    def connector_call(
        self,
        connector: str,
        action: str,
        arguments: dict[str, _tools().Any],
    ) -> dict[str, _tools().Any]:
        approved = self._approved_arguments_for("connector_call")
        snapshot_keys = (
            "connector_id", "connector_name", "connector_version",
            "connector_manifest_sha256", "action", "action_description", "risk",
            "request_method", "request_url", "request_arguments_json",
            "credential_reference",
        )
        expected = (
            {key: approved[key] for key in snapshot_keys}
            if all(key in approved for key in snapshot_keys)
            else None
        )
        return self.connectors.call(
            connector,
            action,
            arguments,
            expected_snapshot=expected,
            transport=_tools()._fetch,
        )
