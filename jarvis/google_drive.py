from __future__ import annotations

import json
import hashlib
import mimetypes
import os
import re
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Callable, Literal
from uuid import uuid4

from .policy import resolve_workspace_path


DRIVE_FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"
DRIVE_NATIVE_MIME_PREFIX = "application/vnd.google-apps."
APP_FILES_SCOPE = "https://www.googleapis.com/auth/drive.file"
FULL_DRIVE_SCOPE = "https://www.googleapis.com/auth/drive"
ACCESS_SCOPES = {
    "app_files": (APP_FILES_SCOPE,),
    "full": (FULL_DRIVE_SCOPE,),
}

DEFAULT_MAX_TRANSFER_BYTES = 100 * 1024 * 1024
HARD_MAX_TRANSFER_BYTES = 512 * 1024 * 1024
TRANSFER_CHUNK_BYTES = 1024 * 1024
MAX_CREDENTIAL_FILE_BYTES = 64 * 1024
MAX_LIST_PAGE_SIZE = 100
MAX_INVENTORY_ITEMS = 1_000
MAX_ORGANIZE_OPERATIONS = 5
MAX_PAGE_TOKEN_CHARS = 2_048
MAX_REMOTE_NAME_CHARS = 255
MAX_DRIVE_ID_CHARS = 256
API_RETRIES = 2

_DRIVE_ID = re.compile(r"^[A-Za-z0-9_-]+$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_MIME_TYPE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]{0,126}/"
    r"[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]{0,126}$"
)
_NATIVE_EXPORT_MIME_TYPES = {
    "application/vnd.google-apps.document": frozenset({
        "application/pdf",
        "application/rtf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "text/plain",
    }),
    "application/vnd.google-apps.drawing": frozenset({
        "application/pdf", "image/jpeg", "image/png",
    }),
    "application/vnd.google-apps.presentation": frozenset({
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    }),
    "application/vnd.google-apps.spreadsheet": frozenset({
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "text/csv",
    }),
}


class GoogleDriveError(RuntimeError):
    """Base error for the standalone Google Drive provider."""


class GoogleDriveDependencyError(GoogleDriveError):
    """Raised when Google's official Python client libraries are unavailable."""


class GoogleDriveCredentialError(GoogleDriveError):
    """Raised when local OAuth configuration or authorization is unusable."""


class GoogleDriveValidationError(GoogleDriveError, ValueError):
    """Raised before an invalid local or remote operation is attempted."""


class GoogleDriveTransferLimitError(GoogleDriveValidationError):
    """Raised when a transfer exceeds the provider's configured byte limit."""


class GoogleDriveAPIError(GoogleDriveError):
    """Raised for a sanitized failure from the remote Drive API."""


@dataclass(frozen=True)
class _GoogleDependencies:
    credentials_type: Any
    request_type: Any
    flow_type: Any
    build: Any
    media_upload_type: Any
    media_download_type: Any


def _google_dependencies() -> _GoogleDependencies:
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
        from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload
    except ImportError:
        raise GoogleDriveDependencyError(
            "Google Drive support requires google-api-python-client, "
            "google-auth-httplib2, and google-auth-oauthlib."
        ) from None
    return _GoogleDependencies(
        credentials_type=Credentials,
        request_type=Request,
        flow_type=InstalledAppFlow,
        build=build,
        media_upload_type=MediaIoBaseUpload,
        media_download_type=MediaIoBaseDownload,
    )


def default_google_drive_credentials_directory() -> Path:
    local_app_data = os.getenv("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "JarvisLocal" / "google-drive"
    return Path.home() / ".config" / "jarvis-local" / "google-drive"


def _is_reparse_point(details: os.stat_result) -> bool:
    attributes = getattr(details, "st_file_attributes", 0)
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _file_identity(details: os.stat_result) -> tuple[int, int, int, int]:
    return (
        int(details.st_dev),
        int(details.st_ino),
        int(details.st_size),
        int(details.st_mtime_ns),
    )


def _workspace_local_path(
    workspace: Path,
    user_path: str | Path,
    *,
    operation: str,
) -> Path:
    try:
        resolved = resolve_workspace_path(workspace, user_path)
        raw = Path(user_path)
        lexical = Path(os.path.abspath(raw if raw.is_absolute() else workspace / raw))
        relative = lexical.relative_to(workspace)
    except (OSError, PermissionError, ValueError):
        raise GoogleDriveValidationError(
            f"{operation} path must stay inside the configured workspace"
        ) from None

    current = workspace
    for part in relative.parts:
        current /= part
        try:
            details = os.lstat(current)
        except FileNotFoundError:
            break
        except OSError:
            raise GoogleDriveValidationError(
                f"{operation} path could not be inspected safely"
            ) from None
        if stat.S_ISLNK(details.st_mode) or _is_reparse_point(details):
            raise GoogleDriveValidationError(
                f"{operation} path must not traverse links or reparse points"
            )
    return resolved


def _opened_file_path(stream: BinaryIO) -> Path | None:
    if os.name == "nt":
        try:
            import ctypes
            import msvcrt

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            get_final_path = kernel32.GetFinalPathNameByHandleW
            get_final_path.argtypes = [
                ctypes.c_void_p,
                ctypes.c_wchar_p,
                ctypes.c_uint32,
                ctypes.c_uint32,
            ]
            get_final_path.restype = ctypes.c_uint32
            handle = msvcrt.get_osfhandle(stream.fileno())
            buffer = ctypes.create_unicode_buffer(32_768)
            length = get_final_path(handle, buffer, len(buffer), 0)
            if not length or length >= len(buffer):
                return None
            value = buffer.value
            if value.startswith("\\\\?\\UNC\\"):
                value = "\\\\" + value[8:]
            elif value.startswith("\\\\?\\"):
                value = value[4:]
            return Path(value).resolve()
        except (OSError, ValueError):
            return None
    descriptor_path = Path(f"/proc/self/fd/{stream.fileno()}")
    try:
        return descriptor_path.resolve(strict=True)
    except OSError:
        return None


def _validate_opened_workspace_file(
    stream: BinaryIO,
    *,
    workspace: Path,
    expected_path: Path,
    operation: str,
) -> None:
    opened_path = _opened_file_path(stream)
    if opened_path is None:
        return
    try:
        opened_path.relative_to(workspace)
    except ValueError:
        raise GoogleDriveValidationError(
            f"{operation} file handle escaped the configured workspace"
        ) from None
    if opened_path != expected_path.resolve():
        raise GoogleDriveValidationError(
            f"{operation} path changed while it was being opened"
        )


def _ordinary_file(path: Path, *, label: str, required: bool) -> os.stat_result | None:
    try:
        details = os.lstat(path)
    except FileNotFoundError:
        if required:
            raise GoogleDriveCredentialError(f"{label} is missing") from None
        return None
    except OSError:
        raise GoogleDriveCredentialError(f"{label} is inaccessible") from None
    if stat.S_ISLNK(details.st_mode) or _is_reparse_point(details) or not stat.S_ISREG(details.st_mode):
        raise GoogleDriveCredentialError(f"{label} must be an ordinary non-link file")
    if details.st_size > MAX_CREDENTIAL_FILE_BYTES:
        raise GoogleDriveCredentialError(
            f"{label} exceeds {MAX_CREDENTIAL_FILE_BYTES} bytes"
        )
    return details


def _read_bounded_json_file(
    path: Path,
    *,
    label: str,
    required: bool,
) -> dict[str, Any] | None:
    before = _ordinary_file(path, label=label, required=required)
    if before is None:
        return None
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as stream:
            opened = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(opened.st_mode)
                or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                != (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
            ):
                raise GoogleDriveCredentialError(f"{label} changed before it was opened")
            raw = stream.read(MAX_CREDENTIAL_FILE_BYTES + 1)
            after = os.fstat(stream.fileno())
    except GoogleDriveCredentialError:
        raise
    except OSError:
        raise GoogleDriveCredentialError(f"{label} could not be read safely") from None
    if (
        len(raw) > MAX_CREDENTIAL_FILE_BYTES
        or (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
        != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    ):
        raise GoogleDriveCredentialError(f"{label} changed while it was being read")
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise GoogleDriveCredentialError(f"{label} is not valid JSON") from None
    if not isinstance(parsed, dict):
        raise GoogleDriveCredentialError(f"{label} must contain a JSON object")
    return parsed


def _validate_drive_id(value: str, *, label: str, allow_root: bool = True) -> str:
    value = str(value).strip()
    if allow_root and value == "root":
        return value
    if (
        not value
        or len(value) > MAX_DRIVE_ID_CHARS
        or not _DRIVE_ID.fullmatch(value)
    ):
        raise GoogleDriveValidationError(f"{label} is not a valid Drive identifier")
    return value


def _validate_page_token(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value or len(value) > MAX_PAGE_TOKEN_CHARS:
        raise GoogleDriveValidationError("Drive page token is invalid")
    if _CONTROL.search(value):
        raise GoogleDriveValidationError("Drive page token contains control characters")
    return value


def _validate_remote_name(value: str, *, local_safe: bool) -> str:
    if not isinstance(value, str):
        raise GoogleDriveValidationError("Drive item name must be text")
    name = value.strip()
    if not name or len(name) > MAX_REMOTE_NAME_CHARS or _CONTROL.search(name):
        raise GoogleDriveValidationError("Drive item name is invalid")
    if local_safe and (name in {".", ".."} or "/" in name or "\\" in name):
        raise GoogleDriveValidationError("Drive item name must not contain path separators")
    return name


def _validate_mime_type(value: str) -> str:
    value = str(value).strip().casefold()
    if len(value) > 255 or not _MIME_TYPE.fullmatch(value):
        raise GoogleDriveValidationError("MIME type is invalid")
    return value


def _normalize_item(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise GoogleDriveAPIError("Google Drive returned invalid item metadata")
    try:
        item_id = _validate_drive_id(
            raw.get("id", ""), label="Returned item ID", allow_root=False
        )
        name = _validate_remote_name(raw.get("name", ""), local_safe=False)
        mime_type = _validate_mime_type(raw.get("mimeType", ""))
    except GoogleDriveValidationError:
        raise GoogleDriveAPIError("Google Drive returned invalid item metadata") from None
    result: dict[str, Any] = {
        "id": item_id,
        "name": name,
        "mime_type": mime_type,
        "is_folder": mime_type == DRIVE_FOLDER_MIME_TYPE,
    }
    trashed = raw.get("trashed", False)
    if not isinstance(trashed, bool):
        raise GoogleDriveAPIError("Google Drive returned invalid trash metadata")
    result["trashed"] = trashed
    if raw.get("size") is not None:
        try:
            size = int(raw["size"])
        except (TypeError, ValueError):
            raise GoogleDriveAPIError("Google Drive returned an invalid item size") from None
        if size < 0:
            raise GoogleDriveAPIError("Google Drive returned an invalid item size")
        result["size"] = size
    modified = raw.get("modifiedTime")
    if modified is not None:
        if not isinstance(modified, str) or len(modified) > 100 or _CONTROL.search(modified):
            raise GoogleDriveAPIError("Google Drive returned an invalid modification time")
        result["modified_time"] = modified
    parents = raw.get("parents")
    if parents is not None:
        if not isinstance(parents, list) or len(parents) > 100:
            raise GoogleDriveAPIError("Google Drive returned invalid parent metadata")
        try:
            result["parents"] = [
                _validate_drive_id(parent, label="Returned parent ID", allow_root=False)
                for parent in parents
            ]
        except GoogleDriveValidationError:
            raise GoogleDriveAPIError("Google Drive returned invalid parent metadata") from None
    return result


class _BoundedReader:
    def __init__(self, stream: BinaryIO, limit: int) -> None:
        self.stream = stream
        self.limit = limit

    def read(self, size: int = -1) -> bytes:
        remaining = self.limit - self.stream.tell()
        requested = remaining + 1 if size < 0 else min(size, remaining + 1)
        data = self.stream.read(requested)
        if len(data) > remaining:
            raise GoogleDriveTransferLimitError(
                f"Upload exceeds the configured {self.limit}-byte limit"
            )
        return data

    def seek(self, offset: int, whence: int = os.SEEK_SET) -> int:
        position = self.stream.seek(offset, whence)
        if position > self.limit:
            raise GoogleDriveTransferLimitError(
                f"Upload exceeds the configured {self.limit}-byte limit"
            )
        return position

    def __getattr__(self, name: str) -> Any:
        return getattr(self.stream, name)


class _BoundedWriter:
    def __init__(self, stream: BinaryIO, limit: int) -> None:
        self.stream = stream
        self.limit = limit

    def write(self, data: bytes) -> int:
        if self.stream.tell() + len(data) > self.limit:
            raise GoogleDriveTransferLimitError(
                f"Download exceeds the configured {self.limit}-byte limit"
            )
        return self.stream.write(data)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.stream, name)


class GoogleDriveProvider:
    """Bounded Drive v3 operations backed by locally stored desktop OAuth tokens.

    Authentication never accepts a client secret, access token, refresh token, or
    authorization code as a method argument. The operator places Google's Desktop
    OAuth JSON file at ``client_secret.json`` in ``credential_directory`` and then
    explicitly calls :meth:`authenticate` to complete the system-browser loopback
    flow.
    """

    def __init__(
        self,
        workspace: Path,
        *,
        credential_directory: Path | None = None,
        access_mode: Literal["app_files", "full"] = "app_files",
        max_transfer_bytes: int = DEFAULT_MAX_TRANSFER_BYTES,
        service: Any | None = None,
    ) -> None:
        workspace = Path(workspace).resolve()
        if not workspace.is_dir():
            raise GoogleDriveValidationError("Google Drive workspace must be an existing directory")
        if access_mode not in ACCESS_SCOPES:
            raise GoogleDriveValidationError("Google Drive access mode must be app_files or full")
        max_transfer_bytes = int(max_transfer_bytes)
        if not 1 <= max_transfer_bytes <= HARD_MAX_TRANSFER_BYTES:
            raise GoogleDriveValidationError(
                f"Transfer limit must be between 1 and {HARD_MAX_TRANSFER_BYTES} bytes"
            )
        self.workspace = workspace
        credential_directory = Path(
            credential_directory or default_google_drive_credentials_directory()
        ).expanduser()
        if not credential_directory.is_absolute():
            credential_directory = Path.cwd() / credential_directory
        # Do not resolve here: the later lstat must still be able to reject a
        # caller-supplied credential-directory symlink or Windows junction.
        self.credential_directory = Path(os.path.abspath(credential_directory))
        credential_target = self.credential_directory.resolve(strict=False)
        try:
            credential_target.relative_to(workspace)
        except ValueError:
            credentials_inside_workspace = False
        else:
            credentials_inside_workspace = True
        try:
            workspace.relative_to(credential_target)
        except ValueError:
            workspace_inside_credentials = False
        else:
            workspace_inside_credentials = True
        if credentials_inside_workspace or workspace_inside_credentials:
            raise GoogleDriveValidationError(
                "Google Drive credentials and workspace must be disjoint directories"
            )
        self.client_secrets_path = self.credential_directory / "client_secret.json"
        self.token_path = self.credential_directory / "token.json"
        self.access_mode = access_mode
        self.scopes = ACCESS_SCOPES[access_mode]
        self.max_transfer_bytes = max_transfer_bytes
        self._service = service

    def _credential_directory_exists(self) -> bool:
        try:
            details = os.lstat(self.credential_directory)
        except FileNotFoundError:
            return False
        except OSError:
            raise GoogleDriveCredentialError(
                "Google Drive credential directory is inaccessible"
            ) from None
        if (
            stat.S_ISLNK(details.st_mode)
            or _is_reparse_point(details)
            or not stat.S_ISDIR(details.st_mode)
        ):
            raise GoogleDriveCredentialError(
                "Google Drive credential directory must be an ordinary non-link directory"
            )
        return True

    def _ensure_credential_directory(self) -> None:
        try:
            self.credential_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            details = os.lstat(self.credential_directory)
        except OSError:
            raise GoogleDriveCredentialError(
                "Google Drive credential directory is inaccessible"
            ) from None
        if (
            stat.S_ISLNK(details.st_mode)
            or _is_reparse_point(details)
            or not stat.S_ISDIR(details.st_mode)
        ):
            raise GoogleDriveCredentialError(
                "Google Drive credential directory must be an ordinary non-link directory"
            )
        try:
            os.chmod(self.credential_directory, 0o700)
        except OSError:
            pass

    def _read_credential_json(
        self,
        path: Path,
        *,
        label: str,
        required: bool,
    ) -> dict[str, Any] | None:
        if not self._credential_directory_exists():
            if required:
                raise GoogleDriveCredentialError(
                    "Google Drive credential directory is missing"
                )
            return None
        return _read_bounded_json_file(path, label=label, required=required)

    def _validate_credential_scopes(self, credentials: Any) -> None:
        granted = getattr(credentials, "granted_scopes", None)
        if not granted:
            granted = getattr(credentials, "scopes", None)
        try:
            granted_scopes = {str(scope) for scope in (granted or ())}
        except TypeError:
            granted_scopes = set()
        if granted_scopes != set(self.scopes):
            raise GoogleDriveCredentialError(
                "Google Drive token scope does not exactly match the configured access mode; "
                "explicit reauthorization is required"
            )

    def _load_credentials(self, dependencies: _GoogleDependencies) -> Any | None:
        authorized_user_info = self._read_credential_json(
            self.token_path,
            label="Google Drive token file",
            required=False,
        )
        if authorized_user_info is None:
            return None
        try:
            credentials = dependencies.credentials_type.from_authorized_user_info(
                authorized_user_info
            )
        except Exception:
            raise GoogleDriveCredentialError(
                "Google Drive token file is invalid; replace it through explicit reauthorization"
            ) from None
        self._validate_credential_scopes(credentials)
        return credentials

    def _save_credentials(self, credentials: Any) -> None:
        try:
            encoded = credentials.to_json()
        except Exception:
            raise GoogleDriveCredentialError("Google Drive credentials could not be serialized") from None
        if not isinstance(encoded, str) or len(encoded.encode("utf-8")) > MAX_CREDENTIAL_FILE_BYTES:
            raise GoogleDriveCredentialError("Google Drive credential payload is invalid")
        try:
            parsed = json.loads(encoded)
        except json.JSONDecodeError:
            raise GoogleDriveCredentialError("Google Drive credential payload is invalid") from None
        if not isinstance(parsed, dict):
            raise GoogleDriveCredentialError("Google Drive credential payload is invalid")

        self._ensure_credential_directory()
        temporary = self.token_path.with_name(f".{self.token_path.name}.{uuid4().hex}.tmp")
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.chmod(temporary, 0o600)
            except OSError:
                pass
            os.replace(temporary, self.token_path)
        except OSError:
            raise GoogleDriveCredentialError("Google Drive token could not be stored safely") from None
        finally:
            temporary.unlink(missing_ok=True)

    def auth_status(self) -> dict[str, Any]:
        try:
            client_configured = self._read_credential_json(
                self.client_secrets_path,
                label="Google Drive Desktop OAuth client file",
                required=False,
            ) is not None
            token_present = self._read_credential_json(
                self.token_path,
                label="Google Drive token file",
                required=False,
            ) is not None
        except GoogleDriveCredentialError:
            return {
                "access_mode": self.access_mode,
                "authenticated": False,
                "client_configured": False,
                "token_present": False,
                "refreshable": False,
                "state": "configuration_invalid",
            }
        base = {
            "access_mode": self.access_mode,
            "authenticated": False,
            "client_configured": client_configured,
            "token_present": token_present,
            "refreshable": False,
        }
        if self._service is not None:
            return {**base, "state": "ready", "authenticated": True}
        if not token_present:
            return {
                **base,
                "state": "authorization_required" if client_configured else "not_configured",
            }
        try:
            dependencies = _google_dependencies()
            credentials = self._load_credentials(dependencies)
        except GoogleDriveDependencyError:
            return {**base, "state": "dependencies_missing"}
        except GoogleDriveCredentialError:
            return {**base, "state": "credentials_invalid"}
        if credentials is not None and bool(getattr(credentials, "valid", False)):
            return {**base, "state": "ready", "authenticated": True}
        refreshable = bool(
            credentials is not None
            and getattr(credentials, "expired", False)
            and getattr(credentials, "refresh_token", None)
        )
        return {
            **base,
            "state": "refresh_required" if refreshable else "authorization_required",
            "refreshable": refreshable,
        }

    def status(self) -> dict[str, Any]:
        status = self.auth_status()
        status.update({
            "client_secrets_path": str(self.client_secrets_path),
            "whole_drive_visible": self.access_mode == "full",
        })
        if status["state"] == "not_configured":
            status["next_action"] = (
                "Create a Google Desktop OAuth client with the Drive API enabled, save the "
                "downloaded JSON at client_secrets_path, then call google_drive_authenticate."
            )
        elif status["state"] in {"authorization_required", "refresh_required"}:
            status["next_action"] = "Call google_drive_authenticate and complete Google consent."
        return status

    def authenticate(self, *, open_browser: bool = True) -> dict[str, Any]:
        """Authorize via Google's Desktop-app loopback flow or refresh a local token."""
        dependencies = _google_dependencies()
        credentials = self._load_credentials(dependencies)
        if credentials is not None and not getattr(credentials, "valid", False):
            if getattr(credentials, "expired", False) and getattr(credentials, "refresh_token", None):
                try:
                    credentials.refresh(dependencies.request_type())
                except Exception:
                    raise GoogleDriveCredentialError(
                        "Google Drive token refresh failed; explicit reauthorization is required"
                    ) from None
                self._save_credentials(credentials)
            else:
                raise GoogleDriveCredentialError(
                    "Google Drive token cannot be refreshed; explicit reauthorization is required"
                )
        if credentials is None:
            client_config = self._read_credential_json(
                self.client_secrets_path,
                label="Google Drive Desktop OAuth client file",
                required=True,
            )
            try:
                flow = dependencies.flow_type.from_client_config(
                    client_config, list(self.scopes)
                )
                credentials = flow.run_local_server(
                    host="127.0.0.1",
                    port=0,
                    open_browser=bool(open_browser),
                    authorization_prompt_message=(
                        "Authorize JARVIS in your browser. Never paste an authorization code "
                        "or token into a chat prompt: {url}"
                    ),
                    success_message="Google Drive authorization completed. You may close this window.",
                    access_type="offline",
                    prompt="consent",
                )
            except Exception:
                raise GoogleDriveCredentialError("Google Drive browser authorization failed") from None
            self._validate_credential_scopes(credentials)
            if not getattr(credentials, "refresh_token", None):
                raise GoogleDriveCredentialError(
                    "Google Drive authorization did not return a refresh token; revoke the "
                    "prior app grant and authorize again"
                )
            self._save_credentials(credentials)
        if not getattr(credentials, "valid", False):
            raise GoogleDriveCredentialError("Google Drive did not return valid credentials")
        self._service = self._build_service(dependencies, credentials)
        return self.auth_status()

    def _build_service(self, dependencies: _GoogleDependencies, credentials: Any) -> Any:
        try:
            return dependencies.build(
                "drive", "v3", credentials=credentials, cache_discovery=False
            )
        except Exception:
            raise GoogleDriveAPIError("Google Drive client initialization failed") from None

    def _service_client(self) -> tuple[Any, _GoogleDependencies]:
        dependencies = _google_dependencies()
        if self._service is not None:
            return self._service, dependencies
        credentials = self._load_credentials(dependencies)
        if credentials is None:
            raise GoogleDriveCredentialError(
                "Google Drive is not authorized; call authenticate() explicitly"
            )
        if not getattr(credentials, "valid", False):
            if not (
                getattr(credentials, "expired", False)
                and getattr(credentials, "refresh_token", None)
            ):
                raise GoogleDriveCredentialError(
                    "Google Drive token is not valid; explicit reauthorization is required"
                )
            try:
                credentials.refresh(dependencies.request_type())
            except Exception:
                raise GoogleDriveCredentialError("Google Drive token refresh failed") from None
            self._save_credentials(credentials)
        self._service = self._build_service(dependencies, credentials)
        return self._service, dependencies

    @staticmethod
    def _prepare(operation: str, factory: Callable[[], Any]) -> Any:
        try:
            return factory()
        except GoogleDriveError:
            raise
        except Exception as exc:
            raise GoogleDriveAPIError(
                f"Google Drive {operation} failed ({type(exc).__name__})"
            ) from None

    @staticmethod
    def _execute(
        request: Any,
        operation: str,
        *,
        retries: int = API_RETRIES,
    ) -> dict[str, Any]:
        try:
            result = request.execute(num_retries=max(0, min(int(retries), API_RETRIES)))
        except GoogleDriveError:
            raise
        except Exception as exc:
            raise GoogleDriveAPIError(
                f"Google Drive {operation} failed ({type(exc).__name__})"
            ) from None
        if not isinstance(result, dict):
            raise GoogleDriveAPIError(f"Google Drive {operation} returned invalid data")
        return result

    def list_files(
        self,
        folder_id: str = "root",
        *,
        page_size: int = 50,
        page_token: str | None = None,
        include_trashed: bool = False,
    ) -> dict[str, Any]:
        folder_id = _validate_drive_id(folder_id, label="Folder ID")
        page_size = max(1, min(int(page_size), MAX_LIST_PAGE_SIZE))
        page_token = _validate_page_token(page_token)
        query = f"'{folder_id}' in parents"
        if not include_trashed:
            query += " and trashed = false"
        service, _ = self._service_client()
        arguments: dict[str, Any] = {
            "q": query,
            "pageSize": page_size,
            "fields": (
                "nextPageToken,files(id,name,mimeType,size,modifiedTime,parents)"
            ),
            "orderBy": "folder,name_natural",
            "spaces": "drive",
            "supportsAllDrives": True,
            "includeItemsFromAllDrives": True,
        }
        if page_token is not None:
            arguments["pageToken"] = page_token
        request = self._prepare(
            "list preparation", lambda: service.files().list(**arguments)
        )
        response = self._execute(request, "list")
        raw_items = response.get("files", [])
        if not isinstance(raw_items, list):
            raise GoogleDriveAPIError("Google Drive list returned invalid items")
        next_token = response.get("nextPageToken")
        if next_token is not None:
            try:
                next_token = _validate_page_token(next_token)
            except GoogleDriveValidationError:
                raise GoogleDriveAPIError(
                    "Google Drive list returned an invalid page token"
                ) from None
        return {
            "items": [_normalize_item(item) for item in raw_items[:page_size]],
            "next_page_token": next_token,
        }

    def list_folder(self, folder_id: str = "root", **kwargs: Any) -> dict[str, Any]:
        return self.list_files(folder_id, **kwargs)

    def inventory(
        self,
        *,
        max_items: int = 500,
        include_trashed: bool = False,
    ) -> dict[str, Any]:
        """Return one bounded account-wide inventory for planning, never mutation."""
        if isinstance(max_items, bool) or not isinstance(max_items, int):
            raise GoogleDriveValidationError("Inventory item limit must be an integer")
        if not 1 <= max_items <= MAX_INVENTORY_ITEMS:
            raise GoogleDriveValidationError(
                f"Inventory item limit must be between 1 and {MAX_INVENTORY_ITEMS}"
            )
        service, _ = self._service_client()
        items: list[dict[str, Any]] = []
        page_token: str | None = None
        truncated = False
        while len(items) < max_items:
            arguments: dict[str, Any] = {
                "q": "trashed = true" if include_trashed else "trashed = false",
                "pageSize": min(MAX_LIST_PAGE_SIZE, max_items - len(items)),
                "fields": (
                    "nextPageToken,files(id,name,mimeType,size,modifiedTime,parents,trashed)"
                ),
                "orderBy": "folder,name_natural",
                "spaces": "drive",
                "supportsAllDrives": True,
                "includeItemsFromAllDrives": True,
            }
            if page_token is not None:
                arguments["pageToken"] = page_token
            request = self._prepare(
                "inventory preparation",
                lambda arguments=arguments: service.files().list(**arguments),
            )
            response = self._execute(request, "inventory")
            raw_items = response.get("files", [])
            if not isinstance(raw_items, list):
                raise GoogleDriveAPIError("Google Drive inventory returned invalid items")
            remaining = max_items - len(items)
            items.extend(_normalize_item(item) for item in raw_items[:remaining])
            raw_next = response.get("nextPageToken")
            if raw_next is None:
                page_token = None
                break
            try:
                page_token = _validate_page_token(raw_next)
            except GoogleDriveValidationError:
                raise GoogleDriveAPIError(
                    "Google Drive inventory returned an invalid page token"
                ) from None
            if not page_token:
                break
        if page_token is not None:
            truncated = True
        return {
            "items": items,
            "item_count": len(items),
            "truncated": truncated,
            "access_mode": self.access_mode,
            "whole_drive_visible": self.access_mode == "full",
        }

    @staticmethod
    def _normalize_organize_operations(
        operations: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        if not isinstance(operations, list) or not 1 <= len(operations) <= MAX_ORGANIZE_OPERATIONS:
            raise GoogleDriveValidationError(
                f"Organize operations must contain between 1 and {MAX_ORGANIZE_OPERATIONS} items"
            )
        normalized: list[dict[str, Any]] = []
        seen: set[str] = set()
        allowed = {"file_id", "new_name", "folder_id", "trash"}
        for raw in operations:
            if not isinstance(raw, dict) or set(raw) - allowed:
                raise GoogleDriveValidationError("Drive organize operation is invalid")
            file_id = _validate_drive_id(
                raw.get("file_id", ""), label="Organize item ID", allow_root=False
            )
            if file_id in seen:
                raise GoogleDriveValidationError("Each Drive item may be organized only once per batch")
            seen.add(file_id)
            operation: dict[str, Any] = {"file_id": file_id}
            if "new_name" in raw:
                operation["new_name"] = _validate_remote_name(
                    raw["new_name"], local_safe=False
                )
            if "folder_id" in raw:
                operation["folder_id"] = _validate_drive_id(
                    raw["folder_id"], label="Destination folder ID"
                )
            if "trash" in raw:
                if not isinstance(raw["trash"], bool) or not raw["trash"]:
                    raise GoogleDriveValidationError("Trash must be the boolean true")
                operation["trash"] = True
            if len(operation) == 1:
                raise GoogleDriveValidationError("Drive organize operation has no requested change")
            normalized.append(operation)
        return normalized

    def _account_permission_id_with_service(self, service: Any) -> str:
        request = self._prepare(
            "account identity preparation",
            lambda: service.about().get(fields="user(permissionId)"),
        )
        about = self._execute(request, "account identity")
        user = about.get("user")
        permission_id = user.get("permissionId") if isinstance(user, dict) else None
        if not isinstance(permission_id, str):
            raise GoogleDriveAPIError("Google Drive returned no stable account identity")
        return _validate_drive_id(
            permission_id, label="Drive account permission ID", allow_root=False
        )

    def _item_snapshot_with_service(self, service: Any, file_id: str) -> dict[str, Any]:
        request = self._prepare(
            "organize item preparation",
            lambda: service.files().get(
                fileId=file_id,
                fields="id,name,mimeType,size,modifiedTime,parents,trashed",
                supportsAllDrives=True,
            ),
        )
        return _normalize_item(self._execute(request, "organize item lookup"))

    def organize_approval_snapshot(
        self,
        operations: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Bind a visible cleanup batch to its account and exact current metadata."""
        normalized = self._normalize_organize_operations(operations)
        service, _ = self._service_client()
        account_id = self._account_permission_id_with_service(service)
        items: list[dict[str, Any]] = []
        for operation in normalized:
            current = self._item_snapshot_with_service(service, operation["file_id"])
            if current["trashed"]:
                raise GoogleDriveValidationError("A trashed Drive item cannot be organized")
            entry: dict[str, Any] = {
                "operation": operation,
                "current": current,
            }
            if "folder_id" in operation:
                destination = self._destination_snapshot_with_service(
                    service, operation["folder_id"]
                )
                if destination["drive_account_permission_id"] != account_id:
                    raise PermissionError("Google Drive account changed during cleanup planning")
                entry["resolved_folder_id"] = destination["resolved_folder_id"]
            items.append(entry)
        return {
            "drive_account_permission_id": account_id,
            "organize_items": items,
        }

    def organize_files(
        self,
        operations: list[dict[str, Any]],
        *,
        expected_approval_snapshot: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Apply at most five exact recoverable Drive changes after a TOCTOU recheck."""
        if not isinstance(expected_approval_snapshot, dict):
            raise PermissionError("Exact approved Drive cleanup snapshot is required")
        current_snapshot = self.organize_approval_snapshot(operations)
        if current_snapshot != expected_approval_snapshot:
            raise PermissionError("Google Drive cleanup targets changed after approval")
        service, _ = self._service_client()
        applied: list[dict[str, Any]] = []
        for entry in current_snapshot["organize_items"]:
            operation = entry["operation"]
            body: dict[str, Any] = {}
            if "new_name" in operation:
                body["name"] = operation["new_name"]
            if operation.get("trash"):
                body["trashed"] = True
            arguments: dict[str, Any] = {
                "fileId": operation["file_id"],
                "body": body,
                "fields": "id,name,mimeType,size,modifiedTime,parents,trashed",
                "supportsAllDrives": True,
            }
            if "resolved_folder_id" in entry:
                arguments["addParents"] = entry["resolved_folder_id"]
                parents = entry["current"].get("parents", [])
                if parents:
                    arguments["removeParents"] = ",".join(parents)
            request = self._prepare(
                "organize update preparation",
                lambda arguments=arguments: service.files().update(**arguments),
            )
            applied.append(_normalize_item(self._execute(
                request, "organize update", retries=0
            )))
        return {"applied": applied, "applied_count": len(applied), "recoverable": True}

    def approval_destination_snapshot(self, folder_id: str = "root") -> dict[str, Any]:
        """Resolve the active Drive account and an exact destination folder ID."""
        folder_value = _validate_drive_id(folder_id, label="Destination folder ID")
        service, _ = self._service_client()
        return self._destination_snapshot_with_service(service, folder_value)

    def _destination_snapshot_with_service(
        self,
        service: Any,
        folder_id: str,
    ) -> dict[str, Any]:
        permission_id = self._account_permission_id_with_service(service)
        folder_request = self._prepare(
            "destination folder preparation",
            lambda: service.files().get(
                fileId=folder_id,
                fields="id,mimeType",
                supportsAllDrives=True,
            ),
        )
        folder = self._execute(folder_request, "destination folder")
        resolved_folder_id = folder.get("id")
        if not isinstance(resolved_folder_id, str):
            raise GoogleDriveAPIError("Google Drive returned no stable destination folder ID")
        resolved_folder_id = _validate_drive_id(
            resolved_folder_id, label="Resolved destination folder ID", allow_root=False
        )
        if folder.get("mimeType") != DRIVE_FOLDER_MIME_TYPE:
            raise GoogleDriveValidationError("Google Drive destination must be a folder")
        return {
            "drive_account_permission_id": permission_id,
            "resolved_folder_id": resolved_folder_id,
        }

    def create_folder(
        self,
        name: str,
        parent_id: str = "root",
        *,
        expected_account_permission_id: str | None = None,
        expected_parent_folder_id: str | None = None,
    ) -> dict[str, Any]:
        name = _validate_remote_name(name, local_safe=True)
        parent_id = _validate_drive_id(parent_id, label="Parent folder ID")
        if (expected_account_permission_id is None) != (expected_parent_folder_id is None):
            raise GoogleDriveValidationError(
                "Approved Drive account and parent folder must be supplied together"
            )
        service, _ = self._service_client()
        if expected_account_permission_id is not None:
            current_destination = self._destination_snapshot_with_service(
                service, parent_id
            )
            if current_destination != {
                "drive_account_permission_id": expected_account_permission_id,
                "resolved_folder_id": expected_parent_folder_id,
            }:
                raise PermissionError(
                    "Google Drive account or parent folder changed after approval"
                )
            parent_id = current_destination["resolved_folder_id"]
        request = self._prepare(
            "folder creation preparation",
            lambda: service.files().create(
                body={
                    "name": name,
                    "mimeType": DRIVE_FOLDER_MIME_TYPE,
                    "parents": [parent_id],
                },
                fields="id,name,mimeType,size,modifiedTime,parents",
                supportsAllDrives=True,
            ),
        )
        # A response-lost retry can create a second same-named folder; Drive has
        # no caller-supplied idempotency key for this request.
        return _normalize_item(self._execute(request, "folder creation", retries=0))

    def upload_file(
        self,
        local_path: str | Path,
        *,
        folder_id: str = "root",
        drive_name: str | None = None,
        mime_type: str | None = None,
        expected_size_bytes: int | None = None,
        expected_sha256: str | None = None,
        expected_account_permission_id: str | None = None,
        expected_folder_id: str | None = None,
    ) -> dict[str, Any]:
        """Upload the exact approved bytes from a descriptor-backed local snapshot."""
        source = _workspace_local_path(
            self.workspace, local_path, operation="Upload source"
        )
        try:
            before = os.lstat(source)
        except OSError:
            raise GoogleDriveValidationError("Upload source is missing or inaccessible") from None
        if stat.S_ISLNK(before.st_mode) or _is_reparse_point(before) or not stat.S_ISREG(before.st_mode):
            raise GoogleDriveValidationError("Upload source must be an ordinary non-link file")
        if before.st_size > self.max_transfer_bytes:
            raise GoogleDriveTransferLimitError(
                f"Upload exceeds the configured {self.max_transfer_bytes}-byte limit"
            )
        if (expected_size_bytes is None) != (expected_sha256 is None):
            raise GoogleDriveValidationError(
                "Approved upload size and SHA-256 must be supplied together"
            )
        if expected_size_bytes is not None:
            if (
                isinstance(expected_size_bytes, bool)
                or not isinstance(expected_size_bytes, int)
                or expected_size_bytes < 0
                or expected_size_bytes > self.max_transfer_bytes
            ):
                raise GoogleDriveValidationError("Approved upload size is invalid")
            if before.st_size != expected_size_bytes:
                raise GoogleDriveValidationError("Upload source no longer matches the approved size")
            if (
                not isinstance(expected_sha256, str)
                or not re.fullmatch(r"[0-9a-fA-F]{64}", expected_sha256)
            ):
                raise GoogleDriveValidationError("Approved upload SHA-256 is invalid")
            expected_sha256 = expected_sha256.casefold()
        folder_id = _validate_drive_id(folder_id, label="Destination folder ID")
        drive_name = _validate_remote_name(drive_name or source.name, local_safe=True)
        guessed_type = mimetypes.guess_type(source.name)[0] or "application/octet-stream"
        mime_type = _validate_mime_type(mime_type or guessed_type)
        service, dependencies = self._service_client()
        if (expected_account_permission_id is None) != (expected_folder_id is None):
            raise GoogleDriveValidationError(
                "Approved Drive account and destination folder must be supplied together"
            )
        if expected_account_permission_id is not None:
            current_destination = self._destination_snapshot_with_service(
                service, folder_id
            )
            if current_destination != {
                "drive_account_permission_id": expected_account_permission_id,
                "resolved_folder_id": expected_folder_id,
            }:
                raise PermissionError(
                    "Google Drive account or destination folder changed after approval"
                )
            folder_id = current_destination["resolved_folder_id"]
        try:
            descriptor = os.open(
                source,
                os.O_RDONLY
                | getattr(os, "O_BINARY", 0)
                | getattr(os, "O_NOFOLLOW", 0),
            )
            with os.fdopen(descriptor, "rb") as stream:
                _validate_opened_workspace_file(
                    stream,
                    workspace=self.workspace,
                    expected_path=source,
                    operation="Upload source",
                )
                opened = os.fstat(stream.fileno())
                if (
                    _file_identity(before) != _file_identity(opened)
                    or not stat.S_ISREG(opened.st_mode)
                ):
                    raise GoogleDriveValidationError("Upload source changed before it was opened")
                if expected_size_bytes is not None and opened.st_size != expected_size_bytes:
                    raise GoogleDriveValidationError(
                        "Upload source no longer matches the approved size"
                    )
                # Upload from an immutable-by-construction temporary snapshot.  A
                # caller cannot swap the path or alter the original inode after the
                # approval hash and thereby change bytes read by the resumable client.
                with tempfile.TemporaryFile(mode="w+b") as snapshot:
                    digest = hashlib.sha256()
                    copied = 0
                    for chunk in iter(lambda: stream.read(TRANSFER_CHUNK_BYTES), b""):
                        copied += len(chunk)
                        if copied > self.max_transfer_bytes:
                            raise GoogleDriveTransferLimitError(
                                f"Upload exceeds the configured {self.max_transfer_bytes}-byte limit"
                            )
                        digest.update(chunk)
                        snapshot.write(chunk)
                    after = os.fstat(stream.fileno())
                    if (
                        _file_identity(opened) != _file_identity(after)
                        or copied != after.st_size
                        or not stat.S_ISREG(after.st_mode)
                    ):
                        raise GoogleDriveValidationError(
                            "Upload source changed while its approved bytes were being read"
                        )
                    actual_sha256 = digest.hexdigest()
                    if expected_size_bytes is not None and (
                        copied != expected_size_bytes
                        or actual_sha256 != expected_sha256
                    ):
                        raise GoogleDriveValidationError(
                            "Upload source no longer matches the approved bytes"
                        )
                    snapshot.flush()
                    snapshot.seek(0)
                    media = self._prepare(
                        "upload media preparation",
                        lambda: dependencies.media_upload_type(
                            _BoundedReader(snapshot, self.max_transfer_bytes),
                            mimetype=mime_type,
                            chunksize=TRANSFER_CHUNK_BYTES,
                            resumable=True,
                        ),
                    )
                    request = self._prepare(
                        "upload request preparation",
                        lambda: service.files().create(
                            body={"name": drive_name, "parents": [folder_id]},
                            media_body=media,
                            fields="id,name,mimeType,size,modifiedTime,parents",
                            supportsAllDrives=True,
                        ),
                    )
                    # Resumable chunk retries happen inside the media request, but the
                    # initial file-creation POST must not be replayed after ambiguity.
                    response = self._execute(request, "upload", retries=0)
        except GoogleDriveError:
            raise
        except OSError:
            raise GoogleDriveValidationError("Upload source could not be read safely") from None
        return _normalize_item(response)

    def upload_approval_snapshot(
        self,
        local_path: str | Path,
        *,
        folder_id: str = "root",
        drive_name: str | None = None,
        mime_type: str | None = None,
    ) -> dict[str, Any]:
        """Return a bounded, descriptor-verified fingerprint for an upload approval."""
        source = _workspace_local_path(
            self.workspace, local_path, operation="Upload source"
        )
        try:
            before = os.lstat(source)
        except OSError:
            raise GoogleDriveValidationError("Upload source is missing or inaccessible") from None
        if (
            stat.S_ISLNK(before.st_mode)
            or _is_reparse_point(before)
            or not stat.S_ISREG(before.st_mode)
        ):
            raise GoogleDriveValidationError("Upload source must be an ordinary non-link file")
        # This check deliberately precedes opening or hashing so an approval
        # request cannot turn an oversized file into unbounded local work.
        if before.st_size > self.max_transfer_bytes:
            raise GoogleDriveTransferLimitError(
                f"Upload exceeds the configured {self.max_transfer_bytes}-byte limit"
            )
        folder_value = _validate_drive_id(folder_id, label="Destination folder ID")
        name_value = _validate_remote_name(drive_name or source.name, local_safe=True)
        guessed_type = mimetypes.guess_type(source.name)[0] or "application/octet-stream"
        mime_value = _validate_mime_type(mime_type or guessed_type)
        try:
            descriptor = os.open(
                source,
                os.O_RDONLY
                | getattr(os, "O_BINARY", 0)
                | getattr(os, "O_NOFOLLOW", 0),
            )
            with os.fdopen(descriptor, "rb") as stream:
                _validate_opened_workspace_file(
                    stream,
                    workspace=self.workspace,
                    expected_path=source,
                    operation="Upload source",
                )
                opened = os.fstat(stream.fileno())
                if (
                    _file_identity(before) != _file_identity(opened)
                    or not stat.S_ISREG(opened.st_mode)
                ):
                    raise GoogleDriveValidationError("Upload source changed before it was opened")
                digest = hashlib.sha256()
                read_bytes = 0
                for chunk in iter(lambda: stream.read(TRANSFER_CHUNK_BYTES), b""):
                    read_bytes += len(chunk)
                    if read_bytes > self.max_transfer_bytes:
                        raise GoogleDriveTransferLimitError(
                            f"Upload exceeds the configured {self.max_transfer_bytes}-byte limit"
                        )
                    digest.update(chunk)
                after = os.fstat(stream.fileno())
                if (
                    _file_identity(opened) != _file_identity(after)
                    or read_bytes != after.st_size
                    or not stat.S_ISREG(after.st_mode)
                ):
                    raise GoogleDriveValidationError(
                        "Upload source changed while its approval fingerprint was computed"
                    )
        except GoogleDriveError:
            raise
        except OSError:
            raise GoogleDriveValidationError("Upload source could not be read safely") from None
        destination = self.approval_destination_snapshot(folder_value)
        return {
            "resolved_local_path": str(source),
            "folder_id": folder_value,
            "drive_name": name_value,
            "mime_type": mime_value,
            "local_size_bytes": read_bytes,
            "local_sha256": digest.hexdigest(),
            **destination,
        }

    def _download_approval_snapshot_with_service(
        self,
        service: Any,
        file_id: str,
        export_mime_type: str | None,
    ) -> dict[str, Any]:
        file_id = _validate_drive_id(file_id, label="File ID", allow_root=False)
        current = self._item_snapshot_with_service(service, file_id)
        if current["id"] != file_id:
            raise GoogleDriveAPIError("Google Drive returned mismatched item metadata")
        if current.get("trashed"):
            raise GoogleDriveValidationError("A trashed Drive item cannot be downloaded")
        if current.get("is_folder"):
            raise GoogleDriveValidationError("Google Drive folders cannot be downloaded as files")
        declared_size = current.get("size")
        if declared_size is not None and declared_size > self.max_transfer_bytes:
            raise GoogleDriveTransferLimitError(
                f"Download exceeds the configured {self.max_transfer_bytes}-byte limit"
            )
        native_document = current["mime_type"].startswith(DRIVE_NATIVE_MIME_PREFIX)
        normalized_export: str | None = None
        if native_document:
            if not export_mime_type:
                raise GoogleDriveValidationError(
                    "Google-native files require an explicit supported export MIME type"
                )
            normalized_export = _validate_mime_type(export_mime_type)
            allowed_exports = _NATIVE_EXPORT_MIME_TYPES.get(current["mime_type"])
            if allowed_exports is None or normalized_export not in allowed_exports:
                raise GoogleDriveValidationError(
                    "Requested Google-native export type is not supported"
                )
        elif export_mime_type is not None:
            raise GoogleDriveValidationError(
                "Export MIME type applies only to Google-native files"
            )
        return {
            "drive_account_permission_id": self._account_permission_id_with_service(service),
            "download_item": current,
            "resolved_export_mime_type": normalized_export,
        }

    def download_approval_snapshot(
        self,
        file_id: str,
        *,
        export_mime_type: str | None = None,
    ) -> dict[str, Any]:
        """Bind a download approval to the exact account and remote item metadata."""
        service, _ = self._service_client()
        return self._download_approval_snapshot_with_service(
            service, file_id, export_mime_type
        )

    def download_file(
        self,
        file_id: str,
        local_path: str | Path,
        *,
        overwrite: bool = False,
        export_mime_type: str | None = None,
        expected_approval_snapshot: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        file_id = _validate_drive_id(file_id, label="File ID", allow_root=False)
        destination = _workspace_local_path(
            self.workspace, local_path, operation="Download destination"
        )
        if destination.exists() and not overwrite:
            raise GoogleDriveValidationError("Download destination already exists")
        if destination.exists() and not destination.is_file():
            raise GoogleDriveValidationError("Download destination must be a file path")
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            raise GoogleDriveValidationError(
                "Download destination directory could not be created"
            ) from None
        service, dependencies = self._service_client()
        if expected_approval_snapshot is not None:
            if not isinstance(expected_approval_snapshot, dict):
                raise PermissionError("Exact approved Google Drive download snapshot is required")
            current_snapshot = self._download_approval_snapshot_with_service(
                service, file_id, export_mime_type
            )
            if current_snapshot != expected_approval_snapshot:
                raise PermissionError("Google Drive download target changed after approval")
            metadata = current_snapshot["download_item"]
            export_mime_type = current_snapshot["resolved_export_mime_type"]
        else:
            metadata_request = self._prepare(
                "metadata request preparation",
                lambda: service.files().get(
                    fileId=file_id,
                    fields="id,name,mimeType,size,modifiedTime,parents,trashed",
                    supportsAllDrives=True,
                ),
            )
            metadata = _normalize_item(self._execute(
                metadata_request,
                "metadata lookup",
            ))
            if metadata["id"] != file_id:
                raise GoogleDriveAPIError("Google Drive returned mismatched item metadata")
        declared_size = metadata.get("size")
        if declared_size is not None and declared_size > self.max_transfer_bytes:
            raise GoogleDriveTransferLimitError(
                f"Download exceeds the configured {self.max_transfer_bytes}-byte limit"
            )
        native_document = metadata["mime_type"].startswith(DRIVE_NATIVE_MIME_PREFIX)
        if native_document:
            if not export_mime_type:
                raise GoogleDriveValidationError(
                    "Google-native files require an explicit supported export MIME type"
                )
            export_mime_type = _validate_mime_type(export_mime_type)
            allowed_exports = _NATIVE_EXPORT_MIME_TYPES.get(metadata["mime_type"])
            if allowed_exports is None:
                raise GoogleDriveValidationError(
                    "This Google-native item type cannot be downloaded by this provider"
                )
            if export_mime_type not in allowed_exports:
                raise GoogleDriveValidationError("Requested Google-native export type is not supported")
            request = self._prepare(
                "export request preparation",
                lambda: service.files().export_media(
                    fileId=file_id, mimeType=export_mime_type
                ),
            )
        else:
            if export_mime_type is not None:
                raise GoogleDriveValidationError(
                    "Export MIME type applies only to Google-native files"
                )
            request = self._prepare(
                "download request preparation",
                lambda: service.files().get_media(
                    fileId=file_id, supportsAllDrives=True
                ),
            )

        temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.part")
        installed_destination = False
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_BINARY", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            with os.fdopen(descriptor, "wb") as raw_stream:
                _validate_opened_workspace_file(
                    raw_stream,
                    workspace=self.workspace,
                    expected_path=temporary,
                    operation="Download temporary",
                )
                stream = _BoundedWriter(raw_stream, self.max_transfer_bytes)
                downloader = self._prepare(
                    "download initialization",
                    lambda: dependencies.media_download_type(
                        stream, request, chunksize=TRANSFER_CHUNK_BYTES
                    ),
                )
                done = False
                while not done:
                    try:
                        _, done = downloader.next_chunk(num_retries=API_RETRIES)
                    except GoogleDriveError:
                        raise
                    except Exception as exc:
                        raise GoogleDriveAPIError(
                            f"Google Drive download failed ({type(exc).__name__})"
                        ) from None
                raw_stream.flush()
                os.fsync(raw_stream.fileno())
                bytes_written = raw_stream.tell()
            current_destination = _workspace_local_path(
                self.workspace, local_path, operation="Download destination"
            )
            if current_destination != destination:
                raise GoogleDriveValidationError(
                    "Download destination changed before installation"
                )
            current_temporary = _workspace_local_path(
                self.workspace, temporary, operation="Download temporary"
            )
            if current_temporary != temporary or not temporary.is_file():
                raise GoogleDriveValidationError(
                    "Download temporary changed before installation"
                )
            if overwrite:
                os.replace(temporary, destination)
            else:
                try:
                    os.link(temporary, destination)
                except FileExistsError:
                    raise GoogleDriveValidationError(
                        "Download destination already exists"
                    ) from None
                except OSError:
                    # A no-overwrite create remains race-safe on filesystems without links.
                    try:
                        with destination.open("xb") as output, temporary.open("rb") as source:
                            installed_destination = True
                            while chunk := source.read(TRANSFER_CHUNK_BYTES):
                                output.write(chunk)
                            output.flush()
                            os.fsync(output.fileno())
                    except OSError:
                        if installed_destination:
                            destination.unlink(missing_ok=True)
                        raise
                    temporary.unlink()
                else:
                    temporary.unlink()
        except GoogleDriveError:
            raise
        except OSError:
            raise GoogleDriveValidationError(
                "Download destination could not be written safely"
            ) from None
        finally:
            temporary.unlink(missing_ok=True)
        return {
            **metadata,
            "local_path": str(destination),
            "bytes_written": bytes_written,
        }


__all__ = [
    "ACCESS_SCOPES",
    "APP_FILES_SCOPE",
    "DEFAULT_MAX_TRANSFER_BYTES",
    "DRIVE_FOLDER_MIME_TYPE",
    "FULL_DRIVE_SCOPE",
    "GoogleDriveAPIError",
    "GoogleDriveCredentialError",
    "GoogleDriveDependencyError",
    "GoogleDriveError",
    "GoogleDriveProvider",
    "GoogleDriveTransferLimitError",
    "GoogleDriveValidationError",
    "default_google_drive_credentials_directory",
]
