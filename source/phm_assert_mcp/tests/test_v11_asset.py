import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from app.config import Settings
from app.domain.entities import Candidate
from app.providers.cache import AsyncMemo
from app.providers.embedding import EmbeddingProvider
from app.repositories.catalog_repository import CatalogRepository
from app.schemas.resolve import ResolveEntityRequest
from app.services.entity_resolver import EntityResolver
from app.services.query_understanding import QueryConstraints, QueryUnderstandingService
from app.services.resolution_policy import ResolutionPolicy, context_entity


@pytest.fixture
def settings():
    return Settings.model_construct(postgres_host='unused', postgres_database='unused',
        postgres_user='unused', postgres_password='unused', llm_base_url='http://test/v1',
        llm_api_key='test-only', embedding_base_url='http://test/v1', rerank_base_url='http://test/v1')


def entity(key, name='13号轧机', kind='equipment', score=.9):
    return Candidate(entity_type=kind, entity_key=key, display_name=name, equip_no=key,
                     metadata={'equip_name':name,'space_path':'炼钢/车间'}, final_score=score)


def resolver(settings, q=None):
    catalog=SimpleNamespace(exact_code=AsyncMock(return_value=[]),official_exact_name=AsyncMock(return_value=[]),
        semantic_exact_name=AsyncMock(return_value=[]),vector_search=AsyncMock(return_value=[]))
    understanding=SimpleNamespace(understand=AsyncMock(return_value=q))
    return EntityResolver(settings,catalog,understanding,SimpleNamespace(embed=AsyncMock(return_value=[.1])),
                          SimpleNamespace(rerank=AsyncMock(return_value={})))


@pytest.mark.asyncio
async def test_single_flight_copy_on_read_and_no_stale_failure():
    cache=AsyncMemo(maxsize=3,ttl=60)
    count=0
    async def calc():
        nonlocal count
        count+=1
        await asyncio.sleep(.01)
        return {'values':[1]}
    values=await asyncio.gather(*(cache.get('same',calc) for _ in range(20)))
    assert count==1
    values[0]['values'].append(9)
    assert (await cache.get('same',calc))=={'values':[1]}
    assert cache.coalesced==19
    bad=AsyncMock(side_effect=RuntimeError('bad'))
    for _ in range(2):
        with pytest.raises(RuntimeError):await cache.get('bad',bad)
    assert bad.await_count==2
    await cache.close()


@pytest.mark.asyncio
async def test_cancel_one_waiter_does_not_cancel_other():
    cache=AsyncMemo()
    release=asyncio.Event()
    async def calc():await release.wait();return 3
    one=asyncio.create_task(cache.get('same',calc))
    two=asyncio.create_task(cache.get('same',calc))
    await asyncio.sleep(0)
    one.cancel()
    with pytest.raises(asyncio.CancelledError):await one
    release.set()
    assert await two==3
    await cache.close()


@pytest.mark.asyncio
async def test_cache_context_is_part_of_key_and_entries_bounded():
    c=AsyncMemo(maxsize=2,ttl=60)
    for context in ['A','B','C']:
        assert await c.get({'query':'这台设备','context':context},AsyncMock(return_value=context))==context
    assert len(c.values)<=2
    await c.close()


@pytest.mark.asyncio
async def test_expired_cache_computes_again():
    c=AsyncMemo(ttl=.001)
    calc=AsyncMock(return_value='v')
    await c.get('k',calc)
    await asyncio.sleep(.003)
    await c.get('k',calc)
    assert calc.await_count==2
    await c.close()


@pytest.mark.asyncio
async def test_literal_code_skips_all_models(settings):
    r=resolver(settings)
    r.catalog.exact_code.return_value=[entity('BB1001')]
    result=await r.resolve(ResolveEntityRequest(query='BB1001',required_entity_level='equipment'))
    assert result.status=='RESOLVED' and result.entity.equip_no=='BB1001'
    r.understanding.understand.assert_not_awaited()
    r.embedding.embed.assert_not_awaited()
    r.reranker.rerank.assert_not_awaited()


@pytest.mark.asyncio
async def test_literal_duplicate_names_still_require_selection(settings):
    r=resolver(settings)
    r.catalog.official_exact_name.return_value=[entity('A'),entity('B')]
    result=await r.resolve(ResolveEntityRequest(query='13号轧机',required_entity_level='equipment',limit=1))
    assert result.needs_disambiguation
    r.embedding.embed.assert_not_awaited()


def test_rerank_limit_one_must_not_create_unique(settings):
    r=resolver(settings)
    request=ResolveEntityRequest(query='轧机',required_entity_level='equipment',limit=1)
    q=QueryConstraints(raw_query='轧机',retrieval_query='轧机',lookup_scope='equipment',equipment_keyword='轧机')
    result=r._decide([entity('A'),entity('B')],q,ResolutionPolicy().decide(request,q),1)
    assert result.status=='NEEDS_DISAMBIGUATION' and len(result.matches)==2


def test_typed_space_reference_drops_child_identity():
    request=ResolveEntityRequest(query='这个车间15号',required_entity_level='equipment',active_entity={
        'entity_type':'point','point_no':'P1','equip_no':'OLD','space_id':'S','space_link':'root/S'})
    q=QueryConstraints(raw_query=request.query,retrieval_query='15号',lookup_scope='equipment',
        equipment_keyword='15号',context_reference=True,context_reference_level='space')
    active=context_entity(request,reference_level='space')
    assert active['space_id']=='S' and 'equip_no' not in active and 'point_no' not in active
    assert ResolutionPolicy().decide(request,q).action=='DOWN_DRILL'


def test_reference_equipment_new_point_does_not_reuse_old_point():
    req=ResolveEntityRequest(query='这台设备另一端',required_entity_level='point',active_entity={
        'equip_no':'E','point_no':'OLD','space_id':'S'})
    q=QueryConstraints(raw_query=req.query,retrieval_query='另一端',lookup_scope='point',
        point_keyword='另一端',context_reference=True,context_reference_level='equipment')
    assert ResolutionPolicy().decide(req,q).action=='DOWN_DRILL'


def test_explicit_switch_refresh_and_pronoun_reuse():
    active={'equip_no':'E','space_id':'S'}
    req=ResolveEntityRequest(query='15号轧机',required_entity_level='equipment',active_entity=active)
    q=QueryConstraints(raw_query=req.query,retrieval_query=req.query,lookup_scope='equipment',equipment_keyword=req.query)
    assert ResolutionPolicy().decide(req,q).action=='REFRESH'
    q.equipment_keyword='';q.context_reference=True;q.context_reference_level='equipment'
    assert ResolutionPolicy().decide(req,q).action=='REUSE'


@pytest.mark.asyncio
async def test_verified_upstream_code_skips_asset_llm():
    llm=SimpleNamespace(extract=AsyncMock())
    q=await QueryUnderstandingService(llm).understand('查询BB1001', 'equipment',None,
        semantic_hints={'equip_no':{'raw_text':'BB1001','retrieval_text':'BB1001'},'needs_asset_lookup':True})
    assert q.equip_no=='BB1001'
    llm.extract.assert_not_awaited()


@pytest.mark.asyncio
async def test_unproven_hint_rejected():
    with pytest.raises(Exception):
        await QueryUnderstandingService(SimpleNamespace()).understand('查询15号','equipment',None,
            semantic_hints={'equipment':{'raw_text':'999号','retrieval_text':'999号'}})


@pytest.mark.asyncio
async def test_attachment_name_provenance_accepted():
    q=await QueryUnderstandingService(SimpleNamespace()).understand('查询图中设备','equipment',None,
       conversation_context={'attachment_texts':['铭牌：15号轧机']},
       semantic_hints={'equipment':{'raw_text':'15号轧机','retrieval_text':'15号轧机'}})
    assert q.equipment_keyword=='15号轧机'


@pytest.mark.asyncio
async def test_sql_code_lookup_keeps_area_filters(settings):
    db=SimpleNamespace(fetch=AsyncMock(return_value=[]))
    await CatalogRepository(db,settings).exact_code(equip_no='E',scope='equipment',
         active_space_link='root/S/',area_keywords=['炼钢'])
    sql,*args=db.fetch.call_args.args
    assert "idx.entity_type = 'equipment'" in sql
    assert 'root/S/' in args and '炼钢' in args and 'E' in args
    assert '炼钢' not in sql


@pytest.mark.asyncio
async def test_embedding_cache_and_response_validation(settings):
    settings.embedding_dimension=2
    provider=EmbeddingProvider(settings)
    await provider.client.aclose()
    count=0
    async def handle(req):
        nonlocal count
        count+=1
        return httpx.Response(200,json={'data':[{'index':0,'embedding':[.1,.2]}]})
    provider.client=httpx.AsyncClient(transport=httpx.MockTransport(handle))
    first=await provider.embed('轧机')
    first.append(9)
    assert await provider.embed('轧机')==[.1,.2]
    assert count==1
    await provider.close()


@pytest.mark.asyncio
async def test_bad_vector_not_cached(settings):
    settings.embedding_dimension=2
    p=EmbeddingProvider(settings);await p.client.aclose()
    p.client=httpx.AsyncClient(transport=httpx.MockTransport(lambda req:httpx.Response(200,json={'data':[{'embedding':[1]}]})))
    for _ in range(2):
        with pytest.raises(Exception):await p.embed('v')
    assert p.cache.misses==2
    await p.close()
