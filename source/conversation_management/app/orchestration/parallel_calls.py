"""Batch only independent reads; entity selection and data consumers stay ordered."""
from __future__ import annotations

import asyncio
import copy
from dataclasses import replace

from app.orchestration.runtime import current_graph_runtime, graph_runtime_scope
from app.tools.dify_knowledge import KNOWLEDGE_TOOL_ID
from app.tools.phm_data_mcp import PHM_DATA_TOOL_IDS
from app.tools.phm_asset_mcp import PHM_ASSET_QUERY_EQUIPMENT_INFO_TOOL_ID
from app.tools.phm_sensor_mcp import SENSOR_TOOL_BY_OPERATION, SENSOR_EVIDENCE_TOOL_ID

PARALLEL_READS = set(PHM_DATA_TOOL_IDS) | set(SENSOR_TOOL_BY_OPERATION.values()) | {SENSOR_EVIDENCE_TOOL_ID, KNOWLEDGE_TOOL_ID, PHM_ASSET_QUERY_EQUIPMENT_INFO_TOOL_ID}


def independent_calls(calls, limit):
    selected, targets = [], set()
    for call in calls:
        safe = call.call_type == "tool" and call.tool_id in PARALLEL_READS and not call.depends_on
        if not selected and not safe:
            return [call]
        if not safe or call.target_id in targets:
            continue
        selected.append(call)
        targets.add(call.target_id)
        if len(selected) >= max(1, limit):
            break
    return selected


async def execute_independent(calls, state, execute):
    runtime = current_graph_runtime()
    original = dict(runtime.transient_tool_payloads)

    async def one(call):
        local_payloads = {k: list(v) if k.startswith("history:") and isinstance(v, list) else v
                          for k, v in original.items()}
        child_runtime = replace(runtime, transient_tool_payloads=local_payloads)
        with graph_runtime_scope(child_runtime):
            try:
                result = await execute(copy.deepcopy(state), call.model_copy(deep=True))
            except asyncio.CancelledError:
                raise
            except Exception:
                result = {"observations": [{
                    "call_id": call.call_id, "call_type": call.call_type, "tool_id": call.tool_id,
                    "target_id": call.target_id, "workflow_id": call.workflow_id,
                    "workflow_step_id": call.workflow_step_id, "status": "FAILED",
                    "answer_markdown": "该项查询暂时无法完成，其他已取得的结果仍可使用。",
                    "error_code": "PARALLEL_CALL_FAILED", "can_support_final_answer": False,
                }]}
        return result, local_payloads

    # gather preserves plan order, regardless of response completion order.
    outcomes = await asyncio.gather(*(one(call) for call in calls))
    merged = {"observations": []}
    for result, payloads in outcomes:
        merged["observations"].extend(result.get("observations", []))
        for key, value in result.items():
            if key != "observations":
                merged[key] = value
        for key, value in payloads.items():
            if key not in original or value is not original[key]:
                if key.startswith("history:"):
                    previous = original.get(key, [])
                    if isinstance(value, list):
                        runtime.transient_tool_payloads.setdefault(key, []).extend(value[len(previous):])
                else:
                    runtime.transient_tool_payloads[key] = value
    return merged
