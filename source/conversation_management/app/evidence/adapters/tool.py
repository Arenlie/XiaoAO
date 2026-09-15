from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.evidence.adapters.common import (
    checksum, compact_summary, completeness_from_payload, freshness_for, json_size,
    scope_from_state, subject_from_state,
)
from app.evidence.models import EvidenceDraft


def _semantic(tool_id: str) -> tuple[str, str, str]:
    tool = tool_id.lower()
    if tool == "evidence.broker":
        return "reference", "evidence_slice", "DERIVED"
    if "query_health_score" in tool or "health_collection" in tool or "health_dimensions" in tool:
        return "record", "health_score", "AUTHORITATIVE"
    if "alarm" in tool and ("query_alarm" in tool or "health_dimension_alarms" in tool or "comprehensive_alarm" in tool):
        return "dataset", "alarm_set", "AUTHORITATIVE"
    if "sensor_fault" in tool or "sensor_status" in tool or "offline_sensor" in tool or "sensor_monitor" in tool:
        return "dataset", "sensor_fault_set", "AUTHORITATIVE"
    if "get_waveform" in tool:
        return "waveform", "waveform", "AUTHORITATIVE"
    if "get_device_data" in tool or "point_data_batch" in tool or "get_data_snapshot" in tool:
        return "dataset", "waveform_set", "AUTHORITATIVE"
    if "feature_trend" in tool or "temperature_trend" in tool:
        return "timeseries", "trend", "AUTHORITATIVE"
    if "extract_vibration_features" in tool:
        return "record", "time_domain_features", "COMPUTED"
    if "extract_rotational_speed_feature" in tool or "point_rpm_batch" in tool:
        return "record", "computed_metric", "COMPUTED"
    if "diagnose_" in tool or "analyze_chart" in tool or "point_diagnosis_batch" in tool:
        return "analysis", "diagnosis_result", "DERIVED"
    if "query_points" in tool or "query_devices" in tool or "query_scope_collection" in tool or "business.query" in tool:
        return "dataset", "entity_set", "AUTHORITATIVE"
    if "query_equipment_info" in tool or "space_tree" in tool or "space_children" in tool:
        return "record", "equipment_entity", "AUTHORITATIVE"
    if tool.startswith("knowledge."):
        return "document", "knowledge_chunk_set", "KNOWLEDGE"
    if tool.startswith("file."):
        return "document", "attachment_document", "USER_PROVIDED"
    if tool.startswith("spreadsheet."):
        return "table", "attachment_table", "COMPUTED"
    return "model_output", "tool_result", "DERIVED"


def adapt_tool_observation(
    observation: dict[str, Any],
    state: dict[str, Any],
    *,
    inline_max_bytes: int,
) -> EvidenceDraft | None:
    if str(observation.get("status") or "").upper() not in {"SUCCESS", "COMPLETED"}:
        return None
    tool_id = str(observation.get("tool_id") or "")
    if not tool_id or tool_id == "evidence.broker":
        return None
    kind, semantic_type, authority = _semantic(tool_id)
    payload: Any = None
    evidence_list = observation.get("evidence")
    if isinstance(evidence_list, list) and evidence_list:
        item = evidence_list[-1]
        if isinstance(item, dict):
            payload = item.get("content")
    if payload is None:
        result = observation.get("tool_result")
        if isinstance(result, dict):
            payload = result.get("structured_content") or result.get("content") or result
        else:
            payload = result
    args = observation.get("arguments") if isinstance(observation.get("arguments"), dict) else {}
    subject = subject_from_state(state)
    for key in ("equip_no", "equipment_no", "point_no", "pointNo", "area", "scope_id"):
        if args.get(key) not in (None, ""):
            subject.setdefault(key, args[key])
    content_descriptor = {
        "tool_id": tool_id,
        "call_id": observation.get("call_id"),
        "payload_bytes": json_size(payload),
        "fields": list(payload.keys())[:64] if isinstance(payload, dict) else [],
    }
    complete = completeness_from_payload(payload)
    summary = compact_summary(payload)
    storage_backend = "inline_json"
    storage_ref: dict[str, Any] = {}
    inline_payload: Any | None = payload

    query_id = payload.get("query_id") if isinstance(payload, dict) else None
    if semantic_type == "entity_set" and query_id:
        storage_backend = "asset_snapshot"
        storage_ref = {"query_id": str(query_id), "tool_id": tool_id,
                       "snapshot_at": payload.get("snapshot_at") if isinstance(payload, dict) else None,
                       "expires_at": payload.get("expires_at") if isinstance(payload, dict) else None}
        inline_payload = None
        if isinstance(payload, dict):
            members = payload.get("devices") or payload.get("items") or payload.get("rows")
            if isinstance(members, list):
                complete.materialized_count = len(members)
    elif kind in {"waveform", "timeseries"} or semantic_type in {"waveform_set", "trend", "trend_set"}:
        storage_backend = "source_reference"
        storage_ref = {"tool_id": tool_id, "arguments": dict(args)}
        inline_payload = None
    elif authority == "USER_PROVIDED" and isinstance(args, dict) and args.get("attachment_id"):
        storage_backend = "attachment_reference"
        storage_ref = {"attachment_id": str(args["attachment_id"]), "tool_id": tool_id}
        inline_payload = None
    elif json_size(payload) > inline_max_bytes:
        storage_backend = "task_event_reference"
        storage_ref = {
            "call_id": str(observation.get("call_id") or ""),
            "tool_id": tool_id,
        }
        inline_payload = None
        if complete.status == "complete":
            complete.status = "partial"
            complete.reason = "payload_exceeds_inline_limit"
    observed_at = datetime.now(UTC)
    return EvidenceDraft(
        kind=kind,
        semantic_type=semantic_type,
        authority=authority,
        subject=subject,
        scope=scope_from_state(state),
        content_descriptor=content_descriptor,
        summary=summary,
        completeness=complete,
        freshness=freshness_for(semantic_type, observed_at=observed_at),
        storage_backend=storage_backend,
        storage_ref=storage_ref,
        inline_payload=inline_payload,
        source_system=tool_id.split(".")[1] if "." in tool_id else "conversation",
        source_tool=tool_id,
        immutable=semantic_type not in {"health_score", "health_score_set", "alarm_set", "sensor_fault_set", "entity_set", "equipment_entity"},
        checksum=checksum(payload) if inline_payload is not None else None,
        observed_at=observed_at,
    )
