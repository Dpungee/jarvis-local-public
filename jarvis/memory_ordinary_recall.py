"""Current Memory domain extracted without changing method control flow.

Global dependencies resolve through jarvis.memory at call time. The facade keeps
public imports, patch seams, constants and authorization helpers authoritative.
"""
from __future__ import annotations

from .vault import VaultNote
from collections.abc import Mapping
from collections.abc import Sequence
from pathlib import Path
from typing import Any
import sqlite3
from .memory_runtime import (_memory, _with_read_snapshot, _with_recall_cache)


class OrdinaryRecallMemoryMixin:
    """Mechanically extracted current Memory methods."""

    def configure_vault(self, vault_dir: Path | None) -> None:
        """Attach the optional human-readable mirror without changing SQLite authority."""
        self.vault = _memory().Vault(vault_dir)

    def begin_vault_task(self) -> None:
        if self.vault is not None:
            self.vault.begin_task()

    def _mirror_vault_note(
        self,
        kind: str,
        title: str,
        body: str,
        *,
        tags: tuple[str, ...] = (),
        links: tuple[str, ...] = (),
        source: str | None = None,
    ) -> None:
        if self.vault is None or not self.vault.enabled:
            return
        try:
            self.vault.write_note(
                kind, title, body, tags=tags, links=links, source=source
            )
        except Exception:
            # The Obsidian vault is a derived convenience mirror. A broken or
            # unavailable mirror must never roll back the canonical SQLite row.
            return

    def sync_vault_notes(
        self,
        notes: list[VaultNote],
        *,
        actor: str = "runtime",
        permission: str = "runtime:indexer",
        conversation_id: int | None = None,
    ) -> dict[str, int]:
        """Synchronize derived vault search records into canonical memory indexing.

        Every change is receipted on the memory spine: a new record appends
        ``memory.created`` before its row, a changed record ``memory.updated``
        after it (after-image digest), and the stale records one
        ``memory.deleted`` listing their ids and digests, never content.  An
        unchanged record appends nothing: the indexer loops re-run this on
        every batch.  Those loops are ``runtime`` / ``runtime:indexer`` (the
        defaults); the chat verb passes ``operator`` / ``operator:interactive``.
        """
        context = self._spine_context(actor, conversation_id, permission)
        desired = {
            f"vault:{note.kind}:"
            f"{_memory().hashlib.sha256(note.relative_path.encode('utf-8')).hexdigest()}": (
                f"Vault note: {note.title}\n{note.search_text}".strip()
            )
            for note in notes
            # Lesson notes are a human-readable mirror of canonical lesson rows.
            # Re-importing them as ordinary ``vault`` memories would bypass the
            # provenance, family, outcome, and calibration checks in
            # ``match_lessons``. Human-edited lesson notes must remain inert too.
            if str(note.kind).casefold() != "lessons"
        }
        inserted = 0
        updated = 0
        removed = 0
        with self._immediate_transaction():
            existing = {
                str(row["source"]): (int(row["id"]), str(row["content"]))
                for row in self.db.execute(
                    "SELECT id, source, content FROM memories WHERE kind='vault'"
                ).fetchall()
                if row["source"] is not None
            }
            stamp = _memory().now_iso()
            stale = [
                (memory_id, content)
                for source, (memory_id, content) in existing.items()
                if source not in desired
            ]
            stale_ids = [memory_id for memory_id, _content in stale]
            if stale_ids:
                placeholders = ",".join("?" for _ in stale_ids)
                for table in (
                    "memory_retrievals",
                    "memory_statistics",
                    "memory_embeddings",
                    "memory_embedding_leases",
                    "ordinary_memory_provenance",
                ):
                    self.db.execute(
                        f"DELETE FROM {table} WHERE memory_id IN ({placeholders})",
                        stale_ids,
                    )
                self.db.execute(
                    f"DELETE FROM memories WHERE id IN ({placeholders})", stale_ids
                )
                removed = len(stale_ids)
                if self._spine_ready:
                    # Deletion receipts carry ids and digests only (design
                    # 12.6 item 2), in bounded chunks: one receipt for a large
                    # removal would exceed the payload cap and roll the whole
                    # pass back, so the stale rows would never go away.  The
                    # ids never come back (memory_id_sequence).
                    chunk_size = max(
                        1, int(getattr(_memory().memory_spine, "MEMORY_DELETED_MAX_IDS", 128))
                    )
                    for offset in range(0, len(stale), chunk_size):
                        chunk = stale[offset:offset + chunk_size]
                        self._append_memory_event(
                            "memory.deleted",
                            memory_id=chunk[-1][0],
                            payload=_memory().memory_spine.memory_deleted_payload(
                                self._spine_key, chunk, reason="vault re-index"
                            ),
                            stamp=stamp,
                            source="vault re-index",
                            context=context,
                        )
            for source, content in desired.items():
                current = existing.get(source)
                fields = {
                    "kind": "vault", "content": content, "source": source,
                    "family": None, "outcome_status": None, "reflection_id": None,
                }
                if current is None:
                    if self._spine_ready:
                        memory_id = _memory().memory_spine.allocate_memory_id(self.db)
                        event_id = self._append_memory_event(
                            "memory.created",
                            memory_id=memory_id,
                            payload=_memory().memory_spine.memory_event_payload(
                                self._spine_key, fields,
                                origin="verified_vault_note", eligible=True,
                            ),
                            stamp=stamp,
                            source="vault re-index",
                            context=context,
                        )
                        self.db.execute(
                            """INSERT INTO memories(
                                   id, created_at, kind, content, source, spine_event_id
                               ) VALUES (?, ?, 'vault', ?, ?, ?)""",
                            (memory_id, stamp, content, source, event_id),
                        )
                    else:
                        cursor = self.db.execute(
                            """INSERT INTO memories(created_at, kind, content, source)
                               VALUES (?, 'vault', ?, ?)""",
                            (stamp, content, source),
                        )
                        memory_id = int(cursor.lastrowid)
                    self._set_ordinary_memory_provenance_locked(
                        memory_id,
                        origin="verified_vault_note",
                        eligible=True,
                    )
                    inserted += 1
                elif current[1] != content:
                    self.db.execute(
                        "UPDATE memories SET content=? WHERE id=?",
                        (content, current[0]),
                    )
                    self._set_ordinary_memory_provenance_locked(
                        int(current[0]),
                        origin="verified_vault_note",
                        eligible=True,
                    )
                    if self._spine_ready:
                        self._append_memory_event(
                            "memory.updated",
                            memory_id=int(current[0]),
                            payload=_memory().memory_spine.memory_event_payload(
                                self._spine_key, fields,
                                origin="verified_vault_note", eligible=True,
                            ),
                            stamp=stamp,
                            source="vault re-index",
                            context=context,
                        )
                    updated += 1
                else:
                    self._set_ordinary_memory_provenance_locked(
                        int(current[0]),
                        origin="verified_vault_note",
                        eligible=True,
                    )
        if inserted or updated or removed:
            # Clear only after the transaction commits.  Updated/deleted note
            # text may otherwise remain in derived token/signature values even
            # though SQLite already exposes the replacement corpus.
            self._recall_cache.clear()
        return {
            "notes": len(desired),
            "inserted": inserted,
            "updated": updated,
            "removed": removed,
        }

    def vault_index_status(
        self, notes: list[VaultNote], *, model: str | None = None
    ) -> dict[str, int | bool]:
        desired = {
            f"vault:{note.kind}:"
            f"{_memory().hashlib.sha256(note.relative_path.encode('utf-8')).hexdigest()}":
            f"Vault note: {note.title}\n{note.search_text}".strip()
            for note in notes
            if str(note.kind).casefold() != "lessons"
        }
        existing = {
            str(row["source"]): str(row["content"])
            for row in self.db.execute(
                "SELECT source, content FROM memories WHERE kind='vault'"
            ).fetchall()
            if row["source"] is not None
        }
        semantic_indexed = 0
        if model:
            for source, content in desired.items():
                row = self.db.execute(
                    """SELECT e.content_sha256
                       FROM memories AS m
                       JOIN memory_embeddings AS e ON e.memory_id=m.id
                       WHERE m.kind='vault' AND m.source=? AND e.model=?""",
                    (source, str(model)),
                ).fetchone()
                if row is not None and str(row["content_sha256"]) == _memory().hashlib.sha256(
                    content.encode("utf-8")
                ).hexdigest():
                    semantic_indexed += 1
        records_fresh = desired == existing
        semantic_fresh = model is None or semantic_indexed == len(desired)
        return {
            "notes": len(desired),
            "indexed": len(existing),
            "semantic_indexed": semantic_indexed,
            "fresh": records_fresh and semantic_fresh,
        }

    @staticmethod
    def _ordinary_memory_provenance_digest(material: dict[str, Any]) -> str:
        return _memory()._ordinary_memory_provenance_digest_from_material(material)

    def _ordinary_memory_provenance_material(
        self,
        memory_id: int,
        *,
        origin: str,
        eligible: bool,
    ) -> dict[str, Any] | None:
        row = self.db.execute(
            """SELECT id, created_at, kind, content, source
               FROM memories WHERE id=?""",
            (int(memory_id),),
        ).fetchone()
        if row is None or str(row["kind"]) in {"lesson", "claim"}:
            return None
        return _memory()._ordinary_memory_provenance_material_from_fields(
            int(row["id"]),
            str(row["created_at"]),
            str(row["kind"]),
            str(row["content"]),
            None if row["source"] is None else str(row["source"]),
            origin=str(origin),
            eligible=bool(eligible),
        )

    def _set_ordinary_memory_provenance_locked(
        self,
        memory_id: int,
        *,
        origin: str,
        eligible: bool,
    ) -> None:
        normalized_origin = _memory()._validated_nonsecret_metadata(
            origin, "Memory provenance origin"
        )[:80]
        if eligible and normalized_origin not in self.ORDINARY_MEMORY_PROVENANCE_ORIGINS:
            raise ValueError("Unknown trusted memory provenance origin")
        if not eligible and normalized_origin != "unverified":
            raise ValueError("Ineligible memory provenance must be unverified")
        material = self._ordinary_memory_provenance_material(
            int(memory_id), origin=normalized_origin, eligible=bool(eligible)
        )
        if material is None:
            raise ValueError("Ordinary memory provenance cannot bind this record")
        content = str(material["memory"]["content"])
        self.db.execute(
            """INSERT INTO ordinary_memory_provenance(
                   memory_id, recorded_at, origin, eligible,
                   content_sha256, provenance_sha256
               ) VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(memory_id) DO UPDATE SET
                   recorded_at=excluded.recorded_at,
                   origin=excluded.origin,
                   eligible=excluded.eligible,
                   content_sha256=excluded.content_sha256,
                   provenance_sha256=excluded.provenance_sha256""",
            (
                int(memory_id),
                _memory().now_iso(),
                normalized_origin,
                int(bool(eligible)),
                _memory().hashlib.sha256(content.encode("utf-8")).hexdigest(),
                self._ordinary_memory_provenance_digest(material),
            ),
        )

    def _ordinary_memory_provenance_validation(
        self,
        memory_id: int,
    ) -> tuple[bool, bool, bool, bool]:
        """Return (valid, eligible, content mismatch, provenance mismatch)."""
        row = self.db.execute(
            """SELECT memory_id, origin, eligible, content_sha256,
                      provenance_sha256
               FROM ordinary_memory_provenance WHERE memory_id=?""",
            (int(memory_id),),
        ).fetchone()
        memory = self.db.execute(
            "SELECT content, kind FROM memories WHERE id=?",
            (int(memory_id),),
        ).fetchone()
        if memory is None or str(memory["kind"]) in {"lesson", "claim"}:
            return False, False, False, row is not None
        if row is None:
            return False, False, False, True
        eligible = bool(int(row["eligible"]))
        origin = str(row["origin"])
        content_hash = _memory().hashlib.sha256(
            str(memory["content"]).encode("utf-8")
        ).hexdigest()
        content_mismatch = str(row["content_sha256"] or "") != content_hash
        material = self._ordinary_memory_provenance_material(
            int(memory_id), origin=origin, eligible=eligible
        )
        expected = (
            None
            if material is None
            else self._ordinary_memory_provenance_digest(material)
        )
        provenance_mismatch = (
            expected is None
            or str(row["provenance_sha256"] or "") != expected
            or (eligible and origin not in self.ORDINARY_MEMORY_PROVENANCE_ORIGINS)
            or (not eligible and origin != "unverified")
        )
        return (
            not content_mismatch and not provenance_mismatch,
            eligible,
            content_mismatch,
            provenance_mismatch,
        )

    def _ordinary_memory_row_recall_eligible(
        self,
        row: Mapping[str, Any],
    ) -> bool:
        """Validate one joined ordinary-memory snapshot without another SQL read.

        The cache key contains only hashes plus non-sensitive provenance
        metadata; its value is one boolean. Any out-of-band field mutation
        changes the key without retaining invalid/private raw material.
        """
        try:
            memory_id = int(row["id"])
            created_at = str(row["created_at"])
            kind = str(row["kind"])
            content = str(row["content"])
            source = None if row["source"] is None else str(row["source"])
            origin = str(row["ordinary_origin"])
            eligible = bool(int(row["ordinary_eligible"]))
            stored_content_sha256 = str(
                row["ordinary_content_sha256"] or ""
            )
            stored_provenance_sha256 = str(
                row["ordinary_provenance_sha256"] or ""
            )
            actual_content_sha256 = _memory().hashlib.sha256(
                content.encode("utf-8")
            ).hexdigest()
            actual_source_sha256 = _memory().hashlib.sha256(
                (source or "").encode("utf-8")
            ).hexdigest()
            quality_binding: tuple[Any, ...] | None = None
            if kind.casefold() == "learning":
                quality_allowed_raw = row["ordinary_quality_allowed"]
                quality_contract_raw = row["ordinary_quality_contract_version"]
                quality_content_sha256 = str(
                    row["ordinary_quality_content_sha256"] or ""
                )
                quality_source_is_null_raw = row[
                    "ordinary_quality_source_is_null"
                ]
                quality_source_sha256 = str(
                    row["ordinary_quality_source_sha256"] or ""
                )
                quality_provenance_sha256 = str(
                    row["ordinary_quality_provenance_sha256"] or ""
                )
                quality_binding = (
                    quality_allowed_raw,
                    quality_contract_raw,
                    quality_content_sha256,
                    quality_source_is_null_raw,
                    quality_source_sha256,
                    quality_provenance_sha256,
                )
        except (KeyError, TypeError, ValueError, OverflowError):
            return False
        cache = _memory()._ACTIVE_RECALL_CACHE.get()
        cache_key: tuple[Any, ...] | None = None
        cached: Any | None = None
        if cache is not None:
            cache_key = (
                "ordinary-eligibility",
                memory_id,
                _memory()._ordinary_cache_field_digest(created_at),
                _memory()._ordinary_cache_field_digest(kind),
                _memory()._ordinary_cache_field_digest(origin),
                eligible,
                source is None,
                bytes.fromhex(actual_content_sha256),
                bytes.fromhex(actual_source_sha256),
                _memory()._ordinary_cache_field_digest(stored_content_sha256),
                _memory()._ordinary_cache_field_digest(stored_provenance_sha256),
                None if quality_binding is None else tuple(
                    _memory()._ordinary_cache_field_digest(value)
                    for value in quality_binding
                ),
            )
            cached = cache.get(cache_key)
        if cached is not None:
            return bool(cached)
        try:
            valid = (
                kind not in {"lesson", "claim"}
                and eligible
                and origin in self.ORDINARY_MEMORY_PROVENANCE_ORIGINS
                and stored_content_sha256 == actual_content_sha256
            )
            if valid:
                material = _memory()._ordinary_memory_provenance_material_from_fields(
                    memory_id,
                    created_at,
                    kind,
                    content,
                    source,
                    origin=origin,
                    eligible=True,
                )
                valid = stored_provenance_sha256 == (
                    _memory()._ordinary_memory_provenance_digest_from_material(material)
                )
            if valid:
                recall_material = "\n".join((content, source or "", kind))
                valid = not (
                    _memory().contains_secret(recall_material)
                    or _memory().contains_private_identifier(recall_material)
                )
            if valid and kind.casefold() == "learning":
                if quality_binding is None:
                    valid = False
                else:
                    (
                        quality_allowed_raw,
                        quality_contract_raw,
                        quality_content_sha256,
                        quality_source_is_null_raw,
                        quality_source_sha256,
                        quality_provenance_sha256,
                    ) = quality_binding
                    valid = (
                        quality_allowed_raw is not None
                        and int(quality_allowed_raw) == 1
                        and int(quality_contract_raw)
                        == _memory().TRAINING_QUALITY_CONTRACT_VERSION
                        and quality_content_sha256 == actual_content_sha256
                        and int(quality_source_is_null_raw)
                        == int(source is None)
                        and quality_source_sha256 == actual_source_sha256
                        and quality_provenance_sha256
                        == stored_provenance_sha256
                        and _memory().learning_memory_record_allowed(
                            content=content,
                            source=source or "",
                        )
                    )
        except (TypeError, ValueError, OverflowError):
            valid = False
        if cache is not None and cache_key is not None:
            cache.put(cache_key, bool(valid), 1)
        return bool(valid)

    def _ordinary_recall_cache_safe_ids(
        self,
        rows: Sequence[Mapping[str, Any]],
    ) -> frozenset[int]:
        """Return IDs whose joined snapshot may populate derived text caches."""
        safe: set[int] = set()
        for row in rows:
            if self._ordinary_memory_row_recall_eligible(row):
                try:
                    safe.add(int(row["id"]))
                except (KeyError, TypeError, ValueError):
                    continue
        return frozenset(safe)

    def _ordinary_memory_recall_eligible(self, memory_id: int) -> bool:
        row = self.db.execute(
            f"""SELECT m.id, m.created_at, m.kind, m.content, m.source,
                      omp.origin AS ordinary_origin,
                      omp.eligible AS ordinary_eligible,
                      omp.content_sha256 AS ordinary_content_sha256,
                      omp.provenance_sha256 AS ordinary_provenance_sha256,
                      {_memory()._LEARNING_QUALITY_SELECT_SQL}
               FROM memories AS m
               LEFT JOIN ordinary_memory_provenance AS omp ON omp.memory_id=m.id
               {_memory()._LEARNING_QUALITY_ASSESSMENT_JOIN_SQL}
               WHERE m.id=?""",
            (int(memory_id),),
        ).fetchone()
        return bool(
            row is not None and self._ordinary_memory_row_recall_eligible(row)
        )

    def _filter_generic_recall_rows(
        self,
        rows: list[sqlite3.Row],
    ) -> list[sqlite3.Row]:
        """Keep active claims and canonically verified ordinary memories only."""
        return [
            row
            for row in rows
            if (
                self._claim_memory_recall_eligible(int(row["id"]))
                if str(row["kind"]) == "claim"
                else self._ordinary_memory_recall_eligible(int(row["id"]))
            )
        ]

    def _rank_generic_recall_rows(
        self,
        rows: list[sqlite3.Row],
        query_terms: list[str],
        *,
        keep_id: bool,
        max_results: int,
        minimum_information_coverage: float = 0.0,
        relative_match_floor: float = 0.0,
        relative_information_floor: float = 0.0,
        query_text: str | None = None,
        cache_safe_ids: frozenset[int] | None = None,
    ) -> tuple[list[dict[str, Any]], bool]:
        """Rank all candidates before filtering provenance, and abstain on shadowing.

        Filtering unverified ordinary memories in SQL can make a weaker verified
        record look like the best answer to a query that actually targets an
        unverified or tampered record. Rank the bounded candidate set first. If
        its strongest lexical match is not recall-eligible, fail closed instead
        of silently substituting different content.
        """
        if cache_safe_ids is None:
            cache_safe_ids = self._ordinary_recall_cache_safe_ids(rows)
        rank_arguments = {
            "keep_id": True,
            "minimum_information_coverage": minimum_information_coverage,
            "relative_match_floor": relative_match_floor,
            "relative_information_floor": relative_information_floor,
            "query_text": query_text,
            # Raw candidates intentionally retain ineligible and tampered hard
            # shadows. Only digest-validated, privacy-clean ordinary snapshots
            # may populate text/signature caches before final eligibility.
            "row_cache_allowed": cache_safe_ids,
        }
        ranked = _memory()._rank_memory_rows(
            rows,
            query_terms,
            identity_conflict_shadow=True,
            **rank_arguments,
        )
        if not ranked and _memory()._rank_memory_rows(
            rows,
            query_terms,
            identity_conflict_shadow=False,
            **rank_arguments,
        ):
            # An identity-conflicted top lexical result is a hard shadow.  Tell
            # hybrid retrieval not to reintroduce it through vector similarity.
            return [], True
        if not ranked:
            return [], False
        eligible: list[dict[str, Any]] = []
        shadowed = False
        for item in ranked:
            if len(eligible) >= max_results:
                break
            try:
                if str(item["kind"]) == "claim":
                    recall_eligible = (
                        self._claim_output_snapshot_safe(item)
                        and self._claim_memory_recall_eligible(
                            int(item["memory_id"])
                        )
                    )
                else:
                    snapshot = dict(item)
                    snapshot["id"] = int(item["memory_id"])
                    recall_eligible = (
                        self._ordinary_memory_row_recall_eligible(snapshot)
                        and self._ordinary_memory_recall_eligible(
                            int(item["memory_id"])
                        )
                    )
            except _memory().sqlite3.DatabaseError:
                return [], True
            if not recall_eligible:
                # Never jump over a stronger ineligible observation to return a
                # weaker answer. A stronger verified prefix remains usable.
                shadowed = True
                break
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
                item.pop(field, None)
            eligible.append(item)
        if not keep_id:
            for item in eligible:
                item.pop("memory_id", None)
        return eligible, shadowed

    def _generic_recall_query_rows(
        self,
        sql: str,
        parameters: list[Any] | tuple[Any, ...],
    ) -> list[sqlite3.Row] | None:
        """Execute one bounded recall query, returning ``None`` on DB failure."""
        try:
            return list(self.db.execute(sql, parameters).fetchall())
        except _memory().sqlite3.DatabaseError:
            return None

    def _lexical_recall_candidates(
        self,
        query: str,
        discovery_terms: list[str],
        *,
        claim_shadow_sql: str,
        claim_shadow_parameters: tuple[Any, ...] | list[Any],
        candidate_limit: int,
        term_chunk_size: int,
    ) -> tuple[list[sqlite3.Row], frozenset[int]] | None:
        """Discover a bounded lexical candidate pool without silent scale failure.

        Discovery starts with a bounded OR pool. When the query contains a
        structural/proper subject identity, that pool is replaced at every
        corpus size by rows proving the identity and supported fact anchors;
        an absent or overflowing association abstains instead of returning a
        generic fact. Natural word length is never treated as identity.

        Generic overflow narrows in fixed stages: one batched frequency query,
        OR of terms that can discriminate inside the visible scope, then an
        all-term intersection. Frequency and quality membership use the same
        project, claim-shadow, and learning-quarantine filters as discovery, so
        hidden/quarantined rows neither consume the cap nor change observable
        selection. Wildcard input retains the bounded LIKE path.

        Returns rows plus IDs whose joined provenance/privacy snapshot permits
        derived text caching. ``None`` denotes a database failure, final
        overflow, or unproved identity association.
        """
        report = _memory()._blank_recall_report(
            "empty",
            candidate_limit=candidate_limit,
            discovery_terms=len(discovery_terms),
        )
        self._last_recall_report = report
        visible_sql = f"""FROM memory_fts
               JOIN memories AS m ON m.id=memory_fts.rowid
               LEFT JOIN memory_claims AS c ON c.memory_id=m.id
               LEFT JOIN ordinary_memory_provenance AS omp ON omp.memory_id=m.id
               {_memory()._LEARNING_QUALITY_ASSESSMENT_JOIN_SQL}
               WHERE memory_fts MATCH ?
                 AND m.kind<>'lesson'
                 {_memory()._LEARNING_QUALITY_LEXICAL_SQL}
                 AND (m.kind<>'claim' OR (
                     c.scope='global' AND c.status IN ('active', 'disputed')
                     {claim_shadow_sql}
                 ))"""
        select_sql = f"""SELECT m.id, m.created_at, m.kind, m.content, m.source,
                      c.status AS claim_status, c.authority AS claim_authority,
                      omp.origin AS ordinary_origin,
                      omp.eligible AS ordinary_eligible,
                      omp.content_sha256 AS ordinary_content_sha256,
                      omp.provenance_sha256 AS ordinary_provenance_sha256,
                      {_memory()._LEARNING_QUALITY_SELECT_SQL}
               {visible_sql}
               ORDER BY memory_fts.rank, m.id DESC LIMIT ?"""
        def fts_rows(match: str) -> list[sqlite3.Row] | None:
            return self._generic_recall_query_rows(
                select_sql,
                (match, *claim_shadow_parameters, candidate_limit + 1),
            )

        def visible_frequencies(matches: Sequence[str]) -> list[int] | None:
            """Count a bounded group of FTS expressions in one SQL statement."""
            if not matches:
                return []
            ctes: list[str] = []
            selects: list[str] = []
            parameters: list[Any] = []
            for index, match in enumerate(matches):
                ctes.append(
                    f"q{index} AS (SELECT m.id {visible_sql} LIMIT ?)"
                )
                selects.append(
                    f"SELECT {index} AS term_index, COUNT(*) AS frequency "
                    f"FROM q{index}"
                )
                parameters.extend((
                    match,
                    *claim_shadow_parameters,
                    candidate_limit + 1,
                ))
            try:
                rows = self.db.execute(
                    f"WITH {', '.join(ctes)} {' UNION ALL '.join(selects)} "
                    "ORDER BY term_index",
                    parameters,
                ).fetchall()
            except _memory().sqlite3.DatabaseError:
                return None
            frequencies = [0] * len(matches)
            for row in rows:
                frequencies[int(row["term_index"])] = int(row["frequency"])
            return frequencies

        def abstain(mode: str) -> None:
            report["mode"] = mode
            report["abstained"] = True

        chunk_size = max(1, int(term_chunk_size))
        collected_rows: dict[int, sqlite3.Row] = {}
        collected_cache_safe_ids: set[int] = set()
        modes: list[str] = []
        for offset in range(0, len(discovery_terms), chunk_size):
            candidate_terms = discovery_terms[offset:offset + chunk_size]
            like_terms = _memory()._memory_like_terms(
                query, candidate_terms, max_terms=chunk_size * 2
            )
            if not like_terms:
                continue
            fts_query = _memory()._memory_fts_query(
                query, candidate_terms, max_index_terms=chunk_size * 3
            )
            if fts_query is None:
                patterns = [f"%{_memory()._escape_like(term)}%" for term in like_terms]
                where = " OR ".join(
                    "lower(m.content) LIKE ? ESCAPE '\\'" for _ in patterns
                )
                match_count = " + ".join(
                    "CASE WHEN lower(m.content) LIKE ? ESCAPE '\\' THEN 1 ELSE 0 END"
                    for _ in patterns
                )
                chunk_rows = self._generic_recall_query_rows(
                    f"""SELECT m.id, m.created_at, m.kind, m.content, m.source,
                               c.status AS claim_status,
                               c.authority AS claim_authority,
                               omp.origin AS ordinary_origin,
                               omp.eligible AS ordinary_eligible,
                               omp.content_sha256 AS ordinary_content_sha256,
                               omp.provenance_sha256 AS ordinary_provenance_sha256,
                               {_memory()._LEARNING_QUALITY_SELECT_SQL}
                        FROM memories AS m
                        LEFT JOIN memory_claims AS c ON c.memory_id=m.id
                        LEFT JOIN ordinary_memory_provenance AS omp
                          ON omp.memory_id=m.id
                        {_memory()._LEARNING_QUALITY_ASSESSMENT_JOIN_SQL}
                        WHERE ({where})
                          AND m.kind<>'lesson'
                          {_memory()._LEARNING_QUALITY_LEXICAL_SQL}
                          AND (m.kind<>'claim' OR (
                              c.scope='global' AND c.status IN ('active', 'disputed')
                              {claim_shadow_sql}
                          ))
                        ORDER BY ({match_count}) DESC, m.id DESC LIMIT ?""",
                    [
                        *patterns,
                        *claim_shadow_parameters,
                        *patterns,
                        candidate_limit + 1,
                    ],
                )
                if chunk_rows is None:
                    abstain("error")
                    return None
                if len(chunk_rows) > candidate_limit:
                    abstain("overflow")
                    return None
                chunk_cache_safe_ids = self._ordinary_recall_cache_safe_ids(
                    chunk_rows
                )
                modes.append("like")
            else:
                chunk_rows = fts_rows(fts_query)
                if chunk_rows is None:
                    abstain("error")
                    return None
                chunk_cache_safe_ids = self._ordinary_recall_cache_safe_ids(
                    chunk_rows
                )
                identity_terms, identity_anchors = _memory()._memory_identity_scope(
                    query,
                    candidate_terms,
                    chunk_rows,
                    row_cache_allowed=chunk_cache_safe_ids,
                )
                if identity_terms:
                    # An identity is a relational constraint, not permission to
                    # return any row merely because that name exists elsewhere
                    # in the store.  Replace the generic OR pool with exact
                    # identity+fact witnesses at every corpus size.  Raw rows
                    # remain in the pool so a tampered/ineligible exact witness
                    # still shadows weaker verified content during ranking.
                    evidence = set(_memory()._memory_evidence_terms(
                        query,
                        chunk_rows,
                        max_terms=_memory().MAX_MEMORY_QUERY_TERMS,
                        row_cache_allowed=chunk_cache_safe_ids,
                    ))
                    required_anchors = [
                        term for term in identity_anchors if term in evidence
                    ]
                    if identity_anchors and not required_anchors:
                        report["unknown_terms"].extend(identity_terms)
                        abstain("identity-unbound")
                        return None
                    # Compound identifiers such as ``CASE-9931`` are retained
                    # by the semantic tokenizer as one term, while FTS5 splits
                    # them into surface tokens.  Feeding that compound back to
                    # ``_memory_fts_query`` silently drops it because neither
                    # surface token has the compound as its canonical form.
                    # Build the identity clause explicitly, then intersect it
                    # with the ordinary anchor groups.
                    structured_identities = {
                        term for term in identity_terms
                        if _memory()._structured_memory_identifier(term)
                    }
                    proof_parts = [
                        f"({_memory()._memory_fts_literal(term)})"
                        for term in identity_terms
                        if term in structured_identities
                    ]
                    natural_identities = [
                        term for term in identity_terms
                        if term not in structured_identities
                    ]
                    natural_identity_query = _memory()._memory_fts_query(
                        query,
                        natural_identities,
                        max_index_terms=chunk_size * 3,
                        require_all=True,
                    )
                    if natural_identity_query:
                        proof_parts.append(natural_identity_query)
                    anchor_query = _memory()._memory_fts_query(
                        query,
                        required_anchors,
                        max_index_terms=chunk_size * 3,
                        require_all=True,
                    )
                    if anchor_query:
                        proof_parts.append(anchor_query)
                    proof_query = " AND ".join(proof_parts) or None
                    proof_rows = (
                        None if proof_query is None else fts_rows(proof_query)
                    )
                    if proof_rows is None:
                        abstain("error")
                        return None
                    if structured_identities:
                        proof_cache_safe_ids = (
                            self._ordinary_recall_cache_safe_ids(proof_rows)
                        )
                        proof_rows = [
                            row for row in proof_rows
                            if structured_identities.issubset(set(_memory()._memory_tokens(
                                str(row["content"] or ""),
                                meaningful_only=False,
                                cache_allowed=(
                                    int(row["id"]) in proof_cache_safe_ids
                                ),
                            )))
                        ]
                    if not proof_rows:
                        report["unknown_terms"].extend(identity_terms)
                        abstain("identity-unbound")
                        return None
                    if len(proof_rows) > candidate_limit:
                        report["unknown_terms"].extend(identity_terms)
                        abstain("identity-overflow")
                        return None
                    was_narrowed = (
                        len(chunk_rows) > candidate_limit
                        or len(proof_rows) < len(chunk_rows)
                    )
                    chunk_rows = proof_rows
                    modes.append("narrowed" if was_narrowed else "or")
                elif len(chunk_rows) > candidate_limit:
                    # Stage 2: keep only terms that can discriminate on their
                    # own, counted over the caller's visible rows.
                    groups = _memory()._memory_fts_term_groups(
                        query, candidate_terms, max_index_terms=chunk_size * 3
                    )
                    grouped = {canonical for canonical, _spellings in groups}
                    structured_terms = [
                        _memory()._normalize_memory_token(term)
                        for term in candidate_terms
                        if (
                            _memory()._normalize_memory_token(term) not in grouped
                            and _memory()._structured_memory_identifier(term)
                        )
                    ]
                    frequency_queries = [
                        _memory()._memory_fts_group_query(spellings)
                        for _term, spellings in groups
                    ] + [
                        _memory()._memory_fts_literal(term) for term in structured_terms
                    ]
                    frequencies = visible_frequencies(frequency_queries)
                    if frequencies is None:
                        abstain("error")
                        return None
                    discriminating: list[str] = []
                    unknown_identities: list[str] = []
                    for (term, _spellings), frequency in zip(
                        groups, frequencies[:len(groups)], strict=True
                    ):
                        if frequency > candidate_limit:
                            report["dropped_terms"].append(term)
                        elif frequency == 0 and _memory()._memory_identity_capable_term(term):
                            unknown_identities.append(term)
                        else:
                            discriminating.append(term)
                    # Compound identifiers (CASE-9931) are not index surfaces,
                    # so the groups above never count them; check each one as
                    # a phrase over the same visible rows.
                    for canonical, frequency in zip(
                        structured_terms,
                        frequencies[len(groups):],
                        strict=True,
                    ):
                        if frequency == 0:
                            unknown_identities.append(canonical)
                    if unknown_identities:
                        # The query names something the visible store has
                        # never seen.  A look-alike record (NorthX for SouthX)
                        # may sit among the dropped rows where the ranker
                        # cannot see it, so returning anything here could be a
                        # substitution.  Fail closed and say why.
                        report["unknown_terms"].extend(unknown_identities)
                        abstain("unknown-identity")
                        return None
                    chunk_rows = None
                    if discriminating and len(discriminating) < len(groups):
                        narrowed_query = _memory()._memory_fts_query(
                            query, discriminating, max_index_terms=chunk_size * 3
                        )
                        if narrowed_query is not None:
                            chunk_rows = fts_rows(narrowed_query)
                            if chunk_rows is None:
                                abstain("error")
                                return None
                            if len(chunk_rows) > candidate_limit:
                                chunk_rows = None
                            else:
                                modes.append("narrowed")
                    if chunk_rows is None:
                        # Stage 3: no term discriminates alone; require every
                        # term.  This is the smallest set that can still hold
                        # the best answer to the whole query.
                        all_terms_query = _memory()._memory_fts_query(
                            query,
                            candidate_terms,
                            max_index_terms=chunk_size * 3,
                            require_all=True,
                        )
                        chunk_rows = (
                            None if all_terms_query is None
                            else fts_rows(all_terms_query)
                        )
                        if chunk_rows is None:
                            abstain("error")
                            return None
                        if len(chunk_rows) > candidate_limit:
                            abstain("overflow")
                            return None
                        modes.append("all-terms")
                else:
                    modes.append("or")
            # ``chunk_rows`` may come from a later proof/narrowing SELECT than
            # the initial OR pool. Revalidate the exact immutable row snapshots
            # that will leave this method; never reuse an earlier ID-only
            # admission across an intervening external update.
            final_cache_safe_ids = self._ordinary_recall_cache_safe_ids(
                chunk_rows
            )
            for row in chunk_rows:
                memory_id = int(row["id"])
                collected_rows.setdefault(memory_id, row)
                if memory_id in final_cache_safe_ids:
                    collected_cache_safe_ids.add(memory_id)
            if len(collected_rows) > candidate_limit:
                abstain("overflow")
                return None
        rows = list(collected_rows.values())
        report["candidates"] = len(rows)
        if modes:
            # Report the most defensive stage any chunk needed.
            for mode in ("all-terms", "narrowed", "like", "or"):
                if mode in modes:
                    report["mode"] = mode
                    break
            report["abstained"] = False
        return rows, frozenset(collected_cache_safe_ids)

    def claim_recall_report(self) -> dict[str, Any]:
        """Return a copy of the diagnostic record for the most recent claim-lane
        recall (``current_claims``).

        ``mode`` is ``idle``, ``screened``, ``project-unavailable``, ``or`` /
        ``all-terms`` (the discovery stage that produced the pool), or one of
        the fail-closed abstentions ``overflow``, ``identity-overflow``,
        ``identity-conflict``, ``ambiguous``, ``corrupt-strongest``, ``error``.
        ``abstained`` is true whenever the lane refused to answer, so an empty
        result is never silent.
        """
        return dict(self._last_claim_recall_report)

    def _abstain_claims(
        self,
        mode: str,
        reason: str,
        *,
        candidates: int | None = None,
    ) -> list[dict[str, Any]]:
        report = self._last_claim_recall_report
        report["mode"] = str(mode)
        report["abstained"] = True
        report["reason"] = str(reason)
        if candidates is not None:
            report["candidates"] = int(candidates)
        return []

    def _record_claim_clock_reads(self) -> None:
        """Persist pending claim-clock read counters without blocking a read.

        The read itself ran in a deferred snapshot.  Telemetry is written in a
        separate short immediate transaction; if a concurrent writer holds the
        lock past the short timeout the counters for this read are dropped and
        counted, never turned into an error for the caller.
        """
        updates = self._pending_claim_clock_updates
        self._pending_claim_clock_updates = []
        if not updates:
            return
        if self.db.in_transaction:
            # Never upgrade a caller's deferred snapshot into a write.
            self._dropped_claim_clock_reads += len(updates)
            return
        restore_timeout = int(
            getattr(self, "_busy_timeout_ms", _memory().DEFAULT_BUSY_TIMEOUT_MS)
        )
        try:
            self.db.execute(f"PRAGMA busy_timeout={_memory()._CLAIM_CLOCK_WRITE_TIMEOUT_MS}")
            try:
                self.db.execute("BEGIN IMMEDIATE")
                self.db.executemany(
                    """INSERT INTO memory_claim_clock_statistics(
                           claim_id, reads, stale_reads, last_effective_confidence,
                           last_clock_status, last_read_at
                       ) VALUES (?, 1, ?, ?, ?, ?)
                       ON CONFLICT(claim_id) DO UPDATE SET
                           reads=reads+1,
                           stale_reads=stale_reads+excluded.stale_reads,
                           last_effective_confidence=excluded.last_effective_confidence,
                           last_clock_status=excluded.last_clock_status,
                           last_read_at=excluded.last_read_at""",
                    updates,
                )
                self.db.commit()
            except (_memory().sqlite3.OperationalError, _memory().sqlite3.IntegrityError):
                if self.db.in_transaction:
                    self.db.rollback()
                self._dropped_claim_clock_reads += len(updates)
        finally:
            self.db.execute(f"PRAGMA busy_timeout={restore_timeout}")

    def recall_report(self) -> dict[str, Any]:
        """Return a copy of the diagnostic record for the most recent lexical
        recall attempt on this store (``search`` or ``hybrid_memory_search``).

        ``mode`` is one of ``idle`` (no recall yet), ``screened`` (the query
        was refused before discovery), ``empty`` (nothing to search),
        ``or`` / ``narrowed`` / ``all-terms`` / ``like`` (the discovery stage
        that produced the pool), ``identity-unbound`` / ``identity-overflow`` /
        ``unknown-identity`` / ``overflow`` / ``error`` (fail-closed
        abstentions). The record is reset at the start of every attempt, so it
        never describes an earlier query.

        Once the transcript channel has read on this store, ``transcript``
        carries its most recent read (``prior_conversation_excerpts``): its
        ``mode``, ``candidates``, ``returned``, ``budget_ms`` / ``elapsed_ms``
        / ``budget``, the rows excluded by the widened screen, and
        ``abstained`` / ``reason``.  A store the channel has never read keeps
        the pre-channel record byte for byte.
        """
        report = _memory().copy.deepcopy(self._last_recall_report)
        if str(self._last_transcript_recall_report.get("mode")) != "idle":
            report["transcript"] = self.transcript_recall_report()
        return report

    def transcript_recall_report(self) -> dict[str, Any]:
        """A copy of the diagnostic record for the most recent transcript-
        channel read, so an empty excerpt list is never silent."""
        return _memory().copy.deepcopy(self._last_transcript_recall_report)

    def _remember_ordinary(
        self,
        content: str,
        kind: str,
        source: str | None,
        *,
        origin: str,
        eligible: bool,
        actor: str = "runtime",
        conversation_id: int | None = None,
        permission: str = "runtime",
    ) -> str:
        safe_content = _memory().redact_secrets(str(content).strip())[:8_000]
        if not safe_content:
            raise ValueError("Memory content must not be empty")
        safe_kind = _memory()._validated_nonsecret_metadata(kind, "Memory kind")[:40]
        if not safe_kind:
            raise ValueError("Memory kind must not be empty")
        if safe_kind in {"lesson", "claim"}:
            raise ValueError("Lessons and claims require their dedicated provenance APIs")
        safe_source = (
            _memory().redact_secrets(str(source).strip())[:2_000] if source else None
        )
        context = self._spine_context(actor, conversation_id, permission)
        with self._immediate_transaction():
            stamp = _memory().now_iso()
            row = self.db.execute(
                """SELECT id, source FROM memories
                   WHERE kind=? AND content=?""",
                (safe_kind, safe_content),
            ).fetchone()
            created = row is None
            if created:
                # Check-then-insert under the write lock (design 12.6 item 1):
                # only an absent row allocates an id and appends
                # ``memory.created`` before the insert, which the lineage
                # trigger requires.
                if self._spine_ready:
                    memory_id = _memory().memory_spine.allocate_memory_id(self.db)
                    event_id = self._append_memory_event(
                        "memory.created",
                        memory_id=memory_id,
                        payload=_memory().memory_spine.memory_event_payload(
                            self._spine_key,
                            {
                                "kind": safe_kind, "content": safe_content,
                                "source": safe_source, "family": None,
                                "outcome_status": None, "reflection_id": None,
                            },
                            origin=origin,
                            eligible=bool(eligible),
                        ),
                        stamp=stamp,
                        source=safe_source,
                        context=context,
                    )
                    self.db.execute(
                        """INSERT INTO memories(
                               id, created_at, kind, content, source, spine_event_id
                           ) VALUES (?, ?, ?, ?, ?, ?)""",
                        (memory_id, stamp, safe_kind, safe_content, safe_source, event_id),
                    )
                else:
                    self.db.execute(
                        """INSERT INTO memories(created_at, kind, content, source)
                           VALUES (?, ?, ?, ?)""",
                        (stamp, safe_kind, safe_content, safe_source),
                    )
                row = self.db.execute(
                    """SELECT id, source FROM memories
                       WHERE kind=? AND content=?""",
                    (safe_kind, safe_content),
                ).fetchone()
                if row is None:
                    raise RuntimeError("Memory could not be persisted")
            memory_id = int(row["id"])
            stored_source = None if row["source"] is None else str(row["source"])
            if eligible and stored_source != safe_source:
                raise ValueError(
                    "Existing memory text has different source provenance"
                )
            existing = self.db.execute(
                "SELECT origin, eligible FROM ordinary_memory_provenance WHERE memory_id=?",
                (memory_id,),
            ).fetchone()
            before = (
                None if existing is None
                else (str(existing["origin"]), bool(int(existing["eligible"])))
            )
            # A later unverified duplicate must never downgrade a trusted row.
            if eligible or existing is None or not bool(int(existing["eligible"])):
                self._set_ordinary_memory_provenance_locked(
                    memory_id, origin=origin, eligible=eligible
                )
            if not created and self._spine_ready:
                # A duplicate write is receipted with the provenance it left
                # behind: ``applied`` only when the provenance row changed
                # (a duplicate ``remember_verified`` upgrades eligibility in
                # place), ``noop`` otherwise.
                current = self.db.execute(
                    "SELECT origin, eligible FROM ordinary_memory_provenance WHERE memory_id=?",
                    (memory_id,),
                ).fetchone()
                after = (
                    None if current is None
                    else (str(current["origin"]), bool(int(current["eligible"])))
                )
                self._append_memory_event(
                    "memory.reasserted",
                    memory_id=memory_id,
                    payload={
                        "origin": None if after is None else after[0],
                        "eligible": None if after is None else after[1],
                        "content_digest": _memory().memory_spine.content_digest(
                            self._spine_key, safe_content
                        ),
                    },
                    stamp=stamp,
                    source=safe_source,
                    context=context,
                    outcome="applied" if after != before else "noop",
                )
            self._sync_learning_quality_quarantine_locked(memory_id)
        return "Stored in long-term memory."

    def remember(
        self,
        content: str,
        kind: str = "fact",
        source: str | None = None,
        *,
        actor: str = "runtime",
        conversation_id: int | None = None,
        permission: str = "runtime",
    ) -> str:
        """Store an auditable but recall-ineligible ordinary memory by default.

        ``actor`` / ``permission`` / ``conversation_id`` are the spine receipt
        context (the model's memory tool passes ``model`` and its admitting
        gate); they never affect recall eligibility.
        """
        return self._remember_ordinary(
            content, kind, source, origin="unverified", eligible=False,
            actor=actor, conversation_id=conversation_id, permission=permission,
        )

    def remember_verified(
        self,
        content: str,
        kind: str = "fact",
        source: str | None = None,
        *,
        origin: str,
        actor: str = "runtime",
        conversation_id: int | None = None,
        permission: str = "runtime",
    ) -> str:
        """Store ordinary memory only through an explicit trusted write path."""
        return self._remember_ordinary(
            content, kind, source, origin=origin, eligible=True,
            actor=actor, conversation_id=conversation_id, permission=permission,
        )

    @_with_recall_cache
    @_with_read_snapshot
    def search(
        self,
        query: str,
        limit: int = 8,
        *,
        include_id: bool = False,
        project_id: int | None = None,
    ) -> list[dict[str, Any]]:
        self._ensure_open()
        self._last_recall_report = _memory()._blank_recall_report("screened")
        query = str(query)
        if len(query) > _memory().MAX_SEARCH_QUERY_CHARS:
            raise ValueError(f"Memory search query exceeds {_memory().MAX_SEARCH_QUERY_CHARS} characters")
        limit = _memory()._bounded_limit(limit, 100)
        if (
            _memory().contains_secret(query)
            or _memory().contains_private_identifier(query)
            or _memory()._memory_query_targets_authority_evasion(query)
        ):
            return []
        discovery_terms = _memory()._memory_candidate_terms(query)
        if not discovery_terms or not limit:
            self._last_recall_report = _memory()._blank_recall_report("empty")
            return []
        project_scope = self._enabled_project_claim_scope(project_id)
        claim_shadow_sql, claim_shadow_parameters = self._global_claim_shadow_clause(
            project_scope
        )
        candidate_batch = self._lexical_recall_candidates(
            query,
            discovery_terms,
            claim_shadow_sql=claim_shadow_sql,
            claim_shadow_parameters=claim_shadow_parameters,
            candidate_limit=_memory().MAX_MEMORY_SEARCH_CANDIDATES,
            term_chunk_size=_memory()._MAX_MEMORY_QUERY_TERM_CANDIDATES,
        )
        if candidate_batch is None:
            return []
        rows, cache_safe_ids = candidate_batch
        rows = _memory()._memory_resolve_sibling_identities(
            list(rows),
            query,
            identity_ignored_terms=_memory()._ORDINARY_MEMORY_IDENTITY_METADATA_TERMS,
            capitalized_subject_identity=True,
            row_cache_allowed=cache_safe_ids,
        )
        if not rows:
            return []
        evidence_terms = _memory()._memory_evidence_terms(
            query,
            rows,
            row_cache_allowed=cache_safe_ids,
        )
        structured_terms = [
            term for term in discovery_terms
            if any(character.isalpha() for character in term)
            and any(character.isdigit() for character in term)
        ]
        query_terms = list(dict.fromkeys([
            *structured_terms,
            *evidence_terms,
        ]))
        for term in _memory()._memory_query_terms(query):
            if term not in query_terms:
                query_terms.append(term)
            if len(query_terms) >= _memory().MAX_MEMORY_QUERY_TERMS:
                break
        query_terms = query_terms[:_memory().MAX_MEMORY_QUERY_TERMS]
        if not query_terms:
            return []
        explicit_multi_fact_query = _memory().re.search(
            r"\b(?:and|plus)\b|[&+]", query, _memory().re.I
        ) is not None
        surface_terms = _memory().re.findall(r"[^\W_]+", query, flags=_memory().re.UNICODE)
        meaningful_surfaces = [
            surface for surface in surface_terms
            if _memory()._normalize_memory_token(surface) in set(query_terms)
        ]
        proper_name_pair = bool(
            len(query_terms) == 2
            and len(meaningful_surfaces) == 2
            and all(surface[:1].isupper() for surface in meaningful_surfaces)
        )
        ambiguous_compact_query = bool(
            len(query_terms) == 2
            and not explicit_multi_fact_query
            and (len(surface_terms) == 2 or proper_name_pair)
        )
        if ambiguous_compact_query:
            # A compact pair is ambiguous when each identity only selects a
            # different record. Require at least one record to satisfy both
            # anchors; explicit connectors retain the intentional multi-fact
            # path. Keep the full candidate set for ranking so legitimate
            # versioned notes can still be returned newest-first.
            variant_sets = [
                set(_memory()._memory_term_variants(term)) for term in query_terms
            ]
            full_match_rows = [
                row for row in rows
                if all(
                    variants.intersection(set(_memory()._memory_tokens(
                        str(row["content"]),
                        meaningful_only=False,
                        cache_allowed=(int(row["id"]) in cache_safe_ids),
                    )))
                    for variants in variant_sets
                )
            ]
            if not full_match_rows:
                return []
        ranked, _shadowed = self._rank_generic_recall_rows(
            list(rows),
            query_terms,
            keep_id=bool(include_id),
            max_results=limit,
            minimum_information_coverage=0.30,
            relative_match_floor=0.85,
            relative_information_floor=0.85,
            query_text=query,
            cache_safe_ids=cache_safe_ids,
        )
        if ambiguous_compact_query and len(ranked) > 1:
            query_variants = set().union(*(
                set(_memory()._memory_term_variants(term)) for term in query_terms
            ))
            token_sets = [
                set(_memory()._memory_tokens(
                    str(item.get("content") or ""), meaningful_only=True
                ))
                for item in ranked[:2]
            ]
            match_counts = [
                sum(
                    bool(set(_memory()._memory_term_variants(term)).intersection(tokens))
                    for term in query_terms
                )
                for tokens in token_sets
            ]
            residuals = [
                {
                    token for token in tokens
                    if not query_variants.intersection(
                        _memory()._memory_term_variants(token)
                    )
                }
                for tokens in token_sets
            ]
            # Sibling records that each satisfy every anchor are legitimate
            # versioned or multi-fact notes, not ambiguity; only records
            # covering different proper subsets of the request stay ambiguous.
            if (
                min(match_counts) < len(query_terms)
                and all(residuals)
                and residuals[0].isdisjoint(residuals[1])
            ):
                return []
        if (
            not explicit_multi_fact_query
            and len(query_terms) <= 3
            and len(ranked) > 1
        ):
            query_variants = set().union(*(
                set(_memory()._memory_term_variants(term)) for term in query_terms
            ))
            token_sets = [
                set(_memory()._memory_tokens(
                    str(item.get("content") or ""), meaningful_only=True
                ))
                for item in ranked[:2]
            ]
            match_counts = [
                sum(
                    bool(set(_memory()._memory_term_variants(term)).intersection(tokens))
                    for term in query_terms
                )
                for tokens in token_sets
            ]
            residuals = [
                {
                    token for token in tokens
                    if not query_variants.intersection(
                        _memory()._memory_term_variants(token)
                    )
                }
                for tokens in token_sets
            ]
            # Records that all satisfy every anchor are versioned or
            # multi-fact notes about one topic; abstention is only for equal
            # partial coverage over unrelated content.
            if (
                all(residuals)
                and match_counts[0] == match_counts[1]
                and match_counts[0] < len(query_terms)
            ):
                overlap = len(residuals[0].intersection(residuals[1]))
                union = len(residuals[0].union(residuals[1]))
                if union and overlap / union < 0.5:
                    return []
        return _memory()._memory_resolve_sibling_identities(
            ranked,
            query,
            identity_ignored_terms=_memory()._ORDINARY_MEMORY_IDENTITY_METADATA_TERMS,
            capitalized_subject_identity=True,
        )[:limit]

    def list_memories(
        self, limit: int = 20, *, with_ids: bool = False
    ) -> list[dict[str, Any]]:
        """Newest ordinary memories, newest first.

        ``with_ids`` adds the operator-facing ``id`` (explicit and never
        reused since schema 47, so it names a row for ``Erase memory #<id>``)
        together with the ordinary provenance ``origin`` and ``eligible``,
        which the listing surfaces print beside the preview.
        """
        self._ensure_open()
        limit = _memory()._bounded_limit(limit, 1_000)
        if not limit:
            return []
        if with_ids:
            rows = self.db.execute(
                """SELECT m.id, m.created_at, m.kind, m.content, m.source,
                          omp.origin AS origin, omp.eligible AS eligible
                   FROM memories AS m
                   LEFT JOIN ordinary_memory_provenance AS omp
                     ON omp.memory_id=m.id
                   WHERE m.kind <> 'claim'
                   ORDER BY m.id DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        else:
            rows = self.db.execute(
                """SELECT created_at, kind, content, source
                   FROM memories
                   WHERE kind <> 'claim'
                   ORDER BY id DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    @_with_read_snapshot
    def verified_operator_preferences(self, limit: int = 2) -> list[dict[str, Any]]:
        """Return newest explicitly stored preferences with valid provenance."""
        self._ensure_open()
        limit = _memory()._bounded_limit(limit, 20)
        if not limit:
            return []
        candidate_limit = min(1_000, max(32, limit * 16))
        try:
            rows = self.db.execute(
                """SELECT m.id, m.created_at, m.kind, m.content, m.source
                   FROM memories AS m
                   JOIN ordinary_memory_provenance AS omp
                     ON omp.memory_id=m.id
                   WHERE m.kind='preference'
                     AND omp.eligible=1
                     AND omp.origin='explicit_operator_memory'
                   ORDER BY m.id DESC LIMIT ?""",
                (candidate_limit + 1,),
            ).fetchall()
        except _memory().sqlite3.DatabaseError:
            return []
        if len(rows) > candidate_limit:
            return []
        verified: list[dict[str, Any]] = []
        for row in rows:
            if not self._ordinary_memory_recall_eligible(int(row["id"])):
                continue
            item = dict(row)
            item.pop("id", None)
            verified.append(item)
            if len(verified) >= limit:
                break
        return verified
