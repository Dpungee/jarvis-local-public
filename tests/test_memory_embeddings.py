from __future__ import annotations

import json
import io
import http.client
import ssl
import tempfile
import unittest
import urllib.error
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

from jarvis.memory_embeddings import (
    EmbeddingError,
    OPENAI_EMBEDDINGS_URL,
    OpenAIEmbeddingClient,
    build_memory_embedder,
    run_memory_index_batch,
)
from jarvis.memory import Memory


class FakeResponse:
    def __init__(self, payload):
        self.body = json.dumps(payload).encode("utf-8")

    def read(self, size=-1):
        return self.body if size < 0 else self.body[:size]

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False


class RawResponse(FakeResponse):
    def __init__(self, body):
        self.body = body


class BrokenReadResponse(FakeResponse):
    def __init__(self):
        self.body = b""

    def read(self, size=-1):
        del size
        raise http.client.IncompleteRead(b"partial")


class SequenceOpen:
    def __init__(self, *items):
        self.items = list(items)
        self.calls = 0

    def __call__(self, request, *, timeout):
        del request, timeout
        self.calls += 1
        item = self.items.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


class CapturingOpen:
    def __init__(self, response):
        self.response = response
        self.request = None
        self.timeout = None

    def __call__(self, request, *, timeout):
        self.request = request
        self.timeout = timeout
        return self.response


class MemoryEmbeddingTests(unittest.TestCase):
    def test_background_index_batch_is_leased_and_idempotent(self):
        class FakeEmbedder:
            model = "test-embedding"
            timeout = 5.0

            def __init__(self):
                self.calls = []

            def embed(self, inputs):
                self.calls.append(list(inputs))
                return [[1.0, float(index + 1)] for index, _value in enumerate(inputs)]

        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            with Memory(data / "jarvis.db") as memory:
                memory.remember_verified(
                    "first durable memory", "fact", "operator",
                    origin="verified_import",
                )
                memory.remember_verified(
                    "second durable memory", "fact", "operator",
                    origin="verified_import",
                )
            embedder = FakeEmbedder()
            config = SimpleNamespace(data_dir=data)
            first = run_memory_index_batch(
                config, "indexer:test", embedder=embedder, limit=32
            )
            second = run_memory_index_batch(
                config, "indexer:test", embedder=embedder, limit=32
            )
            self.assertEqual(first, {"enabled": True, "claimed": 2, "stored": 2})
            self.assertEqual(second, {"enabled": True, "claimed": 0, "stored": 0})
            self.assertEqual(len(embedder.calls), 1)

    def test_background_index_failure_releases_owner_with_bounded_backoff(self):
        class FailingEmbedder:
            model = "test-embedding"
            timeout = 5.0

            def embed(self, _inputs):
                raise EmbeddingError("synthetic outage")

        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            with Memory(data / "jarvis.db") as memory:
                memory.remember_verified(
                    "retryable memory", "fact", "operator",
                    origin="verified_import",
                )
            with self.assertRaises(EmbeddingError):
                run_memory_index_batch(
                    SimpleNamespace(data_dir=data),
                    "indexer:failing",
                    embedder=FailingEmbedder(),
                )
            with Memory(data / "jarvis.db") as memory:
                lease = memory.db.execute(
                    """SELECT lease_owner, last_error, lease_expires_at
                       FROM memory_embedding_leases"""
                ).fetchone()
                self.assertIsNone(lease["lease_owner"])
                self.assertIn("synthetic outage", lease["last_error"])
                self.assertEqual(memory.memory_quality()["totals"]["embedding_failures"], 1)
                self.assertEqual(
                    memory.claim_pending_memory_embeddings(
                        "test-embedding", "indexer:too-soon"
                    ),
                    [],
                )

    def test_openai_embedding_client_batches_and_validates_vectors(self):
        first = [1.0, *([0.0] * 63)]
        second = [0.0, 1.0, *([0.0] * 62)]
        opener = CapturingOpen(FakeResponse({
            "object": "list",
            "data": [
                {"object": "embedding", "index": 1, "embedding": second},
                {"object": "embedding", "index": 0, "embedding": first},
            ],
        }))
        client = OpenAIEmbeddingClient(
            "sk-test-not-real",
            model="text-embedding-3-small",
            dimensions=64,
            open_url=opener,
            max_retries=0,
        )

        vectors = client.embed(["first", "second"])

        self.assertEqual(vectors, [first, second])
        self.assertEqual(opener.request.full_url, OPENAI_EMBEDDINGS_URL)
        body = json.loads(opener.request.data.decode("utf-8"))
        self.assertEqual(body["input"], ["first", "second"])
        self.assertEqual(body["dimensions"], 64)
        self.assertEqual(body["encoding_format"], "float")

    def test_openai_embedding_client_rejects_wrong_dimensions(self):
        client = OpenAIEmbeddingClient(
            "sk-test-not-real",
            dimensions=64,
            open_url=CapturingOpen(FakeResponse({
                "data": [{"index": 0, "embedding": [1.0]}],
            })),
            max_retries=0,
        )

        with self.assertRaisesRegex(EmbeddingError, "dimensions"):
            client.embed(["input"])

    def test_transient_http_failure_retries_without_exposing_provider_body(self):
        error = urllib.error.HTTPError(
            OPENAI_EMBEDDINGS_URL,
            429,
            "limited",
            {},
            io.BytesIO(b"sk-test-not-real provider detail"),
        )
        opener = SequenceOpen(
            error,
            FakeResponse({"data": [{"index": 0, "embedding": [0.0] * 64}]}),
        )
        sleeps = []
        client = OpenAIEmbeddingClient(
            "sk-test-not-real",
            dimensions=64,
            max_retries=1,
            open_url=opener,
            sleep=sleeps.append,
        )

        self.assertEqual(len(client.embed(["input"])[0]), 64)
        self.assertEqual(opener.calls, 2)
        self.assertEqual(sleeps, [0.5])

    def test_malformed_oversized_and_tls_responses_fail_closed(self):
        cases = (
            (RawResponse(b"not-json"), "malformed JSON"),
            (RawResponse(b"x" * (16 * 1024 * 1024 + 1)), "size limit"),
            (urllib.error.URLError(ssl.SSLError("bad certificate")), "could not be reached"),
        )
        for response, message in cases:
            with self.subTest(message=message):
                client = OpenAIEmbeddingClient(
                    "sk-test-not-real",
                    dimensions=64,
                    max_retries=1,
                    open_url=SequenceOpen(response),
                    sleep=lambda _delay: self.fail("non-retryable failure slept"),
                )
                with self.assertRaisesRegex(EmbeddingError, message) as raised:
                    client.embed(["input"])
                self.assertNotIn("sk-test-not-real", str(raised.exception))

    def test_incomplete_http_read_is_normalized_and_retried(self):
        opener = SequenceOpen(
            BrokenReadResponse(),
            FakeResponse({"data": [{"index": 0, "embedding": [0.0] * 64}]}),
        )
        client = OpenAIEmbeddingClient(
            "sk-test-not-real",
            dimensions=64,
            max_retries=1,
            open_url=opener,
            sleep=lambda _delay: None,
        )

        self.assertEqual(len(client.embed(["input"])[0]), 64)
        self.assertEqual(opener.calls, 2)

    def test_vector_indexes_and_components_are_exact_and_unique(self):
        bad_payloads = (
            {"data": [{"index": 0.0, "embedding": [0.0] * 64}]},
            {"data": [
                {"index": 0, "embedding": [0.0] * 64},
                {"index": 0, "embedding": [0.0] * 64},
            ]},
            {"data": [{"index": 0, "embedding": [float("nan")] * 64}]},
        )
        for payload in bad_payloads:
            with self.subTest(payload=payload):
                client = OpenAIEmbeddingClient(
                    "sk-test-not-real",
                    dimensions=64,
                    max_retries=0,
                    open_url=CapturingOpen(FakeResponse(payload)),
                )
                with self.assertRaises(EmbeddingError):
                    client.embed(["input"])

    def test_cloud_memory_requires_explicit_trusted_external_boundary(self):
        base = dict(
            memory_auto_improve=True,
            memory_embeddings="openai",
            cloud_enabled=True,
            memory_embedding_model="text-embedding-3-small",
            memory_embedding_dimensions=64,
            cloud_generation_timeout=30,
            cloud_max_retries=0,
        )
        with patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test-not-real"}, clear=True):
            self.assertIsNone(build_memory_embedder(SimpleNamespace(
                **base, external_access="disabled"
            )))
            self.assertIsInstance(
                build_memory_embedder(SimpleNamespace(
                    **base, external_access="trusted-external"
                )),
                OpenAIEmbeddingClient,
            )


if __name__ == "__main__":
    unittest.main()
