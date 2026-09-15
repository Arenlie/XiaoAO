from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from app.agents.contracts import AgentExecutionRuntime


@dataclass(frozen=True, slots=True)
class GraphRuntimeContext:
    agent_runtime: AgentExecutionRuntime
    set_task_status: Any
    set_task_streaming: Any
    transient_tool_payloads: dict[str, Any]
    workspace_service: Any | None = None


_current_runtime: ContextVar[GraphRuntimeContext | None] = ContextVar(
    "conversation_graph_runtime", default=None
)


@contextmanager
def graph_runtime_scope(runtime: GraphRuntimeContext):
    token = _current_runtime.set(runtime)
    try:
        yield
    finally:
        # Selection, stop and failure must not leave an attachment model request orphaned.
        task=runtime.transient_tool_payloads.get("_attachment_analysis")
        if task is not None and not task.done():
            task.cancel()
        if task is not None and task.done() and not task.cancelled():
            task.exception()
        _current_runtime.reset(token)


def current_graph_runtime() -> GraphRuntimeContext:
    runtime = _current_runtime.get()
    if runtime is None:
        raise RuntimeError("LangGraph runtime context is not bound")
    return runtime
