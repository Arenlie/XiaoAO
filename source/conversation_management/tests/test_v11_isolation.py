import asyncio
from types import SimpleNamespace
from uuid import uuid4

import fakeredis.aioredis
import pytest
from unittest.mock import AsyncMock

from app.config import Settings
from app.domain.exceptions import ConflictError
from app.services.concurrency_service import ConcurrencyService
from app.services.idempotency import IdempotencyGuard
from app.tools.registry import ToolRegistry
from app.tools.contracts import ToolDescriptor, ToolCallRequest, ToolCallResult, ToolProviderType
from app.tools.providers.local import LocalToolProvider
from app.execution.resilient_tool_executor import ResilientToolExecutor


@pytest.fixture
def settings():
    return Settings.model_construct(max_active_generations_per_user=3,max_active_generations_global=20,
        agent_default_max_retries=0,agent_circuit_breaker_threshold=1)


@pytest.mark.asyncio
async def test_old_release_does_not_remove_new_lease_and_release_is_idempotent(settings):
    redis=fakeredis.aioredis.FakeRedis(decode_responses=True)
    service=ConcurrencyService(redis,settings);conv=uuid4()
    old=await service.acquire(conv,'A')
    await service.release(conv,'A',old)
    new=await service.acquire(conv,'A')
    await service.release(conv,'A',old)
    await service.release(conv,'A') # legacy release is fenced too
    assert await redis.get(f'chat:lock:conversation:{conv}')==new
    assert await redis.zcard(service._keys(conv,'A')[1])==1
    await service.release(conv,'A',new)
    await service.release(conv,'A',new)
    assert await redis.zcard(service._keys(conv,'A')[1])==0
    await redis.aclose()


@pytest.mark.asyncio
async def test_same_user_parallel_conversations_and_same_conversation_busy(settings):
    r=fakeredis.aioredis.FakeRedis(decode_responses=True);s=ConcurrencyService(r,settings)
    c1,c2,c3=uuid4(),uuid4(),uuid4()
    one,two,three=await asyncio.gather(s.acquire(c1,'A'),s.acquire(c2,'A'),s.acquire(c3,'B'))
    with pytest.raises(ConflictError):await s.acquire(c1,'A')
    assert len({one,two,three})==3
    assert await r.zcard(s._keys(c1,'A')[1])==2
    assert await r.zcard(s._keys(c3,'B')[1])==1
    assert not await s.renew(c1,'A','wrong')
    assert await s.renew(c1,'A',one)
    await r.aclose()


def guard(redis,*,session='browser-A',conv=None,user='A',payload='x'):
    return IdempotencyGuard(redis,user=user,app='phm',client_session=session,conversation=conv,
                             key='request-1',payload={'content':payload},ttl=60)


@pytest.mark.asyncio
async def test_idempotency_separates_sessions_conversations_users_and_rejects_changed_body():
    r=fakeredis.aioredis.FakeRedis(decode_responses=True)
    g=guard(r);assert await g.begin() is None
    await g.finish({'task_id':'correct-task'})
    assert (await guard(r).begin())['task_id']=='correct-task'
    for kwargs in [{'session':'browser-B'},{'conv':'other'},{'user':'B'}]:
        assert await guard(r,**kwargs).begin() is None
    with pytest.raises(ConflictError):await guard(r,payload='different').begin()
    await r.aclose()


@pytest.mark.asyncio
async def test_duplicate_initial_submissions_have_single_reservation():
    r=fakeredis.aioredis.FakeRedis(decode_responses=True)
    outcomes=await asyncio.gather(*(guard(r).begin() for _ in range(12)),return_exceptions=True)
    assert outcomes.count(None)==1
    assert sum(isinstance(x,ConflictError) for x in outcomes)==11
    await r.aclose()


@pytest.mark.asyncio
async def test_bad_user_tool_failure_does_not_open_other_users_circuit(settings):
    reg=ToolRegistry();provider=LocalToolProvider()
    async def handle(req,token):
        return ToolCallResult(tool_id=req.tool_id,status='FAILED' if req.user_token=='bad' else 'SUCCESS',
                              error_code='HTTP_TOOL_FAILED' if req.user_token=='bad' else None)
    provider.register('test.read',handle);reg.register_provider(ToolProviderType.LOCAL,provider)
    reg.register_tool(ToolDescriptor(tool_id='test.read',display_name='测试读取',description='test',provider_type='local'))
    exe=ResilientToolExecutor(reg,settings)
    async def call(user):
        runtime=SimpleNamespace(user_token=user,data_access_token='credential-'+user,settings=settings,
                                request_id='r',task_id='t',execution_mode='normal',event_service=SimpleNamespace(publish=AsyncMock()))
        req=ToolCallRequest(tool_id='test.read',task_id='t',conversation_id='c',branch_id='b',user_token=user)
        return await exe.execute(request=req,runtime=runtime,parent_span_id=None)
    assert (await call('bad')).status.value=='FAILED'
    assert (await call('bad')).error_code=='TOOL_CIRCUIT_OPEN'
    assert (await call('good')).status.value=='SUCCESS'
