from __future__ import annotations

from uuid import UUID

from sqlalchemy import literal, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.message import Message


class MessageRepository:
    async def get_owned(
        self, session: AsyncSession, message_id: UUID, user_token: str
    ) -> Message | None:
        from app.models.conversation import Conversation

        stmt = (
            select(Message)
            .join(Conversation, Conversation.id == Message.conversation_id)
            .where(Message.id == message_id, Conversation.user_token == user_token)
        )
        return await session.scalar(stmt)

    async def branch_messages(
        self, session: AsyncSession, branch_id: UUID
    ) -> list[Message]:
        stmt = select(Message).where(Message.branch_id == branch_id).order_by(Message.created_at)
        return list((await session.scalars(stmt)).all())

    async def path_to_leaf(
        self, session: AsyncSession, leaf_message_id: UUID | None, *, conversation_id: UUID | None = None
    ) -> list[Message]:
        if leaf_message_id is None:
            return []
        seed = select(
            Message.id,
            Message.parent_message_id,
            literal(0).label("depth"),
        ).where(Message.id == leaf_message_id)
        if conversation_id is not None:
            seed = seed.where(Message.conversation_id == conversation_id)
        path = seed.cte(name="message_path", recursive=True)
        parent = select(
            Message.id,
            Message.parent_message_id,
            (path.c.depth + 1).label("depth"),
        ).join(path, Message.id == path.c.parent_message_id)
        if conversation_id is not None:
            parent = parent.where(Message.conversation_id == conversation_id)
        path = path.union_all(parent)
        stmt = (
            select(Message)
            .join(path, Message.id == path.c.id)
            .order_by(path.c.depth.desc())
        )
        return list((await session.scalars(stmt)).all())

    async def alternatives(
        self, session: AsyncSession, message: Message
    ) -> list[Message]:
        stmt = (
            select(Message)
            .where(
                Message.conversation_id == message.conversation_id,
                Message.parent_message_id == message.parent_message_id,
                Message.role == message.role,
                Message.deleted_at.is_(None),
            )
            .order_by(Message.created_at)
        )
        return list((await session.scalars(stmt)).all())
