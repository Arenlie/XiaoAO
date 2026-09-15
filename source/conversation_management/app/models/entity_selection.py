from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class PendingEntitySelection(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "pending_entity_selections"
    __table_args__ = (
        Index("idx_pending_entity_selection_conversation", "conversation_id", "status"),
    )

    task_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("generation_tasks.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
    )
    conversation_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
    )
    original_query: Mapped[str] = mapped_column(Text, nullable=False)
    candidates: Mapped[list] = mapped_column(JSONB, nullable=False)
    selected_entity: Mapped[dict | None] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="PENDING")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
