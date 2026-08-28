from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable


NETWORK_DEFENSE_ASSESSMENT_VERSION = 1
NETWORK_DEFENSE_RULESET_VERSION = "inventory-presence-v1"
DEFAULT_STALE_AFTER_SECONDS = 24 * 60 * 60
MAX_ASSESSMENT_DEVICES = 1_024
MAX_ASSESSMENT_EVENTS = 1_024
MAX_ASSESSMENT_SIGNALS = 128

_SEVERITY_RANK = {
    "informational": 0,
    "low": 1,
    "medium": 2,
    "high": 3,
}
_OPAQUE_ID = re.compile(r"^[0-9a-f]{32}$")
_TRUST_STATES = frozenset({"unreviewed", "recognized", "watch", "retired"})
_IDENTITY_CONFIDENCE = frozenset({"limited", "moderate"})
_PRESENCE_STATES = frozenset({"reachable", "cached", "unobserved"})


def _utc(value: datetime | None = None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


def _parse_time(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return _utc(parsed)


def _normalized_timestamp(value: Any) -> str | None:
    parsed = _parse_time(value)
    return parsed.isoformat() if parsed is not None else None


def _bounded_device_id(value: Any) -> str | None:
    text = str(value or "").strip().casefold()
    return text if _OPAQUE_ID.fullmatch(text) else None


def _bounded_scope_id(value: Any) -> str | None:
    text = str(value or "").strip().casefold()
    return text if _OPAQUE_ID.fullmatch(text) else None


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        return None
    return normalized if normalized > 0 else None


def _enum(value: Any, allowed: frozenset[str], default: str) -> str:
    normalized = str(value or "").strip().casefold()
    return normalized if normalized in allowed else default


def _signal(
    rule_id: str,
    *,
    severity: str,
    confidence: str,
    category: str,
    summary: str,
    evidence: dict[str, Any],
    recommended_action: str,
    benign_explanations: Iterable[str] = (),
    device_id: str | None = None,
) -> dict[str, Any]:
    identity = {
        "version": NETWORK_DEFENSE_ASSESSMENT_VERSION,
        "rule_id": rule_id,
        "device_id": device_id,
        "evidence": evidence,
    }
    fingerprint = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:24]
    return {
        "signal_id": fingerprint,
        "rule_id": rule_id,
        "severity": severity,
        "confidence": confidence,
        "category": category,
        "summary": summary[:500],
        "device_id": device_id,
        "evidence": evidence,
        "benign_explanations": [str(item)[:300] for item in benign_explanations][:5],
        "recommended_action": recommended_action[:500],
        "automatic_action_taken": False,
        "compromise_established": False,
    }


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def verify_assessment_receipt(value: dict[str, Any]) -> bool:
    """Verify the self-contained integrity hash of one assessment receipt."""
    if not isinstance(value, dict):
        return False
    if value.get("assessment_version") != NETWORK_DEFENSE_ASSESSMENT_VERSION:
        return False
    if value.get("ruleset_version") != NETWORK_DEFENSE_RULESET_VERSION:
        return False
    evidence = value.get("evidence_snapshot")
    if not isinstance(evidence, dict):
        return False
    input_sha256 = str(value.get("input_sha256") or "")
    if not re.fullmatch(r"[0-9a-f]{64}", input_sha256):
        return False
    if _canonical_sha256(evidence) != input_sha256:
        return False
    expected_assessment_id = hashlib.sha256(
        f"{NETWORK_DEFENSE_RULESET_VERSION}:{input_sha256}".encode("utf-8")
    ).hexdigest()[:32]
    if value.get("assessment_id") != expected_assessment_id:
        return False
    if value.get("scan_id") != evidence.get("scan_id"):
        return False
    if value.get("scope_id") != evidence.get("scope_id"):
        return False
    containment = value.get("automatic_containment")
    if not isinstance(containment, dict):
        return False
    if containment.get("enabled") is not False or containment.get("actions_taken") != 0:
        return False
    signals = value.get("signals")
    if not isinstance(signals, list) or len(signals) > MAX_ASSESSMENT_SIGNALS:
        return False
    for signal in signals:
        if not isinstance(signal, dict):
            return False
        if signal.get("automatic_action_taken") is not False:
            return False
        if signal.get("compromise_established") is not False:
            return False
        if signal.get("severity") not in _SEVERITY_RANK:
            return False
        device_id = signal.get("device_id")
        if device_id is not None and _bounded_device_id(device_id) is None:
            return False
    expected = str(value.get("receipt_sha256") or "")
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        return False
    payload = dict(value)
    payload.pop("receipt_sha256", None)
    return _canonical_sha256(payload) == expected


def assess_network_defense(
    inventory: dict[str, Any] | None,
    events: Iterable[dict[str, Any]] = (),
    *,
    now: datetime | None = None,
    stale_after_seconds: int = DEFAULT_STALE_AFTER_SECONDS,
) -> dict[str, Any]:
    """Create an explainable defensive assessment from bounded inventory evidence.

    This is deliberately a hypothesis and visibility layer, not an intrusion
    verdict. It copies no IP, MAC, hostname, packet, payload, or private-content
    fields and never performs containment.
    """
    current = _utc(now)
    source = inventory if isinstance(inventory, dict) else {}
    raw_devices = source.get("devices", [])
    canonical_devices: list[dict[str, Any]] = []
    if isinstance(raw_devices, (list, tuple)):
        for row in raw_devices:
            if not isinstance(row, dict):
                continue
            device_id = _bounded_device_id(row.get("device_id"))
            if device_id is None:
                continue
            raw_presence = _enum(
                row.get("presence_state"), _PRESENCE_STATES, "unobserved"
            )
            visible = bool(row.get("visible_now") or raw_presence == "reachable")
            canonical_devices.append({
                "device_id": device_id,
                "trust_state": _enum(
                    row.get("trust_state"), _TRUST_STATES, "unreviewed"
                ),
                "presence_state": "reachable" if visible else raw_presence,
                "visible_now": visible,
                "identity_confidence": _enum(
                    row.get("identity_confidence"),
                    _IDENTITY_CONFIDENCE,
                    "limited",
                ),
                "last_seen": _normalized_timestamp(row.get("last_seen")),
                "last_active_seen": _normalized_timestamp(
                    row.get("last_active_seen")
                ),
                "profile_updated_at": _normalized_timestamp(
                    row.get("profile_updated_at")
                ),
            })
    canonical_devices.sort(
        key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":"))
    )
    devices_omitted = max(
        0, len(canonical_devices) - MAX_ASSESSMENT_DEVICES
    )
    devices = canonical_devices[:MAX_ASSESSMENT_DEVICES]

    canonical_events: list[dict[str, Any]] = []
    for row in events:
        if not isinstance(row, dict):
            continue
        event_id = _positive_int(row.get("event_id"))
        device_id = _bounded_device_id(row.get("device_id"))
        event_type = str(row.get("event_type") or "").strip().casefold()
        observed_at = _normalized_timestamp(row.get("observed_at"))
        if (
            event_id is None
            or device_id is None
            or observed_at is None
            or event_type != "new_device_observed"
        ):
            continue
        canonical_events.append({
            "event_id": event_id,
            "device_id": device_id,
            "event_type": event_type,
            "observed_at": observed_at,
        })
    canonical_events.sort(key=lambda item: (
        str(item["observed_at"]),
        str(item["device_id"]),
        int(item["event_id"]),
    ))
    events_omitted = max(0, len(canonical_events) - MAX_ASSESSMENT_EVENTS)
    raw_events = canonical_events[:MAX_ASSESSMENT_EVENTS]
    baseline_scan = source.get("baseline_scan") is True
    scan_time = _parse_time(source.get("last_scan_at") or source.get("observed_at"))
    stale_bound = max(60, min(int(stale_after_seconds), 30 * 24 * 60 * 60))
    scan_age = (
        max(0, int((current - scan_time).total_seconds()))
        if scan_time is not None
        else None
    )
    freshness_state = (
        "missing"
        if scan_time is None
        else ("stale" if scan_age is not None and scan_age > stale_bound else "fresh")
    )
    profile_times = [
        parsed
        for parsed in (_parse_time(item.get("profile_updated_at")) for item in devices)
        if parsed is not None
    ]
    # Receipt identity changes only when evidence crosses a material freshness
    # boundary or the operator changes a device profile. It does not churn every
    # second merely because a status page is open.
    state_candidates = list(profile_times)
    if scan_time is None:
        state_candidates.append(
            current.replace(hour=0, minute=0, second=0, microsecond=0)
        )
    elif freshness_state == "stale":
        state_candidates.append(scan_time + timedelta(seconds=stale_bound + 1))
    else:
        state_candidates.append(scan_time)
    state_time = max(state_candidates)
    signals: list[dict[str, Any]] = []

    if scan_time is None:
        signals.append(_signal(
            "monitoring_not_established",
            severity="medium",
            confidence="high",
            category="visibility",
            summary="No completed network observation is available, so current device presence is unknown.",
            evidence={"source": "network_inventory", "last_scan_at": None},
            recommended_action="Run one bounded check on the explicitly paired network, then review the resulting baseline.",
        ))
    elif scan_age is not None and scan_age > stale_bound:
        signals.append(_signal(
            "monitoring_stale",
            severity="medium",
            confidence="high",
            category="visibility",
            summary="The latest network observation is stale; current device presence may differ.",
            evidence={
                "source": "network_inventory",
                "last_scan_at": scan_time.isoformat(),
                "age_seconds_at_least": stale_bound + 1,
                "stale_after_seconds": stale_bound,
            },
            recommended_action="Run a fresh bounded check before making a security decision.",
            benign_explanations=("Jarvis may have been stopped or the computer may have been away from this network.",),
        ))

    if source.get("coverage_complete_for_selected_range") is False:
        signals.append(_signal(
            "observation_range_incomplete",
            severity="medium",
            confidence="high",
            category="visibility",
            summary="The last bounded check did not cover every candidate in the selected paired range.",
            evidence={"source": "network_inventory", "coverage_complete": False},
            recommended_action="Review the configured host bound and paired scope before interpreting absence as offline.",
            benign_explanations=("A deliberately low host limit can truncate an otherwise healthy check.",),
        ))

    recent_new: dict[str, dict[str, Any]] = {}
    for event in (() if baseline_scan else raw_events):
        if str(event.get("event_type") or "").casefold() != "new_device_observed":
            continue
        device_id = _bounded_device_id(event.get("device_id"))
        observed_at = _parse_time(event.get("observed_at"))
        if device_id is None or observed_at is None:
            continue
        event_age = (current - observed_at).total_seconds()
        if 0 <= event_age <= stale_bound:
            event_id = _positive_int(event.get("event_id"))
            if event_id is None:
                continue
            candidate = {
                "event_id": event_id,
                "observed_at": observed_at.isoformat(),
            }
            current_event = recent_new.get(device_id)
            if current_event is None or (
                str(candidate["observed_at"]), str(candidate["event_id"])
            ) > (
                str(current_event["observed_at"]), str(current_event["event_id"])
            ):
                recent_new[device_id] = candidate

    unreviewed = 0
    limited_identity = 0
    for device in devices:
        device_id = str(device["device_id"])
        visible = bool(
            device.get("visible_now")
            or str(device.get("presence_state") or "").casefold() == "reachable"
        )
        trust = str(device["trust_state"])
        identity_confidence = str(device["identity_confidence"])
        profile_updated_at = _parse_time(device.get("profile_updated_at"))
        last_active_seen = _parse_time(device.get("last_active_seen"))
        policy_applies_to_observation = bool(
            profile_updated_at is None
            or (
                last_active_seen is not None
                and last_active_seen >= profile_updated_at
            )
        )
        if trust == "unreviewed":
            unreviewed += 1
        if identity_confidence == "limited":
            limited_identity += 1
        evidence = {
            "source": "network_inventory",
            "device_id": device_id,
            "presence_state": (
                "reachable"
                if visible
                else _enum(device.get("presence_state"), _PRESENCE_STATES, "unobserved")
            ),
            "trust_state": trust,
            "last_seen": device.get("last_seen"),
            "last_active_seen": (
                last_active_seen.isoformat() if last_active_seen else None
            ),
            "profile_updated_at": (
                profile_updated_at.isoformat() if profile_updated_at else None
            ),
        }
        if visible and trust == "retired" and policy_applies_to_observation:
            signals.append(_signal(
                "retired_device_reappeared",
                severity="high",
                confidence=("limited" if identity_confidence == "limited" else "medium"),
                category="operator_policy",
                summary="A device marked as no longer used responded during the latest observation.",
                evidence=evidence,
                recommended_action="Confirm whether the device was intentionally returned to service; otherwise isolate it through the router after verifying its identity.",
                benign_explanations=("The device may have been powered on intentionally or the label may be outdated.",),
                device_id=device_id,
            ))
        elif visible and trust == "watch" and policy_applies_to_observation:
            signals.append(_signal(
                "watched_device_observed",
                severity="medium",
                confidence=("limited" if identity_confidence == "limited" else "medium"),
                category="operator_policy",
                summary="A device the operator marked for closer review is currently responding.",
                evidence=evidence,
                recommended_action="Review the device history and corroborate with router or enrolled-endpoint telemetry before containment.",
                benign_explanations=("The device may be expected online; the watch label is not a compromise verdict.",),
                device_id=device_id,
            ))
        elif trust == "unreviewed" and device_id in recent_new:
            event_evidence = dict(evidence)
            event_evidence.update(recent_new[device_id])
            signals.append(_signal(
                "new_unreviewed_device",
                severity="medium",
                confidence=("limited" if identity_confidence == "limited" else "medium"),
                category="asset_change",
                summary="A recently first-observed device has not been identified by the operator.",
                evidence=event_evidence,
                recommended_action="Match it to an owned device or guest before changing network access; mark it for closer review if it remains unexplained.",
                benign_explanations=(
                    "It may be a guest, a newly installed device, or a device using MAC randomization.",
                    "DHCP and neighbor-cache changes can alter weak network identities.",
                ),
                device_id=device_id,
            ))

    if unreviewed:
        signals.append(_signal(
            "inventory_classification_incomplete",
            severity="informational",
            confidence="high",
            category="asset_management",
            summary=f"{unreviewed} observed device record(s) still need an operator label or review decision.",
            evidence={"source": "network_inventory", "unreviewed_devices": unreviewed},
            recommended_action="Label recognized devices and reserve watch or retired states for records that need follow-up.",
        ))
    if limited_identity:
        signals.append(_signal(
            "limited_device_identity",
            severity="informational",
            confidence="high",
            category="evidence_quality",
            summary=f"{limited_identity} device record(s) rely on identity evidence that may change or be spoofed.",
            evidence={"source": "network_inventory", "limited_identity_devices": limited_identity},
            recommended_action="Use read-only router telemetry or a cryptographically enrolled device agent before relying on identity-sensitive conclusions.",
            benign_explanations=("Modern phones and other clients commonly randomize local MAC addresses.",),
        ))

    signals.sort(key=lambda item: (
        -_SEVERITY_RANK.get(str(item.get("severity")), 0),
        str(item.get("rule_id") or ""),
        str(item.get("device_id") or ""),
    ))
    signals_omitted = max(0, len(signals) - MAX_ASSESSMENT_SIGNALS)
    signals = signals[:MAX_ASSESSMENT_SIGNALS]
    highest = max(
        (str(item.get("severity")) for item in signals),
        key=lambda value: _SEVERITY_RANK.get(value, -1),
        default="none",
    )
    posture = {
        "high": "urgent_review",
        "medium": "review_required",
        "low": "monitor",
        "informational": "monitor",
        "none": "no_current_signals",
    }[highest]
    attention_count = sum(
        1 for item in signals if _SEVERITY_RANK.get(str(item.get("severity")), 0) >= 2
    )
    sanitized_input = {
        "ruleset_version": NETWORK_DEFENSE_RULESET_VERSION,
        "scan_id": _positive_int(source.get("last_scan_id")),
        "scope_id": _bounded_scope_id(source.get("last_scan_scope_id")),
        "last_scan_at": scan_time.isoformat() if scan_time else None,
        "freshness_state": freshness_state,
        "assessment_state_at": state_time.isoformat(),
        "stale_after_seconds": stale_bound,
        "baseline_scan": baseline_scan,
        "devices_omitted": devices_omitted,
        "events_omitted": events_omitted,
        "coverage_complete_for_selected_range": (
            source.get("coverage_complete_for_selected_range")
            if isinstance(source.get("coverage_complete_for_selected_range"), bool)
            else None
        ),
        "devices": [
            {
                "device_id": item["device_id"],
                "trust_state": item["trust_state"],
                "presence_state": item["presence_state"],
                "identity_confidence": item["identity_confidence"],
                "last_seen": item["last_seen"],
                "last_active_seen": item["last_active_seen"],
                "profile_updated_at": item["profile_updated_at"],
            }
            for item in devices
        ],
        "events": [dict(item) for item in raw_events],
    }
    input_sha256 = _canonical_sha256(sanitized_input)
    receipt = {
        "assessment_version": NETWORK_DEFENSE_ASSESSMENT_VERSION,
        "ruleset_version": NETWORK_DEFENSE_RULESET_VERSION,
        "assessment_id": hashlib.sha256(
            f"{NETWORK_DEFENSE_RULESET_VERSION}:{input_sha256}".encode("utf-8")
        ).hexdigest()[:32],
        "input_sha256": input_sha256,
        "scan_id": _positive_int(source.get("last_scan_id")),
        "scope_id": _bounded_scope_id(source.get("last_scan_scope_id")),
        "generated_at": state_time.isoformat(),
        "posture": posture,
        "highest_severity": highest,
        "attention_signal_count": attention_count,
        "signals_omitted": signals_omitted,
        "signals": signals,
        "conclusion": (
            "Current evidence needs prompt operator review; it does not establish compromise."
            if highest == "high"
            else (
                "Current evidence has items to review; none independently establishes compromise."
                if highest == "medium"
                else "No current inventory signal establishes compromise. Coverage remains limited."
            )
        ),
        "coverage": {
            "last_scan_at": scan_time.isoformat() if scan_time else None,
            "freshness_state": freshness_state,
            "stale_after_seconds": stale_bound,
            "known_devices": len(devices),
            "devices_omitted": devices_omitted,
            "events_omitted": events_omitted,
            "current_reachability_only": True,
            "router_logs_available": False,
            "endpoint_telemetry_available": False,
            "packet_or_flow_telemetry_available": False,
            "vulnerability_telemetry_available": False,
        },
        "automatic_containment": {
            "enabled": False,
            "actions_taken": 0,
            "reason": "Inventory evidence alone is insufficient for autonomous containment.",
        },
        "limitations": [
            "An anomaly is a hypothesis, not proof of compromise.",
            "This assessment sees bounded reachability and saved labels, not traffic contents, services, vulnerabilities, or endpoint state.",
            "High-impact containment requires corroborating evidence, exact scope, approval, rollback, and an audit receipt.",
        ],
        "evidence_snapshot": sanitized_input,
    }
    receipt["receipt_sha256"] = _canonical_sha256(receipt)
    return receipt
