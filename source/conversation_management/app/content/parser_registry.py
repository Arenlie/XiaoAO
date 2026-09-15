from __future__ import annotations

from pathlib import Path

from app.attachments.contracts import AttachmentDescriptor, AttachmentKind
from app.content.contracts import ContentKind, ContentUnderstandingResult, ExtractionStatus
from app.content.parsers.image import ImageContentParser
from app.content.parsers.office import (
    DocumentContentParser,
    PdfContentParser,
    PresentationContentParser,
    SpreadsheetContentParser,
)
from app.content.parsers.text import TextContentParser

_PRESENTATION_EXTENSIONS = {".ppt", ".pptx"}
_CODE_EXTENSIONS = {".py", ".js", ".ts", ".java", ".c", ".cpp", ".h", ".go", ".rs", ".sql", ".sh"}


class ContentParserRegistry:
    def __init__(self) -> None:
        self.image = ImageContentParser()
        self.pdf = PdfContentParser()
        self.document = DocumentContentParser()
        self.spreadsheet = SpreadsheetContentParser()
        self.presentation = PresentationContentParser()
        self.text = TextContentParser()

    @staticmethod
    def normalized_kind(descriptor: AttachmentDescriptor) -> ContentKind:
        extension = Path(descriptor.filename).suffix.lower()
        if extension in _PRESENTATION_EXTENSIONS:
            return ContentKind.PRESENTATION
        if extension in _CODE_EXTENSIONS:
            return ContentKind.CODE
        mapping = {
            AttachmentKind.IMAGE: ContentKind.IMAGE,
            AttachmentKind.PDF: ContentKind.PDF,
            AttachmentKind.DOCUMENT: ContentKind.DOCUMENT,
            AttachmentKind.SPREADSHEET: ContentKind.SPREADSHEET,
            AttachmentKind.TEXT: ContentKind.TEXT,
        }
        return mapping.get(descriptor.kind, ContentKind.UNKNOWN)

    async def parse(self, *, descriptor: AttachmentDescriptor, data: bytes, max_characters: int) -> ContentUnderstandingResult:
        kind = self.normalized_kind(descriptor)
        if kind == ContentKind.IMAGE:
            return await self.image.parse(descriptor=descriptor, data=data, max_characters=max_characters)
        if kind == ContentKind.PDF:
            return await self.pdf.parse(descriptor=descriptor, data=data, max_characters=max_characters)
        if kind == ContentKind.PRESENTATION:
            return await self.presentation.parse(descriptor=descriptor, data=data, max_characters=max_characters)
        if kind == ContentKind.DOCUMENT:
            return await self.document.parse(descriptor=descriptor, data=data, max_characters=max_characters)
        if kind == ContentKind.SPREADSHEET:
            return await self.spreadsheet.parse(descriptor=descriptor, data=data, max_characters=max_characters)
        if kind in {ContentKind.TEXT, ContentKind.CODE}:
            return await self.text.parse(descriptor=descriptor, data=data, max_characters=max_characters)
        return ContentUnderstandingResult(
            content_id=str(descriptor.attachment_id),
            attachment_id=descriptor.attachment_id,
            kind=ContentKind.UNKNOWN,
            extraction_status=ExtractionStatus.UNSUPPORTED,
            summary=f"未识别的附件格式：{descriptor.filename}",
            capabilities_required=["specialized_file_tool"],
            warnings=["当前没有适配该格式的解析器或专业工具。"],
            parser_name="unsupported.v1",
        )
