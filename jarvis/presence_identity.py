from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .config import SOURCE_ROOT


_INSTALLATION_DOMAIN = b"jarvis-presence-installation-v1\0"
_LAUNCH_MODES = frozenset({"manual", "managed", "direct"})


def normalized_install_path(value: str | os.PathLike[str]) -> str:
    """Return one stable, absolute representation for installation identity."""

    resolved = str(Path(value).expanduser().resolve())
    return os.path.normcase(resolved) if os.name == "nt" else resolved


def presence_installation_id(
    *,
    source_root: str | os.PathLike[str] = SOURCE_ROOT,
    python_executable: str | os.PathLike[str] = sys.executable,
) -> str:
    """Bind Presence to both its installed source and its Python runtime."""

    payload = (
        normalized_install_path(source_root)
        + "\0"
        + normalized_install_path(python_executable)
    ).encode("utf-8", errors="strict")
    return hashlib.sha256(_INSTALLATION_DOMAIN + payload).hexdigest()


def presence_process_identity(runtime_epoch: str) -> dict[str, Any]:
    """Describe the exact Presence process serving a health response."""

    launch_mode = os.getenv("JARVIS_PRESENCE_LAUNCH_MODE", "direct").strip().casefold()
    if launch_mode not in _LAUNCH_MODES:
        launch_mode = "direct"
    source_root = normalized_install_path(SOURCE_ROOT)
    python_executable = normalized_install_path(sys.executable)
    return {
        "version": __version__,
        "source_root": source_root,
        "python_executable": python_executable,
        "installation_id": presence_installation_id(
            source_root=source_root,
            python_executable=python_executable,
        ),
        "process_id": os.getpid(),
        "runtime_epoch": str(runtime_epoch),
        "launch_mode": launch_mode,
    }
