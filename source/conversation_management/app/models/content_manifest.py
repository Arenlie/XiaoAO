from __future__ import annotations

from uuid import UUID

from sqlalchemy import ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class AttachmentContentManifest(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "attachment_content_manifests"
    __table_args__ = (
        UniqueConstraint("attachment_id", name="uq_attachment_content_manifest_attachment"),
        Index("idx_content_manifests_owner", "user_token", "created_at"),
    )

    attachment_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("attachments.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_token: Mapped[str] = mapped_column(String(128), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    parser_name: Mapped[str] = mapped_column(String(96), nullable=False)
    parser_version: Mapped[str] = mapped_column(String(32), nullable=False, default="1.0")
    extraction_status: Mapped[str] = mapped_column(String(32), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    keywords: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    character_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    page_count: Mapped[int | None] = mapped_column(Integer)
    sheet_count: Mapped[int | None] = mapped_column(Integer)
    slide_count: Mapped[int | None] = mapped_column(Integer)
    warnings: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    result_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)


class AttachmentContentArtifact(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "attachment_content_artifacts"
    __table_args__ = (
        Index("idx_content_artifacts_manifest", "manifest_id", "artifact_type"),
        Index("idx_content_artifacts_owner", "user_token", "created_at"),
    )

    manifest_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("attachment_content_manifests.id", ondelete="CASCADE"),
        nullable=False,
    )
    attachment_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("attachments.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_token: Mapped[str] = mapped_column(String(128), nullable=False)
    artifact_type: Mapped[str] = mapped_column(String(32), nullable=False)
    sequence_no: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    locator: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    content_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    metadata_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    storage_key: Mapped[str | None] = mapped_column(String(512))


class AttachmentContentChunk(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "attachment_content_chunks"
    __table_args__ = (
        UniqueConstraint(
            "attachment_id", "sequence_no", name="uq_attachment_content_chunk_sequence"
        ),
        Index("idx_content_chunks_attachment", "attachment_id", "sequence_no"),
        Index("idx_content_chunks_owner", "user_token", "created_at"),
    )

    manifest_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("attachment_content_manifests.id", ondelete="CASCADE"),
        nullable=False,
    )
    attachment_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("attachments.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_token: Mapped[str] = mapped_column(String(128), nullable=False)
    sequence_no: Mapped[int] = mapped_column(Integer, nullable=False)
    locator: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    content_text: Mapped[str] = mapped_column(Text, nullable=False)
    character_count: Mapped[int] = mapped_column(Integer, nullable=False)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    metadata_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
