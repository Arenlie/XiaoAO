from __future__ import annotations

import math
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import structlog
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.domain.enums import GenerationTaskStatus, MessageStatus
from app.metrics import OUTBOX_EVENTS, OUTBOX_PUBLISH_SECONDS
from app.models.conversation import Conversation
from app.models.generation_task import GenerationTask
from app.models.message import Message
from app.models.outbox_event import OutboxEvent
from app.repositories.outbox_repository import OutboxRepository
from app.services.concurrency_service import ConcurrencyService
from app.services.event_service import EventService
from app.services.queue_service import QueueService
from app.telemetry import inject_trace_context, tracer, use_extracted_context

log = structlog.get_logger(__name__)
_outbox_tracer = tracer(__name__)


class RetryableOutboxError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ClaimedOutboxEvent:
    id: UUID
    conversation_id: UUID
    aggregate_type: str
    aggregate_id: UUID | None
    event_type: str
    payload: dict[str, Any]
    trace_context: dict[str, str]
    attempt_count: int

    @classmethod
    def from_model(cls, event: OutboxEvent) -> "ClaimedOutboxEvent":
        return cls(
            id=event.id,
            conversation_id=event.conversation_id,
            aggregate_type=event.aggregate_type,
            aggregate_id=event.aggregate_id,
            event_type=event.event_type,
            payload=dict(event.payload or {}),
            trace_context={str(k): str(v) for k, v in (event.trace_context or {}).items()},
            attempt_count=event.attempt_count,
        )


class OutboxService:
    def __init__(
        self,
        *,
        session_factory,
        redis: Redis,
        settings: Settings,
        queue: QueueService,
        events: EventService,
        repository: OutboxRepository,
        concurrency: ConcurrencyService,
    ) -> None:
        self.session_factory = session_factory
        self.redis = redis
        self.settings = settings
        self.queue = queue
        self.events = events
        self.repository = repository
        self.concurrency = concurrency

    def add(
        self,
        session: AsyncSession,
        *,
        conversation_id: UUID,
        aggregate_type: str,
        aggregate_id: UUID | None,
        event_type: str,
        payload: dict[str, Any],
        deduplication_key: str,
    ) -> OutboxEvent:
        trace_context = inject_trace_context({})
        event = OutboxEvent(
            id=uuid4(),
            conversation_id=conversation_id,
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            event_type=event_type,
            payload=payload,
            trace_context=trace_context,
            deduplication_key=deduplication_key,
            status="PENDING",
            available_at=datetime.now(UTC),
        )
        session.add(event)
        return event

    async def publish_fast(self, event_id: UUID, *, worker_id: str = "api-fast-path") -> bool:
        """Best-effort immediate delivery after commit.

        Failure never invalidates the already committed API operation; the relay
        will retry the same outbox row.
        """
        try:
            claimed = await self._claim_one(event_id, worker_id)
            if claimed is None:
                return False
            await self._publish_claimed(claimed)
            return True
        except Exception:
            log.exception("outbox_fast_publish_failed", event_id=str(event_id))
            return False

    async def claim_batch(self, worker_id: str) -> list[ClaimedOutboxEvent]:
        async with self.session_factory() as session, session.begin():
            rows = await self.repository.claim_batch(
                session,
                worker_id=worker_id,
                batch_size=self.settings.outbox_batch_size,
                lock_timeout_seconds=self.settings.outbox_lock_timeout_seconds,
            )
            return [ClaimedOutboxEvent.from_model(row) for row in rows]

    async def publish_claimed(self, event: ClaimedOutboxEvent) -> None:
        await self._publish_claimed(event)

    async def _claim_one(
        self, event_id: UUID, worker_id: str
    ) -> ClaimedOutboxEvent | None:
        async with self.session_factory() as session, session.begin():
            event = await self.repository.claim_one(
                session,
                event_id,
                worker_id=worker_id,
                lock_timeout_seconds=self.settings.outbox_lock_timeout_seconds,
            )
            return ClaimedOutboxEvent.from_model(event) if event else None

    async def _publish_claimed(self, event: ClaimedOutboxEvent) -> None:
        started = time.perf_counter()
        with use_extracted_context(event.trace_context):
            with _outbox_tracer.start_as_current_span("outbox.publish") as span:
                span.set_attribute("messaging.system", "redis")
                span.set_attribute("messaging.operation", "publish")
                span.set_attribute("outbox.event_id", str(event.id))
                span.set_attribute("outbox.event_type", event.event_type)
                span.set_attribute("conversation.id", str(event.conversation_id))
                try:
                    await self._dispatch(event)
                except Exception as exc:
                    span.record_exception(exc)
                    terminal = event.attempt_count >= self.settings.outbox_max_attempts
                    OUTBOX_EVENTS.labels(
                        event_type=event.event_type,
                        result="failed" if terminal else "retry",
                    ).inc()
                    await self._mark_failure(event, exc)
                    raise
                else:
                    await self._mark_published(event.id)
                    OUTBOX_EVENTS.labels(
                        event_type=event.event_type,
                        result="published",
                    ).inc()
                finally:
                    OUTBOX_PUBLISH_SECONDS.labels(
                        event_type=event.event_type
                    ).observe(time.perf_counter() - started)

    async def _dispatch(self, event: ClaimedOutboxEvent) -> None:
        trace_context = inject_trace_context({})
        payload = event.payload
        if event.event_type == "generation.enqueue":
            task_id = UUID(str(payload["task_id"]))
            context_key = f"chat:task_context:{task_id}"
            if not await self.redis.exists(context_key):
                raise RetryableOutboxError("generation task context is not available yet")
            await self.queue.enqueue_generation(
                task_id,
                outbox_id=event.id,
                trace_context=trace_context,
            )
            await self.events.publish(
                task_id,
                "task.queued",
                {
                    "task_id": str(task_id),
                    "conversation_id": str(event.conversation_id),
                    "reason": payload.get("reason", "submitted"),
                    "outbox_id": str(event.id),
                },
            )
            return
        if event.event_type == "title.enqueue":
            await self.queue.enqueue_title(
                payload["conversation_id"],
                payload["user_token"],
                payload["query"],
                outbox_id=event.id,
                trace_context=trace_context,
            )
            return
        if event.event_type == "summary.enqueue":
            await self.queue.enqueue_summary(
                payload["branch_id"],
                outbox_id=event.id,
                trace_context=trace_context,
            )
            return
        if event.event_type == "dify_cleanup.enqueue":
            await self.queue.enqueue_dify_cleanup(
                payload["conversation_id"],
                payload["user_token"],
                list(payload["dify_conversation_ids"]),
                outbox_id=event.id,
                trace_context=trace_context,
            )
            return
        raise RuntimeError(f"unsupported outbox event type: {event.event_type}")

    async def _mark_published(self, event_id: UUID) -> None:
        async with self.session_factory() as session, session.begin():
            event = await self.repository.get(session, event_id)
            if event is None:
                return
            event.status = "PUBLISHED"
            event.published_at = datetime.now(UTC)
            event.locked_at = None
            event.locked_by = None
            event.last_error = None

    async def _mark_failure(self, claimed: ClaimedOutboxEvent, exc: Exception) -> None:
        terminal = claimed.attempt_count >= self.settings.outbox_max_attempts
        delay = min(
            self.settings.outbox_max_retry_seconds,
            self.settings.outbox_base_retry_seconds
            * math.pow(2, max(0, claimed.attempt_count - 1)),
        )
        async with self.session_factory() as session, session.begin():
            event = await self.repository.get(session, claimed.id)
            if event is None:
                return
            event.last_error = str(exc)[:4000]
            event.locked_at = None
            event.locked_by = None
            if terminal:
                event.status = "FAILED"
            else:
                event.status = "PENDING"
                event.available_at = datetime.now(UTC) + timedelta(seconds=delay)
        if terminal and claimed.event_type == "generation.enqueue":
            await self._fail_generation_task(
                UUID(str(claimed.payload["task_id"])),
                "OUTBOX_PUBLISH_FAILED",
                f"任务投递 Redis 失败，已重试 {claimed.attempt_count} 次：{exc}",
            )

    async def _fail_generation_task(self, task_id: UUID, code: str, message: str) -> None:
        conversation_id: UUID | None = None
        user_token: str | None = None
        async with self.session_factory() as session, session.begin():
            task = await session.get(GenerationTask, task_id, with_for_update=True)
            if task is None or task.status in {"COMPLETED", "FAILED", "STOPPED", "TIMEOUT"}:
                return
            task.status = GenerationTaskStatus.FAILED.value
            task.error_code = code
            task.error_message = message[:4000]
            task.completed_at = datetime.now(UTC)
            conversation_id = task.conversation_id
            conversation = await session.get(Conversation, task.conversation_id)
            user_token = conversation.user_token if conversation else None
            if task.assistant_message_id:
                assistant = await session.get(
                    Message, task.assistant_message_id, with_for_update=True
                )
                if assistant:
                    assistant.status = MessageStatus.FAILED.value
        try:
            await self.events.publish(
                task_id, "task.failed", {"code": code, "message": message}
            )
        except Exception:
            log.exception("outbox_terminal_failure_event_failed", task_id=str(task_id))
        if conversation_id and user_token:
            await self.concurrency.release(conversation_id, user_token)
