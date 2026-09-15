from __future__ import annotations

import asyncio
import json
import re
from typing import Any
from uuid import UUID

from app.attachments.service import AttachmentService
from app.content.contracts import (
    ContentEnvelope,
    ContentItem,
    ContentKind,
    ContentUnderstandingResult,
)
from app.content.parser_registry import ContentParserRegistry
from app.domain.exceptions import AppError
from app.models.content_manifest import (
    AttachmentContentArtifact,
    AttachmentContentChunk,
    AttachmentContentManifest,
)
from app.repositories.content_repository import ContentRepository

_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_#.-]+|[\u4e00-\u9fff]{2,}")


class ContentUnderstandingService:
    def __init__(
        self,
        *,
        session_factory,
        attachment_service: AttachmentService,
        parser_registry: ContentParserRegistry,
        repository: ContentRepository,
        quick_max_characters: int = 30000,
        standard_max_characters: int = 200000,
        context_max_characters_per_attachment: int = 12000,
        context_max_total_characters: int = 36000,
        chunk_size: int = 1500,
        chunk_overlap: int = 200,
    ) -> None:
        self.session_factory = session_factory
        self.attachment_service = attachment_service
        self.parser_registry = parser_registry
        self.repository = repository
        self.quick_max_characters = quick_max_characters
        self.standard_max_characters = standard_max_characters
        self.context_max_characters_per_attachment = context_max_characters_per_attachment
        self.context_max_total_characters = context_max_total_characters
        self.chunk_size = max(300, chunk_size)
        self.chunk_overlap = max(0, min(chunk_overlap, self.chunk_size // 2))

    def build_envelope(
        self, *, query: str, attachments: list[dict[str, Any]]
    ) -> ContentEnvelope:
        items: list[ContentItem] = []
        if query.strip():
            items.append(
                ContentItem(content_id="query", kind=ContentKind.TEXT, text=query.strip())
            )
        total = 0
        has_images = False
        for raw in attachments:
            attachment_id = UUID(str(raw.get("attachment_id")))
            kind_value = str(raw.get("kind") or "unknown")
            filename = str(raw.get("filename") or "")
            lower_name = filename.lower()
            if lower_name.endswith((".ppt", ".pptx")):
                kind = ContentKind.PRESENTATION
            elif lower_name.endswith(
                (".py", ".js", ".ts", ".java", ".c", ".cpp", ".h", ".go", ".rs", ".sql", ".sh")
            ):
                kind = ContentKind.CODE
            else:
                try:
                    kind = ContentKind(kind_value)
                except ValueError:
                    kind = ContentKind.UNKNOWN
            size = int(raw.get("size_bytes") or 0)
            total += size
            has_images = has_images or kind == ContentKind.IMAGE
            items.append(
                ContentItem(
                    content_id=str(attachment_id),
                    kind=kind,
                    attachment_id=attachment_id,
                    filename=filename or "attachment",
                    mime_type=str(raw.get("mime_type") or "application/octet-stream"),
                    size_bytes=size,
                    sha256=str(raw.get("sha256") or ""),
                    metadata=dict(raw.get("metadata") or {}),
                )
            )
        return ContentEnvelope(
            query=query,
            items=items,
            has_text=bool(query.strip()),
            has_images=has_images,
            has_files=bool(attachments),
            total_attachment_bytes=total,
        )

    @staticmethod
    def _keywords(text: str) -> list[str]:
        values: list[str] = []
        seen: set[str] = set()
        for match in _TOKEN_PATTERN.findall(text.lower()):
            token = match.strip("._-#")
            if len(token) < 2 or token in seen:
                continue
            seen.add(token)
            values.append(token)
            if len(values) >= 80:
                break
        return values

    def _chunks(self, text: str) -> list[str]:
        if not text:
            return []
        values: list[str] = []
        step = self.chunk_size - self.chunk_overlap
        for start in range(0, len(text), step):
            value = text[start : start + self.chunk_size].strip()
            if value:
                values.append(value)
            if start + self.chunk_size >= len(text):
                break
        return values

    @staticmethod
    def _artifact_rows(result: ContentUnderstandingResult) -> list[tuple[str, dict[str, Any]]]:
        rows: list[tuple[str, dict[str, Any]]] = []
        for key, values in (
            ("page", result.pages),
            ("table", result.tables),
            ("sheet", result.sheets),
            ("slide", result.slides),
            ("image", result.images),
            ("code", result.code_files),
        ):
            for item in values:
                rows.append((key, dict(item)))
        return rows

    async def _load_cached(
        self, attachment_id: UUID, user_token: str
    ) -> ContentUnderstandingResult | None:
        async with self.session_factory() as session:
            row = await self.repository.get_manifest(session, attachment_id, user_token)
            if row is None:
                return None
            return ContentUnderstandingResult.model_validate(dict(row.result_json or {}))

    async def _persist(
        self, result: ContentUnderstandingResult, user_token: str
    ) -> None:
        if result.attachment_id is None:
            return
        payload = result.model_dump(mode="json")
        text = result.extracted_text or ""
        manifest = AttachmentContentManifest(
            attachment_id=result.attachment_id,
            user_token=user_token,
            kind=result.kind.value,
            parser_name=result.parser_name,
            parser_version=result.parser_version,
            extraction_status=result.extraction_status.value,
            summary=result.summary,
            keywords=self._keywords("\n".join([result.summary, text[:20000]])),
            character_count=len(text),
            page_count=len(result.pages) or None,
            sheet_count=len(result.sheets) or None,
            slide_count=len(result.slides) or None,
            warnings=list(result.warnings),
            result_json=payload,
        )
        artifacts: list[AttachmentContentArtifact] = []
        for sequence_no, (artifact_type, item) in enumerate(
            self._artifact_rows(result), start=1
        ):
            content_text = str(
                item.get("text")
                or item.get("preview")
                or item.get("summary")
                or ""
            )
            locator = {
                key: item[key]
                for key in (
                    "page_number",
                    "page",
                    "sheet_name",
                    "cell_range",
                    "slide_number",
                    "line_start",
                    "line_end",
                    "filename",
                )
                if item.get(key) is not None
            }
            artifacts.append(
                AttachmentContentArtifact(
                    manifest_id=manifest.id,
                    attachment_id=result.attachment_id,
                    user_token=user_token,
                    artifact_type=artifact_type,
                    sequence_no=sequence_no,
                    locator=locator,
                    content_text=content_text[:20000],
                    metadata_json=item,
                    storage_key=None,
                )
            )
        chunks = [
            AttachmentContentChunk(
                manifest_id=manifest.id,
                attachment_id=result.attachment_id,
                user_token=user_token,
                sequence_no=index,
                locator={"character_start": (index - 1) * (self.chunk_size - self.chunk_overlap)},
                content_text=value,
                character_count=len(value),
                token_count=max(1, len(value) // 3),
                metadata_json={"parser": result.parser_name},
            )
            for index, value in enumerate(self._chunks(text), start=1)
        ]
        async with self.session_factory() as session, session.begin():
            await self.repository.replace(
                session, manifest=manifest, artifacts=artifacts, chunks=chunks
            )

    async def _retrieve_preview(
        self,
        *,
        result: ContentUnderstandingResult,
        query: str,
        user_token: str,
        max_characters: int,
    ) -> ContentUnderstandingResult:
        if result.attachment_id and result.kind in {ContentKind.TEXT, ContentKind.CODE}:
            descriptor, data = await self.attachment_service.read_owned(attachment_id=result.attachment_id, user_token=user_token)
            from app.content.raw_text import search_text
            found = await asyncio.to_thread(search_text, data, self._keywords(query), max_characters,
                self.chunk_size, self.chunk_overlap)
            result.extracted_text = "\n\n--- 检索片段 ---\n\n".join(x["content"] for x in found["matches"])
            result.warnings = [w for w in result.warnings if "仅保留前部预览" not in w] + found["warnings"]
            result.metadata.update(source_scanned_complete=True, source_characters=found["source_characters"],
                source_line_count=found["source_line_count"], selected_locators=[x["locator"] for x in found["matches"]])
            return result
        if result.attachment_id is None or not result.extracted_text:
            return result
        async with self.session_factory() as session:
            chunks = await self.repository.list_chunks(
                session, result.attachment_id, user_token
            )
        if not chunks:
            result.extracted_text = result.extracted_text[:max_characters]
            return result
        tokens = self._keywords(query)
        ranked: list[tuple[int, int, str]] = []
        for row in chunks:
            lower = row.content_text.lower()
            score = sum(3 if token in lower else 0 for token in tokens)
            ranked.append((score, -row.sequence_no, row.content_text))
        ranked.sort(reverse=True)
        selected: list[str] = []
        used = 0
        for _, _, value in ranked:
            if used >= max_characters:
                break
            remaining = max_characters - used
            selected.append(value[:remaining])
            used += min(len(value), remaining)
        result.extracted_text = "\n\n--- 检索片段 ---\n\n".join(selected)
        if len(chunks) > len(selected):
            result.warnings.append(
                f"附件已结构化并分为 {len(chunks)} 个片段；本轮仅注入与问题最相关的内容。"
            )
        return result

    async def search_attachment(
        self,
        *,
        attachment_id: UUID,
        user_token: str,
        query: str,
        max_characters: int = 10000,
    ) -> dict[str, Any]:
        descriptor, data = await self.attachment_service.read_owned(attachment_id=attachment_id, user_token=user_token)
        if descriptor.kind.value == "text":
            from app.content.raw_text import search_text
            found = await asyncio.to_thread(search_text, data, self._keywords(query), max_characters,
                self.chunk_size, self.chunk_overlap)
            return {**found, "attachment_id":str(attachment_id), "filename":descriptor.filename,
                "query":query, "parser_name":"raw_text.v1"}
        async with self.session_factory() as session:
            manifest = await self.repository.get_manifest(session, attachment_id, user_token)
            if manifest is None:
                raise AppError("ATTACHMENT_CONTENT_NOT_READY", "附件尚未完成本地结构化解析", 409)
            chunks = await self.repository.list_chunks(session, attachment_id, user_token)
        tokens = self._keywords(query)
        ranked: list[tuple[int, int, AttachmentContentChunk]] = []
        for row in chunks:
            lower = row.content_text.lower()
            score = sum(3 if token in lower else 0 for token in tokens)
            ranked.append((score, -row.sequence_no, row))
        ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
        matches: list[dict[str, Any]] = []
        used = 0
        for score, _, row in ranked:
            if used >= max_characters:
                break
            remaining = max_characters - used
            content = row.content_text[:remaining]
            if not content:
                continue
            matches.append(
                {
                    "chunk_id": str(row.id),
                    "sequence_no": row.sequence_no,
                    "score": score,
                    "locator": dict(row.locator or {}),
                    "content": content,
                }
            )
            used += len(content)
        return {
            "attachment_id": str(attachment_id),
            "summary": manifest.summary,
            "parser_name": manifest.parser_name,
            "parser_version": manifest.parser_version,
            "query": query,
            "match_count": len(matches),
            "matches": matches,
            "warnings": list(manifest.warnings or []),
        }

    async def understand(
        self,
        *,
        envelope: ContentEnvelope,
        user_token: str,
        execution_mode: str = "normal",
    ) -> list[ContentUnderstandingResult]:
        results: list[ContentUnderstandingResult] = []
        remaining = self.context_max_total_characters
        # Persist a mode-independent structured representation. Quick mode limits only
        # the context preview; otherwise a file first opened in QuickGraph would remain
        # permanently truncated when the same attachment is later analyzed in Normal/Expert.
        parse_limit = self.standard_max_characters
        for item in envelope.items:
            if item.attachment_id is None:
                continue
            cached = await self._load_cached(item.attachment_id, user_token)
            if cached is None:
                try:
                    descriptor, data = await self.attachment_service.read_owned(attachment_id=item.attachment_id,user_token=user_token)
                except Exception as exc:
                    from app.content.contracts import ExtractionStatus
                    results.append(ContentUnderstandingResult(content_id=str(item.attachment_id),attachment_id=item.attachment_id,
                        filename=item.filename or "",kind=item.kind,extraction_status=ExtractionStatus.FAILED,
                        warnings=["该附件当前无法读取，未影响其他已取得的资料。"],read_error_type=type(exc).__name__))
                    continue
                try:
                    cached = await self.parser_registry.parse(descriptor=descriptor,data=data,max_characters=parse_limit)
                except Exception as exc:
                    from app.content.contracts import ExtractionStatus
                    cached = ContentUnderstandingResult(content_id=str(item.attachment_id),attachment_id=item.attachment_id,
                        filename=descriptor.filename,kind=item.kind,extraction_status=ExtractionStatus.FAILED,
                        warnings=["该附件解析未完成；其他附件和平台查询可以继续。"],parser_error_type=type(exc).__name__)

                cached.filename = descriptor.filename
                await self._persist(cached, user_token)
            if not cached.filename:
                cached.filename = item.filename or ""
            mode_limit = (
                self.quick_max_characters
                if execution_mode == "quick"
                else self.standard_max_characters
            )
            preview_limit = min(
                self.context_max_characters_per_attachment,
                mode_limit,
                max(0, remaining),
            )
            selected = cached.model_copy(deep=True)
            try:
                selected = await self._retrieve_preview(result=selected,query=envelope.query,user_token=user_token,max_characters=preview_limit)
            except Exception:
                selected.extracted_text=(selected.extracted_text or "")[:preview_limit]
                selected.warnings.append("本轮附件片段检索未完成，仅使用已保存的有限内容。")
            remaining = max(0, remaining - len(selected.extracted_text))
            if execution_mode == "quick" and len(cached.extracted_text) > preview_limit:
                selected.warnings.append("快速模式仅使用有限文件预览，未执行多轮深度读取。")
            results.append(selected)
        return results
