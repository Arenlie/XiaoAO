from __future__ import annotations

from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class AttachmentKind(StrEnum):
    IMAGE = "image"
    PDF = "pdf"
    DOCUMENT = "document"
    SPREADSHEET = "spreadsheet"
    TEXT = "text"
    OTHER = "other"


class AttachmentStatus(StrEnum):
    UPLOADED = "UPLOADED"
    BOUND = "BOUND"
    DELETED = "DELETED"
    FAILED = "FAILED"


class AttachmentDescriptor(BaseModel):
    """Small, checkpoint-safe attachment reference.

    Binary content, signed URLs and credentials never enter LangGraph state.
    """

    model_config = ConfigDict(extra="allow")

    attachment_id: UUID
    filename: str
    mime_type: str
    kind: AttachmentKind
    size_bytes: int
    sha256: str
    status: AttachmentStatus
    metadata: dict[str, Any] = Field(default_factory=dict)

