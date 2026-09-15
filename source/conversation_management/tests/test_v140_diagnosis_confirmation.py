import asyncio
import copy
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import fakeredis.aioredis
import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy import create_engine, BigInteger
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.config import Settings
from app.api.dependencies import get_container
from app.api.v1.tasks import router
from app.domain.exceptions import AppError, ConflictError
from app.models.conversation import Conversation
from app.models.branch import ConversationBranch
from app.models.message import Message
from app.models.generation_task import GenerationTask
from app.models.entity_selection import PendingEntitySelection
from app.models.execution_event import TaskExecutionEvent
from app.repositories.task_repository import TaskRepository
from app.repositories.conversation_repository import ConversationRepository
from app.repositories.message_repository import MessageRepository
from app.repositories.event_repository import EventRepository
from app.services.task_service import TaskService
from app.services.event_service import EventService
from app.services.generation_service import GenerationService
from app.services.concurrency_service import ConcurrencyService
from app.orchestration.diagnosis_choice import confirmation_update, apply_depth, depth, EXPENSIVE_TOOLS
from app.orchestration.runtime import graph_runtime_scope
from test_incident_1e617332 import make_nodes, make_runtime, initial, run, point, FIXTURE


@compiles(JSONB,'sqlite')
def compile_json(element,compiler,**kw):return 'JSON'

@compiles(BigInteger,'sqlite')
def compile_integer(element,compiler,**kw):return 'INTEGER'


@pytest.fixture
def db():
    engine=create_engine('sqlite://',poolclass=StaticPool)
    for model in (Conversation,ConversationBranch,Message,GenerationTask,PendingEntitySelection,TaskExecutionEvent):
        model.__table__.create(engine)
    class Bridge:
        async def __aenter__(self):self.session=Session(engine,expire_on_commit=False);return self
        async def __aexit__(self,*args):self.session.close()
        @asynccontextmanager
        async def begin(self):
            with self.session.begin():yield self
        async def scalar(self,s):return self.session.scalar(s)
        async def scalars(self,s):return self.session.scalars(s)
        async def execute(self,s):return self.session.execute(s)
        async def get(self,*args,**kw):return self.session.get(*args,**kw)
        async def flush(self,objects=None):self.session.flush(objects)
        def add(self,o):self.session.add(o)
    redis=fakeredis.aioredis.FakeRedis(decode_responses=True)
    settings=Settings.model_construct(outbox_enabled=False,title_mode='rule')
    events=EventService(session_factory=Bridge,redis=redis,settings=settings,repository=EventRepository())
    concurrency=ConcurrencyService(redis,settings)
    queue=SimpleNamespace(enqueue_generation=AsyncMock(),enqueue_summary=AsyncMock(),enqueue_title=AsyncMock())
    service=TaskService(Bridge,redis,settings,TaskRepository(),events,queue,concurrency,None,None)
    def seed(owner='A',expired=False,status='WAITING_CONFIRMATION',changed_leaf=False):
        cid,bid,tid,mid=uuid4(),uuid4(),uuid4(),uuid4()
        choice=confirmation_update({'query':'诊断一下设备A','resolved_entity':{'equip_no':'REAL-A'}},-1 if expired else 900)['diagnosis_confirmation']
        resume={'query':'诊断一下设备A','resolved_entity':{'equip_no':'REAL-A'},'business_intent':{'goal_frame':{'diagnosis_requested':True}},'observations':[]}
        with Session(engine) as s:
            s.add(Conversation(id=cid,user_token=owner,active_branch_id=bid,first_user_message_id=uuid4(),expires_at=datetime.now(UTC)+timedelta(days=1)))
            s.add(ConversationBranch(id=bid,conversation_id=cid,active_leaf_message_id=uuid4() if changed_leaf else mid))
            s.add(Message(id=mid,conversation_id=cid,branch_id=bid,role='ASSISTANT',metadata_json={'diagnosis_confirmation':choice,'diagnosis_resume_state':resume}))
            s.add(GenerationTask(id=tid,conversation_id=cid,branch_id=bid,user_message_id=uuid4(),assistant_message_id=mid,status=status))
            s.commit()
        return SimpleNamespace(cid=cid,bid=bid,tid=tid,mid=mid,choice=choice)
    app=FastAPI();app.include_router(router,prefix='/chat/v1')
    app.dependency_overrides[get_container]=lambda:SimpleNamespace(task_service=service,events=events,settings=settings)
    @app.middleware('http')
    async def request_id(request,next):request.state.request_id='test';return await next(request)
    @app.exception_handler(AppError)
    async def error(request,exc):return JSONResponse({'success':False,'code':exc.code},status_code=exc.status_code)
    yield SimpleNamespace(engine=engine,Bridge=Bridge,redis=redis,service=service,seed=seed,events=events,
        concurrency=concurrency,queue=queue,settings=settings,app=app)
    engine.dispose()


async def store_context(db,row):
    await db.redis.setex(f'chat:task_context:{row.tid}',1800,json.dumps({'user_token':'A','query':'诊断一下设备A','graph_attempt':0,'data_access_token':'test-only'}))


@pytest.mark.asyncio
@pytest.mark.parametrize('mode',['quick','detailed','cancel'])
async def test_choice_persists_exact_target_and_duplicate_click_enqueues_once(db,mode):
    row=db.seed();await store_context(db,row)
    task=await db.service.select_diagnosis_mode(row.tid,'A',row.choice['confirmation_id'],mode)
    assert task.status==('STOPPED' if mode=='cancel' else 'QUEUED')
    await db.service.select_diagnosis_mode(row.tid,'A',row.choice['confirmation_id'],mode)
    assert db.queue.enqueue_generation.await_count==(0 if mode=='cancel' else 1)
    context=json.loads(await db.redis.get(f'chat:task_context:{row.tid}'))
    if mode!='cancel':
        assert context['diagnosis_choice']['mode']==mode
        assert context['diagnosis_resume_state']['resolved_entity']=={'equip_no':'REAL-A'}
        assert context['graph_attempt']==1
    assert await db.service.get_diagnosis_confirmation(row.tid,'A') is None


@pytest.mark.asyncio
@pytest.mark.parametrize('params',[{'expired':True},{'changed_leaf':True},{'status':'STOPPED'}])
async def test_expired_superseded_or_old_branch_choice_never_starts(db,params):
    row=db.seed(**params);await store_context(db,row)
    with pytest.raises(ConflictError):await db.service.select_diagnosis_mode(row.tid,'A',row.choice['confirmation_id'],'detailed')
    assert not db.queue.enqueue_generation.await_count
    assert await db.redis.get(f'chat:lock:conversation:{row.cid}') is None


@pytest.mark.asyncio
async def test_idle_wait_has_no_lease_and_expiry_is_not_auto_detailed(db):
    row=db.seed(expired=True)
    assert await db.service.get_diagnosis_confirmation(row.tid,'A') is None
    with Session(db.engine) as s:assert s.get(GenerationTask,row.tid).status=='STOPPED'
    assert not db.queue.enqueue_generation.await_count
    assert await db.redis.get(f'chat:lock:conversation:{row.cid}') is None


@pytest.mark.asyncio
async def test_http_ownership_empty_200_validation_and_paged_reasoning_optin(db):
    row=db.seed();await store_context(db,row)
    await db.events.publish(row.tid,'answer.reasoning.delta',{'channel':'reasoning','content':'分析摘要'})
    await db.events.publish(row.tid,'answer.delta',{'channel':'final','content':'回答'})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=db.app),base_url='http://test') as c:
        url=f'/chat/v1/tasks/{row.tid}'
        assert (await c.get(url+'/diagnosis-mode',params={'X-User-Token':'B'})).status_code==404
        assert (await c.get(url+'/diagnosis-mode')).status_code==401
        result=await c.get(url+'/diagnosis-mode',params={'X-User-Token':'A'})
        assert result.status_code==200 and 'resume_state' not in str(result.json())
        result=await c.get(url+'/execution-events',params={'X-User-Token':'A','limit':1})
        page=result.json()['data'];assert page['events']==[] and page['has_more'] and page['next_after_sequence']==1
        result=await c.get(url+'/execution-events',params={'X-User-Token':'A','include_reasoning':'true','limit':1})
        assert result.json()['data']['events'][0]['event_type']=='answer.reasoning.delta'
        invalid=await c.post(url+'/diagnosis-mode',params={'X-User-Token':'A'},json={'confirmation_id':row.choice['confirmation_id'],'mode':'automatic'})
        assert invalid.status_code==422
        await db.service.select_diagnosis_mode(row.tid,'A',row.choice['confirmation_id'],'cancel')
        empty=await c.get(url+'/diagnosis-mode',params={'X-User-Token':'A'})
        assert empty.status_code==200 and empty.json()['data'] is None


@pytest.mark.asyncio
async def test_missing_redis_context_rolls_back_choice(db):
    row=db.seed()
    with pytest.raises(ConflictError):await db.service.select_diagnosis_mode(row.tid,'A',row.choice['confirmation_id'],'detailed')
    with Session(db.engine) as s:
        assert s.get(GenerationTask,row.tid).status=='WAITING_CONFIRMATION'
        assert s.get(Message,row.mid).metadata_json['diagnosis_confirmation']['status']=='PENDING'


@pytest.mark.asyncio
async def test_new_question_supersedes_only_its_own_waiting_turn(db):
    row=db.seed();other=db.seed();await store_context(db,row)
    service=GenerationService(session_factory=db.Bridge,redis=db.redis,settings=db.settings,
        conversation_repo=ConversationRepository(),message_repo=MessageRepository(),events=db.events,
        queue=db.queue,concurrency=db.concurrency,outbox=None,
        attachment_service=SimpleNamespace(validate_for_message=AsyncMock(return_value=[]),bind_rows=AsyncMock()))
    accepted=await service._send_impl(user_token='A',data_access_token='test-only',app_code='phm',content='设备B在线吗？',
        conversation_id=row.cid,idempotency_key=None,request_id='test')
    with Session(db.engine) as s:
        assert s.get(GenerationTask,row.tid).status=='STOPPED'
        assert s.get(Message,row.mid).metadata_json['diagnosis_confirmation']['status']=='SUPERSEDED'
        assert s.get(GenerationTask,other.tid).status=='WAITING_CONFIRMATION'
        assert s.get(GenerationTask,accepted.task_id).status=='QUEUED'
    context=json.loads(await db.redis.get(f'chat:task_context:{accepted.task_id}'))
    assert context['query']=='设备B在线吗？' and 'diagnosis_choice' not in context
    assert context['pending_diagnosis_context']['query']=='诊断一下设备A'
    with pytest.raises(ConflictError):await db.service.select_diagnosis_mode(row.tid,'A',row.choice['confirmation_id'],'detailed')


@pytest.mark.asyncio
async def test_started_span_recovery_and_idempotent_terminal(db):
    row=db.seed(status='COMPLETED');span=str(uuid4())
    await db.events.publish(row.tid,'performance.span.started',{'code':'supervisor.entity_intake.llm'},span_id=span)
    await db.events.close_open_spans(row.tid)
    await db.events.close_open_spans(row.tid)
    await db.events.publish(row.tid,'performance.span.completed',{},span_id=span)
    rows=await db.events.list_task_events(row.tid)
    assert [r.event_type for r in rows]==['performance.span.started','performance.span.completed']


@pytest.mark.asyncio
@pytest.mark.parametrize('include_reasoning',[False,True])
async def test_sse_replay_drains_multiple_pages_and_preserves_legacy_final_alias(db,include_reasoning):
    row=db.seed(status='COMPLETED')
    for i in range(106):
        await db.events.publish(row.tid,'answer.reasoning.delta',{'channel':'reasoning','content':f'思考{i}。'})
    await db.events.publish(row.tid,'answer.delta',{'channel':'final','content':'最后一页的正文'})
    await db.events.publish(row.tid,'task.completed',{'task_id':str(row.tid)})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=db.app),base_url='http://test') as c:
        response=await c.get(f'/chat/v1/tasks/{row.tid}/events',params={'X-User-Token':'A',
            'include_reasoning':str(include_reasoning).lower(),'final_delta_event':'agent.output.delta'})
    assert response.status_code==200 and '最后一页的正文' in response.text
    assert 'event: agent.output.delta' in response.text and 'event: task.completed' in response.text
    assert ('event: answer.reasoning.delta' in response.text) is include_reasoning


@pytest.mark.asyncio
async def test_answer_snapshot_is_not_cut_by_process_log_text_limit(db):
    row=db.seed(status='COMPLETED');text='完整回答。'*8000
    await db.events.publish(row.tid,'answer.completed',{'content':text,'replace':True,'api_key':'redact-this'})
    await db.events.publish(row.tid,'answer.delta',{'content':'[1, 2, 3]'})
    rows=await db.events.list_task_events(row.tid)
    assert rows[0].payload['content']==text and rows[0].payload['api_key']!='redact-this'
    assert rows[1].payload['content']=='[1, 2, 3]'


@pytest.mark.asyncio
async def test_generic_point_diagnosis_pauses_before_any_raw_data_or_rpm():
    nodes,asset,model,calls=make_nodes();rt=make_runtime();state=initial(rt);state.pop('diagnosis_choice')
    outcome=await run(nodes,state,rt)
    assert asset.lookup.await_count==1 and outcome['final_status']=='WAITING_CONFIRMATION' and not calls
    assert '10 分钟' in outcome['final_answer']


@pytest.mark.asyncio
async def test_confirmation_resumes_selected_identity_without_classifier_or_search():
    nodes,asset,model,calls=make_nodes();rt=make_runtime()
    original=initial(rt);original.pop('diagnosis_choice')
    waiting=await run(nodes,original,rt)
    from app.orchestration.diagnosis_choice import resume_state
    resumed={**original,'diagnosis_resume_state':resume_state(waiting),'diagnosis_choice':{'source':'user_confirmation','mode':'detailed'}}
    answer=await run(nodes,resumed,rt)
    assert answer['final_status']=='COMPLETED' and len(calls)==3
    assert asset.lookup.await_count==1


@pytest.mark.asyncio
async def test_plain_text_choice_reuses_prior_target_only_when_semantically_confirmed():
    nodes,asset,model,calls=make_nodes();rt=make_runtime()
    saved={'query':'诊断一下17架轧机的减速机测点','resolved_entity':point(),
        'business_intent':copy.deepcopy(FIXTURE['classification']),'business_workflow':{'workflow_id':'diagnosis_analysis','variant_id':'point'}}
    classification=copy.deepcopy(FIXTURE['classification'])
    classification.update(analysis_depth='quick',analysis_depth_explicit=True,analysis_depth_confidence=.99,responds_to_diagnosis_choice=True)
    model.complete_json_profile.return_value=classification
    with graph_runtime_scope(rt):
        result=await nodes.resolve_entity_context({'task_id':rt.agent_runtime.task_id,'query':'快速分析吧',
            'pending_diagnosis_context':{'query':saved['query'],'target':point(),'resume_state':saved}})
    assert result['resolved_entity']==point() and result['business_workflow']=={} and depth(result)=='quick'
    assert asset.lookup.await_count==0 and not calls


@pytest.mark.asyncio
async def test_device_recipe_gate_and_quick_tool_boundary():
    nodes,_,_,calls=make_nodes();rt=make_runtime()
    state={'business_intent':{'goal_frame':{'diagnosis_requested':True}},
        'business_workflow':{'workflow_id':'diagnosis_analysis','variant_id':'device'},'resolved_entity':{'equip_no':'REAL'}}
    from app.orchestration.supervisor.contracts import AgentCall
    with graph_runtime_scope(rt):
        result=await nodes.normal_react_decide(state)
        assert result['final_status']=='WAITING_CONFIRMATION' and not calls
        quick={**state,'diagnosis_choice':{'source':'user_confirmation','mode':'quick'}}
        quick=apply_depth(quick)
        assert not quick['business_workflow'] and not quick['business_intent']['goal_frame']['diagnosis_requested']
        for tool in sorted(EXPENSIVE_TOOLS):
            response=await nodes._execute_call(quick,AgentCall(call_id='test',call_type='tool',tool_id=tool,objective='test'))
            assert response['observations'][0]['error_code']=='ANALYSIS_DEPTH_REQUIRED'
    assert not calls
