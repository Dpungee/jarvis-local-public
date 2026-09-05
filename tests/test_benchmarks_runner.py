"""The public benchmark runner: cache, driver, report, the three benchmarks, CLI.

No test here reaches the network and no test writes a dataset byte inside the
tree. Every fixture is built in-test, in a temporary directory, **in the
datasets' exact published formats**, so the parsers are exercised against the
shape they will actually meet.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts.benchmarks import (
    cache,
    driver,
    locomo,
    longmemeval,
    report,
    ruler_style,
    run as run_cli,
    scoring,
    synthetic,
)


# ---------------------------------------------------------------------------
# Fixtures in the datasets' exact formats
# ---------------------------------------------------------------------------


def longmemeval_records(count: int = 6) -> list[dict[str, object]]:
    """LongMemEval's published instance shape, with one abstention id."""

    records: list[dict[str, object]] = []
    abilities = ["information-extraction", "knowledge-update", "temporal-reasoning"]
    for index in range(count):
        ability = abilities[index % len(abilities)]
        abstention = count > 1 and index == count - 1
        question_id = f"probe{index}" + ("_abs" if abstention else "")
        records.append(
            {
                "question_id": question_id,
                "question_type": "abstention" if abstention else ability,
                "question": f"What is the marker for widget {index}?",
                "answer": "" if abstention else f"marker-{index}",
                "question_date": "2026-09-04",
                "haystack_session_ids": [f"s{index}a", f"s{index}b"],
                "haystack_dates": ["2026-08-01", "2026-08-15"],
                "haystack_sessions": [
                    [
                        {"role": "user", "content": f"The marker for widget {index} is marker-{index}."},
                        {"role": "assistant", "content": "Noted.", "has_answer": True},
                    ],
                    [
                        {"role": "system", "content": "unrelated preamble"},
                        {"role": "user", "content": "Anything else outstanding?"},
                        {"role": "assistant", "content": "Nothing outstanding."},
                        {"role": "user", "content": "   "},
                    ],
                ],
                "answer_session_ids": [f"s{index}a"],
            }
        )
    return records


def locomo_records(samples: int = 2, questions: int = 5) -> list[dict[str, object]]:
    """LoCoMo's published sample shape, including the adversarial category."""

    records: list[dict[str, object]] = []
    for sample in range(samples):
        conversation: dict[str, object] = {"speaker_a": "Wren", "speaker_b": "Alder"}
        for session in (1, 2):
            conversation[f"session_{session}"] = [
                {"speaker": "Wren", "dia_id": f"D{session}:1", "text": f"I set dial {sample} to {session}0."},
                {"speaker": "Alder", "dia_id": f"D{session}:2", "text": "Understood."},
            ]
            conversation[f"session_{session}_date_time"] = f"1:0{session} pm on 4 September, 2026"
        qa: list[dict[str, object]] = []
        for index in range(questions):
            category = (index % 5) + 1
            if category == 5:
                qa.append(
                    {
                        "question": "What did Wren say about the Osprey ledger?",
                        "adversarial_answer": "No information available.",
                        "category": 5,
                    }
                )
            else:
                qa.append(
                    {
                        "question": f"What did Wren set dial {sample} to first?",
                        "answer": "10",
                        "category": category,
                        "evidence": ["D1:1"],
                    }
                )
        records.append({"sample_id": f"conv-{sample}", "conversation": conversation, "qa": qa})
    return records


def write_json(path: Path, payload: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# A backend stub: real SQL, no provider, no jarvis import
# ---------------------------------------------------------------------------


class _StubReply(str):
    def __new__(cls, text: str, *, model: str, status: str = "complete", tool_calls: int = 0):
        instance = str.__new__(cls, text)
        instance.model = model
        instance.status = status
        instance.tool_calls = tool_calls
        return instance


SONNET = "claude-cli:claude-sonnet-4-5"


class _StubMemory:
    """Enough of ``Memory`` for the driver, with a real metrics table.

    The metrics table carries a ``model`` column because that is where the
    driver now reads the observed model from: the configured hint cannot
    attest to itself.
    """

    def __init__(self, *, compaction: bool = False) -> None:
        self.db = sqlite3.connect(":memory:")
        self.db.execute(
            "CREATE TABLE model_call_metrics (id INTEGER PRIMARY KEY, "
            "model TEXT, prompt_tokens INTEGER, completion_tokens INTEGER)"
        )
        self.conversations: list[str] = []
        self.messages: list[tuple[int, str, str]] = []
        self.compacted: list[int] = []
        self.closed = False
        if compaction:
            self.compact_conversation = self._compact  # type: ignore[attr-defined]

    def _compact(self, conversation_id: int) -> None:
        self.compacted.append(int(conversation_id))

    def new_conversation(self, title: str) -> int:
        self.conversations.append(title)
        return len(self.conversations)

    def add_message(self, conversation_id: int, role: str, content: str) -> int:
        self.messages.append((conversation_id, role, content))
        return len(self.messages)

    def record_call(self, prompt: int, completion: int, model: str = SONNET) -> None:
        self.db.execute(
            "INSERT INTO model_call_metrics(model, prompt_tokens, completion_tokens) "
            "VALUES (?, ?, ?)",
            (model, prompt, completion),
        )

    def close(self) -> None:
        self.closed = True
        self.db.close()


class _StubConfig:
    def __init__(self, *, context_length: int, memory_embeddings: str) -> None:
        self.context_length = context_length
        self.memory_embeddings = memory_embeddings


class _StubAgent:
    def __init__(
        self,
        memory: _StubMemory,
        *,
        model: str,
        explode: bool = False,
        served_model: str | None = None,
        record_call: bool = True,
        config: _StubConfig | None = None,
    ) -> None:
        self.memory = memory
        self.model = model
        self.explode = explode
        self.served_model = served_model
        self.record = record_call
        self.config = config
        self.prompts: list[str] = []
        self.conversation_ids: list[object] = []

    def run(self, prompt: str, conversation_id: object = None):
        self.prompts.append(prompt)
        self.conversation_ids.append(conversation_id)
        if self.explode:
            raise TimeoutError("provider timed out")
        if self.record:
            self.memory.record_call(120, 8, self.served_model or SONNET)
        haystack = " ".join(content for _cid, _role, content in self.memory.messages)
        return _StubReply(
            f"Reference says: {haystack[:400]}" if haystack else "Not recorded.",
            model=self.model,
            tool_calls=1,
        )


class _StubBackend:
    def __init__(
        self,
        *,
        compaction: bool = False,
        explode: bool = False,
        served_model: str | None = None,
        record_call: bool = True,
        applies_runtime: bool = True,
    ) -> None:
        self.compaction = compaction
        self.explode = explode
        self.served_model = served_model
        self.record_call = record_call
        self.applies_runtime = applies_runtime
        self.context_length: int | None = None
        self.embeddings = "disabled"
        self.opened: list[Path] = []
        self.closed = 0
        self.last: tuple[_StubMemory, _StubAgent] | None = None

    def open_store(self, store_dir: Path, model: str):
        self.opened.append(Path(store_dir))
        memory = _StubMemory(compaction=self.compaction)
        applied_length = self.context_length or driver.DEFAULT_CONTEXT_LENGTH
        applied_embeddings = self.embeddings
        if not self.applies_runtime:
            # A backend that publishes the runtime block and then ignores it --
            # the H-5 defect, reproduced so the assert-back can catch it.
            applied_length = driver.DEFAULT_CONTEXT_LENGTH
            applied_embeddings = "disabled"
        agent = _StubAgent(
            memory,
            model=model,
            explode=self.explode,
            served_model=self.served_model,
            record_call=self.record_call,
            config=_StubConfig(
                context_length=applied_length, memory_embeddings=applied_embeddings
            ),
        )
        self.last = (memory, agent)
        return memory, agent

    def close_store(self, memory, agent) -> None:  # noqa: ANN001 - stub
        del agent
        self.closed += 1
        memory.close()


class _StubDirectProvider:
    """The control arm's transport, without a provider."""

    def __init__(self, *, reply: str = "", served: str | None = SONNET, explode: bool = False) -> None:
        self.reply = reply
        self.served = served
        self.explode = explode
        self.prompts: list[str] = []

    def complete(self, prompt: str, model: str) -> tuple[str, str | None, int | None, int | None]:
        self.prompts.append(prompt)
        del model
        if self.explode:
            raise ConnectionError("provider unreachable")
        return self.reply or prompt[-200:], self.served, len(prompt) // 4, 5


class _TempCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="jarvis-bench-test-")
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)


# ---------------------------------------------------------------------------
# cache
# ---------------------------------------------------------------------------


class CacheLocationTests(_TempCase):
    def test_a_cache_inside_the_repository_is_refused(self) -> None:
        inside = cache.repository_root() / "reports" / "bench-cache"
        with self.assertRaises(cache.DatasetError) as caught:
            cache.resolve_cache_dir(inside)
        self.assertEqual(caught.exception.code, "cache_inside_repository")

    def test_the_repository_root_itself_is_refused(self) -> None:
        with self.assertRaises(cache.DatasetError):
            cache.resolve_cache_dir(cache.repository_root())

    def test_a_traversal_back_into_the_repository_is_refused(self) -> None:
        sneaky = cache.repository_root() / "docs" / ".." / "jarvis"
        with self.assertRaises(cache.DatasetError):
            cache.resolve_cache_dir(sneaky)

    def test_a_directory_outside_the_repository_is_accepted(self) -> None:
        self.assertEqual(cache.resolve_cache_dir(self.tmp), Path(os.path.normpath(str(self.tmp))))

    def test_the_environment_variable_names_the_cache(self) -> None:
        resolved = cache.default_cache_dir({cache.CACHE_ENV: str(self.tmp / "elsewhere")})
        self.assertEqual(resolved, self.tmp / "elsewhere")

    def test_the_default_falls_back_to_a_home_cache_directory(self) -> None:
        resolved = cache.default_cache_dir({"HOME": str(self.tmp)})
        self.assertEqual(resolved.name, cache.DEFAULT_CACHE_DIRNAME)

    def test_presence_of_the_commercial_use_variable_is_the_declaration(self) -> None:
        self.assertTrue(cache.commercial_use_declared({cache.COMMERCIAL_USE_ENV: "0"}))
        self.assertFalse(cache.commercial_use_declared({}))


class CacheResolutionHardeningTests(_TempCase):
    """H-1: a junction, an 8.3 short name or a ``\\?\\`` prefix is the same path.

    ``Path.absolute()`` is a pure string operation, so the containment check was
    comparing against a path the filesystem does not agree with.  A 2.8 MB
    CC BY-NC dataset planted in the tree that way sits under
    ``MAX_TRACKED_FILE_BYTES`` and survives ``git add -A``.
    """

    def test_the_extended_length_prefix_is_stripped_before_resolving(self) -> None:
        if os.name != "nt":
            self.skipTest("the \\\\?\\ prefix is a Windows form")
        target = cache.repository_root() / "bench-cache-via-prefix"
        with self.assertRaises(cache.DatasetError) as caught:
            cache.resolve_cache_dir(f"\\\\?\\{target}")
        self.assertEqual(caught.exception.code, "cache_inside_repository")

    def test_an_eight_dot_three_short_name_resolves_back_into_the_tree(self) -> None:
        if os.name != "nt":
            self.skipTest("8.3 short names are a Windows form")
        try:
            import ctypes

            buffer = ctypes.create_unicode_buffer(1024)
            length = ctypes.windll.kernel32.GetShortPathNameW(
                str(cache.repository_root()), buffer, 1024
            )
        except (AttributeError, OSError):  # pragma: no cover - no win32 API
            self.skipTest("GetShortPathNameW is unavailable")
        if not length:
            self.skipTest("this volume does not expose 8.3 short names")
        short = Path(buffer.value)
        if short == cache.repository_root():
            self.skipTest("8.3 short names are disabled on this volume")
        with self.assertRaises(cache.DatasetError) as caught:
            cache.resolve_cache_dir(short / "bench-cache-via-short-name")
        self.assertEqual(caught.exception.code, "cache_inside_repository")

    def test_a_junction_into_the_tree_is_refused_and_nothing_is_written(self) -> None:
        if os.name != "nt":
            self.skipTest("mklink /J is a Windows command")
        junction = self.tmp / "junction-into-tree"
        completed = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(junction), str(cache.repository_root())],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if completed.returncode != 0 or not junction.exists():
            self.skipTest("mklink /J is unavailable in this environment")
        # Remove the link itself, never its target.
        self.addCleanup(lambda: junction.is_dir() and os.rmdir(junction))
        with self.assertRaises(cache.DatasetError) as caught:
            cache.resolve_cache_dir(junction / "bench-cache-via-junction")
        self.assertEqual(caught.exception.code, "cache_inside_repository")
        with self.assertRaises(cache.DatasetError):
            cache.ensure_dataset(
                cache.DATASETS["locomo10"],
                cache_dir=junction / "bench-cache-via-junction",
                env={},
                allow_fetch=True,
                fetcher=lambda *_: self.fail("nothing may be fetched through a junction"),
            )
        self.assertFalse((cache.repository_root() / "bench-cache-via-junction").exists())

    def test_a_symlink_into_the_tree_is_refused_when_the_host_allows_one(self) -> None:
        link = self.tmp / "symlink-into-tree"
        try:
            link.symlink_to(cache.repository_root(), target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("this host does not permit creating a directory symlink")
        self.addCleanup(lambda: link.is_symlink() and link.unlink())
        with self.assertRaises(cache.DatasetError) as caught:
            cache.resolve_cache_dir(link / "bench")
        self.assertEqual(caught.exception.code, "cache_inside_repository")

    def test_real_path_leaves_a_legitimate_outside_directory_alone(self) -> None:
        outside = self.tmp / "not-yet-created" / "deeper"
        self.assertEqual(cache.real_path(outside), cache.real_path(self.tmp) / "not-yet-created" / "deeper")
        self.assertEqual(cache.resolve_cache_dir(outside), cache.real_path(outside))

    def test_real_path_resolves_the_nearest_existing_ancestor(self) -> None:
        # The tail need not exist; the ancestor that does is where any link or
        # short name lives.
        resolved = cache.real_path(self.tmp / "a" / "b" / "c")
        self.assertTrue(str(resolved).endswith(os.path.join("a", "b", "c")))


class EnsureDatasetTests(_TempCase):
    def _spec(self, **overrides) -> cache.DatasetSpec:
        base = dict(
            name="probe",
            benchmark="probe",
            url="https://example.invalid/probe.json",
            filename="probe.json",
            sha256=None,
            bytes=None,
            licence="MIT",
            licence_class="open",
        )
        base.update(overrides)
        return cache.DatasetSpec(**base)  # type: ignore[arg-type]

    def _plant(self, payload: str = "[]", *, licence: str | None = None) -> cache.DatasetSpec:
        directory = self.tmp / "probe"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "probe.json").write_text(payload, encoding="utf-8")
        if licence is not None:
            (directory / cache.LICENCE_FILENAME).write_text(licence, encoding="utf-8")
        return self._spec()

    def test_an_unknown_licence_class_is_refused_at_construction(self) -> None:
        with self.assertRaises(cache.DatasetError) as caught:
            self._spec(licence_class="whatever")
        self.assertEqual(caught.exception.code, "licence_class_invalid")

    def test_a_restricted_dataset_refuses_under_a_declared_commercial_use(self) -> None:
        spec = self._spec(licence_class="restricted", licence="CC BY-NC 4.0")
        with self.assertRaises(cache.DatasetError) as caught:
            cache.ensure_dataset(
                spec,
                cache_dir=self.tmp,
                env={cache.COMMERCIAL_USE_ENV: "yes"},
                allow_fetch=True,
                fetcher=lambda *_: self.fail("nothing may be fetched"),
            )
        self.assertEqual(caught.exception.code, "commercial_use_declared")
        self.assertFalse((self.tmp / "probe").exists())

    def test_a_missing_dataset_refuses_rather_than_fetching(self) -> None:
        with self.assertRaises(cache.DatasetError) as caught:
            cache.ensure_dataset(self._spec(), cache_dir=self.tmp, env={})
        self.assertEqual(caught.exception.code, "dataset_not_cached")

    def test_fetching_happens_only_behind_the_explicit_flag(self) -> None:
        calls: list[str] = []

        def _fetcher(url: str, destination: Path) -> None:
            calls.append(url)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text("[]", encoding="utf-8")

        handle = cache.ensure_dataset(
            self._spec(),
            cache_dir=self.tmp,
            env={},
            allow_fetch=True,
            scored=False,
            fetcher=_fetcher,
        )
        self.assertEqual(calls, ["https://example.invalid/probe.json"])
        self.assertTrue(handle.fetched)
        self.assertFalse(handle.pinned)
        self.assertEqual(handle.bytes, 2)

    def test_an_unpinned_digest_refuses_a_scored_run(self) -> None:
        spec = self._plant()
        with self.assertRaises(cache.DatasetError) as caught:
            cache.ensure_dataset(spec, cache_dir=self.tmp, env={})
        self.assertEqual(caught.exception.code, "digest_unpinned")
        self.assertIn("Observed", str(caught.exception))

    def test_a_digest_mismatch_refuses(self) -> None:
        self._plant()
        spec = self._spec(sha256="0" * 64)
        with self.assertRaises(cache.DatasetError) as caught:
            cache.ensure_dataset(spec, cache_dir=self.tmp, env={})
        self.assertEqual(caught.exception.code, "dataset_digest_mismatch")

    def test_a_pinned_digest_resolves(self) -> None:
        self._plant()
        digest = cache.sha256_file(self.tmp / "probe" / "probe.json")
        handle = cache.ensure_dataset(self._spec(sha256=digest), cache_dir=self.tmp, env={})
        self.assertTrue(handle.pinned)
        self.assertEqual(handle.sha256, digest)
        self.assertIn("licence", handle.as_config())

    def test_open_licence_drift_is_reported_not_refused(self) -> None:
        self._plant(licence="MIT text")
        digest = cache.sha256_file(self.tmp / "probe" / "probe.json")
        spec = self._spec(
            sha256=digest,
            licence_url="https://example.invalid/LICENSE",
            licence_sha256="1" * 64,
        )
        handle = cache.ensure_dataset(spec, cache_dir=self.tmp, env={})
        self.assertTrue(handle.licence_drift)

    def test_restricted_licence_drift_refuses_the_run(self) -> None:
        self._plant(licence="CC BY-NC text")
        digest = cache.sha256_file(self.tmp / "probe" / "probe.json")
        spec = self._spec(
            sha256=digest,
            licence_class="restricted",
            licence="CC BY-NC 4.0",
            licence_url="https://example.invalid/LICENSE",
            licence_sha256="1" * 64,
        )
        with self.assertRaises(cache.DatasetError) as caught:
            cache.ensure_dataset(spec, cache_dir=self.tmp, env={})
        self.assertEqual(caught.exception.code, "licence_digest_mismatch")

    def test_a_restricted_dataset_needs_its_licence_text_cached(self) -> None:
        self._plant()
        digest = cache.sha256_file(self.tmp / "probe" / "probe.json")
        spec = self._spec(
            sha256=digest,
            licence_class="restricted",
            licence="CC BY-NC 4.0",
            licence_url="https://example.invalid/LICENSE",
        )
        with self.assertRaises(cache.DatasetError) as caught:
            cache.ensure_dataset(spec, cache_dir=self.tmp, env={})
        self.assertEqual(caught.exception.code, "licence_not_cached")

    def test_the_licence_is_refetched_so_drift_can_actually_be_seen(self) -> None:
        # M-1: digesting the cached copy compared the file with itself, so after
        # the first fetch drift -- the whole point of pinning a licence -- could
        # never be detected.
        texts = iter(["first licence text", "second licence text"])
        urls: list[str] = []

        def _fetcher(url: str, destination: Path) -> None:
            urls.append(url)
            destination.parent.mkdir(parents=True, exist_ok=True)
            if url.endswith("LICENSE"):
                destination.write_text(next(texts), encoding="utf-8")
            else:
                destination.write_text("[]", encoding="utf-8")

        spec = self._spec(licence_url="https://example.invalid/LICENSE")
        first = cache.ensure_dataset(
            spec, cache_dir=self.tmp, env={}, allow_fetch=True, scored=False, fetcher=_fetcher
        )
        second = cache.ensure_dataset(
            spec, cache_dir=self.tmp, env={}, allow_fetch=True, scored=False, fetcher=_fetcher
        )
        self.assertEqual(urls.count("https://example.invalid/LICENSE"), 2)
        self.assertFalse(first.licence_drift)
        self.assertTrue(second.licence_drift)
        self.assertNotEqual(second.licence_sha256, second.previous_licence_sha256)

    def test_a_run_reads_the_cached_licence_without_refetching(self) -> None:
        self._plant(licence="stable licence text")
        digest = cache.sha256_file(self.tmp / "probe" / "probe.json")
        spec = self._spec(sha256=digest, licence_url="https://example.invalid/LICENSE")
        handle = cache.ensure_dataset(
            spec,
            cache_dir=self.tmp,
            env={},
            fetcher=lambda *_: self.fail("a run must not fetch"),
        )
        self.assertIsNotNone(handle.licence_sha256)
        self.assertFalse(handle.licence_drift)

    def test_spec_for_applies_only_the_allowed_overrides(self) -> None:
        spec = cache.spec_for("locomo10", overrides={"sha256": "a" * 64, "licence": "MIT"})
        self.assertEqual(spec.sha256, "a" * 64)
        self.assertEqual(spec.licence, "CC BY-NC 4.0")

    def test_spec_for_rejects_an_unknown_dataset(self) -> None:
        with self.assertRaises(cache.DatasetError) as caught:
            cache.spec_for("nope")
        self.assertEqual(caught.exception.code, "unknown_dataset")

    def test_the_registry_declares_locomo_as_restricted(self) -> None:
        self.assertEqual(cache.DATASETS["locomo10"].licence_class, "restricted")
        self.assertEqual(cache.DATASETS["longmemeval_s"].licence_class, "open")

    def test_a_non_https_url_is_refused_before_any_socket_opens(self) -> None:
        with self.assertRaises(cache.DatasetError) as caught:
            cache.urllib_fetch("http://example.invalid/x.json", self.tmp / "x.json")
        self.assertEqual(caught.exception.code, "insecure_url")


    def test_a_unc_extended_prefix_is_stripped_too(self) -> None:
        # Built from chr(92) so the escaping is unambiguous in the source.
        back = chr(92)
        unc = back * 2 + "?" + back + "UNC" + back + "server" + back + "share"
        stripped = cache._strip_extended_prefix(Path(unc))
        # Path normalises a UNC root with a trailing separator on Windows.
        self.assertTrue(str(stripped).startswith(back * 2 + "server" + back + "share"))
        self.assertNotIn("?", str(stripped))
        plain = Path("relative") / "path"
        self.assertEqual(cache._strip_extended_prefix(plain), plain)

    def test_a_regex_literal_that_will_not_unescape_is_skipped(self) -> None:
        path = self.tmp / "broken.json"
        # A top-level object takes the bounded fallback; the bad escape inside
        # must be skipped rather than crash the scan.
        path.write_text(
            '{"a": "a real sentence with several words in it here",'
            ' "b": "a bad escape \\q with several words in it here"}',
            encoding="utf-8",
        )
        values = cache.sample_dataset_values(path)
        self.assertIn("a real sentence with several words in it here", values)
        self.assertTrue(all("bad escape" not in value for value in values))

    def test_the_walk_stops_at_the_per_element_ceiling(self) -> None:
        element = {
            f"k{index}": f"sentence number {index} with several real words in it"
            for index in range(20)
        }
        collected: list[str] = []
        cache._walk_strings([element], collected, limit=3)
        self.assertEqual(len(collected), 3)
        nested: list[str] = []
        cache._walk_strings({"a": [{"b": ["short", 7, None]}]}, nested, limit=3)
        self.assertEqual(nested, [])


class UrllibFetchTests(_TempCase):
    """The one network call in the package, exercised without a socket."""

    class _Response:
        def __init__(self, chunks: list[bytes]) -> None:
            self._chunks = list(chunks)

        def read(self, _size: int) -> bytes:
            return self._chunks.pop(0) if self._chunks else b""

        def __enter__(self):
            return self

        def __exit__(self, *_exc) -> bool:
            return False

    def test_a_stream_lands_whole_and_leaves_no_partial_file(self) -> None:
        target = self.tmp / "nested" / "data.json"
        with mock.patch(
            "urllib.request.urlopen", return_value=self._Response([b"[1,", b"2]"])
        ):
            cache.urllib_fetch("https://example.invalid/data.json", target)
        self.assertEqual(target.read_text(encoding="utf-8"), "[1,2]")
        self.assertFalse(list(target.parent.glob("*.part")))

    def test_the_fetch_ceiling_refuses_and_removes_the_partial_file(self) -> None:
        target = self.tmp / "huge.json"
        with mock.patch.object(cache, "MAX_FETCH_BYTES", 4):
            with mock.patch(
                "urllib.request.urlopen", return_value=self._Response([b"12345"])
            ):
                with self.assertRaises(cache.DatasetError) as caught:
                    cache.urllib_fetch("https://example.invalid/huge.json", target)
        self.assertEqual(caught.exception.code, "fetch_too_large")
        self.assertFalse(target.exists())
        self.assertFalse(list(self.tmp.glob("*.part")))

    def test_a_transport_failure_is_reported_as_a_fetch_failure(self) -> None:
        with mock.patch("urllib.request.urlopen", side_effect=OSError("no route")):
            with self.assertRaises(cache.DatasetError) as caught:
                cache.urllib_fetch("https://example.invalid/x.json", self.tmp / "x.json")
        self.assertEqual(caught.exception.code, "fetch_failed")


class StreamingJsonTests(_TempCase):
    def test_a_large_array_streams_element_by_element(self) -> None:
        payload = [{"i": index, "text": "x" * 50} for index in range(40)]
        path = write_json(self.tmp / "big.json", payload)
        streamed = list(cache.iter_json_array(path, chunk_bytes=17))
        self.assertEqual(streamed, payload)

    def test_an_empty_array_yields_nothing(self) -> None:
        path = write_json(self.tmp / "empty.json", [])
        self.assertEqual(list(cache.iter_json_array(path)), [])

    def test_a_non_array_top_level_is_refused(self) -> None:
        path = write_json(self.tmp / "object.json", {"a": 1})
        with self.assertRaises(cache.DatasetError) as caught:
            list(cache.iter_json_array(path))
        self.assertEqual(caught.exception.code, "dataset_malformed")

    def test_an_empty_file_is_refused(self) -> None:
        path = self.tmp / "blank.json"
        path.write_text("   ", encoding="utf-8")
        with self.assertRaises(cache.DatasetError):
            list(cache.iter_json_array(path))

    def test_an_array_whose_closing_bracket_is_missing_ends_cleanly(self) -> None:
        path = self.tmp / "unclosed.json"
        path.write_text('[{"a": 1}, {"b": 2}   ', encoding="utf-8")
        self.assertEqual(list(cache.iter_json_array(path, chunk_bytes=5)), [{"a": 1}, {"b": 2}])

    def test_a_truncated_download_names_the_broken_element(self) -> None:
        path = self.tmp / "torn.json"
        path.write_text('[{"a": 1}, {"b": ', encoding="utf-8")
        with self.assertRaises(cache.DatasetError) as caught:
            list(cache.iter_json_array(path, chunk_bytes=4))
        self.assertEqual(caught.exception.code, "dataset_malformed")


class LeakageScanTests(_TempCase):
    def test_sampling_is_deterministic_and_bounded(self) -> None:
        path = write_json(self.tmp / "d.json", longmemeval_records(4))
        first = cache.sample_dataset_values(path, sample_size=5)
        second = cache.sample_dataset_values(path, sample_size=5)
        self.assertEqual(first, second)
        self.assertLessEqual(len(first), 5)
        for value in first:
            self.assertGreaterEqual(len(value), cache.LEAKAGE_MIN_VALUE_CHARS)

    def test_short_or_wordless_values_are_never_sampled(self) -> None:
        path = write_json(self.tmp / "s.json", [{"a": "short", "b": "x" * 60}])
        self.assertEqual(cache.sample_dataset_values(path), [])

    def test_a_planted_value_is_found_and_never_echoed(self) -> None:
        secret = "the quick brown widget marker sentence for leakage probing"
        planted = self.tmp / "tracked.md"
        planted.write_text(f"prose\n{secret}\nmore prose\n", encoding="utf-8")
        findings = cache.dataset_value_leakage_findings(
            [secret], root=self.tmp, files=[planted]
        )
        self.assertEqual(len(findings), 1)
        self.assertIn("tracked.md", findings[0])
        self.assertNotIn(secret, findings[0])

    def test_no_findings_when_nothing_leaked(self) -> None:
        clean = self.tmp / "clean.md"
        clean.write_text("nothing to see", encoding="utf-8")
        self.assertEqual(
            cache.dataset_value_leakage_findings(["absent phrase here"], root=self.tmp, files=[clean]),
            [],
        )

    def test_an_empty_sample_short_circuits(self) -> None:
        self.assertEqual(cache.dataset_value_leakage_findings([], root=self.tmp, files=[]), [])

    def test_the_fetch_gate_reports_the_sampled_count(self) -> None:
        path = write_json(self.tmp / "d.json", longmemeval_records(3))
        handle = cache.DatasetHandle(
            name="probe", benchmark="probe", path=path, sha256="0" * 64, bytes=path.stat().st_size,
            licence="MIT", licence_class="open", licence_path=None, licence_sha256=None,
            pinned=True, licence_drift=False, fetched=True,
        )
        clean = self.tmp / "clean.md"
        clean.write_text("nothing", encoding="utf-8")
        with mock.patch.object(cache, "tracked_text_files", return_value=[clean]):
            scan = cache.scan_cached_dataset_for_leakage(handle, root=self.tmp)
        self.assertTrue(scan.clean)
        self.assertGreater(scan.values_sampled, 0)
        self.assertTrue(scan.full_file)
        self.assertIn("whole file", scan.summary())

    def test_tracked_text_files_lists_only_prose_suffixes(self) -> None:
        listed = cache.tracked_text_files()
        self.assertTrue(any(path.name == "README.md" for path in listed))
        self.assertTrue(all(path.suffix.casefold() in cache._TEXT_SUFFIXES for path in listed))


class LeakageScanCoverageTests(_TempCase):
    """H-3: the scan must see the whole file and the string a fixture holds."""

    ESCAPED_FORMS = (
        'She said "the gate count is four" during the survey call today.',
        "Le reglage du Harrier box a \u00e9t\u00e9 modifie en mars dernier ici.",
        "The Harrier box offset changed.\nIt was revised again in March.",
        "The Harrier box calibration offset was revised again in March.",
        "A tab\tseparated survey note about the Millrace weir gate count.",
    )

    def _dataset(self, *, planted: str, padding_elements: int = 0) -> Path:
        elements = [
            {"question_id": f"pad{index}", "text": f"padding sentence number {index} " * 12}
            for index in range(padding_elements)
        ]
        elements.append({"question_id": "planted", "question": planted})
        return write_json(self.tmp / "dataset.json", elements)

    def test_a_planted_value_is_found_end_to_end_in_every_escaped_form(self) -> None:
        # The old proof called the comparator directly with a phrase it chose,
        # so it demonstrated nothing about whether the sampler would ever
        # produce that phrase.  This runs the real sampler.
        for form in self.ESCAPED_FORMS:
            with self.subTest(form=form[:40]):
                path = self._dataset(planted=form)
                values = cache.sample_dataset_values(path)
                self.assertIn(cache.normalise_whitespace(form), values)
                leaked = self.tmp / "fixture.md"
                leaked.write_text(f"prose\n{form}\nmore prose\n", encoding="utf-8")
                findings = cache.dataset_value_leakage_findings(
                    values, root=self.tmp, files=[leaked]
                )
                self.assertEqual(len(findings), 1, form)
                self.assertNotIn(form[:30], findings[0])

    def test_a_value_reflowed_into_a_fixture_is_still_caught(self) -> None:
        form = "The Harrier box offset changed.\nIt was revised again in March."
        path = self._dataset(planted=form)
        values = cache.sample_dataset_values(path)
        reflowed = self.tmp / "reflowed.md"
        reflowed.write_text(
            "The Harrier box offset changed.\n    It was revised again in March.\n",
            encoding="utf-8",
        )
        findings = cache.dataset_value_leakage_findings(values, root=self.tmp, files=[reflowed])
        self.assertEqual(len(findings), 1)

    def test_a_value_far_past_the_old_prefix_bound_is_still_sampled(self) -> None:
        # The 8 MiB prefix saw 2.9% of longmemeval_s -- roughly the first 15 of
        # 500 instances -- so a question in instance 400 was structurally
        # invisible.
        planted = "A distinctive late question about the Millrace weir gate count."
        path = self._dataset(planted=planted, padding_elements=400)
        self.assertGreater(path.stat().st_size, 100_000)
        values = cache.sample_dataset_values(path, sample_size=4096)
        self.assertIn(cache.normalise_whitespace(planted), values)
        # And the direct demonstration that the prefix bound was the defect:
        # the old bounded reader cannot see the tail of the same file.
        prefix_only = cache._sample_by_regex(path, prefix_bytes=4096)
        self.assertNotIn(cache.normalise_whitespace(planted), prefix_only)

    def test_coverage_is_uniform_rather_than_concentrated_at_the_head(self) -> None:
        path = self._dataset(planted="tail question about the weir gate count here", padding_elements=60)
        values = cache.sample_dataset_values(path, sample_size=4096)
        # Every element contributes, so the last element's value is present.
        self.assertIn("tail question about the weir gate count here", values)
        self.assertGreater(len(values), 20)

    def test_the_regex_fallback_unescapes_before_comparing(self) -> None:
        # A file that is not a top-level array takes the bounded fallback, which
        # must still hand out the unescaped string.
        form = 'She said "the gate count is four" during the survey call today.'
        path = self.tmp / "object.json"
        path.write_text(json.dumps({"a": form}, ensure_ascii=True), encoding="utf-8")
        values = cache.sample_dataset_values(path)
        self.assertIn(cache.normalise_whitespace(form), values)

    def test_a_scan_of_a_non_array_file_says_it_was_not_a_full_file_scan(self) -> None:
        path = self.tmp / "object.json"
        path.write_text(
            json.dumps({"a": "a long enough sentence with several real words in it"}),
            encoding="utf-8",
        )
        handle = cache.DatasetHandle(
            name="probe", benchmark="probe", path=path, sha256="0" * 64,
            bytes=path.stat().st_size, licence="MIT", licence_class="open",
            licence_path=None, licence_sha256=None, pinned=True, licence_drift=False,
            fetched=False,
        )
        with mock.patch.object(cache, "tracked_text_files", return_value=[]):
            scan = cache.scan_cached_dataset_for_leakage(handle, root=self.tmp)
        self.assertFalse(scan.full_file)
        self.assertIn("fallback", scan.summary())

    def test_whitespace_normalisation_is_applied_to_both_sides(self) -> None:
        self.assertEqual(cache.normalise_whitespace("a\n b\tc  "), "a b c")


# ---------------------------------------------------------------------------
# driver
# ---------------------------------------------------------------------------


class DriverShapeTests(unittest.TestCase):
    def test_only_persistable_roles_are_accepted(self) -> None:
        driver.Turn(role="user", content="a")
        with self.assertRaises(driver.DriverError) as caught:
            driver.Turn(role="tool", content="a")
        self.assertEqual(caught.exception.code, "bad_role")

    def test_the_default_battery_model_is_the_operators_live_model(self) -> None:
        self.assertEqual(driver.battery_model({}), "claude-cli:claude-sonnet-4-5")
        self.assertEqual(driver.battery_model({"JARVIS_BATTERY_MODEL": "x:y"}), "x:y")

    def test_the_provider_environment_clears_the_nested_cli_marker(self) -> None:
        environment = {"CLAUDECODE": "1"}
        driver.prepare_provider_environment(environment)
        self.assertNotIn("CLAUDECODE", environment)
        self.assertEqual(environment["JARVIS_CLAUDE_CLI_ENABLED"], "true")

    def test_instance_source_chars_counts_every_turn(self) -> None:
        instance = driver.Instance(
            instance_id="i",
            sessions=(driver.Session("s", "", (driver.Turn("user", "abc"), driver.Turn("assistant", "de"))),),
            cases=(),
        )
        self.assertEqual(instance.source_chars, 5)

    def test_make_runner_rejects_an_unknown_provider(self) -> None:
        with self.assertRaises(driver.DriverError) as caught:
            driver.make_runner("openai")
        self.assertEqual(caught.exception.code, "unknown_provider")

    def test_reconfigure_stdout_is_safe_to_call(self) -> None:
        driver.reconfigure_stdout()


class FakeRunnerTests(unittest.TestCase):
    def _instance(self) -> driver.Instance:
        return driver.Instance(
            instance_id="i1",
            sessions=(driver.Session("s1", "", (driver.Turn("user", "the marker is alpha-7"),)),),
            cases=(driver.Case("c1", "what marker?", "alpha-7", "extraction"),),
        )

    def test_it_answers_from_the_ingested_transcript(self) -> None:
        runner = driver.FakeCaseRunner(wrong_every=0)
        runner.ingest(self._instance())
        outcome = runner.ask(driver.Case("c1", "what marker?", "alpha-7", "extraction"))
        self.assertEqual(outcome.reply, "alpha-7")
        self.assertEqual(outcome.model, driver.FAKE_MODEL)

    def test_it_abstains_when_the_evidence_was_never_ingested(self) -> None:
        runner = driver.FakeCaseRunner(wrong_every=0)
        runner.ingest(self._instance())
        outcome = runner.ask(driver.Case("c2", "?", "beta-9", "extraction"))
        self.assertTrue(outcome.reply.startswith("I have no recorded fact"))

    def test_the_deliberate_error_rate_is_stable_across_processes(self) -> None:
        first = driver.FakeCaseRunner()
        second = driver.FakeCaseRunner()
        ids = [f"case-{index}" for index in range(40)]
        self.assertEqual(
            [first._deliberately_wrong(name) for name in ids],
            [second._deliberately_wrong(name) for name in ids],
        )
        self.assertTrue(any(first._deliberately_wrong(name) for name in ids))

    def test_the_direct_arm_answers_from_the_supplied_context(self) -> None:
        runner = driver.FakeCaseRunner(wrong_every=0)
        outcome = runner.ask_direct(driver.Case("c3", "?", "gamma-1", "vt"), "gamma-1 is here")
        self.assertEqual(outcome.reply, "gamma-1")
        self.assertGreater(outcome.prompt_tokens, 0)

    def test_the_fake_direct_arm_also_reports_a_prompt_it_could_not_deliver(self) -> None:
        runner = driver.FakeCaseRunner(wrong_every=0, context_length=8)
        outcome = runner.ask_direct(driver.Case("c", "?", "g", "k"), "x" * 5000)
        self.assertEqual(outcome.status, driver.CONTEXT_EXCEEDED)
        self.assertLess(outcome.delivered_fraction, 1.0)

    def test_the_fake_runner_answers_a_multi_value_case_with_every_value(self) -> None:
        case = driver.Case(
            "c", "?", "111111", "niah_multivalue", metadata={"values": ("111111", "222222")}
        )
        runner = driver.FakeCaseRunner(wrong_every=0)
        outcome = runner.ask_direct(case, "the record holds 111111 and 222222 today")
        self.assertIn("111111", outcome.reply)
        self.assertIn("222222", outcome.reply)


    def test_close_releases_the_ingested_material(self) -> None:
        runner = driver.FakeCaseRunner()
        runner.ingest(self._instance())
        runner.close()
        self.assertEqual(runner._ingested, [])


class JarvisRunnerTests(_TempCase):
    def _instance(self, name: str = "i1") -> driver.Instance:
        return driver.Instance(
            instance_id=name,
            sessions=(
                driver.Session("s1", "", (driver.Turn("user", "one"), driver.Turn("assistant", "two"))),
                driver.Session("s2", "", (driver.Turn("user", "three"),)),
            ),
            cases=(driver.Case("c1", "what?", "two", "extraction"),),
        )

    def test_each_session_becomes_a_conversation_and_each_turn_a_message(self) -> None:
        backend = _StubBackend()
        runner = driver.JarvisCaseRunner(backend=backend, store_root=self.tmp, model="m")
        self.addCleanup(runner.close)
        runner.ingest(self._instance())
        memory, _agent = backend.last
        self.assertEqual(len(memory.conversations), 2)
        self.assertEqual([row[1] for row in memory.messages], ["user", "assistant", "user"])

    def test_no_governed_command_is_ever_synthesised(self) -> None:
        backend = _StubBackend()
        runner = driver.JarvisCaseRunner(backend=backend, store_root=self.tmp, model="m")
        self.addCleanup(runner.close)
        runner.ingest(self._instance())
        memory, _agent = backend.last
        for _cid, _role, content in memory.messages:
            self.assertNotIn("Remember this project fact:", content)

    def test_the_question_runs_in_a_brand_new_conversation(self) -> None:
        backend = _StubBackend()
        runner = driver.JarvisCaseRunner(backend=backend, store_root=self.tmp, model="m")
        self.addCleanup(runner.close)
        runner.ingest(self._instance())
        runner.ask(driver.Case("c1", "what?", "two", "extraction"))
        _memory, agent = backend.last
        self.assertEqual(agent.conversation_ids, [None])

    def test_token_counts_come_from_the_runtimes_own_metrics_table(self) -> None:
        backend = _StubBackend()
        runner = driver.JarvisCaseRunner(backend=backend, store_root=self.tmp, model="m")
        self.addCleanup(runner.close)
        runner.ingest(self._instance())
        outcome = runner.ask(driver.Case("c1", "what?", "two", "extraction"))
        self.assertEqual(outcome.prompt_tokens, 120)
        self.assertEqual(outcome.completion_tokens, 8)
        self.assertEqual(outcome.tool_calls, 1)
        self.assertEqual(outcome.model, SONNET)

    def test_a_second_instance_gets_a_fresh_store(self) -> None:
        backend = _StubBackend()
        runner = driver.JarvisCaseRunner(backend=backend, store_root=self.tmp, model="m")
        self.addCleanup(runner.close)
        runner.ingest(self._instance("i1"))
        first = backend.last[0]
        runner.ingest(self._instance("i2"))
        self.assertTrue(first.closed)
        self.assertEqual(len(backend.opened), 2)
        self.assertNotEqual(backend.opened[0], backend.opened[1])

    def test_asking_before_ingesting_refuses(self) -> None:
        runner = driver.JarvisCaseRunner(backend=_StubBackend(), store_root=self.tmp)
        self.addCleanup(runner.close)
        with self.assertRaises(driver.DriverError) as caught:
            runner.ask(driver.Case("c1", "?", "", "k"))
        self.assertEqual(caught.exception.code, "no_store")

    def test_one_failing_case_never_kills_the_run(self) -> None:
        backend = _StubBackend(explode=True)
        runner = driver.JarvisCaseRunner(backend=backend, store_root=self.tmp)
        self.addCleanup(runner.close)
        runner.ingest(self._instance())
        outcome = runner.ask(driver.Case("c1", "?", "", "k"))
        self.assertEqual(outcome.status, "error")
        self.assertEqual(outcome.error_code, "TimeoutError")
        self.assertEqual(outcome.reply, "")

    def test_compaction_is_run_when_the_tree_supports_it(self) -> None:
        backend = _StubBackend(compaction=True)
        runner = driver.JarvisCaseRunner(
            backend=backend, store_root=self.tmp, compaction_enabled=True
        )
        self.addCleanup(runner.close)
        runner.ingest(self._instance())
        self.assertEqual(backend.last[0].compacted, [1, 2])
        self.assertEqual(runner.compaction_ran, 2)

    def test_compaction_refuses_loudly_on_a_tree_without_it(self) -> None:
        runner = driver.JarvisCaseRunner(
            backend=_StubBackend(compaction=False), store_root=self.tmp, compaction_enabled=True
        )
        self.addCleanup(runner.close)
        with self.assertRaises(driver.DriverError) as caught:
            runner.ingest(self._instance())
        self.assertEqual(caught.exception.code, "compaction_unavailable")

    def test_the_runtime_config_is_applied_to_the_backend_that_runs(self) -> None:
        backend = _StubBackend()
        backend.context_length = 8192
        backend.embeddings = "openai"
        runner = driver.JarvisCaseRunner(
            backend=backend, store_root=self.tmp, context_length=8192, embeddings="openai"
        )
        self.addCleanup(runner.close)
        runner.ingest(self._instance())
        self.assertEqual(backend.last[1].config.context_length, 8192)
        self.assertEqual(backend.last[1].config.memory_embeddings, "openai")

    def test_two_runtime_configs_produce_different_backend_settings(self) -> None:
        # H-5: the runtime block is hashed and published, so a sweep over
        # JARVIS_CONTEXT_LENGTH must not run five identical configurations and
        # publish five different hashes describing them.
        small = driver.make_runner("jarvis", context_length=8192, embeddings="disabled")
        large = driver.make_runner("jarvis", context_length=65536, embeddings="openai")
        self.addCleanup(small.close)
        self.addCleanup(large.close)
        self.assertEqual(small._backend.context_length, 8192)
        self.assertEqual(large._backend.context_length, 65536)
        self.assertEqual(small._backend.embeddings, "disabled")
        self.assertEqual(large._backend.embeddings, "openai")
        self.assertNotEqual(
            small.direct_prompt_limit_chars, large.direct_prompt_limit_chars
        )

    def test_a_backend_that_ignores_the_published_runtime_is_refused(self) -> None:
        runner = driver.JarvisCaseRunner(
            backend=_StubBackend(applies_runtime=False),
            store_root=self.tmp,
            context_length=8192,
        )
        self.addCleanup(runner.close)
        with self.assertRaises(driver.DriverError) as caught:
            runner.ingest(self._instance())
        self.assertEqual(caught.exception.code, "runtime_config_not_applied")

    def test_an_embeddings_mismatch_is_refused_too(self) -> None:
        runner = driver.JarvisCaseRunner(
            backend=_StubBackend(applies_runtime=False),
            store_root=self.tmp,
            embeddings="openai",
        )
        self.addCleanup(runner.close)
        with self.assertRaises(driver.DriverError) as caught:
            runner.ingest(self._instance())
        self.assertEqual(caught.exception.code, "runtime_config_not_applied")

    def test_the_observed_model_comes_from_the_metrics_table(self) -> None:
        # M-5: AgentResult.model is absent on the incomplete path, and falling
        # back to the configured hint let check_models pass on the strength of
        # the config agreeing with itself.
        backend = _StubBackend(served_model="claude-cli:claude-sonnet-4-5")
        runner = driver.JarvisCaseRunner(
            backend=backend, store_root=self.tmp, model="claude-cli:claude-opus-9"
        )
        self.addCleanup(runner.close)
        runner.ingest(self._instance())
        outcome = runner.ask(driver.Case("c1", "?", "", "k"))
        self.assertEqual(outcome.model, "claude-cli:claude-sonnet-4-5")

    def test_a_turn_with_no_provider_call_attests_nothing(self) -> None:
        backend = _StubBackend(record_call=False)
        runner = driver.JarvisCaseRunner(backend=backend, store_root=self.tmp, model=SONNET)
        self.addCleanup(runner.close)
        runner.ingest(self._instance())
        outcome = runner.ask(driver.Case("c1", "?", "", "k"))
        self.assertEqual(outcome.model, driver.UNRECORDED_MODEL)
        with self.assertRaises(report.ReportError) as caught:
            report.check_models([outcome.model], ("claude-cli:claude-sonnet",))
        self.assertEqual(caught.exception.code, "model_not_allowed")

    def test_a_disagreement_between_metrics_and_the_result_publishes_both(self) -> None:
        backend = _StubBackend(served_model="claude-cli:claude-sonnet-4-5")
        runner = driver.JarvisCaseRunner(
            backend=backend, store_root=self.tmp, model="claude-cli:claude-opus-9"
        )
        self.addCleanup(runner.close)
        runner.ingest(self._instance())
        outcome = runner.ask(driver.Case("c1", "?", "", "k"))
        self.assertEqual(outcome.model, "claude-cli:claude-sonnet-4-5")
        self.assertEqual(outcome.model_reported, "claude-cli:claude-opus-9")
        instance = driver.Instance("i", (), (driver.Case("c1", "q", "g", "k"),))
        row = longmemeval.score_row(instance, instance.cases[0], outcome)
        self.assertEqual(row["model_reported"], "claude-cli:claude-opus-9")

    def test_several_observed_models_are_all_named(self) -> None:
        backend = _StubBackend()
        runner = driver.JarvisCaseRunner(backend=backend, store_root=self.tmp, model=SONNET)
        self.addCleanup(runner.close)
        runner.ingest(self._instance())
        memory, agent = backend.last
        original = agent.run

        def _run(prompt, conversation_id=None):
            # A turn that routed to two providers must name both, not pick one.
            memory.record_call(1, 1, "ollama:qwen3.5")
            return original(prompt, conversation_id)

        agent.run = _run
        outcome = runner.ask(driver.Case("c1", "?", "", "k"))
        self.assertIn("+", outcome.model)
        self.assertIn("ollama:qwen3.5", outcome.model)
        self.assertIn(SONNET, outcome.model)

    def test_an_unowned_root_survives_close_and_an_owned_one_does_not(self) -> None:
        owned = driver.JarvisCaseRunner(backend=_StubBackend())
        root = owned._store_root
        self.assertTrue(root.exists())
        owned.close()
        self.assertFalse(root.exists())

    def test_the_real_backend_records_its_runtime_overrides(self) -> None:
        backend = driver.RealJarvisBackend(context_length=32768, embeddings="disabled")
        self.assertEqual(backend.context_length, 32768)
        self.assertEqual(backend.embeddings, "disabled")

    def test_the_direct_arm_frames_the_haystack_as_reference_text(self) -> None:
        backend = _StubBackend()
        provider = _StubDirectProvider(reply="two")
        runner = driver.JarvisCaseRunner(
            backend=backend, store_root=self.tmp, direct_provider=provider
        )
        self.addCleanup(runner.close)
        runner.ingest(self._instance())
        outcome = runner.ask_direct(driver.Case("c1", "what?", "two", "k"), "HAYSTACK")
        self.assertIn("HAYSTACK", provider.prompts[-1])
        self.assertIn("Question: what?", provider.prompts[-1])
        self.assertEqual(outcome.delivered_fraction, 1.0)
        self.assertEqual(outcome.model, SONNET)

    def test_the_direct_control_never_passes_through_the_agent(self) -> None:
        # H-4: Agent.run applies _compact_messages, whose _clip keeps head
        # 2/3 and tail 1/3 and deletes the middle -- which removed the
        # depth-0.5 needle at the top of the default grid and manufactured
        # the very curve the benchmark exists to observe.
        backend = _StubBackend()
        provider = _StubDirectProvider(reply="ok")
        runner = driver.JarvisCaseRunner(
            backend=backend, store_root=self.tmp, direct_provider=provider
        )
        self.addCleanup(runner.close)
        runner.ingest(self._instance())
        before = list(backend.last[1].prompts)
        runner.ask_direct(driver.Case("c1", "what?", "two", "k"), "HAYSTACK")
        self.assertEqual(backend.last[1].prompts, before)
        self.assertEqual(len(provider.prompts), 1)

    def test_an_oversized_direct_prompt_is_missing_evidence_not_a_failure(self) -> None:
        provider = _StubDirectProvider(reply="ok")
        backend = _StubBackend()
        backend.context_length = 64
        runner = driver.JarvisCaseRunner(
            backend=backend,
            store_root=self.tmp,
            context_length=64,
            direct_provider=provider,
        )
        self.addCleanup(runner.close)
        runner.ingest(self._instance())
        outcome = runner.ask_direct(driver.Case("c1", "what?", "two", "k"), "x" * 5000)
        self.assertEqual(outcome.status, driver.CONTEXT_EXCEEDED)
        self.assertEqual(provider.prompts, [])
        self.assertLess(outcome.delivered_fraction, 1.0)
        self.assertGreater(outcome.prompt_chars, runner.direct_prompt_limit_chars)

    def test_a_direct_provider_failure_never_kills_the_run(self) -> None:
        runner = driver.JarvisCaseRunner(
            backend=_StubBackend(),
            store_root=self.tmp,
            direct_provider=_StubDirectProvider(explode=True),
        )
        self.addCleanup(runner.close)
        runner.ingest(self._instance())
        outcome = runner.ask_direct(driver.Case("c1", "what?", "two", "k"), "HAY")
        self.assertEqual(outcome.status, "error")
        self.assertEqual(outcome.error_code, "ConnectionError")


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------


class RowValidationTests(unittest.TestCase):
    def test_an_unknown_key_is_refused(self) -> None:
        with self.assertRaises(report.ReportError) as caught:
            report.validate_row({"question": "text"})
        self.assertEqual(caught.exception.code, "row_key_not_allowed")

    def test_a_long_string_is_refused_as_case_text(self) -> None:
        with self.assertRaises(report.ReportError) as caught:
            report.validate_row({"case_id": "x" * 65})
        self.assertEqual(caught.exception.code, "row_carries_case_text")

    def test_a_container_value_is_refused(self) -> None:
        with self.assertRaises(report.ReportError) as caught:
            report.validate_row({"case_id": "a", "model": "m", "det": True, "type": "t",
                                 "status": "complete", "benchmark": "b", "arm": "jarvis",
                                 "instance_id": "i", "judge": "CORRECT", "abstained": False,
                                 "gold_abstention": False, "latency_ms": 1, "prompt_tokens": 1,
                                 "completion_tokens": 1, "tool_calls": 0, "error_code": None,
                                 "em": 1.0, "f1": 1.0, "category": "1", "task": "t",
                                 "length": 1, "depth": 0.0, "qa_index": 0, "sample_id": ["x"]})
        self.assertEqual(caught.exception.code, "row_not_scalar")

    def test_every_row_key_a_runner_emits_is_in_the_closed_set(self) -> None:
        outcome = driver.Outcome("c", "reply", "m", "complete", 0, 1, 2, 3)
        instance = driver.Instance("i", (), (driver.Case("c", "q", "g", "k"),))
        report.validate_row(longmemeval.score_row(instance, instance.cases[0], outcome, judge_verdict="CORRECT"))
        locomo_case = driver.Case("c", "q", "g", "1", metadata={"qa_index": 0, "sample_id": "i"})
        report.validate_row(locomo.score_row(instance, locomo_case, outcome, judge_verdict="CORRECT"))
        sample = ruler_style.generate_sample(task="vt", length=256, depth=0.5, seed=1)
        report.validate_row(ruler_style.score_row(sample, outcome, arm="jarvis"))


class JsonlTests(_TempCase):
    def test_rows_round_trip_and_a_torn_line_is_dropped(self) -> None:
        path = self.tmp / "cases.jsonl"
        report.append_case(path, {"case_id": "a", "det": True})
        report.append_case(path, {"case_id": "b", "det": False})
        with open(path, "a", encoding="utf-8") as handle:
            handle.write("\n")
            handle.write('{"case_id": "c", "det"')
        rows = report.read_cases(path)
        self.assertEqual([row["case_id"] for row in rows], ["a", "b"])
        self.assertEqual(report.completed_case_ids(path), {"a", "b"})

    def test_reading_an_absent_file_is_empty_not_an_error(self) -> None:
        self.assertEqual(report.read_cases(self.tmp / "absent.jsonl"), [])

    def test_appending_validates_before_writing(self) -> None:
        path = self.tmp / "cases.jsonl"
        with self.assertRaises(report.ReportError):
            report.append_case(path, {"question": "leaked"})
        self.assertFalse(path.exists())


class ConfigHashTests(unittest.TestCase):
    def test_the_hash_ignores_key_order(self) -> None:
        self.assertEqual(
            report.config_sha256({"a": 1, "b": {"c": 2, "d": 3}}),
            report.config_sha256({"b": {"d": 3, "c": 2}, "a": 1}),
        )

    def test_the_hash_changes_when_a_value_changes(self) -> None:
        self.assertNotEqual(report.config_sha256({"a": 1}), report.config_sha256({"a": 2}))

    def test_git_state_falls_back_when_the_directory_is_not_a_repository(self) -> None:
        with tempfile.TemporaryDirectory() as outside:
            commit, dirty = report.git_state(Path(outside))
        self.assertEqual(commit, "unknown")
        self.assertTrue(dirty)


class AggregateTests(unittest.TestCase):
    ROWS = [
        {"type": "a", "det": True, "judge": "CORRECT", "latency_ms": 100, "prompt_tokens": 10,
         "completion_tokens": 2, "model": "claude-cli:claude-sonnet-4-5", "status": "complete"},
        {"type": "a", "det": False, "judge": "INCORRECT", "latency_ms": 300, "prompt_tokens": 30,
         "completion_tokens": 4, "model": "claude-cli:claude-sonnet-4-5", "status": "complete"},
        {"type": "b", "det": True, "gold_abstention": True, "latency_ms": 200, "em": 1.0, "f1": 1.0,
         "model": "claude-cli:claude-sonnet-4-5", "status": "error"},
    ]

    def test_overall_and_per_group_cells(self) -> None:
        summary = report.aggregate(self.ROWS)
        self.assertEqual(summary["overall"]["n"], 3)
        self.assertAlmostEqual(summary["overall"]["deterministic"], 0.6667, places=3)
        self.assertEqual(summary["by_group"]["a"]["judge"], 0.5)
        self.assertEqual(summary["by_group"]["b"]["em"], 1.0)

    def test_abstention_latency_tokens_errors_and_models(self) -> None:
        summary = report.aggregate(self.ROWS)
        self.assertEqual(
            summary["abstention"],
            {"n": 1, "accuracy": 1.0, "asserted_while_declining": 0},
        )
        self.assertEqual(summary["latency_ms"]["p50"], 200.0)
        self.assertEqual(summary["tokens_per_answer"]["prompt_p50"], 10.0)
        self.assertEqual(summary["errors"], 1)
        self.assertEqual(summary["models_seen"], ["claude-cli:claude-sonnet-4-5"])

    def test_a_row_without_the_group_key_is_left_out_of_the_breakdown(self) -> None:
        summary = report.aggregate([*self.ROWS, {"det": True, "model": "m", "status": "complete"}])
        self.assertEqual(sorted(summary["by_group"]), ["a", "b"])
        self.assertEqual(summary["overall"]["n"], 4)

    def test_an_empty_run_aggregates_to_nulls_rather_than_zeroes(self) -> None:
        summary = report.aggregate([])
        self.assertIsNone(summary["overall"]["deterministic"])
        self.assertIsNone(summary["latency_ms"]["p50"])


class WriteReportTests(_TempCase):
    def _config(self, prefixes=("claude-cli:claude-sonnet",)) -> dict:
        return {
            "benchmark": "probe",
            "dataset": {"sha256": "a" * 64, "licence_sha256": "b" * 64},
            "model": {"answer": "claude-cli:claude-sonnet-4-5", "allowed_model_prefixes": list(prefixes)},
        }

    def _rows(self, model: str = "claude-cli:claude-sonnet-4-5") -> list[dict]:
        return [
            {"case_id": "c1", "type": "a", "det": True, "latency_ms": 5, "model": model,
             "status": "complete", "prompt_tokens": 9, "judge": "CORRECT"},
        ]

    def _build(self, *, tier: str = "subset", model: str = "claude-cli:claude-sonnet-4-5",
               prefixes=("claude-cli:claude-sonnet",), rows=None) -> dict:
        return report.build_report(
            benchmark="probe",
            config=self._config(prefixes),
            rows=self._rows(model) if rows is None else rows,
            tier=tier,
            root=cache.repository_root(),
            started_utc=report.utc_now(),
            limitations=["subset, not the full set"],
        )

    def test_a_report_is_written_with_its_provenance(self) -> None:
        built = self._build()
        target = self.tmp / report.report_filename("probe", built["finished_utc"][:10], built["config_sha256"])
        written = report.write_report(target, built)
        payload = json.loads(written.read_text(encoding="utf-8"))
        self.assertEqual(payload["schema"], report.REPORT_SCHEMA)
        self.assertEqual(payload["n"], 1)
        self.assertEqual(len(payload["config_sha256"]), 64)
        self.assertTrue(payload["commit"])
        self.assertIn("subset, not the full set", payload["limitations"])

    def test_an_unknown_tier_is_refused(self) -> None:
        with self.assertRaises(report.ReportError) as caught:
            self._build(tier="production")
        self.assertEqual(caught.exception.code, "unknown_tier")

    def test_a_smoke_run_can_never_become_a_report(self) -> None:
        built = self._build(tier="smoke")
        with self.assertRaises(report.ReportError) as caught:
            report.write_report(self.tmp / "smoke.json", built)
        self.assertEqual(caught.exception.code, "smoke_is_not_a_report")

    def test_a_model_outside_the_allowed_prefixes_is_refused(self) -> None:
        built = self._build(model=driver.FAKE_MODEL)
        with self.assertRaises(report.ReportError) as caught:
            report.write_report(self.tmp / "fake.json", built)
        self.assertEqual(caught.exception.code, "model_not_allowed")

    def test_an_empty_allowed_prefix_list_is_refused(self) -> None:
        with self.assertRaises(report.ReportError) as caught:
            report.check_models(["anything"], ())
        self.assertEqual(caught.exception.code, "allowed_prefixes_missing")

    def test_a_report_with_no_cases_is_refused(self) -> None:
        built = self._build(rows=[])
        with self.assertRaises(report.ReportError) as caught:
            report.write_report(self.tmp / "empty.json", built)
        self.assertEqual(caught.exception.code, "no_cases")

    def test_a_report_is_never_overwritten(self) -> None:
        built = self._build()
        target = self.tmp / "once.json"
        report.write_report(target, built)
        with self.assertRaises(report.ReportError) as caught:
            report.write_report(target, built)
        self.assertEqual(caught.exception.code, "report_exists")


class ReportGuardTests(_TempCase):
    """M-3, M-4, L-2 and L-3: the guards that were weaker than their rules."""

    def test_a_report_with_no_model_evidence_is_refused(self) -> None:
        with self.assertRaises(report.ReportError) as caught:
            report.check_models([], ("claude-cli:claude-sonnet",))
        self.assertEqual(caught.exception.code, "models_unrecorded")
        with self.assertRaises(report.ReportError):
            report.check_models(["", "   "], ("claude-cli:claude-sonnet",))

    def test_a_row_that_attests_nothing_is_refused(self) -> None:
        with self.assertRaises(report.ReportError) as caught:
            report.validate_row({"case_id": "c1", "det": True}, require_model=True)
        self.assertEqual(caught.exception.code, "row_attests_nothing")
        report.validate_row({"case_id": "c1", "model": SONNET}, require_model=True)

    def test_build_report_requires_model_evidence_on_every_row(self) -> None:
        with self.assertRaises(report.ReportError) as caught:
            report.build_report(
                benchmark="probe",
                config={"model": {"allowed_model_prefixes": ["claude-cli:claude-sonnet"]}},
                rows=[{"case_id": "c1", "det": True, "latency_ms": 5}],
                tier="subset",
                root=cache.repository_root(),
                started_utc=report.utc_now(),
            )
        self.assertEqual(caught.exception.code, "row_attests_nothing")

    def test_a_duplicated_case_is_refused_with_its_line_number(self) -> None:
        path = self.tmp / "cases.jsonl"
        report.append_case(path, {"case_id": "a", "det": True, "model": SONNET})
        report.append_case(path, {"case_id": "b", "det": False, "model": SONNET})
        report.append_case(path, {"case_id": "a", "det": False, "model": SONNET})
        self.assertEqual(len(report.read_cases(path)), 3)
        with self.assertRaises(report.ReportError) as caught:
            report.read_cases(path, allow_duplicates=False)
        self.assertEqual(caught.exception.code, "duplicate_case_id")
        self.assertIn("line 1", str(caught.exception))
        self.assertIn("line 3", str(caught.exception))

    def test_build_report_refuses_duplicated_rows(self) -> None:
        rows = [
            {"case_id": "a", "det": True, "model": SONNET},
            {"case_id": "a", "det": False, "model": SONNET},
        ]
        with self.assertRaises(report.ReportError) as caught:
            report.build_report(
                benchmark="probe",
                config={"model": {"allowed_model_prefixes": ["claude-cli:claude-sonnet"]}},
                rows=rows,
                tier="subset",
                root=cache.repository_root(),
                started_utc=report.utc_now(),
            )
        self.assertEqual(caught.exception.code, "duplicate_case_id")

    def test_a_limitation_is_bounded_and_screened(self) -> None:
        self.assertEqual(report.validate_limitations(["  subset  ", ""]), ["subset"])
        with self.assertRaises(report.ReportError) as caught:
            report.validate_limitations(["x" * (report.MAX_LIMITATION_CHARS + 1)])
        self.assertEqual(caught.exception.code, "limitation_too_long")
        with self.assertRaises(report.ReportError) as second:
            report.validate_limitations(["best-in-class recall"])
        self.assertEqual(second.exception.code, "limitation_banned_claim")
        with self.assertRaises(report.ReportError) as third:
            report.validate_limitations([f"note {index}" for index in range(30)])
        self.assertEqual(third.exception.code, "too_many_limitations")

    def test_the_report_records_the_exact_command(self) -> None:
        built = report.build_report(
            benchmark="probe",
            config={"model": {"allowed_model_prefixes": ["claude-cli:claude-sonnet"]}},
            rows=[{"case_id": "a", "det": True, "model": SONNET}],
            tier="subset",
            root=cache.repository_root(),
            started_utc=report.utc_now(),
            command="python scripts/benchmarks/run.py run probe --n 3",
        )
        self.assertEqual(built["command"], "python scripts/benchmarks/run.py run probe --n 3")
        self.assertNotIn("command", built["config"])
        self.assertIn("`python scripts/benchmarks/run.py run probe --n 3`",
                      report.render_markdown(built))

    def test_the_command_defaults_to_this_processs_argv(self) -> None:
        built = report.build_report(
            benchmark="probe",
            config={"model": {"allowed_model_prefixes": ["claude-cli:claude-sonnet"]}},
            rows=[{"case_id": "a", "det": True, "model": SONNET}],
            tier="subset",
            root=cache.repository_root(),
            started_utc=report.utc_now(),
        )
        self.assertTrue(built["command"])

    def test_judge_reliability_and_delivery_are_reported(self) -> None:
        rows = [
            {"case_id": "a", "det": True, "model": SONNET, "judge": "CORRECT",
             "delivered_fraction": 1.0, "status": "complete"},
            {"case_id": "b", "det": None, "model": SONNET, "judge": "UNPARSED",
             "delivered_fraction": 0.3, "status": "context_exceeded"},
        ]
        summary = report.aggregate(rows)
        self.assertEqual(summary["judge_reliability"]["unparsed"], 1)
        self.assertEqual(summary["judge_reliability"]["unparsed_rate"], 0.5)
        self.assertEqual(summary["delivery"]["not_delivered"], 1)
        self.assertEqual(summary["delivery"]["delivered_fraction_min"], 0.3)
        # A cell that was never delivered is missing evidence, not a failure.
        self.assertEqual(summary["overall"]["deterministic"], 1.0)

    def test_the_markdown_names_the_unparsed_and_undelivered_counts(self) -> None:
        built = report.build_report(
            benchmark="probe",
            config={"model": {"allowed_model_prefixes": ["claude-cli:claude-sonnet"]}},
            rows=[
                {"case_id": "a", "det": True, "model": SONNET, "judge": "UNPARSED",
                 "status": "complete"},
                {"case_id": "b", "det": None, "model": SONNET, "status": "context_exceeded",
                 "delivered_fraction": 0.2},
            ],
            tier="subset",
            root=cache.repository_root(),
            started_utc=report.utc_now(),
        )
        rendered = report.render_markdown(built)
        self.assertIn("judge unparsed", rendered)
        self.assertIn("not delivered", rendered)


class MarkdownTests(unittest.TestCase):
    def _report(self, tier: str = "subset") -> dict:
        return {
            "schema": report.REPORT_SCHEMA,
            "benchmark": "probe",
            "finished_utc": "2026-09-04T10:00:00Z",
            "n": 3,
            "tier": tier,
            "commit": "abcdef1234567890",
            "config_sha256": "0123456789abcdef",
            "config": {"dataset": {"sha256": "d" * 64, "licence_sha256": "e" * 64},
                       "model": {"answer": "claude-cli:claude-sonnet-4-5"}},
            "aggregate": {
                "overall": {"deterministic": 0.5, "judge": None},
                "by_group": {"a": {"n": 3, "deterministic": 0.5, "judge": None, "em": None, "f1": None}},
                "group_key": "type",
                "abstention": {"n": 1, "accuracy": 1.0},
                "latency_ms": {"p50": 120.0},
                "tokens_per_answer": {"prompt_p50": 900.0},
            },
            "limitations": ["subset"],
        }

    def test_the_row_carries_the_provenance_columns(self) -> None:
        rendered = report.render_markdown(self._report())
        self.assertIn("2026-09-04", rendered)
        self.assertIn("claude-cli:claude-sonnet-4-5", rendered)
        self.assertIn("abcdef123456", rendered)
        self.assertIn("0123456789ab", rendered)
        self.assertIn("limitation: subset", rendered)
        self.assertIn("per-type breakdown", rendered)
        self.assertIn("n/a", rendered)

    def test_an_integer_cell_renders_without_decimal_formatting(self) -> None:
        payload = self._report()
        payload["aggregate"]["overall"]["deterministic"] = 1
        self.assertIn("| 1 |", report.render_markdown(payload))

    def test_a_smoke_run_refuses_to_render(self) -> None:
        with self.assertRaises(report.ReportError) as caught:
            report.render_markdown(self._report(tier="smoke"))
        self.assertEqual(caught.exception.code, "smoke_is_not_a_report")

    def test_the_superlative_screen_names_every_banned_claim(self) -> None:
        self.assertEqual(report.banned_claim_findings("clean prose"), [])
        self.assertIn("best-in-class", report.banned_claim_findings("Best-In-Class memory"))


# ---------------------------------------------------------------------------
# LongMemEval
# ---------------------------------------------------------------------------


class LongMemEvalTests(_TempCase):
    def test_the_published_format_maps_onto_the_driver_shapes(self) -> None:
        instance = longmemeval.to_instance(longmemeval_records(1)[0])
        self.assertIsNotNone(instance)
        self.assertEqual(len(instance.sessions), 2)
        self.assertEqual(instance.sessions[0].date, "2026-08-01")
        self.assertEqual(instance.cases[0].kind, "information-extraction")
        # A blank turn is dropped; a non-user/assistant role folds onto the user
        # side rather than being lost.
        roles = [turn.role for turn in instance.sessions[1].turns]
        self.assertEqual(roles, ["user", "user", "assistant"])

    def test_an_abstention_id_is_marked_as_one(self) -> None:
        records = longmemeval_records(3)
        instance = longmemeval.to_instance(records[-1])
        self.assertTrue(instance.cases[0].gold_abstention)
        self.assertTrue(instance.instance_id.endswith("_abs"))

    def test_a_record_without_a_question_is_skipped(self) -> None:
        self.assertIsNone(longmemeval.to_instance({"question_id": "x"}))

    def test_loading_streams_the_file_and_respects_a_limit(self) -> None:
        path = write_json(self.tmp / "lme.json", longmemeval_records(5))
        self.assertEqual(len(longmemeval.load(path)), 5)
        self.assertEqual(len(longmemeval.load(path, limit=2)), 2)

    def test_a_file_of_unusable_records_is_refused(self) -> None:
        path = write_json(self.tmp / "bad.json", [{"nothing": 1}])
        with self.assertRaises(cache.DatasetError) as caught:
            longmemeval.load(path)
        self.assertEqual(caught.exception.code, "dataset_empty")

    def test_a_non_object_element_is_refused(self) -> None:
        path = write_json(self.tmp / "list.json", [[1, 2]])
        with self.assertRaises(cache.DatasetError) as caught:
            longmemeval.load(path)
        self.assertEqual(caught.exception.code, "dataset_malformed")

    def test_malformed_haystack_material_is_tolerated_not_crashed_on(self) -> None:
        instance = longmemeval.to_instance(
            {
                "question_id": "q1",
                "question_type": "information-extraction",
                "question": "what?",
                "answer": "a",
                "haystack_sessions": ["not a session", [], [42, {"role": "user", "content": "keep"}]],
            }
        )
        self.assertEqual(len(instance.sessions), 1)
        self.assertEqual(instance.sessions[0].turns[0].content, "keep")

    def test_the_stratified_sample_is_reproducible_and_covers_the_abilities(self) -> None:
        instances = [longmemeval.to_instance(record) for record in longmemeval_records(9)]
        first = longmemeval.stratified_sample(instances, n=4, seed=7)
        second = longmemeval.stratified_sample(instances, n=4, seed=7)
        self.assertEqual([i.instance_id for i in first], [i.instance_id for i in second])
        self.assertEqual(len({i.metadata["question_type"] for i in first}), 4)

    def test_the_sample_degenerates_safely(self) -> None:
        instances = [longmemeval.to_instance(record) for record in longmemeval_records(3)]
        self.assertEqual(len(longmemeval.stratified_sample(instances, n=None, seed=1)), 3)
        self.assertEqual(len(longmemeval.stratified_sample(instances, n=99, seed=1)), 3)
        self.assertEqual(longmemeval.stratified_sample(instances, n=0, seed=1), [])

    def test_the_row_names_the_type_and_never_the_question(self) -> None:
        instance = longmemeval.to_instance(longmemeval_records(1)[0])
        case = instance.cases[0]
        outcome = driver.Outcome(case.case_id, "It is marker-0.", "claude-cli:claude-sonnet-4-5",
                                 "complete", 0, 12, 100, 5)
        row = longmemeval.score_row(instance, case, outcome, judge_verdict="CORRECT")
        self.assertTrue(row["det"])
        self.assertEqual(row["type"], "information-extraction")
        self.assertNotIn(case.question, json.dumps(row))

    def test_an_errored_outcome_carries_its_code(self) -> None:
        instance = longmemeval.to_instance(longmemeval_records(1)[0])
        outcome = driver.Outcome("probe0", "", "m", "error", 0, 1, None, None, error_code="TimeoutError")
        row = longmemeval.score_row(instance, instance.cases[0], outcome)
        self.assertEqual(row["error_code"], "TimeoutError")
        self.assertNotIn("judge", row)

    def test_degradation_is_oracle_minus_full(self) -> None:
        main = {"aggregate": {"overall": {"deterministic": 0.60}}}
        control = {"aggregate": {"overall": {"deterministic": 0.68}}}
        self.assertAlmostEqual(longmemeval.degradation(main, control), 0.08)
        self.assertIsNone(longmemeval.degradation(main, {"aggregate": {"overall": {}}}))


# ---------------------------------------------------------------------------
# LoCoMo
# ---------------------------------------------------------------------------


class LoCoMoTests(_TempCase):
    def test_the_published_format_maps_onto_the_driver_shapes(self) -> None:
        instance = locomo.to_instance(locomo_records(1)[0])
        self.assertEqual(len(instance.sessions), 2)
        self.assertEqual(instance.sessions[0].session_id, "conv-0#1")
        self.assertIn("Wren:", instance.sessions[0].turns[0].content)
        self.assertEqual(instance.sessions[0].turns[0].role, "user")
        self.assertEqual(instance.sessions[0].turns[1].role, "assistant")

    def test_the_adversarial_category_is_scored_as_abstention(self) -> None:
        instance = locomo.to_instance(locomo_records(1)[0])
        adversarial = [case for case in instance.cases if case.kind == "5"]
        self.assertTrue(adversarial)
        self.assertTrue(all(case.gold_abstention for case in adversarial))

    def test_a_malformed_sample_is_skipped(self) -> None:
        self.assertIsNone(locomo.to_instance({"sample_id": "x"}))
        self.assertIsNone(locomo.to_instance({"sample_id": "x", "conversation": {}, "qa": []}))

    def test_loading_and_counting_read_the_file_rather_than_guessing(self) -> None:
        path = write_json(self.tmp / "loco.json", locomo_records(2, 5))
        instances = locomo.load(path)
        self.assertEqual(locomo.question_count(instances), 10)
        self.assertEqual(len(locomo.load(path, limit=1)), 1)

    def test_an_empty_file_is_refused(self) -> None:
        path = write_json(self.tmp / "none.json", [{"sample_id": "x"}])
        with self.assertRaises(cache.DatasetError):
            locomo.load(path)

    def test_a_non_object_element_is_refused(self) -> None:
        path = write_json(self.tmp / "nested.json", ["text"])
        with self.assertRaises(cache.DatasetError) as caught:
            locomo.load(path)
        self.assertEqual(caught.exception.code, "dataset_malformed")

    def test_malformed_locomo_material_is_tolerated_not_crashed_on(self) -> None:
        instance = locomo.to_instance(
            {
                "sample_id": "conv-x",
                "conversation": {
                    "speaker_a": "Wren",
                    "session_1": "not a list",
                    "session_2": [7, {"speaker": "Wren", "text": "  "}, {"speaker": "Alder", "text": "kept"}],
                    "not_a_session": [],
                },
                "qa": [
                    "not an object",
                    {"question": "   ", "answer": "x", "category": 1},
                    {"question": "real?", "answer": "y", "category": "not a number"},
                ],
            }
        )
        self.assertEqual(len(instance.sessions), 1)
        self.assertEqual(len(instance.cases), 1)
        self.assertEqual(instance.cases[0].kind, "0")

    def test_an_errored_locomo_outcome_carries_its_code(self) -> None:
        instance = locomo.to_instance(locomo_records(1)[0])
        outcome = driver.Outcome("c", "", "m", "error", 0, 1, error_code="OSError")
        row = locomo.score_row(instance, instance.cases[0], outcome)
        self.assertEqual(row["error_code"], "OSError")

    def test_case_selection_is_reproducible_and_category_balanced(self) -> None:
        instances = locomo.load(write_json(self.tmp / "l.json", locomo_records(2, 5)))
        first = locomo.stratified_cases(instances, n=5, seed=3)
        second = locomo.stratified_cases(instances, n=5, seed=3)
        self.assertEqual([case.case_id for _i, case in first], [case.case_id for _i, case in second])
        self.assertEqual(len({case.kind for _i, case in first}), 5)
        self.assertEqual(len(locomo.stratified_cases(instances, n=None, seed=1)), 10)
        self.assertEqual(locomo.stratified_cases(instances, n=0, seed=1), [])

    def test_a_row_carries_ids_categories_and_scores_only(self) -> None:
        instance = locomo.to_instance(locomo_records(1)[0])
        case = instance.cases[0]
        outcome = driver.Outcome(case.case_id, "It was 10.", "claude-cli:claude-sonnet-4-5",
                                 "complete", 0, 7, 50, 3)
        row = locomo.score_row(instance, case, outcome, judge_verdict="CORRECT")
        report.validate_row(row)
        serialised = json.dumps(row)
        self.assertNotIn(case.question, serialised)
        self.assertNotIn("Wren", serialised)
        self.assertEqual(row["sample_id"], "conv-0")
        self.assertEqual(row["category"], "1")
        self.assertTrue(row["det"])

    def test_an_adversarial_row_reports_no_f1(self) -> None:
        instance = locomo.to_instance(locomo_records(1)[0])
        case = [item for item in instance.cases if item.kind == "5"][0]
        outcome = driver.Outcome(case.case_id, "That is not recorded.", "m", "complete", 0, 1)
        row = locomo.score_row(instance, case, outcome)
        self.assertIsNone(row["f1"])
        self.assertIsNone(row["em"])
        self.assertTrue(row["det"])

    def test_a_locomo_row_publishes_a_model_disagreement(self) -> None:
        instance = locomo.to_instance(locomo_records(1)[0])
        outcome = driver.Outcome(
            "c", "10", SONNET, "complete", 0, 1, model_reported="claude-cli:claude-opus-9"
        )
        row = locomo.score_row(instance, instance.cases[0], outcome)
        self.assertEqual(row["model_reported"], "claude-cli:claude-opus-9")

    def test_an_adversarial_reply_that_states_the_forbidden_answer_is_wrong(self) -> None:
        instance = locomo.to_instance(locomo_records(1)[0])
        case = [item for item in instance.cases if item.kind == "5"][0]
        outcome = driver.Outcome(
            case.case_id,
            f"That is not recorded, but the usual reply is {case.gold}.",
            SONNET, "complete", 0, 1,
        )
        row = locomo.score_row(instance, case, outcome)
        self.assertFalse(row["det"])
        self.assertTrue(row["asserted"])

    def test_a_case_selection_beyond_one_stratum_stops_cleanly(self) -> None:
        instances = locomo.load(write_json(self.tmp / "one.json", locomo_records(1, 5)))
        picked = locomo.stratified_cases(instances, n=4, seed=1)
        self.assertEqual(len(picked), 4)


    def test_the_licence_attribution_names_the_terms(self) -> None:
        self.assertIn("CC BY-NC 4.0", locomo.LICENCE_ATTRIBUTION)
        self.assertIn("not", locomo.LICENCE_ATTRIBUTION)


# ---------------------------------------------------------------------------
# RULER-style
# ---------------------------------------------------------------------------


class RulerStyleTests(unittest.TestCase):
    def test_generation_is_a_pure_function_of_its_arguments(self) -> None:
        first = ruler_style.generate_sample(task="niah_single", length=512, depth=0.5, seed=11)
        second = ruler_style.generate_sample(task="niah_single", length=512, depth=0.5, seed=11)
        self.assertEqual(first.context, second.context)
        self.assertEqual(first.case.question, second.case.question)
        third = ruler_style.generate_sample(task="niah_single", length=512, depth=0.5, seed=12)
        self.assertNotEqual(first.context, third.context)

    def test_every_task_puts_its_needle_in_the_haystack(self) -> None:
        for task in ruler_style.TASKS:
            with self.subTest(task=task):
                sample = ruler_style.generate_sample(task=task, length=512, depth=0.5, seed=5)
                self.assertTrue(sample.values)
                for value in sample.values:
                    self.assertIn(value, sample.context)

    def test_depth_moves_the_needle_through_the_haystack(self) -> None:
        early = ruler_style.generate_sample(task="niah_single", length=2048, depth=0.0, seed=3)
        late = ruler_style.generate_sample(task="niah_single", length=2048, depth=1.0, seed=3)
        self.assertLess(early.context.index(early.values[0]), late.context.index(late.values[0]))

    def test_the_haystack_grows_with_the_requested_length(self) -> None:
        short = ruler_style.generate_sample(task="cwe", length=512, depth=0.5, seed=2)
        long = ruler_style.generate_sample(task="cwe", length=4096, depth=0.5, seed=2)
        self.assertGreater(len(long.context), len(short.context) * 2)
        self.assertGreater(long.approx_tokens, short.approx_tokens)

    def test_an_unknown_task_and_a_bad_depth_are_refused(self) -> None:
        with self.assertRaises(ruler_style.RulerError) as caught:
            ruler_style.generate_sample(task="niah_quintuple", length=512, depth=0.5, seed=1)
        self.assertEqual(caught.exception.code, "unknown_task")
        with self.assertRaises(ruler_style.RulerError) as second:
            ruler_style._insert(["a"], ["n"], 1.5)
        self.assertEqual(second.exception.code, "bad_depth")

    def test_the_grid_covers_every_cell(self) -> None:
        samples = ruler_style.generate(
            tasks=("vt",), lengths=(512, 1024), depths=(0.0, 1.0), samples_per_cell=2, seed=1
        )
        self.assertEqual(len(samples), 8)
        self.assertEqual(len({sample.case.case_id for sample in samples}), 8)

    def test_a_sample_becomes_dated_sessions_for_the_jarvis_arm(self) -> None:
        sample = ruler_style.generate_sample(task="vt", length=2048, depth=0.5, seed=4)
        instance = ruler_style.as_instance(sample)
        rebuilt = "".join(
            turn.content for session in instance.sessions for turn in session.turns
        )
        self.assertEqual(rebuilt, sample.context)
        self.assertEqual(instance.cases[0], sample.case)

    def test_scoring_covers_the_single_multi_and_chain_shapes(self) -> None:
        single = ruler_style.generate_sample(task="niah_single", length=512, depth=0.5, seed=6)
        multi = ruler_style.generate_sample(task="niah_multivalue", length=512, depth=0.5, seed=6)
        chain = ruler_style.generate_sample(task="vt", length=512, depth=0.5, seed=6)
        good = driver.Outcome("c", " ".join(multi.values), "m", "complete", 0, 1)
        self.assertTrue(ruler_style.score_row(multi, good, arm="jarvis")["det"])
        partial = driver.Outcome("c", multi.values[0], "m", "complete", 0, 1)
        self.assertFalse(ruler_style.score_row(multi, partial, arm="jarvis")["det"])
        self.assertTrue(
            ruler_style.score_row(
                single, driver.Outcome("c", single.values[0], "m", "complete", 0, 1), arm="direct"
            )["det"]
        )
        whole = driver.Outcome("c", " ".join(chain.values), "m", "complete", 0, 1)
        self.assertTrue(ruler_style.score_row(chain, whole, arm="jarvis")["det"])
        # one link short of the chain is not a full-chain match
        short = driver.Outcome("c", " ".join(chain.values[:-1]), "m", "complete", 0, 1)
        self.assertFalse(ruler_style.score_row(chain, short, arm="jarvis")["det"])

    def test_an_errored_ruler_outcome_carries_its_code(self) -> None:
        sample = ruler_style.generate_sample(task="cwe", length=512, depth=0.0, seed=1)
        outcome = driver.Outcome("c", "", "m", "error", 0, 1, error_code="OSError")
        self.assertEqual(ruler_style.score_row(sample, outcome, arm="direct")["error_code"], "OSError")

    def test_the_depth_curve_and_the_arm_delta_are_reported_per_length(self) -> None:
        rows = [
            {"arm": "jarvis", "length": 4096, "depth": 0.0, "det": True},
            {"arm": "jarvis", "length": 4096, "depth": 0.5, "det": False},
            {"arm": "direct", "length": 4096, "depth": 0.0, "det": True},
            {"arm": "direct", "length": 4096, "depth": 0.5, "det": True},
        ]
        curve = ruler_style.depth_curve(rows, arm="jarvis")
        self.assertEqual(curve["4096"]["0.00"], 1.0)
        self.assertEqual(curve["4096"]["0.50"], 0.0)
        self.assertEqual(ruler_style.arm_delta(rows), {"4096": -0.5})

    def test_the_arm_delta_is_none_when_an_arm_is_missing(self) -> None:
        rows = [{"arm": "jarvis", "length": 4096, "depth": 0.0, "det": True}]
        self.assertEqual(ruler_style.arm_delta(rows), {"4096": None})


# ---------------------------------------------------------------------------
# synthetic fallbacks
# ---------------------------------------------------------------------------


    def test_a_not_delivered_cell_scores_none_and_is_excluded_from_the_rate(self) -> None:
        sample = ruler_style.generate_sample(task="niah_single", length=512, depth=0.5, seed=8)
        outcome = driver.Outcome(
            "c", "", "m", driver.CONTEXT_EXCEEDED, 0, 0,
            error_code=driver.CONTEXT_EXCEEDED, delivered_fraction=0.35, prompt_chars=9000,
        )
        row = ruler_style.score_row(sample, outcome, arm="direct")
        report.validate_row(row)
        self.assertIsNone(row["det"])
        self.assertIsNone(row["abstained"])
        self.assertEqual(row["delivered_fraction"], 0.35)
        self.assertEqual(row["prompt_chars"], 9000)
        # Excluded from the rate rather than counted as a failure.
        self.assertIsNone(scoring.rate([row], "det"))

    def test_a_ruler_row_publishes_a_model_disagreement(self) -> None:
        sample = ruler_style.generate_sample(task="cwe", length=512, depth=0.0, seed=9)
        outcome = driver.Outcome(
            "c", "x", SONNET, "complete", 0, 1, model_reported="claude-cli:claude-opus-9"
        )
        row = ruler_style.score_row(sample, outcome, arm="direct")
        self.assertEqual(row["model_reported"], "claude-cli:claude-opus-9")

    def test_the_delivery_report_names_each_arm_and_length(self) -> None:
        rows = [
            {"arm": "direct", "length": 32768, "status": "context_exceeded",
             "delivered_fraction": 0.7},
            {"arm": "direct", "length": 32768, "status": "complete",
             "delivered_fraction": 1.0},
            {"arm": "jarvis", "length": 4096, "status": "complete",
             "delivered_fraction": None},
            {"status": "complete"},
        ]
        delivery = ruler_style.delivery_report(rows)
        self.assertEqual(sorted(delivery), ["direct@32768", "jarvis@4096"])
        self.assertEqual(delivery["direct@32768"]["not_delivered"], 1)
        self.assertEqual(delivery["direct@32768"]["min_fraction"], 0.7)
        self.assertIsNone(delivery["jarvis@4096"]["min_fraction"])

    def test_the_vt_gold_is_the_whole_chain_of_variable_names(self) -> None:
        sample = ruler_style.generate_sample(task="vt", length=1024, depth=0.5, seed=12)
        self.assertEqual(len(sample.values), 5)
        for name in sample.values:
            self.assertIn(name, sample.context)
        self.assertIn("Name every one of them", sample.case.question)


class SyntheticTests(unittest.TestCase):
    def test_the_longmemeval_shape_covers_every_ability_and_is_seeded(self) -> None:
        first = synthetic.longmemeval_shape(n=10, seed=1)
        second = synthetic.longmemeval_shape(n=10, seed=1)
        self.assertEqual([i.instance_id for i in first], [i.instance_id for i in second])
        self.assertEqual({i.metadata["question_type"] for i in first}, set(synthetic.ABILITIES))

    def test_the_shape_marks_its_abstention_cases(self) -> None:
        abstention = [
            instance
            for instance in synthetic.longmemeval_shape(n=5, seed=1)
            if instance.cases[0].gold_abstention
        ]
        self.assertTrue(abstention)
        self.assertTrue(abstention[0].instance_id.endswith("_abs"))

    def test_the_knowledge_update_case_expects_the_later_value(self) -> None:
        updates = [
            instance
            for instance in synthetic.longmemeval_shape(n=5, seed=1)
            if instance.metadata["question_type"] == "knowledge-update"
        ]
        gold = updates[0].cases[0].gold
        transcript = " ".join(
            turn.content for session in updates[0].sessions for turn in session.turns
        )
        self.assertIn(f"is now {gold}", transcript)

    def test_the_locomo_shape_produces_two_speakers_and_five_categories(self) -> None:
        instances = synthetic.locomo_shape(samples=2, questions=10, seed=2)
        self.assertEqual(len(instances), 2)
        categories = {case.kind for instance in instances for case in instance.cases}
        self.assertEqual(categories, {"1", "2", "3", "4", "5"})
        self.assertTrue(
            all(turn.content.startswith(("Wren:", "Alder:"))
                for instance in instances
                for session in instance.sessions
                for turn in session.turns)
        )

    def test_a_zero_sized_shape_is_empty_rather_than_an_error(self) -> None:
        self.assertEqual(synthetic.longmemeval_shape(n=0), [])
        self.assertEqual(synthetic.locomo_shape(samples=0), [])

    def test_the_shape_names_are_never_the_benchmark_names(self) -> None:
        for name in synthetic.shape_names():
            self.assertIn("shape", name)
            self.assertNotIn(name, ("longmemeval_s", "locomo10"))


# ---------------------------------------------------------------------------
# the CLI
# ---------------------------------------------------------------------------


class CliTests(_TempCase):
    def test_list_prints_the_registry_and_the_cache_location(self) -> None:
        with mock.patch("sys.stdout"):
            self.assertEqual(run_cli.main(["list"]), 0)

    def test_list_says_so_when_a_commercial_use_is_declared(self) -> None:
        with mock.patch.dict(os.environ, {cache.COMMERCIAL_USE_ENV: "1"}, clear=False):
            with mock.patch("sys.stdout"):
                self.assertEqual(run_cli.main(["list"]), 0)

    def test_the_defaults_carry_the_non_commercial_declaration_and_the_prompt_hash(self) -> None:
        config = run_cli.default_config("longmemeval_s")
        self.assertIn("non-commercial", config["use_declaration"])
        self.assertEqual(len(config["model"]["judge_prompt_sha256"]), 64)
        self.assertEqual(config["model"]["allowed_model_prefixes"], ["claude-cli:claude-sonnet"])
        self.assertEqual(config["ingestion"], "transcript")
        self.assertTrue(config["fresh_conversation_per_case"])

    def test_compaction_availability_is_detected_not_asserted(self) -> None:
        config = run_cli.default_config("longmemeval_s")
        expected = (cache.repository_root() / "jarvis" / "memory_compaction.py").exists()
        self.assertEqual(config["compaction_available"], expected)

    def test_a_template_comment_never_reaches_the_hashed_config(self) -> None:
        path = self.tmp / "cfg.json"
        path.write_text(json.dumps({"_comment": ["ignore me"], "n_cases": 7}), encoding="utf-8")
        config = run_cli.load_config(path, "longmemeval_s")
        self.assertNotIn("_comment", config)
        self.assertEqual(config["n_cases"], 7)

    def test_the_judge_prompt_hash_cannot_be_overridden_by_a_config(self) -> None:
        path = self.tmp / "cfg.json"
        path.write_text(json.dumps({"model": {"judge_prompt_sha256": "0" * 64}}), encoding="utf-8")
        config = run_cli.load_config(path, "longmemeval_s")
        self.assertNotEqual(config["model"]["judge_prompt_sha256"], "0" * 64)

    def test_a_broken_config_file_is_refused(self) -> None:
        path = self.tmp / "broken.json"
        path.write_text("{not json", encoding="utf-8")
        with self.assertRaises(run_cli.UsageError):
            run_cli.load_config(path, "longmemeval_s")

    def test_a_non_object_config_is_refused(self) -> None:
        path = write_json(self.tmp / "list.json", [1, 2])
        with self.assertRaises(run_cli.UsageError):
            run_cli.load_config(path, "longmemeval_s")

    def test_every_shipped_config_template_loads(self) -> None:
        templates = sorted((cache.repository_root() / "scripts" / "benchmarks" / "configs").glob("*.json"))
        self.assertGreaterEqual(len(templates), 4)
        for template in templates:
            with self.subTest(template=template.name):
                config = run_cli.load_config(template, "longmemeval_s")
                self.assertEqual(config["model"]["allowed_model_prefixes"], ["claude-cli:claude-sonnet"])

    def test_the_fake_judge_is_deterministic_over_the_closed_verdict_set(self) -> None:
        from scripts.benchmarks import scoring

        correct = scoring.build_judge_prompt("q", "alpha", "the answer is alpha")
        wrong = scoring.build_judge_prompt("q", "alpha", "the answer is beta")
        declined = scoring.build_judge_prompt("q", "alpha", "that is not recorded")
        self.assertEqual(run_cli.fake_judge(correct), "VERDICT: CORRECT")
        self.assertEqual(run_cli.fake_judge(wrong), "VERDICT: INCORRECT")
        self.assertEqual(run_cli.fake_judge(declined), "VERDICT: ABSTAINED")
        # and each of those parses back to the verdict it names
        self.assertEqual(scoring.parse_judge_verdict(run_cli.fake_judge(correct)), "CORRECT")
        self.assertIs(run_cli.make_judge("fake", "m"), run_cli.fake_judge)

    def test_list_prints_the_judge_prompt_hash_the_doc_promises(self) -> None:
        stream = io.StringIO()
        with mock.patch("sys.stdout", stream):
            run_cli.main(["list"])
        printed = stream.getvalue()
        self.assertIn(scoring.judge_prompt_sha256(), printed)
        self.assertIn("temperature=0.0", printed)

    def test_the_full_tier_needs_the_intent_stated_out_loud(self) -> None:
        with mock.patch("sys.stdout"), mock.patch("sys.stderr"):
            code = run_cli.main([
                "run", "longmemeval-shape",
                "--config", str(cache.repository_root() / "scripts/benchmarks/configs/longmemeval_shape_config.json"),
                "--tier", "full", "--provider", "fake", "--out", str(self.tmp),
            ])
        self.assertEqual(code, 2)
        self.assertEqual(list(self.tmp.iterdir()), [])

    def test_an_n_above_the_configs_budget_needs_the_same_confirmation(self) -> None:
        arguments = argparse.Namespace(
            benchmark="longmemeval-shape", n=9999, confirm_full=False, smoke=False
        )
        with self.assertRaises(run_cli.UsageError) as caught:
            run_cli._check_tier(arguments, {"n_cases": 25}, "subset")
        self.assertEqual(caught.exception.code, "n_exceeds_config")

    def test_confirm_full_lets_the_full_tier_through(self) -> None:
        arguments = argparse.Namespace(benchmark="x", n=9999, confirm_full=True, smoke=False)
        run_cli._check_tier(arguments, {"n_cases": 25}, "full")

    def test_a_ruler_smoke_exercises_every_shape_not_just_the_first(self) -> None:
        with mock.patch("sys.stdout"):
            run_cli.main([
                "run", "ruler_style",
                "--config", str(cache.repository_root() / "scripts/benchmarks/configs/ruler_style_config.json"),
                "--smoke", "--provider", "fake", "--out", str(self.tmp),
            ])
        rows = report.read_cases(next(self.tmp.glob("*.smoke.jsonl")))
        self.assertEqual(set(row["task"] for row in rows), set(ruler_style.TASKS))

    def test_round_robin_interleaves_so_a_prefix_covers_every_key(self) -> None:
        items = ["a1", "a2", "a3", "b1", "b2", "c1"]
        woven = run_cli._round_robin(items, key=lambda item: item[0])
        self.assertEqual(woven[:3], ["a1", "b1", "c1"])
        self.assertEqual(sorted(woven), sorted(items))
        self.assertEqual(run_cli._round_robin([], key=lambda item: item), [])

    def test_the_runtime_block_is_read_out_of_the_published_config(self) -> None:
        config = run_cli.default_config("longmemeval_s")
        config["runtime"]["JARVIS_CONTEXT_LENGTH"] = 8192
        config["runtime"]["JARVIS_MEMORY_EMBEDDINGS"] = "openai"
        config["runtime"]["JARVIS_DIRECT_CONTEXT_LENGTH"] = 131072
        self.assertEqual(run_cli.runtime_settings(config), (8192, "openai", 131072))
        config["runtime"]["JARVIS_CONTEXT_LENGTH"] = "not a number"
        with self.assertRaises(run_cli.UsageError) as caught:
            run_cli.runtime_settings(config)
        self.assertEqual(caught.exception.code, "bad_config")

    def test_the_control_arm_is_sized_to_the_grid_and_the_value_recorded(self) -> None:
        # H-4 follow-through: a 32K-token haystack cannot be delivered into a
        # 32K window, so the top row of the default grid would report
        # not-delivered for a configuration artefact rather than a finding.
        with mock.patch("sys.stdout"), mock.patch("sys.stderr"):
            run_cli.main([
                "run", "ruler_style",
                "--config", str(cache.repository_root() / "scripts/benchmarks/configs/ruler_style_config.json"),
                "--n", "2", "--provider", "fake", "--out", str(self.tmp),
                "--arms", "direct", "--lengths", "4096", "--samples-per-cell", "1",
            ])
        rows = report.read_cases(next(self.tmp.glob("*.jsonl")))
        self.assertTrue(rows)
        self.assertTrue(all(row["status"] == "complete" for row in rows))
        self.assertTrue(all(row["delivered_fraction"] == 1.0 for row in rows))

    def test_the_direct_window_defaults_above_the_largest_grid_length(self) -> None:
        runner = driver.make_runner("jarvis", context_length=32768, direct_context_length=65536)
        self.addCleanup(runner.close)
        self.assertEqual(runner.context_length, 32768)
        self.assertEqual(runner.direct_context_length, 65536)
        self.assertEqual(runner.direct_prompt_limit_chars, 65536 * driver.CHARS_PER_TOKEN)

    def test_the_config_pins_the_judge_decoding_parameters(self) -> None:
        config = run_cli.default_config("longmemeval_s")
        self.assertEqual(config["model"]["judge_temperature"], 0.0)
        self.assertEqual(config["model"]["judge_seed"], scoring.JUDGE_SEED)

    def test_a_smoke_run_writes_only_a_smoke_jsonl(self) -> None:
        with mock.patch("sys.stdout"):
            code = run_cli.main([
                "run", "longmemeval-shape",
                "--config", str(cache.repository_root() / "scripts/benchmarks/configs/longmemeval_shape_config.json"),
                "--smoke", "--provider", "fake", "--judge", "--out", str(self.tmp),
            ])
        self.assertEqual(code, 0)
        written = sorted(path.name for path in self.tmp.iterdir())
        self.assertEqual(len(written), 1)
        self.assertTrue(written[0].endswith(".smoke.jsonl"))
        rows = report.read_cases(self.tmp / written[0])
        self.assertEqual(len(rows), run_cli.SMOKE_CASES)
        self.assertTrue(all(row["model"] == driver.FAKE_MODEL for row in rows))

    def test_a_shape_run_is_never_labelled_with_the_benchmarks_plain_name(self) -> None:
        for benchmark, config in (
            ("longmemeval-shape", "longmemeval_shape_config.json"),
            ("locomo-shape", "locomo_shape_config.json"),
        ):
            with self.subTest(benchmark=benchmark):
                out = self.tmp / benchmark
                with mock.patch("sys.stdout"):
                    run_cli.main([
                        "run", benchmark,
                        "--config", str(cache.repository_root() / "scripts/benchmarks/configs" / config),
                        "--smoke", "--provider", "fake", "--out", str(out),
                    ])
                rows = report.read_cases(next(out.glob("*.smoke.jsonl")))
                self.assertTrue(rows)
                self.assertEqual({row["benchmark"] for row in rows}, {benchmark})

    def test_a_fake_provider_run_can_never_write_a_report(self) -> None:
        with mock.patch("sys.stdout"), mock.patch("sys.stderr"):
            code = run_cli.main([
                "run", "locomo-shape",
                "--config", str(cache.repository_root() / "scripts/benchmarks/configs/locomo_shape_config.json"),
                "--n", "4", "--provider", "fake", "--out", str(self.tmp),
            ])
        self.assertEqual(code, 2)
        self.assertFalse(any(path.suffix == ".json" for path in self.tmp.iterdir()))

    def test_an_existing_jsonl_refuses_without_resume_and_continues_with_it(self) -> None:
        arguments = [
            "run", "ruler_style",
            "--config", str(cache.repository_root() / "scripts/benchmarks/configs/ruler_style_config.json"),
            "--smoke", "--provider", "fake", "--out", str(self.tmp),
        ]
        with mock.patch("sys.stdout"):
            self.assertEqual(run_cli.main(arguments), 0)
        jsonl = next(self.tmp.glob("*.smoke.jsonl"))
        before = len(report.read_cases(jsonl))
        with mock.patch("sys.stdout"), mock.patch("sys.stderr"):
            self.assertEqual(run_cli.main(arguments), 2)
        with mock.patch("sys.stdout"):
            self.assertEqual(run_cli.main(arguments + ["--resume"]), 0)
        self.assertEqual(len(report.read_cases(jsonl)), before)

    def test_the_ruler_smoke_runs_both_arms_within_the_case_ceiling(self) -> None:
        with mock.patch("sys.stdout"):
            run_cli.main([
                "run", "ruler_style",
                "--config", str(cache.repository_root() / "scripts/benchmarks/configs/ruler_style_config.json"),
                "--smoke", "--provider", "fake", "--out", str(self.tmp),
            ])
        rows = report.read_cases(next(self.tmp.glob("*.smoke.jsonl")))
        self.assertEqual(len(rows), run_cli.SMOKE_CASES)
        self.assertEqual({row["arm"] for row in rows}, {"jarvis", "direct"})

    def test_an_unknown_benchmark_is_refused_by_name(self) -> None:
        arguments = argparse.Namespace(benchmark="nope", config=None, smoke=False)
        with self.assertRaises(run_cli.UsageError) as caught:
            run_cli.cmd_run(arguments)
        self.assertEqual(caught.exception.code, "unknown_benchmark")

    def test_materialise_refuses_a_benchmark_it_does_not_own(self) -> None:
        arguments = argparse.Namespace(n=None, smoke=False, cache=None)
        with self.assertRaises(run_cli.UsageError):
            run_cli.materialise("ruler_style", run_cli.default_config("ruler_style"), arguments, scored=True)

    def test_the_live_judge_is_built_lazily_and_never_called_here(self) -> None:
        judge = run_cli.make_judge("jarvis", "claude-cli:claude-sonnet-4-5")
        self.assertTrue(callable(judge))
        self.assertIsNot(judge, run_cli.fake_judge)

    def test_resume_skips_the_cases_already_scored(self) -> None:
        arguments = [
            "run", "longmemeval-shape",
            "--config", str(cache.repository_root() / "scripts/benchmarks/configs/longmemeval_shape_config.json"),
            "--smoke", "--provider", "fake", "--out", str(self.tmp),
        ]
        with mock.patch("sys.stdout"):
            self.assertEqual(run_cli.main(arguments), 0)
        jsonl = next(self.tmp.glob("*.smoke.jsonl"))
        before = report.read_cases(jsonl)
        with mock.patch("sys.stdout"):
            self.assertEqual(run_cli.main(arguments + ["--resume"]), 0)
        self.assertEqual(report.read_cases(jsonl), before)

    def test_the_ruler_grid_honours_an_explicit_case_count(self) -> None:
        with mock.patch("sys.stdout"), mock.patch("sys.stderr"):
            # A fake provider still refuses to publish, so the exit code is 2;
            # the per-case JSONL is written all the same.
            code = run_cli.main([
                "run", "ruler_style",
                "--config", str(cache.repository_root() / "scripts/benchmarks/configs/ruler_style_config.json"),
                "--n", "2", "--provider", "fake", "--out", str(self.tmp),
                "--arms", "jarvis", "--lengths", "512",
                "--samples-per-cell", "1",
            ])
        self.assertEqual(code, 2)
        rows = report.read_cases(next(self.tmp.glob("*.jsonl")))
        self.assertEqual(len(rows), 2)
        self.assertEqual({row["arm"] for row in rows}, {"jarvis"})
        self.assertEqual({row["length"] for row in rows}, {512})

    def test_an_allowed_model_writes_a_report_with_its_provenance(self) -> None:
        class _AllowedRunner(driver.FakeCaseRunner):
            model_hint = "claude-cli:claude-sonnet-4-5"

            def ask(self, case):
                outcome = super().ask(case)
                return driver.Outcome(
                    outcome.case_id, outcome.reply, "claude-cli:claude-sonnet-4-5",
                    outcome.status, outcome.tool_calls, outcome.latency_ms,
                    outcome.prompt_tokens, outcome.completion_tokens,
                )

        with mock.patch.object(run_cli, "make_runner", lambda *a, **k: _AllowedRunner()):
            with mock.patch("sys.stdout"):
                code = run_cli.main([
                    "run", "longmemeval-shape",
                    "--config", str(cache.repository_root() / "scripts/benchmarks/configs/longmemeval_shape_config.json"),
                    "--n", "5", "--provider", "fake", "--out", str(self.tmp),
                    "--limitation", "synthetic shape, not LongMemEval",
                ])
        self.assertEqual(code, 0)
        written = [path for path in self.tmp.iterdir() if path.suffix == ".json"]
        self.assertEqual(len(written), 1)
        payload = json.loads(written[0].read_text(encoding="utf-8"))
        self.assertEqual(payload["tier"], "subset")
        self.assertEqual(payload["n"], 5)
        self.assertEqual(payload["aggregate"]["models_seen"], ["claude-cli:claude-sonnet-4-5"])
        self.assertIn("synthetic shape, not LongMemEval", payload["limitations"])
        self.assertIn("non-commercial", payload["config"]["use_declaration"])
        rendered = report.render_markdown(payload)
        self.assertIn(payload["config_sha256"][:12], rendered)

    def test_report_summarises_and_renders_a_written_report(self) -> None:
        payload = {
            "schema": report.REPORT_SCHEMA,
            "benchmark": "probe", "finished_utc": "2026-09-04T00:00:00Z", "n": 1, "tier": "subset",
            "commit": "abc", "config_sha256": "def", "config": {}, "limitations": [],
            "aggregate": {"overall": {"deterministic": 1.0, "judge": None},
                          "by_group": {}, "group_key": "type",
                          "abstention": {"n": 0, "accuracy": None},
                          "latency_ms": {"p50": 1.0}, "tokens_per_answer": {"prompt_p50": 1.0}},
        }
        path = write_json(self.tmp / "r.json", payload)
        with mock.patch("sys.stdout"):
            self.assertEqual(run_cli.main(["report", str(path)]), 0)
            self.assertEqual(run_cli.main(["report", str(path), "--markdown"]), 0)

    def test_a_renamed_per_case_file_is_never_rendered_as_a_result(self) -> None:
        # A single-case JSONL happens to parse as a JSON object. Renaming it
        # must not get it treated as a publishable report.
        disguised = write_json(self.tmp / "looks-official.json", {"case_id": "c1", "det": True})
        with mock.patch("sys.stdout"), mock.patch("sys.stderr"):
            self.assertEqual(run_cli.main(["report", str(disguised), "--markdown"]), 2)
            self.assertEqual(run_cli.main(["report", str(disguised)]), 2)
        with self.assertRaises(report.ReportError) as caught:
            report.require_report({"case_id": "c1"})
        self.assertEqual(caught.exception.code, "not_a_report")

    def test_report_refuses_an_unreadable_path(self) -> None:
        with mock.patch("sys.stderr"):
            self.assertEqual(run_cli.main(["report", str(self.tmp / "absent.json")]), 2)


class CliDatasetPathTests(_TempCase):
    """The dataset-backed paths, against in-test files in the exact formats."""

    def _plant(self, dataset: str, filename: str, payload: object, *, licence: bool) -> str:
        directory = self.tmp / dataset
        directory.mkdir(parents=True, exist_ok=True)
        path = write_json(directory / filename, payload)
        if licence:
            (directory / cache.LICENCE_FILENAME).write_text("licence text", encoding="utf-8")
        return cache.sha256_file(path)

    def test_locomo_materialises_from_the_cache_with_its_licence_recorded(self) -> None:
        digest = self._plant("locomo10", "locomo10.json", locomo_records(2, 5), licence=True)
        config = run_cli.default_config("locomo10")
        config["dataset"]["sha256"] = digest
        arguments = argparse.Namespace(n=4, smoke=False, cache=str(self.tmp))
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(cache.COMMERCIAL_USE_ENV, None)
            with mock.patch("sys.stdout"):
                pairs, dataset_block, group_key = run_cli.materialise(
                    "locomo10", config, arguments, scored=True
                )
        self.assertEqual(len(pairs), 4)
        self.assertEqual(group_key, "category")
        self.assertEqual(dataset_block["licence_class"], "restricted")
        self.assertTrue(dataset_block["licence_sha256"])

    def test_longmemeval_materialises_and_stratifies_from_the_cache(self) -> None:
        digest = self._plant(
            "longmemeval_s", "longmemeval_s_cleaned.json", longmemeval_records(6), licence=True
        )
        config = run_cli.default_config("longmemeval_s")
        config["dataset"]["sha256"] = digest
        arguments = argparse.Namespace(n=3, smoke=False, cache=str(self.tmp))
        pairs, dataset_block, group_key = run_cli.materialise(
            "longmemeval_s", config, arguments, scored=True
        )
        self.assertEqual(len(pairs), 3)
        self.assertEqual(group_key, "type")
        self.assertEqual(dataset_block["licence_class"], "open")

    def test_a_commercial_declaration_stops_locomo_before_the_cache_is_read(self) -> None:
        self._plant("locomo10", "locomo10.json", locomo_records(1, 5), licence=True)
        config = run_cli.default_config("locomo10")
        arguments = argparse.Namespace(n=2, smoke=False, cache=str(self.tmp))
        with mock.patch.dict(os.environ, {cache.COMMERCIAL_USE_ENV: "1"}, clear=False):
            with self.assertRaises(cache.DatasetError) as caught:
                run_cli.materialise("locomo10", config, arguments, scored=True)
        self.assertEqual(caught.exception.code, "commercial_use_declared")

    def test_fetch_verifies_digests_and_runs_the_leakage_gate(self) -> None:
        payload = json.dumps(longmemeval_records(3), ensure_ascii=False)

        def _fetcher(url: str, destination: Path) -> None:
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(
                payload if url.endswith(".json") else "MIT licence text", encoding="utf-8"
            )

        arguments = argparse.Namespace(dataset="longmemeval_s", cache=str(self.tmp))
        with mock.patch.object(cache, "urllib_fetch", _fetcher), mock.patch("sys.stdout"):
            with mock.patch.object(cache, "tracked_text_files", return_value=[]):
                self.assertEqual(run_cli.cmd_fetch(arguments), 0)
        self.assertTrue((self.tmp / "longmemeval_s" / "longmemeval_s_cleaned.json").exists())
        self.assertTrue((self.tmp / "longmemeval_s" / cache.LICENCE_FILENAME).exists())

    def test_fetch_reports_open_licence_drift_without_refusing(self) -> None:
        directory = self.tmp / "longmemeval_s"
        directory.mkdir(parents=True, exist_ok=True)
        write_json(directory / "longmemeval_s_cleaned.json", longmemeval_records(3))
        (directory / cache.LICENCE_FILENAME).write_text("moved licence text", encoding="utf-8")
        drifted = cache.spec_for("longmemeval_s", overrides={"licence_sha256": "9" * 64})
        arguments = argparse.Namespace(dataset="longmemeval_s", cache=str(self.tmp))
        with mock.patch.object(cache, "spec_for", return_value=drifted):
            with mock.patch.object(cache, "tracked_text_files", return_value=[]):
                with mock.patch("sys.stdout"):
                    self.assertEqual(run_cli.cmd_fetch(arguments), 0)

    def test_fetch_fails_when_the_leakage_gate_finds_a_value_in_the_tree(self) -> None:
        data_path = self.tmp / "longmemeval_s" / "longmemeval_s_cleaned.json"
        data_path.parent.mkdir(parents=True, exist_ok=True)
        data_path.write_text(
            json.dumps(longmemeval_records(3), ensure_ascii=False), encoding="utf-8"
        )
        (data_path.parent / cache.LICENCE_FILENAME).write_text("MIT", encoding="utf-8")
        values = cache.sample_dataset_values(data_path)
        self.assertTrue(values)
        leaked = self.tmp / "leaked.md"
        leaked.write_text(values[0], encoding="utf-8")
        arguments = argparse.Namespace(dataset="longmemeval_s", cache=str(self.tmp))
        with mock.patch.object(cache, "tracked_text_files", return_value=[leaked]):
            with mock.patch("sys.stdout"):
                self.assertEqual(run_cli.cmd_fetch(arguments), 3)


if __name__ == "__main__":  # pragma: no cover - manual invocation
    unittest.main()
