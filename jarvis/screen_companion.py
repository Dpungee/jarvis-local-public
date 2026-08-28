from __future__ import annotations

import ctypes
import hashlib
import io
import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .attachments import ImageAttachment, MAX_IMAGE_BYTES
from .memory import Memory
from .redaction import contains_secret, redact_secrets


COMPANION_INDICATOR_TITLE = "JARVIS Companion Controls"
COMPANION_SUGGESTION_TTL_SECONDS = 120.0
DEFAULT_EXCLUDED_APPS = frozenset({
    "1password.exe",
    "authy.exe",
    "bitwarden.exe",
    "credentialui.exe",
    "dashlane.exe",
    "keepass.exe",
    "keepassxc.exe",
    "lastpass.exe",
})
_SENSITIVE_TITLE = re.compile(
    r"\b(?:bank(?:ing)?|credential|credit\s*card|incognito|inprivate|"
    r"login|pass(?:word|phrase)|private\s+browsing|recovery\s+phrase|"
    r"seed\s+phrase|sign[ -]?in|wallet)\b",
    re.I,
)


@dataclass(frozen=True)
class ScreenObservation:
    application: str
    title: str
    observed_at: float
    context_sha256: str
    image: ImageAttachment | None = None
    excluded: bool = False
    exclusion_reason: str | None = None

    def public(self) -> dict[str, Any]:
        return {
            "application": self.application,
            "title": self.title,
            "observed_at": self.observed_at,
            "context_sha256": self.context_sha256,
            "screen_available": self.image is not None,
            "excluded": self.excluded,
            "exclusion_reason": self.exclusion_reason,
        }


class WindowsForegroundProvider:
    """Read only the active window and keep captured pixels in process memory."""

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

    def __init__(self) -> None:
        self.available = os.name == "nt"
        self._user32: Any | None = None
        self._kernel32: Any | None = None
        if not self.available:
            return
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._user32.GetForegroundWindow.restype = ctypes.c_void_p
        self._user32.GetWindowTextLengthW.argtypes = [ctypes.c_void_p]
        self._user32.GetWindowTextLengthW.restype = ctypes.c_int
        self._user32.GetWindowTextW.argtypes = [
            ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int,
        ]
        self._user32.GetWindowTextW.restype = ctypes.c_int
        self._user32.GetWindowThreadProcessId.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32),
        ]
        self._user32.GetWindowThreadProcessId.restype = ctypes.c_uint32
        self._user32.GetWindowRect.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(ctypes.c_long * 4),
        ]
        self._user32.GetWindowRect.restype = ctypes.c_int
        self._kernel32.OpenProcess.argtypes = [
            ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32,
        ]
        self._kernel32.OpenProcess.restype = ctypes.c_void_p
        self._kernel32.QueryFullProcessImageNameW.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_wchar_p,
            ctypes.POINTER(ctypes.c_uint32),
        ]
        self._kernel32.QueryFullProcessImageNameW.restype = ctypes.c_int
        self._kernel32.CloseHandle.argtypes = [ctypes.c_void_p]

    def _process_name(self, pid: int) -> str:
        if self._kernel32 is None or pid <= 0:
            return "unknown"
        handle = self._kernel32.OpenProcess(
            self.PROCESS_QUERY_LIMITED_INFORMATION, 0, int(pid)
        )
        if not handle:
            return "unknown"
        try:
            buffer = ctypes.create_unicode_buffer(32_768)
            size = ctypes.c_uint32(len(buffer))
            if not self._kernel32.QueryFullProcessImageNameW(
                handle, 0, buffer, ctypes.byref(size)
            ):
                return "unknown"
            return Path(buffer.value).name.casefold()[:120] or "unknown"
        finally:
            self._kernel32.CloseHandle(handle)

    def _window_identity(self) -> tuple[int, str, str] | None:
        if self._user32 is None:
            return None
        handle = int(self._user32.GetForegroundWindow() or 0)
        if not handle:
            return None
        length = max(0, min(int(self._user32.GetWindowTextLengthW(handle)), 4_096))
        buffer = ctypes.create_unicode_buffer(length + 1)
        self._user32.GetWindowTextW(handle, buffer, len(buffer))
        pid = ctypes.c_uint32()
        self._user32.GetWindowThreadProcessId(handle, ctypes.byref(pid))
        title = redact_secrets(buffer.value.strip())[:500]
        return handle, self._process_name(int(pid.value)), title

    def active_context(self, *, excluded_apps: set[str]) -> dict[str, Any] | None:
        identity = self._window_identity()
        if identity is None:
            return None
        handle, application, title = identity
        rect = (ctypes.c_long * 4)()
        if self._user32 is None or not self._user32.GetWindowRect(
            ctypes.c_void_p(handle), ctypes.byref(rect)
        ):
            return None
        left, top, right, bottom = (int(value) for value in rect)
        if right <= left or bottom <= top:
            return None
        excluded = application in DEFAULT_EXCLUDED_APPS or application in excluded_apps
        reason = "application is excluded" if excluded else None
        if not excluded and title == COMPANION_INDICATOR_TITLE:
            excluded = True
            reason = "Jarvis Companion indicator is excluded"
        if not excluded and (_SENSITIVE_TITLE.search(title) or contains_secret(title)):
            excluded = True
            reason = "window appears sensitive"
        safe_title = "Sensitive window hidden" if excluded else title
        canonical = (
            f"{handle}\0{application}\0{safe_title}\0"
            f"{left},{top},{right},{bottom}"
        )
        return {
            "handle": handle,
            "application": application,
            "title": safe_title,
            "left": left,
            "top": top,
            "right": right,
            "bottom": bottom,
            "width": right - left,
            "height": bottom - top,
            "context_sha256": hashlib.sha256(
                canonical.encode("utf-8", errors="replace")
            ).hexdigest(),
            "excluded": excluded,
            "exclusion_reason": reason,
        }

    @staticmethod
    def _capture_window(handle: int) -> ImageAttachment | None:
        try:
            from PIL import ImageGrab
        except ImportError:
            return None
        try:
            # A desktop rectangle can include notifications or another window
            # layered over the target.  HWND-targeted capture fails closed when
            # unsupported rather than silently grabbing composite desktop pixels.
            image = ImageGrab.grab(window=handle).convert("RGB")
            # Some accelerated/protected windows return a technically valid PNG
            # whose pixels are uniformly black.  Treat that as capture failure so
            # Companion never offers to act on visual context it cannot actually see.
            extrema = image.getextrema()
            if extrema and all(int(high) <= 8 for _low, high in extrema):
                return None
            if image.width * image.height > 40_000_000:
                return None
            image.thumbnail((1_600, 1_200))
            output = io.BytesIO()
            image.save(output, format="PNG", optimize=True)
            data = output.getvalue()
            if len(data) > MAX_IMAGE_BYTES:
                image.thumbnail((1_024, 768))
                output = io.BytesIO()
                image.save(output, format="PNG", optimize=True)
                data = output.getvalue()
            if len(data) > MAX_IMAGE_BYTES:
                return None
            return ImageAttachment("image/png", data, "active-window.png")
        except (AttributeError, OSError, TypeError, ValueError):
            return None

    def observe(
        self,
        *,
        capture_pixels: bool,
        excluded_apps: set[str],
    ) -> ScreenObservation | None:
        context = self.active_context(excluded_apps=excluded_apps)
        if context is None:
            return None
        image = (
            self._capture_window(int(context["handle"]))
            if capture_pixels and not context["excluded"]
            else None
        )
        if image is not None:
            # The foreground may change while Windows is capturing.  Never retain
            # pixels unless the same non-sensitive HWND/context is still active.
            after = self.active_context(excluded_apps=excluded_apps)
            if (
                after is None
                or bool(after["excluded"])
                or int(after["handle"]) != int(context["handle"])
                or str(after["context_sha256"]) != str(context["context_sha256"])
            ):
                image = None
        return ScreenObservation(
            application=str(context["application"]),
            title=str(context["title"]),
            observed_at=time.time(),
            context_sha256=str(context["context_sha256"]),
            image=image,
            excluded=bool(context["excluded"]),
            exclusion_reason=context["exclusion_reason"],
        )


class ScreenCompanion:
    """Event-driven, opt-in foreground observation and bounded automation triggers."""

    def __init__(
        self,
        database_path: Path,
        *,
        provider: Any | None = None,
        on_action: Callable[[dict[str, Any], ScreenObservation], str | None] | None = None,
        poll_seconds: float = 2.0,
        stable_seconds: float = 8.0,
        automatic_cooldown_seconds: int = 300,
    ) -> None:
        self.database_path = Path(database_path)
        self.provider = provider or WindowsForegroundProvider()
        self.on_action = on_action
        self.poll_seconds = max(0.25, min(float(poll_seconds), 30.0))
        self.stable_seconds = max(0.0, min(float(stable_seconds), 300.0))
        self.automatic_cooldown_seconds = max(
            30, min(int(automatic_cooldown_seconds), 86_400)
        )
        self._shutdown = threading.Event()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._current: ScreenObservation | None = None
        self._stable_since = 0.0
        self._last_auto_digest: str | None = None
        self._last_auto_at = 0.0
        self._last_error: str | None = None
        self._suggestions_today = 0
        self._suggestion_day = time.strftime("%Y-%m-%d")

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._shutdown.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="jarvis-screen-companion",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._shutdown.set()
        if self._thread is not None:
            self._thread.join(timeout=10)

    def forget(self) -> int:
        with self._lock:
            self._current = None
            self._stable_since = 0.0
            self._last_auto_digest = None
            self._last_error = None
        with Memory(self.database_path) as memory:
            return memory.forget_screen_companion_receipts()

    def clear_current(self) -> None:
        """Immediately drop the current observation when paused or disabled."""
        with self._lock:
            self._current = None
            self._stable_since = 0.0
            self._last_auto_digest = None

    def status(self) -> dict[str, Any]:
        with Memory(self.database_path) as memory:
            state = memory.screen_companion_state()
            rules = memory.list_screen_companion_rules()
            learning = memory.screen_companion_learning_stats()
        with self._lock:
            current = None if self._current is None else self._current.public()
            last_error = self._last_error
        return {
            **state,
            "available": bool(getattr(self.provider, "available", True)),
            "current": current,
            "rules": rules,
            "learning": learning,
            "last_error": last_error,
            "raw_screens_persisted": False,
        }

    @staticmethod
    def _matches(rule: dict[str, Any], observation: ScreenObservation) -> bool:
        if not bool(rule.get("enabled")):
            return False
        if str(rule.get("trigger_app") or "").casefold() != observation.application:
            return False
        title_contains = str(rule.get("title_contains") or "").casefold()
        return not title_contains or title_contains in observation.title.casefold()

    def _dispatch(
        self,
        memory: Memory,
        rule: dict[str, Any],
        observation: ScreenObservation,
        *,
        excluded_apps: set[str],
    ) -> None:
        receipt_id: int | None = None
        if rule.get("id") is not None:
            receipt_id = memory.claim_screen_companion_rule(
                int(rule["id"]),
                application=observation.application,
                context_sha256=observation.context_sha256,
            )
            if receipt_id is None:
                return
        try:
            # Foreground polling is metadata-only. Capture pixels once, and only
            # after a suggestion/routine has actually cleared its cooldown gate.
            dispatch_observation = observation
            captured = self.provider.observe(
                capture_pixels=True,
                excluded_apps=excluded_apps,
            )
            if (
                captured is not None
                and not captured.excluded
                and captured.context_sha256 == observation.context_sha256
            ):
                dispatch_observation = captured
            if (
                str(rule.get("source") or "").strip().casefold() == "auto"
                and dispatch_observation.image is None
            ):
                with self._lock:
                    self._last_error = (
                        "Active-window capture was unavailable; the automatic "
                        "suggestion was skipped"
                    )
                return
            job_id = (
                self.on_action(rule, dispatch_observation)
                if self.on_action else None
            )
            status = "queued" if job_id else "suggested"
            if receipt_id is not None:
                memory.finish_screen_companion_receipt(
                    receipt_id, status=status, job_id=job_id
                )
        except Exception as exc:
            if receipt_id is not None:
                memory.finish_screen_companion_receipt(
                    receipt_id, status="failed"
                )
            with self._lock:
                self._last_error = f"{type(exc).__name__}: {redact_secrets(str(exc))[:500]}"

    def suggest_now(self) -> str | None:
        with Memory(self.database_path) as memory:
            state = memory.screen_companion_state()
            if state["mode"] not in {"suggest", "collaborate"} or state["paused"]:
                raise PermissionError("Screen Companion suggestions are paused or disabled")
            excluded = set(DEFAULT_EXCLUDED_APPS) | set(state["excluded_apps"])
            observation = self.provider.observe(
                capture_pixels=True,
                excluded_apps=excluded,
            )
            if observation is None or observation.excluded:
                raise PermissionError("The active window is unavailable or excluded")
            if observation.image is None:
                raise RuntimeError(
                    "The active window could not be read, so no suggestion was created"
                )
            rule = {
                "id": None,
                "source": "manual",
                "action_mode": "suggest",
                "action_prompt": (
                    "Offer one specific, optional next action for the visible work. "
                    "Phrase it naturally and do not perform it until the operator "
                    "accepts the suggestion."
                ),
            }
            return self.on_action(rule, observation) if self.on_action else None

    def _run(self) -> None:
        while not self._shutdown.wait(self.poll_seconds):
            try:
                with Memory(self.database_path) as memory:
                    state = memory.screen_companion_state()
                    if state["mode"] == "disabled" or state["paused"]:
                        with self._lock:
                            self._current = None
                            self._stable_since = 0.0
                        continue
                    excluded = set(DEFAULT_EXCLUDED_APPS) | set(state["excluded_apps"])
                    observation = self.provider.observe(
                        capture_pixels=False,
                        excluded_apps=excluded,
                    )
                    if observation is None:
                        continue
                    now = time.time()
                    with self._lock:
                        changed = (
                            self._current is None
                            or self._current.context_sha256 != observation.context_sha256
                        )
                        self._current = observation
                        if changed:
                            self._stable_since = now
                    if observation.excluded or now - self._stable_since < self.stable_seconds:
                        continue
                    if state["mode"] == "observe":
                        continue
                    rules = [
                        rule for rule in memory.list_screen_companion_rules()
                        if self._matches(rule, observation)
                    ]
                    for rule in rules:
                        effective = dict(rule)
                        effective["source"] = "rule"
                        if (
                            state["mode"] != "collaborate"
                            and effective["action_mode"] == "collaborate"
                        ):
                            effective["action_mode"] = "suggest"
                        self._dispatch(
                            memory,
                            effective,
                            observation,
                            excluded_apps=excluded,
                        )
                    auto_claim = (
                        memory.claim_screen_companion_auto(
                            context_sha256=observation.context_sha256,
                            cooldown_seconds=self.automatic_cooldown_seconds,
                            daily_limit=6,
                        )
                        if bool(state["auto_suggest"])
                        else None
                    )
                    if auto_claim is not None:
                        self._dispatch(
                            memory,
                            {
                                "id": None,
                                "source": "auto",
                                "action_mode": "suggest",
                                "action_prompt": (
                                    "Offer one specific, optional next action for the visible "
                                    "work. Phrase it naturally and do not perform it until the "
                                    "operator accepts the suggestion."
                                ),
                            },
                            observation,
                            excluded_apps=excluded,
                        )
                        self._last_auto_digest = observation.context_sha256
                        self._last_auto_at = now
            except Exception as exc:
                with self._lock:
                    self._last_error = (
                        f"{type(exc).__name__}: {redact_secrets(str(exc))[:500]}"
                    )
