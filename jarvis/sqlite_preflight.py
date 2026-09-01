"""Read-only SQLite inspection helpers that never perform crash recovery."""

from __future__ import annotations

import sqlite3
import shutil
import stat
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


_WINDOWS_REPARSE_POINT = 0x400
_SNAPSHOT_ATTEMPTS = 5


def _ordinary_file(path: Path, *, required: bool) -> bool:
    """Reject links, reparse points, and non-files before copying state."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        if required:
            raise
        return False
    attributes = int(getattr(info, "st_file_attributes", 0))
    if (
        stat.S_ISLNK(info.st_mode)
        or attributes & _WINDOWS_REPARSE_POINT
        or not stat.S_ISREG(info.st_mode)
    ):
        raise OSError("database state path is not an ordinary file")
    return True


def validate_database_path(path: Path) -> bool:
    """Validate a database target and all reserved sidecar names without I/O."""
    candidate = Path(path)
    exists = _ordinary_file(candidate, required=False)
    for suffix in ("-wal", "-shm", "-journal"):
        _ordinary_file(Path(f"{candidate}{suffix}"), required=False)
    return exists


def immutable_connection(path: Path) -> sqlite3.Connection:
    """Open an existing ordinary database without recovery or sidecar writes."""
    candidate = Path(path)
    if not validate_database_path(candidate):
        raise FileNotFoundError(candidate)
    uri = f"{candidate.resolve(strict=True).as_uri()}?mode=ro&immutable=1"
    return sqlite3.connect(uri, uri=True, timeout=5.0)


@contextmanager
def inspection_connection(path: Path) -> Iterator[sqlite3.Connection]:
    """Inspect current DB state while recovering sidecars only on a private copy."""
    candidate = Path(path)
    wal_path = Path(f"{candidate}-wal")
    last_race: OSError | None = None
    for _attempt in range(_SNAPSHOT_ATTEMPTS):
        if not validate_database_path(candidate):
            raise FileNotFoundError(candidate)
        try:
            wal_exists = _ordinary_file(wal_path, required=False)
            wal_size = wal_path.stat().st_size if wal_exists else 0
        except (FileNotFoundError, PermissionError) as exc:
            # A live checkpoint can remove or briefly lock the WAL after the
            # sidecar validation. Restart from a fresh main-file inspection;
            # never combine an older main copy with a vanished/new WAL.
            last_race = exc
            continue
        if not wal_exists or wal_size == 0:
            db = immutable_connection(candidate)
            try:
                yield db
            finally:
                db.close()
            return
        with tempfile.TemporaryDirectory(prefix="jarvis-sqlite-preflight-") as temp:
            copied = Path(temp) / candidate.name
            try:
                shutil.copy2(candidate, copied)
                # WAL is append-only between checkpoints. Copy it after the
                # main file; a checkpoint race discards this attempt entirely.
                shutil.copy2(wal_path, Path(f"{copied}-wal"))
            except (FileNotFoundError, PermissionError) as exc:
                last_race = exc
                continue
            # Never copy the shared-memory index. SQLite can rebuild it from
            # the private main/WAL snapshot, while a live writer may
            # legitimately remove the original -shm at any time.
            db = sqlite3.connect(str(copied), timeout=5.0)
            try:
                yield db
            finally:
                db.close()
            return
    raise OSError(
        "database state changed repeatedly during safe inspection"
    ) from last_race
