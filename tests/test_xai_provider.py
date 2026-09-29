from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from jarvis.model_client import (
    DEFAULT_XAI_MODEL,
    XAI_RESPONSES_URL,
    ModelClient,
    XAIClient,
    build_model_client,
    split_model_reference,
)


class _Response:
    def __init__(self, payload):
        self.body = json.dumps(payload).encode("utf-8")
        self.headers = {"Content-Length": str(len(self.body))}

    def read(self, size=-1):
        return self.body if size < 0 else self.body[:size]

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class _Open:
    def __init__(self, response):
        self.response = response
        self.requests = []

    def __call__(self, request, *, timeout):
        self.requests.append((request, timeout))
        return self.response


def _completed(model=DEFAULT_XAI_MODEL):
    return {
        "status": "completed",
        "model": model,
        "output": [{
            "type": "message",
            "content": [{"type": "output_text", "text": "Grok ready"}],
        }],
        "usage": {"input_tokens": 3, "output_tokens": 2},
    }


class XAIProviderTests(unittest.TestCase):
    def test_xai_reference_is_first_class(self):
        self.assertEqual(
            split_model_reference("xai:grok-4.6"),
            ("xai", "grok-4.6"),
        )

    def test_xai_uses_fixed_official_responses_origin_without_reasoning_leak(self):
        opener = _Open(_Response(_completed()))
        client = XAIClient("xai-test-key-not-real", open_url=opener, max_retries=0)

        response = client.chat(
            [{"role": "user", "content": "hello"}],
            [],
            DEFAULT_XAI_MODEL,
            think="high",
        )

        request = opener.requests[0][0]
        payload = json.loads(request.data)
        self.assertEqual(request.full_url, XAI_RESPONSES_URL)
        self.assertEqual(request.headers["Authorization"], "Bearer xai-test-key-not-real")
        self.assertEqual(payload["model"], DEFAULT_XAI_MODEL)
        self.assertNotIn("reasoning", payload)
        self.assertFalse(payload["store"])
        self.assertEqual(response["content"], "Grok ready")
        self.assertTrue(response.model_attested)

    def test_dispatch_lists_and_uses_xai_without_prefix_leak(self):
        opener = _Open(_Response(_completed()))
        xai = XAIClient("xai-test-key-not-real", open_url=opener, max_retries=0)
        client = ModelClient(None, xai=xai, configured_models=("xai:grok-4.6",))

        self.assertEqual(client.models(), ["xai:grok-4.6"])
        self.assertEqual(client.chat([], [], "xai:grok-4.6")["content"], "Grok ready")
        self.assertEqual(json.loads(opener.requests[0][0].data)["model"], "grok-4.6")
        self.assertTrue(client.provider_status["xai_configured"])

    def test_router_and_cli_treat_xai_as_a_cloud_provider(self):
        from jarvis.cli import _installed_model
        from jarvis.router import ModelRouter, Route

        self.assertTrue(ModelRouter._vision_capable("xai:grok-4.6"))
        self.assertFalse(ModelRouter._vision_capable("xai:not-grok"))
        self.assertTrue(ModelRouter._same_model("xai:grok-4.6", "XAI:grok-4.6"))
        self.assertFalse(ModelRouter._same_model("xai:grok-4.6", "grok-4.6"))
        router = ModelRouter(SimpleNamespace(), ["xai:grok-4.6"])
        self.assertEqual(router._installed_name("xai:grok-4.7"), "xai:grok-4.7")
        # Cloud references stay plain strings; only local routes carry metadata.
        self.assertIs(type(Route("fast", "xai:grok-4.6", "test").model), str)
        self.assertTrue(_installed_model("xai:grok-4.7", ["xai:grok-4.6"]))
        self.assertFalse(_installed_model("xai:grok-4.7", ["openai:gpt-5.6"]))

    def test_close_releases_the_xai_transport(self):
        xai = XAIClient("xai-test-key-not-real", open_url=_Open(_Response(_completed())))
        closed = []
        xai.close = lambda: closed.append(True)
        ModelClient(None, xai=xai).close()
        self.assertEqual(closed, [True])

    def test_factory_requires_both_explicit_switch_and_environment_key(self):
        with tempfile.TemporaryDirectory(prefix="jarvis-xai-factory-") as directory:
            config = SimpleNamespace(
                cloud_enabled=True,
                openai_api_enabled=False,
                xai_api_enabled=True,
                anthropic_api_enabled=False,
                codex_cli_enabled=False,
                claude_cli_enabled=False,
                cloud_generation_timeout=600.0,
                cloud_max_output_tokens=8192,
                cloud_max_response_bytes=8 * 1024 * 1024,
                cloud_max_retries=0,
                cloud_retry_backoff=0.5,
                fast_model="xai:grok-4.6",
                reasoning_model="xai:grok-4.6",
                coding_model="xai:grok-4.6",
                deep_model="xai:grok-4.6",
                background_model="xai:grok-4.6",
                model="auto",
                learning_model=None,
                ollama_enabled=False,
                data_dir=Path(directory),
            )
            with patch.dict(os.environ, {"XAI_API_KEY": "xai-test-not-real"}, clear=True):
                enabled = build_model_client(config)
            self.assertIsNotNone(enabled.xai)

            config.xai_api_enabled = False
            with patch.dict(os.environ, {"XAI_API_KEY": "xai-test-not-real"}, clear=True):
                disabled = build_model_client(config)
            self.assertIsNone(disabled.xai)


if __name__ == "__main__":
    unittest.main()
