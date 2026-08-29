from __future__ import annotations

import re
from datetime import datetime


def instant_casual_reply(prompt: str) -> str:
    """Answer unambiguous social openers without loading or swapping an LLM."""
    folded = prompt.casefold().replace("what's", "whats").replace("what’s", "whats")
    normalized = re.sub(r"[\s!?.',-]+", " ", folded).strip()
    if normalized in {"thanks", "thank you"}:
        return "Anytime. Ready when you are."
    if normalized.startswith("good morning"):
        return "Good morning. Ready when you are."
    if normalized.startswith("good afternoon"):
        return "Good afternoon. Ready when you are."
    if normalized.startswith("good evening"):
        return "Good evening. Ready when you are."
    if re.fullmatch(
        r"(?:(?:hey|yo|sup)(?: (?:jar|jarvis))?(?: whats good)?|"
        r"whats good(?: (?:jar|jarvis))?|"
        r"what up(?: bro)?|whats up(?: bro)?|what is up(?: bro)?)",
        normalized,
    ):
        return "What's up, bro? Ready when you are."
    return "Hey. Ready when you are."


def instant_local_time_reply(
    prompt: str,
    *,
    now: datetime | None = None,
) -> str | None:
    """Answer an unambiguous local-clock question without model inference."""
    if not is_local_time_request(prompt):
        return None
    current = now or datetime.now().astimezone()
    if current.tzinfo is None or current.utcoffset() is None:
        current = current.astimezone()
    clock = current.strftime("%I:%M %p").lstrip("0")
    zone = current.tzname() or current.strftime("UTC%z")
    date = current.strftime("%A, %B %d, %Y").replace(" 0", " ")
    return f"It’s {clock} {zone} on {date}."


def is_local_time_request(prompt: str) -> bool:
    """Recognize a bounded local-clock request without regex backtracking."""
    text = str(prompt)
    if not 1 <= len(text) <= 256:
        return False
    folded = text.casefold().replace("’", "'").replace("what's", "what is")
    for character in ",!:-?.":
        folded = folded.replace(character, " ")
    tokens = folded.split()
    offset = 0
    while offset < len(tokens) and tokens[offset] in {
        "hey", "yo", "ok", "okay", "please",
    }:
        offset += 1
    if offset < len(tokens) and tokens[offset] in {"jar", "jarvis"}:
        offset += 1
    tokens = tokens[offset:]
    if tokens[-2:] == ["right", "now"]:
        tokens = tokens[:-2]
    elif tokens[-1:] == ["now"]:
        tokens = tokens[:-1]
    if tokens == ["what", "time", "is", "it"] or tokens == ["current", "time"]:
        return True
    if tokens[:2] == ["what", "is"]:
        remainder = tokens[2:]
    elif (
        len(tokens) >= 2
        and tokens[0] in {"tell", "give", "show"}
        and tokens[1] == "me"
    ):
        remainder = tokens[2:]
    else:
        return False
    if remainder[:1] == ["the"]:
        remainder = remainder[1:]
    if remainder[:1] == ["current"]:
        remainder = remainder[1:]
    return remainder == ["time"]


def simple_fraction_comparison_reply(prompt: str) -> str | None:
    """Answer one unambiguous two-fraction comparison with exact arithmetic."""
    match = re.fullmatch(
        r"\s*(?:which|what)\s+(?:fraction\s+)?is\s+"
        r"(?:larger|bigger|greater)\s*[:,]?\s*"
        r"(-?\d{1,9})\s*/\s*(-?\d{1,9})\s+"
        r"(?:or|versus|vs\.?)\s+"
        r"(-?\d{1,9})\s*/\s*(-?\d{1,9})"
        r"(?:\s*[?.!,;:]?\s*(?:show\s+(?:one\s+)?line\s+of\s+arithmetic\.?)?)?\s*",
        prompt,
        re.I,
    )
    if match is None:
        return None
    left_num, left_den, right_num, right_den = map(int, match.groups())
    if left_den == 0 or right_den == 0:
        return None
    if left_den < 0:
        left_num, left_den = -left_num, -left_den
    if right_den < 0:
        right_num, right_den = -right_num, -right_den
    left_cross = left_num * right_den
    right_cross = right_num * left_den
    relation = ">" if left_cross > right_cross else "<" if left_cross < right_cross else "="
    return (
        f"{left_num}/{left_den} {relation} {right_num}/{right_den} because "
        f"{left_num}×{right_den} = {left_cross} {relation} {right_cross} = "
        f"{right_num}×{left_den}."
    )
