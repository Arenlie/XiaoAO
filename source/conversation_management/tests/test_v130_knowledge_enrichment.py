import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from app.config import Settings
from app.orchestration.knowledge_enrichment import needs_enrichment, enrichment_query
from app.orchestration.nodes import ConversationGraphNodes
from app.orchestration.runtime import graph_runtime_scope
from app.tools.contracts import ToolDescriptor
from app.tools.registry import ToolRegistry
from app.tools.dify_knowledge import DifyKnowledgeHandler, KNOWLEDGE_TOOL_ID, KNOWLEDGE_NAMES
from app.tools.phm_data_mcp import PHM_QUERY_ALARM_RECORDS_TOOL_ID
from app.tools.phm_diagnosis_mcp import PHM_DIAGNOSIS_POINT_TOOL_ID
from app.tools.phm_asset_mcp import PHM_ASSET_QUERY_EQUIPMENT_INFO_TOOL_ID
from app.tools.phm_sensor_mcp import PHM_SENSOR_TOOL_IDS
from test_v11_orchestration import make_runtime
from test_v11_knowledge import request, record


@pytest.mark.parametrize('tool',[PHM_QUERY_ALARM_RECORDS_TOOL_ID,PHM_DIAGNOSIS_POINT_TOOL_ID,
                               PHM_ASSET_QUERY_EQUIPMENT_INFO_TOOL_ID,*PHM_SENSOR_TOOL_IDS])
def test_business_reads_enrich_even_when_classifier_omits_knowledge_flag(tool):
    state={'business_intent':{'knowledge_required':False},'observations':[{'tool_id':tool,'status':'SUCCESS'}]}
    assert needs_enrichment(state,Settings.model_construct())


def test_chitchat_opt_out_and_previous_attempt_do_not_force_extra_retrieval():
    settings=Settings.model_construct()
    assert needs_enrichment({'query':'你好'},settings)
    state={'business_intent':{'knowledge_opt_out':True,'knowledge_required':True}}
    assert not needs_enrichment(state,settings)
    for status in ('SUCCESS','FAILED'):
        state={'business_intent':{'knowledge_required':True},'observations':[{'tool_id':KNOWLEDGE_TOOL_ID,'status':status}]}
        assert not needs_enrichment(state,settings)


def test_knowledge_query_uses_resolved_names_and_observed_faults():
    state={'query':'这个设备有异常吗？','resolved_entity':{'equip_name':'白灰三车间1#斗提'},
           'observations':[{'tool_id':PHM_QUERY_ALARM_RECORDS_TOOL_ID,'tool_result':{'structured_content':{
               'records':[{'fault_type_name':'偏置电压异常'}]}}}]}
    query=enrichment_query(state)
    assert '白灰三车间1#斗提' in query and '偏置电压异常' in query
    assert len(query)<=250


@pytest.mark.asyncio
async def test_slow_knowledge_base_does_not_erase_fast_sources_and_tasks_close():
    settings=Settings.model_construct(dify_knowledge_api_key='test',dify_knowledge_base_url='http://dify.test/v1',dify_knowledge_timeout_seconds=1)
    stopped=asyncio.Event()
    async def handle(req):
        if req.method=='GET':
            return httpx.Response(200,json={'data':[{'id':str(i),'name':n} for i,n in enumerate(KNOWLEDGE_NAMES)]})
        if '/1/' in req.url.path:
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()
        return httpx.Response(200,json={'records':[record(segment=req.url.path)]})
    req=request().model_copy(update={'knowledge_timeout_seconds':.03})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        result=await asyncio.wait_for(DifyKnowledgeHandler(client,settings)(req,None),.5)
    assert result.status=='SUCCESS'
    assert len(result.structured_content['sources'])==2
    assert result.structured_content['partial'] is True and stopped.is_set()
    assert result.structured_content['timed_out_knowledge_bases']==['历史案例库']


@pytest.mark.asyncio
async def test_final_enrichment_runs_after_business_even_at_tool_budget_and_reaches_synthesis():
    registry=ToolRegistry()
    registry.register_tool(ToolDescriptor(tool_id=KNOWLEDGE_TOOL_ID,display_name='知识库',description='检索',provider_type='local'))
    captured=[]
    class Nodes(ConversationGraphNodes):
        async def _execute_call(self,state,call):
            assert state['observations'][0]['tool_id']==PHM_DIAGNOSIS_POINT_TOOL_ID
            assert '轴承润滑不良' in call.arguments['query']
            captured.append(call)
            return {'observations':[{'call_id':call.call_id,'tool_id':KNOWLEDGE_TOOL_ID,'status':'SUCCESS',
                'tool_result':{'structured_content':{'sources':[{'dataset_id':'D','document_id':'DOC','segment_id':'S',
                    'knowledge_base':'企业智库','document_name':'维护规程.pdf','position':6,'content':'检查轴承润滑。'}]}}}]}
        async def _stream_supervisor_answer(self,state,expert):
            from app.output.customer import render_answer, citation_context
            assert len(citation_context(state))==1
            return render_answer('诊断提示润滑异常，可按维护规程检查润滑[1]。',state)
    nodes=Nodes(registry_service=None,content_understanding_service=None,supervisor=None,
                agent_executor=None,tool_registry=registry,tool_executor=None)
    state={'task_id':'task','query':'诊断这台设备','agent_call_count':6,'current_step':8,
           'supervisor_answer_draft':'先前草稿不能遗漏新知识','supervisor_verdict':{'verdict':'ANSWERABLE'},
           'observations':[{'tool_id':PHM_DIAGNOSIS_POINT_TOOL_ID,'status':'SUCCESS','tool_result':{
               'structured_content':{'conclusion':'轴承润滑不良'}}}]}
    runtime=make_runtime()
    with graph_runtime_scope(runtime):
        result=await nodes.normal_react_finalize(state)
    assert len(captured)==1 and result['final_status']=='COMPLETED'
    assert '维护规程.pdf' in result['final_answer'] and '第 6 段' in result['final_answer']
    assert len(result['observations'])==1  # reducer receives only new evidence


@pytest.mark.asyncio
async def test_enrichment_failure_preserves_business_state_without_forced_followups():
    registry=ToolRegistry()
    registry.register_tool(ToolDescriptor(tool_id=KNOWLEDGE_TOOL_ID,display_name='知识库',description='检索',provider_type='local'))
    nodes=ConversationGraphNodes(registry_service=None,content_understanding_service=None,supervisor=None,
                agent_executor=None,tool_registry=registry,tool_executor=None)
    nodes._execute_call=AsyncMock(side_effect=RuntimeError('unavailable'))
    state={'query':'设备信息','resolved_entity':{'equip_no':'A'},'observations':[{'tool_id':PHM_ASSET_QUERY_EQUIPMENT_INFO_TOOL_ID,'status':'SUCCESS'}]}
    with graph_runtime_scope(make_runtime()):
        enriched,rows=await nodes._ensure_business_knowledge(state)
    assert enriched['resolved_entity']==state['resolved_entity']
    assert len(enriched['observations'])==2 and rows[0]['status']=='FAILED'
    assert 'unavailable' not in str(rows)
    assert len(state['observations'])==1
