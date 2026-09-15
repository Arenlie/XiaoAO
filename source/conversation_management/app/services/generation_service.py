from __future__ import annotations

import json
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode
from uuid import UUID, uuid4

from redis.asyncio import Redis
from sqlalchemy import select

from app.config import Settings
from app.attachments.service import AttachmentService
from app.domain.enums import (
    BranchForkType,
    GenerationTaskStatus,
    MessageRole,
    MessageStatus,
    OperationType,
    TitleSource,
)
from app.domain.exceptions import ConflictError, NotFoundError
from app.models.branch import ConversationBranch
from app.models.conversation import Conversation
from app.models.entity_selection import PendingEntitySelection
from app.models.generation_task import GenerationTask
from app.models.message import Message
from app.repositories.conversation_repository import ConversationRepository
from app.repositories.message_repository import MessageRepository
from app.schemas.message import SendMessageAccepted
from app.services.concurrency_service import ConcurrencyService
from app.services.event_service import EventService
from app.services.outbox_service import OutboxService
from app.services.queue_service import QueueService
from app.services.title_service import rule_title
from app.services.idempotency import IdempotencyGuard


class GenerationService:
    @staticmethod
    def _normalize_execution_mode(value: str | None, default: str = "normal") -> str:
        mode = str(value or default or "normal").strip().lower()
        # v1.0.0 merges the former expert graph into normal ReAct.  Keep accepting
        # the old value at the API boundary, but persist/return only normal.
        return "normal" if mode == "expert" else mode

    def __init__(
        self,
        *,
        session_factory,
        redis: Redis,
        settings: Settings,
        conversation_repo: ConversationRepository,
        message_repo: MessageRepository,
        events: EventService,
        queue: QueueService,
        concurrency: ConcurrencyService,
        outbox: OutboxService,
        attachment_service: AttachmentService,
    ) -> None:
        self.session_factory = session_factory
        self.redis = redis
        self.settings = settings
        self.conversation_repo = conversation_repo
        self.message_repo = message_repo
        self.events = events
        self.queue = queue
        self.concurrency = concurrency
        self.outbox = outbox
        self.attachment_service = attachment_service

    def _stream_url(
        self, task_id: UUID, user_token: str, *, final_delta_event: str = "answer.delta"
    ) -> str:
        params = {"X-User-Token": user_token}
        if final_delta_event != "answer.delta":
            params["final_delta_event"] = final_delta_event
        query = urlencode(params)
        return f"{self.settings.api_prefix}/tasks/{task_id}/events?{query}"

    async def _store_task_context(
        self,
        *,
        accepted: SendMessageAccepted,
        user_token: str,
        data_access_token: str,
        query: str,
        context_before_message_id: str,
        selected_entity: dict | None,
        attachment_ids: list[str] | None = None,
        execution_mode: str = "normal",
        lease_id: str | None = None,
    ) -> None:
        context = {
            "lease_id": lease_id,
            "task_id": str(accepted.task_id),
            "conversation_id": str(accepted.conversation_id),
            "user_message_id": str(accepted.user_message_id),
            "assistant_message_id": str(accepted.assistant_message_id),
            "user_token": user_token,
            "data_access_token": data_access_token,
            "query": query,
            "context_before_message_id": context_before_message_id,
            "selected_entity": selected_entity,
            "attachment_ids": attachment_ids or [],
            "execution_mode": execution_mode,
            "graph_attempt": 0,
        }
        await self.redis.setex(
            f"chat:task_context:{accepted.task_id}",
            self.settings.task_context_ttl_seconds,
            json.dumps(context, ensure_ascii=False, separators=(",", ":")),
        )

    def _add_generation_outbox(
        self,
        session,
        *,
        accepted: SendMessageAccepted,
        reason: str,
    ):
        return self.outbox.add(
            session,
            conversation_id=accepted.conversation_id,
            aggregate_type="generation_task",
            aggregate_id=accepted.task_id,
            event_type="generation.enqueue",
            payload={
                "task_id": str(accepted.task_id),
                "reason": reason,
            },
            deduplication_key=f"generation.enqueue:{accepted.task_id}:{reason}",
        )

    async def _publish_after_commit(
        self,
        *,
        generation_event_id: UUID | None,
        title_event_id: UUID | None = None,
        accepted: SendMessageAccepted,
        direct_reason: str,
    ) -> None:
        if self.settings.outbox_enabled:
            if self.settings.outbox_fast_publish_enabled:
                if generation_event_id:
                    with suppress(Exception):
                        await self.outbox.publish_fast(generation_event_id)
                if title_event_id:
                    with suppress(Exception):
                        await self.outbox.publish_fast(title_event_id)
            return
        await self.events.publish(
            accepted.task_id,
            "task.queued",
            {
                "task_id": str(accepted.task_id),
                "conversation_id": str(accepted.conversation_id),
                "reason": direct_reason,
            },
        )
        await self.queue.enqueue_generation(accepted.task_id)

    async def send(self, *, client_session_id: str | None = None, **kwargs) -> SendMessageAccepted:
        key = kwargs.get("idempotency_key")
        if not key:
            return await self._send_impl(**kwargs)
        guard = IdempotencyGuard(
            self.redis, user=kwargs["user_token"], app=kwargs["app_code"],
            client_session=client_session_id, conversation=kwargs.get("conversation_id"), key=key,
            payload={name: kwargs.get(name) for name in ("content", "attachment_ids", "execution_mode")},
            ttl=self.settings.task_context_ttl_seconds,
        )
        cached = await guard.begin()
        if cached:
            return SendMessageAccepted.model_validate(cached)
        try:
            accepted = await self._send_impl(**{**kwargs, "idempotency_key": guard.digest})
        except BaseException:
            await guard.abort()
            raise
        # A committed task remains accepted even if the best-effort response cache
        # is temporarily unavailable; the claim still prevents immediate duplicates.
        with suppress(Exception):
            await guard.finish(accepted.model_dump(mode="json"))
        return accepted

    async def _send_impl(
        self,
        *,
        user_token: str,
        data_access_token: str,
        app_code: str,
        content: str,
        conversation_id: UUID | None,
        idempotency_key: str | None,
        request_id: str,
        attachment_ids: list[UUID] | None = None,
        execution_mode: str | None = None,
    ) -> SendMessageAccepted:
        execution_mode = self._normalize_execution_mode(execution_mode, self.settings.default_execution_mode)
        now = datetime.now(UTC)
        new_conversation_id = conversation_id or uuid4()
        generation_event_id: UUID | None = None
        title_event_id: UUID | None = None
        superseded_diagnoses = []
        pending_diagnosis_context = {}
        lease_id = await self.concurrency.acquire(new_conversation_id, user_token)
        try:
            async with self.session_factory() as session, session.begin():
                if conversation_id is None:
                    # These ORM models intentionally do not expose relationship()
                    # attributes.  Therefore SQLAlchemy's unit-of-work cannot infer
                    # the required INSERT order from object relationships.  Flush
                    # the parent conversation before inserting its root branch.
                    branch_id = uuid4()
                    conversation = Conversation(
                        id=new_conversation_id,
                        user_token=user_token,
                        app_code=app_code,
                        title=rule_title(content),
                        title_source=TitleSource.AUTO_RULE.value,
                        active_branch_id=branch_id,
                        expires_at=now
                        + timedelta(days=self.settings.conversation_retention_days),
                        last_message_at=now,
                    )
                    session.add(conversation)
                    await session.flush([conversation])

                    branch = ConversationBranch(
                        id=branch_id,
                        conversation_id=new_conversation_id,
                        fork_type=BranchForkType.ROOT.value,
                    )
                    session.add(branch)
                    await session.flush([branch])
                else:
                    conversation = await self.conversation_repo.get_owned(
                        session, conversation_id, user_token, for_update=True
                    )
                    if conversation is None:
                        raise NotFoundError("CONVERSATION_NOT_FOUND", "会话不存在")
                    branch = await self.conversation_repo.active_branch(
                        session, conversation, for_update=True
                    )
                    if branch is None:
                        raise ConflictError("BRANCH_NOT_FOUND", "会话没有活动分支")

                    waiting = (await session.scalars(select(GenerationTask).where(
                        GenerationTask.conversation_id == conversation.id,
                        GenerationTask.branch_id == branch.id,
                        GenerationTask.status.in_(["WAITING_CONFIRMATION", "COMPLETED"]),
                        GenerationTask.assistant_message_id == branch.active_leaf_message_id).with_for_update())).all()
                    for prior in waiting:
                        prior_message = await session.get(Message, prior.assistant_message_id, with_for_update=True)
                        meta = dict(prior_message.metadata_json or {})
                        choice = dict(meta.get("diagnosis_confirmation") or {})
                        if choice.get("status") != "PENDING":
                            continue
                        if choice.get("status") == "PENDING" and datetime.fromisoformat(choice["expires_at"]) > now:
                            pending_diagnosis_context = {"query":choice.get("query"), "target":choice.get("target"),
                                "options":choice.get("options"), "previous_task_id":str(prior.id),
                                "resume_state":dict(meta.get("diagnosis_resume_state") or {})}
                        choice["status"] = "SUPERSEDED"
                        meta["diagnosis_confirmation"] = choice
                        prior_message.metadata_json = meta
                        if prior.status == "WAITING_CONFIRMATION":
                            prior_message.status = "STOPPED"
                            prior.status, prior.completed_at = "STOPPED", now
                            superseded_diagnoses.append(prior.id)

                    # A WAITING_SELECTION task is not terminal: it is the same user turn
                    # paused for one server-validated entity choice.  The worker releases
                    # the short Redis execution lock while waiting, so a second browser
                    # using the same user token could otherwise append a new turn to this
                    # branch.  That would move active_leaf_message_id and make the old
                    # task resume into a newer conversational state.  Reject new turns
                    # until the pending selection is completed or explicitly stopped.
                    pending_selection_task = await session.scalar(
                        select(GenerationTask)
                        .join(
                            PendingEntitySelection,
                            PendingEntitySelection.task_id == GenerationTask.id,
                        )
                        .where(
                            GenerationTask.conversation_id == conversation.id,
                            GenerationTask.status
                            == GenerationTaskStatus.WAITING_SELECTION.value,
                            PendingEntitySelection.status == "PENDING",
                        )
                        .order_by(PendingEntitySelection.created_at.desc())
                        .limit(1)
                    )
                    if pending_selection_task is not None:
                        raise ConflictError(
                            "ENTITY_SELECTION_PENDING",
                            "当前会话正在等待实体选择，请先完成选择或停止该任务后再发送新问题。",
                        )

                attachment_rows = await self.attachment_service.validate_for_message(
                    session,
                    attachment_ids=list(attachment_ids or []),
                    user_token=user_token,
                )
                attachment_summaries = [
                    self.attachment_service.descriptor(row).model_dump(mode="json")
                    for row in attachment_rows
                ]
                display_content = content.strip() or (
                    f"分析附件：{attachment_summaries[0].get('filename', '附件')}"
                    if attachment_summaries
                    else "新对话"
                )

                is_first_message = conversation.first_user_message_id is None
                if is_first_message and not content.strip():
                    conversation.title = rule_title(display_content)
                    conversation.title_source = TitleSource.AUTO_RULE.value
                if (
                    is_first_message
                    and conversation.title_source == TitleSource.DEFAULT.value
                ):
                    conversation.title = rule_title(display_content)
                    conversation.title_source = TitleSource.AUTO_RULE.value

                user_message = Message(
                    id=uuid4(),
                    conversation_id=conversation.id,
                    branch_id=branch.id,
                    parent_message_id=branch.active_leaf_message_id,
                    role=MessageRole.USER.value,
                    content=content,
                    status=MessageStatus.COMPLETED.value,
                    metadata_json={
                        **(
                            {"attachments": attachment_summaries}
                            if attachment_summaries
                            else {}
                        ),
                        "execution_mode": execution_mode,
                    },
                )
                assistant_message = Message(
                    id=uuid4(),
                    conversation_id=conversation.id,
                    branch_id=branch.id,
                    parent_message_id=user_message.id,
                    role=MessageRole.ASSISTANT.value,
                    content="",
                    status=MessageStatus.PENDING.value,
                )
                task = GenerationTask(
                    id=uuid4(),
                    conversation_id=conversation.id,
                    branch_id=branch.id,
                    user_message_id=user_message.id,
                    assistant_message_id=assistant_message.id,
                    operation=OperationType.SEND.value,
                    execution_mode=execution_mode,
                    status=GenerationTaskStatus.QUEUED.value,
                    idempotency_key=idempotency_key,
                    request_id=request_id,
                )
                # Persist the self-referencing message tree in dependency order.
                # user_message must exist before assistant_message.parent_message_id,
                # and both messages must exist before generation_tasks foreign keys.
                session.add(user_message)
                await session.flush([user_message])
                await self.attachment_service.bind_rows(
                    attachment_rows,
                    conversation_id=conversation.id,
                    message_id=user_message.id,
                )
                if attachment_rows:
                    await session.flush(attachment_rows)

                session.add(assistant_message)
                await session.flush([assistant_message])

                session.add(task)
                branch.active_leaf_message_id = assistant_message.id
                conversation.active_branch_id = branch.id
                conversation.last_message_at = now
                conversation.last_message_preview = display_content[:300]
                if conversation.first_user_message_id is None:
                    conversation.first_user_message_id = user_message.id
                await session.flush()

                accepted = SendMessageAccepted(
                    task_id=task.id,
                    conversation_id=conversation.id,
                    user_message_id=user_message.id,
                    assistant_message_id=assistant_message.id,
                    title=conversation.title,
                    execution_mode=execution_mode,
                    stream_url=self._stream_url(task.id, user_token),
                    agent_stream_url=self._stream_url(
                        task.id, user_token, final_delta_event="agent.output.delta"
                    ),
                )
                # Redis context is written before DB COMMIT. A rollback only leaves a
                # short-lived orphan key; a successful commit always has the token context.
                await self._store_task_context(
                    lease_id=lease_id,
                    accepted=accepted,
                    user_token=user_token,
                    data_access_token=data_access_token,
                    query=content,
                    context_before_message_id=str(user_message.parent_message_id or ""),
                    selected_entity=None,
                    attachment_ids=[str(value) for value in (attachment_ids or [])],
                    execution_mode=execution_mode,
                )
                if pending_diagnosis_context:
                    context_key = f"chat:task_context:{task.id}"
                    pending_raw = await self.redis.get(context_key)
                    if pending_raw:
                        pending_context = json.loads(pending_raw)
                        pending_context["pending_diagnosis_context"] = pending_diagnosis_context
                        await self.redis.setex(context_key, self.settings.task_context_ttl_seconds,
                            json.dumps(pending_context, ensure_ascii=False))
                if self.settings.outbox_enabled:
                    generation_event = self._add_generation_outbox(
                        session,
                        accepted=accepted,
                        reason="submitted",
                    )
                    generation_event_id = generation_event.id
                    if is_first_message and self.settings.title_mode != "rule":
                        title_event = self.outbox.add(
                            session,
                            conversation_id=conversation.id,
                            aggregate_type="conversation",
                            aggregate_id=conversation.id,
                            event_type="title.enqueue",
                            payload={
                                "conversation_id": str(conversation.id),
                                "user_token": user_token,
                                "query": content,
                            },
                            deduplication_key=f"title.enqueue:{conversation.id}",
                        )
                        title_event_id = title_event.id

            for previous_id in superseded_diagnoses:
                with suppress(Exception):
                    await self.events.publish(previous_id,"diagnosis.mode.superseded",
                        {"display_message":"已转入新问题，未启动旧的详细诊断。"}, status="SUPERSEDED")
                    await self.events.publish(previous_id,"task.stopped",
                        {"task_id":str(previous_id), "reason":"new_question"}, status="STOPPED")
            await self._publish_after_commit(
                generation_event_id=generation_event_id,
                title_event_id=title_event_id,
                accepted=accepted,
                direct_reason="submitted",
            )
            if (
                not self.settings.outbox_enabled
                and is_first_message
                and self.settings.title_mode != "rule"
            ):
                await self.queue.enqueue_title(conversation.id, user_token, content)
            return accepted
        except Exception:
            await self.concurrency.release(new_conversation_id, user_token, lease_id)
            raise

    async def edit_and_resubmit(
        self,
        *,
        message_id: UUID,
        content: str,
        user_token: str,
        data_access_token: str,
        request_id: str,
        attachment_ids: list[UUID] | None = None,
        execution_mode: str | None = None,
    ) -> SendMessageAccepted:
        execution_mode = self._normalize_execution_mode(execution_mode, self.settings.default_execution_mode)
        async with self.session_factory() as read_session:
            original = await self.message_repo.get_owned(
                read_session, message_id, user_token
            )
            if original is None:
                raise NotFoundError("MESSAGE_NOT_FOUND", "消息不存在")
            if original.role != MessageRole.USER.value:
                raise ConflictError("MESSAGE_ROLE_INVALID", "只允许编辑用户消息")
            conversation_id = original.conversation_id
        generation_event_id: UUID | None = None
        lease_id = await self.concurrency.acquire(conversation_id, user_token)
        try:
            async with self.session_factory() as session, session.begin():
                original = await self.message_repo.get_owned(
                    session, message_id, user_token
                )
                assert original is not None
                if attachment_ids is None:
                    attachment_rows = []
                    attachment_summaries = list(
                        (original.metadata_json or {}).get("attachments") or []
                    )
                else:
                    attachment_rows = await self.attachment_service.validate_for_message(
                        session,
                        attachment_ids=list(attachment_ids),
                        user_token=user_token,
                    )
                    attachment_summaries = [
                        self.attachment_service.descriptor(row).model_dump(mode="json")
                        for row in attachment_rows
                    ]
                conversation = await self.conversation_repo.get_owned(
                    session, original.conversation_id, user_token, for_update=True
                )
                assert conversation is not None
                parent_branch = await session.get(
                    ConversationBranch, original.branch_id
                )
                new_branch = ConversationBranch(
                    id=uuid4(),
                    conversation_id=conversation.id,
                    parent_branch_id=original.branch_id,
                    fork_type=BranchForkType.EDIT.value,
                    forked_from_message_id=original.id,
                    dify_conversation_id=None,
                    active_entity=parent_branch.active_entity if parent_branch else None,
                    summary=parent_branch.summary if parent_branch else None,
                )
                new_user = Message(
                    id=uuid4(),
                    conversation_id=conversation.id,
                    branch_id=new_branch.id,
                    parent_message_id=original.parent_message_id,
                    replaces_message_id=original.id,
                    role=MessageRole.USER.value,
                    content=content,
                    status=MessageStatus.COMPLETED.value,
                    metadata_json={
                        **(
                            {"attachments": attachment_summaries}
                            if attachment_summaries
                            else {}
                        ),
                        "execution_mode": execution_mode,
                    },
                )
                new_assistant = Message(
                    id=uuid4(),
                    conversation_id=conversation.id,
                    branch_id=new_branch.id,
                    parent_message_id=new_user.id,
                    role=MessageRole.ASSISTANT.value,
                    status=MessageStatus.PENDING.value,
                )
                task = GenerationTask(
                    id=uuid4(),
                    conversation_id=conversation.id,
                    branch_id=new_branch.id,
                    user_message_id=new_user.id,
                    assistant_message_id=new_assistant.id,
                    operation=OperationType.EDIT.value,
                    execution_mode=execution_mode,
                    status=GenerationTaskStatus.QUEUED.value,
                    request_id=request_id,
                )
                new_branch.active_leaf_message_id = new_assistant.id
                conversation.active_branch_id = new_branch.id
                conversation.last_message_at = datetime.now(UTC)
                conversation.last_message_preview = (
                    content.strip()
                    or (
                        f"分析附件：{attachment_summaries[0].get('filename', '附件')}"
                        if attachment_summaries
                        else ""
                    )
                )[:300]
                session.add(new_branch)
                await session.flush([new_branch])
                session.add(new_user)
                await session.flush([new_user])
                await self.attachment_service.bind_rows(
                    attachment_rows,
                    conversation_id=conversation.id,
                    message_id=new_user.id,
                )
                if attachment_rows:
                    await session.flush(attachment_rows)
                session.add(new_assistant)
                await session.flush([new_assistant])
                session.add(task)
                await session.flush()
                accepted = SendMessageAccepted(
                    task_id=task.id,
                    conversation_id=conversation.id,
                    user_message_id=new_user.id,
                    assistant_message_id=new_assistant.id,
                    title=conversation.title,
                    execution_mode=execution_mode,
                    stream_url=self._stream_url(task.id, user_token),
                    agent_stream_url=self._stream_url(
                        task.id, user_token, final_delta_event="agent.output.delta"
                    ),
                )
                await self._store_task_context(
                    lease_id=lease_id,
                    accepted=accepted,
                    user_token=user_token,
                    data_access_token=data_access_token,
                    query=content,
                    context_before_message_id=str(original.parent_message_id or ""),
                    selected_entity=None,
                    attachment_ids=[
                        str(item.get("attachment_id"))
                        for item in attachment_summaries
                    ],
                    execution_mode=execution_mode,
                )
                if self.settings.outbox_enabled:
                    event = self._add_generation_outbox(
                        session,
                        accepted=accepted,
                        reason="edit_resubmit",
                    )
                    generation_event_id = event.id

            await self._publish_after_commit(
                generation_event_id=generation_event_id,
                accepted=accepted,
                direct_reason="edit_resubmit",
            )
            return accepted
        except Exception:
            await self.concurrency.release(conversation_id, user_token, lease_id)
            raise

    async def regenerate(
        self,
        *,
        assistant_message_id: UUID,
        force_reresolve: bool,
        user_token: str,
        data_access_token: str,
        request_id: str,
        execution_mode: str | None = None,
    ) -> SendMessageAccepted:
        async with self.session_factory() as read_session:
            original = await self.message_repo.get_owned(
                read_session, assistant_message_id, user_token
            )
            if original is None:
                raise NotFoundError("MESSAGE_NOT_FOUND", "消息不存在")
            if original.role != MessageRole.ASSISTANT.value:
                raise ConflictError("MESSAGE_ROLE_INVALID", "只允许重新生成助手回答")
            parent_user = (
                await self.message_repo.get_owned(
                    read_session, original.parent_message_id, user_token
                )
                if original.parent_message_id
                else None
            )
            if parent_user is None or parent_user.role != MessageRole.USER.value:
                raise ConflictError("MESSAGE_TREE_INVALID", "回答缺少父用户问题")
            regenerate_mode = self._normalize_execution_mode(
                execution_mode
                or (parent_user.metadata_json or {}).get("execution_mode"),
                self.settings.default_execution_mode,
            )
            conversation_id = original.conversation_id
        generation_event_id: UUID | None = None
        lease_id = await self.concurrency.acquire(conversation_id, user_token)
        try:
            async with self.session_factory() as session, session.begin():
                original = await self.message_repo.get_owned(
                    session, assistant_message_id, user_token
                )
                assert original is not None and original.parent_message_id is not None
                parent_user = await self.message_repo.get_owned(
                    session, original.parent_message_id, user_token
                )
                assert parent_user is not None
                conversation = await self.conversation_repo.get_owned(
                    session, original.conversation_id, user_token, for_update=True
                )
                assert conversation is not None
                parent_branch = await session.get(
                    ConversationBranch, original.branch_id
                )
                new_branch = ConversationBranch(
                    id=uuid4(),
                    conversation_id=conversation.id,
                    parent_branch_id=original.branch_id,
                    fork_type=BranchForkType.REGENERATE.value,
                    forked_from_message_id=original.id,
                    dify_conversation_id=None,
                    active_entity=parent_branch.active_entity if parent_branch else None,
                    summary=parent_branch.summary if parent_branch else None,
                )
                new_assistant = Message(
                    id=uuid4(),
                    conversation_id=conversation.id,
                    branch_id=new_branch.id,
                    parent_message_id=parent_user.id,
                    replaces_message_id=original.id,
                    role=MessageRole.ASSISTANT.value,
                    status=MessageStatus.PENDING.value,
                )
                task = GenerationTask(
                    id=uuid4(),
                    conversation_id=conversation.id,
                    branch_id=new_branch.id,
                    user_message_id=parent_user.id,
                    assistant_message_id=new_assistant.id,
                    operation=OperationType.REGENERATE.value,
                    execution_mode=regenerate_mode,
                    status=GenerationTaskStatus.QUEUED.value,
                    request_id=request_id,
                )
                new_branch.active_leaf_message_id = new_assistant.id
                conversation.active_branch_id = new_branch.id
                conversation.last_message_at = datetime.now(UTC)
                session.add(new_branch)
                await session.flush([new_branch])
                session.add(new_assistant)
                await session.flush([new_assistant])
                session.add(task)
                await session.flush()

                entity_result = parent_user.entity_result or {}
                can_reuse = entity_result.get("status") in {"NO_LOOKUP", "UNIQUE"}
                selected = entity_result.get("resolved_entity") if can_reuse else None
                selected = selected if can_reuse and not force_reresolve else None
                accepted = SendMessageAccepted(
                    task_id=task.id,
                    conversation_id=conversation.id,
                    user_message_id=parent_user.id,
                    assistant_message_id=new_assistant.id,
                    title=conversation.title,
                    execution_mode=regenerate_mode,
                    stream_url=self._stream_url(task.id, user_token),
                    agent_stream_url=self._stream_url(
                        task.id, user_token, final_delta_event="agent.output.delta"
                    ),
                )
                await self._store_task_context(
                    lease_id=lease_id,
                    accepted=accepted,
                    user_token=user_token,
                    data_access_token=data_access_token,
                    query=parent_user.content,
                    context_before_message_id=str(parent_user.parent_message_id or ""),
                    selected_entity=selected,
                    attachment_ids=[
                        str(item.get("attachment_id"))
                        for item in (parent_user.metadata_json or {}).get("attachments", [])
                        if item.get("attachment_id")
                    ],
                    execution_mode=regenerate_mode,
                )
                if self.settings.outbox_enabled:
                    event = self._add_generation_outbox(
                        session,
                        accepted=accepted,
                        reason="regenerate",
                    )
                    generation_event_id = event.id

            await self._publish_after_commit(
                generation_event_id=generation_event_id,
                accepted=accepted,
                direct_reason="regenerate",
            )
            return accepted
        except Exception:
            await self.concurrency.release(conversation_id, user_token, lease_id)
            raise
