from __future__ import annotations

import asyncio
import os
import socket
from uuid import UUID, uuid4

import structlog
from sqlalchemy import desc, select

from app.config import Settings
from app.domain.enums import TitleSource
from app.integrations.dify.title_client import TitleClient
from app.models.conversation import Conversation
from app.models.generation_task import GenerationTask
from app.services.event_service import EventService
from app.services.queue_service import QueueService
from app.telemetry import set_attributes, tracer, use_extracted_context

log = structlog.get_logger(__name__)
_title_tracer = tracer(__name__)


class TitleWorker:
    def __init__(
        self,
        session_factory,
        redis,
        queue: QueueService,
        events: EventService,
        client: TitleClient,
        settings: Settings,
    ) -> None:
        self.session_factory = session_factory
        self.redis = redis
        self.queue = queue
        self.events = events
        self.client = client
        self.settings = settings
        self.consumer = f"title-{socket.gethostname()}-{os.getpid()}-{uuid4().hex[:6]}"
        self._stopping = asyncio.Event()

    async def run(self) -> None:
        await self.queue.ensure_groups()
        while not self._stopping.is_set():
            try:
                result = await self.redis.xreadgroup(
                    groupname=self.queue.TITLE_GROUP,
                    consumername=self.consumer,
                    streams={self.queue.TITLE_QUEUE: ">"},
                    count=4,
                    block=self.settings.redis_stream_block_ms,
                )
                for _, records in result or []:
                    for stream_id, fields in records:
                        trace_context = self.queue.trace_context_from_fields(fields)
                        with use_extracted_context(trace_context):
                            with _title_tracer.start_as_current_span(
                                "title.consume"
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
                                    await self._process(fields)
                                finally:
                                    await self.redis.xack(
                                        self.queue.TITLE_QUEUE,
                                        self.queue.TITLE_GROUP,
                                        stream_id,
                                    )
            except asyncio.CancelledError:
                break
            except Exception:
                log.exception("title_worker_failed")
                await asyncio.sleep(1)

    async def stop(self) -> None:
        self._stopping.set()

    async def _process(self, fields: dict[str, str]) -> None:
        conversation_id = UUID(fields["conversation_id"])
        title = await self.client.generate(fields["query"], fields["user_token"])
        if not title:
            return
        async with self.session_factory() as session, session.begin():
            conversation = await session.get(
                Conversation, conversation_id, with_for_update=True
            )
            if (
                conversation is None
                or conversation.title_source == TitleSource.MANUAL.value
            ):
                return
            conversation.title = title[:100]
            conversation.title_source = TitleSource.AUTO_LLM.value
        async with self.session_factory() as session:
            task_id = await session.scalar(
                select(GenerationTask.id)
                .where(GenerationTask.conversation_id == conversation_id)
                .order_by(desc(GenerationTask.created_at))
                .limit(1)
            )
        if task_id:
            await self.events.publish(
                task_id,
                "conversation.title.updated",
                {"conversation_id": str(conversation_id), "title": title[:100]},
            )
