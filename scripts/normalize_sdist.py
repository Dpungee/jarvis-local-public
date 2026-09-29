#!/usr/bin/env python3
"""Normalize a trusted local Python sdist into a reproducible tar.gz artifact."""

from __future__ import annotations

import argparse
import copy
import gzip
import os
import re
import stat
import tarfile
import tempfile
import unicodedata
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Sequence


MAX_SDIST_BYTES = 512 * 1024 * 1024
MAX_SDIST_MEMBERS = 20_000
_WINDOWS_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{number}" for number in range(1, 10)}
    | {f"LPT{number}" for number in range(1, 10)}
)


class SdistNormalizationError(RuntimeError):
    """Raised when an sdist cannot be normalized without weakening safety."""


def _validated_members(archive: tarfile.TarFile) -> list[tarfile.TarInfo]:
    members = archive.getmembers()
    if not members or len(members) > MAX_SDIST_MEMBERS:
        raise SdistNormalizationError("sdist member count is invalid")
    seen: set[str] = set()
    portable_seen: set[str] = set()
    roots: set[str] = set()
    file_paths: set[str] = set()
    total_size = 0
    for member in members:
        pure = PurePosixPath(member.name)
        if (
            not member.name
            or "\\" in member.name
            or re.match(r"^[A-Za-z]:", member.name) is not None
            or pure.is_absolute()
            or ".." in pure.parts
            or not pure.parts
            or member.name != pure.as_posix()
            or any(":" in part for part in pure.parts)
        ):
            raise SdistNormalizationError("sdist contains an unsafe member path")
        for component in pure.parts:
            normalized_component = unicodedata.normalize("NFKC", component)
            stem = normalized_component.split(".", 1)[0].upper()
            if component.rstrip(" .") != component or stem in _WINDOWS_RESERVED_NAMES:
                raise SdistNormalizationError(
                    "sdist contains a Windows-incompatible member path"
                )
        if member.name in seen:
            raise SdistNormalizationError("sdist contains duplicate member paths")
        seen.add(member.name)
        portable_name = unicodedata.normalize("NFKC", member.name).casefold()
        if portable_name in portable_seen:
            raise SdistNormalizationError(
                "sdist contains colliding portable member paths"
            )
        portable_seen.add(portable_name)
        roots.add(pure.parts[0])
        if not (member.isfile() or member.isdir()):
            raise SdistNormalizationError("sdist contains a link or special file")
        if member.isfile():
            file_paths.add(member.name)
        if member.size < 0 or member.size > MAX_SDIST_BYTES:
            raise SdistNormalizationError("sdist member size is invalid")
        total_size += member.size
        if total_size > MAX_SDIST_BYTES:
            raise SdistNormalizationError("sdist expanded size is invalid")
    if len(roots) != 1:
        raise SdistNormalizationError("sdist must contain one top-level directory")
    root = next(iter(roots))
    root_members = [member for member in members if member.name == root]
    if len(root_members) != 1 or not root_members[0].isdir():
        raise SdistNormalizationError(
            "sdist must contain an explicit top-level directory"
        )
    for member in members:
        pure = PurePosixPath(member.name)
        if member.name != root and len(pure.parts) < 2:
            raise SdistNormalizationError(
                "sdist members must descend from the top-level directory"
            )
        if any(parent.as_posix() in file_paths for parent in pure.parents):
            raise SdistNormalizationError(
                "sdist contains a file used as a parent directory"
            )
    return sorted(members, key=lambda item: item.name)


def _normalized_info(member: tarfile.TarInfo, source_epoch: int) -> tarfile.TarInfo:
    normalized = copy.copy(member)
    normalized.mtime = source_epoch
    normalized.uid = 0
    normalized.gid = 0
    normalized.uname = ""
    normalized.gname = ""
    normalized.pax_headers = {}
    # Preserve the executable/read-only contract while discarding host-specific
    # group/other write bits and any platform file-type decoration.
    normalized.mode = member.mode & 0o755
    return normalized


def normalize_sdist(path: Path, *, source_epoch: int) -> None:
    supplied = Path(path)
    try:
        supplied_details = os.lstat(supplied)
    except OSError as exc:
        raise SdistNormalizationError("sdist is unavailable") from exc
    supplied_attributes = getattr(supplied_details, "st_file_attributes", 0)
    if (
        not stat.S_ISREG(supplied_details.st_mode)
        or stat.S_ISLNK(supplied_details.st_mode)
        or supplied_attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        or supplied_details.st_nlink > 1
    ):
        raise SdistNormalizationError("sdist must be an ordinary file")
    lexical = Path(os.path.abspath(supplied))
    artifact = supplied.resolve(strict=True)
    if artifact != lexical:
        raise SdistNormalizationError("sdist path may not traverse links")
    if artifact.stat().st_size <= 0 or artifact.stat().st_size > MAX_SDIST_BYTES:
        raise SdistNormalizationError("sdist size is invalid")
    if isinstance(source_epoch, bool) or source_epoch < 0:
        raise SdistNormalizationError("source epoch must be a non-negative integer")
    if not artifact.name.casefold().endswith(".tar.gz"):
        raise SdistNormalizationError("sdist must use the .tar.gz format")

    parent = artifact.parent
    temporary: Path | None = None
    try:
        with tarfile.open(artifact, mode="r:gz") as source:
            members = _validated_members(source)
            descriptor, raw_temporary = tempfile.mkstemp(
                prefix=f".{artifact.name}.", suffix=".tmp", dir=parent
            )
            os.close(descriptor)
            temporary = Path(raw_temporary)
            with temporary.open("wb") as raw_output:
                with gzip.GzipFile(
                    filename="",
                    mode="wb",
                    fileobj=raw_output,
                    mtime=source_epoch,
                ) as compressed:
                    with tarfile.open(
                        fileobj=compressed,
                        mode="w",
                        format=tarfile.PAX_FORMAT,
                    ) as destination:
                        for member in members:
                            payload: BinaryIO | None = None
                            if member.isfile():
                                payload = source.extractfile(member)
                                if payload is None:
                                    raise SdistNormalizationError(
                                        "sdist member content is unavailable"
                                    )
                            try:
                                destination.addfile(
                                    _normalized_info(member, source_epoch), payload
                                )
                            finally:
                                if payload is not None:
                                    payload.close()
        if temporary is None:
            raise SdistNormalizationError("normalized artifact was not created")
        after = os.lstat(artifact)
        if (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_nlink,
        ) != (
            supplied_details.st_dev,
            supplied_details.st_ino,
            supplied_details.st_size,
            supplied_details.st_mtime_ns,
            supplied_details.st_nlink,
        ):
            raise SdistNormalizationError("sdist changed during normalization")
        os.replace(temporary, artifact)
        temporary = None
    except (OSError, tarfile.TarError) as exc:
        raise SdistNormalizationError("sdist normalization failed") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifact", type=Path)
    parser.add_argument("--source-epoch", type=int, required=True)
    args = parser.parse_args(argv)
    try:
        normalize_sdist(args.artifact, source_epoch=args.source_epoch)
    except SdistNormalizationError as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
