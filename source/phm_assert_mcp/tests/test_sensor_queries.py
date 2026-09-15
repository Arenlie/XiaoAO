"""Replay the collected API contract; synthetic cases exercise negative semantics."""
import asyncio
import copy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from app.config import Settings
from app.providers.sensor_agent import SensorAgentClient, SensorError
from app.services.sensor_query_service import SensorQueryService
from app.schemas.sensor import SensorQuery

SAMPLES = json.loads((Path(__file__).parent/'fixtures/sensor_api_samples.json').read_text())["responses"]


def fault(fid='F-1', equip='DEV-1', point='LOGICAL-1', status='PENDING_CONFIRMATION', name='偏置电压异常', model_type=1):
    return {'id':fid,'equip_num':equip,'point_num':point,'param_num':'ACTUAL-PARAM',
            'model_type':model_type,'model_name':name,'fault_status':status,
            'start_time':'2026-09-02 10:00:00','end_time':None if status in {'PENDING_CONFIRMATION','PENDING_REPAIR'} else '2026-09-02 12:00:00'}


def offline(point='LOGICAL-1', equip='DEV-1'):
    return {'equip_num':equip,'point_num':point,'point_name':'测试测点',
            'vibration_point_num':'PHYSICAL-A','temperature_point_num':'PHYSICAL-T',
            'monitor_status':'SUSPENDED','monitor_status_source':'history_cache_freshness'}


class Harness:
    def __init__(self, **settings):
        self.settings = Settings.model_construct(sensor_agent_base_url='http://sensor.test', sensor_agent_cache_ttl_seconds=0,
                                                sensor_agent_max_retries=0, **settings)
        self.calls = []
        self.overrides = {}
        self.active = [fault(), fault('F-2',point='OTHER',name='温度异常',model_type=3)]
        self.history = [fault('F-H', status='AUTO_RECOVERED')]
        self.offline = [offline()]
        self.states = {}
        self.rules = copy.deepcopy(SAMPLES['rules'])
        self.registry = None  # Pre-1.2.1 replay has no collected registry response.
        self.client = SensorAgentClient(self.settings, transport=httpx.MockTransport(self.respond))
        self.points = SimpleNamespace(query=AsyncMock(return_value=({'equip_name':'测试设备'}, [{'point_no':'LOGICAL-1'},{'point_no':'ONLINE-POINT'}], False)))
        self.service = SensorQueryService(self.settings, self.client, self.points)

    async def respond(self, request):
        path = request.url.path
        self.calls.append(request)
        if path in self.overrides:
            value = self.overrides[path]
            if isinstance(value, Exception):
                raise value
            return httpx.Response(value[0], json=value[1]) if not isinstance(value[1], bytes) else httpx.Response(value[0], content=value[1])
        if path == '/ready':
            return httpx.Response(200,json={'success':True,'status':'READY'})
        if path.endswith('/faults'):
            assert set(request.url.params) == {'only_active','limit'}
            rows = self.active if request.url.params['only_active']=='true' else self.history
            return httpx.Response(200,json={'success':True,'data':{'records':rows}})
        if path.endswith('/dashboard/sensors'):
            assert request.url.params['include_simulation']=='false'
            return httpx.Response(200,json={'success':True,'data':{'configured_sensor_count':3,'monitored_sensor_count':2,
                'offline_sensor_count':len(self.offline),'offline_sensor_list':self.offline,'snapshot_time':'2026-09-08 19:05:54'}})
        if path.endswith('/admin/rules'):
            return httpx.Response(200,json=self.rules)
        if path.endswith('/admin/points'):
            assert dict(request.url.params) == {'limit': '15000'}
            return httpx.Response(200,json=self.registry) if self.registry is not None else httpx.Response(404,json={'detail':'Not Found'})
        if path.endswith('/state'):
            point = path.split('/')[-2]
            equip = request.url.params['equip_num']
            status, payload = self.states.get(point, (200, {'success':True,'data':{'point':{
                'point_no':point,'equip_no':equip,'point_name':'在线测试点','monitor_status_source':'history_cache_freshness'},'monitor_status':'ONLINE'}}))
            return httpx.Response(status,json=payload)
        if path.endswith('/evidence'):
            return httpx.Response(200,json={'success':True,'data':{**fault(), 'rule_code':'BIAS_VOLTAGE_ABNORMAL'}})
        raise AssertionError(path)


@pytest.fixture
async def h():
    harness = Harness()
    yield harness
    await harness.client.close()


@pytest.mark.parametrize('name',['偏置电压异常','偏置电压','偏执电压异常','BIAS_VOLTAGE_ABNORMAL'])
async def test_specific_bias_type_filters_exactly_and_no_evidence_n_plus_one(h,name):
    result=await h.service.query('active',SensorQuery(equip_num='dev-1',fault_type=name))
    assert [r['fault_type_name'] for r in result['records']]==['偏置电压异常']
    assert result['records'][0]['rule_code']=='BIAS_VOLTAGE_ABNORMAL'
    assert result['coverage']['status']=='MIXED'
    assert all(r.method=='GET' for r in h.calls)
    assert not any('/evidence' in r.url.path for r in h.calls)


async def test_actual_collected_rule_names_and_logical_mapping(h):
    h.active=copy.deepcopy(SAMPLES['active_faults']['data']['records'])
    h.offline=copy.deepcopy(SAMPLES['dashboard_sensors']['data']['offline_sensor_list'])
    result=await h.service.query('active',SensorQuery(fault_type='偏置电压异常'))
    assert result['records']
    assert all(r['fault_type_name']=='偏置电压异常' for r in result['records'])
    p=SAMPLES['point_state_01']['data']['point']
    h.offline=[]
    h.states[p['point_no']]=(200,copy.deepcopy(SAMPLES['point_state_01']))
    state=await h.service.query('monitoring',SensorQuery(equip_num=p['equip_no'],point_num=p['point_no']))
    assert state['records'][0]['monitoring_status']=='OFFLINE'
    assert state['records'][0]['point_num']==p['point_no']
    assert 'offline_minutes' not in json.dumps(state)


@pytest.mark.parametrize('status,payload,expected',[
    (404,{'detail':'point not found'},'NOT_MONITORED'),
    (200,{'success':False,'code':'POINT_NOT_FOUND','message':'测点不存在'},'NOT_MONITORED'),
    (404,{'detail':'Not Found'},'UNKNOWN'),
    (403,{'detail':'not authorized'},'UNKNOWN'),
    (200,{'success':True,'data':None},'UNKNOWN'),
    (503,{'success':False},'UNKNOWN'),
])
async def test_unmonitored_requires_explicit_point_negative_not_missing_offline(h,status,payload,expected):
    h.active=[];h.offline=[]
    h.states['ABSENT']=(status,payload)
    result=await h.service.query('active',SensorQuery(equip_num='DEV-1',point_num='ABSENT',fault_type='偏置电压异常'))
    record=result['coverage']['records'][0]
    assert record['monitoring_status']==expected
    assert record['monitored'] is (False if expected=='NOT_MONITORED' else None)
    assert result['status']==('NOT_MONITORED' if expected=='NOT_MONITORED' else 'PARTIAL')
    assert '不能' in result['message'] or '不等同' in result['message']


async def test_empty_faults_do_not_prove_online_and_offline_does_not_mean_unmonitored(h):
    h.active=[]
    result=await h.service.query('active',SensorQuery(equip_num='DEV-1',point_num='LOGICAL-1'))
    assert result['coverage']['status']=='OFFLINE'
    assert result['coverage']['records'][0]['monitored'] is True
    assert result['summary']['published_fault_found'] is False


async def test_temperature_alias_uses_observed_mapping_never_suffix_guessing(h):
    result=await h.service.query('active',SensorQuery(equip_num='DEV-1',point_num='PHYSICAL-T'))
    assert len(result['records'])==1
    assert result['records'][0]['point_num']=='LOGICAL-1'
    assert not any('/state' in r.url.path for r in h.calls)


async def test_live_state_maps_catalog_alias_after_logical_only_endpoint_rejects(h):
    h.offline=[]
    h.points.query.return_value=({},[{'point_no':'LOGICAL'},{'point_no':'ACTUAL-T'}],False)
    h.states['ACTUAL-T']=(404,{'detail':'point not found'})
    h.states['LOGICAL']=(200,{'success':True,'data':{'point':{'equip_no':'DEV-1','point_no':'LOGICAL','temperature_point_num':'ACTUAL-T'},'monitor_status':'ONLINE'}})
    result=await h.service.query('monitoring',SensorQuery(equip_num='DEV-1'))
    assert result['coverage']['checked_logical_points']==1
    assert result['coverage']['unmonitored_point_count']==0
    single=await h.service.query('monitoring',SensorQuery(equip_num='DEV-1',point_num='ACTUAL-T'))
    assert single['records'][0]['point_num']=='LOGICAL'
    assert single['records'][0]['monitored'] is True


async def test_scopes_do_not_mix_identical_point_numbers_on_different_devices(h):
    h.active=[fault(equip='DEV-2')]
    a,b=await asyncio.gather(*(h.service.query('active',SensorQuery(equip_num=d,point_num='LOGICAL-1')) for d in ('DEV-1','DEV-2')))
    assert a['records']==[]
    assert b['records'][0]['equip_num']=='DEV-2'
    assert a['coverage']['status']=='OFFLINE'
    assert b['coverage']['status']=='ONLINE'


async def test_mismatched_identity_never_accepted(h):
    h.states['X']=(200,{'success':True,'data':{'point':{'equip_no':'OTHER','point_no':'X'},'monitor_status':'ONLINE'}})
    result=await h.service.query('monitoring',SensorQuery(equip_num='DEV-1',point_num='X'))
    assert result['records'][0]['monitoring_status']=='UNKNOWN'


async def test_device_partial_monitoring_and_no_catalog_do_not_claim_full_online(h):
    h.states['ONLINE-POINT']=(404,{'detail':'point not found'})
    result=await h.service.query('monitoring',SensorQuery(equip_num='DEV-1'))
    assert result['coverage']['unmonitored_point_count']==1
    assert '未纳入监测' in result['message']
    h.points.query.return_value=({},[],False); h.offline=[]
    result=await h.service.query('monitoring',SensorQuery(equip_num='DEV-1'))
    assert result['status']=='PARTIAL' and result['coverage']['status']=='UNKNOWN'


async def test_history_limit_duplicates_and_timezone_are_visible(h):
    h.history=[fault('REUSED',status='AUTO_RECOVERED') for _ in range(15000)]
    h.history[-1]['end_time']='2026-09-02 13:00:00'
    result=await h.service.query('history',SensorQuery(fault_type='偏置电压异常',start_time_from='2026-09-02T02:00:00Z',start_time_to='2026-09-02T03:00:00Z',limit=2))
    assert result['source']['truncated_possible'] is True
    assert result['source']['duplicate_fault_id_count']==14999
    assert result['summary']['matched_count_in_fetched']==15000
    assert len(result['records'])==2
    assert result['records'][0]['start_time'].endswith('+08:00')


async def test_type_changes_use_live_names_not_static_model_number_mapping(h):
    h.rules['data']['records'][0]['model_name']='现场偏压检测'
    h.active[0]['model_name']='现场偏压检测'
    result=await h.service.query('active',SensorQuery(fault_type='偏置电压'))
    assert result['records'][0]['fault_type_name']=='现场偏压检测'
    with pytest.raises(SensorError,match='类型'):
        await h.service.query('active',SensorQuery(fault_type='轴承内圈损伤'))


@pytest.mark.parametrize('status,body',[(503,{'success':False}),(200,{'success':False,'data':{'records':[]}}),(200,{'success':True,'data':{}}),(200,b'not json')])
async def test_upstream_failure_is_error_not_empty_faults(h,status,body):
    h.overrides['/api/sensor-agent/faults']=(status,body)
    with pytest.raises(SensorError):
        await h.service.query('active',SensorQuery())


async def test_ready_failure_and_timeout_and_response_size_are_explicit(h):
    h.overrides['/ready']=(503,{'success':False})
    with pytest.raises(SensorError) as e:
        await h.service.query('active',SensorQuery())
    assert e.value.code=='SENSOR_AGENT_NOT_READY'
    h.overrides['/ready']=httpx.ReadTimeout('controlled timeout')
    with pytest.raises(SensorError) as e:
        await h.service.query('active',SensorQuery())
    assert e.value.code=='SENSOR_AGENT_TIMEOUT'
    h.settings.sensor_agent_max_response_mb=1
    h.overrides['/ready']=(200,b' '*1048577)
    with pytest.raises(SensorError) as e:
        await h.service.query('active',SensorQuery())
    assert e.value.code=='SENSOR_AGENT_BAD_RESPONSE'


async def test_cache_copy_isolation_and_shared_concurrency_limit():
    h=Harness(sensor_agent_max_parallel=2)
    h.client.cache.ttl=10
    current=peak=0
    original=h.respond
    async def slow(request):
        nonlocal current,peak
        current+=1;peak=max(current,peak)
        await asyncio.sleep(.01)
        try:return await original(request)
        finally:current-=1
    h.client.transport=httpx.MockTransport(slow)
    try:
        results=await asyncio.gather(*(h.client.dashboard() for _ in range(8)))
        assert len(h.calls)==1
        results[0]['body']['data']['offline_sensor_list'].clear()
        assert results[1]['body']['data']['offline_sensor_list']
        await asyncio.gather(*(h.client.point_state('DEV-1',str(i)) for i in range(8)))
        assert peak==2
    finally:await h.client.close()


async def test_explicit_evidence_checks_device_ownership(h):
    # The real evidence endpoint uses fault_id; list records use id.
    row = fault()
    row['fault_id'] = row.pop('id')
    row['rule_code'] = 'BIAS_VOLTAGE_ABNORMAL'
    h.overrides['/api/sensor-agent/internal/faults/F-1/evidence'] = (200, {'success': True, 'data': row})
    result=await h.service.fault_evidence('F-1','DEV-1')
    assert result['record']['fault_id']=='F-1'
    assert result['record']['rule_code']=='BIAS_VOLTAGE_ABNORMAL'
    with pytest.raises(SensorError) as e:
        await h.service.fault_evidence('F-1','OTHER')
    assert e.value.code=='SENSOR_IDENTITY_MISMATCH'


async def test_scope_budget_bounds_scheduled_requests_and_reports_unchecked(h):
    h.offline=[]
    h.settings.sensor_agent_scope_timeout_seconds=.005
    h.points.query.return_value=({},[{'point_no':str(i)} for i in range(100)],False)
    original=h.respond
    async def slow(request):
        if request.url.path.endswith('/state'):
            await asyncio.sleep(.05)
        return await original(request)
    h.client.transport=httpx.MockTransport(slow)
    result=await h.service.query('monitoring',SensorQuery(equip_num='DEV-1'))
    assert result['coverage']['unknown_point_count']==100
    assert result['coverage']['complete'] is False
    assert len(h.client.cache.pending) <= h.settings.sensor_agent_max_parallel


async def test_sensor_tool_real_mcp_http_dispatch_and_not_monitored_status(h,monkeypatch):
    from test_asset_http_lifecycle import asset_app, initialize, rpc
    async with asset_app(monkeypatch) as server:
        server.runtime.sensors=h.service
        await initialize(server)
        tools=await rpc(server,'tools/list')
        assert {'query_active_sensor_faults','query_sensor_monitoring_status','get_sensor_fault_evidence'} <= {t['name'] for t in tools['tools']}
        result=await rpc(server,'tools/call',{'name':'query_active_sensor_faults','arguments':{'equip_num':'DEV-1','fault_type':'偏置电压异常'}})
        assert result['structuredContent']['records'][0]['fault_type_name']=='偏置电压异常'
        h.states['ABSENT']=(404,{'detail':'point not found'})
        result=await rpc(server,'tools/call',{'name':'query_sensor_monitoring_status','arguments':{'equip_num':'DEV-1','point_num':'ABSENT'}})
        assert result['structuredContent']['status']=='NOT_MONITORED'
        invalid=await rpc(server,'tools/call',{'name':'query_active_sensor_faults','arguments':{'point_num':'ABSENT'}})
        assert invalid['structuredContent']['success'] is False


async def test_asset_identity_enrichment_uses_exact_returned_codes_and_never_changes_sensor_fact_success():
    from app.domain.entities import Candidate
    harness = Harness()
    candidate = Candidate(entity_type='point', entity_key='point:1', display_name='13号轧机输入端测点',
                          equip_no='DEV-1', point_no='LOGICAL-1', search_text='',
                          metadata={'point_name':'13号轧机输入端测点','equip_name':'13号轧机','space_path':'第一炼钢事业部/棒材线'},
                          vector_score=1.0, source='code_exact', extra={})
    catalog = SimpleNamespace(lookup_code_tokens=AsyncMock(return_value=[candidate]))
    harness.service.catalog = catalog
    try:
        row = {'equip_num':'DEV-1','point_num':'LOGICAL-1','fault_type_name':'温度异常'}
        meta = await harness.service._enrich_asset_identity([row])
        assert meta == {'attempted': True, 'enriched_count': 1, 'missing_count': 0}
        assert row['point_name'] == '13号轧机输入端测点'
        assert row['equip_name'] == '13号轧机'
        assert row['space_path'] == '第一炼钢事业部/棒材线'
        tokens = catalog.lookup_code_tokens.await_args.args[0]
        assert set(tokens) == {'DEV-1', 'LOGICAL-1'}
    finally:
        await harness.client.close()


async def test_asset_identity_enrichment_failure_is_best_effort_only():
    harness = Harness()
    harness.service.catalog = SimpleNamespace(lookup_code_tokens=AsyncMock(side_effect=RuntimeError('catalog down')))
    try:
        row = {'equip_num':'DEV-1','point_num':'LOGICAL-1','fault_type_name':'温度异常'}
        meta = await harness.service._enrich_asset_identity([row])
        assert meta['attempted'] is True
        assert meta['error'] == 'ASSET_LOOKUP_UNAVAILABLE'
        assert row['point_num'] == 'LOGICAL-1'
        assert 'point_name' not in row
    finally:
        await harness.client.close()
