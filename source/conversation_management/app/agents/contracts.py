from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.attachments.contracts import AttachmentDescriptor
from app.domain.error_contract import LayeredError


class AgentAdapterType(StrEnum):
    DIFY = "dify"
    BUILTIN = "builtin"


class AgentHealthStatus(StrEnum):
    UNKNOWN = "UNKNOWN"
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    UNHEALTHY = "UNHEALTHY"
    DISABLED = "DISABLED"


class AgentResultStatus(StrEnum):
    COMPLETED = "COMPLETED"
    NEEDS_INPUT = "NEEDS_INPUT"
    REJECTED = "REJECTED"
    FAILED = "FAILED"
    STOPPED = "STOPPED"


class AgentDescriptor(BaseModel):
    model_config = ConfigDict(extra="allow")

    schema_version: str = "1.0"
    agent_id: str
    display_name: str
    adapter_type: AgentAdapterType
    version: str = "1.0.0"
    description: str
    public_description: str
    capabilities: list[str] = Field(default_factory=list)
    supported_entity_types: list[str] = Field(default_factory=lambda: ["none"])
    requires_data_access_token: bool = False
    supports_streaming: bool = False
    supports_resume: bool = False
    enabled: bool = True
    routing_enabled: bool = True
    execution_enabled: bool = True
    mandatory: bool = False
    maintenance_message: str | None = None
    priority: int = 100
    timeout_seconds: float = 120.0
    max_retries: int = 2
    health_status: AgentHealthStatus = AgentHealthStatus.UNKNOWN
    health_message: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    supports_attachments: bool = False
    supported_attachment_kinds: list[str] = Field(default_factory=list)
    supports_vision: bool = False
    accepted_content_kinds: list[str] = Field(default_factory=list)
    produced_content_kinds: list[str] = Field(default_factory=lambda: ["markdown"])


class AgentRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    schema_version: str = "1.0"
    task_id: str
    agent_id: str
    conversation_id: str
    branch_id: str
    app_code: str
    query: str
    objective: str = ""
    execution_mode: str = "normal"
    locale: str = "zh-CN"
    memory_context: dict[str, Any] = Field(default_factory=dict)
    user_profile: dict[str, Any] = Field(default_factory=dict)
    resolved_entity: dict[str, Any] = Field(default_factory=dict)
    active_entity: dict[str, Any] = Field(default_factory=dict)
    entity_result: dict[str, Any] = Field(default_factory=dict)
    query_scope: dict[str, Any] = Field(default_factory=dict)
    entity_constraints: dict[str, Any] = Field(default_factory=dict)
    business_intent: dict[str, Any] = Field(default_factory=dict)
    active_diagnosis_task: dict[str, Any] | None = None
    registry_snapshot: dict[str, Any] = Field(default_factory=dict)
    external_conversation_id: str | None = None
    attachments: list[AttachmentDescriptor] = Field(default_factory=list)
    content_envelope: dict[str, Any] = Field(default_factory=dict)
    understanding_results: list[dict[str, Any]] = Field(default_factory=list)
    prior_observations: list[dict[str, Any]] = Field(default_factory=list)
    model_policy: dict[str, Any] = Field(default_factory=dict)
    workflow_id: str | None = None
    workflow_step_id: str | None = None


class AgentResult(BaseModel):
    model_config = ConfigDict(extra="allow")

    schema_version: str = "1.0"
    agent_id: str
    status: AgentResultStatus
    answer_markdown: str = ""
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    missing_information: list[str] = Field(default_factory=list)
    can_support_final_answer: bool = False
    state_updates: dict[str, Any] = Field(default_factory=dict)
    external_ids: dict[str, str | None] = Field(default_factory=dict)
    tool_results: list[dict[str, Any]] = Field(default_factory=list)
    artifacts: list[dict[str, Any]] = Field(default_factory=list)
    citations: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    execution_summary: dict[str, Any] = Field(default_factory=dict)
    error_code: str | None = None
    error_message: str | None = None
    error: LayeredError | None = None


class AgentHealthResult(BaseModel):
    agent_id: str
    status: AgentHealthStatus
    message: str
    checked_at: str
    details: dict[str, Any] = Field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AgentExecutionRuntime:
    task_id: UUID
    task_start_monotonic: float
    user_token: str
    data_access_token: str
    context_key: str
    request_id: str
    event_service: Any
    redis: Any
    settings: Any
    is_cancelled: Any
    cache_external_ids: Any
    span_id: str | None = None
    parent_span_id: str | None = None
    execution_mode: str = "normal"


class AgentAdapter(Protocol):
    descriptor: AgentDescriptor

    async def execute(self, request: AgentRequest, runtime: AgentExecutionRuntime) -> AgentResult: ...

    async def health_check(self) -> AgentHealthResult: ...
