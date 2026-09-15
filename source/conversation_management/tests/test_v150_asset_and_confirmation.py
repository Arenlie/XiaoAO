import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from sqlalchemy.orm import Session

from app.asset_query_contract import AssetQuery, QueryError, TOOL_ID, signature, verify_context
from app.services.asset_collections import build_query, collection_intent, fact_text, validate_response, signed_arguments
from app.orchestration.asset_query_policy import collection_verdict, validate_classification
from app.orchestration.diagnosis_choice import confirmation_update
from app.orchestration.supervisor.contracts import SupervisorVerdictType
from app.models.generation_task import GenerationTask
from app.models.message import Message
from app.services.generation_service import GenerationService
from app.repositories.conversation_repository import ConversationRepository
from app.repositories.message_repository import MessageRepository
from app.workers.generation_worker import GenerationWorker
from app.tools.contracts import ToolCallRequest
from test_v140_diagnosis_confirmation import db, store_context


def asset_state(mode="new"):
    return {"query":"总部钢铁有几台水泵", "resolved_entity":{"entity_type":"space","space_id":"153"},
        "business_intent":{"asset_query":{"active":True,"operation":"count","reference_mode":mode,
            "predicate":{"field":"equipment_class","operator":"is","value":"水泵"} if mode=="new" else None},
            "asset_semantics":{"descendant_recursive":True}},
        "memory_context":{"recent_asset_queries":[{"query_id":str(uuid4()),"source_message_id":"message-A","scope_name":"总部钢铁"}]}}


def response(query):
    q=AssetQuery.model_validate(query)
    return {"success":True,"schema_version":"2.0","operation":q.operation,"group_by":q.group_by,
        "request_signature":signature(q.model_dump(mode="json")),"count":115,"unknown_count":0,"result_complete":True,
        "criteria":{"scope":q.scope.model_dump() if q.scope else {},"predicate":q.predicate},"scope_name":"总部钢铁"}


def test_same_membership_used_for_count_list_group_and_new_scope_switches():
    state=asset_state(); count=build_query(state)
    state["business_intent"]["asset_query"]["operation"]="list"
    listed=build_query(state)
    assert listed["predicate"]==count["predicate"] and listed["scope"]==count["scope"]
    state=asset_state("same_set");state["business_intent"]["asset_query"].update(operation="group",group_by="equipment_class")
    grouped=build_query(state)
    assert "scope" not in grouped and grouped["reference"]["query_id"]==state["memory_context"]["recent_asset_queries"][-1]["query_id"]
    state=asset_state();state["resolved_entity"]["space_id"]="231"
    assert build_query(state)["scope"]["root_space_id"]=="231"


def test_no_user_global_reference_or_name_fallback():
    state=asset_state("same_set");state["memory_context"]={}
    with pytest.raises(QueryError):build_query(state)
    state=asset_state();state["business_intent"]["asset_query"]["predicate"]={"field":"equipment_name","operator":"contains","value":"水泵"}
    assert build_query(state)["predicate"]["field"]=="equipment_name"


def test_contract_conflict_and_main_model_semantics_requirements():
    state=asset_state("same_set")
    state["business_intent"]["asset_semantics"]["area"]={"raw_text":"其他区域"}
    with pytest.raises(ValueError):validate_classification(state["business_intent"],state)
    with pytest.raises(ValueError):validate_classification({"workflow_id":"asset_information_query","variant_id":"asset_collection"},state)
    query=build_query(asset_state());got=response(query);got["criteria"]["scope"]["root_space_id"]="wrong"
    with pytest.raises(QueryError):validate_response(query,got)


def test_legacy_success_cannot_satisfy_collection_goal_and_one_repair_only():
    state=asset_state();state["observations"]=[{"tool_id":"mcp.phm_asset.query_devices","status":"SUCCESS"}]
    descriptors=[SimpleNamespace(tool_id=TOOL_ID,enabled=True)]
    verdict=collection_verdict(state,descriptors)
    assert verdict.next_call.tool_id==TOOL_ID
    state["observations"]=[{"tool_id":TOOL_ID,"error_code":"QUERY_RESULT_MISMATCH"}]
    assert collection_verdict(state,descriptors).verdict==SupervisorVerdictType.NEED_MORE_EVIDENCE
    state["observations"]*=2
    assert collection_verdict(state,descriptors).verdict==SupervisorVerdictType.CANNOT_ANSWER


def test_server_signed_context_is_body_bound_and_arguments_stay_clean():
    state=asset_state();query=build_query(state)
    settings=SimpleNamespace(phm_asset_query_context_secret="x"*64,phm_asset_query_shared_catalog=True)
    request=ToolCallRequest(tool_id=TOOL_ID,task_id="t",conversation_id="c",branch_id="b",user_token="owner-A",arguments={"query":query},asset_query_call_id="call")
    args=signed_arguments(request,SimpleNamespace(settings=settings))
    assert "request_context" not in request.arguments and "owner-A" not in args["request_context"]
    claims=verify_context(settings.phm_asset_query_context_secret,args["request_context"],query)
    assert claims["call_id"]=="call" and claims["shared_catalog"] is True
    with pytest.raises(QueryError):verify_context(settings.phm_asset_query_context_secret,args["request_context"],{**query,"operation":"list"})


def test_fact_render_preserves_counts_unknown_grouping_and_professional_labels():
    data={"success":True,"count":115,"unknown_count":4,"scope_name":"总部钢铁","operation":"group","group_by":"equipment_class",
          "groups":[{"name":"水泵","count":115}],"criteria":{"predicate":{"field":"equipment_class"}},"freshness":"referenced_snapshot"}
    body=fact_text(data)
    assert "115" in body and "4" in body and "设备类别" in body and "上次查询" in body
    assert "unknown_count" not in body and "equipment_class" not in body


@pytest.mark.asyncio
@pytest.mark.parametrize("include_reasoning",[False,True])
async def test_confirmation_worker_ends_chat_turn_with_normal_sse_terminals(db,include_reasoning):
    row=db.seed(status="EXECUTING");await store_context(db,row)
    worker=GenerationWorker.__new__(GenerationWorker)
    worker.session_factory,worker.events,worker.redis,worker.settings=db.Bridge,db.events,db.redis,db.settings
    worker.concurrency=db.concurrency
    lease=await db.concurrency.acquire(row.cid,"A")
    with Session(db.engine) as s:
        task=s.get(GenerationTask,row.tid);assistant=s.get(Message,row.mid)
        s.expunge(task);s.expunge(assistant)
    state=confirmation_update({"query":"诊断一下设备A","resolved_entity":{"equip_no":"REAL-A"}},900)
    await worker._wait_for_diagnosis_confirmation(task,assistant,state,"normal",user_token="A",lease_id=lease)
    next_lease=await db.concurrency.acquire(row.cid,"A")
    await db.concurrency.release(row.cid,"A",lease)
    await db.concurrency.release(row.cid,"A",next_lease)
    with Session(db.engine) as s:
        assert s.get(GenerationTask,row.tid).status=="COMPLETED"
        msg=s.get(Message,row.mid)
        assert msg.status=="COMPLETED" and msg.metadata_json["diagnosis_confirmation"]["status"]=="PENDING"
    events=await db.events.list_task_events(row.tid)
    assert [e.event_type for e in events][-5:]==["answer.started","answer.delta","diagnosis.mode.required","answer.completed","task.completed"]
    pending=await db.service.get_diagnosis_confirmation(row.tid,"A")
    assert pending["status"]=="COMPLETED" and pending["stream_continues"] is False and pending["options"]==[]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=db.app),base_url="http://test") as client:
        output=await client.get(f"/chat/v1/tasks/{row.tid}/events",params={"X-User-Token":"A","include_reasoning":str(include_reasoning).lower(),"final_delta_event":"agent.output.delta"})
    assert "event: task.completed" in output.text and "event: answer.completed" in output.text
    assert "event: agent.output.delta" in output.text and "快速分析" in output.text
    assert db.queue.enqueue_generation.await_count==0


@pytest.mark.asyncio
@pytest.mark.parametrize("reply",["快速分析","详细诊断","设备B在线吗？"])
async def test_new_reply_after_completed_prompt_can_start_and_old_answer_stays_completed(db,reply):
    row=db.seed(status="COMPLETED");await store_context(db,row)
    service=GenerationService(session_factory=db.Bridge,redis=db.redis,settings=db.settings,
        conversation_repo=ConversationRepository(),message_repo=MessageRepository(),events=db.events,
        queue=db.queue,concurrency=db.concurrency,outbox=None,
        attachment_service=SimpleNamespace(validate_for_message=AsyncMock(return_value=[]),bind_rows=AsyncMock()))
    accepted=await service._send_impl(user_token="A",data_access_token="test-only",app_code="phm",content=reply,
        conversation_id=row.cid,idempotency_key=None,request_id="test")
    with Session(db.engine) as s:
        assert s.get(GenerationTask,row.tid).status=="COMPLETED"
        assert s.get(GenerationTask,accepted.task_id).status=="QUEUED"
        assert s.get(Message,row.mid).metadata_json["diagnosis_confirmation"]["status"]=="SUPERSEDED"
    context=json.loads(await db.redis.get(f"chat:task_context:{accepted.task_id}"))
    assert context["query"]==reply and "diagnosis_choice" not in context
    assert context["pending_diagnosis_context"]["query"]=="诊断一下设备A"


@pytest.mark.asyncio
async def test_legacy_pending_prompt_recovery_emits_terminal_once(db):
    row=db.seed()
    await db.service.get_diagnosis_confirmation(row.tid,"A")
    await db.service.get_diagnosis_confirmation(row.tid,"A")
    rows=await db.events.list_task_events(row.tid)
    assert sum(r.event_type=="task.completed" for r in rows)==1


@pytest.mark.parametrize('size',[1,2,7,1000])
def test_streamed_commentary_cannot_replace_authoritative_quantity(size):
    from app.output.asset_commentary import AssetCommentaryStream
    renderer=AssetCommentaryStream()
    raw='本次只有97台设备。[2]分类资料不足时应补充台账。典型频率为50Hz[1]。\n'
    value=''.join(renderer.feed(raw[i:i+size]) for i in range(0,len(raw),size))+renderer.feed('',final=True)
    assert '97' not in value and '[2]' not in value
    assert '补充台账' in value and '50Hz[1]' in value


@pytest.mark.asyncio
async def test_real_graph_executor_provider_routes_one_unified_call_and_streams_facts(monkeypatch):
    from test_incident_1e617332 import make_nodes, make_runtime, initial, run, asset_result
    from app.tools.providers.mcp import MCPToolProvider
    from app.tools.contracts import ToolProviderType
    from app.execution.resilient_tool_executor import ResilientToolExecutor
    nodes,asset,model,old_calls=make_nodes()
    rt=make_runtime()
    for settings in (rt.agent_runtime.settings,nodes.supervisor.settings):
        settings.phm_asset_unified_query_enabled=True
        settings.phm_asset_query_context_secret='x'*64
        settings.agent_default_max_retries=0
    descriptor=nodes.tool_registry.get_descriptor(TOOL_ID);descriptor.enabled=True
    calls=[]
    async def mcp_call(name,args):
        calls.append((name,args))
        assert name=='query_asset_collection'
        query=args['query']
        verify_context('x'*64,args['request_context'],query)
        return {**response(query),'query_id':str(uuid4()),'criteria_signature':'verified',
                'snapshot_available':True,'page_complete':True,'devices':[],'groups':[],'warnings':[]}
    provider=MCPToolProvider(phm_asset_client=SimpleNamespace(call_tool=mcp_call),phm_data_client=None,phm_diagnosis_client=None,phm_feature_client=None)
    nodes.tool_registry.register_provider(ToolProviderType.MCP,provider)
    nodes.tool_executor=ResilientToolExecutor(nodes.tool_registry,rt.agent_runtime.settings)
    remember=AsyncMock();monkeypatch.setattr('app.services.asset_collections.remember_collection',remember)
    classification={"workflow_id":"asset_information_query","variant_id":"asset_collection","confidence":.99,
        "time_range":{"mode":"none"},"asset_query":{"active":True,"operation":"count","predicate":{"field":"equipment_class","operator":"is","value":"水泵"}},
        "asset_semantics":{"needs_asset_lookup":True,"area":{"raw_text":"总部钢铁","retrieval_text":"总部钢铁"},
            "equipment_type":{"raw_text":"水泵","retrieval_text":"水泵"},"descendant_collection_requested":True,
            "descendant_target_level":"equipment","collection_output_mode":"count"},
        "goal_frame":{"goal":"统计总部钢铁的水泵数量","evidence_types":["asset"],"operations":["retrieve","aggregate"],"entity_level":"area"}}
    model.complete_json_profile.return_value=classification
    entity={"entity_type":"area","space_id":"153","space_name":"总部钢铁","space_link":"153","candidate_id":"area:153"}
    asset.lookup.return_value=asset_result([entity]).model_copy(update={"lookup_scope":"area","decision":{"action":"SEARCH","target_entity_level":"area"},"entity_constraints":{},"query_scope":{"target_entity_type":"area"}})
    state={**initial(rt),"query":"总部钢铁有多少台水泵","diagnosis_choice":{}}
    result=await run(nodes,state,rt)
    assert len(calls)==1 and not old_calls
    assert '115' in result['final_answer'] and result['asset_query_result']['count']==115
    assert remember.await_count==1
