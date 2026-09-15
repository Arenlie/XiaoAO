from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, SmallInteger, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class ConversationTopic(Base):
    __tablename__ = "conversation_topics"
    __table_args__ = (
        Index("idx_conversation_topics_conversation", "conversation_id", "updated_at"),
        Index("idx_conversation_topics_searchable", "conversation_id", "status"),
    )

    topic_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    conversation_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str] = mapped_column(Text, nullable=False)
    topic_summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    primary_subject: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    scope: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    current_goal: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="ACTIVE")
    searchable_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    last_task_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("generation_tasks.id", ondelete="SET NULL")
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())


class ConversationContextSlot(Base):
    __tablename__ = "conversation_context_slots"
    __table_args__ = (
        CheckConstraint("slot_no IN (0, 1, 2)", name="slot_no_hot_window"),
        Index("idx_context_slots_topic", "topic_id"),
    )

    conversation_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), primary_key=True
    )
    slot_no: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    topic_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("conversation_topics.topic_id", ondelete="CASCADE"), nullable=False
    )
    activated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class EvidenceObject(Base):
    __tablename__ = "evidence_objects"
    __table_args__ = (
        Index("idx_evidence_conversation", "conversation_id", "created_at"),
        Index("idx_evidence_semantic_type", "semantic_type"),
        Index("idx_evidence_source_task", "source_task_id"),
        Index("idx_evidence_created_at", "created_at"),
    )

    evidence_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    # Practical tenancy boundary. Evidence is not bound to one Topic, but every
    # EvidenceObject has exactly one owning Conversation for RLS and lifecycle cleanup.
    conversation_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    semantic_type: Mapped[str] = mapped_column(String(96), nullable=False)
    authority: Mapped[str] = mapped_column(String(32), nullable=False)
    subject: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    scope: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    content_descriptor: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    summary: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    completeness: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    freshness: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    storage_backend: Mapped[str] = mapped_column(String(48), nullable=False)
    storage_ref: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    inline_payload: Mapped[Any | None] = mapped_column(JSONB)
    source_system: Mapped[str] = mapped_column(String(96), nullable=False)
    source_task_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("generation_tasks.id", ondelete="SET NULL")
    )
    source_tool: Mapped[str | None] = mapped_column(String(192))
    immutable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    supersedes_evidence_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("evidence_objects.evidence_id", ondelete="SET NULL")
    )
    checksum: Mapped[str | None] = mapped_column(String(128))
    observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class TopicEvidenceRef(Base):
    __tablename__ = "topic_evidence_refs"

    topic_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("conversation_topics.topic_id", ondelete="CASCADE"), primary_key=True
    )
    evidence_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("evidence_objects.evidence_id", ondelete="CASCADE"), primary_key=True
    )
    role: Mapped[str] = mapped_column(String(24), nullable=False, default="supporting")
    pinned: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class EvidenceLineageEdge(Base):
    __tablename__ = "evidence_lineage_edges"

    parent_evidence_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("evidence_objects.evidence_id", ondelete="CASCADE"), primary_key=True
    )
    child_evidence_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("evidence_objects.evidence_id", ondelete="CASCADE"), primary_key=True
    )
    relation_type: Mapped[str] = mapped_column(String(48), primary_key=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class TaskEvidenceRef(Base):
    __tablename__ = "task_evidence_refs"

    task_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("generation_tasks.id", ondelete="CASCADE"), primary_key=True
    )
    evidence_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("evidence_objects.evidence_id", ondelete="CASCADE"), primary_key=True
    )
    direction: Mapped[str] = mapped_column(String(8), primary_key=True)
    purpose: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
