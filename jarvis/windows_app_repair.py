from __future__ import annotations

import ctypes
import hashlib
import json
import os
import re
import stat
import subprocess
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from .app_repair import (
    Diagnosis,
    build_repair_plan,
    classify_app_failure,
    complete_repair,
)
from .windows_apps import WindowsAppController

try:
    import winreg
except ImportError:  # pragma: no cover - Windows-only capability
    winreg = None  # type: ignore[assignment]


MAX_CACHE_ENTRIES = 50_000
MAX_CACHE_BYTES = 2_000_000_000
MAX_CACHE_TARGETS = 5
MAX_APPROVAL_SUMMARY_CHARACTERS = 800
MAX_EXECUTABLE_BYTES = 1_000_000_000
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_WINDOWS_RESERVED_NAME = {
    "aux", "clock$", "con", "nul", "prn",
    *(f"com{index}" for index in range(1, 10)),
    *(f"lpt{index}" for index in range(1, 10)),
}
_SYMPTOMS = frozenset({
    "auto",
    "blank_or_unrendered",
    "authentication_failed",
    "connectivity_failed",
    "process_not_running",
    "update_required",
})
_PROFILE_FAILURE_SIGNAL = re.compile(
    r"\b(?:blank|crash(?:ed|es|ing)?|frozen|hang(?:s|ing)?|"
    r"not\s+(?:load|open|render|respond|start|work)|"
    r"won['’]?t\s+(?:load|open|render|respond|start|work)|"
    r"offline|network\s+(?:error|failure)|connection\s+(?:error|failure)|"
    r"diagnose|troubleshoot|investigate|fix|repair|recover)\b",
    re.IGNORECASE,
)
_PROFILE_REPAIR_SIGNAL = re.compile(r"\b(?:fix|repair|recover)\b", re.IGNORECASE)


@dataclass(frozen=True)
class AppRepairProfile:
    """Declarative authority boundary for one installed application."""

    app_id: str
    launch_name: str
    aliases: tuple[str, ...]
    executable_names: tuple[str, ...]
    install_relative_paths: tuple[str, ...]
    cache_patterns: tuple[str, ...]
    backup_root: str
    process_relative_paths: tuple[str, ...] = ()


BUILTIN_APP_REPAIR_PROFILES = (
    AppRepairProfile(
        app_id="epic-games-launcher",
        launch_name="Epic Games Launcher",
        aliases=("epic", "epic games", "epic games launcher"),
        executable_names=("epicgameslauncher.exe",),
        install_relative_paths=(
            "Epic Games/Launcher/Portal/Binaries/Win64/EpicGamesLauncher.exe",
        ),
        # Deliberately target disposable renderer caches, not Cookies, Local
        # Storage, Session Storage, account databases, or credential material.
        cache_patterns=(
            "EpicGamesLauncher/Saved/webcache*/Cache",
            "EpicGamesLauncher/Saved/webcache*/Code Cache",
            "EpicGamesLauncher/Saved/webcache*/GPUCache",
        ),
        backup_root="EpicGamesLauncher/Saved/jarvis-repair-backups",
        process_relative_paths=(
            "Epic Games/Launcher/Portal/Binaries/Win64/EpicGamesLauncher.exe",
            "Epic Games/Launcher/Engine/Binaries/Win64/EpicWebHelper.exe",
            "Epic Games/Launcher/Engine/Binaries/Win32/EpicWebHelper.exe",
        ),
    ),
)


def profiled_application_failure_kind(prompt: str) -> str | None:
    """Resolve failure grammar against the declarative profile catalog."""
    text = " ".join(str(prompt or "").strip().casefold().split())
    if not text or _PROFILE_FAILURE_SIGNAL.search(text) is None:
        return None
    aliases = {
        profile.app_id.casefold()
        for profile in BUILTIN_APP_REPAIR_PROFILES
    }
    for profile in BUILTIN_APP_REPAIR_PROFILES:
        aliases.add(profile.launch_name.casefold())
        aliases.update(alias.casefold() for alias in profile.aliases)
    if not any(
        re.search(rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])", text)
        for alias in aliases
    ):
        return None
    return "repair" if _PROFILE_REPAIR_SIGNAL.search(text) else "diagnose"


def _stable_digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8", errors="strict")).hexdigest()


def _stable_file_digest(path: Path) -> str:
    before = path.stat()
    if before.st_size < 1 or before.st_size > MAX_EXECUTABLE_BYTES:
        raise PermissionError("Profiled executable size is outside the bounded limit")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    after = path.stat()
    if (
        before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
        or getattr(before, "st_ino", None) != getattr(after, "st_ino", None)
    ):
        raise PermissionError("Profiled executable changed while its identity was read")
    return digest.hexdigest()


def _ordinary_directory(path: Path, root: Path) -> Path:
    """Verify every existing directory component without following reparse points."""
    lexical_root = Path(os.path.abspath(os.path.normpath(root)))
    root_details = os.lstat(lexical_root)
    root_attributes = getattr(root_details, "st_file_attributes", 0)
    if (
        not stat.S_ISDIR(root_details.st_mode)
        or stat.S_ISLNK(root_details.st_mode)
        or root_attributes & _REPARSE_POINT
    ):
        raise PermissionError("Application cache profile root must be an ordinary directory")
    resolved_root = lexical_root.resolve(strict=True)
    lexical_path = Path(os.path.abspath(os.path.normpath(path)))
    try:
        relative = lexical_path.relative_to(lexical_root)
    except ValueError as exc:
        raise PermissionError(
            "Application cache target escaped its declared profile root"
        ) from exc

    current = lexical_root
    for component in relative.parts:
        current = current / component
        details = os.lstat(current)
        attributes = getattr(details, "st_file_attributes", 0)
        if (
            not stat.S_ISDIR(details.st_mode)
            or stat.S_ISLNK(details.st_mode)
            or attributes & _REPARSE_POINT
        ):
            raise PermissionError(
                "Application cache targets must have ordinary directory ancestry"
            )
    resolved = current.resolve(strict=True)
    try:
        resolved.relative_to(resolved_root)
    except ValueError as exc:
        raise PermissionError(
            "Application cache target escaped its declared profile root"
        ) from exc
    return resolved


def _ensure_ordinary_directory_chain(path: Path, root: Path) -> Path:
    """Create a backup parent without following a link in any path component."""
    resolved_root = _ordinary_directory(root, root)
    lexical_target = Path(os.path.abspath(os.path.normpath(path)))
    try:
        relative = lexical_target.relative_to(resolved_root)
    except ValueError as exc:
        raise PermissionError(
            "Application repair backup escaped its declared profile root"
        ) from exc

    current = resolved_root
    for component in relative.parts:
        current = current / component
        try:
            os.mkdir(current)
        except FileExistsError:
            pass
        # Verify immediately after each creation/existence check. This prevents
        # a pre-existing junction from redirecting creation of deeper folders.
        current = _ordinary_directory(current, resolved_root)
    return current


def _cache_manifest(path: Path) -> dict[str, Any]:
    """Hash bounded metadata only; never read cache contents or private names."""
    count = 0
    total = 0
    rows: list[tuple[str, int, int, str]] = []
    pending = [path]
    while pending:
        current = pending.pop()
        with os.scandir(current) as entries:
            for entry in entries:
                count += 1
                if count > MAX_CACHE_ENTRIES:
                    raise ValueError("Application cache exceeds the bounded entry limit")
                # DirEntry.stat() reports st_nlink=0 on some Windows builds;
                # the direct path stat preserves the real hard-link count.
                details = os.stat(entry.path, follow_symlinks=False)
                attributes = getattr(details, "st_file_attributes", 0)
                if stat.S_ISLNK(details.st_mode) or attributes & _REPARSE_POINT:
                    raise PermissionError("Application cache contains a link or reparse point")
                relative = Path(entry.path).relative_to(path).as_posix()
                if stat.S_ISDIR(details.st_mode):
                    kind = "directory"
                    pending.append(Path(entry.path))
                elif stat.S_ISREG(details.st_mode):
                    if int(getattr(details, "st_nlink", 1)) > 1:
                        raise PermissionError(
                            "Application cache contains a hard-linked file"
                        )
                    kind = "file"
                    total += int(details.st_size)
                    if total > MAX_CACHE_BYTES:
                        raise ValueError("Application cache exceeds the bounded byte limit")
                else:
                    raise PermissionError("Application cache contains a non-file entry")
                rows.append((relative, int(details.st_size), int(details.st_mtime_ns), kind))
    return {
        "entries": count,
        "bytes": total,
        "metadata_sha256": _stable_digest(sorted(rows)),
    }


def _safe_profile_relative(value: str, label: str) -> Path:
    text = str(value or "").strip().replace("\\", "/")
    parsed = PurePosixPath(text)
    if (
        not text
        or len(text) > 240
        or text.startswith("/")
        or parsed.is_absolute()
        or any(part in {"", ".", ".."} for part in parsed.parts)
        or ":" in text
        or any(character in text for character in "*?[]")
        or any(character in text for character in "\x00\r\n")
        or any(part.rstrip(" .") != part for part in parsed.parts)
        or any(
            part.split(".", 1)[0].casefold() in _WINDOWS_RESERVED_NAME
            for part in parsed.parts
        )
    ):
        raise PermissionError(f"Repair profile {label} is not a canonical relative path")
    return Path(*parsed.parts)


def _safe_cache_pattern(value: str) -> tuple[str, Path]:
    """Allow one bounded wildcard below an exact two-component app prefix."""
    text = str(value or "").strip().replace("\\", "/")
    parsed = PurePosixPath(text)
    wildcard_parts = [part for part in parsed.parts if "*" in part]
    if (
        text.count("*") > 1
        or len(wildcard_parts) > 1
        or any(character in text for character in "?:[]\x00\r\n")
        or any(part in {"", ".", "..", "*"} for part in parsed.parts)
    ):
        raise PermissionError("Repair profile cache pattern is not bounded")
    if wildcard_parts:
        wildcard_index = parsed.parts.index(wildcard_parts[0])
        if wildcard_index < 2:
            raise PermissionError("Repair cache wildcard requires an exact app prefix")
    else:
        wildcard_index = len(parsed.parts)
    _safe_profile_relative(text.replace("*", "x"), "cache pattern")
    anchor = _safe_profile_relative(
        "/".join(parsed.parts[:wildcard_index]),
        "cache anchor",
    )
    return parsed.as_posix(), anchor


def _ordinary_profile_file(root: Path, relative: Path) -> Path | None:
    """Resolve one declared install target with ordinary ancestry only."""
    try:
        resolved_root = _ordinary_directory(root, root)
    except FileNotFoundError:
        return None
    candidate = resolved_root / relative
    if not candidate.exists():
        return None
    _ordinary_directory(candidate.parent, resolved_root)
    details = os.lstat(candidate)
    attributes = getattr(details, "st_file_attributes", 0)
    if (
        not stat.S_ISREG(details.st_mode)
        or stat.S_ISLNK(details.st_mode)
        or attributes & _REPARSE_POINT
    ):
        raise PermissionError("Profiled application executable is not ordinary")
    resolved = candidate.resolve(strict=True)
    try:
        resolved.relative_to(resolved_root)
    except ValueError as exc:
        raise PermissionError(
            "Profiled application executable escaped its install root"
        ) from exc
    return resolved


def _default_install_roots() -> tuple[Path, ...]:
    """Read machine-wide Program Files roots; never trust ambient env overrides."""
    if os.name != "nt" or winreg is None:
        return ()
    roots: dict[str, Path] = {}
    access = winreg.KEY_READ | getattr(winreg, "KEY_WOW64_64KEY", 0)
    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Windows\CurrentVersion",
            0,
            access,
        ) as key:
            for value_name in ("ProgramFilesDir", "ProgramFilesDir (x86)"):
                try:
                    value = str(winreg.QueryValueEx(key, value_name)[0]).strip()
                except OSError:
                    continue
                if value:
                    path = Path(value)
                    roots[os.path.normcase(str(path))] = path
    except OSError:
        return ()
    return tuple(roots.values())


class WindowsAppRepairController:
    """Profile-driven Windows diagnosis and reversible renderer-cache repair."""

    def __init__(
        self,
        computer_root: Path,
        windows_apps: WindowsAppController,
        *,
        profiles: Iterable[AppRepairProfile] = BUILTIN_APP_REPAIR_PROFILES,
        process_probe: Callable[[Path], list[int]] | None = None,
        network_probe: Callable[[list[int]], int] | None = None,
        graceful_close: Callable[[Path, list[int]], bool] | None = None,
        install_roots: Iterable[Path] | None = None,
    ) -> None:
        self.computer_root = Path(computer_root).resolve()
        self.local_app_data = (
            self.computer_root / "AppData" / "Local"
        ).resolve()
        self.windows_apps = windows_apps
        self.profiles = tuple(profiles)
        self.install_roots = tuple(
            install_roots if install_roots is not None else _default_install_roots()
        )
        self._process_probe = process_probe or self._default_process_probe
        self._network_probe = network_probe or self._default_network_probe
        self._graceful_close = graceful_close or self._default_graceful_close

    def _profile(self, application: str) -> AppRepairProfile:
        requested = " ".join(str(application or "").strip().casefold().split())
        if not requested or len(requested) > 200:
            raise ValueError("Application name must contain 1-200 plain characters")
        matches = [
            profile for profile in self.profiles
            if requested in {
                profile.app_id.casefold(),
                profile.launch_name.casefold(),
                *(alias.casefold() for alias in profile.aliases),
            }
        ]
        if not matches:
            raise FileNotFoundError(
                "No bounded repair profile is installed for that application"
            )
        if len(matches) != 1:
            raise ValueError("Application repair profile is ambiguous")
        return matches[0]

    def _launch_snapshot(self, profile: AppRepairProfile) -> dict[str, Any]:
        trusted_targets = self._profile_install_targets(
            profile.install_relative_paths,
            required=True,
        )
        if len(trusted_targets) != 1:
            raise ValueError("Profiled application has multiple install targets")
        trusted = trusted_targets[0]
        try:
            snapshot = self.windows_apps.launch_snapshot(profile.launch_name)
        except FileNotFoundError:
            snapshot = self._profile_install_snapshot(profile)
        executable = str(snapshot.get("resolved_executable") or "")
        if not executable:
            raise PermissionError("Packaged applications do not support cache repair")
        resolved = Path(executable).resolve(strict=True)
        if os.path.normcase(str(resolved)) != os.path.normcase(str(trusted)):
            raise PermissionError(
                "Installed application does not match its declared trusted install target"
            )
        if resolved.name.casefold() not in {
            name.casefold() for name in profile.executable_names
        }:
            raise PermissionError("Installed executable does not match the repair profile")
        details = trusted.stat()
        return {
            **snapshot,
            "resolved_executable": str(trusted),
            "executable_bytes": details.st_size,
            "executable_mtime_ns": details.st_mtime_ns,
            "executable_sha256": _stable_file_digest(trusted),
        }

    def _profile_install_targets(
        self,
        relative_paths: Iterable[str],
        *,
        required: bool,
    ) -> list[Path]:
        candidates: dict[str, Path] = {}
        for raw_relative in relative_paths:
            relative = _safe_profile_relative(raw_relative, "install target")
            for root in self.install_roots:
                candidate = _ordinary_profile_file(Path(root), relative)
                if candidate is not None:
                    candidates[os.path.normcase(str(candidate))] = candidate
        values = sorted(candidates.values(), key=lambda item: os.path.normcase(str(item)))
        if required and not values:
            raise FileNotFoundError("Profiled application install target was not found")
        return values

    def _profile_install_snapshot(self, profile: AppRepairProfile) -> dict[str, Any]:
        unique = self._profile_install_targets(
            profile.install_relative_paths,
            required=True,
        )
        if not unique:
            raise FileNotFoundError(
                f"Installed application not found: {profile.launch_name}"
            )
        if len(unique) != 1:
            raise ValueError("Profiled application has multiple install targets")
        executable = unique[0]
        if executable.name.casefold() not in {
            name.casefold() for name in profile.executable_names
        }:
            raise PermissionError("Profiled executable name is invalid")
        details = executable.stat()
        return {
            "application": profile.launch_name,
            "resolved_executable": str(executable),
            "executable_bytes": details.st_size,
            "executable_mtime_ns": details.st_mtime_ns,
            "source": "app-repair-profile",
        }

    def _profile_process_ids(
        self,
        profile: AppRepairProfile,
        launch_snapshot: dict[str, Any],
    ) -> tuple[list[int], list[int]]:
        main = Path(str(launch_snapshot["resolved_executable"]))
        main_ids = self._process_probe(main)
        targets = [main]
        targets.extend(self._profile_install_targets(
            profile.process_relative_paths,
            required=False,
        ))
        all_ids: set[int] = set(main_ids)
        seen = {os.path.normcase(str(main))}
        for target in targets:
            key = os.path.normcase(str(target))
            if key in seen:
                continue
            seen.add(key)
            all_ids.update(self._process_probe(target))
        return sorted(set(main_ids)), sorted(all_ids)

    def _version(self, launch_snapshot: dict[str, Any]) -> str:
        return "identity-" + _stable_digest({
            "bytes": launch_snapshot.get("executable_bytes"),
            "mtime_ns": launch_snapshot.get("executable_mtime_ns"),
            "sha256": launch_snapshot.get("executable_sha256"),
            "source": launch_snapshot.get("source"),
        })[:24]

    def _cache_snapshots(self, profile: AppRepairProfile) -> list[dict[str, Any]]:
        if not self.local_app_data.is_dir():
            return []
        results: list[dict[str, Any]] = []
        seen: set[str] = set()
        for pattern in profile.cache_patterns:
            relative_pattern, relative_anchor = _safe_cache_pattern(pattern)
            lexical_anchor = self.local_app_data / relative_anchor
            for candidate in sorted(self.local_app_data.glob(relative_pattern)):
                resolved = _ordinary_directory(candidate, self.local_app_data)
                try:
                    Path(os.path.abspath(candidate)).relative_to(
                        Path(os.path.abspath(lexical_anchor))
                    )
                except ValueError as exc:
                    raise PermissionError(
                        "Application cache target escaped its exact profile prefix"
                    ) from exc
                key = os.path.normcase(str(resolved))
                if key in seen:
                    continue
                seen.add(key)
                manifest = _cache_manifest(resolved)
                results.append({
                    "relative_path": resolved.relative_to(self.local_app_data).as_posix(),
                    "resolved_path": str(resolved),
                    **manifest,
                })
        if len(results) > MAX_CACHE_TARGETS:
            raise ValueError("Repair profile resolved too many cache targets")
        return results

    @staticmethod
    def _powershell() -> Path:
        if os.name != "nt":
            raise RuntimeError("Windows PowerShell is unavailable")
        buffer = ctypes.create_unicode_buffer(32_768)
        length = ctypes.windll.kernel32.GetWindowsDirectoryW(  # type: ignore[attr-defined]
            buffer,
            len(buffer),
        )
        if length <= 0 or length >= len(buffer):
            raise RuntimeError("Windows system directory could not be verified")
        windows_root = Path(buffer.value)
        relative = Path(r"System32\WindowsPowerShell\v1.0\powershell.exe")
        target = _ordinary_profile_file(windows_root, relative)
        if target is None or target.name.casefold() != "powershell.exe":
            raise RuntimeError("Canonical Windows PowerShell was not found")
        return target

    def _default_process_probe(self, executable: Path) -> list[int]:
        if os.name != "nt":
            raise RuntimeError("Windows process verification is unavailable")
        escaped = str(executable).replace("'", "''")
        script = (
            "$target=[IO.Path]::GetFullPath('" + escaped + "');"
            "@(Get-CimInstance Win32_Process -Filter \"Name='"
            + executable.name.replace("'", "''")
            + "'\" | Where-Object { $_.ExecutablePath -and "
            "[IO.Path]::GetFullPath($_.ExecutablePath) -eq $target } | "
            "Select-Object -ExpandProperty ProcessId) | ConvertTo-Json -Compress"
        )
        completed = subprocess.run(
            [
                str(self._powershell()), "-NoLogo", "-NoProfile", "-NonInteractive",
                "-Command", script,
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
            env=WindowsAppController._safe_environment(),
        )
        if completed.returncode != 0 or len(completed.stdout or "") > 100_000:
            raise RuntimeError("Exact application process verification failed")
        try:
            decoded = json.loads(completed.stdout or "[]")
        except json.JSONDecodeError as exc:
            raise RuntimeError("Application process verification was malformed") from exc
        values = decoded if isinstance(decoded, list) else [decoded]
        return sorted({int(value) for value in values if int(value) > 0})[:64]

    def _default_network_probe(self, process_ids: list[int]) -> int:
        if os.name != "nt" or not process_ids:
            return 0
        ids = ",".join(str(int(value)) for value in process_ids)
        script = (
            "$ids=@(" + ids + ");"
            "$rows=@(Get-NetTCPConnection -State Established -ErrorAction SilentlyContinue | "
            "Where-Object { $ids -contains $_.OwningProcess -and $_.RemotePort -eq 443 });"
            "$rows.Count"
        )
        completed = subprocess.run(
            [
                str(self._powershell()), "-NoLogo", "-NoProfile", "-NonInteractive",
                "-Command", script,
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
            env=WindowsAppController._safe_environment(),
        )
        try:
            return max(0, min(int((completed.stdout or "0").strip()), 10_000))
        except ValueError:
            return 0

    def _default_graceful_close(
        self,
        executable: Path,
        process_ids: list[int],
    ) -> bool:
        if os.name != "nt" or not process_ids:
            return not process_ids
        ids = ",".join(str(int(value)) for value in process_ids)
        escaped = str(executable).replace("'", "''")
        script = (
            "$ids=@(" + ids + ");"
            "$target=[IO.Path]::GetFullPath('" + escaped + "');"
            "$all=$true; foreach($id in $ids){"
            "$row=Get-CimInstance Win32_Process -Filter \"ProcessId=$id\" "
            "-ErrorAction SilentlyContinue;"
            "if(-not $row -or -not $row.ExecutablePath -or "
            "[IO.Path]::GetFullPath($row.ExecutablePath) -ne $target){$all=$false;continue};"
            "$p=Get-Process -Id $id -ErrorAction SilentlyContinue;"
            "if(-not $p -or -not $p.CloseMainWindow()){$all=$false}};"
            "if($all){exit 0}else{exit 3}"
        )
        completed = subprocess.run(
            [
                str(self._powershell()), "-NoLogo", "-NoProfile", "-NonInteractive",
                "-Command", script,
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
            env=WindowsAppController._safe_environment(),
        )
        return completed.returncode == 0

    def diagnose(self, application: str, symptom: str = "auto") -> dict[str, Any]:
        normalized_symptom = str(symptom or "auto").strip().casefold()
        if normalized_symptom not in _SYMPTOMS:
            raise ValueError("Unsupported application symptom")
        profile = self._profile(application)
        launch_snapshot = self._launch_snapshot(profile)
        main_process_ids, profile_process_ids = self._profile_process_ids(
            profile,
            launch_snapshot,
        )
        https_connections = self._network_probe(profile_process_ids)
        caches = self._cache_snapshots(profile)
        evidence: dict[str, Any] = {
            "process_running": bool(main_process_ids),
            "cache_bytes": sum(int(item["bytes"]) for item in caches),
        }
        if normalized_symptom == "blank_or_unrendered":
            evidence["reported_render_failure"] = True
        elif normalized_symptom == "authentication_failed":
            evidence["reported_authentication_failure"] = True
        elif normalized_symptom == "connectivity_failed":
            evidence["reported_connectivity_failure"] = True
        elif normalized_symptom == "process_not_running" and not main_process_ids:
            evidence["process_running"] = False
        elif normalized_symptom == "update_required":
            evidence["reported_update_required"] = True

        diagnosis = classify_app_failure(evidence)
        version = self._version(launch_snapshot)
        plan = None
        if diagnosis.category != "render_cache" or caches:
            plan = build_repair_plan(
                {
                    "id": profile.app_id,
                    "name": profile.launch_name,
                    "version": version,
                },
                diagnosis,
                {
                    "process_running": bool(main_process_ids),
                    "cache_paths": [item["relative_path"] for item in caches],
                    "backup_root": profile.backup_root,
                },
            )
        stable_material = {
            "profile": profile.app_id,
            "launch": launch_snapshot,
            "version": version,
            "caches": caches,
            "diagnosis": diagnosis.to_payload(),
            "plan": plan.to_payload() if plan is not None else None,
        }
        plan_id = _stable_digest(stable_material)
        snapshot = {
            "application": profile.app_id,
            "display_name": profile.launch_name,
            "application_version": version,
            "symptom": normalized_symptom,
            "diagnosis": diagnosis.to_payload(),
            "plan": plan.to_payload() if plan is not None else None,
            "plan_id": plan_id,
            "repair_supported": diagnosis.category == "render_cache" and bool(caches),
            "observations": {
                "process_running": bool(main_process_ids),
                "profile_processes_running": len(profile_process_ids),
                "established_https_connections": https_connections,
                "cache_directories": len(caches),
                "cache_bytes": sum(int(item["bytes"]) for item in caches),
                "ui_pixels_inspected": False,
                "cache_contents_read": False,
            },
            "_approval_snapshot": stable_material,
        }
        return snapshot

    def repair_snapshot(
        self,
        application: str,
        plan_id: str,
        symptom: str = "blank_or_unrendered",
    ) -> dict[str, Any]:
        diagnosis = self.diagnose(application, symptom)
        if diagnosis["plan_id"] != str(plan_id or "").strip().casefold():
            raise PermissionError("Application repair plan is stale or changed")
        if diagnosis["repair_supported"] is not True:
            raise PermissionError("This diagnosis has no bounded executable repair")
        stable = dict(diagnosis["_approval_snapshot"])
        caches = list(stable["caches"])
        profile = self._profile(application)
        backup_root = self.local_app_data / _safe_profile_relative(
            profile.backup_root, "backup root"
        )
        destinations: list[dict[str, Any]] = []
        for index, cache in enumerate(caches, start=1):
            source = Path(str(cache["resolved_path"]))
            destination = backup_root / plan_id[:16] / (
                f"{index:02d}-{source.name}"
            )
            if destination.exists():
                raise FileExistsError("The exact repair backup already exists")
            destinations.append({
                "source": str(source),
                "destination": str(destination),
                "metadata_sha256": str(cache["metadata_sha256"]),
                "entries": int(cache["entries"]),
                "bytes": int(cache["bytes"]),
            })
        snapshot = {
            "application": profile.app_id,
            "display_name": profile.launch_name,
            "application_version": diagnosis["application_version"],
            "plan_id": diagnosis["plan_id"],
            "diagnosis_category": diagnosis["diagnosis"]["category"],
            "diagnosis_sha256": _stable_digest(diagnosis["diagnosis"]),
            "launch_snapshot": stable["launch"],
            "moves": destinations,
            "operation": "graceful-close, backup-move, restart, verify",
            "reversible": True,
            "deletes_files": False,
            "changes_security_settings": False,
        }
        sources = [
            Path(str(move["source"])).relative_to(
                self.local_app_data
            ).as_posix()
            for move in destinations
        ]
        backups = [
            Path(str(move["destination"])).relative_to(
                self.local_app_data
            ).as_posix()
            for move in destinations
        ]
        if sum(len(source) + len(backup) for source, backup in zip(
            sources,
            backups,
            strict=True,
        )) > MAX_APPROVAL_SUMMARY_CHARACTERS:
            raise PermissionError("Application repair approval summary is too large")
        snapshot["approval_summary"] = {
            "sources": sources,
            "backups": backups,
            "directories": len(destinations),
            "bytes": sum(int(move["bytes"]) for move in destinations),
            "reversible": True,
            "plan_sha256": _stable_digest(snapshot),
        }
        return snapshot

    def apply(
        self,
        application: str,
        plan_id: str,
        *,
        symptom: str,
        approved: dict[str, Any],
    ) -> dict[str, Any]:
        confirmed = self.repair_snapshot(application, plan_id, symptom)
        if confirmed != approved:
            raise PermissionError("Approved application repair changed before execution")
        executable = Path(str(confirmed["launch_snapshot"]["resolved_executable"]))
        profile = self._profile(application)
        main_process_ids, profile_process_ids = self._profile_process_ids(
            profile,
            confirmed["launch_snapshot"],
        )
        if profile_process_ids:
            if not main_process_ids:
                raise RuntimeError(
                    "A profiled helper process is still running without the main application; no cache was moved"
                )
            if not self._graceful_close(executable, main_process_ids):
                raise RuntimeError(
                    "The application did not accept a graceful close; no cache was moved"
                )
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline:
                _main_ids, remaining_ids = self._profile_process_ids(
                    profile,
                    confirmed["launch_snapshot"],
                )
                if not remaining_ids:
                    break
                time.sleep(0.1)
            _main_ids, remaining_ids = self._profile_process_ids(
                profile,
                confirmed["launch_snapshot"],
            )
            if remaining_ids:
                raise RuntimeError(
                    "The application or a profiled helper is still running; no cache was moved"
                )

        pending_plan = build_repair_plan(
            {
                "id": confirmed["application"],
                "name": confirmed["display_name"],
                "version": confirmed["application_version"],
            },
            Diagnosis(
                category=str(confirmed["diagnosis_category"]),
                confidence=1.0,
                evidence=("Bound to the exact approved diagnosis receipt.",),
            ),
            {
                "cache_paths": [
                    Path(str(move["source"])).relative_to(
                        self.local_app_data
                    ).as_posix()
                    for move in confirmed["moves"]
                ],
                "backup_root": profile.backup_root,
            },
        )

        moved: list[tuple[Path, Path, dict[str, Any]]] = []

        def restore_moved() -> bool:
            restored = True
            for source, destination, expected_manifest in reversed(moved):
                try:
                    if destination.exists() and source.exists():
                        restored = False
                        continue
                    if destination.exists() and not source.exists():
                        os.rename(destination, source)
                    if destination.exists() or not source.exists():
                        restored = False
                        continue
                    restored_source = _ordinary_directory(
                        source,
                        self.local_app_data,
                    )
                    if _cache_manifest(restored_source) != expected_manifest:
                        restored = False
                except OSError:
                    restored = False
            return restored

        try:
            for move in confirmed["moves"]:
                source = _ordinary_directory(
                    Path(str(move["source"])), self.local_app_data
                )
                manifest = _cache_manifest(source)
                if manifest["metadata_sha256"] != move["metadata_sha256"]:
                    raise PermissionError("Application cache changed before backup")
                requested_destination = Path(str(move["destination"]))
                destination_parent = _ensure_ordinary_directory_chain(
                    requested_destination.parent,
                    self.local_app_data,
                )
                destination = destination_parent / requested_destination.name
                if destination.exists() or destination.is_symlink():
                    raise FileExistsError("The exact repair backup already exists")
                # The supported host is Windows, where rename refuses to
                # replace an existing destination. Never use os.replace here.
                os.rename(source, destination)
                moved.append((source, destination, manifest))
                verified_destination = _ordinary_directory(
                    destination,
                    self.local_app_data,
                )
                if _cache_manifest(verified_destination) != manifest:
                    raise RuntimeError("Application cache backup verification failed")
        except Exception:
            if not restore_moved():
                raise RuntimeError(
                    "Application cache backup failed and automatic rollback was incomplete"
                )
            raise

        try:
            launch_result = self.windows_apps.launch_approved_snapshot(
                approved=dict(confirmed["launch_snapshot"]),
            )
        except Exception as exc:
            _main_ids, remaining_ids = self._profile_process_ids(
                profile,
                confirmed["launch_snapshot"],
            )
            if remaining_ids:
                raise RuntimeError(
                    "Application restart failed after backup; rollback was withheld because a profiled process is running"
                ) from exc
            if not restore_moved():
                raise RuntimeError(
                    "Application restart failed and automatic cache rollback was incomplete"
                ) from exc
            raise RuntimeError(
                "Application restart failed; the cache backup was restored"
            ) from exc
        # Execution evidence is deliberately incomplete: spawning a process or
        # seeing a window title is not visual proof that the UI rendered.
        outcome = complete_repair(
            pending_plan,
            {
                "approval_authorized": True,
                "backup_created": bool(moved),
                "source_moved": bool(moved),
                "restart_observed": bool(launch_result.get("launched")),
                "ui_rendered": False,
                "health_check_passed": False,
            },
        )
        return {
            "application": confirmed["application"],
            "plan_id": confirmed["plan_id"],
            "repair_applied": True,
            "moved_cache_directories": len(moved),
            "backup_locations": [
                destination.relative_to(self.local_app_data).as_posix()
                for _source, destination, _manifest in moved
            ],
            "source_deleted": False,
            "restart": {
                "application": launch_result.get("application"),
                "launched": launch_result.get("launched") is True,
                "pid": launch_result.get("pid"),
            },
            "verification_status": "awaiting_visual_and_health_evidence",
            "outcome": outcome.to_payload(),
            "lesson_stored": False,
        }
