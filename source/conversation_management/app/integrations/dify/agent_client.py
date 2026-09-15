from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx

from app.config import Settings
from app.domain.exceptions import AppError
from app.telemetry import mark_span_error, set_attributes, tracer

_agent_tracer = tracer(__name__)


class DifyAgentClient:
    def __init__(self, client: httpx.AsyncClient, settings: Settings) -> None:
        self.client = client
        self.settings = settings

    async def stream_chat(
        self,
        *,
        query: str,
        user_token: str,
        data_access_token: str,
        resolved_entity: str,
        active_entity: str,
        memory_context: str,
        user_profile: str,
        conversation_id: str | None,
    ) -> AsyncIterator[dict[str, Any]]:
        request_json = {
            "inputs": {
                "data_access_token": data_access_token,
                "resolved_entity": resolved_entity,
                "active_entity": active_entity,
                "memory_context": memory_context,
                "user_profile": user_profile,
            },
            "query": query,
            "response_mode": "streaming",
            "conversation_id": conversation_id or "",
            "user": user_token,
            "files": [],
        }
        timeout = httpx.Timeout(
            connect=self.settings.agent_connect_timeout_seconds,
            read=self.settings.agent_timeout_seconds,
            write=20.0,
            pool=2.0,
        )
        started = time.perf_counter()
        first_event_seen = False
        event_count = 0
        answer_event_count = 0
        with _agent_tracer.start_as_current_span("dify.agent.stream") as span:
            set_attributes(
                span,
                {
                    "enduser.id": user_token,
                    "dify.conversation.reused": bool(conversation_id),
                    "dify.query.length": len(query),
                    "dify.resolved_entity.present": resolved_entity not in {"", "{}"},
                    "dify.active_entity.present": active_entity not in {"", "{}"},
                },
            )
            try:
                async with self.client.stream(
                    "POST",
                    self.settings.agent_chat_url,
                    headers={
                        "Authorization": f"Bearer {self.settings.agent_api_key}",
                        "Accept": "text/event-stream",
                        "Content-Type": "application/json",
                    },
                    json=request_json,
                    timeout=timeout,
                ) as response:
                    response.raise_for_status()
                    async for line in response.aiter_lines():
                        line = line.strip()
                        if not line or line.startswith(":") or not line.startswith("data:"):
                            continue
                        raw = line[5:].strip()
                        if raw == "[DONE]":
                            break
                        try:
                            payload = json.loads(raw)
                        except json.JSONDecodeError as exc:
                            raise AppError(
                                "AGENT_STREAM_INVALID",
                                "Dify SSE 数据不是合法 JSON",
                                502,
                            ) from exc
                        if not isinstance(payload, dict):
                            continue
                        if not first_event_seen:
                            first_event_seen = True
                            span.set_attribute(
                                "dify.first_event_ms",
                                round((time.perf_counter() - started) * 1000, 2),
                            )
                        event_name = str(payload.get("event") or "unknown")
                        event_count += 1
                        if event_name in {"agent_message", "message", "message_replace"}:
                            answer_event_count += 1
                        if event_name in {
                            "agent_thought",
                            "message_end",
                            "workflow_started",
                            "workflow_finished",
                            "node_started",
                            "node_finished",
                            "error",
                        }:
                            span.add_event("dify.sse.event", {"dify.event": event_name})
                        for key, attribute in (
                            ("task_id", "dify.task_id"),
                            ("message_id", "dify.message_id"),
                            ("conversation_id", "dify.conversation_id"),
                        ):
                            if payload.get(key):
                                span.set_attribute(attribute, str(payload[key]))
                        yield payload
            except httpx.TimeoutException as exc:
                mark_span_error(span, exc)
                raise AppError("AGENT_TIMEOUT", "Dify Agent 调用超时", 504) from exc
            except httpx.HTTPStatusError as exc:
                mark_span_error(span, exc)
                body = f"{exc.response.reason_phrase}"[:1000]
                raise AppError(
                    "AGENT_UNAVAILABLE",
                    f"Dify Agent HTTP {exc.response.status_code}: {body}",
                    502,
                ) from exc
            except httpx.HTTPError as exc:
                mark_span_error(span, exc)
                raise AppError(
                    "AGENT_UNAVAILABLE",
                    f"Dify Agent 连接失败: {exc}",
                    502,
                ) from exc
            except AppError as exc:
                mark_span_error(span, exc)
                raise
            finally:
                span.set_attribute(
                    "dify.stream.duration_ms",
                    round((time.perf_counter() - started) * 1000, 2),
                )
                span.set_attribute("dify.sse.event_count", event_count)
                span.set_attribute("dify.sse.answer_event_count", answer_event_count)

    async def stop(self, dify_task_id: str, user_token: str) -> None:
        with _agent_tracer.start_as_current_span("dify.agent.stop") as span:
            span.set_attribute("dify.task_id", dify_task_id)
            try:
                response = await self.client.post(
                    self.settings.agent_stop_url(dify_task_id),
                    headers={"Authorization": f"Bearer {self.settings.agent_api_key}"},
                    json={"user": user_token},
                    timeout=5.0,
                )
                response.raise_for_status()
            except httpx.HTTPError as exc:
                mark_span_error(span, exc)
                return

    async def delete_conversation(
        self, conversation_id: str, user_token: str
    ) -> None:
        url = f"{self.settings.agent_base_url.rstrip('/')}/conversations/{conversation_id}"
        with _agent_tracer.start_as_current_span("dify.conversation.delete") as span:
            span.set_attribute("dify.conversation_id", conversation_id)
            try:
                response = await self.client.request(
                    "DELETE",
                    url,
                    headers={
                        "Authorization": f"Bearer {self.settings.agent_api_key}",
                        "Content-Type": "application/json",
                    },
                    json={"user": user_token},
                    timeout=10.0,
                )
                if response.status_code not in {204, 404}:
                    response.raise_for_status()
            except httpx.HTTPError as exc:
                mark_span_error(span, exc)
                return
