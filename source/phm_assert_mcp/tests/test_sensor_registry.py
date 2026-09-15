"""Registry documentation examples + controlled boundaries, not a live service test."""
import copy

import httpx
import pytest

from app.providers.sensor_agent import SensorError
from app.schemas.sensor import SensorQuery, MonitoredPointsQuery
from test_sensor_queries import h, fault, offline


def point(code='LOGICAL-1', equip='DEV-1', status='ONLINE', wave=True):
    return {'equip_no':equip,'equip_name':'13号轧机' if equip=='DEV-1' else '17号轧机',
        'point_no':code,'raw_point_no':code+'-BODY','point_name':'输入轴测点',
        'vibration_point_num':code+'-PHYSICAL-A','temperature_point_num':code+'-PHYSICAL-T',
        'bias_param_num':'','velocity_param_num':code+'-ACTUAL-V','temperature_param_num':code+'-ACTUAL-T',
        'monitor_status':status,'monitor_status_source':'history_cache_freshness',
        'suspended_until_next_sync':status=='SUSPENDED','waveform_enabled':wave,
        'feature_params':[{'param_code':code+'-ACTUAL-T','param_name':'温度','feature_kind':'temperature',
                           'point_no':code+'-FEATURE-T','params_unit':'℃'}]}


def registry(h, rows=None, total=None):
    rows=copy.deepcopy(rows if rows is not None else [point(),point('L-2',status='SUSPENDED',wave=False),point('L-3',equip='DEV-2')])
    h.registry={'success':True,'message':'ok','data':{'summary':{
        'logical_point_count':len(rows) if total is None else total,'feature_param_count':len(rows),
        'waveform_point_count':sum(r.get('waveform_enabled') is True for r in rows),
        'suspended_point_count':sum(r.get('monitor_status')=='SUSPENDED' for r in rows),
        'explicit_offline_point_count':sum(r.get('monitor_status') in {'SUSPENDED','OFFLINE'} for r in rows)},'records':rows}}
    h.offline=[]


async def test_complete_registry_list_uses_one_get_no_point_states_or_asset_catalog(h):
    registry(h)
    result=await h.service.monitored_points(MonitoredPointsQuery())
    assert result['status']=='OK' and result['summary']['complete'] is True
    assert result['summary']['total_logical_points']==3
    assert len(result['records'])==3
    assert [r.url.path for r in h.calls]==['/ready','/api/sensor-agent/admin/points']
    h.points.query.assert_not_awaited()


async def test_over_1000_is_partial_before_local_device_filter(h):
    registry(h,[point(str(n),equip='OTHER') for n in range(1000)],total=9086)
    result=await h.service.monitored_points(MonitoredPointsQuery(equip_num='DEV-1'))
    assert result['status']=='PARTIAL' and result['records']==[]
    assert result['summary']['total_logical_points']==9086
    assert result['summary']['returned_by_upstream']==1000
    assert result['summary']['complete'] is False and result['summary']['truncated'] is True
    assert len(h.calls)==2


@pytest.mark.parametrize('code',['LOGICAL-1','LOGICAL-1-BODY','LOGICAL-1-PHYSICAL-A','LOGICAL-1-PHYSICAL-T','LOGICAL-1-FEATURE-T','LOGICAL-1-ACTUAL-T'])
async def test_actual_logical_physical_and_parameter_aliases_are_exact(h,code):
    registry(h)
    result=await h.service.monitored_points(MonitoredPointsQuery(equip_num='DEV-1',point_num=code))
    assert len(result['records'])==1 and result['records'][0]['point_num']=='LOGICAL-1'
    assert result['records'][0]['temperature_param_num']=='LOGICAL-1-ACTUAL-T'
    assert result['records'][0]['bias_param_num']==''
    assert 'lower_limit' not in result['records'][0]['feature_params'][0]
    assert not any(r.url.path.endswith('/state') for r in h.calls)


async def test_code_prefix_cannot_select_another_point_and_state_filter_zero_is_not_unmonitored(h):
    registry(h)
    result=await h.service.monitored_points(MonitoredPointsQuery(equip_num='DEV-1',point_num='LOGICAL'))
    assert result['status']=='NOT_MONITORED'
    result=await h.service.monitored_points(MonitoredPointsQuery(equip_num='DEV-1',point_num='LOGICAL-1',monitor_status='offline'))
    assert result['records']==[] and result['status']=='OK'


@pytest.mark.parametrize('filters,expected',[
    ({'equip_num':'DEV-1'},['LOGICAL-1','L-2']),
    ({'equip_name':'13号','point_name':'输入'},['LOGICAL-1','L-2']),
    ({'monitor_status':'online'},['LOGICAL-1','L-3']),
    ({'monitor_status':'suspended'},['L-2']),
    ({'monitor_status':'OFFLINE'},[]),
    ({'waveform_enabled':False},['L-2']),
    ({'feature_kind':'TEMPERATURE'},['LOGICAL-1','L-2','L-3']),
    ({'feature_kind':'bias'},[]),
])
async def test_filters_respect_raw_status_false_and_actual_configuration(h,filters,expected):
    registry(h)
    result=await h.service.monitored_points(MonitoredPointsQuery(**filters))
    assert [r['point_num'] for r in result['records']]==expected
    if filters.get('monitor_status')=='suspended':
        row=result['records'][0]
        assert row['monitor_status']=='SUSPENDED' and row['upstream_monitor_status']=='SUSPENDED'
        assert row['monitoring_status']=='OFFLINE'


async def test_missing_optional_configuration_is_unknown_and_not_fabricated(h):
    registry(h,[{'equip_no':'DEV-1','point_no':'MISSING','monitor_status':'ONLINE'}])
    result=await h.service.monitored_points(MonitoredPointsQuery(waveform_enabled=False))
    assert result['status']=='PARTIAL' and result['summary']['filter_unknown_count']==1
    result=await h.service.monitored_points(MonitoredPointsQuery())
    assert result['records'][0]['feature_params'] is None
    assert 'bias_param_num' not in result['records'][0]
    assert result['records'][0]['bias_input_configured'] is None


async def test_output_limit_does_not_hide_source_completeness(h):
    registry(h)
    result=await h.service.monitored_points(MonitoredPointsQuery(limit=1))
    assert len(result['records'])==1 and result['summary']['matched_count']==3
    assert result['summary']['registry_complete'] is True
    assert result['summary']['complete'] is False and result['summary']['truncated'] is False
    assert result['summary']['output_limited'] is True


@pytest.mark.parametrize('body',[
    {'success':True,'data':{'records':[]}},
    {'success':True,'data':{'summary':{},'records':[]}},
    {'success':True,'data':{'summary':{'logical_point_count':'0'},'records':[]}},
    {'success':True,'data':{'summary':{'logical_point_count':True},'records':[]}},
    {'success':True,'data':{'summary':{'logical_point_count':0},'records':[point()]}},
    {'success':True,'data':{'summary':{'logical_point_count':2},'records':[point(),point()]}},
    {'success':True,'data':{'summary':{'logical_point_count':1},'records':[{'point_no':'P'}]}},
])
async def test_bad_registry_cannot_become_empty_complete_list(h,body):
    registry(h)
    h.registry=body
    with pytest.raises(SensorError) as exc:
        await h.service.monitored_points(MonitoredPointsQuery())
    assert exc.value.code=='SENSOR_AGENT_BAD_RESPONSE'


@pytest.mark.parametrize('failure',[(500,{'detail':'error'}),(404,{'detail':'Not Found'}),httpx.ReadTimeout('controlled')])
async def test_list_upstream_errors_are_explicit_and_old_fault_queries_can_fall_back(h,failure):
    h.overrides['/api/sensor-agent/admin/points']=failure
    with pytest.raises(SensorError):
        await h.service.monitored_points(MonitoredPointsQuery())
    result=await h.service.query('active',SensorQuery(equip_num='DEV-1',fault_type='偏置电压异常'))
    assert result['records'][0]['fault_type_name']=='偏置电压异常'
    assert result['source']['registry_error']


async def test_complete_registry_avoids_device_n_plus_one_and_retains_raw_states(h):
    registry(h)
    result=await h.service.query('monitoring',SensorQuery(equip_num='DEV-1'))
    assert result['coverage']['status']=='MIXED' and result['coverage']['complete'] is True
    assert {r['monitor_status'] for r in result['records']}=={'ONLINE','SUSPENDED'}
    h.points.query.assert_not_awaited()
    assert not any(r.url.path.endswith('/state') for r in h.calls)


async def test_registry_alias_feeds_existing_fault_query_and_bias_configuration_warning(h):
    registry(h)
    result=await h.service.query('active',SensorQuery(equip_num='DEV-1',point_num='LOGICAL-1-PHYSICAL-T',fault_type='偏置电压异常'))
    assert len(result['records'])==1 and result['records'][0]['point_num']=='LOGICAL-1'
    assert any('未配置偏置' in w for w in result['warnings'])
    h.points.query.assert_not_awaited()


async def test_complete_absence_is_unmonitored_but_truncated_absence_is_not(h):
    registry(h)
    result=await h.service.query('monitoring',SensorQuery(equip_num='DEV-1',point_num='OUTSIDE'))
    assert result['status']=='NOT_MONITORED'
    assert result['coverage']['unmonitored_point_count']==1
    assert not any(r.url.path.endswith('/state') for r in h.calls)
    registry(h,total=9086)
    result=await h.service.query('monitoring',SensorQuery(equip_num='DEV-1',point_num='OUTSIDE'))
    assert result['coverage']['status']=='ONLINE'  # Explicit scoped state is positive, despite truncated list.
    assert any('/OUTSIDE/state' in r.url.path for r in h.calls)


async def test_truncated_positive_point_is_verified_but_device_cannot_claim_all_online(h):
    registry(h,[point()],total=9086)
    result=await h.service.query('monitoring',SensorQuery(equip_num='DEV-1',point_num='LOGICAL-1'))
    assert result['status']=='OK' and result['coverage']['complete'] is True
    assert not any(r.url.path.endswith('/state') for r in h.calls)
    result=await h.service.query('monitoring',SensorQuery(equip_num='DEV-1'))
    assert result['status']=='PARTIAL' and result['coverage']['complete'] is False
    assert not any('/LOGICAL-1/state' in r.url.path for r in h.calls)


async def test_same_point_code_on_different_devices_does_not_cross_match(h):
    registry(h,[point(),point(equip='DEV-2',status='SUSPENDED')])
    result=await h.service.query('monitoring',SensorQuery(equip_num='DEV-2',point_num='LOGICAL-1'))
    assert result['records'][0]['equip_num']=='DEV-2'
    assert result['records'][0]['monitor_status']=='SUSPENDED'


async def test_conflicting_registry_and_dashboard_states_are_not_asserted(h):
    registry(h)
    h.offline=[offline()]
    result=await h.service.query('monitoring',SensorQuery(equip_num='DEV-1',point_num='LOGICAL-1'))
    assert result['status']=='PARTIAL' and result['coverage']['source_conflict'] is True
    assert result['records'][0]['monitoring_status']=='UNKNOWN'
    assert result['records'][0]['upstream_monitor_status']=='ONLINE'
    h.offline=[offline('ABSENT')]
    result=await h.service.query('monitoring',SensorQuery(equip_num='DEV-1',point_num='ABSENT'))
    assert result['status']=='PARTIAL' and result['records'][0]['monitored'] is None
    assert result['coverage']['unmonitored_point_count']==0


async def test_new_tool_real_mcp_protocol_and_invalid_filter(h,monkeypatch):
    from test_asset_http_lifecycle import asset_app, initialize, rpc
    registry(h)
    async with asset_app(monkeypatch) as server:
        server.runtime.sensors=h.service
        await initialize(server)
        names={r['name'] for r in (await rpc(server,'tools/list'))['tools']}
        assert {'query_monitored_sensor_points','query_asset_collection'} <= names and len(names)==15
        result=await rpc(server,'tools/call',{'name':'query_monitored_sensor_points','arguments':{'monitor_status':'suspended'}})
        assert result['structuredContent']['records'][0]['monitor_status']=='SUSPENDED'
        result=await rpc(server,'tools/call',{'name':'query_monitored_sensor_points','arguments':{'feature_kind':'imaginary'}})
        assert result['structuredContent']['status']=='INVALID_QUERY_PARAMETER'
