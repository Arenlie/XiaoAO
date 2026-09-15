from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.evidence import ConversationContextSlot, ConversationTopic


class TopicRepository:
    async def create(
        self,
        session: AsyncSession,
        *,
        conversation_id: UUID,
        title: str,
        topic_summary: str = "",
        primary_subject: dict | None = None,
        scope: dict | None = None,
        current_goal: str | None = None,
        searchable_text: str = "",
        last_task_id: UUID | None = None,
    ) -> ConversationTopic:
        topic = ConversationTopic(
            topic_id=uuid4(),
            conversation_id=conversation_id,
            title=(title or "新主题")[:500],
            topic_summary=(topic_summary or "")[:8000],
            primary_subject=dict(primary_subject or {}),
            scope=dict(scope or {}),
            current_goal=current_goal,
            searchable_text=(searchable_text or "")[:16000],
            last_task_id=last_task_id,
            status="ACTIVE",
            version=1,
        )
        session.add(topic)
        await session.flush()
        return topic

    async def get(
        self, session: AsyncSession, conversation_id: UUID, topic_id: UUID
    ) -> ConversationTopic | None:
        result = await session.execute(
            select(ConversationTopic).where(
                ConversationTopic.conversation_id == conversation_id,
                ConversationTopic.topic_id == topic_id,
            )
        )
        return result.scalar_one_or_none()

    async def list_recent(
        self, session: AsyncSession, conversation_id: UUID, *, limit: int = 50
    ) -> list[ConversationTopic]:
        result = await session.execute(
            select(ConversationTopic)
            .where(ConversationTopic.conversation_id == conversation_id)
            .order_by(ConversationTopic.updated_at.desc())
            .limit(max(1, min(limit, 200)))
        )
        return list(result.scalars())

    async def update(
        self,
        session: AsyncSession,
        topic: ConversationTopic,
        *,
        title: str | None = None,
        topic_summary: str | None = None,
        primary_subject: dict | None = None,
        scope: dict | None = None,
        current_goal: str | None = None,
        searchable_text: str | None = None,
        last_task_id: UUID | None = None,
    ) -> ConversationTopic:
        if title:
            topic.title = title[:500]
        if topic_summary is not None:
            topic.topic_summary = topic_summary[:8000]
        if primary_subject is not None:
            topic.primary_subject = dict(primary_subject)
        if scope is not None:
            topic.scope = dict(scope)
        if current_goal is not None:
            topic.current_goal = current_goal[:4000]
        if searchable_text is not None:
            topic.searchable_text = searchable_text[:16000]
        if last_task_id is not None:
            topic.last_task_id = last_task_id
        topic.updated_at = datetime.now(UTC)
        topic.version = int(topic.version or 0) + 1
        await session.flush()
        return topic

    async def load_slots(
        self, session: AsyncSession, conversation_id: UUID
    ) -> list[ConversationContextSlot]:
        result = await session.execute(
            select(ConversationContextSlot)
            .where(ConversationContextSlot.conversation_id == conversation_id)
            .order_by(ConversationContextSlot.slot_no.asc())
        )
        return list(result.scalars())

    async def replace_slots(
        self,
        session: AsyncSession,
        conversation_id: UUID,
        topic_ids: list[UUID],
    ) -> None:
        await session.execute(
            delete(ConversationContextSlot).where(
                ConversationContextSlot.conversation_id == conversation_id
            )
        )
        now = datetime.now(UTC)
        seen: set[UUID] = set()
        for slot_no, topic_id in enumerate(topic_ids[:3]):
            if topic_id in seen:
                continue
            seen.add(topic_id)
            session.add(
                ConversationContextSlot(
                    conversation_id=conversation_id,
                    slot_no=slot_no,
                    topic_id=topic_id,
                    activated_at=now,
                )
            )
        await session.flush()
