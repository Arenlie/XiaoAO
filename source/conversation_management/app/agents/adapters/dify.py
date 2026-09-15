from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from typing import Any

from app.agents.catalog import (
    AI_DIAGNOSIS_AGENT_ID,
    INDUSTRIAL_DATA_AGENT_ID,
    default_agent_descriptors,
)
from app.agents.contracts import (
    AgentDescriptor,
    AgentExecutionRuntime,
    AgentHealthResult,
    AgentHealthStatus,
    AgentRequest,
    AgentResult,
    AgentResultStatus,
)
from app.domain.exceptions import AppError
from app.integrations.dify.agent_client import DifyAgentClient
from app.integrations.dify.agent_event_mapper import map_dify_event
from app.integrations.dify.sensitive_data import sanitize_event_payload
from app.integrations.dify.think_filter import ThinkTagStreamSplitter, strip_think_blocks
from app.services.context_builder import ContextBuilder


class DifyStreamingAgentAdapter:
    def __init__(
        self,
        descriptor: AgentDescriptor,
        client: DifyAgentClient,
        context_builder: ContextBuilder,
        *,
        query_prefix: str = "",
    ) -> None:
        self.descriptor = descriptor
        self.client = client
        self.context_builder = context_builder
        self.query_prefix = query_prefix

    @staticmethod
    def _compact_json(value: Any, *, limit: int) -> str:
        raw = json.dumps(value, ensure_ascii=False, default=str)
        return raw if len(raw) <= limit else raw[:limit] + "…"

    def _build_query(self, request: AgentRequest) -> str:
        sections = []
        if self.query_prefix:
            sections.append(self.query_prefix)
        sections.extend(
            [
                f"用户问题：{request.query}",
                f"本次目标：{request.objective or '完成对应专业任务'}",
            ]
        )

        if self.descriptor.agent_id == AI_DIAGNOSIS_AGENT_ID:
            if request.active_diagnosis_task:
                sections.append(
                    "活动诊断上下文："
                    + self._compact_json(request.active_diagnosis_task, limit=4000)
                )

            file_evidence: list[dict[str, Any]] = []
            remaining = 12000
            for item in request.understanding_results:
                text = str(item.get("extracted_text") or "")
                if not text or remaining <= 0:
                    continue
                excerpt = text[: min(remaining, 4000)]
                remaining -= len(excerpt)
                file_evidence.append(
                    {
                        "attachment_id": item.get("attachment_id"),
                        "filename": item.get("filename"),
                        "summary": item.get("summary"),
                        "excerpt": excerpt,
                        "warnings": item.get("warnings") or [],
                    }
                )
            if file_evidence:
                sections.append(
                    "附件证据：" + self._compact_json(file_evidence, limit=14000)
                )

            if request.prior_observations:
                sections.append(
                    "前序智能体观察："
                    + self._compact_json(request.prior_observations, limit=6000)
                )

        return "\n".join(section for section in sections if section).strip()

    async def execute(
        self, request: AgentRequest, runtime: AgentExecutionRuntime
    ) -> AgentResult:
        answer = ""
        external_ids: dict[str, str | None] = {
            "task_id": None,
            "message_id": None,
            "conversation_id": request.external_conversation_id,
        }
        splitter = ThinkTagStreamSplitter(eager_final_after_reasoning=True)
        started = time.perf_counter()
        query = self._build_query(request)

        async def consume(conversation_id: str | None) -> bool:
            nonlocal answer
            async for payload in self.client.stream_chat(
                query=query,
                user_token=runtime.user_token,
                data_access_token=runtime.data_access_token,
                resolved_entity=self.context_builder.serialize(request.resolved_entity),
                active_entity=self.context_builder.serialize(request.active_entity),
                memory_context=self.context_builder.serialize(request.memory_context),
                user_profile=self.context_builder.serialize(request.user_profile),
                conversation_id=conversation_id,
            ):
                for key in ("task_id", "message_id", "conversation_id"):
                    if payload.get(key):
                        external_ids[key] = str(payload[key])
                if external_ids.get("task_id"):
                    await runtime.cache_external_ids(external_ids)
                if await runtime.is_cancelled():
                    return True
                raw_event = str(payload.get("event") or "unknown")
                for item in map_dify_event(payload):
                    if item.event == "task.failed":
                        raise AppError(
                            str(item.data.get("code") or "AGENT_STREAM_ERROR"),
                            str(item.data.get("message") or "Dify 智能体返回错误"),
                            502,
                        )
                    if item.event in {"answer.completed", "reasoning.completed"}:
                        continue
                    if item.event == "answer.replace":
                        answer = strip_think_blocks(str(item.data.get("content") or ""))
                        continue
                    if item.event == "answer.delta":
                        raw = str(item.data.get("content") or "")
                        for segment in splitter.feed(raw):
                            if segment.channel == "final":
                                answer += segment.content
                                await runtime.event_service.publish(
                                    runtime.task_id,
                                    "agent.output.delta",
                                    {
                                        "agent_id": self.descriptor.agent_id,
                                        "content": segment.content,
                                        "span_id": runtime.span_id,
                                    },
                                    actor_type="agent",
                                    actor_id=self.descriptor.agent_id,
                                    span_id=runtime.span_id,
                                    parent_span_id=runtime.parent_span_id,
                                    graph_mode=runtime.execution_mode,
                                )
                        continue
                    if item.event == "reasoning.delta":
                        # Do not expose private reasoning. Persist only a progress marker.
                        await runtime.event_service.publish(
                            runtime.task_id,
                            "agent.progress",
                            {
                                "agent_id": self.descriptor.agent_id,
                                "summary": "专业智能体正在分析",
                                "source_event": raw_event,
                                "span_id": runtime.span_id,
                            },
                            actor_type="agent",
                            actor_id=self.descriptor.agent_id,
                            span_id=runtime.span_id,
                            parent_span_id=runtime.parent_span_id,
                            graph_mode=runtime.execution_mode,
                        )
                await runtime.redis.expire(
                    runtime.context_key, runtime.settings.task_context_ttl_seconds
                )
            for segment in splitter.flush():
                if segment.channel == "final" and segment.content:
                    answer += segment.content
                    await runtime.event_service.publish(
                        runtime.task_id,
                        "agent.output.delta",
                        {
                            "agent_id": self.descriptor.agent_id,
                            "content": segment.content,
                            "span_id": runtime.span_id,
                        },
                        actor_type="agent",
                        actor_id=self.descriptor.agent_id,
                        span_id=runtime.span_id,
                        parent_span_id=runtime.parent_span_id,
                        graph_mode=runtime.execution_mode,
                    )
            return False

        stopped = await consume(request.external_conversation_id)
        clean_answer = strip_think_blocks(answer).strip()
        return AgentResult(
            agent_id=self.descriptor.agent_id,
            status=AgentResultStatus.STOPPED if stopped else AgentResultStatus.COMPLETED,
            answer_markdown=clean_answer,
            evidence=[
                {
                    "source_type": "dify_agent",
                    "agent_id": self.descriptor.agent_id,
                    "content": clean_answer,
                }
            ],
            can_support_final_answer=bool(clean_answer),
            external_ids=external_ids,
            execution_summary={
                "duration_ms": round((time.perf_counter() - started) * 1000, 2)
            },
        )

    async def health_check(self) -> AgentHealthResult:
        configured = bool(self.client.settings.agent_api_key and self.client.settings.agent_base_url)
        return AgentHealthResult(
            agent_id=self.descriptor.agent_id,
            status=AgentHealthStatus.HEALTHY if configured else AgentHealthStatus.UNHEALTHY,
            message="Dify 智能体配置完整" if configured else "Dify 智能体配置缺失",
            checked_at=datetime.now(UTC).isoformat(),
            details={"base_url": self.client.settings.agent_base_url},
        )


class DifyIndustrialDataAdapter(DifyStreamingAgentAdapter):
    def __init__(self, client: DifyAgentClient, context_builder: ContextBuilder) -> None:
        descriptor = next(
            item
            for item in default_agent_descriptors(legacy_xiaoao_enabled=True)
            if item.agent_id == INDUSTRIAL_DATA_AGENT_ID
        )
        super().__init__(descriptor, client, context_builder)


class DifyAIDiagnosisAdapter(DifyStreamingAgentAdapter):
    def __init__(self, client: DifyAgentClient, context_builder: ContextBuilder) -> None:
        descriptor = next(
            item
            for item in default_agent_descriptors(legacy_ai_diagnosis_enabled=True)
            if item.agent_id == AI_DIAGNOSIS_AGENT_ID
        )
        super().__init__(
            descriptor,
            client,
            context_builder,
            query_prefix=(
                "你当前作为 AI 诊断智能体工作。请结合活动诊断任务判断这是首次诊断、诊断追问、"
                "结论解释还是重新诊断，并给出证据、结论置信度和缺失信息。"
            ),
        )
