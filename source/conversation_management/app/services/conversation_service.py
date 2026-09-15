from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from redis.asyncio import Redis
from sqlalchemy import select, update

from app.config import Settings
from app.domain.enums import ConversationStatus, TitleSource
from app.domain.exceptions import ConflictError, NotFoundError
from app.models.branch import ConversationBranch
from app.models.conversation import Conversation
from app.models.generation_task import GenerationTask
from app.models.message import Message
from app.models.execution_event import TaskExecutionEvent
from app.repositories.conversation_repository import ConversationRepository
from app.repositories.message_repository import MessageRepository
from app.services.outbox_service import OutboxService
from app.services.queue_service import QueueService


class ConversationService:
    def __init__(
        self,
        session_factory,
        redis: Redis,
        settings: Settings,
        conversation_repo: ConversationRepository,
        message_repo: MessageRepository,
        queue: QueueService,
        outbox: OutboxService,
        events,
    ) -> None:
        self.session_factory = session_factory
        self.redis = redis
        self.settings = settings
        self.conversation_repo = conversation_repo
        self.message_repo = message_repo
        self.queue = queue
        self.outbox = outbox
        self.events = events

    async def create(self, user_token: str, app_code: str) -> Conversation:
        now = datetime.now(UTC)
        async with self.session_factory() as session, session.begin():
            conversation = Conversation(
                id=uuid4(),
                user_token=user_token,
                app_code=app_code,
                expires_at=now + timedelta(days=self.settings.conversation_retention_days),
            )
            branch_id = uuid4()
            conversation.active_branch_id = branch_id
            session.add(conversation)
            await session.flush([conversation])

            branch = ConversationBranch(
                id=branch_id, conversation_id=conversation.id, fork_type="ROOT"
            )
            session.add(branch)
            await session.flush([branch])
            return conversation

    async def list(
        self,
        user_token: str,
        app_code: str,
        search: str | None,
        limit: int,
        offset: int = 0,
    ) -> list[Conversation]:
        async with self.session_factory() as session:
            return await self.conversation_repo.list_owned(
                session, user_token, app_code, search, limit, offset
            )

    async def get(self, conversation_id: UUID, user_token: str) -> Conversation:
        async with self.session_factory() as session:
            conversation = await self.conversation_repo.get_owned(
                session, conversation_id, user_token
            )
            if conversation is None:
                raise NotFoundError("CONVERSATION_NOT_FOUND", "会话不存在")
            return conversation

    async def messages(self, conversation_id: UUID, user_token: str) -> list[Message]:
        async with self.session_factory() as session:
            conversation = await self.conversation_repo.get_owned(
                session, conversation_id, user_token
            )
            if conversation is None:
                raise NotFoundError("CONVERSATION_NOT_FOUND", "会话不存在")
            branch = await self.conversation_repo.active_branch(session, conversation)
            return await self.message_repo.path_to_leaf(
                session, branch.active_leaf_message_id if branch else None, conversation_id=conversation_id
            )

    async def rename(
        self, conversation_id: UUID, user_token: str, title: str
    ) -> Conversation:
        title = title.strip()
        async with self.session_factory() as session, session.begin():
            conversation = await self.conversation_repo.get_owned(
                session, conversation_id, user_token, for_update=True
            )
            if conversation is None:
                raise NotFoundError("CONVERSATION_NOT_FOUND", "会话不存在")
            conversation.title = title
            conversation.title_source = TitleSource.MANUAL.value
            await session.flush()
            return conversation

    async def pin(
        self, conversation_id: UUID, user_token: str, pinned: bool
    ) -> Conversation:
        async with self.session_factory() as session, session.begin():
            conversation = await self.conversation_repo.get_owned(
                session, conversation_id, user_token, for_update=True
            )
            if conversation is None:
                raise NotFoundError("CONVERSATION_NOT_FOUND", "会话不存在")
            conversation.is_pinned = pinned
            conversation.pinned_at = datetime.now(UTC) if pinned else None
            await session.flush()
            return conversation

    async def delete(self, conversation_id: UUID, user_token: str) -> None:
        task_ids: list[UUID] = []
        dify_conversation_ids: list[str] = []
        cleanup_event_id: UUID | None = None
        async with self.session_factory() as session, session.begin():
            conversation = await self.conversation_repo.get_owned(
                session, conversation_id, user_token, for_update=True
            )
            if conversation is None:
                raise NotFoundError("CONVERSATION_NOT_FOUND", "会话不存在")
            active_stmt = select(GenerationTask.id).where(
                GenerationTask.conversation_id == conversation.id,
                GenerationTask.status.not_in(
                    {"COMPLETED", "FAILED", "STOPPED", "TIMEOUT"}
                ),
            )
            task_ids = list((await session.scalars(active_stmt)).all())
            dify_conversation_ids = list(
                (
                    await session.scalars(
                        select(ConversationBranch.dify_conversation_id).where(
                            ConversationBranch.conversation_id == conversation.id,
                            ConversationBranch.dify_conversation_id.is_not(None),
                        )
                    )
                ).all()
            )
            conversation.status = ConversationStatus.DELETED.value
            conversation.deleted_at = datetime.now(UTC)
            conversation.is_pinned = False
            conversation.pinned_at = None
            if task_ids:
                await session.execute(
                    update(GenerationTask)
                    .where(GenerationTask.id.in_(task_ids))
                    .values(status="STOP_REQUESTED")
                )
            unique_dify_ids = list(dict.fromkeys(dify_conversation_ids))
            if (
                self.settings.outbox_enabled
                and unique_dify_ids
            ):
                event = self.outbox.add(
                    session,
                    conversation_id=conversation.id,
                    aggregate_type="conversation",
                    aggregate_id=conversation.id,
                    event_type="dify_cleanup.enqueue",
                    payload={
                        "conversation_id": str(conversation.id),
                        "user_token": user_token,
                        "dify_conversation_ids": unique_dify_ids,
                    },
                    deduplication_key=f"dify_cleanup.enqueue:{conversation.id}",
                )
                cleanup_event_id = event.id

        for task_id in task_ids:
            await self.redis.setex(
                f"chat:cancel:{task_id}", self.settings.task_context_ttl_seconds, "1"
            )
        if cleanup_event_id and self.settings.outbox_fast_publish_enabled:
            await self.outbox.publish_fast(cleanup_event_id)
        elif dify_conversation_ids and not self.settings.outbox_enabled:
            await self.queue.enqueue_dify_cleanup(
                conversation_id,
                user_token,
                list(dict.fromkeys(dify_conversation_ids)),
            )

    async def branches(
        self, conversation_id: UUID, user_token: str
    ) -> list[ConversationBranch]:
        async with self.session_factory() as session:
            conversation = await self.conversation_repo.get_owned(
                session, conversation_id, user_token
            )
            if conversation is None:
                raise NotFoundError("CONVERSATION_NOT_FOUND", "会话不存在")
            return await self.conversation_repo.branches(session, conversation.id)

    async def activate_branch(
        self, conversation_id: UUID, branch_id: UUID, user_token: str
    ) -> ConversationBranch:
        async with self.session_factory() as session, session.begin():
            conversation = await self.conversation_repo.get_owned(
                session, conversation_id, user_token, for_update=True
            )
            if conversation is None:
                raise NotFoundError("CONVERSATION_NOT_FOUND", "会话不存在")
            if await self.conversation_repo.count_active_tasks(session, conversation.id):
                raise ConflictError("CONVERSATION_BUSY", "运行中任务结束前不能切换分支")
            branch = await session.scalar(
                select(ConversationBranch).where(
                    ConversationBranch.id == branch_id,
                    ConversationBranch.conversation_id == conversation.id,
                )
            )
            if branch is None:
                raise NotFoundError("BRANCH_NOT_FOUND", "分支不存在")
            conversation.active_branch_id = branch.id
            await session.flush()
            return branch


    async def message_execution_events(
        self, message_id: UUID, user_token: str, *, after_sequence=0, limit=2000
    ) -> list[TaskExecutionEvent]:
        async with self.session_factory() as session:
            message = await session.scalar(
                select(Message)
                .join(Conversation, Conversation.id == Message.conversation_id)
                .where(
                    Message.id == message_id,
                    Conversation.user_token == user_token,
                    Conversation.deleted_at.is_(None),
                )
            )
            if message is None:
                raise NotFoundError("MESSAGE_NOT_FOUND", "消息不存在")
        return await self.events.list_message_events(message_id, after_sequence=after_sequence, limit=limit)

    async def alternatives(self, message_id: UUID, user_token: str) -> list[Message]:
        async with self.session_factory() as session:
            message = await self.message_repo.get_owned(session, message_id, user_token)
            if message is None:
                raise NotFoundError("MESSAGE_NOT_FOUND", "消息不存在")
            return await self.message_repo.alternatives(session, message)
