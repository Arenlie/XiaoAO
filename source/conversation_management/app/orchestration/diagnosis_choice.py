"""Turn-level analysis depth; all language decisions come from task understanding."""
from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from app.tools.phm_feature_mcp import PHM_FEATURE_TOOL_IDS
from app.tools.phm_diagnosis_mcp import PHM_DIAGNOSIS_TOOL_IDS, PHM_DIAGNOSIS_MODEL_ADMISSION_TOOL_ID
from app.tools.phm_data_mcp import PHM_GET_DATA_SNAPSHOT_TOOL_ID, PHM_GET_WAVEFORM_TOOL_ID
from app.tools.phm_workflow_tools import (PHM_WORKFLOW_POINT_DATA_BATCH_TOOL_ID,
    PHM_WORKFLOW_POINT_RPM_BATCH_TOOL_ID, PHM_WORKFLOW_POINT_DIAGNOSIS_BATCH_TOOL_ID)

EXPENSIVE_TOOLS = PHM_FEATURE_TOOL_IDS | (PHM_DIAGNOSIS_TOOL_IDS - {PHM_DIAGNOSIS_MODEL_ADMISSION_TOOL_ID}) | {
    PHM_WORKFLOW_POINT_DATA_BATCH_TOOL_ID, PHM_WORKFLOW_POINT_RPM_BATCH_TOOL_ID,
    PHM_WORKFLOW_POINT_DIAGNOSIS_BATCH_TOOL_ID}
QUESTION = ("请选择本次分析方式：\n\n"
    "**快速分析**：结合已有资料、设备信息、健康度、报警、传感器状态和知识库给出初步分析，等待时间较短。\n\n"
    "**详细诊断**：读取波形和历史趋势，进行转速识别及专业诊断。耗时可能接近 10 分钟；"
    "设备测点较多、数据读取或模型排队较慢时可能更久。各阶段有先后依赖，多测点需要限流处理。\n\n"
    "直接回复“快速分析”或“详细诊断”，也可以使用含义相近的表述，或直接提出其他问题。"
    "未回复时不会自动启动详细诊断。")


def requires_detailed(call, state):
    return (call.tool_id in EXPENSIVE_TOOLS or (depth(state) == "quick" and
        call.tool_id in {PHM_GET_DATA_SNAPSHOT_TOOL_ID, PHM_GET_WAVEFORM_TOOL_ID}))


def diagnosis_requested(state):
    intent = state.get("business_intent") or {}
    workflow = state.get("business_workflow") or {}
    if workflow.get("workflow_id") == "diagnosis_analysis" and workflow.get("variant_id") == "scope_screening":
        return False
    return bool((intent.get("goal_frame") or {}).get("diagnosis_requested") or (
        workflow.get("workflow_id") == "diagnosis_analysis" and workflow.get("variant_id") in {"point", "device"}))


def depth(state):
    choice = state.get("diagnosis_choice") or {}
    if choice.get("source") == "user_confirmation" and choice.get("mode") in {"quick", "detailed"}:
        return choice["mode"]
    intent = state.get("business_intent") or {}
    if intent.get("analysis_depth_explicit") and float(intent.get("analysis_depth_confidence") or 0) >= .90:
        value = intent.get("analysis_depth")
        return value if value in {"quick", "detailed"} else "unspecified"
    return "unspecified"


def apply_depth(state):
    """A quick assessment retains the original target, but has no diagnosis Recipe."""
    mode = depth(state)
    if mode != "quick":
        return state
    updated = dict(state)
    intent = deepcopy(state.get("business_intent") or {})
    goal = intent.setdefault("goal_frame", {})
    goal.update(diagnosis_requested=False, operations=["retrieve", "assess", "explain"])
    intent.update(recipe_recommended=False, workflow_id="none", variant_id="none")
    updated.update(business_intent=intent, business_workflow={}, analysis_depth="quick")
    return updated


def confirmation_required(state):
    return diagnosis_requested(state) and depth(state) == "unspecified"


def confirmation_update(state, ttl):
    target = dict(state.get("resolved_entity") or state.get("selected_entity") or {})
    return {"final_status":"WAITING_CONFIRMATION", "final_answer": QUESTION,
        "next_call":None, "next_calls":[], "diagnosis_confirmation":{
            "confirmation_id":str(uuid4()), "status":"PENDING", "question":QUESTION,
            "query":str(state.get("query") or ""), "target":target,
            "expires_at":(datetime.now(UTC)+timedelta(seconds=ttl)).isoformat(),
            "options":[]}}


def resume_state(state):
    keys = ("query", "business_intent", "business_workflow", "resolved_entity", "selected_entity",
        "entity_result", "entity_resolution", "entity_dependency", "query_scope", "entity_constraints", "attachments", "understanding_results", "observations")
    return {key:deepcopy(state[key]) for key in keys if key in state}


def public_confirmation(task, value):
    return {"task_id":str(task.id), "message_id":str(task.assistant_message_id),
        **{k:value.get(k) for k in ("confirmation_id", "expires_at")},
        "question": QUESTION, "options": [],
        "status": "COMPLETED" if task.status == "COMPLETED" else "WAITING_CONFIRMATION",
        "requires_action":True, "stream_continues": task.status != "COMPLETED",
        "resume_endpoint":f"/chat/v1/tasks/{task.id}/diagnosis-mode",
        "last_event_id":f"{int(task.event_sequence or 0)}-0"}
