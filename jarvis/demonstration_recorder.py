from __future__ import annotations

import hashlib
import io
import json
import re
import threading
import time
from dataclasses import dataclass
from typing import Any

from .attachments import MAX_IMAGE_BYTES, ImageAttachment
from .redaction import redact_secrets
from .screen_companion import DEFAULT_EXCLUDED_APPS, ScreenObservation


MAX_DEMONSTRATION_SECONDS = 10 * 60
MAX_DEMONSTRATION_FRAMES = 16
MAX_DEMONSTRATION_TRANSITIONS = 128
_SKILL_NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")

DEMONSTRATION_EXTRACTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["summary", "steps", "uncertainties"],
    "properties": {
        "summary": {"type": "string", "minLength": 1, "maxLength": 800},
        "steps": {
            "type": "array",
            "minItems": 1,
            "maxItems": 24,
            "items": {"type": "string", "minLength": 1, "maxLength": 600},
        },
        "uncertainties": {
            "type": "array",
            "maxItems": 12,
            "items": {"type": "string", "minLength": 1, "maxLength": 400},
        },
    },
}

DEMONSTRATION_SKILL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["name", "description", "what_it_does", "how_to_use", "content"],
    "properties": {
        "name": {"type": "string", "minLength": 1, "maxLength": 63},
        "description": {"type": "string", "minLength": 1, "maxLength": 300},
        "what_it_does": {"type": "string", "minLength": 1, "maxLength": 1200},
        "how_to_use": {"type": "string", "minLength": 1, "maxLength": 2000},
        "content": {"type": "string", "minLength": 1, "maxLength": 24000},
    },
}


@dataclass(frozen=True)
class DemonstrationTransition:
    application: str
    title: str
    observed_at: float
    context_sha256: str
    excluded: bool


@dataclass(frozen=True)
class DemonstrationCapture:
    started_at: float
    stopped_at: float
    frames: tuple[ScreenObservation, ...]
    transitions: tuple[DemonstrationTransition, ...]
    excluded_observations: int

    @property
    def duration_seconds(self) -> float:
        return max(0.0, self.stopped_at - self.started_at)


@dataclass(frozen=True)
class DemonstrationSkillDraft:
    name: str
    description: str
    what_it_does: str
    how_to_use: str
    content: str

    def public(self) -> dict[str, str]:
        return {
            "name": self.name,
            "description": self.description,
            "what_it_does": self.what_it_does,
            "how_to_use": self.how_to_use,
            "content": self.content,
        }


class DemonstrationRecorder:
    """Keep one explicit, bounded active-window demonstration in process memory."""

    def __init__(
        self,
        provider: Any,
        *,
        sample_seconds: float = 2.0,
        max_seconds: int = MAX_DEMONSTRATION_SECONDS,
        max_frames: int = MAX_DEMONSTRATION_FRAMES,
    ) -> None:
        self.provider = provider
        self.sample_seconds = max(0.5, min(float(sample_seconds), 10.0))
        self.max_seconds = max(10, min(int(max_seconds), MAX_DEMONSTRATION_SECONDS))
        self.max_frames = max(2, min(int(max_frames), MAX_DEMONSTRATION_FRAMES))
        self._lock = threading.Lock()
        self._shutdown = threading.Event()
        self._thread: threading.Thread | None = None
        self._recording = False
        self._started_at = 0.0
        self._stopped_at = 0.0
        self._frames: list[ScreenObservation] = []
        self._transitions: list[DemonstrationTransition] = []
        self._excluded_observations = 0
        self._last_context_sha256 = ""
        self._last_image_sha256 = ""
        self._replacement_cursor = 1
        self._excluded_apps: set[str] = set(DEFAULT_EXCLUDED_APPS)
        self._last_error: str | None = None
        self._timed_out = False

    def start(self, *, excluded_apps: set[str]) -> dict[str, Any]:
        if not bool(getattr(self.provider, "available", True)):
            raise RuntimeError("Active-window capture is unavailable on this computer")
        with self._lock:
            if self._recording:
                raise RuntimeError("A demonstration is already being recorded")
            self._recording = True
            self._started_at = time.time()
            self._stopped_at = 0.0
            self._frames = []
            self._transitions = []
            self._excluded_observations = 0
            self._last_context_sha256 = ""
            self._last_image_sha256 = ""
            self._replacement_cursor = 1
            self._excluded_apps = set(DEFAULT_EXCLUDED_APPS) | {
                str(item).strip().casefold() for item in excluded_apps if str(item).strip()
            }
            self._last_error = None
            self._timed_out = False
            self._shutdown.clear()
            self._thread = threading.Thread(
                target=self._run,
                name="jarvis-demonstration-recorder",
                daemon=True,
            )
            self._thread.start()
        # Capture the first visible step immediately instead of waiting for a poll.
        self._sample_once()
        return self.status()

    def _append_frame(self, observation: ScreenObservation) -> None:
        image = observation.image
        if image is None:
            return
        image_sha256 = hashlib.sha256(image.data).hexdigest()
        if image_sha256 == self._last_image_sha256:
            return
        self._last_image_sha256 = image_sha256
        if len(self._frames) < self.max_frames:
            self._frames.append(observation)
            return
        # Preserve the first frame and continuously refresh the remaining slots.
        # Sorting by timestamp on stop produces the correct visible sequence.
        self._frames[self._replacement_cursor] = observation
        self._replacement_cursor += 1
        if self._replacement_cursor >= self.max_frames:
            self._replacement_cursor = 1

    def _sample_once(self) -> None:
        with self._lock:
            if not self._recording:
                return
            excluded_apps = set(self._excluded_apps)
        try:
            observation = self.provider.observe(
                capture_pixels=True,
                excluded_apps=excluded_apps,
            )
        except Exception as exc:
            with self._lock:
                self._last_error = (
                    f"{type(exc).__name__}: {redact_secrets(str(exc))[:300]}"
                )
            return
        if observation is None:
            return
        with self._lock:
            if not self._recording:
                return
            if observation.context_sha256 != self._last_context_sha256:
                self._last_context_sha256 = observation.context_sha256
                if len(self._transitions) < MAX_DEMONSTRATION_TRANSITIONS:
                    self._transitions.append(DemonstrationTransition(
                        application=observation.application,
                        title=("Sensitive window hidden" if observation.excluded else observation.title),
                        observed_at=observation.observed_at,
                        context_sha256=observation.context_sha256,
                        excluded=observation.excluded,
                    ))
            if observation.excluded:
                self._excluded_observations += 1
            else:
                self._append_frame(observation)

    def _run(self) -> None:
        while not self._shutdown.wait(self.sample_seconds):
            with self._lock:
                if not self._recording:
                    return
                if time.time() - self._started_at >= self.max_seconds:
                    self._stopped_at = time.time()
                    self._timed_out = True
                    return
            self._sample_once()

    def stop(self) -> DemonstrationCapture:
        with self._lock:
            if not self._recording and not self._timed_out:
                raise RuntimeError("No demonstration is being recorded")
            self._recording = False
            if not self._stopped_at:
                self._stopped_at = time.time()
            self._shutdown.set()
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(2.0, self.sample_seconds + 1.0))
        with self._lock:
            capture = DemonstrationCapture(
                started_at=self._started_at,
                stopped_at=self._stopped_at,
                frames=tuple(sorted(self._frames, key=lambda item: item.observed_at)),
                transitions=tuple(self._transitions),
                excluded_observations=self._excluded_observations,
            )
            self._frames = []
            self._transitions = []
            self._thread = None
            self._timed_out = False
        return capture

    def cancel(self) -> None:
        with self._lock:
            self._recording = False
            self._shutdown.set()
            thread = self._thread
            self._frames = []
            self._transitions = []
            self._stopped_at = time.time()
            self._timed_out = False
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(2.0, self.sample_seconds + 1.0))
        with self._lock:
            self._thread = None

    def status(self) -> dict[str, Any]:
        with self._lock:
            now = time.time()
            duration = (
                max(0.0, (self._stopped_at or now) - self._started_at)
                if self._recording or self._timed_out
                else max(0.0, self._stopped_at - self._started_at)
            )
            return {
                "recording": self._recording,
                "started_at": self._started_at or None,
                "duration_seconds": round(duration, 1),
                "frame_count": len(self._frames),
                "transition_count": len(self._transitions),
                "excluded_observations": self._excluded_observations,
                "max_seconds": self.max_seconds,
                "raw_screens_persisted": False,
                "last_error": self._last_error,
                "timed_out": self._timed_out,
            }


def demonstration_contact_sheet(
    capture: DemonstrationCapture,
) -> ImageAttachment | None:
    """Combine bounded in-memory keyframes into one in-memory visual timeline."""
    if not capture.frames:
        return None
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return None
    frames = []
    for observation in capture.frames:
        if observation.image is None:
            continue
        try:
            frame = Image.open(io.BytesIO(observation.image.data)).convert("RGB")
            frame.thumbnail((480, 300))
            frames.append((observation, frame.copy()))
        except (OSError, ValueError):
            continue
    if not frames:
        return None
    columns = 2
    cell_width, cell_height = 500, 340
    rows = (len(frames) + columns - 1) // columns
    sheet = Image.new("RGB", (columns * cell_width, rows * cell_height), "#171717")
    draw = ImageDraw.Draw(sheet)
    for index, (observation, frame) in enumerate(frames, 1):
        column = (index - 1) % columns
        row = (index - 1) // columns
        x, y = column * cell_width, row * cell_height
        elapsed = max(0.0, observation.observed_at - capture.started_at)
        draw.text((x + 10, y + 8), f"Step {index} · +{elapsed:.1f}s · {observation.application[:48]}", fill="white")
        sheet.paste(frame, (x + 10, y + 34))
    sheet.thumbnail((1_600, 1_600))
    output = io.BytesIO()
    sheet.save(output, format="JPEG", quality=78, optimize=True)
    data = output.getvalue()
    if len(data) > MAX_IMAGE_BYTES:
        return None
    return ImageAttachment("image/jpeg", data, "demonstration-keyframes.jpg")


def demonstration_timeline_json(capture: DemonstrationCapture) -> str:
    timeline = [
        {
            "step": index,
            "seconds_from_start": round(
                max(0.0, item.observed_at - capture.started_at), 1
            ),
            "application": item.application,
            "window_title": item.title,
            "excluded": item.excluded,
        }
        for index, item in enumerate(capture.transitions, 1)
    ]
    return json.dumps(
        {
            "duration_seconds": round(capture.duration_seconds, 1),
            "captured_keyframes": len(capture.frames),
            "excluded_observations": capture.excluded_observations,
            "timeline": timeline,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def structured_response_object(response: Any, *, label: str) -> dict[str, Any]:
    if not isinstance(response, dict):
        raise ValueError(f"{label} response was not an object")
    content = response.get("content")
    if isinstance(content, dict):
        value = content
    elif isinstance(content, str):
        try:
            value = json.loads(content)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{label} response was not valid JSON") from exc
    else:
        raise ValueError(f"{label} response had no structured content")
    if not isinstance(value, dict):
        raise ValueError(f"{label} response was not a JSON object")
    return value


def _bounded_text(value: Any, label: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be text")
    text = redact_secrets(value).strip()
    if not text:
        raise ValueError(f"{label} cannot be empty")
    if len(text) > maximum:
        raise ValueError(f"{label} exceeds its {maximum}-character limit")
    return text


def normalize_skill_name(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("Skill name must be text")
    raw = redact_secrets(value).strip().casefold()
    normalized = re.sub(r"[^a-z0-9]+", "-", raw).strip("-")[:63].rstrip("-")
    if not normalized or not _SKILL_NAME.fullmatch(normalized):
        raise ValueError(
            "Skill name must use lowercase letters, numbers, and single hyphens"
        )
    return normalized


def validate_extraction(value: dict[str, Any]) -> dict[str, Any]:
    summary = _bounded_text(value.get("summary"), "Demonstration summary", 800)
    raw_steps = value.get("steps")
    raw_uncertainties = value.get("uncertainties")
    if not isinstance(raw_steps, list) or not 1 <= len(raw_steps) <= 24:
        raise ValueError("Demonstration extraction must contain 1 to 24 steps")
    if not isinstance(raw_uncertainties, list) or len(raw_uncertainties) > 12:
        raise ValueError("Demonstration extraction uncertainties are invalid")
    steps = [_bounded_text(item, "Demonstration step", 600) for item in raw_steps]
    uncertainties = [
        _bounded_text(item, "Demonstration uncertainty", 400)
        for item in raw_uncertainties
    ]
    return {"summary": summary, "steps": steps, "uncertainties": uncertainties}


def validate_skill_draft(value: dict[str, Any]) -> DemonstrationSkillDraft:
    return DemonstrationSkillDraft(
        name=normalize_skill_name(value.get("name")),
        description=_bounded_text(value.get("description"), "Skill description", 300),
        what_it_does=_bounded_text(value.get("what_it_does"), "What it does", 1200),
        how_to_use=_bounded_text(value.get("how_to_use"), "How to use it", 2000),
        content=_bounded_text(value.get("content"), "Skill instructions", 24000),
    )
