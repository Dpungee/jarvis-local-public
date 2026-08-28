from __future__ import annotations

import enum
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Protocol

from .redaction import contains_secret, redact_secrets


class PresenceMode(str, enum.Enum):
    PRIVATE = "private"
    OPERATOR = "operator"
    COMPANION = "companion"
    STUDIO = "studio"


class ContextClass(str, enum.Enum):
    PUBLIC = "public"
    RELATIONSHIP = "relationship"
    SCREEN_SUMMARY = "screen_summary"
    OPERATIONAL = "operational"
    PRIVATE = "private"
    CREDENTIAL = "credential"
    RAW_SCREEN = "raw_screen"


class EmbodimentIntent(str, enum.Enum):
    IDLE = "idle"
    LISTEN = "listen"
    THINK = "think"
    SPEAK = "speak"
    ACKNOWLEDGE = "acknowledge"
    AGREE = "agree"
    DISAGREE = "disagree"
    CURIOUS = "curious"
    CONCERNED = "concerned"
    CELEBRATE = "celebrate"
    POINT = "point"
    REPOSITION = "reposition"
    CHANGE_SCENE = "change_scene"
    CHANGE_LIGHTING = "change_lighting"
    CAPTURE_SELFIE = "capture_selfie"
    MARK_HIGHLIGHT = "mark_highlight"


_MODE_CONTEXTS: dict[PresenceMode, frozenset[ContextClass]] = {
    PresenceMode.PRIVATE: frozenset({
        ContextClass.PUBLIC,
        ContextClass.RELATIONSHIP,
        ContextClass.OPERATIONAL,
        ContextClass.PRIVATE,
    }),
    PresenceMode.OPERATOR: frozenset({
        ContextClass.PUBLIC,
        ContextClass.RELATIONSHIP,
        ContextClass.SCREEN_SUMMARY,
        ContextClass.OPERATIONAL,
        ContextClass.PRIVATE,
    }),
    PresenceMode.COMPANION: frozenset({
        ContextClass.PUBLIC,
        ContextClass.RELATIONSHIP,
        ContextClass.SCREEN_SUMMARY,
    }),
    PresenceMode.STUDIO: frozenset({ContextClass.PUBLIC}),
}

_ALLOWED_PAYLOAD_FIELDS: dict[EmbodimentIntent, frozenset[str]] = {
    EmbodimentIntent.IDLE: frozenset(),
    EmbodimentIntent.LISTEN: frozenset({"speaker"}),
    EmbodimentIntent.THINK: frozenset({"intensity"}),
    EmbodimentIntent.SPEAK: frozenset({"text", "emotion"}),
    EmbodimentIntent.ACKNOWLEDGE: frozenset({"emotion"}),
    EmbodimentIntent.AGREE: frozenset({"emotion"}),
    EmbodimentIntent.DISAGREE: frozenset({"emotion"}),
    EmbodimentIntent.CURIOUS: frozenset({"emotion"}),
    EmbodimentIntent.CONCERNED: frozenset({"emotion"}),
    EmbodimentIntent.CELEBRATE: frozenset({"emotion", "intensity"}),
    EmbodimentIntent.POINT: frozenset({"element_id", "label"}),
    EmbodimentIntent.REPOSITION: frozenset({"zone"}),
    EmbodimentIntent.CHANGE_SCENE: frozenset({"scene"}),
    EmbodimentIntent.CHANGE_LIGHTING: frozenset({"preset"}),
    EmbodimentIntent.CAPTURE_SELFIE: frozenset({"framing"}),
    EmbodimentIntent.MARK_HIGHLIGHT: frozenset({"label"}),
}

_FORBIDDEN_PAYLOAD_FIELDS = frozenset({
    "angles",
    "bones",
    "coordinates",
    "credential",
    "image_bytes",
    "joint",
    "joints",
    "pixels",
    "quaternion",
    "screen_image",
    "token",
})


@dataclass(frozen=True)
class PresenceEvent:
    intent: EmbodimentIntent
    context_class: ContextClass = ContextClass.PUBLIC
    payload: Mapping[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)


@dataclass(frozen=True)
class PresenceReceipt:
    intent: str
    mode: str
    delivered: bool
    created_at: float
    detail: str = ""


class AvatarDriver(Protocol):
    def apply_intent(self, intent: str, payload: Mapping[str, Any]) -> None: ...


def _clean_scalar(value: Any) -> str | float | int | bool | None:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    text = redact_secrets(str(value)).strip()
    if len(text) > 2_000:
        text = text[:2_000]
    return text


def sanitize_event(event: PresenceEvent, mode: PresenceMode) -> PresenceEvent:
    if event.context_class in {ContextClass.CREDENTIAL, ContextClass.RAW_SCREEN}:
        raise PermissionError("credentials and raw screen data cannot enter embodiment")
    if event.context_class not in _MODE_CONTEXTS[mode]:
        raise PermissionError(
            f"{event.context_class.value} context is unavailable in {mode.value} mode"
        )
    allowed = _ALLOWED_PAYLOAD_FIELDS[event.intent]
    supplied = {str(key).casefold() for key in event.payload}
    if supplied & _FORBIDDEN_PAYLOAD_FIELDS:
        raise PermissionError("raw device, credential, or avatar-joint data is forbidden")
    unexpected = supplied - allowed
    if unexpected:
        raise ValueError(f"unsupported embodiment fields: {', '.join(sorted(unexpected))}")
    cleaned: dict[str, Any] = {}
    for raw_key, value in event.payload.items():
        key = str(raw_key).casefold()
        if isinstance(value, (dict, list, tuple, set, bytes, bytearray)):
            raise ValueError("embodiment payloads must contain bounded scalar values")
        if isinstance(value, str) and contains_secret(value):
            raise PermissionError("embodiment payload contains sensitive material")
        cleaned_value = _clean_scalar(value)
        cleaned[key] = cleaned_value
    return PresenceEvent(
        intent=event.intent,
        context_class=event.context_class,
        payload=cleaned,
        created_at=event.created_at,
    )


class EmbodiedPresence:
    """High-level embodiment controller; models never control raw avatar joints."""

    def __init__(
        self,
        driver: AvatarDriver,
        *,
        mode: PresenceMode = PresenceMode.PRIVATE,
        on_receipt: Callable[[PresenceReceipt], None] | None = None,
    ) -> None:
        self.driver = driver
        self._mode = PresenceMode(mode)
        self.on_receipt = on_receipt
        self._lock = threading.RLock()

    @property
    def mode(self) -> PresenceMode:
        with self._lock:
            return self._mode

    def set_mode(self, mode: PresenceMode, *, operator_confirmed: bool = False) -> None:
        selected = PresenceMode(mode)
        if selected is PresenceMode.STUDIO and not operator_confirmed:
            raise PermissionError("Studio Mode requires explicit operator confirmation")
        with self._lock:
            self._mode = selected

    def dispatch(self, event: PresenceEvent) -> PresenceReceipt:
        with self._lock:
            safe = sanitize_event(event, self._mode)
            self.driver.apply_intent(safe.intent.value, dict(safe.payload))
            receipt = PresenceReceipt(
                intent=safe.intent.value,
                mode=self._mode.value,
                delivered=True,
                created_at=time.time(),
            )
        if self.on_receipt is not None:
            self.on_receipt(receipt)
        return receipt


class VoicePresenceLoop:
    """Provider-neutral duplex voice state with deterministic barge-in behavior."""

    def __init__(
        self,
        presence: EmbodiedPresence,
        *,
        cancel_speech: Callable[[], None] | None = None,
    ) -> None:
        self.presence = presence
        self.cancel_speech = cancel_speech
        self._state = "idle"
        self._lock = threading.RLock()

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    def listening(self, speaker: str = "operator") -> PresenceReceipt:
        with self._lock:
            if self._state == "speaking" and self.cancel_speech is not None:
                self.cancel_speech()
            self._state = "listening"
        return self.presence.dispatch(PresenceEvent(
            EmbodimentIntent.LISTEN,
            ContextClass.PUBLIC,
            {"speaker": speaker},
        ))

    def thinking(self) -> PresenceReceipt:
        with self._lock:
            self._state = "thinking"
        return self.presence.dispatch(PresenceEvent(EmbodimentIntent.THINK))

    def speaking(self, text: str, *, emotion: str = "neutral") -> PresenceReceipt:
        with self._lock:
            self._state = "speaking"
        return self.presence.dispatch(PresenceEvent(
            EmbodimentIntent.SPEAK,
            ContextClass.PUBLIC,
            {"text": text, "emotion": emotion},
        ))

    def finish(self) -> PresenceReceipt:
        with self._lock:
            self._state = "idle"
        return self.presence.dispatch(PresenceEvent(EmbodimentIntent.IDLE))
