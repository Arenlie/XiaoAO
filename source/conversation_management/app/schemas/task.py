from __future__ import annotations

from datetime import datetime
from uuid import UUID
from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.common import ORMModel
from app.schemas.message import ExecutionEventView


class TaskView(ORMModel):
    id: UUID
    conversation_id: UUID
    branch_id: UUID
    user_message_id: UUID
    assistant_message_id: UUID | None
    operation: str
    execution_mode: str
    status: str
    error_code: str | None
    error_message: str | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None


class TaskExecutionHistory(BaseModel):
    task: TaskView
    events: list[ExecutionEventView]
    next_after_sequence: int = 0
    has_more: bool = False


class StopTaskResponse(BaseModel):
    task_id: UUID
    status: str


class EntitySelectionRequest(BaseModel):
    candidate_id: str = Field(min_length=1, max_length=512)


class EntitySelectionView(BaseModel):
    task_id: UUID
    assistant_message_id: UUID | None = None
    status: str
    candidates: list[dict]
    expires_at: datetime
    expires_in_seconds: int
    last_event_id: str


class DiagnosisModeRequest(BaseModel):
    confirmation_id: UUID
    mode: Literal["quick", "detailed", "cancel"]
