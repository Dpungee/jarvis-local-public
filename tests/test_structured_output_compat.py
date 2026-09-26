"""Provider structured-output compatibility: the check that would have caught F-A.

Ollama 0.32.15's grammar compiler refuses the repetition upper bound ``2000``
wherever it reaches it - ``maxLength``, ``minLength``, ``maxItems``, or a
``pattern`` carrying a ``{n,2000}`` repetition - anywhere in a schema, at HTTP
400, before generating a token. It is not a size ceiling: 1999 and 2001 both
compile. The failure has a precondition, measured on this host against Ollama
0.32.15 / ``qwen3.5:9b``: it appears only when the request carries
``think: false``, which is exactly what the production resolver sends.

The TaskContract resolver sent that bound for ``goal``, with ``think=False``,
and its caller treats every ``OllamaError`` as a provider outage - so on this
host every TaskContract resolution against a local model failed silently and
the semantic lane never engaged.

Three things are proven here:

1. Raising the *schema* bound to 2001 is behaviour-neutral. The parser
   independently enforces 2000 one layer up, so a 2001-character goal is still
   rejected; the change is permissive only at the grammar layer.
2. A capability probe distinguishes a grammar rejection from an unavailable
   model from an ordinary generation failure, with the provider's version.
3. A grammar rejection raises a named ``OllamaError`` subclass instead of being
   indistinguishable from an outage, without leaking the provider's error body
   and without changing any other error path or the bytes on the wire.

Every unit test here drives a fake transport. The one live test is skipped
unless ``JARVIS_OLLAMA_ENABLED=true`` and the endpoint actually answers.
"""
from __future__ import annotations

import io
import json
import os
import unittest
import urllib.error

from jarvis.ollama_client import (
    GRAMMAR_ERROR_BODY_SCAN_BYTES,
    GRAMMAR_REJECTION_MARKER,
    OllamaClient,
    OllamaError,
    OllamaGrammarRejected,
)
from jarvis.structured_output_compat import (
    CATEGORIES,
    CATEGORY_GENERATION_FAILED,
    CATEGORY_GRAMMAR_REJECTED,
    CATEGORY_MODEL_UNAVAILABLE,
    CATEGORY_OK,
    PROBE_CONTEXT_LENGTH,
    REASONS,
    REJECTED_GRAMMAR_BOUND,
    canonical_schema_sha256,
    find_rejected_bounds,
    iter_schema_bounds,
    probe_structured_output,
    probe_task_contract_grammar,
)
from jarvis.task_contract import (
    TASK_CONTRACT_RESPONSE_SCHEMA,
    TaskContractError,
    parse_task_contract,
    task_contract_response_schema,
)

# --------------------------------------------------------------------------
# Fake transport. No test below this line opens a socket.
# --------------------------------------------------------------------------

GRAMMAR_ERROR_BODY = (
    b'{"error":"{\\"error\\":{\\"code\\":400,\\"message\\":\\"Failed to initialize '
    b'samplers: failed to parse grammar\\",\\"type\\":\\"invalid_request_error\\"}}"}'
)


class FakeResponse:
    def __init__(self, payload):
        self.body = json.dumps(payload).encode("utf-8")
        self.headers = {"Content-Length": str(len(self.body))}

    def read(self, size=-1):
        return self.body if size < 0 else self.body[:size]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class CountingBytes(io.BytesIO):
    """A body that records whether the client read it at all."""

    def __init__(self, data: bytes) -> None:
        super().__init__(data)
        self.reads: list[int] = []

    def read(self, size=-1):
        self.reads.append(size)
        return super().read(size)


class UnreadableBytes(io.BytesIO):
    def read(self, size=-1):
        raise OSError("body stream is gone")


def http_error(status: int, body: bytes, *, counting: bool = False):
    stream = CountingBytes(body) if counting else io.BytesIO(body)
    error = urllib.error.HTTPError(
        "http://127.0.0.1:11434/api/chat", status, "error", {}, stream
    )
    return error, stream


class SequenceOpen:
    def __init__(self, *items):
        self.items = list(items)
        self.requests = []

    def __call__(self, request, *, timeout):
        self.requests.append(request)
        item = self.items.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def make_client(open_url, **overrides) -> OllamaClient:
    options = {
        "allow_remote": False,
        "health_timeout": 2,
        "generation_timeout": 9,
        "max_output_tokens": 2048,
        "max_response_bytes": 65536,
        "max_retries": 0,
        "retry_backoff": 0.1,
        "keep_alive": "30m",
        "open_url": open_url,
    }
    options.update(overrides)
    return OllamaClient("http://127.0.0.1:11434", **options)


VERSION_RESPONSE = {"version": "0.32.15"}
CHAT_RESPONSE = {
    "model": "qwen3.5:9b",
    "model_attested": True,
    "message": {"role": "assistant", "content": "{}"},
}


# --------------------------------------------------------------------------
# 1. The bound is permissive only at the grammar layer.
# --------------------------------------------------------------------------

PARSER_GOAL_LIMIT = 2_000
SCHEMA_GOAL_LIMIT = 2_001


def schema_accepts_goal(schema, goal: str) -> bool:
    """Apply the JSON Schema string constraints for ``goal`` locally."""
    bounds = schema["properties"]["goal"]
    if bounds["type"] != "string" or not isinstance(goal, str):
        return False
    return bounds["minLength"] <= len(goal) <= bounds["maxLength"]


def grounded_goal(length: int) -> tuple[str, list[str]]:
    """A goal of exactly ``length`` characters, quoted by its grounding text."""
    filler = "compare the current battery systems and report the tradeoffs "
    goal = (filler * (length // len(filler) + 1))[:length]
    return goal, [f"Please {goal} today."]


def contract_payload(goal: str) -> dict:
    return {
        "version": 1,
        "relation": "new",
        "lane": "research",
        "artifact_kind": "none",
        "evidence_source": "public_web",
        "requested_effect": "read",
        "goal": goal,
        "target": None,
        "constraint_quotes": [],
        "missing_inputs": [],
        "acceptance": ["sources"],
    }


class SchemaBoundNeutralityTests(unittest.TestCase):
    def test_schema_goal_bound_is_one_character_above_the_parser_limit(self):
        schema = task_contract_response_schema()
        self.assertEqual(schema["properties"]["goal"]["maxLength"], SCHEMA_GOAL_LIMIT)
        self.assertEqual(SCHEMA_GOAL_LIMIT, PARSER_GOAL_LIMIT + 1)

    def test_a_goal_at_the_parser_limit_passes_both_layers(self):
        goal, grounding = grounded_goal(PARSER_GOAL_LIMIT)
        self.assertEqual(len(goal), PARSER_GOAL_LIMIT)
        self.assertTrue(schema_accepts_goal(task_contract_response_schema(), goal))

        contract = parse_task_contract(
            contract_payload(goal), grounding_texts=grounding
        )
        self.assertEqual(contract.goal, goal)

    def test_a_goal_the_schema_now_admits_is_still_rejected_by_the_parser(self):
        goal, grounding = grounded_goal(SCHEMA_GOAL_LIMIT)
        self.assertEqual(len(goal), SCHEMA_GOAL_LIMIT)
        # Permissive at the grammar layer ...
        self.assertTrue(schema_accepts_goal(task_contract_response_schema(), goal))
        # ... and rejected identically one layer up.
        with self.assertRaises(TaskContractError) as caught:
            parse_task_contract(contract_payload(goal), grounding_texts=grounding)
        self.assertIn(f"exceeds {PARSER_GOAL_LIMIT} characters", str(caught.exception))

    def test_the_schema_is_exactly_one_character_more_permissive(self):
        goal, _ = grounded_goal(SCHEMA_GOAL_LIMIT + 1)
        self.assertFalse(schema_accepts_goal(task_contract_response_schema(), goal))

    def test_no_schema_bound_carries_the_value_the_compiler_rejects(self):
        schema = task_contract_response_schema()
        self.assertEqual(find_rejected_bounds(schema), ())
        values = [value for _path, _keyword, value in iter_schema_bounds(schema)]
        self.assertNotIn(REJECTED_GRAMMAR_BOUND, values)
        self.assertTrue(values, "the schema should declare at least one bound")

    def test_the_module_level_constant_carries_the_same_bounds(self):
        self.assertEqual(find_rejected_bounds(TASK_CONTRACT_RESPONSE_SCHEMA), ())
        self.assertEqual(
            canonical_schema_sha256(TASK_CONTRACT_RESPONSE_SCHEMA),
            canonical_schema_sha256(task_contract_response_schema()),
        )

    def test_the_bound_guard_finds_the_other_keywords_that_carry_the_bound(self):
        # Verified live on Ollama 0.32.15 / qwen3.5:9b with think:false: each of
        # these answers HTTP 400 "failed to parse grammar" exactly as maxLength
        # does, so the static guard must report all of them.
        for keyword, container in (
            ("minLength", "goal"),
            ("maxLength", "goal"),
        ):
            with self.subTest(keyword=keyword):
                schema = task_contract_response_schema()
                schema["properties"][container][keyword] = REJECTED_GRAMMAR_BOUND
                self.assertEqual(
                    find_rejected_bounds(schema),
                    (f"/properties/{container}/{keyword}",),
                )
        schema = task_contract_response_schema()
        schema["properties"]["constraint_quotes"]["maxItems"] = REJECTED_GRAMMAR_BOUND
        self.assertEqual(
            find_rejected_bounds(schema), ("/properties/constraint_quotes/maxItems",)
        )

    def test_the_bound_guard_finds_a_rejected_repetition_inside_a_pattern(self):
        # A regex repetition reaches the same compiler: "^.{0,2000}$" is HTTP
        # 400 while "^.{0,1999}$" compiles, both verified live.
        for pattern in ("^.{0,2000}$", "^.{,2000}$", "^a{2000}$", "^.{ 0 , 2000 }$"):
            with self.subTest(pattern=pattern):
                schema = task_contract_response_schema()
                schema["properties"]["goal"]["pattern"] = pattern
                self.assertEqual(
                    find_rejected_bounds(schema), ("/properties/goal/pattern",)
                )
                self.assertIn(
                    ("/properties/goal/pattern", "pattern", REJECTED_GRAMMAR_BOUND),
                    list(iter_schema_bounds(schema)),
                )

    def test_the_pattern_guard_does_not_fire_on_a_safe_repetition(self):
        for pattern in ("^.{0,1999}$", "^.{0,20000}$", "^[a-z][a-z0-9_]{0,39}$"):
            with self.subTest(pattern=pattern):
                schema = task_contract_response_schema()
                schema["properties"]["goal"]["pattern"] = pattern
                self.assertEqual(find_rejected_bounds(schema), ())
        # The shipped schema already carries a pattern; it must stay clean.
        shipped = task_contract_response_schema()
        self.assertEqual(
            shipped["properties"]["missing_inputs"]["items"]["properties"]["key"][
                "pattern"
            ],
            "^[a-z][a-z0-9_]{0,39}$",
        )
        self.assertEqual(find_rejected_bounds(shipped), ())

    def test_the_bound_guard_finds_nested_and_listed_occurrences(self):
        # The defect is position-independent: a nested 2000 fails identically,
        # so the guard must not only inspect top-level properties.
        schema = task_contract_response_schema()
        schema["properties"]["constraint_quotes"]["items"]["maxLength"] = (
            REJECTED_GRAMMAR_BOUND
        )
        schema["properties"]["target"]["anyOf"][1]["maxLength"] = REJECTED_GRAMMAR_BOUND
        found = find_rejected_bounds(schema)
        self.assertEqual(
            set(found),
            {
                "/properties/constraint_quotes/items/maxLength",
                "/properties/target/anyOf/1/maxLength",
            },
        )

    def test_the_returned_schema_is_a_defensive_copy(self):
        first = task_contract_response_schema()
        first["properties"]["goal"]["maxLength"] = REJECTED_GRAMMAR_BOUND
        self.assertEqual(
            task_contract_response_schema()["properties"]["goal"]["maxLength"],
            SCHEMA_GOAL_LIMIT,
        )
        self.assertEqual(
            TASK_CONTRACT_RESPONSE_SCHEMA["properties"]["goal"]["maxLength"],
            SCHEMA_GOAL_LIMIT,
        )


# --------------------------------------------------------------------------
# 2. The named exception.
# --------------------------------------------------------------------------


class GrammarRejectionErrorTests(unittest.TestCase):
    def test_grammar_rejection_raises_the_named_subclass(self):
        error, _ = http_error(400, GRAMMAR_ERROR_BODY)
        client = make_client(SequenceOpen(error))

        with self.assertRaises(OllamaGrammarRejected) as caught:
            client.chat([], [], "qwen3.5:9b", response_format={"type": "object"})

        exception = caught.exception
        self.assertEqual(exception.status_code, 400)
        self.assertFalse(exception.retryable)
        self.assertIn("structured-output grammar", str(exception))

    def test_the_named_subclass_is_still_an_ollama_error(self):
        # jarvis/agent.py catches OllamaError and is pinned; the fallback there
        # must keep working while the failure becomes distinguishable by type.
        self.assertTrue(issubclass(OllamaGrammarRejected, OllamaError))
        error, _ = http_error(400, GRAMMAR_ERROR_BODY)
        client = make_client(SequenceOpen(error))
        with self.assertRaises(OllamaError):
            client.chat([], [], "qwen3.5:9b", response_format={"type": "object"})

    def test_the_provider_error_body_never_reaches_the_message(self):
        body = (
            b'{"error":"api_key=do-not-leak / Failed to initialize samplers: '
            b'failed to parse grammar"}'
        )
        error, _ = http_error(400, body)
        client = make_client(SequenceOpen(error))

        with self.assertRaises(OllamaGrammarRejected) as caught:
            client.chat([], [], "qwen3.5:9b", response_format={"type": "object"})

        rendered = str(caught.exception)
        self.assertNotIn("do-not-leak", rendered)
        self.assertNotIn("api_key", rendered)

    def test_marker_matching_is_case_insensitive(self):
        error, _ = http_error(400, b'{"error":"FAILED TO PARSE GRAMMAR"}')
        client = make_client(SequenceOpen(error))
        with self.assertRaises(OllamaGrammarRejected):
            client.chat([], [], "qwen3.5:9b", response_format={"type": "object"})
        self.assertEqual(GRAMMAR_REJECTION_MARKER, "failed to parse grammar")

    def test_an_unrelated_400_stays_a_generic_error_and_does_not_leak(self):
        error, _ = http_error(400, b'{"error":"api_key=do-not-leak is invalid"}')
        client = make_client(SequenceOpen(error))

        with self.assertRaises(OllamaError) as caught:
            client.chat([], [], "qwen3.5:9b")

        self.assertNotIsInstance(caught.exception, OllamaGrammarRejected)
        self.assertEqual(caught.exception.status_code, 400)
        self.assertFalse(caught.exception.retryable)
        self.assertIn("HTTP 400", str(caught.exception))
        self.assertNotIn("do-not-leak", str(caught.exception))

    def test_a_marker_straddling_the_scan_boundary_is_still_classified(self):
        # The marker starts one byte before the scan window ends, so a read of
        # exactly GRAMMAR_ERROR_BODY_SCAN_BYTES would cut it in half.
        prefix = b"x" * (GRAMMAR_ERROR_BODY_SCAN_BYTES - 1)
        body = prefix + GRAMMAR_REJECTION_MARKER.encode("utf-8") + b"y" * 4096
        error, _ = http_error(400, body)
        client = make_client(SequenceOpen(error), max_response_bytes=1024 * 1024)

        with self.assertRaises(OllamaGrammarRejected):
            client.chat([], [], "qwen3.5:9b", response_format={"type": "object"})

    def test_a_marker_beyond_the_scan_window_is_not_classified(self):
        # The window is deliberately bounded: a marker far past it is missed,
        # and the generic error is raised rather than a body being read whole.
        body = b"x" * (GRAMMAR_ERROR_BODY_SCAN_BYTES * 2) + GRAMMAR_REJECTION_MARKER.encode(
            "utf-8"
        )
        error, _ = http_error(400, body)
        client = make_client(SequenceOpen(error), max_response_bytes=1024 * 1024)

        with self.assertRaises(OllamaError) as caught:
            client.chat([], [], "qwen3.5:9b", response_format={"type": "object"})
        self.assertNotIsInstance(caught.exception, OllamaGrammarRejected)

    def test_a_400_body_that_cannot_be_read_falls_back_to_the_generic_error(self):
        error = urllib.error.HTTPError(
            "http://127.0.0.1:11434/api/chat", 400, "error", {}, UnreadableBytes(b"")
        )
        client = make_client(SequenceOpen(error))

        with self.assertRaises(OllamaError) as caught:
            client.chat([], [], "qwen3.5:9b")

        self.assertNotIsInstance(caught.exception, OllamaGrammarRejected)
        self.assertIn("HTTP 400", str(caught.exception))

    def test_no_body_is_read_for_a_status_other_than_400(self):
        error, stream = http_error(404, GRAMMAR_ERROR_BODY, counting=True)
        client = make_client(SequenceOpen(error))

        with self.assertRaises(OllamaError) as caught:
            client.chat([], [], "missing:model")

        self.assertNotIsInstance(caught.exception, OllamaGrammarRejected)
        self.assertEqual(caught.exception.status_code, 404)
        self.assertEqual(stream.reads, [])

    def test_transient_errors_still_retry_and_are_unclassified(self):
        error, stream = http_error(503, GRAMMAR_ERROR_BODY, counting=True)
        opener = SequenceOpen(error, FakeResponse(CHAT_RESPONSE))
        delays: list[float] = []
        client = make_client(opener, max_retries=1, sleep=delays.append)

        response = client.chat([], [], "qwen3.5:9b")

        self.assertEqual(response.get("content"), "{}")
        self.assertEqual(delays, [0.1])
        self.assertEqual(stream.reads, [])


# --------------------------------------------------------------------------
# 3. The serialized request must not have moved.
# --------------------------------------------------------------------------

# A schema that already compiled before this change - no bound equals 2000.
COMPILING_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["goal", "notes"],
    "properties": {
        "goal": {"type": "string", "minLength": 1, "maxLength": 1999},
        "notes": {
            "type": "array",
            "maxItems": 12,
            "items": {"type": "string", "minLength": 1, "maxLength": 300},
        },
    },
}

# Captured from the WP-0 baseline jarvis/ollama_client.py with the same
# arguments, then reproduced byte-for-byte from the edited client.
BASELINE_REQUEST_BYTES = (
    b'{"model":"qwen3.5:9b","messages":[{"role":"user","content":"compare current '
    b'battery systems"}],"tools":[],"stream":false,"keep_alive":"30m","options":'
    b'{"temperature":0.0,"num_ctx":8192,"num_predict":2048,"seed":0},"think":false,'
    b'"format":{"type":"object","additionalProperties":false,"required":["goal",'
    b'"notes"],"properties":{"goal":{"type":"string","minLength":1,"maxLength":1999},'
    b'"notes":{"type":"array","maxItems":12,"items":{"type":"string","minLength":1,'
    b'"maxLength":300}}}}}'
)


class RequestByteIdentityTests(unittest.TestCase):
    def test_a_schema_that_already_compiled_serializes_identically(self):
        opener = SequenceOpen(FakeResponse(CHAT_RESPONSE))
        client = make_client(opener)

        client.chat(
            [{"role": "user", "content": "compare current battery systems"}],
            [],
            "qwen3.5:9b",
            context_length=8192,
            think=False,
            temperature=0.0,
            response_format=COMPILING_SCHEMA,
            seed=0,
        )

        request = opener.requests[0]
        self.assertEqual(request.data, BASELINE_REQUEST_BYTES)
        self.assertEqual(request.full_url, "http://127.0.0.1:11434/api/chat")
        self.assertEqual(request.method, "POST")

    def test_the_shipped_schema_serializes_without_the_rejected_literal(self):
        opener = SequenceOpen(FakeResponse(CHAT_RESPONSE))
        client = make_client(opener)

        client.chat(
            [{"role": "user", "content": "hello"}],
            [],
            "qwen3.5:9b",
            response_format=task_contract_response_schema(),
        )

        payload = json.loads(opener.requests[0].data)
        self.assertEqual(payload["format"]["properties"]["goal"]["maxLength"], 2001)
        self.assertEqual(find_rejected_bounds(payload["format"]), ())


# --------------------------------------------------------------------------
# 4. The capability probe.
# --------------------------------------------------------------------------


class StructuredOutputProbeTests(unittest.TestCase):
    def test_ok_when_the_provider_compiles_the_schema(self):
        opener = SequenceOpen(
            FakeResponse(VERSION_RESPONSE), FakeResponse(CHAT_RESPONSE)
        )
        result = probe_task_contract_grammar(client=make_client(opener), model="qwen3.5:9b")

        self.assertEqual(result.category, CATEGORY_OK)
        self.assertEqual(result.reason, "compiled")
        self.assertTrue(result.supported)
        self.assertEqual(result.provider_version, "0.32.15")
        self.assertEqual(result.model, "qwen3.5:9b")
        self.assertIsNone(result.status_code)
        self.assertEqual(
            result.schema_sha256,
            canonical_schema_sha256(task_contract_response_schema()),
        )
        # The probe must send the real schema, with no tools and no streaming.
        payload = json.loads(opener.requests[1].data)
        self.assertEqual(payload["format"], task_contract_response_schema())
        self.assertEqual(payload["tools"], [])
        self.assertIs(payload["stream"], False)

    def test_the_probe_sends_think_false_because_the_defect_needs_it(self):
        # Measured live on Ollama 0.32.15 / qwen3.5:9b with goal.maxLength=2000:
        # think:false -> HTTP 400, think absent -> HTTP 200, think:true -> 200.
        # Production resolves contracts with think=False, so a probe that let
        # think drift would report "ok" for a schema production cannot use.
        opener = SequenceOpen(FakeResponse(VERSION_RESPONSE), FakeResponse(CHAT_RESPONSE))
        probe_task_contract_grammar(client=make_client(opener), model="qwen3.5:9b")

        raw = opener.requests[1].data
        self.assertIn(b'"think":false', raw)
        self.assertIs(json.loads(raw)["think"], False)

    def test_the_convenience_probe_refuses_a_caller_supplied_schema(self):
        # Silently dropping it would make a probe of something else look like a
        # probe of the production contract.
        with self.assertRaises(TypeError):
            probe_task_contract_grammar(
                client=make_client(SequenceOpen()),
                model="qwen3.5:9b",
                schema={"type": "object"},
            )

    def test_grammar_rejected_when_the_compiler_refuses_the_schema(self):
        error, _ = http_error(400, GRAMMAR_ERROR_BODY)
        opener = SequenceOpen(FakeResponse(VERSION_RESPONSE), error)

        rejected = task_contract_response_schema()
        rejected["properties"]["goal"]["maxLength"] = REJECTED_GRAMMAR_BOUND
        result = probe_structured_output(
            client=make_client(opener), model="qwen3.5:9b", schema=rejected
        )

        self.assertEqual(result.category, CATEGORY_GRAMMAR_REJECTED)
        self.assertEqual(result.reason, "grammar_compile_rejected")
        self.assertFalse(result.supported)
        self.assertEqual(result.status_code, 400)
        self.assertEqual(result.provider_version, "0.32.15")
        self.assertEqual(result.schema_sha256, canonical_schema_sha256(rejected))

    def test_model_unavailable_when_the_endpoint_refuses_the_connection(self):
        refused = urllib.error.URLError(ConnectionRefusedError(61, "refused"))
        opener = SequenceOpen(refused, refused)
        result = probe_task_contract_grammar(client=make_client(opener), model="qwen3.5:9b")

        self.assertEqual(result.category, CATEGORY_MODEL_UNAVAILABLE)
        self.assertEqual(result.reason, "provider_unreachable")
        self.assertIsNone(result.status_code)
        self.assertIsNone(result.provider_version)

    def test_model_unavailable_when_the_model_is_missing(self):
        error, _ = http_error(404, b'{"error":"model \'ghost:1b\' not found"}')
        opener = SequenceOpen(FakeResponse(VERSION_RESPONSE), error)
        result = probe_task_contract_grammar(client=make_client(opener), model="ghost:1b")

        self.assertEqual(result.category, CATEGORY_MODEL_UNAVAILABLE)
        self.assertEqual(result.reason, "model_not_found")
        self.assertEqual(result.status_code, 404)

    def test_model_unavailable_when_the_request_times_out(self):
        opener = SequenceOpen(TimeoutError("timed out"), TimeoutError("timed out"))
        result = probe_task_contract_grammar(client=make_client(opener), model="qwen3.5:9b")

        self.assertEqual(result.category, CATEGORY_MODEL_UNAVAILABLE)
        self.assertEqual(result.reason, "provider_unreachable")

    def test_generation_failed_on_an_unrelated_provider_error(self):
        error, _ = http_error(500, b'{"error":"internal"}')
        opener = SequenceOpen(FakeResponse(VERSION_RESPONSE), error)
        result = probe_task_contract_grammar(client=make_client(opener), model="qwen3.5:9b")

        self.assertEqual(result.category, CATEGORY_GENERATION_FAILED)
        self.assertEqual(result.reason, "provider_error")
        self.assertEqual(result.status_code, 500)

    def test_generation_failed_when_a_reachable_endpoint_answers_badly(self):
        # The endpoint answered /api/version, so a status-free failure after
        # that is a bad exchange, not an unreachable provider.
        opener = SequenceOpen(
            FakeResponse(VERSION_RESPONSE), FakeResponse({"model": "qwen3.5:9b"})
        )
        result = probe_task_contract_grammar(client=make_client(opener), model="qwen3.5:9b")

        self.assertEqual(result.category, CATEGORY_GENERATION_FAILED)
        self.assertEqual(result.reason, "malformed_response")
        self.assertIsNone(result.status_code)

    def test_a_timeout_on_a_reachable_endpoint_is_reported_as_a_timeout(self):
        # "unreachable" is reserved for an endpoint that never answered. One
        # that answered and then ran out the deadline is slow, not absent.
        opener = SequenceOpen(FakeResponse(VERSION_RESPONSE), TimeoutError("timed out"))
        result = probe_task_contract_grammar(client=make_client(opener), model="qwen3.5:9b")

        self.assertEqual(result.category, CATEGORY_MODEL_UNAVAILABLE)
        self.assertEqual(result.reason, "provider_timeout")
        self.assertIsNone(result.status_code)

    def test_unreachable_is_reserved_for_an_endpoint_that_never_answered(self):
        refused = urllib.error.URLError(ConnectionRefusedError(61, "refused"))
        opener = SequenceOpen(refused, refused)
        result = probe_task_contract_grammar(client=make_client(opener), model="qwen3.5:9b")

        self.assertEqual(result.reason, "provider_unreachable")
        self.assertIsNone(result.provider_version)

    def test_generation_failed_when_the_request_itself_is_invalid(self):
        # A 64 KiB-busting schema is rejected client-side, before any call.
        big = {"type": "object", "note": "n" * (70 * 1024)}
        result = probe_structured_output(
            client=make_client(SequenceOpen(FakeResponse(VERSION_RESPONSE))),
            model="qwen3.5:9b",
            schema=big,
        )
        self.assertEqual(result.category, CATEGORY_GENERATION_FAILED)
        self.assertEqual(result.reason, "invalid_request")
        self.assertIsNone(result.status_code)

    def test_a_missing_version_endpoint_does_not_decide_the_category(self):
        error, _ = http_error(404, b'{"error":"not found"}')
        opener = SequenceOpen(error, FakeResponse(CHAT_RESPONSE))
        result = probe_task_contract_grammar(client=make_client(opener), model="qwen3.5:9b")

        self.assertEqual(result.category, CATEGORY_OK)
        self.assertIsNone(result.provider_version)

    def test_the_result_carries_no_prompt_output_host_or_path(self):
        error, _ = http_error(400, GRAMMAR_ERROR_BODY)
        opener = SequenceOpen(FakeResponse(VERSION_RESPONSE), error)
        result = probe_task_contract_grammar(client=make_client(opener), model="qwen3.5:9b")

        payload = result.to_payload()
        self.assertEqual(
            set(payload),
            {
                "category",
                "reason",
                "provider",
                "model",
                "provider_version",
                "status_code",
                "schema_sha256",
                "elapsed_seconds",
            },
        )
        # ``model`` is the model the caller asked for and is the point of the
        # record; every other field is scanned for leaked content.
        self.assertEqual(payload.pop("model"), "qwen3.5:9b")
        rendered = json.dumps(payload)
        for forbidden in (
            "Reply with the object",
            "127.0.0.1",
            "localhost",
            "http",
            "qwen",
            "grammar\\\"",
            "samplers",
            "api_key",
            "C:\\\\",
            "/Users/",
            "\\\\Users",
        ):
            self.assertNotIn(forbidden, rendered, msg=forbidden)

    def test_a_hostile_provider_version_is_dropped_not_recorded(self):
        # The version is provider-controlled text that this result carries into
        # evidence, so anything outside a narrow token shape is dropped.
        # The path placeholder is one of scripts/check_public_release.py's
        # allowlisted names, so this fixture cannot itself trip the release
        # scanner it exists to protect.
        hostile = [
            "9" * 5000,
            "0.32.15 C:\\Users\\test\\ollama",
            "0.32.15 (host server.example)",
            "0.32.15\nX-Injected: true",
            "../../etc/passwd",
            "0.32.15 <script>",
            "",
            "   ",
        ]
        for value in hostile:
            with self.subTest(version=value[:32]):
                opener = SequenceOpen(
                    FakeResponse({"version": value}), FakeResponse(CHAT_RESPONSE)
                )
                result = probe_task_contract_grammar(
                    client=make_client(opener), model="qwen3.5:9b"
                )
                self.assertIsNone(result.provider_version)
                # The endpoint still answered, so the category is unaffected.
                self.assertEqual(result.category, CATEGORY_OK)
                self.assertNotIn(
                    "Users", json.dumps(result.to_payload()), msg=value[:32]
                )

    def test_ordinary_provider_versions_are_kept(self):
        for value in ("0.32.15", "0.32.15-rc1", "v1.2.3+build.4", "0.1", "1.0.0~beta"):
            with self.subTest(version=value):
                opener = SequenceOpen(
                    FakeResponse({"version": value}), FakeResponse(CHAT_RESPONSE)
                )
                result = probe_task_contract_grammar(
                    client=make_client(opener), model="qwen3.5:9b"
                )
                self.assertEqual(result.provider_version, value)

    def test_a_dropped_version_still_counts_as_an_answering_endpoint(self):
        # Otherwise a hostile version string would relabel a bad exchange as an
        # unreachable provider.
        opener = SequenceOpen(
            FakeResponse({"version": "x" * 5000}), FakeResponse({"model": "qwen3.5:9b"})
        )
        result = probe_task_contract_grammar(client=make_client(opener), model="qwen3.5:9b")

        self.assertIsNone(result.provider_version)
        self.assertEqual(result.category, CATEGORY_GENERATION_FAILED)
        self.assertEqual(result.reason, "malformed_response")

    def test_every_category_and_reason_comes_from_the_closed_vocabularies(self):
        cases = [
            SequenceOpen(FakeResponse(VERSION_RESPONSE), FakeResponse(CHAT_RESPONSE)),
            SequenceOpen(FakeResponse(VERSION_RESPONSE), http_error(400, GRAMMAR_ERROR_BODY)[0]),
            SequenceOpen(FakeResponse(VERSION_RESPONSE), http_error(404, b"{}")[0]),
            SequenceOpen(FakeResponse(VERSION_RESPONSE), http_error(500, b"{}")[0]),
        ]
        for opener in cases:
            result = probe_task_contract_grammar(
                client=make_client(opener), model="qwen3.5:9b"
            )
            with self.subTest(category=result.category):
                self.assertIn(result.category, CATEGORIES)
                self.assertIn(result.reason, REASONS)
                self.assertEqual(result.provider, "ollama")
                self.assertGreaterEqual(result.elapsed_seconds, 0.0)


# --------------------------------------------------------------------------
# 5. The live probe. Skipped unless Ollama is enabled and answering.
# --------------------------------------------------------------------------


#: A loaded host can spend well over a minute loading a 9B model cold - 82.8 s
#: was observed once in four runs at the probe's own 120 s default, which turned
#: a healthy-but-slow provider into a red suite. The live client gets a generous
#: deadline instead (``OllamaClient`` bounds it at 3600), the model is warmed
#: once for the class, and an unavailable provider skips rather than fails.
LIVE_GENERATION_TIMEOUT = 600


def _ollama_enabled() -> bool:
    raw = (os.getenv("JARVIS_OLLAMA_ENABLED") or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _live_client_and_model():
    """Return a live client and an installed model, or ``None`` when unusable."""
    if not _ollama_enabled():
        return None
    try:
        client = OllamaClient(
            os.getenv("JARVIS_OLLAMA_URL", "http://127.0.0.1:11434"),
            health_timeout=5,
            generation_timeout=LIVE_GENERATION_TIMEOUT,
            max_output_tokens=128,
            max_retries=0,
        )
        installed = set(client.models())
    except (OllamaError, OSError, ValueError):
        return None
    preferred = (os.getenv("JARVIS_FAST_MODEL") or "qwen3.5:9b").strip()
    if preferred not in installed:
        return None
    return client, preferred


class LiveStructuredOutputProbeTests(unittest.TestCase):
    client: OllamaClient | None = None
    model: str = ""

    @classmethod
    def setUpClass(cls):
        live = _live_client_and_model()
        if live is None:
            raise unittest.SkipTest(
                "live probe needs JARVIS_OLLAMA_ENABLED=true, a reachable endpoint, "
                "and the configured model installed"
            )
        cls.client, cls.model = live
        # Pay the cold-load cost once, here, so a slow first load cannot be
        # mistaken for a compatibility verdict inside a probe.
        try:
            cls.client.preload(cls.model, context_length=PROBE_CONTEXT_LENGTH)
        except (OllamaError, OSError, ValueError) as exc:
            raise unittest.SkipTest(
                f"the configured model would not load: {type(exc).__name__}"
            ) from None

    def probe_or_skip(self, **kwargs):
        """Probe live; an unavailable provider skips instead of failing.

        Only ``model_unavailable`` skips. ``generation_failed`` is a real
        anomaly and still fails, and the ok-versus-grammar_rejected verdict -
        the thing this class exists to check - is always asserted.
        """
        result = probe_structured_output(client=self.client, model=self.model, **kwargs)
        if result.category == CATEGORY_MODEL_UNAVAILABLE:
            self.skipTest(
                f"provider was not available for this run ({result.reason}); "
                "the compatibility verdict is unknown, not failed"
            )
        return result

    def test_the_shipped_schema_compiles_and_the_rejected_bound_does_not(self):
        ok = self.probe_or_skip()
        self.assertEqual(ok.category, CATEGORY_OK, msg=ok.to_payload())
        self.assertEqual(ok.model, self.model)
        # The version screen must admit the real provider's real version; a
        # screen so narrow that it drops it would be silently useless.
        self.assertIsNotNone(ok.provider_version, msg=ok.to_payload())

        rejected = task_contract_response_schema()
        rejected["properties"]["goal"]["maxLength"] = REJECTED_GRAMMAR_BOUND
        bad = self.probe_or_skip(schema=rejected)
        self.assertEqual(bad.category, CATEGORY_GRAMMAR_REJECTED, msg=bad.to_payload())
        self.assertEqual(bad.status_code, 400)
        self.assertNotEqual(ok.schema_sha256, bad.schema_sha256)

    def test_a_nested_occurrence_of_the_rejected_bound_fails_the_same_way(self):
        nested = task_contract_response_schema()
        nested["properties"]["constraint_quotes"]["items"]["maxLength"] = (
            REJECTED_GRAMMAR_BOUND
        )
        self.assertEqual(len(find_rejected_bounds(nested)), 1)
        result = self.probe_or_skip(schema=nested)
        self.assertEqual(
            result.category, CATEGORY_GRAMMAR_REJECTED, msg=result.to_payload()
        )

    def test_the_other_keywords_that_carry_the_bound_fail_the_same_way(self):
        # The guard in find_rejected_bounds claims minLength, maxItems and a
        # pattern repetition trip the same compiler. Check the claim live.
        cases = {}
        with_min = task_contract_response_schema()
        with_min["properties"]["goal"]["minLength"] = REJECTED_GRAMMAR_BOUND
        with_min["properties"]["goal"]["maxLength"] = 8000
        cases["minLength"] = with_min

        with_items = task_contract_response_schema()
        with_items["properties"]["constraint_quotes"]["maxItems"] = (
            REJECTED_GRAMMAR_BOUND
        )
        cases["maxItems"] = with_items

        with_pattern = task_contract_response_schema()
        with_pattern["properties"]["goal"].pop("maxLength", None)
        with_pattern["properties"]["goal"]["pattern"] = "^.{0,2000}$"
        cases["pattern"] = with_pattern

        for keyword, schema in cases.items():
            with self.subTest(keyword=keyword):
                self.assertTrue(find_rejected_bounds(schema))
                result = self.probe_or_skip(schema=schema)
                self.assertEqual(
                    result.category,
                    CATEGORY_GRAMMAR_REJECTED,
                    msg=result.to_payload(),
                )


if __name__ == "__main__":
    unittest.main()
