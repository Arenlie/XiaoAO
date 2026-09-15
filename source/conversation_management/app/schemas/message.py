from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator

from app.schemas.common import ORMModel

ExecutionModeLiteral = Literal["quick", "normal", "expert"]


class SendMessageRequest(BaseModel):
    client_session_id: str | None = Field(default=None, min_length=1, max_length=128)
    conversation_id: UUID | None = None
    app_code: str = Field(default="xiaoao", min_length=1, max_length=64)
    content: str = Field(default="", max_length=20000)
    attachments: list[UUID] = Field(default_factory=list, max_length=6)
    execution_mode: ExecutionModeLiteral | None = None

    @model_validator(mode="after")
    def require_content_or_attachment(self) -> "SendMessageRequest":
        if not self.content.strip() and not self.attachments:
            raise ValueError("content 和 attachments 至少提供一个")
        return self


class SendMessageAccepted(BaseModel):
    task_id: UUID
    conversation_id: UUID
    user_message_id: UUID
    assistant_message_id: UUID
    title: str
    execution_mode: ExecutionModeLiteral
    stream_url: str
    agent_stream_url: str | None = None


class EditAndResubmitRequest(BaseModel):
    content: str = Field(default="", max_length=20000)
    attachments: list[UUID] | None = Field(default=None, max_length=6)
    execution_mode: ExecutionModeLiteral | None = None

    @model_validator(mode="after")
    def require_content_or_attachment(self) -> "EditAndResubmitRequest":
        if not self.content.strip() and self.attachments == []:
            raise ValueError("content 和 attachments 至少提供一个")
        return self


class RegenerateRequest(BaseModel):
    force_reresolve: bool = False
    execution_mode: ExecutionModeLiteral | None = None


class MessageView(ORMModel):
    id: UUID
    conversation_id: UUID
    branch_id: UUID
    parent_message_id: UUID | None
    replaces_message_id: UUID | None
    role: str
    kind: str
    content: str
    status: str
    entity_result: dict | None
    metadata_json: dict
    created_at: datetime
    alternative_index: int = 1
    alternative_count: int = 1


class ExecutionEventView(ORMModel):
    id: int
    sequence_no: int
    task_id: UUID
    message_id: UUID | None
    graph_mode: str
    span_id: str | None
    parent_span_id: str | None
    event_type: str
    stage: str | None
    actor_type: str
    actor_id: str | None
    status: str | None
    attempt: int
    input_payload: dict
    output_payload: dict
    payload: dict
    error_code: str | None
    error_message: str | None
    duration_ms: float | None
    created_at: datetime

    @field_validator("payload", mode="before")
    @classmethod
    def customer_progress_wording(cls, value):
        from app.output.progress import progress_payload
        return progress_payload(value)
