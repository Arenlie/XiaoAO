from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

EvidenceAuthority = Literal[
    "AUTHORITATIVE", "COMPUTED", "DERIVED", "MODEL_INFERRED",
    "USER_PROVIDED", "KNOWLEDGE", "EXTERNAL",
]
EvidenceKind = Literal[
    "scalar", "record", "dataset", "timeseries", "waveform", "spectrum",
    "image", "document", "table", "graph", "artifact", "analysis",
    "model_output", "reference",
]
CompletenessStatus = Literal["complete", "partial", "unknown", "not_applicable"]
FreshnessClass = Literal["immutable", "slow_changing", "dynamic", "realtime"]


class EvidenceCompleteness(BaseModel):
    status: CompletenessStatus = "unknown"
    coverage: float | None = Field(default=None, ge=0.0, le=1.0)
    expected_count: int | None = Field(default=None, ge=0)
    available_count: int | None = Field(default=None, ge=0)
    materialized_count: int | None = Field(default=None, ge=0)
    has_more: bool = False
    reason: str | None = None


class EvidenceFreshness(BaseModel):
    observed_at: datetime | None = None
    valid_until: datetime | None = None
    immutable: bool = True
    freshness_class: FreshnessClass = "immutable"

    @property
    def stale(self) -> bool:
        if self.immutable or self.valid_until is None:
            return False
        deadline = self.valid_until
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=UTC)
        return datetime.now(UTC) > deadline


class EvidenceDraft(BaseModel):
    kind: EvidenceKind
    semantic_type: str = Field(min_length=1, max_length=96)
    authority: EvidenceAuthority
    subject: dict[str, Any] = Field(default_factory=dict)
    scope: dict[str, Any] = Field(default_factory=dict)
    content_descriptor: dict[str, Any] = Field(default_factory=dict)
    summary: dict[str, Any] = Field(default_factory=dict)
    completeness: EvidenceCompleteness = Field(default_factory=EvidenceCompleteness)
    freshness: EvidenceFreshness = Field(default_factory=EvidenceFreshness)
    storage_backend: str = Field(min_length=1, max_length=48)
    storage_ref: dict[str, Any] = Field(default_factory=dict)
    inline_payload: Any | None = None
    source_system: str = Field(min_length=1, max_length=96)
    source_tool: str | None = Field(default=None, max_length=192)
    immutable: bool = True
    checksum: str | None = Field(default=None, max_length=128)
    observed_at: datetime | None = None
    supersedes_evidence_id: UUID | None = None


class EvidenceCatalogEntry(BaseModel):
    evidence_id: UUID
    semantic_type: str
    kind: str
    authority: str
    subject: dict[str, Any] = Field(default_factory=dict)
    scope: dict[str, Any] = Field(default_factory=dict)
    content_descriptor: dict[str, Any] = Field(default_factory=dict)
    summary: dict[str, Any] = Field(default_factory=dict)
    completeness: dict[str, Any] = Field(default_factory=dict)
    freshness: dict[str, Any] = Field(default_factory=dict)
    available_operations: list[str] = Field(default_factory=list)
    storage_backend: str | None = None
    created_at: datetime | None = None


class EvidenceRequest(BaseModel):
    evidence_id: UUID
    operation: Literal[
        "describe", "summarize", "materialize", "project", "filter", "sort",
        "slice", "aggregate", "search", "read_section", "lineage",
        "supporting_evidence",
    ] = "describe"
    filters: list[dict[str, Any]] = Field(default_factory=list, max_length=16)
    projection: list[str] = Field(default_factory=list, max_length=64)
    aggregation: dict[str, Any] | None = None
    sort: list[dict[str, Any]] = Field(default_factory=list, max_length=8)
    offset: int | None = Field(default=0, ge=0)
    limit: int | None = Field(default=None, ge=1)
    detail_level: Literal["L0", "L1", "L2", "L3"] = "L1"


class EvidenceSlice(BaseModel):
    evidence_id: UUID
    operation: str
    data: Any = None
    total_count: int | None = None
    returned_count: int | None = None
    truncated: bool = False
    has_more: bool = False
    materialized: bool = True
    storage_backend: str | None = None
    storage_ref: dict[str, Any] = Field(default_factory=dict)
    limitation: str | None = None
    available_operations: list[str] = Field(default_factory=list)


class EvidenceRequirement(BaseModel):
    semantic_type: str
    # One requirement may accept several semantic types from the same evidence family
    # (for example asset -> equipment_entity / point_entity / entity_set).
    acceptable_semantic_types: list[str] = Field(default_factory=list)
    family: str | None = None
    subject_constraint: dict[str, Any] = Field(default_factory=dict)
    scope_constraint: dict[str, Any] = Field(default_factory=dict)
    required_authority: list[str] = Field(default_factory=list)
    required_fields: list[str] = Field(default_factory=list)
    freshness: dict[str, Any] = Field(default_factory=dict)
    completeness: dict[str, Any] = Field(default_factory=dict)
    optional: bool = False


class TaskDelta(BaseModel):
    context_action: str = "NEW_TOPIC"
    subject_resolution: str = "resolve_or_reuse"
    goal_action: str = "answer"
    target_evidence_types: list[str] = Field(default_factory=list)
    freshness_requirement: str = "reuse_if_valid"
    preserve_subject: bool = False
    preserve_scope: bool = False
    output_requirement: dict[str, Any] = Field(default_factory=dict)
