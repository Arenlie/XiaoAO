from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

import httpx
from redis.asyncio import Redis

from app.config import Settings
from app.domain.exceptions import AppError
from app.integrations.dify.entity_parser import parse_workflow_response
from app.integrations.dify.entity_profile import serialize_entity_workflow_profile
from app.schemas.entity import EntityLookupResult
from app.telemetry import set_attributes, tracer

_entity_tracer = tracer(__name__)


class EntityWorkflowClient:
    def __init__(self, client: httpx.AsyncClient, redis: Redis, settings: Settings) -> None:
        self.client = client
        self.redis = redis
        self.settings = settings

    async def lookup(
        self,
        query: str,
        user_token: str,
        user_profile: Mapping[str, Any] | None = None,
        active_entity: Mapping[str, Any] | None = None,
    ) -> EntityLookupResult:
        profile_json = serialize_entity_workflow_profile(
            user_profile,
            max_json_characters=self.settings.entity_profile_max_characters,
        )
        profile_prior_available = profile_json != "{}"
        active_entity_json = json.dumps(
            dict(active_entity or {}),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        cache_key = ""
        if self.settings.entity_lookup_cache_ttl_seconds > 0:
            digest = hashlib.sha256(
                f"{user_token}\0{query.strip()}\0{profile_json}\0{active_entity_json}".encode("utf-8")
            ).hexdigest()
            cache_key = f"chat:entity-cache:{digest}"
            cached = await self.redis.get(cache_key)
            if cached:
                return EntityLookupResult.model_validate_json(cached)

        with _entity_tracer.start_as_current_span("dify.entity_workflow") as span:
            set_attributes(
                span,
                {
                    "enduser.id": user_token,
                    "dify.workflow.profile_prior_available": profile_prior_available,
                    "dify.workflow.query_length": len(query),
                },
            )
            try:
                response = await self.client.post(
                    self.settings.entity_workflow_url,
                    headers={
                        "Authorization": f"Bearer {self.settings.entity_workflow_api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "inputs": {
                            "query": query,
                            self.settings.entity_profile_input_name: profile_json,
                            "active_entity": active_entity_json,
                        },
                        "response_mode": "blocking",
                        "user": user_token,
                    },
                    timeout=httpx.Timeout(
                        connect=self.settings.entity_connect_timeout_seconds,
                        read=self.settings.entity_workflow_timeout_seconds,
                        write=10.0,
                        pool=2.0,
                    ),
                )
                response.raise_for_status()
            except httpx.TimeoutException as exc:
                span.record_exception(exc)
                raise AppError("ENTITY_LOOKUP_TIMEOUT", "实体检索超时", 504) from exc
            except httpx.HTTPError as exc:
                span.record_exception(exc)
                raise AppError("ENTITY_LOOKUP_FAILED", f"实体检索调用失败: {exc}", 502) from exc

            try:
                payload = response.json()
            except ValueError as exc:
                span.record_exception(exc)
                raise AppError("ENTITY_RESULT_INVALID", "实体工作流返回非 JSON 数据", 502) from exc
            result = parse_workflow_response(
                payload, profile_prior_available=profile_prior_available
            )
            set_attributes(
                span,
                {
                    "dify.workflow.status": result.status.value,
                    "dify.workflow.match_count": result.match_count,
                    "dify.workflow.top_similarity": result.top_similarity,
                },
            )

        if cache_key:
            await self.redis.setex(
                cache_key,
                self.settings.entity_lookup_cache_ttl_seconds,
                result.model_dump_json(),
            )
        return result
