from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Iterable


GMAIL_SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"
CALENDAR_EVENTS_SCOPE = "https://www.googleapis.com/auth/calendar.events"
_EMAIL = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
MAX_RECIPIENTS = 50
MAX_MESSAGE_CHARS = 100_000


def _email(value: str) -> str:
    normalized = str(value).strip().casefold()
    if len(normalized) > 254 or not _EMAIL.fullmatch(normalized):
        raise ValueError("Email address is invalid")
    return normalized


def _text(value: Any, *, label: str, limit: int) -> str:
    result = str(value).replace("\x00", "").strip()
    if not result or len(result) > limit:
        raise ValueError(f"{label} must contain 1-{limit} characters")
    return result


@dataclass(frozen=True)
class EmailDraft:
    to: tuple[str, ...]
    subject: str
    body: str

    @classmethod
    def prepare(cls, to: Iterable[str], subject: str, body: str) -> "EmailDraft":
        recipients = tuple(dict.fromkeys(_email(item) for item in to))
        if not recipients or len(recipients) > MAX_RECIPIENTS:
            raise ValueError(f"Email requires 1-{MAX_RECIPIENTS} unique recipients")
        return cls(
            recipients,
            _text(subject, label="Email subject", limit=998),
            _text(body, label="Email body", limit=MAX_MESSAGE_CHARS),
        )

    def review_manifest(self) -> dict[str, Any]:
        return {
            "kind": "gmail_draft",
            "external_mutation": False,
            "execution_requires_approval": True,
            **asdict(self),
        }


@dataclass(frozen=True)
class CalendarEventDraft:
    title: str
    start: str
    end: str
    attendees: tuple[str, ...] = ()
    description: str = ""

    @classmethod
    def prepare(
        cls, title: str, start: str, end: str, *, attendees: Iterable[str] = (),
        description: str = "",
    ) -> "CalendarEventDraft":
        try:
            start_value = datetime.fromisoformat(str(start).replace("Z", "+00:00"))
            end_value = datetime.fromisoformat(str(end).replace("Z", "+00:00"))
        except ValueError:
            raise ValueError("Calendar start and end must be ISO-8601 timestamps") from None
        if start_value.tzinfo is None or end_value.tzinfo is None:
            raise ValueError("Calendar timestamps must include a timezone")
        if end_value <= start_value:
            raise ValueError("Calendar event must end after it starts")
        guests = tuple(dict.fromkeys(_email(item) for item in attendees))
        if len(guests) > MAX_RECIPIENTS:
            raise ValueError(f"Calendar event supports at most {MAX_RECIPIENTS} attendees")
        clean_description = str(description).replace("\x00", "").strip()
        if len(clean_description) > MAX_MESSAGE_CHARS:
            raise ValueError(f"Calendar description exceeds {MAX_MESSAGE_CHARS} characters")
        return cls(
            _text(title, label="Calendar title", limit=1_000),
            start_value.isoformat(), end_value.isoformat(), guests, clean_description,
        )

    def review_manifest(self) -> dict[str, Any]:
        return {
            "kind": "calendar_event_draft",
            "external_mutation": False,
            "execution_requires_approval": True,
            **asdict(self),
        }


def google_workspace_readiness(
    *, gmail_connected: bool, calendar_connected: bool, drive_status: dict[str, Any] | None
) -> dict[str, Any]:
    """Return capability readiness without reading or exposing credentials."""
    drive = dict(drive_status or {})
    drive_ready = bool(drive.get("authenticated"))
    return {
        "gmail": {
            "connected": bool(gmail_connected),
            "required_scope": GMAIL_SEND_SCOPE,
            "safe_without_auth": ("prepare_email_draft",),
            "requires_approval": ("send_email",),
        },
        "calendar": {
            "connected": bool(calendar_connected),
            "required_scope": CALENDAR_EVENTS_SCOPE,
            "safe_without_auth": ("prepare_calendar_event",),
            "requires_approval": ("create_event", "update_event", "delete_event"),
        },
        "drive": {
            "connected": drive_ready,
            "access_mode": drive.get("access_mode", "not_configured"),
            "safe_without_auth": (),
            "requires_approval": ("upload", "move", "rename", "trash"),
        },
        "all_connected": bool(gmail_connected and calendar_connected and drive_ready),
    }
