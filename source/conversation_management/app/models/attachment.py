from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class Attachment(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "attachments"
    __table_args__ = (
        Index("idx_attachments_owner_created", "user_token", "created_at"),
        Index("idx_attachments_message", "message_id"),
        Index("idx_attachments_status_expiry", "status", "expires_at"),
    )

    user_token: Mapped[str] = mapped_column(String(128), nullable=False)
    conversation_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), nullable=True
    )
    message_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("messages.id", ondelete="CASCADE"), nullable=True
    )
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(127), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    storage_backend: Mapped[str] = mapped_column(String(16), nullable=False)
    bucket: Mapped[str | None] = mapped_column(String(128), nullable=True)
    object_key: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    metadata_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
