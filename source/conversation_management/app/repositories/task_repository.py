from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.conversation import Conversation
from app.models.generation_task import GenerationTask


class TaskRepository:
    async def get_owned(
        self,
        session: AsyncSession,
        task_id: UUID,
        user_token: str,
        *,
        for_update: bool = False,
    ) -> GenerationTask | None:
        stmt = (
            select(GenerationTask)
            .join(Conversation, Conversation.id == GenerationTask.conversation_id)
            .where(GenerationTask.id == task_id, Conversation.user_token == user_token)
        )
        if for_update:
            stmt = stmt.with_for_update()
        return await session.scalar(stmt)

    async def get(self, session: AsyncSession, task_id: UUID, *, for_update: bool = False) -> GenerationTask | None:
        stmt = select(GenerationTask).where(GenerationTask.id == task_id)
        if for_update:
            stmt = stmt.with_for_update()
        return await session.scalar(stmt)
