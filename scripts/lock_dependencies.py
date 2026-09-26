#!/usr/bin/env python3
"""Generate and check hash-locked requirement files from exact version pins.

A lock file lists one exact release per package together with the SHA-256 digest of
every file PyPI publishes for that release, so ``pip install --require-hashes -r
<lock>`` resolves to the same bytes on every supported platform and Python version
without the repository trusting an unpinned index resolution.

Generate::

    python scripts/lock_dependencies.py --pins pins.txt --output requirements/x.lock
    python scripts/lock_dependencies.py --latest pip --latest setuptools --output requirements/build-backend.lock

``--pins`` accepts ``pip freeze`` output (``name==version`` lines; comments, blank
lines, and local ``@ file://`` entries are ignored). ``--latest NAME`` pins the
current PyPI release of NAME at generation time.

Check offline (no network)::

    python scripts/lock_dependencies.py --check requirements/x.lock

``--require-python 3.11`` refuses any release whose ``Requires-Python`` excludes that
interpreter or that publishes no compatible wheel for it, so a lock resolved on a
newer interpreter cannot silently break the oldest supported CI leg.

Only ``https://pypi.org`` is contacted, read-only, with a bounded response size.
Nothing is installed or executed.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import re
import sys
import urllib.request
from pathlib import Path

PYPI = "https://pypi.org/pypi"
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
TIMEOUT_SECONDS = 30.0
_PIN = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*==\s*([A-Za-z0-9!+.\-_]+)\s*(?:;.*)?$")
_LOCK_LINE = re.compile(r"^([a-z0-9][a-z0-9.\-]*)==([A-Za-z0-9!+.\-_]+) \\$")
_HASH_LINE = re.compile(r"^    --hash=sha256:([0-9a-f]{64})( \\)?$")


def normalize(name: str) -> str:
    """PEP 503 project-name normalization."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _read_text_any_bom(path: Path) -> str:
    """Read a pins file written by either PowerShell edition (UTF-16 or UTF-8)."""
    data = path.read_bytes()
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16")
    return data.decode("utf-8-sig")


def read_pins(path: Path) -> list[tuple[str, str]]:
    pins: list[tuple[str, str]] = []
    for raw in _read_text_any_bom(path).splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or " @ " in line:
            continue
        match = _PIN.match(line)
        if not match:
            raise SystemExit(f"unsupported pin line (expected name==version): {line!r}")
        pins.append((normalize(match.group(1)), match.group(2)))
    return pins


def _fetch_json(url: str) -> dict:
    if not url.startswith(PYPI + "/"):
        raise SystemExit(f"refusing non-PyPI URL {url!r}")
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:  # noqa: S310 - fixed https host
        body = response.read(MAX_RESPONSE_BYTES + 1)
    if len(body) > MAX_RESPONSE_BYTES:
        raise SystemExit(f"response for {url} exceeded {MAX_RESPONSE_BYTES} bytes")
    document = json.loads(body.decode("utf-8"))
    if not isinstance(document, dict):
        raise SystemExit(f"unexpected document shape for {url}")
    return document


def latest_version(name: str) -> str:
    document = _fetch_json(f"{PYPI}/{normalize(name)}/json")
    version = str(document.get("info", {}).get("version") or "")
    if not version:
        raise SystemExit(f"could not determine the latest version of {name}")
    return version


def _python_allowed(requires_python: str, minimum: tuple[int, int]) -> bool:
    """True when a Requires-Python specifier admits the minimum interpreter."""
    for clause in [c.strip() for c in requires_python.split(",") if c.strip()]:
        match = re.fullmatch(r"(>=|>|<=|<|==|!=|~=)\s*([0-9]+(?:\.[0-9]+)*)(?:\.\*)?", clause)
        if match is None:
            continue
        operator, text = match.groups()
        parts = tuple(int(p) for p in text.split(".")[:2])
        target = parts + (0,) * (2 - len(parts))
        checks = {
            ">=": minimum >= target, ">": minimum > target, "<=": minimum <= target,
            "<": minimum < target, "==": minimum == target, "!=": minimum != target,
            "~=": minimum >= target and minimum[0] == target[0],
        }
        if not checks[operator]:
            return False
    return True


def _wheel_supports(filename: str, minimum: tuple[int, int]) -> bool:
    if not filename.endswith(".whl"):
        return False
    tags = filename[:-4].split("-")[-3:]
    python_tag, abi_tag = tags[0], tags[1]
    wanted = f"cp{minimum[0]}{minimum[1]}"
    return (
        wanted in python_tag.split(".")
        or abi_tag == "abi3"
        or any(tag in {"py3", "py2.py3"} for tag in python_tag.split("."))
    )


def release_hashes(name: str, version: str, *, minimum: tuple[int, int] | None = None) -> list[str]:
    document = _fetch_json(f"{PYPI}/{name}/{version}/json")
    info = document.get("info") or {}
    if normalize(str(info.get("name") or "")) != name or str(info.get("version") or "") != version:
        raise SystemExit(f"PyPI returned a different release for {name}=={version}")
    files = [item for item in (document.get("urls") or ()) if isinstance(item, dict)]
    if minimum is not None:
        requires_python = str(info.get("requires_python") or "")
        if not _python_allowed(requires_python, minimum):
            raise SystemExit(
                f"{name}=={version} requires Python {requires_python!r}; "
                f"the lock must install on {minimum[0]}.{minimum[1]}"
            )
        if not any(_wheel_supports(str(item.get("filename") or ""), minimum) for item in files):
            raise SystemExit(
                f"{name}=={version} publishes no wheel usable on Python {minimum[0]}.{minimum[1]}"
            )
    digests: set[str] = set()
    for item in files:
        digest = str((item.get("digests") or {}).get("sha256") or "")
        if re.fullmatch(r"[0-9a-f]{64}", digest):
            digests.add(digest)
    if not digests:
        raise SystemExit(f"{name}=={version} has no downloadable files with SHA-256 digests")
    return sorted(digests)


def render(
    pins: list[tuple[str, str]],
    *,
    source_note: str,
    command: str,
    minimum: tuple[int, int] | None,
) -> str:
    validated = (
        f"validated for Python {minimum[0]}.{minimum[1]}+" if minimum else "not validated for a minimum Python"
    )
    lines = [
        "# Hash-locked requirements generated by scripts/lock_dependencies.py.",
        f"# Generated: {_dt.datetime.now(_dt.timezone.utc).strftime('%Y-%m-%d')} UTC; {source_note}; {validated}.",
        f"# Command: {command}",
        "# Every published file digest for each release is listed so",
        "# `pip install --require-hashes -r <this file>` works on every supported platform.",
        "# Regenerate with the command above instead of editing by hand.",
        "",
    ]
    for name, version in sorted(set(pins)):
        hashes = release_hashes(name, version, minimum=minimum)
        lines.append(f"{name}=={version} \\")
        for index, digest in enumerate(hashes):
            suffix = " \\" if index < len(hashes) - 1 else ""
            lines.append(f"    --hash=sha256:{digest}{suffix}")
    return "\n".join(lines) + "\n"


def parse_lock(path: Path) -> dict[str, tuple[str, list[str]]]:
    """Return {name: (version, hashes)} and fail on any malformed line."""
    entries: dict[str, tuple[str, list[str]]] = {}
    current: str | None = None
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not raw.strip() or raw.startswith("#"):
            if current is not None:
                raise SystemExit(f"{path}:{number}: hash block ended without a final hash")
            continue
        pin = _LOCK_LINE.match(raw)
        if pin:
            if current is not None:
                raise SystemExit(f"{path}:{number}: previous entry {current} has no hashes")
            name, version = pin.group(1), pin.group(2)
            if name in entries or name != normalize(name):
                raise SystemExit(f"{path}:{number}: duplicate or non-normalized name {name}")
            entries[name] = (version, [])
            current = name
            continue
        digest = _HASH_LINE.match(raw)
        if digest and current is not None:
            entries[current][1].append(digest.group(1))
            if digest.group(2) is None:
                current = None
            continue
        raise SystemExit(f"{path}:{number}: malformed lock line {raw!r}")
    if current is not None:
        raise SystemExit(f"{path}: file ended inside the hash block of {current}")
    for name, (version, hashes) in entries.items():
        if not hashes or len(set(hashes)) != len(hashes):
            raise SystemExit(f"{path}: {name}=={version} has missing or duplicate hashes")
    return entries


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--pins", type=Path, action="append", default=[], help="pip freeze style pin file")
    parser.add_argument("--latest", action="append", default=[], help="pin the current PyPI release of NAME")
    parser.add_argument("--output", type=Path, help="lock file to write")
    parser.add_argument("--check", type=Path, action="append", default=[], help="lock file to validate offline")
    parser.add_argument(
        "--require-python",
        default=None,
        help="oldest supported interpreter, e.g. 3.11; every release must admit it and ship a usable wheel",
    )
    args = parser.parse_args(argv)
    minimum: tuple[int, int] | None = None
    if args.require_python:
        match = re.fullmatch(r"([0-9]+)\.([0-9]+)", str(args.require_python).strip())
        if match is None:
            parser.error("--require-python expects MAJOR.MINOR")
        minimum = (int(match.group(1)), int(match.group(2)))

    if args.check:
        for path in args.check:
            entries = parse_lock(path)
            print(f"{path}: {len(entries)} pinned release(s), {sum(len(h) for _, h in entries.values())} hashes")
        return 0
    if args.output is None or not (args.pins or args.latest):
        parser.error("generation needs --output and at least one --pins or --latest")
    pins: list[tuple[str, str]] = []
    for path in args.pins:
        pins.extend(read_pins(path))
    for name in args.latest:
        pins.append((normalize(name), latest_version(name)))
    seen: dict[str, str] = {}
    for name, version in pins:
        if seen.setdefault(name, version) != version:
            raise SystemExit(f"conflicting pins for {name}: {seen[name]} and {version}")
    note = f"python {sys.version_info.major}.{sys.version_info.minor} resolution on {sys.platform}"
    command = "python scripts/lock_dependencies.py " + " ".join(
        [f"--pins {path.name}" for path in args.pins]
        + [f"--latest {name}" for name in args.latest]
        + ([f"--require-python {args.require_python}"] if args.require_python else [])
        + [f"--output {args.output.as_posix()}"]
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(
        render(pins, source_note=note, command=command, minimum=minimum).encode("utf-8")
    )
    print(f"wrote {args.output} with {len(seen)} release(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
