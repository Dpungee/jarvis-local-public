from __future__ import annotations

import math
import re
import sqlite3
import unicodedata
from collections import Counter
from collections.abc import Iterable
from typing import Any

from .redaction import normalize_private_identifier_text


MAX_MEMORY_QUERY_TERMS = 8
MAX_MEMORY_SEARCH_CANDIDATES = 2_000
_MAX_MEMORY_QUERY_TERM_CANDIDATES = 64

_MEMORY_SEARCH_STOPWORDS = frozenset({
    "about", "after", "again", "also", "am", "an", "and", "any", "are", "as", "at", "be", "been",
    "before", "being", "between", "both", "but", "by", "can", "could", "did", "do",
    "come", "does", "doing", "each", "either", "every",
    "explain", "fictional", "for", "from", "had", "has", "have", "having", "here", "hers", "him",
    "his", "how", "if", "in", "including", "into", "invented", "is", "it", "its", "itself", "just", "keep", "later", "many",
    "more", "most", "must", "my", "not", "of", "off", "on", "once", "one", "only", "or", "other",
    "our", "ours", "out", "over", "own",
    "please", "same", "say", "she", "should", "so", "some", "such", "tell", "than", "that", "the",
    "their", "theirs", "them", "themselves", "then", "there", "these", "they", "this",
    "those", "through", "to", "too", "under", "until", "up", "using", "very", "was",
    "we", "were", "what",
    "when", "where", "which", "while", "who", "whom", "why", "will", "with", "would",
    "use", "want", "you", "your", "yours", "yourself", "yourselves",
})
_LIKE_LITERAL_EDGE_CHARS = "\"'`.,!?;:()[]{}<>"
_AUTHORITY_EVASION_TERMS = frozenset({
    "avoid", "bypass", "circumvent", "deactivate", "disable", "disregard",
    "discard", "evade", "force", "ignore", "merge", "overrule", "override",
    "remove", "regardless", "skip", "trust", "turn", "waive", "without",
})
# Verbs whose dominant reading is defeating a control rather than ordinary
# editing or navigation.  They abstain when paired with any control noun.
_STRONG_AUTHORITY_EVASION_TERMS = frozenset({
    "bypass", "circumvent", "deactivate", "disable", "disregard",
    "evade", "ignore", "overrule", "override", "waive",
})
_AUTHORITY_CONTROL_TERMS = frozenset({
    "approval", "authorization", "gate", "guardrail", "permission",
    "policy", "restriction", "safety", "authority", "source", "provenance",
    "family", "project", "scope", "validation", "clearance", "signoff",
    "access", "boundary", "check", "confirmation", "consent", "control",
    "governance", "requirement", "review", "rule", "safeguard", "security",
    "verification",
})
# Nouns that name a governance control in their dominant reading.  Ordinary
# context nouns (family, project, scope, source) stay in the wider set so that
# unambiguous evasion verbs still abstain around them, but everyday verbs such
# as remove/skip/turn/trust no longer suppress recall about ordinary topics.
_STRONG_AUTHORITY_CONTROL_TERMS = frozenset({
    "approval", "authorization", "authority", "clearance", "gate",
    "guardrail", "permission", "policy", "provenance", "restriction",
    "safety", "signoff", "validation", "access", "boundary", "check",
    "confirmation", "consent", "control", "governance", "requirement",
    "review", "rule", "safeguard", "security", "verification",
})
_AUTHORITY_EVASION_LEXEMES = frozenset({
    *_AUTHORITY_EVASION_TERMS,
    "avoided", "avoiding", "avoidance",
    "bypassed", "bypasses", "bypassing",
    "circumvented", "circumventing", "circumvention",
    "deactivated", "deactivating", "deactivation",
    "disabled", "disabling",
    "disregarded", "disregarding",
    "discarded", "discarding",
    "evaded", "evading", "evasion",
    "forced", "forcing",
    "ignored", "ignores", "ignoring",
    "merged", "merging",
    "overrode", "overridden", "overriding",
    "overruled", "overruling",
    "removed", "removes", "removing", "removal",
    "skipped", "skipping",
    "trusted", "trusting",
    "waived", "waiving", "waiver",
})
_STRONG_AUTHORITY_EVASION_LEXEMES = frozenset({
    *_STRONG_AUTHORITY_EVASION_TERMS,
    "bypassed", "bypasses", "bypassing",
    "circumvented", "circumventing", "circumvention",
    "deactivated", "deactivating", "deactivation",
    "disabled", "disabling",
    "disregarded", "disregarding",
    "evaded", "evading", "evasion",
    "ignored", "ignores", "ignoring",
    "overrode", "overridden", "overriding",
    "overruled", "overruling",
    "waived", "waiving", "waiver",
})


def _separator_tolerant_term_pattern(terms: Iterable[str]) -> re.Pattern[str]:
    """Match declared security words even when split by punctuation/spaces."""
    alternatives = []
    for term in sorted(set(terms), key=lambda item: (-len(item), item)):
        alternatives.append(r"[\W_]*".join(re.escape(character) for character in term))
    return re.compile(
        r"(?<![^\W_])(?:" + "|".join(alternatives) + r")(?![^\W_])",
        re.I | re.UNICODE,
    )


_AUTHORITY_EVASION_OBFUSCATED_RE = _separator_tolerant_term_pattern(
    _AUTHORITY_EVASION_LEXEMES
)
_STRONG_AUTHORITY_EVASION_OBFUSCATED_RE = _separator_tolerant_term_pattern(
    _STRONG_AUTHORITY_EVASION_LEXEMES
)
_AUTHORITY_CONTROL_OBFUSCATED_RE = _separator_tolerant_term_pattern(
    _AUTHORITY_CONTROL_TERMS
)
_STRONG_AUTHORITY_CONTROL_OBFUSCATED_RE = _separator_tolerant_term_pattern(
    _STRONG_AUTHORITY_CONTROL_TERMS
)
_HYPHENATED_COMPOUND_RE = re.compile(
    r"[^\W\d_]+(?:-[^\W\d_]+)+",
    re.UNICODE,
)
_AUTHORITY_NONAPPLICABILITY_RE = re.compile(
    r"\b(?:does|do|did)\s+not\s+apply\b|\bnot\s+applicable\b",
    re.I,
)
_DEFENSIVE_POST_INCIDENT_RE = re.compile(
    r"\b(?:analy(?:s|z)e|defend|detect|harden|mitigate|prevent)\w*\b"
    r".{0,120}\b(?:a|the)\s+(?:bypass|evasion)\b"
    r".{0,120}\b(?:controls?|defenses?|guardrails?|protections?)\b",
    re.I | re.UNICODE,
)
_ORDINARY_CONTROL_ARTIFACT_RE = re.compile(
    r"\b(?:project|family|scope|source)\s+"
    r"(?:[^\W_]+\s+){0,1}"
    r"(?:notes?|milestones?|documents?|summar(?:y|ies)|almanacs?)\b",
    re.I | re.UNICODE,
)
_ORDINARY_CONTROL_TERM_RE = re.compile(
    r"\b(?:project|family|scope|source)\b",
    re.I | re.UNICODE,
)
_COMPOUND_IDENTIFIER_RE = re.compile(
    r"(?<![^\W_])[^\W_]+(?:[-_.:][^\W_]+)+(?![^\W_])",
    re.UNICODE,
)
_MEMORY_NEGATION_TERMS = frozenset({
    "no", "not", "never", "none", "neither", "without",
})
_MEMORY_AFFIRMATIVE_PREDICATION_TERMS = frozenset({
    "am", "are", "be", "been", "being", "can", "could", "did", "do",
    "does", "had", "has", "have", "is", "must", "shall", "should",
    "was", "were", "will", "would",
})
_MEMORY_QUANTIFIER_CLASSES = {
    "universal": frozenset({"all", "always", "both", "each", "every"}),
    "partial": frozenset({"either", "occasionally", "some", "sometimes"}),
    "zero": frozenset({"neither", "never", "no", "none"}),
    "exclusive": frozenset({"exactly", "only"}),
}
_MEMORY_RETRIEVAL_SCOPE_VERBS = frozenset({
    "find", "get", "list", "lookup", "me", "recall", "remember", "remind", "retrieve",
    "return", "search", "show",
})


def _normalize_memory_token(token: str) -> str:
    """Apply a deliberately small amount of stemming for durable-memory lookup."""
    token = unicodedata.normalize("NFKC", str(token)).casefold()
    if len(token) > 4 and token.endswith("ies"):
        return token[:-3] + "y"
    if len(token) > 4 and token.endswith(("ches", "shes", "sses", "xes", "zes")):
        return token[:-2]
    if len(token) > 3 and token.endswith("s") and not token.endswith(("ss", "us", "is")):
        return token[:-1]
    return token


def _memory_term_variants(token: str) -> tuple[str, ...]:
    """Return bounded grammatical variants for lexical retrieval.

    This is intentionally smaller than a language stemmer: it only bridges
    common English ``-ed``/``-ing`` forms and never performs substring or
    edit-distance matching.  That keeps identifiers and unrelated short words
    from becoming accidental anchors.
    """
    canonical = _normalize_memory_token(token)
    variants = [canonical]
    for suffix in ("ing", "ed"):
        if len(canonical) <= len(suffix) + 3 or not canonical.endswith(suffix):
            continue
        stem = canonical[:-len(suffix)]
        for candidate in (stem, stem + "e"):
            if len(candidate) >= 4 and candidate not in variants:
                variants.append(candidate)
        if len(stem) > 3 and stem[-1:] == stem[-2:-1]:
            dedoubled = stem[:-1]
            if dedoubled not in variants:
                variants.append(dedoubled)
    return tuple(variants)


def _structured_memory_identifier(token: str) -> bool:
    """Recognize a bounded identifier without treating long words as IDs."""
    canonical = _normalize_memory_token(token)
    return (
        len(canonical) >= 5
        and any(character.isalpha() for character in canonical)
        and any(character.isdigit() for character in canonical)
    )


def _memory_identity_conflict(
    query_terms: Iterable[str],
    document_tokens: Iterable[str],
    matched_terms: Iterable[str],
) -> bool:
    """Detect near-identical compound identities such as NorthX vs SouthX.

    Retrieval should not inherit facts from a neighboring namespace merely
    because the rest of the sentence is identical.  This deliberately ignores
    containment (for example ``configuration``/``reconfiguration``) and only
    treats a large shared suffix with different leading affixes as a conflict.
    """
    matched = set(matched_terms)
    unmatched_query = {
        _normalize_memory_token(term)
        for term in query_terms
        if _normalize_memory_token(term) not in matched
    }
    document = {_normalize_memory_token(term) for term in document_tokens}
    for query_term in unmatched_query:
        if _structured_memory_identifier(query_term):
            query_shape = re.sub(
                r"[-_.:]", "", re.sub(r"\d+", "#", query_term)
            )
            for document_term in document:
                if (
                    document_term == query_term
                    or not _structured_memory_identifier(document_term)
                ):
                    continue
                document_shape = re.sub(
                    r"[-_.:]", "", re.sub(r"\d+", "#", document_term)
                )
                if document_shape == query_shape:
                    # CASE-123 and CASE-124 are different identities even
                    # though their surrounding prose may be identical.
                    return True
        if len(query_term) < 7:
            continue
        query_variants = set(_memory_term_variants(query_term))
        for document_term in document:
            if len(document_term) < 7 or document_term in matched:
                continue
            if query_variants.intersection(_memory_term_variants(document_term)):
                continue
            if query_term in document_term or document_term in query_term:
                continue
            suffix = 0
            for left, right in zip(
                reversed(query_term), reversed(document_term), strict=False
            ):
                if left != right:
                    break
                suffix += 1
            # A shared suffix captures compound namespaces such as
            # NorthAlderwick/SouthAlderwick.  A shared prefix alone is often a
            # legitimate derivational relation (deterministic/nondeterminism),
            # so it must not trigger an identity denial.
            shared = suffix
            shorter_length = min(len(query_term), len(document_term))
            if shared >= 6 and shared / shorter_length >= 0.60:
                return True
    return False


def _memory_tokens(value: str, *, meaningful_only: bool) -> list[str]:
    normalized = unicodedata.normalize("NFKC", str(value)).casefold()
    normalized = "".join(
        character
        for character in normalized
        if unicodedata.category(character) != "Cf"
    )
    surfaces = re.findall(r"[^\W_]+", normalized, flags=re.UNICODE)
    surface_tokens = [_normalize_memory_token(token) for token in surfaces]
    compound_identifiers = [
        _normalize_memory_token(surface)
        for surface in _COMPOUND_IDENTIFIER_RE.findall(normalized)
        if _structured_memory_identifier(surface)
    ]
    if meaningful_only:
        surface_tokens = [
            token
            for surface, token in zip(surfaces, surface_tokens, strict=True)
            if (
                len(token) >= 2
                and surface not in _MEMORY_SEARCH_STOPWORDS
                and token not in _MEMORY_SEARCH_STOPWORDS
            )
        ]
    ordered: list[str] = []
    for token in [*compound_identifiers, *surface_tokens]:
        if token not in ordered:
            ordered.append(token)
    return ordered


def _memory_candidate_terms(query: str) -> list[str]:
    """Return the bounded full lexical pool used to discover candidates."""
    all_terms = _memory_tokens(query, meaningful_only=True)
    terms = all_terms[:_MAX_MEMORY_QUERY_TERM_CANDIDATES]
    if len(all_terms) > _MAX_MEMORY_QUERY_TERM_CANDIDATES:
        terms[-1] = all_terms[-1]
    return terms


def _memory_query_terms(query: str) -> list[str]:
    """Return bounded, de-duplicated terms without letting preamble crowd out anchors."""
    terms = _memory_candidate_terms(query)
    if len(terms) <= MAX_MEMORY_QUERY_TERMS:
        return terms

    # Length is a small, language-agnostic proxy for information content.  IDs
    # containing digits get priority because a short version, port, or ticket can
    # be more discriminating than a long prose word.  Return the selected terms
    # in source order so phrase scoring remains meaningful and deterministic.
    boundary_indices = {0, len(terms) - 1}
    remaining_slots = MAX_MEMORY_QUERY_TERMS - len(boundary_indices)
    selected_indices = sorted(
        boundary_indices
        | set(sorted(
            (
                index
                for index in range(len(terms))
                if index not in boundary_indices
            ),
            key=lambda index: (
                any(character.isdigit() for character in terms[index]),
                min(len(terms[index]), 16),
                -index,
            ),
            reverse=True,
        )[:remaining_slots])
    )
    return [terms[index] for index in selected_indices]


def _memory_evidence_terms(
    query: str,
    rows: Iterable[sqlite3.Row | dict[str, Any]],
    *,
    content_key: str = "content",
    max_terms: int = MAX_MEMORY_QUERY_TERMS,
) -> list[str]:
    """Select rank terms that have evidence in the bounded candidate set.

    Terms absent from every candidate are not evidence and must not receive the
    highest inverse-frequency weight.  Identity and semantic-conflict checks
    still inspect the full raw query separately.
    """
    max_terms = max(1, int(max_terms))
    candidates = _memory_tokens(query, meaningful_only=True)
    if not candidates:
        return []
    documents = [
        set(_memory_tokens(str(row[content_key]), meaningful_only=False))
        for row in rows
    ]
    frequencies = {
        term: sum(
            bool(set(_memory_term_variants(term)).intersection(document))
            for document in documents
        )
        for term in candidates
    }
    evidence_indices = [
        index for index, term in enumerate(candidates) if frequencies[term] > 0
    ]
    if len(evidence_indices) <= max_terms:
        return [candidates[index] for index in evidence_indices]

    boundary_indices = {evidence_indices[0], evidence_indices[-1]}
    remaining_slots = max_terms - len(boundary_indices)
    selected_indices = sorted(
        boundary_indices
        | set(sorted(
            (
                index for index in evidence_indices
                if index not in boundary_indices
            ),
            key=lambda index: (
                -frequencies[candidates[index]],
                any(character.isdigit() for character in candidates[index]),
                min(len(candidates[index]), 16),
                -index,
            ),
            reverse=True,
        )[:remaining_slots])
    )
    return [candidates[index] for index in selected_indices]


def _memory_semantic_signature(
    value: str,
    *,
    query: bool,
) -> tuple[str, str | None]:
    """Return bounded proposition polarity and explicit quantifier class."""
    normalized = normalize_private_identifier_text(value).casefold()
    tokens = [
        _normalize_memory_token(token)
        for token in re.findall(r"[^\W_]+", normalized, flags=re.UNICODE)
    ]
    token_set = set(tokens)
    negative = bool(token_set.intersection(_MEMORY_NEGATION_TERMS))
    if negative:
        polarity = "negative"
    elif not query or token_set.intersection(_MEMORY_AFFIRMATIVE_PREDICATION_TERMS):
        polarity = "affirmative"
    else:
        polarity = "neutral"

    quantifier: str | None = None
    retrieval_scope = bool(
        query
        and tokens
        and tokens[0] in _MEMORY_RETRIEVAL_SCOPE_VERBS
        and any(
            token in set().union(*_MEMORY_QUANTIFIER_CLASSES.values())
            for token in tokens[:4]
        )
    )
    if not retrieval_scope:
        for name in ("zero", "exclusive", "universal", "partial"):
            if token_set.intersection(_MEMORY_QUANTIFIER_CLASSES[name]):
                quantifier = name
                break
    return polarity, quantifier


def _memory_semantic_constraints_compatible(query: str, document: str) -> bool:
    """Reject candidates that contradict an explicit query proposition."""
    query_polarity, query_quantifier = _memory_semantic_signature(query, query=True)
    document_polarity, document_quantifier = _memory_semantic_signature(
        document, query=False
    )
    if query_polarity != "neutral" and query_polarity != document_polarity:
        return False
    if query_quantifier is not None and query_quantifier != document_quantifier:
        return False
    return True


def _memory_resolve_sibling_identities(
    results: list[sqlite3.Row | dict[str, Any]],
    query: str,
    *,
    content_key: str = "content",
    identity_ignored_terms: Iterable[str] = (),
    unknown_identity_minimum_matches: int = 2,
    explicit_subject_identity: bool = False,
    capitalized_subject_identity: bool = False,
) -> list[sqlite3.Row | dict[str, Any]]:
    """Resolve or abstain from a cluster of near-identical subject records."""
    if not results:
        return results
    if unknown_identity_minimum_matches < 1:
        raise ValueError("unknown identity minimum matches must be positive")
    if explicit_subject_identity or capitalized_subject_identity:
        identity_scoped_results: list[sqlite3.Row | dict[str, Any]] = []
        for item in results:
            content = str(item[content_key] or "")
            declared = bool(
                explicit_subject_identity
                and re.match(
                    r"\s*(?:for\s+[^\W_]+\s*,|[^\W_]+[\u2019']s\b)",
                    content,
                    re.I | re.UNICODE,
                )
            )
            if not declared and capitalized_subject_identity:
                meaningful = _memory_tokens(content, meaningful_only=True)
                for surface in re.findall(r"[^\W_]+", content, re.UNICODE):
                    if (
                        meaningful
                        and _normalize_memory_token(surface) == meaningful[0]
                    ):
                        declared = surface[:1].isupper()
                        break
            if declared:
                identity_scoped_results.append(item)
        if not identity_scoped_results:
            return results
        checked = _memory_resolve_sibling_identities(
            identity_scoped_results,
            query,
            content_key=content_key,
            identity_ignored_terms=identity_ignored_terms,
            unknown_identity_minimum_matches=unknown_identity_minimum_matches,
        )
        if not checked:
            return []
        scoped_ids = {id(item) for item in identity_scoped_results}
        checked_ids = {id(item) for item in checked}
        return [
            item for item in results
            if id(item) not in scoped_ids or id(item) in checked_ids
        ]
    semantic_modifiers = set(_MEMORY_NEGATION_TERMS).union(
        *_MEMORY_QUANTIFIER_CLASSES.values()
    )
    token_lists = [
        [
            token for token in _memory_tokens(
                str(item[content_key] or ""), meaningful_only=True
            )
            if token not in semantic_modifiers
        ]
        for item in results
    ]
    if any(not tokens for tokens in token_lists):
        return results
    # Candidate discovery and scoring stay deliberately capped, but identity
    # conflict detection must inspect every bounded-input token.  Otherwise an
    # identity inserted beyond the candidate-term cap can be dropped while the
    # surrounding anchors still select a different subject's record.
    query_terms = set(_memory_tokens(query, meaningful_only=True))
    identity_query_terms = query_terms - _MEMORY_RETRIEVAL_SCOPE_VERBS - {
        _normalize_memory_token(term) for term in identity_ignored_terms
    }
    document_terms = set().union(*(set(tokens) for tokens in token_lists))
    candidate_identities = {tokens[0] for tokens in token_lists}
    matched_query_terms = {
        term for term in identity_query_terms
        if set(_memory_term_variants(term)).intersection(document_terms)
    }
    named_candidate_identity = any(
        set(_memory_term_variants(term)).intersection(candidate_identities)
        for term in identity_query_terms
    )
    selected_candidate_identities = {
        identity for identity in candidate_identities
        if any(
            set(_memory_term_variants(term)).intersection({identity})
            for term in identity_query_terms
        )
    }
    explicit_multi_fact_query = re.search(
        r"\b(?:and|plus)\b|[&+]", query, re.I
    ) is not None
    if (
        not named_candidate_identity
        and bool(identity_query_terms - matched_query_terms)
        and len(matched_query_terms) >= unknown_identity_minimum_matches
    ):
        # One strong lexical candidate is still a substitution when the query
        # explicitly names a different subject.
        return []
    if len(results) < 2:
        return results
    identities = [tokens[0] for tokens in token_lists]
    if len(set(identities)) < 2:
        return results

    residual_sets = [set(tokens[1:]) for tokens in token_lists]
    reference = residual_sets[0]
    if any(
        not reference
        or not residual
        or len(reference.intersection(residual))
        / max(1, len(reference.union(residual))) < 0.50
        for residual in residual_sets[1:]
    ):
        return results

    selected_identities = set(identities).intersection(
        selected_candidate_identities
    )
    if selected_identities:
        if len(selected_identities) > 1 and not explicit_multi_fact_query:
            return []
        return [
            item for item, identity in zip(results, identities, strict=True)
            if identity in selected_identities
        ]
    if identity_query_terms - matched_query_terms:
        # The query names a third sibling rather than the broad shared topic.
        return []
    return results


def _memory_query_targets_authority_evasion(query: str) -> bool:
    """Keep authority-bypass-shaped text from selecting reusable memory.

    Memory is supporting evidence, never authority.  Conservatively abstaining
    when a query combines a control with an evasion concept prevents a stored
    lesson or note from being recruited to weaken approval or policy gates.
    The live policy layer remains responsible for answering or refusing the
    request itself.

    Everyday verbs (remove, skip, turn, trust, ...) only count against nouns
    whose dominant reading is a governance control; pairing them with ordinary
    context nouns such as project, family, scope, or source is normal recall
    and must stay searchable.  Unambiguous evasion verbs and explicit
    non-applicability claims abstain for every control noun.
    """
    normalized_query = normalize_private_identifier_text(query).casefold()
    terms = set(_memory_tokens(normalized_query, meaningful_only=True))
    # Hyphenated compounds (for example sign-off) tokenize into fragments;
    # governance-noun detection also needs their joined spelling.
    for compound in _HYPHENATED_COMPOUND_RE.findall(normalized_query):
        terms.add(_normalize_memory_token(compound.replace("-", "")))
    evasion_forms = set(terms)
    for term in terms:
        if len(term) > 5 and term.endswith("ing"):
            stem = term[:-3]
            evasion_forms.update((stem, stem + "e"))
            if len(stem) > 2 and stem[-1] == stem[-2]:
                evasion_forms.add(stem[:-1])
        if len(term) > 4 and term.endswith("ed"):
            stem = term[:-2]
            evasion_forms.update((stem, stem + "e"))
            if len(stem) > 2 and stem[-1] == stem[-2]:
                evasion_forms.add(stem[:-1])
    if (
        not terms.intersection(_AUTHORITY_CONTROL_TERMS)
        and _AUTHORITY_CONTROL_OBFUSCATED_RE.search(normalized_query) is None
    ):
        return False
    if _AUTHORITY_NONAPPLICABILITY_RE.search(normalized_query):
        return True
    if _DEFENSIVE_POST_INCIDENT_RE.search(normalized_query):
        return False
    if _STRONG_AUTHORITY_EVASION_OBFUSCATED_RE.search(normalized_query):
        return True
    if (
        not evasion_forms.intersection(_AUTHORITY_EVASION_TERMS)
        and _AUTHORITY_EVASION_OBFUSCATED_RE.search(normalized_query) is None
    ):
        return False
    weak_evasions = evasion_forms.intersection(_AUTHORITY_EVASION_TERMS)
    # ``turn`` and ``without`` are ordinary connective words in many useful
    # requests.  Treat them as control evasion only when their local grammar
    # actually targets the named control.  This keeps phrases such as "turn
    # each finding into tested hardening" and "follow up without making the
    # person repeat requirements" retrievable while still rejecting "turn off
    # safety policy" and "continue without the sign-off".
    if weak_evasions and weak_evasions <= {"turn"}:
        turn_targets_control = re.search(
            r"\bturn\s+(?:off|down)\b|\bturn\b.{0,80}"
            r"\b(?:approval|authorization|clearance|consent|gate|guardrail|"
            r"permission|policy|review|safety|signoff|validation)\b.{0,24}"
            r"\b(?:off|down)\b",
            normalized_query,
            re.I | re.UNICODE,
        )
        if turn_targets_control is None:
            return False
    if weak_evasions and weak_evasions <= {"without"}:
        without_targets_control = re.search(
            r"\bwithout\b(?:\W+[^\W_]+){0,4}\W+"
            r"(?:approval|authorization|clearance|confirmation|consent|gate|"
            r"guardrail|permission|review|safety|sign(?:\W|_)*off|validation)\b",
            normalized_query,
            re.I | re.UNICODE,
        )
        if without_targets_control is None:
            return False
    if (
        terms.intersection(_STRONG_AUTHORITY_CONTROL_TERMS)
        or _STRONG_AUTHORITY_CONTROL_OBFUSCATED_RE.search(normalized_query)
    ):
        return True
    # Project, family, source, and scope also modify ordinary artifacts.  The
    # exception is valid only when *every* such control-like noun belongs to one
    # concrete artifact phrase.  Merely appending ``project summary`` must not
    # sanitize a separate ``skip project scope`` clause.
    artifact_matches = list(_ORDINARY_CONTROL_ARTIFACT_RE.finditer(normalized_query))
    control_matches = list(_ORDINARY_CONTROL_TERM_RE.finditer(normalized_query))
    ordinary_artifact = any(
        all(
            artifact.start() <= control.start()
            and control.end() <= artifact.end()
            for control in control_matches
        )
        for artifact in artifact_matches
    )
    return not ordinary_artifact


def _memory_surface_terms(query: str, semantic_terms: list[str]) -> list[str]:
    """Keep surface inflections that an unstemmed SQL index still needs to find."""
    selected = set(semantic_terms)
    surface_terms: list[str] = []
    normalized = unicodedata.normalize("NFKC", str(query)).casefold()
    for surface in re.findall(r"[^\W_]+", normalized, flags=re.UNICODE):
        canonical = _normalize_memory_token(surface)
        if canonical in selected and canonical not in _MEMORY_SEARCH_STOPWORDS:
            for term in (canonical, surface):
                if term not in surface_terms:
                    surface_terms.append(term)
    return surface_terms


def _memory_inflection_terms(term: str) -> tuple[str, ...]:
    """Return bounded index spellings that normalize back to the same token."""
    canonical = _normalize_memory_token(term)
    if len(canonical) > 3 and canonical.endswith("y") and canonical[-2] not in "aeiou":
        return canonical, canonical[:-1] + "ies"
    if len(canonical) > 3 and canonical.endswith(("ch", "sh", "ss", "x", "z")):
        return canonical, canonical + "es"
    if (
        len(canonical) > 3
        and not canonical.endswith(("s", "us", "is"))
    ):
        return canonical, canonical + "s"
    return (canonical,)


def _memory_like_terms(
    query: str,
    semantic_terms: list[str],
    *,
    max_terms: int = MAX_MEMORY_QUERY_TERMS * 2,
) -> list[str]:
    """Keep SQL wildcard characters literal without weakening semantic ranking."""
    literal_terms = []
    for raw in query.casefold().split():
        raw = raw.strip(_LIKE_LITERAL_EDGE_CHARS)
        if len(raw) > 2 and ("%" in raw or "_" in raw):
            literal_terms.append(raw)
    literal_semantic_terms = set(
        _memory_tokens(" ".join(literal_terms), meaningful_only=True)
    )
    semantic_candidates: list[str] = []
    for term in _memory_surface_terms(query, semantic_terms):
        if term in literal_semantic_terms:
            continue
        semantic_candidates.extend(_memory_term_variants(term))
        if len(term) > 3 and term.endswith("y") and term[-2] not in "aeiou":
            # LIKE cannot apply the token normalization used by the final ranker.
            semantic_candidates.append(term[:-1] + "ies")
    ordered = [*literal_terms, *semantic_candidates]
    unique: list[str] = []
    for term in ordered:
        if term not in unique:
            unique.append(term)
        if len(unique) >= max(1, int(max_terms)):
            break
    return unique


def _memory_fts_query(
    query: str,
    query_terms: list[str],
    *,
    max_index_terms: int = MAX_MEMORY_QUERY_TERMS * 3,
) -> str | None:
    """Build a literal OR query; wildcard-bearing input keeps the LIKE fallback."""
    if not query_terms or "%" in query or "_" in query:
        return None
    index_terms: list[str] = []
    surface_terms = _memory_surface_terms(query, query_terms) or query_terms
    for surface in surface_terms:
        if len(index_terms) >= max(1, int(max_index_terms)):
            break
        for term in (surface, *_memory_inflection_terms(surface)):
            if term not in index_terms:
                index_terms.append(term)
            if len(index_terms) >= max(1, int(max_index_terms)):
                break
    quoted = [
        f'"{term.replace(chr(34), chr(34) * 2)}"'
        for term in index_terms
    ]
    return " OR ".join(quoted)


def evaluate_response_conditioned_retrieval(
    relevant_ids: Iterable[str | int],
    conditioned_ids: Iterable[str | int],
) -> dict[str, int | float | bool | None]:
    """Score only memory records actually supplied to a generated response.

    ``conditioned_ids`` must be the post-filter, post-compaction records visible
    to the response generator, not the wider candidate ranking.  Precision is
    undefined for an abstention and recall is undefined when no relevant record
    exists; returning ``None`` keeps those states distinct from a measured zero.
    Duplicate IDs are counted once in both sets.
    """
    def normalized_ids(values: Iterable[str | int], label: str) -> set[str]:
        if isinstance(values, (str, bytes)):
            raise TypeError(f"{label} must be an iterable of IDs, not text")
        normalized: set[str] = set()
        for item in values:
            identifier = str(item).strip()
            if not identifier:
                raise ValueError(f"{label} cannot contain an empty ID")
            normalized.add(identifier)
        return normalized

    relevant = normalized_ids(relevant_ids, "relevant_ids")
    conditioned = normalized_ids(conditioned_ids, "conditioned_ids")
    hits = relevant.intersection(conditioned)
    return {
        "relevant_count": len(relevant),
        "conditioned_count": len(conditioned),
        "hit_count": len(hits),
        "abstained": not conditioned,
        "response_conditioned_precision": (
            len(hits) / len(conditioned) if conditioned else None
        ),
        "response_conditioned_recall": (
            len(hits) / len(relevant) if relevant else None
        ),
    }


def _rank_memory_rows(
    rows: list[sqlite3.Row],
    query_terms: list[str],
    *,
    keep_id: bool = False,
    content_key: str = "content",
    family_scope_single_anchor: bool = True,
    family_single_anchor_min_chars: int = 0,
    family_single_anchor_requires_identifier: bool = False,
    identity_conflict_shadow: bool = False,
    require_structured_identifier_match: bool = False,
    minimum_information_coverage: float = 0.0,
    relative_match_floor: float = 0.0,
    relative_information_floor: float = 0.0,
    specificity_gap_prunes_weaker: int = 0,
    query_text: str | None = None,
) -> list[dict[str, Any]]:
    """Rank candidates by query coverage, phrase fidelity, then BM25 relevance."""
    if not rows or not query_terms:
        return []
    if not 0.0 <= minimum_information_coverage <= 1.0:
        raise ValueError("minimum_information_coverage must be between 0 and 1")
    if not 0.0 <= relative_match_floor <= 1.0:
        raise ValueError("relative_match_floor must be between 0 and 1")
    if not 0.0 <= relative_information_floor <= 1.0:
        raise ValueError("relative_information_floor must be between 0 and 1")
    if family_single_anchor_min_chars < 0:
        raise ValueError("family_single_anchor_min_chars must not be negative")
    if specificity_gap_prunes_weaker < 0:
        raise ValueError("specificity_gap_prunes_weaker must not be negative")

    documents = [_memory_tokens(row[content_key], meaningful_only=False) for row in rows]
    query_variants = {
        term: set(_memory_term_variants(term)) for term in query_terms
    }
    document_frequencies = Counter(
        term for tokens in documents for term in query_terms
        if query_variants[term].intersection(tokens)
    )
    average_length = sum(len(tokens) for tokens in documents) / max(1, len(documents))
    normalized_query = " ".join(query_terms)
    scored: list[tuple[tuple[float, ...], int, sqlite3.Row, bool]] = []

    for row, tokens in zip(rows, documents, strict=True):
        if query_text is not None and not _memory_semantic_constraints_compatible(
            query_text,
            str(row[content_key]),
        ):
            continue
        row_keys = set(row.keys())
        frequencies = Counter(tokens)
        matched = [
            term for term in query_terms
            if query_variants[term].intersection(frequencies)
        ]
        if not matched:
            # The SQL prefilter is substring-based; discard accidental partial matches.
            continue
        if require_structured_identifier_match and any(
            _structured_memory_identifier(term) and term not in matched
            for term in query_terms
        ):
            # An explicit identifier is a hard target, not optional context.
            # Never substitute a record that only matches surrounding prose.
            continue
        # One coincidental word is weak evidence for a multi-concept request.
        # Short searches remain useful, while longer requests must share at
        # least two independently tokenized concepts with a candidate.
        family_scoped = (
            family_scope_single_anchor
            and "family" in row_keys
            and bool(row["family"])
        )
        strong_family_anchor = (
            family_scoped
            and (
                not family_single_anchor_min_chars
                or max(len(term) for term in matched)
                >= family_single_anchor_min_chars
            )
            and (
                not family_single_anchor_requires_identifier
                or len(query_terms) <= 2
                or any(_structured_memory_identifier(term) for term in matched)
            )
        )
        minimum_matches = (
            1 if len(query_terms) <= 2 or strong_family_anchor else 2
        )
        if len(matched) < minimum_matches:
            continue
        coverage = len(matched) / len(query_terms)
        normalized_document = " ".join(tokens)
        exact_document = float(normalized_document == normalized_query)
        phrase_match = float(normalized_query in normalized_document)
        length_ratio = len(tokens) / max(1.0, average_length)
        bm25 = 0.0
        matched_information = 0.0
        query_information = 0.0
        for term in query_terms:
            term_frequency = document_frequencies[term]
            inverse_frequency = math.log(
                1.0
                + (len(rows) - term_frequency + 0.5)
                / (term_frequency + 0.5)
            )
            # A modest token-length factor prevents short residual words from
            # dominating while keeping acronyms and numeric IDs eligible.
            information = inverse_frequency * (1.0 + math.log1p(len(term)))
            query_information += information
            if term in matched:
                matched_information += information
        for term in matched:
            frequency = sum(
                frequencies[variant] for variant in query_variants[term]
            )
            frequency_weight = (
                frequency * 2.2
                / (frequency + 1.2 * (0.25 + 0.75 * length_ratio))
            )
            inverse_frequency = math.log(
                1.0
                + (len(rows) - document_frequencies[term] + 0.5)
                / (document_frequencies[term] + 0.5)
            )
            bm25 += inverse_frequency * frequency_weight
        information_coverage = matched_information / max(query_information, 1e-12)
        lexical_query_information = sum(
            1.0 + math.log1p(len(term)) for term in query_terms
        )
        lexical_information_coverage = sum(
            1.0 + math.log1p(len(term)) for term in matched
        ) / max(lexical_query_information, 1e-12)
        if lexical_information_coverage < minimum_information_coverage:
            continue
        density = len(matched) / max(1, len(tokens))
        resolved_uses = (
            max(0, int(row["utility_resolved"] or 0))
            if "utility_resolved" in row_keys
            else 0
        )
        successful_uses = (
            max(0, int(row["utility_successes"] or 0))
            if "utility_successes" in row_keys
            else 0
        )
        observed_utility = (successful_uses + 2.0) / (resolved_uses + 4.0)
        learned_utility = 0.5 + (
            observed_utility - 0.5
        ) * min(1.0, resolved_uses / 10.0)
        score = (
            information_coverage,
            coverage,
            exact_document,
            phrase_match,
            learned_utility,
            bm25,
            density,
            float(row["id"]),
        )
        identity_conflict = (
            identity_conflict_shadow
            and _memory_identity_conflict(query_terms, tokens, matched)
        )
        scored.append((score, len(matched), row, identity_conflict))

    scored.sort(key=lambda item: item[0], reverse=True)
    if scored and scored[0][3]:
        # The strongest lexical candidate names a neighboring identity.  Do not
        # discard it and silently fall through to a weaker, unrelated record.
        return []
    scored = [item for item in scored if not item[3]]
    if (
        specificity_gap_prunes_weaker
        and len(scored) >= 2
        and scored[0][1] - scored[1][1] >= specificity_gap_prunes_weaker
    ):
        strongest_match_count = scored[0][1]
        scored = [
            item for item in scored if item[1] == strongest_match_count
        ]
    reference_match_count = scored[0][1] if scored else 0
    reference_information_coverage = scored[0][0][0] if scored else 0.0
    if scored and relative_match_floor:
        minimum_relative_matches = max(
            1,
            math.ceil(reference_match_count * relative_match_floor),
        )
        scored = [
            item for item in scored if item[1] >= minimum_relative_matches
        ]
    if scored and relative_information_floor:
        minimum_relative_information = (
            reference_information_coverage * relative_information_floor
        )
        scored = [
            item for item in scored
            if item[0][0] >= minimum_relative_information
        ]
    results: list[dict[str, Any]] = []
    for _, _, row, _identity_conflict in scored:
        result = dict(row)
        if content_key != "content":
            result.pop(content_key, None)
        result.pop("utility_resolved", None)
        result.pop("utility_successes", None)
        memory_id = result.pop("id", None)
        if keep_id and memory_id is not None:
            result["memory_id"] = int(memory_id)
        results.append(result)
    return results
