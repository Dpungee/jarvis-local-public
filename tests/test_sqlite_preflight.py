from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jarvis.long_horizon import LongHorizonStateError, LongHorizonStore
from jarvis.memory import Memory
from jarvis.public_presence_store import PublicPresenceStore, PublicPresenceStoreError
from jarvis.relationship_memory import RelationshipMemory, RelationshipMemoryError
from jarvis.sqlite_preflight import inspection_connection
from tests.sqlite_crash_fixture import (
    create_future_schema_in_hot_wal,
    snapshot_directory,
)


class SQLitePreflightTests(unittest.TestCase):
    def test_hot_wal_inspection_rebuilds_private_shm_instead_of_copying_live_shm(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "state.db"
            create_future_schema_in_hot_wal(path, user_version=77)
            before = snapshot_directory(path)
            copied_sources: list[Path] = []
            real_copy2 = shutil.copy2

            def recording_copy(source, destination, *args, **kwargs):
                copied_sources.append(Path(source))
                return real_copy2(source, destination, *args, **kwargs)

            with patch(
                "jarvis.sqlite_preflight.shutil.copy2",
                side_effect=recording_copy,
            ):
                with inspection_connection(path) as db:
                    version = int(db.execute("PRAGMA user_version").fetchone()[0])

            self.assertEqual(version, 77)
            self.assertTrue(any(str(item).endswith("-wal") for item in copied_sources))
            self.assertFalse(any(str(item).endswith("-shm") for item in copied_sources))
            self.assertEqual(snapshot_directory(path), before)

    def test_hot_wal_checkpoint_race_restarts_from_the_new_main_file(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "state.db"
            create_future_schema_in_hot_wal(path, user_version=78)
            wal_path = Path(f"{path}-wal")
            real_copy2 = shutil.copy2
            checkpoint_snapshot: dict[str, bytes] | None = None

            def checkpoint_before_wal_copy(source, destination, *args, **kwargs):
                nonlocal checkpoint_snapshot
                if Path(source) == wal_path and checkpoint_snapshot is None:
                    checkpoint = sqlite3.connect(path)
                    try:
                        checkpoint.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
                    finally:
                        checkpoint.close()
                    checkpoint_snapshot = snapshot_directory(path)
                    raise FileNotFoundError(wal_path)
                return real_copy2(source, destination, *args, **kwargs)

            with patch(
                "jarvis.sqlite_preflight.shutil.copy2",
                side_effect=checkpoint_before_wal_copy,
            ):
                with inspection_connection(path) as db:
                    version = int(db.execute("PRAGMA user_version").fetchone()[0])

            self.assertEqual(version, 78)
            self.assertIsNotNone(checkpoint_snapshot)
            self.assertEqual(snapshot_directory(path), checkpoint_snapshot)

    def test_dangling_database_links_never_create_targets_or_sidecars(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            probe = root / "probe-link"
            try:
                os.symlink(root / "missing-probe-target", probe)
                probe.unlink()
            except OSError:
                self.skipTest("file symlinks are unavailable on this host")

            cases = (
                ("memory.db", lambda path: Memory(path), RuntimeError),
                (
                    "relationship.db",
                    lambda path: RelationshipMemory(path),
                    RelationshipMemoryError,
                ),
                (
                    "public_presence.db",
                    lambda path: PublicPresenceStore(path),
                    PublicPresenceStoreError,
                ),
                (
                    "long-horizon.db",
                    lambda path: LongHorizonStore(path, project_id=1),
                    LongHorizonStateError,
                ),
            )
            for name, constructor, error in cases:
                with self.subTest(name=name):
                    case_root = root / name.replace(".db", "")
                    case_root.mkdir()
                    target = case_root / "outside" / "created.db"
                    target.parent.mkdir()
                    link = case_root / name
                    os.symlink(target, link)

                    with self.assertRaises(error):
                        constructor(link)

                    self.assertFalse(target.exists())
                    for suffix in (
                        "-wal",
                        "-shm",
                        "-journal",
                        ".long-horizon.key",
                    ):
                        self.assertFalse(Path(f"{target}{suffix}").exists())

    def _database(self, root: Path) -> Path:
        path = root / "state.db"
        db = sqlite3.connect(path)
        try:
            db.execute("CREATE TABLE state(value TEXT)")
            db.commit()
        finally:
            db.close()
        return path

    def test_nonregular_reserved_sidecars_fail_with_no_or_zero_wal(self) -> None:
        cases = (
            ("-wal", False),
            ("-shm", False),
            ("-shm", True),
            ("-journal", False),
        )
        for suffix, zero_wal in cases:
            with self.subTest(suffix=suffix, zero_wal=zero_wal):
                with tempfile.TemporaryDirectory() as temp:
                    path = self._database(Path(temp))
                    if zero_wal:
                        Path(f"{path}-wal").touch()
                    Path(f"{path}{suffix}").mkdir()
                    with self.assertRaisesRegex(OSError, "ordinary file"):
                        with inspection_connection(path):
                            self.fail("invalid sidecar must not be opened")

    def test_symlinked_wal_and_shm_fail_closed_when_supported(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            probe = root / "probe-link"
            victim = root / "victim.bin"
            victim.write_bytes(b"do not touch")
            try:
                os.symlink(victim, probe)
                probe.unlink()
            except OSError:
                self.skipTest("file symlinks are unavailable on this host")

            for suffix, zero_wal in (
                ("-wal", False),
                ("-shm", False),
                ("-shm", True),
                ("-journal", False),
            ):
                with self.subTest(suffix=suffix, zero_wal=zero_wal):
                    case_root = root / f"case-{suffix[1:]}-{int(zero_wal)}"
                    case_root.mkdir()
                    path = self._database(case_root)
                    if zero_wal:
                        Path(f"{path}-wal").touch()
                    os.symlink(victim, Path(f"{path}{suffix}"))
                    with self.assertRaisesRegex(OSError, "ordinary file"):
                        with inspection_connection(path):
                            self.fail("symlinked sidecar must not be opened")
                    self.assertEqual(victim.read_bytes(), b"do not touch")


if __name__ == "__main__":
    unittest.main()
