from __future__ import annotations

from uuid import UUID

from sqlalchemy import delete, select

from app.models.attachment import Attachment
from app.models.content_manifest import (
    AttachmentContentArtifact,
    AttachmentContentChunk,
    AttachmentContentManifest,
)


class ContentRepository:
    async def get_manifest(self, session, attachment_id: UUID, user_token: str):
        return await session.scalar(
            select(AttachmentContentManifest).where(
                AttachmentContentManifest.attachment_id == attachment_id,
                AttachmentContentManifest.user_token == user_token,
            )
        )

    async def replace(
        self,
        session,
        *,
        manifest: AttachmentContentManifest,
        artifacts: list[AttachmentContentArtifact],
        chunks: list[AttachmentContentChunk],
    ) -> None:
        # Serialize parsing writeback for the same owned attachment across workers.
        await session.execute(select(Attachment.id).where(
            Attachment.id == manifest.attachment_id, Attachment.user_token == manifest.user_token
        ).with_for_update())
        existing = await self.get_manifest(
            session, manifest.attachment_id, manifest.user_token
        )
        if existing is not None:
            await session.execute(
                delete(AttachmentContentManifest).where(
                    AttachmentContentManifest.id == existing.id
                )
            )
            await session.flush()
        session.add(manifest)
        await session.flush([manifest])
        for row in artifacts:
            row.manifest_id = manifest.id
        for row in chunks:
            row.manifest_id = manifest.id
        session.add_all([*artifacts, *chunks])

    async def list_chunks(
        self, session, attachment_id: UUID, user_token: str
    ) -> list[AttachmentContentChunk]:
        rows = await session.scalars(
            select(AttachmentContentChunk)
            .where(
                AttachmentContentChunk.attachment_id == attachment_id,
                AttachmentContentChunk.user_token == user_token,
            )
            .order_by(AttachmentContentChunk.sequence_no.asc())
        )
        return list(rows.all())
