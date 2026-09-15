from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config import Settings
from app.orchestration.entity_resolution_layer import UnifiedEntityResolutionLayer
from app.orchestration.graphs.normal_graph import build_normal_graph
from app.orchestration.nodes import ConversationGraphNodes
from app.orchestration.runtime import graph_runtime_scope
from app.orchestration.supervisor.agent import SupervisorAgent
from app.schemas.entity import EntityLookupResult
from app.tools.contracts import ToolCallResult
from app.tools.phm_data_mcp import PHM_QUERY_ALARM_RECORDS_TOOL_ID, phm_data_tool_descriptors
from app.tools.registry import ToolRegistry
from app.workflows.registry import BusinessWorkflowRegistry
from app.output.customer import render_answer
from test_v11_orchestration import make_runtime


QUERY='白灰三车间1#斗提有问题吗？'
ENTITY={'entity_type':'equipment','equip_no':'TEST-BUCKET-1','equip_name':'1#斗提',
        'space_id':'TEST-AREA','space_name':'白灰三车间','space_link':'/test/area/'}


def classification():
    return {'workflow_id':'none','variant_id':'none','confidence':.99,'recipe_recommended':False,
        'time_range':{},'asset_semantics':{
            'area':{'raw_text':'白灰三车间','retrieval_text':'白灰三车间'},
            'equipment':{'raw_text':'1#斗提','retrieval_text':'1号斗式提升机'},
            'needs_asset_lookup':False},
        'goal_frame':{'goal':'查询指定设备状态','anchor_entity_level':'equipment','target_entity_level':'equipment',
                      'evidence_types':['alarm'],'evidence_required':True,'operations':['retrieve','assess']},
        # Simulate the exact old model output that previously ended the graph early.
        'needs_clarification':True,'clarification_question':'尚未定位到报警查询所需的真实区域、设备或测点。',
        'clarification_missing_fields':['equip_no']}


def result(status='UNIQUE'):
    rows=[ENTITY] if status=='UNIQUE' else [ENTITY,{**ENTITY,'equip_no':'TEST-BUCKET-2'}] if status=='MULTIPLE' else []
    return EntityLookupResult(status=status,need_lookup=True,matches=rows,match_count=len(rows),
        need_disambiguation=status=='MULTIPLE',resolved_entity=ENTITY if status=='UNIQUE' else None,
        decision={'action':'SEARCH','target_entity_level':'equipment'},
        message='已找到测试设备' if status=='UNIQUE' else '没有唯一匹配')


def setup(*,status='UNIQUE',intent=None):
    order=[]
    async def lookup(**kwargs):
        order.append('asset')
        assert kwargs['query']==QUERY
        assert kwargs['semantic_hints']['needs_asset_lookup'] is True
        assert kwargs['semantic_hints']['area']['raw_text']=='白灰三车间'
        return result(status)
    asset=SimpleNamespace(lookup=AsyncMock(side_effect=lookup))
    class Model:
        async def plan_tool_calls(self,**kwargs):
            order.append('plan')
            assert 'TEST-BUCKET-1' in kwargs['user']
            return SimpleNamespace(content='',tool_calls=[SimpleNamespace(call_id='alarms',
                name='call_tool_'+PHM_QUERY_ALARM_RECORDS_TOOL_ID.replace('.','_'),
                arguments={'required_entity_level':'equipment','time_mode':'default'})])
        async def stream_profile(self,**kwargs):yield '当前查询范围内未发现报警记录，不能仅据此断言设备完全无故障。'
    supervisor=SupervisorAgent(model_client=Model(),model_registry=SimpleNamespace(get=lambda _:None),settings=Settings.model_construct())
    supervisor.classify_business_workflow=AsyncMock(return_value=(intent or classification(),None))
    registry=ToolRegistry()
    for descriptor in phm_data_tool_descriptors(Settings.model_construct()):
        registry.register_tool(descriptor)
    async def execute(*,request,**kwargs):
        order.append('data')
        assert request.tool_id==PHM_QUERY_ALARM_RECORDS_TOOL_ID
        assert 'TEST-BUCKET-1' in str(request.arguments)
        return ToolCallResult(tool_id=request.tool_id,status='SUCCESS',content='未发现报警记录。',
                             structured_content={'success':True,'record_count':0,'records':[]})
    class Nodes(ConversationGraphNodes):
        async def load_context(self,state):return {'registry_snapshot':{}}
        async def prepare_content(self,state):return {'understanding_results':[]}
    nodes=Nodes(registry_service=None,content_understanding_service=None,supervisor=supervisor,agent_executor=None,
        tool_registry=registry,tool_executor=SimpleNamespace(execute=execute),
        workflow_registry=BusinessWorkflowRegistry(),entity_resolution_layer=UnifiedEntityResolutionLayer(asset))
    return nodes,asset,order


@pytest.mark.asyncio
async def test_reported_question_reaches_asset_then_data_in_real_graph():
    nodes,asset,order=setup();runtime=make_runtime()
    with graph_runtime_scope(runtime):
        answer=await build_normal_graph(nodes).ainvoke({'task_id':runtime.agent_runtime.task_id,'conversation_id':'C',
            'branch_id':'B','query':QUERY,'execution_mode':'normal','observations':[]})
    assert asset.lookup.await_count==1 and order.index('asset')<order.index('data')
    assert answer['final_status']=='COMPLETED'
    assert '未发现报警' in answer['final_answer'] and '可继续询问' not in answer['final_answer']
    assert not answer.get('clarification_context')


@pytest.mark.asyncio
@pytest.mark.parametrize('status,route',[('MULTIPLE','selection'),('NOT_FOUND','failure')])
async def test_actual_asset_outcome_drives_next_step(status,route):
    nodes,asset,order=setup(status=status);runtime=make_runtime()
    with graph_runtime_scope(runtime):
        update=await nodes.resolve_entity_context({'task_id':runtime.agent_runtime.task_id,'query':QUERY})
    assert asset.lookup.await_count==1
    assert nodes.route_after_entity_resolution(update)==route
    assert update['entity_result']['status']==status
    assert 'needs_clarification' not in update['business_intent']


@pytest.mark.asyncio
async def test_concept_question_does_not_force_asset_lookup():
    intent={'workflow_id':'none','asset_semantics':{'needs_asset_lookup':False},
            'goal_frame':{'anchor_entity_level':'none','evidence_types':['conversation_context'],'can_answer_from_context':True}}
    nodes,asset,_=setup(intent=intent);runtime=make_runtime()
    with graph_runtime_scope(runtime):
        update=await nodes.resolve_entity_context({'task_id':runtime.agent_runtime.task_id,'query':'解释一下刚才的报警含义'})
    asset.lookup.assert_not_awaited()
    assert update['entity_result']['status']=='NO_LOOKUP'


@pytest.mark.asyncio
async def test_classifier_ignores_legacy_clarification_fields():
    model=SimpleNamespace(complete_json_profile=AsyncMock(return_value=classification()))
    supervisor=SupervisorAgent(model_client=model,model_registry=SimpleNamespace(get=lambda _:None),settings=Settings.model_construct())
    intent,_=await supervisor.classify_business_workflow({'query':QUERY},BusinessWorkflowRegistry())
    assert 'needs_clarification' not in intent and 'clarification_question' not in intent
    assert 'needs_clarification' not in model.complete_json_profile.call_args.kwargs['system']


def test_old_question_section_is_not_reinserted_and_answer_is_preserved():
    answer=render_answer('已完成查询。\n\n可继续询问：\n\n- 知识库有哪些资料？',{},suggestions=True)
    assert answer=='已完成查询。'
