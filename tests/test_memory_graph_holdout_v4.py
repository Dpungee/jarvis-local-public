"""Sealed one-use multi-hop holdout for the VTMF M3 temporal graph (v4).

Holdouts v1, v2 and v3 were each scored once, failed the gate, and are
quarantined.  This fixture and the sealed scorer between the ``BEGIN``/``END``
markers were authored by a fourth, independent agent, which read only sections
1-5 and 7.14 of the M3 design (see ``docs/MEMORY_GRAPH.md``) and the boss
rulings 10.4, 10.5, 10.6 and 10.7, the quarantined v3 scorer as a sealing
template (never any quarantined fixture, never a score log), the public write
and read surface of ``jarvis.memory.Memory`` and ``agent._named_fact_subjects``.
That agent never read ``jarvis/memory_graph.py``, ``jarvis/memory.py``,
``jarvis/agent.py`` beyond those signatures, the graph tests, or any
development battery, and never called ``Memory.graph_chains`` while authoring:
every expectation in the fixture comes from a reference walker written from the
design text alone.

The domain, entities, predicates and questions are new: a fictional upland
honey circuit, where a hive is worked from an ``apiary`` yard, a yard answers
to a ``holding`` estate, an estate sells at a ``market`` fair, and a fair keeps
an ``overseer`` bailiff -- four relation types, so a depth-four chain proves
the three-hop cap.

**One-use is procedural, not cryptographic.**  The run token is derivable from
the two digests by anyone holding the files, so it is a tamper seal and nothing
more.  The discipline is what makes the number mean anything: the boss scores
this fixture exactly once against a frozen runtime pin and records the result.

Three parts of the module sit deliberately outside the sealed region so a
signature drift can be shimmed without breaking the seal or the token:
``invoke_graph_chains``, ``PINNED_FILES``, and the placeholder handling.
Everything that decides a number is inside the seal.

Scoring follows ruling 10.6 item 6 and ruling 10.7 item 8: record mapping by
claim id first and then scope-aware content; every forbidden-record and
forbidden-subject comparison scope-aware on ``(scope, entity_key)``; an
overflow note naming the case's own subject, or any subject independently
visible to the asking project, is never a leak; the lane-silence mapping of
10.7 item 5 is pinned; ``expect_incomplete`` is satisfied by any returned row
of the case carrying ``incomplete: true``; a two-subject case with one
unidentified name expects the resolved subject's chain and ``unresolved``
naming the other; and the abstention modes gate while the name of a successful
read is reported only.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import statistics
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any


FIXTURE_PATH = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "memory_graph_holdout_v4.json"
)
FIXTURE_SHA256 = "ca2f213b21b73d6e19dee988d361264839b2b577958406a97afa9ccf9b470136"
SCORER_SHA256 = "a0bcef7cbd4ee77ad8b67bf24be7bf106bdcd05c22ce6b64cc427d8a805a5e0c"
SCORER_START = "# -- BEGIN SEALED MEMORY GRAPH HOLDOUT V4 SCORER --"
SCORER_END = "# -- END SEALED MEMORY GRAPH HOLDOUT V4 SCORER --"
TOKEN_ENVIRONMENT_VARIABLE = "JARVIS_MEMORY_GRAPH_HOLDOUT_V4_TOKEN"

# The four files the fixture pins.  ``jarvis/agent.py`` is deliberately absent
# (design section 1.4): the scored path is store side, and the fixture bakes
# the question -> subjects mapping so the agent parser is never called here.
PINNED_FILES = (
    "jarvis/memory.py",
    "jarvis/memory_graph.py",
    "jarvis/memory_retrieval.py",
    "jarvis/redaction.py",
)
# An unsealed pin.  ``claude-reseal-runtime-pins.py`` (boss item) replaces each
# per-file placeholder with the real digest, then rewrites FIXTURE_SHA256 and
# the run token.  While any placeholder stands, the sealed test skips.
PLACEHOLDER_DIGEST = "0" * 64
REPOSITORY_ROOT = Path(__file__).resolve().parent.parent


def _sealed_scorer_bytes() -> bytes:
    source = Path(__file__).read_text(encoding="utf-8")
    normalized = source.replace("\r\n", "\n").replace("\r", "\n")
    opening = SCORER_START + "\n"
    closing = "\n" + SCORER_END
    start = normalized.index(opening) + len(opening)
    end = normalized.index(closing, start)
    return normalized[start:end].encode("utf-8")


def _required_run_token() -> str:
    seals = f"{FIXTURE_SHA256}:{SCORER_SHA256}".encode("ascii")
    return hashlib.sha256(seals).hexdigest()


def _seal_is_placeholder() -> bool:
    return PLACEHOLDER_DIGEST in {FIXTURE_SHA256, SCORER_SHA256}


def _pin_is_placeholder(fixture: dict[str, Any]) -> bool:
    pin = fixture["runtime_sha256"]
    return any(pin.get(name) == PLACEHOLDER_DIGEST for name in PINNED_FILES)


def _runtime_pin_now() -> dict[str, str]:
    """Digest the four pinned files as they stand in this tree."""
    digests: dict[str, str] = {}
    for name in PINNED_FILES:
        path = REPOSITORY_ROOT / name
        digests[name] = (
            hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else ""
        )
    return digests


def invoke_graph_chains(
    memory: Any,
    *,
    question: str,
    project_id: int | None,
    subjects: list[str],
    seed_claims: list[Any],
    temporal: bool,
    as_of: str | None,
    lane_mode: str | None,
    limit: int,
) -> dict[str, Any]:
    """Call the graph channel.  Shim here, never inside the sealed block.

    Design section 5.1 names the signature.  If it drifts, the boss adapts this
    one function; the seal and the run token are unaffected because the digest
    covers only the region between the markers.
    """
    return memory.graph_chains(
        question,
        project_id=project_id,
        subjects=list(subjects),
        seed_claims=list(seed_claims),
        temporal=bool(temporal),
        as_of=as_of,
        lane_mode=lane_mode,
        limit=int(limit),
    )


# -- BEGIN SEALED MEMORY GRAPH HOLDOUT V4 SCORER --
def _load_fixture() -> dict[str, Any]:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def _entity_key(text: str) -> str:
    import unicodedata

    return " ".join(unicodedata.normalize("NFKC", str(text)).casefold().split())


# Fictional filler for the generator directives.  None of it names a real
# person, place, host, or product.
_WORD_POOL = (
    "combwax", "heather", "linden", "sainfoin", "borage", "hawkbit",
    "propolis", "nectar", "brood", "queenright", "supering", "uncapping",
    "settling", "creaming", "granulate", "smoker", "veiling", "drawnfoam",
    "meadowsweet", "clovergold", "thistleblue", "gorseamber", "limeflower",
    "chestnutdark", "orchardpale", "ivylate", "willowearly", "rosebaywhite",
    "phaceliaclear", "buckwheatdeep",
)
_PLACE_POOL = (
    "Quillhampton", "Dornmarch", "Ashfielden", "Vantreybourne", "Ferrowick",
    "Pellamere", "Ostreymoat", "Immerholt", "Sorbeckton", "Havermarsh",
)
_WAY_TYPES = ("Lane", "Street", "Road", "Avenue", "Court", "Drive")
_BASE32 = "abcdefghijklmnopqrstuvwxyz234567"
_HEXDIGITS = "0123456789abcdef"
_ALPHABET = "abcdefghijklmnopqrstuvwxyz"


def _seeded_stream(seed: int, record_id: str, field: str, count: int) -> bytes:
    material = f"{int(seed)}|{record_id}|{field}".encode("ascii")
    out = bytearray()
    counter = 0
    while len(out) < count:
        out.extend(hashlib.sha256(
            material + b"|" + str(counter).encode("ascii")).digest())
        counter += 1
    return bytes(out[:count])


def _pick(stream: bytes, index: int, choices: Any) -> Any:
    return choices[stream[index % len(stream)] % len(choices)]


def _digits(stream: bytes, start: int, count: int, *, first_min: int = 0) -> str:
    out = []
    for offset in range(count):
        value = stream[(start + offset) % len(stream)] % 10
        if offset == 0 and value < first_min:
            value = first_min + (value % max(1, 10 - first_min))
        out.append(str(value))
    return "".join(out)


def _luhn_check_digit(body: str) -> int:
    total = 0
    for index, character in enumerate(reversed(body)):
        digit = int(character)
        if index % 2 == 0:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return (10 - (total % 10)) % 10


def _fullwidth(text: str) -> str:
    out = []
    for character in text:
        code = ord(character)
        if 0x21 <= code <= 0x7E:
            out.append(chr(code + 0xFEE0))
        elif character == " ":
            out.append(chr(0x3000))
        else:
            out.append(character)
    return "".join(out)


def _homoglyph(text: str) -> str:
    table = {"a": chr(0x0430), "e": chr(0x0435), "o": chr(0x043E),
             "p": chr(0x0440), "c": chr(0x0441), "y": chr(0x0443),
             "x": chr(0x0445)}
    out = []
    replaced = 0
    for character in text:
        lowered = character.lower()
        if replaced < 2 and lowered in table and character.islower():
            out.append(table[lowered])
            replaced += 1
        else:
            out.append(character)
    if not replaced:
        raise AssertionError("homoglyph directive found nothing to substitute")
    return "".join(out)


def _expand_directive(directive: dict[str, Any], seed: int, record_id: str,
                      field: str) -> str:
    """Expand one generator directive deterministically (design section 1.4).

    The fixture bytes stay public-safe because no live secret, private
    identifier, or confusable spelling is written into the file: this closed
    vocabulary is the only way such content enters the store, and it is
    reproducible from ``fixture["generator_seed"]``.
    """
    if "spelling" in directive:
        base = str(directive["base"])
        style = str(directive["spelling"])
        if style == "nfkc_confusable":
            return _fullwidth(base)
        if style == "homoglyph":
            return _homoglyph(base)
        raise AssertionError(f"unsupported spelling directive: {style}")
    template = str(directive["value_template"])
    stream = _seeded_stream(seed, record_id, field, 96)
    dot = "."
    colon = ":"
    at_sign = "@"
    slash = "/"
    if template == "secret_token":
        return "sk" + "-" + "".join(_BASE32[byte % 32] for byte in stream[:32])
    if template == "secret_assignment":
        body = "".join(_HEXDIGITS[byte % 16] for byte in stream[:32])
        return "api" + "_key = " + body
    if template == "ipv4_private":
        return dot.join(("10", str(stream[0] % 250 + 3),
                         str(stream[1] % 250 + 3), str(stream[2] % 250 + 3)))
    if template == "ipv4_exempt":
        family = _pick(stream, 3, ("loopback", "documentation", "multicast"))
        if family == "loopback":
            return dot.join(("127", "0", "0", "1"))
        if family == "documentation":
            return dot.join(("192", "0", "2", str(stream[4] % 240 + 5)))
        return dot.join(("224", "0", "0", str(stream[5] % 18 + 2)))
    if template == "ipv6_global":
        head = "".join(_HEXDIGITS[byte % 16] for byte in stream[:4])
        tail = "".join(_HEXDIGITS[byte % 16] for byte in stream[4:7])
        return "2a" + head[:2] + colon + head[2:] + "be" + colon + colon + tail
    if template == "ipv6_exempt":
        if stream[0] % 2:
            return colon + colon + "1"
        tail = "".join(_HEXDIGITS[byte % 16] for byte in stream[1:4])
        return "2001" + colon + "db8" + colon + colon + tail
    if template == "phone_us":
        return "-".join((_digits(stream, 0, 3, first_min=2), "555",
                         _digits(stream, 4, 4)))
    if template == "phone_e164":
        return ("+" + _digits(stream, 0, 2, first_min=1) + " "
                + _digits(stream, 3, 3) + " " + _digits(stream, 7, 3) + " "
                + _digits(stream, 11, 4))
    if template == "ssn":
        return "-".join((_digits(stream, 0, 3, first_min=1),
                         _digits(stream, 4, 2, first_min=1),
                         _digits(stream, 7, 4, first_min=1)))
    if template in {"card_luhn", "card_not_luhn"}:
        body = "4" + _digits(stream, 0, 14)
        check = _luhn_check_digit(body)
        if template == "card_not_luhn":
            check = (check + 3) % 10
        return body + str(check)
    if template == "street_address":
        return " ".join((str(stream[0] % 380 + 14),
                         _pick(stream, 1, _PLACE_POOL),
                         _pick(stream, 2, _WAY_TYPES)))
    if template == "email_person":
        local = _pick(stream, 0, _WORD_POOL) + dot + _pick(stream, 1, _WORD_POOL)
        return local + at_sign + _pick(stream, 2, _WORD_POOL) + dot + "example"
    if template == "email_ip_host":
        host = dot.join(("10", str(stream[3] % 250 + 3),
                         str(stream[4] % 250 + 3), str(stream[5] % 250 + 3)))
        return _pick(stream, 0, _WORD_POOL) + at_sign + host
    if template == "user_home_windows":
        separator = chr(92)
        return separator.join(("C" + colon, "Users",
                               _pick(stream, 0, _WORD_POOL),
                               _pick(stream, 1, _WORD_POOL)))
    if template == "user_home_posix":
        return (slash + "home" + slash + _pick(stream, 0, _WORD_POOL) + slash
                + _pick(stream, 1, _WORD_POOL))
    if template == "version":
        return dot.join((str(stream[0] % 9 + 1), str(stream[1] % 30),
                         str(stream[2] % 40)))
    if template == "port_range":
        low = 9000 + (stream[0] % 900)
        return f"{low}-{low + 9}"
    if template == "isbn13":
        body = "978" + _digits(stream, 0, 9)
        total = sum(int(character) * (1 if index % 2 == 0 else 3)
                    for index, character in enumerate(body))
        check = (10 - (total % 10)) % 10
        return "-".join(("978", body[3:4], body[4:8], body[8:12], str(check)))
    if template == "mac":
        pairs = ["02"]
        pairs.extend(
            _HEXDIGITS[stream[index] % 16] + _HEXDIGITS[stream[index + 1] % 16]
            for index in range(0, 10, 2)
        )
        return colon.join(pairs)
    if template == "cidr":
        return dot.join(("10", str(stream[0] % 240 + 5), "0", "0")) + slash + "16"
    if template == "iso_date":
        return "-".join(("20" + _digits(stream, 0, 2, first_min=3),
                         "{0:02d}".format(stream[3] % 12 + 1),
                         "{0:02d}".format(stream[4] % 28 + 1)))
    if template == "rack_label":
        return "R{0}-B{1}-U{2}".format(stream[0] % 9 + 1, stream[1] % 40 + 1,
                                       stream[2] % 40 + 1)
    if template == "long_prose":
        count = int(directive["words"])
        return " ".join(
            _WORD_POOL[stream[index % len(stream)] % len(_WORD_POOL)]
            for index in range(count))
    if template in {"long_value", "label_overlong"}:
        count = int(directive["chars"])
        return "".join(_ALPHABET[stream[index % len(stream)] % 26]
                       for index in range(count))
    raise AssertionError(f"unsupported value template: {template}")


def _expanded_field(record: dict[str, Any], field: str, seed: int) -> str:
    value = record.get(field)
    if isinstance(value, dict):
        return _expand_directive(value, seed, str(record["id"]), field)
    return str(value)


class _FixtureClock:
    """Monotonic, injectable store clock so ``as_of`` cases are well defined."""

    def __init__(self, start: str, tick_seconds: float) -> None:
        from datetime import datetime

        self.moment = datetime.fromisoformat(start)
        self.tick = float(tick_seconds)

    def advance(self, seconds: float) -> None:
        from datetime import timedelta

        self.moment = self.moment + timedelta(seconds=float(seconds))

    def __call__(self) -> str:
        self.advance(self.tick)
        return self.moment.isoformat()

    def offset_from(self, stamp: str, seconds: float) -> str:
        from datetime import datetime, timedelta

        moment = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
        return (moment + timedelta(seconds=float(seconds))).isoformat()


def _project_fact_command(kind: str, **fields: str) -> str:
    prefix = {
        "claim": "Remember this project fact: ",
        "forget": "Forget this project fact: ",
        "erase": "Erase this project fact: ",
    }[kind]
    return prefix + json.dumps(fields, sort_keys=True)


def _seed_store(memory_module: Any, memory: Any, store: dict[str, Any],
                fixture: dict[str, Any]) -> dict[str, Any]:
    """Replay one store's ordered script through the public write API only."""
    seed = int(fixture["generator_seed"])
    clock = _FixtureClock(fixture["clock"]["start"],
                          fixture["clock"]["tick_seconds"])
    real_now = memory_module.now_iso
    memory_module.now_iso = clock
    projects: dict[int, int] = {}
    conversations: dict[int, int] = {}
    claim_ids: dict[str, int] = {}
    tombstones = 0
    try:
        for logical in store["projects"]:
            logical = int(logical)
            projects[logical] = memory.add_project(
                f"Holdout project {logical}",
                f"@projects/holdout-graph-v4-{store['id']}-{logical}",
            )
            conversations[logical] = memory.new_conversation(
                f"graph holdout v4 {store['id']} p{logical}",
                project_id=projects[logical],
            )
        for record in store["records"]:
            operation = str(record["op"])
            if operation == "clock":
                clock.advance(float(record["advance_seconds"]))
                continue
            subject = _expanded_field(record, "subject", seed)
            predicate = str(record["predicate"])
            scope = str(record["scope"])
            if operation == "claim":
                value = _expanded_field(record, "value", seed)
                if scope == "global":
                    claim_id = int(memory.remember_claim(
                        subject, predicate, value,
                        source="synthetic graph holdout v4 fictional survey",
                        authority=str(record["authority"]),
                        confidence=float(record["confidence"]),
                    ))
                else:
                    logical = int(scope.split(":")[1])
                    receipt = memory.remember_explicit_project_claim(
                        conversations[logical], projects[logical],
                        _project_fact_command(
                            "claim", subject=subject, predicate=predicate,
                            value=value,
                        ),
                    )
                    claim_id = int(receipt["claim_id"])
                claim_ids[str(record["id"])] = claim_id
            elif operation == "forget":
                logical = int(scope.split(":")[1])
                memory.retract_explicit_project_claim(
                    conversations[logical], projects[logical],
                    _project_fact_command("forget", subject=subject,
                                          predicate=predicate),
                )
            elif operation == "erase":
                logical = int(scope.split(":")[1])
                memory.erase_explicit_project_claim(
                    conversations[logical], projects[logical],
                    _project_fact_command("erase", subject=subject,
                                          predicate=predicate),
                )
                tombstones += 1
            else:
                raise AssertionError(f"unsupported record op: {operation}")
        for index in range(int(store.get("padding_claims") or 0)):
            # the values repeat every 400 rows so the padded store lands near
            # the design's twelve-thousand-key entity scale rather than double it
            memory.remember_claim(
                "Slipgate{0:05d} comb".format(index),
                "padroll",
                "Vantwood{0:05d} roll".format(index % 400),
                source="synthetic graph holdout v4 fictional padding",
                authority="external", confidence=0.7,
            )
    finally:
        memory_module.now_iso = real_now
    rows: dict[str, dict[str, Any]] = {}
    for record_id, claim_id in claim_ids.items():
        row = memory.db.execute(
            "SELECT id, scope, subject, predicate, value, status, authority,"
            " valid_from FROM memory_claims WHERE id=?",
            (claim_id,),
        ).fetchone()
        if row is not None:
            rows[record_id] = dict(row)
    visible_keys: dict[str, set[str]] = {}
    for row in memory.db.execute(
            "SELECT scope, subject, value FROM memory_claims"):
        entry = dict(row)
        bucket = visible_keys.setdefault(str(entry["scope"]), set())
        bucket.add(_entity_key(entry["subject"]))
        bucket.add(_entity_key(entry["value"]))
    return {
        "projects": projects,
        "claim_ids": claim_ids,
        "rows": rows,
        "tombstones": tombstones,
        "keys_by_scope": visible_keys,
        "clock": clock,
    }


def _record_index(seeded: dict[str, Any]) -> dict[str, Any]:
    """Map a returned row back to a fixture record id.

    By claim id first; otherwise by scope-aware content, so a project row and a
    global or other-project row with identical text can never be confused
    (ruling 10.6 item 6).
    """
    by_claim = {int(claim_id): record_id
                for record_id, claim_id in seeded["claim_ids"].items()}
    by_content: dict[tuple[str, str, str, str], str] = {}
    for record_id, row in seeded["rows"].items():
        key = (str(row["scope"]), _entity_key(row["subject"]),
               _entity_key(row["predicate"]), _entity_key(row["value"]))
        by_content.setdefault(key, record_id)
    return {"by_claim": by_claim, "by_content": by_content}


def _screened_directive_strings(store: dict[str, Any],
                                fixture: dict[str, Any]) -> list[str]:
    """Every expansion the store must never echo back (design section 1.4)."""
    seed = int(fixture["generator_seed"])
    out: list[str] = []
    for record in store["records"]:
        if str(record.get("screened_expectation") or "") != "screen":
            continue
        for field in ("subject", "value"):
            value = record.get(field)
            if isinstance(value, dict):
                out.append(_expand_directive(value, seed, str(record["id"]),
                                             field))
    return [text for text in out if len(text) >= 8]


def _row_record_id(row: dict[str, Any], index: dict[str, Any],
                   fallback_scope: str) -> str | None:
    for field in ("claim_id", "id", "record_id"):
        if row.get(field) is not None:
            try:
                candidate = int(row[field])
            except (TypeError, ValueError):
                continue
            if candidate in index["by_claim"]:
                return index["by_claim"][candidate]
    content = (_entity_key(row.get("subject", "")),
               _entity_key(row.get("predicate", "")),
               _entity_key(row.get("value", "")))
    # Scope-aware first (ruling 10.6 item 6): a project row and a global row
    # with identical text are different records.  Only a row that carried no
    # scope at all falls back to the other visible scope.
    scopes = [str(row.get("scope") or fallback_scope)]
    if not row.get("scope"):
        scopes.extend(["global", fallback_scope])
    for scope in scopes:
        found = index["by_content"].get((scope, *content))
        if found is not None:
            return found
    return None


def _is_note_row(row: dict[str, Any]) -> bool:
    return str(row.get("status", "")) == "overflow"


def _screens() -> tuple[Any, Any]:
    from jarvis import redaction

    widened = getattr(redaction, "contains_private_identifier_extended", None)
    if widened is None:
        widened = redaction.contains_private_identifier
    return redaction.contains_secret, widened


def _scope_visible(scope: str, project_scope: str | None) -> bool:
    if not scope:
        return True
    if scope == "global":
        return True
    return project_scope is not None and scope == project_scope


def _unresolved_names(report: dict[str, Any]) -> set[str]:
    """Entity keys the walk reported as unidentified (ruling 10.7 item 4).

    Tolerant of shape: a list of strings, a list of mappings carrying a name,
    or a single string.
    """
    raw = report.get("unresolved")
    out: set[str] = set()
    if raw is None:
        return out
    if isinstance(raw, str):
        return {_entity_key(raw)}
    if isinstance(raw, dict):
        raw = list(raw.values())
    try:
        entries = list(raw)
    except TypeError:
        return out
    for entry in entries:
        if isinstance(entry, str):
            out.add(_entity_key(entry))
        elif isinstance(entry, dict):
            for field in ("subject", "name", "spelling", "key", "entity_key"):
                if entry.get(field):
                    out.add(_entity_key(str(entry[field])))
                    break
    return out


_ABSTENTION_MODES = frozenset({
    "screened", "project-unavailable", "no-start", "identity-conflict",
    "overflow", "budget-exceeded", "screened-rows", "no-answer",
})


def _blank_metrics() -> dict[str, Any]:
    return {
        "cases": 0,
        "expected_chains": 0,
        "matched_chains": 0,
        "returned_rows": 0,
        "allowed_rows": 0,
        "unmapped_rows": 0,
        "note_rows": 0,
        "forbidden_hits": 0,
        "leakage": 0,
        "abstention_cases": 0,
        "abstention_passes": 0,
        "mode_cases": 0,
        "mode_passes": 0,
        "abstain_mode_cases": 0,
        "abstain_mode_passes": 0,
        "answer_mode_cases": 0,
        "answer_mode_passes": 0,
        "marker_cases": 0,
        "marker_passes": 0,
        "strict_chain_cases": 0,
        "strict_chain_passes": 0,
        "errors": 0,
    }


def _hop_satisfied(expected: Any, pool_at_hop: set[str]) -> bool:
    """One hop of an expected chain.

    A plain record id must be present.  A list is a *sibling terminal group*
    (design section 5.4: several terminals sharing hops 1..n-1 are one chain,
    and the row cap shows only the strongest of them), so it is satisfied when
    any one of its members is present.
    """
    if isinstance(expected, (list, tuple)):
        return any(str(item) in pool_at_hop for item in expected)
    return str(expected) in pool_at_hop


def _score_case(memory: Any, case: dict[str, Any], seeded: dict[str, Any],
                index: dict[str, Any], forbidden_strings: list[str],
                ) -> dict[str, Any]:
    contains_secret, contains_private = _screens()
    logical_project = case.get("project")
    project_id = None
    project_scope = None
    if logical_project is not None:
        project_id = seeded["projects"][int(logical_project)]
        project_scope = f"project:{project_id}"
    try:
        seed_rows = memory.current_claims(
            case["question"], limit=8, project_id=project_id)[:4]
        lane_mode = str(memory.claim_recall_report().get("mode") or "")
    except Exception:  # noqa: BLE001 - a screened query is a lane outcome
        seed_rows = []
        lane_mode = "error"
    as_of = None
    if case.get("as_of"):
        anchor = seeded["rows"][str(case["as_of"]["record"])]["valid_from"]
        as_of = seeded["clock"].offset_from(
            anchor, float(case["as_of"]["offset_seconds"]))
    outcome: dict[str, Any] = {"error": None}
    repeats = 3 if case.get("latency") else 1
    elapsed_ms = None
    result: dict[str, Any] = {"rows": [], "overflow": [],
                              "report": {"mode": "error"}}
    for _ in range(repeats):
        started = time.perf_counter()
        try:
            result = invoke_graph_chains(
                memory,
                question=str(case["question"]),
                project_id=project_id,
                subjects=list(case["subjects"]),
                seed_claims=seed_rows,
                temporal=bool(case.get("temporal")),
                as_of=as_of,
                lane_mode=lane_mode,
                limit=8,
            )
        except Exception as error:  # noqa: BLE001
            outcome["error"] = f"{type(error).__name__}: {error}"
            result = {"rows": [], "overflow": [], "report": {"mode": "error"}}
        sample = (time.perf_counter() - started) * 1000.0
        elapsed_ms = sample if elapsed_ms is None else min(elapsed_ms, sample)
        if outcome["error"]:
            break
    outcome["elapsed_ms"] = elapsed_ms or 0.0
    rows = list(result.get("rows") or [])
    report = dict(result.get("report") or {})
    overflow_entries = list(result.get("overflow") or [])

    note_rows = [row for row in rows if _is_note_row(row)]
    answer_rows = [row for row in rows if not _is_note_row(row)]
    forbidden = set(case.get("forbid_record_ids") or [])
    allowed = set(case.get("allow_record_ids") or [])
    for chain in case.get("expect_chain") or []:
        for element in chain:
            if isinstance(element, (list, tuple)):
                allowed.update(str(item) for item in element)
            else:
                allowed.add(str(element))

    fallback_scope = project_scope or "global"
    returned_ids = [_row_record_id(row, index, fallback_scope)
                    for row in answer_rows]
    allowed_rows = 0
    unmapped = 0
    forbidden_hits = 0
    leakage = 0
    for row, record_id in zip(answer_rows, returned_ids):
        if record_id is None:
            unmapped += 1
        elif record_id in forbidden:
            forbidden_hits += 1
        elif record_id in allowed:
            allowed_rows += 1
        for field in ("subject", "value"):
            text = str(row.get(field) or "")
            if text and (contains_secret(text) or contains_private(text)):
                leakage += 1
        scope = str(row.get("scope") or "")
        if scope and not _scope_visible(scope, project_scope):
            leakage += 1
        elif not scope and record_id is not None:
            seeded_row = seeded["rows"].get(record_id)
            if seeded_row is not None and not _scope_visible(
                    str(seeded_row["scope"]), project_scope):
                leakage += 1
    leakage += forbidden_hits

    # Ruling 10.6 item 6: an overflow note naming the case's own subject, or a
    # subject the asking project can see on its own, is never a leak.  Only a
    # hub the project cannot see counts.
    project_visible_keys: set[str] = set(
        seeded["keys_by_scope"].get("global", set()))
    if project_scope:
        project_visible_keys |= seeded["keys_by_scope"].get(project_scope, set())
    named_keys = {_entity_key(name) for name in case.get("subjects") or []}
    for entry in list(overflow_entries) + note_rows:
        subject = _entity_key(str(entry.get("subject") or ""))
        if not subject or subject in named_keys or subject in project_visible_keys:
            continue
        forbidden_hits += 1
        leakage += 1

    serialized = json.dumps(result, default=str)
    echoed = sum(1 for probe in forbidden_strings if probe in serialized)
    leakage += echoed

    pool: dict[int, set[str]] = {}
    groups: dict[Any, dict[int, set[str]]] = {}
    for position, (row, record_id) in enumerate(zip(answer_rows, returned_ids)):
        if record_id is None:
            continue
        chain = row.get("chain")
        chain = 1 if chain is None else chain
        hop = row.get("hop")
        try:
            hop = int(hop)
        except (TypeError, ValueError):
            hop = position + 1
        pool.setdefault(hop, set()).add(record_id)
        groups.setdefault(chain, {}).setdefault(hop, set()).add(record_id)

    expected_chains = [list(chain) for chain in (case.get("expect_chain") or [])]
    matched = 0
    for chain in expected_chains:
        if all(_hop_satisfied(element, pool.get(offset + 1, set()))
               for offset, element in enumerate(chain)):
            matched += 1

    expect_abstain = case.get("expect_abstain")
    abstained = not answer_rows
    abstention_pass = None
    if expect_abstain is True:
        abstention_pass = abstained and not forbidden_hits
    elif expect_abstain is False:
        abstention_pass = bool(answer_rows)

    mode_pass = None
    mode_is_abstention = False
    if case.get("expect_mode"):
        mode_pass = str(report.get("mode") or "") == str(case["expect_mode"])
        mode_is_abstention = str(case["expect_mode"]) in _ABSTENTION_MODES

    marker_pass = None
    minimum_overflow = case.get("expect_overflow_min")
    if minimum_overflow is not None:
        marker_pass = (len(overflow_entries) + len(note_rows)) >= int(minimum_overflow)
    expect_incomplete = case.get("expect_incomplete")
    if expect_incomplete is not None:
        # Ruling 10.7 item 6 as refined: inside a sibling group the marking is
        # per row, so the case is satisfied by ANY returned row carrying it.
        marked = any(bool(row.get("incomplete")) for row in answer_rows)
        observed = (bool(answer_rows) and marked) if expect_incomplete else not marked
        marker_pass = observed if marker_pass is None else (marker_pass and observed)
    expect_unresolved = case.get("expect_unresolved")
    if expect_unresolved:
        wanted = {_entity_key(name) for name in expect_unresolved}
        observed = wanted <= _unresolved_names(report)
        marker_pass = observed if marker_pass is None else (marker_pass and observed)

    strict_pass = None
    if expected_chains:
        returned_paths = set()
        for group in groups.values():
            hops = sorted(group)
            if not hops:
                continue
            for terminal in group[hops[-1]]:
                path = [sorted(group[hop])[0] for hop in hops[:-1]] + [terminal]
                returned_paths.add(tuple(path))
        flattened = set()
        for chain in expected_chains:
            if any(isinstance(element, (list, tuple)) for element in chain):
                continue
            flattened.add(tuple(str(element) for element in chain))
        strict_pass = flattened <= returned_paths if flattened else None

    outcome.update({
        "case": case["id"],
        "kind": case["kind"],
        "mode": report.get("mode"),
        "lane_mode": lane_mode,
        "rows": len(answer_rows),
        "note_rows": len(note_rows),
        "overflow_entries": len(overflow_entries),
        "expected_chains": len(expected_chains),
        "matched_chains": matched,
        "allowed_rows": allowed_rows,
        "unmapped_rows": unmapped,
        "forbidden_hits": forbidden_hits,
        "leakage": leakage,
        "echoed_directives": echoed,
        "abstention_pass": abstention_pass,
        "mode_pass": mode_pass,
        "mode_is_abstention": mode_is_abstention,
        "marker_pass": marker_pass,
        "strict_pass": strict_pass,
        "unresolved": sorted(_unresolved_names(report)),
        "returned": list(returned_ids),
    })
    return outcome


def _evaluate_holdout(memory_module: Any, memory_factory: Any,
                      fixture: dict[str, Any]) -> dict[str, Any]:
    """Seed every store, score every case, and return the report."""
    metrics = _blank_metrics()
    per_kind: dict[str, dict[str, Any]] = {}
    latencies: list[float] = []
    details: list[dict[str, Any]] = []
    seeding: list[dict[str, Any]] = []
    cases_by_store: dict[str, list[dict[str, Any]]] = {}
    for case in fixture["cases"]:
        cases_by_store.setdefault(str(case["store"]), []).append(case)

    for store in fixture["stores"]:
        with memory_factory(store["id"]) as memory:
            seeded = _seed_store(memory_module, memory, store, fixture)
            index = _record_index(seeded)
            forbidden_strings = _screened_directive_strings(store, fixture)
            claim_rows = int(memory.db.execute(
                "SELECT COUNT(*) FROM memory_claims").fetchone()[0])
            seeding.append({
                "store": store["id"],
                "claims": claim_rows,
                "expected_claims": store["expected_counts"]["claims"],
                "tombstones": seeded["tombstones"],
                "expected_tombstones": store["expected_counts"]["tombstones"],
            })
            for case in cases_by_store.get(str(store["id"]), []):
                outcome = _score_case(memory, case, seeded, index,
                                      forbidden_strings)
                details.append(outcome)
                if case.get("latency"):
                    latencies.append(outcome["elapsed_ms"])
                bucket = per_kind.setdefault(str(case["kind"]), _blank_metrics())
                for counters in (metrics, bucket):
                    counters["cases"] += 1
                    if outcome["error"]:
                        counters["errors"] += 1
                    counters["expected_chains"] += outcome["expected_chains"]
                    counters["matched_chains"] += outcome["matched_chains"]
                    counters["returned_rows"] += outcome["rows"]
                    counters["allowed_rows"] += outcome["allowed_rows"]
                    counters["unmapped_rows"] += outcome["unmapped_rows"]
                    counters["note_rows"] += outcome["note_rows"]
                    counters["forbidden_hits"] += outcome["forbidden_hits"]
                    counters["leakage"] += outcome["leakage"]
                    if outcome["abstention_pass"] is not None:
                        counters["abstention_cases"] += 1
                        counters["abstention_passes"] += int(
                            outcome["abstention_pass"])
                    if outcome["mode_pass"] is not None:
                        counters["mode_cases"] += 1
                        counters["mode_passes"] += int(outcome["mode_pass"])
                        if outcome["mode_is_abstention"]:
                            counters["abstain_mode_cases"] += 1
                            counters["abstain_mode_passes"] += int(
                                outcome["mode_pass"])
                        else:
                            counters["answer_mode_cases"] += 1
                            counters["answer_mode_passes"] += int(
                                outcome["mode_pass"])
                    if outcome["marker_pass"] is not None:
                        counters["marker_cases"] += 1
                        counters["marker_passes"] += int(outcome["marker_pass"])
                    if outcome["strict_pass"] is not None:
                        counters["strict_chain_cases"] += 1
                        counters["strict_chain_passes"] += int(
                            outcome["strict_pass"])

    def finalize(counters: dict[str, Any]) -> dict[str, Any]:
        returned = counters["returned_rows"]
        expected = counters["expected_chains"]
        return {
            **counters,
            "chain_precision": (
                counters["allowed_rows"] / returned if returned else 1.0),
            "chain_recall": (
                counters["matched_chains"] / expected if expected else 1.0),
            "abstention_accuracy": (
                counters["abstention_passes"] / counters["abstention_cases"]
                if counters["abstention_cases"] else 1.0),
            "abstain_mode_accuracy": (
                counters["abstain_mode_passes"] / counters["abstain_mode_cases"]
                if counters["abstain_mode_cases"] else 1.0),
            # Reported, never gated (ruling 10.6 item 6): the name of a
            # successful read is not pinned by the design.
            "mode_accuracy": (
                counters["answer_mode_passes"] / counters["answer_mode_cases"]
                if counters["answer_mode_cases"] else 1.0),
            "marker_accuracy": (
                counters["marker_passes"] / counters["marker_cases"]
                if counters["marker_cases"] else 1.0),
            # Reported, never gated: recall is pooled by hop across chains.
            "strict_chain_rate": (
                counters["strict_chain_passes"] / counters["strict_chain_cases"]
                if counters["strict_chain_cases"] else 1.0),
        }

    ordered = sorted(latencies)
    if ordered:
        position = max(0, min(len(ordered) - 1,
                              int(round(0.95 * (len(ordered) - 1)))))
        p95 = ordered[position]
        mean_latency = statistics.fmean(ordered)
    else:
        p95 = 0.0
        mean_latency = 0.0
    return {
        "holdout": fixture["holdout"],
        "stores": len(fixture["stores"]),
        "cases": len(fixture["cases"]),
        "seeding": seeding,
        "aggregate": finalize(metrics),
        "kinds": {name: finalize(bucket)
                  for name, bucket in sorted(per_kind.items())},
        "latency": {
            "samples": len(ordered),
            "p95_ms": p95,
            "mean_ms": mean_latency,
            "max_ms": ordered[-1] if ordered else 0.0,
        },
        "failures": [
            outcome for outcome in details
            if (
                outcome["error"]
                or outcome["forbidden_hits"]
                or outcome["leakage"]
                or outcome["matched_chains"] != outcome["expected_chains"]
                or outcome["abstention_pass"] is False
                or outcome["mode_pass"] is False
                or outcome["marker_pass"] is False
                or outcome["unmapped_rows"]
            )
        ],
        "all": details,
    }
# -- END SEALED MEMORY GRAPH HOLDOUT V4 SCORER --


_SENSITIVE_PATTERNS = (
    r"(?i)https?://",
    r"(?i)[a-z]:[\\/](?:users|documents|desktop)[\\/]",
    r"(?i)\b[\w.+-]+@[\w.-]+\.[a-z]{2,}\b",
    r"\b(?:\d{1,3}\.){3}\d{1,3}\b",
    r"(?i)-----BEGIN [A-Z ]+-----",
    r"(?i)\bsk-[a-z0-9]{12,}\b",
    # split so this list does not match itself when the scan runs over this
    # module's own bytes
    r"(?i)\b(?:pass" + r"word|pass" + r"wd|api[_ -]?key|private[_ -]?key)\b",
)

_CASE_KINDS = frozenset({
    "forward2", "forward3", "reverse1", "reverse2", "reverse3",
    "asof_superseded", "after_forget", "after_erase", "cross_project",
    "global_shadowed", "secret_in_chain", "private_in_chain", "hub_overflow",
    "incomplete_chain", "lookalike", "alias", "world_knowledge", "two_subjects",
})

_REPORT_MODES = frozenset({
    "idle", "screened", "project-unavailable", "no-start", "identity-conflict",
    "overflow", "budget-exceeded", "screened-rows", "no-answer", "complete",
    "error",
})


class MemoryGraphHoldoutV4IntegrityTests(unittest.TestCase):
    """Unsealed checks.  These run in the ordinary suite and must stay green."""

    def setUp(self) -> None:
        self.fixture_bytes = FIXTURE_PATH.read_bytes()
        self.fixture = json.loads(self.fixture_bytes.decode("utf-8"))

    def test_fixture_and_scorer_are_sealed(self) -> None:
        if _seal_is_placeholder():
            self.skipTest(
                "the fixture and scorer digests are still placeholders: the "
                "boss stamps them with claude-reseal-runtime-pins.py before "
                "this holdout is scored"
            )
        self.assertEqual(
            hashlib.sha256(self.fixture_bytes).hexdigest(), FIXTURE_SHA256)
        self.assertEqual(
            hashlib.sha256(_sealed_scorer_bytes()).hexdigest(), SCORER_SHA256)
        self.assertEqual(
            _required_run_token(),
            hashlib.sha256(
                f"{FIXTURE_SHA256}:{SCORER_SHA256}".encode("ascii")).hexdigest())

    def test_runtime_pin_has_the_four_file_shape(self) -> None:
        pin = self.fixture["runtime_sha256"]
        self.assertEqual(tuple(pin), PINNED_FILES)
        self.assertNotIn("jarvis/agent.py", pin)
        for name in PINNED_FILES:
            self.assertRegex(pin[name], r"\A[0-9a-f]{64}\Z")

    def test_fixture_and_scorer_are_public_safe(self) -> None:
        material = self.fixture_bytes.decode("utf-8")
        module = Path(__file__).read_bytes().decode("utf-8")
        self.assertTrue(self.fixture["public_safe"])
        self.assertTrue(self.fixture["fictional_only"])
        for pattern in _SENSITIVE_PATTERNS:
            self.assertIsNone(re.search(pattern, material), pattern)
            self.assertIsNone(re.search(pattern, module), pattern)
        self.assertTrue(all(character in "\n\t" or 32 <= ord(character) <= 126
                            for character in material))
        self.assertNotIn(b"\r\n", self.fixture_bytes)
        self.assertTrue(self.fixture_bytes.endswith(b"\n"))

    def test_counts_and_case_structure(self) -> None:
        fixture = self.fixture
        expected = fixture["expected_counts"]
        stores = fixture["stores"]
        cases = fixture["cases"]
        store_ids = [str(store["id"]) for store in stores]
        case_ids = [str(case["id"]) for case in cases]
        record_ids = [str(record["id"]) for store in stores
                      for record in store["records"]
                      if record["op"] != "clock"]
        self.assertEqual(len(store_ids), len(set(store_ids)))
        self.assertEqual(len(case_ids), len(set(case_ids)))
        self.assertEqual(len(record_ids), len(set(record_ids)))
        self.assertEqual(len(stores), expected["stores"])
        self.assertEqual(len(cases), expected["cases"])
        self.assertEqual(len(record_ids), expected["records"])
        self.assertGreaterEqual(len(stores), 8)
        self.assertLessEqual(len(stores), 12)
        self.assertGreaterEqual(len(cases), 85)
        self.assertLessEqual(len(cases), 95)

        claim_ops = sum(1 for store in stores for record in store["records"]
                        if record["op"] == "claim")
        self.assertEqual(claim_ops, expected["claim_operations"])
        self.assertGreaterEqual(claim_ops, 300)
        self.assertGreaterEqual(expected["supersessions"], 40)
        self.assertGreaterEqual(expected["retractions"], 10)
        self.assertGreaterEqual(expected["erasures"], 6)
        self.assertGreaterEqual(expected["padding_claims"], 12000)

        known = set(record_ids)
        abstain = 0
        leakage_cases = 0
        latency_cases = 0
        for case in cases:
            self.assertIn(case["kind"], _CASE_KINDS)
            self.assertIn(str(case["store"]), set(store_ids))
            self.assertIsInstance(case["subjects"], list)
            self.assertIn("expect_chain", case)
            self.assertIn("allow_record_ids", case)
            self.assertIn(case.get("expect_abstain"), (True, False))
            if case.get("expect_mode"):
                self.assertIn(case["expect_mode"], _REPORT_MODES)
            forbidden = set(case.get("forbid_record_ids") or [])
            allowed = set(case.get("allow_record_ids") or [])
            expected_ids: set[str] = set()
            for chain in case.get("expect_chain") or []:
                for element in chain:
                    if isinstance(element, list):
                        expected_ids.update(str(item) for item in element)
                    else:
                        expected_ids.add(str(element))
            self.assertTrue(expected_ids <= known, case["id"])
            self.assertTrue(forbidden <= known, case["id"])
            self.assertTrue(allowed <= known, case["id"])
            self.assertTrue(forbidden.isdisjoint(allowed), case["id"])
            self.assertTrue(forbidden.isdisjoint(expected_ids), case["id"])
            if case["expect_abstain"] is True:
                abstain += 1
                self.assertEqual(case["expect_chain"], [], case["id"])
                self.assertTrue(str(case.get("reason") or "").strip(), case["id"])
            else:
                self.assertTrue(case["expect_chain"], case["id"])
            if case["kind"] in {"secret_in_chain", "private_in_chain"}:
                leakage_cases += 1
            if case.get("latency"):
                latency_cases += 1
        self.assertEqual(abstain, expected["abstention_cases"])
        self.assertGreaterEqual(abstain, 20)
        self.assertEqual(leakage_cases, expected["leakage_cases"])
        self.assertGreaterEqual(leakage_cases, 10)
        self.assertGreaterEqual(latency_cases, 8)

        counted: dict[str, int] = {}
        for case in cases:
            counted[case["kind"]] = counted.get(case["kind"], 0) + 1
        self.assertEqual(counted, expected["kinds"])
        self.assertEqual(set(counted), _CASE_KINDS)
        # Design 1.4 asks for six cases of every kind; eighteen kinds cannot all
        # carry six inside a ninety-question budget, so four is the floor here
        # and every kind clears it.
        for kind, count in counted.items():
            self.assertGreaterEqual(count, 4, kind)

    def test_thresholds_are_the_designed_ones(self) -> None:
        thresholds = self.fixture["thresholds"]
        self.assertEqual(thresholds["chain_precision"], 1.0)
        self.assertEqual(thresholds["chain_recall"], 0.90)
        self.assertEqual(thresholds["abstention_accuracy"], 1.0)
        self.assertEqual(thresholds["leakage"], 0)
        self.assertEqual(thresholds["store_p95_ms"], 25)

    def test_directives_expand_deterministically(self) -> None:
        fixture = self.fixture
        seed = int(fixture["generator_seed"])
        directives = [(record, field)
                      for store in fixture["stores"]
                      for record in store["records"]
                      for field in ("subject", "value")
                      if isinstance(record.get(field), dict)]
        self.assertGreaterEqual(len(directives), 24)
        seen_templates: set[str] = set()
        expansions: set[str] = set()
        screened_expansions: list[str] = []
        for record, field in directives:
            directive = record[field]
            first = _expand_directive(directive, seed, str(record["id"]), field)
            second = _expand_directive(directive, seed, str(record["id"]), field)
            self.assertEqual(first, second, record["id"])
            self.assertTrue(first)
            expansions.add(first)
            seen_templates.add(
                directive.get("value_template") or directive["spelling"])
            if str(record.get("screened_expectation") or "") == "screen":
                screened_expansions.append(first)
        self.assertGreaterEqual(len(seen_templates), 20)
        self.assertGreaterEqual(len(expansions), 24)
        material = self.fixture_bytes.decode("utf-8")
        for expansion in screened_expansions:
            self.assertNotIn(expansion, material)
        self.assertGreaterEqual(len(screened_expansions), 12)

    def test_screened_directives_actually_screen(self) -> None:
        contains_secret, contains_private = _screens()
        seed = int(self.fixture["generator_seed"])
        positives = 0
        negatives = 0
        for store in self.fixture["stores"]:
            for record in store["records"]:
                for field in ("subject", "value"):
                    directive = record.get(field)
                    if not isinstance(directive, dict) or "spelling" in directive:
                        continue
                    text = _expand_directive(directive, seed,
                                             str(record["id"]), field)
                    screened = bool(contains_secret(text)
                                    or contains_private(text))
                    wants = str(record.get("screened_expectation") or "") == "screen"
                    self.assertEqual(screened, wants,
                                     f"{record['id']}.{field} "
                                     f"{directive.get('value_template')}")
                    positives += int(wants)
                    negatives += int(not wants)
        self.assertGreaterEqual(positives, 12)
        self.assertGreaterEqual(negatives, 10)

    def test_spelling_directives_fold_as_section_two_one_says(self) -> None:
        seed = int(self.fixture["generator_seed"])
        confusable = _expand_directive(
            {"spelling": "nfkc_confusable", "base": "Immerly hive"},
            seed, "probe", "subject")
        homoglyph = _expand_directive(
            {"spelling": "homoglyph", "base": "Immerly hive"},
            seed, "probe", "subject")
        self.assertNotEqual(confusable, "Immerly hive")
        self.assertEqual(_entity_key(confusable), "immerly hive")
        self.assertNotEqual(homoglyph, "Immerly hive")
        self.assertNotEqual(_entity_key(homoglyph), "immerly hive")

    def test_every_expected_chain_is_self_consistent(self) -> None:
        """Chains, forbidden ids and markers agree with the fixture script."""
        fixture = self.fixture
        records: dict[str, dict[str, Any]] = {}
        store_of: dict[str, str] = {}
        for store in fixture["stores"]:
            for record in store["records"]:
                if record["op"] == "clock":
                    continue
                records[str(record["id"])] = record
                store_of[str(record["id"])] = str(store["id"])
        for case in fixture["cases"]:
            case_store = str(case["store"])
            referenced: set[str] = set(case.get("forbid_record_ids") or [])
            referenced.update(case.get("allow_record_ids") or [])
            for chain in case.get("expect_chain") or []:
                self.assertLessEqual(len(chain), 3, case["id"])
                for element in chain:
                    if isinstance(element, list):
                        self.assertTrue(element, case["id"])
                        referenced.update(str(item) for item in element)
                    else:
                        referenced.add(str(element))
            for record_id in referenced:
                self.assertEqual(store_of.get(record_id), case_store,
                                 f"{case['id']} references {record_id}")
                self.assertEqual(records[record_id]["op"], "claim", record_id)
            if case.get("as_of"):
                anchor = str(case["as_of"]["record"])
                self.assertEqual(store_of.get(anchor), case_store, case["id"])
            if case["expect_abstain"] is True:
                self.assertFalse(case.get("expect_incomplete"), case["id"])
            if case.get("expect_mode") in {"identity-conflict", "no-start"}:
                self.assertTrue(case["expect_abstain"], case["id"])
            if case.get("expect_unresolved"):
                self.assertEqual(case["kind"], "two_subjects", case["id"])

    def test_no_screened_record_is_ever_expected_or_allowed(self) -> None:
        """A record whose expansion screens must never be an expected answer."""
        fixture = self.fixture
        screened: set[str] = set()
        for store in fixture["stores"]:
            for record in store["records"]:
                if str(record.get("screened_expectation") or "") == "screen":
                    screened.add(str(record["id"]))
        self.assertGreaterEqual(len(screened), 12)
        for case in fixture["cases"]:
            allowed = set(case.get("allow_record_ids") or [])
            for chain in case.get("expect_chain") or []:
                for element in chain:
                    if isinstance(element, list):
                        allowed.update(str(item) for item in element)
                    else:
                        allowed.add(str(element))
            self.assertTrue(allowed.isdisjoint(screened), case["id"])

    def test_case_subjects_match_the_agent_parser(self) -> None:
        """The baked subject lists are what ``agent.py`` would emit today.

        The agent is deliberately not pinned (design 1.4), so a drift here is a
        development-battery finding and this test is unsealed: it reports the
        drift without touching the sealed scorer or the run token.
        """
        try:
            from jarvis.agent import _named_fact_subjects
        except Exception as error:  # noqa: BLE001
            self.skipTest(f"agent parser unavailable: {error}")
        drift = []
        for case in self.fixture["cases"]:
            produced = list(_named_fact_subjects(str(case["question"])))
            if produced != list(case["subjects"]):
                drift.append((case["id"], case["question"],
                              case["subjects"], produced))
        self.assertEqual(drift, [], "baked subjects drifted from the parser")

    def test_seeding_scripts_are_well_formed(self) -> None:
        for store in self.fixture["stores"]:
            live: set[tuple[str, str, str]] = set()
            for record in store["records"]:
                operation = str(record["op"])
                if operation == "clock":
                    self.assertGreater(float(record["advance_seconds"]), 0)
                    continue
                scope = str(record["scope"])
                self.assertTrue(scope == "global" or scope.startswith("project:"))
                if scope.startswith("project:"):
                    self.assertIn(int(scope.split(":")[1]), store["projects"])
                subject = record["subject"]
                key = (scope, json.dumps(subject, sort_keys=True),
                       str(record["predicate"]))
                if operation == "claim":
                    self.assertIn("value", record)
                    self.assertIn(record["authority"],
                                  {"external", "learned", "verified", "operator"})
                    self.assertGreaterEqual(float(record["confidence"]), 0.0)
                    self.assertLessEqual(float(record["confidence"]), 1.0)
                    live.add(key)
                else:
                    # forget and erase only ever address a key the script wrote
                    self.assertIn(key, live, f"{store['id']} {record['id']}")
                    if operation == "erase":
                        live.discard(key)


class MemoryGraphHoldoutV4ProductionTests(unittest.TestCase):
    """The sealed gate.  Scored once, by the boss, with the token supplied."""

    def test_sealed_memory_graph_holdout_v4(self) -> None:
        fixture = _load_fixture()
        if _seal_is_placeholder() or _pin_is_placeholder(fixture):
            self.skipTest(
                "the runtime pin or the fixture and scorer digests are still "
                "placeholders: the boss reseals the four pinned files with "
                "claude-reseal-runtime-pins.py before this holdout is scored"
            )
        if os.environ.get(TOKEN_ENVIRONMENT_VARIABLE) != _required_run_token():
            self.skipTest("sealed graph holdout v4 run token was not supplied")

        from jarvis.memory import Memory
        import jarvis.memory as memory_module

        if not hasattr(Memory, "graph_chains"):
            self.skipTest(
                "Memory.graph_chains is absent: the M3 graph channel is not "
                "present in this tree")
        pin = _runtime_pin_now()
        self.assertEqual(
            pin,
            {name: fixture["runtime_sha256"][name] for name in PINNED_FILES},
            "runtime pin mismatch")

        with tempfile.TemporaryDirectory(prefix="jarvis-graph-holdout-v4-") as root:
            def factory(store_id: str) -> Any:
                return Memory(Path(root) / f"holdout-{store_id}.db")

            report = _evaluate_holdout(memory_module, factory, fixture)

        print(json.dumps(report, sort_keys=True, indent=2, default=str))
        thresholds = fixture["thresholds"]
        aggregate = report["aggregate"]
        for entry in report["seeding"]:
            self.assertEqual(entry["claims"], entry["expected_claims"],
                             entry["store"])
            self.assertEqual(entry["tombstones"], entry["expected_tombstones"],
                             entry["store"])
        self.assertEqual(aggregate["errors"], 0)
        self.assertEqual(aggregate["unmapped_rows"], 0)
        self.assertEqual(aggregate["leakage"], thresholds["leakage"])
        self.assertEqual(aggregate["forbidden_hits"], 0)
        self.assertGreaterEqual(aggregate["chain_precision"],
                                thresholds["chain_precision"])
        self.assertGreaterEqual(aggregate["chain_recall"],
                                thresholds["chain_recall"])
        self.assertGreaterEqual(aggregate["abstention_accuracy"],
                                thresholds["abstention_accuracy"])
        # Only the abstention modes gate: sections 2.3, 3.2 and 5.6 and ruling
        # 10.7 item 5 pin those verbatim (ruling 10.6 item 6), while the name of
        # a successful read is reported so a naming drift is visible without
        # failing the gate.
        self.assertEqual(aggregate["abstain_mode_accuracy"], 1.0)
        self.assertEqual(aggregate["marker_accuracy"], 1.0)
        self.assertLessEqual(report["latency"]["p95_ms"],
                             thresholds["store_p95_ms"])


if __name__ == "__main__":
    unittest.main()
