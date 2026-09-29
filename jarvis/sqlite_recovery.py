from __future__ import annotations

import hashlib
import os
import sqlite3
import stat
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4


@dataclass(frozen=True)
class SQLiteBackupEvidence:
    """Sanitized evidence from an online-backup and isolated-restore drill."""

    checks: dict[str, bool]
    backup_sha256: str
    restored_sha256: str
    schema_version: int
    created_at_utc: str

    @property
    def passed(self) -> bool:
        return bool(self.checks) and all(self.checks.values())


def _readonly_connection(path: Path) -> sqlite3.Connection:
    # A URI is required for SQLite's read-only enforcement. quote() keeps Windows
    # drive separators intact while escaping URI metacharacters in path segments.
    uri_path = quote(path.resolve().as_posix(), safe="/:")
    return sqlite3.connect(f"file:{uri_path}?mode=ro&immutable=1", uri=True)


def _file_sha256(path: Path) -> str:
    """Hash a database incrementally without exposing or buffering its content."""

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _ordinary_directory(path: Path) -> Path:
    """Resolve an existing directory without accepting links or reparse points."""
    details = path.lstat()
    attributes = getattr(details, "st_file_attributes", 0)
    if (
        not stat.S_ISDIR(details.st_mode)
        or stat.S_ISLNK(details.st_mode)
        or attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    ):
        raise ValueError("recovery artifact directory must be an ordinary directory")
    resolved = path.resolve(strict=True)
    if Path(os.path.abspath(path)) != resolved:
        raise ValueError("recovery artifact directory must not traverse a link")
    return resolved


def _assert_artifacts_absent(path: Path) -> None:
    """An orphan journal/WAL is another caller's data even if its main file is absent."""
    for suffix in ("", "-journal", "-shm", "-wal"):
        try:
            Path(f"{path}{suffix}").lstat()
        except FileNotFoundError:
            continue
        raise FileExistsError("recovery artifacts and sidecars must not already exist")


def _reserve_database(path: Path) -> None:
    """Reserve a new artifact inside a caller-owned ordinary directory."""
    _ordinary_directory(path.parent)
    _assert_artifacts_absent(path)
    # Exclusive creation prevents accidental reuse. The caller must provide a
    # private directory; this helper does not claim isolation from another process
    # already holding the same OS-user authority.
    with path.open("xb"):
        pass


def remove_sqlite_artifacts(path: Path) -> None:
    """Remove one caller-owned database artifact and its exact SQLite sidecars."""

    database_path = Path(path)
    database_path.unlink(missing_ok=True)
    for suffix in ("-journal", "-shm", "-wal"):
        Path(f"{database_path}{suffix}").unlink(missing_ok=True)


def verify_online_backup_and_restore(
    source: sqlite3.Connection,
    *,
    temporary_root: Path,
    expected_schema_version: int,
    backup_path: Path | None = None,
) -> SQLiteBackupEvidence:
    """Back up a live connection and validate a separate restored database.

    The caller owns ``source``. Restore scratch space must be a caller-created
    temporary directory. An optional backup path may retain the verified online
    backup in another caller-owned ordinary directory. Existing artifacts are never
    reused, and a failed or unverified backup is never published at the final path.
    """

    if (isinstance(expected_schema_version, bool) or not isinstance(expected_schema_version, int)
            or expected_schema_version < 0):
        raise ValueError("expected_schema_version must be a non-negative integer")
    # sqlite3.backup on a connection with its own pending write transaction can
    # wait indefinitely. Never resolve this by committing/rolling back caller state.
    if getattr(source, "in_transaction", False):
        raise ValueError("source must have no pending transaction; commit or roll back explicitly")
    root = _ordinary_directory(Path(temporary_root))
    requested = Path(backup_path) if backup_path is not None else root / "online-backup.db"
    requested_backup_path = (
        _ordinary_directory(requested.parent) / requested.name
    )
    restored_path = root / "isolated-restore.db"
    if requested_backup_path == restored_path:
        raise ValueError("backup and restore artifacts must have different paths")
    _assert_artifacts_absent(requested_backup_path)
    _assert_artifacts_absent(restored_path)
    staging_backup_path = requested_backup_path.with_name(
        f".{requested_backup_path.name}.partial-{uuid4().hex}"
    )

    checks: dict[str, bool] = {
        "source_integrity_check": source.execute(
            "PRAGMA integrity_check"
        ).fetchone()[0] == "ok",
        "source_foreign_key_check": source.execute(
            "PRAGMA foreign_key_check"
        ).fetchone() is None,
    }

    staging_owned = False
    try:
        _reserve_database(staging_backup_path)
        staging_owned = True
        backup = sqlite3.connect(staging_backup_path)
        try:
            created_at_utc = datetime.now(timezone.utc).isoformat()
            source.backup(backup)
        finally:
            backup.close()

        with closing(_readonly_connection(staging_backup_path)) as backup_readonly:
            backup_schema = int(
                backup_readonly.execute("PRAGMA user_version").fetchone()[0]
            )
            checks.update({
                "backup_integrity_check": backup_readonly.execute(
                    "PRAGMA integrity_check"
                ).fetchone()[0] == "ok",
                "backup_foreign_key_check": backup_readonly.execute(
                    "PRAGMA foreign_key_check"
                ).fetchone() is None,
                "backup_schema_current": backup_schema == expected_schema_version,
            })
            backup_sha256 = _file_sha256(staging_backup_path)
            _reserve_database(restored_path)
            restored = sqlite3.connect(restored_path)
            try:
                backup_readonly.backup(restored)
            finally:
                restored.close()

        with closing(_readonly_connection(restored_path)) as restored_readonly:
            restored_schema = int(
                restored_readonly.execute("PRAGMA user_version").fetchone()[0]
            )
            checks.update({
                "restore_integrity_check": restored_readonly.execute(
                    "PRAGMA integrity_check"
                ).fetchone()[0] == "ok",
                "restore_foreign_key_check": restored_readonly.execute(
                    "PRAGMA foreign_key_check"
                ).fetchone() is None,
                "restore_schema_current": restored_schema == expected_schema_version,
            })
            restored_sha256 = _file_sha256(restored_path)

        checks["restore_matches_backup"] = restored_sha256 == backup_sha256
        evidence = SQLiteBackupEvidence(
            checks=checks,
            backup_sha256=backup_sha256,
            restored_sha256=restored_sha256,
            schema_version=restored_schema,
            created_at_utc=created_at_utc,
        )
        if evidence.passed:
            _assert_artifacts_absent(requested_backup_path)
            # Windows rename is atomic and refuses an existing destination. POSIX
            # rename replaces, so use an exclusive hard link there instead.
            if os.name == "nt":
                os.rename(staging_backup_path, requested_backup_path)
            else:
                os.link(staging_backup_path, requested_backup_path)
        return evidence
    finally:
        if staging_owned:
            remove_sqlite_artifacts(staging_backup_path)
