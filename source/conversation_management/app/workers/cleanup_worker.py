from __future__ import annotations

import asyncio
import json
import os
import socket
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import structlog
from sqlalchemy import delete, select

from app.config import Settings
from app.attachments.service import AttachmentService
from app.models.attachment import Attachment
from app.models.conversation import Conversation
from app.services.queue_service import QueueService
from app.telemetry import set_attributes, tracer, use_extracted_context

log = structlog.get_logger(__name__)
_cleanup_tracer = tracer(__name__)


class CleanupWorker:
    def __init__(
        self,
        session_factory,
        settings: Settings,
        attachment_service: AttachmentService | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.settings = settings
        self.attachment_service = attachment_service
        self.last_attachment_count = 0

    async def run_once(self) -> int:
        now = datetime.now(UTC)
        purge_before = now - timedelta(days=self.settings.deleted_conversation_purge_days)
        storage_rows: list[Attachment] = []
        async with self.session_factory() as session, session.begin():
            purge_condition = (
                ((Conversation.status == "DELETED") & (Conversation.deleted_at < purge_before))
                | (Conversation.expires_at < now)
            )
            conversation_ids = list(
                (await session.scalars(select(Conversation.id).where(purge_condition))).all()
            )
            if self.attachment_service is not None:
                storage_rows.extend(
                    await self.attachment_service.repository.list_for_conversations(
                        session, conversation_ids
                    )
                )
                expired = await self.attachment_service.repository.list_expired_unbound(
                    session, now=now, limit=1000
                )
                storage_rows.extend(expired)
                for row in expired:
                    await session.delete(row)

            result = await session.execute(delete(Conversation).where(purge_condition))
            purged_conversations = int(result.rowcount or 0)

        unique_rows = list({row.id: row for row in storage_rows}.values())
        self.last_attachment_count = len(unique_rows)
        if self.attachment_service is not None and unique_rows:
            await self.attachment_service.delete_storage_rows(unique_rows)
        return purged_conversations


class DifyCleanupWorker:
    def __init__(
        self, redis, queue: QueueService, agent_client, settings: Settings
    ) -> None:
        self.redis = redis
        self.queue = queue
        self.agent_client = agent_client
        self.settings = settings
        self.consumer = (
            f"dify-cleanup-{socket.gethostname()}-{os.getpid()}-{uuid4().hex[:6]}"
        )
        self._stopping = asyncio.Event()

    async def run(self) -> None:
        await self.queue.ensure_groups()
        while not self._stopping.is_set():
            try:
                result = await self.redis.xreadgroup(
                    groupname=self.queue.DIFY_CLEANUP_GROUP,
                    consumername=self.consumer,
                    streams={self.queue.DIFY_CLEANUP_QUEUE: ">"},
                    count=10,
                    block=self.settings.redis_stream_block_ms,
                )
                for _, records in result or []:
                    for stream_id, fields in records:
                        trace_context = self.queue.trace_context_from_fields(fields)
                        with use_extracted_context(trace_context):
                            with _cleanup_tracer.start_as_current_span(
                                "dify_cleanup.consume"
                            ) as span:
                                set_attributes(
                                    span,
                                    {
                                        "conversation.id": fields.get(
                                            "conversation_id"
                                        ),
                                        "outbox.event_id": fields.get("outbox_id"),
                                    },
                                )
                                try:
                                    ids = json.loads(
                                        fields.get("dify_conversation_ids", "[]")
                                    )
                                    for dify_id in ids:
                                        await self.agent_client.delete_conversation(
                                            str(dify_id), fields["user_token"]
                                        )
                                finally:
                                    await self.redis.xack(
                                        self.queue.DIFY_CLEANUP_QUEUE,
                                        self.queue.DIFY_CLEANUP_GROUP,
                                        stream_id,
                                    )
            except asyncio.CancelledError:
                break
            except Exception:
                log.exception("dify_cleanup_worker_failed")
                await asyncio.sleep(1)

    async def stop(self) -> None:
        self._stopping.set()
