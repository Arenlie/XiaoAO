from __future__ import annotations

from typing import Protocol

from app.attachments.contracts import AttachmentDescriptor
from app.content.contracts import ContentUnderstandingResult


class ContentParser(Protocol):
    name: str
    supported_kinds: set[str]

    async def parse(
        self,
        *,
        descriptor: AttachmentDescriptor,
        data: bytes,
        max_characters: int,
    ) -> ContentUnderstandingResult: ...
