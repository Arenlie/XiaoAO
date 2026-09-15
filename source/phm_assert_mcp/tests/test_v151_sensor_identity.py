import asyncio
import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from app.config import Settings
from app.domain.entities import Candidate
from app.providers.cache import AsyncMemo
from app.providers.sensor_agent import SensorAgentClient, SensorError
from app.schemas.resolve import ResolveEntityRequest
from app.schemas.sensor import SensorQuery
from app.services.sensor_identity_index import SensorIdentityIndex, resolve_tokens
from app.services.sensor_registry import SensorRegistry
from test_sensor_queries import h, fault
from test_sensor_registry import point, registry
from test_v11_asset import settings, resolver

EQ = 'BBGPG02010160030041'
PT = 'BBGPG0201016003004101G04VA'
PARAM = 'BBGPG0201016003004101G04VT000'


def response(rows, total=None):
    return {'body': {'success': True, 'data': {'summary': {'logical_point_count': len(rows) if total is None else total},
                                            'records': rows}},
            'source': {'endpoint': '/api/sensor-agent/admin/points', 'fetched_at': '2026-09-09T00:00:00+00:00'}}


async def test_refresh_writes_through_and_concurrent_readers_share_the_new_value():
    cache = AsyncMemo(ttl=60)
    await cache.get('p', AsyncMock(return_value={'value': 'old'}))
    count = 0
    async def read():
        nonlocal count
        count += 1
        await asyncio.sleep(.01)
        return {'value': 'new'}
    result = await asyncio.gather(*(cache.get('p', read, refresh=True) for _ in range(25)))
    assert count == 1 and all(r['value'] == 'new' for r in result)
    assert (await cache.get('p', AsyncMock(side_effect=AssertionError())))['value'] == 'new'
    await cache.close()


async def test_live_http_refresh_persists_and_limit_is_configurable():
    version, calls = 1, []
    async def serve(request):
        calls.append(request)
        return httpx.Response(200, json={'success': True, 'data': {'version': version}})
    client = SensorAgentClient(Settings.model_construct(sensor_agent_base_url='http://test',
        sensor_agent_fetch_limit=22000, sensor_agent_cache_ttl_seconds=60), transport=httpx.MockTransport(serve))
    try:
        assert (await client.monitored_points())['body']['data']['version'] == 1
        version = 2
        await client.monitored_points(refresh=True)
        assert (await client.monitored_points())['body']['data']['version'] == 2
        assert len(calls) == 2 and all(r.url.params['limit'] == '22000' for r in calls)
    finally:
        await client.close()


def test_exact_compound_and_real_parameter_aliases_preserve_ambiguity():
    a, b = point(PT, EQ), point('LOGICAL-2', 'DEVICE-2')
    a['temperature_param_num'] = PARAM
    a['raw_point_no'] = b['raw_point_no'] = 'GY000'
    data = SensorRegistry.parse(response([a, b]))
    assert len(resolve_tokens(data, ['GY000'])['targets']) == 2
    assert resolve_tokens(data, [EQ, 'GY000'])['targets'][0]['point_num'] == PT
    assert resolve_tokens(data, [EQ, PARAM])['targets'][0]['point_num'] == PT
    assert not resolve_tokens(data, ['DEVICE-2', PARAM])['targets']
    assert not resolve_tokens(data, [PARAM[:-3]])['targets']


async def test_mapping_failure_preserves_old_complete_identity_with_stale_flag():
    client = SimpleNamespace(monitored_points=AsyncMock(return_value=response([point(PT, EQ)])),
                             settings=SimpleNamespace(sensor_agent_max_parallel=1))
    index = SensorIdentityIndex(client, 300)
    original, error = await index.get()
    assert original.complete and not error
    client.monitored_points.side_effect = SensorError('SENSOR_AGENT_TIMEOUT', '超时')
    old, error = await index.get(refresh=True)
    assert old is original and error['code'] == 'SENSOR_AGENT_TIMEOUT'
    assert not resolve_tokens(old, ['MISSING-1'], stale=True)['absence_verified']
    await index.close()


async def test_partial_snapshot_cannot_replace_last_complete_map_or_prove_absence():
    client = SimpleNamespace(monitored_points=AsyncMock(return_value=response([point(PT, EQ)])),
                             settings=SimpleNamespace(sensor_agent_max_parallel=1))
    index = SensorIdentityIndex(client)
    original, _ = await index.get()
    client.monitored_points.return_value = response([point('OTHER-1', 'OTHER-2')], 9086)
    partial, _ = await index.get(refresh=True)
    assert index.last_complete is original and not partial.complete
    assert not resolve_tokens(partial, [EQ])['absence_verified']
    await index.close()


async def test_identity_cache_does_not_supply_live_online_status(h):
    registry(h, [point(PT, EQ)])
    resolved = await h.service.resolve_identity([PT])
    target = resolved['identity_resolution']['targets'][0]
    assert target['equip_num'] == EQ and 'monitor_status' not in target
    registry(h, [point(PT, EQ, status='SUSPENDED')])
    h.offline = [{'equip_num': EQ, 'point_num': PT, 'monitor_status': 'SUSPENDED'}]
    live = await h.service.query('monitoring', SensorQuery(equip_num=EQ, point_num=PT, refresh=True))
    assert live['records'][0]['monitoring_status'] == 'OFFLINE'
    await h.service.close()


async def test_logical_point_keeps_verified_equipment_when_physical_mapping_is_missing(h):
    registry(h, [point(PT, EQ)])
    equipment = Candidate(entity_type='equipment', entity_key='equipment:501', equip_no=EQ,
                          display_name='实际设备', metadata={})
    h.service.catalog = SimpleNamespace(lookup_code_tokens=AsyncMock(side_effect=[[], [equipment]]))
    result = await h.service.resolve_identity([PT])
    found = result['identity_resolution']
    assert found['targets'][0]['point_num'] == PT
    assert found['asset_entities'][0]['equip_no'] == EQ
    assert not found['asset_entities'][0].get('point_no')
    await h.service.close()


async def test_literal_codes_bypass_every_model_and_never_fuzzy_fallback(settings):
    r = resolver(settings)
    candidate = Candidate(entity_type='point', entity_key='point:1', equip_no=EQ, point_no=PT,
                          display_name='真实点', metadata={})
    r.catalog.lookup_code_tokens = AsyncMock(return_value=[candidate])
    q = ResolveEntityRequest(query=f'{PT} 温度异常 设备 {EQ} 分析这个传感器', required_entity_level='point',
                             active_entity={'equip_no': 'WRONG-2', 'point_no': 'GY000'})
    result = await r.resolve(q)
    assert result.entity.point_no == PT and result.decision['context_used'] is False
    r.catalog.lookup_code_tokens.return_value = []
    missing = await r.resolve(q)
    assert missing.status == 'ENTITY_NOT_FOUND' and not missing.matches
    r.understanding.understand.assert_not_awaited()
    r.embedding.embed.assert_not_awaited()
    r.reranker.rerank.assert_not_awaited()


async def test_full_ai_detail_over_6000_and_wrong_object_rejection(h):
    original = '已有 AI 复核依据。' * 2000
    path = '/api/sensor-agent/internal/faults/F-1/evidence'
    h.overrides[path] = (200, {'success': True, 'data': {**fault(), 'analysis_result': original}})
    detail = await h.service.fault_evidence('F-1', 'DEV-1', 'LOGICAL-1')
    assert detail['record']['analysis_result'] == original
    assert detail['record']['analysis_complete'] is True
    h.active = [{**fault(), 'analysis_result': original}]
    listed = await h.service.query('active', SensorQuery())
    assert len(listed['records'][0]['analysis_result']) == 6000
    assert listed['records'][0]['analysis_complete'] is False
    with pytest.raises(SensorError, match='设备|故障'):
        await h.service.fault_evidence('F-1', 'OTHER-1')
