"""Dataset acquisition, digest pinning, and the licence rules.

Three rules are structural, not advisory:

* **No dataset byte is ever written inside the repository.**  The cache
  directory is resolved, made absolute, and refused when it is the repository
  root or anything beneath it.  ``scripts/check_public_release.py``'s
  ``MAX_TRACKED_FILE_BYTES`` (5 MiB) stays where it is; nothing here needs it
  raised.
* **Nothing is fetched unless the caller asked for a fetch.**  ``allow_fetch``
  defaults to ``False``, so a scoring run reads the cache or refuses.  Tests
  never fetch: they build small synthetic files in the datasets' exact formats.
* **The licence rules are keyed on the licence, not on the benchmark's name.**
  A dataset whose ``licence_class`` is ``restricted`` refuses to be fetched or
  read at all when a commercial use is declared, and a restricted licence whose
  digest has moved refuses the run.  A future restricted dataset inherits both
  for free.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

CACHE_ENV = "JARVIS_BENCHMARK_CACHE"
COMMERCIAL_USE_ENV = "JARVIS_BENCHMARK_COMMERCIAL_USE"
DEFAULT_CACHE_DIRNAME = "jarvis-benchmarks"
LICENCE_CLASSES = ("open", "restricted")
LICENCE_FILENAME = "LICENCE.fetched.txt"

_CHUNK_BYTES = 1024 * 1024
# Nothing in the registry is within two orders of magnitude of this; it exists
# so a redirected or hostile URL cannot fill the disk.
MAX_FETCH_BYTES = 8 * 1024 * 1024 * 1024
# The leakage scan streams the whole file (see ``sample_dataset_values``); the
# prefix bound below is only the fallback for a file that is not a top-level
# JSON array.  Values are taken per element so coverage is uniform rather than
# concentrated at the head of a 264 MiB file.
LEAKAGE_SCAN_PREFIX_BYTES = 8 * 1024 * 1024
LEAKAGE_SAMPLE_SIZE = 64
LEAKAGE_VALUES_PER_ELEMENT = 6
LEAKAGE_MIN_VALUE_CHARS = 32

_JSON_STRING_RE = re.compile(r'"((?:[^"\\\x00-\x1f]|\\.){%d,400})"' % LEAKAGE_MIN_VALUE_CHARS)
_TEXT_SUFFIXES = frozenset(
    {
        ".py", ".md", ".txt", ".json", ".yml", ".yaml", ".toml", ".cfg", ".ini",
        ".html", ".css", ".js", ".bat", ".ps1", ".sh", ".example", ".gitignore",
    }
)


class DatasetError(RuntimeError):
    """A closed-reason refusal from the dataset layer."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class DatasetSpec:
    """One fetchable artefact, with everything the published config records."""

    name: str
    benchmark: str
    url: str
    filename: str
    sha256: str | None
    bytes: int | None
    licence: str
    licence_class: str
    licence_url: str | None = None
    licence_sha256: str | None = None
    notes: str = ""

    def __post_init__(self) -> None:
        if self.licence_class not in LICENCE_CLASSES:
            raise DatasetError(
                f"{self.name}: licence_class must be one of {LICENCE_CLASSES}",
                code="licence_class_invalid",
            )


@dataclass(frozen=True)
class DatasetHandle:
    """The result of resolving a dataset: paths, digests, and licence state."""

    name: str
    benchmark: str
    path: Path
    sha256: str
    bytes: int
    licence: str
    licence_class: str
    licence_path: Path | None
    licence_sha256: str | None
    pinned: bool
    licence_drift: bool
    fetched: bool
    # The digest of the copy that was in the cache before this fetch, so a
    # drift report can print both sides rather than only the new one.
    previous_licence_sha256: str | None = None

    def as_config(self) -> dict[str, object]:
        """The dataset block a published config carries verbatim."""

        return {
            "name": self.name,
            "sha256": self.sha256,
            "bytes": self.bytes,
            "licence": self.licence,
            "licence_class": self.licence_class,
            "licence_sha256": self.licence_sha256,
            "pinned": self.pinned,
            "licence_drift": self.licence_drift,
        }


# ---------------------------------------------------------------------------
# The registry.  Every fact below was verified by a read-only metadata call on
# 2026-09-04 (VTMF M5 design section 3.2); no dataset was downloaded to obtain
# it.  ``sha256=None`` means "not yet pinned on this host": ``fetch`` prints the
# observed digest and a scored run refuses until it is written into the config.
# ---------------------------------------------------------------------------

DATASETS: dict[str, DatasetSpec] = {
    "longmemeval_s": DatasetSpec(
        name="longmemeval_s",
        benchmark="longmemeval_s",
        url=(
            "https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned/"
            "resolve/main/longmemeval_s_cleaned.json"
        ),
        filename="longmemeval_s_cleaned.json",
        sha256=None,
        bytes=277_383_467,
        licence="MIT",
        licence_class="open",
        licence_url="https://huggingface.co/api/datasets/xiaowu0162/longmemeval-cleaned",
        licence_sha256=None,
        notes="500 instances, five abilities, 30 abstention ids suffixed _abs.",
    ),
    "longmemeval_oracle": DatasetSpec(
        name="longmemeval_oracle",
        benchmark="longmemeval_oracle",
        url=(
            "https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned/"
            "resolve/main/longmemeval_oracle.json"
        ),
        filename="longmemeval_oracle.json",
        sha256=None,
        bytes=15_388_478,
        licence="MIT",
        licence_class="open",
        licence_url="https://huggingface.co/api/datasets/xiaowu0162/longmemeval-cleaned",
        licence_sha256=None,
        notes="Evidence sessions only. The honest control arm for degradation.",
    ),
    "locomo10": DatasetSpec(
        name="locomo10",
        benchmark="locomo10",
        url="https://raw.githubusercontent.com/snap-research/locomo/main/data/locomo10.json",
        filename="locomo10.json",
        sha256=None,
        bytes=2_805_274,
        licence="CC BY-NC 4.0",
        licence_class="restricted",
        licence_url="https://raw.githubusercontent.com/snap-research/locomo/main/LICENSE.txt",
        licence_sha256=None,
        notes=(
            "Commercial use prohibited. Measurements only are published: ids, "
            "categories and scores, never question, answer, dialogue, persona, "
            "caption or URL text."
        ),
    ),
}

# Benchmarks that generate their own material and touch no network at all.
GENERATED_BENCHMARKS = ("ruler_style", "longmemeval-shape", "locomo-shape")


def repository_root() -> Path:
    """The tree this package lives in."""

    return Path(__file__).resolve().parents[2]


def iter_json_array(path: Path, *, chunk_bytes: int = _CHUNK_BYTES):
    """Yield the elements of a top-level JSON array without loading the file.

    ``longmemeval_s_cleaned.json`` is 264 MiB; ``json.load`` on it costs well
    over a gigabyte of Python objects for no reason, because every runner here
    consumes one instance at a time.  Streaming also means a truncated download
    fails on the element that is actually broken instead of on the whole file.
    """

    decoder = json.JSONDecoder()
    with open(path, "r", encoding="utf-8") as handle:
        buffer = ""
        while not buffer.strip():
            chunk = handle.read(chunk_bytes)
            if not chunk:
                raise DatasetError(f"{path} holds no JSON value", code="dataset_malformed")
            buffer += chunk
        start = len(buffer) - len(buffer.lstrip())
        if buffer[start] != "[":
            raise DatasetError(
                f"{path} must hold a top-level JSON array", code="dataset_malformed"
            )
        position = start + 1
        while True:
            while True:
                trimmed = buffer[position:].lstrip()
                position = len(buffer) - len(trimmed)
                if trimmed.startswith(","):
                    position += 1
                    continue
                if trimmed.startswith("]"):
                    return
                if trimmed:
                    break
                chunk = handle.read(chunk_bytes)
                if not chunk:
                    return
                buffer += chunk
            while True:
                try:
                    value, end = decoder.raw_decode(buffer, position)
                except ValueError:
                    chunk = handle.read(chunk_bytes)
                    if not chunk:
                        raise DatasetError(
                            f"{path} ends inside a JSON value; the download is truncated",
                            code="dataset_malformed",
                        ) from None
                    buffer += chunk
                    continue
                break
            yield value
            buffer = buffer[end:]
            position = 0


def commercial_use_declared(env: Mapping[str, str] | None = None) -> bool:
    """Whether a commercial use has been declared for this process.

    Presence of the variable is the declaration, whatever its value.  Failing
    closed here is deliberate: a run that sets ``...=0`` meaning "no" and a run
    that sets it meaning "yes" are indistinguishable to us, and refusing to
    touch a use-restricted dataset is the cheap side of that ambiguity.
    """

    environment = os.environ if env is None else env
    return COMMERCIAL_USE_ENV in environment


def default_cache_dir(env: Mapping[str, str] | None = None) -> Path:
    """The per-host cache location, before the outside-the-repository check."""

    environment = os.environ if env is None else env
    configured = environment.get(CACHE_ENV)
    if configured:
        return Path(configured)
    local_app_data = environment.get("LOCALAPPDATA")
    if sys.platform == "win32" and local_app_data:
        return Path(local_app_data) / DEFAULT_CACHE_DIRNAME
    home = environment.get("HOME") or environment.get("USERPROFILE")
    base = Path(home) if home else Path.home()
    return base / ".cache" / DEFAULT_CACHE_DIRNAME


def _strip_extended_prefix(path: Path) -> Path:
    """Drop a Windows ``\\\\?\\`` / ``\\\\?\\UNC\\`` prefix before resolving.

    The extended-length forms name the same file as the plain path but compare
    unequal to it as strings, so a containment check that skips this step can be
    walked straight past.
    """

    raw = str(path)
    if raw.startswith("\\\\?\\UNC\\"):
        return Path("\\\\" + raw[len("\\\\?\\UNC\\") :])
    if raw.startswith("\\\\?\\"):
        return Path(raw[len("\\\\?\\") :])
    return path


def real_path(path: Path | str) -> Path:
    """The path the filesystem agrees with, even when it does not exist yet.

    ``Path.absolute()`` is a pure string operation: it does not follow a
    junction or a symlink and it does not expand an 8.3 short name, so a
    containment test built on it compares against a path the filesystem does
    not recognise.  ``Path.resolve()`` does all three but needs the path -- or
    at least a real ancestor -- to exist.  So: resolve the nearest **existing**
    ancestor, which is where any link or short name lives, then rejoin the
    not-yet-created tail.
    """

    candidate = _strip_extended_prefix(Path(path).expanduser().absolute())
    candidate = Path(os.path.normpath(str(candidate)))
    existing = candidate
    tail: list[str] = []
    while existing != existing.parent:
        try:
            if existing.exists():
                break
        except OSError:  # pragma: no cover - unreadable ancestor
            break
        tail.append(existing.name)
        existing = existing.parent
    try:
        resolved = existing.resolve()
    except OSError:  # pragma: no cover - resolution refused by the OS
        resolved = existing
    for name in reversed(tail):
        resolved = resolved / name
    return resolved


def resolve_cache_dir(
    explicit: Path | str | None = None,
    *,
    env: Mapping[str, str] | None = None,
    root: Path | None = None,
) -> Path:
    """Resolve the cache directory and refuse any path inside the repository.

    The comparison is made on :func:`real_path`, not on the string the caller
    supplied, so a junction, a symlink, an 8.3 short name or an extended-length
    prefix that lands inside the tree is refused like the plain path is.  A
    2.8 MB CC BY-NC dataset dropped into the tree that way would sit under
    ``MAX_TRACKED_FILE_BYTES`` and survive a ``git add -A``.
    """

    candidate = Path(explicit) if explicit is not None else default_cache_dir(env)
    resolved = real_path(candidate)
    repository = real_path(root or repository_root())
    if resolved == repository or repository in resolved.parents:
        raise DatasetError(
            "the benchmark cache may not live inside the repository "
            f"({resolved} is under {repository}); set {CACHE_ENV} to a path outside it",
            code="cache_inside_repository",
        )
    return resolved


def sha256_file(path: Path) -> str:
    """Stream a file through SHA-256 without loading it into memory."""

    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(_CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def urllib_fetch(url: str, destination: Path) -> None:
    """Stream one HTTPS URL to disk.  The only network call in this package."""

    if not url.lower().startswith("https://"):
        raise DatasetError(f"refusing a non-HTTPS dataset URL: {url}", code="insecure_url")
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_suffix(destination.suffix + ".part")
    written = 0
    request = urllib.request.Request(url, headers={"User-Agent": "jarvis-benchmarks/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=120) as response:  # noqa: S310
            with open(partial, "wb") as handle:
                while True:
                    chunk = response.read(_CHUNK_BYTES)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > MAX_FETCH_BYTES:
                        raise DatasetError(
                            f"{url} exceeded the {MAX_FETCH_BYTES} byte fetch ceiling",
                            code="fetch_too_large",
                        )
                    handle.write(chunk)
    except DatasetError:
        partial.unlink(missing_ok=True)
        raise
    except OSError as exc:
        partial.unlink(missing_ok=True)
        raise DatasetError(f"could not fetch {url}: {exc}", code="fetch_failed") from exc
    shutil.move(str(partial), str(destination))


Fetcher = Callable[[str, Path], None]


def ensure_dataset(
    spec: DatasetSpec,
    *,
    cache_dir: Path | str | None = None,
    env: Mapping[str, str] | None = None,
    fetcher: Fetcher | None = None,
    allow_fetch: bool = False,
    scored: bool = True,
    root: Path | None = None,
) -> DatasetHandle:
    """Resolve one dataset to a verified on-disk path, or refuse with a code.

    ``allow_fetch`` is the explicit flag the design requires: only the ``fetch``
    subcommand passes it.  A scoring run with an absent cache entry refuses
    rather than silently reaching for the network.
    """

    if spec.licence_class == "restricted" and commercial_use_declared(env):
        raise DatasetError(
            f"{spec.name} is licensed {spec.licence}, which restricts use, and "
            f"{COMMERCIAL_USE_ENV} declares a commercial use; nothing was fetched "
            "or read",
            code="commercial_use_declared",
        )

    directory = resolve_cache_dir(cache_dir, env=env, root=root) / spec.name
    data_path = directory / spec.filename
    licence_path = directory / LICENCE_FILENAME
    fetch = fetcher or urllib_fetch
    fetched = False

    if not data_path.exists():
        if not allow_fetch:
            raise DatasetError(
                f"{spec.name} is not in the cache at {data_path}; run "
                f"`python scripts/benchmarks/run.py fetch {spec.name}` first",
                code="dataset_not_cached",
            )
        directory.mkdir(parents=True, exist_ok=True)
        fetch(spec.url, data_path)
        fetched = True

    observed = sha256_file(data_path)
    size = data_path.stat().st_size
    pinned = spec.sha256 is not None
    if pinned and observed != spec.sha256:
        raise DatasetError(
            f"{spec.name} digest mismatch: config pins {spec.sha256}, the cached "
            f"file is {observed}",
            code="dataset_digest_mismatch",
        )
    if not pinned and scored:
        raise DatasetError(
            f"{spec.name} has no pinned sha256. Observed {observed}; write that "
            "into the config's dataset.sha256 before scoring a run",
            code="digest_unpinned",
        )

    licence_digest: str | None = None
    licence_drift = False
    cached_licence_sha256: str | None = None
    if spec.licence_url is not None:
        if allow_fetch:
            # M-1: always re-fetch. Digesting the cached copy compares the file
            # with itself, so drift -- the whole point of pinning the licence --
            # could never be detected after the first fetch. The licence is a
            # few kilobytes; fetch it into a staging file, digest that, and only
            # then let it become the cached copy.
            cached_licence_sha256 = (
                sha256_file(licence_path) if licence_path.exists() else None
            )
            staged = licence_path.with_suffix(licence_path.suffix + ".fetching")
            fetch(spec.licence_url, staged)
            fetched = True
            fresh = sha256_file(staged)
            if cached_licence_sha256 is not None and fresh != cached_licence_sha256:
                licence_drift = True
            shutil.move(str(staged), str(licence_path))
        if licence_path.exists():
            licence_digest = sha256_file(licence_path)
            if spec.licence_sha256 is not None and licence_digest != spec.licence_sha256:
                if spec.licence_class == "restricted":
                    raise DatasetError(
                        f"{spec.name}'s licence text has changed since it was pinned "
                        f"({spec.licence_sha256} -> {licence_digest}); a restricted "
                        "licence you rely on must not move under you, so the run "
                        "refuses",
                        code="licence_digest_mismatch",
                    )
                licence_drift = True
        elif not allow_fetch:
            # Reported, never fatal for an open licence; a restricted dataset
            # cannot reach a scored run without its licence text.
            if spec.licence_class == "restricted" and scored:
                raise DatasetError(
                    f"{spec.name} is restricted but its licence text is not cached; "
                    f"run `fetch {spec.name}` so the run can record what it relied on",
                    code="licence_not_cached",
                )

    return DatasetHandle(
        name=spec.name,
        benchmark=spec.benchmark,
        path=data_path,
        sha256=observed,
        bytes=size,
        licence=spec.licence,
        licence_class=spec.licence_class,
        licence_path=licence_path if licence_path.exists() else None,
        licence_sha256=licence_digest,
        previous_licence_sha256=cached_licence_sha256,
        pinned=pinned,
        licence_drift=licence_drift,
        fetched=fetched,
    )


def spec_for(name: str, *, overrides: Mapping[str, object] | None = None) -> DatasetSpec:
    """Look one dataset up, applying a config's digest pins."""

    try:
        spec = DATASETS[name]
    except KeyError:
        known = ", ".join(sorted(DATASETS))
        raise DatasetError(f"unknown dataset {name!r}; known: {known}", code="unknown_dataset") from None
    if not overrides:
        return spec
    allowed = {"sha256", "licence_sha256", "url", "licence_url", "bytes"}
    changes = {key: value for key, value in overrides.items() if key in allowed and value is not None}
    return replace(spec, **changes)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# The leakage scan (design section 3.8 assertion 4, compensation N-10).
# ---------------------------------------------------------------------------


def normalise_whitespace(text: str) -> str:
    """Collapse every run of whitespace to one space.

    A dataset value carrying a newline is copied into a fixture reflowed, and a
    raw substring test would miss it.  Both sides of the comparison go through
    this, so a value only has to survive re-wrapping to be caught.
    """

    return " ".join(str(text).split())


def _is_candidate_value(value: str) -> bool:
    normalised = normalise_whitespace(value)
    # A short string, or one without several real words, is not distinctive
    # enough for its presence in the tree to be evidence of anything.
    return len(normalised) >= LEAKAGE_MIN_VALUE_CHARS and len(normalised.split()) >= 4


def _walk_strings(node: Any, out: list[str], *, limit: int) -> None:
    """Collect candidate strings from one parsed element, depth-first, bounded."""

    if len(out) >= limit:
        return
    if isinstance(node, str):
        if _is_candidate_value(node):
            out.append(normalise_whitespace(node))
        return
    if isinstance(node, Mapping):
        for key in sorted(node):
            _walk_strings(node[key], out, limit=limit)
            if len(out) >= limit:
                return
        return
    if isinstance(node, (list, tuple)):
        for item in node:
            _walk_strings(item, out, limit=limit)
            if len(out) >= limit:
                return


def _sample_by_regex(path: Path, *, prefix_bytes: int) -> list[str]:
    """Fallback for a file that is not a top-level JSON array.

    Each captured literal is unescaped with ``json.loads`` before it is used:
    the sampler must hand out the string a fixture would contain, not the
    escaped source substring.  Before this, any value carrying a quote, a
    newline or a ``\\uXXXX`` escape -- which is most dialogue text -- could not
    be matched at all.
    """

    with open(path, "rb") as handle:
        blob = handle.read(prefix_bytes)
    text = blob.decode("utf-8", errors="ignore")
    found: list[str] = []
    for match in _JSON_STRING_RE.finditer(text):
        try:
            value = json.loads('"' + match.group(1) + '"')
        except json.JSONDecodeError:
            continue
        if _is_candidate_value(value):
            found.append(normalise_whitespace(value))
    return found


def sample_dataset_values(
    path: Path,
    *,
    sample_size: int = LEAKAGE_SAMPLE_SIZE,
    per_element: int = LEAKAGE_VALUES_PER_ELEMENT,
    prefix_bytes: int = LEAKAGE_SCAN_PREFIX_BYTES,
) -> list[str]:
    """Sample string values from the **whole** file, streaming, unescaped.

    The previous implementation read an 8 MiB prefix, which is 2.9 % of
    ``longmemeval_s_cleaned.json`` -- roughly the first 15 of 500 instances --
    and returned the raw escaped literal rather than the string a fixture would
    hold.  Both defects meant the strongest leakage assertion could not see the
    values most likely to be copied.

    Now: :func:`iter_json_array` streams the array one element at a time, so
    memory stays bounded by the largest single instance rather than by the
    file; each element contributes at most ``per_element`` values, so coverage
    is uniform across the file instead of concentrated at its head; and every
    value arrives already unescaped, because it came through the JSON parser.
    Ordering is by digest, so the sample is a pure function of the file.
    """

    collected: list[str] = []
    try:
        for element in iter_json_array(Path(path)):
            per: list[str] = []
            _walk_strings(element, per, limit=max(1, int(per_element)))
            collected.extend(per)
    except DatasetError:
        collected = _sample_by_regex(Path(path), prefix_bytes=prefix_bytes)
    ordered = sorted(
        set(collected), key=lambda value: hashlib.sha256(value.encode("utf-8")).hexdigest()
    )
    return ordered[:sample_size]


def tracked_text_files(root: Path | None = None) -> list[Path]:
    """Every tracked file whose suffix says it could carry prose."""

    base = (root or repository_root()).resolve()
    try:
        output = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=base,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as exc:  # pragma: no cover - no git
        raise DatasetError(f"could not list tracked files: {exc}", code="git_unavailable") from exc
    paths: list[Path] = []
    for raw in output.split(b"\0"):
        if not raw:
            continue
        relative = raw.decode("utf-8", errors="surrogateescape")
        candidate = base / relative
        if candidate.suffix.casefold() in _TEXT_SUFFIXES and candidate.is_file():
            paths.append(candidate)
    return paths


def dataset_value_leakage_findings(
    values: Sequence[str],
    *,
    root: Path | None = None,
    files: Iterable[Path] | None = None,
) -> list[str]:
    """Report every tracked file that contains a sampled dataset value.

    A finding names the file and the value's digest, never the value: the whole
    point of the check is that dataset text must not be written down here, and
    a failure message is written down too.
    """

    base = (root or repository_root()).resolve()
    candidates = list(files) if files is not None else tracked_text_files(base)
    # Both sides are whitespace-normalised: a value carrying a newline is
    # re-wrapped when it is pasted into a fixture, and a raw substring test
    # would miss exactly the values worth catching.
    wanted = [
        (normalise_whitespace(value), hashlib.sha256(value.encode("utf-8")).hexdigest()[:12])
        for value in values
        if normalise_whitespace(value)
    ]
    if not wanted:
        return []
    findings: list[str] = []
    for candidate in candidates:
        try:
            text = normalise_whitespace(candidate.read_text(encoding="utf-8", errors="ignore"))
        except OSError:  # pragma: no cover - unreadable tracked file
            continue
        for needle, digest in wanted:
            if needle in text:
                try:
                    shown = candidate.relative_to(base).as_posix()
                except ValueError:  # pragma: no cover - outside the tree
                    shown = candidate.name
                findings.append(f"{shown}: contains dataset value {digest}")
    return sorted(set(findings))


@dataclass(frozen=True)
class LeakageScan:
    """What the fetch-time gate inspected, so "skipped" is never inferred."""

    findings: tuple[str, ...]
    values_sampled: int
    full_file: bool
    bytes_scanned: int

    @property
    def clean(self) -> bool:
        return not self.findings

    def summary(self) -> str:
        coverage = "whole file" if self.full_file else "bounded prefix (fallback)"
        return (
            f"{self.values_sampled} sampled values, {coverage}, "
            f"{self.bytes_scanned:,} bytes"
        )


def scan_cached_dataset_for_leakage(
    handle: DatasetHandle,
    *,
    root: Path | None = None,
) -> LeakageScan:
    """The fetch-time leakage gate: sample the fetched file, scan the tree."""

    values = sample_dataset_values(handle.path)
    full_file = True
    try:
        for _element in iter_json_array(handle.path):
            break
    except DatasetError:
        full_file = False
    return LeakageScan(
        findings=tuple(dataset_value_leakage_findings(values, root=root)),
        values_sampled=len(values),
        full_file=full_file,
        bytes_scanned=handle.path.stat().st_size if full_file else LEAKAGE_SCAN_PREFIX_BYTES,
    )
