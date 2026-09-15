"""New registry operation through the existing graph and identity boundary."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config import Settings
from app.output.customer import render_answer
from app.tools.phm_sensor_context import build_sensor_arguments, format_sensor_result
from app.tools.phm_sensor_mcp import SENSOR_TOOL_BY_OPERATION
from app.tools.independent_queries import IndependentQueryHandler
from test_sensor_integration import classification, make_nodes, run, entity
from test_v11_orchestration import make_runtime
from test_v11_multi_queries import request


@pytest.mark.parametrize('filters',[
    {}, {'feature_kind':'temperature'}, {'feature_kind':'bias'},
    {'monitor_status':'ONLINE'}, {'monitor_status':'SUSPENDED'}, {'waveform_enabled':False},
])
async def test_registry_recipe_preserves_model_understood_filters_after_entity_resolution(filters):
    c=classification('points',fault=None)
    c['sensor_query'].update(filters)
    nodes,asset,model,calls=make_nodes(c)
    result=await run(nodes,make_runtime(),{'query':'测试设备1有哪些传感器监测点及配置？'})
    assert result['final_status']=='COMPLETED'
    assert [r.tool_id for r in calls]==[SENSOR_TOOL_BY_OPERATION['points']]
    assert calls[0].arguments['equip_num']=='DEV_1'
    assert 'point_num' not in calls[0].arguments
    assert 'fault_type' not in calls[0].arguments
    for k,v in filters.items(): assert calls[0].arguments[k]==v
    assert asset.lookup.call_args.kwargs['required_entity_level']=='equipment'
    model.plan_tool_calls.assert_not_awaited()


async def test_global_dynamic_registry_query_ignores_old_entity_and_keeps_requested_limit():
    c=classification('points','global',None,recipe=False)
    c['sensor_query'].update(monitor_status='SUSPENDED',limit=1000)
    nodes,asset,model,calls=make_nodes(c)
    result=await run(nodes,make_runtime(),{'query':'列出当前全部暂停的监测点','active_entity':entity('OLD')})
    assert result['final_status']=='COMPLETED'
    assert calls[0].arguments=={'limit':1000,'monitor_status':'SUSPENDED'}
    asset.lookup.assert_not_awaited()


async def test_registry_device_switch_retrieves_and_filters_new_device():
    c=classification('points',fault=None)
    c['sensor_query']['feature_kind']='temperature'
    nodes,asset,model,calls=make_nodes(c,device='NEW_DEVICE')
    await run(nodes,make_runtime(),{'query':'改查测试设备1的温度监测点','active_entity':entity('OLD_DEVICE')})
    assert calls[0].arguments['equip_num']=='NEW_DEVICE'
    assert calls[0].arguments['feature_kind']=='temperature'
    assert asset.lookup.call_args.kwargs['allow_context_reuse'] is False


def test_current_null_filters_clear_previous_conditions_and_model_codes_cannot_override_identity():
    c=classification('points','point',None)
    c['sensor_query'].update(feature_kind=None,monitor_status=None,waveform_enabled=None)
    args,missing=build_sensor_arguments(SENSOR_TOOL_BY_OPERATION['points'],
        {'business_intent':c,'selected_entity':entity(scope='point')},
        {'equip_num':'INVENTED','point_num':'WRONG','feature_kind':'bias','monitor_status':'SUSPENDED','waveform_enabled':True})
    assert not missing
    assert args=={'equip_num':'DEV_1','point_num':'DEV_1_POINT','limit':50}


def test_registry_current_configuration_cannot_answer_historical_snapshot():
    c=classification('points',fault=None)
    c['time_range']={'mode':'range','start_time':'2026-09-01','end_time':'2026-09-02'}
    args,missing=build_sensor_arguments(SENSOR_TOOL_BY_OPERATION['points'],{'business_intent':c,'selected_entity':entity()}, {})
    assert missing and not args


async def test_parallel_registry_queries_keep_separate_configuration_filters():
    req=request(['E13','E17'],query='E13的温度监测点与E17未监听波形的监测点')
    req.arguments['queries'][0].update(operation='sensor_points',feature_kind='temperature')
    req.arguments['queries'][1].update(operation='sensor_points',waveform_enabled=False)
    async def resolve(**kwargs):
        return {'status':'RESOLVED','entity':{'entity_type':'equipment','equip_no':kwargs['query']}}
    client=SimpleNamespace(resolve_entity=resolve,call_tool=AsyncMock(return_value={'success':True,'status':'OK','records':[]}))
    handler=IndependentQueryHandler(client,SimpleNamespace(),Settings.model_construct())
    result=await handler(req,None)
    assert not result.structured_content['partial']
    args=[call.args[1] for call in client.call_tool.await_args_list]
    assert {'equip_num':'E13','feature_kind':'temperature','limit':50} in args
    assert {'equip_num':'E17','waveform_enabled':False,'limit':50} in args


def test_customer_registry_summary_keeps_real_parameter_codes_and_explains_output_limit():
    payload={'query_type':'MONITORED_SENSOR_POINTS','message':'注册测点总数9086，本次取得1000，匹配50个。',
        'records':[{'point_name':'输入轴测点','monitoring_status_name':'暂停诊断，当前没有新鲜数据',
                    'waveform_enabled':False,'temperature_param_num':'REAL_PARAM_000',
                    'feature_params':[{'feature_kind':'temperature','param_code':'REAL_PARAM_000','param_name':'温度'}]}],
        'warnings':['清单达到上游限制，无法列出全部测点。']}
    text=format_sensor_result(payload)
    assert '未监听波形' in text and '温度' in text and '无法列出全部测点' in text
    state={'observations':[{'tool_result':{'structured_content':payload}}]}
    rendered=render_answer(text+'\ntemperature_param_num=REAL_PARAM_000 monitor_status=SUSPENDED',state)
    assert 'REAL_PARAM_000' in rendered and 'temperature_param_num' not in rendered and 'monitor_status' not in rendered
