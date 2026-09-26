"""Current Memory domain extracted without changing method control flow.

Global dependencies resolve through jarvis.memory at call time. The facade keeps
public imports, patch seams, constants and authorization helpers authoritative.
"""
from __future__ import annotations

from collections.abc import Mapping
from collections.abc import Sequence
from typing import Any
import sqlite3
from .memory_runtime import (_memory, _with_read_snapshot)


class LessonsMemoryMixin:
    """Mechanically extracted current Memory methods."""

    @staticmethod
    def _canonical_utc_timestamp(value: Any) -> str | None:
        try:
            parsed = _memory().datetime.fromisoformat(str(value))
        except (TypeError, ValueError):
            return None
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        return parsed.astimezone(_memory().timezone.utc).isoformat()

    def _lesson_project_for_context(
        self,
        task_id: Any,
        conversation_id: Any,
    ) -> int | None:
        try:
            if task_id is not None:
                row = self.db.execute(
                    "SELECT project_id FROM tasks WHERE id=?", (int(task_id),)
                ).fetchone()
            elif conversation_id is not None:
                row = self.db.execute(
                    "SELECT project_id FROM conversations WHERE id=?",
                    (int(conversation_id),),
                ).fetchone()
            else:
                return None
        except (_memory().sqlite3.DatabaseError, TypeError, ValueError):
            return None
        if row is None:
            return None
        try:
            return self._project_id(int(row["project_id"] or 1))
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _lesson_control_material(
        *,
        memory_id: int,
        prediction_id: int,
        reflection_id: int,
        content_sha256: str,
        provenance_sha256: str,
        project_id: int,
        observed_at: str,
        valid_until: str,
        lifecycle_status: str,
        superseded_by: int | None,
    ) -> dict[str, Any]:
        return {
            "schema": "jarvis.lesson-control.v1",
            "memory_id": int(memory_id),
            "prediction_id": int(prediction_id),
            "reflection_id": int(reflection_id),
            "content_sha256": str(content_sha256),
            "provenance_sha256": str(provenance_sha256),
            "project_id": int(project_id),
            "observed_at": str(observed_at),
            "valid_until": str(valid_until),
            "lifecycle_status": str(lifecycle_status),
            "superseded_by": (
                None if superseded_by is None else int(superseded_by)
            ),
        }

    @staticmethod
    def _lesson_control_digest(material: dict[str, Any]) -> str:
        canonical = _memory().json.dumps(
            material,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return _memory().hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _lesson_control_validation(
        self,
        memory_id: int,
        *,
        project_id: int | None = None,
        as_of: str | None = None,
    ) -> tuple[bool, str]:
        """Validate project, freshness, lifecycle, and an internal integrity receipt."""
        try:
            row = self.db.execute(
                """SELECT lc.memory_id, lc.project_id, lc.observed_at,
                          lc.valid_until, lc.lifecycle_status, lc.superseded_by,
                          lc.control_sha256, lp.prediction_id, lp.reflection_id,
                          lp.content_sha256, lp.provenance_sha256
                   FROM lesson_controls AS lc
                   JOIN lesson_provenance AS lp ON lp.memory_id=lc.memory_id
                   WHERE lc.memory_id=?
                   ORDER BY lp.verified_at DESC, lp.prediction_id DESC LIMIT 1""",
                (int(memory_id),),
            ).fetchone()
        except (_memory().sqlite3.DatabaseError, TypeError, ValueError):
            return False, "control_unavailable"
        if row is None:
            return False, "control_missing"
        try:
            normalized_project = (
                None if project_id is None else self._project_id(project_id)
            )
            observed_at = self._canonical_utc_timestamp(row["observed_at"])
            valid_until = self._canonical_utc_timestamp(row["valid_until"])
            current_at = self._canonical_utc_timestamp(as_of or _memory().now_iso())
            if None in {observed_at, valid_until, current_at}:
                return False, "timestamp_invalid"
            observed = _memory().datetime.fromisoformat(str(observed_at))
            expires = _memory().datetime.fromisoformat(str(valid_until))
            current = _memory().datetime.fromisoformat(str(current_at))
            if observed > current + _memory().timedelta(minutes=5):
                return False, "observed_in_future"
            if expires <= observed:
                return False, "validity_invalid"
            if current > expires:
                return False, "expired"
            if expires - observed > _memory().timedelta(days=_memory().LESSON_DEFAULT_TTL_DAYS):
                # Every trusted write path bounds validity to the default TTL
                # window from observation.  A longer window can only come from
                # tampering, even when the unkeyed integrity digest was
                # recomputed to match, so fail closed instead of honoring it.
                return False, "validity_invalid"
            if normalized_project is not None and int(row["project_id"]) != normalized_project:
                return False, "project_mismatch"
            status = str(row["lifecycle_status"])
            superseded_by = row["superseded_by"]
            if status != "active" or superseded_by is not None:
                return False, status
            material = self._lesson_control_material(
                memory_id=int(row["memory_id"]),
                prediction_id=int(row["prediction_id"]),
                reflection_id=int(row["reflection_id"]),
                content_sha256=str(row["content_sha256"]),
                provenance_sha256=str(row["provenance_sha256"] or ""),
                project_id=int(row["project_id"]),
                observed_at=str(observed_at),
                valid_until=str(valid_until),
                lifecycle_status=status,
                superseded_by=(
                    None if superseded_by is None else int(superseded_by)
                ),
            )
            if str(row["control_sha256"] or "") != self._lesson_control_digest(material):
                return False, "control_digest_mismatch"
        except (OverflowError, TypeError, ValueError):
            return False, "control_invalid"
        return True, "active"

    def _lesson_application_values(
        self,
        *,
        family: str,
        application_created_at: Any,
        application_resolved_at: Any,
        application_successful: Any,
        prediction_created_at: Any,
        prediction_resolved_at: Any,
        prediction_actual_status: Any,
        prediction_evidence_ok: Any,
        prediction_verification: Any,
        lesson_observed_at: Any,
        lesson_valid_until: Any,
        validation_at: Any = None,
    ) -> tuple[str, str | None, int | None] | None:
        """Reconcile one application row with its prediction and lesson window.

        The application ledger is derived evidence, never an authority of its
        own.  A row is usable only when its timestamps fit the observation and
        prediction timelines and its terminal fields exactly mirror the bound
        prediction.  Returning canonical values also prevents textual timestamp
        variants from surviving a migration as distinct claims.
        """
        if family not in self.PREDICTION_FAMILIES:
            return None
        verification = str(prediction_verification or "")
        if verification not in self.PREDICTION_VERIFICATION:
            return None
        if (
            family in _memory().LESSON_EVIDENCE_REQUIRED_FAMILIES
            and verification == "not_applicable"
        ):
            return None
        app_created_at = self._canonical_utc_timestamp(application_created_at)
        prediction_created_at = self._canonical_utc_timestamp(
            prediction_created_at
        )
        lesson_observed_at = self._canonical_utc_timestamp(lesson_observed_at)
        lesson_valid_until = self._canonical_utc_timestamp(lesson_valid_until)
        validation_at = self._canonical_utc_timestamp(validation_at or _memory().now_iso())
        if None in {
            app_created_at,
            prediction_created_at,
            lesson_observed_at,
            lesson_valid_until,
            validation_at,
        }:
            return None
        try:
            app_created = _memory().datetime.fromisoformat(str(app_created_at))
            prediction_created = _memory().datetime.fromisoformat(str(prediction_created_at))
            lesson_observed = _memory().datetime.fromisoformat(str(lesson_observed_at))
            lesson_expires = _memory().datetime.fromisoformat(str(lesson_valid_until))
            validation_time = _memory().datetime.fromisoformat(str(validation_at))
        except (TypeError, ValueError):
            return None
        if (
            lesson_expires <= lesson_observed
            or app_created < lesson_observed
            or app_created > lesson_expires
            or app_created < prediction_created
            or app_created > validation_time + _memory().timedelta(minutes=5)
        ):
            return None

        raw_prediction_resolved_at = prediction_resolved_at
        raw_application_resolved_at = application_resolved_at
        prediction_resolved_at = (
            None
            if raw_prediction_resolved_at is None
            else self._canonical_utc_timestamp(raw_prediction_resolved_at)
        )
        application_resolved_at = (
            None
            if raw_application_resolved_at is None
            else self._canonical_utc_timestamp(raw_application_resolved_at)
        )
        if (
            raw_prediction_resolved_at is not None
            and prediction_resolved_at is None
        ) or (
            raw_application_resolved_at is not None
            and application_resolved_at is None
        ):
            return None
        if prediction_resolved_at is None:
            if (
                prediction_actual_status is not None
                or prediction_evidence_ok is not None
                or application_resolved_at is not None
                or application_successful is not None
            ):
                return None
            return str(app_created_at), None, None

        status = str(prediction_actual_status or "")
        if (
            application_resolved_at is None
            or str(application_resolved_at) != str(prediction_resolved_at)
            or status not in {"complete", "incomplete", "failed"}
            or isinstance(application_successful, bool)
            or not isinstance(application_successful, int)
            or application_successful not in {0, 1}
            or int(application_successful) != int(status == "complete")
        ):
            return None
        try:
            resolved = _memory().datetime.fromisoformat(str(application_resolved_at))
        except (TypeError, ValueError):
            return None
        if resolved < app_created or resolved < prediction_created:
            return None
        if resolved > validation_time + _memory().timedelta(minutes=5):
            return None
        if prediction_evidence_ok not in {None, 0, 1}:
            return None
        if status == "complete" and verification != "not_applicable":
            if prediction_evidence_ok != 1:
                return None
        return (
            str(app_created_at),
            str(application_resolved_at),
            int(application_successful),
        )

    @staticmethod
    def _canonical_reflection_lesson_content(
        *,
        family: str,
        outcome_status: str,
        summary: str,
        mistakes: str,
        improvements: str,
        project_id: int | None = None,
        reflection_id: int | None = None,
    ) -> str | None:
        """Reconstruct canonical lesson text, optionally bound to one observation."""
        reusable = str(improvements).strip()
        if not reusable:
            return None
        if (project_id is None) != (reflection_id is None):
            raise ValueError("Lesson scope requires both project and reflection IDs")
        parts = [
            f"Task family: {family}.",
            f"Observed outcome: {outcome_status}.",
        ]
        if str(summary):
            parts.append(f"Observed result: {summary}")
        if str(mistakes):
            parts.append(f"Observed blocker: {mistakes}")
        if project_id is not None and reflection_id is not None:
            parts.append(
                f"Evidence scope: project {int(project_id)}; "
                f"reflection {int(reflection_id)}."
            )
        parts.append(f"Reusable lesson: {reusable}")
        return "\n".join(parts)

    def _lesson_provenance_material(
        self,
        memory_id: int,
        prediction_id: int,
        reflection_id: int,
    ) -> dict[str, Any] | None:
        """Return canonical source material only for one internally exact chain."""
        lesson = self.db.execute(
            """SELECT id, kind, content, source, family, outcome_status, reflection_id
               FROM memories WHERE id=?""",
            (int(memory_id),),
        ).fetchone()
        if lesson is None or str(lesson["kind"]) != "lesson":
            return None
        return self._lesson_provenance_material_from_fields(
            lesson, prediction_id, reflection_id
        )

    def _lesson_provenance_material_from_fields(
        self,
        lesson: Mapping[str, Any],
        prediction_id: int,
        reflection_id: int,
    ) -> dict[str, Any] | None:
        """The provenance material for a lesson row given as its fields
        (``id``, ``kind``, ``content``, ``source``, ``family``,
        ``outcome_status``, ``reflection_id``), so the digest can be computed
        before the row exists: its ``lesson.created`` event must carry the
        digest and must precede the insert."""
        if str(lesson["kind"]) != "lesson":
            return None
        chain = self.db.execute(
            """SELECT
                   r.id AS reflection_id, r.created_at AS reflection_created_at,
                   r.task_id AS reflection_task_id,
                   r.conversation_id AS reflection_conversation_id,
                   r.prediction_id AS reflection_prediction_id,
                   r.status AS reflection_status, r.summary AS reflection_summary,
                   r.mistakes AS reflection_mistakes,
                   r.improvements AS reflection_improvements,
                   r.tool_calls AS reflection_tool_calls,
                   p.id AS prediction_id, p.created_at AS prediction_created_at,
                   p.task_id AS prediction_task_id,
                   p.conversation_id AS prediction_conversation_id,
                   p.origin AS prediction_origin, p.family AS prediction_family,
                   p.profile AS prediction_profile, p.model AS prediction_model,
                   p.predicted_success, p.predicted_steps,
                   p.predicted_verification, p.basis AS prediction_basis,
                   p.resolved_at AS prediction_resolved_at,
                   p.actual_status AS prediction_actual_status,
                   p.actual_steps AS prediction_actual_steps,
                   p.evidence_ok AS prediction_evidence_ok,
                   p.failure_class AS prediction_failure_class
               FROM reflections AS r
               JOIN task_predictions AS p ON p.id=?
               WHERE r.id=?""",
            (int(prediction_id), int(reflection_id)),
        ).fetchone()
        if chain is None:
            return None
        row: dict[str, Any] = {name: chain[name] for name in chain.keys()}
        row.update({
            "memory_id": int(lesson["id"]),
            "lesson_kind": str(lesson["kind"]),
            "lesson_content": lesson["content"],
            "lesson_source": lesson["source"],
            "lesson_family": lesson["family"],
            "lesson_outcome_status": lesson["outcome_status"],
            "lesson_reflection_id": lesson["reflection_id"],
        })
        durable_text = "\n".join((
            str(row["lesson_content"] or ""),
            str(row["reflection_summary"] or ""),
            str(row["reflection_mistakes"] or ""),
            str(row["reflection_improvements"] or ""),
        ))
        if _memory().contains_secret(durable_text) or _memory().contains_private_identifier(durable_text):
            return None
        project_id = self._lesson_project_for_context(
            row["prediction_task_id"], row["prediction_conversation_id"]
        )
        if project_id is None:
            return None
        legacy_expected_content = self._canonical_reflection_lesson_content(
            family=str(row["prediction_family"] or ""),
            outcome_status=str(row["reflection_status"] or ""),
            summary=str(row["reflection_summary"] or ""),
            mistakes=str(row["reflection_mistakes"] or ""),
            improvements=str(row["reflection_improvements"] or ""),
        )
        scoped_expected_content = self._canonical_reflection_lesson_content(
            family=str(row["prediction_family"] or ""),
            outcome_status=str(row["reflection_status"] or ""),
            summary=str(row["reflection_summary"] or ""),
            mistakes=str(row["reflection_mistakes"] or ""),
            improvements=str(row["reflection_improvements"] or ""),
            project_id=project_id,
            reflection_id=int(reflection_id),
        )
        if (
            legacy_expected_content is None
            or scoped_expected_content is None
            or str(row["lesson_content"])
            not in {legacy_expected_content, scoped_expected_content}
            or row["lesson_reflection_id"] is None
            or int(row["lesson_reflection_id"]) != int(reflection_id)
            or row["reflection_prediction_id"] is None
            or int(row["reflection_prediction_id"]) != int(prediction_id)
            or str(row["lesson_family"] or "")
            != str(row["prediction_family"] or "")
            or str(row["lesson_outcome_status"] or "")
            != str(row["reflection_status"] or "")
            or str(row["reflection_status"] or "")
            != str(row["prediction_actual_status"] or "")
            or row["prediction_resolved_at"] is None
            or str(row["prediction_resolved_at"]) > str(row["reflection_created_at"])
            or row["prediction_actual_steps"] is None
            or int(row["prediction_actual_steps"])
            != int(row["reflection_tool_calls"])
            or str(row["prediction_origin"])
            not in _memory().LESSON_REUSABLE_PREDICTION_ORIGINS
            or (
                str(row["prediction_family"])
                in _memory().LESSON_EVIDENCE_REQUIRED_FAMILIES
                and str(row["predicted_verification"]) == "not_applicable"
            )
        ):
            return None
        if row["reflection_task_id"] is not None:
            if (
                row["prediction_task_id"] is None
                or int(row["prediction_task_id"]) != int(row["reflection_task_id"])
            ):
                return None
        elif row["reflection_conversation_id"] is not None:
            if (
                row["prediction_task_id"] is not None
                or row["prediction_conversation_id"] is None
                or int(row["prediction_conversation_id"])
                != int(row["reflection_conversation_id"])
            ):
                return None
        else:
            return None
        if str(row["lesson_outcome_status"]) == "complete" and (
            str(row["predicted_verification"]) != "not_applicable"
            and int(row["prediction_evidence_ok"] or 0) != 1
        ):
            return None
        return {
            "schema": "jarvis.lesson-provenance.v1",
            "lesson": {
                "memory_id": int(row["memory_id"]),
                "content": str(row["lesson_content"]),
                "source": row["lesson_source"],
                "family": str(row["lesson_family"]),
                "outcome_status": str(row["lesson_outcome_status"]),
                "reflection_id": int(row["lesson_reflection_id"]),
            },
            "reflection": {
                "id": int(row["reflection_id"]),
                "created_at": str(row["reflection_created_at"]),
                "task_id": row["reflection_task_id"],
                "conversation_id": row["reflection_conversation_id"],
                "prediction_id": int(row["reflection_prediction_id"]),
                "status": str(row["reflection_status"]),
                "summary": str(row["reflection_summary"]),
                "mistakes": str(row["reflection_mistakes"]),
                "improvements": str(row["reflection_improvements"]),
                "tool_calls": int(row["reflection_tool_calls"]),
            },
            "prediction": {
                "id": int(row["prediction_id"]),
                "created_at": str(row["prediction_created_at"]),
                "task_id": row["prediction_task_id"],
                "conversation_id": row["prediction_conversation_id"],
                "origin": str(row["prediction_origin"]),
                "family": str(row["prediction_family"]),
                "profile": str(row["prediction_profile"]),
                "model": str(row["prediction_model"]),
                "predicted_success": float(row["predicted_success"]),
                "predicted_steps": int(row["predicted_steps"]),
                "predicted_verification": str(row["predicted_verification"]),
                "basis": str(row["prediction_basis"]),
                "resolved_at": str(row["prediction_resolved_at"]),
                "actual_status": str(row["prediction_actual_status"]),
                "actual_steps": int(row["prediction_actual_steps"]),
                "evidence_ok": (
                    None if row["prediction_evidence_ok"] is None
                    else int(row["prediction_evidence_ok"])
                ),
                "failure_class": row["prediction_failure_class"],
            },
        }

    @staticmethod
    def _lesson_provenance_digest(material: dict[str, Any]) -> str:
        canonical = _memory().json.dumps(
            material,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return _memory().hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _lesson_provenance_validation(
        self,
        memory_id: int,
    ) -> tuple[bool, bool, bool]:
        """Return (valid, content mismatch, chain/digest mismatch)."""
        try:
            rows = self.db.execute(
                """SELECT prediction_id, memory_id, reflection_id, content_sha256,
                          provenance_sha256
                   FROM lesson_provenance WHERE memory_id=?
                   ORDER BY verified_at DESC, prediction_id DESC""",
                (int(memory_id),),
            ).fetchall()
            content_row = self.db.execute(
                "SELECT content FROM memories WHERE id=? AND kind='lesson'",
                (int(memory_id),),
            ).fetchone()
        except (_memory().sqlite3.DatabaseError, TypeError, ValueError):
            return False, False, True
        if content_row is None:
            return False, False, bool(rows)
        observed_content_hash = _memory().hashlib.sha256(
            str(content_row["content"]).encode("utf-8")
        ).hexdigest()
        content_mismatch = False
        chain_mismatch = not bool(rows)
        for row in rows:
            if str(row["content_sha256"] or "") != observed_content_hash:
                content_mismatch = True
                continue
            material = self._lesson_provenance_material(
                int(row["memory_id"]),
                int(row["prediction_id"]),
                int(row["reflection_id"]),
            )
            if material is None:
                chain_mismatch = True
                continue
            expected = self._lesson_provenance_digest(material)
            if str(row["provenance_sha256"] or "") != expected:
                chain_mismatch = True
                continue
            return True, content_mismatch, chain_mismatch
        return False, content_mismatch, True

    def _lesson_prediction_for_reflection(
        self,
        reflection_id: int,
        *,
        family: str,
        outcome_status: str,
        allow_legacy_inference: bool = False,
        bind_legacy_inference: bool = True,
    ) -> sqlite3.Row | None:
        """Return the one exact resolved prediction that can support a lesson."""
        reflection = self.db.execute(
            """SELECT id, created_at, task_id, conversation_id, prediction_id,
                      status, tool_calls
               FROM reflections WHERE id=?""",
            (int(reflection_id),),
        ).fetchone()
        if reflection is None:
            return None
        if str(reflection["status"]) != outcome_status:
            return None
        task_id = reflection["task_id"]
        conversation_id = reflection["conversation_id"]
        prediction_id = reflection["prediction_id"]
        if prediction_id is None and allow_legacy_inference:
            if task_id is not None:
                candidates = self.db.execute(
                    """SELECT id, task_id, conversation_id, origin, family,
                              predicted_verification, actual_status, actual_steps,
                              evidence_ok, resolved_at
                       FROM task_predictions
                       WHERE task_id=? AND resolved_at IS NOT NULL
                         AND resolved_at<=?
                       ORDER BY resolved_at DESC, id DESC LIMIT 25""",
                    (int(task_id), str(reflection["created_at"])),
                ).fetchall()
            elif conversation_id is not None:
                candidates = self.db.execute(
                    """SELECT id, task_id, conversation_id, origin, family,
                              predicted_verification, actual_status, actual_steps,
                              evidence_ok, resolved_at
                       FROM task_predictions
                       WHERE conversation_id=? AND task_id IS NULL
                         AND resolved_at IS NOT NULL AND resolved_at<=?
                       ORDER BY resolved_at DESC, id DESC LIMIT 25""",
                    (int(conversation_id), str(reflection["created_at"])),
                ).fetchall()
            else:
                candidates = []
            exact_candidates = [
                candidate for candidate in candidates
                if str(candidate["origin"])
                in _memory().LESSON_REUSABLE_PREDICTION_ORIGINS
                and str(candidate["family"]) == family
                and str(candidate["actual_status"]) == outcome_status
                and candidate["actual_steps"] is not None
                and int(candidate["actual_steps"]) == int(reflection["tool_calls"])
                and not (
                    family in _memory().LESSON_EVIDENCE_REQUIRED_FAMILIES
                    and str(candidate["predicted_verification"]) == "not_applicable"
                )
                and (
                    outcome_status != "complete"
                    or str(candidate["predicted_verification"]) == "not_applicable"
                    or int(candidate["evidence_ok"] or 0) == 1
                )
            ]
            if len(exact_candidates) != 1:
                return None
            prediction_id = int(exact_candidates[0]["id"])
            if bind_legacy_inference:
                cursor = self.db.execute(
                    """UPDATE reflections SET prediction_id=?
                       WHERE id=? AND prediction_id IS NULL
                         AND NOT EXISTS (
                             SELECT 1 FROM reflections AS bound
                             WHERE bound.prediction_id=? AND bound.id<>?
                         )""",
                    (
                        prediction_id, int(reflection_id), prediction_id,
                        int(reflection_id),
                    ),
                )
                if cursor.rowcount != 1:
                    return None
        if prediction_id is None:
            return None
        prediction = self.db.execute(
            """SELECT id, task_id, conversation_id, origin, family,
                      predicted_verification, actual_status, actual_steps,
                      evidence_ok, resolved_at
               FROM task_predictions WHERE id=? AND resolved_at IS NOT NULL""",
            (int(prediction_id),),
        ).fetchone()
        if prediction is None:
            return None
        if str(prediction["origin"]) not in _memory().LESSON_REUSABLE_PREDICTION_ORIGINS:
            return None
        if str(prediction["resolved_at"]) > str(reflection["created_at"]):
            return None
        if task_id is not None:
            if prediction["task_id"] is None or int(prediction["task_id"]) != int(task_id):
                return None
        elif conversation_id is not None:
            if (
                prediction["task_id"] is not None
                or prediction["conversation_id"] is None
                or int(prediction["conversation_id"]) != int(conversation_id)
            ):
                return None
        else:
            return None
        if str(prediction["family"]) != family:
            return None
        if (
            family in _memory().LESSON_EVIDENCE_REQUIRED_FAMILIES
            and str(prediction["predicted_verification"]) == "not_applicable"
        ):
            return None
        if str(prediction["actual_status"]) != outcome_status:
            return None
        actual_steps = prediction["actual_steps"]
        if actual_steps is None or int(actual_steps) != int(reflection["tool_calls"]):
            return None
        if outcome_status == "complete" and (
            str(prediction["predicted_verification"]) != "not_applicable"
            and int(prediction["evidence_ok"] or 0) != 1
        ):
            return None
        return prediction

    def remember_verified_lesson(
        self,
        content: str,
        *,
        family: str,
        outcome_status: str,
        reflection_id: int,
        actor: str = "runtime",
        conversation_id: int | None = None,
        permission: str = "runtime",
    ) -> int:
        """Persist a reflection-derived lesson with controlled provenance.

        The row is on the memory spine as ``lesson.created`` carrying the
        provenance digest (computed from the row's fields before the insert);
        lessons have no ordinary provenance row, so ``origin`` and
        ``eligible`` are ``None`` in the payload and the digest is what the
        rebuild verifies.  A duplicate that binds another prediction to the
        same text appends ``memory.reasserted``.  The actor context is a
        receipt only.
        """
        if family not in self.PREDICTION_FAMILIES:
            raise ValueError(f"Unknown lesson family: {family}")
        if outcome_status not in {"complete", "incomplete", "failed"}:
            raise ValueError("Unknown lesson outcome status")
        safe = _memory()._bounded_persisted_text(
            _memory().redact_private_identifiers(str(content).strip()),
            8_000,
            "verified lesson",
        )
        if not safe:
            raise ValueError("Verified lesson must not be empty")
        normalized_reflection = self._prediction_optional_id(
            reflection_id, "reflection_id"
        )
        prediction = self._lesson_prediction_for_reflection(
            int(normalized_reflection),
            family=family,
            outcome_status=outcome_status,
        )
        if prediction is None:
            raise ValueError(
                "Verified lesson requires an exact resolved reflection/prediction outcome"
            )
        reflection = self.db.execute(
            """SELECT created_at, summary, mistakes, improvements
               FROM reflections WHERE id=?""",
            (int(normalized_reflection),),
        ).fetchone()
        if reflection is None:
            raise ValueError("Verified lesson reflection is unavailable")
        project_id = self._lesson_project_for_context(
            prediction["task_id"], prediction["conversation_id"]
        )
        observed_at = self._canonical_utc_timestamp(reflection["created_at"])
        if project_id is None or observed_at is None:
            raise ValueError("Verified lesson lacks a valid project or observation time")
        expected_content = self._canonical_reflection_lesson_content(
            family=family,
            outcome_status=outcome_status,
            summary=str(reflection["summary"] or ""),
            mistakes=str(reflection["mistakes"] or ""),
            improvements=str(reflection["improvements"] or ""),
            project_id=project_id,
            reflection_id=int(normalized_reflection),
        )
        if expected_content is None or safe != expected_content:
            raise ValueError(
                "Verified lesson must be exactly derived from its bound reflection"
            )
        valid_until = (
            _memory().datetime.fromisoformat(observed_at)
            + _memory().timedelta(days=_memory().LESSON_DEFAULT_TTL_DAYS)
        ).isoformat()
        source = (
            f"verified reflection:{normalized_reflection};"
            f"prediction:{int(prediction['id'])}"
        )
        content_sha256 = _memory().hashlib.sha256(safe.encode("utf-8")).hexdigest()
        context = self._spine_context(actor, conversation_id, permission)
        with self._immediate_transaction():
            stamp = _memory().now_iso()
            row = self.db.execute(
                """SELECT id, source, family, outcome_status, reflection_id
                   FROM memories
                   WHERE kind='lesson' AND content=?""",
                (safe,),
            ).fetchone()
            created = row is None
            expected_provenance_sha256: str | None = None
            if created:
                fields = {
                    "kind": "lesson", "content": safe, "source": source,
                    "family": family, "outcome_status": outcome_status,
                    "reflection_id": int(normalized_reflection),
                }
                if self._spine_ready:
                    memory_id = _memory().memory_spine.allocate_memory_id(self.db)
                    # The provenance digest is computed from the row's fields
                    # before the row exists: the lesson event must carry it
                    # and must precede the insert (lineage trigger).
                    material = self._lesson_provenance_material_from_fields(
                        {"id": memory_id, **fields},
                        int(prediction["id"]),
                        int(normalized_reflection),
                    )
                    if material is None:
                        raise ValueError("Verified lesson provenance chain is inconsistent")
                    expected_provenance_sha256 = self._lesson_provenance_digest(material)
                    event_id = self._append_memory_event(
                        "lesson.created",
                        memory_id=memory_id,
                        payload=_memory().memory_spine.memory_event_payload(
                            self._spine_key, fields, origin=None, eligible=None,
                            provenance_sha256=expected_provenance_sha256,
                        ),
                        stamp=stamp,
                        source=source,
                        context=context,
                    )
                    self.db.execute(
                        """INSERT INTO memories(
                               id, created_at, kind, content, source, family,
                               outcome_status, reflection_id, spine_event_id
                           ) VALUES (?, ?, 'lesson', ?, ?, ?, ?, ?, ?)""",
                        (
                            memory_id, stamp, safe, source, family, outcome_status,
                            normalized_reflection, event_id,
                        ),
                    )
                else:
                    self.db.execute(
                        """INSERT INTO memories(
                               created_at, kind, content, source, family,
                               outcome_status, reflection_id
                           ) VALUES (?, 'lesson', ?, ?, ?, ?, ?)""",
                        (
                            stamp, safe, source, family, outcome_status,
                            normalized_reflection,
                        ),
                    )
                row = self.db.execute(
                    """SELECT id, source, family, outcome_status, reflection_id
                       FROM memories
                       WHERE kind='lesson' AND content=?""",
                    (safe,),
                ).fetchone()
            provenance_inserted = False
            provenance_sha256 = ""
            if row is not None and (
                str(row["family"] or "") != family
                or str(row["outcome_status"] or "") != outcome_status
                or row["reflection_id"] is None
                or int(row["reflection_id"]) != int(normalized_reflection)
            ):
                raise ValueError(
                    "Existing lesson text has different family, outcome, or reflection provenance"
                )
            if row is not None:
                material = self._lesson_provenance_material(
                    int(row["id"]),
                    int(prediction["id"]),
                    int(normalized_reflection),
                )
                if material is None:
                    raise ValueError("Verified lesson provenance chain is inconsistent")
                provenance_sha256 = self._lesson_provenance_digest(material)
                if (
                    expected_provenance_sha256 is not None
                    and provenance_sha256 != expected_provenance_sha256
                ):
                    raise RuntimeError(
                        "Verified lesson provenance digest changed after insert"
                    )
                existing = self.db.execute(
                    """SELECT memory_id, reflection_id, content_sha256,
                              provenance_sha256
                       FROM lesson_provenance WHERE prediction_id=?""",
                    (int(prediction["id"]),),
                ).fetchone()
                if existing is None:
                    self.db.execute(
                        """INSERT INTO lesson_provenance(
                               prediction_id, memory_id, reflection_id, verified_at,
                               content_sha256, provenance_sha256
                           ) VALUES (?, ?, ?, ?, ?, ?)""",
                        (
                            int(prediction["id"]), int(row["id"]),
                            int(normalized_reflection), _memory().now_iso(), content_sha256,
                            provenance_sha256,
                        ),
                    )
                    provenance_inserted = True
                elif (
                    int(existing["memory_id"]) != int(row["id"])
                    or int(existing["reflection_id"]) != int(normalized_reflection)
                    or str(existing["content_sha256"]) != content_sha256
                    or str(existing["provenance_sha256"] or "")
                    != provenance_sha256
                ):
                    raise ValueError("Prediction is already bound to different lesson provenance")
                control_material = self._lesson_control_material(
                    memory_id=int(row["id"]),
                    prediction_id=int(prediction["id"]),
                    reflection_id=int(normalized_reflection),
                    content_sha256=content_sha256,
                    provenance_sha256=provenance_sha256,
                    project_id=project_id,
                    observed_at=observed_at,
                    valid_until=valid_until,
                    lifecycle_status="active",
                    superseded_by=None,
                )
                control_sha256 = self._lesson_control_digest(control_material)
                existing_control = self.db.execute(
                    """SELECT project_id, observed_at, valid_until,
                              lifecycle_status, superseded_by, control_sha256
                       FROM lesson_controls WHERE memory_id=?""",
                    (int(row["id"]),),
                ).fetchone()
                if existing_control is None:
                    self.db.execute(
                        """INSERT INTO lesson_controls(
                               memory_id, project_id, observed_at, valid_until,
                               lifecycle_status, superseded_by, recorded_at,
                               control_sha256
                           ) VALUES (?, ?, ?, ?, 'active', NULL, ?, ?)""",
                        (
                            int(row["id"]), project_id, observed_at, valid_until,
                            _memory().now_iso(), control_sha256,
                        ),
                    )
                elif (
                    int(existing_control["project_id"]) != project_id
                    or str(existing_control["observed_at"]) != observed_at
                    or str(existing_control["valid_until"]) != valid_until
                    or str(existing_control["lifecycle_status"]) != "active"
                    or existing_control["superseded_by"] is not None
                    or str(existing_control["control_sha256"]) != control_sha256
                ):
                    raise ValueError("Lesson is already bound to different reuse controls")
                if not created and self._spine_ready:
                    # A duplicate lesson text bound to another prediction:
                    # ``applied`` iff a provenance row was added.
                    self._append_memory_event(
                        "memory.reasserted",
                        memory_id=int(row["id"]),
                        payload={
                            "origin": None,
                            "eligible": None,
                            "content_digest": _memory().memory_spine.content_digest(
                                self._spine_key, safe
                            ),
                            "provenance_sha256": provenance_sha256,
                        },
                        stamp=stamp,
                        source=source,
                        context=context,
                        outcome="applied" if provenance_inserted else "noop",
                    )
        if row is None:
            raise RuntimeError("Verified lesson could not be persisted")
        lesson_id = int(row["id"])
        self._mirror_vault_note(
            "lessons",
            f"{family.replace('_', ' ').title()} lesson — Reflection {normalized_reflection}",
            safe,
            tags=("jarvis", "verified-lesson", family, outcome_status),
            links=(f"Reflection {normalized_reflection}",),
            source=source,
        )
        return lesson_id

    @_with_read_snapshot
    def match_lessons(
        self,
        query: str,
        family: str,
        *,
        limit: int = 3,
        project_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """Match fresh proven lessons inside one project, or fail closed.

        Every exit records why in ``lesson_recall_report()`` (M4 design 5.4).
        The record is written before both raises, so a caller that catches the
        ``ValueError`` can still read the reason.  Nothing else about this
        method changed in M4: the screens, the candidate cap, the ranking
        parameters, the substitution refusals and the eligible-prefix rule are
        the strongest read discipline in the store and are untouched.
        """
        started = _memory().time.monotonic()
        report = _memory().learning_ladder.lesson_recall_record("idle")
        report["family"] = str(family)
        self._last_lesson_recall_report = report
        evaluation_at = _memory().now_iso()
        if family not in self.PREDICTION_FAMILIES:
            self._lesson_exit(report, "family_unsupported", started)
            raise ValueError(f"Unknown lesson family: {family}")
        if _memory().contains_secret(str(query)):
            self._lesson_exit(report, "secret_query", started)
            raise ValueError("Potential secret detected; lesson matching refused")
        if _memory().contains_private_identifier(str(query)):
            return self._lesson_abstain(
                report, "private_identifier_query", started
            )
        if project_id is None:
            enabled_projects = self.db.execute(
                "SELECT id FROM agent_projects WHERE enabled=1 ORDER BY id LIMIT 2"
            ).fetchall()
            if len(enabled_projects) != 1:
                # A missing scope is unambiguous only before the operator creates
                # another enabled project. Never silently fall back to project 1
                # in a multi-project database.
                return self._lesson_abstain(
                    report, "project_ambiguous", started
                )
            normalized_project = int(enabled_projects[0]["id"])
        else:
            normalized_project = self._project_id(project_id)
        report["project_id"] = int(normalized_project)
        limit = _memory()._bounded_limit(limit, 10)
        if _memory()._memory_query_targets_authority_evasion(str(query)):
            return self._lesson_abstain(
                report, "authority_evasion", started
            )
        discovery_terms = [
            term for term in _memory()._memory_tokens(str(query), meaningful_only=True)
            if term not in _memory()._LESSON_QUERY_METADATA_TERMS
        ]
        query_terms = [
            term for term in _memory()._memory_query_terms(str(query))
            if term not in _memory()._LESSON_QUERY_METADATA_TERMS
        ]
        structured_query_terms = {
            term for term in discovery_terms
            if any(character.isalpha() for character in term)
            and any(character.isdigit() for character in term)
        }
        namespaced_query = bool(
            structured_query_terms
            or _memory().re.search(r"\b[A-Z][a-z]+[A-Z][A-Za-z0-9]*\b", str(query))
        )
        if not limit or not discovery_terms:
            return self._lesson_abstain(
                report, "no_discovery_terms", started
            )
        # An explicit alphanumeric identifier is a hard target for lesson
        # retrieval.  Use it for the bounded SQL candidate set rather than
        # allowing generic recovery words to overflow the pool and hide the
        # exact target.  Both requested- and other-family rows remain visible,
        # so provenance and family-conflict checks still fail closed.
        lesson_text = (
            "CASE WHEN instr(lower(m.content), 'reusable lesson:') > 0 "
            "THEN substr(m.content, instr(lower(m.content), 'reusable lesson:') "
            "+ length('reusable lesson:')) ELSE m.content END"
        )
        retrieval_text = (
            "CASE WHEN r.id IS NOT NULL "
            "THEN COALESCE(r.summary, '') || ' ' || COALESCE(r.improvements, '') "
            f"ELSE {lesson_text} END"
        )
        improvement_text = (
            "CASE WHEN r.id IS NOT NULL THEN COALESCE(r.improvements, '') "
            f"ELSE {lesson_text} END"
        )
        candidate_limit = 320
        if structured_query_terms:
            term_groups = [sorted(structured_query_terms)]
        elif len(discovery_terms) <= _memory()._MAX_MEMORY_QUERY_TERM_CANDIDATES:
            # Preserve the established high-precision candidate pool for normal
            # requests.  Chunked full-query discovery is only needed when an
            # input actually exceeds the bounded pool.
            term_groups = [query_terms]
        else:
            term_groups = [
                discovery_terms[offset:offset + _memory()._MAX_MEMORY_QUERY_TERM_CANDIDATES]
                for offset in range(
                    0, len(discovery_terms), _memory()._MAX_MEMORY_QUERY_TERM_CANDIDATES
                )
            ]
        collected_rows: dict[int, _memory().sqlite3.Row] = {}
        collected_shadow_rows: dict[int, _memory().sqlite3.Row] = {}
        try:
            for term_group in term_groups:
                candidate_like_terms = _memory()._memory_like_terms(
                    str(query),
                    term_group,
                    max_terms=_memory()._MAX_MEMORY_QUERY_TERM_CANDIDATES * 2,
                )
                if not candidate_like_terms:
                    continue
                patterns = [
                    f"%{_memory()._escape_like(term)}%" for term in candidate_like_terms
                ]
                where = " OR ".join(
                    f"lower({retrieval_text}) LIKE ? ESCAPE '\\'" for _ in patterns
                )
                match_count = " + ".join(
                    f"CASE WHEN lower({retrieval_text}) LIKE ? ESCAPE '\\' THEN 1 ELSE 0 END"
                    for _ in patterns
                )
                chunk_rows = self.db.execute(
                    f"""SELECT m.id, m.created_at, m.kind, m.content, m.source, m.family,
                           m.outcome_status, m.reflection_id,
                           lc.project_id AS lesson_project_id,
                           {retrieval_text} AS retrieval_content,
                           {improvement_text} AS improvement_content,
                           0 AS utility_resolved, 0 AS utility_successes
                    FROM memories AS m
                    LEFT JOIN reflections AS r ON r.id=m.reflection_id
                    LEFT JOIN lesson_controls AS lc ON lc.memory_id=m.id
                    WHERE m.kind='lesson' AND m.family=? AND ({where})
                    ORDER BY ({match_count}) DESC, m.id DESC LIMIT ?""",
                [
                    family, *patterns, *patterns, candidate_limit + 1,
                ],
                ).fetchall()
                chunk_shadow_rows = self.db.execute(
                    f"""SELECT m.id, m.created_at, m.kind, m.content, m.source, m.family,
                           m.outcome_status, m.reflection_id,
                           lc.project_id AS lesson_project_id,
                           {retrieval_text} AS retrieval_content,
                           {improvement_text} AS improvement_content,
                           0 AS utility_resolved, 0 AS utility_successes
                    FROM memories AS m
                    LEFT JOIN reflections AS r ON r.id=m.reflection_id
                    LEFT JOIN lesson_controls AS lc ON lc.memory_id=m.id
                    WHERE m.kind='lesson' AND m.family<>? AND ({where})
                    ORDER BY ({match_count}) DESC, m.id DESC LIMIT ?""",
                [
                    family, *patterns, *patterns, candidate_limit + 1,
                ],
                ).fetchall()
                if (
                    len(chunk_rows) > candidate_limit
                    or len(chunk_shadow_rows) > candidate_limit
                ):
                    return self._lesson_abstain(
                        report, "chunk_overflow", started
                    )
                for row in chunk_rows:
                    collected_rows.setdefault(int(row["id"]), row)
                for row in chunk_shadow_rows:
                    collected_shadow_rows.setdefault(int(row["id"]), row)
                if (
                    len(collected_rows) > candidate_limit
                    or len(collected_shadow_rows) > candidate_limit
                ):
                    return self._lesson_abstain(
                        report, "pool_overflow", started
                    )
        except _memory().sqlite3.DatabaseError:
            return self._lesson_abstain(
                report, "database_error", started
            )
        rows = list(collected_rows.values())
        shadow_rows = list(collected_shadow_rows.values())
        report["candidates"] = len(rows) + len(shadow_rows)
        discovery_variant_sets = [
            set(_memory()._memory_term_variants(term))
            for term in (
                discovery_terms
                if len(discovery_terms) > _memory()._MAX_MEMORY_QUERY_TERM_CANDIDATES
                else query_terms
            )
        ]
        advice_anchored_rows: list[_memory().sqlite3.Row] = []
        for row in [*rows, *shadow_rows]:
            improvement_tokens = set(_memory()._memory_tokens(
                str(row["improvement_content"]), meaningful_only=False
            ))
            if any(
                variants.intersection(improvement_tokens)
                for variants in discovery_variant_sets
            ):
                advice_anchored_rows.append(row)
        report["anchored"] = len(advice_anchored_rows)
        requested_family_rows = [
            row for row in advice_anchored_rows
            if str(row["family"] or "") == family
        ]
        if not requested_family_rows:
            return self._lesson_abstain(
                report, "no_anchor", started
            )
        requested_family_rows = _memory()._memory_resolve_sibling_identities(
            list(requested_family_rows),
            str(query),
            content_key="improvement_content",
            identity_ignored_terms=_memory()._LESSON_IDENTITY_METADATA_TERMS,
            unknown_identity_minimum_matches=1,
            explicit_subject_identity=True,
        )
        if not requested_family_rows:
            return self._lesson_abstain(
                report, "unknown_identity", started
            )
        if not query_terms:
            return self._lesson_abstain(
                report, "no_query_terms", started
            )
        query_variant_sets = [
            set(_memory()._memory_term_variants(term)) for term in query_terms
        ]
        requested_signatures = {
            " ".join(_memory()._memory_tokens(
                str(row["improvement_content"]), meaningful_only=False
            ))
            for row in requested_family_rows
        }

        def family_shadow_score(row: sqlite3.Row) -> int:
            tokens = set(_memory()._memory_tokens(
                str(row["improvement_content"]), meaningful_only=False
            ))
            return sum(
                bool(variants.intersection(tokens))
                for variants in query_variant_sets
            )

        best_requested_score = max(
            family_shadow_score(row) for row in requested_family_rows
        )
        for row in advice_anchored_rows:
            if str(row["family"] or "") == family:
                continue
            signature = " ".join(_memory()._memory_tokens(
                str(row["improvement_content"]), meaningful_only=False
            ))
            if signature and signature in requested_signatures:
                continue
            row_tokens = set(_memory()._memory_tokens(
                str(row["improvement_content"]), meaningful_only=False
            ))
            row_score = family_shadow_score(row)
            if (
                structured_query_terms.intersection(row_tokens)
                or (
                    row_score > best_requested_score
                    and (
                        namespaced_query
                        or row_score == len(query_terms)
                        # A two-anchor margin is decisive specificity even in
                        # plain prose, mirroring the ranking convention of
                        # ``specificity_gap_prunes_weaker``.  A one-anchor
                        # margin stays treated as wording noise.
                        or row_score - best_requested_score >= 2
                    )
                )
            ):
                # A stronger exact target exists under a different task family.
                # Do not replace it with weaker advice from the requested family.
                return self._lesson_abstain(
                    report, "cross_family_stronger", started
                )
        in_project_rows = [
            row for row in requested_family_rows
            if int(row["lesson_project_id"] or -1) == normalized_project
        ]
        if not in_project_rows:
            return self._lesson_abstain(
                report, "out_of_project", started
            )
        report["in_project"] = len(in_project_rows)
        in_project_signatures = {
            " ".join(_memory()._memory_tokens(
                str(row["improvement_content"]), meaningful_only=False
            ))
            for row in in_project_rows
        }
        best_in_project_score = max(
            family_shadow_score(row) for row in in_project_rows
        )
        for row in requested_family_rows:
            if int(row["lesson_project_id"] or -1) == normalized_project:
                continue
            signature = " ".join(_memory()._memory_tokens(
                str(row["improvement_content"]), meaningful_only=False
            ))
            if signature and signature in in_project_signatures:
                continue
            if (
                family_shadow_score(row) > best_in_project_score
                and (
                    namespaced_query
                    or family_shadow_score(row) == len(query_terms)
                )
            ):
                return self._lesson_abstain(
                    report, "cross_project_stronger", started
                )
        rows = requested_family_rows
        eligible_rows = []
        for row in rows:
            # The same short-circuit order as before the M4 instrumentation:
            # outcome status, then provenance, then controls.  Only the
            # control reason is newly kept, so a lesson the operator retired
            # or that aged out is reportable instead of vanishing silently.
            if str(row["outcome_status"] or "") != "complete":
                continue
            if not self._lesson_provenance_validation(int(row["id"]))[0]:
                continue
            control_ok, control_reason = self._lesson_control_validation(
                int(row["id"]),
                project_id=normalized_project,
                as_of=evaluation_at,
            )
            if control_ok:
                eligible_rows.append(row)
            elif control_reason in _memory()._LESSON_LIFECYCLE_SHADOW_REASONS:
                report["superseded_shadowed"] += 1
        if not eligible_rows:
            return self._lesson_abstain(
                report, "none_eligible", started
            )
        report["eligible"] = len(eligible_rows)
        eligible_token_union = set().union(*(
            set(_memory()._memory_tokens(
                str(row["improvement_content"]), meaningful_only=False
            ))
            for row in eligible_rows
        ))
        best_eligible_score = max(
            family_shadow_score(row) for row in eligible_rows
        )
        for row in rows:
            if row in eligible_rows:
                continue
            row_tokens = set(_memory()._memory_tokens(
                str(row["improvement_content"]), meaningful_only=False
            ))
            shared = {
                term for term in query_terms
                if len(term) >= 6
                and term in row_tokens
                and term in eligible_token_union
            }
            unique = {
                term for term in query_terms
                if len(term) >= 4
                and term in row_tokens
                and term not in eligible_token_union
            }
            if shared and unique and family_shadow_score(row) >= best_eligible_score:
                return self._lesson_abstain(
                    report, "ineligible_shadow", started
                )
        ranked = _memory()._rank_memory_rows(
            list(rows),
            query_terms,
            keep_id=True,
            content_key="retrieval_content",
            family_scope_single_anchor=True,
            family_single_anchor_min_chars=7,
            family_single_anchor_requires_identifier=False,
            identity_conflict_shadow=True,
            require_structured_identifier_match=True,
            specificity_gap_prunes_weaker=2,
            relative_match_floor=0.85,
            relative_information_floor=0.85,
        )
        eligibility: dict[int, bool] = {}
        eligible_signatures: set[str] = set()
        for item in ranked:
            memory_id = int(item["memory_id"])
            valid = (
                str(item.get("family") or "") == family
                and str(item.get("outcome_status") or "") == "complete"
                and self._lesson_provenance_validation(memory_id)[0]
                and self._lesson_control_validation(
                    memory_id,
                    project_id=normalized_project,
                    as_of=evaluation_at,
                )[0]
            )
            eligibility[memory_id] = valid
            if valid:
                eligible_signatures.add(" ".join(_memory()._memory_tokens(
                    str(item.get("improvement_content") or ""),
                    meaningful_only=False,
                )))

        eligible_prefix: list[dict[str, Any]] = []
        for item in ranked:
            memory_id = int(item["memory_id"])
            signature = " ".join(_memory()._memory_tokens(
                str(item.get("improvement_content") or ""),
                meaningful_only=False,
            ))
            item.pop("improvement_content", None)
            if not eligibility[memory_id]:
                if signature and signature in eligible_signatures:
                    # Identical authenticated advice may be stored separately in
                    # multiple projects. Prefer the in-scope copy instead of
                    # letting row recency turn the duplicate into a denial.
                    continue
                # If the best-matching observation is incomplete, failed,
                # unproven, expired, or belongs elsewhere, do not silently
                # substitute weaker successful advice. Only an eligible ranked
                # prefix can condition a response.
                break
            eligible_prefix.append(item)
            if len(eligible_prefix) >= limit:
                break
        report["returned"] = len(eligible_prefix)
        if eligible_prefix:
            self._lesson_exit(report, "rows_returned", started)
            return eligible_prefix
        if not ranked:
            # Nothing cleared the ranker's own floors: the store looked and
            # found nothing relevant, which is not a refusal and does not cue.
            return self._lesson_abstain(report, "ranker_floor", started)
        # The best ranked row is ineligible, so the lane returns nothing
        # rather than substituting the weaker eligible row beneath it.  This
        # is a different path from "every candidate is ineligible".
        return self._lesson_abstain(
            report, "ineligible_prefix", started
        )

    def record_lesson_applications(
        self,
        prediction_id: int,
        family: str,
        memory_ids: list[int],
    ) -> None:
        normalized_prediction = self._prediction_optional_id(
            prediction_id, "prediction_id"
        )
        if family not in self.PREDICTION_FAMILIES:
            raise ValueError(f"Unknown lesson family: {family}")
        bounded_ids = []
        for raw in memory_ids[:10]:
            normalized = self._prediction_optional_id(raw, "memory_id")
            if normalized not in bounded_ids:
                bounded_ids.append(normalized)
        stamp = _memory().now_iso()
        try:
            self._record_lesson_applications_locked(
                normalized_prediction, family, bounded_ids, stamp
            )
        except (_memory().sqlite3.Error, _memory().memory_spine.SpineError) as exc:
            # S-3: the worker holds the write lock.  The turn degrades to a
            # recorded non-fatal outcome rather than failing, and every
            # ValueError this method raises -- an already-resolved prediction,
            # another family, an ineligible lesson -- is untouched, because
            # those are caller bugs and the ladder's proof depends on them
            # still being refused.
            #
            # ``SpineError`` joins it for ruling 27: this runs on the turn
            # path, and a chain that will not accept the ``lesson.applied``
            # receipt must cost the turn its receipt, never the turn itself.
            # The proof then refuses ``proof_unbacked``, which is the correct
            # fail-closed consequence.
            self._record_degraded_write(
                "apply_lessons", type(exc).__name__,
                reason=(
                    "spine_unverified"
                    if isinstance(exc, _memory().memory_spine.SpineError)
                    else "store_locked"
                ),
            )

    def _append_lesson_applied_receipt(
        self,
        prediction_id: int,
        family: str,
        project_id: int,
        lesson_ids: Sequence[int],
        stamp: str,
    ) -> None:
        """One digest-only receipt for the turn's applications (ruling 21).

        ``lesson_applications`` is the single input that decides whether a
        document is promoted, and before this it was the only such input with
        no receipt: ``proof_sha256`` is computed *over* whatever rows are
        there, so it was self-consistent with a forged set.  The event binds
        the **identity** of the rows -- ``(id, prediction_id, memory_id)`` --
        which are immutable from insert; the verdict stays live, re-derived by
        the proof's own clauses, because the row's ``successful`` column is
        still NULL at this instant and digesting it would disagree with every
        later re-check.

        The digest covers every application row for this prediction after the
        insert, not just the ones this call added, so the newest event for a
        prediction always describes the legitimate current set and two calls
        in one turn stay consistent.  Appended inside the caller's
        transaction; a store whose spine does not carry the kind writes
        nothing and the proof check stays inert until it does.
        """
        if not self._spine_ready or _memory()._LESSON_APPLIED_KIND not in _memory().memory_spine.SPINE_KINDS:
            return
        rows = self.db.execute(
            """SELECT id, prediction_id, memory_id FROM lesson_applications
               WHERE prediction_id=? ORDER BY id""",
            (int(prediction_id),),
        ).fetchall()
        if not rows:
            return
        _memory().memory_spine.append_event(
            self.db,
            self._spine_key,
            kind=_memory()._LESSON_APPLIED_KIND,
            actor="runtime",
            source="lesson application",
            scope="global",
            permission="runtime",
            outcome="applied",
            payload={
                "at": stamp,
                "family": str(family),
                "project_id": int(project_id),
                "prediction_id": int(prediction_id),
                "lesson_ids": sorted({int(value) for value in lesson_ids})[:10],
                "count": len(rows),
                "applications_digest": _memory().memory_spine.lesson_applications_digest(
                    self._spine_key,
                    [
                        (
                            int(row["id"]), int(row["prediction_id"]),
                            int(row["memory_id"]),
                        )
                        for row in rows
                    ],
                ),
            },
            now=stamp,
            subject_kind="lesson",
            # The rank-1 lesson: the first id the caller passed, which is the
            # row whose ``rank`` column is 1.
            subject_id=int(lesson_ids[0]),
        )

    def _record_lesson_applications_locked(
        self,
        normalized_prediction: int,
        family: str,
        bounded_ids: list[int],
        stamp: str,
    ) -> None:
        with self._immediate_transaction():
            prediction = self.db.execute(
                """SELECT family, origin, created_at, resolved_at, actual_status,
                          evidence_ok, predicted_verification,
                          task_id, conversation_id
                   FROM task_predictions WHERE id=?""",
                (normalized_prediction,),
            ).fetchone()
            if (
                prediction is None
                or prediction["family"] != family
                or str(prediction["origin"])
                not in _memory().LESSON_REUSABLE_PREDICTION_ORIGINS
                or prediction["resolved_at"] is not None
                or (
                    family in _memory().LESSON_EVIDENCE_REQUIRED_FAMILIES
                    and str(prediction["predicted_verification"])
                    == "not_applicable"
                )
            ):
                raise ValueError("Lesson application must bind to the active matching prediction")
            project_id = self._lesson_project_for_context(
                prediction["task_id"], prediction["conversation_id"]
            )
            if project_id is None:
                raise ValueError("Lesson application lacks a valid project scope")
            for rank, memory_id in enumerate(bounded_ids, 1):
                valid = self.db.execute(
                    """SELECT lc.observed_at, lc.valid_until
                       FROM memories AS m
                       JOIN lesson_controls AS lc ON lc.memory_id=m.id
                       WHERE m.id=? AND m.kind='lesson' AND m.family=?
                         AND m.outcome_status='complete'""",
                    (memory_id, family),
                ).fetchone()
                application_values = (
                    None if valid is None else self._lesson_application_values(
                        family=family,
                        application_created_at=stamp,
                        application_resolved_at=None,
                        application_successful=None,
                        prediction_created_at=prediction["created_at"],
                        prediction_resolved_at=prediction["resolved_at"],
                        prediction_actual_status=prediction["actual_status"],
                        prediction_evidence_ok=prediction["evidence_ok"],
                        prediction_verification=prediction[
                            "predicted_verification"
                        ],
                        lesson_observed_at=valid["observed_at"],
                        lesson_valid_until=valid["valid_until"],
                        validation_at=stamp,
                    )
                )
                if (
                    valid is None
                    or application_values is None
                    or not self._lesson_provenance_validation(memory_id)[0]
                    or not self._lesson_control_validation(
                        memory_id,
                        project_id=project_id,
                        as_of=application_values[0],
                    )[0]
                ):
                    raise ValueError("Lesson application references an ineligible lesson")
                self.db.execute(
                    """INSERT OR IGNORE INTO lesson_applications(
                           created_at, prediction_id, memory_id, family, rank
                       ) VALUES (?, ?, ?, ?, ?)""",
                    (
                        application_values[0], normalized_prediction,
                        memory_id, family, rank,
                    ),
                )
            # One receipt for the turn, inside the same transaction as the
            # rows it binds, so a row can never exist without its event
            # (ruling 21).
            self._append_lesson_applied_receipt(
                normalized_prediction, family, project_id, bounded_ids, stamp
            )

    def lesson_effectiveness(self, family: str | None = None) -> list[dict[str, Any]]:
        if family is not None and family not in self.PREDICTION_FAMILIES:
            raise ValueError(f"Unknown lesson family: {family}")
        clause = "WHERE a.family=?" if family else ""
        parameters: tuple[Any, ...] = (family,) if family else ()
        try:
            rows = self.db.execute(
                f"""SELECT a.family, a.memory_id,
                           a.created_at AS application_created_at,
                           a.resolved_at AS application_resolved_at,
                           a.successful AS application_successful,
                           p.family AS prediction_family,
                           p.origin AS prediction_origin,
                           p.created_at AS prediction_created_at,
                           p.resolved_at AS prediction_resolved_at,
                           p.actual_status AS prediction_actual_status,
                           p.evidence_ok AS prediction_evidence_ok,
                           p.predicted_verification,
                           p.task_id, p.conversation_id,
                           m.family AS lesson_family,
                           m.outcome_status AS lesson_outcome_status,
                           lc.observed_at AS lesson_observed_at,
                           lc.valid_until AS lesson_valid_until
                    FROM lesson_applications AS a
                    JOIN task_predictions AS p ON p.id=a.prediction_id
                    JOIN memories AS m ON m.id=a.memory_id
                    JOIN lesson_controls AS lc ON lc.memory_id=a.memory_id
                    {clause}
                    ORDER BY a.family, a.id""",
                parameters,
            ).fetchall()
        except _memory().sqlite3.DatabaseError:
            return []
        totals: dict[str, dict[str, Any]] = {}
        evaluation_at = _memory().now_iso()
        for row in rows:
            row_family = str(row["family"])
            if (
                row_family not in self.PREDICTION_FAMILIES
                or str(row["prediction_family"]) != row_family
                or str(row["prediction_origin"])
                not in _memory().LESSON_REUSABLE_PREDICTION_ORIGINS
                or str(row["lesson_family"]) != row_family
                or str(row["lesson_outcome_status"] or "") != "complete"
            ):
                continue
            project_id = self._lesson_project_for_context(
                row["task_id"], row["conversation_id"]
            )
            memory_id = int(row["memory_id"])
            application_values = self._lesson_application_values(
                family=row_family,
                application_created_at=row["application_created_at"],
                application_resolved_at=row["application_resolved_at"],
                application_successful=row["application_successful"],
                prediction_created_at=row["prediction_created_at"],
                prediction_resolved_at=row["prediction_resolved_at"],
                prediction_actual_status=row["prediction_actual_status"],
                prediction_evidence_ok=row["prediction_evidence_ok"],
                prediction_verification=row["predicted_verification"],
                lesson_observed_at=row["lesson_observed_at"],
                lesson_valid_until=row["lesson_valid_until"],
                validation_at=evaluation_at,
            )
            if (
                project_id is None
                or application_values is None
                or not self._lesson_provenance_validation(memory_id)[0]
                or not self._lesson_control_validation(
                    memory_id, project_id=project_id
                )[0]
            ):
                continue
            aggregate = totals.setdefault(row_family, {
                "family": row_family,
                "applications": 0,
                "resolved": 0,
                "successes": 0,
            })
            aggregate["applications"] += 1
            if application_values[1] is not None:
                aggregate["resolved"] += 1
                aggregate["successes"] += int(application_values[2] or 0)
        result: list[dict[str, Any]] = []
        for row_family in sorted(totals):
            aggregate = totals[row_family]
            resolved = int(aggregate.pop("resolved"))
            successes = int(aggregate.pop("successes"))
            aggregate["resolved"] = resolved
            aggregate["success_rate"] = (
                successes / resolved if resolved else None
            )
            result.append(aggregate)
        return result

    def supersede_verified_lesson(
        self,
        memory_id: int,
        replacement_memory_id: int,
        *,
        contradiction: bool = False,
    ) -> None:
        """Retire one lesson only in favor of newer proven same-scope evidence."""
        original_id = self._prediction_optional_id(memory_id, "memory_id")
        replacement_id = self._prediction_optional_id(
            replacement_memory_id, "replacement_memory_id"
        )
        if original_id == replacement_id:
            raise ValueError("A lesson cannot supersede itself")
        with self._immediate_transaction():
            rows = self.db.execute(
                """SELECT m.id, m.family, lc.project_id, lc.observed_at,
                          lc.valid_until, lc.lifecycle_status, lc.superseded_by,
                          lp.prediction_id, lp.reflection_id, lp.content_sha256,
                          lp.provenance_sha256
                   FROM memories AS m
                   JOIN lesson_controls AS lc ON lc.memory_id=m.id
                   JOIN lesson_provenance AS lp ON lp.memory_id=m.id
                   WHERE m.id IN (?, ?)
                   ORDER BY m.id""",
                (original_id, replacement_id),
            ).fetchall()
            by_id = {int(row["id"]): row for row in rows}
            original = by_id.get(original_id)
            replacement = by_id.get(replacement_id)
            if original is None or replacement is None:
                raise ValueError("Both lessons require integrity-checked reuse controls")
            if (
                str(original["family"]) != str(replacement["family"])
                or int(original["project_id"]) != int(replacement["project_id"])
            ):
                raise ValueError("Replacement lesson must have the same family and project")
            if not self._lesson_provenance_validation(replacement_id)[0] or not (
                self._lesson_control_validation(
                    replacement_id, project_id=int(replacement["project_id"])
                )[0]
            ):
                raise ValueError("Replacement lesson is not currently eligible")
            if not self._lesson_provenance_validation(original_id)[0] or not (
                self._lesson_control_validation(
                    original_id, project_id=int(original["project_id"])
                )[0]
            ):
                raise ValueError("Original lesson is not currently eligible")
            original_observed = self._canonical_utc_timestamp(original["observed_at"])
            replacement_observed = self._canonical_utc_timestamp(
                replacement["observed_at"]
            )
            if (
                original_observed is None
                or replacement_observed is None
                or _memory().datetime.fromisoformat(replacement_observed)
                <= _memory().datetime.fromisoformat(original_observed)
            ):
                raise ValueError("Replacement evidence must be newer than the original")
            status = "contradicted" if contradiction else "superseded"
            material = self._lesson_control_material(
                memory_id=original_id,
                prediction_id=int(original["prediction_id"]),
                reflection_id=int(original["reflection_id"]),
                content_sha256=str(original["content_sha256"]),
                provenance_sha256=str(original["provenance_sha256"] or ""),
                project_id=int(original["project_id"]),
                observed_at=str(original_observed),
                valid_until=str(original["valid_until"]),
                lifecycle_status=status,
                superseded_by=replacement_id,
            )
            updated = self.db.execute(
                """UPDATE lesson_controls
                   SET lifecycle_status=?, superseded_by=?, recorded_at=?,
                       control_sha256=?
                   WHERE memory_id=? AND lifecycle_status='active'
                     AND superseded_by IS NULL""",
                (
                    status, replacement_id, _memory().now_iso(),
                    self._lesson_control_digest(material), original_id,
                ),
            )
            if updated.rowcount != 1:
                raise ValueError("Lesson lifecycle changed concurrently")
