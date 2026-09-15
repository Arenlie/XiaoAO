from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from app.domain.enums import EntityStatus


class EntityMatch(BaseModel):
    candidate_id: str
    entity_type: str | None = None
    score: float | None = None
    data: dict[str, Any] = Field(default_factory=dict)


class EntityLookupResult(BaseModel):
    status: EntityStatus
    need_lookup: bool
    need_disambiguation: bool = False
    matches: list[dict[str, Any]] = Field(default_factory=list)
    match_count: int = 0
    top_similarity: float = 0.0
    resolved_entity: dict[str, Any] | None = None
    message: str | None = None
    workflow_run_id: str | None = None
    profile_prior_used: bool = False
    lookup_scope: str | None = None
    resolution_source: str | None = None
    query_scope: dict[str, Any] = Field(default_factory=dict)
    entity_constraints: dict[str, Any] = Field(default_factory=dict)
    decision: dict[str, Any] = Field(default_factory=dict)
    return_mode: str = "single"
    query_fingerprint: str | None = None
