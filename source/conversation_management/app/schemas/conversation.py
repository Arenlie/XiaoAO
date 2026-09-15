from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field

from app.schemas.common import ORMModel
from app.schemas.message import MessageView


class CreateConversationRequest(BaseModel):
    app_code: str = Field(default="xiaoao", min_length=1, max_length=64)


class RenameConversationRequest(BaseModel):
    title: str = Field(min_length=1, max_length=100)


class ConversationListItem(ORMModel):
    id: UUID
    title: str
    title_source: str
    is_pinned: bool
    pinned_at: datetime | None
    last_message_preview: str | None
    last_message_at: datetime | None
    active_branch_id: UUID | None
    created_at: datetime


class ConversationDetail(ConversationListItem):
    app_code: str
    status: str
    messages: list[MessageView] = Field(default_factory=list)


class BranchView(ORMModel):
    id: UUID
    conversation_id: UUID
    parent_branch_id: UUID | None
    fork_type: str
    forked_from_message_id: UUID | None
    active_leaf_message_id: UUID | None
    dify_conversation_id: str | None
    active_entity: dict | None
    summary: str | None
    status: str
    created_at: datetime
