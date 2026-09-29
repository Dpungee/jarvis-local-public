from __future__ import annotations

import gzip
import hashlib
import io
import os
import tarfile
import tempfile
import unittest
from pathlib import Path

from scripts.normalize_sdist import SdistNormalizationError, normalize_sdist


class NormalizeSdistTests(unittest.TestCase):
    def _archive(
        self,
        path: Path,
        *,
        timestamp: int,
        unsafe_name: str | None = None,
        link: bool = False,
    ) -> None:
        with tarfile.open(path, mode="w:gz") as archive:
            root = tarfile.TarInfo("example-1.0")
            root.type = tarfile.DIRTYPE
            root.mode = 0o775
            root.mtime = timestamp
            archive.addfile(root)
            name = unsafe_name or "example-1.0/module.py"
            member = tarfile.TarInfo(name)
            member.mtime = timestamp
            member.mode = 0o664
            if link:
                member.type = tarfile.SYMTYPE
                member.linkname = "outside"
                archive.addfile(member)
            else:
                payload = b"VALUE = 1\n"
                member.size = len(payload)
                archive.addfile(member, io.BytesIO(payload))

    def test_different_build_times_normalize_to_identical_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "first.tar.gz"
            second = root / "second.tar.gz"
            self._archive(first, timestamp=100)
            self._archive(second, timestamp=900)

            normalize_sdist(first, source_epoch=123456789)
            normalize_sdist(second, source_epoch=123456789)

            first_bytes = first.read_bytes()
            second_bytes = second.read_bytes()
            self.assertEqual(first_bytes, second_bytes)
            self.assertEqual(
                hashlib.sha256(first_bytes).hexdigest(),
                hashlib.sha256(second_bytes).hexdigest(),
            )
            self.assertEqual(int.from_bytes(first_bytes[4:8], "little"), 123456789)
            with gzip.open(first, "rb") as expanded:
                with tarfile.open(fileobj=expanded, mode="r:") as archive:
                    for member in archive.getmembers():
                        self.assertEqual(member.mtime, 123456789)
                        self.assertEqual((member.uid, member.gid), (0, 0))
                        self.assertEqual((member.uname, member.gname), ("", ""))

    def test_traversal_member_is_rejected_without_replacing_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            artifact = Path(temporary) / "unsafe.tar.gz"
            self._archive(artifact, timestamp=1, unsafe_name="../escape.py")
            original = artifact.read_bytes()

            with self.assertRaisesRegex(SdistNormalizationError, "unsafe member"):
                normalize_sdist(artifact, source_epoch=1)

            self.assertEqual(artifact.read_bytes(), original)

    def test_links_are_rejected_without_replacing_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            artifact = Path(temporary) / "link.tar.gz"
            self._archive(artifact, timestamp=1, link=True)
            original = artifact.read_bytes()

            with self.assertRaisesRegex(SdistNormalizationError, "link or special"):
                normalize_sdist(artifact, source_epoch=1)

            self.assertEqual(artifact.read_bytes(), original)

    def test_existing_non_archive_is_not_destroyed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            artifact = Path(temporary) / "not-an-archive.tar.gz"
            artifact.write_bytes(b"keep this")
            with self.assertRaisesRegex(SdistNormalizationError, "failed"):
                normalize_sdist(artifact, source_epoch=1)
            self.assertEqual(artifact.read_bytes(), b"keep this")

    def test_noncanonical_member_path_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            artifact = Path(temporary) / "noncanonical.tar.gz"
            self._archive(
                artifact,
                timestamp=1,
                unsafe_name="example-1.0//module.py",
            )
            with self.assertRaisesRegex(SdistNormalizationError, "unsafe member"):
                normalize_sdist(artifact, source_epoch=1)

    def test_windows_drive_and_backslash_traversal_members_are_rejected(self) -> None:
        for index, unsafe in enumerate(
            (r"C:\private\escape.txt", r"example-1.0\..\..\escape.txt")
        ):
            with self.subTest(unsafe=unsafe), tempfile.TemporaryDirectory() as temporary:
                artifact = Path(temporary) / f"unsafe-{index}.tar.gz"
                self._archive(artifact, timestamp=1, unsafe_name=unsafe)
                with self.assertRaisesRegex(SdistNormalizationError, "unsafe member"):
                    normalize_sdist(artifact, source_epoch=1)

    def test_case_colliding_member_paths_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            artifact = Path(temporary) / "collision.tar.gz"
            with tarfile.open(artifact, mode="w:gz") as archive:
                for name in ("example-1.0/File.py", "example-1.0/file.py"):
                    payload = b"x"
                    member = tarfile.TarInfo(name)
                    member.size = len(payload)
                    archive.addfile(member, io.BytesIO(payload))
            with self.assertRaisesRegex(SdistNormalizationError, "colliding portable"):
                normalize_sdist(artifact, source_epoch=1)

    def test_top_level_member_must_be_an_explicit_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            artifact = Path(temporary) / "root-file.tar.gz"
            with tarfile.open(artifact, mode="w:gz") as archive:
                payload = b"not a directory"
                root = tarfile.TarInfo("example-1.0")
                root.size = len(payload)
                archive.addfile(root, io.BytesIO(payload))
            with self.assertRaisesRegex(SdistNormalizationError, "top-level directory"):
                normalize_sdist(artifact, source_epoch=1)

    def test_file_cannot_also_be_a_parent_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            artifact = Path(temporary) / "parent-file.tar.gz"
            with tarfile.open(artifact, mode="w:gz") as archive:
                root = tarfile.TarInfo("example-1.0")
                root.type = tarfile.DIRTYPE
                archive.addfile(root)
                for name in ("example-1.0/module", "example-1.0/module/data.txt"):
                    payload = b"x"
                    member = tarfile.TarInfo(name)
                    member.size = len(payload)
                    archive.addfile(member, io.BytesIO(payload))
            with self.assertRaisesRegex(SdistNormalizationError, "parent directory"):
                normalize_sdist(artifact, source_epoch=1)

    def test_windows_reserved_and_trailing_components_are_rejected(self) -> None:
        for index, unsafe in enumerate(
            (
                "example-1.0/NUL",
                "example-1.0/con.txt",
                "example-1.0/file. ",
                "example-1.0/COM¹.txt",
            )
        ):
            with self.subTest(unsafe=unsafe), tempfile.TemporaryDirectory() as temporary:
                artifact = Path(temporary) / f"windows-{index}.tar.gz"
                self._archive(artifact, timestamp=1, unsafe_name=unsafe)
                with self.assertRaisesRegex(
                    SdistNormalizationError, "Windows-incompatible"
                ):
                    normalize_sdist(artifact, source_epoch=1)

    def test_input_symlink_is_rejected_without_changing_link_or_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "target.tar.gz"
            link = root / "link.tar.gz"
            self._archive(target, timestamp=1)
            original = target.read_bytes()
            try:
                os.symlink(target, link)
            except OSError as exc:
                self.skipTest(f"file symlinks are unavailable: {exc}")
            with self.assertRaisesRegex(SdistNormalizationError, "ordinary file"):
                normalize_sdist(link, source_epoch=1)
            self.assertTrue(link.is_symlink())
            self.assertEqual(target.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
