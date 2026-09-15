from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.outbox_event import OutboxEvent


class OutboxRepository:
    async def claim_one(
        self,
        session: AsyncSession,
        event_id: UUID,
        *,
        worker_id: str,
        lock_timeout_seconds: int,
    ) -> OutboxEvent | None:
        now = datetime.now(UTC)
        stale_before = now - timedelta(seconds=lock_timeout_seconds)
        event = await session.scalar(
            select(OutboxEvent)
            .where(
                OutboxEvent.id == event_id,
                or_(
                    OutboxEvent.status == "PENDING",
                    and_(
                        OutboxEvent.status == "PROCESSING",
                        OutboxEvent.locked_at < stale_before,
                    ),
                ),
            )
            .with_for_update(skip_locked=True)
        )
        if event is None:
            return None
        event.status = "PROCESSING"
        event.locked_by = worker_id
        event.locked_at = now
        event.attempt_count += 1
        await session.flush()
        return event

    async def claim_batch(
        self,
        session: AsyncSession,
        *,
        worker_id: str,
        batch_size: int,
        lock_timeout_seconds: int,
    ) -> list[OutboxEvent]:
        now = datetime.now(UTC)
        stale_before = now - timedelta(seconds=lock_timeout_seconds)
        rows = list(
            (
                await session.scalars(
                    select(OutboxEvent)
                    .where(
                        OutboxEvent.available_at <= now,
                        or_(
                            OutboxEvent.status == "PENDING",
                            and_(
                                OutboxEvent.status == "PROCESSING",
                                OutboxEvent.locked_at < stale_before,
                            ),
                        ),
                    )
                    .order_by(OutboxEvent.created_at, OutboxEvent.id)
                    .limit(batch_size)
                    .with_for_update(skip_locked=True)
                )
            ).all()
        )
        for event in rows:
            event.status = "PROCESSING"
            event.locked_by = worker_id
            event.locked_at = now
            event.attempt_count += 1
        await session.flush()
        return rows

    async def get(self, session: AsyncSession, event_id: UUID) -> OutboxEvent | None:
        return await session.get(OutboxEvent, event_id)
