"""Current Memory domain extracted without changing method control flow.

Global dependencies resolve through jarvis.memory at call time. The facade keeps
public imports, patch seams, constants and authorization helpers authoritative.
"""
from __future__ import annotations

from typing import Any
from .memory_runtime import (_memory, _with_read_snapshot, _with_recall_cache)


class EmbeddingStoreMemoryMixin:
    """Mechanically extracted current Memory methods."""

    @staticmethod
    def _embedding_vector(value: Any, *, dimensions: int | None = None) -> list[float]:
        if not isinstance(value, list) or not value or len(value) > 4096:
            raise ValueError("Memory embedding must be a non-empty bounded vector")
        if dimensions is not None and len(value) != dimensions:
            raise ValueError("Memory embedding dimensions do not match")
        vector: list[float] = []
        for component in value:
            if isinstance(component, bool):
                raise ValueError("Memory embedding contains a non-number")
            try:
                number = float(component)
            except (TypeError, ValueError):
                raise ValueError("Memory embedding contains a non-number") from None
            if not _memory().math.isfinite(number):
                raise ValueError("Memory embedding contains a non-finite number")
            vector.append(number)
        if not any(vector):
            raise ValueError("Memory embedding must not be the zero vector")
        norm = _memory().math.hypot(*vector)
        if not _memory().math.isfinite(norm) or norm <= 0:
            raise ValueError("Memory embedding has an invalid norm")
        return vector

    @classmethod
    def _embedding_blob(cls, value: Any) -> tuple[list[float], bytes, float]:
        vector = cls._embedding_vector(value)
        try:
            blob = _memory().struct.pack(f"<{len(vector)}f", *vector)
            stored = list(_memory().struct.unpack(f"<{len(vector)}f", blob))
        except (OverflowError, _memory().struct.error):
            raise ValueError("Memory embedding cannot be represented as float32") from None
        norm = _memory().math.hypot(*stored)
        if not _memory().math.isfinite(norm) or norm <= 0:
            raise ValueError("Memory embedding has an invalid float32 norm")
        return stored, blob, norm

    @classmethod
    def _embedding_from_storage(
        cls,
        blob: Any,
        legacy_json: Any,
        dimensions: int,
    ) -> tuple[list[float], float]:
        raw = bytes(blob) if blob is not None else b""
        if len(raw) == dimensions * 4:
            try:
                vector = list(_memory().struct.unpack(f"<{dimensions}f", raw))
            except _memory().struct.error:
                vector = []
            if vector and all(_memory().math.isfinite(value) for value in vector):
                norm = _memory().math.hypot(*vector)
                if _memory().math.isfinite(norm) and norm > 0:
                    return vector, norm
        try:
            vector = cls._embedding_vector(
                _memory().json.loads(str(legacy_json)), dimensions=dimensions
            )
        except (ValueError, _memory().json.JSONDecodeError):
            raise ValueError("Stored memory embedding is invalid") from None
        norm = _memory().math.hypot(*vector)
        if not _memory().math.isfinite(norm) or norm <= 0:
            raise ValueError("Stored memory embedding has an invalid norm")
        return vector, norm

    @staticmethod
    def _query_embedding_sha256(query: str) -> str:
        normalized = str(query).strip()
        return _memory().hashlib.sha256(
            ("jarvis-query-embedding-v1\0" + normalized).encode("utf-8")
        ).hexdigest()

    def cached_query_embedding(
        self,
        query: str,
        model: str,
        *,
        dimensions: int | None = None,
    ) -> list[float] | None:
        """Return an exact cached query vector without storing the raw query."""
        normalized = str(query).strip()
        if not normalized or len(normalized) > _memory().MAX_SEARCH_QUERY_CHARS:
            return None
        if _memory().contains_secret(normalized):
            raise ValueError("Potential secret detected; query embedding cache refused")
        safe_model = _memory()._validated_nonsecret_metadata(model, "Embedding model")[:200]
        if dimensions is not None:
            if (
                isinstance(dimensions, bool)
                or not isinstance(dimensions, int)
                or not 1 <= dimensions <= 4096
            ):
                raise ValueError("Embedding dimensions are invalid")
            row = self.db.execute(
                """SELECT dimensions, embedding_blob
                   FROM memory_query_embeddings
                   WHERE query_sha256=? AND model=? AND dimensions=?""",
                (self._query_embedding_sha256(normalized), safe_model, dimensions),
            ).fetchone()
        else:
            row = self.db.execute(
                """SELECT dimensions, embedding_blob
                   FROM memory_query_embeddings
                   WHERE query_sha256=? AND model=?
                   ORDER BY last_used_at DESC LIMIT 1""",
                (self._query_embedding_sha256(normalized), safe_model),
            ).fetchone()
        if row is None:
            return None
        try:
            vector, _norm = self._embedding_from_storage(
                row["embedding_blob"], "[]", int(row["dimensions"])
            )
        except ValueError:
            return None
        self.db.execute(
            """UPDATE memory_query_embeddings
               SET hit_count=hit_count+1, last_used_at=?
               WHERE query_sha256=? AND model=? AND dimensions=?""",
            (
                _memory().now_iso(), self._query_embedding_sha256(normalized), safe_model,
                int(row["dimensions"]),
            ),
        )
        return vector

    def cache_query_embedding(
        self,
        query: str,
        model: str,
        vector: list[float],
    ) -> None:
        """Persist a bounded semantic-query cache keyed only by a one-way digest."""
        normalized = str(query).strip()
        if not normalized or len(normalized) > _memory().MAX_SEARCH_QUERY_CHARS:
            raise ValueError("Query embedding cache input is empty or too long")
        if _memory().contains_secret(normalized):
            raise ValueError("Potential secret detected; query embedding cache refused")
        safe_model = _memory()._validated_nonsecret_metadata(model, "Embedding model")[:200]
        stored, blob, norm = self._embedding_blob(vector)
        digest = self._query_embedding_sha256(normalized)
        stamp = _memory().now_iso()
        with self._immediate_transaction():
            self.db.execute(
                """INSERT INTO memory_query_embeddings(
                       query_sha256, model, dimensions, embedding_blob,
                       vector_norm, created_at, last_used_at, hit_count
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, 0)
                   ON CONFLICT(query_sha256, model, dimensions) DO UPDATE SET
                       embedding_blob=excluded.embedding_blob,
                       vector_norm=excluded.vector_norm,
                       last_used_at=excluded.last_used_at""",
                (
                    digest, safe_model, len(stored), blob, norm, stamp, stamp,
                ),
            )
            self.db.execute(
                """DELETE FROM memory_query_embeddings
                   WHERE rowid IN (
                       SELECT rowid FROM memory_query_embeddings
                       ORDER BY last_used_at DESC, rowid DESC
                       LIMIT -1 OFFSET ?
                   )""",
                (_memory().MAX_QUERY_EMBEDDING_CACHE,),
            )

    @_with_read_snapshot
    def pending_memory_embeddings(
        self,
        model: str,
        *,
        limit: int = 32,
    ) -> list[dict[str, Any]]:
        """Return a bounded batch whose raw text is already redacted at persistence."""
        safe_model = _memory()._validated_nonsecret_metadata(model, "Embedding model")[:200]
        limit = _memory()._bounded_limit(limit, 64)
        if not limit:
            return []
        rows = self.db.execute(
            f"""SELECT m.id, m.created_at, m.kind, m.content, m.source,
                      c.status AS claim_status, c.authority AS claim_authority,
                      omp.origin AS ordinary_origin,
                      omp.eligible AS ordinary_eligible,
                      omp.content_sha256 AS ordinary_content_sha256,
                      omp.provenance_sha256 AS ordinary_provenance_sha256,
                      {_memory()._LEARNING_QUALITY_SELECT_SQL},
                      e.content_sha256, e.embedding_blob
               FROM memories AS m
               LEFT JOIN memory_embeddings AS e
                 ON e.memory_id=m.id AND e.model=?
               LEFT JOIN memory_claims AS c ON c.memory_id=m.id
               LEFT JOIN ordinary_memory_provenance AS omp ON omp.memory_id=m.id
               {_memory()._LEARNING_QUALITY_ASSESSMENT_JOIN_SQL}
               WHERE m.kind<>'lesson'
                 {_memory()._LEARNING_QUALITY_ALLOWED_SQL}
                 AND (m.kind<>'claim' OR (
                     c.scope='global' AND c.status IN ('active', 'disputed')
                 ))
                 AND (m.kind='claim' OR omp.eligible=1)
               ORDER BY CASE
                            WHEN e.memory_id IS NULL OR e.embedding_blob IS NULL THEN 0
                            ELSE 1
                        END, m.id
               LIMIT ?""",
            (safe_model, min(_memory().MAX_MEMORY_SEARCH_CANDIDATES, max(limit * 8, 64))),
        ).fetchall()
        pending: list[dict[str, Any]] = []
        for row in rows:
            if str(row["kind"]) == "claim":
                snapshot_eligible = self._claim_output_snapshot_safe(row)
                current_eligible = (
                    snapshot_eligible
                    and self._claim_memory_recall_eligible(int(row["id"]))
                )
            else:
                snapshot_eligible = self._ordinary_memory_row_recall_eligible(row)
                current_eligible = (
                    snapshot_eligible
                    and self._ordinary_memory_recall_eligible(int(row["id"]))
                )
            if not current_eligible:
                continue
            content = str(row["content"])
            digest = _memory().hashlib.sha256(content.encode("utf-8")).hexdigest()
            if row["content_sha256"] == digest and row["embedding_blob"] is not None:
                continue
            pending.append({
                "memory_id": int(row["id"]),
                "content": content,
                "content_sha256": digest,
            })
            if len(pending) >= limit:
                break
        return pending

    def claim_pending_memory_embeddings(
        self,
        model: str,
        lease_owner: str,
        *,
        limit: int = 32,
        lease_seconds: int = 300,
    ) -> list[dict[str, Any]]:
        """Atomically lease unindexed records so agents never duplicate API work."""
        safe_model = _memory()._validated_nonsecret_metadata(model, "Embedding model")[:200]
        owner = _memory()._validated_worker_id(lease_owner)
        limit = _memory()._bounded_limit(limit, 64)
        duration = max(30, min(int(lease_seconds), 3_600))
        if not limit:
            return []
        current = _memory()._as_utc()
        expires = (current + _memory().timedelta(seconds=duration)).isoformat()
        selected: list[dict[str, Any]] = []
        with self._immediate_transaction():
            rows = self.db.execute(
                f"""SELECT m.id, m.kind, m.content, e.content_sha256 AS embedded_sha256,
                          e.embedding_blob,
                          l.content_sha256 AS leased_sha256,
                          l.lease_owner, l.lease_expires_at
                   FROM memories AS m
                   LEFT JOIN memory_embeddings AS e
                     ON e.memory_id=m.id AND e.model=?
                   LEFT JOIN memory_embedding_leases AS l
                     ON l.memory_id=m.id AND l.model=?
                   LEFT JOIN memory_claims AS c ON c.memory_id=m.id
                   LEFT JOIN ordinary_memory_provenance AS omp
                     ON omp.memory_id=m.id
                   {_memory()._LEARNING_QUALITY_ASSESSMENT_JOIN_SQL}
                   WHERE m.kind<>'lesson'
                     {_memory()._LEARNING_QUALITY_ALLOWED_SQL}
                     AND (m.kind<>'claim' OR (
                         c.scope='global' AND c.status IN ('active', 'disputed')
                     ))
                     AND (m.kind='claim' OR omp.eligible=1)
                   ORDER BY CASE
                                WHEN e.memory_id IS NULL OR e.embedding_blob IS NULL THEN 0
                                ELSE 1
                            END, m.id
                   LIMIT ?""",
                (
                    safe_model,
                    safe_model,
                    min(_memory().MAX_MEMORY_SEARCH_CANDIDATES, max(limit * 16, 128)),
                ),
            ).fetchall()
            for row in rows:
                if not (
                    self._claim_memory_recall_eligible(int(row["id"]))
                    if str(row["kind"]) == "claim"
                    else self._ordinary_memory_recall_eligible(int(row["id"]))
                ):
                    continue
                content = str(row["content"])
                digest = _memory().hashlib.sha256(content.encode("utf-8")).hexdigest()
                if (
                    str(row["embedded_sha256"] or "") == digest
                    and row["embedding_blob"] is not None
                ):
                    continue
                lease_live = (
                    str(row["leased_sha256"] or "") == digest
                    and str(row["lease_expires_at"] or "") > current.isoformat()
                )
                if lease_live:
                    continue
                self.db.execute(
                    """INSERT INTO memory_embedding_leases(
                           memory_id, model, content_sha256, lease_owner,
                           lease_expires_at, attempt_count, last_error, updated_at
                       ) VALUES (?, ?, ?, ?, ?, 1, NULL, ?)
                       ON CONFLICT(memory_id, model) DO UPDATE SET
                           content_sha256=excluded.content_sha256,
                           lease_owner=excluded.lease_owner,
                           lease_expires_at=excluded.lease_expires_at,
                           attempt_count=CASE
                               WHEN memory_embedding_leases.content_sha256=excluded.content_sha256
                               THEN memory_embedding_leases.attempt_count+1 ELSE 1 END,
                           last_error=NULL, updated_at=excluded.updated_at""",
                    (
                        int(row["id"]), safe_model, digest, owner,
                        expires, current.isoformat(),
                    ),
                )
                selected.append({
                    "memory_id": int(row["id"]),
                    "content": content,
                    "content_sha256": digest,
                })
                if len(selected) >= limit:
                    break
        return selected

    def store_memory_embeddings(
        self,
        model: str,
        records: list[dict[str, Any]],
        vectors: list[list[float]],
        *,
        lease_owner: str | None = None,
    ) -> int:
        """Cache neural indexes only when they still match immutable persisted text."""
        safe_model = _memory()._validated_nonsecret_metadata(model, "Embedding model")[:200]
        owner = None if lease_owner is None else _memory()._validated_worker_id(lease_owner)
        if len(records) != len(vectors) or len(records) > 64:
            raise ValueError("Embedding records and vectors must be equally bounded")
        stored = 0
        stamp = _memory().now_iso()
        with self._immediate_transaction():
            for record, raw_vector in zip(records, vectors, strict=True):
                memory_id = self._prediction_optional_id(
                    record.get("memory_id"), "memory_id"
                )
                row = self.db.execute(
                    "SELECT content, kind FROM memories WHERE id=?", (memory_id,)
                ).fetchone()
                if row is None or str(row["kind"]) == "lesson":
                    continue
                if not (
                    self._claim_memory_recall_eligible(memory_id)
                    if str(row["kind"]) == "claim"
                    else self._ordinary_memory_recall_eligible(memory_id)
                ):
                    continue
                content = str(row["content"])
                digest = _memory().hashlib.sha256(content.encode("utf-8")).hexdigest()
                if digest != str(record.get("content_sha256") or ""):
                    continue
                if owner is not None:
                    lease = self.db.execute(
                        """SELECT 1 FROM memory_embedding_leases
                           WHERE memory_id=? AND model=? AND content_sha256=?
                             AND lease_owner=? AND lease_expires_at>?""",
                        (memory_id, safe_model, digest, owner, stamp),
                    ).fetchone()
                    if lease is None:
                        continue
                vector, blob, norm = self._embedding_blob(raw_vector)
                self.db.execute(
                    """INSERT INTO memory_embeddings(
                           memory_id, model, dimensions, content_sha256,
                           embedding_json, embedding_blob, vector_norm,
                           created_at, updated_at
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(memory_id, model) DO UPDATE SET
                           dimensions=excluded.dimensions,
                           content_sha256=excluded.content_sha256,
                           embedding_json=excluded.embedding_json,
                           embedding_blob=excluded.embedding_blob,
                           vector_norm=excluded.vector_norm,
                           updated_at=excluded.updated_at""",
                    (
                        memory_id, safe_model, len(vector), digest,
                        "[]", blob, norm, stamp, stamp,
                    ),
                )
                self.db.execute(
                    """DELETE FROM memory_embedding_leases
                       WHERE memory_id=? AND model=? AND content_sha256=?
                         AND ? IS NOT NULL AND lease_owner=?""",
                    (memory_id, safe_model, digest, owner, owner),
                )
                stored += 1
        return stored

    def fail_memory_embedding_batch(
        self,
        model: str,
        records: list[dict[str, Any]],
        lease_owner: str,
        error: Any,
        *,
        retry_seconds: int = 60,
    ) -> int:
        safe_model = _memory()._validated_nonsecret_metadata(model, "Embedding model")[:200]
        owner = _memory()._validated_worker_id(lease_owner)
        current = _memory()._as_utc()
        retry_at = (
            current + _memory().timedelta(seconds=max(15, min(int(retry_seconds), 3_600)))
        ).isoformat()
        safe_error = _memory().redact_secrets(str(error))[:1_000]
        changed = 0
        with self._immediate_transaction():
            for record in records[:64]:
                memory_id = self._prediction_optional_id(
                    record.get("memory_id"), "memory_id"
                )
                updated = self.db.execute(
                    """UPDATE memory_embedding_leases
                       SET lease_owner=NULL, lease_expires_at=?, last_error=?, updated_at=?
                       WHERE memory_id=? AND model=? AND content_sha256=?
                         AND lease_owner=?""",
                    (
                        retry_at, safe_error, current.isoformat(), memory_id,
                        safe_model, str(record.get("content_sha256") or ""), owner,
                    ),
                )
                changed += int(updated.rowcount)
        return changed

    @staticmethod
    def _learned_memory_utility(row: dict[str, Any]) -> float:
        resolved = max(0, int(row.get("utility_resolved") or 0))
        observed = float(row.get("utility") or 0.5)
        confidence = min(1.0, resolved / 10.0)
        return 0.5 + (observed - 0.5) * confidence

    @_with_recall_cache
    @_with_read_snapshot
    def semantic_memory_search(
        self,
        query_vector: list[float],
        model: str,
        *,
        limit: int = 12,
        project_id: int | None = None,
    ) -> list[dict[str, Any]]:
        safe_model = _memory()._validated_nonsecret_metadata(model, "Embedding model")[:200]
        query = self._embedding_vector(query_vector)
        limit = _memory()._bounded_limit(limit, 100)
        if not limit:
            return []
        project_scope = self._enabled_project_claim_scope(project_id)
        claim_shadow_sql, claim_shadow_parameters = self._global_claim_shadow_clause(
            project_scope
        )
        query_norm = _memory().math.hypot(*query)
        rows = self.db.execute(
            f"""SELECT m.id, m.created_at, m.kind, m.content, m.source,
                      c.status AS claim_status, c.authority AS claim_authority,
                      omp.origin AS ordinary_origin,
                      omp.eligible AS ordinary_eligible,
                      omp.content_sha256 AS ordinary_content_sha256,
                      omp.provenance_sha256 AS ordinary_provenance_sha256,
                      {_memory()._LEARNING_QUALITY_SELECT_SQL},
                      e.dimensions, e.content_sha256 AS embedding_content_sha256,
                      e.embedding_json, e.embedding_blob, e.vector_norm,
                      COALESCE(s.resolved, 0) AS utility_resolved,
                      COALESCE(s.utility, 0.5) AS utility
               FROM memory_embeddings AS e
               JOIN memories AS m ON m.id=e.memory_id
               LEFT JOIN memory_claims AS c ON c.memory_id=m.id
               LEFT JOIN memory_statistics AS s ON s.memory_id=m.id
               LEFT JOIN ordinary_memory_provenance AS omp ON omp.memory_id=m.id
               {_memory()._LEARNING_QUALITY_ASSESSMENT_JOIN_SQL}
               WHERE e.model=? AND e.dimensions=?
                 AND m.kind<>'lesson'
                 {_memory()._LEARNING_QUALITY_ALLOWED_SQL}
                 AND (m.kind<>'claim' OR (
                     c.scope='global' AND c.status IN ('active', 'disputed')
                     {claim_shadow_sql}
                 ))
                 AND (m.kind='claim' OR omp.eligible=1)
               ORDER BY m.id DESC LIMIT ?""",
            (
                safe_model,
                len(query),
                *claim_shadow_parameters,
                _memory().MAX_MEMORY_SEARCH_CANDIDATES + 1,
            ),
        ).fetchall()
        if len(rows) > _memory().MAX_MEMORY_SEARCH_CANDIDATES:
            # Never rank a truncated recency window: an omitted older vector
            # could be the authoritative or identity-conflicting best match.
            return []
        scored: list[tuple[float, int, dict[str, Any]]] = []
        for raw in rows:
            row = dict(raw)
            content = str(row.get("content") or "")
            embedded_digest = str(row.pop("embedding_content_sha256", "") or "")
            if embedded_digest != _memory().hashlib.sha256(
                content.encode("utf-8")
            ).hexdigest():
                continue
            try:
                vector, stored_norm = self._embedding_from_storage(
                    row.pop("embedding_blob", None),
                    row.pop("embedding_json", "[]"),
                    len(query),
                )
            except ValueError:
                continue
            row.pop("vector_norm", None)
            norm = stored_norm
            if not _memory().math.isfinite(norm) or norm <= 0:
                continue
            # Normalize before multiplication so finite high-magnitude inputs
            # cannot overflow a dot product into inf/NaN and bypass the
            # non-positive similarity check.
            similarity = _memory().math.fsum(
                (left / query_norm) * (right / norm)
                for left, right in zip(query, vector, strict=True)
            )
            if not _memory().math.isfinite(similarity) or similarity <= 0:
                continue
            similarity = min(1.0, similarity)
            utility = self._learned_memory_utility(row)
            adjusted = similarity * (0.9 + 0.2 * utility)
            memory_id = int(row.pop("id"))
            row.pop("dimensions", None)
            row.pop("utility_resolved", None)
            row.pop("utility", None)
            row["memory_id"] = memory_id
            row["semantic_score"] = similarity
            scored.append((adjusted, memory_id, row))
        scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
        # Eligibility is a per-row database check.  Rank first and validate
        # in rank order until the requested count is filled, so a large vector
        # store no longer pays that check for every row it will never return.
        # Ineligible rows are skipped, never returned, exactly as before.
        results: list[dict[str, Any]] = []
        for _adjusted, memory_id, row in scored:
            if len(results) >= limit:
                break
            if str(row["kind"]) == "claim":
                recall_eligible = (
                    self._claim_output_snapshot_safe(row)
                    and self._claim_memory_recall_eligible(memory_id)
                )
            else:
                snapshot = dict(row)
                snapshot["id"] = memory_id
                recall_eligible = (
                    self._ordinary_memory_row_recall_eligible(snapshot)
                    and self._ordinary_memory_recall_eligible(memory_id)
                )
            if not recall_eligible:
                continue
            for field in (
                "ordinary_origin",
                "ordinary_eligible",
                "ordinary_content_sha256",
                "ordinary_provenance_sha256",
                "ordinary_quality_allowed",
                "ordinary_quality_contract_version",
                "ordinary_quality_content_sha256",
                "ordinary_quality_source_is_null",
                "ordinary_quality_source_sha256",
                "ordinary_quality_provenance_sha256",
            ):
                row.pop(field, None)
            results.append(row)
        return results

    @_with_recall_cache
    @_with_read_snapshot
    def hybrid_memory_search(
        self,
        query: str,
        query_vector: list[float],
        model: str,
        *,
        limit: int = 12,
        project_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """Fuse sparse and neural recall; learned utility only adjusts close matches."""
        self._last_recall_report = _memory()._blank_recall_report("screened")
        query = str(query)
        if len(query) > _memory().MAX_SEARCH_QUERY_CHARS:
            raise ValueError(f"Memory search query exceeds {_memory().MAX_SEARCH_QUERY_CHARS} characters")
        limit = _memory()._bounded_limit(limit, 100)
        if (
            not limit
            or _memory().contains_secret(query)
            or _memory().contains_private_identifier(query)
            or _memory()._memory_query_targets_authority_evasion(query)
        ):
            return []
        project_scope = self._enabled_project_claim_scope(project_id)
        claim_shadow_sql, claim_shadow_parameters = self._global_claim_shadow_clause(
            project_scope
        )
        query_terms = _memory()._memory_query_terms(query)
        lexical: list[dict[str, Any]] = []
        identity_terms: list[str] = []
        identity_anchors: list[str] = []
        if not query_terms:
            self._last_recall_report = _memory()._blank_recall_report("empty")
        else:
            candidate_batch = self._lexical_recall_candidates(
                query,
                query_terms,
                claim_shadow_sql=claim_shadow_sql,
                claim_shadow_parameters=claim_shadow_parameters,
                candidate_limit=_memory().MAX_MEMORY_SEARCH_CANDIDATES,
                term_chunk_size=_memory().MAX_MEMORY_QUERY_TERMS,
            )
            if candidate_batch is None:
                return []
            rows, lexical_cache_safe_ids = candidate_batch
            identity_terms, identity_anchors = _memory()._memory_identity_scope(
                query,
                query_terms,
                rows,
                row_cache_allowed=lexical_cache_safe_ids,
            )
            if identity_terms and identity_anchors:
                lexical_evidence = set(_memory()._memory_evidence_terms(
                    query,
                    rows,
                    max_terms=_memory().MAX_MEMORY_QUERY_TERMS,
                    row_cache_allowed=lexical_cache_safe_ids,
                ))
                identity_anchors = [
                    term for term in identity_anchors
                    if term in lexical_evidence
                ]
            lexical_limit = max(limit * 4, 24)
            lexical, shadowed = self._rank_generic_recall_rows(
                list(rows),
                query_terms,
                keep_id=True,
                max_results=lexical_limit,
                cache_safe_ids=lexical_cache_safe_ids,
            )
            if shadowed:
                return [
                    {**item, "retrieval_channel": "lexical"}
                    for item in lexical[:limit]
                ]
            lexical = lexical[:lexical_limit]
        semantic = self.semantic_memory_search(
            query_vector,
            model,
            limit=max(limit * 4, 24),
            project_id=project_id,
        )
        if query_terms:
            identity_safe_semantic: list[dict[str, Any]] = []
            for item in semantic:
                tokens = _memory()._memory_tokens(
                    str(item.get("content") or ""), meaningful_only=False
                )
                matched = [
                    term for term in query_terms
                    if set(_memory()._memory_term_variants(term)).intersection(tokens)
                ]
                identity_bound = all(
                    set(_memory()._memory_term_variants(term)).intersection(tokens)
                    for term in (*identity_terms, *identity_anchors)
                )
                if (
                    identity_bound
                    and not _memory()._memory_identity_conflict(query_terms, tokens, matched)
                ):
                    identity_safe_semantic.append(item)
            semantic = identity_safe_semantic
        fused: dict[int, dict[str, Any]] = {}
        for channel, items in (("lexical", lexical), ("semantic", semantic)):
            for rank, item in enumerate(items, 1):
                memory_id = int(item["memory_id"])
                entry = fused.setdefault(memory_id, {
                    "item": item,
                    "score": 0.0,
                    "channels": set(),
                })
                entry["score"] += 1.0 / (60.0 + rank)
                entry["channels"].add(channel)
                if channel == "lexical":
                    entry["item"] = item
        if fused:
            memory_ids = sorted(fused)
            placeholders = ",".join("?" for _ in memory_ids)
            statistics = self.db.execute(
                f"""SELECT memory_id, resolved AS utility_resolved, utility
                    FROM memory_statistics WHERE memory_id IN ({placeholders})""",
                memory_ids,
            ).fetchall()
            for raw in statistics:
                row = dict(raw)
                memory_id = int(row["memory_id"])
                # Learned utility can reorder close results, but it is too
                # weak to overpower strong lexical/semantic relevance.
                fused[memory_id]["score"] += (
                    self._learned_memory_utility(row) - 0.5
                ) * 0.004
        ranked = sorted(
            fused.values(),
            key=lambda entry: (entry["score"], int(entry["item"]["memory_id"])),
            reverse=True,
        )
        results: list[dict[str, Any]] = []
        for entry in ranked[:limit]:
            item = dict(entry["item"])
            item.pop("semantic_score", None)
            channels = entry["channels"]
            item["retrieval_channel"] = "hybrid" if len(channels) > 1 else next(iter(channels))
            results.append(item)
        return results

    def record_memory_retrievals(
        self,
        prediction_id: int,
        family: str,
        query: str,
        memories: list[dict[str, Any]],
        *,
        conversation_id: int | None = None,
    ) -> int:
        """Persist outcome-linkable retrieval evidence without persisting the raw query."""
        normalized_prediction = self._prediction_optional_id(
            prediction_id, "prediction_id"
        )
        if family not in self.PREDICTION_FAMILIES:
            raise ValueError(f"Unknown memory retrieval family: {family}")
        normalized_conversation = self._prediction_optional_id(
            conversation_id, "conversation_id"
        )
        fingerprint = _memory().hashlib.sha256(
            ("jarvis-memory-query-v1\0" + str(query)).encode("utf-8")
        ).hexdigest()
        stamp = _memory().now_iso()
        inserted = 0
        with self._immediate_transaction():
            prediction = self.db.execute(
                """SELECT family, resolved_at, task_id, conversation_id
                   FROM task_predictions WHERE id=?""",
                (normalized_prediction,),
            ).fetchone()
            if (
                prediction is None
                or prediction["family"] != family
                or prediction["resolved_at"] is not None
            ):
                raise ValueError("Memory retrieval must bind to the active matching prediction")
            for rank, item in enumerate(memories[:10], 1):
                memory_id = self._prediction_optional_id(
                    item.get("memory_id"), "memory_id"
                )
                channel = str(item.get("retrieval_channel") or "lexical")
                if channel not in {"lexical", "semantic", "hybrid"}:
                    raise ValueError("Unknown memory retrieval channel")
                valid = self.db.execute(
                    "SELECT kind FROM memories WHERE id=?", (memory_id,)
                ).fetchone()
                if valid is None:
                    raise ValueError("Memory retrieval references a missing memory")
                if str(valid["kind"]) == "lesson":
                    raise ValueError(
                        "Verified lessons require the dedicated provenance retrieval path"
                    )
                if not (
                    self._claim_memory_recall_eligible(memory_id)
                    if str(valid["kind"]) == "claim"
                    else self._ordinary_memory_recall_eligible(memory_id)
                ):
                    raise ValueError(
                        "Memory retrieval references an ineligible ordinary memory"
                    )
                cursor = self.db.execute(
                    """INSERT OR IGNORE INTO memory_retrievals(
                           created_at, prediction_id, conversation_id, family,
                           query_sha256, memory_id, rank, channel
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        stamp, normalized_prediction, normalized_conversation,
                        family, fingerprint, memory_id, rank, channel,
                    ),
                )
                if cursor.rowcount == 1:
                    self.db.execute(
                        """INSERT INTO memory_statistics(
                               memory_id, retrievals, updated_at, last_retrieved_at
                           ) VALUES (?, 1, ?, ?)
                           ON CONFLICT(memory_id) DO UPDATE SET
                               retrievals=retrievals+1,
                               updated_at=excluded.updated_at,
                               last_retrieved_at=excluded.last_retrieved_at""",
                        (memory_id, stamp, stamp),
                    )
                    inserted += 1
        return inserted

    def memory_quality(self, limit: int = 20) -> dict[str, Any]:
        """Expose measured recall coverage and utility without model-authored claims."""
        limit = _memory()._bounded_limit(limit, 100)
        totals = self.db.execute(
            """SELECT COUNT(*) AS memories,
                      (SELECT COUNT(*) FROM memories AS em
                       LEFT JOIN memory_claims AS ec ON ec.memory_id=em.id
                       WHERE em.kind<>'lesson'
                         AND (em.kind<>'claim' OR (
                             ec.scope='global'
                             AND ec.status IN ('active','disputed')
                         )))
                          AS embedding_eligible,
                      (SELECT COUNT(*) FROM memory_embeddings) AS embeddings,
                      (SELECT COUNT(*) FROM memory_embeddings
                       WHERE embedding_blob IS NOT NULL) AS binary_embeddings,
                      (SELECT COUNT(*) FROM memory_embedding_leases
                       WHERE lease_owner IS NOT NULL
                         AND lease_expires_at>strftime('%Y-%m-%dT%H:%M:%f+00:00','now'))
                          AS active_embedding_leases,
                      (SELECT COUNT(*) FROM memory_embedding_leases
                       WHERE last_error IS NOT NULL) AS embedding_failures,
                      (SELECT COUNT(*) FROM memory_query_embeddings)
                          AS cached_query_embeddings,
                      (SELECT COALESCE(SUM(hit_count), 0)
                       FROM memory_query_embeddings) AS query_embedding_cache_hits,
                      (SELECT COUNT(*) FROM memory_retrievals) AS retrievals,
                      (SELECT COUNT(*) FROM memory_retrievals WHERE resolved_at IS NOT NULL)
                          AS resolved_retrievals,
                      (SELECT AVG(successful) FROM memory_retrievals
                       WHERE resolved_at IS NOT NULL) AS observed_utility,
                      (SELECT COUNT(*) FROM memory_claims
                       WHERE status='active') AS active_claims,
                      (SELECT COUNT(*) FROM memory_claims
                       WHERE status='disputed') AS disputed_claims,
                      (SELECT COUNT(*) FROM memory_claims
                       WHERE status='superseded') AS superseded_claims,
                      (SELECT COUNT(*) FROM memory_claim_events) AS claim_events
                      ,(SELECT COUNT(*) FROM memory_claim_observations)
                          AS claim_observations
                      ,(SELECT COUNT(*) FROM memory_claim_volatility)
                          AS claim_clock_predicates
                      ,(SELECT COUNT(*) FROM memory_claim_volatility
                        WHERE pair_count>=6) AS claim_clock_mature_predicates
                      ,(SELECT COALESCE(SUM(reads), 0)
                        FROM memory_claim_clock_statistics) AS claim_clock_reads
                      ,(SELECT COALESCE(SUM(stale_reads), 0)
                        FROM memory_claim_clock_statistics) AS claim_clock_stale_reads
               FROM memories"""
        ).fetchone()
        top = self.db.execute(
            """SELECT s.memory_id, m.kind,
                      s.retrievals, s.resolved, s.successes, s.failures, s.utility,
                      s.last_retrieved_at, s.last_resolved_at
               FROM memory_statistics AS s
               JOIN memories AS m ON m.id=s.memory_id
               ORDER BY s.resolved DESC, s.utility DESC, s.memory_id DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        lesson_ids = {
            int(row["id"])
            for row in self.db.execute(
                "SELECT id FROM memories WHERE kind='lesson' ORDER BY id"
            ).fetchall()
        }
        valid_lesson_ids: set[int] = set()
        eligible_lesson_ids: set[int] = set()
        hash_mismatch_ids: set[int] = set()
        digest_mismatch_ids: set[int] = set()
        lesson_control_reasons: _memory().Counter[str] = _memory().Counter()
        for memory_id in lesson_ids:
            valid, content_mismatch, digest_mismatch = (
                self._lesson_provenance_validation(memory_id)
            )
            if valid:
                valid_lesson_ids.add(memory_id)
            if content_mismatch:
                hash_mismatch_ids.add(memory_id)
            if digest_mismatch:
                digest_mismatch_ids.add(memory_id)
            control_valid, control_reason = self._lesson_control_validation(memory_id)
            if valid and control_valid:
                eligible_lesson_ids.add(memory_id)
            else:
                lesson_control_reasons[control_reason] += 1
        ordinary_ids = {
            int(row["id"])
            for row in self.db.execute(
                """SELECT id FROM memories
                   WHERE kind NOT IN ('lesson', 'claim') ORDER BY id"""
            ).fetchall()
        }
        valid_ordinary_ids: set[int] = set()
        eligible_ordinary_ids: set[int] = set()
        ordinary_hash_mismatch_ids: set[int] = set()
        ordinary_digest_mismatch_ids: set[int] = set()
        for memory_id in ordinary_ids:
            valid, eligible, content_mismatch, provenance_mismatch = (
                self._ordinary_memory_provenance_validation(memory_id)
            )
            if valid:
                valid_ordinary_ids.add(memory_id)
            if valid and eligible:
                eligible_ordinary_ids.add(memory_id)
            if content_mismatch:
                ordinary_hash_mismatch_ids.add(memory_id)
            if provenance_mismatch:
                ordinary_digest_mismatch_ids.add(memory_id)
        # Project-scoped claims are selected through the typed claim lane and
        # deliberately never enter the generic embedding/indexing pipeline.
        # Keep this metric aligned with the records that can actually be
        # embedded rather than counting every active claim in every project.
        active_claim_count = int(
            self.db.execute(
                """SELECT COUNT(*) FROM memory_claims
                   WHERE scope='global' AND status IN ('active','disputed')"""
            ).fetchone()[0]
            or 0
        )
        measured_totals = dict(totals)
        measured_totals.update({
            "embedding_eligible": len(eligible_ordinary_ids) + active_claim_count,
            "ordinary_memory_records": len(ordinary_ids),
            "ordinary_memory_provenance_valid": len(valid_ordinary_ids),
            "ordinary_memory_recall_eligible": len(eligible_ordinary_ids),
            "ordinary_memory_quarantined": len(ordinary_ids - eligible_ordinary_ids),
            "ordinary_memory_hash_mismatches": len(ordinary_hash_mismatch_ids),
            "ordinary_memory_digest_mismatches": len(ordinary_digest_mismatch_ids),
            "structured_lessons": len(lesson_ids),
            "lesson_records": len(lesson_ids),
            "provenance_valid_lessons": len(valid_lesson_ids),
            "provenance_quarantined_lessons": len(lesson_ids - valid_lesson_ids),
            "provenance_hash_mismatches": len(hash_mismatch_ids),
            "provenance_digest_mismatches": len(digest_mismatch_ids),
            "lesson_recall_eligible": len(eligible_lesson_ids),
            "lesson_control_quarantined": len(lesson_ids - eligible_lesson_ids),
            "lesson_expired": lesson_control_reasons["expired"],
            "lesson_contradicted": lesson_control_reasons["contradicted"],
            "lesson_superseded": lesson_control_reasons["superseded"],
            "lesson_control_digest_mismatches": lesson_control_reasons[
                "control_digest_mismatch"
            ],
        })
        return {
            "totals": measured_totals,
            "measured_memories": [
                dict(row)
                for row in top
                if str(row["kind"]) == "claim"
                or int(row["memory_id"]) in eligible_ordinary_ids
            ],
        }
