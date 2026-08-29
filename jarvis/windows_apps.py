from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import time
import urllib.parse
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .desktop import resolve_computer_path
from .trusted_executables import windows_system_executable

try:
    import winreg
except ImportError:  # pragma: no cover - Windows-only capability
    winreg = None  # type: ignore[assignment]


MAX_PHOTOSHOP_INPUT_BYTES = 2_000_000_000
PHOTOSHOP_INPUT_SUFFIXES = frozenset({
    ".bmp", ".gif", ".jpeg", ".jpg", ".png", ".psb", ".psd", ".tif", ".tiff", ".webp",
})
_BLOCKED_EXECUTABLES = frozenset({
    "cmd.exe", "conhost.exe", "control.exe", "cscript.exe", "mshta.exe",
    "msiexec.exe", "powershell.exe", "pwsh.exe", "reg.exe", "regedit.exe",
    "rundll32.exe", "taskmgr.exe", "winget.exe", "wscript.exe", "wsl.exe", "wt.exe",
})
_BLOCKED_NAME_WORDS = re.compile(
    r"\b(?:admin|agent|command\s+prompt|helper|installer|powershell|registry\s+editor|"
    r"server|service|shell|subsystem|terminal|uninstall|updat(?:e|er))\b",
    re.IGNORECASE,
)
_PHOTOSHOP_VERSION = re.compile(
    r"Adobe Photoshop\s+(20\d{2})$", re.IGNORECASE
)
_PACKAGE_ACTIVATION_ID = re.compile(
    r"[A-Za-z0-9._-]{1,200}![A-Za-z0-9._-]{1,100}\Z"
)
_FRIENDLY_EXECUTABLE_NAMES = {
    "acrobat.exe": "Adobe Acrobat",
    "chrome.exe": "Google Chrome",
    "code.exe": "Microsoft VS Code",
    "devenv.exe": "Microsoft Visual Studio",
    "excel.exe": "Microsoft Excel",
    "illustrator.exe": "Adobe Illustrator",
    "mspaint.exe": "Microsoft Paint",
    "msaccess.exe": "Microsoft Access",
    "mspub.exe": "Microsoft Publisher",
    "notepad.exe": "Notepad",
    "onenote.exe": "Microsoft OneNote",
    "outlook.exe": "Microsoft Outlook",
    "powerpnt.exe": "Microsoft PowerPoint",
    "winword.exe": "Microsoft Word",
}


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _regular_file(path: Path) -> Path:
    details = os.lstat(path)
    attributes = getattr(details, "st_file_attributes", 0)
    if (
        not stat.S_ISREG(details.st_mode)
        or stat.S_ISLNK(details.st_mode)
        or attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    ):
        raise PermissionError("Application and image targets must be ordinary files")
    return path.resolve(strict=True)


def _ordinary_executable(path: Path) -> Path:
    """Resolve one executable without accepting link-based identity ambiguity."""
    executable = _regular_file(path)
    details = os.stat(executable, follow_symlinks=False)
    if int(getattr(details, "st_nlink", 1)) > 1:
        raise PermissionError("Application executables must not be hard-linked")
    return executable


def _clean_display_name(executable: Path) -> str:
    friendly = _FRIENDLY_EXECUTABLE_NAMES.get(executable.name.casefold())
    if friendly:
        return friendly
    parent = executable.parent.name.strip()
    if parent and parent.casefold() not in {"application", "bin", "program", "programs"}:
        return parent
    return executable.stem.replace("_", " ").strip()


@dataclass(frozen=True)
class InstalledApplication:
    name: str
    executable: Path | None
    source: str
    activation_id: str | None = None

    def as_dict(self) -> dict[str, str]:
        result = {
            "name": self.name,
            "executable": str(self.executable) if self.executable is not None else "",
            "source": self.source,
        }
        if self.activation_id is not None:
            result["activation_id"] = self.activation_id
        return result


class WindowsAppController:
    """Bounded Windows app discovery plus explicit high-level app adapters."""

    def __init__(
        self,
        computer_root: Path,
        data_dir: Path,
        *,
        runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
        launcher: Callable[..., subprocess.Popen[bytes]] | None = None,
        catalog: Callable[[], list[InstalledApplication]] | None = None,
        com_server_lookup: Callable[[str], Path | None] | None = None,
    ) -> None:
        self.computer_root = Path(computer_root).resolve()
        self.data_dir = Path(data_dir).resolve()
        self._runner = runner or subprocess.run
        self._launcher = launcher or subprocess.Popen
        self._catalog_override = catalog
        self._com_server_lookup = com_server_lookup

    @staticmethod
    def _registry_catalog() -> list[InstalledApplication]:
        if os.name != "nt" or winreg is None:
            return []
        discovered: dict[str, InstalledApplication] = {}
        roots = (
            (winreg.HKEY_CURRENT_USER, winreg.KEY_READ),
            (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_READ),
            (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_READ | getattr(winreg, "KEY_WOW64_32KEY", 0)),
            (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_READ | getattr(winreg, "KEY_WOW64_64KEY", 0)),
        )
        registry_path = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"
        for root, access in roots:
            try:
                parent = winreg.OpenKey(root, registry_path, 0, access)
            except OSError:
                continue
            with parent:
                index = 0
                while True:
                    try:
                        key_name = winreg.EnumKey(parent, index)
                    except OSError:
                        break
                    index += 1
                    try:
                        with winreg.OpenKey(parent, key_name, 0, access) as child:
                            raw, _kind = winreg.QueryValueEx(child, None)
                        candidate = Path(os.path.expandvars(str(raw).strip().strip('"')))
                        resolved = _regular_file(candidate)
                        if resolved.suffix.casefold() != ".exe":
                            continue
                    except (OSError, PermissionError, ValueError):
                        continue
                    name = _clean_display_name(resolved)
                    discovered.setdefault(
                        str(resolved).casefold(),
                        InstalledApplication(name=name, executable=resolved, source="app-paths"),
                    )

        adobe_root = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Adobe"
        if adobe_root.is_dir():
            for candidate in adobe_root.glob("Adobe Photoshop */Photoshop.exe"):
                try:
                    resolved = _regular_file(candidate)
                except (OSError, PermissionError):
                    continue
                discovered[str(resolved).casefold()] = InstalledApplication(
                    name=candidate.parent.name,
                    executable=resolved,
                    source="adobe-installation",
                )
        return sorted(discovered.values(), key=lambda app: (app.name.casefold(), str(app.executable).casefold()))

    def catalog(self) -> list[InstalledApplication]:
        if self._catalog_override:
            return list(self._catalog_override())
        merged = self._registry_catalog() + self._packaged_catalog()
        discovered: dict[tuple[str, str], InstalledApplication] = {}
        for app in merged:
            target = str(app.executable or app.activation_id or "").casefold()
            discovered.setdefault((app.name.casefold(), target), app)
        return sorted(
            discovered.values(),
            key=lambda app: (app.name.casefold(), str(app.executable or app.activation_id).casefold()),
        )

    def _packaged_catalog(self) -> list[InstalledApplication]:
        """Discover signed Start-menu package activations such as Calculator."""
        if os.name != "nt":
            return []
        try:
            powershell = windows_system_executable(
                "System32", "WindowsPowerShell", "v1.0", "powershell.exe"
            )
            completed = self._runner(
                [
                    str(powershell), "-NoLogo", "-NoProfile", "-NonInteractive",
                    "-Command",
                    "Get-StartApps | Select-Object Name,AppID | ConvertTo-Json -Compress",
                ],
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
                env=self._safe_environment(),
            )
        except (OSError, PermissionError, subprocess.SubprocessError):
            return []
        if completed.returncode != 0 or len(completed.stdout or "") > 2_000_000:
            return []
        try:
            decoded = json.loads(completed.stdout or "[]")
        except json.JSONDecodeError:
            return []
        rows = decoded if isinstance(decoded, list) else [decoded]
        apps: list[InstalledApplication] = []
        for row in rows[:5_000]:
            if not isinstance(row, dict):
                continue
            name = str(row.get("Name") or "").strip()
            activation_id = str(row.get("AppID") or "").strip()
            if (
                not name
                or len(name) > 200
                or any(character in name for character in "\x00\r\n")
                or _PACKAGE_ACTIVATION_ID.fullmatch(activation_id) is None
                or _BLOCKED_NAME_WORDS.search(name)
            ):
                continue
            apps.append(InstalledApplication(
                name=name,
                executable=None,
                source="start-apps",
                activation_id=activation_id,
            ))
        return apps

    def list_apps(self, query: str = "", limit: int = 50) -> dict[str, Any]:
        needle = str(query or "").strip().casefold()
        bounded_limit = max(1, min(int(limit), 100))
        matches = [
            app for app in self.catalog()
            if (
                not needle
                or needle in app.name.casefold()
                or (
                    app.executable is not None
                    and needle in app.executable.name.casefold()
                )
                or needle in str(app.activation_id or "").casefold()
            )
        ][:bounded_limit]
        return {
            "platform": "windows" if os.name == "nt" else os.name,
            "query": query,
            "applications": [app.as_dict() for app in matches],
            "count": len(matches),
        }

    def _resolve_app(self, application: str) -> InstalledApplication:
        requested = str(application or "").strip().casefold()
        if not requested or len(requested) > 200 or any(char in requested for char in "\x00\r\n"):
            raise ValueError("Application name must contain 1-200 plain characters")
        apps = self.catalog()
        exact = [app for app in apps if app.name.casefold() == requested]
        candidates = exact or [app for app in apps if requested in app.name.casefold()]
        if requested in {"photoshop", "adobe photoshop"}:
            stable = [app for app in apps if _PHOTOSHOP_VERSION.fullmatch(app.name)]
            if stable:
                candidates = sorted(
                    stable,
                    key=lambda app: int(_PHOTOSHOP_VERSION.fullmatch(app.name).group(1)),  # type: ignore[union-attr]
                    reverse=True,
                )[:1]
        if not candidates:
            raise FileNotFoundError(f"Installed application not found: {application}")
        if len(candidates) != 1:
            names = ", ".join(app.name for app in candidates[:8])
            raise ValueError(f"Application name is ambiguous; choose one of: {names}")
        app = candidates[0]
        if _BLOCKED_NAME_WORDS.search(app.name):
            raise PermissionError("Shells, installers, and system-management apps cannot be launched")
        if app.activation_id is not None:
            if _PACKAGE_ACTIVATION_ID.fullmatch(app.activation_id) is None:
                raise PermissionError("Packaged application activation is invalid")
            return app
        if app.executable is None:
            raise FileNotFoundError(f"Installed application target is unavailable: {application}")
        executable = _ordinary_executable(app.executable)
        if executable.name.casefold() in _BLOCKED_EXECUTABLES:
            raise PermissionError("Shells, installers, and system-management apps cannot be launched")
        return InstalledApplication(app.name, executable, app.source)

    def launch_snapshot(self, application: str) -> dict[str, Any]:
        app = self._resolve_app(application)
        if app.activation_id is not None:
            return {
                "application": app.name,
                "activation_id": app.activation_id,
                "source": app.source,
            }
        if app.executable is None:
            raise FileNotFoundError(f"Installed application target is unavailable: {application}")
        details = app.executable.stat()
        return {
            "application": app.name,
            "resolved_executable": str(app.executable),
            "executable_bytes": details.st_size,
            "executable_mtime_ns": details.st_mtime_ns,
            "executable_sha256": _sha256_file(app.executable),
            "source": app.source,
        }

    @staticmethod
    def _safe_environment() -> dict[str, str]:
        secret_words = ("API_KEY", "PASSWORD", "SECRET", "TOKEN", "CREDENTIAL")
        return {
            key: value
            for key, value in os.environ.items()
            if not key.upper().startswith("JARVIS_")
            and not any(word in key.upper() for word in secret_words)
        }

    def launch_app(self, application: str, *, approved: dict[str, Any]) -> dict[str, Any]:
        confirmed = self.launch_snapshot(application)
        if confirmed != approved:
            raise PermissionError("Approved application changed before launch")
        flags = 0
        if os.name == "nt":
            flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
        if "activation_id" in confirmed:
            explorer = windows_system_executable("explorer.exe")
            command = [
                str(explorer),
                "shell:AppsFolder\\" + str(confirmed["activation_id"]),
            ]
            cwd = str(explorer.parent)
        else:
            command = [confirmed["resolved_executable"]]
            cwd = str(Path(confirmed["resolved_executable"]).parent)
        process = self._launcher(
            command,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=self._safe_environment(),
            creationflags=flags,
            close_fds=True,
        )
        return {
            "application": confirmed["application"],
            "executable": command[0],
            "activation_id": confirmed.get("activation_id"),
            "launched": True,
            "pid": process.pid,
        }

    def launch_approved_snapshot(self, *, approved: dict[str, Any]) -> dict[str, Any]:
        """Launch one already-profiled executable after an exact identity recheck."""
        executable_value = str(approved.get("resolved_executable") or "")
        application = str(approved.get("application") or "").strip()
        source = str(approved.get("source") or "").strip()
        if not executable_value or not application or not source:
            raise PermissionError("Approved application snapshot is incomplete")
        executable = _ordinary_executable(Path(executable_value))
        if (
            executable.suffix.casefold() != ".exe"
            or executable.name.casefold() in _BLOCKED_EXECUTABLES
            or _BLOCKED_NAME_WORDS.search(application)
        ):
            raise PermissionError("Approved application target is not launchable")
        details = executable.stat()
        confirmed = {
            "application": application,
            "resolved_executable": str(executable),
            "executable_bytes": details.st_size,
            "executable_mtime_ns": details.st_mtime_ns,
            "source": source,
        }
        expected_sha256 = str(approved.get("executable_sha256") or "")
        if not re.fullmatch(r"[a-f0-9]{64}", expected_sha256):
            raise PermissionError("Approved application content identity is missing")
        confirmed["executable_sha256"] = _sha256_file(executable)
        if confirmed != approved:
            raise PermissionError("Approved application changed before launch")
        flags = 0
        if os.name == "nt":
            flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
        process = self._launcher(
            [str(executable)],
            cwd=str(executable.parent),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=self._safe_environment(),
            creationflags=flags,
            close_fds=True,
        )
        return {
            "application": application,
            "executable": str(executable),
            "activation_id": None,
            "launched": True,
            "pid": process.pid,
        }

    @staticmethod
    def url_snapshot(url: str) -> dict[str, Any]:
        text = str(url or "").strip()
        parsed = urllib.parse.urlsplit(text)
        if (
            len(text) > 4096
            or parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or any(character in text for character in "\x00\r\n")
        ):
            raise ValueError("Browser URL must be a credential-free HTTP(S) URL")
        return {"url": text, "host": parsed.hostname.casefold()}

    def open_url(self, url: str, *, approved: dict[str, Any]) -> dict[str, Any]:
        confirmed = self.url_snapshot(url)
        if confirmed != approved:
            raise PermissionError("Approved browser URL changed before launch")
        explorer = windows_system_executable("explorer.exe")
        flags = 0
        if os.name == "nt":
            flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
        process = self._launcher(
            [str(explorer), confirmed["url"]],
            cwd=str(explorer.parent),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=self._safe_environment(),
            creationflags=flags,
            close_fds=True,
        )
        return {**confirmed, "opened": True, "pid": process.pid}

    @staticmethod
    def _registry_com_server(prog_id: str) -> Path | None:
        if os.name != "nt" or winreg is None:
            return None
        try:
            with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, rf"{prog_id}\CLSID") as key:
                clsid, _kind = winreg.QueryValueEx(key, None)
            with winreg.OpenKey(
                winreg.HKEY_CLASSES_ROOT, rf"CLSID\{clsid}\LocalServer32"
            ) as key:
                command, _kind = winreg.QueryValueEx(key, None)
        except OSError:
            return None
        text = os.path.expandvars(str(command).strip())
        if text.startswith('"'):
            end = text.find('"', 1)
            executable = text[1:end] if end > 1 else ""
        else:
            marker = re.search(
                r"\s/(?:Automation|Embedding)\b", text, re.IGNORECASE
            )
            executable = text[:marker.start()] if marker else text
        try:
            return _regular_file(Path(executable.strip()))
        except (OSError, PermissionError, ValueError):
            return None

    def _photoshop_application(self) -> tuple[InstalledApplication, str]:
        stable: list[tuple[int, InstalledApplication, str]] = []
        beta: list[tuple[int, InstalledApplication, str]] = []
        for app in self.catalog():
            match = _PHOTOSHOP_VERSION.fullmatch(app.name)
            if match:
                year = int(match.group(1))
                stable.append((year, app, f"Photoshop.Application.{(year - 2006) * 10}"))
            elif "photoshop" in app.name.casefold() and "beta" in app.name.casefold():
                beta.append((0, app, "Photoshop.Application.BETA"))
        lookup = self._com_server_lookup or self._registry_com_server
        for _version, app, prog_id in (
            sorted(stable, key=lambda item: item[0], reverse=True) + beta
        ):
            if app.executable is None:
                continue
            try:
                executable = _ordinary_executable(app.executable)
            except (OSError, PermissionError):
                continue
            server = lookup(prog_id)
            if server is not None and server.resolve() == executable:
                return InstalledApplication(app.name, executable, app.source), prog_id
        raise RuntimeError(
            "No installed Photoshop version has a matching registered COM automation server"
        )

    def photoshop_snapshot(
        self,
        input_path: str,
        output_path: str,
        *,
        overwrite: bool = False,
    ) -> dict[str, Any]:
        source = _regular_file(resolve_computer_path(self.computer_root, input_path))
        source_details = source.stat()
        if source.suffix.casefold() not in PHOTOSHOP_INPUT_SUFFIXES:
            raise ValueError("Photoshop input must be a supported image or Photoshop document")
        if source_details.st_size <= 0 or source_details.st_size > MAX_PHOTOSHOP_INPUT_BYTES:
            raise ValueError("Photoshop input must be between 1 byte and 2 GB")
        destination = resolve_computer_path(self.computer_root, output_path)
        if destination.suffix.casefold() != ".png":
            raise ValueError("Background-removed output must use the .png extension")
        if not destination.parent.is_dir():
            raise FileNotFoundError("Output directory does not exist")
        destination_exists = destination.exists()
        if destination_exists and not overwrite:
            raise FileExistsError("Output already exists; set overwrite=true to replace it with a backup")
        if destination_exists:
            destination = _regular_file(destination)
        app, prog_id = self._photoshop_application()
        app_details = app.executable.stat()
        snapshot: dict[str, Any] = {
            "input_path": input_path,
            "output_path": output_path,
            "overwrite": bool(overwrite),
            "resolved_input_path": str(source),
            "input_bytes": source_details.st_size,
            "input_sha256": _sha256_file(source),
            "resolved_output_path": str(destination),
            "output_exists": destination_exists,
            "photoshop_application": app.name,
            "photoshop_executable": str(app.executable),
            "photoshop_executable_bytes": app_details.st_size,
            "photoshop_executable_mtime_ns": app_details.st_mtime_ns,
            "photoshop_executable_sha256": _sha256_file(app.executable),
            "photoshop_prog_id": prog_id,
        }
        if destination_exists:
            existing_digest = _sha256_file(destination)
            snapshot["existing_output_sha256"] = existing_digest
            snapshot["existing_output_bytes"] = destination.stat().st_size
            snapshot["backup_path"] = str(destination.with_name(
                f"{destination.name}.jarvis-backup-{existing_digest[:12]}"
            ))
        return snapshot

    @staticmethod
    def _powershell_executable() -> Path:
        try:
            return windows_system_executable(
                "System32", "WindowsPowerShell", "v1.0", "powershell.exe"
            )
        except (OSError, PermissionError, ValueError) as exc:
            raise RuntimeError(
                "Windows PowerShell is unavailable for Photoshop automation"
            ) from exc

    def remove_photoshop_background(
        self,
        input_path: str,
        output_path: str,
        *,
        overwrite: bool = False,
        approved: dict[str, Any],
        timeout_seconds: int = 300,
    ) -> dict[str, Any]:
        confirmed = self.photoshop_snapshot(input_path, output_path, overwrite=overwrite)
        if confirmed != approved:
            raise PermissionError("Approved Photoshop files or application changed before execution")
        source = Path(confirmed["resolved_input_path"])
        destination = Path(confirmed["resolved_output_path"])
        source_snapshot = destination.with_name(
            f".jarvis-src-{uuid.uuid4().hex[:8]}{source.suffix.casefold()}"
        )
        temporary = destination.with_name(
            f".jarvis-bg-{uuid.uuid4().hex[:8]}.png"
        )
        copied_digest = hashlib.sha256()
        copied_bytes = 0
        with source.open("rb") as source_stream, source_snapshot.open("xb") as snapshot_stream:
            while chunk := source_stream.read(1024 * 1024):
                copied_bytes += len(chunk)
                if copied_bytes > MAX_PHOTOSHOP_INPUT_BYTES:
                    raise ValueError("Photoshop input exceeded the 2 GB limit while snapshotting")
                copied_digest.update(chunk)
                snapshot_stream.write(chunk)
        if (
            copied_bytes != confirmed["input_bytes"]
            or copied_digest.hexdigest() != confirmed["input_sha256"]
        ):
            try:
                source_snapshot.unlink()
            except OSError:
                pass
            raise PermissionError("Approved Photoshop source bytes changed before execution")
        jsx = "\n".join((
            "#target photoshop",
            "app.displayDialogs = DialogModes.NO;",
            f"var inputFile = new File({json.dumps(str(source_snapshot), ensure_ascii=True)});",
            f"var outputFile = new File({json.dumps(str(temporary), ensure_ascii=True)});",
            "if (!inputFile.exists) { throw new Error('Input image no longer exists'); }",
            "var documentRef = app.open(inputFile);",
            "try {",
            "  app.activeDocument = documentRef;",
            "  executeAction(stringIDToTypeID('removeBackground'), undefined, DialogModes.NO);",
            "  var options = new PNGSaveOptions();",
            "  options.interlaced = false;",
            "  documentRef.saveAs(outputFile, options, true, Extension.LOWERCASE);",
            "} finally {",
            "  documentRef.close(SaveOptions.DONOTSAVECHANGES);",
            "}",
        ))
        encoded_jsx = base64.b64encode(jsx.encode("utf-8")).decode("ascii")
        ps_script = "\n".join((
            "$ErrorActionPreference = 'Stop'",
            f"$jsx = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{encoded_jsx}'))",
            f"$photoshop = New-Object -ComObject '{confirmed['photoshop_prog_id']}'",
            "$photoshop.Visible = $true",
            "$photoshop.DoJavaScript($jsx)",
            "[void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($photoshop)",
        ))
        encoded_command = base64.b64encode(ps_script.encode("utf-16le")).decode("ascii")
        started = time.monotonic()
        backup: Path | None = None
        try:
            completed = self._runner(
                [
                    str(self._powershell_executable()),
                    "-NoLogo", "-NoProfile", "-NonInteractive",
                    "-EncodedCommand", encoded_command,
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=max(30, min(int(timeout_seconds), 600)),
                env=self._safe_environment(),
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                check=False,
            )
            if completed.returncode != 0:
                detail = (completed.stderr or completed.stdout or "Photoshop script failed").strip()
                raise RuntimeError(detail[:1000])
            output = _regular_file(temporary)
            with output.open("rb") as stream:
                if stream.read(8) != b"\x89PNG\r\n\x1a\n":
                    raise RuntimeError("Photoshop did not produce a valid PNG")
            if destination.exists():
                backup = Path(confirmed["backup_path"])
                if backup.exists():
                    if _sha256_file(backup) != confirmed["existing_output_sha256"]:
                        raise FileExistsError("Bound Photoshop backup path contains different data")
                else:
                    shutil.copy2(destination, backup)
            os.replace(output, destination)
        finally:
            for transient in (temporary, source_snapshot):
                try:
                    if transient.exists():
                        transient.unlink()
                except OSError:
                    pass
        return {
            "application": confirmed["photoshop_application"],
            "input_path": str(source),
            "output_path": str(destination),
            "output_sha256": _sha256_file(destination),
            "output_bytes": destination.stat().st_size,
            "backup": str(backup) if backup else None,
            "source_unchanged": _sha256_file(source) == confirmed["input_sha256"],
            "duration_seconds": round(time.monotonic() - started, 3),
        }
