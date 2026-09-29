from __future__ import annotations  # noqa: F401 - compatibility facade

import copy  # noqa: F401 - compatibility facade
import functools  # noqa: F401 - compatibility facade
import hashlib  # noqa: F401 - compatibility facade
import hmac  # noqa: F401 - compatibility facade
import json  # noqa: F401 - compatibility facade
import math  # noqa: F401 - compatibility facade
import os  # noqa: F401 - compatibility facade
import re  # noqa: F401 - compatibility facade
import secrets  # noqa: F401 - compatibility facade
import sqlite3  # noqa: F401 - compatibility facade
from decimal import Decimal  # noqa: F401 - compatibility facade
import struct  # noqa: F401 - compatibility facade
import time  # noqa: F401 - compatibility facade
from collections import Counter  # noqa: F401 - compatibility facade
from collections.abc import Mapping, Sequence  # noqa: F401 - compatibility facade
from contextlib import contextmanager  # noqa: F401 - compatibility facade
from datetime import datetime, timedelta, timezone  # noqa: F401 - compatibility facade
from pathlib import Path  # noqa: F401 - compatibility facade
from pathlib import PurePosixPath  # noqa: F401 - compatibility facade
from typing import Any, Iterator  # noqa: F401 - compatibility facade
from uuid import uuid4  # noqa: F401 - compatibility facade

from .claim_clock import (  # noqa: F401 - compatibility facade
    DEFAULT_HAZARD_PER_DAY,
    MIN_HAZARD_PAIRS,
    age_days as claim_age_days,
    effective_confidence as claim_effective_confidence,
    estimate_hazard as estimate_claim_hazard,
    protected_predicate,
    source_key as claim_source_key,
)
from . import learning_ladder  # noqa: F401 - compatibility facade
from . import memory_bridge  # noqa: F401 - compatibility facade
from . import memory_compaction  # noqa: F401 - compatibility facade
from . import memory_graph  # noqa: F401 - compatibility facade
from . import memory_spine  # noqa: F401 - compatibility facade
from .governed_memory import (  # noqa: F401 - compatibility facade
    GovernedMemoryCommandError,
    parse_explicit_project_fact,
    parse_explicit_project_fact_erasure,
    parse_explicit_project_fact_retraction,
    project_claim_scope,
)
from .learning_memory_quality import (  # noqa: F401 - compatibility facade
    TRAINING_QUALITY_CONTRACT_VERSION,
    learning_memory_record_allowed,
)
from .memory_retrieval import (  # noqa: F401 - compatibility facade
    _ACTIVE_RECALL_CACHE,
    MAX_MEMORY_QUERY_TERMS,  # noqa: F401 - compatibility facade
    MAX_MEMORY_SEARCH_CANDIDATES,
    _MAX_MEMORY_QUERY_TERM_CANDIDATES,
    _memory_candidate_terms,
    _memory_evidence_terms,
    _memory_fts_group_query,
    _memory_fts_literal,
    _memory_fts_query,
    _memory_fts_term_groups,
    _memory_identity_capable_term,
    _memory_identity_conflict,
    _memory_identity_scope,
    _memory_like_terms,
    _memory_query_targets_authority_evasion,
    _memory_query_terms,
    _memory_resolve_sibling_identities,
    _memory_term_variants,
    _memory_tokens,
    _normalize_memory_token,
    _rank_memory_rows,
    _structured_memory_identifier,
    RecallCache,
)
from . import skill_library  # noqa: F401 - compatibility facade
from .redaction import (  # noqa: F401 - compatibility facade
    contains_explicit_sensitive_key_phrase,
    contains_private_identifier,
    contains_sensitive_key_phrase,
    contains_secret,
    is_redacted_descriptor,
    is_sensitive_key,
    redact_private_identifiers,
    redact_secrets,
    screen_endpoint,
)
from .reliability_lab import validate_failure_case  # noqa: F401 - compatibility facade
from .run_observability import aggregate_run_metrics, sanitize_run_metrics  # noqa: F401 - compatibility facade
from .specialists import (  # noqa: F401 - compatibility facade
    SPECIALISTS,
    SPECIALIST_BY_KEY,
    specialist_for_consultation_prompt,
    specialist_for_family,
    specialist_for_prompt,
    specialist_for_scheduled_prompt,
)
from .strategy_transfer import (  # noqa: F401 - compatibility facade
    STRATEGY_SET,
    StrategyTransferError,
    strategies_from_evidence,
)
from .strategy_transfer_trial import (  # noqa: F401 - compatibility facade
    TRIAL_ABORT_REASONS,
    TRIAL_ARMS,
    TRIAL_ASSIGNMENT_SCHEMA,
    TRIAL_ASSIGNMENT_STATUSES,
    TRIAL_BLOCK_SIZE,
    TRIAL_CONTAMINATION_REASONS,
    TRIAL_MANIFEST_STATUSES,
    TRIAL_MAX_DAYS,
    TRIAL_PROMPT_RECEIPT_SCHEMA,
    TRIAL_SCHEMA,
    StrategyTransferTrialError,
    arm_for_slot,
    family_caps,
    sha256_json,
    strategy_transfer_runtime_sha256,
    validated_seed,
    validated_sha256,
)
from .vault import Vault, VaultNote  # noqa: F401 - compatibility facade


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _recall_timestamp_valid(value: Any) -> bool:
    """Accept only privacy-clean, timezone-aware ISO timestamps at read boundaries."""
    text = str(value or "")
    if not text or contains_secret(text) or contains_private_identifier(text):
        return False
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


def _ordinary_memory_provenance_material_from_fields(
    memory_id: int,
    created_at: str,
    kind: str,
    content: str,
    source: str | None,
    *,
    origin: str,
    eligible: bool,
) -> dict[str, Any]:
    """Build the canonical ordinary-memory receipt without database access."""
    return {
        "schema": "jarvis.ordinary-memory-provenance.v1",
        "memory": {
            "id": int(memory_id),
            "created_at": str(created_at),
            "kind": str(kind),
            "content": str(content),
            "source": None if source is None else str(source),
        },
        "authorization": {
            "origin": str(origin),
            "eligible": bool(eligible),
        },
    }


def _ordinary_memory_provenance_digest_from_material(
    material: dict[str, Any],
) -> str:
    canonical = json.dumps(
        material,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _sqlite_sha256_text(value: Any) -> str:
    """Deterministic SQLite helper for exact text-to-digest joins."""
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def _sqlite_ordinary_memory_provenance_sha256(
    memory_id: Any,
    created_at: Any,
    kind: Any,
    content: Any,
    source: Any,
    origin: Any,
    eligible: Any,
) -> str:
    """Recompute an ordinary-memory receipt inside bounded recall SQL."""
    try:
        material = _ordinary_memory_provenance_material_from_fields(
            int(memory_id),
            str(created_at),
            str(kind),
            str(content),
            None if source is None else str(source),
            origin=str(origin),
            eligible=bool(int(eligible)),
        )
    except (TypeError, ValueError, OverflowError):
        return ""
    return _ordinary_memory_provenance_digest_from_material(material)


def training_prompt_split(prompt: str, task_kind: str) -> str:
    """Keep every response for one normalized task prompt in the same data split."""
    split_key = json.dumps(
        {
            "prompt": " ".join(str(prompt).casefold().split()),
            "task_kind": str(task_kind).strip().casefold(),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    bucket = int(hashlib.sha256(split_key.encode("utf-8")).hexdigest()[:8], 16) % 100
    return "train" if bucket < 80 else "validation" if bucket < 90 else "test"


SCHEMA_VERSION = 54

_ORDINARY_MEMORY_PROVENANCE_ORIGINS = frozenset({
    "explicit_operator_memory",
    "explicit_user_feedback",
    "verified_learning",
    "verified_vault_note",
    "verified_import",
})


def _learning_quality_assessment(
    memory_id: Any,
    created_at: Any,
    kind: Any,
    content: Any,
    source: Any,
    origin: Any,
    eligible: Any,
    content_sha256: Any,
    provenance_sha256: Any,
) -> bool | None:
    """Return ALLOW/DENY for an authenticated clean learning row, else UNKNOWN."""
    try:
        if str(kind).casefold() != "learning":
            return None
        normalized_eligible = bool(int(eligible))
        normalized_origin = str(origin)
        if (
            normalized_eligible
            and normalized_origin not in _ORDINARY_MEMORY_PROVENANCE_ORIGINS
        ) or (not normalized_eligible and normalized_origin != "unverified"):
            return None
        normalized_content = str(content)
        if str(content_sha256) != hashlib.sha256(
            normalized_content.encode("utf-8")
        ).hexdigest():
            return None
        normalized_source = None if source is None else str(source)
        material = _ordinary_memory_provenance_material_from_fields(
            int(memory_id),
            str(created_at),
            str(kind),
            normalized_content,
            normalized_source,
            origin=normalized_origin,
            eligible=normalized_eligible,
        )
        if str(provenance_sha256) != (
            _ordinary_memory_provenance_digest_from_material(material)
        ):
            return None
        recall_material = "\n".join((
            normalized_content,
            "" if normalized_source is None else normalized_source,
        ))
        if (
            contains_secret(recall_material)
            or contains_private_identifier(recall_material)
        ):
            return None
        return bool(
            normalized_eligible
            and learning_memory_record_allowed(
                content=normalized_content,
                source="" if normalized_source is None else normalized_source,
            )
        )
    except (TypeError, ValueError, OverflowError):
        return None


def _learning_quality_quarantined_record(
    memory_id: Any,
    created_at: Any,
    kind: Any,
    content: Any,
    source: Any,
    origin: Any,
    eligible: Any,
    content_sha256: Any,
    provenance_sha256: Any,
) -> int:
    """Compatibility predicate used by the v43-to-v44 migration."""
    decision = _learning_quality_assessment(
        memory_id,
        created_at,
        kind,
        content,
        source,
        origin,
        eligible,
        content_sha256,
        provenance_sha256,
    )
    return int(decision is False)


_LEARNING_QUALITY_ASSESSMENT_JOIN_SQL = f"""
LEFT JOIN ordinary_memory_quality_assessments AS omqa
  ON omqa.memory_id=m.id
 AND lower(m.kind)='learning'
 AND omqa.contract_version={TRAINING_QUALITY_CONTRACT_VERSION}
 AND omqa.content_sha256=omp.content_sha256
 AND omqa.provenance_sha256=omp.provenance_sha256
 AND omqa.source_is_null=(m.source IS NULL)
 AND omp.content_sha256=jarvis_sha256(m.content)
 AND omqa.source_sha256=jarvis_sha256(COALESCE(m.source, ''))
 AND omp.provenance_sha256=jarvis_ordinary_memory_provenance_sha256(
       m.id, m.created_at, m.kind, m.content, m.source,
       omp.origin, omp.eligible
 )
"""
_LEARNING_QUALITY_SELECT_SQL = """
omqa.recall_allowed AS ordinary_quality_allowed,
omqa.contract_version AS ordinary_quality_contract_version,
omqa.content_sha256 AS ordinary_quality_content_sha256,
omqa.source_is_null AS ordinary_quality_source_is_null,
omqa.source_sha256 AS ordinary_quality_source_sha256,
omqa.provenance_sha256 AS ordinary_quality_provenance_sha256
"""
_LEARNING_QUALITY_LEXICAL_SQL = """
AND (
    lower(m.kind)<>'learning'
    OR omqa.recall_allowed IS NULL
    OR omqa.recall_allowed=1
)
"""
_LEARNING_QUALITY_ALLOWED_SQL = """
AND (lower(m.kind)<>'learning' OR omqa.recall_allowed=1)
"""

LESSON_DEFAULT_TTL_DAYS = 180
LESSON_REUSABLE_PREDICTION_ORIGINS = frozenset({
    "interactive", "worker", "proactive",
})
LESSON_EVIDENCE_REQUIRED_FAMILIES = frozenset({
    "code_build", "code_fix", "code_refactor", "code_test", "deep_research",
    "learning_brief", "file_ops", "desktop_file_ops", "external_publish",
    "security_analysis",
})
STRATEGY_TRANSFER_APPLICATION_MODES = frozenset({"observe", "trial", "advise"})
STRATEGY_TRANSFER_ATTESTATION_KINDS = frozenset({"sealed_benchmark", "applied_ab"})
STRATEGY_TRANSFER_ACTIVATION_THRESHOLDS = {
    "minimum_control_predictions": 20,
    "minimum_applied_predictions": 20,
    "minimum_source_target_pairs": 3,
    "minimum_applied_success_rate": 0.70,
    "minimum_lift_pp": 15.0,
    "maximum_invalid_receipts": 0,
    "maximum_harm_quarantines": 0,
}


_PERSISTENT_READ_APPROVAL_TOOLS = frozenset({
    "computer_list_files",
    "computer_read_file",
    "computer_search_files",
    "computer_storage_report",
})
_PAIRING_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
_PAIRING_PBKDF2_ROUNDS = 150_000
# Claim-clock read telemetry is best-effort: a foreground read must never wait
# the full busy timeout on a concurrent writer just to bump a counter.
_CLAIM_CLOCK_WRITE_TIMEOUT_MS = 250
TERMINAL_TASK_STATUSES = frozenset({"done", "failed", "cancelled"})
MAX_WORKER_ID_CHARS = 500
MAX_SEARCH_QUERY_CHARS = 5_000
# A question names a handful of subjects.  Every start beyond that is another
# traversal root and, on the non-exact path, another store-wide look-alike
# comparison, so the graph channel bounds them and reports the remainder as
# ``subjects_dropped`` rather than letting a crafted question widen the read.
MAX_GRAPH_START_SUBJECTS = 3
# Claims-lane modes that silence the graph channel outright (design 5.6.1,
# 10.7 item 5).  Every other mode -- including the identity floors -- leaves
# the graph to answer on its own rules.
_LANE_SILENCING_MODES = frozenset({
    "screened", "project-unavailable", "corrupt-strongest", "error",
})
MAX_TASK_RESULT_CHARS = 100_000
MAX_TASK_ERROR_CHARS = 10_000
MAX_QUERY_EMBEDDING_CACHE = 2_048


class ModelBudgetExceeded(RuntimeError):
    """Raised before a provider call would exceed one request-lineage budget."""

CLAIM_AUTHORITIES = frozenset({"external", "learned", "verified", "operator"})
_CLAIM_AUTHORITY_WEIGHT = {
    "external": 10,
    "learned": 30,
    "verified": 70,
    "operator": 100,
}
_CLAIM_QUERY_METADATA_TERMS = frozenset({
    "according", "claim", "conflict", "conflicting", "current", "currently",
    "fact", "give", "information", "known", "notice", "operator", "preference",
    "plus", "present", "record", "recorded", "reported", "revised", "setting",
    "store", "stored", "user", "value",
})
_CLAIM_DERIVATIONAL_SUFFIXES = (
    "ations", "ation", "ators", "ator", "ating", "ated", "ates", "ate",
    "ments", "ment", "ions", "ion", "ors", "or", "ers", "er", "ing", "ed",
    "age", "e",
)
_CLAIM_COMPOUND_PREFIXES = frozenset({
    "anti", "counter", "inter", "intra", "macro", "micro", "multi",
    "non", "over", "post", "pre", "re", "sub", "super", "under",
})
_CLAIM_IDENTITY_DESCRIPTOR_TERMS = frozenset({
    "account", "contact", "identity", "operator", "owner", "person",
    "profile", "user",
})
_MAX_SUPERSEDED_CLAIM_VERSIONS = 64
_CLAIM_RECALL_BATCH_SIZE = 400
_MAX_CLAIM_QUERY_TERMS = 32
_LESSON_QUERY_METADATA_TERMS = frozenset({
    "apply", "complete", "completed", "completion", "family", "lesson",
    "project", "rule", "task",
})
_ORDINARY_MEMORY_IDENTITY_METADATA_TERMS = frozenset({
    "fact", "knowledge", "learn", "learned", "memory", "note", "pull",
    "record", "saved", "stored",
})
_LESSON_IDENTITY_METADATA_TERMS = frozenset({
    *_LESSON_QUERY_METADATA_TERMS,
    "learn", "learned", "reuse", "reused", "reusing",
})


def _claim_cache_key(namespace: str, terms: Sequence[str]) -> tuple[str, bytes]:
    """Return a boundary-safe digest key without retaining structured fields."""
    digest = hashlib.blake2b(digest_size=16, person=b"jarvis-claim-v1")
    for term in sorted(str(item) for item in terms):
        encoded = term.encode("utf-8")
        digest.update(len(encoded).to_bytes(4, "big"))
        digest.update(encoded)
    return str(namespace), digest.digest()


def _ordinary_cache_field_digest(value: Any) -> bytes:
    """Digest one untrusted eligibility field before it enters a cache key."""
    return hashlib.blake2b(
        str(value).encode("utf-8"),
        digest_size=16,
        person=b"jarvis-ord-v1",
    ).digest()


def _claim_ordered_cache_key(
    namespace: str,
    parts: Sequence[Any],
) -> tuple[str, bytes]:
    """Digest an ordered structured record without retaining any raw field."""
    digest = hashlib.blake2b(digest_size=16, person=b"jarvis-claim-v1")
    for part in parts:
        encoded = repr(part).encode("utf-8")
        digest.update(len(encoded).to_bytes(4, "big"))
        digest.update(encoded)
    return str(namespace), digest.digest()


def _claim_term_root(term: str, *, cache_allowed: bool = True) -> str:
    """Return a conservative derivational root for claim-field matching.

    Memoized in the active store's ``RecallCache`` (cleared on close or
    deletion); a plain pure function outside a recall call.
    """
    cache = _ACTIVE_RECALL_CACHE.get() if cache_allowed else None
    if cache is None:
        return _claim_term_root_uncached(term)
    key = _claim_cache_key("claim-root", (str(term),))
    root = cache.get(key)
    if root is None:
        root = _claim_term_root_uncached(term)
        cache.put(key, root, len(str(term)) + len(root))
    return root


def _claim_term_root_uncached(term: str) -> str:
    """Return a conservative derivational root for claim-field matching.

    Both sides of a comparison receive the same one-suffix normalization.  A
    doubled trailing consonant is collapsed and a trailing ``e`` is removed so
    common verb/noun families meet at one exact root without enabling substring
    matching (for example, inspect/inspection and plan/planning).
    """
    normalized = str(term).casefold()
    if len(normalized) < 6:
        return normalized
    for suffix in _CLAIM_DERIVATIONAL_SUFFIXES:
        if normalized.endswith(suffix):
            root = normalized[:-len(suffix)]
            # Bare ``-ion`` is highly collision-prone for four-letter stems
            # (miss/mission, pass/passion, vers/version, port/portion).  Keep
            # that family conservative while retaining longer, useful pairs
            # such as inspect/inspection.
            minimum_root_length = 5 if suffix in {"ion", "ions"} else 4
            if len(root) >= minimum_root_length:
                if root[-1] == root[-2] and root[-1] not in "aeiou":
                    root = root[:-1]
                if len(root) > 4 and root.endswith("e"):
                    root = root[:-1]
                return root
    if len(normalized) > 4 and normalized.endswith("e"):
        return normalized[:-1]
    return normalized


def _claim_matched_query_terms(
    query_terms: set[str],
    record_terms: set[str],
    *,
    cache_allowed: bool = True,
) -> set[str]:
    """Match bounded inflections/compounds without treating substrings as facts.

    Each query term is decided independently by three rules: an exact record
    term; a record term sharing its derivational root where at least one side
    is not already the bare root; or a bounded compound-prefix relation whose
    shorter side has at least five characters.  The rules are evaluated with
    set lookups, so the cost no longer grows with the number of record terms.
    ``tests/test_memory_scale_recall.py`` pins equality with the original
    pairwise loop on random vocabularies.
    """
    matches: set[str] = set()
    record_set = frozenset(record_terms)
    if not record_set:
        return matches
    # One claim's field tokens are matched many times per query and again on
    # later turns, so the root summary of a record-term set is memoized in
    # the active store's RecallCache alongside the per-term roots.
    cache = _ACTIVE_RECALL_CACHE.get() if cache_allowed else None
    record_key = _claim_cache_key("claim-record-roots", tuple(record_set))
    summary = cache.get(record_key) if cache is not None else None
    if summary is None:
        roots_present: set[str] = set()
        roots_with_inflected_term: set[str] = set()
        for record_term in record_set:
            record_root = _claim_term_root(
                record_term, cache_allowed=cache_allowed
            )
            roots_present.add(record_root)
            if record_root != record_term:
                roots_with_inflected_term.add(record_root)
        summary = (frozenset(roots_present), frozenset(roots_with_inflected_term))
        if cache is not None:
            cache.put(
                record_key,
                summary,
                sum(len(term) for term in record_set)
                + sum(len(root) for root in roots_present),
            )
    roots_present, roots_with_inflected_term = summary
    query_key = _claim_cache_key("claim-query-shapes", tuple(query_terms))
    shapes = cache.get(query_key) if cache is not None else None
    if shapes is None:
        shapes = tuple(
            (
                query_term,
                *_claim_query_term_shape(
                    query_term, cache_allowed=cache_allowed
                ),
            )
            for query_term in query_terms
        )
        if cache is not None:
            cache.put(query_key, shapes, sum(len(term) * 4 for term in query_terms))
    for query_term, query_root, prefixed_forms, bare_forms in shapes:
        if query_term in record_set:
            matches.add(query_term)
            continue
        if query_root in roots_present and (
            query_root != query_term or query_root in roots_with_inflected_term
        ):
            matches.add(query_term)
            continue
        if any(form in record_set for form in prefixed_forms) or any(
            form in record_set for form in bare_forms
        ):
            matches.add(query_term)
    return matches


def _claim_query_term_shape(
    query_term: str,
    *,
    cache_allowed: bool = True,
) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
    """Return the root and the bounded compound forms one query term can match.

    ``prefixed_forms`` are the longer record spellings that would contain this
    term behind a compound prefix; ``bare_forms`` are the shorter record
    spellings this term would contain behind one.  Both honour the five
    character minimum on the shorter side.  Memoized in the active store's
    ``RecallCache``; pure outside a recall call.
    """
    cache = _ACTIVE_RECALL_CACHE.get() if cache_allowed else None
    key = _claim_cache_key("claim-shape", (str(query_term),))
    if cache is not None:
        shape = cache.get(key)
        if shape is not None:
            return shape
    shape = _claim_query_term_shape_uncached(query_term)
    if cache is not None:
        cache.put(
            key,
            shape,
            len(str(query_term))
            + sum(len(form) for group in shape[1:] for form in group),
        )
    return shape


def _claim_query_term_shape_uncached(
    query_term: str,
) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
    root = _claim_term_root(query_term)
    prefixed_forms = (
        tuple(prefix + query_term for prefix in sorted(_CLAIM_COMPOUND_PREFIXES))
        if len(query_term) >= 5
        else ()
    )
    bare_forms = tuple(
        query_term[len(prefix):]
        for prefix in sorted(_CLAIM_COMPOUND_PREFIXES)
        if query_term.startswith(prefix) and len(query_term) - len(prefix) >= 5
    )
    return root, prefixed_forms, bare_forms


def _claim_query_terms(query: str) -> list[str]:
    """Select at most 16 claim terms while preserving both identity boundaries."""
    all_terms = [
        term for term in _memory_tokens(query, meaningful_only=True)
        if term not in _CLAIM_QUERY_METADATA_TERMS
    ]
    all_terms = list(dict.fromkeys(all_terms))
    if len(all_terms) <= _MAX_CLAIM_QUERY_TERMS:
        return all_terms
    boundary_indices = {0, len(all_terms) - 1}
    selected_indices = sorted(
        boundary_indices
        | set(sorted(
            (index for index in range(1, len(all_terms) - 1)),
            key=lambda index: (
                any(character.isdigit() for character in all_terms[index]),
                min(len(all_terms[index]), 16),
                -index,
            ),
            reverse=True,
        )[:_MAX_CLAIM_QUERY_TERMS - 2])
    )
    return [all_terms[index] for index in selected_indices]


def _validated_claim_scope(scope: str) -> str:
    """Validate the private storage-layer global/project scope contract."""
    normalized = str(scope).strip().casefold()
    if normalized == "global":
        return normalized
    match = re.fullmatch(r"project:([1-9][0-9]{0,18})", normalized)
    if match is None:
        raise ValueError("Claim scope must be global or project:<positive id>")
    project_id = int(match.group(1))
    if project_id > 9_223_372_036_854_775_807:
        raise ValueError("Claim project scope is out of range")
    return project_claim_scope(project_id)


_PROJECT_CLAIM_RECORD_PREFIX = "[jarvis project claim v1]"


def _claim_memory_content(
    subject: str,
    predicate: str,
    value: str,
    scope: str,
    record_stamp: str | None = None,
) -> str:
    """Keep legacy globals stable and frame project fields without ambiguity."""
    canonical = f"{subject} {predicate}: {value}"
    if scope == "global":
        return canonical
    if not record_stamp:
        raise ValueError("Project claim backing content requires a record timestamp")
    payload = {
        "created_at": str(record_stamp),
        "predicate": predicate,
        "schema": "jarvis.project-claim-memory.v1",
        "scope": scope,
        "subject": subject,
        "value": value,
    }
    return _PROJECT_CLAIM_RECORD_PREFIX + json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _claim_subject_identity_conflict(
    subject_head: str,
    query_terms: set[str],
) -> bool:
    """Detect a look-alike namespace token without requiring every subject word."""
    head = str(subject_head).casefold()
    if head in query_terms or len(head) < 5:
        return False
    for term in query_terms:
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
_EXPLICIT_USER_POSTAL_CODE = re.compile(
    r"(?:\bmy\s+zip(?:\s*code)?\s*(?:is|=|:)?\s*"
    r"|\bzip(?:\s*code)?\s+is\s+"
    r"|\bi\s+(?:live|reside|am\s+located)\s+(?:in|near)\s+"
    r"(?:zip(?:\s*code)?\s*)?)"
    r"([0-9]{5})(?:-[0-9]{4})?\b",
    re.I,
)

_AMBIGUOUS_LEARNING_REFERENCE = re.compile(
    r"\b(?:all|any|some)\s+of\s+(?:those|these|them)\b|"
    r"\b(?:do|add|install|remove|delete|upload|send|build|fix)\s+(?:it|that|those|these|them)\b",
    re.I,
)
_ACTION_LEARNING_REQUEST = re.compile(
    r"^\s*(?:(?:ok|okay|now|please|also)\b[, ]*)*"
    r"(?:i\s+(?:want|need)\s+you\s+to\s+|can\s+you\s+|go\s+(?:and\s+)?)?"
    r"(?:add|build|clean|convert|copy|create|delete|deploy|edit|export|generate|"
    r"include|install|launch|move|open|organize|publish|remove|rename|render|run|"
    r"send|test|update|upload|verify|write)\b",
    re.I,
)
_CONTEXTUAL_ACTION_LEARNING_REQUEST = re.compile(
    r"^\s*(?:(?:inside|within|in|using|with)\b[^.!?\r\n]{0,180}[,:]\s*)"
    r"(?:add|build|clean|convert|copy|create|delete|deploy|edit|export|generate|"
    r"include|install|launch|move|open|organize|publish|remove|rename|render|run|"
    r"send|test|update|upload|verify|write)\b",
    re.I,
)
_FOLLOWUP_ACTION_LEARNING_CLAUSE = re.compile(
    r"(?:^|[.!?;]\s+)(?:and\s+|then\s+)?(?:do\s+not\s+)?"
    r"(?:add|build|clean|convert|copy|create|delete|deploy|edit|export|generate|"
    r"include|install|launch|move|open|organize|publish|remove|rename|render|run|"
    r"send|test|update|upload|verify|write)\b",
    re.I,
)


def _validated_learning_topic(topic: Any) -> str:
    """Return a durable subject, rejecting commands and unresolved anaphora."""
    normalized = redact_secrets(" ".join(str(topic).strip().split()))
    if not normalized:
        raise ValueError("Learning topic must not be empty")
    if len(normalized) > 500:
        raise ValueError("Learning topic exceeds the 500 character limit")
    if (
        _AMBIGUOUS_LEARNING_REFERENCE.search(normalized)
        or _ACTION_LEARNING_REQUEST.search(normalized)
        or _CONTEXTUAL_ACTION_LEARNING_REQUEST.search(normalized)
        or _FOLLOWUP_ACTION_LEARNING_CLAUSE.search(normalized)
    ):
        raise ValueError(
            "Learning topic must be a self-contained subject, not an action or unresolved reference"
        )
    return normalized


def _bounded_persisted_text(value: Any, limit: int, label: str) -> str:
    text = str(value)
    if len(text) <= limit:
        return text
    suffix = f"\n...[{label} clipped before persistence]"
    return text[: max(0, limit - len(suffix))] + suffix


def backing_content_variants(
    subject: str,
    predicate: str,
    value: str,
    scope: str,
    created_at: str,
    claim_key: str,
) -> tuple[str, str]:
    """The only two contents a claim's backing ``memories`` row may hold.

    ``(canonical, keyed)``.  Two claim keys can render the same global
    backing content (``Kestrel relay / port / 9090`` and ``Kestrel / relay
    port / 9090``); the second write takes the keyed variant so it gets its
    own backing row instead of colliding on ``UNIQUE(memory_claims.
    memory_id)``.  Nothing is recorded anywhere about which variant was
    chosen: the writer, recall eligibility, the rebuild, and ``verify``
    all derive both from the claim row's own fields, so they cannot
    disagree (M3 design 6.3, review R1).

    The canonical part is truncated **first** and the fixed-length suffix
    appended afterwards, so ``_bounded_persisted_text``'s clip marker can
    never cut the suffix off.
    """
    raw = _claim_memory_content(subject, predicate, value, scope, created_at)
    suffix = f" [jarvis claim {str(claim_key)[:16]}]"
    canonical = _bounded_persisted_text(raw, 8_000, "temporal claim")
    keyed = _bounded_persisted_text(
        raw, 8_000 - len(suffix), "temporal claim"
    ) + suffix
    return canonical, keyed


def _redacted_json_value(value: Any) -> Any:
    """Recursively redact strings while preserving valid structured JSON."""
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for raw_key, item in value.items():
            key = redact_secrets(str(raw_key))
            sensitive_key = is_sensitive_key(str(raw_key))
            protected_descriptor = is_redacted_descriptor(item)
            cleaned[key] = (
                "[REDACTED]"
                if sensitive_key and not protected_descriptor
                else _redacted_json_value(item)
            )
        return cleaned
    if isinstance(value, (list, tuple, set)):
        return [_redacted_json_value(item) for item in value]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return redact_secrets(str(value))


def _redacted_json_text(value: Any, *, default: Any = str) -> str:
    return json.dumps(
        _redacted_json_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=default,
    )


def _validated_nonsecret_metadata(value: Any, label: str) -> str:
    text = str(value).strip()
    if contains_secret(text):
        raise ValueError(f"{label} must not contain a credential or secret")
    return text


def _claim_has_sensitive_key(subject: str, predicate: str) -> bool:
    """Detect credential field names split across structured claim keys."""
    subject_text = str(subject).strip()
    predicate_text = str(predicate).strip()
    combined = f"{subject_text} {predicate_text}".strip()
    return any(
        is_sensitive_key(candidate) or contains_sensitive_key_phrase(candidate)
        for candidate in (subject_text, predicate_text)
    ) or (
        is_sensitive_key(combined)
        or contains_explicit_sensitive_key_phrase(combined)
    )


def _validated_worker_id(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("Worker id must be a string")
    owner = value.strip()
    if not owner:
        raise ValueError("Worker id must not be empty")
    if len(owner) > MAX_WORKER_ID_CHARS:
        raise ValueError(f"Worker id exceeds {MAX_WORKER_ID_CHARS} characters")
    if any(ord(character) < 32 or ord(character) == 127 for character in owner):
        raise ValueError("Worker id contains control characters")
    if contains_secret(owner):
        raise ValueError("Worker id must not contain a credential or secret")
    return owner




def _as_utc(value: datetime | None = None) -> datetime:
    value = value or datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


_RECALL_REPORT_ABSTAINING_MODES = frozenset({
    "screened", "empty", "overflow", "error", "unknown-identity",
    "identity-unbound", "identity-overflow",
})


def _blank_recall_report(
    mode: str,
    *,
    candidate_limit: int = MAX_MEMORY_SEARCH_CANDIDATES,
    discovery_terms: int = 0,
) -> dict[str, Any]:
    """Fresh diagnostic record for one lexical recall attempt."""
    return {
        "channel": "lexical",
        "mode": str(mode),
        "candidate_limit": int(candidate_limit),
        "discovery_terms": int(discovery_terms),
        "dropped_terms": [],
        "unknown_terms": [],
        "candidates": 0,
        "abstained": str(mode) in _RECALL_REPORT_ABSTAINING_MODES,
    }


def _blank_claim_recall_report(mode: str) -> dict[str, Any]:
    """Fresh diagnostic record for one claim-lane recall attempt."""
    return {
        "channel": "claim",
        "mode": str(mode),
        "candidate_limit": int(MAX_MEMORY_SEARCH_CANDIDATES),
        "discovery_terms": 0,
        "candidates": 0,
        "returned": 0,
        "abstained": False,
        "reason": None,
    }


# --- the lesson lane's diagnostic record (M4 design 5.4) -------------------
#
# ``match_lessons`` has fifteen ``return []`` statements, two raises and an
# empty-prefix break, and before M4 a caller could not tell any of them from
# "the store had nothing".  ``learning_ladder.LESSON_EXITS`` is the single
# mapping from each of those exits to one of sixteen modes, and it is owned by
# ``learning_ladder`` alone so the store, the Agent, the tests and the sealed
# holdout scorer cannot drift apart: the store writes its report through
# ``lesson_recall_record`` and the scorer reads the same table.
#
# This is **instrumentation only**.  M4 changes no threshold, no ordering and
# no refusal in ``match_lessons``, and a differential test pins the returned
# rows byte for byte against a baseline captured before the change.
LESSON_RECALL_MODES: tuple[str, ...] = learning_ladder.LESSON_RECALL_MODES
LESSON_ABSTAINING_MODES: frozenset[str] = learning_ladder.LESSON_ABSTENTION_MODES
# ``_lesson_control_validation`` reasons that mean the operator retired the
# advice or it aged out, as opposed to a tamper or a scope mismatch.  These are
# the answer to "why did my lesson go quiet", reported as
# ``superseded_shadowed``, printed by ``/ladder``, and never shown to the
# model.
_LESSON_LIFECYCLE_SHADOW_REASONS: frozenset[str] = frozenset({
    "superseded", "contradicted", "quarantined", "expired",
})


def _blank_graph_recall_report(mode: str) -> dict[str, Any]:
    """Fresh diagnostic record for one graph-channel read.

    ``mode`` is the closed set of M3 design 5.6: ``idle``, ``screened``,
    ``project-unavailable``, ``no-start``, ``identity-conflict``, ``overflow``,
    ``budget-exceeded``, ``screened-rows``, ``no-answer``, ``complete``,
    ``error``.
    """
    return {
        "channel": "graph",
        "mode": str(mode),
        "budget": None,
        "starts": 0,
        "expanded": 0,
        "edges": 0,
        "chains": 0,
        "rows": 0,
        "overflow": 0,
        "incomplete": 0,
        "excluded_by_screen": 0,
        "abstained": False,
        "reason": None,
        "lane_mode": None,
        "lane_abstained": False,
        "subjects_dropped": 0,
        # Typed names that resolved nothing while another named subject did
        # (design 10.7 item 4).  The walk fills it; the key is declared here so
        # every report carries it, including the abstention paths that never
        # reach the walk, and so a consumer can read it without a default.
        "unresolved": [],
        "elapsed_ms": 0.0,
    }


# --- prior-conversation excerpts: the transcript recall channel -------------
#
# A brand-new conversation could reach what the operator said in an earlier
# one only through the ``session_search`` tool, which the model rarely chose
# (4 of 100 LongMemEval turns, 0 of 50 LoCoMo turns in the first live runs).
# This channel gives the automatic read path a bounded look at the transcript
# rows of OTHER conversations in the same store: FTS over ``messages``, the
# claims lane's query screens, the staged OR / discriminating-terms / all-terms
# narrowing with scoped term counts and the unknown-identity floor, one
# whole-call deadline, a hard cap on excerpts, and the widened private-
# identifier screen on every excerpt.  The current conversation is never read
# (same-conversation recall inflates scores and is banned), a governed command
# or receipt row is never an excerpt (those facts live in the claims lane,
# which outranks this channel), and an excerpt never becomes a claim, a
# memory, or a receipt.
TRANSCRIPT_RECALL_EXCERPT_CAP = 8
TRANSCRIPT_RECALL_TIME_BUDGET_MS = 25.0
TRANSCRIPT_RECALL_COLD_TIME_BUDGET_MS = 40.0
TRANSCRIPT_RECALL_EXCERPT_CHARS = 600
# Overlapping windows no longer than the screen's own scan cap, so the
# over-long rule never fires on prose and a value straddling a boundary is
# still seen whole by one window.
_TRANSCRIPT_SCREEN_WINDOW = 512
_TRANSCRIPT_SCREEN_STEP = 448
_TRANSCRIPT_GOVERNED_ROW = re.compile(
    r"(?is)\A\s*(?:(?:remember|forget|erase)\s+this\s+project\s+fact\s*:"
    r"|(?:stored|updated|reasserted|retracted|forgot|erased)\s+project\s+fact\s*\()"
)
_TRANSCRIPT_RECALL_ABSTAINING_MODES = frozenset({
    "screened", "empty", "project-unavailable", "unknown-identity", "overflow",
    "error",
})


def _blank_transcript_recall_report(
    mode: str, *, budget_ms: float = TRANSCRIPT_RECALL_TIME_BUDGET_MS
) -> dict[str, Any]:
    """Fresh diagnostic record for one transcript-channel read.

    ``mode`` is ``idle``, ``screened``, ``empty``, ``project-unavailable``,
    ``or`` / ``narrowed`` / ``all-terms`` / ``like`` (the discovery stage that
    produced the pool), ``unknown-identity`` / ``overflow`` / ``error``
    (fail-closed abstentions) or ``budget-exceeded`` (what was found so far
    is returned, and ``budget`` names the limit that stopped the call).
    """
    return {
        "channel": "transcript",
        "mode": str(mode),
        "budget_ms": float(budget_ms),
        "elapsed_ms": 0.0,
        "budget": None,
        "candidate_limit": int(MAX_MEMORY_SEARCH_CANDIDATES),
        "candidates": 0,
        "returned": 0,
        "excluded_by_screen": 0,
        "excluded_governed": 0,
        "dropped_terms": [],
        "unknown_terms": [],
        "abstained": str(mode) in _TRANSCRIPT_RECALL_ABSTAINING_MODES,
        "reason": None,
    }


def _transcript_excerpt_window(content: str, spellings: Sequence[str]) -> str:
    """A bounded window of one transcript row around its first matched term.

    Presentation only: the row was selected by FTS rank.  A short row is
    shown whole; a long one is cut to ``TRANSCRIPT_RECALL_EXCERPT_CHARS``
    starting a little before the earliest query spelling so the matched
    passage, not the row's opening, is what the model sees.
    """
    text = " ".join(str(content).split())
    limit = int(TRANSCRIPT_RECALL_EXCERPT_CHARS)
    if len(text) <= limit:
        return text
    folded = text.casefold()
    first = -1
    for spelling in spellings:
        needle = str(spelling).casefold()
        if not needle:
            continue
        position = folded.find(needle)
        if position >= 0 and (first < 0 or position < first):
            first = position
    start = 0 if first < 0 else max(0, first - limit // 3)
    if start + limit > len(text):
        start = max(0, len(text) - limit)
    window = text[start:start + limit]
    return ("..." if start else "") + window + ("..." if start + limit < len(text) else "")


def _transcript_excerpt_screen_reason(text: str) -> str | None:
    """The widened private-identifier / secret screen over one excerpt.

    ``screen_endpoint`` was built for entity labels and treats any value over
    its 512-character scan cap as ``long_value``; an excerpt is prose, so it
    is screened in overlapping windows of at most that length instead, each
    with the full kind set (secrets, e-mail, phone, bare IPv4/IPv6, SSN,
    Luhn card, street address, user-home path).  Any hit drops the excerpt.
    """
    value = str(text)
    if not value:
        return None
    starts = range(0, max(1, len(value) - _TRANSCRIPT_SCREEN_WINDOW + _TRANSCRIPT_SCREEN_STEP), _TRANSCRIPT_SCREEN_STEP)
    for start in starts:
        blocked, reason = screen_endpoint(value[start:start + _TRANSCRIPT_SCREEN_WINDOW])
        if blocked:
            return str(reason or "private_identifier")
    return None








def _bounded_limit(value: int, maximum: int) -> int:
    return max(0, min(int(value), maximum))


# --- the learning ladder: record tables, sequence and lineage (schema 49) ---
#
# Two record tables, never projections.  A sealed calibration epoch and a
# skill promotion are *records of decisions*: neither is derivable from the
# claim projection, so neither is rebuilt by ``rebuild-claims`` and neither
# joins ``memory_spine._REBUILT_PROJECTIONS`` (M4 design 4.1, M-12).  Both
# carry spine lineage instead, both are created once by
# ``Memory._migrate_v49``, and **neither is ever dropped by a migration** — a
# downgrade that would discard authentic spine-backed ladder state refuses to
# open, exactly as migration 46 refuses an authentic spine below 46 (M4
# design 4.3, H-6).

LADDER_TABLES: tuple[str, ...] = (
    "memory_calibration_ledger",
    "ladder_promotions",
    "ladder_id_sequence",
)
LADDER_PROMOTION_STAGES: frozenset[str] = frozenset({
    "staged", "approved", "unapproved_legacy", "rolled_back", "withdrawn",
    "discarded",
})
# The two stages that claim the live document.  One row per
# ``(project_id, skill_name)`` may hold either, which
# ``idx_ladder_promotions_one_live`` enforces and the grandfather pass relies
# on for idempotence (M4 design 4.3, S-8).
LADDER_LIVE_STAGES: tuple[str, ...] = ("approved", "unapproved_legacy")
# The seven digest-only spine kinds of M4 design 4.4.  They are spelled here
# so ``memory.py`` stays importable before ``memory_spine`` carries them;
# ``tests/test_learning_ladder_integration.py`` pins this tuple against
# ``memory_spine.LADDER_KINDS``, so the two can never drift.
LADDER_SPINE_KINDS: tuple[str, ...] = (
    "ladder.calibration_sealed",
    "ladder.candidate",
    "ladder.staged",
    "ladder.grandfathered",
    "ladder.approved",
    "ladder.rolled_back",
    "ladder.withdrawn",
)
# A promotion row may name only a candidate or a grandfather event as its
# lineage; a ledger row may name only a seal event.  The triggers below
# enforce that on write and ``verify_calibration_ledger`` re-checks it, in
# both directions, on read.
LADDER_PROMOTION_CREATING_KINDS: tuple[str, ...] = (
    "ladder.candidate", "ladder.grandfathered",
)
LADDER_LEDGER_CREATING_KIND = "ladder.calibration_sealed"
# The gate population, shared verbatim with ``competence()`` and
# ``calibration_gate``, so a sealed epoch and the gate always describe the
# same rows (M4 design 2.2).
LADDER_LEDGER_ORIGINS: tuple[str, ...] = ("interactive", "worker", "proactive")
# A recorded tool name is a bounded, screened identifier or nothing at all
# (M4 design 3.4, H-7).
_LADDER_TOOL_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
# Domain tag for the keyed coverage digest, tagged the way
# ``memory_spine.content_digest`` is, so a database without its key sidecar
# can neither forge nor brute-force one.
_LADDER_COVERAGE_DIGEST_TAG = "jarvis-ladder-coverage-v1"

_LADDER_LEDGER_SQL = """CREATE TABLE memory_calibration_ledger (
    id INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL,
    family TEXT NOT NULL,
    epoch INTEGER NOT NULL CHECK(epoch > 0),
    n INTEGER NOT NULL CHECK(n > 0),
    successes INTEGER NOT NULL CHECK(successes >= 0 AND successes <= n),
    mean_predicted REAL NOT NULL CHECK(mean_predicted BETWEEN 0.0 AND 1.0),
    brier REAL NOT NULL CHECK(brier BETWEEN 0.0 AND 1.0),
    calibration_error REAL NOT NULL CHECK(calibration_error BETWEEN 0.0 AND 1.0),
    evidence_applicable INTEGER NOT NULL CHECK(evidence_applicable >= 0),
    evidence_successes INTEGER NOT NULL CHECK(evidence_successes >= 0),
    applied_n INTEGER NOT NULL CHECK(applied_n >= 0),
    applied_successes INTEGER NOT NULL CHECK(applied_successes >= 0),
    unapplied_n INTEGER NOT NULL CHECK(unapplied_n >= 0),
    unapplied_successes INTEGER NOT NULL CHECK(unapplied_successes >= 0),
    refused_stagings INTEGER NOT NULL DEFAULT 0 CHECK(refused_stagings >= 0),
    refused_approvals INTEGER NOT NULL DEFAULT 0 CHECK(refused_approvals >= 0),
    withdrawals INTEGER NOT NULL DEFAULT 0 CHECK(withdrawals >= 0),
    screened_components INTEGER NOT NULL DEFAULT 0 CHECK(screened_components >= 0),
    unverified_at_seal INTEGER NOT NULL DEFAULT 0 CHECK(unverified_at_seal >= 0),
    first_prediction_id INTEGER NOT NULL,
    last_prediction_id INTEGER NOT NULL
        CHECK(last_prediction_id >= first_prediction_id),
    -- The exact prediction ids the epoch covers, ascending, canonical JSON.
    -- The [first, last] pair above is the reported range and the index key,
    -- but it does NOT determine the covered set: a prediction held open while
    -- the block around it is cut sits inside that range, so a range test
    -- would leave it permanently uncoverable while competence() kept counting
    -- it (design 10.7 item 8, the S-2 defect one level down).  The spine
    -- payload stays digest-only: coverage_digest binds this set, and the ids
    -- themselves live only here.
    covered_ids_json TEXT NOT NULL,
    coverage_digest TEXT NOT NULL
        CHECK(length(coverage_digest)=64 AND coverage_digest NOT GLOB '*[^0-9a-f]*'),
    spine_event_id INTEGER NOT NULL UNIQUE,
    UNIQUE(family, epoch),
    FOREIGN KEY(spine_event_id) REFERENCES memory_spine_events(id)
)"""

# ``approval_token`` is a CONFIRMATION CODE, not a capability (design 4.2,
# S-1): sixteen random url-safe characters generated at staging, stored in
# cleartext on the row, read back only by the operator surfaces (``ladder
# list``, ``ladder show``, ``/ladder``), compared with
# ``hmac.compare_digest``, and single-use because a successful approval moves
# the row out of ``staged``.  Its job is to prove the operator looked at the
# staged document.  It is in no spine payload (which carries only the boolean
# ``token_required``), no ``activity_log`` row, no run metric, no Presence
# payload and no prompt block.  Revision 2's ``approval_token_sha256`` column
# is withdrawn.
#
# The four one-directional stage CHECKs (S-6) replace revision 2's equality,
# which would have nulled ``approved_sha256``/``approved_at`` on every
# transition to ``rolled_back`` or ``withdrawn`` and so destroyed the row's
# record of what was live and when.  A terminal row keeps both.
_LADDER_PROMOTIONS_SQL = """CREATE TABLE ladder_promotions (
    id INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    project_id INTEGER NOT NULL,
    family TEXT NOT NULL,
    skill_name TEXT NOT NULL,
    stage TEXT NOT NULL CHECK(stage IN
        ('staged','approved','unapproved_legacy','rolled_back','withdrawn','discarded')),
    stage_reason TEXT,
    lesson_ids_json TEXT NOT NULL,
    proof_json TEXT NOT NULL,
    proof_sha256 TEXT NOT NULL
        CHECK(length(proof_sha256)=64 AND proof_sha256 NOT GLOB '*[^0-9a-f]*'),
    reuse_count INTEGER NOT NULL CHECK(reuse_count >= 0),
    context_count INTEGER NOT NULL CHECK(context_count >= 0),
    epoch_id INTEGER,
    gate_json TEXT NOT NULL,
    staged_sha256 TEXT NOT NULL
        CHECK(length(staged_sha256)=64 AND staged_sha256 NOT GLOB '*[^0-9a-f]*'),
    approval_token TEXT NOT NULL
        CHECK(length(approval_token) BETWEEN 16 AND 43
              AND approval_token NOT GLOB '*[^A-Za-z0-9_-]*'),
    approved_sha256 TEXT,
    approved_at TEXT,
    prior_sha256 TEXT,
    prior_document BLOB CHECK(prior_document IS NULL OR length(prior_document) <= 32768),
    prior_document_pruned INTEGER NOT NULL DEFAULT 0
        CHECK(prior_document_pruned IN (0,1)),
    spine_event_id INTEGER NOT NULL UNIQUE,
    FOREIGN KEY(project_id) REFERENCES agent_projects(id),
    FOREIGN KEY(epoch_id) REFERENCES memory_calibration_ledger(id),
    FOREIGN KEY(spine_event_id) REFERENCES memory_spine_events(id),
    CHECK(stage NOT IN ('staged','discarded') OR approved_sha256 IS NULL),
    CHECK(stage NOT IN ('staged','discarded') OR approved_at IS NULL),
    CHECK(stage NOT IN ('approved','unapproved_legacy') OR approved_sha256 IS NOT NULL),
    CHECK(stage NOT IN ('approved','unapproved_legacy') OR approved_at IS NOT NULL)
)"""

_LADDER_SEQUENCE_SQL = """CREATE TABLE ladder_id_sequence (
    id INTEGER PRIMARY KEY CHECK(id=1),
    next_id INTEGER NOT NULL CHECK(next_id > 0)
)"""

_LADDER_INDEX_SQL: tuple[str, ...] = (
    """CREATE INDEX IF NOT EXISTS idx_memory_calibration_ledger_family
       ON memory_calibration_ledger(family, epoch)""",
    """CREATE UNIQUE INDEX IF NOT EXISTS idx_ladder_promotions_one_staged
       ON ladder_promotions(project_id, skill_name) WHERE stage='staged'""",
    """CREATE UNIQUE INDEX IF NOT EXISTS idx_ladder_promotions_one_live
       ON ladder_promotions(project_id, skill_name)
       WHERE stage IN ('approved','unapproved_legacy')""",
    """CREATE INDEX IF NOT EXISTS idx_ladder_promotions_scope
       ON ladder_promotions(project_id, family, stage, id)""",
)

# Modelled byte-for-byte on ``memory_spine._MEMORY_LINEAGE_TRIGGER_SQL``,
# including its ``NEW.id IS -1`` property: a BEFORE INSERT trigger sees -1
# when the writer omits the id, so an implicit id aborts and every ladder id
# comes from ``ladder_id_sequence``.
_LADDER_PROMOTION_LINEAGE_TRIGGER_SQL = (
    """CREATE TRIGGER ladder_promotions_require_spine_event
BEFORE INSERT ON ladder_promotions
WHEN NEW.spine_event_id IS NULL OR NOT EXISTS (
    SELECT 1 FROM memory_spine_events AS e
    WHERE e.id = NEW.spine_event_id
      AND e.kind IN ('ladder.candidate','ladder.grandfathered')
      AND e.subject_kind = 'ladder' AND e.subject_id = NEW.id)
BEGIN SELECT RAISE(ABORT, 'ladder promotions require a spine event'); END"""
)
_LADDER_LEDGER_LINEAGE_TRIGGER_SQL = (
    """CREATE TRIGGER memory_calibration_ledger_require_spine_event
BEFORE INSERT ON memory_calibration_ledger
WHEN NEW.spine_event_id IS NULL OR NOT EXISTS (
    SELECT 1 FROM memory_spine_events AS e
    WHERE e.id = NEW.spine_event_id
      AND e.kind = 'ladder.calibration_sealed'
      AND e.subject_kind = 'calibration' AND e.subject_id = NEW.id)
BEGIN SELECT RAISE(ABORT, 'calibration ledger rows require a spine event'); END"""
)
# These two make "append-only" a database property rather than a convention.
_LADDER_LEDGER_APPEND_ONLY_TRIGGER_SQL = (
    """CREATE TRIGGER memory_calibration_ledger_append_only
BEFORE UPDATE ON memory_calibration_ledger
BEGIN SELECT RAISE(ABORT, 'the calibration ledger is append-only'); END"""
)
_LADDER_LEDGER_NO_DELETE_TRIGGER_SQL = (
    """CREATE TRIGGER memory_calibration_ledger_no_delete
BEFORE DELETE ON memory_calibration_ledger
BEGIN SELECT RAISE(ABORT, 'the calibration ledger is append-only'); END"""
)
LADDER_TRIGGER_SQL: dict[str, str] = {
    "ladder_promotions_require_spine_event":
        _LADDER_PROMOTION_LINEAGE_TRIGGER_SQL,
    "memory_calibration_ledger_require_spine_event":
        _LADDER_LEDGER_LINEAGE_TRIGGER_SQL,
    "memory_calibration_ledger_append_only":
        _LADDER_LEDGER_APPEND_ONLY_TRIGGER_SQL,
    "memory_calibration_ledger_no_delete":
        _LADDER_LEDGER_NO_DELETE_TRIGGER_SQL,
}


# One call bounds a bulk catch-up so ``ladder seal --all`` over a long history
# takes many short write locks rather than one long one (L-5).
_LADDER_SEAL_MAX_EPOCHS = 4096
_LADDER_PROOF_DIGEST_TAG = "jarvis-ladder-proof-v1"
_LADDER_PLAN_DIGEST_TAG = "jarvis-ladder-plan-v1"
_LADDER_VERIFY_CACHE_TAG = "jarvis-ladder-unverified-v1"
_LESSON_APPLIED_KIND = "lesson.applied"
# The confirmation code's alphabet, the same one the column CHECK names.
# Shape-checked before ``hmac.compare_digest``, which refuses a non-ASCII
# str operand with a TypeError: an operator typing a fullwidth or Cyrillic
# code must get a refusal, not a traceback (R-4).
_LADDER_APPROVAL_CODE_RE = re.compile(r"^[A-Za-z0-9_-]{16,43}$")
# Every reason code the ladder emits is a closed code, never prose; the spine's
# payload validator enforces the same shape from the other side.
_LADDER_REASON_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
# The two template lines whose variable parts design 3.4 requires to be
# re-screened.  They are the whole screenable surface of a staged document:
# everything else in it is fixed template text.
_LADDER_TOOLS_LINE_RE = re.compile(
    r"^Tools sampled from \d+ verified reuses:\s*(.+)$", re.MULTILINE
)
_LADDER_ORACLES_LINE_RE = re.compile(
    r"^Verification oracles observed:\s*(.+)$", re.MULTILINE
)


def _ladder_covered_id_list(value: Any) -> list[int]:
    """The covered prediction ids of one sealed epoch, ascending.

    A row whose column cannot be parsed contributes nothing rather than
    raising: ``verify_calibration_ledger`` reports it as
    ``coverage_shape_invalid`` and the tail treats those rows as uncovered,
    which is the fail-closed direction (they get sealed again into a new
    epoch and the overlap is then visible).
    """
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError):
        return []
    if not isinstance(parsed, list):
        return []
    ids: list[int] = []
    for item in parsed:
        if isinstance(item, bool) or not isinstance(item, int) or item <= 0:
            return []
        ids.append(int(item))
    return ids


def _ladder_payload(value: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _ladder_epoch_dict(row: Mapping[str, Any]) -> dict[str, Any]:
    """One sealed epoch as plain data, with the covered ids parsed.

    ``learning_ladder.monotonicity_verdict`` reads a subset of these keys and
    ignores the rest, so this is also the exact shape the statistics see.
    """
    record = {key: row[key] for key in row.keys()}
    record["covered_ids"] = _ladder_covered_id_list(row["covered_ids_json"])
    return record


def _ladder_promotion_dict(
    row: Mapping[str, Any], *, include_token: bool = False
) -> dict[str, Any]:
    """One promotion row as plain data.

    ``approval_token`` is dropped unless the caller asked for it, and
    ``prior_document`` is never returned: it is up to 32 KB of bytes that only
    ``rollback_ladder_promotion`` needs, and a reader that carries it by
    default is a reader that eventually logs it.
    """
    record = {
        key: row[key] for key in row.keys()
        if key not in {"approval_token", "prior_document"}
    }
    record["lesson_ids"] = _ladder_payload_list(row["lesson_ids_json"])
    record["has_prior_document"] = row["prior_document"] is not None
    if include_token:
        record["approval_token"] = str(row["approval_token"])
    return record


def _ladder_payload_list(value: Any) -> list[Any]:
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


def _ladder_clear_catalog_cache() -> None:
    """Drop ``learning_ladder``'s per-workspace catalog memo.

    Called after this module moves a learned document, which it does behind
    that module's back.  Tolerant of a build where the helper is absent: a
    missing memo is not a reason to fail a withdrawal.
    """
    clear = getattr(learning_ladder, "clear_catalog_cache", None)
    if callable(clear):
        try:
            clear()
        except Exception:  # noqa: BLE001 - a stale memo must not fail a write
            pass


def _ladder_payload_admits(kind: str, key: str) -> bool:
    """Does the spine's contract for ``kind`` allow ``key``?

    Lets this module carry a field the moment ladder-core's key set admits it,
    without a flag day in either direction: before it lands the key is simply
    not sent, and no store writes a payload its own validator would reject.
    """
    try:
        _required, allowed = memory_spine.payload_keys(str(kind))
    except Exception:  # noqa: BLE001 - an unknown or open kind admits nothing
        return False
    return str(key) in allowed



def _ladder_proof_record(proof: Mapping[str, Any]) -> dict[str, Any]:
    """The stored form of a derived proof: counts, ids and screened names.

    Never lesson text and never operator prose, so ``proof_json`` is safe to
    keep on a row an operator surface renders, and the privacy screen has a
    small exact surface.  The row is a record of what was counted; the digest
    beside it is what anything actually trusts, and every gate re-derives
    rather than reading either.
    """
    return {
        "family": str(proof["family"]),
        "project_id": int(proof["project_id"]),
        "lesson_ids": [int(value) for value in proof["lesson_ids"]],
        "reuses": int(proof["reuses"]),
        "contexts": int(proof["contexts"]),
        "tool_names": [str(name) for name in proof["tool_names"]],
        "oracles": [str(name) for name in proof["oracles"]],
        "application_ids": [
            int(use["application_id"]) for use in proof["applications"]
        ],
        "prediction_ids": sorted({
            int(use["prediction_id"]) for use in proof["applications"]
        }),
        "effectiveness": dict(proof["effectiveness"]),
        "minimum_reuses": int(proof["minimum_reuses"]),
        "minimum_lessons": int(proof["minimum_lessons"]),
    }


def _ladder_document_components(document: Mapping[str, Any]) -> list[str]:
    """The screenable components of a live or staged skill document.

    Only the variable parts design 3.4 defines -- the sampled tool names and
    the observed oracle names -- because everything else in the template is
    fixed text the store wrote itself.  A pre-M4 legacy document carries
    neither line and yields nothing, which is honest: there is nothing in it
    the ladder chose.
    """
    content = str(document.get("content") or "")
    names: list[str] = []
    for pattern in (_LADDER_TOOLS_LINE_RE, _LADDER_ORACLES_LINE_RE):
        for match in pattern.finditer(content):
            for part in str(match.group(1)).split(","):
                cleaned = part.strip()
                if cleaned and cleaned != "none recorded":
                    names.append(cleaned)
    return names


def _sqlite_table_exists(db: sqlite3.Connection, table: str) -> bool:
    return db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _sqlite_table_columns(db: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in db.execute(f"PRAGMA table_info({table})")}


def ladder_ready(db: sqlite3.Connection) -> bool:
    """True once every ladder object of schema 49 exists."""
    return all(_sqlite_table_exists(db, name) for name in LADDER_TABLES)


def ladder_sequence_floor(db: sqlite3.Connection) -> int:
    """The highest ladder id the store has ever used: live rows in either
    record table and every ladder- or calibration-subject spine event, so a
    discarded id never comes back."""
    ledger = db.execute(
        "SELECT COALESCE(MAX(id), 0) FROM memory_calibration_ledger"
    ).fetchone()[0]
    promotions = db.execute(
        "SELECT COALESCE(MAX(id), 0) FROM ladder_promotions"
    ).fetchone()[0]
    seen = db.execute(
        """SELECT COALESCE(MAX(subject_id), 0) FROM memory_spine_events
           WHERE subject_kind IN ('ladder','calibration')"""
    ).fetchone()[0]
    return max(int(ledger or 0), int(promotions or 0), int(seen or 0))


def allocate_ladder_id(db: sqlite3.Connection) -> int:
    """Explicit, never-reused ladder ids from the one sequence both record
    tables share (the spine's ``subject_kind`` tells a promotion from an
    epoch).  Allocate only inside the write transaction that appends the
    lineage event: the trigger aborts an implicit id."""
    row = db.execute("SELECT next_id FROM ladder_id_sequence WHERE id=1").fetchone()
    if row is None:
        raise memory_spine.SpineError("ladder id sequence is missing")
    ladder_id = int(row[0])
    if ladder_id <= ladder_sequence_floor(db):
        raise memory_spine.SpineError(
            "ladder id sequence is behind the store; run ladder verify"
        )
    db.execute("UPDATE ladder_id_sequence SET next_id=? WHERE id=1", (ladder_id + 1,))
    return ladder_id


def ladder_coverage_digest(key: bytes, covered: Sequence[Sequence[Any]]) -> str:
    """Keyed digest over one sealed epoch's exact coverage.

    ``covered`` is the list of ``(id, predicted_success, actual_status,
    evidence_ok)`` for every row the epoch covers, sorted by id.  Keyed, so an
    epoch re-cut over different rows — a hand-edited ``last_prediction_id``, a
    covered failure flipped to ``complete``, a row inserted into a sealed
    range — cannot be made to re-derive without the spine key sidecar (M4
    design 2.2, 2.4).
    """
    material = memory_spine.canonical([
        [
            int(row[0]),
            float(row[1]),
            str(row[2]),
            None if row[3] is None else int(row[3]),
        ]
        for row in sorted(covered, key=lambda item: int(item[0]))
    ])
    return hmac.new(
        key,
        (_LADDER_COVERAGE_DIGEST_TAG + "\0" + material).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def screened_tool_name(value: Any) -> str | None:
    """One bounded, screened tool name, or ``None``.

    ``[a-z][a-z0-9_]{0,63}`` and clean under both ``screen_endpoint`` (the
    widened M3 screen) and ``contains_secret`` (M4 design 3.4).  Anything else
    becomes ``None`` rather than raising: this runs on the outcome-recording
    path, and a strange tool name must never cost the store a resolved
    prediction — losing one would change the gate population, which is the
    thing the calibration ledger exists to keep honest.  A non-string is still
    a caller bug and raises.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("primary_tool must be a string or None")
    name = value.strip()
    if not _LADDER_TOOL_NAME_RE.match(name):
        return None
    if contains_secret(name) or screen_endpoint(name)[0]:
        return None
    return name


from .memory_runtime import (  # noqa: F401 - stable public facade
    DEFAULT_BUSY_TIMEOUT_MS, DEFAULT_LEASE_SECONDS,
    _with_recall_cache, _with_read_snapshot, _with_immediate_snapshot,
)
from .memory_approvals import ApprovalsMemoryMixin
from .memory_claims import ClaimsMemoryMixin
from .memory_conversation_compaction import ConversationCompactionMemoryMixin
from .memory_conversations import ConversationsMemoryMixin
from .memory_embedding_store import EmbeddingStoreMemoryMixin
from .memory_governance import GovernanceMemoryMixin
from .memory_learning_ladder import LearningLadderMemoryMixin
from .memory_lessons import LessonsMemoryMixin
from .memory_operator_state import OperatorStateMemoryMixin
from .memory_ordinary_recall import OrdinaryRecallMemoryMixin
from .memory_predictions import PredictionsMemoryMixin
from .memory_presence_companion import PresenceCompanionMemoryMixin
from .memory_projects_budget import ProjectsBudgetMemoryMixin
from .memory_schema_migrations import SchemaMigrationsMemoryMixin
from .memory_strategy_transfer import StrategyTransferMemoryMixin
from .memory_strategy_trial import StrategyTrialMemoryMixin
from .memory_tasks_scheduling import TasksSchedulingMemoryMixin


class Memory(
    ApprovalsMemoryMixin,
    ClaimsMemoryMixin,
    ConversationCompactionMemoryMixin,
    ConversationsMemoryMixin,
    EmbeddingStoreMemoryMixin,
    GovernanceMemoryMixin,
    LearningLadderMemoryMixin,
    LessonsMemoryMixin,
    OperatorStateMemoryMixin,
    OrdinaryRecallMemoryMixin,
    PredictionsMemoryMixin,
    PresenceCompanionMemoryMixin,
    ProjectsBudgetMemoryMixin,
    SchemaMigrationsMemoryMixin,
    StrategyTransferMemoryMixin,
    StrategyTrialMemoryMixin,
    TasksSchedulingMemoryMixin,
):
    ORDINARY_MEMORY_PROVENANCE_ORIGINS = _ORDINARY_MEMORY_PROVENANCE_ORIGINS
    SCREEN_COMPANION_LEARNING_CATEGORIES = frozenset({
        "coding", "general", "navigation", "organization", "research", "writing",
    })
    SCREEN_COMPANION_LEARNING_DECISIONS = frozenset({"accepted", "dismissed"})
    SCREEN_COMPANION_LEARNING_OUTCOMES = frozenset({
        "complete", "failed", "incomplete",
    })
    SCREEN_COMPANION_LEARNING_EVIDENCE = frozenset({
        "cited_sources", "failure_observed", "process_evidence", "tool_success",
    })
    SCREEN_COMPANION_PREDICTION_ORIGINS = frozenset({
        "companion_action", "companion_suggestion",
    })
    PREDICTION_FAMILIES = frozenset({
        "code_build", "code_fix", "code_refactor", "code_test", "deep_research",
        "learning_brief", "file_ops", "desktop_file_ops", "external_publish",
        "security_analysis", "conversation",
    })
    PREDICTION_FAILURE_CLASSES = frozenset({
        "misread_spec", "wrong_target_file", "verification_absent",
        "verification_vacuous", "tool_denied_policy", "approval_required",
        "budget_exhausted", "model_unavailable", "model_hallucinated_api",
        "research_no_authoritative_source", "edit_conflict_hash", "probe_failed",
        "cancelled", "unknown",
    })
    PREDICTION_ORIGINS = frozenset({
        "companion_action", "companion_suggestion", "interactive", "worker",
        "proactive", "practice",
    })
    PREDICTION_VERIFICATION = frozenset({
        "process_evidence", "cited_sources", "tool_success", "not_applicable",
    })

    def __init__(
        self,
        path: Path,
        *,
        busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
        worker_id: str | None = None,
    ) -> None:
        self.path = Path(path)
        if str(self.path) != ":memory:":
            from .sqlite_preflight import inspection_connection, validate_database_path
            try:
                path_exists = validate_database_path(self.path)
                if path_exists:
                    with inspection_connection(self.path) as preflight:
                        existing_version = int(
                            preflight.execute("PRAGMA user_version").fetchone()[0]
                        )
            except (OSError, sqlite3.DatabaseError, TypeError, ValueError) as exc:
                raise RuntimeError("Database could not be inspected safely") from exc
            if path_exists and existing_version > SCHEMA_VERSION:
                raise RuntimeError(
                    f"Database schema version {existing_version} is newer than "
                    f"supported version {SCHEMA_VERSION}"
                )
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.worker_id = _validated_worker_id(
            f"{os.getpid()}:{uuid4().hex}" if worker_id is None else worker_id
        )
        self._closed = False
        self._strategy_transfer_candidate_telemetry: dict[str, Any] = {
            "schema": "jarvis.strategy-transfer-candidate-health.v1",
            "available": True,
            "reason": "not_evaluated",
            "quarantined_strategies": 0,
            "unavailable_strategies": 0,
        }
        self._strategy_transfer_trial_telemetry: dict[str, Any] = {
            "schema": "jarvis.strategy-transfer-trial-health.v1",
            "available": True,
            "reason": "not_evaluated",
            "eligible_manifests": 0,
        }
        self._claim_clock_ready = False
        # The spine key lives beside the database; it is loaded once the
        # store is open (created only for a store that has no spine yet).  An
        # in-memory store gets an ephemeral key.  Hooks stay off until the
        # spine schema exists.
        self._spine_key = b""
        self._spine_ready = False
        # The temporal graph is a projection of the claim projection; the
        # write hooks stay off until its tables exist (schema 48).
        self._graph_ready = False
        # The learning ladder's two record tables (schema 49).  Unlike the
        # graph these are not a projection: the flag gates the ladder
        # methods, and nothing rebuilds them.
        self._ladder_ready = False
        # Typed-invariant compaction (schema 50).  Like the ladder these are
        # records, not a projection -- a compacted span is the ONLY copy of
        # the transcript rows it replaced -- so nothing rebuilds them and a
        # downgrade over them refuses rather than dropping (M5 design 2.11,
        # H-1).
        self._compaction_ready = False
        self._bridge_ready = False
        self._dropped_spine_events = 0
        # Per-store memo for pure recall helpers; digest-keyed, byte-bounded,
        # cleared on deletion and close (see RecallCache).
        self._recall_cache = RecallCache()
        self._last_claim_recall_report = _blank_claim_recall_report("idle")
        self._last_graph_recall_report = _blank_graph_recall_report("idle")
        self._last_transcript_recall_report = _blank_transcript_recall_report("idle")
        # The first transcript read on a store object pays the cold cost
        # (page cache, statement cache), so it gets the cold deadline once.
        self._transcript_recall_warm = False
        self._last_lesson_recall_report = learning_ladder.lesson_recall_record(
            "idle"
        )
        # Turn-path writes that lost the race for the write lock (S-3).
        # Held in memory as well as written to the receipt path, because
        # the receipt itself needs the very lock the write just failed to
        # get: the caller must be able to learn about the degradation on
        # the turn it happened, not only after the worker lets go.
        self._degraded_writes: list[dict[str, Any]] = []
        self._pending_degraded_receipts: list[tuple[str, str, str]] = []
        self._pending_claim_clock_updates: list[tuple[Any, ...]] = []
        self._dropped_claim_clock_reads = 0
        # Diagnostic record of the most recent lexical recall attempt so an
        # abstention at scale is observable instead of silent.  Read it
        # through ``recall_report()``, which returns a copy.
        self._last_recall_report: dict[str, Any] = _blank_recall_report("idle")
        self.vault: Vault | None = None
        busy_timeout_ms = max(100, min(int(busy_timeout_ms), 120_000))
        self._busy_timeout_ms = busy_timeout_ms
        self.db = sqlite3.connect(
            str(self.path),
            timeout=busy_timeout_ms / 1000,
            isolation_level=None,
        )
        self.db.row_factory = sqlite3.Row
        self.db.create_function(
            "jarvis_sha256", 1, _sqlite_sha256_text, deterministic=True
        )
        self.db.create_function(
            "jarvis_ordinary_memory_provenance_sha256",
            7,
            _sqlite_ordinary_memory_provenance_sha256,
            deterministic=True,
        )
        try:
            self.db.execute("PRAGMA foreign_keys=ON")
            self.db.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
            self.db.execute("PRAGMA synchronous=FULL")
            # Freed pages are zeroed so erased or deleted values do not linger
            # in the file (right to forget; see docs/MEMORY_SPINE.md).
            self.db.execute("PRAGMA secure_delete=ON")
            has_spine = self.db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='memory_spine_head'"
            ).fetchone() is not None
            self._spine_key = memory_spine.load_spine_key(
                None if str(self.path) == ":memory:" else self.path,
                create=not has_spine,
            )
            self._migrate()
            if str(self.path) != ":memory:":
                self.db.execute("PRAGMA journal_mode=WAL").fetchone()
            self._claim_clock_ready = True
            self._spine_ready = memory_spine.spine_ready(self.db)
            self._graph_ready = memory_graph.graph_ready(self.db)
            self._ladder_ready = ladder_ready(self.db)
            self._compaction_ready = memory_compaction.compaction_ready(self.db)
            self._bridge_ready = memory_bridge.bridge_ready(self.db)
        except BaseException:
            self.db.close()
            self._closed = True
            raise






    @property
    def closed(self) -> bool:
        return self._closed

    def __enter__(self) -> "Memory":
        if self._closed:
            raise RuntimeError("Memory database is closed")
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()

    def close(self) -> None:
        if self._closed:
            return
        if self.db.in_transaction:
            self.db.rollback()
        # A degradation queued while the worker held the write lock is written
        # here or never (R-10): a CLI invocation, a worker pass or a turn whose
        # store closes at the end would otherwise leave no trace of a write it
        # dropped, and the epoch counters would lose it too.
        try:
            self._flush_degraded_receipts()
        except sqlite3.DatabaseError:
            pass
        self.db.close()
        self._closed = True
        self._recall_cache.clear()

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("Memory database is closed")

    @contextmanager
    def _immediate_transaction(self) -> Iterator[None]:
        self._ensure_open()
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            if self.db.in_transaction:
                self.db.rollback()
            raise
        else:
            self.db.commit()

    def _migrate(self) -> None:
        with self._immediate_transaction():
            # The version check and every migration share one write lock. A
            # concurrent upgrader cannot change authority between them.
            version = int(self.db.execute("PRAGMA user_version").fetchone()[0])
            if version > SCHEMA_VERSION:
                raise RuntimeError(
                    f"Database schema version {version} is newer than supported version {SCHEMA_VERSION}"
                )
            # FIRST, before any other schema work.  A store carrying compacted
            # spans below schema 50 is a downgrade over records that cannot be
            # rebuilt: the ``messages`` rows a span replaced were deleted when
            # it was written, so the span is the only copy.  The check has to
            # precede the graph DROP three lines down (N-9) or a migration
            # that is about to refuse has already destroyed the graph on its
            # way to raising.
            self._refuse_compaction_downgrade_locked(version)
            if version < 48:
                # A store re-migrated from below 48 runs the 46 and 47 steps
                # first, and those write and rewrite ``memory_claims``; a
                # stale graph from an earlier 48 would still hold foreign keys
                # into rows they are about to recreate.  Dropping the three
                # graph tables at the very start of the migration transaction
                # makes the whole migration see a store with no graph, which
                # is the state ``_migrate_v48`` is written for.  A partial
                # graph from an interrupted migration was never authoritative
                # (M3 design 4.3 step 0; the same premise as v45's proposals
                # table).
                for statement in memory_graph.DROP_GRAPH_SQL:
                    self.db.execute(statement)
            if version < 47:
                # Legacy steps below write claims and memories before the
                # spine exists; a lineage trigger left over from a newer life
                # must not abort them.  v46 recreates the spine and v47 its
                # complete trigger set.
                memory_spine.drop_spine_triggers(self.db)
            if version < 1:
                self._migrate_v1()
                version = 1
            if version < 2:
                self._migrate_v2()
                version = 2
            if version < 3:
                self._migrate_v3()
                version = 3
            if version < 4:
                self._migrate_v4()
                version = 4
            if version < 5:
                self._migrate_v5()
                version = 5
            if version < 6:
                self._migrate_v6()
                version = 6
            if version < 7:
                self._migrate_v7()
                version = 7
            if version < 8:
                self._migrate_v8()
                version = 8
            if version < 9:
                self._migrate_v9()
                version = 9
            if version < 10:
                self._migrate_v10()
                version = 10
            if version < 11:
                self._migrate_v11()
                version = 11
            if version < 12:
                self._migrate_v12()
                version = 12
            if version < 13:
                self._migrate_v13()
                version = 13
            if version < 14:
                self._migrate_v14()
                version = 14
            if version < 15:
                self._migrate_v15()
                version = 15
            if version < 16:
                self._migrate_v16()
                version = 16
            if version < 17:
                self._migrate_v17()
                version = 17
            if version < 18:
                self._migrate_v18()
                version = 18
            if version < 19:
                self._migrate_v19()
                version = 19
            if version < 20:
                self._migrate_v20()
                version = 20
            if version < 21:
                self._migrate_v21()
                version = 21
            if version < 22:
                self._migrate_v22()
                version = 22
            if version < 23:
                self._migrate_v23()
                version = 23
            if version < 24:
                self._migrate_v24()
                version = 24
            if version < 25:
                self._migrate_v25()
                version = 25
            if version < 26:
                self._migrate_v26()
                version = 26
            if version < 27:
                self._migrate_v27()
                version = 27
            if version < 28:
                self._migrate_v28()
                version = 28
            if version < 29:
                self._migrate_v29()
                version = 29
            if version < 30:
                self._migrate_v30()
                version = 30
            if version < 31:
                self._migrate_v31()
                version = 31
            if version < 32:
                self._migrate_v32()
                version = 32
            if version < 33:
                self._migrate_v33()
                version = 33
            if version < 34:
                self._migrate_v34()
                version = 34
            if version < 35:
                self._migrate_v35()
                version = 35
            if version < 36:
                self._migrate_v36()
                version = 36
            if version < 37:
                self._migrate_v37()
                version = 37
            if version < 38:
                self._migrate_v38()
                version = 38
            if version < 39:
                self._migrate_v39()
                version = 39
            if version < 40:
                self._migrate_v40()
                version = 40
            if version < 41:
                self._migrate_v41()
                version = 41
            if version < 42:
                self._migrate_v42()
                version = 42
            if version < 43:
                self._migrate_v43()
                version = 43
            quality_schema_healthy = False
            if version < 44:
                self._migrate_v44()
                version = 44
            else:
                quality_schema_healthy = (
                    self._learning_quality_schema_healthy_locked()
                )
                if not quality_schema_healthy:
                    self._migrate_v44()
            # Decisions are derived from authenticated canonical rows. Repair
            # missing/stale decisions and deterministically restore trigger
            # definitions on every open, including a healthy-looking v44 DB.
            self._reconcile_learning_quality_assessments_locked(
                reinstall_triggers=not quality_schema_healthy
            )
            if version < 45:
                self._migrate_v45()
                version = 45
            if version < 46:
                self._migrate_v46()
                version = 46
            if version < 47:
                self._migrate_v47()
                version = 47
            if version < 48:
                self._migrate_v48()
                version = 48
            if version < 49:
                self._migrate_v49()
                version = 49
            if version < 50:
                self._migrate_v50()
                version = 50
            if version < 51:
                self._migrate_v51()
                version = 51
            if version < 52:
                self._migrate_v52()
                version = 52
            if version < 53:
                self._migrate_v53()
                version = 53
            if version < 54:
                self._migrate_v54()
                version = 54
            self.db.execute(f"PRAGMA user_version={version}")






























































    @staticmethod
    def _project_id(value: int | None) -> int:
        if value is None:
            return 1
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError("project_id must be a positive integer")
        if value > 9_223_372_036_854_775_807:
            raise ValueError("project_id is out of range")
        return value


































    # --- the calibration ledger (M4 design 2) -------------------------------
    #
    # An append-only record of what the gate was reading, cut mechanically
    # into fixed-size epochs.  ``competence()`` answers "how good is Jarvis at
    # this family right now, over everything currently in the table"; the
    # ledger answers the three questions that are different in kind: what was
    # true when the gate authorised a promotion, whether competence has
    # regressed since, and whether the ladder itself misbehaved in that
    # period.  Nothing here is derived on read: the numbers were frozen at
    # seal time, which is why erasing evidence afterwards cannot improve a
    # verdict.



















    # --- the promotion ladder: the proof, and reading the record ------------
    #
    # Design 3.3's proof is re-derived at every gate and never trusted from
    # the row.  ``LADDER_MIN_VERIFIED_REUSES`` is a **usage threshold, not a
    # significance test**: three successes at an 0.8 base rate happen 51 % of
    # the time by chance, and an application row is filed when the lesson
    # *matched*, not when the model used it, so "verified reuse" means "the
    # lesson was in the prompt and the turn succeeded".  The statistical work
    # is done by the effectiveness clause and by the ledger's regression
    # predicate, both of which have a comparison group; the reuse count only
    # ensures the artefact is not built from a single incident.








    # --- reading the promotion record ---------------------------------------
















    # --- the promotion ladder: the write path -------------------------------
    #
    # Rung 2 is an *event*, not a stored stage: ``candidate`` names the moment
    # the proof is met, the gate is open and the ledger has not regressed, and
    # it is recorded as ``ladder.candidate``, which is also the row's lineage.
    # The row is born at ``staged``, because a candidate that is never staged
    # leaves no artefact to govern.  A row never moves backwards: rolled back,
    # withdrawn and discarded are terminal, and a later candidate opens a new
    # row, so the history of what was live, when, and on whose authority is a
    # sequence of rows and events rather than a mutable state machine.



















    # --- the gate-closed half of the abstention cue (design 10.7 item 10) ---




    # --- the reconciler (design 6.3) ----------------------------------------



















































































































































    # --- typed-invariant compaction (schema 50) ---------------------------
    #
    # Ownership, so a later reader does not have to reconstruct it:
    # ``memory_compaction`` owns every pure decision -- what a turn is, where a
    # span may be cut, the canonical form, both digests, the invariants and the
    # summary.  This file owns the SQL, the transaction, the receipt, and the
    # judgement calls a pure function is not allowed to make: whether the spine
    # verified, whether a conversation is in scope, and whether the rebuild
    # equivalence holds (design 11.18 -- two sources, one comparison, done by
    # the layer allowed to have an opinion).





















    # ``@_with_recall_cache`` FIRST, then ``@_with_read_snapshot``: M3's
    # channel 3 is memoised per store, and the pair is load-bearing in that
    # order.  Do not insert anything between a decorator and the function it
    # decorates -- an M5 insertion anchored on ``@_with_read_snapshot`` landed
    # a whole method block here and silently moved the memo onto an unrelated
    # helper.  Nothing failed, because the behaviour is memo-identical and only
    # latency moves.  Anchor future insertions on a line matching
    # ``^(@|class |def )`` and put new code AFTER a decorated function.

































































































































    # Proactive-assistant control plane ---------------------------------
