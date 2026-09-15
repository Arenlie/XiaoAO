from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import Boolean, DateTime, Index, String, Text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.enums import ConversationStatus, TitleSource
from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class Conversation(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "conversations"
    __table_args__ = (
        Index(
            "idx_conversations_user_list",
            "user_token",
            "app_code",
            "status",
            "is_pinned",
            "pinned_at",
            "last_message_at",
        ),
    )

    user_token: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    app_code: Mapped[str] = mapped_column(String(64), nullable=False, default="xiaoao")
    title: Mapped[str] = mapped_column(String(100), nullable=False, default="新对话")
    title_source: Mapped[str] = mapped_column(
        String(16), nullable=False, default=TitleSource.DEFAULT.value
    )
    is_pinned: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    pinned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    active_branch_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    first_user_message_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=ConversationStatus.ACTIVE.value
    )
    last_message_preview: Mapped[str | None] = mapped_column(Text)
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
