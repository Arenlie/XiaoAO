import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest

from app.config import Settings
from app.performance import detail_span
from app.output.streaming import AnswerStreamSession, AnswerStreamStopped, CustomerTextStream
from app.integrations.openai.chat_client import OpenAICompatibleChatClient, ModelStreamDelta
from app.reasoning.model_registry import ModelRegistry
from app.content.raw_text import search_text, read_text_lines, numeric_summary
from app.orchestration.runtime import graph_runtime_scope
from test_v11_orchestration import make_runtime
from test_v11_knowledge import evidence_state


@pytest.mark.asyncio
async def test_cancel_during_committed_start_closes_same_span_once():
    rt=make_runtime(); rt.agent_runtime.settings.performance_monitor_enabled=True
    seen=[]; started=asyncio.Event()
    async def publish(task,event,payload,**kw):
        seen.append((event,payload,kw))
        if event=='performance.span.started':
            started.set();await asyncio.Event().wait()
    rt.agent_runtime.event_service.publish=publish
    async def run():
        with graph_runtime_scope(rt):
            async with detail_span({},code='supervisor.entity_intake.llm',name='快速识别查询对象',description='',category='llm'):
                raise AssertionError('Work must not begin after cancellation')
    task=asyncio.create_task(run());await asyncio.wait_for(started.wait(),1);task.cancel()
    with pytest.raises(asyncio.CancelledError):await task
    assert [x[0] for x in seen]==['performance.span.started','performance.span.completed']
    assert seen[0][1]['span_id']==seen[1][1]['span_id']
    assert seen[1][2]['status']=='CANCELLED' and seen[1][1]['affects_task'] is False


@pytest.mark.asyncio
async def test_first_reasoning_and_final_arrive_before_provider_finishes():
    rt=make_runtime();seen=[];first_reason=asyncio.Event();first_answer=asyncio.Event();finish=asyncio.Event()
    async def publish(task,event,data,**kw):
        seen.append((event,data,kw))
        if event=='answer.reasoning.delta':first_reason.set()
        if event=='answer.delta':first_answer.set()
    rt.agent_runtime.event_service.publish=publish
    async def source():
        yield ModelStreamDelta('reasoning','依据所见趋势进行比较。','provider_output')
        await first_reason.wait()
        yield ModelStreamDelta('final','先检查润滑状态[1]。')
        await finish.wait()
        yield ModelStreamDelta('final','保留采样时间。')
    session=AnswerStreamSession(evidence_state(),rt,span_id='model-span')
    task=asyncio.create_task(session.run(source()))
    await asyncio.wait_for(first_answer.wait(),.5)
    assert first_reason.is_set() and not task.done()
    finish.set();answer=await task
    assert '维护手册.pdf' in answer and len(session.meta['citations'])==1
    assert all(x[1]['generation_id']==session.generation_id for x in seen)
    assert seen[0][0]=='answer.started' and seen[-1][0]=='answer.reasoning.completed'
    assert ''.join(d['content'] for e,d,k in seen if e=='answer.delta')==answer


@pytest.mark.asyncio
@pytest.mark.parametrize('reasoning_enabled',[True,False])
async def test_no_reasoning_is_optional_not_an_unfinished_node(reasoning_enabled):
    rt=make_runtime();rt.agent_runtime.settings.stream_reasoning_enabled=reasoning_enabled
    async def source():yield ModelStreamDelta('final','正常回答。')
    session=AnswerStreamSession({},rt);assert await session.run(source())=='正常回答。'
    rows=rt.agent_runtime.event_service.publish.await_args_list
    assert not any(r.args[1]=='answer.reasoning.delta' for r in rows)
    assert rows[-1].args[2]['available'] is False and rows[-1].kwargs['status']=='SKIPPED'


@pytest.mark.asyncio
@pytest.mark.parametrize('stop',[True,False])
async def test_partial_stream_cancellation_or_failure_does_not_restart(stop):
    rt=make_runtime();cancelled=False;closed=asyncio.Event()
    async def is_cancelled():return cancelled
    rt.agent_runtime.is_cancelled=is_cancelled
    async def source():
        nonlocal cancelled
        try:
            yield ModelStreamDelta('final','已读到部分数据。')
            await asyncio.sleep(.01)
            if stop:
                cancelled=True
                await asyncio.Event().wait()
            raise RuntimeError('upstream interrupted')
        finally:closed.set()
    session=AnswerStreamSession({},rt)
    with pytest.raises(AnswerStreamStopped if stop else RuntimeError):
        await asyncio.wait_for(session.run(source()),1)
    assert closed.is_set() and session.meta['content']=='已读到部分数据。'
    types=[r.args[1] for r in rt.agent_runtime.event_service.publish.await_args_list]
    assert types.count('answer.started')==1
    assert ('answer.cancelled' if stop else 'answer.failed') in types


@pytest.mark.parametrize('size',[1,2,3,7,20,2000])
def test_cross_chunk_fields_credentials_citations_and_reference_filter(size):
    state={**evidence_state(),'resolved_entity':{'equip_no':'PLANT_A_001'}}
    raw='equip_no 为 PLANT_A_001，thresholdScore 为 98。检查润滑[1]。未知引用[99]。\n\n参考文献\n[99] 假文献'
    renderer=CustomerTextStream(state)
    text=''.join(renderer.feed(raw[i:i+size]) for i in range(0,len(raw),size))+renderer.finish()+renderer.bibliography()
    assert 'equip_no' not in text and 'thresholdScore' not in text and 'PLANT_A_001' in text
    assert '[99]' not in text and '假文献' not in text and '维护手册.pdf' in text
    renderer=CustomerTextStream({});raw='内部地址 http://10.10.1.2/v1；dataset-abcdefghijklmnop；foo_unknown_field。'
    text=''.join(renderer.feed(raw[i:i+size]) for i in range(0,len(raw),size))+renderer.finish()
    assert '10.10.1.2' not in text and 'abcdefghijklmnop' not in text and 'foo_unknown_field' not in text


@pytest.mark.asyncio
@pytest.mark.parametrize('model,policy,expected',[('qwen3.8-flash','auto',True),('other-model','auto',False),('qwen3.8-flash','omit',False),('gateway-alias','enable_thinking',True)])
async def test_provider_native_reasoning_and_non_reasoning_parameter_compatibility(model,policy,expected):
    requests=[]
    def handle(request):
        requests.append(json.loads(request.content))
        frames=[{'choices':[{'delta':{'reasoning_content':'先看趋势。'}}]}, {'choices':[{'delta':{'content':'初步结论。'}}]}]
        return httpx.Response(200,text=''.join('data: '+json.dumps(x,ensure_ascii=False)+'\n\n' for x in frames)+'data: [DONE]\n\n')
    settings=Settings.model_construct(supervisor_model=model,supervisor_model_base_url='http://mock/v1',supervisor_model_api_key='test',model_reasoning_parameters=policy)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        client=OpenAICompatibleChatClient(http,settings)
        rows=[x async for x in client.stream_profile_events(profile=ModelRegistry(settings).get('supervisor'),system='test',user='test')]
    assert [r.channel for r in rows]==['reasoning','final']
    assert ('enable_thinking' in requests[0]) is expected
    assert requests[0]['stream'] is True


def test_responses_summary_and_inline_tag_protocols():
    client=OpenAICompatibleChatClient(None,Settings.model_construct())
    r=client._extract_stream_events({'type':'response.reasoning_summary_text.delta','delta':'摘要'})
    assert r[0].reasoning_kind=='summary' and r[0].channel=='reasoning'
    from app.integrations.dify.think_filter import ThinkTagStreamSplitter
    splitter=ThinkTagStreamSplitter(eager_final_after_reasoning=True)
    rows=[]
    for c in '<think>分析</think>正式回答':rows.extend(splitter.feed(c))
    rows+=splitter.flush()
    assert ''.join(r.content for r in rows if r.channel=='reasoning')=='分析'
    assert ''.join(r.content for r in rows if r.channel=='final')=='正式回答'


@pytest.mark.asyncio
async def test_tool_planning_does_not_force_vendor_reasoning_fields():
    requests=[]
    def handle(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200,json={'choices':[{'message':{'content':'ready','tool_calls':[]}}]})
    settings=Settings.model_construct(supervisor_model='plain-model',supervisor_model_base_url='http://mock/v1',supervisor_model_api_key='test')
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        client=OpenAICompatibleChatClient(http,settings)
        await client.plan_tool_calls(profile=ModelRegistry(settings).get('supervisor'),system='test',user='test',tools=[])
    assert len(requests)==1 and 'enable_thinking' not in requests[0]


def test_full_source_search_and_tail_after_old_200k_preview():
    data=('普通记录\n'*50000+'目标设备XYZ123的偏置电压异常\n最后一行').encode()
    result=search_text(data,['xyz123'],3000)
    assert result['source_scanned_complete'] and result['context_limited']
    assert any('XYZ123' in r['content'] for r in result['matches'])
    assert max(r['locator']['line_start'] for r in result['matches'])>49000
    tail=read_text_lines(data,tail=True,max_lines=2)
    assert 'XYZ123' in tail['content'] and tail['line_end']==50002


def test_numeric_summary_uses_all_rows_and_explicit_column_without_inventing_units():
    data=('time,amplitude\n'+'\n'.join(f'{i},{1 if i%2 else -1}' for i in range(12001))+'\n12001,NaN\n12002,error').encode()
    result=numeric_summary(data,value_column=2,delimiter=',',header_lines=1)
    assert result['sample_count']==12001 and result['skipped_non_numeric_rows']==2
    assert result['rms']==1 and result['peak_to_peak']==2 and result['unit'] is None
    assert result['sample_rate_hz'] is None and result['duration_seconds'] is None


def test_citation_keeps_real_filename_instead_of_translating_it_as_a_field():
    state=evidence_state()
    state['observations'][0]['tool_result']['structured_content']['sources'][0]['document_name']='sensor_manual_v2.pdf'
    stream=CustomerTextStream(state)
    stream.feed('检查润滑[1]。');stream.finish()
    assert stream.citations()[0]['document_name']=='sensor_manual_v2.pdf'
    assert 'sensor_manual_v2.pdf' in stream.bibliography()
