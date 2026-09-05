"""Module tests for compaction's pure half plus its two owned tables (VTMF M5).

No Agent and no Memory: every fixture here is built in-test, and the only
database is an in-memory SQLite carrying the handful of columns
``jarvis/memory_compaction.py`` actually reads.  The store-side and agent-side
halves are ``tests/test_memory_compaction_integration.py`` and
``tests/test_agent_compaction.py``, owned by the other two implementers.

Two disciplines the M4 record made binding and this file tries to keep:
every guard is asserted in **both** directions, so a flag hardwired false
cannot satisfy it; and where a number is the point, the test asserts the
number rather than that something did not raise.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import json
import sqlite3
import tempfile
import unittest
import zlib
from pathlib import Path

from jarvis import memory_compaction as mc
from jarvis import memory_spine as spine

KEY = b"k" * 32
OTHER_KEY = b"j" * 32


# --- builders ---------------------------------------------------------------

def row(mid, role="user", *, conversation_id=1, chars=None, content=None, at=None):
    """One MessageRow; ``at`` defaults to a stamp that sorts with the id."""
    stamp = at if at is not None else f"2026-09-04T09:{mid % 60:02d}:{mid % 60:02d}Z"
    if content is None and chars is None:
        chars = 100
    if content is not None:
        return mc.MessageRow(id=mid, conversation_id=conversation_id,
                             created_at=stamp, role=role, content=content)
    return mc.MessageRow(id=mid, conversation_id=conversation_id,
                         created_at=stamp, role=role, chars=chars)


def turns(count, *, start=1, chars=100, conversation_id=1, content=None):
    """``count`` complete two-row turns starting at message id ``start``."""
    out = []
    mid = start
    for _ in range(count):
        out.append(row(mid, "user", conversation_id=conversation_id,
                       chars=chars, content=content))
        out.append(row(mid + 1, "assistant", conversation_id=conversation_id,
                       chars=chars, content=content))
        mid += 2
    return out


def event(eid, kind, *, at, conversation_id=1, subject_kind=None, subject_id=None,
          payload=None, outcome="applied"):
    return mc.SpineEventRow(
        id=eid, conversation_id=conversation_id, created_at=at, kind=kind,
        outcome=outcome, subject_kind=subject_kind, subject_id=subject_id,
        payload=payload,
    )


def bounds(**changes):
    base = dict(
        conversation_id=1, first_message_id=1, last_message_id=10,
        message_count=10, source_chars=1000, last_created_at="2026-09-04T09:30:00Z",
        span_has_proposal=0, message_ids=tuple(range(1, 11)),
    )
    base.update(changes)
    return mc.SpanBounds(**base)


_SPINE_DDL = """CREATE TABLE memory_spine_events (
    id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, kind TEXT NOT NULL,
    outcome TEXT NOT NULL, conversation_id INTEGER, subject_kind TEXT,
    subject_id INTEGER, payload_json TEXT)"""


def replace_body(db, body):
    """Damage a span the only way the store permits.

    ``UPDATE`` is refused by ``memory_compacted_spans_immutable`` -- which is
    the point of the trigger -- so a tamper test must do what a file-level
    attacker does and put different bytes under the same handle.
    """
    row = db.execute(
        "SELECT handle, milestone_id, conversation_id, body_chars "
        "FROM memory_compacted_spans"
    ).fetchone()
    db.execute("DELETE FROM memory_compacted_spans WHERE handle=?", (row[0],))
    db.execute(
        "INSERT INTO memory_compacted_spans(handle, milestone_id, "
        "conversation_id, body, body_chars) VALUES (?, ?, ?, ?, ?)",
        (row[0], row[1], row[2], body, row[3]),
    )


def replace_span_row(db, **changes):
    """Re-file a span under different metadata, same handle and body."""
    row = db.execute(
        "SELECT handle, milestone_id, conversation_id, body, body_chars "
        "FROM memory_compacted_spans"
    ).fetchone()
    values = dict(zip(("handle", "milestone_id", "conversation_id", "body",
                       "body_chars"), row))
    values.update(changes)
    db.execute("DELETE FROM memory_compacted_spans WHERE handle=?", (row[0],))
    db.execute(
        "INSERT INTO memory_compacted_spans(handle, milestone_id, "
        "conversation_id, body, body_chars) VALUES (:handle, :milestone_id, "
        ":conversation_id, :body, :body_chars)", values)


def set_payload(db, payload_json):
    """Rewrite the receipt payload.  memory_spine_events carries no
    immutability trigger in this file's minimal fixture, and the real spine's
    redaction-only trigger permits a payload rewrite, so an UPDATE is right
    here where it is wrong on the milestone tables."""
    db.execute("UPDATE memory_spine_events SET payload_json=?", (payload_json,))


_OPEN_STORES: list[sqlite3.Connection] = []


def tearDownModule():  # noqa: N802 - unittest's spelling
    """Close every fixture connection, so the suite emits no ResourceWarning."""
    while _OPEN_STORES:
        try:
            _OPEN_STORES.pop().close()
        except sqlite3.Error:
            pass


def _bare():
    """An empty connection, registered so the module tears it down."""
    db = sqlite3.connect(":memory:")
    _OPEN_STORES.append(db)
    return db


def store():
    """An in-memory store carrying only the columns this module reads."""
    db = sqlite3.connect(":memory:")
    _OPEN_STORES.append(db)
    db.execute("CREATE TABLE conversations (id INTEGER PRIMARY KEY)")
    db.execute("""CREATE TABLE messages (
        id INTEGER PRIMARY KEY, conversation_id INTEGER NOT NULL,
        created_at TEXT NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL)""")
    db.execute(_SPINE_DDL)
    db.execute("INSERT INTO conversations(id) VALUES (1)")
    mc.migrate_compaction_v50(db, KEY, now="2026-09-04T10:00:00Z")
    return db


def make_record(*, conversation_id=1, seq=1, first=1, count=4, events=(), after=0,
                key=KEY, observed=None, span_has_proposal=0):
    """One CompactedSpan over ``count`` synthetic rows."""
    messages = []
    mid = first
    for index in range(count):
        role = "user" if index % 2 == 0 else "assistant"
        messages.append(row(mid, role, conversation_id=conversation_id,
                            content=f"turn {index} of the relay work. " * 3,
                            at=f"2026-09-04T09:{10 + index:02d}:00Z"))
        mid += 1
    span = mc.SpanBounds(
        conversation_id=conversation_id,
        first_message_id=messages[0].id,
        last_message_id=messages[-1].id,
        message_count=len(messages),
        source_chars=sum(m.chars for m in messages),
        last_created_at=messages[-1].created_at,
        span_has_proposal=span_has_proposal,
        message_ids=tuple(m.id for m in messages),
    )
    return messages, mc.build_compacted_span(
        span=span, messages=messages, events=list(events), after=after,
        seq=seq, key=key, observed=observed,
    )


def write_record(db, record, *, event_id=1, created_at="2026-09-04T10:00:00Z",
                 payload=None, kind=None):
    """Insert the milestone, the span and a matching spine receipt."""
    db.execute(
        "INSERT INTO memory_spine_events(id, created_at, kind, outcome, "
        "conversation_id, subject_kind, subject_id, payload_json) "
        "VALUES (?, ?, ?, 'applied', ?, 'conversation', ?, ?)",
        (event_id, created_at, kind or mc.COMPACTION_SPINE_KIND,
         record.conversation_id, record.conversation_id,
         json.dumps(payload if payload is not None
                    else record.spine_payload(at=created_at))),
    )
    milestone = record.milestone_row(created_at=created_at, spine_event_id=event_id)
    columns = ", ".join(milestone)
    marks = ", ".join("?" for _ in milestone)
    cursor = db.execute(
        f"INSERT INTO memory_milestones({columns}) VALUES ({marks})",
        tuple(milestone.values()),
    )
    milestone_id = int(cursor.lastrowid)
    span_row = record.span_row(milestone_id=milestone_id)
    db.execute(
        "INSERT INTO memory_compacted_spans(handle, milestone_id, conversation_id, "
        "body, body_chars) VALUES (:handle, :milestone_id, :conversation_id, "
        ":body, :body_chars)",
        span_row,
    )
    return milestone_id


# --- 1. the closed sets -----------------------------------------------------

class ClosedSetTests(unittest.TestCase):
    def test_the_six_rehydration_codes_are_the_design_set_in_decision_order(self):
        self.assertEqual(
            mc.REHYDRATION_ERROR_CODES,
            ("malformed_handle", "unknown_handle", "erased", "key_mismatch",
             "digest_mismatch", "store_unavailable"),
        )
        self.assertEqual(len(set(mc.REHYDRATION_ERROR_CODES)), 6)
        self.assertIs(mc.REHYDRATION_CODES, mc.REHYDRATION_ERROR_CODES)
        # key_mismatch is decided BEFORE digest_mismatch (H-7), and the tuple
        # order is the contract the other two owners read.
        self.assertLess(
            mc.REHYDRATION_ERROR_CODES.index("key_mismatch"),
            mc.REHYDRATION_ERROR_CODES.index("digest_mismatch"),
        )

    def test_the_two_budget_vocabularies_are_separate_constants(self):
        self.assertEqual(mc.READ_MODE_BUDGET_EXCEEDED, "budget-exceeded")
        self.assertEqual(mc.REFUSAL_BUDGET_EXCEEDED, "budget_exceeded")
        self.assertNotEqual(mc.READ_MODE_BUDGET_EXCEEDED, mc.REFUSAL_BUDGET_EXCEEDED)
        self.assertIn(mc.READ_MODE_BUDGET_EXCEEDED, mc.READ_MODES)
        self.assertNotIn(mc.READ_MODE_BUDGET_EXCEEDED, mc.COMPACTION_REFUSAL_CODES)
        self.assertIn(mc.REFUSAL_BUDGET_EXCEEDED, mc.COMPACTION_REFUSAL_CODES)
        self.assertNotIn(mc.REFUSAL_BUDGET_EXCEEDED, mc.READ_MODES)

    def test_the_refusal_and_read_mode_sets_are_the_design_sets(self):
        self.assertEqual(set(mc.READ_MODES), {
            "complete", "none", "partial", "budget-exceeded",
            "project-unavailable", "error",
        })
        self.assertEqual(set(mc.COMPACTION_REFUSAL_CODES), {
            "budget_exceeded", "compaction_downgrade_refused", "error",
            "key_unavailable", "model_author_not_supported", "schema_too_old",
            "span_busy", "spine_unverified", "stale_plan", "stale_span",
        })
        self.assertEqual(mc.BUSY_REASONS,
                         ("approval_pending", "job_active", "workflow_active"))
        self.assertEqual(mc.DERIVED_OUTCOMES, ("complete", "partial"))
        self.assertEqual(mc.COMPACTION_AUTHORS, ("runtime", "model"))

    def test_the_never_compacted_list_is_the_designs_nine_rules_as_data(self):
        self.assertEqual(len(mc.NEVER_COMPACTED), 9)
        self.assertEqual([entry.item for entry in mc.NEVER_COMPACTED],
                         list(range(1, 10)))
        for entry in mc.NEVER_COMPACTED:
            self.assertTrue(entry.what and entry.why)
        proposals = mc.NEVER_COMPACTED[4]
        self.assertIn("memory_fact_proposals", proposals.tables)
        self.assertIn("approvals", mc.NEVER_COMPACTED[8].tables)

    def test_half_a_exports_no_configuration_key(self):
        # Boss ruling: no JARVIS_COMPACTION_* surface in half A.  Asserted as
        # an absence in the exports AND as a presence of the constants that
        # replace them, so the test cannot pass by the module being empty.
        exported = {name: getattr(mc, name) for name in mc.__all__}
        for name, value in exported.items():
            self.assertNotIn("JARVIS_COMPACTION", str(value),
                             f"{name} names a configuration key")
        self.assertFalse(hasattr(mc, "COMPACTION_CONFIG_KEYS"))
        self.assertEqual(mc.DEFAULT_KEEP_TURNS, 12)
        self.assertEqual(mc.DEFAULT_MIN_SPAN_CHARS, 12_000)
        self.assertEqual(mc.DEFAULT_MAX_SPAN_CHARS, 200_000)
        self.assertEqual(mc.MAX_SPAN_MESSAGES, 400)
        self.assertEqual(mc.COMPACTED_HISTORY_LIMIT, 2_400)
        self.assertEqual(mc.MILESTONE_TOMBSTONE_MAX_IDS, 128)

    def test_every_name_in_dunder_all_resolves(self):
        for name in mc.__all__:
            self.assertTrue(hasattr(mc, name), name)
        self.assertEqual(len(mc.__all__), len(set(mc.__all__)))

    def test_resolved_ambiguities_are_published_as_data(self):
        topics = {topic for topic, _answer in mc.RESOLVED_AMBIGUITIES}
        self.assertIn("span_has_proposal", topics)
        self.assertIn("screened", topics)
        for _topic, answer in mc.RESOLVED_AMBIGUITIES:
            self.assertGreater(len(answer), 40)

    def test_the_payload_key_sets_agree_with_the_spine_in_both_directions(self):
        # The spine half has not landed yet, so today the kind is unknown and
        # payload_keys refuses it.  Both branches assert; when the kind lands
        # this test starts enforcing equality without being edited.
        if mc.COMPACTION_SPINE_KIND not in spine.SPINE_KINDS:
            with self.assertRaises(spine.SpineError):
                spine.payload_keys(mc.COMPACTION_SPINE_KIND)
        else:
            required, allowed = spine.payload_keys(mc.COMPACTION_SPINE_KIND)
            self.assertEqual(required, mc.COMPACTED_REQUIRED_KEYS)
            self.assertEqual(allowed, mc.COMPACTED_PAYLOAD_KEYS)
        if "conversation.deleted" in spine.UNCONSTRAINED_PAYLOAD_KINDS:
            with self.assertRaises(spine.SpineError):
                spine.payload_keys("conversation.deleted")
        else:
            required, allowed = spine.payload_keys("conversation.deleted")
            self.assertEqual(required, mc.CONVERSATION_DELETED_REQUIRED_KEYS)
            self.assertEqual(allowed, mc.CONVERSATION_DELETED_PAYLOAD_KEYS)

    def test_the_compacted_key_sets_are_the_designs(self):
        self.assertEqual(len(mc.COMPACTED_REQUIRED_KEYS), 16)
        self.assertEqual(len(mc.COMPACTED_PAYLOAD_KEYS), 21)
        self.assertTrue(mc.COMPACTED_REQUIRED_KEYS < mc.COMPACTED_PAYLOAD_KEYS)
        self.assertEqual(
            mc.COMPACTED_PAYLOAD_KEYS - mc.COMPACTED_REQUIRED_KEYS,
            {"at", "model", "reduction_ratio", "excluded_by_screen",
             "span_has_proposal"},
        )
        # "at" stays OPTIONAL on conversation.deleted: the live writer emits
        # {"messages_removed": N} with no "at" today.
        self.assertEqual(mc.CONVERSATION_DELETED_REQUIRED_KEYS, {"messages_removed"})
        self.assertIn("at", mc.CONVERSATION_DELETED_PAYLOAD_KEYS)
        self.assertNotIn("at", mc.CONVERSATION_DELETED_REQUIRED_KEYS)
        # No payload key may be shaped like a secret (the spine's own rule).
        for name in mc.COMPACTED_PAYLOAD_KEYS | mc.CONVERSATION_DELETED_PAYLOAD_KEYS:
            self.assertNotRegex(name, r"token|code|secret|password|credential")

    def test_the_problem_kind_set_is_closed_and_sorted(self):
        self.assertEqual(len(mc.COMPACTION_PROBLEM_KINDS), 18)
        self.assertEqual(list(mc.COMPACTION_PROBLEM_KINDS),
                         sorted(mc.COMPACTION_PROBLEM_KINDS))
        self.assertEqual(len(set(mc.COMPACTION_PROBLEM_KINDS)), 18)


# --- 2. message rows and turn structure -------------------------------------

class MessageRowTests(unittest.TestCase):
    def test_chars_comes_from_content_when_content_is_given(self):
        with_content = row(1, content="abcde")
        self.assertEqual(with_content.chars, 5)
        self.assertTrue(with_content.has_content)
        metadata_only = row(1, chars=77)
        self.assertEqual(metadata_only.chars, 77)
        self.assertFalse(metadata_only.has_content)

    def test_a_row_needs_content_or_a_chars_count(self):
        with self.assertRaises(ValueError):
            mc.MessageRow(id=1, conversation_id=1, created_at="x", role="user")

    def test_only_persisted_roles_are_accepted(self):
        for role in ("user", "assistant"):
            self.assertEqual(row(1, role).role, role)
        for role in ("tool", "system", ""):
            with self.assertRaises(ValueError):
                row(1, role)


class TurnTests(unittest.TestCase):
    def test_a_turn_is_a_user_row_plus_the_assistant_rows_after_it(self):
        rows = [row(1, "user"), row(2, "assistant"), row(3, "assistant"),
                row(4, "user"), row(5, "assistant")]
        segments, first = tuple(mc.segment_turns(rows))
        self.assertEqual(first, 0)
        self.assertEqual([(t.start, t.end, t.complete) for t in segments],
                         [(0, 2, True), (3, 4, True)])

    def test_a_trailing_user_row_is_an_incomplete_turn(self):
        rows = [row(1, "user"), row(2, "assistant"), row(3, "user")]
        segments, _first = mc.segment_turns(rows)
        self.assertEqual([t.complete for t in segments], [True, False])
        # ...and is never compacted, even at keep_turns 0.
        self.assertEqual(mc.keep_boundary(rows, keep_turns=0), 2)

    def test_rows_before_the_first_user_row_are_not_a_turn(self):
        rows = [row(1, "assistant"), row(2, "assistant"), row(3, "user"),
                row(4, "assistant")]
        segments, first = mc.segment_turns(rows)
        self.assertEqual(first, 2)
        self.assertEqual(len(segments), 1)
        # They are candidate whenever anything is: with every turn protected
        # the boundary is the first turn's start, not the end of the list.
        self.assertEqual(mc.keep_boundary(rows, keep_turns=5), 2)

    def test_keep_boundary_keeps_exactly_the_last_n_complete_turns(self):
        rows = turns(6)
        self.assertEqual(mc.keep_boundary(rows, keep_turns=2), 8)
        self.assertEqual(mc.keep_boundary(rows, keep_turns=6), 0)
        self.assertEqual(mc.keep_boundary(rows, keep_turns=99), 0)
        self.assertEqual(mc.keep_boundary(rows, keep_turns=0), len(rows))
        self.assertEqual(mc.segment_turns([])[0], ())
        self.assertEqual(mc.keep_boundary([], keep_turns=0), 0)

    def test_a_negative_keep_turns_is_refused(self):
        with self.assertRaises(ValueError):
            mc.keep_boundary(turns(2), keep_turns=-1)


# --- 3. span selection ------------------------------------------------------

class PlanTests(unittest.TestCase):
    def test_a_row_from_another_conversation_raises_rather_than_being_filtered(self):
        rows = turns(4) + [row(99, "user", conversation_id=2)]
        with self.assertRaises(mc.CompactionError) as caught:
            mc.plan_spans(1, rows, keep_turns=0, min_span_chars=1)
        self.assertEqual(caught.exception.code, "cross_conversation")
        # ...and the same rows all in conversation 1 plan cleanly.
        plan = mc.plan_spans(1, turns(4), keep_turns=0, min_span_chars=1)
        self.assertEqual(len(plan.spans), 1)

    def test_out_of_order_ids_raise(self):
        rows = [row(5, "user"), row(2, "assistant")]
        with self.assertRaises(mc.CompactionError) as caught:
            mc.plan_spans(1, rows, keep_turns=0, min_span_chars=1)
        self.assertEqual(caught.exception.code, "unordered_messages")

    def test_a_busy_conversation_refuses_span_busy_and_an_idle_one_does_not(self):
        rows = turns(6)
        busy = mc.plan_spans(1, rows, keep_turns=0, min_span_chars=1,
                             busy_reason="job_active")
        self.assertEqual(busy.refusal, "span_busy")
        self.assertEqual(busy.refusal_detail, "job_active")
        self.assertEqual(busy.spans, ())
        # The candidate accounting is still reported, so an operator sees what
        # would have happened.
        self.assertEqual(busy.candidate_rows, 12)
        idle = mc.plan_spans(1, rows, keep_turns=0, min_span_chars=1)
        self.assertIsNone(idle.refusal)
        self.assertEqual(len(idle.spans), 1)

    def test_an_unknown_busy_reason_is_refused(self):
        with self.assertRaises(ValueError):
            mc.plan_spans(1, turns(2), busy_reason="on_holiday")

    def test_configuration_bounds_are_validated(self):
        rows = turns(2)
        for kwargs in (
            {"keep_turns": -1},
            {"min_span_chars": 0},
            {"min_span_chars": 100, "max_span_chars": 10},
            {"max_span_messages": 0},
        ):
            with self.assertRaises(ValueError):
                mc.plan_spans(1, rows, **kwargs)

    def test_a_conversation_with_nothing_old_enough_plans_nothing(self):
        plan = mc.plan_spans(1, turns(3), keep_turns=12)
        self.assertEqual(plan.spans, ())
        self.assertEqual(plan.skipped, ())
        self.assertEqual(plan.candidate_rows, 0)
        self.assertIsNone(plan.candidate_first_id)
        self.assertIsNone(plan.candidate_last_id)
        self.assertEqual(plan.eligible_rows, 0)
        self.assertEqual(plan.eligible_chars, 0)
        self.assertEqual(mc.partition_spans([]), ())

    def test_a_sub_region_below_the_minimum_is_skipped_not_lost(self):
        rows = turns(4, chars=100)
        plan = mc.plan_spans(1, rows, keep_turns=0, min_span_chars=10_000)
        self.assertEqual(plan.spans, ())
        self.assertEqual(len(plan.skipped), 1)
        self.assertEqual(plan.skipped[0].reason, "below_min_span_chars")
        self.assertEqual(plan.skipped[0].source_chars, 800)
        # The same rows above the threshold are eligible.
        generous = mc.plan_spans(1, rows, keep_turns=0, min_span_chars=800)
        self.assertEqual(len(generous.spans), 1)
        self.assertEqual(generous.skipped, ())

    def test_the_region_partitions_around_a_proposal_and_the_row_stays_live(self):
        rows = turns(6, chars=100)
        held = rows[4].id
        plan = mc.plan_spans(1, rows, keep_turns=0, min_span_chars=1,
                             proposal_message_ids=[held])
        self.assertEqual(len(plan.spans), 2)
        self.assertEqual(plan.held_back_message_ids, (held,))
        self.assertEqual(plan.held_back_chars, 100)
        covered = [mid for span in plan.spans for mid in span.message_ids]
        self.assertNotIn(held, covered)
        self.assertEqual(len(covered), 11)
        # Both sub-regions border the held-back row, so both report 1.
        self.assertEqual([span.span_has_proposal for span in plan.spans], [1, 1])
        # With no proposal the same rows are one span reporting 0.
        clean = mc.plan_spans(1, rows, keep_turns=0, min_span_chars=1)
        self.assertEqual(len(clean.spans), 1)
        self.assertEqual(clean.spans[0].span_has_proposal, 0)

    def test_a_proposal_at_the_edge_borders_only_one_sub_region(self):
        rows = turns(4, chars=100)
        plan = mc.plan_spans(1, rows, keep_turns=0, min_span_chars=1,
                             proposal_message_ids=[rows[0].id])
        self.assertEqual(len(plan.spans), 1)
        self.assertEqual(plan.spans[0].span_has_proposal, 1)
        self.assertEqual(plan.spans[0].first_message_id, rows[1].id)

    def test_a_long_region_splits_at_the_char_cap_rather_than_being_refused(self):
        rows = turns(10, chars=1_000)
        plan = mc.plan_spans(1, rows, keep_turns=0, min_span_chars=1,
                             max_span_chars=5_000)
        self.assertGreater(len(plan.spans), 1)
        for span in plan.spans:
            self.assertLessEqual(span.source_chars, 5_000)
        self.assertEqual(sum(span.message_count for span in plan.spans), 20)
        # Contiguous and ordered: no row is lost or duplicated at a split.
        covered = [mid for span in plan.spans for mid in span.message_ids]
        self.assertEqual(covered, [r.id for r in rows])
        # A split boundary is not a proposal boundary.
        self.assertEqual({span.span_has_proposal for span in plan.spans}, {0})

    def test_a_long_region_splits_at_the_message_cap(self):
        rows = turns(10, chars=10)
        plan = mc.plan_spans(1, rows, keep_turns=0, min_span_chars=1,
                             max_span_messages=6)
        self.assertEqual([span.message_count for span in plan.spans], [6, 6, 6, 2])

    def test_a_single_row_over_the_cap_is_emitted_alone(self):
        rows = [row(1, "user", chars=9_000), row(2, "assistant", chars=10)]
        plan = mc.plan_spans(1, rows, keep_turns=0, min_span_chars=1,
                             max_span_chars=1_000)
        self.assertEqual([span.message_count for span in plan.spans], [1, 1])
        self.assertEqual(plan.spans[0].source_chars, 9_000)

    def test_partition_spans_returns_ineligible_regions_with_a_reason(self):
        rows = turns(6, chars=100)
        regions = mc.partition_spans(rows, held_back_ids=[rows[4].id],
                                     min_span_chars=500)
        self.assertEqual([region.eligible for region in regions], [False, True])
        self.assertEqual([region.reason for region in regions],
                         ["below_min_span_chars", None])
        self.assertEqual(regions[0].count, 4)
        self.assertEqual(regions[0].source_chars, 400)
        self.assertEqual(regions[0].conversation_id, 1)
        self.assertEqual((regions[0].first_id, regions[0].last_id), (1, 4))
        self.assertEqual(len(regions[0].messages), 4)
        generous = mc.partition_spans(rows, min_span_chars=1)
        self.assertEqual([region.eligible for region in generous], [True])
        self.assertIsNone(generous[0].reason)

    def test_a_span_carries_its_conversation_into_every_range_statement(self):
        span = bounds(conversation_id=7, first_message_id=100, last_message_id=200)
        sql, params = span.range_predicate()
        self.assertIn("conversation_id = ?", sql)
        self.assertIn("id BETWEEN ? AND ?", sql)
        self.assertEqual(params, (7, 100, 200))
        self.assertTrue(span.covers(150))
        self.assertFalse(span.covers(201))


# --- 4. the design 2.13 worked example, executable --------------------------

class WorkedExampleTests(unittest.TestCase):
    """Design 2.13: conversation 41, two sub-regions in one pass, per
    sub-region watermarks.  Every number below is the design's."""

    def rows(self):
        # 812..1073 arranged so 944 and 1049 are assistant rows and the last
        # twelve complete turns are exactly 1050..1073.
        roles = {}
        for mid in range(812, 944):
            roles[mid] = "user" if mid % 2 == 0 else "assistant"
        roles[944] = "assistant"
        for mid in range(945, 1047):
            roles[mid] = "user" if mid % 2 == 1 else "assistant"
        roles[1047] = "assistant"
        roles[1048] = "user"
        roles[1049] = "assistant"
        for mid in range(1050, 1074):
            roles[mid] = "user" if mid % 2 == 0 else "assistant"

        def spread(ids, total):
            base = total // len(ids)
            sizes = {mid: base for mid in ids}
            sizes[ids[0]] += total - base * len(ids)
            return sizes

        sizes = {}
        sizes.update(spread(list(range(812, 944)), 22_940))
        sizes[944] = 306
        sizes.update(spread(list(range(945, 1050)), 18_240))
        for mid in range(1050, 1074):
            sizes[mid] = 200
        fixed = {943: "2026-09-04T09:41:06Z", 1049: "2026-09-04T09:58:44Z"}
        out = []
        for index, mid in enumerate(range(812, 1074)):
            out.append(mc.MessageRow(
                id=mid, conversation_id=41,
                created_at=fixed.get(mid, f"2026-09-04T09:{index % 40:02d}:00Z"),
                role=roles[mid], content="x" * sizes[mid],
            ))
        return out

    def events(self):
        """Conversation 41's events: eight in (2180, 2195], nine in
        (2195, 2211], and the pass's own receipt at 2214, unclaimed."""
        keys = ("project:1|kestrel relay|maintainer",
                "project:1|kestrel relay|listen port")
        first = [
            event(2181, "claim.created", at="2026-09-04T09:22:14Z",
                  conversation_id=41, subject_kind="claim", subject_id=1196,
                  payload={"claim_key": keys[0], "claim_id": 1196}),
            event(2182, "claim.created", at="2026-09-04T09:23:00Z",
                  conversation_id=41, subject_kind="claim", subject_id=1197,
                  payload={"claim_key": keys[1], "claim_id": 1197}),
            event(2183, "claim.reasserted", at="2026-09-04T09:24:00Z",
                  conversation_id=41, subject_kind="claim", subject_id=1196,
                  payload={"claim_key": keys[0], "claim_id": 1196}),
            event(2184, "claim.reasserted", at="2026-09-04T09:25:00Z",
                  conversation_id=41, subject_kind="claim", subject_id=1197,
                  payload={"claim_key": keys[1], "claim_id": 1197}),
            event(2190, "memory.reasserted", at="2026-09-04T09:30:00Z",
                  conversation_id=41, subject_kind="memory", subject_id=86,
                  payload={}),
            event(2191, "memory.reasserted", at="2026-09-04T09:31:00Z",
                  conversation_id=41, subject_kind="memory", subject_id=86,
                  payload={}),
            event(2193, "proposal.confirmed", at="2026-09-04T09:35:00Z",
                  conversation_id=41, payload={"claim_key": keys[0]}),
            event(2195, "memory.created", at="2026-09-04T09:40:55Z",
                  conversation_id=41, subject_kind="memory", subject_id=86,
                  payload={}),
        ]
        second_keys = ("project:1|kestrel relay|listen port",
                       "project:1|kestrel relay|deployed on host")
        second = [
            event(2196, "claim.created", at="2026-09-04T09:42:10Z",
                  conversation_id=41, subject_kind="claim", subject_id=1204,
                  payload={"claim_key": second_keys[0], "claim_id": 1204}),
            event(2197, "claim.created", at="2026-09-04T09:43:00Z",
                  conversation_id=41, subject_kind="claim", subject_id=1205,
                  payload={"claim_key": second_keys[1], "claim_id": 1205}),
            event(2198, "claim.superseded", at="2026-09-04T09:44:00Z",
                  conversation_id=41, subject_kind="claim", subject_id=1180,
                  payload={"claim_key": second_keys[0], "claim_id": 1180,
                           "related_claim_id": 1204}),
            event(2199, "claim.reasserted", at="2026-09-04T09:45:00Z",
                  conversation_id=41, subject_kind="claim", subject_id=1204,
                  payload={"claim_key": second_keys[0], "claim_id": 1204}),
            event(2200, "claim.reasserted", at="2026-09-04T09:46:00Z",
                  conversation_id=41, subject_kind="claim", subject_id=1205,
                  payload={"claim_key": second_keys[1], "claim_id": 1205}),
            event(2201, "proposal.confirmed", at="2026-09-04T09:47:00Z",
                  conversation_id=41, payload={"claim_key": second_keys[0]}),
            event(2202, "proposal.not_stored", at="2026-09-04T09:48:00Z",
                  conversation_id=41, outcome="noop", payload={"variant": "readonly"}),
            event(2203, "proposal.not_stored", at="2026-09-04T09:49:00Z",
                  conversation_id=41, outcome="noop", payload={"variant": "readonly"}),
            event(2211, "memory.created", at="2026-09-04T09:58:40Z",
                  conversation_id=41, subject_kind="memory", subject_id=88,
                  payload={}),
        ]
        tail = [event(2214, mc.COMPACTION_SPINE_KIND, at="2026-09-04T10:02:41Z",
                      conversation_id=41, subject_kind="conversation",
                      subject_id=41, payload={})]
        return first + second + tail

    def test_the_pass_partitions_into_the_designs_two_sub_regions(self):
        plan = mc.plan_spans(41, self.rows(), proposal_message_ids=[944],
                             keep_turns=12)
        self.assertEqual(plan.candidate_rows, 238)
        self.assertEqual(plan.candidate_chars, 41_486)
        self.assertEqual((plan.candidate_first_id, plan.candidate_last_id),
                         (812, 1049))
        self.assertEqual(plan.held_back_message_ids, (944,))
        self.assertEqual(plan.held_back_chars, 306)
        self.assertEqual(plan.eligible_rows, 237)
        self.assertEqual(plan.eligible_chars, 41_180)
        self.assertEqual(
            [(s.first_message_id, s.last_message_id, s.message_count,
              s.source_chars, s.span_has_proposal) for s in plan.spans],
            [(812, 943, 132, 22_940, 1), (945, 1049, 105, 18_240, 1)],
        )
        self.assertEqual(plan.protected_rows, 24)

    def test_the_two_sub_regions_get_disjoint_contiguous_watermarks(self):
        plan = mc.plan_spans(41, self.rows(), proposal_message_ids=[944],
                             keep_turns=12)
        events = self.events()
        first = mc.build_invariants(span=plan.spans[0], events=events, after=2180)
        second = mc.build_invariants(
            span=plan.spans[1], events=events,
            after=first["derived"]["event_range"]["through"],
            observed={"tools_used": ["read_file"],
                      "files_touched": ["workspace/relay-notes.md"]},
        )
        self.assertEqual(first["derived"]["event_range"],
                         {"after": 2180, "through": 2195})
        self.assertEqual(second["derived"]["event_range"],
                         {"after": 2195, "through": 2211})
        # Contiguous, disjoint, and neither empty.
        self.assertEqual(first["derived"]["event_range"]["through"],
                         second["derived"]["event_range"]["after"])
        self.assertEqual(first["derived"]["event_count"], 8)
        self.assertEqual(second["derived"]["event_count"], 9)
        # The pass's own receipt at 2214 is past every watermark written in
        # this pass, so it counts in a LATER milestone, never its own.
        self.assertGreater(2214, second["derived"]["event_range"]["through"])

    def test_the_designs_derived_values_are_reproduced(self):
        plan = mc.plan_spans(41, self.rows(), proposal_message_ids=[944],
                             keep_turns=12)
        events = self.events()
        first = mc.build_invariants(span=plan.spans[0], events=events,
                                    after=2180)["derived"]
        second = mc.build_invariants(
            span=plan.spans[1], events=events, after=2195,
            observed={"tools_used": ["read_file"],
                      "files_touched": ["workspace/relay-notes.md"]},
        )
        derived = second["derived"]
        self.assertEqual(first["claims_created"], [1196, 1197])
        self.assertEqual(first["memories_created"], [86])
        self.assertEqual(first["proposals_confirmed"], 1)
        self.assertEqual(first["proposals_not_stored"], 0)
        self.assertEqual(first["message_ids"],
                         {"first": 812, "last": 943, "count": 132})
        self.assertEqual(first["event_first_at"], "2026-09-04T09:22:14Z")
        self.assertEqual(first["event_last_at"], "2026-09-04T09:40:55Z")
        self.assertEqual(first["outcome"], "complete")
        self.assertFalse(first["screened"])
        self.assertEqual(first["excluded_by_screen"], 0)
        self.assertEqual(derived["claims_created"], [1204, 1205])
        self.assertEqual(derived["claims_superseded"], [[1180, 1204]])
        self.assertEqual(derived["claims_retracted"], [])
        self.assertEqual(derived["claims_tombstoned"], [])
        self.assertEqual(derived["claim_keys"],
                         ["project:1|kestrel relay|deployed on host",
                          "project:1|kestrel relay|listen port"])
        self.assertEqual(derived["memories_created"], [88])
        self.assertEqual(derived["lessons_created"], [])
        self.assertEqual(derived["proposals_confirmed"], 1)
        self.assertEqual(derived["proposals_not_stored"], 2)
        self.assertEqual(derived["message_ids"],
                         {"first": 945, "last": 1049, "count": 105})
        self.assertEqual(derived["event_first_at"], "2026-09-04T09:42:10Z")
        self.assertEqual(derived["event_last_at"], "2026-09-04T09:58:40Z")
        self.assertEqual(derived["span_has_proposal"], 1)
        self.assertEqual(second["observed"],
                         {"tools_used": ["read_file"],
                          "files_touched": ["workspace/relay-notes.md"]})

    def test_the_pass_round_trips_byte_exact_and_the_digests_are_coherent(self):
        rows = self.rows()
        plan = mc.plan_spans(41, rows, proposal_message_ids=[944], keep_turns=12)
        events = self.events()
        second = [r for r in rows if 945 <= r.id <= 1049]
        record = mc.build_compacted_span(
            span=plan.spans[1], messages=second, events=events, after=2195,
            seq=4, key=KEY,
        )
        self.assertEqual(record.handle,
                         f"mem:span/41/4/{record.span_unkeyed_sha256[:12]}")
        self.assertNotEqual(record.span_sha256, record.span_unkeyed_sha256)
        self.assertNotIn(record.span_sha256[:12], record.handle)
        back = mc.rehydrate_span(
            record.handle,
            milestone=record.milestone_row(created_at="now", spine_event_id=2214),
            body=record.body, key=KEY,
        )
        self.assertEqual([m["id"] for m in back["messages"]],
                         [r.id for r in second])
        self.assertEqual([m["content"] for m in back["messages"]],
                         [r.content for r in second])
        self.assertEqual([m["created_at"] for m in back["messages"]],
                         [r.created_at for r in second])
        self.assertEqual(back["source_chars"], 18_240)
        # Compaction is worth doing on this shape.
        self.assertLess(record.reduction_ratio, 0.25)


# --- 5. canonical span, digests, bytes --------------------------------------

class CanonicalSpanTests(unittest.TestCase):
    def test_the_canonical_form_is_the_designs_and_is_stable(self):
        rows = [row(1, "user", content="hello"), row(2, "assistant", content="hi")]
        text = mc.canonical_span(1, rows)
        self.assertEqual(json.loads(text), {
            "v": 1, "conversation_id": 1,
            "messages": [
                {"id": 1, "created_at": rows[0].created_at, "role": "user",
                 "content": "hello"},
                {"id": 2, "created_at": rows[1].created_at, "role": "assistant",
                 "content": "hi"},
            ],
        })
        self.assertEqual(text, spine.canonical(json.loads(text)))
        self.assertEqual(text, mc.canonical_span(1, rows))

    def test_a_foreign_row_or_a_metadata_row_cannot_enter_a_span(self):
        with self.assertRaises(mc.CompactionError) as foreign:
            mc.canonical_span(1, [row(1, content="x", conversation_id=2)])
        self.assertEqual(foreign.exception.code, "cross_conversation")
        with self.assertRaises(mc.CompactionError) as bare:
            mc.canonical_span(1, [row(1, chars=10)])
        self.assertEqual(bare.exception.code, "error")

    def test_the_two_digests_differ_and_only_the_unkeyed_one_is_printable(self):
        text = mc.canonical_span(1, [row(1, content="hello")])
        digests = mc.span_digests(KEY, text)
        self.assertEqual(digests.keyed, mc.keyed_span_sha256(KEY, text))
        self.assertEqual(digests.unkeyed, mc.unkeyed_span_sha256(text))
        self.assertEqual(digests.unkeyed,
                         hashlib.sha256(text.encode("utf-8")).hexdigest())
        self.assertNotEqual(digests.keyed, digests.unkeyed)
        # The keyed one moves with the key; the identity one does not.
        self.assertNotEqual(mc.span_digests(OTHER_KEY, text).keyed, digests.keyed)
        self.assertEqual(mc.span_digests(OTHER_KEY, text).unkeyed, digests.unkeyed)
        keyed, unkeyed = digests
        self.assertEqual((keyed, unkeyed), (digests.keyed, digests.unkeyed))

    def test_compression_round_trips_and_is_smaller_on_prose(self):
        text = mc.canonical_span(1, [row(1, content="the relay is up. " * 200)])
        body = mc.compress_span(text)
        self.assertEqual(mc.decompress_span(body), text)
        self.assertLess(len(body), len(text))
        with self.assertRaises(zlib.error):
            mc.decompress_span(b"not zlib at all")


# --- 6. handles -------------------------------------------------------------

class HandleTests(unittest.TestCase):
    def test_a_handle_is_the_designs_format(self):
        digest = "9a5c78388964" + "0" * 52
        self.assertEqual(mc.handle_for(41, 3, digest), "mem:span/41/3/9a5c78388964")
        parsed = mc.parse_handle("mem:span/41/3/9a5c78388964")
        self.assertEqual((parsed.conversation_id, parsed.seq, parsed.digest_prefix),
                         (41, 3, "9a5c78388964"))

    def test_handle_construction_validates_its_inputs(self):
        digest = "a" * 64
        for conversation_id, seq in ((-1, 1), (1, 0)):
            with self.assertRaises(ValueError):
                mc.handle_for(conversation_id, seq, digest)
        for bad in ("A" * 64, "a" * 63, "zz" + "a" * 62):
            with self.assertRaises(ValueError):
                mc.handle_for(1, 1, bad)

    def test_a_unicode_confusable_handle_does_not_parse(self):
        # U+0663 is ARABIC-INDIC DIGIT THREE.  Python's \d matches it, which
        # is why the pattern uses [0-9] with re.ASCII.
        confusable = "mem:span/41/\u0663/9a5c78388964"
        with self.assertRaises(mc.RehydrationError) as caught:
            mc.parse_handle(confusable)
        self.assertEqual(caught.exception.code, "malformed_handle")
        self.assertIsNone(mc.try_parse_handle(confusable))
        # The ASCII spelling of the same handle parses.
        self.assertIsNotNone(mc.try_parse_handle("mem:span/41/3/9a5c78388964"))

    def test_every_malformed_shape_is_refused(self):
        for bad in (
            "", "mem:span/41/3", "mem:span/41/3/9A5C78388964",
            "mem:span/41/3/9a5c7838896", "mem:span/41/3/9a5c783889645",
            "mem:span//3/9a5c78388964", "mem:span/41/0003/9a5c78388964x",
            " mem:span/41/3/9a5c78388964", "mem:span/41/3/9a5c78388964 ",
            "MEM:SPAN/41/3/9a5c78388964", 17, None,
        ):
            with self.assertRaises(mc.RehydrationError) as caught:
                mc.parse_handle(bad)
            self.assertEqual(caught.exception.code, "malformed_handle")

    def test_handle_matches_agrees_and_disagrees_for_the_right_reasons(self):
        digest = "9a5c78388964" + "0" * 52
        handle = mc.handle_for(41, 3, digest)
        self.assertTrue(mc.handle_matches(handle, conversation_id=41, seq=3,
                                          span_unkeyed_sha256=digest))
        self.assertFalse(mc.handle_matches(handle, conversation_id=42, seq=3,
                                           span_unkeyed_sha256=digest))
        self.assertFalse(mc.handle_matches(handle, conversation_id=41, seq=4,
                                           span_unkeyed_sha256=digest))
        self.assertFalse(mc.handle_matches(handle, conversation_id=41, seq=3,
                                           span_unkeyed_sha256="b" * 64))
        self.assertFalse(mc.handle_matches("nonsense", conversation_id=41, seq=3,
                                           span_unkeyed_sha256=digest))


# --- 7. rehydration: all five pure codes, and success -----------------------

class RehydrationTests(unittest.TestCase):
    def setUp(self):
        self.messages, self.record = make_record(count=4)
        self.milestone = self.record.milestone_row(
            created_at="2026-09-04T10:00:00Z", spine_event_id=1)

    def rehydrate(self, **changes):
        arguments = {"milestone": self.milestone, "body": self.record.body,
                     "key": KEY}
        arguments.update(changes)
        handle = arguments.pop("handle", self.record.handle)
        return mc.rehydrate_span(handle, **arguments)

    def code_of(self, **changes):
        with self.assertRaises(mc.RehydrationError) as caught:
            self.rehydrate(**changes)
        return caught.exception.code

    def test_a_good_handle_returns_the_original_rows(self):
        result = self.rehydrate()
        self.assertEqual(result["conversation_id"], 1)
        self.assertEqual(result["seq"], 1)
        self.assertEqual(result["message_count"], 4)
        self.assertEqual(result["first_message_id"], self.messages[0].id)
        self.assertEqual(result["last_message_id"], self.messages[-1].id)
        self.assertEqual([m["content"] for m in result["messages"]],
                         [m.content for m in self.messages])

    def test_all_five_store_free_codes_are_reachable_and_distinct(self):
        seen = {
            self.code_of(handle="not-a-handle"),
            self.code_of(milestone=None),
            self.code_of(body=None),
            self.code_of(key=OTHER_KEY),
            self.code_of(body=mc.compress_span("tampered")),
        }
        self.assertEqual(seen, {"malformed_handle", "unknown_handle", "erased",
                                "key_mismatch", "digest_mismatch"})
        for code in seen:
            self.assertIn(code, mc.REHYDRATION_ERROR_CODES)

    def test_a_milestone_naming_another_span_is_unknown_not_a_scope_error(self):
        for change in ({"conversation_id": 99}, {"seq": 99},
                       {"span_unkeyed_sha256": "f" * 64}):
            milestone = dict(self.milestone)
            milestone.update(change)
            self.assertEqual(self.code_of(milestone=milestone), "unknown_handle")

    def test_key_loss_is_decided_before_tampering(self):
        # Both a swapped key AND a corrupt body: the answer must be
        # key_mismatch, because a wrong key cannot judge a digest.
        milestone = dict(self.milestone)
        milestone["key_fingerprint"] = spine.key_fingerprint(OTHER_KEY)
        self.assertEqual(self.code_of(milestone=milestone,
                                      body=mc.compress_span("tampered")),
                         "key_mismatch")
        # With the key right, the same corrupt body is tampering.
        self.assertEqual(self.code_of(body=mc.compress_span("tampered")),
                         "digest_mismatch")

    def test_an_unreadable_body_fails_closed_as_tampering(self):
        self.assertEqual(self.code_of(body=b"\x00 not zlib"), "digest_mismatch")
        self.assertEqual(self.code_of(body=zlib.compress(b"\xff\xfe not utf8")),
                         "digest_mismatch")

    def test_a_body_that_digests_correctly_but_is_not_a_span_is_refused(self):
        # The digest is recomputed over the planted text, so this reaches the
        # JSON check rather than stopping at the digest.
        for planted in ("not json at all", json.dumps({"messages": []}),
                        json.dumps({"conversation_id": 99, "messages": []})):
            milestone = dict(self.milestone)
            milestone["span_sha256"] = mc.keyed_span_sha256(KEY, planted)
            self.assertEqual(
                self.code_of(milestone=milestone, body=mc.compress_span(planted)),
                "digest_mismatch",
            )

    def test_a_single_flipped_byte_is_detected(self):
        damaged = bytearray(mc.compress_span(self.record.canonical))
        damaged[-3] ^= 0x01
        with self.assertRaises(mc.RehydrationError) as caught:
            self.rehydrate(body=bytes(damaged))
        self.assertEqual(caught.exception.code, "digest_mismatch")


# --- 8. screens -------------------------------------------------------------

class ScreenTests(unittest.TestCase):
    def test_a_long_clean_span_is_not_screened_although_screen_endpoint_says_it_is(self):
        from jarvis.redaction import screen_endpoint
        clean = "the relay is up and the port moved. " * 200
        # The published measurement: the endpoint screen calls ANY value past
        # 512 characters long_value, which is right for a claim endpoint and
        # would make `screened` true for every span.
        self.assertEqual(screen_endpoint(clean), (True, "long_value"))
        self.assertEqual(mc.screen_span_text(clean), (False, None))

    def test_an_identifier_anywhere_in_a_long_span_is_found_and_named(self):
        from jarvis.redaction import screen_endpoint
        buried = "x" * 600 + " contact ada@example.com now " + "y" * 600
        screened, reason = mc.screen_span_text(buried)
        self.assertTrue(screened)
        self.assertEqual(reason, "email")
        # The whole-text screen can only say long_value here; the windowed one
        # names the actual kind.
        self.assertEqual(screen_endpoint(buried)[1], "long_value")

    def test_an_identifier_straddling_a_window_boundary_is_still_found(self):
        window = mc.SPAN_SCREEN_WINDOW
        for offset in range(-12, 13, 4):
            text = ("a" * (window + offset)) + "ada@example.com" + ("b" * 600)
            self.assertTrue(mc.screen_span_text(text)[0], offset)

    def test_an_empty_or_short_clean_text_is_not_screened(self):
        self.assertEqual(mc.screen_span_text(""), (False, None))
        self.assertEqual(mc.screen_span_text("the operator scoped the work"),
                         (False, None))

    def test_screen_entries_keeps_clean_values_and_counts_the_drops(self):
        kept, excluded = mc.screen_entries([
            "project:1|kestrel relay|listen port",
            "project:1|kestrel relay|listen port",
            "project:1|ada@example.com|owner",
            "",
            None,
            "project:1|kestrel relay|maintainer",
        ])
        self.assertEqual(kept, ("project:1|kestrel relay|listen port",
                                "project:1|kestrel relay|maintainer"))
        self.assertEqual(excluded, 3)
        self.assertEqual(mc.screen_entries([]), ((), 0))

    def test_an_over_long_endpoint_is_dropped(self):
        kept, excluded = mc.screen_entries(["a" * 600])
        self.assertEqual(kept, ())
        self.assertEqual(excluded, 1)


# --- 9. invariants ----------------------------------------------------------

class WatermarkTests(unittest.TestCase):
    def events(self):
        return [
            event(10, "claim.created", at="2026-09-04T09:00:00Z"),
            event(20, "claim.created", at="2026-09-04T09:10:00Z"),
            event(30, "claim.created", at="2026-09-04T09:20:00Z"),
        ]

    def test_the_watermark_is_the_newest_event_at_or_before_the_boundary(self):
        events = self.events()
        self.assertEqual(mc.event_watermark(
            events, boundary_created_at="2026-09-04T09:15:00Z", after=0), 20)
        self.assertEqual(mc.event_watermark(
            events, boundary_created_at="2026-09-04T09:10:00Z", after=0), 20)
        self.assertEqual(mc.event_watermark(
            events, boundary_created_at="2026-09-04T09:59:00Z", after=0), 30)

    def test_a_sub_region_with_no_new_events_gets_an_empty_range(self):
        events = self.events()
        self.assertEqual(mc.event_watermark(
            events, boundary_created_at="2026-09-04T09:20:00Z", after=30), 30)
        self.assertEqual(mc.event_watermark(
            [], boundary_created_at="2026-09-04T09:20:00Z", after=0), 0)

    def test_a_watermark_that_runs_backwards_refuses_spine_unverified(self):
        with self.assertRaises(mc.CompactionError) as caught:
            mc.event_watermark(self.events(),
                               boundary_created_at="2026-09-04T09:10:00Z", after=30)
        self.assertEqual(caught.exception.code, "spine_unverified")
        with self.assertRaises(mc.CompactionError):
            mc.event_watermark([], boundary_created_at="2026-09-04T09:10:00Z",
                               after=1)


class InvariantTests(unittest.TestCase):
    def build(self, events, **changes):
        arguments = {"span": bounds(), "events": events, "after": 0}
        arguments.update(changes)
        return mc.build_invariants(**arguments)["derived"]

    def test_the_envelope_is_version_two_with_derived_and_observed(self):
        whole = mc.build_invariants(span=bounds(), events=[], after=0)
        self.assertEqual(whole["v"], 2)
        self.assertEqual(set(whole), {"v", "derived", "observed"})
        self.assertEqual(whole["observed"], {"tools_used": [], "files_touched": []})
        self.assertEqual(set(whole["derived"]), {
            "event_range", "event_count", "event_first_at", "event_last_at",
            "claims_created", "claims_superseded", "claims_retracted",
            "claims_tombstoned", "claim_keys", "memories_created",
            "lessons_created", "proposals_confirmed", "proposals_not_stored",
            "message_ids", "outcome", "screened", "excluded_by_screen",
            "span_has_proposal",
        })

    def test_each_kind_lands_in_its_own_bucket(self):
        derived = self.build([
            event(1, "claim.created", at="a", subject_kind="claim", subject_id=11,
                  payload={"claim_key": "k1", "claim_id": 11}),
            event(2, "claim.imported", at="b", subject_kind="claim", subject_id=12,
                  payload={"claim_key": "k2", "claim_id": 12}),
            event(3, "claim.superseded", at="c", subject_kind="claim", subject_id=9,
                  payload={"claim_key": "k1", "claim_id": 9, "related_claim_id": 11}),
            event(4, "claim.retracted", at="d", subject_kind="claim", subject_id=12,
                  payload={"claim_key": "k2", "claim_id": 12}),
            event(5, "claim.tombstoned", at="e", subject_kind="claim", subject_id=9,
                  payload={"claim_key": "k3", "removed_claim_ids": [7, 8]}),
            event(6, "memory.created", at="f", subject_kind="memory", subject_id=50),
            event(7, "memory.imported", at="g", subject_kind="memory", subject_id=51),
            event(8, "lesson.created", at="h", subject_kind="lesson", subject_id=60),
            event(9, "proposal.confirmed", at="i", payload={"claim_key": "k4"}),
            event(10, "proposal.not_stored", at="j", outcome="noop",
                  payload={"variant": "readonly"}),
        ], boundary_created_at="z")
        self.assertEqual(derived["claims_created"], [11, 12])
        self.assertEqual(derived["claims_superseded"], [[9, 11]])
        self.assertEqual(derived["claims_retracted"], [12])
        self.assertEqual(derived["claims_tombstoned"], [7, 8])
        self.assertEqual(derived["memories_created"], [50, 51])
        self.assertEqual(derived["lessons_created"], [60])
        self.assertEqual(derived["proposals_confirmed"], 1)
        self.assertEqual(derived["proposals_not_stored"], 1)
        self.assertEqual(derived["claim_keys"], ["k1", "k2", "k3", "k4"])
        self.assertEqual(derived["event_count"], 10)
        self.assertEqual(derived["outcome"], "complete")

    def test_a_supersession_with_no_partner_records_a_null_rather_than_guessing(self):
        derived = self.build([
            event(1, "claim.superseded", at="a", subject_kind="claim", subject_id=9,
                  payload={"claim_key": "k", "claim_id": 9}),
        ], boundary_created_at="z")
        self.assertEqual(derived["claims_superseded"], [[9, None]])

    def test_a_claim_id_falls_back_to_the_subject_when_the_payload_omits_it(self):
        derived = self.build([
            event(1, "claim.created", at="a", subject_kind="claim", subject_id=44,
                  payload={"claim_key": "k"}),
            event(2, "claim.created", at="b", payload={"claim_key": "k2"}),
        ], boundary_created_at="z")
        self.assertEqual(derived["claims_created"], [44])

    def test_a_rejected_claim_event_is_not_counted_but_a_noop_proposal_is(self):
        derived = self.build([
            event(1, "claim.created", at="a", outcome="rejected",
                  subject_kind="claim", subject_id=11, payload={"claim_key": "k"}),
            event(2, "proposal.not_stored", at="b", outcome="noop", payload={}),
        ], boundary_created_at="z")
        self.assertEqual(derived["claims_created"], [])
        self.assertEqual(derived["claim_keys"], [])
        self.assertEqual(derived["proposals_not_stored"], 1)
        self.assertEqual(derived["event_count"], 2)
        self.assertEqual(derived["outcome"], "complete")

    def test_an_unreadable_payload_makes_the_outcome_partial(self):
        clean = [event(1, "claim.created", at="a", subject_kind="claim",
                       subject_id=11, payload={"claim_key": "k", "claim_id": 11})]
        self.assertEqual(self.build(clean, boundary_created_at="z")["outcome"],
                         "complete")
        redacted = [event(1, "claim.created", at="a", subject_kind="claim",
                          subject_id=11, payload=None)]
        partial = self.build(redacted, boundary_created_at="z")
        self.assertEqual(partial["outcome"], "partial")
        self.assertEqual(partial["claims_created"], [])
        self.assertEqual(partial["event_count"], 1)

    def test_an_unknown_kind_makes_the_outcome_partial(self):
        derived = self.build([event(1, "transcript.compacted", at="a")],
                             boundary_created_at="z")
        expected = "partial" if "transcript.compacted" not in spine.SPINE_KINDS \
            else "complete"
        self.assertEqual(derived["outcome"], expected)
        self.assertEqual(self.build([event(1, "not.a.kind", at="a")],
                                    boundary_created_at="z")["outcome"], "partial")

    def test_a_redacted_proposal_payload_still_counts_and_reports_partial(self):
        derived = self.build([event(1, "proposal.confirmed", at="a", payload=None)],
                             boundary_created_at="z")
        self.assertEqual(derived["proposals_confirmed"], 1)
        self.assertEqual(derived["outcome"], "partial")

    def test_an_event_from_another_conversation_raises(self):
        with self.assertRaises(mc.CompactionError) as caught:
            self.build([event(1, "claim.created", at="a", conversation_id=2)],
                       boundary_created_at="z")
        self.assertEqual(caught.exception.code, "cross_conversation")
        # An event with no conversation at all is accepted: the spine leaves
        # the column NULL for some receipts.
        self.build([event(1, "claim.created", at="a", conversation_id=None,
                          payload={})], boundary_created_at="z")

    def test_claim_keys_and_files_are_screened_and_the_drops_are_counted(self):
        derived = self.build(
            [event(1, "claim.created", at="a", subject_kind="claim", subject_id=1,
                   payload={"claim_key": "project:1|ada@example.com|owner",
                            "claim_id": 1}),
             event(2, "claim.created", at="b", subject_kind="claim", subject_id=2,
                   payload={"claim_key": "project:1|relay|port", "claim_id": 2})],
            boundary_created_at="z",
            observed={"files_touched": ["workspace/notes.md",
                                        "/home/operator/secret.txt"],
                      "tools_used": ["read_file"]},
        )
        self.assertEqual(derived["claim_keys"], ["project:1|relay|port"])
        self.assertEqual(derived["excluded_by_screen"], 2)
        whole = mc.build_invariants(
            span=bounds(), events=[], after=0,
            observed={"files_touched": ["/home/operator/secret.txt"],
                      "tools_used": ["read_file", 17]},
        )
        self.assertEqual(whole["observed"]["files_touched"], [])
        self.assertEqual(whole["observed"]["tools_used"], ["read_file"])
        self.assertEqual(whole["derived"]["excluded_by_screen"], 1)

    def test_a_recorded_range_is_used_verbatim_on_the_rebuild_path(self):
        events = [event(5, "claim.created", at="2026-09-04T23:00:00Z",
                        subject_kind="claim", subject_id=1,
                        payload={"claim_key": "k", "claim_id": 1})]
        # The boundary would exclude event 5, but the recorded range includes
        # it; the rebuild must reproduce what was recorded.
        derived = self.build(events, through=5, after=0)
        self.assertEqual(derived["event_range"], {"after": 0, "through": 5})
        self.assertEqual(derived["claims_created"], [1])
        with self.assertRaises(mc.CompactionError) as caught:
            self.build(events, through=1, after=9)
        self.assertEqual(caught.exception.code, "spine_unverified")

    def test_the_screened_flag_is_carried_through_untouched(self):
        self.assertFalse(self.build([], boundary_created_at="z")["screened"])
        self.assertTrue(self.build([], boundary_created_at="z",
                                   screened=True)["screened"])


# --- 10. the summary --------------------------------------------------------

class SummaryTests(unittest.TestCase):
    def derived(self, **changes):
        base = {
            "message_ids": {"first": 1, "last": 10, "count": 10},
            "claims_created": [1, 2], "claims_superseded": [[3, 1]],
            "claims_retracted": [], "memories_created": [9],
            "lessons_created": [], "proposals_confirmed": 1,
            "proposals_not_stored": 2,
        }
        base.update(changes)
        return base

    def test_clip_text_is_the_agents_clip(self):
        from jarvis.agent import _clip
        cases = ["", "short", "a" * 159, "a" * 160, "a" * 161, "word " * 400,
                 "\u00e9" * 300]
        for value in cases:
            for limit in (-1, 0, 1, 16, 40, 160, 1200):
                self.assertEqual(mc.clip_text(value, limit), _clip(value, limit),
                                 (len(value), limit))

    def test_sentence_extraction_takes_the_first_and_the_last(self):
        text = "First one. Second one! Third one?"
        self.assertEqual(mc.first_sentence(text), "First one.")
        self.assertEqual(mc.last_sentence(text), "Third one?")
        self.assertEqual(mc.first_sentence("no terminator here"),
                         "no terminator here")
        self.assertEqual(mc.last_sentence("no terminator here"),
                         "no terminator here")
        self.assertEqual(mc.last_sentence("Only one."), "Only one.")
        self.assertEqual(mc.first_sentence("  spaced   out  words "),
                         "spaced out words")

    def test_a_clean_span_quotes_both_ends_and_a_screened_one_quotes_neither(self):
        messages = [row(1, "user", content="Where does the relay listen? Also hi."),
                    row(2, "assistant", content="It listens on 8080. Rebound today.")]
        open_summary = mc.build_summary(derived=self.derived(), messages=messages)
        self.assertEqual(open_summary.excerpts_included, 2)
        self.assertIn("Where does the relay listen?", open_summary.text)
        self.assertIn("Rebound today.", open_summary.text)
        self.assertTrue(open_summary.text.startswith(mc.SUMMARY_LEAD))
        self.assertFalse(open_summary.fell_back_to_counts)
        shut = mc.build_summary(derived=self.derived(), messages=messages,
                                screened=True)
        self.assertEqual(shut.excerpts_included, 0)
        self.assertNotIn("relay", shut.text)
        self.assertIn("10 messages (1-10)", shut.text)
        self.assertEqual(shut.chars, len(shut.text))

    def test_the_counts_clause_reports_what_derived_holds(self):
        text = mc.build_summary(derived=self.derived()).text
        self.assertIn("2 facts recorded", text)
        self.assertIn("1 superseded", text)
        self.assertIn("0 retracted", text)
        self.assertIn("1 memories", text)
        self.assertIn("0 lessons", text)
        self.assertIn("1 proposals confirmed", text)
        self.assertIn("2 not stored", text)
        self.assertIn("Earlier in this conversation: 10 messages (1-10)", text)
        self.assertEqual(
            mc.build_summary(derived={}).text,
            "Earlier in this conversation: 0 messages (0-0), 0 facts recorded, "
            "0 superseded, 0 retracted, 0 memories, 0 lessons, 0 proposals "
            "confirmed, 0 not stored.",
        )

    def test_a_screened_excerpt_is_dropped_and_counted(self):
        messages = [row(1, "user", content="Mail me at ada@example.com."),
                    row(2, "assistant", content="Understood, noted.")]
        summary = mc.build_summary(derived=self.derived(), messages=messages)
        self.assertEqual(summary.excerpts_screened, 1)
        self.assertEqual(summary.excerpts_included, 1)
        self.assertNotIn("ada@example.com", summary.text)
        self.assertIn("Understood, noted.", summary.text)

    def test_an_unscreenable_counts_clause_withholds_the_whole_summary(self):
        # derived is caller-supplied and message_ids' values reach the text
        # directly, so the whole-text screen is the only guard on them.
        summary = mc.build_summary(derived=self.derived(
            message_ids={"first": 1, "last": 10, "count": "ada@example.com"}))
        self.assertTrue(summary.fell_back_to_counts)
        self.assertEqual(summary.text, mc.SUMMARY_WITHHELD)
        self.assertNotIn("ada@example.com", summary.text)
        self.assertEqual(mc.screen_span_text(mc.SUMMARY_WITHHELD), (False, None))

    def test_the_summary_is_bounded_and_deterministic(self):
        messages = [row(1, "user", content="Q. " + "long question " * 50),
                    row(2, "assistant", content="A. " + "long answer " * 50)]
        summary = mc.build_summary(derived=self.derived(), messages=messages,
                                   summary_chars=200)
        self.assertLessEqual(summary.chars, 200)
        again = mc.build_summary(derived=self.derived(), messages=messages,
                                 summary_chars=200)
        self.assertEqual(summary, again)
        self.assertGreater(len(mc.build_summary(derived=self.derived(),
                                                summary_chars=1).text), 0)

    def test_runtime_summary_is_the_same_text_from_raw_strings(self):
        messages = [row(1, "user", content="Where does the relay listen?"),
                    row(2, "assistant", content="On 8080.")]
        self.assertEqual(
            mc.runtime_summary(self.derived(),
                               first_user_text="Where does the relay listen?",
                               last_assistant_text="On 8080."),
            mc.build_summary(derived=self.derived(), messages=messages).text,
        )
        self.assertEqual(mc.runtime_summary(self.derived(), screened=True),
                         mc.build_summary(derived=self.derived(),
                                          screened=True).text)

    def test_a_span_with_only_one_role_quotes_only_that_end(self):
        only_user = mc.build_summary(derived=self.derived(),
                                     messages=[row(1, "user", content="Hello.")])
        self.assertEqual(only_user.excerpts_included, 1)
        metadata_only = mc.build_summary(derived=self.derived(),
                                         messages=[row(1, "user", chars=5)])
        self.assertEqual(metadata_only.excerpts_included, 0)
        blank = mc.build_summary(derived=self.derived(),
                                 messages=[row(1, "user", content="   ")])
        self.assertEqual(blank.excerpts_included, 0)


# --- 11. the assembled record -----------------------------------------------

class CompactedSpanTests(unittest.TestCase):
    def setUp(self):
        self.messages, self.record = make_record(count=4, span_has_proposal=1)

    def test_the_record_carries_every_column_the_two_tables_need(self):
        milestone = self.record.milestone_row(created_at="2026-09-04T10:00:00Z",
                                              spine_event_id=7)
        self.assertEqual(set(milestone), {
            "created_at", "conversation_id", "seq", "first_message_id",
            "last_message_id", "message_count", "source_chars", "stored_bytes",
            "summary", "summary_chars", "invariants_json", "handle",
            "span_sha256", "span_unkeyed_sha256", "summary_sha256",
            "invariants_sha256", "key_fingerprint", "author", "model",
            "spine_event_id",
        })
        self.assertEqual(milestone["author"], "runtime")
        self.assertIsNone(milestone["model"])
        self.assertEqual(milestone["spine_event_id"], 7)
        span_row = self.record.span_row(milestone_id=3)
        self.assertEqual(set(span_row), {"handle", "milestone_id",
                                         "conversation_id", "body", "body_chars"})
        self.assertEqual(span_row["body_chars"], len(self.record.canonical))

    def test_the_payload_is_digest_only_and_inside_the_closed_key_set(self):
        payload = self.record.spine_payload(at="2026-09-04T10:00:00Z")
        self.assertTrue(set(payload) <= mc.COMPACTED_PAYLOAD_KEYS)
        self.assertTrue(mc.COMPACTED_REQUIRED_KEYS <= set(payload))
        self.assertEqual(payload["span_has_proposal"], 1)
        self.assertEqual(payload["event_range"], {"after": 0, "through": 0})
        # No content, no summary text, no claim value.
        blob = json.dumps(payload)
        for message in self.messages:
            self.assertNotIn(message.content, blob)
        self.assertNotIn(self.record.summary, blob)
        self.assertLess(len(blob.encode("utf-8")), spine.MAX_PAYLOAD_BYTES)

    def test_the_payload_self_check_fires_in_both_directions(self):
        original = mc.COMPACTED_REQUIRED_KEYS
        try:
            mc.COMPACTED_REQUIRED_KEYS = original | {"a_key_nobody_writes"}
            with self.assertRaises(mc.CompactionError) as caught:
                self.record.spine_payload(at="now")
            self.assertEqual(caught.exception.code, "error")
        finally:
            mc.COMPACTED_REQUIRED_KEYS = original
        allowed = mc.COMPACTED_PAYLOAD_KEYS
        try:
            mc.COMPACTED_PAYLOAD_KEYS = frozenset({"seq"})
            with self.assertRaises(mc.CompactionError):
                self.record.spine_payload(at="now")
        finally:
            mc.COMPACTED_PAYLOAD_KEYS = allowed
        # Restored: the ordinary payload builds again.
        self.assertTrue(self.record.spine_payload(at="now"))

    def test_a_model_author_is_refused_and_runtime_is_not(self):
        span = self.record
        with self.assertRaises(mc.CompactionError) as caught:
            mc.build_compacted_span(
                span=bounds(message_ids=tuple(m.id for m in self.messages),
                            first_message_id=self.messages[0].id,
                            last_message_id=self.messages[-1].id,
                            message_count=4, source_chars=1),
                messages=self.messages, events=[], after=0, seq=1, key=KEY,
                author="model")
        self.assertEqual(caught.exception.code, "model_author_not_supported")
        self.assertEqual(span.author, "runtime")
        with self.assertRaises(ValueError):
            mc.build_compacted_span(
                span=bounds(message_ids=tuple(m.id for m in self.messages)),
                messages=self.messages, events=[], after=0, seq=1, key=KEY,
                author="operator")

    def test_rows_that_are_not_the_planned_rows_refuse_stale_span(self):
        with self.assertRaises(mc.CompactionError) as caught:
            mc.build_compacted_span(span=bounds(), messages=self.messages,
                                    events=[], after=0, seq=1, key=KEY)
        self.assertEqual(caught.exception.code, "stale_span")
        with self.assertRaises(mc.CompactionError) as empty:
            mc.build_compacted_span(span=bounds(), messages=[], events=[],
                                    after=0, seq=1, key=KEY)
        self.assertEqual(empty.exception.code, "error")

    def test_the_same_span_always_yields_the_same_record(self):
        _messages, again = make_record(count=4, span_has_proposal=1)
        self.assertEqual(again.handle, self.record.handle)
        self.assertEqual(again.span_sha256, self.record.span_sha256)
        self.assertEqual(again.span_unkeyed_sha256, self.record.span_unkeyed_sha256)
        self.assertEqual(again.summary, self.record.summary)
        self.assertEqual(again.invariants_json, self.record.invariants_json)
        # A different key moves the keyed digests and nothing else.
        _messages, other = make_record(count=4, span_has_proposal=1, key=OTHER_KEY)
        self.assertEqual(other.span_unkeyed_sha256, self.record.span_unkeyed_sha256)
        self.assertNotEqual(other.span_sha256, self.record.span_sha256)
        self.assertEqual(other.handle, self.record.handle)

    def test_the_read_row_is_the_five_prompt_fields_plus_the_selectors_two(self):
        read = self.record.milestone_read_row()
        self.assertEqual(set(read), {"seq", "handle", "summary", "message_ids",
                                     "claim_keys", "files_touched", "outcome"})
        self.assertEqual(read["outcome"], "complete")
        self.assertEqual(read["message_ids"]["count"], 4)

    def test_the_read_row_screens_again_on_the_way_out(self):
        messages, record = make_record(count=2)
        poisoned = json.loads(record.invariants_json)
        poisoned["derived"]["claim_keys"] = ["project:1|ada@example.com|owner",
                                             "project:1|relay|port"]
        poisoned["observed"]["files_touched"] = ["/home/operator/secret.txt", "ok.md"]
        object.__setattr__(record, "invariants", poisoned)
        read = record.milestone_read_row()
        self.assertEqual(read["claim_keys"], ["project:1|relay|port"])
        self.assertEqual(read["files_touched"], ["ok.md"])
        self.assertTrue(messages)

    def test_the_reduction_ratio_is_stored_over_source(self):
        self.assertAlmostEqual(
            self.record.reduction_ratio,
            round(self.record.stored_bytes / self.record.source_chars, 6))
        empty = mc.CompactedSpan(
            conversation_id=1, seq=1, handle="mem:span/1/1/" + "a" * 12,
            first_message_id=1, last_message_id=1, message_count=1,
            source_chars=0, stored_bytes=10, body=b"", body_chars=0,
            summary="", summary_chars=0, invariants={"derived": {}},
            invariants_json="{}", span_sha256="a" * 64,
            span_unkeyed_sha256="a" * 64, summary_sha256="a" * 64,
            invariants_sha256="a" * 64, key_fingerprint="a" * 64,
            author="runtime", screened=False, screen_reason=None,
            span_has_proposal=0)
        self.assertEqual(empty.reduction_ratio, 0.0)

    def test_a_span_carrying_an_identifier_is_screened_and_says_why(self):
        messages = [row(1, "user", content="mail ada@example.com about the relay"),
                    row(2, "assistant", content="noted")]
        span = mc.SpanBounds(
            conversation_id=1, first_message_id=1, last_message_id=2,
            message_count=2, source_chars=sum(m.chars for m in messages),
            last_created_at=messages[-1].created_at, span_has_proposal=0,
            message_ids=(1, 2))
        record = mc.build_compacted_span(span=span, messages=messages, events=[],
                                         after=0, seq=1, key=KEY)
        self.assertTrue(record.screened)
        self.assertEqual(record.screen_reason, "email")
        self.assertTrue(record.invariants["derived"]["screened"])
        self.assertNotIn("ada@example.com", record.summary)
        self.assertNotIn("relay", record.summary)
        # ...and the span bytes themselves are still exact.
        back = mc.rehydrate_span(
            record.handle,
            milestone=record.milestone_row(created_at="now", spine_event_id=1),
            body=record.body, key=KEY)
        self.assertEqual(back["messages"][0]["content"], messages[0].content)

    def test_the_record_carries_its_own_conversation_scoped_predicate(self):
        sql, params = self.record.range_predicate()
        self.assertIn("conversation_id = ?", sql)
        self.assertEqual(params[0], self.record.conversation_id)
        self.assertIs(mc.build_span_record, mc.build_compacted_span)
        self.assertIs(mc.SpanRecord, mc.CompactedSpan)


# --- 12. the compacted-history block ----------------------------------------

def read_row(seq=1, summary="Earlier in this conversation: 10 messages.",
             handle=None, first=1, last=10, count=10, outcome="complete"):
    return {
        "seq": seq,
        "handle": handle if handle is not None else f"mem:span/1/{seq}/" + "a" * 12,
        "summary": summary,
        "message_ids": {"first": first, "last": last, "count": count},
        "claim_keys": ["project:1|relay|port"],
        "files_touched": ["workspace/notes.md"],
        "outcome": outcome,
    }


def _guidance_literals():
    """The literals ``_dialogue_claim_guidance`` SCANS its input for, read out
    of the real function rather than retyped.

    Two hand-written tuples used to live here, and they were the third
    instance in one evening of the construct behind design item 11.21 --
    after ``_TRIGGER_SQL`` missed M4 triggers and ``_MILESTONE_COLUMNS``
    mirrored a DDL, both of which were deleted for exactly this reason.  A
    snapshot of a measurement is what goes stale: add an eleventh literal to
    ``agent.py``, or reformat an existing one, and frozen tuples would go on
    protecting yesterday's vocabulary while still passing green.

    Extracted by ``ast`` from the membership tests themselves -- ``"x" in
    dialogue_context`` -- rather than by scraping every quoted string in the
    source, which sweeps up the docstring and the guidance prose.
    """
    import ast
    import inspect
    from jarvis import agent as agent_module

    tree = ast.parse(inspect.getsource(agent_module._dialogue_claim_guidance))
    found = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Compare)
            and len(node.ops) == 1
            and isinstance(node.ops[0], ast.In)
            and isinstance(node.left, ast.Constant)
            and isinstance(node.left.value, str)
        ):
            found.add(node.left.value)
    # The gate the function opens with is a tag, not a status literal.
    found.discard("<temporal_claims>")
    return tuple(sorted(found))


def _partition_literals(literals):
    """``(colliding, suffixed)`` by the rule, not by a list.

    The block renders a value as ``"outcome":"<value>"``, so a literal can be
    formed by a bare value exactly when the literal IS a quoted bare word:
    it opens and closes with a quote and contains no others.  Everything else
    carries a suffix (``:true``, ``":``) or a second quoted field
    (``"match":"subject"``) that no single value can supply.

    Returns the injectable VALUES for the colliding side -- the literal with
    its quotes stripped -- and the raw literals for the safe side.
    """
    colliding, suffixed = [], []
    for literal in literals:
        bare = (
            literal.startswith('"')
            and literal.endswith('"')
            and literal.count('"') == 2
        )
        (colliding if bare else suffixed).append(
            literal.strip('"') if bare else literal)
    return tuple(colliding), tuple(suffixed)


class HistoryBlockTests(unittest.TestCase):
    def test_the_block_is_the_designs_sibling_element(self):
        block = mc.render_compacted_history_block([read_row(1), read_row(2)])
        self.assertTrue(block.text.startswith("\n\n<jarvis_compacted_history>\n"))
        self.assertTrue(block.text.endswith("</jarvis_compacted_history>"))
        self.assertIn(mc.COMPACTED_HISTORY_LEAD, block.text)
        self.assertEqual(block.rows_rendered, 2)
        self.assertEqual(block.rows_dropped, 0)
        self.assertIsNone(block.refusal)
        self.assertEqual(block.chars, len(block.text))
        self.assertEqual(mc.compacted_history_suffix([read_row(1), read_row(2)]),
                         block.text)

    def test_the_block_renders_the_five_prompt_fields_and_no_others(self):
        payload = mc.render_compacted_history_block([read_row(1)]).text
        rendered = json.loads(payload.split(mc.COMPACTED_HISTORY_LEAD)[1]
                              .split("</jarvis_compacted_history>")[0].strip())
        self.assertEqual(set(rendered[0]), set(mc.HISTORY_ROW_FIELDS))
        self.assertEqual(rendered[0]["outcome"], "complete")
        self.assertNotIn("claim_keys", payload)
        self.assertNotIn("files_touched", payload)
        self.assertNotIn("project:1|relay|port", payload)

    def test_no_rows_render_no_block(self):
        empty = mc.render_compacted_history_block([])
        self.assertEqual(empty.text, "")
        self.assertEqual(empty.rows_rendered, 0)
        self.assertEqual(mc.render_compacted_history_block([read_row()],
                                                           max_rows=0).text, "")

    def test_the_whole_suffix_stays_inside_the_budget(self):
        rows = [read_row(seq, summary="s" * 400) for seq in range(1, 7)]
        block = mc.render_compacted_history_block(rows)
        self.assertLessEqual(block.chars, mc.COMPACTED_HISTORY_LIMIT)
        self.assertGreater(block.rows_dropped, 0)
        self.assertGreater(block.rows_rendered, 0)
        # The rows kept are the NEWEST ones.
        self.assertIn('"seq":6', block.text)
        self.assertNotIn('"seq":1', block.text)

    def test_more_rows_than_max_rows_keeps_the_newest(self):
        rows = [read_row(seq, summary="short") for seq in range(1, 11)]
        block = mc.render_compacted_history_block(rows, max_rows=3)
        self.assertEqual(block.rows_rendered, 3)
        self.assertIn('"seq":10', block.text)
        self.assertNotIn('"seq":7', block.text)

    def test_a_single_oversized_row_loses_its_summary_before_the_block_goes(self):
        cleared = mc.render_compacted_history_block([read_row(1, summary="s" * 900)],
                                                    char_budget=600)
        self.assertEqual(cleared.rows_rendered, 1)
        self.assertEqual(cleared.summaries_cleared, 1)
        self.assertNotIn("ssss", cleared.text)
        self.assertIn("mem:span/1/1/", cleared.text)
        self.assertLessEqual(cleared.chars, 600)
        gone = mc.render_compacted_history_block([read_row(1)], char_budget=50)
        self.assertEqual(gone.text, "")
        self.assertEqual(gone.rows_rendered, 0)

    def test_the_prompt_characters_are_escaped(self):
        block = mc.render_compacted_history_block(
            [read_row(1, summary="a < b & c > d")])
        self.assertNotIn("< b", block.text)
        self.assertIn("\\u003c", block.text)
        self.assertIn("\\u0026", block.text)
        self.assertIn("\\u003e", block.text)
        # The only angle brackets left are the element's own tags.
        body = block.text.replace("<jarvis_compacted_history>", "").replace(
            "</jarvis_compacted_history>", "")
        self.assertNotIn("<", body)
        self.assertNotIn(">", body)

    def test_a_screened_summary_is_dropped_from_the_block_and_counted(self):
        block = mc.render_compacted_history_block(
            [read_row(1, summary="reach me at ada@example.com")])
        self.assertNotIn("ada@example.com", block.text)
        self.assertEqual(block.excluded_by_screen, 1)
        self.assertEqual(block.rows_rendered, 1)
        clean = mc.render_compacted_history_block([read_row(1)])
        self.assertEqual(clean.excluded_by_screen, 0)

    def test_a_malformed_handle_is_dropped_from_the_block(self):
        block = mc.render_compacted_history_block(
            [read_row(1, handle="mem:span/1/1/NOTHEX000000")])
        self.assertEqual(block.excluded_by_screen, 1)
        self.assertNotIn("NOTHEX", block.text)
        self.assertIn('"handle":""', block.text)

    def test_an_outcome_is_emitted_only_when_the_source_stated_one(self):
        # Found by compaction-surface: defaulting a MISSING outcome to
        # "partial" manufactures a status out of an absence -- the shape
        # RESOLVED_AMBIGUITIES forbids for derived.outcome -- and does it on
        # the one surface in this phase that faces the model.
        stated = mc.render_compacted_history_block([read_row(1, outcome="complete")])
        self.assertIn('"outcome":"complete"', stated.text)
        self.assertEqual(stated.outcome_missing, 0)
        for silent in ({"seq": 2}, read_row(1, outcome=None),
                       read_row(1, outcome="everything is fine")):
            block = mc.render_compacted_history_block([silent])
            # 11.19b: the clause is STATED, not dropped -- omission reads as
            # success by default.
            self.assertIn('"outcome":"unstated"', block.text)
            # 11.19a: and the unknown never lands inside the closed set.
            self.assertNotIn('"outcome":"partial"', block.text)
            self.assertNotIn('"outcome":"complete"', block.text)
            self.assertNotIn("everything is fine", block.text)
            self.assertEqual(block.rows_rendered, 1)
            self.assertEqual(block.outcome_missing, 1)
        # The sentinel is deliberately outside the closed set.
        self.assertNotIn(mc.HISTORY_OUTCOME_UNSTATED, mc.DERIVED_OUTCOMES)

    def test_a_rendered_row_never_carries_a_key_outside_the_declared_five(self):
        for row in (read_row(1), {"seq": 2}, read_row(1, outcome=None)):
            payload = mc.render_compacted_history_block([row]).text
            rendered = json.loads(payload.split(mc.COMPACTED_HISTORY_LEAD)[1]
                                  .split("</jarvis_compacted_history>")[0].strip())
            self.assertEqual(set(rendered[0]), set(mc.HISTORY_ROW_FIELDS))

    def test_a_missing_field_becomes_a_zero_rather_than_a_crash(self):
        block = mc.render_compacted_history_block([{"seq": 2}])
        self.assertIn('"first":0', block.text)
        self.assertIn('"count":0', block.text)
        self.assertEqual(block.rows_rendered, 1)
        # A zero id is arithmetic with no status meaning; an outcome is a
        # status, so it says the record was silent rather than guessing.
        self.assertIn('"outcome":"unstated"', block.text)

    def test_block_safety_names_all_three_forbidden_shapes(self):
        self.assertEqual(mc.block_safety("an ordinary summary"), (True, None))
        self.assertEqual(mc.block_safety("digest " + "a" * 64)[1],
                         "digest_shaped_token")
        self.assertEqual(
            mc.block_safety("promotion #12 approve ABCDEFGHIJKLMNOP")[1],
            "confirmation_code_shaped_token")
        self.assertEqual(mc.block_safety("write to ada@example.com")[1], "email")

    def test_an_unsafe_assembled_block_loses_its_summaries_then_refuses(self):
        # A digest-shaped run survives the per-field screen (it is not an
        # identifier) and is caught on the assembled text.
        block = mc.render_compacted_history_block(
            [read_row(1, summary="digest " + "f" * 64)])
        self.assertEqual(block.rows_rendered, 1)
        self.assertNotIn("f" * 64, block.text)
        self.assertGreaterEqual(block.summaries_cleared, 1)
        self.assertTrue(mc.block_safety(block.text)[0])
        # And when even the stripped block cannot be made safe, nothing is
        # emitted rather than something unsafe.
        refused = mc.render_compacted_history_block(
            [read_row(1, handle="mem:span/1/1/" + "a" * 12)], char_budget=1)
        self.assertEqual(refused.text, "")

    def test_a_block_that_cannot_be_made_safe_is_not_emitted_at_all(self):
        # The stripped block carries only a seq, a pattern-checked handle,
        # integer ids and a closed-set outcome, so nothing a caller can put in
        # a row reaches this last fallback today.  It exists so that a future
        # widening of the row fields, or of block_safety, fails closed instead
        # of emitting something unsafe -- and a guard nobody can trigger is a
        # guard nobody has tested, so the dependency is replaced to reach it.
        original = mc.block_safety
        try:
            mc.block_safety = lambda text: (False, "digest_shaped_token")
            refused = mc.render_compacted_history_block([read_row(1)])
        finally:
            mc.block_safety = original
        self.assertEqual(refused.text, "")
        self.assertEqual(refused.rows_rendered, 0)
        self.assertEqual(refused.refusal, "digest_shaped_token")
        self.assertGreaterEqual(refused.summaries_cleared, 1)
        # With the real screen back, the same row renders.
        self.assertNotEqual(mc.render_compacted_history_block([read_row(1)]).text, "")

    def test_no_outcome_value_can_flip_a_dialogue_guidance_line(self):
        """Design item 11.23: the invariant, not the instance.

        ``not_recorded`` was a correct-sounding token that silently cost the
        M-5 hazard one of its two closures, because it is one of the ten
        literals ``_dialogue_claim_guidance`` scans for and, as a JSON *value*
        rather than inside one, it is not defused by escaping.  The string was
        tonight; this is the class.  Asserted BEHAVIOURALLY against the real
        function rather than against a retyped copy of the ten literals --
        a second list of the same things is the mechanism that failed in
        11.21.
        """
        from jarvis.agent import _dialogue_claim_guidance
        base = ('<temporal_claims>[{"subject":"relay","value":"8080"}]'
                '</temporal_claims>')
        baseline = _dialogue_claim_guidance(base)
        vocabulary = (*mc.DERIVED_OUTCOMES, mc.HISTORY_OUTCOME_UNSTATED)
        self.assertIn(mc.HISTORY_OUTCOME_UNSTATED, vocabulary)
        for value in vocabulary:
            injected = f'{base} {{"outcome":"{value}"}}'
            self.assertEqual(
                _dialogue_claim_guidance(injected), baseline,
                f"outcome value {value!r} changes a guidance line")
        # The check discriminates, measured rather than assumed.  Three rows,
        # and the middle one is the interesting one: a literal that needs
        # more than its own text to match cannot be formed by a bare outcome
        # value, so a guard firing on it would be the false positive.
        #
        #   value           as {"outcome":"<value>"}   why
        #   not_recorded    FLIPS                      matches verbatim
        #   overflow        FLIPS                      matches verbatim
        #   lane_abstained  safe                       needs '":true' to match
        # All four, and the RULE that predicts them, which compaction-surface
        # derived after finding bridge_from in my incomplete table: a literal
        # collides as a bare value exactly when it has no suffix beyond its
        # closing quote.  Four of the ten are bare; the other six need
        # ':true', '":' or '":"subject"', which an outcome value cannot
        # supply.  Naming the rule beats naming tonight's four, because the
        # rule predicts the fifth.
        literals = _guidance_literals()
        colliding, suffixed = _partition_literals(literals)
        # The partition must COVER the literals, or the derivation has drifted
        # from the function and the loops below protect a subset in silence.
        # Compared by count and by re-quoting, because the colliding side is
        # returned as injectable values with their quotes stripped.
        self.assertEqual(len(colliding) + len(suffixed), len(literals))
        self.assertEqual(
            {f'"{value}"' for value in colliding} | set(suffixed), set(literals))
        self.assertTrue(colliding, "no bare literal found; the scan changed shape")
        self.assertTrue(suffixed, "no suffixed literal found; ditto")
        for value in colliding:
            self.assertNotEqual(
                _dialogue_claim_guidance(f'{base} {{"outcome":"{value}"}}'),
                baseline, f"{value} should flip a guidance line")
        for literal in suffixed:
            word = literal.strip('"').split('"')[0].rstrip(":")
            self.assertEqual(
                _dialogue_claim_guidance(f'{base} {{"outcome":"{word}"}}'),
                baseline, f"{literal} is not formable from a bare value")
        # ...and the LOOP above bites, not just the literals: poison the
        # vocabulary with each collider and the assertion the test makes must
        # fail every time.  Proving the literals flip is not the same as
        # proving this loop would notice.
        for poison in colliding:
            with self.assertRaises(AssertionError, msg=poison):
                for value in (*mc.DERIVED_OUTCOMES, poison):
                    self.assertEqual(
                        _dialogue_claim_guidance(f'{base} {{"outcome":"{value}"}}'),
                        baseline)
        # A benign value must NOT trip the loop, or the guard is over-strict
        # on correct code -- the failure mode a symmetric containment rule has.
        for literal in suffixed:
            benign = literal.strip('"').split('"')[0].rstrip(":")
            for value in (*mc.DERIVED_OUTCOMES, benign):
                self.assertEqual(
                    _dialogue_claim_guidance(f'{base} {{"outcome":"{value}"}}'),
                    baseline)
        # ...and the real rendered block carries none of it either.
        block = mc.render_compacted_history_block([read_row(1), {"seq": 2}])
        self.assertEqual(_dialogue_claim_guidance(base + block.text), baseline)

    def test_the_block_is_screened_once_per_fit_not_three_times(self):
        """M-6.  ``fit_history_rows`` used to answer "which rows fit" by
        RENDERING inside each of two ``while`` conditions, and the caller then
        rendered again to produce the block -- three renders, and every render
        screens every summary.

        Asserted as a mechanism (how many times the screen is called) rather
        than as a duration: a millisecond threshold in a unit test is flaky on
        a loaded machine and tells a later reader nothing about what went
        wrong.  The duration lives in the store's own scale test, which
        measures the budgeted call end to end.
        """
        calls = []
        real = mc.screen_endpoint
        rows = [read_row(seq, summary="s" * 300) for seq in range(1, 7)]
        try:
            mc.screen_endpoint = lambda text: (calls.append(len(text)), real(text))[1]
            calls.clear()
            mc.render_compacted_history_block(rows)
            one_render = len(calls)
            calls.clear()
            mc.fit_history_rows(rows)
            one_fit = len(calls)
        finally:
            mc.screen_endpoint = real
        # The fixture must actually reach the screen, or this passes on zero.
        self.assertGreater(one_render, 0)
        # A fit costs exactly one render, not two and not three.
        self.assertEqual(one_fit, one_render)

    def test_fit_and_render_agree_on_which_rows_survive(self):
        """The other half of the M-6 change: ``fit_history_rows`` now derives
        its answer FROM the render, so the store can no longer report a row
        count the agent will silently trim afterwards."""
        rows = [read_row(seq, summary="s" * 300) for seq in range(1, 7)]
        kept, overflow = mc.fit_history_rows(rows)
        block = mc.render_compacted_history_block(rows)
        self.assertEqual(len(kept), block.rows_rendered)
        self.assertEqual(list(block.rows), kept)
        # The fixture is big enough that trimming really happens, or the
        # agreement above is trivial.
        self.assertLess(len(kept), len(rows))
        self.assertTrue(overflow)
        # ...and when everything fits, nothing is dropped and overflow is False.
        small = [read_row(1, summary="short")]
        kept_small, overflow_small = mc.fit_history_rows(small)
        self.assertEqual(len(kept_small), 1)
        self.assertFalse(overflow_small)

    def test_the_surviving_rows_are_the_source_rows(self):
        """``HistoryBlock.rows`` carries the caller's OWN row objects, not the
        sanitised copies the prompt gets, so a caller using one render for
        both purposes still hands its own data onward."""
        rows = [read_row(seq) for seq in range(1, 4)]
        block = mc.render_compacted_history_block(rows)
        self.assertTrue(block.rows)
        for survivor in block.rows:
            self.assertIn(survivor, rows)
            # The source row keeps the selector fields the prompt never sees.
            self.assertIn("claim_keys", survivor)
        self.assertNotIn("claim_keys", block.text)

    def test_fit_history_rows_reports_overflow_in_both_directions(self):
        rows = [read_row(seq, summary="short") for seq in range(1, 4)]
        kept, overflow = mc.fit_history_rows(rows)
        self.assertEqual(len(kept), 3)
        self.assertFalse(overflow)
        capped, overflow = mc.fit_history_rows(rows, max_rows=2)
        self.assertEqual(len(capped), 2)
        self.assertTrue(overflow)
        starved, overflow = mc.fit_history_rows(rows, char_budget=10)
        self.assertEqual(starved, [])
        self.assertTrue(overflow)


# --- 13. schema 50: the tables, the migration, verify and rebuild -----------

class MigrationTests(unittest.TestCase):
    def test_the_tables_appear_only_when_the_migration_runs(self):
        db = _bare()
        db.execute("CREATE TABLE conversations (id INTEGER PRIMARY KEY)")
        self.assertFalse(mc.compaction_ready(db))
        self.assertEqual(mc.compaction_row_counts(db), {})
        first = mc.migrate_compaction_v50(db, KEY, now="2026-09-04T10:00:00Z")
        self.assertTrue(first["created"])
        self.assertTrue(mc.compaction_ready(db))
        self.assertEqual(first["key_fingerprint"], spine.key_fingerprint(KEY))
        self.assertEqual(mc.compaction_row_counts(db),
                         {"memory_compacted_spans": 0, "memory_milestones": 0})
        second = mc.migrate_compaction_v50(db, KEY, now="2026-09-04T11:00:00Z")
        self.assertFalse(second["created"])
        self.assertTrue(mc.compaction_ready(db))

    def test_the_migration_needs_the_spine_key(self):
        db = _bare()
        with self.assertRaises(TypeError):
            mc.migrate_compaction_v50(db, "not bytes", now="now")

    def test_a_re_migration_preserves_every_row(self):
        db = store()
        _messages, record = make_record()
        write_record(db, record)
        mc.migrate_compaction_v50(db, KEY, now="2026-09-04T11:00:00Z")
        self.assertEqual(mc.compaction_row_counts(db),
                         {"memory_compacted_spans": 1, "memory_milestones": 1})

    def test_a_milestone_row_cannot_be_updated(self):
        db = store()
        _messages, record = make_record()
        milestone_id = write_record(db, record)
        with self.assertRaises(sqlite3.IntegrityError):
            db.execute("UPDATE memory_milestones SET summary='x' WHERE id=?",
                       (milestone_id,))
        with self.assertRaises(sqlite3.IntegrityError):
            db.execute("UPDATE memory_compacted_spans SET body_chars=0")
        # A delete is still permitted: a tombstone removes a milestone.
        db.execute("DELETE FROM memory_compacted_spans WHERE handle=?",
                   (record.handle,))
        db.execute("DELETE FROM memory_milestones WHERE id=?", (milestone_id,))
        self.assertEqual(mc.compaction_row_counts(db)["memory_milestones"], 0)

    def test_a_tombstoned_milestone_id_is_never_reused(self):
        db = store()
        _messages, first = make_record(seq=1, first=1)
        _messages, second = make_record(seq=2, first=100)
        write_record(db, first, event_id=1)
        milestone_id = write_record(db, second, event_id=2)
        db.execute("DELETE FROM memory_compacted_spans WHERE milestone_id=?",
                   (milestone_id,))
        db.execute("DELETE FROM memory_milestones WHERE id=?", (milestone_id,))
        _messages, third = make_record(seq=3, first=200)
        reused = write_record(db, third, event_id=3)
        self.assertNotEqual(reused, milestone_id)
        self.assertGreater(reused, milestone_id)

    def test_the_next_seq_is_monotone_across_a_deletion(self):
        db = store()
        self.assertEqual(mc.next_seq(db, 1), 1)
        _messages, first = make_record(seq=1, first=1)
        write_record(db, first, event_id=1)
        self.assertEqual(mc.next_seq(db, 1), 2)
        _messages, second = make_record(seq=2, first=100)
        milestone_id = write_record(db, second, event_id=2)
        self.assertEqual(mc.next_seq(db, 1), 3)
        db.execute("DELETE FROM memory_compacted_spans WHERE milestone_id=?",
                   (milestone_id,))
        db.execute("DELETE FROM memory_milestones WHERE id=?", (milestone_id,))
        # MAX(seq)+1 would hand back 2 here and collide the handle.
        self.assertEqual(mc.next_seq(db, 1), 3)
        self.assertEqual(mc.next_seq(db, 999), 1)
        bare = _bare()
        self.assertEqual(mc.next_seq(bare, 1), 1)

    def test_a_populated_span_table_refuses_to_be_dropped(self):
        db = store()
        _messages, record = make_record()
        write_record(db, record)
        with self.assertRaises(mc.CompactionError) as caught:
            mc.drop_compaction_tables(db)
        self.assertEqual(caught.exception.code, "compaction_downgrade_refused")
        self.assertTrue(mc.compaction_ready(db))
        # Emptied, the same call succeeds.
        db.execute("DELETE FROM memory_compacted_spans")
        db.execute("DELETE FROM memory_milestones")
        mc.drop_compaction_tables(db)
        self.assertFalse(mc.compaction_ready(db))
        mc.drop_compaction_tables(db)

    def test_the_downgrade_predicate_reports_the_span_count_or_nothing(self):
        db = store()
        self.assertIsNone(mc.compaction_downgrade_blocked(db))
        _messages, record = make_record()
        write_record(db, record)
        self.assertEqual(mc.compaction_downgrade_blocked(db), 1)
        bare = _bare()
        self.assertIsNone(mc.compaction_downgrade_blocked(bare))

    def test_the_refusal_message_names_the_state_and_a_recovery_that_exists(self):
        message = mc.compaction_downgrade_message(3, version=49)
        self.assertIn("compaction_downgrade_refused", message)
        self.assertIn("3 compacted transcript span(s)", message)
        self.assertIn("schema marker is 49", message)
        self.assertIn("PRAGMA user_version = 50", message)
        self.assertIn("docs/COMPACTION.md", message)
        # repair-schema is deferred to M5.1; the message must not name it.
        self.assertNotIn("repair-schema", message)
        self.assertTrue(message.isascii())


class VerifyTests(unittest.TestCase):
    def setUp(self):
        self.db = store()
        self.messages, self.record = make_record(count=4)
        self.milestone_id = write_record(self.db, self.record)

    def test_a_healthy_store_verifies_and_reports_its_counts(self):
        result = mc.verify_compaction(self.db, KEY)
        self.assertTrue(result["ok"])
        self.assertTrue(result["checked"])
        self.assertEqual(result["milestones_checked"], 1)
        self.assertEqual(result["problems"], [])
        self.assertIsNone(result["refusal"])
        self.assertEqual(result["counts"], {"milestones": 1, "spans": 1,
                                            "conversations": 1, "verified": 1,
                                            "unverifiable": 0})
        self.assertEqual(result["key_fingerprint"], spine.key_fingerprint(KEY))

    def test_a_clean_result_never_claims_a_chain_it_did_not_check(self):
        # compaction-surface found this by reading the source: every receipt_*
        # kind checks a receipt against its milestone, and nothing checks that
        # receipt against the chain it sits on.  A forged chain would
        # otherwise yield ok True, which an operator reads as "the compacted
        # turns are sound".
        unstated = mc.verify_compaction(self.db, KEY)
        self.assertIsNone(unstated["chain_verified"])
        self.assertTrue(unstated["ok"])
        verified = mc.verify_compaction(self.db, KEY, spine_ok=True)
        self.assertTrue(verified["chain_verified"])
        self.assertTrue(verified["ok"])
        self.assertIsNone(verified["refusal"])
        broken = mc.verify_compaction(self.db, KEY, spine_ok=False)
        self.assertFalse(broken["chain_verified"])
        self.assertFalse(broken["ok"])
        self.assertEqual(broken["refusal"], "spine_unverified")
        self.assertIn("downstream", broken["refusal_detail"])

    def test_a_broken_chain_still_returns_the_problem_list(self):
        # The deliberate asymmetry with rebuild_milestones, which refuses
        # outright: there the harm is emitting an equivalence NUMBER over a
        # forged chain, here it is a green TICK.  An operator whose chain is
        # broken is exactly who needs the compaction detail, so the detail
        # survives and only the verdict is withheld.
        self.db.execute("DELETE FROM memory_compacted_spans")
        broken = mc.verify_compaction(self.db, KEY, spine_ok=False)
        self.assertFalse(broken["ok"])
        self.assertTrue(broken["checked"])
        self.assertEqual(broken["milestones_checked"], 1)
        self.assertIn("span_missing",
                      {problem[1] for problem in broken["problems"]})
        # ...whereas the sibling emits no comparison at all.
        refused = mc.rebuild_milestones(self.db, KEY, spine_ok=False)
        self.assertEqual(refused["checked"], 0)
        self.assertEqual(refused["divergences"], [])

    def test_a_real_refusal_is_not_overwritten_by_the_chain_one(self):
        # schema_too_old is a fact about this store; spine_unverified is a
        # fact the caller supplied.  The first one found wins, so the reason
        # a reader sees is the one that actually stopped the check.
        bare = _bare()
        both = mc.verify_compaction(bare, KEY, spine_ok=False)
        self.assertEqual(both["refusal"], "schema_too_old")
        self.assertFalse(both["chain_verified"])
        self.assertFalse(both["ok"])

    def test_an_empty_migrated_store_is_checked_and_ok(self):
        empty = store()
        result = mc.verify_compaction(empty, KEY)
        self.assertTrue(result["ok"])
        self.assertTrue(result["checked"])
        self.assertEqual(result["milestones_checked"], 0)

    def test_a_store_without_the_tables_is_refused_not_reported_healthy(self):
        bare = _bare()
        result = mc.verify_compaction(bare, KEY)
        self.assertFalse(result["ok"])
        self.assertFalse(result["checked"])
        self.assertEqual(result["refusal"], "schema_too_old")
        self.assertEqual(result["milestones_checked"], 0)

    def test_a_broken_database_is_a_refusal_and_never_an_exception(self):
        self.db.close()
        result = mc.verify_compaction(self.db, KEY)
        self.assertEqual(result["refusal"], "error")
        self.assertEqual(result["refusal_detail"], "ProgrammingError")
        self.assertFalse(result["ok"])

    def kinds(self):
        return {kind for _id, kind, _detail in mc.verify_compaction(self.db, KEY)
                ["problems"]}

    def test_a_missing_span_row_is_a_problem_and_the_rest_still_verifies(self):
        self.db.execute("DELETE FROM memory_compacted_spans")
        self.assertEqual(self.kinds(), {"span_missing"})
        result = mc.verify_compaction(self.db, KEY)
        self.assertFalse(result["ok"])
        self.assertEqual(result["counts"]["unverifiable"], 1)

    def test_a_tampered_body_is_reported_as_a_digest_problem(self):
        replace_body(self.db, mc.compress_span("something else entirely"))
        self.assertEqual(self.kinds(), {"span_digest", "span_identity",
                                        "span_chars"})

    def test_an_unreadable_body_is_reported_as_unreadable(self):
        replace_body(self.db, b"junk")
        self.assertEqual(self.kinds(), {"span_unreadable"})

    def test_a_swapped_key_is_key_loss_and_never_tampering(self):
        result = mc.verify_compaction(self.db, OTHER_KEY)
        self.assertEqual({kind for _id, kind, _d in result["problems"]},
                         {"key_mismatch"})
        self.assertEqual(result["refusal"], "key_mismatch")
        self.assertIn("key loss", result["refusal_detail"])
        # No digest problem is reported: a wrong key cannot judge a digest.
        self.assertNotIn("span_digest",
                         {kind for _id, kind, _d in result["problems"]})

    def test_a_dangling_or_wrong_receipt_is_reported(self):
        self.db.execute("DELETE FROM memory_spine_events")
        self.assertIn("receipt_missing", self.kinds())
        self.db.execute(
            "INSERT INTO memory_spine_events(id, created_at, kind, outcome, "
            "conversation_id, payload_json) VALUES (1, 'now', 'claim.created', "
            "'applied', 1, '{}')")
        self.assertIn("receipt_kind", self.kinds())

    def test_a_receipt_with_the_wrong_keys_or_digests_is_reported(self):
        payload = self.record.spine_payload(at="2026-09-04T10:00:00Z")
        payload["span_sha256"] = "b" * 64
        payload["nonsense"] = 1
        del payload["seq"]
        set_payload(self.db, json.dumps(payload))
        kinds = self.kinds()
        self.assertIn("receipt_digest", kinds)
        self.assertIn("receipt_extra_key", kinds)
        self.assertIn("receipt_missing_key", kinds)
        set_payload(self.db, "[")
        self.assertIn("receipt_unreadable", self.kinds())
        set_payload(self.db, "[1,2]")
        self.assertIn("receipt_unreadable", self.kinds())

    def test_a_handle_that_no_longer_matches_its_digest_is_reported(self):
        db = store()
        _messages, record = make_record(count=2)
        milestone = record.milestone_row(created_at="now", spine_event_id=1)
        milestone["handle"] = "mem:span/1/1/" + "b" * 12
        columns = ", ".join(milestone)
        marks = ", ".join("?" for _ in milestone)
        db.execute(f"INSERT INTO memory_milestones({columns}) VALUES ({marks})",
                   tuple(milestone.values()))
        db.execute("INSERT INTO memory_compacted_spans(handle, milestone_id, "
                   "conversation_id, body, body_chars) VALUES (?, 1, 1, ?, ?)",
                   (milestone["handle"], record.body, record.body_chars))
        kinds = {kind for _id, kind, _d in mc.verify_compaction(db, KEY)["problems"]}
        self.assertIn("handle_prefix", kinds)

    def test_an_edited_summary_or_invariants_column_is_caught_by_its_digest(self):
        # The spine holds only digests, so an out-of-band edit to the text
        # columns is invisible unless verify recomputes them.
        for column, kind in (("summary", "summary_digest"),
                             ("invariants_json", "invariants_digest")):
            db = store()
            _messages, record = make_record(count=2)
            milestone = record.milestone_row(created_at="now", spine_event_id=1)
            milestone[column] = milestone[column] + " tampered"
            columns = ", ".join(milestone)
            marks = ", ".join("?" for _ in milestone)
            db.execute(f"INSERT INTO memory_milestones({columns}) "
                       f"VALUES ({marks})", tuple(milestone.values()))
            db.execute("INSERT INTO memory_compacted_spans(handle, milestone_id, "
                       "conversation_id, body, body_chars) VALUES (?, 1, 1, ?, ?)",
                       (record.handle, record.body, record.body_chars))
            db.execute(
                "INSERT INTO memory_spine_events(id, created_at, kind, outcome, "
                "conversation_id, subject_kind, subject_id, payload_json) VALUES "
                "(1, 'now', ?, 'applied', 1, 'conversation', 1, ?)",
                (mc.COMPACTION_SPINE_KIND,
                 json.dumps(record.spine_payload(at="now"))))
            kinds = {problem[1]
                     for problem in mc.verify_compaction(db, KEY)["problems"]}
            self.assertIn(kind, kinds, column)
            db.close()

    def test_a_span_filed_under_another_conversation_is_reported(self):
        replace_span_row(self.db, conversation_id=9)
        self.assertIn("span_conversation", self.kinds())

    def test_two_milestones_covering_the_same_ids_overlap(self):
        db = store()
        _messages, first = make_record(seq=1, first=1, count=4)
        _messages, second = make_record(seq=2, first=3, count=4)
        write_record(db, first, event_id=1)
        write_record(db, second, event_id=2)
        kinds = {kind for _id, kind, _d in mc.verify_compaction(db, KEY)["problems"]}
        self.assertIn("range_overlap", kinds)

    def test_a_live_message_inside_a_compacted_range_is_reported(self):
        self.db.execute("INSERT INTO messages(id, conversation_id, created_at, "
                        "role, content) VALUES (2, 1, 'now', 'user', 'still here')")
        self.assertIn("live_overlap", self.kinds())
        # A live row of ANOTHER conversation inside the same id range is not a
        # problem: message ids are one global sequence (N-1).
        clean = store()
        _messages, record = make_record(count=4)
        write_record(clean, record)
        clean.execute("INSERT INTO conversations(id) VALUES (2)")
        clean.execute("INSERT INTO messages(id, conversation_id, created_at, "
                      "role, content) VALUES (2, 2, 'now', 'user', 'neighbour')")
        self.assertTrue(mc.verify_compaction(clean, KEY)["ok"])

    def test_every_problem_kind_the_function_can_emit_is_in_the_closed_set(self):
        original = mc.COMPACTION_PROBLEM_KINDS
        try:
            mc.COMPACTION_PROBLEM_KINDS = ("nothing_matches",)
            self.db.execute("DELETE FROM memory_compacted_spans")
            with self.assertRaises(mc.CompactionError) as caught:
                mc.verify_compaction(self.db, KEY)
            self.assertEqual(caught.exception.code, "error")
        finally:
            mc.COMPACTION_PROBLEM_KINDS = original
        self.assertIn("span_missing", self.kinds())


class RebuildTests(unittest.TestCase):
    def setUp(self):
        self.db = store()
        self.events = [
            event(1, "claim.created", at="2026-09-04T09:11:00Z",
                  subject_kind="claim", subject_id=11,
                  payload={"claim_key": "project:1|relay|port", "claim_id": 11}),
            event(2, "proposal.confirmed", at="2026-09-04T09:12:00Z",
                  payload={"claim_key": "project:1|relay|port"}),
        ]
        for row_event in self.events:
            self.db.execute(
                "INSERT INTO memory_spine_events(id, created_at, kind, outcome, "
                "conversation_id, subject_kind, subject_id, payload_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (row_event.id, row_event.created_at, row_event.kind,
                 row_event.outcome, row_event.conversation_id,
                 row_event.subject_kind, row_event.subject_id,
                 json.dumps(dict(row_event.payload or {}))),
            )
        _messages, self.record = make_record(count=4, events=self.events, after=0)
        write_record(self.db, self.record, event_id=99)

    def test_the_rebuild_reproduces_derived_byte_for_byte(self):
        result = mc.rebuild_milestones(self.db, KEY)
        self.assertEqual(result["checked"], 1)
        self.assertEqual(result["equivalent"], 1)
        self.assertEqual(result["divergent"], 0)
        self.assertEqual(result["divergences"], [])
        self.assertIsNone(result["refusal"])
        # The write path recorded a non-empty range, so equivalence is not
        # vacuous.
        self.assertEqual(self.record.event_range["through"], 2)
        self.assertEqual(
            json.loads(self.record.invariants_json)["derived"]["claims_created"],
            [11])

    def test_the_rebuild_diverges_under_a_deliberate_perturbation(self):
        # The payload's claim_id is what the derivation reads, so that is
        # what a perturbation has to move for the assertion to be able to fail.
        self.db.execute(
            "UPDATE memory_spine_events SET payload_json=? WHERE id=1",
            (json.dumps({"claim_key": "project:1|relay|port", "claim_id": 77}),))
        result = mc.rebuild_milestones(self.db, KEY)
        self.assertEqual(result["divergent"], 1)
        self.assertEqual(result["equivalent"], 0)
        divergence = result["divergences"][0]
        self.assertEqual(divergence["kind"], "derived_divergence")
        self.assertEqual(divergence["fields"], ["claims_created"])

    def test_the_result_carries_exactly_the_keys_its_docstring_names(self):
        # compaction-store indexed ok, rebuild_equivalence_derived and derived
        # from a docstring that mentioned them in prose while the result never
        # carried them -- the "named in prose, absent from behaviour" shape,
        # which would have raised KeyError at a phase gate.  Both directions:
        # the named keys are present and the disclaimed ones are absent.
        lean = mc.rebuild_milestones(self.db, KEY)
        self.assertEqual(set(lean), {
            "checked", "equivalent", "divergent", "divergences", "refusal",
            "refusal_detail", "chain_verified", "key_fingerprint"})
        for absent in ("ok", "rebuild_equivalence_derived", "derived"):
            self.assertNotIn(absent, lean)
        # ...and the docstring does not promise anything the result omits.
        import inspect
        doc = inspect.getdoc(mc.rebuild_milestones)
        self.assertIn("carries exactly these keys", doc)
        self.assertIn("no", doc.split("carries exactly these keys")[1][:400])

    def test_include_derived_returns_both_blocks_from_one_derivation(self):
        # E-2 needs the per-milestone blocks to check the byte-for-byte claim
        # without a second derivation, which would reintroduce the two-
        # implementations problem this function exists to avoid.
        rich = mc.rebuild_milestones(self.db, KEY, include_derived=True)
        self.assertIn("derived", rich)
        self.assertEqual(len(rich["derived"]), 1)
        blocks = next(iter(rich["derived"].values()))
        self.assertEqual(set(blocks),
                         {"milestone_id", "rebuilt_sha256", "stored", "rebuilt"})
        # Equivalent here, so the two blocks are byte-identical under the same
        # canonicalisation the comparison uses.
        self.assertEqual(spine.canonical(blocks["stored"]),
                         spine.canonical(blocks["rebuilt"]))
        self.assertEqual(blocks["stored"]["claims_created"], [11])
        # The third leg, from compaction-store: anchor "stored" to the TABLE.
        # Without it, both sides are this module's own reading and an equality
        # could be an artefact of core and the test looking at different rows
        # -- the byte-for-byte claim would be about core's view of itself
        # rather than about the row on disk.
        from_table = json.loads(self.db.execute(
            "SELECT invariants_json FROM memory_milestones").fetchone()[0])
        self.assertEqual(spine.canonical(blocks["stored"]),
                         spine.canonical(from_table["derived"]))
        # The counts are unchanged by asking for the blocks.
        lean = mc.rebuild_milestones(self.db, KEY)
        self.assertEqual((rich["checked"], rich["equivalent"], rich["divergent"]),
                         (lean["checked"], lean["equivalent"], lean["divergent"]))

    def test_the_block_states_what_was_derived_and_never_whether_it_matched(self):
        # Design 11.18.  A block that carried its own verdict would let the
        # wrapper compute a gate number from one reader twice, and my earlier
        # "equality by construction" argument for exactly that was backwards:
        # an equality that cannot fail is not an exit gate.
        rich = mc.rebuild_milestones(self.db, KEY, include_derived=True)
        block = next(iter(rich["derived"].values()))
        for verdict in ("matched", "equivalent", "divergent", "ok",
                        "rebuild_equivalence_derived"):
            self.assertNotIn(verdict, block)
        self.assertEqual(block["milestone_id"],
                         next(iter(rich["derived"])))
        # The digest is of the REBUILT side, computed with the helper the
        # wrapper will use on the stored rows it reads for itself.
        self.assertEqual(block["rebuilt_sha256"],
                         mc.derived_digest(block["rebuilt"]))
        self.assertEqual(block["rebuilt_sha256"],
                         mc.derived_digest(block["stored"]))
        self.assertRegex(block["rebuilt_sha256"], r"\A[0-9a-f]{64}\Z")

    def test_forging_the_stored_diagnostic_cannot_move_the_derived_digest(self):
        # Store's gate test asserts the wrapper's NUMBER does not move when
        # this block's stored side is forged.  This is the half of that
        # property core owns: the digest is of the rebuilt side alone, so a
        # forged stored block leaves it untouched.
        rich = mc.rebuild_milestones(self.db, KEY, include_derived=True)
        block = next(iter(rich["derived"].values()))
        honest = block["rebuilt_sha256"]
        block["stored"] = {"claims_created": [999], "forged": True}
        self.assertEqual(block["rebuilt_sha256"], honest)
        self.assertEqual(mc.derived_digest(block["rebuilt"]), honest)
        # ...and the forged side now digests differently, so the wrapper
        # comparing its own row against this digest would see the mismatch.
        self.assertNotEqual(mc.derived_digest(block["stored"]), honest)

    def test_derived_is_complete_for_the_range_or_names_what_it_skipped(self):
        # A ratio over a partial set is the same failure as a ratio over an
        # empty one, one row later, so a skipped milestone is named rather
        # than silently absent.
        clean = mc.rebuild_milestones(self.db, KEY, include_derived=True)
        self.assertEqual(clean["derived_skipped"], [])
        self.assertEqual(len(clean["derived"]), clean["checked"])
        db = store()
        _messages, record = make_record(count=2)
        milestone = record.milestone_row(created_at="now", spine_event_id=1)
        milestone["invariants_json"] = "{not json"
        columns = ", ".join(milestone)
        marks = ", ".join("?" for _ in milestone)
        db.execute(f"INSERT INTO memory_milestones({columns}) VALUES ({marks})",
                   tuple(milestone.values()))
        partial = mc.rebuild_milestones(db, KEY, include_derived=True)
        self.assertEqual(partial["checked"], 1)
        self.assertEqual(partial["derived"], {})
        self.assertEqual(len(partial["derived_skipped"]), 1)
        # The two together still account for every milestone examined.
        self.assertEqual(len(partial["derived"]) + len(partial["derived_skipped"]),
                         partial["checked"])

    def test_include_derived_shows_both_sides_of_a_divergence(self):
        self.db.execute(
            "UPDATE memory_spine_events SET payload_json=? WHERE id=1",
            (json.dumps({"claim_key": "project:1|relay|port", "claim_id": 77}),))
        rich = mc.rebuild_milestones(self.db, KEY, include_derived=True)
        self.assertEqual(rich["divergent"], 1)
        blocks = next(iter(rich["derived"].values()))
        self.assertEqual(blocks["stored"]["claims_created"], [11])
        self.assertEqual(blocks["rebuilt"]["claims_created"], [77])
        # ...and the divergent side's "stored" is still the row on disk, so a
        # divergence cannot be an artefact of core misreading the table.
        from_table = json.loads(self.db.execute(
            "SELECT invariants_json FROM memory_milestones").fetchone()[0])
        self.assertEqual(spine.canonical(blocks["stored"]),
                         spine.canonical(from_table["derived"]))
        self.assertNotEqual(spine.canonical(blocks["stored"]),
                            spine.canonical(blocks["rebuilt"]))

    def test_include_derived_on_a_refused_store_returns_an_empty_map(self):
        bare = _bare()
        refused = mc.rebuild_milestones(bare, KEY, include_derived=True)
        self.assertEqual(refused["refusal"], "schema_too_old")
        self.assertEqual(refused["derived"], {})
        self.assertEqual(refused["derived_skipped"], [])
        chain = mc.rebuild_milestones(self.db, KEY, spine_ok=False,
                                      include_derived=True)
        self.assertEqual(chain["refusal"], "spine_unverified")
        self.assertNotIn("derived", chain)

    def test_the_rebuild_never_claims_the_chain_it_did_not_check(self):
        # A rebuild replays events AS STORED, so on a forged chain it happily
        # reproduces the forged value and reports divergent 0.  Reading that
        # as "equivalence holds" is the absence-implies-status error, so the
        # caller states what it knows and the result echoes it.
        unstated = mc.rebuild_milestones(self.db, KEY)
        self.assertIsNone(unstated["chain_verified"])
        self.assertEqual(unstated["divergent"], 0)
        self.assertIsNone(unstated["refusal"])
        verified = mc.rebuild_milestones(self.db, KEY, spine_ok=True)
        self.assertTrue(verified["chain_verified"])
        self.assertEqual(verified["equivalent"], 1)
        self.assertIsNone(verified["refusal"])
        # A chain known not to verify yields no equivalence number at all.
        refused = mc.rebuild_milestones(self.db, KEY, spine_ok=False)
        self.assertFalse(refused["chain_verified"])
        self.assertEqual(refused["refusal"], "spine_unverified")
        self.assertIn("spine_unverified", mc.COMPACTION_REFUSAL_CODES)
        self.assertEqual(refused["checked"], 0)
        self.assertEqual(refused["equivalent"], 0)
        self.assertEqual(refused["divergences"], [])

    def test_an_unverified_chain_refuses_before_the_schema_is_even_looked_at(self):
        # compaction-store depends on this ordering and called it something
        # they did not ask for, which is the definition of an emergent
        # property: true today, silently reversible by any refactor that
        # moves the spine_ok check inside the try block.  Pinned here so the
        # reversal fails a test instead of a phase gate.
        bare = _bare()
        self.assertEqual(mc.rebuild_milestones(bare, KEY)["refusal"],
                         "schema_too_old")
        self.assertEqual(mc.rebuild_milestones(bare, KEY, spine_ok=True)["refusal"],
                         "schema_too_old")
        refused = mc.rebuild_milestones(bare, KEY, spine_ok=False)
        self.assertEqual(refused["refusal"], "spine_unverified")
        self.assertFalse(refused["chain_verified"])
        # Fail-closed means the WORSE fact wins: a chain known not to verify
        # is reported even when the store could not have been read anyway.
        self.assertNotEqual(refused["refusal"], "schema_too_old")

    def forge(self, field, value):
        """Rewrite one stored derived field.  The immutability trigger has to
        come down first, which is what a file-level attacker does anyway."""
        row = self.db.execute(
            "SELECT invariants_json FROM memory_milestones").fetchone()[0]
        stored = json.loads(str(row))
        stored["derived"][field] = value
        self.db.execute("DROP TRIGGER IF EXISTS memory_milestones_immutable")
        self.db.execute("UPDATE memory_milestones SET invariants_json=?",
                        (json.dumps(stored),))
        return json.loads(str(row))["derived"]

    def test_forging_an_echoed_field_cannot_buy_an_equivalence_number(self):
        """M-5: three derived fields were echoed from the stored row into the
        rebuilt one, so forging them yielded ``equivalent`` and a 1.0 -- the
        echo hole 11.18 exists to close, surviving in the fields that were not
        part of the digest.

        They cannot be re-derived: the messages are gone.  So they are
        EXCLUDED from the digest and from the comparison, and the honesty they
        used to pretend to comes from the keyed ``invariants_sha256`` instead,
        which is asserted here rather than assumed.
        """
        honest = self.db.execute(
            "SELECT invariants_json FROM memory_milestones").fetchone()[0]
        honest_derived = json.loads(str(honest))["derived"]
        for field in mc.ECHOED_DERIVED_FIELDS:
            with self.subTest(field=field):
                current = honest_derived[field]
                forged = (not current) if isinstance(current, bool) else (
                    current + 99 if isinstance(current, int)
                    else {"after": 0, "through": 999})
                self.forge(field, forged)
                # The equivalence number does not move, because the field is
                # not part of it any more...
                self.assertEqual(
                    mc.derived_digest(json.loads(str(
                        self.db.execute("SELECT invariants_json FROM "
                                        "memory_milestones").fetchone()[0]
                    ))["derived"]),
                    mc.derived_digest(honest_derived),
                    "an echoed field still moves the equivalence digest")
                # ...and the forgery is caught by the keyed digest instead, so
                # excluding it from the number did not excuse it.
                result = mc.verify_compaction(self.db, KEY)
                self.assertFalse(result["ok"])
                self.assertIn("invariants_digest",
                              {problem[1] for problem in result["problems"]})
                self.db.execute("UPDATE memory_milestones SET invariants_json=?",
                                (honest,))
        # Control: verify is clean again, so the loop was not passing on a
        # store that was broken from the start.
        self.assertTrue(mc.verify_compaction(self.db, KEY)["ok"])

    def test_forging_a_replayable_field_still_diverges(self):
        """The other direction: excluding the echoed three must not have
        excluded anything a rebuild CAN check."""
        honest = self.db.execute(
            "SELECT invariants_json FROM memory_milestones").fetchone()[0]
        for field, forged in (("claims_created", [4242]),
                              ("claim_keys", ["project:1|forged|key"]),
                              ("proposals_confirmed", 99),
                              ("outcome", "partial")):
            with self.subTest(field=field):
                self.forge(field, forged)
                result = mc.rebuild_milestones(self.db, KEY)
                self.assertEqual(result["divergent"], 1, field)
                self.assertIn(field, result["divergences"][0]["fields"])
                self.db.execute("UPDATE memory_milestones SET invariants_json=?",
                                (honest,))
        self.assertEqual(mc.rebuild_milestones(self.db, KEY)["equivalent"], 1)

    def test_the_echoed_set_is_exactly_what_cannot_be_re_derived(self):
        self.assertEqual(mc.ECHOED_DERIVED_FIELDS,
                         ("event_range", "screened", "span_has_proposal"))
        block = {"a": 1, "screened": True, "event_range": {}, "b": 2,
                 "span_has_proposal": 3}
        self.assertEqual(mc.replayable_derived(block), {"a": 1, "b": 2})
        # The digest ignores them, and notices everything else.
        self.assertEqual(mc.derived_digest(block),
                         mc.derived_digest({"a": 1, "b": 2}))
        self.assertNotEqual(mc.derived_digest(block),
                            mc.derived_digest({"a": 1, "b": 3}))

    def test_an_unreadable_invariants_column_is_reported_not_raised(self):
        db = store()
        _messages, record = make_record(count=2)
        milestone = record.milestone_row(created_at="now", spine_event_id=1)
        milestone["invariants_json"] = "{not json"
        columns = ", ".join(milestone)
        marks = ", ".join("?" for _ in milestone)
        db.execute(f"INSERT INTO memory_milestones({columns}) VALUES ({marks})",
                   tuple(milestone.values()))
        result = mc.rebuild_milestones(db, KEY)
        self.assertEqual(result["divergences"][0]["kind"], "invariants_unreadable")

    def test_a_store_without_the_tables_or_a_broken_one_refuses(self):
        bare = _bare()
        self.assertEqual(mc.rebuild_milestones(bare, KEY)["refusal"],
                         "schema_too_old")
        self.db.close()
        broken = mc.rebuild_milestones(self.db, KEY)
        self.assertEqual(broken["refusal"], "error")
        self.assertEqual(broken["refusal_detail"], "ProgrammingError")

    def test_span_bounds_from_a_milestone_carry_the_recorded_shape(self):
        milestone = self.record.milestone_row(created_at="now", spine_event_id=1)
        derived = json.loads(self.record.invariants_json)["derived"]
        recovered = mc.span_bounds_from_milestone(milestone, derived)
        self.assertEqual(recovered.conversation_id, self.record.conversation_id)
        self.assertEqual(recovered.first_message_id, self.record.first_message_id)
        self.assertEqual(recovered.message_count, self.record.message_count)
        self.assertEqual(recovered.message_ids, ())
        self.assertEqual(recovered.last_created_at, "")


class SpineRowAdapterTests(unittest.TestCase):
    def test_a_row_with_an_unreadable_payload_becomes_payload_none(self):
        db = store()
        db.row_factory = sqlite3.Row
        db.execute("INSERT INTO memory_spine_events(id, created_at, kind, outcome, "
                   "conversation_id, subject_kind, subject_id, payload_json) "
                   "VALUES (1, 'a', 'claim.created', 'applied', 1, 'claim', 5, ?)",
                   (json.dumps({"claim_key": "k"}),))
        db.execute("INSERT INTO memory_spine_events(id, created_at, kind, outcome, "
                   "conversation_id, subject_kind, subject_id, payload_json) "
                   "VALUES (2, 'b', 'claim.created', 'applied', NULL, NULL, NULL, "
                   "'{not json')")
        db.execute("INSERT INTO memory_spine_events(id, created_at, kind, outcome, "
                   "conversation_id, subject_kind, subject_id, payload_json) "
                   "VALUES (3, 'c', 'claim.created', 'applied', 1, 'claim', 6, '[]')")
        rows = mc.spine_event_rows(db.execute(
            "SELECT id, created_at, kind, outcome, conversation_id, subject_kind, "
            "subject_id, payload_json FROM memory_spine_events ORDER BY id"))
        self.assertEqual(rows[0].payload, {"claim_key": "k"})
        self.assertEqual(rows[0].subject_id, 5)
        self.assertIsNone(rows[1].payload)
        self.assertIsNone(rows[1].conversation_id)
        self.assertIsNone(rows[1].subject_kind)
        self.assertIsNone(rows[2].payload)


class StoreRehydrateTests(unittest.TestCase):
    def setUp(self):
        self.db = store()
        self.messages, self.record = make_record(count=4)
        write_record(self.db, self.record)

    def test_a_live_handle_returns_the_original_rows(self):
        result = mc.rehydrate(self.db, KEY, self.record.handle)
        self.assertEqual([m["content"] for m in result["messages"]],
                         [m.content for m in self.messages])

    def test_an_unknown_or_erased_handle_is_refused(self):
        missing = mc.handle_for(1, 9, "c" * 64)
        with self.assertRaises(mc.RehydrationError) as caught:
            mc.rehydrate(self.db, KEY, missing)
        self.assertEqual(caught.exception.code, "unknown_handle")
        self.db.execute("DELETE FROM memory_compacted_spans")
        with self.assertRaises(mc.RehydrationError) as erased:
            mc.rehydrate(self.db, KEY, self.record.handle)
        self.assertEqual(erased.exception.code, "erased")

    def test_a_milestone_filed_under_another_conversation_is_unknown(self):
        db = store()
        _messages, record = make_record(count=2, conversation_id=1)
        milestone = record.milestone_row(created_at="now", spine_event_id=1)
        milestone["conversation_id"] = 2
        columns = ", ".join(milestone)
        marks = ", ".join("?" for _ in milestone)
        db.execute("INSERT INTO conversations(id) VALUES (2)")
        db.execute(f"INSERT INTO memory_milestones({columns}) VALUES ({marks})",
                   tuple(milestone.values()))
        db.execute("INSERT INTO memory_compacted_spans(handle, milestone_id, "
                   "conversation_id, body, body_chars) VALUES (?, 1, 2, ?, ?)",
                   (record.handle, record.body, record.body_chars))
        with self.assertRaises(mc.RehydrationError) as caught:
            mc.rehydrate(db, KEY, record.handle)
        self.assertEqual(caught.exception.code, "unknown_handle")

    def test_a_store_without_the_tables_or_a_broken_one_is_store_unavailable(self):
        bare = _bare()
        with self.assertRaises(mc.RehydrationError) as absent:
            mc.rehydrate(bare, KEY, self.record.handle)
        self.assertEqual(absent.exception.code, "store_unavailable")
        self.db.close()
        with self.assertRaises(mc.RehydrationError) as broken:
            mc.rehydrate(self.db, KEY, self.record.handle)
        self.assertEqual(broken.exception.code, "store_unavailable")

    def test_a_malformed_handle_never_reaches_the_store(self):
        with self.assertRaises(mc.RehydrationError) as caught:
            mc.rehydrate(self.db, KEY, "not a handle")
        self.assertEqual(caught.exception.code, "malformed_handle")


class SpineContractTests(unittest.TestCase):
    """The spine half of M5 (items 15-19), asserted from the compaction side.

    These live here because this module is the only reader of the contract
    that is not the spine itself: a payload key spelled from memory on the
    reading side cost the M4 build a debugging cycle, and two independent
    literals plus a comparison is the guard against that.
    """

    def test_the_kind_is_in_the_frozenset_and_in_the_table_check(self):
        self.assertIn(mc.COMPACTION_SPINE_KIND, spine.SPINE_KINDS)
        # The frozenset and the SQL CHECK are two lists that must not drift:
        # M4's HIGH-1 was exactly a CHECK never widened beside its frozenset.
        self.assertIn(f"'{mc.COMPACTION_SPINE_KIND}'", spine._EVENT_TABLE_SQL)
        self.assertEqual(spine.SPINE_SCHEMA_VERSION,
                         mc.COMPACTION_SPINE_SCHEMA_VERSION)
        # conversation is already a legal subject kind, so nothing widened.
        self.assertIn("conversation", spine.SPINE_SUBJECT_KINDS)

    def test_every_kind_in_the_frozenset_appears_in_the_table_check(self):
        # The general form of the drift above, so a future kind cannot be
        # added to one list only.
        missing = sorted(
            kind for kind in spine.SPINE_KINDS
            if f"'{kind}'" not in spine._EVENT_TABLE_SQL
        )
        self.assertEqual(missing, [])

    def test_the_contract_the_spine_publishes_is_the_one_this_module_writes(self):
        required, allowed = spine.payload_keys(mc.COMPACTION_SPINE_KIND)
        self.assertEqual(required, mc.COMPACTED_REQUIRED_KEYS)
        self.assertEqual(allowed, mc.COMPACTED_PAYLOAD_KEYS)
        deleted_required, deleted_allowed = spine.payload_keys("conversation.deleted")
        self.assertEqual(deleted_required, mc.CONVERSATION_DELETED_REQUIRED_KEYS)
        self.assertEqual(deleted_allowed, mc.CONVERSATION_DELETED_PAYLOAD_KEYS)

    def test_conversation_deleted_is_no_longer_unvalidated(self):
        # M-9's real fix at schema 49: a REMOVAL from the unconstrained set,
        # not a branch added to a fall-through the M4 rewrite deleted.
        self.assertNotIn("conversation.deleted", spine.UNCONSTRAINED_PAYLOAD_KINDS)
        self.assertIn("spine.genesis", spine.UNCONSTRAINED_PAYLOAD_KINDS)

    def test_a_real_payload_validates_and_an_edited_one_is_refused(self):
        _messages, record = make_record(count=4)
        payload = record.spine_payload(at="2026-09-04T10:00:00Z")
        clean = spine.validate_payload(mc.COMPACTION_SPINE_KIND, payload)
        self.assertEqual(set(clean), set(payload))
        # An extra key and a missing key are both refused -- the E-6 pair.
        extra = dict(payload, smuggled="content")
        with self.assertRaises(spine.SpineError):
            spine.validate_payload(mc.COMPACTION_SPINE_KIND, extra)
        for name in sorted(mc.COMPACTED_REQUIRED_KEYS):
            short = {k: v for k, v in payload.items() if k != name}
            with self.assertRaises(spine.SpineError):
                spine.validate_payload(mc.COMPACTION_SPINE_KIND, short)

    def test_the_type_bounds_refuse_a_malformed_payload(self):
        _messages, record = make_record(count=4)
        good = record.spine_payload(at="2026-09-04T10:00:00Z")
        for name, bad in (
            ("span_sha256", "not a digest"),
            ("key_fingerprint", "z" * 64),
            ("handle", ""),
            ("message_count", 0),
            ("seq", -1),
            ("author", "model"),
            ("screened", "no"),
            ("event_range", {"after": 5, "through": 1}),
            ("event_range", {"after": 1}),
            ("excluded_by_screen", -1),
            ("model", "claude"),
            ("last_message_id", 0),
        ):
            with self.assertRaises(spine.SpineError, msg=name):
                spine.validate_payload(mc.COMPACTION_SPINE_KIND, dict(good, **{name: bad}))
        # ...and the untouched payload still passes, so the loop above is not
        # rejecting everything.
        self.assertTrue(spine.validate_payload(mc.COMPACTION_SPINE_KIND, good))

    def test_a_conversation_deleted_payload_is_now_bounded(self):
        self.assertTrue(spine.validate_payload(
            "conversation.deleted", {"messages_removed": 3}))
        self.assertTrue(spine.validate_payload(
            "conversation.deleted",
            {"messages_removed": 3, "milestones_removed": 1, "spans_removed": 1}))
        for bad in ({}, {"messages_removed": -1}, {"messages_removed": "three"},
                    {"messages_removed": 3, "spans_removed": -2},
                    {"messages_removed": 3, "invented": 1}):
            with self.assertRaises(spine.SpineError):
                spine.validate_payload("conversation.deleted", bad)

    def test_the_tombstone_gained_the_two_capped_milestone_keys(self):
        _required, allowed = spine.payload_keys("claim.tombstoned")
        self.assertIn("removed_milestone_ids", allowed)
        self.assertIn("removed_span_handles", allowed)
        self.assertEqual(spine.MILESTONE_TOMBSTONE_MAX_IDS,
                         mc.MILESTONE_TOMBSTONE_MAX_IDS)
        self.assertEqual(spine.MILESTONE_TOMBSTONE_MAX_IDS,
                         spine.MEMORY_DELETED_MAX_IDS)

    def test_rebuild_milestones_reuses_the_projection_receipt(self):
        # A derived rebuild reuses projection.rebuilt; only a real mutation
        # earns a kind.
        self.assertIn("milestones", spine._REBUILT_PROJECTIONS)
        self.assertIn("claims", spine._REBUILT_PROJECTIONS)
        self.assertNotIn("milestones.rebuilt", spine.SPINE_KINDS)


#: The frozen pre-M5 store the migration tests run against, and the key its
#: keyed digests verify under.  See the header of the .sql file for how it was
#: captured and why it is a dump rather than a ``git archive``: the commit it
#: came from is a local intermediate that ceases to exist when this branch
#: squashes to one commit, and archiving it would also fail on a shallow clone
#: and in a source tarball with no ``.git``.
LEGACY_DUMP = Path(__file__).resolve().parent / "fixtures" / "m5_legacy_store_schema49.sql"
LEGACY_KEY = bytes.fromhex("5a" * 32)
#: Separates the two restore passes; see the dump header for why.
LEGACY_PASS_SPLIT = ("-- ==== PASS 2: virtual tables, their rebuild, and their triggers ====")


class SpineMigrationTests(unittest.TestCase):
    """Migration 49 against a store the code that predates it actually built.

    Two disciplines the boss made binding, each paid for tonight:

    * The store is restored from a **frozen dump captured from the M4 tree**,
      so the events table really carries the pre-M5 CHECK and M4's triggers
      really exist.  A store the working tree builds is already widened, and
      every test against one passes by never running the copy at all.
    * **The precondition is asserted before the property.**  Each test proves
      the rebuild actually ran -- the CHECK moved, the rows were copied --
      before asserting the migration was correct.  A test that cannot tell
      "migrated correctly" from "found nothing to do" measures nothing and
      passes right up to the upgrade that matters.
    """

    def legacy_copy(self):
        """A private store restored from the frozen dump, with its sidecar.

        **Two passes, and the reopen between them is the point.**  Pass 1 is
        the base schema, its rows and its ordinary triggers.  Pass 2 is the
        FTS5 virtual tables, their external-content rebuild and their six
        triggers, and it must run on a FRESH connection: a connection that
        has just written a virtual table cannot use it until it reloads the
        schema, which is why a single-pass restore of an ``iterdump`` dies
        with ``no such table: memory_fts``.
        """
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, True)
        path = os.path.join(directory, "m.db")
        dump = LEGACY_DUMP.read_text(encoding="utf-8")
        self.assertIn(LEGACY_PASS_SPLIT, dump, "the dump lost its pass marker")
        first, second = dump.split(LEGACY_PASS_SPLIT, 1)
        for chunk in (first, second):
            connection = sqlite3.connect(path)
            try:
                connection.executescript(chunk)
                connection.commit()
            finally:
                connection.close()
        Path(path + spine.KEY_SIDECAR_SUFFIX).write_text(
            LEGACY_KEY.hex(), encoding="ascii")
        return path, LEGACY_KEY

    def opened(self, path):
        db = sqlite3.connect(path)
        db.row_factory = sqlite3.Row
        self.addCleanup(db.close)
        return db

    def events_sql(self, db):
        return str(db.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' "
            "AND name='memory_spine_events'").fetchone()[0])

    def schema_objects(self, db):
        """Every named object in the schema, whatever its kind."""
        return {
            (str(kind), str(name)) for kind, name in db.execute(
                "SELECT type, name FROM sqlite_master WHERE name IS NOT NULL")
        }

    def trigger_names(self, db):
        return {str(name) for (name,) in db.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger'")}

    def dependent_triggers(self, db):
        return {str(name) for (name,) in db.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' "
            "AND sql LIKE '%memory_spine_events%' "
            "AND tbl_name != 'memory_spine_events'")}

    def migrate(self, db, key):
        """v49 with the trigger bracketing the caller owns."""
        spine.drop_spine_triggers(db)
        try:
            return spine.migrate_memory_spine_v49(
                db, key, now="2026-09-04T11:00:00Z")
        finally:
            spine.create_spine_triggers(db)

    def test_the_dump_really_gives_a_pre_m5_store(self):
        """The precondition every other test in this class rests on.

        Also the guard against the fixture DRIFTING into a modern store: if
        someone re-dumps from a current tree, the CHECK below carries
        ``transcript.compacted`` and this fails loudly, rather than every
        migration test quietly starting to migrate nothing.
        """
        path, key = self.legacy_copy()
        db = self.opened(path)
        self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 49)
        self.assertNotIn("'transcript.compacted'", self.events_sql(db))
        self.assertGreater(
            db.execute("SELECT COUNT(*) FROM memory_spine_events").fetchone()[0], 0)
        # M4 really is present, so the trigger hazard is reachable here.
        unknown = self.dependent_triggers(db) - set(spine._TRIGGER_SQL)
        self.assertIn("ladder_promotions_require_spine_event", unknown)
        # The dump restored a coherent, verifying spine under the recorded key
        # -- so a later digest failure is the migration, not the fixture.
        self.assertTrue(spine.verify_spine(db, key)["ok"])
        # And the fixture is portable: no .git, no network, no subprocess.
        self.assertTrue(LEGACY_DUMP.is_file())
        # FTS5 MUST be present (boss ruling).  The defect this fixture exists
        # to catch is a rename re-parsing every trigger in the schema, and a
        # trigger nobody listed breaking it; FTS brings six such triggers.  A
        # fixture that quietly loses a whole class of real triggers cannot
        # reproduce the class of failure it was built for.
        virtual = {
            str(name) for (name,) in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND sql LIKE '%VIRTUAL TABLE%'")
        }
        self.assertEqual(virtual, {"memory_fts", "message_fts"})
        fts_triggers = {
            str(name) for (name,) in db.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' "
                "AND (sql LIKE '%memory_fts%' OR sql LIKE '%message_fts%')")
        }
        self.assertEqual(len(fts_triggers), 6, sorted(fts_triggers))
        # They are usable, not just declared: pass 2 rebuilt the index from
        # the content tables, so a query returns the seeded rows.
        self.assertTrue(db.execute(
            "SELECT COUNT(*) FROM message_fts WHERE message_fts MATCH ?",
            ("relay",)).fetchone()[0])

    def test_the_migration_does_the_work_and_the_work_is_correct(self):
        path, key = self.legacy_copy()
        db = self.opened(path)
        rows_before = db.execute(
            "SELECT COUNT(*) FROM memory_spine_events").fetchone()[0]
        triggers_before = self.trigger_names(db)
        self.assertNotIn("'transcript.compacted'", self.events_sql(db))

        result = self.migrate(db, key)

        # PRECONDITION: the copy ran and the rename executed.
        self.assertEqual(result["events_table_rebuilt"], 1)
        self.assertIn("'transcript.compacted'", self.events_sql(db))
        # PROPERTY: and having done the work, it did it correctly.
        self.assertGreater(rows_before, 0)
        self.assertEqual(
            db.execute("SELECT COUNT(*) FROM memory_spine_events").fetchone()[0],
            rows_before)
        self.assertEqual(self.trigger_names(db), triggers_before)
        self.assertTrue(spine.verify_spine(db, key)["ok"])

    def test_the_rebuild_restores_triggers_drop_spine_triggers_cannot_know(self):
        """The 11.21 defect, pinned against the store that exposes it.

        ``drop_spine_triggers`` knows only ``_TRIGGER_SQL``.  M4 added two
        triggers on OTHER tables that reference ``memory_spine_events``, and
        SQLite re-parses every trigger on ``ALTER TABLE ... RENAME``.  With the
        discovery in ``_rebuild_events_table`` reverted, this exact store fails
        with ``OperationalError: no such table: main.memory_spine_events``
        AFTER the old table has been dropped.
        """
        path, key = self.legacy_copy()
        db = self.opened(path)
        dependent = self.dependent_triggers(db)
        unknown = dependent - set(spine._TRIGGER_SQL)
        self.assertTrue(unknown, "no trigger outside _TRIGGER_SQL to protect")

        # PRECONDITION: the rename really executed on this store.
        self.assertEqual(self.migrate(db, key)["events_table_rebuilt"], 1)

        surviving = self.trigger_names(db)
        self.assertTrue(dependent <= surviving)
        self.assertTrue(unknown <= surviving)
        # They still FIRE, which is the point of restoring them.
        with self.assertRaises(sqlite3.IntegrityError):
            db.execute("INSERT INTO memory_claims(scope, claim_key, subject, "
                       "predicate, value, value_sha256, status, authority, "
                       "confidence, source, valid_from, created_at, updated_at) "
                       "VALUES ('global','k','s','p','v',?,'active','operator',"
                       "1.0,'t','x','x','x')", ("a" * 64,))

    def test_a_second_migration_finds_nothing_to_do(self):
        path, key = self.legacy_copy()
        db = self.opened(path)
        self.assertEqual(self.migrate(db, key)["events_table_rebuilt"], 1)
        # Idempotent: having done the work once, it does none the second time.
        self.assertEqual(self.migrate(db, key)["events_table_rebuilt"], 0)
        self.assertTrue(spine.verify_spine(db, key)["ok"])

    def test_the_rename_survives_any_dependent_schema_object(self):
        """M-1, red team: the hazard is every object SQLite re-parses on a
        rename, not the one KIND of object that happened to break us first.

        The trigger fix discovered triggers.  Views were always in the same
        class and were left out, and the symptom is worse: no data loss, but
        the store cannot be opened at all.  The fix is
        ``PRAGMA legacy_alter_table``, which makes the rename touch no other
        object, so a fourth kind nobody has named is covered too.
        """
        extras = {
            "view": "CREATE VIEW spine_recent AS SELECT id, kind "
                    "FROM memory_spine_events ORDER BY id DESC LIMIT 10;",
            "foreign key": "CREATE TABLE audit_note (id INTEGER PRIMARY KEY, "
                           "event_id INTEGER, FOREIGN KEY(event_id) "
                           "REFERENCES memory_spine_events(id));",
            "trigger on another table": "CREATE TABLE audit_log (id INTEGER "
                    "PRIMARY KEY, note TEXT); CREATE TRIGGER audit_needs_event "
                    "BEFORE INSERT ON audit_log FOR EACH ROW WHEN NOT EXISTS "
                    "(SELECT 1 FROM memory_spine_events) BEGIN SELECT "
                    "RAISE(ABORT, 'no events'); END;",
        }
        combined = "".join(extras.values())
        for label, extra in list(extras.items()) + [("all three", combined)]:
            with self.subTest(dependent=label):
                path, key = self.legacy_copy()
                connection = sqlite3.connect(path)
                connection.executescript(extra)
                connection.commit()
                connection.close()
                db = self.opened(path)
                rows_before = db.execute(
                    "SELECT COUNT(*) FROM memory_spine_events").fetchone()[0]
                objects_before = self.schema_objects(db)
                # PRECONDITION: the rename really runs on this store.
                self.assertEqual(self.migrate(db, key)["events_table_rebuilt"], 1)
                self.assertIn("'transcript.compacted'", self.events_sql(db))
                self.assertEqual(
                    db.execute("SELECT COUNT(*) FROM memory_spine_events"
                               ).fetchone()[0], rows_before)
                # Every dependent object survives, whatever kind it is.
                self.assertEqual(self.schema_objects(db), objects_before)
                self.assertTrue(spine.verify_spine(db, key)["ok"])
                # ...and the store still opens, which is what M-1 broke.
                db.execute("SELECT COUNT(*) FROM memory_spine_events").fetchone()

    def test_an_unknown_events_column_is_refused_not_silently_dropped(self):
        """M-2: the copy names columns, so a column this build does not know
        would be dropped with its data and no message.  Preserving it is
        impossible -- the new table has nowhere to put it -- so refuse."""
        path, key = self.legacy_copy()
        connection = sqlite3.connect(path)
        connection.executescript(
            "ALTER TABLE memory_spine_events ADD COLUMN operator_note TEXT;")
        connection.commit()
        connection.close()
        db = self.opened(path)
        with self.assertRaises(spine.SpineError) as caught:
            self.migrate(db, key)
        self.assertEqual(getattr(caught.exception, "code", None),
                         "events_table_unknown_columns")
        self.assertIn("operator_note", str(caught.exception))
        # Refused means REFUSED: the column is still on the table and no row
        # was copied away from under it.  (The value cannot be seeded here --
        # memory_spine_events_redaction_only permits only a tombstone
        # redaction UPDATE -- so the column and the row count are what this
        # can honestly assert.)
        self.assertIn("operator_note",
                      {str(name) for name in spine._table_columns(
                          db, "memory_spine_events")})
        self.assertGreater(db.execute(
            "SELECT COUNT(*) FROM memory_spine_events").fetchone()[0], 0)
        self.assertNotIn("'transcript.compacted'", self.events_sql(db))
        # And the control: without the extra column the same store migrates,
        # so the guard is not refusing everything.
        clean_path, clean_key = self.legacy_copy()
        clean = self.opened(clean_path)
        self.assertEqual(self.migrate(clean, clean_key)["events_table_rebuilt"], 1)

    def test_the_migration_refuses_a_spineless_store_and_a_non_key(self):
        empty = _bare()
        with self.assertRaises(spine.SpineError) as absent:
            spine.migrate_memory_spine_v49(empty, KEY, now="x")
        self.assertEqual(getattr(absent.exception, "code", None), "spine_missing")
        path, key = self.legacy_copy()
        db = self.opened(path)
        with self.assertRaises(spine.SpineError):
            spine.migrate_memory_spine_v49(db, "not bytes", now="x")
        # ...and the real key still migrates, so the guard is not blanket.
        self.assertEqual(self.migrate(db, key)["events_table_rebuilt"], 1)

    def test_a_swapped_key_migrates_but_the_operator_is_still_told(self):
        """Migrating quietly under a key that cannot vouch for the chain is
        right; migrating SILENTLY is not."""
        path, real = self.legacy_copy()
        db = self.opened(path)
        swapped = OTHER_KEY
        self.assertTrue(spine.verify_spine(db, real)["ok"])
        before = spine.verify_spine(db, swapped)
        self.assertFalse(before["ok"])
        # Named as a KEY problem, not as tampering (H-7 / N-4).
        self.assertFalse(before["key_ok"])

        # PRECONDITION: it really migrated rather than finding nothing to do.
        self.assertEqual(self.migrate(db, swapped)["events_table_rebuilt"], 1)

        after_swapped = spine.verify_spine(db, swapped)
        self.assertFalse(after_swapped["ok"])
        self.assertFalse(after_swapped["key_ok"])
        after_real = spine.verify_spine(db, real)
        self.assertTrue(after_real["ok"])
        self.assertTrue(after_real["key_ok"])


class TombstoneChunkingTests(unittest.TestCase):
    """An erase naming more milestones than the cap must RECEIPT them all.

    The code truncated at ``MILESTONE_TOMBSTONE_MAX_IDS`` while its own key-set
    comment and the ``MEMORY_DELETED_MAX_IDS`` precedent it cites both said
    chunk, so milestones past the 128th were deleted and named in no receipt --
    a hole in the audit trail wearing a cap's clothing.  Found by an
    independent correctness review.
    """

    def store_with_claim(self, milestones):
        """A real store, one governed claim, and ``milestones`` planted rows
        whose invariants name that claim key.  Returns (memory, key, ids)."""
        from jarvis.memory import Memory
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, True)
        memory = Memory(os.path.join(directory, "m.db"))
        self.addCleanup(memory.close)
        conversation = memory.new_conversation("erase talk")
        memory.remember_explicit_project_claim(
            conversation, 1,
            'Remember this project fact: {"subject":"Millrace weir",'
            '"predicate":"gate count","value":"four"}',
        )
        claim_key = str(memory.db.execute(
            "SELECT claim_key FROM memory_claims ORDER BY id DESC LIMIT 1"
        ).fetchone()[0])
        event_id = int(memory.db.execute(
            "SELECT MAX(id) FROM memory_spine_events").fetchone()[0])
        planted = []
        for index in range(milestones):
            handle = mc.handle_for(conversation, index + 1, f"{index:064x}")
            memory.db.execute(
                "INSERT INTO memory_milestones(created_at, conversation_id, seq,"
                " first_message_id, last_message_id, message_count, source_chars,"
                " stored_bytes, summary, summary_chars, invariants_json, handle,"
                " span_sha256, span_unkeyed_sha256, summary_sha256,"
                " invariants_sha256, key_fingerprint, author, spine_event_id)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'runtime', ?)",
                ("2026-09-05T00:00:00Z", conversation, index + 1, 1, 2, 2, 10, 10,
                 "s", 1, json.dumps({"v": 2, "derived": {"claim_keys": [claim_key]}}),
                 handle, "a" * 64, f"{index:064x}", "c" * 64, "d" * 64, "e" * 64,
                 event_id),
            )
            planted.append(handle)
        memory.db.commit()
        return memory, claim_key, conversation, planted

    def tombstone_payloads(self, memory):
        rows = memory.db.execute(
            "SELECT payload_json FROM memory_spine_events "
            "WHERE kind='claim.tombstoned' ORDER BY id").fetchall()
        return [json.loads(str(row[0])) for row in rows]

    def test_an_erase_under_the_cap_still_uses_one_receipt(self):
        # The control: chunking must not fire when it is not needed, or the
        # test below cannot tell chunking from a change in event count.
        memory, _key, conversation, planted = self.store_with_claim(5)
        memory.erase_explicit_project_claim(
            conversation, 1,
            'Erase this project fact: {"subject":"Millrace weir",'
            '"predicate":"gate count"}',
        )
        payloads = self.tombstone_payloads(memory)
        self.assertEqual(len(payloads), 1)
        self.assertEqual(len(payloads[0]["removed_milestone_ids"]), 5)
        self.assertEqual(sorted(payloads[0]["removed_span_handles"]),
                         sorted(planted))

    def test_an_erase_over_the_cap_receipts_every_milestone(self):
        cap = spine.MILESTONE_TOMBSTONE_MAX_IDS
        total = cap * 2 + 17
        memory, _key, conversation, planted = self.store_with_claim(total)
        live = {int(row[0]) for row in memory.db.execute(
            "SELECT id FROM memory_milestones")}
        self.assertEqual(len(live), total, "the fixture did not plant them all")

        memory.erase_explicit_project_claim(
            conversation, 1,
            'Erase this project fact: {"subject":"Millrace weir",'
            '"predicate":"gate count"}',
        )

        payloads = self.tombstone_payloads(memory)
        # PRECONDITION: chunking actually happened rather than the cap simply
        # not being reached.
        self.assertEqual(len(payloads), 3, "expected 128 + 128 + 17")
        for payload in payloads:
            self.assertLessEqual(len(payload["removed_milestone_ids"]), cap)
            self.assertEqual(len(payload["removed_milestone_ids"]),
                             len(payload["removed_span_handles"]))
        # PROPERTY: every milestone is named exactly once across the chain.
        named, handles = [], []
        for payload in payloads:
            named.extend(payload["removed_milestone_ids"])
            handles.extend(payload["removed_span_handles"])
        self.assertEqual(sorted(named), sorted(live))
        self.assertEqual(len(named), len(set(named)), "a milestone named twice")
        self.assertEqual(sorted(handles), sorted(planted))
        # The claim ids ride on the FIRST receipt only, so a replay cannot
        # double-count them.
        self.assertTrue(payloads[0]["removed_claim_ids"])
        for payload in payloads[1:]:
            self.assertEqual(payload["removed_claim_ids"], [])
        # And the rows really are gone, so this is an erase and not a report.
        self.assertEqual(memory.db.execute(
            "SELECT COUNT(*) FROM memory_milestones").fetchone()[0], 0)
        self.assertEqual(memory.db.execute(
            "SELECT COUNT(*) FROM memory_compacted_spans").fetchone()[0], 0)
        # The chain still verifies with the overflow events on it.
        key = bytes.fromhex(Path(
            memory.db_path + spine.KEY_SIDECAR_SUFFIX
        ).read_bytes().decode().strip()) if hasattr(memory, "db_path") else None
        if key is not None:
            self.assertTrue(spine.verify_spine(memory.db, key)["ok"])


# --- 14. the runtime pin ----------------------------------------------------

class RuntimePinTests(unittest.TestCase):
    def test_the_pin_covers_the_designs_four_files_and_not_the_agent(self):
        self.assertEqual(mc.COMPACTION_RUNTIME_FILES, (
            "jarvis/memory.py", "jarvis/memory_compaction.py",
            "jarvis/memory_spine.py", "jarvis/redaction.py"))
        self.assertNotIn("jarvis/agent.py", mc.COMPACTION_RUNTIME_FILES)
        self.assertIs(mc.memory_compaction_runtime_sha256,
                      mc.compaction_runtime_sha256)

    def test_the_pin_is_stable_and_moves_when_a_pinned_file_moves(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "jarvis").mkdir()
            for name in mc.COMPACTION_RUNTIME_FILES:
                (root / name).write_bytes(b"original\n")
            first = mc.compaction_runtime_sha256(root)
            self.assertEqual(first, mc.compaction_runtime_sha256(root))
            self.assertRegex(first, r"\A[0-9a-f]{64}\Z")
            (root / "jarvis/redaction.py").write_bytes(b"changed\n")
            self.assertNotEqual(mc.compaction_runtime_sha256(root), first)
            # An unpinned file moving does not move the pin.
            (root / "jarvis/agent.py").write_bytes(b"anything\n")
            (root / "jarvis/redaction.py").write_bytes(b"original\n")
            self.assertEqual(mc.compaction_runtime_sha256(root), first)

    def test_the_pin_reads_the_real_tree_by_default(self):
        self.assertRegex(mc.compaction_runtime_sha256(), r"\A[0-9a-f]{64}\Z")


# --- 15. source hygiene -----------------------------------------------------

class SourceTests(unittest.TestCase):
    def test_both_owned_files_are_lf_only_ascii_safe_and_free_of_nuls(self):
        for name in ("jarvis/memory_compaction.py",
                     "tests/test_memory_compaction.py"):
            path = Path(__file__).resolve().parent.parent / name
            raw = path.read_bytes()
            self.assertEqual(raw.count(b"\r"), 0, name)
            self.assertEqual(raw.count(b"\x00"), 0, name)
        tests = (Path(__file__).resolve()).read_bytes()
        self.assertTrue(tests.decode("utf-8").isascii())

    def test_no_test_case_here_shadows_unittests_outcome_attribute(self):
        for value in list(globals().values()):
            if isinstance(value, type) and issubclass(value, unittest.TestCase):
                self.assertNotIn("_outcome", vars(value), value.__name__)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
