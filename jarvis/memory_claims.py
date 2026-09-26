"""Current Memory domain extracted without changing method control flow.

Global dependencies resolve through jarvis.memory at call time. The facade keeps
public imports, patch seams, constants and authorization helpers authoritative.
"""
from __future__ import annotations

from collections.abc import Mapping
from collections.abc import Sequence
from typing import Any
import sqlite3
from .memory_runtime import (_memory, _with_recall_cache)


class ClaimsMemoryMixin:
    """Mechanically extracted current Memory methods."""

    @staticmethod
    def _global_claim_shadow_clause(
        project_scope: str | None,
        *,
        claim_alias: str = "c",
    ) -> tuple[str, tuple[str, ...]]:
        """Exclude globals overridden by a live claim in the active project."""
        if project_scope is None:
            return "", ()
        if claim_alias not in {"c"}:
            raise ValueError("Unsupported claim alias")
        return (
            f""" AND NOT EXISTS (
                     SELECT 1 FROM memory_claims AS project_claim
                     WHERE project_claim.scope=?
                       AND project_claim.claim_key={claim_alias}.claim_key
                       AND project_claim.status IN ('active', 'disputed')
                 )""",
            (project_scope,),
        )

    def record_fact_proposal(
        self,
        conversation_id: int,
        assistant_message_id: int,
        project_id: int,
        command: str,
        *,
        assisted: bool,
        reply_asked_question: bool,
    ) -> int:
        """Keep the runtime's own record of a project-fact proposal it showed.

        A confirmation ("store it") is resolved against this record, never
        against assistant text, so a reply that imitates the receipt can never
        be confirmed.  The command must be a valid governed command.
        """
        text = str(command).strip()
        if _memory().parse_explicit_project_fact(text) is None:
            raise ValueError("A fact proposal must be an exact governed command")
        for name, value in (
            ("conversation_id", conversation_id),
            ("assistant_message_id", assistant_message_id),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        normalized_project = self._project_id(project_id)
        # The digest is salted per proposal so that, after an erase blanks the
        # command, a low-entropy value (a port number) cannot be confirmed by
        # hashing guesses against a surviving digest.
        salt = _memory().secrets.token_hex(16)
        digest = _memory().hashlib.sha256(f"{salt}\n{text}".encode("utf-8")).hexdigest()
        with self._immediate_transaction():
            cursor = self.db.execute(
                """INSERT INTO memory_fact_proposals(
                       created_at, conversation_id, assistant_message_id, project_id,
                       command, command_sha256, command_salt, assisted,
                       reply_asked_question, status
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'shown')""",
                (
                    _memory().now_iso(),
                    int(conversation_id),
                    int(assistant_message_id),
                    normalized_project,
                    text,
                    digest,
                    salt,
                    int(bool(assisted)),
                    int(bool(reply_asked_question)),
                ),
            )
            return int(cursor.lastrowid)

    def fact_proposal_digest(self, proposal_id: int) -> str | None:
        """The salted digest recorded for a shown proposal."""
        self._ensure_open()
        row = self.db.execute(
            "SELECT command_sha256 FROM memory_fact_proposals WHERE id=?",
            (int(proposal_id),),
        ).fetchone()
        return str(row[0]) if row is not None else None

    def link_fact_proposal_event(self, proposal_id: int, event_id: int) -> None:
        """Remember which spine event receipted a shown proposal, so a later
        confirmation can point back at exactly that event."""
        with self._immediate_transaction():
            self.db.execute(
                "UPDATE memory_fact_proposals SET spine_event_id=? WHERE id=? AND spine_event_id IS NULL",
                (int(event_id), int(proposal_id)),
            )

    @staticmethod
    def claim_key_for(subject: str, predicate: str) -> str:
        """The claim identity a governed command would write under."""
        return _memory().Memory._claim_identity(subject, predicate)

    def pending_fact_proposal(self, conversation_id: int) -> dict[str, Any] | None:
        """The proposal shown by the last message of a conversation, if any.

        Returns ``None`` unless the newest transcript row of the conversation
        is the assistant message that carried the proposal and a user message
        precedes it; a crashed, cancelled, or ordinary turn in between ends
        the offer.
        """
        self._ensure_open()
        if (
            isinstance(conversation_id, bool)
            or not isinstance(conversation_id, int)
            or conversation_id <= 0
        ):
            return None
        row = self.db.execute(
            """SELECT id, assistant_message_id, project_id, command, assisted,
                      reply_asked_question, command_sha256, spine_event_id
               FROM memory_fact_proposals
               WHERE conversation_id=? AND status='shown'
               ORDER BY id DESC LIMIT 1""",
            (conversation_id,),
        ).fetchone()
        if row is None:
            return None
        newest = self.db.execute(
            "SELECT MAX(id) FROM messages WHERE conversation_id=?",
            (conversation_id,),
        ).fetchone()[0]
        if newest is None or int(newest) != int(row["assistant_message_id"]):
            return None
        previous = self.db.execute(
            """SELECT content FROM messages
               WHERE conversation_id=? AND id<? AND role='user'
               ORDER BY id DESC LIMIT 1""",
            (conversation_id, int(row["assistant_message_id"])),
        ).fetchone()
        if previous is None:
            return None
        return {
            "id": int(row["id"]),
            "assistant_message_id": int(row["assistant_message_id"]),
            "project_id": int(row["project_id"]),
            "command": str(row["command"]),
            "assisted": bool(row["assisted"]),
            "reply_asked_question": bool(row["reply_asked_question"]),
            "previous_user_text": str(previous["content"]),
            "command_sha256": str(row["command_sha256"]),
            "spine_event_id": (
                int(row["spine_event_id"]) if row["spine_event_id"] is not None else None
            ),
        }

    def resolve_fact_proposal(
        self, proposal_id: int, status: str, *, claim_id: int | None = None
    ) -> None:
        """Mark a shown proposal confirmed, refused, or expired (once)."""
        if status not in {"confirmed", "refused", "expired"}:
            raise ValueError("Unknown fact proposal status")
        with self._immediate_transaction():
            self.db.execute(
                """UPDATE memory_fact_proposals
                   SET status=?, resolved_at=?, claim_id=?
                   WHERE id=? AND status='shown'""",
                (status, _memory().now_iso(), claim_id, int(proposal_id)),
            )

    def _claim_memory_recall_eligible(
        self,
        memory_id: int,
        *,
        project_id: int | None = None,
    ) -> bool:
        """Keep private claim material local and outside model-facing recall."""
        row = self.db.execute(
            """SELECT c.id AS claim_id, c.memory_id, c.created_at, c.updated_at,
                      c.claim_key,
                      c.subject, c.predicate, c.value, c.value_sha256,
                      c.source, c.authority, c.confidence, c.scope, c.status
               FROM memory_claims AS c
               WHERE c.memory_id=?
                 AND c.status IN ('active', 'disputed')""",
            (int(memory_id),),
        ).fetchone()
        if row is None:
            return False
        return int(row["claim_id"]) in self._claim_rows_recall_eligible(
            [row], project_id=project_id
        )

    @classmethod
    def _claim_recall_material_eligible(
        cls,
        row: Mapping[str, Any],
        memory_row: Mapping[str, Any] | None,
        evidence_rows: Sequence[Mapping[str, Any]],
        *,
        visible_scopes: set[str],
        allow_superseded: bool = False,
    ) -> bool:
        """Validate one already-fetched claim without issuing database reads.

        ``allow_superseded`` admits retired versions for the temporal history
        read; every other check (canonical backing row, evidence, privacy)
        applies to them unchanged.
        """
        if memory_row is None or str(memory_row["memory_kind"] or "") != "claim":
            return False
        row_keys = set(row.keys())
        created_at = str(row["created_at"] or "")
        updated_at = str(
            row["updated_at"] if "updated_at" in row_keys else created_at
        )
        if not (
            _memory()._recall_timestamp_valid(created_at)
            and _memory()._recall_timestamp_valid(updated_at)
        ):
            return False
        try:
            if _memory().datetime.fromisoformat(updated_at.replace("Z", "+00:00")) < (
                _memory().datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            ):
                return False
        except ValueError:
            return False
        status = str(row["status"] or "")
        admitted_statuses = (
            {"active", "disputed", "superseded"}
            if allow_superseded else {"active", "disputed"}
        )
        if status not in admitted_statuses:
            return False
        scope = str(row["scope"] or "")
        if scope not in visible_scopes:
            return False
        subject = str(row["subject"] or "")
        predicate = str(row["predicate"] or "")
        value = str(row["value"] or "")
        source = str(row["source"] or "")
        authority = str(row["authority"] or "")
        if authority not in _memory()._CLAIM_AUTHORITY_WEIGHT:
            return False
        if "claim_key" in row_keys and str(row["claim_key"] or "") != (
            cls._claim_identity(subject, predicate)
        ):
            return False
        if "confidence" in row_keys:
            try:
                confidence = float(row["confidence"])
            except (TypeError, ValueError):
                return False
            if not _memory().math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
                return False
        memory_created_at = str(memory_row["memory_created_at"] or "")
        if (
            not _memory()._recall_timestamp_valid(memory_created_at)
            or memory_created_at != created_at
        ):
            return False
        if str(memory_row["memory_content"] or "") not in _memory().backing_content_variants(
            subject, predicate, value, scope, created_at,
            cls._claim_identity(subject, predicate),
        ):
            # Claims are returned from their structured fields.  If either the
            # structured row or its paired memory was modified independently,
            # fail closed instead of trusting a non-canonical reconstruction.
            # Both legal variants are recomputed from this row alone, so a
            # claim whose backing content carries the collision suffix stays
            # recallable instead of reading as a corrupt projection.
            return False
        if _memory()._claim_has_sensitive_key(subject, predicate):
            # A structured claim whose subject/predicate pair names a
            # credential field must never become model-facing memory, even
            # when its arbitrary value does not resemble a provider-specific
            # token format. This also catches split keys such as API + key.
            return False
        canonical_value_sha256 = _memory().hashlib.sha256(
            " ".join(value.casefold().split()).encode("utf-8")
        ).hexdigest()
        if str(row["value_sha256"] or "") != canonical_value_sha256:
            return False
        if str(memory_row["memory_source"] or "") != f"{authority}:{source}"[:2_000]:
            return False
        supported = False
        for evidence in evidence_rows:
            evidence_authority = str(evidence["authority"] or "")
            evidence_source = str(evidence["source"] or "")
            try:
                evidence_confidence = float(evidence["confidence"])
            except (TypeError, ValueError):
                continue
            if not _memory().math.isfinite(evidence_confidence):
                continue
            evidence_sha256 = _memory().hashlib.sha256(
                _memory().json.dumps(
                    {
                        "authority": evidence_authority,
                        "confidence": round(evidence_confidence, 6),
                        "source": evidence_source,
                        "value": canonical_value_sha256,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            if (
                evidence_authority == authority
                and evidence_source == source
                and str(evidence["evidence_sha256"] or "") == evidence_sha256
            ):
                supported = True
                break
        if not supported:
            return False
        # Scan atomic fields independently. Joining a benign predicate such as
        # "review token" to its value with punctuation can look like a
        # credential assignment even though neither stored field is secret.
        # The canonical memory content/source were authenticated by exact
        # equality above, so scanning those synthesized strings again would
        # introduce the same cross-field false positive. Inputs are also
        # checked at the write boundary; this second check protects recall if
        # the database is modified out of band.
        return all(
            not _memory().contains_secret(field)
            and not _memory().contains_private_identifier(field)
            for field in (
                created_at,
                updated_at,
                subject,
                predicate,
                value,
                source,
                authority,
                scope,
                status,
                memory_created_at,
            )
        )

    def _claim_rows_recall_eligible(
        self,
        rows: Sequence[Mapping[str, Any]],
        *,
        project_id: int | None = None,
        allow_superseded: bool = False,
    ) -> set[int]:
        """Validate a bounded claim set with constant-size batched SQL reads."""
        if not rows:
            return set()
        visible_scopes = {"global"}
        if project_id is not None:
            visible_scopes.add(_memory().project_claim_scope(self._project_id(project_id)))
        try:
            claim_rows = {
                int(row["claim_id"]): row
                for row in rows
            }
            memory_ids = list(dict.fromkeys(
                int(row["memory_id"]) for row in claim_rows.values()
            ))
        except (KeyError, TypeError, ValueError):
            return set()

        memories_by_id: dict[int, _memory().sqlite3.Row] = {}
        evidence_by_claim: dict[int, list[_memory().sqlite3.Row]] = {}
        try:
            for offset in range(0, len(memory_ids), _memory()._CLAIM_RECALL_BATCH_SIZE):
                chunk = memory_ids[offset:offset + _memory()._CLAIM_RECALL_BATCH_SIZE]
                placeholders = ",".join("?" for _memory_id in chunk)
                memory_rows = self.db.execute(
                    f"""SELECT id AS memory_id, created_at AS memory_created_at,
                               kind AS memory_kind,
                               content AS memory_content, source AS memory_source
                        FROM memories WHERE id IN ({placeholders})""",
                    chunk,
                ).fetchall()
                for memory_row in memory_rows:
                    memories_by_id[int(memory_row["memory_id"])] = memory_row

            claim_ids = list(claim_rows)
            for offset in range(0, len(claim_ids), _memory()._CLAIM_RECALL_BATCH_SIZE):
                chunk = claim_ids[offset:offset + _memory()._CLAIM_RECALL_BATCH_SIZE]
                placeholders = ",".join("?" for _claim_id in chunk)
                evidence_rows = self.db.execute(
                    f"""SELECT claim_id, source, authority, confidence,
                               evidence_sha256
                        FROM memory_claim_evidence
                        WHERE claim_id IN ({placeholders})""",
                    chunk,
                ).fetchall()
                for evidence in evidence_rows:
                    evidence_by_claim.setdefault(
                        int(evidence["claim_id"]), []
                    ).append(evidence)
        except _memory().sqlite3.DatabaseError:
            return set()

        eligible: set[int] = set()
        cache = _memory()._ACTIVE_RECALL_CACHE.get()
        for claim_id, row in claim_rows.items():
            memory_row = memories_by_id.get(int(row["memory_id"]))
            claim_evidence = evidence_by_claim.get(claim_id, ())
            cache_key: tuple[str, bytes] | None = None
            cached_eligible: Any | None = None
            if cache is not None:
                cache_key = _memory()._claim_ordered_cache_key(
                    "claim-eligibility-history" if allow_superseded else "claim-eligibility",
                    (
                        tuple(sorted(visible_scopes)),
                        tuple(
                            (key, row[key]) for key in sorted(row.keys())
                        ),
                        None if memory_row is None else tuple(
                            (key, memory_row[key])
                            for key in sorted(memory_row.keys())
                        ),
                        tuple(sorted(
                            tuple(
                                (key, evidence[key])
                                for key in sorted(evidence.keys())
                            )
                            for evidence in claim_evidence
                        )),
                    ),
                )
                cached_eligible = cache.get(cache_key)
            is_eligible = (
                bool(cached_eligible)
                if cached_eligible is not None
                else self._claim_recall_material_eligible(
                    row,
                    memory_row,
                    claim_evidence,
                    visible_scopes=visible_scopes,
                    allow_superseded=allow_superseded,
                )
            )
            if cache is not None and cache_key is not None and cached_eligible is None:
                cache.put(cache_key, is_eligible, 1)
            if is_eligible:
                eligible.add(claim_id)
        return eligible

    @staticmethod
    def _claim_output_snapshot_safe(row: Mapping[str, Any]) -> bool:
        """Screen every claim-memory field that can leave generic recall."""
        try:
            if str(row["kind"] or "") != "claim":
                return False
            fields = (
                str(row["created_at"] or ""),
                str(row["content"] or ""),
                str(row["source"] or ""),
                str(row["claim_status"] or ""),
                str(row["claim_authority"] or ""),
            )
        except (KeyError, TypeError):
            return False
        return (
            _memory()._recall_timestamp_valid(fields[0])
            and fields[3] in {"active", "disputed"}
            and fields[4] in _memory()._CLAIM_AUTHORITY_WEIGHT
            and all(
                not _memory().contains_secret(field)
                and not _memory().contains_private_identifier(field)
                for field in fields
            )
        )

    @staticmethod
    def _claim_identity(subject: str, predicate: str) -> str:
        canonical = _memory().json.dumps(
            {
                "subject": " ".join(subject.casefold().split()),
                "predicate": " ".join(predicate.casefold().split()),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return _memory().hashlib.sha256(("jarvis-claim-v1\0" + canonical).encode("utf-8")).hexdigest()

    @staticmethod
    def _claim_clock_predicate(predicate: str) -> str:
        return " ".join(str(predicate).casefold().split())

    def _refit_claim_volatility_locked(self, predicate: str, stamp: str) -> None:
        normalized = self._claim_clock_predicate(predicate)
        rows = self.db.execute(
            """SELECT claim_key, observed_at, value_sha256, source_key, confidence
               FROM (
                   SELECT id, claim_key, observed_at, value_sha256,
                          source_key, confidence
                   FROM memory_claim_observations
                   WHERE predicate=? ORDER BY id DESC LIMIT 4000
               )
               ORDER BY claim_key, observed_at, id""",
            (normalized,),
        ).fetchall()
        previous: dict[str, sqlite3.Row] = {}
        pairs: list[tuple[float, bool, float, float]] = []
        values: set[str] = set()
        for row in rows:
            claim_key = str(row["claim_key"])
            values.add(str(row["value_sha256"]))
            prior = previous.get(claim_key)
            if prior is not None and str(prior["source_key"]) != str(row["source_key"]):
                delta = _memory().claim_age_days(
                    str(prior["observed_at"]), str(row["observed_at"])
                )
                if delta > 0:
                    pairs.append(
                        (
                            delta,
                            str(prior["value_sha256"]) == str(row["value_sha256"]),
                            float(prior["confidence"]),
                            float(row["confidence"]),
                        )
                    )
            previous[claim_key] = row
        vocabulary_size = max(2, len(values))
        hazard, pair_count = _memory().estimate_claim_hazard(
            pairs[-900:], vocabulary_size=vocabulary_size
        )
        self.db.execute(
            """INSERT INTO memory_claim_volatility(
                   predicate, hazard_per_day, pair_count, vocabulary_size, fitted_at
               ) VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(predicate) DO UPDATE SET
                   hazard_per_day=excluded.hazard_per_day,
                   pair_count=excluded.pair_count,
                   vocabulary_size=excluded.vocabulary_size,
                   fitted_at=excluded.fitted_at""",
            (normalized, hazard, pair_count, vocabulary_size, stamp),
        )

    def _record_claim_observation_locked(
        self,
        claim_id: int,
        *,
        claim_key: str,
        predicate: str,
        value_sha256: str,
        source_identity: str | None,
        authority: str,
        confidence: float,
        stamp: str,
    ) -> None:
        if not self._claim_clock_ready:
            return
        normalized = self._claim_clock_predicate(predicate)
        self.db.execute(
            """INSERT INTO memory_claim_observations(
                   claim_id, claim_key, predicate, observed_at, value_sha256,
                   source_key, authority, confidence
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                claim_id, claim_key, normalized, stamp, value_sha256,
                _memory().claim_source_key(authority, source_identity), authority, confidence,
            ),
        )
        observation_count = int(
            self.db.execute(
                "SELECT COUNT(*) FROM memory_claim_observations WHERE predicate=?",
                (normalized,),
            ).fetchone()[0]
        )
        if (
            observation_count <= _memory().MIN_HAZARD_PAIRS + 2
            or observation_count % 8 == 0
        ):
            self._refit_claim_volatility_locked(normalized, stamp)

    def _set_claim_status_locked(
        self,
        claim_id: int,
        status: str,
        *,
        stamp: str,
        reason: str,
        related_claim_id: int | None = None,
        actor: str = "runtime",
        conversation_id: int | None = None,
        permission: str = "runtime",
        spine_kind: str | None = None,
    ) -> None:
        row = self.db.execute(
            "SELECT status FROM memory_claims WHERE id=?", (claim_id,)
        ).fetchone()
        if row is None or str(row["status"]) == status:
            return
        valid_until = stamp if status == "superseded" else None
        self.db.execute(
            """UPDATE memory_claims
               SET status=?, valid_until=?, updated_at=? WHERE id=?""",
            (status, valid_until, stamp, claim_id),
        )
        if self._graph_ready:
            # The edge copies the claim's status and interval so traversal can
            # rank current over superseded without a join (design 4.5).  A
            # claim the projection excluded has no edge and this is a no-op.
            _memory().memory_graph.update_edge(
                self.db, claim_id, status=status, valid_until=valid_until
            )
        kind = spine_kind or {
            "active": "claim.reasserted",
            "disputed": "claim.disputed",
            "superseded": "claim.superseded",
        }[status]
        spine_event_id = self._append_claim_after_image_event(
            claim_id, kind=kind, stamp=stamp, reason=reason,
            related_claim_id=related_claim_id, actor=actor,
            conversation_id=conversation_id, permission=permission,
        )
        if self._spine_ready:
            self.db.execute(
                """INSERT INTO memory_claim_events(
                       claim_id, created_at, status, reason, related_claim_id, spine_event_id
                   ) VALUES (?, ?, ?, ?, ?, ?)""",
                (claim_id, stamp, status, reason[:200], related_claim_id, spine_event_id),
            )
        else:
            self.db.execute(
                """INSERT INTO memory_claim_events(
                       claim_id, created_at, status, reason, related_claim_id
                   ) VALUES (?, ?, ?, ?, ?)""",
                (claim_id, stamp, status, reason[:200], related_claim_id),
            )

    def _remember_claim_locked(
        self,
        subject: str,
        predicate: str,
        value: str,
        *,
        source: str,
        authority: str,
        confidence: float,
        stamp: str,
        source_identity: str | None = None,
        scope: str = "global",
        actor: str = "runtime",
        conversation_id: int | None = None,
        permission: str = "runtime",
    ) -> int:
        scope = _memory()._validated_claim_scope(scope)
        _set_status = _memory().functools.partial(
            self._set_claim_status_locked,
            actor=actor,
            conversation_id=conversation_id,
            permission=permission,
        )
        if scope == "global" and _memory()._PROJECT_CLAIM_RECORD_PREFIX in (
            f"{subject} {predicate}: {value}".casefold()
        ):
            raise ValueError("Claim content contains a reserved project-record prefix")
        claim_key = self._claim_identity(subject, predicate)
        latest_event = self.db.execute(
            """SELECT MAX(e.created_at)
               FROM memory_claim_events AS e
               JOIN memory_claims AS c ON c.id=e.claim_id
               WHERE c.scope=? AND c.claim_key=?""",
            (scope, claim_key),
        ).fetchone()[0]
        if latest_event:
            requested_at = _memory()._as_utc(
                _memory().datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
            )
            latest_at = _memory()._as_utc(
                _memory().datetime.fromisoformat(str(latest_event).replace("Z", "+00:00"))
            )
            if requested_at <= latest_at:
                stamp = (latest_at + _memory().timedelta(microseconds=1)).isoformat()
        normalized_value = " ".join(value.casefold().split())
        value_sha256 = _memory().hashlib.sha256(normalized_value.encode("utf-8")).hexdigest()
        evidence_sha256 = _memory().hashlib.sha256(
            _memory().json.dumps(
                {
                    "authority": authority,
                    "confidence": round(confidence, 6),
                    "source": source,
                    "value": value_sha256,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        all_claims = self.db.execute(
            """SELECT id, memory_id, value_sha256, source, authority,
                      confidence, status
               FROM memory_claims
               WHERE scope=? AND claim_key=?
               ORDER BY id""",
            (scope, claim_key),
        ).fetchall()
        live = [
            row for row in all_claims
            if str(row["status"]) in {"active", "disputed"}
        ]
        same_source_claim_ids: set[int] = set()
        if source_identity is not None and self._claim_clock_ready:
            incoming_source_key = _memory().claim_source_key(authority, source_identity)
            same_source_claim_ids = {
                int(row["claim_id"])
                for row in self.db.execute(
                    """SELECT DISTINCT o.claim_id
                       FROM memory_claim_observations AS o
                       JOIN memory_claims AS c ON c.id=o.claim_id
                       WHERE c.scope=? AND o.claim_key=? AND o.source_key=?""",
                    (scope, claim_key, incoming_source_key),
                ).fetchall()
            }
        same_source_live = [
            row for row in live if int(row["id"]) in same_source_claim_ids
        ]
        competing_live = [
            row for row in live if int(row["id"]) not in same_source_claim_ids
        ]
        matching = [
            row for row in all_claims if row["value_sha256"] == value_sha256
        ]
        new_weight = _memory()._CLAIM_AUTHORITY_WEIGHT[authority]
        strongest_weight = max(
            (_memory()._CLAIM_AUTHORITY_WEIGHT[str(row["authority"])] for row in live),
            default=-1,
        )

        if matching:
            selected = max(
                matching,
                key=lambda row: (
                    _memory()._CLAIM_AUTHORITY_WEIGHT[str(row["authority"])], int(row["id"])
                ),
            )
            claim_id = int(selected["id"])
            existing_weight = _memory()._CLAIM_AUTHORITY_WEIGHT[str(selected["authority"])]
            evidence_exists = self.db.execute(
                """SELECT 1 FROM memory_claim_evidence
                   WHERE claim_id=? AND evidence_sha256=?""",
                (claim_id, evidence_sha256),
            ).fetchone() is not None
            combined_confidence = float(selected["confidence"])
            if not evidence_exists:
                combined_confidence = min(
                    0.999,
                    1.0
                    - (1.0 - combined_confidence) * (1.0 - confidence * 0.5),
                )
            promoted_authority = authority if new_weight > existing_weight else str(selected["authority"])
            promoted_source = (
                source
                if new_weight >= existing_weight
                else str(selected["source"])
            )
            self.db.execute(
                """UPDATE memory_claims
                   SET updated_at=?, confidence=?, authority=?, source=? WHERE id=?""",
                (
                    stamp, combined_confidence, promoted_authority,
                    promoted_source, claim_id,
                ),
            )
            self.db.execute(
                "UPDATE memories SET source=? WHERE id=?",
                (
                    f"{promoted_authority}:{promoted_source}"[:2_000],
                    int(selected["memory_id"]),
                ),
            )
            if self._graph_ready:
                _memory().memory_graph.update_edge(
                    self.db, claim_id,
                    confidence=combined_confidence,
                    authority=promoted_authority,
                )
            self._append_claim_after_image_event(
                claim_id, kind="claim.reasserted", stamp=stamp,
                reason="matching value asserted again", related_claim_id=None,
                actor=actor, conversation_id=conversation_id, permission=permission,
            )
            same_source_reassertion = claim_id in same_source_claim_ids
            if same_source_reassertion:
                competing_weight = max(
                    (
                        _memory()._CLAIM_AUTHORITY_WEIGHT[str(row["authority"])]
                        for row in competing_live
                    ),
                    default=-1,
                )
                promoted = (
                    not competing_live
                    or new_weight > competing_weight
                    or (authority == "operator" and new_weight >= competing_weight)
                )
                _set_status(
                    claim_id,
                    "active" if promoted else "disputed",
                    stamp=stamp,
                    reason="same source published a newer claim version",
                )
                for row in same_source_live:
                    other_id = int(row["id"])
                    if other_id != claim_id:
                        _set_status(
                            other_id, "superseded", stamp=stamp,
                            reason="superseded by a newer version from the same source",
                            related_claim_id=claim_id,
                        )
                if promoted:
                    for row in competing_live:
                        _set_status(
                            int(row["id"]), "superseded", stamp=stamp,
                            reason="superseded by stronger matching claim",
                            related_claim_id=claim_id,
                        )
                elif new_weight == competing_weight:
                    for row in competing_live:
                        if (
                            _memory()._CLAIM_AUTHORITY_WEIGHT[str(row["authority"])]
                            == new_weight
                        ):
                            _set_status(
                                int(row["id"]), "disputed", stamp=stamp,
                                reason="equal-authority values conflict",
                                related_claim_id=claim_id,
                            )
            elif new_weight > strongest_weight or (
                authority == "operator" and new_weight >= strongest_weight
            ):
                _set_status(
                    claim_id, "active", stamp=stamp,
                    reason="matching claim promoted by stronger evidence",
                )
                for row in live:
                    other_id = int(row["id"])
                    if other_id != claim_id:
                        _set_status(
                            other_id, "superseded", stamp=stamp,
                            reason="superseded by stronger matching claim",
                            related_claim_id=claim_id,
                        )
        else:
            canonical_content, keyed_content = _memory().backing_content_variants(
                subject, predicate, value, scope, stamp, claim_key
            )
            content = canonical_content
            if self.db.execute(
                """SELECT 1 FROM memories AS m
                   JOIN memory_claims AS c ON c.memory_id=m.id
                   WHERE m.kind='claim' AND m.content=? AND c.claim_key<>?
                   LIMIT 1""",
                (canonical_content, claim_key),
            ).fetchone() is not None:
                # Another claim key already renders this exact content and owns
                # the backing row; reusing it would violate
                # UNIQUE(memory_claims.memory_id) and lose this fact.
                content = keyed_content
            backing_source = f"{authority}:{source}"[:2_000]
            competing_weight = max(
                (
                    _memory()._CLAIM_AUTHORITY_WEIGHT[str(row["authority"])]
                    for row in competing_live
                ),
                default=-1,
            )
            if not competing_live or new_weight > competing_weight or (
                authority == "operator" and new_weight == competing_weight
            ):
                status = "active"
            else:
                status = "disputed"
            supersedes_id = None
            if same_source_live:
                predecessor = max(same_source_live, key=lambda row: int(row["id"]))
                supersedes_id = int(predecessor["id"])
            elif status == "active" and competing_live:
                strongest = max(
                    competing_live,
                    key=lambda row: (
                        _memory()._CLAIM_AUTHORITY_WEIGHT[str(row["authority"])], int(row["id"])
                    ),
                )
                supersedes_id = int(strongest["id"])
            event_reason = (
                "new strongest claim" if status == "active" else "conflicts with stronger claim"
            )
            if self._spine_ready:
                claim_id = _memory().memory_spine.allocate_claim_id(self.db)
                context = self._spine_context(actor, conversation_id, permission)
                spine_event_id = _memory().memory_spine.append_event(
                    self.db,
                    self._spine_key,
                    kind="claim.created",
                    actor=context["actor"],
                    source=source,
                    scope=scope,
                    permission=context["permission"],
                    outcome="applied",
                    payload=_memory().memory_spine.claim_event_payload(
                        {
                            "claim_key": claim_key, "subject": subject,
                            "predicate": predicate, "value": value,
                            "value_sha256": value_sha256, "source": source,
                            "authority": authority, "confidence": confidence,
                            "status": status, "valid_from": stamp,
                            "valid_until": None, "supersedes_id": supersedes_id,
                        },
                        at=stamp,
                    ),
                    now=stamp,
                    conversation_id=context["conversation_id"],
                    subject_kind="claim",
                    subject_id=claim_id,
                )
                # The backing row carries the claim's creating event as its
                # lineage (design 12.6 item 1): claim id -> claim.created ->
                # backing memory row -> claim row.
                memory_row = self.db.execute(
                    "SELECT id FROM memories WHERE kind='claim' AND content=?", (content,)
                ).fetchone()
                if memory_row is None:
                    memory_id = _memory().memory_spine.allocate_memory_id(self.db)
                    self.db.execute(
                        """INSERT INTO memories(
                               id, created_at, kind, content, source, spine_event_id
                           ) VALUES (?, ?, 'claim', ?, ?, ?)""",
                        (memory_id, stamp, content, backing_source, spine_event_id),
                    )
                else:
                    # A legacy orphan backing row with this exact content is
                    # reused; its lineage becomes this claim's event.
                    memory_id = int(memory_row["id"])
                    self.db.execute(
                        "UPDATE memories SET source=?, spine_event_id=? WHERE id=?",
                        (backing_source, spine_event_id, memory_id),
                    )
                self.db.execute(
                    """INSERT INTO memory_claims(
                           id, memory_id, created_at, updated_at, scope, claim_key, subject,
                           predicate, value, value_sha256, source, authority,
                           confidence, status, valid_from, valid_until, supersedes_id,
                           spine_event_id
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)""",
                    (
                        claim_id, memory_id, stamp, stamp, scope, claim_key,
                        subject, predicate, value, value_sha256, source, authority,
                        confidence, status, stamp, supersedes_id, spine_event_id,
                    ),
                )
                self.db.execute(
                    """INSERT INTO memory_claim_events(
                           claim_id, created_at, status, reason, related_claim_id, spine_event_id
                       ) VALUES (?, ?, ?, ?, ?, ?)""",
                    (claim_id, stamp, status, event_reason, supersedes_id, spine_event_id),
                )
            else:
                self.db.execute(
                    """INSERT OR IGNORE INTO memories(created_at, kind, content, source)
                       VALUES (?, 'claim', ?, ?)""",
                    (stamp, content, backing_source),
                )
                memory_row = self.db.execute(
                    "SELECT id FROM memories WHERE kind='claim' AND content=?", (content,)
                ).fetchone()
                if memory_row is None:
                    raise RuntimeError("Temporal claim memory could not be persisted")
                memory_id = int(memory_row["id"])
                cursor = self.db.execute(
                    """INSERT INTO memory_claims(
                           memory_id, created_at, updated_at, scope, claim_key, subject,
                           predicate, value, value_sha256, source, authority,
                           confidence, status, valid_from, valid_until, supersedes_id
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)""",
                    (
                        memory_id, stamp, stamp, scope, claim_key, subject,
                        predicate, value, value_sha256, source, authority,
                        confidence, status, stamp, supersedes_id,
                    ),
                )
                claim_id = int(cursor.lastrowid)
                self.db.execute(
                    """INSERT INTO memory_claim_events(
                           claim_id, created_at, status, reason, related_claim_id
                       ) VALUES (?, ?, ?, ?, ?)""",
                    (claim_id, stamp, status, event_reason, supersedes_id),
                )
            if self._graph_ready:
                # One edge per claim version, inside this write's own
                # transaction (design 4.5).  An excluded claim (reserved
                # predicate, private subject, over-long subject) projects
                # nothing and is counted by ``verify_graph``.
                _memory().memory_graph.project_claim(
                    self.db, self._graph_claim_row_locked(claim_id), now=stamp
                )
            for row in same_source_live:
                _set_status(
                    int(row["id"]), "superseded", stamp=stamp,
                    reason="superseded by a newer version from the same source",
                    related_claim_id=claim_id,
                )
            if status == "active":
                for row in competing_live:
                    _set_status(
                        int(row["id"]), "superseded", stamp=stamp,
                        reason="replaced by newer authoritative claim",
                        related_claim_id=claim_id,
                    )
            elif new_weight == competing_weight:
                for row in competing_live:
                    if _memory()._CLAIM_AUTHORITY_WEIGHT[str(row["authority"])] == new_weight:
                        _set_status(
                            int(row["id"]), "disputed", stamp=stamp,
                            reason="equal-authority values conflict",
                            related_claim_id=claim_id,
                        )

        self.db.execute(
            """INSERT OR IGNORE INTO memory_claim_evidence(
                   claim_id, created_at, source, authority, confidence, evidence_sha256
               ) VALUES (?, ?, ?, ?, ?, ?)""",
            (claim_id, stamp, source, authority, confidence, evidence_sha256),
        )
        if scope == "global":
            # The existing learned claim clock is global. Project facts must not
            # alter another project's volatility estimate through a shared
            # predicate, so this bounded M1 lane keeps explicit project claims
            # out of the clock until the clock itself has first-class scopes.
            self._record_claim_observation_locked(
                claim_id,
                claim_key=claim_key,
                predicate=predicate,
                value_sha256=value_sha256,
                source_identity=source_identity,
                authority=authority,
                confidence=confidence,
                stamp=stamp,
            )
        return claim_id

    def remember_claim(
        self,
        subject: str,
        predicate: str,
        value: str,
        *,
        source: str,
        authority: str,
        confidence: float = 1.0,
        source_identity: str | None = None,
        actor: str = "runtime",
        conversation_id: int | None = None,
        permission: str = "runtime",
    ) -> int:
        """Record a global versioned fact; authority is a closed runtime enum.

        Project-scoped facts intentionally have no generic public write option.
        They must cross ``remember_explicit_project_claim`` so the operator
        command, project binding, fixed provenance, and claim write are checked
        and committed atomically.
        """
        subject = _memory()._validated_nonsecret_metadata(subject, "Claim subject")
        predicate = _memory()._validated_nonsecret_metadata(predicate, "Claim predicate")
        if _memory()._claim_has_sensitive_key(subject, predicate):
            raise ValueError(
                "Claim subject or predicate must not name a credential or secret"
            )
        value = _memory().redact_secrets(str(value).strip())
        source = _memory()._validated_nonsecret_metadata(source, "Claim source")
        authority = str(authority).strip().casefold()
        if authority not in _memory().CLAIM_AUTHORITIES:
            raise ValueError("Unknown claim authority")
        if not subject or len(subject) > 500:
            raise ValueError("Claim subject must contain 1-500 characters")
        if not predicate or len(predicate) > 200:
            raise ValueError("Claim predicate must contain 1-200 characters")
        if not value or len(value) > 4_000:
            raise ValueError("Claim value must contain 1-4,000 characters")
        if not source or len(source) > 500:
            raise ValueError("Claim source must contain 1-500 characters")
        if source_identity is not None:
            source_identity = _memory()._validated_nonsecret_metadata(
                source_identity, "Claim source identity"
            )
            if not source_identity or len(source_identity) > 500:
                raise ValueError("Claim source identity must contain 1-500 characters")
        confidence = float(confidence)
        if not _memory().math.isfinite(confidence):
            raise ValueError("Claim confidence must be finite")
        confidence = max(0.0, min(confidence, 1.0))
        with self._immediate_transaction():
            return self._remember_claim_locked(
                subject, predicate, value, source=source, authority=authority,
                confidence=confidence, stamp=_memory().now_iso(),
                source_identity=source_identity,
                scope="global",
            actor=actor,
            conversation_id=conversation_id,
            permission=permission,
            )

    def remember_explicit_project_claim(
        self,
        conversation_id: int,
        project_id: int,
        operator_prompt: str,
        *,
        permission: str = "operator:interactive",
    ) -> dict[str, Any]:
        """Atomically persist one exact operator-authored project fact.

        Parsing is repeated at the storage boundary so a caller cannot provide
        model-inferred fields, authority, source, confidence, or scope.  The
        operator message, versioned claim, and fixed assistant receipt commit
        or roll back together.
        """
        parsed = _memory().parse_explicit_project_fact(operator_prompt)
        if parsed is None:
            raise ValueError("Prompt is not an explicit project fact command")
        normalized_project = self._project_id(project_id)
        scope = _memory().project_claim_scope(normalized_project)
        if (
            isinstance(conversation_id, bool)
            or not isinstance(conversation_id, int)
            or conversation_id <= 0
        ):
            raise ValueError("conversation_id must be a positive integer")
        subject = parsed["subject"]
        predicate = parsed["predicate"]
        value = parsed["value"]
        claim_key = self._claim_identity(subject, predicate)
        stamp = _memory().now_iso()
        with self._immediate_transaction():
            project = self.db.execute(
                """SELECT p.id, p.enabled
                   FROM conversations AS c
                   JOIN agent_projects AS p ON p.id=c.project_id
                   WHERE c.id=?
                     AND NOT EXISTS (
                         SELECT 1 FROM screen_companion_conversations AS companion
                         WHERE companion.conversation_id=c.id
                     )""",
                (conversation_id,),
            ).fetchone()
            if (
                project is None
                or int(project["id"]) != normalized_project
                or not bool(project["enabled"])
            ):
                raise ValueError(
                    "Conversation project does not exist, is disabled, or mismatched"
                )
            before = self.db.execute(
                """SELECT id, status
                   FROM memory_claims
                   WHERE scope=? AND claim_key=?
                   ORDER BY id""",
                (scope, claim_key),
            ).fetchall()
            before_ids = {int(row["id"]) for row in before}
            before_live_ids = {
                int(row["id"])
                for row in before
                if str(row["status"]) in {"active", "disputed"}
            }
            self.db.execute(
                """INSERT INTO messages(conversation_id, created_at, role, content)
                   VALUES (?, ?, 'user', ?)""",
                (conversation_id, stamp, str(operator_prompt).strip()),
            )
            claim_id = self._remember_claim_locked(
                subject,
                predicate,
                value,
                source="explicit operator project fact",
                authority="operator",
                confidence=1.0,
                stamp=stamp,
                source_identity=f"operator:{scope}",
                scope=scope,
                actor="operator",
                conversation_id=conversation_id,
                permission=permission,
            )
            stored = self.db.execute(
                """SELECT status FROM memory_claims
                   WHERE id=? AND scope=? AND claim_key=?""",
                (claim_id, scope, claim_key),
            ).fetchone()
            if stored is None or str(stored["status"]) != "active":
                raise RuntimeError("Project claim did not become the active version")
            remaining_live = {
                int(row["id"])
                for row in self.db.execute(
                    """SELECT id FROM memory_claims
                       WHERE scope=? AND claim_key=?
                         AND status IN ('active', 'disputed')""",
                    (scope, claim_key),
                ).fetchall()
            }
            if remaining_live != {claim_id}:
                raise RuntimeError("Project claim supersession did not resolve uniquely")
            if claim_id in before_live_ids:
                action = "reasserted"
            elif before_live_ids:
                action = "superseded"
            elif claim_id in before_ids:
                action = "reasserted"
            else:
                action = "created"
            action_text = {
                "created": "Stored",
                "reasserted": "Reasserted",
                "superseded": "Updated",
            }[action]
            history_note = (
                " The prior value remains in this project's version history."
                if action == "superseded"
                else ""
            )
            assistant_message = (
                f"{action_text} project fact (claim record #{claim_id})."
                f"{history_note}"
            )
            assistant_cursor = self.db.execute(
                """INSERT INTO messages(conversation_id, created_at, role, content)
                   VALUES (?, ?, 'assistant', ?)""",
                (conversation_id, stamp, assistant_message),
            )
            return {
                "project_id": normalized_project,
                "scope": scope,
                "claim_id": claim_id,
                "action": action,
                "assistant_message_id": int(assistant_cursor.lastrowid),
                "assistant_message": assistant_message,
                "subject": subject,
                "predicate": predicate,
                "value": value,
                "authority": "operator",
                "confidence": 1.0,
            }

    @_with_recall_cache
    def current_claims(
        self,
        query: str = "",
        limit: int = 8,
        *,
        clock_mode: str = "disabled",
        stale_threshold: float = 0.70,
        as_of: str | None = None,
        project_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """Return current claims with optional shadow/enforced confidence aging.

        The read runs in one deferred snapshot and never takes the write lock.
        Clock telemetry is persisted afterwards, best-effort, so a concurrent
        writer can never turn a foreground read into an error.
        """
        self._pending_claim_clock_updates = []
        items = self._current_claims_read(
            query,
            limit,
            clock_mode=clock_mode,
            stale_threshold=stale_threshold,
            as_of=as_of,
            project_id=project_id,
        )
        self._record_claim_clock_reads()
        return items

    def claim_history(
        self,
        subject: str,
        predicate: str,
        *,
        as_of: str | None = None,
        project_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """Return versions from exactly one global or project claim scope."""
        subject = _memory()._validated_nonsecret_metadata(subject, "Claim subject")
        predicate = _memory()._validated_nonsecret_metadata(predicate, "Claim predicate")
        claim_key = self._claim_identity(subject, predicate)
        scope = (
            "global"
            if project_id is None
            else _memory().project_claim_scope(self._project_id(project_id))
        )
        if project_id is not None:
            project = self.db.execute(
                "SELECT enabled FROM agent_projects WHERE id=?",
                (self._project_id(project_id),),
            ).fetchone()
            if project is None or not bool(project["enabled"]):
                return []
        if as_of is None:
            rows = self.db.execute(
                """SELECT id AS claim_id, memory_id, scope, created_at, updated_at,
                          subject, predicate, value, source, authority, confidence,
                          status, valid_from, valid_until, supersedes_id
                   FROM memory_claims
                   WHERE scope=? AND claim_key=? ORDER BY id""",
                (scope, claim_key),
            ).fetchall()
            return [dict(row) for row in rows]
        try:
            parsed = _memory().datetime.fromisoformat(str(as_of).replace("Z", "+00:00"))
        except ValueError:
            raise ValueError("as_of must be an ISO-8601 timestamp") from None
        stamp = _memory()._as_utc(parsed).isoformat()
        rows = self.db.execute(
            """SELECT c.id AS claim_id, c.memory_id, c.scope,
                      c.created_at, c.updated_at,
                      c.subject, c.predicate, c.value, c.source, c.authority,
                      c.confidence,
                      (SELECT e.status FROM memory_claim_events AS e
                       WHERE e.claim_id=c.id AND e.created_at<=?
                       ORDER BY e.created_at DESC, e.id DESC LIMIT 1) AS status,
                      c.valid_from, c.valid_until, c.supersedes_id
               FROM memory_claims AS c
               WHERE c.scope=? AND c.claim_key=? AND c.created_at<=?
                 AND EXISTS (
                     SELECT 1 FROM memory_claim_events AS e
                     WHERE e.claim_id=c.id AND e.created_at<=?
                 )
               ORDER BY c.id""",
            (stamp, scope, claim_key, stamp, stamp),
        ).fetchall()
        return [
            dict(row) for row in rows
            if str(row["status"]) in {"active", "disputed"}
        ]
