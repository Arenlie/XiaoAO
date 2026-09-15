from __future__ import annotations

from uuid import UUID

from sqlalchemy import ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.enums import BranchForkType
from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class ConversationBranch(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "conversation_branches"
    __table_args__ = (Index("idx_branches_conversation", "conversation_id", "created_at"),)

    conversation_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    parent_branch_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("conversation_branches.id", ondelete="SET NULL")
    )
    fork_type: Mapped[str] = mapped_column(
        String(16), nullable=False, default=BranchForkType.ROOT.value
    )
    forked_from_message_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    active_leaf_message_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    dify_conversation_id: Mapped[str | None] = mapped_column(String(128))
    active_entity: Mapped[dict | None] = mapped_column(JSONB)
    summary: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="ACTIVE")
