"""Real HTTP + ORM query tests, with SQLite standing in for PostgreSQL."""
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy import create_engine, event
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api.dependencies import get_container
from app.api.v1.conversations import router
from app.domain.exceptions import AppError
from app.models.conversation import Conversation
from app.models.generation_task import GenerationTask
from app.models.entity_selection import PendingEntitySelection
from app.services.task_service import TaskService


@compiles(JSONB, 'sqlite')
def jsonb_for_sqlite(element, compiler, **kwargs):
    return 'JSON'


@pytest.fixture
def env():
    engine=create_engine('sqlite://',poolclass=StaticPool)
    for table in [Conversation.__table__,GenerationTask.__table__,PendingEntitySelection.__table__]:
        table.create(engine)
    def postgres_timezone(value, context):
        if value.expires_at.tzinfo is None:
            value.expires_at=value.expires_at.replace(tzinfo=UTC)
    event.listen(PendingEntitySelection,'load',postgres_timezone)
    class SessionBridge:
        async def __aenter__(self):
            self.session=Session(engine);return self
        async def __aexit__(self,*args):self.session.close()
        async def execute(self,statement):return self.session.execute(statement)
    service=TaskService(SessionBridge,None,None,None,None,None,None,None,None)
    app=FastAPI();app.include_router(router,prefix='/chat/v1')
    app.dependency_overrides[get_container]=lambda:SimpleNamespace(task_service=service)
    @app.middleware('http')
    async def request_id(request,call_next):
        request.state.request_id='test-request';return await call_next(request)
    @app.exception_handler(AppError)
    async def error(request,exc):
        return JSONResponse({'success':False,'code':exc.code},status_code=exc.status_code)
    def seed(*,owner='user-A',task_status='WAITING_SELECTION',selection_status='PENDING',expired=False,empty=False):
        conversation_id,task_id=uuid4(),uuid4()
        with Session(engine) as session:
            session.add(Conversation(id=conversation_id,user_token=owner,expires_at=datetime.now(UTC)+timedelta(days=1)))
            session.add(GenerationTask(id=task_id,conversation_id=conversation_id,branch_id=uuid4(),
                user_message_id=uuid4(),assistant_message_id=uuid4(),status=task_status))
            session.add(PendingEntitySelection(task_id=task_id,conversation_id=conversation_id,original_query='测试设备',
                candidates=[] if empty else [{'candidate_id':'test-candidate','equip_no':'TEST-E1'}],
                status=selection_status,expires_at=datetime.now(UTC)+timedelta(minutes=-1 if expired else 10)))
            session.commit()
        return conversation_id,task_id
    yield SimpleNamespace(app=app,seed=seed,service=service)
    event.remove(PendingEntitySelection,'load',postgres_timezone);engine.dispose()


async def get(env,conversation,user='user-A'):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=env.app,raise_app_exceptions=False),base_url='http://test') as client:
        return await client.get(f'/chat/v1/conversations/{conversation}/pending-entity-selection',
                                params={} if user is None else {'X-User-Token':user})


@pytest.mark.asyncio
async def test_no_record_is_http_200_with_null_data(env):
    result=await get(env,uuid4())
    assert result.status_code==200
    assert result.json()=={'success':True,'data':None,'message':'ok','request_id':'test-request'}


@pytest.mark.asyncio
@pytest.mark.parametrize('conditions',[
    {'expired':True},{'empty':True},{'task_status':'COMPLETED'},{'selection_status':'SELECTED'},
])
async def test_no_usable_selection_returns_empty_success(env,conditions):
    conversation,_=env.seed(**conditions)
    result=await get(env,conversation)
    assert result.status_code==200 and result.json()['data'] is None


@pytest.mark.asyncio
async def test_valid_selection_preserves_response_shape(env):
    conversation,task=env.seed()
    result=await get(env,conversation);data=result.json()['data']
    assert result.status_code==200 and data['task_id']==str(task)
    assert data['candidates']==[{'candidate_id':'test-candidate','equip_no':'TEST-E1'}]
    assert set(data)=={'task_id','assistant_message_id','status','candidates','expires_at','expires_in_seconds','last_event_id'}


@pytest.mark.asyncio
async def test_other_users_candidate_is_never_returned(env):
    conversation,_=env.seed(owner='user-B')
    result=await get(env,conversation,user='user-A')
    assert result.status_code==200 and result.json()['data'] is None
    assert (await get(env,conversation,user='user-B')).json()['data']['candidates']


@pytest.mark.asyncio
async def test_missing_identity_stays_unauthorized(env):
    assert (await get(env,uuid4(),user=None)).status_code==401


@pytest.mark.asyncio
async def test_database_failure_is_not_disguised_as_empty_success(env):
    def unavailable():raise RuntimeError('database unavailable')
    env.service.session_factory=unavailable
    assert (await get(env,uuid4())).status_code==500
