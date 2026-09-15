from __future__ import annotations

import asyncio
import io

from app.attachments.contracts import AttachmentDescriptor
from app.content.contracts import ContentKind, ContentUnderstandingResult, ExtractionStatus


class ImageContentParser:
    name = "image.v1"
    supported_kinds = {"image"}

    async def parse(self, *, descriptor: AttachmentDescriptor, data: bytes, max_characters: int) -> ContentUnderstandingResult:
        del max_characters
        def _inspect():
            try:
                from PIL import Image
                with Image.open(io.BytesIO(data)) as image:
                    return image.width, image.height, image.mode, image.format
            except Exception:
                return None, None, None, None
        width, height, mode, image_format = await asyncio.to_thread(_inspect)
        metadata = {"width": width, "height": height, "mode": mode, "format": image_format}
        return ContentUnderstandingResult(
            content_id=str(descriptor.attachment_id),
            attachment_id=descriptor.attachment_id,
            kind=ContentKind.IMAGE,
            extraction_status=ExtractionStatus.NOT_REQUIRED,
            summary=(
                f"图片 {descriptor.filename}"
                + (f"，尺寸 {width}×{height}" if width and height else "")
                + "。"
            ),
            images=[metadata],
            capabilities_required=["image_understanding"],
            parser_name=self.name,
        )
