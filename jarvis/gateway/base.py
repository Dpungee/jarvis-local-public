from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Protocol


@dataclass(frozen=True)
class InboundMessage:
    sender_id: str
    text: str
    message_id: str
    attachments: tuple[str, ...] = ()


class ChannelAdapter(Protocol):
    channel: str

    def poll_or_listen(self) -> Iterable[InboundMessage]: ...

    def send(self, sender_id: str, text: str) -> None: ...
