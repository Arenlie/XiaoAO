from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator
from app.context.models import ContextResolutionDecision
from app.query_contract import QueryPlan, ResultFollowup


class WorkflowStepDefinition(BaseModel):
    step_id: str
    display_name: str
    call_type: Literal["agent", "tool"]
    target_id: str
    objective: str
    required_entity_level: Literal["none", "area", "equipment", "point"] = "none"
    arguments: dict[str, Any] = Field(default_factory=dict)
    depends_on: list[str] = Field(default_factory=list)
    failure_policy: Literal["stop", "continue"] = "stop"


class WorkflowVariantDefinition(BaseModel):
    variant_id: str
    display_name: str
    selection_rule: str
    required_entity_level: Literal["none", "area", "equipment", "point"]
    supports_collection: bool = False
    steps: list[WorkflowStepDefinition]


class WorkflowDefinition(BaseModel):
    workflow_id: str
    display_name: str
    version: str
    description: str
    enabled: bool = True
    match_priority: int = 100
    source_file: str = "app/workflows/definitions.py"
    variants: list[WorkflowVariantDefinition]


class WorkflowSelection(BaseModel):
    workflow_id: str
    workflow_version: str
    display_name: str
    variant_id: str
    variant_display_name: str
    required_entity_level: str
    supports_collection: bool = False
    selection_reason: str
    source_file: str
    time_window_hours: float | None = Field(default=None, gt=0.0)


class AssetSemanticConstraint(BaseModel):
    """One model-understood asset phrase and its retrieval normalization.

    ``raw_text`` must be copied from the user's current utterance.  Asset MCP checks
    that provenance before it accepts ``retrieval_text`` as a database filter, so the
    supervisor can normalize operator language without inventing an asset identity.
    """

    @model_validator(mode="before")
    @classmethod
    def normalize_term_pair(cls,value):
        if value is None:return {"raw_text":"","retrieval_text":""}
        if not isinstance(value,dict):return value
        value=dict(value);raw=value.get("raw_text") or "";retrieval=value.get("retrieval_text") or ""
        if raw and not retrieval:retrieval=raw
        if retrieval and not raw:raise ValueError("检索词缺少可核对的原文")
        value.update(raw_text=raw,retrieval_text=retrieval)
        return value

    raw_text: str = Field(default="", max_length=128)
    retrieval_text: str = Field(default="", max_length=128)


class AssetSemanticHints(BaseModel):
    """Structured language hints for Asset MCP; never resolved IDs or facts.

    Semantic levels use ``space/equipment/point`` consistently.  ``area`` is accepted
    only as an input alias for compatibility with older Supervisor outputs and is
    normalized *before* Pydantic validates the contract.
    """

    @model_validator(mode="before")
    @classmethod
    def normalize_legacy_semantic_aliases(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        data = dict(value)
        if data.get("collection_output_mode") in (None, ""):
            data["collection_output_mode"] = "list"
        for key in ("reference_target_level", "descendant_target_level"):
            if str(data.get(key) or "").strip().lower() == "area":
                data[key] = "space"
        return data

    equip_no: AssetSemanticConstraint = Field(default_factory=AssetSemanticConstraint)
    point_no: AssetSemanticConstraint = Field(default_factory=AssetSemanticConstraint)
    equipment: AssetSemanticConstraint = Field(default_factory=AssetSemanticConstraint)
    equipment_type: AssetSemanticConstraint = Field(
        default_factory=AssetSemanticConstraint
    )
    area: AssetSemanticConstraint = Field(default_factory=AssetSemanticConstraint)
    point: AssetSemanticConstraint = Field(default_factory=AssetSemanticConstraint)
    component: AssetSemanticConstraint = Field(default_factory=AssetSemanticConstraint)
    position: AssetSemanticConstraint = Field(default_factory=AssetSemanticConstraint)
    direction: AssetSemanticConstraint = Field(default_factory=AssetSemanticConstraint)
    measurement: AssetSemanticConstraint = Field(default_factory=AssetSemanticConstraint)
    reference_target_level: Literal["none", "space", "equipment", "point"] = "none"
    needs_asset_lookup: bool = False
    collection_requested: bool = False
    descendant_collection_requested: bool = False
    descendant_target_level: Literal["none", "space", "equipment", "point"] = "none"
    descendant_target_type: AssetSemanticConstraint = Field(
        default_factory=AssetSemanticConstraint
    )
    collection_filters: list[AssetSemanticConstraint] = Field(default_factory=list, max_length=8)
    collection_output_mode: Literal["list", "count"] = "list"
    collection_output_expression: AssetSemanticConstraint = Field(
        default_factory=AssetSemanticConstraint
    )
    scope_mode: Literal["auto", "recursive", "direct_only"] = "auto"
    scope_expression: AssetSemanticConstraint = Field(default_factory=AssetSemanticConstraint)
    explicit_overrides: list[Literal[
        "equipment", "equipment_type", "area", "point", "component",
        "position", "direction", "measurement", "descendant_target_type",
        "collection_filters", "collection_output_mode", "scope_mode",
        "descendant_target_level",
    ]] = Field(default_factory=list, max_length=16)
    # Compatibility/output field. The model may emit it, but Semantic Policy rewrites
    # it deterministically before any tool arguments are built.
    descendant_recursive: bool = True
    refresh_requested: bool = False


class StructuredTimeRange(BaseModel):
    """LLM-resolved absolute time semantics for the current user turn."""

    mode: Literal["none", "range", "latest", "nearest", "latest_before"] = "none"
    start_time: str = ""
    end_time: str = ""
    target_time: str = ""
    source_expression: str = Field(default="", max_length=256)


class IndependentQuery(BaseModel):
    target: str = Field(min_length=1, max_length=250)
    query: str = Field(min_length=1, max_length=500)
    operation: Literal["health", "alarms", "equipment_info", "points", "sensor_active", "sensor_history", "sensor_offline", "sensor_monitoring", "sensor_overview", "sensor_points"]
    entity_level: Literal["equipment", "space", "point"] = "equipment"
    fault_type: str | None = Field(default=None, max_length=128)
    monitor_status: Literal["ONLINE", "OFFLINE", "SUSPENDED"] | None = None
    waveform_enabled: bool | None = None
    feature_kind: Literal["bias", "velocity", "temperature"] | None = None
    time_field: Literal["start_time", "end_time"] | None = "start_time"
    use_context_reference: bool = False
    time_range: StructuredTimeRange = Field(default_factory=StructuredTimeRange)


class IndependentQueries(BaseModel):
    queries: list[IndependentQuery] = Field(min_length=2, max_length=8)




class CompletionContract(BaseModel):
    """Structured definition of what must be true before this turn is complete.

    The task-understanding model emits this together with the business intent.  The
    runtime may fill conservative defaults from ``goal_frame`` for backward
    compatibility, but the contract is evaluated against real tool results before
    a Recipe or dynamic ReAct turn may finish.
    """

    actions: list[str] = Field(default_factory=list, max_length=12)
    target_granularity: Literal["none", "space", "equipment", "point", "record"] = "none"
    result_source: Literal["auto", "new_query", "previous_displayed", "previous_all", "context"] = "auto"
    collection_action: Literal["none", "new", "preserve", "refine", "refresh", "requery"] = "none"
    required_fields: list[str] = Field(default_factory=list, max_length=24)
    result_scope: Literal["unspecified", "all_returned", "all_verified", "top_n", "current_page", "sample"] = "unspecified"
    preserve_members: bool = False
    preserve_order: bool = False
    output_type: Literal["answer", "list", "table", "prose", "report", "count", "analysis"] = "answer"
    response_mode: Literal["auto", "facts_only", "facts_plus_explanation", "analysis"] = "auto"
    allow_partial: bool = True

    @model_validator(mode="before")
    @classmethod
    def normalize_values(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        data = dict(value)
        level = str(data.get("target_granularity") or "").strip().lower()
        if level == "area":
            data["target_granularity"] = "space"
        fields = []
        for raw in data.get("required_fields") or []:
            field = str(raw or "").strip()
            if field and field not in fields:
                fields.append(field)
        data["required_fields"] = fields[:24]
        actions = []
        for raw in data.get("actions") or []:
            action = str(raw or "").strip().lower()
            if action and action not in actions:
                actions.append(action)
        data["actions"] = actions[:12]
        return data


class TaskGoalFrame(BaseModel):
    """Goal-first semantic frame used by the normal ReAct control plane.

    ``space/equipment/point`` is the canonical semantic hierarchy.  Runtime adapters
    translate ``space`` to the legacy executor requirement name ``area`` only at the
    Asset-MCP boundary.  This prevents the model contract itself from using two names
    for the same concept.
    """

    @model_validator(mode="before")
    @classmethod
    def normalize_legacy_entity_levels(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        data = dict(value)
        for key in ("target_entity_level", "anchor_entity_level"):
            if str(data.get(key) or "").strip().lower() == "area":
                data[key] = "space"
        return data

    goal: str = ""
    # Evidence explicitly named by the user (for example "根据报警信息" or
    # "基于振动数据").  This is kept separate from planner-inferred evidence so
    # control-plane normalization can safely remove an accidentally over-escalated
    # diagnosis dependency without discarding evidence the user actually requested.
    requested_evidence_types: list[str] = Field(default_factory=list, max_length=12)
    evidence_types: list[str] = Field(default_factory=list, max_length=12)
    operations: list[str] = Field(default_factory=list, max_length=12)
    output_type: str = "answer"
    # Professional Diagnosis MCP is an escalation capability.  Generic status checks
    # such as "最近有没有出问题/异常" must remain false unless the user is actually
    # asking for a diagnostic conclusion (fault type/cause/mechanism/professional
    # diagnosis).  A dedicated confidence prevents one ambiguous classifier label
    # from forcing the expensive diagnosis pipeline.
    diagnosis_requested: bool = False
    diagnosis_request_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    target_entity_level: Literal["none", "space", "equipment", "point"] = "none"
    anchor_entity_level: Literal["none", "space", "equipment", "point", "any"] = "any"
    target_is_system_output: bool = False
    evidence_required: bool = False
    can_answer_from_context: bool = False


class SensorQueryIntent(BaseModel):
    operation: Literal["none", "active", "history", "offline", "monitoring", "overview", "points"] = "none"
    scope: Literal["global", "equipment", "point"] = "equipment"
    fault_type: str | None = Field(default=None, max_length=128)
    fault_status: Literal["PENDING_CONFIRMATION", "PENDING_REPAIR", "REPAIR_COMPLETED", "AUTO_RECOVERED", "DATA_INTERRUPTED"] | None = None
    time_field: Literal["start_time", "end_time"] | None = "start_time"
    monitor_status: Literal["ONLINE", "OFFLINE", "SUSPENDED"] | None = None
    waveform_enabled: bool | None = None
    feature_kind: Literal["bias", "velocity", "temperature"] | None = None
    limit: int = Field(default=50, ge=1, le=1000)

class AssetCollectionIntent(BaseModel):
    """Main model semantic decision; real IDs and SQL are never model outputs."""
    active: bool = False
    operation: Literal["count", "list", "group"] = "list"
    predicate: dict[str, Any] | None = None
    group_by: Literal["equipment_class", "equipment_subclass", "purpose", "structure_type", "model", "company", "plant", "line", "area"] | None = None
    reference_mode: Literal["new", "same_set", "refine_set", "refresh"] = "new"
    source_message_id: str | None = Field(default=None, max_length=128)
    source_expression: str = Field(default="", max_length=500)
    page_size: int = Field(default=1000, ge=1, le=5000)

    @model_validator(mode="after")
    def validate_intent(self):
        from app.asset_query_contract import validate_predicate
        if self.active:
            validate_predicate(self.predicate)
            if self.operation == "group" and not self.group_by:
                raise ValueError("分类汇总必须指定分组维度")
            if self.operation != "group" and self.group_by:
                raise ValueError("只有分类汇总使用分组维度")
            if self.reference_mode == "same_set" and self.predicate:
                raise ValueError("原集合不能重新填写筛选条件")
        return self


class WorkflowIntentClassification(BaseModel):
    """Structured semantic decision returned by the supervisor model.

    Natural-language routing is model-owned. This contract only validates the model
    output; it does not infer intent from keywords or regular expressions.
    """

    workflow_id: Literal[
        "diagnosis_analysis",
        "health_analysis",
        "asset_information_query",
        "sensor_information_query",
        "none",
    ] = "none"
    variant_id: str = "none"
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    reason: str = ""
    context_resolution: ContextResolutionDecision = Field(default_factory=ContextResolutionDecision)
    time_window_hours: float | None = Field(
        default=None,
        gt=0.0,
        description="用户明确提出的数据/诊断时间窗口，统一换算为小时；未明确时为 null。",
    )
    time_range: StructuredTimeRange = Field(default_factory=StructuredTimeRange)
    asset_semantics: AssetSemanticHints = Field(default_factory=AssetSemanticHints)
    asset_query: AssetCollectionIntent = Field(default_factory=AssetCollectionIntent)
    query_plan: QueryPlan = Field(default_factory=QueryPlan)
    result_followup: ResultFollowup = Field(default_factory=ResultFollowup)
    goal_frame: TaskGoalFrame = Field(default_factory=TaskGoalFrame)
    completion_contract: CompletionContract = Field(default_factory=CompletionContract)
    # Optional mature-recipe recommendation.  The runtime may ignore it and build a
    # dynamic MCP plan when the semantic fit is not exact.
    recipe_match_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    recipe_recommended: bool = False
    analysis_depth: Literal["unspecified", "quick", "detailed"] = "unspecified"
    analysis_depth_explicit: bool = False
    analysis_depth_confidence: float = Field(default=0.0, ge=0, le=1)
    responds_to_diagnosis_choice: bool = False
    knowledge_required: bool = False
    knowledge_opt_out: bool = False
    sensor_query: SensorQueryIntent = Field(default_factory=SensorQueryIntent)
    independent_queries: list[IndependentQuery] = Field(default_factory=list, max_length=8)
    knowledge_query: str = Field(default="", max_length=250)
    knowledge_bases: list[Literal["企业智库", "历史案例库", "硬件部署信息库"]] = Field(default_factory=list)
