from __future__ import annotations

from typing import Any, Literal
from pydantic import Field, model_validator
from .common import EntityView, StrictModel


class SemanticConstraintHint(StrictModel):
    @model_validator(mode="before")
    @classmethod
    def normalize_term_pair(cls,value):
        if value is None: return {"raw_text":"","retrieval_text":""}
        if not isinstance(value,dict):return value
        value=dict(value)
        raw=value.get("raw_text") or ""
        retrieval=value.get("retrieval_text") or ""
        if raw and not retrieval:retrieval=raw
        if retrieval and not raw:raise ValueError("检索词缺少可核对的原文")
        value.update(raw_text=raw,retrieval_text=retrieval)
        return value

    raw_text: str = Field(default="", max_length=128)
    retrieval_text: str = Field(default="", max_length=128)


class AssetSemanticHints(StrictModel):
    """Language-only hints from the conversation model; never resolved asset IDs."""

    equip_no: SemanticConstraintHint = Field(default_factory=SemanticConstraintHint)
    point_no: SemanticConstraintHint = Field(default_factory=SemanticConstraintHint)
    equipment: SemanticConstraintHint = Field(default_factory=SemanticConstraintHint)
    equipment_type: SemanticConstraintHint = Field(default_factory=SemanticConstraintHint)
    area: SemanticConstraintHint = Field(default_factory=SemanticConstraintHint)
    point: SemanticConstraintHint = Field(default_factory=SemanticConstraintHint)
    component: SemanticConstraintHint = Field(default_factory=SemanticConstraintHint)
    position: SemanticConstraintHint = Field(default_factory=SemanticConstraintHint)
    direction: SemanticConstraintHint = Field(default_factory=SemanticConstraintHint)
    measurement: SemanticConstraintHint = Field(default_factory=SemanticConstraintHint)
    reference_target_level: Literal["none", "space", "equipment", "point"] = "none"
    # Explicit upstream routing decision.  None preserves compatibility with older
    # callers; False means Asset MCP must skip its own LLM/Embedding/Reranker path.
    needs_asset_lookup: bool | None = None
    collection_requested: bool = False
    # A descendant collection is different from resolving multiple matching roots.
    # The root entity is still resolved as one real space; a later deterministic
    # collection query expands below that root.
    descendant_collection_requested: bool = False
    descendant_target_level: Literal["none", "space", "equipment", "point"] = "none"
    descendant_target_type: SemanticConstraintHint = Field(default_factory=SemanticConstraintHint)
    # Compatibility fields: collection filtering is executed only by
    # query_scope_collection.  resolve_entity accepts these values so a rolling
    # upgrade or older Conversation worker cannot fail strict Pydantic validation.
    collection_filters: list[SemanticConstraintHint] = Field(default_factory=list, max_length=8)
    collection_output_mode: Literal["list", "count"] = "list"
    descendant_recursive: bool = True
    refresh_requested: bool = False


class ResolveEntityRequest(StrictModel):
    query: str = Field(min_length=1, max_length=4000)
    required_entity_level: Literal["any", "space", "area", "line", "equipment", "point"] = "any"
    active_entity: dict[str, Any] | None = None
    user_profile: dict[str, Any] | None = None
    previous_resolution: dict[str, Any] | None = None
    conversation_context: dict[str, Any] | None = None
    semantic_hints: AssetSemanticHints | None = None
    force_refresh: bool = False
    allow_context_reuse: bool = True
    limit: int = Field(default=10, ge=1, le=100)


class ResolveEntityResponse(StrictModel):
    success: bool
    status: str
    entity: EntityView | None = None
    confidence: float = Field(ge=0, le=1)
    needs_disambiguation: bool
    matches: list[dict[str, Any]] = Field(default_factory=list)
    candidate_count: int = 0
    message: str = ""
    # Legacy-compatible fields from the Dify output contract.
    legacy_status: str | None = None
    need_lookup: bool | None = None
    need_disambiguation: bool | None = None
    match_count: int | None = None
    top_similarity: float | None = None
    matches_json: str | None = None
    lookup_scope: str | None = None
    resolution_source: str | None = None
    profile_prior_used: bool | None = None
    query_scope: dict[str, Any] | None = None
    entity_constraints: dict[str, Any] | None = None
    decision: dict[str, Any] = Field(default_factory=dict)
    return_mode: str = "single"
    resolved_entities: list[dict[str, Any]] = Field(default_factory=list)
    query_fingerprint: str | None = None
