import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config import Settings
from app.tools.contracts import ToolCallRequest
from app.tools.independent_queries import IndependentQueryHandler, MULTI_QUERY_TOOL_ID, multi_query_descriptor
from app.orchestration.supervisor.agent import SupervisorAgent


def request(targets, query=None):
    return ToolCallRequest(tool_id=MULTI_QUERY_TOOL_ID, task_id='task', conversation_id='c', branch_id='b', user_token='u',
        arguments={'queries':[{'target':t,'query':t+'的健康度','operation':'health'} for t in targets],
                   '_verified_request_context':{'query':query or '比较'+ '和'.join(targets)+'的健康度',
                       'active_entity':{'equip_no':'OLD'}}})


@pytest.mark.asyncio
async def test_same_tool_different_targets_run_concurrently_and_keep_identity():
    active=0;peak=0
    async def resolve(**kwargs):
        assert kwargs['active_entity'] is None and not kwargs['allow_context_reuse']
        return {'status':'RESOLVED','entity':{'entity_type':'equipment','equip_no':kwargs['query']}}
    async def data(name,args):
        nonlocal active,peak
        active+=1;peak=max(peak,active);await asyncio.sleep(.01);active-=1
        return {'success':True,'data':{'scope_id':args['scope_id'],'score':91 if args['scope_id']=='E13' else 72}}
    handler=IndependentQueryHandler(SimpleNamespace(resolve_entity=resolve),SimpleNamespace(call_tool=data),Settings.model_construct())
    result=await handler(request(['E13','E17']),None)
    assert peak==2 and not result.structured_content['partial']
    rows=result.structured_content['items']
    assert rows[0]['entity']['equip_no']=='E13' and rows[1]['entity']['equip_no']=='E17'
    assert rows[0]['result']['data']['score']==91 and rows[1]['result']['data']['score']==72


@pytest.mark.asyncio
async def test_ambiguous_target_does_not_block_other_results():
    async def resolve(**kwargs):
        return ({'status':'NEEDS_DISAMBIGUATION','needs_disambiguation':True}
                if kwargs['query']=='轧机' else {'status':'RESOLVED','entity':{'equip_no':'E17'}})
    data=AsyncMock(return_value={'success':True,'data':{'score':72}})
    handler=IndependentQueryHandler(SimpleNamespace(resolve_entity=resolve),SimpleNamespace(call_tool=data),Settings.model_construct())
    result=await handler(request(['轧机','E17']),None)
    assert [x['status'] for x in result.structured_content['items']]==['NEEDS_INPUT','SUCCESS']
    assert data.await_count==1


@pytest.mark.asyncio
async def test_invented_targets_never_reach_asset_or_data():
    asset=SimpleNamespace(resolve_entity=AsyncMock());data=SimpleNamespace(call_tool=AsyncMock())
    handler=IndependentQueryHandler(asset,data,Settings.model_construct())
    result=await handler(request(['E13','E17'],query='解释健康分'),None)
    assert all(x['status']=='REJECTED' for x in result.structured_content['items'])
    asset.resolve_entity.assert_not_awaited();data.call_tool.assert_not_awaited()


@pytest.mark.asyncio
async def test_item_time_ranges_are_validated_and_not_shared():
    asset=SimpleNamespace(resolve_entity=AsyncMock(return_value={'status':'RESOLVED','entity':{'equip_no':'E17'}}))
    data=SimpleNamespace(call_tool=AsyncMock(return_value={'success':True,'data':{'score':72}}))
    req=request(['E13','E17'])
    req.arguments['queries'][0]['time_range']={'mode':'range','start_time':'invalid','end_time':'invalid'}
    result=await IndependentQueryHandler(asset,data,Settings.model_construct())(req,None)
    assert [x['status'] for x in result.structured_content['items']]==['FAILED','SUCCESS']
    assert data.call_tool.await_count==1 and 'start_time' not in data.call_tool.call_args.args[1]


@pytest.mark.asyncio
async def test_planner_executes_multi_once_then_synthesizes():
    settings=Settings.model_construct()
    tool=multi_query_descriptor(settings)
    # All $refs in the tool schema resolve from its root, not a nested object.
    assert 'IndependentQuery' in tool.input_schema['$defs']
    supervisor=SupervisorAgent(model_client=None,model_registry=None,settings=settings)
    state={'business_intent':{'independent_queries':request(['E13','E17']).arguments['queries']}}
    verdict=await supervisor.next_react_action(state,[],[tool])
    assert verdict.next_call.tool_id==MULTI_QUERY_TOOL_ID
    state['observations']=[{'tool_id':MULTI_QUERY_TOOL_ID,'status':'SUCCESS'}]
    assert (await supervisor.next_react_action(state,[],[tool])).answerable
