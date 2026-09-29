from __future__ import annotations

import sqlite3
import stat
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from jarvis.sqlite_recovery import verify_online_backup_and_restore


class SQLiteRecoveryTests(unittest.TestCase):
    def test_orphan_sidecars_are_refused_and_preserved_at_every_artifact_path(self) -> None:
        for name in ("online-backup.db", "isolated-restore.db", ".online-backup.db.partial-fixed"):
            for suffix in ("-journal", "-shm", "-wal"):
                with self.subTest(name=name, suffix=suffix), tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    recovery = root / "recovery"
                    recovery.mkdir()
                    sidecar = recovery / (name + suffix)
                    sidecar.write_bytes(b"preexisting-private-sidecar")
                    with closing(self._database(root)) as source, \
                            patch("jarvis.sqlite_recovery.uuid4", return_value=SimpleNamespace(hex="fixed")):
                        with self.assertRaises(FileExistsError):
                            verify_online_backup_and_restore(source, temporary_root=recovery,
                                                             expected_schema_version=7)
                    self.assertEqual(list(recovery.iterdir()), [sidecar])
                    self.assertEqual(sidecar.read_bytes(), b"preexisting-private-sidecar")

    def test_final_artifact_or_sidecar_created_during_backup_is_preserved(self) -> None:
        for suffix in ("", "-journal", "-shm", "-wal"):
            with self.subTest(suffix=suffix), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                recovery, retained = root / "recovery", root / "retained"
                recovery.mkdir()
                retained.mkdir()
                final = retained / "backup.db"
                foreign = Path(str(final) + suffix)
                with closing(self._database(root)) as source:
                    def backup(destination, foreign=foreign):
                        source.backup(destination)
                        foreign.write_bytes(b"created-by-another-caller")

                    proxy = SimpleNamespace(execute=source.execute, backup=backup)
                    with self.assertRaises(FileExistsError):
                        verify_online_backup_and_restore(proxy, temporary_root=recovery,
                                                         expected_schema_version=7, backup_path=final)
                self.assertEqual(list(retained.iterdir()), [foreign])
                self.assertEqual(foreign.read_bytes(), b"created-by-another-caller")

    def test_pending_source_transaction_is_not_committed_rolled_back_or_waited_on(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            recovery = root / "recovery"
            recovery.mkdir()
            with closing(self._database(root)) as source:
                source.execute("INSERT INTO parent VALUES (2, 'pending-not-backed-up')")
                with self.assertRaisesRegex(ValueError, "pending transaction"):
                    verify_online_backup_and_restore(source, temporary_root=recovery, expected_schema_version=7)
                self.assertTrue(source.in_transaction)
                self.assertEqual(source.execute("SELECT value FROM parent WHERE id=2").fetchone()[0],
                                 "pending-not-backed-up")
                source.rollback()
                self.assertIsNone(source.execute("SELECT value FROM parent WHERE id=2").fetchone())
            self.assertEqual(list(recovery.iterdir()), [])

    def test_schema_version_requires_an_actual_nonnegative_integer(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with closing(self._database(root)) as source:
                for version in (True, False, -1, 7.0, "7", None):
                    with self.subTest(version=version), self.assertRaises(ValueError):
                        verify_online_backup_and_restore(source, temporary_root=root,
                                                         expected_schema_version=version)

    def test_backup_and_restore_paths_must_be_distinct(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with closing(self._database(root)) as source:
                with self.assertRaisesRegex(ValueError, "different paths"):
                    verify_online_backup_and_restore(source, temporary_root=root, expected_schema_version=7,
                                                     backup_path=root / "isolated-restore.db")

    def test_reparse_root_is_refused_before_database_artifacts_are_created(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            recovery = root / "recovery"
            recovery.mkdir()
            original = Path.lstat

            def lstat(path):
                if path == recovery:
                    return SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0x400)
                return original(path)

            with closing(self._database(root)) as source, patch.object(Path, "lstat", lstat):
                with self.assertRaisesRegex(ValueError, "ordinary directory"):
                    verify_online_backup_and_restore(source, temporary_root=recovery, expected_schema_version=7)
            self.assertEqual(list(recovery.iterdir()), [])

    def test_directory_resolution_cannot_cross_a_link(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            recovery, other = root / "recovery", root / "other"
            recovery.mkdir()
            other.mkdir()
            original = Path.resolve

            def resolve(path, **kwargs):
                return other if path == recovery else original(path, **kwargs)

            with closing(self._database(root)) as source, patch.object(Path, "resolve", resolve):
                with self.assertRaisesRegex(ValueError, "traverse a link"):
                    verify_online_backup_and_restore(source, temporary_root=recovery, expected_schema_version=7)
            self.assertEqual(list(recovery.iterdir()), [])
            self.assertEqual(list(other.iterdir()), [])

    def test_colliding_staging_file_and_sidecars_are_never_removed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            recovery = root / "recovery"
            recovery.mkdir()
            staging = recovery / ".online-backup.db.partial-fixed"
            artifacts = [staging, Path(str(staging) + "-wal")]
            for artifact in artifacts:
                artifact.write_bytes(b"not-owned-by-this-drill")
            with closing(self._database(root)) as source, \
                    patch("jarvis.sqlite_recovery.uuid4", return_value=SimpleNamespace(hex="fixed")):
                with self.assertRaises(FileExistsError):
                    verify_online_backup_and_restore(source, temporary_root=recovery,
                                                     expected_schema_version=7)
            for artifact in artifacts:
                self.assertEqual(artifact.read_bytes(), b"not-owned-by-this-drill")

    def test_recovery_enforcement_is_not_self_repairable(self) -> None:
        from jarvis.self_diagnosis import _IMMUTABLE_REPAIR_FILES, _repair_path_reason
        self.assertIn("jarvis/sqlite_recovery.py", _IMMUTABLE_REPAIR_FILES)
        self.assertIsNotNone(_repair_path_reason("jarvis/sqlite_recovery.py"))

    def _database(self, root: Path, *, schema_version: int = 7) -> sqlite3.Connection:
        connection = sqlite3.connect(root / "live.db")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.executescript(
            """
            CREATE TABLE parent(id INTEGER PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE child(
                id INTEGER PRIMARY KEY,
                parent_id INTEGER NOT NULL REFERENCES parent(id)
            );
            INSERT INTO parent(id, value) VALUES (1, 'committed-in-wal');
            INSERT INTO child(id, parent_id) VALUES (1, 1);
            """
        )
        connection.execute(f"PRAGMA user_version={schema_version}")
        connection.commit()
        return connection

    def test_online_backup_restores_committed_wal_state_and_matches_digest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            recovery = root / "recovery"
            recovery.mkdir()
            with closing(self._database(root)) as source:
                evidence = verify_online_backup_and_restore(
                    source,
                    temporary_root=recovery,
                    expected_schema_version=7,
                )

            self.assertTrue(evidence.passed)
            self.assertTrue(all(evidence.checks.values()))
            self.assertEqual(evidence.backup_sha256, evidence.restored_sha256)
            self.assertEqual(len(evidence.backup_sha256), 64)
            self.assertEqual(evidence.schema_version, 7)
            restored = sqlite3.connect(recovery / "isolated-restore.db")
            try:
                self.assertEqual(
                    restored.execute("SELECT value FROM parent WHERE id=1").fetchone()[0],
                    "committed-in-wal",
                )
            finally:
                restored.close()

    def test_foreign_key_corruption_fails_every_copy_without_being_repaired(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            recovery = root / "recovery"
            recovery.mkdir()
            with closing(self._database(root)) as source:
                source.execute("PRAGMA foreign_keys=OFF")
                source.execute("INSERT INTO child(id, parent_id) VALUES (2, 999)")
                source.commit()
                evidence = verify_online_backup_and_restore(
                    source,
                    temporary_root=recovery,
                    expected_schema_version=7,
                )

            self.assertFalse(evidence.passed)
            self.assertFalse(evidence.checks["source_foreign_key_check"])
            self.assertFalse(evidence.checks["backup_foreign_key_check"])
            self.assertFalse(evidence.checks["restore_foreign_key_check"])
            self.assertTrue(evidence.checks["restore_matches_backup"])

    def test_schema_mismatch_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            recovery = root / "recovery"
            recovery.mkdir()
            with closing(self._database(root, schema_version=8)) as source:
                evidence = verify_online_backup_and_restore(
                    source,
                    temporary_root=recovery,
                    expected_schema_version=7,
                )

            self.assertFalse(evidence.passed)
            self.assertFalse(evidence.checks["backup_schema_current"])
            self.assertFalse(evidence.checks["restore_schema_current"])

    def test_existing_artifact_is_never_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            recovery = root / "recovery"
            recovery.mkdir()
            existing = recovery / "online-backup.db"
            existing.write_bytes(b"keep-me")
            with closing(self._database(root)) as source:
                with self.assertRaisesRegex(FileExistsError, "must not already exist"):
                    verify_online_backup_and_restore(
                        source,
                        temporary_root=recovery,
                        expected_schema_version=7,
                    )
            self.assertEqual(existing.read_bytes(), b"keep-me")

    def test_failed_backup_leaves_no_final_or_partial_artifact(self) -> None:
        class FailingBackupSource:
            def __init__(self, connection: sqlite3.Connection) -> None:
                self.connection = connection

            def execute(self, statement: str):
                return self.connection.execute(statement)

            def backup(self, _destination: sqlite3.Connection) -> None:
                raise sqlite3.OperationalError("injected backup failure")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            recovery = root / "recovery"
            retained = root / "retained"
            recovery.mkdir()
            retained.mkdir()
            final = retained / "verified-backup.db"
            with closing(self._database(root)) as connection:
                with self.assertRaisesRegex(sqlite3.OperationalError, "injected"):
                    verify_online_backup_and_restore(
                        FailingBackupSource(connection),
                        temporary_root=recovery,
                        expected_schema_version=7,
                        backup_path=final,
                    )

            self.assertFalse(final.exists())
            self.assertEqual(list(retained.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
