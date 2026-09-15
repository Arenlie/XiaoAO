from __future__ import annotations

from app.evidence.models import EvidenceRequirement

LEGACY_TO_SEMANTIC: dict[str, list[str]] = {
    "asset": ["equipment_entity", "point_entity", "entity_set"],
    "health": ["health_score", "health_score_set"],
    "alarm": ["alarm_set"],
    "sensor": ["sensor_fault_set", "sensor_monitoring_set"],
    "sensor_active_faults": ["sensor_fault_set"],
    "sensor_fault_history": ["sensor_fault_set"],
    "sensor_points": ["entity_set"],
    "sensor_monitoring": ["sensor_fault_set", "sensor_monitoring_set"],
    "sensor_offline": ["sensor_fault_set", "sensor_monitoring_set"],
    "vibration": ["waveform", "waveform_set", "trend", "trend_set", "time_domain_features", "frequency_domain_features"],
    "temperature": ["trend", "trend_set"],
    "diagnosis_result": ["diagnosis_result"],
    "file": ["attachment_document", "attachment_table", "attachment_image", "analysis_result"],
    "conversation_context": [],
}

AUTHORITATIVE_ONLY = {
    "equipment_entity", "point_entity", "entity_set", "health_score", "health_score_set",
    "alarm_set", "sensor_fault_set", "sensor_monitoring_set", "waveform", "waveform_set",
    "trend", "trend_set",
}


def requirement_for(
    semantic_type: str,
    *,
    family: str | None = None,
    acceptable_semantic_types: list[str] | None = None,
    subject_constraint: dict | None = None,
    scope_constraint: dict | None = None,
    required_fields: list[str] | None = None,
    allow_partial: bool = True,
    freshness_mode: str = "reuse_if_valid",
) -> EvidenceRequirement:
    authorities = ["AUTHORITATIVE"] if semantic_type in AUTHORITATIVE_ONLY else []
    if semantic_type in {"time_domain_features", "frequency_domain_features", "computed_metric"}:
        authorities = ["COMPUTED", "DERIVED"]
    if semantic_type == "diagnosis_result":
        authorities = ["DERIVED", "MODEL_INFERRED", "COMPUTED"]
    if semantic_type.startswith("attachment_"):
        authorities = ["USER_PROVIDED", "COMPUTED"]
    if semantic_type == "analysis_result" and family == "file":
        authorities = ["MODEL_INFERRED", "DERIVED", "COMPUTED"]
    if semantic_type == "knowledge_chunk_set":
        authorities = ["KNOWLEDGE"]
    return EvidenceRequirement(
        semantic_type=semantic_type,
        acceptable_semantic_types=list(dict.fromkeys(acceptable_semantic_types or [semantic_type])),
        family=family,
        subject_constraint=dict(subject_constraint or {}),
        scope_constraint=dict(scope_constraint or {}),
        required_authority=authorities,
        required_fields=list(required_fields or []),
        freshness={"mode": freshness_mode},
        completeness={"allow_partial": bool(allow_partial)},
    )
