from __future__ import annotations

from enum import StrEnum
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field
from app.domain.error_contract import LayeredError


class ToolProviderType(StrEnum):
    MCP = "mcp"
    DIFY_WORKFLOW = "dify_workflow"
    HTTP = "http"
    LOCAL = "local"


class ToolResultStatus(StrEnum):
    SUCCESS = "SUCCESS"
    NEEDS_INPUT = "NEEDS_INPUT"
    REJECTED = "REJECTED"
    FAILED = "FAILED"


class ToolDescriptor(BaseModel):
    model_config = ConfigDict(extra="allow")

    schema_version: str = "1.0"
    tool_id: str
    display_name: str
    provider_type: ToolProviderType
    description: str
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    requires_data_access_token: bool = False
    enabled: bool = True
    timeout_seconds: float = 60.0
    metadata: dict[str, Any] = Field(default_factory=dict)
    supports_attachments: bool = False
    supported_attachment_kinds: list[str] = Field(default_factory=list)


class ToolCallRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    schema_version: str = "1.0"
    tool_id: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    task_id: str
    conversation_id: str
    branch_id: str
    user_token: str
    attachments: list[dict[str, Any]] = Field(default_factory=list)
    workflow_id: str | None = None
    workflow_step_id: str | None = None


class ToolCallResult(BaseModel):
    model_config = ConfigDict(extra="allow")

    schema_version: str = "1.0"
    tool_id: str
    status: ToolResultStatus
    content: Any = None
    structured_content: dict[str, Any] = Field(default_factory=dict)
    requirements: list[dict[str, Any]] = Field(default_factory=list)
    error_code: str | None = None
    error_message: str | None = None
    error: LayeredError | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ToolProvider(Protocol):
    async def call(
        self,
        descriptor: ToolDescriptor,
        request: ToolCallRequest,
        *,
        data_access_token: str | None,
    ) -> ToolCallResult: ...
