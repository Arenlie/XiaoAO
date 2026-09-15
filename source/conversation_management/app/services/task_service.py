from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import UUID

from redis.asyncio import Redis
from sqlalchemy import select

from app.config import Settings
from app.domain.enums import GenerationTaskStatus, MessageStatus
from app.domain.exceptions import ConflictError, NotFoundError
from app.integrations.dify.agent_client import DifyAgentClient
from app.integrations.dify.entity_identity import build_candidate_id
from app.models.branch import ConversationBranch
from app.models.conversation import Conversation
from app.models.entity_selection import PendingEntitySelection
from app.models.generation_task import GenerationTask
from app.models.message import Message
from app.orchestration.entity_selection_resume import build_selected_entity_result
from app.repositories.task_repository import TaskRepository
from app.services.concurrency_service import ConcurrencyService
from app.services.event_service import EventService
from app.services.outbox_service import OutboxService
from app.services.queue_service import QueueService


from app.services.diagnosis_confirmation import DiagnosisConfirmationMixin


class TaskService(DiagnosisConfirmationMixin):
    def __init__(
        self,
        session_factory,
        redis: Redis,
        settings: Settings,
        task_repo: TaskRepository,
        events: EventService,
        queue: QueueService,
        concurrency: ConcurrencyService,
        agent_client: DifyAgentClient,
        outbox: OutboxService,
    ) -> None:
        self.session_factory = session_factory
        self.redis = redis
        self.settings = settings
        self.task_repo = task_repo
        self.events = events
        self.queue = queue
        self.concurrency = concurrency
        self.agent_client = agent_client
        self.outbox = outbox

    async def get(self, task_id: UUID, user_token: str) -> GenerationTask:
        async with self.session_factory() as session:
            task = await self.task_repo.get_owned(session, task_id, user_token)
            if task is None:
                raise NotFoundError("TASK_NOT_FOUND", "任务不存在")
        if task.status == "WAITING_CONFIRMATION":
            # Reconnect/task polling also repairs pre-upgrade diagnosis prompts,
            # without requiring a new frontend to call the optional choice API.
            await self.get_diagnosis_confirmation(task_id, user_token)
            async with self.session_factory() as session:
                task = await self.task_repo.get_owned(session, task_id, user_token)
        return task

    async def execution_events(self, task_id: UUID, user_token: str):
        await self.get(task_id, user_token)
        return await self.events.list_task_events(task_id)

    @staticmethod
    def _selection_payload(
        task: GenerationTask,
        pending: PendingEntitySelection,
    ) -> dict[str, object]:
        now = datetime.now(UTC)
        return {
            "task_id": task.id,
            "assistant_message_id": task.assistant_message_id,
            "status": task.status,
            "candidates": [{**c, "candidate_id": build_candidate_id(c) if c.get("point_no") or c.get("entity_type") == "point"
                             else c.get("candidate_id") or build_candidate_id(c)}
                            for c in pending.candidates or [] if isinstance(c, dict)],
            "expires_at": pending.expires_at,
            "expires_in_seconds": max(
                0, int((pending.expires_at - now).total_seconds())
            ),
            "last_event_id": f"{int(task.event_sequence or 0)}-0",
        }

    async def get_pending_selection(
        self, task_id: UUID, user_token: str
    ) -> dict[str, object]:
        async with self.session_factory() as session:
            task = await self.task_repo.get_owned(session, task_id, user_token)
            if task is None:
                raise NotFoundError("TASK_NOT_FOUND", "任务不存在")
            pending = await session.scalar(
                select(PendingEntitySelection).where(
                    PendingEntitySelection.task_id == task.id
                )
            )
            if pending is None or pending.status != "PENDING":
                raise NotFoundError("ENTITY_SELECTION_NOT_FOUND", "待选候选不存在")
            if pending.expires_at <= datetime.now(UTC):
                raise ConflictError("ENTITY_SELECTION_EXPIRED", "候选已过期")
            return self._selection_payload(task, pending)

    async def get_pending_selection_for_conversation(
        self, conversation_id: UUID, user_token: str
    ) -> dict[str, object] | None:
        """Poll the user's currently usable selection; absence is a normal result."""
        async with self.session_factory() as session:
            row = await session.execute(
                select(GenerationTask, PendingEntitySelection)
                .join(
                    PendingEntitySelection,
                    PendingEntitySelection.task_id == GenerationTask.id,
                )
                .join(Conversation, Conversation.id == GenerationTask.conversation_id)
                .where(
                    GenerationTask.conversation_id == conversation_id,
                    PendingEntitySelection.conversation_id == conversation_id,
                    Conversation.user_token == user_token,
                    Conversation.status == "ACTIVE",
                    GenerationTask.status
                    == GenerationTaskStatus.WAITING_SELECTION.value,
                    PendingEntitySelection.status == "PENDING",
                    PendingEntitySelection.expires_at > datetime.now(UTC),
                )
                .order_by(PendingEntitySelection.created_at.desc())
                .limit(1)
            )
            found = row.first()
            if found is None:
                return None
            task, pending = found
            # Expiry may occur between the SQL predicate and response construction.
            if pending.expires_at <= datetime.now(UTC) or not pending.candidates:
                return None
            return self._selection_payload(task, pending)

    async def stop(self, task_id: UUID, user_token: str) -> GenerationTask:
        dify_task_id: str | None = None
        release_concurrency = False
        async with self.session_factory() as session, session.begin():
            task = await self.task_repo.get_owned(
                session, task_id, user_token, for_update=True
            )
            if task is None:
                raise NotFoundError("TASK_NOT_FOUND", "任务不存在")
            if task.status in {"COMPLETED", "FAILED", "STOPPED", "TIMEOUT"}:
                raise ConflictError("TASK_NOT_STOPPABLE", "任务已结束")
            if task.status in {
                GenerationTaskStatus.WAITING_SELECTION.value,
                GenerationTaskStatus.WAITING_INPUT.value,
                GenerationTaskStatus.WAITING_CONFIRMATION.value,
            }:
                task.status = GenerationTaskStatus.STOPPED.value
                task.completed_at = datetime.now(UTC)
                release_concurrency = True
                if task.assistant_message_id:
                    assistant = await session.get(
                        Message, task.assistant_message_id, with_for_update=True
                    )
                    if assistant is not None:
                        assistant.status = MessageStatus.STOPPED.value
            else:
                task.status = GenerationTaskStatus.STOP_REQUESTED.value
            dify_task_id = task.dify_task_id
            await session.flush()

        await self.redis.setex(
            f"chat:cancel:{task_id}", self.settings.task_context_ttl_seconds, "1"
        )
        terminal_event = (
            "task.stopped"
            if task.status == GenerationTaskStatus.STOPPED.value
            else "task.stop_requested"
        )
        await self.events.publish(
            task_id,
            terminal_event,
            {"task_id": str(task_id)},
            graph_mode=task.execution_mode,
            actor_type="api",
            actor_id="task_service",
            status=task.status,
        )
        raw_context = await self.redis.get(f"chat:task_context:{task_id}")
        if raw_context:
            context = json.loads(raw_context)
            dify_task_id = (
                dify_task_id
                or context.get("external_task_id")
                or context.get("dify_task_id")
            )
        if dify_task_id:
            await self.agent_client.stop(dify_task_id, user_token)
        if release_concurrency:
            lease_id = json.loads(raw_context).get("lease_id") if raw_context else None
            await self.concurrency.release(task.conversation_id, user_token, lease_id)
        return task

    async def select_entity(
        self, task_id: UUID, user_token: str, candidate_id: str
    ) -> GenerationTask:
        async with self.session_factory() as session:
            task = await self.task_repo.get_owned(session, task_id, user_token)
            if task is None:
                raise NotFoundError("TASK_NOT_FOUND", "任务不存在")
            conversation_id = task.conversation_id

        lease_id = await self.concurrency.acquire(conversation_id, user_token)
        outbox_event_id: UUID | None = None
        try:
            async with self.session_factory() as session, session.begin():
                task = await self.task_repo.get_owned(
                    session, task_id, user_token, for_update=True
                )
                if task is None:
                    raise NotFoundError("TASK_NOT_FOUND", "任务不存在")
                if task.status != GenerationTaskStatus.WAITING_SELECTION.value:
                    raise ConflictError(
                        "ENTITY_SELECTION_INVALID", "任务不处于待实体选择状态"
                    )
                pending = await session.scalar(
                    select(PendingEntitySelection)
                    .where(PendingEntitySelection.task_id == task.id)
                    .with_for_update()
                )
                if pending is None or pending.status != "PENDING":
                    raise ConflictError("ENTITY_SELECTION_INVALID", "待选候选不存在")
                if pending.expires_at <= datetime.now(UTC):
                    raise ConflictError("ENTITY_SELECTION_EXPIRED", "候选已过期")

                selected: dict | None = None
                matching_candidates = []
                for candidate in pending.candidates or []:
                    if not isinstance(candidate, dict):
                        continue
                    cid = candidate.get("candidate_id") or build_candidate_id(candidate)
                    if candidate_id in {str(cid), build_candidate_id(candidate)}:
                        matching_candidates.append(dict(candidate))
                if len(matching_candidates) > 1:
                    raise ConflictError("ENTITY_SELECTION_AMBIGUOUS", "该选择编号对应多个测点，请刷新候选后重新选择。")
                if matching_candidates:
                    selected = matching_candidates[0]
                    selected["candidate_id"] = build_candidate_id(selected)
                if selected is None:
                    raise ConflictError("ENTITY_SELECTION_INVALID", "候选 ID 不合法")
                from app.services.sensor_references import code_tokens, matches_tokens
                literal = code_tokens(pending.original_query)
                if literal and not matches_tokens({"equip_num": selected.get("equip_no"),
                    "point_num": selected.get("point_no")}, literal):
                    raise ConflictError("ENTITY_SELECTION_TARGET_MISMATCH", "该候选与原问题的编码不一致，请重新查询原设备或测点。")

                key = f"chat:task_context:{task_id}"
                raw = await self.redis.get(key)
                if not raw:
                    raise ConflictError("TASK_CONTEXT_EXPIRED", "任务临时上下文已过期")
                context = json.loads(raw)
                context["lease_id"] = lease_id
                context["selected_entity"] = selected
                context["selection_resume"] = True
                context["graph_attempt"] = int(context.get("graph_attempt") or 0) + 1
                await self.redis.setex(
                    key,
                    self.settings.task_context_ttl_seconds,
                    json.dumps(context, ensure_ascii=False, separators=(",", ":")),
                )
                await self.redis.delete(f"chat:cancel:{task_id}")

                pending.selected_entity = selected
                pending.status = "SELECTED"
                task.status = GenerationTaskStatus.QUEUED.value
                task.worker_id = None
                task.heartbeat_at = None
                task.completed_at = None
                task.error_code = None
                task.error_message = None

                branch = await session.get(
                    ConversationBranch, task.branch_id, with_for_update=True
                )
                if branch is None:
                    raise ConflictError("ENTITY_SELECTION_STALE", "任务所在会话分支已不存在")
                # Resume is valid only while this task's assistant message is still the
                # active leaf.  This is a server-side fence against stale selections
                # from another browser or from a race created before the pending-turn
                # guard was installed.
                if branch.active_leaf_message_id != task.assistant_message_id:
                    raise ConflictError(
                        "ENTITY_SELECTION_STALE",
                        "该实体选择对应的任务已不是当前会话最新任务，请刷新会话后重试。",
                    )
                branch.active_entity = selected

                user_message = await session.get(
                    Message, task.user_message_id, with_for_update=True
                )
                if user_message is not None:
                    previous = dict(user_message.entity_result or {})
                    user_message.entity_result = build_selected_entity_result(
                        previous,
                        selected,
                    )

                if task.assistant_message_id:
                    assistant = await session.get(
                        Message, task.assistant_message_id, with_for_update=True
                    )
                    if assistant is not None:
                        assistant.content = ""
                        assistant.status = MessageStatus.PENDING.value

                if self.settings.outbox_enabled:
                    event = self.outbox.add(
                        session,
                        conversation_id=task.conversation_id,
                        aggregate_type="generation_task",
                        aggregate_id=task.id,
                        event_type="generation.enqueue",
                        payload={
                            "task_id": str(task.id),
                            "reason": "entity_selected",
                        },
                        deduplication_key=(
                            f"generation.enqueue:{task.id}:entity_selected:"
                            f"{context['graph_attempt']}"
                        ),
                    )
                    outbox_event_id = event.id
                await session.flush()

            await self.events.publish(
                task_id,
                "entity.selection.completed",
                {
                    "task_id": str(task_id),
                    "candidate_id": candidate_id,
                    "resolved_entity": selected,
                },
                graph_mode=task.execution_mode,
                stage="entity_selection",
                actor_type="user",
                actor_id="chat_ui",
                status="COMPLETED",
                output_payload={"resolved_entity": selected},
            )

            if self.settings.outbox_enabled:
                if self.settings.outbox_fast_publish_enabled and outbox_event_id:
                    await self.outbox.publish_fast(outbox_event_id)
            else:
                await self.queue.enqueue_generation(task_id)
                await self.events.publish(
                    task_id,
                    "task.queued",
                    {"task_id": str(task_id), "reason": "entity_selected"},
                    graph_mode=task.execution_mode,
                    actor_type="api",
                    actor_id="task_service",
                    status="QUEUED",
                )
            return task
        except Exception:
            await self.concurrency.release(conversation_id, user_token, lease_id)
            raise
