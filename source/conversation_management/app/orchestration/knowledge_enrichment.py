"""Business evidence triggers knowledge enrichment, never natural-language rules."""
from __future__ import annotations

from app.orchestration.entity_dependency import LIVE_EVIDENCE
from app.output.customer import customer_text
from app.tools.dify_knowledge import KNOWLEDGE_TOOL_ID
from app.tools.independent_queries import MULTI_QUERY_TOOL_ID
from app.tools.phm_asset_mcp import PHM_ASSET_TOOL_IDS
from app.tools.phm_data_mcp import PHM_DATA_TOOL_IDS
from app.tools.phm_diagnosis_mcp import PHM_DIAGNOSIS_TOOL_IDS
from app.tools.phm_feature_mcp import PHM_FEATURE_TOOL_IDS
from app.tools.phm_sensor_mcp import PHM_SENSOR_TOOL_IDS
from app.tools.phm_workflow_tools import PHM_WORKFLOW_TOOL_IDS

BUSINESS_TOOLS = set().union(PHM_ASSET_TOOL_IDS, PHM_DATA_TOOL_IDS,
    PHM_DIAGNOSIS_TOOL_IDS, PHM_FEATURE_TOOL_IDS, PHM_SENSOR_TOOL_IDS,
    PHM_WORKFLOW_TOOL_IDS, {MULTI_QUERY_TOOL_ID, "dify.comprehensive_alarm_query"})


def needs_enrichment(state, settings):
    intent = state.get("business_intent") or {}
    if (intent.get("result_followup") or {}).get("active") and (intent.get("result_followup") or {}).get("action")=="render":
        return False  # Presentation-only turn reuses platform facts; no new claim needs retrieval.
    if intent.get("knowledge_opt_out"):
        return False
    if any(isinstance(row, dict) and row.get("tool_id") == KNOWLEDGE_TOOL_ID
           for row in state.get("observations") or []):
        return False  # Once per turn, including no-hit/error; no automatic retry loop.
    if intent.get("knowledge_required"):
        return True
    if not getattr(settings, "dify_knowledge_auto_enrich", True):
        return False
    return True


def enrichment_query(state):
    """Use resolved names and observed fault labels without another LLM round trip."""
    intent = state.get("business_intent") or {}
    entity = state.get("resolved_entity") or state.get("selected_entity") or {}
    names = list(dict.fromkeys(str(entity[k]) for k in
                 ("equip_name", "equipment_name", "point_name", "space_name", "display_name")
                 if isinstance(entity, dict) and entity.get(k)))
    labels = []
    target = state.get("sensor_target") or {}
    if target.get("fault_type_name"):
        labels.append(str(target["fault_type_name"])[:65])
    visited = 0
    def walk(value, depth=0):
        nonlocal visited
        if depth > 10 or visited >= 1500 or len(labels) >= 4:
            return
        visited += 1
        if isinstance(value, dict):
            for key, item in value.items():
                if key in {"fault_type_name", "fault_type", "model_name", "diagnosis_name",
                           "conclusion", "fault_description", "warn_description", "alarm_description"}:
                    if isinstance(item, str) and item.strip() and item not in labels:
                        labels.append(customer_text(item)[:65])
                elif isinstance(item, (dict, list)):
                    walk(item, depth + 1)
                if len(labels) >= 4:
                    break
        elif isinstance(value, list):
            for item in value[:100]:
                walk(item, depth + 1)
                if len(labels) >= 4:
                    break
    for row in reversed(state.get("observations") or []):
        if isinstance(row, dict) and row.get("tool_id") in BUSINESS_TOOLS:
            walk((row.get("tool_result") or {}).get("structured_content") or row.get("evidence") or {})
    base = customer_text(str(intent.get("knowledge_query") or state.get("query") or "设备维护知识"))
    if target.get("fault_id"):
        base = "传感器" + str(target.get("fault_type_name") or "故障") + "原因与排查；" + str(target.get("analysis_result") or "")[:90]
    parts = [base[:120]]
    if names:
        parts.append("对象：" + "、".join(names)[:45])
    if labels:
        parts.append("已查结果：" + "、".join(labels)[:65])
    if state.get("understanding_results"):
        evidence = "；".join(str(x.get("summary") or x.get("extracted_text") or "")[:100]
            for x in state["understanding_results"][:2] if isinstance(x, dict))
        parts.append("附件现象：" + customer_text(evidence)[:100])
    parts.append("相关原理、排查维护依据和类似案例")
    return "；".join(parts)[:250]
