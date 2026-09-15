import pytest
from pydantic import ValidationError
from app.providers.sensor_agent import SensorError
from app.schemas.sensor import MonitoredPointsQuery, SensorQuery
from test_sensor_queries import h, fault
from test_sensor_registry import registry, point


@pytest.mark.parametrize('count', [1001, 14999, 15000])
async def test_registry_after_first_thousand_is_searchable_without_n_plus_one(h, count):
    rows = [point(str(n), equip='OTHER') for n in range(count - 1)]
    rows.append(point('TARGET', equip='ASKED-DEVICE'))
    registry(h, rows)
    result = await h.service.monitored_points(MonitoredPointsQuery(equip_num='ASKED-DEVICE', limit=1))
    assert result['records'][0]['point_num'] == 'TARGET'
    assert result['summary']['registry_complete'] is True
    assert result['summary']['returned_by_upstream'] == count
    assert result['summary']['returned_count'] == 1
    assert result['status'] == 'OK'
    h.points.query.assert_not_awaited()
    assert not any(request.url.path.endswith('/state') for request in h.calls)
    assert next(request for request in h.calls if request.url.path.endswith('/admin/points')).url.params['limit'] == '15000'


async def test_partial_15000_registry_does_not_turn_missing_device_into_not_monitored(h):
    registry(h, [point(str(n), equip='OTHER') for n in range(15000)], total=15001)
    result = await h.service.monitored_points(MonitoredPointsQuery(equip_num='MISSING'))
    assert result['status'] == 'PARTIAL'
    assert result['summary']['registry_complete'] is False
    assert result['summary']['truncated'] is True
    assert result['records'] == []


async def test_registry_larger_than_requested_limit_is_valid_when_total_and_budget_match(h):
    registry(h, [point(str(n)) for n in range(15001)])
    result = await h.service.monitored_points(MonitoredPointsQuery())
    assert result['summary']['registry_complete'] is True
    assert result['summary']['returned_by_upstream'] == 15001
    assert result['summary']['output_limited'] is True


@pytest.mark.parametrize('count, capped', [(1000, False), (14999, False), (15000, True)])
async def test_fault_truncation_remains_separate_from_output_and_duplicates(h, count, capped):
    h.history = [fault('SHARED-ID', status='AUTO_RECOVERED') for _ in range(count)]
    result = await h.service.query('history', SensorQuery(limit=1))
    assert result['source']['upstream_record_count'] == count
    assert result['source']['truncated_possible'] is capped
    assert result['source']['duplicate_fault_id_count'] == count - 1
    assert len(result['records']) == 1
    assert result['source']['output_limited'] is True
    request = next(request for request in h.calls if request.url.path.endswith('/faults'))
    assert request.url.params['limit'] == '15000'


def test_display_limits_are_unchanged():
    assert MonitoredPointsQuery().limit == 1000
    assert SensorQuery().limit == 50
    with pytest.raises(ValidationError):
        MonitoredPointsQuery(limit=1001)
    with pytest.raises(ValidationError):
        SensorQuery(limit=201)
