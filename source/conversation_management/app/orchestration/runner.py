from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.config import Settings
from app.domain.enums import ExecutionMode
from app.domain.exceptions import AppError
from app.orchestration.graphs.normal_graph import build_normal_graph
from app.orchestration.graphs.quick_graph import build_quick_graph
from app.orchestration.nodes import ConversationGraphNodes
from app.orchestration.runtime import GraphRuntimeContext, graph_runtime_scope


@dataclass(slots=True)
class GraphRunResult:
    state: dict[str, Any]

    @property
    def final_status(self) -> str:
        return str(self.state.get("final_status") or "FAILED")

    @property
    def final_answer(self) -> str:
        return str(self.state.get("final_answer") or "")

    @property
    def external_ids(self) -> dict[str, str | None]:
        return dict(self.state.get("external_ids") or {})


class ConversationGraphRunner:
    def __init__(
        self,
        *,
        settings: Settings,
        nodes: ConversationGraphNodes,
        checkpointer=None,
    ) -> None:
        normal_graph = build_normal_graph(nodes, checkpointer=checkpointer)
        self.settings = settings
        self.graphs = {
            ExecutionMode.QUICK.value: build_quick_graph(nodes, checkpointer=checkpointer),
            ExecutionMode.NORMAL.value: normal_graph,
            # Backward compatibility only.  Old callers that still send "expert" are
            # executed by the exact same bounded normal ReAct graph.
            ExecutionMode.EXPERT.value: normal_graph,
        }

    async def run(
        self,
        initial_state: dict[str, Any],
        runtime: GraphRuntimeContext,
    ) -> GraphRunResult:
        requested_mode = str(
            initial_state.get("execution_mode") or self.settings.default_execution_mode
        ).lower()
        mode = "normal" if requested_mode == "expert" else requested_mode
        graph = self.graphs.get(mode)
        if graph is None:
            raise AppError("EXECUTION_MODE_INVALID", f"不支持的执行模式: {requested_mode}", 422)
        if requested_mode == "expert":
            initial_state = {**initial_state, "execution_mode": "normal"}
        config = {
            "configurable": {
                "thread_id": f"{initial_state['task_id']}:{initial_state.get('graph_attempt', 0)}",
                "checkpoint_ns": f"{mode}:{initial_state['branch_id']}",
                "run_id": initial_state["task_id"],
            },
            "recursion_limit": max(
                24,
                int(getattr(self.settings, "normal_max_execution_steps", 8) or 8) * 3,
            ),
        }
        try:
            with graph_runtime_scope(runtime):
                state = await graph.ainvoke(initial_state, config=config)
            await runtime.agent_runtime.event_service.publish(
                runtime.agent_runtime.task_id,
                "graph.completed",
                {
                    "graph_mode": mode,
                    "final_status": state.get("final_status"),
                    "span_id": state.get("root_span_id"),
                },
                graph_mode=mode,
                actor_type="graph",
                actor_id=f"{mode}_graph",
                status="COMPLETED",
                span_id=state.get("root_span_id"),
            )
        except AppError:
            raise
        except Exception as exc:
            raise AppError("LANGGRAPH_EXECUTION_FAILED", str(exc), 500) from exc
        return GraphRunResult(state=dict(state))
