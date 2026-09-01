from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def create_hot_future_database(
    path: Path, *, user_version: int, application_id: int = 0, public_marker: bool = False
) -> None:
    script = r'''
import os, sqlite3, sys
path, version, app_id, marker = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), sys.argv[4] == "1"
db = sqlite3.connect(path)
db.execute("PRAGMA journal_mode=DELETE")
db.execute("CREATE TABLE seed(id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
db.execute("INSERT INTO seed VALUES(1, 'committed')")
if marker:
    db.execute("CREATE TABLE public_schema(singleton INTEGER PRIMARY KEY, version INTEGER NOT NULL, migrated_at REAL NOT NULL)")
    db.execute("INSERT INTO public_schema VALUES(1, ?, 0)", (version,))
db.execute(f"PRAGMA application_id={app_id}")
db.execute(f"PRAGMA user_version={version}")
db.commit()
db.execute("BEGIN IMMEDIATE")
db.execute("UPDATE seed SET value='uncommitted' WHERE id=1")
os._exit(0)
'''
    subprocess.run(
        [sys.executable, "-c", script, str(path), str(user_version), str(application_id), "1" if public_marker else "0"],
        check=True,
    )
    assert Path(f"{path}-journal").exists()


def snapshot_directory(path: Path) -> dict[str, bytes]:
    return {item.name: item.read_bytes() for item in path.parent.iterdir() if item.is_file()}


def create_future_schema_in_hot_wal(path: Path, *, user_version: int) -> None:
    """Crash with a committed future schema visible only through a WAL."""
    script = r'''
import os, sqlite3, sys
path, version = sys.argv[1], int(sys.argv[2])
db = sqlite3.connect(path)
db.execute("PRAGMA journal_mode=WAL")
db.execute("PRAGMA wal_autocheckpoint=0")
db.execute("BEGIN IMMEDIATE")
db.execute("CREATE TABLE future_wal_only(value TEXT)")
db.execute(f"PRAGMA user_version={version}")
db.commit()
os._exit(0)
'''
    subprocess.run(
        [sys.executable, "-c", script, str(path), str(user_version)], check=True
    )
    assert Path(f"{path}-wal").stat().st_size > 0
