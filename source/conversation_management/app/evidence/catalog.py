from __future__ import annotations

from typing import Any

from app.evidence.models import EvidenceCatalogEntry
from app.models.evidence import EvidenceObject


OPERATIONS_BY_KIND = {
    "scalar": ["describe", "summarize"],
    "record": ["describe", "summarize", "project"],
    "dataset": ["describe", "summarize", "materialize", "project", "filter", "aggregate", "sort", "slice"],
    "table": ["describe", "summarize", "materialize", "project", "filter", "aggregate", "sort", "slice"],
    "timeseries": ["describe", "summarize", "slice", "aggregate"],
    "waveform": ["describe", "summarize", "slice"],
    "spectrum": ["describe", "summarize", "slice"],
    "document": ["describe", "summarize", "search", "read_section"],
    "image": ["describe", "summarize"],
    "analysis": ["describe", "summarize", "lineage", "supporting_evidence"],
    "model_output": ["describe", "summarize", "lineage", "supporting_evidence"],
    "artifact": ["describe", "summarize", "materialize"],
    "reference": ["describe", "summarize", "materialize"],
    "graph": ["describe", "summarize", "lineage"],
}


def available_operations(evidence: EvidenceObject) -> list[str]:
    ops = list(OPERATIONS_BY_KIND.get(evidence.kind, ["describe", "summarize"]))
    if evidence.storage_backend in {"source_reference", "asset_snapshot", "attachment_reference", "artifact_reference"} and "materialize" not in ops:
        ops.append("materialize")
    return ops


def to_catalog_entry(evidence: EvidenceObject, *, expose_storage: bool = False) -> dict[str, Any]:
    entry = EvidenceCatalogEntry(
        evidence_id=evidence.evidence_id,
        semantic_type=evidence.semantic_type,
        kind=evidence.kind,
        authority=evidence.authority,
        subject=dict(evidence.subject or {}),
        scope=dict(evidence.scope or {}),
        content_descriptor=dict(evidence.content_descriptor or {}),
        summary=dict(evidence.summary or {}),
        completeness=dict(evidence.completeness or {}),
        freshness=dict(evidence.freshness or {}),
        available_operations=available_operations(evidence),
        storage_backend=evidence.storage_backend if expose_storage else None,
        created_at=evidence.created_at,
    )
    value = entry.model_dump(mode="json")
    if not expose_storage:
        value.pop("storage_backend", None)
    return value
