"""M5 half A store-integration exit tests: typed-invariant compaction at
schema 50, its migration, its erase transitivity, and the two phase gates.

Design of record: ``VTMF_M5_COMPACTION_BENCHMARKS_DESIGN.md`` revision 3.  The
E-numbers in the test names are its section 2.14 exit tests; the store owner
(design section 6, owner B) owns E-2, E-3, E-8, E-12, E-13, E-14, E-18, E-20,
E-21 and E-22, and builds E-1 here as well because E-1 and E-2 together are the
phase gate.  ``tests/test_memory_compaction.py`` holds compaction-core's own
module-level suite; this file only ever drives the store through
``jarvis.memory``'s public methods.

Four rules from the M4 build record section 4 bind every assertion below:

1. **Publish the measurement, not the conclusion.**  Where two owners share a
   contract, this file measures its own side and prints the number.  The spine
   payload key set is read back off a real appended event rather than compared
   with compaction-core's constant, because three M4 seam defects were green
   under tests that were correct about one side of a two-owner contract.
2. **A status is what was observed, never what an absence implies.**  No test
   concludes "clean" from "nothing raised".
3. **Assert both directions.**  Every equivalence check is paired with a
   deliberate perturbation that must break it.
4. **An assertion that cannot fail is not evidence.**  In particular the
   interleaving the N-1 gate depends on is itself asserted, because a fixture
   that wrote each conversation to completion in turn would let the missing
   ``conversation_id`` predicate ship straight through E-1.

Seeding is through public writers (``new_conversation``, ``add_message``,
``remember_explicit_project_claim``, ``record_fact_proposal``, ``erase_*``)
except in ``_bulk_seed_messages``, which is used only by the 50,000-row scale
fixture and says at its own site why and what it is equivalent to.
"""
from __future__ import annotations

import inspect
import json
import os
import shutil
import sqlite3
import tempfile
import time
import unittest
import zlib
from pathlib import Path
from typing import Any
from unittest.mock import patch

from jarvis import long_horizon, memory_spine
from jarvis.memory import (
    Memory,
    SCHEMA_VERSION,
    _sqlite_table_exists,
    now_iso,
)
from tests.legacy_store_fixture import strip_spine

try:  # pragma: no cover - import guard, exercised only before the M5 build
    from jarvis import memory_compaction
except ImportError:  # pragma: no cover
    memory_compaction = None  # type: ignore[assignment]

if SCHEMA_VERSION >= 50 and memory_compaction is None:  # pragma: no cover
    # Loud, never a skip: schema 50 without the module is a broken tree, not an
    # unbuilt one, and a skip here would hide the phase gate entirely.
    raise AssertionError(
        "SCHEMA_VERSION is 50 but jarvis.memory_compaction is missing"
    )

# The skip is SPLIT deliberately, and the split is the lesson of the
# 2026-09-04 rename incident rather than a convenience.
#
# A module-level ``raise unittest.SkipTest`` made every test here inert until
# ``SCHEMA_VERSION`` reached 50 -- which is precisely the window in which the
# two-owner contract churns hardest and in which a rename in
# ``memory_compaction`` costs the most to discover late.  A tripwire that
# cannot run during the build is not a tripwire.
#
# So: anything that needs a schema-50 STORE is gated on ``COMPACTION_BUILT``,
# and anything that only reads compaction-core's published CONTRACT runs the
# moment ``jarvis.memory_compaction`` is importable -- today, on every full
# suite run, before GO.
COMPACTION_MODULE_PRESENT = memory_compaction is not None
COMPACTION_BUILT = COMPACTION_MODULE_PRESENT and SCHEMA_VERSION >= 50

requires_compaction_module = unittest.skipUnless(
    COMPACTION_MODULE_PRESENT, "jarvis.memory_compaction is not in this tree"
)
requires_schema_50 = unittest.skipUnless(
    COMPACTION_BUILT, "M5 half A (schema 50 compaction) is not built yet"
)


# The wall-clock gates of design 2.6 are enforced only under
# ``JARVIS_ENFORCE_TIMING_GATES=1`` -- the M3/M4 rule.  Every other run
# measures the same figures, prints them and passes, so the suite never turns
# machine contention into a red test and never silently stops measuring.
ENFORCE_TIMING_GATES = os.environ.get("JARVIS_ENFORCE_TIMING_GATES") == "1"

# Design 2.6 budgets, in milliseconds.
READ_BUDGET_MS = 10.0
WRITE_BUDGET_MS = 2000.0

# The real schema-49 store E-12 needs, as a frozen SQL dump captured once from
# the M4 tree.  E-12 used to build it by `git archive`-ing that commit, which
# meant it SKIPPED on every ordinary run (the env var naming the commit is
# unset) and would have failed outright once the branch squashed the commit out
# of existence, on a shallow clone, or in a source tarball with no .git.
# See the dump header for the capture recipe and why it restores in two passes.
LEGACY_DUMP = Path(__file__).resolve().parent / "fixtures" / "m5_legacy_store_schema49.sql"
LEGACY_PASS_SPLIT = (
    "-- ==== PASS 2: virtual tables, their rebuild, and their triggers ===="
)
LEGACY_KEY_HEX = "5a" * 32

# The exact governed command ``record_fact_proposal`` accepts; free text raises
# ``ValueError`` because ``parse_explicit_project_fact`` refuses it
# (memory.py:10318, design 4.6 item 4).
PROPOSAL_COMMAND = (
    'Remember this project fact: {"subject":"Millrace weir",'
    '"predicate":"gate count","value":"four"}'
)

# Design 2.8's documented payload key sets, spelled here so a drift on
# compaction-core's side is caught from this side too.  The tests compare the
# module constant against these AND compare a real appended event's payload
# against the module constant, so neither side can move alone.
DOCUMENTED_COMPACTED_REQUIRED_KEYS = frozenset({
    "seq", "handle", "span_sha256", "span_unkeyed_sha256", "summary_sha256",
    "invariants_sha256", "key_fingerprint", "first_message_id",
    "last_message_id", "message_count", "source_chars", "stored_bytes",
    "summary_chars", "author", "screened", "event_range",
})
DOCUMENTED_COMPACTED_OPTIONAL_KEYS = frozenset({
    "at", "model", "reduction_ratio", "excluded_by_screen", "span_has_proposal",
})
DOCUMENTED_REHYDRATION_CODES = (
    "malformed_handle", "unknown_handle", "key_mismatch", "digest_mismatch",
    "erased", "store_unavailable",
)
DOCUMENTED_READ_MODES = frozenset({
    "complete", "none", "partial", "budget-exceeded", "project-unavailable",
    "error",
})

# The ten tables that carry a ``memory_id`` column on a live store, minus
# ``memory_claims``.  N-5: neither M5 table carries that column, so the count
# stays ten and the derived list must not grow.  Copied from
# ``tests/test_memory_graph_integration.py`` deliberately: if M5 changed it,
# both files would have to move, which is the point.
DOCUMENTED_MEMORY_DEPENDENT_TABLE_COUNT = 10


@requires_compaction_module
class CompactionModuleContractTests(unittest.TestCase):
    """Compaction-core's published contract, checked WITHOUT a schema-50 store.

    These run before GO, which is the whole point of them.  Everything here
    reads ``jarvis.memory_compaction``'s exported names and constants, so it is
    live from the moment core's file exists -- unlike the store tests below,
    which cannot run until ``SCHEMA_VERSION`` reaches 50.

    Recorded because it was paid for: on 2026-09-04 core renamed
    ``VERIFY_PROBLEM_KINDS`` to ``COMPACTION_PROBLEM_KINDS`` and
    ``MILESTONE_AUTHORS`` to ``COMPACTION_AUTHORS`` mid-build.  Two consumers
    had the old names written down, and it was caught by a third owner running
    ``hasattr`` rather than trusting a message.  Had these tests been gated on
    schema 50, the suite would have been green throughout and the break would
    have surfaced at GO, inside the migration work, with three owners moving.
    """

    def test_the_refusal_and_mode_spellings_do_not_collide(self) -> None:
        """Design 2.9 wrote the deadline as ``budget-exceeded`` on both paths;
        the tree spells every refusal snake_case and every read mode hyphenated
        (``project-unavailable``).  Core split them, and both spellings are now
        load-bearing in different places, so a test pins each to its own set."""
        self.assertEqual(memory_compaction.READ_MODE_BUDGET_EXCEEDED,
                         "budget-exceeded")
        self.assertEqual(memory_compaction.REFUSAL_BUDGET_EXCEEDED,
                         "budget_exceeded")
        self.assertIn(memory_compaction.READ_MODE_BUDGET_EXCEEDED,
                      memory_compaction.READ_MODES)
        self.assertIn(memory_compaction.REFUSAL_BUDGET_EXCEEDED,
                      memory_compaction.COMPACTION_REFUSAL_CODES)
        self.assertEqual(set(memory_compaction.READ_MODES),
                         set(DOCUMENTED_READ_MODES))
        self.assertEqual(set(memory_compaction.REHYDRATION_CODES),
                         set(DOCUMENTED_REHYDRATION_CODES))
        self.assertEqual(len(memory_compaction.REHYDRATION_CODES), 6,
                         "M-15 closed the set at six; scope_denied must not "
                         "come back as a cross-project existence oracle")
        # ``key_unavailable`` is a WRITE refusal and never a verify result: per
        # N-4 a missing sidecar cannot be observed from a running store at all,
        # because ``Memory.__init__`` refuses first, and the only reachable
        # state is a swapped sidecar, reported as ``key_mismatch``.
        self.assertIn("key_unavailable",
                      memory_compaction.COMPACTION_REFUSAL_CODES)
        self.assertIn("key_mismatch", memory_compaction.COMPACTION_PROBLEM_KINDS)
        self.assertNotIn("key_unavailable",
                         memory_compaction.COMPACTION_PROBLEM_KINDS)

    def test_every_compaction_name_this_file_depends_on_exists(self) -> None:
        """A rename must fail here, once and readably.

        On 2026-09-04 core renamed ``VERIFY_PROBLEM_KINDS`` to
        ``COMPACTION_PROBLEM_KINDS`` mid-build.  Two owners had already written
        the old name from a measurement taken forty minutes earlier, and it was
        caught only because a third owner executed ``hasattr`` instead of
        trusting the message -- the M4 seam defect exactly, and the second time
        this contract has produced one.  Attribute access alone would surface a
        rename as a scattering of ``AttributeError``s inside unrelated tests;
        this turns it into one failure naming every missing name at once.
        ``__all__`` membership is asserted too, because a name that exists but
        is not exported is a name core has not promised to keep.
        """
        depended_on = (
            "COMPACTION_TABLES", "COMPACTION_PROBLEM_KINDS",
            "COMPACTION_REFUSAL_CODES", "REHYDRATION_CODES", "READ_MODES",
            "READ_MODE_BUDGET_EXCEEDED", "REFUSAL_BUDGET_EXCEEDED",
            "BUSY_REASONS", "COMPACTION_AUTHORS", "COMPACTION_SPINE_KIND",
            "COMPACTION_SCHEMA_VERSION", "COMPACTION_SPINE_SCHEMA_VERSION",
            "COMPACTED_HISTORY_LIMIT", "HANDLE_PATTERN",
            "MILESTONE_TOMBSTONE_MAX_IDS", "MAX_SPAN_MESSAGES",
            "DEFAULT_KEEP_TURNS", "DEFAULT_MIN_SPAN_CHARS",
            "DEFAULT_MAX_SPAN_CHARS", "DEFAULT_SUMMARY_CHARS",
            "DEFAULT_READ_BUDGET_MS", "DEFAULT_WRITE_BUDGET_MS",
            "SPINE_EVENT_COLUMNS", "SUMMARY_WITHHELD",
            "RehydrationError", "CompactionError",
            "compaction_ready", "compaction_downgrade_blocked",
            "compaction_downgrade_message", "migrate_compaction_v50",
            "drop_compaction_tables", "next_seq", "keep_boundary",
            "partition_spans", "plan_spans", "canonical_span", "span_digests",
            "handle_for", "parse_handle", "build_invariants",
            "event_watermark", "spine_event_rows", "runtime_summary",
            "build_compacted_span", "rehydrate_span", "verify_compaction",
            "rebuild_milestones",
        )
        missing = [name for name in depended_on
                   if not hasattr(memory_compaction, name)]
        self.assertEqual(missing, [], "renamed or removed by compaction-core")
        exported = set(getattr(memory_compaction, "__all__", ()))
        self.assertEqual(
            [name for name in depended_on if name not in exported], [],
            "depended on but not in memory_compaction.__all__",
        )

    def test_the_spine_event_column_order_is_pinned(self) -> None:
        """``spine_event_rows`` accepts a plain tuple in this exact order, so
        the order is part of the contract and not an implementation detail.

        Core found this the honest way: their own ``rebuild_milestones``
        queries ran against a connection with no ``row_factory``, and the
        reader indexed by column NAME, so every rebuild raised ``TypeError``.
        ``Memory`` sets ``sqlite3.Row``, so this store would never have hit it
        -- which is exactly why the store side must pin the order rather than
        rely on its own row factory hiding the difference.  The write path
        builds its SELECT from this constant instead of spelling the columns.
        """
        self.assertEqual(
            tuple(memory_compaction.SPINE_EVENT_COLUMNS),
            ("id", "created_at", "kind", "outcome", "conversation_id",
             "subject_kind", "subject_id", "payload_json"),
        )
        # ``actor`` is deliberately absent: the derivation does not read it,
        # and selecting a column nothing consumes invites a reader to start.
        self.assertNotIn("actor", memory_compaction.SPINE_EVENT_COLUMNS)

    def test_rebuild_milestones_offers_three_states_not_two(self) -> None:
        """Core's ``spine_ok`` is deliberately tri-state, and that matters.

        A hardwired boolean would force every caller to assert something about
        the chain; ``None`` lets a caller that genuinely has not checked say so
        rather than guess.  Pinned here because it is pure and therefore
        checkable before GO, and because collapsing it back to two states later
        would silently turn "not checked" into "checked and fine" at every call
        site that omitted the argument.
        """
        parameters = inspect.signature(
            memory_compaction.rebuild_milestones
        ).parameters
        self.assertIn("spine_ok", parameters)
        self.assertIsNone(parameters["spine_ok"].default)
        self.assertEqual(parameters["spine_ok"].kind,
                         inspect.Parameter.KEYWORD_ONLY)
        self.assertIn("spine_unverified",
                      memory_compaction.COMPACTION_REFUSAL_CODES)

    def test_the_two_tri_states_refuse_in_deliberately_opposite_orders(
        self,
    ) -> None:
        """Both siblings carry ``spine_ok``, and they order the refusal
        OPPOSITELY on purpose.  Pinned live, because an inconsistency that is
        deliberate looks exactly like one that is accidental.

        ``rebuild_milestones``: a caller-supplied ``spine_ok=False`` OVERRIDES
        a real ``schema_too_old``, because the harm there is emitting an
        equivalence *number* over a chain that does not verify -- so it refuses
        outright and produces no number at all.

        ``verify_compaction``: the real ``schema_too_old`` SURVIVES, because
        the harm there is a green *tick*, and an operator whose check stopped
        for a schema reason must be told the reason that actually stopped it
        rather than one the caller supplied.

        Measured, not assumed: an earlier measurement of mine had
        ``verify_compaction`` returning ``spine_unverified`` here, and core
        changed the ordering afterwards.  This test exists so the next change
        is a failure rather than a surprise.
        """
        for function in (memory_compaction.verify_compaction,
                         memory_compaction.rebuild_milestones):
            parameters = inspect.signature(function).parameters
            self.assertIn("spine_ok", parameters, function.__name__)
            self.assertIsNone(parameters["spine_ok"].default)
            self.assertEqual(parameters["spine_ok"].kind,
                             inspect.Parameter.KEYWORD_ONLY)

        blank = sqlite3.connect(":memory:")
        blank.row_factory = sqlite3.Row
        try:
            for state, expected in ((None, None), (True, True), (False, False)):
                verified = memory_compaction.verify_compaction(
                    blank, b"k" * 32, spine_ok=state)
                rebuilt = memory_compaction.rebuild_milestones(
                    blank, b"k" * 32, spine_ok=state)
                self.assertIs(verified["chain_verified"], expected)
                self.assertIs(rebuilt["chain_verified"], expected)
                # The real reason always wins on the read path...
                self.assertEqual(verified["refusal"], "schema_too_old", state)
            # ...and only the rebuild path lets the caller's answer override it.
            self.assertEqual(
                memory_compaction.rebuild_milestones(
                    blank, b"k" * 32, spine_ok=False)["refusal"],
                "spine_unverified",
            )
            for state in (None, True):
                self.assertEqual(
                    memory_compaction.rebuild_milestones(
                        blank, b"k" * 32, spine_ok=state)["refusal"],
                    "schema_too_old", state,
                )
        finally:
            blank.close()

    def test_include_derived_is_opt_in_and_absent_when_nothing_was_compared(
        self,
    ) -> None:
        """E-2 needs the per-milestone blocks; everyone else needs counts.

        Pure, so it runs before GO.  The third state is the one worth pinning:
        under ``spine_ok=False`` the result carries **no** ``derived`` key at
        all rather than an empty map, because the function refused before it
        compared anything -- "I compared nothing" and "I compared and found
        nothing" must not render alike.  An empty map on a schema-refused store
        is the opposite case and is correct: it got as far as looking.
        """
        parameters = inspect.signature(
            memory_compaction.rebuild_milestones
        ).parameters
        self.assertIs(parameters["include_derived"].default, False)
        self.assertEqual(parameters["include_derived"].kind,
                         inspect.Parameter.KEYWORD_ONLY)
        blank = sqlite3.connect(":memory:")
        blank.row_factory = sqlite3.Row
        try:
            self.assertNotIn(
                "derived",
                memory_compaction.rebuild_milestones(blank, b"k" * 32),
            )
            self.assertEqual(
                memory_compaction.rebuild_milestones(
                    blank, b"k" * 32, include_derived=True)["derived"], {},
            )
            self.assertNotIn(
                "derived",
                memory_compaction.rebuild_milestones(
                    blank, b"k" * 32, include_derived=True, spine_ok=False),
            )
        finally:
            blank.close()

    def test_the_m5_insertion_did_not_steal_m3s_memo_decorator(self) -> None:
        """Correctness review HIGH-1, pinned so it cannot happen twice.

        The M5 method block was inserted with an anchor of
        ``@_with_read_snapshot
    def graph_chains(``, which sits BETWEEN
        ``@_with_recall_cache`` and its target.  The whole block landed there,
        so ``_compaction_message_rows`` wore M3's per-store memo and channel 3
        lost it.  Nothing in the tree failed: the behaviour is memo-identical
        and only latency moves, which is exactly why it needs an assertion
        rather than a test.

        The cost is deliberately NOT quantified here.  The reviewer declined to
        quote one because their benchmark had channel 3 abstaining ``no-start``
        and returning zero rows in every column, so no honest figure exists
        yet; inventing one would be worse than recording the structural fact.

        compaction-surface hit the same anchor shape twice in their own codemod
        and fixed it by anchoring on ``^(@|class |def )``.  Theirs broke
        loudly; a lost memo decorator does not.
        """
        def memoised(function: Any) -> bool:
            # ``_with_recall_cache``'s wrapper is the one that references
            # ``activate``; ``_with_read_snapshot``'s references ``rollback``.
            # Both use functools.wraps, so ``__wrapped__`` alone cannot tell
            # them apart and the chain has to be walked.
            while hasattr(function, "__wrapped__"):
                if "activate" in function.__code__.co_names:
                    return True
                function = function.__wrapped__
            return False

        self.assertTrue(
            memoised(Memory.graph_chains),
            "graph_chains lost @_with_recall_cache -- check whether something "
            "was inserted between the decorator and the function",
        )
        # The control: the helper that wrongly wore it must not, or the check
        # above would pass on a tree where BOTH carry it.
        self.assertFalse(memoised(Memory._compaction_message_rows))
        # And the detector itself discriminates, measured on a function that
        # genuinely has the memo and one that genuinely does not.
        self.assertTrue(memoised(Memory.current_claims))
        self.assertFalse(memoised(Memory.conversation_milestones))

    def test_the_downgrade_message_is_the_literal_the_docs_quote(self) -> None:
        """Boss ruling F-3, checkable before GO because the function is pure.

        ``docs/COMPACTION.md`` quotes this rather than paraphrasing it, so the
        doc and the refusal cannot drift.  A refusal naming a command the
        operator does not have would be worse than one naming none, which is
        why ``repair-schema`` must be absent until M5.1 ships it.
        """
        message = memory_compaction.compaction_downgrade_message(17, version=49)
        self.assertIn("compaction_downgrade_refused", message)
        self.assertIn("17", message)
        self.assertIn("49", message)
        self.assertIn("PRAGMA user_version = 50", message)
        self.assertIn("docs/COMPACTION.md", message)
        self.assertNotIn("repair-schema", message)
        # The console on this host is cp1252, so the message must survive it.
        message.encode("ascii")
        # The schema number is interpolated, never typed, so a renumber cannot
        # leave the message lying about which version restores the store.
        self.assertIn(str(memory_compaction.COMPACTION_SCHEMA_VERSION), message)

    def test_half_a_ships_with_no_configuration_surface(self) -> None:
        """Boss ruling on F-5.  Design 2.6's nine ``JARVIS_COMPACTION_*`` env
        keys do not exist: the tunables are module constants and there is no
        ``.env.example`` entry and no ``_DOTENV_KEYS`` obligation.  Asserted
        rather than trusted, because F-1 was the same class of mistake in the
        other direction -- a key in ``.env.example`` that ``_DOTENV_KEYS`` did
        not know broke every command on a first run.
        """
        from jarvis import config as jarvis_config

        leaked = sorted(
            name for name in getattr(jarvis_config, "_DOTENV_KEYS", ())
            if name.startswith("JARVIS_COMPACTION")
        )
        self.assertEqual(leaked, [])
        example = Path(__file__).resolve().parents[1] / ".env.example"
        if example.exists():
            self.assertNotIn("JARVIS_COMPACTION",
                             example.read_text(encoding="utf-8"))
        # The defaults the keyword arguments fall back to are module constants,
        # and ``compact_conversation(...)`` with no overrides must use them.
        # Names measured off the landed module, not guessed.
        self.assertFalse(hasattr(memory_compaction, "COMPACTION_CONFIG_KEYS"))
        for name, expected in (
            ("DEFAULT_KEEP_TURNS", 12),
            ("DEFAULT_MIN_SPAN_CHARS", 12000),
            ("DEFAULT_MAX_SPAN_CHARS", 200000),
            ("MAX_SPAN_MESSAGES", 400),
            ("DEFAULT_SUMMARY_CHARS", 1200),
            ("DEFAULT_READ_BUDGET_MS", 10),
            ("DEFAULT_WRITE_BUDGET_MS", 2000),
        ):
            self.assertEqual(getattr(memory_compaction, name), expected, name)



@requires_schema_50
class CompactionStoreCase(unittest.TestCase):
    """A temp store, an interleaved transcript, and the sidecar cleanup rule.

    Test cleanup removes ``<db>.memory-spine.key``: a leaked sidecar makes the
    next case in the same directory open a store under a foreign key, which is
    the one reachable shape of ``key_mismatch`` and would look like a product
    defect (design 2.9, N-4).
    """

    conversations_count = 8
    turns_per_conversation = 12

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="m5-store-")
        self.data_dir = Path(self.temp.name) / "data"
        self.data_dir.mkdir()
        self.db_path = self.data_dir / "jarvis.db"
        self.memory = Memory(self.db_path)
        self.addCleanup(self._cleanup)

    def _cleanup(self) -> None:
        try:
            self.memory.close()
        except Exception:
            pass
        sidecar = Path(str(self.db_path) + memory_spine.KEY_SIDECAR_SUFFIX)
        if sidecar.exists():
            sidecar.unlink()
        self.temp.cleanup()

    # --- fixtures ---------------------------------------------------------

    def seed_interleaved(
        self,
        *,
        conversations: int | None = None,
        turns: int | None = None,
        project_id: int = 1,
    ) -> list[int]:
        """Write ``conversations`` transcripts ROUND-ROBIN, one turn each.

        Round-robin is load-bearing, not stylistic: ``messages.id`` is a single
        global sequence (memory.py:1723), so a generator that wrote each
        conversation to completion in turn would produce contiguous per-
        conversation id blocks and could not detect a range read or a delete
        that forgot its ``conversation_id`` predicate (N-1).
        ``assert_ids_interleave`` proves the fixture actually did it.
        """
        count = self.conversations_count if conversations is None else conversations
        turn_count = self.turns_per_conversation if turns is None else turns
        ids = [
            self.memory.new_conversation(f"conversation {index}",
                                         project_id=project_id)
            for index in range(count)
        ]
        for turn in range(turn_count):
            for position, conversation in enumerate(ids):
                # Long enough that a span clears the REAL
                # ``DEFAULT_MIN_SPAN_CHARS`` of 12,000.  Lowering the threshold
                # in the fixture instead would mean the gate tests never
                # exercise the eligibility rule the product actually applies.
                self.memory.add_message(
                    conversation, "user",
                    f"Turn {turn} in conversation {position}: what is the "
                    f"listen port of the kestrel relay after the rebind? "
                    + ("The operator walked through the relay change and the "
                       "rebind and read relay-notes.md aloud. " * 4),
                )
                self.memory.add_message(
                    conversation, "assistant",
                    f"Reply {turn} in conversation {position}: the relay was "
                    f"rebound and relay-notes.md records the change. "
                    + ("The listen port moved and the host binding followed "
                       "it, as the notes record. " * 4),
                )
        return ids

    def seed_more(self, conversations: list[int], turns: int) -> None:
        """Append turns to conversations that already exist.

        Needed because a claim only lands INSIDE a compacted span's event
        range if events were appended between message batches: the watermark
        is the newest event at or before the sub-region's LAST message (N-3),
        so a claim made after all the seeding belongs to history that is still
        live and is correctly excluded.  Tests that need a milestone to name a
        claim key therefore seed, claim, and seed again.
        """
        for turn in range(turns):
            for position, conversation in enumerate(conversations):
                self.memory.add_message(
                    conversation, "user",
                    f"Later turn {turn} in {position}: the kestrel relay and "
                    f"the millrace weir both changed. "
                    + ("The operator recorded the change in relay-notes.md "
                       "and confirmed the rebind. " * 4),
                )
                self.memory.add_message(
                    conversation, "assistant",
                    f"Later reply {turn} in {position}: noted and recorded. "
                    + ("The listen port and the gate count are both on "
                       "record now. " * 4),
                )

    def _bulk_seed_messages(
        self, conversations: list[int], turns: int, *, stamp: str | None = None
    ) -> int:
        """Seed the 50,000-row scale fixture with one transaction of raw INSERTs.

        Public-writer seeding is the rule everywhere else in this file, and
        ``add_message`` is what the product actually calls.  It is not usable at
        this size: it opens one ``BEGIN IMMEDIATE`` per row under
        ``synchronous=FULL``, measured at 3.7 ms/row on this host, so 50,000
        rows cost ~185 s of fixture time against 1.3 s for the batch below.
        The rows are equivalent because ``add_message``'s only transformations
        are the role check, ``redact_secrets`` and the 100,000-character clip
        (memory.py:10274-10281), and this seeder emits short, secret-free,
        already-valid ``user``/``assistant`` rows.  ``messages`` carries no
        lineage trigger, unlike ``memories``, so a raw insert here is honest;
        ``seed_legacy_memory_row`` exists for the table where it is not.
        """
        moment = stamp or now_iso()
        rows = []
        for turn in range(turns):
            for conversation in conversations:
                rows.append((conversation, moment, "user",
                             f"scale question {turn} for {conversation}"))
                rows.append((conversation, moment, "assistant",
                             f"scale reply {turn} for {conversation}"))
        with self.memory._immediate_transaction():
            self.memory.db.executemany(
                "INSERT INTO messages(conversation_id, created_at, role, content)"
                " VALUES (?, ?, ?, ?)",
                rows,
            )
        return len(rows)

    def snapshot_messages(self) -> dict[int, list[tuple[Any, ...]]]:
        """Every transcript row, grouped by conversation, in id order."""
        grouped: dict[int, list[tuple[Any, ...]]] = {}
        for row in self.memory.db.execute(
            "SELECT id, conversation_id, created_at, role, content "
            "FROM messages ORDER BY id"
        ):
            grouped.setdefault(int(row["conversation_id"]), []).append(
                (int(row["id"]), str(row["created_at"]), str(row["role"]),
                 str(row["content"]))
            )
        return grouped

    # --- shared assertions ------------------------------------------------

    def assert_ids_interleave(self, conversations: list[int]) -> None:
        """The fixture's ids really do interleave; otherwise E-1/E-18/E-22 are
        vacuous.  Rule 4: an assertion that cannot fail is not evidence, and
        this is the assertion that keeps the others able to fail."""
        order = [
            int(row[0]) for row in self.memory.db.execute(
                "SELECT conversation_id FROM messages ORDER BY id"
            )
        ]
        switches = sum(
            1 for left, right in zip(order, order[1:]) if left != right
        )
        self.assertGreater(
            switches, len(conversations),
            "the transcript fixture is not interleaved; a per-conversation "
            "range delete could not be shown to be scoped",
        )
        for conversation in conversations:
            bounds = self.memory.db.execute(
                "SELECT MIN(id), MAX(id) FROM messages WHERE conversation_id=?",
                (conversation,),
            ).fetchone()
            if bounds[0] is None:
                continue
            inside = int(self.memory.db.execute(
                "SELECT COUNT(*) FROM messages WHERE id BETWEEN ? AND ?",
                (bounds[0], bounds[1]),
            ).fetchone()[0])
            own = int(self.memory.db.execute(
                "SELECT COUNT(*) FROM messages WHERE conversation_id=? "
                "AND id BETWEEN ? AND ?",
                (conversation, bounds[0], bounds[1]),
            ).fetchone()[0])
            self.assertGreater(
                inside, own,
                f"conversation {conversation}'s id range holds no foreign rows",
            )

    def _last_payload(self, kind: str) -> dict[str, Any]:
        row = self.memory.db.execute(
            "SELECT payload_json FROM memory_spine_events WHERE kind=? "
            "ORDER BY id DESC LIMIT 1", (kind,)
        ).fetchone()
        self.assertIsNotNone(row, f"no {kind} event was appended")
        return json.loads(str(row[0]))

    def _message_stamp(self, message_id: int) -> str:
        row = self.memory.db.execute(
            "SELECT created_at FROM messages WHERE id=?", (int(message_id),)
        ).fetchone()
        return "" if row is None else str(row[0])

    def assert_compaction_clean(self) -> None:
        """``verify_compaction`` is only a health statement when someone
        checked the chain the records live on.

        ``ok`` alone is the read-path twin of the ``rebuild_milestones`` gap:
        the check confirms each receipt is present and that recorded digests
        match, but a forged chain leaves every one of those facts true, so a
        bare ``ok`` renders a clean compaction line in ``doctor`` over a spine
        that does not verify.  Every site in this file goes through here so the
        qualifier cannot be dropped one assertion at a time.
        """
        health = self.memory.verify_compaction()
        self.assertTrue(health["checked"], health.get("refusal"))
        self.assertIs(health["chain_verified"], True)
        self.assertTrue(health["ok"], health.get("problems"))
        self.assertEqual(health["problems"], [])

    def assert_spine_and_graph_clean(self) -> None:
        spine = self.memory.verify_spine()
        self.assertTrue(spine["ok"], spine.get("problems"))
        graph = self.memory.verify_graph()
        self.assertTrue(graph["ok"], graph.get("problems"))

    def milestones(self, conversation_id: int | None = None) -> list[sqlite3.Row]:
        if conversation_id is None:
            return list(self.memory.db.execute(
                "SELECT * FROM memory_milestones ORDER BY id"
            ))
        return list(self.memory.db.execute(
            "SELECT * FROM memory_milestones WHERE conversation_id=? ORDER BY seq",
            (int(conversation_id),),
        ))

    def compact_all(self, conversations: list[int], **kwargs: Any) -> list[dict]:
        """Plan then apply one pass per conversation, through the plan token."""
        applied = []
        for conversation in conversations:
            plan = self.memory.compact_conversation(conversation, **kwargs)
            if not plan.get("spans"):
                continue
            result = self.memory.compact_conversation(
                conversation, apply=True, plan_token=plan["plan_token"], **kwargs
            )
            self.assertTrue(result.get("applied"), result)
            applied.append(result)
        return applied


class CompactAndRehydrateEquivalenceTests(CompactionStoreCase):
    """E-1, the first phase gate, on an interleaved store (N-1)."""

    conversations_count = 8
    turns_per_conversation = 250  # 8 x 250 x 2 = 4,000 rows, design 2.14 E-1

    def test_e1_every_span_rehydrates_byte_exact_and_the_transcript_reassembles(
        self,
    ) -> None:
        conversations = self.seed_interleaved()
        self.assert_ids_interleave(conversations)
        before = self.snapshot_messages()
        total_before = sum(len(rows) for rows in before.values())
        self.assertGreaterEqual(total_before, 4000)

        applied = self.compact_all(conversations)
        self.assertTrue(applied, "nothing was compacted; the gate is vacuous")

        rebuilt: dict[int, list[tuple[Any, ...]]] = {}
        for row in self.milestones():
            span = self.memory.rehydrate(str(row["handle"]))
            self.assertEqual(int(span["conversation_id"]),
                             int(row["conversation_id"]))
            self.assertEqual(len(span["messages"]), int(row["message_count"]))
            rebuilt.setdefault(int(row["conversation_id"]), []).extend(
                (int(item["id"]), str(item["created_at"]), str(item["role"]),
                 str(item["content"]))
                for item in span["messages"]
            )
        live = self.snapshot_messages()
        for conversation, rows in before.items():
            spliced = sorted(
                rebuilt.get(conversation, []) + live.get(conversation, []),
                key=lambda item: item[0],
            )
            self.assertEqual(
                spliced, rows,
                f"conversation {conversation} did not reassemble byte-exact",
            )

        # Every conversation that was NOT compacted still holds every row.
        compacted = {int(row["conversation_id"]) for row in self.milestones()}
        for conversation, rows in before.items():
            if conversation in compacted:
                continue
            self.assertEqual(live.get(conversation), rows)

        self.assert_spine_and_graph_clean()

    def test_e1_a_second_pass_is_a_no_op_that_appends_no_event(self) -> None:
        conversations = self.seed_interleaved(conversations=4, turns=60)
        self.compact_all(conversations)
        events_before = self._event_count()
        milestones_before = [tuple(row) for row in self.milestones()]
        for conversation in conversations:
            plan = self.memory.compact_conversation(conversation)
            self.assertEqual(plan.get("spans") or [], [], plan)
        self.assertEqual(self._event_count(), events_before)
        self.assertEqual([tuple(row) for row in self.milestones()],
                         milestones_before)

    def test_e1_corrupting_one_body_byte_fails_closed(self) -> None:
        """Both directions (rule 3): the same store that reassembles byte-exact
        must return NOTHING once a single byte moves."""
        conversations = self.seed_interleaved(conversations=2, turns=60)
        self.compact_all(conversations)
        row = self.milestones()[0]
        handle = str(row["handle"])
        self.assertTrue(self.memory.rehydrate(handle)["messages"])
        body = self.memory.db.execute(
            "SELECT body FROM memory_compacted_spans WHERE handle=?", (handle,)
        ).fetchone()[0]
        raw = bytearray(zlib.decompress(body))
        raw[len(raw) // 2] ^= 0x01
        # The immutability trigger forbids UPDATE, so tamper the way an
        # attacker would have to: outside the product, on a closed store.
        self.memory.close()
        tamper = sqlite3.connect(str(self.db_path))
        try:
            tamper.execute("DROP TRIGGER memory_compacted_spans_immutable")
            tamper.execute(
                "UPDATE memory_compacted_spans SET body=? WHERE handle=?",
                (zlib.compress(bytes(raw), 6), handle),
            )
            tamper.commit()
        finally:
            tamper.close()
        self.memory = Memory(self.db_path)
        with self.assertRaises(memory_compaction.RehydrationError) as caught:
            self.memory.rehydrate(handle)
        self.assertEqual(caught.exception.code, "digest_mismatch")

    def _event_count(self) -> int:
        return int(self.memory.db.execute(
            "SELECT COUNT(*) FROM memory_spine_events"
        ).fetchone()[0])


class DeleteAndRebuildEquivalenceTests(CompactionStoreCase):
    """E-2, the second phase gate: ``derived`` is spine-replayable, ``observed``
    is reported and never compared (H-4)."""

    conversations_count = 4
    turns_per_conversation = 60

    def setUp(self) -> None:
        super().setUp()
        self.conversations = self.seed_interleaved(turns=8)
        # The governed facts go BETWEEN message batches.  A claim made after
        # all the seeding is newer than the span's last message, so under N-3
        # it belongs to history that is still live and is correctly excluded --
        # which would leave every ``derived`` block empty and make E-2 compare
        # nothing against nothing.
        self._seed_governed_facts(self.conversations[0])
        self.seed_more(self.conversations, turns=52)
        self.compact_all(self.conversations)

    def _seed_governed_facts(self, conversation: int) -> None:
        for subject, predicate, value in (
            ("Kestrel relay", "maintainer", "Dana Okonkwo"),
            ("Kestrel relay", "listen port", "8443"),
            ("Millrace weir", "gate count", "four"),
        ):
            self.memory.remember_explicit_project_claim(
                conversation, 1,
                'Remember this project fact: '
                + json.dumps({"subject": subject, "predicate": predicate,
                              "value": value}),
            )

    def test_e2_rebuild_milestones_reproduces_derived_byte_for_byte(self) -> None:
        stored = {
            int(row["id"]): json.loads(str(row["invariants_json"]))["derived"]
            for row in self.milestones()
        }
        self.assertTrue(stored, "no milestone to rebuild; the gate is vacuous")
        report = self.memory.rebuild_milestones(include_derived=True)
        self.assertTrue(report["ok"], report.get("divergences"))
        self.assertEqual(report["divergences"], [])
        self.assertEqual(report["rebuild_equivalence_derived"], 1.0)
        # ``1.0`` on its own says only "the milestones agree with the spine as
        # stored" -- on a forged chain the rebuild faithfully reproduces the
        # forged value and still reports zero divergences.  The number is an
        # equivalence statement only when someone checked the chain, so the
        # gate asserts the qualifier and not just the metric (core's gap, found
        # by the perturbation test below).
        self.assertIs(report["chain_verified"], True)
        # Boss ruling 11.18: two sources, one comparison, done by the layer
        # allowed to have an opinion.  Core derives the rebuilt block from the
        # spine and publishes its digest; the wrapper fetches the stored rows
        # itself and judges.  An equality computed inside one call that reads
        # BOTH sides cannot fail, and a gate that cannot fail is not a gate --
        # a defect populating the stored side from the rebuilt value would
        # make this pass unconditionally.
        self.assertEqual(set(report["derived"]), set(stored))
        self.assertEqual(report["derived_skipped"], [])
        for milestone_id, derived in stored.items():
            block = report["derived"][milestone_id]
            self.assertEqual(
                set(block),
                {"milestone_id", "rebuilt_sha256", "stored", "rebuilt"},
            )
            # The block states what was derived and never whether it matched:
            # no verdict field may appear here, or the judgement moves back
            # into the layer that is not allowed to make it.
            for verdict in ("matched", "equivalent", "ok", "divergent"):
                self.assertNotIn(verdict, block)
            # This test digests the row it read off the table and compares it
            # with core's published digest -- the same comparison the wrapper
            # makes, from the same independent side.
            self.assertEqual(
                memory_compaction.derived_digest(derived),
                str(block["rebuilt_sha256"]),
                f"milestone {milestone_id} did not replay byte-for-byte",
            )

    def test_e2_the_equivalence_can_fail(self) -> None:
        """Rule 3.  Perturb one stored ``derived`` block out of band; the
        rebuild must report the divergence rather than 1.0."""
        row = self.milestones()[0]
        payload = json.loads(str(row["invariants_json"]))
        payload["derived"]["claims_created"] = [999999]
        self.memory.close()
        tamper = sqlite3.connect(str(self.db_path))
        try:
            tamper.execute("DROP TRIGGER memory_milestones_immutable")
            tamper.execute(
                "UPDATE memory_milestones SET invariants_json=? WHERE id=?",
                (memory_spine.canonical(payload), int(row["id"])),
            )
            tamper.commit()
        finally:
            tamper.close()
        self.memory = Memory(self.db_path)
        report = self.memory.rebuild_milestones()
        self.assertNotEqual(report["rebuild_equivalence_derived"], 1.0)
        self.assertFalse(report["ok"])
        # The wrapper's own verdict, not core's dry-run counters: under 11.18
        # ``equivalent`` / ``divergent`` / ``divergences`` are core's CLI
        # surface and are explicitly not the gate.
        self.assertIn(int(row["id"]), report["equivalence_mismatched"])

    def test_e2_an_echoing_wrapper_would_get_a_different_number(self) -> None:
        """Boss ruling 11.18: the gate must not be computed from core's blocks.

        The earlier version of this test forged ``stored`` and ``rebuilt`` to
        the SAME value and asserted the number did not move.  That was a
        presence check wearing a discrimination's docstring: the wrapper reads
        exactly one field out of core's block (``rebuilt_sha256``, measured --
        ``stored`` appears zero times in it), so forging ``stored`` could not
        move the number under any implementation, and forging both to one
        value would have looked identical to an ECHOING wrapper that compared
        core's two halves against each other.

        Forging them to DIFFERENT values is what separates the two:

          forge              independent wrapper   echoing wrapper
          stored == rebuilt  1.0                   1.0   (indistinguishable)
          stored != rebuilt  1.0                   < 1.0 (discriminates)

        The real independence proof is
        ``test_e2_the_equivalence_can_fail``, which perturbs an
        ``invariants_json`` row out of band in SQLite and asserts the number
        moves; the control below, which forges the digest the wrapper does
        read, is the other half.  This test's job is narrower and worth
        stating exactly: an implementation that judged by comparing core's
        ``stored`` against core's ``rebuilt`` would fail here and pass
        everything else.
        """
        honest = self.memory.rebuild_milestones(include_derived=True)
        self.assertEqual(honest["rebuild_equivalence_derived"], 1.0)
        self.assertTrue(honest["ok"])

        real_rebuild = memory_compaction.rebuild_milestones

        def forged(db, key, **kwargs):
            report = real_rebuild(db, key, **kwargs)
            for block in (report.get("derived") or {}).values():
                # Deliberately DIFFERENT: an echoing wrapper comparing these
                # two against each other now disagrees with an independent one.
                block["stored"] = {"forged": "stored side"}
                block["rebuilt"] = {"forged": "rebuilt side"}
            return report

        with patch.object(memory_compaction, "rebuild_milestones", forged):
            tampered = self.memory.rebuild_milestones(include_derived=True)
        self.assertEqual(
            tampered["rebuild_equivalence_derived"],
            honest["rebuild_equivalence_derived"],
            "forging core's diagnostic blocks to DIFFERENT values moved the "
            "gate number, so the gate is comparing core's two halves against "
            "each other rather than the stored rows against core's digest",
        )
        self.assertTrue(tampered["ok"])
        self.assertEqual(tampered["equivalence_mismatched"], [])
        # And the control: forging the DIGEST -- the value the gate actually
        # reads -- must move it, or the comparison is inert in both
        # directions rather than one.
        def forged_digest(db, key, **kwargs):
            report = real_rebuild(db, key, **kwargs)
            for block in (report.get("derived") or {}).values():
                block["rebuilt_sha256"] = "0" * 64
            return report

        with patch.object(memory_compaction, "rebuild_milestones", forged_digest):
            broken = self.memory.rebuild_milestones()
        self.assertEqual(broken["rebuild_equivalence_derived"], 0.0)
        self.assertFalse(broken["ok"])

    def test_e2_a_partial_derivation_is_never_a_ratio(self) -> None:
        """Both directions on ``derived_skipped``.

        A ratio over a partial set is the same failure as a ratio over an
        empty one, one row later: the number looks like evidence and is
        computed over whatever happened to be available.  So a non-empty
        ``derived_skipped`` yields ``None`` with a reason, never a figure --
        and the honest path still yields a figure, or the refusal would be
        unfalsifiable.
        """
        honest = self.memory.rebuild_milestones()
        self.assertEqual(honest["rebuild_equivalence_derived"], 1.0)
        self.assertIsNone(honest["equivalence_reason"])

        real_rebuild = memory_compaction.rebuild_milestones

        def skipping(db, key, **kwargs):
            report = real_rebuild(db, key, **kwargs)
            derived = report.get("derived") or {}
            if derived:
                victim = sorted(derived)[0]
                report["derived_skipped"] = [victim]
                derived.pop(victim)
            return report

        with patch.object(memory_compaction, "rebuild_milestones", skipping):
            partial = self.memory.rebuild_milestones()
        self.assertIsNone(partial["rebuild_equivalence_derived"])
        self.assertEqual(partial["equivalence_reason"], "partial_derivation")
        self.assertFalse(partial["ok"])

    def test_e2_an_empty_derivation_is_never_a_flattering_one(self) -> None:
        """The empty case, which is the one that would read as perfect."""
        real_rebuild = memory_compaction.rebuild_milestones

        def emptied(db, key, **kwargs):
            report = real_rebuild(db, key, **kwargs)
            report["derived"] = {}
            report["derived_skipped"] = []
            return report

        with patch.object(memory_compaction, "rebuild_milestones", emptied):
            empty = self.memory.rebuild_milestones()
        self.assertIsNone(empty["rebuild_equivalence_derived"])
        self.assertEqual(empty["equivalence_reason"], "nothing_derived")
        self.assertFalse(empty["ok"])

    def test_e2_a_perturbed_spine_cannot_yield_a_confident_one_point_zero(
        self,
    ) -> None:
        """The other half of rule 3, on the side that actually feeds the
        derivation.

        ``test_e2_the_equivalence_can_fail`` perturbs the STORED ``derived``
        block, which proves the rebuild is not echoing what it is meant to
        check.  It does not prove the rebuild reads the spine correctly.  Core
        measured the trap: moving an event's ``subject_id`` changes nothing,
        because ``build_invariants`` reads ``payload["claim_id"]`` first and
        only falls back to ``subject_id`` -- so a perturbation test aimed at
        ``subject_id`` passes while proving nothing.  This moves the payload's
        ``claim_id``, which is the field the derivation actually consumes.

        Tampering with a payload breaks the keyed chain, so there are two
        acceptable outcomes and one forbidden one.  Acceptable: the rebuild
        refuses (``spine_unverified``), or it reports a divergence.  Forbidden:
        a confident ``rebuild_equivalence_derived == 1.0`` over a spine that no
        longer verifies.  Which branch fires is printed rather than asserted,
        because the branch is core's to choose and the guarantee is mine.
        """
        row = None
        derived = None
        for candidate in self.milestones():
            block = json.loads(str(candidate["invariants_json"]))["derived"]
            if block.get("claim_keys"):
                row, derived = candidate, block
                break
        self.assertIsNotNone(
            row, "no milestone carried a claim key; the fixture puts no event "
                 "inside a compacted span and this test would prove nothing",
        )
        # ``claim_key`` is the field ``_collect_claim_key`` reads to build
        # ``derived["claim_keys"]``, so moving it is guaranteed to change what
        # a correct re-derivation produces.  Core warned that aiming at
        # ``subject_id`` instead can prove nothing, because the derivation
        # prefers a payload id where one exists; this aims at a value the
        # derivation is measured to consume.
        event = self.memory.db.execute(
            """SELECT id, payload_json FROM memory_spine_events
               WHERE conversation_id=? AND id > ? AND id <= ?
                 AND kind='claim.created' AND payload_json IS NOT NULL
               ORDER BY id LIMIT 1""",
            (int(row["conversation_id"]),
             derived["event_range"]["after"], derived["event_range"]["through"]),
        ).fetchone()
        self.assertIsNotNone(event, "no claim event inside the recorded range")
        payload = json.loads(str(event["payload_json"]))
        self.assertIn("claim_key", payload)
        payload["claim_key"] = "0" * 64

        self.memory.close()
        tamper = sqlite3.connect(str(self.db_path))
        try:
            tamper.execute("DROP TRIGGER memory_spine_events_redaction_only")
            tamper.execute(
                "UPDATE memory_spine_events SET payload_json=? WHERE id=?",
                (memory_spine.canonical(payload), int(event["id"])),
            )
            tamper.commit()
        finally:
            tamper.close()
        self.memory = Memory(self.db_path)

        # The store wrapper runs ``verify_spine`` itself and passes the
        # answer down, so after this tamper the branch is determinate rather
        # than emergent and the test pins it.
        self.assertFalse(self.memory.verify_spine()["ok"])
        report = self.memory.rebuild_milestones()
        self.assertEqual(report["refusal"], "spine_unverified")
        self.assertIs(report["chain_verified"], False)
        self.assertTrue(report["refusal_detail"])
        # No equivalence number is produced AT ALL over a chain known not to
        # verify -- which is the number E-2 must never be able to emit.
        self.assertFalse(report["ok"])
        self.assertEqual(report["divergences"], [])
        self.assertNotEqual(report.get("rebuild_equivalence_derived"), 1.0)

    def test_e2_observed_is_reported_and_never_compared(self) -> None:
        """H-4: ``observed`` is reported, never gated.

        This used to prove it by writing a different ``observed`` block into
        the row out of band and asserting the equivalence still read 1.0.  That
        method stopped being available once the gate started consulting
        ``verify_compaction`` (red team H-1): an out-of-band edit to a
        milestone IS a broken store, and the gate now correctly refuses to
        produce a number for one.  The test was measuring the right property
        through a mechanism that has since become indistinguishable from the
        thing the gate exists to catch.

        The property is proved directly instead, and more strongly: the digest
        the gate compares is taken over ``derived`` alone, so no value of
        ``observed`` can reach it.  Both directions -- changing ``observed``
        must not move the digest, and changing ``derived`` must.
        """
        row = self.milestones()[0]
        payload = json.loads(str(row["invariants_json"]))
        self.assertIn("observed", payload)
        baseline = memory_compaction.derived_digest(payload["derived"])

        for observed in (
            {"tools_used": ["read_file"], "files_touched": ["workspace/n.md"]},
            {"tools_used": [], "files_touched": []},
            {},
        ):
            payload["observed"] = observed
            self.assertEqual(
                memory_compaction.derived_digest(payload["derived"]), baseline,
                "an observed value reached the digest the gate compares",
            )

        # The control: the digest is not simply constant.
        moved = json.loads(str(row["invariants_json"]))["derived"]
        moved["outcome"] = "partial"
        self.assertNotEqual(memory_compaction.derived_digest(moved), baseline)

        # And the live gate still passes on the untouched store, so this test
        # cannot pass on a store where nothing works.
        report = self.memory.rebuild_milestones()
        self.assertEqual(report["rebuild_equivalence_derived"], 1.0)
        self.assertTrue(report["ok"])

    def test_e2_claim_and_graph_rebuilds_stay_equivalent_after_compaction(
        self,
    ) -> None:
        claims = self.memory.rebuild_claim_projection()
        self.assertTrue(claims["ok"], claims)
        graph = self.memory.rebuild_graph_projection()
        self.assertEqual(graph.get("divergences") or [], [])
        self.assert_spine_and_graph_clean()
        self.assert_compaction_clean()

    def test_e2_survives_erasing_one_conversation_and_one_claim_key(self) -> None:
        self.memory.delete_conversation(self.conversations[-1])
        self.memory.erase_explicit_project_claim(
            self.conversations[0], 1,
            'Erase this project fact: {"subject":"Millrace weir",'
            '"predicate":"gate count"}',
        )
        self.assert_spine_and_graph_clean()
        self.assert_compaction_clean()
        report = self.memory.rebuild_milestones()
        self.assertEqual(report["rebuild_equivalence_derived"], 1.0, report)
        self.assertIs(report["chain_verified"], True)
        claims = self.memory.rebuild_claim_projection()
        self.assertTrue(claims["ok"], claims)
        self.assertEqual(
            self.memory.rebuild_graph_projection().get("divergences") or [], []
        )

    def test_the_wrapper_never_leaves_the_chain_unclaimed(self) -> None:
        """The store-side half of core's three states.

        ``memory_compaction.rebuild_milestones`` defaults to
        ``spine_ok=None`` -> ``chain_verified None``, which is right for a pure
        function: it cannot know, so it declines to say.  ``Memory`` CAN know,
        because it owns ``verify_spine``, so the wrapper must never pass the
        question through.  A ``chain_verified: None`` reaching an operator
        surface is the absence-implies-status error one level up -- the reader
        sees ``1.0`` beside a blank qualifier and supplies the optimistic
        reading themselves.  Both directions: the module may abstain, the
        wrapper may not.
        """
        parameters = inspect.signature(
            memory_compaction.rebuild_milestones
        ).parameters
        self.assertIsNone(parameters["spine_ok"].default)
        module_level = memory_compaction.rebuild_milestones(
            self.memory.db, self.memory._spine_key
        )
        self.assertIsNone(module_level["chain_verified"])

        wrapped = self.memory.rebuild_milestones()
        self.assertIsNotNone(
            wrapped["chain_verified"],
            "Memory.rebuild_milestones passed the question through instead of "
            "answering it",
        )
        self.assertIsInstance(wrapped["chain_verified"], bool)


class EraseTellsTheOperatorTheTruthTests(CompactionStoreCase):
    """Red team H-3.  An erase receipt is a privacy decision's input.

    Two failures, and the second is worse.  An erase destroyed a 56-turn span
    -- the only copy of those turns -- and the operator sentence never
    mentioned it.  And ``transcript_copies`` counted ``messages`` and
    ``conversation_goals`` only, so the same operator was told "48 copies
    remain" while sixteen copies of the erased value sat inside another
    conversation's surviving span.  A number that omits an entire storage
    class is worse than no number, because it will be acted on.
    """

    VALUE = "quillfeather-escutcheon-9931"

    def setUp(self) -> None:
        super().setUp()
        self.first, self.second = self.seed_interleaved(
            conversations=2, turns=6
        )
        # The value appears in BOTH conversations' transcripts...
        for conversation in (self.first, self.second):
            for _ in range(3):
                self.memory.add_message(
                    conversation, "user",
                    f"the weir gate count is {self.VALUE} " * 12,
                )
                self.memory.add_message(
                    conversation, "assistant", f"noted {self.VALUE} " * 12
                )
        # ...but only the FIRST carries the governed claim, so only its
        # milestone names the key and only its span is destroyed.
        self.memory.remember_explicit_project_claim(
            self.first, 1,
            'Remember this project fact: {"subject":"Millrace weir",'
            f'"predicate":"gate count","value":"{self.VALUE}"}}',
        )
        self.seed_more([self.first, self.second], turns=50)
        self.compact_all([self.first, self.second])

    def test_a_destroyed_span_is_named_in_the_operator_sentence(self) -> None:
        naming = [
            row for row in self.milestones()
            if str(self.memory.db.execute(
                "SELECT claim_key FROM memory_claims ORDER BY id DESC LIMIT 1"
            ).fetchone()[0]) in set(
                json.loads(str(row["invariants_json"]))["derived"]["claim_keys"]
            )
        ]
        self.assertTrue(naming, "no span would be destroyed; test is vacuous")

        result = self.memory.erase_explicit_project_claim(
            self.first, 1,
            'Erase this project fact: {"subject":"Millrace weir",'
            '"predicate":"gate count"}',
        )
        sentence = str(result["assistant_message"])
        self.assertIn("compacted span", sentence, sentence)
        self.assertIn("cannot be rehydrated", sentence, sentence)
        self.assertIn(str(len(naming)), sentence, sentence)
        # No value is echoed, which the receipt has always been careful about.
        self.assertNotIn(self.VALUE, sentence)

    def test_surviving_spans_are_counted_among_the_copies_that_remain(
        self,
    ) -> None:
        surviving_before = int(self.memory.db.execute(
            "SELECT COUNT(*) FROM memory_compacted_spans").fetchone()[0])
        self.assertGreaterEqual(
            surviving_before, 2,
            "both conversations must have a span or this proves nothing",
        )
        result = self.memory.erase_explicit_project_claim(
            self.first, 1,
            'Erase this project fact: {"subject":"Millrace weir",'
            '"predicate":"gate count"}',
        )
        # The second conversation's span survives AND still holds the value.
        survivors = [
            row for row in self.memory.db.execute(
                "SELECT handle, body FROM memory_compacted_spans")
        ]
        self.assertTrue(survivors)
        holding = [
            row for row in survivors
            if self.VALUE in memory_compaction.decompress_span(row["body"])
        ]
        self.assertTrue(
            holding,
            "no surviving span holds the value; the count cannot be checked",
        )
        self.assertGreaterEqual(
            int(result["transcript_copies"]), len(holding),
            "the receipt's copy count omits the compacted spans that still "
            "hold the erased value",
        )

    def test_the_count_is_zero_when_no_span_holds_the_value(self) -> None:
        """The other direction: the new term must not inflate every erase."""
        other = self.memory.new_conversation("unrelated", project_id=1)
        for _ in range(40):
            self.memory.add_message(other, "user", "nothing to see here " * 20)
            self.memory.add_message(other, "assistant", "indeed " * 40)
        self.memory.remember_explicit_project_claim(
            other, 1,
            'Remember this project fact: {"subject":"Harrier box",'
            '"predicate":"datacenter","value":"Fenwick"}',
        )
        self.seed_more([other], turns=40)
        self.compact_all([other])
        counted = self.memory._compacted_span_copies_locked(["Fenwick"])
        self.assertEqual(
            self.memory._compacted_span_copies_locked(
                ["a-value-no-span-contains-31337"]
            ),
            0,
        )
        self.assertGreaterEqual(counted, 0)


class MilestoneScaleTests(CompactionStoreCase):
    """Red team M-6: at 3,011 milestones the read returned ZERO rows.

    Not slow -- empty.  The scan was unbounded, so the 10 ms deadline expired
    part-way and the caller got ``rows: []``, which reads as "this
    conversation has no history".  That is the silent scale cliff M1 shipped
    and a phase went into repairing: the store answering "nothing" when it
    means "too many".
    """

    def test_three_thousand_milestones_still_return_a_page(self) -> None:
        conversation = self.seed_interleaved(conversations=2, turns=60)[0]
        self.compact_all([conversation])
        template = self.milestones(conversation)[0]
        # Synthetic rows: the subject here is the SQL bound, not the content,
        # and building 3,000 real spans would cost minutes.  They are never
        # verified or rehydrated by this test.
        span_template = self.memory.db.execute(
            "SELECT body, body_chars FROM memory_compacted_spans WHERE handle=?",
            (str(template["handle"]),),
        ).fetchone()
        with self.memory._immediate_transaction():
            columns = [key for key in template.keys() if key != "id"]
            values = [template[key] for key in columns]
            seq_at = columns.index("seq")
            handle_at = columns.index("handle")
            for extra in range(3010):
                row = list(values)
                row[seq_at] = 1000 + extra
                handle = f"mem:span/{conversation}/{1000 + extra}/{extra:012x}"
                row[handle_at] = handle
                milestone_id = int(self.memory.db.execute(
                    f"INSERT INTO memory_milestones({', '.join(columns)}) "
                    f"VALUES ({', '.join('?' for _ in columns)})",
                    row,
                ).lastrowid)
                # Each synthetic milestone gets a span row too.  Without one
                # every row is skipped as ``partial`` and the page comes back
                # empty for a reason that has nothing to do with the scale
                # bound this test is about -- which is itself worth knowing:
                # milestones whose spans are gone consume the scan budget and
                # can starve a page, honestly reported as ``partial``.
                self.memory.db.execute(
                    """INSERT INTO memory_compacted_spans(handle, milestone_id,
                           conversation_id, body, body_chars)
                       VALUES (?, ?, ?, ?, ?)""",
                    (handle, milestone_id, conversation,
                     span_template["body"], int(span_template["body_chars"])),
                )
        total = int(self.memory.db.execute(
            "SELECT COUNT(*) FROM memory_milestones WHERE conversation_id=?",
            (conversation,)).fetchone()[0])
        self.assertGreaterEqual(total, 3011)

        started = time.perf_counter()
        result = self.memory.conversation_milestones(conversation, project_id=1)
        cold = (time.perf_counter() - started) * 1000.0
        warm = []
        for _ in range(9):
            began = time.perf_counter()
            self.memory.conversation_milestones(conversation, project_id=1)
            warm.append((time.perf_counter() - began) * 1000.0)
        warm.sort()
        elapsed = warm[len(warm) // 2]
        print(f"\n[M-6] conversation_milestones over {total} milestones: "
              f"cold {cold:.2f} ms, warm p50 {elapsed:.2f} ms, "
              f"warm max {warm[-1]:.2f} ms (budget {READ_BUDGET_MS} ms); "
              f"{len(result['rows'])} rows, mode {result['report']['mode']}")

        self.assertTrue(
            result["rows"],
            "the store answered 'nothing' when it meant 'too many'",
        )
        self.assertLessEqual(len(result["rows"]), 6)
        self.assertTrue(result["overflow"])
        self.assertIn(result["report"]["mode"], DOCUMENTED_READ_MODES)
        self.assertNotEqual(result["report"]["mode"], "none")
        if ENFORCE_TIMING_GATES:
            self.assertLessEqual(elapsed, READ_BUDGET_MS)

    def test_a_small_store_still_says_complete(self) -> None:
        """The other direction, so the fix is not "always report overflow"."""
        conversation = self.seed_interleaved(conversations=2, turns=60)[0]
        self.compact_all([conversation])
        result = self.memory.conversation_milestones(conversation, project_id=1)
        self.assertEqual(result["report"]["mode"], "complete")
        self.assertFalse(result["overflow"])


class WatermarkFailsClosedTests(CompactionStoreCase):
    """Red team M-7: an unreadable previous milestone used to yield 0.

    Zero is the most dangerous value available: the next milestone then claims
    every event from the start of the conversation, overlapping every range
    already written, and nothing downstream detects it because overlapping
    ranges still replay perfectly.
    """

    def test_an_unreadable_previous_milestone_refuses(self) -> None:
        conversations = self.seed_interleaved(conversations=2, turns=6)
        conversation = conversations[0]
        self.memory.remember_explicit_project_claim(
            conversation, 1,
            'Remember this project fact: {"subject":"Kestrel relay",'
            '"predicate":"maintainer","value":"Dana Okonkwo"}',
        )
        self.seed_more(conversations, turns=50)
        self.compact_all([conversation])
        first = self.milestones(conversation)[-1]
        self.assertGreater(
            int(json.loads(str(first["invariants_json"]))
                ["derived"]["event_range"]["through"]), 0,
            "the first milestone claimed no events; the overlap this guards "
            "against could not arise",
        )

        self.memory.close()
        tamper = sqlite3.connect(str(self.db_path))
        try:
            tamper.execute("DROP TRIGGER memory_milestones_immutable")
            tamper.execute(
                "UPDATE memory_milestones SET invariants_json='not json' "
                "WHERE id=?", (int(first["id"]),))
            tamper.commit()
        finally:
            tamper.close()
        self.memory = Memory(self.db_path)

        self.seed_more(conversations, turns=50)
        with self.assertRaises(memory_spine.SpineError) as caught:
            self.memory.compact_conversation(conversation, keep_turns=4)
        self.assertEqual(caught.exception.code, "watermark_unreadable")
        # And nothing was written on the way to refusing.
        self.assertEqual(
            int(self.memory.db.execute(
                "SELECT COUNT(*) FROM memory_milestones "
                "WHERE conversation_id=?", (conversation,)).fetchone()[0]),
            len(self.milestones(conversation)),
        )

    def test_no_previous_milestone_is_still_zero(self) -> None:
        """The other direction: absence of a milestone is not a failure, it is
        the defined start, and conflating the two would refuse every first
        compaction."""
        conversation = self.seed_interleaved(conversations=2, turns=60)[0]
        self.assertEqual(self.memory._previous_watermark(conversation), 0)
        plan = self.memory.compact_conversation(conversation, keep_turns=4)
        self.assertIsNone(plan["refusal"])
        self.assertTrue(plan["spans"])


class GateConsultsTheVerifierTests(CompactionStoreCase):
    """Red team H-1: the phase gate must not pass a store the verifier calls
    broken.

    ``rebuild_milestones`` answers one question -- do the recorded invariants
    replay from the spine -- and that question stays true when the DATA behind
    them is gone.  Deleting every span blob leaves each ``derived`` block
    replaying perfectly while the compacted transcript no longer exists.  So
    the gate consulted the wrong single source, and E-2 would have certified a
    restore that lost everything.

    Each scenario runs after a control on the same store, because "the number
    changed" is only evidence if it was the passing number to begin with.
    """

    conversations_count = 2
    turns_per_conversation = 60

    def setUp(self) -> None:
        super().setUp()
        self.conversations = self.seed_interleaved()
        self.compact_all(self.conversations)
        self.assertTrue(self.milestones())

    def _control(self) -> None:
        report = self.memory.rebuild_milestones()
        self.assertTrue(report["ok"], report)
        self.assertEqual(report["rebuild_equivalence_derived"], 1.0)
        self.assertTrue(report["verify_ok"])
        self.assertTrue(self.memory.verify_compaction()["ok"])

    def _assert_gate_refuses(self, scenario: str) -> None:
        verification = self.memory.verify_compaction()
        self.assertFalse(
            verification["ok"],
            f"{scenario}: the verifier did not call this store broken, so "
            "this scenario no longer tests what it was written for",
        )
        report = self.memory.rebuild_milestones()
        self.assertFalse(report["ok"], f"{scenario}: the gate passed")
        self.assertIsNone(
            report["rebuild_equivalence_derived"],
            f"{scenario}: a passing equivalence number was produced over a "
            "store the verifier calls broken",
        )
        self.assertEqual(report["equivalence_reason"], "store_unverified")
        self.assertFalse(report["verify_ok"])
        self.assertTrue(report["verify_problems"])

    def test_every_span_blob_deleted_is_not_a_pass(self) -> None:
        """The restore that lost the entire compacted transcript."""
        self._control()
        with self.memory._immediate_transaction():
            self.memory.db.execute("DELETE FROM memory_compacted_spans")
        self._assert_gate_refuses("every span blob deleted")

    def test_every_receipt_missing_is_not_a_pass(self) -> None:
        self._control()
        self.memory.close()
        tamper = sqlite3.connect(str(self.db_path))
        try:
            tamper.execute("DROP TRIGGER memory_milestones_immutable")
            tamper.execute("UPDATE memory_milestones SET spine_event_id=999999")
            tamper.commit()
        finally:
            tamper.close()
        self.memory = Memory(self.db_path)
        self._assert_gate_refuses("every receipt missing")

    def test_a_live_message_inside_a_compacted_range_is_not_a_pass(self) -> None:
        """A span and the live transcript disagreeing about who owns an id."""
        self._control()
        row = self.milestones()[0]
        with self.memory._immediate_transaction():
            self.memory.db.execute(
                """INSERT INTO messages(id, conversation_id, created_at, role,
                       content) VALUES (?, ?, ?, 'user', 'resurrected')""",
                (int(row["first_message_id"]), int(row["conversation_id"]),
                 now_iso()),
            )
        self._assert_gate_refuses("a live message inside a compacted range")

    def test_the_gate_still_passes_on_an_intact_store(self) -> None:
        """The other direction, so the fix cannot be "always refuse"."""
        self._control()
        self.seed_more(self.conversations, turns=4)
        self._control()


class WorkedExampleTests(CompactionStoreCase):
    """Design 2.13, executed: one pass over a conversation whose candidate
    region holds a live proposal writes TWO milestones with their own
    watermarks (N-3), and the interleaved neighbour is untouched (N-1)."""

    def test_the_worked_example_writes_two_sub_regions(self) -> None:
        first, neighbour = self.seed_interleaved(conversations=2, turns=2)[:2]
        held = self._seed_with_a_proposal_in_the_middle(first, neighbour)
        neighbour_before = self.snapshot_messages()[neighbour]

        plan = self.memory.compact_conversation(first, keep_turns=12)
        self.assertEqual(len(plan["spans"]), 2, plan)
        self.assertEqual(plan["held_back_messages"], [held])
        result = self.memory.compact_conversation(
            first, keep_turns=12, apply=True, plan_token=plan["plan_token"]
        )
        self.assertTrue(result["applied"], result)

        rows = self.milestones(first)
        self.assertEqual(len(rows), 2)
        low, high = rows
        self.assertEqual(int(low["seq"]) + 1, int(high["seq"]))
        self.assertLess(int(low["last_message_id"]), held)
        self.assertGreater(int(high["first_message_id"]), held)

        low_derived = json.loads(str(low["invariants_json"]))["derived"]
        high_derived = json.loads(str(high["invariants_json"]))["derived"]
        for derived in (low_derived, high_derived):
            span = derived["event_range"]
            self.assertGreater(span["through"], span["after"],
                               "a sub-region took an empty event range (N-3)")
            self.assertEqual(derived["span_has_proposal"], 1)
        self.assertEqual(low_derived["event_range"]["through"],
                         high_derived["event_range"]["after"],
                         "the two ranges are not gap-free and disjoint")

        # No milestone may claim an event created after its own last message.
        for row, derived in ((low, low_derived), (high, high_derived)):
            # The row is gone from ``messages`` -- compaction deleted it --
            # so its timestamp lives only inside the span.  Reading it back
            # through ``rehydrate`` is both the only honest source and a
            # second exercise of the byte-exact path.
            span = self.memory.rehydrate(str(row["handle"]))
            last_stamp = str(span["messages"][-1]["created_at"])
            self.assertEqual(int(span["messages"][-1]["id"]),
                             int(row["last_message_id"]))
            newest = self.memory.db.execute(
                "SELECT MAX(created_at) FROM memory_spine_events "
                "WHERE conversation_id=? AND id > ? AND id <= ?",
                (first, derived["event_range"]["after"],
                 derived["event_range"]["through"]),
            ).fetchone()[0]
            if newest is not None:
                self.assertLessEqual(str(newest), last_stamp)

        # The held message and the whole neighbour survive.
        self.assertEqual(
            int(self.memory.db.execute(
                "SELECT COUNT(*) FROM messages WHERE id=?", (held,)
            ).fetchone()[0]), 1)
        self.assertEqual(self.snapshot_messages()[neighbour], neighbour_before)

    def test_the_receipt_carries_only_the_documented_keys(self) -> None:
        """Measured off a real appended event (rule 1), not read off the
        constant: an appended payload is what ``validate_payload`` actually
        admitted."""
        first, _ = self.seed_interleaved(conversations=2, turns=2)[:2]
        self._seed_with_a_proposal_in_the_middle(first, _)
        plan = self.memory.compact_conversation(first, keep_turns=12)
        self.memory.compact_conversation(
            first, keep_turns=12, apply=True, plan_token=plan["plan_token"]
        )
        events = list(self.memory.db.execute(
            "SELECT id, kind, actor, subject_kind, subject_id, outcome, "
            "payload_json FROM memory_spine_events "
            "WHERE kind='transcript.compacted' ORDER BY id"
        ))
        self.assertEqual(len(events), len(self.milestones(first)))
        for event in events:
            payload = json.loads(str(event["payload_json"]))
            keys = set(payload)
            self.assertLessEqual(
                keys,
                DOCUMENTED_COMPACTED_REQUIRED_KEYS
                | DOCUMENTED_COMPACTED_OPTIONAL_KEYS,
                f"undocumented payload key(s): "
                f"{sorted(keys - DOCUMENTED_COMPACTED_REQUIRED_KEYS - DOCUMENTED_COMPACTED_OPTIONAL_KEYS)}",
            )
            self.assertLessEqual(DOCUMENTED_COMPACTED_REQUIRED_KEYS, keys)
            self.assertEqual(str(event["subject_kind"]), "conversation")
            self.assertEqual(int(event["subject_id"]), first)
            self.assertEqual(str(event["outcome"]), "applied")
            # Digest-only: no transcript content, no summary text.
            blob = memory_spine.canonical(payload)
            self.assertNotIn("kestrel relay", blob.casefold())
            self.assertNotIn("Earlier in this conversation", blob)

    def test_the_published_contract_matches_the_design(self) -> None:
        """The other direction of the same seam.  ``payload_keys`` is the
        tree's single published source of the names -- its own docstring
        records the 2026-09-04 debugging cycle where a reader spelled a key the
        validator did not have -- so the contract is read from there, never
        from a constant this file keeps."""
        required, allowed = memory_spine.payload_keys("transcript.compacted")
        self.assertEqual(set(required), set(DOCUMENTED_COMPACTED_REQUIRED_KEYS))
        self.assertEqual(
            set(allowed),
            set(DOCUMENTED_COMPACTED_REQUIRED_KEYS)
            | set(DOCUMENTED_COMPACTED_OPTIONAL_KEYS),
        )

    def test_conversation_deleted_is_no_longer_unconstrained(self) -> None:
        """M-9.  ``conversation.deleted`` sits in ``UNCONSTRAINED_PAYLOAD_KINDS``
        today (memory_spine.py:1344-1347), so the two counts M5 adds to its
        payload would be validated by nothing.  The fix is to remove it from
        that set AND add the branches -- both halves, or E-8's payload
        assertions pass on whatever the test itself wrote."""
        self.assertNotIn("conversation.deleted",
                         memory_spine.UNCONSTRAINED_PAYLOAD_KINDS)
        required, allowed = memory_spine.payload_keys("conversation.deleted")
        self.assertEqual(set(required), {"messages_removed"})
        self.assertEqual(
            set(allowed),
            {"messages_removed", "at", "milestones_removed", "spans_removed"},
        )

    def test_an_extra_or_missing_receipt_key_is_rejected(self) -> None:
        """Rule 3, on the validator itself: the contract must refuse in both
        directions, or 'digest-only, closed key set' is unenforced."""
        required, allowed = memory_spine.payload_keys("transcript.compacted")
        good = {name: 1 for name in required}
        good["event_range"] = {"after": 0, "through": 1}
        with self.assertRaises(memory_spine.SpineError):
            memory_spine.validate_payload(
                "transcript.compacted", {**good, "not_a_key": 1})
        for name in sorted(required):
            lean = {key: value for key, value in good.items() if key != name}
            with self.assertRaises(memory_spine.SpineError):
                memory_spine.validate_payload("transcript.compacted", lean)

    def _seed_with_a_proposal_in_the_middle(
        self, first: int, neighbour: int
    ) -> int:
        held = 0
        for turn in range(70):
            self.memory.add_message(first, "user", f"question {turn} " * 60)
            assistant = self.memory.add_message(
                first, "assistant", f"reply {turn} " * 60
            )
            self.memory.add_message(neighbour, "user", f"other {turn} " * 60)
            self.memory.add_message(neighbour, "assistant", f"answer {turn} " * 60)
            if turn == 10:
                # An event inside the FIRST sub-region.
                self.memory.remember_explicit_project_claim(
                    first, 1,
                    'Remember this project fact: {"subject":"Kestrel relay",'
                    '"predicate":"maintainer","value":"Dana Okonkwo"}',
                )
            if turn == 24:
                self.memory.record_fact_proposal(
                    first, assistant, 1, PROPOSAL_COMMAND,
                    assisted=False, reply_asked_question=False,
                )
                held = assistant
            if turn == 40:
                # And one inside the SECOND, so neither watermark is empty and
                # the disjointness assertion has something to be about.
                self.memory.remember_explicit_project_claim(
                    first, 1,
                    'Remember this project fact: {"subject":"Kestrel relay",'
                    '"predicate":"listen port","value":"8443"}',
                )
        self.assertTrue(held)
        return held


class ClaimReachabilityTests(CompactionStoreCase):
    """E-3 / I-1: the serialized output of the three read surfaces is identical
    before and after compaction, for a fixed query set."""

    QUERIES = [
        "kestrel relay maintainer", "kestrel relay listen port",
        "millrace weir gate count", "who maintains the relay",
        "what port does the relay listen on", "harrier box datacenter",
    ] * 7  # 42 queries, design 2.14 E-3 asks for 40

    def test_e3_reads_are_identical_before_and_after(self) -> None:
        conversations = self.seed_interleaved(conversations=3, turns=40)
        for subject, predicate, value in (
            ("Kestrel relay", "maintainer", "Dana Okonkwo"),
            ("Kestrel relay", "listen port", "8443"),
            ("Millrace weir", "gate count", "four"),
        ):
            self.memory.remember_explicit_project_claim(
                conversations[0], 1,
                "Remember this project fact: "
                + json.dumps({"subject": subject, "predicate": predicate,
                              "value": value}),
            )
        before = [self._read(query) for query in self.QUERIES]
        self.compact_all(conversations)
        after = [self._read(query) for query in self.QUERIES]
        for query, left, right in zip(self.QUERIES, before, after):
            self.assertEqual(left, right, f"read moved for query {query!r}")
        # And every claim id a milestone names is still resolvable by id.
        for row in self.milestones():
            derived = json.loads(str(row["invariants_json"]))["derived"]
            for claim_id in derived["claims_created"]:
                self.assertIsNotNone(self.memory.db.execute(
                    "SELECT 1 FROM memory_claims WHERE id=?", (claim_id,)
                ).fetchone(), f"claim {claim_id} became unreachable")

    def _read(self, query: str) -> str:
        return memory_spine.canonical([
            self.memory.current_claims(query, project_id=1),
            self.memory.graph_chains(query, project_id=1,
                                     subjects=["Kestrel relay"],
                                     seed_claims=[])["rows"],
            self.memory.search(query, project_id=1),
        ])


class EraseTransitivityTests(CompactionStoreCase):
    """E-8 / I-6: all three erase paths reach the spans and the milestones,
    scrub ``message_fts`` FIRST (M-4), and name the counts in their receipt."""

    def test_e8_delete_conversation_removes_spans_and_milestones(self) -> None:
        conversations = self.seed_interleaved(conversations=3, turns=50)
        self.compact_all(conversations)
        target = conversations[0]
        self.assertTrue(self.milestones(target))
        handles = [str(row["handle"]) for row in self.milestones(target)]
        neighbour_before = self.snapshot_messages()[conversations[1]]

        self.memory.delete_conversation(target)

        self.assertEqual(self.milestones(target), [])
        for handle in handles:
            self.assertIsNone(self.memory.db.execute(
                "SELECT 1 FROM memory_compacted_spans WHERE handle=?", (handle,)
            ).fetchone())
        payload = self._last_payload("conversation.deleted")
        self.assertEqual(payload["milestones_removed"], len(handles))
        self.assertEqual(payload["spans_removed"], len(handles))
        self.assertEqual(self.snapshot_messages()[conversations[1]],
                         neighbour_before)
        self.assert_spine_and_graph_clean()
        self.assert_compaction_clean()

    def test_e8_a_claim_tombstone_removes_the_milestones_that_name_the_key(
        self,
    ) -> None:
        conversations = self.seed_interleaved(conversations=2, turns=6)
        self.memory.remember_explicit_project_claim(
            conversations[0], 1,
            'Remember this project fact: {"subject":"Millrace weir",'
            '"predicate":"gate count","value":"four"}',
        )
        # More turns AFTER the claim, so the claim event falls inside the
        # compacted sub-region rather than in the live window (N-3).
        self.seed_more(conversations, turns=50)
        self.compact_all(conversations)
        # ``claim_keys`` holds the store's OWN claim-key values, which are
        # digests, not the operator's words -- ``memory_claims.claim_key``,
        # the ``claim.created`` payload and the ``claim.tombstoned`` payload
        # all carry the same digest.  An earlier version of this test searched
        # for the plaintext subject and matched nothing, so it asserted
        # "vacuous" instead of silently passing over an erase that never ran.
        digest = str(self.memory.db.execute(
            "SELECT claim_key FROM memory_claims ORDER BY id DESC LIMIT 1"
        ).fetchone()[0])
        naming = [
            row for row in self.milestones()
            if digest in set(json.loads(str(row["invariants_json"]))
                             ["derived"]["claim_keys"])
        ]
        self.assertTrue(naming, "no milestone named the key; test is vacuous")

        self.memory.erase_explicit_project_claim(
            conversations[0], 1,
            'Erase this project fact: {"subject":"Millrace weir",'
            '"predicate":"gate count"}',
        )
        payload = self._last_payload("claim.tombstoned")
        self.assertEqual(sorted(payload["removed_milestone_ids"]),
                         sorted(int(row["id"]) for row in naming))
        self.assertEqual(sorted(payload["removed_span_handles"]),
                         sorted(str(row["handle"]) for row in naming))
        for row in naming:
            self.assertIsNone(self.memory.db.execute(
                "SELECT 1 FROM memory_milestones WHERE id=?", (int(row["id"]),)
            ).fetchone())
            self.assertIsNone(self.memory.db.execute(
                "SELECT 1 FROM memory_compacted_spans WHERE handle=?",
                (str(row["handle"]),)
            ).fetchone())
        # seq gaps are legal and expected (design 2.10 item 3).
        self.assert_compaction_clean()

    def test_e8_the_memory_dependent_table_count_stays_ten(self) -> None:
        """N-5: neither M5 table carries a ``memory_id`` column, so the derived
        erase order must not grow."""
        derived = self.memory._memory_dependent_tables()
        self.assertEqual(len(derived), DOCUMENTED_MEMORY_DEPENDENT_TABLE_COUNT,
                         derived)
        self.assertNotIn("memory_milestones", derived)
        self.assertNotIn("memory_compacted_spans", derived)
        for table in ("memory_milestones", "memory_compacted_spans"):
            columns = {
                str(row[1]) for row in self.memory.db.execute(
                    f'PRAGMA table_info("{table}")'
                )
            }
            self.assertNotIn("memory_id", columns)

    def test_e8_fts_is_scrubbed_before_the_delete(self) -> None:
        """M-4, measured rather than asserted from the call order: FTS5
        ``secure-delete`` affects SUBSEQUENT deletions, so the proof is the
        retained index size after a partial delete at 1,500 messages."""
        conversations = self.seed_interleaved(conversations=2, turns=375)
        self.assertGreaterEqual(
            int(self.memory.db.execute(
                "SELECT COUNT(*) FROM messages").fetchone()[0]), 1500)
        before = self._fts_index_bytes()
        self.compact_all(conversations[:1])
        after = self._fts_index_bytes()
        print(f"\n[E-8] message_fts retained bytes {before} -> {after}")
        self.assertLess(after, before,
                        "the FTS index did not shrink; the scrub did not run "
                        "before the delete")

    def test_e8_the_erased_plaintext_is_not_in_the_database_file(self) -> None:
        marker = "quillfeather-escutcheon-9931"
        conversations = self.seed_interleaved(conversations=2, turns=40)
        for _ in range(4):
            self.memory.add_message(conversations[0], "user",
                                    f"remember {marker} for later")
            self.memory.add_message(conversations[0], "assistant", "noted")
        self.compact_all(conversations[:1])
        self.memory.delete_conversation(conversations[0])
        self.memory.db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        self.memory.close()
        blob = self.db_path.read_bytes()
        self.assertNotIn(marker.encode("utf-8"), blob)
        self.memory = Memory(self.db_path)

    def _fts_index_bytes(self) -> int:
        return int(self.memory.db.execute(
            "SELECT COALESCE(SUM(LENGTH(block)), 0) FROM message_fts_data"
        ).fetchone()[0])


class SpanPartitionTests(CompactionStoreCase):
    """E-20 (H-2 and N-3) and E-21: partitioning around proposal-referenced
    messages, and the single referential dependent that makes it necessary."""

    def test_e20_a_proposal_in_the_region_never_raises_and_never_moves(
        self,
    ) -> None:
        conversation = self.seed_interleaved(conversations=2, turns=40)[0]
        assistant_ids = [
            int(row[0]) for row in self.memory.db.execute(
                "SELECT id FROM messages WHERE conversation_id=? AND role='assistant'"
                " ORDER BY id", (conversation,)
            )
        ]
        held = assistant_ids[len(assistant_ids) // 2]
        proposal = self.memory.record_fact_proposal(
            conversation, held, 1, PROPOSAL_COMMAND,
            assisted=False, reply_asked_question=False,
        )
        before = tuple(self.memory.db.execute(
            "SELECT * FROM memory_fact_proposals WHERE id=?", (proposal,)
        ).fetchone())

        plan = self.memory.compact_conversation(conversation, keep_turns=4)
        result = self.memory.compact_conversation(
            conversation, keep_turns=4, apply=True,
            plan_token=plan["plan_token"],
        )
        self.assertTrue(result["applied"], result)

        self.assertEqual(int(self.memory.db.execute(
            "SELECT COUNT(*) FROM messages WHERE id=?", (held,)
        ).fetchone()[0]), 1, "a proposal-referenced message was compacted")
        self.assertEqual(tuple(self.memory.db.execute(
            "SELECT * FROM memory_fact_proposals WHERE id=?", (proposal,)
        ).fetchone()), before, "the anti-forgery record was modified")
        for row in self.milestones(conversation):
            derived = json.loads(str(row["invariants_json"]))["derived"]
            self.assertEqual(derived["span_has_proposal"], 1)

    def test_e21_messages_has_exactly_one_referential_dependent(self) -> None:
        """Design 2.7 step 1b: any future ``REFERENCES messages`` must join the
        partition list in the same commit that adds the foreign key."""
        root = Path(__file__).resolve().parents[1] / "jarvis"
        hits = [
            f"{path.name}:{number}"
            for path in sorted(root.glob("*.py"))
            for number, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), start=1)
            if "REFERENCES messages(" in line
        ]
        self.assertEqual(
            hits, ["memory.py:" + str(self._references_messages_line())],
            "a new foreign key into messages(id) appeared; add it to the "
            "compaction partition list (design 2.7 step 1b) before this test "
            "is repointed",
        )

    @staticmethod
    def _references_messages_line() -> int:
        path = Path(__file__).resolve().parents[1] / "jarvis" / "memory.py"
        for number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if "REFERENCES messages(" in line:
                return number
        raise AssertionError("memory.py no longer references messages(id)")


@requires_schema_50
class SpanBusyTests(CompactionStoreCase):
    """Design item 11.25: the three busy predicates, against REAL rows.

    ``_span_busy_reason`` is the caller-side computation behind design 2.2
    item 9 -- never compact a busy conversation.  compaction-core's module
    tests cover the other half correctly: given ``busy_reason="job_active"``,
    the planner refuses ``span_busy``.  Nothing showed that these three SQL
    queries can ever PRODUCE "job_active".

    The failure mode is what makes it worth its own class.  A wrong status
    string, a malformed scope, or a column renamed by someone else would make
    the function return ``None`` unconditionally; compaction would then run on
    a busy conversation, and every test in the suite would still pass, because
    a refusal that never fires is invisible from both sides.  The general rule
    the boss recorded from it: **a parameterised condition is a hole in the
    test, not a seam in the design** -- wherever a pure function takes a
    condition its caller computes, the computation needs its own test against
    real rows.
    """

    def setUp(self) -> None:
        super().setUp()
        self.conversation = self.seed_interleaved(conversations=2, turns=40)[0]

    def test_an_idle_conversation_is_not_busy(self) -> None:
        """The negative comes first, and it is not a formality: without it a
        function that returned a constant busy reason would pass all three
        positives below."""
        self.assertIsNone(self.memory._span_busy_reason(self.conversation))
        plan = self.memory.compact_conversation(self.conversation, keep_turns=4)
        self.assertIsNone(plan["refusal"])
        self.assertTrue(plan["spans"])

    def test_a_pending_approval_makes_the_conversation_busy(self) -> None:
        stamp = now_iso()
        with self.memory._immediate_transaction():
            self.memory.db.execute(
                """INSERT INTO approvals(created_at, updated_at, fingerprint,
                       action, resource, reason, status, scope)
                   VALUES (?, ?, 'fp', 'act', 'res', 'because', 'pending', ?)""",
                (stamp, stamp, f"conversation:{self.conversation}"),
            )
        self.assertEqual(
            self.memory._span_busy_reason(self.conversation), "approval_pending"
        )
        self._assert_refuses_span_busy()

    def test_a_queued_presence_job_makes_the_conversation_busy(self) -> None:
        stamp = now_iso()
        with self.memory._immediate_transaction():
            self.memory.db.execute(
                """INSERT INTO presence_jobs(job_id, created_at, updated_at,
                       conversation_id, project_id, prompt, model_override,
                       status)
                   VALUES ('job-1', ?, ?, ?, 1, 'p', 'auto', 'queued')""",
                (stamp, stamp, self.conversation),
            )
        self.assertEqual(
            self.memory._span_busy_reason(self.conversation), "job_active"
        )
        self._assert_refuses_span_busy()

    def test_an_active_workflow_makes_the_conversation_busy(self) -> None:
        """Also the first exercise of ``workflow_active`` against a real row.

        A DESIGN FACT HAS DRIFTED, and the test records it rather than
        preserving the design's version.  L-4 says ``long_horizon_plans`` is
        "created lazily by ``long_horizon``, not by ``Memory._migrate``", so an
        unguarded read would raise ``OperationalError`` on a store that never
        ran a workflow.  Measured on this tree: ``Memory._migrate`` DOES create
        it -- ``memory.py:4675`` calls ``migrate_long_horizon_v40`` -- so the
        table is present on every fresh store and the hazard L-4 describes is
        no longer reachable through ``Memory``.  The existence guard stays,
        because it costs nothing and a partially migrated store is still a
        shape someone can produce, but it is now defence rather than the
        necessity the design calls it.
        """
        self.assertTrue(
            _sqlite_table_exists(self.memory.db, "long_horizon_plans"),
            "L-4's premise has changed back; re-read the guard's rationale",
        )
        # Present but empty is still idle -- otherwise this test would pass on
        # a function that keyed off the table existing rather than a row.
        self.assertIsNone(self.memory._span_busy_reason(self.conversation))

        # And the guard's own path, on a store where the table is genuinely
        # absent: it must return None rather than raise.
        with self.memory._immediate_transaction():
            self.memory.db.execute("DROP TABLE long_horizon_plans")
        self.assertIsNone(self.memory._span_busy_reason(self.conversation))
        long_horizon.migrate_long_horizon_v40(self.memory.db)

        stamp = now_iso()
        with self.memory._immediate_transaction():
            # ``long_horizon_plans`` carries foreign keys into tasks,
            # conversations and agent_projects, so the plan needs a real task.
            task_id = int(self.memory.db.execute(
                """INSERT INTO tasks(created_at, updated_at, status, prompt)
                   VALUES (?, ?, 'running', 'a long horizon workflow')""",
                (stamp, stamp),
            ).lastrowid)
            self.memory.db.execute(
                """INSERT INTO long_horizon_plans(created_at, updated_at,
                       clock_floor_at, project_id, conversation_id, task_id,
                       status, manifest_json, manifest_sha256,
                       manifest_mac_sha256, stage_count)
                   VALUES (?, ?, ?, 1, ?, ?, 'active', '{}', ?, ?, 5)""",
                (stamp, stamp, stamp, self.conversation, task_id,
                 "a" * 64, "b" * 64),
            )
        self.assertEqual(
            self.memory._span_busy_reason(self.conversation), "workflow_active"
        )
        self._assert_refuses_span_busy()

    def test_every_reason_this_store_can_produce_is_one_core_knows(self) -> None:
        """The two halves of the seam name the same vocabulary.  Without this
        the caller could return a reason the planner has never heard of and
        the refusal would read as a typo rather than a state."""
        for reason in ("approval_pending", "job_active", "workflow_active"):
            self.assertIn(reason, memory_compaction.BUSY_REASONS)

    def _assert_refuses_span_busy(self) -> None:
        plan = self.memory.compact_conversation(self.conversation, keep_turns=4)
        self.assertEqual(plan["refusal"], "span_busy", plan)
        self.assertEqual(plan["spans"], [])
        applied = self.memory.compact_conversation(
            self.conversation, keep_turns=4, apply=True,
            plan_token=plan["plan_token"],
        )
        self.assertFalse(applied["applied"])
        self.assertEqual(applied["refusal"], "span_busy")
        self.assertEqual(
            int(self.memory.db.execute(
                "SELECT COUNT(*) FROM memory_milestones").fetchone()[0]), 0,
            "a busy conversation was compacted anyway",
        )


class NoCollateralDeletionTests(CompactionStoreCase):
    """E-22 (N-1).  On this host the exposure is not theoretical: with 60
    conversations written round-robin, conversation 1's id range spans 49,922
    rows of which only 834 are its own, so an unscoped delete destroys 49,088
    live rows that exist in no span blob."""

    def test_e22_the_interleaved_neighbour_is_byte_identical_afterwards(
        self,
    ) -> None:
        conversations = self.seed_interleaved(conversations=2, turns=80)
        target, neighbour = conversations
        self.assert_ids_interleave(conversations)
        before = self.snapshot_messages()[neighbour]
        bounds = self.memory.db.execute(
            "SELECT MIN(id), MAX(id) FROM messages WHERE conversation_id=?",
            (target,),
        ).fetchone()
        foreign_in_range = int(self.memory.db.execute(
            "SELECT COUNT(*) FROM messages WHERE conversation_id<>? "
            "AND id BETWEEN ? AND ?", (target, bounds[0], bounds[1])
        ).fetchone()[0])
        self.assertGreater(foreign_in_range, 0,
                           "no foreign rows in range; E-22 would be vacuous")

        self.compact_all([target])

        self.assertEqual(self.snapshot_messages()[neighbour], before,
                         f"{foreign_in_range} interleaved rows were at risk and "
                         "the neighbour changed")
        self.assert_compaction_clean()

    def test_e22_conversation_milestones_never_returns_a_neighbour_bounded_row(
        self,
    ) -> None:
        conversations = self.seed_interleaved(conversations=2, turns=80)
        target, neighbour = conversations
        self.compact_all([target])
        neighbour_ids = {
            int(row[0]) for row in self.memory.db.execute(
                "SELECT id FROM messages WHERE conversation_id=?", (neighbour,)
            )
        }
        result = self.memory.conversation_milestones(target, project_id=1)
        self.assertEqual(result["report"]["mode"], "complete", result["report"])
        self.assertIn(result["report"]["mode"], DOCUMENTED_READ_MODES)
        for row in result["rows"]:
            self.assertNotIn(row["message_ids"]["first"], neighbour_ids)
            self.assertNotIn(row["message_ids"]["last"], neighbour_ids)


class CrashSafetyTests(CompactionStoreCase):
    """E-13, at the REAL kill points (M-17).  Steps 6-11 are one transaction,
    so a kill inside them tests SQLite's atomicity rather than this design's;
    the two windows that belong to the design are the one between the
    outside-the-lock build and the transaction, and the one between commit and
    ``_recall_cache.clear()``."""

    def test_e13_a_kill_before_the_lock_leaves_no_partial_state(self) -> None:
        conversation = self.seed_interleaved(conversations=2, turns=40)[0]
        before = self.snapshot_messages()
        with self.assertRaises(RuntimeError):
            self._compact_raising_at("before_transaction", conversation)
        self.assertEqual(self.snapshot_messages(), before)
        self.assertEqual(self.milestones(), [])
        self.assertEqual(int(self.memory.db.execute(
            "SELECT COUNT(*) FROM memory_spine_events "
            "WHERE kind='transcript.compacted'").fetchone()[0]), 0)
        self.assert_spine_and_graph_clean()

    def test_e13_a_kill_after_commit_still_leaves_a_consistent_store(
        self,
    ) -> None:
        conversation = self.seed_interleaved(conversations=2, turns=40)[0]
        with self.assertRaises(RuntimeError):
            self._compact_raising_at("after_commit", conversation)
        # The write committed; only the cache clear was lost.  Reopen and prove
        # the store is whole and the stale cache cannot be observed.
        self.memory.close()
        self.memory = Memory(self.db_path)
        self.assertTrue(self.milestones(conversation))
        self.assert_compaction_clean()
        self.assert_spine_and_graph_clean()
        for row in self.milestones(conversation):
            self.assertTrue(self.memory.rehydrate(str(row["handle"]))["messages"])

    def _compact_raising_at(self, window: str, conversation: int) -> None:
        """Kill the pass at one of the two windows the DESIGN owns.

        Steps inside the transaction are SQLite's atomicity, not this
        design's, so killing there would test the database.  The two windows
        that belong to the design are the gap between the outside-the-lock
        build and the transaction, and the gap between commit and the recall
        cache clear (M-17).
        """
        plan = self.memory.compact_conversation(conversation, keep_turns=4)
        self.assertTrue(plan["spans"], "nothing to compact; the kill is moot")
        if window == "before_transaction":
            with patch.object(
                Memory, "_apply_compaction_locked",
                side_effect=RuntimeError("killed before the lock"),
            ):
                self.memory.compact_conversation(
                    conversation, keep_turns=4, apply=True,
                    plan_token=plan["plan_token"],
                )
            return
        if window == "after_commit":
            # ``RecallCache`` uses __slots__, so its ``clear`` cannot be
            # patched in place; the whole attribute is swapped for a proxy.
            real_cache = self.memory._recall_cache

            class _DiesOnSecondClear:
                def __init__(self) -> None:
                    self.calls = 0

                def clear(self) -> None:
                    self.calls += 1
                    real_cache.clear()
                    if self.calls >= 2:
                        raise RuntimeError("killed after commit")

                def __getattr__(self, name: str) -> Any:
                    return getattr(real_cache, name)

            self.memory._recall_cache = _DiesOnSecondClear()
            try:
                self.memory.compact_conversation(
                    conversation, keep_turns=4, apply=True,
                    plan_token=plan["plan_token"],
                )
            finally:
                self.memory._recall_cache = real_cache
            return
        raise AssertionError(f"unknown kill window {window!r}")


class ConcurrencyTests(CompactionStoreCase):
    """E-14: a claim writer racing a compaction; neither crashes, and the read
    path never takes the write lock (the ``graph_chains`` pattern,
    memory.py:11306-11308 -- L-2 withdrew the ``current_claims`` citation)."""

    def test_e14_the_read_path_never_takes_the_write_lock(self) -> None:
        conversations = self.seed_interleaved(conversations=2, turns=40)
        self.compact_all(conversations[:1])
        blocker = sqlite3.connect(str(self.db_path), timeout=0.2)
        try:
            blocker.execute("BEGIN IMMEDIATE")
            blocker.execute(
                "INSERT INTO conversations(created_at, title, project_id) "
                "VALUES (?, 'blocker', 1)", (now_iso(),))
            result = self.memory.conversation_milestones(
                conversations[0], project_id=1
            )
            self.assertIn(result["report"]["mode"], DOCUMENTED_READ_MODES)
            self.assertNotEqual(result["report"]["mode"], "error")
            handle = str(self.milestones(conversations[0])[0]["handle"])
            self.assertTrue(self.memory.rehydrate(handle)["messages"])
        finally:
            blocker.rollback()
            blocker.close()

    def test_e14_a_concurrent_writer_never_turns_a_turn_into_a_crash(
        self,
    ) -> None:
        conversations = self.seed_interleaved(conversations=2, turns=40)
        blocker = sqlite3.connect(str(self.db_path), timeout=0.2)
        try:
            blocker.execute("BEGIN IMMEDIATE")
            blocker.execute(
                "INSERT INTO conversations(created_at, title, project_id) "
                "VALUES (?, 'blocker', 1)", (now_iso(),))
            plan = self.memory.compact_conversation(conversations[0])
            result = self.memory.compact_conversation(
                conversations[0], apply=True, plan_token=plan["plan_token"]
            )
            self.assertFalse(result["applied"])
            self.assertIn(result["refusal"],
                          memory_compaction.COMPACTION_REFUSAL_CODES)
        finally:
            blocker.rollback()
            blocker.close()
        self.assert_spine_and_graph_clean()


class ScaleTests(CompactionStoreCase):
    """E-18: 50,000 messages across 60 interleaved conversations, plus the
    cap-legal span shape (M-8).  Timings are always measured and printed; they
    are only enforced under ``JARVIS_ENFORCE_TIMING_GATES=1``."""

    def test_e18_the_read_path_holds_its_budget_at_scale(self) -> None:
        conversations = [
            self.memory.new_conversation(f"scale {index}", project_id=1)
            for index in range(60)
        ]
        seeded = self._bulk_seed_messages(conversations, turns=417)
        self.assertGreaterEqual(seeded, 50000)
        self.assert_ids_interleave(conversations)
        counts_before = {
            int(row[0]): int(row[1]) for row in self.memory.db.execute(
                "SELECT conversation_id, COUNT(*) FROM messages "
                "GROUP BY conversation_id")
        }

        started = time.perf_counter()
        self.compact_all(conversations[:10])
        write_elapsed = (time.perf_counter() - started) * 1000.0

        samples = []
        for conversation in conversations[:10]:
            began = time.perf_counter()
            result = self.memory.conversation_milestones(
                conversation, project_id=1
            )
            samples.append((time.perf_counter() - began) * 1000.0)
            self.assertIn(result["report"]["mode"], DOCUMENTED_READ_MODES)
        samples.sort()
        p50 = samples[len(samples) // 2]
        p95 = samples[min(len(samples) - 1, int(len(samples) * 0.95))]
        print(f"\n[E-18] conversation_milestones p50 {p50:.2f} ms  "
              f"p95 {p95:.2f} ms  max {samples[-1]:.2f} ms  "
              f"(budget {READ_BUDGET_MS} ms); compaction of 10 conversations "
              f"{write_elapsed:.0f} ms")
        if ENFORCE_TIMING_GATES:
            self.assertLessEqual(p95, READ_BUDGET_MS)

        counts_after = {
            int(row[0]): int(row[1]) for row in self.memory.db.execute(
                "SELECT conversation_id, COUNT(*) FROM messages "
                "GROUP BY conversation_id")
        }
        for conversation in conversations[10:]:
            self.assertEqual(counts_after.get(conversation),
                             counts_before.get(conversation),
                             f"conversation {conversation} lost rows to a "
                             "neighbour's compaction (N-1)")

    def test_e18_a_cap_legal_span_stays_inside_the_read_deadline(self) -> None:
        """M-8: ``add_message`` clips at 100,000 characters and
        ``MAX_SPAN_MESSAGES`` is 400, so an unbounded region measured 40 MB and
        ~131 ms per rehydrate.  The splitter must make that unreachable."""
        conversation = self.memory.new_conversation("cap", project_id=1)
        neighbour = self.memory.new_conversation("cap neighbour", project_id=1)
        body = "x" * 40_000
        for turn in range(30):
            self.memory.add_message(conversation, "user", body)
            self.memory.add_message(conversation, "assistant", body)
            self.memory.add_message(neighbour, "user", "short")
            self.memory.add_message(neighbour, "assistant", "short")
        self.compact_all([conversation], keep_turns=2)
        rows = self.milestones(conversation)
        self.assertTrue(rows)
        for row in rows:
            self.assertLessEqual(int(row["source_chars"]), 200_000,
                                 "max_span_chars did not split the region")
            began = time.perf_counter()
            span = self.memory.rehydrate(str(row["handle"]))
            elapsed = (time.perf_counter() - began) * 1000.0
            print(f"[E-18] rehydrate {int(row['message_count'])} rows / "
                  f"{int(row['source_chars'])} chars: {elapsed:.1f} ms")
            self.assertEqual(len(span["messages"]), int(row["message_count"]))


class SchemaFiftyTests(CompactionStoreCase):
    """The storage contract itself: version, immutability, lineage, and the id
    sequence that a tombstoned milestone must not give back."""

    def test_the_schema_version_is_fifty(self) -> None:
        self.assertEqual(SCHEMA_VERSION, 50)
        self.assertEqual(int(self.memory.db.execute(
            "PRAGMA user_version").fetchone()[0]), SCHEMA_VERSION)
        self.assertEqual(memory_spine.SPINE_SCHEMA_VERSION, 49)
        self.assertTrue(self.memory._compaction_ready)

    def test_neither_table_permits_an_update(self) -> None:
        conversation = self.seed_interleaved(conversations=2, turns=40)[0]
        self.compact_all([conversation])
        row = self.milestones(conversation)[0]
        with self.assertRaises(sqlite3.IntegrityError):
            with self.memory._immediate_transaction():
                self.memory.db.execute(
                    "UPDATE memory_milestones SET summary='x' WHERE id=?",
                    (int(row["id"]),))
        with self.assertRaises(sqlite3.IntegrityError):
            with self.memory._immediate_transaction():
                self.memory.db.execute(
                    "UPDATE memory_compacted_spans SET body_chars=0 "
                    "WHERE handle=?", (str(row["handle"]),))

    def test_a_milestone_without_its_spine_event_is_caught_by_verify(self) -> None:
        """Lineage is enforced at VERIFY time, not by a write-time trigger, and
        that is a deliberate trade rather than an omission.

        The schema-47/49 idiom is a ``BEFORE INSERT`` trigger naming the
        creating event.  Compaction ships none, and no foreign key into
        ``memory_spine_events`` either (``COMPACTION_SPINE_FOREIGN_KEYS`` is
        False) -- because a trigger on another table that references the events
        table is exactly what broke every real store at 49->50: SQLite
        re-parses it on the rebuild's ``ALTER TABLE ... RENAME``.  Buying
        write-time enforcement would cost the thing that made the migration
        fail.  So the guarantee moves to ``verify_compaction``, which reports
        ``receipt_missing``, and this test pins that it actually fires --
        otherwise "no trigger" would quietly mean "no check".
        """
        conversation = self.seed_interleaved(conversations=2, turns=60)[0]
        self.compact_all([conversation])
        self.assert_compaction_clean()
        row = self.milestones(conversation)[0]
        self.memory.close()
        tamper = sqlite3.connect(str(self.db_path))
        try:
            tamper.execute("DROP TRIGGER memory_milestones_immutable")
            tamper.execute(
                "UPDATE memory_milestones SET spine_event_id=999999 WHERE id=?",
                (int(row["id"]),))
            tamper.commit()
        finally:
            tamper.close()
        self.memory = Memory(self.db_path)
        health = self.memory.verify_compaction()
        self.assertFalse(health["ok"])
        self.assertIn(
            "receipt_missing",
            {str(problem[1]) for problem in health["problems"]},
            health["problems"],
        )

    def _unused_lineage_trigger_probe(self) -> None:
        conversation = self.seed_interleaved(conversations=2, turns=40)[0]
        with self.assertRaises(sqlite3.IntegrityError):
            with self.memory._immediate_transaction():
                self.memory.db.execute(
                    "INSERT INTO memory_milestones(created_at, conversation_id,"
                    " seq, first_message_id, last_message_id, message_count,"
                    " source_chars, stored_bytes, summary, summary_chars,"
                    " invariants_json, handle, span_sha256, span_unkeyed_sha256,"
                    " summary_sha256, invariants_sha256, key_fingerprint,"
                    " author, spine_event_id)"
                    " VALUES (?, ?, 1, 1, 2, 2, 10, 10, 's', 1, '{}', 'h',"
                    f" '{'0' * 64}', '{'0' * 64}', '{'0' * 64}', '{'0' * 64}',"
                    f" '{'0' * 64}', 'runtime', 999999)",
                    (now_iso(), conversation),
                )

    def test_a_tombstoned_milestone_id_is_never_reused(self) -> None:
        """Boss ruling on F-2.  ``id INTEGER PRIMARY KEY`` is a rowid alias, and
        without ``AUTOINCREMENT`` SQLite hands a deleted top id straight back --
        measured on 3.50.4: delete id 3 of 3, insert, get 3.  A claim tombstone
        DELETES milestone rows (design 2.10 item 3) and writes their ids into
        ``claim.tombstoned``'s ``removed_milestone_ids``, which is append-only
        history that can never be corrected.  So a reused id makes the spine
        permanently record "milestone 17 was removed" while a live, unrelated
        milestone 17 exists -- an append-only record falsified by a later
        insert, which is the one thing append-only exists to prevent.
        """
        conversations = self.seed_interleaved(conversations=2, turns=6)
        conversation = conversations[0]
        self.memory.remember_explicit_project_claim(
            conversation, 1,
            'Remember this project fact: {"subject":"Millrace weir",'
            '"predicate":"gate count","value":"four"}',
        )
        self.seed_more(conversations, turns=50)
        self.compact_all([conversation])
        erased_ids = [int(row["id"]) for row in self.milestones()]
        erased_seqs = [int(row["seq"]) for row in self.milestones(conversation)]
        erased_handles = [str(row["handle"]) for row in self.milestones()]
        self.assertTrue(erased_ids)
        self.memory.erase_explicit_project_claim(
            conversation, 1,
            'Erase this project fact: {"subject":"Millrace weir",'
            '"predicate":"gate count"}',
        )
        self.assertEqual(self.milestones(), [],
                         "the tombstone did not remove the milestones that "
                         "named the key; the reuse window never opens")
        recorded = self._last_payload("claim.tombstoned")["removed_milestone_ids"]
        self.assertEqual(sorted(recorded), sorted(erased_ids))

        for turn in range(60):
            self.memory.add_message(conversation, "user", f"later {turn} " * 60)
            self.memory.add_message(conversation, "assistant", f"more {turn} " * 60)
        self.compact_all([conversation])
        fresh = self.milestones()
        self.assertTrue(fresh)
        self.assertFalse(
            {int(row["id"]) for row in fresh} & set(erased_ids),
            "a deleted milestone id came back; the spine's "
            "removed_milestone_ids now names a live row",
        )
        # ``seq`` is embedded in the handle, so a reused ``seq`` collides two
        # different spans on the operator-facing identity as well.
        self.assertFalse(
            {int(row["seq"]) for row in self.milestones(conversation)}
            & set(erased_seqs),
            "a deleted seq came back; MAX(seq)+1 is not a monotone source",
        )
        self.assertFalse(
            {str(row["handle"]) for row in fresh} & set(erased_handles),
            "a handle from an erased span was minted again",
        )

    def test_the_downgrade_refusal_names_the_state_and_a_recovery_that_exists(
        self,
    ) -> None:
        """Boss ruling on F-3.  The message must tell an operator which marker
        they are looking at, how much is at stake, and what to type -- and must
        not name ``compaction repair-schema``, which half A does not ship.
        E-12 asserts the same thing on a real schema-49 store; this asserts it
        on every run.  (E-12 no longer skips: it restores a frozen dump
        instead of archiving a commit, so it exercises the real
        ``_migrate_v50`` path on every ordinary run.)
        """
        conversation = self.seed_interleaved(conversations=2, turns=60)[0]
        self.compact_all([conversation])
        spans = int(self.memory.db.execute(
            "SELECT COUNT(*) FROM memory_compacted_spans").fetchone()[0])
        self.assertGreater(spans, 0)
        self.memory.close()
        raw = sqlite3.connect(str(self.db_path))
        try:
            raw.execute("PRAGMA user_version=49")
            raw.commit()
        finally:
            raw.close()

        with self.assertRaises(RuntimeError) as caught:
            Memory(self.db_path)
        message = str(caught.exception)
        self.assertIn("compaction_downgrade_refused", message)
        self.assertIn(str(spans), message)
        self.assertIn("49", message)
        self.assertIn("PRAGMA user_version = 50", message)
        self.assertIn("docs/COMPACTION.md", message)
        self.assertNotIn("repair-schema", message)

        # The refusal wrote nothing and dropped nothing: the marker is still 49
        # and the spans are still there, so the advice it just gave is true.
        raw = sqlite3.connect(str(self.db_path))
        try:
            self.assertEqual(
                int(raw.execute("PRAGMA user_version").fetchone()[0]), 49)
            self.assertEqual(int(raw.execute(
                "SELECT COUNT(*) FROM memory_compacted_spans").fetchone()[0]),
                spans)
            raw.execute("PRAGMA user_version = 50")
            raw.commit()
        finally:
            raw.close()
        self.memory = Memory(self.db_path)
        self.assert_compaction_clean()

    def test_the_verify_compaction_contract_is_one_shape(self) -> None:
        """The two-owner contract that nearly went out as two.

        compaction-surface measured an earlier `memory_compaction` whose
        ``checked`` was an int (milestones examined) while this wrapper had
        committed to ``checked`` as a bool (was the store checked at all).  The
        failure was silent in both directions: ``if not health["checked"]``
        reads a healthy empty store as *not checked*, and a refused store that
        examined four milestones as *checked* -- M4 finding 2 exactly.  Core has
        since landed both facts under separate names.  This pins that, so the
        collision cannot reopen quietly, and the wrapper returns core's dict
        unmodified rather than adapting it: an adapter is where the two
        meanings would go back to hiding.
        """
        health = self.memory.verify_compaction()
        self.assertIsInstance(health["checked"], bool)
        self.assertIsInstance(health["milestones_checked"], int)
        self.assertIsInstance(health["ok"], bool)
        self.assertIsInstance(health["counts"], dict)
        self.assertEqual(
            set(health["counts"]),
            {"milestones", "spans", "conversations", "verified", "unverifiable"},
        )
        self.assertIn("refusal", health)
        self.assertIn("refusal_detail", health)
        self.assertIn("chain_verified", health)
        self.assertNotIn("reason", health,
                         "the wrapper adapted core's keys instead of passing "
                         "them through")
        # A healthy empty store: checked, ok, nothing examined.  "Nothing to
        # check" and "not checked" must not render the same.
        self.assertTrue(health["checked"])
        self.assertTrue(health["ok"], health.get("problems"))
        self.assertEqual(health["milestones_checked"], 0)
        self.assertIsNone(health["refusal"])
        # An empty store still gets a chain answer: "nothing to check" and
        # "checked nothing on a chain nobody verified" are different facts.
        self.assertIs(health["chain_verified"], True)

        conversation = self.seed_interleaved(conversations=2, turns=60)[0]
        self.compact_all([conversation])
        health = self.memory.verify_compaction()
        self.assertTrue(health["checked"])
        self.assertTrue(health["ok"], health.get("problems"))
        self.assertGreater(health["milestones_checked"], 0)
        for problem in health["problems"]:
            self.assertIn(problem[1], memory_compaction.COMPACTION_PROBLEM_KINDS)

    def test_the_verify_wrapper_never_leaves_the_chain_unclaimed(self) -> None:
        """Boss ruling, and the read-path twin of the rebuild ruling.

        Measured rather than assumed: ``memory_compaction.verify_compaction``
        names ``verify_spine`` only in prose and never calls it -- the module
        greps zero for ``prev_sha256``, ``event_sha256`` and ``head_mac``, and
        the single ``verify_spine(`` in the whole file is inside a comment.
        That is correct for a pure function, which cannot know.  ``Memory``
        owns ``verify_spine``, so the wrapper answers the question instead of
        passing it up to an operator surface where the reader would supply the
        optimistic answer.
        """
        parameters = inspect.signature(
            memory_compaction.verify_compaction
        ).parameters
        self.assertIsNone(parameters["spine_ok"].default)
        module_level = memory_compaction.verify_compaction(
            self.memory.db, self.memory._spine_key
        )
        self.assertIsNone(module_level["chain_verified"])

        wrapped = self.memory.verify_compaction()
        self.assertIsNotNone(
            wrapped["chain_verified"],
            "Memory.verify_compaction passed the question through instead of "
            "answering it",
        )
        self.assertIsInstance(wrapped["chain_verified"], bool)

    def test_a_forged_chain_does_not_render_a_clean_compaction_line(
        self,
    ) -> None:
        """The false branch, and the trap inside it.

        A forged chain leaves every fact ``verify_compaction`` checks intact:
        each receipt is present, each recorded digest matches its record.  So
        ``problems`` comes back EMPTY -- and an empty problem list is exactly
        what a printer reads as healthy.  The guarantee is therefore not "the
        problems list is non-empty" but "the line cannot render as clean": the
        qualifier is false and ``ok`` is false despite there being nothing to
        list.  Boss ruling: no separate spine line in ``doctor``, because two
        adjacent lines get read independently and an operator scanning for red
        sees a green compaction line and stops.
        """
        conversation = self.seed_interleaved(conversations=2, turns=60)[0]
        self.compact_all([conversation])
        self.assert_compaction_clean()

        event = self.memory.db.execute(
            "SELECT id, payload_json FROM memory_spine_events "
            "WHERE payload_json IS NOT NULL ORDER BY id LIMIT 1"
        ).fetchone()
        payload = json.loads(str(event["payload_json"]))
        payload["forged"] = True
        self.memory.close()
        tamper = sqlite3.connect(str(self.db_path))
        try:
            tamper.execute("DROP TRIGGER memory_spine_events_redaction_only")
            tamper.execute(
                "UPDATE memory_spine_events SET payload_json=? WHERE id=?",
                (memory_spine.canonical(payload), int(event["id"])),
            )
            tamper.commit()
        finally:
            tamper.close()
        self.memory = Memory(self.db_path)

        self.assertFalse(self.memory.verify_spine()["ok"])
        health = self.memory.verify_compaction()
        self.assertIs(health["chain_verified"], False)
        self.assertFalse(health["ok"])
        self.assertEqual(health["refusal"], "spine_unverified")
        self.assertTrue(health["refusal_detail"])
        # The trap, asserted explicitly: nothing is WRONG with the compaction
        # records themselves, so a reader keying on ``problems`` alone would
        # print this as healthy.
        self.assertEqual(
            health["problems"], [],
            "if the forged chain produced compaction problems this test is "
            "no longer exercising the trap it was written for",
        )

    def test_a_silent_outcome_is_never_reported_as_complete(self) -> None:
        """Correctness review HIGH-2 / design item 11.19, at the store layer.

        ``conversation_milestones`` used to read ``derived.get("outcome") or
        "complete"``, which manufactured the STRONGEST value in the closed set
        out of an absence, on the one surface in this phase that reaches a
        model.  All three parts of 11.19 failed at once: the closed set
        absorbed the unknown, the model was told the span completed, and
        ``outcome_missing`` could never leave zero because the renderer never
        saw a silent row.

        Both directions, because a pass-through that always returned ``None``
        would satisfy half of this.
        """
        conversation = self.seed_interleaved(conversations=2, turns=60)[0]
        self.compact_all([conversation])
        row = self.milestones(conversation)[0]

        # Direction 1: a stated outcome survives unchanged.
        stated = self.memory.conversation_milestones(conversation, project_id=1)
        self.assertEqual(stated["report"]["mode"], "complete")
        self.assertEqual([entry["outcome"] for entry in stated["rows"]],
                         ["complete"])
        block = memory_compaction.render_compacted_history_block(stated["rows"])
        self.assertEqual(block.outcome_missing, 0)
        self.assertNotIn(memory_compaction.HISTORY_OUTCOME_UNSTATED, block.text)

        # Direction 2: an absent outcome stays absent all the way out.
        payload = json.loads(str(row["invariants_json"]))
        payload["derived"].pop("outcome")
        self.memory.close()
        tamper = sqlite3.connect(str(self.db_path))
        try:
            tamper.execute("DROP TRIGGER memory_milestones_immutable")
            tamper.execute(
                "UPDATE memory_milestones SET invariants_json=? WHERE id=?",
                (memory_spine.canonical(payload), int(row["id"])),
            )
            tamper.commit()
        finally:
            tamper.close()
        self.memory = Memory(self.db_path)

        silent = self.memory.conversation_milestones(conversation, project_id=1)
        self.assertTrue(silent["rows"])
        for entry in silent["rows"]:
            self.assertIsNone(
                entry["outcome"],
                "the store invented an outcome for a record that has none",
            )
            self.assertNotEqual(entry["outcome"], "complete")
        # And the renderer's existing mapping now actually sees a silent row,
        # which is the thing that was unreachable end to end.
        block = memory_compaction.render_compacted_history_block(silent["rows"])
        self.assertEqual(block.outcome_missing, len(silent["rows"]))
        self.assertIn(memory_compaction.HISTORY_OUTCOME_UNSTATED, block.text)
        self.assertNotIn('"outcome":"complete"', block.text)

    def test_the_author_column_refuses_model(self) -> None:
        """M-16: the CHECK keeps ``'model'`` so a later phase needs no table
        rebuild, but M5's writer must refuse it."""
        self.assertIn("model", memory_compaction.COMPACTION_AUTHORS)
        conversation = self.seed_interleaved(conversations=2, turns=40)[0]
        result = self.memory.compact_conversation(conversation, apply=True,
                                                  plan_token="x")
        self.assertFalse(result["applied"])

    def test_strip_spine_refuses_a_non_empty_span_table(self) -> None:
        """H-1: the graph rule does not transfer -- a span is the only copy."""
        conversation = self.seed_interleaved(conversations=2, turns=40)[0]
        self.compact_all([conversation])
        with self.assertRaises(memory_compaction.CompactionError) as caught:
            strip_spine(self.memory.db)
        self.assertEqual(caught.exception.code, "compaction_downgrade_refused")
        # And it refused rather than emptying them on the way past.
        self.assertTrue(self.milestones())


@requires_schema_50
class RealSchemaFortyNineStoreMigrationTests(unittest.TestCase):
    """E-12: migration 49 -> 50 on a store built by the M4 commit's OWN writers.

    The M4 correctness review's HIGH-1 was exactly this gap: every fixture built
    its "legacy" store with the CURRENT tree, which creates the widened events
    table directly and so never runs ``_rebuild_events_table``'s copy-and-rename
    -- the step that made every real store fail to open.  A synthetic fixture
    cannot reproduce it, so this one shells out to the M4 tree.

    That store is now a frozen SQL dump captured once from the M4 tree, so
    this class RUNS on every ordinary checkout instead of skipping when an
    environment variable is unset -- which is how it came to be green for a
    whole phase without ever exercising the store-layer migration.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory(prefix="m5-legacy-")
        cls.root = Path(cls._tmp.name)
        pristine = cls.root / "pristine" / "data"
        pristine.mkdir(parents=True)
        cls._restore(pristine / "jarvis.db")
        cls.baseline = cls._baseline_of(pristine / "jarvis.db")

    @classmethod
    def _restore(cls, database: Path) -> None:
        """Two passes with a reopen between them.

        ``sqlite3.iterdump`` writes a virtual table through ``sqlite_master``
        under ``writable_schema=ON`` and then INSERTs into it in the same
        script, before the connection reloads the schema, so a one-pass
        restore dies with ``no such table: memory_fts``.  Pass 2 is ordinary
        DDL on a fresh connection.
        """
        text = LEGACY_DUMP.read_text(encoding="utf-8")
        assert LEGACY_PASS_SPLIT in text, "the dump lost its pass marker"
        for chunk in text.split(LEGACY_PASS_SPLIT, 1):
            connection = sqlite3.connect(str(database))
            try:
                connection.executescript(chunk)
                connection.commit()
            finally:
                connection.close()
        Path(str(database) + memory_spine.KEY_SIDECAR_SUFFIX).write_text(
            LEGACY_KEY_HEX, encoding="ascii")

    @classmethod
    def _baseline_of(cls, database: Path) -> dict:
        """Read the fixture's own shape out of the restored store.

        Derived rather than checked in beside the dump: these are facts ABOUT
        the fixture, not expectations about the migration, so reading them
        from the pre-migration store is honest and removes a second artefact
        that could drift out of step with the first.
        """
        connection = sqlite3.connect(str(database))
        connection.row_factory = sqlite3.Row
        try:
            conversations = [
                int(row["id"]) for row in connection.execute(
                    "SELECT id FROM conversations ORDER BY id")
            ]
            longest = int(connection.execute(
                "SELECT conversation_id FROM messages GROUP BY conversation_id "
                "ORDER BY COUNT(*) DESC, conversation_id LIMIT 1").fetchone()[0])
            held = connection.execute(
                "SELECT assistant_message_id FROM memory_fact_proposals "
                "WHERE assistant_message_id IS NOT NULL "
                "ORDER BY id LIMIT 1").fetchone()
            baseline = {
                "conversations": conversations,
                "long_conversation_id": longest,
                "held_message_id": None if held is None else int(held[0]),
                "messages": int(connection.execute(
                    "SELECT COUNT(*) FROM messages").fetchone()[0]),
                "events": int(connection.execute(
                    "SELECT COUNT(*) FROM memory_spine_events").fetchone()[0]),
                "events_sql": str(connection.execute(
                    "SELECT sql FROM sqlite_master WHERE type='table' "
                    "AND name='memory_spine_events'").fetchone()[0]),
                "user_version": int(connection.execute(
                    "PRAGMA user_version").fetchone()[0]),
            }
        finally:
            connection.close()
        # The fixture must be genuinely pre-M5, or every test below passes by
        # migrating nothing -- the M4 HIGH-1 shape, and the reason this class
        # exists at all.
        assert baseline["user_version"] == 49, baseline["user_version"]
        assert "'transcript.compacted'" not in baseline["events_sql"]
        assert baseline["held_message_id"] is not None
        assert len(baseline["conversations"]) >= 4
        return baseline

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def setUp(self) -> None:
        self.case = Path(tempfile.mkdtemp(prefix="m5-legacy-case-",
                                          dir=str(self.root)))
        shutil.copytree(self.root / "pristine" / "data", self.case / "data")
        self.db_path = self.case / "data" / "jarvis.db"

    def test_the_fixture_is_genuinely_pre_m5(self) -> None:
        """The precondition every other test in this class rests on, asserted
        rather than assumed -- and the guard against someone re-dumping from a
        current tree, which would silently turn all of E-12 into a suite that
        migrates nothing."""
        raw = sqlite3.connect(str(self.db_path))
        try:
            self.assertEqual(
                int(raw.execute("PRAGMA user_version").fetchone()[0]), 49)
            events_sql = str(raw.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' "
                "AND name='memory_spine_events'").fetchone()[0])
            self.assertNotIn("'transcript.compacted'", events_sql)
            self.assertGreater(int(raw.execute(
                "SELECT COUNT(*) FROM messages").fetchone()[0]), 400)
            # M4 is really present, so the trigger hazard is reachable here.
            dependent = {
                str(name) for (name,) in raw.execute(
                    "SELECT name FROM sqlite_master WHERE type='trigger' "
                    "AND sql LIKE '%memory_spine_events%' "
                    "AND tbl_name != 'memory_spine_events'")
            }
            self.assertIn("ladder_promotions_require_spine_event", dependent)
        finally:
            raw.close()

    def test_e12_a_real_schema_49_store_opens_migrates_and_loses_nothing(
        self,
    ) -> None:
        raw = sqlite3.connect(str(self.db_path))
        try:
            self.assertEqual(
                int(raw.execute("PRAGMA user_version").fetchone()[0]), 49)
            counts_before = self._counts(raw)
            # PRECONDITION, design 11.21(a): the events table must start
            # NARROW, or the migration below finds nothing to do and this
            # whole test passes by never running the copy-and-rename -- the
            # M4 HIGH-1 shape and the reason this class exists.
            events_sql_before = str(raw.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' "
                "AND name='memory_spine_events'").fetchone()[0])
            self.assertNotIn("'transcript.compacted'", events_sql_before)
        finally:
            raw.close()

        memory = Memory(self.db_path)
        try:
            self.assertEqual(int(memory.db.execute(
                "PRAGMA user_version").fetchone()[0]), SCHEMA_VERSION)
            # ...and the work was ACTUALLY DONE: the CHECK moved, which only
            # happens if _rebuild_events_table copied and renamed the table.
            events_sql_after = str(memory.db.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' "
                "AND name='memory_spine_events'").fetchone()[0])
            self.assertIn("'transcript.compacted'", events_sql_after)
            self.assertNotEqual(events_sql_after, events_sql_before)
            # The rename really did re-parse the schema, and every dependent
            # object came back -- including M4 triggers on other tables and
            # the six FTS triggers, which is what M-1 broke.
            self.assertIn("ladder_promotions_require_spine_event", {
                str(name) for (name,) in memory.db.execute(
                    "SELECT name FROM sqlite_master WHERE type='trigger'")})
            self.assertTrue(memory_compaction.compaction_ready(memory.db))
            self.assertTrue(memory._compaction_ready)
            counts_after = self._counts(memory.db)
            for table, count in counts_before.items():
                self.assertEqual(counts_after[table], count,
                                 f"{table} changed across the migration")
            self.assertTrue(memory.verify_spine()["ok"])
            self.assertTrue(memory.verify_graph()["ok"])
            compaction_health = memory.verify_compaction()
            self.assertIs(compaction_health["chain_verified"], True)
            self.assertTrue(compaction_health["ok"],
                            compaction_health.get("problems"))
            self.assertTrue(memory.rebuild_claim_projection()["ok"])
            self.assertEqual(
                memory.rebuild_graph_projection().get("divergences") or [], [])
            # No backfill: nothing was compacted by the migration itself.
            self.assertEqual(int(memory.db.execute(
                "SELECT COUNT(*) FROM memory_milestones").fetchone()[0]), 0)
            self.assertEqual(int(memory.db.execute(
                "SELECT COUNT(*) FROM memory_spine_events "
                "WHERE kind='transcript.compacted'").fetchone()[0]), 0)
            # The store still works: one real compaction on migrated data.
            conversation = int(self.baseline["long_conversation_id"])
            plan = memory.compact_conversation(conversation, keep_turns=4)
            self.assertTrue(plan["spans"], plan)
            applied = memory.compact_conversation(
                conversation, keep_turns=4, apply=True,
                plan_token=plan["plan_token"])
            self.assertTrue(applied["applied"], applied)
            handle = str(memory.db.execute(
                "SELECT handle FROM memory_milestones ORDER BY id LIMIT 1"
            ).fetchone()[0])
            self.assertTrue(memory.rehydrate(handle)["messages"])
        finally:
            memory.close()

        memory = Memory(self.db_path)  # idempotent reopen
        try:
            self.assertTrue(memory.verify_spine()["ok"])
            compaction_health = memory.verify_compaction()
            self.assertIs(compaction_health["chain_verified"], True)
            self.assertTrue(compaction_health["ok"],
                            compaction_health.get("problems"))
        finally:
            memory.close()

    def test_e12_a_downgrade_with_spans_refuses_and_drops_nothing(self) -> None:
        memory = Memory(self.db_path)
        try:
            conversation = int(self.baseline["long_conversation_id"])
            plan = memory.compact_conversation(conversation, keep_turns=4)
            memory.compact_conversation(
                conversation, keep_turns=4, apply=True,
                plan_token=plan["plan_token"])
            spans = int(memory.db.execute(
                "SELECT COUNT(*) FROM memory_compacted_spans").fetchone()[0])
            self.assertGreater(spans, 0)
            bodies = [tuple(row) for row in memory.db.execute(
                "SELECT handle, body FROM memory_compacted_spans ORDER BY handle")]
        finally:
            memory.close()

        raw = sqlite3.connect(str(self.db_path))
        try:
            raw.execute("PRAGMA user_version=49")
            raw.commit()
        finally:
            raw.close()
        with self.assertRaises(RuntimeError) as caught:
            Memory(self.db_path)
        self.assertIn("compaction_downgrade_refused", str(caught.exception))

        # Nothing was dropped, and the bytes are byte-identical (H-1).
        raw = sqlite3.connect(str(self.db_path))
        try:
            self.assertEqual(
                [tuple(row) for row in raw.execute(
                    "SELECT handle, body FROM memory_compacted_spans "
                    "ORDER BY handle")],
                bodies,
            )
        finally:
            raw.close()

    def test_e12_the_refusal_precedes_the_graph_drop(self) -> None:
        """N-9: the check is the first statement in ``_migrate``'s transaction,
        before the ``if version < 48`` graph DROP at memory.py:1470-1481.  A
        migration that is about to refuse must not have dropped the three graph
        tables on its way to raising."""
        memory = Memory(self.db_path)
        try:
            conversation = int(self.baseline["long_conversation_id"])
            plan = memory.compact_conversation(conversation, keep_turns=4)
            memory.compact_conversation(
                conversation, keep_turns=4, apply=True,
                plan_token=plan["plan_token"])
            graph_rows = int(memory.db.execute(
                "SELECT COUNT(*) FROM memory_graph_edges").fetchone()[0])
            self.assertGreater(graph_rows, 0)
        finally:
            memory.close()
        raw = sqlite3.connect(str(self.db_path))
        try:
            raw.execute("PRAGMA user_version=47")
            raw.commit()
        finally:
            raw.close()
        with self.assertRaises(RuntimeError) as caught:
            Memory(self.db_path)
        self.assertIn("compaction_downgrade_refused", str(caught.exception))
        raw = sqlite3.connect(str(self.db_path))
        try:
            for table in ("memory_graph_edges", "memory_graph_entities"):
                self.assertIsNotNone(
                    raw.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                        (table,)).fetchone(),
                    f"{table} was dropped by a migration that then refused",
                )
            self.assertEqual(int(raw.execute(
                "SELECT COUNT(*) FROM memory_graph_edges").fetchone()[0]),
                graph_rows)
        finally:
            raw.close()

    @staticmethod
    def _counts(db: sqlite3.Connection) -> dict[str, int]:
        return {
            table: int(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in (
                "messages", "conversations", "memories", "memory_claims",
                "memory_spine_events", "memory_graph_edges",
                "memory_graph_entities", "memory_fact_proposals",
            )
        }




if __name__ == "__main__":  # pragma: no cover
    unittest.main()
