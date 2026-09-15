from __future__ import annotations

import asyncio
import hashlib
import random
import time
from uuid import uuid4

from app.agents.contracts import AgentExecutionRuntime
from app.config import Settings
from app.domain.exceptions import AppError
from app.domain.error_contract import LayeredError, build_layered_error
from app.tools.contracts import ToolCallRequest, ToolCallResult, ToolResultStatus
from app.tools.payload_safety import sanitize_large_payloads
from app.tools.registry import ToolRegistry
from app.performance import sanitize_mcp_trace, strip_mcp_performance


class ResilientToolExecutor:
    RETRYABLE_CODES = {
        "TOOL_TIMEOUT",
        "TOOL_EXECUTION_ERROR",
        "TOOL_PROVIDER_UNAVAILABLE",
        "HTTP_TOOL_FAILED",
        "MCP_TOOL_FAILED",
        "DIFY_WORKFLOW_FAILED",
        "DIFY_WORKFLOW_TIMEOUT",
        "DIFY_WORKFLOW_HTTP_RETRYABLE",
        "DIFY_WORKFLOW_TOOL_FAILED",
        "PHM_ASSET_MCP_TIMEOUT",
        "PHM_ASSET_MCP_UNAVAILABLE",
        "PHM_DATA_MCP_TIMEOUT",
        "PHM_DATA_MCP_UNAVAILABLE",
        "PHM_DIAGNOSIS_MCP_TIMEOUT",
        "PHM_DIAGNOSIS_MCP_UNAVAILABLE",
        "PHM_FEATURE_MCP_TIMEOUT",
        "PHM_FEATURE_MCP_UNAVAILABLE",
    }

    def __init__(self, registry: ToolRegistry, settings: Settings) -> None:
        self.registry = registry
        self.settings = settings
        self._failures: dict[str, int] = {}
        self._opened_at: dict[str, float] = {}

    def _circuit_open(self, tool_id: str) -> bool:
        opened = self._opened_at.get(tool_id)
        if opened is None:
            return False
        if time.monotonic() - opened >= self.settings.agent_circuit_breaker_reset_seconds:
            self._opened_at.pop(tool_id, None)
            self._failures[tool_id] = 0
            return False
        return True

    async def execute(
        self,
        *,
        request: ToolCallRequest,
        runtime: AgentExecutionRuntime,
        parent_span_id: str | None,
    ) -> ToolCallResult:
        descriptor = self.registry.get_descriptor(request.tool_id)
        principal = hashlib.sha256((runtime.user_token + "\0" + (runtime.data_access_token or "")).encode()).hexdigest()[:24]
        circuit_key = request.tool_id + ":" + principal
        if len(self._failures) > 4096:
            self._failures.clear()
            self._opened_at.clear()
        public_arguments = sanitize_large_payloads(request.arguments)
        if self._circuit_open(circuit_key):
            error = build_layered_error(
                code="TOOL_CIRCUIT_OPEN",
                operator_message=f"circuit breaker open for tool {request.tool_id}",
                component=descriptor.display_name,
                workflow_id=request.workflow_id,
                step_id=request.workflow_step_id,
                request_id=runtime.request_id,
                retryable=True,
            )
            return ToolCallResult(
                tool_id=request.tool_id,
                status=ToolResultStatus.FAILED,
                error_code="TOOL_CIRCUIT_OPEN",
                error_message=error.public_message,
                error=error,
            )
        max_retries = self.settings.agent_default_max_retries
        span_id = str(uuid4())
        for attempt in range(1, max_retries + 2):
            provider_error: LayeredError | None = None
            started = time.perf_counter()
            await runtime.event_service.publish(
                runtime.task_id,
                "tool.started" if attempt == 1 else "tool.retrying",
                {
                    "tool_id": request.tool_id,
                    "arguments": public_arguments,
                    "attempt": attempt,
                    "span_id": span_id,
                },
                actor_type="tool",
                actor_id=request.tool_id,
                status="STARTED" if attempt == 1 else "RETRYING",
                attempt=attempt,
                span_id=span_id,
                parent_span_id=parent_span_id,
                graph_mode=runtime.execution_mode,
                input_payload=public_arguments,
            )
            try:
                result = await asyncio.wait_for(
                    self.registry.call(
                        request,
                        data_access_token=runtime.data_access_token,
                    ),
                    timeout=descriptor.timeout_seconds,
                )
                if result.status == ToolResultStatus.FAILED:
                    provider_error = result.error
                    raise AppError(
                        result.error_code or "TOOL_FAILED",
                        (
                            result.error.operator_message
                            if result.error is not None
                            else result.error_message or "工具执行失败"
                        ),
                        502,
                    )
                self._failures[circuit_key] = 0
                mcp_perf = strip_mcp_performance(result)
                internal_trace = None
                if (
                    mcp_perf
                    and runtime.settings.performance_monitor_enabled
                    and runtime.settings.performance_trace_include_mcp_details
                ):
                    internal_trace = sanitize_mcp_trace(
                        mcp_perf, runtime.settings.performance_trace_max_mcp_spans
                    )
                public_result = sanitize_large_payloads(result.model_dump(mode="json"))
                structured = (
                    result.structured_content
                    if isinstance(result.structured_content, dict)
                    else {}
                )
                query_strategy = (
                    structured.get("query_strategy")
                    if isinstance(structured.get("query_strategy"), dict)
                    else {}
                )
                pagination_notice = (
                    str(query_strategy.get("message_cn") or "")
                    if str(query_strategy.get("mode") or "") == "parallel_paged"
                    else ""
                )
                await runtime.event_service.publish(
                    runtime.task_id,
                    "tool.completed",
                    {
                        "tool_id": request.tool_id,
                        "attempt": attempt,
                        "span_id": span_id,
                        "name_cn": descriptor.display_name,
                        "description_cn": pagination_notice,
                        "query_strategy": query_strategy if pagination_notice else {},
                        "internal_monitor_enabled": bool(internal_trace),
                        "execution_trace": internal_trace,
                    },
                    actor_type="tool",
                    actor_id=request.tool_id,
                    status="COMPLETED",
                    attempt=attempt,
                    span_id=span_id,
                    parent_span_id=parent_span_id,
                    graph_mode=runtime.execution_mode,
                    output_payload=public_result,
                    duration_ms=round((time.perf_counter() - started) * 1000, 2),
                )
                return result
            except asyncio.TimeoutError:
                exc = AppError("TOOL_TIMEOUT", f"工具 {request.tool_id} 执行超时", 504)
            except AppError as error:
                exc = error
            except Exception as error:
                exc = AppError("TOOL_EXECUTION_ERROR", str(error), 502)

            retryable = exc.code in self.RETRYABLE_CODES
            if not retryable or attempt > max_retries:
                count = self._failures.get(circuit_key, 0) + 1
                self._failures[circuit_key] = count
                if count >= self.settings.agent_circuit_breaker_threshold:
                    self._opened_at[circuit_key] = time.monotonic()
                error = provider_error or build_layered_error(
                    code=exc.code,
                    operator_message=exc.message,
                    component=descriptor.display_name,
                    workflow_id=request.workflow_id,
                    step_id=request.workflow_step_id,
                    request_id=runtime.request_id,
                    retryable=retryable,
                )
                await runtime.event_service.publish(
                    runtime.task_id,
                    "tool.failed",
                    {
                        "tool_id": request.tool_id,
                        "attempt": attempt,
                        "code": exc.code,
                        "message": error.public_message,
                        "error": error.model_dump(mode="json"),
                        "retryable": retryable,
                        "span_id": span_id,
                    },
                    actor_type="tool",
                    actor_id=request.tool_id,
                    status="FAILED",
                    attempt=attempt,
                    span_id=span_id,
                    parent_span_id=parent_span_id,
                    graph_mode=runtime.execution_mode,
                    error_code=exc.code,
                    error_message=error.operator_message,
                    duration_ms=round((time.perf_counter() - started) * 1000, 2),
                )
                return ToolCallResult(
                    tool_id=request.tool_id,
                    status=ToolResultStatus.FAILED,
                    error_code=error.code,
                    error_message=error.public_message,
                    error=error,
                    metadata={"attempts": attempt, "retryable": retryable},
                )
            delay = min(
                self.settings.agent_retry_max_delay_seconds,
                self.settings.agent_retry_base_delay_seconds * (2 ** (attempt - 1)),
            )
            await asyncio.sleep(delay + random.uniform(0.0, min(0.5, delay / 4)))
        raise AssertionError("unreachable")
