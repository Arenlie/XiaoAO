from __future__ import annotations

from collections.abc import Iterable
from uuid import UUID

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.attachment import Attachment


class AttachmentRepository:
    async def get_owned(
        self, session: AsyncSession, attachment_id: UUID, user_token: str, *, for_update: bool = False
    ) -> Attachment | None:
        stmt = select(Attachment).where(
            Attachment.id == attachment_id,
            Attachment.user_token == user_token,
        )
        if for_update:
            stmt = stmt.with_for_update()
        return await session.scalar(stmt)

    async def list_owned(
        self, session: AsyncSession, attachment_ids: Iterable[UUID], user_token: str, *, for_update: bool = False
    ) -> list[Attachment]:
        ids = list(dict.fromkeys(attachment_ids))
        if not ids:
            return []
        stmt = select(Attachment).where(
            Attachment.id.in_(ids),
            Attachment.user_token == user_token,
        )
        if for_update:
            stmt = stmt.with_for_update()
        rows = list((await session.scalars(stmt)).all())
        by_id = {row.id: row for row in rows}
        return [by_id[value] for value in ids if value in by_id]

    async def list_for_message(
        self, session: AsyncSession, message_id: UUID, user_token: str
    ) -> list[Attachment]:
        stmt = (
            select(Attachment)
            .where(
                Attachment.message_id == message_id,
                Attachment.user_token == user_token,
            )
            .order_by(Attachment.created_at)
        )
        return list((await session.scalars(stmt)).all())
    async def list_expired_unbound(
        self, session: AsyncSession, *, now: datetime, limit: int = 500
    ) -> list[Attachment]:
        stmt = (
            select(Attachment)
            .where(
                Attachment.message_id.is_(None),
                Attachment.expires_at.is_not(None),
                Attachment.expires_at < now,
                Attachment.status == "UPLOADED",
            )
            .order_by(Attachment.expires_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        return list((await session.scalars(stmt)).all())

    async def list_for_conversations(
        self, session: AsyncSession, conversation_ids: Iterable[UUID]
    ) -> list[Attachment]:
        ids = list(dict.fromkeys(conversation_ids))
        if not ids:
            return []
        stmt = select(Attachment).where(Attachment.conversation_id.in_(ids))
        return list((await session.scalars(stmt)).all())
    async def usage_for_owner(
        self, session: AsyncSession, *, user_token: str, now: datetime
    ) -> tuple[int, int, int]:
        unbound_stmt = select(
            func.count(Attachment.id),
            func.coalesce(func.sum(Attachment.size_bytes), 0),
        ).where(
            Attachment.user_token == user_token,
            Attachment.message_id.is_(None),
            Attachment.status == "UPLOADED",
            (Attachment.expires_at.is_(None) | (Attachment.expires_at > now)),
        )
        unbound_count, unbound_bytes = (await session.execute(unbound_stmt)).one()
        total_stmt = select(func.coalesce(func.sum(Attachment.size_bytes), 0)).where(
            Attachment.user_token == user_token,
            Attachment.status != "DELETED",
        )
        total_bytes = await session.scalar(total_stmt)
        return int(unbound_count or 0), int(unbound_bytes or 0), int(total_bytes or 0)

