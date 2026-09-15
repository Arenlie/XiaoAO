from __future__ import annotations

import json
from collections.abc import Mapping
from uuid import UUID

from redis.asyncio import Redis


class QueueService:
    GENERATION_QUEUE = "chat:generation:queue"
    GENERATION_GROUP = "chat:generation:consumer-group"
    TITLE_QUEUE = "chat:title:queue"
    TITLE_GROUP = "chat:title:consumer-group"
    SUMMARY_QUEUE = "chat:summary:queue"
    SUMMARY_GROUP = "chat:summary:consumer-group"
    DIFY_CLEANUP_QUEUE = "chat:dify-cleanup:queue"
    DIFY_CLEANUP_GROUP = "chat:dify-cleanup:consumer-group"

    def __init__(self, redis: Redis) -> None:
        self.redis = redis

    async def ensure_groups(self) -> None:
        for stream, group in (
            (self.GENERATION_QUEUE, self.GENERATION_GROUP),
            (self.TITLE_QUEUE, self.TITLE_GROUP),
            (self.SUMMARY_QUEUE, self.SUMMARY_GROUP),
            (self.DIFY_CLEANUP_QUEUE, self.DIFY_CLEANUP_GROUP),
        ):
            try:
                await self.redis.xgroup_create(stream, group, id="0-0", mkstream=True)
            except Exception as exc:
                if "BUSYGROUP" not in str(exc):
                    raise

    @staticmethod
    def _trace_fields(trace_context: Mapping[str, str] | None) -> dict[str, str]:
        return {
            f"trace_{key}": str(value)
            for key, value in (trace_context or {}).items()
            if value
        }

    @staticmethod
    def trace_context_from_fields(fields: Mapping[str, str]) -> dict[str, str]:
        return {
            key.removeprefix("trace_"): str(value)
            for key, value in fields.items()
            if key.startswith("trace_") and value
        }

    async def enqueue_generation(
        self,
        task_id: UUID | str,
        *,
        outbox_id: UUID | str | None = None,
        trace_context: Mapping[str, str] | None = None,
    ) -> str:
        fields = {
            "task_id": str(task_id),
            **self._trace_fields(trace_context),
        }
        if outbox_id:
            fields["outbox_id"] = str(outbox_id)
        return str(
            await self.redis.xadd(
                self.GENERATION_QUEUE,
                fields,
                maxlen=100000,
                approximate=True,
            )
        )

    async def enqueue_title(
        self,
        conversation_id: UUID | str,
        user_token: str,
        query: str,
        *,
        outbox_id: UUID | str | None = None,
        trace_context: Mapping[str, str] | None = None,
    ) -> str:
        fields = {
            "conversation_id": str(conversation_id),
            "user_token": user_token,
            "query": query,
            **self._trace_fields(trace_context),
        }
        if outbox_id:
            fields["outbox_id"] = str(outbox_id)
        return str(
            await self.redis.xadd(
                self.TITLE_QUEUE,
                fields,
                maxlen=10000,
                approximate=True,
            )
        )

    async def enqueue_summary(
        self,
        branch_id: UUID | str,
        *,
        outbox_id: UUID | str | None = None,
        trace_context: Mapping[str, str] | None = None,
    ) -> str:
        fields = {
            "branch_id": str(branch_id),
            **self._trace_fields(trace_context),
        }
        if outbox_id:
            fields["outbox_id"] = str(outbox_id)
        return str(
            await self.redis.xadd(
                self.SUMMARY_QUEUE,
                fields,
                maxlen=10000,
                approximate=True,
            )
        )

    async def enqueue_dify_cleanup(
        self,
        conversation_id: UUID | str,
        user_token: str,
        dify_conversation_ids: list[str],
        *,
        outbox_id: UUID | str | None = None,
        trace_context: Mapping[str, str] | None = None,
    ) -> str:
        fields = {
            "conversation_id": str(conversation_id),
            "user_token": user_token,
            "dify_conversation_ids": json.dumps(dify_conversation_ids),
            **self._trace_fields(trace_context),
        }
        if outbox_id:
            fields["outbox_id"] = str(outbox_id)
        return str(
            await self.redis.xadd(
                self.DIFY_CLEANUP_QUEUE,
                fields,
                maxlen=10000,
                approximate=True,
            )
        )
