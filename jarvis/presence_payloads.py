"""Closed, secret-safe payload boundaries for Jarvis Presence.

Presence carries two very different kinds of data: operator-facing assistant
text, which must remain readable, and prompt-free operational metrics, which
must remain a closed schema.  Keeping those boundaries here prevents generic
event plumbing from accidentally exposing nested credentials or private model
inputs.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

from .redaction import redact_secrets
from .run_observability import aggregate_run_metrics, sanitize_run_metrics


MAX_EVENT_DEPTH = 8
MAX_EVENT_KEYS = 100
MAX_EVENT_ITEMS = 4_096


def safe_presence_text(value: Any, limit: int = 100_000) -> str:
    """Return bounded display text with credentials and NUL bytes removed."""

    text = redact_secrets(str(value), "[REDACTED]").replace("\x00", "")
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 24)] + "\n…[display truncated]"


def safe_presence_network_payload(value: Any, *, depth: int = 0) -> Any:
    """Bound and redact private-LAN data before it crosses the Presence API."""

    if depth > 6:
        return None
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return safe_presence_text(value, 1_000)
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for raw_key, item in list(value.items())[:80]:
            key = safe_presence_text(raw_key, 100).strip()
            if key:
                result[key] = safe_presence_network_payload(item, depth=depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        return [
            safe_presence_network_payload(item, depth=depth + 1)
            for item in list(value)[:MAX_EVENT_ITEMS]
        ]
    return safe_presence_text(value, 500)


def safe_presence_http_url(value: Any) -> str | None:
    """Allow only credential-free HTTP(S) links suitable for safe UI anchors."""

    raw = str(value or "").strip()
    if not raw or len(raw) > 2_000:
        return None
    parsed = urlsplit(raw)
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.username is not None
        or parsed.password is not None
        or not parsed.hostname
    ):
        return None
    return raw


def safe_presence_product_comparison(value: Any) -> dict[str, Any] | None:
    """Bound a verified agent comparison before it crosses the Presence API."""

    if not isinstance(value, dict) or not isinstance(value.get("products"), list):
        return None
    products: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    seen_urls: set[str] = set()
    for raw in value["products"][:4]:
        if not isinstance(raw, dict):
            continue
        name = safe_presence_text(raw.get("name") or "", 300).strip()
        source_url = safe_presence_http_url(raw.get("source_url"))
        name_key = re.sub(r"\W+", "", name).casefold()
        if (
            not name_key
            or source_url is None
            or name_key in seen_names
            or source_url in seen_urls
        ):
            continue
        source_kind = str(raw.get("source_kind") or "other").casefold()
        if source_kind not in {"manufacturer", "seller", "other"}:
            source_kind = "other"
        specs = raw.get("key_specs")
        products.append({
            "name": name,
            "source_url": source_url,
            "source_kind": source_kind,
            "seller": safe_presence_text(raw.get("seller") or "", 300).strip() or None,
            "manufacturer": safe_presence_text(
                raw.get("manufacturer") or "", 300
            ).strip() or None,
            "price_text": safe_presence_text(raw.get("price_text") or "", 100).strip() or None,
            "currency": safe_presence_text(raw.get("currency") or "", 20).strip() or None,
            "availability": safe_presence_text(
                raw.get("availability") or "", 200
            ).strip() or None,
            "key_specs": [
                safe_presence_text(item, 300).strip()
                for item in (specs if isinstance(specs, list) else [])[:8]
                if isinstance(item, str) and item.strip()
            ],
            "why_fit": safe_presence_text(raw.get("why_fit") or "", 700).strip(),
            "tradeoff": safe_presence_text(raw.get("tradeoff") or "", 700).strip(),
            "observed_at": safe_presence_text(
                raw.get("observed_at") or "", 100
            ).strip() or None,
            # Remote image URLs never cross this boundary. The UI deliberately
            # renders a local placeholder rather than leaking browsing metadata.
            "image_url": None,
        })
        seen_names.add(name_key)
        seen_urls.add(source_url)
    if not products:
        return None
    return {
        "ranking": safe_presence_text(value.get("ranking") or "", 1_000).strip(),
        "products": products,
    }


def safe_presence_metrics(value: Any) -> dict[str, Any]:
    """Return only valid prompt-free run metrics from the closed schema.

    Metrics are processed one field at a time. A future or malformed field is
    omitted instead of breaking delivery of the assistant's completed answer;
    a supported nested field (currently bounded tool counters) is recursively
    sanitized and any secret-shaped strings become a literal redaction marker.
    """

    if not isinstance(value, Mapping):
        return {}
    safe: dict[str, Any] = {}
    for raw_key, raw_value in list(value.items())[:MAX_EVENT_KEYS]:
        if not isinstance(raw_key, str):
            continue
        try:
            field = sanitize_run_metrics(
                {raw_key: raw_value},
                secret_policy="redact",
            )
        except (TypeError, ValueError):
            continue
        safe.update(field)
    return safe


def _safe_event_value(value: Any, *, depth: int = 0) -> Any:
    if depth > MAX_EVENT_DEPTH:
        return None
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return safe_presence_text(value)
    if isinstance(value, Mapping):
        output: dict[str, Any] = {}
        for raw_key, raw_value in list(value.items())[:MAX_EVENT_KEYS]:
            key = safe_presence_text(raw_key, 100).strip()
            if key:
                output[key] = _safe_event_value(raw_value, depth=depth + 1)
        return output
    if isinstance(value, (list, tuple)):
        return [
            _safe_event_value(item, depth=depth + 1)
            for item in list(value)[:MAX_EVENT_ITEMS]
        ]
    return safe_presence_text(value, 1_000)


def safe_presence_event_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Recursively redact an event while enforcing the metric sub-schema."""

    output: dict[str, Any] = {}
    for raw_key, value in list(payload.items())[:MAX_EVENT_KEYS]:
        key = safe_presence_text(raw_key, 100).strip()
        if not key:
            continue
        output[key] = (
            safe_presence_metrics(value)
            if key == "metrics"
            else _safe_event_value(value)
        )
    return output


def _top_counts(records: list[dict[str, Any]], field: str) -> list[dict[str, Any]]:
    counts = Counter(
        str(record[field])
        for record in records
        if isinstance(record.get(field), str) and record[field]
    )
    return [
        {"name": name, "count": count}
        for name, count in sorted(
            counts.items(), key=lambda item: (-item[1], item[0])
        )[:12]
    ]


def presence_performance_summary(
    rows: list[Mapping[str, Any]],
    *,
    requested_limit: int,
) -> dict[str, Any]:
    """Aggregate terminal Presence metrics without reading prompts or messages."""

    records: list[dict[str, Any]] = []
    finished_values: list[str] = []
    rejected_records = 0
    for row in rows:
        raw_metrics = row.get("metrics_json")
        try:
            decoded = json.loads(raw_metrics) if isinstance(raw_metrics, str) else raw_metrics
        except json.JSONDecodeError:
            rejected_records += 1
            continue
        metrics = safe_presence_metrics(decoded)
        # Redaction markers are useful at the live event boundary, but they are
        # not route/model identifiers and therefore must not become dashboard
        # dimensions. Keep valid sibling counters while dropping the marker.
        metrics = {
            key: (
                {
                    nested_key: nested_value
                    for nested_key, nested_value in value.items()
                    if nested_key != "[REDACTED]"
                }
                if isinstance(value, dict)
                else value
            )
            for key, value in metrics.items()
            if value != "[REDACTED]"
        }
        metrics = {
            key: value
            for key, value in metrics.items()
            if not isinstance(value, dict) or value
        }
        if not metrics:
            rejected_records += 1
            continue
        status = str(row.get("status") or "").strip().casefold()
        if status in {"completed", "failed", "cancelled", "interrupted"}:
            metrics["status"] = status
        records.append(metrics)
        finished_at = safe_presence_text(row.get("finished_at") or "", 100).strip()
        if finished_at:
            finished_values.append(finished_at)

    all_runs = aggregate_run_metrics(records)
    no_tool_records = [row for row in records if row.get("tool_calls") == 0]
    no_tool_runs = aggregate_run_metrics(no_tool_records)
    metrics = all_runs.get("metrics", {})
    first_visible_field = next(
        (
            field
            for field in (
                "end_to_end_ttft_ms",
                "first_visible_ms",
                "time_to_first_token_ms",
            )
            if field in metrics
        ),
        None,
    )
    return {
        "schema_version": 1,
        "requested_limit": requested_limit,
        "records": len(records),
        "rejected_records": rejected_records,
        "window": {
            "oldest_finished_at": min(finished_values) if finished_values else None,
            "newest_finished_at": max(finished_values) if finished_values else None,
        },
        "latency": {
            "queue_ms": metrics.get("queue_ms"),
            "first_visible_ms": (
                metrics.get(first_visible_field) if first_visible_field else None
            ),
            "first_visible_source": first_visible_field,
            "total_ms": metrics.get("total_ms"),
            "model_latency_ms": metrics.get("model_latency_ms"),
            "no_tool_total_ms": no_tool_runs.get("metrics", {}).get("total_ms"),
        },
        "work": {
            "context_chars": metrics.get("context_chars"),
            "model_attempts": metrics.get("model_attempts"),
            "retries": metrics.get("retries"),
            "tool_calls": metrics.get("tool_calls"),
        },
        "tokens": all_runs.get("tokens", {}),
        "routes": {
            "providers": _top_counts(records, "provider"),
            "models": _top_counts(records, "model"),
            "profiles": _top_counts(records, "profile"),
            "statuses": _top_counts(records, "status"),
        },
        "targets": {
            "queue_p95_ms": 250,
            "first_visible_p95_ms": 2_000,
            "no_tool_total_p95_ms": 5_000,
        },
        "privacy": {
            "prompts_read": False,
            "messages_read": False,
            "tool_arguments_read": False,
            "closed_metric_schema": True,
        },
    }


__all__ = [
    "safe_presence_event_payload",
    "safe_presence_http_url",
    "safe_presence_metrics",
    "safe_presence_network_payload",
    "safe_presence_product_comparison",
    "safe_presence_text",
    "presence_performance_summary",
]
