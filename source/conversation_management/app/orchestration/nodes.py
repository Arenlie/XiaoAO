from __future__ import annotations

import time

import asyncio
import json
from typing import Any
from uuid import uuid4

from app.agents.catalog import FUZZY_ENTITY_AGENT_ID, GENERAL_CONTENT_AGENT_ID
from app.agents.contracts import AgentDescriptor, AgentRequest
from app.attachments.contracts import AttachmentDescriptor
from app.content.contracts import ContentEnvelope
from app.content.visual_evidence import add_visual_evidence
from app.orchestration.diagnosis_choice import (apply_depth, confirmation_required, confirmation_update, depth, diagnosis_requested, EXPENSIVE_TOOLS, requires_detailed)
from app.content.understanding_service import ContentUnderstandingService
from app.execution.resilient_executor import ResilientAgentExecutor
from app.execution.resilient_tool_executor import ResilientToolExecutor
from app.integrations.dify.entity_scope import enrich_query_scope
from app.domain.phm_point_codes import enrich_phm_point_entity
from app.orchestration.runtime import current_graph_runtime
from app.orchestration.parallel_calls import independent_calls, execute_independent
from app.tools.dify_knowledge import KNOWLEDGE_TOOL_ID
from app.tools.independent_queries import MULTI_QUERY_TOOL_ID
from app.output.customer import render_answer, customer_text
from app.performance import detail_span, emit_performance_event, sanitize_mcp_trace, trace_node
from app.orchestration.entity_lifecycle import (
    can_down_drill_equipment_to_point,
    current_turn_entity,
    has_current_turn_entity_for_level,
    entity_anchor,
)
from app.orchestration.entity_resolution_layer import UnifiedEntityResolutionLayer
from app.orchestration.entity_resolution_layer import EntityLayerOutcome
from app.orchestration.early_entity import classify_with_early_entity
from app.orchestration.completion import (
    STATUS_COMPLETE,
    STATUS_NEED_MORE_EVIDENCE,
    STATUS_PARTIAL_FINAL,
    STATUS_PRESENTATION_ONLY,
    evaluate_completion,
)
from app.orchestration.knowledge_enrichment import needs_enrichment, enrichment_query
from app.orchestration.entity_dependency import (identity_dependency, identity_service_failed, identity_failure_message, structured_anchor)
from app.orchestration.entity_selection_resume import (
    build_point_selection_result,
    build_selected_entity_result,
    points_for_selected_parent,
    selected_entity_is_compatible,
    selection_required_entity_level,
)
from app.orchestration.entity_postrank import (
    candidate_equipment_identity,
    collapse_to_equipment_candidates,
)
from app.orchestration.entity_capability import (
    capability_requirement_for_tool,
    should_expand_equipment_capability,
)
from app.orchestration.supervisor.agent import SupervisorAgent
from app.orchestration.supervisor.capability_boundary import is_referential_entity_followup
from app.orchestration.supervisor.contracts import AgentCall, SupervisorVerdict, SupervisorVerdictType
from app.services.agent_registry_service import AgentRegistryService
from app.tools.alarm_context import (
    active_entity_matches_query,
    entity_satisfies_required_level,
    build_alarm_business_intent,
    build_alarm_workflow_inputs,
    infer_required_entity_level,
)
from app.tools.comprehensive_alarm import COMPREHENSIVE_ALARM_TOOL_ID
from app.tools.health_context import (
    health_query_requests_history,
    health_required_entity_level,
    space_health_history_unsupported,
    infer_health_scope_type,
)
from app.tools.contracts import ToolCallRequest, ToolResultStatus
from app.tools.payload_safety import contains_base64_payload, sanitize_large_payloads
from app.tools.phm_asset_context import (
    asset_identity_available,
    asset_tool_required_entity_level,
    build_phm_asset_arguments,
    enrich_equipment_entity_from_asset_detail,
    format_asset_result,
)
from app.tools.phm_sensor_mcp import PHM_SENSOR_TOOL_IDS, SENSOR_TOOL_BY_OPERATION
from app.tools.phm_sensor_context import sensor_required_level, build_sensor_arguments, format_sensor_result
from app.tools.phm_asset_mcp import (
    PHM_ASSET_QUERY_EQUIPMENT_INFO_TOOL_ID,
    PHM_ASSET_QUERY_POINTS_TOOL_ID,
    PHM_ASSET_QUERY_SCOPE_COLLECTION_TOOL_ID,
    PHM_ASSET_QUERY_SPACE_TREE_TOOL_ID,
    PHM_ASSET_TOOL_IDS,
)
from app.tools.phm_data_context import (
    build_phm_data_arguments,
    format_health_observation,
    resolve_phm_identity_fields,
)
from app.tools.phm_diagnosis_context import (
    build_phm_diagnosis_arguments,
    compact_diagnosis_result,
)
from app.tools.phm_data_mcp import (
    PHM_DATA_TOOL_IDS,
    PHM_CHECK_DATA_AVAILABILITY_TOOL_ID,
    PHM_GET_DATA_SNAPSHOT_TOOL_ID,
    PHM_GET_DEVICE_DATA_TOOL_ID,
    PHM_GET_FEATURE_TREND_TOOL_ID,
    PHM_GET_TEMPERATURE_TREND_TOOL_ID,
    PHM_GET_WAVEFORM_TOOL_ID,
    PHM_QUERY_ALARM_RECORDS_TOOL_ID,
    PHM_QUERY_HEALTH_SCORE_TOOL_ID,
)
from app.tools.phm_diagnosis_mcp import (
    PHM_DIAGNOSIS_ANALYZE_CHART_TOOL_ID,
    PHM_DIAGNOSIS_COMPREHENSIVE_TOOL_ID,
    PHM_DIAGNOSIS_DEVICE_TOOL_ID,
    PHM_DIAGNOSIS_MODEL_ADMISSION_TOOL_ID,
    PHM_DIAGNOSIS_POINT_TOOL_ID,
    PHM_DIAGNOSIS_TOOL_IDS,
)
from app.tools.phm_feature_context import build_phm_feature_arguments, extract_supported_rpm
from app.tools.phm_data_decoder import decode_phm_data_for_public
from app.tools.phm_feature_mcp import PHM_FEATURE_EXTRACT_RPM_TOOL_ID, PHM_FEATURE_TOOL_IDS
from app.tools.phm_workflow_tools import (
    PHM_WORKFLOW_POINT_DIAGNOSIS_BATCH_TOOL_ID,
    PHM_WORKFLOW_POINT_RPM_BATCH_TOOL_ID,
    PHM_WORKFLOW_TOOL_IDS,
    build_workflow_tool_arguments,
    format_workflow_result,
    public_workflow_payload,
    workflow_tool_required_entity_level,
)
from app.tools.registry import ToolRegistry
from app.workflows import BusinessWorkflowRegistry
from app.domain.error_contract import build_layered_error, public_error_payload


class ConversationGraphNodes:
    def __init__(
        self,
        *,
        registry_service: AgentRegistryService,
        content_understanding_service: ContentUnderstandingService,
        supervisor: SupervisorAgent,
        agent_executor: ResilientAgentExecutor,
        tool_registry: ToolRegistry,
        tool_executor: ResilientToolExecutor,
        workflow_registry: BusinessWorkflowRegistry | None = None,
        entity_resolution_layer: UnifiedEntityResolutionLayer | None = None,
    ) -> None:
        self.registry_service = registry_service
        self.content_understanding_service = content_understanding_service
        self.supervisor = supervisor
        self.agent_executor = agent_executor
        self.tool_registry = tool_registry
        self.tool_executor = tool_executor
        self.workflow_registry = workflow_registry
        self.entity_resolution_layer = entity_resolution_layer

    @staticmethod
    def _descriptors(snapshot: dict[str, Any]) -> list[AgentDescriptor]:
        return [AgentDescriptor.model_validate(value) for value in snapshot.values()]

    @classmethod
    def _selected_entity_resume_update(
        cls,
        state: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Apply a server-validated candidate without re-running any language model.

        ``TaskService.select_entity`` only puts a candidate here after checking its
        opaque candidate ID against the pending selection row owned by this task.
        Reinterpreting the original utterance after that explicit user choice would
        re-open a resolved ambiguity and can create an endless selection loop.
        """

        selected = state.get("selected_entity")
        if not state.get("selection_resume") or not isinstance(selected, dict) or not selected:
            return None

        workflow = (
            dict(state.get("business_workflow") or {})
            if isinstance(state.get("business_workflow"), dict)
            else {}
        )
        business_intent = (
            dict(state.get("business_intent") or {})
            if isinstance(state.get("business_intent"), dict)
            else {}
        )
        previous = (
            dict(state.get("entity_result") or {})
            if isinstance(state.get("entity_result"), dict)
            else {}
        )
        required_level = selection_required_entity_level(workflow, previous)
        parent_points = points_for_selected_parent(previous, selected) if required_level == "point" else None
        if parent_points:
            # A parent choice narrows this task's original point candidates. Never
            # reinterpret the wording or fetch unrelated points after that choice.
            point_result = build_point_selection_result(previous, selected, parent_points)
            if len(parent_points) == 1:
                selected_point = parent_points[0]
                resumed = cls._selected_entity_resume_update({
                    **state, "selected_entity": selected_point, "entity_result": point_result,
                })
                # All downstream identity readers must see the point, including
                # readers that intentionally give an explicit selection precedence.
                return {**resumed, "selected_entity": selected_point}
            return {
                "business_intent": business_intent, "business_workflow": workflow,
                "entity_dependency": identity_dependency(state),
                "selected_entity": {}, "resolved_entity": {},
                "entity_result": point_result, "query_scope": point_result["query_scope"],
                "entity_constraints": dict(point_result.get("entity_constraints") or {}),
                "entity_resolution": {
                    "action": "reuse", "source": "user_selection", "anchor_entity": dict(selected),
                    "target_entity_level": "point", "return_mode": "candidates",
                    "reason": point_result["message"], "policy_action": "DOWN_DRILL",
                },
                "observations": [{
                    "call_id": "entity_selection_resume", "call_type": "system",
                    "target_id": "user_entity_selection", "status": "COMPLETED",
                    "answer_markdown": point_result["message"], "can_support_final_answer": False,
                }],
            }
        if not selected_entity_is_compatible(selected, required_level):
            error = build_layered_error(
                code="ENTITY_SELECTION_LEVEL_MISMATCH",
                component="用户实体选择恢复",
                public_message="所选资产与当前业务步骤要求不一致，请重新发起查询。",
                operator_message=(
                    "server-validated candidate does not satisfy stored required level: "
                    f"required={required_level}"
                ),
                workflow_id=str(workflow.get("workflow_id") or "") or None,
                step_id="entity_selection_resume",
                request_id=str(state.get("task_id") or ""),
                retryable=False,
            )
            entity_result = {
                **previous,
                "status": "ERROR",
                "need_lookup": False,
                "need_disambiguation": False,
                "matches": [],
                "match_count": 0,
                "resolved_entity": None,
                "message": error.public_message,
                "error": error.model_dump(mode="json"),
                "resolution_source": "user_selection_guard",
            }
            return {
                "business_intent": business_intent,
                "business_workflow": workflow,
                "entity_resolution": {
                    "action": "none",
                    "source": "user_selection_guard",
                    "anchor_entity": {},
                    "scope_hint": {},
                    "reason": error.public_message,
                    "confidence": 0.0,
                    "policy_action": "ERROR",
                    "target_entity_level": required_level,
                },
                "entity_result": entity_result,
                "resolved_entity": {},
                "observations": [
                    {
                        "call_id": "entity_selection_resume",
                        "call_type": "system",
                        "target_id": "user_entity_selection",
                        "objective": "应用用户确认的数据库资产候选",
                        "status": "FAILED",
                        "answer_markdown": error.public_message,
                        "error_code": error.code,
                        "error_message": error.public_message,
                        "operator_error": error.model_dump(mode="json"),
                        "can_support_final_answer": False,
                    }
                ],
                "errors": [error.model_dump(mode="json")],
            }

        entity_result = build_selected_entity_result(previous, selected)
        query_scope = dict(entity_result.get("query_scope") or {})
        lifecycle = {
            "action": "reuse",
            "source": "user_selection",
            "anchor_entity": dict(selected),
            "scope_hint": {},
            "reason": "用户已从本任务的真实数据库候选中确认目标实体。",
            "confidence": float(entity_result.get("top_similarity") or 1.0),
            "policy_action": "USER_SELECTION",
            "target_entity_level": required_level,
            "return_mode": "single",
            "query_fingerprint": entity_result.get("query_fingerprint"),
        }
        observation = {
            "call_id": "entity_selection_resume",
            "call_type": "system",
            "target_id": "user_entity_selection",
            "objective": "应用用户确认的数据库资产候选",
            "status": "COMPLETED",
            "answer_markdown": "已应用用户确认的资产实体，无需重新执行参数提取或向量检索。",
            "evidence": [{"source_type": "user_entity_selection", "result": entity_result}],
            "can_support_final_answer": True,
            "state_updates": {
                "resolved_entity": dict(selected),
                "entity_result": entity_result,
                "query_scope": query_scope,
            },
        }
        return {
            "business_intent": business_intent,
            "business_workflow": workflow,
            "entity_resolution": lifecycle,
            "entity_result": entity_result,
            "resolved_entity": dict(selected),
            "query_scope": query_scope,
            "entity_constraints": dict(entity_result.get("entity_constraints") or {}),
            "observations": [observation],
        }

    @staticmethod
    def _has_entity_for_level(state: dict[str, Any], required_level: str) -> bool:
        # R37: identity availability is turn-scoped.  After the user explicitly
        # switches objects (entity_resolution=replace), stale resolved/active entities
        # from the previous turn must never suppress a mandatory fuzzy lookup.
        return has_current_turn_entity_for_level(state, required_level)

    def _required_entity_level_for_call(
        self,
        call: AgentCall,
        state: dict[str, Any],
    ) -> str:
        """Return the deterministic identity level required by a concrete business call.

        This is deliberately independent of the LLM plan. A new conversation must not
        be able to execute a PHM business tool with an empty identity merely because the
        planner forgot to add fuzzy-entity.
        """
        if call.call_type != "tool":
            return "none"
        tool_id = call.tool_id or ""
        query = str(state.get("query") or "")
        if tool_id in PHM_SENSOR_TOOL_IDS:
            from app.orchestration.sensor_identity import available
            required = sensor_required_level(state, call.arguments, tool_id)
            return "none" if available(state, required) else required
        if tool_id in PHM_WORKFLOW_TOOL_IDS:
            return workflow_tool_required_entity_level(
                tool_id,
                str(call.arguments.get("required_entity_level") or "none"),
            )
        if tool_id in PHM_ASSET_TOOL_IDS:
            if (
                tool_id == PHM_ASSET_QUERY_SPACE_TREE_TOOL_ID
                and str(call.arguments.get("_identity_source") or "")
                == "equipment_parent"
            ):
                return "equipment"
            return asset_tool_required_entity_level(tool_id)
        if tool_id == PHM_QUERY_ALARM_RECORDS_TOOL_ID or tool_id == COMPREHENSIVE_ALARM_TOOL_ID:
            if (tool_id == PHM_QUERY_ALARM_RECORDS_TOOL_ID
                    and (state.get("sensor_target") or {}).get("asset_identity_available")
                    and str(call.arguments.get("required_entity_level")) == "equipment"):
                # Device alarms supplement the sensor result; a logical sensor
                # point must not force a fabricated physical point prerequisite.
                return "equipment"
            return infer_required_entity_level(
                query,
                str(call.arguments.get("required_entity_level") or "none"),
                self._state_object(state, "query_scope"),
                structured_level=structured_anchor(state),
            )
        if tool_id == PHM_QUERY_HEALTH_SCORE_TOOL_ID:
            entity = (
                state.get("selected_entity")
                or state.get("resolved_entity")
                or state.get("active_entity")
                or {}
            )
            scope_type = infer_health_scope_type(
                query,
                call.arguments,
                entity if isinstance(entity, dict) else {},
                state.get("business_intent") if isinstance(state.get("business_intent"), dict) else {},
            )
            return health_required_entity_level(
                query, scope_type, str(call.arguments.get("required_entity_level") or "none")
            )
        if tool_id in PHM_DATA_TOOL_IDS:
            return "equipment" if tool_id == PHM_GET_DEVICE_DATA_TOOL_ID else "point"
        if tool_id in PHM_FEATURE_TOOL_IDS:
            return "point"
        if tool_id == PHM_DIAGNOSIS_DEVICE_TOOL_ID:
            return "equipment"
        if tool_id in {PHM_DIAGNOSIS_POINT_TOOL_ID, PHM_DIAGNOSIS_ANALYZE_CHART_TOOL_ID}:
            return "point"
        if tool_id == PHM_DIAGNOSIS_MODEL_ADMISSION_TOOL_ID:
            return "none"
        return "none"

    def _repair_missing_entity_dependencies(
        self,
        pending: list[AgentCall],
        state: dict[str, Any],
    ) -> list[AgentCall]:
        """Execution-side identity guard for every PHM business tool.

        Planner output is advisory. If a clean/new conversation has no entity, a PHM
        business call cannot run until a resolver call has completed. Conversely, a
        point-scoped request on a REUSE equipment is left to the Asset-MCP down-drill
        repair instead of adding an unsafe global fuzzy lookup.
        """
        resolver_by_level: dict[str, AgentCall] = {}
        for item in pending:
            if item.call_type == "agent" and item.agent_id == FUZZY_ENTITY_AGENT_ID:
                level = str(item.arguments.get("required_entity_level") or "none").lower()
                resolver_by_level.setdefault(level, item)

        for call in list(pending):
            required_level = self._required_entity_level_for_call(call, state)
            if required_level == "none":
                continue
            call.arguments["required_entity_level"] = required_level
            if self._has_entity_for_level(state, required_level):
                continue
            if (
                required_level == "point"
                and can_down_drill_equipment_to_point(state)
                and self._asset_point_down_drill_enabled()
            ):
                # The next repair inserts query_points inside the anchored equipment.
                continue
            resolver = resolver_by_level.get(required_level)
            if resolver is None:
                resolver = AgentCall(
                    call_id=str(uuid4()),
                    call_type="agent",
                    agent_id=FUZZY_ENTITY_AGENT_ID,
                    objective=f"定位 {call.objective or call.target_id} 所需的真实{required_level}实体",
                    arguments={"required_entity_level": required_level},
                )
                pending.insert(pending.index(call), resolver)
                resolver_by_level[required_level] = resolver
            if resolver.call_id not in call.depends_on:
                call.depends_on.append(resolver.call_id)
        return pending

    def _repair_entity_capability_dependencies(
        self,
        pending: list[AgentCall],
        state: dict[str, Any],
    ) -> list[AgentCall]:
        """Expand equipment-scoped data requests into point-scoped dependencies.

        This is the execution-side Entity Capability Resolver. It does not decide what
        the user meant; it only repairs a concrete tool requirement. When the current
        entity lifecycle says to reuse an equipment and a selected Data MCP tool needs a
        point, Asset MCP resolves that point *inside the anchored equipment*.

        A user who explicitly switches devices still follows the normal fuzzy path: the
        lifecycle action is then ``replace`` and this repair intentionally does nothing.
        """

        if self._has_entity_for_level(state, "point"):
            return pending
        if not self._asset_point_down_drill_enabled():
            return pending

        targets = [
            item
            for item in pending
            if item.call_type == "tool"
            and capability_requirement_for_tool(item.tool_id or "") is not None
            and should_expand_equipment_capability(state, item.tool_id or "")
        ]
        if not targets:
            return pending

        # A single turn may theoretically request temperature and vibration data at the
        # same time. Those are different point scopes and must not share one mutable
        # resolved_entity. The existing diagnosis bundle already handles its mixed data
        # needs after selecting a vibration point, so this generic repair only expands a
        # homogeneous point type. Mixed direct data requests stay planner-controlled
        # rather than risking cross-wiring one point into another tool.
        requirements = [
            capability_requirement_for_tool(item.tool_id or "") for item in targets
        ]
        point_types = {item.point_type for item in requirements if item is not None}
        if len(point_types) != 1:
            return pending

        requirement = next(item for item in requirements if item is not None)
        point_type = requirement.point_type
        existing = next(
            (
                item
                for item in pending
                if item.call_type == "tool"
                and item.tool_id == PHM_ASSET_QUERY_POINTS_TOOL_ID
                and bool(item.arguments.get("_entity_down_drill"))
                and str(item.arguments.get("point_type") or "") == point_type
            ),
            None,
        )
        if existing is None:
            first_index = min(pending.index(item) for item in targets)
            existing = self._point_down_drill_call(
                point_type=point_type,
                objective=requirement.objective,
            )
            pending.insert(first_index, existing)

        # In a reuse turn, a planner-generated point fuzzy lookup is both redundant and
        # unsafe: it may leave the anchored equipment scope. Replace only point-scoped
        # fuzzy dependencies; a fresh entity switch (replace) never enters this branch.
        point_fuzzy_calls = [
            item
            for item in list(pending)
            if item.call_type == "agent"
            and item.agent_id == FUZZY_ENTITY_AGENT_ID
            and str(item.arguments.get("required_entity_level") or "").lower() == "point"
        ]
        fuzzy_ids = {item.call_id for item in point_fuzzy_calls}
        for item in point_fuzzy_calls:
            pending.remove(item)

        for target in targets:
            rewritten = [dep for dep in target.depends_on if dep not in fuzzy_ids]
            if existing.call_id not in rewritten:
                rewritten.append(existing.call_id)
            target.depends_on = rewritten
        for item in pending:
            if item is existing:
                continue
            if any(dep in fuzzy_ids for dep in item.depends_on):
                item.depends_on = [
                    existing.call_id if dep in fuzzy_ids else dep for dep in item.depends_on
                ]
        return pending

    def _repair_normal_diagnosis_dependencies(
        self,
        pending: list[AgentCall],
        state: dict[str, Any],
    ) -> list[AgentCall]:
        """Repair new Diagnosis MCP 1.0.0 prerequisites.

        Device diagnosis is equipment-scoped and uses one get_device_data call. Point
        diagnosis remains point-scoped and uses the snapshot/temperature path.
        """
        diagnosis_calls = [
            item for item in pending
            if item.call_type == "tool"
            and item.tool_id in {PHM_DIAGNOSIS_DEVICE_TOOL_ID, PHM_DIAGNOSIS_POINT_TOOL_ID}
        ]
        if not diagnosis_calls:
            return pending

        # R33 execution guard: if this run contains a whole-device diagnosis, stale
        # point-scoped planner calls are incompatible with the new Diagnosis MCP
        # contract.  Remove them before generic entity dependency repair can turn the
        # resolver into point scope and present a measurement-point selection UI.
        has_device_diagnosis = any(
            item.tool_id == PHM_DIAGNOSIS_DEVICE_TOOL_ID for item in diagnosis_calls
        )
        if has_device_diagnosis:
            point_only_tools = {
                PHM_GET_WAVEFORM_TOOL_ID,
                PHM_GET_FEATURE_TREND_TOOL_ID,
                PHM_GET_TEMPERATURE_TREND_TOOL_ID,
                PHM_CHECK_DATA_AVAILABILITY_TOOL_ID,
                PHM_GET_DATA_SNAPSHOT_TOOL_ID,
                PHM_ASSET_QUERY_POINTS_TOOL_ID,
                PHM_DIAGNOSIS_POINT_TOOL_ID,
                PHM_DIAGNOSIS_ANALYZE_CHART_TOOL_ID,
            } | set(PHM_FEATURE_TOOL_IDS)
            removed_ids = {
                item.call_id
                for item in pending
                if (item.call_type == "tool" and item.tool_id in point_only_tools)
                or (
                    item.call_type == "agent"
                    and item.agent_id == FUZZY_ENTITY_AGENT_ID
                    and str(item.arguments.get("required_entity_level") or "none").lower() == "point"
                )
            }
            if removed_ids:
                pending = [item for item in pending if item.call_id not in removed_ids]
                for item in pending:
                    item.depends_on = [dep for dep in item.depends_on if dep not in removed_ids]
                diagnosis_calls = [
                    item for item in pending
                    if item.call_type == "tool"
                    and item.tool_id in {PHM_DIAGNOSIS_DEVICE_TOOL_ID, PHM_DIAGNOSIS_POINT_TOOL_ID}
                ]

        try:
            transient = current_graph_runtime().transient_tool_payloads
        except Exception:
            transient = {}

        for diagnosis_call in list(diagnosis_calls):
            if diagnosis_call.tool_id == PHM_DIAGNOSIS_DEVICE_TOOL_ID:
                # Equipment-level diagnosis must not be forced down to one point.
                fuzzy = None
                if not self._has_entity_for_level(state, "equipment"):
                    fuzzy = next((
                        x for x in pending
                        if x.call_type == "agent"
                        and x.agent_id == FUZZY_ENTITY_AGENT_ID
                        and str(x.arguments.get("required_entity_level") or "").lower() == "equipment"
                    ), None)
                    if fuzzy is None:
                        fuzzy = next((x for x in pending if x.call_type == "agent" and x.agent_id == FUZZY_ENTITY_AGENT_ID), None)
                    if fuzzy is None:
                        fuzzy = AgentCall(
                            call_id=str(uuid4()), call_type="agent", agent_id=FUZZY_ENTITY_AGENT_ID,
                            objective="定位设备级综合诊断所需的真实设备编码",
                            arguments={"required_entity_level": "equipment"},
                        )
                        pending.insert(pending.index(diagnosis_call), fuzzy)
                    else:
                        # A planner may still emit the old point/area fuzzy dependency.
                        # Device Diagnosis MCP 1.0.0 only needs the equipment identity;
                        # force this resolver to equipment so get_device_data cannot run
                        # against an unresolved/stale device.
                        fuzzy.arguments["required_entity_level"] = "equipment"
                device_data = next((x for x in pending if x.call_type == "tool" and x.tool_id == PHM_GET_DEVICE_DATA_TOOL_ID), None)
                current = transient.get(f"latest:{PHM_GET_DEVICE_DATA_TOOL_ID}")
                if not isinstance(current, dict) or not current:
                    if device_data is None:
                        device_data = AgentCall(
                            call_id=str(uuid4()), call_type="tool", tool_id=PHM_GET_DEVICE_DATA_TOOL_ID,
                            objective="一次获取整设备全部可用测点波形和趋势，供设备综合诊断",
                            arguments={"required_entity_level": "equipment", "trend_days": 30},
                        )
                        pending.insert(pending.index(diagnosis_call), device_data)
                    else:
                        device_data.arguments["required_entity_level"] = "equipment"
                    # get_device_data only depends on equipment identity. Stale point/RPM/
                    # temperature dependencies from an old planner contract must never
                    # block the new whole-device diagnosis pipeline.
                    device_data.depends_on = [fuzzy.call_id] if fuzzy is not None else []
                    diagnosis_call.depends_on = [device_data.call_id]
                else:
                    # The required raw device payload is already in this Worker run.
                    # Diagnosis has no other hard prerequisite.
                    diagnosis_call.depends_on = []
                continue

            # diagnose_point: keep entity continuity and only down-drill inside anchored equipment.
            point_ready = self._has_entity_for_level(state, "point")
            resolver = None
            if not point_ready and can_down_drill_equipment_to_point(state) and self._asset_point_down_drill_enabled():
                resolver = next((x for x in pending if x.call_type == "tool" and x.tool_id == PHM_ASSET_QUERY_POINTS_TOOL_ID and bool(x.arguments.get("_entity_down_drill"))), None)
                if resolver is None:
                    resolver = self._point_down_drill_call()
                    pending.insert(pending.index(diagnosis_call), resolver)
            elif not point_ready:
                resolver = next((x for x in pending if x.call_type == "agent" and x.agent_id == FUZZY_ENTITY_AGENT_ID), None)
                if resolver is None:
                    resolver = AgentCall(
                        call_id=str(uuid4()), call_type="agent", agent_id=FUZZY_ENTITY_AGENT_ID,
                        objective="定位单测点诊断所需的真实测点", arguments={"required_entity_level": "point"},
                    )
                    pending.insert(pending.index(diagnosis_call), resolver)

            snapshot = next((x for x in pending if x.call_type == "tool" and x.tool_id == PHM_GET_DATA_SNAPSHOT_TOOL_ID), None)
            current_snapshot = transient.get(f"latest:{PHM_GET_DATA_SNAPSHOT_TOOL_ID}")
            if not isinstance(current_snapshot, dict) or not current_snapshot:
                if snapshot is None:
                    snapshot = AgentCall(
                        call_id=str(uuid4()), call_type="tool", tool_id=PHM_GET_DATA_SNAPSHOT_TOOL_ID,
                        objective="获取单测点诊断所需波形和特征趋势", arguments={"required_entity_level": "point"},
                    )
                    pending.insert(pending.index(diagnosis_call), snapshot)
                if resolver is not None and resolver.call_id not in snapshot.depends_on:
                    snapshot.depends_on.append(resolver.call_id)
                if snapshot.call_id not in diagnosis_call.depends_on:
                    diagnosis_call.depends_on.append(snapshot.call_id)

            temperature = next((x for x in pending if x.call_type == "tool" and x.tool_id == PHM_GET_TEMPERATURE_TREND_TOOL_ID), None)
            if temperature is None:
                temperature = AgentCall(
                    call_id=str(uuid4()), call_type="tool", tool_id=PHM_GET_TEMPERATURE_TREND_TOOL_ID,
                    objective="补充单测点诊断温度趋势；无数据时允许降级",
                    arguments={"required_entity_level": "point", "days": 30, "return_mode": "auto"},
                )
                pending.insert(pending.index(diagnosis_call), temperature)
            if resolver is not None and resolver.call_id not in temperature.depends_on:
                temperature.depends_on.append(resolver.call_id)
            if temperature.call_id not in diagnosis_call.depends_on:
                diagnosis_call.depends_on.append(temperature.call_id)
        return pending

    @staticmethod
    def _is_soft_diagnosis_dependency(
        downstream: AgentCall,
        dependency_tool_id: str | None,
    ) -> bool:
        return (
            downstream.call_type == "tool"
            and downstream.tool_id
            in {
                PHM_DIAGNOSIS_DEVICE_TOOL_ID,
                PHM_DIAGNOSIS_POINT_TOOL_ID,
                PHM_WORKFLOW_POINT_DIAGNOSIS_BATCH_TOOL_ID,
            }
            and dependency_tool_id
            in {
                PHM_FEATURE_EXTRACT_RPM_TOOL_ID,
                PHM_GET_FEATURE_TREND_TOOL_ID,
                PHM_GET_TEMPERATURE_TREND_TOOL_ID,
                PHM_WORKFLOW_POINT_RPM_BATCH_TOOL_ID,
            }
        )

    async def _emit(
        self,
        state: dict[str, Any],
        event: str,
        data: dict[str, Any],
        *,
        stage: str,
        actor_type: str = "graph",
        actor_id: str | None = None,
        status: str | None = None,
        input_payload: dict[str, Any] | None = None,
        output_payload: dict[str, Any] | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> None:
        runtime = current_graph_runtime().agent_runtime
        await runtime.event_service.publish(
            runtime.task_id,
            event,
            data,
            graph_mode=str(state.get("execution_mode") or runtime.execution_mode),
            stage=stage,
            actor_type=actor_type,
            actor_id=actor_id,
            status=status,
            span_id=state.get("root_span_id"),
            input_payload=input_payload,
            output_payload=output_payload,
            error_code=error_code,
            error_message=error_message,
        )


    async def _stream_supervisor_answer(self, state, *, expert):
        from app.output.streaming import AnswerStreamSession, AnswerStreamStopped
        from app.integrations.openai.chat_client import ModelStreamDelta
        from app.content.attachment_analysis import finish as finish_attachment_analysis
        runtime = current_graph_runtime()
        await runtime.set_task_streaming()
        settings = runtime.agent_runtime.settings
        image_sources = state.get("attachments") or state.get("understanding_results") or []
        if (any(x.get("kind")=="image" for x in image_sources) and self.content_understanding_service is not None and not runtime.transient_tool_payloads.get("_final_model_images")
                and settings.file_allow_native_model_upload and settings.file_external_model_policy != "NEVER_SEND_ORIGINAL"):
            from app.integrations.openai.chat_client import MultimodalInput
            owned = self.content_understanding_service.attachment_service
            for attachment in image_sources:
                if attachment.get("kind") == "image" and attachment.get("attachment_id"):
                    try:
                        desc, data = await owned.read_owned(attachment_id=UUID(str(attachment["attachment_id"])),
                            user_token=runtime.agent_runtime.user_token)
                        runtime.transient_tool_payloads.setdefault("_final_model_images", []).append(MultimodalInput(desc,data))
                    except Exception:
                        # Persisted extracted evidence remains usable if the original is unavailable.
                        continue
        async with detail_span(state, code="supervisor.final.llm", name="主控大模型：生成最终回答",
                description="实时输出模型提供的推理和正式回答", category="llm") as perf:
            session = AnswerStreamSession(state, runtime, span_id=perf["span_id"] if perf else None)
            async def events():
                from app.services.asset_collections import fact_text
                from app.output.asset_commentary import AssetCommentaryStream
                prefix = state.get("query_fact_text") or fact_text(state.get("asset_query_result") or {})
                guard = AssetCommentaryStream() if prefix else None
                if prefix:
                    state["asset_query_facts_emitted"] = not bool(state.get("query_fact_text"))
                    state["query_facts_ready"] = bool(state.get("query_fact_text"))
                    yield ModelStreamDelta("final", prefix + "\n\n")
                    completion_contract = (state.get("business_intent",{}).get("completion_contract") or {})
                    if (
                        ((state.get("business_intent",{}).get("result_followup") or {}).get("action") == "render")
                        or completion_contract.get("response_mode") == "facts_only"
                    ) and state.get("query_fact_text"):
                        return
                await finish_attachment_analysis(state,self.supervisor)
                if callable(getattr(self.supervisor, "synthesize_events", None)):
                    async for event in self.supervisor.synthesize_events(state, expert=expert):
                        if guard and event.channel == "final":
                            value = guard.feed(event.content)
                            if value:
                                yield ModelStreamDelta("final", value)
                        else:
                            yield event
                else:
                    async for text in self.supervisor.synthesize_stream(state, expert=expert):
                        value = guard.feed(text) if guard else text
                        if value:
                            yield ModelStreamDelta("final", value)
                if guard:
                    tail = guard.feed("", final=True)
                    if tail:
                        yield ModelStreamDelta("final", tail)
            try:
                return await session.run(events())
            except AnswerStreamStopped:
                state["final_status"] = "STOPPED"
                if perf is not None:
                    perf["status"] = "CANCELLED"
                return session.meta["content"]
            finally:
                if perf is not None:
                    perf["metrics"].update({key:value for key,value in session.meta.items()
                        if key in {"first_reasoning_ms", "first_final_ms", "reasoning_available"}})
                    perf["metrics"]["ttft_ms"] = session.meta.get("first_final_ms")
                    perf["metrics"]["output_chars"] = len(session.meta["content"])

    async def _ensure_business_knowledge(self, state):
        """One bounded final enrichment; never consumes the business-call budget."""
        runtime = current_graph_runtime().agent_runtime
        if not needs_enrichment(state, runtime.settings):
            return state, []
        enabled = any(d.tool_id == KNOWLEDGE_TOOL_ID and d.enabled for d in self.tool_registry.list_descriptors())
        if not enabled:
            return {**state, "knowledge_enrichment_status": "NOT_CONFIGURED"}, []
        budget = min(float(runtime.settings.dify_knowledge_timeout_seconds), float(getattr(
            runtime.settings, "dify_knowledge_enrichment_timeout_seconds", 8)))
        intent = state.get("business_intent") or {}
        call = AgentCall(call_id=str(uuid4()), call_type="tool", tool_id=KNOWLEDGE_TOOL_ID,
            objective="在业务结果基础上补充企业规程、原理与案例，提供可核对的文献依据",
            arguments={"query": enrichment_query(state), "knowledge_bases": intent.get("knowledge_bases") or []})
        async with detail_span(state, code="knowledge.enrich", name="补充企业知识与参考文献",
                description="业务完成后检索相关资料；有独立时间预算，失败不影响已取得的业务事实", category="retrieval") as span:
            try:
                async with asyncio.timeout(budget + 1):
                    update = await self._execute_call({**state, "_knowledge_timeout_seconds": budget}, call)
                rows = list(update.get("observations") or [])
            except Exception:
                rows = [{"call_id": call.call_id, "call_type": "tool", "tool_id": KNOWLEDGE_TOOL_ID,
                    "status": "FAILED", "answer_markdown": "本轮知识库补充未完成，未取得可引用依据。",
                    "can_support_final_answer": False}]
            if not rows:
                rows = [{"call_id": call.call_id, "tool_id": KNOWLEDGE_TOOL_ID, "status": "FAILED",
                         "can_support_final_answer": False}]
            if span is not None:
                span["metrics"].update(status=rows[-1].get("status"), budget_seconds=budget)
        return {**state, "observations": list(state.get("observations") or []) + rows}, rows

    @trace_node("context.load")
    async def load_context(self, state: dict[str, Any]) -> dict[str, Any]:
        snapshot = await self.registry_service.snapshot()
        root_span_id = str(state.get("root_span_id") or uuid4())
        update = {"registry_snapshot": snapshot, "root_span_id": root_span_id}
        state_for_event = {**state, **update}
        await self._emit(
            state_for_event,
            "graph.started",
            {
                "task_id": state["task_id"],
                "graph_mode": state.get("execution_mode"),
                "span_id": root_span_id,
            },
            stage="load_context",
            status="STARTED",
        )
        # Make memory wiring observable without exposing the memory contents.  These
        # exact state objects are subsequently consumed by the Supervisor classifier
        # and planning/final synthesis contexts.
        memory = state.get("memory_context") if isinstance(state.get("memory_context"), dict) else {}
        await self._emit(
            state_for_event,
            "context.loaded",
            {
                "summary_chars": len(str(memory.get("summary") or "")),
                "recent_message_count": len(list(memory.get("recent_messages") or [])),
                "recent_entity_count": len(list(memory.get("recent_entities") or [])),
                "user_profile_present": bool(state.get("user_profile")),
                "pending_clarification_present": bool(memory.get("pending_clarification")),
                "injected_into_supervisor": True,
            },
            stage="load_context",
            actor_type="context",
            actor_id="conversation_memory",
            status="COMPLETED",
        )
        return update

    @trace_node("context.topic.activate")
    async def activate_evidence_context(self, state: dict[str, Any]) -> dict[str, Any]:
        runtime = current_graph_runtime()
        service = getattr(runtime, "workspace_service", None)
        if service is None or not bool(getattr(runtime.agent_runtime.settings, "evidence_fabric_enabled", True)):
            return {}
        classification = dict(state.get("business_intent") or {})
        from uuid import UUID
        update = await service.activate_turn(
            conversation_id=UUID(str(state["conversation_id"])),
            task_id=UUID(str(state["task_id"])),
            query=str(state.get("query") or ""),
            classification=classification,
            state=state,
        )
        decision = dict(update.get("context_resolution") or {})
        classification["context_resolution"] = decision
        memory = dict(state.get("memory_context") or {})
        memory["current_topic_id"] = (update.get("topic_workspace") or {}).get("topic_id")
        memory["evidence_catalog"] = list(update.get("evidence_catalog") or [])
        result = {**update, "business_intent": classification, "memory_context": memory}
        event_type = "context.topic.created" if (update.get("topic_workspace") or {}).get("created") else (
            "context.topic.resumed" if str(decision.get("action") or "").startswith("RESUME") else "context.topic.resolved"
        )
        await self._emit(
            {**state, **result},
            event_type,
            {
                "topic_id": (update.get("topic_workspace") or {}).get("topic_id"),
                "context_action": decision.get("action"),
                "context_reason": decision.get("reason"),
                "evidence_catalog_count": len(update.get("evidence_catalog") or []),
                "requirements": update.get("evidence_requirements") or [],
            },
            stage="context_resolution",
            actor_type="context",
            actor_id="evidence_workspace",
            status="COMPLETED",
            output_payload={
                "topic_workspace": update.get("topic_workspace") or {},
                "task_delta": update.get("task_delta") or {},
                "evidence_requirements": update.get("evidence_requirements") or [],
            },
        )
        await self._emit(
            {**state, **result},
            "planner.requirements.created",
            {
                "topic_id": (update.get("topic_workspace") or {}).get("topic_id"),
                "requirement_count": len(update.get("evidence_requirements") or []),
            },
            stage="planning",
            actor_type="planner",
            actor_id="task_compiler",
            status="COMPLETED",
            output_payload={"requirements": update.get("evidence_requirements") or []},
        )
        return result

    async def _classify_for_entity(self, state):
        if self.workflow_registry is None:
            return {}, None
        async with detail_span(
            state, code="supervisor.classify.llm", name="主控大模型：问题理解与路由",
            description="提取业务目标和结构化语义；可与轻量对象识别及实体检索重叠", category="llm",
            metrics={"model": current_graph_runtime().agent_runtime.settings.supervisor_model or ""},
        ) as perf:
            classification, selection = await self.supervisor.classify_business_workflow(state, self.workflow_registry)
            if perf is not None:
                perf["metrics"].update({
                    "workflow_id": getattr(selection, "workflow_id", None),
                    "variant_id": getattr(selection, "variant_id", None),
                    **{key: classification.get(key) for key in ("routing_status", "classification_attempts",
                        "classification_retried", "classification_input_chars", "classification_reasoning_enabled")},
                })
        return classification, selection

    @trace_node("entity.resolve")
    async def resolve_entity_context(self, state: dict[str, Any]) -> dict[str, Any]:
        if state.get("diagnosis_resume_state") and (state.get("diagnosis_choice") or {}).get("source") == "user_confirmation":
            saved = state["diagnosis_resume_state"]
            return apply_depth({**saved, "diagnosis_choice":state["diagnosis_choice"],
                "entity_resolution": {"required": bool(saved.get("resolved_entity")),
                    "target_entity_level": (saved.get("business_workflow") or {}).get("required_entity_level", "any")}})
        """Run the single Asset-MCP-backed entity decision layer for this turn."""

        routing_pending = bool((state.get("business_intent") or {}).get("routing_pending"))
        selection_resume = self._selected_entity_resume_update(state)
        from app.orchestration.entity_lifecycle import has_explicit_different_code
        if selection_resume and has_explicit_different_code(str(state.get("query") or ""), selection_resume.get("resolved_entity")):
            selection_resume = None
            state = {**state, "selected_entity": {}, "selection_resume": False}
        if selection_resume is not None and not routing_pending:
            workflow = dict(selection_resume.get("business_workflow") or {})
            entity_result = dict(selection_resume.get("entity_result") or {})
            failed = str(entity_result.get("status") or "").upper() == "ERROR"
            await self._emit(
                {**state, **selection_resume},
                "entity.selection.applied" if not failed else "entity.selection.rejected",
                {
                    "workflow_id": workflow.get("workflow_id"),
                    "workflow_variant": workflow.get("variant_id"),
                    "target_entity_level": (
                        selection_resume.get("entity_resolution") or {}
                    ).get("target_entity_level"),
                    "resolution_source": entity_result.get("resolution_source"),
                    "selected_from_multiple": bool(
                        entity_result.get("selected_from_multiple")
                    ),
                    "skipped_workflow_classifier": True,
                    "skipped_asset_llm": True,
                    "skipped_embedding": True,
                    "skipped_reranker": True,
                },
                stage="entity_selection",
                actor_type="system",
                actor_id="user_entity_selection",
                status="FAILED" if failed else "COMPLETED",
                output_payload={
                    "entity_resolution": selection_resume.get("entity_resolution") or {},
                    "entity_result": entity_result,
                },
            )
            return selection_resume

        prefetched = None
        early_selection = False
        settings = current_graph_runtime().agent_runtime.settings
        if selection_resume is not None:
            # An early choice carries a pending business route. Apply that verified
            # choice first, then understand the business goal without re-opening it.
            state = {**state, **selection_resume}
        use_early = bool(getattr(settings, "supervisor_early_entity_enabled", False)
            and self.workflow_registry is not None and self.entity_resolution_layer is not None
            and not state.get("selection_resume") and not state.get("pending_diagnosis_context") and not state.get("attachments")
            and not state.get("understanding_results")
            and not (state.get("memory_context") or {}).get("recent_results")
            and not (state.get("memory_context") or {}).get("pending_clarification")
            and callable(getattr(self.supervisor, "understand_entity_target", None)))
        if use_early:
            classification, selection, prefetched, early_selection = await classify_with_early_entity(
                self, state, lambda: self._classify_for_entity(state))
        else:
            classification, selection = await self._classify_for_entity(state)
        classification = {key:value for key,value in classification.items() if key not in {
            "needs_clarification", "clarification_question", "clarification_missing_fields", "clarification_reason",
        }}
        previous = state.get("pending_diagnosis_context") or {}
        saved = previous.get("resume_state") or {}
        if (saved and not state.get("attachments") and classification.get("responds_to_diagnosis_choice")
                and depth({"business_intent":classification}) in {"quick", "detailed"}):
            # Only the semantic classifier may interpret a plain-text choice. A new
            # entity/question must take the ordinary authoritative resolution path.
            chosen_intent = {**dict(saved.get("business_intent") or {}),
                **{key:classification[key] for key in ("analysis_depth", "analysis_depth_explicit", "analysis_depth_confidence")}}
            return apply_depth({**saved, "business_intent":chosen_intent,
                "pending_diagnosis_context":{}, "diagnosis_choice":{},
                "query":str(saved.get("query") or previous.get("query") or state.get("query"))})
        selection_payload = selection.model_dump(mode="json") if selection else {}
        routing_failed = classification.get("routing_status") == "error"
        await self._emit(
            state,
            (
                "workflow.classification.deferred"
                if early_selection
                else "workflow.classification.failed"
                if routing_failed
                else "workflow.selected"
                if selection
                else "workflow.not_selected"
            ),
            {
                **selection_payload,
                "classification": classification,
            },
            stage="workflow_classification" if routing_failed else "workflow_selection",
            actor_type="workflow",
            actor_id=selection.workflow_id if selection else "dynamic",
            status="DEFERRED" if early_selection else "FAILED" if routing_failed else "COMPLETED",
        )

        if routing_failed:
            update = {
                "business_intent": classification,
                "business_workflow": {},
                "entity_result": {
                    "status": "ERROR",
                    "message": "业务链路语义分类未成功，本轮已停止，未使用关键词规则降级路由。",
                    "error": {
                        "code": "WORKFLOW_LLM_CLASSIFICATION_FAILED",
                        "public_message": "本轮问题理解服务暂时不可用，请稍后重试。",
                        "operator_message": (
                            "supervisor workflow classifier returned no valid structured decision"
                        ),
                    },
                },
                "observations": [],
            }
            return update

        if (len(classification.get("independent_queries") or []) >= 2
                and any(t.tool_id == MULTI_QUERY_TOOL_ID and t.enabled for t in self.tool_registry.list_descriptors())):
            # A single global entity cannot represent independently named targets.
            # Each batch item resolves against its own target before any data call.
            return {"business_intent": classification, "business_workflow": {},
                    "entity_result": {"status": "NO_LOOKUP", "need_lookup": False},
                    "observations": [], "clarification_context": {}}

        from app.orchestration.sensor_identity import prepare_identity
        sensor_update = await prepare_identity(self, state, classification)
        if sensor_update is not None:
            await self._emit({**state, **sensor_update}, "entity.resolution.completed",
                {"status": "COMPLETED", "source": "sensor_identity_verification"},
                stage="entity_resolution", actor_type="system", actor_id="unified_entity_resolution_layer",
                status="COMPLETED", output_payload={"sensor_target": sensor_update["sensor_target"],
                    "identity_resolution": sensor_update["sensor_identity_resolution"]})
            return sensor_update

        # The same declared identity dependency governs Recipes and dynamic goals.
        asset_semantics = classification.get("asset_semantics") or {}
        dependency = identity_dependency({"business_intent": classification, "business_workflow": selection_payload})
        anchor_level = dependency["level"]
        if selection_resume is not None and entity_satisfies_required_level(
                selection_resume.get("resolved_entity") or {}, anchor_level):
            prefetched = EntityLayerOutcome(
                updates={key: value for key, value in selection_resume.items()
                         if key not in {"observations", "business_intent", "business_workflow"}},
                observation=(selection_resume.get("observations") or [{}])[0],
            )
        workflow_needs_asset = bool(selection is not None and selection.required_entity_level != "none")
        model_needs_asset = bool(dependency["required"])
        if not model_needs_asset and not workflow_needs_asset:
            reason = "主控模型判定本轮不需要绑定真实资产；已跳过 Asset MCP。"
            skipped = {
                "entity_dependency": dependency,
                "clarification_context": {},
                "business_intent": classification,
                "business_workflow": selection_payload,
                "entity_resolution": {
                    "action": "none",
                    "source": "supervisor_asset_gate",
                    "anchor_entity": {},
                    "scope_hint": {},
                    "reason": reason,
                    "confidence": float(classification.get("confidence") or 0.0),
                    "policy_action": "SKIP",
                    "target_entity_level": "any",
                    "return_mode": "none",
                },
                "entity_result": {
                    "status": "NO_LOOKUP",
                    "need_lookup": False,
                    "need_disambiguation": False,
                    "matches": [],
                    "match_count": 0,
                    "resolved_entity": None,
                    "message": reason,
                    "resolution_source": "supervisor_asset_gate",
                    "entity_constraints": asset_semantics,
                },
                "resolved_entity": {},
                "query_scope": {},
                "entity_constraints": asset_semantics,
                "observations": [],
            }
            await self._emit(
                {**state, **skipped},
                "entity.resolution.skipped",
                {
                    "action": "SKIP",
                    "target_entity_level": "any",
                    "return_mode": "none",
                    "status": "NO_LOOKUP",
                    "reason": reason,
                    "workflow_id": selection.workflow_id if selection else None,
                    "needs_asset_lookup": False,
                    "skipped_asset_mcp": True,
                },
                stage="entity_resolution",
                actor_type="system",
                actor_id="supervisor_asset_gate",
                status="COMPLETED",
                output_payload={
                    "entity_resolution": skipped["entity_resolution"],
                    "entity_result": skipped["entity_result"],
                },
            )
            return skipped

        if self.entity_resolution_layer is None:
            raise RuntimeError("UnifiedEntityResolutionLayer must be configured")
        asset_semantics = {**asset_semantics, "needs_asset_lookup": True}
        classification = {**classification, "asset_semantics": asset_semantics}
        enriched_state = {
            **state,
            "entity_dependency": dependency,
            "business_intent": classification,
            "business_workflow": selection_payload,
        }
        async with detail_span(
            state, code="asset.resolve.mcp", name="Asset MCP：实体检索",
            description="调用 Asset MCP 完成语义理解、Embedding、PostgreSQL 候选召回、Reranker 精排和实体决策",
            category="mcp", metrics={"service": "phm-asset-mcp"},
        ) as perf:
            # Recipes resolve their declared input identity. Dynamic goals resolve
            # their anchor, so a requested collection member is still discovered
            # downstream instead of being required as a singular input.
            required_anchor_level = anchor_level
            if required_anchor_level in {"none", "any"} and workflow_needs_asset:
                required_anchor_level = str(selection.required_entity_level or "any")
            if required_anchor_level == "none":
                required_anchor_level = "any"
            outcome = prefetched or await self.entity_resolution_layer.resolve(
                enriched_state, required_entity_level=required_anchor_level,
            )
            if perf is not None:
                if (outcome.updates.get("entity_result") or {}).get("status") == "ERROR":
                    perf["status"] = "FAILED"
                perf["metrics"].update({
                    "reused_early_lookup": bool(prefetched),
                    "early_selection": early_selection,
                    "entity_status": str((outcome.updates.get("entity_result") or {}).get("status") or ""),
                    "match_count": int((outcome.updates.get("entity_result") or {}).get("match_count") or 0),
                })
        query_scope_for_perf = outcome.updates.get("query_scope") if isinstance(outcome.updates.get("query_scope"), dict) else {}
        asset_perf_trace = query_scope_for_perf.pop("_performance_trace", None) if isinstance(query_scope_for_perf, dict) else None
        asset_execution_trace = None
        if (
            asset_perf_trace
            and current_graph_runtime().agent_runtime.settings.performance_monitor_enabled
            and current_graph_runtime().agent_runtime.settings.performance_trace_include_mcp_details
        ):
            asset_execution_trace = sanitize_mcp_trace(
                asset_perf_trace,
                current_graph_runtime().agent_runtime.settings.performance_trace_max_mcp_spans,
            )
        if selection is not None and selection.required_entity_level == "point":
            entity_result = dict(outcome.updates.get("entity_result") or {})
            entity_status = str(entity_result.get("status") or "").upper()
            resolved_entity = outcome.updates.get("resolved_entity")
            level_mismatch = entity_status in {"UNIQUE", "NO_LOOKUP"} and not (
                isinstance(resolved_entity, dict)
                and entity_satisfies_required_level(resolved_entity, "point")
            )
            if entity_status == "MULTIPLE":
                matches = [
                    item
                    for item in entity_result.get("matches") or []
                    if isinstance(item, dict)
                ]
                level_mismatch = not matches or not all(
                    entity_satisfies_required_level(item, "point") for item in matches
                )
            if level_mismatch:
                error = build_layered_error(
                    code="ENTITY_LEVEL_MISMATCH",
                    component="PHM 测点实体解析",
                    public_message=(
                        "尚未从真实资产候选中确认具体测点，本次诊断已在数据读取前停止。"
                        "请重新选择测点或补充部件、位置、方向。"
                    ),
                    operator_message=(
                        "point workflow received an entity result that does not contain "
                        "both equip_no and point identity"
                    ),
                    workflow_id=selection.workflow_id,
                    step_id="entity_resolution",
                    request_id=str(state.get("task_id") or ""),
                    retryable=False,
                )
                entity_result.update(
                    {
                        "status": "ERROR",
                        "need_disambiguation": False,
                        "matches": [],
                        "match_count": 0,
                        "resolved_entity": None,
                        "message": error.public_message,
                        "error": error.model_dump(mode="json"),
                        "resolution_source": "required_entity_level_guard",
                    }
                )
                outcome.updates.update(
                    {
                        "entity_result": entity_result,
                        "resolved_entity": {},
                        "errors": list(outcome.updates.get("errors") or [])
                        + [error.model_dump(mode="json")],
                    }
                )
                state_updates = dict(outcome.observation.get("state_updates") or {})
                state_updates.update(
                    {"entity_result": entity_result, "resolved_entity": {}}
                )
                outcome.observation.update(
                    {
                        "status": "FAILED",
                        "answer_markdown": error.public_message,
                        "error_code": error.code,
                        "error_message": error.public_message,
                        "operator_error": error.model_dump(mode="json"),
                        "can_support_final_answer": False,
                        "state_updates": state_updates,
                    }
                )
        if (
            selection is not None
            and selection.workflow_id == "diagnosis_analysis"
            and selection.variant_id == "point"
            and str((outcome.updates.get("entity_result") or {}).get("status") or "").upper()
            == "MULTIPLE"
        ):
            candidates = list((outcome.updates.get("entity_result") or {}).get("matches") or [])
            parent_ids = [
                candidate_equipment_identity(item)
                for item in candidates
                if isinstance(item, dict)
            ]
            multiple_parent_devices = (
                bool(candidates)
                and len(parent_ids) == len(candidates)
                and all(parent_ids)
                and len(set(parent_ids)) > 1
            )
            entity_constraints = outcome.updates.get("entity_constraints") or {}
            explicit_component = str(
                entity_constraints.get("component_expression")
                or entity_constraints.get("component_keyword")
                or ""
                if isinstance(entity_constraints, dict)
                else ""
            ).strip()
            if multiple_parent_devices:
                equipment_candidates = collapse_to_equipment_candidates(
                    [dict(item) for item in candidates if isinstance(item, dict)]
                )
                query_scope = dict(outcome.updates.get("query_scope") or {})
                query_scope.update(
                    {
                        "target_entity_type": "equipment",
                        "return_mode": "candidates",
                    }
                )
                entity_result = dict(outcome.updates.get("entity_result") or {})
                entity_result.update(
                    {
                        "status": "MULTIPLE",
                        "need_disambiguation": True,
                        "matches": equipment_candidates,
                        "match_count": len(equipment_candidates),
                        "resolved_entity": None,
                        "message": (
                            "找到多台同名设备。请先选择目标设备；确认设备后，系统再在该设备"
                            "内部选择一个符合部件条件的测点进行诊断。"
                        ),
                        "resolution_source": "parent_equipment_disambiguation",
                        # Retain the actual point recall for the next selection
                        # stage; a parent projection alone cannot finish diagnosis.
                        "point_selection_candidates": candidates,
                        "return_mode": "candidates",
                        "query_scope": query_scope,
                    }
                )
                outcome.updates.update(
                    {
                        "entity_result": entity_result,
                        "resolved_entity": {},
                        "query_scope": query_scope,
                    }
                )
                state_updates = dict(outcome.observation.get("state_updates") or {})
                state_updates.update(
                    {
                        "entity_result": entity_result,
                        "resolved_entity": {},
                        "query_scope": query_scope,
                    }
                )
                outcome.observation.update(
                    {
                        "answer_markdown": entity_result["message"],
                        "can_support_final_answer": False,
                        "state_updates": state_updates,
                    }
                )
            else:
                # Asset MCP has returned more than one real point for the already
                # resolved equipment.  A language model must never silently turn
                # that ambiguity into an asset identity decision: every candidate is
                # returned to the operator for an explicit selection.
                entity_result = dict(outcome.updates.get("entity_result") or {})
                entity_result.update(
                    {
                        "status": "MULTIPLE",
                        "need_disambiguation": True,
                        "message": (
                            f"找到多个{explicit_component or ''}测点。请选择需要诊断的具体测点；"
                            "确认后系统将执行数据读取、转速识别和单测点诊断。"
                        ),
                        "resolution_source": "point_candidate_disambiguation",
                        "return_mode": "candidates",
                    }
                )
                outcome.updates.update(
                    {
                        "entity_result": entity_result,
                        "resolved_entity": {},
                    }
                )
                state_updates = dict(outcome.observation.get("state_updates") or {})
                state_updates.update(
                    {
                        "entity_result": entity_result,
                        "resolved_entity": {},
                    }
                )
                outcome.observation.update(
                    {
                        "answer_markdown": entity_result["message"],
                        "can_support_final_answer": False,
                        "state_updates": state_updates,
                    }
                )
        update = {
            **outcome.updates,
            "entity_dependency": dependency,
            "clarification_context": {},
            "business_intent": classification,
            "business_workflow": selection_payload,
            "observations": [outcome.observation],
        }
        decision = dict(update.get("entity_resolution") or {})
        await self._emit(
            {**state, **update},
            "entity.resolution.completed",
            {
                "action": decision.get("policy_action") or decision.get("action"),
                "target_entity_level": decision.get("target_entity_level"),
                "return_mode": decision.get("return_mode"),
                "status": (update.get("entity_result") or {}).get("status"),
                "reason": decision.get("reason"),
                "workflow_id": selection.workflow_id if selection else None,
                "internal_monitor_enabled": bool(asset_execution_trace),
                "execution_trace": asset_execution_trace,
                "early_selection": early_selection,
                "routing_pending": bool(classification.get("routing_pending")),
            },
            stage="entity_resolution",
            actor_type="system",
            actor_id="unified_entity_resolution_layer",
            status="COMPLETED" if (update.get("entity_result") or {}).get("status") != "ERROR" else "FAILED",
            output_payload={
                "entity_dependency": dependency,
                "entity_resolution": decision,
                "entity_result": update.get("entity_result") or {},
            },
        )
        return update

    @classmethod
    def route_after_entity_resolution(cls, state: dict[str, Any]) -> str:
        result = state.get("entity_result") if isinstance(state.get("entity_result"), dict) else {}
        status = str(result.get("status") or "").upper()
        business_intent = (
            state.get("business_intent")
            if isinstance(state.get("business_intent"), dict)
            else {}
        )
        if business_intent.get("routing_status") == "error":
            return "failure"
        if status == "MULTIPLE" and len(result.get("matches") or []) > 1:
            return "selection"
        workflow = state.get("business_workflow") if isinstance(state.get("business_workflow"), dict) else {}
        if status == "COLLECTION" and workflow and not bool(workflow.get("supports_collection")):
            return "failure"
        if identity_service_failed(state):
            return "failure"
        if status in {"NOT_FOUND", "ENTITY_NOT_FOUND"} and (state.get("entity_dependency") or identity_dependency(state)).get("required"):
            return "failure"
        return "continue"

    async def entity_resolution_failure(self, state: dict[str, Any]) -> dict[str, Any]:
        result = state.get("entity_result") if isinstance(state.get("entity_result"), dict) else {}
        raw_error = result.get("error") if isinstance(result.get("error"), dict) else {}
        workflow = state.get("business_workflow") if isinstance(state.get("business_workflow"), dict) else {}
        collection_unsupported = (
            str(result.get("status") or "").upper() == "COLLECTION"
            and workflow
            and not bool(workflow.get("supports_collection"))
        )
        public_message = str(
            (
                "当前链路暂不支持一次处理多个实体，请缩小范围或指定单个对象。"
                if collection_unsupported
                else raw_error.get("public_message") or result.get("message")
            )
            or "未能定位本次业务查询所需的区域、设备或测点，请补充更完整的自然语言名称或范围；系统会负责对应真实资产编码。"
        )
        operator_message = str(
            (
                f"entity resolver returned collection(count={result.get('match_count')}) "
                f"to singular workflow {workflow.get('workflow_id')}/{workflow.get('variant_id')}"
                if collection_unsupported
                else raw_error.get("operator_message") or result.get("message")
            )
            or "Asset MCP entity resolution returned no usable entity"
        )
        code = str(
            "ENTITY_COLLECTION_UNSUPPORTED"
            if collection_unsupported
            else raw_error.get("code") or "ENTITY_RESOLUTION_FAILED"
        )
        await self._emit(
            state,
            "workflow.failed",
            {
                "workflow_id": workflow.get("workflow_id"),
                "step_id": "entity_resolution",
                "public_message": public_message,
                "operator_message": operator_message,
                "error_code": code,
            },
            stage="entity_resolution",
            actor_type="workflow",
            actor_id=str(workflow.get("workflow_id") or "dynamic"),
            status="FAILED",
            error_code=code,
            error_message=operator_message,
        )
        if str(result.get("status") or "").upper() == "ERROR" and not state.get("understanding_results"):
            public_message = identity_failure_message(state)
            answer = render_answer(public_message, state)
            await current_graph_runtime().set_task_streaming()
            await current_graph_runtime().agent_runtime.event_service.publish(
                state["task_id"], "answer.delta", {"content": answer, "channel": "final"},
                graph_mode=state.get("execution_mode") or "normal", actor_type="system",
                actor_id="entity_dependency", status="FAILED",
            )
            return {"final_answer": answer, "final_status": "FAILED"}

        # Entity failure prevents identity-dependent reads, not useful explanation.
        # Keep the failure as evidence, optionally retrieve independent knowledge,
        # and answer the part supported by general knowledge/attachments.
        fallback = {**state, "business_workflow": {}, "resolved_entity": {},
                    "selected_entity": {}, "active_entity": {},
                    "available_tool_ids": [d.tool_id for d in self.tool_registry.list_descriptors() if d.enabled],
                    "observations": list(state.get("observations") or []) + [{
                        "call_type":"agent", "agent_id":FUZZY_ENTITY_AGENT_ID,
                        "status":"FAILED", "answer_markdown":customer_text(public_message),
                        "can_support_final_answer":False,
                    }]}
        fallback, _ = await self._ensure_business_knowledge(fallback)
        answer = await self._stream_supervisor_answer(fallback, expert=False)
        return {"final_answer":answer, "final_status":"COMPLETED", "observations":fallback["observations"], "understanding_results":fallback.get("understanding_results") or []}

    def _workflow_failure_answer(self, state: dict[str, Any]) -> str | None:
        workflow = state.get("business_workflow") if isinstance(state.get("business_workflow"), dict) else {}
        workflow_id = str(workflow.get("workflow_id") or "")
        if not workflow_id:
            return None
        failures: list[dict[str, Any]] = []
        for item in state.get("observations") or []:
            if not isinstance(item, dict) or str(item.get("workflow_id") or "") != workflow_id:
                continue
            if str(item.get("status") or "").upper() not in {"FAILED", "ERROR", "TIMEOUT", "NEEDS_INPUT", "REJECTED"}:
                continue
            step_id = str(item.get("workflow_step_id") or "unknown")
            if self.workflow_registry is not None and not self.workflow_registry.is_hard_failure(workflow, step_id):
                continue
            failures.append(item)
        if not failures:
            return None

        # Stopping a workflow appends synthetic dependency failures for downstream
        # steps. Prefer the concrete upstream failure so operators see the root cause.
        root = next(
            (
                item
                for item in reversed(failures)
                if str(item.get("error_code") or "").upper()
                not in {"DEPENDENCY_FAILED", "DEPENDENCY_UNRESOLVED"}
            ),
            failures[-1],
        )
        step_id = str(root.get("workflow_step_id") or "unknown")
        operator = root.get("operator_error") if isinstance(root.get("operator_error"), dict) else {}
        public_message = str(
            operator.get("public_message")
            or root.get("error_message")
            or root.get("answer_markdown")
            or "本次业务链路未能完整执行。"
        )
        operator_message = str(
            operator.get("operator_message")
            or root.get("error_message")
            or "workflow step failed without operator detail"
        )
        code = str(operator.get("code") or root.get("error_code") or "WORKFLOW_STEP_FAILED")
        return customer_text(public_message)

    @staticmethod
    def _attach_workflow_error(
        state: dict[str, Any],
        call: AgentCall,
        observations: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Attach workflow identity and the stable two-audience error contract.

        Some deterministic preflight guards return before the resilient executor.
        Normalizing here guarantees those failures have the same contract as MCP,
        agent, timeout, and dependency failures.
        """

        normalized: list[dict[str, Any]] = []
        failure_statuses = {"FAILED", "ERROR", "TIMEOUT", "NEEDS_INPUT", "REJECTED"}
        for raw in observations:
            item = dict(raw)
            item.setdefault("workflow_id", call.workflow_id)
            item.setdefault("workflow_step_id", call.workflow_step_id)
            status = str(item.get("status") or "").upper()
            if status in failure_statuses and not isinstance(item.get("operator_error"), dict):
                code = str(item.get("error_code") or "WORKFLOW_STEP_FAILED")
                operator_message = str(
                    item.get("error_message")
                    or item.get("answer_markdown")
                    or f"workflow call {call.call_id} failed without detail"
                )
                layered = build_layered_error(
                    code=code,
                    operator_message=operator_message,
                    component=call.objective or call.target_id,
                    workflow_id=call.workflow_id,
                    step_id=call.workflow_step_id,
                    request_id=str(state.get("task_id") or ""),
                )
                item["error_code"] = layered.code
                item["error_message"] = layered.public_message
                item["answer_markdown"] = layered.public_message
                item["operator_error"] = layered.model_dump(mode="json")
            normalized.append(item)
        return normalized

    @trace_node("content.prepare")
    async def prepare_content(self, state: dict[str, Any]) -> dict[str, Any]:
        if state.get("selection_resume") and state.get("understanding_results"):
            return {"understanding_results":state["understanding_results"]}
        if state.get("diagnosis_resume_state") and (state.get("diagnosis_choice") or {}).get("source") == "user_confirmation":
            return {"understanding_results":state["diagnosis_resume_state"].get("understanding_results") or []}
        attachments = list(state.get("attachments") or [])
        await self._emit(
            state,
            "attachment.preparation.started",
            {"attachment_count": len(attachments)},
            stage="prepare_content",
            status="STARTED",
        )
        for item in attachments:
            await self._emit(
                state,
                "attachment.parser.started",
                {
                    "attachment_id": str(item.get("attachment_id") or ""),
                    "filename": item.get("filename"),
                    "kind": item.get("kind"),
                },
                stage="prepare_content",
                actor_type="file_parser",
                actor_id=str(item.get("kind") or "unknown"),
                status="STARTED",
            )
        envelope = self.content_understanding_service.build_envelope(
            query=str(state.get("query") or ""),
            attachments=attachments,
        )
        runtime = current_graph_runtime().agent_runtime
        try:
            results = await self.content_understanding_service.understand(
                envelope=envelope,
                user_token=runtime.user_token,
                execution_mode=str(state.get("execution_mode") or runtime.execution_mode),
            )
        except Exception as exc:
            await self._emit(
                state,
                "attachment.preparation.failed",
                {"attachment_count": len(attachments), "message": str(exc)},
                stage="prepare_content",
                actor_type="file_parser",
                status="FAILED",
            )
            raise
        serialized = [item.model_dump(mode="json") for item in results]
        if str(state.get("execution_mode") or "normal") != "quick" and serialized:
            serialized = await add_visual_evidence(
                serialized, supervisor=self.supervisor,
                attachment_service=self.content_understanding_service.attachment_service,
                runtime=runtime,
            )
        from app.content.contracts import ContentUnderstandingResult
        results = [ContentUnderstandingResult.model_validate(x) for x in serialized]
        for result in results:
            await self._emit(
                state,
                "attachment.parser.completed",
                {
                    "attachment_id": str(result.attachment_id or ""),
                    "filename": result.filename,
                    "parser_name": result.parser_name,
                    "parser_version": result.parser_version,
                    "extraction_status": result.extraction_status.value,
                    "character_count": len(result.extracted_text or ""),
                    "warnings": result.warnings,
                },
                stage="prepare_content",
                actor_type="file_parser",
                actor_id=result.parser_name,
                status="COMPLETED",
            )
            await self._emit(
                state,
                "attachment.search.completed",
                {
                    "attachment_id": str(result.attachment_id or ""),
                    "selected_character_count": len(result.extracted_text or ""),
                    "selection_mode": "keyword_chunk_retrieval",
                },
                stage="prepare_content",
                actor_type="file_retriever",
                actor_id="local_content_retriever",
                status="COMPLETED",
            )
        await self._emit(
            state,
            "attachment.route.decided",
            {
                "route": "HYBRID" if results else "LOCAL_CONTEXT",
                "attachment_count": len(results),
                "native_file_policy": runtime.settings.file_external_model_policy,
            },
            stage="prepare_content",
            actor_type="file_router",
            actor_id="file_route_policy",
            status="COMPLETED",
        )
        await self._emit(
            state,
            "attachment.preparation.completed",
            {
                "attachment_count": len(results),
                "statuses": [item.extraction_status.value for item in results],
                "route": "HYBRID" if results else "LOCAL_CONTEXT",
            },
            stage="prepare_content",
            status="COMPLETED",
            output_payload={
                "results": [
                    {
                        "attachment_id": str(item.attachment_id or ""),
                        "parser_name": item.parser_name,
                        "status": item.extraction_status.value,
                        "character_count": len(item.extracted_text or ""),
                    }
                    for item in results
                ]
            },
        )
        from app.content.attachment_analysis import start as start_attachment_analysis
        start_attachment_analysis({**state,"understanding_results":serialized},self.supervisor)
        return {
            "content_envelope": envelope.model_dump(mode="json"),
            "understanding_results": serialized,
        }

    @staticmethod
    def _state_object(state: dict[str, Any], key: str) -> dict[str, Any]:
        direct = state.get(key)
        if isinstance(direct, dict) and direct:
            return dict(direct)
        entity_result = state.get("entity_result")
        if isinstance(entity_result, dict):
            nested = entity_result.get(key)
            if isinstance(nested, dict):
                return dict(nested)
        return {}

    @classmethod
    def _business_intent_from_call(
        cls, state: dict[str, Any], call: AgentCall
    ) -> dict[str, Any]:
        return build_alarm_business_intent(
            cls._state_object(state, "business_intent"),
            call.arguments,
            objective=call.objective,
        )

    @classmethod
    def _alarm_tool_arguments(
        cls, state: dict[str, Any], call: AgentCall
    ) -> dict[str, Any]:
        selected = dict(state.get("selected_entity") or {})
        resolved = selected or dict(state.get("resolved_entity") or {})
        active = selected or dict(state.get("active_entity") or {})
        return build_alarm_workflow_inputs(
            query=str(state.get("query") or ""),
            resolved_entity=resolved,
            active_entity=active,
            query_scope=cls._state_object(state, "query_scope"),
            entity_constraints=cls._state_object(state, "entity_constraints"),
            business_intent=cls._business_intent_from_call(state, call),
        )

    def _asset_point_down_drill_enabled(self) -> bool:
        try:
            descriptor = self.tool_registry.get_descriptor(PHM_ASSET_QUERY_POINTS_TOOL_ID)
        except Exception:
            return False
        return bool(descriptor.enabled)

    @staticmethod
    def _point_down_drill_call(
        *,
        call_id: str | None = None,
        depends_on: list[str] | None = None,
        point_type: str = "vibration_acceleration",
        objective: str | None = None,
    ) -> AgentCall:
        objective = objective or "在当前已锚定设备内部查询专业诊断可用的振动加速度测点"
        return AgentCall(
            call_id=call_id or str(uuid4()),
            call_type="tool",
            tool_id=PHM_ASSET_QUERY_POINTS_TOOL_ID,
            objective=objective,
            arguments={
                "required_entity_level": "equipment",
                "point_type": point_type,
                "limit": 1000,
                "_entity_down_drill": True,
            },
            depends_on=list(depends_on or []),
        )

    @staticmethod
    def _point_entities_from_asset_payload(
        state: dict[str, Any], payload: dict[str, Any]
    ) -> list[dict[str, Any]]:
        anchor = entity_anchor(state)
        equipment = payload.get("equipment")
        equipment = dict(equipment) if isinstance(equipment, dict) else {}
        points = payload.get("points")
        points = points if isinstance(points, list) else []
        candidates: list[dict[str, Any]] = []
        for raw in points:
            if not isinstance(raw, dict):
                continue
            point = dict(raw)
            merged = {**anchor, **equipment, **point}
            merged["entity_type"] = "point"
            metadata: dict[str, Any] = {}
            for source in (
                anchor.get("metadata") if isinstance(anchor.get("metadata"), dict) else {},
                point.get("metadata") if isinstance(point.get("metadata"), dict) else {},
            ):
                metadata.update(source)
            if metadata:
                merged["metadata"] = metadata
            enriched = enrich_phm_point_entity(merged)
            if enriched.get("equip_no") and enriched.get("point_no"):
                candidates.append(enriched)
        return candidates

    @classmethod
    def _point_down_drill_state_updates(
        cls,
        state: dict[str, Any],
        payload: dict[str, Any],
        *,
        point_type: str = "vibration_acceleration",
    ) -> tuple[dict[str, Any], str | None]:
        candidates = cls._point_entities_from_asset_payload(state, payload)
        if not candidates:
            label = {
                "temperature": "温度测点",
                "vibration": "振动测点",
                "vibration_acceleration": "振动加速度测点",
            }.get(point_type, "目标测点")
            return {}, f"当前设备下没有检索到可用于本次数据查询的{label}。"

        if len(candidates) == 1:
            resolved = candidates[0]
            query_scope = enrich_query_scope(
                cls._state_object(state, "query_scope"), resolved
            )
            entity_result = {
                "status": "UNIQUE",
                "need_lookup": True,
                "need_disambiguation": False,
                "matches": [resolved],
                "match_count": 1,
                "resolved_entity": resolved,
                "resolution_source": "phm_asset.query_points",
                "query_scope": query_scope,
                "entity_constraints": cls._state_object(state, "entity_constraints"),
            }
            return {
                "resolved_entity": resolved,
                "entity_result": entity_result,
                "query_scope": query_scope,
            }, None

        # A point-scoped capability must never guess among multiple real points. Keep
        # all candidates inside the anchored equipment and reuse the existing entity
        # selection mechanism so the user can choose the intended temperature/vibration
        # point before Data MCP is invoked.
        entity_result = {
            "status": "MULTIPLE",
            "need_lookup": True,
            "need_disambiguation": True,
            "matches": candidates,
            "match_count": len(candidates),
            "resolved_entity": None,
            "resolution_source": "phm_asset.query_points",
            "query_scope": cls._state_object(state, "query_scope"),
            "entity_constraints": cls._state_object(state, "entity_constraints"),
        }
        return {"entity_result": entity_result}, None

    @classmethod
    def _reused_entity_observation(
        cls, state: dict[str, Any], call: AgentCall, entity: dict[str, Any]
    ) -> dict[str, Any]:
        query_scope = enrich_query_scope(cls._state_object(state, "query_scope"), entity)
        entity_result = {
            "status": "UNIQUE",
            "need_lookup": False,
            "need_disambiguation": False,
            "matches": [entity],
            "match_count": 1,
            "resolved_entity": entity,
            "resolution_source": "conversation_entity_anchor",
            "query_scope": query_scope,
            "entity_constraints": cls._state_object(state, "entity_constraints"),
        }
        observation = {
            "call_id": call.call_id,
            "call_type": "agent",
            "agent_id": call.agent_id,
            "target_id": call.agent_id,
            "objective": call.objective,
            "status": "COMPLETED",
            "answer_markdown": "已复用当前对话中明确锚定的实体，无需重新模糊检索。",
            "evidence": [{"source_type": "entity_anchor", "result": entity_result}],
            "can_support_final_answer": True,
            "state_updates": {
                "resolved_entity": entity,
                "entity_result": entity_result,
            },
        }
        return {
            "observations": [observation],
            "resolved_entity": entity,
            "entity_result": entity_result,
            "query_scope": query_scope,
            "entity_constraints": cls._state_object(state, "entity_constraints"),
        }

    def _request(self, state: dict[str, Any], call: AgentCall) -> AgentRequest:
        attachments = [
            AttachmentDescriptor.model_validate(item)
            for item in state.get("attachments") or []
        ]

        selected = dict(state.get("selected_entity") or {})
        resolved = dict(state.get("resolved_entity") or {})
        active = dict(state.get("active_entity") or {})

        # Entity lifecycle is evaluated before task planning.  A fuzzy call must obey
        # that decision rather than blindly inheriting the branch entity or discarding
        # it.  REUSE anchors the current entity; REPLACE clears old device/point IDs
        # while retaining only a safe spatial scope hint for the fresh lookup.
        lifecycle = state.get("entity_resolution")
        lifecycle = dict(lifecycle) if isinstance(lifecycle, dict) else {}
        lifecycle_action = str(lifecycle.get("action") or "")
        if call.call_type == "agent" and call.agent_id == FUZZY_ENTITY_AGENT_ID and not selected:
            if lifecycle_action == "reuse":
                anchor = lifecycle.get("anchor_entity")
                if isinstance(anchor, dict) and anchor:
                    resolved = dict(anchor)
                    active = dict(anchor)
            elif lifecycle_action == "replace":
                resolved = {}
                scope_hint = lifecycle.get("scope_hint")
                active = dict(scope_hint) if isinstance(scope_hint, dict) else {}

        return AgentRequest(
            task_id=str(state["task_id"]),
            agent_id=call.agent_id,
            conversation_id=str(state["conversation_id"]),
            branch_id=str(state["branch_id"]),
            app_code=str(state.get("app_code") or "xiaoao"),
            query=str(state.get("query") or ""),
            objective=call.objective,
            required_entity_level=str(call.arguments.get("required_entity_level") or "none"),
            execution_mode=str(state.get("execution_mode") or "normal"),
            locale=str(state.get("locale") or "zh-CN"),
            memory_context=dict(state.get("memory_context") or {}),
            user_profile=dict(state.get("user_profile") or {}),
            resolved_entity=selected or resolved,
            active_entity=selected or active,
            entity_result=self._state_object(state, "entity_result"),
            query_scope=self._state_object(state, "query_scope"),
            entity_constraints=self._state_object(state, "entity_constraints"),
            business_intent=self._business_intent_from_call(state, call),
            active_diagnosis_task=state.get("active_diagnosis_task"),
            registry_snapshot=dict(state.get("registry_snapshot") or {}),
            external_conversation_id=(state.get("external_ids") or {}).get("conversation_id"),
            attachments=attachments,
            content_envelope=dict(state.get("content_envelope") or {}),
            understanding_results=list(state.get("understanding_results") or []),
            prior_observations=public_error_payload(list(state.get("observations") or [])),
            model_policy={
                "stream_as_final": bool((call.arguments or {}).get("stream_as_final")),
            },
            workflow_id=call.workflow_id,
            workflow_step_id=call.workflow_step_id,
        )

    @staticmethod
    def _entity_selection_required(state: dict[str, Any]) -> bool:
        result = dict(state.get("entity_result") or {})
        return (
            str(result.get("status") or "").upper() == "MULTIPLE"
            and len(result.get("matches") or []) > 1
        )

    async def wait_for_entity_selection(self, state: dict[str, Any]) -> dict[str, Any]:
        result = dict(state.get("entity_result") or {})
        await self._emit(
            state,
            "entity.selection.pending",
            {
                "task_id": state.get("task_id"),
                "candidate_count": len(result.get("matches") or []),
            },
            stage="entity_selection",
            actor_type="graph",
            actor_id=f"{state.get('execution_mode')}_graph",
            status="WAITING_SELECTION",
        )
        return {
            "final_answer": "",
            "final_status": "WAITING_SELECTION",
        }

    @classmethod
    def route_after_agent_execution(cls, state: dict[str, Any]) -> str:
        if identity_service_failed(state):
            return "failure"
        if (state.get("entity_result") or {}).get("status") == "NOT_FOUND" and (state.get("entity_dependency") or identity_dependency(state)).get("required"):
            return "failure"
        return "selection" if cls._entity_selection_required(state) else "continue"

    async def _execute_call(self, state: dict[str, Any], call: AgentCall) -> dict[str, Any]:
        if call.call_type == "tool" and requires_detailed(call, state) and depth(state) != "detailed":
            return {"observations":[{"call_id":call.call_id, "tool_id":call.tool_id,
                "status":"REJECTED", "can_support_final_answer":False, "evidence":[],
                "error_code":"ANALYSIS_DEPTH_REQUIRED", "answer_markdown":"本轮使用已有信息进行初步分析，尚未执行耗时专业诊断。"}]}
        runtime = current_graph_runtime().agent_runtime
        selected_entity = dict(state.get("selected_entity") or {})

        # Execution-level entity continuity guard. Even if the supervisor incorrectly
        # classifies a follow-up as a fresh professional diagnosis and requests a
        # point-level fuzzy lookup, do not lose a known equipment anchor. Reuse an
        # already-sufficient entity, or down-drill inside the anchored equipment via
        # Asset MCP. Only REPLACE/NONE paths may reach global fuzzy resolution.
        if call.call_type == "agent" and call.agent_id == FUZZY_ENTITY_AGENT_ID:
            required_level = str(call.arguments.get("required_entity_level") or "none").lower()
            from app.services.sensor_references import code_tokens
            from app.orchestration.entity_lifecycle import has_explicit_different_code
            explicit = code_tokens(str(call.arguments.get("query") or "") + " " + call.objective)
            original = code_tokens(state.get("query"))
            sensor_target = state.get("sensor_target") or {}
            authorized = set(original)
            if sensor_target.get("verified"):
                authorized.update(str(sensor_target[k]) for k in ("equip_num", "point_num", "param_num") if sensor_target.get(k))
            mismatch = any(o.get("error_code") == "SENSOR_IDENTITY_MISMATCH" for o in state.get("observations") or [])
            # A model correction may only use codes grounded in the user request;
            # changing prose cannot introduce a new target or bypass the retry cap.
            correction = bool(explicit and set(explicit).issubset(authorized) and
                (mismatch or has_explicit_different_code(" ".join(explicit), current_turn_entity(state))))
            if correction:
                if int(state.get("sensor_identity_corrections") or 0) >= 1:
                    return {"observations": [{"call_id": call.call_id, "status": "REJECTED",
                        "can_support_final_answer": False, "error_code": "IDENTITY_RECHECK_EXHAUSTED",
                        "answer_markdown": "已重新核对原编码，仍未能确认的部分将单独说明。"}]}
                state = {**state, "selected_entity": {}, "active_entity": {}, "resolved_entity": {},
                         "entity_result": {}, "entity_resolution": {"action": "replace"}}
                from app.orchestration.sensor_identity import prepare_identity
                update = await prepare_identity(self, state, state.get("business_intent") or {}, refresh=True)
                if update is not None:
                    update["sensor_identity_corrections"] = 1
                    if update.get("sensor_target"):
                        update["sensor_target"]["revision"] += ":1"
                        update["business_intent"].setdefault("asset_semantics", {})["refresh_requested"] = True
                    return update

            # resolve_entity_context already executes the single global Asset-MCP
            # resolution for this turn.  If that result satisfies the planner's level,
            # reuse it regardless of whether the lifecycle action is REUSE or REPLACE.
            # current_turn_entity() is turn-aware and rejects stale prior-turn entities.
            if not correction and has_current_turn_entity_for_level(state, required_level):
                anchor = current_turn_entity(state)
                if anchor:
                    return self._reused_entity_observation(state, call, anchor)

            # If the current turn resolved an equipment but the planner later asks for
            # a point, down-drill under that equipment rather than launching another
            # global fuzzy resolve.
            if (
                required_level == "point"
                and can_down_drill_equipment_to_point(state)
                and self._asset_point_down_drill_enabled()
            ):
                down_drill = self._point_down_drill_call(
                    call_id=call.call_id, depends_on=call.depends_on
                )
                return await self._execute_call(state, down_drill)
            if self.entity_resolution_layer is not None:
                intent = dict(state.get("business_intent") or {})
                semantics = {**dict(intent.get("asset_semantics") or {}), "needs_asset_lookup": True}
                lookup_state = {**state, "business_intent": {**intent, "asset_semantics": semantics}}
                outcome = await self.entity_resolution_layer.resolve(
                    lookup_state, required_entity_level=required_level if required_level != "none" else "any",
                )
                dependency = {"required": True, "level": required_level, "source": "tool_prerequisite"}
                observation = {**outcome.observation, "call_id": call.call_id, "objective": call.objective}
                await self._emit(state, "entity.resolution.completed",
                    {"status": (outcome.updates.get("entity_result") or {}).get("status"), "source": "tool_prerequisite"},
                    stage="entity_resolution", actor_type="system", actor_id="unified_entity_resolution_layer",
                    status="FAILED" if (outcome.updates.get("entity_result") or {}).get("status") == "ERROR" else "COMPLETED",
                    output_payload={"entity_dependency": dependency, "entity_result": outcome.updates.get("entity_result") or {}},
                )
                return {**outcome.updates, "entity_dependency": dependency, "observations": [observation],
                        "sensor_identity_corrections": int(state.get("sensor_identity_corrections") or 0) + int(correction)}
        if (
            call.call_type == "agent"
            and call.agent_id == FUZZY_ENTITY_AGENT_ID
            and selected_entity
            and entity_satisfies_required_level(
                selected_entity,
                str(call.arguments.get("required_entity_level") or "none"),
            )
        ):
            previous_entity_result = dict(state.get("entity_result") or {})
            query_scope = enrich_query_scope(
                self._state_object(state, "query_scope"), selected_entity
            )
            entity_constraints = self._state_object(state, "entity_constraints")
            entity_result = {
                **{
                    key: previous_entity_result[key]
                    for key in (
                        "workflow_run_id",
                        "lookup_scope",
                        "resolution_source",
                        "profile_prior_used",
                    )
                    if key in previous_entity_result
                },
                "status": "UNIQUE",
                "need_lookup": True,
                "need_disambiguation": False,
                "matches": [selected_entity],
                "match_count": 1,
                "resolved_entity": selected_entity,
                "selected_from_multiple": True,
                "query_scope": query_scope,
                "entity_constraints": entity_constraints,
            }
            observation = {
                "call_id": call.call_id,
                "call_type": "agent",
                "agent_id": call.agent_id,
                "target_id": call.agent_id,
                "objective": call.objective,
                "status": "COMPLETED",
                "answer_markdown": "已使用用户在候选弹窗中确认的实体。",
                "evidence": [
                    {
                        "source_type": "entity_selection",
                        "result": entity_result,
                    }
                ],
                "can_support_final_answer": True,
                "state_updates": {
                    "resolved_entity": selected_entity,
                    "entity_result": entity_result,
                },
            }
            return {
                "observations": [observation],
                "resolved_entity": selected_entity,
                "entity_result": entity_result,
                "query_scope": query_scope,
                "entity_constraints": entity_constraints,
            }
        if call.call_type == "tool":
            if call.tool_id in PHM_WORKFLOW_TOOL_IDS:
                required_level = workflow_tool_required_entity_level(
                    call.tool_id,
                    str(call.arguments.get("required_entity_level") or "none"),
                )
                if required_level != "none" and not self._has_entity_for_level(state, required_level):
                    return {
                        "observations": [
                            {
                                "call_id": call.call_id,
                                "call_type": "tool",
                                "tool_id": call.tool_id,
                                "target_id": call.tool_id,
                                "objective": call.objective,
                                "status": "NEEDS_INPUT",
                                "answer_markdown": "业务链路批处理缺少已确认的实体范围。",
                                "evidence": [],
                                "can_support_final_answer": False,
                                "error_code": "WORKFLOW_ENTITY_REQUIRED",
                                "error_message": f"缺少 {required_level} 级实体",
                            }
                        ]
                    }
                arguments = build_workflow_tool_arguments(
                    tool_id=call.tool_id,
                    state=state,
                    call_arguments=call.arguments,
                )
            elif call.tool_id in PHM_SENSOR_TOOL_IDS:
                arguments, missing = build_sensor_arguments(call.tool_id, state, call.arguments)
                if missing:
                    return {"observations": [{"call_id": call.call_id, "call_type": "tool", "tool_id": call.tool_id,
                        "target_id": call.tool_id, "objective": call.objective, "status": "NEEDS_INPUT",
                        "answer_markdown": "；".join(missing), "error_code": "SENSOR_QUERY_PREREQUISITE_MISSING",
                        "error_message": "；".join(missing), "evidence": [], "can_support_final_answer": False}]}
            elif call.tool_id in PHM_ASSET_TOOL_IDS:
                required_level = asset_tool_required_entity_level(call.tool_id)
                call.arguments["required_entity_level"] = required_level
                if not asset_identity_available(state, call.tool_id):
                    identity_name = "equip_no" if required_level == "equipment" else "space_id"
                    observation = {
                        "call_id": call.call_id,
                        "call_type": "tool",
                        "tool_id": call.tool_id,
                        "target_id": call.tool_id,
                        "objective": call.objective,
                        "status": "NEEDS_INPUT",
                        "answer_markdown": (
                            f"PHM Asset MCP 需要真实 {identity_name}，但当前实体解析结果没有可用编码。"
                        ),
                        "evidence": [],
                        "can_support_final_answer": False,
                        "error_code": "PHM_ASSET_ENTITY_REQUIRED",
                        "error_message": f"缺少真实 {identity_name}",
                    }
                    return {"observations": [observation]}
                arguments, missing = build_phm_asset_arguments(
                    tool_id=call.tool_id,
                    state=state,
                    call_arguments=call.arguments,
                )
                if missing:
                    detail = "、".join(missing)
                    return {
                        "observations": [
                            {
                                "call_id": call.call_id,
                                "call_type": "tool",
                                "tool_id": call.tool_id,
                                "target_id": call.tool_id,
                                "objective": call.objective,
                                "status": "NEEDS_INPUT",
                                "answer_markdown": "资产查询缺少确定性编码：" + detail,
                                "evidence": [],
                                "can_support_final_answer": False,
                                "error_code": "PHM_ASSET_ARGUMENTS_MISSING",
                                "error_message": "缺少参数：" + detail,
                            }
                        ]
                    }
            elif call.tool_id in PHM_FEATURE_TOOL_IDS:
                arguments, missing = build_phm_feature_arguments(
                    tool_id=call.tool_id,
                    state=state,
                    call_arguments=call.arguments,
                    transient_payloads=current_graph_runtime().transient_tool_payloads,
                )
                if missing:
                    detail = "、".join(missing)
                    observation = {
                        "call_id": call.call_id,
                        "call_type": "tool",
                        "tool_id": call.tool_id,
                        "target_id": call.tool_id,
                        "objective": call.objective,
                        "status": "NEEDS_INPUT",
                        "answer_markdown": "PHM 特征/转速提取所需波形尚未准备完成：" + detail,
                        "evidence": [],
                        "can_support_final_answer": False,
                        "error_code": "PHM_FEATURE_INPUT_MISSING",
                        "error_message": "缺少或非法参数：" + detail,
                    }
                    return {"observations": [observation]}
            elif call.tool_id in PHM_DIAGNOSIS_TOOL_IDS:
                arguments, missing = build_phm_diagnosis_arguments(
                    tool_id=call.tool_id,
                    state=state,
                    call_arguments=call.arguments,
                    transient_payloads=current_graph_runtime().transient_tool_payloads,
                )
                if missing:
                    detail = "、".join(missing)
                    observation = {
                        "call_id": call.call_id,
                        "call_type": "tool",
                        "tool_id": call.tool_id,
                        "target_id": call.tool_id,
                        "objective": call.objective,
                        "status": "NEEDS_INPUT",
                        "answer_markdown": (
                            "专业诊断所需数据尚未准备完成：" + detail
                        ),
                        "evidence": [],
                        "can_support_final_answer": False,
                        "error_code": "PHM_DIAGNOSIS_INPUT_MISSING",
                        "error_message": "缺少参数：" + detail,
                    }
                    return {"observations": [observation]}
                if call.tool_id in {PHM_DIAGNOSIS_DEVICE_TOOL_ID, PHM_DIAGNOSIS_POINT_TOOL_ID}:
                    identity = resolve_phm_identity_fields(state)
                    points = arguments.get("points") or []
                    await self._emit(
                        state,
                        "diagnosis.input.prepared",
                        {
                            "diagnosis_scope": "device" if call.tool_id == PHM_DIAGNOSIS_DEVICE_TOOL_ID else "point",
                            "point_count": len(points) if isinstance(points, list) else 1,
                            "has_waveform": bool(arguments.get("waveform")),
                            "feature_trend_count": len(arguments.get("feature_trends") or []),
                            "temperature_trend_count": len(arguments.get("temperature_trends") or []),
                            "has_speed_rpm": bool(arguments.get("speed_rpm")),
                            "device_code": arguments.get("device_code") or identity.get("device_code"),
                            "point_no": arguments.get("point_no") or identity.get("wave_point_no"),
                        },
                        stage="diagnosis_prepare",
                        actor_type="system",
                        actor_id="phm_diagnosis_pipeline",
                        status="COMPLETED",
                    )
            elif call.tool_id in PHM_DATA_TOOL_IDS:
                if call.tool_id == PHM_QUERY_ALARM_RECORDS_TOOL_ID:
                    required_level = self._required_entity_level_for_call(call, state)
                    call.arguments["required_entity_level"] = required_level
                elif call.tool_id == PHM_QUERY_HEALTH_SCORE_TOOL_ID:
                    query = str(state.get("query") or "")
                    entity = (
                        state.get("selected_entity")
                        or state.get("resolved_entity")
                        or state.get("active_entity")
                        or {}
                    )
                    scope_type = infer_health_scope_type(
                        query,
                        call.arguments,
                        entity if isinstance(entity, dict) else {},
                        state.get("business_intent") if isinstance(state.get("business_intent"), dict) else {},
                    )
                    call.arguments["scope_type"] = scope_type
                    required_level = health_required_entity_level(
                        query,
                        scope_type,
                        str(call.arguments.get("required_entity_level") or "none"),
                    )
                    call.arguments["required_entity_level"] = required_level
                else:
                    # get_device_data is equipment-scoped; legacy waveform/trend/snapshot
                    # tools remain point-scoped. Never let planner "none" bypass identity.
                    default_level = "equipment" if call.tool_id == PHM_GET_DEVICE_DATA_TOOL_ID else "point"
                    required_level = str(call.arguments.get("required_entity_level") or default_level).lower()
                    if required_level == "none":
                        required_level = default_level
                    if call.tool_id == PHM_GET_DEVICE_DATA_TOOL_ID:
                        required_level = "equipment"
                    call.arguments["required_entity_level"] = required_level

                has_entity = self._has_entity_for_level(state, required_level)
                if required_level != "none" and not has_entity:
                    observation = {
                        "call_id": call.call_id,
                        "call_type": "tool",
                        "tool_id": call.tool_id,
                        "target_id": call.tool_id,
                        "objective": call.objective,
                        "status": "NEEDS_INPUT",
                        "answer_markdown": (
                            "PHM 数据查询依赖明确的区域、设备或测点，但实体检索未取得可用结果。"
                        ),
                        "evidence": [],
                        "can_support_final_answer": False,
                        "error_code": "PHM_DATA_ENTITY_REQUIRED",
                        "error_message": "请补充更完整的区域、设备或测点自然语言名称；系统会负责对应真实资产编码。",
                    }
                    return {"observations": [observation]}
                try:
                    arguments, missing = build_phm_data_arguments(
                        tool_id=call.tool_id,
                        state=state,
                        call_arguments=call.arguments,
                    )
                except ValueError as exc:
                    return {
                        "observations": [
                            {
                                "call_id": call.call_id,
                                "call_type": "tool",
                                "tool_id": call.tool_id,
                                "target_id": call.tool_id,
                                "objective": call.objective,
                                "status": "FAILED",
                                "answer_markdown": "模型生成的结构化时间参数未通过后端合法性校验，未执行数据查询。",
                                "evidence": [],
                                "can_support_final_answer": False,
                                "error_code": "PHM_DATA_TIME_ARGUMENT_INVALID",
                                "error_message": str(exc),
                            }
                        ]
                    }
                if (
                    call.tool_id == PHM_QUERY_HEALTH_SCORE_TOOL_ID
                    and space_health_history_unsupported(
                        str(arguments.get("scope_type") or ""),
                        str(state.get("query") or ""),
                        arguments,
                    )
                ):
                    # PHM Data MCP explicitly provides region health as realtime-only.
                    # Return a usable business answer without issuing an illegal MCP call.
                    return {
                        "observations": [
                            {
                                "call_id": call.call_id,
                                "call_type": "tool",
                                "tool_id": call.tool_id,
                                "target_id": call.tool_id,
                                "objective": call.objective,
                                "status": "SUCCESS",
                                "answer_markdown": (
                                    "当前区域健康度仅提供实时结果，不支持历史健康度查询。"
                                ),
                                "evidence": [],
                                "can_support_final_answer": True,
                                "error_code": None,
                                "error_message": None,
                            }
                        ]
                    }
                if missing:
                    observation = {
                        "call_id": call.call_id,
                        "call_type": "tool",
                        "tool_id": call.tool_id,
                        "target_id": call.tool_id,
                        "objective": call.objective,
                        "status": "NEEDS_INPUT",
                        "answer_markdown": (
                            "当前实体缺少 PHM Data MCP 所需的确定性编码："
                            + "、".join(missing)
                        ),
                        "evidence": [],
                        "can_support_final_answer": False,
                        "error_code": "PHM_DATA_ARGUMENTS_MISSING",
                        "error_message": "缺少参数：" + "、".join(missing),
                    }
                    return {"observations": [observation]}
            elif call.tool_id == COMPREHENSIVE_ALARM_TOOL_ID:
                # Explicit rollback path only; the PHM MCP alarm tool is the default.
                required_level = infer_required_entity_level(
                    str(state.get("query") or ""),
                    str(call.arguments.get("required_entity_level") or "none"),
                    self._state_object(state, "query_scope"),
                    structured_level=structured_anchor(state),
                )
                has_entity = self._has_entity_for_level(state, required_level)
                if required_level != "none" and not has_entity:
                    return {
                        "observations": [
                            {
                                "call_id": call.call_id,
                                "call_type": "tool",
                                "tool_id": call.tool_id,
                                "target_id": call.tool_id,
                                "objective": call.objective,
                                "status": "NEEDS_INPUT",
                                "answer_markdown": "报警查询依赖明确的区域、设备或测点。",
                                "evidence": [],
                                "can_support_final_answer": False,
                                "error_code": "ALARM_QUERY_ENTITY_REQUIRED",
                                "error_message": "请补充更完整的区域、设备或测点自然语言名称；系统会负责对应真实资产编码。",
                            }
                        ]
                    }
                arguments = self._alarm_tool_arguments(state, call)
            else:
                arguments = {
                    key: value
                    for key, value in call.arguments.items()
                    if key not in {"objective", "required_entity_level"}
                }
            if call.tool_id == MULTI_QUERY_TOOL_ID:
                arguments["_verified_request_context"] = {
                    "query": str(state.get("query") or ""),
                    "active_entity": state.get("active_entity") or {},
                    "conversation_context": UnifiedEntityResolutionLayer._conversation_context(state),
                    "attachment_texts": [str(item.get("extracted_text") or "")
                                         for item in state.get("understanding_results") or []],
                }
            request = ToolCallRequest(
                tool_id=call.tool_id,
                arguments=arguments,
                task_id=str(state["task_id"]),
                conversation_id=str(state["conversation_id"]),
                branch_id=str(state["branch_id"]),
                user_token=runtime.user_token,
                attachments=list(state.get("attachments") or []),
                workflow_id=call.workflow_id,
                workflow_step_id=call.workflow_step_id,
            )
            if call.tool_id == KNOWLEDGE_TOOL_ID and state.get("_knowledge_timeout_seconds"):
                # Server-owned execution metadata; not a model or frontend argument.
                request = request.model_copy(update={"knowledge_timeout_seconds": state["_knowledge_timeout_seconds"]})
            from app.asset_query_contract import TOOL_ID as COLLECTION_TOOL_ID
            if call.tool_id == COLLECTION_TOOL_ID:
                from app.services.asset_collections import recent_collections
                request = request.model_copy(update={"asset_query_call_id": call.call_id,
                    "asset_allowed_query_ids": [v["query_id"] for v in recent_collections(state)]})
            result = await self.tool_executor.execute(
                request=request,
                runtime=runtime,
                parent_span_id=state.get("root_span_id"),
            )
            # PHM Data results are kept run-local so Diagnosis/RPM MCP calls can reuse
            # the exact structured object (including Base64) without putting it into
            # LangGraph checkpoints, PostgreSQL events, or the final synthesis prompt.
            raw_structured = result.structured_content or {}
            if call.tool_id in PHM_SENSOR_TOOL_IDS and result.status == ToolResultStatus.SUCCESS:
                from app.orchestration.sensor_identity import validate_result
                if not validate_result(state, arguments, raw_structured):
                    return {"observations": [{"call_id": call.call_id, "tool_id": call.tool_id,
                        "status": "FAILED", "can_support_final_answer": False, "evidence": [],
                        "sensor_target_revision": (state.get("sensor_target") or {}).get("revision"),
                        "error_code": "SENSOR_IDENTITY_MISMATCH", "arguments": arguments,
                        "answer_markdown": "返回记录与本次查询的设备或测点不一致，该结果未用于判断故障状态。"}]}
            if call.tool_id == COLLECTION_TOOL_ID and result.status == ToolResultStatus.SUCCESS:
                from app.services.asset_collections import remember_collection
                await remember_collection(runtime, request, raw_structured)
            if (
                call.tool_id == PHM_ASSET_QUERY_SCOPE_COLLECTION_TOOL_ID
                and result.status == ToolResultStatus.SUCCESS
                and isinstance(raw_structured, dict)
            ):
                business_intent = (
                    state.get("business_intent")
                    if isinstance(state.get("business_intent"), dict)
                    else {}
                )
                semantic_policy = (
                    business_intent.get("semantic_policy")
                    if isinstance(business_intent.get("semantic_policy"), dict)
                    else {}
                )
                expected_recursive = semantic_policy.get("effective_recursive")
                actual_recursive = raw_structured.get("recursive")
                if (
                    isinstance(expected_recursive, bool)
                    and isinstance(actual_recursive, bool)
                    and expected_recursive != actual_recursive
                ):
                    return {
                        "observations": [
                            {
                                "call_id": call.call_id,
                                "call_type": "tool",
                                "tool_id": call.tool_id,
                                "target_id": call.tool_id,
                                "objective": call.objective,
                                "status": "ERROR",
                                "answer_markdown": (
                                    "资产集合查询范围校验失败，系统已阻止使用可能错误的统计结果。"
                                ),
                                "evidence": [],
                                "can_support_final_answer": False,
                                "error_code": "ASSET_SCOPE_POLICY_MISMATCH",
                                "error_message": (
                                    f"expected recursive={expected_recursive}, "
                                    f"asset returned recursive={actual_recursive}"
                                ),
                            }
                        ]
                    }
            transient_tool_ids = (
                PHM_DATA_TOOL_IDS
                | PHM_SENSOR_TOOL_IDS
                | PHM_ASSET_TOOL_IDS
                | PHM_DIAGNOSIS_TOOL_IDS
                | PHM_FEATURE_TOOL_IDS
                | PHM_WORKFLOW_TOOL_IDS
            )
            if call.tool_id in transient_tool_ids and result.status == ToolResultStatus.SUCCESS:
                graph_runtime = current_graph_runtime()
                graph_runtime.transient_tool_payloads[call.call_id] = raw_structured
                graph_runtime.transient_tool_payloads[f"latest:{call.tool_id}"] = raw_structured
                history_key = f"history:{call.tool_id}"
                history = graph_runtime.transient_tool_payloads.setdefault(history_key, [])
                if isinstance(history, list):
                    history.append(raw_structured)

            if call.tool_id == COLLECTION_TOOL_ID:
                public_structured = {**raw_structured, "devices": list(raw_structured.get("devices") or [])[:200]}
                if len(raw_structured.get("devices") or []) > 200:
                    public_structured["page_complete"] = False
            elif call.tool_id in PHM_DIAGNOSIS_TOOL_IDS:
                public_structured = compact_diagnosis_result(raw_structured)
            elif call.tool_id == PHM_ASSET_QUERY_SCOPE_COLLECTION_TOOL_ID:
                # The next workflow step consumes the complete collection from the
                # transient store.  Avoid duplicating thousands of entity rows into the
                # final-model evidence; health_collection_batch will return every entity
                # again together with its authoritative health result.
                source = raw_structured if isinstance(raw_structured, dict) else {}
                collection = [
                    item for item in source.get("collection") or [] if isinstance(item, dict)
                ]
                workflow_state = (
                    dict(state.get("business_workflow") or {})
                    if isinstance(state.get("business_workflow"), dict)
                    else {}
                )
                forwarded_to_batch = (
                    str(workflow_state.get("workflow_id") or "") == "health_analysis"
                    and str(workflow_state.get("variant_id") or "") == "scope_collection"
                )
                # For a pure asset collection/count query the final model must receive
                # enough authoritative members to answer a small "有哪些" request.
                # For very large collections we keep the prompt bounded and expose
                # count + a sample; the full collection remains transient/runtime-only.
                public_items = (
                    []
                    if forwarded_to_batch
                    else (collection if len(collection) <= 200 else collection[:50])
                )
                public_structured = {
                    "success": source.get("success"),
                    "status": source.get("status"),
                    "root": source.get("root") or {},
                    "target_entity_level": source.get("target_entity_level"),
                    "target_space_type": source.get("target_space_type"),
                    "target_equipment_type": source.get("target_equipment_type"),
                    "semantic_filters": source.get("semantic_filters") or [],
                    "output_mode": source.get("output_mode") or "list",
                    "semantic_resolution": source.get("semantic_resolution") or {},
                    "recursive": source.get("recursive"),
                    "count": source.get("count", len(collection)),
                    "returned_count": source.get("returned_count", len(collection)),
                    "uncertain_count": source.get("uncertain_count", 0),
                    "candidate_count": source.get("candidate_count"),
                    "query_strategy": source.get("query_strategy") or {},
                    "category_summary": source.get("category_summary") or {},
                    "truncated": source.get("truncated"),
                    "sample": collection[:10],
                    "items": public_items,
                    "items_complete": (not forwarded_to_batch and len(collection) <= 200),
                    "collection_forwarded_to_batch": forwarded_to_batch,
                }
            elif call.tool_id in PHM_DATA_TOOL_IDS:
                public_structured = decode_phm_data_for_public(call.tool_id, raw_structured)
            elif call.tool_id in PHM_WORKFLOW_TOOL_IDS:
                public_structured = public_workflow_payload(call.tool_id, raw_structured)
            else:
                public_structured = raw_structured
            public_structured = sanitize_large_payloads(public_structured)
            content = sanitize_large_payloads(result.content)
            if call.tool_id in PHM_SENSOR_TOOL_IDS and result.status == ToolResultStatus.SUCCESS:
                answer_markdown = format_sensor_result(public_structured if isinstance(public_structured, dict) else {})
            elif call.tool_id in PHM_ASSET_TOOL_IDS and result.status == ToolResultStatus.SUCCESS:
                answer_markdown = format_asset_result(
                    call.tool_id,
                    public_structured if isinstance(public_structured, dict) else {},
                )
            elif call.tool_id == PHM_QUERY_HEALTH_SCORE_TOOL_ID and result.status == ToolResultStatus.SUCCESS:
                answer_markdown = format_health_observation(
                    public_structured if isinstance(public_structured, dict) else {},
                    query=str(state.get("query") or ""),
                    call_arguments=call.arguments,
                )
            elif call.tool_id in PHM_WORKFLOW_TOOL_IDS and result.status == ToolResultStatus.SUCCESS:
                answer_markdown = format_workflow_result(
                    call.tool_id,
                    public_structured if isinstance(public_structured, dict) else {},
                )
            else:
                answer_markdown = (
                    content
                    if isinstance(content, str)
                    else json.dumps(
                        public_structured or content,
                        ensure_ascii=False,
                        default=str,
                    )
                )
            evidence_payload = public_structured or content
            public_result = result.model_dump(mode="json")
            if call.tool_id in PHM_DIAGNOSIS_TOOL_IDS:
                public_result = compact_diagnosis_result(public_result)
            elif call.tool_id in (PHM_DATA_TOOL_IDS | PHM_WORKFLOW_TOOL_IDS | PHM_ASSET_TOOL_IDS):
                # Never leave the raw Base64 inside model-visible tool_result. Otherwise
                # sanitize_large_payloads would replace it with
                # binary_payload_not_persisted and the final LLM could incorrectly claim
                # that concrete values are unavailable even though we decoded them above.
                public_result["structured_content"] = public_structured
            public_result = sanitize_large_payloads(public_result)
            observation = {
                "call_id": call.call_id,
                "call_type": "tool",
                "tool_id": call.tool_id,
                "target_id": call.tool_id,
                "objective": call.objective,
                "workflow_id": call.workflow_id,
                "workflow_step_id": call.workflow_step_id,
                "status": result.status.value,
                "answer_markdown": answer_markdown,
                "evidence": [
                    {
                        "source_type": "tool",
                        "source_id": call.tool_id,
                        "content": evidence_payload,
                    }
                ],
                "can_support_final_answer": result.status == ToolResultStatus.SUCCESS,
                "error_code": result.error_code,
                "error_message": result.error_message,
                "operator_error": result.error.model_dump(mode="json") if result.error else None,
                "tool_result": public_result,
            }
            if call.tool_id in (PHM_SENSOR_TOOL_IDS | PHM_ASSET_TOOL_IDS | PHM_DATA_TOOL_IDS) and state.get("sensor_target"):
                observation["sensor_target_revision"] = (state.get("sensor_target") or {}).get("revision")
            # Evidence provenance requires the actual tool locator/parameters. Keep a
            # sanitized copy for every tool, never raw Base64/numerical blobs.
            observation["arguments"] = sanitize_large_payloads(arguments)
            update: dict[str, Any] = {"observations": [observation]}
            from app.query_contract import QUERY_TOOL_ID, RESULT_TOOL_ID
            if call.tool_id in {QUERY_TOOL_ID, RESULT_TOOL_ID} and result.status == ToolResultStatus.SUCCESS:
                answer_result = raw_structured.get("answer_result")
                if isinstance(answer_result, dict):
                    from app.services.query_results import render_result
                    update.update(answer_results=[answer_result], query_result=answer_result,
                                  query_fact_text=render_result(answer_result))
            if call.tool_id == COLLECTION_TOOL_ID and result.status == ToolResultStatus.SUCCESS:
                from app.services.query_results import make_result,identity
                rows=[identity(x) for x in list(raw_structured.get("devices") or [])[:200]] if raw_structured.get("operation")=="list" else list(raw_structured.get("groups") or [])
                criteria=raw_structured.get("criteria") or {}
                root={"entity_type":"space","space_id":(criteria.get("scope") or {}).get("root_space_id"),"name":raw_structured.get("scope_name")}
                plan={"active":True,"domain":"asset","target":"equipment","operation":raw_structured.get("operation") or "list",
                      "anchor":"space","predicate":criteria.get("predicate"),"group_by":raw_structured.get("group_by"),"recursive":(criteria.get("scope") or {}).get("recursive",True)}
                # Only supported dimensions can be refreshed by the unified adapter; retained rows are always renderable.
                if plan["group_by"] not in {None,"area","equipment_class"}:plan["group_by"]=None
                update["answer_results"]=[make_result(plan=plan,rows=rows,root=root,complete=raw_structured.get("result_complete",False),
                    total=raw_structured.get("count"),notes=list(raw_structured.get("warnings") or []))]
                update["answer_results"][0]["asset_snapshot"]=raw_structured.get("query_id")
            if call.tool_id in PHM_SENSOR_TOOL_IDS and result.status == ToolResultStatus.SUCCESS:
                from app.orchestration.sensor_identity import fault_from_current_result
                fault_target = fault_from_current_result(state, call.tool_id, raw_structured)
                if fault_target:
                    update["sensor_target"] = fault_target
                if call.tool_id in set(SENSOR_TOOL_BY_OPERATION.values()) and isinstance(raw_structured, dict):
                    from app.services.query_results import render_result, sensor_result
                    answer_result = sensor_result(raw_structured, state.get("business_intent") or {})
                    update["answer_results"] = [answer_result]
                    update["query_result"] = answer_result
                    contract = (state.get("business_intent") or {}).get("completion_contract") or {}
                    if contract.get("response_mode") == "facts_only":
                        update["query_fact_text"] = render_result(answer_result)
                        update["deterministic_output_ready"] = True
            if call.tool_id == COLLECTION_TOOL_ID and result.status == ToolResultStatus.SUCCESS:
                update["asset_query_result"] = public_structured

            if (
                call.tool_id == PHM_ASSET_QUERY_EQUIPMENT_INFO_TOOL_ID
                and result.status == ToolResultStatus.SUCCESS
                and isinstance(raw_structured, dict)
            ):
                current_equipment = (
                    state.get("selected_entity")
                    or state.get("resolved_entity")
                    or {}
                )
                enriched_equipment = enrich_equipment_entity_from_asset_detail(
                    current_equipment if isinstance(current_equipment, dict) else {},
                    raw_structured,
                )
                if enriched_equipment:
                    update["resolved_entity"] = enriched_equipment
                    observation["state_updates"] = {
                        "resolved_entity": enriched_equipment,
                    }

            if (
                call.tool_id == PHM_ASSET_QUERY_POINTS_TOOL_ID
                and bool(call.arguments.get("_entity_down_drill"))
                and result.status == ToolResultStatus.SUCCESS
            ):
                down_drill_updates, down_drill_error = self._point_down_drill_state_updates(
                    state,
                    raw_structured if isinstance(raw_structured, dict) else {},
                    point_type=str(call.arguments.get("point_type") or "vibration_acceleration"),
                )
                if down_drill_error:
                    observation.update(
                        {
                            "status": "FAILED",
                            "can_support_final_answer": False,
                            "error_code": "PHM_ASSET_POINT_DOWN_DRILL_EMPTY",
                            "error_message": down_drill_error,
                            "answer_markdown": down_drill_error,
                        }
                    )
                else:
                    update.update(down_drill_updates)
                    observation["state_updates"] = {
                        key: value
                        for key, value in down_drill_updates.items()
                        if key in {"resolved_entity", "entity_result", "query_scope"}
                    }

            if (
                call.tool_id == PHM_FEATURE_EXTRACT_RPM_TOOL_ID
                and result.status == ToolResultStatus.SUCCESS
            ):
                rpm = extract_supported_rpm(raw_structured)
                if rpm is not None:
                    update["speed_rpm"] = rpm
            return update

        result = await self.agent_executor.execute(
            request=self._request(state, call),
            runtime=runtime,
            parent_span_id=state.get("root_span_id"),
        )
        observation = {
            "call_id": call.call_id,
            "call_type": "agent",
            "agent_id": call.agent_id,
            "target_id": call.agent_id,
            "objective": call.objective,
            "workflow_id": call.workflow_id,
            "workflow_step_id": call.workflow_step_id,
            **result.model_dump(mode="json"),
        }
        update: dict[str, Any] = {"observations": [observation]}
        if result.state_updates.get("resolved_entity"):
            update["resolved_entity"] = dict(result.state_updates["resolved_entity"])
        if result.state_updates.get("entity_result"):
            update["entity_result"] = dict(result.state_updates["entity_result"])
        if result.state_updates.get("query_scope"):
            update["query_scope"] = dict(result.state_updates["query_scope"])
        if result.state_updates.get("entity_constraints"):
            update["entity_constraints"] = dict(result.state_updates["entity_constraints"])
        if result.state_updates.get("business_intent"):
            update["business_intent"] = dict(result.state_updates["business_intent"])
        if result.external_ids:
            merged = dict(state.get("external_ids") or {})
            merged.update({key: value for key, value in result.external_ids.items() if value})
            update["external_ids"] = merged
        return update

    @trace_node("quick.answer")
    async def quick_answer(self, state: dict[str, Any]) -> dict[str, Any]:
        from app.services.asset_collections import collection_intent
        runtime = current_graph_runtime().agent_runtime
        if state.get("sensor_identity_attempted"):
            from app.orchestration.sensor_identity import sensor_verdict
            collected = []
            for _ in range(3):
                verdict = sensor_verdict(state, self.tool_registry.list_descriptors(), runtime.settings)
                calls = independent_calls(verdict.next_calls or ([verdict.next_call] if verdict.next_call else []),
                                          runtime.settings.normal_max_parallel_calls)
                if not calls:
                    break
                update = await execute_independent(calls, state, self._execute_call)
                collected += update.get("observations", [])
                state = {**state, **update, "observations": list(state.get("observations") or []) + update.get("observations", [])}
            state, knowledge_rows = await self._ensure_business_knowledge(state)
            answer = await self._stream_supervisor_answer(state, expert=False)
            return {"observations": collected + knowledge_rows, "sensor_target": state.get("sensor_target") or {},
                    "final_answer": answer, "final_status": state.get("final_status") or "COMPLETED"}
        if getattr(runtime.settings, "phm_asset_unified_query_enabled", False) and collection_intent(state):
            from app.asset_query_contract import TOOL_ID as COLLECTION_TOOL_ID
            update = await self._execute_call(state, AgentCall(call_id=str(uuid4()), call_type="tool",
                tool_id=COLLECTION_TOOL_ID, objective="查询本轮指定的设备集合", arguments={}))
            state = {**state, **update, "observations": list(state.get("observations") or [])+update.get("observations", [])}
            state, knowledge_rows = await self._ensure_business_knowledge(state)
            answer = await self._stream_supervisor_answer(state, expert=False)
            return {**update, "observations": update.get("observations", [])+knowledge_rows,
                    "final_answer": answer, "final_status": state.get("final_status") or "COMPLETED"}
        state, knowledge_rows = await self._ensure_business_knowledge(state)
        await current_graph_runtime().set_task_streaming()
        call = AgentCall(
            call_id=str(uuid4()),
            agent_id=GENERAL_CONTENT_AGENT_ID,
            objective="在快速模式下直接回答用户问题",
        )
        from app.output.streaming import AnswerStreamStopped
        try:
            update = await self._execute_call(state, call)
        except AnswerStreamStopped:
            meta = current_graph_runtime().transient_tool_payloads.get("_answer_generation") or {}
            return {"observations":knowledge_rows, "final_answer":meta.get("content") or "", "final_status":"STOPPED"}
        observation = update["observations"][0]
        answer = str(observation.get("answer_markdown") or "")
        if observation.get("status") == "FAILED":
            meta = current_graph_runtime().transient_tool_payloads.get("_answer_generation") or {}
            return {**update, "observations":knowledge_rows+update.get("observations",[]),
                "final_answer":meta.get("content") or observation.get("error_message") or "回答生成失败，请稍后重试。",
                "final_status":"FAILED"}
        if not answer:
            answer = str(observation.get("error_message") or "快速回答模型当前不可用。")

        execution_summary = observation.get("execution_summary") or {}
        already_streamed = bool(
            isinstance(execution_summary, dict)
            and execution_summary.get("answer_streamed")
            and observation.get("answer_markdown")
        )
        # Native attachment analysis is still a single model response. Text-only quick
        # answers are already emitted incrementally by GeneralContentAgent and must not
        # be duplicated here.
        if not already_streamed:
            answer = render_answer(answer, state, suggestions=False)
            await current_graph_runtime().agent_runtime.event_service.publish(
                state["task_id"],
                "answer.delta",
                {"content": answer, "channel": "final"},
                graph_mode="quick",
                actor_type="agent",
                actor_id=GENERAL_CONTENT_AGENT_ID,
                status="STREAMING",
                span_id=state.get("root_span_id"),
            )
        return {
            **update,
            "observations": knowledge_rows + update.get("observations", []),
            "quick_result": observation,
            "final_answer": answer,
            "final_status": "COMPLETED",
        }

    @staticmethod
    def _normalize_react_call_for_state(call: AgentCall, state: dict[str, Any]) -> AgentCall:
        """Apply turn-level semantic constraints before duplicate detection/execution."""
        if call.call_type != "tool":
            return call
        arguments = dict(call.arguments or {})
        if call.tool_id == PHM_QUERY_ALARM_RECORDS_TOOL_ID:
            intent = state.get("business_intent") if isinstance(state.get("business_intent"), dict) else {}
            time_range = intent.get("time_range") if isinstance(intent.get("time_range"), dict) else {}
            mode = str(time_range.get("mode") or "none").strip().lower()
            for field in ("start_time", "end_time", "target_time"):
                arguments.pop(field, None)
            if mode == "range":
                if time_range.get("start_time"):
                    arguments["start_time"] = str(time_range["start_time"])
                if time_range.get("end_time"):
                    arguments["end_time"] = str(time_range["end_time"])
                arguments["time_mode"] = "range"
            elif mode in {"nearest", "latest_before"}:
                if time_range.get("target_time"):
                    arguments["target_time"] = str(time_range["target_time"])
                arguments["time_mode"] = mode
            elif mode == "latest":
                arguments["time_mode"] = "latest"
            else:
                # A vague "recent" query without a user-specified window uses the MCP's
                # stable default; the ReAct model must not invent a moving 7/30-day range.
                arguments["time_mode"] = "default"
        return call.model_copy(update={"arguments": arguments})

    @classmethod
    def _react_action_signature(cls, call: AgentCall, state: dict[str, Any]) -> str:
        call = cls._normalize_react_call_for_state(call, state)
        arguments = dict(call.arguments or {})
        # These fields change presentation/cost but not the evidence identity.
        for key in ("objective", "limit"):
            arguments.pop(key, None)
        from app.services.sensor_references import code_tokens
        if call.agent_id == FUZZY_ENTITY_AGENT_ID:
            arguments.pop("query", None)
            arguments["literal_codes"] = sorted(code_tokens(str(call.arguments.get("query") or "") + " " + call.objective)
                                                 or code_tokens(state.get("query")))
        if call.tool_id in PHM_SENSOR_TOOL_IDS:
            bound, missing = build_sensor_arguments(call.tool_id, state, call.arguments)
            if not missing:
                arguments = bound
        entity = current_turn_entity(state)
        if call.tool_id != KNOWLEDGE_TOOL_ID:
            arguments["bound_identity"] = {k: entity.get(k) for k in ("equip_no", "point_no", "space_id") if entity.get(k)}
            arguments["sensor_revision"] = (state.get("sensor_target") or {}).get("revision")
        return (
            f"{call.call_type}:{call.target_id}:"
            + json.dumps(arguments, ensure_ascii=False, sort_keys=True, default=str)
        )

    async def _apply_completion_gate(
        self,
        state: dict[str, Any],
        verdict: SupervisorVerdict,
        *,
        allow_gap_plan: bool,
    ) -> tuple[SupervisorVerdict, dict[str, Any]]:
        """Apply one shared completion contract before an ANSWERABLE verdict may finish."""
        if verdict.verdict != SupervisorVerdictType.ANSWERABLE:
            return verdict, {}

        evaluation = evaluate_completion(state)
        update: dict[str, Any] = {"completion_evaluation": evaluation}

        if evaluation.get("status") == STATUS_PRESENTATION_ONLY:
            results = [item for item in state.get("answer_results") or [] if isinstance(item, dict)]
            if results:
                from app.services.query_results import render_result
                fact_text = render_result(results[-1])
                update.update(query_fact_text=fact_text, deterministic_output_ready=True)
                state = {**state, **update}
                evaluation = evaluate_completion(state)
                update["completion_evaluation"] = evaluation

        await self._emit(
            {**state, **update},
            "completion.evidence.checked",
            {
                "status": evaluation.get("status"),
                "gaps": evaluation.get("gaps") or [],
                "limitations": evaluation.get("limitations") or [],
                "requirements": state.get("evidence_requirements") or [],
            },
            stage="completion",
            actor_type="planner",
            actor_id="evidence_completion",
            status="COMPLETED",
            output_payload={"completion_evaluation": evaluation},
        )

        if evaluation.get("status") == STATUS_NEED_MORE_EVIDENCE:
            if allow_gap_plan:
                gap_state = {**state, "completion_evaluation": evaluation}
                planned = await self.supervisor.plan_completion_gap(
                    gap_state,
                    self._descriptors(state.get("registry_snapshot") or {}),
                    self.tool_registry.list_descriptors(),
                )
                if planned:
                    await self._emit(
                        {**state, **update},
                        "planner.capability.selected",
                        {
                            "reason": "completion_gap",
                            "missing_evidence": evaluation.get("gaps") or [],
                            "calls": [item.model_dump(mode="json") for item in planned],
                        },
                        stage="planning",
                        actor_type="planner",
                        actor_id="capability_planner",
                        status="COMPLETED",
                    )
                    return (
                        SupervisorVerdict(
                            verdict=SupervisorVerdictType.NEED_MORE_EVIDENCE,
                            answerable=False,
                            next_call=planned[0],
                            next_calls=planned,
                            missing_evidence=[str(item) for item in evaluation.get("gaps") or []],
                            reason_summary="统一完成检查发现可补齐缺口，仅补查缺失部分。",
                        ),
                        update,
                    )
            limitations = list(evaluation.get("limitations") or [])
            for gap in evaluation.get("gaps") or []:
                if isinstance(gap, dict) and gap.get("type") == "missing_field":
                    limitations.append(f"现有能力未补齐字段：{gap.get('field')}")
                elif isinstance(gap, dict) and gap.get("type") == "missing_evidence":
                    limitations.append(f"现有能力未补齐证据：{gap.get('evidence')}")
                elif isinstance(gap, dict):
                    limitations.append("现有能力未补齐本轮所需的结构化结果")
            evaluation = {**evaluation, "status": STATUS_PARTIAL_FINAL, "limitations": list(dict.fromkeys(limitations))}
            update["completion_evaluation"] = evaluation

        if evaluation.get("status") == STATUS_PARTIAL_FINAL:
            contract = evaluation.get("contract") if isinstance(evaluation.get("contract"), dict) else {}
            if not bool(contract.get("allow_partial", True)):
                return (
                    SupervisorVerdict(
                        verdict=SupervisorVerdictType.CANNOT_ANSWER,
                        answerable=False,
                        missing_evidence=list(evaluation.get("limitations") or []),
                        final_answer_draft="本轮必要信息尚未完整取得，现有证据不足以按要求给出结论。",
                        reason_summary="统一完成检查确认存在不可接受的必要信息缺口。",
                    ),
                    update,
                )
            if contract.get("response_mode") == "facts_only" and state.get("query_fact_text"):
                limitations = [str(x) for x in evaluation.get("limitations") or [] if str(x).strip()]
                if limitations:
                    note = "数据范围说明：" + "；".join(limitations) + "。"
                    fact_text = str(state.get("query_fact_text") or "").rstrip()
                    if note not in fact_text:
                        update["query_fact_text"] = fact_text + "\n\n" + note
                    update["deterministic_output_ready"] = True
            verdict = verdict.model_copy(update={
                "answerable": True,
                "missing_evidence": [str(x) for x in evaluation.get("limitations") or []],
                "reason_summary": "统一完成检查确认只能按已知事实部分回答，并明确数据局限。",
            })
        elif evaluation.get("status") == STATUS_COMPLETE:
            verdict = verdict.model_copy(update={
                "answerable": True,
                "reason_summary": "统一完成检查已确认本轮回答要求满足。",
            })
        return verdict, update

    @trace_node("normal.react.decide")
    async def normal_react_decide(self, state: dict[str, Any]) -> dict[str, Any]:
        """Choose the next action or independent read group for normal mode.

        A mature Recipe is reused only when task understanding explicitly marked it as
        a high-confidence fit.  Otherwise the supervisor evaluates current evidence and
        composes MCP capabilities dynamically.  The loop is intentionally small and
        rejects repeated action signatures before execution.
        """

        state = apply_depth(state)
        if confirmation_required(state):
            return confirmation_update(state, min(current_graph_runtime().agent_runtime.settings.diagnosis_confirmation_ttl_seconds, current_graph_runtime().agent_runtime.settings.task_context_ttl_seconds))
        await current_graph_runtime().set_task_status("PLANNING")
        settings = current_graph_runtime().agent_runtime.settings
        current_step = int(state.get("current_step") or 0)
        calls = int(state.get("agent_call_count") or 0)
        max_steps = max(2, int(getattr(settings, "normal_max_execution_steps", 8) or 8))
        max_calls = max(1, int(getattr(settings, "normal_max_agent_calls", 6) or 6))
        at_execution_bound = current_step >= max_steps or calls >= max_calls
        completion_update: dict[str, Any] = {}

        if at_execution_bound:
            if state.get("observations"):
                # A safety bound is an internal execution policy, not a user-facing
                # business failure.  Synthesize the best evidence already acquired.
                verdict = SupervisorVerdict(
                    verdict=SupervisorVerdictType.ANSWERABLE,
                    answerable=True,
                    final_answer_draft="",
                    reason_summary="normal ReAct bound reached; synthesize acquired evidence",
                )
            else:
                verdict = SupervisorVerdict(
                    verdict=SupervisorVerdictType.CANNOT_ANSWER,
                    answerable=False,
                    final_answer_draft="当前系统尚未取得可用于回答的业务证据。",
                    reason_summary="normal ReAct bound reached without evidence",
                )
        else:
            intent = state.get("business_intent") if isinstance(state.get("business_intent"), dict) else {}
            workflow = state.get("business_workflow") if isinstance(state.get("business_workflow"), dict) else {}
            use_recipe = bool(
                self.workflow_registry is not None
                and not (getattr(settings, "phm_asset_unified_query_enabled", False)
                         and (intent.get("asset_query") or {}).get("active"))
                and workflow.get("workflow_id")
                and bool(intent.get("recipe_recommended"))
                and float(intent.get("recipe_match_confidence") or 0.0)
                >= float(getattr(settings, "normal_recipe_min_confidence", 0.92))
            )

            verdict = None
            if use_recipe:
                ready_calls = self.workflow_registry.next_recipe_calls(state, workflow)
                next_call = ready_calls[0] if ready_calls else None
                workflow_observations = [
                    item for item in state.get("observations") or []
                    if isinstance(item, dict)
                    and item.get("workflow_id") == workflow.get("workflow_id")
                ]
                hard_failed = any(
                    str(item.get("status") or "").upper()
                    in {"FAILED", "ERROR", "TIMEOUT", "NEEDS_INPUT", "REJECTED"}
                    and self.workflow_registry.is_hard_failure(
                        workflow, str(item.get("workflow_step_id") or "unknown")
                    )
                    for item in workflow_observations
                )
                if next_call is not None and not hard_failed:
                    verdict = SupervisorVerdict(
                        verdict=SupervisorVerdictType.NEED_MORE_EVIDENCE,
                        next_call=next_call,
                        next_calls=ready_calls,
                        reason_summary=(
                            f"复用成熟 Recipe {workflow.get('workflow_id')}/{workflow.get('variant_id')} 的下一步骤。"
                        ),
                    )
                elif not hard_failed:
                    verdict = SupervisorVerdict(
                        verdict=SupervisorVerdictType.ANSWERABLE,
                        answerable=True,
                        reason_summary="成熟 Recipe 已完成，现有证据可以进入最终回答。",
                    )
                # If a Recipe step failed, do not make the Recipe a cage.  Fall through
                # to dynamic planning so another MCP path can still satisfy the goal.

            if verdict is None:
                async with detail_span(
                    state,
                    code="supervisor.react.llm",
                    name="主控大模型：ReAct 下一步决策",
                    description="依据目标、证据契约和当前观察选择一个最有价值的下一步",
                    category="llm",
                    metrics={"model": settings.supervisor_model or ""},
                ) as perf:
                    verdict = await self.supervisor.next_react_action(
                        state,
                        self._descriptors(state.get("registry_snapshot") or {}),
                        self.tool_registry.list_descriptors(),
                    )
                    if perf is not None:
                        perf["metrics"]["verdict"] = str(verdict.verdict.value)

        # Recipe completion and dynamic ReAct completion share the same content-level
        # contract. A successful tool/finished Recipe is not itself proof that the
        # requested members, fields and range are complete.
        verdict, completion_update = await self._apply_completion_gate(
            state, verdict, allow_gap_plan=not at_execution_bound
        )
        if completion_update:
            state = {**state, **completion_update}

        # Repair prerequisites before batching; only ready reads may overlap.
        candidates = verdict.next_calls or ([verdict.next_call] if verdict.next_call else [])
        ready, seen = [], set(state.get("visited_actions") or [])
        for candidate in candidates:
            if requires_detailed(candidate, state) and depth(state) != "detailed":
                # Applies equally to Recipes and dynamic model calls, before prerequisite repair.
                if diagnosis_requested(state) and depth(state) == "unspecified":
                    return confirmation_update(state, min(settings.diagnosis_confirmation_ttl_seconds, settings.task_context_ttl_seconds))
                continue
            if candidate.tool_id == KNOWLEDGE_TOOL_ID and (state.get("business_intent") or {}).get("knowledge_opt_out"):
                continue
            call = self._repair_react_missing_entity_call(candidate, state)
            call = self._repair_react_entity_capability_call(call, state)
            call = self._normalize_react_call_for_state(call, state)
            signature = self._react_action_signature(call, state)
            from app.asset_query_contract import TOOL_ID as COLLECTION_TOOL_ID
            prior_asset = [o for o in state.get("observations") or [] if o.get("tool_id") == COLLECTION_TOOL_ID]
            targeted_repair = (call.tool_id == COLLECTION_TOOL_ID and len(prior_asset) == 1
                               and prior_asset[0].get("error_code") == "QUERY_RESULT_MISMATCH")
            if call.agent_id == FUZZY_ENTITY_AGENT_ID:
                from app.orchestration.entity_lifecycle import has_explicit_different_code
                targeted_repair = targeted_repair or (int(state.get("sensor_identity_corrections") or 0) < 1
                    and has_explicit_different_code(str(state.get("query") or ""), current_turn_entity(state)))
            if signature not in seen or targeted_repair:
                ready.append(call)
                seen.add(signature)
        ready = independent_calls(ready, min(settings.normal_max_parallel_calls, max_calls - calls))
        verdict.next_calls = ready
        verdict.next_call = ready[0] if ready else None
        if candidates and not ready:
            verdict = SupervisorVerdict(verdict=SupervisorVerdictType.ANSWERABLE, answerable=True,
                                        reason_summary="重复查询已拦截，使用已取得的证据说明结果与缺口。")
            verdict, duplicate_completion_update = await self._apply_completion_gate(
                state, verdict, allow_gap_plan=False
            )
            if duplicate_completion_update:
                completion_update.update(duplicate_completion_update)
                state = {**state, **duplicate_completion_update}

        await self._emit(
            state,
            "supervisor.evaluation.completed",
            {
                "verdict": verdict.verdict.value,
                "reason_summary": verdict.reason_summary,
                "next_call_type": verdict.next_call.call_type if verdict.next_call else None,
                "next_target_id": verdict.next_call.target_id if verdict.next_call else None,
                "execution_strategy": "recipe" if (
                    isinstance(state.get("business_intent"), dict)
                    and state.get("business_intent", {}).get("recipe_recommended")
                ) else "dynamic_react",
            },
            stage="normal_react_decide",
            actor_type="supervisor",
            actor_id="builtin.supervisor",
            status="COMPLETED",
            output_payload=verdict.model_dump(mode="json"),
        )
        return {
            "business_intent": state.get("business_intent") or {},
            "business_workflow": state.get("business_workflow") or {},
            "analysis_depth": depth(state),
            **completion_update,
            "supervisor_verdict": verdict.model_dump(mode="json"),
            "next_call": verdict.next_call.model_dump(mode="json") if verdict.next_call else None,
            "next_calls": [call.model_dump(mode="json") for call in verdict.next_calls],
            "supervisor_answer_draft": verdict.final_answer_draft,
            "current_step": current_step + 1,
        }

    @staticmethod
    def route_normal_react(state: dict[str, Any]) -> str:
        if state.get("final_status") == "WAITING_CONFIRMATION":
            return "confirmation"
        return "execute" if state.get("next_call") else "finalize"

    @trace_node("normal.react.execute")
    async def normal_react_execute(self, state: dict[str, Any]) -> dict[str, Any]:
        await current_graph_runtime().set_task_status("EXECUTING")
        raw_calls = state.get("next_calls") or [state["next_call"]]
        calls = [AgentCall.model_validate(raw) for raw in raw_calls]
        visited = list(state.get("visited_actions") or [])
        if len(calls) == 1:
            update = await self._execute_call(state, calls[0])
        else:
            update = await execute_independent(calls, state, self._execute_call)
        observations = []
        for call in calls:
            rows = [dict(item) for item in update.get("observations") or []
                    if isinstance(item, dict) and item.get("call_id") == call.call_id]
            observations.extend(self._attach_workflow_error(state, call, rows))
        update["observations"] = observations
        update.update({
            "agent_call_count": int(state.get("agent_call_count") or 0) + len(calls),
            "visited_actions": visited + [self._react_action_signature(call, state) for call in calls],
            "next_call": None, "next_calls": [],
        })
        return update

    @trace_node("normal.react.finalize")
    async def normal_react_finalize(self, state: dict[str, Any]) -> dict[str, Any]:
        state = {**state, "available_tool_ids": [d.tool_id for d in self.tool_registry.list_descriptors() if d.enabled]}
        contract = (state.get("business_intent") or {}).get("completion_contract") or {}
        deterministic_facts_only = bool(
            contract.get("response_mode") == "facts_only" and state.get("query_fact_text")
        )
        if deterministic_facts_only:
            knowledge_rows = []
        else:
            state, knowledge_rows = await self._ensure_business_knowledge(state)
        draft = str(state.get("supervisor_answer_draft") or "").strip()
        if state.get("query_fact_text"):
            state = {**state, "query_facts_ready": True}
        verdict = dict(state.get("supervisor_verdict") or {})
        verdict_type = str(verdict.get("verdict") or "")

        if deterministic_facts_only:
            from app.output.streaming import AnswerStreamSession, AnswerStreamStopped
            from app.integrations.openai.chat_client import ModelStreamDelta
            async def deterministic_events():
                yield ModelStreamDelta("final", str(state.get("query_fact_text") or ""))
            session = AnswerStreamSession(state, current_graph_runtime(), source="deterministic_facts")
            try:
                answer = await session.run(deterministic_events())
            except AnswerStreamStopped:
                state["final_status"] = "STOPPED"
                answer = session.meta["content"]
        # A ReAct evaluation draft is already a public, evidence-aware answer.  Reuse it
        # directly to avoid a second LLM call on small questions.
        elif draft and not state.get("query_fact_text") and not state.get("understanding_results") and not any(x.get("tool_id") == KNOWLEDGE_TOOL_ID for x in state.get("observations") or []) and verdict_type in {"ANSWERABLE", "NEED_USER_INPUT", "CANNOT_ANSWER"}:
            from app.output.streaming import AnswerStreamSession, AnswerStreamStopped
            from app.integrations.openai.chat_client import ModelStreamDelta
            async def draft_events():
                yield ModelStreamDelta("final", draft)
            session = AnswerStreamSession(state, current_graph_runtime(), source="draft")
            try:
                answer = await session.run(draft_events())
            except AnswerStreamStopped:
                state["final_status"] = "STOPPED"
                answer = session.meta["content"]
        else:
            # The LLM can still explain general principles when no tool could answer.
            answer = await self._stream_supervisor_answer(state, expert=False)
        final_status = "STOPPED" if state.get("final_status") == "STOPPED" else "FAILED" if identity_service_failed(state) else "WAITING_INPUT" if verdict_type == "NEED_USER_INPUT" else "COMPLETED"
        return {"final_answer": answer, "final_status": final_status, "observations": knowledge_rows, "understanding_results":state.get("understanding_results") or []}

    def _repair_react_missing_entity_call(
        self,
        call: AgentCall,
        state: dict[str, Any],
    ) -> AgentCall:
        if call.call_type != "tool":
            return call
        required_level = self._required_entity_level_for_call(call, state)
        if required_level == "none" or self._has_entity_for_level(state, required_level):
            return call
        if (
            required_level == "point"
            and can_down_drill_equipment_to_point(state)
            and self._asset_point_down_drill_enabled()
        ):
            requirement = capability_requirement_for_tool(call.tool_id or "")
            return self._point_down_drill_call(
                point_type=requirement.point_type if requirement is not None else "vibration_acceleration",
                objective=requirement.objective if requirement is not None else "在当前设备内部定位真实测点",
            )
        return AgentCall(
            call_id=str(uuid4()),
            call_type="agent",
            agent_id=FUZZY_ENTITY_AGENT_ID,
            objective=f"定位 {call.objective or call.target_id} 所需的真实{required_level}实体",
            arguments={"required_entity_level": required_level},
        )

    def _repair_react_entity_capability_call(
        self,
        call: AgentCall,
        state: dict[str, Any],
    ) -> AgentCall:
        """Expand equipment context to the point capability required by the selected tool.

        Normal ReAct executes one call per iteration. The Asset point lookup is executed first;
        the next iteration sees the resolved point and can issue the original MCP capability safely.
        """

        if call.call_type != "tool":
            return call
        requirement = capability_requirement_for_tool(call.tool_id or "")
        if requirement is None:
            return call
        if not should_expand_equipment_capability(state, call.tool_id or ""):
            return call
        if not self._asset_point_down_drill_enabled():
            return call
        return self._point_down_drill_call(
            point_type=requirement.point_type,
            objective=requirement.objective,
        )
