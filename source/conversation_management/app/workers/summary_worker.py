from __future__ import annotations

import asyncio
import os
import socket
from uuid import UUID, uuid4

import structlog
from sqlalchemy import desc, select

from app.config import Settings
from app.models.branch import ConversationBranch
from app.models.context_snapshot import ContextSnapshot
from app.repositories.message_repository import MessageRepository
from app.services.queue_service import QueueService
from app.telemetry import set_attributes, tracer, use_extracted_context

log = structlog.get_logger(__name__)
_summary_tracer = tracer(__name__)


class SummaryWorker:
    """Build deterministic rolling context snapshots outside the TTFT path."""

    def __init__(
        self, session_factory, redis, settings: Settings, queue: QueueService
    ) -> None:
        self.session_factory = session_factory
        self.redis = redis
        self.settings = settings
        self.queue = queue
        self.message_repo = MessageRepository()
        self.consumer = f"summary-{socket.gethostname()}-{os.getpid()}-{uuid4().hex[:6]}"
        self._stopping = asyncio.Event()

    async def run(self) -> None:
        await self.queue.ensure_groups()
        while not self._stopping.is_set():
            try:
                result = await self.redis.xreadgroup(
                    groupname=self.queue.SUMMARY_GROUP,
                    consumername=self.consumer,
                    streams={self.queue.SUMMARY_QUEUE: ">"},
                    count=4,
                    block=self.settings.redis_stream_block_ms,
                )
                for _, records in result or []:
                    for stream_id, fields in records:
                        trace_context = self.queue.trace_context_from_fields(fields)
                        with use_extracted_context(trace_context):
                            with _summary_tracer.start_as_current_span(
                                "summary.consume"
                            ) as span:
                                set_attributes(
                                    span,
                                    {
                                        "conversation.branch_id": fields.get(
                                            "branch_id"
                                        ),
                                        "outbox.event_id": fields.get("outbox_id"),
                                    },
                                )
                                try:
                                    await self._process(UUID(fields["branch_id"]))
                                finally:
                                    await self.redis.xack(
                                        self.queue.SUMMARY_QUEUE,
                                        self.queue.SUMMARY_GROUP,
                                        stream_id,
                                    )
            except asyncio.CancelledError:
                break
            except Exception:
                log.exception("summary_worker_failed")
                await asyncio.sleep(1)

    async def stop(self) -> None:
        self._stopping.set()

    async def _process(self, branch_id: UUID) -> None:
        async with self.session_factory() as session:
            branch = await session.get(ConversationBranch, branch_id)
            if branch is None or branch.active_leaf_message_id is None:
                return
            path = await self.message_repo.path_to_leaf(
                session, branch.active_leaf_message_id, conversation_id=branch.conversation_id
            )
            path = [
                m
                for m in path
                if m.include_in_context and m.status in {"COMPLETED", "STOPPED"}
            ]
            if len(path) < self.settings.summary_trigger_message_count:
                return
            cutoff_index = (
                len(path) - self.settings.context_recent_message_limit - 1
            )
            if cutoff_index < 0:
                return
            cutoff = path[cutoff_index]
            latest = await session.scalar(
                select(ContextSnapshot)
                .where(ContextSnapshot.branch_id == branch_id)
                .order_by(desc(ContextSnapshot.created_at))
                .limit(1)
            )
            start_index = 0
            existing_summary = ""
            if latest is not None:
                existing_summary = latest.summary
                for index, message in enumerate(path):
                    if message.id == latest.up_to_message_id:
                        start_index = index + 1
                        break
                if latest.up_to_message_id == cutoff.id:
                    return
            additions = path[start_index : cutoff_index + 1]
            lines = [
                f"{m.role.lower()}: {m.content.strip()}"
                for m in additions
                if m.content.strip()
            ]
            merged = "\n".join(
                part for part in (existing_summary, *lines) if part
            )
            merged = merged[-self.settings.context_max_characters :]

        async with self.session_factory() as session, session.begin():
            branch = await session.get(
                ConversationBranch, branch_id, with_for_update=True
            )
            if branch is None:
                return
            branch.summary = merged
            session.add(
                ContextSnapshot(
                    conversation_id=branch.conversation_id,
                    branch_id=branch.id,
                    up_to_message_id=cutoff.id,
                    summary=merged,
                    message_count=cutoff_index + 1,
                )
            )
