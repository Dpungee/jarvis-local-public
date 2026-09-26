"""Current Memory domain extracted without changing method control flow.

Global dependencies resolve through jarvis.memory at call time. The facade keeps
public imports, patch seams, constants and authorization helpers authoritative.
"""
from __future__ import annotations

from typing import Any
from .memory_runtime import (_memory)


class PredictionsMemoryMixin:
    """Mechanically extracted current Memory methods."""

    @staticmethod
    def _prediction_optional_id(value: int | None, label: str) -> int | None:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"{label} must be a positive integer or None")
        return value

    @staticmethod
    def _prediction_run_digest(value: str | None) -> str | None:
        if value is None:
            return None
        run_id = str(value).strip()
        if (
            not run_id
            or len(run_id) > 200
            or _memory().re.fullmatch(r"[A-Za-z0-9._:-]+", run_id) is None
            or _memory().contains_secret(run_id)
        ):
            raise ValueError("Prediction run ID is invalid")
        return _memory().hashlib.sha256(
            ("jarvis-presence-prediction-v1\0" + run_id).encode("utf-8")
        ).hexdigest()

    def record_prediction(
        self,
        *,
        family: str,
        profile: str,
        model: str,
        predicted_success: float,
        predicted_steps: int,
        predicted_verification: str,
        basis: str = "prior",
        origin: str = "interactive",
        task_id: int | None = None,
        conversation_id: int | None = None,
        run_id: str | None = None,
    ) -> int:
        """Persist one controlled-vocabulary prediction without prompt or evidence text."""
        if family not in self.PREDICTION_FAMILIES:
            raise ValueError(f"Unknown task family: {family}")
        if isinstance(predicted_success, bool):
            raise ValueError("predicted_success must be a finite number between 0 and 1")
        try:
            confidence = float(predicted_success)
        except (TypeError, ValueError):
            raise ValueError(
                "predicted_success must be a finite number between 0 and 1"
            ) from None
        if not _memory().math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
            raise ValueError("predicted_success must be a finite number between 0 and 1")
        if (
            isinstance(predicted_steps, bool)
            or not isinstance(predicted_steps, int)
            or predicted_steps < 0
        ):
            raise ValueError("predicted_steps must be a non-negative integer")
        if predicted_verification not in self.PREDICTION_VERIFICATION:
            raise ValueError(f"Unknown prediction verification: {predicted_verification}")
        if basis not in {"prior", "competence", "model"}:
            raise ValueError("basis must be prior, competence, or model")
        if origin not in self.PREDICTION_ORIGINS:
            raise ValueError(f"Unknown prediction origin: {origin}")
        safe_profile = _memory()._validated_nonsecret_metadata(profile, "Prediction profile")
        safe_model = _memory()._validated_nonsecret_metadata(model, "Prediction model")
        if not safe_profile or not safe_model:
            raise ValueError("Prediction profile and model must not be empty")
        normalized_task_id = self._prediction_optional_id(task_id, "task_id")
        normalized_conversation_id = self._prediction_optional_id(
            conversation_id, "conversation_id"
        )
        run_id_sha256 = self._prediction_run_digest(run_id)
        with self._immediate_transaction():
            cur = self.db.execute(
                """INSERT INTO task_predictions(
                       created_at, task_id, conversation_id, origin, family, profile,
                       model, predicted_success, predicted_steps,
                       predicted_verification, basis, run_id_sha256
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    _memory().now_iso(), normalized_task_id, normalized_conversation_id, origin,
                    family, safe_profile[:40], safe_model[:200], confidence,
                    predicted_steps, predicted_verification, basis, run_id_sha256,
                ),
            )
            return int(cur.lastrowid)

    def resolve_prediction(
        self,
        prediction_id: int,
        *,
        actual_status: str,
        actual_steps: int | None,
        evidence_ok: bool | None,
        failure_class: str | None = None,
        primary_tool: str | None = None,
    ) -> bool:
        """Resolve one prediction exactly once; evidence may be not applicable.

        ``primary_tool`` is the one tool name this outcome records, stamped
        onto every ``lesson_applications`` row the resolution closes (M4
        design 3.4, H-7).  ``task_predictions`` has no tool column and M4 does
        not add one, so the learning ladder builds a staged document's tool
        line from these values alone — one per outcome, which makes the line a
        **sample and not a union**, and a row written before schema 49 keeps
        ``NULL`` and honestly reports "none recorded".  The Agent passes the
        alphabetically first non-internal tool actually called in the turn.
        The value is bounded to ``[a-z][a-z0-9_]{0,63}`` and screened by
        ``screen_endpoint`` and ``contains_secret``; anything else is recorded
        as ``NULL`` rather than raising, because losing a resolved prediction
        to a strange tool name would change the very population the
        calibration ledger exists to keep honest.
        """
        normalized_id = self._prediction_optional_id(prediction_id, "prediction_id")
        safe_tool = _memory().screened_tool_name(primary_tool)
        if actual_status not in {"complete", "incomplete", "failed"}:
            raise ValueError("actual_status must be complete, incomplete, or failed")
        if actual_steps is not None and (
            isinstance(actual_steps, bool)
            or not isinstance(actual_steps, int)
            or actual_steps < 0
        ):
            raise ValueError("actual_steps must be a non-negative integer or None")
        if evidence_ok is not None and not isinstance(evidence_ok, bool):
            raise ValueError("evidence_ok must be true, false, or None")
        if actual_status == "complete" and evidence_ok is False:
            # Completion without the task's required evidence is not a success.
            # Keeping it as complete would train the competence model to reward
            # confident prose after a failed or missing real-world action.
            actual_status = "incomplete"
            failure_class = failure_class or "verification_absent"
        if actual_status == "complete":
            failure_class = None
        elif failure_class is None:
            failure_class = "unknown"
        if failure_class is not None and failure_class not in self.PREDICTION_FAILURE_CLASSES:
            raise ValueError(f"Unknown failure class: {failure_class}")
        try:
            return self._resolve_prediction_locked(
                normalized_id,
                actual_status=actual_status,
                actual_steps=actual_steps,
                evidence_ok=evidence_ok,
                failure_class=failure_class,
                safe_tool=safe_tool,
            )
        except (_memory().sqlite3.Error, _memory().memory_spine.SpineError) as exc:
            # S-3: the worker holds the write lock; the turn degrades to a
            # recorded non-fatal outcome instead of failing.  Every ValueError
            # above is untouched -- those are caller bugs and still raise.
            # ``SpineError`` joins it for ruling 27.
            self._record_degraded_write(
                "resolve", type(exc).__name__,
                reason=(
                    "spine_unverified"
                    if isinstance(exc, _memory().memory_spine.SpineError)
                    else "store_locked"
                ),
            )
            return False

    def _resolve_prediction_locked(
        self,
        normalized_id: int,
        *,
        actual_status: str,
        actual_steps: int | None,
        evidence_ok: bool | None,
        failure_class: str | None,
        safe_tool: str | None,
    ) -> bool:
        with self._immediate_transaction():
            stamp = _memory().now_iso()
            try:
                trial_boundary = self.db.execute(
                    """SELECT provider_dispatched_at
                       FROM strategy_transfer_trial_assignments
                       WHERE prediction_id=?""",
                    (normalized_id,),
                ).fetchone()
            except _memory().sqlite3.DatabaseError:
                trial_boundary = None
            if trial_boundary is not None and trial_boundary["provider_dispatched_at"]:
                dispatch_stamp = self._canonical_utc_timestamp(
                    trial_boundary["provider_dispatched_at"]
                )
                resolved_stamp = self._canonical_utc_timestamp(stamp)
                if (
                    dispatch_stamp is not None
                    and resolved_stamp is not None
                    and resolved_stamp == dispatch_stamp
                ):
                    stamp = (
                        _memory().datetime.fromisoformat(dispatch_stamp)
                        + _memory().timedelta(microseconds=1)
                    ).isoformat()
            updated = self.db.execute(
                """UPDATE task_predictions
                   SET resolved_at=?, actual_status=?, actual_steps=?, evidence_ok=?,
                       failure_class=?
                   WHERE id=? AND resolved_at IS NULL""",
                (
                    stamp, actual_status, actual_steps,
                    None if evidence_ok is None else int(evidence_ok),
                    failure_class, normalized_id,
                ),
            )
            if updated.rowcount == 1:
                self.db.execute(
                    """UPDATE lesson_applications
                       SET resolved_at=?, successful=?,
                           tool_name=COALESCE(?, tool_name)
                       WHERE prediction_id=? AND resolved_at IS NULL""",
                    (
                        stamp, int(actual_status == "complete"), safe_tool,
                        normalized_id,
                    ),
                )
                memory_ids = [
                    int(row["memory_id"])
                    for row in self.db.execute(
                        """SELECT memory_id FROM memory_retrievals
                           WHERE prediction_id=? AND resolved_at IS NULL""",
                        (normalized_id,),
                    ).fetchall()
                ]
                self.db.execute(
                    """UPDATE memory_retrievals
                       SET resolved_at=?, successful=?
                       WHERE prediction_id=? AND resolved_at IS NULL""",
                    (stamp, int(actual_status == "complete"), normalized_id),
                )
                strategy_rows = self.db.execute(
                    """SELECT id, created_at, prediction_id, memory_id, project_id,
                              strategy, source_family, target_family, mode, rank,
                              applied,
                              source_observation_sha256,
                              source_provenance_sha256, source_control_sha256
                       FROM strategy_transfer_applications
                       WHERE prediction_id=? AND resolved_at IS NULL
                       ORDER BY id""",
                    (normalized_id,),
                ).fetchall()
                for strategy_row in strategy_rows:
                    successful = int(
                        actual_status == "complete" and evidence_ok is True
                    )
                    material = self._strategy_transfer_application_material(
                        created_at=str(strategy_row["created_at"]),
                        prediction_id=int(strategy_row["prediction_id"]),
                        memory_id=int(strategy_row["memory_id"]),
                        project_id=int(strategy_row["project_id"]),
                        strategy=str(strategy_row["strategy"]),
                        source_family=str(strategy_row["source_family"]),
                        target_family=str(strategy_row["target_family"]),
                        mode=str(strategy_row["mode"]),
                        applied=bool(int(strategy_row["applied"])),
                        rank=int(strategy_row["rank"]),
                        source_observation_sha256=str(
                            strategy_row["source_observation_sha256"]
                        ),
                        source_provenance_sha256=str(
                            strategy_row["source_provenance_sha256"]
                        ),
                        source_control_sha256=str(
                            strategy_row["source_control_sha256"]
                        ),
                        resolved_at=stamp,
                        successful=successful,
                    )
                    self.db.execute(
                        """UPDATE strategy_transfer_applications
                           SET resolved_at=?, successful=?, application_sha256=?
                           WHERE id=? AND resolved_at IS NULL""",
                        (
                            stamp,
                            successful,
                            self._strategy_transfer_application_digest(material),
                            int(strategy_row["id"]),
                        ),
                    )
                self._resolve_strategy_transfer_trial_assignment(
                    normalized_id,
                    stamp=stamp,
                    actual_status=actual_status,
                    evidence_ok=evidence_ok,
                )
                for memory_id in memory_ids:
                    aggregate = self.db.execute(
                        """SELECT COUNT(*) AS resolved,
                                  COALESCE(SUM(successful), 0) AS successes
                           FROM memory_retrievals
                           WHERE memory_id=? AND resolved_at IS NOT NULL""",
                        (memory_id,),
                    ).fetchone()
                    resolved = int(aggregate["resolved"])
                    successes = int(aggregate["successes"])
                    failures = resolved - successes
                    # Beta(2, 2) prior prevents one lucky or unlucky run from
                    # dominating recall. Evidence strengthens automatically.
                    utility = (successes + 2.0) / (resolved + 4.0)
                    self.db.execute(
                        """UPDATE memory_statistics
                           SET resolved=?, successes=?, failures=?, utility=?,
                               last_resolved_at=?, updated_at=?
                           WHERE memory_id=?""",
                        (
                            resolved, successes, failures, utility,
                            stamp, stamp, memory_id,
                        ),
                    )
        return updated.rowcount == 1

    def _record_degraded_write(
        self, action: str, detail: str, *, reason: str = "store_locked"
    ) -> None:
        """Note that a turn-path write lost the race for the write lock.

        Design 3.4 (iii), S-3.  ``Memory.record_lesson_applications`` and
        ``Memory.resolve_prediction`` are the two writers a turn takes that
        can collide with the consolidation worker, and the second pass
        measured what a collision costs: a 1 s worker hold delays the turn's
        write by about 1,080 ms and a hold past the busy timeout raises
        ``OperationalError: database is locked``.  Failing the operator's turn
        over that is the M1 round-2 M-1 finding one layer up, so both writers
        degrade -- and the degradation is **recorded**, not swallowed, because
        a prediction that silently failed to resolve would quietly leave the
        gate population that the calibration ledger exists to keep honest.

        ``reason`` is the closed code (``spine_unverified`` when the chain
        refused the append, ``store_locked`` when the worker held the write
        lock); ``detail`` stays free text for a human.

        Recorded twice, on purpose.  The durable receipt is an
        ``activity_log`` row, but writing it needs the very lock the write
        just failed to get, so it is queued and flushed on the next attempt
        that succeeds.  The in-memory record is what the caller can read on
        the turn it happened, through ``degraded_writes()``, and it is also
        what distinguishes "the store was locked" from ``resolve_prediction``'s
        other ``False``, "this prediction was already resolved".

        Never raises: a receipt that cannot be written must not become the
        second failure of the same turn.
        """
        stamp = _memory().now_iso()
        record = {
            "at": stamp,
            "action": str(action),
            # A closed code a caller can branch on.  The sealed holdout read
            # ``row.get("reason")`` on one of these and got ``None``, because
            # the record carried only a free-text ``detail`` -- so a scorer
            # (and an operator surface) could not tell a spine refusal from a
            # lock timeout without parsing prose.
            "reason": str(reason),
            "detail": str(detail)[:200],
        }
        self._degraded_writes.append(record)
        # Bounded: a store that is locked out for a long time must not grow a
        # list until it is the problem.
        if len(self._degraded_writes) > 256:
            del self._degraded_writes[:-256]
        self._pending_degraded_receipts.append(
            (stamp, str(action), str(detail)[:200])
        )
        if len(self._pending_degraded_receipts) > 256:
            del self._pending_degraded_receipts[:-256]
        self._flush_degraded_receipts()

    def _flush_degraded_receipts(self) -> None:
        """Write any queued degradation receipts, or leave them queued."""
        if not self._pending_degraded_receipts:
            return
        queued = list(self._pending_degraded_receipts)
        try:
            for stamp, action, detail in queued:
                self.db.execute(
                    """INSERT INTO activity_log(
                           created_at, category, action, status, details_json
                       ) VALUES (?, 'ladder', ?, 'degraded', ?)""",
                    (
                        stamp, action,
                        _memory().memory_spine.canonical({"detail": detail}),
                    ),
                )
        except _memory().sqlite3.DatabaseError:
            # Still locked out.  The queue keeps them for the next attempt and
            # ``degraded_writes()`` already knows.
            return
        del self._pending_degraded_receipts[:len(queued)]

    def degraded_writes(self) -> list[dict[str, Any]]:
        """Turn-path writes this store degraded rather than failing on.

        Read it to tell a locked-out ``resolve_prediction`` (which returns
        ``False`` having written nothing) from an ordinary one (which returns
        ``False`` because the prediction was already resolved).  The list is
        per-store, bounded, and never reaches the model.
        """
        self._flush_degraded_receipts()
        return [dict(record) for record in self._degraded_writes]

    def calibration_gate(
        self,
        family: str,
        *,
        minimum_attempts: int = 20,
        maximum_brier: float = 0.25,
        maximum_calibration_error: float = 0.15,
        minimum_success_rate: float = 0.70,
        minimum_evidence_rate: float = 0.70,
    ) -> dict[str, Any]:
        """Fail closed unless one family's outcome predictions are trustworthy."""
        if family not in self.PREDICTION_FAMILIES:
            raise ValueError(f"Unknown task family: {family}")
        rows = self.competence(family)
        row = rows[0] if rows else None
        attempts = int(row["attempts"]) if row else 0
        brier = float(row["brier"]) if row and row["brier"] is not None else None
        predicted = (
            float(row["mean_predicted"])
            if row and row["mean_predicted"] is not None else None
        )
        observed = (
            float(row["success_rate"])
            if row and row["success_rate"] is not None else None
        )
        evidence_applicable = int(row["evidence_applicable"]) if row else 0
        evidence_rate = (
            float(row["evidence_rate"])
            if row and row["evidence_rate"] is not None else None
        )
        calibration_error = (
            float(abs(_memory().Decimal(str(predicted)) - _memory().Decimal(str(observed))))
            if predicted is not None and observed is not None else None
        )
        reasons: list[str] = []
        if attempts < int(minimum_attempts):
            reasons.append(f"requires {int(minimum_attempts)} outcomes; has {attempts}")
        if brier is None or brier > float(maximum_brier):
            reasons.append(
                f"Brier score must be <= {float(maximum_brier):.2f}; "
                + ("unknown" if brier is None else f"is {brier:.3f}")
            )
        if calibration_error is None or calibration_error > float(maximum_calibration_error):
            reasons.append(
                f"calibration error must be <= {float(maximum_calibration_error):.2f}; "
                + (
                    "unknown"
                    if calibration_error is None
                    else "is " + (
                        f"{calibration_error:.4f}"
                        if float(f"{calibration_error:.4f}") > float(maximum_calibration_error)
                        else repr(calibration_error)
                    )
                )
            )
        if observed is None or observed < float(minimum_success_rate):
            reasons.append(
                f"observed success must be >= {float(minimum_success_rate):.2f}; "
                + ("unknown" if observed is None else f"is {observed:.3f}")
            )
        if evidence_applicable and (
            evidence_rate is None or evidence_rate < float(minimum_evidence_rate)
        ):
            reasons.append(
                f"verification evidence rate must be >= {float(minimum_evidence_rate):.2f}; "
                + ("unknown" if evidence_rate is None else f"is {evidence_rate:.3f}")
            )
        return {
            "family": family,
            "allowed": not reasons,
            "attempts": attempts,
            "brier": brier,
            "mean_predicted": predicted,
            "observed_success": observed,
            "evidence_applicable": evidence_applicable,
            "evidence_rate": evidence_rate,
            "calibration_error": calibration_error,
            "requirements": {
                "minimum_attempts": int(minimum_attempts),
                "maximum_brier": float(maximum_brier),
                "maximum_calibration_error": float(maximum_calibration_error),
                "minimum_success_rate": float(minimum_success_rate),
                "minimum_evidence_rate": float(minimum_evidence_rate),
            },
            "reasons": reasons,
        }

    def competence(self, family: str | None = None) -> list[dict[str, Any]]:
        """Return resolved non-practice outcomes grouped by the canonical task family."""
        if family is not None and family not in self.PREDICTION_FAMILIES:
            raise ValueError(f"Unknown task family: {family}")
        clause = "AND family=?" if family is not None else ""
        params: tuple[Any, ...] = (family,) if family is not None else ()
        rows = self.db.execute(
            f"""SELECT family,
                       COUNT(*) AS attempts,
                       AVG(predicted_success) AS mean_predicted,
                       AVG(CASE WHEN actual_status='complete' THEN 1.0 ELSE 0.0 END)
                           AS success_rate,
                       AVG((predicted_success -
                           CASE WHEN actual_status='complete' THEN 1.0 ELSE 0.0 END) *
                           (predicted_success -
                           CASE WHEN actual_status='complete' THEN 1.0 ELSE 0.0 END)) AS brier,
                       AVG(actual_steps) AS mean_steps,
                       MAX(actual_steps) AS max_steps,
                       SUM(CASE WHEN evidence_ok IS NOT NULL THEN 1 ELSE 0 END)
                           AS evidence_applicable,
                       AVG(CASE WHEN evidence_ok IS NOT NULL THEN evidence_ok END)
                           AS evidence_rate
                FROM task_predictions
                WHERE resolved_at IS NOT NULL
                  AND origin IN ('interactive','worker','proactive') {clause}
                GROUP BY family ORDER BY family""",
            params,
        ).fetchall()
        return [dict(row) for row in rows]

    def calibration(self, bins: int = 10) -> list[dict[str, Any]]:
        """Compare prediction bands with observed completion rates."""
        if isinstance(bins, bool) or not isinstance(bins, int):
            raise ValueError("bins must be an integer")
        bins = max(2, min(bins, 20))
        rows = self.db.execute(
            """SELECT MIN(? - 1, CAST(predicted_success * ? AS INTEGER)) AS bucket,
                      COUNT(*) AS n,
                      AVG(predicted_success) AS mean_predicted,
                      AVG(CASE WHEN actual_status='complete' THEN 1.0 ELSE 0.0 END)
                          AS observed
               FROM task_predictions
               WHERE resolved_at IS NOT NULL
                 AND origin IN ('interactive','worker','proactive')
               GROUP BY bucket ORDER BY bucket""",
            (bins, bins),
        ).fetchall()
        return [dict(row) for row in rows]

    def failure_histogram(
        self,
        family: str | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        if family is not None and family not in self.PREDICTION_FAMILIES:
            raise ValueError(f"Unknown task family: {family}")
        clause = "AND family=?" if family is not None else ""
        params: tuple[Any, ...] = (family,) if family is not None else ()
        rows = self.db.execute(
            f"""SELECT failure_class, COUNT(*) AS n
                FROM task_predictions
                WHERE resolved_at IS NOT NULL AND failure_class IS NOT NULL
                  AND origin IN ('interactive','worker','proactive') {clause}
                GROUP BY failure_class ORDER BY n DESC, failure_class LIMIT ?""",
            (*params, _memory()._bounded_limit(limit, 100)),
        ).fetchall()
        return [dict(row) for row in rows]

    def drift_report(
        self,
        *,
        window: int = 30,
        baseline: int = 90,
        minimum_samples: int = 10,
    ) -> list[dict[str, Any]]:
        """Compare recent outcomes with an earlier per-family baseline."""
        for value, label, maximum in (
            (window, "window", 1_000),
            (baseline, "baseline", 10_000),
            (minimum_samples, "minimum_samples", 1_000),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
                raise ValueError(f"{label} must be an integer from 1 to {maximum}")

        rows = self.db.execute(
            """SELECT id, family, predicted_success, actual_status, actual_steps,
                      evidence_ok, failure_class
               FROM task_predictions
               WHERE resolved_at IS NOT NULL
                 AND origin IN ('interactive','worker','proactive')
               ORDER BY family, resolved_at DESC, id DESC"""
        ).fetchall()
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            grouped.setdefault(str(row["family"]), []).append(dict(row))

        def metrics(items: list[dict[str, Any]]) -> dict[str, Any]:
            evidence = [item for item in items if item["evidence_ok"] is not None]
            steps = [int(item["actual_steps"]) for item in items if item["actual_steps"] is not None]
            failures = _memory().Counter(
                str(item["failure_class"])
                for item in items
                if item["failure_class"] is not None
            )
            return {
                "n": len(items),
                "success_rate": sum(item["actual_status"] == "complete" for item in items) / len(items),
                "evidence_n": len(evidence),
                "evidence_rate": (
                    sum(bool(item["evidence_ok"]) for item in evidence) / len(evidence)
                    if evidence else None
                ),
                "brier": sum(
                    (
                        float(item["predicted_success"])
                        - (1.0 if item["actual_status"] == "complete" else 0.0)
                    ) ** 2
                    for item in items
                ) / len(items),
                "mean_steps": sum(steps) / len(steps) if steps else None,
                "failure_classes": dict(sorted(failures.items())),
            }

        findings: list[dict[str, Any]] = []
        for family in sorted(grouped):
            items = grouped[family]
            recent_items = items[:window]
            baseline_items = items[window:window + baseline]
            if len(recent_items) < minimum_samples or len(baseline_items) < minimum_samples:
                continue
            recent = metrics(recent_items)
            prior = metrics(baseline_items)
            signals: list[dict[str, Any]] = []
            if recent["success_rate"] < prior["success_rate"] - 0.15:
                signals.append({
                    "signal": "success_rate_drop",
                    "recent": recent["success_rate"],
                    "baseline": prior["success_rate"],
                    "threshold": 0.15,
                })
            if (
                recent["evidence_n"] >= minimum_samples
                and prior["evidence_n"] >= minimum_samples
                and recent["evidence_rate"] is not None
                and prior["evidence_rate"] is not None
                and recent["evidence_rate"] < prior["evidence_rate"] - 0.15
            ):
                signals.append({
                    "signal": "evidence_rate_drop",
                    "recent": recent["evidence_rate"],
                    "baseline": prior["evidence_rate"],
                    "threshold": 0.15,
                })
            if recent["brier"] > prior["brier"] + 0.10:
                signals.append({
                    "signal": "brier_increase",
                    "recent": recent["brier"],
                    "baseline": prior["brier"],
                    "threshold": 0.10,
                })
            for failure_class, count in recent["failure_classes"].items():
                if count >= 3 and failure_class not in prior["failure_classes"]:
                    signals.append({
                        "signal": "new_failure_class",
                        "failure_class": failure_class,
                        "recent_count": count,
                        "baseline_count": 0,
                        "threshold": 3,
                    })
            if (
                recent["mean_steps"] is not None
                and prior["mean_steps"] is not None
                and recent["mean_steps"] > prior["mean_steps"] * 1.5
            ):
                signals.append({
                    "signal": "mean_steps_increase",
                    "recent": recent["mean_steps"],
                    "baseline": prior["mean_steps"],
                    "threshold_ratio": 1.5,
                })
            if signals:
                findings.append({
                    "family": family,
                    "recent": recent,
                    "baseline": prior,
                    "signals": signals,
                })
        return findings

    def open_prediction_count(self) -> int:
        self._ensure_open()
        return int(
            self.db.execute(
                "SELECT COUNT(*) FROM task_predictions WHERE resolved_at IS NULL"
            ).fetchone()[0]
        )

    def health_indicators(self, *, approval_ttl_hours: int = 24) -> dict[str, int]:
        """Return bounded integrity counters used by deep local diagnostics."""
        if (
            isinstance(approval_ttl_hours, bool)
            or not isinstance(approval_ttl_hours, int)
            or not 1 <= approval_ttl_hours <= 720
        ):
            raise ValueError("approval_ttl_hours must be an integer from 1 to 720")
        cutoff = (_memory().datetime.now(_memory().timezone.utc) - _memory().timedelta(hours=approval_ttl_hours)).isoformat()
        stale_awaiting = int(self.db.execute(
            """SELECT COUNT(*) FROM tasks
               WHERE status='awaiting_approval' AND updated_at<?""",
            (cutoff,),
        ).fetchone()[0])
        return {
            "open_predictions": self.open_prediction_count(),
            "stale_awaiting_approval_tasks": stale_awaiting,
        }

    def prediction_origin_for_task(self, task_id: int) -> str:
        """Distinguish operator-queued work from scheduler-created backlog work."""
        normalized_id = self._prediction_optional_id(task_id, "task_id")
        row = self.db.execute(
            "SELECT backlog_id, initiative_event_id FROM tasks WHERE id=?",
            (normalized_id,),
        ).fetchone()
        return (
            "proactive"
            if row is not None
            and (
                row["backlog_id"] is not None
                or row["initiative_event_id"] is not None
            )
            else "worker"
        )

    def _prediction_family_for_context(
        self,
        task_id: int | None,
        conversation_id: int | None,
    ) -> str | None:
        clauses: list[str] = []
        parameters: list[int] = []
        if task_id is not None:
            clauses.append("task_id=?")
            parameters.append(int(task_id))
        if conversation_id is not None:
            clauses.append("conversation_id=?")
            parameters.append(int(conversation_id))
        if not clauses:
            return None
        row = self.db.execute(
            "SELECT family FROM task_predictions WHERE resolved_at IS NOT NULL "
            "AND origin NOT IN ('companion_action','companion_suggestion') AND ("
            + " OR ".join(clauses)
            + ") ORDER BY id DESC LIMIT 1",
            parameters,
        ).fetchone()
        family = str(row["family"]) if row is not None else None
        return family if family in self.PREDICTION_FAMILIES else None
