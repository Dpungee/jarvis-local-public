from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from .redaction import contains_secret, is_sensitive_key


SENSITIVE_ACTIONS: dict[str, tuple[str, str]] = {
    "computer_list_files": ("access_private_files", "This inspects files outside the designated workspace."),
    "computer_read_file": ("access_private_files", "This reads a file outside the designated workspace."),
    "computer_search_files": ("access_private_files", "This searches files outside the designated workspace."),
    "computer_storage_report": ("access_private_files", "This inspects file and folder sizes outside the designated workspace."),
    "computer_write_file": ("change_outside_workspace", "This changes a file outside the designated workspace."),
    "install_project_dependencies": ("install_dependencies", "This performs a dependency installation with network access on the trusted host. Approval shows exact manifest and executor fingerprints plus a bounded summary of direct package requests; package installation can execute third-party build code with the current user account authority."),
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


def _desktop_interact_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    """Keep every bounded desktop action visible to the approving operator."""
    actions = arguments.get("actions")
    if not isinstance(actions, list) or not 1 <= len(actions) <= 12:
        raise ValueError("Desktop approval must contain 1-12 actions")
    safe: dict[str, Any] = {
        "action_count": len(actions),
    }
    expected = arguments.get("expected_context_sha256")
    if expected is not None:
        safe["expected_context_sha256"] = _safe_argument_value(expected)
    foreground = arguments.get("foreground")
    if isinstance(foreground, dict):
        for key in (
            "application", "title", "left", "top", "right", "bottom",
            "width", "height", "context_sha256",
        ):
            if key in foreground:
                safe[f"foreground_{key}"] = _safe_argument_value(foreground[key])
    for index, action in enumerate(actions, start=1):
        if not isinstance(action, dict):
            raise ValueError("Every desktop approval action must be an object")
        rendered = json.dumps(
            action,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        if len(rendered) > 2_500 or contains_secret(rendered):
            raise ValueError("Desktop approval action is not display-safe")
        safe[f"action_{index:02d}"] = rendered
    return safe


def _google_drive_download_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    """Render the exact bounded Drive download target or fail closed."""
    item = arguments.get("download_item")
    if not isinstance(item, dict):
        raise ValueError("Google Drive download approval metadata is unavailable")
    allowed_item_keys = {
        "id", "name", "mime_type", "is_folder", "trashed", "size",
        "modified_time", "parents",
    }
    if set(item) - allowed_item_keys:
        raise ValueError("Google Drive download approval metadata is not bounded")

    def bounded_text(value: Any, label: str, maximum: int) -> str:
        if (
            not isinstance(value, str)
            or not value.strip()
            or len(value) > maximum
            or any(character in value for character in "\x00\r\n")
        ):
            raise ValueError(f"Google Drive download {label} is not display-safe")
        return value

    def visible_text(value: str) -> Any:
        # Unlike generic arguments, approval targets are either shown exactly
        # or rejected by the final resource-size bound. Secret-shaped values
        # remain bound by digest without being disclosed.
        return _digest_descriptor(value) if contains_secret(value) else value

    remote_id = bounded_text(item.get("id"), "file ID", 500)
    requested_id = bounded_text(arguments.get("file_id"), "requested file ID", 500)
    if remote_id != requested_id:
        raise ValueError("Google Drive download file ID does not match its snapshot")
    name = bounded_text(item.get("name"), "item name", 1_000)
    mime_type = bounded_text(item.get("mime_type"), "MIME type", 255)
    destination = bounded_text(
        arguments.get("resolved_local_path"), "destination", 1_000
    )
    if not isinstance(item.get("is_folder"), bool):
        raise ValueError("Google Drive download folder state is unavailable")
    if not isinstance(item.get("trashed"), bool):
        raise ValueError("Google Drive download trash state is unavailable")
    if not isinstance(arguments.get("overwrite"), bool):
        raise ValueError("Google Drive download overwrite state is unavailable")

    size = item.get("size")
    if size is not None and (
        isinstance(size, bool) or not isinstance(size, int) or size < 0
    ):
        raise ValueError("Google Drive download size is invalid")
    modified_time = item.get("modified_time")
    if modified_time is not None:
        modified_time = bounded_text(modified_time, "modified time", 100)
    export_mime_type = arguments.get("resolved_export_mime_type")
    if export_mime_type is not None:
        export_mime_type = bounded_text(export_mime_type, "export MIME type", 255)

    return {
        "remote_file_id": visible_text(remote_id),
        "remote_name": visible_text(name),
        "remote_mime_type": visible_text(mime_type),
        "remote_size_bytes": size,
        "remote_modified_time": (
            visible_text(modified_time) if modified_time is not None else None
        ),
        "remote_trashed": item["trashed"],
        "remote_is_folder": item["is_folder"],
        "destination": visible_text(destination),
        "overwrite": arguments["overwrite"],
        "export_mime_type": (
            visible_text(export_mime_type)
            if export_mime_type is not None else None
        ),
    }


_DEPENDENCY_MANIFEST_NAMES = frozenset({
    "requirements.lock",
    "requirements.txt",
    "npm-shrinkwrap.json",
    "package-lock.json",
    "package.json",
})
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")


def _dependency_display_text(value: Any, label: str) -> str | dict[str, Any]:
    """Keep a path/declaration recognizable without allowing unbounded UI data."""
    if (
        not isinstance(value, str)
        or not value
        or any(character in value for character in "\x00\r\n")
        or contains_secret(value)
    ):
        raise ValueError(f"Dependency approval {label} is not display-safe")
    if len(value) <= 240:
        return value
    return {
        "prefix": value[:80],
        "suffix": value[-40:],
        "characters": len(value),
    }


def _dependency_install_display_resource(
    arguments: dict[str, Any],
    exact_resource: str,
) -> str:
    """Render a complete, bounded operator view without changing authorization."""
    try:
        canonical = json.loads(exact_resource)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("Dependency approval authorization resource is invalid") from exc
    if (
        not isinstance(canonical, dict)
        or canonical.get("tool") != "install_project_dependencies"
        or not isinstance(canonical.get("arguments_sha256"), str)
        or _SHA256_PATTERN.fullmatch(canonical["arguments_sha256"]) is None
    ):
        raise ValueError("Dependency approval authorization resource is incomplete")
    rendered_arguments = json.dumps(
        arguments,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8", errors="replace")
    if hashlib.sha256(rendered_arguments).hexdigest() != canonical["arguments_sha256"]:
        raise ValueError("Dependency approval arguments do not match authorization")

    def bounded_count(key: str, maximum: int) -> int:
        value = arguments.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
            raise ValueError(f"Dependency approval {key} is invalid")
        return value

    def exact_sha(key: str) -> str:
        value = arguments.get(key)
        if not isinstance(value, str) or _SHA256_PATTERN.fullmatch(value) is None:
            raise ValueError(f"Dependency approval {key} is invalid")
        return value

    requested_cwd = _dependency_display_text(arguments.get("cwd"), "cwd")
    resolved_cwd = _dependency_display_text(
        arguments.get("resolved_cwd"), "resolved cwd"
    )
    manifest_count = bounded_count("dependency_manifest_count", 5)
    if manifest_count < 1:
        raise ValueError("Dependency approval has no selected manifest")
    manifest_names: list[str] = []
    for index in range(1, manifest_count + 1):
        value = arguments.get(f"dependency_manifest_{index:02d}")
        if not isinstance(value, str):
            raise ValueError("Dependency approval manifest snapshot is incomplete")
        name, separator, _remainder = value.partition(" | ")
        if (
            not separator
            or name not in _DEPENDENCY_MANIFEST_NAMES
            or re.fullmatch(
                re.escape(name) + r" \| [0-9]+ bytes \| sha256:[0-9a-f]{64}",
                value,
            ) is None
            or contains_secret(value)
        ):
            raise ValueError("Dependency approval manifest snapshot is invalid")
        manifest_names.append(name)
    if len(set(manifest_names)) != len(manifest_names):
        raise ValueError("Dependency approval manifest snapshot contains duplicates")

    dependency_count = bounded_count("dependency_declaration_count", 1_000_000)
    source_omitted = bounded_count("dependency_summary_omitted_count", 1_000_000)
    summaries: list[str | dict[str, Any]] = []
    for index in range(1, 9):
        key = f"dependency_{index:02d}"
        if key not in arguments:
            break
        summaries.append(_dependency_display_text(arguments[key], key))
    if dependency_count != len(summaries) + source_omitted:
        raise ValueError("Dependency approval summary count is inconsistent")

    executors: list[dict[str, Any]] = []
    if "package.json" in manifest_names:
        for identity, prefix in (("node", "node"), ("npm-cli", "npm_cli")):
            byte_count = arguments.get(f"dependency_{prefix}_bytes")
            if (
                isinstance(byte_count, bool)
                or not isinstance(byte_count, int)
                or byte_count < 0
            ):
                raise ValueError("Dependency approval executor size is invalid")
            executors.append({
                "identity": identity,
                "path": _dependency_display_text(
                    arguments.get(f"dependency_{prefix}_path"),
                    f"{identity} path",
                ),
                "bytes": byte_count,
                "sha256": exact_sha(f"dependency_{prefix}_sha256"),
            })

    display_arguments: dict[str, Any] = {
        "cwd": requested_cwd,
        "resolved_cwd": resolved_cwd,
        "manifest_names": manifest_names,
        "manifest_tree_sha256": exact_sha("dependency_tree_sha256"),
        "network_access": arguments.get("dependency_network_access") is True,
        "host_authority": arguments.get("dependency_host_authority") is True,
        "node_lifecycle_scripts": arguments.get("node_lifecycle_scripts"),
        "executors": executors,
        "direct_dependency_count": dependency_count,
        "direct_dependencies": [],
        "omitted_dependency_count": dependency_count,
    }
    if (
        display_arguments["network_access"] is not True
        or display_arguments["host_authority"] is not True
        or display_arguments["node_lifecycle_scripts"] != "disabled"
    ):
        raise ValueError("Dependency approval authority controls are incomplete")

    payload = {
        "tool": "install_project_dependencies",
        "arguments_sha256": canonical["arguments_sha256"],
        "arguments": display_arguments,
    }
    maximum_size = 1_900
    for summary in summaries:
        display_arguments["direct_dependencies"].append(summary)
        display_arguments["omitted_dependency_count"] = (
            dependency_count - len(display_arguments["direct_dependencies"])
        )
        candidate = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        if len(candidate) > maximum_size:
            display_arguments["direct_dependencies"].pop()
            display_arguments["omitted_dependency_count"] = (
                dependency_count - len(display_arguments["direct_dependencies"])
            )
            break
    serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if len(serialized) > maximum_size:
        raise ValueError("Dependency approval summary cannot fit the operator display")
    if dependency_count and not display_arguments["direct_dependencies"]:
        raise ValueError("Dependency approval cannot show a direct dependency preview")
    return serialized


def approval_display_resource(
    tool_name: str,
    arguments: dict[str, Any],
    exact_resource: str,
) -> str:
    """Return presentation JSON while leaving the exact authorization input intact."""
    if tool_name == "install_project_dependencies":
        return _dependency_install_display_resource(arguments, exact_resource)
    return exact_resource


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
    elif tool_name == "desktop_interact" and isinstance(arguments.get("actions"), list):
        safe = _desktop_interact_arguments(arguments)
    elif tool_name == "google_drive_download_file" and any(
        key in arguments
        for key in ("file_id", "local_path", "resolved_local_path", "download_item")
    ):
        safe = _google_drive_download_arguments(arguments)
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
    maximum_size = 32_000 if tool_name == "desktop_interact" else 1_900
    if len(serialized) > maximum_size:
        if tool_name in {
            "windows_app_repair", "desktop_interact", "google_drive_download_file",
        }:
            label = {
                "windows_app_repair": "Application repair",
                "desktop_interact": "Desktop",
                "google_drive_download_file": "Google Drive download",
            }[tool_name]
            raise ValueError(f"{label} approval summary is too large")
        payload["arguments"] = {
            key: value if isinstance(value, (bool, int, float)) or value is None
            else _digest_descriptor(value)
            for key, value in sorted(arguments.items())
        }
        serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return serialized
