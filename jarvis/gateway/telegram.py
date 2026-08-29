from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Iterable

from .base import InboundMessage


MAX_TELEGRAM_RESPONSE_BYTES = 2_000_000


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Keep the bot-token URL bound to Telegram's configured API origin."""

    def redirect_request(
        self, req: Any, fp: Any, code: int, msg: str,
        headers: Any, newurl: str,
    ) -> None:
        return None


class TelegramAdapter:
    channel = "telegram"

    def __init__(
        self,
        token: str,
        *,
        offset: int = 0,
        poll_seconds: int = 20,
        transport: Any | None = None,
    ) -> None:
        if not token or len(token) > 4096 or any(ord(char) < 32 for char in token):
            raise ValueError("Telegram token is invalid")
        self._url = f"https://api.telegram.org/bot{token}/"
        self.offset = max(0, int(offset))
        self.poll_seconds = max(1, min(int(poll_seconds), 30))
        self._transport = transport or urllib.request.build_opener(
            _NoRedirectHandler()
        ).open

    def _call(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        request = urllib.request.Request(
            self._url + method,
            data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
            headers={"Content-Type": "application/json", "User-Agent": "jarvis-private-gateway/1"},
            method="POST",
        )
        try:
            with self._transport(request, timeout=self.poll_seconds + 10) as response:
                raw = response.read(MAX_TELEGRAM_RESPONSE_BYTES + 1)
        except (OSError, urllib.error.URLError) as exc:
            raise RuntimeError("Telegram gateway request failed") from exc
        if len(raw) > MAX_TELEGRAM_RESPONSE_BYTES:
            raise RuntimeError("Telegram gateway response exceeded its size bound")
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("Telegram gateway returned malformed data") from exc
        if not isinstance(parsed, dict) or parsed.get("ok") is not True:
            raise RuntimeError("Telegram gateway rejected the request")
        return parsed

    def poll_or_listen(self) -> Iterable[InboundMessage]:
        payload = self._call(
            "getUpdates",
            {
                "offset": self.offset,
                "timeout": self.poll_seconds,
                "allowed_updates": ["message"],
            },
        )
        results = payload.get("result", [])
        if not isinstance(results, list):
            raise RuntimeError("Telegram gateway returned an invalid update list")
        messages: list[InboundMessage] = []
        for item in results[:100]:
            if not isinstance(item, dict) or not isinstance(item.get("update_id"), int):
                continue
            update_id = int(item["update_id"])
            self.offset = max(self.offset, update_id + 1)
            message = item.get("message")
            if not isinstance(message, dict):
                continue
            sender = message.get("from")
            text = message.get("text")
            if not isinstance(sender, dict) or not isinstance(sender.get("id"), int):
                continue
            if not isinstance(text, str) or not text.strip() or len(text) > 20_000:
                continue
            messages.append(InboundMessage(
                sender_id=str(sender["id"]),
                text=text.strip(),
                message_id=str(update_id),
            ))
        return messages

    def send(self, sender_id: str, text: str) -> None:
        if not sender_id or len(sender_id) > 128:
            raise ValueError("Telegram recipient is invalid")
        if not text or len(text) > 4096:
            raise ValueError("Telegram messages must contain 1-4096 characters")
        self._call("sendMessage", {"chat_id": sender_id, "text": text})
