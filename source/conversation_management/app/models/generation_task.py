from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class GenerationTask(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "generation_tasks"
    __table_args__ = (
        Index("idx_tasks_status", "status", "created_at"),
        Index(
            "uq_task_idempotency",
            "conversation_id",
            "idempotency_key",
            unique=True,
            postgresql_where="idempotency_key IS NOT NULL",
        ),
    )

    conversation_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    branch_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("conversation_branches.id", ondelete="CASCADE"), nullable=False
    )
    user_message_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("messages.id", ondelete="CASCADE"), nullable=False
    )
    assistant_message_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("messages.id", ondelete="SET NULL")
    )
    operation: Mapped[str] = mapped_column(String(24), nullable=False, default="SEND")
    execution_mode: Mapped[str] = mapped_column(String(16), nullable=False, default="normal")
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    event_sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    idempotency_key: Mapped[str | None] = mapped_column(String(128))
    request_id: Mapped[str | None] = mapped_column(String(64))
    entity_workflow_run_id: Mapped[str | None] = mapped_column(String(128))
    dify_task_id: Mapped[str | None] = mapped_column(String(128))
    worker_id: Mapped[str | None] = mapped_column(String(128))
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
