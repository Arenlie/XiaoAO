"""Replay production task understanding with controlled external service outcomes."""
import copy
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.agents.catalog import FUZZY_ENTITY_AGENT_ID
from app.integrations.phm_asset_mcp.client import PhmAssetMcpClient
from app.integrations.phm_asset_mcp.errors import PhmAssetMcpError
from app.integrations.phm_asset_mcp.entity_adapter import adapt_asset_resolution
from app.orchestration.entity_dependency import identity_dependency
from app.orchestration.graphs.normal_graph import build_normal_graph
from app.orchestration.runtime import graph_runtime_scope
from app.orchestration.supervisor.contracts import AgentCall
from app.tools.alarm_context import infer_required_entity_level
from app.tools.contracts import ToolCallResult
from app.tools.phm_data_mcp import PHM_QUERY_ALARM_RECORDS_TOOL_ID, PHM_QUERY_HEALTH_SCORE_TOOL_ID
from test_v111_lookup_before_answer import ENTITY, result, setup
from test_v11_orchestration import make_runtime

FIXTURE = json.loads((Path(__file__).parent / "fixtures/incident_810d98cd.json").read_text())


def intent():
    return copy.deepcopy(FIXTURE["classification"])


def upstream_error():
    error = FIXTURE["asset_error"]
    return PhmAssetMcpError(error["code"], error["operator_message"],
                           public_message=error["public_message"], retryable=error["retryable"])


@pytest.mark.asyncio
async def test_real_incident_service_fault_is_failed_task_without_name_question_or_data_calls():
    nodes, asset, order = setup(intent=intent())
    asset.lookup.side_effect = upstream_error()
    nodes.supervisor.model_client = SimpleNamespace(plan_tool_calls=AsyncMock(side_effect=AssertionError("No planner after service failure")))
    runtime = make_runtime()
    runtime.agent_runtime.settings.performance_monitor_enabled = True
    with graph_runtime_scope(runtime):
        final = await build_normal_graph(nodes).ainvoke({"task_id": runtime.agent_runtime.task_id,
            "query": FIXTURE["query"], "conversation_id": "TEST-C", "branch_id": "TEST-B",
            "observations": [], "active_entity": {**ENTITY, "equip_no": "STALE-EQUIPMENT"}})
    assert asset.lookup.await_count == 2
    assert not order and final["final_status"] == "FAILED"
    assert final["entity_dependency"]["required"] is True
    assert final["entity_dependency"]["level"] == "equipment"
    assert final["entity_result"]["decision"]["lookup_attempts"] == 2
    assert not final["resolved_entity"] and final["invalidate_active_entity"]
    error = final["entity_result"]["error"]
    assert error["code"] == "UPSTREAM_ERROR" and error["retryable"] is True
    assert "client has been closed" in error["operator_message"]
    assert [f["attempt"] for f in error["upstream_failures"]] == [1, 2]
    assert "检索服务" in final["final_answer"]
    for forbidden in ("请补充", "可继续询问", "UPSTREAM_ERROR", "equip_no", "RuntimeError", "STALE-EQUIPMENT"):
        assert forbidden not in final["final_answer"]
    events = runtime.agent_runtime.event_service.publish.call_args_list
    failed_codes = {c.args[2]["code"] for c in events if c.args[1] == "performance.span.failed"}
    assert {"asset.resolve.mcp", "entity.resolve"} <= failed_codes


@pytest.mark.asyncio
async def test_real_incident_success_uses_equipment_code_for_alarm_and_health():
    nodes, asset, _ = setup(intent=intent())
    records = []
    class Model:
        async def plan_tool_calls(self, **kwargs):
            return SimpleNamespace(content="", tool_calls=[SimpleNamespace(call_id="health",
                name="call_tool_" + PHM_QUERY_HEALTH_SCORE_TOOL_ID.replace(".", "_"),
                arguments={"required_entity_level": "equipment", "scope_type": "device", "time_mode": "default"})])
        async def stream_profile(self, **kwargs):
            yield "本次未发现报警，健康度为 88 分；这些数据不足以排除所有故障。"
    nodes.supervisor.model_client = Model()

    async def execute(*, request, **kwargs):
        assert asset.lookup.await_count == 1
        records.append((request.tool_id, copy.deepcopy(request.arguments)))
        if request.tool_id == PHM_QUERY_ALARM_RECORDS_TOOL_ID:
            assert request.arguments["equip_no"] == ENTITY["equip_no"]
            assert not request.arguments.get("space_id"), "Device alarm must not become an area-only filter"
            data = {"success": True, "records": [], "record_count": 0}
        else:
            assert request.tool_id == PHM_QUERY_HEALTH_SCORE_TOOL_ID
            assert request.arguments["scope_type"] == "device"
            assert request.arguments["scope_id"] == ENTITY["equip_no"]
            data = {"success": True, "score": 88, "scope_type": "device", "device_code": ENTITY["equip_no"]}
        return ToolCallResult(tool_id=request.tool_id, status="SUCCESS", content="已取得测试数据", structured_content=data)

    nodes.tool_executor = SimpleNamespace(execute=execute)
    runtime = make_runtime()
    with graph_runtime_scope(runtime):
        final = await build_normal_graph(nodes).ainvoke({"task_id": runtime.agent_runtime.task_id,
            "query": FIXTURE["query"], "conversation_id": "TEST-C", "branch_id": "TEST-B", "observations": []})
    assert final["final_status"] == "COMPLETED"
    assert {tool for tool, _ in records} == {PHM_QUERY_ALARM_RECORDS_TOOL_ID, PHM_QUERY_HEALTH_SCORE_TOOL_ID}
    assert asset.lookup.call_args.kwargs["required_entity_level"] == "equipment"


@pytest.mark.asyncio
async def test_one_transient_retry_recovers_without_reinterpreting_or_dropping_target():
    nodes, asset, _ = setup(intent=intent())
    asset.lookup.side_effect = [upstream_error(), result()]
    runtime = make_runtime()
    with graph_runtime_scope(runtime):
        update = await nodes.resolve_entity_context({"task_id": runtime.agent_runtime.task_id, "query": FIXTURE["query"]})
    assert update["entity_result"]["status"] == "UNIQUE"
    assert update["entity_result"]["lookup_recovery"]["attempts"] == 2
    assert asset.lookup.call_args_list[0] == asset.lookup.call_args_list[1]
    assert update["resolved_entity"]["equip_no"] == ENTITY["equip_no"]
    assert nodes.route_after_entity_resolution(update) == "continue"


@pytest.mark.asyncio
async def test_retry_cannot_extend_configured_entity_timeout_budget():
    nodes, asset, _ = setup(intent=intent())
    asset.timeout_seconds = .02
    cancelled = asyncio.Event()
    async def slow_lookup(**kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
    asset.lookup.side_effect = slow_lookup
    runtime = make_runtime()
    with graph_runtime_scope(runtime):
        update = await asyncio.wait_for(nodes.resolve_entity_context({
            "task_id": runtime.agent_runtime.task_id, "query": FIXTURE["query"]}), .5)
    assert cancelled.is_set() and asset.lookup.await_count == 1
    assert update["entity_result"]["error"]["code"] == "PHM_ASSET_MCP_TIMEOUT"


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["NOT_FOUND", "MULTIPLE"])
async def test_real_business_outcomes_do_not_trigger_service_retries(outcome):
    nodes, asset, _ = setup(status=outcome, intent=intent())
    runtime = make_runtime()
    with graph_runtime_scope(runtime):
        update = await nodes.resolve_entity_context({"task_id": runtime.agent_runtime.task_id, "query": FIXTURE["query"]})
    assert asset.lookup.await_count == 1
    assert update["entity_result"]["status"] == outcome
    assert not update.get("errors")
    assert nodes.route_after_entity_resolution(update) == ("selection" if outcome == "MULTIPLE" else "failure")


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["ERROR", "NOT_FOUND"])
async def test_late_planner_identity_requirement_uses_same_failure_contract(status):
    nodes, asset, _ = setup(intent=intent())
    asset.lookup.side_effect = upstream_error() if status == "ERROR" else None
    if status == "NOT_FOUND":
        asset.lookup.return_value = result("NOT_FOUND")
    runtime = make_runtime()
    with graph_runtime_scope(runtime):
        update = await nodes._execute_call({"task_id": runtime.agent_runtime.task_id, "query": FIXTURE["query"],
            "business_intent": intent(), "entity_result": {"status": "NO_LOOKUP", "need_lookup": False}},
            AgentCall(call_id="late-lookup", agent_id=FUZZY_ENTITY_AGENT_ID, objective="核验设备",
                      arguments={"required_entity_level": "equipment"}))
    assert asset.lookup.await_count == (2 if status == "ERROR" else 1)
    assert nodes.route_after_agent_execution(update) == "failure"
    assert update["observations"][0]["call_id"] == "late-lookup"


@pytest.mark.asyncio
async def test_supervisor_cannot_translate_service_error_to_name_clarification():
    nodes, asset, _ = setup(intent=intent())
    asset.lookup.side_effect = upstream_error()
    runtime = make_runtime()
    with graph_runtime_scope(runtime):
        state = {"query": FIXTURE["query"], "task_id": runtime.agent_runtime.task_id}
        state.update(await nodes.resolve_entity_context(state))
        verdict = await nodes.supervisor.next_react_action(state, [], nodes.tool_registry.list_descriptors())
    assert verdict.verdict.value == "CANNOT_ANSWER"
    assert "请补充" not in verdict.final_answer_draft


@pytest.mark.asyncio
async def test_asset_client_preserves_structured_error_retryability(monkeypatch):
    import mcp
    class Client:
        def __init__(self, *args): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def call_tool(self, *args):
            return SimpleNamespace(is_error=False, structured_content={"success": False, "error": FIXTURE["asset_error"]})
    monkeypatch.setattr(mcp, "Client", Client)
    with pytest.raises(PhmAssetMcpError) as caught:
        await PhmAssetMcpClient(url="http://test/mcp").resolve_entity(query=FIXTURE["query"])
    assert caught.value.retryable is True
    assert "client has been closed" in caught.value.message
    assert caught.value.details["code"] == "UPSTREAM_ERROR"


@pytest.mark.asyncio
async def test_invalid_resolved_identity_is_service_contract_failure_not_area_fallback():
    nodes, asset, _ = setup(intent=intent())
    asset.lookup.side_effect = None
    asset.lookup.return_value = result().model_copy(update={"resolved_entity": {"entity_type": "space", "space_id": "AREA-ONLY"}})
    runtime = make_runtime()
    with graph_runtime_scope(runtime):
        update = await nodes.resolve_entity_context({"task_id": runtime.agent_runtime.task_id, "query": FIXTURE["query"]})
    assert asset.lookup.await_count == 1
    assert update["entity_result"]["error"]["code"] == "PHM_ASSET_MCP_INVALID_RESULT"
    assert nodes.route_after_entity_resolution(update) == "failure"


@pytest.mark.parametrize("payload", [{"status": "RESOLVED"}, {"status": "UNKNOWN"}])
def test_invalid_asset_contract_not_silently_mapped_to_not_found(payload):
    with pytest.raises(PhmAssetMcpError):
        adapt_asset_resolution(payload)


@pytest.mark.parametrize("word", ["斗提", "刮板输送机", "ABX非标机组"])
def test_alarm_scope_does_not_depend_on_equipment_vocabulary(word):
    assert infer_required_entity_level(f"白灰三车间1#{word}有问题吗？", "none", structured_level="equipment") == "equipment"
    assert infer_required_entity_level(f"白灰三车间1#{word}有问题吗？", "area", structured_level="equipment") == "equipment"


def test_root_area_and_general_explanation_have_different_identity_requirements():
    collection = intent()
    collection["goal_frame"].update(anchor_entity_level="area", target_is_system_output=True)
    assert identity_dependency({"business_intent": collection})["level"] == "area"
    assert infer_required_entity_level("白灰三车间哪些设备有报警？", "equipment", structured_level="area") == "area"
    general = {"asset_semantics": {"needs_asset_lookup": False}, "goal_frame": {
        "anchor_entity_level": "none", "evidence_types": ["general_knowledge"], "can_answer_from_context": True}}
    assert identity_dependency({"business_intent": general})["required"] is False


def test_named_live_query_cannot_skip_identity_due_to_inconsistent_none_flags():
    current = intent()
    current["goal_frame"]["anchor_entity_level"] = "none"
    current["asset_semantics"]["needs_asset_lookup"] = False
    dependency = identity_dependency({"business_intent": current})
    assert dependency["required"] is True and dependency["level"] == "equipment"
