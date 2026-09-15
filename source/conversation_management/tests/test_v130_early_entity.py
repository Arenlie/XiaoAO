import asyncio
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from app.config import Settings
from app.integrations.openai.chat_client import OpenAICompatibleChatClient
from app.orchestration.early_entity import EntityIntake, eligible
from app.orchestration.runtime import graph_runtime_scope
from app.orchestration.supervisor.agent import SupervisorAgent
from app.reasoning.model_registry import ModelRegistry
from app.workflows.registry import BusinessWorkflowRegistry
from test_v111_lookup_before_answer import setup, classification, ENTITY, QUERY, result as asset_result
from test_v11_orchestration import make_runtime as legacy_runtime


def make_runtime():
    runtime = legacy_runtime()
    runtime.agent_runtime.settings.supervisor_early_entity_enabled = True
    return runtime


def intake(query=QUERY):
    c = classification()
    c['asset_semantics']['needs_asset_lookup'] = True
    return EntityIntake(required_entity_level='equipment', confidence=.99,
                        asset_semantics=c['asset_semantics'])


@pytest.mark.asyncio
async def test_real_candidates_pause_before_slow_route_and_cancel_it_then_resume_without_lookup():
    nodes, asset, _ = setup(status='MULTIPLE')
    route_started, cancelled = asyncio.Event(), asyncio.Event()
    async def slow_route(*args):
        route_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
    async def fast_identity(state):
        await route_started.wait()
        return intake()
    nodes.supervisor.classify_business_workflow = slow_route
    nodes.supervisor.understand_entity_target = fast_identity
    runtime = make_runtime()
    runtime.agent_runtime.settings.performance_monitor_enabled = True
    state = {'task_id': runtime.agent_runtime.task_id, 'query': QUERY, 'observations': [], 'root_span_id': 'ROOT'}
    with graph_runtime_scope(runtime):
        update = await asyncio.wait_for(nodes.resolve_entity_context(state), .5)
    assert cancelled.is_set()
    assert nodes.route_after_entity_resolution(update) == 'selection'
    assert update['business_intent']['routing_pending'] is True
    assert asset.lookup.await_count == 1
    candidates = update['entity_result']['matches']
    chosen = candidates[-1]
    classifier = AsyncMock(return_value=(classification(), None))
    nodes.supervisor.classify_business_workflow = classifier
    with graph_runtime_scope(runtime):
        resumed = await nodes.resolve_entity_context({**state, **update, 'selection_resume': True, 'selected_entity': chosen})
    assert resumed['entity_result']['status'] == 'UNIQUE'
    assert resumed['resolved_entity']['equip_no'] == chosen['equip_no']
    assert asset.lookup.await_count == 1
    assert not resumed['business_intent'].get('routing_pending')
    assert classifier.call_args.args[0]['selected_entity']['equip_no'] == chosen['equip_no']
    events = [c.args[2] | {'event': c.args[1], 'status': c.kwargs.get('status')} for c in runtime.agent_runtime.event_service.publish.call_args_list if c.args[1].startswith('performance.span.')]
    parent = next(e for e in events if e['code']=='entity.resolve' and e['event']=='performance.span.started')
    route = next(e for e in events if e['code']=='supervisor.classify.llm' and e['event']=='performance.span.started')
    assert route['parent_span_id'] == parent['span_id']
    assert any(e['code']=='supervisor.classify.llm' and e['status']=='CANCELLED' for e in events)


@pytest.mark.asyncio
async def test_matching_inflight_prefetch_is_reused_when_route_finishes_first():
    nodes, asset, _ = setup()
    lookup_started, release = asyncio.Event(), asyncio.Event()
    raw = asset_result()
    async def lookup(**kwargs):
        lookup_started.set()
        await release.wait()
        return raw
    asset.lookup.side_effect = lookup
    async def route(*args):
        await lookup_started.wait()
        release.set()
        return classification(), None
    nodes.supervisor.classify_business_workflow = route
    nodes.supervisor.understand_entity_target = AsyncMock(return_value=intake())
    runtime = make_runtime()
    with graph_runtime_scope(runtime):
        update = await asyncio.wait_for(nodes.resolve_entity_context({'task_id':runtime.agent_runtime.task_id,'query':QUERY}), .5)
    assert update['entity_result']['status']=='UNIQUE'
    assert asset.lookup.await_count==1
    assert not update['business_intent'].get('routing_pending')


@pytest.mark.asyncio
async def test_optional_intake_failure_retains_original_lookup():
    nodes, asset, _ = setup()
    nodes.supervisor.understand_entity_target = AsyncMock(side_effect=ValueError('bad fast result'))
    runtime = make_runtime()
    with graph_runtime_scope(runtime):
        update=await nodes.resolve_entity_context({'task_id':runtime.agent_runtime.task_id,'query':QUERY})
    assert update['entity_result']['status']=='UNIQUE' and asset.lookup.await_count==1


@pytest.mark.asyncio
async def test_disagreeing_route_cancels_prefetch_and_uses_current_scope():
    nodes, asset, _ = setup()
    started, cancelled = asyncio.Event(), asyncio.Event()
    raw = asset_result()
    calls = []
    async def lookup(**kwargs):
        calls.append(kwargs)
        if len(calls)==1:
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        return raw
    asset.lookup.side_effect=lookup
    decision=copy.deepcopy(classification())
    decision['asset_semantics']['refresh_requested']=True
    async def route(*args):
        await started.wait()
        return decision,None
    nodes.supervisor.classify_business_workflow=route
    nodes.supervisor.understand_entity_target=AsyncMock(return_value=intake())
    runtime=make_runtime()
    with graph_runtime_scope(runtime):
        update=await asyncio.wait_for(nodes.resolve_entity_context({'task_id':runtime.agent_runtime.task_id,'query':QUERY}),.5)
    assert cancelled.is_set() and len(calls)==2
    assert calls[-1]['semantic_hints']['refresh_requested'] is True
    assert update['entity_result']['status']=='UNIQUE'


@pytest.mark.parametrize('change',[
    {'multiple_targets':True}, {'confidence':.7}, {'required_entity_level':'point'},
    {'required_entity_level':'none'},
])
def test_early_choices_exclude_unsupported_or_uncertain_scopes(change):
    assert not eligible({'query':QUERY}, intake().model_copy(update=change))


@pytest.mark.asyncio
async def test_attachment_keeps_original_authoritative_resolution_path():
    nodes, asset, _=setup()
    nodes.supervisor.understand_entity_target=AsyncMock(return_value=intake())
    runtime=make_runtime()
    with graph_runtime_scope(runtime):
        await nodes.resolve_entity_context({'task_id':runtime.agent_runtime.task_id,'query':QUERY,'attachments':[{'id':'A'}]})
    nodes.supervisor.understand_entity_target.assert_not_awaited()
    assert asset.lookup.await_count==1


@pytest.mark.asyncio
async def test_routing_http_disables_thinking_without_changing_planning_profile():
    captured=[]
    def handle(request):
        body=json.loads(request.content);captured.append(body)
        return httpx.Response(200,json={'choices':[{'message':{'content':json.dumps(classification())}}]})
    settings=Settings.model_construct(supervisor_model_base_url='http://model.test/v1',supervisor_model_api_key='test',supervisor_model='qwen-test')
    registry=ModelRegistry(settings)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        supervisor=SupervisorAgent(model_client=OpenAICompatibleChatClient(http,settings),model_registry=registry,settings=settings)
        decision,_=await supervisor.classify_business_workflow({'query':QUERY},BusinessWorkflowRegistry())
    assert captured[0]['enable_thinking'] is False
    assert registry.get('supervisor').supports_reasoning is True
    assert decision['classification_attempts']==1
    context=json.loads(captured[0]['messages'][1]['content'])
    assert context['query']==QUERY


@pytest.mark.asyncio
async def test_classification_retries_share_one_total_budget():
    model=SimpleNamespace(complete_json_profile=AsyncMock(side_effect=lambda **kw: None))
    async def slow(**kwargs):
        await asyncio.Event().wait()
    model.complete_json_profile.side_effect=slow
    settings=Settings.model_construct(supervisor_classification_timeout_seconds=.02)
    supervisor=SupervisorAgent(model_client=model,model_registry=SimpleNamespace(get=lambda x:None),settings=settings)
    decision,_=await asyncio.wait_for(supervisor.classify_business_workflow({'query':QUERY},BusinessWorkflowRegistry()),.5)
    assert decision['routing_status']=='unmatched'
    assert model.complete_json_profile.await_count==1


def test_context_stays_valid_json_and_retains_current_question():
    from app.orchestration.classification_context import encode_context
    query='当前问题'+('设备A，'*10000)
    encoded=encode_context({'query':query,'recent_messages':[{'role':'assistant','content':'x'*10000}],
                            'attachment_evidence':[{'extracted_text':'a'*20000}]})
    decoded=json.loads(encoded)
    assert decoded['query']==query
    assert len(decoded['recent_messages'][0]['content'])<2000
    assert '省略' in decoded['attachment_evidence'][0]['extracted_text']


@pytest.mark.asyncio
@pytest.mark.parametrize('same_user', [True, False])
async def test_shared_nodes_early_choices_remain_task_local(same_user):
    from app.orchestration.runtime import current_graph_runtime
    from app.orchestration.graphs.normal_graph import build_normal_graph
    nodes, asset, _ = setup(status='MULTIPLE')
    cancelled = set()
    async def route(state, *args):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.add(state['task_id'])
    async def lookup(**kwargs):
        task_id = current_graph_runtime().agent_runtime.task_id
        assert kwargs['request_id'] == task_id
        await asyncio.sleep(.001)
        result = asset_result('MULTIPLE').model_copy(deep=True)
        for row in result.matches:
            row['equip_no'] = task_id + ':' + row['equip_no']
        return result
    nodes.supervisor.classify_business_workflow = route
    nodes.supervisor.understand_entity_target = AsyncMock(return_value=intake())
    asset.lookup.side_effect = lookup
    async def run(user):
        runtime = legacy_runtime(user)
        runtime.agent_runtime.settings.supervisor_early_entity_enabled = True
        task_id = runtime.agent_runtime.task_id
        with graph_runtime_scope(runtime):
            result = await build_normal_graph(nodes).ainvoke({'task_id':task_id,'query':QUERY,
                'conversation_id':task_id,'branch_id':task_id,'execution_mode':'normal','observations':[]})
        assert result['final_status']=='WAITING_SELECTION'
        assert result['business_intent']['routing_pending'] is True
        assert all(row['equip_no'].startswith(task_id + ':') for row in result['entity_result']['matches'])
        return task_id
    ids = await asyncio.wait_for(asyncio.gather(run('A'), run('A' if same_user else 'B')), 1)
    assert len(set(ids)) == 2 and set(ids) == cancelled


@pytest.mark.asyncio
async def test_stopping_query_cancels_both_route_and_asset_work():
    nodes, asset, _ = setup()
    lookup_started = asyncio.Event()
    stopped = set()
    async def route(*args):
        try:
            await asyncio.Event().wait()
        finally:
            stopped.add('route')
    async def lookup(**kwargs):
        lookup_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.add('asset')
    nodes.supervisor.classify_business_workflow = route
    nodes.supervisor.understand_entity_target = AsyncMock(return_value=intake())
    asset.lookup.side_effect = lookup
    runtime = make_runtime()
    with graph_runtime_scope(runtime):
        task = asyncio.create_task(nodes.resolve_entity_context({'task_id':runtime.agent_runtime.task_id,'query':QUERY}))
        await asyncio.wait_for(lookup_started.wait(), .5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert stopped == {'route', 'asset'}
