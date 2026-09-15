import json
from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4
import pytest
from app.query_contract import QueryPlan, ResultFollowup, normalize_plan, QUERY_TOOL_ID, RESULT_TOOL_ID
from app.services.query_results import make_result,resolve_result,rank_rows,render_result,load_results
from app.orchestration.evidence_compiler import compile_evidence
from app.orchestration.entity_resolution_layer import UnifiedEntityResolutionLayer
from app.tools.structured_query import StructuredQueryHandler
from app.tools.contracts import ToolCallRequest
from app.workflows.contracts import WorkflowIntentClassification, AssetSemanticConstraint
from app.orchestration.entity_dependency import identity_dependency


def saved():
    r=make_result(plan={"domain":"health","target":"equipment","operation":"rank","anchor":"space","limit":2},
        rows=[{"entity_key":"E1","equip_no":"E1","name":"设备一","health_score":50},
              {"entity_key":"E2","equip_no":"E2","name":"设备二","health_score":55}],root={"space_id":"A"},total=96)
    r["source_message_id"]="M1"
    return r


def test_followup_skips_entity_lookup_and_preserves_membership():
    r=saved()
    p=normalize_plan({"result_followup":{"active":True,"action":"enrich","fields":["area"]}}, {"query":"每个设备加上所在区域"})
    intent=WorkflowIntentClassification.model_validate(p).model_dump()
    assert not identity_dependency({"business_intent":intent})["required"]
    got=resolve_result({"answer_results":[r]},p["result_followup"])
    assert [x["equip_no"] for x in got["rows"]]==["E1","E2"]
    with pytest.raises(ValueError):resolve_result({"answer_results":[r]},{"selection":"all"})


def test_result_ids_cannot_escape_own_context_and_ordinal_bounds():
    r=saved()
    with pytest.raises(ValueError):resolve_result({"answer_results":[r]},{"source_result_id":"someone-else"})
    with pytest.raises(ValueError):resolve_result({"answer_results":[r]},{"selection":"ordinals","ordinals":[3]})
    assert resolve_result({"answer_results":[r]},{"selection":"ordinals","ordinals":[2]})["rows"][0]["equip_no"]=="E2"


@pytest.mark.asyncio
async def test_enrichment_partial_failure_preserves_original_rank_and_scores():
    class Asset:
        async def call_tool(self,name,args):
            if args["equip_no"]=="E2":raise TimeoutError()
            return {"success":True,"equipment":{"equip_no":"E1","space_path":"集团/车间"}}
    h=StructuredQueryHandler(Asset(),None,SimpleNamespace(normal_max_parallel_calls=2))
    source=saved();result=await h.followup(None,{"answer_results":[source]},ResultFollowup(active=True,action="enrich",fields=["area"]))
    assert [r["equip_no"] for r in result["rows"]]==["E1","E2"]
    assert [r["health_score"] for r in result["rows"]]==[50,55]
    assert result["rows"][0]["area"]=="集团/车间"
    assert "supplement_status" in result["rows"][1]
    assert source==saved_with_id(source)  # source is not mutated


def saved_with_id(source):
    fresh=saved();fresh["result_id"]=source["result_id"];fresh["created_at"]=source["created_at"];return fresh


def test_evidence_compiler_retains_later_health_source_after_large_asset_payload():
    obs=[{"tool_id":"asset","status":"SUCCESS","tool_result":{"structured_content":{"devices":[{"name":"设备"+str(i),"raw":"X"*5000} for i in range(96)]}}},
         {"tool_id":"health","status":"SUCCESS","tool_result":{"structured_content":{"count":96,"score":60,"grade":"重点关注","items":[{"score":60}]*96}}}]
    text=compile_evidence(obs);got=json.loads(text)
    assert len(text)<48000 and len(got)==2 and got[1]["evidence"]["score"]==60
    assert "omitted_items" in text


def test_new_object_beats_reference_but_parent_reference_scopes_drilldown():
    check=UnifiedEntityResolutionLayer._has_explicit_current_identity
    assert check({"reference_target_level":"equipment","equipment":{"raw_text":"另一台水泵"}},"equipment")
    assert not check({"reference_target_level":"space","equipment":{"raw_text":"1号泵"}},"equipment")
    assert check({"reference_target_level":"equipment","equip_no":{"raw_text":"E1001"}},"equipment")


def test_term_normalization_never_fabricates_raw_provenance():
    assert AssetSemanticConstraint.model_validate({"raw_text":"白灰三车间"}).retrieval_text=="白灰三车间"
    with pytest.raises(ValueError):AssetSemanticConstraint.model_validate({"retrieval_text":"白灰三车间"})


def test_structured_plan_and_query_render_fields():
    p=normalize_plan({"query_plan":{"active":True,"domain":"health","operation":"rank","limit":10}}, {"query":"最低十台"})
    intent=WorkflowIntentClassification.model_validate(p)
    assert intent.goal_frame.anchor_entity_level=="space" and not intent.recipe_recommended
    r=saved();r["display_format"]="list"
    assert "1. " in render_result(r) and "| ---" not in render_result(r)
    with pytest.raises(ValueError):QueryPlan(start_time="2026-09-10",end_time="2026-09-09")


@pytest.mark.asyncio
async def test_current_area_health_does_not_expand_descendants():
    h=StructuredQueryHandler(None,None,SimpleNamespace())
    root,rows,complete,total=await h.candidates(None,{"entity":{"entity_type":"space","space_id":"164","space_name":"白灰三车间"}},QueryPlan(active=True,domain="health",target="space",operation="detail"))
    assert complete and total==1 and rows[0]["space_id"]=="164"


@pytest.mark.asyncio
async def test_model_plan_is_accepted_and_routed_once_without_another_planner_call():
    from unittest.mock import AsyncMock
    from app.config import Settings
    from app.orchestration.supervisor.agent import SupervisorAgent
    from app.reasoning.model_registry import ModelRegistry
    from app.workflows.registry import BusinessWorkflowRegistry
    from app.tools.structured_query import descriptors
    settings=Settings.model_construct()
    model=SimpleNamespace(complete_json_profile=AsyncMock(return_value={"workflow_id":"none","variant_id":"none","confidence":.99,
        "reason":"按健康度列出三台设备","time_range":{"mode":"none"},"asset_semantics":{"area":{"raw_text":"白灰三车间"}},
        "query_plan":{"active":True,"domain":"health","operation":"rank","limit":3}}))
    agent=SupervisorAgent(model_client=model,model_registry=ModelRegistry(settings),settings=settings)
    intent,_=await agent.classify_business_workflow({"query":"列出白灰三车间健康度最低的三台设备"},BusinessWorkflowRegistry())
    assert not intent['classification_degraded'] and intent['query_plan']['active']
    state={"business_intent":intent}
    first=await agent.next_react_action(state,[],descriptors(settings))
    assert first.next_call.tool_id==QUERY_TOOL_ID
    state['observations']=[{"tool_id":QUERY_TOOL_ID,"status":"SUCCESS"}]
    end=await agent.next_react_action(state,[],descriptors(settings))
    assert end.answerable and model.complete_json_profile.await_count==1


@pytest.mark.asyncio
async def test_count_reformat_keeps_115_instead_of_counting_empty_member_rows():
    h=StructuredQueryHandler(None,None,SimpleNamespace())
    source=make_result(plan={"domain":"asset","target":"equipment","operation":"count","anchor":"space"},rows=[],total=115)
    result=await h.followup(None,{"answer_results":[source]},ResultFollowup(active=True,action="render"))
    assert result['total_count']==115 and '115' in render_result(result)


@pytest.mark.asyncio
async def test_query_facts_stream_before_final_model_finishes():
    import asyncio
    from app.orchestration.runtime import graph_runtime_scope
    from app.orchestration.nodes import ConversationGraphNodes
    from app.integrations.openai.chat_client import ModelStreamDelta
    from test_v11_orchestration import make_runtime
    rt=make_runtime();sent=asyncio.Event();release=asyncio.Event()
    async def publish(task,event,data,**kwargs):
        if event=='answer.delta' and '设备一' in data.get('content',''):sent.set()
    rt.agent_runtime.event_service.publish=publish
    async def generate(state,expert=False):
        assert state['query_facts_ready'];await release.wait();yield ModelStreamDelta('final','结合数据时间进行现场核查。')
    nodes=ConversationGraphNodes(registry_service=None,content_understanding_service=None,
        supervisor=SimpleNamespace(synthesize_events=generate),agent_executor=None,tool_registry=None,tool_executor=None)
    state={'task_id':rt.agent_runtime.task_id,'query_fact_text':render_result(saved())}
    with graph_runtime_scope(rt):
        task=asyncio.create_task(nodes._stream_supervisor_answer(state,expert=False))
        await asyncio.wait_for(sent.wait(),1)
        assert not task.done()
        release.set();answer=await task
    assert '设备一' in answer and '现场核查' in answer


@pytest.mark.asyncio
@pytest.mark.parametrize('group_by,expected', [('equipment', 2), ('area', 1)])
async def test_alarm_group_uses_identity_and_compares_aggregated_periods(group_by, expected):
    from unittest.mock import AsyncMock
    h=StructuredQueryHandler(None,None,SimpleNamespace())
    candidates=[{'equip_no':'E1','name':'同名泵','area':'集团/车间','space_id':'A'},
                {'equip_no':'E2','name':'同名泵','area':'集团/车间','space_id':'A'}]
    h.candidates=AsyncMock(return_value=({'space_id':'A'},candidates,True,2))
    h.invoke=AsyncMock(return_value={'success':True,'complete':True,'items':[
        {'equip_no':'E1','alarm_count':2,'previous_alarm_count':1},
        {'equip_no':'E2','alarm_count':6,'previous_alarm_count':3}]})
    plan=QueryPlan(active=True,domain='alarm',operation='compare',group_by=group_by,
        start_time='2026-09-02',end_time='2026-09-03',comparison_start='2026-09-01',comparison_end='2026-09-02',alarm_state='all')
    result=await h.execute(None,{},plan)
    assert len(result['rows'])==expected
    assert sum(r['alarm_count'] for r in result['rows'])==8
    assert all(r['change_percent']==100 for r in result['rows'])
    if group_by=='equipment':assert {r['equip_no'] for r in result['rows']}=={'E1','E2'}


@pytest.mark.asyncio
async def test_alarm_median_uses_complete_collection_instead_of_single_device_detail():
    from unittest.mock import AsyncMock
    h=StructuredQueryHandler(None,None,SimpleNamespace())
    h.candidates=AsyncMock(return_value=({'space_id':'A'},[{'equip_no':'E1'},{'equip_no':'E2'}],True,2))
    h.invoke=AsyncMock(return_value={'success':True,'complete':True,'items':[
        {'equip_no':'E1','alarm_count':2},{'equip_no':'E2','alarm_count':6}]})
    h.advanced=AsyncMock(return_value={'validated':True})
    plan=QueryPlan(active=True,domain='alarm',operation='detail',statistic='median',advanced_expression='报警记录数中位数')
    assert await h.execute(None,{},plan)=={'validated':True}
    assert h.invoke.call_args.args[1]=='query_alarm_collection'
    assert h.invoke.call_args.args[2]['operation']=='list'
    assert len(h.advanced.call_args.args[0])==2


@pytest.mark.parametrize('plan',[
    {'domain':'health','operation':'group','group_by':'area'},
    {'domain':'alarm','operation':'group','group_by':'warn_level'},
])
def test_unsupported_group_semantics_cannot_return_device_counts_as_health_or_alarm_grades(plan):
    with pytest.raises(ValueError):QueryPlan(active=True,**plan)
