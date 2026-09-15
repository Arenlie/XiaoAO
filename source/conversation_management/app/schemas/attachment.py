from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field

from app.attachments.contracts import AttachmentKind, AttachmentStatus


class AttachmentView(BaseModel):
    id: UUID
    filename: str
    mime_type: str
    kind: AttachmentKind
    size_bytes: int
    sha256: str
    status: AttachmentStatus
    created_at: datetime
    content_url: str


class AttachmentListResponse(BaseModel):
    items: list[AttachmentView] = Field(default_factory=list)
