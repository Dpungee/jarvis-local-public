"""Current Memory domain extracted without changing method control flow.

Global dependencies resolve through jarvis.memory at call time. The facade keeps
public imports, patch seams, constants and authorization helpers authoritative.
"""
from __future__ import annotations

from collections.abc import Mapping
from collections.abc import Sequence
from pathlib import Path
from typing import Any
import sqlite3
from .memory_runtime import (_memory)


class LearningLadderMemoryMixin:
    """Mechanically extracted current Memory methods."""

    def _ladder_available(self) -> bool:
        return bool(self._ladder_ready and self._spine_ready)

    def _require_ladder(self) -> None:
        self._ensure_open()
        if not self._ladder_available():
            raise RuntimeError(
                "the learning ladder is unavailable on this store "
                "(schema 49 objects or the memory spine are missing)"
            )

    def _ladder_eligible_outcomes(self, family: str) -> list[sqlite3.Row]:
        """The gate population for one family, in id order.

        Exactly ``competence()``'s restriction — resolved, and an origin in
        ``LADDER_LEDGER_ORIGINS`` — so a sealed epoch and the calibrated gate
        always describe the same rows (design 2.2).
        """
        placeholders = ", ".join("?" for _ in _memory().LADDER_LEDGER_ORIGINS)
        return self.db.execute(
            f"""SELECT id, created_at, predicted_success, actual_status, evidence_ok
                FROM task_predictions
                WHERE resolved_at IS NOT NULL AND family=?
                  AND origin IN ({placeholders})
                ORDER BY id""",
            (str(family), *_memory().LADDER_LEDGER_ORIGINS),
        ).fetchall()

    def _ladder_covered_ids(self, family: str) -> set[int]:
        """Every prediction id any sealed epoch of this family covers.

        Read from ``covered_ids_json`` and never inferred from the
        ``[first_prediction_id, last_prediction_id]`` range: a prediction held
        open while the block around it is cut sits *inside* that range, so a
        range test would leave it permanently uncoverable while
        ``competence()`` kept counting it — the S-2 defect, one level down
        (design 10.7 item 8).
        """
        covered: set[int] = set()
        for row in self.db.execute(
            """SELECT covered_ids_json FROM memory_calibration_ledger
               WHERE family=? ORDER BY epoch""",
            (str(family),),
        ):
            covered.update(_memory()._ladder_covered_id_list(row["covered_ids_json"]))
        return covered

    def _ladder_unsealed_tail(self, family: str) -> list[sqlite3.Row]:
        covered = self._ladder_covered_ids(family)
        return [
            row for row in self._ladder_eligible_outcomes(family)
            if int(row["id"]) not in covered
        ]

    def _ladder_next_epoch(self, family: str) -> int:
        row = self.db.execute(
            "SELECT COALESCE(MAX(epoch), 0) FROM memory_calibration_ledger WHERE family=?",
            (str(family),),
        ).fetchone()
        return int(row[0] or 0) + 1

    def _ladder_live_promotion_windows(self, family: str) -> list[tuple[str, str | None]]:
        """``(approved_at, ended_at)`` for every artefact this family has had
        live, so an outcome can be labelled applied or unapplied.

        A terminal row keeps ``approved_at`` (S-6), and the table records no
        "stopped being live" instant, so ``updated_at`` stands in for one on a
        row that has left the live stages.  Stated here rather than left
        implicit, because it is the one approximation in the epoch numbers.
        """
        windows: list[tuple[str, str | None]] = []
        for row in self.db.execute(
            """SELECT stage, approved_at, updated_at FROM ladder_promotions
               WHERE family=? AND approved_at IS NOT NULL""",
            (str(family),),
        ):
            started = str(row["approved_at"])
            if str(row["stage"]) in _memory().LADDER_LIVE_STAGES:
                windows.append((started, None))
            else:
                windows.append((started, str(row["updated_at"])))
        return windows

    def _ladder_applied_ids(
        self, family: str, block: Sequence[sqlite3.Row]
    ) -> set[int]:
        """Covered predictions that had the learning channel behind them:
        either a ``lesson_applications`` row, or a live artefact for the
        family at the moment the prediction was created (design 2.2)."""
        ids = [int(row["id"]) for row in block]
        applied: set[int] = set()
        for start in range(0, len(ids), 400):
            chunk = ids[start:start + 400]
            placeholders = ", ".join("?" for _ in chunk)
            applied.update(
                int(found[0]) for found in self.db.execute(
                    f"""SELECT DISTINCT prediction_id FROM lesson_applications
                        WHERE prediction_id IN ({placeholders})""",
                    chunk,
                )
            )
        windows = self._ladder_live_promotion_windows(family)
        if windows:
            for row in block:
                if int(row["id"]) in applied:
                    continue
                created = str(row["created_at"])
                if any(
                    started <= created and (ended is None or created < ended)
                    for started, ended in windows
                ):
                    applied.add(int(row["id"]))
        return applied

    def _ladder_epoch_receipts(
        self, family: str, since: str | None, until: str
    ) -> dict[str, int]:
        """The ladder's own counters for one epoch window.

        Two of the four are read from the spine, which is where design 2.2
        puts them: ``withdrawals`` counts ``ladder.withdrawn`` events for this
        family in ``(since, until]``, and ``screened_components`` counts the
        subset whose reason is ``screened_component``.

        The other two cannot come from the spine, and saying so is the point:
        **a refused staging or approval appends no spine event at all** — a
        refusal returns a dict and changes nothing, which is exactly why it
        leaves no receipt on an append-only chain.  They are read instead from
        ``activity_log`` category ``ladder``, which design 3.4 already names
        as the refusal receipt path, and ``docs/LEARNING_LADDER.md`` records
        that these two counters have a different provenance from the twenty
        beside them.
        """
        counts = {
            "refused_stagings": 0,
            "refused_approvals": 0,
            "withdrawals": 0,
            "screened_components": 0,
        }
        window = "created_at <= ?" if since is None else "created_at > ? AND created_at <= ?"
        params: tuple[Any, ...] = (until,) if since is None else (since, until)
        for row in self.db.execute(
            f"""SELECT payload_json FROM memory_spine_events
                WHERE kind='ladder.withdrawn' AND {window}""",
            params,
        ):
            payload = _memory()._ladder_payload(row["payload_json"])
            if str(payload.get("family") or "") != str(family):
                continue
            counts["withdrawals"] += 1
            if str(payload.get("reason") or "") == "screened_component":
                counts["screened_components"] += 1
        try:
            for row in self.db.execute(
                f"""SELECT action, details_json FROM activity_log
                    WHERE category='ladder' AND status='refused' AND {window}""",
                params,
            ):
                details = _memory()._ladder_payload(row["details_json"])
                if str(details.get("family") or "") != str(family):
                    continue
                if str(row["action"]) == "stage":
                    counts["refused_stagings"] += 1
                elif str(row["action"]) == "approve":
                    counts["refused_approvals"] += 1
                if str(details.get("reason") or "") == "screened_component":
                    counts["screened_components"] += 1
        except _memory().sqlite3.DatabaseError:
            # An activity log that cannot be read is a reporting problem, not
            # a reason to refuse a seal: the counters stay at zero and the
            # epoch's calibration numbers, which are what the gate reads, are
            # unaffected.
            pass
        return counts

    def _log_ladder_refusal(
        self, action: str, *, family: str, project_id: int | None, reason: str
    ) -> None:
        """Record one refusal on the receipt path (design 3.4).

        Never the confirmation code, never a document digest, never operator
        prose: only the action, the family, the project and a closed reason
        code, so the epoch counters have a durable source that an operator can
        also read directly.
        """
        try:
            self.db.execute(
                """INSERT INTO activity_log(
                       created_at, category, action, status, details_json
                   ) VALUES (?, 'ladder', ?, 'refused', ?)""",
                (
                    _memory().now_iso(),
                    str(action),
                    _memory().memory_spine.canonical({
                        "family": str(family),
                        "project_id": None if project_id is None else int(project_id),
                        "reason": str(reason),
                    }),
                ),
            )
        except _memory().sqlite3.DatabaseError:
            # A refusal is already a no-op; failing to log it must not turn it
            # into an exception on the worker's pass.
            pass

    def _ladder_epoch_metrics(
        self, family: str, epoch: int, block: Sequence[sqlite3.Row]
    ) -> dict[str, Any]:
        """Every number an epoch freezes, computed from the covered rows alone
        and **outside** the write transaction (L-5)."""
        n = len(block)
        successes = sum(
            1 for row in block if str(row["actual_status"]) == "complete"
        )
        predicted = [float(row["predicted_success"]) for row in block]
        mean_predicted = sum(predicted) / n
        brier = sum(
            (float(row["predicted_success"])
             - (1.0 if str(row["actual_status"]) == "complete" else 0.0)) ** 2
            for row in block
        ) / n
        evidence_rows = [row for row in block if row["evidence_ok"] is not None]
        applied = self._ladder_applied_ids(family, block)
        applied_rows = [row for row in block if int(row["id"]) in applied]
        unapplied_rows = [row for row in block if int(row["id"]) not in applied]
        covered_ids = sorted(int(row["id"]) for row in block)
        return {
            "family": str(family),
            "epoch": int(epoch),
            "n": n,
            "successes": successes,
            "mean_predicted": mean_predicted,
            "brier": brier,
            "calibration_error": abs(mean_predicted - successes / n),
            "evidence_applicable": len(evidence_rows),
            "evidence_successes": sum(
                1 for row in evidence_rows if int(row["evidence_ok"]) == 1
            ),
            "applied_n": len(applied_rows),
            "applied_successes": sum(
                1 for row in applied_rows
                if str(row["actual_status"]) == "complete"
            ),
            "unapplied_n": len(unapplied_rows),
            "unapplied_successes": sum(
                1 for row in unapplied_rows
                if str(row["actual_status"]) == "complete"
            ),
            "first_prediction_id": covered_ids[0],
            "last_prediction_id": covered_ids[-1],
            "covered_ids": covered_ids,
            "coverage_digest": _memory().ladder_coverage_digest(
                self._spine_key,
                [
                    (
                        int(row["id"]), float(row["predicted_success"]),
                        str(row["actual_status"]), row["evidence_ok"],
                    )
                    for row in block
                ],
            ),
        }

    def seal_calibration_epoch(
        self,
        family: str,
        *,
        workspace: Path | None = None,
        actor: str = "runtime",
        conversation_id: int | None = None,
        permission: str = "runtime",
        maximum_epochs: int = 64,
    ) -> list[dict[str, Any]]:
        """Seal every whole unsealed block of ``LADDER_EPOCH_SIZE`` outcomes.

        Never seals a partial block, never takes a boundary from the caller,
        and never re-cuts a sealed one, so ``ladder seal --all`` and a worker
        that seals after every single outcome produce byte-identical ledgers
        for the same store (design 2.2, exit test 7.1 part 2).  Returns the
        rows it sealed, possibly empty.

        The whole coverage scan and every derived number are computed
        **outside** the write transaction; the transaction re-reads the
        family's newest epoch and the exact covered id set and aborts if
        either moved, so a bulk catch-up takes one short lock per epoch rather
        than one long one for the whole scan (L-5).  ``maximum_epochs`` bounds
        one call; the worker simply calls again.

        ``workspace`` is what makes ``unverified_at_seal`` the full design 3.7
        count.  Without one the filesystem cannot be consulted, so the count
        covers only the reasons the store can derive on its own and the
        difference is recorded on the row rather than hidden: pass the
        project's workspace from the worker and the CLI, which both have one.
        """
        self._require_ladder()
        if family not in self.PREDICTION_FAMILIES:
            raise ValueError(f"Unknown task family: {family}")
        if actor not in _memory().memory_spine.SPINE_ACTORS:
            raise ValueError(f"Unknown spine actor: {actor}")
        size = int(_memory().learning_ladder.LADDER_EPOCH_SIZE)
        bound = max(0, min(int(maximum_epochs), _memory()._LADDER_SEAL_MAX_EPOCHS))
        sealed: list[dict[str, Any]] = []
        for _ in range(bound):
            tail = self._ladder_unsealed_tail(family)
            if len(tail) < size:
                break
            block = tail[:size]
            epoch = self._ladder_next_epoch(family)
            metrics = self._ladder_epoch_metrics(family, epoch, block)
            row = self._ladder_commit_epoch(
                metrics,
                workspace=workspace,
                actor=actor,
                conversation_id=conversation_id,
                permission=permission,
            )
            if row is None:
                # The block moved under us; the caller retries on its next
                # pass rather than sealing something it did not measure.
                break
            sealed.append(row)
        return sealed

    def _ladder_commit_epoch(
        self,
        metrics: Mapping[str, Any],
        *,
        workspace: Path | None,
        actor: str,
        conversation_id: int | None,
        permission: str,
    ) -> dict[str, Any] | None:
        family = str(metrics["family"])
        epoch = int(metrics["epoch"])
        covered = [int(value) for value in metrics["covered_ids"]]
        stamp = _memory().now_iso()
        previous = self.db.execute(
            """SELECT created_at FROM memory_calibration_ledger
               WHERE family=? ORDER BY epoch DESC LIMIT 1""",
            (family,),
        ).fetchone()
        receipts = self._ladder_epoch_receipts(
            family, None if previous is None else str(previous["created_at"]), stamp
        )
        unverified = len(self._ladder_unverified_for_seal(workspace))
        with self._immediate_transaction():
            if self._ladder_next_epoch(family) != epoch:
                return None
            tail = self._ladder_unsealed_tail(family)
            if [int(row["id"]) for row in tail[:len(covered)]] != covered:
                return None
            ladder_id = _memory().allocate_ladder_id(self.db)
            event_id = _memory().memory_spine.append_event(
                self.db,
                self._spine_key,
                kind=_memory().LADDER_LEDGER_CREATING_KIND,
                actor=actor,
                source="calibration ledger",
                scope="global",
                permission=permission,
                outcome="applied",
                payload={
                    "at": stamp,
                    "family": family,
                    "epoch": epoch,
                    "n": int(metrics["n"]),
                    "successes": int(metrics["successes"]),
                    "mean_predicted": float(metrics["mean_predicted"]),
                    "brier": float(metrics["brier"]),
                    "calibration_error": float(metrics["calibration_error"]),
                    "evidence_applicable": int(metrics["evidence_applicable"]),
                    "evidence_successes": int(metrics["evidence_successes"]),
                    "applied_n": int(metrics["applied_n"]),
                    "applied_successes": int(metrics["applied_successes"]),
                    "unapplied_n": int(metrics["unapplied_n"]),
                    "unapplied_successes": int(metrics["unapplied_successes"]),
                    "refused_stagings": int(receipts["refused_stagings"]),
                    "refused_approvals": int(receipts["refused_approvals"]),
                    "withdrawals": int(receipts["withdrawals"]),
                    "screened_components": int(receipts["screened_components"]),
                    "unverified_at_seal": int(unverified),
                    "first_prediction_id": int(metrics["first_prediction_id"]),
                    "last_prediction_id": int(metrics["last_prediction_id"]),
                    "coverage_digest": str(metrics["coverage_digest"]),
                },
                now=stamp,
                conversation_id=conversation_id,
                subject_kind="calibration",
                subject_id=ladder_id,
            )
            self.db.execute(
                """INSERT INTO memory_calibration_ledger(
                       id, created_at, family, epoch, n, successes,
                       mean_predicted, brier, calibration_error,
                       evidence_applicable, evidence_successes,
                       applied_n, applied_successes,
                       unapplied_n, unapplied_successes,
                       refused_stagings, refused_approvals, withdrawals,
                       screened_components, unverified_at_seal,
                       first_prediction_id, last_prediction_id,
                       covered_ids_json, coverage_digest, spine_event_id
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                             ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    ladder_id, stamp, family, epoch,
                    int(metrics["n"]), int(metrics["successes"]),
                    float(metrics["mean_predicted"]), float(metrics["brier"]),
                    float(metrics["calibration_error"]),
                    int(metrics["evidence_applicable"]),
                    int(metrics["evidence_successes"]),
                    int(metrics["applied_n"]), int(metrics["applied_successes"]),
                    int(metrics["unapplied_n"]), int(metrics["unapplied_successes"]),
                    int(receipts["refused_stagings"]),
                    int(receipts["refused_approvals"]),
                    int(receipts["withdrawals"]),
                    int(receipts["screened_components"]),
                    int(unverified),
                    int(metrics["first_prediction_id"]),
                    int(metrics["last_prediction_id"]),
                    _memory().memory_spine.canonical(covered),
                    str(metrics["coverage_digest"]),
                    int(event_id),
                ),
            )
        return self.calibration_epoch(ladder_id)

    def _ladder_unverified_for_seal(self, workspace: Path | None) -> list[dict[str, Any]]:
        """``unverified_at_seal``'s population, degraded honestly.

        With a workspace this is design 3.7 exactly.  Without one the
        filesystem reasons (``digest_mismatch``, ``orphan_document``) cannot
        be evaluated, so the count is the store-derivable subset; it can only
        ever be an under-count, and clause (4) of design 2.3 treats any
        non-zero value as a regression, so a degraded count never turns a
        regression into a pass.
        """
        try:
            return self.ladder_unverified_promotions(workspace=workspace)
        except (OSError, ValueError, _memory().sqlite3.DatabaseError):
            return []

    def calibration_epoch(self, epoch_id: int) -> dict[str, Any] | None:
        row = self.db.execute(
            "SELECT * FROM memory_calibration_ledger WHERE id=?", (int(epoch_id),)
        ).fetchone()
        return None if row is None else _memory()._ladder_epoch_dict(row)

    def calibration_ledger(
        self, family: str | None = None, *, since_epoch: int = 1, limit: int = 500
    ) -> list[dict[str, Any]]:
        """Sealed epochs in order, newest last.  Read-only and derived from
        nothing: every number was frozen when the epoch closed."""
        self._ensure_open()
        if family is not None and family not in self.PREDICTION_FAMILIES:
            raise ValueError(f"Unknown task family: {family}")
        if not self._ladder_ready:
            return []
        clause = "AND family=?" if family is not None else ""
        params: tuple[Any, ...] = (
            (int(since_epoch), family) if family is not None else (int(since_epoch),)
        )
        rows = self.db.execute(
            f"""SELECT * FROM memory_calibration_ledger
                WHERE epoch >= ? {clause}
                ORDER BY family, epoch LIMIT ?""",
            (*params, _memory()._bounded_limit(limit, 5_000)),
        ).fetchall()
        return [_memory()._ladder_epoch_dict(row) for row in rows]

    def calibration_ledger_monotonicity(
        self, family: str, *, since_epoch: int = 1
    ) -> dict[str, Any]:
        """Has this family's sealed ledger regressed?

        ``memory.py`` owns the query and ``learning_ladder`` owns every piece
        of the arithmetic (design 2.3's four clauses, both bands and the
        consecutive-epoch streak), so neither can reimplement the other's
        half.  The wrapper adds only ``family`` and ``coverage_intact``.

        Read ``currently_regressed`` to refuse a staging or an approval, and
        ``monotone`` / ``newest_regressed`` to record or display a verdict:
        the streak is what refuses, the per-epoch predicate is what is
        recorded, and swapping them would either silence a healthy family or
        record a regression that never gated anything.
        """
        self._ensure_open()
        if family not in self.PREDICTION_FAMILIES:
            raise ValueError(f"Unknown task family: {family}")
        epochs = self.calibration_ledger(family, since_epoch=since_epoch)
        verdict = dict(_memory().learning_ladder.monotonicity_verdict(epochs))
        verdict["family"] = str(family)
        verdict["coverage_intact"] = bool(
            self.verify_calibration_ledger(family)["coverage_intact"]
        )
        return verdict

    def verify_calibration_ledger(self, family: str | None = None) -> dict[str, Any]:
        """Lineage both ways, epoch numbering, disjoint coverage, and the
        keyed digest over the exact covered rows.

        What check 4 defends against is the important one and it is not
        exotic: an epoch **re-cut over different rows** — a hand-edited
        boundary, a covered failure flipped to ``complete``, a row inserted
        into a sealed range.  What check 5 reports is narrower and honestly
        labelled: no product path deletes a row from the gate population, so a
        coverage gap means an out-of-band ``DELETE`` or a path nobody has
        written yet.
        """
        self._ensure_open()
        if family is not None and family not in self.PREDICTION_FAMILIES:
            raise ValueError(f"Unknown task family: {family}")
        report: dict[str, Any] = {
            "rows": 0,
            "families": [],
            "chain_ok": True,
            "sequence_ok": True,
            "lineage_ok": True,
            "coverage_gaps": [],
            "coverage_intact": True,
            # The design 6.3 figure, so an operator surface can print
            # "coverage: K of N epochs re-derivable" rather than a bare
            # boolean (R-5).  ``epochs_rederivable`` counts epochs whose
            # covered rows all still exist AND whose keyed digest still
            # matches, which is the only statement worth making.
            "epochs_total": 0,
            "epochs_rederivable": 0,
            "problems": [],
        }
        if not self._ladder_available():
            report["chain_ok"] = False
            report["coverage_intact"] = False
            report["problems"].append({"kind": "ladder_unavailable"})
            return report
        clause = "WHERE family=?" if family is not None else ""
        params: tuple[Any, ...] = (family,) if family is not None else ()
        rows = self.db.execute(
            f"SELECT * FROM memory_calibration_ledger {clause} ORDER BY family, epoch",
            params,
        ).fetchall()
        report["rows"] = len(rows)
        report["epochs_total"] = len(rows)
        report["families"] = sorted({str(row["family"]) for row in rows})
        by_family: dict[str, list[sqlite3.Row]] = {}
        for row in rows:
            by_family.setdefault(str(row["family"]), []).append(row)

        # (1) lineage, both directions.
        for row in rows:
            event = self.db.execute(
                """SELECT kind, subject_kind, subject_id FROM memory_spine_events
                   WHERE id=?""",
                (int(row["spine_event_id"]),),
            ).fetchone()
            if (
                event is None
                or str(event["kind"]) != _memory().LADDER_LEDGER_CREATING_KIND
                or str(event["subject_kind"] or "") != "calibration"
                or int(event["subject_id"] or 0) != int(row["id"])
            ):
                report["lineage_ok"] = False
                report["problems"].append({
                    "kind": "lineage_broken",
                    "epoch_id": int(row["id"]),
                    "family": str(row["family"]),
                    "epoch": int(row["epoch"]),
                })
        for event in self.db.execute(
            """SELECT id, subject_id FROM memory_spine_events
               WHERE kind=? AND subject_kind='calibration'""",
            (_memory().LADDER_LEDGER_CREATING_KIND,),
        ):
            backing = self.db.execute(
                """SELECT 1 FROM memory_calibration_ledger
                   WHERE id=? AND spine_event_id=?""",
                (int(event["subject_id"] or 0), int(event["id"])),
            ).fetchone()
            if backing is None:
                report["lineage_ok"] = False
                report["problems"].append({
                    "kind": "orphan_seal_event", "event_id": int(event["id"]),
                })

        for name, family_rows in by_family.items():
            # (2) epoch numbers are 1..K with no gap and no duplicate.
            numbers = [int(row["epoch"]) for row in family_rows]
            if numbers != list(range(1, len(numbers) + 1)):
                report["sequence_ok"] = False
                report["problems"].append({
                    "kind": "epoch_sequence_broken", "family": name,
                    "epochs": numbers,
                })
            # (3) exactly n covered ids per epoch, pairwise disjoint, with an
            #     increasing reported range.
            # Ranges are deliberately NOT required to increase.  When a
            # prediction resolves late its id is lower than the range of the
            # epoch that was cut around it, so the epoch that finally covers
            # it legitimately starts inside an earlier epoch's span -- that is
            # the whole reason the covered ids are stored, and a range-order
            # check here would report the fix as the fault (design 10.7 item
            # 8).  Disjointness of the id sets is the real invariant and is
            # checked below.
            seen: set[int] = set()
            for row in family_rows:
                covered = _memory()._ladder_covered_id_list(row["covered_ids_json"])
                if len(covered) != int(row["n"]) or sorted(set(covered)) != covered:
                    report["coverage_intact"] = False
                    report["problems"].append({
                        "kind": "coverage_shape_invalid", "family": name,
                        "epoch": int(row["epoch"]),
                    })
                    continue
                overlap = seen.intersection(covered)
                if overlap:
                    report["coverage_intact"] = False
                    report["problems"].append({
                        "kind": "coverage_overlap", "family": name,
                        "epoch": int(row["epoch"]),
                        "ids": sorted(overlap)[:10],
                    })
                seen.update(covered)
                if (
                    covered[0] != int(row["first_prediction_id"])
                    or covered[-1] != int(row["last_prediction_id"])
                ):
                    report["coverage_intact"] = False
                    report["problems"].append({
                        "kind": "coverage_range_mismatch", "family": name,
                        "epoch": int(row["epoch"]),
                    })
                # (4) and (5): the digest over the exact covered rows.
                placeholders = ", ".join("?" for _ in covered)
                present = self.db.execute(
                    f"""SELECT id, predicted_success, actual_status, evidence_ok
                        FROM task_predictions WHERE id IN ({placeholders})
                        ORDER BY id""",
                    covered,
                ).fetchall()
                if len(present) != len(covered):
                    missing = sorted(
                        set(covered) - {int(item["id"]) for item in present}
                    )
                    report["coverage_intact"] = False
                    report["coverage_gaps"].append({
                        "family": name,
                        "epoch": int(row["epoch"]),
                        "missing": missing[:20],
                        "missing_count": len(missing),
                    })
                    continue
                recomputed = _memory().ladder_coverage_digest(
                    self._spine_key,
                    [
                        (
                            int(item["id"]), float(item["predicted_success"]),
                            str(item["actual_status"]), item["evidence_ok"],
                        )
                        for item in present
                    ],
                )
                if not _memory().hmac.compare_digest(
                    recomputed, str(row["coverage_digest"])
                ):
                    # THE tamper of design 2.4 -- an epoch re-cut over
                    # different rows.  It used to append a problem and leave
                    # ``coverage_intact`` true, so every operator surface,
                    # which reads only that flag, printed a clean ledger over
                    # a forged one (R-5).
                    report["coverage_intact"] = False
                    report["problems"].append({
                        "kind": "coverage_digest_mismatch", "family": name,
                        "epoch": int(row["epoch"]),
                    })
                else:
                    report["epochs_rederivable"] += 1
        report["chain_ok"] = not report["problems"]
        return report

    def _ladder_verified_reuses(
        self,
        family: str,
        project_id: int,
        *,
        now: str | None = None,
        restrict: Sequence[int] | None = None,
    ) -> dict[int, list[dict[str, Any]]]:
        """Per lesson, the applications that count as a verified reuse.

        Every clause of design 3.3 is applied here and nothing is cached: a
        lesson that has since been superseded, contradicted, quarantined or
        expired stops counting the moment it does, which is what makes
        ``proof_stale`` a live check rather than a stored flag.

        ``restrict`` limits the scan to an exact set of application ids -- the
        set a promotion row recorded.  Re-validating over the recorded set
        rather than over everything that currently qualifies is ruling 16:
        digesting the maximal set meant an approved skill's own next verified
        reuse moved the digest, so the artefact withdrew on the very outcome
        the ladder exists to reward (R-1).  New evidence must never invalidate
        an old proof.
        """
        stamp = str(now or _memory().now_iso())
        restricted = None if restrict is None else sorted({int(v) for v in restrict})
        clause = ""
        extra: tuple[Any, ...] = ()
        if restricted is not None:
            if not restricted:
                return {}
            clause = " AND la.id IN (" + ", ".join("?" for _ in restricted) + ")"
            extra = tuple(restricted)
        rows = self.db.execute(
            f"""SELECT la.id AS application_id, la.memory_id, la.prediction_id,
                      la.created_at AS application_created_at,
                      la.resolved_at AS application_resolved_at,
                      la.successful, la.tool_name,
                      p.origin, p.created_at AS prediction_created_at,
                      p.resolved_at AS prediction_resolved_at,
                      p.actual_status, p.evidence_ok, p.predicted_verification,
                      p.task_id, p.conversation_id,
                      lc.observed_at, lc.valid_until
               FROM lesson_applications AS la
               JOIN task_predictions AS p ON p.id = la.prediction_id
               JOIN lesson_controls AS lc ON lc.memory_id = la.memory_id
               WHERE la.family=? AND la.resolved_at IS NOT NULL
                 AND la.successful=1 AND lc.project_id=?{clause}
               ORDER BY la.memory_id, la.id""",
            (str(family), int(project_id), *extra),
        ).fetchall()
        eligible: dict[int, bool] = {}
        per_lesson: dict[int, list[dict[str, Any]]] = {}
        seen_keys: dict[int, set[tuple[str, int]]] = {}
        for row in rows:
            memory_id = int(row["memory_id"])
            # (2) evidence, with no exemption, and (3) a reusable origin.
            if row["evidence_ok"] is None or int(row["evidence_ok"]) != 1:
                continue
            if str(row["origin"]) not in _memory().LESSON_REUSABLE_PREDICTION_ORIGINS:
                continue
            # (4) the application falls inside the lesson's validity window.
            values = self._lesson_application_values(
                family=str(family),
                application_created_at=row["application_created_at"],
                application_resolved_at=row["application_resolved_at"],
                application_successful=row["successful"],
                prediction_created_at=row["prediction_created_at"],
                prediction_resolved_at=row["prediction_resolved_at"],
                prediction_actual_status=row["actual_status"],
                prediction_evidence_ok=row["evidence_ok"],
                prediction_verification=row["predicted_verification"],
                lesson_observed_at=row["observed_at"],
                lesson_valid_until=row["valid_until"],
                validation_at=stamp,
            )
            if values is None:
                continue
            # (5) provenance and controls both valid *now*.
            if memory_id not in eligible:
                eligible[memory_id] = bool(
                    self._lesson_provenance_validation(memory_id)[0]
                    and self._lesson_control_validation(
                        memory_id, project_id=int(project_id), as_of=stamp
                    )[0]
                )
            if not eligible[memory_id]:
                continue
            # (6) the distinctness key, so three applications from three
            #     deleted conversations neither collapse into one nor count as
            #     three (M-13).  A row with neither link does not count at all.
            if row["conversation_id"] is not None:
                key = ("c", int(row["conversation_id"]))
            elif row["task_id"] is not None:
                key = ("t", int(row["task_id"]))
            else:
                continue
            keys = seen_keys.setdefault(memory_id, set())
            if key in keys and restricted is None:
                # Maximal derivation: three applications in one conversation
                # are one context, so the first wins.  On the restricted path
                # a collision means two recorded rows have come to share a key
                # (a conversation delete nulled one), which is real staleness
                # and is caught by the set comparison in ``ladder_proof``
                # rather than hidden by dropping a row here.
                continue
            keys.add(key)
            per_lesson.setdefault(memory_id, []).append({
                "application_id": int(row["application_id"]),
                "memory_id": memory_id,
                "prediction_id": int(row["prediction_id"]),
                "key": list(key),
                "resolved_at": str(row["application_resolved_at"]),
                "successful": 1,
                "evidence_ok": 1,
                "tool_name": (
                    None if row["tool_name"] is None else str(row["tool_name"])
                ),
                "oracle": str(row["predicted_verification"]),
            })
        return per_lesson

    def _ladder_effectiveness(self, family: str) -> dict[str, Any]:
        """The comparison group, computed from **sealed epochs** and not from
        live rows -- which is what makes the ledger load-bearing for staging
        rather than decorative (design 3.3, H-10)."""
        epochs = self.calibration_ledger(family)
        applied_n = sum(int(row["applied_n"]) for row in epochs)
        applied_successes = sum(int(row["applied_successes"]) for row in epochs)
        unapplied_n = sum(int(row["unapplied_n"]) for row in epochs)
        unapplied_successes = sum(
            int(row["unapplied_successes"]) for row in epochs
        )
        minimum = int(_memory().learning_ladder.LADDER_EFFECTIVENESS_MIN_APPLIED)
        if applied_n < minimum:
            satisfied = False
            contrast = "insufficient applied outcomes"
        elif unapplied_n == 0:
            satisfied = True
            contrast = "no unapplied outcomes"
        else:
            satisfied = (
                applied_successes / applied_n >= unapplied_successes / unapplied_n
            )
            contrast = "applied versus unapplied"
        return {
            "satisfied": bool(satisfied),
            "contrast": contrast,
            "applied_n": applied_n,
            "applied_successes": applied_successes,
            "unapplied_n": unapplied_n,
            "unapplied_successes": unapplied_successes,
            "minimum_applied": minimum,
        }

    def ladder_proof(
        self,
        *,
        family: str,
        project_id: int,
        now: str | None = None,
        application_ids: Sequence[int] | None = None,
    ) -> dict[str, Any]:
        """Derive design 3.3's outcome proof, or say exactly why there is none.

        Always returns a dict.  ``reason`` is ``None`` when the proof holds and
        one of ``no_eligible_lesson``, ``insufficient_reuse``,
        ``insufficient_effectiveness``, ``proof_stale`` or ``proof_unbacked``
        when it does not.

        **Two modes, and the difference is ruling 16.**  With no
        ``application_ids`` this derives the maximal proof, which is what
        staging does.  With them it re-validates *exactly* that recorded set:
        every recorded row must still exist, still be resolved and successful,
        still be evidence-backed and in-window, and still belong to a lesson
        whose provenance and controls hold -- a **subset check**.  Digesting
        the maximal set at re-validation time was R-1: an approved skill's own
        next verified reuse moved the digest, the row failed ``proof_stale``,
        the artefact withdrew, and the family's ladder never recovered.  Adding
        evidence must never invalidate a proof; only losing it can.
        """
        self._require_ladder()
        stamp = str(now or _memory().now_iso())
        recorded = (
            None if application_ids is None
            else sorted({int(value) for value in application_ids})
        )
        per_lesson = self._ladder_verified_reuses(
            family, project_id, now=stamp, restrict=recorded
        )
        minimum = int(_memory().learning_ladder.LADDER_MIN_VERIFIED_REUSES)
        if recorded is None:
            qualifying = {
                memory_id: uses
                for memory_id, uses in per_lesson.items()
                if len(uses) >= minimum
            }
        else:
            # The recorded set already cleared the threshold when it was
            # staged; what matters now is that all of it survived.
            qualifying = dict(per_lesson)
        effectiveness = self._ladder_effectiveness(family)
        proof: dict[str, Any] = {
            "family": str(family),
            "project_id": int(project_id),
            "lesson_ids": sorted(qualifying),
            "reuses": sum(len(uses) for uses in qualifying.values()),
            "contexts": len({
                tuple(use["key"])
                for uses in qualifying.values() for use in uses
            }),
            "tool_names": sorted({
                str(use["tool_name"])
                for uses in qualifying.values() for use in uses
                if use["tool_name"]
            }),
            "oracles": sorted({
                str(use["oracle"])
                for uses in qualifying.values() for use in uses
            }),
            "applications": sorted(
                (use for uses in qualifying.values() for use in uses),
                key=lambda use: int(use["application_id"]),
            ),
            "effectiveness": effectiveness,
            "minimum_reuses": minimum,
            "minimum_lessons": int(_memory().learning_ladder.LADDER_MIN_DISTINCT_LESSONS),
            "reason": None,
        }
        if recorded is not None:
            survived = sorted(
                int(use["application_id"])
                for uses in qualifying.values() for use in uses
            )
            if survived != recorded:
                # Something the row was proved on is gone or no longer
                # qualifies.  Only a LOSS can reach here: the scan was
                # restricted to the recorded ids, so a new application cannot
                # appear in it.
                proof["reason"] = "proof_stale"
                proof["missing_applications"] = sorted(
                    set(recorded) - set(survived)
                )[:20]
            elif not self._ladder_applications_are_receipted(
                [int(use["prediction_id"]) for use in proof["applications"]]
            ):
                # Ruling 21: the evidence table needs its own integrity
                # binding.  Rows planted by raw SQL have no matching
                # ``lesson.applied`` receipt and change the recomputed
                # identity digest, so they cannot manufacture a proof.
                proof["reason"] = "proof_unbacked"
        elif not per_lesson:
            proof["reason"] = "no_eligible_lesson"
        elif len(qualifying) < int(_memory().learning_ladder.LADDER_MIN_DISTINCT_LESSONS):
            proof["reason"] = "insufficient_reuse"
        elif not effectiveness["satisfied"]:
            proof["reason"] = "insufficient_effectiveness"
        elif not self._ladder_applications_are_receipted(
            [int(use["prediction_id"]) for use in proof["applications"]]
        ):
            proof["reason"] = "proof_unbacked"
        proof["sha256"] = self._ladder_proof_digest(proof)
        return proof

    def _ladder_recorded_application_ids(
        self, row: Mapping[str, Any]
    ) -> list[int] | None:
        """The application ids a promotion row was proved on, or ``None``.

        ``None`` means the row predates the recorded-set contract (or its
        ``proof_json`` is unreadable) and the caller falls back to the maximal
        derivation, which is the pre-fix behaviour and is only reachable for a
        row written before this change.
        """
        record = _memory()._ladder_payload(row["proof_json"])
        ids = record.get("application_ids")
        if not isinstance(ids, list) or not ids:
            return None
        found: list[int] = []
        for item in ids:
            if isinstance(item, bool) or not isinstance(item, int) or item <= 0:
                return None
            found.append(int(item))
        return sorted(set(found))

    def _ladder_application_identity_digest(self, prediction_id: int) -> str:
        """Keyed digest over the identity of one prediction's applications.

        Identity only -- ``(id, prediction_id, memory_id)`` -- because those
        three are immutable from the moment the row is inserted.  The
        application's ``successful`` column is deliberately NOT in it: the
        event is appended when the lesson is *matched*, which is before the
        turn resolves, so the column is NULL at that instant and any digest
        over it would disagree with every later re-check.  That is R-1's
        failure mode by another route, and it is why the receipt binds the
        evidence's identity while the proof's own clauses re-derive its
        verdict live.
        """
        rows = self.db.execute(
            """SELECT id, prediction_id, memory_id FROM lesson_applications
               WHERE prediction_id=? ORDER BY id""",
            (int(prediction_id),),
        ).fetchall()
        return _memory().memory_spine.lesson_applications_digest(
            self._spine_key,
            [
                (int(row["id"]), int(row["prediction_id"]), int(row["memory_id"]))
                for row in rows
            ],
        )

    def _ladder_applications_are_receipted(
        self, prediction_ids: Sequence[int]
    ) -> bool:
        """Every prediction in the proof has a matching ``lesson.applied``.

        Ruling 21: the application table is the single input that decides
        whether a document is promoted, and it is the only such input with no
        receipt -- ``proof_sha256`` is computed *over* whatever rows are
        there, so it is self-consistent with a forged set.  The event binds
        the set that existed when the turn matched; a row planted by raw SQL
        afterwards changes the recomputed identity digest and matches no
        event, so it cannot manufacture a proof.

        The **newest** event for a prediction is the authoritative one, so two
        legitimate calls in one turn stay consistent (each event digests the
        cumulative set after its own insert).  Returns True unchanged on a
        store whose spine does not carry the kind yet, so the check switches
        on with the kind rather than failing every proof before it lands.
        """
        if _memory()._LESSON_APPLIED_KIND not in _memory().memory_spine.SPINE_KINDS:
            return True
        for prediction_id in sorted({int(value) for value in prediction_ids}):
            event = self.db.execute(
                """SELECT payload_json FROM memory_spine_events
                   WHERE kind=?
                     AND json_extract(payload_json, '$.prediction_id')=?
                   ORDER BY id DESC LIMIT 1""",
                (_memory()._LESSON_APPLIED_KIND, int(prediction_id)),
            ).fetchone()
            if event is None:
                return False
            recorded = str(
                _memory()._ladder_payload(event["payload_json"]).get(
                    "applications_digest"
                ) or ""
            )
            if not _memory().hmac.compare_digest(
                recorded, self._ladder_application_identity_digest(prediction_id)
            ):
                return False
        return True

    def _ladder_proof_digest(self, proof: Mapping[str, Any]) -> str:
        """Keyed HMAC over the sorted proof material.

        Only the material, never the verdict: the digest covers the lesson
        ids, the application ids, the prediction ids, the distinctness keys
        and each application's ``(resolved_at, successful, evidence_ok,
        tool_name)``, so re-deriving it later answers "is this the same
        evidence" and nothing else.
        """
        material = [
            [
                int(use["application_id"]), int(use["memory_id"]),
                int(use["prediction_id"]), list(use["key"]),
                str(use["resolved_at"]), int(use["successful"]),
                int(use["evidence_ok"]),
                None if use["tool_name"] is None else str(use["tool_name"]),
            ]
            for use in proof["applications"]
        ]
        payload = _memory().memory_spine.canonical({
            "family": str(proof["family"]),
            "project_id": int(proof["project_id"]),
            "lesson_ids": [int(value) for value in proof["lesson_ids"]],
            "applications": material,
        })
        return _memory().hmac.new(
            self._spine_key,
            (_memory()._LADDER_PROOF_DIGEST_TAG + "\0" + payload).encode("utf-8"),
            _memory().hashlib.sha256,
        ).hexdigest()

    def ladder_promotions(
        self,
        *,
        project_id: int | None = None,
        family: str | None = None,
        stages: Sequence[str] | None = None,
        skill_name: str | None = None,
        include_token: bool = False,
    ) -> list[dict[str, Any]]:
        """Promotion rows, newest last.

        ``include_token`` is off by default and every model-facing or
        network-facing caller leaves it off: the confirmation code is an
        operator surface only (S-1), and a payload builder that has the value
        and must remember to drop it is the shape that leaks.  ``ladder list``,
        ``ladder show`` and ``/ladder`` are the three callers that pass True.
        """
        self._ensure_open()
        if family is not None and family not in self.PREDICTION_FAMILIES:
            raise ValueError(f"Unknown task family: {family}")
        if not self._ladder_ready:
            return []
        clauses: list[str] = []
        params: list[Any] = []
        if project_id is not None:
            clauses.append("project_id=?")
            params.append(int(project_id))
        if family is not None:
            clauses.append("family=?")
            params.append(str(family))
        if skill_name is not None:
            clauses.append("skill_name=?")
            params.append(str(skill_name))
        if stages is not None:
            wanted = [str(stage) for stage in stages]
            unknown = sorted(set(wanted) - _memory().LADDER_PROMOTION_STAGES)
            if unknown:
                raise ValueError(f"Unknown promotion stage: {unknown[0]}")
            clauses.append(
                "stage IN (" + ", ".join("?" for _ in wanted) + ")"
            )
            params.extend(wanted)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = self.db.execute(
            f"SELECT * FROM ladder_promotions {where} ORDER BY id", params
        ).fetchall()
        return [_memory()._ladder_promotion_dict(row, include_token=include_token) for row in rows]

    def ladder_promotion(
        self, promotion_id: int, *, include_token: bool = False
    ) -> dict[str, Any] | None:
        self._ensure_open()
        if not self._ladder_ready:
            return None
        row = self.db.execute(
            "SELECT * FROM ladder_promotions WHERE id=?", (int(promotion_id),)
        ).fetchone()
        return (
            None if row is None
            else _memory()._ladder_promotion_dict(row, include_token=include_token)
        )

    def _ladder_live_documents(
        self, workspace: Path | None
    ) -> dict[str, dict[str, Any]]:
        """Every auto-distilled document currently live in one workspace.

        Keyed by name.  A workspace that does not exist, or one on a drive
        that has gone away, yields nothing rather than raising: the callers
        report ``workspace_unavailable`` instead of failing a worker pass
        (S-8).
        """
        if workspace is None:
            return {}
        live: dict[str, dict[str, Any]] = {}
        try:
            catalog = _memory().skill_library.list_available_skills(_memory().Path(workspace))
        except (OSError, ValueError):
            return {}
        for entry in catalog:
            if not bool(entry.get("auto_distilled")):
                continue
            name = str(entry.get("name") or "")
            try:
                document = _memory().skill_library.read_available_skill(name, _memory().Path(workspace))
            except (KeyError, OSError, ValueError, PermissionError):
                continue
            live[name] = {
                "name": name,
                "family": entry.get("family"),
                "verified_outcomes": entry.get("verified_outcomes"),
                "sha256": str(document.get("sha256") or ""),
                "content": str(document.get("content") or ""),
            }
        return live

    def ladder_legacy_documents(
        self, *, workspace: Path, project_id: int | None = None
    ) -> list[dict[str, Any]]:
        """Pre-M4 live documents the grandfather pass adopted (ruling 2).

        A separate bucket from the unverified promotions, because no ladder
        promotion ever claimed them: they reach the model with no proof, no
        gate and no ledger check until an operator approves or rolls one back,
        which is the pre-M4 status quo made visible rather than a new
        weakness (S-4).
        """
        self._ensure_open()
        if not self._ladder_ready:
            return []
        live = self._ladder_live_documents(workspace)
        rows = self.ladder_promotions(
            project_id=project_id, stages=("unapproved_legacy",)
        )
        found: list[dict[str, Any]] = []
        for row in rows:
            document = live.get(str(row["skill_name"]))
            found.append({
                "promotion_id": int(row["id"]),
                "project_id": int(row["project_id"]),
                "family": str(row["family"]),
                "skill_name": str(row["skill_name"]),
                "approved_sha256": row["approved_sha256"],
                "adopted": True,
                "live": document is not None,
                "digest_matches": bool(
                    document is not None
                    and str(document["sha256"]) == str(row["approved_sha256"] or "")
                ),
            })
        # Documents the ladder has never touched at all: pre-M4 artefacts on a
        # store where the grandfather pass has not run yet.  They belong in
        # this bucket and not among the unverified promotions -- see
        # ``_ladder_untouched_documents`` for why that distinction is
        # load-bearing rather than cosmetic.
        for name in sorted(self._ladder_untouched_documents(live, project_id)):
            found.append({
                "promotion_id": None,
                "project_id": None if project_id is None else int(project_id),
                "family": live[name].get("family"),
                "skill_name": name,
                "approved_sha256": None,
                "adopted": False,
                "live": True,
                "digest_matches": False,
            })
        return found

    def _ladder_untouched_documents(
        self, live: Mapping[str, Mapping[str, Any]], project_id: int | None
    ) -> set[str]:
        """Live auto-distilled documents with **no promotion row of any stage**.

        The distinction this draws is load-bearing, and it was found by
        execution rather than by reading (see the review record).  Design
        3.7's ``no_approved_row`` is meant for the crash window of design 7.8
        -- the file was written and the approving transaction then failed --
        and in that case a row for the pair *does* exist, at ``staged``.  A
        document with no row at all is something else: a pre-M4 artefact on a
        store whose grandfather pass has not run yet.

        Counting the second as an unverified promotion is a trap with a
        measured consequence.  ``unverified_at_seal`` is frozen into an
        append-only ledger row, clause (4) of design 2.3 has no band and no
        slack, and every epoch sealed before the first grandfather pass would
        therefore record a regression that can never be corrected -- so the
        family refuses every staging and every approval, including the very
        approval that would clear the document causing it.  That is the S-4
        trap by another route, and excluding untouched documents here closes
        it while leaving the crash window exactly as design 7.8 wants it.

        They are not ignored: they are reported by
        ``ladder_legacy_documents`` with ``adopted: False``, which is what
        ``ladder status`` and ``jarvis doctor`` print, and the grandfather
        pass adopts them at ``unapproved_legacy`` on its first run.
        """
        touched = {
            str(row["skill_name"])
            for row in self.ladder_promotions(project_id=project_id)
        } | self._ladder_named_in_spine()
        return {name for name in live if name not in touched}

    def _ladder_named_in_spine(self) -> set[str]:
        """Every skill name any ``ladder.*`` event has ever mentioned.

        A row can be deleted by raw SQL; an event cannot.  Without this, a
        raw-SQL ``DELETE`` of an ``approved`` row under a live document turned
        that document into an *untouched* one, which the next grandfather pass
        adopted at ``unapproved_legacy`` -- after which it reached the model
        with no proof, no gate and no ledger check, and never counted toward
        ``unverified_at_seal`` (R-9).  The spine is the thing the attacker
        cannot rewrite, so it is what decides whether the ladder has ever
        touched a name.
        """
        placeholders = ", ".join("?" for _ in _memory().LADDER_SPINE_KINDS)
        return {
            str(row[0]) for row in self.db.execute(
                f"""SELECT DISTINCT json_extract(payload_json, '$.skill_name')
                    FROM memory_spine_events
                    WHERE kind IN ({placeholders})
                      AND json_extract(payload_json, '$.skill_name') IS NOT NULL""",
                _memory().LADDER_SPINE_KINDS,
            )
        }

    def _ladder_unverified_cache_key(
        self,
        project_id: int | None,
        workspace: Path | None,
        live: Mapping[str, Mapping[str, Any]],
        rows: Sequence[Mapping[str, Any]],
    ) -> tuple[Any, ...]:
        """Everything the verdict depends on, as one keyed digest.

        The cache this keys is a **correctness surface**, not a convenience,
        so the key is the whole input and not a summary of it.  Staleness
        cannot hide a tamper because every input a tamper can touch is in the
        digest: an edited live document changes its ``sha256`` and therefore
        the key, so it misses the cache and is re-verified from scratch.

        Covered, and each is here because something can change it without
        changing anything else:

        1. the project and the workspace path;
        2. every promotion row's stage, digests and ``updated_at`` -- any
           ladder write moves one of these;
        3. the live documents' names and **content digests** -- the tamper;
        4. the spine head event id -- every seal, erase, memory write and
           ladder transition appends an event, so this moves for all of them;
        5. the resolved-prediction count and high-water id -- an ordinary turn
           resolving can shut the calibrated gate without touching 2-4;
        6. a digest over the scoped lessons' lifecycle rows -- a supersede or
           a contradiction flips ``lesson_controls`` in place and appends no
           event, so it would otherwise be invisible to 4.

        **Staged documents are deliberately NOT in the key**, and their
        absence is the point: this function's verdict reads live documents,
        promotion rows, the spine and the lesson lifecycle, and nothing
        staged.  Keying on them bought no correctness, invalidated the cache
        whenever anyone staged, and cost a staging-root walk on every call --
        including the calls where the caller passed ``documents`` precisely
        to avoid a second walk.  With them gone this method touches the
        filesystem **not at all** when ``documents`` is supplied.

        Not covered, deliberately and stated so it can be checked rather than
        trusted: nothing that the verdict reads.  If a future input is added
        to the verdict it must be added here, and the test battery asserts
        each of the six above invalidates on its own.
        """
        head = self.db.execute(
            "SELECT COALESCE(MAX(id), 0) FROM memory_spine_events"
        ).fetchone()[0]
        outcomes = self.db.execute(
            """SELECT COUNT(*), COALESCE(MAX(id), 0) FROM task_predictions
               WHERE resolved_at IS NOT NULL"""
        ).fetchone()
        lesson_ids: set[int] = set()
        for row in rows:
            for value in _memory()._ladder_payload_list(row["lesson_ids_json"]):
                if isinstance(value, int) and not isinstance(value, bool):
                    lesson_ids.add(int(value))
        lifecycle: list[list[Any]] = []
        if lesson_ids:
            scoped = sorted(lesson_ids)
            placeholders = ", ".join("?" for _ in scoped)
            lifecycle = [
                [
                    int(item["memory_id"]), str(item["lifecycle_status"]),
                    None if item["superseded_by"] is None
                    else int(item["superseded_by"]),
                    str(item["valid_until"]),
                ]
                for item in self.db.execute(
                    f"""SELECT memory_id, lifecycle_status, superseded_by,
                               valid_until
                        FROM lesson_controls WHERE memory_id IN ({placeholders})
                        ORDER BY memory_id""",
                    scoped,
                )
            ]
        material = _memory().memory_spine.canonical({
            "project_id": None if project_id is None else int(project_id),
            "workspace": (
                None if workspace is None
                else _memory().os.path.normcase(str(_memory().Path(workspace)))
            ),
            "rows": [
                [
                    int(row["id"]), str(row["stage"]), str(row["family"]),
                    str(row["skill_name"]),
                    str(row["approved_sha256"] or ""),
                    str(row["proof_sha256"]), str(row["updated_at"]),
                ]
                for row in rows
            ],
            "live": sorted(
                (name, str(entry["sha256"])) for name, entry in live.items()
            ),
            "head": int(head or 0),
            "outcomes": [int(outcomes[0] or 0), int(outcomes[1] or 0)],
            "lifecycle": lifecycle,
            # The pending-withdrawal set is part of the answer (item 30), so
            # it has to be part of the key: a row that becomes pending, or
            # stops being pending when its receipt finally lands, must miss.
            "pending": sorted(
                int(entry["promotion_id"])
                for entry in self.ladder_pending_withdrawals(project_id)
            ),
        })
        return (
            "ladder-unverified",
            _memory().hmac.new(
                self._spine_key,
                (_memory()._LADDER_VERIFY_CACHE_TAG + "\x1f" + material).encode("utf-8"),
                _memory().hashlib.sha256,
            ).hexdigest(),
        )

    def ladder_unverified_promotions(
        self,
        *,
        workspace: Path | None,
        project_id: int | None = None,
        documents: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        """Every live promoted artefact that is not verified, with its reason.

        A *live promoted artefact* is an auto-distilled document in the live
        root **whose ``(project_id, skill_name)`` has no ``unapproved_legacy``
        row** (S-4): a document the grandfather pass adopted is not a promoted
        artefact at all, is reported by ``ladder_legacy_documents`` instead,
        and can therefore never make clause (4) of design 2.3 fire -- which
        would otherwise trap the store, since a regressed family refuses the
        very approval that would clear the legacy row.

        Reasons (closed set): ``no_approved_row``, ``digest_mismatch``,
        ``proof_stale``, ``gate_closed``, ``ledger_regressed``,
        ``lineage_broken``, ``screened_component``, ``orphan_document``.
        """
        self._ensure_open()
        if not self._ladder_ready:
            return []
        # ``documents`` lets a caller that already walked the catalog this turn
        # hand it over instead of paying for a second walk (the seam rule's
        # append-a-keyword-with-a-default, as ``skill_channel_report`` does).
        live = (
            self._ladder_live_documents(workspace) if documents is None
            else dict(documents)
        )
        rows_for_key = self.ladder_promotions(project_id=project_id)
        cache_key = self._ladder_unverified_cache_key(
            project_id, workspace, live, rows_for_key
        )
        cached = self._recall_cache.get(cache_key)
        if cached is not None:
            return [dict(entry) for entry in cached]
        # Out of scope entirely: a document the grandfather pass adopted,
        # and a document the ladder has never touched.  Neither was ever
        # claimed by a promotion, so neither can be an unverified one (S-4).
        legacy = {
            str(row["skill_name"])
            for row in self.ladder_promotions(
                project_id=project_id, stages=("unapproved_legacy",)
            )
        } | self._ladder_untouched_documents(live, project_id)
        approved = {
            str(row["skill_name"]): row
            for row in self.ladder_promotions(
                project_id=project_id, stages=("approved",)
            )
        }
        # Design 3.7: verified requires the chain to verify THROUGH the
        # approving event.  A broken head invalidates every approved artefact
        # at once, so it is checked once here rather than per row.
        head_ok = self._ladder_spine_head_ok()
        unverified: list[dict[str, Any]] = []
        for name, document in sorted(live.items()):
            if name in legacy:
                continue
            row = approved.get(name)
            if row is not None:
                try:
                    if head_ok:
                        reason = self._ladder_promotion_defect(row, document)
                    else:
                        # A broken head invalidates every approved artefact
                        # at once; no per-row check can rescue one.
                        reason = "lineage_broken"
                except (RuntimeError, _memory().sqlite3.DatabaseError):
                    # Fail closed on the artefact rather than on the turn.
                    reason = "proof_stale"
                if reason is not None:
                    unverified.append({
                        "skill_name": name,
                        "promotion_id": int(row["id"]),
                        "project_id": int(row["project_id"]),
                        "family": str(row["family"]),
                        "reason": reason,
                        "deferred": False,
                    })
                continue
            if row is None:
                # ``orphan_document``: a live FILE the ladder has touched
                # before but that no live row claims -- including the shape
                # R-9 plants, where the approved row was deleted by raw SQL.
                # ``no_approved_row`` is kept for the design 7.8 crash window,
                # where a row for the pair does exist, at ``staged``.
                touched_rows = any(
                    str(entry["skill_name"]) == name for entry in rows_for_key
                )
                unverified.append({
                    "skill_name": name,
                    "promotion_id": None,
                    "family": document.get("family"),
                    "reason": (
                        "no_approved_row" if touched_rows else "orphan_document"
                    ),
                    "deferred": False,
                })
                continue
        # A row that claims the live document but has none is the other half
        # of the crash window (design 7.8), and it has its own name there:
        # ``live_document_missing``.  ``orphan_document`` means the opposite
        # shape -- a live FILE with no row claiming it -- and the two readers
        # must not use one word for both, which is what made the reconciler
        # and this function disagree.
        for name, row in sorted(approved.items()):
            if name not in live:
                unverified.append({
                    "skill_name": name,
                    "promotion_id": int(row["id"]),
                    "project_id": int(row["project_id"]),
                    "family": str(row["family"]),
                    "reason": "live_document_missing",
                    "deferred": False,
                })
        # Design 10.7 item 30: while a withdrawal's receipt is outstanding
        # the artefact keeps being listed, on EVERY later call, until the
        # receipt is flushed.  These rows have no live document and no
        # approved row -- the first read parked one and moved the other -- so
        # nothing above can find them, which is precisely why the second read
        # used to say the store was fine.
        # Item 35: pass the live set so an already-parked orphan surfaces as
        # a spine-derived outstanding withdrawal, while a still-live orphan is
        # excluded here and reported as ``orphan_document`` by the scan above
        # (the ``listed`` skip below is the second guard against a double).
        listed = {entry["skill_name"] for entry in unverified}
        for pending in self.ladder_pending_withdrawals(
            project_id, documents=live
        ):
            if str(pending["skill_name"]) in listed:
                continue
            project_value = pending.get("project_id")
            unverified.append({
                "skill_name": str(pending["skill_name"]),
                "promotion_id": int(pending["promotion_id"]),
                "project_id": (
                    None if project_value is None else int(project_value)
                ),
                "family": (
                    None if pending.get("family") is None
                    else str(pending["family"])
                ),
                "reason": str(pending.get("reason", "lineage_broken")),
                "deferred": bool(pending.get("deferred", True)),
            })
        self._recall_cache.put(
            cache_key, [dict(entry) for entry in unverified], 0
        )
        return [dict(entry) for entry in unverified]

    def _ladder_spine_head_ok(self) -> bool:
        """Does the keyed head record still verify and name the chain's tip?

        The O(1) half of ``verify_spine``, and exactly the check
        ``append_event`` makes before it will chain onto the head.  Design
        3.7 requires the chain to verify **through** an artefact's approving
        event, not merely that the event exists; without this a store whose
        head had been tampered with kept serving its approved skills, because
        the per-row lineage lookup still found the event sitting there.

        Cheap on purpose: a full chain walk per read-path call would be paid
        on every turn, and the head is what a tamper or a partially restored
        backup breaks.
        """
        if not self._spine_ready:
            return False
        try:
            last = self.db.execute(
                "SELECT id, event_sha256 FROM memory_spine_events "
                "ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if last is None:
                return True
            head = self.db.execute(
                "SELECT last_event_id, last_event_sha256, head_mac "
                "FROM memory_spine_head WHERE id=1"
            ).fetchone()
        except _memory().sqlite3.DatabaseError:
            return False
        if head is None:
            return False
        return bool(
            _memory().hmac.compare_digest(
                _memory().memory_spine.head_mac(
                    self._spine_key, int(head[0]), str(head[1])
                ),
                str(head[2]),
            )
            and int(head[0]) == int(last[0])
            and str(head[1]) == str(last[1])
        )

    def _ladder_promotion_defect(
        self, row: Mapping[str, Any], document: Mapping[str, Any]
    ) -> str | None:
        """The first design 3.7 clause an approved artefact fails, or None."""
        if str(document.get("sha256") or "") != str(row["approved_sha256"] or ""):
            return "digest_mismatch"
        event = self.db.execute(
            """SELECT id FROM memory_spine_events
               WHERE kind='ladder.approved' AND subject_kind='ladder'
                 AND subject_id=? ORDER BY id DESC LIMIT 1""",
            (int(row["id"]),),
        ).fetchone()
        if event is None:
            return "lineage_broken"
        family = str(row["family"])
        try:
            proof = self.ladder_proof(
                family=family, project_id=int(row["project_id"]),
                application_ids=self._ladder_recorded_application_ids(row),
            )
        except (ValueError, RuntimeError, _memory().sqlite3.DatabaseError):
            # ``RuntimeError`` covers ``memory_spine.SpineError`` and the
            # ladder-unavailable guard.  A store that cannot answer whether
            # the proof holds must report the artefact as unverified, not
            # raise into the caller: the read path reaches here on every turn
            # (ruling 27).
            return "proof_stale"
        if str(proof["reason"] or "") == "proof_unbacked":
            return "proof_unbacked"
        if proof["reason"] is not None or not _memory().hmac.compare_digest(
            str(proof["sha256"]), str(row["proof_sha256"])
        ):
            return "proof_stale"
        try:
            gate = self.calibration_gate(
                family, **_memory().learning_ladder.LADDER_GATE_THRESHOLDS
            )
        except (ValueError, _memory().sqlite3.DatabaseError):
            return "gate_closed"
        if not bool(gate.get("allowed")):
            return "gate_closed"
        if bool(
            self.calibration_ledger_monotonicity(family)["currently_regressed"]
        ):
            return "ledger_regressed"
        for component in (
            family, *(str(name) for name in _memory()._ladder_document_components(document))
        ):
            if _memory().contains_secret(component) or _memory().screen_endpoint(component)[0]:
                return "screened_component"
        return None

    def withdraw_ladder_promotion(
        self,
        promotion_id: int,
        *,
        reason: str,
        workspace: Path | None = None,
        actor: str = "runtime",
        conversation_id: int | None = None,
        permission: str = "runtime",
    ) -> dict[str, Any]:
        """Pull an artefact the runtime can no longer vouch for.

        Idempotent per ``(promotion_id, reason)``: a row already withdrawn for
        this reason is a no-op that appends no second event, so a read path
        that consults it on every turn does not fill the chain.  The operator
        learns *why* their skill went quiet instead of finding silence.

        **Never raises because the spine will not accept an append (ruling
        27).**  The read path reaches this method --
        ``approved_skills`` -> ``_withdraw_unverified`` -> here -- so on a
        store whose head no longer verifies, letting ``SpineError`` out would
        crash the operator's turn at exactly the moment the ladder is trying
        to protect them.  Instead:

        * the document is **parked first**, before any append is attempted,
          because moving the bytes out of the live root is the thing that
          actually stops the model seeing them and it must not depend on the
          chain being writable;
        * the row still moves to ``withdrawn``, with
          ``stage_reason='spine_unverified'``.  That is safe without the
          receipt because ``ladder_promotions_require_spine_event`` is a
          ``BEFORE INSERT`` trigger: it guards row creation, not a status
          transition, and the row keeps the creating event it was born with;
        * the receipt it could not append is recorded as a deferred write in
          ``degraded_writes()``, which ``ladder verify`` reports;
        * the return is a refusal -- ``{"withdrawn": False, "reason":
          "spine_unverified", "parked": True, ...}`` -- because the withdrawal
          is not fully recorded, and saying it was would be the lie that
          matters here.
        """
        self._require_ladder()
        code = str(reason)
        if not _memory()._LADDER_REASON_RE.match(code):
            raise ValueError(f"Unknown withdrawal reason: {reason}")
        if actor not in _memory().memory_spine.SPINE_ACTORS or actor == "model":
            raise ValueError(f"Unknown spine actor: {actor}")
        row = self.db.execute(
            "SELECT * FROM ladder_promotions WHERE id=?", (int(promotion_id),)
        ).fetchone()
        if row is None:
            return {"withdrawn": False, "reason": "missing"}
        if str(row["stage"]) == "withdrawn":
            # Idempotent per ``(promotion_id, reason)`` and, deliberately,
            # informative for ANY repeat: the read path calls this twice on a
            # withdrawing turn -- once to exclude the artefact and once to
            # learn why -- and a caller that treats every refusal as "receipt
            # deferred" would otherwise read a receipted withdrawal as a
            # deferred one just because the second call named a different
            # reason.  The row's own recorded reason comes back so the report
            # can tell the two apart.
            recorded = str(row["stage_reason"] or "")
            deferred = recorded == "spine_unverified" and bool(
                self.ladder_pending_withdrawals(int(row["project_id"]))
            )
            if deferred and self._flush_pending_withdrawal(int(row["id"])):
                # The spine has been repaired since; the receipt lands now and
                # the artefact leaves the pending set (item 30's "until the
                # receipt is flushed").
                deferred = False
            return {
                "withdrawn": False,
                "reason": "already_withdrawn",
                "promotion_id": int(row["id"]),
                "recorded_reason": recorded,
                "receipt_deferred": deferred,
            }
        if str(row["stage"]) not in {"staged", *_memory().LADDER_LIVE_STAGES}:
            return {
                "withdrawn": False, "reason": "not_live",
                "promotion_id": int(row["id"]), "stage": str(row["stage"]),
            }
        was_live = str(row["stage"]) in _memory().LADDER_LIVE_STAGES
        stamp = _memory().now_iso()
        # Park BEFORE the append (ruling 27).  On a healthy store the order is
        # invisible; on a broken one it is the whole point.
        parked = self._ladder_park_document(row, workspace, was_live=was_live)
        try:
            return self._withdraw_ladder_promotion_receipted(
                row, code, stamp, parked,
                actor=actor, conversation_id=conversation_id,
                permission=permission,
            )
        except _memory().memory_spine.SpineError as exc:
            return self._withdraw_ladder_promotion_deferred(
                row, code, stamp, parked, exc,
            )

    def _ladder_park_document(
        self,
        row: Mapping[str, Any],
        workspace: Path | None,
        *,
        was_live: bool,
    ) -> dict[str, Any] | None:
        """Move a live learned document into the staging root, or nothing.

        Ruling 16, second half.  Withdrawing a LIVE row while leaving the file
        live was R-1 steps 2-4: the document became an uncountable orphan,
        ``unverified_at_seal`` never returned to zero, clause (4) has no
        slack, and the family stayed ``currently_regressed`` for good -- with
        the only exit deleting the operator's document.  The bytes go to the
        staging root under a ``withdrawn-`` prefix instead: unreachable by the
        catalog and by the model's file tools, listed by ``ladder verify``,
        recoverable only through a new promotion.  Never deleted.
        """
        if not was_live or workspace is None:
            return None
        try:
            parked = _memory().skill_library.withdraw_learned_skill(
                _memory().Path(workspace), str(row["skill_name"])
            )
        except (OSError, ValueError, PermissionError):
            # ``ladder verify`` reconciles the file on its next run.
            return None
        # ``learning_ladder`` memoizes the catalog per workspace behind a
        # digest key; this module just moved a file out from under it, so the
        # memo is dropped rather than left answering from a document that is
        # no longer live.
        _memory()._ladder_clear_catalog_cache()
        return parked

    def _withdraw_ladder_promotion_deferred(
        self,
        row: Mapping[str, Any],
        code: str,
        stamp: str,
        parked: Mapping[str, Any] | None,
        exc: BaseException,
    ) -> dict[str, Any]:
        """The spine refused the append; withdraw anyway and defer the receipt."""
        promotion_id = int(row["id"])
        self._record_degraded_write(
            "withdraw",
            f"promotion={promotion_id} skill={str(row['skill_name'])} "
            f"({type(exc).__name__})",
            reason="spine_unverified",
        )
        moved = False
        try:
            with self._immediate_transaction():
                # Safe without the receipt: the lineage trigger is BEFORE
                # INSERT, so it guards creation and not this transition, and
                # the row keeps the creating event it was born with.
                self.db.execute(
                    """UPDATE ladder_promotions
                       SET stage='withdrawn', stage_reason='spine_unverified',
                           updated_at=?
                       WHERE id=?""",
                    (stamp, promotion_id),
                )
                moved = True
        except _memory().sqlite3.Error:
            moved = False
        return {
            "withdrawn": False,
            "reason": "spine_unverified",
            "promotion_id": promotion_id,
            "family": str(row["family"]),
            "skill_name": str(row["skill_name"]),
            "parked": bool(parked and parked.get("withdrawn")),
            "row_withdrawn": moved,
            "receipt_deferred": True,
            "intended_reason": code,
        }

    def _withdraw_ladder_promotion_receipted(
        self,
        row: Mapping[str, Any],
        code: str,
        stamp: str,
        parked: Mapping[str, Any] | None,
        *,
        actor: str,
        conversation_id: int | None,
        permission: str,
    ) -> dict[str, Any]:
        promotion_id = int(row["id"])
        with self._immediate_transaction():
            current = self.db.execute(
                "SELECT stage, stage_reason FROM ladder_promotions WHERE id=?",
                (int(promotion_id),),
            ).fetchone()
            if current is None or (
                str(current["stage"]) == "withdrawn"
                and str(current["stage_reason"] or "") == code
            ):
                return {
                    "withdrawn": False, "reason": "already_withdrawn",
                    "promotion_id": int(promotion_id),
                    "parked": bool(parked and parked.get("withdrawn")),
                }
            _memory().memory_spine.append_event(
                self.db,
                self._spine_key,
                kind="ladder.withdrawn",
                actor=actor,
                source="learning ladder",
                scope="global",
                permission=permission,
                outcome="applied",
                payload={
                    "at": stamp,
                    "family": str(row["family"]),
                    "project_id": int(row["project_id"]),
                    "skill_name": str(row["skill_name"]),
                    "withdrawn_sha256": row["approved_sha256"],
                    "reason": code,
                },
                now=stamp,
                conversation_id=conversation_id,
                subject_kind="ladder",
                subject_id=int(promotion_id),
            )
            self.db.execute(
                """UPDATE ladder_promotions
                   SET stage='withdrawn', stage_reason=?, updated_at=?
                   WHERE id=?""",
                (code, stamp, int(promotion_id)),
            )
        return {
            "withdrawn": True,
            "promotion_id": int(promotion_id),
            "reason": code,
            "family": str(row["family"]),
            "skill_name": str(row["skill_name"]),
            "parked": bool(parked and parked.get("withdrawn")),
            "replaced_parked_sha256": (
                None if parked is None else parked.get("replaced_sha256")
            ),
        }

    def ladder_pending_withdrawals(
        self,
        project_id: int | None = None,
        *,
        workspace: Path | None = None,
        documents: Mapping[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Withdrawals whose receipt is still outstanding (design 10.7 item 30).

        A withdrawal on a spine that will not append moves the row and parks
        the document but cannot write its ``ladder.withdrawn`` event.  The
        first read reported that correctly; every LATER read reported
        nothing -- the document was gone from the live root and no approved
        row remained, so ``ladder_unverified_promotions`` returned ``[]`` and
        the channel said ``none-approved``.  An artefact that is unverified
        with its receipt outstanding was being described as fine.

        Outstanding is decided from **durable** state and not from the
        in-memory queue: a row at ``withdrawn`` with
        ``stage_reason='spine_unverified'`` and no ``ladder.withdrawn`` event
        naming it.  That survives a fresh ``Memory`` instance, which
        ``degraded_writes()`` does not -- and the holdout's second read
        happens in a new one, which is exactly how this was found.

        **A pure read.**  No write, no transaction, no side effect: it runs on
        every turn that reaches the family check, including turns with nothing
        else to do.  The self-healing append lives in
        ``withdraw_ladder_promotion``, which is the path that already owns the
        write.
        """
        self._ensure_open()
        if not self._ladder_ready:
            return []
        clauses = ["p.stage = 'withdrawn'", "p.stage_reason = 'spine_unverified'"]
        params: list[Any] = []
        if project_id is not None:
            clauses.append("p.project_id = ?")
            params.append(int(project_id))
        try:
            rows = self.db.execute(
                f"""SELECT p.id, p.project_id, p.family, p.skill_name,
                           p.approved_sha256, p.updated_at
                    FROM ladder_promotions AS p
                    WHERE {' AND '.join(clauses)}
                      AND NOT EXISTS (
                          SELECT 1 FROM memory_spine_events AS e
                          WHERE e.kind = 'ladder.withdrawn'
                            AND e.subject_kind = 'ladder'
                            AND e.subject_id = p.id)
                    ORDER BY p.id""",
                params,
            ).fetchall()
        except _memory().sqlite3.DatabaseError:
            # A read that cannot run must not fail the turn; an empty pending
            # set is the quiet direction and the next call retries.
            return []
        result = [
            {
                "promotion_id": int(row["id"]),
                "project_id": int(row["project_id"]),
                "family": str(row["family"]),
                "skill_name": str(row["skill_name"]),
                "reason": "lineage_broken",
                "deferred": True,
                "intended_reason": "spine_unverified",
                "approved_sha256": row["approved_sha256"],
                "withdrawn_at": str(row["updated_at"]),
            }
            for row in rows
        ]
        # Spine-sourced orphan withdrawals (design 10.7 item 35).  Only when a
        # live set is available, so a not-yet-parked orphan (whose file is
        # still live and is reported as ``orphan_document`` by the live scan)
        # is not double-counted here as an outstanding receipt.  Without a
        # workspace this returns the row-backed set exactly as before, which
        # every existing caller relies on.
        live_names: set[str] | None = None
        if documents is not None:
            live_names = {str(name) for name in documents}
        elif workspace is not None:
            try:
                live_names = {
                    str(name) for name in self._ladder_live_documents(workspace)
                }
            except (OSError, ValueError, _memory().sqlite3.DatabaseError):
                live_names = None
        if live_names is not None:
            already = {entry["skill_name"] for entry in result}
            for orphan in self._ladder_orphan_pending(project_id):
                name = str(orphan["skill_name"])
                if name in live_names or name in already:
                    continue
                result.append(orphan)
        return result

    def _flush_pending_withdrawal(self, promotion_id: int) -> bool:
        """Append the receipt a broken spine refused, once it will take it.

        Called from ``withdraw_ladder_promotion``'s already-withdrawn branch,
        which the read path reaches on every withdrawing turn -- so a repaired
        store clears its own pending set on the next read rather than waiting
        for an operator.  Returns True when the receipt landed.
        """
        if not self._ladder_available() or not self._ladder_spine_head_ok():
            return False
        row = self.db.execute(
            """SELECT id, project_id, family, skill_name, approved_sha256
               FROM ladder_promotions WHERE id=?""",
            (int(promotion_id),),
        ).fetchone()
        if row is None:
            return False
        stamp = _memory().now_iso()
        try:
            with self._immediate_transaction():
                _memory().memory_spine.append_event(
                    self.db,
                    self._spine_key,
                    kind="ladder.withdrawn",
                    actor="runtime",
                    source="learning ladder",
                    scope="global",
                    permission="runtime",
                    outcome="applied",
                    payload={
                        "at": stamp,
                        "family": str(row["family"]),
                        "project_id": int(row["project_id"]),
                        "skill_name": str(row["skill_name"]),
                        "withdrawn_sha256": row["approved_sha256"],
                        "reason": "spine_unverified",
                    },
                    now=stamp,
                    subject_kind="ladder",
                    subject_id=int(promotion_id),
                )
        except (_memory().memory_spine.SpineError, _memory().sqlite3.Error):
            return False
        return True

    def _ladder_orphan_approval(
        self, skill_name: str
    ) -> sqlite3.Row | None:
        """The newest ``ladder.approved`` event for a name that still owes a
        withdrawal receipt (design 10.7 item 35).

        "Still owes" is the whole orphan condition, read from the spine: an
        approving event whose ``subject_id`` has **no promotion row at all**
        (the row was deleted -- the R-9 shape) and **no ``ladder.withdrawn``
        event** naming it.  The row is what a raw ``DELETE`` removes; the event
        is what it cannot.  ``ORDER BY id DESC`` so repeated parks flush the
        oldest-owed last, which lets the rare two-orphans-one-name case drain
        one call at a time instead of stranding the second forever.

        Returns the ``promotion_id`` (the event's ``subject_id``) plus the
        ``approved_sha256``, ``family`` and ``project_id`` its payload froze --
        everything a faithful receipt needs, none of it recoverable from a
        deleted row and the confirmation code deliberately not among it (S-1).
        """
        try:
            return self.db.execute(
                """SELECT e.subject_id AS promotion_id,
                          json_extract(e.payload_json, '$.approved_sha256')
                              AS approved_sha256,
                          json_extract(e.payload_json, '$.family') AS family,
                          json_extract(e.payload_json, '$.project_id')
                              AS project_id
                   FROM memory_spine_events AS e
                   WHERE e.kind='ladder.approved' AND e.subject_kind='ladder'
                     AND json_extract(e.payload_json, '$.skill_name')=?
                     AND NOT EXISTS (
                         SELECT 1 FROM ladder_promotions AS p
                         WHERE p.id = e.subject_id)
                     AND NOT EXISTS (
                         SELECT 1 FROM memory_spine_events AS w
                         WHERE w.kind='ladder.withdrawn'
                           AND w.subject_kind='ladder'
                           AND w.subject_id = e.subject_id)
                   ORDER BY e.id DESC LIMIT 1""",
                (str(skill_name),),
            ).fetchone()
        except _memory().sqlite3.DatabaseError:
            return None

    def _ladder_orphan_pending(
        self, project_id: int | None = None
    ) -> list[dict[str, Any]]:
        """Outstanding orphan withdrawals, derived from the spine alone.

        The durable half of item 35: once an orphan is parked, the live file
        is gone and no row exists, so nothing on the promotion side remembers
        that a ``ladder.withdrawn`` receipt is still owed -- which is holdout
        v2's shape one level along.  The spine does remember: an approving
        event with no promotion row and no matching withdrawn event is an
        outstanding receipt, and that survives a fresh ``Memory`` instance
        because it is read from ``memory_spine_events``, not from an in-memory
        queue.

        Whether the file is still live is NOT decided here -- this reader has
        no workspace.  A caller with a live set (``ladder_pending_withdrawals``
        given ``documents``/``workspace``; ``ladder_unverified_promotions`` via
        its ``listed`` skip) excludes a not-yet-parked orphan, which is
        reported as ``orphan_document`` from the live scan instead.
        """
        clauses = ["e.kind='ladder.approved'", "e.subject_kind='ladder'"]
        params: list[Any] = []
        if project_id is not None:
            clauses.append(
                "json_extract(e.payload_json, '$.project_id') = ?"
            )
            params.append(int(project_id))
        try:
            rows = self.db.execute(
                f"""SELECT e.subject_id AS promotion_id,
                           json_extract(e.payload_json, '$.project_id')
                               AS project_id,
                           json_extract(e.payload_json, '$.family') AS family,
                           json_extract(e.payload_json, '$.skill_name')
                               AS skill_name,
                           json_extract(e.payload_json, '$.approved_sha256')
                               AS approved_sha256,
                           MAX(e.created_at) AS approved_at
                    FROM memory_spine_events AS e
                    WHERE {' AND '.join(clauses)}
                      AND NOT EXISTS (
                          SELECT 1 FROM ladder_promotions AS p
                          WHERE p.id = e.subject_id)
                      AND NOT EXISTS (
                          SELECT 1 FROM memory_spine_events AS w
                          WHERE w.kind='ladder.withdrawn'
                            AND w.subject_kind='ladder'
                            AND w.subject_id = e.subject_id)
                    GROUP BY e.subject_id
                    ORDER BY e.subject_id""",
                params,
            ).fetchall()
        except _memory().sqlite3.DatabaseError:
            # A read that cannot run must not fail the turn (ruling 27).
            return []
        return [
            {
                "promotion_id": int(row["promotion_id"]),
                "project_id": (
                    None if row["project_id"] is None
                    else int(row["project_id"])
                ),
                "family": (
                    None if row["family"] is None else str(row["family"])
                ),
                "skill_name": str(row["skill_name"]),
                "reason": "orphan_parked",
                "deferred": True,
                "intended_reason": "orphan_parked",
                "approved_sha256": row["approved_sha256"],
                "withdrawn_at": str(row["approved_at"]),
            }
            for row in rows
            if row["skill_name"] is not None
        ]

    def _append_orphan_withdrawal(
        self,
        promotion_id: int,
        family: Any,
        project_id: int,
        skill_name: str,
        approved_sha256: Any,
    ) -> bool:
        """Append the ``ladder.withdrawn`` receipt for a parked orphan.

        Keyed to the ``promotion_id`` recovered from the approving event, so
        the receipt names the same subject the approval did and the pending
        derivation clears the moment it lands.  Returns True when it landed,
        False when the chain refused it (a broken head) -- in which case the
        fact stays derivable from the spine until a repaired store flushes it.
        Never raises: this is reached from the read-path sweep (ruling 27).
        """
        if not self._ladder_available() or not self._ladder_spine_head_ok():
            return False
        stamp = _memory().now_iso()
        try:
            with self._immediate_transaction():
                _memory().memory_spine.append_event(
                    self.db,
                    self._spine_key,
                    kind="ladder.withdrawn",
                    actor="runtime",
                    source="learning ladder",
                    scope="global",
                    permission="runtime",
                    outcome="applied",
                    payload={
                        "at": stamp,
                        "family": str(family),
                        "project_id": int(project_id),
                        "skill_name": str(skill_name),
                        "withdrawn_sha256": approved_sha256,
                        "reason": "orphan_parked",
                    },
                    now=stamp,
                    subject_kind="ladder",
                    subject_id=int(promotion_id),
                )
        except (_memory().memory_spine.SpineError, _memory().sqlite3.Error):
            return False
        return True

    def park_orphan_document(
        self,
        workspace: Path | None,
        *,
        project_id: int,
        skill_name: str,
    ) -> dict[str, Any]:
        """Park a live orphan document and receipt it (design 10.7 item 35).

        An *orphan* is a live learned document whose ``(project_id,
        skill_name)`` has ``ladder.*`` events but no active promotion row --
        the row was deleted (raw SQL, or a crash) while the file stayed live,
        so ``approved_skills`` excludes it but the file-based catalog
        (``list_available_skills`` / ``read_available_skill``) still serves it,
        around the ladder.  This moves the file out of the live root and writes
        the withdrawal receipt the vanished row can no longer carry.

        Fail-closed and never raises -- ladder-core calls this from the
        read-path sweep (ruling 27).  It **never** parks a name with no
        ``ladder.*`` events: a hand-authored or pre-M4 file that merely shares
        the name shape is guarded out by ``_ladder_named_in_spine`` and stays
        exactly where it is.

        Returns ``{"parked", "promotion_id", "receipt_deferred", "reason"}``:

        * ``parked`` -- the document is no longer live after this call;
        * ``promotion_id`` -- the id recovered from the approving event, or
          ``None`` on a refusal;
        * ``receipt_deferred`` -- the ``ladder.withdrawn`` append was refused
          (a broken head) and the outstanding fact is left derivable from the
          spine; the durable receipt lands on a later call once the chain
          verifies;
        * ``reason`` -- ``orphan_parked`` on success, ``spine_unverified``
          when the receipt deferred, else a refusal code.
        """
        self._ensure_open()
        name = str(skill_name)

        def refuse(reason: str) -> dict[str, Any]:
            return {
                "parked": False,
                "promotion_id": None,
                "receipt_deferred": False,
                "reason": reason,
            }

        if not self._ladder_ready:
            return refuse("ladder_unavailable")
        try:
            # The load-bearing guard: only a name the spine has actually seen
            # may be parked.  A file with no ladder.* events is not an orphan;
            # it is a hand-authored or pre-M4 document, and parking it would be
            # data loss for something the ladder never governed.
            if name not in self._ladder_named_in_spine():
                return refuse("not_ladder_touched")
            approval = self._ladder_orphan_approval(name)
            if approval is None:
                # No approving event that still owes a receipt: either the row
                # is intact (not an orphan) or the receipt is already written.
                return refuse("no_orphan")
            promotion_id = int(approval["promotion_id"])
            family = approval["family"]
            approved_sha256 = approval["approved_sha256"]
            event_project = (
                int(project_id) if approval["project_id"] is None
                else int(approval["project_id"])
            )
            # Park the live file, if it is still live.  A re-call after a
            # deferred receipt finds nothing to park and only re-attempts the
            # receipt, which is the flush path a repaired store needs.
            parked_file = False
            try:
                live = (
                    None if workspace is None
                    else self._ladder_live_documents(workspace).get(name)
                )
            except (OSError, ValueError, _memory().sqlite3.DatabaseError):
                live = None
            if live is not None:
                try:
                    _memory().skill_library.withdraw_learned_skill(_memory().Path(workspace), name)
                    _memory()._ladder_clear_catalog_cache()
                    parked_file = True
                except (OSError, ValueError, PermissionError):
                    # Could not move the file; it is still served, so this is
                    # not a park.  The next sweep retries.
                    return refuse("park_failed")
            landed = self._append_orphan_withdrawal(
                promotion_id, family, event_project, name, approved_sha256
            )
            receipt_deferred = not landed
            if receipt_deferred:
                # The in-turn courtesy record; the durable, cross-instance fact
                # is the spine derivation in ``_ladder_orphan_pending``.
                self._record_degraded_write(
                    "ladder.orphan_parked",
                    f"orphan {name} parked; withdrawal receipt deferred",
                    reason="spine_unverified",
                )
            return {
                "parked": bool(parked_file or live is None),
                "promotion_id": promotion_id,
                "receipt_deferred": receipt_deferred,
                "reason": (
                    "spine_unverified" if receipt_deferred else "orphan_parked"
                ),
            }
        except (_memory().sqlite3.DatabaseError, RuntimeError, OSError, ValueError):
            # Ruling 27: a refusal, never an exception, on the read path.
            return refuse("read_failed")

    def _ladder_workspace_matches(
        self, project_id: int, workspace: Path | None
    ) -> bool:
        """The workspace check ``Memory`` can actually make (S-5).

        ``memory.py`` imports no config module and
        ``config.resolve_project_workspace`` takes a canonical relative path
        rather than a project id, so the real derivation happens at the caller
        -- the CLI, the governed verb, the agent -- and what is checked here
        is the half the store can see on its own: for
        ``"@projects/<slug>"`` the workspace directory must be named
        ``<slug>``, and for ``"."`` the caller's declared workspace is taken
        as given.  Both layers refuse with the same name,
        ``workspace_mismatch``, so an operator sees one reason and not two.
        """
        if workspace is None:
            return False
        project = self.get_project(int(project_id))
        if project is None:
            return False
        relative = str(project.get("relative_path") or ".").strip()
        if relative in {"", "."}:
            return True
        slug = _memory().PurePosixPath(relative.replace("\\", "/")).name
        return bool(slug) and _memory().Path(workspace).name == slug

    def _ladder_refuse(
        self,
        action: str,
        reason: str,
        *,
        family: str,
        project_id: int | None = None,
        log: bool = True,
        **extra: Any,
    ) -> dict[str, Any]:
        """Every refusal is a returned dict with a reason, never a raise.

        Design 3.4: the old distiller call site swallowed exceptions, so a
        refusal that raised would be lost exactly where it matters.  The
        refusal is also written to the receipt path so the epoch counters have
        a durable source and an operator can read the history directly.
        """
        if log:
            self._log_ladder_refusal(
                action, family=family, project_id=project_id, reason=reason
            )
        refusal = {
            "staged" if action == "stage" else "applied": False,
            "reason": str(reason),
            "family": str(family),
        }
        if project_id is not None:
            refusal["project_id"] = int(project_id)
        refusal.update(extra)
        return refusal

    def ladder_candidates(
        self,
        *,
        project_id: int | None = None,
        family: str | None = None,
        workspace: Path | None = None,
    ) -> list[dict[str, Any]]:
        """Every ``(project, family)`` whose proof, gate and ledger currently
        justify a staging, with nothing staged for it yet.

        The consolidation worker calls this each pass and stages what
        qualifies.  No queue and no new table are needed because the proof
        lives entirely in the store, which is also why removing the
        agent-side distiller call loses nothing.

        ``family`` and ``project_id`` narrow the scan.  ``run_ladder_pass``
        iterates the ten ladder families and passes both, so the pass costs
        ten filtered derivations rather than ten full ones -- the proof
        derivation is the expensive part and doing it for nine families you
        are about to discard is nine wasted derivations per pass.
        ``workspace`` is accepted and unused: this method reads no file, and
        taking it keeps the driver from having to introspect the signature.

        Every row is a mapping carrying at least ``family`` and
        ``project_id``, which the driver relies on to attribute a candidate to
        the right family rather than to the loop it was found in.
        """
        self._require_ladder()
        del workspace  # read no file; see the docstring
        if family is not None and family not in self.PREDICTION_FAMILIES:
            raise ValueError(f"Unknown task family: {family}")
        projects = (
            [int(project_id)] if project_id is not None
            else [int(row["id"]) for row in self.db.execute(
                "SELECT id FROM agent_projects WHERE enabled=1 ORDER BY id"
            )]
        )
        families = (
            sorted(_memory().learning_ladder.LADDER_FAMILIES) if family is None
            else ([family] if family in _memory().learning_ladder.LADDER_FAMILIES else [])
        )
        candidates: list[dict[str, Any]] = []
        for pid in projects:
            for family in families:
                gate = self.calibration_gate(
                    family, **_memory().learning_ladder.LADDER_GATE_THRESHOLDS
                )
                if not bool(gate["allowed"]):
                    continue
                epochs = self.calibration_ledger(family)
                if not epochs:
                    continue
                verdict = self.calibration_ledger_monotonicity(family)
                if bool(verdict["currently_regressed"]):
                    continue
                proof = self.ladder_proof(family=family, project_id=pid)
                if proof["reason"] is not None:
                    continue
                name = _memory().learning_ladder.auto_skill_name(family)
                if self.ladder_promotions(
                    project_id=pid, skill_name=name, stages=("staged",)
                ):
                    continue
                candidates.append({
                    "project_id": pid,
                    "family": family,
                    "skill_name": name,
                    "reuses": int(proof["reuses"]),
                    "contexts": int(proof["contexts"]),
                    "epoch": int(epochs[-1]["epoch"]),
                    "proof_sha256": str(proof["sha256"]),
                })
        return candidates

    def stage_ladder_promotion(
        self,
        *,
        family: str,
        project_id: int,
        workspace: Path,
        now: str | None = None,
        actor: str = "runtime",
        conversation_id: int | None = None,
        permission: str = "runtime",
    ) -> dict[str, Any]:
        """Derive the proof, refuse or stage.  Never touches the live root.

        Always returns a dict; never raises for a refusal.  Refusal reasons
        (closed set): ``family_unsupported``, ``family_excluded``,
        ``gate_closed``, ``ledger_regressed``, ``no_epoch``,
        ``no_eligible_lesson``, ``insufficient_reuse``,
        ``insufficient_effectiveness``, ``screened_component``,
        ``document_unchanged``, ``staging_exists``, ``workspace_mismatch``,
        ``spine_unavailable``, ``staging_write_failed``.

        The staged bytes are written **outside** the database transaction and
        the database is the record: if the write fails the row is moved to
        ``withdrawn`` with ``staging_write_failed`` in a second transaction,
        and ``ladder verify`` reconciles the filesystem to the record rather
        than the other way round.  A filesystem operation inside a write
        transaction would hold the lock for as long as the disk takes.
        """
        stamp = str(now or _memory().now_iso())
        if not self._ladder_available():
            return self._ladder_refuse(
                "stage", "spine_unavailable", family=str(family),
                project_id=project_id, log=False,
            )
        if family not in self.PREDICTION_FAMILIES:
            return self._ladder_refuse(
                "stage", "family_unsupported", family=str(family),
                project_id=project_id,
            )
        if family in _memory().learning_ladder.LADDER_EXCLUDED_FAMILIES:
            # The conversation family's predictions carry evidence_ok NULL and
            # calibration_gate skips the evidence clause entirely when nothing
            # is applicable, so a promotion there would rest on no
            # verification at all (M-2).
            return self._ladder_refuse(
                "stage", "family_excluded", family=str(family),
                project_id=project_id,
            )
        if not self._ladder_workspace_matches(project_id, workspace):
            return self._ladder_refuse(
                "stage", "workspace_mismatch", family=str(family),
                project_id=project_id,
            )
        gate = self.calibration_gate(
            family, **_memory().learning_ladder.LADDER_GATE_THRESHOLDS
        )
        if not bool(gate["allowed"]):
            return self._ladder_refuse(
                "stage", "gate_closed", family=str(family),
                project_id=project_id, reasons=list(gate["reasons"]),
            )
        epochs = self.calibration_ledger(family)
        if not epochs:
            return self._ladder_refuse(
                "stage", "no_epoch", family=str(family), project_id=project_id
            )
        verdict = self.calibration_ledger_monotonicity(family)
        if bool(verdict["currently_regressed"]):
            return self._ladder_refuse(
                "stage", "ledger_regressed", family=str(family),
                project_id=project_id,
            )
        proof = self.ladder_proof(
            family=family, project_id=int(project_id), now=stamp
        )
        if proof["reason"] is not None:
            return self._ladder_refuse(
                "stage", str(proof["reason"]), family=str(family),
                project_id=project_id,
            )
        try:
            content = _memory().learning_ladder.build_staged_document(
                family=str(family),
                reuses=int(proof["reuses"]),
                contexts=int(proof["contexts"]),
                tool_names=list(proof["tool_names"]),
                oracles=list(proof["oracles"]),
                gate=gate,
                epoch=int(epochs[-1]["epoch"]),
                monotone=bool(verdict["monotone"]),
                lift_pp=verdict.get("lift_pp"),
            )
        except _memory().learning_ladder.ScreenedComponent as exc:
            return self._ladder_refuse(
                "stage", "screened_component", family=str(family),
                project_id=project_id, component=str(exc.component),
            )
        except ValueError as exc:
            message = str(exc)
            if message.startswith("screened_component"):
                return self._ladder_refuse(
                    "stage", "screened_component", family=str(family),
                    project_id=project_id,
                    component=message.split(":", 1)[-1].strip(),
                )
            raise
        name = _memory().learning_ladder.auto_skill_name(family)
        if self.ladder_promotions(
            project_id=int(project_id), skill_name=name, stages=("staged",)
        ):
            return self._ladder_refuse(
                "stage", "staging_exists", family=str(family),
                project_id=project_id,
            )
        live = self._ladder_live_documents(workspace).get(name)
        prior_sha256 = None if live is None else str(live["sha256"])
        # The staged bytes are written BEFORE the row, and the row records the
        # digest the writer produced rather than one this method computed.
        # ``skill_library`` renders a description and front matter around the
        # body, so a digest taken over ``content`` here would not be the
        # digest of the file that ``apply_ladder_promotion`` later re-reads --
        # and the approval would refuse ``staged_digest_mismatch`` on a
        # perfectly good staging.  Recomputing the rendering in this module to
        # avoid that would couple the store to a document format that is
        # ladder-core's to change.
        #
        # The order is safe in both crash directions, unlike approval, where
        # it is forced.  A staged file with no row is unreachable -- the
        # catalog does not walk the staging root and the model's file tools
        # refuse the whole directory -- and ``ladder verify`` discards it; a
        # row with no staged file refuses ``staged_missing`` at approval and
        # is reconciled the same way.  Neither is an unverified promotion,
        # which is the thing that forces approval's order.
        try:
            written = _memory().skill_library.stage_learned_skill(
                _memory().Path(workspace),
                name,
                _memory().learning_ladder.staged_skill_description(str(family)),
                content,
                family=str(family),
                verified_outcomes=int(proof["reuses"]),
            )
        except (OSError, ValueError, PermissionError) as exc:
            return self._ladder_refuse(
                "stage", "staging_write_failed", family=str(family),
                project_id=project_id, detail=type(exc).__name__,
            )
        staged_sha256 = str(written["sha256"])

        def _discard_staged() -> None:
            try:
                _memory().skill_library.discard_staged_skill(_memory().Path(workspace), name)
            except (KeyError, OSError, ValueError, PermissionError):
                pass

        if (
            prior_sha256 == staged_sha256
            and self.ladder_promotions(
                project_id=int(project_id), skill_name=name,
                stages=("approved",),
            )
        ):
            _discard_staged()
            return self._ladder_refuse(
                "stage", "document_unchanged", family=str(family),
                project_id=project_id,
            )
        token = _memory().secrets.token_urlsafe(12)
        try:
            transaction = self._immediate_transaction()
        except BaseException:  # pragma: no cover - defensive
            _discard_staged()
            raise
        with transaction:
            promotion_id = _memory().allocate_ladder_id(self.db)
            payload_common = {
                "at": stamp,
                "family": str(family),
                "project_id": int(project_id),
                "skill_name": name,
            }
            candidate_event = _memory().memory_spine.append_event(
                self.db,
                self._spine_key,
                kind="ladder.candidate",
                actor=actor,
                source="learning ladder",
                scope="global",
                permission=permission,
                outcome="applied",
                payload={
                    **payload_common,
                    "lesson_ids": [int(value) for value in proof["lesson_ids"]][:10],
                    "reuse_count": int(proof["reuses"]),
                    "context_count": int(proof["contexts"]),
                    "proof_sha256": str(proof["sha256"]),
                    "epoch": int(epochs[-1]["epoch"]),
                    "gate_allowed": True,
                    "ledger_monotone": bool(verdict["monotone"]),
                    "brier": gate.get("brier"),
                    "calibration_error": gate.get("calibration_error"),
                    "attempts": int(gate.get("attempts") or 0),
                },
                now=stamp,
                conversation_id=conversation_id,
                subject_kind="ladder",
                subject_id=promotion_id,
            )
            _memory().memory_spine.append_event(
                self.db,
                self._spine_key,
                kind="ladder.staged",
                actor=actor,
                source="learning ladder",
                scope="global",
                permission=permission,
                outcome="applied",
                payload={
                    **payload_common,
                    "staged_sha256": staged_sha256,
                    "prior_sha256": prior_sha256,
                    "verified_outcomes": int(proof["reuses"]),
                    "tools_count": len(proof["tool_names"]),
                    "oracles_count": len(proof["oracles"]),
                    # The boolean and nothing else: the confirmation code is
                    # never published, not even as a digest (S-1).
                    "token_required": True,
                },
                now=stamp,
                conversation_id=conversation_id,
                subject_kind="ladder",
                subject_id=promotion_id,
            )
            self.db.execute(
                """INSERT INTO ladder_promotions(
                       id, created_at, updated_at, project_id, family,
                       skill_name, stage, stage_reason, lesson_ids_json,
                       proof_json, proof_sha256, reuse_count, context_count,
                       epoch_id, gate_json, staged_sha256, approval_token,
                       prior_sha256, spine_event_id
                   ) VALUES (?, ?, ?, ?, ?, ?, 'staged', NULL, ?, ?, ?, ?, ?,
                             ?, ?, ?, ?, ?, ?)""",
                (
                    promotion_id, stamp, stamp, int(project_id), str(family),
                    name,
                    _memory().memory_spine.canonical(
                        [int(value) for value in proof["lesson_ids"]]
                    ),
                    _memory().memory_spine.canonical(_memory()._ladder_proof_record(proof)),
                    str(proof["sha256"]), int(proof["reuses"]),
                    int(proof["contexts"]), int(epochs[-1]["id"]),
                    _memory().memory_spine.canonical(gate), staged_sha256, token,
                    prior_sha256, int(candidate_event),
                ),
            )
        return {
            "staged": True,
            "promotion_id": int(promotion_id),
            "project_id": int(project_id),
            "family": str(family),
            "skill_name": name,
            "staged_sha256": staged_sha256,
            "prior_sha256": prior_sha256,
            "reuse_count": int(proof["reuses"]),
            "context_count": int(proof["contexts"]),
            "epoch": int(epochs[-1]["epoch"]),
            # Returned once to the caller.  The worker hands it to the
            # operator surfaces; it is in no spine payload and no log line.
            "approval_token": token,
        }

    def apply_ladder_promotion(
        self,
        promotion_id: int,
        *,
        approval_token: str,
        workspace: Path,
        actor: str = "operator",
        conversation_id: int | None = None,
        permission: str = "operator:interactive",
        operator_prompt: str | None = None,
    ) -> dict[str, Any]:
        """The only path to a live learned skill.  Operator-typed only.

        Every gate is re-derived here and none is read from the row: the
        proof, the calibrated gate, the ledger, the staged digest, the live
        digest and the document's components.  ``operator_prompt``, when the
        caller supplies one with a ``conversation_id``, is written to
        ``messages`` **verbatim** inside the approving transaction, exactly as
        the shipped governed verbs write their raw turn -- and **redaction is
        the caller's responsibility**: the confirmation code must already have
        been replaced before it reaches this method, because a transcript is
        replayed into later prompts and the code being single-use does not
        remove it from one.  ``memory.py`` performs no redaction and never
        learns the verb's grammar.

        Refusals (closed set, each a fixed reason and no state change):
        ``missing``, ``not_staged``, ``token_mismatch``, ``proof_stale``,
        ``gate_closed``, ``ledger_regressed``, ``staged_missing``,
        ``staged_digest_mismatch``, ``live_digest_unexpected``,
        ``screened_component``, ``workspace_mismatch``, ``spine_unavailable``.
        """
        if actor != "operator":
            # Structural, not conventional: a verifier can then assert that
            # every ladder.approved event in the chain carries actor=operator.
            raise ValueError("skill promotions are approved by the operator only")
        if not self._ladder_available():
            return {"applied": False, "reason": "spine_unavailable"}
        row = self.db.execute(
            "SELECT * FROM ladder_promotions WHERE id=?", (int(promotion_id),)
        ).fetchone()
        if row is None:
            return {"applied": False, "reason": "missing"}
        family = str(row["family"])
        name = str(row["skill_name"])
        project_id = int(row["project_id"])

        def refuse(reason: str, **extra: Any) -> dict[str, Any]:
            return self._ladder_refuse(
                "approve", reason, family=family, project_id=project_id,
                promotion_id=int(promotion_id), **extra,
            )

        # 1. staged, which is also what makes the code single use.
        if str(row["stage"]) != "staged":
            return refuse("not_staged", stage=str(row["stage"]))
        # 2. the shape first, THEN a constant-time compare against the
        #    cleartext code on the row.  ``hmac.compare_digest`` raises
        #    ``TypeError`` on a non-ASCII ``str`` operand, so a fullwidth,
        #    en-dashed, Cyrillic or zero-width-joined code reached the
        #    operator as a traceback out of the CLI instead of a refusal
        #    (R-4).  The alphabet is the one the column CHECK already names.
        if not _memory()._LADDER_APPROVAL_CODE_RE.match(str(approval_token)):
            return refuse("token_malformed")
        if not _memory().hmac.compare_digest(
            str(approval_token), str(row["approval_token"])
        ):
            return refuse("token_mismatch")
        # 3. the workspace the caller derived is this project's.
        if not self._ladder_workspace_matches(project_id, workspace):
            return refuse("workspace_mismatch")
        # 4. the proof re-derives to the same digest NOW, over exactly the
        #    application set this row was proved on (ruling 16).
        proof = self.ladder_proof(
            family=family, project_id=project_id,
            application_ids=self._ladder_recorded_application_ids(row),
        )
        if str(proof["reason"] or "") == "proof_unbacked":
            return refuse("proof_unbacked")
        if proof["reason"] is not None or not _memory().hmac.compare_digest(
            str(proof["sha256"]), str(row["proof_sha256"])
        ):
            return refuse("proof_stale")
        # 5. the gate is still open.
        gate = self.calibration_gate(
            family, **_memory().learning_ladder.LADDER_GATE_THRESHOLDS
        )
        if not bool(gate["allowed"]):
            return refuse("gate_closed", reasons=list(gate["reasons"]))
        # 6. the newest sealed epoch has not regressed.
        verdict = self.calibration_ledger_monotonicity(family)
        if bool(verdict["currently_regressed"]):
            return refuse("ledger_regressed")
        # 7. the staged file is there and is the document that was measured.
        try:
            staged = _memory().skill_library.read_staged_skill(name, _memory().Path(workspace))
        except (KeyError, OSError, ValueError, PermissionError):
            return refuse("staged_missing")
        if str(staged.get("sha256") or "") != str(row["staged_sha256"]):
            return refuse("staged_digest_mismatch")
        # 8. nothing changed the live document behind the ladder's back.
        live = self._ladder_live_documents(workspace).get(name)
        live_sha256 = None if live is None else str(live["sha256"])
        if live_sha256 != (
            None if row["prior_sha256"] is None else str(row["prior_sha256"])
        ):
            return refuse("live_digest_unexpected")
        # 9. the document's components re-screen.
        for component in (
            family, *_memory()._ladder_document_components(staged)
        ):
            if _memory().contains_secret(component) or _memory().screen_endpoint(component)[0]:
                return refuse("screened_component")

        promoted = _memory().skill_library.promote_staged_skill(
            _memory().Path(workspace), name, expected_staged_sha256=str(row["staged_sha256"])
        )
        stamp = _memory().now_iso()
        approved_sha256 = str(promoted["approved_sha256"])
        prior_document = promoted.get("prior_document")
        prior_sha256 = promoted.get("prior_sha256")
        try:
            return self._apply_ladder_promotion_committed(
                row, proof, verdict, stamp, approved_sha256, prior_document,
                prior_sha256,
                conversation_id=conversation_id, permission=permission,
                operator_prompt=operator_prompt,
            )
        except _memory().sqlite3.IntegrityError as exc:
            # A constraint on this path is a refusal at worst: the bytes are
            # already live, so raising would leave the operator with a moved
            # document, no row, and a traceback.
            return refuse("row_conflict", detail=str(exc)[:120])

    def _apply_ladder_promotion_committed(
        self,
        row: Mapping[str, Any],
        proof: Mapping[str, Any],
        verdict: Mapping[str, Any],
        stamp: str,
        approved_sha256: str,
        prior_document: Any,
        prior_sha256: Any,
        *,
        conversation_id: int | None,
        permission: str,
        operator_prompt: str | None,
    ) -> dict[str, Any]:
        promotion_id = int(row["id"])
        project_id = int(row["project_id"])
        family = str(row["family"])
        name = str(row["skill_name"])
        with self._immediate_transaction():
            # Retire whatever currently holds the live slot for this
            # (project, skill) -- an approved row just as much as a legacy one
            # -- in the same transaction, so ``idx_ladder_promotions_one_live``
            # never sees two.  Retiring only the legacy case meant the SECOND
            # approval of a family raised ``sqlite3.IntegrityError`` out of the
            # operator's turn, while design 1.4 (only the newest approval keeps
            # ``prior_document``) and design 3.6 (``not_newest``) both assume
            # successive approvals are ordinary.
            #
            # ``withdrawn`` + ``superseded_by_approval`` is the terminal stage,
            # not a new ``superseded`` one: the stored stage set is the six of
            # the DDL ``CHECK`` (design 3.1), and the retired row KEEPS its
            # ``approved_sha256`` and ``approved_at`` because those record what
            # was live and when.
            superseded = self.db.execute(
                """SELECT id FROM ladder_promotions
                   WHERE project_id=? AND skill_name=? AND id<>?
                     AND stage IN ('approved','unapproved_legacy')
                   ORDER BY id DESC""",
                (project_id, name, int(promotion_id)),
            ).fetchall()
            for retired in superseded:
                self.db.execute(
                    """UPDATE ladder_promotions
                       SET stage='withdrawn', stage_reason='superseded_by_approval',
                           updated_at=?
                       WHERE id=?""",
                    (stamp, int(retired["id"])),
                )
            legacy = superseded[0] if superseded else None
            _memory().memory_spine.append_event(
                self.db,
                self._spine_key,
                kind="ladder.approved",
                actor="operator",
                source="operator approval",
                scope="global",
                permission=permission,
                outcome="applied",
                payload={
                    "at": stamp,
                    "family": family,
                    "project_id": project_id,
                    "skill_name": name,
                    "approved_sha256": approved_sha256,
                    "prior_sha256": (
                        None if prior_sha256 is None else str(prior_sha256)
                    ),
                    "proof_sha256": str(proof["sha256"]),
                    "epoch": int(
                        self.calibration_ledger(family)[-1]["epoch"]
                    ),
                    "gate_allowed": True,
                    "ledger_monotone": bool(verdict["monotone"]),
                    # Named only once ladder-core's contract admits it, so
                    # this lands green either way and starts carrying the
                    # retired id the moment the key set grows.
                    **(
                        {"superseded_promotion_id": (
                            None if legacy is None else int(legacy["id"])
                        )}
                        if _memory()._ladder_payload_admits(
                            "ladder.approved", "superseded_promotion_id"
                        ) else {}
                    ),
                },
                now=stamp,
                conversation_id=conversation_id,
                subject_kind="ladder",
                subject_id=int(promotion_id),
            )
            self.db.execute(
                """UPDATE ladder_promotions
                   SET stage='approved', stage_reason=NULL, updated_at=?,
                       approved_sha256=?, approved_at=?, prior_sha256=?,
                       prior_document=?
                   WHERE id=?""",
                (
                    stamp, approved_sha256, stamp,
                    None if prior_sha256 is None else str(prior_sha256),
                    prior_document, int(promotion_id),
                ),
            )
            # Only the newest approved promotion per skill name keeps its
            # prior bytes; rollback is exactly one step, so nothing it needs
            # is pruned (LADDER_PRIOR_DOCUMENT_RETAINED = 1).
            self.db.execute(
                """UPDATE ladder_promotions
                   SET prior_document=NULL, prior_document_pruned=1
                   WHERE project_id=? AND skill_name=? AND id<>?
                     AND prior_document IS NOT NULL""",
                (project_id, name, int(promotion_id)),
            )
            if conversation_id is not None and operator_prompt:
                self.db.execute(
                    """INSERT INTO messages(conversation_id, created_at, role, content)
                       VALUES (?, ?, 'user', ?)""",
                    (int(conversation_id), stamp, str(operator_prompt)),
                )
        return {
            "applied": True,
            "promotion_id": int(promotion_id),
            "project_id": project_id,
            "family": family,
            "skill_name": name,
            "approved_sha256": approved_sha256,
            "prior_sha256": None if prior_sha256 is None else str(prior_sha256),
            "had_prior_document": prior_document is not None,
            "retired_legacy": legacy is not None,
        }

    def rollback_ladder_promotion(
        self,
        promotion_id: int,
        *,
        workspace: Path,
        actor: str = "operator",
        conversation_id: int | None = None,
        permission: str = "operator:interactive",
        reason: str = "operator_rollback",
        operator_prompt: str | None = None,
    ) -> dict[str, Any]:
        """Restore the exact bytes the promotion replaced, or remove the
        document.  Operator-typed only, and exactly one step.

        Deliberately **exempt from ``ledger_regressed``**: a regressed family
        must always be able to undo, or a store can trap itself with an
        artefact it can neither verify nor withdraw (S-4).

        Refusals: ``missing``, ``not_approved``, ``not_newest``, ``pruned``,
        ``live_digest_unexpected``, ``workspace_mismatch``,
        ``spine_unavailable``.
        """
        if actor != "operator":
            raise ValueError("skill promotions are rolled back by the operator only")
        if not self._ladder_available():
            return {"rolled_back": False, "reason": "spine_unavailable"}
        row = self.db.execute(
            "SELECT * FROM ladder_promotions WHERE id=?", (int(promotion_id),)
        ).fetchone()
        if row is None:
            return {"rolled_back": False, "reason": "missing"}
        family = str(row["family"])
        name = str(row["skill_name"])
        project_id = int(row["project_id"])

        def refuse(code: str, **extra: Any) -> dict[str, Any]:
            self._log_ladder_refusal(
                "rollback", family=family, project_id=project_id, reason=code
            )
            return {
                "rolled_back": False, "reason": code,
                "promotion_id": int(promotion_id), "family": family, **extra,
            }

        if str(row["stage"]) not in _memory().LADDER_LIVE_STAGES:
            if str(row["stage_reason"] or "") == "superseded_by_approval":
                # A row a later approval retired is not "not approved" -- it
                # WAS approved, and the actionable fact is which promotion
                # took its place.  Design 3.6's one-step rule then reads
                # correctly: roll back the newer one first.
                newer = self.db.execute(
                    """SELECT id FROM ladder_promotions
                       WHERE project_id=? AND skill_name=?
                         AND stage IN ('approved','unapproved_legacy')
                       ORDER BY id DESC LIMIT 1""",
                    (project_id, name),
                ).fetchone()
                return refuse(
                    "not_newest",
                    newest_promotion_id=None if newer is None else int(newer["id"]),
                )
            return refuse("not_approved", stage=str(row["stage"]))
        if not self._ladder_workspace_matches(project_id, workspace):
            return refuse("workspace_mismatch")
        newest = self.db.execute(
            """SELECT id FROM ladder_promotions
               WHERE project_id=? AND skill_name=? AND stage IN ('approved','unapproved_legacy')
               ORDER BY id DESC LIMIT 1""",
            (project_id, name),
        ).fetchone()
        if newest is None or int(newest["id"]) != int(promotion_id):
            return refuse(
                "not_newest",
                newest_promotion_id=None if newest is None else int(newest["id"]),
            )
        if bool(int(row["prior_document_pruned"] or 0)):
            # A corrupted store fails closed rather than silently doing
            # nothing: pruned means the bytes existed and are gone.
            return refuse("pruned")
        live = self._ladder_live_documents(workspace).get(name)
        live_sha256 = None if live is None else str(live["sha256"])
        if live_sha256 != str(row["approved_sha256"] or ""):
            return refuse("live_digest_unexpected")
        prior_document = row["prior_document"]
        restored = _memory().skill_library.restore_learned_skill(
            _memory().Path(workspace), name,
            None if prior_document is None else bytes(prior_document),
        )
        _memory()._ladder_clear_catalog_cache()
        stamp = _memory().now_iso()
        # Design 10.7 item 32(B).  Approval retires whatever held the live
        # slot; rolling that approval back has to undo the retirement too, or
        # the older document is restored on disk with NO row at ``approved``
        # -- ``approved_skills`` then serves nothing and the restored file is
        # an orphan.  The second-approval fix created this: retiring the older
        # row was right, and nothing put it back.
        #
        # Only reinstated when the bytes actually on disk are the ones that
        # row approved.  A digest that does not match means the restored
        # document is not what the older promotion vouched for, and
        # reinstating it would make the row claim a document it never
        # approved -- the exact lie the whole ladder exists to prevent.
        reinstated = self._ladder_reinstatement_candidate(
            project_id, name, int(promotion_id), prior_document
        )
        with self._immediate_transaction():
            _memory().memory_spine.append_event(
                self.db,
                self._spine_key,
                kind="ladder.rolled_back",
                actor="operator",
                source="operator rollback",
                scope="global",
                permission=permission,
                outcome="applied",
                payload={
                    "at": stamp,
                    "family": family,
                    "project_id": project_id,
                    "skill_name": name,
                    "restored_sha256": (
                        None if row["prior_sha256"] is None
                        else str(row["prior_sha256"])
                    ),
                    "removed_sha256": str(row["approved_sha256"] or ""),
                    "reason": str(reason),
                    **(
                        {"reinstated_promotion_id": (
                            None if reinstated is None else int(reinstated["id"])
                        )}
                        if _memory()._ladder_payload_admits(
                            "ladder.rolled_back", "reinstated_promotion_id"
                        ) else {}
                    ),
                },
                now=stamp,
                conversation_id=conversation_id,
                subject_kind="ladder",
                subject_id=int(promotion_id),
            )
            self.db.execute(
                """UPDATE ladder_promotions
                   SET stage='rolled_back', stage_reason=?, updated_at=?
                   WHERE id=?""",
                (str(reason), stamp, int(promotion_id)),
            )
            if reinstated is not None:
                # withdrawn -> approved, in the same transaction as the
                # rollback, so the live slot is never empty and never doubly
                # occupied: ``idx_ladder_promotions_one_live`` sees the newer
                # row leave before the older one returns.
                self.db.execute(
                    """UPDATE ladder_promotions
                       SET stage='approved', stage_reason=NULL, updated_at=?
                       WHERE id=?""",
                    (stamp, int(reinstated["id"])),
                )
            if conversation_id is not None and operator_prompt:
                self.db.execute(
                    """INSERT INTO messages(conversation_id, created_at, role, content)
                       VALUES (?, ?, 'user', ?)""",
                    (int(conversation_id), stamp, str(operator_prompt)),
                )
        return {
            "rolled_back": True,
            "promotion_id": int(promotion_id),
            "family": family,
            "skill_name": name,
            "restored": bool(restored.get("restored", prior_document is not None)),
            "removed": prior_document is None,
            "reinstated_promotion_id": (
                None if reinstated is None else int(reinstated["id"])
            ),
        }

    def _ladder_reinstatement_candidate(
        self,
        project_id: int,
        skill_name: str,
        promotion_id: int,
        prior_document: Any,
    ) -> Mapping[str, Any] | None:
        """The retired row a rollback should put back, or ``None``.

        The newest row this promotion superseded, and only when the bytes just
        restored are the ones it approved.  ``prior_document`` of the row
        being rolled back IS the older document -- ``promote_staged_skill``
        captured it -- so the check is a digest comparison and needs no second
        filesystem read.

        Returns ``None`` when nothing was superseded (the ordinary first-level
        rollback), when the restored document is absent, or when the digests
        disagree.  All three are the fail-closed direction: an artefact that
        cannot be shown to be the one a row approved is not served.
        """
        if prior_document is None:
            return None
        candidate = self.db.execute(
            """SELECT id, approved_sha256 FROM ladder_promotions
               WHERE project_id=? AND skill_name=? AND id<>?
                 AND stage='withdrawn'
                 AND stage_reason='superseded_by_approval'
               ORDER BY id DESC LIMIT 1""",
            (int(project_id), str(skill_name), int(promotion_id)),
        ).fetchone()
        if candidate is None:
            return None
        restored_digest = _memory().hashlib.sha256(bytes(prior_document)).hexdigest()
        if not _memory().hmac.compare_digest(
            restored_digest, str(candidate["approved_sha256"] or "")
        ):
            return None
        return candidate

    def discard_ladder_promotion(
        self,
        promotion_id: int,
        *,
        workspace: Path,
        actor: str = "operator",
        conversation_id: int | None = None,
        permission: str = "operator:interactive",
        operator_prompt: str | None = None,
    ) -> dict[str, Any]:
        """Throw a staged document away.  Refuses anything not staged."""
        if actor != "operator":
            # Validated like approve and rollback (R-8).  Discarding is not a
            # promotion, but it destroys an operator's candidate, and the
            # ternary that used to stand in for this guard resolved to the
            # same value on both branches.
            raise ValueError(
                "staged skill promotions are discarded by the operator only"
            )
        if not self._ladder_available():
            return {"discarded": False, "reason": "spine_unavailable"}
        row = self.db.execute(
            "SELECT * FROM ladder_promotions WHERE id=?", (int(promotion_id),)
        ).fetchone()
        if row is None:
            return {"discarded": False, "reason": "missing"}
        if str(row["stage"]) != "staged":
            return {
                "discarded": False, "reason": "not_staged",
                "promotion_id": int(promotion_id), "stage": str(row["stage"]),
            }
        if not self._ladder_workspace_matches(int(row["project_id"]), workspace):
            return {
                "discarded": False, "reason": "workspace_mismatch",
                "promotion_id": int(promotion_id),
            }
        name = str(row["skill_name"])
        try:
            _memory().skill_library.discard_staged_skill(_memory().Path(workspace), name)
        except (KeyError, OSError, ValueError, PermissionError):
            # The record is authoritative; a missing staged file is exactly
            # what `ladder verify` reconciles.
            pass
        stamp = _memory().now_iso()
        with self._immediate_transaction():
            _memory().memory_spine.append_event(
                self.db,
                self._spine_key,
                kind="ladder.withdrawn",
                actor="runtime",
                source="operator discard",
                scope="global",
                permission=permission,
                outcome="applied",
                payload={
                    "at": stamp,
                    "family": str(row["family"]),
                    "project_id": int(row["project_id"]),
                    "skill_name": name,
                    "withdrawn_sha256": str(row["staged_sha256"]),
                    "reason": "operator_discard",
                },
                now=stamp,
                conversation_id=conversation_id,
                subject_kind="ladder",
                subject_id=int(promotion_id),
            )
            self.db.execute(
                """UPDATE ladder_promotions
                   SET stage='discarded', stage_reason='operator_discard',
                       updated_at=?
                   WHERE id=?""",
                (stamp, int(promotion_id)),
            )
            if conversation_id is not None and operator_prompt:
                self.db.execute(
                    """INSERT INTO messages(conversation_id, created_at, role, content)
                       VALUES (?, ?, 'user', ?)""",
                    (int(conversation_id), stamp, str(operator_prompt)),
                )
        return {
            "discarded": True, "promotion_id": int(promotion_id),
            "family": str(row["family"]), "skill_name": name,
        }

    def grandfather_ladder(
        self,
        workspace: Path,
        *,
        project_id: int,
        actor: str = "runtime",
        conversation_id: int | None = None,
        permission: str = "runtime",
    ) -> dict[str, Any]:
        """Adopt pre-M4 live documents at stage ``unapproved_legacy``.

        ``Memory`` has no workspace and cannot get one in M4, so this cannot
        happen inside the migration (H-1): it is a one-time receipted pass.
        The documents **stay live** -- no operator-visible regression -- and
        are counted in their own bucket rather than as unverified promotions,
        because no ladder promotion ever claimed them.  A legacy document
        reaches the model with no proof, no gate and no ledger check until it
        is approved or rolled back; that is the pre-M4 status quo made
        visible, and ``ladder status`` says so in those words.

        Idempotence is ``idx_ladder_promotions_one_live``, not a
        check-then-insert: the partial unique index is what makes a duplicate
        adoption impossible under a concurrent second pass, and the
        ``IntegrityError`` it raises is caught here as "already grandfathered"
        (S-8).
        """
        self._require_ladder()
        if not self._ladder_workspace_matches(project_id, workspace):
            return {"grandfathered": 0, "reason": "workspace_mismatch"}
        try:
            live = self._ladder_live_documents(workspace)
        except OSError:
            return {"grandfathered": 0, "reason": "workspace_unavailable"}
        if not live:
            return {"grandfathered": 0, "adopted": [], "skipped": []}
        adopted: list[dict[str, Any]] = []
        skipped: list[str] = []
        # A row can be deleted by raw SQL; an event cannot.  Without consulting
        # the spine, a deleted ``approved`` row under a live document made that
        # document look untouched, and this pass adopted it at
        # ``unapproved_legacy`` -- after which it reached the model with no
        # proof, no gate and no ledger check, and never counted toward
        # ``unverified_at_seal`` (R-9).  A name the ladder has ever named is
        # never legacy; it is an ``orphan_document`` for the reconciler.
        named = self._ladder_named_in_spine()
        for name, document in sorted(live.items()):
            family = str(document.get("family") or "")
            if family not in _memory().learning_ladder.LADDER_READ_FAMILIES:
                skipped.append(name)
                continue
            if name in named:
                skipped.append(name)
                continue
            if self.ladder_promotions(project_id=int(project_id), skill_name=name):
                skipped.append(name)
                continue
            stamp = _memory().now_iso()
            digest = str(document["sha256"])
            try:
                with self._immediate_transaction():
                    promotion_id = _memory().allocate_ladder_id(self.db)
                    event_id = _memory().memory_spine.append_event(
                        self.db,
                        self._spine_key,
                        kind="ladder.grandfathered",
                        actor=actor,
                        source="learning ladder",
                        scope="global",
                        permission=permission,
                        outcome="applied",
                        payload={
                            "at": stamp,
                            "family": family,
                            "project_id": int(project_id),
                            "skill_name": name,
                            "approved_sha256": digest,
                            "source": "legacy_document",
                        },
                        now=stamp,
                        conversation_id=conversation_id,
                        subject_kind="ladder",
                        subject_id=promotion_id,
                    )
                    gate = self.calibration_gate(
                        family, **_memory().learning_ladder.LADDER_GATE_THRESHOLDS
                    ) if family in self.PREDICTION_FAMILIES else {}
                    self.db.execute(
                        """INSERT INTO ladder_promotions(
                               id, created_at, updated_at, project_id, family,
                               skill_name, stage, stage_reason,
                               lesson_ids_json, proof_json, proof_sha256,
                               reuse_count, context_count, epoch_id, gate_json,
                               staged_sha256, approval_token, approved_sha256,
                               approved_at, prior_sha256, prior_document,
                               spine_event_id
                           ) VALUES (?, ?, ?, ?, ?, ?, 'unapproved_legacy',
                                     'legacy_document', '[]', ?, ?, 0, 0, NULL,
                                     ?, ?, ?, ?, ?, NULL, NULL, ?)""",
                        (
                            promotion_id, stamp, stamp, int(project_id), family,
                            name,
                            _memory().memory_spine.canonical({"legacy": True}),
                            self._ladder_legacy_proof_digest(),
                            _memory().memory_spine.canonical(gate),
                            digest, _memory().secrets.token_urlsafe(12), digest, stamp,
                            int(event_id),
                        ),
                    )
            except _memory().sqlite3.IntegrityError:
                # The partial unique index fired: a concurrent pass adopted
                # it first, which is the same outcome by a different route.
                skipped.append(name)
                continue
            except _memory().memory_spine.SpineError as exc:
                # Ruling 27: the agent runs this on its first workspace turn,
                # so a spine that will not accept an append must stop the pass
                # rather than the turn.  Nothing was adopted; the pass is
                # idempotent and runs again once the chain is repaired.
                self._record_degraded_write(
                    "grandfather",
                    f"skill={name} ({type(exc).__name__})",
                    reason="spine_unverified",
                )
                return {
                    "grandfathered": len(adopted),
                    "adopted": adopted,
                    "skipped": sorted(skipped + [name]),
                    "reason": "spine_unverified",
                }
            adopted.append({
                "promotion_id": int(promotion_id),
                "skill_name": name,
                "family": family,
                "approved_sha256": digest,
            })
        return {
            "grandfathered": len(adopted),
            "adopted": adopted,
            "skipped": sorted(skipped),
        }

    def _ladder_legacy_proof_digest(self) -> str:
        """The digest over the literal legacy material.

        A legacy row has no proof and says so: the digest is over
        ``{"legacy": true}`` and nothing else, so it can never accidentally
        equal a real proof digest and an approval that re-derives a proof will
        always refuse ``proof_stale`` against it -- which is correct, because
        approving a legacy document means staging a real promotion for the
        family and approving that instead.
        """
        return _memory().hmac.new(
            self._spine_key,
            (_memory()._LADDER_PROOF_DIGEST_TAG + "\0legacy").encode("utf-8"),
            _memory().hashlib.sha256,
        ).hexdigest()

    def lesson_candidate_count(
        self, family: str, project_id: int, *, limit: int | None = None
    ) -> int:
        """How many eligible lessons the closed gate is withholding, bounded.

        The abstention cue must fire on ``gate-closed`` only when something
        was actually withheld, otherwise every uncalibrated family on every
        turn tells the model that no lesson is available -- which is true but
        uninformative, and noise is how a cue stops being read.  This is the
        cheap count that answers "was anything withheld", called by the Agent
        **only when the gate is closed**, so it is never on the hot path of a
        turn that is about to run the channel anyway.

        Scope and eligibility are ``match_lessons``'s candidate stage: this
        family, this project, a complete outcome, an active lifecycle with no
        successor, and inside the validity window.  It deliberately does
        **not** re-derive the provenance and control digests per row, because
        that is O(rows) Python and this has a 1 ms budget.  It therefore
        over-counts relative to the lane's own eligibility, and that is the
        safe direction: an over-count can only make the cue fire on a turn
        where the lane would also have returned nothing, while an under-count
        would silence it on a turn where advice really was withheld.

        ``limit`` caps the work: the count is taken over a ``LIMIT``
        subquery, so a family with ten thousand lessons costs the same as one
        with fifty.  The caller needs a boolean, not a census.
        """
        self._ensure_open()
        if family not in self.PREDICTION_FAMILIES:
            raise ValueError(f"Unknown lesson family: {family}")
        bounded = _memory()._bounded_limit(
            _memory().learning_ladder.LADDER_WITHHELD_CAP if limit is None else limit,
            1_000,
        )
        if not bounded:
            return 0
        try:
            row = self.db.execute(
                """SELECT COUNT(*) FROM (
                       SELECT m.id FROM memories AS m
                       JOIN lesson_controls AS lc ON lc.memory_id = m.id
                       WHERE m.kind='lesson' AND m.family=?
                         AND m.outcome_status='complete'
                         AND lc.project_id=?
                         AND lc.lifecycle_status='active'
                         AND lc.superseded_by IS NULL
                         AND lc.valid_until > ?
                       LIMIT ?)""",
                (
                    str(family), self._project_id(project_id), _memory().now_iso(),
                    bounded,
                ),
            ).fetchone()
        except _memory().sqlite3.DatabaseError:
            # A count that cannot be taken must not fail the turn; zero
            # suppresses the cue, which is the quiet direction.
            return 0
        return int(row[0] or 0)

    def record_lesson_gate_closed(
        self,
        gate: Mapping[str, Any],
        *,
        family: str,
        project_id: int | None,
        withheld_candidates: int | None = None,
    ) -> dict[str, Any]:
        """Publish the report for a turn whose gate was closed before the lane
        ran, and return it.

        ``match_lessons`` is never called on such a turn, so without this the
        report would still read ``idle`` from the previous turn and the cue
        would key on stale state.  The mode stays ``idle`` -- the lane
        genuinely did not run, and ``idle`` is an availability fact, not a
        claim about competence -- and three keys of the shared record carry
        what the gate said: ``gate_closed``, ``gate_closure`` (the
        ``insufficient`` / ``calibration`` split, whose definition lives in
        ``learning_ladder`` so the store and the skill channel cannot
        disagree) and ``withheld_candidates``.  They are arguments to the
        shared builder rather than keys bolted on here: a record one caller
        widens and another does not puts the shape back in two places, which
        is the drift the shared builder exists to prevent, and the sealed
        holdout scorer reads this record.

        The caller passes ``withheld_candidates`` from
        ``lesson_candidate_count`` when it wants the cue to distinguish "the
        gate is closed and there was advice it could not offer" from "the gate
        is closed and there was nothing to offer anyway".
        """
        self._ensure_open()
        record = _memory().learning_ladder.lesson_recall_record(
            "idle",
            family=str(family),
            project_id=None if project_id is None else int(project_id),
            gate_closed=not bool(gate.get("allowed")),
            gate_closure=_memory().learning_ladder.gate_closed_reason(gate),
            withheld_candidates=withheld_candidates,
        )
        self._last_lesson_recall_report = record
        return dict(record)

    def ladder_reconciliation_plan(
        self, workspace: Path, *, project_id: int
    ) -> dict[str, Any]:
        """What ``ladder verify --apply`` would change, and a plan token.

        Read-only.  The database is the record and the filesystem is
        reconciled to it, never the other way round, so every action below
        either moves a row to a terminal stage or adopts a document the
        ladder has never seen.  Nothing here overwrites an operator's edit: a
        live document whose digest has drifted is **withdrawn**, not restored.

        The plan token is twelve hex characters of a keyed digest over the
        planned actions *and* the state they were derived from, so a store
        that moved between printing a plan and applying it produces a
        different token and the apply refuses ``stale_plan`` -- the
        ``graph rebuild`` and ``spine rebuild-claims`` discipline exactly.
        """
        self._require_ladder()
        if not self._ladder_workspace_matches(project_id, workspace):
            return {
                "reason": "workspace_mismatch", "actions": [],
                "plan_token": None, "project_id": int(project_id),
            }
        try:
            live = self._ladder_live_documents(workspace)
            staged = {
                str(entry["name"]): entry
                for entry in _memory().skill_library.list_staged_skills(_memory().Path(workspace))
            }
        except (OSError, ValueError, PermissionError):
            # A project directory that has gone away is reported, not treated
            # as "every document was deleted" (S-8).
            return {
                "reason": "workspace_unavailable", "actions": [],
                "plan_token": None, "project_id": int(project_id),
            }
        rows = self.ladder_promotions(project_id=int(project_id))
        by_name: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            by_name.setdefault(str(row["skill_name"]), []).append(row)
        actions: list[dict[str, Any]] = []

        # 1. Pre-M4 documents the ladder has never touched.
        for name in sorted(self._ladder_untouched_documents(live, project_id)):
            actions.append({
                "action": "grandfather",
                "skill_name": name,
                "family": live[name].get("family"),
                "reason": "legacy_document",
            })

        for name, entries in sorted(by_name.items()):
            live_row = next(
                (row for row in entries if str(row["stage"]) in _memory().LADDER_LIVE_STAGES),
                None,
            )
            staged_row = next(
                (row for row in entries if str(row["stage"]) == "staged"), None
            )
            document = live.get(name)
            # 2. A row that claims the live document but has none.
            if live_row is not None and document is None:
                actions.append({
                    "action": "withdraw",
                    "promotion_id": int(live_row["id"]),
                    "skill_name": name,
                    "family": str(live_row["family"]),
                    "reason": "live_document_missing",
                })
            # 3. A live document an operator or another path edited.  Never
            #    silently overwritten: the row is withdrawn and the operator
            #    keeps their bytes.
            elif (
                live_row is not None
                and str(document["sha256"]) != str(live_row["approved_sha256"] or "")
            ):
                actions.append({
                    "action": "withdraw",
                    "promotion_id": int(live_row["id"]),
                    "skill_name": name,
                    "family": str(live_row["family"]),
                    "reason": "live_digest_mismatch",
                })
            # 4. A staged row whose file is gone.  It cannot be re-staged
            #    from the row: the record keeps the document's digest, not its
            #    bytes, on purpose -- 32 KB per staging that nothing reads
            #    would be a cost paid on every candidate.  The worker restages
            #    from the proof on its next pass, which is cheap and is the
            #    same code path that produced it.
            if staged_row is not None and name not in staged:
                actions.append({
                    "action": "discard",
                    "promotion_id": int(staged_row["id"]),
                    "skill_name": name,
                    "family": str(staged_row["family"]),
                    "reason": "staged_file_missing",
                })

        # 5. A staged file with no staged row: the crash window on the other
        #    side of staging.  Unreachable by the model either way, so the
        #    only thing to do is tidy it.
        for name in sorted(staged):
            entries = by_name.get(name, [])
            if not any(str(row["stage"]) == "staged" for row in entries):
                actions.append({
                    "action": "discard_file",
                    "skill_name": name,
                    "reason": "staged_row_missing",
                })

        # 6. ``orphan_document``: a live document the ladder has touched
        #    before that no live row claims.  Design 7.8's first crash
        #    direction, and R-9's deleted-row shape.
        #
        #    "Has touched before" is decided by the SPINE, not by the rows,
        #    and it is the same condition ``ladder_unverified_promotions`` and
        #    ``grandfather_ladder`` use -- one condition named once, because
        #    the three readers disagreeing about it is what let a deleted
        #    approved row launder a promoted artefact into the legacy bucket.
        #    A row can be deleted; an event cannot.
        named = self._ladder_named_in_spine()
        for name, document in sorted(live.items()):
            entries = by_name.get(name, [])
            if any(str(row["stage"]) in _memory().LADDER_LIVE_STAGES for row in entries):
                continue
            if not entries and name not in named:
                # Never touched at all: a pre-M4 document, which step 1
                # grandfathers rather than treating as an orphan.
                continue
            actions.append({
                "action": "orphan_document",
                "skill_name": name,
                "family": document.get("family"),
                "reason": "reconciled_orphan",
            })

        # Withdrawals whose receipt never landed (item 30).  Listed, never
        # acted on: the fix is to repair the spine, and the read path flushes
        # the receipt itself on the next turn once it can.  ``ladder verify``
        # shows them so an operator learns the chain needs attention.
        pending = self.ladder_pending_withdrawals(project_id)
        return {
            "reason": None,
            "project_id": int(project_id),
            "actions": actions,
            "pending_withdrawals": pending,
            "plan_token": self._ladder_plan_token(project_id, actions, live, staged),
        }

    def _ladder_plan_token(
        self,
        project_id: int,
        actions: Sequence[Mapping[str, Any]],
        live: Mapping[str, Mapping[str, Any]],
        staged: Mapping[str, Mapping[str, Any]],
    ) -> str:
        """Twelve hex characters binding the plan to the state it came from."""
        material = _memory().memory_spine.canonical({
            "project_id": int(project_id),
            "actions": [
                {key: value for key, value in sorted(action.items())}
                for action in actions
            ],
            "live": sorted(
                (name, str(entry["sha256"])) for name, entry in live.items()
            ),
            "staged": sorted(
                (name, str(entry.get("sha256") or "")) for name, entry in staged.items()
            ),
        })
        return _memory().hmac.new(
            self._spine_key,
            (_memory()._LADDER_PLAN_DIGEST_TAG + "\0" + material).encode("utf-8"),
            _memory().hashlib.sha256,
        ).hexdigest()[:12]

    def reconcile_ladder(
        self,
        workspace: Path,
        *,
        project_id: int,
        apply: bool = False,
        plan_token: str | None = None,
        purge_orphans: bool = False,   # retained, ignored; see the docstring
        actor: str = "runtime",
        conversation_id: int | None = None,
        permission: str = "runtime",
    ) -> dict[str, Any]:
        """Reconcile the filesystem to the record, or say what would change.

        Without ``apply`` this is exactly ``ladder_reconciliation_plan``.
        With it, ``plan_token`` (when supplied) must equal the token the plan
        carried, or the call refuses ``stale_plan`` and changes nothing --
        which is the whole point of printing a token: an operator approves the
        plan they read, not whatever the store happens to hold a minute later.

        A clean run appends no event at all, the M3 ``graph rebuild``
        precedent.  Every action that does change something appends its own
        receipt: ``ladder.grandfathered`` for an adoption, ``ladder.withdrawn``
        for everything else.

        ``purge_orphans`` is **retained and ignored.**  It used to delete a
        live document no row claimed; an orphan is now parked in the staging
        root under the ``withdrawn-`` prefix unconditionally, because deleting
        an operator's artefact to repair the store's own bookkeeping is the
        wrong trade and because R-1 made that flag the routine exit from a
        common state.  The keyword stays only so a caller mid-flight does not
        break; it should be dropped from the CLI.
        """
        plan = self.ladder_reconciliation_plan(workspace, project_id=project_id)
        if plan["reason"] is not None or not apply:
            plan["applied"] = False
            plan["changed"] = 0
            return plan
        if plan_token is None:
            # R-11: every other reconciler in this family makes ``--plan``
            # optional, and this is the one whose apply path can move an
            # operator's live learned skill out of the live root.  The plan is
            # what the operator read; applying without it is applying to a
            # store they did not see.
            return {
                **plan, "applied": False, "changed": 0,
                "reason": "plan_required",
            }
        if not _memory().hmac.compare_digest(
            str(plan_token), str(plan["plan_token"])
        ):
            return {
                **plan, "applied": False, "changed": 0, "reason": "stale_plan",
            }
        changed = 0
        performed: list[dict[str, Any]] = []
        for action in plan["actions"]:
            kind = str(action["action"])
            if kind == "grandfather":
                result = self.grandfather_ladder(
                    workspace, project_id=int(project_id), actor=actor,
                    conversation_id=conversation_id, permission=permission,
                )
                changed += int(result.get("grandfathered") or 0)
                performed.append({**action, "done": bool(result.get("grandfathered"))})
            elif kind in {"withdraw", "discard"}:
                result = self.withdraw_ladder_promotion(
                    int(action["promotion_id"]), reason=str(action["reason"]),
                    workspace=workspace, actor=actor,
                    conversation_id=conversation_id, permission=permission,
                )
                changed += int(bool(result.get("withdrawn")))
                performed.append({**action, "done": bool(result.get("withdrawn"))})
            elif kind == "discard_file":
                done = True
                try:
                    _memory().skill_library.discard_staged_skill(
                        _memory().Path(workspace), str(action["skill_name"])
                    )
                except (KeyError, OSError, ValueError, PermissionError):
                    done = False
                changed += int(done)
                performed.append({**action, "done": done})
            elif kind == "orphan_document":
                # A live document no row claims is PARKED, never deleted and
                # never left in place (ruling 16 / the correctness review):
                # leaving it is R-1's uncountable orphan, and deleting it
                # throws away an operator's artefact to fix the store's own
                # bookkeeping.  The bytes move to the staging root under the
                # ``withdrawn-`` prefix, where nothing model-facing can reach
                # them and a new promotion can supersede them.
                #
                # Item 35: when the orphan is a DELETED APPROVED row -- an
                # approving event exists but no row and no withdrawal -- the
                # reconciler also writes the ``ladder.withdrawn`` receipt the
                # vanished row can no longer carry, keeping the promise this
                # method's docstring makes and stopping the parked orphan from
                # lingering as a spine-derived pending withdrawal.  A
                # grandfathered orphan (a ``ladder.grandfathered`` event, no
                # ``ladder.approved``) has no such receipt to write and is not
                # flagged by ``_ladder_orphan_pending`` either, so it is parked
                # exactly as before.
                orphan_name = str(action["skill_name"])
                approval = self._ladder_orphan_approval(orphan_name)
                done = False
                receipt_deferred = False
                try:
                    parked = _memory().skill_library.withdraw_learned_skill(
                        _memory().Path(workspace), orphan_name
                    )
                    done = bool(parked.get("withdrawn"))
                    _memory()._ladder_clear_catalog_cache()
                except (KeyError, OSError, ValueError, PermissionError):
                    done = False
                if done and approval is not None:
                    receipt_deferred = not self._append_orphan_withdrawal(
                        int(approval["promotion_id"]), approval["family"],
                        int(project_id), orphan_name,
                        approval["approved_sha256"],
                    )
                changed += int(done)
                performed.append({
                    **action, "done": done,
                    "note": (
                        (
                            "parked in the staging root as withdrawn-"
                            + (
                                "; receipt deferred, rerun ladder verify"
                                if receipt_deferred else ""
                            )
                        )
                        if done else "could not be parked; rerun ladder verify"
                    ),
                })
        return {
            **plan,
            "applied": True,
            "changed": changed,
            "actions": performed,
        }
