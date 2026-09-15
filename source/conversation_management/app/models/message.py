from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.enums import MessageStatus
from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class Message(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "messages"
    __table_args__ = (
        Index("idx_messages_conversation", "conversation_id", "created_at"),
        Index("idx_messages_parent", "parent_message_id", "role", "created_at"),
    )

    conversation_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    branch_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("conversation_branches.id", ondelete="CASCADE"), nullable=False
    )
    parent_message_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("messages.id", ondelete="SET NULL")
    )
    replaces_message_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("messages.id", ondelete="SET NULL")
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False, default="TEXT")
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=MessageStatus.PENDING.value
    )
    include_in_context: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    entity_result: Mapped[dict | None] = mapped_column(JSONB)
    dify_message_id: Mapped[str | None] = mapped_column(String(128))
    metadata_json: Mapped[dict] = mapped_column("metadata", JSONB, nullable=False, default=dict)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
