from __future__ import annotations

from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ContentKind(StrEnum):
    TEXT = "text"
    IMAGE = "image"
    PDF = "pdf"
    DOCUMENT = "document"
    SPREADSHEET = "spreadsheet"
    PRESENTATION = "presentation"
    CODE = "code"
    ARCHIVE = "archive"
    AUDIO = "audio"
    VIDEO = "video"
    UNKNOWN = "unknown"


class ExtractionStatus(StrEnum):
    NOT_REQUIRED = "NOT_REQUIRED"
    COMPLETED = "COMPLETED"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    UNSUPPORTED = "UNSUPPORTED"


class ContentItem(BaseModel):
    model_config = ConfigDict(extra="allow")

    content_id: str
    kind: ContentKind
    text: str | None = None
    attachment_id: UUID | None = None
    filename: str | None = None
    mime_type: str | None = None
    size_bytes: int | None = None
    sha256: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ContentEnvelope(BaseModel):
    model_config = ConfigDict(extra="allow")

    query: str
    items: list[ContentItem]
    has_text: bool
    has_images: bool
    has_files: bool
    total_attachment_bytes: int = 0
    warnings: list[str] = Field(default_factory=list)


class ContentUnderstandingResult(BaseModel):
    model_config = ConfigDict(extra="allow")

    content_id: str
    attachment_id: UUID | None = None
    filename: str = ""
    kind: ContentKind
    extraction_status: ExtractionStatus
    summary: str = ""
    extracted_text: str = ""
    pages: list[dict[str, Any]] = Field(default_factory=list)
    tables: list[dict[str, Any]] = Field(default_factory=list)
    sheets: list[dict[str, Any]] = Field(default_factory=list)
    slides: list[dict[str, Any]] = Field(default_factory=list)
    images: list[dict[str, Any]] = Field(default_factory=list)
    code_files: list[dict[str, Any]] = Field(default_factory=list)
    capabilities_required: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    parser_name: str = "metadata"
    parser_version: str = "1.0"
