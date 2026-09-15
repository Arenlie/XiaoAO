import copy
import json
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import httpx
import pytest

from app.output.customer import render_answer
from app.output.streaming import CustomerTextStream
from app.output.progress import progress_payload, professional_text
from app.orchestration.diagnosis_choice import public_confirmation, QUESTION, depth
from app.orchestration.runtime import graph_runtime_scope
from test_v140_diagnosis_confirmation import db
from test_incident_1e617332 import make_nodes, make_runtime, point, FIXTURE


def reference_state():
    return {"observations": [{"tool_id": "knowledge.dify.retrieve", "tool_result": {
        "structured_content": {"sources": [dict(dataset_id="D", document_id=f"D{i}",
            segment_id=f"S{i}", knowledge_base="企业智库", document_name=f"manual_{i}.pdf",
            position=i, content=f"来源{i}的实际内容。") for i in range(1, 13)]}}}]}


@pytest.mark.parametrize("size", [1, 2, 3, 7, 1000])
def test_stream_reference_order_and_bibliography_remain_consistent(size):
    raw = "甲[2]。乙[1]。丙[12]。丁[5]。再次引用[2]。无效[99]。"
    renderer = CustomerTextStream(reference_state())
    body = "".join(renderer.feed(raw[i:i+size]) for i in range(0, len(raw), size)) + renderer.finish()
    assert body == "甲[1]。乙[2]。丙[3]。丁[4]。再次引用[1]。无效。"
    refs = renderer.citations()
    assert [row["number"] for row in refs] == [1, 2, 3, 4]
    assert [row["document_id"] for row in refs] == ["D2", "D1", "D12", "D5"]
    assert [row["segment_id"] for row in refs] == ["S2", "S1", "S12", "S5"]
    assert [row["position"] for row in refs] == [2, 1, 12, 5]
    bibliography = renderer.bibliography()
    assert bibliography.startswith("\n\n---\n\n**知识库依据：**\n\n")
    assert "manual_12.pdf" in bibliography and "[12]" not in bibliography
    complete = render_answer(raw, reference_state())
    assert complete.startswith(body) and "\n\n---\n\n**知识库依据：**" in complete
    # A distinct answer cannot inherit the previous answer's numbering.
    independent = CustomerTextStream(reference_state())
    assert independent.feed("引用[5]。") + independent.finish() == "引用[1]。"


@pytest.mark.parametrize("size", [1, 7, 1000])
def test_model_cannot_append_its_own_bold_reference_list(size):
    raw = "结论[2]。\n\n**知识库依据：**\n\n[99] 自编文献"
    renderer = CustomerTextStream(reference_state())
    result = "".join(renderer.feed(raw[i:i+size]) for i in range(0, len(raw), size)) + renderer.finish() + renderer.bibliography()
    assert "自编文献" not in result and result.count("**知识库依据：**") == 1
    assert "自编文献" not in render_answer(raw, reference_state())


@pytest.mark.asyncio
@pytest.mark.parametrize("status,boundary", [
    ("COMPLETED", "task.completed"), ("FAILED", "task.failed"),
    ("STOPPED", "task.stopped"), ("WAITING_INPUT", "task.waiting_input"),
    ("WAITING_SELECTION", "entity.selection.required"),
    ("WAITING_CONFIRMATION", "diagnosis.mode.required")])
async def test_missing_optional_span_ends_before_every_task_boundary(db, status, boundary):
    row = db.seed(status=status)
    span = str(uuid4())
    await db.events.publish(row.tid, "performance.span.started", {
        "code": "supervisor.entity_intake.llm", "name_cn": "快速识别查询对象"}, span_id=span, status="STARTED")
    await db.events.publish(row.tid, boundary, {})
    events = await db.events.list_task_events(row.tid)
    assert [r.event_type for r in events] == ["performance.span.started", "performance.span.completed", boundary]
    assert events[1].span_id == span and events[1].status == "CANCELLED"
    assert events[1].payload["affects_task"] is False
    await db.events.close_open_spans(row.tid)
    await db.events.publish(row.tid, "performance.span.completed", {}, span_id=span)
    assert len(await db.events.list_task_events(row.tid)) == 3


def sse_events(text):
    result = []
    for block in text.split("\n\n"):
        event = next((s[7:] for s in block.splitlines() if s.startswith("event: ")), None)
        if event:
            data = json.loads(next(s[6:] for s in block.splitlines() if s.startswith("data: ")))
            result.append((event, data, block))
    return result


@pytest.mark.asyncio
@pytest.mark.parametrize("include_reasoning", [False, True])
@pytest.mark.parametrize("late_end", [False, True])
async def test_legacy_sse_drains_or_recovers_missing_ends_before_closing(db, include_reasoning, late_end):
    row = db.seed(status="COMPLETED")
    span = str(uuid4())
    await db.events.publish(row.tid, "performance.span.started", {"code": "supervisor.entity_intake.llm"}, span_id=span)
    # Reproduce a pre-upgrade task, whose task terminal was committed too early.
    with patch.object(db.events, "_close_open_spans_locked", AsyncMock()):
        await db.events.publish(row.tid, "task.completed", {})
    if late_end:
        await db.events.publish(row.tid, "performance.span.completed", {}, span_id=span, status="CANCELLED")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=db.app), base_url="http://test") as client:
        response = await client.get(f"/chat/v1/tasks/{row.tid}/events", params={"X-User-Token": "A", "include_reasoning": include_reasoning})
    events = sse_events(response.text)
    names = [event for event, _, _ in events]
    assert names.index("performance.span.completed") < names.index("task.completed")
    ids = [int(line[4:].split("-")[0]) for line in response.text.splitlines() if line.startswith("id: ")]
    assert ids == sorted(set(ids))
    assert events[-1][1]["stream_terminal"] is True
    assert events[-1][1]["recovered_from_task_state"] is True


def test_progress_replaces_existing_text_and_never_duplicates_protocol_fields():
    source = {"name_cn": "Asset MCP：实体检索", "span_id": "immutable", "status": "COMPLETED",
        "description_cn": "调用 Asset MCP 完成语义理解、Embedding、PostgreSQL 候选召回、Reranker 精排和实体决策",
        "execution_trace": {"spans": [{"name": "MongoDB 数据库：get_device_data",
            "description": "在 MongoDB 数据库 中执行 get_device_data", "status": "COMPLETED"}]}}
    result = progress_payload(source)
    assert result.keys() == source.keys()
    assert result["span_id"] == "immutable" and result["status"] == "COMPLETED"
    assert "MCP" not in result["description_cn"]
    assert "get_device_data" not in str(result["execution_trace"])
    assert "Asset MCP" in source["name_cn"]
    assert professional_text("快速识别查询对象") == "快速识别查询对象"
    assert professional_text("执行 query_points") == "设备测点查询"
    assert professional_text("执行 unknown_future_tool") == "执行本项查询或分析"
    assert professional_text("phm-asset-mcp 接收并执行 MCP 工具 query_points") == "执行本项查询或分析。"


@pytest.mark.asyncio
async def test_old_and_new_confirmations_expose_text_without_buttons(db):
    row = db.seed()
    pending = await db.service.get_diagnosis_confirmation(row.tid, "A")
    assert pending["options"] == [] and "直接回复" in pending["question"]
    assert "不会自动启动" in pending["question"]
    assert "点击" not in pending["question"]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,text", [("quick", "先给个初步判断"), ("detailed", "按专业流程完整诊断")])
async def test_text_choice_restores_verified_target_and_selected_depth(mode, text):
    nodes, asset, model, calls = make_nodes()
    rt = make_runtime()
    saved = {"query": "诊断17架轧机的减速机测点", "resolved_entity": point(),
        "business_intent": copy.deepcopy(FIXTURE["classification"]),
        "business_workflow": {"workflow_id": "diagnosis_analysis", "variant_id": "point"}}
    classification = copy.deepcopy(FIXTURE["classification"])
    classification.update(analysis_depth=mode, analysis_depth_explicit=True,
        analysis_depth_confidence=.99, responds_to_diagnosis_choice=True)
    model.complete_json_profile.return_value = classification
    with graph_runtime_scope(rt):
        result = await nodes.resolve_entity_context({"task_id": rt.agent_runtime.task_id, "query": text,
            "pending_diagnosis_context": {"query": saved["query"], "target": point(), "resume_state": saved}})
    assert result["resolved_entity"] == point() and depth(result) == mode
    assert result["query"] == saved["query"] and asset.lookup.await_count == 0
    assert bool(result["business_workflow"]) is (mode == "detailed")
