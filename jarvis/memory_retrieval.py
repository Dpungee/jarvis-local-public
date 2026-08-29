from __future__ import annotations

import math
import re
import sqlite3
from collections import Counter
from typing import Any


MAX_MEMORY_QUERY_TERMS = 8
MAX_MEMORY_SEARCH_CANDIDATES = 2_000

_MEMORY_SEARCH_STOPWORDS = frozenset({
    "about", "after", "also", "been", "before", "could", "does", "explain",
    "from", "have", "into", "just", "more", "please", "should", "tell", "than",
    "that", "their", "there", "these", "they", "this", "using", "what", "when",
    "where", "which", "with", "would", "your",
})
_LIKE_LITERAL_EDGE_CHARS = "\"'`.,!?;:()[]{}<>"


def _normalize_memory_token(token: str) -> str:
    """Apply a deliberately small amount of stemming for durable-memory lookup."""
    token = token.casefold()
    if len(token) > 4 and token.endswith("ies"):
        return token[:-3] + "y"
    if len(token) > 4 and token.endswith("es") and token[-3] in "sxz":
        return token[:-2]
    if len(token) > 3 and token.endswith("s") and not token.endswith(("ss", "us", "is")):
        return token[:-1]
    return token


def _memory_tokens(value: str, *, meaningful_only: bool) -> list[str]:
    tokens = [
        _normalize_memory_token(token)
        for token in re.findall(r"[^\W_]+", str(value).casefold(), flags=re.UNICODE)
    ]
    if meaningful_only:
        tokens = [
            token
            for token in tokens
            if len(token) >= 2 and token not in _MEMORY_SEARCH_STOPWORDS
        ]
    return tokens


def _memory_query_terms(query: str) -> list[str]:
    """Return bounded, de-duplicated terms while retaining query order."""
    terms: list[str] = []
    for token in _memory_tokens(query, meaningful_only=True):
        if token not in terms:
            terms.append(token)
        if len(terms) >= MAX_MEMORY_QUERY_TERMS:
            break
    return terms


def _memory_like_terms(query: str, semantic_terms: list[str]) -> list[str]:
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
    for term in semantic_terms:
        if term in literal_semantic_terms:
            continue
        semantic_candidates.append(term)
        if len(term) > 3 and term.endswith("y") and term[-2] not in "aeiou":
            # LIKE cannot apply the token normalization used by the final ranker.
            semantic_candidates.append(term[:-1] + "ies")
    ordered = [*literal_terms, *semantic_candidates]
    unique: list[str] = []
    for term in ordered:
        if term not in unique:
            unique.append(term)
        if len(unique) >= MAX_MEMORY_QUERY_TERMS * 2:
            break
    return unique


def _memory_fts_query(query: str, query_terms: list[str]) -> str | None:
    """Build a literal OR query; wildcard-bearing input keeps the LIKE fallback."""
    if not query_terms or "%" in query or "_" in query:
        return None
    quoted = [f'"{term.replace(chr(34), chr(34) * 2)}"' for term in query_terms]
    return " OR ".join(quoted)


def _rank_memory_rows(
    rows: list[sqlite3.Row],
    query_terms: list[str],
    *,
    keep_id: bool = False,
) -> list[dict[str, Any]]:
    """Rank candidates by query coverage, phrase fidelity, then BM25 relevance."""
    if not rows or not query_terms:
        return []

    documents = [_memory_tokens(row["content"], meaningful_only=False) for row in rows]
    document_frequencies = Counter(
        term
        for tokens in documents
        for term in set(tokens).intersection(query_terms)
    )
    average_length = sum(len(tokens) for tokens in documents) / max(1, len(documents))
    normalized_query = " ".join(query_terms)
    scored: list[tuple[tuple[float, ...], sqlite3.Row]] = []

    for row, tokens in zip(rows, documents, strict=True):
        row_keys = set(row.keys())
        frequencies = Counter(tokens)
        matched = [term for term in query_terms if frequencies[term]]
        if not matched:
            # The SQL prefilter is substring-based; discard accidental partial matches.
            continue
        coverage = len(matched) / len(query_terms)
        normalized_document = " ".join(tokens)
        exact_document = float(normalized_document == normalized_query)
        phrase_match = float(normalized_query in normalized_document)
        length_ratio = len(tokens) / max(1.0, average_length)
        bm25 = 0.0
        for term in matched:
            frequency = frequencies[term]
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
            coverage,
            exact_document,
            phrase_match,
            learned_utility,
            bm25,
            density,
            float(row["id"]),
        )
        scored.append((score, row))

    scored.sort(key=lambda item: item[0], reverse=True)
    results: list[dict[str, Any]] = []
    for _, row in scored:
        result = dict(row)
        result.pop("utility_resolved", None)
        result.pop("utility_successes", None)
        memory_id = result.pop("id", None)
        if keep_id and memory_id is not None:
            result["memory_id"] = int(memory_id)
        results.append(result)
    return results
