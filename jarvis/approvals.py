from __future__ import annotations

import hashlib
import json
from typing import Any

from .redaction import contains_secret, is_sensitive_key


SENSITIVE_ACTIONS: dict[str, tuple[str, str]] = {
    "computer_list_files": ("access_private_files", "This inspects files outside the designated workspace."),
    "computer_read_file": ("access_private_files", "This reads a file outside the designated workspace."),
    "computer_search_files": ("access_private_files", "This searches files outside the designated workspace."),
    "computer_storage_report": ("access_private_files", "This inspects file and folder sizes outside the designated workspace."),
    "computer_write_file": ("change_outside_workspace", "This changes a file outside the designated workspace."),
    "windows_launch_app": ("control_desktop_application", "This launches the exact installed desktop application shown."),
    "windows_app_repair": ("change_outside_workspace", "This gracefully closes the exact application, moves only its declared disposable renderer caches to the shown reversible backup, and restarts it."),
    "windows_open_url": ("control_desktop_application", "This opens the exact public URL shown in the default browser."),
    "desktop_active_window": ("access_private_screen", "This reads the title, application, and bounds of the current foreground window."),
    "desktop_interact": ("control_desktop_application", "This sends the exact bounded keyboard or mouse action batch shown to the verified foreground window."),
    "photoshop_remove_background": ("change_outside_workspace", "This opens the exact source image in Photoshop and writes the shown PNG output."),
    "home_device_control": ("control_home_device", "This sends the exact shown action to the paired and allowlisted home device."),
    "github_create_repository": ("publish_external", "This creates an externally visible account resource."),
    "github_push": ("publish_external", "This publishes local commits to an external service."),
    "google_drive_authenticate": ("access_credentials", "This starts an account authorization flow."),
    "google_drive_create_folder": ("communicate_external", "This changes an external account."),
    "google_drive_upload_file": ("expose_private_information", "This uploads a local file to an external service."),
    "google_drive_download_file": ("communicate_external", "This transfers external account data into the workspace."),
    "google_drive_organize_files": ("communicate_external", "This renames, moves, or recoverably trashes the exact Google Drive items shown."),
    "vercel_deploy": ("publish_external", "This publishes a deployment to an external service."),
    "connector_install": ("extend_capability", "This installs the exact declarative connector shown and grants Jarvis a new bounded API surface."),
    "connector_call": ("communicate_external", "This sends the exact shown request through an operator-installed external connector."),
    "feature_setup_decide": ("extend_capability", "This changes the exact optional Jarvis capability shown. It never downloads a tool, runs a probe, or authorizes containment."),
}


def _digest_descriptor(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        encoded = value.encode("utf-8", errors="replace")
    else:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8", errors="replace")
    return {
        "redacted": True,
        "bytes": len(encoded),
        "sha256": hashlib.sha256(encoded).hexdigest(),
    }


def _safe_argument_value(value: Any) -> Any:
    if isinstance(value, str):
        if contains_secret(value):
            return _digest_descriptor(value)
        if len(value) <= 160:
            return value
        encoded = value.encode("utf-8", errors="replace")
        return {
            "prefix": value[:160],
            "characters": len(value),
            "sha256": hashlib.sha256(encoded).hexdigest(),
        }
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    if isinstance(value, list):
        return {
            "items": len(value),
            "preview": [_safe_argument_value(item) for item in value[:5]],
            "sha256": _digest_descriptor(value)["sha256"],
        }
    return {
        "redacted": True,
        "type": type(value).__name__,
        "sha256": _digest_descriptor(value)["sha256"],
    }


def _application_repair_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    """Keep the complete bounded repair plan human-readable or fail closed."""
    safe: dict[str, Any] = {}
    scalar_keys = {
        "application",
        "plan_id",
        "symptom",
        "repair_target",
        "repair_operation",
        "repair_directories",
        "repair_bytes",
        "repair_reversible",
        "repair_plan_sha256",
    }
    move_keys = sorted(
        key for key in arguments
        if key.startswith("repair_move_") and key[12:].isdigit()
    )
    for key in sorted(scalar_keys):
        if key in arguments:
            safe[key] = _safe_argument_value(arguments[key])
    for key in move_keys:
        value = arguments[key]
        if (
            not isinstance(value, str)
            or not value
            or len(value) > 500
            or any(character in value for character in "\x00\r\n")
            or contains_secret(value)
        ):
            raise ValueError("Application repair approval target is not display-safe")
        safe[key] = value
    if "repair_plan" in arguments:
        safe["repair_plan"] = _digest_descriptor(arguments["repair_plan"])
    return safe


def approval_resource(tool_name: str, arguments: dict[str, Any]) -> str:
    """Describe a sensitive action without persisting file contents or secrets."""
    canonical_arguments = json.dumps(
        arguments,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8", errors="replace")
    safe: dict[str, Any] = {}
    if tool_name == "windows_app_repair":
        safe = _application_repair_arguments(arguments)
    else:
        for key, value in sorted(arguments.items()):
            if is_sensitive_key(key):
                safe[key] = _digest_descriptor(value)
            elif key.casefold() in {"content", "old_text", "new_text"}:
                safe[key] = _digest_descriptor(str(value))
            else:
                safe[key] = _safe_argument_value(value)
    payload = {
        "tool": tool_name,
        "arguments_sha256": hashlib.sha256(canonical_arguments).hexdigest(),
        "arguments": safe,
    }
    serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if len(serialized) > 1_900:
        if tool_name == "windows_app_repair":
            raise ValueError("Application repair approval summary is too large")
        payload["arguments"] = {
            key: value if isinstance(value, (bool, int, float)) or value is None
            else _digest_descriptor(value)
            for key, value in sorted(arguments.items())
        }
        serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return serialized
