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


class StrategyTransferMemoryMixin:
    """Mechanically extracted current Memory methods."""

    @staticmethod
    def _strategy_observation_material(
        *,
        created_at: str,
        prediction: Mapping[str, Any],
        project_id: int,
        evidence: Mapping[str, Any],
        strategies: Sequence[str],
    ) -> dict[str, Any]:
        return {
            "schema": "jarvis.task-strategy-observation.v1",
            "created_at": str(created_at),
            "project_id": int(project_id),
            "prediction": {
                "id": int(prediction["id"]),
                "created_at": str(prediction["created_at"]),
                "task_id": prediction["task_id"],
                "conversation_id": prediction["conversation_id"],
                "origin": str(prediction["origin"]),
                "family": str(prediction["family"]),
                "profile": str(prediction["profile"]),
                "model": str(prediction["model"]),
                "predicted_success": float(prediction["predicted_success"]),
                "predicted_steps": int(prediction["predicted_steps"]),
                "predicted_verification": str(
                    prediction["predicted_verification"]
                ),
                "basis": str(prediction["basis"]),
                "resolved_at": str(prediction["resolved_at"]),
                "actual_status": str(prediction["actual_status"]),
                "actual_steps": int(prediction["actual_steps"]),
                "evidence_ok": int(prediction["evidence_ok"]),
                "failure_class": prediction["failure_class"],
            },
            "evidence": dict(evidence),
            "strategies": list(strategies),
        }

    @staticmethod
    def _strategy_observation_digest(material: Mapping[str, Any]) -> str:
        canonical = _memory().json.dumps(
            material,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return _memory().hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _strategy_prediction_row(self, prediction_id: int) -> sqlite3.Row | None:
        return self.db.execute(
            """SELECT id, created_at, task_id, conversation_id, origin, family,
                      profile, model, predicted_success, predicted_steps,
                      predicted_verification, basis, resolved_at, actual_status,
                      actual_steps, evidence_ok, failure_class
               FROM task_predictions WHERE id=?""",
            (int(prediction_id),),
        ).fetchone()

    def _strategy_prediction_project(
        self,
        prediction: Mapping[str, Any],
    ) -> int | None:
        project_id = self._lesson_project_for_context(
            prediction["task_id"], prediction["conversation_id"]
        )
        if project_id is None:
            return None
        project = self.get_project(project_id)
        if project is None or not bool(project["enabled"]):
            return None
        return int(project_id)

    def record_strategy_observations(
        self,
        prediction_id: int,
        evidence: Mapping[str, Any],
    ) -> bool:
        """Persist one closed, outcome-bound procedural observation.

        The observation contains only the fixed strategy vocabulary. It never
        stores lesson prose, prompts, paths, URLs, tools, or authority claims.
        Replaying the exact evidence is idempotent; a conflicting replay fails.
        """
        normalized_prediction = self._prediction_optional_id(
            prediction_id, "prediction_id"
        )
        if not isinstance(evidence, _memory().Mapping):
            raise _memory().StrategyTransferError("strategy evidence must be an object")
        strategies = _memory().strategies_from_evidence(evidence)
        canonical_evidence = _memory().json.dumps(
            dict(evidence),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        canonical_strategies = _memory().json.dumps(list(strategies), separators=(",", ":"))
        with self._immediate_transaction():
            prediction = self._strategy_prediction_row(normalized_prediction)
            if (
                prediction is None
                or prediction["resolved_at"] is None
                or str(prediction["actual_status"]) != "complete"
                or prediction["actual_steps"] is None
                or int(prediction["evidence_ok"] or 0) != 1
                or str(prediction["origin"])
                not in _memory().LESSON_REUSABLE_PREDICTION_ORIGINS
                or str(prediction["family"]) not in self.PREDICTION_FAMILIES
                or str(prediction["predicted_verification"])
                not in self.PREDICTION_VERIFICATION
                or str(prediction["predicted_verification"]) == "not_applicable"
            ):
                raise ValueError(
                    "Strategy evidence requires an exact successful verified prediction"
                )
            project_id = self._strategy_prediction_project(prediction)
            if project_id is None:
                raise ValueError("Strategy evidence lacks an enabled project scope")
            existing = self.db.execute(
                """SELECT evidence_json, strategies_json
                   FROM task_strategy_observations WHERE prediction_id=?""",
                (normalized_prediction,),
            ).fetchone()
            if existing is not None:
                valid, payload = self._task_strategy_observation_validation(
                    normalized_prediction, project_id=project_id
                )
                if (
                    valid
                    and isinstance(payload, dict)
                    and str(existing["evidence_json"]) == canonical_evidence
                    and str(existing["strategies_json"]) == canonical_strategies
                ):
                    return False
                raise ValueError("Conflicting or invalid strategy evidence replay")
            stamp = _memory().now_iso()
            canonical_stamp = self._canonical_utc_timestamp(stamp)
            prediction_created = self._canonical_utc_timestamp(
                prediction["created_at"]
            )
            prediction_resolved = self._canonical_utc_timestamp(
                prediction["resolved_at"]
            )
            if None in {canonical_stamp, prediction_created, prediction_resolved}:
                raise ValueError("Strategy evidence timestamps are invalid")
            if _memory().datetime.fromisoformat(str(canonical_stamp)) < _memory().datetime.fromisoformat(
                str(prediction_resolved)
            ):
                raise ValueError("Strategy evidence predates its resolved outcome")
            material = self._strategy_observation_material(
                created_at=str(canonical_stamp),
                prediction=prediction,
                project_id=project_id,
                evidence=dict(evidence),
                strategies=strategies,
            )
            self.db.execute(
                """INSERT INTO task_strategy_observations(
                       prediction_id, created_at, project_id, source_family,
                       evidence_json, strategies_json, observation_sha256
                   ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    normalized_prediction,
                    canonical_stamp,
                    project_id,
                    str(prediction["family"]),
                    canonical_evidence,
                    canonical_strategies,
                    self._strategy_observation_digest(material),
                ),
            )
        return True

    def record_task_strategy_observation(
        self,
        prediction_id: int,
        payload: Mapping[str, Any],
    ) -> bool:
        """Compatibility alias for the agent's bounded runtime receipt hook."""
        return self.record_strategy_observations(prediction_id, payload)

    def _task_strategy_observation_validation(
        self,
        prediction_id: int,
        *,
        project_id: int | None = None,
    ) -> tuple[bool, dict[str, Any] | str]:
        try:
            row = self.db.execute(
                """SELECT o.prediction_id, o.created_at, o.project_id,
                          o.source_family, o.evidence_json, o.strategies_json,
                          o.observation_sha256,
                          p.created_at AS prediction_created_at, p.task_id,
                          p.conversation_id, p.origin, p.family, p.profile, p.model,
                          p.predicted_success, p.predicted_steps,
                          p.predicted_verification, p.basis, p.resolved_at,
                          p.actual_status, p.actual_steps, p.evidence_ok,
                          p.failure_class
                   FROM task_strategy_observations AS o
                   JOIN task_predictions AS p ON p.id=o.prediction_id
                   WHERE o.prediction_id=?""",
                (int(prediction_id),),
            ).fetchone()
        except (_memory().sqlite3.DatabaseError, TypeError, ValueError):
            return False, "observation_unavailable"
        if row is None:
            return False, "observation_missing"
        try:
            observed_project = self._project_id(int(row["project_id"]))
            if project_id is not None and observed_project != self._project_id(project_id):
                return False, "project_mismatch"
            prediction = {
                "id": int(row["prediction_id"]),
                "created_at": str(row["prediction_created_at"]),
                "task_id": row["task_id"],
                "conversation_id": row["conversation_id"],
                "origin": str(row["origin"]),
                "family": str(row["family"]),
                "profile": str(row["profile"]),
                "model": str(row["model"]),
                "predicted_success": float(row["predicted_success"]),
                "predicted_steps": int(row["predicted_steps"]),
                "predicted_verification": str(row["predicted_verification"]),
                "basis": str(row["basis"]),
                "resolved_at": str(row["resolved_at"]),
                "actual_status": str(row["actual_status"]),
                "actual_steps": int(row["actual_steps"]),
                "evidence_ok": int(row["evidence_ok"]),
                "failure_class": row["failure_class"],
            }
            if (
                prediction["origin"] not in _memory().LESSON_REUSABLE_PREDICTION_ORIGINS
                or prediction["family"] not in self.PREDICTION_FAMILIES
                or prediction["family"] != str(row["source_family"])
                or prediction["actual_status"] != "complete"
                or prediction["evidence_ok"] != 1
                or prediction["predicted_verification"] == "not_applicable"
                or self._strategy_prediction_project(prediction) != observed_project
            ):
                return False, "prediction_mismatch"
            created_at = self._canonical_utc_timestamp(row["created_at"])
            prediction_created = self._canonical_utc_timestamp(
                row["prediction_created_at"]
            )
            prediction_resolved = self._canonical_utc_timestamp(row["resolved_at"])
            if None in {created_at, prediction_created, prediction_resolved}:
                return False, "timestamp_invalid"
            if (
                str(row["created_at"]) != created_at
                or str(row["prediction_created_at"]) != prediction_created
                or str(row["resolved_at"]) != prediction_resolved
            ):
                return False, "timestamp_noncanonical"
            created = _memory().datetime.fromisoformat(str(created_at))
            if created < _memory().datetime.fromisoformat(str(prediction_resolved)):
                return False, "observation_predates_outcome"
            if created > _memory().datetime.now(_memory().timezone.utc) + _memory().timedelta(minutes=5):
                return False, "observation_in_future"
            evidence = _memory().json.loads(str(row["evidence_json"]))
            stored_strategies = _memory().json.loads(str(row["strategies_json"]))
            if not isinstance(evidence, dict) or not isinstance(stored_strategies, list):
                return False, "payload_invalid"
            strategies = _memory().strategies_from_evidence(evidence)
            if (
                stored_strategies != list(strategies)
                or str(row["evidence_json"])
                != _memory().json.dumps(
                    evidence,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                or str(row["strategies_json"])
                != _memory().json.dumps(list(strategies), separators=(",", ":"))
            ):
                return False, "payload_noncanonical"
            material = self._strategy_observation_material(
                created_at=str(created_at),
                prediction=prediction,
                project_id=observed_project,
                evidence=evidence,
                strategies=strategies,
            )
            digest = self._strategy_observation_digest(material)
            if str(row["observation_sha256"]) != digest:
                return False, "observation_digest_mismatch"
        except (
            _memory().json.JSONDecodeError,
            OverflowError,
            _memory().StrategyTransferError,
            TypeError,
            ValueError,
        ):
            return False, "observation_invalid"
        return True, {
            "prediction_id": int(row["prediction_id"]),
            "project_id": observed_project,
            "source_family": str(row["source_family"]),
            "created_at": str(created_at),
            "strategies": list(strategies),
            "observation_sha256": digest,
        }

    @staticmethod
    def _strategy_transfer_z_timestamp(value: Any) -> str | None:
        canonical = _memory().Memory._canonical_utc_timestamp(value)
        if canonical is None:
            return None
        return canonical.replace("+00:00", "Z")

    @_with_read_snapshot
    def strategy_transfer_candidates(
        self,
        target_family: str,
        *,
        project_id: int,
        as_of: str | None = None,
        limit: int = 128,
    ) -> list[dict[str, Any]]:
        """Return selector-ready metadata, never reusable lesson prose."""
        if target_family not in self.PREDICTION_FAMILIES:
            raise ValueError(f"Unknown target family: {target_family}")
        normalized_project = self._project_id(project_id)
        project = self.get_project(normalized_project)
        if project is None or not bool(project["enabled"]):
            return []
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 128:
            raise ValueError("strategy candidate limit must be between 1 and 128")
        current_at = self._canonical_utc_timestamp(as_of or _memory().now_iso())
        if current_at is None:
            raise ValueError("strategy candidate timestamp must be timezone-aware")
        try:
            rows = self.db.execute(
                """SELECT m.id AS memory_id, m.family AS source_family,
                          m.outcome_status, lp.prediction_id,
                          lp.provenance_sha256, lc.observed_at, lc.valid_until,
                          lc.lifecycle_status, lc.superseded_by, lc.control_sha256,
                          o.observation_sha256
                   FROM memories AS m
                   JOIN lesson_provenance AS lp ON lp.memory_id=m.id
                   JOIN lesson_controls AS lc ON lc.memory_id=m.id
                   JOIN task_strategy_observations AS o
                     ON o.prediction_id=lp.prediction_id
                   WHERE m.kind='lesson' AND m.outcome_status='complete'
                     AND m.family<>? AND lc.project_id=?
                     AND o.project_id=? AND o.source_family=m.family
                   ORDER BY lc.observed_at DESC, m.id DESC
                   LIMIT 129""",
                (target_family, normalized_project, normalized_project),
            ).fetchall()
        except _memory().sqlite3.DatabaseError:
            self._strategy_transfer_candidate_telemetry = {
                "schema": "jarvis.strategy-transfer-candidate-health.v1",
                "available": False,
                "reason": "candidate_query_unavailable",
                "quarantined_strategies": 0,
                "unavailable_strategies": 0,
            }
            return []
        if len(rows) > 128:
            # Never hand the selector an ambiguous pool larger than its closed
            # contract. The caller can wait for lifecycle pruning/supersession.
            self._strategy_transfer_candidate_telemetry = {
                "schema": "jarvis.strategy-transfer-candidate-health.v1",
                "available": False,
                "reason": "candidate_pool_overflow",
                "quarantined_strategies": 0,
                "unavailable_strategies": 0,
            }
            return []
        calibrated: dict[str, bool] = {}
        candidates: list[dict[str, Any]] = []
        quarantined_strategies = 0
        unavailable_strategies = 0
        for row in rows:
            memory_id = int(row["memory_id"])
            source_family = str(row["source_family"] or "")
            if source_family not in self.PREDICTION_FAMILIES:
                continue
            if source_family not in calibrated:
                calibrated[source_family] = bool(
                    self.calibration_gate(source_family)["allowed"]
                )
            observation_valid, observation = (
                self._task_strategy_observation_validation(
                    int(row["prediction_id"]), project_id=normalized_project
                )
            )
            control_valid, _ = self._lesson_control_validation(
                memory_id, project_id=normalized_project, as_of=current_at
            )
            observed_at = self._strategy_transfer_z_timestamp(row["observed_at"])
            valid_until = self._strategy_transfer_z_timestamp(row["valid_until"])
            if (
                not calibrated[source_family]
                or not observation_valid
                or not isinstance(observation, dict)
                or not observation["strategies"]
                or not self._lesson_provenance_validation(memory_id)[0]
                or not control_valid
                or str(row["lifecycle_status"]) != "active"
                or row["superseded_by"] is not None
                or observed_at is None
                or valid_until is None
                or str(row["provenance_sha256"] or "") == ""
            ):
                continue
            safe_strategies: list[str] = []
            for raw_strategy in observation["strategies"]:
                strategy = str(raw_strategy)
                available, failures, _reason = (
                    self._strategy_transfer_harm_failure_count(
                        memory_id,
                        strategy=strategy,
                        target_family=target_family,
                    )
                )
                if not available:
                    unavailable_strategies += 1
                    continue
                if failures >= 2:
                    quarantined_strategies += 1
                    continue
                safe_strategies.append(strategy)
            if not safe_strategies:
                continue
            candidates.append({
                "id": f"lesson:{memory_id}",
                "record_kind": "lesson",
                "source_family": source_family,
                "outcome_status": "complete",
                "derived_from": "verified_reflection",
                "provenance_valid": True,
                "provenance_sha256": str(row["provenance_sha256"]),
                "observed_at": observed_at,
                "valid_until": valid_until,
                "contradicted_by": [],
                "strategies": safe_strategies,
                "authority_claims": [],
                "tool_claims": [],
            })
            if len(candidates) >= limit:
                break
        self._strategy_transfer_candidate_telemetry = {
            "schema": "jarvis.strategy-transfer-candidate-health.v1",
            "available": unavailable_strategies == 0,
            "reason": (
                "harm_ledger_unavailable"
                if unavailable_strategies else "available"
            ),
            "quarantined_strategies": quarantined_strategies,
            "unavailable_strategies": unavailable_strategies,
        }
        return candidates

    def strategy_transfer_candidate_health(self) -> dict[str, Any]:
        """Return prompt-free status from the most recent candidate evaluation."""
        return dict(self._strategy_transfer_candidate_telemetry)

    @staticmethod
    def _strategy_transfer_application_material(
        *,
        created_at: str,
        prediction_id: int,
        memory_id: int,
        project_id: int,
        strategy: str,
        source_family: str,
        target_family: str,
        mode: str,
        applied: bool,
        rank: int,
        source_observation_sha256: str,
        source_provenance_sha256: str,
        source_control_sha256: str,
        resolved_at: str | None,
        successful: int | None,
    ) -> dict[str, Any]:
        return {
            "schema": "jarvis.strategy-transfer-application.v1",
            "created_at": str(created_at),
            "prediction_id": int(prediction_id),
            "memory_id": int(memory_id),
            "project_id": int(project_id),
            "strategy": str(strategy),
            "source_family": str(source_family),
            "target_family": str(target_family),
            "mode": str(mode),
            "applied": bool(applied),
            "rank": int(rank),
            "source_observation_sha256": str(source_observation_sha256),
            "source_provenance_sha256": str(source_provenance_sha256),
            "source_control_sha256": str(source_control_sha256),
            "resolved_at": None if resolved_at is None else str(resolved_at),
            "successful": None if successful is None else int(successful),
        }

    @staticmethod
    def _strategy_transfer_application_digest(
        material: Mapping[str, Any],
    ) -> str:
        canonical = _memory().json.dumps(
            material,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return _memory().hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def _strategy_transfer_identifier(value: Any, label: str) -> str:
        if not isinstance(value, str):
            raise _memory().StrategyTransferError(f"{label} must be a string")
        normalized = value.strip()
        if (
            not normalized
            or len(normalized) > 96
            or any(ord(character) < 32 for character in normalized)
        ):
            raise _memory().StrategyTransferError(f"{label} is malformed")
        return normalized

    def _strategy_transfer_selection_rows(
        self,
        selection: Mapping[str, Any],
        *,
        target_family: str,
    ) -> list[dict[str, Any]]:
        if not isinstance(selection, _memory().Mapping):
            raise _memory().StrategyTransferError("strategy transfer selection must be an object")
        expected_fields = {
            "schema", "task_id", "target_family", "desired_strategies",
            "advice", "rejected", "advisory_only", "authority_grants",
            "tool_grants",
        }
        if set(selection) != expected_fields:
            raise _memory().StrategyTransferError("strategy transfer selection fields are invalid")
        if selection.get("schema") != "jarvis.strategy-transfer.v1":
            raise _memory().StrategyTransferError("strategy transfer selection schema is unsupported")
        self._strategy_transfer_identifier(selection.get("task_id"), "task_id")
        if selection.get("target_family") != target_family:
            raise _memory().StrategyTransferError("strategy transfer target family does not match")
        if selection.get("advisory_only") is not True:
            raise _memory().StrategyTransferError("strategy transfer must remain advisory-only")
        for field in ("authority_grants", "tool_grants"):
            value = selection.get(field)
            if not isinstance(value, list) or value:
                raise _memory().StrategyTransferError(
                    "strategy transfer may not grant tools, permissions, or authority"
                )
        desired_raw = selection.get("desired_strategies")
        if not isinstance(desired_raw, list) or len(desired_raw) > len(_memory().STRATEGY_SET):
            raise _memory().StrategyTransferError("desired strategies must be a bounded array")
        desired: list[str] = []
        for value in desired_raw:
            if not isinstance(value, str) or value not in _memory().STRATEGY_SET:
                raise _memory().StrategyTransferError("desired strategy is unsupported")
            if value in desired:
                raise _memory().StrategyTransferError("desired strategies contain duplicates")
            desired.append(value)
        rejected = selection.get("rejected")
        if not isinstance(rejected, list) or len(rejected) > 128:
            raise _memory().StrategyTransferError("rejected strategy candidates are malformed")
        for item in rejected:
            if not isinstance(item, _memory().Mapping) or set(item) != {"lesson_id", "reason"}:
                raise _memory().StrategyTransferError("rejected strategy candidate is malformed")
            self._strategy_transfer_identifier(item.get("lesson_id"), "lesson_id")
            self._strategy_transfer_identifier(item.get("reason"), "rejection reason")
        advice = selection.get("advice")
        if not isinstance(advice, list) or len(advice) > len(_memory().STRATEGY_SET):
            raise _memory().StrategyTransferError("strategy advice must be a bounded array")
        selected: set[str] = set()
        flattened: list[dict[str, Any]] = []
        for item in advice:
            if not isinstance(item, _memory().Mapping) or set(item) != {
                "strategy", "evidence_lesson_ids", "source_families", "confidence"
            }:
                raise _memory().StrategyTransferError("strategy advice fields are invalid")
            strategy = item.get("strategy")
            if (
                not isinstance(strategy, str)
                or strategy not in _memory().STRATEGY_SET
                or strategy not in desired
                or strategy in selected
            ):
                raise _memory().StrategyTransferError("strategy advice is unsupported or duplicated")
            selected.add(strategy)
            confidence = item.get("confidence")
            if (
                isinstance(confidence, bool)
                or not isinstance(confidence, (int, float))
                or not _memory().math.isfinite(float(confidence))
                or not 0.0 <= float(confidence) <= 1.0
            ):
                raise _memory().StrategyTransferError("strategy advice confidence is invalid")
            evidence_ids = item.get("evidence_lesson_ids")
            families = item.get("source_families")
            if (
                not isinstance(evidence_ids, list)
                or not 1 <= len(evidence_ids) <= 5
                or len(evidence_ids) != len(set(evidence_ids))
                or not isinstance(families, list)
                or not 1 <= len(families) <= 5
                or len(families) != len(set(families))
            ):
                raise _memory().StrategyTransferError("strategy advice evidence is malformed")
            normalized_families: list[str] = []
            for family in families:
                if not isinstance(family, str) or family not in self.PREDICTION_FAMILIES:
                    raise _memory().StrategyTransferError("strategy advice family is unsupported")
                if family == target_family:
                    raise _memory().StrategyTransferError("same-family strategy transfer is forbidden")
                normalized_families.append(family)
            for lesson_id in evidence_ids:
                if not isinstance(lesson_id, str):
                    raise _memory().StrategyTransferError("strategy lesson ID must be opaque text")
                match = _memory().re.fullmatch(r"lesson:([1-9][0-9]*)", lesson_id)
                if match is None:
                    raise _memory().StrategyTransferError("strategy lesson ID is malformed")
                memory_id = self._prediction_optional_id(
                    int(match.group(1)), "memory_id"
                )
                flattened.append({
                    "memory_id": memory_id,
                    "strategy": strategy,
                    "declared_source_families": tuple(sorted(normalized_families)),
                })
        if len(flattened) > 32:
            raise _memory().StrategyTransferError("strategy application exceeds 32 receipts")
        return flattened

    def _strategy_transfer_source_metadata(
        self,
        memory_id: int,
        *,
        project_id: int,
        target_family: str,
        strategy: str,
        as_of: str,
    ) -> dict[str, Any] | None:
        try:
            rows = self.db.execute(
                """SELECT m.family AS source_family, m.outcome_status,
                          lp.prediction_id, lp.provenance_sha256,
                          lc.project_id, lc.lifecycle_status, lc.superseded_by,
                          lc.control_sha256, o.observation_sha256
                   FROM memories AS m
                   JOIN lesson_provenance AS lp ON lp.memory_id=m.id
                   JOIN lesson_controls AS lc ON lc.memory_id=m.id
                   JOIN task_strategy_observations AS o
                     ON o.prediction_id=lp.prediction_id
                   WHERE m.id=? AND m.kind='lesson'""",
                (int(memory_id),),
            ).fetchall()
        except _memory().sqlite3.DatabaseError:
            return None
        if len(rows) != 1:
            return None
        row = rows[0]
        source_family = str(row["source_family"] or "")
        observation_valid, observation = self._task_strategy_observation_validation(
            int(row["prediction_id"]), project_id=project_id
        )
        if (
            source_family not in self.PREDICTION_FAMILIES
            or source_family == target_family
            or str(row["outcome_status"] or "") != "complete"
            or int(row["project_id"]) != project_id
            or str(row["lifecycle_status"]) != "active"
            or row["superseded_by"] is not None
            or not observation_valid
            or not isinstance(observation, dict)
            or observation["source_family"] != source_family
            or strategy not in observation["strategies"]
            or not self._lesson_provenance_validation(memory_id)[0]
            or not self._lesson_control_validation(
                memory_id, project_id=project_id, as_of=as_of
            )[0]
            or not bool(self.calibration_gate(source_family)["allowed"])
        ):
            return None
        return {
            "source_family": source_family,
            "observation_sha256": str(row["observation_sha256"]),
            "provenance_sha256": str(row["provenance_sha256"]),
            "control_sha256": str(row["control_sha256"]),
        }

    def record_strategy_transfer_applications(
        self,
        prediction_id: int,
        target_family: str,
        selection_payload_or_rows: Mapping[str, Any],
        *,
        mode: str = "observe",
        applied: bool = False,
    ) -> int:
        """Persist idempotent receipts for safe cross-family procedural advice."""
        normalized_prediction = self._prediction_optional_id(
            prediction_id, "prediction_id"
        )
        if target_family not in self.PREDICTION_FAMILIES:
            raise ValueError(f"Unknown target family: {target_family}")
        if mode not in _memory().STRATEGY_TRANSFER_APPLICATION_MODES:
            raise ValueError("strategy transfer mode must be observe or advise")
        if not isinstance(applied, bool):
            raise ValueError("strategy transfer applied must be a boolean")
        if applied and mode not in {"advise", "trial"}:
            raise ValueError("observe-mode strategy evidence cannot be applied")
        expected = self._strategy_transfer_selection_rows(
            selection_payload_or_rows, target_family=target_family
        )
        if str(selection_payload_or_rows.get("task_id")) != (
            f"prediction:{normalized_prediction}"
        ):
            raise _memory().StrategyTransferError(
                "strategy transfer selection does not match its prediction"
            )
        stamp = self._canonical_utc_timestamp(_memory().now_iso())
        if stamp is None:
            raise RuntimeError("Current UTC timestamp is unavailable")
        with self._immediate_transaction():
            prediction = self._strategy_prediction_row(normalized_prediction)
            if (
                prediction is None
                or str(prediction["family"]) != target_family
                or str(prediction["origin"])
                not in _memory().LESSON_REUSABLE_PREDICTION_ORIGINS
                or (
                    target_family in _memory().LESSON_EVIDENCE_REQUIRED_FAMILIES
                    and str(prediction["predicted_verification"]) == "not_applicable"
                )
            ):
                raise ValueError(
                    "Strategy application must bind to the matching active prediction"
                )
            project_id = self._strategy_prediction_project(prediction)
            if project_id is None:
                raise ValueError("Strategy application lacks an enabled project scope")
            if mode == "trial":
                trial_row = self.db.execute(
                    """SELECT * FROM strategy_transfer_trial_assignments
                       WHERE prediction_id=?""",
                    (normalized_prediction,),
                ).fetchone()
                if trial_row is None or not self._strategy_transfer_trial_assignment_validation(
                    trial_row, require_prompt=True
                )[0]:
                    raise _memory().StrategyTransferTrialError(
                        "trial application lacks a valid pre-prompt assignment receipt"
                    )
                if (
                    int(trial_row["project_id"]) != project_id
                    or str(trial_row["target_family"]) != target_family
                    or bool(int(trial_row["advice_applied"])) != applied
                    or (str(trial_row["arm"]) == "treatment") != applied
                ):
                    raise _memory().StrategyTransferTrialError(
                        "trial application does not match its randomized arm"
                    )
                trial_selection = self._strategy_transfer_trial_selection_material(
                    prediction_id=normalized_prediction,
                    target_family=target_family,
                    selection=selection_payload_or_rows,
                    flattened=expected,
                )
                if _memory().sha256_json(trial_selection) != str(
                    trial_row["selection_sha256"]
                ):
                    raise _memory().StrategyTransferTrialError(
                        "trial application selection differs from assignment"
                    )
            elif mode == "observe" and applied:
                raise ValueError("observe-mode strategy evidence cannot be applied")
            expected_keys: set[tuple[int, str]] = set()
            prepared: list[dict[str, Any]] = []
            for rank, item in enumerate(expected, 1):
                key = (int(item["memory_id"]), str(item["strategy"]))
                if key in expected_keys:
                    raise _memory().StrategyTransferError(
                        "strategy selection contains a duplicate evidence receipt"
                    )
                expected_keys.add(key)
                source = self._strategy_transfer_source_metadata(
                    key[0],
                    project_id=project_id,
                    target_family=target_family,
                    strategy=key[1],
                    as_of=stamp,
                )
                if source is None:
                    raise ValueError(
                        "Strategy application references ineligible source evidence"
                    )
                if tuple(sorted({source["source_family"]})) != tuple(
                    item["declared_source_families"]
                ):
                    # The selector reports all source families for an advice
                    # item. Validate the full set after the individual rows are
                    # assembled instead of trusting model- or caller-supplied text.
                    pass
                prepared.append({**item, **source, "rank": rank})
            by_strategy: dict[str, set[str]] = {}
            declared_by_strategy: dict[str, tuple[str, ...]] = {}
            for item in prepared:
                strategy = str(item["strategy"])
                by_strategy.setdefault(strategy, set()).add(
                    str(item["source_family"])
                )
                declared_by_strategy[strategy] = tuple(
                    item["declared_source_families"]
                )
            if any(
                tuple(sorted(families)) != declared_by_strategy[strategy]
                for strategy, families in by_strategy.items()
            ):
                raise _memory().StrategyTransferError(
                    "strategy advice source families do not match its evidence"
                )
            existing_rows = self.db.execute(
                """SELECT * FROM strategy_transfer_applications
                   WHERE prediction_id=? ORDER BY rank, id""",
                (normalized_prediction,),
            ).fetchall()
            existing_keys = {
                (int(row["memory_id"]), str(row["strategy"]))
                for row in existing_rows
            }
            if not existing_keys.issubset(expected_keys):
                raise ValueError("Conflicting strategy application replay")
            prepared_by_key = {
                (int(item["memory_id"]), str(item["strategy"])): item
                for item in prepared
            }
            for row in existing_rows:
                key = (int(row["memory_id"]), str(row["strategy"]))
                item = prepared_by_key[key]
                if (
                    int(row["project_id"]) != project_id
                    or str(row["target_family"]) != target_family
                    or str(row["source_family"]) != item["source_family"]
                    or str(row["mode"]) != mode
                    or bool(int(row["applied"])) != applied
                    or int(row["rank"]) != int(item["rank"])
                    or not self._strategy_transfer_application_validation(
                        int(row["id"])
                    )[0]
                ):
                    raise ValueError("Conflicting or invalid strategy application replay")
            if prediction["resolved_at"] is not None:
                if existing_keys == expected_keys:
                    return 0
                raise ValueError("Resolved predictions cannot gain strategy applications")
            inserted = 0
            for item in prepared:
                key = (int(item["memory_id"]), str(item["strategy"]))
                if key in existing_keys:
                    continue
                material = self._strategy_transfer_application_material(
                    created_at=stamp,
                    prediction_id=normalized_prediction,
                    memory_id=key[0],
                    project_id=project_id,
                    strategy=key[1],
                    source_family=str(item["source_family"]),
                    target_family=target_family,
                    mode=mode,
                    applied=applied,
                    rank=int(item["rank"]),
                    source_observation_sha256=str(item["observation_sha256"]),
                    source_provenance_sha256=str(item["provenance_sha256"]),
                    source_control_sha256=str(item["control_sha256"]),
                    resolved_at=None,
                    successful=None,
                )
                cursor = self.db.execute(
                    """INSERT INTO strategy_transfer_applications(
                           created_at, prediction_id, memory_id, project_id,
                           strategy, source_family, target_family, mode, rank,
                           applied,
                           source_observation_sha256, source_provenance_sha256,
                           source_control_sha256, resolved_at, successful,
                           application_sha256
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?)""",
                    (
                        stamp, normalized_prediction, key[0], project_id, key[1],
                        item["source_family"], target_family, mode, item["rank"],
                        int(applied),
                        item["observation_sha256"], item["provenance_sha256"],
                        item["control_sha256"],
                        self._strategy_transfer_application_digest(material),
                    ),
                )
                inserted += int(cursor.rowcount == 1)
        return inserted

    def _strategy_transfer_application_validation(
        self,
        application_id: int,
    ) -> tuple[bool, str]:
        try:
            row = self.db.execute(
                """SELECT a.*, p.created_at AS prediction_created_at,
                          p.task_id, p.conversation_id, p.origin,
                          p.family AS prediction_family,
                          p.predicted_verification, p.resolved_at AS prediction_resolved_at,
                          p.actual_status AS prediction_actual_status,
                          p.evidence_ok AS prediction_evidence_ok
                   FROM strategy_transfer_applications AS a
                   JOIN task_predictions AS p ON p.id=a.prediction_id
                   WHERE a.id=?""",
                (int(application_id),),
            ).fetchone()
        except (_memory().sqlite3.DatabaseError, TypeError, ValueError):
            return False, "application_unavailable"
        if row is None:
            return False, "application_missing"
        try:
            created_at = self._canonical_utc_timestamp(row["created_at"])
            prediction_created = self._canonical_utc_timestamp(
                row["prediction_created_at"]
            )
            raw_resolved = row["resolved_at"]
            prediction_raw_resolved = row["prediction_resolved_at"]
            resolved_at = (
                None if raw_resolved is None
                else self._canonical_utc_timestamp(raw_resolved)
            )
            prediction_resolved = (
                None if prediction_raw_resolved is None
                else self._canonical_utc_timestamp(prediction_raw_resolved)
            )
            if (
                created_at is None
                or prediction_created is None
                or str(row["created_at"]) != created_at
                or str(row["prediction_created_at"]) != prediction_created
                or (raw_resolved is not None and resolved_at is None)
                or (
                    prediction_raw_resolved is not None
                    and prediction_resolved is None
                )
            ):
                return False, "timestamp_invalid"
            if _memory().datetime.fromisoformat(created_at) < _memory().datetime.fromisoformat(
                prediction_created
            ):
                return False, "application_predates_prediction"
            if _memory().datetime.fromisoformat(created_at) > _memory().datetime.now(
                _memory().timezone.utc
            ) + _memory().timedelta(minutes=5):
                return False, "application_in_future"
            project_id = self._project_id(int(row["project_id"]))
            target_family = str(row["target_family"])
            source_family = str(row["source_family"])
            strategy = str(row["strategy"])
            applied = bool(int(row["applied"]))
            if (
                target_family not in self.PREDICTION_FAMILIES
                or source_family not in self.PREDICTION_FAMILIES
                or source_family == target_family
                or strategy not in _memory().STRATEGY_SET
                or str(row["prediction_family"]) != target_family
                or str(row["origin"]) not in _memory().LESSON_REUSABLE_PREDICTION_ORIGINS
                or str(row["mode"]) not in _memory().STRATEGY_TRANSFER_APPLICATION_MODES
                or (applied and str(row["mode"]) not in {"advise", "trial"})
                or not 1 <= int(row["rank"]) <= 32
            ):
                return False, "application_scope_invalid"
            prediction = self._strategy_prediction_row(int(row["prediction_id"]))
            if prediction is None or self._strategy_prediction_project(
                prediction
            ) != project_id:
                return False, "project_mismatch"
            if prediction_resolved is None:
                if resolved_at is not None or row["successful"] is not None:
                    return False, "forged_resolution"
                successful = None
            else:
                if (
                    resolved_at != prediction_resolved
                    or isinstance(row["successful"], bool)
                    or not isinstance(row["successful"], int)
                    or int(row["successful"])
                    != int(
                        str(row["prediction_actual_status"]) == "complete"
                        and int(row["prediction_evidence_ok"] or 0) == 1
                    )
                ):
                    return False, "resolution_mismatch"
                successful = int(row["successful"])
            source = self._strategy_transfer_source_metadata(
                int(row["memory_id"]),
                project_id=project_id,
                target_family=target_family,
                strategy=strategy,
                as_of=created_at,
            )
            if source is None:
                return False, "source_invalid"
            if (
                source["source_family"] != source_family
                or source["observation_sha256"]
                != str(row["source_observation_sha256"])
                or source["provenance_sha256"]
                != str(row["source_provenance_sha256"])
                or source["control_sha256"]
                != str(row["source_control_sha256"])
            ):
                return False, "source_receipt_mismatch"
            material = self._strategy_transfer_application_material(
                created_at=created_at,
                prediction_id=int(row["prediction_id"]),
                memory_id=int(row["memory_id"]),
                project_id=project_id,
                strategy=strategy,
                source_family=source_family,
                target_family=target_family,
                mode=str(row["mode"]),
                applied=applied,
                rank=int(row["rank"]),
                source_observation_sha256=str(
                    row["source_observation_sha256"]
                ),
                source_provenance_sha256=str(
                    row["source_provenance_sha256"]
                ),
                source_control_sha256=str(row["source_control_sha256"]),
                resolved_at=resolved_at,
                successful=successful,
            )
            if str(row["application_sha256"]) != (
                self._strategy_transfer_application_digest(material)
            ):
                return False, "application_digest_mismatch"
        except (OverflowError, TypeError, ValueError):
            return False, "application_invalid"
        return True, "valid"

    def _strategy_transfer_harm_failure_count(
        self,
        memory_id: int,
        *,
        strategy: str,
        target_family: str,
    ) -> tuple[bool, int, str]:
        if strategy not in _memory().STRATEGY_SET or target_family not in self.PREDICTION_FAMILIES:
            return False, 0, "harm_scope_invalid"
        try:
            rows = self.db.execute(
                """SELECT id, prediction_id
                   FROM strategy_transfer_applications
                   WHERE memory_id=? AND strategy=? AND target_family=?
                     AND applied=1 AND resolved_at IS NOT NULL AND successful=0
                   ORDER BY id""",
                (int(memory_id), strategy, target_family),
            ).fetchall()
        except (_memory().sqlite3.DatabaseError, TypeError, ValueError):
            return False, 0, "harm_ledger_query_unavailable"
        failed_predictions: set[int] = set()
        for row in rows:
            valid, _reason = self._strategy_transfer_application_validation(
                int(row["id"])
            )
            if not valid:
                return False, 0, "harm_ledger_validation_failed"
            failed_predictions.add(int(row["prediction_id"]))
        return True, len(failed_predictions), "available"

    def _strategy_transfer_ledger_health(self) -> dict[str, Any]:
        try:
            rows = self.db.execute(
                """SELECT id, prediction_id, memory_id, strategy,
                          target_family, applied, resolved_at, successful
                   FROM strategy_transfer_applications ORDER BY id"""
            ).fetchall()
        except _memory().sqlite3.DatabaseError:
            return {
                "available": False,
                "valid_receipts": 0,
                "invalid_receipts": 1,
                "harm_quarantines": 0,
            }
        valid_receipts = 0
        invalid_receipts = 0
        failures: dict[tuple[int, str, str], set[int]] = {}
        for row in rows:
            if not self._strategy_transfer_application_validation(int(row["id"]))[0]:
                invalid_receipts += 1
                continue
            valid_receipts += 1
            if (
                bool(int(row["applied"]))
                and row["resolved_at"] is not None
                and int(row["successful"] or 0) == 0
            ):
                key = (
                    int(row["memory_id"]), str(row["strategy"]),
                    str(row["target_family"]),
                )
                failures.setdefault(key, set()).add(int(row["prediction_id"]))
        return {
            "available": True,
            "valid_receipts": valid_receipts,
            "invalid_receipts": invalid_receipts,
            "harm_quarantines": sum(
                1 for prediction_ids in failures.values()
                if len(prediction_ids) >= 2
            ),
        }

    def strategy_transfer_effectiveness(
        self,
        family: str | None = None,
    ) -> list[dict[str, Any]]:
        if family is not None and family not in self.PREDICTION_FAMILIES:
            raise ValueError(f"Unknown target family: {family}")
        clause = "WHERE target_family=?" if family else ""
        parameters: tuple[Any, ...] = (family,) if family else ()
        try:
            rows = self.db.execute(
                f"""SELECT id, target_family, strategy, mode, applied, prediction_id,
                            resolved_at, successful
                     FROM strategy_transfer_applications {clause}
                     ORDER BY target_family, strategy, mode, id""",
                parameters,
            ).fetchall()
        except _memory().sqlite3.DatabaseError:
            return []
        grouped: dict[tuple[str, str, str, bool], dict[str, Any]] = {}
        for row in rows:
            if not self._strategy_transfer_application_validation(int(row["id"]))[0]:
                continue
            key = (
                str(row["target_family"]), str(row["strategy"]),
                str(row["mode"]), bool(int(row["applied"])),
            )
            aggregate = grouped.setdefault(key, {
                "target_family": key[0],
                "strategy": key[1],
                "mode": key[2],
                "applied": key[3],
                "applications": 0,
                "_predictions": set(),
                "_resolved": {},
            })
            aggregate["applications"] += 1
            prediction_id = int(row["prediction_id"])
            aggregate["_predictions"].add(prediction_id)
            if row["resolved_at"] is not None:
                aggregate["_resolved"][prediction_id] = int(row["successful"])
        result: list[dict[str, Any]] = []
        for key in sorted(grouped):
            aggregate = grouped[key]
            resolved_outcomes = aggregate.pop("_resolved")
            resolved = len(resolved_outcomes)
            predictions = len(aggregate.pop("_predictions"))
            successes = sum(int(value) for value in resolved_outcomes.values())
            aggregate["target_predictions"] = predictions
            aggregate["resolved"] = resolved
            aggregate["successes"] = successes
            aggregate["success_rate"] = successes / resolved if resolved else None
            result.append(aggregate)
        return result

    def strategy_transfer_readiness(
        self,
        *,
        mode: str = "observe",
        evaluator_version: str | None = None,
        evaluator_sha256: str | None = None,
        config_sha256: str | None = None,
        project_id: int | None = None,
        target_family: str | None = None,
        strategies: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        if mode not in _memory().STRATEGY_TRANSFER_APPLICATION_MODES:
            raise ValueError(
                "strategy transfer readiness mode must be observe, trial, or advise"
            )
        valid_observations = 0
        invalid_observations = 0
        observed_strategies: _memory().Counter[str] = _memory().Counter()
        calibrated_families: set[str] = set()
        try:
            rows = self.db.execute(
                """SELECT prediction_id, source_family
                   FROM task_strategy_observations ORDER BY prediction_id"""
            ).fetchall()
        except _memory().sqlite3.DatabaseError:
            rows = []
        for row in rows:
            valid, payload = self._task_strategy_observation_validation(
                int(row["prediction_id"])
            )
            if not valid or not isinstance(payload, dict):
                invalid_observations += 1
                continue
            valid_observations += 1
            observed_strategies.update(payload["strategies"])
            family = str(row["source_family"])
            if bool(self.calibration_gate(family)["allowed"]):
                calibrated_families.add(family)
        valid_applications = 0
        invalid_applications = 0
        observe_outcomes: dict[int, int] = {}
        applied_outcomes: dict[int, int] = {}
        applied_pairs: set[tuple[str, str]] = set()
        applied_failures: dict[tuple[int, str, str], set[int]] = {}
        try:
            app_rows = self.db.execute(
                """SELECT id, prediction_id, memory_id, strategy,
                          source_family, target_family, mode, applied,
                          resolved_at, successful
                   FROM strategy_transfer_applications ORDER BY id"""
            ).fetchall()
        except _memory().sqlite3.DatabaseError:
            app_rows = []
        for row in app_rows:
            valid = self._strategy_transfer_application_validation(int(row["id"]))[0]
            if not valid:
                invalid_applications += 1
                continue
            valid_applications += 1
            if row["resolved_at"] is None:
                continue
            prediction_id = int(row["prediction_id"])
            successful = int(row["successful"])
            is_applied = bool(int(row["applied"]))
            outcomes = applied_outcomes if is_applied else observe_outcomes
            prior = outcomes.get(prediction_id)
            if prior is not None and prior != successful:
                invalid_applications += 1
                continue
            outcomes[prediction_id] = successful
            if is_applied:
                pair = (str(row["source_family"]), str(row["target_family"]))
                applied_pairs.add(pair)
                if not successful:
                    key = (
                        int(row["memory_id"]), str(row["strategy"]), pair[1]
                    )
                    applied_failures.setdefault(key, set()).add(prediction_id)
        observe_resolved = len(observe_outcomes)
        observe_successes = sum(observe_outcomes.values())
        observe_success_rate = (
            observe_successes / observe_resolved if observe_resolved else None
        )
        applied_resolved = len(applied_outcomes)
        applied_successes = sum(applied_outcomes.values())
        applied_success_rate = (
            applied_successes / applied_resolved if applied_resolved else None
        )
        quarantine_count = sum(
            1 for failures in applied_failures.values() if len(failures) >= 2
        )
        ledger_health = self._strategy_transfer_ledger_health()
        invalid_applications = max(
            invalid_applications, int(ledger_health["invalid_receipts"])
        )
        quarantine_count = max(
            quarantine_count, int(ledger_health["harm_quarantines"])
        )
        try:
            attestation_rows = self.db.execute(
                """SELECT * FROM strategy_transfer_attestations
                   ORDER BY id DESC"""
            ).fetchall()
        except _memory().sqlite3.DatabaseError:
            attestation_rows = []

        def latest_valid_attestation(kind: str) -> sqlite3.Row | None:
            for candidate in attestation_rows:
                if (
                    str(candidate["kind"]) == kind
                    and self._strategy_transfer_stored_attestation_validation(
                        candidate
                    )[0]
                ):
                    return candidate
            return None

        benchmark_row = latest_valid_attestation("sealed_benchmark")
        applied_ab_row = latest_valid_attestation("applied_ab")
        benchmark_valid = benchmark_row is not None
        applied_ab_valid = applied_ab_row is not None
        scope_valid = mode != "advise"
        promoted_manifest_valid = False
        normalized_project: int | None = None
        normalized_family: str | None = None
        normalized_strategies: tuple[str, ...] = ()
        if mode == "advise":
            try:
                normalized_project = self._prediction_optional_id(
                    project_id, "project_id"
                )
                normalized_family = str(target_family or "")
                if normalized_family not in self.PREDICTION_FAMILIES:
                    raise ValueError("target family is invalid")
                if (
                    strategies is None
                    or isinstance(strategies, (str, bytes))
                    or not isinstance(strategies, _memory().Sequence)
                ):
                    raise ValueError("selected strategies are required")
                normalized_strategies = tuple(sorted(str(item) for item in strategies))
                if (
                    not normalized_strategies
                    or len(normalized_strategies) != len(set(normalized_strategies))
                    or any(item not in _memory().STRATEGY_SET for item in normalized_strategies)
                ):
                    raise ValueError("selected strategies are invalid")
            except (TypeError, ValueError):
                normalized_project = None
                normalized_family = None
                normalized_strategies = ()
            scoped_attestation: _memory().sqlite3.Row | None = None
            if (
                normalized_project is not None
                and normalized_family is not None
                and normalized_strategies
            ):
                for candidate in attestation_rows:
                    if str(candidate["kind"]) != "applied_ab":
                        continue
                    if not self._strategy_transfer_stored_attestation_validation(
                        candidate
                    )[0]:
                        continue
                    try:
                        candidate_artifact = _memory().json.loads(
                            str(candidate["artifact_json"])
                        )
                        if candidate_artifact.get("schema_version") != (
                            "strategy_transfer_applied_ab_attestation/v2"
                        ):
                            continue
                        candidate_manifest_row = self.db.execute(
                            """SELECT * FROM strategy_transfer_trial_manifests
                               WHERE manifest_sha256=?""",
                            (str(candidate_artifact["assignment_manifest_sha256"]),),
                        ).fetchone()
                        if candidate_manifest_row is None:
                            continue
                        manifest_ok, candidate_manifest = (
                            self._strategy_transfer_trial_manifest_validation(
                                candidate_manifest_row
                            )
                        )
                        if (
                            not manifest_ok
                            or not isinstance(candidate_manifest, dict)
                            or candidate_manifest["status"] != "promoted"
                            or int(candidate_manifest["project_id"])
                            != normalized_project
                            or normalized_family
                            not in candidate_manifest["target_families"]
                            or tuple(candidate_manifest["strategies"])
                            != normalized_strategies
                        ):
                            continue
                        scoped_attestation = candidate
                        promoted_manifest_valid = True
                        scope_valid = True
                        break
                    except (
                        _memory().json.JSONDecodeError, KeyError, _memory().sqlite3.DatabaseError,
                        TypeError, ValueError,
                    ):
                        continue
            applied_ab_row = scoped_attestation
            applied_ab_valid = applied_ab_row is not None
        causal_trial_valid = False
        if applied_ab_row is not None:
            try:
                applied_artifact = _memory().json.loads(
                    str(applied_ab_row["artifact_json"])
                )
                causal_trial_valid = (
                    applied_artifact.get("schema_version")
                    == "strategy_transfer_applied_ab_attestation/v2"
                    and applied_artifact.get("claim_scope")
                    == "pre_outcome_randomized_trial_activation_evidence"
                    and applied_artifact.get("all_exit_criteria") is True
                )
            except (_memory().json.JSONDecodeError, TypeError, ValueError):
                causal_trial_valid = False
        installed_binding: dict[str, Any] | None = None
        supplied_binding_matches = True
        try:
            installed_binding = self._strategy_transfer_trial_contract()
            supplied = (
                ("evaluator_version", evaluator_version),
                ("evaluator_sha256", evaluator_sha256),
                ("config_sha256", config_sha256),
            )
            supplied_binding_matches = all(
                value is None or str(value) == str(installed_binding[field])
                for field, value in supplied
            )
            evaluator_version = str(installed_binding["evaluator_version"])
            evaluator_sha256 = str(installed_binding["evaluator_sha256"])
            config_sha256 = str(installed_binding["config_sha256"])
        except (
            OSError, _memory().StrategyTransferError, _memory().StrategyTransferTrialError,
            TypeError, ValueError,
        ):
            installed_binding = None
        expected_binding_present = installed_binding is not None
        binding_matches = False
        if benchmark_valid and applied_ab_valid and expected_binding_present:
            binding_matches = all(
                str(applied_ab_row[field]) == expected
                for field, expected in (
                    ("evaluator_version", str(evaluator_version)),
                    ("evaluator_sha256", str(evaluator_sha256)),
                    ("config_sha256", str(config_sha256)),
                )
            )
        reasons: list[str] = []
        if mode != "advise":
            reasons.append("explicit advise mode is required for activation")
        if mode == "advise" and not scope_valid:
            reasons.append(
                "advise requires one promoted trial matching the exact project, "
                "target family, and selected strategy set"
            )
        elif mode == "advise" and not promoted_manifest_valid:
            reasons.append("the matching trial manifest has not been promoted")
        if not benchmark_valid:
            reasons.append("sealed strategy-transfer benchmark attestation is absent or invalid")
        if not applied_ab_valid:
            reasons.append("real applied A/B attestation is absent or invalid")
        elif not causal_trial_valid:
            reasons.append(
                "applied A/B evidence is retrospective rather than a valid "
                "pre-outcome randomized trial"
            )
        if not expected_binding_present:
            reasons.append("current evaluator version and evaluator/config hashes are required")
        elif not supplied_binding_matches:
            reasons.append("caller-supplied evaluator or config binding is not current")
        elif not binding_matches:
            reasons.append("current evaluator or config binding does not match attestations")
        if invalid_observations:
            reasons.append(
                f"requires zero invalid observation rows; has {invalid_observations}"
            )
        if not ledger_health["available"] or invalid_applications:
            reasons.append(
                f"requires zero invalid receipt rows; has {invalid_applications}"
            )
        if quarantine_count:
            reasons.append(
                f"requires zero empirical harm quarantines; has {quarantine_count}"
            )
        allowed = not reasons
        return {
            "schema": "jarvis.strategy-transfer-readiness.v1",
            "allowed": allowed,
            "reporting_only": not allowed,
            "requested_mode": mode,
            "reasons": reasons,
            "requirements": {
                "sealed_benchmark_attestation": True,
                "independent_applied_ab_evidence": True,
                "pre_outcome_randomized_trial_assignment": True,
                "minimum_resolved_applied_targets": 20,
                "minimum_source_target_pairs": 3,
                "minimum_success_rate": 0.70,
                "maximum_invalid_receipts": 0,
            },
            "valid_observations": valid_observations,
            "invalid_observations": invalid_observations,
            "observed_strategies": {
                strategy: int(observed_strategies.get(strategy, 0))
                for strategy in sorted(_memory().STRATEGY_SET)
            },
            "calibrated_source_families": sorted(calibrated_families),
            "valid_applications": valid_applications,
            "invalid_applications": invalid_applications,
            "resolved_observe_targets": observe_resolved,
            "successful_observe_targets": observe_successes,
            "observe_success_rate": observe_success_rate,
            "resolved_applied_targets": applied_resolved,
            "successful_applied_targets": applied_successes,
            "applied_success_rate": applied_success_rate,
            "sealed_benchmark_attested": benchmark_valid,
            "applied_ab_evidence_attested": applied_ab_valid,
            "activation_trial_supported": True,
            "causal_trial_attested": causal_trial_valid,
            "attestation_binding_matches_current": binding_matches,
            "advise_scope_matches_promoted_manifest": scope_valid
                and promoted_manifest_valid,
            "source_target_pairs": [
                {"source_family": source, "target_family": target}
                for source, target in sorted(applied_pairs)
            ],
            "empirical_harm_quarantines": quarantine_count,
            "candidate_filter_health": self.strategy_transfer_candidate_health(),
            "effectiveness": self.strategy_transfer_effectiveness(),
        }
