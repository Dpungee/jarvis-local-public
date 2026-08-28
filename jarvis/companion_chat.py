from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping


_COMPANION_SUBJECT = re.compile(
    r"\b(?:screen\s+companion|companion(?:\s+mode)?|"
    r"observ(?:e|ing|ation)\s+mode|screen\s+watch(?:er|ing)?(?:\s+mode)?)\b",
    re.I,
)
_REFERENTIAL_CONTROL = re.compile(
    r"^\s*(?:(?:yes|yeah|yep|ok(?:ay)?|please|now)[,!. ]*)*"
    r"(?:(?:can|could|would)\s+you\s+)?(?:please\s+)?"
    r"(?:turn|switch|set|change|put|pause|resume|unpause|stop|start|enable|disable)\b"
    r"[^\r\n]{0,100}\b(?:it|that|this|mode)\b[^\r\n]{0,40}[?!. ]*$",
    re.I,
)
_MODE_CHANGE = re.compile(
    r"\b(?:switch|set|change|put|move|turn|use)\b[^.!?\r\n]{0,70}"
    r"\b(?:to|into|on)\b[^.!?\r\n]{0,30}"
    r"\b(?P<mode>observe|suggest|collaborate)(?:\s+mode)?\b|"
    r"\b(?P<leading_mode>observe|suggest|collaborate)(?:\s+mode)?\b"
    r"[^.!?\r\n]{0,50}\b(?:please|now)\b",
    re.I,
)
_STATUS_SIGNAL = re.compile(
    r"\b(?:status|current(?:ly)?|right\s+now|active|running|working|enabled|"
    r"disabled|paused|on|off)\b",
    re.I,
)
_LEARNING_SIGNAL = re.compile(
    r"\b(?:learn(?:ing|ed)?|improv(?:e|ing|ed)|feedback|training|trained|"
    r"remember(?:ing|ed)?|reusable\s+(?:outcomes?|patterns?))\b",
    re.I,
)
_COMPANION_LEARNING_SUBJECT = re.compile(
    r"\b(?:screen\s+companion|companion\s+mode|observ(?:e|ing|ation)\s+mode|"
    r"screen\s+watch(?:er|ing)?(?:\s+mode)?)\b",
    re.I,
)
_TERSE_LEARNING_STATUS = re.compile(
    r"^\s*(?:(?:hey|yo)\s+jarvis[, ]*)?(?:screen\s+)?companion(?:\s+mode)?\s+"
    r"(?:learning|feedback|training)(?:\s+status|\s+stats?)?\s*\?*\s*$",
    re.I,
)
_MODE_STATUS_QUESTION = re.compile(
    r"^\s*(?:(?:hey|yo)\s+jarvis[, ]*)?(?:what|which|word)\s+mode\s+"
    r"(?:is|does)\s+(?:the\s+|your\s+)?(?:screen\s+)?companion\b"
    r"(?:\s+(?:in|using|use))?|"
    r"^\s*(?:(?:hey|yo)\s+jarvis[, ]*)?(?:what|which|word)\s+"
    r"(?:is|are)\s+(?:the\s+|your\s+)?(?:screen\s+)?companion(?:['’]s)?\s+"
    r"mode\b(?:\s+set\s+to)?",
    re.I,
)
_STATUS_QUESTION = re.compile(
    r"^\s*(?:(?:hey|yo)\s+jarvis[, ]*)?(?:is|are|am)\b",
    re.I,
)
_WH_STATUS_QUESTION = re.compile(
    r"^\s*(?:(?:hey|yo)\s+jarvis[, ]*)?(?:what|which|word)\b"
    r"[^.!?\r\n]{0,100}\b(?:status|current(?:ly)?|right\s+now)\b",
    re.I,
)
_POLITE_STATUS_REQUEST = re.compile(
    r"^\s*(?:(?:hey|yo)\s+jarvis[, ]*)?"
    r"(?:(?:can|could|would)\s+you\s+)?(?:please\s+)?"
    r"(?:check|show|get|tell\s+me)\b[^.!?\r\n]{0,80}"
    r"\b(?:status|whether|currently|right\s+now)\b",
    re.I,
)
_TERSE_STATUS = re.compile(
    r"^\s*(?:(?:hey|yo)\s+jarvis[, ]*)?(?:screen\s+)?companion(?:\s+mode)?\s+"
    r"(?:status|on|off|active|running|enabled|disabled|paused)\s*\?\s*$",
    re.I,
)
_INTERROGATIVE = re.compile(
    r"^\s*(?:(?:hey|yo)\s+jarvis[, ]*)?"
    r"(?:is|are|am|what|which|word|can|could|do|does|tell|check|show)\b",
    re.I,
)
_PAUSE_STATE_QUESTION = re.compile(
    r"^\s*(?:(?:hey|yo)\s+jarvis[, ]*)?(?:is|are)\b"
    r"[^.!?\r\n]{0,70}\bpaused\b",
    re.I,
)
_SCREEN_VISIBILITY_STATUS = re.compile(
    r"\b(?:can|are|is)\b[^.!?\r\n]{0,45}"
    r"\b(?:see|watch|observ(?:e|ing))\b[^.!?\r\n]{0,35}\b(?:screen|desktop|me)\b|"
    r"\b(?:see|watch|observ(?:e|ing))\b[^.!?\r\n]{0,35}\b(?:right\s+now|currently)\b",
    re.I,
)
_EXPLICIT_PAUSE = re.compile(r"\bpause(?:d|\s+it|\s+the\s+mode|\s+observing)?\b", re.I)
_EXPLICIT_RESUME = re.compile(r"\b(?:resume|unpause|continue\s+observing)\b", re.I)
_EXPLICIT_OFF = re.compile(
    r"\b(?:turn|switch|set|shut|put)\b[^.!?\r\n]{0,45}\boff\b|"
    r"\b(?:disable|deactivate)\b|\bstop\s+(?:watching|observing)\b|"
    r"\bstop(?:\s+the)?\s+(?:screen\s+)?companion(?:\s+mode)?\b",
    re.I,
)
_EXPLICIT_ON = re.compile(
    r"\b(?:turn|switch|set|put)\b[^.!?\r\n]{0,45}\bon\b|"
    r"\b(?:enable|activate)\b|\bstart\s+(?:watching|observing)\b|"
    r"\bstart(?:\s+the)?\s+(?:screen\s+)?companion(?:\s+mode)?\b",
    re.I,
)
_TERSE_STATE_COMMAND = re.compile(
    r"^\s*(?:(?:hey|yo)\s+jarvis[, ]*)?(?:screen\s+)?companion(?:\s+mode)?\s+"
    r"(?P<state>on|off|pause|paused|resume|unpause)\s*[!.]*\s*$",
    re.I,
)
_CONTROL_SPEECH_ACT = re.compile(
    r"^\s*(?:(?:hey|yo)\s+jarvis[, ]*)?"
    r"(?:(?:yes|yeah|yep|ok(?:ay)?|now)[,!. ]*)*"
    r"(?:(?:(?:can|could|would)\s+you|i\s+(?:want|need)\s+you\s+to)\s+|"
    r"please\s+)?"
    r"(?P<body>(?:turn|switch|set|change|put|move|use|pause|resume|unpause|"
    r"stop|start|enable|disable|activate|deactivate)\b[^\r\n]{0,180})"
    r"[?!. ]*$",
    re.I,
)
_CONTROL_CONFIRMATION_SUFFIX = re.compile(
    r"(?:,?\s+(?:and|then)\s+)(?:please\s+)?(?:"
    r"confirm(?:\s+(?:the\s+)?(?:change|state|status|result))?|"
    r"report(?:\s+(?:the\s+)?(?:new\s+)?(?:state|status|result))?|"
    r"tell\s+me(?:\s+(?:the\s+)?(?:new\s+)?(?:state|status|result))?|"
    r"let\s+me\s+know(?:\s+(?:that\s+)?(?:it|the\s+mode|screen\s+companion)"
    r"\s+(?:is|was)\s+(?:on|off|paused|resumed|enabled|disabled))?"
    r")\s*$",
    re.I,
)
_NEGATED_CONTROL = re.compile(
    r"\b(?:do\s+not|don['’]?t|dont|never|not)\b[^.!?\r\n]{0,55}"
    r"\b(?:turn|switch|set|change|put|pause|resume|unpause|stop|start|enable|disable)\b",
    re.I,
)
_HYPOTHETICAL_CONTROL = re.compile(
    r"\b(?:what\s+(?:would|happens?\s+if)|how\s+(?:would|do)\s+i|"
    r"should\s+i|could\s+i|would\s+it|if\s+i)\b[^.!?\r\n]{0,100}"
    r"\b(?:turn|switch|set|pause|resume|stop|start|enable|disable)\b",
    re.I,
)
_INVALID_MODE_CHANGE = re.compile(
    r"\b(?:switch|set|change|put|move|turn|use)\b[^.!?\r\n]{0,70}"
    r"\b(?:companion|mode)\b[^.!?\r\n]{0,40}\b(?:to|into|as)\s+"
    r"(?P<mode>[a-z][a-z0-9_-]{1,30})\b",
    re.I,
)


@dataclass(frozen=True)
class CompanionChatIntent:
    """One bounded operator request concerning the Screen Companion control plane."""

    action: str
    mode: str | None = None


def _recent_mentions_companion(messages: Iterable[Mapping[str, Any]]) -> bool:
    recent = list(messages)[-4:]
    return any(
        _COMPANION_SUBJECT.search(str(message.get("content") or "")) is not None
        for message in recent
        if str(message.get("role") or "") in {"user", "assistant"}
    )


def screen_companion_chat_intent(
    prompt: str,
    recent_messages: Iterable[Mapping[str, Any]] = (),
) -> CompanionChatIntent | None:
    """Resolve status and explicit control language without delegating authority to a model.

    The resolver is deliberately semantic and small: it requires a Companion/screen-
    observation subject (or a short referential follow-up after that subject) plus an
    unambiguous status or control signal. General conversation remains model-owned.
    """

    text = re.sub(r"\s+", " ", str(prompt or "")).strip()
    if not text or len(text) > 320:
        return None
    has_subject = _COMPANION_SUBJECT.search(text) is not None
    if not has_subject:
        has_subject = bool(
            _REFERENTIAL_CONTROL.fullmatch(text)
            and _recent_mentions_companion(recent_messages)
        )
    if not has_subject and _SCREEN_VISIBILITY_STATUS.search(text) is None:
        return None

    quoted = any(mark in text for mark in ('"', "'", "“", "”", "‘", "’"))
    if quoted and re.search(
        r"\b(?:turn|switch|set|pause|resume|stop|start|enable|disable)\b",
        text,
        re.I,
    ):
        return None
    if _NEGATED_CONTROL.search(text) or _HYPOTHETICAL_CONTROL.search(text):
        return None

    terse = _TERSE_STATE_COMMAND.fullmatch(text)
    if terse is not None:
        state = str(terse.group("state")).casefold()
        return CompanionChatIntent({
            "on": "on",
            "off": "off",
            "pause": "pause",
            "paused": "pause",
            "resume": "resume",
            "unpause": "resume",
        }[state])

    speech_act = _CONTROL_SPEECH_ACT.fullmatch(text)
    if speech_act is not None:
        body = _CONTROL_CONFIRMATION_SUFFIX.sub("", str(speech_act.group("body"))).strip()
        mode_words = {
            match.casefold()
            for match in re.findall(r"\b(?:observe|suggest|collaborate)\b", body, re.I)
        }
        if len(mode_words) > 1:
            return CompanionChatIntent("ambiguous")
        mode_match = _MODE_CHANGE.search(body)
        if mode_match is not None:
            mode = str(
                mode_match.group("mode") or mode_match.group("leading_mode")
            ).casefold()
            return CompanionChatIntent("mode", mode)

        resume_requested = _EXPLICIT_RESUME.search(body) is not None
        pause_requested = _EXPLICIT_PAUSE.search(body) is not None
        off_requested = _EXPLICIT_OFF.search(body) is not None
        on_requested = _EXPLICIT_ON.search(body) is not None
        if (resume_requested and pause_requested) or (off_requested and on_requested):
            return CompanionChatIntent("ambiguous")
        if re.search(r"\b(?:and|then|also)\b", body, re.I):
            return None

        # Resume/unpause must be checked before pause because "unpause" contains "pause".
        if resume_requested:
            return CompanionChatIntent("resume")
        if pause_requested:
            return CompanionChatIntent("pause")
        if off_requested:
            return CompanionChatIntent("off")
        if on_requested:
            return CompanionChatIntent("on")

        invalid_mode = _INVALID_MODE_CHANGE.search(body)
        if invalid_mode is not None:
            candidate = str(invalid_mode.group("mode")).casefold()
            if candidate not in {"observe", "suggest", "collaborate"}:
                return CompanionChatIntent("invalid_mode", candidate)

    if _COMPANION_LEARNING_SUBJECT.search(text) and _LEARNING_SIGNAL.search(text) and (
        _INTERROGATIVE.search(text) or _TERSE_LEARNING_STATUS.fullmatch(text)
    ):
        return CompanionChatIntent("learning_status")

    if (
        _SCREEN_VISIBILITY_STATUS.search(text)
        or _MODE_STATUS_QUESTION.search(text)
        or _TERSE_STATUS.fullmatch(text)
        or (
            _STATUS_SIGNAL.search(text)
            and (
                _STATUS_QUESTION.search(text)
                or _WH_STATUS_QUESTION.search(text)
                or _POLITE_STATUS_REQUEST.search(text)
            )
        )
    ):
        return CompanionChatIntent("status")
    return None


def public_screen_companion_state(state: Mapping[str, Any]) -> dict[str, Any]:
    """Return only operator-useful control state; never expose window titles or rules."""

    mode = str(state.get("mode") or "disabled").strip().casefold()
    if mode not in {"disabled", "observe", "suggest", "collaborate"}:
        mode = "disabled"
    paused = bool(state.get("paused", mode == "disabled"))
    enabled = mode != "disabled"
    raw_learning = state.get("learning")

    def count(name: str) -> int:
        if not isinstance(raw_learning, Mapping):
            return 0
        value = raw_learning.get(name, 0)
        if isinstance(value, bool):
            return 0
        try:
            return max(0, min(int(value), 1_000_000_000))
        except (TypeError, ValueError):
            return 0

    return {
        "mode": mode,
        "paused": paused,
        "enabled": enabled,
        "active": enabled and not paused,
        "auto_suggest": bool(state.get("auto_suggest", False)),
        "captures_pixels": enabled and not paused and mode in {"suggest", "collaborate"},
        "raw_screens_persisted": False,
        "updated_at": str(state.get("updated_at") or ""),
        "available": (
            bool(state.get("available")) if isinstance(state.get("available"), bool)
            else None
        ),
        "has_runtime_error": bool(str(state.get("last_error") or "").strip()),
        "learning": {
            "feedback": count("feedback"),
            "accepted": count("accepted"),
            "dismissed": count("dismissed"),
            "verified_outcomes": count("verified_outcomes"),
            "reusable_outcomes": count("reusable_outcomes"),
            "non_reusable_outcomes": count("non_reusable_outcomes"),
        },
    }


def render_screen_companion_state(
    state: Mapping[str, Any],
    *,
    changed: bool,
) -> str:
    public = public_screen_companion_state(state)
    mode = str(public["mode"])
    prefix = "Done — " if changed else ""
    if mode == "disabled":
        return (
            f"{prefix}Screen Companion is off. It is not observing your screen."
        )
    label = mode.capitalize()
    if public["paused"]:
        return (
            f"{prefix}Screen Companion is paused in {label} mode, so it is not "
            "observing right now."
        )
    if public["available"] is False or public["has_runtime_error"]:
        return (
            f"{prefix}Screen Companion is configured for {label} mode and is not "
            "paused, but screen observation is unavailable in the running Presence "
            "service right now. No raw screenshots are stored."
        )
    live_prefix = (
        f"{prefix}Screen Companion is on in {label} mode and is not paused."
        if public["available"] is True
        else (
            f"{prefix}Screen Companion is configured for {label} mode and is not "
            "paused."
        )
    )
    if mode == "observe":
        detail = (
            "It can follow the active app/window context, but this mode does not "
            "capture screen pixels or act on the computer."
        )
    elif mode == "suggest":
        detail = (
            "It may inspect the visible screen to offer help, but it will not control "
            "the computer on its own."
        )
    else:
        detail = (
            "It may inspect the visible screen and run only the bounded actions you "
            "explicitly configured; normal approval and safety gates still apply."
        )
    return f"{live_prefix} {detail} Raw screenshots are not stored."


def render_screen_companion_learning_state(state: Mapping[str, Any]) -> str:
    """Explain exact, bounded Companion learning without implying model training."""
    learning = public_screen_companion_state(state)["learning"]
    feedback = int(learning["feedback"])
    accepted = int(learning["accepted"])
    dismissed = int(learning["dismissed"])
    verified = int(learning["verified_outcomes"])
    reusable = int(learning["reusable_outcomes"])
    if feedback == 0:
        return (
            "Not yet—Companion has no explicit feedback or verified reusable action "
            "patterns. Observe mode intentionally does not train on or store your screen. "
            "Learning begins only when you accept or dismiss a suggestion; an accepted "
            "action becomes reusable only after its exact outcome is independently verified."
        )
    return (
        f"Yes, in a bounded way: Companion has {feedback} explicit feedback signal"
        f"{'s' if feedback != 1 else ''} ({accepted} accepted, {dismissed} dismissed), "
        f"{verified} verified action outcome{'s' if verified != 1 else ''}, and "
        f"{reusable} verified reusable category signal"
        f"{'s' if reusable != 1 else ''}. "
        "It stores only content-free digests and outcome categories—not screenshots, "
        "window titles, visible text, or suggestion text. Feedback can improve ranking "
        "or suppress poor suggestions, but it cannot grant authority or bypass approvals."
    )
