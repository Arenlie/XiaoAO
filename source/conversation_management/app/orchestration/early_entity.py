"""Bounded speculative identity lookup; publish only confirmed database choices.

All tasks and state belong to one graph invocation. A pending route is cancelled
when a valid early choice pauses the graph, and reconstructed after user selection.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.orchestration.entity_dependency import identity_dependency
from app.performance import detail_span
from app.tools.alarm_context import entity_satisfies_required_level
from app.workflows.contracts import AssetSemanticHints, WorkflowIntentClassification


class EntityIntake(BaseModel):
    model_config = ConfigDict(extra="forbid")
    @model_validator(mode="before")
    @classmethod
    def compatible_shape(cls, value):
        if not isinstance(value, dict): return value
        value = dict(value)
        if value.get("required_entity_level") == "area": value["required_entity_level"] = "space"
        # Known explanatory output is not an identity decision. Unknown fields still fail validation.
        value.pop("reason", None)
        if value.get("multiple_targets") is None: value["multiple_targets"] = False
        return value

    required_entity_level: Literal["none", "space", "equipment", "point"]
    multiple_targets: bool = False
    confidence: float = Field(ge=0, le=1)
    asset_semantics: AssetSemanticHints


ENTITY_INTAKE_PROMPT = """
你只识别当前问题是否需要查询真实资产，以及查询的根对象；不回答、不制定业务计划。
结合当前问题和最近对话由语义判断，禁止关键词规则。当前明确的新对象优先；指代沿用已确认上下文。
仅输出JSON：required_entity_level(none/space/equipment/point)、multiple_targets(bool)、confidence(0..1)、asset_semantics。
asset_semantics 只输出非空项：area/equipment/equipment_type/equip_no/point/point_no/component/position/direction/measurement，
每项为{"raw_text":"逐字原文","retrieval_text":"简短规范语义"}；以及needs_asset_lookup、reference_target_level(none/space/equipment/point)、refresh_requested。
raw_text必须来自本轮原文，不能从摘要编造；编号必须是用户实际提供的编号，不猜编码。
具体设备带专名/编号/序号填equipment；裸类别填equipment_type。reference_target_level只声明指代层级，不把“它/这个设备”填入equipment。
原回答加字段、改格式、引用第几条等结果追问：none，needs_asset_lookup=false；原结果标识由主控与结果服务确认，禁止抢先检索其中某一个设备。
纯概念解释、仅解释已给出的上下文、无资产绑定的知识/附件、全局传感器统计：none，needs_asset_lookup=false。
用户分别点名多个独立设备/区域，或按类别/条件要求多个根对象：multiple_targets=true，不抢先选择一个全局对象。
区域下设备清单、数量、比较、筛选、找最严重设备：根是space，具体成员是查询输出，不要求先选成员。
设备测点清单：根是equipment。明确诊断具体测点/位置/“设备的测点”：point；明确诊断整台设备：equipment。
健康度为什么扣分仍是健康度解释，不能当成单测点诊断。“有问题吗”先查询状态，不升级专业诊断。
在线、离线、偏置电压等传感器自检：按用户指定equipment或point；全局查询根为none。
真实身份及重名必须交资产服务检索；不在此输出resolved_entity。字段不确定时降低confidence。
""".strip()


def intake_state(state, intake):
    level = intake.required_entity_level
    classification = WorkflowIntentClassification(
        confidence=intake.confidence, asset_semantics=intake.asset_semantics,
        goal_frame={"goal": str(state.get("query") or ""), "anchor_entity_level": level,
                    "target_entity_level": level, "evidence_required": True},
    ).model_dump(mode="json")
    classification.update(routing_pending=True, routing_status="pending", entity_intake_level=level)
    return {**state, "business_intent": classification, "business_workflow": {}}


def eligible(state, intake):
    # Point workflows must retain their established equipment -> point guard and
    # multi-parent point selection. Multiple root targets are excluded; a single
    # parent of a descendant collection can still be resolved early.
    if (intake is None or intake.confidence < .95 or intake.multiple_targets
            or intake.required_entity_level not in {"space", "equipment"}
            or not intake.asset_semantics.needs_asset_lookup):
        return False
    hints = intake.asset_semantics
    if any(getattr(hints, key).raw_text for key in ("point", "point_no", "component", "position", "direction", "measurement")):
        return False
    fields = ("area",) if intake.required_entity_level == "space" else ("equipment", "equip_no")
    query = str(state.get("query") or "")
    return any(term and term in query for term in (getattr(hints, key).raw_text for key in fields))


def fingerprint(hints, level):
    keys = ("area",) if level in {"space", "area"} else ("area", "equipment", "equipment_type", "equip_no")
    return tuple((key, str((hints.get(key) or {}).get("raw_text") or ""),
                  str((hints.get(key) or {}).get("retrieval_text") or "")) for key in keys) + (
        ("reference", str(hints.get("reference_target_level") or "none")),
        ("refresh", bool(hints.get("refresh_requested"))),
    )


def compatible(intake, classification, selection):
    if classification.get("routing_pending") or classification.get("routing_status") == "error" or len(classification.get("independent_queries") or []) >= 2:
        return False
    selected = selection.model_dump(mode="json") if selection else {}
    dep = identity_dependency({"business_intent": classification, "business_workflow": selected})
    level = "space" if dep["level"] == "area" else dep["level"]
    return (dep["required"] and level == intake.required_entity_level
            and fingerprint(intake.asset_semantics.model_dump(mode="json"), level)
            == fingerprint(classification.get("asset_semantics") or {}, level))


async def classify_with_early_entity(nodes, state, classify):
    """Return classification, Recipe, optional outcome, and early-selection flag."""
    progress = {}

    async def early_lookup():
        try:
            # Keep the parallel entity-intake optimization, but do not expose this
            # internal acceleration step as a frontend-visible performance node.
            from app.services.sensor_references import code_tokens
            if code_tokens(state.get("query")):
                return None
            intake = await nodes.supervisor.understand_entity_target(state)
            if not eligible(state, intake):
                return None
            progress["intake"] = intake
            lookup_state = intake_state(deepcopy(state), intake)
            required = "area" if intake.required_entity_level == "space" else intake.required_entity_level
            async with detail_span(state, code="asset.resolve.prefetch", name="Asset MCP：提前检索实体",
                    description="按轻量模型提取的真实查询范围检索；仅数据库候选可用于选择", category="mcp") as span:
                outcome = await nodes.entity_resolution_layer.resolve(lookup_state, required_entity_level=required)
                if span is not None:
                    span["metrics"]["entity_status"] = (outcome.updates.get("entity_result") or {}).get("status")
            return intake, outcome
        except Exception as exc:
            import logging
            from pydantic import ValidationError
            fields = [{"type": e["type"], "loc": list(e["loc"])} for e in exc.errors(include_input=False, include_url=False)] if isinstance(exc, ValidationError) else []
            logging.getLogger(__name__).warning("early_entity_failed", extra={"task_id": str(state.get("task_id") or ""), "error_type": type(exc).__name__, "validation_fields": fields})
            # Optional acceleration can fail without changing the authoritative path.
            return None

    route_task = asyncio.create_task(classify(), name="phm-business-classification")
    entity_task = asyncio.create_task(early_lookup(), name="phm-early-entity")
    try:
        done, _ = await asyncio.wait((route_task, entity_task), return_when=asyncio.FIRST_COMPLETED)
        early = entity_task.result() if entity_task in done else None
        if early and not route_task.done():
            intake, outcome = early
            result = outcome.updates.get("entity_result") or {}
            rows = result.get("matches") or []
            required = "area" if intake.required_entity_level == "space" else intake.required_entity_level
            if (result.get("status") == "MULTIPLE" and len(rows) > 1
                    and all(isinstance(row, dict) and entity_satisfies_required_level(row, required) for row in rows)):
                # No business calls run with the provisional routing frame. The normal
                # worker persists candidates + resume context before publishing SSE.
                return intake_state(state, intake)["business_intent"], None, outcome, True
        classification, selection = await route_task
        intake = early[0] if early else progress.get("intake")
        if intake and compatible(intake, classification, selection):
            early = early or await entity_task
            if early and (early[1].updates.get("entity_result") or {}).get("status") in {"UNIQUE", "MULTIPLE", "NO_LOOKUP"}:
                return classification, selection, early[1], False
        return classification, selection, None, False
    finally:
        for task in (route_task, entity_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(route_task, entity_task, return_exceptions=True)
