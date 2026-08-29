from __future__ import annotations

import ctypes
import os
import shutil
import stat
from pathlib import Path

_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


def _is_link_or_reparse(details: os.stat_result) -> bool:
    return stat.S_ISLNK(details.st_mode) or bool(
        getattr(details, "st_file_attributes", 0) & _REPARSE_POINT
    )


def _ordinary_executable(path: Path, *, allow_os_hardlinks: bool = False) -> Path:
    """Return one stable executable path without link or hard-link ambiguity."""
    candidate = Path(os.path.abspath(path))
    current = Path(candidate.anchor)
    for part in candidate.parts[1:]:
        current /= part
        details = os.lstat(current)
        if _is_link_or_reparse(details):
            raise PermissionError("Trusted utility paths may not traverse links")
    details = os.lstat(candidate)
    if (
        not stat.S_ISREG(details.st_mode)
        or _is_link_or_reparse(details)
        or (
            not allow_os_hardlinks
            and int(getattr(details, "st_nlink", 1)) > 1
        )
    ):
        raise PermissionError("Trusted system utilities must be ordinary files")
    return candidate.resolve(strict=True)


def windows_directory() -> Path:
    """Resolve the real Windows directory through the OS, never an environment variable."""
    if os.name != "nt":
        raise OSError("Windows system utilities are unavailable on this platform")
    buffer = ctypes.create_unicode_buffer(32_768)
    length = ctypes.windll.kernel32.GetWindowsDirectoryW(buffer, len(buffer))
    if length <= 0 or length >= len(buffer):
        raise OSError("The canonical Windows directory could not be resolved")
    root = Path(buffer.value)
    details = os.lstat(root)
    if not stat.S_ISDIR(details.st_mode) or _is_link_or_reparse(details):
        raise PermissionError("The canonical Windows directory is not ordinary")
    return root.resolve(strict=True)


def windows_system_executable(*parts: str) -> Path:
    """Resolve a fixed executable below the OS-reported Windows directory."""
    if not parts or any(
        not part or Path(part).name != part or part in {".", ".."}
        for part in parts
    ):
        raise ValueError("Windows utility path components must be fixed names")
    root = windows_directory()
    current = root
    for part in parts:
        current /= part
        details = os.lstat(current)
        if _is_link_or_reparse(details):
            raise PermissionError("Windows utility paths may not traverse links")
    # Windows services system binaries through the component store, so a
    # canonical System32 file can legitimately have multiple hard links. Its
    # identity is anchored by GetWindowsDirectoryW plus a link-free path.
    executable = _ordinary_executable(current, allow_os_hardlinks=True)
    try:
        executable.relative_to(root)
    except ValueError:
        raise PermissionError("Windows utility escaped the canonical system directory") from None
    return executable


def _trusted_install_roots() -> tuple[Path, ...]:
    """Return OS-administered roots that an ordinary user cannot redirect."""
    if os.name != "nt":
        return tuple(
            root.resolve(strict=True)
            for root in (Path("/usr/bin"), Path("/usr/sbin"))
            if root.exists()
        )

    roots: list[Path] = [windows_directory()]
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Windows\CurrentVersion",
        ) as key:
            for value_name in ("ProgramFilesDir", "ProgramFilesDir (x86)"):
                try:
                    value, _kind = winreg.QueryValueEx(key, value_name)
                except OSError:
                    continue
                path = Path(str(value)).resolve(strict=True)
                if path not in roots:
                    roots.append(path)
    except (ImportError, OSError):
        pass
    return tuple(roots)


def trusted_install_file(
    path: Path,
    *,
    prohibited_roots: tuple[Path, ...] = (),
) -> Path | None:
    """Resolve one ordinary support file only below an OS-administered install root."""
    try:
        candidate = _ordinary_executable(Path(path))
    except (OSError, PermissionError):
        return None
    for root in prohibited_roots:
        try:
            candidate.relative_to(Path(root).resolve(strict=True))
        except (OSError, ValueError):
            continue
        return None
    for root in _trusted_install_roots():
        try:
            candidate.relative_to(root)
        except ValueError:
            continue
        return candidate
    return None


def trusted_path_executable(
    name: str,
    *,
    prohibited_roots: tuple[Path, ...] = (),
) -> Path | None:
    """Resolve one optional utility only from an OS-administered install root."""
    discovered = shutil.which(name)
    if not discovered:
        return None
    try:
        # Git for Windows legitimately hard-links cmd/git.exe and bin/git.exe.
        # The absolute, link-free PATH and prohibited-root checks below remove
        # search-path substitution without disabling that signed installation.
        executable = _ordinary_executable(
            Path(discovered), allow_os_hardlinks=(os.name == "nt")
        )
    except (OSError, PermissionError):
        return None
    for root in prohibited_roots:
        try:
            executable.relative_to(Path(root).resolve(strict=True))
        except (OSError, ValueError):
            continue
        return None
    for root in _trusted_install_roots():
        try:
            executable.relative_to(root)
        except ValueError:
            continue
        return executable
    return None
