from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any

from app.evidence.models import EvidenceCompleteness, EvidenceDraft, EvidenceFreshness


def json_size(value: Any) -> int:
    try:
        return len(json.dumps(value, ensure_ascii=False, default=str).encode("utf-8"))
    except Exception:
        return 0


def checksum(value: Any) -> str | None:
    try:
        data = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    except Exception:
        return None
    return hashlib.sha256(data).hexdigest()


def subject_from_state(state: dict[str, Any]) -> dict[str, Any]:
    for key in ("resolved_entity", "selected_entity", "active_entity"):
        value = state.get(key)
        if isinstance(value, dict) and value:
            return {
                k: value.get(k)
                for k in (
                    "entity_type", "name", "equip_no", "equipment_no", "point_no",
                    "pointNo", "space_id", "area", "company", "plant", "line",
                )
                if value.get(k) not in (None, "")
            }
    return {}


def scope_from_state(state: dict[str, Any]) -> dict[str, Any]:
    value = state.get("query_scope")
    return dict(value) if isinstance(value, dict) else {}


def completeness_from_payload(payload: Any) -> EvidenceCompleteness:
    if not isinstance(payload, dict):
        if isinstance(payload, list):
            return EvidenceCompleteness(status="complete", coverage=1.0, expected_count=len(payload), available_count=len(payload), materialized_count=len(payload), has_more=False)
        return EvidenceCompleteness(status="unknown")
    complete = payload.get("complete")
    if complete is None:
        complete = payload.get("result_complete")
    truncated = bool(payload.get("truncated") or payload.get("has_more") is True)
    total = payload.get("total_count", payload.get("count"))
    rows = None
    for key in ("rows", "items", "records", "devices", "points", "alarms", "members", "data"):
        if isinstance(payload.get(key), list):
            rows = payload[key]
            break
    available = len(rows) if rows is not None else None
    try:
        expected = int(total) if total is not None else available
    except (TypeError, ValueError):
        expected = available
    if complete is True and not truncated:
        status = "complete"
    elif complete is False or truncated:
        status = "partial"
    elif expected is not None and available is not None and available >= expected:
        status = "complete"
    else:
        status = "unknown"
    coverage = None
    if expected is not None and expected > 0 and available is not None:
        coverage = min(1.0, available / expected)
    return EvidenceCompleteness(
        status=status,
        coverage=coverage,
        expected_count=expected,
        available_count=available,
        materialized_count=available,
        has_more=truncated or bool(payload.get("has_more")),
        reason=("source_truncated" if truncated else None),
    )


def freshness_for(semantic_type: str, *, observed_at: datetime | None = None) -> EvidenceFreshness:
    now = observed_at or datetime.now(UTC)
    if semantic_type.startswith("attachment_") or semantic_type in {
        "diagnosis_result", "analysis_result", "time_domain_features",
        "frequency_domain_features", "computed_metric", "frequency_spectrum",
        "envelope_spectrum", "order_spectrum",
    }:
        return EvidenceFreshness(observed_at=now, immutable=True, freshness_class="immutable")
    if semantic_type in {"equipment_entity", "point_entity", "entity_set", "knowledge_chunk_set"}:
        return EvidenceFreshness(observed_at=now, valid_until=now + timedelta(days=1), immutable=False, freshness_class="slow_changing")
    if semantic_type in {"alarm_set", "sensor_fault_set", "sensor_monitoring_set"}:
        return EvidenceFreshness(observed_at=now, valid_until=now + timedelta(minutes=5), immutable=False, freshness_class="realtime")
    if semantic_type in {"health_score", "health_score_set"}:
        return EvidenceFreshness(observed_at=now, valid_until=now + timedelta(minutes=30), immutable=False, freshness_class="dynamic")
    if semantic_type in {"waveform", "waveform_set", "trend", "trend_set"}:
        return EvidenceFreshness(observed_at=now, immutable=True, freshness_class="immutable")
    return EvidenceFreshness(observed_at=now, valid_until=now + timedelta(minutes=30), immutable=False, freshness_class="dynamic")


def compact_summary(payload: Any, *, max_fields: int = 20) -> dict[str, Any]:
    if isinstance(payload, dict):
        summary: dict[str, Any] = {}
        for key in (
            "count", "total_count", "grade", "total_score", "score", "status", "success",
            "time_range", "start_time", "end_time", "target_time", "query_id", "requested_count",
            "valid_count", "missing_count", "point_count", "fault_count", "offline_count",
            "online_count", "diagnosis", "conclusion", "result", "summary",
        ):
            value = payload.get(key)
            if value not in (None, "", [], {}):
                if isinstance(value, str) and len(value) > 800:
                    value = value[:800] + "…"
                summary[key] = value
            if len(summary) >= max_fields:
                break
        if not summary:
            summary["fields"] = list(payload.keys())[:max_fields]
        return summary
    if isinstance(payload, list):
        return {"count": len(payload)}
    if isinstance(payload, str):
        return {"text": payload[:800] + ("…" if len(payload) > 800 else "")}
    return {"value": payload}
