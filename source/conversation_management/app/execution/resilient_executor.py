from __future__ import annotations

import asyncio
import hashlib
import random
import time
from dataclasses import replace
from uuid import uuid4

from app.agents.contracts import (
    AgentExecutionRuntime,
    AgentRequest,
    AgentResult,
    AgentResultStatus,
)
from app.agents.registry import AgentAdapterRegistry
from app.config import Settings
from app.domain.exceptions import AppError
from app.output.streaming import AnswerStreamStopped
from app.orchestration.runtime import current_graph_runtime
from app.domain.error_contract import LayeredError, build_layered_error


class ResilientAgentExecutor:
    RETRYABLE_CODES = {
        "MODEL_TIMEOUT", "MODEL_UNAVAILABLE", "ENTITY_LOOKUP_TIMEOUT",
        "ENTITY_LOOKUP_FAILED", "AGENT_UNAVAILABLE", "AGENT_STREAM_ERROR",
        "MULTIMODAL_MODEL_TIMEOUT", "MULTIMODAL_MODEL_UNAVAILABLE",
    }

    def __init__(self, registry: AgentAdapterRegistry, settings: Settings) -> None:
        self.registry = registry
        self.settings = settings
        self._failures: dict[str, int] = {}
        self._opened_at: dict[str, float] = {}

    def _circuit_open(self, agent_id: str) -> bool:
        opened = self._opened_at.get(agent_id)
        if opened is None:
            return False
        if time.monotonic() - opened >= self.settings.agent_circuit_breaker_reset_seconds:
            self._opened_at.pop(agent_id, None)
            self._failures[agent_id] = 0
            return False
        return True

    async def execute(
        self,
        *,
        request: AgentRequest,
        runtime: AgentExecutionRuntime,
        parent_span_id: str | None,
    ) -> AgentResult:
        adapter = self.registry.get(request.agent_id)
        agent_id = adapter.descriptor.agent_id
        principal = hashlib.sha256((runtime.user_token + "\0" + (runtime.data_access_token or "")).encode()).hexdigest()[:24]
        circuit_key = agent_id + ":" + principal
        if len(self._failures) > 4096:
            self._failures.clear()
            self._opened_at.clear()
        if self._circuit_open(circuit_key):
            error = build_layered_error(
                code="AGENT_CIRCUIT_OPEN",
                operator_message=f"circuit breaker open for agent {agent_id}",
                component=adapter.descriptor.display_name,
                workflow_id=request.workflow_id,
                step_id=request.workflow_step_id,
                request_id=runtime.request_id,
                retryable=True,
            )
            return AgentResult(
                agent_id=agent_id,
                status=AgentResultStatus.FAILED,
                error_code="AGENT_CIRCUIT_OPEN",
                error_message=error.public_message,
                error=error,
            )
        max_retries = max(adapter.descriptor.max_retries, self.settings.agent_default_max_retries)
        span_id = str(uuid4())
        child_runtime = replace(
            runtime,
            span_id=span_id,
            parent_span_id=parent_span_id,
            execution_mode=request.execution_mode,
        )
        for attempt in range(1, max_retries + 2):
            provider_error: LayeredError | None = None
            started = time.perf_counter()
            await runtime.event_service.publish(
                runtime.task_id,
                "agent.started" if attempt == 1 else "agent.retrying",
                {
                    "agent_id": agent_id,
                    "objective": request.objective,
                    "attempt": attempt,
                    "span_id": span_id,
                },
                actor_type="agent",
                actor_id=agent_id,
                status="STARTED" if attempt == 1 else "RETRYING",
                attempt=attempt,
                span_id=span_id,
                parent_span_id=parent_span_id,
                graph_mode=request.execution_mode,
                input_payload={"objective": request.objective, "query": request.query},
            )
            try:
                result = await asyncio.wait_for(
                    adapter.execute(request, child_runtime),
                    timeout=adapter.descriptor.timeout_seconds
                    or self.settings.agent_default_timeout_seconds,
                )
                if result.status == AgentResultStatus.FAILED:
                    provider_error = result.error
                    raise AppError(
                        result.error_code or "AGENT_FAILED",
                        (
                            result.error.operator_message
                            if result.error is not None
                            else result.error_message or "智能体执行失败"
                        ),
                        502,
                    )
                self._failures[circuit_key] = 0
                await runtime.event_service.publish(
                    runtime.task_id,
                    "agent.completed",
                    {
                        "agent_id": agent_id,
                        "attempt": attempt,
                        "span_id": span_id,
                        "can_support_final_answer": result.can_support_final_answer,
                    },
                    actor_type="agent",
                    actor_id=agent_id,
                    status="COMPLETED",
                    attempt=attempt,
                    span_id=span_id,
                    parent_span_id=parent_span_id,
                    graph_mode=request.execution_mode,
                    output_payload=result.model_dump(mode="json"),
                    duration_ms=round((time.perf_counter() - started) * 1000, 2),
                )
                return result
            except AnswerStreamStopped:
                raise
            except asyncio.TimeoutError:
                exc = AppError("AGENT_TIMEOUT", f"智能体 {agent_id} 执行超时", 504)
            except AppError as error:
                exc = error
            except Exception as error:
                exc = AppError("AGENT_EXECUTION_ERROR", str(error), 502)

            retryable = exc.code in self.RETRYABLE_CODES or exc.code in {
                "AGENT_TIMEOUT", "AGENT_EXECUTION_ERROR"
            }
            if request.execution_mode == "quick":
                try:
                    streamed = current_graph_runtime().transient_tool_payloads.get("_answer_generation") or {}
                    if streamed.get("content") or streamed.get("reasoning_available"):
                        retryable = False
                except RuntimeError:
                    pass
            if not retryable or attempt > max_retries:
                count = self._failures.get(circuit_key, 0) + 1
                self._failures[circuit_key] = count
                if count >= self.settings.agent_circuit_breaker_threshold:
                    self._opened_at[circuit_key] = time.monotonic()
                error = provider_error or build_layered_error(
                    code=exc.code,
                    operator_message=exc.message,
                    component=adapter.descriptor.display_name,
                    workflow_id=request.workflow_id,
                    step_id=request.workflow_step_id,
                    request_id=runtime.request_id,
                    retryable=retryable,
                )
                await runtime.event_service.publish(
                    runtime.task_id,
                    "agent.failed",
                    {
                        "agent_id": agent_id,
                        "attempt": attempt,
                        "code": exc.code,
                        "message": error.public_message,
                        "error": error.model_dump(mode="json"),
                        "retryable": retryable,
                        "span_id": span_id,
                    },
                    actor_type="agent",
                    actor_id=agent_id,
                    status="FAILED",
                    attempt=attempt,
                    span_id=span_id,
                    parent_span_id=parent_span_id,
                    graph_mode=request.execution_mode,
                    error_code=exc.code,
                    error_message=error.operator_message,
                    duration_ms=round((time.perf_counter() - started) * 1000, 2),
                )
                return AgentResult(
                    agent_id=agent_id,
                    status=AgentResultStatus.FAILED,
                    error_code=error.code,
                    error_message=error.public_message,
                    error=error,
                    execution_summary={"attempts": attempt, "retryable": retryable},
                )
            delay = min(
                self.settings.agent_retry_max_delay_seconds,
                self.settings.agent_retry_base_delay_seconds * (2 ** (attempt - 1)),
            )
            await asyncio.sleep(delay + random.uniform(0.0, min(0.5, delay / 4)))
        raise AssertionError("unreachable")
