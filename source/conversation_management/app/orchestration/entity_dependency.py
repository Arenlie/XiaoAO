"""One identity contract shared by Recipes and dynamic business queries.

Only structured model intent and declared tool requirements are used here.
No device names, equipment vocabulary or language matching rules belong here.
"""
from __future__ import annotations

from typing import Any, Mapping

from app.asset_collection_scope import is_unscoped_new_asset_collection

LIVE_EVIDENCE = {"alarm", "health", "vibration", "temperature", "asset", "diagnosis_result",
                 "sensor", "sensor_active_faults", "sensor_fault_history", "sensor_monitoring", "sensor_offline", "sensor_points"}


def level(value: Any) -> str | None:
    value = str(value or "").lower()
    value = "area" if value == "space" else value
    return value if value in {"none", "area", "equipment", "point"} else None


def structured_anchor(state: Mapping[str, Any]) -> str | None:
    intent = state.get("business_intent") or {}
    goal = intent.get("goal_frame") or {}
    semantics = intent.get("asset_semantics") or {}
    anchor = level(goal.get("anchor_entity_level"))
    live_lookup = bool(set(goal.get("evidence_types") or []) & LIVE_EVIDENCE) and not goal.get("can_answer_from_context")
    if anchor not in {None, "none"} or (anchor == "none" and not semantics.get("needs_asset_lookup") and not live_lookup):
        return anchor
    for name, target in (("point_no", "point"), ("point", "point"),
                         ("equip_no", "equipment"), ("equipment", "equipment"), ("area", "area")):
        value = semantics.get(name)
        if isinstance(value, Mapping) and str(value.get("raw_text") or "").strip():
            return target
    return anchor


def identity_dependency(state: Mapping[str, Any]) -> dict[str, Any]:
    intent = state.get("business_intent") or {}
    goal = intent.get("goal_frame") or {}
    semantics = intent.get("asset_semantics") or {}
    workflow = state.get("business_workflow") or {}
    if (intent.get("result_followup") or {}).get("active"):
        return {"required": False, "level": "any", "source": "persisted_result_reference", "live_evidence": []}
    anchor = structured_anchor(state)
    workflow_level = level(workflow.get("required_entity_level"))
    workflow_required = workflow_level not in {None, "none"}
    live = bool(set(goal.get("evidence_types") or []) & LIVE_EVIDENCE)
    context_only = bool(goal.get("can_answer_from_context"))
    unscoped_collection = is_unscoped_new_asset_collection(state)
    # A new global equipment collection still needs the Asset collection capability,
    # but it does not need a singular entity identity.  In particular, a category
    # predicate such as equipment_type=水泵 must not be promoted to one equipment.
    model_identity_lookup = bool(semantics.get("needs_asset_lookup")) and not unscoped_collection
    required = workflow_required or model_identity_lookup or bool(
        live and not context_only and anchor not in {None, "none"}
    )
    # A registered Recipe declares its input identity (point diagnosis needs a
    # point, even when the model also names its equipment as the semantic anchor).
    # Dynamic collection goals still resolve their parent anchor; the members are
    # discovered by later tools, rather than being mandatory singular inputs.
    required_level = workflow_level if workflow_required else anchor
    return {"required": required, "level": required_level if required_level not in {None, "none"} else "any",
            "source": (
                "workflow_and_structured_goal"
                if workflow_required
                else "shared_catalog_collection"
                if unscoped_collection
                else "structured_goal"
            ),
            "live_evidence": sorted(set(goal.get("evidence_types") or []) & LIVE_EVIDENCE)}


def identity_service_failed(state: Mapping[str, Any]) -> bool:
    result = state.get("entity_result") or {}
    dependency = state.get("entity_dependency") or identity_dependency(state)
    # A late tool prerequisite can establish the dependency after the initial gate.
    required = bool(dependency.get("required")) or bool(result.get("need_lookup"))
    return required and str(result.get("status") or "").upper() == "ERROR"


def identity_failure_message(state: Mapping[str, Any]) -> str:
    result = state.get("entity_result") or {}
    if (result.get("error") or {}).get("code") == "WORKFLOW_LLM_CLASSIFICATION_FAILED":
        return "本轮问题理解服务暂时不可用，请稍后重试。"
    if str(result.get("status") or "").upper() == "ERROR":
        return "设备与区域检索服务暂时未能完成查询，因此目前无法确认目标设备或读取其运行状态。请稍后重试。"
    return "已检索资产目录，但尚未找到能够确认的目标对象，因此目前无法判断其运行状态。"
