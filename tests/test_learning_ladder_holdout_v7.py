"""Sealed one-use holdout for the VTMF M4 learning ladder (v7).

Authored by an independent agent that read only sections 1-6, 7.14 and all of
10.7 of ``VTMF_M4_LEARNING_LADDER_DESIGN.md`` (revision 3), the public
signatures and docstrings of ``jarvis.memory.Memory``,
``jarvis.learning_ladder`` and ``jarvis.skill_library``, and the *plumbing* of
the spent v6 holdout as a sealing template.  That agent never read the body of
``jarvis/memory.py``, ``jarvis/learning_ladder.py`` or ``jarvis/agent.py``,
never read the M4 tests or any development battery, never read the v1-v6
fixtures, and never read a score log.

v1 through v6 were each scored once and are quarantined (rulings 27, 30, 31,
32, 33, 34, 36).  None of their domains, store names, case identifiers,
markers or vocabulary is reused: every candidate term in this fixture was
scanned against all six quarantined directories before it was chosen.  The
domain here is new and fictional -- an inland canal lock flight, where
lengthsmen draw ground paddles, set rymer and needle, watch the cill and the
quoin, and close a tailbay without a slam.  Nothing in the fixture or this
module names a real person, host, product or place.

**The six ways a previous holdout killed itself, each now a rule here.**

*Ruling 31 (v3): seed through the sealed path, and prove it.*
``test_every_store_seeds_through_the_sealed_path`` runs the sealed ``_Replay``
over every store, in fixture order, with every tamper, ``PRAGMA
foreign_keys`` on, a fresh temporary workspace per store, and asserts each
store's ``expected_counts`` and every record-level expectation.  No tamper
deletes a row another row references: the only ``DELETE`` is of a
``ladder_promotions`` row, and ``test_nothing_references_the_row_a_tamper_
deletes`` proves that from the live schema rather than from prose.

*Ruling 34 P1 (v5): the leak material is the model-facing block and nothing
else.*  Neither ``approved_skills`` nor ``skill_channel_report`` returns the
confirmation code; only ``stage_ladder_promotion``'s return does, by design.
``_CaseRunner._leakage`` therefore scans exactly what the model would be
shown -- the matched lesson rows and the returned skill documents -- and never
a report, never the staging return, never a ``ladder_promotions`` row.  That
the code stays out of the store's own durable text is a separate check
(``no_code_in_store``) over ``messages``, ``activity_log`` and the spine.

*Ruling 34 P3 (v5): identity scope uses single-token subjects.*  Every lesson
in the identity corpus is shaped ``For <Name>, ...`` with a one-word subject.
A compound subject collapses identity to its head word -- a pre-M4 resolver
limitation deferred to M4.1 -- and is deliberately not exercised.

*Ruling 32(c) (v4): withdrawal sweeps are per project.*  Each planted state a
case must observe on its own read sits in its own project (``lck-tamper`` has
five) or its own store, and
``test_a_planted_state_is_read_before_its_project_is_swept`` enforces the
ordering.  A deferred withdrawal is the exception and stays visible for ever
(ruling 30).

*Ruling 34 P4 (v5): rollback and reinstatement are ledger-inert.*  They write
no ledger row and seal no epoch, so no case expects a monotonicity verdict to
move across one.  Every epoch sequence here is written by the ``seal`` op.

*Ruling 36 (v6), the two that cost the last run.*  First, a bare
``ladder_pending_withdrawals`` is row backed, and a parked orphan has no row
by definition, so ``call_pending`` always passes ``workspace=``: a bare call
genuinely cannot decide "parked" without the live set, and scoring it as a
product failure was the holdout's error, not the store's.  Second, the M1
ranker gates ``_rank_memory_rows`` on ``minimum_matches`` BEFORE scoring --
one matched term is enough only when the query reduces to two terms or the
longest matched term reaches seven characters.  Every ``inject_hit`` case
here therefore either reduces to two query terms or names a marker of at
least seven characters, each proved to retrieve on the frozen tree during the
build (``test_every_injection_hit_clears_the_ranker_precondition`` pins the
structural half).  And because the limitation is real, PRE-EXISTING and
routed to M4.1 rather than papered over, ``abstain-kelnop-anchor`` pins the
abstention as CORRECT: a three-term question naming a six-character marker
returns nothing, and the case gates that emptiness while only reporting the
mode, so the exit gate records the limitation instead of designing around it.

**What is gated, and what is only reported.**  A holdout is worth its one use
only if every gated expectation follows from the design rather than from an
implementation the author may not read.  The guarantees always gate: no
substitution, nothing unverified reaching the model, no forbidden ref, no
leak, no staged or parked document in a catalog, the store-wide unverified
bucket, rollback byte-exactness, and every closed-set refusal reason.  Where
the design leaves the outcome to the ranker or the resolver -- which of the
three substitution refusals fires, whether a retired lesson's successor comes
back, what a family with only a legacy document reports -- the case carries
``mode_gated: false`` or ``skill_mode_gated: false`` and the report prints the
agreement rate beside the gated one.  Where the design names more than one
refusal for one state, ``expect_lesson_mode`` / ``expect_skill_mode`` /
``expect_refusal`` may be a **list**, and the gate is membership in that closed
set -- never a wildcard.

**Scoring is store side only** (design 7.14).  The scorer imports ``Memory``,
``learning_ladder`` and ``skill_library`` and nothing else -- never ``Agent``,
never ``proactive``, never ``tools``.  It reproduces the Agent's precondition
chain itself, in the order S-7 fixes: the calibrated gate first, then
``skill_channel_report`` always and bare, then ``match_lessons`` **only when
the gate allows**, then ``abstention_cue_expected`` with the required
keyword-only ``withheld_candidates`` of ruling 10.

**One-use is procedural, not cryptographic.**  The run token is derivable from
the two digests by anyone holding the files; it is a tamper seal, not a
secret.  The boss scores this fixture exactly once against a frozen runtime
pin and records the result.

Three parts sit deliberately outside the sealed region so a signature drift
can be shimmed without breaking the seal or the run token: the ``call_*``
shims, ``PINNED_FILES``, and the placeholder handling.  Everything that
decides a number is inside the seal.
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
    / "learning_ladder_holdout_v7.json"
)
FIXTURE_SHA256 = "bbc9d0a8d216dbf2a2547d3713094dd85df08f416afc74285720e84d9c89352c"
SCORER_SHA256 = "0ed826db90629a69fd97c448142c4ffa3b45434171aa20823d2ed16e60ce2e4e"
SCORER_START = "# -- BEGIN SEALED LEARNING LADDER HOLDOUT V7 SCORER --"
SCORER_END = "# -- END SEALED LEARNING LADDER HOLDOUT V7 SCORER --"
TOKEN_ENVIRONMENT_VARIABLE = "JARVIS_LEARNING_LADDER_HOLDOUT_V7_TOKEN"

# Exactly the files ``learning_ladder.LADDER_RUNTIME_FILES`` lists, in its
# order.  ``jarvis/agent.py``, ``jarvis/proactive.py`` and ``jarvis/tools.py``
# are deliberately absent (design 1.5): the scored path is store side, the
# fixture bakes the five effective gate thresholds so the meta-gate module is
# never imported, and the staging root's file-tool refusal has its own
# behavioural test in 7.6.
PINNED_FILES = (
    "jarvis/learning_ladder.py",
    "jarvis/memory.py",
    "jarvis/skill_evolution.py",
    "jarvis/skill_library.py",
)
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
    seals = "{0}:{1}".format(FIXTURE_SHA256, SCORER_SHA256).encode("ascii")
    return hashlib.sha256(seals).hexdigest()


def _seal_is_placeholder() -> bool:
    return PLACEHOLDER_DIGEST in {FIXTURE_SHA256, SCORER_SHA256}


def _pin_is_placeholder(fixture: dict[str, Any]) -> bool:
    pin = fixture["runtime_sha256"]
    return any(pin.get(name) == PLACEHOLDER_DIGEST for name in PINNED_FILES)


def _runtime_pin_now() -> dict[str, str]:
    digests: dict[str, str] = {}
    for name in PINNED_FILES:
        path = REPOSITORY_ROOT / name
        digests[name] = (
            hashlib.sha256(path.read_bytes()).hexdigest() if path.exists()
            else ""
        )
    return digests


# ---------------------------------------------------------------------------
# Shims.  Design 8.1 names each signature; if one drifts the boss adapts the
# shim here and neither the sealed region nor the run token moves.
# ---------------------------------------------------------------------------
def call_gate(memory: Any, family: str, thresholds: dict[str, Any]
              ) -> dict[str, Any]:
    return memory.calibration_gate(family, **thresholds)


def call_skill_report(ladder: Any, *, workspace: Path, memory: Any,
                      family: str, project_id: int | None,
                      gate: dict[str, Any] | None) -> dict[str, Any]:
    return ladder.skill_channel_report(
        workspace=workspace, memory=memory, family=family,
        project_id=project_id, gate=gate)


def call_match_lessons(memory: Any, question: str, family: str, *,
                       project_id: int | None, limit: int
                       ) -> list[dict[str, Any]]:
    return memory.match_lessons(question, family, limit=limit,
                                project_id=project_id)


def call_cue(ladder: Any, lesson_mode: str, skill_mode: str, *,
             withheld_candidates: int) -> bool:
    return bool(ladder.abstention_cue_expected(
        lesson_mode, skill_mode, withheld_candidates=withheld_candidates))


def call_candidate_count(memory: Any, family: str, project_id: int, *,
                         limit: int) -> int:
    return int(memory.lesson_candidate_count(family, project_id, limit=limit))


def call_approved_skills(ladder: Any, *, workspace: Path, memory: Any,
                         family: str, project_id: int, limit: int = 2
                         ) -> list[dict[str, Any]]:
    return ladder.approved_skills(workspace=workspace, memory=memory,
                                  family=family, project_id=project_id,
                                  limit=limit)


def call_stage(memory: Any, *, family: str, project_id: int, workspace: Path
               ) -> dict[str, Any]:
    return memory.stage_ladder_promotion(family=family, project_id=project_id,
                                         workspace=workspace)


def call_approve(memory: Any, promotion_id: int, *, approval_token: str,
                 workspace: Path) -> dict[str, Any]:
    return memory.apply_ladder_promotion(promotion_id,
                                         approval_token=approval_token,
                                         workspace=workspace)


def call_rollback(memory: Any, promotion_id: int, *, workspace: Path
                  ) -> dict[str, Any]:
    return memory.rollback_ladder_promotion(promotion_id, workspace=workspace)


def call_discard(memory: Any, promotion_id: int, *, workspace: Path
                 ) -> dict[str, Any]:
    return memory.discard_ladder_promotion(promotion_id, workspace=workspace)


def call_seal(memory: Any, family: str, *, workspace: Path
              ) -> list[dict[str, Any]]:
    return memory.seal_calibration_epoch(family, workspace=workspace)


def call_grandfather(memory: Any, workspace: Path, *, project_id: int
                     ) -> dict[str, Any]:
    return memory.grandfather_ladder(workspace, project_id=project_id)


def call_proof(memory: Any, *, family: str, project_id: int) -> dict[str, Any]:
    return memory.ladder_proof(family=family, project_id=project_id)


def call_unverified(memory: Any, *, workspace: Path, project_id: int | None
                    ) -> list[dict[str, Any]]:
    return memory.ladder_unverified_promotions(workspace=workspace,
                                               project_id=project_id)


def call_legacy(memory: Any, *, workspace: Path, project_id: int | None
                ) -> list[dict[str, Any]]:
    return memory.ladder_legacy_documents(workspace=workspace,
                                          project_id=project_id)


def call_pending(memory: Any, project_id: int | None, *, workspace: Path
                 ) -> list[dict[str, Any]]:
    """Ruling 36, the author fact that cost v6 three checks.

    ``ladder_pending_withdrawals`` is ROW backed when it is called bare, and a
    parked orphan has no promotion row by definition -- its pending state is
    derived from the spine against the live set (ruling 35).  A bare call
    genuinely cannot decide "parked" without that set, so the live workspace
    is always supplied here.  v6 called it bare, scored three correct
    refusals as failures, and lost the run on the recall threshold instead.
    """
    return memory.ladder_pending_withdrawals(project_id, workspace=workspace)


def call_monotonicity(memory: Any, family: str) -> dict[str, Any]:
    return memory.calibration_ledger_monotonicity(family)


def call_verify_ledger(memory: Any, family: str | None = None
                       ) -> dict[str, Any]:
    return memory.verify_calibration_ledger(family)


def call_ledger(memory: Any, family: str) -> list[dict[str, Any]]:
    return memory.calibration_ledger(family)


# -- BEGIN SEALED LEARNING LADDER HOLDOUT V7 SCORER --
LEARNED_DIRECTORY = ".jarvis-skills"
STAGED_DIRECTORY = ".jarvis-skills-staging"
WITHDRAWN_PREFIX = "withdrawn-"
CUE_LESSON_MODES = frozenset({
    "screened", "authority-evasion", "project-ambiguous", "pool-overflow",
    "error", "unknown-identity", "cross-family-stronger", "out-of-project",
    "cross-project-stronger", "none-eligible", "ineligible-shadow",
    "ineligible-prefix"})
CUE_SKILL_MODES = frozenset({"gate-closed", "unverified-withdrawn"})
CUE_CONDITIONAL_SKILL_MODES = frozenset({"gate-closed"})
_ALPHABET = "abcdefghijklmnopqrstuvwxyz"
_HEX = "0123456789abcdef"
_REACHES = ("Tailbay", "Summit", "Byewash", "Quoin", "Riser", "Invert")
_PARISHES = ("Tunbeck", "Cawlode", "Millgarth", "Ravensty", "Osterlade",
             "Fallowmere")
_HOME_ROOT = "C" + ":" + chr(92) + "Users" + chr(92)


def _load_fixture() -> dict[str, Any]:
    return json.loads(FIXTURE_PATH.read_bytes().decode("utf-8"))


def _stream(seed: int, record_id: str, field: str, count: int) -> bytes:
    """A deterministic byte stream for one (record, field) directive."""
    material = "{0}:{1}:{2}".format(seed, record_id, field).encode("utf-8")
    out = b""
    counter = 0
    while len(out) < count:
        out += hashlib.sha256(material + str(counter).encode("ascii")).digest()
        counter += 1
    return out[:count]


def _pick(stream: bytes, index: int, choices: Any) -> Any:
    return choices[stream[index % len(stream)] % len(choices)]


def _digits(stream: bytes, start: int, count: int, *, first_min: int = 0
            ) -> str:
    out = []
    for offset in range(count):
        value = stream[(start + offset) % len(stream)] % 10
        if offset == 0 and value < first_min:
            value = first_min + (value % max(1, 10 - first_min))
        out.append(str(value))
    return "".join(out)


def _luhn_tail(body: str) -> str:
    total = 0
    for position, character in enumerate(reversed(body)):
        digit = int(character)
        if position % 2 == 0:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return str((10 - (total % 10)) % 10)


def _expand_directive(directive: dict[str, Any], seed: int, record_id: str,
                      field: str) -> str:
    """Expand one generator directive into public-safe, screen-tripping text.

    The expansions never appear literally in the fixture bytes, which is what
    keeps ``scripts/check_public_release.py`` and the public-safety integrity
    test green while still driving the privacy screens for real.
    """
    template = str(directive["value_template"])
    stream = _stream(seed, record_id, field, 96)
    if template == "secret_token":
        body = "".join(_pick(stream, index, _ALPHABET + "0123456789")
                       for index in range(28))
        return "sk-" + body
    if template == "secret_assignment":
        body = "".join(_pick(stream, index + 3, _HEX) for index in range(32))
        return "api" + "_key=" + body
    if template == "ipv4_private":
        return "10.{0}.{1}.{2}".format(_digits(stream, 5, 2),
                                       _digits(stream, 7, 2),
                                       _digits(stream, 9, 2, first_min=1))
    if template == "ipv4_exempt":
        return "127.0.0." + _digits(stream, 11, 1, first_min=1)
    if template == "phone_us":
        return "({0}) {1}-{2}".format(_digits(stream, 13, 3, first_min=2),
                                      _digits(stream, 16, 3, first_min=2),
                                      _digits(stream, 19, 4))
    if template == "ssn":
        return "{0}-{1}-{2}".format(_digits(stream, 23, 3, first_min=1),
                                    _digits(stream, 26, 2, first_min=1),
                                    _digits(stream, 28, 4, first_min=1))
    if template == "card_luhn":
        body = "4" + _digits(stream, 31, 14)
        return body + _luhn_tail(body)
    if template == "street_address":
        return "{0} {1} {2}, {3}".format(
            _digits(stream, 37, 3, first_min=1),
            _pick(stream, 40, _PARISHES), _pick(stream, 41, _REACHES),
            _pick(stream, 42, _PARISHES))
    if template == "email_person":
        first = "".join(_pick(stream, 43 + index, _ALPHABET)
                        for index in range(6))
        host = "".join(_pick(stream, 51 + index, _ALPHABET)
                       for index in range(7))
        return first + "@" + host + ".example"
    if template == "user_home_windows":
        who = "".join(_pick(stream, 59 + index, _ALPHABET)
                      for index in range(6))
        return _HOME_ROOT + who + chr(92) + "Documents" + chr(92) + "flight"
    if template == "version":
        return "{0}.{1}.{2}".format(_digits(stream, 66, 1),
                                    _digits(stream, 67, 2),
                                    _digits(stream, 69, 1))
    if template == "isbn13":
        body = "978" + _digits(stream, 71, 9)
        return body + _luhn_tail(body)
    if template == "tool_name":
        head = "".join(_pick(stream, 81 + index, _ALPHABET)
                       for index in range(5))
        tail = "".join(_pick(stream, 87 + index, _ALPHABET)
                       for index in range(4))
        return head + "_" + tail
    if template == "long_value":
        return "".join(_pick(stream, index, _ALPHABET) for index in range(90))
    if template == "long_prose":
        words = ["the", "pound", "cill", "rymer", "windlass", "lengthsman",
                 "penstock", "gunwale"]
        return " ".join(_pick(stream, index, words) for index in range(60))
    raise AssertionError("unknown value_template " + template)


def _text_or_directive(value: Any, seed: int, record_id: str,
                       field: str) -> str | None:
    if value is None:
        return None
    if isinstance(value, dict):
        return _expand_directive(value, seed, record_id, field)
    return str(value)


class _StoreClock:
    """A monotonic, injectable, time-zone aware store clock.

    Aware on purpose: ``jarvis.memory.now_iso`` returns an aware stamp, and a
    naive one makes ``remember_verified_lesson`` refuse for want of a valid
    observation time -- which would silently produce a store with no lessons
    in it at all and a holdout that scored nothing.

    Every moment is derived from one run anchor, so a store seeded now and the
    same store seeded tomorrow age identically relative to the 180-day lesson
    window the read path applies at *scoring* time, outside this clock.
    """

    def __init__(self, anchor: Any, start_offset_days: float,
                 tick_seconds: float) -> None:
        from datetime import timedelta

        self.anchor = anchor
        self.timedelta = timedelta
        self.moment = anchor + timedelta(days=float(start_offset_days))
        self.tick = float(tick_seconds)

    def set_offset_days(self, offset_days: float) -> None:
        self.moment = self.anchor + self.timedelta(days=float(offset_days))

    def advance(self, seconds: float) -> None:
        self.moment = self.moment + self.timedelta(seconds=float(seconds))

    def __call__(self) -> str:
        self.advance(self.tick)
        return self.moment.isoformat()


def _statuses(record: dict[str, Any]) -> str:
    """One symbol per outcome: ``+`` complete, ``-`` failed.

    Symbols rather than letters, so a run of statuses is never a word in the
    fixture's own bytes and the public-safety and vocabulary scans look at
    prose instead of at an encoding.
    """
    repeat = int(record.get("repeat") or 1)
    value = record.get("statuses")
    return "+" * repeat if value is None else str(value)


def _evidence(record: dict[str, Any], statuses: str) -> str:
    """``1`` evidence-backed, ``0`` not, ``?`` not applicable."""
    value = record.get("evidence")
    if value is not None:
        return str(value)
    return "".join("1" if character == "+" else "0" for character in statuses)


class _Replay:
    """Replay one store's ordered script through the public write API."""

    def __init__(self, memory_module: Any, memory: Any, store: dict[str, Any],
                 fixture: dict[str, Any], root: Path, skill_library: Any,
                 ladder: Any, anchor: Any) -> None:
        self.memory_module = memory_module
        self.memory = memory
        self.store = store
        self.fixture = fixture
        self.seed = int(fixture["generator_seed"])
        self.root = root
        self.skill_library = skill_library
        self.ladder = ladder
        clock = fixture["clock"]
        self.clock = _StoreClock(anchor, float(store.get(
            "start_offset_days", clock["start_offset_days"])),
            float(clock["tick_seconds"]))
        self.projects: dict[int, int] = {}
        self.workspaces: dict[int, Path] = {}
        self.decoy = root / "unbound-workspace"
        self.conversations: dict[str, int] = {}
        self.lessons: dict[str, int] = {}
        self.predictions: dict[str, int] = {}
        self.promotions: dict[str, int] = {}
        self.codes: dict[str, str] = {}
        self.results: dict[str, dict[str, Any]] = {}
        self.timings: dict[str, float] = {}
        self.live_before_approve: dict[str, bytes | None] = {}
        self.screened: list[str] = []
        self.script_checks: list[dict[str, Any]] = []

    # ------------------------------------------------------------ utilities
    def workspace(self, logical: int | None, mode: str = "self") -> Path:
        if mode == "decoy":
            return self.decoy
        if mode.startswith("other:"):
            return self.workspaces[int(mode.split(":", 1)[1])]
        return self.workspaces[int(logical)]

    def live_path(self, workspace: Path, name: str) -> Path:
        """The LIVE root, and nothing else (ruling 30 defect c)."""
        return workspace / LEARNED_DIRECTORY / name / "SKILL.md"

    def conversation(self, key: str, project: int) -> int:
        if key not in self.conversations:
            title = "holdout {0} {1}".format(self.store["id"], key)
            self.conversations[key] = self.memory.new_conversation(
                title[:80], project_id=self.projects[project])
        return self.conversations[key]

    def lesson_ids(self) -> set[int]:
        return {int(row[0]) for row in self.memory.db.execute(
            "SELECT id FROM memories WHERE kind='lesson'")}

    def row_workspace(self, row: dict[str, Any], mode: str = "self") -> Path:
        logical = None
        for key, value in self.projects.items():
            if int(value) == int(row["project_id"]):
                logical = key
                break
        if logical is None:
            logical = int(self.store["projects"][0])
        return self.workspace(logical, mode)

    def read_live(self, workspace: Path, name: str) -> bytes | None:
        """Exactly the live document, or ``None`` when nothing is live.

        Ruling 30 defect (c): v2's snapshot returned bytes for a skill that
        had nothing live, so a correct "removed" rollback scored as a failed
        byte comparison.  There is one path to the bytes and it is the live
        root; the staged and parked roots are never consulted here.
        """
        path = self.live_path(workspace, name)
        return path.read_bytes() if path.is_file() else None

    @staticmethod
    def guarded(call: Any, *arguments: Any, **keywords: Any) -> dict[str, Any]:
        """Call one ladder verb; turn a raise into a recorded refusal.

        Ruling 27 one layer up: a store method that raises must not abort a
        whole store's seeding, or every case behind it scores nothing and the
        run reports an error instead of a number.  The reason is prefixed
        ``raised:`` so it can never be mistaken for one of the design's closed
        refusal codes.
        """
        try:
            return dict(call(*arguments, **keywords))
        except Exception as error:                   # noqa: BLE001
            return {"reason": "raised:" + type(error).__name__,
                    "raised": "{0}: {1}".format(type(error).__name__, error)}

    def note_script(self, record: dict[str, Any], observed: dict[str, Any]
                    ) -> None:
        expected = record.get("expect")
        if not expected:
            return
        wrong = []
        for key in sorted(expected):
            want = expected[key]
            got = observed.get(key)
            if isinstance(want, list) and key == "reason":
                if got not in want:
                    wrong.append((key, want, got))
            elif isinstance(want, list):
                if sorted(got or []) != sorted(want):
                    wrong.append((key, want, got))
            elif got != want:
                wrong.append((key, want, got))
        self.script_checks.append({
            "record": str(record["id"]), "op": str(record["op"]),
            "pass": not wrong,
            "wrong": [{"field": item[0], "expected": item[1],
                       "observed": item[2]} for item in wrong]})

    # ----------------------------------------------------------------- run
    def run(self) -> None:
        real_now = self.memory_module.now_iso
        self.memory_module.now_iso = self.clock
        try:
            self.decoy.mkdir(parents=True, exist_ok=True)
            for value in self.store["projects"]:
                logical = int(value)
                slug = str(self.store["paths"][str(logical)])
                self.projects[logical] = self.memory.add_project(
                    "Holdout " + slug, "@projects/" + slug)
                path = self.root / slug
                path.mkdir(parents=True, exist_ok=True)
                self.workspaces[logical] = path
            for record in self.store["records"]:
                handler = getattr(self, "_op_" + str(record["op"]))
                handler(record)
        finally:
            self.memory_module.now_iso = real_now
            self.ladder.clear_catalog_cache()

    # ------------------------------------------------------------- writers
    def _op_clock(self, record: dict[str, Any]) -> None:
        if record.get("set_offset_days") is not None:
            self.clock.set_offset_days(float(record["set_offset_days"]))
        if record.get("advance_seconds"):
            self.clock.advance(float(record["advance_seconds"]))
        self.results[str(record["id"])] = {"op": "clock"}

    def _op_outcomes(self, record: dict[str, Any]) -> None:
        repeat = int(record.get("repeat") or 1)
        statuses = _statuses(record)
        evidence = _evidence(record, statuses)
        reflect = record.get("reflect")
        project = int(record["project"])
        family = str(record["family"])
        steps = int(record.get("predicted_steps") or 3)
        conversation_mode = str(record.get("conversation") or "new")
        tool = _text_or_directive(record.get("primary_tool"), self.seed,
                                  str(record["id"]), "primary_tool")
        if isinstance(record.get("primary_tool"), dict) and tool:
            self.screened.append(tool)
        made: list[str] = []
        for index in range(repeat):
            reference = (str(record["id"]) if repeat == 1
                         else "{0}#{1}".format(record["id"], index))
            if conversation_mode == "none":
                conversation_id = None
            elif conversation_mode == "new":
                conversation_id = self.conversation(
                    "{0}::c{1}".format(record["id"], index), project)
            else:
                conversation_id = self.conversation(
                    conversation_mode.split(":", 1)[1], project)
            prediction_id = int(self.memory.record_prediction(
                family=family, profile="coding", model="flight-reference",
                predicted_success=float(record["predicted_success"]),
                predicted_steps=steps,
                predicted_verification=str(record.get("verification")
                                           or "tool_success"),
                basis="prior",
                origin=str(record.get("origin") or "interactive"),
                conversation_id=conversation_id))
            self.predictions[reference] = prediction_id
            for lesson_ref in list(record.get("apply_lessons") or []):
                self.memory.record_lesson_applications(
                    prediction_id, family, [self.lessons[lesson_ref]])
            if record.get("resolve", True):
                complete = statuses[index] == "+"
                mark = evidence[index]
                self.memory.resolve_prediction(
                    prediction_id,
                    actual_status="complete" if complete else "failed",
                    actual_steps=steps,
                    evidence_ok=(True if mark == "1"
                                 else (None if mark == "?" else False)),
                    failure_class=None if complete else "probe_failed",
                    primary_tool=tool)
            if reflect and statuses[index] == "+" and record.get("resolve",
                                                                 True):
                token = (str(reflect["marker"]) if repeat == 1
                         else "{0}{1:04d}".format(reflect["marker"], index))
                lesson_ref = (str(reflect["lesson_id"]) if repeat == 1
                              else "{0}#{1}".format(reflect["lesson_id"],
                                                    index))
                before = self.lesson_ids()
                self.memory.record_reflection(
                    status="complete",
                    summary=str(reflect["summary"]).format(marker=token),
                    mistakes=str(reflect.get("mistakes") or "").format(
                        marker=token),
                    improvements=str(reflect["improvements"]).format(
                        marker=token),
                    conversation_id=conversation_id,
                    prediction_id=prediction_id, tool_calls=steps)
                fresh = sorted(self.lesson_ids() - before)
                if len(fresh) != 1:
                    raise AssertionError(
                        "{0} produced {1} lessons".format(record["id"],
                                                          len(fresh)))
                self.lessons[lesson_ref] = fresh[0]
                made.append(lesson_ref)
        self.results[str(record["id"])] = {"op": "outcomes", "lessons": made,
                                           "count": repeat}

    def _op_resolve(self, record: dict[str, Any]) -> None:
        """Close a prediction that was deliberately held open across a seal.

        Design 7.14 author fact 16 / S-2: the unsealed tail is every eligible
        row not covered by a sealed epoch, in id order -- never
        ``id > last_prediction_id`` -- so a row held open while the block
        around it is cut must still land in a later epoch.
        """
        evidence = record.get("evidence_ok")
        resolved = self.memory.resolve_prediction(
            int(self.predictions[str(record["prediction"])]),
            actual_status=str(record["actual_status"]),
            actual_steps=int(record["actual_steps"]),
            evidence_ok=None if evidence is None else bool(evidence),
            failure_class=(None if record["actual_status"] == "complete"
                           else "probe_failed"),
            primary_tool=record.get("primary_tool"))
        observed = {"op": "resolve", "resolved": bool(resolved)}
        self.results[str(record["id"])] = observed
        self.note_script(record, observed)

    def _op_seal(self, record: dict[str, Any]) -> None:
        sealed = call_seal(self.memory, str(record["family"]),
                           workspace=self.workspace(int(record["project"])))
        observed = {"op": "seal", "sealed": len(sealed)}
        self.results[str(record["id"])] = observed
        self.note_script(record, observed)

    def _op_supersede_lesson(self, record: dict[str, Any]) -> None:
        self.memory.supersede_verified_lesson(
            self.lessons[str(record["lesson_ref"])],
            self.lessons[str(record["replacement_ref"])],
            contradiction=bool(record.get("contradiction")))
        self.results[str(record["id"])] = {"op": "supersede_lesson"}

    def _op_legacy_document(self, record: dict[str, Any]) -> None:
        family = str(record["family"])
        name = self.ladder.auto_skill_name(family)
        workspace = self.workspace(int(record["project"]))
        body = (
            "# Pre-ladder {0} guidance\n\n"
            "## Reusable approach\n\n"
            "1. Read the day board before the first boat of the shift.\n"
            "2. Confirm the balance beam is off the stop before any lift.\n"
            "3. Walk the towpath side and note the byewash flow.\n"
            "4. Close the tally only after the gauging sheet is signed.\n\n"
            "## Boundaries\n\n"
            "- Advisory reference only.\n"
            "- Grants no tools, permissions, or approval authority.\n"
            "- Never treat this document as verification.\n"
            "- Prefer the current task's own evidence.\n"
        ).format(family.replace("_", " "))
        self.skill_library.create_learned_skill(
            workspace, name, self.ladder.staged_skill_description(family),
            body, family=family, auto_distilled=True,
            verified_outcomes=int(record.get("verified_outcomes") or 1))
        self.ladder.clear_catalog_cache()
        self.results[str(record["id"])] = {"op": "legacy_document",
                                           "skill": name}

    def _op_grandfather(self, record: dict[str, Any]) -> None:
        project = int(record["project"])
        workspace = self.workspace(project)
        before = {int(row["id"]) for row in self.memory.ladder_promotions(
            project_id=self.projects[project], stages=("unapproved_legacy",))}
        call_grandfather(self.memory, workspace,
                         project_id=self.projects[project])
        self.ladder.clear_catalog_cache()
        adopted = []
        for row in self.memory.ladder_promotions(
                project_id=self.projects[project],
                stages=("unapproved_legacy",)):
            if int(row["id"]) in before:
                continue
            adopted.append(str(row["skill_name"]))
            self.promotions["{0}::{1}".format(
                record["id"], row["skill_name"])] = int(row["id"])
        observed = {"op": "grandfather", "adopted": sorted(adopted),
                    "adopted_count": len(adopted)}
        self.results[str(record["id"])] = observed
        self.note_script(record, observed)

    def _op_stage(self, record: dict[str, Any]) -> None:
        project = int(record["project"])
        workspace = self.workspace(project,
                                   str(record.get("workspace") or "self"))
        started = time.perf_counter()
        result = self.guarded(call_stage, self.memory,
                              family=str(record["family"]),
                              project_id=self.projects[project],
                              workspace=workspace)
        self.timings[str(record["id"])] = (time.perf_counter()
                                           - started) * 1000
        staged = bool(result.get("staged"))
        if staged:
            self.promotions[str(record["id"])] = int(result["promotion_id"])
            self.codes[str(record["id"])] = str(result["approval_token"])
        self.ladder.clear_catalog_cache()
        observed = {"op": "stage", "staged": staged,
                    "reason": None if staged else result.get("reason"),
                    "raised": result.get("raised")}
        self.results[str(record["id"])] = observed
        self.note_script(record, observed)

    def _promotion_id(self, record: dict[str, Any]) -> int:
        if record.get("missing"):
            return 9_000_017
        return int(self.promotions[str(record["promotion_ref"])])

    @staticmethod
    def _rotate_code(code: str) -> str:
        alphabet = ("ABCDEFGHIJKLMNOPQRSTUVWXYZ"
                    "abcdefghijklmnopqrstuvwxyz0123456789-_")
        if not code:
            return "A" * 16
        head = alphabet[(alphabet.index(code[0]) + 1) % len(alphabet)]
        return head + code[1:]

    def _op_approve(self, record: dict[str, Any]) -> None:
        record_id = str(record["id"])
        reference = str(record.get("promotion_ref") or "")
        promotion_id = self._promotion_id(record)
        source = str(record.get("code_source") or "correct")
        code = self.codes.get(reference, "")
        if source == "wrong":
            code = self._rotate_code(code)
        elif source == "malformed":
            code = "sh o rt"
        row = (self.memory.ladder_promotion(promotion_id)
               if promotion_id < 9_000_000 else None)
        mode = str(record.get("workspace") or "self")
        if row is not None:
            # The snapshot is keyed on THIS record, not on the promotion, so
            # two approvals of one skill cannot overwrite each other's before
            # image (ruling 30 defect c), and it reads the live root only.
            self.live_before_approve[record_id] = self.read_live(
                self.row_workspace(row), str(row["skill_name"]))
            workspace = self.row_workspace(row, mode)
        else:
            self.live_before_approve[record_id] = None
            workspace = self.workspace(int(self.store["projects"][0]), mode)
        started = time.perf_counter()
        result = self.guarded(call_approve, self.memory, promotion_id,
                              approval_token=code or "x" * 16,
                              workspace=workspace)
        self.timings[record_id] = (time.perf_counter() - started) * 1000
        self.ladder.clear_catalog_cache()
        applied = bool(result.get("applied"))
        observed = {"op": "approve", "applied": applied,
                    "reason": None if applied else result.get("reason"),
                    "raised": result.get("raised"),
                    "retired_legacy": bool(result.get("retired_legacy"))}
        self.results[record_id] = observed
        self.note_script(record, observed)

    def _op_rollback(self, record: dict[str, Any]) -> None:
        record_id = str(record["id"])
        promotion_id = self._promotion_id(record)
        row = (self.memory.ladder_promotion(promotion_id)
               if promotion_id < 9_000_000 else None)
        workspace = (self.row_workspace(
            row, str(record.get("workspace") or "self"))
            if row is not None
            else self.workspace(int(self.store["projects"][0])))
        started = time.perf_counter()
        result = self.guarded(call_rollback, self.memory, promotion_id,
                              workspace=workspace)
        self.timings[record_id] = (time.perf_counter() - started) * 1000
        self.ladder.clear_catalog_cache()
        rolled = bool(result.get("rolled_back"))
        after = (self.read_live(self.row_workspace(row),
                                str(row["skill_name"]))
                 if row is not None else None)
        observed = {"op": "rollback", "rolled_back": rolled,
                    "reason": None if rolled else result.get("reason"),
                    "raised": result.get("raised"),
                    "restored": bool(result.get("restored")),
                    "removed": bool(result.get("removed"))}
        self.results[record_id] = dict(observed, after_bytes=after)
        self.note_script(record, observed)

    def _op_discard(self, record: dict[str, Any]) -> None:
        promotion_id = self._promotion_id(record)
        row = (self.memory.ladder_promotion(promotion_id)
               if promotion_id < 9_000_000 else None)
        workspace = (self.row_workspace(row) if row is not None
                     else self.workspace(int(self.store["projects"][0])))
        started = time.perf_counter()
        result = self.guarded(call_discard, self.memory, promotion_id,
                              workspace=workspace)
        self.timings[str(record["id"])] = (time.perf_counter()
                                           - started) * 1000
        self.ladder.clear_catalog_cache()
        discarded = bool(result.get("discarded"))
        observed = {"op": "discard", "discarded": discarded,
                    "reason": None if discarded else result.get("reason"),
                    "raised": result.get("raised")}
        self.results[str(record["id"])] = observed
        self.note_script(record, observed)

    def _op_plant_applications(self, record: dict[str, Any]) -> None:
        """Raw-SQL application rows: ruling 21's unbacked proof material.

        No product path writes a ``lesson_applications`` row without the
        ``lesson.applied`` receipt, which is exactly why the proof must refuse
        a set that has no matching event.  Nothing is deleted to make room for
        them and every column they name already exists.
        """
        memory_id = int(self.lessons[str(record["lesson_ref"])])
        family = str(record["family"])
        columns = {str(row[1]) for row in self.memory.db.execute(
            "PRAGMA table_info(lesson_applications)")}
        for rank, prediction_ref in enumerate(record["prediction_refs"]):
            prediction_id = int(self.predictions[str(prediction_ref)])
            row = self.memory.db.execute(
                "SELECT resolved_at, actual_status FROM task_predictions"
                " WHERE id=?", (prediction_id,)).fetchone()
            fields = ["prediction_id", "memory_id", "family", "rank"]
            values: list[Any] = [prediction_id, memory_id, family,
                                 (rank % 10) + 1]
            if "resolved_at" in columns:
                fields.append("resolved_at")
                values.append(row[0])
            if "successful" in columns:
                fields.append("successful")
                values.append(1 if str(row[1]) == "complete" else 0)
            if "created_at" in columns:
                fields.append("created_at")
                values.append(row[0])
            self.memory.db.execute(
                "INSERT OR IGNORE INTO lesson_applications ({0}) VALUES ({1})"
                .format(", ".join(fields),
                        ", ".join("?" for _ in fields)), tuple(values))
        self.memory.db.commit()
        self.results[str(record["id"])] = {"op": "plant_applications"}

    # ------------------------------------------------------------- tampers
    def _op_tamper(self, record: dict[str, Any]) -> None:
        """Every branch plants state no product path produces.

        Design 2.4 and 7.5: the coverage checks exist for an out-of-band
        ``DELETE`` and for an epoch re-cut over different rows, so the tamper
        is raw SQL of need, and the fixture's note on each case says so.
        Ruling 31: no branch deletes a row another row references.  The only
        delete is of a ``ladder_promotions`` row, and an unsealed test proves
        from the live schema that no foreign key points at that table.
        """
        handler = getattr(self, "_tamper_" + str(record["target"]))
        handler(record)
        self.ladder.clear_catalog_cache()
        self.results[str(record["id"])] = {"op": "tamper",
                                           "target": str(record["target"])}

    def _drop_ledger_write_triggers(self) -> None:
        self.memory.db.execute(
            "DROP TRIGGER IF EXISTS memory_calibration_ledger_append_only")
        self.memory.db.execute(
            "DROP TRIGGER IF EXISTS memory_calibration_ledger_no_delete")

    def _tamper_corrupt_spine_head(self, record: dict[str, Any]) -> None:
        """Ruling 27's store: the head MAC no longer verifies.

        Nothing product-side can produce this; it is what a database file
        edited outside the product looks like, and the read path must fail
        closed rather than raise.
        """
        row = self.memory.db.execute(
            "SELECT id, head_mac FROM memory_spine_head"
            " ORDER BY id DESC LIMIT 1").fetchone()
        if row is None:
            raise AssertionError("no spine head to corrupt")
        current = str(row[1] or "")
        head = "b" if current[:1] != "b" else "c"
        self.memory.db.execute(
            "UPDATE memory_spine_head SET head_mac=? WHERE id=?",
            (head + current[1:] if current else head * 64, int(row[0])))
        self.memory.db.commit()

    def _tamper_quarantine_lesson(self, record: dict[str, Any]) -> None:
        self.memory.db.execute(
            "UPDATE lesson_controls SET lifecycle_status='quarantined'"
            " WHERE memory_id=?",
            (int(self.lessons[str(record["lesson_ref"])]),))
        self.memory.db.commit()

    def _tamper_delete_promotion_row(self, record: dict[str, Any]) -> None:
        """The one delete, and it is safe.

        ``ladder_promotions`` is referenced by no foreign key in schema 49:
        the row points outward at ``agent_projects``,
        ``memory_calibration_ledger`` and ``memory_spine_events`` and nothing
        points back.  Deleting it leaves the document it claimed on disk with
        its ladder events still on the spine, which is exactly the orphan of
        ruling 34 P2 and 35.
        """
        self.memory.db.execute(
            "DELETE FROM ladder_promotions WHERE id=?",
            (int(self.promotions[str(record["promotion_ref"])]),))
        self.memory.db.commit()

    def _tamper_remove_live_document(self, record: dict[str, Any]) -> None:
        """Design 7.8's crash direction: the row committed, the file is gone."""
        row = self.memory.ladder_promotion(
            int(self.promotions[str(record["promotion_ref"])]))
        path = self.live_path(self.row_workspace(row), str(row["skill_name"]))
        if path.exists():
            path.unlink()
        if path.parent.exists() and not any(path.parent.iterdir()):
            path.parent.rmdir()

    def _tamper_edit_live_document(self, record: dict[str, Any]) -> None:
        row = self.memory.ladder_promotion(
            int(self.promotions[str(record["promotion_ref"])]))
        path = self.live_path(self.row_workspace(row), str(row["skill_name"]))
        path.write_bytes(path.read_bytes()
                         + b"\n<!-- edited behind the ladder -->\n")

    def _tamper_tool_name(self, record: dict[str, Any]) -> None:
        secret = _expand_directive(record["value"], self.seed,
                                   str(record["id"]), "value")
        self.screened.append(secret)
        self.memory.db.execute(
            "UPDATE lesson_applications SET tool_name=? WHERE memory_id=?",
            (secret, int(self.lessons[str(record["lesson_ref"])])))
        self.memory.db.commit()

    def _tamper_ledger_boundary(self, record: dict[str, Any]) -> None:
        self._drop_ledger_write_triggers()
        self.memory.db.execute(
            "UPDATE memory_calibration_ledger"
            " SET last_prediction_id = last_prediction_id + 7"
            " WHERE family=? AND epoch=?",
            (str(record["family"]), int(record["epoch"])))
        self.memory.db.commit()

    def _tamper_ledger_covered_ids(self, record: dict[str, Any]) -> None:
        self._drop_ledger_write_triggers()
        row = self.memory.db.execute(
            "SELECT id, covered_ids_json FROM memory_calibration_ledger"
            " WHERE family=? AND epoch=?",
            (str(record["family"]), int(record["epoch"]))).fetchone()
        moved = json.dumps([int(value) + 1 for value in json.loads(row[1])],
                           separators=(",", ":"))
        self.memory.db.execute(
            "UPDATE memory_calibration_ledger SET covered_ids_json=?"
            " WHERE id=?", (moved, int(row[0])))
        self.memory.db.commit()

    def _tamper_flip_outcome(self, record: dict[str, Any]) -> None:
        self.memory.db.execute(
            "UPDATE task_predictions SET actual_status='complete',"
            " failure_class=NULL WHERE id=?",
            (int(self.predictions[str(record["prediction_ref"])]),))
        self.memory.db.commit()


# ---------------------------------------------------------------- the scorer
def _skill_names(entries: Any) -> set[str]:
    names: set[str] = set()
    for entry in entries or []:
        if isinstance(entry, dict) and entry.get("name"):
            names.add(str(entry["name"]))
    return names


def _row_ids(rows: Any) -> list[int]:
    found: list[int] = []
    for row in rows or []:
        for key in ("memory_id", "id"):
            if isinstance(row, dict) and row.get(key) is not None:
                found.append(int(row[key]))
                break
    return found


def _texts(rows: Any) -> str:
    parts: list[str] = []
    for row in rows or []:
        if isinstance(row, dict):
            for value in row.values():
                if isinstance(value, str):
                    parts.append(value)
        elif isinstance(row, str):
            parts.append(row)
    return "\n".join(parts)


def _as_list(value: Any) -> list[Any]:
    """A one-or-many helper that keeps ``None`` as a value, not as absence.

    Ruling 30 defect (d): v2 mapped ``None`` to ``[]``, so a case that
    correctly expected a null reason -- a proof that holds, an operation that
    refused nothing -- could never match and a correct implementation scored
    as a failure.  ``None`` is an expectation here, and the only way to say
    "not checked" is to leave the key out.
    """
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def _reason_pairs(rows: Any) -> list[tuple[str, str]]:
    return sorted((str(row.get("skill_name")), str(row.get("reason")))
                  for row in rows or [])


def _reasons_match(observed: list[tuple[str, str]], wanted: Any) -> bool:
    """Match a bucket against per-row closed sets of the design's reasons.

    One project per planted state means several rows can carry the SAME skill
    name with different reasons, so this is a bipartite matching over rows and
    not a dict keyed on the name: keying on the name would silently collapse
    five planted tampers into one expectation.  A wanted ``reason`` may be a
    list wherever the design names more than one refusal for the same state.
    """
    expected = [(str(item["skill"]), [str(value) for value in
                                      _as_list(item["reason"])])
                for item in wanted or []]
    if len(observed) != len(expected):
        return False
    used = [False] * len(expected)

    def place(index: int) -> bool:
        if index == len(observed):
            return True
        name, reason = observed[index]
        for slot, (want_name, want_reasons) in enumerate(expected):
            if used[slot] or want_name != name or reason not in want_reasons:
                continue
            used[slot] = True
            if place(index + 1):
                return True
            used[slot] = False
        return False

    return place(0)


def _blank_metrics() -> dict[str, Any]:
    return {"cases": 0, "errors": 0, "returned_rows": 0, "allowed_rows": 0,
            "expected_lessons": 0, "matched_lessons": 0, "forbidden_hits": 0,
            "leakage": 0, "staged_visibility": 0, "unverified_served": 0,
            "empty_recall_cases": 0, "empty_recall_passes": 0,
            "lesson_mode_cases": 0, "lesson_mode_passes": 0,
            "skill_mode_cases": 0, "skill_mode_passes": 0,
            "cue_cases": 0, "cue_passes": 0, "gate_cases": 0, "gate_passes": 0,
            "closure_cases": 0, "closure_passes": 0,
            "skill_cases": 0, "skill_passes": 0,
            "op_cases": 0, "op_passes": 0,
            "check_cases": 0, "check_passes": 0,
            "rollback_cases": 0, "rollback_passes": 0,
            "reported_mode_cases": 0, "reported_mode_agreements": 0}


def _accumulate(counters: dict[str, Any], outcome: dict[str, Any]) -> None:
    counters["cases"] += 1
    counters["errors"] += len(outcome["errors"])
    counters["returned_rows"] += outcome["rows_total"]
    counters["allowed_rows"] += outcome["allowed_rows"]
    counters["expected_lessons"] += outcome["expected_lessons"]
    counters["matched_lessons"] += outcome["matched_lessons"]
    counters["forbidden_hits"] += outcome["forbidden_hits"]
    counters["leakage"] += outcome["leakage"]
    counters["staged_visibility"] += outcome["staged_visibility"]
    counters["unverified_served"] += outcome["unverified_served"]
    counters["reported_mode_cases"] += outcome["reported_mode_case"]
    counters["reported_mode_agreements"] += outcome["reported_mode_agreement"]
    for key, prefix in (("empty_recall_pass", "empty_recall"),
                        ("lesson_mode_pass", "lesson_mode"),
                        ("skill_mode_pass", "skill_mode"),
                        ("cue_pass", "cue"), ("gate_pass", "gate"),
                        ("closure_pass", "closure"), ("skill_pass", "skill"),
                        ("op_pass", "op")):
        value = outcome.get(key)
        if value is not None:
            counters[prefix + "_cases"] += 1
            counters[prefix + "_passes"] += int(bool(value))
    for item in outcome["check_results"]:
        counters["check_cases"] += 1
        counters["check_passes"] += int(item["pass"])
        if item["name"] == "rollback_bytes":
            counters["rollback_cases"] += 1
            counters["rollback_passes"] += int(item["pass"])


def _finalize(counters: dict[str, Any]) -> dict[str, Any]:
    def ratio(passes: str, cases: str) -> float:
        return counters[passes] / counters[cases] if counters[cases] else 1.0

    return {
        **counters,
        "injection_precision": (counters["allowed_rows"]
                                / counters["returned_rows"]
                                if counters["returned_rows"] else 1.0),
        "injection_recall": (counters["matched_lessons"]
                             / counters["expected_lessons"]
                             if counters["expected_lessons"] else 1.0),
        "empty_recall_accuracy": ratio("empty_recall_passes",
                                       "empty_recall_cases"),
        "lesson_mode_accuracy": ratio("lesson_mode_passes",
                                      "lesson_mode_cases"),
        "skill_mode_accuracy": ratio("skill_mode_passes", "skill_mode_cases"),
        "cue_accuracy": ratio("cue_passes", "cue_cases"),
        "gate_accuracy": ratio("gate_passes", "gate_cases"),
        "closure_accuracy": ratio("closure_passes", "closure_cases"),
        "skill_accuracy": ratio("skill_passes", "skill_cases"),
        "op_accuracy": ratio("op_passes", "op_cases"),
        "check_accuracy": ratio("check_passes", "check_cases"),
        "rollback_exactness": ratio("rollback_passes", "rollback_cases"),
        "reported_mode_agreement_rate": ratio("reported_mode_agreements",
                                              "reported_mode_cases"),
    }


def _observed_counts(memory: Any, replay: _Replay) -> dict[str, Any]:
    def scalar(sql: str) -> int:
        return int(memory.db.execute(sql).fetchone()[0])

    epochs = {str(row[0]): int(row[1]) for row in memory.db.execute(
        "SELECT family, COUNT(*) FROM memory_calibration_ledger"
        " GROUP BY family")}
    stages: dict[str, int] = {}
    for row in memory.db.execute(
            "SELECT stage, COUNT(*) FROM ladder_promotions GROUP BY stage"):
        # ``withdrawn`` and ``rolled_back`` share one bucket on purpose:
        # ruling 29(e) says a superseded approval moves to "a terminal stage"
        # without saying which, so counting them apart would gate a guess.
        # Which stage an individual row reached is pinned by the
        # ``promotion_stages`` check, where a disjunction can be expressed.
        stage = str(row[0])
        if stage in ("withdrawn", "rolled_back"):
            stage = "terminal"
        stages[stage] = stages.get(stage, 0) + int(row[1])
    staged_files = 0
    live_files = 0
    parked_files = 0
    for workspace in replay.workspaces.values():
        for path in (workspace / STAGED_DIRECTORY).glob("*/SKILL.md"):
            if path.parent.name.startswith(WITHDRAWN_PREFIX):
                parked_files += 1
            else:
                staged_files += 1
        live_files += len(list((workspace / LEARNED_DIRECTORY)
                               .glob("*/SKILL.md")))
    return {
        "predictions": scalar("SELECT COUNT(*) FROM task_predictions"),
        "lessons": scalar("SELECT COUNT(*) FROM memories WHERE kind='lesson'"),
        "applications": scalar("SELECT COUNT(*) FROM lesson_applications"),
        "epochs": dict(sorted(epochs.items())),
        "promotions": dict(sorted(stages.items())),
        "staged_files": staged_files,
        "live_files": live_files,
        "parked_files": parked_files,
    }


class _CaseRunner:
    """Score one case against one seeded store, catching every failure.

    Every step is guarded: design 10.7 item 27 was found because a read-path
    exception aborted the whole v1 run before an aggregate existed.  A defect
    here must produce a scored failure with a message, never an aborted run.
    """

    def __init__(self, memory: Any, replay: _Replay, ladder: Any,
                 skill_library: Any, fixture: dict[str, Any],
                 snapshots: dict[str, Any], reopen: Any) -> None:
        self.memory = memory
        self.replay = replay
        self.ladder = ladder
        self.skill_library = skill_library
        self.fixture = fixture
        self.snapshots = snapshots
        self.reopen = reopen

    def run(self, case: dict[str, Any]) -> dict[str, Any]:
        outcome: dict[str, Any] = {
            "case": str(case["id"]), "kind": str(case["kind"]),
            "store": str(case["store"]), "errors": [], "rows_total": 0,
            "allowed_rows": 0, "expected_lessons": 0, "matched_lessons": 0,
            "forbidden_hits": 0, "leakage": 0, "staged_visibility": 0,
            "unverified_served": 0, "empty_recall_pass": None,
            "lesson_mode_pass": None, "skill_mode_pass": None,
            "cue_pass": None, "gate_pass": None, "closure_pass": None,
            "skill_pass": None, "op_pass": None, "check_results": [],
            "checks_pass": True, "elapsed_ms": 0.0, "write_ms": None,
            "reported_mode_case": 0, "reported_mode_agreement": 0,
            "observed": {}}
        handle = self.memory
        opened = None
        try:
            if case.get("fresh_store"):
                # Ruling 30 defect (b) and ruling 35: the outstanding
                # withdrawal must still be visible to a LATER read in a NEW
                # process.  A fresh store object is the only way to prove the
                # state is durable and spine-derived rather than an artefact
                # of one instance's in-memory queue.
                opened = self.reopen()
                handle = opened
            self._read_path(case, outcome, handle)
        except Exception as error:                       # noqa: BLE001
            outcome["errors"].append("read path: {0}: {1}".format(
                type(error).__name__, error))
        try:
            self._operations(case, outcome)
        except Exception as error:                       # noqa: BLE001
            outcome["errors"].append("operations: {0}: {1}".format(
                type(error).__name__, error))
        try:
            self._checks(case, outcome, handle)
        except Exception as error:                       # noqa: BLE001
            outcome["errors"].append("checks: {0}: {1}".format(
                type(error).__name__, error))
        if opened is not None:
            try:
                opened.close()
            except Exception as error:                   # noqa: BLE001
                outcome["errors"].append("reopen close: {0}: {1}".format(
                    type(error).__name__, error))
            self.ladder.clear_catalog_cache()
        outcome["checks_pass"] = all(item["pass"]
                                     for item in outcome["check_results"])
        return outcome

    # ------------------------------------------------------------ read path
    def _question(self, case: dict[str, Any]) -> str:
        question = str(case["question"])
        directive = case.get("question_directive")
        if directive:
            question = question + " " + _expand_directive(
                directive, int(self.fixture["generator_seed"]),
                str(case["id"]), "question")
        return question

    def _read_path(self, case: dict[str, Any], outcome: dict[str, Any],
                   memory: Any) -> None:
        if not case.get("question"):
            return
        replay = self.replay
        logical = int(case["project"])
        project_id = (None if case.get("project_mode") == "none"
                      else replay.projects[logical])
        workspace = replay.workspaces[logical]
        family = str(case["family"])
        question = self._question(case)
        started = time.perf_counter()
        # Design 7.14 / S-7: the gate first, the skill report always and bare
        # (ruling 29a: a bare call must report what a threaded one would), the
        # lesson lane only when the gate allows, then the cue.
        gate = call_gate(memory, family, self.fixture["gate_thresholds"])
        skill_report = call_skill_report(
            self.ladder, workspace=workspace, memory=memory,
            family=family, project_id=project_id, gate=gate)
        if gate.get("allowed"):
            try:
                rows = call_match_lessons(memory, question, family,
                                          project_id=project_id, limit=3)
            except ValueError:
                # Design 5.4: the diagnostic record is written BEFORE both
                # raises, so a caller that catches still learns the mode.
                rows = []
            lesson_report = memory.lesson_recall_report()
            withheld = 0
        else:
            rows = []
            lesson_report = {"mode": "idle", "reason": None}
            withheld = (call_candidate_count(
                memory, family, replay.projects[logical],
                limit=int(self.fixture["constants"]["LADDER_WITHHELD_CAP"]))
                + int(skill_report.get("withheld") or 0))
        cue = call_cue(self.ladder, str(lesson_report.get("mode")),
                       str(skill_report.get("mode")),
                       withheld_candidates=withheld)
        documents = call_approved_skills(
            self.ladder, workspace=workspace, memory=memory,
            family=family, project_id=replay.projects[logical], limit=2)
        outcome["elapsed_ms"] = (time.perf_counter() - started) * 1000

        lesson_mode = str(lesson_report.get("mode"))
        skill_mode = str(skill_report.get("mode"))
        outcome["observed"].update({
            "gate_allowed": bool(gate.get("allowed")),
            "gate_closure": self.ladder.gate_closed_reason(gate),
            "lesson_mode": lesson_mode,
            "lesson_reason": lesson_report.get("reason"),
            "skill_mode": skill_mode,
            "skill_reason": skill_report.get("reason"),
            "receipt_deferred": skill_report.get("receipt_deferred"),
            "withheld": withheld, "cue": bool(cue),
            "returned": len(rows),
            "skills": sorted(_skill_names(documents))})

        if "expect_skill_reason" in case:
            outcome["check_results"].append({
                "name": "skill_reason", "pass":
                    skill_report.get("reason")
                    in _as_list(case["expect_skill_reason"]),
                "expected": case["expect_skill_reason"],
                "observed": skill_report.get("reason")})
        if "expect_receipt_deferred" in case:
            # Ruling 28: the skill-channel record carries the key, and ruling
            # 27 makes it true exactly when the withdrawal's receipt could not
            # be appended.  The artefact is excluded either way.
            outcome["check_results"].append({
                "name": "receipt_deferred", "pass":
                    bool(skill_report.get("receipt_deferred"))
                    is bool(case["expect_receipt_deferred"]),
                "expected": case["expect_receipt_deferred"],
                "observed": skill_report.get("receipt_deferred")})

        if case.get("expect_gate_allowed") is not None:
            outcome["gate_pass"] = (bool(gate.get("allowed"))
                                    is bool(case["expect_gate_allowed"]))
        if "expect_gate_closure" in case:
            outcome["closure_pass"] = (
                self.ladder.gate_closed_reason(gate)
                == case["expect_gate_closure"])

        expected_ids = [replay.lessons[ref] for ref in case["expect_lessons"]]
        returned_ids = _row_ids(rows)
        outcome["rows_total"] = len(returned_ids)
        forbidden_ids = {replay.lessons[ref] for ref in case["forbid_refs"]}
        outcome["forbidden_hits"] = sum(1 for value in returned_ids
                                        if value in forbidden_ids)
        if case["recall_gated"]:
            allowed = set(expected_ids)
            outcome["allowed_rows"] = sum(1 for value in returned_ids
                                          if value in allowed)
            outcome["expected_lessons"] = len(expected_ids)
            outcome["matched_lessons"] = sum(1 for value in expected_ids
                                             if value in returned_ids)
            if not expected_ids:
                outcome["empty_recall_pass"] = not returned_ids
        else:
            outcome["allowed_rows"] = len(returned_ids)

        if case["mode_gated"]:
            outcome["lesson_mode_pass"] = (
                lesson_mode in _as_list(case["expect_lesson_mode"]))
        else:
            outcome["reported_mode_case"] += 1
            outcome["reported_mode_agreement"] += int(
                lesson_mode in _as_list(case["expect_lesson_mode"]))
        if case["skill_mode_gated"]:
            outcome["skill_mode_pass"] = (
                skill_mode in _as_list(case["expect_skill_mode"]))
        else:
            outcome["reported_mode_case"] += 1
            outcome["reported_mode_agreement"] += int(
                skill_mode in _as_list(case["expect_skill_mode"]))
        if case["cue_gated"]:
            outcome["cue_pass"] = bool(cue) is bool(case["expect_cue"])

        wanted_skill = case.get("expect_skill")
        names = _skill_names(documents)
        outcome["skill_pass"] = (names == {str(wanted_skill)} if wanted_skill
                                 else not names)

        # Ruling 34 P1: the leak material is the model-facing block ONLY --
        # the matched lesson rows and the returned documents.  A report is not
        # a surface the model sees, and the staging return and a raw promotion
        # row are surfaces the scorer must never serialize at all.
        outcome["leakage"] += self._leakage(
            _texts(rows) + "\n" + _texts(documents))

        # Two guarantees that need no fixture input, computed for every read.
        outcome["staged_visibility"] += self._staged_visibility(
            workspace, names)
        outcome["unverified_served"] += self._unverified_served(
            memory, workspace, replay.projects[logical], names)

    def _leakage(self, material: str) -> int:
        hits = 0
        for code in self.replay.codes.values():
            if code and code in material:
                hits += 1
        for secret in self.replay.screened:
            if secret and secret in material:
                hits += 1
        return hits

    def _parked_or_staged(self, workspace: Path) -> set[str]:
        names: set[str] = set()
        for path in (workspace / STAGED_DIRECTORY).glob("*/SKILL.md"):
            folder = path.parent.name
            if folder.startswith(WITHDRAWN_PREFIX):
                names.add(folder[len(WITHDRAWN_PREFIX):])
            else:
                names.add(folder)
        return names

    def _staged_visibility(self, workspace: Path, served: set[str]) -> int:
        """A staged or parked document that any catalog would hand over."""
        catalog = _skill_names(
            self.skill_library.list_available_skills(workspace))
        return len(self._parked_or_staged(workspace) & (catalog | served))

    def _unverified_served(self, memory: Any, workspace: Path,
                           project_id: int, served: set[str]) -> int:
        """An artefact the store itself calls unverified, handed to the model.

        A pure read, taken after the skill channel has already run, so it
        reports what the model was given against what the store says about the
        same moment.
        """
        if not served:
            return 0
        rows = call_unverified(memory, workspace=workspace,
                               project_id=project_id)
        unverified = {str(row.get("skill_name")) for row in rows or []}
        return len(served & unverified)

    # ----------------------------------------------------------- operations
    def _operations(self, case: dict[str, Any], outcome: dict[str, Any]
                    ) -> None:
        reference = case.get("op_ref")
        if not reference:
            return
        recorded = self.replay.results.get(str(reference))
        if case.get("timed") == "write":
            outcome["write_ms"] = self.replay.timings.get(str(reference))
        if recorded is None:
            outcome["op_pass"] = False
            outcome["errors"].append("no recorded op for " + str(reference))
            return
        outcome["observed"]["op"] = {key: value
                                     for key, value in recorded.items()
                                     if key != "after_bytes"}
        wanted = case.get("expect_op") or {}
        passes = True
        for key in sorted(wanted):
            want = wanted[key]
            got = recorded.get(key)
            if isinstance(want, list):
                passes = passes and got in want
            else:
                passes = passes and got == want
        if "expect_refusal" in case:
            # ``None`` here means "this operation refused nothing", and the
            # helper keeps it as a value rather than dropping it (ruling 30d).
            passes = passes and (recorded.get("reason")
                                 in _as_list(case["expect_refusal"]))
        outcome["op_pass"] = passes

    # --------------------------------------------------------------- checks
    def _checks(self, case: dict[str, Any], outcome: dict[str, Any],
                memory: Any) -> None:
        checks = case.get("checks") or {}
        for name in sorted(checks):
            handler = getattr(self, "_check_" + name, None)
            if handler is None:
                outcome["check_results"].append(
                    {"name": name, "pass": False, "expected": checks[name],
                     "observed": "unknown check"})
                continue
            try:
                observed, passed = handler(case, checks[name], memory)
            except Exception as error:                   # noqa: BLE001
                observed, passed = "{0}: {1}".format(type(error).__name__,
                                                     error), False
            outcome["check_results"].append(
                {"name": name, "pass": bool(passed),
                 "expected": checks[name], "observed": observed})

    def _workspace(self, case: dict[str, Any]) -> Path:
        return self.replay.workspaces[int(case["project"])]

    def _project(self, case: dict[str, Any]) -> int:
        return self.replay.projects[int(case["project"])]

    def _check_unverified(self, case: dict[str, Any], wanted: Any,
                          memory: Any):
        """The store-wide bucket as it stood before any case read it."""
        observed = _reason_pairs(self.snapshots["unverified"])
        return observed, _reasons_match(observed, wanted)

    def _check_unverified_now(self, case: dict[str, Any], wanted: Any,
                              memory: Any):
        rows = call_unverified(memory, workspace=self._workspace(case),
                               project_id=self._project(case))
        observed = _reason_pairs(rows)
        return observed, _reasons_match(observed, wanted)

    def _check_pending_withdrawals(self, case: dict[str, Any], wanted: Any,
                                   memory: Any):
        """Ruling 30: outstanding withdrawals are durable, not per-instance.

        Ruling 36: the live set is supplied, because an orphan has no row and
        a bare call cannot decide "parked" without it.
        """
        rows = call_pending(memory, self._project(case),
                            workspace=self._workspace(case))
        observed = sorted(str(row.get("skill_name")) for row in rows)
        return observed, observed == sorted(str(name) for name in wanted)

    def _check_pending_details(self, case: dict[str, Any], wanted: Any,
                               memory: Any):
        """Ruling 35: a parked orphan's outstanding receipt, spine-derived.

        The name and the deferral are the guarantee; the reason string is
        matched against the closed set the design and the rulings name for
        this state, never a single guessed value.  ``deferred`` and
        ``receipt_deferred`` are both accepted as the flag, because the
        rulings name the fact and not the key.
        """
        rows = call_pending(memory, self._project(case),
                            workspace=self._workspace(case))
        observed = []
        for row in rows or []:
            deferred = row.get("deferred")
            if deferred is None:
                deferred = row.get("receipt_deferred")
            observed.append({"skill": str(row.get("skill_name")),
                             "deferred": bool(deferred),
                             "reason": str(row.get("reason"))})
        observed.sort(key=lambda item: item["skill"])
        expected = sorted(wanted, key=lambda item: str(item["skill"]))
        if len(observed) != len(expected):
            return observed, False
        passed = True
        for got, want in zip(observed, expected):
            passed = passed and got["skill"] == str(want["skill"])
            if "deferred" in want:
                passed = passed and got["deferred"] is bool(want["deferred"])
            if "reason" in want:
                passed = passed and got["reason"] in [
                    str(value) for value in _as_list(want["reason"])]
        return observed, passed

    def _check_legacy(self, case: dict[str, Any], wanted: Any, memory: Any):
        rows = call_legacy(memory, workspace=self._workspace(case),
                           project_id=self._project(case))
        observed = sorted(str(row.get("skill_name")) for row in rows)
        return observed, observed == sorted(str(name) for name in wanted)

    def _check_absent_skills(self, case: dict[str, Any], wanted: Any,
                             memory: Any):
        workspace = self._workspace(case)
        approved = _skill_names(call_approved_skills(
            self.ladder, workspace=workspace, memory=memory,
            family=str(case["family"]), project_id=self._project(case),
            limit=2))
        seen = sorted(approved & {str(name) for name in wanted})
        return seen, not seen

    def _check_catalog_absent(self, case: dict[str, Any], wanted: Any,
                              memory: Any):
        """Ruling 34 P2: the FILE catalog must stop serving a parked name.

        ``list_available_skills`` and ``read_available_skill`` walk the live
        root with no ladder consultation, which is how an orphan reached the
        model around the ladder in the first place.
        """
        workspace = self._workspace(case)
        catalog = _skill_names(self.skill_library.list_available_skills(
            workspace))
        listed = sorted(catalog & {str(name) for name in wanted})
        readable = []
        for name in wanted:
            try:
                entry = self.skill_library.read_available_skill(str(name),
                                                                workspace)
            except Exception:                            # noqa: BLE001
                continue
            if entry:
                readable.append(str(name))
        observed = {"listed": listed, "readable": sorted(readable)}
        return observed, not listed and not readable

    def _check_catalog_present(self, case: dict[str, Any], wanted: Any,
                               memory: Any):
        """Ruling 34 P2, the other half: a file with no ladder event is never
        parked, so the ordinary catalog still holds it."""
        workspace = self._workspace(case)
        catalog = _skill_names(self.skill_library.list_available_skills(
            workspace))
        missing = sorted({str(name) for name in wanted} - catalog)
        return sorted(catalog), not missing

    def _check_withdrawn_receipts(self, case: dict[str, Any], wanted: Any,
                                  memory: Any):
        """How many ``ladder.withdrawn`` events name this skill."""
        skill = str(wanted["skill"])
        found = 0
        for row in memory.db.execute(
                "SELECT payload_json FROM memory_spine_events"
                " WHERE kind='ladder.withdrawn'"):
            try:
                payload = json.loads(row[0] or "{}")
            except Exception:                            # noqa: BLE001
                continue
            if str(payload.get("skill_name")) == skill:
                found += 1
        passed = True
        if "minimum" in wanted:
            passed = passed and found >= int(wanted["minimum"])
        if "maximum" in wanted:
            passed = passed and found <= int(wanted["maximum"])
        return found, passed

    def _check_candidate_count(self, case: dict[str, Any], wanted: Any,
                               memory: Any):
        count = call_candidate_count(
            memory, str(wanted["family"]), self._project(case),
            limit=int(self.fixture["constants"]["LADDER_WITHHELD_CAP"]))
        passed = True
        if "minimum" in wanted:
            passed = passed and count >= int(wanted["minimum"])
        if "maximum" in wanted:
            passed = passed and count <= int(wanted["maximum"])
        return count, passed

    def _check_staged_files(self, case: dict[str, Any], wanted: Any,
                            memory: Any):
        workspace = self._workspace(case)
        observed = sorted(
            entry.parent.name
            for entry in (workspace / STAGED_DIRECTORY).glob("*/SKILL.md")
            if not entry.parent.name.startswith(WITHDRAWN_PREFIX))
        return observed, observed == sorted(str(name) for name in wanted)

    def _check_parked_files(self, case: dict[str, Any], wanted: Any,
                            memory: Any):
        workspace = self._workspace(case)
        observed = sorted(
            entry.parent.name[len(WITHDRAWN_PREFIX):]
            for entry in (workspace / STAGED_DIRECTORY).glob("*/SKILL.md")
            if entry.parent.name.startswith(WITHDRAWN_PREFIX))
        return observed, observed == sorted(str(name) for name in wanted)

    def _check_live_files(self, case: dict[str, Any], wanted: Any,
                          memory: Any):
        workspace = self._workspace(case)
        observed = sorted(entry.parent.name for entry
                          in (workspace / LEARNED_DIRECTORY)
                          .glob("*/SKILL.md"))
        return observed, observed == sorted(str(name) for name in wanted)

    def _check_monotone(self, case: dict[str, Any], wanted: Any, memory: Any):
        verdict = call_monotonicity(memory, str(wanted["family"]))
        observed = {key: verdict.get(key) for key in sorted(wanted)
                    if key != "family"}
        expected = {key: wanted[key] for key in sorted(wanted)
                    if key != "family"}
        return observed, observed == expected

    def _check_ledger_epochs(self, case: dict[str, Any], wanted: Any,
                             memory: Any):
        rows = call_ledger(memory, str(wanted["family"]))
        observed = [{"epoch": int(row["epoch"]), "n": int(row["n"]),
                     "successes": int(row["successes"])} for row in rows]
        expected = [{"epoch": int(item["epoch"]), "n": int(item["n"]),
                     "successes": int(item["successes"])}
                    for item in wanted["epochs"]]
        return observed, observed == expected

    def _check_late_resolution(self, case: dict[str, Any], wanted: Any,
                               memory: Any):
        """Design 7.14 author fact 16 / S-2, made observable.

        The unsealed tail is every eligible row not covered by a sealed
        epoch, in id order -- never ``id > last_prediction_id``.  A prediction
        held open while the block around it was cut therefore lands in a later
        epoch whose *first* covered id is lower than an earlier epoch's last.
        """
        rows = {int(row["epoch"]): row
                for row in call_ledger(memory, str(wanted["family"]))}
        later = rows.get(int(wanted["epoch"]))
        earlier = rows.get(int(wanted["earlier_epoch"]))
        if later is None or earlier is None:
            return {"later": None, "earlier": None}, False
        observed = {"later_first": int(later["first_prediction_id"]),
                    "earlier_last": int(earlier["last_prediction_id"])}
        return observed, observed["later_first"] < observed["earlier_last"]

    def _check_ledger_verify(self, case: dict[str, Any], wanted: Any,
                             memory: Any):
        report = call_verify_ledger(memory, wanted.get("family"))
        observed = {"coverage_intact": bool(report.get("coverage_intact")),
                    "problem_kinds": sorted({str(item.get("kind"))
                                             for item in
                                             report.get("problems") or []}),
                    "gaps": len(report.get("coverage_gaps") or [])}
        passed = (observed["coverage_intact"]
                  is bool(wanted["coverage_intact"]))
        if wanted.get("problems_present") is not None:
            has = bool(observed["problem_kinds"]) or observed["gaps"] > 0
            passed = passed and has is bool(wanted["problems_present"])
        return observed, passed

    def _check_proof(self, case: dict[str, Any], wanted: Any, memory: Any):
        proof = call_proof(memory, family=str(wanted["family"]),
                           project_id=self.replay.projects[
                               int(wanted.get("project", case["project"]))])
        observed = {"reason": proof.get("reason")}
        return observed, proof.get("reason") in _as_list(wanted["reason"])

    def _check_promotion_stages(self, case: dict[str, Any], wanted: Any,
                                memory: Any):
        observed = {}
        passed = True
        for reference in sorted(wanted):
            promotion_id = self.replay.promotions.get(reference)
            row = (memory.ladder_promotion(int(promotion_id))
                   if promotion_id else None)
            stage = None if row is None else str(row["stage"])
            observed[reference] = stage
            passed = passed and stage in _as_list(wanted[reference])
        return observed, passed

    def _check_rollback_bytes(self, case: dict[str, Any], wanted: Any,
                              memory: Any):
        """Design 3.6 rollback equivalence, byte for byte.

        ``before`` is the live document as it stood immediately before the
        NAMED approval -- keyed on that approval's own record, so successive
        approvals of one skill each keep their own before image (ruling 30c).
        """
        rolled = self.replay.results.get(str(wanted["rollback_ref"])) or {}
        if str(wanted["approve_ref"]) not in self.replay.live_before_approve:
            return {"before_bytes": "no snapshot"}, False
        before = self.replay.live_before_approve[str(wanted["approve_ref"])]
        after = rolled.get("after_bytes")
        observed = {"before_bytes": None if before is None else len(before),
                    "after_bytes": None if after is None else len(after),
                    "shape": "removed" if after is None else "restored"}
        passed = before == after
        if wanted.get("expect"):
            passed = passed and observed["shape"] == str(wanted["expect"])
        return observed, passed

    def _check_degraded_writes(self, case: dict[str, Any], wanted: Any,
                               memory: Any):
        rows = memory.degraded_writes()
        reasons = sorted({str(row.get("reason")) for row in rows
                          if isinstance(row, dict)})
        observed = {"count": len(rows), "reasons": reasons}
        passed = True
        if "minimum" in wanted:
            passed = passed and len(rows) >= int(wanted["minimum"])
        if "maximum" in wanted:
            passed = passed and len(rows) <= int(wanted["maximum"])
        if wanted.get("reason"):
            passed = passed and str(wanted["reason"]) in reasons
        return observed, passed

    def _check_spine(self, case: dict[str, Any], wanted: Any, memory: Any):
        report = memory.verify_spine()
        observed = {"chain_ok": bool(report.get("chain_ok"))}
        return observed, observed["chain_ok"] is bool(wanted["chain_ok"])

    def _check_no_code_in_store(self, case: dict[str, Any], wanted: Any,
                                memory: Any):
        """Ruling 1 and 6.2 item 4: the code is an operator surface only."""
        material = []
        for table, column in (("messages", "content"),
                              ("activity_log", "details_json"),
                              ("memory_spine_events", "payload_json")):
            try:
                rows = memory.db.execute(
                    "SELECT {0} FROM {1}".format(column, table)).fetchall()
            except Exception:                            # noqa: BLE001
                continue
            material.extend(str(row[0]) for row in rows if row[0] is not None)
        blob = "\n".join(material)
        leaked = sorted({code for code in self.replay.codes.values()
                         if code and code in blob})
        return len(leaked), not leaked and bool(wanted)


def _seed_store(store: dict[str, Any], fixture: dict[str, Any], root: Path,
                memory_module: Any, memory: Any, ladder: Any,
                skill_library: Any, anchor: Any) -> _Replay:
    """The one seeding path.  The sealed run and the unsealed integrity test
    both call this, so no store can be scored through a script the integrity
    test never executed (ruling 31)."""
    replay = _Replay(memory_module, memory, store, fixture, root,
                     skill_library, ladder, anchor)
    replay.run()
    return replay


def _blank_detail(case: dict[str, Any], store: dict[str, Any],
                  message: str) -> dict[str, Any]:
    return {"case": str(case["id"]), "kind": str(case["kind"]),
            "store": str(store["id"]), "errors": [message],
            "rows_total": 0, "allowed_rows": 0, "expected_lessons": 0,
            "matched_lessons": 0, "forbidden_hits": 0, "leakage": 0,
            "staged_visibility": 0, "unverified_served": 0,
            "empty_recall_pass": None, "lesson_mode_pass": None,
            "skill_mode_pass": None, "cue_pass": None, "gate_pass": None,
            "closure_pass": None, "skill_pass": None, "op_pass": None,
            "check_results": [], "checks_pass": False, "elapsed_ms": 0.0,
            "write_ms": None, "reported_mode_case": 0,
            "reported_mode_agreement": 0, "observed": {}}


def _warm(replay: _Replay, cases: list[dict[str, Any]],
          fixture: dict[str, Any], memory: Any) -> None:
    """One untimed pass over every (family, project) this store will read.

    Design 1.4's budget is the warm one, and 7.9 measures it inside the
    activation the turn uses.  Only the gate and the lesson lane are warmed:
    reading the skill channel would run the withdrawal sweep and change what
    the first scored case of that project observes.
    """
    seen: set[tuple[str, int]] = set()
    for case in cases:
        if not case.get("question"):
            continue
        key = (str(case["family"]), int(case["project"]))
        if key in seen:
            continue
        seen.add(key)
        try:
            gate = call_gate(memory, key[0], fixture["gate_thresholds"])
            if gate.get("allowed"):
                call_match_lessons(memory, "warm the caches now", key[0],
                                   project_id=replay.projects[key[1]],
                                   limit=3)
        except Exception:                                # noqa: BLE001
            pass


def _score_store(store: dict[str, Any], cases: list[dict[str, Any]],
                 fixture: dict[str, Any], root: Path, memory_module: Any,
                 memory_factory: Any, ladder: Any, skill_library: Any,
                 anchor: Any) -> dict[str, Any]:
    store_root = root / str(store["id"])
    store_root.mkdir(parents=True, exist_ok=True)
    details: list[dict[str, Any]] = []
    seeding = {"store": str(store["id"]), "observed": {}, "expected": {},
               "script": {"checks": 0, "passes": 0, "wrong": []},
               "error": None}
    memory = memory_factory(str(store["id"]))
    try:
        try:
            replay = _seed_store(store, fixture, store_root, memory_module,
                                 memory, ladder, skill_library, anchor)
        except Exception as error:                       # noqa: BLE001
            seeding["error"] = "{0}: {1}".format(type(error).__name__, error)
            for case in cases:
                details.append(_blank_detail(
                    case, store,
                    "store did not seed: " + str(seeding["error"])))
            return {"seeding": seeding, "details": details}
        seeding["script"]["checks"] = len(replay.script_checks)
        seeding["script"]["passes"] = sum(1 for item in replay.script_checks
                                          if item["pass"])
        seeding["script"]["wrong"] = [item for item in replay.script_checks
                                      if not item["pass"]]
        seeding["observed"] = _observed_counts(memory, replay)
        seeding["expected"] = {key: store["expected_counts"][key]
                               for key in seeding["observed"]}
        # Design 3.7 consumer 1: reading the skill channel withdraws an
        # artefact that stopped verifying, so each bucket is snapshotted
        # once, read-only, before any case runs a read.
        snapshots: dict[str, Any] = {"unverified": [], "legacy": []}
        for logical in store["projects"]:
            workspace = replay.workspaces[int(logical)]
            project_id = replay.projects[int(logical)]
            try:
                snapshots["unverified"].extend(
                    dict(row) for row in call_unverified(
                        memory, workspace=workspace, project_id=project_id))
                snapshots["legacy"].extend(
                    dict(row) for row in call_legacy(
                        memory, workspace=workspace, project_id=project_id))
            except Exception as error:                   # noqa: BLE001
                snapshots.setdefault("errors", []).append(
                    "{0}: {1}".format(type(error).__name__, error))
        _warm(replay, cases, fixture, memory)

        def reopen() -> Any:
            return memory_factory(str(store["id"]))

        runner = _CaseRunner(memory, replay, ladder, skill_library, fixture,
                             snapshots, reopen)
        for case in cases:
            details.append(runner.run(case))
    finally:
        try:
            memory.close()
        except Exception:                                # noqa: BLE001
            pass
        ladder.clear_catalog_cache()
    return {"seeding": seeding, "details": details}


def _evaluate_holdout(memory_module: Any, memory_factory: Any,
                      fixture: dict[str, Any], root: Path, ladder: Any,
                      skill_library: Any, anchor: Any) -> dict[str, Any]:
    metrics = _blank_metrics()
    per_kind: dict[str, dict[str, Any]] = {}
    read_latencies: list[float] = []
    write_latencies: list[float] = []
    details: list[dict[str, Any]] = []
    seeding: list[dict[str, Any]] = []
    by_store: dict[str, list[dict[str, Any]]] = {}
    for case in fixture["cases"]:
        by_store.setdefault(str(case["store"]), []).append(case)

    for store in fixture["stores"]:
        result = _score_store(store, by_store.get(str(store["id"]), []),
                              fixture, root, memory_module, memory_factory,
                              ladder, skill_library, anchor)
        seeding.append(result["seeding"])
        for outcome in result["details"]:
            details.append(outcome)
            bucket = per_kind.setdefault(outcome["kind"], _blank_metrics())
            _accumulate(metrics, outcome)
            _accumulate(bucket, outcome)
        for case, outcome in zip(by_store.get(str(store["id"]), []),
                                 result["details"]):
            if case.get("timed") == "read" and outcome["elapsed_ms"]:
                read_latencies.append(float(outcome["elapsed_ms"]))
            if case.get("timed") == "write" and outcome.get("write_ms"):
                write_latencies.append(float(outcome["write_ms"]))

    ordered = sorted(read_latencies)
    if ordered:
        position = max(0, min(len(ordered) - 1,
                              int(round(0.95 * (len(ordered) - 1)))))
        p95 = ordered[position]
        mean_read = statistics.fmean(ordered)
    else:
        p95, mean_read = 0.0, 0.0
    script_checks = sum(int(entry["script"]["checks"]) for entry in seeding)
    script_passes = sum(int(entry["script"]["passes"]) for entry in seeding)
    # Ruling 36's discipline one layer up: every denominator this report
    # divides by is COUNTED from the cases that actually ran, never taken
    # from the fixture's declared list, and the sealed test asserts each one
    # against the number the fixture says it must be.  A case that errors, is
    # filtered out or never reaches its metric makes a denominator shrink and
    # the run fail, rather than making a ratio read 1.0 over nothing.
    scored_kinds: dict[str, int] = {}
    for outcome in details:
        scored_kinds[outcome["kind"]] = scored_kinds.get(
            outcome["kind"], 0) + 1
    denominators = {name: metrics[name] for name in (
        "cases", "expected_lessons", "empty_recall_cases",
        "lesson_mode_cases", "skill_mode_cases", "cue_cases", "gate_cases",
        "closure_cases", "skill_cases", "op_cases", "check_cases",
        "rollback_cases", "reported_mode_cases")}
    return {
        "holdout": fixture["holdout"],
        "stores": len(fixture["stores"]),
        "cases": len(fixture["cases"]),
        "scored_kinds": dict(sorted(scored_kinds.items())),
        "denominators": denominators,
        "seeding": seeding,
        "script_accuracy": (script_passes / script_checks
                            if script_checks else 1.0),
        "script_checks": script_checks,
        "script_passes": script_passes,
        "aggregate": _finalize(metrics),
        "kinds": {name: _finalize(bucket)
                  for name, bucket in sorted(per_kind.items())},
        "read_latency": {"samples": len(ordered), "p95_ms": p95,
                         "mean_ms": mean_read,
                         "max_ms": ordered[-1] if ordered else 0.0},
        "write_latency": {"samples": len(write_latencies),
                          "max_ms": max(write_latencies)
                          if write_latencies else 0.0,
                          "mean_ms": statistics.fmean(write_latencies)
                          if write_latencies else 0.0},
        "failures": [outcome for outcome in details
                     if (outcome["errors"] or outcome["forbidden_hits"]
                         or outcome["leakage"]
                         or outcome["staged_visibility"]
                         or outcome["unverified_served"]
                         or outcome["lesson_mode_pass"] is False
                         or outcome["skill_mode_pass"] is False
                         or outcome["cue_pass"] is False
                         or outcome["gate_pass"] is False
                         or outcome["closure_pass"] is False
                         or outcome["skill_pass"] is False
                         or outcome["op_pass"] is False
                         or outcome["empty_recall_pass"] is False
                         or not outcome["checks_pass"]
                         or outcome["matched_lessons"]
                         != outcome["expected_lessons"])],
        "all": details,
    }
# -- END SEALED LEARNING LADDER HOLDOUT V7 SCORER --


_SENSITIVE_PATTERNS = (
    r"(?i)https?://",
    r"(?i)[a-z]:[\\/](?:users|documents|desktop)[\\/]",
    r"(?i)\b[\w.+-]+@[\w.-]+\.[a-z]{2,}\b",
    r"\b(?:\d{1,3}\.){3}\d{1,3}\b",
    r"(?i)-----BEGIN [A-Z ]+-----",
    r"(?i)\bsk-[a-z0-9]{12,}\b",
    # split so the list cannot match itself when the scan runs over this
    # module's own bytes
    r"(?i)\b(?:pass" + r"word|pass" + r"wd|api[_ -]?key|private[_ -]?key)\b",
)

_CASE_KINDS = frozenset({
    "inject_hit", "inject_miss", "gate_closed", "pool_overflow",
    "unknown_identity", "out_of_project", "none_eligible",
    "superseded_lesson", "expired_lesson", "contradicted_lesson",
    "cross_family", "staged_invisible", "legacy_live",
    "unverified_promotion", "approve_ok", "approve_refused",
    "rollback_exact", "ledger_monotone", "ledger_regression",
})
_READ_KINDS = frozenset({
    "inject_hit", "inject_miss", "gate_closed", "pool_overflow",
    "unknown_identity", "out_of_project", "none_eligible",
    "superseded_lesson", "expired_lesson", "contradicted_lesson",
    "cross_family", "staged_invisible", "legacy_live",
})
_WRITE_KINDS = frozenset({"approve_ok", "approve_refused", "rollback_exact"})
_LESSON_MODES = frozenset({
    "idle", "complete", "no-match", "screened", "family-unsupported",
    "project-ambiguous", "authority-evasion", "pool-overflow", "error",
    "unknown-identity", "cross-family-stronger", "out-of-project",
    "cross-project-stronger", "none-eligible", "ineligible-shadow",
    "ineligible-prefix",
})
_SKILL_MODES = frozenset({
    "idle", "gate-closed", "no-prediction", "no-project", "none-approved",
    "unverified-withdrawn", "legacy-only", "legacy-live", "complete",
})
_GATE_CLOSURES = frozenset({"insufficient", "calibration"})
_STAGE_REFUSALS = frozenset({
    "family_unsupported", "family_excluded", "gate_closed",
    "ledger_regressed", "no_epoch", "no_eligible_lesson",
    "insufficient_reuse", "insufficient_effectiveness", "screened_component",
    "document_unchanged", "staging_exists", "workspace_mismatch",
    "spine_unavailable", "staging_write_failed", "proof_unbacked",
    "proof_stale",
})
_APPROVE_REFUSALS = frozenset({
    "missing", "not_staged", "token_mismatch", "token_malformed",
    "proof_stale", "gate_closed", "ledger_regressed", "staged_missing",
    "staged_digest_mismatch", "live_digest_unexpected", "screened_component",
    "workspace_mismatch", "spine_unavailable",
})
_ROLLBACK_REFUSALS = frozenset({
    "missing", "not_approved", "not_newest", "pruned",
    "live_digest_unexpected", "workspace_mismatch", "spine_unavailable",
})
_DISCARD_REFUSALS = frozenset({"missing", "not_staged", "workspace_mismatch",
                               "spine_unavailable"})
# Ruling 28: TEN, not the eight of design 3.7 -- ``proof_unbacked`` (ruling 21)
# and ``live_document_missing`` (an approved row whose file is gone, split from
# ``orphan_document``, which is a live file no row claims) join the eight, and
# ``learning_ladder.LADDER_UNVERIFIED_REASONS`` is the one definition.
_UNVERIFIED_REASONS = frozenset({
    "no_approved_row", "digest_mismatch", "proof_stale", "gate_closed",
    "ledger_regressed", "lineage_broken", "screened_component",
    "orphan_document", "proof_unbacked", "live_document_missing",
})
_VALUE_TEMPLATES = frozenset({
    "secret_token", "secret_assignment", "ipv4_private", "ipv4_exempt",
    "phone_us", "ssn", "card_luhn", "street_address", "email_person",
    "user_home_windows", "version", "isbn13", "long_prose", "long_value",
    "tool_name",
})
_TAMPER_TARGETS = frozenset({
    "corrupt_spine_head", "quarantine_lesson", "delete_promotion_row",
    "remove_live_document", "edit_live_document", "tool_name",
    "ledger_boundary", "ledger_covered_ids", "flip_outcome",
})
_STORE_COUNT_KEYS = ("predictions", "lessons", "applications", "epochs",
                     "promotions", "staged_files", "live_files",
                     "parked_files")
# Ruling 34 P3: identity scope with SINGLE-TOKEN subjects.  No lesson in this
# fixture contains a proper-cased word except an identity subject, and these
# are the only capitalised mid-sentence tokens a question may carry -- each of
# them a crew the store has never seen.
_DELIBERATE_UNSEEN = frozenset({"Astrivane", "Dunmarrow", "Elgareth",
                                "Pellowin", "Rothwaine", "Sennovar"})
_IDENTITY_SUBJECTS = ("Bardolf", "Ingrith", "Corvell", "Merisant", "Thelwin")
# Ruling 36's precondition, expressed as something a fixture can PROVE rather
# than as a model of a tokenizer this module may not read.
# ``_rank_memory_rows`` applies ``minimum_matches`` before scoring: one
# matched term suffices only when the query reduces to two terms or the
# longest matched term reaches seven characters.  The second limb is a
# property of the marker and is checkable from the bytes.  The first is a
# property of M1's own stopword handling, which is not this module's to guess
# -- so a case may claim it only by using the EXACT question shape that was
# executed against the frozen tree during the build and observed to retrieve.
# Every inject_hit declares the limb it relies on, and the declaration is
# re-derived here rather than believed.
WORD_BOUND = r"\b{0}\b"
_RANKER_ANCHOR_LENGTH = 7
_MEASURED_TWO_TERM = "What do we know about {0}?"
_RANKER_LIMBS = frozenset({"anchor", "two_term"})
# Question openers, so a sentence-initial capital is not mistaken for a name.
_QUESTION_OPENERS = frozenset({"What", "Is", "Remind", "Any", "How", "Whose",
                               "Where", "When", "Which", "Do", "Does", "Can"})


def _ladder_modules():
    """Import the scoring path, or say why the tree cannot supply it."""
    import jarvis.learning_ladder as ladder
    import jarvis.memory as memory_module
    import jarvis.skill_library as skill_library
    from jarvis.memory import Memory

    for attribute in ("stage_ladder_promotion", "apply_ladder_promotion",
                      "seal_calibration_epoch", "ladder_proof",
                      "ladder_pending_withdrawals", "park_orphan_document",
                      "calibration_ledger_monotonicity"):
        if not hasattr(Memory, attribute):
            raise RuntimeError(
                "Memory.{0} is absent: the M4 learning ladder is not present "
                "in this tree".format(attribute))
    return ladder, memory_module, skill_library, Memory


class SealedLadderHoldoutIntegrityTests(unittest.TestCase):
    """Unsealed checks.  These run in the ordinary suite and must stay green."""

    def setUp(self) -> None:
        self.fixture_bytes = FIXTURE_PATH.read_bytes()
        self.fixture = json.loads(self.fixture_bytes.decode("utf-8"))

    def test_fixture_and_scorer_are_sealed(self) -> None:
        if _seal_is_placeholder():
            self.skipTest(
                "the fixture and scorer digests are still placeholders: the "
                "boss stamps them with claude-reseal-runtime-pins.py before "
                "this holdout is scored")
        self.assertEqual(hashlib.sha256(self.fixture_bytes).hexdigest(),
                         FIXTURE_SHA256)
        self.assertEqual(hashlib.sha256(_sealed_scorer_bytes()).hexdigest(),
                         SCORER_SHA256)
        self.assertEqual(
            _required_run_token(),
            hashlib.sha256("{0}:{1}".format(FIXTURE_SHA256, SCORER_SHA256)
                           .encode("ascii")).hexdigest())

    def test_runtime_pin_names_exactly_the_four_files(self) -> None:
        pin = self.fixture["runtime_sha256"]
        self.assertEqual(tuple(pin), PINNED_FILES)
        self.assertNotIn("jarvis/agent.py", pin)
        self.assertNotIn("jarvis/proactive.py", pin)
        self.assertNotIn("jarvis/tools.py", pin)
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

    def test_no_confirmation_code_is_written_into_the_fixture(self) -> None:
        """Design 7.14: a fixture that contains a token is rejected at load."""
        forbidden = {"token", "approval_token", "code", "approval"}

        def walk(node: Any, path: str) -> None:
            if isinstance(node, dict):
                for key, value in node.items():
                    self.assertNotIn(str(key).lower(), forbidden,
                                     "{0}.{1}".format(path, key))
                    walk(value, "{0}.{1}".format(path, key))
            elif isinstance(node, list):
                for index, value in enumerate(node):
                    walk(value, "{0}[{1}]".format(path, index))

        walk(self.fixture, "fixture")
        sources = [str(record.get("code_source"))
                   for store in self.fixture["stores"]
                   for record in store["records"] if record["op"] == "approve"]
        self.assertTrue(sources)
        self.assertLessEqual(set(sources),
                             {"correct", "wrong", "malformed", "None"})

    def test_counts_and_case_structure_meet_the_design_floors(self) -> None:
        fixture = self.fixture
        totals = fixture["expected_counts"]
        stores = fixture["stores"]
        cases = fixture["cases"]
        store_ids = [str(store["id"]) for store in stores]
        case_ids = [str(case["id"]) for case in cases]
        record_ids = [str(record["id"]) for store in stores
                      for record in store["records"]]
        self.assertEqual(len(store_ids), len(set(store_ids)))
        self.assertEqual(len(case_ids), len(set(case_ids)))
        self.assertEqual(len(record_ids), len(set(record_ids)))
        self.assertEqual(len(stores), totals["stores"])
        self.assertEqual(len(cases), totals["cases"])
        self.assertEqual(len(record_ids), totals["records"])
        self.assertGreaterEqual(len(cases), 120)
        self.assertGreaterEqual(totals["predictions"], 400)
        self.assertGreaterEqual(totals["lessons"], 30)
        self.assertGreaterEqual(totals["applications"], 60)
        self.assertGreaterEqual(totals["epochs"], 16)
        self.assertGreaterEqual(len(stores), 4)
        families = {str(record["family"]) for store in stores
                    for record in store["records"]
                    if record["op"] == "outcomes"}
        self.assertGreaterEqual(len(families), 4)
        self.assertGreaterEqual(
            sum(len(store["projects"]) for store in stores), 2)
        operations: dict[str, int] = {}
        for store in stores:
            for record in store["records"]:
                operations[str(record["op"])] = operations.get(
                    str(record["op"]), 0) + 1
        self.assertGreaterEqual(operations.get("stage", 0), 6)
        self.assertGreaterEqual(operations.get("approve", 0), 4)
        self.assertGreaterEqual(operations.get("rollback", 0), 3)
        self.assertGreaterEqual(operations.get("legacy_document", 0), 2)
        self.assertGreaterEqual(operations.get("grandfather", 0), 1)
        # Design 7.14 asks for at least two grandfathered DOCUMENTS, not two
        # passes: one pass over a workspace holding two pre-M4 documents
        # adopts both, and the pass asserts the count it adopted.
        adopted = sum(int((record.get("expect") or {}).get("adopted_count")
                          or 0)
                      for store in stores for record in store["records"]
                      if record["op"] == "grandfather")
        self.assertGreaterEqual(adopted, 2)
        self.assertGreaterEqual(operations.get("discard", 0), 1)
        self.assertGreaterEqual(operations.get("tamper", 0), 8)
        counted: dict[str, int] = {}
        for case in cases:
            counted[str(case["kind"])] = counted.get(str(case["kind"]), 0) + 1
        self.assertEqual(counted, totals["kinds"])
        self.assertEqual(set(counted), _CASE_KINDS)
        for kind, total in sorted(counted.items()):
            self.assertGreaterEqual(total, 6, kind)

    def test_every_case_carries_the_designed_fields(self) -> None:
        required = ("id", "kind", "store", "project", "family", "question",
                    "expect_lessons", "expect_lesson_mode",
                    "expect_skill_mode", "expect_cue", "expect_skill",
                    "forbid_refs", "mode_gated",
                    "skill_mode_gated", "cue_gated", "recall_gated",
                    "checks", "timed", "note")
        store_ids = {str(store["id"]) for store in self.fixture["stores"]}
        for case in self.fixture["cases"]:
            for field in required:
                self.assertIn(field, case, case["id"])
            self.assertIn(str(case["store"]), store_ids, case["id"])
            for mode in _as_list(case["expect_lesson_mode"]):
                self.assertIn(mode, _LESSON_MODES, case["id"])
            for mode in _as_list(case["expect_skill_mode"]):
                self.assertIn(mode, _SKILL_MODES, case["id"])
            self.assertIn(case["expect_cue"], (True, False), case["id"])
            self.assertIn(case["kind"], _CASE_KINDS, case["id"])
            self.assertTrue(str(case["note"]).strip(), case["id"])
            if case["kind"] in _WRITE_KINDS:
                self.assertEqual(case["timed"], "write", case["id"])
                self.assertTrue(case.get("op_ref"), case["id"])
            if case["kind"] in _READ_KINDS:
                self.assertTrue(case["question"], case["id"])
                self.assertIn(case["timed"], ("read", None), case["id"])
            if case.get("expect_gate_closure") is not None:
                self.assertIn(case["expect_gate_closure"], _GATE_CLOSURES,
                              case["id"])
            for reason in _as_list(case.get("expect_refusal")):
                if reason is None:
                    continue
                self.assertIn(reason,
                              _APPROVE_REFUSALS | _ROLLBACK_REFUSALS
                              | _DISCARD_REFUSALS | _STAGE_REFUSALS,
                              case["id"])
            if case["recall_gated"] and case["expect_lessons"]:
                self.assertTrue(case["mode_gated"], case["id"])

    def test_a_gated_cue_never_depends_on_a_reported_mode(self) -> None:
        """The cue is a function of both modes, so gating it needs cover.

        Three shapes are admissible, and nothing else is: both modes gated;
        or an expected FALSE cue where no mode in either closed set can fire
        it; or an expected TRUE cue carried by a gated, UNCONDITIONAL skill
        abstention mode.  A cue gated on top of a reported mode would be
        gating the ranker's discretion, which is precisely what the reported
        column exists to avoid.
        """
        checked = 0
        for case in self.fixture["cases"]:
            if not case["cue_gated"]:
                continue
            checked += 1
            if case["mode_gated"] and case["skill_mode_gated"]:
                continue
            lesson_modes = set(_as_list(case["expect_lesson_mode"]))
            skill_modes = set(_as_list(case["expect_skill_mode"]))
            if not case["expect_cue"]:
                self.assertFalse(lesson_modes & CUE_LESSON_MODES, case["id"])
                self.assertFalse(skill_modes & CUE_SKILL_MODES, case["id"])
                continue
            self.assertTrue(case["skill_mode_gated"], case["id"])
            self.assertLessEqual(
                skill_modes, CUE_SKILL_MODES - CUE_CONDITIONAL_SKILL_MODES,
                case["id"])
        self.assertGreaterEqual(checked, 40)

    def test_read_and_write_budgets_cover_disjoint_case_sets(self) -> None:
        """H-9(c): a 300 ms operator write is never timed against a 25 ms p95.

        A read case is timed only when an earlier case has already read the
        same (store, project), so every timed sample is a WARM one -- design
        1.4 states the 25 ms budget warm and the 40 ms one cold, and 7.9
        measures the warm path.  The cold first read of each project is still
        scored, just not timed.
        """
        timings = {str(case["id"]): case["timed"]
                   for case in self.fixture["cases"]}
        read = {name for name, value in timings.items() if value == "read"}
        write = {name for name, value in timings.items() if value == "write"}
        self.assertEqual(read & write, set())
        self.assertGreaterEqual(len(read), 40)
        self.assertGreaterEqual(len(write), 10)
        seen: set[tuple[str, int]] = set()
        for case in self.fixture["cases"]:
            key = (str(case["store"]), int(case["project"]))
            if case["timed"] == "write":
                self.assertIn(case["kind"], _WRITE_KINDS, case["id"])
            if case["timed"] == "read":
                self.assertIn(case["kind"], _READ_KINDS, case["id"])
                self.assertIn(key, seen, case["id"])
            if case.get("question"):
                seen.add(key)

    def test_every_injection_hit_clears_the_ranker_precondition(self) -> None:
        """RULING 36, the structural half of what cost v6 its run.

        v6 wrote an ``inject_hit`` whose question had three terms and a
        six-character marker, and so measured M1's ranker rather than the
        ladder.  Every ``inject_hit`` here declares which limb of the
        precondition it relies on, and the declaration is RE-DERIVED from the
        fixture bytes rather than believed: ``anchor`` means an expected
        marker of at least seven characters appears in the question, and
        ``two_term`` means the question is byte for byte the shape that was
        executed against the frozen tree during the build and observed to
        retrieve.  Nothing here models M1's stopword list, because this
        module may not read it and a wrong model would be worse than none.
        """
        markers = self._markers_by_lesson()
        hits = [case for case in self.fixture["cases"]
                if case["kind"] == "inject_hit"]
        self.assertGreaterEqual(len(hits), 6)
        limbs: dict[str, int] = {}
        for case in hits:
            self.assertTrue(case["expect_lessons"], case["id"])
            self.assertTrue(case["recall_gated"], case["id"])
            self.assertTrue(case["mode_gated"], case["id"])
            limb = case.get("ranker_limb")
            self.assertIn(limb, _RANKER_LIMBS, case["id"])
            limbs[limb] = limbs.get(limb, 0) + 1
            named = [markers[ref] for ref in case["expect_lessons"]]
            question = str(case["question"])
            if limb == "anchor":
                self.assertTrue(
                    any(len(name) >= _RANKER_ANCHOR_LENGTH
                        and name.lower() in question.lower()
                        for name in named),
                    "{0}: no marker of {1}+ characters in {2}".format(
                        case["id"], _RANKER_ANCHOR_LENGTH, question))
            else:
                self.assertEqual(len(named), 1, case["id"])
                self.assertEqual(question,
                                 _MEASURED_TWO_TERM.format(named[0]),
                                 case["id"])
        # Both limbs must actually be exercised, or the abstention pin below
        # has no partner and the knife-edge is not isolated.
        self.assertGreaterEqual(limbs.get("anchor", 0), 5)
        self.assertGreaterEqual(limbs.get("two_term", 0), 1)

    def test_the_ranker_limitation_is_pinned_not_designed_around(self) -> None:
        """RULING 36's other order: record the limitation inside the gate.

        There must be a case that names a SHORT marker under a question that
        is NOT the measured two-term shape, expects nothing, gates that
        emptiness and the forbidden refs, and only REPORTS the mode -- so a
        later reader sees that M4 shipped knowing about the M1 ranker floor
        rather than having quietly routed around it.  Its partner must show
        the same marker retrieving under the two-term shape, or the pin would
        not isolate the question shape as the cause.
        """
        markers = self._markers_by_lesson()
        pins = [case for case in self.fixture["cases"]
                if "RULING 36" in str(case["note"])]
        self.assertEqual(len(pins), 1, "exactly one abstention pin")
        pin = pins[0]
        question = str(pin["question"])
        named = sorted({marker for marker in markers.values()
                        if re.search(WORD_BOUND.format(re.escape(marker)),
                                     question)}, key=len)
        self.assertEqual(len(named), 1, "{0}: {1}".format(pin["id"], named))
        marker = named[0]
        self.assertLess(len(marker), _RANKER_ANCHOR_LENGTH, marker)
        self.assertNotEqual(question, _MEASURED_TWO_TERM.format(marker))
        self.assertEqual(pin["expect_lessons"], [], pin["id"])
        self.assertTrue(pin["recall_gated"], pin["id"])
        self.assertTrue(pin["forbid_refs"], pin["id"])
        self.assertFalse(pin["mode_gated"], pin["id"])
        partners = [case for case in self.fixture["cases"]
                    if case["kind"] == "inject_hit"
                    and case["store"] == pin["store"]
                    and case["question"] == _MEASURED_TWO_TERM.format(marker)]
        self.assertEqual(len(partners), 1,
                         "the short marker must retrieve under the measured "
                         "two-term shape")
        self.assertEqual(partners[0]["ranker_limb"], "two_term")
        self.assertEqual(sorted(partners[0]["expect_lessons"]),
                         sorted(ref for ref, value in markers.items()
                                if value == marker))

    def test_the_pending_withdrawal_reader_is_given_the_live_set(self) -> None:
        """RULING 36's first half, pinned as behaviour and not as prose.

        ``ladder_pending_withdrawals`` is row backed when it is called bare,
        and a parked orphan has no row, so every call inside the sealed
        region supplies the live workspace.  v6 called it bare and scored
        three correct answers as product failures.  This reads the sealed
        bytes rather than trusting the sentence above it.
        """
        source = _sealed_scorer_bytes().decode("utf-8")
        calls = list(re.finditer(r"call_pending\(", source))
        self.assertEqual(len(calls), 2)
        for match in calls:
            tail = source[match.end():match.end() + 160]
            self.assertIn("workspace=", tail)
        self.assertNotIn("call_pending(memory, self._project(case))", source)

    def _markers_by_lesson(self) -> dict[str, str]:
        markers: dict[str, str] = {}
        for store in self.fixture["stores"]:
            for record in store["records"]:
                reflection = record.get("reflect")
                if not reflection:
                    continue
                repeat = int(record.get("repeat") or 1)
                base = str(reflection["lesson_id"])
                marker = str(reflection["marker"])
                if repeat == 1:
                    markers[base] = marker
                else:
                    for index in range(repeat):
                        markers["{0}#{1}".format(base, index)] = (
                            "{0}{1:04d}".format(marker, index))
        return markers

    def test_only_deliberate_questions_name_an_unseen_entity(self) -> None:
        """Ruling 33 and ruling 34 P3, as a structural rule.

        Every identity-scoped lesson in this fixture uses a SINGLE-TOKEN
        subject, and no other lesson contains a proper-cased word, so any
        capitalised mid-sentence token in a question is an entity the store
        has never seen.  Every such token is declared, and every case that
        carries one is an ``unknown_identity`` case that gates empty recall
        and the forbidden refs while only reporting the mode.
        """
        pattern = re.compile(r"\b([A-Z][A-Za-z0-9-]*)\b")
        naming: set[str] = set()
        for case in self.fixture["cases"]:
            question = case.get("question")
            if not question:
                continue
            found = set(pattern.findall(question))
            opener = str(question).split(" ", 1)[0].strip("'.,?:")
            if opener in _QUESTION_OPENERS:
                found.discard(opener)
            self.assertLessEqual(found, _DELIBERATE_UNSEEN,
                                 "{0}: {1}".format(case["id"], question))
            if found:
                naming.add(case["id"])
                self.assertEqual(case["kind"], "unknown_identity", case["id"])
                self.assertEqual(case["expect_lessons"], [], case["id"])
                self.assertTrue(case["recall_gated"], case["id"])
                self.assertTrue(case["forbid_refs"], case["id"])
                self.assertFalse(case["mode_gated"], case["id"])
                self.assertFalse(case["cue_gated"], case["id"])
        self.assertGreaterEqual(len(naming), 6)
        subjects: list[str] = []
        for store in self.fixture["stores"]:
            for record in store["records"]:
                reflection = record.get("reflect") or {}
                text = str(reflection.get("improvements") or "")
                if text.startswith("For "):
                    subjects.append(str(reflection["marker"]))
        self.assertTrue(subjects)
        for subject in subjects:
            self.assertNotIn(" ", subject, subject)
            self.assertIn(subject, _IDENTITY_SUBJECTS, subject)
        self.assertEqual(set(_DELIBERATE_UNSEEN) & set(subjects), set())

    def test_every_referenced_ref_exists_in_its_store(self) -> None:
        lessons_by_store: dict[str, set[str]] = {}
        records_by_store: dict[str, set[str]] = {}
        for store in self.fixture["stores"]:
            lessons: set[str] = set()
            for record in store["records"]:
                if record["op"] != "outcomes" or not record.get("reflect"):
                    continue
                repeat = int(record.get("repeat") or 1)
                statuses = _statuses(record)
                base = str(record["reflect"]["lesson_id"])
                if repeat == 1:
                    lessons.add(base)
                else:
                    lessons.update("{0}#{1}".format(base, index)
                                   for index in range(repeat)
                                   if statuses[index] == "+")
            lessons_by_store[str(store["id"])] = lessons
            records_by_store[str(store["id"])] = {str(record["id"])
                                                  for record in
                                                  store["records"]}
        for case in self.fixture["cases"]:
            known = lessons_by_store[str(case["store"])]
            for ref in list(case["expect_lessons"]) + list(
                    case["forbid_refs"]):
                self.assertIn(ref, known,
                              "{0} -> {1}".format(case["id"], ref))
            if case.get("op_ref"):
                self.assertIn(str(case["op_ref"]),
                              records_by_store[str(case["store"])],
                              case["id"])
            checks = case.get("checks") or {}
            rollback = checks.get("rollback_bytes")
            if rollback:
                for key in ("rollback_ref", "approve_ref"):
                    self.assertIn(str(rollback[key]),
                                  records_by_store[str(case["store"])],
                                  case["id"])
            for reference in sorted(checks.get("promotion_stages") or {}):
                # A grandfathered row is addressed as ``<record>::<skill>``,
                # because one adoption pass can create several rows.
                head = str(reference).split("::", 1)[0]
                self.assertIn(head, records_by_store[str(case["store"])],
                              case["id"])

    def test_scripts_are_well_formed(self) -> None:
        staged_by_store: dict[str, set[str]] = {}
        for store in self.fixture["stores"]:
            projects = {int(value) for value in store["projects"]}
            self.assertEqual(sorted(int(key) for key in store["paths"]),
                             sorted(projects))
            written: set[str] = set()
            staged: set[str] = set()
            for record in store["records"]:
                operation = str(record["op"])
                if operation == "outcomes":
                    repeat = int(record.get("repeat") or 1)
                    self.assertGreaterEqual(repeat, 1)
                    statuses = record.get("statuses")
                    if statuses is not None:
                        self.assertEqual(len(statuses), repeat, record["id"])
                        self.assertLessEqual(set(statuses), {"+", "-"},
                                             record["id"])
                    evidence = record.get("evidence")
                    if evidence is not None:
                        self.assertEqual(len(evidence), repeat, record["id"])
                        self.assertLessEqual(set(evidence), {"0", "1", "?"},
                                             record["id"])
                    self.assertIn(int(record["project"]), projects)
                    self.assertGreaterEqual(
                        float(record["predicted_success"]), 0.0)
                    self.assertLessEqual(
                        float(record["predicted_success"]), 1.0)
                    for ref in record.get("apply_lessons") or []:
                        self.assertIn(ref, written, record["id"])
                    reflection = record.get("reflect")
                    if reflection:
                        marks = _statuses(record)
                        base = str(reflection["lesson_id"])
                        if repeat == 1:
                            written.add(base)
                        else:
                            written.update("{0}#{1}".format(base, index)
                                           for index in range(repeat)
                                           if marks[index] == "+")
                        self.assertTrue(
                            str(reflection["improvements"]).strip(),
                            record["id"])
                        self.assertIn("{marker}",
                                      str(reflection["improvements"]),
                                      record["id"])
                elif operation in ("supersede_lesson", "plant_applications"):
                    for key in ("lesson_ref", "replacement_ref"):
                        if record.get(key):
                            self.assertIn(str(record[key]), written,
                                          record["id"])
                elif operation == "stage":
                    self.assertIn(int(record["project"]), projects)
                    if (record.get("expect") or {}).get("staged"):
                        staged.add(str(record["id"]))
                    reason = (record.get("expect") or {}).get("reason")
                    if reason:
                        for value in _as_list(reason):
                            self.assertIn(value, _STAGE_REFUSALS,
                                          record["id"])
                elif operation == "tamper":
                    self.assertIn(str(record["target"]), _TAMPER_TARGETS,
                                  record["id"])
                elif operation in ("approve", "rollback", "discard"):
                    if not record.get("missing"):
                        self.assertTrue(record.get("promotion_ref"),
                                        record["id"])
            staged_by_store[str(store["id"])] = staged
        self.assertGreaterEqual(
            sum(len(value) for value in staged_by_store.values()), 6)

    def test_thresholds_and_constants_are_the_designed_ones(self) -> None:
        thresholds = self.fixture["thresholds"]
        self.assertEqual(thresholds["injection_precision"], 1.0)
        self.assertEqual(thresholds["injection_recall"], 0.90)
        self.assertEqual(thresholds["mode_accuracy"], 1.0)
        self.assertEqual(thresholds["cue_accuracy"], 1.0)
        self.assertEqual(thresholds["unverified_promotions"], 0)
        self.assertEqual(thresholds["rollback_exactness"], 1.0)
        self.assertEqual(thresholds["staged_visibility"], 0)
        self.assertEqual(thresholds["leakage"], 0)
        self.assertEqual(thresholds["monotonicity_accuracy"], 1.0)
        self.assertEqual(thresholds["read_p95_ms"], 25)
        self.assertEqual(thresholds["write_max_ms"], 300)
        self.assertEqual(self.fixture["gate_thresholds"], {
            "minimum_attempts": 20, "maximum_brier": 0.25,
            "maximum_calibration_error": 0.15, "minimum_success_rate": 0.70,
            "minimum_evidence_rate": 0.70})
        constants = self.fixture["constants"]
        self.assertEqual(constants["LADDER_EPOCH_SIZE"], 20)
        self.assertEqual(constants["LADDER_REGRESSION_STREAK"], 2)
        self.assertEqual(constants["LADDER_WITHHELD_CAP"], 50)
        self.assertEqual(constants["LADDER_MIN_VERIFIED_REUSES"], 3)
        self.assertEqual(constants["LADDER_EFFECTIVENESS_MIN_APPLIED"], 10)
        self.assertEqual(constants["LADDER_PRIOR_DOCUMENT_RETAINED"], 1)

    def test_the_baked_constants_match_the_module(self) -> None:
        """Design 1.5: a drift is a finding, not a silent holdout change."""
        try:
            import jarvis.learning_ladder as ladder
        except Exception as error:                       # noqa: BLE001
            self.skipTest("learning_ladder unavailable: {0}".format(error))
        constants = self.fixture["constants"]
        self.assertEqual(dict(ladder.LADDER_GATE_THRESHOLDS),
                         self.fixture["gate_thresholds"])
        self.assertEqual(ladder.LADDER_EPOCH_SIZE,
                         constants["LADDER_EPOCH_SIZE"])
        self.assertEqual(ladder.LADDER_REGRESSION_STREAK,
                         constants["LADDER_REGRESSION_STREAK"])
        self.assertEqual(ladder.LADDER_WITHHELD_CAP,
                         constants["LADDER_WITHHELD_CAP"])
        self.assertEqual(ladder.LADDER_MIN_VERIFIED_REUSES,
                         constants["LADDER_MIN_VERIFIED_REUSES"])
        self.assertEqual(ladder.LADDER_EFFECTIVENESS_MIN_APPLIED,
                         constants["LADDER_EFFECTIVENESS_MIN_APPLIED"])
        self.assertEqual(ladder.LADDER_PRIOR_DOCUMENT_RETAINED,
                         constants["LADDER_PRIOR_DOCUMENT_RETAINED"])
        self.assertEqual(tuple(ladder.LADDER_RUNTIME_FILES), PINNED_FILES)
        self.assertEqual(set(self.fixture["runtime_sha256"]),
                         set(PINNED_FILES))
        self.assertEqual(set(ladder.LESSON_RECALL_MODES), set(_LESSON_MODES))
        self.assertEqual(set(ladder.SKILL_CHANNEL_MODES), set(_SKILL_MODES))
        self.assertEqual(set(ladder.LESSON_ABSTENTION_MODES),
                         set(CUE_LESSON_MODES))
        self.assertEqual(set(ladder.SKILL_ABSTENTION_MODES),
                         set(CUE_SKILL_MODES))
        self.assertEqual(set(ladder.SKILL_CONDITIONAL_CUE_MODES),
                         set(CUE_CONDITIONAL_SKILL_MODES))
        self.assertEqual(set(_UNVERIFIED_REASONS),
                         set(ladder.LADDER_UNVERIFIED_REASONS))
        self.assertEqual(len(_UNVERIFIED_REASONS), 10)
        self.assertEqual(ladder.LEARNED_SKILL_DIRECTORY, LEARNED_DIRECTORY)
        self.assertEqual(set(ladder.LADDER_EXCLUDED_FAMILIES),
                         {"conversation"})

    def test_directives_expand_deterministically_and_stay_out_of_the_bytes(
            self) -> None:
        seed = int(self.fixture["generator_seed"])
        directives: list[tuple[str, dict[str, Any], str]] = []
        for store in self.fixture["stores"]:
            for record in store["records"]:
                for field in ("primary_tool", "value"):
                    value = record.get(field)
                    if isinstance(value, dict):
                        directives.append((str(record["id"]), value, field))
        for case in self.fixture["cases"]:
            value = case.get("question_directive")
            if isinstance(value, dict):
                directives.append((str(case["id"]), value, "question"))
        self.assertGreaterEqual(len(directives), 3)
        templates = {str(item[1]["value_template"]) for item in directives}
        self.assertGreaterEqual(len(templates), 2)
        self.assertLessEqual(templates, _VALUE_TEMPLATES)
        material = self.fixture_bytes.decode("utf-8")
        for record_id, directive, field in directives:
            first = _expand_directive(directive, seed, record_id, field)
            second = _expand_directive(directive, seed, record_id, field)
            self.assertEqual(first, second, record_id)
            self.assertTrue(first)
            self.assertNotIn(first, material, record_id)

    def test_screening_directives_really_screen(self) -> None:
        try:
            import jarvis.learning_ladder as ladder
        except Exception as error:                       # noqa: BLE001
            self.skipTest("learning_ladder unavailable: {0}".format(error))
        seed = int(self.fixture["generator_seed"])
        for template in ("secret_token", "secret_assignment"):
            text = _expand_directive({"value_template": template}, seed,
                                     "probe", "value")
            self.assertTrue(ladder.contains_secret(text), template)
        for template in ("ssn", "ipv4_private", "phone_us"):
            text = _expand_directive({"value_template": template}, seed,
                                     "probe", "value")
            flagged = bool(ladder.contains_secret(text))
            if not flagged:
                flagged = bool(ladder.screen_endpoint(text)[0])
            self.assertTrue(flagged, template)
        benign = _expand_directive({"value_template": "tool_name"}, seed,
                                   "probe", "value")
        self.assertRegex(benign, r"\A[a-z][a-z0-9_]{0,63}\Z")
        self.assertFalse(ladder.contains_secret(benign))

    def test_expected_reasons_are_in_their_closed_sets(self) -> None:
        for store in self.fixture["stores"]:
            for record in store["records"]:
                expected = record.get("expect") or {}
                reason = expected.get("reason")
                if not reason:
                    continue
                pool = {"stage": _STAGE_REFUSALS,
                        "approve": _APPROVE_REFUSALS,
                        "rollback": _ROLLBACK_REFUSALS,
                        "discard": _DISCARD_REFUSALS}[str(record["op"])]
                for value in _as_list(reason):
                    self.assertIn(value, pool, record["id"])
        for case in self.fixture["cases"]:
            checks = case.get("checks") or {}
            for key in ("unverified", "unverified_now"):
                for entry in checks.get(key) or []:
                    for value in _as_list(entry["reason"]):
                        self.assertIn(value, _UNVERIFIED_REASONS, case["id"])

    def test_the_scorer_reproduces_the_agent_precondition_chain(self) -> None:
        """Design 7.14 item 14: a closed gate never reaches the lesson lane."""
        source = _sealed_scorer_bytes().decode("utf-8")
        gate_at = source.index("gate = call_gate(")
        report_at = source.index("skill_report = call_skill_report(")
        match_at = source.index("rows = call_match_lessons(")
        cue_at = source.index("cue = call_cue(")
        self.assertLess(gate_at, report_at)
        self.assertLess(report_at, match_at)
        self.assertLess(match_at, cue_at)
        self.assertIn("if gate.get(\"allowed\"):", source)
        self.assertIn("withheld_candidates=withheld", source)
        self.assertNotIn("sweep=", source)
        for case in self.fixture["cases"]:
            if case.get("expect_gate_allowed") is False:
                self.assertEqual(case["expect_lesson_mode"], "idle",
                                 case["id"])
                self.assertEqual(case["expect_skill_mode"], "gate-closed",
                                 case["id"])
                self.assertEqual(case["expect_lessons"], [], case["id"])

    def test_the_leak_material_is_the_model_facing_block_only(self) -> None:
        """Ruling 34 P1, pinned as behaviour and not as prose."""
        source = _sealed_scorer_bytes().decode("utf-8")
        self.assertIn(
            'outcome["leakage"] += self._leakage(\n'
            '            _texts(rows) + "\\n" + _texts(documents))', source)
        self.assertNotIn("json.dumps(lesson_report", source)
        self.assertNotIn("json.dumps(skill_report", source)
        self.assertNotIn("_texts(skill_report", source)
        self.assertIn("_check_no_code_in_store", source)
        covered = {str(case["store"]) for case in self.fixture["cases"]
                   if "no_code_in_store" in (case.get("checks") or {})}
        self.assertGreaterEqual(len(covered), 1)

    def test_the_v2_scorer_defects_stay_fixed(self) -> None:
        """Ruling 30 (c) and (d), pinned as behaviour and not as prose."""
        self.assertEqual(_as_list(None), [None])
        self.assertEqual(_as_list("proof_stale"), ["proof_stale"])
        self.assertEqual(_as_list(["a", "b"]), ["a", "b"])
        source = _sealed_scorer_bytes().decode("utf-8")
        self.assertIn("return path.read_bytes() if path.is_file() else None",
                      source)
        self.assertIn("workspace / LEARNED_DIRECTORY / name", source)
        self.assertIn("self.live_before_approve[record_id]", source)
        rollbacks = [case for case in self.fixture["cases"]
                     if (case.get("checks") or {}).get("rollback_bytes")]
        self.assertTrue(rollbacks)
        self.assertTrue(any(
            (case["checks"]["rollback_bytes"].get("expect") == "removed")
            for case in rollbacks))
        self.assertTrue(any(
            (case["checks"]["rollback_bytes"].get("expect") == "restored")
            for case in rollbacks))
        proofs = [case for case in self.fixture["cases"]
                  if (case.get("checks") or {}).get("proof")]
        self.assertTrue(proofs)

    def test_a_multi_row_bucket_is_matched_row_by_row(self) -> None:
        """One project per planted state means one name, several reasons."""
        observed = [("learned-code-fix", "digest_mismatch"),
                    ("learned-code-fix", "live_document_missing"),
                    ("learned-code-fix", "proof_stale"),
                    ("learned-code-fix", "screened_component")]
        wanted = [{"skill": "learned-code-fix", "reason": "digest_mismatch"},
                  {"skill": "learned-code-fix",
                   "reason": "live_document_missing"},
                  {"skill": "learned-code-fix", "reason": "proof_stale"},
                  {"skill": "learned-code-fix",
                   "reason": ["proof_stale", "screened_component"]}]
        self.assertTrue(_reasons_match(observed, wanted))
        self.assertFalse(_reasons_match(observed[:3], wanted))
        self.assertFalse(_reasons_match(
            observed, [{"skill": "learned-code-fix",
                        "reason": "digest_mismatch"}]))
        self.assertTrue(_reasons_match([], []))

    def test_the_gated_and_reported_split_is_visible(self) -> None:
        cases = self.fixture["cases"]
        gated = [case for case in cases if case["mode_gated"]]
        reported = [case for case in cases if not case["mode_gated"]]
        self.assertGreaterEqual(len(gated), 40)
        self.assertTrue(reported)
        for kind in ("gate_closed", "inject_hit", "inject_miss",
                     "out_of_project", "none_eligible", "pool_overflow",
                     "legacy_live", "staged_invisible"):
            self.assertTrue(any(case["mode_gated"] and case["kind"] == kind
                                for case in cases), kind)
        for kind in ("unknown_identity", "superseded_lesson",
                     "contradicted_lesson", "cross_family", "expired_lesson"):
            self.assertTrue(all(not case["mode_gated"]
                                for case in cases if case["kind"] == kind),
                            kind)
        self.assertTrue(any(case["expect_cue"] for case in cases))
        self.assertTrue(any(not case["expect_cue"] for case in cases))
        self.assertGreaterEqual(
            sum(1 for case in cases if case["skill_mode_gated"]), 40)

    def test_the_orphan_seam_is_covered_three_ways(self) -> None:
        """Rulings 34 P2 and 35: healthy, deferred, and never-touched.

        A live document whose approving row was deleted must be parked on a
        healthy store with its receipt landed and nothing outstanding; parked
        with a DEFERRED receipt on a store whose head will not verify, and
        still reported through a fresh instance; and a document with no
        ``ladder.*`` event must never be parked at all.
        """
        by_store: dict[str, list[dict[str, Any]]] = {}
        for case in self.fixture["cases"]:
            if case["kind"] != "unverified_promotion":
                continue
            by_store.setdefault(str(case["store"]), []).append(case)
        healthy = by_store.get("lck-orphan-live") or []
        held = by_store.get("lck-orphan-held") or []
        untouched = by_store.get("lck-untouched") or []
        for group in (healthy, held, untouched):
            self.assertGreaterEqual(len(group), 3)
            self.assertTrue(any(case.get("fresh_store") for case in group))
        for store_id in ("lck-orphan-live", "lck-orphan-held"):
            store = [item for item in self.fixture["stores"]
                     if item["id"] == store_id][0]
            targets = [record["target"] for record in store["records"]
                       if record["op"] == "tamper"]
            self.assertIn("delete_promotion_row", targets, store_id)
        held_store = [item for item in self.fixture["stores"]
                      if item["id"] == "lck-orphan-held"][0]
        self.assertIn("corrupt_spine_head",
                      [record["target"] for record in held_store["records"]
                       if record["op"] == "tamper"])
        untouched_store = [item for item in self.fixture["stores"]
                           if item["id"] == "lck-untouched"][0]
        self.assertEqual([record["op"] for record in
                          untouched_store["records"]
                          if record["op"] == "grandfather"], [])
        for case in healthy:
            checks = case["checks"]
            self.assertEqual(checks["live_files"], [])
            self.assertEqual(checks["parked_files"], ["learned-code-fix"])
            self.assertEqual(checks["pending_withdrawals"], [])
            self.assertGreaterEqual(
                int(checks["withdrawn_receipts"]["minimum"]), 1)
            self.assertIn("catalog_absent", checks)
        for case in held:
            checks = case["checks"]
            self.assertEqual(checks["live_files"], [])
            self.assertEqual(checks["parked_files"], ["learned-code-build"])
            self.assertTrue(checks["pending_details"][0]["deferred"])
            self.assertEqual(int(checks["withdrawn_receipts"]["maximum"]), 0)
        for case in untouched:
            checks = case["checks"]
            self.assertEqual(checks["live_files"],
                             ["learned-security-analysis"])
            self.assertEqual(checks["parked_files"], [])
            self.assertEqual(checks["catalog_present"],
                             ["learned-security-analysis"])
            self.assertEqual(checks["pending_withdrawals"], [])
            self.assertIsNone(case["expect_skill"])

    def test_a_planted_state_is_read_before_its_project_is_swept(self) -> None:
        """Item 32(c): the sweep is per PROJECT, so ordering is load-bearing.

        A case that must observe a planted unverified state has to be the
        FIRST case of its project, because the first read on a project
        withdraws every unverified artefact in it and a normally receipted
        withdrawal does not stay visible.
        """
        seen: set[tuple[str, int]] = set()
        for case in self.fixture["cases"]:
            key = (str(case["store"]), int(case["project"]))
            wants_planted = any(
                str(entry.get("reason")) not in ("None", "lineage_broken")
                for entry in (case.get("checks") or {}).get(
                    "unverified_now") or [])
            if wants_planted:
                self.assertNotIn(key, seen, case["id"])
            if case.get("question"):
                seen.add(key)
        withdrawn = [case for case in self.fixture["cases"]
                     if case["expect_skill_mode"] == "unverified-withdrawn"
                     and case["skill_mode_gated"]]
        self.assertGreaterEqual(len(withdrawn), 3)
        for case in withdrawn:
            self.assertIsNone(case["expect_skill"], case["id"])

    def test_every_store_pins_its_unverified_bucket(self) -> None:
        """Design 3.7 consumer 3: emptiness after every scripted state."""
        covered = {str(case["store"]) for case in self.fixture["cases"]
                   if "unverified" in (case.get("checks") or {})}
        self.assertEqual(covered,
                         {str(store["id"])
                          for store in self.fixture["stores"]})

    def test_nothing_references_the_row_a_tamper_deletes(self) -> None:
        """Ruling 31, the other half: never delete a referenced row."""
        try:
            _ladder, _module, _library, memory_class = _ladder_modules()
        except Exception as error:                       # noqa: BLE001
            self.skipTest(str(error))
        with tempfile.TemporaryDirectory(
                prefix="jarvis-ladder-holdout-v7-schema-") as root:
            memory = memory_class(Path(root) / "schema.db")
            try:
                self.assertEqual(
                    int(memory.db.execute(
                        "PRAGMA foreign_keys").fetchone()[0]), 1)
                tables = [str(row[0]) for row in memory.db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")]
                referring = []
                for table in tables:
                    for row in memory.db.execute(
                            'PRAGMA foreign_key_list("{0}")'.format(table)):
                        if str(row[2]) == "ladder_promotions":
                            referring.append(table)
                self.assertEqual(referring, [])
            finally:
                memory.close()

    def test_every_store_seeds_through_the_sealed_path(self) -> None:
        """Ruling 31, the rule holdout v3 broke, made a test.

        Every store is seeded by the SEALED ``_Replay``, in fixture order,
        with every tamper, on a fresh temporary workspace, with foreign keys
        on -- and each store's ``expected_counts`` is asserted, along with
        every record-level expectation the script carries.  A fixture whose
        seeding raises, or whose counts do not follow from the script, is a
        fixture defect and is caught here rather than by spending the
        holdout's one run.
        """
        from datetime import datetime, timezone

        try:
            ladder, memory_module, skill_library, memory_class = \
                _ladder_modules()
        except Exception as error:                       # noqa: BLE001
            self.skipTest(str(error))
        fixture = self.fixture
        anchor = datetime.now(timezone.utc)
        before = {path.name for path in REPOSITORY_ROOT.iterdir()}
        with tempfile.TemporaryDirectory(
                prefix="jarvis-ladder-holdout-v7-seed-") as root:
            root_path = Path(root)
            for store in fixture["stores"]:
                store_root = root_path / str(store["id"])
                store_root.mkdir(parents=True, exist_ok=True)
                memory = memory_class(store_root / "holdout.db")
                try:
                    foreign_keys = memory.db.execute(
                        "PRAGMA foreign_keys").fetchone()[0]
                    self.assertEqual(int(foreign_keys), 1, store["id"])
                    replay = _seed_store(store, fixture, store_root,
                                         memory_module, memory, ladder,
                                         skill_library, anchor)
                    observed = _observed_counts(memory, replay)
                    self.assertEqual(
                        observed,
                        {key: store["expected_counts"][key]
                         for key in _STORE_COUNT_KEYS},
                        store["id"])
                    wrong = [item for item in replay.script_checks
                             if not item["pass"]]
                    self.assertEqual(wrong, [], store["id"])
                    applied = {str(record["target"])
                               for record in store["records"]
                               if record["op"] == "tamper"}
                    self.assertLessEqual(applied, _TAMPER_TARGETS,
                                         store["id"])
                finally:
                    try:
                        memory.close()
                    except Exception:                    # noqa: BLE001
                        pass
                    ladder.clear_catalog_cache()
        after = {path.name for path in REPOSITORY_ROOT.iterdir()}
        self.assertEqual(after - before, set(),
                         "seeding wrote outside its temporary directory")
        planted = {str(record["target"]) for store in fixture["stores"]
                   for record in store["records"]
                   if record["op"] == "tamper"}
        self.assertEqual(planted, _TAMPER_TARGETS)


class SealedLadderHoldoutProductionTests(unittest.TestCase):
    """The sealed gate.  Scored once, by the boss, with the token supplied."""

    def test_sealed_learning_ladder_holdout_v7(self) -> None:
        fixture = _load_fixture()
        if _seal_is_placeholder() or _pin_is_placeholder(fixture):
            self.skipTest(
                "the runtime pin or the fixture and scorer digests are still "
                "placeholders: the boss reseals the four pinned files with "
                "claude-reseal-runtime-pins.py before this holdout is scored")
        if os.environ.get(TOKEN_ENVIRONMENT_VARIABLE) != _required_run_token():
            self.skipTest(
                "sealed ladder holdout v7 run token was not supplied")

        from datetime import datetime, timezone

        try:
            ladder, memory_module, skill_library, memory_class = \
                _ladder_modules()
        except Exception as error:                       # noqa: BLE001
            self.skipTest(str(error))
        pin = _runtime_pin_now()
        self.assertEqual(
            pin, {name: fixture["runtime_sha256"][name]
                  for name in PINNED_FILES},
            "runtime pin mismatch")

        before = {path.name for path in REPOSITORY_ROOT.iterdir()}
        anchor = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory(
                prefix="jarvis-ladder-holdout-v7-") as root:
            root_path = Path(root)

            def factory(store_id: str) -> Any:
                return memory_class(
                    root_path / "holdout-{0}.db".format(store_id))

            report = _evaluate_holdout(memory_module, factory, fixture,
                                       root_path, ladder, skill_library,
                                       anchor)
        after = {path.name for path in REPOSITORY_ROOT.iterdir()}
        self.assertEqual(after - before, set(),
                         "the holdout wrote outside its temporary directory")
        self.assertFalse((REPOSITORY_ROOT / ".jarvis-skills").exists())
        self.assertFalse((REPOSITORY_ROOT / ".jarvis-skills-staging").exists())

        print(json.dumps({key: report[key] for key in report if key != "all"},
                         sort_keys=True, indent=2, default=str))
        thresholds = fixture["thresholds"]
        aggregate = report["aggregate"]
        for entry in report["seeding"]:
            self.assertIsNone(entry["error"], entry["store"])
            self.assertEqual(entry["observed"], entry["expected"],
                             entry["store"])

        # Every denominator this report divides by is COUNTED from the cases
        # that actually ran, and each is asserted against the number the
        # fixture says it must be.  A case that errors, is filtered out or
        # never reaches its metric shrinks a denominator and fails here,
        # rather than letting a ratio read 1.0 over an empty set.  v3 was
        # spent by a seeding failure that a shrinking denominator would have
        # hidden.
        self.assertEqual(report["scored_kinds"], fixture["expected_counts"]
                         ["kinds"])
        self.assertEqual(report["denominators"],
                         fixture["expected_counts"]["gates"])
        # ``returned_rows`` is the one denominator the fixture cannot predict
        # -- it counts what the ranker chose to return on the un-gated cases
        # -- so precision is guarded by a floor instead: a precision of 1.0
        # over zero returned rows would mean nothing at all.
        self.assertGreaterEqual(
            aggregate["returned_rows"],
            fixture["expected_counts"]["minimum_returned_rows"])
        self.assertEqual(report["script_checks"],
                         fixture["expected_counts"]["script_checks"])

        self.assertEqual(report["script_accuracy"], 1.0)
        self.assertEqual(aggregate["errors"], 0)
        self.assertEqual(aggregate["forbidden_hits"], 0)
        self.assertEqual(aggregate["leakage"], thresholds["leakage"])
        self.assertEqual(aggregate["staged_visibility"],
                         thresholds["staged_visibility"])
        self.assertEqual(aggregate["unverified_served"],
                         thresholds["unverified_promotions"])
        self.assertGreaterEqual(aggregate["injection_precision"],
                                thresholds["injection_precision"])
        self.assertGreaterEqual(aggregate["injection_recall"],
                                thresholds["injection_recall"])
        self.assertEqual(aggregate["empty_recall_accuracy"], 1.0)
        self.assertEqual(aggregate["lesson_mode_accuracy"],
                         thresholds["mode_accuracy"])
        self.assertEqual(aggregate["skill_mode_accuracy"],
                         thresholds["mode_accuracy"])
        self.assertEqual(aggregate["cue_accuracy"], thresholds["cue_accuracy"])
        self.assertEqual(aggregate["gate_accuracy"], 1.0)
        self.assertEqual(aggregate["closure_accuracy"], 1.0)
        self.assertEqual(aggregate["skill_accuracy"], 1.0)
        self.assertEqual(aggregate["op_accuracy"], 1.0)
        self.assertEqual(aggregate["rollback_exactness"],
                         thresholds["rollback_exactness"])
        # the ladder verdicts, both ladder readers, rollback exactness, staged
        # invisibility, the proof reasons and the ledger's own verification
        self.assertEqual(aggregate["check_accuracy"],
                         thresholds["monotonicity_accuracy"])
        self.assertLessEqual(report["read_latency"]["p95_ms"],
                             thresholds["read_p95_ms"])
        self.assertLessEqual(report["write_latency"]["max_ms"],
                             thresholds["write_max_ms"])


if __name__ == "__main__":
    unittest.main()
