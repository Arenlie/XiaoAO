from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from app.config import Settings
from app.domain.exceptions import AppError


class AttachmentStorage(Protocol):
    backend_name: str

    async def put(self, *, object_key: str, data: bytes, mime_type: str) -> tuple[str | None, str]: ...
    async def get(self, *, bucket: str | None, object_key: str) -> bytes: ...
    async def delete(self, *, bucket: str | None, object_key: str) -> None: ...


@dataclass(slots=True)
class LocalAttachmentStorage:
    root: Path
    backend_name: str = "local"

    def __post_init__(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, object_key: str) -> Path:
        path = (self.root / object_key).resolve()
        root = self.root.resolve()
        if root not in path.parents and path != root:
            raise AppError("ATTACHMENT_PATH_INVALID", "附件存储路径无效", 500)
        return path

    async def put(self, *, object_key: str, data: bytes, mime_type: str) -> tuple[str | None, str]:
        del mime_type
        path = self._path(object_key)
        await asyncio.to_thread(path.parent.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(path.write_bytes, data)
        return None, object_key

    async def get(self, *, bucket: str | None, object_key: str) -> bytes:
        del bucket
        path = self._path(object_key)
        if not path.is_file():
            raise AppError("ATTACHMENT_CONTENT_NOT_FOUND", "附件内容不存在", 404)
        return await asyncio.to_thread(path.read_bytes)

    async def delete(self, *, bucket: str | None, object_key: str) -> None:
        del bucket
        path = self._path(object_key)
        if path.exists():
            await asyncio.to_thread(path.unlink)


class MinioAttachmentStorage:
    backend_name = "minio"

    def __init__(self, settings: Settings) -> None:
        if not settings.minio_endpoint or not settings.minio_access_key or not settings.minio_secret_key:
            raise AppError("MINIO_NOT_CONFIGURED", "MinIO 附件存储未完整配置", 500)
        try:
            from minio import Minio
        except ImportError as exc:
            raise AppError("MINIO_DEPENDENCY_MISSING", "缺少 minio Python 依赖", 500) from exc
        self.bucket = settings.minio_attachment_bucket
        self.client = Minio(
            settings.minio_endpoint,
            access_key=settings.minio_access_key,
            secret_key=settings.minio_secret_key,
            secure=settings.minio_secure,
            region=settings.minio_region,
        )

    async def ensure_bucket(self) -> None:
        exists = await asyncio.to_thread(self.client.bucket_exists, self.bucket)
        if not exists:
            await asyncio.to_thread(self.client.make_bucket, self.bucket)

    async def put(self, *, object_key: str, data: bytes, mime_type: str) -> tuple[str | None, str]:
        from io import BytesIO

        await self.ensure_bucket()
        stream = BytesIO(data)
        try:
            await asyncio.to_thread(
                self.client.put_object,
                self.bucket,
                object_key,
                stream,
                len(data),
                content_type=mime_type,
            )
        except Exception as exc:
            code = getattr(exc, "code", type(exc).__name__)
            raise AppError("ATTACHMENT_STORAGE_FAILED", f"MinIO 保存附件失败: {code}", 502) from exc
        return self.bucket, object_key

    async def get(self, *, bucket: str | None, object_key: str) -> bytes:
        target_bucket = bucket or self.bucket

        def _read() -> bytes:
            response = self.client.get_object(target_bucket, object_key)
            try:
                return response.read()
            finally:
                response.close()
                response.release_conn()

        try:
            return await asyncio.to_thread(_read)
        except Exception as exc:
            raise AppError("ATTACHMENT_CONTENT_NOT_FOUND", "附件内容不存在", 404) from exc

    async def delete(self, *, bucket: str | None, object_key: str) -> None:
        target_bucket = bucket or self.bucket
        try:
            await asyncio.to_thread(self.client.remove_object, target_bucket, object_key)
        except Exception:
            return


def create_attachment_storage(settings: Settings) -> AttachmentStorage:
    if settings.attachment_storage_backend == "minio":
        return MinioAttachmentStorage(settings)
    return LocalAttachmentStorage(Path(settings.attachment_local_dir))
