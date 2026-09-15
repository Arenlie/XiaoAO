from __future__ import annotations

from operator import add
from typing import Annotated, Any, Literal, TypedDict


class BaseGraphState(TypedDict, total=False):
    task_id: str
    conversation_id: str
    branch_id: str
    user_message_id: str
    assistant_message_id: str
    app_code: str
    operation: str
    graph_attempt: int
    diagnosis_choice: dict[str, Any]
    diagnosis_resume_state: dict[str, Any]
    diagnosis_confirmation: dict[str, Any]
    pending_diagnosis_context: dict[str, Any]
    analysis_depth: str
    selection_resume: bool
    execution_mode: Literal["quick", "normal", "expert"]
    query: str
    locale: str
    attachments: list[dict[str, Any]]

    memory_context: dict[str, Any]
    recent_messages: list[dict[str, Any]]
    recent_entities: list[dict[str, Any]]
    user_profile: dict[str, Any]
    active_entity: dict[str, Any]
    entity_resolution: dict[str, Any]
    selected_entity: dict[str, Any]
    resolved_entity: dict[str, Any]
    entity_result: dict[str, Any]
    query_scope: dict[str, Any]
    entity_constraints: dict[str, Any]
    entity_dependency: dict[str, Any]
    sensor_target: dict[str, Any]
    sensor_identity_attempted: bool
    sensor_identity_error: str | None
    sensor_identity_resolution: dict[str, Any]
    sensor_identity_corrections: int
    # When Task Understanding contains an explicit new asset expression, the previous
    # branch entity must not remain authoritative if the new root is unresolved.
    invalidate_active_entity: bool
    explicit_current_identity: bool
    business_intent: dict[str, Any]
    # Evidence Fabric persistent workspace. These are lightweight manifests/catalogs;
    # large payloads remain behind Evidence Broker/storage references.
    topic_workspace: dict[str, Any]
    evidence_catalog: list[dict[str, Any]]
    task_delta: dict[str, Any]
    evidence_requirements: list[dict[str, Any]]
    answer_results: Annotated[list[dict[str, Any]], add]
    query_result: dict[str, Any]
    query_fact_text: str
    completion_evaluation: dict[str, Any]
    deterministic_output_ready: bool
    asset_query_result: dict[str, Any]
    asset_query_facts_emitted: bool
    business_workflow: dict[str, Any]
    clarification_context: dict[str, Any]
    active_diagnosis_task: dict[str, Any] | None
    speed_rpm: float | None

    registry_snapshot: dict[str, Any]
    content_envelope: dict[str, Any]
    understanding_results: list[dict[str, Any]]
    observations: Annotated[list[dict[str, Any]], add]
    external_ids: dict[str, str | None]
    root_span_id: str

    final_answer: str
    final_status: Literal["COMPLETED", "WAITING_INPUT", "WAITING_SELECTION", "WAITING_CONFIRMATION", "FAILED", "STOPPED"]
    errors: Annotated[list[dict[str, Any]], add]


class QuickGraphState(BaseGraphState, total=False):
    quick_result: dict[str, Any]


class NormalGraphState(BaseGraphState, total=False):
    # Legacy batch-plan fields remain readable for checkpoint compatibility.
    supervisor_plan: dict[str, Any]
    pending_calls: list[dict[str, Any]]
    completed_calls: list[dict[str, Any]]
    # Bounded ReAct runtime state used by normal mode.
    current_step: int
    agent_call_count: int
    next_calls: list[dict[str, Any]]
    next_call: dict[str, Any] | None
    supervisor_verdict: dict[str, Any]
    supervisor_answer_draft: str
    visited_actions: list[str]
