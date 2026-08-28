from __future__ import annotations

import json
import http.client
import math
import os
import socket
import ssl
import time
import urllib.error
import urllib.request
from typing import Any, Callable


OPENAI_EMBEDDINGS_URL = "https://api.openai.com/v1/embeddings"
MAX_EMBEDDING_INPUTS = 64
MAX_EMBEDDING_INPUT_CHARS = 8_000
MAX_EMBEDDING_RESPONSE_BYTES = 16 * 1024 * 1024


class EmbeddingError(RuntimeError):
    """A bounded embedding request failed without affecting normal memory recall."""


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_: Any, **__: Any) -> None:
        return None


class OpenAIEmbeddingClient:
    """Small dependency-free client for OpenAI's embeddings endpoint."""

    def __init__(
        self,
        api_key: str,
        *,
        model: str = "text-embedding-3-small",
        dimensions: int = 512,
        timeout: float = 30.0,
        max_retries: int = 1,
        open_url: Callable[..., Any] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        key = str(api_key).strip()
        if not key or len(key) > 4096 or any(ord(char) < 32 for char in key):
            raise ValueError("OpenAI API key is missing or invalid")
        normalized_model = str(model).strip()
        if not normalized_model or len(normalized_model) > 200:
            raise ValueError("Embedding model is invalid")
        if isinstance(dimensions, bool) or not isinstance(dimensions, int):
            raise ValueError("Embedding dimensions must be an integer")
        if not 64 <= dimensions <= 4096:
            raise ValueError("Embedding dimensions must be between 64 and 4096")
        self.api_key = key
        self.model = normalized_model
        self.dimensions = dimensions
        self.timeout = max(1.0, min(float(timeout), 120.0))
        self.max_retries = max(0, min(int(max_retries), 3))
        self._sleep = sleep
        opener = urllib.request.build_opener(_NoRedirectHandler())
        self._open_url = open_url or opener.open

    def embed(self, inputs: list[str]) -> list[list[float]]:
        if not isinstance(inputs, list) or not inputs:
            raise ValueError("Embedding inputs must be a non-empty list")
        if len(inputs) > MAX_EMBEDDING_INPUTS:
            raise ValueError(f"Embedding request exceeds {MAX_EMBEDDING_INPUTS} inputs")
        normalized: list[str] = []
        for value in inputs:
            text = str(value).strip()
            if not text:
                raise ValueError("Embedding input must not be empty")
            normalized.append(text[:MAX_EMBEDDING_INPUT_CHARS])
        payload = json.dumps(
            {
                "input": normalized,
                "model": self.model,
                "dimensions": self.dimensions,
                "encoding_format": "float",
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        deadline = time.monotonic() + self.timeout
        for attempt in range(self.max_retries + 1):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise EmbeddingError("embedding request exceeded its deadline")
            request = urllib.request.Request(
                OPENAI_EMBEDDINGS_URL,
                data=payload,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            try:
                with self._open_url(request, timeout=max(0.1, remaining)) as response:
                    raw = response.read(MAX_EMBEDDING_RESPONSE_BYTES + 1)
                if len(raw) > MAX_EMBEDDING_RESPONSE_BYTES:
                    raise EmbeddingError("embedding response exceeded its size limit")
                try:
                    body = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    raise EmbeddingError("embedding endpoint returned malformed JSON") from None
                return self._validated_vectors(body, len(normalized))
            except urllib.error.HTTPError as exc:
                status = int(exc.code)
                try:
                    exc.close()
                except Exception:
                    pass
                retryable = status in {408, 409, 429, 500, 502, 503, 504}
                if retryable and attempt < self.max_retries:
                    self._sleep(min(0.5 * (2**attempt), max(0.0, deadline - time.monotonic())))
                    continue
                raise EmbeddingError(f"embedding request failed with HTTP {status}") from None
            except (
                urllib.error.URLError,
                TimeoutError,
                socket.timeout,
                ConnectionError,
                http.client.HTTPException,
                OSError,
            ) as exc:
                reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
                retryable = not isinstance(reason, (ssl.SSLError, ssl.CertificateError, ValueError))
                if retryable and attempt < self.max_retries:
                    self._sleep(min(0.5 * (2**attempt), max(0.0, deadline - time.monotonic())))
                    continue
                raise EmbeddingError("embedding endpoint could not be reached") from None
        raise EmbeddingError("embedding request failed")

    def _validated_vectors(self, body: Any, expected: int) -> list[list[float]]:
        if not isinstance(body, dict) or not isinstance(body.get("data"), list):
            raise EmbeddingError("embedding endpoint returned an unexpected response")
        ordered: list[list[float] | None] = [None] * expected
        for item in body["data"]:
            if (
                not isinstance(item, dict)
                or isinstance(item.get("index"), bool)
                or not isinstance(item.get("index"), int)
            ):
                raise EmbeddingError("embedding endpoint returned malformed vector metadata")
            index = item["index"]
            vector = item.get("embedding")
            if not 0 <= index < expected or not isinstance(vector, list):
                raise EmbeddingError("embedding endpoint returned an invalid vector")
            if ordered[index] is not None:
                raise EmbeddingError("embedding response duplicated an input index")
            if len(vector) != self.dimensions:
                raise EmbeddingError("embedding vector dimensions did not match the request")
            normalized: list[float] = []
            for component in vector:
                if isinstance(component, bool):
                    raise EmbeddingError("embedding vector contained a non-number")
                try:
                    number = float(component)
                except (TypeError, ValueError):
                    raise EmbeddingError("embedding vector contained a non-number") from None
                if not math.isfinite(number):
                    raise EmbeddingError("embedding vector contained a non-finite number")
                normalized.append(number)
            ordered[index] = normalized
        if any(vector is None for vector in ordered):
            raise EmbeddingError("embedding response omitted one or more inputs")
        return [vector for vector in ordered if vector is not None]


def build_memory_embedder(config: Any) -> OpenAIEmbeddingClient | None:
    """Create the optional neural indexer; absence never disables sparse recall."""
    if not bool(getattr(config, "memory_auto_improve", True)):
        return None
    if str(getattr(config, "memory_embeddings", "disabled")) != "openai":
        return None
    if not bool(getattr(config, "cloud_enabled", True)):
        return None
    if str(getattr(config, "external_access", "disabled")) != "trusted-external":
        return None
    key = os.getenv("OPENAI_API_KEY")
    if not key:
        return None
    return OpenAIEmbeddingClient(
        key,
        model=str(getattr(config, "memory_embedding_model", "text-embedding-3-small")),
        dimensions=int(getattr(config, "memory_embedding_dimensions", 512)),
        timeout=min(60.0, float(getattr(config, "cloud_generation_timeout", 600.0))),
        max_retries=min(1, int(getattr(config, "cloud_max_retries", 2))),
    )


def run_memory_index_batch(
    config: Any,
    lease_owner: str,
    *,
    embedder: OpenAIEmbeddingClient | None = None,
    limit: int = 32,
) -> dict[str, Any]:
    """Lease and index one batch without holding SQLite open during network I/O."""
    from .memory import Memory
    from .vault import Vault

    database = config.data_dir / "jarvis.db"
    vault_dir = getattr(config, "vault_dir", None)
    vault_sync: dict[str, Any] | None = None
    vault_error = False
    if vault_dir is not None:
        try:
            notes = Vault(vault_dir).list_notes()
            with Memory(database) as memory:
                vault_sync = memory.sync_vault_notes(notes)
        except (OSError, RuntimeError, ValueError):
            vault_error = True
    vault_result = (
        {"vault": vault_sync, "vault_error": vault_error}
        if vault_dir is not None
        else {}
    )
    client = embedder or build_memory_embedder(config)
    if client is None:
        return {"enabled": False, "claimed": 0, "stored": 0, **vault_result}
    with Memory(database) as memory:
        records = memory.claim_pending_memory_embeddings(
            client.model,
            lease_owner,
            limit=limit,
            lease_seconds=max(60, int(client.timeout) + 30),
        )
    if not records:
        return {"enabled": True, "claimed": 0, "stored": 0, **vault_result}
    try:
        vectors = client.embed([str(item["content"]) for item in records])
        with Memory(database) as memory:
            stored = memory.store_memory_embeddings(
                client.model,
                records,
                vectors,
                lease_owner=lease_owner,
            )
        return {
            "enabled": True,
            "claimed": len(records),
            "stored": stored,
            **vault_result,
        }
    except (EmbeddingError, RuntimeError, ValueError) as exc:
        with Memory(database) as memory:
            memory.fail_memory_embedding_batch(
                client.model, records, lease_owner, exc
            )
        raise
