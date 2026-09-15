from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import BigInteger, DateTime, Float, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class TaskExecutionEvent(Base):
    __tablename__ = "task_execution_events"
    __table_args__ = (
        Index("uq_execution_events_task_sequence", "task_id", "sequence_no", unique=True),
        Index("idx_execution_events_task_id", "task_id", "sequence_no"),
        Index("idx_execution_events_message_id", "message_id", "id"),
        Index("idx_execution_events_span", "task_id", "span_id", "sequence_no"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    sequence_no: Mapped[int] = mapped_column(Integer, nullable=False)
    task_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("generation_tasks.id", ondelete="CASCADE"),
        nullable=False,
    )
    message_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("messages.id", ondelete="CASCADE")
    )
    graph_mode: Mapped[str] = mapped_column(String(16), nullable=False)
    span_id: Mapped[str | None] = mapped_column(String(64))
    parent_span_id: Mapped[str | None] = mapped_column(String(64))
    event_type: Mapped[str] = mapped_column(String(96), nullable=False)
    stage: Mapped[str | None] = mapped_column(String(96))
    actor_type: Mapped[str] = mapped_column(String(32), nullable=False, default="system")
    actor_id: Mapped[str | None] = mapped_column(String(128))
    status: Mapped[str | None] = mapped_column(String(32))
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    input_payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    output_payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)
    duration_ms: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
