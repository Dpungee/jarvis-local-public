from __future__ import annotations

from .embodied_presence import (
    ContextClass,
    EmbodiedPresence,
    EmbodimentIntent,
    PresenceEvent,
    PresenceReceipt,
)
from .redaction import contains_secret, redact_secrets
from .screen_companion import ScreenObservation


class ScreenPresenceBridge:
    """Turns an approved scene summary into a reaction without forwarding pixels."""

    def __init__(self, presence: EmbodiedPresence) -> None:
        self.presence = presence

    def speak_summary(
        self,
        observation: ScreenObservation,
        summary: str,
        *,
        emotion: str = "helpful",
    ) -> PresenceReceipt:
        if observation.excluded:
            raise PermissionError("excluded windows cannot drive embodied reactions")
        raw_summary = str(summary).strip()
        if not raw_summary or len(raw_summary) > 2_000:
            raise ValueError("screen summary must be between 1 and 2000 characters")
        if contains_secret(raw_summary):
            raise PermissionError("screen summary contains sensitive material")
        safe_summary = redact_secrets(raw_summary)
        return self.presence.dispatch(PresenceEvent(
            intent=EmbodimentIntent.SPEAK,
            context_class=ContextClass.SCREEN_SUMMARY,
            payload={"text": safe_summary, "emotion": emotion},
        ))

