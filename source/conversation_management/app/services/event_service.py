from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

if TYPE_CHECKING:
    from redis.asyncio import Redis
else:
    Redis = Any

from sqlalchemy import select
from app.config import Settings
from app.integrations.dify.sensitive_data import sanitize_event_payload
from app.models.execution_event import TaskExecutionEvent
from app.models.generation_task import GenerationTask
from app.repositories.event_repository import EventRepository
from app.output.progress import progress_payload

_TASK_TERMINALS = {"task.completed", "task.failed", "task.stopped", "task.waiting_input"}
_PAUSES = {"entity.selection.required", "diagnosis.mode.required"}
_OPTIONAL_SPANS = {"supervisor.entity_intake.llm", "asset.resolve.prefetch"}


class EventService:
    def __init__(
        self,
        redis: Redis,
        settings: Settings,
        session_factory,
        repository: EventRepository,
    ) -> None:
        self.redis = redis
        self.settings = settings
        self.session_factory = session_factory
        self.repository = repository

    @staticmethod
    def stream_key(task_id: UUID | str) -> str:
        return f"chat:events:{task_id}"

    @staticmethod
    def _cursor_value(cursor: str | None) -> int:
        value = str(cursor or "0").split("-", 1)[0]
        try:
            return max(0, int(value))
        except ValueError:
            return 0

    async def publish(
        self,
        task_id: UUID | str,
        event: str,
        data: dict[str, Any],
        *,
        message_id: UUID | None = None,
        graph_mode: str | None = None,
        stage: str | None = None,
        actor_type: str = "system",
        actor_id: str | None = None,
        status: str | None = None,
        attempt: int = 1,
        span_id: str | None = None,
        parent_span_id: str | None = None,
        input_payload: dict[str, Any] | None = None,
        output_payload: dict[str, Any] | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        duration_ms: float | None = None,
    ) -> str:
        task_uuid = UUID(str(task_id))
        clean = progress_payload(sanitize_event_payload(data), event)
        if event in {"answer.started", "answer.delta", "answer.reasoning.delta", "answer.completed"} and isinstance(data.get("content"), str):
            # Public answer text is already handled by the customer renderer. Keep
            # its string type and exact length: process-log truncation or implicit
            # JSON parsing would corrupt stream replay and the final replacement.
            clean["content"] = data["content"]
        async with self.session_factory() as session, session.begin():
            task = await session.get(GenerationTask, task_uuid, with_for_update=True)
            if task is None:
                raise RuntimeError(f"generation task does not exist: {task_id}")
            # End records and the task boundary commit atomically, in that order.
            # A finally-block after task.completed is too late for a live SSE reader.
            if event in _TASK_TERMINALS or (event in _PAUSES and task.status in {
                "WAITING_SELECTION", "WAITING_CONFIRMATION"
            }):
                await self._close_open_spans_locked(session, task)
            if event in {"performance.span.completed", "performance.span.failed"} and (span_id or clean.get("span_id")):
                existing = await session.scalar(select(TaskExecutionEvent).where(
                    TaskExecutionEvent.task_id == task_uuid,
                    TaskExecutionEvent.span_id == (span_id or clean.get("span_id")),
                    TaskExecutionEvent.event_type.in_(["performance.span.completed", "performance.span.failed"])).limit(1))
                if existing is not None:
                    return f"{existing.sequence_no}-0"
            task.event_sequence = int(task.event_sequence or 0) + 1
            sequence_no = task.event_sequence
            row = TaskExecutionEvent(
                sequence_no=sequence_no,
                task_id=task_uuid,
                message_id=message_id or task.assistant_message_id,
                graph_mode=graph_mode or task.execution_mode or "normal",
                span_id=span_id or clean.get("span_id"),
                parent_span_id=parent_span_id or clean.get("parent_span_id"),
                event_type=event,
                stage=stage,
                actor_type=actor_type,
                actor_id=actor_id,
                status=status,
                attempt=max(1, int(attempt)),
                input_payload=sanitize_event_payload(input_payload or {}),
                output_payload=sanitize_event_payload(output_payload or {}),
                payload=clean,
                error_code=error_code,
                error_message=error_message,
                duration_ms=duration_ms,
            )
            await self.repository.add(session, row)
        key = self.stream_key(task_uuid)
        try:
            await self.redis.xadd(
                key,
                {
                    "event": event,
                    "data": json.dumps(clean, ensure_ascii=False, separators=(",", ":")),
                    "ts": datetime.now(UTC).isoformat(),
                },
                id=f"{sequence_no}-0",
                maxlen=self.settings.task_event_maxlen,
                approximate=True,
            )
            await self.redis.expire(key, self.settings.task_event_ttl_seconds)
        except Exception:
            # PostgreSQL is the source of truth. SSE reconnect can recover this event.
            pass
        return f"{sequence_no}-0"

    @staticmethod
    def _stream_payload(row: TaskExecutionEvent) -> dict[str, Any]:
        payload = progress_payload(row.payload, row.event_type)
        payload.update(
            {
                "sequence_no": row.sequence_no,
                "task_id": str(row.task_id),
                "message_id": str(row.message_id) if row.message_id else None,
                "graph_mode": row.graph_mode,
                "span_id": row.span_id,
                "parent_span_id": row.parent_span_id,
                "stage": row.stage,
                "actor_type": row.actor_type,
                "actor_id": row.actor_id,
                "status": row.status,
                "attempt": row.attempt,
                "input_payload": dict(row.input_payload or {}),
                "output_payload": dict(row.output_payload or {}),
                "error_code": row.error_code,
                "error_message": row.error_message,
                "duration_ms": row.duration_ms,
                "created_at": row.created_at.isoformat(),
            }
        )
        return payload

    async def read_batch(
        self,
        task_id: UUID | str,
        last_event_id: str,
        *,
        block_ms: int | None = None,
        count: int = 100,
    ) -> list[tuple[str, str, dict[str, Any]]]:
        task_uuid = UUID(str(task_id))
        after_sequence = self._cursor_value(last_event_id)
        async with self.session_factory() as session:
            rows = await self.repository.list_task(
                session, task_uuid, after_sequence=after_sequence, limit=count
            )
        if rows:
            return [
                (f"{row.sequence_no}-0", row.event_type, self._stream_payload(row))
                for row in rows
            ]
        if block_ms == 0:
            return []
        key = self.stream_key(task_uuid)
        result = await self.redis.xread(
            streams={key: last_event_id or "0-0"},
            count=count,
            block=max(1, int(block_ms or self.settings.sse_xread_block_ms)),
        )
        if not result:
            return []

        # Redis only wakes blocked SSE readers. Query PostgreSQL again so events are
        # returned in the task-local committed sequence even when concurrent agents finish
        # in a different order.
        async with self.session_factory() as session:
            rows = await self.repository.list_task(
                session, task_uuid, after_sequence=after_sequence, limit=count
            )
        return [
            (f"{row.sequence_no}-0", row.event_type, self._stream_payload(row))
            for row in rows
        ]

    async def read(
        self, task_id: UUID | str, last_event_id: str
    ) -> AsyncIterator[tuple[str, str, dict[str, Any]]]:
        cursor = last_event_id or "0-0"
        while True:
            rows = await self.read_batch(task_id, cursor)
            if not rows:
                yield "", "heartbeat", {
                    "time": datetime.now(UTC).isoformat(),
                    "task_id": str(task_id),
                }
                continue
            for event_id, event, payload in rows:
                cursor = event_id
                yield event_id, event, payload

    async def close_open_spans(self, task_id):
        async with self.session_factory() as session, session.begin():
            task = await session.get(GenerationTask, UUID(str(task_id)), with_for_update=True)
            if task is None:
                return
            await self._close_open_spans_locked(session, task)

    async def _close_open_spans_locked(self, session, task):
        rows = list((await session.scalars(select(TaskExecutionEvent).where(
            TaskExecutionEvent.task_id == task.id,
            TaskExecutionEvent.event_type.in_(["performance.span.started", "performance.span.completed", "performance.span.failed"])
            ).order_by(TaskExecutionEvent.sequence_no))).all())
        ended = {r.span_id for r in rows if r.event_type != "performance.span.started"}
        for row in rows:
            if row.event_type != "performance.span.started" or not row.span_id or row.span_id in ended:
                continue
            optional = row.stage in _OPTIONAL_SPANS or (row.payload or {}).get("code") in _OPTIONAL_SPANS
            # Recovery proves termination, not successful execution of missing work.
            status = "FAILED" if task.status in {"FAILED", "TIMEOUT"} and not optional else "CANCELLED"
            data = {**dict(row.payload or {}), "finish_reason": "worker_finished", "recovered": True,
                    "optional": optional, "affects_task": not optional}
            now = datetime.now(UTC)
            started = row.created_at.replace(tzinfo=UTC) if row.created_at.tzinfo is None else row.created_at
            task.event_sequence = int(task.event_sequence or 0) + 1
            await self.repository.add(session, TaskExecutionEvent(
                task_id=task.id, message_id=row.message_id or task.assistant_message_id,
                sequence_no=task.event_sequence, event_type="performance.span.failed" if status == "FAILED" else "performance.span.completed",
                status=status, span_id=row.span_id, parent_span_id=row.parent_span_id,
                graph_mode=row.graph_mode, stage=row.stage, actor_type="performance", actor_id=row.actor_id,
                payload=data, duration_ms=max(0.0, (now-started).total_seconds()*1000)))
            ended.add(row.span_id)

    async def task_event_page(self, task_id, after_sequence=0, limit=5000):
        async with self.session_factory() as session:
            rows = await self.repository.list_task(session, task_id, after_sequence=after_sequence, limit=limit+1)
        return rows[:limit], len(rows)>limit

    async def list_task_events(self, task_id: UUID) -> list[TaskExecutionEvent]:
        async with self.session_factory() as session:
            return await self.repository.list_task(session, task_id, after_sequence=0, limit=5000)

    async def list_message_events(self, message_id: UUID, *, after_sequence=0, limit=2000) -> list[TaskExecutionEvent]:
        async with self.session_factory() as session:
            return await self.repository.list_message(session, message_id, after_sequence=after_sequence, limit=limit)

    @staticmethod
    def encode_sse(event_id: str, event: str, data: dict[str, Any]) -> bytes:
        lines: list[str] = []
        if event_id:
            lines.append(f"id: {event_id}")
        lines.append(f"event: {event}")
        payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        for line in payload.splitlines() or [""]:
            lines.append(f"data: {line}")
        return ("\n".join(lines) + "\n\n").encode("utf-8")

    @staticmethod
    def encode_retry(milliseconds: int) -> bytes:
        return f"retry: {max(250, int(milliseconds))}\n\n".encode("utf-8")
