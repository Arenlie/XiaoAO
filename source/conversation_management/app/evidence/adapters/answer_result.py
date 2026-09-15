from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from app.evidence.adapters.common import freshness_for, scope_from_state, subject_from_state
from app.evidence.models import EvidenceCompleteness, EvidenceDraft


def adapt_answer_result(
    result: dict[str, Any],
    state: dict[str, Any],
    *,
    assistant_message_id: UUID,
) -> EvidenceDraft | None:
    result_id = str(result.get("result_id") or "")
    if not result_id:
        return None
    plan = result.get("plan") if isinstance(result.get("plan"), dict) else {}
    domain = str(plan.get("domain") or "").lower()
    rows = list(result.get("rows") or []) if isinstance(result.get("rows"), list) else []
    total = result.get("total_count")
    try:
        total_count = int(total) if total is not None else len(rows)
    except (TypeError, ValueError):
        total_count = len(rows)
    semantic = {
        "asset": "entity_set",
        "health": "health_score_set" if total_count != 1 else "health_score",
        "alarm": "alarm_set",
        "sensor": "sensor_fault_set",
    }.get(domain, "dataset_result")
    complete_flag = result.get("source_complete")
    if complete_flag is True:
        status = "complete"
    elif complete_flag is False:
        status = "partial"
    else:
        status = "complete" if result.get("display_complete") is True else "unknown"
    snapshot = result.get("asset_snapshot")
    if snapshot:
        storage_backend = "asset_snapshot"
        storage_ref = {
            "query_id": str(snapshot),
            "message_id": str(assistant_message_id),
            "result_id": result_id,
            "snapshot_at": result.get("snapshot_at"),
            "expires_at": result.get("snapshot_expires_at"),
        }
    else:
        storage_backend = "message_answer_result"
        storage_ref = {"message_id": str(assistant_message_id), "result_id": result_id}
    observed_at = datetime.now(UTC)
    return EvidenceDraft(
        kind="dataset" if semantic.endswith("set") or semantic in {"entity_set", "alarm_set", "sensor_fault_set", "dataset_result"} else "record",
        semantic_type=semantic,
        authority="AUTHORITATIVE",
        subject=subject_from_state(state),
        scope=scope_from_state(state),
        content_descriptor={
            "result_id": result_id,
            "domain": domain,
            "operation": plan.get("operation"),
            "row_fields": (
                sorted({str(k) for row in rows[:20] if isinstance(row, dict) for k in row.keys()})[:64]
                or (["entity_type", "entity_key", "name", "equip_no", "area", "equipment_type", "model"] if snapshot and domain == "asset" else [])
            ),
            "count": total_count,
        },
        summary={
            "count": total_count,
            "displayed_count": result.get("displayed_count", len(rows)),
            "domain": domain,
            "operation": plan.get("operation"),
            "notes": list(result.get("notes") or [])[:8],
        },
        completeness=EvidenceCompleteness(
            status=status,
            coverage=(1.0 if status == "complete" else None),
            expected_count=total_count,
            available_count=total_count if snapshot else len(rows),
            materialized_count=len(rows),
            has_more=bool(total_count > len(rows)),
            reason=None if status == "complete" else "source_not_verified_complete",
        ),
        freshness=freshness_for(semantic, observed_at=observed_at),
        storage_backend=storage_backend,
        storage_ref=storage_ref,
        inline_payload=None,
        source_system="answer_result_compat",
        source_tool=None,
        immutable=semantic not in {"health_score", "health_score_set", "alarm_set", "sensor_fault_set", "entity_set"},
        observed_at=observed_at,
    )
