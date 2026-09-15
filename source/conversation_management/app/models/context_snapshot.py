from __future__ import annotations

from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, Integer, Text, func
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UUIDPrimaryKeyMixin


class ContextSnapshot(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "context_snapshots"

    conversation_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    branch_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("conversation_branches.id", ondelete="CASCADE"), nullable=False
    )
    up_to_message_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("messages.id", ondelete="CASCADE"), nullable=False
    )
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    message_count: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[object] = mapped_column(DateTime(timezone=True), server_default=func.now())
