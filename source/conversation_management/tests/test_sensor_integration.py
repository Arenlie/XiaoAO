"""Sensor routing through the real normal graph; external LLM/MCP are controlled."""
import asyncio
import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config import Settings
from app.content.understanding_service import ContentUnderstandingService
from app.orchestration.entity_resolution_layer import UnifiedEntityResolutionLayer
from app.orchestration.entity_selection_resume import build_selected_entity_result
from app.orchestration.graphs.normal_graph import build_normal_graph
from app.orchestration.nodes import ConversationGraphNodes
from app.orchestration.runtime import graph_runtime_scope
from app.orchestration.supervisor.agent import SupervisorAgent
from app.output.customer import render_answer
from app.schemas.entity import EntityLookupResult
from app.tools.contracts import ToolCallResult
from app.tools.phm_asset_mcp import phm_asset_tool_descriptors
from app.tools.phm_data_mcp import phm_data_tool_descriptors
from app.tools.phm_diagnosis_mcp import phm_diagnosis_tool_descriptors
from app.tools.phm_sensor_context import build_sensor_arguments, sensor_required_level
from app.tools.phm_sensor_mcp import phm_sensor_tool_descriptors, SENSOR_TOOL_BY_OPERATION, PHM_SENSOR_TOOL_IDS
from app.tools.registry import ToolRegistry
from app.workflows.registry import BusinessWorkflowRegistry
from test_v11_orchestration import make_runtime, assert_single_business_classification


def entity(device='DEV_1', scope='equipment'):
    result={'entity_type':scope,'equip_no':device,'equip_name':'测试设备1','space_id':'AREA','space_name':'测试车间','similarity':.98}
    if scope=='point':result.update(point_no=device+'_POINT',point_name='输入端测点')
    return result


def classification(op='active',scope='equipment',fault='偏置电压异常',recipe=True):
    return {'workflow_id':'sensor_information_query' if recipe else 'none','variant_id':op if recipe else 'none',
        'confidence':.99,'recipe_recommended':recipe,'recipe_match_confidence':.99,'reason':'用户要求查询传感器自检信息',
        'time_range':{'mode':'none'},'sensor_query':{'operation':op,'scope':scope,'fault_type':fault},
        'asset_semantics':{'needs_asset_lookup':scope!='global',
            'equipment':{'raw_text':'测试设备1','retrieval_text':'测试设备1'} if scope!='global' else {},
            'point':{'raw_text':'输入端测点','retrieval_text':'输入端测点'} if scope=='point' else {}},
        'goal_frame':{'goal':'查询传感器信息','anchor_entity_level':'none' if scope=='global' else scope}}


def make_nodes(c=None, rows=None, device='DEV_1', business_status='OK'):
    c=copy.deepcopy(c or classification())
    scope=c['sensor_query']['scope']
    settings=Settings.model_construct(phm_asset_mcp_url='http://test/asset',phm_data_mcp_url='http://test/data',phm_diagnosis_mcp_url='http://test/diagnosis')
    class Model:
        complete_json_profile=AsyncMock(return_value=c)
        plan_tool_calls=AsyncMock(side_effect=AssertionError('Sensor intent/recipe already declares the needed query'))
        async def stream_profile(self,**kwargs):
            yield '该测点未纳入监测，不能判断为在线或无故障。' if business_status=='NOT_MONITORED' else '查询结果显示偏置电压异常。设备编号：'+device+'。'
    model=Model()
    supervisor=SupervisorAgent(model_client=model,model_registry=SimpleNamespace(get=lambda _:None),settings=settings)
    registry=ToolRegistry()
    for factory in (phm_asset_tool_descriptors,phm_data_tool_descriptors,phm_diagnosis_tool_descriptors,phm_sensor_tool_descriptors):
        for descriptor in factory(settings):registry.register_tool(descriptor)
    rows=rows if rows is not None else [entity(device,'point' if scope=='point' else 'equipment')]
    response=EntityLookupResult(status='UNIQUE' if len(rows)==1 else 'MULTIPLE' if rows else 'NOT_FOUND',
        need_lookup=True,matches=copy.deepcopy(rows),match_count=len(rows),need_disambiguation=len(rows)>1,
        resolved_entity=copy.deepcopy(rows[0]) if len(rows)==1 else None,
        lookup_scope='point' if scope=='point' else 'equipment',top_similarity=.98,
        decision={'action':'SEARCH','target_entity_level':'point' if scope=='point' else 'equipment'})
    asset=SimpleNamespace(lookup=AsyncMock(return_value=response))
    calls=[]
    async def execute(*,request,**kwargs):
        calls.append(copy.deepcopy(request))
        assert request.tool_id in PHM_SENSOR_TOOL_IDS
        data={'success':True,'status':business_status,'query_type':'ACTIVE','message':'该测点未纳入监测。' if business_status=='NOT_MONITORED' else '已查询传感器信息。',
            'coverage':{'scope':scope,'status':business_status,'monitored_point_count':0 if business_status=='NOT_MONITORED' else 1},
            'records':[] if business_status=='NOT_MONITORED' else [{'equip_num':request.arguments.get('equip_num'),'point_num':request.arguments.get('point_num'),
                'fault_type_name':request.arguments.get('fault_type'),'fault_id':'F-REAL'}]}
        return ToolCallResult(tool_id=request.tool_id,status='SUCCESS',structured_content=data)
    nodes=ConversationGraphNodes(registry_service=SimpleNamespace(snapshot=AsyncMock(return_value={})),
        content_understanding_service=ContentUnderstandingService(session_factory=None,attachment_service=None,parser_registry=None,repository=None),
        supervisor=supervisor,agent_executor=None,tool_registry=registry,tool_executor=SimpleNamespace(execute=execute),
        workflow_registry=BusinessWorkflowRegistry(),entity_resolution_layer=UnifiedEntityResolutionLayer(asset))
    return nodes,asset,model,calls


async def run(nodes,rt,state=None):
    initial={'task_id':rt.agent_runtime.task_id,'conversation_id':'C','branch_id':'B','query':'测试设备1输入端测点有偏置电压异常吗？',
             'execution_mode':'normal','observations':[],**(state or {})}
    with graph_runtime_scope(rt):return await build_normal_graph(nodes).ainvoke(initial)


@pytest.mark.parametrize('op',['active','history','offline','monitoring','overview'])
async def test_every_sensor_recipe_resolves_device_then_calls_matching_tool(op):
    c=classification(op,fault='偏置电压异常' if op in {'active','history','overview'} else None)
    nodes,asset,model,calls=make_nodes(c)
    result=await run(nodes,make_runtime())
    assert result['final_status']=='COMPLETED'
    assert asset.lookup.call_args.kwargs['required_entity_level']=='equipment'
    assert [r.tool_id for r in calls]==[SENSOR_TOOL_BY_OPERATION[op]]
    assert calls[0].arguments['equip_num']=='DEV_1'
    if c['sensor_query']['fault_type']:assert calls[0].arguments['fault_type']=='偏置电压异常'
    assert 'point_num' not in calls[0].arguments
    model.plan_tool_calls.assert_not_awaited()


async def test_point_not_monitored_remains_explanatory_business_result():
    nodes,asset,model,calls=make_nodes(classification('monitoring','point',None),business_status='NOT_MONITORED')
    result=await run(nodes,make_runtime())
    assert result['final_status']=='COMPLETED'
    assert calls[0].arguments['point_num']=='DEV_1_POINT'
    assert '未纳入监测' in result['final_answer']
    assert '可继续询问' not in result['final_answer']
    model.plan_tool_calls.assert_not_awaited()


async def test_global_sensor_question_does_not_inherit_previous_device():
    nodes,asset,model,calls=make_nodes(classification('overview','global',None))
    result=await run(nodes,make_runtime(),{'query':'全部传感器现在情况怎么样','active_entity':entity('OLD')})
    assert result['final_status']=='COMPLETED'
    assert 'equip_num' not in calls[0].arguments and 'point_num' not in calls[0].arguments
    asset.lookup.assert_not_awaited()


async def test_dynamic_sensor_intent_retains_type_without_extra_diagnosis():
    nodes,asset,model,calls=make_nodes(classification(recipe=False))
    result=await run(nodes,make_runtime())
    assert result['final_status']=='COMPLETED'
    assert [c.tool_id for c in calls]==[SENSOR_TOOL_BY_OPERATION['active']]
    assert calls[0].arguments['fault_type']=='偏置电压异常'


async def test_switching_equipment_retrieves_new_identity():
    nodes,asset,model,calls=make_nodes(device='NEW_DEVICE')
    await run(nodes,make_runtime(),{'active_entity':entity('OLD_DEVICE'),'resolved_entity':entity('OLD_DEVICE'),
        'query':'测试设备1有偏置电压异常吗？'})
    assert calls[0].arguments['equip_num']=='NEW_DEVICE'
    assert asset.lookup.call_args.kwargs['allow_context_reuse'] is False


async def test_explicit_context_reference_and_fault_type_switch_are_preserved():
    c=classification(fault='温度异常')
    c['asset_semantics']['equipment']={}
    c['asset_semantics']['reference_target_level']='equipment'
    nodes,asset,model,calls=make_nodes(c)
    await run(nodes,make_runtime(),{'active_entity':entity(),'query':'那这个设备有温度异常吗？',
        'recent_messages':[{'role':'user','content':'测试设备1有偏置电压异常吗？'}]})
    assert calls[0].arguments['fault_type']=='温度异常'
    assert calls[0].arguments['equip_num']=='DEV_1'


async def test_model_recognized_sensor_type_is_not_escalated_to_diagnosis():
    c=classification(recipe=False)
    c['goal_frame'].update(evidence_types=['diagnosis_result','vibration'],diagnosis_requested=True,diagnosis_request_confidence=.99)
    c.update(workflow_id='diagnosis_analysis',variant_id='device',recipe_recommended=True)
    nodes,asset,model,calls=make_nodes(c)
    result=await run(nodes,make_runtime())
    assert result['business_intent']['goal_frame']['diagnosis_requested'] is False
    assert [r.tool_id for r in calls]==[SENSOR_TOOL_BY_OPERATION['active']]


async def test_ambiguous_entity_waits_and_selection_resumes_sensor_filters():
    nodes,asset,model,calls=make_nodes(rows=[entity('DEV_1'),entity('DEV_2')])
    rt=make_runtime()
    waiting=await run(nodes,rt)
    assert waiting['final_status']=='WAITING_SELECTION' and not calls
    selected=waiting['entity_result']['matches'][1]
    final=await run(nodes,rt,{'selection_resume':True,'selected_entity':selected,
        'business_intent':waiting['business_intent'],'business_workflow':waiting['business_workflow'],
        'entity_result':build_selected_entity_result(waiting['entity_result'],selected)})
    assert final['final_status']=='COMPLETED'
    assert calls[0].arguments['equip_num']=='DEV_2' and calls[0].arguments['fault_type']=='偏置电压异常'
    assert_single_business_classification(model)


async def test_same_user_two_tasks_do_not_mix_target_or_filters():
    a=make_nodes(classification(fault='偏置电压异常'),device='FIRST')
    b=make_nodes(classification(fault='温度异常'),device='SECOND')
    await asyncio.gather(run(a[0],make_runtime('same-user')),run(b[0],make_runtime('same-user')))
    assert a[3][0].arguments['equip_num']=='FIRST' and a[3][0].arguments['fault_type']=='偏置电压异常'
    assert b[3][0].arguments['equip_num']=='SECOND' and b[3][0].arguments['fault_type']=='温度异常'


def test_sensor_adapter_ignores_model_codes_and_uses_authoritative_time():
    c=classification('history','point')
    c['time_range']={'mode':'range','start_time':'2026-09-01T00:00:00+08:00','end_time':'2026-09-02T00:00:00+08:00'}
    state={'query':'测试设备1输入端测点历史偏置异常','business_intent':c,'selected_entity':entity(scope='point')}
    args,missing=build_sensor_arguments(SENSOR_TOOL_BY_OPERATION['history'],state,
        {'equip_num':'INVENTED','point_num':'INVENTED','fault_type':'数据卡死','start_time_from':'1900-01-01'})
    assert not missing
    assert args['equip_num']=='DEV_1' and args['point_num']=='DEV_1_POINT'
    assert args['fault_type']=='偏置电压异常'
    assert args['start_time_from']==c['time_range']['start_time']
    assert sensor_required_level(state,{'scope':'global'})=='point'


def test_customer_output_translates_sensor_fields_but_keeps_business_codes():
    state={'observations':[{'tool_result':{'structured_content':{'equip_num':'DEV_1','point_num':'P_2'}}}]}
    answer=render_answer('equip_num=DEV_1 point_num=P_2 monitor_status=NOT_MONITORED fault_type_name=偏置电压异常',state)
    assert 'DEV_1' in answer and 'P_2' in answer and '未纳入监测' in answer
    assert all(x not in answer for x in ('equip_num','point_num','monitor_status','fault_type_name'))


async def test_conflicting_global_scope_with_explicit_device_stays_scoped():
    c=classification()
    c['sensor_query']['scope']='global'
    nodes,asset,model,calls=make_nodes(c)
    result=await run(nodes,make_runtime())
    assert result['business_intent']['sensor_query']['scope']=='equipment'
    assert calls[0].arguments['equip_num']=='DEV_1'


@pytest.mark.parametrize('operation', ['active', 'monitoring', 'offline', 'overview'])
def test_historical_online_question_cannot_silently_query_current_snapshot(operation):
    c=classification(operation)
    c['time_range']={'mode':'range','start_time':'2026-09-01T00:00:00+08:00','end_time':'2026-09-02T00:00:00+08:00'}
    args,missing=build_sensor_arguments(SENSOR_TOOL_BY_OPERATION[operation],{'business_intent':c,'selected_entity':entity()}, {})
    assert missing and '历史' in missing[0]


async def test_parallel_sensor_targets_keep_distinct_fault_type_filters():
    from app.tools.independent_queries import IndependentQueryHandler
    from test_v11_multi_queries import request
    current=peak=0
    captured=[]
    async def resolve(**kwargs):
        return {'status':'RESOLVED','entity':{'entity_type':'equipment','equip_no':kwargs['query']}}
    async def call(name,args):
        nonlocal current,peak
        current+=1;peak=max(peak,current)
        await asyncio.sleep(.01)
        current-=1
        captured.append((name,dict(args)))
        return {'success':True,'status':'OK','records':[]}
    req=request(['E13','E17'],query='E13偏置电压异常和E17温度异常情况')
    for row,name in zip(req.arguments['queries'],['偏置电压异常','温度异常']):
        row.update(operation='sensor_active',fault_type=name)
    data=SimpleNamespace(call_tool=AsyncMock())
    handler=IndependentQueryHandler(SimpleNamespace(resolve_entity=resolve,call_tool=call),data,Settings.model_construct())
    result=await handler(req,None)
    assert peak==2 and not result.structured_content['partial']
    assert {(args['equip_num'],args['fault_type']) for _,args in captured}=={('E13','偏置电压异常'),('E17','温度异常')}
    data.call_tool.assert_not_awaited()
