"""The temporal memory graph: entities, validity-interval edges, and bounded
traversal as retrieval channel 3 (VTMF M3, schema 48).

The graph is a **projection of the claim projection**, which is itself a
projection of the spine: every non-excluded ``memory_claims`` row (every
version) becomes exactly one edge whose primary key is the claim id, and every
admitted subject or value becomes one entity per scope.  Nothing here is
authoritative: ``rebuild_graph_projection`` recomputes the whole graph from the
live claim rows and ``reproject`` reconciles it in place, so a lost or tampered
graph is repaired, never trusted.

Three rules the rest of the module exists to keep:

* **Nothing that fails a screen becomes a node.**  A subject that carries a
  secret or a widened private identifier (``redaction.screen_endpoint``)
  excludes its whole claim from the projection; such a value is a literal
  terminal, never joinable.  The write path screens a subject for secrets
  only, so node admission is the first place a private identifier stored as a
  subject is caught.
* **A chain never crosses a scope boundary.**  Edges carry the claim's scope
  and claim key and are filtered by the caller's shadowing predicate, the same
  one the claims lane builds.
* **A bounded answer is never presented as a complete one.**  Fan-out caps,
  the node and edge budgets and the one whole-call deadline all surface as an
  explicit overflow entry or an ``incomplete`` marker on every row of the
  chain they truncated.

This module owns only SQL on a caller-supplied connection and pure functions;
``Memory`` owns ``user_version``, transactions, locking, the key and the query
screens.  See ``docs/MEMORY_GRAPH.md`` and the M3 design.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
import unicodedata
from collections import deque
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from . import memory_spine
from .redaction import screen_endpoint

GRAPH_SCHEMA_VERSION = 48

# --- bounds ----------------------------------------------------------------
# An entity label is display-only and never model-facing, but it bounds what a
# subject may be: ``memory_graph_edges.src_entity_id`` is NOT NULL, so a
# subject whose key is longer than this is an exclusion, not a truncation.
ENTITY_LABEL_MAX_CHARS = 80
MAX_HOPS = 3
FANOUT_CAP = 16
FANOUT_CAP_FILTERED = 32
# The last hop reads answers rather than a frontier to expand, so its cap is
# larger: a 40-in-edge hub overflowed at 16 and at 32 and could never answer.
FANOUT_CAP_TERMINAL = 64
NODE_BUDGET = 48
EDGE_BUDGET = 96
SCREENED_ROW_CAP = 24
CHAIN_ROW_CAP = 8
CHAIN_CAP = 2
OVERFLOW_NOTE_CAP = 2
# One budget for the whole ``graph_chains`` call, not for the traversal loop:
# the screen phase alone measures 14.6 ms for 24 rows.
TIME_BUDGET_MS = 25.0

GRAPH_TABLES: tuple[str, ...] = (
    "memory_graph_edges",
    "memory_graph_entities",
    "memory_graph_entity_sequence",
)
# Edges first: the entity tables are their foreign-key targets.
DROP_GRAPH_SQL: tuple[str, ...] = tuple(
    f"DROP TABLE IF EXISTS {name}" for name in GRAPH_TABLES
)
EXCLUSION_KINDS: tuple[str, ...] = (
    "excluded_predicate", "subject_private", "subject_too_long",
)
VALUE_KINDS: tuple[str, ...] = ("entity", "literal")
CURRENT_STATUSES: frozenset[str] = frozenset({"active", "disputed"})
# The closed report mode set of design §5.6.
GRAPH_MODES: frozenset[str] = frozenset({
    "idle", "screened", "project-unavailable", "no-start", "identity-conflict",
    "overflow", "budget-exceeded", "screened-rows", "no-answer", "complete",
    "error",
})
VERIFY_PROBLEM_KINDS: frozenset[str] = frozenset({
    "missing_edge", "extra_edge", "field", "entity_key", "orphan_entity",
    "sequence", "screen", "label",
})
# A verbatim copy of ``governed_memory._RESERVED_PREDICATE_NAMESPACE``: a Codex
# boundary that is copied and equality-tested, never loosened.  Identity,
# permission, preference and safety rows are not facts to chain through, and
# the global claim API stores them (every preference is a global claim).
EXCLUDED_PREDICATE_NAMESPACE = re.compile(
    r"\A(?:identity|permissions?|preferences?|safety)(?:\b|[_:./-])",
    re.IGNORECASE,
)
# A copy of ``memory._CLAIM_AUTHORITY_WEIGHT``; ``memory`` imports this module,
# so the dependency cannot run the other way.  Equality is asserted by
# ``tests/test_memory_graph.py``.
AUTHORITY_WEIGHT: dict[str, int] = {
    "external": 10,
    "learned": 30,
    "verified": 70,
    "operator": 100,
}
# The one copy of the configured-value vocabulary: ``agent`` aliases this name
# rather than keeping a second set, because ``memory`` must not import
# ``agent``.  Equality is asserted by ``tests/test_memory_graph.py``.
ASKED_VALUE_WORDS: frozenset[str] = frozenset({
    "port", "ports", "host", "hosts", "hosted", "hostname", "owner", "owners",
    "owns", "owned", "lead", "leads", "maintainer", "maintainers", "maintains",
    "maintained", "address", "url", "path", "version", "region", "zone",
    "datacenter", "rack", "channel", "branch", "schedule", "scheduled",
    "deadline", "due", "status", "config", "configured", "configuration",
    "setting", "settings", "limit", "endpoint", "cluster", "namespace",
    "environment", "release", "deploy", "deployed", "deployment", "contact",
    "listen", "listens", "listening", "runs", "running", "pinned", "timezone",
    "location", "located", "credentials", "repo", "repository", "database",
    "server", "instance", "node", "queue", "bucket", "image", "tag",
})
# Closed stoplist for the "lower-case noun of four characters or more" arm of
# the asked-predicate rule: without it every question word would rank as an
# asked predicate.  Words in ASKED_VALUE_WORDS are matched first and are never
# removed by this list.
ASKED_STOPWORDS: frozenset[str] = frozenset({
    "about", "after", "again", "against", "along", "also", "another", "any",
    "anything", "around", "because", "been", "before", "being", "beside",
    "between", "both", "cannot", "could", "current", "currently", "does",
    "doing", "done", "down", "during", "each", "either", "else", "even",
    "ever", "every", "from", "give", "have", "having", "here", "however",
    "into", "just", "know", "like", "list", "made", "make", "many", "more",
    "most", "much", "must", "name", "need", "next", "none", "only", "other",
    "over", "please", "říct", "said", "same", "says", "show", "since", "some",
    "still", "such", "tell", "than", "that", "their", "them", "then", "there",
    "these", "they", "thing", "things", "this", "those", "through", "under",
    "until", "upon", "used", "using", "very", "want", "what", "whatever",
    "when", "where", "whether", "which", "while", "whose", "will", "with",
    "within", "without", "would", "your",
})
# Configured value words that ask about *activity* rather than naming a stored
# predicate.  They belong in ASKED_VALUE_WORDS because ``_named_fact_subjects``
# uses that set to spot a named subject ("Where does Osprey run?"), which is a
# different job: as asked predicates they drop every answering chain.  The
# design says so itself — §5.4 calls "What runs in Fenwick?" an *open*
# question, and §7.4 expects "Which datacenter used to host the Kestrel relay?"
# to rank the ``datacenter`` chain first, which it cannot while "host" narrows
# to ``deployed on host``.  Removing a word here never drops an answer; it only
# gives up a narrowing hint.
ASKED_OPEN_WORDS: frozenset[str] = frozenset({
    "configured", "host", "hosted", "hosting", "hosts", "leads", "listening",
    "listens", "located", "maintained", "maintains", "owned", "owns", "pinned",
    "running", "runs", "scheduled",
})
# The fixed clause the cue carries when the main lane could not resolve the
# subject and the graph answered from an exact key (design §2.3d / §5.9).
# Lane modes that silence the channel outright: the lane refused for a
# security reason, so the graph is not consulted at all (design 5.6 floor 1).
LANE_SILENT_MODES: frozenset[str] = frozenset({
    "screened", "project-unavailable", "corrupt-strongest", "error",
})


def lane_forces_exact_only(lane_mode: str | None) -> bool:
    """Whether a lane mode disables non-exact start resolution.

    **No mode does, since design 10.3 item 1.**  ``identity-conflict`` and
    ``identity-overflow`` are the lane saying *its own* substring scan could
    not tell which subject was meant; that is not evidence against the
    graph's rules, each of which carries its own floor -- and the sealed
    holdout showed the gate turning four correct resolutions into
    ``no-start``, two of which should have abstained ``identity-conflict``
    and could not, because the rule that raises it never ran.  The
    lane-abstained clause still travels with the answer, which is the honest
    part of the old behaviour.  Kept as a function so the rule has one home
    and a test.
    """
    _ = lane_mode
    return False


LANE_ABSTAINED_CLAUSE = (
    "The main memory lane could not tell which stored subject this names; "
    "the chain below starts from an exactly matching name."
)
# The graph holdout's runtime pin covers exactly these four files, in this
# order; ``agent.py`` is deliberately not pinned (design §1.4, review R12).
MEMORY_GRAPH_RUNTIME_FILES: tuple[str, ...] = (
    "memory.py", "memory_graph.py", "memory_retrieval.py", "redaction.py",
)

# A value the write path already rewrote: ``remember_claim`` stores a
# secret-shaped value as "[REDACTED]", and the private-identifier redactor
# emits "[EMAIL]", "[USER]", "[HOST]".  Such a value is not a name — two
# different credentials arrive as the same string — so it must never become a
# node that joins the facts about them.
REDACTION_PLACEHOLDER = re.compile(r"\A\[[A-Z_ ]+\]\Z")
# Stands in for an asked predicate the store has no word for: no predicate key
# can contain a NUL, so no chain answers it, which is what keeps "asked for a
# fact the store does not have" distinct from "asked nothing in particular".
UNMATCHED_PREDICATE = "\x00unmatched"
# Sorts after every character an entity key can hold, so a prefix range scan
# is expressible as key >= low AND key < low + this.
_KEY_RANGE_TOP = "\U0010ffff"
_SENTENCE_TERMINATOR = re.compile(r"[.!?]\s")
_WORD = re.compile(r"[A-Za-z][\w-]*")
_PROSE_WORD_COUNT = 9
_UNSET: Any = object()

# Exactly the columns ``project_claim`` reads.  ``Memory`` selects this set for
# the write hook and this module selects it for the migration backfill, so the
# hook and the backfill project from one row shape.
CLAIM_ROW_COLUMNS: tuple[str, ...] = (
    "id", "scope", "claim_key", "subject", "predicate", "value", "status",
    "authority", "confidence", "valid_from", "valid_until", "spine_event_id",
)
# The per-claim tuple ``verify`` and ``rebuild`` compare.  ``label`` and
# ``created_at`` are display-only and are deliberately absent, as is the entity
# ``id`` (ids are allocated and never reused; a rebuild must not renumber a
# surviving entity).
_EDGE_EQUIVALENCE_FIELDS: tuple[str, ...] = (
    "scope", "claim_key", "src_entity_key", "predicate_key", "dst_entity_key",
    "value_kind", "status", "authority", "confidence", "valid_from",
    "valid_until", "spine_event_id",
)

_ENTITY_SEQUENCE_SQL = """CREATE TABLE memory_graph_entity_sequence (
    id INTEGER PRIMARY KEY CHECK(id=1),
    next_id INTEGER NOT NULL CHECK(next_id >= 1)
)"""
_ENTITIES_SQL = """CREATE TABLE memory_graph_entities (
    id INTEGER PRIMARY KEY,
    scope TEXT NOT NULL,
    entity_key TEXT NOT NULL,
    label TEXT NOT NULL CHECK(length(label) <= 80),
    created_at TEXT NOT NULL,
    UNIQUE(scope, entity_key)
)"""
_EDGES_SQL = """CREATE TABLE memory_graph_edges (
    claim_id INTEGER PRIMARY KEY,
    scope TEXT NOT NULL,
    claim_key TEXT NOT NULL,
    src_entity_id INTEGER NOT NULL,
    predicate_key TEXT NOT NULL,
    dst_entity_id INTEGER,
    value_kind TEXT NOT NULL CHECK(value_kind IN ('entity','literal')),
    status TEXT NOT NULL CHECK(status IN ('active','disputed','superseded')),
    authority TEXT NOT NULL CHECK(authority IN
        ('external','learned','verified','operator')),
    confidence REAL NOT NULL CHECK(confidence BETWEEN 0 AND 1),
    valid_from TEXT NOT NULL,
    valid_until TEXT,
    spine_event_id INTEGER NOT NULL,
    projected_at TEXT NOT NULL,
    CHECK((value_kind='entity') = (dst_entity_id IS NOT NULL)),
    FOREIGN KEY(claim_id) REFERENCES memory_claims(id),
    FOREIGN KEY(src_entity_id) REFERENCES memory_graph_entities(id),
    FOREIGN KEY(dst_entity_id) REFERENCES memory_graph_entities(id)
)"""
_EDGE_UPSERT_SQL = """INSERT INTO memory_graph_edges(
       claim_id, scope, claim_key, src_entity_id, predicate_key,
       dst_entity_id, value_kind, status, authority, confidence,
       valid_from, valid_until, spine_event_id, projected_at
   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
   ON CONFLICT(claim_id) DO UPDATE SET
       scope=excluded.scope, claim_key=excluded.claim_key,
       src_entity_id=excluded.src_entity_id,
       predicate_key=excluded.predicate_key,
       dst_entity_id=excluded.dst_entity_id,
       value_kind=excluded.value_kind, status=excluded.status,
       authority=excluded.authority, confidence=excluded.confidence,
       valid_from=excluded.valid_from, valid_until=excluded.valid_until,
       spine_event_id=excluded.spine_event_id,
       projected_at=excluded.projected_at"""
_GRAPH_INDEX_SQL: tuple[str, ...] = (
    "CREATE INDEX idx_memory_graph_entities_key "
    "ON memory_graph_entities(entity_key, scope)",
    "CREATE INDEX idx_memory_graph_edges_out "
    "ON memory_graph_edges(scope, src_entity_id, status, predicate_key, claim_id)",
    "CREATE INDEX idx_memory_graph_edges_in "
    "ON memory_graph_edges(scope, dst_entity_id, status, predicate_key, claim_id)",
    "CREATE INDEX idx_memory_graph_edges_key "
    "ON memory_graph_edges(scope, claim_key, claim_id)",
)


class GraphError(memory_spine.SpineError):
    """A graph write or verification could not proceed safely.

    ``code`` is a fixed machine-readable reason, never operator-derived text:
    ``graph_missing``, ``not_in_transaction``, ``stale_plan``,
    ``residual_divergence``, ``write_conflict``, ``sequence_behind``.
    Subclasses ``SpineError`` so the store's existing refusal mapping keeps
    working.
    """


# --- pure helpers -----------------------------------------------------------

def entity_key(text: str) -> str:
    """The identity of a *name*: NFKC, casefolded, whitespace-collapsed.

    This is ``Memory._claim_identity``'s subject normalization plus NFKC, so a
    fullwidth or compatibility spelling joins the entity of the ordinary
    spelling while a Cyrillic look-alike does not — the same boundary
    ``normalize_private_identifier_text`` draws.  An entity is a coarser
    equivalence than a claim key: several claim keys can share one subject.
    """
    return " ".join(unicodedata.normalize("NFKC", str(text)).casefold().split())


def entity_label(text: str) -> str:
    """The display spelling stored beside an entity key.

    Display only and never model-facing: traversal joins on ``entity_key`` and
    every cue string is read from the claim row.  When the collapsed spelling
    is longer than the column allows while its key is not (NFKC can shorten
    text), the key is the label, so ``entity_key(label) == entity_key`` and
    ``length(label) <= 80`` hold by construction.
    """
    collapsed = " ".join(str(text).split())
    if len(collapsed) <= ENTITY_LABEL_MAX_CHARS:
        return collapsed
    return entity_key(collapsed)[:ENTITY_LABEL_MAX_CHARS]


def claim_exclusion(subject: str, predicate: str) -> str | None:
    """The exclusion category of a claim, or ``None`` when it projects.

    Closed set: ``excluded_predicate`` (the reserved namespace),
    ``subject_too_long`` (the label bound above), ``subject_private`` (the
    subject carries a secret or a widened private identifier — the write path
    screens a subject for secrets only).  Cheapest test first, so an over-long
    subject is never screened at all; that is also why a subject that is both
    over-long and private is reported as ``subject_too_long``.
    """
    if EXCLUDED_PREDICATE_NAMESPACE.match(entity_key(predicate)) is not None:
        return "excluded_predicate"
    if len(entity_key(subject)) > ENTITY_LABEL_MAX_CHARS:
        return "subject_too_long"
    if screen_endpoint(subject)[0]:
        return "subject_private"
    return None


def is_redaction_placeholder(value: str) -> bool:
    """Whether a value is nothing but a redaction placeholder.

    The write path rewrites a secret-shaped value to "[REDACTED]" before it is
    stored, and the private-identifier redactor emits "[EMAIL]", "[USER]",
    "[HOST]".  Such a value carries no information at all: it is not a name,
    it is not the fact the operator asked for, and two unrelated credentials
    render as the same string.  It is never a node, and it is never a cue row
    either.
    """
    return REDACTION_PLACEHOLDER.match(" ".join(str(value).split())) is not None


def _looks_like_prose(text: str) -> bool:
    return (
        len(text.split()) >= _PROSE_WORD_COUNT
        or _SENTENCE_TERMINATOR.search(text) is not None
    )


def value_admission(value: str) -> str:
    """``"entity"`` when a value may be a node, ``"literal"`` otherwise.

    A literal still gets an edge, so a chain can end on "listen port 9090",
    but never a node: it is a terminal hop, never joinable and never a start.
    That is the reversed-triple join rule in one line — a value links to the
    facts about it exactly when the same key exists as a subject entity in a
    visible scope.

    A **redaction placeholder** is a literal for a different reason than the
    rest: it passes every screen, because the write path already removed the
    secret.  ``remember_claim`` stores a secret-shaped value as "[REDACTED]",
    so two unrelated credentials arrive as one string; as a node that string
    would join the facts about them and a question about one probe could
    reach the other through the shared name.  A placeholder is not a name.
    """
    text = " ".join(str(value).split())
    return "literal" if (
        not text
        or len(text) > ENTITY_LABEL_MAX_CHARS
        or not any(character.isalpha() for character in text)
        or is_redaction_placeholder(text)
        or _looks_like_prose(text)
        or screen_endpoint(text)[0]
    ) else "entity"


def alias_subject(subject: str, known_subjects: Sequence[str]) -> str:
    """Resolve a one-word subject to the single stored multi-word subject that
    ends with it ("Kestrel" -> "Kestrel relay"); otherwise return it unchanged.

    The one copy of the rule: ``Agent._alias_subject`` delegates here, and
    ``tests/test_memory_graph.py`` drives both over one table of inputs so the
    two cannot silently diverge.  Deterministic, so a confirmation re-derives
    the same subject.
    """
    tokens = str(subject).split()
    if len(tokens) != 1:
        return subject
    head = tokens[0].casefold()
    matches: dict[str, str] = {}
    for known in known_subjects:
        words = str(known).casefold().split()
        if len(words) > 1 and words[-1] == head:
            matches.setdefault(" ".join(words), str(known))
    if len(matches) != 1:
        return subject
    return next(iter(matches.values()))


def asked_predicate_words(query: str) -> frozenset[str]:
    """The predicate words a question asks for.

    A token counts when it is a configured value word or a lower-case token of
    at least four characters that is not a question word.  Activity verbs are
    kept here and dropped by ``narrow_asked_words``, which is the only place
    that knows whether the question asked for anything else and whether the
    store has a predicate by that name.
    """
    words: set[str] = set()
    for token in _WORD.findall(str(query)):
        folded = token.casefold()
        if folded in ASKED_VALUE_WORDS:
            words.add(folded)
        elif (
            token.islower()
            and len(folded) >= 4
            and folded not in ASKED_STOPWORDS
        ):
            words.add(folded)
    return frozenset(words)


def _singular(word: str) -> str:
    """A trailing plural folded away: "relays" and "relay" are one word."""
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def _in_vocabulary(word: str, vocabulary: frozenset[str]) -> bool:
    folded = _singular(word)
    if word in vocabulary or folded in vocabulary:
        return True
    return any(_singular(entry) == folded for entry in vocabulary)


def narrow_asked_words(
    words: frozenset[str],
    *,
    subjects: Sequence[str] = (),
    known: frozenset[str] = frozenset(),
    vocabulary: Any = None,
) -> tuple[frozenset[str], bool]:
    """``(asked, unmatched)``: what a question narrows the walk by.

    Three rules, each one a sealed-holdout failure or a pinned exit test:

    * **A word of the subject the operator named is the subject**, not an
      attribute they asked for.  "Where is the Alder probe hosted?" asks
      nothing about "probe"; four holdout ``lookalike`` cases and two joins
      answered nothing because their only asked word was a word of their own
      subject.
    * **An activity verb is dropped only when the question asked for
      something else too.**  In "Which datacenter used to host the Kestrel
      relay?" the attribute is the datacenter and "host" must not narrow to
      ``deployed on host`` (design 7.4); but in "Where is the Dornick probe
      hosted?" it is all the operator asked, the store has a ``hosted in``
      predicate, and dropping it made a question about hosting answer with
      the subject's channel instead.
    * **The visible store's own vocabulary decides what is an attribute**
      (design 10.3 item 2, replacing the configured-word test that let
      "almanac" through).  A word naming a predicate narrows; a word the
      store knows only as a subject or a value is a *thing*, not an attribute,
      and never narrows ("hall" in "the same hall as"); a word appearing
      nowhere is an attribute the store cannot reach, so the question is
      unanswerable and the not_recorded cue answers it (design 7.8, and the
      holdout's "Which almanac lists the Aldwin barge?", which was being
      answered with a moorage and a district).  A trailing plural folds.

    ``known`` is the visible predicate vocabulary; ``vocabulary`` is the
    visible subject/value vocabulary, a set or a zero-argument callable so the
    caller can leave it unread on the common path.
    """
    subject_words = {
        word for subject in subjects for word in entity_key(subject).split()
    }
    asked = frozenset(words) - subject_words
    substantive = asked - ASKED_OPEN_WORDS
    if substantive:
        asked = substantive
    predicates = frozenset(known)
    narrowed = frozenset(word for word in asked if _in_vocabulary(word, predicates))
    if narrowed:
        return narrowed, False
    if not asked:
        return frozenset(), False
    # Only now is the subject/value vocabulary worth reading: on the common
    # path an asked word does name a predicate and this never runs.
    # An activity verb never makes a question unanswerable: design 5.4 calls
    # "What runs in Fenwick?" an open question, and a store with no "runs"
    # predicate has not failed to hold an attribute -- it was never asked for
    # one.  Only a noun the store has never heard of does that.
    nouns = asked - ASKED_OPEN_WORDS
    if not nouns:
        return frozenset(), False
    if callable(vocabulary):
        # The probe form: it is told which words to look for and returns the
        # subset it found, so the whole vocabulary is never materialised.
        found = frozenset(vocabulary(nouns) or ())
        unknown = any(word not in found for word in nouns)
    else:
        entity_words = frozenset(vocabulary or ())
        unknown = any(not _in_vocabulary(word, entity_words) for word in nouns)
    if unknown:
        return frozenset({UNMATCHED_PREDICATE}), True
    return frozenset(), False


def memory_graph_runtime_sha256() -> str:
    """The canonical four-file digest the graph holdout pins.

    Same canonical form as ``long_horizon_runtime_sha256`` and
    ``strategy_transfer_runtime_sha256``: sha256 over the canonical JSON of a
    ``{filename: sha256}`` mapping, so the reseal tool can add a third cascade
    with no special case.  ``agent.py`` is deliberately absent.
    """
    package_dir = Path(__file__).resolve().parent
    material = {
        name: hashlib.sha256((package_dir / name).read_bytes()).hexdigest()
        for name in MEMORY_GRAPH_RUNTIME_FILES
    }
    return hashlib.sha256(
        json.dumps(
            material, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()


# --- schema -----------------------------------------------------------------

def _table_exists(db: sqlite3.Connection, table: str) -> bool:
    row = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def graph_ready(db: sqlite3.Connection) -> bool:
    """True once every graph object of schema 48 exists."""
    return all(_table_exists(db, name) for name in GRAPH_TABLES)


def create_graph_tables(db: sqlite3.Connection) -> None:
    """Create the three tables and their four indexes (schema 48)."""
    for statement in (_ENTITY_SEQUENCE_SQL, _ENTITIES_SQL, _EDGES_SQL):
        db.execute(statement)
    for statement in _GRAPH_INDEX_SQL:
        db.execute(statement)


def drop_graph_tables(db: sqlite3.Connection) -> None:
    """Drop the graph, edges first.

    ``Memory._migrate`` calls this at the very top of its transaction, before
    the v46/v47 steps, because those steps rewrite ``memory_claims`` and a
    stale edge would still hold foreign keys into rows they are about to
    recreate.  ``tests/legacy_store_fixture.strip_spine`` calls it for the
    same reason a store below 48 has no graph.
    """
    for statement in DROP_GRAPH_SQL:
        db.execute(statement)


def entity_sequence_floor(db: sqlite3.Connection) -> int:
    row = db.execute(
        "SELECT COALESCE(MAX(id), 0) FROM memory_graph_entities"
    ).fetchone()
    return int(row[0] or 0)


def allocate_entity_id(db: sqlite3.Connection) -> int:
    """Explicit, never-reused entity ids: the sequence only moves forward, so
    an entity removed by the orphan sweep never returns under its old id."""
    row = db.execute(
        "SELECT next_id FROM memory_graph_entity_sequence WHERE id=1"
    ).fetchone()
    if row is None:
        raise GraphError(
            "memory graph entity sequence is missing", code="graph_missing"
        )
    entity_id = int(row[0])
    if entity_id <= entity_sequence_floor(db):
        raise GraphError(
            "memory graph entity sequence is behind the store; run graph verify",
            code="sequence_behind",
        )
    db.execute(
        "UPDATE memory_graph_entity_sequence SET next_id=? WHERE id=1",
        (entity_id + 1,),
    )
    return entity_id


# --- projection -------------------------------------------------------------

def projection_cache(*, batch: bool = False) -> dict[str, Any]:
    """Fresh scratch for one bulk projection; never shared between calls.

    ``batch=True`` also collects the edge rows so the caller can flush them
    with one ``executemany`` (``flush_projection``) instead of 20,000
    statements.
    """
    cache: dict[str, Any] = {"entities": {}, "exclusions": {}, "values": {}}
    if batch:
        cache["edges"] = []
    return cache


def flush_projection(db: sqlite3.Connection, cache: Mapping[str, Any]) -> int:
    """Write the edges a ``batch=True`` projection collected; return the count."""
    pending = cache.get("edges")
    if not pending:
        return 0
    rows = list(pending)
    db.executemany(_EDGE_UPSERT_SQL, rows)
    pending.clear()
    return len(rows)


def _field(row: Mapping[str, Any], name: str, default: Any = None) -> Any:
    try:
        return row[name]
    except (KeyError, IndexError):
        return default


def _text(row: Mapping[str, Any], name: str) -> str:
    value = _field(row, name)
    return "" if value is None else str(value)


def _upsert_entity(
    db: sqlite3.Connection,
    scope: str,
    key: str,
    label: str,
    now: str,
    cache: dict[tuple[str, str], int] | None = None,
) -> int:
    if cache is not None:
        cached = cache.get((scope, key))
        if cached is not None:
            return cached
    row = db.execute(
        "SELECT id FROM memory_graph_entities WHERE scope=? AND entity_key=?",
        (scope, key),
    ).fetchone()
    if row is not None:
        if cache is not None:
            cache[(scope, key)] = int(row[0])
        return int(row[0])
    entity_id = allocate_entity_id(db)
    db.execute(
        """INSERT INTO memory_graph_entities(id, scope, entity_key, label, created_at)
           VALUES (?, ?, ?, ?, ?)""",
        (entity_id, scope, key, label, str(now)),
    )
    if cache is not None:
        cache[(scope, key)] = entity_id
    return entity_id


def project_claim(
    db: sqlite3.Connection,
    claim_row: Mapping[str, Any],
    *,
    now: str,
    cache: Mapping[str, Any] | None = None,
) -> str | None:
    """Project one claim row into one edge, allocating entities as needed.

    Returns the exclusion category when the claim does not project, otherwise
    ``None``.  Runs inside the caller's write transaction and takes no lock.
    ``claim_row`` must expose ``CLAIM_ROW_COLUMNS`` by name.

    ``cache`` is the optional run-local scratch of a bulk projection (the
    migration backfill and ``reproject`` build one with ``projection_cache()``):
    it memoises ``(scope, entity_key) -> id`` and the exclusion category of a
    ``(subject, predicate)`` pair for the duration of that one call, which is
    where the backfill's cost lives — ~40,000 statements for ~13,500 distinct
    entities without it.  It is a local, never module state, so the purity the
    screen battery asserts is untouched, and it changes no id and no order.
    """
    subject = _text(claim_row, "subject")
    predicate = _text(claim_row, "predicate")
    entities: dict[tuple[str, str], int] | None = None
    exclusions: dict[tuple[str, str], str | None] | None = None
    if cache is not None:
        entities = cache.get("entities")
        exclusions = cache.get("exclusions")
    if exclusions is None:
        excluded = claim_exclusion(subject, predicate)
    else:
        pair = (subject, predicate)
        if pair in exclusions:
            excluded = exclusions[pair]
        else:
            excluded = claim_exclusion(subject, predicate)
            exclusions[pair] = excluded
    if excluded is not None:
        return excluded
    scope = _text(claim_row, "scope") or "global"
    value = _text(claim_row, "value")
    src_entity_id = _upsert_entity(
        db, scope, entity_key(subject), entity_label(subject), now, entities
    )
    values: dict[str, str] | None = cache.get("values") if cache is not None else None
    if values is None:
        value_kind = value_admission(value)
    elif value in values:
        value_kind = values[value]
    else:
        value_kind = values.setdefault(value, value_admission(value))
    dst_entity_id: int | None = None
    if value_kind == "entity":
        dst_entity_id = _upsert_entity(
            db, scope, entity_key(value), entity_label(value), now, entities
        )
    parameters = (
        int(_field(claim_row, "id")), scope, _text(claim_row, "claim_key"),
        src_entity_id, entity_key(predicate), dst_entity_id, value_kind,
        _text(claim_row, "status"), _text(claim_row, "authority"),
        float(_field(claim_row, "confidence") or 0.0),
        _text(claim_row, "valid_from"), _field(claim_row, "valid_until"),
        int(_field(claim_row, "spine_event_id") or 0), str(now),
    )
    pending = cache.get("edges") if cache is not None else None
    if pending is None:
        db.execute(_EDGE_UPSERT_SQL, parameters)
    else:
        pending.append(parameters)
    return None


def update_edge(
    db: sqlite3.Connection,
    claim_id: int,
    *,
    status: str | None = None,
    authority: str | None = None,
    confidence: float | None = None,
    valid_until: Any = _UNSET,
) -> bool:
    """Keep an edge's mutable columns in step with its claim row.

    A silent no-op returning ``False`` when the claim has no edge: an excluded
    claim still goes through the status writer.  ``valid_until`` uses a
    sentinel rather than ``None`` so "clear it" is distinguishable from "not
    supplied", and is honoured only alongside a status change (the two move
    together on the claim row).
    """
    if not _table_exists(db, "memory_graph_edges"):
        return False
    assignments: list[str] = []
    parameters: list[Any] = []
    if status is not None:
        assignments.append("status=?")
        parameters.append(str(status))
        if valid_until is not _UNSET:
            assignments.append("valid_until=?")
            parameters.append(valid_until)
    if authority is not None:
        assignments.append("authority=?")
        parameters.append(str(authority))
    if confidence is not None:
        assignments.append("confidence=?")
        parameters.append(float(confidence))
    if not assignments:
        return False
    parameters.append(int(claim_id))
    cursor = db.execute(
        f"UPDATE memory_graph_edges SET {', '.join(assignments)} WHERE claim_id=?",
        parameters,
    )
    return int(cursor.rowcount) > 0


def sweep_orphan_entities(
    db: sqlite3.Connection, entity_ids: Sequence[int] | set[int]
) -> list[int]:
    """Remove the given entities that no edge references any more."""
    candidates = sorted({int(value) for value in entity_ids})
    if not candidates or not _table_exists(db, "memory_graph_entities"):
        return []
    placeholders = ",".join("?" for _ in candidates)
    rows = db.execute(
        f"""SELECT id FROM memory_graph_entities AS n
            WHERE n.id IN ({placeholders})
              AND NOT EXISTS (SELECT 1 FROM memory_graph_edges AS e
                              WHERE e.src_entity_id = n.id)
              AND NOT EXISTS (SELECT 1 FROM memory_graph_edges AS e
                              WHERE e.dst_entity_id = n.id)
            ORDER BY n.id""",
        candidates,
    ).fetchall()
    removed = [int(row[0]) for row in rows]
    if removed:
        db.execute(
            "DELETE FROM memory_graph_entities WHERE id IN "
            f"({','.join('?' for _ in removed)})",
            removed,
        )
    return removed


def sweep_all_orphan_entities(db: sqlite3.Connection) -> list[int]:
    """Remove every entity no edge references, wherever it came from."""
    if not _table_exists(db, "memory_graph_entities"):
        return []
    rows = db.execute(
        """SELECT id FROM memory_graph_entities AS n
           WHERE NOT EXISTS (SELECT 1 FROM memory_graph_edges AS e
                             WHERE e.src_entity_id = n.id)
             AND NOT EXISTS (SELECT 1 FROM memory_graph_edges AS e
                             WHERE e.dst_entity_id = n.id)
           ORDER BY n.id"""
    ).fetchall()
    removed = [int(row[0]) for row in rows]
    if removed:
        db.execute(
            "DELETE FROM memory_graph_entities WHERE id IN "
            f"({','.join('?' for _ in removed)})",
            removed,
        )
    return removed


def delete_edges(
    db: sqlite3.Connection, claim_ids: Sequence[int]
) -> list[int]:
    """Remove the edges of erased claims and sweep the entities they orphan.

    Called inside ``erase_explicit_project_claim`` before the ``memory_claims``
    delete (the foreign key requires it); the returned entity ids go into the
    tombstone payload as ``removed_entity_ids``.  ``Forget`` changes an edge's
    status and removes nothing.
    """
    identifiers = sorted({int(value) for value in claim_ids})
    if not identifiers or not _table_exists(db, "memory_graph_edges"):
        return []
    placeholders = ",".join("?" for _ in identifiers)
    candidates: set[int] = set()
    for row in db.execute(
        "SELECT src_entity_id, dst_entity_id FROM memory_graph_edges "
        f"WHERE claim_id IN ({placeholders})",
        identifiers,
    ).fetchall():
        candidates.add(int(row[0]))
        if row[1] is not None:
            candidates.add(int(row[1]))
    db.execute(
        f"DELETE FROM memory_graph_edges WHERE claim_id IN ({placeholders})",
        identifiers,
    )
    return sweep_orphan_entities(db, candidates)


def graph_counts(db: sqlite3.Connection) -> dict[str, Any]:
    """Cheap counts for ``graph status``: no per-claim comparison."""
    if not graph_ready(db):
        return {"edges": 0, "entities": 0, "excluded": _empty_exclusions()}
    edges = int(
        db.execute("SELECT COUNT(*) FROM memory_graph_edges").fetchone()[0]
    )
    entities = int(
        db.execute("SELECT COUNT(*) FROM memory_graph_entities").fetchone()[0]
    )
    return {"edges": edges, "entities": entities, "excluded": _exclusions(db)}


def _empty_exclusions() -> dict[str, int]:
    return {kind: 0 for kind in EXCLUSION_KINDS}


def _exclusions(db: sqlite3.Connection) -> dict[str, int]:
    """The three category counts over the live claim rows."""
    counts = _empty_exclusions()
    memo: dict[tuple[str, str], str | None] = {}
    for row in db.execute(
        "SELECT subject, predicate FROM memory_claims"
    ).fetchall():
        pair = (str(row[0] or ""), str(row[1] or ""))
        if pair not in memo:
            memo[pair] = claim_exclusion(*pair)
        if memo[pair] is not None:
            counts[str(memo[pair])] += 1
    return counts


# --- migration --------------------------------------------------------------

def migrate_memory_graph_v48(
    db: sqlite3.Connection, key: bytes, *, now: str
) -> dict[str, Any]:
    """Create the graph and project every claim row into it (schema 48).

    Runs inside ``Memory._migrate``'s single write transaction after
    ``_migrate_v47``; for a legacy store below 46 the spine steps have already
    run in the same transaction, so the receipt can be appended here.  Reads
    ``memory_claims`` and writes no spine table except that one receipt: a
    ``projection.rebuilt`` with ``projection: "graph"`` whose ``excluded`` is
    the three-key category object, so an operator can see how many facts the
    graph is not carrying and why.

    Idempotent: the graph is dropped first, so a re-migration rebuilds it from
    scratch.  ``Memory._migrate`` still drops it at the top of the transaction
    (before v46/v47), which is what keeps those steps from meeting stale
    foreign keys.
    """
    drop_graph_tables(db)
    create_graph_tables(db)
    db.execute("INSERT INTO memory_graph_entity_sequence(id, next_id) VALUES (1, 1)")
    excluded = _empty_exclusions()
    cache = projection_cache(batch=True)
    rows = db.execute(
        f"SELECT {', '.join(CLAIM_ROW_COLUMNS)} FROM memory_claims ORDER BY id"
    ).fetchall()
    for row in rows:
        category = project_claim(db, row, now=now, cache=cache)
        if category is not None:
            excluded[category] += 1
    flush_projection(db, cache)
    edges = int(
        db.execute("SELECT COUNT(*) FROM memory_graph_edges").fetchone()[0]
    )
    entities = int(
        db.execute("SELECT COUNT(*) FROM memory_graph_entities").fetchone()[0]
    )
    event_id = memory_spine.append_event(
        db, key,
        kind="projection.rebuilt", actor="system", source="graph migration 48",
        scope="global", permission="migration", outcome="applied",
        subject_kind="projection",
        payload={
            "at": str(now),
            "projection": "graph",
            "rows_before": 0,
            "rows_after": edges,
            "divergences_fixed": 0,
            "removed_ids": [],
            "entities": entities,
            "excluded": excluded,
        },
        now=now,
    )
    return {
        "edges": edges,
        "entities": entities,
        "excluded": excluded,
        "event_id": int(event_id),
    }


# --- start resolution (design §2.3) ------------------------------------------

def subject_identity_conflict(
    subject_head: str, other_keys: Sequence[str] | set[str]
) -> bool:
    """A copy of ``memory._claim_subject_identity_conflict``.

    ``memory`` imports this module, so the dependency cannot run the other
    way; ``tests/test_memory_graph.py`` drives both over one table of inputs
    and asserts identical output.  The graph applies it **store versus
    store** — a non-exact candidate's full normalized key against every other
    visible entity key — in ONE call, never once per key.
    """
    head = str(subject_head).casefold()
    if head in other_keys or len(head) < 5:
        return False
    for term in other_keys:
        candidate = str(term).casefold()
        if len(candidate) < 5:
            continue
        shorter, longer = sorted((head, candidate), key=len)
        if longer.startswith(shorter) or longer.endswith(shorter):
            return True
        prefix = 0
        for left, right in zip(head, candidate, strict=False):
            if left != right:
                break
            prefix += 1
        suffix = 0
        for left, right in zip(reversed(head), reversed(candidate), strict=False):
            if left != right:
                break
            suffix += 1
        if min(len(head), len(candidate)) >= 7 and max(prefix, suffix) >= 3:
            return True
    return False


def default_scope_filter(
    visible_scopes: Sequence[str], project_scope: str | None = None, alias: str = "e"
) -> tuple[str, list[Any]]:
    """The edge scope + shadowing predicate, for module tests and callers that
    have no claims-lane filter to hand.

    ``Memory`` passes its own ``_claim_scope_filter`` text so the graph and the
    claims lane can never diverge; this reproduces it: a project edge shadows a
    global edge of the same claim key only while the project row is current.
    """
    scopes = list(dict.fromkeys(str(scope) for scope in visible_scopes))
    placeholders = ",".join("?" for _ in scopes)
    sql = f"{alias}.scope IN ({placeholders})"
    params: list[Any] = list(scopes)
    if project_scope:
        sql += (
            f" AND ({alias}.scope=? OR NOT EXISTS ("
            "SELECT 1 FROM memory_claims AS pc WHERE pc.scope=? "
            f"AND pc.claim_key={alias}.claim_key "
            "AND pc.status IN ('active','disputed')))"
        )
        params.extend([str(project_scope), str(project_scope)])
    return sql, params


def _entity_rows(
    db: sqlite3.Connection, visible_scopes: Sequence[str], key: str
) -> list[sqlite3.Row]:
    scopes = list(visible_scopes)
    placeholders = ",".join("?" for _ in scopes)
    return db.execute(
        "SELECT id, scope, entity_key, label FROM memory_graph_entities "
        f"WHERE entity_key=? AND scope IN ({placeholders}) ORDER BY scope <> 'global', id",
        [str(key), *scopes],
    ).fetchall()


def _visible_entity_keys(
    db: sqlite3.Connection, visible_scopes: Sequence[str]
) -> set[str]:
    """The whole visible key set, read lazily: only a non-exact resolution
    pays for it (3.7 ms at 12,220 keys), never the exact path."""
    scopes = list(visible_scopes)
    placeholders = ",".join("?" for _ in scopes)
    return {
        str(row[0])
        for row in db.execute(
            "SELECT DISTINCT entity_key FROM memory_graph_entities "
            f"WHERE scope IN ({placeholders})",
            scopes,
        ).fetchall()
    }


def _within_one_edit(left: str, right: str) -> bool:
    """Whether two words differ by at most one insert, delete or substitution.

    Two pointers, no matrix: O(len) and allocation-free.
    """
    if left == right:
        return True
    if abs(len(left) - len(right)) > 1:
        return False
    if len(left) == len(right):
        return sum(1 for a, b in zip(left, right) if a != b) == 1
    shorter, longer = (left, right) if len(left) < len(right) else (right, left)
    index = offset = 0
    skipped = False
    while index < len(shorter) and offset < len(longer):
        if shorter[index] == longer[offset]:
            index += 1
            offset += 1
            continue
        if skipped:
            return False
        skipped = True
        offset += 1
    return True


def near_miss_subject(typed: str, keys: Sequence[str] | set[str]) -> bool:
    """Whether a typed name looks like a **misspelling** of a stored key.

    Same number of words, exactly one of them different, and that word within
    one edit: "Kestrel rely" is a near miss of "Kestrel relay", while "Kestrel
    gateway", "Merlin relay", "Kestrel payroll ledger" and "Zephyr gadget" are
    not.  ``subject_identity_conflict`` cannot do this job — it fires on any
    two names of seven characters or more that share three characters at
    either end, so it calls every unseen "<word> relay" a look-alike, and the
    operator is told the store might know the name under another spelling
    when it has simply never heard of it (recommendation 10 keeps ``no-start``
    and ``identity-conflict`` apart precisely so that distinction survives).
    """
    typed_words = str(typed).split()
    if not typed_words:
        return False
    length = len(typed)
    for key in keys:
        if abs(len(key) - length) > 1:
            continue
        words = key.split()
        if len(words) != len(typed_words):
            continue
        differing = [
            index for index, (left, right) in enumerate(zip(typed_words, words))
            if left != right
        ]
        if len(differing) == 1 and _within_one_edit(
            typed_words[differing[0]], words[differing[0]]
        ):
            return True
    return False


def _near_miss_candidates(
    db: sqlite3.Connection, visible_scopes: Sequence[str], key: str
) -> list[str]:
    """The only keys a one-edit near miss can match: those within one
    character of the typed length.  Bounded in SQLite rather than by
    materialising the whole key set in Python — at 50,000 entities that read
    alone costs 26 ms, which is the whole call budget."""
    if not key:
        return []
    scopes = list(visible_scopes)
    placeholders = ",".join("?" for _ in scopes)
    # Bounded by the first character (an indexed range) and then by length.
    # A near miss whose single edit is the first character of the first word
    # is therefore not found; the cost of that is a ``no-start`` cue instead
    # of ``identity-conflict``, never an answer, and it keeps resolution off
    # a full key-set read.
    rows = db.execute(
        "SELECT DISTINCT entity_key FROM memory_graph_entities "
        f"WHERE scope IN ({placeholders}) AND entity_key >= ? AND entity_key < ? "
        "AND length(entity_key) BETWEEN ? AND ?",
        [*scopes, key[0], key[0] + _KEY_RANGE_TOP, len(key) - 1, len(key) + 1],
    ).fetchall()
    return [str(row[0]) for row in rows]


def _alias_candidates(
    db: sqlite3.Connection, visible_scopes: Sequence[str], word: str
) -> list[str]:
    """Multi-word keys whose last word is ``word`` (the one-word alias rule),
    matched in SQLite instead of over the whole key set."""
    scopes = list(visible_scopes)
    placeholders = ",".join("?" for _ in scopes)
    rows = db.execute(
        "SELECT DISTINCT entity_key FROM memory_graph_entities "
        f"WHERE scope IN ({placeholders}) AND entity_key LIKE ? ESCAPE '\\'",
        [*scopes, "% " + _like_escaped(word)],
    ).fetchall()
    return [str(row[0]) for row in rows]


def _like_escaped(text: str) -> str:
    for character in ("\\", "%", "_"):
        text = text.replace(character, "\\" + character)
    return text


UNRESOLVED_NAME_CAP = 2


def _shadows_unresolved(key: str, unresolved_keys: set[str]) -> bool:
    """Whether a seed endpoint is a look-alike of a name that resolved nothing.

    Two tests, both cheap and both aimed at the same reading error: the
    store-versus-store floor, and a shared first word -- which is how the
    claims lane found the row in the first place, since it discovers by
    substring.  A key equal to the typed name cannot be a look-alike of it:
    it would have resolved.
    """
    if not key:
        return False
    head = key.split()[:1]
    for typed in unresolved_keys:
        if not typed or key == typed:
            continue
        if head and head == typed.split()[:1]:
            return True
        if subject_identity_conflict(key, {typed}):
            return True
    return False


def _screened_spellings(spellings: list[str]) -> list[str]:
    """The operator spellings a report may name, screened and capped.

    A name the operator typed goes back to the model in the not-recorded
    line, so it passes the same screen an entity label does -- a typed name
    can carry a private identifier as easily as a stored one.
    """
    kept: list[str] = []
    for spelling in spellings:
        text = " ".join(str(spelling).split())
        if not text or screen_endpoint(text)[0]:
            continue
        if text not in kept:
            kept.append(text)
        if len(kept) >= UNRESOLVED_NAME_CAP:
            break
    return kept


def _word_prefix_candidates(
    db: sqlite3.Connection, visible_scopes: Sequence[str], key: str
) -> list[str]:
    """Visible keys of which the typed name is a **word-prefix**.

    Every typed word equals the candidate's word at the same position, and a
    typed name longer than the candidate is never a prefix — so "Kestrel"
    reaches "Kestrel relay" while "Kestrel payroll ledger", "Kestrel gateway"
    and an 81-character name that merely shares a first word reach nothing.
    The red team of 2026-09-03 resolved all three to "Kestrel relay" under the
    first-word rule this replaces, and the store answered with a different
    subject's value and no cue.

    One indexed query per named subject (``entity_key = ?`` or the key
    followed by a space), not a scan of the whole key set.
    """
    typed = str(key)
    if not typed:
        return []
    scopes = list(visible_scopes)
    placeholders = ",".join("?" for _ in scopes)
    # A range, not a LIKE: SQLite declines the index optimisation for LIKE
    # with an ESCAPE clause, and at 50,000 entities that is a full scan on
    # every named subject.
    rows = db.execute(
        "SELECT DISTINCT entity_key FROM memory_graph_entities "
        f"WHERE scope IN ({placeholders}) "
        "AND (entity_key = ? OR (entity_key >= ? AND entity_key < ?))",
        [*scopes, typed, typed + " ", typed + " " + _KEY_RANGE_TOP],
    ).fetchall()
    return [str(row[0]) for row in rows]


def resolve_starts(
    db: sqlite3.Connection,
    *,
    subjects: Sequence[str] = (),
    seed_claims: Sequence[Mapping[str, Any]] = (),
    visible_scopes: Sequence[str],
    allow_non_exact: bool = True,
    deadline: float | None = None,
) -> tuple[list[dict[str, Any]], str | None, list[str]]:
    """Resolve the question's named subjects to start entities.

    Returns ``(starts, mode)``; ``mode`` is ``None`` or ``"identity-conflict"``
    and, when set, ``starts`` is empty.

    An **exact** full-key match resolves a start unambiguously and supersedes
    the lexical look-alike floor: ``UNIQUE(scope, entity_key)`` means an exact
    key names one stored subject and no other, so the substring-scan failure
    mode that floor guards against cannot occur, and two exactly resolved
    subjects are both allowed (the join case).

    **A seed claim's own subject and value resolve by exact key only.**  They
    come from stored rows, so whenever such an endpoint became a node an exact
    entity for it exists; when it did not the seed simply contributes no
    start.  Putting a seed through the non-exact path was a defect: the lane
    hands the agent up to four rows on every ordinary turn, and one of their
    values conflicting with an unrelated stored name under the look-alike
    floor would abstain a call that the named subject had already resolved
    exactly.

    Only the **question's own** unresolved names take the non-exact path — the
    one-word alias rule and the first-word phrase rule — and each carries the
    store-versus-store floor.  When a name the operator typed resolves only
    non-exactly and fails the floor, the whole call abstains
    ``identity-conflict`` even if another named subject resolved exactly: the
    operator named something the store cannot identify, and a confident
    half-answer to it is worse than silence (§1.4 ``two_subjects``, §7.15).  A
    non-exact candidate from any other source is dropped silently instead,
    which is what keeps a lane-supplied row from abstaining a question the
    operator spelled correctly.

    ``allow_non_exact=False`` is what the caller passes when the claims lane
    abstained ``identity-overflow`` / ``identity-conflict``: those are identity
    floors, so the graph answers only from exact keys.
    """
    if not graph_ready(db):
        return [], None, []
    scopes = list(dict.fromkeys(str(scope) for scope in visible_scopes))
    if not scopes:
        return [], None, []
    if _expired(deadline):
        return [], "budget-exceeded", []
    starts: list[dict[str, Any]] = []
    claimed: set[str] = set()
    # The visible key set is read at most once per call, lazily, and only a
    # non-exact resolution pays for it (3.7 ms at 12,220 keys).
    key_set: set[str] | None = None

    def visible_keys() -> set[str]:
        nonlocal key_set
        if key_set is None:
            key_set = _visible_entity_keys(db, scopes)
        return key_set

    def add(
        key: str,
        rows: Sequence[sqlite3.Row],
        exact: bool,
        seed_claim_id: int | None = None,
        named: bool = False,
    ) -> None:
        if not rows or key in claimed:
            return
        claimed.add(key)
        starts.append({
            "entity_key": key,
            "label": str(rows[-1]["label"]),
            "ids": [int(row["id"]) for row in rows],
            "exact": exact,
            "seed_claim_id": seed_claim_id,
            # A name the operator typed, as opposed to a row the claims lane
            # handed over: the two rank differently (design 5.4).
            "named": named,
        })

    # 1. named subjects, by exact key only.
    unresolved_named: list[str] = []
    exact_named: set[str] = set()
    for subject in subjects:
        key = entity_key(subject)
        if not key:
            continue
        rows = _entity_rows(db, scopes, key)
        if rows:
            add(key, rows, True, named=True)
            exact_named.add(key)
        else:
            unresolved_named.append(str(subject))

    # 2. the seed claims' own subjects and values, by exact key only.  A start
    #    taken from a seed's *value* records that claim, so the chain onward
    #    from it can show the operator how the walk reached the name.
    non_exact: list[tuple[str, bool]] = []   # (key, the operator named it)
    named_non_exact: set[str] = set()
    # Whole rows, not loose endpoints: a seed about a look-alike is
    # dropped entire, so the row has to survive as a unit until the
    # named subjects have been resolved.
    deferred_seeds: list[list[tuple[str, int | None]]] = []
    unresolved_spellings: list[str] = []
    candidates_of: dict[str, set[str]] = {}
    for claim in seed_claims:
        deferred_row: list[tuple[str, int | None]] = []
        claim_id = _field(claim, "claim_id", _field(claim, "id"))
        endpoints = {
            field: entity_key(_field(claim, field, "") or "")
            for field in ("subject", "value")
        }
        # One exception to "seeds are exempt from the identity floor": a seed
        # row about a *look-alike of a name the operator spelled exactly* is
        # dropped whole.  The lane discovers rows by substring, so a question
        # naming "Kestrel relay" hands back rows about "Kestrel relay 2";
        # each is an exact key of its own, and without this the answer to the
        # correctly spelled name would arrive mixed with its look-alikes
        # (§2.3a, §7.15).  The whole row goes, not just the offending
        # endpoint: its value is a fact *about* the look-alike and leads
        # straight back to it.
        if exact_named and any(
            key and key not in exact_named
            and subject_identity_conflict(key, exact_named)
            for key in endpoints.values()
        ):
            continue
        for field in ("subject", "value"):
            key = endpoints[field]
            if not key:
                continue
            # Held back until the named subjects have had their turn: a seed
            # must not answer for a name the store cannot identify (10.7
            # item 3).  The lane discovers rows by an OR-scan, so a question
            # about an unknown "Yealand fold" arrives with a row about
            # "Yealand mill", both of whose endpoints are exact keys -- and
            # the graph answered about the mill.
            deferred_row.append((
                key,
                int(claim_id)
                if field == "value" and isinstance(claim_id, int)
                and not isinstance(claim_id, bool)
                else None,
            ))
        if deferred_row:
            deferred_seeds.append(deferred_row)

    # 3. the word-prefix rule and the one-word alias rule, for a named subject
    #    that did not resolve exactly.  A typed name that is a word-prefix of
    #    nothing resolves nothing: when it looks like a stored key it is a
    #    typo and abstains, otherwise the store has simply never heard of it.
    if allow_non_exact:
        for subject in unresolved_named:
            if _expired(deadline):
                return [], "budget-exceeded"
            typed = entity_key(subject)
            candidates = _word_prefix_candidates(db, scopes, typed)
            if len(typed.split()) == 1:
                # Every last-word alias candidate, not just the unique one.
                # ``alias_subject`` returns the input unchanged when two keys
                # share the last word, which used to leave the candidate list
                # empty and fall through to ``no-start``; two candidates are
                # an ambiguity and must abstain ``identity-conflict`` through
                # the floor below (10.7 item 1, correcting 2.3 source 3).
                candidates.extend(
                    key for key in _alias_candidates(db, scopes, typed)
                    if key and key != typed
                )
            fresh = [
                key for key in dict.fromkeys(candidates)
                if key and key not in claimed
            ]
            if not fresh:
                # A word-prefix of nothing: a misspelling of a stored name
                # abstains, a name the store has never seen resolves nothing
                # and lets the not_recorded cue do its job.
                if near_miss_subject(typed, _near_miss_candidates(db, scopes, typed)):
                    return [], "identity-conflict", []
                unresolved_spellings.append(str(subject))
                continue
            non_exact.extend((key, True) for key in fresh)
            named_non_exact.update(fresh)
            candidates_of[str(subject)] = set(fresh)

    # Every non-exact candidate carries the store-versus-store look-alike
    # floor: the visible key set is read once, lazily, and compared in one
    # batched call (0.7 ms) rather than once per key (14 ms).
    pending: dict[str, bool] = {}
    for key, from_named in non_exact:
        if key in claimed:
            continue
        pending[key] = pending.get(key, False) or from_named
    named_candidate = any(pending.values())
    resolved: str | None = None
    if pending:
        def refuse() -> tuple[list[dict[str, Any]], str | None, list[str]]:
            # A name the operator typed that the store cannot identify
            # abstains the call; anything else is simply dropped.
            return (
                ([], "identity-conflict", []) if named_candidate
                else (starts, None, [])
            )

        candidates = list(pending)
        if len(candidates) > 1:
            return refuse()
        candidate = candidates[0]
        if subject_identity_conflict(candidate, visible_keys() - {candidate}):
            return refuse()
        resolved = candidate

    # A named subject that resolved nothing is reported, not fatal: the call
    # answers from whichever name resolved and the agent says the other was not
    # recorded (10.7 item 4).
    for spelling, keys in candidates_of.items():
        if resolved is None or resolved not in keys:
            unresolved_spellings.append(spelling)

    # Seeds add starts only beside a resolved named subject, or when the
    # question named none (10.7 item 3).
    named_resolved = bool(exact_named) or (
        resolved is not None and resolved in named_non_exact
    )
    if not subjects or named_resolved:
        # The second half of the same rule that drops a seed about a
        # look-alike of an exactly spelled name: a seed about a look-alike of
        # a name the store could NOT identify is dropped too, and for a
        # sharper reason.  The lane OR-scans, so a question about an unknown
        # "Tarnworth mill" comes back with a row about "Tarnworth bolt 2";
        # printed beside the not-recorded line for the mill, that row reads
        # as the answer to it.  Evaluated here rather than in step 2 so a
        # name that resolves non-exactly is not treated as unresolved.
        unresolved_keys = {
            key for key in (entity_key(name) for name in unresolved_spellings) if key
        }
        for deferred_row in deferred_seeds:
            if any(
                _shadows_unresolved(key, unresolved_keys)
                for key, _seed_claim_id in deferred_row
            ):
                continue
            for key, seed_claim_id in deferred_row:
                if key in claimed:
                    continue
                rows = _entity_rows(db, scopes, key)
                if rows:
                    add(key, rows, True, seed_claim_id=seed_claim_id)
    if resolved is not None:
        add(
            resolved, _entity_rows(db, scopes, resolved), False,
            named=resolved in named_non_exact,
        )
    # The store-versus-store floor reads the whole key set, which is the most
    # expensive thing resolution does; check the clock on the way out.
    if _expired(deadline):
        return [], "budget-exceeded", []
    ordered = [
        spelling for spelling in subjects
        if str(spelling) in set(unresolved_spellings)
    ]
    return starts, None, _screened_spellings(ordered)


# --- bounded traversal (design §5.3) -----------------------------------------

_AUTHORITY_ORDER_SQL = (
    "CASE e.authority WHEN 'operator' THEN 100 WHEN 'verified' THEN 70 "
    "WHEN 'learned' THEN 30 ELSE 10 END DESC"
)
_STATUS_ORDER_SQL = "CASE WHEN e.status IN ('active','disputed') THEN 0 ELSE 1 END"
_EDGE_SELECT_SQL = """SELECT e.claim_id, e.scope, e.claim_key, e.predicate_key,
       e.value_kind, e.status, e.authority, e.confidence, e.valid_from,
       e.valid_until, s.entity_key AS src_key, d.entity_key AS dst_key
FROM memory_graph_edges AS e
JOIN memory_graph_entities AS s ON s.id = e.src_entity_id
LEFT JOIN memory_graph_entities AS d ON d.id = e.dst_entity_id"""


def _expired(deadline: float | None) -> bool:
    return deadline is not None and time.monotonic() >= deadline


def _mode_filter(mode: str, as_of: str | None) -> tuple[str, list[Any]]:
    if mode == "as_of" and as_of:
        # The third conjunct is required: a row superseded in place before
        # schema 46 has status 'superseded' with a NULL valid_until, and
        # without it such a legacy row would answer every dated question.
        return (
            "e.valid_from <= ? AND (e.valid_until IS NULL OR e.valid_until > ?) "
            "AND NOT (e.status = 'superseded' AND e.valid_until IS NULL)",
            [str(as_of), str(as_of)],
        )
    if mode == "temporal":
        return "1=1", []
    return "e.status IN ('active','disputed')", []


def _expand_where(
    *,
    ids: Sequence[int],
    direction: str,
    scope_sql: str,
    scope_params: Sequence[Any],
    mode: str,
    as_of: str | None,
    predicates: Sequence[str] = (),
) -> tuple[str, list[Any]]:
    """The WHERE clause both the overflow probe and the fetch share."""
    column = "src_entity_id" if direction == "out" else "dst_entity_id"
    id_placeholders = ",".join("?" for _ in ids)
    mode_sql, mode_params = _mode_filter(mode, as_of)
    where = (
        f"({scope_sql}) AND e.{column} IN ({id_placeholders}) AND ({mode_sql})"
    )
    params: list[Any] = [*scope_params, *[int(value) for value in ids], *mode_params]
    if predicates:
        where += f" AND e.predicate_key IN ({','.join('?' for _ in predicates)})"
        params.extend(str(item) for item in predicates)
    return where, params


def _expand_overflows(
    db: sqlite3.Connection,
    *,
    cap: int,
    ids: Sequence[int],
    direction: str,
    scope_sql: str,
    scope_params: Sequence[Any],
    mode: str,
    as_of: str | None,
    predicates: Sequence[str] = (),
) -> bool:
    """Whether this node has more than ``cap`` edges in this direction.

    Asked without an ORDER BY, so SQLite stops at ``cap + 1`` rows instead of
    sorting the whole fan-out to throw all but seventeen of it away.  A hub of
    three thousand edges cost 3.8 ms of temp b-tree for a decision that reads
    seventeen rows; this is 0.03 ms and the answer is identical, because
    whether a set is larger than a cap does not depend on its order.
    """
    where, params = _expand_where(
        ids=ids, direction=direction, scope_sql=scope_sql,
        scope_params=scope_params, mode=mode, as_of=as_of, predicates=predicates,
    )
    rows = db.execute(
        f"SELECT 1 FROM memory_graph_edges AS e WHERE {where} LIMIT ?",
        [*params, int(cap) + 1],
    ).fetchall()
    return len(rows) > int(cap)


def _expand(
    db: sqlite3.Connection,
    *,
    ids: Sequence[int],
    direction: str,
    limit: int,
    scope_sql: str,
    scope_params: Sequence[Any],
    mode: str,
    as_of: str | None,
    predicates: Sequence[str] = (),
) -> list[sqlite3.Row]:
    where, params = _expand_where(
        ids=ids, direction=direction, scope_sql=scope_sql,
        scope_params=scope_params, mode=mode, as_of=as_of, predicates=predicates,
    )
    sql = (
        f"{_EDGE_SELECT_SQL}\nWHERE {where} "
        f"ORDER BY {_STATUS_ORDER_SQL}, {_AUTHORITY_ORDER_SQL}, e.claim_id DESC LIMIT ?"
    )
    return db.execute(sql, [*params, int(limit)]).fetchall()


def traverse(
    db: sqlite3.Connection,
    *,
    starts: Sequence[Mapping[str, Any]],
    scope_sql: str,
    scope_params: Sequence[Any],
    visible_scopes: Sequence[str],
    mode: str = "now",
    as_of: str | None = None,
    asked: frozenset[str] = frozenset(),
    deadline: float | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str | None]:
    """Walk the graph in both directions from every start, under every bound.

    Returns ``(edges, overflow, budget)``.  The frontier is an entity **key**
    mapped to the set of visible-scope entity ids for that key, expanded once
    with ``IN (…)``, so a name present in both ``global`` and ``project:N`` is
    one node and the fan-out cap applies to the union — otherwise a hub could
    be expanded twice and pass a cap it should have overflowed.  A hub is
    never expanded partially: it overflows whole, at any depth, and the entry
    records the hop so the caller can mark every chain through it incomplete.
    """
    edges: list[dict[str, Any]] = []
    overflow: list[dict[str, Any]] = []
    budget: str | None = None
    if not starts:
        return edges, overflow, budget
    scopes = list(dict.fromkeys(str(scope) for scope in visible_scopes)) or ["global"]
    frontier: deque[tuple[str, tuple[int, ...], int, int | None, str]] = deque(
        (
            str(start["entity_key"]),
            tuple(int(value) for value in start["ids"]),
            0,
            None,
            str(start["entity_key"]),
        )
        for start in starts
    )
    seen = {str(start["entity_key"]) for start in starts}
    ids_by_key: dict[str, tuple[int, ...]] = {}
    # An edge is reachable from both of its ends, so a breadth-first walk meets
    # it twice; the first sighting is the shallowest and is the only one kept,
    # otherwise one stored fact would appear as two hops of one chain.
    walked: set[int] = set()
    stopped = False
    while frontier and not stopped:
        if _expired(deadline):
            budget = "time"
            break
        key, ids, depth, parent, root = frontier.popleft()
        if depth >= MAX_HOPS:
            continue
        terminal = depth == MAX_HOPS - 1
        for direction in ("out", "in"):
            if _expired(deadline):
                budget = "time"
                stopped = True
                break
            base_cap = FANOUT_CAP_TERMINAL if terminal else FANOUT_CAP
            cap = base_cap
            probe = dict(
                ids=ids, direction=direction, scope_sql=scope_sql,
                scope_params=scope_params, mode=mode, as_of=as_of,
            )
            # Ask whether the fan-out overflows before asking for it in order:
            # the ordered fetch sorts the whole fan-out, and a hub is exactly
            # the case where the answer is "do not read it at all".
            narrowed = False
            over = _expand_overflows(db, cap=cap, **probe)
            if over and asked:
                narrowed = True
                cap = FANOUT_CAP_TERMINAL if terminal else FANOUT_CAP_FILTERED
                probe["predicates"] = sorted(asked)
                over = _expand_overflows(db, cap=cap, **probe)
            rows = (
                []
                if over
                else _expand(db, limit=cap + 1, **probe)
            )
            # A node whose fan-out exceeded the cap and whose narrowed re-query
            # matched nothing was NOT read: none of its edges was seen, so the
            # walk past it is as incomplete as an ordinary overflow.  Reporting
            # it is invariant 3 (never present a pruned list as complete)
            # outranking the "narrow and proceed" shape of the §5.3 sketch.
            if over or (narrowed and not rows):
                overflow.append({
                    "entity_key": key, "direction": direction,
                    "cap": cap if over else base_cap,
                    "hop": depth + 1, "parent": parent, "root": root,
                })
                continue
            for row in rows:
                if int(row["claim_id"]) in walked:
                    continue
                walked.add(int(row["claim_id"]))
                other = row["dst_key"] if direction == "out" else row["src_key"]
                edge = {
                    "index": len(edges),
                    "claim_id": int(row["claim_id"]),
                    "scope": str(row["scope"]),
                    "claim_key": str(row["claim_key"]),
                    "predicate_key": str(row["predicate_key"]),
                    "value_kind": str(row["value_kind"]),
                    "status": str(row["status"]),
                    "authority": str(row["authority"]),
                    "confidence": float(row["confidence"]),
                    "valid_from": str(row["valid_from"]),
                    "valid_until": row["valid_until"],
                    "src_key": str(row["src_key"]),
                    "dst_key": None if row["dst_key"] is None else str(row["dst_key"]),
                    "direction": direction,
                    "hop": depth + 1,
                    "parent": parent,
                    "node_key": key,
                    "other_key": None if other is None else str(other),
                    "root": root,
                }
                edges.append(edge)
                if len(edges) >= EDGE_BUDGET:
                    budget = "edges"
                    stopped = True
                    break
                if edge["other_key"] and edge["other_key"] not in seen:
                    if len(seen) >= NODE_BUDGET:
                        budget = "nodes"
                        continue
                    neighbour = edge["other_key"]
                    if neighbour not in ids_by_key:
                        ids_by_key[neighbour] = tuple(
                            int(item["id"])
                            for item in _entity_rows(db, scopes, neighbour)
                        )
                    if not ids_by_key[neighbour]:
                        continue
                    seen.add(neighbour)
                    frontier.append(
                        (neighbour, ids_by_key[neighbour], depth + 1, edge["index"], root)
                    )
    return edges, overflow, budget


# --- chains and ranking (design §5.4) ----------------------------------------

def _weight(authority: str) -> int:
    return AUTHORITY_WEIGHT.get(str(authority), 0)


def _predicate_words(edge: Mapping[str, Any]) -> set[str]:
    return set(str(edge["predicate_key"]).split())


def _answers(edge: Mapping[str, Any], asked: frozenset[str]) -> bool:
    """Whether this one hop matches the asked predicate."""
    return not asked or bool(_predicate_words(edge) & asked)


def _chain_answers(
    edges: Sequence[Mapping[str, Any]], path: Sequence[int], asked: frozenset[str]
) -> bool:
    """Whether a whole walk answers: **any** hop matching is enough.

    Design 10.3 item 3 replaces the terminal-only rule.  Reading two hops
    further back from a name the question did name is still an answer to it:
    "Which relays are in the Northgate region?" reaches the relay through
    ``region`` at hop 1, and under the terminal-only rule the hops that
    carried the answer were dropped for not mentioning it themselves.  A
    matching terminal still ranks first, through the overlap term in the
    rank tuple.
    """
    if not asked:
        return True
    return any(_answers(edges[int(index)], asked) for index in path)


def _path_of(edges: Sequence[Mapping[str, Any]], index: int) -> list[int]:
    path: list[int] = []
    cursor: int | None = index
    while cursor is not None:
        path.append(int(cursor))
        cursor = edges[int(cursor)]["parent"]
    path.reverse()
    return path


def _walk_key(chain: Mapping[str, Any]) -> tuple[str, str, int]:
    """Which walk a chain belongs to.

    Chains of one walk share a start, a direction and their first edge: the
    two-hop reading of a path continues from the one-hop reading of it.
    """
    first = chain["path"][0] if chain["path"] else chain["terminals"][0]
    return (str(chain["start_key"]), str(chain["direction"]), int(first))


def build_chains(
    edges: Sequence[Mapping[str, Any]],
    overflow: Sequence[Mapping[str, Any]],
    *,
    asked: frozenset[str] = frozenset(),
    budget: str | None = None,
    chain_cap: int = CHAIN_CAP,
    named_starts: Sequence[str] = (),
) -> tuple[list[dict[str, Any]], int, dict[str, Any] | None]:
    """Group the walk's edges into ranked chains.

    Sibling terminal edges are **one** chain, not many: forty walks that share
    every hop but the last are one group with several rows at the last hop, so
    ``chain_cap`` counts groups.  Ranking is terminal-predicate overlap (desc),
    hops (asc), the chain's **minimum** authority (desc), current before
    superseded, newest terminal claim id (desc).

    A chain is exactly as strong as its weakest hop: ``weakest_index`` names
    that hop (earliest on a tie) and ``chain_authority`` is set when it is
    below ``operator``.  A chain that passed through an overflowing hub, or
    that was walking when a budget ran out, is ``incomplete``.

    Returns ``(chains, dropped, cut)``; ``dropped`` is how many answering
    walks the cap left out and ``cut`` is ``{"key", "hop"}`` for the first of
    them -- the node it continues from -- so the note can name something the
    operator can actually ask about.
    """
    # Keyed by (start entity, path prefix, direction).  Hop-1 edges from two
    # different named subjects share the empty prefix but are not siblings,
    # and merging them produced one chain of eight rows about one subject with
    # a note claiming to count both; an out-edge and an in-edge from the same
    # start are not siblings either ("the box is in Fenwick" and "the relay
    # runs on the box" are two answers, not one list).
    named = {str(key) for key in named_starts}
    overflowing = {str(entry["entity_key"]) for entry in overflow}
    groups: dict[tuple[str, tuple[int, ...], str], list[int]] = {}
    for edge in edges:
        path = _path_of(edges, int(edge["index"]))
        if not _chain_answers(edges, path, asked):
            continue
        groups.setdefault(
            (str(edge["root"]), tuple(path[:-1]), str(edge["direction"])), []
        ).append(int(edge["index"]))
    chains: list[dict[str, Any]] = []
    for (root, prefix, direction), terminals in groups.items():
        ranked = sorted(
            terminals,
            key=lambda index: (
                -len(_predicate_words(edges[index]) & asked),
                0 if edges[index]["status"] in CURRENT_STATUSES else 1,
                -_weight(edges[index]["authority"]),
                -int(edges[index]["claim_id"]),
            ),
        )
        best = edges[ranked[0]]
        members = [edges[index] for index in (*prefix, ranked[0])]
        weakest_weight = min(_weight(edge["authority"]) for edge in members)
        weakest_index = next(
            index for index in (*prefix, ranked[0])
            if _weight(edges[index]["authority"]) == weakest_weight
        )
        superseded = any(edge["status"] not in CURRENT_STATUSES for edge in members)
        chains.append({
            "path": list(prefix),
            "terminals": ranked,
            # Terminals that walk INTO a node whose own expansion overflowed:
            # the chain ends at a hub, so that row is as incomplete as one
            # that passed through it, while a sibling ending elsewhere is not
            # (10.7 item 6).
            "hub_terminals": [
                index for index in ranked
                if str(edges[index].get("other_key") or "") in overflowing
            ],
            "hops": len(prefix) + 1,
            "start_key": str(root),
            "terminal_count": len(ranked),
            "weakest_index": int(weakest_index),
            "weakest_authority": str(edges[weakest_index]["authority"]),
            "chain_authority": (
                None if weakest_weight >= AUTHORITY_WEIGHT["operator"]
                else str(edges[weakest_index]["authority"])
            ),
            "superseded": superseded,
            "named": str(root) in named,
            "direction": str(direction),
            "_rank": (
                -len(_predicate_words(best) & asked),
                # A chain from a name the operator typed outranks one from a
                # row the lane happened to hand over, whatever its shape: the
                # seed's own forward chain must never take the slot the asked
                # subject's answer needs (exit test 7.3 row 2).
                0 if str(root) in named else 1,
                len(prefix) + 1,
                -weakest_weight,
                1 if superseded else 0,
                -int(best["claim_id"]),
            ),
        })
    chains.sort(key=lambda chain: chain["_rank"])
    cap = max(0, int(chain_cap))
    selected: list[dict[str, Any]] = []
    # The cap counts WALKS, not chain entries.  Since 10.3 item 3 a walk
    # answers at whichever hop matches and every longer reading of it answers
    # too, so one walk can produce several entries -- the design 1.1 case
    # produces three, and counting entries dropped the very hops that carried
    # the answer.  Named starts first, and one walk per (start, direction)
    # before any of them gets a second: two subjects with nine facts each must
    # not become eight rows about the first of them, and an open question at a
    # hub has one walk per direction.  Seed-derived starts take what is left.
    walks: set[tuple[str, str, int]] = set()

    def admit(chain: dict[str, Any]) -> bool:
        key = _walk_key(chain)
        if key in walks:
            selected.append(chain)
            return True
        if len(walks) >= cap:
            return False
        walks.add(key)
        selected.append(chain)
        return True

    for candidates in (
        [chain for chain in chains if chain["named"]],
        [chain for chain in chains if not chain["named"]],
    ):
        covered: set[tuple[str, str]] = set()
        for chain in candidates:
            facet = (str(chain["start_key"]), str(chain["direction"]))
            if facet in covered:
                continue
            if admit(chain):
                covered.add(facet)
        for chain in candidates:
            if all(chain is not picked for picked in selected):
                admit(chain)
    dropped = max(0, len({_walk_key(chain) for chain in chains}) - len(walks))
    # The first walk the cap left out, in rank order: its terminal hangs off
    # some node, and that node is the one the operator can ask about to see
    # the rest.
    cut: dict[str, Any] | None = None
    for chain in chains:
        if _walk_key(chain) in walks:
            continue
        terminal = edges[int(chain["terminals"][0])]
        cut = {
            "key": str(terminal.get("node_key") or ""),
            "hop": len(chain["path"]) + 1,
        }
        break
    chains = sorted(selected, key=lambda chain: chain["_rank"])
    # One walk, one chain number: a chain that continues from an earlier
    # chain's terminal is the same path read one hop further, not a second
    # answer.  Grouping merges sibling terminals; numbering merges a path with
    # its own continuation.
    numbered: list[dict[str, Any]] = []
    next_number = 0
    for chain in chains:
        chain.pop("_rank", None)
        shared = next(
            (
                earlier["chain"] for earlier in numbered
                if earlier["start_key"] == chain["start_key"]
                and len(chain["path"]) > len(earlier["path"])
                and chain["path"][:len(earlier["path"])] == earlier["path"]
                and chain["path"][len(earlier["path"])] in earlier["terminals"]
            ),
            None,
        )
        if shared is None:
            next_number += 1
            shared = next_number
        chain["chain"] = shared
        numbered.append(chain)
        # Passing THROUGH a hub marks the whole chain; ending AT one marks
        # only the row that ends there, which is why the hub set is consulted
        # per terminal rather than over the whole walk.
        chain["incomplete"] = budget is not None or any(
            (entry["parent"] is not None and int(entry["parent"]) in set(chain["path"]))
            or (entry["parent"] is None and str(entry["root"]) == chain["start_key"])
            for entry in overflow
        )
    return chains, dropped, cut


def chain_claim_ids(
    edges: Sequence[Mapping[str, Any]],
    chains: Sequence[Mapping[str, Any]],
    *,
    limit: int = SCREENED_ROW_CAP,
    seed_hops: Mapping[str, int] | None = None,
) -> list[int]:
    """The distinct claim ids of the ranked chains, in emission order, capped
    at ``SCREENED_ROW_CAP`` — exactly the rows the caller loads and screens.

    A chain whose start came from a seed claim's value leads with that claim,
    so its id is loaded and screened like any other hop.
    """
    ordered: list[int] = []
    seen: set[int] = set()

    def take(claim_id: int) -> bool:
        if claim_id in seen:
            return True
        seen.add(claim_id)
        ordered.append(claim_id)
        return len(ordered) < int(limit)

    for chain in chains:
        seed_claim_id = (seed_hops or {}).get(str(chain.get("start_key")))
        if seed_claim_id is not None and not take(int(seed_claim_id)):
            return ordered
        for index in (*chain["path"], *chain["terminals"]):
            if not take(int(edges[int(index)]["claim_id"])):
                return ordered
    return ordered


# --- the whole walk (design §5.1-5.4) ----------------------------------------

def graph_walk(
    db: sqlite3.Connection,
    *,
    visible_scopes: Sequence[str],
    query: str = "",
    scope_sql: str | None = None,
    scope_params: Sequence[Any] | None = None,
    project_scope: str | None = None,
    subjects: Sequence[str] = (),
    seed_claims: Sequence[Mapping[str, Any]] = (),
    temporal: bool = False,
    as_of: str | None = None,
    exact_only: bool = False,
    deadline: float | None = None,
    asked: frozenset[str] | None = None,
    predicate_words: frozenset[str] | None = None,
) -> dict[str, Any]:
    """Resolve, traverse and rank in one call; the caller then loads
    ``walk["claim_ids"]``, keeps the eligible rows and passes them to
    ``assemble_rows``.

    ``scope_sql`` / ``scope_params`` are the caller's own claim scope filter
    with alias ``e`` (``Memory._claim_scope_filter``), so the graph and the
    claims lane can never diverge; ``default_scope_filter`` builds an
    equivalent one when they are omitted.  ``exact_only`` is what the caller
    passes when the lane abstained on an identity floor.
    """
    words = asked_predicate_words(query) if asked is None else frozenset(asked)
    walk: dict[str, Any] = {
        "edges": [], "chains": [], "overflow": [], "claim_ids": [],
        "starts": 0, "expanded": 0, "mode": "idle", "budget": None,
        "asked": words, "exact_only": bool(exact_only), "seed_hops": {},
        "truncated_chains": 0, "asked_unmatched": False, "unresolved": [],
        "truncated_from": None,
    }
    if not graph_ready(db):
        return walk
    scopes = list(dict.fromkeys(str(scope) for scope in visible_scopes)) or ["global"]
    if scope_sql is None or scope_params is None:
        scope_sql, scope_params = default_scope_filter(scopes, project_scope)
    # A question word that names no stored predicate must not narrow anything:
    # "Which relays are in the Northgate region?" was dropping the chain that
    # reaches the relay because "relays" is not a predicate here.
    if words:
        known = (
            _visible_predicate_words(db, scopes)
            if predicate_words is None else frozenset(predicate_words)
        )
        words, unmatched = narrow_asked_words(
            words, subjects=subjects, known=known,
            vocabulary=lambda nouns: _vocabulary_hits(db, scopes, nouns),
        )
        walk["asked"] = words
        walk["asked_unmatched"] = unmatched
    starts, conflict, unresolved = resolve_starts(
        db, subjects=subjects, seed_claims=seed_claims,
        visible_scopes=scopes, allow_non_exact=not exact_only, deadline=deadline,
    )
    walk["unresolved"] = unresolved
    if conflict is not None:
        walk["mode"] = conflict
        if conflict == "budget-exceeded":
            walk["budget"] = "time"
        return walk
    if _expired(deadline):
        walk["mode"] = "budget-exceeded"
        walk["budget"] = "time"
        return walk
    walk["starts"] = len(starts)
    # A start taken from a seed claim's value: the chain onward from it opens
    # with that claim, so the model sees the whole path rather than a fact
    # about a name the block never introduced.
    seed_hops = {
        str(start["entity_key"]): int(start["seed_claim_id"])
        for start in starts
        if start.get("seed_claim_id") is not None
    }
    walk["seed_hops"] = seed_hops
    if not starts:
        walk["mode"] = "no-start"
        return walk
    mode = "as_of" if as_of else ("temporal" if temporal else "now")
    edges, overflow, budget = traverse(
        db, starts=starts, scope_sql=scope_sql, scope_params=list(scope_params),
        visible_scopes=scopes, mode=mode, as_of=as_of, asked=words, deadline=deadline,
    )
    if budget is None and _expired(deadline):
        # The loop's own checks can be passed by a single long expansion; the
        # call still overran, and every chain it produced is truncated.
        budget = "time"
    chains, dropped, cut = build_chains(
        edges, overflow, asked=words, budget=budget,
        named_starts=[
            str(start["entity_key"]) for start in starts if start.get("named")
        ],
    )
    walk["truncated_chains"] = dropped
    walk["truncated_from"] = dict(cut) if cut else None
    # The name the sibling terminals share, so "ask about one by name" points
    # at the hub rather than at one arbitrary answer (design §5.4).
    label_keys = {
        str(edges[int(chain["terminals"][0])]["node_key"]) for chain in chains
    }
    if cut and cut.get("key"):
        label_keys.add(str(cut["key"]))
    hub_labels = _labels_for(db, label_keys, scopes)
    if walk["truncated_from"]:
        # Screened at capture: a label that fails the screen must not travel
        # in the walk at all, not merely be skipped when the note is built.
        display = hub_labels.get(str(cut["key"]), str(cut["key"]))
        walk["truncated_from"]["label"] = (
            "" if screen_endpoint(display)[0] else display
        )
    for chain in chains:
        chain["hub_label"] = hub_labels.get(
            str(edges[int(chain["terminals"][0])]["node_key"]), ""
        )
    walk["edges"] = edges
    walk["chains"] = chains
    walk["overflow"] = _labelled_overflow(db, overflow, scopes)
    walk["claim_ids"] = chain_claim_ids(edges, chains, seed_hops=seed_hops)
    walk["budget"] = budget
    walk["expanded"] = len(
        {str(edge["node_key"]) for edge in edges}
        | {str(start["entity_key"]) for start in starts}
    )
    if chains:
        walk["mode"] = "complete"
    elif overflow:
        walk["mode"] = "overflow"
    else:
        walk["mode"] = "no-answer"
    return walk


def _visible_predicate_words(
    db: sqlite3.Connection, visible_scopes: Sequence[str]
) -> frozenset[str]:
    """Every word that appears in a stored predicate key in these scopes.

    One query per call (the caller may pass its own cached set through
    ``graph_walk(predicate_words=...)``).  An asked word outside this set
    names no stored predicate, so it must neither narrow a fan-out nor drop a
    chain.
    """
    scopes = list(visible_scopes)
    placeholders = ",".join("?" for _ in scopes)
    words: set[str] = set()
    for row in db.execute(
        "SELECT DISTINCT predicate_key FROM memory_graph_edges "
        f"WHERE scope IN ({placeholders})",
        scopes,
    ).fetchall():
        words.update(str(row[0]).split())
    return frozenset(words)


def _visible_entity_words(
    db: sqlite3.Connection, visible_scopes: Sequence[str]
) -> frozenset[str]:
    """Every word the visible store knows as a subject or an entity value.

    Materialises the whole vocabulary; ``_vocabulary_hits`` is what the read
    path uses, because at ten thousand entities this costs 3.6 ms of Python
    string splitting and only a handful of words are ever asked about.  Kept
    for tests and for a caller that wants to cache the set itself.
    """
    scopes = list(visible_scopes)
    placeholders = ",".join("?" for _ in scopes)
    words: set[str] = set()
    for row in db.execute(
        "SELECT DISTINCT entity_key FROM memory_graph_entities "
        f"WHERE scope IN ({placeholders})",
        scopes,
    ).fetchall():
        words.update(str(row[0]).split())
    return frozenset(words)


def _vocabulary_hits(
    db: sqlite3.Connection, visible_scopes: Sequence[str], words: frozenset[str]
) -> frozenset[str]:
    """Which of ``words`` the visible store knows as a subject or value word.

    One short-circuiting query per word instead of materialising the whole
    vocabulary: the four patterns cover the word alone, at the start, at the
    end and in the middle of an entity key, SQLite stops at the first hit,
    and the string work happens in C.  A trailing plural is probed too.
    """
    scopes = list(visible_scopes)
    if not words or not scopes:
        return frozenset()
    placeholders = ",".join("?" for _ in scopes)
    statement = (
        "SELECT 1 FROM memory_graph_entities "
        f"WHERE scope IN ({placeholders}) AND ("
        "entity_key = ?1x OR entity_key LIKE ?2x OR entity_key LIKE ?3x "
        "OR entity_key LIKE ?4x) LIMIT 1"
    ).replace("?1x", "?").replace("?2x", "?").replace("?3x", "?").replace("?4x", "?")
    found: set[str] = set()
    for word in words:
        for probe in {word, _singular(word)}:
            escaped = _like_escaped(probe)
            row = db.execute(
                statement,
                [*scopes, probe, escaped + " %", "% " + escaped, "% " + escaped + " %"],
            ).fetchone()
            if row is not None:
                found.add(word)
                break
    return frozenset(found)


def _labels_for(
    db: sqlite3.Connection, keys: set[str], visible_scopes: Sequence[str]
) -> dict[str, str]:
    """One batched read of the display spellings of a set of entity keys."""
    wanted = sorted(key for key in keys if key)
    if not wanted:
        return {}
    placeholders = ",".join("?" for _ in wanted)
    scope_placeholders = ",".join("?" for _ in visible_scopes)
    labels: dict[str, str] = {}
    for row in db.execute(
        "SELECT entity_key, label FROM memory_graph_entities "
        f"WHERE entity_key IN ({placeholders}) AND scope IN ({scope_placeholders}) "
        "ORDER BY scope <> 'global'",
        [*wanted, *[str(scope) for scope in visible_scopes]],
    ).fetchall():
        labels[str(row[0])] = str(row[1])
    return labels


def _labelled_overflow(
    db: sqlite3.Connection,
    overflow: Sequence[Mapping[str, Any]],
    visible_scopes: Sequence[str],
) -> list[dict[str, Any]]:
    if not overflow:
        return []
    labels = _labels_for(
        db, {str(entry["entity_key"]) for entry in overflow}, visible_scopes
    )
    return [
        {**entry, "label": labels.get(str(entry["entity_key"]), str(entry["entity_key"]))}
        for entry in overflow
    ]


# --- screened cue rows (design §5.4-5.6, §5.8) --------------------------------

def _row_text(claim: Mapping[str, Any], name: str, fallback: str = "") -> str:
    value = _field(claim, name)
    return fallback if value is None else str(value)


def _hub_note(entry: Mapping[str, Any]) -> str:
    return (
        f"More than {int(entry['cap'])} stored facts link to this name at hop "
        f"{int(entry['hop'])}; the chain above is incomplete. Ask about one by name."
    )


CHAIN_NOTE_MAX_CHARS = 130


def _chain_truncation_template(dropped: int, label: str) -> str:
    plural = "" if int(dropped) == 1 else "s"
    return (
        f"{int(dropped)} more chain{plural} found and not shown; the first "
        f"continues from {label}. Ask about {label} by name."
    )


def _fit_note_label(label: str, dropped: int) -> str:
    """The label clipped so the note stays inside its bound.

    The name appears twice in the sentence and an entity label may be 80
    characters, which is 240 characters of note; the surface clips a note at
    200 anyway, so an unclipped name would be cut mid-sentence there instead
    of deliberately here.  Clipped visibly, because a silently shortened name
    would read as one the operator could ask about verbatim.
    """
    text = str(label)
    fixed = len(_chain_truncation_template(dropped, ""))
    budget = max(0, (CHAIN_NOTE_MAX_CHARS - 1 - fixed) // 2)
    if len(text) <= budget:
        return text
    return text[: max(0, budget - 3)] + "..."


def _chain_truncation_note(dropped: int, label: str) -> str:
    """The chain-cap note: what was cut, and the name to ask about.

    Distinct from the hub sibling-count note, whose wording is unchanged.  The
    old text said "N more stored chains answer this; ask about one by name",
    and the live battery showed a model rendering it as "at least 2 more
    stored facts ... didn't fit in the context window": chains became facts,
    the count drifted, and "one by name" named nothing the operator could ask
    about.  A cut chain is a *continuation*, so it never "answers this" -- it
    continues from somewhere, and the note says where.
    """
    return _chain_truncation_template(dropped, _fit_note_label(label, dropped))


def _sibling_note(found: int, shown: int) -> str:
    return (
        f"{int(found)} stored facts answer this; the {int(shown)} strongest are "
        "shown. Ask about one by name for the rest."
    )


def assemble_rows(
    walk: Mapping[str, Any],
    claim_rows: Mapping[int, Mapping[str, Any]],
    *,
    limit: int = CHAIN_ROW_CAP,
    deadline: float | None = None,
    started: float | None = None,
    screen: Callable[[str], tuple[bool, str | None]] = screen_endpoint,
    match_subject_keys: Sequence[str] = (),
) -> dict[str, Any]:
    """Screen the walk's chains and render the cue rows.

    ``claim_rows`` is ``claim_id -> row`` for the rows that already passed the
    caller's eligibility check; rows it does not contain are treated as
    dropped.  Every returned row's **subject and value** are screened here
    with the widened screen — the write path screens a subject for secrets
    only, and a chain row's subject can also arrive through the seed-claim
    path or through a row projected by migration 48, so this is the last gate
    before the model.  A dropped row breaks its chain at that hop: the rows
    after it are dropped too, and a chain with no surviving terminal no longer
    answers.  The whole-call deadline is checked before the loop and after
    every screened row; on expiry what is screened so far is returned with
    every surviving chain marked ``incomplete``.
    """
    edges: Sequence[Mapping[str, Any]] = walk.get("edges") or []
    walk_mode = str(walk.get("mode") or "idle")
    overflow_entries = list(walk.get("overflow") or [])
    report: dict[str, Any] = {
        "channel": "graph",
        "mode": walk_mode,
        "budget": walk.get("budget"),
        "starts": int(walk.get("starts") or 0),
        "expanded": int(walk.get("expanded") or 0),
        "edges": len(edges),
        "chains": 0,
        "rows": 0,
        "overflow": len(overflow_entries),
        "overflow_hubs": len(overflow_entries),
        "sibling_notes": 0,
        "truncated_chains": int(walk.get("truncated_chains") or 0),
        "incomplete": 0,
        "excluded_by_screen": 0,
        # Names the operator typed that resolved nothing, in question order,
        # screened and capped: the agent turns each into one not-recorded
        # line so half an answered question is visibly half (10.7 item 4).
        "unresolved": list(walk.get("unresolved") or []),
        "elapsed_ms": 0.0,
    }
    if walk_mode in {"idle", "no-start", "identity-conflict"}:
        return _finish({"rows": [], "overflow": [], "report": report}, started)
    # ``match: subject`` marks a row the model must read as *context*, not as
    # an answer: the lead sentence tells it such entries are not the asked
    # fact.  So it belongs only on a chain that does not answer the asked
    # predicate, never on an answering chain and never on an open question,
    # where every chain answers.  Since a chain that reaches this point always
    # answers, the tag is emitted only when the question asked for a predicate
    # the store has no word for (walk["asked_unmatched"]).
    match_keys = (
        {str(key) for key in match_subject_keys}
        if walk.get("asked_unmatched") else set()
    )
    screened: dict[int, Mapping[str, Any] | None] = {}
    expired = _expired(deadline)

    def keep(claim_id: int) -> Mapping[str, Any] | None:
        """The screened claim row, or ``None`` when it must not be shown."""
        nonlocal expired
        if claim_id in screened:
            return screened[claim_id]
        claim = claim_rows.get(int(claim_id))
        if claim is None:
            screened[claim_id] = None
            return None
        # A redaction placeholder passes every screen -- the write path
        # already removed the secret -- but it answers nothing, and the
        # sealed holdout emitted two of them as a chain's answer to "what is
        # located at the Tarn bay".  It is dropped here like a screened row.
        blocked = (
            is_redaction_placeholder(_row_text(claim, "value"))
            or screen(_row_text(claim, "value"))[0]
            or screen(_row_text(claim, "subject"))[0]
        )
        if blocked:
            report["excluded_by_screen"] += 1
        screened[claim_id] = None if blocked else claim
        # The screen phase is the dominant term in the budget, so the deadline
        # is checked after every screened row and not only around traversal.
        expired = expired or _expired(deadline)
        return screened[claim_id]

    rows: list[dict[str, Any]] = []
    notes: list[dict[str, Any]] = []
    emitted_rows: dict[int, dict[str, Any]] = {}
    # Claim ids whose row walks into a node that overflowed: incomplete even
    # when the rest of their chain is whole (10.7 item 6).
    ends_at_hub: set[int] = set()
    answered = 0
    pending_chains = list(walk.get("chains") or [])
    for position, chain in enumerate(pending_chains):
        if expired or len(rows) >= int(limit):
            break
        path_rows: list[tuple[Mapping[str, Any], Mapping[str, Any]]] = []
        broken = False
        seed_pair = _seed_hop(walk, chain, edges, keep)
        if seed_pair is not None:
            path_rows.append(seed_pair)
        for index in chain["path"]:
            edge = edges[int(index)]
            claim = keep(int(edge["claim_id"]))
            if claim is None or expired:
                broken = True
                break
            path_rows.append((edge, claim))
        if broken:
            continue
        hub_terminals = {int(index) for index in chain.get("hub_terminals") or ()}
        terminal_rows: list[tuple[Mapping[str, Any], Mapping[str, Any]]] = []
        for index in chain["terminals"]:
            edge = edges[int(index)]
            claim = keep(int(edge["claim_id"]))
            if claim is not None:
                terminal_rows.append((edge, claim))
                if int(index) in hub_terminals:
                    ends_at_hub.add(int(edge["claim_id"]))
            if expired:
                break
        if not terminal_rows:
            continue
        if _emit_chain(
            chain, path_rows, terminal_rows, rows, notes, emitted_rows,
            chain_number=int(chain.get("chain") or answered + 1),
            limit=int(limit), match_keys=match_keys, expired=expired,
            reserved=max(0, len(pending_chains) - position - 1),
            ends_at_hub=ends_at_hub,
        ):
            answered += 1
    # Only the cap can truncate, so a block that emitted fewer chains than the
    # cap allows truncated nothing: it had room and the remaining candidates
    # were the same walk read again.  Reporting otherwise produced the note
    # "1 more stored chains answer this" beside a single emitted chain.
    truncated_chains = (
        int(walk.get("truncated_chains") or 0) if answered >= CHAIN_CAP else 0
    )
    cut = walk.get("truncated_from") or {}
    cut_label = str(cut.get("label") or "")
    if truncated_chains and rows and cut_label:
        # The cap dropped answering walks; name the node the first one
        # continues from, so "ask by name" is something the operator can act
        # on.  A note that cannot name it is not emitted at all -- the count
        # still stands in report["truncated_chains"] -- and a label that
        # fails the screen is dropped by _overflow_notes like any other.
        notes.append({
            "subject": cut_label,
            "predicate": "", "value": "", "status": "overflow",
            "hop": int(cut.get("hop") or rows[0].get("hop") or 1),
            "note": _chain_truncation_note(truncated_chains, cut_label),
        })
    report["truncated_chains"] = truncated_chains
    report["chains"] = len({row["chain"] for row in rows})
    report["rows"] = len(rows)
    report["incomplete"] = sum(1 for row in rows if row.get("incomplete"))
    budget = "time" if expired else walk.get("budget")
    if expired or walk.get("budget"):
        report["mode"] = "budget-exceeded"
        report["budget"] = budget
    elif not rows:
        if walk.get("chains"):
            report["mode"] = "screened-rows"
        elif overflow_entries:
            report["mode"] = "overflow"
        else:
            report["mode"] = "no-answer"
    else:
        report["mode"] = "complete"
    emitted_notes = _overflow_notes(overflow_entries, notes, screen)
    report["overflow_hubs"] = len(overflow_entries)
    report["sibling_notes"] = len(notes)
    report["overflow"] = len(overflow_entries) + len(notes)
    return _finish({"rows": rows, "overflow": emitted_notes, "report": report}, started)


def _seed_hop(
    walk: Mapping[str, Any],
    chain: Mapping[str, Any],
    edges: Sequence[Mapping[str, Any]],
    keep: Any,
) -> tuple[Mapping[str, Any], Mapping[str, Any]] | None:
    """The seed claim a chain starts from, as a hop-1 pair.

    ``None`` when the chain did not start from a seed value, when the seed
    claim is already one of the chain's own hops (the walk found the same
    edge from the other end), or when its row is missing or screened.  The
    synthetic edge carries only what ``_cue_row`` reads as a fallback.
    """
    seed_claim_id = (walk.get("seed_hops") or {}).get(str(chain.get("start_key")))
    if seed_claim_id is None:
        return None
    seed_claim_id = int(seed_claim_id)
    for index in (*chain["path"], *chain["terminals"]):
        if int(edges[int(index)]["claim_id"]) == seed_claim_id:
            return None
    claim = keep(seed_claim_id)
    if claim is None:
        return None
    return (
        {
            "claim_id": seed_claim_id,
            "status": _row_text(claim, "status", "active"),
            "authority": _row_text(claim, "authority", "operator"),
            "confidence": float(_field(claim, "confidence", 1.0) or 0.0),
            "valid_until": _field(claim, "valid_until"),
        },
        claim,
    )


def _emit_chain(
    chain: Mapping[str, Any],
    path_rows: Sequence[tuple[Mapping[str, Any], Mapping[str, Any]]],
    terminal_rows: Sequence[tuple[Mapping[str, Any], Mapping[str, Any]]],
    rows: list[dict[str, Any]],
    notes: list[dict[str, Any]],
    emitted_rows: dict[int, dict[str, Any]],
    *,
    chain_number: int,
    limit: int,
    match_keys: set[str],
    expired: bool,
    reserved: int = 0,
    ends_at_hub: set[int] | None = None,
) -> bool:
    """Render one chain into ``rows``; return whether it emitted anything.

    A hop already emitted under an earlier chain is written once and only
    once, and still serves as the ``bridge_from`` of the hop after it, so a
    shared prefix costs one row rather than two.  When *this* chain is
    incomplete the shared row is marked too: the marker has to survive the
    tail-shrink, and the row that carries it may be the one an earlier chain
    already placed.
    """
    fresh_path = sum(
        1 for edge, _claim in path_rows
        if int(edge["claim_id"]) not in emitted_rows
    )
    # One row is held back for each chain still to come, so a first chain with
    # forty answers cannot spend the whole block and leave the operator's
    # second named subject out of it entirely -- but never more than half of
    # what is left, so the reservation cannot starve the chain in hand.
    available = limit - len(rows) - fresh_path
    room = available - min(max(0, int(reserved)), max(0, available // 2))
    shown_terminals = terminal_rows[: max(0, room)]
    if not shown_terminals:
        return False
    incomplete = (
        bool(chain.get("incomplete"))
        or expired
        or len(shown_terminals) < len(terminal_rows)
    )
    members = [*path_rows, shown_terminals[0]]
    weakest = min(_weight(str(edge["authority"])) for edge, _claim in members)
    weakest_claim_id = next(
        int(edge["claim_id"]) for edge, _claim in members
        if _weight(str(edge["authority"])) == weakest
    )
    previous: dict[str, Any] | None = None
    emitted_here = 0
    hop = 0
    for edge, claim in path_rows:
        hop += 1
        row = _cue_row(
            edge, claim, chain=chain_number, hop=hop, previous=previous,
            weakest=int(edge["claim_id"]) == weakest_claim_id,
            chain_authority=None, incomplete=incomplete,
            match=hop == 1 and str(chain["start_key"]) in match_keys,
        )
        existing = emitted_rows.get(int(edge["claim_id"]))
        if existing is None:
            emitted_rows[int(edge["claim_id"])] = row
            rows.append(row)
            emitted_here += 1
        elif incomplete:
            existing["incomplete"] = True
        previous = row
    hop += 1
    for edge, claim in shown_terminals:
        existing = emitted_rows.get(int(edge["claim_id"]))
        if existing is not None:
            if incomplete:
                existing["incomplete"] = True
            continue
        terminal_row = _cue_row(
            edge, claim, chain=chain_number, hop=hop, previous=previous,
            weakest=int(edge["claim_id"]) == weakest_claim_id,
            chain_authority=chain.get("chain_authority"),
            incomplete=incomplete or int(edge["claim_id"]) in (ends_at_hub or set()),
            match=hop == 1 and str(chain["start_key"]) in match_keys,
        )
        emitted_rows[int(edge["claim_id"])] = terminal_row
        rows.append(terminal_row)
        emitted_here += 1
    if not emitted_here:
        # Every row was already shown under an earlier chain: this is the same
        # walk read again, not a second answer.
        return False
    if len(shown_terminals) < int(chain["terminal_count"]):
        notes.append({
            # The name the answers share, not one of the answers.
            "subject": str(chain.get("hub_label") or "")
            or _row_text(shown_terminals[0][1], "subject"),
            "predicate": "", "value": "", "status": "overflow", "hop": hop,
            "note": _sibling_note(int(chain["terminal_count"]), len(shown_terminals)),
        })
    return True


def _cue_row(
    edge: Mapping[str, Any],
    claim: Mapping[str, Any],
    *,
    chain: int,
    hop: int,
    previous: Mapping[str, Any] | None,
    weakest: bool,
    chain_authority: str | None,
    incomplete: bool,
    match: bool,
) -> dict[str, Any]:
    status = _row_text(claim, "status", str(edge["status"]))
    row: dict[str, Any] = {
        # The claim id is what the surface dedupes the block by (design 5.8);
        # the scope is store-side provenance and is kept out of the model
        # whitelist by the surface (design 10.3 item 4).
        "claim_id": int(edge["claim_id"]),
        "scope": _row_text(claim, "scope", str(edge.get("scope") or "")),
        "subject": _row_text(claim, "subject"),
        "predicate": _row_text(claim, "predicate"),
        "value": _row_text(claim, "value"),
        "status": status,
        "authority": _row_text(claim, "authority", str(edge["authority"])),
        "confidence": float(_field(claim, "confidence", edge["confidence"]) or 0.0),
        "chain": int(chain),
        "hop": int(hop),
    }
    updated_at = _field(claim, "updated_at")
    if updated_at is not None:
        row["updated_at"] = str(updated_at)
    if previous is not None:
        row["bridge_from"] = f"{previous['subject']} / {previous['predicate']}"
    if status == "superseded":
        superseded_at = _field(claim, "valid_until", edge.get("valid_until"))
        if superseded_at is not None:
            row["superseded_at"] = str(superseded_at)
    if bool(_field(claim, "retracted", False)):
        row["retracted"] = True
    if weakest:
        row["weakest"] = True
    if chain_authority:
        row["chain_authority"] = str(chain_authority)
    if incomplete:
        row["incomplete"] = True
    if match:
        row["match"] = "subject"
    return row


def _overflow_notes(
    entries: Sequence[Mapping[str, Any]],
    sibling_notes: Sequence[Mapping[str, Any]],
    screen: Callable[[str], tuple[bool, str | None]],
) -> list[dict[str, Any]]:
    """At most ``OVERFLOW_NOTE_CAP`` notes in total, at any depth; the hubs a
    walk could not read come first.  Further overflows stay counted in
    ``report.overflow`` and the chains they truncated stay marked."""
    emitted: list[dict[str, Any]] = []
    for entry in entries:
        if len(emitted) >= OVERFLOW_NOTE_CAP:
            break
        label = str(entry.get("label") or entry.get("entity_key") or "")
        if not label or screen(label)[0]:
            continue
        emitted.append({
            "subject": label, "predicate": "", "value": "", "status": "overflow",
            "hop": int(entry["hop"]), "note": _hub_note(entry),
        })
    for note in sibling_notes:
        if len(emitted) >= OVERFLOW_NOTE_CAP:
            break
        subject = str(note.get("subject") or "")
        if subject and screen(subject)[0]:
            continue
        emitted.append({
            "subject": subject, "predicate": "", "value": "", "status": "overflow",
            "hop": int(note["hop"]), "note": str(note["note"]),
        })
    return emitted


def _finish(result: dict[str, Any], started: float | None) -> dict[str, Any]:
    if started is not None:
        result["report"]["elapsed_ms"] = round(
            (time.monotonic() - float(started)) * 1000.0, 3
        )
    return result


# --- verify and rebuild (design §4.6) ----------------------------------------

def verify_graph(db: sqlite3.Connection) -> dict[str, Any]:
    """Check the projection against the live claim rows; repair nothing.

    ``{"ok", "ready", "edges", "entities", "edges_expected",
    "entities_expected", "excluded", "problems"}`` where every problem is
    ``{"claim_id"|"entity_id", "kind", "detail", "repair"}``, ``kind`` is one
    of ``VERIFY_PROBLEM_KINDS`` and ``detail`` names fields, never values.

    "Every non-excluded claim has exactly one edge" is exact because the three
    categories of ``claim_exclusion`` are the complete definition of excluded,
    so a claim with no edge is either in a category or a ``missing_edge``
    problem — never unexplained.
    """
    excluded = _empty_exclusions()
    if not graph_ready(db):
        return {
            "ok": False, "ready": False, "edges": 0, "entities": 0,
            "edges_expected": 0, "entities_expected": 0, "excluded": excluded,
            "problems": [],
        }
    problems: list[dict[str, Any]] = []

    def problem(kind: str, detail: str, **identity: Any) -> None:
        problems.append({"kind": kind, "detail": detail, "repair": _REPAIR_OF[kind],
                         "claim_id": None, "entity_id": None, **identity})

    entities = {
        int(row["id"]): row
        for row in db.execute(
            "SELECT id, scope, entity_key, label FROM memory_graph_entities"
        ).fetchall()
    }
    edges = {
        int(row["claim_id"]): row
        for row in db.execute(
            "SELECT claim_id, scope, claim_key, src_entity_id, predicate_key, "
            "dst_entity_id, value_kind, status, authority, confidence, valid_from, "
            "valid_until, spine_event_id FROM memory_graph_edges"
        ).fetchall()
    }
    referenced: set[int] = set()
    expected_entities: set[tuple[str, str]] = set()
    edges_expected = 0
    # Call-local memos over two pure functions; a real store repeats the same
    # subject and the same value thousands of times.
    exclusion_memo: dict[tuple[str, str], str | None] = {}
    admission_memo: dict[str, str] = {}
    for claim in db.execute(
        f"SELECT {', '.join(CLAIM_ROW_COLUMNS)} FROM memory_claims ORDER BY id"
    ).fetchall():
        claim_id = int(claim["id"])
        subject = _text(claim, "subject")
        value = _text(claim, "value")
        scope = _text(claim, "scope") or "global"
        pair = (subject, _text(claim, "predicate"))
        if pair not in exclusion_memo:
            exclusion_memo[pair] = claim_exclusion(*pair)
        category = exclusion_memo[pair]
        if category is not None:
            excluded[category] += 1
            if claim_id in edges:
                problem(
                    "screen" if category == "subject_private" else "extra_edge",
                    f"excluded claim ({category}) has an edge", claim_id=claim_id,
                )
            continue
        edges_expected += 1
        subject_key = entity_key(subject)
        expected_entities.add((scope, subject_key))
        if value not in admission_memo:
            admission_memo[value] = value_admission(value)
        kind = admission_memo[value]
        value_key = entity_key(value) if kind == "entity" else None
        if value_key is not None:
            expected_entities.add((scope, value_key))
        edge = edges.get(claim_id)
        if edge is None:
            problem("missing_edge", "claim has no edge", claim_id=claim_id)
            continue
        if str(edge["value_kind"]) != kind:
            problem(
                "screen" if screen_endpoint(value)[0] else "field",
                "value_kind: differs", claim_id=claim_id,
            )
        for name, expected in (
            ("scope", scope),
            ("claim_key", _text(claim, "claim_key")),
            ("predicate_key", entity_key(_text(claim, "predicate"))),
            ("status", _text(claim, "status")),
            ("authority", _text(claim, "authority")),
            ("valid_from", _text(claim, "valid_from")),
        ):
            if str(edge[name]) != expected:
                problem("field", f"{name}: differs", claim_id=claim_id)
        if (edge["valid_until"] or None) != (_field(claim, "valid_until") or None):
            problem("field", "valid_until: differs", claim_id=claim_id)
        if abs(float(edge["confidence"]) - float(_field(claim, "confidence") or 0.0)) > 1e-9:
            problem("field", "confidence: differs", claim_id=claim_id)
        if int(edge["spine_event_id"] or 0) != int(_field(claim, "spine_event_id") or 0):
            problem("field", "spine_event_id: differs", claim_id=claim_id)
        for column, expected_key in (
            ("src_entity_id", subject_key), ("dst_entity_id", value_key),
        ):
            entity_id = edge[column]
            if expected_key is None:
                if entity_id is not None:
                    problem("entity_key", f"{column}: unexpected entity", claim_id=claim_id)
                continue
            if entity_id is None:
                problem("entity_key", f"{column}: missing entity", claim_id=claim_id)
                continue
            referenced.add(int(entity_id))
            node = entities.get(int(entity_id))
            if node is None:
                problem("entity_key", f"{column}: entity is absent", claim_id=claim_id)
            elif str(node["entity_key"]) != expected_key or str(node["scope"]) != scope:
                problem("entity_key", f"{column}: differs", claim_id=claim_id)
    for claim_id in sorted(set(edges) - _live_claim_ids(db)):
        problem("extra_edge", "edge has no claim", claim_id=claim_id)
    for entity_id, node in sorted(entities.items()):
        if entity_id not in referenced:
            problem("orphan_entity", "entity has no edge", entity_id=entity_id)
        label = str(node["label"])
        if (
            entity_key(label) != str(node["entity_key"])
            or len(label) > ENTITY_LABEL_MAX_CHARS
            or screen_endpoint(label)[0]
        ):
            problem("label", "label: differs", entity_id=entity_id)
    row = db.execute(
        "SELECT next_id FROM memory_graph_entity_sequence WHERE id=1"
    ).fetchone()
    if row is None or int(row[0]) <= entity_sequence_floor(db):
        problem("sequence", "entity sequence is behind the store")
    return {
        "ok": not problems,
        "ready": True,
        "edges": len(edges),
        "entities": len(entities),
        "edges_expected": edges_expected,
        "entities_expected": len(expected_entities),
        "excluded": excluded,
        "problems": problems,
    }


_REPAIR_OF: dict[str, str] = {
    "missing_edge": "project", "field": "project", "entity_key": "project",
    "screen": "project", "extra_edge": "delete", "orphan_entity": "sweep",
    "sequence": "sequence", "label": "label",
}


def _live_claim_ids(db: sqlite3.Connection) -> set[int]:
    return {
        int(row[0]) for row in db.execute("SELECT id FROM memory_claims").fetchall()
    }


def rebuild_graph_projection(db: sqlite3.Connection) -> dict[str, Any]:
    """The dry run: ``verify_graph`` shaped as an equivalence report.

    One engine, so a divergence the dry run reports is exactly the one
    ``reproject`` repairs.  ``label`` and ``created_at`` are display-only and
    are not compared, and an entity's ``id`` is not compared either: ids are
    allocated and never reused, and a rebuild must not renumber a survivor.
    """
    report = verify_graph(db)
    return {
        "ok": report["ok"],
        "ready": report["ready"],
        "edges_live": report["edges"],
        "edges_expected": report["edges_expected"],
        "entities_live": report["entities"],
        "entities_expected": report["entities_expected"],
        "excluded": report["excluded"],
        "divergences": report["problems"],
    }


def divergence_signature(report: Mapping[str, Any]) -> list[tuple[str, str, str]]:
    """The plan token's material: ``(claim_id, entity_id, kind)``.

    ``memory_spine._divergence_signature`` keys on ``(claim_id, kind)`` alone,
    which would collapse every entity-side problem into one entry and leave a
    stale plan over them undetectable; this is the same mechanism, one field
    wider.
    """
    items = report.get("divergences")
    if items is None:
        items = report.get("problems") or []
    return sorted({
        (str(item.get("claim_id")), str(item.get("entity_id")), str(item.get("kind")))
        for item in items
    })


def reproject(db: sqlite3.Connection, *, now: str) -> dict[str, Any]:
    """Reconcile the graph with the live claim rows, in place, inside the
    caller's write transaction; append no receipt of its own.

    Only divergent rows are touched: extra edges are deleted, missing and
    field-divergent ones re-projected, orphans swept, a drifted label reset to
    its key, the sequence advanced.  The dry run is re-run afterwards and
    ``residual_divergence`` is raised when anything survives, so the caller
    rolls back rather than committing a projection it cannot explain.
    """
    before = verify_graph(db)
    if not before["ready"]:
        raise GraphError("the memory graph is missing", code="graph_missing")
    to_delete: set[int] = set()
    to_project: set[int] = set()
    to_sweep: set[int] = set()
    to_relabel: set[int] = set()
    advance_sequence = False
    for item in before["problems"]:
        repair = str(item.get("repair"))
        if repair == "delete" and item.get("claim_id") is not None:
            to_delete.add(int(item["claim_id"]))
        elif repair == "project" and item.get("claim_id") is not None:
            to_project.add(int(item["claim_id"]))
        elif repair == "sweep" and item.get("entity_id") is not None:
            to_sweep.add(int(item["entity_id"]))
        elif repair == "label" and item.get("entity_id") is not None:
            to_relabel.add(int(item["entity_id"]))
        elif repair == "sequence":
            advance_sequence = True
    to_project -= to_delete
    try:
        if advance_sequence:
            db.execute(
                "INSERT INTO memory_graph_entity_sequence(id, next_id) VALUES (1, ?) "
                "ON CONFLICT(id) DO UPDATE SET next_id=excluded.next_id",
                (entity_sequence_floor(db) + 1,),
            )
        removed_entity_ids = list(delete_edges(db, sorted(to_delete)))
        for entity_id in sorted(to_relabel):
            db.execute(
                "UPDATE memory_graph_entities SET label=entity_key WHERE id=?",
                (int(entity_id),),
            )
        cache = projection_cache(batch=True)
        for claim_id in sorted(to_project):
            claim = db.execute(
                f"SELECT {', '.join(CLAIM_ROW_COLUMNS)} FROM memory_claims WHERE id=?",
                (int(claim_id),),
            ).fetchone()
            if claim is None:
                continue
            project_claim(db, claim, now=now, cache=cache)
        flush_projection(db, cache)
        # A FULL sweep, not the pre-repair list: re-projecting a claim onto a
        # different entity strands the one it used to point at, and that
        # entity is not in any problem the dry run reported.  Sweeping only
        # the reported orphans left it behind, the residual verify saw it, and
        # the whole apply rolled back for ever — including every
        # rebuild-claims --apply that reaches this through post_apply.
        _ = to_sweep
        swept = sweep_all_orphan_entities(db)
    except sqlite3.IntegrityError as exc:
        raise GraphError(
            f"write_conflict: the graph projection could not be written ({exc})",
            code="write_conflict",
        ) from exc
    removed_entity_ids = sorted({*removed_entity_ids, *swept})
    after = verify_graph(db)
    if after["problems"]:
        raise GraphError(
            "residual_divergence: the graph still diverges after apply; rolled back",
            code="residual_divergence",
        )
    return {
        "edges_before": int(before["edges"]),
        "edges_after": int(after["edges"]),
        "entities": int(after["entities"]),
        "divergences_fixed": len(before["problems"]),
        "removed_ids": sorted(to_delete),
        "removed_entity_ids": removed_entity_ids,
        "excluded": after["excluded"],
    }


def apply_graph_projection(
    db: sqlite3.Connection,
    key: bytes,
    plan: Mapping[str, Any] | None = None,
    *,
    now: str,
    actor: str = "operator",
    permission: str = "operator:cli",
    source: str = "graph rebuild --apply",
) -> dict[str, Any]:
    """``reproject`` plus the ``projection.rebuilt {projection:"graph"}``
    receipt, inside the caller's write transaction.

    Refusals raise ``GraphError`` with a fixed code and change nothing:
    ``not_in_transaction``, ``graph_missing``, ``stale_plan`` (the store moved
    since the dry run the operator confirmed), ``write_conflict``,
    ``residual_divergence``.
    """
    if not db.in_transaction:
        raise GraphError(
            "not_in_transaction: apply runs inside the caller's write transaction",
            code="not_in_transaction",
        )
    before = rebuild_graph_projection(db)
    if not before["ready"]:
        raise GraphError("the memory graph is missing", code="graph_missing")
    if plan is not None and divergence_signature(plan) != divergence_signature(before):
        raise GraphError(
            "stale_plan: the store changed since the dry run; run graph rebuild again",
            code="stale_plan",
        )
    result = reproject(db, now=now)
    event_id = memory_spine.append_event(
        db, key,
        kind="projection.rebuilt", actor=actor, source=source, scope="global",
        permission=permission, outcome="applied", subject_kind="projection",
        payload={
            "at": str(now),
            "projection": "graph",
            "rows_before": int(result["edges_before"]),
            "rows_after": int(result["edges_after"]),
            "divergences_fixed": int(result["divergences_fixed"]),
            "removed_ids": list(result["removed_ids"]),
            "removed_entity_ids": list(result["removed_entity_ids"]),
            "entities": int(result["entities"]),
            "excluded": dict(result["excluded"]),
        },
        now=now,
    )
    return {"ok": True, "event_id": int(event_id), **result}
