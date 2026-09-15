import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest


def test_customer_guard_preserves_verified_business_codes():
    from app.output.customer import render_answer
    result=render_answer('equip_no 为 PLANT_A_001，newInternalField 已查询。',
                         {'resolved_entity':{'equip_no':'PLANT_A_001'}}, suggestions=False)
    assert 'PLANT_A_001' in result and '设备编号' in result
    assert 'equip_no' not in result and 'newInternalField' not in result

from app.config import Settings
from app.tools.contracts import ToolCallRequest
from app.tools.dify_knowledge import DifyKnowledgeHandler, KNOWLEDGE_TOOL_ID, KNOWLEDGE_NAMES
from app.output.customer import render_answer, citation_context, customer_text


@pytest.fixture
def settings():
    return Settings.model_construct(dify_knowledge_api_key='test-only',dify_knowledge_base_url='http://dify.test/v1')


def request(**args):
    return ToolCallRequest(tool_id=KNOWLEDGE_TOOL_ID,task_id='task',conversation_id='conv',branch_id='branch',
                           user_token='A',arguments={'query':'轴承如何维护',**args})


def record(name='维护手册.pdf',segment='seg',position=3,content='检查润滑状态，并按维护规程检查轴承。'):
    return {'score':.9,'segment':{'id':segment,'position':position,'content':content,'enabled':True,
        'status':'completed','document':{'id':'doc','name':name}}}


@pytest.mark.asyncio
async def test_catalog_pagination_and_three_retrievals_really_overlap(settings):
    calls=[];active=0;peak=0
    async def handle(req):
        nonlocal active,peak
        calls.append(req)
        assert req.headers['Authorization']=='Bearer test-only'
        if req.method=='GET':
            page=int(req.url.params['page'])
            return httpx.Response(200,json={'data':[{'id':str(page),'name':KNOWLEDGE_NAMES[page-1]}],'has_more':page<3})
        active+=1;peak=max(peak,active)
        await asyncio.sleep(.02)
        active-=1
        assert json.loads(req.content)=={'query':'轴承如何维护'}
        return httpx.Response(200,json={'records':[record(segment=str(req.url))]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        handler=DifyKnowledgeHandler(client,settings)
        result=await handler(request(),None)
        assert result.status.value=='SUCCESS' and len(result.structured_content['sources'])==3
        assert peak==3
        await handler(request(),None)
        assert sum(x.method=='GET' for x in calls)==3
        assert sum(x.method=='POST' for x in calls)==6 # No cached answer shared across users/turns.


@pytest.mark.asyncio
async def test_duplicate_names_never_choose_arbitrarily(settings):
    posts=[]
    async def handle(req):
        if req.method=='GET':return httpx.Response(200,json={'data':[{'id':'A','name':'企业智库'},{'id':'B','name':'企业智库'}]})
        posts.append(req);return httpx.Response(200,json={'records':[]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        result=await DifyKnowledgeHandler(client,settings)(request(knowledge_bases=['企业智库']),None)
    assert result.status.value=='FAILED' and not posts
    assert '同名' in result.structured_content['warnings'][0]


@pytest.mark.asyncio
async def test_partial_failure_preserves_real_sources(settings):
    async def handle(req):
        if req.method=='GET':return httpx.Response(200,json={'data':[{'id':str(i),'name':n} for i,n in enumerate(KNOWLEDGE_NAMES)]})
        if '/0/' in str(req.url):return httpx.Response(503,text='secret backend stack')
        return httpx.Response(200,json={'records':[record()]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        result=await DifyKnowledgeHandler(client,settings)(request(),None)
    assert result.status.value=='SUCCESS' and result.structured_content['partial']
    assert len(result.structured_content['sources'])==2
    assert 'secret' not in result.model_dump_json()


@pytest.mark.asyncio
async def test_total_timeout_is_bounded(settings):
    settings.dify_knowledge_timeout_seconds=.01
    async def handle(req):await asyncio.sleep(.1);return httpx.Response(200,json={})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        result=await DifyKnowledgeHandler(client,settings)(request(),None)
    assert result.status.value=='FAILED' and not result.structured_content.get('sources')


@pytest.mark.parametrize('args',[{'knowledge_bases':['其他库']},{'query':'a'*251},{'query':''}])
@pytest.mark.asyncio
async def test_out_of_scope_or_invalid_queries_do_not_reach_server(settings,args):
    def handle(req):raise AssertionError('No HTTP expected')
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        assert (await DifyKnowledgeHandler(client,settings)(request(**args),None)).status.value=='REJECTED'


@pytest.mark.asyncio
async def test_disabled_and_incomplete_sources_are_not_citable(settings):
    disabled=record();disabled['segment']['enabled']=False
    unnamed=record();unnamed['segment']['document']={}
    async def handle(req):
        if req.method=='GET':return httpx.Response(200,json={'data':[{'id':'A','name':'企业智库'}]})
        return httpx.Response(200,json={'records':[disabled,unnamed]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        result=await DifyKnowledgeHandler(client,settings)(request(knowledge_bases=['企业智库']),None)
    assert result.status.value=='SUCCESS' and result.structured_content['sources']==[]


def evidence_state():
    source={'dataset_id':'D','document_id':'DOC','segment_id':'S','knowledge_base':'企业智库',
            'document_name':'维护手册.pdf','position':3,'content':'定期检查润滑状态。'}
    return {'observations':[{'tool_id':KNOWLEDGE_TOOL_ID,'status':'SUCCESS',
                            'tool_result':{'structured_content':{'sources':[source]}}}]}


def test_server_owned_bibliography_and_invalid_reference_removal():
    state=evidence_state()
    text=render_answer('应检查润滑状态[1]。未知结论[99]。\n\n参考文献\n[99] 假文档',state,suggestions=False)
    assert '维护手册.pdf' in text and '第 3 段' in text and '定期检查润滑状态' in text
    assert '[99]' not in text and '假文档' not in text
    assert len(citation_context(state))==1


def test_uncited_docs_do_not_appear_as_used():
    assert '维护手册' not in render_answer('这部分是通用原理。',evidence_state(),suggestions=False)


def test_fields_and_keys_are_customer_language():
    answer=customer_text('equip_no=E1，thresholdScore=98，point_id=P1；HTTP http://10.10.1.2/v1；dataset-abcdefghijklmnop；foo_unknown_field')
    assert '设备编号' in answer and '阈值健康分' in answer
    assert 'equip_no' not in answer and 'thresholdScore' not in answer and 'abcdefghijklmnop' not in answer
    assert '10.10.1.2' not in answer and 'foo_unknown_field' not in answer


def test_no_automatic_followups_even_with_enabled_capabilities():
    state={'resolved_entity':{'equip_no':'E'},'available_tool_ids':[], 'observations':[]}
    assert render_answer('查询已完成。',state)=='查询已完成。'
    state['available_tool_ids']=[KNOWLEDGE_TOOL_ID]
    assert render_answer('查询已完成。',state,suggestions=True)=='查询已完成。'
    state['observations']=[{'tool_id':KNOWLEDGE_TOOL_ID,'status':'FAILED'}]
    assert render_answer('查询已完成。',state)=='查询已完成。'
