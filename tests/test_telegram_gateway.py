from __future__ import annotations

import json
import unittest
import urllib.error

from jarvis.gateway.telegram import (
    MAX_TELEGRAM_RESPONSE_BYTES,
    TelegramAdapter,
    _NoRedirectHandler,
)


class FakeResponse:
    def __init__(self, payload: object) -> None:
        self.raw = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self, limit: int = -1) -> bytes:
        return self.raw if limit < 0 else self.raw[:limit]


class TelegramGatewayTests(unittest.TestCase):
    def test_poll_is_bounded_and_advances_only_valid_updates(self):
        calls = []

        def transport(request, *, timeout):
            calls.append((request, timeout))
            return FakeResponse({
                "ok": True,
                "result": [
                    {"update_id": 7, "message": {"from": {"id": 42}, "text": " hello "}},
                    {"update_id": "bad", "message": {}},
                    {"update_id": 8, "message": {"from": {"id": 43}, "text": ""}},
                ],
            })

        adapter = TelegramAdapter("12345:test-token", poll_seconds=3, transport=transport)
        messages = list(adapter.poll_or_listen())
        self.assertEqual(len(messages), 1)
        self.assertEqual((messages[0].sender_id, messages[0].text), ("42", "hello"))
        self.assertEqual(adapter.offset, 9)
        sent = json.loads(calls[0][0].data)
        self.assertEqual(sent["allowed_updates"], ["message"])
        self.assertEqual(calls[0][1], 13)

    def test_default_redirect_policy_refuses_cross_origin_followup(self):
        handler = _NoRedirectHandler()
        self.assertIsNone(handler.redirect_request(
            object(), None, 302, "Found", {}, "https://attacker.example/"
        ))

    def test_transport_failure_and_oversize_are_sanitized(self):
        token = "12345:private-token"

        def failed(*_args, **_kwargs):
            raise urllib.error.URLError(f"request URL contained {token}")

        adapter = TelegramAdapter(token, transport=failed)
        with self.assertRaises(RuntimeError) as caught:
            adapter.poll_or_listen()
        self.assertNotIn(token, str(caught.exception))

        class Oversized:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def read(self, _limit):
                return b"x" * (MAX_TELEGRAM_RESPONSE_BYTES + 1)

        adapter = TelegramAdapter(token, transport=lambda *_args, **_kwargs: Oversized())
        with self.assertRaisesRegex(RuntimeError, "size bound"):
            adapter.poll_or_listen()


if __name__ == "__main__":
    unittest.main()
