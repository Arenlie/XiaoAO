from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.execution_event import TaskExecutionEvent


class EventRepository:
    async def add(
        self, session: AsyncSession, event: TaskExecutionEvent
    ) -> TaskExecutionEvent:
        session.add(event)
        await session.flush([event])
        return event

    async def list_task(
        self,
        session: AsyncSession,
        task_id: UUID,
        *,
        after_sequence: int = 0,
        limit: int = 500,
    ) -> list[TaskExecutionEvent]:
        result = await session.execute(
            select(TaskExecutionEvent)
            .where(
                TaskExecutionEvent.task_id == task_id,
                TaskExecutionEvent.sequence_no > after_sequence,
            )
            .order_by(TaskExecutionEvent.sequence_no.asc())
            .limit(limit)
        )
        return list(result.scalars())

    async def list_message(
        self, session: AsyncSession, message_id: UUID, *, limit: int = 2000, after_sequence: int = 0
    ) -> list[TaskExecutionEvent]:
        result = await session.execute(
            select(TaskExecutionEvent)
            .where(TaskExecutionEvent.message_id == message_id, TaskExecutionEvent.sequence_no > after_sequence)
            .order_by(TaskExecutionEvent.sequence_no.asc())
            .limit(limit)
        )
        return list(result.scalars())
