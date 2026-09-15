from __future__ import annotations

import asyncio
import time
from contextvars import ContextVar
from contextlib import asynccontextmanager
from typing import Any
from uuid import uuid4
from app.output.progress import professional_text, progress_span

STAGE_LABELS: dict[str, tuple[str, str, str]] = {
    "context.load": ("加载对话上下文", "读取本轮所需的注册表、历史上下文和运行状态", "context"),
    "supervisor.classify": ("大模型理解用户任务", "提取目标、范围、证据、操作、时间和资产语义，并判断是否复用可选 Recipe", "llm"),
    "entity.resolve": ("查询对象准备（含路由与检索）", "父步骤墙钟时间；子步骤可能重叠，不应相加。候选就绪可提前进入选择", "orchestration"),
    "content.prepare": ("处理附件与输入内容", "解析附件并准备模型可使用的内容", "content"),
    "quick.answer": ("快速模式生成回答", "调用快速回答智能体生成最终内容", "llm"),
    "normal.react.decide": ("ReAct 决策", "基于目标与当前证据选择一次最有价值的能力调用，证据充分时立即结束", "planning"),
    "normal.react.execute": ("ReAct 执行", "执行当前选中的 MCP、智能体或稳定 Recipe 步骤", "workflow"),
    "normal.react.finalize": ("汇总证据并组织回答", "补齐知识依据后生成最终回答", "orchestration"),
}

_active_span: ContextVar[str | None] = ContextVar("phm_performance_parent", default=None)


def _runtime():
    from app.orchestration.runtime import current_graph_runtime
    return current_graph_runtime()


def _runtime_or_none():
    """Return the graph runtime when bound; direct unit/service calls may have none."""
    try:
        return _runtime()
    except RuntimeError as exc:
        if "runtime context is not bound" not in str(exc):
            raise
        return None


async def emit_performance_event(
    state: dict[str, Any],
    event_type: str,
    *,
    code: str,
    name: str,
    description: str,
    category: str,
    span_id: str,
    duration_ms: float | None = None,
    status: str = "STARTED",
    metrics: dict[str, Any] | None = None,
    parent_span_id: str | None = None,
) -> None:
    runtime = _runtime().agent_runtime
    if not runtime.settings.performance_monitor_enabled:
        return
    payload = {
        "code": code,
        "name_cn": professional_text(name),
        "description_cn": professional_text(description),
        "category": category,
        "span_id": span_id,
        "parent_span_id": parent_span_id or _active_span.get() or state.get("root_span_id"),
        "metrics": metrics or {},
        **{key:(metrics or {})[key] for key in ("optional", "affects_task", "finish_reason") if key in (metrics or {})},
    }
    await runtime.event_service.publish(
        runtime.task_id,
        event_type,
        payload,
        graph_mode=str(state.get("execution_mode") or runtime.execution_mode),
        stage=code,
        actor_type="performance",
        actor_id=code,
        status=status,
        span_id=span_id,
        parent_span_id=parent_span_id or _active_span.get() or state.get("root_span_id"),
        duration_ms=duration_ms,
    )


async def _finish_event(awaitable) -> None:
    """Drain a terminal write even if its caller is cancelled during publication."""
    task = asyncio.create_task(awaitable)
    try:
        await asyncio.wait_for(asyncio.shield(task), 3.0)
    except asyncio.CancelledError:
        try:
            await asyncio.wait_for(asyncio.shield(task), 3.0)
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        raise
    except TimeoutError:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def trace_node(code: str):
    def decorator(func):
        async def wrapped(self, state, *args, **kwargs):
            name, desc, category = STAGE_LABELS.get(code, (code, code, "internal"))
            async with detail_span(state, code=code, name=name, description=desc, category=category) as span:
                result = await func(self, state, *args, **kwargs)
                if (span is not None and code == "entity.resolve" and isinstance(result, dict)
                        and (result.get("entity_result") or {}).get("status") == "ERROR"):
                    span["status"] = "FAILED"
                return result
        wrapped.__name__ = getattr(func, "__name__", "wrapped")
        wrapped.__doc__ = getattr(func, "__doc__", None)
        return wrapped
    return decorator


@asynccontextmanager
async def detail_span(state, *, code, name, description, category, metrics=None):
    context = _runtime_or_none()
    if context is None or not context.agent_runtime.settings.performance_monitor_enabled:
        yield None
        return
    span_id, start_ns = str(uuid4()), time.perf_counter_ns()
    parent = _active_span.get() or state.get("root_span_id")
    mutable = {"metrics": dict(metrics or {}), "span_id": span_id}
    token = None
    status = "COMPLETED"
    optional = code in {"supervisor.entity_intake.llm", "asset.resolve.prefetch"}
    try:
        # Publishing STARTED can itself be cancelled after its database commit.
        # It must therefore be INSIDE the same terminal-event protection as the work.
        await emit_performance_event(state, "performance.span.started", code=code, name=name,
            description=description, category=category, span_id=span_id, status="STARTED",
            metrics={**mutable["metrics"], "optional": optional}, parent_span_id=parent)
        token = _active_span.set(span_id)
        yield mutable
        status = mutable.get("status") or "COMPLETED"
    except BaseException as exc:
        cancelled = isinstance(exc, asyncio.CancelledError)
        status = "CANCELLED" if cancelled else "FAILED"
        mutable["metrics"].update({"cancelled": True, "finish_reason": "superseded_or_stopped"}
            if cancelled else {"error_type": type(exc).__name__})
        raise
    finally:
        if token is not None:
            _active_span.reset(token)
        mutable["metrics"].update(optional=optional, affects_task=not optional)
        await _finish_event(emit_performance_event(state,
            "performance.span.failed" if status == "FAILED" else "performance.span.completed",
            code=code, name=name, description=description, category=category, span_id=span_id,
            parent_span_id=parent, status=status, metrics=mutable["metrics"],
            duration_ms=round((time.perf_counter_ns()-start_ns)/1_000_000, 3)))


def strip_mcp_performance(result: Any) -> dict[str, Any] | None:
    """Remove MCP-internal performance metadata from business evidence and return it."""
    structured = getattr(result, "structured_content", None)
    if not isinstance(structured, dict):
        return None
    trace = structured.pop("_performance_trace", None)
    return trace if isinstance(trace, dict) else None


def sanitize_mcp_trace(trace: dict[str, Any], max_spans: int = 120) -> dict[str, Any]:
    spans = trace.get("spans") if isinstance(trace.get("spans"), list) else []
    clean = []
    for item in spans[:max(1, max_spans)]:
        if not isinstance(item, dict):
            continue
        clean.append(progress_span({
            key: item.get(key)
            for key in (
                "span_id", "parent_span_id", "code", "name", "category", "description",
                "component", "status", "started_offset_ms", "duration_ms", "metrics", "error"
            ) if item.get(key) is not None
        }))
    return {
        "schema_version": trace.get("schema_version") or "1.0",
        "service": trace.get("service"),
        "tool_name": trace.get("tool_name"),
        "enabled": bool(trace.get("enabled", True)),
        "total_duration_ms": trace.get("total_duration_ms"),
        "span_count": len(clean),
        "spans": clean,
    }


def build_performance_report(task: Any, events: list[Any]) -> dict[str, Any]:
    completed = [row for row in events if str(getattr(row, "event_type", "")) in {"performance.span.completed", "performance.span.failed"}]
    tool_rows = [row for row in events if str(getattr(row, "event_type", "")) == "tool.completed"]
    detail_rows = [
        row for row in events
        if str(getattr(row, "event_type", "")) == "performance.mcp.details"
        or isinstance((getattr(row, "payload", {}) or {}).get("execution_trace"), dict)
    ]
    queue_wait_ms = None
    try:
        if task.started_at and task.created_at:
            queue_wait_ms = round((task.started_at-task.created_at).total_seconds()*1000, 3)
    except Exception:
        pass
    total_ms = None
    try:
        if task.completed_at and task.created_at:
            total_ms = round((task.completed_at-task.created_at).total_seconds()*1000, 3)
    except Exception:
        pass
    category_totals: dict[str, float] = {}
    slow = []
    for row in completed:
        payload = dict(getattr(row, "payload", {}) or {})
        cat = str(payload.get("category") or "other")
        dur = float(getattr(row, "duration_ms", 0) or 0)
        category_totals[cat] = round(category_totals.get(cat, 0.0)+dur, 3)
        slow.append({
            "name": payload.get("name_cn") or getattr(row, "stage", None) or "步骤",
            "code": payload.get("code") or getattr(row, "stage", None),
            "category": cat,
            "duration_ms": dur,
            "status": getattr(row, "status", None) or ("FAILED" if row.event_type == "performance.span.failed" else "COMPLETED"),
        })
    mcp_internal_database_ms = 0.0
    mcp_internal_llm_ms = 0.0
    for row in detail_rows:
        payload = getattr(row, "payload", {}) or {}
        trace = payload.get("execution_trace") or payload.get("trace") or {}
        for span in trace.get("spans") or []:
            dur = float(span.get("duration_ms") or 0)
            if span.get("category") == "database": mcp_internal_database_ms += dur
            if span.get("category") == "llm": mcp_internal_llm_ms += dur
    slow.sort(key=lambda x: x["duration_ms"], reverse=True)
    return {
        "enabled": True,
        "task_id": str(task.id),
        "total_duration_ms": total_ms,
        "queue_wait_ms": queue_wait_ms,
        "performance_span_count": len(completed),
        "mcp_call_count": len(tool_rows),
        "mcp_detail_count": len(detail_rows),
        "category_totals_ms": category_totals,
        "mcp_internal_database_ms": round(mcp_internal_database_ms, 3),
        "mcp_internal_llm_ms": round(mcp_internal_llm_ms, 3),
        "slowest_steps": slow[:8],
        "note": "并行子步骤的 duration_ms 不能简单相加作为整次请求墙钟时间。",
    }
