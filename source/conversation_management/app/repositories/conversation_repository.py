from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import Select, and_, desc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import ConversationStatus
from app.models.branch import ConversationBranch
from app.models.conversation import Conversation


class ConversationRepository:
    async def get_owned(
        self,
        session: AsyncSession,
        conversation_id: UUID,
        user_token: str,
        *,
        include_deleted: bool = False,
        for_update: bool = False,
    ) -> Conversation | None:
        stmt: Select[tuple[Conversation]] = select(Conversation).where(
            Conversation.id == conversation_id,
            Conversation.user_token == user_token,
        )
        if not include_deleted:
            stmt = stmt.where(Conversation.status == ConversationStatus.ACTIVE.value)
        if for_update:
            stmt = stmt.with_for_update()
        return await session.scalar(stmt)

    async def list_owned(
        self,
        session: AsyncSession,
        user_token: str,
        app_code: str,
        search: str | None,
        limit: int,
        offset: int = 0,
    ) -> list[Conversation]:
        stmt = select(Conversation).where(
            Conversation.user_token == user_token,
            Conversation.app_code == app_code,
            Conversation.status == ConversationStatus.ACTIVE.value,
        )
        if search:
            escaped = search.replace("%", "\\%").replace("_", "\\_")
            stmt = stmt.where(Conversation.title.ilike(f"%{escaped}%", escape="\\"))
        stmt = stmt.order_by(
            desc(Conversation.is_pinned),
            desc(Conversation.pinned_at).nullslast(),
            desc(Conversation.last_message_at).nullslast(),
            desc(Conversation.id),
        ).offset(offset).limit(limit)
        return list((await session.scalars(stmt)).all())

    async def active_branch(
        self, session: AsyncSession, conversation: Conversation, *, for_update: bool = False
    ) -> ConversationBranch | None:
        if conversation.active_branch_id is None:
            return None
        stmt = select(ConversationBranch).where(
            ConversationBranch.id == conversation.active_branch_id,
            ConversationBranch.conversation_id == conversation.id,
        )
        if for_update:
            stmt = stmt.with_for_update()
        return await session.scalar(stmt)

    async def branches(
        self, session: AsyncSession, conversation_id: UUID
    ) -> list[ConversationBranch]:
        stmt = (
            select(ConversationBranch)
            .where(ConversationBranch.conversation_id == conversation_id)
            .order_by(ConversationBranch.created_at)
        )
        return list((await session.scalars(stmt)).all())

    async def count_active_tasks(self, session: AsyncSession, conversation_id: UUID) -> int:
        from app.models.generation_task import GenerationTask

        terminal = {"COMPLETED", "FAILED", "STOPPED", "TIMEOUT"}
        stmt = select(func.count()).select_from(GenerationTask).where(
            GenerationTask.conversation_id == conversation_id,
            GenerationTask.status.not_in(terminal),
        )
        return int((await session.scalar(stmt)) or 0)
