from __future__ import annotations

import hashlib
import mimetypes
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

from fastapi import UploadFile

from app.attachments.contracts import AttachmentDescriptor, AttachmentKind, AttachmentStatus
from app.attachments.storage import AttachmentStorage
from app.config import Settings
from app.domain.exceptions import AppError, NotFoundError
from app.models.attachment import Attachment
from app.repositories.attachment_repository import AttachmentRepository
from app.schemas.attachment import AttachmentView

_IMAGE_MIMES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
_PDF_MIMES = {"application/pdf"}
_DOCUMENT_EXTENSIONS = {".doc", ".docx", ".odt", ".rtf", ".ppt", ".pptx"}
_SPREADSHEET_EXTENSIONS = {".csv", ".tsv", ".xls", ".xlsx"}
_TEXT_EXTENSIONS = {
    ".txt", ".md", ".markdown", ".json", ".yaml", ".yml", ".xml", ".html", ".htm",
    ".py", ".js", ".ts", ".java", ".c", ".cpp", ".h", ".hpp", ".go", ".rs",
    ".sql", ".sh", ".ps1", ".css", ".log", ".ini", ".toml",
}
_SAFE_NAME_RE = re.compile(r"[^\w.()\-\u4e00-\u9fff ]+", re.UNICODE)


class AttachmentService:
    def __init__(
        self,
        *,
        session_factory,
        settings: Settings,
        repository: AttachmentRepository,
        storage: AttachmentStorage,
    ) -> None:
        self.session_factory = session_factory
        self.settings = settings
        self.repository = repository
        self.storage = storage

    @staticmethod
    def _safe_filename(value: str | None) -> str:
        raw = Path(value or "attachment").name.strip() or "attachment"
        cleaned = _SAFE_NAME_RE.sub("_", raw).strip(" .")
        return (cleaned or "attachment")[:255]

    @staticmethod
    def _kind(filename: str, mime_type: str) -> AttachmentKind:
        extension = Path(filename).suffix.lower()
        if mime_type in _IMAGE_MIMES or extension in _IMAGE_EXTENSIONS:
            return AttachmentKind.IMAGE
        if mime_type in _PDF_MIMES or extension == ".pdf":
            return AttachmentKind.PDF
        if extension in _SPREADSHEET_EXTENSIONS:
            return AttachmentKind.SPREADSHEET
        if extension in _DOCUMENT_EXTENSIONS:
            return AttachmentKind.DOCUMENT
        if mime_type.startswith("text/") or extension in _TEXT_EXTENSIONS:
            return AttachmentKind.TEXT
        return AttachmentKind.OTHER

    @staticmethod
    def _validate_signature(
        *, filename: str, mime_type: str, kind: AttachmentKind, data: bytes
    ) -> None:
        extension = Path(filename).suffix.lower()
        head = data[:4096]
        valid = True
        if kind == AttachmentKind.IMAGE:
            valid = (
                (extension in {".jpg", ".jpeg"} and head.startswith(b"\xff\xd8\xff"))
                or (extension == ".png" and head.startswith(b"\x89PNG\r\n\x1a\n"))
                or (extension == ".gif" and head[:6] in {b"GIF87a", b"GIF89a"})
                or (extension == ".webp" and head.startswith(b"RIFF") and head[8:12] == b"WEBP")
                or (mime_type == "image/jpeg" and head.startswith(b"\xff\xd8\xff"))
                or (mime_type == "image/png" and head.startswith(b"\x89PNG\r\n\x1a\n"))
                or (mime_type == "image/gif" and head[:6] in {b"GIF87a", b"GIF89a"})
                or (mime_type == "image/webp" and head.startswith(b"RIFF") and head[8:12] == b"WEBP")
            )
        elif kind == AttachmentKind.PDF:
            valid = head.lstrip().startswith(b"%PDF-")
        elif extension in {".docx", ".xlsx", ".pptx", ".odt"}:
            valid = head.startswith(b"PK\x03\x04")
        elif extension in {".doc", ".xls", ".ppt"}:
            valid = head.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1")
        elif extension == ".rtf":
            valid = head.lstrip().startswith(b"{\\rtf")
        elif kind == AttachmentKind.TEXT or extension in {".csv", ".tsv"}:
            valid = b"\x00" not in head
        if not valid:
            raise AppError(
                "ATTACHMENT_CONTENT_MISMATCH",
                f"附件内容与文件类型不匹配: {filename}",
                415,
            )

    def _validate(self, *, filename: str, mime_type: str, size_bytes: int) -> AttachmentKind:
        if size_bytes <= 0:
            raise AppError("ATTACHMENT_EMPTY", "附件内容为空", 422)
        if size_bytes > self.settings.attachment_max_file_bytes:
            raise AppError(
                "ATTACHMENT_TOO_LARGE",
                f"单个附件不能超过 {self.settings.attachment_max_file_bytes // 1024 // 1024} MB",
                413,
            )
        kind = self._kind(filename, mime_type)
        if kind == AttachmentKind.OTHER and not self.settings.attachment_allow_other_types:
            raise AppError("ATTACHMENT_TYPE_NOT_SUPPORTED", f"暂不支持该附件类型: {mime_type}", 415)
        return kind

    @staticmethod
    def descriptor(row: Attachment) -> AttachmentDescriptor:
        return AttachmentDescriptor(
            attachment_id=row.id,
            filename=row.filename,
            mime_type=row.mime_type,
            kind=AttachmentKind(row.kind),
            size_bytes=row.size_bytes,
            sha256=row.sha256,
            status=AttachmentStatus(row.status),
            metadata=dict(row.metadata_json or {}),
        )

    def view(self, row: Attachment) -> AttachmentView:
        return AttachmentView(
            id=row.id,
            filename=row.filename,
            mime_type=row.mime_type,
            kind=AttachmentKind(row.kind),
            size_bytes=row.size_bytes,
            sha256=row.sha256,
            status=AttachmentStatus(row.status),
            created_at=row.created_at,
            content_url=f"{self.settings.api_prefix}/files/{row.id}/content",
        )

    async def upload(self, *, file: UploadFile, user_token: str) -> AttachmentView:
        filename = self._safe_filename(file.filename)
        data = await file.read(self.settings.attachment_max_file_bytes + 1)
        mime_type = (file.content_type or mimetypes.guess_type(filename)[0] or "application/octet-stream").lower()
        kind = self._validate(filename=filename, mime_type=mime_type, size_bytes=len(data))
        self._validate_signature(
            filename=filename, mime_type=mime_type, kind=kind, data=data
        )
        async with self.session_factory() as usage_session:
            unbound_count, unbound_bytes, total_bytes = await self.repository.usage_for_owner(
                usage_session, user_token=user_token, now=datetime.now(UTC)
            )
        if unbound_count >= self.settings.attachment_max_unbound_count_per_user:
            raise AppError(
                "ATTACHMENT_UNBOUND_COUNT_EXCEEDED",
                "未发送附件数量已达到上限，请发送、删除附件或等待自动清理",
                429,
            )
        if unbound_bytes + len(data) > self.settings.attachment_max_unbound_bytes_per_user:
            raise AppError(
                "ATTACHMENT_UNBOUND_QUOTA_EXCEEDED",
                "未发送附件占用空间已达到上限",
                413,
            )
        if total_bytes + len(data) > self.settings.attachment_max_storage_bytes_per_user:
            raise AppError(
                "ATTACHMENT_STORAGE_QUOTA_EXCEEDED",
                "用户附件存储空间已达到上限",
                413,
            )
        attachment_id = uuid4()
        digest = hashlib.sha256(data).hexdigest()
        object_key = f"{datetime.now(UTC):%Y/%m/%d}/{hashlib.sha256(user_token.encode()).hexdigest()[:16]}/{attachment_id.hex}/{filename}"
        bucket, stored_key = await self.storage.put(object_key=object_key, data=data, mime_type=mime_type)
        row = Attachment(
            id=attachment_id,
            user_token=user_token,
            filename=filename,
            mime_type=mime_type,
            kind=kind.value,
            size_bytes=len(data),
            sha256=digest,
            storage_backend=self.storage.backend_name,
            bucket=bucket,
            object_key=stored_key,
            status=AttachmentStatus.UPLOADED.value,
            metadata_json={},
            expires_at=datetime.now(UTC) + timedelta(seconds=self.settings.attachment_unbound_ttl_seconds),
        )
        try:
            async with self.session_factory() as session, session.begin():
                session.add(row)
                await session.flush()
        except Exception:
            await self.storage.delete(bucket=bucket, object_key=stored_key)
            raise
        return self.view(row)

    async def get_owned(self, *, attachment_id: UUID, user_token: str) -> Attachment:
        async with self.session_factory() as session:
            row = await self.repository.get_owned(session, attachment_id, user_token)
            if row is None or row.status == AttachmentStatus.DELETED.value:
                raise NotFoundError("ATTACHMENT_NOT_FOUND", "附件不存在")
            return row

    async def read_owned(self, *, attachment_id: UUID, user_token: str) -> tuple[AttachmentDescriptor, bytes]:
        row = await self.get_owned(attachment_id=attachment_id, user_token=user_token)
        data = await self.storage.get(bucket=row.bucket, object_key=row.object_key)
        if hashlib.sha256(data).hexdigest() != row.sha256:
            raise AppError("ATTACHMENT_HASH_MISMATCH", "附件完整性校验失败", 500)
        return self.descriptor(row), data

    async def validate_for_message(
        self, session, *, attachment_ids: list[UUID], user_token: str
    ) -> list[Attachment]:
        if len(attachment_ids) > self.settings.attachment_max_count_per_message:
            raise AppError(
                "ATTACHMENT_COUNT_EXCEEDED",
                f"每条消息最多上传 {self.settings.attachment_max_count_per_message} 个附件",
                422,
            )
        rows = await self.repository.list_owned(session, attachment_ids, user_token, for_update=True)
        if len(rows) != len(list(dict.fromkeys(attachment_ids))):
            raise NotFoundError("ATTACHMENT_NOT_FOUND", "部分附件不存在或不属于当前用户")
        total = sum(row.size_bytes for row in rows)
        if total > self.settings.attachment_max_total_bytes_per_message:
            raise AppError("ATTACHMENT_TOTAL_TOO_LARGE", "单条消息附件总大小超过限制", 413)
        now = datetime.now(UTC)
        for row in rows:
            if row.status not in {AttachmentStatus.UPLOADED.value, AttachmentStatus.BOUND.value}:
                raise AppError("ATTACHMENT_STATUS_INVALID", f"附件不可使用: {row.filename}", 409)
            if row.expires_at and row.expires_at <= now and row.message_id is None:
                raise AppError("ATTACHMENT_EXPIRED", f"附件已过期: {row.filename}", 410)
        return rows

    async def bind_rows(self, rows: list[Attachment], *, conversation_id: UUID, message_id: UUID) -> None:
        for row in rows:
            if row.message_id and row.message_id != message_id:
                raise AppError("ATTACHMENT_ALREADY_BOUND", f"附件已被其他消息使用: {row.filename}", 409)
            row.conversation_id = conversation_id
            row.message_id = message_id
            row.status = AttachmentStatus.BOUND.value
            row.expires_at = None

    async def delete_owned(self, *, attachment_id: UUID, user_token: str) -> None:
        async with self.session_factory() as session, session.begin():
            row = await self.repository.get_owned(session, attachment_id, user_token, for_update=True)
            if row is None:
                raise NotFoundError("ATTACHMENT_NOT_FOUND", "附件不存在")
            if row.message_id is not None:
                raise AppError("ATTACHMENT_BOUND", "已发送消息中的附件不能单独删除", 409)
            row.status = AttachmentStatus.DELETED.value
            row.deleted_at = datetime.now(UTC)
            bucket, key = row.bucket, row.object_key
        await self.storage.delete(bucket=bucket, object_key=key)

    async def delete_storage_rows(self, rows: list[Attachment]) -> int:
        deleted = 0
        for row in rows:
            try:
                await self.storage.delete(bucket=row.bucket, object_key=row.object_key)
                deleted += 1
            except Exception:
                # Database cleanup must not be rolled back because an object store is
                # temporarily unavailable. The object key remains observable in logs.
                continue
        return deleted

