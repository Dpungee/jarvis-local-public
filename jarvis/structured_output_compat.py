"""Compile a structured-output schema against the configured provider.

Why this module exists
----------------------
The TaskContract resolver asks the provider to constrain its reply to
``task_contract_response_schema()``. A provider that cannot compile that schema
into a sampler answers HTTP 400 before generating anything, and the resolver's
caller treats the resulting error as an ordinary provider outage - so the
semantic lane silently stops engaging and no operator surface shows it.

Ollama 0.32.15 does exactly that, and the trigger is narrower than a size
ceiling. What its grammar compiler refuses is the **repetition upper bound
2000**, wherever that number reaches it: ``maxLength: 2000``, ``minLength:
2000``, ``maxItems: 2000``, and a ``pattern`` carrying a ``{n,2000}``
repetition all fail identically, at any depth in the schema. 1999, 2001, 3000
and 8000 all compile, so there is no ceiling and nothing to clamp to.

The failure also has a precondition: it appears only when the request carries
``think: false``. With ``think`` absent or true the same schema compiles.
Measured on this host against Ollama 0.32.15 / ``qwen3.5:9b``, with
``goal.maxLength`` at 2000: ``think: false`` -> HTTP 400, ``think`` absent ->
HTTP 200, ``think: true`` -> HTTP 200. That matters because the production
resolver sends ``think=False`` (``jarvis/agent.py``, and ``_resolve_one`` in
``jarvis/task_contract_benchmark.py``), so production sits squarely in the
affected configuration - and so must this probe, which therefore always sends
``think=False``.

This module is the check that would have caught it: it compiles the schema the
production resolver actually sends, in the configuration production sends it,
against the provider that will actually serve it, and reports one of four
categories. It answers "can this provider honour our structured-output contract
right now" - not "is the model any good".

Privacy
-------
The probe sends a fixed, operator-free prompt. The result carries a category, a
closed-vocabulary reason, the model that was asked for, the provider's own
version string, the HTTP status and a digest of the schema. It never carries
prompt text, model output, provider error text, a host name, or a local path.
The version string is provider-controlled, so it is admitted only if it matches
a narrow token shape and is dropped otherwise.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from collections.abc import Iterator, Mapping
from copy import deepcopy
from dataclasses import asdict, dataclass
from typing import Any

from .ollama_client import OllamaClient, OllamaError, OllamaGrammarRejected
from .task_contract import task_contract_response_schema

PROVIDER_OLLAMA = "ollama"
DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"
DEFAULT_PROBE_MODEL = "qwen3.5:9b"

#: The provider compiled the schema and produced a message.
CATEGORY_OK = "ok"
#: The provider refused the schema at the grammar-compiler stage.
CATEGORY_GRAMMAR_REJECTED = "grammar_rejected"
#: The endpoint or the model was not there to answer in time.
CATEGORY_MODEL_UNAVAILABLE = "model_unavailable"
#: The provider was reachable and accepted the schema but the call still failed.
CATEGORY_GENERATION_FAILED = "generation_failed"

CATEGORIES = (
    CATEGORY_OK,
    CATEGORY_GRAMMAR_REJECTED,
    CATEGORY_MODEL_UNAVAILABLE,
    CATEGORY_GENERATION_FAILED,
)

#: Closed reason vocabulary. Reasons are chosen from this set so a recorded
#: probe result can never contain free-form provider text.
REASONS = (
    "compiled",
    "grammar_compile_rejected",
    "provider_unreachable",
    "provider_timeout",
    "model_not_found",
    "provider_error",
    "malformed_response",
    "invalid_request",
)

#: A fixed prompt with no operator content. Only the grammar matters here.
PROBE_PROMPT = "Reply with the object."
#: ``OllamaClient`` floors ``num_predict`` at 128, so this is the lowest output
#: budget the public client accepts. A compile failure costs nothing anyway: the
#: provider rejects the request before it generates a token.
PROBE_MAX_OUTPUT_TOKENS = 128
PROBE_CONTEXT_LENGTH = 2048
PROBE_GENERATION_TIMEOUT = 120.0
PROBE_HEALTH_TIMEOUT = 5.0
#: Production resolves contracts with thinking off, and the defect only appears
#: in that configuration, so the probe must not drift away from it.
PROBE_THINK = False

#: Repetition bounds carrying this value are refused by Ollama's grammar
#: compiler. Kept here so the schema guard and its regression test read one
#: definition.
REJECTED_GRAMMAR_BOUND = 2000
#: JSON Schema keywords whose integer values reach the grammar compiler as a
#: repetition bound. ``pattern`` carries one too, but as text - see
#: :data:`_REJECTED_REPETITION_RE`.
BOUND_KEYWORDS = ("maxLength", "minLength", "maxItems", "minItems")
#: A regex repetition whose upper bound is the rejected value - ``{0,2000}``,
#: ``{,2000}``, ``{2000}``, and the spaced variants - inside a ``pattern``.
_REJECTED_REPETITION_RE = re.compile(
    r"\{\s*\d*\s*,?\s*" + str(REJECTED_GRAMMAR_BOUND) + r"\s*\}"
)
#: Provider-controlled version strings are admitted only in this shape: a short
#: token of the characters real version strings use. Anything else - a long
#: string, a path, a host name, an injected newline - is dropped rather than
#: recorded, because the result is meant to be safe to write into evidence.
_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+:~-]{0,63}$")


@dataclass(frozen=True)
class StructuredOutputProbe:
    """One provider compatibility observation. Safe to record verbatim."""

    category: str
    reason: str
    provider: str
    model: str
    provider_version: str | None
    status_code: int | None
    schema_sha256: str
    elapsed_seconds: float

    @property
    def supported(self) -> bool:
        return self.category == CATEGORY_OK

    def to_payload(self) -> dict[str, Any]:
        return asdict(self)


def canonical_schema_sha256(schema: Mapping[str, Any]) -> str:
    """Digest a schema independently of key order and whitespace."""
    return hashlib.sha256(
        json.dumps(schema, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def iter_schema_bounds(
    schema: Any,
    *,
    path: str = "",
) -> Iterator[tuple[str, str, int]]:
    """Yield ``(path, keyword, value)`` for every repetition bound in a schema.

    Walks objects and arrays alike, so a bound nested inside ``items``,
    ``properties``, ``anyOf`` or any other container is reported too - the
    defect this guards against is position-independent.

    Two shapes are reported. Integer bounds under :data:`BOUND_KEYWORDS` are
    yielded with their own value. A ``pattern`` string carrying a repetition
    whose upper bound is :data:`REJECTED_GRAMMAR_BOUND` is yielded as
    ``(path, "pattern", REJECTED_GRAMMAR_BOUND)`` - a regex repetition reaches
    the same compiler, but only the known-bad bound is recognised in text, so
    patterns are not enumerated for other values.
    """
    if isinstance(schema, Mapping):
        for key, value in schema.items():
            child = f"{path}/{key}" if path else f"/{key}"
            if key in BOUND_KEYWORDS and isinstance(value, int) and not isinstance(value, bool):
                yield (child, key, value)
            elif key == "pattern" and isinstance(value, str):
                if _REJECTED_REPETITION_RE.search(value):
                    yield (child, "pattern", REJECTED_GRAMMAR_BOUND)
            else:
                yield from iter_schema_bounds(value, path=child)
    elif isinstance(schema, (list, tuple)):
        for index, value in enumerate(schema):
            yield from iter_schema_bounds(value, path=f"{path}/{index}")


def find_rejected_bounds(
    schema: Mapping[str, Any],
    *,
    rejected: int = REJECTED_GRAMMAR_BOUND,
) -> tuple[str, ...]:
    """Return the paths of every bound equal to the value Ollama cannot compile.

    An empty tuple means the schema carries no known-rejected repetition bound.
    This is a static check: it needs no provider, so it can run in an offline
    suite. Note that ``pattern`` occurrences are only detected for
    :data:`REJECTED_GRAMMAR_BOUND` itself, so passing a different ``rejected``
    value covers the integer keywords only.
    """
    return tuple(
        path for path, _keyword, value in iter_schema_bounds(schema) if value == rejected
    )


def default_probe_client(*, base_url: str | None = None) -> OllamaClient:
    """Build a short-deadline, no-retry client for probing.

    A probe must not retry: a grammar rejection is deterministic, and an
    unreachable endpoint should be reported quickly rather than waited out.
    Callers on a loaded host that need a warm-up budget should build their own
    client with a longer ``generation_timeout`` and pass it in.
    """
    return OllamaClient(
        base_url or os.getenv("JARVIS_OLLAMA_URL", DEFAULT_OLLAMA_URL),
        max_output_tokens=PROBE_MAX_OUTPUT_TOKENS,
        max_retries=0,
        generation_timeout=PROBE_GENERATION_TIMEOUT,
        health_timeout=PROBE_HEALTH_TIMEOUT,
    )


def default_probe_model() -> str:
    model = (os.getenv("JARVIS_FAST_MODEL") or "").strip()
    return model or DEFAULT_PROBE_MODEL


def _provider_version(client: Any) -> tuple[str | None, bool]:
    """Return ``(version, endpoint_answered)`` without deciding the category.

    The second element is evidence, not decoration: an error carrying no HTTP
    status means either "nothing answered" or "something answered and the
    exchange went wrong afterwards", and whether this call reached the endpoint
    a moment earlier is what separates the two.

    The version itself is provider-controlled text that this result is meant to
    carry into evidence, so it is admitted only in the narrow token shape of
    :data:`_VERSION_RE`. An over-long string, or one carrying a path, a host
    name or a newline, is reported as ``None`` while the endpoint still counts
    as having answered.
    """
    reader = getattr(client, "version", None)
    if not callable(reader):
        return (None, False)
    try:
        value = reader()
    except (OllamaError, OSError, ValueError, TypeError):
        return (None, False)
    if not isinstance(value, str):
        return (None, True)
    candidate = value.strip()
    return (candidate if _VERSION_RE.match(candidate) else None, True)


def probe_structured_output(
    *,
    client: Any | None = None,
    model: str | None = None,
    schema: Mapping[str, Any] | None = None,
    base_url: str | None = None,
) -> StructuredOutputProbe:
    """Compile ``schema`` against the provider and classify the outcome.

    ``schema`` defaults to the schema the production TaskContract resolver
    sends. ``client`` defaults to a probe-configured :class:`OllamaClient`;
    callers pass their own to probe a specific configuration - a longer
    deadline on a loaded host, say - and tests pass one built on a fake
    transport so no unit test touches the network.

    The request always carries ``think=False``, because that is what production
    sends and the compiler defect this exists to catch appears only there.

    Callers are responsible for honouring ``JARVIS_OLLAMA_ENABLED``: this
    function always attempts the call it is asked to attempt.
    """
    resolved_schema = deepcopy(dict(schema)) if schema is not None else task_contract_response_schema()
    digest = canonical_schema_sha256(resolved_schema)
    resolved_client = client if client is not None else default_probe_client(base_url=base_url)
    selected_model = (model or "").strip() or default_probe_model()

    version, endpoint_answered = _provider_version(resolved_client)
    started = time.monotonic()

    def result(category: str, reason: str, status_code: int | None) -> StructuredOutputProbe:
        return StructuredOutputProbe(
            category=category,
            reason=reason,
            provider=PROVIDER_OLLAMA,
            model=selected_model,
            provider_version=version,
            status_code=status_code,
            schema_sha256=digest,
            elapsed_seconds=round(max(0.0, time.monotonic() - started), 3),
        )

    try:
        response = resolved_client.chat(
            [{"role": "user", "content": PROBE_PROMPT}],
            [],
            selected_model,
            context_length=PROBE_CONTEXT_LENGTH,
            think=PROBE_THINK,
            temperature=0.0,
            response_format=resolved_schema,
            seed=0,
        )
    except OllamaGrammarRejected as exc:
        return result(
            CATEGORY_GRAMMAR_REJECTED,
            "grammar_compile_rejected",
            getattr(exc, "status_code", None),
        )
    except OllamaError as exc:
        status_code = getattr(exc, "status_code", None)
        if status_code == 404:
            # The endpoint is there; the model it was asked for is not.
            return result(CATEGORY_MODEL_UNAVAILABLE, "model_not_found", status_code)
        if status_code is not None:
            return result(CATEGORY_GENERATION_FAILED, "provider_error", status_code)
        # No HTTP status at all. Either nothing answered - refused connection,
        # DNS failure - or the endpoint answered and the exchange failed
        # afterwards. The version call is the evidence for the first case, and
        # it is the only thing "unreachable" is allowed to mean. A host that
        # answered and then ran out the deadline is slow or loaded, not absent,
        # and is reported as a timeout; anything else came back unusable.
        if not endpoint_answered:
            return result(CATEGORY_MODEL_UNAVAILABLE, "provider_unreachable", None)
        if getattr(exc, "retryable", False):
            return result(CATEGORY_MODEL_UNAVAILABLE, "provider_timeout", None)
        return result(CATEGORY_GENERATION_FAILED, "malformed_response", None)
    except (TypeError, ValueError):
        # A malformed request never reaches the provider. The exception text can
        # quote caller-supplied values, so it is deliberately not bound or kept.
        return result(CATEGORY_GENERATION_FAILED, "invalid_request", None)

    if not isinstance(response, Mapping):
        return result(CATEGORY_GENERATION_FAILED, "malformed_response", None)
    return result(CATEGORY_OK, "compiled", None)


def probe_task_contract_grammar(**kwargs: Any) -> StructuredOutputProbe:
    """Probe with the exact schema the production TaskContract resolver sends.

    Supplying ``schema`` is a caller error rather than a silent no-op: this
    function's whole contract is that the schema is the production one, and
    quietly discarding a caller's schema would make a probe of something else
    look like a probe of production.
    """
    if "schema" in kwargs:
        raise TypeError(
            "probe_task_contract_grammar() always uses the production TaskContract "
            "schema; call probe_structured_output() to probe a different one"
        )
    return probe_structured_output(schema=task_contract_response_schema(), **kwargs)
