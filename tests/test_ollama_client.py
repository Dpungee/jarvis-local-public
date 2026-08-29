import io
import json
import urllib.error
import unittest
from unittest.mock import patch

from jarvis.ollama_client import ChatResponse, OllamaClient, OllamaError, normalize_ollama_url


class FakeResponse:
    def __init__(self, payload=None, *, raw=None, headers=None):
        self.body = raw if raw is not None else json.dumps(payload).encode("utf-8")
        self.headers = {"Content-Length": str(len(self.body))} if headers is None else headers
        self.read_sizes = []

    def read(self, size=-1):
        self.read_sizes.append(size)
        return self.body if size < 0 else self.body[:size]

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False


class SequenceOpen:
    def __init__(self, *items):
        self.items = list(items)
        self.requests = []
        self.timeouts = []

    def __call__(self, request, *, timeout):
        self.requests.append(request)
        self.timeouts.append(timeout)
        item = self.items.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def make_client(open_url, **overrides):
    options = {
        "allow_remote": False,
        "health_timeout": 2,
        "generation_timeout": 9,
        "max_output_tokens": 2048,
        "max_response_bytes": 1024,
        "max_retries": 0,
        "retry_backoff": 0.1,
        "open_url": open_url,
    }
    options.update(overrides)
    return OllamaClient("http://127.0.0.1:11434", **options)


class UrlPolicyTests(unittest.TestCase):
    def test_loopback_is_default_and_localhost_is_canonicalized(self):
        self.assertEqual(
            normalize_ollama_url("http://localhost:11434/"),
            "http://127.0.0.1:11434",
        )
        self.assertEqual(
            normalize_ollama_url("https://[::1]:11434"),
            "https://[::1]:11434",
        )
        with self.assertRaises(ValueError):
            normalize_ollama_url("https://ollama.example")

    def test_trusted_remote_requires_https(self):
        self.assertEqual(
            normalize_ollama_url("https://ollama.example:443", allow_remote=True),
            "https://ollama.example:443",
        )
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            normalize_ollama_url("http://ollama.example:11434", allow_remote=True)

    def test_url_credentials_and_extra_components_are_rejected_without_echoing(self):
        with self.assertRaises(ValueError) as caught:
            normalize_ollama_url(
                "https://jarvis:do-not-leak@ollama.example",
                allow_remote=True,
            )
        self.assertNotIn("do-not-leak", str(caught.exception))
        for value in (
            "https://ollama.example/api",
            "https://ollama.example?token=do-not-leak",
            "file:///tmp/ollama",
        ):
            with self.assertRaises(ValueError):
                normalize_ollama_url(value, allow_remote=True)


class ClientTests(unittest.TestCase):
    def test_health_and_generation_use_different_timeouts_and_retain_metrics(self):
        opener = SequenceOpen(
            FakeResponse({"models": [{"name": "qwen3:8b"}]}),
            FakeResponse(
                {
                    "model": "qwen3:8b",
                    "created_at": "2026-08-11T00:00:00Z",
                    "message": {"role": "assistant", "content": "done"},
                    "done": True,
                    "done_reason": "stop",
                    "prompt_eval_count": 12,
                    "eval_count": 4,
                    "total_duration": 100,
                    "load_duration": 10,
                    "prompt_eval_duration": 20,
                    "eval_duration": 70,
                }
            ),
        )
        client = make_client(opener)

        self.assertEqual(client.models(), ["qwen3:8b"])
        response = client.chat([{"role": "user", "content": "hello"}], [], "qwen3:8b")

        self.assertIsInstance(response, dict)
        self.assertIsInstance(response, ChatResponse)
        self.assertEqual(dict(response), {"role": "assistant", "content": "done"})
        self.assertEqual(response.done_reason, "stop")
        self.assertEqual(response.metrics.prompt_tokens, 12)
        self.assertEqual(response.metrics.completion_tokens, 4)
        self.assertEqual(response.metadata["model"], "qwen3:8b")
        self.assertTrue(response.model_attested)
        self.assertTrue(response.metadata["model_attested"])
        self.assertEqual(len(opener.timeouts), 2)
        for actual, configured in zip(opener.timeouts, (2.0, 9.0), strict=True):
            with self.subTest(actual=actual, configured=configured):
                self.assertGreater(actual, 0)
                self.assertLessEqual(actual, configured)
                self.assertAlmostEqual(actual, configured, delta=0.05)

    def test_chat_model_attestation_fails_closed_without_server_model(self):
        opener = SequenceOpen(FakeResponse({
            "message": {"role": "assistant", "content": "done"},
        }))
        client = make_client(opener)

        response = client.chat([], [], "qwen3:8b")

        self.assertIsNone(response.model)
        self.assertFalse(response.model_attested)
        self.assertFalse(response.metadata["model_attested"])

    def test_chat_payload_honors_explicit_thinking_and_keeps_model_warm(self):
        opener = SequenceOpen(
            FakeResponse({"message": {"role": "assistant", "content": "fast"}}),
            FakeResponse({"message": {"role": "assistant", "content": "reasoned"}}),
        )
        client = make_client(opener, keep_alive="30m")

        client.chat(
            [{"role": "user", "content": "hello"}],
            [],
            "qwen3:8b",
            context_length=8192,
            think=False,
            temperature=0.0,
        )
        client.chat(
            [{"role": "user", "content": "analyze"}],
            [],
            "gpt-oss:20b",
            context_length=65536,
            think="high",
        )

        payloads = [json.loads(request.data) for request in opener.requests]
        self.assertEqual(payloads[0]["think"], False)
        self.assertEqual(payloads[0]["options"]["num_ctx"], 8192)
        self.assertEqual(payloads[0]["options"]["temperature"], 0.0)
        self.assertEqual(payloads[0]["options"]["num_predict"], 2048)
        self.assertEqual(payloads[1]["think"], "high")
        self.assertEqual(payloads[1]["options"]["num_ctx"], 65536)
        self.assertEqual(payloads[1]["options"]["temperature"], 0.2)
        self.assertEqual(payloads[1]["options"]["num_predict"], 2048)
        self.assertEqual([payload["keep_alive"] for payload in payloads], ["30m", "30m"])

    def test_chat_omits_think_when_caller_does_not_select_it(self):
        opener = SequenceOpen(
            FakeResponse({"message": {"role": "assistant", "content": "done"}}),
        )
        client = make_client(opener)

        client.chat([{"role": "user", "content": "hello"}], [], "qwen3:8b")

        payload = json.loads(opener.requests[0].data)
        self.assertNotIn("think", payload)

    def test_chat_can_unload_one_large_request_without_changing_default_residency(self):
        opener = SequenceOpen(
            FakeResponse({"message": {"role": "assistant", "content": "deep"}}),
            FakeResponse({"message": {"role": "assistant", "content": "fast"}}),
        )
        client = make_client(opener, keep_alive="30m")

        client.chat([], [], "qwen3-coder:30b", keep_alive="0")
        client.chat([], [], "qwen3.5:9b")

        payloads = [json.loads(request.data) for request in opener.requests]
        self.assertEqual([payload["keep_alive"] for payload in payloads], ["0", "30m"])

    def test_local_resource_controls_and_preload_are_forwarded(self):
        opener = SequenceOpen(
            FakeResponse({"message": {"role": "assistant", "content": "done"}}),
            FakeResponse({"done": True, "load_duration": 123}),
        )
        client = make_client(opener, keep_alive="45m", num_thread=8)

        client.chat([{"role": "user", "content": "hello"}], [], "qwen3-coder:30b")
        metrics = client.preload("qwen3-coder:30b", context_length=8192)

        chat_payload = json.loads(opener.requests[0].data)
        preload_payload = json.loads(opener.requests[1].data)
        self.assertEqual(chat_payload["keep_alive"], "45m")
        self.assertEqual(chat_payload["options"]["num_thread"], 8)
        self.assertEqual(preload_payload, {
            "model": "qwen3-coder:30b",
            "stream": False,
            "keep_alive": "45m",
            "options": {"num_ctx": 8192, "num_thread": 8},
        })
        self.assertEqual(metrics.load_duration_ns, 123)

    def test_invalid_resource_controls_fail_closed(self):
        opener = SequenceOpen()
        for value in ("", "forever", "-0", "1d"):
            with self.subTest(keep_alive=value), self.assertRaises(ValueError):
                make_client(opener, keep_alive=value)
        for value in (0, 257, "many"):
            with self.subTest(num_thread=value), self.assertRaises(ValueError):
                make_client(opener, num_thread=value)

    def test_model_capabilities_are_cached_and_drive_thinking_support(self):
        opener = SequenceOpen(FakeResponse({
            "capabilities": ["completion", "tools"],
        }))
        client = make_client(opener)

        self.assertFalse(client.supports_thinking("qwen3-coder:30b"))
        self.assertFalse(client.supports_thinking("qwen3-coder:30b"))

        self.assertEqual(len(opener.requests), 1)
        self.assertTrue(opener.requests[0].full_url.endswith("/api/show"))
        self.assertEqual(
            json.loads(opener.requests[0].data),
            {"model": "qwen3-coder:30b"},
        )

    def test_chat_supports_bounded_structured_output_and_seed(self):
        opener = SequenceOpen(FakeResponse({
            "message": {"role": "assistant", "content": '{"ok":true}'},
        }))
        client = make_client(opener)
        schema = {
            "type": "object",
            "properties": {"ok": {"type": "boolean"}},
            "required": ["ok"],
            "additionalProperties": False,
        }
        client.chat(
            [{"role": "user", "content": "return json"}],
            [],
            "qwen3:8b",
            response_format=schema,
            seed=42,
        )
        payload = json.loads(opener.requests[0].data.decode("utf-8"))
        self.assertEqual(payload["format"], schema)
        self.assertEqual(payload["options"]["seed"], 42)

        with self.assertRaises(ValueError):
            client.chat([], [], "qwen3:8b", response_format="xml")
        with self.assertRaises(ValueError):
            client.chat([], [], "qwen3:8b", seed=-1)

    def test_response_body_is_bounded_before_json_parsing(self):
        response = FakeResponse(raw=b"x" * 1025, headers={})
        client = make_client(SequenceOpen(response))

        with self.assertRaisesRegex(OllamaError, "size limit"):
            client.models()

        self.assertEqual(response.read_sizes, [1025])

    def test_transient_network_errors_retry_with_deterministic_backoff(self):
        opener = SequenceOpen(
            urllib.error.URLError(ConnectionResetError()),
            urllib.error.URLError(ConnectionResetError()),
            FakeResponse({"models": []}),
        )
        delays = []
        client = make_client(opener, max_retries=2, sleep=delays.append)

        self.assertEqual(client.models(), [])
        self.assertEqual(len(opener.requests), 3)
        self.assertEqual(delays, [0.1, 0.2])

    def test_retry_backoff_never_exceeds_total_deadline(self):
        opener = SequenceOpen(urllib.error.URLError(ConnectionResetError()))
        delays = []
        client = make_client(
            opener,
            health_timeout=0.1,
            max_retries=1,
            retry_backoff=10,
            sleep=delays.append,
        )
        with patch("jarvis.ollama_client.time.monotonic", side_effect=[0.0, 0.0, 0.09, 0.1]):
            with self.assertRaisesRegex(OllamaError, "total deadline"):
                client.models()
        self.assertEqual(len(opener.requests), 1)
        self.assertAlmostEqual(delays[0], 0.01, places=6)

    def test_transient_http_error_retries(self):
        transient = urllib.error.HTTPError(
            "http://127.0.0.1:11434/api/tags",
            503,
            "busy",
            {},
            io.BytesIO(b"internal detail"),
        )
        opener = SequenceOpen(transient, FakeResponse({"models": []}))
        delays = []
        client = make_client(opener, max_retries=1, sleep=delays.append)

        self.assertEqual(client.models(), [])
        self.assertEqual(delays, [0.1])

    def test_non_transient_http_error_does_not_leak_body_or_retry(self):
        error = urllib.error.HTTPError(
            "http://127.0.0.1:11434/api/tags",
            400,
            "bad request",
            {},
            io.BytesIO(b"api_key=do-not-leak"),
        )
        opener = SequenceOpen(error)
        client = make_client(opener, max_retries=2)

        with self.assertRaises(OllamaError) as caught:
            client.models()

        self.assertEqual(caught.exception.status_code, 400)
        self.assertFalse(caught.exception.retryable)
        self.assertNotIn("do-not-leak", str(caught.exception))
        self.assertEqual(len(opener.requests), 1)

    def test_models_can_use_cache_and_refresh_explicitly(self):
        opener = SequenceOpen(
            FakeResponse({"models": [{"name": "first:latest"}, {"name": "first:latest"}]}),
            FakeResponse({"models": [{"name": "second:latest"}]}),
        )
        client = make_client(opener)

        self.assertEqual(client.models(), ["first:latest"])
        self.assertEqual(client.models(refresh=False), ["first:latest"])
        self.assertEqual(client.refresh_models(), ["second:latest"])
        self.assertEqual(client.cached_models, ("second:latest",))
        self.assertEqual(len(opener.requests), 2)

    def test_malformed_response_does_not_echo_response_body(self):
        client = make_client(SequenceOpen(FakeResponse(raw=b'{"secret":"do-not-leak"')))

        with self.assertRaises(OllamaError) as caught:
            client.models()

        self.assertNotIn("do-not-leak", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
