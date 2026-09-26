"""Current Memory domain extracted without changing method control flow.

Global dependencies resolve through jarvis.memory at call time. The facade keeps
public imports, patch seams, constants and authorization helpers authoritative.
"""
from __future__ import annotations

from collections.abc import Mapping
from collections.abc import Sequence
from typing import Any
import sqlite3
from .memory_runtime import (_memory)


class StrategyTrialMemoryMixin:
    """Mechanically extracted current Memory methods."""

    @staticmethod
    def _strategy_transfer_trial_manifest_material(
        *,
        created_at: str,
        expires_at: str,
        project_id: int,
        target_families: Sequence[str],
        family_cap_values: Mapping[str, int],
        strategies: Sequence[str],
        sample_cap: int,
        seed: str,
        evaluator_version: str,
        evaluator_sha256: str,
        fixture_sha256: str,
        config_sha256: str,
        runtime_sha256: str,
    ) -> dict[str, Any]:
        return {
            "schema": _memory().TRIAL_SCHEMA,
            "created_at": str(created_at),
            "expires_at": str(expires_at),
            "project_id": int(project_id),
            "target_families": list(target_families),
            "family_caps": {
                str(key): int(family_cap_values[key])
                for key in sorted(family_cap_values)
            },
            "strategies": list(strategies),
            "sample_cap": int(sample_cap),
            "block_size": _memory().TRIAL_BLOCK_SIZE,
            "seed": str(seed),
            "evaluator_version": str(evaluator_version),
            "evaluator_sha256": str(evaluator_sha256),
            "fixture_sha256": str(fixture_sha256),
            "config_sha256": str(config_sha256),
            "runtime_sha256": str(runtime_sha256),
            "operator_confirmed": True,
        }

    @staticmethod
    def _strategy_transfer_trial_state_material(
        *,
        manifest_sha256: str,
        updated_at: str,
        status: str,
        status_reason: str | None,
        closed_at: str | None,
        promoted_at: str | None,
    ) -> dict[str, Any]:
        return {
            "schema": "jarvis.strategy-transfer-trial-state.v1",
            "manifest_sha256": str(manifest_sha256),
            "updated_at": str(updated_at),
            "status": str(status),
            "status_reason": status_reason,
            "closed_at": closed_at,
            "promoted_at": promoted_at,
        }

    def _strategy_transfer_trial_manifest_validation(
        self,
        row: Mapping[str, Any],
    ) -> tuple[bool, dict[str, Any] | str]:
        try:
            created_at = self._canonical_utc_timestamp(row["created_at"])
            updated_at = self._canonical_utc_timestamp(row["updated_at"])
            expires_at = self._canonical_utc_timestamp(row["expires_at"])
            closed_at = (
                None if row["closed_at"] is None
                else self._canonical_utc_timestamp(row["closed_at"])
            )
            promoted_at = (
                None if row["promoted_at"] is None
                else self._canonical_utc_timestamp(row["promoted_at"])
            )
            if (
                created_at is None
                or updated_at is None
                or expires_at is None
                or str(row["created_at"]) != created_at
                or str(row["updated_at"]) != updated_at
                or str(row["expires_at"]) != expires_at
                or (row["closed_at"] is not None and row["closed_at"] != closed_at)
                or (
                    row["promoted_at"] is not None
                    and row["promoted_at"] != promoted_at
                )
            ):
                return False, "manifest_timestamp_invalid"
            created = _memory().datetime.fromisoformat(created_at)
            expires = _memory().datetime.fromisoformat(expires_at)
            if not created < expires <= created + _memory().timedelta(days=_memory().TRIAL_MAX_DAYS):
                return False, "manifest_expiry_invalid"
            project_id = self._project_id(int(row["project_id"]))
            if self.get_project(project_id) is None:
                return False, "manifest_project_invalid"
            target_families = _memory().json.loads(str(row["target_families_json"]))
            cap_values = _memory().json.loads(str(row["family_caps_json"]))
            strategies = _memory().json.loads(str(row["strategies_json"]))
            if (
                not isinstance(target_families, list)
                or not isinstance(cap_values, dict)
                or not isinstance(strategies, list)
                or target_families != sorted(target_families)
                or strategies != sorted(strategies)
                or not all(
                    isinstance(family, str)
                    and family in self.PREDICTION_FAMILIES
                    for family in target_families
                )
                or not strategies
                or not all(
                    isinstance(strategy, str) and strategy in _memory().STRATEGY_SET
                    for strategy in strategies
                )
                or len(strategies) != len(set(strategies))
            ):
                return False, "manifest_scope_invalid"
            expected_caps = _memory().family_caps(target_families, int(row["sample_cap"]))
            if cap_values != expected_caps:
                return False, "manifest_caps_invalid"
            canonical_families = self._strategy_transfer_canonical_json(
                target_families
            )
            canonical_caps = self._strategy_transfer_canonical_json(cap_values)
            canonical_strategies = self._strategy_transfer_canonical_json(strategies)
            if (
                str(row["target_families_json"]) != canonical_families
                or str(row["family_caps_json"]) != canonical_caps
                or str(row["strategies_json"]) != canonical_strategies
                or int(row["block_size"]) != _memory().TRIAL_BLOCK_SIZE
                or int(row["operator_confirmed"]) != 1
            ):
                return False, "manifest_encoding_invalid"
            seed = _memory().validated_seed(row["seed"])
            evaluator_version = self._strategy_transfer_identifier(
                row["evaluator_version"], "evaluator_version"
            )
            evaluator_sha256 = _memory().validated_sha256(
                row["evaluator_sha256"], "evaluator"
            )
            fixture_sha256 = _memory().validated_sha256(row["fixture_sha256"], "fixture")
            config_sha256 = _memory().validated_sha256(row["config_sha256"], "config")
            runtime_sha256 = _memory().validated_sha256(row["runtime_sha256"], "runtime")
            material = self._strategy_transfer_trial_manifest_material(
                created_at=created_at,
                expires_at=expires_at,
                project_id=project_id,
                target_families=target_families,
                family_cap_values=cap_values,
                strategies=strategies,
                sample_cap=int(row["sample_cap"]),
                seed=seed,
                evaluator_version=evaluator_version,
                evaluator_sha256=evaluator_sha256,
                fixture_sha256=fixture_sha256,
                config_sha256=config_sha256,
                runtime_sha256=runtime_sha256,
            )
            manifest_sha256 = _memory().sha256_json(material)
            if str(row["manifest_sha256"]) != manifest_sha256:
                return False, "manifest_digest_mismatch"
            status = str(row["status"])
            status_reason = (
                None if row["status_reason"] is None
                else str(row["status_reason"])
            )
            if status not in _memory().TRIAL_MANIFEST_STATUSES:
                return False, "manifest_status_invalid"
            state = self._strategy_transfer_trial_state_material(
                manifest_sha256=manifest_sha256,
                updated_at=updated_at,
                status=status,
                status_reason=status_reason,
                closed_at=closed_at,
                promoted_at=promoted_at,
            )
            if str(row["state_sha256"]) != _memory().sha256_json(state):
                return False, "manifest_state_digest_mismatch"
        except (
            _memory().json.JSONDecodeError,
            KeyError,
            OverflowError,
            _memory().StrategyTransferError,
            _memory().StrategyTransferTrialError,
            TypeError,
            ValueError,
        ):
            return False, "manifest_invalid"
        return True, {
            "manifest_id": int(row["id"]),
            "project_id": project_id,
            "created_at": created_at,
            "updated_at": updated_at,
            "expires_at": expires_at,
            "target_families": target_families,
            "family_caps": cap_values,
            "strategies": strategies,
            "sample_cap": int(row["sample_cap"]),
            "seed": seed,
            "evaluator_version": evaluator_version,
            "evaluator_sha256": evaluator_sha256,
            "fixture_sha256": fixture_sha256,
            "config_sha256": config_sha256,
            "runtime_sha256": runtime_sha256,
            "status": status,
            "status_reason": status_reason,
            "closed_at": closed_at,
            "promoted_at": promoted_at,
            "manifest_sha256": manifest_sha256,
        }

    def _strategy_transfer_trial_benchmark_matches(
        self,
        manifest: Mapping[str, Any],
    ) -> bool:
        """Bind both the Phase 4A benchmark and installed causal evaluator."""
        benchmark = self._strategy_transfer_benchmark_row()
        if benchmark is None:
            return False
        try:
            contract = self._strategy_transfer_trial_contract()
        except (
            OSError, _memory().StrategyTransferError, _memory().StrategyTransferTrialError,
            TypeError, ValueError,
        ):
            return False
        return all(
            str(contract[field]) == str(manifest[field])
            for field in (
                "evaluator_version", "evaluator_sha256", "fixture_sha256",
                "config_sha256",
            )
        )

    def _strategy_transfer_trial_contract(self) -> dict[str, Any]:
        """Validate and pin the independently frozen Phase 4B causal holdout."""
        from . import strategy_transfer_trial_eval as trial_eval

        evaluator_path = _memory().Path(str(trial_eval.__file__)).resolve()
        fixture_path = (
            evaluator_path.parent
            / "evaluation_fixtures"
            / "strategy_transfer_trial_holdout_v1.json"
        )
        raw_fixture = fixture_path.read_bytes()
        fixture_sha256 = _memory().hashlib.sha256(raw_fixture).hexdigest()
        fixture = _memory().json.loads(raw_fixture.decode("utf-8"))
        if not isinstance(fixture, dict) or set(fixture) != {
            "schema", "phase4a_benchmark_attestation_sha256", "manifest", "rows",
        }:
            raise _memory().StrategyTransferTrialError("causal holdout fixture is malformed")
        trial_manifest = fixture.get("manifest")
        if not isinstance(trial_manifest, dict):
            raise _memory().StrategyTransferTrialError("causal holdout manifest is malformed")
        manifest_sha256 = _memory().validated_sha256(
            trial_manifest.get("manifest_sha256"), "causal holdout manifest"
        )
        report = trial_eval.evaluate_strategy_transfer_trial(
            fixture_path,
            expected_artifact_sha256=fixture_sha256,
            expected_manifest_sha256=manifest_sha256,
        )
        if (
            not isinstance(report, _memory().Mapping)
            or report.get("all_exit_criteria_passed") is not True
            or report.get("activation_authorized") is not False
        ):
            raise _memory().StrategyTransferTrialError("causal holdout did not pass closed")
        config = dict(trial_eval.EVALUATION_CONFIG)
        return {
            "evaluator_version": str(trial_eval.TRIAL_EVALUATOR_VERSION),
            "evaluator_sha256": _memory().hashlib.sha256(
                evaluator_path.read_bytes()
            ).hexdigest(),
            "fixture_sha256": fixture_sha256,
            "config_sha256": _memory().sha256_json(config),
            "config": config,
            "fixture_manifest_sha256": manifest_sha256,
        }

    def _strategy_transfer_trial_manifest_eligibility(
        self,
        row: Mapping[str, Any],
        *,
        target_family: str,
        current_runtime_sha256: str,
    ) -> tuple[bool, dict[str, Any] | str]:
        valid, manifest = self._strategy_transfer_trial_manifest_validation(row)
        if not valid or not isinstance(manifest, dict):
            return False, str(manifest)
        try:
            supplied_runtime = _memory().validated_sha256(
                current_runtime_sha256, "current runtime"
            )
            actual_runtime = _memory().strategy_transfer_runtime_sha256()
        except (OSError, _memory().StrategyTransferTrialError):
            return False, "runtime_hash_unavailable"
        if (
            manifest["status"] != "active"
            or target_family not in manifest["target_families"]
            or _memory().datetime.now(_memory().timezone.utc) >= _memory().datetime.fromisoformat(
                manifest["expires_at"]
            )
            or supplied_runtime != actual_runtime
            or supplied_runtime != manifest["runtime_sha256"]
        ):
            return False, "manifest_inactive_expired_or_drifted"
        project = self.get_project(int(manifest["project_id"]))
        if project is None or not bool(project["enabled"]):
            return False, "manifest_project_disabled"
        if not self._strategy_transfer_trial_benchmark_matches(manifest):
            return False, "manifest_benchmark_drift"
        ledger_health = self._strategy_transfer_ledger_health()
        if (
            ledger_health["available"] is not True
            or int(ledger_health["invalid_receipts"]) != 0
            or int(ledger_health["harm_quarantines"]) != 0
        ):
            return False, "manifest_ledger_or_quarantine_unavailable"
        try:
            assigned = int(self.db.execute(
                """SELECT COUNT(*) FROM strategy_transfer_trial_assignments
                   WHERE manifest_id=? AND target_family=?""",
                (int(manifest["manifest_id"]), target_family),
            ).fetchone()[0])
        except _memory().sqlite3.DatabaseError:
            return False, "manifest_assignment_ledger_unavailable"
        if assigned >= int(manifest["family_caps"][target_family]):
            return False, "manifest_family_cap_reached"
        return True, manifest

    def _strategy_transfer_trial_dispatch_eligibility(
        self,
        assignment: Mapping[str, Any],
    ) -> tuple[bool, str]:
        """Recheck mutable trial gates immediately before provider dispatch.

        This deliberately does not consult the sample cap: the assignment was
        already durably admitted. It protects only conditions that may change
        after assignment and before the provider sees a treatment prompt.
        Callers must hold an immediate transaction while invoking it.
        """
        try:
            manifest_row = self.db.execute(
                "SELECT * FROM strategy_transfer_trial_manifests WHERE id=?",
                (int(assignment["manifest_id"]),),
            ).fetchone()
        except (KeyError, _memory().sqlite3.DatabaseError, TypeError, ValueError):
            return False, "dispatch_manifest_query_unavailable"
        if manifest_row is None:
            return False, "dispatch_manifest_missing"
        valid, manifest = self._strategy_transfer_trial_manifest_validation(
            manifest_row
        )
        if not valid or not isinstance(manifest, dict):
            return False, "dispatch_manifest_invalid"
        try:
            target_family = str(assignment["target_family"])
            project_id = int(assignment["project_id"])
            actual_runtime = _memory().strategy_transfer_runtime_sha256()
            expired = _memory().datetime.now(_memory().timezone.utc) >= _memory().datetime.fromisoformat(
                str(manifest["expires_at"])
            )
        except (KeyError, OSError, TypeError, ValueError):
            return False, "dispatch_runtime_or_time_unavailable"
        if (
            manifest["status"] != "active"
            or expired
            or target_family not in manifest["target_families"]
            or project_id != int(manifest["project_id"])
        ):
            return False, "dispatch_manifest_inactive_expired_or_out_of_scope"
        if actual_runtime != manifest["runtime_sha256"]:
            return False, "dispatch_runtime_drift"
        project = self.get_project(project_id)
        if project is None or not bool(project["enabled"]):
            return False, "dispatch_project_disabled"
        if not self._strategy_transfer_trial_benchmark_matches(manifest):
            return False, "dispatch_benchmark_drift"
        ledger = self._strategy_transfer_ledger_health()
        if ledger["available"] is not True or int(ledger["invalid_receipts"]):
            return False, "dispatch_application_ledger_unavailable"
        if int(ledger["harm_quarantines"]):
            return False, "dispatch_harm_quarantine"
        return True, "eligible"

    def create_strategy_transfer_trial_manifest(
        self,
        *,
        project_id: int,
        target_families: Sequence[str],
        strategies: Sequence[str],
        sample_cap: int,
        expires_at: str,
        seed: str,
        evaluator_version: str,
        evaluator_sha256: str,
        fixture_sha256: str,
        config_sha256: str,
        runtime_sha256: str,
        operator_confirmed: bool,
    ) -> dict[str, Any]:
        """Create an explicit, bounded trial before any target outcome exists."""
        if operator_confirmed is not True:
            raise _memory().StrategyTransferTrialError(
                "operator confirmation is required to create a transfer trial"
            )
        normalized_project = self._project_id(project_id)
        project = self.get_project(normalized_project)
        if project is None or not bool(project["enabled"]):
            raise _memory().StrategyTransferTrialError("trial project is unavailable")
        if isinstance(target_families, (str, bytes)) or not isinstance(
            target_families, _memory().Sequence
        ):
            raise _memory().StrategyTransferTrialError("target families must be an array")
        normalized_families = sorted(str(item) for item in target_families)
        if not all(
            family in self.PREDICTION_FAMILIES for family in normalized_families
        ):
            raise _memory().StrategyTransferTrialError("trial target family is unsupported")
        caps = _memory().family_caps(normalized_families, sample_cap)
        if isinstance(strategies, (str, bytes)) or not isinstance(
            strategies, _memory().Sequence
        ):
            raise _memory().StrategyTransferTrialError("strategies must be an array")
        normalized_strategies = sorted(str(item) for item in strategies)
        if (
            not normalized_strategies
            or len(normalized_strategies) != len(set(normalized_strategies))
            or any(item not in _memory().STRATEGY_SET for item in normalized_strategies)
        ):
            raise _memory().StrategyTransferTrialError(
                "trial strategies must be distinct closed strategy labels"
            )
        normalized_seed = _memory().validated_seed(seed)
        safe_version = self._strategy_transfer_identifier(
            evaluator_version, "evaluator_version"
        )
        pins = {
            "evaluator_sha256": _memory().validated_sha256(
                evaluator_sha256, "evaluator"
            ),
            "fixture_sha256": _memory().validated_sha256(fixture_sha256, "fixture"),
            "config_sha256": _memory().validated_sha256(config_sha256, "config"),
            "runtime_sha256": _memory().validated_sha256(runtime_sha256, "runtime"),
        }
        try:
            actual_runtime = _memory().strategy_transfer_runtime_sha256()
        except OSError as exc:
            raise _memory().StrategyTransferTrialError(
                "current trial runtime hash is unavailable"
            ) from exc
        if pins["runtime_sha256"] != actual_runtime:
            raise _memory().StrategyTransferTrialError("trial runtime pin does not match")
        created_at = _memory().now_iso()
        canonical_expiry = self._canonical_utc_timestamp(expires_at)
        if canonical_expiry is None:
            raise _memory().StrategyTransferTrialError("trial expiry must be timezone-aware")
        created = _memory().datetime.fromisoformat(created_at)
        expiry = _memory().datetime.fromisoformat(canonical_expiry)
        if not created < expiry <= created + _memory().timedelta(days=_memory().TRIAL_MAX_DAYS):
            raise _memory().StrategyTransferTrialError(
                "trial expiry must be in the next fourteen days"
            )
        binding = {
            "evaluator_version": safe_version,
            **pins,
        }
        if not self._strategy_transfer_trial_benchmark_matches(binding):
            raise _memory().StrategyTransferTrialError(
                "trial pins do not match a valid sealed benchmark"
            )
        target_json = self._strategy_transfer_canonical_json(normalized_families)
        caps_json = self._strategy_transfer_canonical_json(caps)
        strategies_json = self._strategy_transfer_canonical_json(
            normalized_strategies
        )
        material = self._strategy_transfer_trial_manifest_material(
            created_at=created_at,
            expires_at=canonical_expiry,
            project_id=normalized_project,
            target_families=normalized_families,
            family_cap_values=caps,
            strategies=normalized_strategies,
            sample_cap=sample_cap,
            seed=normalized_seed,
            evaluator_version=safe_version,
            evaluator_sha256=pins["evaluator_sha256"],
            fixture_sha256=pins["fixture_sha256"],
            config_sha256=pins["config_sha256"],
            runtime_sha256=pins["runtime_sha256"],
        )
        manifest_sha256 = _memory().sha256_json(material)
        state = self._strategy_transfer_trial_state_material(
            manifest_sha256=manifest_sha256,
            updated_at=created_at,
            status="active",
            status_reason=None,
            closed_at=None,
            promoted_at=None,
        )
        with self._immediate_transaction():
            existing_seed = self.db.execute(
                """SELECT * FROM strategy_transfer_trial_manifests
                   WHERE project_id=? AND seed=?""",
                (normalized_project, normalized_seed),
            ).fetchone()
            if existing_seed is not None:
                valid, existing = self._strategy_transfer_trial_manifest_validation(
                    existing_seed
                )
                if (
                    valid
                    and isinstance(existing, dict)
                    and existing["project_id"] == normalized_project
                    and existing["target_families"] == normalized_families
                    and existing["family_caps"] == caps
                    and existing["strategies"] == normalized_strategies
                    and existing["sample_cap"] == sample_cap
                    and existing["evaluator_version"] == safe_version
                    and all(existing[key] == value for key, value in pins.items())
                ):
                    return self.strategy_transfer_trial_status(
                        int(existing_seed["id"])
                    )
                raise _memory().StrategyTransferTrialError(
                    "trial seed is already bound to another manifest"
                )
            active_rows = self.db.execute(
                """SELECT * FROM strategy_transfer_trial_manifests
                   WHERE project_id=? AND status='active'""",
                (normalized_project,),
            ).fetchall()
            for active_row in active_rows:
                valid, active = self._strategy_transfer_trial_manifest_validation(
                    active_row
                )
                if not valid or not isinstance(active, dict):
                    raise _memory().StrategyTransferTrialError(
                        "an active trial manifest is invalid"
                    )
                if set(active["target_families"]).intersection(
                    normalized_families
                ):
                    raise _memory().StrategyTransferTrialError(
                        "target family already has an active trial"
                    )
            cursor = self.db.execute(
                """INSERT INTO strategy_transfer_trial_manifests(
                       created_at, updated_at, expires_at, project_id,
                       target_families_json, family_caps_json, strategies_json,
                       sample_cap, block_size, seed, evaluator_version,
                       evaluator_sha256, fixture_sha256, config_sha256,
                       runtime_sha256, operator_confirmed, status, status_reason,
                       closed_at, promoted_at, manifest_sha256, state_sha256
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1,
                             'active', NULL, NULL, NULL, ?, ?)""",
                (
                    created_at, created_at, canonical_expiry, normalized_project,
                    target_json, caps_json, strategies_json, sample_cap,
                    _memory().TRIAL_BLOCK_SIZE, normalized_seed, safe_version,
                    pins["evaluator_sha256"], pins["fixture_sha256"],
                    pins["config_sha256"], pins["runtime_sha256"],
                    manifest_sha256, _memory().sha256_json(state),
                ),
            )
            manifest_id = int(cursor.lastrowid)
        return self.strategy_transfer_trial_status(manifest_id)

    @staticmethod
    def _strategy_transfer_trial_selection_material(
        *,
        prediction_id: int,
        target_family: str,
        selection: Mapping[str, Any],
        flattened: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        """Return the closed selector subset; never persist model/user prose."""
        advice = selection.get("advice")
        if not isinstance(advice, list):
            raise _memory().StrategyTransferTrialError("trial selection advice is unavailable")
        strategies = sorted(str(item["strategy"]) for item in advice)
        receipts = sorted(
            {
                (int(item["memory_id"]), str(item["strategy"]))
                for item in flattened
            }
        )
        return {
            "schema": "jarvis.strategy-transfer-trial-selection.v1",
            "prediction_id": int(prediction_id),
            "target_family": str(target_family),
            "strategies": strategies,
            "receipts": [
                {"lesson_id": f"lesson:{memory_id}", "strategy": strategy}
                for memory_id, strategy in receipts
            ],
        }

    @staticmethod
    def _strategy_transfer_trial_assignment_material(
        *,
        manifest_sha256: str,
        created_at: str,
        prediction_id: int,
        project_id: int,
        target_family: str,
        family_sequence: int,
        block_index: int,
        block_slot: int,
        arm: str,
        strategies: Sequence[str],
        selection_sha256: str,
    ) -> dict[str, Any]:
        return {
            "schema": _memory().TRIAL_ASSIGNMENT_SCHEMA,
            "manifest_sha256": str(manifest_sha256),
            "created_at": str(created_at),
            "prediction_id": int(prediction_id),
            "project_id": int(project_id),
            "target_family": str(target_family),
            "family_sequence": int(family_sequence),
            "block_index": int(block_index),
            "block_slot": int(block_slot),
            "arm": str(arm),
            "strategies": list(strategies),
            "selection_sha256": str(selection_sha256),
        }

    @staticmethod
    def _strategy_transfer_trial_prompt_material(
        *,
        assignment_sha256: str,
        prompt_recorded_at: str,
        base_prompt_sha256: str,
        final_prompt_sha256: str,
        advice_applied: bool,
    ) -> dict[str, Any]:
        return {
            "schema": _memory().TRIAL_PROMPT_RECEIPT_SCHEMA,
            "assignment_sha256": str(assignment_sha256),
            "prompt_recorded_at": str(prompt_recorded_at),
            "base_prompt_sha256": str(base_prompt_sha256),
            "final_prompt_sha256": str(final_prompt_sha256),
            "advice_applied": bool(advice_applied),
        }

    @staticmethod
    def _strategy_transfer_trial_dispatch_material(
        *,
        assignment_sha256: str,
        prompt_receipt_sha256: str,
        provider_dispatched_at: str,
    ) -> dict[str, Any]:
        return {
            "schema": "jarvis.strategy-transfer-trial-provider-dispatch.v1",
            "assignment_sha256": str(assignment_sha256),
            "prompt_receipt_sha256": str(prompt_receipt_sha256),
            "provider_dispatched_at": str(provider_dispatched_at),
        }

    @staticmethod
    def _strategy_transfer_trial_outcome_material(
        *,
        assignment_sha256: str,
        prompt_receipt_sha256: str | None,
        status: str,
        status_reason: str | None,
        resolved_at: str,
        successful: int | None,
    ) -> dict[str, Any]:
        return {
            "schema": "jarvis.strategy-transfer-trial-outcome.v1",
            "assignment_sha256": str(assignment_sha256),
            "prompt_receipt_sha256": prompt_receipt_sha256,
            "status": str(status),
            "status_reason": status_reason,
            "resolved_at": str(resolved_at),
            "successful": successful,
        }

    def _strategy_transfer_trial_assignment_payload(
        self,
        row: Mapping[str, Any],
    ) -> dict[str, Any]:
        strategies = _memory().json.loads(str(row["strategies_json"]))
        arm = str(row["arm"])
        return {
            "schema": _memory().TRIAL_ASSIGNMENT_SCHEMA,
            "manifest_id": int(row["manifest_id"]),
            "prediction_id": int(row["prediction_id"]),
            "project_id": int(row["project_id"]),
            "target_family": str(row["target_family"]),
            "family_sequence": int(row["family_sequence"]),
            "block_index": int(row["block_index"]),
            "block_slot": int(row["block_slot"]),
            "arm": arm,
            "apply_advice": arm == "treatment",
            "strategies": strategies,
            "selection_sha256": str(row["selection_sha256"]),
            "assignment_sha256": str(row["assignment_sha256"]),
        }

    def _strategy_transfer_trial_assignment_validation(
        self,
        row: Mapping[str, Any],
        *,
        require_prompt: bool = False,
        require_dispatch: bool = False,
    ) -> tuple[bool, str]:
        try:
            manifest_row = self.db.execute(
                "SELECT * FROM strategy_transfer_trial_manifests WHERE id=?",
                (int(row["manifest_id"]),),
            ).fetchone()
            if manifest_row is None:
                return False, "assignment_manifest_missing"
            valid, manifest = self._strategy_transfer_trial_manifest_validation(
                manifest_row
            )
            if not valid or not isinstance(manifest, dict):
                return False, "assignment_manifest_invalid"
            prediction = self._strategy_prediction_row(int(row["prediction_id"]))
            if prediction is None:
                return False, "assignment_prediction_missing"
            prediction_project = self._strategy_prediction_project(prediction)
            if (
                prediction_project != int(row["project_id"])
                or prediction_project != int(manifest["project_id"])
                or str(prediction["family"]) != str(row["target_family"])
            ):
                return False, "assignment_prediction_scope_mismatch"
            strategies = _memory().json.loads(str(row["strategies_json"]))
            created_at = self._canonical_utc_timestamp(row["created_at"])
            prediction_created = self._canonical_utc_timestamp(
                prediction["created_at"]
            )
            if (
                created_at is None or prediction_created is None
                or str(row["created_at"]) != created_at
                or _memory().datetime.fromisoformat(created_at)
                < _memory().datetime.fromisoformat(prediction_created)
                or _memory().datetime.fromisoformat(created_at)
                > _memory().datetime.now(_memory().timezone.utc) + _memory().timedelta(minutes=5)
            ):
                return False, "assignment_timestamp_invalid"
            if (
                not isinstance(strategies, list)
                or strategies != sorted(strategies)
                or len(strategies) != len(set(strategies))
                or not strategies
                or strategies != list(manifest["strategies"])
            ):
                return False, "assignment_strategy_invalid"
            sequence = int(row["family_sequence"])
            block_index, block_slot = divmod(sequence, _memory().TRIAL_BLOCK_SIZE)
            arm = _memory().arm_for_slot(
                seed=manifest["seed"],
                target_family=str(row["target_family"]),
                block_index=block_index,
                block_slot=block_slot,
            )
            if (
                int(row["block_index"]) != block_index
                or int(row["block_slot"]) != block_slot
                or str(row["arm"]) != arm
            ):
                return False, "assignment_randomization_mismatch"
            material = self._strategy_transfer_trial_assignment_material(
                manifest_sha256=manifest["manifest_sha256"],
                created_at=created_at,
                prediction_id=int(row["prediction_id"]),
                project_id=int(row["project_id"]),
                target_family=str(row["target_family"]),
                family_sequence=sequence,
                block_index=block_index,
                block_slot=block_slot,
                arm=arm,
                strategies=strategies,
                selection_sha256=_memory().validated_sha256(
                    row["selection_sha256"], "selection"
                ),
            )
            assignment_sha256 = _memory().sha256_json(material)
            if str(row["assignment_sha256"]) != assignment_sha256:
                return False, "assignment_digest_mismatch"
            prompt_recorded = row["prompt_recorded_at"] is not None
            if require_prompt and not prompt_recorded:
                return False, "prompt_receipt_missing"
            if prompt_recorded:
                prompt_recorded_at = self._canonical_utc_timestamp(
                    row["prompt_recorded_at"]
                )
                if (
                    prompt_recorded_at is None
                    or str(row["prompt_recorded_at"]) != prompt_recorded_at
                    or _memory().datetime.fromisoformat(prompt_recorded_at)
                    < _memory().datetime.fromisoformat(created_at)
                ):
                    return False, "prompt_receipt_timestamp_invalid"
                base = _memory().validated_sha256(row["base_prompt_sha256"], "base prompt")
                final = _memory().validated_sha256(row["final_prompt_sha256"], "final prompt")
                applied = bool(int(row["advice_applied"]))
                if (arm == "control" and (applied or base != final)) or (
                    arm == "treatment" and (not applied or base == final)
                ):
                    return False, "prompt_arm_mismatch"
                prompt_material = self._strategy_transfer_trial_prompt_material(
                    assignment_sha256=assignment_sha256,
                    prompt_recorded_at=prompt_recorded_at,
                    base_prompt_sha256=base,
                    final_prompt_sha256=final,
                    advice_applied=applied,
                )
                if str(row["prompt_receipt_sha256"]) != _memory().sha256_json(prompt_material):
                    return False, "prompt_receipt_digest_mismatch"
            dispatched = row["provider_dispatched_at"] is not None
            dispatched_at: str | None = None
            if require_dispatch and not dispatched:
                return False, "provider_dispatch_receipt_missing"
            if dispatched:
                if not prompt_recorded:
                    return False, "provider_dispatch_precedes_prompt"
                dispatched_at = self._canonical_utc_timestamp(
                    row["provider_dispatched_at"]
                )
                if (
                    dispatched_at is None
                    or str(row["provider_dispatched_at"]) != dispatched_at
                    or _memory().datetime.fromisoformat(dispatched_at)
                    < _memory().datetime.fromisoformat(str(row["prompt_recorded_at"]))
                ):
                    return False, "provider_dispatch_timestamp_invalid"
                dispatch = self._strategy_transfer_trial_dispatch_material(
                    assignment_sha256=assignment_sha256,
                    prompt_receipt_sha256=str(row["prompt_receipt_sha256"]),
                    provider_dispatched_at=dispatched_at,
                )
                if str(row["provider_dispatch_sha256"]) != _memory().sha256_json(dispatch):
                    return False, "provider_dispatch_digest_mismatch"
            status = str(row["status"])
            if status not in _memory().TRIAL_ASSIGNMENT_STATUSES:
                return False, "assignment_status_invalid"
            if status != "assigned":
                resolved_at = self._canonical_utc_timestamp(row["resolved_at"])
                if resolved_at is None or str(row["resolved_at"]) != resolved_at:
                    return False, "assignment_outcome_timestamp_invalid"
                if (
                    status in {"resolved", "contaminated"}
                    and (
                        dispatched_at is None
                        or _memory().datetime.fromisoformat(dispatched_at)
                        >= _memory().datetime.fromisoformat(resolved_at)
                    )
                ):
                    return False, "provider_dispatch_outcome_order_invalid"
                successful = (
                    None if row["successful"] is None else int(row["successful"])
                )
                outcome = self._strategy_transfer_trial_outcome_material(
                    assignment_sha256=assignment_sha256,
                    prompt_receipt_sha256=(
                        None if row["prompt_receipt_sha256"] is None
                        else str(row["prompt_receipt_sha256"])
                    ),
                    status=status,
                    status_reason=(
                        None if row["status_reason"] is None
                        else str(row["status_reason"])
                    ),
                    resolved_at=resolved_at,
                    successful=successful,
                )
                if str(row["outcome_sha256"]) != _memory().sha256_json(outcome):
                    return False, "assignment_outcome_digest_mismatch"
        except (
            _memory().json.JSONDecodeError,
            KeyError,
            OSError,
            _memory().sqlite3.DatabaseError,
            _memory().StrategyTransferTrialError,
            TypeError,
            ValueError,
        ):
            return False, "assignment_validation_unavailable"
        return True, "valid"

    def active_strategy_transfer_trial(
        self,
        project_id: int,
        target_family: str,
        current_runtime_sha256: str,
    ) -> dict[str, Any] | None:
        """Return exactly one eligible manifest; ambiguity and corruption close."""
        if target_family not in self.PREDICTION_FAMILIES:
            raise ValueError(f"Unknown target family: {target_family}")
        normalized_project = self._project_id(project_id)
        try:
            rows = self.db.execute(
                """SELECT * FROM strategy_transfer_trial_manifests
                   WHERE project_id=? AND status='active' ORDER BY id""",
                (normalized_project,),
            ).fetchall()
        except _memory().sqlite3.DatabaseError:
            self._strategy_transfer_trial_telemetry = {
                "available": False, "reason": "manifest_query_unavailable"
            }
            return None
        eligible: list[dict[str, Any]] = []
        rejected: list[str] = []
        for row in rows:
            if target_family not in str(row["target_families_json"]):
                continue
            valid, result = self._strategy_transfer_trial_manifest_eligibility(
                row,
                target_family=target_family,
                current_runtime_sha256=current_runtime_sha256,
            )
            if valid and isinstance(result, dict):
                eligible.append(result)
            else:
                rejected.append(str(result))
        reason = "available" if len(eligible) == 1 else (
            "manifest_ambiguous" if len(eligible) > 1 else
            (rejected[0] if rejected else "manifest_absent")
        )
        self._strategy_transfer_trial_telemetry = {
            "available": len(eligible) == 1,
            "reason": reason,
            "eligible": len(eligible),
            "rejected": len(rejected),
        }
        if len(eligible) != 1:
            return None
        result = dict(eligible[0])
        result.pop("seed", None)
        return result

    def assign_strategy_transfer_trial(
        self,
        prediction_id: int,
        target_family: str,
        selection: Mapping[str, Any],
        *,
        manifest_id: int | None = None,
        current_runtime_sha256: str,
    ) -> dict[str, Any]:
        """Persist the randomized arm before prompt construction/provider use."""
        normalized_prediction = self._prediction_optional_id(
            prediction_id, "prediction_id"
        )
        if target_family not in self.PREDICTION_FAMILIES:
            raise ValueError(f"Unknown target family: {target_family}")
        flattened = self._strategy_transfer_selection_rows(
            selection, target_family=target_family
        )
        if str(selection.get("task_id")) != f"prediction:{normalized_prediction}":
            raise _memory().StrategyTransferTrialError("trial selection prediction mismatches")
        selection_material = self._strategy_transfer_trial_selection_material(
            prediction_id=normalized_prediction,
            target_family=target_family,
            selection=selection,
            flattened=flattened,
        )
        selection_sha256 = _memory().sha256_json(selection_material)
        selected_strategies = list(selection_material["strategies"])
        if not selected_strategies:
            raise _memory().StrategyTransferTrialError("trial requires selected strategy advice")
        with self._immediate_transaction():
            prediction = self._strategy_prediction_row(normalized_prediction)
            if prediction is None or prediction["resolved_at"] is not None:
                raise _memory().StrategyTransferTrialError("trial prediction is not active")
            if str(prediction["family"]) != target_family:
                raise _memory().StrategyTransferTrialError("trial prediction family mismatches")
            project_id = self._strategy_prediction_project(prediction)
            if project_id is None:
                raise _memory().StrategyTransferTrialError("trial prediction project is unavailable")
            existing = self.db.execute(
                "SELECT * FROM strategy_transfer_trial_assignments WHERE prediction_id=?",
                (normalized_prediction,),
            ).fetchone()
            if existing is not None:
                valid, _reason = self._strategy_transfer_trial_assignment_validation(
                    existing
                )
                if (
                    valid
                    and str(existing["target_family"]) == target_family
                    and str(existing["selection_sha256"]) == selection_sha256
                    and (manifest_id is None or int(existing["manifest_id"]) == manifest_id)
                ):
                    return self._strategy_transfer_trial_assignment_payload(existing)
                raise _memory().StrategyTransferTrialError("conflicting trial assignment replay")
            if manifest_id is None:
                active = self.active_strategy_transfer_trial(
                    project_id, target_family, current_runtime_sha256
                )
                if active is None:
                    raise _memory().StrategyTransferTrialError(
                        "no single eligible strategy-transfer trial is active"
                    )
                normalized_manifest = int(active["manifest_id"])
            else:
                normalized_manifest = self._prediction_optional_id(
                    manifest_id, "manifest_id"
                )
            manifest_row = self.db.execute(
                "SELECT * FROM strategy_transfer_trial_manifests WHERE id=?",
                (normalized_manifest,),
            ).fetchone()
            if manifest_row is None:
                raise _memory().StrategyTransferTrialError("trial manifest is unavailable")
            eligible, manifest = self._strategy_transfer_trial_manifest_eligibility(
                manifest_row,
                target_family=target_family,
                current_runtime_sha256=current_runtime_sha256,
            )
            if not eligible or not isinstance(manifest, dict):
                raise _memory().StrategyTransferTrialError(f"trial unavailable: {manifest}")
            if int(manifest["project_id"]) != project_id:
                raise _memory().StrategyTransferTrialError("trial project mismatches prediction")
            if selected_strategies != list(manifest["strategies"]):
                raise _memory().StrategyTransferTrialError(
                    "selection must match the predeclared trial strategy set"
                )
            sequence = int(self.db.execute(
                """SELECT COUNT(*) FROM strategy_transfer_trial_assignments
                   WHERE manifest_id=? AND target_family=?""",
                (normalized_manifest, target_family),
            ).fetchone()[0])
            if sequence >= int(manifest["family_caps"][target_family]):
                raise _memory().StrategyTransferTrialError("trial family sample cap reached")
            block_index, block_slot = divmod(sequence, _memory().TRIAL_BLOCK_SIZE)
            arm = _memory().arm_for_slot(
                seed=manifest["seed"], target_family=target_family,
                block_index=block_index, block_slot=block_slot,
            )
            stamp = _memory().now_iso()
            material = self._strategy_transfer_trial_assignment_material(
                manifest_sha256=manifest["manifest_sha256"],
                created_at=stamp,
                prediction_id=normalized_prediction,
                project_id=project_id,
                target_family=target_family,
                family_sequence=sequence,
                block_index=block_index,
                block_slot=block_slot,
                arm=arm,
                strategies=selected_strategies,
                selection_sha256=selection_sha256,
            )
            assignment_sha256 = _memory().sha256_json(material)
            cursor = self.db.execute(
                """INSERT INTO strategy_transfer_trial_assignments(
                       manifest_id, prediction_id, created_at, project_id,
                       target_family, family_sequence, block_index, block_slot,
                       arm, strategies_json, selection_sha256,
                       assignment_sha256, status
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'assigned')""",
                (
                    normalized_manifest, normalized_prediction, stamp, project_id,
                    target_family, sequence, block_index, block_slot, arm,
                    self._strategy_transfer_canonical_json(selected_strategies),
                    selection_sha256, assignment_sha256,
                ),
            )
            row = self.db.execute(
                "SELECT * FROM strategy_transfer_trial_assignments WHERE id=?",
                (int(cursor.lastrowid),),
            ).fetchone()
            if row is None or not self._strategy_transfer_trial_assignment_validation(row)[0]:
                raise _memory().StrategyTransferTrialError("persisted trial assignment is invalid")
            return self._strategy_transfer_trial_assignment_payload(row)

    def record_strategy_transfer_trial_prompt_receipt(
        self,
        prediction_id: int,
        *,
        base_prompt_sha256: str,
        final_prompt_sha256: str,
        advice_applied: bool,
    ) -> bool:
        """Bind the final/base prompt hashes before the provider can be called."""
        normalized_prediction = self._prediction_optional_id(
            prediction_id, "prediction_id"
        )
        base = _memory().validated_sha256(base_prompt_sha256, "base prompt")
        final = _memory().validated_sha256(final_prompt_sha256, "final prompt")
        if not isinstance(advice_applied, bool):
            raise _memory().StrategyTransferTrialError("advice_applied must be boolean")
        with self._immediate_transaction():
            row = self.db.execute(
                "SELECT * FROM strategy_transfer_trial_assignments WHERE prediction_id=?",
                (normalized_prediction,),
            ).fetchone()
            if row is None or not self._strategy_transfer_trial_assignment_validation(row)[0]:
                raise _memory().StrategyTransferTrialError("trial assignment is unavailable")
            expected_applied = str(row["arm"]) == "treatment"
            if advice_applied != expected_applied or (
                expected_applied and base == final
            ) or (not expected_applied and base != final):
                raise _memory().StrategyTransferTrialError(
                    "prompt receipt does not match its randomized arm"
                )
            if row["prompt_recorded_at"] is not None:
                existing_material = self._strategy_transfer_trial_prompt_material(
                    assignment_sha256=str(row["assignment_sha256"]),
                    prompt_recorded_at=str(row["prompt_recorded_at"]),
                    base_prompt_sha256=base,
                    final_prompt_sha256=final,
                    advice_applied=advice_applied,
                )
                if str(row["prompt_receipt_sha256"]) == _memory().sha256_json(
                    existing_material
                ):
                    return False
                raise _memory().StrategyTransferTrialError("conflicting prompt receipt replay")
            prompt_stamp = _memory().now_iso()
            material = self._strategy_transfer_trial_prompt_material(
                assignment_sha256=str(row["assignment_sha256"]),
                prompt_recorded_at=prompt_stamp,
                base_prompt_sha256=base,
                final_prompt_sha256=final,
                advice_applied=advice_applied,
            )
            digest = _memory().sha256_json(material)
            updated = self.db.execute(
                """UPDATE strategy_transfer_trial_assignments
                   SET prompt_recorded_at=?, base_prompt_sha256=?,
                       final_prompt_sha256=?, advice_applied=?,
                       prompt_receipt_sha256=?
                   WHERE prediction_id=? AND prompt_recorded_at IS NULL
                     AND status='assigned'""",
                (
                    prompt_stamp, base, final, int(advice_applied), digest,
                    normalized_prediction,
                ),
            )
            if updated.rowcount != 1:
                raise _memory().StrategyTransferTrialError("prompt receipt was not persisted")
            persisted = self.db.execute(
                "SELECT * FROM strategy_transfer_trial_assignments WHERE prediction_id=?",
                (normalized_prediction,),
            ).fetchone()
            if persisted is None or not self._strategy_transfer_trial_assignment_validation(
                persisted, require_prompt=True
            )[0]:
                raise _memory().StrategyTransferTrialError("persisted prompt receipt is invalid")
            return True

    def record_strategy_transfer_trial_provider_dispatch(
        self,
        prediction_id: int,
    ) -> bool:
        """Seal a dispatch boundary after prompt/application receipts exist."""
        normalized_prediction = self._prediction_optional_id(
            prediction_id, "prediction_id"
        )
        with self._immediate_transaction():
            row = self.db.execute(
                "SELECT * FROM strategy_transfer_trial_assignments WHERE prediction_id=?",
                (normalized_prediction,),
            ).fetchone()
            if row is None:
                raise _memory().StrategyTransferTrialError(
                    "provider dispatch requires a valid prompt receipt"
                )
            dispatch_eligible, dispatch_reason = (
                self._strategy_transfer_trial_dispatch_eligibility(row)
            )
            if not dispatch_eligible:
                raise _memory().StrategyTransferTrialError(
                    f"provider dispatch eligibility failed: {dispatch_reason}"
                )
            if not self._strategy_transfer_trial_assignment_validation(
                row, require_prompt=True
            )[0]:
                raise _memory().StrategyTransferTrialError(
                    "provider dispatch requires a valid prompt receipt"
                )
            applications = self.db.execute(
                """SELECT id, created_at, mode, applied, strategy
                   FROM strategy_transfer_applications
                   WHERE prediction_id=? ORDER BY id""",
                (normalized_prediction,),
            ).fetchall()
            expected_strategies = set(_memory().json.loads(str(row["strategies_json"])))
            expected_applied = str(row["arm"]) == "treatment"
            if (
                not applications
                or {str(item["strategy"]) for item in applications}
                != expected_strategies
                or any(
                    str(item["mode"]) != "trial"
                    or bool(int(item["applied"])) != expected_applied
                    or not self._strategy_transfer_application_validation(
                        int(item["id"])
                    )[0]
                    for item in applications
                )
            ):
                raise _memory().StrategyTransferTrialError(
                    "provider dispatch requires exact trial application receipts"
                )
            stamp = _memory().now_iso()
            dispatch = self._strategy_transfer_trial_dispatch_material(
                assignment_sha256=str(row["assignment_sha256"]),
                prompt_receipt_sha256=str(row["prompt_receipt_sha256"]),
                provider_dispatched_at=stamp,
            )
            digest = _memory().sha256_json(dispatch)
            if row["provider_dispatched_at"] is not None:
                if str(row["provider_dispatch_sha256"]) == digest:
                    return False
                # The timestamp is intentionally immutable; replay is checked
                # against persisted material rather than a newly generated time.
                persisted = self._strategy_transfer_trial_dispatch_material(
                    assignment_sha256=str(row["assignment_sha256"]),
                    prompt_receipt_sha256=str(row["prompt_receipt_sha256"]),
                    provider_dispatched_at=str(row["provider_dispatched_at"]),
                )
                if str(row["provider_dispatch_sha256"]) == _memory().sha256_json(persisted):
                    return False
                raise _memory().StrategyTransferTrialError(
                    "conflicting provider dispatch receipt replay"
                )
            updated = self.db.execute(
                """UPDATE strategy_transfer_trial_assignments
                   SET provider_dispatched_at=?, provider_dispatch_sha256=?
                   WHERE prediction_id=? AND provider_dispatched_at IS NULL
                     AND status='assigned'""",
                (stamp, digest, normalized_prediction),
            )
            if updated.rowcount != 1:
                raise _memory().StrategyTransferTrialError(
                    "provider dispatch receipt was not persisted"
                )
            persisted = self.db.execute(
                "SELECT * FROM strategy_transfer_trial_assignments WHERE prediction_id=?",
                (normalized_prediction,),
            ).fetchone()
            if persisted is None or not self._strategy_transfer_trial_assignment_validation(
                persisted, require_prompt=True, require_dispatch=True
            )[0]:
                raise _memory().StrategyTransferTrialError(
                    "persisted provider dispatch receipt is invalid"
                )
            return True

    def strategy_transfer_trial_pins(self) -> dict[str, str]:
        """Return exact installed, independently validated trial contract pins."""
        benchmark = self._strategy_transfer_benchmark_row()
        if benchmark is None:
            raise _memory().StrategyTransferTrialError(
                "a valid sealed strategy-transfer benchmark is required"
            )
        contract = self._strategy_transfer_trial_contract()
        try:
            runtime_sha256 = _memory().strategy_transfer_runtime_sha256()
        except OSError as exc:
            raise _memory().StrategyTransferTrialError(
                "the installed strategy-transfer runtime cannot be sealed"
            ) from exc
        return {
            "evaluator_version": str(contract["evaluator_version"]),
            "evaluator_sha256": str(contract["evaluator_sha256"]),
            "fixture_sha256": str(contract["fixture_sha256"]),
            "config_sha256": str(contract["config_sha256"]),
            "runtime_sha256": runtime_sha256,
        }

    def _strategy_transfer_trial_update_manifest_state(
        self,
        manifest_row: Mapping[str, Any],
        *,
        status: str,
        status_reason: str | None,
        stamp: str,
        promoted: bool = False,
    ) -> None:
        if status not in _memory().TRIAL_MANIFEST_STATUSES:
            raise _memory().StrategyTransferTrialError("trial manifest status is invalid")
        closed_at = None if status == "active" else stamp
        promoted_at = stamp if promoted else None
        state = self._strategy_transfer_trial_state_material(
            manifest_sha256=str(manifest_row["manifest_sha256"]),
            updated_at=stamp,
            status=status,
            status_reason=status_reason,
            closed_at=closed_at,
            promoted_at=promoted_at,
        )
        updated = self.db.execute(
            """UPDATE strategy_transfer_trial_manifests
               SET updated_at=?, status=?, status_reason=?, closed_at=?,
                   promoted_at=?, state_sha256=? WHERE id=?""",
            (
                stamp, status, status_reason, closed_at, promoted_at,
                _memory().sha256_json(state), int(manifest_row["id"]),
            ),
        )
        if updated.rowcount != 1:
            raise _memory().StrategyTransferTrialError("trial manifest state update failed")

    def _resolve_strategy_transfer_trial_assignment(
        self,
        prediction_id: int,
        *,
        stamp: str,
        actual_status: str,
        evidence_ok: bool | None,
    ) -> None:
        row = self.db.execute(
            "SELECT * FROM strategy_transfer_trial_assignments WHERE prediction_id=?",
            (int(prediction_id),),
        ).fetchone()
        if row is None or str(row["status"]) != "assigned":
            return
        valid, reason = self._strategy_transfer_trial_assignment_validation(
            row, require_prompt=True, require_dispatch=True
        )
        contamination_reason: str | None = None
        if not valid:
            contamination_reason = (
                "prompt_receipt_missing" if reason == "prompt_receipt_missing"
                else "provider_dispatch_missing"
                if reason == "provider_dispatch_receipt_missing"
                else "assignment_integrity"
            )
        else:
            manifest_row = self.db.execute(
                "SELECT * FROM strategy_transfer_trial_manifests WHERE id=?",
                (int(row["manifest_id"]),),
            ).fetchone()
            manifest_valid, manifest = (
                (False, "manifest_missing") if manifest_row is None
                else self._strategy_transfer_trial_manifest_validation(manifest_row)
            )
            if not manifest_valid or not isinstance(manifest, dict):
                contamination_reason = "manifest_drift"
            else:
                try:
                    current_runtime = _memory().strategy_transfer_runtime_sha256()
                except OSError:
                    current_runtime = ""
                if current_runtime != manifest["runtime_sha256"]:
                    contamination_reason = "runtime_drift"
                elif not self._strategy_transfer_trial_benchmark_matches(manifest):
                    contamination_reason = "manifest_drift"
        applications = self.db.execute(
            """SELECT * FROM strategy_transfer_applications
               WHERE prediction_id=? ORDER BY rank, id""",
            (int(prediction_id),),
        ).fetchall()
        expected_applied = str(row["arm"]) == "treatment"
        expected_strategies = set(_memory().json.loads(str(row["strategies_json"])))
        actual_strategies: set[str] = set()
        if not applications:
            contamination_reason = contamination_reason or "application_receipt_invalid"
        for application in applications:
            actual_strategies.add(str(application["strategy"]))
            if (
                str(application["mode"]) != "trial"
                or bool(int(application["applied"])) != expected_applied
                or not self._strategy_transfer_application_validation(
                    int(application["id"])
                )[0]
            ):
                contamination_reason = "application_receipt_invalid"
        if actual_strategies != expected_strategies:
            contamination_reason = "application_receipt_invalid"
        ledger = self._strategy_transfer_ledger_health()
        if not ledger["available"] or int(ledger["invalid_receipts"]):
            contamination_reason = "application_receipt_invalid"
        elif int(ledger["harm_quarantines"]):
            contamination_reason = "quarantine_detected"
        if contamination_reason is None:
            dispatched_at = self._canonical_utc_timestamp(
                row["provider_dispatched_at"]
            )
            resolved_at = self._canonical_utc_timestamp(stamp)
            if (
                dispatched_at is None
                or resolved_at is None
                or _memory().datetime.fromisoformat(dispatched_at)
                >= _memory().datetime.fromisoformat(resolved_at)
            ):
                contamination_reason = "assignment_integrity"
        if contamination_reason is None:
            status = "resolved"
            successful: int | None = int(
                actual_status == "complete" and evidence_ok is True
            )
        else:
            status = "contaminated"
            successful = None
        outcome = self._strategy_transfer_trial_outcome_material(
            assignment_sha256=str(row["assignment_sha256"]),
            prompt_receipt_sha256=(
                None if row["prompt_receipt_sha256"] is None
                else str(row["prompt_receipt_sha256"])
            ),
            status=status,
            status_reason=contamination_reason,
            resolved_at=stamp,
            successful=successful,
        )
        self.db.execute(
            """UPDATE strategy_transfer_trial_assignments
               SET status=?, status_reason=?, resolved_at=?, successful=?,
                   outcome_sha256=?
               WHERE prediction_id=? AND status='assigned'""",
            (
                status, contamination_reason, stamp, successful,
                _memory().sha256_json(outcome), int(prediction_id),
            ),
        )

    def abort_strategy_transfer_trial(
        self,
        manifest_id: int,
        *,
        reason_code: str = "operator_abort",
    ) -> bool:
        normalized_manifest = self._prediction_optional_id(
            manifest_id, "manifest_id"
        )
        if reason_code not in _memory().TRIAL_ABORT_REASONS:
            raise _memory().StrategyTransferTrialError("trial abort reason is unsupported")
        with self._immediate_transaction():
            manifest_row = self.db.execute(
                "SELECT * FROM strategy_transfer_trial_manifests WHERE id=?",
                (normalized_manifest,),
            ).fetchone()
            if manifest_row is None:
                raise _memory().StrategyTransferTrialError("trial manifest is unavailable")
            valid, manifest = self._strategy_transfer_trial_manifest_validation(
                manifest_row
            )
            if not valid or not isinstance(manifest, dict):
                raise _memory().StrategyTransferTrialError("trial manifest is invalid")
            if manifest["status"] == "aborted":
                if manifest["status_reason"] == reason_code:
                    return False
                raise _memory().StrategyTransferTrialError("conflicting trial abort replay")
            if manifest["status"] != "active":
                raise _memory().StrategyTransferTrialError("only active trials can be aborted")
            stamp = _memory().now_iso()
            pending = self.db.execute(
                """SELECT * FROM strategy_transfer_trial_assignments
                   WHERE manifest_id=? AND status='assigned' ORDER BY id""",
                (normalized_manifest,),
            ).fetchall()
            for row in pending:
                outcome = self._strategy_transfer_trial_outcome_material(
                    assignment_sha256=str(row["assignment_sha256"]),
                    prompt_receipt_sha256=(
                        None if row["prompt_receipt_sha256"] is None
                        else str(row["prompt_receipt_sha256"])
                    ),
                    status="aborted", status_reason="operator_abort",
                    resolved_at=stamp, successful=None,
                )
                self.db.execute(
                    """UPDATE strategy_transfer_trial_assignments
                       SET status='aborted', status_reason='operator_abort',
                           resolved_at=?, outcome_sha256=? WHERE id=?""",
                    (stamp, _memory().sha256_json(outcome), int(row["id"])),
                )
            self._strategy_transfer_trial_update_manifest_state(
                manifest_row, status="aborted", status_reason=reason_code,
                stamp=stamp,
            )
            return True

    def strategy_transfer_trial_status(
        self,
        manifest_id: int | None = None,
    ) -> dict[str, Any] | list[dict[str, Any]]:
        parameters: tuple[Any, ...]
        clause: str
        if manifest_id is None:
            clause, parameters = "", ()
        else:
            normalized = self._prediction_optional_id(manifest_id, "manifest_id")
            clause, parameters = "WHERE id=?", (normalized,)
        try:
            rows = self.db.execute(
                f"SELECT * FROM strategy_transfer_trial_manifests {clause} ORDER BY id",
                parameters,
            ).fetchall()
        except _memory().sqlite3.DatabaseError:
            return [] if manifest_id is None else {
                "schema": "jarvis.strategy-transfer-trial-status.v1",
                "available": False,
                "status_reason": "manifest_query_unavailable",
                "causal_attestation_valid": False,
                "promotion_ready": False,
            }
        results: list[dict[str, Any]] = []
        for row in rows:
            valid, manifest = self._strategy_transfer_trial_manifest_validation(row)
            if not valid or not isinstance(manifest, dict):
                results.append({
                    "schema": "jarvis.strategy-transfer-trial-status.v1",
                    "manifest_id": int(row["id"]),
                    "available": False,
                    "status": "invalid",
                    "effective_status": "invalid",
                    "status_reason": str(manifest),
                    "causal_attestation_valid": False,
                    "promotion_ready": False,
                })
                continue
            try:
                assignments = self.db.execute(
                    """SELECT * FROM strategy_transfer_trial_assignments
                       WHERE manifest_id=? ORDER BY target_family, family_sequence""",
                    (int(row["id"]),),
                ).fetchall()
            except _memory().sqlite3.DatabaseError:
                results.append({
                    "schema": "jarvis.strategy-transfer-trial-status.v1",
                    "manifest_id": int(row["id"]),
                    "project_id": manifest["project_id"],
                    "available": False,
                    "status": "unavailable",
                    "effective_status": "unavailable",
                    "status_reason": "assignment_query_unavailable",
                    "causal_attestation_valid": False,
                    "promotion_ready": False,
                })
                continue
            invalid = sum(
                1 for assignment in assignments
                if not self._strategy_transfer_trial_assignment_validation(assignment)[0]
            )
            counts = _memory().Counter(str(item["status"]) for item in assignments)
            arms = _memory().Counter(str(item["arm"]) for item in assignments)
            complete_blocks = 0
            for family in manifest["target_families"]:
                family_rows = [
                    item for item in assignments
                    if str(item["target_family"]) == family
                    and str(item["status"]) == "resolved"
                ]
                by_block: dict[int, list[Mapping[str, Any]]] = {}
                for item in family_rows:
                    by_block.setdefault(int(item["block_index"]), []).append(item)
                complete_blocks += sum(
                    1 for block in by_block.values()
                    if len(block) == _memory().TRIAL_BLOCK_SIZE
                    and _memory().Counter(str(item["arm"]) for item in block)
                    == _memory().Counter({"control": 2, "treatment": 2})
                )
            now = _memory().datetime.now(_memory().timezone.utc)
            expired = now >= _memory().datetime.fromisoformat(manifest["expires_at"])
            effective = "expired" if manifest["status"] == "active" and expired else manifest["status"]
            resolved = int(counts["resolved"])
            contaminated = int(counts["contaminated"])
            aborted = int(counts["aborted"])
            causal_valid = False
            try:
                attestation_rows = self.db.execute(
                    """SELECT * FROM strategy_transfer_attestations
                       WHERE kind='applied_ab'
                         AND assignment_manifest_sha256=? ORDER BY id DESC""",
                    (manifest["manifest_sha256"],),
                ).fetchall()
                causal_valid = any(
                    self._strategy_transfer_stored_attestation_validation(
                        candidate
                    )[0]
                    for candidate in attestation_rows
                )
            except _memory().sqlite3.DatabaseError:
                causal_valid = False
            structurally_ready = bool(
                manifest["status"] in {"active", "closed"}
                and not expired
                and len(assignments) == manifest["sample_cap"]
                and resolved == manifest["sample_cap"]
                and contaminated == 0 and aborted == 0 and invalid == 0
                and complete_blocks == manifest["sample_cap"] // _memory().TRIAL_BLOCK_SIZE
                and self._strategy_transfer_trial_benchmark_matches(manifest)
            )
            try:
                runtime_matches = (
                    _memory().strategy_transfer_runtime_sha256()
                    == manifest["runtime_sha256"]
                )
            except OSError:
                runtime_matches = False
            promotion_ready = False
            if structurally_ready and runtime_matches:
                try:
                    trial_attestation = (
                        self.build_strategy_transfer_trial_ab_attestation(
                            int(manifest["manifest_id"]),
                            run_id=f"trial-{int(manifest['manifest_id'])}-status",
                        )
                    )
                    promotion_ready = bool(
                        trial_attestation["all_exit_criteria"]
                    )
                except (
                    OSError, _memory().sqlite3.DatabaseError, _memory().StrategyTransferError,
                    _memory().StrategyTransferTrialError, TypeError, ValueError,
                ):
                    promotion_ready = False
            results.append({
                "schema": "jarvis.strategy-transfer-trial-status.v1",
                "manifest_id": manifest["manifest_id"],
                "project_id": manifest["project_id"],
                "available": True,
                "status": manifest["status"],
                "effective_status": effective,
                "status_reason": manifest["status_reason"],
                "created_at": manifest["created_at"],
                "updated_at": manifest["updated_at"],
                "expires_at": manifest["expires_at"],
                "target_families": manifest["target_families"],
                "family_caps": manifest["family_caps"],
                "strategies": manifest["strategies"],
                "sample_cap": manifest["sample_cap"],
                "block_size": _memory().TRIAL_BLOCK_SIZE,
                "assigned": len(assignments),
                "resolved": resolved,
                "contaminated": contaminated,
                "aborted_assignments": aborted,
                "control_assigned": int(arms["control"]),
                "treatment_assigned": int(arms["treatment"]),
                "remaining": max(0, manifest["sample_cap"] - len(assignments)),
                "complete_blocks": complete_blocks,
                "invalid_assignments": invalid,
                "manifest_sha256": manifest["manifest_sha256"],
                "runtime_sha256": manifest["runtime_sha256"],
                "evaluator_version": manifest["evaluator_version"],
                "evaluator_sha256": manifest["evaluator_sha256"],
                "fixture_sha256": manifest["fixture_sha256"],
                "config_sha256": manifest["config_sha256"],
                "causal_attestation_valid": causal_valid,
                "promotion_ready": promotion_ready,
            })
        if manifest_id is None:
            return results
        if not results:
            raise _memory().StrategyTransferTrialError("trial manifest is unavailable")
        return results[0]

    def promote_strategy_transfer_trial(
        self,
        manifest_id: int,
        *,
        operator_confirmed: bool,
    ) -> dict[str, Any]:
        """Seal successful causal evidence, then explicitly promote once."""
        if operator_confirmed is not True:
            raise _memory().StrategyTransferTrialError(
                "explicit operator confirmation is required for promotion"
            )
        normalized_manifest = self._prediction_optional_id(
            manifest_id, "manifest_id"
        )
        current = self.strategy_transfer_trial_status(normalized_manifest)
        if not isinstance(current, dict):
            raise _memory().StrategyTransferTrialError("trial status is unavailable")
        if current.get("status") == "promoted" and current.get(
            "causal_attestation_valid"
        ) is True:
            row = self.db.execute(
                """SELECT * FROM strategy_transfer_attestations
                   WHERE kind='applied_ab' AND assignment_manifest_sha256=?
                   ORDER BY id DESC LIMIT 1""",
                (str(current["manifest_sha256"]),),
            ).fetchone()
            artifact = {} if row is None else _memory().json.loads(str(row["artifact_json"]))
            return self._strategy_transfer_trial_promotion_payload(
                current, artifact, promoted=False
            )
        if current.get("promotion_ready") is not True:
            raise _memory().StrategyTransferTrialError(
                "trial causal evidence has not met every promotion gate"
            )
        artifact = self.build_strategy_transfer_trial_ab_attestation(
            normalized_manifest,
            run_id=f"trial-{normalized_manifest}-promotion",
        )
        if artifact["all_exit_criteria"] is not True:
            raise _memory().StrategyTransferTrialError("trial causal attestation failed")
        inserted = self.record_strategy_transfer_attestation(
            "applied_ab",
            artifact,
            evaluator_version=str(artifact["evaluator_version"]),
            evaluator_sha256=str(artifact["evaluator_sha256"]),
            config_sha256=str(artifact["config_sha256"]),
        )
        with self._immediate_transaction():
            row = self.db.execute(
                "SELECT * FROM strategy_transfer_trial_manifests WHERE id=?",
                (normalized_manifest,),
            ).fetchone()
            if row is None:
                raise _memory().StrategyTransferTrialError("trial manifest disappeared")
            valid, manifest = self._strategy_transfer_trial_manifest_validation(row)
            if not valid or not isinstance(manifest, dict):
                raise _memory().StrategyTransferTrialError("trial manifest became invalid")
            if manifest["status"] == "promoted":
                inserted = False
            elif manifest["status"] in {"active", "closed"}:
                self._strategy_transfer_trial_update_manifest_state(
                    row, status="promoted", status_reason="operator_promoted",
                    stamp=_memory().now_iso(), promoted=True,
                )
            else:
                raise _memory().StrategyTransferTrialError("trial cannot be promoted")
        final = self.strategy_transfer_trial_status(normalized_manifest)
        if not isinstance(final, dict) or final.get("causal_attestation_valid") is not True:
            raise _memory().StrategyTransferTrialError("promoted trial attestation is invalid")
        return self._strategy_transfer_trial_promotion_payload(
            final, artifact, promoted=bool(inserted)
        )

    @staticmethod
    def _strategy_transfer_trial_promotion_payload(
        status: Mapping[str, Any],
        artifact: Mapping[str, Any],
        *,
        promoted: bool,
    ) -> dict[str, Any]:
        counts = artifact.get("counts", {}) if isinstance(artifact, _memory().Mapping) else {}
        metrics = artifact.get("metrics", {}) if isinstance(artifact, _memory().Mapping) else {}
        return {
            "schema": "jarvis.strategy-transfer-trial-promotion.v1",
            "manifest_id": int(status["manifest_id"]),
            "project_id": int(status["project_id"]),
            "promoted": bool(promoted),
            "status": str(status["status"]),
            "attestation_sha256": artifact.get("attestation_sha256"),
            "artifact_sha256": artifact.get("attestation_sha256"),
            "control_predictions": int(counts.get("control_predictions", 0)),
            "treatment_predictions": int(counts.get("applied_predictions", 0)),
            "source_target_pairs": int(counts.get("source_target_pairs", 0)),
            "control_success_rate": metrics.get("control_success_rate"),
            "treatment_success_rate": metrics.get("applied_success_rate"),
            "lift_pp": metrics.get("lift_pp"),
        }

    @staticmethod
    def _strategy_transfer_canonical_json(value: Any) -> str:
        return _memory().json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    @classmethod
    def _strategy_transfer_config_digest(cls, config: Mapping[str, Any]) -> str:
        return _memory().hashlib.sha256(
            cls._strategy_transfer_canonical_json(dict(config)).encode("utf-8")
        ).hexdigest()

    @classmethod
    def _strategy_transfer_ab_attestation_digest(
        cls,
        artifact: Mapping[str, Any],
    ) -> str:
        keys = [
            "schema_version", "evaluator_version", "evaluator_sha256",
            "config", "config_sha256", "benchmark_attestation_sha256",
            "assignment_manifest_sha256", "control_prediction_ids",
            "applied_prediction_ids", "counts", "metrics", "passes",
            "all_exit_criteria", "claim_scope",
        ]
        if str(artifact.get("schema_version", "")).endswith("/v2"):
            keys.extend((
                "trial_evidence_artifact_sha256",
                "causal_evaluator_attestation_sha256",
            ))
        material = {key: artifact[key] for key in keys}
        return _memory().hashlib.sha256(
            cls._strategy_transfer_canonical_json(material).encode("utf-8")
        ).hexdigest()

    @staticmethod
    def _strategy_transfer_sha256(value: Any, label: str) -> str:
        text = str(value or "")
        if _memory().re.fullmatch(r"[0-9a-f]{64}", text) is None:
            raise ValueError(f"{label} must be a lowercase SHA-256 digest")
        return text

    def build_strategy_transfer_benchmark_attestation(
        self,
        *,
        run_id: str,
    ) -> dict[str, Any]:
        """Run the frozen holdout and preserve its evaluator-owned seal exactly."""
        safe_run_id = self._strategy_transfer_identifier(run_id, "run_id")
        from . import strategy_transfer_outcome_eval as outcome_eval

        evaluator_path = _memory().Path(str(outcome_eval.__file__)).resolve()
        fixture_path = (
            evaluator_path.parent
            / "evaluation_fixtures"
            / outcome_eval.FROZEN_STRATEGY_TRANSFER_OUTCOME_V2_NAME
        )
        fixture = outcome_eval.load_strategy_transfer_outcome_fixture(fixture_path)
        report = outcome_eval.run_strategy_transfer_outcome_fixture(fixture_path)
        if not isinstance(report, _memory().Mapping):
            raise ValueError("Strategy transfer evaluator returned a non-object")
        config = dict(fixture["thresholds"])
        config_sha256 = self._strategy_transfer_config_digest(config)
        if str(report.get("config_sha256")) != config_sha256:
            raise ValueError("Strategy transfer evaluator config seal is inconsistent")
        # Round-trip through canonical JSON so the stored artifact cannot retain
        # evaluator-owned mutable containers.  The evaluator's own fields and
        # attestation_sha256 remain byte-for-byte values from the sealed run.
        artifact = _memory().json.loads(self._strategy_transfer_canonical_json(dict(report)))
        artifact["config"] = config
        artifact["all_exit_criteria"] = bool(
            report.get(
                "all_exit_criteria",
                report.get("all_exit_criteria_passed", False),
            )
        )
        artifact["generated_at"] = self._strategy_transfer_z_timestamp(_memory().now_iso())
        artifact["run_id"] = safe_run_id
        return artifact

    def _strategy_transfer_benchmark_attestation_validation(
        self,
        artifact: Mapping[str, Any],
    ) -> tuple[bool, str]:
        try:
            if not isinstance(artifact, _memory().Mapping):
                return False, "benchmark_fields_invalid"
            if (
                artifact["schema_version"]
                != "strategy_transfer_outcome_attestation/v2"
                or artifact["benchmark_version"] != "2.0.0"
                or artifact["evaluator_module"]
                != "jarvis.strategy_transfer_outcome_eval"
                or artifact["evaluator_version"] != "2.0.0"
            ):
                return False, "benchmark_identity_invalid"
            self._strategy_transfer_identifier(artifact["run_id"], "run_id")
            generated_at = self._strategy_transfer_z_timestamp(
                artifact["generated_at"]
            )
            if generated_at != artifact["generated_at"]:
                return False, "benchmark_timestamp_invalid"
            from . import strategy_transfer_outcome_eval as outcome_eval

            evaluator_path = _memory().Path(str(outcome_eval.__file__)).resolve()
            fixture_path = (
                evaluator_path.parent
                / "evaluation_fixtures"
                / outcome_eval.FROZEN_STRATEGY_TRANSFER_OUTCOME_V2_NAME
            )
            fixture = outcome_eval.load_strategy_transfer_outcome_fixture(
                fixture_path
            )
            evaluator_report = outcome_eval.run_strategy_transfer_outcome_fixture(
                fixture_path
            )
            if not isinstance(evaluator_report, _memory().Mapping):
                return False, "benchmark_evaluator_output_invalid"
            wrapper_fields = {
                "config", "all_exit_criteria", "generated_at", "run_id",
            }
            expected_fields = set(evaluator_report).union(wrapper_fields)
            if set(artifact) != expected_fields:
                return False, "benchmark_fields_invalid"
            for key, value in evaluator_report.items():
                if key in {"generated_at", "run_id"}:
                    continue
                if artifact[key] != value:
                    return False, f"benchmark_{key}_mismatch"
            config = dict(fixture["thresholds"])
            if artifact["config"] != config:
                return False, "benchmark_config_mismatch"
            if str(artifact["config_sha256"]) != self._strategy_transfer_config_digest(
                config
            ):
                return False, "benchmark_config_sha256_mismatch"
            if str(artifact["evaluator_sha256"]) != _memory().hashlib.sha256(
                evaluator_path.read_bytes()
            ).hexdigest():
                return False, "benchmark_evaluator_sha256_mismatch"
            if str(artifact["fixture_sha256"]) != _memory().hashlib.sha256(
                fixture_path.read_bytes()
            ).hexdigest():
                return False, "benchmark_fixture_sha256_mismatch"
            if artifact["all_exit_criteria"] is not True:
                return False, "benchmark_exit_criteria_failed"
            if not isinstance(artifact["passes"], _memory().Mapping) or not all(
                value is True for value in artifact["passes"].values()
            ):
                return False, "benchmark_passes_failed"
        except (
            KeyError, OSError, _memory().sqlite3.DatabaseError, _memory().StrategyTransferError,
            TypeError, ValueError,
        ):
            return False, "benchmark_validation_failed"
        return True, "valid"

    def _strategy_transfer_prediction_receipt_summary(
        self,
        prediction_id: int,
        *,
        expected_mode: str,
        expected_applied: bool,
    ) -> dict[str, Any] | None:
        try:
            rows = self.db.execute(
                """SELECT id, source_family, target_family, mode, applied,
                          resolved_at, successful
                   FROM strategy_transfer_applications
                   WHERE prediction_id=? ORDER BY id""",
                (int(prediction_id),),
            ).fetchall()
        except (_memory().sqlite3.DatabaseError, TypeError, ValueError):
            return None
        if not rows:
            return None
        outcome: int | None = None
        pairs: set[tuple[str, str]] = set()
        for row in rows:
            if (
                str(row["mode"]) != expected_mode
                or bool(int(row["applied"])) != expected_applied
                or row["resolved_at"] is None
                or not self._strategy_transfer_application_validation(
                    int(row["id"])
                )[0]
            ):
                return None
            successful = int(row["successful"])
            if outcome is not None and outcome != successful:
                return None
            outcome = successful
            pairs.add((str(row["source_family"]), str(row["target_family"])))
        return {
            "prediction_id": int(prediction_id),
            "successful": int(outcome or 0),
            "source_target_pairs": pairs,
        }

    def _strategy_transfer_benchmark_row(self) -> tuple[dict[str, Any], sqlite3.Row] | None:
        try:
            rows = self.db.execute(
                """SELECT * FROM strategy_transfer_attestations
                   WHERE kind='sealed_benchmark' ORDER BY id DESC"""
            ).fetchall()
            for row in rows:
                artifact = _memory().json.loads(str(row["artifact_json"]))
                if (
                    isinstance(artifact, dict)
                    and self._strategy_transfer_stored_attestation_validation(
                        row
                    )[0]
                ):
                    return artifact, row
        except (_memory().json.JSONDecodeError, _memory().sqlite3.DatabaseError, TypeError, ValueError):
            return None
        return None

    def build_strategy_transfer_applied_ab_attestation(
        self,
        *,
        control_prediction_ids: Sequence[int],
        applied_prediction_ids: Sequence[int],
        assignment_manifest_sha256: str,
        run_id: str,
    ) -> dict[str, Any]:
        """Bind a disjoint assignment manifest to actual validated receipts."""
        manifest_sha256 = self._strategy_transfer_sha256(
            assignment_manifest_sha256, "assignment manifest"
        )
        safe_run_id = self._strategy_transfer_identifier(run_id, "run_id")
        benchmark = self._strategy_transfer_benchmark_row()
        if benchmark is None:
            raise ValueError("A valid sealed benchmark attestation is required")
        benchmark_artifact, _benchmark_row = benchmark
        trial_contract = self._strategy_transfer_trial_contract()

        def normalized_ids(values: Sequence[int], label: str) -> list[int]:
            if isinstance(values, (str, bytes)) or not isinstance(values, _memory().Sequence):
                raise ValueError(f"{label} prediction IDs must be an array")
            if len(values) > 10_000:
                raise ValueError(f"{label} prediction IDs exceed 10,000")
            result = [
                self._prediction_optional_id(value, "prediction_id")
                for value in values
            ]
            if len(result) != len(set(result)):
                raise ValueError(f"{label} prediction IDs contain duplicates")
            return sorted(result)

        control_ids = normalized_ids(control_prediction_ids, "control")
        applied_ids = normalized_ids(applied_prediction_ids, "applied")
        if set(control_ids).intersection(applied_ids):
            raise ValueError("Control and applied prediction assignments overlap")
        control_summaries = [
            self._strategy_transfer_prediction_receipt_summary(
                prediction_id,
                expected_mode="observe",
                expected_applied=False,
            )
            for prediction_id in control_ids
        ]
        applied_summaries = [
            self._strategy_transfer_prediction_receipt_summary(
                prediction_id,
                expected_mode="advise",
                expected_applied=True,
            )
            for prediction_id in applied_ids
        ]
        if any(item is None for item in control_summaries + applied_summaries):
            raise ValueError("A/B assignments do not match actual validated receipts")
        controls = [item for item in control_summaries if item is not None]
        applied = [item for item in applied_summaries if item is not None]
        control_successes = sum(int(item["successful"]) for item in controls)
        applied_successes = sum(int(item["successful"]) for item in applied)
        control_rate = control_successes / len(controls) if controls else 0.0
        applied_rate = applied_successes / len(applied) if applied else 0.0
        applied_pairs = set().union(
            *(item["source_target_pairs"] for item in applied)
        ) if applied else set()
        lift_pp = round(100.0 * (applied_rate - control_rate), 6)
        thresholds = _memory().STRATEGY_TRANSFER_ACTIVATION_THRESHOLDS
        ledger_health = self._strategy_transfer_ledger_health()
        passes = {
            "disjoint_assignments": not bool(set(control_ids).intersection(applied_ids)),
            "minimum_control_predictions": len(controls)
            >= thresholds["minimum_control_predictions"],
            "minimum_applied_predictions": len(applied)
            >= thresholds["minimum_applied_predictions"],
            "source_target_pairs": len(applied_pairs)
            >= thresholds["minimum_source_target_pairs"],
            "applied_success_rate": applied_rate
            >= thresholds["minimum_applied_success_rate"],
            "completion_lift": lift_pp >= thresholds["minimum_lift_pp"],
            # This observe-only release has no persisted pre-outcome randomized
            # assignment ledger. Disjoint IDs plus a caller-supplied digest are
            # not proof that arm assignment was independent of the outcome.
            "pre_outcome_randomized_assignment": False,
            "independent_outcomes": False,
            "no_invalid_receipts": (
                ledger_health["available"] is True
                and int(ledger_health["invalid_receipts"])
                <= thresholds["maximum_invalid_receipts"]
            ),
            "no_harm_quarantines": (
                ledger_health["available"] is True
                and int(ledger_health["harm_quarantines"])
                <= thresholds["maximum_harm_quarantines"]
            ),
        }
        artifact: dict[str, Any] = {
            "schema_version": "strategy_transfer_applied_ab_attestation/v1",
            "evaluator_version": str(benchmark_artifact["evaluator_version"]),
            "evaluator_sha256": str(benchmark_artifact["evaluator_sha256"]),
            "config": dict(trial_contract["config"]),
            "config_sha256": str(benchmark_artifact["config_sha256"]),
            "benchmark_attestation_sha256": str(
                benchmark_artifact["attestation_sha256"]
            ),
            "assignment_manifest_sha256": manifest_sha256,
            "control_prediction_ids": control_ids,
            "applied_prediction_ids": applied_ids,
            "counts": {
                "control_predictions": len(controls),
                "applied_predictions": len(applied),
                "source_target_pairs": len(applied_pairs),
            },
            "metrics": {
                "control_successes": control_successes,
                "applied_successes": applied_successes,
                "control_success_rate": round(control_rate, 6),
                "applied_success_rate": round(applied_rate, 6),
                "lift_pp": lift_pp,
                "independent_outcomes_rate": 0.0,
            },
            "passes": passes,
            "all_exit_criteria": all(passes.values()),
            "claim_scope": (
                "retrospective_receipt_comparison_only_not_activation_evidence"
            ),
            "generated_at": self._strategy_transfer_z_timestamp(_memory().now_iso()),
            "run_id": safe_run_id,
        }
        artifact["attestation_sha256"] = (
            self._strategy_transfer_ab_attestation_digest(artifact)
        )
        return artifact

    def _strategy_transfer_ab_attestation_validation(
        self,
        artifact: Mapping[str, Any],
    ) -> tuple[bool, str]:
        base_fields = {
            "schema_version", "evaluator_version", "evaluator_sha256",
            "config", "config_sha256", "benchmark_attestation_sha256",
            "assignment_manifest_sha256", "control_prediction_ids",
            "applied_prediction_ids", "counts", "metrics", "passes",
            "all_exit_criteria", "claim_scope", "generated_at", "run_id",
            "attestation_sha256",
        }
        try:
            if not isinstance(artifact, _memory().Mapping):
                return False, "applied_ab_fields_invalid"
            schema_version = artifact["schema_version"]
            if schema_version not in {
                "strategy_transfer_applied_ab_attestation/v1",
                "strategy_transfer_applied_ab_attestation/v2",
            }:
                return False, "applied_ab_schema_invalid"
            expected_fields = set(base_fields)
            if schema_version.endswith("/v2"):
                expected_fields.update({
                    "trial_evidence_artifact_sha256",
                    "causal_evaluator_attestation_sha256",
                })
            if set(artifact) != expected_fields:
                return False, "applied_ab_fields_invalid"
            expected_scope = (
                "retrospective_receipt_comparison_only_not_activation_evidence"
                if schema_version.endswith("/v1")
                else "pre_outcome_randomized_trial_activation_evidence"
            )
            if artifact["claim_scope"] != expected_scope:
                return False, "applied_ab_claim_scope_invalid"
            self._strategy_transfer_identifier(artifact["run_id"], "run_id")
            generated_at = self._strategy_transfer_z_timestamp(
                artifact["generated_at"]
            )
            if generated_at != artifact["generated_at"]:
                return False, "applied_ab_timestamp_invalid"
            if schema_version.endswith("/v1"):
                expected = self.build_strategy_transfer_applied_ab_attestation(
                    control_prediction_ids=artifact["control_prediction_ids"],
                    applied_prediction_ids=artifact["applied_prediction_ids"],
                    assignment_manifest_sha256=str(
                        artifact["assignment_manifest_sha256"]
                    ),
                    run_id=str(artifact["run_id"]),
                )
            else:
                manifest_row = self.db.execute(
                    """SELECT id FROM strategy_transfer_trial_manifests
                       WHERE manifest_sha256=?""",
                    (str(artifact["assignment_manifest_sha256"]),),
                ).fetchone()
                if manifest_row is None:
                    return False, "applied_ab_manifest_missing"
                expected = self.build_strategy_transfer_trial_ab_attestation(
                    int(manifest_row["id"]), run_id=str(artifact["run_id"])
                )
            for key in expected_fields - {"generated_at", "run_id"}:
                if artifact[key] != expected[key]:
                    return False, f"applied_ab_{key}_mismatch"
            if artifact["all_exit_criteria"] is not True:
                return False, "applied_ab_exit_criteria_failed"
        except (KeyError, OSError, _memory().StrategyTransferError, TypeError, ValueError):
            return False, "applied_ab_validation_failed"
        return True, "valid"

    def build_strategy_transfer_trial_ab_attestation(
        self,
        manifest_id: int,
        *,
        run_id: str,
    ) -> dict[str, Any]:
        """Recompute causal evidence only from valid pre-outcome trial rows."""
        normalized_manifest = self._prediction_optional_id(
            manifest_id, "manifest_id"
        )
        safe_run_id = self._strategy_transfer_identifier(run_id, "run_id")
        manifest_row = self.db.execute(
            "SELECT * FROM strategy_transfer_trial_manifests WHERE id=?",
            (normalized_manifest,),
        ).fetchone()
        if manifest_row is None:
            raise _memory().StrategyTransferTrialError("trial manifest is unavailable")
        valid, manifest = self._strategy_transfer_trial_manifest_validation(
            manifest_row
        )
        if not valid or not isinstance(manifest, dict):
            raise _memory().StrategyTransferTrialError("trial manifest is invalid")
        benchmark = self._strategy_transfer_benchmark_row()
        if benchmark is None or not self._strategy_transfer_trial_benchmark_matches(
            manifest
        ):
            raise _memory().StrategyTransferTrialError("sealed benchmark binding is invalid")
        benchmark_artifact, _benchmark_row = benchmark
        try:
            if _memory().strategy_transfer_runtime_sha256() != manifest["runtime_sha256"]:
                raise _memory().StrategyTransferTrialError("trial runtime drifted")
        except OSError as exc:
            raise _memory().StrategyTransferTrialError("trial runtime hash is unavailable") from exc
        from . import strategy_transfer_trial_eval as trial_eval

        evidence_artifact = self.build_strategy_transfer_trial_evidence_artifact(
            normalized_manifest
        )
        causal_report = trial_eval.evaluate_strategy_transfer_trial_artifact(
            evidence_artifact,
            expected_manifest_sha256=str(manifest["manifest_sha256"]),
        )
        if not isinstance(causal_report, _memory().Mapping):
            raise _memory().StrategyTransferTrialError(
                "causal trial evaluator returned an invalid report"
            )
        report_passes = causal_report.get("passes")
        outcomes_per_arm = causal_report.get("outcomes_per_arm")
        successes_per_arm = causal_report.get("successes_per_arm")
        rates = causal_report.get("rates")
        interval = causal_report.get("difference_ci_95")
        if not all(
            isinstance(value, _memory().Mapping)
            for value in (
                report_passes, outcomes_per_arm, successes_per_arm, rates,
            )
        ) or not isinstance(interval, list) or len(interval) != 2:
            raise _memory().StrategyTransferTrialError(
                "causal trial evaluator report is incomplete"
            )
        control_ids = sorted(
            int(row["assignment"]["prediction_id"])
            for row in evidence_artifact["rows"]
            if row["assignment"]["arm"] == "control"
        )
        treatment_ids = sorted(
            int(row["assignment"]["prediction_id"])
            for row in evidence_artifact["rows"]
            if row["assignment"]["arm"] == "treatment"
        )
        control_total = int(outcomes_per_arm["control"])
        treatment_total = int(outcomes_per_arm["treatment"])
        control_successes = int(successes_per_arm["control"])
        treatment_successes = int(successes_per_arm["treatment"])
        control_rate = float(rates["control"])
        treatment_rate = float(rates["treatment"])
        lift_pp = float(causal_report["lift_points"])
        ci_low_pp = round(100.0 * float(interval[0]), 6)
        ci_high_pp = round(100.0 * float(interval[1]), 6)
        pairs_count = int(causal_report["source_target_pairs"])
        evidence_sha256 = _memory().sha256_json(evidence_artifact)
        replay_valid = (
            causal_report.get("artifact_sha256") == evidence_sha256
            and causal_report.get("manifest_sha256")
            == manifest["manifest_sha256"]
            and causal_report.get("evaluator_version")
            == manifest["evaluator_version"]
            and causal_report.get("claim_scope")
            == "sealed_randomized_trial_evidence_only"
            and causal_report.get("activation_authorized") is False
            and causal_report.get("all_exit_criteria_passed") is True
        )
        ledger = self._strategy_transfer_ledger_health()
        passes = {
            "pinned_causal_evaluator_replay": replay_valid,
            "pre_outcome_randomized_assignment": bool(
                report_passes["balanced_complete_blocks"]
            ),
            "independent_outcomes": bool(
                report_passes["zero_invalid_or_contaminated_rows"]
            ),
            "minimum_control_predictions": bool(
                report_passes["minimum_outcomes"]
            ),
            "minimum_applied_predictions": bool(
                report_passes["minimum_outcomes"]
            ),
            "source_target_pairs": bool(report_passes["minimum_pairs"]),
            "applied_success_rate": bool(report_passes["treatment_rate"]),
            "completion_lift": bool(report_passes["lift"]),
            "confidence_interval_positive": bool(
                report_passes["predeclared_significance"]
            ),
            "no_target_family_negative_effect": bool(
                report_passes["no_negative_family_effect"]
            ),
            "no_invalid_receipts": ledger["available"] is True
                and int(ledger["invalid_receipts"]) == 0,
            "no_harm_quarantines": ledger["available"] is True
                and int(ledger["harm_quarantines"]) == 0,
        }
        artifact: dict[str, Any] = {
            "schema_version": "strategy_transfer_applied_ab_attestation/v2",
            "evaluator_version": manifest["evaluator_version"],
            "evaluator_sha256": manifest["evaluator_sha256"],
            "config": dict(trial_eval.EVALUATION_CONFIG),
            "config_sha256": manifest["config_sha256"],
            "benchmark_attestation_sha256": str(
                benchmark_artifact["attestation_sha256"]
            ),
            "assignment_manifest_sha256": manifest["manifest_sha256"],
            "trial_evidence_artifact_sha256": evidence_sha256,
            "causal_evaluator_attestation_sha256": str(
                causal_report["attestation_sha256"]
            ),
            "control_prediction_ids": control_ids,
            "applied_prediction_ids": treatment_ids,
            "counts": {
                "control_predictions": control_total,
                "applied_predictions": treatment_total,
                "source_target_pairs": pairs_count,
                "invalid_assignments": 0,
            },
            "metrics": {
                "control_successes": control_successes,
                "applied_successes": treatment_successes,
                "control_success_rate": round(control_rate, 6),
                "applied_success_rate": round(treatment_rate, 6),
                "lift_pp": lift_pp,
                "lift_ci95_low_pp": ci_low_pp,
                "lift_ci95_high_pp": ci_high_pp,
                "confidence_interval_method": "newcombe_wilson_95",
                "independent_outcomes_rate": (
                    1.0 if control_total + treatment_total else 0.0
                ),
            },
            "passes": passes,
            "all_exit_criteria": all(passes.values()),
            "claim_scope": "pre_outcome_randomized_trial_activation_evidence",
            "generated_at": self._strategy_transfer_z_timestamp(_memory().now_iso()),
            "run_id": safe_run_id,
        }
        artifact["attestation_sha256"] = self._strategy_transfer_ab_attestation_digest(
            artifact
        )
        return artifact

    def build_strategy_transfer_trial_evidence_artifact(
        self,
        manifest_id: int,
    ) -> dict[str, Any]:
        """Export only closed, digest-bound v39 receipts for causal evaluation."""
        normalized_manifest = self._prediction_optional_id(
            manifest_id, "manifest_id"
        )
        manifest_row = self.db.execute(
            "SELECT * FROM strategy_transfer_trial_manifests WHERE id=?",
            (normalized_manifest,),
        ).fetchone()
        if manifest_row is None:
            raise _memory().StrategyTransferTrialError("trial manifest is unavailable")
        valid, manifest = self._strategy_transfer_trial_manifest_validation(
            manifest_row
        )
        if not valid or not isinstance(manifest, dict):
            raise _memory().StrategyTransferTrialError("trial manifest is invalid")
        benchmark = self._strategy_transfer_benchmark_row()
        if benchmark is None:
            raise _memory().StrategyTransferTrialError("Phase 4A benchmark is unavailable")
        benchmark_artifact, _benchmark_row = benchmark
        manifest_material = self._strategy_transfer_trial_manifest_material(
            created_at=manifest["created_at"],
            expires_at=manifest["expires_at"],
            project_id=manifest["project_id"],
            target_families=manifest["target_families"],
            family_cap_values=manifest["family_caps"],
            strategies=manifest["strategies"],
            sample_cap=manifest["sample_cap"],
            seed=manifest["seed"],
            evaluator_version=manifest["evaluator_version"],
            evaluator_sha256=manifest["evaluator_sha256"],
            fixture_sha256=manifest["fixture_sha256"],
            config_sha256=manifest["config_sha256"],
            runtime_sha256=manifest["runtime_sha256"],
        )
        manifest_material["manifest_sha256"] = manifest["manifest_sha256"]
        assignment_rows = self.db.execute(
            """SELECT * FROM strategy_transfer_trial_assignments
               WHERE manifest_id=? ORDER BY target_family, family_sequence""",
            (normalized_manifest,),
        ).fetchall()
        exported_rows: list[dict[str, Any]] = []
        for row in assignment_rows:
            if (
                str(row["status"]) != "resolved"
                or not self._strategy_transfer_trial_assignment_validation(
                    row, require_prompt=True, require_dispatch=True
                )[0]
            ):
                raise _memory().StrategyTransferTrialError(
                    "trial evidence contains unresolved or invalid assignment"
                )
            assignment = self._strategy_transfer_trial_assignment_material(
                manifest_sha256=manifest["manifest_sha256"],
                created_at=str(row["created_at"]),
                prediction_id=int(row["prediction_id"]),
                project_id=int(row["project_id"]),
                target_family=str(row["target_family"]),
                family_sequence=int(row["family_sequence"]),
                block_index=int(row["block_index"]),
                block_slot=int(row["block_slot"]),
                arm=str(row["arm"]),
                strategies=_memory().json.loads(str(row["strategies_json"])),
                selection_sha256=str(row["selection_sha256"]),
            )
            assignment["assignment_sha256"] = str(row["assignment_sha256"])
            prompt = self._strategy_transfer_trial_prompt_material(
                assignment_sha256=str(row["assignment_sha256"]),
                prompt_recorded_at=str(row["prompt_recorded_at"]),
                base_prompt_sha256=str(row["base_prompt_sha256"]),
                final_prompt_sha256=str(row["final_prompt_sha256"]),
                advice_applied=bool(int(row["advice_applied"])),
            )
            prompt["prompt_receipt_sha256"] = str(row["prompt_receipt_sha256"])
            dispatch = self._strategy_transfer_trial_dispatch_material(
                assignment_sha256=str(row["assignment_sha256"]),
                prompt_receipt_sha256=str(row["prompt_receipt_sha256"]),
                provider_dispatched_at=str(row["provider_dispatched_at"]),
            )
            dispatch["provider_dispatch_sha256"] = str(
                row["provider_dispatch_sha256"]
            )
            applications = self.db.execute(
                """SELECT * FROM strategy_transfer_applications
                   WHERE prediction_id=? ORDER BY rank, id""",
                (int(row["prediction_id"]),),
            ).fetchall()
            exported_applications: list[dict[str, Any]] = []
            for application in applications:
                if not self._strategy_transfer_application_validation(
                    int(application["id"])
                )[0]:
                    raise _memory().StrategyTransferTrialError(
                        "trial evidence application receipt is invalid"
                    )
                material = self._strategy_transfer_application_material(
                    created_at=str(application["created_at"]),
                    prediction_id=int(application["prediction_id"]),
                    memory_id=int(application["memory_id"]),
                    project_id=int(application["project_id"]),
                    strategy=str(application["strategy"]),
                    source_family=str(application["source_family"]),
                    target_family=str(application["target_family"]),
                    mode=str(application["mode"]),
                    applied=bool(int(application["applied"])),
                    rank=int(application["rank"]),
                    source_observation_sha256=str(
                        application["source_observation_sha256"]
                    ),
                    source_provenance_sha256=str(
                        application["source_provenance_sha256"]
                    ),
                    source_control_sha256=str(
                        application["source_control_sha256"]
                    ),
                    resolved_at=str(application["resolved_at"]),
                    successful=int(application["successful"]),
                )
                material["application_sha256"] = str(
                    application["application_sha256"]
                )
                exported_applications.append(material)
            outcome = self._strategy_transfer_trial_outcome_material(
                assignment_sha256=str(row["assignment_sha256"]),
                prompt_receipt_sha256=str(row["prompt_receipt_sha256"]),
                status=str(row["status"]),
                status_reason=None,
                resolved_at=str(row["resolved_at"]),
                successful=int(row["successful"]),
            )
            outcome["outcome_sha256"] = str(row["outcome_sha256"])
            exported_rows.append({
                "block_id": f"{row['target_family']}:{int(row['block_index'])}",
                "assignment": assignment,
                "prompt_receipt": prompt,
                "applications": exported_applications,
                "provider_dispatch": dispatch,
                "outcome": outcome,
            })
        return {
            "schema": "jarvis.strategy-transfer-trial-evidence.v1",
            "phase4a_benchmark_attestation_sha256": str(
                benchmark_artifact["attestation_sha256"]
            ),
            "manifest": manifest_material,
            "rows": exported_rows,
        }

    def record_strategy_transfer_attestation(
        self,
        kind: str,
        artifact: Mapping[str, Any],
        *,
        evaluator_version: str,
        evaluator_sha256: str,
        config_sha256: str,
    ) -> bool:
        """Persist an immutable validated benchmark or real A/B attestation."""
        if kind not in _memory().STRATEGY_TRANSFER_ATTESTATION_KINDS:
            raise ValueError("Unknown strategy transfer attestation kind")
        safe_version = self._strategy_transfer_identifier(
            evaluator_version, "evaluator_version"
        )
        safe_evaluator_sha = self._strategy_transfer_sha256(
            evaluator_sha256, "evaluator"
        )
        safe_config_sha = self._strategy_transfer_sha256(
            config_sha256, "config"
        )
        if not isinstance(artifact, _memory().Mapping):
            raise ValueError("Strategy transfer attestation must be an object")
        if (
            str(artifact.get("evaluator_version")) != safe_version
            or str(artifact.get("evaluator_sha256")) != safe_evaluator_sha
            or str(artifact.get("config_sha256")) != safe_config_sha
        ):
            raise ValueError("Attestation evaluator or config binding does not match")
        validation = (
            self._strategy_transfer_benchmark_attestation_validation(artifact)
            if kind == "sealed_benchmark"
            else self._strategy_transfer_ab_attestation_validation(artifact)
        )
        if not validation[0]:
            raise ValueError(f"Strategy transfer attestation is invalid: {validation[1]}")
        canonical = self._strategy_transfer_canonical_json(dict(artifact))
        artifact_sha256 = _memory().hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        fixture_sha256 = (
            self._strategy_transfer_sha256(artifact["fixture_sha256"], "fixture")
            if kind == "sealed_benchmark" else None
        )
        assignment_sha256 = (
            self._strategy_transfer_sha256(
                artifact["assignment_manifest_sha256"], "assignment manifest"
            )
            if kind == "applied_ab" else None
        )
        internal_attestation_sha = self._strategy_transfer_sha256(
            artifact["attestation_sha256"], "attestation"
        )
        with self._immediate_transaction():
            existing = self.db.execute(
                """SELECT * FROM strategy_transfer_attestations
                   WHERE kind=? AND artifact_sha256=?""",
                (kind, artifact_sha256),
            ).fetchone()
            if existing is not None:
                if (
                    str(existing["artifact_json"]) == canonical
                    and self._strategy_transfer_stored_attestation_validation(
                        existing
                    )[0]
                ):
                    return False
                raise ValueError("Conflicting or invalid attestation replay")
            digest_replay = self.db.execute(
                """SELECT * FROM strategy_transfer_attestations
                   WHERE kind=? AND attestation_sha256=?""",
                (kind, internal_attestation_sha),
            ).fetchone()
            if digest_replay is not None:
                if (
                    kind == "applied_ab"
                    and str(digest_replay["assignment_manifest_sha256"])
                    == str(assignment_sha256)
                    and self._strategy_transfer_stored_attestation_validation(
                        digest_replay
                    )[0]
                ):
                    # Concurrent promotion attempts may have distinct wrapper
                    # timestamps but the same immutable causal core. Treat the
                    # already-validated receipt for this exact manifest as the
                    # one successful append, never as a conflicting replay.
                    return False
                raise ValueError(
                    "Attestation digest is already bound to a different receipt"
                )
            self.db.execute(
                """INSERT INTO strategy_transfer_attestations(
                       kind, recorded_at, evaluator_version, evaluator_sha256,
                       config_sha256, fixture_sha256,
                       assignment_manifest_sha256, artifact_json,
                       artifact_sha256, attestation_sha256
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    kind, _memory().now_iso(), safe_version, safe_evaluator_sha,
                    safe_config_sha, fixture_sha256, assignment_sha256,
                    canonical, artifact_sha256, internal_attestation_sha,
                ),
            )
        return True

    def _strategy_transfer_stored_attestation_validation(
        self,
        row: Mapping[str, Any],
    ) -> tuple[bool, str]:
        try:
            kind = str(row["kind"])
            if kind not in _memory().STRATEGY_TRANSFER_ATTESTATION_KINDS:
                return False, "attestation_kind_invalid"
            artifact = _memory().json.loads(str(row["artifact_json"]))
            if not isinstance(artifact, dict):
                return False, "attestation_artifact_invalid"
            canonical = self._strategy_transfer_canonical_json(artifact)
            if canonical != str(row["artifact_json"]):
                return False, "attestation_artifact_noncanonical"
            if _memory().hashlib.sha256(canonical.encode("utf-8")).hexdigest() != str(
                row["artifact_sha256"]
            ):
                return False, "attestation_artifact_digest_mismatch"
            if (
                str(row["evaluator_version"])
                != str(artifact["evaluator_version"])
                or str(row["evaluator_sha256"])
                != str(artifact["evaluator_sha256"])
                or str(row["config_sha256"])
                != str(artifact["config_sha256"])
                or str(row["attestation_sha256"])
                != str(artifact["attestation_sha256"])
            ):
                return False, "attestation_binding_mismatch"
            if kind == "sealed_benchmark":
                if (
                    str(row["fixture_sha256"])
                    != str(artifact["fixture_sha256"])
                    or row["assignment_manifest_sha256"] is not None
                ):
                    return False, "benchmark_storage_binding_mismatch"
                return self._strategy_transfer_benchmark_attestation_validation(
                    artifact
                )
            if (
                row["fixture_sha256"] is not None
                or str(row["assignment_manifest_sha256"])
                != str(artifact["assignment_manifest_sha256"])
            ):
                return False, "applied_ab_storage_binding_mismatch"
            return self._strategy_transfer_ab_attestation_validation(artifact)
        except (
            _memory().json.JSONDecodeError,
            KeyError,
            OSError,
            _memory().sqlite3.DatabaseError,
            _memory().StrategyTransferError,
            TypeError,
            ValueError,
        ):
            return False, "attestation_validation_failed"
